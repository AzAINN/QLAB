//! The child's screen as desk state: what the pane holds, and every way it changes.
//!
//! `pty_session.rs` pins the child's own lifecycle — bytes out, keystrokes in,
//! a size, an ending. This file pins what the *desk* does with all of that: the
//! parser it advances, the frame it owes, the sentence it composes when a child
//! is gone, and the two things it refuses. Between them there is one seam, and
//! it is the bus: `PtyEvent` goes in at one end and `AppEvent::Pty` comes out
//! at the other, which is what makes a frame a pure function of the store even
//! though a process is writing to it.
//!
//! **Real children, and synthetic events, deliberately mixed.** A pane cannot
//! exist without a child, so every test here starts one — a scripted `sh`, for
//! `pty_session.rs`'s reason: `qlab cli` wants an owner on a port and a
//! credential, and a test may assume neither. But a child's *timing* is not
//! this file's subject, so once a pane is up most of these hand the event arm
//! the exact `PtyEvent` A1 documents rather than waiting for one. What that
//! buys is a test that fails for the reason it is named after instead of
//! occasionally timing out on a loaded machine; what it costs is covered by the
//! three tests here that do drive a real child end to end.
//!
//! Gated with the module it is about: a monitoring build has no pane, no
//! parser, and no key that could open either.
#![cfg(feature = "operator")]

use atlas::bus::AppEvent;
use atlas::pty::{PtyEvent, Spawn};
use atlas::store::{PtyState, Store};
use atlas::ui::widgets::toast;
use portable_pty::CommandBuilder;
use std::time::{Duration, Instant};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};

/// How long a test waits for a child with a millisecond of work to do.
///
/// Generous for `pty_session.rs`'s reason: nothing here measures time, and the
/// deadline exists only so an implementation that never posts fails with a
/// sentence instead of hanging the suite.
const PATIENCE: Duration = Duration::from_secs(20);

/// A scripted child, on a `PATH` this file chose.
///
/// `CommandBuilder` resolves `argv[0]` against its **own** `PATH`, so the value
/// set here is what decides which `sh` runs.
struct Script(&'static str);

impl Spawn for Script {
    fn command(&self) -> CommandBuilder {
        let mut cmd = CommandBuilder::new("sh");
        cmd.arg("-c");
        cmd.arg(self.0);
        cmd.env("PATH", "/bin:/usr/bin:/usr/local/bin");
        cmd
    }
}

/// A child that stays until it is spoken to, and says nothing meanwhile.
///
/// The fixture most of these want: a pane needs a live child to exist at all,
/// and a child writing on its own would race the synthetic events under test.
const WAITING: Script = Script("read x");

/// A binary no desk has, on a `PATH` that certainly does not hold it.
struct Missing;

const ABSENT: &str = "qlab-cli-that-is-not-installed";

impl Spawn for Missing {
    fn command(&self) -> CommandBuilder {
        let mut cmd = CommandBuilder::new(ABSENT);
        cmd.env("PATH", "/bin:/usr/bin");
        cmd
    }
}

/// The stamps `open_pty` gives the first and second panes of a run.
///
/// Nothing in production reads a pane's id back — the store hands it to the
/// forwarder and compares it in the fold, and that is the whole of it — so a
/// test that hands the fold an event *by hand* has to agree with the store's
/// own numbering. Nothing here asserts that numbering directly, and nothing
/// has to: every test below that stamps `FIRST` while the first pane is open
/// fails if the store started counting anywhere else, and the two at the bottom
/// fail if `SECOND` is not a different number from it.
const FIRST: u64 = 1;
const SECOND: u64 = 2;

/// One event as it leaves a pane, for a test that is not waiting on a child to
/// produce the real thing.
fn from_pane(pane: u64, event: PtyEvent) -> AppEvent {
    AppEvent::Pty { pane, event }
}

type Bus = (UnboundedSender<AppEvent>, UnboundedReceiver<AppEvent>);

/// A desk with a pane in it, and the bus its child writes onto.
fn desk() -> (Store, Bus) {
    let (tx, rx) = unbounded_channel();
    (Store::default(), (tx, rx))
}

/// The child's screen as text, or the empty string when there is no pane.
fn screen(store: &Store) -> String {
    store
        .pty_screen()
        .map(|screen| screen.contents())
        .unwrap_or_default()
}

/// Fold whatever the bus carries into the store until the desk shows what the
/// test is waiting for.
async fn until(
    store: &mut Store,
    rx: &mut UnboundedReceiver<AppEvent>,
    what: &str,
    ready: impl Fn(&Store) -> bool,
) {
    let deadline = tokio::time::Instant::now() + PATIENCE;
    while !ready(store) {
        match tokio::time::timeout_at(deadline, rx.recv()).await {
            Ok(Some(ev)) => {
                store.apply(ev, Instant::now());
            }
            // Every sender is gone, so the thing waited for is never coming and
            // the deadline would only be spent proving it.
            Ok(None) => panic!("nothing is left to say {what}: every sender is gone"),
            Err(_) => panic!(
                "waited {PATIENCE:?} for {what}, screen was {:?}",
                screen(store)
            ),
        }
    }
}

/// The next event off the bus, or a sentence saying what was waited for.
async fn next(rx: &mut UnboundedReceiver<AppEvent>, what: &str) -> AppEvent {
    match tokio::time::timeout(PATIENCE, rx.recv()).await {
        Ok(Some(ev)) => ev,
        Ok(None) => panic!("nothing is left to say {what}: every sender is gone"),
        Err(_) => panic!("waited {PATIENCE:?} for {what}"),
    }
}

/// The sentence a pty event carries, whatever shape it came in.
fn said(ev: &AppEvent) -> String {
    match ev {
        AppEvent::Pty {
            event: PtyEvent::Failed { said },
            ..
        } => said.clone(),
        AppEvent::Pty {
            event: PtyEvent::Exited { said, .. },
            ..
        } => said.clone(),
        AppEvent::Pty {
            event: PtyEvent::Bytes(bytes),
            ..
        } => String::from_utf8_lossy(bytes).into_owned(),
        _ => panic!("not a pty event"),
    }
}

// -- the screen --------------------------------------------------------------

#[tokio::test]
async fn the_childs_bytes_reach_the_screen_and_owe_a_frame() {
    let (mut store, (tx, _rx)) = desk();
    store
        .open_pty(&WAITING, 40, 10, tx)
        .expect("the child started");
    store.take_dirty();

    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Bytes(b"the desk is up on :8765\r\n".to_vec()),
        ),
        Instant::now(),
    );

    assert!(
        screen(&store).contains("the desk is up on :8765"),
        "the child's bytes are the screen: {:?}",
        screen(&store)
    );
    // Without this the pane repaints on the 100 ms idle floor, which is a
    // terminal that lags every keystroke by up to a tenth of a second.
    assert!(store.take_dirty(), "a byte the child wrote owes a frame");
}

#[tokio::test]
async fn bytes_with_no_pane_to_draw_them_are_not_a_pane() {
    // The race is real: closing the pane drops the session, and bytes already
    // on the channel arrive after. A store that built a pane out of them would
    // put a terminal back on the desk that the operator had just closed.
    let (mut store, (_tx, _rx)) = desk();
    store.apply(
        from_pane(FIRST, PtyEvent::Bytes(b"orphan\r\n".to_vec())),
        Instant::now(),
    );
    assert_eq!(store.pty_state(), PtyState::Absent);
    assert!(store.pty_screen().is_none());
}

// -- the ending --------------------------------------------------------------

#[tokio::test]
async fn a_child_that_ends_says_what_happened_and_how_to_start_another() {
    let (mut store, (tx, mut rx)) = desk();
    store
        .open_pty(&Script("printf 'bye\\r\\n'"), 40, 10, tx)
        .expect("the child started");

    until(&mut store, &mut rx, "the child to end", |store| {
        matches!(store.pty_state(), PtyState::Ended { .. })
    })
    .await;

    // `pty.rs` says what happened and stops there; how to start another is the
    // desk's own vocabulary, and `/cli` is only a word here. The name in the
    // first half is the *child's own* — a scripted `sh` in this file, and
    // `qlab cli` on a desk, which is the sentence `golden_terminal.rs` draws.
    let PtyState::Ended { said } = store.pty_state() else {
        panic!("the child ended: {:?}", store.pty_state())
    };
    assert!(
        said.starts_with("`sh -c printf 'bye\\r\\n'` ended on its own"),
        "the ending is the child's own account of itself: {said:?}"
    );
    assert!(
        said.ends_with(" · /cli starts another"),
        "and the desk says how to start another: {said:?}"
    );
    // The pane stays: the last thing a failed session printed is on that screen.
    assert!(screen(&store).contains("bye"), "{:?}", screen(&store));
}

#[tokio::test]
async fn a_non_zero_exit_keeps_its_own_status_in_the_sentence() {
    let (mut store, (tx, _rx)) = desk();
    store.open_pty(&WAITING, 40, 10, tx).expect("started");

    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Exited {
                status: 3,
                said: "`qlab cli` exited 3".to_string(),
            },
        ),
        Instant::now(),
    );

    assert_eq!(
        store.pty_state(),
        PtyState::Ended {
            said: "`qlab cli` exited 3 · /cli starts another".to_string()
        }
    );
    assert!(store.take_dirty(), "an ending owes a frame");
}

#[tokio::test]
async fn an_ending_heard_after_the_pane_was_closed_does_not_put_it_back() {
    let (mut store, (tx, _rx)) = desk();
    store.open_pty(&WAITING, 40, 10, tx).expect("started");
    store.close_pty();

    // Closing kills, so this ending is the one the close asked for and it
    // arrives after the pane is gone. A store that acted on it would draw a
    // terminal nobody has open.
    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Exited {
                status: 0,
                said: "`qlab cli` ended on its own".to_string(),
            },
        ),
        Instant::now(),
    );
    assert_eq!(store.pty_state(), PtyState::Absent);
}

// -- the two refusals --------------------------------------------------------

#[tokio::test]
async fn a_second_child_is_refused_by_name() {
    let (mut store, (tx, mut rx)) = desk();
    store
        .open_pty(&WAITING, 40, 10, tx.clone())
        .expect("the first child started");
    store.apply(
        from_pane(FIRST, PtyEvent::Bytes(b"the first child\r\n".to_vec())),
        Instant::now(),
    );

    let refused = store
        .open_pty(&WAITING, 40, 10, tx)
        .expect_err("a second child in one pane is refused");

    assert!(
        refused.contains("qlab cli") && refused.contains("already running"),
        "the refusal names the child that has the pane: {refused:?}"
    );
    // And the pane still belongs to the child that was there first — a refusal
    // that had replaced it would have killed a live session to say no.
    assert_eq!(store.pty_state(), PtyState::Running);
    assert!(
        screen(&store).contains("the first child"),
        "{:?}",
        screen(&store)
    );
    // Invariant 4: the refusal is on the desk's own bus, so it reaches the
    // operator whether or not the caller looks at what `open_pty` returned.
    assert_eq!(said(&next(&mut rx, "the refusal").await), refused);
}

#[tokio::test]
async fn a_child_that_never_started_is_refused_by_name_and_leaves_no_pane() {
    let (mut store, (tx, mut rx)) = desk();

    let refused = store
        .open_pty(&Missing, 40, 10, tx)
        .expect_err("a binary this desk does not have is refused");

    assert!(
        refused.contains(ABSENT),
        "the refusal names the binary: {refused:?}"
    );
    // No child, so no pane: an empty terminal with a border would be a session
    // that never existed drawn as one that did.
    assert_eq!(store.pty_state(), PtyState::Absent);
    assert!(store.pty_screen().is_none());
    // `pty.rs` posts the same sentence it returned, and the bridge carries it.
    assert_eq!(said(&next(&mut rx, "the refusal").await), refused);
}

// -- the keyboard ------------------------------------------------------------

#[tokio::test]
async fn the_keyboard_goes_back_to_the_desk_when_the_child_ends() {
    let (mut store, (tx, _rx)) = desk();
    store.open_pty(&WAITING, 40, 10, tx).expect("started");
    store.pty_focus(true);
    assert!(store.pty_focused(), "a live child can hold the keyboard");

    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Exited {
                status: 0,
                said: "`qlab cli` ended on its own".to_string(),
            },
        ),
        Instant::now(),
    );

    // The state the pane must never be drawn in: `pty.rs` answers every
    // keystroke to a dead child with a sentence, so a pane left focused after
    // an exit fills the desk with them — and the border would be offering a
    // keyboard to a process that cannot take it.
    assert!(!store.pty_focused(), "an ending returns the keyboard");
    assert!(matches!(store.pty_state(), PtyState::Ended { .. }));
}

#[tokio::test]
async fn the_keyboard_is_only_ever_a_live_childs() {
    let (mut store, (tx, _rx)) = desk();
    // No pane at all.
    store.pty_focus(true);
    assert!(!store.pty_focused());

    store.open_pty(&WAITING, 40, 10, tx).expect("started");
    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Exited {
                status: 0,
                said: "`qlab cli` ended on its own".to_string(),
            },
        ),
        Instant::now(),
    );
    store.pty_focus(true);
    assert!(!store.pty_focused(), "an ended child takes no keystrokes");
}

#[tokio::test]
async fn what_the_desk_types_reaches_the_child() {
    let (mut store, (tx, mut rx)) = desk();
    store
        .open_pty(&Script("read x; printf 'got %s\\r\\n' \"$x\""), 40, 10, tx)
        .expect("started");

    store.pty_write(b"hi\n");

    until(&mut store, &mut rx, "the child's answer", |store| {
        screen(store).contains("got hi")
    })
    .await;
}

// -- the size ----------------------------------------------------------------

#[tokio::test]
async fn the_pane_and_the_child_are_resized_together() {
    let (mut store, (tx, mut rx)) = desk();
    // `stty size` prints rows then columns, so the child reports the size this
    // side set. The `read` between the two orders the second measurement after
    // the resize: bytes to a pty arrive in order.
    store
        .open_pty(&Script("stty size; read go; stty size"), 40, 10, tx)
        .expect("started");
    until(
        &mut store,
        &mut rx,
        "the child's first measurement",
        |store| screen(store).contains("10 40"),
    )
    .await;

    store.pty_resize(100, 30);

    // Both halves, because either alone is a broken pane: a child that was not
    // told wraps its output to a geometry it no longer has, and a parser that
    // was not told draws the answer into a grid of the old shape.
    assert_eq!(
        store.pty_screen().expect("a pane").size(),
        (30, 100),
        "the screen the desk parses onto is the size of the pane"
    );
    store.pty_write(b"go\n");
    until(
        &mut store,
        &mut rx,
        "the child's second measurement",
        |store| screen(store).contains("30 100"),
    )
    .await;
}

// -- closing and starting again ----------------------------------------------

#[tokio::test]
async fn closing_the_pane_ends_the_child_and_takes_the_pane_with_it() {
    let (mut store, (tx, mut rx)) = desk();
    store.open_pty(&WAITING, 40, 10, tx).expect("started");

    store.close_pty();

    assert_eq!(store.pty_state(), PtyState::Absent);
    assert!(store.pty_screen().is_none());
    // And the child is really gone rather than orphaned on a pty nobody reads.
    loop {
        match next(&mut rx, "the closed child to end").await {
            AppEvent::Pty {
                event: PtyEvent::Exited { .. },
                ..
            } => break,
            AppEvent::Pty {
                event: PtyEvent::Bytes(_),
                ..
            } => continue,
            other => panic!("expected the child to end, got {:?}", said(&other)),
        }
    }
}

#[tokio::test]
async fn a_child_started_after_another_ended_gets_a_clean_screen() {
    let (mut store, (tx, _rx)) = desk();
    store
        .open_pty(&WAITING, 40, 10, tx.clone())
        .expect("the first child started");
    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Bytes(b"what the last one said\r\n".to_vec()),
        ),
        Instant::now(),
    );
    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Exited {
                status: 0,
                said: "`qlab cli` ended on its own".to_string(),
            },
        ),
        Instant::now(),
    );

    // An ended pane is one `/cli` may start another in — that is what its own
    // sentence says — and the new session must not open onto the last one's
    // output.
    store
        .open_pty(&WAITING, 40, 10, tx)
        .expect("an ended pane takes a new child");

    assert_eq!(store.pty_state(), PtyState::Running);
    assert!(
        !screen(&store).contains("what the last one said"),
        "a new child opens on a clean screen: {:?}",
        screen(&store)
    );
}

// -- the sentence with nowhere else to go ------------------------------------

#[tokio::test]
async fn a_keystroke_that_never_reached_the_child_is_said() {
    // The pane's border can only carry a sentence about a child that has
    // *ended* — while one is live the border is naming the keyboard — so a
    // keystroke that went nowhere has no surface but a toast.
    let ev = from_pane(
        FIRST,
        PtyEvent::Failed {
            said: "`qlab cli` has ended — what you typed did not reach it".to_string(),
        },
    );
    let toast = toast::for_event(&ev).expect("a lost keystroke is said somewhere");
    assert!(
        toast.message.contains("did not reach it"),
        "{:?}",
        toast.message
    );

    // And a byte batch is not news: fifty a second, each one already on screen.
    assert!(toast::for_event(&from_pane(FIRST, PtyEvent::Bytes(b"x".to_vec()))).is_none());
    // Nor is an ending, which the pane's own border states.
    assert!(toast::for_event(&from_pane(
        FIRST,
        PtyEvent::Exited {
            status: 0,
            said: "`qlab cli` ended on its own".to_string(),
        }
    ))
    .is_none());
}

// -- one pane's news is not another's ----------------------------------------

#[tokio::test]
async fn a_dead_childs_ending_must_not_end_the_child_that_replaced_it() {
    let (mut store, (tx, mut rx)) = desk();
    store
        .open_pty(&WAITING, 40, 10, tx.clone())
        .expect("the first child started");
    // `close_pty` signals and returns — it does not join the reader thread — so
    // the ending it asked for is still in flight while the next child opens.
    store.close_pty();
    let ending = loop {
        let ev = next(&mut rx, "the closed child's own ending").await;
        if matches!(
            &ev,
            AppEvent::Pty {
                event: PtyEvent::Exited { .. },
                ..
            }
        ) {
            break ev;
        }
    };

    store
        .open_pty(
            &Script("read x; printf 'the second child is alive: %s\\r\\n' \"$x\""),
            40,
            10,
            tx,
        )
        .expect("the second child started");
    store.apply(ending, Instant::now());

    // Two failures at once if this is dispatched on "is there a pane": the desk
    // reports a live session as ended, carrying a sentence about a different
    // child — and writing `Gone` over the live arm drops that `PtySession`,
    // whose `Drop` kills it. The store would be making its own false report
    // true, on a session somebody is working in.
    assert_eq!(store.pty_state(), PtyState::Running);
    store.pty_write(b"yes\n");
    until(&mut store, &mut rx, "the second child to answer", |store| {
        screen(store).contains("the second child is alive: yes")
    })
    .await;
}

#[tokio::test]
async fn a_dead_childs_last_bytes_must_not_land_on_the_new_childs_screen() {
    let (mut store, (tx, _rx)) = desk();
    store
        .open_pty(&WAITING, 40, 10, tx.clone())
        .expect("the first child started");
    store.close_pty();
    store
        .open_pty(&WAITING, 40, 10, tx)
        .expect("the second child started");

    store.apply(
        from_pane(
            FIRST,
            PtyEvent::Bytes(b"what the dead child was saying\r\n".to_vec()),
        ),
        Instant::now(),
    );

    assert!(
        !screen(&store).contains("what the dead child was saying"),
        "a closed child's trailing bytes painted into the pane that replaced it: {:?}",
        screen(&store)
    );

    // And the open pane's own bytes still arrive. Without this the rule under
    // test could be "drop everything" and read as passing — which is a terminal
    // that never draws, discovered by an operator rather than here.
    store.apply(
        from_pane(
            SECOND,
            PtyEvent::Bytes(b"what the new child is saying\r\n".to_vec()),
        ),
        Instant::now(),
    );
    assert!(
        screen(&store).contains("what the new child is saying"),
        "the open pane's own bytes are not stale: {:?}",
        screen(&store)
    );
}
