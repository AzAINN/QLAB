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
