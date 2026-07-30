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

pub struct SseEvent {
    pub kind: String,
    pub payload: serde_json::Value,
    pub ts: Option<String>,
    pub id: Option<i64>,
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
