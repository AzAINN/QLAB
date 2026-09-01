//! Giving the terminal away for a while: Claude Code on this checkout, run from inside the workstation.
//!
//! `/cli` and `/build` are the only keys on this desk that stop being about the
//! desk, and only one of the two still comes through here. Everything else
//! turns a keystroke into a request; these two turn it into a *child process* —
//! `/cli`'s child gets a pseudoterminal of its own inside ATLAS's column
//! (`pty.rs`) and leaves the desk on screen, and only `/build`'s wants the same
//! terminal this client is painting on. So the whole of this module is one
//! sequence — give the screen back, run the child, take the screen again,
//! repaint — and the reason it is a module with a trait in front of it rather
//! than four lines in `main.rs` is invariant 10: `main.rs` has no test harness,
//! and an order that has to be right is the last thing that should only be
//! checkable by running it.
//!
//! **What is spawned is the desk's own verb, never `claude` directly.** Which
//! tools a Claude session gets, which MCP config it reads and which persona it
//! wears is decided in one place — `qlab/tui/claude.py`, tested there — and a
//! client that assembled its own `claude` command line would be a second,
//! unreviewed answer to that question living where nothing checks it. This file
//! knows two words: `build`, which it spawns, and [`CLI`], which the pane
//! does.
//!
//! **The reader is paused across the whole of it.** This client keeps a
//! background task on the same stdin the child is about to want; left running
//! it competes with Claude for every keystroke, and worse, it *posts what it
//! stole onto the bus* — so on return the desk's own command line resolves a
//! sentence the operator typed at Claude, under whatever posture the desk is
//! armed to. So `pause_input` comes before the screen goes down, `resume_input`
//! after it comes back, and `drain_input` in between throws away whatever
//! queued while the terminal was not this client's.
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
///
/// One variant, since `/cli` stopped handing the terminal over: the pane in
/// ATLAS's column is what an interactive Claude gets now, and a `Cli` arm here
/// that no `Command` could produce was a hand-off nothing could ask for. The
/// enum stays an enum because the `match` below is what makes a second child
/// added later a compile error rather than a silent inheritance of `/build`'s
/// rules.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Child {
    /// Claude Code on this checkout, with the operator's request as turn one.
    Build(String),
}

/// The desk's verb for an interactive Claude session: `<launcher> cli`.
///
/// Here rather than in `pty.rs`, beside [`launcher`] and the paragraph above
/// that says why the child is a `qlab` verb at all: "which qlab" and "which
/// verb" are one answer, and the pane is only the current caller of it. The
/// tests spell the word out rather than reading it from here — an assertion
/// that takes both sides from the same constant pins nothing.
pub const CLI: &str = "cli";

/// The launcher this client hands work to.
///
/// `QLAB_BIN` overrides it for a checkout whose `qlab` is not the one on PATH —
/// the same shape as `QLAB_ATLAS_BIN` in the other direction, and the same
/// reason: a developer's copy should win over an installed one only when they
/// say so. The desk's launcher publishes it, so `/build` opens *this* checkout
/// rather than whichever `qlab` a shell happens to resolve.
///
/// Read once, at the composition root, and passed down. `argv` and `run` take
/// the word rather than reaching for the environment themselves: a function
/// whose result depends on an ambient variable is one whose test passes or
/// fails according to the shell it was run from.
pub fn launcher() -> String {
    match std::env::var("QLAB_BIN") {
        Ok(said) if !said.trim().is_empty() => said,
        _ => "qlab".to_string(),
    }
}

/// What gets spawned, in full.
///
/// A `match` over one variant, deliberately: it is the compiler's half of the
/// note on [`Child`], and a `let` would let a second child slip through with
/// `/build`'s argv.
pub fn argv(child: &Child, launcher: &str) -> Vec<String> {
    match child {
        Child::Build(request) => vec![launcher.to_string(), "build".to_string(), request.clone()],
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
    /// Stop the background task reading this process's stdin.
    ///
    /// First, before anything touches the screen. The child is about to want
    /// the same fd, and a reader left running does not merely lose races for
    /// keystrokes — it posts what it wins onto the bus, where the desk resolves
    /// it as a command line on return.
    fn pause_input(&mut self);
    /// Start a fresh reader on the stdin the child gave back.
    fn resume_input(&mut self);
    /// Throw away whatever input queued while the terminal was not ours —
    /// both what the terminal still holds and what is already on the bus.
    fn drain_input(&mut self);
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
pub fn run(child: Child, launcher: &str, host: &mut dyn Host) -> Vec<String> {
    let argv = argv(&child, launcher);
    let mut notes = Vec::new();

    // Before the screen, and unconditionally: every path below reaches the
    // matching `resume_input`, the spawn failure included. A reader left
    // paused is a workstation that ignores the keyboard forever, which is the
    // same failure as the one this call prevents, one step later.
    host.pause_input();
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
    // Drained *before* the reader is restarted, not after: a drain that ran
    // with a live reader beside it would race the operator's first real
    // keystroke and eat it, and the point is to throw away what belongs to the
    // child rather than what belongs to the person who just came back.
    host.drain_input();
    host.resume_input();
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

    // Only ever an offer: a desk restarted out from under an operator who was
    // mid-approval is not something a keystroke may decide (invariant 8 says
    // the restart is needed, not that this client may perform it).
    //
    // Asked of every child, because a build is the only child there is. It used
    // to be `matches!(child, Child::Build(_))`, guarding against a `/cli`
    // hand-off whose session has no filesystem tools — that child is a pane
    // now, and a condition whose false arm nothing can construct is a condition
    // no test can reach. A second variant added to `Child` breaks `argv` above,
    // which is where the rule is restated for whoever adds it.
    if host.desk_sources_changed() {
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
///
/// The wait is a *blocking* wait inside an async runtime, which is why it is
/// wrapped: a Claude session is minutes to hours, and a worker thread parked on
/// it for that long is one fewer thread for the poller, the stream and the
/// ticker — all of which keep running behind the child and all of which the
/// first frame back depends on. `block_in_place` hands the parked worker's
/// queue to the rest of the pool for the duration.
///
/// It requires the multi-thread runtime and panics on `current_thread`. The
/// binary is a bare `#[tokio::main]` with `rt-multi-thread`, so that holds
/// today; a flavor change would have to move this to `spawn_blocking`.
#[cfg(feature = "operator")]
pub fn spawn_inheriting(argv: &[String]) -> std::io::Result<Option<i32>> {
    tokio::task::block_in_place(|| {
        let mut child = std::process::Command::new(&argv[0])
            .args(&argv[1..])
            .spawn()?;
        Ok(child.wait()?.code())
    })
}

/// Take everything queued on the bus while the terminal was not ours, throw the
/// input away, and hand the rest back.
///
/// The input is the whole point: a key event on this bus was read off the same
/// stdin the child was using, so it is a fragment of something the operator said
/// to Claude and not to the desk. Everything else — a snapshot, a stream event,
/// a tick — is news about the desk that arrived while the operator was away, and
/// dropping it would leave the first frame back stale.
///
/// Non-blocking by construction: it takes what is already queued and stops. A
/// receiver that waited would hang the return on a desk with nothing to say.
#[cfg(feature = "operator")]
pub fn drain_input(
    rx: &mut tokio::sync::mpsc::UnboundedReceiver<crate::bus::AppEvent>,
) -> Vec<crate::bus::AppEvent> {
    let mut kept = Vec::new();
    while let Ok(ev) = rx.try_recv() {
        if !matches!(
            ev,
            crate::bus::AppEvent::Key(_) | crate::bus::AppEvent::Mouse(_)
        ) {
            kept.push(ev);
        }
    }
    kept
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
