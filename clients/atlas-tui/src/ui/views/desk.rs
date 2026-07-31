//! DESK — why the desk is doing what it is doing, and the tiles that say what it holds.
//!
//! A placeholder in the shape Task 14 fills: the left column is the explainer
//! ported from the Textual client, the right is the 2×3 tile grid that becomes
//! the big-text equity hero and its neighbours. The tiles read `--` because
//! nothing wires them yet — `--` is the honest rendering of a number this view
//! does not read, and a plausible-looking one would be worse than empty.

use crate::cmd::Command;
use crate::format::{text, MISSING};
use crate::fx::FlashTracker;
use crate::model::Snapshot;
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::{panel_block, panel_header};
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
    Frame,
};
use std::time::Instant;

/// The tiles Task 14 fills, in the order it lays them out.
const TILES: [&str; 6] = [
    "equity",
    "regime",
    "allocation",
    "alerts",
    "verdict",
    "replay",
];

pub struct DeskView;

impl View for DeskView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        let cols = Layout::horizontal([Constraint::Percentage(40), Constraint::Percentage(60)])
            .split(area);
        draw_why(f, cols[0], store);
        draw_tiles(f, cols[1]);
    }

    fn on_key(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

fn draw_why(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let mut body = vec![panel_header("why the desk is quiet"), Line::from("")];
    for bullet in why(store.snapshot.as_ref()) {
        body.push(Line::from(vec![
            Span::styled("• ", Style::default().fg(t.accent)),
            Span::styled(bullet, Style::default().fg(t.text_primary)),
        ]));
        body.push(Line::from(""));
    }
    f.render_widget(Paragraph::new(body).wrap(Wrap { trim: true }), area);
}

fn draw_tiles(f: &mut Frame, area: Rect) {
    let t = theme();
    // Header, value, rule. Task 14 grows the first row into the big-text equity
    // hero; until something fills them, tall empty boxes would read as panes
    // that failed to render rather than panes that are not built.
    let rows = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(3),
        Constraint::Length(3),
        Constraint::Min(0),
    ])
    .split(area);
    for (row, pair) in rows.iter().zip(TILES.chunks(2)) {
        let cells = Layout::horizontal([Constraint::Ratio(1, 2); 2]).split(*row);
        for (cell, title) in cells.iter().zip(pair) {
            let block = panel_block();
            let inner = block.inner(*cell);
            f.render_widget(block, *cell);
            f.render_widget(
                Paragraph::new(vec![
                    panel_header(title),
                    Line::from(Span::styled(
                        MISSING,
                        Style::default()
                            .fg(t.text_secondary)
                            .add_modifier(Modifier::BOLD),
                    )),
                ]),
                inner,
            );
        }
    }
}

/// Why the desk is not doing anything, stated rather than implied.
///
/// Ported from the Textual client through `app.rs::Desk::why`: a desk that is
/// idle on purpose and one that is broken look identical from outside, and a
/// mode name answers neither. Never returns empty — an empty explainer is the
/// black-box failure this panel exists to fix.
///
/// The old version had a fourth branch keyed on `atlas.open_tasks`. The owner
/// has never served that field (`Atlas.status()` does not build it; the queue
/// is a separate `atlas_tasks` list the model does not carry), so the branch
/// was dead and the count was always zero. `current_task_id` is the fact the
/// owner does report about work in flight.
pub fn why(snapshot: Option<&Snapshot>) -> Vec<String> {
    let atlas = snapshot.and_then(|s| s.atlas.as_ref());
    let beat = snapshot.and_then(|s| s.atlas_heartbeat.as_ref());
    let coordinator = beat.and_then(|b| b.coordinator.as_ref());

    let mode = atlas.and_then(|a| text(a.mode.as_ref()));
    let autonomous = beat.and_then(|b| b.autonomous).unwrap_or(false);
    let driving = coordinator.and_then(|c| c.driving).unwrap_or(false);
    let workflow = coordinator.and_then(|c| text(c.workflow_id.as_ref()));
    let reason = coordinator.and_then(|c| text(c.reason.as_ref()));

    let mut out = Vec::new();
    match mode {
        Some("observe") => out.push("Mode is OBSERVE: Atlas may start no workflow at all.".into()),
        Some("paused") => {
            out.push("Mode is PAUSED: monitoring continues, no new work is created.".into())
        }
        Some(mode) if !autonomous => out.push(format!(
            "Mode is {} but autonomy is OFF — Atlas queues work and waits.",
            mode.to_uppercase()
        )),
        Some(mode) => out.push(format!(
            "Mode is {} and autonomy is ON: Atlas starts what its mode permits.",
            mode.to_uppercase()
        )),
        // Absent is its own fact. Rendering "Mode is --" would read as a mode.
        None => out.push("The owner has not reported a mode for Atlas.".into()),
    }
    if driving {
        out.push(format!(
            "A coordinator is driving workflow {} right now.",
            workflow.unwrap_or(MISSING)
        ));
    } else if let Some(reason) = reason {
        out.push(format!("Cannot drive a run: {reason}"));
    }
    match atlas.and_then(|a| text(a.current_task_id.as_ref())) {
        Some(task) => out.push(format!("Task {task} is in flight.")),
        None => out.push(
            "No trigger has fired — no drawdown tier, drift breach, regime flip, \
             or data outage. Nothing to act on is not idle by accident."
                .into(),
        ),
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(json: &str) -> Snapshot {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn why_never_returns_nothing() {
        assert!(!why(None).is_empty());
        assert!(!why(Some(&snapshot("{}"))).is_empty());
    }

    #[test]
    fn why_explains_an_undrivable_dispatch() {
        let snap = snapshot(
            r#"{"atlas": {"mode": "research", "current_task_id": "t-9"},
                "atlas_heartbeat": {"autonomous": true,
                  "coordinator": {"driving": false,
                                  "reason": "the `claude` CLI is not on PATH"}}}"#,
        );
        let why = why(Some(&snap));
        assert!(why.iter().any(|l| l.contains("not on PATH")));
        assert!(why.iter().any(|l| l.contains("t-9 is in flight")));
    }

    #[test]
    fn why_names_the_workflow_a_coordinator_is_actually_walking() {
        // Registering a workflow is not running it, so `driving` is the fact
        // worth stating — a workflow row on screen does not answer it.
        let snap = snapshot(
            r#"{"atlas": {"mode": "propose"},
                "atlas_heartbeat": {"autonomous": true,
                  "coordinator": {"driving": true, "workflow_id": "wf-42"}}}"#,
        );
        assert!(why(Some(&snap))
            .iter()
            .any(|l| l.contains("driving workflow wf-42")));
    }

    #[test]
    fn why_separates_a_mode_that_forbids_work_from_autonomy_that_is_off() {
        let observe = snapshot(r#"{"atlas": {"mode": "observe"}}"#);
        assert!(why(Some(&observe))[0].contains("OBSERVE"));
        let paused = snapshot(r#"{"atlas": {"mode": "paused"}}"#);
        assert!(why(Some(&paused))[0].contains("PAUSED"));
        let waiting = snapshot(
            r#"{"atlas": {"mode": "research"}, "atlas_heartbeat": {"autonomous": false}}"#,
        );
        assert!(why(Some(&waiting))[0].contains("autonomy is OFF"));
    }

    #[test]
    fn an_unset_string_from_the_owner_is_absent_not_a_value() {
        // The owner serialises what it never set as `""`. A run that read it as
        // a value would announce "driving workflow " with nothing after it.
        let snap = snapshot(
            r#"{"atlas": {"mode": "", "current_task_id": ""},
                "atlas_heartbeat": {"coordinator": {"driving": false, "reason": ""}}}"#,
        );
        let why = why(Some(&snap));
        assert!(why[0].contains("has not reported a mode"));
        assert!(why.iter().any(|l| l.contains("No trigger has fired")));
        assert!(!why.iter().any(|l| l.contains("Cannot drive")));
    }
}
