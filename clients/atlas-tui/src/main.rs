//! atlas-tui — a Ratatui client for the qlab owner runtime.
//!
//! Read-only by construction. It talks to the owner over HTTP and has no order
//! path, no registry handle, and no way to acquire either — invariant 3 is
//! preserved by absence rather than by a check that could be removed. Paper
//! execution stays in the Textual client, where the confirm dialog lives.
//!
//! Runs alongside the Textual TUI rather than replacing it. Both read the same
//! `/api/tui` snapshot, so there is no cutover cliff and no window where the
//! desk has two disagreeing faces.
//!
//! Everything that can produce work — the terminal, the animation beat, the
//! owner poll — writes into one bus, and the loop drains it before drawing at
//! most one frame. A burst of events is a frame, not a stampede.

use atlas::bus::{AppEvent, Channel, Tx};
use atlas::cmd::Command;
use atlas::fx::{FlashKey, FlashTracker};
use atlas::net::http::{self, PollerHandle};
use atlas::net::sse;
use atlas::store::{should_render, Store, Trigger};
use atlas::{theme, ui};
use color_eyre::eyre::Result;
use crossterm::{
    cursor,
    event::{Event, EventStream, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use futures_util::StreamExt;
use ratatui::{backend::CrosstermBackend, Terminal};
use std::{
    io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};
use tracing_subscriber::EnvFilter;

/// The animation beat: ticker, throbbers, and the Atlas glyph advance on it.
const TICK: Duration = Duration::from_millis(120);

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();

    // Preamble order is load-bearing. Tracing first, so a failure in anything
    // after it lands on disk instead of into a fullscreen void. Theme second —
    // `init` refuses once a colour has been resolved. Terminal last, so nothing
    // that can fail runs while the screen is taken.
    init_tracing(args.iter().any(|a| a == "-v"))?;
    install_hooks()?;
    theme::init(theme::detect());

    let offline = !args.iter().any(|a| a == "--live");
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
    let base = http::base_from_env();

    // The threshold the frame marks stale numbers at comes from the cadence that
    // refreshes them, so the two cannot drift apart.
    let mut store = Store::new(http::stale_after(http::POLL_INTERVAL));
    // Probe before the screen is taken. The first frame is drawn before any
    // event arrives, and a frame that says "no owner" because it has not asked
    // yet has already lied to the operator once.
    let readiness = http::readiness(&base).await;
    if !readiness.is_ready() {
        tracing::warn!(reason = readiness.reason(), "owner is not reachable");
    }
    store.apply(
        if readiness.is_ready() {
            AppEvent::ConnUp(Channel::Owner)
        } else {
            AppEvent::ConnDown(Channel::Owner)
        },
        Instant::now(),
    );

    let poller = http::spawn_poller(base.clone(), offline, tx.clone());
    // The stream holds the poller so the two feeds are one story: an event that
    // says the desk moved brings the next snapshot forward instead of letting
    // the frame show a plan that already executed for another whole interval.
    sse::spawn_sse(base, tx.clone(), poller.clone());
    spawn_ticker(tx.clone());
    spawn_signal_watch();

    // The guard owns the screen from here: every return path below it, panicking
    // or not, restores through `Drop`.
    let _guard = TerminalGuard::enter()?;
    // After raw mode, not before: a key pressed while the tty is still
    // line-buffered would sit in the kernel until Enter.
    spawn_terminal_events(tx);
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    run(&mut terminal, &mut store, &mut rx, &poller).await
}

async fn run(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    store: &mut Store,
    rx: &mut tokio::sync::mpsc::UnboundedReceiver<AppEvent>,
    poller: &PollerHandle,
) -> Result<()> {
    // Effect state lives here rather than on the `Store`: the store is what the
    // owner said plus the diff of it, and a decaying animation stamp is neither.
    // Keeping it out is what lets a golden frame be a pure function of the store
    // and an instant.
    let mut fx = FlashTracker::default();

    // One frame before the first event, or the operator stares at the shell's
    // leftovers until the ticker fires.
    let mut last_frame = Instant::now();
    terminal.draw(|f| ui::shell::draw(f, store, &fx, last_frame))?;

    loop {
        // Block until something happens. The 120 ms tick is what guarantees the
        // loop keeps waking, so the idle heartbeat below is reachable.
        let Some(first) = rx.recv().await else {
            return Ok(()); // every producer is gone
        };
        // The iteration's one clock read, taken where its work begins. Pacing
        // decides against the instant the caller measured, not against whatever
        // the clock says several statements later.
        let now = Instant::now();
        let mut quit = ingest(first, store, poller, &mut fx, now);
        // Drain-then-render: fifty quote events coalesce into one repaint.
        while let Ok(ev) = rx.try_recv() {
            quit |= ingest(ev, store, poller, &mut fx, now);
        }

        // A decaying flash owes frames nothing else asked for: the 100 ms idle
        // heartbeat would sample a 200 ms step visibly late, and a desk with no
        // other news would freeze the row mid-flash. Task 15 ORs the effect
        // manager's running flag in here.
        if should_render(store.take_dirty(), fx.active(now), last_frame, now) {
            terminal.draw(|f| ui::shell::draw(f, store, &fx, now))?;
            last_frame = now;
        }
        if quit {
            return Ok(());
        }
    }
}

/// Fold one event into the store. Returns whether it quits.
///
/// The runtime is the only thing that acts: the shell decides what a keystroke
/// *means* and hands back a `Command`, and nothing in `ui/` can reach the
/// network or the process lifetime on its own.
fn ingest(
    ev: AppEvent,
    store: &mut Store,
    poller: &PollerHandle,
    fx: &mut FlashTracker,
    now: Instant,
) -> bool {
    let mut quit = false;
    if let AppEvent::Key(key) = &ev {
        if key.kind == KeyEventKind::Press {
            match ui::shell::on_key(*key, store) {
                Some(Command::Quit) => quit = true,
                // The refresh jumps the poll queue instead of fetching inline:
                // a synchronous fetch in this loop froze the client for its
                // duration.
                Some(Command::Refresh) => poller.now(),
                None => {}
            }
        }
    }
    for trigger in store.apply(ev, now) {
        // The diff decides *what moved*; this decides what that looks like. The
        // translation lives here rather than in the store so the motion
        // vocabulary can change without touching the diff — and so no renderer
        // ever needs a clock to know how far a flash has decayed.
        if let Trigger::QuoteTick(ticker) = &trigger {
            fx.flash(FlashKey::price(ticker), now);
        }
        // Task 15 turns the rest into effects. Logging them means the diff has
        // a consumer today and its behaviour is observable during QA.
        tracing::debug!(?trigger, "desk transition");
    }
    quit
}

// -- producers -------------------------------------------------------------

/// Keys and resizes onto the bus, so the loop has exactly one drain point.
fn spawn_terminal_events(tx: Tx) {
    tokio::spawn(async move {
        let mut stream = EventStream::new();
        while let Some(next) = stream.next().await {
            let ev = match next {
                Ok(Event::Key(key)) => AppEvent::Key(key),
                Ok(Event::Resize(_, _)) => AppEvent::Resize,
                Ok(_) => continue,
                // A terminal that stopped producing events cannot be recovered
                // from in here. Say so, and stop rather than spin on the error.
                Err(err) => {
                    tracing::error!(%err, "terminal event stream failed");
                    return;
                }
            };
            if tx.send(ev).is_err() {
                return;
            }
        }
    });
}

fn spawn_ticker(tx: Tx) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(TICK);
        loop {
            interval.tick().await;
            if tx.send(AppEvent::Tick).is_err() {
                return;
            }
        }
    });
}

/// SIGINT and SIGTERM reach the same restore as every other exit.
///
/// Raw mode disables `ISIG`, so these arrive from outside the terminal — a
/// `kill` during a render must not leave the operator's shell in raw mode. The
/// loop cannot be trusted to still be scheduling by then, which is why this
/// restores and exits here rather than asking it to.
fn spawn_signal_watch() {
    tokio::spawn(async move {
        // 128 + signal number, so a supervisor reads back which one arrived.
        #[cfg(unix)]
        let code = {
            use tokio::signal::unix::{signal, SignalKind};
            let (mut term, mut intr) = match (
                signal(SignalKind::terminate()),
                signal(SignalKind::interrupt()),
            ) {
                (Ok(term), Ok(intr)) => (term, intr),
                _ => {
                    tracing::error!("could not install signal handlers");
                    return;
                }
            };
            tokio::select! {
                _ = term.recv() => 143,
                _ = intr.recv() => 130,
            }
        };
        #[cfg(not(unix))]
        let code = {
            if tokio::signal::ctrl_c().await.is_err() {
                return;
            }
            130
        };
        restore();
        std::process::exit(code);
    });
}

// -- terminal lifetime -----------------------------------------------------

/// Set while the alternate screen is held. Three exits share one restore path
/// (`Drop`, the panic hook, a signal); this makes the second and third calls
/// no-ops instead of a double teardown that can un-restore the first.
static ENTERED: AtomicBool = AtomicBool::new(false);

/// The one restore. Errors go to the log because there is nowhere else for them
/// to go — the screen they would print on is the thing that failed.
fn restore() {
    if !ENTERED.swap(false, Ordering::SeqCst) {
        return;
    }
    if let Err(err) = disable_raw_mode() {
        tracing::error!(%err, "could not leave raw mode");
    }
    if let Err(err) = execute!(io::stdout(), LeaveAlternateScreen, cursor::Show) {
        tracing::error!(%err, "could not leave the alternate screen");
    }
}

struct TerminalGuard;

impl TerminalGuard {
    fn enter() -> io::Result<Self> {
        enable_raw_mode()?;
        execute!(io::stdout(), EnterAlternateScreen)?;
        ENTERED.store(true, Ordering::SeqCst);
        Ok(Self)
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        restore();
    }
}

/// color-eyre's panic report, behind a restore.
///
/// A backtrace printed into raw mode is unreadable and the shell it lands in is
/// unusable, so the screen goes back before the report prints.
fn install_hooks() -> Result<()> {
    let (panic_hook, eyre_hook) = color_eyre::config::HookBuilder::default().into_hooks();
    eyre_hook.install()?;
    let panic_hook = panic_hook.into_panic_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore();
        panic_hook(info);
    }));
    Ok(())
}

/// A fullscreen client cannot `println`.
///
/// Warnings and up go to a file — `-v` lowers the bar to debug, `ATLAS_LOG`
/// moves it — so a failure that happened behind the alternate screen is still
/// readable after the client is gone.
///
/// `-v` raises this crate only. A global debug filter buried the two lines that
/// mattered under thirty of the HTTP stack's connection plumbing.
fn init_tracing(verbose: bool) -> io::Result<()> {
    let path = PathBuf::from(std::env::var("ATLAS_LOG").unwrap_or_else(|_| "atlas.log".into()));
    let file = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "ATLAS_LOG must name a file"))?;
    let dir = match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    };
    tracing_subscriber::fmt()
        .with_writer(tracing_appender::rolling::never(dir, file))
        .with_ansi(false)
        .with_env_filter(EnvFilter::new(if verbose {
            "warn,atlas=debug"
        } else {
            "warn"
        }))
        .init();
    Ok(())
}
