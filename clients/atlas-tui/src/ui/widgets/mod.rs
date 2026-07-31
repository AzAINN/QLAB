//! Widgets shared across views. A widget lives here only once a second view needs it.

pub mod braille_chart;
pub mod heat_cell;
mod panel_header;
pub mod pulse;
mod refusal;
pub mod table_cell;
pub mod ticker;
pub mod tristate_spark;

pub use panel_header::{panel_block, panel_header};
pub use refusal::refuse;
