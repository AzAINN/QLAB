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
use atlas::fx::Fx;
use atlas::net::http::{self, PollerHandle};
use atlas::net::sse;
use atlas::store::{should_render, Store, TICK};
use atlas::ui::views::Views;
use atlas::ui::widgets::pulse;
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
    time::Instant,
};
use tracing_subscriber::EnvFilter;

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
    let mut fx = Fx::default();
    // The views, built once. Built per keystroke and per frame — which is what
    // this replaced — every selection and crosshair an operator moved was
    // dropped before the frame that would have drawn it.
    let mut views = Views::new();

    // One frame before the first event, or the operator stares at the shell's
    // leftovers until the ticker fires. It is also what publishes the first set
    // of rects, so the rules of the opening snapshot have somewhere to aim.
    let mut last_frame = Instant::now();
    terminal.draw(|f| ui::shell::draw(f, store, &views, &fx, last_frame))?;
    let mut budget = fx.budget(last_frame);

    loop {
        // Two waits, and which one is used is the whole difference between an
        // effect that plays and one that steps. Idle, the loop blocks on the
        // channel and the tick guarantees it keeps waking. Moving, it waits with
        // a timeout instead: `should_render` has always said "paint while
        // effects are active", but nothing woke this loop faster than the 100 ms
        // heartbeat, so a 600 ms reveal arrived in five visible chunks.
        let next = match budget {
            Some(wait) => match tokio::time::timeout(wait, rx.recv()).await {
                Ok(Some(ev)) => Some(ev),
                Ok(None) => return Ok(()), // every producer is gone
                Err(_) => None,            // the effect beat came due first
            },
            None => match rx.recv().await {
                Some(ev) => Some(ev),
                None => return Ok(()),
            },
        };
        // The iteration's one clock read, taken where its work begins. Pacing
        // decides against the instant the caller measured, not against whatever
        // the clock says several statements later.
        let now = Instant::now();
        let mut quit = false;
        if let Some(first) = next {
            quit |= ingest(first, store, poller, &mut views, &mut fx, now);
        }
        // Drain-then-render: fifty quote events coalesce into one repaint.
        while let Ok(ev) = rx.try_recv() {
            quit |= ingest(ev, store, poller, &mut views, &mut fx, now);
        }

        // A decaying flash owes frames nothing else asked for: the 100 ms idle
        // heartbeat would sample a 200 ms step visibly late, and a desk with no
        // other news would freeze the row mid-flash.
        if should_render(store.take_dirty(), fx.active(now), last_frame, now) {
            // Effects are driven by elapsed time rather than by a stamp, and the
            // elapsed they get is the gap between the frames they were actually
            // drawn into — not the wall clock, which would advance through the
            // frames this rule skipped.
            let elapsed = now.saturating_duration_since(last_frame);
            terminal.draw(|f| {
                ui::shell::draw(f, store, &views, &fx, now);
                // After every widget, over the painted buffer. Deliberately not
                // inside `shell::draw`: a golden frame calls `draw` and never
                // this, so no snapshot can capture a half-finished effect.
                let area = f.area();
                fx.process(elapsed, f.buffer_mut(), area);
            })?;
            last_frame = now;
        }
        // Decided after the frame, from the state the frame left behind: an
        // effect that just finished must let the loop go back to blocking.
        budget = fx.budget(now);
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
    views: &mut Views,
    fx: &mut Fx,
    now: Instant,
) -> bool {
    let mut quit = false;
    if let AppEvent::Key(key) = &ev {
        if key.kind == KeyEventKind::Press {
            let before = store.nav.view;
            match ui::shell::on_key(*key, store, views) {
                Some(Command::Quit) => quit = true,
                // The refresh jumps the poll queue instead of fetching inline:
                // a synchronous fetch in this loop froze the client for its
                // duration.
                Some(Command::Refresh) => poller.now(),
                None => {}
            }
            // A view switch is not a desk transition and so is not a `Trigger`:
            // the store diffs what the owner said, and which pane an operator is
            // looking at is not something the owner said. This is the one place
            // that can see the nav move, so the coalesce is fired from here.
            if store.nav.view != before {
                fx.on_view_switch();
            }
        }
    }
    // Read before the fold, because the fold is what sets it. The first
    // snapshot is diffed against nothing and therefore announces the state it
    // arrived in — wanted for the halt and the read, and suppressed for the
    // regime sweep, which would otherwise fight the frame arriving under it.
    let opening = store.last_snapshot_at.is_none();
    // Only these two can move the gauge, and asking on every quote frame would
    // walk the panel's readings fifty times a second for an answer that cannot
    // have changed.
    let scored = matches!(ev, AppEvent::Snapshot(_) | AppEvent::RegimePanel(_));

    for trigger in store.apply(ev, now) {
        // The diff decides *what moved*; `Fx` decides what that looks like. The
        // translation lives beside the effect state rather than in the store so
        // the motion vocabulary can change without touching the diff — and so
        // no renderer ever needs a clock to know how far a flash has decayed or
        // how much of the read has arrived.
        fx.on_trigger(&trigger, now, opening);
        // Logged as well as animated: a transition is then observable during QA
        // rather than only visible for the 300 ms it moves for.
        tracing::debug!(?trigger, "desk transition");
    }

    // The needle is told the new reading here rather than discovering it at
    // draw time, which is one frame late and has no value left to set off from.
    if scored {
        if let Some(score) = pulse::desk_stress_of(store) {
            fx.gauge.set(score, now);
        }
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
