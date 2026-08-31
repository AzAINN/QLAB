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

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
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

/// The five, given what the process environment said — which variables are
/// forwarded, which is forced, and which are dropped for being blank.
///
/// A pure function of its inputs rather than four `std::env::var` calls inside
/// [`DeskCli::from_env`], so the *policy* can be pinned by a test that neither
/// mutates the environment it runs in nor passes vacuously on a machine that
/// happens not to set `QLAB_UI_PORT`. `from_env` is its only caller and does
/// nothing but the reading.
fn stated_env(
    launcher: &str,
    path: Option<String>,
    home: Option<String>,
    port: Option<String>,
) -> Vec<(String, String)> {
    let mut env = Vec::new();
    // PATH is not merely for the child's convenience: `CommandBuilder` resolves
    // `argv[0]` against its *own* PATH (`cmdbuilder::search_path`), so which
    // `qlab` gets started is decided by this value and not by the one `execvp`
    // would have used. HOME is where the child's own config lives, and is what
    // `CommandBuilder` falls back to for the working directory.
    //
    // A blank value is dropped rather than forwarded: an empty PATH is worse
    // than an absent one, because it overrides the base environment's with a
    // list containing nowhere to look.
    for (key, value) in [("PATH", path), ("HOME", home)] {
        if let Some(value) = value.filter(|value| !value.is_empty()) {
            env.push((key.to_string(), value));
        }
    }
    env.push(("TERM".to_string(), TERM.to_string()));
    // Forwarded only when this process has one. An unset variable means both
    // sides already agree on the default port, and writing a number here would
    // pin the child to a port this client had merely guessed.
    if let Some(port) = port.filter(|port| !port.trim().is_empty()) {
        env.push(("QLAB_UI_PORT".to_string(), port));
    }
    // Always, and always the word this client resolved: a child that starts a
    // qlab of its own must start the one this desk is running, not whichever a
    // fresh PATH lookup happens to find.
    env.push(("QLAB_BIN".to_string(), launcher.to_string()));
    env
}

impl DeskCli {
    /// Read this process's environment and keep the answers.
    pub fn from_env() -> Self {
        let launcher = crate::handoff::launcher();
        let env = stated_env(
            &launcher,
            std::env::var("PATH").ok(),
            std::env::var("HOME").ok(),
            std::env::var("QLAB_UI_PORT").ok(),
        );
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
            // ESRCH is not a failure to stop — it is the child already being
            // gone, which is what was asked for. The window is real: the reader
            // thread reaps before it stores `ended`, so a kill landing between
            // the two would otherwise put "would not stop" on the desk about a
            // child that stopped perfectly well. 3 on every platform this
            // builds for, and spelled here rather than taken from `libc`, which
            // this crate does not otherwise depend on.
            const ESRCH: i32 = 3;
            if err.raw_os_error() != Some(ESRCH) {
                let _ = self.events.send(PtyEvent::Failed {
                    said: format!("`{}` would not stop: {err}", self.label),
                });
            }
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
            // Any other read error, with the child possibly still alive. Killed
            // before the loop is left, because from here on nothing drains the
            // master: BSD `ttyclose` makes a child block until the tty's output
            // queue is emptied, and this thread is its only drainer — so the
            // `wait` below would park on a live child forever, `ended` would
            // stay false, and the desk would go on showing a session that had
            // in fact stopped. Unreachable today (EIO arrives as `Ok(0)`, EINTR
            // is handled above), which is exactly why it must not be left to
            // whichever errno a future platform invents.
            Err(_) => {
                let _ = killer.kill();
                break;
            }
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

/// What a keystroke is, on the wire the child reads.
///
/// **A codec, not a router, and the signature is the argument.** It is handed a
/// keystroke and can return bytes; it holds no store, returns no `Command` and
/// reaches no session, so there is no key it could bind to anything the desk
/// does. That is why it has no section in `input::KEYMAP` while the router that
/// calls it does: the overlay lists what the *desk* claims, and this claims
/// nothing — it spells, for a child, what the operator's terminal spelled for
/// this process. A key added here can only ever become bytes.
///
/// It lives beside the child rather than in `ui/` for the reason the spawn
/// does: the monitoring artifact contains no forwarded keystroke, and absence
/// is a property only a gate can hold.
///
/// **`None` is not a lost keystroke.** It is a key with no wire form at all — a
/// media key, a bare modifier, a variant a later crossterm invents — and a
/// child would make no more of an invented sequence than of nothing. The lost
/// keystroke is the other case, and the session says that one out loud.
///
/// The sequences are xterm's, in the *normal* cursor-key mode. A child that
/// switched its keypad into application mode would want the `SS3` forms for the
/// arrows; nothing on this desk reads that mode back off the parser, and the
/// CSI forms are what every library in reach accepts.
pub fn encode(key: KeyEvent) -> Option<Vec<u8>> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    // The cursor and editing keys, which carry their modifiers *inside* the
    // sequence and are therefore returned whole. Falling through to the escape
    // prefix below would send Alt twice — once as a parameter and once as a
    // leading escape — which a child reads as Esc and then an unmodified key.
    let csi = match key.code {
        KeyCode::Delete => Some(b"\x1b[3~".as_slice()),
        KeyCode::Insert => Some(b"\x1b[2~".as_slice()),
        KeyCode::Up => Some(b"\x1b[A".as_slice()),
        KeyCode::Down => Some(b"\x1b[B".as_slice()),
        KeyCode::Right => Some(b"\x1b[C".as_slice()),
        KeyCode::Left => Some(b"\x1b[D".as_slice()),
        KeyCode::Home => Some(b"\x1b[H".as_slice()),
        KeyCode::End => Some(b"\x1b[F".as_slice()),
        KeyCode::PageUp => Some(b"\x1b[5~".as_slice()),
        KeyCode::PageDown => Some(b"\x1b[6~".as_slice()),
        _ => None,
    };
    if let Some(plain) = csi {
        return Some(modified(plain, key));
    }
    let bytes = match key.code {
        KeyCode::Char(c) if ctrl => control_byte(c)?,
        KeyCode::Char(c) => c.to_string().into_bytes(),
        // CR, not LF. The terminal sends what the Return key sends, and what
        // that *means* is the child's line discipline to decide; a client
        // helpfully sending `\n` would be answering a question the pty owns.
        KeyCode::Enter => vec![b'\r'],
        KeyCode::Tab => vec![b'\t'],
        KeyCode::BackTab => b"\x1b[Z".to_vec(),
        KeyCode::Esc => vec![0x1b],
        // DEL, which is what the key labelled Backspace sends in every terminal
        // this desk runs in. BS (0x08) is Ctrl-H, and a different key.
        KeyCode::Backspace => vec![0x7f],
        KeyCode::F(n) => function_key(n)?,
        _ => return None,
    };
    // Alt is a prefixed escape in every terminal this client runs in, over a
    // control byte exactly as over a letter: xterm's `metaSendsEscape` puts the
    // ESC in front of whatever the key produced, and `readline`'s default
    // keymap is built on that — `\e\C-h`, `\e\C-e`, `\e\C-y`. Treating a
    // control byte as though it already carried the modifier drops the ALT
    // silently and makes the whole Ctrl-Alt family a dead key in the pane.
    match key.modifiers.contains(KeyModifiers::ALT) {
        true => Some([&[0x1b][..], &bytes].concat()),
        false => Some(bytes),
    }
}

/// The control character a key carries when Ctrl is held.
///
/// The C0 range is the top three bits cleared: `?` through `_` map onto 0x1f
/// down to 0x00, and a lowercase letter is the same physical key as its
/// capital. Ctrl-? is DEL, which is outside that arithmetic and is the one
/// special case.
///
/// **`4` through `7` are not digits here.** crossterm reports the top of the C0
/// range by its own legacy names — 0x1C..0x1F arrive spelled `4`, `5`, `6`, `7`
/// with control held (`event/sys/unix/parse.rs`), which is the same table that
/// makes Ctrl-] arrive as `5`. So these four arms are Ctrl-\, Ctrl-], Ctrl-^
/// and Ctrl-_, and the arithmetic above cannot reach them because the character
/// the arithmetic would see is a digit. Dropping them costs a child that
/// ignores SIGINT its quit signal and readline its undo, which is the
/// difference between a terminal and a keyboard that mostly works. (`5` never
/// arrives here: the router intercepts it as the key that returns the
/// keyboard, which is that byte's other name.)
///
/// Anything else — a real digit, a comma — has no control form, and nothing is
/// sent rather than a byte the operator did not type.
fn control_byte(c: char) -> Option<Vec<u8>> {
    match c {
        '?' => Some(vec![0x7f]),
        ' ' => Some(vec![0x00]),
        '4'..='7' => Some(vec![0x1c + (c as u8 - b'4')]),
        c if c.is_ascii() => {
            let upper = c.to_ascii_uppercase() as u8;
            (0x40..=0x5f).contains(&upper).then(|| vec![upper & 0x1f])
        }
        _ => None,
    }
}

/// A cursor or editing key, with whatever modifiers it was pressed under.
///
/// xterm's encoding: a bitfield offset by one — shift 1, alt 2, control 4 —
/// spliced in as the second parameter. An unmodified key keeps its short form,
/// because that is what an unmodified key sends and a parameter of 1 is a
/// different string for a reader that compares them literally.
fn modified(plain: &[u8], key: KeyEvent) -> Vec<u8> {
    let mut bits = 0u8;
    for (held, bit) in [
        (KeyModifiers::SHIFT, 1),
        (KeyModifiers::ALT, 2),
        (KeyModifiers::CONTROL, 4),
    ] {
        if key.modifiers.contains(held) {
            bits |= bit;
        }
    }
    if bits == 0 {
        return plain.to_vec();
    }
    let param = bits + 1;
    let body = String::from_utf8_lossy(&plain[2..plain.len() - 1]).into_owned();
    match plain[plain.len() - 1] {
        // `\x1b[5~` already carries a parameter and gains a second.
        b'~' => format!("\x1b[{body};{param}~").into_bytes(),
        // `\x1b[A` carries none, and the first is the implicit 1.
        tail => format!("\x1b[{body}1;{param}{}", tail as char).into_bytes(),
    }
}

/// The function keys, in the two families a terminal spells them with.
///
/// F1–F4 are `SS3`; the rest are `CSI … ~`, with the gaps at 16 and 22 that the
/// historical VT keyboards left behind. Past F12 there is no agreed sequence,
/// so nothing is sent rather than something invented.
fn function_key(n: u8) -> Option<Vec<u8>> {
    let sequence = match n {
        1..=4 => format!("\x1bO{}", (b'P' + n - 1) as char),
        5 => "\x1b[15~".to_string(),
        6..=9 => format!("\x1b[{}~", 11 + u16::from(n)),
        10 => "\x1b[21~".to_string(),
        11 => "\x1b[23~".to_string(),
        12 => "\x1b[24~".to_string(),
        _ => return None,
    };
    Some(sequence.into_bytes())
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
        // starts is decided by this value. HOME and QLAB_UI_PORT decide which
        // owner the pane talks to and where the child's own config is read
        // from, so each is compared against what this process actually holds
        // rather than merely asserted present.
        assert_eq!(
            stated.get("PATH").map(|path| path.to_string()),
            std::env::var("PATH").ok().filter(|path| !path.is_empty())
        );
        assert_eq!(
            stated.get("HOME").map(|home| home.to_string()),
            std::env::var("HOME").ok().filter(|home| !home.is_empty())
        );
        assert_eq!(
            stated.get("QLAB_UI_PORT").map(|port| port.to_string()),
            std::env::var("QLAB_UI_PORT")
                .ok()
                .filter(|port| !port.trim().is_empty())
        );
        // And the child runs where the desk does, not in $HOME.
        assert_eq!(
            command.get_cwd().map(PathBuf::from),
            std::env::current_dir().ok()
        );
    }

    #[test]
    fn the_five_are_the_five_whatever_the_process_environment_said() {
        // The wiring test above compares against `std::env`, which makes its
        // QLAB_UI_PORT arm vacuous on a machine that does not set one — and a
        // vacuous pin would let the forwarding be deleted without a suite
        // noticing, leaving the pane talking to 8765 on a desk running
        // somewhere else. This one states both environments outright.
        assert_eq!(
            stated_env(
                "/opt/qlab/bin/qlab",
                Some("/bin".to_string()),
                Some("/home/desk".to_string()),
                Some("9931".to_string()),
            ),
            vec![
                ("PATH".to_string(), "/bin".to_string()),
                ("HOME".to_string(), "/home/desk".to_string()),
                ("TERM".to_string(), TERM.to_string()),
                ("QLAB_UI_PORT".to_string(), "9931".to_string()),
                ("QLAB_BIN".to_string(), "/opt/qlab/bin/qlab".to_string()),
            ]
        );
        // Blank is not a value. A port of `"  "` forwarded as itself would pin
        // the child to a number nobody wrote, and an empty PATH would override
        // the inherited one with a list containing nowhere to look.
        assert_eq!(
            stated_env("qlab", None, Some(String::new()), Some("  ".to_string())),
            vec![
                ("TERM".to_string(), TERM.to_string()),
                ("QLAB_BIN".to_string(), "qlab".to_string()),
            ]
        );
    }

    /// What was done to the child, in the order it was done.
    #[derive(Debug, Clone, Default)]
    struct Calls(Arc<Mutex<Vec<&'static str>>>);

    impl Calls {
        fn saw(&self, what: &'static str) {
            self.0
                .lock()
                .expect("the recorder outlives its writers")
                .push(what);
        }

        fn seen(&self) -> Vec<&'static str> {
            self.0
                .lock()
                .expect("the recorder outlives its writers")
                .clone()
        }
    }

    #[derive(Debug)]
    struct FakeKiller(Calls);

    impl ChildKiller for FakeKiller {
        fn kill(&mut self) -> std::io::Result<()> {
            self.0.saw("kill");
            Ok(())
        }

        fn clone_killer(&self) -> Box<dyn ChildKiller + Send + Sync> {
            Box::new(FakeKiller(self.0.clone()))
        }
    }

    #[derive(Debug)]
    struct FakeChild(Calls);

    impl ChildKiller for FakeChild {
        fn kill(&mut self) -> std::io::Result<()> {
            self.0.saw("kill");
            Ok(())
        }

        fn clone_killer(&self) -> Box<dyn ChildKiller + Send + Sync> {
            Box::new(FakeKiller(self.0.clone()))
        }
    }

    impl portable_pty::Child for FakeChild {
        fn try_wait(&mut self) -> std::io::Result<Option<ExitStatus>> {
            Ok(None)
        }

        /// Returns rather than blocking, deliberately: a real one would block
        /// forever here, and a test that hung would be reporting the bug as a
        /// timeout instead of as an order.
        fn wait(&mut self) -> std::io::Result<ExitStatus> {
            self.0.saw("wait");
            Ok(ExitStatus::with_exit_code(0))
        }

        fn process_id(&self) -> Option<u32> {
            None
        }
    }

    /// A master that only ever fails — the one thing a real pty will not do on
    /// demand.
    struct Broken;

    impl Read for Broken {
        fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
            Err(std::io::Error::other("the master went away"))
        }
    }

    #[test]
    fn a_read_that_fails_kills_the_child_before_it_waits_for_it() {
        // The one arm no scripted child can reach: EIO arrives as `Ok(0)` and
        // EINTR is handled, so nothing a test may spawn produces a read error
        // here. It still has to be right, and the cost of being wrong is the
        // worst failure this module has — from the moment the loop is left
        // nothing drains the master, and BSD `ttyclose` makes a child block
        // until the tty's output queue empties, so a `wait` on a live child
        // parks this thread forever, `ended` never becomes true, and the desk
        // shows `Running` for a session that has stopped.
        //
        // Pinned as an *order* against a fake, which is how `cli_handoff.rs`
        // pins the other hand-off and for the same reason.
        let calls = Calls::default();
        let (events, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let ended = AtomicBool::new(false);

        watch(
            Box::new(Broken),
            Box::new(FakeChild(calls.clone())),
            Box::new(FakeKiller(calls.clone())),
            "sh",
            &ended,
            &events,
        );

        assert_eq!(
            calls.seen(),
            ["kill", "wait"],
            "the child is stopped before it is waited for"
        );
        assert!(ended.load(Ordering::SeqCst), "the session knows it is over");
        assert!(
            matches!(rx.try_recv(), Ok(PtyEvent::Exited { status: 0, .. })),
            "and says so exactly once"
        );
        assert!(rx.try_recv().is_err(), "exactly once");
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

    // -- the codec ----------------------------------------------------------
    //
    // Exact bytes, because "the child received something" is not the property:
    // an arrow that arrives as the wrong sequence moves a cursor the operator
    // did not mean to move, and no assertion about *reaching* the child would
    // notice. The sequences are xterm's, which is what every terminal library a
    // `qlab cli` session might use was written against.

    fn bytes(code: KeyCode) -> Vec<u8> {
        encode(KeyEvent::new(code, KeyModifiers::NONE)).expect("a key with a wire form")
    }

    fn with(code: KeyCode, mods: KeyModifiers) -> Vec<u8> {
        encode(KeyEvent::new(code, mods)).expect("a key with a wire form")
    }

    #[test]
    fn a_printable_key_is_its_own_utf8_and_nothing_more() {
        assert_eq!(bytes(KeyCode::Char('q')), b"q");
        assert_eq!(bytes(KeyCode::Char('/')), b"/");
        assert_eq!(bytes(KeyCode::Char('3')), b"3");
        // A question is typed in whatever language it is asked in, and a client
        // that sent one byte per `char` would corrupt every one of them.
        assert_eq!(bytes(KeyCode::Char('é')), "é".as_bytes());
    }

    #[test]
    fn ctrl_c_is_the_interrupt_byte_and_the_c0_range_is_arithmetic() {
        // The byte the whole focus ruling is about: with ISIG set on the pty,
        // this is what makes the child's own line discipline raise SIGINT.
        assert_eq!(with(KeyCode::Char('c'), KeyModifiers::CONTROL), [0x03]);
        // The same key by its capital, and the ends of the range.
        assert_eq!(with(KeyCode::Char('C'), KeyModifiers::CONTROL), [0x03]);
        assert_eq!(with(KeyCode::Char('d'), KeyModifiers::CONTROL), [0x04]);
        assert_eq!(with(KeyCode::Char('@'), KeyModifiers::CONTROL), [0x00]);
        assert_eq!(with(KeyCode::Char('_'), KeyModifiers::CONTROL), [0x1f]);
        assert_eq!(with(KeyCode::Char(' '), KeyModifiers::CONTROL), [0x00]);
        assert_eq!(with(KeyCode::Char('?'), KeyModifiers::CONTROL), [0x7f]);
        // The four digits crossterm's legacy table actually delivers, which are
        // not digits an operator typed at all: 0x1C..0x1F arrive spelled `4`
        // through `7`, so these are Ctrl-\\, Ctrl-], Ctrl-^ and Ctrl-_. The
        // first is the quit signal a child ignoring SIGINT is stopped with, and
        // the last is readline's undo — dropping them is the difference between
        // a terminal and a keyboard that mostly works.
        assert_eq!(with(KeyCode::Char('4'), KeyModifiers::CONTROL), [0x1c]);
        assert_eq!(with(KeyCode::Char('5'), KeyModifiers::CONTROL), [0x1d]);
        assert_eq!(with(KeyCode::Char('6'), KeyModifiers::CONTROL), [0x1e]);
        assert_eq!(with(KeyCode::Char('7'), KeyModifiers::CONTROL), [0x1f]);
        // And a key with no control form sends nothing rather than a byte the
        // operator did not type. A digit OUTSIDE that window, deliberately:
        // the first version of this test asserted on `3`, the one digit near
        // the window that no terminal spells this way, so it passed while the
        // four that do arrive were being dropped.
        assert!(encode(KeyEvent::new(KeyCode::Char('8'), KeyModifiers::CONTROL)).is_none());
        assert!(encode(KeyEvent::new(KeyCode::Char(','), KeyModifiers::CONTROL)).is_none());
    }

    #[test]
    fn the_keys_a_session_is_actually_driven_with_carry_their_own_sequences() {
        assert_eq!(bytes(KeyCode::Enter), b"\r");
        assert_eq!(bytes(KeyCode::Tab), b"\t");
        assert_eq!(bytes(KeyCode::BackTab), b"\x1b[Z");
        assert_eq!(bytes(KeyCode::Esc), b"\x1b");
        assert_eq!(bytes(KeyCode::Backspace), [0x7f]);
        assert_eq!(bytes(KeyCode::Up), b"\x1b[A");
        assert_eq!(bytes(KeyCode::Down), b"\x1b[B");
        assert_eq!(bytes(KeyCode::Right), b"\x1b[C");
        assert_eq!(bytes(KeyCode::Left), b"\x1b[D");
        assert_eq!(bytes(KeyCode::Home), b"\x1b[H");
        assert_eq!(bytes(KeyCode::End), b"\x1b[F");
        assert_eq!(bytes(KeyCode::PageUp), b"\x1b[5~");
        assert_eq!(bytes(KeyCode::PageDown), b"\x1b[6~");
        assert_eq!(bytes(KeyCode::Delete), b"\x1b[3~");
        assert_eq!(bytes(KeyCode::Insert), b"\x1b[2~");
        assert_eq!(bytes(KeyCode::F(1)), b"\x1bOP");
        assert_eq!(bytes(KeyCode::F(4)), b"\x1bOS");
        assert_eq!(bytes(KeyCode::F(5)), b"\x1b[15~");
        assert_eq!(bytes(KeyCode::F(6)), b"\x1b[17~");
        assert_eq!(bytes(KeyCode::F(12)), b"\x1b[24~");
        // Past F12 there is no sequence anyone agrees on.
        assert!(encode(KeyEvent::new(KeyCode::F(13), KeyModifiers::NONE)).is_none());
    }

    #[test]
    fn a_modified_cursor_key_splices_its_modifier_in_rather_than_dropping_it() {
        // Word-wise movement is Alt or Ctrl and an arrow in every line editor a
        // session might present; a client that forwarded the plain form would
        // move by one character and look like a bug in the child.
        assert_eq!(with(KeyCode::Left, KeyModifiers::CONTROL), b"\x1b[1;5D");
        assert_eq!(with(KeyCode::Right, KeyModifiers::ALT), b"\x1b[1;3C");
        assert_eq!(with(KeyCode::Up, KeyModifiers::SHIFT), b"\x1b[1;2A");
        assert_eq!(with(KeyCode::Home, KeyModifiers::CONTROL), b"\x1b[1;5H");
        // The `~` family already carries a parameter and gains a second.
        assert_eq!(with(KeyCode::Delete, KeyModifiers::CONTROL), b"\x1b[3;5~");
        assert_eq!(with(KeyCode::PageUp, KeyModifiers::SHIFT), b"\x1b[5;2~");
    }

    #[test]
    fn alt_is_a_prefixed_escape_over_every_key_a_control_byte_included() {
        assert_eq!(with(KeyCode::Char('b'), KeyModifiers::ALT), b"\x1bb");
        assert_eq!(with(KeyCode::Enter, KeyModifiers::ALT), b"\x1b\r");
        // Ctrl-Alt-C is the interrupt byte with an escape in front of it, not
        // an escape in front of a `c` — and not the bare byte either, which is
        // what an encoder that treated a control byte as "already modified"
        // would send. `readline`'s own keymap is full of these (`\e\C-h`,
        // `\e\C-e`, `\e\C-y`), so the whole family is a dead key without it.
        assert_eq!(
            with(
                KeyCode::Char('c'),
                KeyModifiers::ALT | KeyModifiers::CONTROL
            ),
            b"\x1b\x03"
        );
        assert_eq!(
            with(
                KeyCode::Char('h'),
                KeyModifiers::ALT | KeyModifiers::CONTROL
            ),
            b"\x1b\x08"
        );
    }

    #[test]
    fn a_key_with_no_wire_form_is_not_sent() {
        // Not a lost keystroke — a key a child would make nothing of. The
        // alternative is inventing a sequence, which puts bytes into someone's
        // session that no terminal would ever have produced.
        assert!(encode(KeyEvent::new(KeyCode::Menu, KeyModifiers::NONE)).is_none());
        assert!(encode(KeyEvent::new(KeyCode::CapsLock, KeyModifiers::NONE)).is_none());
    }
}
