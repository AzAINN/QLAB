//! A pane refusing to draw, and saying what it would take.
//!
//! One shape for every refusal on the workstation — no sectors prewarmed, a
//! grid under its floor, a ribbon whose cells have been starved by the split —
//! because they are one statement: what is missing, then the remedy. Silence, a
//! clipped pane and a half-drawn grid are the three renderings an operator
//! cannot tell from a working desk.
//!
//! Promoted out of `views/markets.rs` once BOOK grew refusals of its own: three
//! copies of "say what it would take" is three chances for one of them to stop
//! saying the remedy.

use crate::theme::theme;
use ratatui::{
    layout::Rect,
    style::Style,
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
    Frame,
};

/// Wrapped, since a pane too narrow to hold the numbers is also too narrow to
/// hold the sentence about them, and a remedy clipped to `qlab prewar` is one an
/// operator cannot run.
pub fn refuse(f: &mut Frame, area: Rect, message: String) {
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            message,
            Style::default().fg(theme().text_dim),
        )))
        .wrap(Wrap { trim: true }),
        area,
    );
}
