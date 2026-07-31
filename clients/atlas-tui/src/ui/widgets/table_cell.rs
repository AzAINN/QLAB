//! One table cell, held to its column so an overflow costs a digit and not a sign.
//!
//! Promoted out of `views/markets.rs` when BOOK's blotter became the second
//! grid. The rule it encodes is the one this client cannot afford to spell
//! twice: ratatui right-aligns an overlong line by dropping its *leading*
//! cells, and the leading cell of a number is its sign.

use ratatui::{
    style::Style,
    text::{Line, Span},
    widgets::Cell,
};

/// Whether a column's contents are pushed right. Named, because
/// `cell(text, style, true, 6)` at a call site says nothing about which end.
pub const LEFT: bool = false;
pub const RIGHT: bool = true;

/// One table cell, right-aligned unless it is a name or a picture, and held to
/// its column's width.
///
/// Right because a column of numbers only reads as a column when the decimal
/// points line up. Held because ratatui right-aligns an overlong line by
/// dropping its *leading* cells, and the leading cell of a number is its sign:
/// a `-10.1%` twenty-day change renders as `10.1%` in a five-wide column — a
/// loss drawn as a gain. Keeping the head instead costs the last digit, which
/// is a number that is coarse rather than one that is wrong.
pub fn cell(text: String, style: Style, right: bool, width: u16) -> Cell<'static> {
    let line = Line::from(Span::styled(head(text, width), style));
    Cell::from(if right {
        line.right_aligned()
    } else {
        line.left_aligned()
    })
}

/// The leading `width` characters of `text`, or all of it.
///
/// Characters rather than bytes, and every glyph these grids render is one cell
/// wide — the arrows, the eight spark levels and the trend rules included.
pub fn head(text: String, width: u16) -> String {
    match text.char_indices().nth(width as usize) {
        Some((byte, _)) => text[..byte].to_string(),
        None => text,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_column_too_narrow_for_its_number_loses_the_last_digit_and_not_the_sign() {
        // Ratatui right-aligns an overlong line by dropping its leading cells,
        // so `-10.1%` in a five-wide column renders `10.1%` — a loss drawn as a
        // gain. This is the half of the guard that does not depend on any
        // particular column being wide enough.
        assert_eq!(head("-10.1%".into(), 5), "-10.1");
        assert_eq!(head("▼ 12.34".into(), 6), "▼ 12.3");
        assert_eq!(head("6.3%".into(), 5), "6.3%");
        // Multi-byte glyphs are cut on character boundaries, not byte ones — a
        // byte slice through `▁▂▃` panics rather than truncating.
        assert_eq!(head("▁▂▃▄▅▆▇█".into(), 4), "▁▂▃▄");
    }

    #[test]
    fn a_held_cell_keeps_the_alignment_its_column_asked_for() {
        // The flag is the whole difference between a name and a number, and a
        // `Cell` cannot be read back — so this pins that both arms are wired.
        assert_ne!(
            cell("SPY".into(), Style::default(), LEFT, 6),
            cell("SPY".into(), Style::default(), RIGHT, 6)
        );
    }
}
