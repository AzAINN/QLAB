//! Application state, and the diffing that decides which fields changed enough to trigger.
//!
//! Two jobs, deliberately in one place. The store holds what the owner said,
//! and it decides what *changed* — because a change is the only thing motion is
//! allowed to be about. An effect fired from a render pass would animate on
//! every repaint; an effect fired from a diff animates once, when the desk
//! actually moved.

use crate::bus::{AppEvent, Channel, HttpResult};
use crate::format::text;
use crate::glyph::Mood;
use crate::model::{RegimePanel, Snapshot};
use std::time::{Duration, Instant};

/// The idle heartbeat. Three indicators on screen claim the client is alive —
/// the glyph, the throbbers, the quote ages — and a frame this often is what
/// makes that claim true when no event has arrived.
const IDLE_FRAME: Duration = Duration::from_millis(100);

/// Whether the loop owes the terminal a frame.
///
/// `now` is a parameter, never a clock read: a rule that called `elapsed()`
/// would decide against a different instant than the caller measured, and its
/// own test would race a descheduled thread. It lives beside the store rather
/// than in `main` so it can be tested without a terminal. `fx_active` renders
/// unconditionally: effects are sampled per frame, and the 16 ms cadence they
/// want is the loop's wake interval, not a gate here.
pub fn should_render(dirty: bool, fx_active: bool, last_frame: Instant, now: Instant) -> bool {
    dirty || fx_active || now.saturating_duration_since(last_frame) >= IDLE_FRAME
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

/// The seven views, in nav-rail order.
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

impl ViewId {
    /// Nav order. The digit keys index this, so it is also the numbering an
    /// operator sees — the two cannot drift apart.
    pub const ALL: [ViewId; 7] = [
        ViewId::Desk,
        ViewId::Markets,
        ViewId::Book,
        ViewId::Research,
        ViewId::Workforce,
        ViewId::Audit,
        ViewId::Settings,
    ];

    /// At most five cells: the nav rail is eight wide and spends three of them
    /// on the active marker and the digit.
    pub fn label(self) -> &'static str {
        match self {
            ViewId::Desk => "DESK",
            ViewId::Markets => "MKTS",
            ViewId::Book => "BOOK",
            ViewId::Research => "RSCH",
            ViewId::Workforce => "WORK",
            ViewId::Audit => "AUDIT",
            ViewId::Settings => "SETT",
        }
    }

    /// The one-based position the digit keys use.
    pub fn index(self) -> usize {
        ViewId::ALL.iter().position(|v| *v == self).unwrap_or(0)
    }

    pub fn from_digit(digit: char) -> Option<ViewId> {
        let n = digit.to_digit(10)? as usize;
        if n == 0 {
            return None;
        }
        ViewId::ALL.get(n - 1).copied()
    }

    /// Cycling wraps in both directions: an operator holding Tab must never
    /// reach a wall and wonder whether the client stopped responding.
    pub fn next(self) -> ViewId {
        ViewId::ALL[(self.index() + 1) % ViewId::ALL.len()]
    }

    pub fn prev(self) -> ViewId {
        ViewId::ALL[(self.index() + ViewId::ALL.len() - 1) % ViewId::ALL.len()]
    }
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
    /// The animation beat, counted rather than read from a clock. Every frame
    /// the shell draws is a pure function of the store, so the phase an
    /// automaton is at has to be state — a renderer that called `Instant::now`
    /// could not be pinned by a golden frame.
    pub tick: u64,
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
            // The beat advances but does not dirty: the glyph is redrawn by the
            // idle heartbeat in the pacing rule, and dirtying here would force a
            // frame every 120 ms and make that rule decorative.
            AppEvent::Tick => self.tick = self.tick.wrapping_add(1),
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

    /// The manager's mood, derived from desk facts rather than set.
    ///
    /// Ported from `Desk::mood`. Derived is the whole point: an animation that
    /// can say "working" while the book is halted is worse than no animation.
    /// It lives here because the halt rule (the live book decides) is a store
    /// rule, and a view that re-derived it would be a second source for it.
    pub fn mood(&self) -> Mood {
        let Some(snapshot) = &self.snapshot else {
            return Mood::Dormant;
        };
        Mood::from_desk(
            halted(snapshot).unwrap_or(false),
            snapshot
                .atlas_heartbeat
                .as_ref()
                .and_then(|b| b.coordinator.as_ref())
                .and_then(|c| c.driving)
                .unwrap_or(false),
            snapshot
                .atlas
                .as_ref()
                .and_then(|a| text(a.mode.as_ref()))
                .unwrap_or_default(),
        )
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
        assert_eq!(
            store.apply(snap(json!({"portfolio": {"halted": false}}))),
            vec![]
        );
        assert_eq!(
            store.apply(snap(json!({"portfolio": {"halted": true}}))),
            vec![Trigger::Halted]
        );
        // Still halted is not a new halt — the effect is already running.
        assert_eq!(
            store.apply(snap(json!({"portfolio": {"halted": true}}))),
            vec![]
        );
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
        assert_eq!(
            store.apply(snap(json!({"portfolio": {"equity": 1.0}}))),
            vec![]
        );
    }

    #[test]
    fn only_a_changed_robust_state_flips_the_regime() {
        let mut store = Store::default();
        let calm = json!({"market": {"regime": {"robust_state": "calm"}}});
        assert_eq!(
            store.apply(snap(calm.clone())),
            vec![Trigger::RegimeChanged]
        );
        assert_eq!(store.apply(snap(calm)), vec![]);
        assert_eq!(
            store.apply(snap(
                json!({"market": {"regime": {"robust_state": "stress"}}})
            )),
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
            store.apply(snap(
                json!({"market": {"regime": {"robust_state": "calm"}}})
            )),
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
        assert_eq!(
            store.apply(snap(first)),
            vec![],
            "the same read is not a new one"
        );
        assert_eq!(
            store.apply(snap(
                json!({"atlas_read": {"as_of": "2026-07-30T12:30:00Z"}})
            )),
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
        assert_eq!(
            store.apply(snap(json!({"portfolio": {"equity": 2.0}}))),
            vec![]
        );
        assert!(store.take_dirty());
    }

    #[test]
    fn a_tick_advances_the_beat_without_being_a_state_change() {
        // The tick drives the glyph, not the desk. If it dirtied the store the
        // pacing rule would be decorative — every beat would force a frame.
        let mut store = Store::default();
        assert_eq!(store.apply(AppEvent::Tick), vec![]);
        assert_eq!(
            store.tick, 1,
            "the beat has to advance or the glyph freezes"
        );
        assert!(!store.take_dirty());
    }

    #[test]
    fn the_mood_is_derived_from_the_desk_not_set() {
        let mut store = Store::default();
        // Nothing seen yet is not "idle": an empty client must not animate as
        // though it were watching a desk.
        assert_eq!(store.mood(), Mood::Dormant);

        store.apply(snap(json!({"atlas": {"mode": "research"}})));
        assert_eq!(store.mood(), Mood::Idle);
        store.apply(snap(json!({
            "atlas": {"mode": "research"},
            "atlas_heartbeat": {"coordinator": {"driving": true}}
        })));
        assert_eq!(store.mood(), Mood::Working);
        // The live book decides, and a halt overrides a running coordinator.
        store.apply(snap(json!({
            "atlas": {"mode": "research"},
            "atlas_heartbeat": {"coordinator": {"driving": true}},
            "live_portfolio": {"halted": true}
        })));
        assert_eq!(store.mood(), Mood::Alarmed);
    }

    #[test]
    fn the_view_order_is_the_numbering_the_operator_sees() {
        assert_eq!(ViewId::from_digit('1'), Some(ViewId::Desk));
        assert_eq!(ViewId::from_digit('7'), Some(ViewId::Settings));
        assert_eq!(ViewId::from_digit('8'), None);
        assert_eq!(ViewId::from_digit('0'), None, "there is no view zero");
        for (i, id) in ViewId::ALL.iter().enumerate() {
            assert_eq!(
                ViewId::from_digit(char::from_digit(i as u32 + 1, 10).unwrap()),
                Some(*id)
            );
            assert!(
                id.label().chars().count() <= 5,
                "{id:?} does not fit the rail"
            );
        }
        // Wrapping in both directions: a wall would read as a hung client.
        assert_eq!(ViewId::Settings.next(), ViewId::Desk);
        assert_eq!(ViewId::Desk.prev(), ViewId::Settings);
    }

    #[test]
    fn a_connection_transition_is_dirty_once_not_every_poll() {
        let mut store = Store::default();
        assert!(!store.conn.owner);
        store.apply(AppEvent::ConnUp(Channel::Owner));
        assert!(store.conn.owner);
        assert!(store.take_dirty());
        store.apply(AppEvent::ConnUp(Channel::Owner));
        assert!(
            !store.take_dirty(),
            "a repeat of the same state repaints nothing"
        );
        store.apply(AppEvent::ConnDown(Channel::Owner));
        assert!(store.take_dirty());
        assert!(!store.conn.owner && !store.conn.stream);
    }
}
