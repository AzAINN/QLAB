//! Desk state, derived from one owner snapshot.
//!
//! Everything rendered comes from `/api/tui` — the same endpoint the Textual
//! client reads. Two clients that computed their own view of the desk would
//! eventually disagree about it, and the one an operator happened to have open
//! would decide what they believed.

use crate::client::{dig, dig_bool, dig_f64, dig_str, OwnerClient, Readiness};
use crate::glyph::Mood;
use serde_json::Value;

#[derive(Debug, Clone, Default)]
pub struct Desk {
    pub mode: String,
    pub state: String,
    pub autonomous: bool,
    pub fast: bool,
    pub driving: bool,
    pub driving_workflow: String,
    pub drive_reason: String,
    pub open_tasks: u64,
    pub equity: Option<f64>,
    pub drawdown: Option<f64>,
    pub halted: bool,
    pub desk_label: String,
    pub regime: String,
    pub news_source: String,
}

impl Desk {
    pub fn from_snapshot(snap: &Value) -> Desk {
        let beat = "atlas_heartbeat";
        Desk {
            mode: dig_str(snap, "atlas.mode").unwrap_or_else(|| "—".into()),
            state: dig_str(snap, "atlas.state").unwrap_or_else(|| "—".into()),
            autonomous: dig_bool(snap, &format!("{beat}.autonomous")).unwrap_or(false),
            fast: dig_bool(snap, &format!("{beat}.fast")).unwrap_or(false),
            driving: dig_bool(snap, &format!("{beat}.coordinator.driving"))
                .unwrap_or(false),
            driving_workflow: dig_str(snap, &format!("{beat}.coordinator.workflow_id"))
                .unwrap_or_default(),
            drive_reason: dig_str(snap, &format!("{beat}.coordinator.reason"))
                .unwrap_or_default(),
            open_tasks: dig_f64(snap, "atlas.open_tasks").unwrap_or(0.0) as u64,
            equity: dig_f64(snap, "portfolio.equity"),
            drawdown: dig_f64(snap, "portfolio.drawdown"),
            // A missing halt flag is not "not halted" in any meaningful sense,
            // but rendering it as halted on every sparse payload would cry wolf.
            // False is the honest default; the equity panel shows the number.
            halted: dig_bool(snap, "portfolio.halted").unwrap_or(false),
            desk_label: dig_str(snap, "desk_mode.label").unwrap_or_else(|| "—".into()),
            regime: dig_str(snap, "regime.regime").unwrap_or_else(|| "unknown".into()),
            news_source: dig_str(snap, "atlas_read.news_source").unwrap_or_default(),
        }
    }

    pub fn mood(&self) -> Mood {
        Mood::from_desk(self.halted, self.driving, &self.mode)
    }

    /// Why the desk is not doing anything, stated rather than implied.
    ///
    /// Ported deliberately from the Textual client: a desk that is idle on
    /// purpose and one that is broken look identical from outside, and a mode
    /// name answers neither.
    pub fn why(&self) -> Vec<String> {
        let mut out = Vec::new();
        match self.mode.as_str() {
            "observe" => out.push(
                "Mode is OBSERVE: Atlas may start no workflow at all.".into(),
            ),
            "paused" => out.push(
                "Mode is PAUSED: monitoring continues, no new work is created.".into(),
            ),
            _ if !self.autonomous => out.push(format!(
                "Mode is {} but autonomy is OFF — Atlas queues work and waits.",
                self.mode.to_uppercase()
            )),
            _ => out.push(format!(
                "Mode is {} and autonomy is ON: Atlas starts what its mode permits.",
                self.mode.to_uppercase()
            )),
        }
        if self.driving {
            out.push(format!(
                "A coordinator is driving workflow {} right now.",
                self.driving_workflow
            ));
        } else if !self.drive_reason.is_empty() {
            out.push(format!("Cannot drive a run: {}", self.drive_reason));
        }
        if self.open_tasks > 0 {
            out.push(format!("{} task(s) queued.", self.open_tasks));
        } else {
            out.push(
                "No trigger has fired — no drawdown tier, drift breach, regime \
                 flip, or data outage. Nothing to act on is not idle by accident."
                    .into(),
            );
        }
        out
    }
}

pub struct App {
    pub client: OwnerClient,
    pub readiness: Readiness,
    pub desk: Desk,
    pub snapshot: Option<Value>,
    pub tick: u64,
    pub last_error: String,
    pub offline: bool,
    pub should_quit: bool,
}

impl App {
    pub fn new(client: OwnerClient, offline: bool) -> Self {
        let readiness = client.readiness();
        Self {
            client,
            readiness,
            desk: Desk::default(),
            snapshot: None,
            tick: 0,
            last_error: String::new(),
            offline,
            should_quit: false,
        }
    }

    /// Pull a fresh snapshot. A failure downgrades readiness rather than
    /// leaving stale numbers on screen labelled as current — the one thing a
    /// trading surface must never do.
    pub fn refresh(&mut self) {
        match self.client.snapshot(self.offline) {
            Ok(snap) => {
                self.desk = Desk::from_snapshot(&snap);
                self.snapshot = Some(snap);
                self.readiness = Readiness::Ready;
                self.last_error.clear();
            }
            Err(err) => {
                self.last_error = err.to_string();
                self.readiness = Readiness::Unreachable(err.to_string());
            }
        }
    }

    pub fn workflows(&self) -> Vec<(String, String, String)> {
        let Some(snap) = &self.snapshot else { return Vec::new() };
        let Some(rows) = dig(snap, "workflows").and_then(|v| v.as_array()) else {
            return Vec::new();
        };
        rows.iter()
            .take(6)
            .map(|w| {
                (
                    w.get("workflow_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("—")
                        .chars()
                        .take(10)
                        .collect(),
                    w.get("status").and_then(|v| v.as_str()).unwrap_or("—").into(),
                    w.get("goal").and_then(|v| v.as_str()).unwrap_or("").into(),
                )
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_sparse_snapshot_does_not_invent_state() {
        // The owner omits whole objects early in startup. Every field must have
        // an honest empty rather than a plausible-looking zero.
        let desk = Desk::from_snapshot(&json!({}));
        assert_eq!(desk.mode, "—");
        assert!(desk.equity.is_none());
        assert!(desk.drawdown.is_none());
        assert!(!desk.driving);
    }

    #[test]
    fn desk_reads_the_coordinator_the_owner_reports() {
        let snap = json!({
            "atlas": {"mode": "research", "state": "coordinating", "open_tasks": 2},
            "atlas_heartbeat": {
                "autonomous": true, "fast": true,
                "coordinator": {"driving": true, "workflow_id": "wf-9", "reason": ""}
            },
            "portfolio": {"equity": 10450.0, "drawdown": 0.02, "halted": false}
        });
        let desk = Desk::from_snapshot(&snap);
        assert_eq!(desk.mode, "research");
        assert!(desk.driving && desk.autonomous && desk.fast);
        assert_eq!(desk.driving_workflow, "wf-9");
        assert_eq!(desk.open_tasks, 2);
        assert_eq!(desk.mood(), Mood::Working);
    }

    #[test]
    fn a_halted_desk_overrides_a_driving_coordinator() {
        let snap = json!({
            "atlas": {"mode": "propose"},
            "atlas_heartbeat": {"coordinator": {"driving": true}},
            "portfolio": {"halted": true}
        });
        assert_eq!(Desk::from_snapshot(&snap).mood(), Mood::Alarmed);
    }

    #[test]
    fn why_explains_an_undrivable_dispatch() {
        let snap = json!({
            "atlas": {"mode": "research", "open_tasks": 1},
            "atlas_heartbeat": {
                "autonomous": true,
                "coordinator": {"driving": false, "reason": "the `claude` CLI is not on PATH"}
            }
        });
        let why = Desk::from_snapshot(&snap).why();
        assert!(why.iter().any(|l| l.contains("not on PATH")));
        assert!(why.iter().any(|l| l.contains("1 task(s) queued")));
    }

    #[test]
    fn why_never_returns_nothing() {
        // An empty explainer is the black-box failure this panel exists to fix.
        assert!(!Desk::from_snapshot(&json!({})).why().is_empty());
    }
}
