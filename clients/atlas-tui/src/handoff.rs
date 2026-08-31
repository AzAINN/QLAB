//! Giving the terminal away for a while: the real Claude CLI, run from inside the workstation.
//!
//! `/cli` and `/build` are the only keys on this desk that stop being about the
//! desk. Everything else here turns a keystroke into a request; these two turn a
//! keystroke into a *child process* that wants the same terminal this client is
//! painting on. So the whole of this module is one sequence — give the screen
//! back, run the child, take the screen again, repaint — and the reason it is a
//! module with a trait in front of it rather than four lines in `main.rs` is
//! invariant 10: `main.rs` has no test harness, and an order that has to be
//! right is the last thing that should only be checkable by running it.
//!
//! **What is spawned is the desk's own verb, never `claude` directly.** Which
//! tools a Claude session gets, which MCP config it reads and which persona it
//! wears is decided in one place — `qlab/tui/claude.py`, tested there — and a
//! client that assembled its own `claude` command line would be a second,
//! unreviewed answer to that question living where nothing checks it. This file
//! knows two words: `cli` and `build`.
//!
//! Not a write path, and not routed through one. Nothing here reaches the owner
//! or holds a client; the posture gate that matters is in `cmd::resolve`, which
//! refuses both scopes to a window the desk has not armed, exactly as it refuses
//! `/mode`.

/// Which hand-off, and what it was asked for.
///
/// Ungated, like [`crate::cmd::ModelChoice`] and for the same reason: this is
/// what a line *means*, and the grammar is one grammar in both builds. What is
/// gated is the `Command` variant that can reach it and the `run` below that
/// acts on it — a monitoring build can describe this and has no key that
/// produces one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Child {
    /// Interactive Claude as Atlas, bounded to the owner's proxy plus
    /// read-only web.
    Cli,
    /// Claude Code on this checkout, with the operator's request as turn one.
    Build(String),
}

/// The launcher this client hands work to.
///
/// `QLAB_BIN` overrides it for a checkout whose `qlab` is not the one on PATH —
/// the same shape as `QLAB_ATLAS_BIN` in the other direction, and the same
/// reason: a developer's copy should win over an installed one only when they
/// say so.
fn launcher() -> String {
    match std::env::var("QLAB_BIN") {
        Ok(said) if !said.trim().is_empty() => said,
        _ => "qlab".to_string(),
    }
}

/// What gets spawned, in full.
pub fn argv(child: &Child) -> Vec<String> {
    match child {
        Child::Cli => vec![launcher(), "cli".to_string()],
        Child::Build(request) => vec![launcher(), "build".to_string(), request.clone()],
    }
}

/// The four things a hand-off does to the terminal, and the one question it
/// asks afterwards.
///
/// A trait so the sequence can be pinned without a tty. The real implementation
/// lives in the binary, beside the guard that owns the screen; what a child
/// actually does with an inherited terminal is not something a test harness with
/// no terminal can observe, so what is testable is the order around it — which
/// is exactly the part that has to be right.
pub trait Host {
    /// Leave the alternate screen, stop capturing the mouse, leave raw mode.
    fn leave_screen(&mut self) -> std::io::Result<()>;
    /// Run the child on the inherited terminal and wait. `None` is a child that
    /// was killed by a signal rather than exiting with a code.
    fn spawn(&mut self, argv: &[String]) -> std::io::Result<Option<i32>>;
    /// Take the screen back: raw mode, alternate screen, mouse capture.
    fn enter_screen(&mut self) -> std::io::Result<()>;
    /// Force a full repaint — the child wrote all over the buffer ratatui
    /// believes it left behind.
    fn redraw(&mut self);
    /// Whether `git status` shows work under the trees the desk serves.
    fn desk_sources_changed(&mut self) -> bool;
}

/// Hand the terminal over, run the child, take it back, and say what is left.
///
/// The screen is restored on **every** path, the spawn failure included. An
/// early return between the leave and the enter would leave the operator
/// looking at their own shell with raw mode still on and this client still
/// running behind it, which is indistinguishable from a hang.
///
/// The notes come back as values rather than being printed. There is nowhere to
/// print: anything written before `enter_screen` is wiped by the alternate
/// screen coming back, and anything after it lands under the frame. The runtime
/// turns them into toasts, which is where every other sentence this client has
/// to say already goes.
#[cfg(feature = "operator")]
pub fn run(child: Child, host: &mut dyn Host) -> Vec<String> {
    let argv = argv(&child);
    let mut notes = Vec::new();

    if let Err(err) = host.leave_screen() {
        // Said, and then attempted anyway: a failed restore usually means the
        // terminal is in a state neither this client nor the child can rely on,
        // and running the child blind is still better than a key that silently
        // did nothing (invariant 4).
        notes.push(format!("the screen would not come down cleanly: {err}"));
    }
    let outcome = host.spawn(&argv);
    if let Err(err) = host.enter_screen() {
        notes.push(format!("the screen would not come back cleanly: {err}"));
    }
    host.redraw();

    match outcome {
        Err(err) => notes.push(format!(
            "could not run `{}`: {err} — is qlab on PATH? ($QLAB_BIN overrides it.)",
            argv.join(" ")
        )),
        // The child's own refusal — an absent `claude`, an owner that is not
        // up — was printed onto a screen this client has just painted over, so
        // the code is all that survives. Named rather than swallowed: a
        // hand-off that flashed and vanished with no explanation is the
        // hung-key reading this workstation refuses everywhere else.
        Ok(Some(code)) if code != 0 => notes.push(format!(
            "`{}` exited {code} — run it from a shell to read what it said",
            argv.join(" ")
        )),
        Ok(None) => notes.push(format!("`{}` was killed before it exited", argv.join(" "))),
        Ok(Some(_)) => {}
    }

    // Only after a build, and only ever as an offer. `/cli`'s session has no
    // filesystem tools, so asking git after it would be a sentence with no
    // cause; and a desk restarted out from under an operator who was mid-
    // approval is not something a keystroke may decide (invariant 8 says the
    // restart is needed, not that this client may perform it).
    if matches!(child, Child::Build(_)) && host.desk_sources_changed() {
        notes.push(
            "this build touched code the desk serves — restart it to pick it up: \
             qlab --restart runtime"
                .to_string(),
        );
    }
    notes
}

/// Run a child on this process's own terminal and wait for it.
///
/// No pipes and no new process group: the child is interactive, so it needs the
/// tty, and Ctrl-C belongs to it while it holds the screen.
#[cfg(feature = "operator")]
pub fn spawn_inheriting(argv: &[String]) -> std::io::Result<Option<i32>> {
    let mut child = std::process::Command::new(&argv[0])
        .args(&argv[1..])
        .spawn()?;
    Ok(child.wait()?.code())
}

/// The two trees a change has to be restarted or rebuilt to be seen in.
#[cfg(feature = "operator")]
const DESK_SOURCES: [&str; 2] = ["qlab/", "clients/atlas-tui/"];

/// Whether the checkout has uncommitted work under the trees the desk serves.
///
/// A false answer on any failure, deliberately. The restart line is a
/// convenience; a complaint about version control after a build is noise the
/// operator cannot act on, and this is not a governance check — nothing is
/// permitted or refused on the strength of it.
#[cfg(feature = "operator")]
pub fn desk_sources_changed() -> bool {
    let Ok(out) = std::process::Command::new("git")
        .args(["status", "--porcelain"])
        .output()
    else {
        return false;
    };
    String::from_utf8_lossy(&out.stdout).lines().any(|line| {
        // Porcelain v1: two status columns, a space, then the path — and for a
        // rename, "old -> new", where the destination is what was written.
        let path = line.get(3..).unwrap_or("");
        let path = path.rsplit(" -> ").next().unwrap_or(path).trim_matches('"');
        DESK_SOURCES.iter().any(|tree| path.starts_with(tree))
    })
}
