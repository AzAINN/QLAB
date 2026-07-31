//! BOOK — the desk's own numbers, stated once, at the top of the view.
//!
//! The ribbon is the workstation's single account of its headline KPIs: what the
//! book is worth, what it is up or down, what the window did, and how it is
//! positioned. Everything under it — the blotter, the holdings heatmap and the
//! equity curve — reads positions and series, never these aggregates. One panel
//! repeating the equity is a second account of it, and two accounts of one
//! number is how a desk ends up trusting neither. The blotter carries no equity,
//! no cash and no total P&L for exactly that reason.
//!
//! The heatmap is a rail beside the curve at the foot of the view rather than
//! beside the blotter, which is where the plan drew it. It cannot go there: the
//! blotter's nine columns need 75 cells and the workstation's baseline frame
//! hands this view 77, so a rail up there would refuse on every terminal anyone
//! actually opens. The rail is 23 cells of the curve's width instead, which the
//! curve can spare and the blotter cannot.
//!
//! Every number comes from `live_portfolio` (the mark-to-market view) and
//! `performance` (the realized-metrics bundle). The registry's own `portfolio`
//! section is deliberately *not* read: the owner computes two P&L views and
//! documents that they must never be shown under one label, because their
//! disagreement is a reconciliation finding rather than a display choice.
//!
//! The ribbon lives here rather than in `ui/widgets/` by the rule that file
//! states: a widget moves there once a *second* view needs it. Task 14's equity
//! tile is the likely second caller, and lifting it then is a rename; lifting it
//! now would be a shared seam with one user and no second opinion about its
//! shape.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::{FlashKey, FlashTracker};
use crate::model::{EquityPoint, LivePortfolio, Metrics, Performance, Position};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
// The ribbon is one panel — one rule under a band of four cells — so it takes
// `panel_block` and heads its cells with `label` rather than `panel_header`:
// four amber bars across one band would read as four panels.
use crate::ui::widgets::table_cell::{cell, head, LEFT, RIGHT};
use crate::ui::widgets::tristate_spark::{self, SPARK_W};
use crate::ui::widgets::{braille_chart, heat_cell, panel_block, panel_header, refuse};
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Row, Table},
    Frame,
};
use std::cell::Cell;
use std::cmp::Ordering;
use std::time::Instant;
use unicode_width::UnicodeWidthStr;

/// Label row, hero row, sub row, and the rule the block reserves.
///
/// Three rows is the whole budget: a fourth would push the blotter below the
/// fold on a 24-row terminal, which is the height a laptop half-screen gives.
const RIBBON_H: u16 = 4;

/// The share of the ribbon each cell takes, as parts of a hundred.
///
/// The risk block is the widest because it carries six numbers to the others'
/// two; the value cell is next because money is the widest thing on the row.
const CELL_RATIOS: [u32; 4] = [28, 20, 16, 36];

/// What each cell needs before it is drawing rather than guessing.
///
/// Derived from the widest *shape* each cell renders, not from today's numbers:
///
/// - value: `2 pos · cash $999.99K` — the sub-line, which is longer than both
///   the `PORTFOLIO VALUE` label and a seven-figure equity hero.
/// - P&L: the `UNREALIZED P&L` label. Longer than `+12.34% ▲9 ▼9`, and the
///   label is the part that may not shrink: a sub-line past its cell drops a
///   whole chip (`sub_line`), while a truncated header is a column an operator
///   has to guess at.
/// - window: `-12.34%`, the widest hero over a five-character label.
/// - risk: two chip columns and the cell between them (see `CHIP_W`).
const CELL_MINS: [u16; 4] = [21, 14, 7, CHIP_W * 2 + 1];

/// One risk chip: a six-cell label (`SHARPE`, `CVAR95`) and a right-aligned
/// seven-cell value (`-100.0%` is the widest a net exposure or a drawdown gets).
const CHIP_W: u16 = 13;

/// The label column inside a chip.
const CHIP_LABEL_W: usize = 6;

/// The narrowest pane at which every cell clears `CELL_MINS`.
///
/// Not `max(min / ratio)`: the ratios are solved, not multiplied, and the solver
/// rounds a cell up as often as down — spelling the floor as that arithmetic
/// puts it at 75, a column away from where the refusal actually bites. A test
/// walks the real split instead and pins both sides of this boundary, so a
/// ratatui that solves differently fails there rather than in the field, where
/// it would cost a digit off a percentage.
const RIBBON_W: u16 = 74;

/// The rows the footer takes from the blotter's share.
///
/// Ten: the curve's header and nine rows of canvas, which is also the rail's
/// header, six rows of holdings and the three its movers need. A shorter band
/// would put the curve under its own four-row floor at the workstation's
/// baseline height, and a chart that refuses on every terminal is not a chart.
const BOTTOM_H: u16 = 10;

/// Where the operator is looking in the blotter, the heatmap and the curve.
/// Never what the desk says — that is the `Store`'s, and a view that held a copy
/// would be a second account of it.
#[derive(Default)]
pub struct BookView {
    sort: Sort,
    /// Which quantity the holdings rail is shaded by, and which slice of the
    /// series the curve draws. Both are where the operator is looking, so both
    /// live here and survive a trip through another view.
    heat: Heat,
    period: Period,
    /// The first blotter row on screen, as an index into the *sorted* rows.
    ///
    /// The scroll position is kept as a row and not as a page number: a stored
    /// page multiplied by a page size that just changed lands the operator
    /// somewhere they never scrolled to, while a row index survives the resize
    /// and the page it falls on is recomputed from it.
    top: usize,
    selected: usize,
    /// How many rows the last frame actually gave the blotter's body.
    ///
    /// A `Cell` because the page size is an *allocation* and only the draw
    /// knows it, while `on_key` is where a page turn happens. It records
    /// geometry, never anything the operator set — drawing the same frame twice
    /// records the same number, which is what keeps a repaint from moving the
    /// cursor.
    page_rows: Cell<usize>,
}

impl View for BookView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant) {
        let rows = Layout::vertical([Constraint::Length(RIBBON_H), Constraint::Min(0)]).split(area);
        draw_ribbon(f, rows[0], store);
        // The band's height is arithmetic rather than a constraint, so the
        // blotter keeps its floor and the footer is what shrinks. Handed to the
        // solver as two constraints, a short pane resolves in the solver's
        // favour and not the desk's: the footer would survive by pushing the
        // book itself off the screen, which is a trade-off nobody chose.
        let bottom = BOTTOM_H.min(rows[1].height.saturating_sub(BLOTTER_H));
        let under =
            Layout::vertical([Constraint::Min(0), Constraint::Length(bottom)]).split(rows[1]);
        self.draw_blotter(f, under[0], store, fx, now);
        self.draw_footer(f, under[1], store);
    }

    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        let rows = row_count(store);
        // At least one, so a key pressed before the first frame moves by a row
        // rather than dividing by zero. In the runtime the loop draws first.
        let page = self.page_rows.get().max(1);
        match k.code {
            // Cycling the column resets the scroll and the cursor: after a
            // re-sort, row 4 is a different position, and carrying the index
            // would leave the marker on a row the operator did not choose.
            KeyCode::Char('s') => {
                self.sort = self.sort.next();
                self.top = 0;
                self.selected = 0;
            }
            KeyCode::Char(']') => self.page_to(self.top + page, rows, page),
            KeyCode::Char('[') => self.page_to(self.top.saturating_sub(page), rows, page),
            // Neither touches the blotter: the rail shades the same rows the
            // blotter orders, and the curve is a different section entirely.
            KeyCode::Char('h') => self.heat = self.heat.next(),
            KeyCode::Char('p') => self.period = self.period.next(),
            // Both ends are walls, not wraps, exactly as the markets grid: an
            // operator holding an arrow must land on the first or last row,
            // never at the other end of a book they did not scroll to.
            KeyCode::Up => self.select(self.cursor(rows).saturating_sub(1), page),
            KeyCode::Down => self.select((self.cursor(rows) + 1).min(rows.saturating_sub(1)), page),
            _ => return None,
        }
        None
    }
}

impl BookView {
    /// The cursor, clamped to the book actually on screen.
    ///
    /// The book can shrink between the keystroke that moved the cursor and the
    /// frame that draws it — the owner closing a position does exactly that —
    /// and an index past the end must render the last row rather than panic.
    fn cursor(&self, rows: usize) -> usize {
        self.selected.min(rows.saturating_sub(1))
    }

    /// The first visible row: the scroll, clamped to the last page's own first
    /// row.
    ///
    /// The last page start rather than `rows - 1`, because the owner closing
    /// positions under the cursor must leave the remaining book on screen — a
    /// clamp to the last *row* would answer a shrunk book with one row and the
    /// rest of the pane blank. It is also what keeps a preserved top honest
    /// after a resize: a top inside the book is never past this bound unless the
    /// page grew, and then it is the page it grew into.
    fn scroll(&self, rows: usize, page: usize) -> usize {
        let page = page.max(1);
        self.top.min(rows.saturating_sub(1) / page * page)
    }

    /// Move the cursor, dragging the page along only as far as it has to come.
    fn select(&mut self, row: usize, page: usize) {
        self.selected = row;
        if row < self.top {
            self.top = row;
        } else if row >= self.top + page {
            self.top = row + 1 - page;
        }
    }

    /// Turn to the page starting at `top`, and put the cursor on its first row.
    ///
    /// Clamped to the last page's own first row, and never backwards: after a
    /// resize the scroll can sit past that boundary (the top row is preserved,
    /// the page boundaries are not), and a forward key that moved the operator
    /// up the book would be worse than one that did nothing.
    fn page_to(&mut self, top: usize, rows: usize, page: usize) {
        let last = rows.saturating_sub(1) / page * page;
        self.top = top.min(last.max(self.top));
        self.selected = self.top;
    }

    /// Which sort column is live, for the registry's own tests.
    ///
    /// `cfg(test)` rather than a plain accessor: the draw path reads these off
    /// `self`, and a second reader in the runtime would be a copy of the cursor
    /// that could disagree with the one on screen.
    #[cfg(test)]
    pub(crate) fn sort(&self) -> Sort {
        self.sort
    }

    #[cfg(test)]
    pub(crate) fn top(&self) -> usize {
        self.top
    }

    #[cfg(test)]
    pub(crate) fn heat(&self) -> Heat {
        self.heat
    }

    #[cfg(test)]
    pub(crate) fn period(&self) -> Period {
        self.period
    }
}

/// The four cells, or the reason there are none.
fn draw_ribbon(f: &mut Frame, area: Rect, store: &Store) {
    // Below `RIBBON_H` the vertical split hands back what there is, and a
    // `Paragraph` clipped from the bottom loses its last line without saying so
    // — here that is the sub row, where the cash split, the gainers and the
    // window's basis live. Guarded on the allocation for the same reason the
    // width is: what was asked for is not what was given.
    if area.height < RIBBON_H {
        refuse(
            f,
            area,
            format!(
                "book ribbon needs {RIBBON_H} rows for its labels, values and sub-lines — \
                 this pane has {}, make the terminal taller",
                area.height
            ),
        );
        return;
    }

    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);

    let cells = split(inner);
    // Split first, then guard each *allocation*. The pane is not the cell, and
    // here the two disagree even about whether the ribbon fits: the cells need
    // 69 columns between them, so at 73 a guard on the pane sees four columns of
    // slack — while the ratios have already starved the value cell and the risk
    // block by one each. A `Paragraph` drops what does not fit off the right, so
    // that column is `$10,000.0` and `100.0`: a fully-invested book drawn as a
    // hundredth of one, which is the reading this refusal exists to prevent.
    if let Some(short) = cells
        .iter()
        .zip(CELL_MINS)
        .position(|(cell, min)| cell.width < min)
    {
        refuse(
            f,
            inner,
            format!(
                "book ribbon needs {RIBBON_W} columns to state the book — this pane has {}, \
                 which leaves the {} cell {} of the {} it needs; widen the terminal",
                inner.width, CELL_LABELS[short], cells[short].width, CELL_MINS[short],
            ),
        );
        return;
    }

    let snapshot = store.snapshot.as_ref();
    let book = snapshot.and_then(|s| s.live_portfolio.as_ref());
    let performance = snapshot.and_then(|s| s.performance.as_ref());
    let metrics = performance.and_then(|p| p.metrics.as_ref());

    // The halt comes off the store rather than off the section this cell reads:
    // it is a desk rule (the live book decides, the reconciled book answers when
    // the live one has not been marked), and a view that re-derived it would be
    // a second source for the fact the glyph in the rail is already animating.
    draw_value(f, cells[0], book, store.halted().unwrap_or(false));
    draw_pnl(f, cells[1], book);
    draw_window(f, cells[2], performance);
    draw_risk(f, cells[3], book, metrics);
}

/// The cells of a ribbon `width` wide, in `CELL_RATIOS` order.
fn split(area: Rect) -> std::rc::Rc<[Rect]> {
    Layout::horizontal(CELL_RATIOS.map(|share| Constraint::Ratio(share, 100))).split(area)
}

/// The names the refusal uses for the cells, and the labels three of them draw.
const CELL_LABELS: [&str; 4] = ["PORTFOLIO VALUE", "UNREALIZED P&L", "WINDOW", "RISK"];

/// PORTFOLIO VALUE — the equity, and what it is made of.
fn draw_value(f: &mut Frame, area: Rect, book: Option<&LivePortfolio>, halted: bool) {
    let t = theme();
    let dim = Style::default().fg(t.text_tertiary);
    let positions = book.map(|b| b.positions.len());
    let cash = book.and_then(|b| b.cash);

    // A halted or blocked book renders exactly like a working one otherwise —
    // same equity, same positions — so the state has to be said rather than
    // implied. It takes the sub-line whole rather than sharing it: at the floor
    // the cash split does not fit beside the word, and of the two the word is
    // the one an operator must not miss. Task 15 gives the halt its motion
    // treatment; this is the fact it will animate.
    let halt = if halted {
        Some("HALTED")
    } else if book.is_some_and(|b| b.blocked == Some(true)) {
        // Only the live book can be blocked: it is the report that goes empty
        // when the owner refuses to value a desk on prices it could not fetch,
        // and every cell around this word is honestly `--` because of it.
        Some("BLOCKED")
    } else {
        None
    };
    let sub = match halt {
        Some(word) => Line::from(Span::styled(
            word,
            Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
        )),
        None => sub_line(
            vec![
                Span::styled(
                    format!("{} pos", or_missing(positions.map(|n| n.to_string()))),
                    dim,
                ),
                Span::styled(
                    format!("cash {}", or_missing(cash.map(format::compact_money))),
                    dim,
                ),
            ],
            " · ",
            area.width,
        ),
    };

    f.render_widget(
        Paragraph::new(vec![
            label(CELL_LABELS[0]),
            // Amber, the workstation's one theme-defining colour, spent on the
            // number every other number on this view is a share of.
            hero(
                or_missing(book.and_then(|b| b.equity).map(format::money)),
                t.accent,
            ),
            sub,
        ]),
        area,
    );
}

/// UNREALIZED P&L — the open P&L, its return on cost, and the breadth under it.
fn draw_pnl(f: &mut Frame, area: Rect, book: Option<&LivePortfolio>) {
    let t = theme();
    let pnl = book.and_then(|b| b.unrealized_pnl);
    let counts = book.map(|b| gainers_and_losers(&b.positions));
    let (gainers, losers) = match counts {
        Some((up, down)) => (up.to_string(), down.to_string()),
        None => (MISSING.to_string(), MISSING.to_string()),
    };

    f.render_widget(
        Paragraph::new(vec![
            label(CELL_LABELS[1]),
            hero(
                or_missing(pnl.map(format::signed_money)),
                // At the two decimals `signed_money` prints: a cash P&L of
                // -1e-13 renders `+$0.00` and must not be red beside it.
                pnl.map(|v| format::change_tone(v, 2))
                    .unwrap_or(t.text_secondary),
            ),
            // A single space between the chips rather than the value cell's
            // ` · `: at the baseline width this cell is fifteen columns, and the
            // two cells the separator would cost are a two-digit breadth on a
            // ten-name book — the common case, not the extreme one.
            sub_line(
                vec![
                    Span::styled(
                        or_missing(book.and_then(return_on_cost).map(format::signed_pct)),
                        Style::default().fg(t.text_tertiary),
                    ),
                    Span::styled(format!("▲{gainers}"), Style::default().fg(t.positive)),
                    Span::styled(format!("▼{losers}"), Style::default().fg(t.negative)),
                ],
                " ",
                area.width,
            ),
        ]),
        area,
    );
}

/// WINDOW — what the equity curve did across the marks it is drawn from.
///
/// Not `TODAY`, which is what the plan called this cell. The owner's
/// `window_change` is measured across the whole charted window — up to 365 daily
/// marks (`_CHART_DAYS` in `qlab/ui/server.py`) — and dated from the first of
/// them, precisely so it can never be read off a point outside the chart. A cell
/// headed TODAY would state a day's move the number is not about, which is worse
/// than not stating it: the sub-line says how many marks it spans instead.
fn draw_window(f: &mut Frame, area: Rect, performance: Option<&Performance>) {
    let t = theme();
    let change = performance.and_then(|p| p.window_change);
    // The series the chart draws, which is the same window the change was
    // measured over — `performance.marks` counts the raw marks behind it, a
    // larger number that would say the change spans more than it does.
    let marks = performance.map(|p| p.series.len());

    f.render_widget(
        Paragraph::new(vec![
            label(CELL_LABELS[2]),
            hero(
                or_missing(change.map(format::signed_pct)),
                // At the two decimals of a percent `signed_pct` prints.
                change
                    .map(|v| format::change_tone(v * 100.0, 2))
                    .unwrap_or(t.text_secondary),
            ),
            Line::from(Span::styled(
                format!("{} marks", or_missing(marks.map(|n| n.to_string()))),
                Style::default().fg(t.text_tertiary),
            )),
        ]),
        area,
    );
}

/// The risk and positioning block: six chips, in three labelled pairs.
///
/// Three rows of two rather than the two rows of three the plan drew, because at
/// the workstation's baseline width this cell is 28 cells and the six full
/// labels do not fit across three columns — `GROSS 100.0%`, `NET 100.0%` and
/// `CVAR95 -2.3%` alone are 34. The pairs are the plan's pairs either way, and a
/// chip labelled `GRS` is a number an operator has to decode.
fn draw_risk(f: &mut Frame, area: Rect, book: Option<&LivePortfolio>, metrics: Option<&Metrics>) {
    // Read across a row for the plan's pairs — realized against positioning —
    // and down a column for what the desk earned against how it is standing.
    let columns = [
        vec![
            // A Sharpe is the one number here that carries no unit, so it is the
            // one number rendered as a bare ratio.
            chip("SHARPE", metrics.and_then(|m| m.sharpe).map(ratio)),
            chip("VOL", metrics.and_then(|m| m.ann_vol).map(format::pct1)),
            chip(
                "MDD",
                metrics.and_then(|m| m.max_drawdown).map(format::pct1),
            ),
        ],
        vec![
            chip(
                "GROSS",
                book.and_then(|b| b.gross_exposure).map(format::pct1),
            ),
            chip("NET", book.and_then(|b| b.net_exposure).map(format::pct1)),
            chip("CVAR95", metrics.and_then(|m| m.cvar_95).map(format::pct1)),
        ],
    ];

    let cols = Layout::horizontal([Constraint::Length(CHIP_W); 2])
        .spacing(1)
        .split(area);
    for (col, chips) in cols.iter().zip(columns) {
        f.render_widget(Paragraph::new(chips), *col);
    }
}

/// One chip: a dim label, then the value pushed to the far side of the column.
///
/// Right-aligned so a column of chips reads as a column — the same reason the
/// markets grid right-aligns its numbers — and padded rather than truncated,
/// since the cell is guarded to a width every value fits in.
fn chip(name: &str, value: Option<String>) -> Line<'static> {
    let t = theme();
    let value = value.unwrap_or_else(|| MISSING.to_string());
    let pad = (CHIP_W as usize - CHIP_LABEL_W).saturating_sub(value.width());
    Line::from(vec![
        Span::styled(
            format!("{name:<CHIP_LABEL_W$}"),
            Style::default().fg(t.text_secondary),
        ),
        Span::raw(" ".repeat(pad)),
        Span::styled(value, Style::default().fg(t.text_primary)),
    ])
}

/// A sub-line, dropping whole chips rather than digits when it outgrows its cell.
///
/// `Paragraph` clips from the right, so a cash figure one column past its cell
/// renders `$6.82` where the owner said `$6.82K` — off by a thousand — and a
/// breadth of `▼10` renders `▼1`. Both are numbers that are wrong, which is the
/// one thing worse than a number that is absent. The cells are sized for the
/// shapes in `CELL_MINS`; this is what happens past them — a three-figure
/// position count, a book up more than 100% on cost — and the chips are ordered
/// so that what goes is what the desk can most afford to lose.
fn sub_line(chips: Vec<Span<'static>>, separator: &str, width: u16) -> Line<'static> {
    let mut chips = chips;
    while chips.len() > 1 && line_width(&chips, separator) > width as usize {
        chips.pop();
    }
    let sep = Span::styled(
        separator.to_string(),
        Style::default().fg(theme().text_tertiary),
    );
    let mut spans: Vec<Span<'static>> = Vec::with_capacity(chips.len() * 2);
    for (i, chip) in chips.into_iter().enumerate() {
        if i > 0 {
            spans.push(sep.clone());
        }
        spans.push(chip);
    }
    Line::from(spans)
}

/// The cells `chips` would occupy, separators included.
fn line_width(chips: &[Span<'static>], separator: &str) -> usize {
    let content: usize = chips.iter().map(|c| c.content.width()).sum();
    content + separator.width() * chips.len().saturating_sub(1)
}

/// A rendered value, or the one spelling of a value there is none of.
fn or_missing(value: Option<String>) -> String {
    value.unwrap_or_else(|| MISSING.to_string())
}

/// A ratio at two decimals — a Sharpe, and nothing else so far.
fn ratio(value: f64) -> String {
    if !value.is_finite() {
        return MISSING.to_string();
    }
    format!("{value:.2}")
}

/// A cell's label: the same uppercase, one ramp step down from the value under
/// it. Not `panel_header`: four amber bars across one band would read as four
/// panels, and the ribbon is one.
fn label(text: &str) -> Line<'static> {
    Line::from(Span::styled(
        text.to_uppercase(),
        Style::default().fg(theme().text_secondary),
    ))
}

/// A cell's headline number.
fn hero(text: String, tone: ratatui::style::Color) -> Line<'static> {
    Line::from(Span::styled(
        text,
        Style::default().fg(tone).add_modifier(Modifier::BOLD),
    ))
}

/// How many positions are up and how many are down.
///
/// Flat is neither, exactly as the pulse rail counts the breadth of a tape: a
/// position that has not moved is not a gainer, and counting it as one would
/// tilt every quiet book green. A position the owner sent no P&L for is not
/// counted at all — it is not flat, it is unmeasured.
///
/// Flat at the two decimals the blotter's own P&L cell prints, so the two panes
/// cannot disagree about one row: a position drawn `+$0.00` in the neutral tone
/// and counted as a decliner in the ribbon above it is the same desk saying both.
fn gainers_and_losers(positions: &[Position]) -> (usize, usize) {
    positions
        .iter()
        .filter_map(|p| p.unrealized_pnl)
        .filter(|pnl| pnl.is_finite())
        .fold((0, 0), |(up, down), pnl| {
            if format::zero_at(pnl, 2) {
                (up, down)
            } else if format::negative_at(pnl, 2) {
                (up, down + 1)
            } else {
                (up + 1, down)
            }
        })
}

/// The book's open P&L as a return on what it paid.
///
/// On cost rather than on equity: `+$40` beside `+10%` says the positions are up
/// a tenth of what they cost, which is what a P&L percentage means everywhere
/// else on a desk. Against equity the same book would read `+4%` and quietly
/// mean something different — how much of the *book* the gain is — which is a
/// second fact, not a clearer version of the first.
///
/// The cost basis is the owner's own: `qty × avg_price` per position, the
/// denominator it divides each position's `unrealized_pnl_pct` by. Gross, so a
/// short and a long cannot cancel into a denominator near zero.
fn return_on_cost(book: &LivePortfolio) -> Option<f64> {
    let pnl = book.unrealized_pnl?;
    let cost: f64 = book
        .positions
        .iter()
        .filter_map(|p| Some((p.qty? * p.avg_price?).abs()))
        .sum();
    // A book with no cost basis has no return on one. Zero would render `+0.00%`
    // beside a non-zero P&L, which is a division nobody did.
    (cost > 0.0).then(|| pnl / cost)
}

// -- the blotter -----------------------------------------------------------

/// The nine columns: title, the cells each needs at its widest rendering, and
/// whether its contents are pushed right.
///
/// Nine of the reference blotter's eleven. `COST BASIS` and `CHG%` are not
/// here because the owner's `Position` carries neither (`model::Position`): a
/// cost basis is `qty × avg_price`, a number this client would be *computing*
/// and then stating under the owner's name, and a day change on a position is a
/// market fact the position row has no field for at all. `AVG` and `TREND` sit
/// in their places, and both are things the owner did send.
///
/// The widths are the widest *shape*, not today's numbers. `SYMBOL` is seven —
/// a cursor marker and a six-cell ticker, six being what the header itself
/// needs plus the sort glyph. `MKTVAL` and `P&L` are nine because that is what
/// `compact_money` and `signed_compact_money` are bounded at, and the whole
/// reason those two exist rather than `money`: an unbounded figure in a sized
/// column is a number that is wrong rather than one that is coarse.
///
/// Right for every number, because a column of numbers only reads as a column
/// when the decimal points line up; left for `SYMBOL`, which is a name.
const BLOTTER_COLS: [(&str, u16, bool); 9] = [
    ("SYMBOL", 7, LEFT),
    ("QTY", 7, RIGHT),
    ("LAST", 7, RIGHT),
    ("AVG", 7, RIGHT),
    ("WT%", 6, RIGHT),
    ("MKTVAL", 9, RIGHT),
    ("P&L", 9, RIGHT),
    ("P&L%", 7, RIGHT),
    ("TREND", SPARK_W as u16, RIGHT),
];

/// The blotter's floor, derived from the columns so the two cannot drift.
const BLOTTER_W: u16 = blotter_w();

const fn blotter_w() -> u16 {
    // Every column plus the cell of spacing that follows it, minus the one
    // after the last. The cursor marker takes no column of its own: it rides
    // inside `SYMBOL`, which is what buys the ninth column its width back.
    let mut w = 0;
    let mut i = 0;
    while i < BLOTTER_COLS.len() {
        w += BLOTTER_COLS[i].1 + 1;
        i += 1;
    }
    w - 1
}

/// The pane a view is handed at the workstation's baseline width: 120 cells,
/// less both rails and the rule the content column reserves.
const BASELINE_PANE: u16 = 120 - crate::ui::shell::NAV_W - crate::ui::shell::PULSE_W - 1;

/// The floor has to fit the frame the workstation is designed for, or the
/// blotter refuses to draw on every terminal anyone actually opens it on.
///
/// Asserted at compile time rather than in a test: this is a fact about whether
/// the view can exist at all, and it should fail `cargo build` rather than one
/// test somebody could mark ignored.
const _: () = assert!(BLOTTER_W <= BASELINE_PANE);

/// The panel header (which carries the pager), the column header, and one row.
///
/// A blotter with no room for a row is two headers over nothing, which reads as
/// a book holding no positions — the one thing this pane must not say when what
/// is actually short is the terminal.
const BLOTTER_H: u16 = 3;

/// Which column the rows are ordered by.
///
/// A subset of the nine rather than all of them: these are the five an operator
/// re-sorts a blotter to answer — where is the money, what is winning, what is
/// losing, and where is a name I know. `QTY`, `LAST` and `AVG` order a book by
/// nothing anyone asks about.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Sort {
    #[default]
    Weight,
    MktVal,
    Pnl,
    PnlPct,
    Symbol,
}

impl Sort {
    /// The cycle `s` walks, in the order it walks it.
    const ALL: [Sort; 5] = [
        Sort::Weight,
        Sort::MktVal,
        Sort::Pnl,
        Sort::PnlPct,
        Sort::Symbol,
    ];

    fn next(self) -> Sort {
        let at = Sort::ALL.iter().position(|s| *s == self).unwrap_or(0);
        Sort::ALL[(at + 1) % Sort::ALL.len()]
    }

    /// The column this sort heads, as an index into `BLOTTER_COLS`.
    fn column(self) -> usize {
        match self {
            Sort::Symbol => 0,
            Sort::Weight => 4,
            Sort::MktVal => 5,
            Sort::Pnl => 6,
            Sort::PnlPct => 7,
        }
    }

    /// Direction is fixed per column rather than toggled: `s` is one key, and a
    /// second key for the direction is a control nobody asked for. Money sorts
    /// biggest-first because that is the question — *where is the exposure* —
    /// and a name sorts A-to-Z because that is how a name is looked up.
    fn descending(self) -> bool {
        self != Sort::Symbol
    }
}

/// One rendered position: the numbers to sort on, kept beside nothing at all.
///
/// The display strings are built at render time *from* these, never parsed back
/// out of them. A blotter that sorted its own text puts `$2.50M` under
/// `$999.00K`, which is the ordering an operator would act on last.
struct BlotterRow<'a> {
    /// `None` is a position the owner sent with no ticker — a broken contract,
    /// and still a row: dropping it would leave the blotter's count disagreeing
    /// with the ribbon's `N pos`, which is the sort of gap nobody reconciles.
    ticker: Option<&'a str>,
    qty: Option<f64>,
    last: Option<f64>,
    avg: Option<f64>,
    weight: Option<f64>,
    value: Option<f64>,
    pnl: Option<f64>,
    pnl_pct: Option<f64>,
    /// The asset's closes, or `None` for a ticker the market section does not
    /// carry at all. The two are different facts; see `tristate_spark`.
    history: Option<&'a [f64]>,
}

impl BlotterRow<'_> {
    fn key(&self, sort: Sort) -> Option<f64> {
        match sort {
            Sort::Weight => self.weight,
            Sort::MktVal => self.value,
            Sort::Pnl => self.pnl,
            Sort::PnlPct => self.pnl_pct,
            // Not a number, and never compared as one.
            Sort::Symbol => None,
        }
    }
}

/// Every position the live book carries, resolved against what the desk knows.
///
/// The live book rather than the registry's, for the same reason the ribbon
/// reads it: the two are different views of one desk, and their disagreement is
/// a reconciliation finding rather than a display choice.
fn blotter_rows(store: &Store) -> Vec<BlotterRow<'_>> {
    let facts = store.asset_facts();
    let positions = store
        .snapshot
        .as_ref()
        .and_then(|s| s.live_portfolio.as_ref())
        .map(|b| b.positions.as_slice())
        .unwrap_or_default();

    positions
        .iter()
        .map(|p| {
            let ticker = format::text(p.ticker.as_ref());
            // `asset_view` is the only reader of a price in this client — a row
            // that read `position.price` would render the poll's number and
            // silently lose every quote that arrived since. The position's own
            // mark answers only when the market section does not carry the
            // ticker: that is a book held outside the polled universe, where the
            // overlay has nothing to be fresher *than*, and a `--` beside a
            // stated market value would read as a value with no price.
            let quoted = ticker.and_then(|t| store.asset_view(t).price);
            BlotterRow {
                ticker,
                qty: p.qty,
                last: quoted.or(p.price),
                avg: p.avg_price,
                weight: p.weight,
                value: p.value,
                pnl: p.unrealized_pnl,
                pnl_pct: p.unrealized_pnl_pct,
                history: ticker
                    .and_then(|t| facts.iter().find(|a| a.ticker == t).map(|a| a.history)),
            }
        })
        .collect()
}

/// How many rows the blotter would draw.
///
/// The count without the order: a key path that sorted the whole book to ask
/// how long it is would do a frame's work for a number `len` already has. One
/// row per position by construction — including the one the owner sent with no
/// ticker — so this and `blotter_rows` cannot disagree.
fn row_count(store: &Store) -> usize {
    store
        .snapshot
        .as_ref()
        .and_then(|s| s.live_portfolio.as_ref())
        .map_or(0, |b| b.positions.len())
}

/// The rows in the order `sort` asks for.
///
/// `sort_by` is stable, and that is load-bearing rather than incidental: a book
/// the owner sent at one weight must keep the owner's order, or the blotter
/// reshuffles itself on a repaint. A value the owner did not send sorts last
/// whichever way the column runs — absent is not zero, and a `--` at the head of
/// a heaviest-first list is the one row an operator would read as the answer.
fn sorted<'a>(mut rows: Vec<BlotterRow<'a>>, sort: Sort) -> Vec<BlotterRow<'a>> {
    if sort == Sort::Symbol {
        rows.sort_by(|a, b| match (a.ticker, b.ticker) {
            (Some(a), Some(b)) => a.cmp(b),
            (Some(_), None) => Ordering::Less,
            (None, Some(_)) => Ordering::Greater,
            (None, None) => Ordering::Equal,
        });
        return rows;
    }
    let descending = sort.descending();
    rows.sort_by(|a, b| match (a.key(sort), b.key(sort)) {
        (Some(a), Some(b)) => {
            let order = a.partial_cmp(&b).unwrap_or(Ordering::Equal);
            if descending {
                order.reverse()
            } else {
                order
            }
        }
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    });
    rows
}

impl BookView {
    /// The blotter: a panel header, the grid, and the pager under it.
    fn draw_blotter(
        &self,
        f: &mut Frame,
        area: Rect,
        store: &Store,
        fx: &FlashTracker,
        now: Instant,
    ) {
        if area.height < BLOTTER_H {
            refuse(
                f,
                area,
                format!(
                    "positions blotter needs {BLOTTER_H} rows for its headers and a \
                     position — this pane has {}, make the terminal taller",
                    area.height
                ),
            );
            return;
        }
        // Below the floor `Table` shrinks its columns underneath the widths
        // every cell was held to, and a right-aligned cell then loses its
        // *leading* characters — the sign first. `head` cannot see it: it is
        // spent against the declared width, and the allocation is what shrank.
        // So the blotter refuses rather than drawing numbers that are wrong.
        if area.width < BLOTTER_W {
            refuse(
                f,
                area,
                format!(
                    "positions blotter needs {BLOTTER_W} columns for its nine — this pane has \
                     {}, widen the terminal",
                    area.width
                ),
            );
            return;
        }

        let rows = Layout::vertical([
            Constraint::Length(1), // the panel header, and the pager on its far side
            Constraint::Min(0),    // the column header and the rows under it
        ])
        .split(area);

        // The allocation, not the pane it came from: sized off the frame the
        // page would be eleven rows too long and the pager would claim there
        // was only one. The pager rides on the header row rather than taking a
        // footer of its own for the same reason — a footer's row would have to
        // be reserved whether or not the pager used it, which makes the page
        // size depend on whether the pager was drawn, which depends on the page
        // size.
        let page = rows[1].height.saturating_sub(1) as usize;
        self.page_rows.set(page);

        // Built from the store each frame rather than cached: a cached list is
        // a second account of the book, and it would go stale between the poll
        // that closed a position and the frame that still drew it.
        let all = sorted(blotter_rows(store), self.sort);
        let top = self.scroll(all.len(), page);
        draw_header(f, rows[0], top, page, all.len());
        if all.is_empty() {
            // Which kind of nothing it is. An owner that sent no live book at
            // all and a book that genuinely holds nothing are different facts,
            // and one sentence for both would hide a broken poll.
            let said = match store
                .snapshot
                .as_ref()
                .and_then(|s| s.live_portfolio.as_ref())
            {
                Some(_) => "the live book holds no positions",
                None => "no live book in the last snapshot",
            };
            f.render_widget(
                Paragraph::new(Line::from(Span::styled(
                    said,
                    Style::default().fg(theme().text_tertiary),
                ))),
                rows[1],
            );
            return;
        }

        let cursor = self.cursor(all.len());
        let table = Table::new(
            all[top..(top + page).min(all.len())]
                .iter()
                .enumerate()
                .map(|(i, row)| self.draw_row(row, top + i == cursor, fx, now)),
            BLOTTER_COLS.map(|(_, w, _)| Constraint::Length(w)),
        )
        .header(self.header())
        .column_spacing(1);
        f.render_widget(table, rows[1]);
    }

    /// The column header, with the sort glyph on the column that is live.
    ///
    /// A grid that reorders itself with nothing on screen saying why is a grid
    /// an operator stops trusting. The glyphs are `▴`/`▾` rather than the
    /// `▲`/`▼` the change columns use: a sort direction and a price direction
    /// are different claims and must not share a mark.
    fn header(&self) -> Row<'static> {
        let live = self.sort.column();
        let arrow = if self.sort.descending() { "▾" } else { "▴" };
        Row::new(
            BLOTTER_COLS
                .iter()
                .enumerate()
                .map(|(i, (name, width, right))| {
                    let title = if i == live {
                        format!("{name}{arrow}")
                    } else {
                        name.to_string()
                    };
                    cell(title, Style::default(), *right, *width)
                })
                .collect::<Vec<_>>(),
        )
        .style(Style::default().fg(theme().text_secondary))
    }

    /// One position.
    fn draw_row(
        &self,
        row: &BlotterRow<'_>,
        selected: bool,
        fx: &FlashTracker,
        now: Instant,
    ) -> Row<'static> {
        let t = theme();
        // One decision, one input. Both money cells answer "did this position
        // make money", so both take their tone from the P&L itself — a percent
        // coloured off its own sign would be a second axis that can disagree
        // with the first, and a green `+1.20%` beside a red `-$4.00` is a row
        // that says both.
        let pnl_tone = pnl_tone(row.pnl);
        let (trend, trend_style) = tristate_spark::tristate(row.history);
        let marker = if selected { "▌" } else { " " };

        // In `BLOTTER_COLS` order and zipped with it below, so a cell and the
        // column it is held to cannot drift apart.
        let cells: [(String, Style); BLOTTER_COLS.len()] = [
            (
                format!("{marker}{}", row.ticker.unwrap_or(MISSING)),
                Style::default().fg(t.cyan).add_modifier(Modifier::BOLD),
            ),
            (
                row.qty.map(qty).unwrap_or_else(|| MISSING.to_string()),
                Style::default().fg(t.text_secondary),
            ),
            (
                row.last
                    .map(format::price)
                    .unwrap_or_else(|| MISSING.to_string()),
                match row.ticker {
                    // The same key the tape lights, because it is the same
                    // fact: one quote moves the price wherever it is drawn.
                    Some(ticker) => fx.style_for(
                        &FlashKey::price(ticker),
                        now,
                        Style::default().fg(t.text_primary),
                    ),
                    None => Style::default().fg(t.text_primary),
                },
            ),
            (
                row.avg
                    .map(format::price)
                    .unwrap_or_else(|| MISSING.to_string()),
                Style::default().fg(t.text_secondary),
            ),
            (
                format::opt_pct(row.weight),
                Style::default().fg(t.text_secondary),
            ),
            (
                row.value
                    .map(format::compact_money)
                    .unwrap_or_else(|| MISSING.to_string()),
                // Amber: the market value is what every other cell on the row
                // is a fact about, the same spend the ribbon makes on equity.
                Style::default().fg(t.warning),
            ),
            (
                row.pnl
                    .map(format::signed_compact_money)
                    .unwrap_or_else(|| MISSING.to_string()),
                Style::default().fg(pnl_tone),
            ),
            (
                row.pnl_pct
                    .map(format::signed_pct)
                    .unwrap_or_else(|| MISSING.to_string()),
                Style::default().fg(pnl_tone),
            ),
            (trend, trend_style),
        ];

        let row = Row::new(
            cells
                .into_iter()
                .zip(BLOTTER_COLS)
                .map(|((text, style), (_, width, right))| cell(text, style, right, width))
                .collect::<Vec<_>>(),
        );
        if selected {
            // A marker *and* a shade: on a 256-colour terminal the shade alone
            // is one step of a ramp, which is not an answer to "which row".
            row.style(Style::default().bg(t.bg_hover))
        } else {
            row
        }
    }
}

/// The panel header, with the keys that drive the grid pushed to its far side.
///
/// `« ‹ 2/3 › »` says which page the *top row* falls on rather than claiming the
/// screen holds exactly that page: the scroll position is a row, so a resize
/// keeps the operator where they were and cannot also keep the page boundaries.
/// The arrows dim at the ends, so "there is nothing further" is visible without
/// pressing the key to find out — and the pager is absent entirely when the
/// whole book is on screen, because a `1/1` is a control that does nothing.
///
/// `s sort` stays whatever the length of the book: an operator who cannot see
/// that the column order is theirs to change will read the default as the only
/// order there is.
fn draw_header(f: &mut Frame, area: Rect, top: usize, page: usize, rows: usize) {
    let t = theme();
    let dim = Style::default().fg(t.text_dim);
    let pages = rows.div_ceil(page.max(1));
    // A book with nothing in it gets no keys at all: a control offered over an
    // empty grid is one an operator presses to find out it does nothing.
    let mut keys = if rows == 0 {
        Vec::new()
    } else {
        vec![Span::styled("s sort", dim)]
    };
    if pages > 1 {
        let at = top / page.max(1);
        let back = if top > 0 {
            t.text_secondary
        } else {
            t.text_dim
        };
        let on = if at + 1 < pages {
            t.text_secondary
        } else {
            t.text_dim
        };
        keys.extend([
            Span::styled(" · [ ] page  ", dim),
            Span::styled("« ‹ ", Style::default().fg(back)),
            Span::styled(
                format!("{}/{pages}", at + 1),
                Style::default().fg(t.text_primary),
            ),
            Span::styled(" › »", Style::default().fg(on)),
        ]);
    }

    let title = panel_header("positions");
    let gap = (area.width as usize)
        .saturating_sub(title.width() + keys.iter().map(|s| s.content.width()).sum::<usize>());
    let mut spans = title.spans;
    spans.push(Span::raw(" ".repeat(gap)));
    spans.extend(keys);
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Share counts as a desk states them: whole where they are whole, two decimals
/// where the owner's optimizer produced a fraction of a share.
///
/// Not a fixed two everywhere: a book of round lots would then read `100.00`,
/// which is precision the number does not carry and two cells of a column that
/// has none to spare.
fn qty(value: f64) -> String {
    if !value.is_finite() {
        return MISSING.to_string();
    }
    if value.fract() == 0.0 {
        format!("{value:.0}")
    } else {
        format!("{value:.2}")
    }
}

// -- the footer: the equity curve, and the holdings rail beside it -----------

/// One holding cell: a five-cell ticker and the six-cell percentage beside it.
///
/// Five is the longest ticker the owner's universes carry (`XLRE`, `GOOGL`);
/// six is `-123.4%`'s worth less its last digit, which is what `head` costs a
/// runaway loss rather than costing it the sign.
const HOLD_W: u16 = 11;

/// Two columns of holdings, and the cell between them.
///
/// Two rather than three because the movers under them are 22 cells wide — a
/// glyph, a role, a ticker and a percentage — and a rail whose footer is wider
/// than its grid reads as two panels that happen to be stacked.
const RAIL_W: u16 = HOLD_W * 2 + 1;

/// The rail's header, one row of holdings, and its movers footer.
const RAIL_H: u16 = 1 + 1 + MOVERS_H;

/// The movers footer: its header and the two rows under it.
const MOVERS_H: u16 = 3;

/// The narrowest curve pane worth splitting the footer for.
///
/// A `$10,012.40` scale and the cell that keeps it off the line, plus the eight
/// columns of plot `braille_chart` holds itself to. Spelled here as well because
/// the split happens before the chart sees anything: without it a narrow footer
/// hands the curve a pane with no cells in it, where a refusal has nowhere to be
/// drawn. The chart's own guard is the exact one — this is only what says the
/// band is worth splitting at all — and a test pins the two together.
const CURVE_W: u16 = 19;

/// The footer's floor: the rail, the cell between the panes, and the curve.
const BOTTOM_W: u16 = RAIL_W + 1 + CURVE_W;

/// Which quantity the holdings rail is shaded by.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Heat {
    /// Open P&L, on the semantic pair: green up, red down.
    #[default]
    Pnl,
    /// Allocation, on the accent pair: a weight has a size and no direction.
    Weight,
}

impl Heat {
    fn next(self) -> Heat {
        match self {
            Heat::Pnl => Heat::Weight,
            Heat::Weight => Heat::Pnl,
        }
    }

    /// What the header calls the live mode. Not `P&L%`/`WT%`: those are blotter
    /// column names, and a rail that borrowed them would read as a third view of
    /// the same two columns rather than as a shading of the whole book.
    fn label(self) -> &'static str {
        match self {
            Heat::Pnl => "P&L",
            Heat::Weight => "WT",
        }
    }
}

/// Which slice of the equity series the curve draws.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Period {
    #[default]
    All,
    Y1,
    M3,
    M1,
}

impl Period {
    /// The cycle `p` walks, longest first — the same direction the eye reads the
    /// row in, so the key and the strip agree about which way is "shorter".
    const ALL: [Period; 4] = [Period::All, Period::Y1, Period::M3, Period::M1];

    fn next(self) -> Period {
        let at = Period::ALL.iter().position(|p| *p == self).unwrap_or(0);
        Period::ALL[(at + 1) % Period::ALL.len()]
    }

    fn label(self) -> &'static str {
        match self {
            Period::All => "ALL",
            Period::Y1 => "1Y",
            Period::M3 => "3M",
            Period::M1 => "1M",
        }
    }

    /// How many trailing marks the slice keeps, or the whole series.
    ///
    /// Marks, not days. The owner's `performance.series` is one point per
    /// booked mark and it is not guaranteed dense — a desk that was down for a
    /// week has no marks for it — so 63 is "the last 63 marks", which is a
    /// quarter of trading days only on a series with no gaps. A window measured
    /// in dates would need the owner to serve them; `EquityPoint::ts` carries a
    /// date string this client does not parse, and parsing one to slice by it
    /// would be a calendar this client invented.
    fn marks(self) -> Option<usize> {
        match self {
            Period::All => None,
            Period::Y1 => Some(365),
            Period::M3 => Some(63),
            Period::M1 => Some(21),
        }
    }

    /// The trailing window of `series` this period names.
    fn slice(self, series: &[EquityPoint]) -> &[EquityPoint] {
        match self.marks() {
            Some(marks) => &series[series.len().saturating_sub(marks)..],
            None => series,
        }
    }
}

impl BookView {
    /// The footer: the curve, and the holdings rail on its right.
    fn draw_footer(&self, f: &mut Frame, area: Rect, store: &Store) {
        // A band with no cells in it. Nothing can be said here, refusal
        // included; the blotter above it has the whole pane.
        if area.width == 0 || area.height == 0 {
            return;
        }
        if area.width < BOTTOM_W {
            refuse(
                f,
                area,
                format!(
                    "book footer needs {BOTTOM_W} columns for an equity curve beside \
                     {RAIL_W} of holdings — this pane has {}, widen the terminal",
                    area.width
                ),
            );
            return;
        }

        // Split first, then guard each allocation — the pane is not the pane.
        let cols = Layout::horizontal([Constraint::Min(0), Constraint::Length(RAIL_W)])
            .spacing(1)
            .split(area);
        self.draw_curve(f, cols[0], store);
        self.draw_holdings(f, cols[1], store);
    }

    /// The equity curve over the slice `p` last chose.
    fn draw_curve(&self, f: &mut Frame, area: Rect, store: &Store) {
        let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).split(area);
        f.render_widget(
            Paragraph::new(header_keys("equity", self.period_keys(), rows[0].width)),
            rows[0],
        );

        let Some(performance) = store.snapshot.as_ref().and_then(|s| s.performance.as_ref()) else {
            refuse(f, rows[1], "no equity series in the last snapshot".into());
            return;
        };
        // A mark the owner sent no equity for is not a zero: plotted at one it
        // would draw a cliff to the axis and back. Dropped, the curve is the
        // marks that were valued, which is what the window it is sliced from
        // was measured over.
        let equity: Vec<f64> = self
            .period
            .slice(&performance.series)
            .iter()
            .filter_map(|point| point.equity)
            .collect();
        if equity.len() < 2 {
            // The honest analogue of a greyed-out period button: two points is
            // the least a line can be drawn between, and one mark rendered as a
            // dot in the middle of an empty pane is a chart of nothing.
            refuse(f, rows[1], "needs more history — daily marks only".into());
            return;
        }

        braille_chart::draw(
            f,
            rows[1],
            braille_chart::Chart {
                name: "equity curve",
                series: &equity,
                // No crosshair: the markets hero's chip is indexed because a
                // price series carries no dates, and this one is *sliced* by
                // period — an index into a window that moves under the key
                // would name a different mark every time `p` is pressed.
                crosshair: None,
                // Grouped money at book scale. `compact_money` would render
                // four identical `$10.00K` labels on a book that moved a
                // hundred dollars, which is a scale with no scale on it.
                label: format::money,
            },
        );
    }

    /// The period strip, with the live slice lit.
    fn period_keys(&self) -> Vec<Span<'static>> {
        let t = theme();
        let mut keys = vec![Span::styled("p period ", Style::default().fg(t.text_dim))];
        for period in Period::ALL {
            let style = if period == self.period {
                Style::default().fg(t.text_primary)
            } else {
                Style::default().fg(t.text_dim)
            };
            keys.push(Span::styled(format!(" {}", period.label()), style));
        }
        keys
    }

    /// The holdings rail: a heat grid over the book, and its two ends under it.
    fn draw_holdings(&self, f: &mut Frame, area: Rect, store: &Store) {
        if area.width < RAIL_W || area.height < RAIL_H {
            refuse(
                f,
                area,
                format!(
                    "holdings rail needs {RAIL_W}×{RAIL_H} for a row of cells and its \
                     movers — this pane is {}×{}, make the terminal larger",
                    area.width, area.height
                ),
            );
            return;
        }
        let rows = Layout::vertical([
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(MOVERS_H),
        ])
        .split(area);
        f.render_widget(
            Paragraph::new(header_keys("holdings", self.heat_keys(), rows[0].width)),
            rows[0],
        );

        // The blotter's rows in the blotter's order: one book, one ordering. A
        // rail that sorted itself would put a name in a different place from
        // the row an operator just read it off.
        let holdings = sorted(blotter_rows(store), self.sort);
        draw_heat_grid(f, rows[1], &holdings, self.heat);
        draw_movers(f, rows[2], &holdings);
    }

    fn heat_keys(&self) -> Vec<Span<'static>> {
        let t = theme();
        vec![
            Span::styled("h ", Style::default().fg(t.text_dim)),
            Span::styled(self.heat.label(), Style::default().fg(t.text_primary)),
        ]
    }
}

/// A panel header with the keys that drive it pushed to the far side.
///
/// The keys go whole or not at all: a `Paragraph` clips from the right, and
/// `ALL 1Y 3` is a control an operator reads as broken rather than as absent.
fn header_keys(title: &str, keys: Vec<Span<'static>>, width: u16) -> Line<'static> {
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

/// Every holding as a shaded tile, two to a row.
fn draw_heat_grid(f: &mut Frame, area: Rect, holdings: &[BlotterRow<'_>], mode: Heat) {
    // Which kind of nothing this is has already been said by the blotter above,
    // and a second copy of the same sentence reads as two separate failures.
    if holdings.is_empty() {
        return;
    }
    let gap = Span::raw(" ");
    let lines: Vec<Line<'static>> = holdings
        .chunks(2)
        .map(|pair| {
            let mut spans = Vec::with_capacity(3);
            for (i, row) in pair.iter().enumerate() {
                if i > 0 {
                    spans.push(gap.clone());
                }
                spans.push(heat_tile(row, mode));
            }
            Line::from(spans)
        })
        .collect();

    // The same sub-floor class as the sector strip: a `Paragraph` taller than
    // its area is clipped without complaint, and a book missing its last two
    // names is a rail that says the desk does not hold them.
    if lines.len() > area.height as usize {
        refuse(
            f,
            area,
            format!(
                "holdings heatmap needs {} rows for {} positions — this pane has {}, \
                 make the terminal taller",
                lines.len(),
                holdings.len(),
                area.height
            ),
        );
        return;
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// One holding: its ticker, the number the mode is about, and the shade.
fn heat_tile(row: &BlotterRow<'_>, mode: Heat) -> Span<'static> {
    let t = theme();
    // Absent is not flat and not zero: the tile takes the tone for "the owner
    // declined to say", exactly as the blotter's row does. A value that is not
    // finite reads the same way — `signed_pct1` already renders it `--`, and a
    // tile that said `--` on a red background would shade a number nobody
    // computed.
    let unmeasured = (MISSING.to_string(), Style::default().fg(t.text_secondary));
    let (value, style) = match mode {
        Heat::Pnl => match row.pnl_pct {
            Some(pct) if pct.is_finite() => (format::signed_pct1(pct), pnl_shade(pct)),
            _ => unmeasured,
        },
        Heat::Weight => match row.weight {
            // Unsigned: a weight's sign is which side of the book it is on, and
            // the blotter's `WT%` column is where that is read.
            Some(weight) if weight.is_finite() => (format::pct1(weight), weight_shade(weight)),
            _ => unmeasured,
        },
    };
    // Through `head` on both halves: the value is right-aligned inside its six
    // cells, and ratatui drops *leading* cells from an overlong right-aligned
    // run — which on `-123.4%` is the minus. Costing the last digit instead
    // makes a runaway loss coarse rather than making it a gain.
    Span::styled(
        format!(
            "{:<5}{:>6}",
            head(row.ticker.unwrap_or(MISSING).to_string(), 5),
            head(value, 6)
        ),
        style,
    )
}

/// The P&L ramp: the magnitude against a twenty-percent full scale.
///
/// Twenty because that is the move at which a position is the story of the book
/// rather than noise in it. Past it the ramp saturates rather than letting one
/// runaway name be the only lit tile on the rail — a heatmap whose scale is set
/// by its outlier says nothing about the other nine holdings.
const PNL_FULL_SCALE: f64 = 20.0;

/// The allocation ramp's full scale, in percent. Forty because a position past
/// it is the book, on a desk the mandate holds to a handful of names.
const WEIGHT_FULL_SCALE: f64 = 40.0;

fn pnl_step(pct: f64) -> u8 {
    heat_cell::step((pct * 100.0).abs() / PNL_FULL_SCALE)
}

fn weight_step(weight: f64) -> u8 {
    // The magnitude, so a short leg shades by its size rather than reading as
    // the dimmest tile on the rail whatever it is worth.
    heat_cell::step((weight * 100.0).abs() / WEIGHT_FULL_SCALE)
}

/// A blotter row's P&L tone, in three states.
///
/// Beside `pnl_shade` rather than inline in `draw_row`, because it is the same
/// rule the rail applies at a different precision, and a rule with a name is a
/// rule a test can hold to its own boundary instead of scraping it back out of
/// a rendered buffer.
///
/// Flat is neither: `format::change_tone` paints zero green, which is right for
/// the ribbon's single hero and wrong for a column of them — a paper book that
/// opened flat would render as ten green rows, a claim the desk made money on
/// all ten. Absent is a fourth thing again, and not flat.
///
/// Decided at the two decimals `signed_compact_money` prints, never off the raw
/// double: a fully-invested paper book carries -1e-13, which renders `+$0.00`
/// and was drawn in red beside itself.
fn pnl_tone(pnl: Option<f64>) -> Color {
    let t = theme();
    match pnl {
        Some(v) if format::zero_at(v, 2) => t.text_primary,
        Some(v) if format::negative_at(v, 2) => t.negative,
        // A number that is not finite reaches neither guard — absent is what it
        // is, and the cell beside it already renders `--`.
        Some(v) if v.is_finite() => t.positive,
        _ => t.text_secondary,
    }
}

fn pnl_shade(pct: f64) -> Style {
    let t = theme();
    // Flat is neither, exactly as the blotter's paired P&L columns: `format::change_tone`
    // paints zero green, which is right for one hero number and wrong for a grid
    // of them — a paper book that opened flat would render as a rail of green
    // tiles, a claim the desk made money on every name it holds.
    //
    // At the one decimal of a percent `signed_pct1` prints on the tile, not off
    // the raw double: a P&L of -1e-13 reads `+0.0%` and took the negative ramp,
    // the shade contradicting the digits it was painting.
    let printed = pct * 100.0;
    if format::zero_at(printed, 1) {
        return Style::default().bg(t.bg_base).fg(t.text_primary);
    }
    let (dim, bright) = if format::negative_at(printed, 1) {
        (t.negative_dim, t.negative)
    } else {
        (t.positive_dim, t.positive)
    };
    heat_cell::style(pnl_step(pct), dim, bright)
}

fn weight_shade(weight: f64) -> Style {
    let t = theme();
    // Amber rather than the semantic pair: an allocation has a size and no
    // direction, and a green 40% would say the position is winning.
    heat_cell::style(weight_step(weight), t.accent_dim, t.accent)
}

/// The two ends of the book by open P&L.
///
/// The ends of the *book*, not of the page: an operator turning to page two has
/// not changed which name is winning, and a footer that followed the pager would
/// be answering a different question every keystroke.
fn draw_movers(f: &mut Frame, area: Rect, holdings: &[BlotterRow<'_>]) {
    let mut lines = vec![panel_header("top movers")];
    match movers(holdings) {
        // A book of one is its own best and worst; two identical rows would read
        // as two movers, which is a desk this client is not looking at.
        Some((best, worst)) if best == worst => lines.push(mover("only", &holdings[best])),
        Some((best, worst)) => {
            lines.push(mover("best", &holdings[best]));
            lines.push(mover("worst", &holdings[worst]));
        }
        None => lines.push(Line::from(Span::styled(
            format!(" {MISSING}"),
            Style::default().fg(theme().text_tertiary),
        ))),
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// The best and worst holdings by the P&L percentage they carry, as indices.
///
/// Indices rather than rows, so "the same holding twice" is an identity the
/// caller can check: two positions can tie at the same percentage and they are
/// still two movers, while one position is one.
fn movers(holdings: &[BlotterRow<'_>]) -> Option<(usize, usize)> {
    let marked: Vec<usize> = (0..holdings.len())
        .filter(|i| holdings[*i].pnl_pct.is_some())
        .collect();
    let key = |i: &usize| holdings[*i].pnl_pct.unwrap_or_default();
    let by_pnl = |a: &usize, b: &usize| key(a).partial_cmp(&key(b)).unwrap_or(Ordering::Equal);
    Some((
        *marked.iter().max_by(|a, b| by_pnl(a, b))?,
        *marked.iter().min_by(|a, b| by_pnl(a, b))?,
    ))
}

/// One end of the book: a direction glyph, the role, the name and the number.
fn mover(role: &str, row: &BlotterRow<'_>) -> Line<'static> {
    let t = theme();
    // `movers` only ever hands back rows that carry a percentage.
    let pct = row.pnl_pct.unwrap_or_default();
    let text = format::signed_pct1(pct);
    // Glyph and colour from one rounding, at the single decimal of a percent
    // `signed_pct1` prints — a ▼ over `+0.0%`, or a red `▲`, is a row
    // contradicting itself. Flat gets neither arrow — a ▲ over a book that did
    // not move is a rise the desk did not make.
    let (arrow, tone) = if format::zero_at(pct * 100.0, 1) {
        ("·", t.text_primary)
    } else if format::negative_at(pct * 100.0, 1) {
        ("▼", t.negative)
    } else {
        ("▲", t.positive)
    };
    Line::from(vec![
        Span::styled(format!(" {arrow} "), Style::default().fg(tone)),
        Span::styled(format!("{role:<6}"), Style::default().fg(t.text_secondary)),
        Span::styled(
            format!("{:<5}", head(row.ticker.unwrap_or(MISSING).to_string(), 5)),
            Style::default().fg(t.cyan).add_modifier(Modifier::BOLD),
        ),
        Span::styled(format!("{:>8}", head(text, 8)), Style::default().fg(tone)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_gainers_and_losers_count_only_positions_that_moved() {
        let positions: Vec<Position> = serde_json::from_str(
            r#"[{"unrealized_pnl": 12.0}, {"unrealized_pnl": -3.0},
                {"unrealized_pnl": 0.0}, {"unrealized_pnl": 0.5}, {}]"#,
        )
        .unwrap();
        // Two up, one down: the flat one is neither and the one with no P&L at
        // all is unmeasured rather than flat.
        assert_eq!(gainers_and_losers(&positions), (2, 1));
        assert_eq!(gainers_and_losers(&[]), (0, 0));

        // A figure that is not finite is unmeasured too, not a gainer. Both
        // rounded thresholds answer false for a NaN, so without the explicit
        // guard it falls through to the winners — the one direction a count may
        // never guess in. Built by hand because JSON cannot carry a NaN: it is
        // unreachable from the owner and reachable from arithmetic this client
        // may grow, which is the same reason the heat tile guards its own.
        let mut broken: Vec<Position> =
            serde_json::from_value(serde_json::json!([{"unrealized_pnl": 0.0}])).unwrap();
        broken[0].unrealized_pnl = Some(f64::NAN);
        assert_eq!(gainers_and_losers(&broken), (0, 0));
    }

    #[test]
    fn the_return_is_on_what_the_book_paid_and_not_on_what_it_is_worth() {
        let book: LivePortfolio = serde_json::from_str(
            r#"{"equity": 1000.0, "unrealized_pnl": 40.0,
                "positions": [{"qty": 2.0, "avg_price": 100.0},
                              {"qty": 1.0, "avg_price": 200.0}]}"#,
        )
        .unwrap();
        // $40 on a $400 cost basis. Against the $1,000 equity the same book
        // would read +4%, a different fact under the same label.
        assert_eq!(return_on_cost(&book), Some(0.1));
    }

    #[test]
    fn a_short_leg_adds_to_the_cost_basis_rather_than_cancelling_it() {
        // Signed, a market-neutral pair nets to a denominator at zero and the
        // return goes to infinity — or, worse, to a plausible large number.
        let book: LivePortfolio = serde_json::from_str(
            r#"{"unrealized_pnl": 10.0,
                "positions": [{"qty": 5.0, "avg_price": 100.0},
                              {"qty": -5.0, "avg_price": 100.0}]}"#,
        )
        .unwrap();
        assert_eq!(return_on_cost(&book), Some(0.01));
    }

    #[test]
    fn a_book_with_no_cost_basis_has_no_return_on_one() {
        let empty: LivePortfolio =
            serde_json::from_str(r#"{"equity": 1000.0, "unrealized_pnl": 0.0, "positions": []}"#)
                .unwrap();
        assert_eq!(return_on_cost(&empty), None);
        // And a P&L the owner never sent is not a zero return.
        let unmarked: LivePortfolio =
            serde_json::from_str(r#"{"positions": [{"qty": 1.0, "avg_price": 10.0}]}"#).unwrap();
        assert_eq!(return_on_cost(&unmarked), None);
    }

    #[test]
    fn a_chip_pads_its_value_to_the_far_side_of_its_column() {
        let rendered =
            |line: Line| -> String { line.spans.iter().map(|s| s.content.to_string()).collect() };
        assert_eq!(
            rendered(chip("SHARPE", Some("0.27".into()))),
            "SHARPE   0.27"
        );
        assert_eq!(rendered(chip("CVAR95", None)), "CVAR95     --");
        // The widest value a chip can carry still fits its column.
        assert_eq!(
            rendered(chip("NET", Some("-100.0%".into()))),
            "NET   -100.0%"
        );
        for line in [
            chip("SHARPE", Some("-1.23".into())),
            chip("CVAR95", Some("-100.0%".into())),
            chip("VOL", None),
        ] {
            assert_eq!(rendered(line).chars().count(), CHIP_W as usize);
        }
    }

    #[test]
    fn a_sub_line_past_its_cell_drops_a_chip_rather_than_a_digit() {
        let rendered =
            |line: Line| -> String { line.spans.iter().map(|s| s.content.to_string()).collect() };
        let chips = || vec![Span::raw("123 pos"), Span::raw("cash $999.99K")];
        // Room for both, and the separator between them.
        assert_eq!(
            rendered(sub_line(chips(), " · ", 23)),
            "123 pos · cash $999.99K"
        );
        // One column short. Clipped, this reads `cash $999.99` — a balance a
        // thousandth of the one the owner sent.
        assert_eq!(rendered(sub_line(chips(), " · ", 22)), "123 pos");
        // The first chip is kept whatever happens: something on the line beats a
        // blank row, and it is the one an operator asked for first.
        assert_eq!(rendered(sub_line(chips(), " · ", 2)), "123 pos");
        // The breadth chips go one at a time, not both at once.
        let breadth = vec![Span::raw("+12.34%"), Span::raw("▲10"), Span::raw("▼10")];
        assert_eq!(
            rendered(sub_line(breadth.clone(), " ", 15)),
            "+12.34% ▲10 ▼10"
        );
        assert_eq!(rendered(sub_line(breadth, " ", 14)), "+12.34% ▲10");
    }

    #[test]
    fn a_sharpe_is_a_ratio_and_an_absent_one_is_not_a_zero() {
        assert_eq!(ratio(0.2688), "0.27");
        assert_eq!(ratio(-1.2345), "-1.23");
        assert_eq!(ratio(f64::NAN), MISSING);
    }

    /// A row carrying only what the sort reads. The display side is pinned by
    /// the rendered frames in `tests/golden_book.rs`; these are about order.
    fn row(ticker: &str, key: Option<f64>) -> BlotterRow<'_> {
        BlotterRow {
            ticker: (!ticker.is_empty()).then_some(ticker),
            qty: None,
            last: None,
            avg: None,
            weight: key,
            value: key,
            pnl: key,
            pnl_pct: key,
            history: None,
        }
    }

    fn order<'a>(rows: Vec<BlotterRow<'a>>, sort: Sort) -> Vec<&'a str> {
        sorted(rows, sort)
            .iter()
            .map(|r| r.ticker.unwrap_or(MISSING))
            .collect()
    }

    #[test]
    fn a_money_sort_is_biggest_first_and_a_name_sort_is_a_to_z() {
        let rows = || {
            vec![
                row("MID", Some(500.0)),
                row("BIG", Some(2_500_000.0)),
                row("SMALL", Some(1.0)),
            ]
        };
        for sort in [Sort::Weight, Sort::MktVal, Sort::Pnl, Sort::PnlPct] {
            assert_eq!(order(rows(), sort), vec!["BIG", "MID", "SMALL"], "{sort:?}");
        }
        assert_eq!(order(rows(), Sort::Symbol), vec!["BIG", "MID", "SMALL"]);
        // …and A-to-Z is really the name, not the money it happens to track.
        assert_eq!(
            order(
                vec![row("ZZZ", Some(9.0)), row("AAA", Some(1.0))],
                Sort::Symbol
            ),
            vec!["AAA", "ZZZ"]
        );
    }

    #[test]
    fn a_number_the_owner_did_not_send_sorts_last_and_never_first() {
        // Absent is not zero and it is not the biggest either: a `--` at the
        // head of a heaviest-first list is the row an operator reads as the
        // answer to "where is the exposure".
        for sort in [Sort::Weight, Sort::MktVal, Sort::Pnl, Sort::PnlPct] {
            let rows = vec![
                row("NONE", None),
                row("SOME", Some(1.0)),
                row("ALSO", Some(2.0)),
            ];
            assert_eq!(*order(rows, sort).last().unwrap(), "NONE", "{sort:?}");
        }
        // What "absent" means under a name sort is an absent *name* — the
        // position the owner sent with no ticker, which is still a row.
        assert_eq!(
            order(
                vec![row("", Some(9.0)), row("ZZZ", Some(1.0)), row("AAA", None)],
                Sort::Symbol
            ),
            vec!["AAA", "ZZZ", MISSING]
        );
    }

    #[test]
    fn a_sort_is_stable_so_the_owners_order_survives_a_tie() {
        // Unstable, a book the owner sent at one weight reshuffles on every
        // repaint — a blotter that will not hold still.
        let tied = || {
            vec![
                row("D", Some(1.0)),
                row("C", Some(1.0)),
                row("B", Some(1.0)),
                row("A", Some(1.0)),
            ]
        };
        assert_eq!(order(tied(), Sort::Weight), vec!["D", "C", "B", "A"]);
        assert_eq!(order(tied(), Sort::MktVal), vec!["D", "C", "B", "A"]);
    }

    #[test]
    fn the_sort_cycle_visits_every_column_once_and_comes_back() {
        let mut seen = vec![Sort::default()];
        let mut at = Sort::default();
        for _ in 0..Sort::ALL.len() {
            at = at.next();
            if at != Sort::default() {
                seen.push(at);
            }
        }
        assert_eq!(at, Sort::default(), "the cycle does not close");
        assert_eq!(seen, Sort::ALL.to_vec());
        // Every column the cycle names is a column that exists, and no two
        // sorts head the same one.
        let mut columns: Vec<usize> = Sort::ALL.iter().map(|s| s.column()).collect();
        columns.sort_unstable();
        columns.dedup();
        assert_eq!(columns.len(), Sort::ALL.len());
        assert!(columns.iter().all(|c| *c < BLOTTER_COLS.len()));
    }

    #[test]
    fn a_share_count_is_whole_where_it_is_whole() {
        // Two decimals everywhere would read `100.00` for a round lot —
        // precision the number does not carry, in a column with none to spare.
        assert_eq!(qty(100.0), "100");
        assert_eq!(qty(59.18910760019987), "59.19");
        assert_eq!(qty(4.121014846223033), "4.12");
        assert_eq!(qty(-3.0), "-3");
        assert_eq!(qty(f64::NAN), MISSING);
    }

    #[test]
    fn every_column_is_wide_enough_for_its_own_header_and_sort_glyph() {
        // A column narrower than its title renders `MKTVA`, and a truncated
        // header is a column an operator has to guess at — the sort glyph
        // included, since it lands inside the same cell.
        for sort in Sort::ALL {
            let (name, width, _) = BLOTTER_COLS[sort.column()];
            assert!(
                name.chars().count() < width as usize,
                "{name} is {width} wide and its sort glyph does not fit"
            );
        }
        for (name, width, _) in BLOTTER_COLS {
            assert!(
                name.chars().count() <= width as usize,
                "{name} does not fit its own column"
            );
        }
    }

    #[test]
    fn the_blotter_floor_is_the_columns_own_arithmetic() {
        // The guard and the column widths are one fact. Spelled twice, a column
        // widened for a longer ticker would silently start truncating.
        let columns: u16 = BLOTTER_COLS.iter().map(|(_, w, _)| w).sum();
        assert_eq!(BLOTTER_W, columns + BLOTTER_COLS.len() as u16 - 1);
    }

    #[test]
    fn the_scroll_holds_the_top_row_and_stops_at_the_last_pages_first() {
        let at = |top: usize, rows: usize, page: usize| {
            let view = BookView {
                top,
                ..BookView::default()
            };
            view.scroll(rows, page)
        };
        // Twelve rows, five a page: the pages start at 0, 5 and 10.
        assert_eq!(at(0, 12, 5), 0);
        assert_eq!(at(10, 12, 5), 10);
        assert_eq!(at(11, 12, 5), 10, "the scroll ran past the last page");
        // The owner closes ten positions under the cursor. Clamped to the last
        // *row* this would answer with one row and a blank pane.
        assert_eq!(at(10, 2, 5), 0);
        // A preserved top after a resize is left where it was.
        assert_eq!(at(5, 12, 10), 5);
        // Nothing at all cannot scroll anywhere.
        assert_eq!(at(3, 0, 5), 0);
    }

    #[test]
    fn a_page_turn_lands_on_a_page_and_never_walks_backwards() {
        let turn = |top: usize, to: usize, rows: usize, page: usize| {
            let mut view = BookView {
                top,
                ..BookView::default()
            };
            view.page_to(to, rows, page);
            (view.top, view.selected)
        };
        // Forward through twelve rows at five a page, and a wall at the end.
        assert_eq!(turn(0, 5, 12, 5), (5, 5));
        assert_eq!(turn(5, 10, 12, 5), (10, 10));
        assert_eq!(turn(10, 15, 12, 5), (10, 10), "the last page wrapped");
        // Back, and a wall at the start.
        assert_eq!(turn(10, 5, 12, 5), (5, 5));
        assert_eq!(turn(0, 0, 12, 5), (0, 0));
        // The case a resize creates: a top past the last page's first row. A
        // forward key must not answer by moving the operator *up* the book.
        assert_eq!(turn(11, 21, 12, 10), (11, 11));
    }

    // -- the footer ---------------------------------------------------------

    #[test]
    fn the_holdings_ramp_bands_at_a_twentieth_of_its_full_scale() {
        // Six even steps to 20%: a position under 3.33% is the dimmest tile and
        // one past 20% is the brightest, however far past. A ramp scaled by its
        // own outlier would say nothing about the nine names beside it.
        let step = |pct: f64| pnl_step(pct / 100.0);
        assert_eq!(step(0.0), 1);
        assert_eq!(step(0.4), 1);
        assert_eq!(step(3.3), 1);
        assert_eq!(step(3.4), 2);
        assert_eq!(step(10.0), 4);
        assert_eq!(step(20.0), 6);
        assert_eq!(step(21.0), 6, "the ramp did not clamp");
        assert_eq!(step(900.0), 6);
        // Magnitude only: brightness says how much, the pair says which way.
        for pct in [0.4, 3.4, 12.0, 99.0] {
            assert_eq!(step(pct), step(-pct), "{pct}");
        }
        assert_ne!(pnl_shade(0.034), pnl_shade(-0.034));
    }

    #[test]
    fn a_position_that_has_not_moved_is_neither_a_gain_nor_a_loss_on_the_rail() {
        // The blotter's rule, one pane over: `format::change_tone` paints zero green,
        // and a paper book that opened flat would render as a rail of green
        // tiles — a claim the desk made money on every name it holds.
        let t = theme();
        let flat = pnl_shade(0.0);
        assert_eq!(flat.fg, Some(t.text_primary));
        // A move that survives the tile's own precision still takes a side.
        assert_ne!(flat, pnl_shade(0.001), "flat took the winners' ramp");
        assert_ne!(flat, pnl_shade(-0.001));
    }

    #[test]
    fn the_pnl_pair_takes_its_side_at_the_precision_each_surface_prints() {
        // The round-once rule at the two sites it had not reached. Both keep
        // three states — flat is neither, which is why they cannot simply use
        // `change_tone` — and both now decide *which* of the other two at the
        // precision the cell beside them actually prints.
        let t = theme();
        let flat = pnl_shade(0.0);

        // A fully-invested paper book carries -1e-13; `desk.rs` cites the same
        // magnitude off a real one. The tile renders it `+0.0%`, and shading
        // that on the negative ramp is the shade contradicting the digits it is
        // painting, in the half of the cell a reader trusts first.
        for tiny in [-1e-13, -1e-9, -0.0004] {
            assert_eq!(
                format::signed_pct1(tiny),
                "+0.0%",
                "{tiny} is not the case this test is about"
            );
            assert_eq!(pnl_shade(tiny), flat, "{tiny} shaded a loss nobody had");
            assert_eq!(pnl_tone(Some(tiny)), t.text_primary, "{tiny}");
        }

        // The true-negative neighbour, at the edge where the printed digit
        // survives — one decimal of a percent for the tile, two decimals of
        // money for the row. Without these the rule would swallow real losses.
        for real in [-0.0005, -0.001, -0.12] {
            assert!(format::signed_pct1(real).starts_with('-'), "{real}");
            assert_eq!(
                pnl_shade(real),
                heat_cell::style(pnl_step(real), t.negative_dim, t.negative),
                "{real} left the negative ramp"
            );
        }
        assert_eq!(pnl_tone(Some(-0.005)), t.negative, "-$0.01 is a loss");
        assert_eq!(pnl_tone(Some(0.005)), t.positive);
        assert_eq!(pnl_tone(Some(-0.004)), t.text_primary, "+$0.00 is not");

        // And absent is none of the three: the tone for "the owner declined to
        // say", which a rounding rule must never turn into flat.
        assert_eq!(pnl_tone(None), t.text_secondary);
        assert_eq!(pnl_tone(Some(f64::NAN)), t.text_secondary);

        // The two neighbours the sweep for this pattern turned up, on the same
        // rule. The footer's mover glyph — `▲` over `+0.0%` is the row arguing
        // with itself just as `▼` over it would be.
        let glyph = |pct: f64| {
            let row = BlotterRow {
                ticker: Some("SPY"),
                qty: None,
                last: None,
                avg: None,
                weight: None,
                value: None,
                pnl: None,
                pnl_pct: Some(pct),
                history: None,
            };
            mover("best", &row).spans[0].content.trim().to_string()
        };
        assert_eq!(glyph(-1e-13), "·", "a debt of nothing took an arrow");
        assert_eq!(glyph(0.0), "·");
        assert_eq!(glyph(-0.0005), "▼");
        assert_eq!(glyph(0.0005), "▲");

        // And the ribbon's breadth count, which has to agree with the blotter
        // row under it about whether one position moved.
        let pnl = |v: f64| {
            gainers_and_losers(&[serde_json::from_value::<crate::model::Position>(
                serde_json::json!({"ticker": "SPY", "unrealized_pnl": v}),
            )
            .unwrap()])
        };
        assert_eq!(pnl(-1e-13), (0, 0), "a row drawn `+$0.00` was a decliner");
        assert_eq!(pnl(0.0), (0, 0));
        assert_eq!(pnl(-0.005), (0, 1));
        assert_eq!(pnl(0.005), (1, 0));
    }

    #[test]
    fn the_allocation_ramp_is_amber_from_nothing_to_forty_percent() {
        // Amber, not the semantic pair: a weight has a size and no direction.
        let step = |pct: f64| weight_step(pct / 100.0);
        assert_eq!(step(0.0), 1);
        assert_eq!(step(20.0), 4);
        assert_eq!(step(40.0), 6);
        assert_eq!(step(90.0), 6, "the ramp did not clamp");
        // A short leg shades by its size rather than reading as the dimmest
        // tile on the rail whatever it is worth.
        assert_eq!(step(-40.0), 6);
        let t = theme();
        assert_eq!(weight_shade(0.4).bg, Some(t.accent));
        assert_ne!(weight_shade(0.4).bg, Some(t.positive));
    }

    #[test]
    fn a_period_is_a_trailing_window_of_marks_and_never_the_head_of_the_series() {
        // A window off the *front* would draw the oldest quarter of the book
        // under a label that says the most recent one.
        let series: Vec<EquityPoint> = (0..400)
            .map(|i| serde_json::from_str(&format!(r#"{{"equity": {i}.0}}"#)).unwrap())
            .collect();
        let ends = |p: Period| {
            let slice = p.slice(&series);
            (slice.len(), slice.first().unwrap().equity.unwrap())
        };
        assert_eq!(ends(Period::All), (400, 0.0));
        assert_eq!(ends(Period::Y1), (365, 35.0));
        assert_eq!(ends(Period::M3), (63, 337.0));
        assert_eq!(ends(Period::M1), (21, 379.0));
        // A window longer than the series is the series, not a panic and not a
        // pad: 21 marks of a five-mark book is those five marks.
        let short = &series[..5];
        assert_eq!(Period::M1.slice(short).len(), 5);
        assert_eq!(Period::All.slice(&[]).len(), 0);
    }

    #[test]
    fn both_footer_cycles_visit_every_option_once_and_come_back() {
        let mut at = Period::default();
        let mut seen = vec![at];
        for _ in 1..Period::ALL.len() {
            at = at.next();
            seen.push(at);
        }
        assert_eq!(seen, Period::ALL.to_vec());
        assert_eq!(at.next(), Period::default(), "the cycle does not close");

        assert_eq!(Heat::default(), Heat::Pnl);
        assert_eq!(Heat::Pnl.next(), Heat::Weight);
        assert_eq!(Heat::Weight.next(), Heat::Pnl);
        assert_ne!(Heat::Pnl.label(), Heat::Weight.label());
    }

    #[test]
    fn the_footers_split_leaves_the_curve_the_columns_the_chart_asks_for() {
        // `CURVE_W` is spelled here and derived in `braille_chart`, and the two
        // going out of step is silent in both directions: too low and the split
        // hands the curve a pane the chart refuses, too high and the footer
        // refuses a curve that would have drawn. So the constant is checked
        // against the widget it is a promise about.
        let equity = [9987.1, 10012.4, 10000.0];
        let drawn = |w: u16| {
            let mut term =
                ratatui::Terminal::new(ratatui::backend::TestBackend::new(w, 9)).unwrap();
            term.draw(|f| {
                braille_chart::draw(
                    f,
                    Rect::new(0, 0, w, 9),
                    braille_chart::Chart {
                        name: "equity curve",
                        series: &equity,
                        crosshair: None,
                        label: format::money,
                    },
                )
            })
            .unwrap();
            let buf = term.backend().buffer().clone();
            (0..9)
                .flat_map(|y| (0..w).map(move |x| (x, y)))
                .map(|(x, y)| buf[(x, y)].symbol().to_string())
                .collect::<String>()
        };
        assert!(
            drawn(CURVE_W).contains("$10,012.40"),
            "the footer's floor is under the chart's own"
        );
        assert!(
            drawn(CURVE_W - 1).contains("needs"),
            "the footer's floor is above the chart's own"
        );
        assert_eq!(BOTTOM_W, RAIL_W + 1 + CURVE_W);
    }

    #[test]
    fn the_movers_are_the_ends_of_the_book_and_a_book_of_one_is_only_itself() {
        let rows = |keys: Vec<Option<f64>>| -> Vec<BlotterRow<'static>> {
            keys.into_iter()
                .map(|pct| BlotterRow {
                    ticker: Some("X"),
                    qty: None,
                    last: None,
                    avg: None,
                    weight: None,
                    value: None,
                    pnl: None,
                    pnl_pct: pct,
                    history: None,
                })
                .collect()
        };
        assert_eq!(
            movers(&rows(vec![Some(0.1), Some(-0.2), Some(0.05)])),
            Some((0, 1))
        );
        // One holding is its own best and worst; two identical rows would read
        // as two movers, which is a desk this client is not looking at.
        assert_eq!(movers(&rows(vec![Some(0.1)])), Some((0, 0)));
        // Two positions that tie are still two movers.
        assert_eq!(movers(&rows(vec![Some(0.0), Some(0.0)])), Some((1, 0)));
        // A row the owner sent no percentage for is not a mover at either end —
        // absent is not the worst holding in the book.
        assert_eq!(movers(&rows(vec![None, Some(0.1)])), Some((1, 1)));
        assert_eq!(movers(&rows(vec![None, None])), None);
        assert_eq!(movers(&[]), None);
    }

    #[test]
    fn a_tile_loses_its_last_digit_and_never_its_sign() {
        // The same clamp the blotter's cells take, at the one place on the rail
        // where a number meets a fixed column. `-123.4%` is seven characters in
        // six cells, and ratatui drops the *leading* one from a right-aligned
        // run — which here is the minus, turning a wipeout into a doubling.
        let row = BlotterRow {
            ticker: Some("TOOLONG"),
            qty: None,
            last: None,
            avg: None,
            weight: Some(0.4),
            value: None,
            pnl: None,
            pnl_pct: Some(-1.2345),
            history: None,
        };
        let text = |row: &BlotterRow, mode| heat_tile(row, mode).content.to_string();
        assert_eq!(text(&row, Heat::Pnl), "TOOLO-123.4");
        assert_eq!(text(&row, Heat::Pnl).chars().count(), HOLD_W as usize);
        // The weight side is unsigned — its direction is the blotter's column.
        assert_eq!(text(&row, Heat::Weight), "TOOLO 40.0%");
    }

    #[test]
    fn a_tile_shades_nothing_the_owner_did_not_measure() {
        // `NaN > 0.0` is false, so a percentage nobody computed took the
        // *negative* ramp and rendered `--` on a red background — a loss the
        // desk never had, in the half of the cell a reader trusts before the
        // text. Unreachable from the owner's JSON, which cannot carry a NaN;
        // reachable from arithmetic this client may grow later.
        let t = theme();
        let row = BlotterRow {
            ticker: Some("SPY"),
            qty: None,
            last: None,
            avg: None,
            weight: Some(f64::NAN),
            value: None,
            pnl: None,
            pnl_pct: Some(f64::NAN),
            history: None,
        };
        let unmeasured = Style::default().fg(t.text_secondary);
        for mode in [Heat::Pnl, Heat::Weight] {
            let tile = heat_tile(&row, mode);
            assert!(tile.content.contains(MISSING), "{mode:?}: {tile:?}");
            assert_eq!(tile.style, unmeasured, "{mode:?} shaded an absent number");
        }
    }

    #[test]
    fn the_ribbon_floor_is_the_narrowest_pane_where_every_cell_clears() {
        // The constant against the solver that produces it. Spelled as
        // `max(min / ratio)` the floor is 75, one column from where the refusal
        // actually bites — the ratios are solved rather than multiplied, and the
        // solver rounds a cell up as often as down.
        let clears = |width: u16| {
            split(Rect::new(0, 0, width, RIBBON_H))
                .iter()
                .zip(CELL_MINS)
                .all(|(cell, min)| cell.width >= min)
        };
        assert!(clears(RIBBON_W), "the floor refuses itself");
        assert!(
            !clears(RIBBON_W - 1),
            "a pane under the floor clears every cell, so the floor is too high"
        );
        // And nothing narrower sneaks through: the solver is not monotone in the
        // way a division is, so the boundary is checked rather than assumed.
        for width in 0..RIBBON_W {
            assert!(!clears(width), "{width} cleared below the floor");
        }
    }
}
