//! Application event bus: every input, tick, and network result flows through one channel.
use crate::model::{RegimePanel, Snapshot};

pub enum AppEvent {
    Key(crossterm::event::KeyEvent),
    Resize,
    Tick,
    Snapshot(Box<Snapshot>),
    RegimePanel(RegimePanel),
    Sse(SseEvent),
    Http(HttpResult),
    ConnUp(Channel),
    ConnDown(Channel),
    /// What the owner said about a write this client asked for.
    ///
    /// On the bus rather than handled where it is awaited: a write runs in its
    /// own task so a slow owner cannot freeze the frame loop, and the answer
    /// has to come back through the one drain point that owns the store, the
    /// toasts and the poller.
    #[cfg(feature = "operator")]
    Wrote(Wrote),
}

/// The answer to one write, in the three shapes an operator must be able to
/// tell apart.
///
/// `Refused` is not `Failed`. The execution gate declines with **HTTP 200** and
/// `executed: false`, so a client that folded the two together would either
/// report a governance refusal as a broken connection or — far worse — let it
/// pass as a booked fill. It carries the owner's own `blocked_by` and reasons
/// because those are the sentence that tells the operator what to do next.
#[cfg(feature = "operator")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Wrote {
    /// The owner booked the plan.
    Executed { plan_id: String },
    /// The desk answered, considered it, and said no.
    Refused {
        plan_id: String,
        blocked_by: String,
        reasons: Vec<String>,
    },
    /// A human decision reached the record. `decision` is the owner's own word
    /// for it — `approved` or `rejected`.
    Decided {
        approval_id: String,
        decision: &'static str,
    },
    /// The request itself failed: no owner, a timeout, a non-2xx. `said` is the
    /// owner's words verbatim when there were any.
    Failed { what: String, said: String },
}

/// One frame off `/api/stream`, already split into the parts the desk uses.
///
/// `ts` and `id` are the resume cursor, not decoration: the owner resumes a
/// subscription strictly after that exact pair. `id` is a `String` because the
/// owner's is — `event_id VARCHAR PRIMARY KEY` in `qlab/state/registry.py`, and
/// the quote producer writes a `uuid4`. Parsing it as a number would leave every
/// real id unparsed, the cursor never advancing, and every reconnect replaying
/// the primer backlog: a resumable outage turned into silent data loss.
#[derive(Debug, Clone)]
pub struct SseEvent {
    pub kind: String,
    pub payload: serde_json::Value,
    pub ts: Option<String>,
    pub id: Option<String>,
}

pub enum HttpResult {
    Malformed { url: String, error: String },
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Channel {
    Owner,
    Stream,
}

pub type Tx = tokio::sync::mpsc::UnboundedSender<AppEvent>;
pub type Rx = tokio::sync::mpsc::UnboundedReceiver<AppEvent>;
