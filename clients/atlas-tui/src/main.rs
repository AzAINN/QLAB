//! atlas-tui — a Ratatui client for the qlab owner runtime.
//!
//! It talks to the owner over HTTP and has no registry handle and no way to
//! acquire one. Whether it has an order path is a build question: the
//! `operator` feature is the only thing that compiles one into this crate at
//! all, and it is on by default because the desk this client serves is a desk
//! an operator works. Built with `--no-default-features` there is no
//! `net::write` module, no POST call site, and no `Posture::Operator` for the
//! status line to hold — a monitoring box builds that artifact and cannot be
//! argued, configured, or flagged into writing.
//!
//! In the shipped build, what this window may do is the *desk's* answer, not an
//! argument's: the owner persists a posture, serves it on `/api/tui`, and
//! `store::Posture::from_desk` re-derives the scope from every snapshot. A desk
//! nobody has armed offers nothing, and a desk disarmed from another window
//! disarms this one at the next poll. `--glass` is the operator's own veto on
//! top of that, and it is the only posture fact a launch still carries.
//!
//! What the write path can reach is still the owner's governed API and nothing
//! else: an approval decision, a plan execution that consumes a persisted
//! approval, and Atlas's own controls. There is no raw-order tool here and no
//! agent-reachable execution path — a fill requires a human to type six
//! characters of the plan's `targets_hash` into `ui::widgets::confirm`, and the
//! owner refuses the request regardless unless a matching approval is on record.
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
use atlas::dispatch::Writes;
use atlas::fx::Fx;
// `handoff` itself only in the build that can act on one: the default build
// has no `Command` variant that produces a `Child`, so the module is named
// nowhere in it.
#[cfg(feature = "operator")]
use atlas::handoff;
use atlas::handoff::Child;
use atlas::net::http::{self, PollerHandle};
use atlas::net::sse;
use atlas::store::{should_render, Store, ViewId, TICK};
use atlas::ui::views::Views;
use atlas::ui::widgets::{pulse, toast};
use atlas::{theme, ui};
use color_eyre::eyre::Result;
use crossterm::{
    cursor,
    event::{
        DisableMouseCapture, EnableMouseCapture, Event, EventStream, KeyEventKind, MouseEventKind,
    },
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

    // Before anything reads a flag. The build that removed `--operator` removed
    // the `exit(2)` that refused it too, so `atlas --operator` — and every typo
    // of every flag below — became a silent no-op, which is the mechanism that
    // let the original `--operator` bug go unseen. Invariant 4: refuse loudly.
    if let Err(unknown) = unknown_args(&args) {
        eprintln!(
            "atlas: unrecognised argument{} {}\n\
             accepted: {}\n\
             the desk's armed/read-only posture is the owner's, not a flag's — \
             `--glass` only declines it for this window.",
            if unknown.len() == 1 { "" } else { "s" },
            unknown.join(", "),
            KNOWN_ARGS.join(" "),
        );
        std::process::exit(2);
    }

    let offline = !args.iter().any(|a| a == "--live");
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
    let base = http::base_from_env();

    // The threshold the frame marks stale numbers at comes from the cadence that
    // refreshes them, so the two cannot drift apart.
    let mut store = Store::new(http::stale_after(http::POLL_INTERVAL));
    // The frame says where it is looking. An operator with two desks up — or one
    // owner on a port they did not choose — otherwise has to read a chip that
    // names no host and guess which desk it is about.
    store.base = base.clone();
    // The one posture fact a launch still carries. The desk's own answer is
    // what arms this window — see `store::Posture::from_desk` — and it arrives
    // with the first snapshot, so the client starts glass and stays glass until
    // an owner says otherwise. `--glass` is this window declining an authority
    // the desk may be offering, and it is sticky: no later snapshot revokes it.
    store.forced_glass = args.iter().any(|a| a == "--glass");
    if store.forced_glass {
        tracing::info!("--glass: this window will stay read-only whatever the desk says");
    }
    // Parsed in both builds, and acted on in both: a glass window started with
    // it is shown the door it cannot answer, which is the honest reply to
    // "let me choose" from a window that cannot. Before the first frame, so a
    // run started to choose opens on the question rather than on one frame of a
    // desk the operator has not answered for yet.
    if args.iter().any(|a| a == "--pick") {
        store.pick();
    }
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

    // The write half, built here and only here. Fallibly, and before the screen
    // is taken: a client whose writer could not be built would run looking
    // capable and refuse every key at the moment it mattered. Invariant 4 —
    // refuse loudly rather than degrade quietly. What it may be *used* for is
    // still the desk's answer, re-derived on every snapshot.
    let writes = Writes::new(&base, store.forced_glass, tx.clone())?;

    let poller = http::spawn_poller(base.clone(), offline, tx.clone());
    // Once, at startup, because the startup door names what this desk reads on
    // the step where the operator is choosing the lane it reads it for. The
    // route rides no beat, so without this the door's line would be `--` until
    // somebody opened SETTINGS — which is after the door is gone.
    poller.news();
    // And once for the catalog, for the same reason and on the same route
    // shape: the startup door can open straight on the model question — a desk
    // whose pair was named long ago and whose mind never was — and that list is
    // built from `/api/llm/backends`, which rides no beat either. Without this
    // the only producer of `Command::Backends` is a keystroke, so the first
    // question an operator is ever asked about which mind runs Atlas would
    // offer none until they pressed one. The door keeps its own retry row for
    // when this answer does not arrive (an owner that is not up yet, a fetch
    // that failed) — this is what makes that row the exception rather than the
    // route.
    poller.backends();
    // And once at startup for the rights, which is the *only* moment they are
    // owed one: the file is read when a chat session is launched, so what it
    // says is a fact about this desk before anybody opens SETTINGS, and nothing
    // outside the card that sets them ever moves it. `r` on the pane and the
    // card's own POST are the other two, and there is no third.
    poller.rights();
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
    //
    // The handle is kept, and the sender with it, because the reader has to be
    // stoppable: `/build` hands this same stdin to a child, and a reader still
    // on it steals the operator's keystrokes and then replays them into the
    // desk's command line. See `handoff`.
    let reader = spawn_terminal_events(tx.clone());
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    let outcome = run(
        &mut terminal,
        &mut store,
        &mut rx,
        &poller,
        &writes,
        Reader { handle: reader, tx },
    )
    .await;
    // What quitting did to the child, if it did anything. Read before the
    // guard is dropped and printed after: nothing draws another frame once the
    // loop has returned, and anything printed while the alternate screen is up
    // goes with it. The sentence and the rule that picks it are
    // `pane::quit_note`'s, where both arms have a test.
    #[cfg(feature = "operator")]
    let ending = atlas::pane::quit_note(&store.pty_state());
    drop(_guard);
    #[cfg(feature = "operator")]
    if let Some(said) = ending {
        eprintln!("atlas: {said}");
    }
    outcome
}

/// The stdin reader, held so it can be stopped and started again.
///
/// A struct rather than two loose locals because the two halves are only ever
/// useful together: aborting the task without keeping the sender that respawns
/// it leaves a workstation that has stopped listening to its keyboard.
///
/// Built in both builds so the runtime loop keeps one shape, and read in only
/// one: the default build has no command that can ask for a hand-off, so its
/// reader runs untouched for the life of the process.
#[cfg_attr(not(feature = "operator"), allow(dead_code))]
struct Reader {
    handle: tokio::task::JoinHandle<()>,
    tx: Tx,
}

async fn run(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    store: &mut Store,
    rx: &mut tokio::sync::mpsc::UnboundedReceiver<AppEvent>,
    poller: &PollerHandle,
    writes: &Writes,
    #[cfg_attr(not(feature = "operator"), allow(unused_variables))] mut reader: Reader,
) -> Result<()> {
    // Carried and never stopped in the default build: nothing there can ask for
    // a hand-off, so the reader runs for the life of the process.
    #[cfg(not(feature = "operator"))]
    let _ = &mut reader;
    // Effect state lives here rather than on the `Store`: the store is what the
    // owner said plus the diff of it, and a decaying animation stamp is neither.
    // Keeping it out is what lets a golden frame be a pure function of the store
    // and an instant.
    let mut fx = Fx::default();
    // Beside `Fx` for the same reason: what the owner said is the store's, and a
    // box that disappears after four seconds is not something the owner said.
    let mut toasts = toast::ToastQueue::default();
    // The views, built once. Built per keystroke and per frame — which is what
    // this replaced — every selection and crosshair an operator moved was
    // dropped before the frame that would have drawn it.
    let mut views = Views::new();
    // A sender of this loop's own bus, cloned once: `/cli` starts a child that
    // writes onto it, and the reader that holds the other copy is stopped and
    // restarted around the full-screen hand-off.
    let bus = reader.tx.clone();

    // One frame before the first event, or the operator stares at the shell's
    // leftovers until the ticker fires. It is also what publishes the first set
    // of rects, so the rules of the opening snapshot have somewhere to aim.
    let mut last_frame = Instant::now();
    terminal.draw(|f| ui::shell::draw(f, store, &views, &fx, last_frame))?;
    let mut budget = wake(&fx, &toasts, last_frame);

    loop {
        // Two waits, and which one is used is the whole difference between an
        // effect that plays and one that steps. Idle, the loop blocks on the
        // channel and the tick guarantees it keeps waking. Moving, it waits with
        // a timeout instead: `should_render` has always said "paint while
        // effects are active", but nothing woke this loop faster than the 100 ms
        // heartbeat, so the 600 ms reveal arrived in 11 repaints of up to 96
        // characters against a live owner, rather than the ~30 of 5–29 it takes
        // now. The timeout `Fx::budget` hands back is also what keeps a halted
        // desk from repainting the whole buffer 31 times a second for hours.
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
        // And the one wall-clock read, taken here for the same reason and put
        // on the store as data. An `Instant` is monotonic — it cannot be
        // compared with a stamp the owner wrote — and exactly one row needs
        // that comparison (`format::since`). Every renderer stays a pure
        // function of the store, and a clock this loop never reached would
        // leave the model reading's age permanently `--`.
        store.wall = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .ok()
            .and_then(|since| i64::try_from(since.as_secs()).ok());
        let mut quit = false;
        // At most one hand-off per drain. A burst that somehow carried two
        // would open the second onto a terminal the first has already given
        // back; the last one asked for wins, which is the one the operator is
        // still looking at.
        let mut opening: Option<Child> = None;
        if let Some(first) = next {
            quit |= ingest(
                first,
                store,
                poller,
                writes,
                &bus,
                &mut views,
                &mut fx,
                &mut toasts,
                &mut opening,
                now,
            );
        }
        // Drain-then-render: fifty quote events coalesce into one repaint.
        while let Ok(ev) = rx.try_recv() {
            quit |= ingest(
                ev,
                store,
                poller,
                writes,
                &bus,
                &mut views,
                &mut fx,
                &mut toasts,
                &mut opening,
                now,
            );
        }
        // Before the frame, not after: the child is about to paint over this
        // terminal, so a frame drawn first is a frame nobody sees. The screen
        // is handed over and taken back inside `handoff::run`, which restores
        // on every path including a child that never started.
        #[cfg(feature = "operator")]
        if let Some(child) = opening.take() {
            // What survived the drain: desk news that arrived while the
            // operator was inside Claude. Folded in after the host is dropped,
            // because `ingest` needs the views and the host holds the terminal.
            let mut kept = Vec::new();
            let notes = {
                let mut host = ScreenHost {
                    terminal,
                    reader: &mut reader,
                    rx,
                    kept: &mut kept,
                };
                handoff::run(child, &handoff::launcher(), &mut host)
            };
            for ev in kept {
                quit |= ingest(
                    ev,
                    store,
                    poller,
                    writes,
                    &bus,
                    &mut views,
                    &mut fx,
                    &mut toasts,
                    &mut opening,
                    now,
                );
            }
            // A hand-off cannot open a hand-off: the only events replayed here
            // are the ones the drain kept, and it keeps no keys.
            debug_assert!(opening.is_none());
            for note in notes {
                // A toast, because there is nowhere else: anything printed
                // before the screen comes back is wiped by the alternate
                // screen, and anything after it lands under the frame.
                toasts.push(
                    toast::Toast::new(toast::Level::Warn, "CLAUDE", note),
                    Instant::now(),
                );
            }
            // Painted here rather than left to `should_render`, which diffs
            // against a buffer the child scrolled away: on a quiet desk nothing
            // would be judged to have changed, and the operator would come back
            // from Claude to their own shell's leftovers until the next tick.
            // `Instant::now()` and not `now`: a build is minutes old by here.
            let back = Instant::now();
            terminal.draw(|f| ui::shell::draw(f, store, &views, &fx, back))?;
            last_frame = back;
        }
        #[cfg(not(feature = "operator"))]
        let _ = opening;

        // A decaying flash owes frames nothing else asked for: the 100 ms idle
        // heartbeat would sample a 200 ms step visibly late, and a desk with no
        // other news would freeze the row mid-flash. A visible toast rides the
        // same path: its age counts up and it has to disappear on time, and
        // nothing else on a quiet desk would ask for the frame that does it.
        if should_render(
            store.take_dirty(),
            fx.active(now) || toasts.active(now),
            last_frame,
            now,
        ) {
            // Effects are driven by elapsed time rather than by a stamp, and the
            // elapsed they get is the gap between the frames they were actually
            // drawn into — not the wall clock, which would advance through the
            // frames this rule skipped.
            let elapsed = now.saturating_duration_since(last_frame);
            terminal.draw(|f| {
                ui::shell::draw(f, store, &views, &fx, now);
                // Between the shell and the effects on purpose. After the shell,
                // because a toast is over the frame rather than in it; before
                // the effect pass, so a halted desk's red breath crosses the
                // boxes too — a toast that stayed clean while everything under
                // it reddened would read as belonging to a different client.
                let area = f.area();
                toasts.draw(f, area, now);
                // After every widget, over the painted buffer. Deliberately not
                // inside `shell::draw`: a golden frame calls `draw` and never
                // this, so no snapshot can capture a half-finished effect.
                fx.process(elapsed, f.buffer_mut(), area);
            })?;
            last_frame = now;
            // After the frame, never inside one. The pane's size is known only
            // where the layout is decided, and `Store::pty_resize` takes
            // `&mut Store` precisely so a renderer cannot reshape a process —
            // so the column publishes the rect it drew and this hands it on. A
            // terminal the operator resized needs no special case: the next
            // frame reports the new rect, and a frame that drew no pane
            // publishes an empty one, which resizes nothing.
            #[cfg(feature = "operator")]
            atlas::pane::resized(store, views.pane_inner());
        }
        // Decided after the frame, from the state the frame left behind: an
        // effect that just finished must let the loop go back to blocking.
        budget = wake(&fx, &toasts, now);
        if quit {
            return Ok(());
        }
    }
}

/// How long the loop may sleep, across every lane that owes a frame.
///
/// The toasts are their own lane rather than a fourth flag inside `Fx::budget`:
/// they are not effects, and the queue is state the effect module has no reason
/// to hold. `or_else` is the whole arbitration because every effect cadence is
/// at least as fast as the toast's — see `toast::FRAME`.
fn wake(fx: &Fx, toasts: &toast::ToastQueue, now: Instant) -> Option<Duration> {
    fx.budget(now).or_else(|| toasts.budget(now))
}

/// Fold one event into the store. Returns whether it quits.
///
/// The runtime is the only thing that acts: the shell decides what a keystroke
/// *means* and hands back a `Command`, and nothing in `ui/` can reach the
/// network or the process lifetime on its own.
#[allow(clippy::too_many_arguments)]
fn ingest(
    ev: AppEvent,
    store: &mut Store,
    poller: &PollerHandle,
    writes: &Writes,
    // The desk's own bus, for the one command that starts a child writing onto
    // it. Handed in rather than held by the store: a pane's forwarder is the
    // session's, and it ends when that session's senders drop.
    bus: &Tx,
    views: &mut Views,
    fx: &mut Fx,
    toasts: &mut toast::ToastQueue,
    // Out, not in: `ingest` has no terminal, and a hand-off that happened from
    // inside an event fold would be a child process spawned from the middle of
    // a drain. What it does is record that one was asked for; the loop, which
    // owns the screen, is what acts.
    opening: &mut Option<Child>,
    now: Instant,
) -> bool {
    // Carried and unused in the default build: there is no `Command` variant
    // for it to dispatch there. One loop shape rather than two, because a
    // runtime that forked on a feature is a runtime only one leg ever runs.
    #[cfg(not(feature = "operator"))]
    let _ = writes;
    // Carried and never set in the default build, for the same reason: there is
    // no `Command` variant that could ask for a hand-off there.
    #[cfg(not(feature = "operator"))]
    let _ = opening;
    // And carried unused there too: no key in that build opens a pane, so
    // nothing of this loop's bus is ever handed to a child.
    #[cfg(not(feature = "operator"))]
    let _ = bus;
    let mut quit = false;
    if let AppEvent::Key(key) = &ev {
        if key.kind == KeyEventKind::Press {
            let before = store.nav.view;
            // A line the chat typed is resolved here, where the shell's own
            // resolver has the views it needs, and whatever it produces
            // takes the same arms as a palette line would.
            let command = match ui::shell::on_key(*key, store, views) {
                Some(Command::RunLine(line)) => ui::shell::run_line(&line, store, views),
                other => other,
            };
            match command {
                Some(Command::Quit) => quit = true,
                // Resolved above; a resolver that produced another line would
                // be a grammar that recurses, and this one does not.
                Some(Command::RunLine(_)) => {}
                // The refresh jumps the poll queue instead of fetching inline:
                // a synchronous fetch in this loop froze the client for its
                // duration. On PREDICTORS it also re-asks for the board —
                // `r` refreshes what the operator is looking at, and the board
                // rides no beat that would ever refresh it otherwise.
                Some(Command::Refresh) => {
                    poller.now();
                    if store.nav.view == ViewId::Predictors {
                        poller.predictors();
                    }
                    // Same rule on SETTINGS: `r` refreshes what the operator is
                    // looking at, and neither the news answer nor the method
                    // rides a beat that would ever refresh them otherwise.
                    if store.nav.view == ViewId::Settings {
                        poller.news();
                        poller.method();
                        poller.rights();
                    }
                    // And on VISUALS: the registry is a walk over the owner's
                    // own package, so nothing but a deploy changes it and no
                    // beat would ever re-read it. The *drawing* is not
                    // re-asked — the operator chose which one they wanted, and
                    // `r` must not silently render something else.
                    if store.nav.view == ViewId::Visuals {
                        poller.visuals();
                    }
                }
                // A read, and the only one a keystroke asks for. The store
                // decides whether asking again could learn anything — the
                // owner's own cache window — so a palette opened twice in a
                // second does not probe every daemon twice.
                Some(Command::Backends) if store.wants_backends(now) => poller.backends(),
                // Inside that window the route can only answer out of its own
                // cache. Its own arm rather than a guard falling through: the
                // arm below dispatches to the *writer*, and a read that reached
                // it would be a request this command never meant.
                Some(Command::Backends) => {}
                // The other read a keystroke asks for, and its own arm for the
                // same reason: the arm below dispatches to the *writer*, and a
                // read that reached it would be a request this command never
                // meant. The pane has already recorded what it is waiting on.
                Some(Command::RenderVisual(name)) => poller.visual(&name),
                // The two words that start a child, and they no longer do the
                // same thing. Both are above the dispatch arm for the reason
                // `Backends` is — neither is a request, and a `Command` that
                // fell through to the writer would be one sent to an owner that
                // has no verb for it.
                //
                // `/cli` opens the pane in ATLAS's own column rather than
                // taking the screen, which is the whole of what makes this a
                // session *on* the desk. Acted on here rather than recorded the
                // way the hand-off below is: it needs no screen, and it has to
                // run on the loop that owns the bus the child's bytes arrive
                // on — `Store::open_pty` spawns the task that bridges them, and
                // spawning off the runtime panics.
                //
                // The rect is the column a pane will be drawn in, asked of
                // the layout that will draw it: the desk rail gives its own up
                // for a pane that would otherwise be too narrow, so the column
                // is wider than the one this frame is showing. `Resolved::Cli`
                // has already brought ATLAS up, so the next frame draws it
                // there; anything the geometry misses by, the resize after that
                // frame corrects.
                //
                // The `Err` is dropped deliberately: both refusals are on the
                // bus already (`Store::open_pty`), and toasting one here would
                // put a single refusal in two boxes.
                #[cfg(feature = "operator")]
                Some(Command::OpenCli) => {
                    // Measured before the store is borrowed to open on: the
                    // column is a fact about the frame and the desk together,
                    // and the open takes the store whole.
                    let column = ui::shell::pane_column(fx.rects.frame.get(), store);
                    let _ = atlas::pane::open(
                        store,
                        views,
                        &atlas::pty::DeskCli::from_env(),
                        column,
                        bus.clone(),
                    );
                }
                // And `/build` keeps the full-screen hand-off: Claude Code
                // editing this checkout wants the whole terminal. Recorded, not
                // performed — this function has no terminal, and the loop above
                // owns the screen the child is about to want.
                #[cfg(feature = "operator")]
                Some(Command::OpenBuild(request)) => *opening = Some(Child::Build(request)),
                // The only place a keystroke reaches the network. A view
                // decided what the key means and handed back a `Command`; the
                // runtime is what acts on it.
                // The chokepoint. The posture travels with the command so the
                // runtime — not the 33 places in `ui/` that decide whether to
                // offer a key — has the last word on whether it may happen.
                #[cfg(feature = "operator")]
                Some(cmd) => writes.dispatch(cmd, store.posture),
                None => {}
            }
            // A view switch is not a desk transition and so is not a `Trigger`:
            // the store diffs what the owner said, and which pane an operator is
            // looking at is not something the owner said. This is the one place
            // that can see the nav move, so the coalesce is fired from here.
            if store.nav.view != before {
                fx.on_view_switch();
                // Edge-triggered, so a Tick can never re-ask: arriving on
                // PREDICTORS is the one moment the board is wanted and not held.
                if store.nav.view == ViewId::Predictors {
                    poller.predictors();
                }
                if store.nav.view == ViewId::Settings {
                    poller.news();
                    poller.method();
                }
                if store.nav.view == ViewId::Visuals {
                    poller.visuals();
                }
            }
        }
    }
    // The mouse rides the same seam the keys do: the shell decides what it
    // means, the runtime dispatches whatever `Command` comes back. Today no
    // view produces one from a click, and the arm is here so the first that
    // does is dispatched rather than dropped.
    if let AppEvent::Mouse(m) = &ev {
        let before = store.nav.view;
        match ui::shell::on_mouse(*m, store, views) {
            #[cfg(feature = "operator")]
            Some(cmd) => writes.dispatch(cmd, store.posture),
            #[cfg(not(feature = "operator"))]
            Some(_) => {}
            None => {}
        }
        if store.nav.view != before {
            fx.on_view_switch();
            // The rail is clickable, so a mouse can arrive on PREDICTORS the
            // same way a digit does — same edge, same single fetch. SETTINGS
            // too, and for the same reason.
            if store.nav.view == ViewId::Predictors {
                poller.predictors();
            }
            if store.nav.view == ViewId::Settings {
                poller.news();
                poller.method();
            }
            if store.nav.view == ViewId::Visuals {
                poller.visuals();
            }
        }
    }
    // What a write outcome owes the poller is `dispatch::refetches`, which is
    // where it can be pinned per variant. The rule is not obvious enough to
    // live as a `matches!` in a loop nothing tests: a *failed* write is the
    // outcome that most needs the refresh, not the one that needs it least.
    #[cfg(feature = "operator")]
    if atlas::dispatch::refetches(&ev) {
        poller.now();
    }
    // And the one payload no snapshot carries. The pane's own POST changes
    // what `/api/news/settings` answers and nothing else on the desk, so the
    // nudge above would bring back a snapshot that says nothing about it —
    // and the card would keep drawing the stack the operator just replaced.
    #[cfg(feature = "operator")]
    if matches!(&ev, AppEvent::Wrote(atlas::bus::Wrote::NewsSaved { .. })) {
        poller.news();
    }
    // And the method, for the same reason and one more: the owner *merges* an
    // override into the mandate and recomputes its cap warning on the way out,
    // so what is now in force is a fact only its own answer holds. A card left
    // drawing what it asked for would show a cap the mandate had clamped.
    #[cfg(feature = "operator")]
    if matches!(&ev, AppEvent::Wrote(atlas::bus::Wrote::MethodSet { .. })) {
        poller.method();
    }
    // And the rights, for the news answer's reason: the route writes the full
    // three-key object and answers with what is now on disk, so the card must
    // read the owner's own file rather than the toggle it just sent. The
    // snapshot nudge above carries none of it.
    #[cfg(feature = "operator")]
    if matches!(&ev, AppEvent::Wrote(atlas::bus::Wrote::RightSet { .. })) {
        poller.rights();
    }
    // And the board the run above just replaced. The nudge is the snapshot's,
    // and a snapshot carries only the one-row `predictors` summary — the full
    // board rides its own endpoint and no beat at all, so without this the
    // pane would go on drawing the board from before the run it watched
    // finish. Fired whatever pane is on screen, for the reason `views.wrote`
    // is: the answer arrives while the operator may be looking anywhere.
    #[cfg(feature = "operator")]
    if matches!(&ev, AppEvent::Wrote(atlas::bus::Wrote::PredictorRan { .. })) {
        poller.predictors();
    }
    // And the one surface that is *waiting* for an answer rather than being
    // told about one. The login form sends and then has to hear what the owner
    // said — a consent question to put to the operator, a refusal to show under
    // the fields — and the answer arrives here rather than out of the key that
    // asked for it, because a write never blocks the frame loop.
    #[cfg(feature = "operator")]
    if let AppEvent::Wrote(outcome) = &ev {
        views.wrote(outcome);
    }
    // Before the fold, because the fold consumes the event. A toast is about the
    // event itself rather than about the state it leaves behind — an approval
    // that arrived and was consumed inside one drain still happened.
    if let Some(toast) = toast::for_event(&ev) {
        toasts.push(toast, now);
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

// -- argv ------------------------------------------------------------------

/// Every argument this client understands. A whitelist rather than a blacklist:
/// the point is that anything *not* here is refused, so a flag retired later
/// cannot quietly become a no-op the way `--operator` did.
const KNOWN_ARGS: [&str; 4] = ["--live", "--glass", "--pick", "-v"];

/// The arguments that are not [`KNOWN_ARGS`], or `Ok(())` when there are none.
///
/// `argv[0]` is skipped; it is the path this binary was invoked by, not a flag.
/// Pure so the rule can be tested without a process — the refusal itself is the
/// caller's, because a library function that called `exit` could not be.
fn unknown_args(args: &[String]) -> Result<(), Vec<String>> {
    let unknown: Vec<String> = args
        .iter()
        .skip(1)
        .filter(|a| !KNOWN_ARGS.contains(&a.as_str()))
        .cloned()
        .collect();
    match unknown.is_empty() {
        true => Ok(()),
        false => Err(unknown),
    }
}

// -- producers -------------------------------------------------------------

/// Keys and resizes onto the bus, so the loop has exactly one drain point.
/// The stdin reader, as a handle the runtime can stop.
///
/// It returns its `JoinHandle` because `/cli` and `/build` hand this same stdin
/// to a child process: a reader left on it competes for the operator's
/// keystrokes and posts what it wins onto the bus, where the desk resolves it
/// as a command line the moment the screen comes back.
fn spawn_terminal_events(tx: Tx) -> tokio::task::JoinHandle<()> {
    // Returned, not detached: see the doc comment above.
    tokio::spawn(async move {
        let mut stream = EventStream::new();
        while let Some(next) = stream.next().await {
            let ev = match next {
                Ok(Event::Key(key)) => AppEvent::Key(key),
                // Wheel and press only. Move and drag arrive dozens per
                // second under capture, and nothing downstream reads them —
                // forwarding them would wake the loop for events every view
                // declines.
                Ok(Event::Mouse(m))
                    if matches!(
                        m.kind,
                        MouseEventKind::ScrollUp
                            | MouseEventKind::ScrollDown
                            | MouseEventKind::Down(_)
                    ) =>
                {
                    AppEvent::Mouse(m)
                }
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
    })
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
    // Mouse capture off before the screen goes back: releasing the alternate
    // screen first would leave one report's worth of mouse escapes printing
    // into the shell the operator just got back.
    if let Err(err) = execute!(
        io::stdout(),
        DisableMouseCapture,
        LeaveAlternateScreen,
        cursor::Show
    ) {
        tracing::error!(%err, "could not leave the alternate screen");
    }
}

/// The real end of `handoff::Host`: this process's own screen.
///
/// It lives here rather than in the library because it is the half that cannot
/// be tested — there is no tty in a test harness — and everything about the
/// hand-off that *can* be pinned is in `atlas::handoff` with a fake in front of
/// it. What is left here is four one-line transcriptions.
///
/// `restore` and `TerminalGuard::enter` are reused rather than re-spelled: the
/// order raw mode, the alternate screen and the mouse capture come down and go
/// back up in is subtle (see `restore`), and a second copy of it here would be
/// the copy that drifts.
#[cfg(feature = "operator")]
struct ScreenHost<'a> {
    terminal: &'a mut Terminal<CrosstermBackend<io::Stdout>>,
    reader: &'a mut Reader,
    rx: &'a mut tokio::sync::mpsc::UnboundedReceiver<AppEvent>,
    /// What the drain kept: desk news that arrived while the operator was
    /// away. Written here and folded in by the loop, which owns the views.
    kept: &'a mut Vec<AppEvent>,
}

#[cfg(feature = "operator")]
impl handoff::Host for ScreenHost<'_> {
    fn pause_input(&mut self) {
        // Aborted rather than signalled: the task is parked inside
        // `EventStream::next`, a blocking read on the far side of a helper
        // thread, so there is no cooperative point a flag would be seen at.
        //
        // `abort` is a *request*, and it is not awaited — the task stops soon,
        // not now, and a read already in flight can still post one event. So
        // this call is not what makes the hand-off safe: `drain_input` is. Any
        // keystroke that slips through this gap is discarded there, before the
        // fresh reader starts and before the first frame back. Pausing is the
        // half that stops the child and the desk fighting over stdin; the
        // drain is the half that guarantees nothing stolen reaches the desk.
        self.reader.handle.abort();
    }

    fn resume_input(&mut self) {
        // A fresh `EventStream`, not the old one resumed: the aborted task's
        // stream is gone with it, and a new one starts by reading the fd as it
        // is now. Cheap — it is a task and a channel, not a terminal mode.
        self.reader.handle = spawn_terminal_events(self.reader.tx.clone());
    }

    fn drain_input(&mut self) {
        // Two queues, in the order they fill. What the terminal still holds is
        // upstream of the bus, so swallowing the bus first would leave the tty
        // to hand the same bytes to the fresh reader a moment later.
        // `Ok(true)` and nothing else: a terminal that says nothing is pending,
        // or cannot say at all, has nothing more to throw away here either way.
        while let Ok(true) = crossterm::event::poll(Duration::ZERO) {
            if crossterm::event::read().is_err() {
                break;
            }
        }
        self.kept.extend(handoff::drain_input(self.rx));
    }

    fn leave_screen(&mut self) -> io::Result<()> {
        restore();
        Ok(())
    }

    fn spawn(&mut self, argv: &[String]) -> io::Result<Option<i32>> {
        handoff::spawn_inheriting(argv)
    }

    fn enter_screen(&mut self) -> io::Result<()> {
        // The guard is dropped immediately and deliberately: `ENTERED` is what
        // the panic hook and the signal handler read, and it is a static rather
        // than something this value owns. A guard held here would restore the
        // screen a second time when it went out of scope.
        std::mem::forget(TerminalGuard::enter()?);
        Ok(())
    }

    fn redraw(&mut self) {
        // The child scrolled the terminal, so ratatui's record of what is on
        // screen is fiction and a diffed frame would paint almost nothing.
        if let Err(err) = self.terminal.clear() {
            tracing::error!(%err, "could not clear after the hand-off");
        }
    }

    fn desk_sources_changed(&mut self) -> bool {
        handoff::desk_sources_changed()
    }
}

struct TerminalGuard;

impl TerminalGuard {
    fn enter() -> io::Result<Self> {
        enable_raw_mode()?;
        // Capture after the screen is taken, mirroring `restore`'s reverse
        // order: a capture enabled on the primary screen would spray mouse
        // escapes into the scrollback if the next call failed.
        execute!(io::stdout(), EnterAlternateScreen, EnableMouseCapture)?;
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

#[cfg(test)]
mod tests {
    use super::{unknown_args, KNOWN_ARGS};

    fn argv(rest: &[&str]) -> Vec<String> {
        std::iter::once("atlas")
            .chain(rest.iter().copied())
            .map(String::from)
            .collect()
    }

    #[test]
    fn every_accepted_flag_is_accepted_together() {
        assert_eq!(unknown_args(&argv(&KNOWN_ARGS)), Ok(()));
        assert_eq!(unknown_args(&argv(&[])), Ok(()));
    }

    #[test]
    fn a_retired_flag_is_named_rather_than_ignored() {
        // The regression this exists for: `--operator` used to exit(2) with a
        // rebuild line, and deleting the flag deleted the refusal with it. A
        // silent no-op is how the original bug stayed invisible.
        assert_eq!(
            unknown_args(&argv(&["--operator"])),
            Err(vec!["--operator".to_string()])
        );
    }

    #[test]
    fn a_typo_is_not_the_flag_it_nearly_is() {
        assert_eq!(
            unknown_args(&argv(&["--glas", "--live", "--Pick"])),
            Err(vec!["--glas".to_string(), "--Pick".to_string()])
        );
    }

    #[test]
    fn the_binarys_own_path_is_never_a_flag() {
        assert_eq!(unknown_args(&["/opt/bin/--operator".to_string()]), Ok(()));
    }
}
