//! Toasts: the events a status chip cannot hold, said once and then let go.
//!
//! A chip states a *condition* — the stream is open, the numbers are current —
//! and stays as long as the condition does. A toast states an *event*: an
//! approval was created, a plan executed, the desk halted, a payload could not
//! be read. Those are moments, and a moment rendered as a chip is a chip that
//! either lies a second later or never goes out.
//!
//! Time is data here for the reason the whole client keeps it that way: `push`
//! is told when the event arrived and `visible` is told when the frame is being
//! drawn, so expiry is arithmetic a test can pin rather than a timer it has to
//! race. Nothing in this module reads a clock.
//!
//! The queue lives beside the `Store` in `main`, never inside it — same rule as
//! `Fx`. The store is what the owner said plus the diff of it; a box that
//! disappears after four seconds is neither.

use crate::bus::{AppEvent, HttpResult, SseEvent};
use crate::store::IDLE_FRAME;
use crate::theme::theme;
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};
use serde_json::Value;
use std::time::{Duration, Instant};

/// How long one toast stays on screen.
///
/// Long enough to read a title and a line at a glance away from the desk, short
/// enough that a burst does not bury the frame it is drawn over.
pub const LIFE: Duration = Duration::from_secs(4);

/// How many are on screen at once. A fourth box would reach a third of the way
/// down the frame, at which point the toasts are the view.
pub const MAX: usize = 3;

/// One toast's box: a border, a title row, a message row, and a border.
pub const W: u16 = 40;
pub const H: u16 = 4;

/// Under this the box is two rules and no message, so nothing is drawn at all —
/// a frame missing a toast beats a frame with a hole in it.
const MIN_W: u16 = 20;

/// How often a visible toast owes the terminal a frame.
///
/// The idle heartbeat, deliberately: an age counting up in whole seconds needs
/// to be sampled about ten times a second to land on the right one, and this
/// costs no extra wakes over a quiet desk. Faster would be paying the effect
/// lane's rate for a number that changes once a second.
pub const FRAME: Duration = IDLE_FRAME;

/// How loud a toast is. Three, matching the three things an operator does about
/// one: nothing, look, act.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    /// The desk did something it was allowed to do.
    Info,
    /// Something is degraded and still running.
    Warn,
    /// Something stopped, or cannot be read.
    Alarm,
}

impl Level {
    fn tone(self) -> Color {
        let t = theme();
        match self {
            Level::Info => t.accent,
            Level::Warn => t.warning,
            Level::Alarm => t.negative,
        }
    }
}

/// One thing that happened.
///
/// No stamp: the queue owns *when*, because a toast pushed twice is one event
/// the desk repeated and the arrival that matters is the queue's record of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Toast {
    pub level: Level,
    pub title: String,
    pub message: String,
}

impl Toast {
    pub fn new(level: Level, title: &str, message: String) -> Self {
        Self {
            level,
            title: title.to_string(),
            message,
        }
    }
}

/// What is currently on screen, newest first.
#[derive(Debug, Default)]
pub struct ToastQueue {
    live: Vec<(Toast, Instant)>,
}

impl ToastQueue {
    /// Record one event.
    ///
    /// An identical toast that is still visible is not new news — a broken owner
    /// republishes its failure every poll, and three copies of one sentence
    /// stacked over the desk say less than one does. The original keeps its own
    /// expiry rather than being refreshed: the box is about the moment the
    /// condition was first seen, and the chips are what say it is still true.
    pub fn push(&mut self, toast: Toast, now: Instant) {
        self.live.retain(|(_, at)| alive(*at, now));
        if self.live.iter().any(|(held, _)| *held == toast) {
            return;
        }
        self.live.push((toast, now));
        // Bounded at the point of record, so a burst cannot grow this between
        // frames — `visible` caps what is drawn, not what is held.
        while self.live.len() > MAX {
            self.live.remove(0);
        }
    }

    /// What to draw at `now`, newest first, with how long each has been up.
    pub fn visible(&self, now: Instant) -> Vec<(&Toast, Duration)> {
        self.live
            .iter()
            .rev()
            .filter(|(_, at)| alive(*at, now))
            .take(MAX)
            .map(|(toast, at)| (toast, now.saturating_duration_since(*at)))
            .collect()
    }

    /// Whether anything is on screen, and therefore whether an expiry is still
    /// owed a frame. Fed to `should_render`'s effect path for that reason: a
    /// toast the loop never repaints stays on screen until the next event.
    pub fn active(&self, now: Instant) -> bool {
        self.live.iter().any(|(_, at)| alive(*at, now))
    }

    /// How long the loop may sleep before the toasts owe a frame, or `None`.
    pub fn budget(&self, now: Instant) -> Option<Duration> {
        self.active(now).then_some(FRAME)
    }

    /// Draw the stack into the top-right of `area`.
    ///
    /// An overlay pass, called after the shell has painted and before the effect
    /// managers run: the boxes are part of the frame the effects tint, which is
    /// what makes a toast during a halt read as belonging to the halted desk.
    pub fn draw(&self, f: &mut Frame, area: Rect, now: Instant) {
        let t = theme();
        let width = W.min(area.width);
        if width < MIN_W || area.height < H {
            return;
        }
        for (i, (toast, age)) in self.visible(now).into_iter().enumerate() {
            // Under the ticker tape: the tape is one of the three indicators
            // that claim this client is alive, and a box over it would hide the
            // one row that says so.
            let y = area.y + 1 + i as u16 * H;
            if y + H > area.bottom() {
                return;
            }
            let rect = Rect {
                x: area.right() - width,
                y,
                width,
                height: H,
            };
            // Whatever the shell drew underneath is not showing through a box
            // that is meant to be on top of it.
            f.render_widget(Clear, rect);
            let block = Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(toast.level.tone()))
                .style(Style::default().bg(t.bg_raised));
            let inner = block.inner(rect);
            f.render_widget(block, rect);
            f.render_widget(
                Paragraph::new(vec![
                    title_line(toast, age, inner.width as usize),
                    Line::from(Span::styled(
                        head(&toast.message, inner.width as usize),
                        Style::default().fg(t.text_secondary),
                    )),
                ]),
                inner,
            );
        }
    }
}

/// `● TITLE` on the left, how long it has been up on the right.
///
/// The age is what tells an operator who looked away whether the box is about
/// something that just happened or something from four seconds ago — the same
/// question `STALE` answers for the numbers.
fn title_line(toast: &Toast, age: Duration, width: usize) -> Line<'static> {
    let t = theme();
    let stamp = format!("{}s", age.as_secs());
    let dot = "● ";
    let room = width.saturating_sub(dot.chars().count() + stamp.chars().count() + 1);
    let title = head(&toast.title.to_uppercase(), room);
    let pad =
        width.saturating_sub(dot.chars().count() + title.chars().count() + stamp.chars().count());
    Line::from(vec![
        Span::styled(dot, Style::default().fg(toast.level.tone())),
        Span::styled(
            title,
            Style::default()
                .fg(t.text_primary)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw(" ".repeat(pad)),
        Span::styled(stamp, Style::default().fg(t.text_tertiary)),
    ])
}

fn alive(at: Instant, now: Instant) -> bool {
    now.saturating_duration_since(at) < LIFE
}

/// The first `width` characters, cut on a character rather than a byte: a
/// headline carries `—` and `’`, and slicing one in half panics.
fn head(text: &str, width: usize) -> String {
    match text.char_indices().nth(width) {
        Some((cut, _)) => text[..cut].to_string(),
        None => text.to_string(),
    }
}

// -- what deserves one -----------------------------------------------------

/// The toast one bus event is worth, or `None` for the events that are not
/// moments.
///
/// A pure function of the event so the wiring has a test: the routing is the
/// half of this that invariant 10 says goes wrong — a queue that exists and
/// nothing pushes to is a queue that never runs.
pub fn for_event(ev: &AppEvent) -> Option<Toast> {
    match ev {
        AppEvent::Sse(event) => from_stream(event),
        #[cfg(feature = "operator")]
        AppEvent::Wrote(outcome) => Some(from_write(outcome)),
        // The one HTTP outcome that is an event rather than a condition. The
        // chip says the owner is answering; this says what it answered with.
        AppEvent::Http(HttpResult::Malformed { url, error }) => Some(Toast::new(
            Level::Alarm,
            "owner payload malformed",
            format!("{} — from {url}", first_line(error)),
        )),
        _ => None,
    }
}

/// What the owner said about a write this operator asked for.
///
/// Three outcomes and three different things to do about them, which is why the
/// levels differ. A refusal is `Alarm`, not `Info`: the desk considered a fill
/// and declined it, and a box that read like a receipt would tell an operator a
/// trade was booked when it was not. That is the failure this whole path exists
/// to prevent, and the colour is where an operator actually meets it.
///
/// The booked case deliberately words itself *exactly* as the stream's own
/// `plan_executed` toast. Both arrive — the write returns and the owner
/// publishes — and the queue drops an identical toast that is already up, so one
/// fill is one box rather than two saying the same thing in different words.
#[cfg(feature = "operator")]
fn from_write(outcome: &crate::bus::Wrote) -> Toast {
    use crate::bus::Wrote;
    match outcome {
        Wrote::Executed { plan_id } => Toast::new(
            Level::Info,
            "plan executed",
            format!("plan {plan_id} booked a paper fill"),
        ),
        Wrote::Refused {
            blocked_by,
            reasons,
            ..
        } => Toast::new(
            Level::Alarm,
            "fill refused",
            // The gate's own word plus its first reason. `Execution::read`
            // guarantees the list is never empty, because a refusal an operator
            // cannot read is not actionable.
            match reasons.first() {
                Some(why) => format!("{blocked_by}: {why}"),
                None => format!("the desk blocked this fill ({blocked_by})"),
            },
        ),
        Wrote::Decided {
            approval_id,
            decision,
        } => Toast::new(
            Level::Info,
            &format!("approval {decision}"),
            format!("{approval_id} is on the record as {decision}"),
        ),
        // The owner's own sentence, not a receipt of this client's own making.
        // `atlas_message` answers 200 whether or not a coordinator exists to
        // read the question — "coordinator unavailable; Atlas is degraded and
        // cannot answer" is a 200 — so a box saying "sent" would report a
        // question as asked of something that cannot hear it. `Warn` for that
        // case, because it is a degraded desk and not a delivery.
        Wrote::Asked { note } => Toast::new(
            if note.contains("unavailable") {
                Level::Warn
            } else {
                Level::Info
            },
            "desk asked",
            note.clone(),
        ),
        Wrote::Started {
            template,
            workflow_id,
        } => Toast::new(
            Level::Info,
            "workflow started",
            // "registered", not "running": registering a workflow is not
            // driving it, and only the coordinator's own `driving` flag says
            // the phases are advancing. The pipeline pane shows which it is.
            format!("{template} registered as {workflow_id}"),
        ),
        // `Warn` when the owner took a book it cannot reach, `Info` otherwise.
        // A desk pointed at Alpaca with no usable login changed and cannot
        // trade, and an `Info` box would report that as a clean switch.
        Wrote::Pointed { label, warning } => Toast::new(
            match warning {
                Some(_) => Level::Warn,
                None => Level::Info,
            },
            "desk mode",
            match warning {
                Some(why) => format!("{label} — {why}"),
                None => format!("the desk is now {label}"),
            },
        ),
        // The owner's own account of what the choice means, which is not always
        // what an operator expects: naming a reasoner model does not switch the
        // reasoner on, and the owner says so in the same sentence. A box that
        // read "model set" would hide exactly that.
        Wrote::Chose { said } => Toast::new(Level::Info, "model routing", said.clone()),
        // `Warn`, not `Alarm`: the desk considered the choice and declined it,
        // and nothing moved. The sentence carries the remedy — "start it with
        // `ollama serve`" — which is the half that has to survive.
        Wrote::ChoiceRefused { said } => {
            Toast::new(Level::Warn, "model choice refused", said.clone())
        }
        // The owner's own verdict on what it stored, never a receipt this
        // client composed: a login can be written and still be shadowed by an
        // environment pair, which is a file that changed and a desk that cannot
        // trade. `Warn` for that, as for the book it cannot reach.
        Wrote::LoggedIn { usable, note } => Toast::new(
            match usable {
                true => Level::Info,
                false => Level::Warn,
            },
            "alpaca login",
            note.clone(),
        ),
        // A question, not news — and it has a box because the form that asked
        // it is on one pane. An operator who navigated away while the request
        // was in flight would otherwise be left with a consent question drawn
        // where they are not looking. The owner's sentence, verbatim.
        Wrote::LoginNeedsConsent { said } => {
            Toast::new(Level::Warn, "alpaca login not stored", said.clone())
        }
        Wrote::LoginRefused { said } => {
            Toast::new(Level::Warn, "alpaca login refused", said.clone())
        }
        // `Warn`, not `Alarm`: a venue that will not take the stored key is a
        // desk that cannot trade the real book, which is the same severity as
        // pointing at that book without a login. The request itself worked.
        Wrote::Tested { ok, summary } => Toast::new(
            match ok {
                true => Level::Info,
                false => Level::Warn,
            },
            match ok {
                true => "alpaca reachable",
                false => "alpaca refused the login",
            },
            summary.clone(),
        ),
        Wrote::Failed { what, said } => {
            Toast::new(Level::Alarm, "write failed", format!("{what} — {said}"))
        }
    }
}

fn from_stream(event: &SseEvent) -> Option<Toast> {
    let payload = &event.payload;
    match event.kind.as_str() {
        // The owner writes `{approval_id, plan_id}` (`qlab/ui/server.py:1804`,
        // `qlab/autopilot/loop.py:652`). The plan is the half an operator can
        // act on, so it leads.
        "approval_created" => Some(Toast::new(
            Level::Warn,
            "approval requested",
            match field(payload, &["plan_id", "approval_id"]) {
                Some(what) => format!("{what} is waiting on a human"),
                None => "the desk is waiting on a human".to_string(),
            },
        )),
        // `{plan_id, n_fills, n_replayed}` — `qlab/trader/plan.py:355`.
        "plan_executed" => Some(Toast::new(
            Level::Info,
            "plan executed",
            match field(payload, &["plan_id"]) {
                Some(id) => format!("plan {id} booked a paper fill"),
                None => "a checked plan booked a paper fill".to_string(),
            },
        )),
        // The sentence behind the chip pulse. `Trigger::PhaseAdvanced` crosses
        // the status line's chip run from every view, which is right — a
        // governed run moving is desk news wherever an operator is looking —
        // but amber crossing the chips with nothing saying why is a pulse they
        // learn to read past. This is what keeps that pulse meaning something
        // on MARKETS and BOOK, where no pipeline is drawn.
        //
        // `Info`, not `Warn`: a phase advancing is the desk getting on with
        // work it was already doing. The two lifecycle events that *are* a
        // human's problem — `workflow_interrupted` and `workflow_abandoned` —
        // are the console's and AUDIT's, and giving them a box is a decision
        // for whoever builds the controls that resolve them.
        //
        // The owner writes `{workflow_id, phase, agent, status, summary}`
        // (`registry.py:1596`). The workflow's *goal* is deliberately not
        // reached for: it lives in the snapshot's `request`, not on the event,
        // and a toast that read the store would stop being a function of the
        // moment it is about. The phase's own summary is the better line
        // anyway — it is what that role just said, rather than what the run was
        // asked for twenty minutes ago.
        "workflow_phase" => Some(Toast::new(
            Level::Info,
            &match field(payload, &["phase"]) {
                Some(phase) => format!("phase {phase}"),
                None => "workflow phase".to_string(),
            },
            match (
                field(payload, &["summary"]),
                field(payload, &["status"]),
                field(payload, &["workflow_id"]),
            ) {
                (Some(summary), _, _) => summary,
                (None, Some(status), Some(id)) => format!("{status} on {id}"),
                (None, Some(status), None) => format!("the phase is {status}"),
                (None, None, Some(id)) => format!("workflow {id} moved"),
                (None, None, None) => "a governed run advanced a phase".to_string(),
            },
        )),
        "halt" => Some(Toast::new(
            Level::Alarm,
            "desk halted",
            match field(payload, &["reason", "why", "by"]) {
                Some(why) => format!("the live book is halted: {why}"),
                None => "the live book is halted".to_string(),
            },
        )),
        // The audit stream losing rows is the failure a governed desk may not
        // have quietly. The chip counts them; this is what says one just went.
        "stream.malformed" => Some(Toast::new(
            Level::Warn,
            "stream frame dropped",
            match field(payload, &["raw"]) {
                Some(raw) => format!("could not read {raw}"),
                None => "a frame the client could not read".to_string(),
            },
        )),
        _ => None,
    }
}

/// The first of `keys` the payload actually set, as text.
///
/// `Some("")` is absent, exactly as everywhere else in this client, and a number
/// reads as its own text rather than as nothing — the owner's ids are strings
/// but its counts are not.
fn field(payload: &Value, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| match payload.get(key)? {
        Value::String(text) if !text.is_empty() => Some(text.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    })
}

/// The line of a decode error that says what is wrong; serde appends the path
/// it walked, which is longer than the box.
fn first_line(error: &str) -> &str {
    error.lines().next().unwrap_or_default().trim()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn info(title: &str) -> Toast {
        Toast::new(Level::Info, title, "body".to_string())
    }

    fn sse(kind: &str, payload: Value) -> AppEvent {
        AppEvent::Sse(SseEvent {
            kind: kind.to_string(),
            payload,
            ts: Some("2026-07-30T12:00:00+00:00".into()),
            id: Some("e1".into()),
        })
    }

    #[test]
    fn a_toast_expires_on_its_own_clock_and_the_clock_is_data() {
        let start = Instant::now();
        let mut q = ToastQueue::default();
        assert!(!q.active(start), "nothing has happened yet");

        q.push(info("halted"), start);
        assert!(q.active(start));
        assert_eq!(q.visible(start).len(), 1);
        assert_eq!(q.visible(start)[0].1, Duration::ZERO);

        // The age is what the box prints, so it has to advance.
        let mid = start + Duration::from_millis(2_500);
        assert_eq!(q.visible(mid)[0].1, Duration::from_millis(2_500));
        assert!(q.active(mid));

        assert!(
            !q.active(start + LIFE),
            "a toast that never expires is a chip, and a chip is a different claim"
        );
        assert!(q.visible(start + LIFE).is_empty());
        assert_eq!(q.budget(mid), Some(FRAME));
        assert_eq!(
            q.budget(start + LIFE),
            None,
            "an expired queue must not pin the loop awake forever"
        );
    }

    #[test]
    fn three_stack_newest_first_and_a_fourth_pushes_the_oldest_off() {
        let start = Instant::now();
        let mut q = ToastQueue::default();
        for (i, title) in ["first", "second", "third", "fourth"].iter().enumerate() {
            q.push(info(title), start + Duration::from_millis(i as u64 * 10));
        }
        let titles: Vec<&str> = q
            .visible(start + Duration::from_millis(30))
            .iter()
            .map(|(toast, _)| toast.title.as_str())
            .collect();
        assert_eq!(
            titles,
            vec!["fourth", "third", "second"],
            "the newest is the one at the top, and the oldest has left"
        );
    }

    #[test]
    fn the_same_failure_every_poll_is_one_toast_and_keeps_its_own_expiry() {
        // A broken owner republishes its failure every three seconds. Three
        // copies of one sentence stacked over the desk say less than one does,
        // and a refreshed stamp would make the box permanent.
        let start = Instant::now();
        let mut q = ToastQueue::default();
        let bad = Toast::new(
            Level::Alarm,
            "owner payload malformed",
            "expected f64".into(),
        );
        q.push(bad.clone(), start);
        q.push(bad.clone(), start + Duration::from_secs(3));
        assert_eq!(q.visible(start + Duration::from_secs(3)).len(), 1);
        assert!(
            !q.active(start + LIFE),
            "the repeat refreshed a stamp it must not own"
        );

        // Once it has gone, the same failure is news again.
        q.push(bad, start + LIFE);
        assert_eq!(q.visible(start + LIFE).len(), 1);
    }

    #[test]
    fn every_source_the_brief_names_reaches_a_toast() {
        // Invariant 10 at the seam it keeps biting: a queue nothing pushes to is
        // indistinguishable from one that does not exist.
        let approval = for_event(&sse(
            "approval_created",
            json!({"approval_id": "ap-1", "plan_id": "pl-42"}),
        ))
        .expect("an approval is the one event a governed desk may not miss");
        assert_eq!(approval.level, Level::Warn);
        assert!(approval.message.contains("pl-42"), "{approval:?}");

        let executed = for_event(&sse("plan_executed", json!({"plan_id": "pl-42"})))
            .expect("a booked fill is a moment");
        assert_eq!(executed.level, Level::Info);
        assert!(executed.message.contains("pl-42"));

        let halt = for_event(&sse("halt", json!({"by": "tool"}))).expect("a halt is a moment");
        assert_eq!(halt.level, Level::Alarm);
        assert!(halt.message.contains("tool"), "{halt:?}");

        let dropped = for_event(&sse("stream.malformed", json!({"raw": "{oops"})))
            .expect("a dropped audit frame is a moment");
        assert_eq!(dropped.level, Level::Warn);
        assert!(dropped.message.contains("{oops"));

        let malformed = for_event(&AppEvent::Http(HttpResult::Malformed {
            url: "http://127.0.0.1:8765/api/tui".into(),
            error: "invalid type: string \"x\", expected f64\n  at line 4".into(),
        }))
        .expect("an unreadable payload is a moment");
        assert_eq!(malformed.level, Level::Alarm);
        assert!(malformed.message.contains("expected f64"));
        assert!(
            !malformed.message.contains("at line 4"),
            "the whole serde path does not fit a box: {malformed:?}"
        );
    }

    #[test]
    fn a_phase_advancing_says_which_phase_and_what_it_said() {
        // This pin was the other way round: `workflow_phase` was listed among
        // the events that "do not deserve a box". It inverted when the chip
        // pulse got its own effect key — the pulse crosses the status line from
        // every view, and on MARKETS or BOOK, where no pipeline is drawn, amber
        // with nothing explaining it is a pulse an operator learns to read past.
        let advanced = for_event(&sse(
            "workflow_phase",
            json!({"workflow_id": "805e0729", "phase": "referee", "agent": "referee",
                   "status": "done", "summary": "Targets check out against the mandate."}),
        ))
        .expect("the pulse on the chip run needs a sentence behind it");
        assert_eq!(advanced.level, Level::Info, "routine work is not an alarm");
        assert!(advanced.title.contains("referee"), "{advanced:?}");
        assert!(advanced.message.contains("mandate"), "{advanced:?}");

        // No summary yet — the common case for a phase that just opened. The
        // box still has to say something an operator can act on.
        let opened = for_event(&sse(
            "workflow_phase",
            json!({"workflow_id": "805e0729", "phase": "optimizer", "status": "working"}),
        ))
        .expect("a phase opening is a moment too");
        assert!(opened.title.contains("optimizer"), "{opened:?}");
        assert!(opened.message.contains("805e0729"), "{opened:?}");
        assert!(opened.message.contains("working"), "{opened:?}");
    }

    #[test]
    fn the_events_that_are_not_moments_get_nothing() {
        // A toast per quote frame would be a box on screen permanently, which is
        // the failure mode the four-second life exists to prevent.
        for kind in ["quote", "resume", "referee_verdict"] {
            assert!(
                for_event(&sse(kind, json!({}))).is_none(),
                "{kind} does not deserve a box"
            );
        }
        assert!(for_event(&AppEvent::Tick).is_none());
        assert!(for_event(&AppEvent::Resize).is_none());
    }

    #[test]
    fn a_payload_with_nothing_in_it_still_says_what_happened() {
        // The owner's payloads are contracts, and a contract that changed must
        // not leave a box with a blank line in it.
        for kind in [
            "approval_created",
            "plan_executed",
            "halt",
            "stream.malformed",
            "workflow_phase",
        ] {
            let toast = for_event(&sse(kind, json!({}))).expect(kind);
            assert!(!toast.title.is_empty(), "{kind}");
            assert!(!toast.message.is_empty(), "{kind}");
        }
        // An unset string is absent, exactly as everywhere else.
        let blank = for_event(&sse("plan_executed", json!({"plan_id": ""}))).unwrap();
        assert_eq!(blank.message, "a checked plan booked a paper fill");
    }
}
