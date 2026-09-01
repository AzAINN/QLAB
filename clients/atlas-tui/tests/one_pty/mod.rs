//! One pseudoterminal at a time, for every test that opens a real one.
//!
//! At full parallelism this suite exhausts something in the platform's pty
//! allocation: `cargo test` with no `--test-threads` fails a *different* test on
//! every run with `could not open a terminal for \`sh -c read x\`: failed to
//! openpty: Os { code: -6 }`, which is not an errno any of this code produces
//! and says nothing about the code under test. It reproduces on an unmodified
//! tree — three runs in four — and the standing workaround was
//! `--test-threads=4`, which CI does not pass and which would slow every test in
//! the crate that has nothing to do with a child. A turn taken here fixes the
//! contention where it is instead of hiding it behind a flag.
//!
//! **The turn is held for the whole test, not just the open.** What contends is
//! a live pty and the process on the other end of it, so a lock released once
//! `open` returned would let the sessions pile up exactly as before.
//!
//! **One mechanism, one lock per test binary.** This file is compiled into each
//! test crate that declares `mod one_pty;`, so each gets its own `static` — and
//! that is the right scope, because a test binary is a process and cargo runs
//! test binaries one at a time. Tests inside a binary are what run in parallel,
//! and they are what this serialises. A cargo that ran two binaries at once
//! would need a lock the OS shares rather than one a process holds; nothing in
//! this suite asks for one.
//!
//! **Held across `.await`, deliberately.** `clippy::await_holding_lock` fires on
//! every async test that takes a turn, and the hazard it names is not present
//! here: `#[tokio::test]` builds a current-thread runtime with exactly one task
//! on it, so there is no second task whose progress a held guard could be
//! blocking. What is being blocked is the other *test threads*, which is the
//! whole point. The three files with async pty tests state that and allow the
//! lint; the alternative — an async-aware mutex — would hand the sync tests in
//! `pty_session.rs` a `blocking_lock` and make one mechanism into two doors.
//!
//! Not a `tests/*.rs` target: files under a subdirectory are compiled into the
//! crate that declares them rather than becoming a test binary of their own,
//! which is what `harness/` relies on too.

use std::sync::{Mutex, MutexGuard, PoisonError};

/// The turn itself. Guards no data — only the right to hold a pty — which is
/// why the `()` inside it is never read.
static PTY: Mutex<()> = Mutex::new(());

/// Wait for the pty, and hold it until the returned guard is dropped.
///
/// Poisoning is recovered from rather than unwrapped. The lock protects a turn
/// and not a value, so a test that panicked while holding it left nothing
/// inconsistent behind for the next one to find — and an `unwrap` here would
/// turn one real failure into a failure of every pty test that ran after it,
/// burying the one that actually broke under a cascade of poisoned locks.
pub fn turn() -> MutexGuard<'static, ()> {
    PTY.lock().unwrap_or_else(PoisonError::into_inner)
}
