//! Widgets shared across views. A widget lives here only once a second view needs it.

pub mod braille_chart;
mod panel_header;
pub mod pulse;
pub mod ticker;

pub use panel_header::{panel_block, panel_header};
