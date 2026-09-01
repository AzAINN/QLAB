//! The column changing hands: what `/cli` does to ATLAS, and what a frame owes
//! the child after it.
//!
//! `store_pty.rs` pins the pane as state and `keys_pty.rs` pins the keyboard.
//! This is the seam between them and the desk: the one place that decides how
//! big the child's screen is, what happens to the row the pane is drawn over,
//! and how an operator leaves a pane whose child has ended. Every claim here is
//! about a call the runtime makes — `pane::open` on `/cli`, `pane::resized`
//! after every frame — because a geometry that is right in the widget and wrong
//! at the call site is a Claude session wrapping to a screen it was never given.
//!
//! Real children on a real pty, for `keys_pty.rs`'s reason: a size handed to
//! nobody looks exactly like a size handed over correctly, so the child is asked
//! (`stty size`) wherever the claim is that it was told.
//!
//! Gated with the pane: a monitoring build has no `/cli` and no column to give.
#![cfg(feature = "operator")]

use atlas::bus::AppEvent;
use atlas::pty::{PtyEvent, Spawn};
use atlas::store::{PtyState, Store, ViewId};
use atlas::ui::views::Views;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use portable_pty::CommandBuilder;
use ratatui::layout::Rect;
use std::time::{Duration, Instant};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};

/// How long a test waits for a child with a millisecond of work to do.
///
/// `store_pty.rs`'s reason, unchanged: nothing here measures time, and the
/// deadline exists only so an implementation that never forwards fails with a
/// sentence instead of hanging the suite.
const PATIENCE: Duration = Duration::from_secs(20);

/// The ATLAS column at a 120×36 terminal, measured off the real frame: the nav
/// rail (8) and its rule, and the desk rail (34) beyond it.
///
/// Written as the rect a `draw` is handed rather than as two numbers, because
/// the whole of what this file is about is that the pane takes the *area* and
/// the child is given what is inside it.
const COLUMN: Rect = Rect {
    x: 9,
    y: 1,
    width: 77,
    height: 34,
};

/// A scripted child, on a `PATH` this file chose.
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

/// A child that says how big its terminal is, waits, and says it again.
///
/// `store_pty.rs`'s shape: `stty size` prints rows then columns, and the `read`
/// between the two orders the second measurement after the resize, because
/// bytes to a pty arrive in order.
const MEASURING: Script = Script("stty size; read go; stty size");
/// A child that waits for a line and says nothing until it has one.
const WAITING: Script = Script("read x");
/// A binary the desk does not have.
struct Missing;

impl Spawn for Missing {
    fn command(&self) -> CommandBuilder {
        CommandBuilder::new("/nonexistent/qlab")
    }
}

type Bus = (UnboundedSender<AppEvent>, UnboundedReceiver<AppEvent>);

/// An armed desk on ATLAS, the views it draws with, and the bus a child writes
/// onto.
fn desk() -> (Store, Views, Bus) {
    let (tx, rx) = unbounded_channel();
    let mut store = Store::default();
    store.posture = atlas::store::Posture::Operator;
    store.nav.view = ViewId::Atlas;
    (store, Views::new(), (tx, rx))
}

/// One keystroke, routed exactly as the runtime routes it.
fn press(store: &mut Store, views: &mut Views, code: KeyCode) -> Option<atlas::cmd::Command> {
    atlas::ui::shell::on_key(KeyEvent::new(code, KeyModifiers::NONE), store, views)
}

/// The child's screen as text, or the empty string when there is no pane.
fn screen(store: &Store) -> String {
    store
        .pty_screen()
        .map(|screen| screen.contents())
        .unwrap_or_default()
}

/// The screen's size as the parser holds it, in the order a terminal is spoken
/// about: columns then rows.
fn parsed_size(store: &Store) -> (u16, u16) {
    let (rows, cols) = store.pty_screen().expect("a pane").size();
    (cols, rows)
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
            Ok(None) => panic!("nothing is left to say {what}: every sender is gone"),
            Err(_) => panic!(
                "waited {PATIENCE:?} for {what}, screen was {:?}",
                screen(store)
            ),
        }
    }
}

// -- the geometry -----------------------------------------------------------

#[tokio::test]
async fn the_child_is_given_the_rect_inside_the_border_and_not_the_pane() {
    let (mut store, mut views, (tx, mut rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &MEASURING, COLUMN, tx).expect("the child started");
    // The parser first: it is the screen the child's bytes are painted onto,
    // and a pane two columns wider than the child was told folds every line at
    // the wrong place.
    assert_eq!(
        parsed_size(&store),
        (75, 32),
        "the pane is 77×34 and the child draws inside its border"
    );
    // And the child's own answer, because a size the store recorded and never
    // sent is indistinguishable from one it sent correctly.
    until(
        &mut store,
        &mut rx,
        "the child's own measurement",
        |store| screen(store).contains("32 75"),
    )
    .await;
}

#[tokio::test]
async fn a_frame_that_moved_the_pane_tells_the_child_its_new_size() {
    let (mut store, mut views, (tx, mut rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &MEASURING, COLUMN, tx).expect("the child started");
    until(&mut store, &mut rx, "the opening size", |store| {
        screen(store).contains("32 75")
    })
    .await;

    // The frame the operator resized into, published by the pane's own branch
    // of `draw` — the inner rect, which is what the runtime hands on.
    atlas::pane::resized(
        &mut store,
        Rect {
            x: 10,
            y: 2,
            width: 115,
            height: 38,
        },
    );
    assert_eq!(parsed_size(&store), (115, 38));
    store.pty_write(b"go\n");
    until(
        &mut store,
        &mut rx,
        "the child's new measurement",
        |store| screen(store).contains("38 115"),
    )
    .await;
}

#[tokio::test]
async fn a_frame_that_drew_no_pane_resizes_nothing() {
    // The rect a frame publishes when it drew no pane is empty, and a child
    // told it would be given a screen with no cells on it. The runtime calls
    // this after *every* frame, including the ones another view drew.
    let (mut store, mut views, (tx, _rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx).expect("the child started");
    atlas::pane::resized(&mut store, Rect::default());
    assert_eq!(parsed_size(&store), (75, 32));
}

// -- the ask row the pane is drawn over -------------------------------------

#[tokio::test]
async fn a_half_typed_question_does_not_eat_the_panes_keys() {
    // A4's likeliest real bug: `typing()` answers on the ask row's own state,
    // so a question half-typed before `/cli` would keep claiming every
    // printable key — for a row the pane is now drawn over.
    let (mut store, mut views, (tx, _rx)) = desk();
    press(&mut store, &mut views, KeyCode::Char('w'));
    press(&mut store, &mut views, KeyCode::Char('h'));
    press(&mut store, &mut views, KeyCode::Char('y'));
    assert!(
        views.typing(ViewId::Atlas),
        "the fixture is wrong: the row must be holding a question"
    );

    atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx).expect("the child started");
    assert!(
        !views.typing(ViewId::Atlas),
        "a row nobody can see is still claiming the keyboard"
    );
    // The harm, in the key an operator would reach for first: with the row
    // still claiming, `q` was a character in an invisible question.
    assert_eq!(
        press(&mut store, &mut views, KeyCode::Char('q')),
        Some(atlas::cmd::Command::Quit),
        "the desk's own keys are still the desk's while the pane is unfocused"
    );
}

#[tokio::test]
async fn a_printable_key_while_the_pane_is_up_is_not_a_question() {
    // The other half: the row is settled when the pane opens, and nothing may
    // re-arm it while the pane holds the column — one printable key would
    // otherwise put `typing()` back and take the workstation's keys with it.
    let (mut store, mut views, (tx, _rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx).expect("the child started");
    press(&mut store, &mut views, KeyCode::Char('w'));
    assert!(
        !views.typing(ViewId::Atlas),
        "a printable key typed into a row the pane is drawn over"
    );
    assert_eq!(
        press(&mut store, &mut views, KeyCode::Char('q')),
        Some(atlas::cmd::Command::Quit)
    );
}

#[tokio::test]
async fn the_question_comes_back_to_a_row_that_is_on_screen_again() {
    // Settling is not a mode: once the pane is gone the row is a row again,
    // and the first printable key types into it.
    let (mut store, mut views, (tx, _rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx).expect("the child started");
    store.close_pty();
    press(&mut store, &mut views, KeyCode::Char('w'));
    assert!(views.typing(ViewId::Atlas), "the ask row never came back");
}

// -- leaving a dead pane ----------------------------------------------------

#[tokio::test]
async fn one_key_closes_a_pane_whose_child_has_ended() {
    let (mut store, mut views, (tx, mut rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &Script("exit 0"), COLUMN, tx)
        .expect("the child started");
    until(&mut store, &mut rx, "the child to end", |store| {
        matches!(store.pty_state(), PtyState::Ended { .. })
    })
    .await;

    assert_eq!(press(&mut store, &mut views, KeyCode::Char('c')), None);
    assert_eq!(
        store.pty_state(),
        PtyState::Absent,
        "the column is still held by a pane nobody can leave"
    );
}

#[tokio::test]
async fn a_window_that_lost_its_arming_can_still_leave_a_dead_pane() {
    // The posture is the owner's to change and it can change under a live pane,
    // which is how a desk ends up glass with a terminal in its main column.
    // Closing a dead pane writes nothing — the child is gone and the session
    // with it — so this key is not the posture's to refuse, and the alternative
    // is a chat the operator cannot get back to until the desk is re-armed.
    let (mut store, mut views, (tx, mut rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &Script("exit 0"), COLUMN, tx)
        .expect("the child started");
    until(&mut store, &mut rx, "the child to end", |store| {
        matches!(store.pty_state(), PtyState::Ended { .. })
    })
    .await;

    store.posture = atlas::store::Posture::Glass;
    assert_eq!(press(&mut store, &mut views, KeyCode::Char('c')), None);
    assert_eq!(
        store.pty_state(),
        PtyState::Absent,
        "a desk flipped to glass under a pane cannot leave the one it is holding"
    );
}

#[tokio::test]
async fn a_window_that_lost_its_arming_still_may_not_close_a_live_one() {
    // The other half, and the reason the key moved rather than the whole block:
    // what makes closing safe is the *ending*, not the posture. A running child
    // is still a Claude session one keystroke may not end.
    let (mut store, mut views, (tx, _rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx).expect("the child started");
    store.posture = atlas::store::Posture::Glass;
    press(&mut store, &mut views, KeyCode::Char('c'));
    assert_eq!(store.pty_state(), PtyState::Running);
}

#[tokio::test]
async fn the_key_that_closes_a_dead_pane_leaves_a_live_child_alone() {
    // One keystroke may not end a Claude session. Ctrl-C interrupts the child
    // (`keys_pty.rs`), and this key is for the pane it leaves behind.
    let (mut store, mut views, (tx, _rx)) = desk();
    atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx).expect("the child started");
    press(&mut store, &mut views, KeyCode::Char('c'));
    assert_eq!(store.pty_state(), PtyState::Running);
}

#[tokio::test]
async fn cli_on_a_dead_pane_restarts_in_place() {
    let (mut store, mut views, (tx, mut rx)) = desk();
    atlas::pane::open(
        &mut store,
        &mut views,
        &Script("printf 'what the first child said\\r\\n'"),
        COLUMN,
        tx.clone(),
    )
    .expect("the first child started");
    until(&mut store, &mut rx, "the first child to end", |store| {
        matches!(store.pty_state(), PtyState::Ended { .. })
    })
    .await;

    atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx).expect("the second child");
    assert_eq!(store.pty_state(), PtyState::Running);
    assert!(
        !screen(&store).contains("what the first child said"),
        "the new session opened onto the dead one's output: {:?}",
        screen(&store)
    );
}

#[tokio::test]
async fn cli_while_a_child_is_running_is_refused_and_the_first_child_keeps_the_pane() {
    // A pane whose child has *ended* is replaced; a live one is not. Closing
    // first drops the session, and dropping it kills the child — so a second
    // `/cli` on a working Claude session would end it to make room for its
    // successor. The store refuses by name instead, and the first child is
    // still there to answer for itself.
    let (mut store, mut views, (tx, mut rx)) = desk();
    atlas::pane::open(
        &mut store,
        &mut views,
        &Script("read x; printf 'the first child is alive: %s\\r\\n' \"$x\""),
        COLUMN,
        tx.clone(),
    )
    .expect("the first child started");

    let said = atlas::pane::open(&mut store, &mut views, &WAITING, COLUMN, tx)
        .expect_err("a second child in one pane");
    assert!(said.contains("already running"), "{said}");
    assert_eq!(store.pty_state(), PtyState::Running);
    // Alive, not merely reported as running: the failure this pins is a `Drop`
    // that killed the session while the state machine went on saying Running.
    store.pty_write(b"yes\n");
    until(&mut store, &mut rx, "the first child to answer", |store| {
        screen(store).contains("the first child is alive: yes")
    })
    .await;
}

#[tokio::test]
async fn a_retry_that_fails_replaces_the_panes_sentence_rather_than_adding_one() {
    // One frame, one story about one child. A retry that leaves the previous
    // ending on the border and puts its own refusal in a toast is two
    // sentences about two different children on screen at once.
    let (mut store, mut views, (tx, mut rx)) = desk();
    atlas::pane::open(
        &mut store,
        &mut views,
        &Script("exit 3"),
        COLUMN,
        tx.clone(),
    )
    .expect("the child started");
    until(&mut store, &mut rx, "the child to end", |store| {
        matches!(store.pty_state(), PtyState::Ended { .. })
    })
    .await;

    let refused = atlas::pane::open(&mut store, &mut views, &Missing, COLUMN, tx)
        .expect_err("a binary the desk does not have");
    assert!(refused.contains("/nonexistent/qlab"), "{refused}");
    assert_eq!(
        store.pty_state(),
        PtyState::Absent,
        "the dead child's ending is still on a border beside a refusal about another child"
    );
}

// -- a window with no room --------------------------------------------------

#[tokio::test]
async fn a_window_with_no_room_for_a_terminal_is_told_so_and_gets_no_child() {
    // Loud rather than degraded: a child opened behind a refusal is a session
    // an operator can neither see nor read the way out of, and a screen with no
    // columns panics inside the parser. The sentence is on the bus as well as
    // in the value, because the bus is what puts it on screen.
    let (mut store, mut views, (tx, mut rx)) = desk();
    let column = atlas::ui::shell::pane_column(Rect::new(0, 0, 45, 20), &store);
    let said = atlas::pane::open(&mut store, &mut views, &WAITING, column, tx)
        .expect_err("a column with no room for a terminal");
    assert!(said.contains("no room"), "{said}");
    assert!(said.contains("qlab cli"), "{said}");
    assert_eq!(store.pty_state(), PtyState::Absent);
    let mut posted = Vec::new();
    while let Ok(ev) = rx.try_recv() {
        if let AppEvent::Pty {
            event: PtyEvent::Failed { said },
            ..
        } = &ev
        {
            posted.push(said.clone());
        }
    }
    assert_eq!(
        posted,
        vec![said],
        "the refusal the caller got is not the one on the bus"
    );
}

#[tokio::test]
async fn the_column_a_pane_gets_is_wider_than_the_one_beside_the_desk_rail() {
    // The measurement `/cli` makes is the layout's own, and at a width where
    // the rail gives its column up the two differ by the whole rail — which is
    // the difference between a terminal and a refusal.
    //
    // Three widths, because the completion record states three and a number
    // stated nowhere else is how the stale "the column is 45" survived a whole
    // task. 96 is where the rail has already gone (96 − 8 − 1); 120 and 160
    // both keep it (width − 8 − 34 − 1), and the widest is the one the record
    // quoted with no test behind it.
    let (store, _views, _bus) = desk();
    let narrow = atlas::ui::shell::pane_column(Rect::new(0, 0, 96, 36), &store);
    assert_eq!(narrow.width, 87);
    let wide = atlas::ui::shell::pane_column(Rect::new(0, 0, 120, 36), &store);
    assert_eq!(wide.width, 77);
    let wider = atlas::ui::shell::pane_column(Rect::new(0, 0, 160, 36), &store);
    assert_eq!(wider.width, 117);
}

// -- what quitting the workstation did to the child -------------------------

#[test]
fn quitting_with_a_child_running_says_so_and_says_nothing_otherwise() {
    // The kill needs no code — the session drops with the store — so what is
    // pinned is the sentence, and both arms of it. An `Ended` pane gets none:
    // that child is already gone, and it said so on the pane's own border while
    // the operator was looking at it.
    let said = atlas::pane::quit_note(&PtyState::Running).expect("a live child is ended by this");
    assert!(said.contains("ATLAS pane"), "{said}");
    assert_eq!(atlas::pane::quit_note(&PtyState::Absent), None);
    assert_eq!(
        atlas::pane::quit_note(&PtyState::Ended {
            said: "`qlab cli` ended on its own · /cli starts another".to_string()
        }),
        None
    );
}
