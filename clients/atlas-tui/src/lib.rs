//! atlas — the workstation's library half.
//!
//! The binary is a thin shell over this crate so integration tests can decode
//! owner payloads and render frames without a terminal. Modules are declared
//! here and nowhere else: declaring them again in `main.rs` would compile a
//! second copy of every module, and `theme`'s `OnceLock` would then exist twice
//! — one initialised, one not, depending on which copy the caller reached.

pub mod bus;
pub mod client;
pub mod cmd;
pub mod format;
pub mod fx;
pub mod glyph;
pub mod input;
pub mod model;
pub mod net;
pub mod store;
pub mod theme;
pub mod ui;
