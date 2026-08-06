//! WORKFORCE — what the governed workforce is doing, and the two things a human may ask of it.
//!
//! Three stacked panes answering three questions in the order an operator asks
//! them. The pipelines say *where each run has got to*; the console says *what
//! just happened*, in the order it happened; and the input row is where a human
//! puts a question or starts a run.
//!
//! The two top panes read different sources for the reason AUDIT's do: a
//! workflow's shape is a *state* and the poll is the account of state, while an
//! event is a *moment* and no aggregate can put moments back in order. A pane
//! that derived the console from the workflow rows would show five phases
//! completing in seq order however they actually landed.
//!
//! **Registering a workflow is not running it.** That distinction is the whole
//! reason the coordinator chip sits in the header beside the pipelines: phases
//! advance only while a coordinator walks them, so a parked run and a live one
//! carry identical `running` statuses and identical amber nodes. `driving` is
//! the only evidence, and when the owner is *not* driving it says why — a quiet
//! desk that cannot explain itself is the failure the Atlas surfaces were
//! rebuilt to fix.
//!
//! What the keys can do is the posture's decision, not the build's, exactly as
//! on AUDIT. In the glass build there is no `Command::Message`, no
//! `Command::StartWorkflow` and no writer to reach, so the input row is absent
//! by construction; in a featured build that a human did not arm the row is
//! *hidden* rather than disabled, because a prompt an operator cannot type into
//! is a client that looks broken.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::Workflow;
use crate::store::{AuditEvent, Store};
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::event_row;
use crate::ui::widgets::pipeline;
use crate::ui::widgets::{panel_block, panel_header, refuse};
#[cfg(feature = "operator")]
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};
use std::time::{Duration, Instant};

/// The kinds the console shows: everything a governed run emits, plus the
/// questions a human put to the desk.
///
/// A filter rather than the whole bus, because this pane sits under the
/// pipelines and answers "why did that phase move" — AUDIT is where the whole
/// record lives, and a console carrying approvals and fills would be a second,
/// worse copy of it. `atlas_message` is here because a question and the phase it
/// provoked are one story, and reading them in two places loses the order.
/// `atlas_coordinator_event` is here for the reason the pane exists: a phase
/// moving is the desk's account of a run, and the agent's own sentence is the
/// run's. Reading only the first is how a governed session became a status word.
pub const CONSOLE_KINDS: [&str; 7] = [
    "workflow_started",
    "workflow_phase",
    "workflow_interrupted",
    "workflow_resumed",
    "workflow_abandoned",
    "atlas_message",
    "atlas_coordinator_event",
];

/// How long a driving coordinator may say nothing before the pane says so.
///
/// Long enough that a single long tool call is not reported as silence — the
/// owner's own dispatches routinely run tens of seconds — and short enough that
/// a wedged run is named while an operator is still watching. The number is the
/// whole contract of this line: SILENCE IS REPORTED, NOT ANIMATED. A spinner
/// that spun regardless would claim progress the desk cannot observe.
pub const SILENCE_AFTER: Duration = Duration::from_secs(45);

/// The workflow id, as a prefix plus its space. Eight characters of a
/// sixteen-character digest is far past what distinguishes two runs on any desk.
const ID_W: usize = 9;
/// `interrupted` is the longest status the registry writes, at eleven.
const STATUS_W: usize = 12;
/// `999h` and a space — see `format::span` for why this is a span and not an age.
const SPAN_W: usize = 5;
/// Under this a goal is not a sentence, it is a word and an ellipsis.
const GOAL_MIN: usize = 16;
/// The phase column on a pipeline's own line.
const PHASE_W: usize = 12;

/// The pane's floor: the marker, the columns, and a goal worth reading.
const PANE_MIN_W: u16 = (1 + ID_W + STATUS_W + GOAL_MIN + SPAN_W) as u16;
/// A header and one workflow's two lines, plus a header and one console row.
const PANE_MIN_H: u16 = 6;

/// The picker's own floor, in rows of the *view's* area.
///
/// Four rows inside the box are not optional — the header, one template, the
/// goal field, and the sentence about whose phase graph actually runs — plus
/// its two borders, and `centred` insets the box by two rows top and bottom.
///
/// It is a floor of its own because the view's is far lower. At `PANE_MIN_H`
/// the box came out two rows tall with an inner height of zero: nothing drawn,
/// no template list, no goal field, no disclosure — while the field still held
/// the keyboard and Enter still started a governed run. An armed control an
/// operator cannot see is worse than one that refuses, so below this the picker
/// refuses to open and says what it would take.
#[cfg(feature = "operator")]
const PICKER_MIN_H: u16 = 10;

/// How much of the frame the pipelines take before the console gets the rest.
///
/// Deliberately not a fixed height: a workflow occupies two lines and the owner
/// serves ten, so a fixed pane would either waste rows on a quiet desk or clip
/// the runs on a busy one. The console's floor is what keeps this from eating
/// the whole frame.
const PIPELINES_SHARE: u16 = 58;

/// Where the operator is looking, and what they have typed. Never what the desk
/// says — that is the `Store`'s.
#[derive(Default)]
pub struct WorkforceView {
    /// What is typed into the input row, when the row has focus.
    ///
    /// `Some("")` is a focused, empty field and `None` is an unfocused one —
    /// the one place in this client where those two differ, because here the
    /// distinction is about the operator rather than about the owner.
    #[cfg(feature = "operator")]
    ask: Option<String>,
    /// The template picker, while the operator is asking for it.
    ///
    /// *Asking for*, not *showing*: below [`PICKER_MIN_H`] the pane refuses to
    /// draw the box and says so instead, and this stays `Some` only until the
    /// next keystroke retires it. What must never happen is the third state —
    /// a box that is armed and invisible.
    #[cfg(feature = "operator")]
    picker: Option<Picker>,
    /// The view's height at the last frame, published by `draw`.
    ///
    /// Interior mutability for the same reason `fx::ShellRects` has it: `draw`
    /// is a `&self` renderer that publishes the layout it derived, and whether
    /// the picker fits is a fact about that layout. Nothing *renders* from it —
    /// the frame stays a pure function of (store, effects, instant) — it only
    /// decides whether a key may open a box that would not fit, which is a
    /// question about the last frame and can only be answered by it.
    #[cfg(feature = "operator")]
    height: std::cell::Cell<u16>,
}

/// The picker's state: which template is under the cursor, and the goal typed
/// so far.
#[cfg(feature = "operator")]
#[derive(Default)]
struct Picker {
    at: usize,
    goal: String,
}

impl View for WorkforceView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant) {
        if area.width < PANE_MIN_W || area.height < PANE_MIN_H {
            refuse(
                f,
                area,
                format!(
                    "WORKFORCE needs {PANE_MIN_W} columns for a pipeline beside its goal; \
                     this pane has {}.",
                    area.width
                ),
            );
            return;
        }

        // The input row is a *row*, taken out of the layout before anything
        // else claims it. Hidden entirely in the glass posture rather than
        // drawn disabled: a prompt that cannot be typed into is a client that
        // looks broken, and the status line already says which window this is.
        let rows = Layout::vertical([
            Constraint::Percentage(PIPELINES_SHARE),
            Constraint::Min(0),
            Constraint::Length(u16::from(self.input_row(store))),
        ])
        .split(area);

        self.draw_pipelines(f, rows[0], store, now);
        draw_console(f, rows[1], store, fx, now);
        self.draw_input(f, rows[2], store);
        // Over the view rather than over the frame: the question it asks is
        // about this pane's own controls, unlike the confirm box, which asks
        // about an order and belongs to the whole workstation.
        self.draw_picker(f, area, store);
    }

    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        self.keys(k, store)
    }

    /// Whether a text field on this pane currently owns the keyboard.
    ///
    /// True only while the operator is actually typing — into the input row or
    /// into the picker's goal field. An always-on claim would cost this view
    /// `q`, `r` and the digits permanently, which is the whole workstation's
    /// navigation for the sake of a field nobody is using.
    fn typing(&self) -> bool {
        #[cfg(feature = "operator")]
        {
            // A picker too tall for the pane is drawn as a refusal, not as a
            // box, so it holds no keyboard. This is the load-bearing half of
            // that: without it the goal field would still swallow every key —
            // and Enter would still start a run — against a box nobody can see.
            self.ask.is_some() || (self.picker.is_some() && self.picker_fits())
        }
        #[cfg(not(feature = "operator"))]
        false
    }
}

// -- the keys ---------------------------------------------------------------

#[cfg(feature = "operator")]
impl WorkforceView {
    /// Whether this window draws an input row at all.
    fn input_row(&self, store: &Store) -> bool {
        store.posture.writes()
    }

    /// Whether the last frame left room to draw the picker.
    ///
    /// Read off the height `draw` published, because the floor is a fact about
    /// the pane and a key handler is never told one. Zero before the first
    /// frame, which refuses — a client that has not drawn cannot know it has
    /// room, and the runtime draws once before it reads its first event.
    fn picker_fits(&self) -> bool {
        self.height.get() >= PICKER_MIN_H
    }

    /// Every key this pane claims, gated on the posture rather than the build.
    ///
    /// The order is load-bearing: an open picker outranks a focused input row,
    /// which outranks the two keys that open them. A field that shared the
    /// alphabet with its own shortcuts would make `i` untypeable inside a goal.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn keys(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        if !store.posture.writes() {
            // Same rule as AUDIT's arrows: a key with no visible effect reads as
            // a hung client, so an unarmed window declines rather than swallows.
            return None;
        }
        // A terminal that shrank under an open picker retires it, on the first
        // key after the resize. The box is already refusing to draw and already
        // holding no keyboard by then — this is what keeps the *state* from
        // outliving the box too, so a later resize back up cannot restore a
        // half-typed goal the operator has not seen since.
        if self.picker.is_some() && !self.picker_fits() {
            self.picker = None;
            return None;
        }
        if self.picker.is_some() {
            return self.picker_key(k, store);
        }
        if self.ask.is_some() {
            return self.ask_key(k);
        }
        match k.code {
            KeyCode::Char('i') => self.ask = Some(String::new()),
            KeyCode::Char('S') => self.picker = Some(Picker::default()),
            _ => {}
        }
        None
    }

    /// The input row's keys. Enter sends, Esc abandons, and nothing else leaves
    /// this pane.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn ask_key(&mut self, k: KeyEvent) -> Option<Command> {
        let typed = self.ask.as_mut()?;
        match k.code {
            KeyCode::Char(c) => typed.push(c),
            KeyCode::Backspace => {
                typed.pop();
            }
            KeyCode::Esc => self.ask = None,
            KeyCode::Enter => {
                let text = typed.trim().to_string();
                // An empty question is not a question. Sending one would put a
                // blank row on the audit log and the owner would refuse it
                // anyway ("message text is required"), which would reach the
                // operator as a failed write rather than as a slip.
                if text.is_empty() {
                    return None;
                }
                self.ask = None;
                return Some(Command::Message(text));
            }
            _ => {}
        }
        None
    }

    /// The picker's keys: a cursor over the owner's templates, a goal field,
    /// and the one key that starts a run.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn picker_key(&mut self, k: KeyEvent, store: &Store) -> Option<Command> {
        let rows = store.templates().len();
        let picker = self.picker.as_mut()?;
        match k.code {
            // Walls at both ends, as every other cursor on this workstation.
            KeyCode::Up => picker.at = picker.at.saturating_sub(1),
            KeyCode::Down => picker.at = (picker.at + 1).min(rows.saturating_sub(1)),
            KeyCode::Char(c) => picker.goal.push(c),
            KeyCode::Backspace => {
                picker.goal.pop();
            }
            KeyCode::Esc => self.picker = None,
            KeyCode::Enter => {
                let goal = picker.goal.trim().to_string();
                let template = format::text(
                    store
                        .templates()
                        .get(picker.at.min(rows.saturating_sub(1)))?
                        .template_id
                        .as_ref(),
                )?
                .to_string();
                // The box stays up on an empty goal rather than starting with
                // the owner's default. A run nobody stated a purpose for is one
                // whose record cannot later be read for intent — and the phase
                // graph the owner runs is its own (see `draw_picker`), so the
                // goal is the *only* thing this key contributes.
                if goal.is_empty() {
                    return None;
                }
                self.picker = None;
                return Some(Command::StartWorkflow { template, goal });
            }
            _ => {}
        }
        None
    }
}

#[cfg(not(feature = "operator"))]
impl WorkforceView {
    /// No row, and no branch that could grow one: the commands it would send
    /// are not in this build.
    fn input_row(&self, _store: &Store) -> bool {
        false
    }

    fn keys(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

// -- the pipelines ----------------------------------------------------------

impl WorkforceView {
    fn draw_pipelines(&self, f: &mut Frame, area: Rect, store: &Store, now: Instant) {
        let block = panel_block();
        let inner = block.inner(area);
        f.render_widget(block, area);

        let mut lines = vec![head("WORKFLOWS", &coordinator_chip(store), inner.width)];
        let room = inner.height.saturating_sub(1) as usize;
        let flows = store.workflows();
        if flows.is_empty() {
            lines.push(dim("no workflow has run on this desk yet"));
        }
        // Newest first, in the owner's own order — `list_workflows` is
        // `ORDER BY created_at DESC` — because a pane that re-sorted would be a
        // different account of the same ten rows every poll. Two lines each, so
        // what fits is counted in pairs rather than in rows.
        let driving = driving_id(store);
        // Derived here rather than inside the loop so the pane cannot draw two
        // different accounts of one desk, and computed against the frame's own
        // instant — the age is data, not a clock read in a renderer.
        // `driving` is the owner's own claim, not "this pane found a row to put
        // a marker on": a coordinator that reports no workflow id is still
        // driving, and asking the marker would quietly answer a different
        // question than the one the line is about.
        let said = activity(is_driving(store), store.last_agent_event_at(), now);
        // Which run the line goes under, asked of the coordinator rather than
        // of the marker, so the two questions stay separate.
        //
        // `Some("")` is absent here as everywhere: an empty coordinator id must
        // never match an empty flow id, which `format::text` would otherwise
        // make it do by turning both into `None`.
        let at = store
            .coordinator()
            .and_then(|c| format::text(c.workflow_id.as_ref()));
        // A driving coordinator that reports no workflow id is a real state —
        // `driver.busy` and `current_workflow_id` are read separately
        // (`qlab/ui/server.py`), so a dispatch can be seen between them — and
        // the derived fact must not be dropped because there is no row to hang
        // it on. It goes under the header instead, belonging to no run.
        if at.is_none() && lines.len() <= room {
            if let Some((silence, text)) = &said {
                lines.push(activity_row(*silence, text));
            }
        }
        for flow in flows.iter().take(room / 2) {
            lines.push(headline(flow, driving, inner.width));
            lines.push(graph(flow, store.tick, inner.width));
            // Placement only. *Whether* there is anything to say is
            // `activity`'s decision and nothing else's — a second opinion here
            // is how a pane comes to report liveness the derivation refused.
            // The row check is the pane's floor: a line pushed past it would
            // silently displace another run's pipeline.
            let on_this = at.is_some() && at == format::text(flow.workflow_id.as_ref());
            if on_this && lines.len() <= room {
                if let Some((silence, text)) = &said {
                    lines.push(activity_row(*silence, text));
                }
            }
        }
        f.render_widget(Paragraph::new(lines), inner);
    }
}

/// One workflow's first line: whether the coordinator is on it, its id, its
/// status, its goal, and the span the owner has recorded for it.
fn headline(flow: &Workflow, driving: Option<&str>, width: u16) -> Line<'static> {
    let t = theme();
    let id = format::text(flow.workflow_id.as_ref());
    let status = format::text(flow.status.as_ref()).unwrap_or(MISSING);
    // The marker means "a coordinator is walking this one", not "this one is
    // running". They are different claims and the desk has been wrong about it
    // before: a registered workflow reads `running` forever with nobody on it.
    let live = driving.is_some() && driving == id;
    let span = format::span(flow.created_at.as_ref(), flow.updated_at.as_ref());
    // The goal takes what the fixed columns leave. It is the only column here
    // that can be shortened without becoming a different fact.
    let goal_w = (width as usize)
        .saturating_sub(1 + ID_W + STATUS_W + SPAN_W)
        .max(GOAL_MIN);
    Line::from(vec![
        Span::styled(
            if live { "▌" } else { " " },
            Style::default().fg(t.positive),
        ),
        Span::styled(
            format!("{:<ID_W$}", event_row::short(flow.workflow_id.as_ref(), 8)),
            Style::default().fg(t.text_primary),
        ),
        Span::styled(
            format!("{status:<STATUS_W$}"),
            Style::default()
                .fg(status_tone(status))
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("{:<goal_w$}", clip(goal_of(flow), goal_w)),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(
            span.unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_tertiary),
        ),
    ])
}

/// One workflow's second line: the pipeline, then what the run last said.
fn graph(flow: &Workflow, tick: u64, width: u16) -> Line<'static> {
    let t = theme();
    let mut spans = vec![Span::raw("  ")];
    spans.extend(pipeline::line(&flow.steps, tick).spans);

    // The pipeline's width is arithmetic rather than a measurement, so what is
    // left for the words beside it is known before either is drawn.
    let used = 2 + pipeline::width(flow.steps.len());
    let room = (width as usize).saturating_sub(used + 2);
    if room >= PHASE_W {
        let phase = format::text(flow.current_phase.as_ref()).unwrap_or(MISSING);
        spans.push(Span::styled(
            format!("  {:<PHASE_W$}", clip(phase, PHASE_W)),
            Style::default().fg(t.text_tertiary),
        ));
        // What the run last said, whichever phase said it. A summary is the one
        // thing on this pane written by the agents themselves, so it is worth
        // the whole rest of the line.
        spans.push(Span::styled(
            clip(latest_summary(flow), room - PHASE_W),
            Style::default().fg(t.text_dim),
        ));
    }
    Line::from(spans)
}

/// The most recent thing any phase of this run has said.
///
/// Walked backwards over the steps, which the owner serves in `seq` order, so a
/// finished run shows its reporter rather than its analyst. Empty when nothing
/// has been written yet, which is the honest rendering of a run that has only
/// been registered.
fn latest_summary(flow: &Workflow) -> &str {
    flow.steps
        .iter()
        .rev()
        .find_map(|step| format::text(step.summary.as_ref()))
        .unwrap_or_default()
}

/// What a run was asked to do. The owner puts it in the request body it
/// persisted, which is the only place it exists.
fn goal_of(flow: &Workflow) -> &str {
    flow.request
        .as_ref()
        .and_then(|request| request.get("goal"))
        .and_then(|goal| goal.as_str())
        .filter(|goal| !goal.is_empty())
        .unwrap_or(MISSING)
}

/// What each workflow status means, in colour.
///
/// The three the registry writes plus `complete`. An unrecognised one renders
/// dim rather than taking the theme's default foreground, exactly as AUDIT's
/// approval statuses do: a status this client has never heard of must not read
/// as a live run.
fn status_tone(status: &str) -> ratatui::style::Color {
    let t = theme();
    match status {
        "running" => t.accent,
        "complete" => t.positive,
        "interrupted" => t.warning,
        "abandoned" | "failed" | "blocked" => t.negative,
        _ => t.text_dim,
    }
}

/// What the desk can honestly say about a live run's activity, if anything.
///
/// A pure function of three facts, none of them a clock read: whether a
/// coordinator is walking anything, when this client last heard the workforce
/// speak, and the instant the frame is being drawn at. `None` is the common
/// answer — a desk nobody is driving has no activity to report, and the age of
/// the last thing a finished run said is not liveness.
///
/// Past `SILENCE_AFTER` the line names the silence rather than dressing it up.
/// That is the whole design: an operator has to be able to tell a run that is
/// thinking from one that has stopped, and only the desk's own record can say.
pub fn activity_line(driving: bool, last: Option<Instant>, now: Instant) -> Option<String> {
    activity(driving, last, now).map(|(_, said)| said)
}

/// What a run's activity is, and whether it is silence.
///
/// One decision, made once. The tone was briefly re-derived by the renderer
/// reading `"no word for"` off the front of this function's own output, which
/// is a second decision made from a formatted string — rewording the line there
/// would have silently reverted silence to the dim tone of ordinary progress.
fn activity(driving: bool, last: Option<Instant>, now: Instant) -> Option<(Silence, String)> {
    if !driving {
        return None;
    }
    // Driving with nothing heard is a distinct fact from silence: the run may
    // be seconds old, and "no word for 0s" would read as a stall at startup.
    let Some(last) = last else {
        return Some((Silence::No, "no word yet".to_string()));
    };
    // Saturating: `now` is the frame's instant and `last` an arrival stamped on
    // another thread, so the two can cross by a hair. A negative age here would
    // be a panic behind the alternate screen.
    let age = now.saturating_duration_since(last);
    Some(if age >= SILENCE_AFTER {
        (Silence::Yes, format!("no word for {}s", age.as_secs()))
    } else {
        (Silence::No, format!("spoke {}s ago", age.as_secs()))
    })
}

/// Whether an activity line is reporting silence. Carried beside the words
/// rather than recovered from them.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Silence {
    Yes,
    No,
}

/// The activity line as it is drawn: under the phase node, in the tone of what
/// it says. Silence is a warning colour because it is one.
fn activity_row(silence: Silence, text: &str) -> Line<'static> {
    let t = theme();
    let tone = match silence {
        Silence::Yes => t.warning,
        Silence::No => t.text_dim,
    };
    Line::from(vec![
        Span::styled("  ↳ ", Style::default().fg(t.text_tertiary)),
        Span::styled(text.to_string(), Style::default().fg(tone)),
    ])
}

/// Whether the owner says a coordinator is walking anything at all.
fn is_driving(store: &Store) -> bool {
    store
        .coordinator()
        .is_some_and(|c| c.driving.unwrap_or(false))
}

/// The workflow the owner's coordinator is actually walking, if any.
fn driving_id(store: &Store) -> Option<&str> {
    let coordinator = store.coordinator()?;
    coordinator
        .driving
        .unwrap_or(false)
        .then(|| format::text(coordinator.workflow_id.as_ref()))
        .flatten()
}

/// What the header says about the coordinator.
///
/// Three states, because "not driving" splits in two and the split is the whole
/// point: an owner that *cannot* drive says why, and one that simply has
/// nothing to do says that instead. A pane that showed only the first would
/// leave an operator watching a parked pipeline with no way to tell whether the
/// desk was broken or idle.
fn coordinator_chip(store: &Store) -> String {
    let Some(coordinator) = store.coordinator() else {
        return "coordinator unreported".to_string();
    };
    if coordinator.driving.unwrap_or(false) {
        return match format::text(coordinator.workflow_id.as_ref()) {
            Some(id) => format!("driving {}", event_row::head_of(id, 8)),
            None => "driving".to_string(),
        };
    }
    match format::text(coordinator.reason.as_ref()) {
        Some(reason) => format!("idle — {reason}"),
        None => "idle".to_string(),
    }
}

// -- the console ------------------------------------------------------------

/// The workforce's own slice of the durable bus, newest first.
fn draw_console(f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines = vec![head("CONSOLE", "newest first", inner.width)];
    let room = inner.height.saturating_sub(1) as usize;
    let mut drawn = 0usize;
    for event in store.audit_events().filter(|e| in_console(e)).take(room) {
        lines.push(event_row::row(event, fx, now, inner.width));
        drawn += 1;
    }
    if drawn == 0 {
        lines.push(dim("the workforce has said nothing yet"));
    }
    f.render_widget(Paragraph::new(lines), inner);
}

/// Whether one bus row belongs on this console.
pub fn in_console(event: &AuditEvent) -> bool {
    CONSOLE_KINDS.contains(&event.kind.as_str())
}

// -- the operator's row -----------------------------------------------------

#[cfg(feature = "operator")]
impl WorkforceView {
    fn draw_input(&self, f: &mut Frame, area: Rect, store: &Store) {
        if !store.posture.writes() || area.height == 0 {
            return;
        }
        let t = theme();
        let line = match &self.ask {
            // Focused: the prompt, what has been typed, and a block for the
            // caret. A field with no caret is one an operator cannot tell from
            // a label.
            Some(typed) => Line::from(vec![
                Span::styled("> ", Style::default().fg(t.accent)),
                Span::styled(
                    typed.clone(),
                    Style::default()
                        .fg(t.text_primary)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled("▏", Style::default().fg(t.accent)),
                Span::styled(
                    "   Enter asks · Esc cancels",
                    Style::default().fg(t.text_dim),
                ),
            ]),
            None => Line::from(vec![
                Span::styled("  i ", Style::default().fg(t.accent)),
                Span::styled("ask the desk", Style::default().fg(t.text_secondary)),
                Span::styled("    S ", Style::default().fg(t.accent)),
                Span::styled("start a workflow", Style::default().fg(t.text_secondary)),
            ]),
        };
        f.render_widget(Paragraph::new(line), area);
    }

    /// The template picker, drawn over the view that opened it.
    fn draw_picker(&self, f: &mut Frame, area: Rect, store: &Store) {
        use ratatui::widgets::{Block, Borders, Clear};
        // Published first, and on every frame including the ones that draw no
        // box: this is what a later keystroke reads to decide whether the
        // picker may open, and a height only recorded when the box already fits
        // could never report that it stopped fitting.
        self.height.set(area.height);
        let Some(picker) = &self.picker else {
            return;
        };
        let t = theme();
        // Refuse rather than open invisible. Below the floor the box would have
        // an inner height of zero — no list, no goal field, no disclosure —
        // and `typing` declines the keyboard for exactly this frame, so the
        // refusal is the whole of what the operator gets.
        if area.height < PICKER_MIN_H {
            let row = Rect {
                x: area.x,
                y: area.y + area.height / 2,
                width: area.width,
                height: 1,
            };
            f.render_widget(Clear, row);
            refuse(
                f,
                row,
                format!(
                    "the template picker needs {PICKER_MIN_H} rows; this pane has {}.",
                    area.height
                ),
            );
            return;
        }
        let rect = centred(area);
        if rect.width == 0 || rect.height == 0 {
            return;
        }
        f.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(t.accent))
            .style(Style::default().bg(t.bg_raised));
        let inner = block.inner(rect);
        f.render_widget(block, rect);

        let mut lines = vec![panel_header("start a workflow")];
        let templates = store.templates();
        if templates.is_empty() {
            // "Not yet" and "not there" are different facts, and both are
            // possible here: the templates poll runs on a sixty-second beat.
            lines.push(dim("the owner has served no templates yet"));
        }
        // Three rows of chrome — the header, the goal prompt, and the footnote —
        // leave the rest for the list.
        let room = inner.height.saturating_sub(3) as usize;
        let at = picker.at.min(templates.len().saturating_sub(1));
        let top = at.saturating_sub(room.saturating_sub(1));
        for (i, template) in templates.iter().enumerate().skip(top).take(room) {
            let id = format::or_missing(template.template_id.as_ref());
            let purpose = format::or_missing(template.purpose.as_ref());
            // The authority marker. The owner refuses a plan-creating template
            // below `propose` mode, so an operator has to be able to see which
            // request is going to be declined before they make it.
            let creates_plan = template.creates_plan.unwrap_or(false);
            lines.push(Line::from(vec![
                Span::styled(
                    if i == at { "▌" } else { " " },
                    Style::default().fg(t.accent),
                ),
                Span::styled(
                    format!("{:<22}", clip(id, 22)),
                    Style::default()
                        .fg(t.text_primary)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    if creates_plan { "plan " } else { "     " },
                    Style::default().fg(t.warning),
                ),
                Span::styled(
                    // Saturating, not `-`: the view's own floor keeps this
                    // above zero today with nine cells to spare, but an
                    // arithmetic underflow in a render path is a panic behind
                    // the alternate screen, which is the one failure mode a
                    // fullscreen client cannot report.
                    clip(purpose, (inner.width as usize).saturating_sub(28)),
                    Style::default().fg(t.text_secondary),
                ),
            ]));
        }
        lines.push(Line::from(vec![
            Span::styled("goal > ", Style::default().fg(t.accent)),
            Span::styled(
                picker.goal.clone(),
                Style::default()
                    .fg(t.text_primary)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled("▏", Style::default().fg(t.accent)),
        ]));
        // Stated because it is a documented property of the route this key
        // calls, and an operator who picked `estimation_panel` would otherwise
        // watch the owner's standard five phases appear and read it as a bug:
        // `/api/workflows/start` will not take a phase graph from a network
        // caller, by design, so the template names the *intent* and the owner
        // chooses the graph.
        lines.push(dim(
            "the owner runs its own phase graph for a run started here",
        ));
        f.render_widget(Paragraph::new(lines), inner);
    }
}

#[cfg(not(feature = "operator"))]
impl WorkforceView {
    fn draw_input(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}
    fn draw_picker(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}
}

/// The picker's rect: most of the view, centred, and never larger than it.
#[cfg(feature = "operator")]
fn centred(area: Rect) -> Rect {
    let w = area.width.saturating_sub(4).max(1);
    let h = area.height.saturating_sub(4).clamp(1, 16);
    Rect {
        x: area.x + (area.width.saturating_sub(w)) / 2,
        y: area.y + (area.height.saturating_sub(h)) / 2,
        width: w,
        height: h,
    }
}

// -- shared shapes ----------------------------------------------------------

/// A panel header with its note pushed to the far side of the pane.
fn head(title: &str, note: &str, width: u16) -> Line<'static> {
    let t = theme();
    let mut spans = panel_header(title).spans;
    let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
    let note = clip(note, (width as usize).saturating_sub(used + 1));
    let pad = (width as usize).saturating_sub(used + note.chars().count());
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(note, Style::default().fg(t.text_dim)));
    Line::from(spans)
}

fn dim(text: &str) -> Line<'static> {
    Line::from(Span::styled(
        text.to_string(),
        Style::default().fg(theme().text_dim),
    ))
}

/// `text` in at most `width` cells, cut at a word boundary and marked as cut.
///
/// The alternative — a hard slice — is what turns "Review the current portfolio
/// and market read." into "Review the current portfo", which reads as a
/// different goal rather than as a shortened one. A single token longer than the
/// column has no honest break in it, so that one *is* cut hard; the ellipsis is
/// what says so either way, and it is the whole reason the cut is never silent.
fn clip(text: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    if text.chars().count() <= width {
        return text.to_string();
    }
    // One cell is the ellipsis's.
    let room = width - 1;
    let head: String = text.chars().take(room).collect();
    let cut = match head.rfind(char::is_whitespace) {
        // Only when a word boundary leaves something to read. Backing off to a
        // space in the first cell or two would render the whole column as `…`.
        Some(at) if at >= room / 2 => head[..at].trim_end().to_string(),
        _ => head.trim_end().to_string(),
    };
    format!("{cut}…")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_clipped_goal_is_shortened_at_a_word_and_says_that_it_was() {
        let goal = "Review the current portfolio and market read.";
        assert_eq!(clip(goal, 100), goal, "what fits is never touched");
        assert_eq!(clip(goal, goal.chars().count()), goal, "exactly is a fit");
        assert_eq!(clip(goal, 20), "Review the current…");
        assert_eq!(clip(goal, 12), "Review the…");
        // Never mid-word, and never wider than the column it was given.
        for width in 1..goal.chars().count() {
            let out = clip(goal, width);
            assert!(out.chars().count() <= width, "{width}: {out}");
            assert!(out.ends_with('…'), "{width}: a cut has to say it was cut");
        }
    }

    #[test]
    fn a_single_token_longer_than_the_column_is_cut_rather_than_blanked() {
        // There is no word boundary to back off to, and a column of `…` says
        // less than a cut prefix does.
        let id = "workflow-805e0729cfec4d67-estimation-panel";
        assert_eq!(clip(id, 10), "workflow-…");
        // And backing off to a boundary in the first cell or two must not blank
        // the column either.
        assert_eq!(clip("a verylongtokenindeed", 12), "a verylongt…");
    }

    #[test]
    fn clipping_counts_cells_and_not_bytes() {
        // A goal an agent wrote can carry anything. Slicing on bytes panics on
        // a character boundary, which is a crash in the render path.
        assert_eq!(clip("régime review of the desk", 7), "régime…");
        assert_eq!(clip("→→→→→→→→→→", 4), "→→→…");
        assert_eq!(clip("", 4), "");
        assert_eq!(clip("anything", 0), "");
    }

    #[test]
    fn every_status_a_workflow_can_carry_has_a_tone_and_an_unknown_one_is_dim() {
        let t = theme();
        assert_eq!(status_tone("running"), t.accent);
        assert_eq!(status_tone("complete"), t.positive);
        assert_eq!(status_tone("interrupted"), t.warning);
        assert_eq!(status_tone("abandoned"), t.negative);
        assert_eq!(status_tone("something-new"), t.text_dim);
    }

    #[test]
    fn silence_is_reported_rather_than_animated() {
        let t0 = Instant::now();
        let secs = std::time::Duration::from_secs;
        // Parked says nothing: an age with nobody walking the run is not
        // liveness, it is the age of the last thing a finished run said.
        assert_eq!(activity_line(false, Some(t0), t0), None);
        assert_eq!(
            activity_line(true, Some(t0), t0 + secs(3)).unwrap(),
            "spoke 3s ago"
        );
        assert_eq!(
            activity_line(true, Some(t0), t0 + secs(47)).unwrap(),
            "no word for 47s"
        );
        // Both sides of the threshold, because the threshold is a comparison
        // and a comparison with one case is an untested one.
        assert!(activity_line(true, Some(t0), t0 + secs(44))
            .unwrap()
            .starts_with("spoke"));
        assert!(activity_line(true, Some(t0), t0 + secs(45))
            .unwrap()
            .starts_with("no word"));
        // Driving with nothing heard yet is its own fact, and it is not silence
        // measured from a clock read this function was never given.
        assert_eq!(
            activity_line(true, None, t0).unwrap(),
            "no word yet",
            "a run that has said nothing is not a run that went quiet"
        );
        // A stamp from the future is a monotonic clock and a store that raced,
        // not a negative age.
        assert_eq!(
            activity_line(true, Some(t0 + secs(9)), t0).unwrap(),
            "spoke 0s ago"
        );
    }

    #[test]
    fn silence_is_drawn_in_the_colour_of_a_warning_and_progress_is_not() {
        // The tone is carried beside the words rather than recovered from them.
        // While it was parsed back off the rendered string, rewording the line
        // would have quietly turned silence into ordinary progress.
        let t = theme();
        let t0 = Instant::now();
        let secs = std::time::Duration::from_secs;
        let tone_of = |driving, last, now| {
            let (silence, text) = activity(driving, last, now).unwrap();
            activity_row(silence, &text).spans[1].style.fg.unwrap()
        };
        assert_eq!(tone_of(true, Some(t0), t0 + secs(3)), t.text_dim);
        assert_eq!(tone_of(true, Some(t0), t0 + SILENCE_AFTER), t.warning);
        // A run that has said nothing *yet* has not gone quiet, and must not
        // wear the colour of a desk that has.
        assert_eq!(tone_of(true, None, t0), t.text_dim);
    }

    #[test]
    fn the_console_shows_the_workforce_and_leaves_the_rest_of_the_bus_to_audit() {
        let row = |kind: &str| AuditEvent {
            id: None,
            ts: None,
            kind: kind.to_string(),
            payload: serde_json::Value::Null,
        };
        for kind in CONSOLE_KINDS {
            assert!(in_console(&row(kind)), "{kind}");
        }
        // The bus carries twenty kinds. A console that took the governance ones
        // too would be a second, worse copy of AUDIT under the pipelines.
        for kind in [
            "approval_created",
            "plan_executed",
            "halt",
            "referee_verdict",
            "quote",
        ] {
            assert!(!in_console(&row(kind)), "{kind}");
        }
    }
}
