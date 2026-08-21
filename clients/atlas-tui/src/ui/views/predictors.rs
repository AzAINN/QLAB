//! PREDICTORS — the full board: every evaluated model, in the owner's ranking.
//!
//! RESEARCH keeps the one-row readout; this pane is the evaluation itself, for
//! the operator asking whether the quantum feature augmentation is earning its
//! place. Everything drawn here is the owner's own arithmetic and the owner's
//! own prose: the verdict sentence, the lane explainer, the champion call and
//! the |t| >= 2 convention all arrive on the payload, because two wordings of
//! an admission verdict would drift and the drifted one would be trusted.
//!
//! The rows render in the owner's ranking order, un-re-sorted, for the same
//! reason RESEARCH does not re-sort the leaderboard. Models here are visible
//! and not runnable from this client: running the board is the owner's own
//! governed tool (`research.predictor_board`), held by the desk's agents, and
//! this pane is where an operator reads what that produced.
//!
//! Nothing here scrolls. What does not fit is counted rather than dropped.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::{PredictorDetail, PredictorRow};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::table_cell::{cell, head, LEFT, RIGHT};
use crate::ui::widgets::{panel_block, panel_header, refuse, tristate_spark};
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Row, Table, Wrap},
    Frame,
};
use std::time::Instant;

/// The nine columns: the model, which lane of the experiment it sits in, its
/// six numbers, and the owner's marks.
///
/// `MODEL` is 18 because the longest id the board can serve is
/// `groupwise:angle_zz` (`research/board.py::MODEL_IDS`); at 17 it would render
/// without its final variant character, which is the character that
/// distinguishes it from `groupwise:angle`. `Δ IC` and `MEAN IC` are seven for
/// a sign, a zero, a point and three decimals — three because the admission
/// bar is 0.03 and two would round the gate itself away.
const COLS: [(&str, u16, bool); 9] = [
    ("MODEL", 18, LEFT),
    ("LANE", 5, LEFT),
    ("MEAN IC", 7, RIGHT),
    ("STD", 6, RIGHT),
    ("Δ IC", 7, RIGHT),
    ("t", 6, RIGHT),
    // Wins over the baseline and negative folds, one cell: both are counts
    // out of the same folds, and two four-wide columns did not fit beside
    // the spark — the desk pane is 77 columns after the rails.
    ("W/N", 5, RIGHT),
    // The fold-by-fold IC shape. Folds that change sign are not a skill
    // estimate, and a sign flip is visible in a spark long before it is
    // legible in a standard deviation.
    ("FOLDS", 8, LEFT),
    ("", 7, LEFT),
];

/// The pane's floor, derived from the columns so the two cannot drift.
const TABLE_W: u16 = table_w();

const fn table_w() -> u16 {
    let mut w = 0;
    let mut i = 0;
    while i < COLS.len() {
        w += COLS[i].1 + 1;
        i += 1;
    }
    w - 1
}

/// Nothing to retain: no cursor, no page, no field. The fetch that fills the
/// pane is the runtime's, fired on arriving here — see `main::ingest`.
#[derive(Default)]
pub struct PredictorsView;

impl View for PredictorsView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        if area.width < TABLE_W || area.height < 10 {
            refuse(
                f,
                area,
                format!(
                    "PREDICTORS needs {TABLE_W} columns to render the board without \
                     clipping a sign; this pane has {}.",
                    area.width
                ),
            );
            return;
        }

        let block = panel_block();
        let inner = block.inner(area);
        f.render_widget(block, area);

        let Some(board) = store.predictor_detail() else {
            // Not-asked-yet and desk-has-no-board are different facts. This
            // one is the client's — the fetch fires on arriving here — so the
            // remedy named is the client's own key, not an owner command.
            f.render_widget(
                Paragraph::new(vec![
                    panel_header("predictor board"),
                    Line::from(""),
                    Line::from(Span::styled(
                        "asking the owner for the board — r asks again if this persists",
                        Style::default().fg(theme().text_dim),
                    )),
                ]),
                inner,
            );
            return;
        };

        // A board that never ran or cannot be read carries its own sentence,
        // and the sentence is the whole pane: drawing an empty table under it
        // would render "no evaluation" as an evaluation of nothing.
        if board.status.as_deref() != Some("ok") {
            f.render_widget(
                Paragraph::new(vec![
                    panel_header("predictor board"),
                    Line::from(""),
                    Line::from(Span::styled(
                        format::text(board.reason.as_ref())
                            .unwrap_or("the owner served a board with no status")
                            .to_string(),
                        Style::default().fg(status_tone(board)),
                    )),
                ])
                .wrap(Wrap { trim: true }),
                inner,
            );
            return;
        }

        // The verdict gets the rows the table does not need, and the lane
        // explainer takes what is left: the sentence that says whether this
        // is evidence outranks the paragraph that says what the lanes are.
        let table_rows = (board.models.len() as u16 + 1).min(12);
        let bar_rows = match board.models.len() {
            0 => 0,
            n => (n as u16 + 2).min(11),
        };
        let rows = Layout::vertical([
            Constraint::Length(2),
            Constraint::Length(verdict_h(board, inner.width)),
            Constraint::Length(table_rows),
            Constraint::Length(bar_rows),
            Constraint::Min(0),
        ])
        .split(inner);

        f.render_widget(Paragraph::new(header_lines(board, inner.width)), rows[0]);
        f.render_widget(
            Paragraph::new(verdict_line(board)).wrap(Wrap { trim: true }),
            rows[1],
        );
        draw_table(f, rows[2], board);
        draw_delta_bars(f, rows[3], board);
        f.render_widget(
            Paragraph::new(footer_lines(board)).wrap(Wrap { trim: true }),
            rows[4],
        );
    }

    // Read-only, like RESEARCH: every key pressed here belongs to whoever
    // claims it next. The board is re-asked by the shell's own `r`.
    fn on_key(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

/// The pane header: what this board is, and the run it came from.
fn header_lines(board: &PredictorDetail, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let note = format!(
        "as of {} · run {}",
        format::text(board.as_of.as_ref()).unwrap_or(MISSING),
        head(
            format::text(board.run_id.as_ref())
                .unwrap_or(MISSING)
                .to_string(),
            10
        ),
    );
    let mut spans = panel_header("predictor board").spans;
    let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
    let pad = (width as usize).saturating_sub(used + note.chars().count());
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(note, Style::default().fg(t.text_dim)));

    let spec = Line::from(Span::styled(
        format!(
            "target {} · horizon {}d · embargo {}d · {} obs · {} folds",
            format::text(board.target.as_ref()).unwrap_or(MISSING),
            count(board.horizon_days),
            count(board.embargo_days),
            count(board.n_obs),
            count(board.n_folds),
        ),
        Style::default().fg(t.text_tertiary),
    ));
    vec![Line::from(spans), spec]
}

/// The owner's verdict sentence, verbatim, toned by the board's own flags.
fn verdict_line(board: &PredictorDetail) -> Line<'static> {
    Line::from(Span::styled(
        format::text(board.reason.as_ref())
            .unwrap_or("the board stated no verdict")
            .to_string(),
        Style::default().fg(verdict_tone(board)),
    ))
}

/// Rows the verdict needs at this width, so a long "NOT ESTABLISHED" sentence
/// is never silently clipped to its optimistic first clause.
fn verdict_h(board: &PredictorDetail, width: u16) -> u16 {
    let chars = board.reason.as_deref().map(str::len).unwrap_or(24);
    (chars as u16).div_ceil(width.max(1)).clamp(1, 4) + 1
}

/// What the verdict's flags mean, in colour.
///
/// `champion_established: None` with an admitted champion takes the warning
/// tone, not the positive one: "not tested against the selection null" must
/// not render as "passed it".
fn verdict_tone(board: &PredictorDetail) -> Color {
    let t = theme();
    match (board.admitted_any, board.champion_established) {
        (Some(true), Some(true)) => t.positive,
        (Some(true), _) => t.warning,
        _ => t.negative,
    }
}

/// `never_ran` is a fact about the desk; anything else unreadable is a broken
/// contract and takes the warning tone.
fn status_tone(board: &PredictorDetail) -> Color {
    let t = theme();
    match board.status.as_deref() {
        Some("never_ran") => t.text_dim,
        _ => t.warning,
    }
}

/// The board itself, one row per model, in the owner's ranking order.
fn draw_table(f: &mut Frame, area: Rect, board: &PredictorDetail) {
    let t = theme();
    let header = Row::new(
        COLS.map(|(name, width, right)| cell(name.to_string(), Style::default(), right, width)),
    )
    .style(Style::default().fg(t.text_secondary));

    let room = area.height.saturating_sub(1) as usize;
    let total = board.models.len();
    let shown = match total <= room {
        true => total,
        false => room.saturating_sub(1),
    };
    let table = Table::new(
        board.models.iter().take(shown).map(model_row),
        COLS.map(|(_, w, _)| Constraint::Length(w)),
    )
    .header(header)
    .column_spacing(1);
    f.render_widget(table, area);
    if shown < total {
        let at = area.y + 1 + shown as u16;
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!("… {} more", total - shown),
                Style::default().fg(t.text_dim),
            ))),
            Rect::new(area.x, at, area.width, 1),
        );
    }
}

/// One model's row: its id, its lane, its numbers, and the owner's marks.
fn model_row(row: &PredictorRow) -> Row<'static> {
    let t = theme();
    let (lane, lane_tone) = lane_word(row);
    let cells: [(String, Style); COLS.len()] = [
        (
            format::text(row.model_id.as_ref())
                .unwrap_or(MISSING)
                .to_string(),
            Style::default().fg(t.text_primary),
        ),
        (lane.to_string(), Style::default().fg(lane_tone)),
        toned(row.mean_ic),
        (
            row.ic_std
                .map(|v| decimals(v, 3))
                .unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_secondary),
        ),
        toned_signed(row.delta_mean_ic_vs_baseline),
        toned_at(row.paired_t_vs_baseline, 2),
        (
            format!(
                "{}/{}",
                row.wins_vs_baseline
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| MISSING.to_string()),
                row.negative_folds
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| MISSING.to_string()),
            ),
            Style::default().fg(t.text_secondary),
        ),
        fold_spark(row),
        mark(row),
    ];
    Row::new(
        cells
            .into_iter()
            .zip(COLS)
            .map(|((text, style), (_, width, right))| cell(text, style, right, width)),
    )
}

/// The per-fold ICs as an eight-level spark, toned by the delta's own sign.
///
/// The quantizer is the desk's one (`tristate_spark`): two spellings of "the
/// tail of a series" is how bars and colour once came to disagree. An empty
/// series is absent, not flat — the summary payload serves fold dicts and the
/// detail serves plain numbers, and a decode drift would land here first.
fn fold_spark(row: &PredictorRow) -> (String, Style) {
    let t = theme();
    if row.per_fold.is_empty() {
        return (MISSING.to_string(), Style::default().fg(t.text_tertiary));
    }
    let tone = match row.delta_mean_ic_vs_baseline {
        Some(d) => format::change_tone(d, 3),
        None => t.text_secondary,
    };
    (
        tristate_spark::glyphs(&row.per_fold, 8),
        Style::default().fg(tone),
    )
}

/// Δ IC vs the baseline, as magnitude bars in the owner's ranking order.
///
/// The table's Δ column says the number; this pane says the *proportion* — a
/// mapped model earning a third of what another loses is legible at a glance
/// here and only at a squint in a column of signed decimals. Scaled to the
/// board's own largest |Δ|, and the baseline row is the axis itself.
fn draw_delta_bars(f: &mut Frame, area: Rect, board: &PredictorDetail) {
    if area.height < 2 {
        return;
    }
    let t = theme();
    let widest = board
        .models
        .iter()
        .filter_map(|m| m.delta_mean_ic_vs_baseline)
        .map(f64::abs)
        .fold(0.0_f64, f64::max);
    let mut lines = vec![Line::from(Span::styled(
        format!(
            "Δ IC vs {}",
            format::text(board.baseline.as_ref()).unwrap_or(MISSING)
        ),
        Style::default().fg(t.text_secondary),
    ))];
    let room = area.height.saturating_sub(1) as usize;
    for row in board.models.iter().take(room) {
        lines.push(delta_bar_line(row, widest));
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// One model's bar: id, magnitude, signed value — toned by what is printed.
fn delta_bar_line(row: &PredictorRow, widest: f64) -> Line<'static> {
    let t = theme();
    const BAR_W: usize = 24;
    let id = format!(
        "{:<19}",
        head(
            format::text(row.model_id.as_ref())
                .unwrap_or(MISSING)
                .to_string(),
            18
        )
    );
    let Some(delta) = row.delta_mean_ic_vs_baseline else {
        return Line::from(vec![
            Span::styled(id, Style::default().fg(t.text_primary)),
            Span::styled(MISSING.to_string(), Style::default().fg(t.text_tertiary)),
        ]);
    };
    if row.is_baseline == Some(true) {
        return Line::from(vec![
            Span::styled(id, Style::default().fg(t.text_primary)),
            Span::styled(
                format!("{:<BAR_W$} baseline", "▏", BAR_W = BAR_W),
                Style::default().fg(t.text_dim),
            ),
        ]);
    }
    // Printed-value discipline, same as every signed cell: dust that rounds
    // to 0.000 draws the axis tick, not a one-cell bar claiming an edge.
    let printed = signed(delta, 3);
    let cells = if widest > 0.0 && !printed.chars().all(|c| c == '0' || c == '.') {
        ((delta.abs() / widest) * BAR_W as f64).round().max(1.0) as usize
    } else {
        0
    };
    let bar = format!("{:<BAR_W$}", "█".repeat(cells.min(BAR_W)), BAR_W = BAR_W);
    Line::from(vec![
        Span::styled(id, Style::default().fg(t.text_primary)),
        Span::styled(bar, Style::default().fg(format::change_tone(delta, 3))),
        Span::styled(
            format!(" {printed}"),
            Style::default().fg(format::change_tone(delta, 3)),
        ),
    ])
}

/// Which arm of the experiment a model sits in, as the owner filed it.
///
/// Read off `augmented` rather than the family: `kernel:linear` is a control
/// in the kernel family, and a lane inferred from the id would file it in the
/// treatment arm — the exact mistake the payload's `control_note` documents.
fn lane_word(row: &PredictorRow) -> (&'static str, Color) {
    let t = theme();
    match row.augmented {
        Some(true) => ("quant", t.accent),
        Some(false) => ("ctrl", t.text_dim),
        None => (MISSING, t.text_tertiary),
    }
}

/// The owner's marks on a row. `★CHAMP` outranks `BASE` outranks `sig`:
/// exactly one model can be champion, the baseline cannot be it, and a
/// significant loser is still worth a mark — |t| >= 2 *below* the baseline is
/// the augmentation measurably costing accuracy.
fn mark(row: &PredictorRow) -> (String, Style) {
    let t = theme();
    if row.is_champion == Some(true) {
        return (
            "★CHAMP".to_string(),
            Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
        );
    }
    if row.is_baseline == Some(true) {
        return ("BASE".to_string(), Style::default().fg(t.text_secondary));
    }
    match row.significant {
        Some(true) => ("sig".to_string(), Style::default().fg(t.cyan)),
        _ => (String::new(), Style::default()),
    }
}

/// Caveats first — the owner's own honesty — then the lane explainer.
fn footer_lines(board: &PredictorDetail) -> Vec<Line<'static>> {
    let t = theme();
    let mut lines = Vec::new();
    for caveat in &board.caveats {
        lines.push(Line::from(Span::styled(
            format!("· {caveat}"),
            Style::default().fg(t.warning),
        )));
    }
    if let Some(lane) = format::text(board.lane.as_ref()) {
        if !lines.is_empty() {
            lines.push(Line::from(""));
        }
        lines.push(Line::from(Span::styled(
            lane.to_string(),
            Style::default().fg(t.text_dim),
        )));
    }
    lines
}

/// An IC cell, toned on the printed value.
fn toned(value: Option<f64>) -> (String, Style) {
    let t = theme();
    match value {
        Some(v) => (
            decimals(v, 3),
            Style::default().fg(format::change_tone(v, 3)),
        ),
        None => (MISSING.to_string(), Style::default().fg(t.text_tertiary)),
    }
}

/// A delta cell: signed by what is printed, because `0.031` beside a baseline
/// does not say which side of it the model sits on.
fn toned_signed(value: Option<f64>) -> (String, Style) {
    let t = theme();
    match value {
        Some(v) => (signed(v, 3), Style::default().fg(format::change_tone(v, 3))),
        None => (MISSING.to_string(), Style::default().fg(t.text_tertiary)),
    }
}

/// A t-statistic cell at its own precision.
fn toned_at(value: Option<f64>, dp: usize) -> (String, Style) {
    let t = theme();
    match value {
        Some(v) => (
            signed(v, dp),
            Style::default().fg(format::change_tone(v, dp)),
        ),
        None => (MISSING.to_string(), Style::default().fg(t.text_tertiary)),
    }
}

/// A count that may be absent. Absent is not zero: a board with no stated fold
/// count has not said "no folds".
fn count(value: Option<i64>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| MISSING.to_string())
}

/// A plain decimal, signed by what is printed rather than by the double —
/// `{:.3}` on -1e-13 would draw a minus sign on a zero.
fn decimals(value: f64, dp: usize) -> String {
    if !value.is_finite() {
        return MISSING.to_string();
    }
    let digits = format!("{:.*}", dp, value.abs());
    match format::negative_at(value, dp) {
        true => format!("-{digits}"),
        false => digits,
    }
}

/// An explicit `+` on the positive side, signed by what is printed: a value
/// that rounds away prints `0.000` and carries no sign in either direction —
/// `+` on a zero would claim an edge the precision cannot see.
fn signed(value: f64, dp: usize) -> String {
    let printed = decimals(value, dp);
    let zero = printed.chars().all(|c| c == '0' || c == '.');
    match printed.starts_with('-') || zero || !value.is_finite() {
        true => printed,
        false => format!("+{printed}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_table_floor_is_the_columns_own_arithmetic() {
        let columns: u16 = COLS.iter().map(|(_, w, _)| w).sum();
        assert_eq!(TABLE_W, columns + COLS.len() as u16 - 1);
    }

    /// The longest id `research/board.py::MODEL_IDS` can serve, verbatim. At
    /// one column narrower it loses the character that distinguishes it from
    /// `groupwise:angle`.
    const LONGEST_MODEL: &str = "groupwise:angle_zz";

    #[test]
    fn the_model_column_holds_the_boards_longest_id() {
        let width = COLS[0].1;
        assert!(LONGEST_MODEL.chars().count() <= width as usize);
        assert_eq!(head(LONGEST_MODEL.to_string(), width), LONGEST_MODEL);
    }

    #[test]
    fn every_metric_column_fits_its_widest_honest_rendering() {
        // Right-aligned cells lose their *leading* characters when they do not
        // fit — the sign first, which inverts the number.
        let width = |name: &str| COLS.iter().find(|(n, _, _)| *n == name).unwrap().1 as usize;
        assert!(decimals(-0.123, 3).chars().count() <= width("MEAN IC"));
        assert!(signed(-0.123, 3).chars().count() <= width("Δ IC"));
        assert!(signed(-12.34, 2).chars().count() <= width("t"));
        assert!(decimals(-0.123, 3).chars().count() <= width("STD"));
    }

    #[test]
    fn the_lane_is_the_owners_filing_not_the_familys() {
        // `kernel:linear`: kernel family, no feature map. The lane must come
        // off `augmented`, or a control renders in the treatment arm.
        let control = PredictorRow {
            family: Some("kernel".into()),
            augmented: Some(false),
            ..Default::default()
        };
        assert_eq!(lane_word(&control).0, "ctrl");
        let mapped = PredictorRow {
            augmented: Some(true),
            ..Default::default()
        };
        assert_eq!(lane_word(&mapped).0, "quant");
        // Unstated is unstated — not a control by default.
        assert_eq!(lane_word(&PredictorRow::default()).0, MISSING);
    }

    #[test]
    fn a_champion_mark_outranks_baseline_outranks_significance() {
        let champion = PredictorRow {
            is_champion: Some(true),
            is_baseline: Some(false),
            significant: Some(true),
            ..Default::default()
        };
        assert_eq!(mark(&champion).0, "★CHAMP");
        let baseline = PredictorRow {
            is_baseline: Some(true),
            significant: Some(true),
            ..Default::default()
        };
        assert_eq!(mark(&baseline).0, "BASE");
        // A significant *loser* still earns the mark: |t| >= 2 below the
        // baseline is the augmentation measurably costing accuracy.
        let loser = PredictorRow {
            significant: Some(true),
            delta_mean_ic_vs_baseline: Some(-0.05),
            ..Default::default()
        };
        assert_eq!(mark(&loser).0, "sig");
        assert_eq!(mark(&PredictorRow::default()).0, "");
    }

    #[test]
    fn not_tested_against_the_null_does_not_render_as_passed() {
        let t = theme();
        let tested = PredictorDetail {
            admitted_any: Some(true),
            champion_established: Some(true),
            ..Default::default()
        };
        assert_eq!(verdict_tone(&tested), t.positive);
        // Admitted with the null untested (or withheld) is the warning tone.
        let untested = PredictorDetail {
            admitted_any: Some(true),
            champion_established: None,
            ..Default::default()
        };
        assert_eq!(verdict_tone(&untested), t.warning);
        let refuted = PredictorDetail {
            admitted_any: Some(true),
            champion_established: Some(false),
            ..Default::default()
        };
        assert_eq!(verdict_tone(&refuted), t.warning);
        let nothing = PredictorDetail {
            admitted_any: Some(false),
            ..Default::default()
        };
        assert_eq!(verdict_tone(&nothing), t.negative);
    }

    #[test]
    fn a_long_verdict_is_given_rows_rather_than_clipped() {
        // The pessimistic clause arrives *after* the optimistic one in the
        // owner's sentence, so clipping is not neutral: it keeps the good news.
        let board = PredictorDetail {
            reason: Some("x".repeat(300)),
            ..Default::default()
        };
        assert_eq!(verdict_h(&board, 100), 4);
        let short = PredictorDetail {
            reason: Some("admitted".into()),
            ..Default::default()
        };
        assert_eq!(verdict_h(&short, 100), 2);
    }

    #[test]
    fn signs_are_decided_on_what_is_printed() {
        assert_eq!(signed(0.031, 3), "+0.031");
        assert_eq!(signed(-0.031, 3), "-0.031");
        // Dust rounds to zero and must not carry a sign either way.
        assert_eq!(signed(-1e-13, 3), "0.000");
        assert_eq!(decimals(f64::NAN, 3), MISSING);
    }
}
