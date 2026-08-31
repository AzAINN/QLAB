//! The child the desk owns: `qlab cli` on a pseudoterminal, and every way it can end.
//!
//! `handoff.rs` gives the whole terminal away and gets it back when the child
//! exits; for the length of a Claude session the desk — proposal card, audit
//! stream, your-call pointers — is not on the screen. This module is the other
//! arrangement: the child gets a terminal of its own, a pty this process is on
//! the master end of, so the desk keeps drawing beside it. Everything here is
//! that child's lifecycle and nothing else — bytes out, keystrokes in, a size,
//! a stop, and an ending. What the bytes look like on screen is a renderer's
//! problem; where the events go is the store's.
//!
//! **Gated with the commands that can reach it.** The monitoring build has no
//! `Command` variant that opens a pane, so it has no reason to carry a spawn —
//! and the design's claim is that it *contains* none, which is a property only
//! absence can hold. `handoff.rs` states the same rule for the same reason.
//!
//! **The child is always the desk's own verb.** Which tools a Claude session
//! gets, which MCP config it reads and which persona it wears is decided in
//! `qlab/tui/claude.py` and tested there. A client that assembled its own
//! `claude` command line would be a second, unreviewed answer to that question
//! living where nothing checks it.
//!
//! **Blocking threads, not tasks.** This crate's tokio is built without
//! `process` or `io-util`, so there is no async reader for a pty fd here and
//! adding those features to get one would pull a second process API into a
//! crate that already has the one it needs. One thread per child does the
//! reading and the reaping; the sender it posts on is the same unbounded
//! channel kind the bus already is, which is what lets a blocking thread hand
//! work to an async loop without a runtime handle.

use portable_pty::{
    ChildKiller, CommandBuilder, ExitStatus, MasterPty, NativePtySystem, PtyPair, PtySize,
    PtySystem,
};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

/// What the pane draws, so what the child is told it is writing for.
///
/// Stated rather than inherited: the pane parses the child through `vt100`,
/// which implements xterm-256color. A `TERM` copied from whatever terminal
/// launched this client would have the child emitting sequences the parser has
/// never seen — or, for `dumb`, refusing to draw anything at all.
const TERM: &str = "xterm-256color";

/// How wide a child's name may be inside a sentence about it.
const LABEL: usize = 60;

/// The status of a child that did not choose its own ending.
///
/// A signal death carries no exit code; portable-pty stands `1` in for one,
/// which would make a hangup indistinguishable from a child that failed on
/// purpose. A negative number says "this is not an exit code" instead, and the
/// sentence beside it says which ending it was.
const UNKNOWN: i32 = -1;

/// What the desk hears from the child it started.
///
/// Three things, because there are three: what the child wrote, the fact that
/// it is over, and the fact that it never began. The second and third each
/// carry their own sentence rather than a code the caller would have to turn
/// into one — a process that vanished with no explanation is the hung-key
/// reading this workstation refuses everywhere else.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PtyEvent {
    /// Bytes exactly as the child wrote them, escape sequences included. Not
    /// decoded here: a read can land mid-sequence and mid-codepoint, and only
    /// the parser downstream holds enough state to know.
    Bytes(Vec<u8>),
    /// The child is gone and has been reaped. `status` is [`UNKNOWN`] when the
    /// ending was not one the child chose.
    Exited { status: i32, said: String },
    /// Something that should have reached the child did not — it never started,
    /// or a keystroke went nowhere.
    Failed { said: String },
}

/// Where a session's events go.
///
/// Unbounded on purpose, and the reason is not throughput. A bounded sender
/// leaves two choices on a full channel and both are wrong here: blocking parks
/// the reader thread, which stops draining the pty and back-pressures the child
/// into a stall the operator cannot see; dropping loses bytes out of the middle
/// of an escape sequence, and a `vt100` screen that lost half a sequence stays
/// wrong for the rest of the session.
pub type Events = tokio::sync::mpsc::UnboundedSender<PtyEvent>;

/// Why there is no session.
///
/// Carries the same sentence the [`PtyEvent::Failed`] beside it carries — see
/// [`refuse`] — because the caller and the desk need different things from one
/// failure: a value that says there is nothing to hold, and a line to read.
#[derive(Debug, Clone)]
pub struct PtyError {
    said: String,
}

impl PtyError {
    pub fn said(&self) -> &str {
        &self.said
    }
}

impl std::fmt::Display for PtyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.said)
    }
}

impl std::error::Error for PtyError {}

/// What to run on the pty.
///
/// A trait in front of one real answer, for `handoff::Host`'s reason: the real
/// child is a Claude session that wants an owner on a port, a credential and
/// minutes of a person's attention, none of which a test may assume — so what
/// is testable is a scripted child that exercises the same lifecycle.
pub trait Spawn {
    fn command(&self) -> CommandBuilder;
}

/// The desk's own verb: `<launcher> cli`, and the five variables the pane
/// states rather than leaves to chance.
///
/// **What the child inherits.** `CommandBuilder::new` seeds itself from
/// `std::env::vars_os` (`cmdbuilder::get_base_env`), so a child spawned from an
/// untouched builder gets this process's whole environment — the same thing
/// `handoff::spawn_inheriting` gives the full-screen hand-off, which is what
/// makes the pane the *same session* rather than a poorer one. A `qlab cli`
/// started with a cleared environment would lose the credential, the locale and
/// the certificate paths its own tools need, and would fail in ways that look
/// like the desk's fault.
///
/// **What it is told anyway.** `env` below is the short list this pane will not
/// leave to an inherited value: `TERM` because the pane, not the outer
/// terminal, is what the child is drawing on; `QLAB_BIN` because "which qlab"
/// has one answer here and it may be a word the parent never spelled; the rest
/// because a promise this module makes should not rest on a base environment
/// assembled inside a dependency.
///
/// Read once, here, and carried — the rule [`crate::handoff::launcher`] states
/// and for the same reason: a `command()` that reached for the environment
/// itself would build a different child depending on which shell started this
/// process, and would test differently for exactly the same reason.
pub struct DeskCli {
    launcher: String,
    cwd: Option<PathBuf>,
    env: Vec<(String, String)>,
}

impl DeskCli {
    /// Read this process's environment and keep the answers.
    pub fn from_env() -> Self {
        let launcher = crate::handoff::launcher();
        let mut env = Vec::new();
        // PATH is not merely for the child's convenience: `CommandBuilder`
        // resolves `argv[0]` against its *own* PATH (`cmdbuilder::search_path`),
        // so which `qlab` gets started is decided by this value and not by the
        // one `execvp` would have used. HOME is where the child's own config
        // lives, and is what `CommandBuilder` falls back to for the working
        // directory.
        for key in ["PATH", "HOME"] {
            if let Ok(value) = std::env::var(key) {
                if !value.is_empty() {
                    env.push((key.to_string(), value));
                }
            }
        }
        env.push(("TERM".to_string(), TERM.to_string()));
        // Forwarded only when this process has one. An unset variable means both
        // sides already agree on the default port, and writing a number here
        // would pin the child to a port this client had merely guessed.
        if let Ok(port) = std::env::var("QLAB_UI_PORT") {
            if !port.trim().is_empty() {
                env.push(("QLAB_UI_PORT".to_string(), port));
            }
        }
        // Always, and always the word this client resolved: a child that starts
        // a qlab of its own must start the one this desk is running, not
        // whichever a fresh PATH lookup happens to find.
        env.push(("QLAB_BIN".to_string(), launcher.clone()));
        Self {
            launcher,
            cwd: std::env::current_dir().ok(),
            env,
        }
    }

    /// The explicit form, for a caller that has already read its environment.
    pub fn new(
        launcher: impl Into<String>,
        cwd: Option<PathBuf>,
        env: Vec<(String, String)>,
    ) -> Self {
        Self {
            launcher: launcher.into(),
            cwd,
            env,
        }
    }
}

impl Spawn for DeskCli {
    fn command(&self) -> CommandBuilder {
        let mut command = CommandBuilder::new(&self.launcher);
        command.arg("cli");
        // Overrides, on top of the base environment the builder seeded itself
        // with. `env` marks each of these as the caller's own, which is what
        // `iter_extra_env_as_str` — and the test below — can tell apart from a
        // value that merely happened to be inherited.
        for (key, value) in &self.env {
            command.env(key, value);
        }
        // The full-screen hand-off inherits this process's directory. A pane
        // child that quietly ran in $HOME instead — `CommandBuilder`'s default
        // when no cwd is set — would be a different session from the one
        // `/build` opens, in a checkout nobody chose.
        if let Some(cwd) = &self.cwd {
            command.cwd(cwd);
        }
        command
    }
}

/// A child on a pty, for as long as the desk holds this.
pub struct PtySession {
    master: Box<dyn MasterPty + Send>,
    /// Behind a lock because the writer wants `&mut` and a keystroke arrives
    /// through `&self`. Poisoning is recovered from rather than unwrapped: a
    /// keyboard that stopped working because some earlier write panicked is a
    /// worse failure than the one it would be reporting.
    writer: Mutex<Box<dyn Write + Send>>,
    killer: Box<dyn ChildKiller + Send + Sync>,
    /// Written by the reader thread when the child has been reaped, read by
    /// every method that would otherwise talk to a process that is not there.
    ended: Arc<AtomicBool>,
    killed: bool,
    events: Events,
    label: String,
}

impl PtySession {
    /// Start the child, and start reading it.
    ///
    /// Every failure before the child exists is a [`PtyError`] *and* a
    /// [`PtyEvent::Failed`]; every ending after it exists is a
    /// [`PtyEvent::Exited`], whoever caused it. One ending, one path.
    pub fn open(
        spawn: &dyn Spawn,
        cols: u16,
        rows: u16,
        events: Events,
    ) -> Result<PtySession, PtyError> {
        let command = spawn.command();
        let label = label_of(&command);

        let PtyPair { slave, master } = NativePtySystem::default()
            .openpty(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|err| {
                refuse(
                    &events,
                    format!("could not open a terminal for `{label}`: {err}"),
                )
            })?;

        // The reader and the writer before the child, deliberately: both need
        // only the master, and taking them first means every failure here
        // happens while there is still no process for an early return to leave
        // running behind it.
        let reader = master.try_clone_reader().map_err(|err| {
            refuse(
                &events,
                format!("could not read the terminal `{label}` would run on: {err}"),
            )
        })?;
        let writer = master.take_writer().map_err(|err| {
            refuse(
                &events,
                format!("could not write to the terminal `{label}` would run on: {err}"),
            )
        })?;

        let child = slave.spawn_command(command).map_err(|err| {
            refuse(
                &events,
                format!(
                    "could not run `{label}`: {err} — is it on PATH? \
                     ($QLAB_BIN names the qlab this desk starts.)"
                ),
            )
        })?;
        // The child has its own copy of this end now; the one this process
        // still holds is a descriptor per session that nothing here will ever
        // read or write. On Linux it is worse than a leak — a slave still open
        // in *any* process keeps the master from reading EOF, so the exit below
        // would never be reached. macOS ends the read when the last process
        // using the pty as its controlling terminal exits, which is why the
        // suite on this machine cannot tell the difference; it is a platform
        // difference, not a spare line.
        drop(slave);

        let mut killer = child.clone_killer();
        let ended = Arc::new(AtomicBool::new(false));
        let watching = std::thread::Builder::new()
            .name("atlas-pty".to_string())
            .spawn({
                let events = events.clone();
                let ended = Arc::clone(&ended);
                let label = label.clone();
                let killer = child.clone_killer();
                move || watch(reader, child, killer, &label, &ended, &events)
            });
        if let Err(err) = watching {
            // Nothing would ever read or reap this child, which makes it an
            // orphan holding a terminal rather than a session.
            let _ = killer.kill();
            return Err(refuse(&events, format!("could not watch `{label}`: {err}")));
        }

        Ok(PtySession {
            master,
            writer: Mutex::new(writer),
            killer,
            ended,
            killed: false,
            events,
            label,
        })
    }

    /// Send keystrokes to the child.
    ///
    /// A write to a child that has ended is *said* rather than swallowed: these
    /// bytes are something the operator aimed at the child, and a keyboard that
    /// silently stopped arriving anywhere is the hung-client reading a pane
    /// must never produce.
    pub fn write(&self, bytes: &[u8]) {
        if self.ended.load(Ordering::SeqCst) {
            let _ = self.events.send(PtyEvent::Failed {
                said: format!(
                    "`{}` has ended — what you typed did not reach it",
                    self.label
                ),
            });
            return;
        }
        let mut writer = self.writer.lock().unwrap_or_else(|held| held.into_inner());
        let wrote = writer.write_all(bytes).and_then(|()| writer.flush());
        if let Err(err) = wrote {
            let _ = self.events.send(PtyEvent::Failed {
                said: format!("`{}` would not take what you typed: {err}", self.label),
            });
        }
    }

    /// Tell the child how big its terminal is now.
    ///
    /// Silent on a child that has ended, and only there: nobody *asked* for a
    /// resize — the window changed shape — so there is no lost intent to
    /// report. A resize that fails while the child is alive is a different
    /// thing and is said.
    pub fn resize(&self, cols: u16, rows: u16) {
        if self.ended.load(Ordering::SeqCst) {
            return;
        }
        let size = PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        };
        if let Err(err) = self.master.resize(size) {
            let _ = self.events.send(PtyEvent::Failed {
                said: format!(
                    "`{}` could not be told the pane is {cols}x{rows}: {err}",
                    self.label
                ),
            });
        }
    }

    /// Stop the child.
    ///
    /// Idempotent because the callers cannot be made to agree: closing the
    /// pane, quitting the workstation and dropping this session are three paths
    /// to one kill, and a second signal at a pid this process has not reaped —
    /// or worse, has — is not a thing to leave to call order.
    ///
    /// A hangup, which is what a real terminal sends when its window closes and
    /// what portable-pty's cloned killer can send, holding only the pid. A child
    /// that ignores SIGHUP also gets the tty's own hangup when this session's
    /// master is dropped; one that survives both has to be dealt with from a
    /// shell. The *ending* is not announced from here: it comes from the reader
    /// thread, so a kill and a child that died on its own are one path.
    pub fn kill(&mut self) {
        if self.killed || self.ended.load(Ordering::SeqCst) {
            return;
        }
        self.killed = true;
        if let Err(err) = self.killer.kill() {
            let _ = self.events.send(PtyEvent::Failed {
                said: format!("`{}` would not stop: {err}", self.label),
            });
        }
    }
}

impl std::fmt::Debug for PtySession {
    /// Written out rather than derived: a pty master, a writer and a killer are
    /// trait objects with nothing to print, and `Store` — which will hold one —
    /// derives `Debug`. What is worth reading is which child this is and whether
    /// it is still there.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PtySession")
            .field("child", &self.label)
            .field("ended", &self.ended.load(Ordering::SeqCst))
            .field("killed", &self.killed)
            .finish_non_exhaustive()
    }
}

impl Drop for PtySession {
    /// A child whose session is gone has nobody reading it, nobody typing at it
    /// and no way to be stopped. The pty dies with this client either way, and
    /// a silent orphan holding a terminal is the worse of the two endings.
    fn drop(&mut self) {
        self.kill();
    }
}

/// Read the child until its end of the pty closes, then reap it and say how it
/// went.
///
/// One thread for both halves rather than two, and that is what orders the
/// events: EOF on the master means the child's side is closed, so every byte it
/// will ever write has already been posted by the time the exit is. A separate
/// waiter could announce the ending while the last of the output was still in
/// flight, and the pane would lose whatever the child said on its way out —
/// which, for a session that failed, is the only part that mattered.
fn watch(
    mut reader: Box<dyn Read + Send>,
    mut child: Box<dyn portable_pty::Child + Send + Sync>,
    mut killer: Box<dyn ChildKiller + Send + Sync>,
    label: &str,
    ended: &AtomicBool,
    events: &Events,
) {
    let mut buf = [0u8; 8192];
    loop {
        match reader.read(&mut buf) {
            // The child's end closed. portable-pty maps Linux's EIO onto this
            // too, so one arm covers both platforms' spelling of a hangup.
            Ok(0) => break,
            Ok(read) => {
                if events.send(PtyEvent::Bytes(buf[..read].to_vec())).is_err() {
                    // Nobody is listening any more, and a child writing into a
                    // pty nobody drains blocks forever once the buffer fills —
                    // which would park this thread in the `wait` below for good.
                    let _ = killer.kill();
                    break;
                }
            }
            Err(err) if err.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }

    // Reaped here rather than by the session, because this is the only place
    // that knows the child's end is closed. A `wait` on the desk's own thread
    // would park the frame loop for as long as the session lasted.
    let (status, said) = match child.wait() {
        Ok(status) => (code(&status), ending(label, &status)),
        Err(err) => (
            UNKNOWN,
            format!("`{label}` ended, and could not be reaped: {err}"),
        ),
    };
    // Set before the event is posted, not after. A caller that wrote a
    // keystroke the instant it read the exit would otherwise reach a session
    // that still believed its child was alive.
    ended.store(true, Ordering::SeqCst);
    let _ = events.send(PtyEvent::Exited { status, said });
}

/// The number a caller may compare against.
fn code(status: &ExitStatus) -> i32 {
    match status.signal() {
        Some(_) => UNKNOWN,
        None => status.exit_code() as i32,
    }
}

/// What happened, in a sentence, for each of the three ways a child ends.
fn ending(label: &str, status: &ExitStatus) -> String {
    match status.signal() {
        Some(signal) => format!("`{label}` was killed ({signal})"),
        None if status.success() => format!("`{label}` ended on its own"),
        None => format!("`{label}` exited {}", status.exit_code()),
    }
}

/// Say it once, to both readers: the caller who needs a value and the desk that
/// needs a line.
fn refuse(events: &Events, said: String) -> PtyError {
    let _ = events.send(PtyEvent::Failed { said: said.clone() });
    PtyError { said }
}

/// What the sentences call the child.
fn label_of(command: &CommandBuilder) -> String {
    let full = command
        .get_argv()
        .iter()
        .map(|word| word.to_string_lossy())
        .collect::<Vec<_>>()
        .join(" ");
    // A `said` becomes one line on a desk of finite width. `qlab cli` is two
    // words; a child spawned with a script as its argument is not, and would
    // push the reason off the end of the line it shares.
    match full.char_indices().nth(LABEL) {
        Some((at, _)) => format!("{}…", &full[..at]),
        None => full,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;

    fn argv(command: &CommandBuilder) -> Vec<String> {
        command
            .get_argv()
            .iter()
            .map(|word| word.to_string_lossy().into_owned())
            .collect()
    }

    #[test]
    fn the_child_is_the_desks_own_verb_and_carries_what_it_was_given() {
        let cli = DeskCli::new(
            "/opt/qlab/bin/qlab",
            Some(PathBuf::from("/tmp")),
            vec![("PATH".to_string(), "/bin".to_string())],
        );
        let command = cli.command();
        assert_eq!(argv(&command), ["/opt/qlab/bin/qlab", "cli"]);
        assert_eq!(command.get_env("PATH"), Some(OsStr::new("/bin")));
        assert_eq!(
            command.get_cwd().map(AsRef::as_ref),
            Some(OsStr::new("/tmp"))
        );
    }

    #[test]
    fn the_desk_states_the_five_rather_than_inheriting_them() {
        let command = DeskCli::from_env().command();
        let launcher = crate::handoff::launcher();
        // `iter_extra_env_as_str` and not `get_env`, and that is the whole point
        // of this test. The builder seeds itself from this process's
        // environment, so `get_env("TERM")` answers `Some` on any machine whose
        // own terminal happens to be xterm-256color — including this one, which
        // means a `get_env` assertion would pass just as happily against a
        // version of `from_env` that set nothing at all. The extra-env iterator
        // is the caller's own list, and only that.
        let stated: std::collections::BTreeMap<&str, &str> =
            command.iter_extra_env_as_str().collect();
        assert_eq!(stated.get("TERM"), Some(&TERM));
        // One reading of `QLAB_BIN`, not two: `handoff::launcher` is the crate's
        // answer to "which qlab", and a second reader here would be free to
        // disagree with the full-screen hand-off about which one `/cli` starts.
        assert_eq!(argv(&command), [launcher.clone(), "cli".to_string()]);
        assert_eq!(stated.get("QLAB_BIN"), Some(&launcher.as_str()));
        // PATH is what `CommandBuilder` resolves `qlab` against, so which one
        // starts is decided by this value.
        assert_eq!(
            stated.get("PATH").map(|path| path.to_string()),
            std::env::var("PATH").ok()
        );
        // And the child runs where the desk does, not in $HOME.
        assert_eq!(
            command.get_cwd().map(PathBuf::from),
            std::env::current_dir().ok()
        );
    }

    #[test]
    fn a_long_command_line_does_not_push_the_reason_off_the_sentence() {
        let mut command = CommandBuilder::new("sh");
        command.arg("-c");
        command.arg("x".repeat(400));
        let said = label_of(&command);
        assert!(said.starts_with("sh -c xxx"), "{said:?}");
        assert!(said.chars().count() <= LABEL + 1, "{said:?}");
    }
}
