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
use crate::format::{self, clock, MISSING};
use crate::fx::FlashTracker;
use crate::model::Approval;
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
#[cfg(feature = "operator")]
use crate::ui::widgets::confirm::{self, Modal, Pending};
use crate::ui::widgets::event_row::{self, short};
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
            //
            // Posture-gated like every other key on this pane, and for the same
            // reason the marker is: the only thing this cursor selects is a
            // decision, so an unarmed window has nothing to point at. Ungated it
            // swallowed the arrows to move a cursor it never drew — a key
            // pressed with no effect anyone can see reads as a hung client, and
            // the pane declining them leaves them free for whatever claims them
            // next.
            KeyCode::Up if store.posture.writes() => {
                self.selected = self.cursor(rows).saturating_sub(1)
            }
            KeyCode::Down if store.posture.writes() => {
                self.selected = (self.cursor(rows) + 1).min(rows.saturating_sub(1))
            }
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

        // One row for the header; whatever is left is the queue.
        let room = inner.height.saturating_sub(1) as usize;
        if rows.is_empty() {
            lines.push(Line::from(Span::styled(
                "no approval is waiting on a human",
                Style::default().fg(theme().text_dim),
            )));
        }
        // The window follows the cursor. The cursor is clamped to the whole
        // queue rather than to what fits, so on a pane shorter than the queue a
        // fixed top would leave the marker off screen and `a`/`R` acting on a
        // row nobody can see.
        let cursor = self.cursor(rows.len());
        let top = cursor.saturating_sub(room.saturating_sub(1));
        for (i, approval) in rows.iter().enumerate().skip(top).take(room) {
            // A cursor only means something where a key can act on it.
            let selected = store.posture.writes() && i == cursor;
            lines.push(approval_row(approval, selected, inner.width));
        }
        f.render_widget(Paragraph::new(lines), inner);
    }
}

/// One approval: the marker, its id, the plan it binds, its status, its expiry.
///
/// The expiry goes whole or not at all. Between the pane's refusal floor and its
/// design width the row would otherwise be clipped by the `Paragraph`, and the
/// column that loses its tail is the clock — leaving `19:1` where an operator
/// reads the deadline of the decision they are about to make. A dropped chip
/// beats a truncated digit, exactly as the BOOK ribbon's sub-line does it.
fn approval_row(approval: &Approval, selected: bool, width: u16) -> Line<'static> {
    let t = theme();
    let status = format::text(approval.status.as_ref()).unwrap_or(MISSING);
    let mut spans = vec![
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
    ];
    if width >= APPROVALS_W {
        spans.push(Span::styled(
            clock(approval.expires_at.as_ref()).unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_tertiary),
        ));
    }
    Line::from(spans)
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
        lines.push(event_row::row(event, fx, now, inner.width));
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

#[cfg(test)]
mod tests {
    use super::*;

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
}
