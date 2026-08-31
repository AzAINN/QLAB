//! Widgets shared across views. A widget lives here only once a second view needs it.

pub mod braille_chart;
// The confirm modal and the token it mints. Gated with the writer it guards: a
// build with no `execute_plan` to reach has nothing for a `ConfirmToken` to
// unlock, and shipping the box that asks a human to authorise a fill into a
// binary that cannot place one would be a lie drawn on screen. The attribute and
// the declaration are pinned verbatim by `tests/operator_gate.rs`.
#[cfg(feature = "operator")]
pub mod confirm;
pub mod event_row;
pub mod heat_cell;
pub mod help;
pub mod md;
mod panel_header;
pub mod pipeline;
// The desk's current proposal, as one card. Ungated: a monitoring window shows
// the open question exactly as an armed one does, and what the gate removes is
// the word that opens the box and the box itself.
pub mod proposal;
pub mod pulse;
mod refusal;
pub mod table_cell;
// The child's screen, drawn on the desk. Gated with the module that obtains one:
// a monitoring build has no pty, so it has no `vt100::Screen` to hand this widget
// and no key that could open a pane to hold one. Ungated it would also link the
// parser and the terminal widget into the artifact whose manifest states that
// nothing in it references either — and a renderer with no reachable caller is
// the unreachable seam invariant 10 forbids. The attribute and the declaration
// are pinned verbatim by `tests/operator_gate.rs`.
#[cfg(feature = "operator")]
pub mod terminal;
pub mod ticker;
pub mod toast;
pub mod tristate_spark;

pub use panel_header::{header_keys, panel_block, panel_header};
pub use refusal::refuse;
