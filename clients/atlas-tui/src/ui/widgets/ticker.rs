//! The scrolling ticker row — the desk's pulse, and one of the three "alive" indicators.
//!
//! Two properties carry the whole widget. It **tiles**: the row is an endless
//! tape, so a universe narrower than the terminal repeats seamlessly instead of
//! leaving half a row of dead space that reads as a feed that stopped. And it
//! rotates by **display cell, never by byte** — `▲` is three bytes and one cell,
//! and a byte offset would split it into replacement characters on two frames
//! out of every three.
//!
//! Prices come from `Store::asset_view`, never from `market.assets`: a row that
//! read the snapshot directly would render the poll's price and silently lose
//! every quote that arrived since it.

use crate::format::{self, MISSING};
use crate::fx::{FlashKey, FlashTracker};
use crate::store::AssetView;
use crate::theme::theme;
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Paragraph},
    Frame,
};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthChar;

/// Between triplets. Three cells: two read as a wide word gap inside one
/// quote, four start to read as two separate rows of content.
const GAP: &str = "   ";

/// The left gutter, so the first symbol does not sit flush against the frame
/// edge. Content scrolls under it rather than through it — a pad inside the tape
/// would appear at every wrap point and break the tiling.
const GUTTER: u16 = 1;

/// Draw the tape at `offset` cells, into `area`.
///
/// `now` and `offset` are both data: the offset is the tick count from the
/// store, so the row's position is a pure function of state and a golden frame
/// can pin it. `stale_after` is data for the same reason it is on the `Store` —
/// it is the poller's cadence, not the renderer's opinion.
pub fn draw(
    f: &mut Frame,
    area: Rect,
    views: &[AssetView],
    offset: usize,
    stale_after: Duration,
    fx: &FlashTracker,
    now: Instant,
) {
    let t = theme();
    f.render_widget(
        Block::default().style(Style::default().bg(t.bg_raised)),
        area,
    );
    let inner = Rect {
        x: area.x + GUTTER.min(area.width),
        width: area.width.saturating_sub(GUTTER),
        ..area
    };
    if inner.width == 0 {
        return;
    }

    let cells = cells(&tape(views, stale_after, fx, now));
    if cells.is_empty() {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "no market assets in the last snapshot",
                Style::default().fg(t.text_tertiary),
            ))),
            inner,
        );
        return;
    }
    f.render_widget(
        Paragraph::new(Line::from(window(&cells, offset, inner.width as usize))),
        inner,
    );
}

/// The endless tape, one pass: `SYM price ▲x.xx%` and a gap, per asset.
///
/// The gap trails every triplet including the last, which is what makes the
/// wrap point look like every other gap on the row.
pub fn tape(
    views: &[AssetView],
    stale_after: Duration,
    fx: &FlashTracker,
    now: Instant,
) -> Vec<Span<'static>> {
    let t = theme();
    let mut spans = Vec::with_capacity(views.len() * 4);
    for view in views {
        // Stale prices lose their colour along with their claim to be current.
        // A green tick that is four minutes old is a statement about the tape
        // that nobody made.
        //
        // Per cell, against the feed that fed it: a live stream and a dead
        // poller is a tape of prices current to the second, and dimming those
        // because the aggregate snapshot went quiet is the same lie told the
        // other way round. `AssetView::stale` is the one rule for it.
        let stale = view.stale(stale_after, now);
        let symbol = if stale { t.text_tertiary } else { t.cyan };
        let value = if stale {
            t.text_tertiary
        } else {
            t.text_primary
        };
        let tone = if stale {
            t.text_tertiary
        } else {
            view.change_1d
                .map(|c| t.change(c))
                .unwrap_or(t.text_secondary)
        };

        spans.push(Span::styled(
            format!("{} ", view.ticker),
            Style::default().fg(symbol).add_modifier(Modifier::BOLD),
        ));
        let price = Style::default().fg(value);
        spans.push(Span::styled(
            view.price
                .map(format::price)
                .unwrap_or_else(|| MISSING.into()),
            fx.style_for(&FlashKey::price(view.ticker), now, price),
        ));
        spans.push(Span::styled(
            format!(
                " {}{GAP}",
                view.change_1d
                    .map(format::arrow_pct)
                    .unwrap_or_else(|| MISSING.into())
            ),
            Style::default().fg(tone),
        ));
    }
    spans
}

/// One display cell of the tape.
///
/// The tape is addressed in cells, never in bytes or in chars: `▲` is three
/// bytes and one cell, and a CJK symbol is one char and two cells. A
/// double-width glyph occupies two entries — the second carries no text, so a
/// window that opens on it renders a space instead of half a glyph.
#[derive(Debug, Clone, Copy)]
struct TapeCell {
    ch: Option<char>,
    style: Style,
}

fn cells(spans: &[Span<'static>]) -> Vec<TapeCell> {
    let mut out = Vec::new();
    for span in spans {
        for ch in span.content.chars() {
            // Zero-width joiners and combining marks occupy no cell, so they
            // cannot be rotated to and are dropped rather than silently
            // attaching to whichever glyph the window happens to start on.
            let width = ch.width().unwrap_or(0);
            if width == 0 {
                continue;
            }
            out.push(TapeCell {
                ch: Some(ch),
                style: span.style,
            });
            out.extend((1..width).map(|_| TapeCell {
                ch: None,
                style: span.style,
            }));
        }
    }
    out
}

/// `width` cells of the endless tape, starting `start` cells in.
///
/// The tape repeats, so this is the tiling: a universe narrower than the
/// terminal wraps into itself with the same three-space gap at the seam as
/// everywhere else, and `start` may be any tick count without a modulo at the
/// call site.
fn window(cells: &[TapeCell], start: usize, width: usize) -> Vec<Span<'static>> {
    // Reduced before anything is added to it: `start` is a free-running tick
    // count, and the sum below is the one place it could overflow.
    let start = start % cells.len();
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut text = String::new();
    let mut style: Option<Style> = None;
    let mut push = |ch: char, cell_style: Style, text: &mut String, spans: &mut Vec<Span>| {
        if style != Some(cell_style) {
            if !text.is_empty() {
                spans.push(Span::styled(
                    std::mem::take(text),
                    style.unwrap_or_default(),
                ));
            }
            style = Some(cell_style);
        }
        text.push(ch);
    };

    let mut at = 0;
    while at < width {
        let cell = cells[(start + at) % cells.len()];
        match cell.ch {
            // The second half of a glyph the window opened in the middle of, or
            // one whose other half falls past the right edge. Either way the
            // glyph cannot be drawn where it belongs, so the cell stays blank.
            None => {
                push(' ', cell.style, &mut text, &mut spans);
                at += 1;
            }
            Some(ch) => {
                let w = ch.width().unwrap_or(1).max(1);
                if at + w > width {
                    push(' ', cell.style, &mut text, &mut spans);
                    at += 1;
                } else {
                    push(ch, cell.style, &mut text, &mut spans);
                    at += w;
                }
            }
        }
    }
    if !text.is_empty() {
        spans.push(Span::styled(text, style.unwrap_or_default()));
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    use unicode_width::UnicodeWidthStr;

    /// How long a price may go unrefreshed in these tests. Every fixture view
    /// carries `at: None` — nothing has told them when they arrived — so none of
    /// them is stale, which is the state these layout pins are about.
    const FRESH: Duration = Duration::from_secs(10);

    fn views() -> Vec<AssetView<'static>> {
        vec![
            AssetView {
                ticker: "SPY",
                price: Some(729.46),
                change_1d: Some(-0.0154),
                at: None,
            },
            AssetView {
                ticker: "QQQ",
                price: Some(661.73),
                change_1d: Some(0.0204),
                at: None,
            },
        ]
    }

    /// One rendered row, read back as text.
    fn row(views: &[AssetView], offset: usize, w: u16) -> String {
        let fx = FlashTracker::default();
        let now = Instant::now();
        let mut term = Terminal::new(TestBackend::new(w, 1)).unwrap();
        term.draw(|f| draw(f, Rect::new(0, 0, w, 1), views, offset, FRESH, &fx, now))
            .unwrap();
        let buf = term.backend().buffer().clone();
        let mut out = String::new();
        let mut x = 0;
        while x < w {
            let symbol = buf[(x, 0)].symbol();
            out.push_str(symbol);
            // A double-width glyph owns the cell after it, and the backend
            // leaves that cell holding the row's background. Reading it back
            // would count the same two cells three times wide.
            x += symbol.width().max(1) as u16;
        }
        out
    }

    #[test]
    fn the_tape_is_triplets_separated_by_three_spaces() {
        let text: String = tape(&views(), FRESH, &FlashTracker::default(), Instant::now())
            .iter()
            .map(|s| s.content.as_ref())
            .collect();
        assert_eq!(text, "SPY 729.46 ▼1.54%   QQQ 661.73 ▲2.04%   ");
    }

    #[test]
    fn a_universe_narrower_than_the_terminal_tiles_instead_of_running_out() {
        // A half-empty row reads as a feed that stopped. Forty cells is under
        // the two triplets' own width, so this also pins the truncation.
        let narrow = row(&views(), 0, 40);
        assert_eq!(narrow, " SPY 729.46 ▼1.54%   QQQ 661.73 ▲2.04%  ");

        let wide = row(&views(), 0, 90);
        assert_eq!(
            wide,
            " SPY 729.46 ▼1.54%   QQQ 661.73 ▲2.04%   \
             SPY 729.46 ▼1.54%   QQQ 661.73 ▲2.04%   SPY 729.4"
        );
    }

    #[test]
    fn the_tape_rotates_one_cell_at_a_time_and_wraps_seamlessly() {
        let at = |offset| row(&views(), offset, 40);
        assert!(at(0).starts_with(" SPY 729.46"));
        assert!(at(1).starts_with(" PY 729.46 "));
        assert!(at(5).starts_with(" 29.46 "));

        // The tape is 40 cells wide, so a full lap returns to where it started —
        // the property that makes "seamless" testable rather than a claim.
        let period = tape(&views(), FRESH, &FlashTracker::default(), Instant::now())
            .iter()
            .map(|s| s.content.width())
            .sum::<usize>();
        assert_eq!(period, 40);
        assert_eq!(at(0), at(period));
        assert_eq!(at(3), at(period + 3));
        assert_eq!(at(period - 1), at(2 * period - 1));
    }

    #[test]
    fn rotation_counts_display_cells_not_bytes() {
        // `▼` is three bytes and one cell. A byte offset would split it into
        // replacement characters on two frames out of every three, and would
        // put the wrap point in a different place than the eye expects.
        for offset in 0..40 {
            let rendered = row(&views(), offset, 40);
            assert!(
                !rendered.contains('\u{fffd}'),
                "offset {offset} split a glyph: {rendered:?}"
            );
            assert_eq!(
                rendered.width(),
                40,
                "offset {offset} rendered {rendered:?}"
            );
        }
        // The arrow arrives whole, one cell later each frame.
        assert_eq!(row(&views(), 11, 8), " ▼1.54% ");
        assert_eq!(row(&views(), 12, 8), " 1.54%  ");
    }

    #[test]
    fn a_double_width_glyph_is_never_cut_in_half() {
        // Nothing in the tape is double-width today, so this pins the window
        // itself: a universe whose symbols are CJK must render a space at the
        // seam rather than half a glyph.
        let wide = vec![AssetView {
            ticker: "日経",
            price: Some(39000.0),
            change_1d: Some(0.01),
            at: None,
        }];
        for offset in 0..24 {
            let rendered = row(&wide, offset, 12);
            assert_eq!(rendered.width(), 12, "offset {offset}: {rendered:?}");
        }
    }

    #[test]
    fn a_window_is_exactly_the_cells_it_was_asked_for() {
        // The contract the rendered row cannot state: ratatui truncates an
        // over-wide line, so a window that hands back one cell too many looks
        // right and is wrong — until the day something else shares the row.
        let tape = cells(&[Span::raw("日経x")]);
        assert_eq!(tape.len(), 5, "two double-width glyphs and one single");
        for start in 0..tape.len() * 2 {
            for width in 1..8 {
                let rendered: String = window(&tape, start, width)
                    .iter()
                    .map(|s| s.content.as_ref())
                    .collect();
                assert_eq!(
                    rendered.width(),
                    width,
                    "start {start} width {width}: {rendered:?}"
                );
            }
        }
    }

    #[test]
    fn an_empty_universe_says_so_rather_than_scrolling_nothing() {
        let rendered = row(&[], 0, 60);
        assert!(rendered.contains("no market assets"), "{rendered:?}");
    }

    #[test]
    fn a_one_cell_row_renders_without_panicking() {
        for w in [1u16, 2, 3] {
            let rendered = row(&views(), 7, w);
            assert_eq!(rendered.width(), w as usize);
        }
    }
}
