//! The panel vocabulary: one amber bar, one uppercase title, one dim rule.
//!
//! Every panel on the workstation is headed the same way, so the eye learns the
//! shape once. Boxing each pane in a full border instead would spend four cells
//! of every panel's width on chrome that carries no information — on a 120-cell
//! frame with three columns that is most of a data column.

use crate::theme::theme;
use ratatui::{
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders},
};
use unicode_width::UnicodeWidthStr;

/// `▌ TITLE` — amber bar, bold uppercase title.
///
/// The title is uppercased here rather than at every call site: a panel that
/// disagreed with its neighbours about casing would read as a different kind of
/// thing.
pub fn panel_header(title: &str) -> Line<'static> {
    let t = theme();
    Line::from(vec![
        Span::styled("▌", Style::default().fg(t.accent)),
        Span::styled(
            format!(" {}", title.to_uppercase()),
            Style::default()
                .fg(t.text_primary)
                .add_modifier(Modifier::BOLD),
        ),
    ])
}

/// The rule under a panel. Separate from the header because the caller owns the
/// area: the header is a line inside the block's inner rect, and the block is
/// what reserves the row the rule is drawn on.
pub fn panel_block() -> Block<'static> {
    Block::default()
        .borders(Borders::BOTTOM)
        .border_style(Style::default().fg(theme().border_dim))
}

/// A panel header with the keys that drive it pushed to the far side.
///
/// The keys go whole or not at all: a `Paragraph` clips from the right, and
/// `ALL 1Y 3` is a control an operator reads as broken rather than as absent.
///
/// Promoted out of `views/book.rs` when MARKETS grew its own key hints, by the
/// rule this module's parent states: two spellings of "the keys on the far
/// side" is two chances to disagree about when they are dropped.
pub fn header_keys(title: &str, keys: Vec<Span<'static>>, width: u16) -> Line<'static> {
    let title = panel_header(title);
    let title_w = title.width();
    let keys_w: usize = keys.iter().map(|s| s.content.width()).sum();
    let mut spans = title.spans;
    if let Some(gap) = (width as usize).checked_sub(title_w + keys_w) {
        spans.push(Span::raw(" ".repeat(gap)));
        spans.extend(keys);
    }
    Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{
        buffer::Buffer,
        layout::Rect,
        widgets::{Paragraph, Widget},
    };

    fn render(line: Line<'static>, w: u16) -> String {
        let area = Rect::new(0, 0, w, 1);
        let mut buf = Buffer::empty(area);
        Paragraph::new(line).render(area, &mut buf);
        (0..w).map(|x| buf[(x, 0)].symbol().to_string()).collect()
    }

    #[test]
    fn the_header_is_the_bar_a_space_and_the_uppercase_title() {
        // The assertion is on the rendered cells, not on the spans: the golden
        // frames pin `▌ PULSE`, and a header that only looked right in the
        // struct would fail there instead of here.
        assert_eq!(render(panel_header("pulse"), 10), "▌ PULSE   ");
        assert_eq!(render(panel_header("Atlas Read"), 14), "▌ ATLAS READ  ");
    }

    #[test]
    fn the_bar_is_the_accent_and_the_title_is_not() {
        let header = panel_header("book");
        assert_eq!(header.spans[0].style.fg, Some(theme().accent));
        assert_eq!(header.spans[1].style.fg, Some(theme().text_primary));
        assert!(header.spans[1].style.add_modifier.contains(Modifier::BOLD));
    }
}
