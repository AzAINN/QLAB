//! The child the desk owns: a real process on a real pseudoterminal, and every way it can end.
//!
//! `cli_handoff.rs` pins the *order* of the full-screen hand-off against a fake
//! spawner, because what a child does with an inherited tty cannot be seen from
//! a harness that has no tty. This file is the opposite case and takes the
//! opposite approach: a pty **is** a tty, so the children here are real ones,
//! and what is pinned is what actually came back down the wire — the bytes, the
//! exit status, the size the child measured, and the sentence each ending
//! produced.
//!
//! Scripted `sh` children rather than `qlab cli`: the desk's own verb needs an
//! owner on a port, a Claude credential and minutes of a person's attention,
//! none of which a test may assume. What `sh` proves is the whole of this
//! module's job — the pty, the reader thread, the writer, the resize and the
//! reaping — and what it deliberately does not prove is that `qlab cli` is
//! installed, which is a fact about a machine rather than about this code.

#![cfg(feature = "operator")]

use atlas::pty::{PtyEvent, PtySession, Spawn};
use portable_pty::CommandBuilder;
use std::time::{Duration, Instant};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver};

/// How long a test waits for a child that has a millisecond of work to do.
///
/// Generous on purpose. Nothing here measures time: the deadline exists only so
/// that an implementation which never posts an event fails with a sentence
/// instead of hanging the suite forever, and a tight bound would buy strictness
/// this file does not want at the price of flakiness on a loaded machine.
const PATIENCE: Duration = Duration::from_secs(20);

/// A scripted child, on a `PATH` this file chose.
///
/// `CommandBuilder` resolves `argv[0]` against **its own** `PATH`
/// (`cmdbuilder.rs::search_path`) rather than letting `execvp` do it, so the
/// value set here is what decides which `sh` runs. Stated rather than inherited
/// so the same `sh` runs wherever the suite does.
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

/// A binary no desk has, on a `PATH` that certainly does not hold it.
struct Missing;

/// Named in the assertions below, so the sentence is checked against the word
/// the caller asked for rather than against whatever the OS chose to say.
const ABSENT: &str = "qlab-cli-that-is-not-installed";

impl Spawn for Missing {
    fn command(&self) -> CommandBuilder {
        let mut cmd = CommandBuilder::new(ABSENT);
        cmd.env("PATH", "/bin:/usr/bin");
        cmd
    }
}

/// The next event, or a sentence saying what was waited for.
fn wait_for(rx: &mut UnboundedReceiver<PtyEvent>, what: &str) -> PtyEvent {
    use tokio::sync::mpsc::error::TryRecvError;
    let deadline = Instant::now() + PATIENCE;
    loop {
        match rx.try_recv() {
            Ok(event) => return event,
            // Every sender is gone, so the thing waited for is never coming and
            // the deadline would only be spent proving it. Reported at once, and
            // separately: "the session never said it" and "nothing is left to
            // say it" are different failures with different causes.
            Err(TryRecvError::Disconnected) => {
                panic!("nothing is left to say {what}: every sender is gone")
            }
            Err(TryRecvError::Empty) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(2))
            }
            Err(_) => panic!("waited {PATIENCE:?} for {what} and it never came"),
        }
    }
}

/// Everything the child wrote, and the event that ended it.
fn until_it_ends(rx: &mut UnboundedReceiver<PtyEvent>) -> (String, PtyEvent) {
    let mut said = Vec::new();
    loop {
        match wait_for(rx, "the child to end") {
            PtyEvent::Bytes(bytes) => said.extend_from_slice(&bytes),
            over => return (String::from_utf8_lossy(&said).into_owned(), over),
        }
    }
}

/// Read until the child has said `needle`, keeping everything read so far.
fn until_it_says(rx: &mut UnboundedReceiver<PtyEvent>, needle: &str) -> String {
    let mut said = String::new();
    while !said.contains(needle) {
        match wait_for(rx, needle) {
            PtyEvent::Bytes(bytes) => said.push_str(&String::from_utf8_lossy(&bytes)),
            over => panic!("the child ended before saying {needle:?}: {over:?} (said {said:?})"),
        }
    }
    said
}

/// Nothing arrived, and the waiting was long enough to mean it.
fn silence(rx: &mut UnboundedReceiver<PtyEvent>, about: &str) {
    let deadline = Instant::now() + Duration::from_millis(300);
    while Instant::now() < deadline {
        if let Ok(event) = rx.try_recv() {
            panic!("expected nothing about {about}, got {event:?}");
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

#[test]
fn a_child_yields_its_bytes_and_then_says_it_ended() {
    let (tx, mut rx) = unbounded_channel();
    let session =
        PtySession::open(&Script("printf 'hello\\n'"), 40, 10, tx).expect("the child started");

    let (said, over) = until_it_ends(&mut rx);
    assert!(said.contains("hello"), "the child's own bytes: {said:?}");
    match over {
        PtyEvent::Exited { status, said } => {
            assert_eq!(status, 0, "a clean exit is a zero");
            assert!(
                said.contains("ended on its own"),
                "an ending nobody asked for says so: {said:?}"
            );
        }
        other => panic!("expected an exit, got {other:?}"),
    }
    drop(session);
}

#[test]
fn a_non_zero_exit_is_a_sentence_naming_the_status() {
    let (tx, mut rx) = unbounded_channel();
    let session = PtySession::open(&Script("exit 3"), 40, 10, tx).expect("the child started");

    match until_it_ends(&mut rx).1 {
        PtyEvent::Exited { status, said } => {
            assert_eq!(status, 3);
            assert!(said.contains("exited 3"), "{said:?}");
        }
        other => panic!("expected an exit, got {other:?}"),
    }
    drop(session);
}

#[test]
fn a_binary_the_desk_does_not_have_is_refused_by_name() {
    let (tx, mut rx) = unbounded_channel();
    let refusal = PtySession::open(&Missing, 40, 10, tx).expect_err("there is no such binary");

    // Both halves, because they serve different readers: the `Err` is what tells
    // the caller there is no session to hold, and the event is what puts the
    // reason on the desk. A refusal that only did one of them would either be a
    // silent failure or a session that does not exist.
    assert!(
        refusal.said().contains(ABSENT),
        "the refusal names the binary: {refusal}"
    );
    match wait_for(&mut rx, "the refusal") {
        PtyEvent::Failed { said } => {
            assert!(said.contains(ABSENT), "{said:?}");
            assert!(
                said.contains("QLAB_BIN"),
                "a binary that was not found names the override that would find it: {said:?}"
            );
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
}

#[test]
fn what_the_desk_writes_reaches_the_child() {
    let (tx, mut rx) = unbounded_channel();
    let session = PtySession::open(
        &Script("read line; printf 'you said %s\\n' \"$line\""),
        40,
        10,
        tx,
    )
    .expect("the child started");

    session.write(b"ping\n");
    let (said, over) = until_it_ends(&mut rx);
    assert!(said.contains("you said ping"), "{said:?}");
    assert!(
        matches!(over, PtyEvent::Exited { status: 0, .. }),
        "{over:?}"
    );
    drop(session);
}

#[test]
fn the_child_is_told_how_big_the_pane_is_and_how_big_it_became() {
    let (tx, mut rx) = unbounded_channel();
    // `stty size` prints rows then columns, so the child is reporting the size
    // this side set rather than one it chose. The `read` between the two is what
    // makes the second measurement ordered after the resize: bytes written to a
    // pty arrive in order, so a child that has not yet seen `go` cannot have run
    // the second `stty`.
    let session = PtySession::open(&Script("stty size; read go; stty size"), 40, 10, tx)
        .expect("the child started");

    until_it_says(&mut rx, "10 40");
    session.resize(100, 30);
    session.write(b"go\n");

    let (said, over) = until_it_ends(&mut rx);
    assert!(said.contains("30 100"), "the child measured: {said:?}");
    assert!(
        matches!(over, PtyEvent::Exited { status: 0, .. }),
        "{over:?}"
    );
    drop(session);
}

#[test]
fn after_the_child_is_gone_a_keystroke_is_said_and_a_resize_is_not() {
    let (tx, mut rx) = unbounded_channel();
    let session = PtySession::open(&Script("printf 'bye\\n'"), 40, 10, tx).expect("started");
    let (_, over) = until_it_ends(&mut rx);
    assert!(matches!(over, PtyEvent::Exited { .. }), "{over:?}");

    // The asymmetry is the point. A keystroke is something the operator aimed
    // at the child, so its loss is news; a resize is the window changing shape,
    // which nobody asked for and which nothing was riding on.
    session.resize(80, 24);
    silence(&mut rx, "a resize of a child that has ended");

    session.write(b"x");
    match wait_for(&mut rx, "the lost keystroke") {
        PtyEvent::Failed { said } => assert!(
            said.contains("has ended"),
            "a keystroke that went nowhere says where it went: {said:?}"
        ),
        other => panic!("expected the write to be said, got {other:?}"),
    }
    drop(session);
}

#[test]
fn killing_the_child_ends_it_and_killing_it_again_is_a_no_op() {
    let (tx, mut rx) = unbounded_channel();
    let mut session = PtySession::open(&Script("sleep 30"), 40, 10, tx).expect("the child started");

    session.kill();
    match until_it_ends(&mut rx).1 {
        PtyEvent::Exited { said, .. } => assert!(said.contains("killed"), "{said:?}"),
        other => panic!("expected an exit, got {other:?}"),
    }

    // Idempotent because the callers cannot easily be made to agree: the pane
    // closing, the operator quitting and this session being dropped are three
    // paths to the same kill, and a second one must be a no-op rather than a
    // signal at a pid the OS may have handed to somebody else.
    session.kill();
    silence(&mut rx, "a second kill");
}

#[test]
fn dropping_the_session_kills_the_child_rather_than_orphaning_it() {
    let (tx, mut rx) = unbounded_channel();
    let session = PtySession::open(&Script("sleep 30"), 40, 10, tx).expect("the child started");
    drop(session);

    // The reader thread holds its own sender, so the ending still arrives after
    // the session that started it is gone — which is what makes this
    // observable, and is also why quitting the workstation can say what it did.
    match until_it_ends(&mut rx).1 {
        PtyEvent::Exited { said, .. } => assert!(said.contains("killed"), "{said:?}"),
        other => panic!("expected an exit, got {other:?}"),
    }
}
