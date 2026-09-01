//! `/cli` and `/build`: the desk's two Claude words, and what the hand-off does with the terminal.
//!
//! Two things are pinned here and neither is about HTTP. First the *order* of
//! the hand-off — a child that inherits a terminal still in raw mode with the
//! alternate screen up gets a tty it cannot use and leaves the operator's shell
//! wrecked when it exits — and second the fact that a build which touched the
//! desk's own code is *offered* a restart rather than given one. The desk is
//! live; restarting it out from under an operator mid-approval is not a thing a
//! keystroke may decide.
//!
//! Third, and the one that was missing on the first pass: this client has a
//! background task reading the *same stdin* the child is about to want. Left
//! running, it competes with Claude for every keystroke and then replays what
//! it stole into the desk's own command line when the screen comes back —
//! under Operator posture, a sentence typed at Claude becomes a line the
//! palette resolves. So the reader is paused around the child and whatever
//! queued behind it is thrown away, and both are pinned here by position.
//!
//! The spawner is fake, deliberately: what a real `qlab build` does with an
//! inherited tty cannot be observed from a test harness that has no tty, so
//! what is testable is the sequence around it, and that is what this file is.

#![cfg(feature = "operator")]

use atlas::cmd::{self, Command, Resolved, Scope};
use atlas::handoff::{self, Child, Host};
use atlas::store::{Posture, Store};

/// Every call the hand-off made, in the order it made them.
#[derive(Default)]
struct Fake {
    calls: Vec<String>,
    exit: Option<i32>,
    spawn_fails: bool,
    dirty: bool,
}

impl Host for Fake {
    fn pause_input(&mut self) {
        self.calls.push("pause".into());
    }

    fn resume_input(&mut self) {
        self.calls.push("resume".into());
    }

    fn drain_input(&mut self) {
        self.calls.push("drain".into());
    }

    fn leave_screen(&mut self) -> std::io::Result<()> {
        self.calls.push("leave".into());
        Ok(())
    }

    fn spawn(&mut self, argv: &[String]) -> std::io::Result<Option<i32>> {
        self.calls.push(format!("spawn:{}", argv.join(" ")));
        match self.spawn_fails {
            true => Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "no such file",
            )),
            false => Ok(self.exit),
        }
    }

    fn enter_screen(&mut self) -> std::io::Result<()> {
        self.calls.push("enter".into());
        Ok(())
    }

    fn redraw(&mut self) {
        self.calls.push("redraw".into());
    }

    fn desk_sources_changed(&mut self) -> bool {
        self.calls.push("git".into());
        self.dirty
    }
}

// -- the sequence -----------------------------------------------------------

#[test]
fn the_screen_is_given_up_before_the_child_and_taken_back_after_it() {
    let mut host = Fake {
        exit: Some(0),
        ..Default::default()
    };
    let notes = handoff::run(Child::Build("add a visual".into()), "qlab", &mut host);
    assert_eq!(
        host.calls,
        vec![
            "pause".to_string(),
            "leave".to_string(),
            "spawn:qlab build add a visual".to_string(),
            "enter".to_string(),
            "drain".to_string(),
            "resume".to_string(),
            "redraw".to_string(),
            // The git question is last and outside the sequence: it is asked of
            // the checkout after the screen is back, not of the child.
            "git".to_string(),
        ],
        "the reader stops before the screen comes down and starts after it goes back"
    );
    // A clean run over a clean checkout says nothing: the operator watched it
    // happen.
    assert!(notes.is_empty(), "{notes:?}");
}

#[test]
fn a_child_that_never_started_still_gets_the_screen_back_and_says_why() {
    // The failure that matters most. A spawn error between the leave and the
    // enter, reported by returning early, is a workstation that never repaints
    // over a shell that no longer has raw mode — which looks exactly like a
    // hang.
    let mut host = Fake {
        spawn_fails: true,
        ..Default::default()
    };
    let notes = handoff::run(Child::Build("add a visual".into()), "qlab", &mut host);
    assert_eq!(
        host.calls,
        vec![
            "pause".to_string(),
            "leave".to_string(),
            "spawn:qlab build add a visual".to_string(),
            "enter".to_string(),
            "drain".to_string(),
            "resume".to_string(),
            "redraw".to_string(),
            "git".to_string(),
        ],
        "a child that never started still hands the terminal back"
    );
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(notes[0].contains("qlab"), "{notes:?}");
}

#[test]
fn a_child_that_refused_carries_its_own_exit_code_back() {
    // `qlab build` refuses by name when the Claude binary is absent, and its
    // sentence is printed onto a screen this client is about to paint over. The
    // code is what survives, so the note has to name it.
    let mut host = Fake {
        exit: Some(2),
        ..Default::default()
    };
    let notes = handoff::run(Child::Build("add a visual".into()), "qlab", &mut host);
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(notes[0].contains('2'), "{notes:?}");
}

#[test]
fn a_build_carries_the_request_through_to_the_child() {
    let mut host = Fake {
        exit: Some(0),
        ..Default::default()
    };
    handoff::run(
        Child::Build("add a heatmap visual".into()),
        "qlab",
        &mut host,
    );
    assert_eq!(host.calls[2], "spawn:qlab build add a heatmap visual");
}

#[test]
fn a_build_that_touched_the_desks_code_offers_a_restart_and_never_performs_one() {
    let mut host = Fake {
        exit: Some(0),
        dirty: true,
        ..Default::default()
    };
    let notes = handoff::run(Child::Build("add a visual".into()), "qlab", &mut host);
    assert!(host.calls.contains(&"git".to_string()));
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(notes[0].contains("qlab --restart runtime"), "{notes:?}");
    // Nothing in the sequence restarts anything: the only child spawned is the
    // build itself.
    assert_eq!(
        host.calls
            .iter()
            .filter(|c| c.starts_with("spawn:"))
            .count(),
        1
    );
}

#[test]
fn a_build_that_changed_nothing_the_desk_serves_offers_nothing() {
    let mut host = Fake {
        exit: Some(0),
        ..Default::default()
    };
    let notes = handoff::run(Child::Build("read the code".into()), "qlab", &mut host);
    assert!(host.calls.contains(&"git".to_string()));
    assert!(notes.is_empty(), "{notes:?}");
}

// -- what the child left in the queue ---------------------------------------

/// The keystrokes of one sentence, as the reader would have posted them.
fn typed(text: &str) -> Vec<atlas::bus::AppEvent> {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    text.chars()
        .map(|c| atlas::bus::AppEvent::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)))
        .collect()
}

#[test]
fn what_was_typed_at_claude_is_thrown_away_and_everything_else_is_kept() {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    for ev in typed("/exec") {
        tx.send(ev).unwrap();
    }
    tx.send(atlas::bus::AppEvent::Tick).unwrap();
    tx.send(atlas::bus::AppEvent::Resize).unwrap();

    let kept = handoff::drain_input(&mut rx);

    // Input is gone. Everything else survives, in order: a snapshot or a
    // stream event that arrived while the operator was in Claude is news about
    // the desk, and dropping it would leave the frame stale for a whole beat.
    assert!(
        !kept.iter().any(|ev| matches!(
            ev,
            atlas::bus::AppEvent::Key(_) | atlas::bus::AppEvent::Mouse(_)
        )),
        "a keystroke survived the drain"
    );
    // `AppEvent` is deliberately neither `Debug` nor `Clone` — see `bus` — so
    // the assertion counts what came back rather than printing it.
    assert_eq!(kept.len(), 2, "the two non-input events must both survive");
    // And the channel is empty afterwards, not merely peeked at.
    assert!(rx.try_recv().is_err());
}

#[test]
fn a_sentence_typed_at_claude_never_lands_in_the_desks_command_line() {
    // The bug this exists for, proved by contrast: the same queue twice, once
    // without the drain and once with. Under Operator posture the first one
    // resolves — `/exec` is four characters from a scope that opens the box in
    // front of a fill — which is why "the reader was still running" is a
    // Critical and not an annoyance.
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    fn replay(events: Vec<atlas::bus::AppEvent>) -> String {
        let mut store = armed();
        let mut views = atlas::ui::views::Views::new();
        for ev in events {
            if let atlas::bus::AppEvent::Key(key) = ev {
                atlas::ui::shell::on_key(key, &mut store, &mut views);
            }
        }
        store.cmd.text().to_string()
    }

    // Built twice rather than cloned: `AppEvent` is not `Clone`, on purpose.
    let queued = || {
        let mut evs = typed("/exec");
        evs.push(atlas::bus::AppEvent::Key(KeyEvent::new(
            KeyCode::Char('x'),
            KeyModifiers::NONE,
        )));
        evs
    };

    // Undrained, it is a command line the operator never opened.
    assert_eq!(replay(queued()), "/execx");

    // Drained, there is nothing left to replay.
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    for ev in queued() {
        tx.send(ev).unwrap();
    }
    assert_eq!(replay(handoff::drain_input(&mut rx)), "");
}

// -- the argv ---------------------------------------------------------------

#[test]
fn the_child_is_the_desks_own_verb_and_never_claude_directly() {
    // One place decides what authority a Claude session gets, and it is the
    // Python verb: which tools, which MCP config, which persona. A client that
    // built a `claude` command line itself would be a second, unreviewed answer
    // to that question living where nothing tests it.
    // The launcher is a parameter and not an env read, so this asserts the
    // same thing in a shell that has `QLAB_BIN` set as in one that does not.
    let child = Child::Build("do it".into());
    assert_eq!(
        handoff::argv(&child, "/opt/qlab/bin/qlab"),
        vec!["/opt/qlab/bin/qlab", "build", "do it"]
    );
    // Destructured rather than matched, so a second `Child` added without a
    // `Command` that produces it fails to compile here. This file used to build
    // a `Child::Cli` nothing in the client could: `/cli` opens a pane
    // (`pane_column.rs`) and hands the terminal to nobody.
    let Child::Build(request) = &child;
    assert_eq!(request, "do it");
    // The other verb still has one spelling, next to the launcher it goes with,
    // and `pty.rs` pins the argv the pane builds out of the two.
    assert_eq!(handoff::CLI, "cli");
}

// -- the grammar ------------------------------------------------------------

fn armed() -> Store {
    let mut store = Store::default();
    store.posture = Posture::Operator;
    store
}

fn resolved(line: &str, store: &Store, posture: Posture) -> Resolved {
    cmd::resolve(&cmd::parse(line), store, posture)
}

#[test]
fn the_two_words_resolve_on_an_armed_desk() {
    // The trailing space is the accept, exactly as everywhere else in this
    // grammar: a complete word with nothing after it is still the picker, and
    // Enter on it rewrites the buffer rather than acting.
    let store = armed();
    assert_eq!(resolved("/cli ", &store, Posture::Operator), Resolved::Cli);
    assert_eq!(
        resolved("/build add a visual", &store, Posture::Operator),
        Resolved::Build("add a visual".into())
    );
}

#[test]
fn a_build_with_nothing_to_build_is_refused_rather_than_opened() {
    let store = armed();
    match resolved("/build ", &store, Posture::Operator) {
        Resolved::Refused(said) => assert!(said.contains("what to build"), "{said}"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn the_cli_takes_no_argument() {
    let store = armed();
    match resolved("/cli please", &store, Posture::Operator) {
        Resolved::Refused(said) => assert!(said.contains("takes no argument"), "{said}"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_read_only_window_may_open_neither() {
    // Both change things: `/build` edits this checkout, and `/cli` opens a
    // session that can start research on the desk. A window the desk has not
    // armed gets the same sentence every other write scope gives it.
    let store = Store::default();
    for line in ["/cli ", "/build add a visual"] {
        match resolved(line, &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(said.contains("not armed"), "{line}: {said}"),
            other => panic!("{line}: {other:?}"),
        }
    }
}

#[test]
fn both_words_are_write_scopes_so_a_glass_window_is_never_offered_them() {
    assert!(Scope::Cli.writes());
    assert!(Scope::Build.writes());
    let offered = cmd::suggestions(&cmd::parse(""), &Store::default(), Posture::Glass);
    for word in ["/cli", "/build"] {
        assert!(
            !offered.iter().any(|s| s.value == word),
            "{word} was offered to a window that cannot use it"
        );
    }
    let offered = cmd::suggestions(&cmd::parse(""), &armed(), Posture::Operator);
    for word in ["/cli", "/build"] {
        assert!(offered.iter().any(|s| s.value == word), "{word} is missing");
    }
}

#[test]
fn the_shell_hands_the_runtime_a_command_rather_than_acting_itself() {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    let mut store = armed();
    let mut views = atlas::ui::views::Views::new();
    // Two Enters, not one: the first accepts the picker into `/cli `, which is
    // what puts an operator in front of the scope before it acts.
    for c in "/cli".chars() {
        atlas::ui::shell::on_key(
            KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE),
            &mut store,
            &mut views,
        );
    }
    let accepted = atlas::ui::shell::on_key(
        KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        &mut store,
        &mut views,
    );
    assert_eq!(accepted, None, "the first Enter accepts the scope");
    let got = atlas::ui::shell::on_key(
        KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        &mut store,
        &mut views,
    );
    assert_eq!(got, Some(Command::OpenCli));
}

// -- which mind the desk reasons with ---------------------------------------

/// A desk whose owner says which backend runs Atlas.
///
/// Folded through `Store::apply` rather than assigned, so what is refused below
/// is refused off the payload a running client actually holds.
fn reasoning_with(backend: &str) -> Store {
    let mut store = Store::default();
    let snapshot: atlas::model::Snapshot = serde_json::from_str(&format!(
        r#"{{"llm": {{"reasoner": {{"backend": "{backend}", "model": "granite3.3:8b"}}}}}}"#
    ))
    .unwrap();
    store.apply(
        atlas::bus::AppEvent::Snapshot(Box::new(snapshot)),
        std::time::Instant::now(),
    );
    store.posture = Posture::Operator;
    store
}

#[test]
fn a_local_reasoner_is_refused_the_cli_by_name() {
    // The pane runs `qlab cli`, which is a Claude verb whatever this desk
    // reasons with. A window that opened it anyway would put a second mind in
    // the tab the operator configured for the first one.
    let store = reasoning_with("granite");
    match resolved("/cli ", &store, Posture::Operator) {
        Resolved::Refused(said) => {
            assert!(said.contains("Claude"), "{said}");
            assert!(said.contains("granite"), "{said}");
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_claude_desk_opens_the_pane_and_a_desk_that_named_nobody_is_unchanged() {
    // Silent on a claude reasoner, and silent when nothing has named one: a
    // refusal on an unnamed backend would be a distinction this client
    // invented, and the door is what asks that question (Task B2).
    for store in [reasoning_with("claude"), armed()] {
        assert_eq!(resolved("/cli ", &store, Posture::Operator), Resolved::Cli);
    }
}

#[test]
fn a_local_reasoner_still_gets_the_build() {
    // `/build` opens Claude Code on this checkout, which is a different
    // question from which mind runs the desk — and the MODELS card says so.
    let store = reasoning_with("granite");
    assert_eq!(
        resolved("/build add a visual", &store, Posture::Operator),
        Resolved::Build("add a visual".into())
    );
}

#[test]
fn the_cli_brings_atlas_up_because_that_is_where_the_pane_is_drawn() {
    let mut store = armed();
    store.nav.view = atlas::store::ViewId::Book;
    let mut views = atlas::ui::views::Views::new();
    // The accepted scope, as the second Enter submits it: a bare `/cli`
    // is still the picker and rewrites the buffer instead of acting.
    let got = atlas::ui::shell::run_line("/cli ", &mut store, &mut views);
    assert_eq!(got, Some(Command::OpenCli));
    assert_eq!(
        store.nav.view,
        atlas::store::ViewId::Atlas,
        "a child started on a column nobody is looking at"
    );
}
