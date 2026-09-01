//! The keyboard changing hands: what a keystroke means while a child holds it.
//!
//! `store_pty.rs` pins the pane as *state* — the parser, the ending, the two
//! refusals. This file pins the one thing that state exists for: routing. While
//! the pane holds the keyboard every key is the child's, `Ctrl-C` included, and
//! one key gives it back. Both halves go through the real `shell::on_key`,
//! because a router that is correct and a shell that never reaches it is the
//! shape invariant 10 exists for.
//!
//! **Real children, on purpose, for the keys.** A byte forwarded to nowhere
//! looks exactly like a byte forwarded correctly, so every claim of the form
//! "this key reached the child" is answered by the child itself — it reads
//! what was typed and prints something about it, and the assertion is on the
//! screen the desk parsed. `Ctrl-C` is answered the strongest way there is: the
//! child dies of it.
//!
//! Gated with the pane it is about: a monitoring build has no child to type at
//! and no key that could open one.
#![cfg(feature = "operator")]
// Unix, and only the tests: `portable-pty` is cross-platform and `src/pty.rs`
// builds everywhere, but every claim here is answered by a scripted `sh` child
// reading what was typed at it, and `Ctrl-C` is answered by that child dying of
// SIGINT. `pty_session.rs` states the whole reason. Whole-file rather than
// per-test, because the two tests that open no pane are the "and with no child"
// arm of the routing claims beside them.
#![cfg(unix)]
// A turn is held across every `.await` in this file, which is what
// `clippy::await_holding_lock` names — and the hazard it names is absent: a
// `#[tokio::test]` runtime is current-thread with one task on it, so nothing
// else is waiting to make progress, and the turn has to outlive the child
// rather than the `open`. `one_pty::turn()` is the only lock taken here;
// `one_pty/mod.rs` states the whole reason.
#![allow(clippy::await_holding_lock)]

mod one_pty;

use atlas::bus::AppEvent;
use atlas::cmd::Command;
use atlas::pty::{PtyEvent, Spawn};
use atlas::store::{Focus, Posture, PtyState, Store, ViewId};
use atlas::ui::views::Views;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use portable_pty::CommandBuilder;
use std::time::{Duration, Instant};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};

/// How long a test waits for a child with a millisecond of work to do.
///
/// `store_pty.rs`'s reason, unchanged: nothing here measures time, and the
/// deadline exists only so an implementation that never forwards fails with a
/// sentence instead of hanging the suite.
const PATIENCE: Duration = Duration::from_secs(20);

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

/// A child that waits for a line and says nothing until it has one.
const WAITING: Script = Script("read x");

type Bus = (UnboundedSender<AppEvent>, UnboundedReceiver<AppEvent>);

/// A desk with nothing on it and the bus a child would write onto.
fn desk() -> (Store, Views, Bus) {
    let (tx, rx) = unbounded_channel();
    (Store::default(), Views::new(), (tx, rx))
}

/// The same desk with a child in the pane, already running.
async fn desk_with(child: &'static Script) -> (Store, Views, Bus) {
    let (mut store, views, (tx, rx)) = desk();
    store
        .open_pty(child, 40, 10, tx.clone())
        .expect("the child started");
    (store, views, (tx, rx))
}

/// The stamp `desk_with`'s pane is given: every test here opens exactly one.
///
/// An event on the bus names the pane it came from, because the bus outlives
/// the pane — see `store_pty.rs`, where the rule and its numbering are pinned.
const FIRST: u64 = 1;

/// One event as it leaves that pane.
fn from_pane(pane: u64, event: PtyEvent) -> AppEvent {
    AppEvent::Pty { pane, event }
}

/// One keystroke, routed exactly as the runtime routes it.
fn press(store: &mut Store, views: &mut Views, code: KeyCode) -> Option<Command> {
    atlas::ui::shell::on_key(KeyEvent::new(code, KeyModifiers::NONE), store, views)
}

/// The same, with the control key down.
fn ctrl(store: &mut Store, views: &mut Views, code: KeyCode) -> Option<Command> {
    atlas::ui::shell::on_key(KeyEvent::new(code, KeyModifiers::CONTROL), store, views)
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
            Ok(None) => panic!("nothing is left to say {what}: every sender is gone"),
            Err(_) => panic!(
                "waited {PATIENCE:?} for {what}, screen was {:?}",
                screen(store)
            ),
        }
    }
}

/// Whether a quit was asked for.
fn quits(command: &Option<Command>) -> bool {
    matches!(command, Some(Command::Quit))
}

// -- while the pane holds the keyboard ---------------------------------------

#[tokio::test]
async fn every_key_the_desk_claims_belongs_to_the_child_while_the_pane_has_it() {
    // `q` quits, `/` opens the command line and a digit jumps to a view — three
    // keys the whole workstation depends on, and all three are characters a
    // session needs to be able to type. A pane that let the shell keep them
    // would be a terminal an operator cannot use.
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, mut rx)) =
        desk_with(&Script("read x; printf 'read [%s]\\r\\n' \"$x\"")).await;
    store.pty_focus(true);
    let opened_on = store.nav.view;

    for code in [KeyCode::Char('q'), KeyCode::Char('/'), KeyCode::Char('3')] {
        let acted = press(&mut store, &mut views, code);
        assert!(
            acted.is_none(),
            "{code:?} produced {acted:?} while the child had the keyboard"
        );
    }
    press(&mut store, &mut views, KeyCode::Enter);

    // The desk did not move under the typing.
    assert_eq!(
        store.nav.focus,
        Focus::Content,
        "`/` opened the desk's line"
    );
    assert_eq!(store.nav.view, opened_on, "a digit moved the desk's view");
    // And the child read all three, in order, as one line.
    until(&mut store, &mut rx, "the child's answer", |store| {
        screen(store).contains("read [q/3]")
    })
    .await;
}

#[tokio::test]
async fn ctrl_c_interrupts_the_child_and_does_not_quit_the_desk() {
    // The ruling this task exists for. A terminal that cannot interrupt its own
    // child is not a terminal, so `Ctrl-C` stops being an unconditional quit
    // for exactly as long as the pane holds the keyboard.
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, mut rx)) = desk_with(&WAITING).await;
    store.pty_focus(true);

    let acted = ctrl(&mut store, &mut views, KeyCode::Char('c'));
    assert!(
        !quits(&acted),
        "Ctrl-C quit the workstation instead of reaching the child: {acted:?}"
    );

    // The strongest available proof that the byte arrived: the child dies of
    // it. Nothing here writes to the child except that keystroke.
    until(
        &mut store,
        &mut rx,
        "the child to be interrupted",
        |store| matches!(store.pty_state(), PtyState::Ended { .. }),
    )
    .await;
}

#[tokio::test]
async fn the_keyboard_comes_back_on_ctrl_bracket_and_the_next_q_quits() {
    // The way out, and the exchange it buys: quitting is still one key away,
    // and the pane's border already names the key that gets there.
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, mut rx)) =
        desk_with(&Script("read x; printf 'len %s\\r\\n' \"${#x}\"")).await;
    store.pty_focus(true);

    let acted = ctrl(&mut store, &mut views, KeyCode::Char(']'));
    assert!(
        acted.is_none(),
        "returning the keyboard asked for {acted:?}"
    );
    assert!(!store.pty_focused(), "Ctrl-] did not return the keyboard");
    assert!(
        quits(&press(&mut store, &mut views, KeyCode::Char('q'))),
        "the desk did not get its own `q` back"
    );

    // And the key the desk kept was not also sent: one character was typed
    // after it, and the child counts one.
    store.pty_focus(true);
    press(&mut store, &mut views, KeyCode::Char('x'));
    press(&mut store, &mut views, KeyCode::Enter);
    until(&mut store, &mut rx, "the child's count", |store| {
        screen(store).contains("len 1")
    })
    .await;
}

#[tokio::test]
async fn the_terminal_that_spells_ctrl_bracket_as_ctrl_five_is_understood_too() {
    // 0x1D is one byte with two names on a keyboard, and crossterm reports it
    // as `5` with control down unless the kitty protocol is negotiated — which
    // this client does not negotiate. A router that only knew the bracket would
    // have shipped a border naming a key that does nothing.
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, _rx)) = desk_with(&WAITING).await;
    store.pty_focus(true);

    ctrl(&mut store, &mut views, KeyCode::Char('5'));
    assert!(
        !store.pty_focused(),
        "the spelling this client actually receives did not return the keyboard"
    );
}

#[tokio::test]
async fn a_child_that_has_ended_takes_no_keys_and_says_nothing_per_keystroke() {
    // Carried from A1, and binding: a write to a dead child is *said*, one
    // sentence per keystroke, so a pane left focused after an exit would fill
    // the desk with them. The ending takes the keyboard back with the child.
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, mut rx)) = desk_with(&WAITING).await;
    store.pty_focus(true);

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
    assert!(
        !store.pty_focused(),
        "an ending left the keyboard with a child that is gone"
    );

    // Every key is the desk's again, starting with the one that leaves.
    assert!(quits(&press(&mut store, &mut views, KeyCode::Char('q'))));
    for code in [KeyCode::Char('a'), KeyCode::Char('b'), KeyCode::Enter] {
        press(&mut store, &mut views, code);
    }
    while let Ok(ev) = rx.try_recv() {
        if let AppEvent::Pty {
            event: PtyEvent::Failed { said },
            ..
        } = &ev
        {
            assert!(
                !said.contains("did not reach it"),
                "a keystroke was forwarded to a child that had ended: {said}"
            );
        }
    }
}

// -- the regression pin ------------------------------------------------------

/// Every global key, asserted to do exactly what it does today.
fn the_desk_still_has_its_keys(store: &mut Store, views: &mut Views, when: &str) {
    store.nav.view = ViewId::Desk;
    assert!(
        quits(&press(store, views, KeyCode::Char('q'))),
        "`q` stopped quitting {when}"
    );
    assert!(
        quits(&ctrl(store, views, KeyCode::Char('c'))),
        "Ctrl-C stopped quitting {when}"
    );
    assert!(
        quits(&press(store, views, KeyCode::Esc)),
        "Esc stopped quitting {when}"
    );
    assert!(
        matches!(
            press(store, views, KeyCode::Char('r')),
            Some(Command::Refresh)
        ),
        "`r` stopped refreshing {when}"
    );

    press(store, views, KeyCode::Char('/'));
    assert_eq!(store.nav.focus, Focus::Command, "`/` lost the line {when}");
    press(store, views, KeyCode::Esc);
    assert_eq!(store.nav.focus, Focus::Content, "Esc lost the line {when}");

    press(store, views, KeyCode::Char('?'));
    assert_eq!(store.nav.focus, Focus::Help, "`?` lost the overlay {when}");
    press(store, views, KeyCode::Esc);
    assert_eq!(
        store.nav.focus,
        Focus::Content,
        "Esc lost the overlay {when}"
    );

    press(store, views, KeyCode::Char('1'));
    assert_eq!(
        store.nav.view,
        ViewId::Atlas,
        "a digit stopped moving {when}"
    );
    press(store, views, KeyCode::Tab);
    assert_eq!(
        store.nav.view,
        ViewId::Atlas.next(),
        "Tab stopped moving {when}"
    );
}

#[tokio::test]
async fn with_no_pane_at_all_every_key_is_what_it_was() {
    let (mut store, mut views, (_tx, _rx)) = desk();
    assert_eq!(store.pty_state(), PtyState::Absent);
    the_desk_still_has_its_keys(&mut store, &mut views, "with no pane on the desk");
}

#[tokio::test]
async fn with_a_child_running_but_unfocused_every_key_is_what_it_was() {
    // The pin the whole change rests on: opening a pane changes nothing until
    // the operator hands it the keyboard.
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, _rx)) = desk_with(&WAITING).await;
    assert_eq!(store.pty_state(), PtyState::Running);
    assert!(!store.pty_focused());
    the_desk_still_has_its_keys(&mut store, &mut views, "with a pane the desk still owns");
}

// -- giving it away ----------------------------------------------------------

#[tokio::test]
async fn i_gives_the_keyboard_to_a_running_child() {
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, _rx)) = desk_with(&WAITING).await;
    store.posture = Posture::Operator;
    store.nav.view = ViewId::Atlas;

    let acted = press(&mut store, &mut views, KeyCode::Char('i'));
    assert!(acted.is_none(), "the focus key asked for {acted:?}");
    assert!(
        store.pty_focused(),
        "`i` did not give the child the keyboard"
    );
    // And the ask row underneath did not take it as well: two surfaces holding
    // one keyboard is the state the border could not describe.
    assert!(
        !views.typing(ViewId::Atlas),
        "the ask row took the focus key too"
    );
}

#[tokio::test]
async fn with_no_pane_i_still_focuses_the_ask_row() {
    // The regression half: `i` is the ask row's key on every ATLAS frame that
    // has no child in it, which is every frame this client drew before now.
    let (mut store, mut views, (_tx, _rx)) = desk();
    store.posture = Posture::Operator;
    store.nav.view = ViewId::Atlas;

    press(&mut store, &mut views, KeyCode::Char('i'));
    assert!(
        views.typing(ViewId::Atlas),
        "`i` stopped focusing the ask row"
    );
}

#[tokio::test]
async fn a_dead_pane_does_not_hand_the_focus_key_to_a_row_nobody_can_see() {
    // The pane keeps the column after its child ends, so the ask row is not on
    // screen — and a key that focused it would arm a field the operator cannot
    // see, which is the hung-client reading this pane must never produce.
    let _pty = one_pty::turn();
    let (mut store, mut views, (_tx, _rx)) = desk_with(&WAITING).await;
    store.posture = Posture::Operator;
    store.nav.view = ViewId::Atlas;
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

    press(&mut store, &mut views, KeyCode::Char('i'));
    assert!(
        !store.pty_focused(),
        "a child that has ended took a keyboard"
    );
    assert!(
        !views.typing(ViewId::Atlas),
        "the focus key reached a row the pane is drawn over"
    );
}
