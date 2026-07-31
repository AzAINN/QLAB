//! AUDIT — the human decision queue, and the durable record of what the desk did.
//!
//! Two panes answering two questions an operator asks in sequence. On the left,
//! the approvals: what is waiting on a human, and what a human has already
//! authorised but the desk has not yet spent. On the right, the audit bus: what
//! actually happened, newest first, as the registry recorded it.
//!
//! This is the first renderer the durable bus has ever had in this client. Until
//! now the stream existed only to nudge the poller and raise a four-second
//! toast, which means an operator who looked away had no way to find out what
//! they missed — on a governed desk, where the whole point of the bus is that
//! every decision leaves a trace, that is the one pane whose absence was itself
//! a governance gap.
//!
//! The two panes read different sources on purpose. Approvals come from the
//! snapshot, because status is a *state* and the poll is the account of state.
//! Events come from `Store::events_ring`, because an event is a *moment* and no
//! aggregate can put moments back in order.
//!
//! What the keys can do is decided by two gates in series, the same pair the
//! status badge answers. In the glass build the decision branches below are not
//! compiled at all — there is no `confirm::Host`, no `Command::Approve`, and no
//! writer for either to reach — so that artifact's read-only claim is absence
//! rather than a check. In a featured build the *posture* decides, because a
//! binary the human did not arm with `--operator` reads GLASS on the status
//! line, and a pane offering `a` there would contradict it.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::{FlashKey, FlashTracker};
use crate::model::Approval;
use crate::store::{AuditEvent, Store};
use crate::theme::theme;
use crate::ui::views::View;
#[cfg(feature = "operator")]
use crate::ui::widgets::confirm::{self, Modal, Pending};
use crate::ui::widgets::{panel_block, panel_header, refuse};
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};
use std::time::Instant;

/// The approvals pane's share of the view: the marker, an id, the plan it
/// binds, its status, and when it expires.
///
/// A fixed width rather than a ratio, and it is the *stream* that takes what is
/// left. The columns here are ids and words that do not compress — a status
/// clipped to `invalidat` is a status an operator has to guess at — while a bus
/// row degrades gracefully, dropping the tail of its subject first and keeping
/// the clock and the kind that make it findable.
///
/// The ids are shown as prefixes. Eleven characters of a sixteen-character
/// digest is far past what distinguishes two rows on any desk, and the record an
/// operator is actually deciding is named in full inside the confirm box.
const APPROVALS_W: u16 = 44;
const APPROVALS_MIN: u16 = 40;

/// The stream's floor: a clock, a kind, and a space between them. Under this
/// there is no row left to shorten.
const STREAM_MIN: u16 = 28;

/// The kind column. `workflow_interrupted` is the owner's longest at twenty;
/// this holds every other kind whole and takes two characters off that one,
/// which costs less than the subject the width would otherwise come out of.
const KIND_W: usize = 18;

/// Where the operator is looking in the approvals list. Never what the desk
/// says — that is the `Store`'s.
#[derive(Default)]
pub struct AuditView {
    selected: usize,
    /// The blocking question this pane is asking, if any.
    #[cfg(feature = "operator")]
    confirm: confirm::Host,
}

impl View for AuditView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant) {
        // The approvals pane takes a fixed width and the stream takes the rest:
        // a ratio would shrink the id column on a narrow terminal, and the ids
        // are the part that may not shrink. Under the two floors together the
        // view says so rather than drawing two unreadable columns.
        if area.width < APPROVALS_MIN + STREAM_MIN + 1 || area.height < 3 {
            refuse(
                f,
                area,
                format!(
                    "AUDIT needs {} columns for the approvals queue and the event \
                     stream; this pane has {}.",
                    APPROVALS_MIN + STREAM_MIN + 1,
                    area.width
                ),
            );
            return;
        }
        let left = APPROVALS_W.min(area.width.saturating_sub(STREAM_MIN + 1));
        // A column between them, or the expiry clock of the last approval runs
        // straight into the timestamp of the first event and reads as one
        // number.
        let cols = Layout::horizontal([Constraint::Length(left), Constraint::Min(0)])
            .spacing(1)
            .split(area);
        self.draw_approvals(f, cols[0], store);
        draw_stream(f, cols[1], store, fx, now);
    }

    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        let rows = store.approvals().len();
        match k.code {
            // Walls at both ends, exactly as every other cursor on this
            // workstation: an operator holding an arrow must never wrap onto a
            // decision at the other end of a queue they did not scroll to.
            KeyCode::Up => self.selected = self.cursor(rows).saturating_sub(1),
            KeyCode::Down => self.selected = (self.cursor(rows) + 1).min(rows.saturating_sub(1)),
            // Both decisions go through a typed word rather than a keypress.
            // There is no plan hash to bind here — approving is not booking —
            // but a decision that authorises a later fill is still not
            // something a stray keystroke may make.
            //
            // Reject is `R` and not `r` because the shell claims lowercase `r`
            // for the workstation's refresh, and a view may not take a binding
            // the whole client depends on — an operator who lost refresh on
            // this one pane would read it as a hung client. Shift also suits
            // the half of the pair that throws a decision away.
            #[cfg(feature = "operator")]
            KeyCode::Char('a') if store.posture.writes() => {
                self.ask(store, "approve", Pending::Approve)
            }
            #[cfg(feature = "operator")]
            KeyCode::Char('R') if store.posture.writes() => {
                self.ask(store, "reject", Pending::Reject)
            }
            _ => return None,
        }
        None
    }

    #[cfg(feature = "operator")]
    fn confirm(&self) -> Option<&confirm::Host> {
        Some(&self.confirm)
    }

    #[cfg(feature = "operator")]
    fn confirm_mut(&mut self) -> Option<&mut confirm::Host> {
        Some(&mut self.confirm)
    }
}

impl AuditView {
    /// The cursor, clamped to the queue actually on screen. The queue shrinks
    /// under the operator every time a decision is taken, so an index past the
    /// end has to render the last row rather than panic.
    fn cursor(&self, rows: usize) -> usize {
        self.selected.min(rows.saturating_sub(1))
    }

    /// Put the confirm box up for the selected approval, if it is one a human
    /// can still decide.
    ///
    /// Only `pending`. An approved record is the operator's own earlier
    /// decision and the owner refuses to re-decide it ("approval is 'approved',
    /// not pending"), so offering the key would teach the operator that the
    /// refusal is the client's fault.
    #[cfg(feature = "operator")]
    fn ask(&mut self, store: &Store, verb: &str, pending: fn(String) -> Pending) {
        let rows = store.approvals();
        let Some(approval) = rows.get(self.cursor(rows.len())) else {
            return;
        };
        if approval.status.as_deref() != Some("pending") {
            return;
        }
        let Some(id) = format::text(approval.approval_id.as_ref()) else {
            return;
        };
        let mut facts = vec![("approval".to_string(), id.to_string())];
        if let Some(plan) = format::text(approval.plan_id.as_ref()) {
            facts.push(("plan".to_string(), plan.to_string()));
        }
        if let Some(expires) = clock(approval.expires_at.as_ref()) {
            facts.push(("expires".to_string(), expires));
        }
        self.confirm.open(
            Modal::action(&format!("{} APPROVAL", verb.to_uppercase()), facts),
            pending(id.to_string()),
        );
    }

    fn draw_approvals(&self, f: &mut Frame, area: Rect, store: &Store) {
        let block = panel_block();
        let inner = block.inner(area);
        f.render_widget(block, area);
        let rows = store.approvals();
        // The header carries the keys, so the pane states this window's own
        // posture — read off the store, not off `cfg!(feature = "operator")`.
        // A featured binary the human did not arm reads GLASS on the status
        // line and must offer nothing here either.
        let keys = if store.posture.writes() {
            "a approve  R reject"
        } else {
            "view-only"
        };
        let mut lines = vec![head("APPROVALS", keys, inner.width)];

        // One row for the header; whatever is left is the queue. The cursor is
        // clamped to the *queue*, not to what fits, so a decision scrolled off
        // a short pane is still reachable by walking down to it.
        let room = inner.height.saturating_sub(1) as usize;
        if rows.is_empty() {
            lines.push(Line::from(Span::styled(
                "no approval is waiting on a human",
                Style::default().fg(theme().text_dim),
            )));
        }
        for (i, approval) in rows.iter().take(room).enumerate() {
            // A cursor only means something where a key can act on it.
            let selected = store.posture.writes() && i == self.cursor(rows.len());
            lines.push(approval_row(approval, selected));
        }
        f.render_widget(Paragraph::new(lines), inner);
    }
}

/// One approval: the marker, its id, the plan it binds, its status, its expiry.
fn approval_row(approval: &Approval, selected: bool) -> Line<'static> {
    let t = theme();
    let status = format::text(approval.status.as_ref()).unwrap_or(MISSING);
    Line::from(vec![
        Span::styled(
            if selected { "▌" } else { " " },
            Style::default().fg(t.accent),
        ),
        Span::styled(
            format!("{:<12}", short(approval.approval_id.as_ref(), 11)),
            Style::default().fg(t.text_primary),
        ),
        Span::styled(
            format!("{:<11}", short(approval.plan_id.as_ref(), 10)),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(
            format!("{status:<12}"),
            Style::default()
                .fg(status_tone(status))
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            clock(approval.expires_at.as_ref()).unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_tertiary),
        ),
    ])
}

/// What each approval status means, in colour.
///
/// Four tones for five words. `pending` is amber because it is the one that
/// wants an operator; `approved` is green because the desk may act on it;
/// `rejected` and `invalidated` are red because a decision was refused or lost
/// its subject; `consumed` and `expired` are dim because they are history.
fn status_tone(status: &str) -> Color {
    let t = theme();
    match status {
        "pending" => t.warning,
        "approved" => t.positive,
        "rejected" | "invalidated" => t.negative,
        _ => t.text_dim,
    }
}

/// The audit bus, newest first.
fn draw_stream(f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines = vec![head("EVENT STREAM", "newest first", inner.width)];
    let room = inner.height.saturating_sub(1) as usize;
    let mut drawn = 0usize;
    for event in store.audit_events().take(room) {
        lines.push(event_row(event, fx, now, inner.width));
        drawn += 1;
    }
    if drawn == 0 {
        lines.push(Line::from(Span::styled(
            "nothing on the bus yet",
            Style::default().fg(theme().text_dim),
        )));
    }
    f.render_widget(Paragraph::new(lines), inner);
}

/// One bus row: when, what kind, and what it was about.
///
/// The flash is per row, keyed by the owner's own event id, so the event that
/// just arrived lights and the log under it does not. A pane-wide effect would
/// animate the whole record every time anything happened on the desk.
fn event_row(event: &AuditEvent, fx: &FlashTracker, now: Instant, width: u16) -> Line<'static> {
    let t = theme();
    let base = Style::default().fg(kind_tone(&event.kind));
    let lit = match event.id.as_deref() {
        Some(id) => fx.style_for(&FlashKey::audit(id), now, base),
        None => base,
    };
    let stamp = clock(event.ts.as_ref()).unwrap_or_else(|| MISSING.to_string());
    // The subject is what gives when the pane is narrow: the clock and the kind
    // are what make a row findable, and at the workstation's baseline width the
    // two panes together leave only a few characters for it. It grows back as
    // the terminal does.
    let subject = subject(event);
    let room = (width as usize).saturating_sub(stamp.chars().count() + KIND_W + 2);
    Line::from(vec![
        Span::styled(format!("{stamp} "), Style::default().fg(t.text_tertiary)),
        Span::styled(format!("{:<KIND_W$} ", head_of(&event.kind, KIND_W)), lit),
        Span::styled(
            head_of(&subject, room),
            Style::default().fg(t.text_secondary),
        ),
    ])
}

/// What a bus row is about, from the payload the owner actually writes.
///
/// The ids in that order because that is the order an operator looks for them:
/// a plan is what a fill or an approval is *about*, and the approval id is the
/// record. Anything else falls back to the whole payload rather than to nothing
/// — an unrecognised event on a governed bus is exactly the one worth reading.
fn subject(event: &AuditEvent) -> String {
    for key in ["plan_id", "approval_id", "workflow_id", "decision_id"] {
        if let Some(value) = event.payload.get(key).and_then(|v| v.as_str()) {
            if !value.is_empty() {
                return value.to_string();
            }
        }
    }
    match &event.payload {
        serde_json::Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// The tone one event kind carries.
///
/// Prefix matching rather than an exhaustive table: the owner adds bus kinds
/// (there are already twenty), and a client that coloured only the ones it knew
/// would silently render a new governance event as chrome.
fn kind_tone(kind: &str) -> Color {
    let t = theme();
    match kind {
        "halt" | "approval_rejected" | "approval_invalidated" => t.negative,
        "plan_executed" | "approval_approved" | "resume" => t.positive,
        "approval_created" | "approval_challenged" | "approval_expired" => t.warning,
        _ if kind.starts_with("workflow") => t.accent,
        _ if kind.starts_with("approval") => t.warning,
        _ => t.text_secondary,
    }
}

/// A panel header with its keys pushed to the far side of the pane.
fn head(title: &str, keys: &str, width: u16) -> Line<'static> {
    let t = theme();
    let mut spans = panel_header(title).spans;
    let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
    let pad = (width as usize).saturating_sub(used + keys.chars().count());
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(
        keys.to_string(),
        Style::default().fg(t.text_dim),
    ));
    Line::from(spans)
}

/// `HH:MM:SS` out of an ISO stamp, or absent.
///
/// A slice of the owner's own string rather than a parse: the registry writes
/// ISO-8601 with a fixed offset, the client renders wall-clock time as the owner
/// stated it, and a timezone conversion here would be this client inventing a
/// clock the audit log does not have.
pub fn clock(stamp: Option<&String>) -> Option<String> {
    let text = format::text(stamp)?;
    let time: String = text.chars().skip(11).take(8).collect();
    (time.chars().count() == 8).then_some(time)
}

/// The head of an id, so a column of them lines up. `--` when there is none.
fn short(value: Option<&String>, width: usize) -> String {
    match format::text(value) {
        Some(text) => head_of(text, width),
        None => MISSING.to_string(),
    }
}

fn head_of(text: &str, width: usize) -> String {
    match text.char_indices().nth(width) {
        Some((cut, _)) => text[..cut].to_string(),
        None => text.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_clock_is_the_owners_own_wall_time_and_never_a_conversion() {
        assert_eq!(
            clock(Some(&"2026-07-30T18:12:18.773652+00:00".to_string())),
            Some("18:12:18".to_string())
        );
        // Absent, empty and malformed are all absent — a half-read stamp is
        // worse than none, because it looks like a time.
        assert_eq!(clock(None), None);
        assert_eq!(clock(Some(&String::new())), None);
        assert_eq!(clock(Some(&"2026-07-30".to_string())), None);
    }

    #[test]
    fn every_status_the_owner_can_serve_has_a_tone_and_pending_is_the_loud_one() {
        // The five the transition table can produce, plus `consumed`. A status
        // this client has never heard of must still render, dimly, rather than
        // taking the theme's default foreground and reading as a live row.
        let t = theme();
        assert_eq!(status_tone("pending"), t.warning);
        assert_eq!(status_tone("approved"), t.positive);
        assert_eq!(status_tone("rejected"), t.negative);
        assert_eq!(status_tone("invalidated"), t.negative);
        assert_eq!(status_tone("consumed"), t.text_dim);
        assert_eq!(status_tone("expired"), t.text_dim);
        assert_eq!(status_tone("something-new"), t.text_dim);
    }

    #[test]
    fn a_kind_the_client_has_never_seen_still_gets_a_tone_from_its_family() {
        let t = theme();
        assert_eq!(kind_tone("workflow_reassigned"), t.accent);
        assert_eq!(kind_tone("approval_deferred"), t.warning);
        assert_eq!(kind_tone("plan_executed"), t.positive);
        assert_eq!(kind_tone("halt"), t.negative);
        assert_eq!(kind_tone("something_else"), t.text_secondary);
    }

    #[test]
    fn a_row_says_what_it_was_about_and_falls_back_to_the_payload() {
        let event = |payload: serde_json::Value| AuditEvent {
            id: Some("e1".into()),
            ts: None,
            kind: "k".into(),
            payload,
        };
        assert_eq!(
            subject(&event(
                serde_json::json!({"approval_id": "ap", "plan_id": "pl"})
            )),
            "pl",
            "the plan is what a governance event is about"
        );
        assert_eq!(
            subject(&event(serde_json::json!({"approval_id": "ap"}))),
            "ap"
        );
        // An unrecognised shape is the row most worth reading, so it is shown
        // whole rather than blanked.
        assert_eq!(
            subject(&event(serde_json::json!({"reason": "kill switch"}))),
            r#"{"reason":"kill switch"}"#
        );
        assert_eq!(subject(&event(serde_json::Value::Null)), "");
        // `Some("")` is absent here as everywhere else in this client.
        assert_eq!(
            subject(&event(serde_json::json!({"plan_id": ""}))),
            "{\"plan_id\":\"\"}"
        );
    }
}
