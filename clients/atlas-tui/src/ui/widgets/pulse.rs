//! The pulse rail: the desk stress gauge, breadth, the movers, and the regime strip.
//!
//! Drawn by the shell rather than routed to as a view: the rail is on screen
//! under every view, so it claims no keystroke and holds nothing an operator
//! moved. One pure function of (store, rect).
//!
//! The gauge is the fear/greed adaptation kept honest to what this desk actually
//! measures — the HMM posterior's calm-minus-stress spread, docked by the two
//! panel readings that speak to the same question. Like the panel it reads, it
//! is a diagnostic of market state and never a signal.
//!
//! Sections are laid out top-down and each one is whole or absent. A clipped
//! breadth bar and a working one are indistinguishable at a glance, which is the
//! only way this rail is ever read.

use crate::format::{self, MISSING, PENDING};
use crate::model::{Reading, Regime, RegimePanel, Snapshot};
use crate::store::{AssetView, Store};
use crate::theme::theme;
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Gauge, Paragraph},
    Frame,
};
use std::cmp::Ordering;
use std::time::Instant;

use super::panel_header;

/// The regime strip's label column — the longest indicator name the owner
/// serves, once the two that do not fit the rail are abbreviated.
const LABEL_W: usize = 10;
/// The state column, wide enough for the owner's longest reading state.
const STATE_W: usize = 7;
/// The percentile bar: one cell per decile. A finer bar on a cell grid would
/// claim precision the percentile does not carry.
const PCT_CELLS: usize = 10;
/// Above this percentile the turbulence reading is in its own tail.
const TURBULENT: f64 = 0.9;
/// What the owner writes in a reading whose detector did not run
/// (`qlab/signals/panel.py`). Not a state of the market.
const FAILED: &str = "failed";
/// What each panel reading docks off the posterior's score.
const TURBULENCE_PENALTY: f64 = 15.0;
const DRAWDOWN_PENALTY: f64 = 10.0;

/// Glyph and label per indicator id.
///
/// One cell of glyph so a row is recognisable without being read, and a label an
/// operator can read: two of the owner's ids do not fit the column, and an id
/// cut mid-word is not a name.
const INDICATORS: [(&str, &str, &str); 6] = [
    ("turbulence", "◈", "turbulence"),
    ("absorption", "◉", "absorption"),
    ("drawdown", "▽", "drawdown"),
    ("tail_risk", "◣", "tail risk"),
    ("volatility_term_structure", "◐", "vol term"),
    ("hmm", "◇", "hmm"),
];

/// A detector this client has never heard of. It still gets a row: the panel's
/// whole point is showing what ran, and dropping the unknown would shorten it
/// silently.
const UNKNOWN_GLYPH: &str = "◌";

/// One frame of the rail.
///
/// `now` is taken and not read: Task 15 tweens the gauge value with an ease-out,
/// which needs the caller's instant. A renderer that reached for a clock instead
/// could not be pinned by a golden frame, so the parameter is here from the
/// start rather than arriving with the effect.
pub fn draw(f: &mut Frame, area: Rect, store: &Store, _now: Instant) {
    let sections = [
        stress_section(store),
        breadth_section(store, area.width),
        movers_section(store),
        regime_section(store),
    ];

    let mut y = area.y;
    let bottom = area.y.saturating_add(area.height);
    for (i, section) in sections.iter().enumerate() {
        let left = bottom.saturating_sub(y);
        // One row held back while sections remain, so the notice always has
        // somewhere to go: a section that fits exactly and leaves nothing for
        // the line announcing the rest is how a rail ends up silently short.
        //
        // The guard reads the rect this widget was handed, never the rail around
        // it — the shell splits before it hands over, and a guard on the parent
        // is off by whatever the split spent.
        let reserve = u16::from(i + 1 < sections.len());
        if left < section.height() + reserve {
            if left > 0 {
                f.render_widget(
                    Paragraph::new(Line::from(Span::styled(
                        format!(" ▾ {} more below", sections.len() - i),
                        Style::default().fg(theme().text_dim),
                    ))),
                    Rect {
                        y,
                        height: 1,
                        ..area
                    },
                );
            }
            return;
        }
        section.draw(
            f,
            Rect {
                y,
                height: section.height(),
                ..area
            },
        );
        y += section.height();
    }
}

/// One block of the rail: the lines above its gauge, the gauge, and the lines
/// below it.
///
/// The gauge is separate because a `Gauge` is a widget and wants a rect of its
/// own, which a `Paragraph` of lines cannot hand it.
struct Section {
    head: Vec<Line<'static>>,
    gauge: Option<Gauge<'static>>,
    tail: Vec<Line<'static>>,
}

impl Section {
    fn text(lines: Vec<Line<'static>>) -> Self {
        Self {
            head: lines,
            gauge: None,
            tail: Vec::new(),
        }
    }

    fn height(&self) -> u16 {
        (self.head.len() + usize::from(self.gauge.is_some()) + self.tail.len()) as u16
    }

    fn draw(&self, f: &mut Frame, area: Rect) {
        let mut y = lines_at(f, area, area.y, &self.head);
        if let Some(gauge) = &self.gauge {
            f.render_widget(
                gauge,
                Rect {
                    x: area.x + 1,
                    y,
                    width: area.width.saturating_sub(2),
                    height: 1,
                },
            );
            y += 1;
        }
        lines_at(f, area, y, &self.tail);
    }
}

/// Draw `lines` starting at `y`, and report the row after them.
fn lines_at(f: &mut Frame, area: Rect, y: u16, lines: &[Line<'static>]) -> u16 {
    if lines.is_empty() {
        return y;
    }
    f.render_widget(
        Paragraph::new(lines.to_vec()),
        Rect {
            y,
            height: lines.len() as u16,
            ..area
        },
    );
    y + lines.len() as u16
}

/// The gauge, and the desk facts that put a number beside it.
fn stress_section(store: &Store) -> Section {
    let t = theme();
    let snapshot = store.snapshot.as_ref();
    let regime = snapshot
        .and_then(|s| s.market.as_ref())
        .and_then(|m| m.regime.as_ref());
    let stress = snapshot.and_then(|s| s.stress.as_ref());

    let mut head = vec![panel_header("pulse")];
    let gauge = match desk_stress(regime, store.regime_panel.as_ref()) {
        Some(score) => {
            let (word, tone) = band(score);
            Some(
                Gauge::default()
                    // The raw value. Task 15 gives it an ease-out tween, which
                    // needs the previous value and an instant — state this
                    // renderer deliberately does not hold yet.
                    .ratio((score / 100.0).clamp(0.0, 1.0))
                    .gauge_style(Style::default().fg(tone).bg(t.bg_hover))
                    .label(Span::styled(
                        format!("{score:.0}  {word}"),
                        Style::default()
                            .fg(t.text_primary)
                            .add_modifier(Modifier::BOLD),
                    )),
            )
        }
        None => {
            // Not a 50: a gauge with nothing behind it would put the needle
            // exactly where NEUTRAL sits and mean nothing at all by it. Which
            // kind of nothing is the renderer's to say — a panel that has not
            // arrived is `…`, and one that arrived with nothing this gauge can
            // read is `--`. The strip below spells out which detectors failed.
            let absent = if store.regime_panel.is_none() {
                PENDING
            } else {
                MISSING
            };
            head.push(Line::from(vec![
                label(" desk stress  "),
                Span::styled(absent, Style::default().fg(t.text_dim)),
            ]));
            None
        }
    };

    // The five facts the rail stated before the gauge did, packed two to a line.
    // The rewrite may render the desk differently; it may not render less of it.
    let tail = vec![
        Line::from(vec![
            label(" regime "),
            state_span(regime.and_then(|r| format::text(r.regime.as_ref()))),
            label("  robust "),
            state_span(regime.and_then(|r| format::text(r.robust_state.as_ref()))),
        ]),
        Line::from(vec![
            label(" drawdown "),
            value(format::opt_pct(drawdown(snapshot))),
            label("  tier "),
            value(format::upper(
                stress.and_then(|s| format::text(s.drawdown_tier.as_ref())),
            )),
        ]),
        Line::from(vec![
            label(" gross "),
            value(format::opt_pct(stress.and_then(|s| s.gross_exposure))),
        ]),
        Line::from(""),
    ];
    Section { head, gauge, tail }
}

/// How much of the universe went which way.
fn breadth_section(store: &Store, width: u16) -> Section {
    let t = theme();
    let (advancing, declining) = breadth(store);
    // Absent is not zero, and the two look identical in a count: a desk with no
    // marks at all has an unknown breadth, not a flat one.
    let marked = store
        .asset_views()
        .iter()
        .filter(|view| view.change_1d.is_some())
        .count();

    let bar = if advancing + declining == 0 {
        Line::from(vec![Span::styled(
            format!(" {MISSING}"),
            Style::default().fg(t.text_tertiary),
        )])
    } else {
        // One cell of inset each side, matching every other line in the rail.
        let (up, down) = segments(advancing, declining, width.saturating_sub(2));
        Line::from(vec![
            Span::raw(" "),
            Span::styled("█".repeat(up as usize), Style::default().fg(t.positive)),
            Span::styled("█".repeat(down as usize), Style::default().fg(t.negative)),
        ])
    };

    let count = |n: usize| {
        if marked == 0 {
            MISSING.to_string()
        } else {
            n.to_string()
        }
    };
    Section::text(vec![
        panel_header("breadth"),
        bar,
        Line::from(vec![
            label(" adv "),
            Span::styled(count(advancing), Style::default().fg(t.positive)),
            label(" / dec "),
            Span::styled(count(declining), Style::default().fg(t.negative)),
        ]),
        Line::from(""),
    ])
}

/// The two ends of the tape.
fn movers_section(store: &Store) -> Section {
    let mut lines = vec![panel_header("movers")];
    match movers(store) {
        // A universe of one is its own best and worst; two identical rows would
        // read as two movers, which is a desk this client is not looking at.
        Some((best, worst)) if best.ticker == worst.ticker => lines.push(mover("only", best)),
        Some((best, worst)) => {
            lines.push(mover("best", best));
            lines.push(mover("worst", worst));
        }
        None => lines.push(Line::from(Span::styled(
            format!(" {MISSING}"),
            Style::default().fg(theme().text_tertiary),
        ))),
    }
    lines.push(Line::from(""));
    Section::text(lines)
}

fn mover(role: &str, view: AssetView<'_>) -> Line<'static> {
    let t = theme();
    // `movers` only ever hands back rows that carry a change.
    let change = view.change_1d.unwrap_or_default();
    let text = format::signed_pct(change);
    // The glyph reads the *rendered* number: `signed_pct` takes its sign from
    // the rounded value, and a ▼ over `+0.00%` is a row contradicting itself.
    let (arrow, tone) = if text.starts_with('-') {
        ("▼", t.negative)
    } else {
        ("▲", t.positive)
    };
    Line::from(vec![
        Span::styled(format!(" {arrow} "), Style::default().fg(tone)),
        Span::styled(format!("{role:<6}"), Style::default().fg(t.text_secondary)),
        Span::styled(
            format!("{:<5}", view.ticker),
            Style::default().fg(t.cyan).add_modifier(Modifier::BOLD),
        ),
        Span::styled(format!("{text:>8}"), Style::default().fg(tone)),
    ])
}

/// Every reading the panel carried, in the panel's own order.
fn regime_section(store: &Store) -> Section {
    let t = theme();
    let mut lines = vec![panel_header("regime")];
    match store.regime_panel.as_ref() {
        Some(panel) if !panel.readings.is_empty() => {
            lines.extend(panel.readings.iter().map(reading_row));
        }
        // An answered panel with nothing in it is a different fact from no
        // panel: the owner replied, and what it replied was nothing.
        Some(_) => lines.push(Line::from(Span::styled(
            " no readings in the last panel",
            Style::default().fg(t.text_tertiary),
        ))),
        None => lines.push(Line::from(vec![
            Span::styled(format!(" {PENDING} "), Style::default().fg(t.text_dim)),
            Span::styled(
                "waiting for the regime panel",
                Style::default().fg(t.text_tertiary),
            ),
        ])),
    }
    Section::text(lines)
}

fn reading_row(reading: &Reading) -> Line<'static> {
    let t = theme();
    let (glyph, name) = indicator(&reading.indicator_id);
    let state = format::text(reading.state.as_ref());
    let tone = state_tone(state);
    Line::from(vec![
        Span::styled(format!(" {glyph} "), Style::default().fg(tone)),
        Span::styled(
            format!("{name:<LABEL_W$} "),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(
            format!("{:<STATE_W$}", format::upper(state)),
            Style::default().fg(tone),
        ),
        Span::styled(
            percentile_bar(reading.percentile),
            Style::default().fg(tone),
        ),
    ])
}

/// The desk stress score, 0–100, or `None` when nothing it reads is there.
///
/// Two sources on purpose: the posterior is the HMM's own probability mass, and
/// the two penalised readings are the detectors that disagree with it most often
/// — a desk can be posterior-calm while sitting in the turbulence tail, and the
/// gauge has to say so.
fn desk_stress(regime: Option<&Regime>, panel: Option<&RegimePanel>) -> Option<f64> {
    // The panel is what makes this a reading rather than a guess.
    let panel = panel?;
    let posterior = regime.and_then(|r| r.posterior.as_ref());
    let turbulence = reading(panel, "turbulence").and_then(|r| r.percentile);
    // `failed` is the owner's word for a detector that did not run, so it is an
    // absent input rather than a calm one — the distinction the score turns on.
    let drawdown = reading(panel, "drawdown")
        .and_then(|r| format::text(r.state.as_ref()))
        .filter(|state| *state != FAILED);

    // A panel whose detectors all failed is not a calm desk. Without a posterior
    // *and* without either reading there is no input at all, and 50 NEUTRAL is
    // the most confident thing this gauge can say — which is why it must not be
    // what it says when it knows nothing.
    if posterior.is_none() && turbulence.is_none() && drawdown.is_none() {
        return None;
    }

    // An absent posterior beside a reading that did run is no opinion, which is
    // the middle of the scale — the owner ships without `hmmlearn` installed
    // more often than with it, and the panel still measured something.
    let mass = |state: &str| {
        posterior
            .and_then(|p| p.get(state))
            .copied()
            .filter(|v| v.is_finite())
            .unwrap_or(0.0)
    };

    let mut score = 50.0 + 50.0 * (mass("calm") - mass("stress"));
    if turbulence.is_some_and(|percentile| percentile > TURBULENT) {
        score -= TURBULENCE_PENALTY;
    }
    if drawdown.is_some_and(is_stressed) {
        score -= DRAWDOWN_PENALTY;
    }
    Some(score.clamp(0.0, 100.0))
}

/// The band a score sits in. Every edge is closed at the top, so a desk at
/// exactly 80 reads CALM rather than SERENE.
fn band(score: f64) -> (&'static str, Color) {
    let t = theme();
    match score {
        s if s <= 20.0 => ("STRESSED", t.negative),
        s if s <= 40.0 => ("TENSE", t.warning),
        s if s <= 60.0 => ("NEUTRAL", t.warning),
        s if s <= 80.0 => ("CALM", t.positive),
        _ => ("SERENE", t.positive),
    }
}

/// How many assets rose and how many fell. Flat is neither: a day that did not
/// move is not an advance, and counting it as one would tilt every quiet tape
/// green.
fn breadth(store: &Store) -> (usize, usize) {
    store
        .asset_views()
        .iter()
        .filter_map(|view| view.change_1d)
        .fold((0, 0), |(up, down), change| {
            match change.partial_cmp(&0.0) {
                Some(Ordering::Greater) => (up + 1, down),
                Some(Ordering::Less) => (up, down + 1),
                _ => (up, down),
            }
        })
}

/// The two segments of a `width`-wide breadth bar.
fn segments(advancing: usize, declining: usize, width: u16) -> (u16, u16) {
    let total = advancing + declining;
    if total == 0 || width == 0 {
        return (0, 0);
    }
    let width = width as usize;
    let mut up = ((advancing * width) as f64 / total as f64).round() as usize;
    // A side that has rows in it must occupy a cell: a segment of zero says
    // nothing advanced, which the caption beside it flatly contradicts.
    if advancing > 0 {
        up = up.max(1);
    }
    if declining > 0 {
        up = up.min(width - 1);
    }
    (up as u16, (width - up) as u16)
}

/// The best and worst rows of the universe by the change they carry.
fn movers(store: &Store) -> Option<(AssetView<'_>, AssetView<'_>)> {
    let marked: Vec<AssetView> = store
        .asset_views()
        .into_iter()
        .filter(|view| view.change_1d.is_some())
        .collect();
    let by_change = |a: &AssetView, b: &AssetView| {
        a.change_1d
            .partial_cmp(&b.change_1d)
            .unwrap_or(Ordering::Equal)
    };
    Some((
        marked.iter().copied().max_by(by_change)?,
        marked.iter().copied().min_by(by_change)?,
    ))
}

/// A percentile as ten filled-or-empty cells, or `MISSING` when the detector
/// reported none — ten empty cells would be a measurement of zero.
fn percentile_bar(percentile: Option<f64>) -> String {
    let Some(percentile) = percentile else {
        return MISSING.to_string();
    };
    let filled =
        ((percentile * PCT_CELLS as f64).round() as i64).clamp(0, PCT_CELLS as i64) as usize;
    format!("{}{}", "▰".repeat(filled), "▱".repeat(PCT_CELLS - filled))
}

/// The colour a reading's state is drawn in.
fn state_tone(state: Option<&str>) -> Color {
    let t = theme();
    match state.unwrap_or_default() {
        "calm" | "ok" => t.positive,
        "stress" | "stressed" => t.negative,
        // `uncertain`, the owner's `failed`, and the null a detector that did
        // not run leaves behind are one fact on screen: nobody knows.
        _ => t.warning,
    }
}

fn is_stressed(state: &str) -> bool {
    matches!(state, "stress" | "stressed")
}

fn indicator(id: &str) -> (&'static str, String) {
    match INDICATORS.iter().find(|(known, _, _)| *known == id) {
        Some((_, glyph, name)) => (glyph, (*name).to_string()),
        None => (UNKNOWN_GLYPH, head(id.replace('_', " "), LABEL_W)),
    }
}

fn reading<'a>(panel: &'a RegimePanel, id: &str) -> Option<&'a Reading> {
    panel
        .readings
        .iter()
        .find(|reading| reading.indicator_id == id)
}

/// The leading `width` characters of `text`, or all of it.
///
/// A second copy of `markets::head`, deliberately: that one is about a
/// right-aligned column dropping its *sign* when it overflows, and this one is a
/// label column with no sign to lose. They graduate to one helper when a third
/// grid wants it (Tasks 11/13), not before — a shared function whose doc comment
/// has to describe both is worth less than either.
fn head(text: String, width: usize) -> String {
    match text.char_indices().nth(width) {
        Some((byte, _)) => text[..byte].to_string(),
        None => text,
    }
}

/// The live book decides the drawdown for the same reason it decides the halt:
/// it is the one marked to the tape.
fn drawdown(snapshot: Option<&Snapshot>) -> Option<f64> {
    let snapshot = snapshot?;
    snapshot
        .live_portfolio
        .as_ref()
        .and_then(|p| p.drawdown)
        .or_else(|| snapshot.portfolio.as_ref().and_then(|p| p.drawdown))
}

fn label(text: &'static str) -> Span<'static> {
    Span::styled(text, Style::default().fg(theme().text_secondary))
}

fn value(text: String) -> Span<'static> {
    Span::styled(text, Style::default().fg(theme().text_primary))
}

/// A state word in its own colour, or `MISSING` when the owner did not send one
/// — an absent state is not an uncertain one.
fn state_span(state: Option<&str>) -> Span<'static> {
    match state {
        Some(state) => Span::styled(
            state.to_uppercase(),
            Style::default().fg(state_tone(Some(state))),
        ),
        None => Span::styled(
            MISSING.to_string(),
            Style::default().fg(theme().text_secondary),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bus::AppEvent;
    use crate::model::Snapshot;
    use crate::theme::theme;
    use serde_json::json;

    /// A regime as the owner nests it under `market`, decoded rather than built:
    /// a score that read a field the owner never fills is exactly the bug the
    /// fixture path would hide.
    fn regime(value: serde_json::Value) -> Regime {
        serde_json::from_value(value).unwrap()
    }

    fn panel(value: serde_json::Value) -> RegimePanel {
        serde_json::from_value(value).unwrap()
    }

    /// A panel whose two penalised readings say what the caller asks them to.
    fn penalties(turbulence_percentile: Option<f64>, drawdown_state: &str) -> RegimePanel {
        panel(json!({"readings": [
            {"indicator_id": "turbulence", "percentile": turbulence_percentile,
             "state": "calm"},
            {"indicator_id": "drawdown", "state": drawdown_state}
        ]}))
    }

    fn store_with(assets: serde_json::Value) -> Store {
        let mut store = Store::default();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value::<Snapshot>(json!({"market": {"assets": assets}})).unwrap(),
            )),
            Instant::now(),
        );
        store
    }

    #[test]
    fn the_score_is_the_posterior_spread_then_the_panels_penalties() {
        // The five cases the gauge is specified by. Spelled as a table because
        // the formula is the whole widget: a band word is only worth anything if
        // the number under it is the one the plan states.
        let calm = regime(json!({"posterior": {"calm": 1.0, "stress": 0.0}}));
        let stressed = regime(json!({"posterior": {"calm": 0.0, "stress": 1.0}}));
        let mixed = regime(json!({"posterior": {"calm": 0.71, "normal": 0.22, "stress": 0.07}}));
        let quiet = penalties(Some(0.5), "calm");

        // 50 + 50·(1 − 0) with nothing to dock.
        assert_eq!(desk_stress(Some(&calm), Some(&quiet)), Some(100.0));
        assert_eq!(desk_stress(Some(&stressed), Some(&quiet)), Some(0.0));
        // 50 + 50·(0.71 − 0.07) = 82.
        assert_eq!(desk_stress(Some(&mixed), Some(&quiet)), Some(82.0));
        // The tail is strict: 0.9 itself is not past the threshold.
        assert_eq!(
            desk_stress(Some(&mixed), Some(&penalties(Some(0.9), "calm"))),
            Some(82.0)
        );
        // 82 − 15 (turbulence past its tail) − 10 (drawdown stressed).
        assert_eq!(
            desk_stress(Some(&mixed), Some(&penalties(Some(0.95), "stress"))),
            Some(57.0)
        );
        // The clamp bites rather than the gauge asking ratatui to render −25.
        assert_eq!(
            desk_stress(Some(&stressed), Some(&penalties(Some(0.99), "stress"))),
            Some(0.0)
        );
    }

    #[test]
    fn a_panel_that_has_not_arrived_is_pending_rather_than_a_number() {
        // The whole gauge is a reading of the panel. Scoring without one would
        // put a confident 50 NEUTRAL on screen for a measurement nobody made.
        let calm = regime(json!({"posterior": {"calm": 1.0, "stress": 0.0}}));
        assert_eq!(desk_stress(Some(&calm), None), None);
        assert_eq!(desk_stress(None, None), None);
    }

    #[test]
    fn an_absent_posterior_leaves_the_base_alone_and_still_takes_the_penalties() {
        // `hmmlearn` is not installed on every owner, so the posterior is the
        // half of the score that is routinely missing. Absent is no opinion —
        // 50 — and the panel readings that did run still count.
        let no_hmm = regime(json!({"regime": "calm"}));
        assert_eq!(
            desk_stress(Some(&no_hmm), Some(&penalties(Some(0.5), "calm"))),
            Some(50.0)
        );
        assert_eq!(
            desk_stress(Some(&no_hmm), Some(&penalties(Some(0.95), "stress"))),
            Some(25.0)
        );
    }

    #[test]
    fn a_panel_whose_detectors_all_failed_scores_nothing_at_all() {
        // The reachable half of the same failure: the owner keeps a row per
        // detector and marks it `failed`, so the readings are there and every
        // input is null. With no posterior either, 50 NEUTRAL would be the most
        // confident sentence this gauge can say about a desk it cannot see.
        let no_hmm = regime(json!({"regime": "calm"}));
        let all_failed = panel(json!({"readings": [
            {"indicator_id": "turbulence", "state": "failed"},
            {"indicator_id": "drawdown", "state": "failed"}
        ]}));
        assert_eq!(desk_stress(Some(&no_hmm), Some(&all_failed)), None);
        assert_eq!(
            desk_stress(Some(&no_hmm), Some(&panel(json!({"readings": []})))),
            None
        );

        // One usable input is enough to score: the drawdown detector ran and
        // said calm, which is a measurement even though it docks nothing.
        assert_eq!(
            desk_stress(
                Some(&no_hmm),
                Some(&panel(json!({"readings": [
                    {"indicator_id": "turbulence", "state": "failed"},
                    {"indicator_id": "drawdown", "state": "calm"}
                ]})))
            ),
            Some(50.0)
        );
    }

    #[test]
    fn the_bands_are_closed_at_their_upper_edge() {
        // Every edge is `≤`, so a score sitting exactly on one belongs to the
        // band below it. Off by one here and a desk at 80 reads SERENE.
        let t = theme();
        assert_eq!(band(0.0), ("STRESSED", t.negative));
        assert_eq!(band(20.0), ("STRESSED", t.negative));
        assert_eq!(band(20.1), ("TENSE", t.warning));
        assert_eq!(band(40.0), ("TENSE", t.warning));
        assert_eq!(band(40.1), ("NEUTRAL", t.warning));
        assert_eq!(band(60.0), ("NEUTRAL", t.warning));
        assert_eq!(band(60.1), ("CALM", t.positive));
        assert_eq!(band(80.0), ("CALM", t.positive));
        assert_eq!(band(80.1), ("SERENE", t.positive));
        assert_eq!(band(100.0), ("SERENE", t.positive));
    }

    #[test]
    fn breadth_counts_the_assets_that_moved_and_no_others() {
        // Flat is not an advance and absent is not a decline: both would put a
        // count on screen that nothing measured.
        let store = store_with(json!([
            {"ticker": "A", "change_1d": 0.01},
            {"ticker": "B", "change_1d": 0.02},
            {"ticker": "C", "change_1d": 0.001},
            {"ticker": "D", "change_1d": -0.01},
            {"ticker": "E", "change_1d": -0.02},
            {"ticker": "F", "change_1d": 0.0},
            {"ticker": "G"}
        ]));
        assert_eq!(breadth(&store), (3, 2));
        assert_eq!(breadth(&Store::default()), (0, 0));
    }

    #[test]
    fn the_bar_splits_by_proportion_and_never_hides_a_side_that_moved() {
        assert_eq!(segments(3, 2, 10), (6, 4));
        assert_eq!(segments(1, 1, 10), (5, 5));
        // A side with rows in it must occupy a cell: a segment of zero says
        // nothing advanced, which is a claim the counts contradict.
        assert_eq!(segments(1, 99, 10), (1, 9));
        assert_eq!(segments(99, 1, 10), (9, 1));
        // Nothing measured draws nothing, rather than a full bar of either side.
        assert_eq!(segments(0, 0, 10), (0, 0));
        assert_eq!(segments(4, 0, 10), (10, 0));
    }

    #[test]
    fn the_movers_are_the_extremes_of_the_universe_by_change() {
        let store = store_with(json!([
            {"ticker": "A", "change_1d": 0.01},
            {"ticker": "B", "change_1d": 0.03},
            {"ticker": "C", "change_1d": -0.02},
            {"ticker": "D"}
        ]));
        let (best, worst) = movers(&store).expect("a universe that moved has movers");
        assert_eq!(best.ticker, "B");
        assert_eq!(worst.ticker, "C");

        // An all-red tape still has a best: the least bad row, and the arrow it
        // carries has to be the one its own number earns.
        let red = store_with(json!([
            {"ticker": "A", "change_1d": -0.01},
            {"ticker": "B", "change_1d": -0.03}
        ]));
        let (best, worst) = movers(&red).unwrap();
        assert_eq!((best.ticker, worst.ticker), ("A", "B"));
        assert_eq!(
            mover("best", best).spans[0].style.fg,
            Some(theme().negative)
        );

        // Nothing that carries a change is no movers at all, not a row of zeros.
        assert_eq!(movers(&Store::default()).map(|(b, _)| b.ticker), None);
        assert_eq!(
            movers(&store_with(json!([{"ticker": "A"}]))).map(|(b, _)| b.ticker),
            None
        );
    }

    #[test]
    fn a_single_asset_is_its_own_best_and_worst() {
        // Two identical rows would read as two movers, which is a universe this
        // desk does not hold.
        let store = store_with(json!([{"ticker": "A", "change_1d": 0.01}]));
        let (best, worst) = movers(&store).unwrap();
        assert_eq!(best.ticker, worst.ticker);
    }

    #[test]
    fn the_percentile_bar_is_ten_cells_and_absent_is_missing() {
        assert_eq!(percentile_bar(Some(0.0)), "▱▱▱▱▱▱▱▱▱▱");
        assert_eq!(percentile_bar(Some(0.8008)), "▰▰▰▰▰▰▰▰▱▱");
        assert_eq!(percentile_bar(Some(1.0)), "▰▰▰▰▰▰▰▰▰▰");
        // A detector that did not run has no percentile, and a bar of ten empty
        // cells is a measurement of zero.
        assert_eq!(percentile_bar(None), MISSING);
        // Out of range is still ten cells rather than a panic or a long row.
        assert_eq!(percentile_bar(Some(4.0)).chars().count(), 10);
        assert_eq!(percentile_bar(Some(-1.0)).chars().count(), 10);
    }

    #[test]
    fn the_state_tone_says_calm_from_stress_and_unknown_from_both() {
        let t = theme();
        assert_eq!(state_tone(Some("calm")), t.positive);
        assert_eq!(state_tone(Some("stress")), t.negative);
        assert_eq!(state_tone(Some("uncertain")), t.warning);
        // `failed` is the owner's word for a detector that did not run, and the
        // fixture's is a null. Neither is calm, and neither is stress.
        assert_eq!(state_tone(Some("failed")), t.warning);
        assert_eq!(state_tone(None), t.warning);
        assert_eq!(state_tone(Some("")), t.warning);
    }

    #[test]
    fn every_indicator_the_owner_serves_has_a_row_that_fits_the_rail() {
        for id in [
            "turbulence",
            "absorption",
            "drawdown",
            "tail_risk",
            "volatility_term_structure",
            "hmm",
        ] {
            let (glyph, label) = indicator(id);
            assert_eq!(glyph.chars().count(), 1, "{id}");
            assert!(label.chars().count() <= LABEL_W, "{id} → {label}");
        }
        // A detector this client has never heard of still gets a row: the panel
        // shows what ran, and dropping an unknown id would quietly shorten it.
        let (glyph, label) = indicator("new_detector_nobody_told_us_about");
        assert_eq!(glyph.chars().count(), 1);
        assert!(label.chars().count() <= LABEL_W, "{label}");
    }
}
