//! Application state, and the diffing that decides which fields changed enough to trigger.
//!
//! Two jobs, deliberately in one place. The store holds what the owner said,
//! and it decides what *changed* — because a change is the only thing motion is
//! allowed to be about. An effect fired from a render pass would animate on
//! every repaint; an effect fired from a diff animates once, when the desk
//! actually moved.
//!
//! Time enters as data: the only clock read is `elapsed()` on a stamp handed in
//! from outside, so every decision here is a function of its arguments and is
//! testable by arithmetic rather than by a sleep.

use crate::bus::{AppEvent, Channel, HttpResult};
use crate::model::{RegimePanel, Snapshot};
use std::time::{Duration, Instant};

/// The idle heartbeat. Three indicators on screen claim the client is alive —
/// the glyph, the throbbers, the quote ages — and a frame this often is what
/// makes that claim true when no event has arrived.
const IDLE_FRAME: Duration = Duration::from_millis(100);

/// Whether the loop owes the terminal a frame.
///
/// Pure, and it lives beside the store rather than in `main` so it can be
/// tested without a terminal. `fx_active` renders unconditionally: effects are
/// sampled per frame, and the 16 ms cadence they want is the loop's wake
/// interval, not a gate here.
pub fn should_render(dirty: bool, fx_active: bool, last_frame: Instant) -> bool {
    dirty || fx_active || last_frame.elapsed() >= IDLE_FRAME
}

/// A desk change worth animating.
///
/// Named for what happened on the desk, never for the effect that renders it —
/// the motion vocabulary is Task 15's to change without touching this diff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Trigger {
    RegimeChanged,
    DrawdownTierWorse,
    Halted,
    Resumed,
    ApprovalCreated,
    PhaseAdvanced,
    PlanExecuted,
    QuoteTick(String),
    ReadChanged,
}

/// The seven views. Key routing (`1`..`7`, Tab) is Task 5's.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ViewId {
    #[default]
    Desk,
    Markets,
    Book,
    Research,
    Workforce,
    Audit,
    Settings,
}

/// Which surface owns the next keystroke. A view-local pane index is not
/// modeled until a view has more than one focusable pane.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Focus {
    #[default]
    Content,
    Command,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Nav {
    pub view: ViewId,
    pub focus: Focus,
}

/// What this client can currently see. Both start down: a surface that assumes
/// its feeds are up renders stale numbers as current for one poll interval.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Conn {
    pub owner: bool,
    pub stream: bool,
}

#[derive(Debug, Default)]
pub struct Store {
    pub snapshot: Option<Snapshot>,
    pub regime_panel: Option<RegimePanel>,
    pub nav: Nav,
    pub conn: Conn,
    /// Private on purpose: `take_dirty` is the only reader, so the flag cannot
    /// be observed by something that then forgets to clear it.
    dirty: bool,
}

impl Store {
    /// Fold one event in and report what changed on the desk.
    pub fn apply(&mut self, ev: AppEvent) -> Vec<Trigger> {
        match ev {
            AppEvent::Snapshot(snap) => return self.apply_snapshot(*snap),
            AppEvent::RegimePanel(panel) => {
                self.regime_panel = Some(panel);
                self.dirty = true;
            }
            // A keystroke may move a selection and a resize moves everything;
            // both owe a frame even though neither is desk news.
            AppEvent::Key(_) | AppEvent::Resize => self.dirty = true,
            // The tick drives the glyph, which the store does not hold. Marking
            // it dirty would make the pacing rule decorative.
            AppEvent::Tick => {}
            AppEvent::ConnUp(channel) => self.set_conn(channel, true),
            AppEvent::ConnDown(channel) => self.set_conn(channel, false),
            // Task 8 stores the audit stream. Until something holds an event,
            // dirtying on one would repaint an unchanged frame.
            AppEvent::Sse(_) => {}
            AppEvent::Http(HttpResult::Malformed { url, error }) => {
                // Fail loud. Task 16 raises this to a toast; until then the log
                // is the channel, and silence is not an option.
                tracing::error!(%url, %error, "owner payload did not decode");
            }
        }
        Vec::new()
    }

    /// Read the repaint flag and clear it in one move.
    pub fn take_dirty(&mut self) -> bool {
        std::mem::take(&mut self.dirty)
    }

    /// Diff the incoming snapshot against the one it replaces.
    ///
    /// A first snapshot is diffed against nothing, and nothing normalises to
    /// absent — so the first payload announces the state it arrives in. That is
    /// the point: an operator who opens this client onto a halted desk has to
    /// see the halt, not a quiet red number.
    fn apply_snapshot(&mut self, next: Snapshot) -> Vec<Trigger> {
        let mut out = Vec::new();
        let prev = self.snapshot.as_ref();

        // Absent halt means not halted — the same honest default the desk
        // renders. A flag that *stopped* being reported is missing information,
        // not a resume, so it clears nothing.
        let was_halted = prev.and_then(halted).unwrap_or(false);
        match halted(&next) {
            Some(true) if !was_halted => out.push(Trigger::Halted),
            Some(false) if was_halted => out.push(Trigger::Resumed),
            _ => {}
        }
        if changed(prev.and_then(robust_state), robust_state(&next)) {
            out.push(Trigger::RegimeChanged);
        }
        if changed(prev.and_then(read_as_of), read_as_of(&next)) {
            out.push(Trigger::ReadChanged);
        }
        // Tasks 8/15 emit the remaining variants — `QuoteTick` from the SSE
        // overlay, the tier/approval/phase/plan diffs with the views that
        // render them. A trigger with no view to move is motion for its own
        // sake, and would ship untestable.

        self.snapshot = Some(next);
        self.dirty = true;
        out
    }

    fn set_conn(&mut self, channel: Channel, up: bool) {
        let slot = match channel {
            Channel::Owner => &mut self.conn.owner,
            Channel::Stream => &mut self.conn.stream,
        };
        if *slot != up {
            *slot = up;
            self.dirty = true;
        }
    }
}

/// A trigger fires when the new value is present and differs. An absent new
/// value is the owner declining to say, which is not a transition.
fn changed(prev: Option<&str>, next: Option<&str>) -> bool {
    next.is_some() && prev != next
}

/// The owner serialises a string it never set as `""`. Absent and empty are one
/// fact here; treating them as two would flash on every sparse payload.
fn text(value: Option<&String>) -> Option<&str> {
    value.map(String::as_str).filter(|s| !s.is_empty())
}

/// The live book decides the halt: it is the one marked to the tape, and the
/// reconciled book can lag it by a full valuation.
fn halted(snap: &Snapshot) -> Option<bool> {
    snap.live_portfolio
        .as_ref()
        .and_then(|p| p.halted)
        .or_else(|| snap.portfolio.as_ref().and_then(|p| p.halted))
}

/// The guarded state the desk acts on, not the raw detector label — the two
/// disagree on purpose, and only the guarded one is worth animating.
fn robust_state(snap: &Snapshot) -> Option<&str> {
    text(snap.market.as_ref()?.regime.as_ref()?.robust_state.as_ref())
}

fn read_as_of(snap: &Snapshot) -> Option<&str> {
    text(snap.atlas_read.as_ref()?.as_of.as_ref())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bus::{AppEvent, Channel};
    use crate::model::Snapshot;
    use serde_json::json;

    fn snap(value: serde_json::Value) -> AppEvent {
        // Through the real decoder, not a hand-built struct: a diff that reads a
        // field the owner never fills is a bug the fixture path would hide.
        AppEvent::Snapshot(Box::new(serde_json::from_value::<Snapshot>(value).unwrap()))
    }

    #[test]
    fn a_halt_announces_itself_and_a_resume_is_a_separate_trigger() {
        let mut store = Store::default();
        assert_eq!(store.apply(snap(json!({"portfolio": {"halted": false}}))), vec![]);
        assert_eq!(
            store.apply(snap(json!({"portfolio": {"halted": true}}))),
            vec![Trigger::Halted]
        );
        // Still halted is not a new halt — the effect is already running.
        assert_eq!(store.apply(snap(json!({"portfolio": {"halted": true}}))), vec![]);
        assert_eq!(
            store.apply(snap(json!({"portfolio": {"halted": false}}))),
            vec![Trigger::Resumed]
        );
    }

    #[test]
    fn opening_onto_a_halted_desk_still_announces_the_halt() {
        // The transition happened before this client existed. Waiting for one
        // that will never be sent again would render a halted desk quietly.
        let mut store = Store::default();
        assert_eq!(
            store.apply(snap(json!({"live_portfolio": {"halted": true}}))),
            vec![Trigger::Halted]
        );
    }

    #[test]
    fn the_live_book_decides_the_halt_and_a_vanished_flag_is_not_a_resume() {
        let mut store = Store::default();
        assert_eq!(
            store.apply(snap(
                json!({"live_portfolio": {"halted": true}, "portfolio": {"halted": false}})
            )),
            vec![Trigger::Halted]
        );
        // The owner stopped reporting the flag. That is missing information,
        // not a resume, and clearing the HALT effect on it would be a lie.
        assert_eq!(store.apply(snap(json!({"portfolio": {"equity": 1.0}}))), vec![]);
    }

    #[test]
    fn only_a_changed_robust_state_flips_the_regime() {
        let mut store = Store::default();
        let calm = json!({"market": {"regime": {"robust_state": "calm"}}});
        assert_eq!(store.apply(snap(calm.clone())), vec![Trigger::RegimeChanged]);
        assert_eq!(store.apply(snap(calm)), vec![]);
        assert_eq!(
            store.apply(snap(json!({"market": {"regime": {"robust_state": "stress"}}}))),
            vec![Trigger::RegimeChanged]
        );
    }

    #[test]
    fn an_empty_string_is_absent_not_a_change() {
        // The owner serialises an unset string as `""`. Diffing it as a value
        // would fire a regime flip on every sparse payload.
        let mut store = Store::default();
        assert_eq!(
            store.apply(snap(json!({"market": {"regime": {"robust_state": ""}}}))),
            vec![]
        );
        assert_eq!(
            store.apply(snap(json!({"market": {"regime": {"robust_state": "calm"}}}))),
            vec![Trigger::RegimeChanged]
        );
        assert_eq!(
            store.apply(snap(json!({"market": {"regime": {"robust_state": ""}}}))),
            vec![]
        );
    }

    #[test]
    fn a_new_read_retriggers_the_reveal() {
        let mut store = Store::default();
        let first = json!({"atlas_read": {"as_of": "2026-07-30T12:00:00Z"}});
        assert_eq!(store.apply(snap(first.clone())), vec![Trigger::ReadChanged]);
        assert_eq!(store.apply(snap(first)), vec![], "the same read is not a new one");
        assert_eq!(
            store.apply(snap(json!({"atlas_read": {"as_of": "2026-07-30T12:30:00Z"}}))),
            vec![Trigger::ReadChanged]
        );
    }

    #[test]
    fn one_snapshot_can_carry_several_transitions() {
        let mut store = Store::default();
        store.apply(snap(json!({
            "portfolio": {"halted": false},
            "market": {"regime": {"robust_state": "calm"}},
            "atlas_read": {"as_of": "t0"}
        })));
        let triggers = store.apply(snap(json!({
            "portfolio": {"halted": true},
            "market": {"regime": {"robust_state": "stress"}},
            "atlas_read": {"as_of": "t1"}
        })));
        assert!(triggers.contains(&Trigger::Halted));
        assert!(triggers.contains(&Trigger::RegimeChanged));
        assert!(triggers.contains(&Trigger::ReadChanged));
    }

    #[test]
    fn a_snapshot_is_always_dirty_even_when_nothing_triggers() {
        // Numbers move without any of the motion triggers firing; the frame
        // still owes the operator the new ones.
        let mut store = Store::default();
        store.apply(snap(json!({"portfolio": {"equity": 1.0}})));
        assert!(store.take_dirty());
        assert!(!store.take_dirty(), "taking the flag clears it");
        assert_eq!(store.apply(snap(json!({"portfolio": {"equity": 2.0}}))), vec![]);
        assert!(store.take_dirty());
    }

    #[test]
    fn a_tick_is_not_a_state_change() {
        // The tick drives the glyph, not the store. If it dirtied the store the
        // pacing rule would be decorative — every beat would force a frame.
        let mut store = Store::default();
        assert_eq!(store.apply(AppEvent::Tick), vec![]);
        assert!(!store.take_dirty());
    }

    #[test]
    fn a_connection_transition_is_dirty_once_not_every_poll() {
        let mut store = Store::default();
        assert!(!store.conn.owner);
        store.apply(AppEvent::ConnUp(Channel::Owner));
        assert!(store.conn.owner);
        assert!(store.take_dirty());
        store.apply(AppEvent::ConnUp(Channel::Owner));
        assert!(!store.take_dirty(), "a repeat of the same state repaints nothing");
        store.apply(AppEvent::ConnDown(Channel::Owner));
        assert!(store.take_dirty());
        assert!(!store.conn.owner && !store.conn.stream);
    }
}
