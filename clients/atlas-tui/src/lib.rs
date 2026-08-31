//! atlas — the workstation's library half.
//!
//! The binary is a thin shell over this crate so integration tests can decode
//! owner payloads and render frames without a terminal. Modules are declared
//! here and nowhere else: declaring them again in `main.rs` would compile a
//! second copy of every module, and `theme`'s `OnceLock` would then exist twice
//! — one initialised, one not, depending on which copy the caller reached.

pub mod bus;
pub mod clipboard;
pub mod cmd;
/// The seam between a confirmed `Command` and a request. Here rather than in
/// `main.rs` because the binary has no test harness, and what reaches the owner
/// is the last thing in this crate that should be untestable.
pub mod dispatch;
pub mod format;
pub mod fx;
pub mod glyph;
/// Giving the terminal away to the real Claude CLI and taking it back. Here
/// rather than in `main.rs` for the same reason `dispatch` is: the order of a
/// screen hand-off has to be right, and the binary has no test harness.
pub mod handoff;
pub mod input;
pub mod model;
pub mod net;
/// A child of this process on a pseudoterminal, so the ATLAS tab can hold a
/// real `qlab cli` beside the desk rather than instead of it. Gated with the
/// commands that can reach one: the monitoring build has no key that opens a
/// pane, and the design's claim is that the artifact *contains* no spawn — a
/// property only absence can hold.
#[cfg(feature = "operator")]
pub mod pty;
/// A credential an operator typed, carried without being printable. Gated with
/// the only path that can produce or spend one: the login form and the POST it
/// feeds both exist under `operator`, so the default build has no such value to
/// hold and nothing that could construct one.
#[cfg(feature = "operator")]
pub mod secret;
pub mod store;
pub mod theme;
pub mod ui;
