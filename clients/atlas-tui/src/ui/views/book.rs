//! BOOK — the desk's own numbers, stated once, at the top of the view.
//!
//! The ribbon is the workstation's single account of its headline KPIs: what the
//! book is worth, what it is up or down, what the window did, and how it is
//! positioned. Everything the plan hangs under it — the blotter (Task 12), the
//! allocation heatmap and the equity chart (Task 13) — reads positions and
//! series, never these aggregates. One panel repeating the equity is a second
//! account of it, and two accounts of one number is how a desk ends up trusting
//! neither.
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
use crate::fx::FlashTracker;
use crate::model::{LivePortfolio, Metrics, Performance, Position};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
// The ribbon is one panel — one rule under a band of four cells — so it takes
// `panel_block` and heads its cells with `label` rather than `panel_header`:
// four amber bars across one band would read as four panels.
use crate::ui::widgets::panel_block;
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
    Frame,
};
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

pub struct BookView;

impl View for BookView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        let rows = Layout::vertical([Constraint::Length(RIBBON_H), Constraint::Min(0)]).split(area);
        draw_ribbon(f, rows[0], store);
        draw_rest(f, rows[1]);
    }

    fn on_key(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
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
                pnl.map(|v| t.change(v)).unwrap_or(t.text_secondary),
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
                change.map(|v| t.change(v)).unwrap_or(t.text_secondary),
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
fn gainers_and_losers(positions: &[Position]) -> (usize, usize) {
    positions
        .iter()
        .filter_map(|p| p.unrealized_pnl)
        .fold((0, 0), |(up, down), pnl| {
            if pnl > 0.0 {
                (up + 1, down)
            } else if pnl < 0.0 {
                (up, down + 1)
            } else {
                (up, down)
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

/// What the rest of the view is, while it is not built yet.
///
/// Named rather than left empty: for as long as this branch is half-built,
/// "nothing here yet" and "this pane failed to draw" have to be distinguishable
/// at a glance. Wrapped, because at 100 columns the unwrapped line is cut to
/// `…and equi` — a half-sentence reads as a rendering fault, which is the one
/// thing this line exists to rule out.
fn draw_rest(f: &mut Frame, area: Rect) {
    f.render_widget(
        Paragraph::new(vec![
            Line::from(""),
            Line::from(Span::styled(
                "positions blotter — Task 12 · allocation heatmap and equity chart — Task 13",
                Style::default().fg(theme().text_dim),
            )),
        ])
        .wrap(Wrap { trim: true }),
        area,
    );
}

/// A ribbon refusing to draw, and saying what it would take.
///
/// Wrapped, because a pane too narrow to hold the numbers is also too narrow to
/// hold the sentence about them, and a remedy cut off mid-word is one an
/// operator cannot act on.
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
