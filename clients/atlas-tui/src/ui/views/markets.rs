//! MARKETS — the universe as a grid, one asset as a chart, the sectors as a heat strip.
//!
//! Three panes answering three questions an operator asks in one glance: what is
//! everything doing, what is *this one* doing, and where is the money rotating.
//!
//! The two halves of a row come from two places on purpose. `Store::asset_facts`
//! carries what the poll knows — history, 20-day change, realized vol — and
//! `Store::asset_view` carries the two facts the quote stream can overtake. A
//! grid that read `market.assets` for the price would render the poll's number
//! and silently lose every quote that arrived since.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::{FlashKey, FlashTracker};
use crate::store::{AssetFacts, Store};
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::{braille_chart, panel_block, panel_header};
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Cell, Paragraph, Row, Table, TableState, Wrap},
    Frame,
};
use std::time::Instant;

/// The eight columns: title, the cells each needs at its widest rendering, and
/// whether its contents are pushed right.
///
/// `SYMBOL` is six because the header is: a column narrower than its own title
/// renders `SYMB`, and a truncated header is a column an operator has to guess.
/// `20D` is six because `format::pct1` spends one on the sign — a twenty-day
/// change is the one column here that is routinely negative and routinely
/// double-digit, and `-10.1%` does not fit in five.
///
/// Right for every number, because a column of numbers only reads as a column
/// when the decimal points line up; left for `SYMBOL`, which is a name. In
/// `SPARK` the flag places the header alone — the glyph run is `SPARK_W` wide by
/// construction, so it fills the column either way.
const COLS: [(&str, u16, bool); 8] = [
    ("SYMBOL", 6, LEFT),
    ("LAST", 6, RIGHT),
    ("CHG%", 6, RIGHT),
    ("20D", 6, RIGHT),
    ("VOL", 5, RIGHT),
    ("SPARK", SPARK_W as u16, RIGHT),
    ("WT", 5, RIGHT),
    ("TGT", 5, RIGHT),
];

const LEFT: bool = false;
const RIGHT: bool = true;

/// How many bars of the tail the `SPARK` cell draws. Eight, matching the eight
/// levels: a window narrower than the ramp cannot use all of it.
const SPARK_W: usize = 8;

/// The grid's floor, derived from the columns so the two cannot drift.
const GRID_W: u16 = grid_w();

const fn grid_w() -> u16 {
    // The selection marker owns a column of its own, then every column plus the
    // cell of spacing that follows it — minus the one after the last.
    let mut w = 1;
    let mut i = 0;
    while i < COLS.len() {
        w += COLS[i].1 + 1;
        i += 1;
    }
    w - 1
}

/// One heat cell: a four-cell symbol, a space, a signed percent, and a gap.
const HEAT_W: u16 = 12;

/// Header plus two rows of cells — twelve sectors at any width the workstation
/// is usable at.
const HEAT_H: u16 = 3;

/// Eight levels, low to high.
const SPARK_GLYPHS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];

/// The band edges of the sector ramp, in percent.
///
/// Six edges for six steps, and the last only bites through the clamp in
/// `heat_step`: a 3.4% sector and a 2.6% one land on the same brightest cell.
/// That is the intent — the ramp saturates rather than inventing a seventh
/// shade, which a 256-colour terminal could not render distinctly anyway.
const HEAT_EDGES: [f64; 6] = [0.5, 1.0, 1.5, 2.0, 2.5, 3.3];

/// The sector map's universe: twelve liquid ETF proxies rather than 500
/// constituents.
///
/// This is FinceptTerminal's mechanism (its sector view is a grid of SPDR
/// proxies, not a market-cap rollup) and it is the only version that works here
/// — the owner prewarms a universe of ETFs, so a constituent-level map would be
/// a screen of `--`. Which subset is on screen depends on the prewarmed tier.
const SPDR_SECTORS: [&str; 12] = [
    "XLK", "XLV", "XLF", "XLE", "XLY", "XLI", "XLB", "XLU", "XLRE", "XLC", "XLP", "SOXX",
];

/// Where the operator is looking. Never what the desk says — that is the
/// `Store`'s, and a view that held a copy would be a second account of it.
#[derive(Default)]
pub struct MarketsView {
    selected: usize,
    /// An index into the selected asset's history, or no crosshair at all.
    /// Absent rather than defaulted to the last bar: a rule and a chip nobody
    /// asked for read as a measurement the desk made.
    crosshair: Option<usize>,
}

impl MarketsView {
    /// Where the operator is looking, for the registry's own tests.
    ///
    /// `cfg(test)` rather than a plain accessor: the draw path reads these off
    /// `self`, and a second reader in the runtime would be a copy of the cursor
    /// that could disagree with the one on screen.
    #[cfg(test)]
    pub(crate) fn selected(&self) -> usize {
        self.selected
    }

    #[cfg(test)]
    pub(crate) fn crosshair(&self) -> Option<usize> {
        self.crosshair
    }

    /// The selected row, clamped to the universe actually on screen.
    ///
    /// The universe can shrink between the keystroke that moved the cursor and
    /// the frame that draws it — `mandate.yaml` flipping back off `extended`
    /// does exactly that — and an index past the end must render the last row
    /// rather than panic in the draw path.
    fn row(&self, rows: usize) -> usize {
        self.selected.min(rows.saturating_sub(1))
    }

    fn select(&mut self, row: usize) {
        if self.selected != row {
            // Index 3 of one asset's history is a different point from index 3
            // of another's. Carrying it across would leave the chip on screen
            // and silently change what it means.
            self.crosshair = None;
        }
        self.selected = row;
    }
}

impl View for MarketsView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant) {
        let rows = Layout::vertical([Constraint::Min(0), Constraint::Length(HEAT_H)]).split(area);
        // The block's rule is what separates the grid from the sector strip;
        // the strip below it is headed, not boxed, like every other panel.
        let block = panel_block();
        let main = block.inner(rows[0]);
        f.render_widget(block, rows[0]);

        // The grid takes its floor before the hero takes its share. At 120 cells
        // the specified 60/40 split leaves the eight columns six cells short of
        // what the widest row needs, and a grid whose numbers are truncated is
        // worse than a chart that is narrower. Above ~135 cells the percentage
        // wins and the split is the specified one.
        //
        // The cell of spacing is not decoration: at the floor the two panes abut,
        // and the hero's gutter label lands against the `TGT` column as
        // `6.3%156.36` — two numbers an operator has to parse apart.
        //
        // Below the floor `Min` yields: ratatui shrinks the columns underneath
        // the widths every cell was held to, and a right-aligned cell loses its
        // *leading* characters — the arrow first, then the sign, then the
        // leading digit, then a spark bar off the head of a window whose colour
        // is still computed from all eight values. `head` cannot see any of it,
        // because it is spent against the declared width and the allocation is
        // what actually shrank. So the grid refuses rather than drawing numbers
        // that are wrong.
        let cols = Layout::horizontal([Constraint::Min(GRID_W), Constraint::Percentage(40)])
            .spacing(1)
            .split(main);

        // Split first, then guard the *allocation*. The pane is not the grid —
        // the cell of spacing above sits between them — so a guard on `main`
        // admits a grid one cell short of its own floor, and one cell is a spark
        // bar or an arrow. Comparing what the layout actually handed over is
        // what keeps the threshold from drifting the next time the split gains
        // a constraint; spelling it as `GRID_W + 1` would encode the spacing a
        // second place and go stale exactly the same way.
        if cols[0].width < GRID_W {
            refuse(
                f,
                main,
                format!(
                    "markets grid needs {GRID_W} columns to render its numbers — this pane has {}, widen the terminal",
                    cols[0].width
                ),
            );
            draw_sectors(f, rows[1], store);
            return;
        }

        let facts = store.asset_facts();
        let selected = self.row(facts.len());
        draw_grid(f, cols[0], store, &facts, selected, fx, now);
        draw_hero(f, cols[1], &facts, selected, self.crosshair);
        draw_sectors(f, rows[1], store);
    }

    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        let facts = store.asset_facts();
        let selected = self.row(facts.len());
        match k.code {
            // Both ends are walls, not wraps: an operator holding an arrow must
            // land on the first or last row, never at the other end of a
            // universe they did not scroll to.
            KeyCode::Up => self.select(selected.saturating_sub(1)),
            KeyCode::Down => self.select((selected + 1).min(facts.len().saturating_sub(1))),
            KeyCode::Left | KeyCode::Right => {
                let len = facts.get(selected).map_or(0, |a| a.history.len());
                // Nothing to point at: a crosshair over an empty series would
                // draw a rule and a chip for a price the owner never sent.
                if len > 0 {
                    let last = len - 1;
                    let forward = k.code == KeyCode::Right;
                    self.crosshair = Some(match (self.crosshair, forward) {
                        // Whichever direction the operator reached for is the
                        // edge they meant to start from.
                        (None, true) => 0,
                        (None, false) => last,
                        (Some(i), true) => (i + 1).min(last),
                        (Some(i), false) => i.saturating_sub(1),
                    });
                }
            }
            _ => {}
        }
        None
    }
}

fn draw_grid(
    f: &mut Frame,
    area: Rect,
    store: &Store,
    facts: &[AssetFacts],
    selected: usize,
    fx: &FlashTracker,
    now: Instant,
) {
    let t = theme();
    if facts.is_empty() {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "no market assets in the last snapshot",
                Style::default().fg(t.text_tertiary),
            ))),
            area,
        );
        return;
    }

    // A header that sat over the other edge of its column would be a title for
    // a column that is not there.
    let header = Row::new(
        COLS.map(|(name, width, right)| cell(name.to_string(), Style::default(), right, width)),
    )
    .style(Style::default().fg(t.text_secondary));

    let book = store.snapshot.as_ref().and_then(|s| s.portfolio.as_ref());
    let rows = facts.iter().map(|facts| {
        // The live half of the row. `asset_view` is the only reader of a price
        // in this client; the overlay is why.
        let view = store.asset_view(facts.ticker);
        let (change, tone) = view
            // `arrow_chg` is the magnitude helper, so the fraction the owner
            // sends becomes percent units here and the `CHG%` header carries
            // the unit. The sign stays in the glyph either way.
            .change_1d
            .map(|c| format::arrow_chg(c * 100.0))
            .unwrap_or((MISSING.to_string(), t.text_secondary));

        // A weight the book does not carry is absent. `0.0%` would read as a
        // deliberate exclusion, which is a decision nobody made.
        let weight = |held: Option<&f64>| {
            held.map(|w| format::pct1(*w))
                .unwrap_or_else(|| MISSING.to_string())
        };

        // In `COLS` order and zipped with it below, so a cell and the column it
        // is held to cannot drift apart.
        let cells: [(String, Style); COLS.len()] = [
            (
                facts.ticker.to_string(),
                Style::default().fg(t.cyan).add_modifier(Modifier::BOLD),
            ),
            (
                view.price
                    .map(format::price)
                    .unwrap_or_else(|| MISSING.to_string()),
                Style::default().fg(t.accent),
            ),
            (
                change,
                fx.style_for(
                    &FlashKey::change(facts.ticker),
                    now,
                    Style::default().fg(tone),
                ),
            ),
            (opt_pct(facts.change_20d), tone_of(facts.change_20d)),
            (
                opt_pct(facts.realized_vol),
                Style::default().fg(t.text_secondary),
            ),
            spark_cell(facts.history),
            (
                weight(book.and_then(|b| b.weights.get(facts.ticker))),
                Style::default().fg(t.text_primary),
            ),
            (
                weight(book.and_then(|b| b.target_weights.get(facts.ticker))),
                Style::default().fg(t.text_secondary),
            ),
        ];
        Row::new(
            cells
                .into_iter()
                .zip(COLS)
                .map(|((text, style), (_, width, right))| cell(text, style, right, width)),
        )
    });

    let table = Table::new(rows, COLS.map(|(_, w, _)| Constraint::Length(w)))
        .header(header)
        .column_spacing(1)
        // A marker, not only a shade: on a 256-colour terminal the highlight is
        // a shade, and a shade is not an answer to "which row is the chart".
        .highlight_symbol(Text::from(Span::styled(
            "▌",
            Style::default().fg(t.accent),
        )))
        .row_highlight_style(Style::default().bg(t.bg_hover));
    f.render_stateful_widget(
        table,
        area,
        // Derived from the view's cursor every frame rather than retained: the
        // selection has exactly one home, and it is the view.
        &mut TableState::new().with_selected(Some(selected)),
    );
}

/// One table cell, right-aligned unless it is a name or a picture, and held to
/// its column's width.
///
/// Right because a column of numbers only reads as a column when the decimal
/// points line up. Held because ratatui right-aligns an overlong line by
/// dropping its *leading* cells, and the leading cell of a number is its sign:
/// a `-10.1%` twenty-day change renders as `10.1%` in a five-wide column — a
/// loss drawn as a gain. Keeping the head instead costs the last digit, which
/// is a number that is coarse rather than one that is wrong.
fn cell(text: String, style: Style, right: bool, width: u16) -> Cell<'static> {
    let line = Line::from(Span::styled(head(text, width), style));
    Cell::from(if right {
        line.right_aligned()
    } else {
        line.left_aligned()
    })
}

/// The leading `width` characters of `text`, or all of it.
///
/// Characters rather than bytes, and every glyph this grid renders is one cell
/// wide — the arrows in `CHG%` and the eight spark levels included.
fn head(text: String, width: u16) -> String {
    match text.char_indices().nth(width as usize) {
        Some((byte, _)) => text[..byte].to_string(),
        None => text,
    }
}

/// The 8-level quantize of a series tail, and the tone that says which way it
/// went.
///
/// Ratatui's `Sparkline` is a `Widget` and a `Table` cell holds `Text`, so a
/// real sparkline cannot live in this column. The glyphs are the same eight
/// levels drawn as text.
fn spark_cell(history: &[f64]) -> (String, Style) {
    let t = theme();
    let glyphs = spark(history, SPARK_W);
    if glyphs.is_empty() {
        return (MISSING.to_string(), Style::default().fg(t.text_tertiary));
    }
    // Slope over the window the cell actually draws — the same slice `spark`
    // quantized, not the whole series. The reference desk colours a sparkline by
    // its own visible direction, and it has to: a tail climbing out of a crash
    // painted red says the bars on screen are falling, which they are not.
    let window = tail(history, SPARK_W);
    let rising = match (window.first(), window.last()) {
        (Some(first), Some(last)) => last >= first,
        _ => true,
    };
    (
        glyphs,
        Style::default().fg(if rising { t.positive } else { t.negative }),
    )
}

fn opt_pct(value: Option<f64>) -> String {
    value.map(format::pct1).unwrap_or_else(|| MISSING.to_string())
}

fn tone_of(value: Option<f64>) -> Style {
    let t = theme();
    Style::default().fg(value.map(|v| t.change(v)).unwrap_or(t.text_secondary))
}

/// The selected asset, charted.
fn draw_hero(
    f: &mut Frame,
    area: Rect,
    facts: &[AssetFacts],
    selected: usize,
    crosshair: Option<usize>,
) {
    // An empty universe: the grid beside this one already says so, and a second
    // copy of the same sentence reads as two separate failures.
    let Some(asset) = facts.get(selected) else {
        return;
    };
    let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).split(area);
    // `▌ SPY`, not `▌ SPY — SPDR S&P 500`: the owner's `market.assets` rows carry
    // a ticker, a price and a history and no long name (`model::Asset`), and a
    // client that filled the name in from a table of its own would be asserting
    // an instrument identity the owner never stated.
    f.render_widget(Paragraph::new(panel_header(asset.ticker)), rows[0]);
    braille_chart::draw(f, rows[1], asset.history, crosshair);
}

/// The sector strip: one heat cell per SPDR proxy the snapshot actually carried.
fn draw_sectors(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).split(area);
    f.render_widget(Paragraph::new(panel_header("sectors")), rows[0]);

    // In the canonical order rather than the snapshot's: the map is read by
    // position, and a strip that reshuffled when the owner reordered its
    // universe would be a different map every poll.
    let held: Vec<&str> = store.asset_facts().into_iter().map(|f| f.ticker).collect();
    let cells: Vec<Span<'static>> = SPDR_SECTORS
        .iter()
        .filter(|sector| held.contains(sector))
        .map(|sector| {
            let change = store.asset_view(sector).change_1d;
            let label = format!(
                "{sector:<4} {}",
                change
                    .map(format::signed_pct)
                    .unwrap_or_else(|| MISSING.to_string())
            );
            Span::styled(
                // Padded *and* truncated: every cell is exactly `HEAT_W` wide,
                // so a double-digit sector move cannot shove the row's grid out
                // of alignment.
                format!("{label:<width$.width$} ", width = HEAT_W as usize - 1),
                match change {
                    Some(change) => heat_style(change),
                    None => Style::default().fg(t.text_tertiary),
                },
            )
        })
        .collect();

    if cells.is_empty() {
        // Fail loud. An empty strip reads as "every sector is flat", which is a
        // statement about the market that nobody made. Through `refuse` like the
        // other two, because a remedy clipped to `qlab prewar` is a remedy an
        // operator cannot run.
        refuse(
            f,
            rows[1],
            "sector map needs the extended universe — qlab prewarm --universe candidates".to_string(),
        );
        return;
    }

    let per_row = (rows[1].width / HEAT_W).max(1) as usize;
    let lines: Vec<Line<'static>> = cells
        .chunks(per_row)
        .map(|chunk| Line::from(chunk.to_vec()))
        .collect();

    // The same sub-floor class as the grid: a `Paragraph` taller than its area
    // is clipped silently, and a sector map missing its last row is a map that
    // says a sector did not move. It says how wide it needs to be instead.
    if lines.len() > rows[1].height as usize {
        let needed = cells.len().div_ceil(rows[1].height.max(1) as usize) as u16 * HEAT_W;
        refuse(
            f,
            rows[1],
            format!(
                "sector map needs {needed} columns for {} sectors — this strip has {}, widen the terminal",
                cells.len(),
                rows[1].width
            ),
        );
        return;
    }
    f.render_widget(Paragraph::new(lines), rows[1]);
}

/// A pane refusing to draw, and saying what it would take.
///
/// One shape for all three refusals on this view — no sectors prewarmed, a grid
/// under its floor, a strip under its own — because they are one statement:
/// what is missing, then the remedy. Wrapped, since a pane too narrow to hold
/// the numbers is also too narrow to hold the sentence about them, and a remedy
/// clipped to `qlab prewar` cannot be run. Silence, a clipped pane and a
/// half-drawn grid are the three renderings an operator cannot tell from a
/// working desk.
fn refuse(f: &mut Frame, area: Rect, message: String) {
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            message,
            Style::default().fg(theme().text_dim),
        )))
        .wrap(Wrap { trim: true }),
        area,
    );
}

/// The quantized heat ramp.
///
/// Inline here until Task 13 lifts it into a shared `heat_cell` widget — the
/// book and research grids want the same six steps and the same two-token
/// spend, and three copies of this arithmetic is how the three of them end up
/// disagreeing about what a 2% move looks like.
///
/// A cell grid has no alpha, so "six alpha steps" is spent as three levels of
/// the depth ramp and then the semantic pair itself — the same technique
/// `fx::style_for` uses to fade a flash without a fade.
fn heat_style(change: f64) -> Style {
    let t = theme();
    let (dim, bright) = if change >= 0.0 {
        (t.positive_dim, t.positive)
    } else {
        (t.negative_dim, t.negative)
    };
    let base = Style::default();
    match heat_step(change * 100.0) {
        1 => base.bg(t.bg_base).fg(dim),
        2 => base.bg(t.bg_raised).fg(dim),
        3 => base.bg(t.bg_hover).fg(bright),
        4 => base.bg(dim).fg(t.text_primary),
        5 => base.bg(dim).fg(t.text_primary).add_modifier(Modifier::BOLD),
        _ => base.bg(bright).fg(t.bg_base).add_modifier(Modifier::BOLD),
    }
}

/// Which of the six steps a move of `change_pct` percent lands on.
///
/// Magnitude only: brightness says *how much* and the positive/negative token
/// pair says which way. A ramp that folded the sign in would make a 2% fall
/// dimmer than a 2% rise for no reason an operator could name.
fn heat_step(change_pct: f64) -> u8 {
    let magnitude = change_pct.abs();
    let crossed = HEAT_EDGES.iter().filter(|edge| magnitude >= **edge).count();
    (1 + crossed).min(6) as u8
}

/// The window a spark is drawn from: the last `width` closes, or all of them.
///
/// One definition, because the glyphs and the colour must be reading the same
/// slice. Two spellings of "the tail" is how the bars came to say one thing and
/// the colour another.
fn tail(history: &[f64], width: usize) -> &[f64] {
    &history[history.len().saturating_sub(width)..]
}

/// The last `width` closes, quantized into the eight block glyphs.
///
/// Scaled to the window rather than the whole series: the cell's job is the
/// recent shape, and an outlier twenty bars back would flatten every bar the
/// operator is actually looking at. `history` is finite by construction — JSON
/// carries no NaN, so the model cannot decode one.
fn spark(history: &[f64], width: usize) -> String {
    if history.is_empty() || width == 0 {
        return String::new();
    }
    let tail = tail(history, width);
    let lo = tail.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = tail.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let span = hi - lo;
    let top = SPARK_GLYPHS.len() - 1;
    tail.iter()
        .map(|value| {
            let level = if span > 0.0 {
                (((value - lo) / span) * top as f64).round() as usize
            } else {
                // A series with no range is neither at the top of its range nor
                // at the bottom of it. A row of `▁` claims one and a row of `█`
                // claims the other; the middle claims neither.
                SPARK_GLYPHS.len() / 2 - 1
            };
            SPARK_GLYPHS[level.min(top)]
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_spark_quantizes_a_known_series_into_the_eight_glyphs() {
        // A `Sparkline` cannot live inside a `Table` cell, so the row draws its
        // own. Pinning the exact string is the only way a quantizer that is
        // subtly off by a level fails loudly rather than looking plausible.
        assert_eq!(
            spark(&[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], 8),
            "▁▂▃▄▅▆▇█"
        );
        assert_eq!(
            spark(&[8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0], 8),
            "█▇▆▅▄▃▂▁"
        );
    }

    #[test]
    fn the_spark_reads_the_tail_and_scales_to_it() {
        // The window is the recent shape, so an old outlier must not flatten
        // every bar the operator is actually looking at.
        assert_eq!(spark(&[100.0, 1.0, 2.0, 3.0], 3), "▁▅█");
    }

    #[test]
    fn the_spark_takes_its_colour_from_the_window_it_draws() {
        // The bars are the window, so the colour has to be the window's. Read
        // off the whole series instead, a tail climbing out of a crash paints
        // red — the cell then says the eight bars on screen are falling while
        // they visibly rise, which is the one thing a sparkline must not do.
        let crashed_then_climbing = [100.0, 50.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0];
        let (glyphs, style) = spark_cell(&crashed_then_climbing);
        assert_eq!(glyphs, "▁▂▃▄▅▆▇█", "the window is the last eight closes");
        assert_eq!(
            style.fg,
            Some(theme().positive),
            "a rising window painted as a fall"
        );

        // The mirror, so this cannot be satisfied by colouring everything green.
        let rallied_then_sliding = [1.0, 2.0, 100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0];
        assert_eq!(spark_cell(&rallied_then_sliding).1.fg, Some(theme().negative));
    }

    #[test]
    fn a_flat_series_reads_from_the_middle_rather_than_the_floor() {
        // A row of `▁` reads as "at the bottom of its range" and a row of `█` as
        // "at the top". A series with no range is neither.
        assert_eq!(spark(&[5.0, 5.0, 5.0], 3), "▄▄▄");
        assert_eq!(spark(&[5.0], 4), "▄");
    }

    #[test]
    fn a_series_the_owner_did_not_send_draws_nothing_at_all() {
        assert_eq!(spark(&[], 8), "");
    }

    #[test]
    fn the_heat_ramp_bands_at_the_documented_edges_and_then_saturates() {
        // Six steps at 0.5/1/1.5/2/2.5/3.3%. The sixth edge only bites through
        // the clamp — a 3.4% sector and a 2.6% one are the same brightest cell,
        // which is the point: the ramp saturates rather than inventing a
        // seventh shade a 256-colour terminal could not render anyway.
        assert_eq!(heat_step(0.0), 1);
        assert_eq!(heat_step(0.4), 1);
        assert_eq!(heat_step(0.5), 2);
        assert_eq!(heat_step(0.9), 2);
        assert_eq!(heat_step(1.0), 3);
        assert_eq!(heat_step(2.4), 5);
        assert_eq!(heat_step(2.5), 6);
        assert_eq!(heat_step(3.4), 6);
        assert_eq!(heat_step(99.0), 6);
    }

    #[test]
    fn the_heat_ramp_reads_magnitude_and_leaves_direction_to_the_colour() {
        for pct in [0.4, 0.5, 1.2, 2.6, 9.0] {
            assert_eq!(heat_step(pct), heat_step(-pct), "{pct}");
        }
        // And the colour is the direction: two cells of the same magnitude must
        // not be the same style, or the map says nothing about rotation.
        assert_ne!(heat_style(0.021), heat_style(-0.021));
    }

    #[test]
    fn every_heat_step_is_visually_distinct_from_its_neighbour() {
        // Six steps that render as four is a ramp with two decorative bands.
        let styles: Vec<Style> = [0.4, 0.6, 1.2, 1.7, 2.2, 3.5]
            .iter()
            .map(|pct| heat_style(pct / 100.0))
            .collect();
        for i in 0..styles.len() {
            for j in (i + 1)..styles.len() {
                assert_ne!(styles[i], styles[j], "step {} and step {} collide", i + 1, j + 1);
            }
        }
    }

    #[test]
    fn the_sector_set_is_the_twelve_spdr_proxies() {
        assert_eq!(SPDR_SECTORS.len(), 12);
        assert!(SPDR_SECTORS.contains(&"XLK"));
        assert!(SPDR_SECTORS.contains(&"SOXX"));
        // Every symbol fits the cell it is drawn in, label and all.
        for sector in SPDR_SECTORS {
            assert!(sector.len() <= 4, "{sector} does not fit a heat cell");
        }
    }

    #[test]
    fn a_column_too_narrow_for_its_number_loses_the_last_digit_and_not_the_sign() {
        // Ratatui right-aligns an overlong line by dropping its leading cells,
        // so `-10.1%` in a five-wide column renders `10.1%` — a loss drawn as a
        // gain. This is the half of the guard that does not depend on any
        // particular column being wide enough.
        assert_eq!(head("-10.1%".into(), 5), "-10.1");
        assert_eq!(head("▼ 12.34".into(), 6), "▼ 12.3");
        assert_eq!(head("6.3%".into(), 5), "6.3%");
    }

    #[test]
    fn the_twenty_day_column_fits_a_double_digit_fall() {
        // The other half, and the one the fixture actually caught: QQQ is
        // -10.1% over twenty days. A column sized for today's smaller numbers
        // is a column that starts lying on the day the numbers get big.
        let width = COLS.iter().find(|(name, _, _)| *name == "20D").unwrap().1;
        let text = format::pct1(-0.101);
        assert_eq!(text, "-10.1%");
        assert!(
            text.chars().count() <= width as usize,
            "20D is {width} wide and {text} needs {}",
            text.chars().count()
        );
    }

    #[test]
    fn the_grid_floor_is_the_columns_own_arithmetic() {
        // The layout's `Min` and the column widths are one fact. Spelled twice,
        // a column widened for a longer symbol would silently start truncating.
        let columns: u16 = COLS.iter().map(|(_, w, _)| w).sum();
        assert_eq!(GRID_W, columns + COLS.len() as u16 - 1 + 1);
    }
}
