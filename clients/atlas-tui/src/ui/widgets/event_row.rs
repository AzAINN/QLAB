//! One row of the durable audit bus: when, what kind, and what it was about.
//!
//! Extracted from `views::audit` once WORKFORCE grew a console pane over the
//! same ring. The two panes read the same events for different reasons — AUDIT
//! shows the whole bus, the console shows the six kinds a workforce run
//! produces — and a second spelling of "which colour is a governance event" or
//! "what is this row about" is how the two come to disagree about one desk.
//!
//! The flash is per row, keyed by the owner's own `event_id`, so the event that
//! just arrived lights and the log under it does not. A pane-wide effect would
//! animate the whole record every time anything happened on the desk.

use crate::format::{clock, MISSING};
use crate::fx::{FlashKey, FlashTracker};
use crate::store::AuditEvent;
use crate::theme::theme;
use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};
use std::time::Instant;

/// The kind column. `workflow_interrupted` is the owner's longest at twenty;
/// this holds every other kind whole and takes two characters off that one,
/// which costs less than the subject the width would otherwise come out of.
pub const KIND_W: usize = 18;

/// One bus row, rendered to `width` cells.
///
/// The subject is what gives when the pane is narrow: the clock and the kind are
/// what make a row findable, and at the workstation's baseline width two panes
/// side by side leave only a few characters for it. It grows back as the
/// terminal does.
pub fn row(event: &AuditEvent, fx: &FlashTracker, now: Instant, width: u16) -> Line<'static> {
    let t = theme();
    let base = Style::default().fg(tone(event));
    let lit = match event.id.as_deref() {
        Some(id) => fx.style_for(&FlashKey::audit(id), now, base),
        None => base,
    };
    let stamp = clock(event.ts.as_ref()).unwrap_or_else(|| MISSING.to_string());
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
/// The ids in that order because that is the order an operator looks for them: a
/// plan is what a fill or an approval is *about*, and the approval id is the
/// record. `text` comes last of the named keys, for `atlas_message` — the one
/// kind whose subject is a sentence rather than an id, and whose whole content
/// would otherwise render as JSON in both panes that show it.
///
/// Anything else falls back to the whole payload rather than to nothing — an
/// unrecognised event on a governed bus is exactly the one worth reading.
pub fn subject(event: &AuditEvent) -> String {
    // The coordinator's rows are the one family whose subject is *who said
    // what*. They carry a `workflow_id` too, and the ids-first rule below would
    // render every agent's turn as the same eight hex characters — which is the
    // pane reporting a status word again, in a longer form.
    if event.kind == COORDINATOR {
        return coordinator_subject(event);
    }
    for key in [
        "plan_id",
        "approval_id",
        "workflow_id",
        "decision_id",
        "text",
    ] {
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

/// The bus kind the owner's coordinator republishes its agents' events under.
const COORDINATOR: &str = "atlas_coordinator_event";

/// A coordinator row read as a sentence: who spoke, then what they said.
///
/// The owner writes `agent`, `tool` and `text` on every one of these
/// (`qlab/operator/coordinator.py::_on_event`) and `_head` has already redacted
/// and collapsed them, so this only has to choose. `text` first because prose is
/// what a reader wants; `tool` is the fallback for a `tool_start` whose text is
/// empty, which is most of them. A row with neither still names its agent
/// rather than rendering blank — the agent is a fact, and the pane is a record.
fn coordinator_subject(event: &AuditEvent) -> String {
    let field = |key: &str| {
        event
            .payload
            .get(key)
            .and_then(|v| v.as_str())
            .filter(|v| !v.is_empty())
    };
    let agent = field("agent").unwrap_or("coordinator");
    match field("text").or_else(|| field("tool")) {
        Some(said) => format!("{agent}  {said}"),
        None => agent.to_string(),
    }
}

/// The tone one row carries.
///
/// A coordinator row's meaning is in its `event_kind`, not in the bus kind they
/// all share: an error and a tool call arriving in the same colour would make
/// the console's whole point — reading a run as it happens — a scan of prose.
pub fn tone(event: &AuditEvent) -> Color {
    let t = theme();
    if event.kind == COORDINATOR {
        return match event.payload.get("event_kind").and_then(|v| v.as_str()) {
            Some("error") => t.negative,
            Some("tool_start") => t.accent,
            _ => t.text_primary,
        };
    }
    kind_tone(&event.kind)
}

/// The tone one event kind carries.
///
/// Prefix matching rather than an exhaustive table: the owner adds bus kinds
/// (there are already twenty), and a client that coloured only the ones it knew
/// would silently render a new governance event as chrome.
pub fn kind_tone(kind: &str) -> Color {
    let t = theme();
    match kind {
        "halt" | "approval_rejected" | "approval_invalidated" => t.negative,
        "plan_executed" | "approval_approved" | "resume" => t.positive,
        "approval_created" | "approval_challenged" | "approval_expired" => t.warning,
        // The two lifecycle transitions a human has to notice inside the
        // workflow family, which is otherwise uniformly the desk's own colour.
        "workflow_interrupted" | "workflow_abandoned" => t.warning,
        _ if kind.starts_with("workflow") => t.accent,
        _ if kind.starts_with("approval") => t.warning,
        _ => t.text_secondary,
    }
}

/// The head of a string, cut on a character boundary. `--` when there is none.
pub fn short(value: Option<&String>, width: usize) -> String {
    match crate::format::text(value) {
        Some(text) => head_of(text, width),
        None => MISSING.to_string(),
    }
}

pub fn head_of(text: &str, width: usize) -> String {
    match text.char_indices().nth(width) {
        Some((cut, _)) => text[..cut].to_string(),
        None => text.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(kind: &str, payload: serde_json::Value) -> AuditEvent {
        AuditEvent {
            id: Some("e1".into()),
            ts: None,
            kind: kind.into(),
            payload,
        }
    }

    #[test]
    fn a_row_says_what_it_was_about_and_falls_back_to_the_payload() {
        assert_eq!(
            subject(&event(
                "approval_created",
                serde_json::json!({"approval_id": "ap", "plan_id": "pl"})
            )),
            "pl",
            "the plan is what a governance event is about"
        );
        assert_eq!(
            subject(&event("x", serde_json::json!({"approval_id": "ap"}))),
            "ap"
        );
        // The console's own case: a message is a sentence, and rendering it as
        // `{"text":"why flat?"}` puts JSON in front of an operator.
        assert_eq!(
            subject(&event(
                "atlas_message",
                serde_json::json!({"text": "why flat?"})
            )),
            "why flat?"
        );
        // An id still outranks it, so `workflow_phase` — which carries both —
        // stays keyed to the run it is about.
        assert_eq!(
            subject(&event(
                "workflow_phase",
                serde_json::json!({"workflow_id": "wf1", "text": "ignored"})
            )),
            "wf1"
        );
        // An unrecognised shape is the row most worth reading, so it is shown
        // whole rather than blanked.
        assert_eq!(
            subject(&event("halt", serde_json::json!({"reason": "kill switch"}))),
            r#"{"reason":"kill switch"}"#
        );
        assert_eq!(subject(&event("x", serde_json::Value::Null)), "");
        // `Some("")` is absent here as everywhere else in this client.
        assert_eq!(
            subject(&event("x", serde_json::json!({"plan_id": ""}))),
            "{\"plan_id\":\"\"}"
        );
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
    fn a_workflow_that_stopped_is_not_the_same_colour_as_one_that_advanced() {
        // The console draws all five workflow kinds. If they were one colour the
        // pane would say "a workflow did something" and nothing more, which is
        // the distinction an operator scans the log for.
        let t = theme();
        assert_eq!(kind_tone("workflow_phase"), t.accent);
        assert_eq!(kind_tone("workflow_started"), t.accent);
        assert_eq!(kind_tone("workflow_resumed"), t.accent);
        assert_eq!(kind_tone("workflow_interrupted"), t.warning);
        assert_eq!(kind_tone("workflow_abandoned"), t.warning);
    }
}
