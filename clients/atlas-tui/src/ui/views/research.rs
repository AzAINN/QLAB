//! RESEARCH — the ablation's ranking, the run ledger, and the staged catalog.
//!
//! Three panes carrying what the Textual reference view carried, because this
//! one replaces it: a surface that quietly stopped existing at the cutover
//! would be a governance record an operator can no longer reach.
//!
//! The leaderboard is the owner's own ranking, in the owner's own order. It
//! sorts arms it could not score *last* rather than dropping them, and this
//! client does not re-sort — a second ordering rule here would need an answer
//! for the rows with no number, and any answer it invented would be a claim
//! about an arm the ablation never made.
//!
//! The catalog draws `stage` beside every id because the stage is not a label:
//! `algorithms.solve` enforces it in code, so a research or offline entry is
//! visible and not agent-runnable. A list of ids alone would show nineteen
//! methods and imply the desk can run all of them.
//!
//! Nothing here scrolls. What does not fit is *counted* rather than dropped, so
//! a pane cannot read as a shorter history than the desk has.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::{Algorithm, LeaderboardRow, RunSpec};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::table_cell::{cell, head, LEFT, RIGHT};
use crate::ui::widgets::{panel_block, panel_header, refuse};
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Row, Table},
    Frame,
};
use std::time::Instant;

/// The seven columns: the method, the mark that says what kind of arm it is,
/// and the five metrics the owner judges arms on.
///
/// The five are `OVERLAY_METRICS` in the owner's reading order — one definition
/// on that side, and a sixth column here would be a metric the desk does not
/// judge arms on. Left for the name, right for every number, because a column
/// of numbers only reads as a column when the decimals line up.
///
/// `RET` and `MAXDD` are seven because both are routinely negative and can be
/// double-digit: `-11.3%` needs six and the header needs five. `CVAR95` is
/// seven for the same reason at two decimals.
///
/// `METHOD` is 24 because the owner's longest arm name is
/// `Equal risk contribution` at 23 (`core/reference.py::ARM_NAMES`). At 22 it
/// rendered `Equal risk contributio`, which a live capture caught and no
/// fixture would have — the fixture's names are all short.
const COLS: [(&str, u16, bool); 7] = [
    ("METHOD", 24, LEFT),
    ("", 5, LEFT),
    ("SHARPE", 6, RIGHT),
    ("RET", 7, RIGHT),
    ("MAXDD", 7, RIGHT),
    ("CVAR95", 7, RIGHT),
    ("DSR", 5, RIGHT),
];

/// The leaderboard's floor, derived from the columns so the two cannot drift.
const BOARD_W: u16 = board_w();

const fn board_w() -> u16 {
    let mut w = 0;
    let mut i = 0;
    while i < COLS.len() {
        w += COLS[i].1 + 1;
        i += 1;
    }
    w - 1
}

/// The run ledger's share of the bottom band: an id prefix, a kind, and a
/// clock. A fixed width rather than a ratio — the catalog beside it holds ids
/// up to `mvsk_ising_resource_estimator`, and a ratio would take those cells
/// from whichever pane the terminal happened to be narrow for.
const RUNS_W: u16 = 34;

/// The catalog's floor: the longest id in the owner's catalog plus its stage
/// chip and a space.
const CATALOG_MIN: u16 = 30 + 1 + 11;

/// The bottom band's floor: a header, a rule, and enough rows to be a list
/// rather than a hint.
const BOTTOM_MIN: u16 = 8;

/// Nothing to retain: no cursor, no page, no field. Every pane is a rendering
/// of what the owner said.
#[derive(Default)]
pub struct ResearchView;

impl View for ResearchView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        // Below the floor ratatui shrinks the columns underneath the widths
        // every cell was held to, and a right-aligned cell loses its *leading*
        // characters — the sign first. A return of `-11.3%` drawn as `11.3%` is
        // a loss rendered as a gain, so the pane refuses instead.
        if area.width < BOARD_W || area.height < BOTTOM_MIN + 5 {
            refuse(
                f,
                area,
                format!(
                    "RESEARCH needs {BOARD_W} columns to render the five metrics without \
                     clipping a sign; this pane has {}.",
                    area.width
                ),
            );
            return;
        }

        // The leaderboard takes exactly the rows it has, and the bottom band
        // takes the rest: the catalog is the pane that grows with the owner's
        // deployment, so it is the one that should get the spare rows.
        let arms = store.leaderboard().len().max(1) as u16;
        let board = (arms + 3).min(area.height.saturating_sub(BOTTOM_MIN + 1));
        // The forecast readout leads, on one row of its own. It is the only
        // admission verdict on this view — whether the desk may use the vol
        // forecast at all — and everything below it is evidence.
        let rows = Layout::vertical([
            Constraint::Length(1),
            Constraint::Length(board),
            Constraint::Min(0),
        ])
        .split(area);
        draw_forecast(f, rows[0], store);
        draw_board(f, rows[1], store);

        let left = RUNS_W.min(rows[2].width.saturating_sub(CATALOG_MIN + 1));
        let cols = Layout::horizontal([Constraint::Length(left), Constraint::Min(0)])
            .spacing(1)
            .split(rows[2]);
        draw_runs(f, cols[0], store);
        draw_catalog(f, cols[1], store);
    }

    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    //
    // This one claims none. Every pane is read-only and nothing selects, so a
    // key pressed here belongs to whoever claims it next.
    fn on_key(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

/// The vol forecast, and whether the desk may use it.
///
/// Parity with the Textual reference view's research summary. Left out of the
/// cutover this would be a research-admission signal that silently disappeared:
/// the IC says how much of next month's realized volatility the model explains
/// out of sample, and the verdict says whether anything downstream is allowed
/// to read it.
///
/// Three states rather than two, because "the desk has never forecast" and
/// "there is a run and this client cannot read its spec" have different
/// remedies and must not share a sentence.
fn draw_forecast(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let label = Span::styled(" vol forecast  ", Style::default().fg(t.text_secondary));
    let Some(run) = store
        .runs()
        .iter()
        .find(|run| run.kind.as_deref() == Some("prediction"))
    else {
        f.render_widget(
            Paragraph::new(Line::from(vec![
                label,
                Span::styled(
                    "no prediction run yet",
                    Style::default().fg(theme().text_dim),
                ),
            ])),
            area,
        );
        return;
    };
    let Some(spec) = run.spec.as_ref() else {
        // A run exists and its record is unreadable. Loud rather than folded
        // into "none yet": one is a desk that has not looked, the other is a
        // desk whose answer this client cannot read.
        f.render_widget(
            Paragraph::new(Line::from(vec![
                label,
                Span::styled(
                    format!(
                        "run {} carries no readable spec",
                        format::text(run.run_id.as_ref()).unwrap_or(MISSING)
                    ),
                    Style::default().fg(t.warning),
                ),
            ])),
            area,
        );
        return;
    };

    let admitted = admitted(spec);
    let mut spans = vec![
        label,
        Span::styled("IC ", Style::default().fg(t.text_tertiary)),
        // Three decimals, because the admission threshold is 0.03 and two would
        // round the gate itself away.
        match spec.mean_ic {
            Some(ic) => Span::styled(
                decimals(ic, 3),
                Style::default().fg(format::change_tone(ic, 3)),
            ),
            None => Span::styled(MISSING.to_string(), Style::default().fg(t.text_tertiary)),
        },
        Span::styled("  stability ", Style::default().fg(t.text_tertiary)),
        Span::styled(
            spec.ic_stability
                .map(|v| decimals(v, 3))
                .unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_secondary),
        ),
    ];
    // The word only when the run stated the threshold it would be judged
    // against. Calling a number stable against a gate nobody named would be
    // this client inventing the admission rule.
    if let Some(word) = stability_word(spec) {
        spans.push(Span::styled(
            format!(" ({word})"),
            Style::default().fg(t.text_secondary),
        ));
    }
    spans.push(Span::styled(
        if admitted {
            "  —  usable"
        } else {
            "  —  not usable"
        },
        Style::default()
            .fg(if admitted { t.positive } else { t.negative })
            .add_modifier(Modifier::BOLD),
    ));
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Whether the desk may use this forecast.
///
/// The owner's own `usable` flag **and** the run's own stated gate, cleared by
/// the run's own numbers. The thresholds are read off the payload rather than
/// carried here: the owner writes them into every spec, and a client holding
/// `0.03` and `0.5` of its own would keep asserting an old gate after the owner
/// moved it — on a research-admission signal, which is exactly the number that
/// must not drift quietly.
///
/// Absent evidence is not a pass. A `usable: true` with no IC behind it is a
/// verdict nothing supports, and this pane will not repeat it.
fn admitted(spec: &RunSpec) -> bool {
    if spec.usable != Some(true) {
        return false;
    }
    let (Some(ic), Some(stability)) = (spec.mean_ic, spec.ic_stability) else {
        return false;
    };
    let gate = spec.admission.clone().unwrap_or_default();
    gate.mean_ic_strictly_above.is_none_or(|floor| ic > floor)
        && gate
            .ic_stability_strictly_above
            .is_none_or(|floor| stability > floor)
}

/// `stable` or `unstable`, when the run named the threshold that decides it.
fn stability_word(spec: &RunSpec) -> Option<&'static str> {
    let floor = spec.admission.as_ref()?.ic_stability_strictly_above?;
    Some(match spec.ic_stability? > floor {
        true => "stable",
        false => "unstable",
    })
}

/// The newest ablation, ranked as the owner ranked it.
fn draw_board(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let arms = store.leaderboard();

    let head_rows = Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).split(inner);
    f.render_widget(
        Paragraph::new(titled(
            "leaderboard",
            &format!("newest ablation · {} arms", arms.len()),
            inner.width,
        )),
        head_rows[0],
    );

    if arms.is_empty() {
        // "No ablation yet" and "this pane is broken" must not look the same,
        // and the remedy is a command an operator can actually run.
        refuse(
            f,
            head_rows[1],
            "no ablation recorded yet — qlab batch configs/specs/ablation_v1.yaml --offline"
                .to_string(),
        );
        return;
    }

    let header = Row::new(
        COLS.map(|(name, width, right)| cell(name.to_string(), Style::default(), right, width)),
    )
    .style(Style::default().fg(t.text_secondary));

    // One row for the column header; what is left is the ranking. A row past
    // the end is counted rather than dropped — nothing here scrolls, and a
    // truncated ranking reads as a shorter ablation than the desk ran.
    let room = head_rows[1].height.saturating_sub(1) as usize;
    let shown = fits(arms.len(), room);
    let table = Table::new(
        arms.iter().take(shown).map(arm_row),
        COLS.map(|(_, w, _)| Constraint::Length(w)),
    )
    .header(header)
    .column_spacing(1);
    f.render_widget(table, head_rows[1]);
    if shown < arms.len() {
        let at = head_rows[1].y + 1 + shown as u16;
        f.render_widget(
            Paragraph::new(more(arms.len() - shown)),
            Rect::new(head_rows[1].x, at, head_rows[1].width, 1),
        );
    }
}

/// One ranked arm: what it is, what kind of arm it is, and its five numbers.
fn arm_row(arm: &LeaderboardRow) -> Row<'static> {
    let t = theme();
    let (mark, mark_tone) = match (arm.champion, arm.benchmark) {
        // The arm `mandate.operational_policy` names — the one the paper book
        // is actually run under. Exactly one row can wear it.
        (Some(true), _) => ("★", t.accent),
        // Not a champion, and not an ordinary arm either: it is what every
        // hypothesis has to beat.
        (_, Some(true)) => ("BENCH", t.text_dim),
        _ => ("", t.text_dim),
    };
    let cells: [(String, Style); COLS.len()] = [
        (
            format::text(arm.name.as_ref())
                .unwrap_or(MISSING)
                .to_string(),
            Style::default().fg(t.text_primary),
        ),
        (
            mark.to_string(),
            Style::default().fg(mark_tone).add_modifier(Modifier::BOLD),
        ),
        metric(arm.sharpe, ratio, Unit::Ratio, 2),
        metric(arm.ann_return, format::signed_pct1, Unit::Percent, 1),
        // A drawdown and a tail loss are losses by construction: colouring them
        // red would say nothing an operator does not already know from the
        // column header, and would leave the two columns that *do* carry a
        // direction indistinguishable from them.
        (
            arm.max_drawdown
                .map(format::pct1)
                .unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_secondary),
        ),
        (
            arm.cvar_95
                .map(format::signed_pct)
                .unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_secondary),
        ),
        (
            arm.deflated_sharpe
                .map(ratio)
                .unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_secondary),
        ),
    ];
    Row::new(
        cells
            .into_iter()
            .zip(COLS)
            .map(|((text, style), (_, width, right))| cell(text, style, right, width)),
    )
}

/// Whether a metric is a fraction the formatter will multiply out, or a raw
/// ratio that prints itself.
///
/// Named rather than inferred from the decimal count. `change_tone` has to be
/// handed the same expression the formatter prints — the whole point of it is
/// that the colour is decided on the printed value — and deriving that from
/// `dp` would be two facts that happen to agree today: a two-decimal percent
/// column would silently be toned as if it were a ratio, a factor of a hundred
/// out, and the only visible symptom would be the colour of small numbers.
#[derive(Debug, Clone, Copy)]
enum Unit {
    Ratio,
    Percent,
}

/// One metric cell, toned at the precision it is printed to.
///
/// The precision matters: dust magnitudes are real in a leaderboard, and an arm
/// at -1e-13 prints `0.00` — a cell painted red over a zero says the arm lost
/// money when what it did was round away.
fn metric(value: Option<f64>, show: fn(f64) -> String, unit: Unit, dp: usize) -> (String, Style) {
    let t = theme();
    let Some(value) = value else {
        // Absent is not zero and not neutral-because-flat: the ablation
        // produced no comparable number for this arm.
        return (MISSING.to_string(), Style::default().fg(t.text_tertiary));
    };
    let printed = match unit {
        Unit::Ratio => value,
        Unit::Percent => value * 100.0,
    };
    (
        show(value),
        Style::default().fg(format::change_tone(printed, dp)),
    )
}

/// A Sharpe or a deflated Sharpe: a ratio, not a percentage.
fn ratio(value: f64) -> String {
    decimals(value, 2)
}

/// A plain decimal, signed by what is *printed* rather than by the double.
///
/// `{:.2}` on -1e-13 prints `-0.00` — a minus sign drawn on a zero, which reads
/// as a small loss in a column an operator scans for direction. Two columns on
/// this view need it at two different precisions (the leaderboard's ratios, the
/// forecast's IC), so the rule is one function. `negative_at` is `format`'s own
/// rule, asked at the precision this cell prints at.
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

/// The research run ledger, newest first as the owner serves it.
fn draw_runs(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let runs = store.runs();
    let mut lines = vec![titled("runs", "newest first", inner.width)];
    let room = inner.height.saturating_sub(1) as usize;
    if runs.is_empty() {
        lines.push(absent("no research run in the registry yet"));
    }
    let shown = fits(runs.len(), room);
    for run in runs.iter().take(shown) {
        lines.push(Line::from(vec![
            Span::styled(
                format!("{:<11}", head(short(run.run_id.as_ref()), 10)),
                Style::default().fg(t.text_primary),
            ),
            Span::styled(
                format!("{:<12}", head(short(run.kind.as_ref()), 11)),
                Style::default().fg(t.cyan),
            ),
            Span::styled(
                format::clock(run.created_at.as_ref()).unwrap_or_else(|| MISSING.to_string()),
                Style::default().fg(t.text_tertiary),
            ),
        ]));
    }
    if shown < runs.len() {
        lines.push(more(runs.len() - shown));
    }
    f.render_widget(Paragraph::new(lines), inner);
}

/// The algorithm catalog, with the stage that decides what may run.
fn draw_catalog(f: &mut Frame, area: Rect, store: &Store) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let rows = store.algorithms();
    // The count an operator needs before reading any of the ids: most of this
    // catalog is evidence, not runtime.
    let operational = rows
        .iter()
        .filter(|a| a.stage.as_deref() == Some("operational"))
        .count();
    let mut lines = vec![titled(
        "catalog",
        &format!("{operational} of {} operational", rows.len()),
        inner.width,
    )];
    let room = inner.height.saturating_sub(1) as usize;
    if rows.is_empty() {
        lines.push(absent("the owner sent no algorithm catalog"));
    }
    let shown = fits(rows.len(), room);
    for spec in rows.iter().take(shown) {
        lines.push(catalog_row(spec, inner.width));
    }
    if shown < rows.len() {
        lines.push(more(rows.len() - shown));
    }
    f.render_widget(Paragraph::new(lines), inner);
}

/// One catalog entry: the id, and the stage in the stage's own colour.
fn catalog_row(spec: &Algorithm, width: u16) -> Line<'static> {
    let t = theme();
    let stage = format::text(spec.stage.as_ref()).unwrap_or(MISSING);
    let id_w = (width as usize).saturating_sub(12).max(1);
    Line::from(vec![
        Span::styled(
            format!(
                "{:<id_w$}",
                head(short(spec.id.as_ref()), id_w as u16),
                id_w = id_w
            ),
            Style::default().fg(t.text_primary),
        ),
        Span::styled(
            stage.to_uppercase(),
            Style::default().fg(stage_tone(stage)).add_modifier(
                // The one an agent may actually run is the one that reads as
                // available at a glance.
                match stage {
                    "operational" => Modifier::BOLD,
                    _ => Modifier::empty(),
                },
            ),
        ),
    ])
}

/// What each stage means, in colour.
///
/// Three tones for three stages, because the boundary is the point: green is
/// what `algorithms.solve` will run, amber is a hypothesis retained for honest
/// ablation, and dim is what is not in the staged runtime at all. A stage this
/// client has never heard of renders dimly rather than taking the theme's
/// default foreground and reading as a runnable one.
fn stage_tone(stage: &str) -> Color {
    let t = theme();
    match stage {
        "operational" => t.positive,
        "research" => t.accent,
        _ => t.text_dim,
    }
}

/// How many rows fit, leaving a row for the count when there are more.
///
/// A pane that drew every row it could and stopped would read as a shorter
/// history than the desk has, which is the one thing a ledger may not do.
fn fits(total: usize, room: usize) -> usize {
    match total <= room {
        true => total,
        false => room.saturating_sub(1),
    }
}

fn more(hidden: usize) -> Line<'static> {
    Line::from(Span::styled(
        format!("… {hidden} more"),
        Style::default().fg(theme().text_dim),
    ))
}

fn absent(what: &str) -> Line<'static> {
    Line::from(Span::styled(
        what.to_string(),
        Style::default().fg(theme().text_dim),
    ))
}

fn short(value: Option<&String>) -> String {
    format::text(value).unwrap_or(MISSING).to_string()
}

/// A panel header with its note pushed to the far side of the pane.
fn titled(title: &str, note: &str, width: u16) -> Line<'static> {
    let t = theme();
    let mut spans = panel_header(title).spans;
    let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
    let pad = (width as usize).saturating_sub(used + note.chars().count());
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(
        note.to_string(),
        Style::default().fg(t.text_dim),
    ));
    Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_board_floor_is_the_columns_own_arithmetic() {
        // The layout's guard and the column widths are one fact. Spelled twice,
        // a column widened for a longer method name would silently start
        // clipping the sign off a return.
        let columns: u16 = COLS.iter().map(|(_, w, _)| w).sum();
        assert_eq!(BOARD_W, columns + COLS.len() as u16 - 1);
    }

    /// The longest name the owner's `ARM_NAMES` can send, verbatim. A name
    /// column that clips is a method an operator has to guess at, and this one
    /// is the arm the champion is compared against.
    const LONGEST_ARM: &str = "Equal risk contribution";

    #[test]
    fn the_method_column_holds_the_owners_longest_arm_name() {
        let width = COLS[0].1 as usize;
        assert!(
            LONGEST_ARM.chars().count() <= width,
            "METHOD is {width} and {LONGEST_ARM:?} needs {}",
            LONGEST_ARM.chars().count()
        );
        // And the cell holds it whole rather than the `head` cut kicking in.
        assert_eq!(head(LONGEST_ARM.to_string(), COLS[0].1), LONGEST_ARM);
    }

    #[test]
    fn every_metric_column_fits_its_widest_honest_rendering() {
        // The failure this guards is silent and inverts the number: ratatui
        // right-aligns an overlong line by dropping its *leading* cells, so a
        // return that does not fit loses its sign first.
        let width = |name: &str| COLS.iter().find(|(n, _, _)| *n == name).unwrap().1 as usize;
        assert!(format::signed_pct1(-0.101).chars().count() <= width("RET"));
        assert!(format::pct1(-0.4217).chars().count() <= width("MAXDD"));
        assert!(format::signed_pct(-0.0311).chars().count() <= width("CVAR95"));
        assert!(ratio(-1.23).chars().count() <= width("SHARPE"));
    }

    #[test]
    fn an_unscored_arm_is_absent_rather_than_zero_or_flat() {
        let t = theme();
        let (text, style) = metric(None, ratio, Unit::Ratio, 2);
        assert_eq!(text, MISSING);
        assert_eq!(style.fg, Some(t.text_tertiary), "absent is not flat");
        // And a dust magnitude takes the tone of what is printed, not of the
        // double: an arm at -1e-13 prints `0.00`, and a red cell over a zero
        // says the arm lost money when what it did was round away.
        let (text, style) = metric(Some(-1e-13), ratio, Unit::Ratio, 2);
        assert_eq!(text, "0.00");
        assert_ne!(style.fg, Some(t.negative));
        // The neighbour that survives the rounding still reads as a loss.
        assert_eq!(
            metric(Some(-0.006), ratio, Unit::Ratio, 2).1.fg,
            Some(t.negative)
        );
        // A percent column is toned on the multiplied-out value. At -0.006 the
        // *fraction* rounds away and the *percent* does not — read as a ratio,
        // a -0.6% return would be painted as flat.
        assert_eq!(
            metric(Some(-0.006), format::signed_pct1, Unit::Percent, 1)
                .1
                .fg,
            Some(t.negative)
        );
        assert_eq!(
            metric(Some(-0.006), format::signed_pct1, Unit::Ratio, 1)
                .1
                .fg,
            Some(t.positive),
            "the wrong unit tones a real loss as flat — this is what Unit prevents"
        );
        assert_eq!(ratio(-0.006), "-0.01");
        // Found by the test above: `{:.2}` prints `-0.00` for a dust-sized
        // negative, drawing a minus sign on a zero in a column read for
        // direction.
        assert_eq!(ratio(-1e-13), "0.00");
        assert_eq!(ratio(f64::NAN), MISSING);
    }

    #[test]
    fn every_stage_the_owner_can_serve_has_a_tone_and_the_runnable_one_is_the_loud_one() {
        let t = theme();
        assert_eq!(stage_tone("operational"), t.positive);
        assert_eq!(stage_tone("research"), t.accent);
        assert_eq!(stage_tone("offline"), t.text_dim);
        assert_eq!(stage_tone("something-new"), t.text_dim);
        assert_ne!(stage_tone("operational"), stage_tone("research"));
    }

    #[test]
    fn a_list_longer_than_its_pane_spends_a_row_on_saying_so() {
        // Exactly-fits keeps every row; one over spends a row on the count, so
        // the pane can never claim a shorter ledger than the desk has.
        assert_eq!(fits(3, 5), 3);
        assert_eq!(fits(5, 5), 5);
        assert_eq!(fits(30, 5), 4);
        assert_eq!(fits(30, 0), 0);
    }
}
