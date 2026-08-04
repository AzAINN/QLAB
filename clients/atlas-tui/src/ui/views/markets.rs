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
use crate::ui::widgets::table_cell::{cell, LEFT, RIGHT};
use crate::ui::widgets::tristate_spark::{self, SPARK_W};
use crate::ui::widgets::{
    braille_chart, header_keys, heat_cell, panel_block, panel_header, pulse, refuse,
};
use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Paragraph, Row, Table, TableState},
    Frame,
};
use std::cell::Cell;
use std::time::Instant;
use unicode_width::UnicodeWidthStr;

/// The eight columns: title, the cells each needs at its widest rendering, and
/// whether its contents are pushed right.
///
/// `SYMBOL` is seven: six for the header itself and one for the sort glyph the
/// name sort parks on it — a column narrower than its own title renders `SYMB`,
/// and a truncated header is a column an operator has to guess.
/// `20D` is six because `format::pct1` spends one on the sign — a twenty-day
/// change is the one column here that is routinely negative and routinely
/// double-digit, and `-10.1%` does not fit in five.
///
/// Right for every number, because a column of numbers only reads as a column
/// when the decimal points line up; left for `SYMBOL`, which is a name. In
/// `SPARK` the flag places the header alone — the glyph run is `SPARK_W` wide by
/// construction, so it fills the column either way.
const COLS: [(&str, u16, bool); 8] = [
    ("SYMBOL", 7, LEFT),
    ("LAST", 6, RIGHT),
    ("CHG%", 6, RIGHT),
    ("20D", 6, RIGHT),
    ("VOL", 5, RIGHT),
    ("SPARK", SPARK_W as u16, RIGHT),
    ("WT", 5, RIGHT),
    ("TGT", 5, RIGHT),
];

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

/// The breadth strip: its header — which carries the sort hints — and the one
/// line of facts under it.
const BREADTH_H: u16 = 2;

/// The adv/dec bar's width. Twelve cells for a universe of a dozen: one cell a
/// name at the tiers the owner prewarms, and narrow enough that the four chips
/// beside it fit the workstation's baseline pane.
const BREADTH_BAR_W: u16 = 12;

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

/// Which order the grid's rows are in.
///
/// `Payload` is the owner's own order and the default: every other list in
/// this client takes the payload's order, and a grid that opened re-sorted
/// would be a second opinion about which asset the desk lists first. The other
/// three are the questions an operator re-sorts a universe to answer — what is
/// moving, what is volatile, and where is a name I know.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Sort {
    #[default]
    Payload,
    Change,
    Vol,
    Name,
}

impl Sort {
    /// The cycle `s` walks, in the order it walks it.
    const ALL: [Sort; 4] = [Sort::Payload, Sort::Change, Sort::Vol, Sort::Name];

    fn next(self) -> Sort {
        let at = Sort::ALL.iter().position(|s| *s == self).unwrap_or(0);
        Sort::ALL[(at + 1) % Sort::ALL.len()]
    }

    /// The column this sort heads, as an index into `COLS` — or none, because
    /// the payload's order is nobody's column.
    fn column(self) -> Option<usize> {
        match self {
            Sort::Payload => None,
            Sort::Name => Some(0),
            Sort::Change => Some(2),
            Sort::Vol => Some(4),
        }
    }

    /// Direction is fixed per column, exactly as BOOK's blotter: `s` is one
    /// key, and a second key for the direction is a control nobody asked for.
    /// A move sorts biggest-first because the question is *what is moving*,
    /// and a name sorts A-to-Z because that is how a name is looked up.
    fn descending(self) -> bool {
        !matches!(self, Sort::Name | Sort::Payload)
    }
}

/// The grid's rows in the order `sort` asks for.
///
/// The change key reads `asset_view`, not the snapshot: the column it orders is
/// the one the quote stream overtakes, and a sort off the poll's number would
/// order rows by prices the grid is no longer showing. `sort_by` is stable, so
/// the owner's order survives a tie; a value the owner did not send sorts last
/// whichever way the column runs — absent is not zero, and a `--` at the head
/// of a biggest-first list is the row an operator reads as the answer.
fn ordered<'a>(store: &'a Store, sort: Sort) -> Vec<AssetFacts<'a>> {
    let mut facts = store.asset_facts();
    let by = |facts: &mut Vec<AssetFacts<'a>>, key: &dyn Fn(&AssetFacts) -> Option<f64>| {
        facts.sort_by(|a, b| match (key(a), key(b)) {
            (Some(a), Some(b)) => b.partial_cmp(&a).unwrap_or(std::cmp::Ordering::Equal),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        });
    };
    match sort {
        Sort::Payload => {}
        Sort::Name => facts.sort_by(|a, b| a.ticker.cmp(b.ticker)),
        // Magnitude, not the signed move: "what is moving" is a question about
        // size, and a signed sort would file the day's biggest fall last.
        Sort::Change => by(&mut facts, &|f| {
            store.asset_view(f.ticker).change_1d.map(f64::abs)
        }),
        Sort::Vol => by(&mut facts, &|f| f.realized_vol),
    }
    facts
}

/// Where the operator is looking. Never what the desk says — that is the
/// `Store`'s, and a view that held a copy would be a second account of it.
#[derive(Default)]
pub struct MarketsView {
    selected: usize,
    /// An index into the selected asset's history, or no crosshair at all.
    /// Absent rather than defaulted to the last bar: a rule and a chip nobody
    /// asked for read as a measurement the desk made.
    crosshair: Option<usize>,
    /// Which order the grid is in. The operator's, so it lives here.
    sort: Sort,
    /// Where the last frame drew the grid's table, so a click can be read back
    /// as a row. Geometry, never anything the operator set — the same shape as
    /// ATLAS's `input_row`, and a `Cell` for the same reason: only the draw
    /// knows the allocation, and a repaint records the same rect.
    grid: Cell<Rect>,
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

    #[cfg(test)]
    pub(crate) fn sort(&self) -> Sort {
        self.sort
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

    /// Put the cursor on a symbol, and say whether this pane holds one.
    ///
    /// The grid's own rows, so a symbol the market section does not carry gets
    /// `false` rather than a cursor parked on the wrong row — the command line
    /// is what turns that into a sentence.
    ///
    /// The index is into the *sorted* rows, because that is what the cursor
    /// means: a row found in payload order and selected by that index would
    /// land the marker on whatever row the current sort happens to have put
    /// there.
    pub(crate) fn select_ticker(&mut self, symbol: &str, store: &Store) -> bool {
        let Some(row) = ordered(store, self.sort)
            .iter()
            .position(|asset| asset.ticker == symbol)
        else {
            return false;
        };
        self.select(row);
        true
    }

    /// Move the crosshair one bar, or plant it at the edge it was reached from.
    ///
    /// `bars` is how long the selected asset's series is. Zero is nothing to
    /// point at: a crosshair over an empty series would draw a rule and a chip
    /// for a price the owner never sent.
    fn step_crosshair(&mut self, forward: bool, bars: usize) {
        if bars == 0 {
            return;
        }
        let last = bars - 1;
        self.crosshair = Some(match (self.crosshair, forward) {
            // Whichever direction the operator reached for is the edge they
            // meant to start from.
            (None, true) => 0,
            (None, false) => last,
            (Some(i), true) => (i + 1).min(last),
            (Some(i), false) => i.saturating_sub(1),
        });
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
        // The breadth strip goes first because it is the sentence the view
        // exists to open with — what kind of day — and the two facts under it
        // are what the grid then itemises. Its rows come off the hero's share
        // of the pane, not the grid's: the grid keeps its floor either way.
        let rows = Layout::vertical([
            Constraint::Length(BREADTH_H),
            Constraint::Min(0),
            Constraint::Length(HEAT_H),
        ])
        .split(area);
        draw_breadth(f, rows[0], store, self.sort);
        // The block's rule is what separates the grid from the sector strip;
        // the strip below it is headed, not boxed, like every other panel.
        let block = panel_block();
        let main = block.inner(rows[1]);
        f.render_widget(block, rows[1]);

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
            // A refused grid is a grid a click cannot land on.
            self.grid.set(Rect::default());
            refuse(
                f,
                main,
                format!(
                    "markets grid needs {GRID_W} columns to render its numbers — this pane has {}, widen the terminal",
                    cols[0].width
                ),
            );
            draw_sectors(f, rows[2], store);
            return;
        }

        let facts = ordered(store, self.sort);
        let selected = self.row(facts.len());
        self.grid.set(cols[0]);
        draw_grid(f, cols[0], store, &facts, selected, self.sort, fx, now);
        draw_hero(f, cols[1], &facts, selected, self.crosshair);
        draw_sectors(f, rows[2], store);
    }

    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        let facts = ordered(store, self.sort);
        let selected = self.row(facts.len());
        let bars = facts.get(selected).map_or(0, |a| a.history.len());
        match k.code {
            // Both ends are walls, not wraps: an operator holding an arrow must
            // land on the first or last row, never at the other end of a
            // universe they did not scroll to.
            KeyCode::Up => self.select(selected.saturating_sub(1)),
            KeyCode::Down => self.select((selected + 1).min(facts.len().saturating_sub(1))),
            // Two arms rather than one arm that re-reads `k.code` for the
            // direction. The direction is what the *pattern* already said, and
            // asking again spelled the right-arrow variant twice in one router
            // — which the keymap equivalence has to count as two bindings,
            // because it cannot tell a pattern from a comparison.
            KeyCode::Left => self.step_crosshair(false, bars),
            KeyCode::Right => self.step_crosshair(true, bars),
            // Cycling the order resets the cursor and its crosshair: after a
            // re-sort, row 2 is a different asset, and carrying either would
            // leave the marker — and a chip — on a row the operator did not
            // choose. Row zero, not the followed ticker: the point of a
            // re-sort is to read the top of the new order.
            KeyCode::Char('s') => {
                self.sort = self.sort.next();
                self.selected = 0;
                self.crosshair = None;
            }
            _ => {}
        }
        None
    }

    /// The wheel walks the cursor and a click plants it — the mouse spelling of
    /// the arrow keys, with the same walls at both ends.
    fn on_mouse(&mut self, m: MouseEvent, store: &mut Store) -> Option<Command> {
        let rows = store.asset_facts().len();
        let selected = self.row(rows);
        match m.kind {
            MouseEventKind::ScrollUp => self.select(selected.saturating_sub(1)),
            MouseEventKind::ScrollDown => self.select((selected + 1).min(rows.saturating_sub(1))),
            MouseEventKind::Down(MouseButton::Left) => {
                // The first row of the published rect is the column header, so
                // a click's row is its offset past it. Only a row the grid
                // actually drew: a click in the blank under a five-asset
                // universe selects nothing rather than the last row.
                let grid = self.grid.get();
                if grid.height > 0
                    && m.row > grid.y
                    && m.column >= grid.x
                    && m.column < grid.x.saturating_add(grid.width)
                {
                    let row = (m.row - grid.y - 1) as usize;
                    if row < rows {
                        self.select(row);
                    }
                }
            }
            _ => {}
        }
        None
    }
}

#[allow(clippy::too_many_arguments)]
fn draw_grid(
    f: &mut Frame,
    area: Rect,
    store: &Store,
    facts: &[AssetFacts],
    selected: usize,
    sort: Sort,
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
    // a column that is not there. The sort glyph rides the live column so a
    // grid that reorders itself always says why — `▴`/`▾` rather than the
    // `▲`/`▼` the change column uses, because a sort direction and a price
    // direction are different claims and must not share a mark.
    let live = sort.column();
    let arrow = if sort.descending() { "▾" } else { "▴" };
    let header = Row::new(COLS.iter().enumerate().map(|(i, (name, width, right))| {
        let title = if Some(i) == live {
            format!("{name}{arrow}")
        } else {
            (*name).to_string()
        };
        cell(title, Style::default(), *right, *width)
    }))
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
            (format::opt_pct(facts.change_20d), tone_of(facts.change_20d)),
            (
                format::opt_pct(facts.realized_vol),
                Style::default().fg(t.text_secondary),
            ),
            tristate_spark::cell(facts.history),
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
        .highlight_symbol(Text::from(Span::styled("▌", Style::default().fg(t.accent))))
        .row_highlight_style(Style::default().bg(t.bg_hover));
    f.render_stateful_widget(
        table,
        area,
        // Derived from the view's cursor every frame rather than retained: the
        // selection has exactly one home, and it is the view.
        &mut TableState::new().with_selected(Some(selected)),
    );
}

/// The tone for a column rendered through `opt_pct` — one decimal of a percent,
/// which is the precision the colour has to be decided at.
fn tone_of(value: Option<f64>) -> Style {
    let t = theme();
    Style::default().fg(value
        .map(|v| format::change_tone(v * 100.0, 1))
        .unwrap_or(t.text_secondary))
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
    braille_chart::draw(
        f,
        rows[1],
        braille_chart::Chart {
            name: "price chart",
            series: asset.history,
            crosshair,
            // A quote, spelled as the grid beside it spells one. The chart's
            // gutter width follows from this, which is what lets the same
            // widget carry BOOK's `$10,012.40` equity scale without either
            // surface clipping the other's numbers.
            label: format::price,
        },
    );
}

/// The breadth strip: adv/dec, the two ends of the tape, and how spread out the
/// day is — the "what kind of day" sentence, stated once at the top.
///
/// Every number here comes through the same arithmetic the pulse rail uses
/// (`pulse::breadth`, `pulse::movers`): a strip that counted for itself would
/// be a second account of one universe, and the rail is one column away.
fn draw_breadth(f: &mut Frame, area: Rect, store: &Store, sort: Sort) {
    let t = theme();
    if area.height < BREADTH_H {
        return;
    }
    let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).split(area);
    f.render_widget(
        Paragraph::new(header_keys("markets", sort_keys(sort), rows[0].width)),
        rows[0],
    );

    let (advancing, declining) = pulse::breadth(store);
    // Chips are grouped so the fit below drops a whole fact: a mover popped
    // span by span would leave `worst XLK` with no number on it.
    let mut chips: Vec<Vec<Span<'static>>> = Vec::new();
    if advancing + declining == 0 {
        // Absent is not flat: a desk with no marks has an unknown breadth.
        chips.push(vec![Span::styled(
            MISSING.to_string(),
            Style::default().fg(t.text_tertiary),
        )]);
    } else {
        let (up, down) = pulse::segments(advancing, declining, BREADTH_BAR_W);
        chips.push(vec![
            Span::styled("█".repeat(up as usize), Style::default().fg(t.positive)),
            Span::styled("█".repeat(down as usize), Style::default().fg(t.negative)),
            Span::styled(format!(" ▲{advancing}"), Style::default().fg(t.positive)),
            Span::styled(format!("▼{declining}"), Style::default().fg(t.negative)),
        ]);
    }
    if let Some((best, worst)) = pulse::movers(store) {
        let end = |role: &str, ticker: &str, change: Option<f64>| {
            let change = change.unwrap_or_default();
            let tone = format::change_tone(change * 100.0, 2);
            vec![
                Span::styled(format!("  {role} "), Style::default().fg(t.text_secondary)),
                // Padded to five as the pulse rail pads its movers — one
                // spelling of "a mover" per workstation, and the padding is
                // also what keeps this chip from reading as a sector heat cell,
                // whose label is the same ticker one space from the same move.
                Span::styled(
                    format!("{ticker:<5}"),
                    Style::default().fg(t.cyan).add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!(" {}", format::signed_pct(change)),
                    Style::default().fg(tone),
                ),
            ]
        };
        chips.push(end("best", best.ticker, best.change_1d));
        // A universe of one is its own best and worst, and stating it twice
        // would read as two movers — the rail's rule, one pane over.
        if best.ticker != worst.ticker {
            chips.push(end("worst", worst.ticker, worst.change_1d));
        }
    }
    if let Some(spread) = dispersion(store) {
        chips.push(vec![
            Span::styled("  disp ", Style::default().fg(t.text_secondary)),
            Span::styled(format::pct1(spread), Style::default().fg(t.text_primary)),
        ]);
    }

    // Whole chips or not at all, from the right: a dispersion clipped to
    // `disp 1.` is a number that is wrong, and the chips are ordered so what
    // goes first is what the sentence can most afford to lose.
    let width = |chips: &[Vec<Span>]| -> usize {
        chips
            .iter()
            .flatten()
            .map(|s| s.content.width())
            .sum::<usize>()
    };
    while chips.len() > 1 && width(&chips) > rows[1].width as usize {
        chips.pop();
    }
    f.render_widget(
        Paragraph::new(Line::from(chips.into_iter().flatten().collect::<Vec<_>>())),
        rows[1],
    );
}

/// The sort hints on the strip's far side, with the live order lit.
fn sort_keys(sort: Sort) -> Vec<Span<'static>> {
    let t = theme();
    let mut keys = vec![Span::styled("s sort ", Style::default().fg(t.text_dim))];
    for (option, label) in [
        (Sort::Payload, "DESK"),
        (Sort::Change, "CHG"),
        (Sort::Vol, "VOL"),
        (Sort::Name, "NAME"),
    ] {
        let style = if option == sort {
            Style::default().fg(t.text_primary)
        } else {
            Style::default().fg(t.text_dim)
        };
        keys.push(Span::styled(format!(" {label}"), style));
    }
    keys.push(Span::styled(
        "  ↑↓ row  ←→ bar",
        Style::default().fg(t.text_dim),
    ));
    keys
}

/// How spread out the day's moves are: the gap between the universe's best and
/// worst one-day change, as a fraction.
///
/// The range rather than a standard deviation, because it is the version an
/// operator can check against the two names beside it: `best − worst`, and the
/// movers chips state both ends. Two marked assets is the least a spread can be
/// measured between; one is a gap of zero that nobody measured.
fn dispersion(store: &Store) -> Option<f64> {
    let changes: Vec<f64> = store
        .asset_views()
        .iter()
        .filter_map(|view| view.change_1d)
        .collect();
    if changes.len() < 2 {
        return None;
    }
    let hi = changes.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let lo = changes.iter().copied().fold(f64::INFINITY, f64::min);
    Some(hi - lo)
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
            "sector map needs the extended universe — qlab prewarm --universe candidates"
                .to_string(),
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

/// One sector cell's style: the semantic pair the direction picks, at the step
/// the magnitude lands on.
fn heat_style(change: f64) -> Style {
    let t = theme();
    // At the two decimals of a percent the cell's own label prints through
    // `signed_pct`, never off the raw double: a sector at -1e-13 reads `+0.00%`
    // and took the negative pair, the shade contradicting the digits on it.
    // Two states rather than three here, matching `change_tone`: a sector map is
    // read for direction, and one cell of it painted neutral would read as a
    // sector that failed to arrive rather than one that did not move.
    let (dim, bright) = if format::negative_at(change * 100.0, 2) {
        (t.negative_dim, t.negative)
    } else {
        (t.positive_dim, t.positive)
    };
    heat_cell::style(heat_step(change * 100.0), dim, bright)
}

/// Which of the six steps a move of `change_pct` percent lands on.
///
/// Magnitude only: brightness says *how much* and the positive/negative token
/// pair says which way. A ramp that folded the sign in would make a 2% fall
/// dimmer than a 2% rise for no reason an operator could name.
///
/// The bands stay here rather than in `heat_cell` because they are a fact about
/// sectors — 2% is a large day for a sector and a rounding error for a
/// position's P&L — while the quantization and the spend are shared.
fn heat_step(change_pct: f64) -> u8 {
    heat_cell::step_at(change_pct.abs(), &HEAT_EDGES)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A store carrying the named assets, applied the way the runtime applies
    /// a snapshot — a fixture that bypassed the fold could render a state the
    /// running client can never be in.
    fn store_with(assets: serde_json::Value) -> Store {
        let mut store = Store::default();
        store.apply(
            crate::bus::AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({"market": {"assets": assets}})).unwrap(),
            )),
            Instant::now(),
        );
        store
    }

    fn order(store: &Store, sort: Sort) -> Vec<&str> {
        ordered(store, sort).iter().map(|f| f.ticker).collect()
    }

    #[test]
    fn the_default_order_is_the_owners_and_each_sort_answers_its_own_question() {
        let store = store_with(serde_json::json!([
            {"ticker": "MID", "change_1d": 0.01, "realized_vol": 0.20},
            {"ticker": "BIG", "change_1d": -0.03, "realized_vol": 0.10},
            {"ticker": "SMALL", "change_1d": 0.002, "realized_vol": 0.30}
        ]));
        assert_eq!(order(&store, Sort::Payload), vec!["MID", "BIG", "SMALL"]);
        // Magnitude, not the signed move: the day's biggest fall leads.
        assert_eq!(order(&store, Sort::Change), vec!["BIG", "MID", "SMALL"]);
        assert_eq!(order(&store, Sort::Vol), vec!["SMALL", "MID", "BIG"]);
        assert_eq!(order(&store, Sort::Name), vec!["BIG", "MID", "SMALL"]);
    }

    #[test]
    fn a_change_the_owner_did_not_send_sorts_last_and_never_first() {
        // Absent is not zero and it is not the biggest either: a `--` at the
        // head of a biggest-first list is the row an operator reads as the
        // answer to "what is moving".
        let store = store_with(serde_json::json!([
            {"ticker": "NONE"},
            {"ticker": "SOME", "change_1d": 0.001, "realized_vol": 0.1}
        ]));
        assert_eq!(*order(&store, Sort::Change).last().unwrap(), "NONE");
        assert_eq!(*order(&store, Sort::Vol).last().unwrap(), "NONE");
    }

    #[test]
    fn an_empty_or_one_row_universe_orders_without_panicking() {
        let empty = Store::default();
        for sort in Sort::ALL {
            assert!(order(&empty, sort).is_empty(), "{sort:?}");
        }
        let one = store_with(serde_json::json!([{"ticker": "SPY", "change_1d": -0.01}]));
        for sort in Sort::ALL {
            assert_eq!(order(&one, sort), vec!["SPY"], "{sort:?}");
        }
    }

    #[test]
    fn the_change_sort_reads_the_stream_and_not_the_polls_number() {
        // The whole reason the key goes through `asset_view`: a quote that
        // arrived since the poll must reorder the grid, or the column the sort
        // heads and the order under it disagree about one row.
        let mut store = store_with(serde_json::json!([
            {"ticker": "AAA", "change_1d": 0.02},
            {"ticker": "BBB", "change_1d": 0.01}
        ]));
        assert_eq!(order(&store, Sort::Change), vec!["AAA", "BBB"]);
        store.apply(
            crate::bus::AppEvent::Sse(crate::bus::SseEvent {
                kind: "quote".into(),
                payload: serde_json::json!({"rows": [
                    {"ticker": "BBB", "price": 10.0, "change_1d": 0.05}
                ]}),
                ts: None,
                id: None,
            }),
            Instant::now(),
        );
        assert_eq!(order(&store, Sort::Change), vec!["BBB", "AAA"]);
    }

    #[test]
    fn the_sort_cycle_visits_every_order_once_and_comes_back() {
        let mut at = Sort::default();
        let mut seen = vec![at];
        for _ in 1..Sort::ALL.len() {
            at = at.next();
            seen.push(at);
        }
        assert_eq!(seen, Sort::ALL.to_vec());
        assert_eq!(at.next(), Sort::default(), "the cycle does not close");
        // Every column a sort heads exists and fits its glyph; the payload's
        // order heads nobody's column, deliberately.
        assert_eq!(Sort::Payload.column(), None);
        for sort in [Sort::Change, Sort::Vol, Sort::Name] {
            let at = sort.column().unwrap();
            let (name, width, _) = COLS[at];
            assert!(
                name.chars().count() < width as usize,
                "{name} is {width} wide and its sort glyph does not fit"
            );
        }
    }

    #[test]
    fn the_wheel_walks_the_cursor_and_a_click_plants_it() {
        // The mouse spelling of the arrow keys: walls at both ends, and a
        // click reads a row back off the rect the last draw published — the
        // header row and the blank under a short universe select nothing.
        let store_json = serde_json::json!([
            {"ticker": "AAA", "change_1d": 0.01},
            {"ticker": "BBB", "change_1d": 0.02},
            {"ticker": "CCC", "change_1d": 0.03}
        ]);
        let mut store = store_with(store_json);
        let mut view = MarketsView::default();
        let mouse = |kind, column, row| MouseEvent {
            kind,
            column,
            row,
            modifiers: crossterm::event::KeyModifiers::NONE,
        };

        view.on_mouse(mouse(MouseEventKind::ScrollDown, 0, 0), &mut store);
        assert_eq!(view.selected(), 1);
        for _ in 0..5 {
            view.on_mouse(mouse(MouseEventKind::ScrollDown, 0, 0), &mut store);
        }
        assert_eq!(view.selected(), 2, "the wheel ran past the last row");
        for _ in 0..5 {
            view.on_mouse(mouse(MouseEventKind::ScrollUp, 0, 0), &mut store);
        }
        assert_eq!(view.selected(), 0, "the wheel ran past the first row");

        // The grid the last frame published: header at y=3, rows under it.
        view.grid.set(Rect::new(9, 3, 56, 10));
        let click = MouseEventKind::Down(MouseButton::Left);
        view.on_mouse(mouse(click, 12, 5), &mut store);
        assert_eq!(view.selected(), 1, "a click on the second row missed");
        view.on_mouse(mouse(click, 12, 3), &mut store);
        assert_eq!(view.selected(), 1, "the header row moved the cursor");
        view.on_mouse(mouse(click, 12, 9), &mut store);
        assert_eq!(view.selected(), 1, "a click past the universe selected");
        view.on_mouse(mouse(click, 80, 5), &mut store);
        assert_eq!(view.selected(), 1, "a click outside the grid selected");
        // A refused grid publishes no rect, and a click lands nowhere.
        view.grid.set(Rect::default());
        view.on_mouse(mouse(click, 12, 5), &mut store);
        assert_eq!(view.selected(), 1);
    }

    #[test]
    fn the_dispersion_is_the_gap_between_the_days_two_ends() {
        let store = store_with(serde_json::json!([
            {"ticker": "UP", "change_1d": 0.0124},
            {"ticker": "MID", "change_1d": 0.0},
            {"ticker": "DOWN", "change_1d": -0.0208}
        ]));
        let spread = dispersion(&store).unwrap();
        assert!((spread - 0.0332).abs() < 1e-12, "{spread}");
        // One marked asset is a gap of zero nobody measured, and none at all
        // is not a flat day.
        assert_eq!(dispersion(&store_with(serde_json::json!([]))), None);
        assert_eq!(
            dispersion(&store_with(
                serde_json::json!([{"ticker": "SPY", "change_1d": 0.01}])
            )),
            None
        );
        assert_eq!(
            dispersion(&store_with(
                serde_json::json!([{"ticker": "SPY"}, {"ticker": "QQQ"}])
            )),
            None,
            "two unmarked assets have no spread"
        );
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

        // The direction is read at the two decimals of a percent the cell's own
        // label prints. A sector at -1e-13 reads `+0.00%` and took the negative
        // pair — the shade contradicting the digits printed on it.
        assert_eq!(format::signed_pct(-1e-13), "+0.00%");
        assert_eq!(heat_style(-1e-13), heat_style(0.0));
        // The neighbour that survives the rounding still takes the other pair,
        // or the rule would have swallowed a real move.
        assert_ne!(heat_style(-0.00005), heat_style(0.00005));
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
                assert_ne!(
                    styles[i],
                    styles[j],
                    "step {} and step {} collide",
                    i + 1,
                    j + 1
                );
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
