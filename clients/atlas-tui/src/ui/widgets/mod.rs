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
pub mod ticker;
pub mod toast;
pub mod tristate_spark;

pub use panel_header::{header_keys, panel_block, panel_header};
pub use refusal::refuse;
