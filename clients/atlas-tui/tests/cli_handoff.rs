//! `/cli` and `/build`: what the workstation does with the terminal while the real Claude CLI holds it.
//!
//! Two things are pinned here and neither is about HTTP. First the *order* of
//! the hand-off — a child that inherits a terminal still in raw mode with the
//! alternate screen up gets a tty it cannot use and leaves the operator's shell
//! wrecked when it exits — and second the fact that a build which touched the
//! desk's own code is *offered* a restart rather than given one. The desk is
//! live; restarting it out from under an operator mid-approval is not a thing a
//! keystroke may decide.
//!
//! The spawner is fake, deliberately: what a real `qlab cli` does with an
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
    let notes = handoff::run(Child::Cli, &mut host);
    assert_eq!(
        host.calls,
        vec![
            "leave".to_string(),
            "spawn:qlab cli".to_string(),
            "enter".to_string(),
            "redraw".to_string(),
        ],
        "the child may only run between a leave and an enter"
    );
    // A clean run says nothing: the operator watched it happen.
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
    let notes = handoff::run(Child::Cli, &mut host);
    assert_eq!(
        host.calls,
        vec![
            "leave".to_string(),
            "spawn:qlab cli".to_string(),
            "enter".to_string(),
            "redraw".to_string(),
        ]
    );
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(notes[0].contains("qlab"), "{notes:?}");
}

#[test]
fn a_child_that_refused_carries_its_own_exit_code_back() {
    // `qlab cli` refuses by name when the Claude binary is absent or the owner
    // is down, and its sentence is printed onto a screen this client is about
    // to paint over. The code is what survives, so the note has to name it.
    let mut host = Fake {
        exit: Some(2),
        ..Default::default()
    };
    let notes = handoff::run(Child::Cli, &mut host);
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(notes[0].contains('2'), "{notes:?}");
}

#[test]
fn a_build_carries_the_request_through_to_the_child() {
    let mut host = Fake {
        exit: Some(0),
        ..Default::default()
    };
    handoff::run(Child::Build("add a heatmap visual".into()), &mut host);
    assert_eq!(host.calls[1], "spawn:qlab build add a heatmap visual");
}

#[test]
fn a_build_that_touched_the_desks_code_offers_a_restart_and_never_performs_one() {
    let mut host = Fake {
        exit: Some(0),
        dirty: true,
        ..Default::default()
    };
    let notes = handoff::run(Child::Build("add a visual".into()), &mut host);
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
    let notes = handoff::run(Child::Build("read the code".into()), &mut host);
    assert!(host.calls.contains(&"git".to_string()));
    assert!(notes.is_empty(), "{notes:?}");
}

#[test]
fn the_cli_never_asks_git_anything() {
    // It changed no files by construction — its session has no filesystem
    // tools — so a restart offer after it would be a sentence with no cause.
    let mut host = Fake {
        exit: Some(0),
        dirty: true,
        ..Default::default()
    };
    let notes = handoff::run(Child::Cli, &mut host);
    assert!(!host.calls.contains(&"git".to_string()));
    assert!(notes.is_empty(), "{notes:?}");
}

// -- the argv ---------------------------------------------------------------

#[test]
fn the_child_is_the_desks_own_verb_and_never_claude_directly() {
    // One place decides what authority a Claude session gets, and it is the
    // Python verb: which tools, which MCP config, which persona. A client that
    // built a `claude` command line itself would be a second, unreviewed answer
    // to that question living where nothing tests it.
    assert_eq!(handoff::argv(&Child::Cli), vec!["qlab", "cli"]);
    assert_eq!(
        handoff::argv(&Child::Build("do it".into())),
        vec!["qlab", "build", "do it"]
    );
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
