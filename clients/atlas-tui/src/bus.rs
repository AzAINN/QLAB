//! Application event bus: every input, tick, and network result flows through one channel.
use crate::model::{LlmCatalog, RegimePanel, Snapshot, Template};

pub enum AppEvent {
    Key(crossterm::event::KeyEvent),
    /// A mouse event, captured because the nav rail and the chat scroll are
    /// clickable. Kept deliberately minimal downstream — wheel and click only.
    Mouse(crossterm::event::MouseEvent),
    Resize,
    Tick,
    Snapshot(Box<Snapshot>),
    RegimePanel(RegimePanel),
    /// The workflow templates the owner is registered to start.
    ///
    /// Its own event rather than a field folded into the snapshot, because it
    /// comes off its own endpoint on its own cadence — see `model::Templates`.
    Templates(Vec<Template>),
    /// What the owner's backends serve, from `/api/llm/backends`.
    ///
    /// Its own event for the same reason `Templates` is, and one more: it is
    /// the only payload this client fetches on an *action* rather than on a
    /// beat. The route probes daemons, so a cadence here would put a network
    /// round trip per backend behind every poll — the owner refuses to do that
    /// on `/api/tui` for exactly that reason, and a client that polled the
    /// prober would have moved the cost rather than avoided it.
    Backends(LlmCatalog),
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
    /// A question reached the desk manager. It authorises nothing — the owner
    /// records the message and answers only through the coordinator.
    ///
    /// `note` is the owner's own sentence about whether an answer is even
    /// possible ("coordinator unavailable; Atlas is degraded and cannot
    /// answer"). Dropping it would let a client report a question as delivered
    /// to something that cannot hear it.
    Asked { note: String },
    /// A governed workforce run was registered. `workflow_id` is the owner's,
    /// so the row that appears in the pipeline pane is the one this key started.
    Started {
        template: String,
        workflow_id: String,
    },
    /// The desk is now pointed somewhere else. `label` is the owner's own word
    /// for the pair (`DeskMode.label`), never one this client composed — the
    /// two halves an operator typed are not the sentence the owner made of them.
    ///
    /// `warning` carries the owner's credential description when it accepted a
    /// book it cannot reach. Pointing the desk at Alpaca with no usable login is
    /// a 200 that changed the desk and cannot trade it, which is exactly the
    /// "succeeded and did nothing" shape this client refuses to render as a
    /// receipt.
    Pointed {
        label: String,
        warning: Option<String>,
    },
    /// The owner stored an Alpaca paper login.
    ///
    /// `usable` is its own verdict on what it can now read (`credentials_ok`),
    /// not this client's: a login can be written and still be shadowed by an
    /// environment pair or unreadable at the resolver, which is a 200 that
    /// changed a file and cannot trade — the "succeeded and did nothing" shape
    /// this client refuses to draw as a receipt. `note` is the owner's own
    /// description of the credential, and never anything that was typed.
    LoggedIn { usable: bool, note: String },
    /// The owner will not overwrite what is already stored without being asked
    /// twice. `said` is its sentence, which names what would be lost — rendered
    /// verbatim, because this client owns none of that wording.
    LoginNeedsConsent { said: String },
    /// The owner would not store the login as sent. Its own sentence, which
    /// never quotes what was typed.
    LoginRefused { said: String },
    /// A surface is pointed at a model, or the reasoner was switched.
    ///
    /// `said` is the owner's own account of what that means (`effect`), never a
    /// receipt this client composed: the same 200 can mean "Atlas reasons with
    /// ollama qwen2.5:7b" or "Atlas answers you on it; enable the reasoner to
    /// let it choose templates too", and only the owner knows which.
    Chose { said: String },
    /// The owner would not point the surface there — an unreachable daemon, a
    /// model it does not serve, a switch on the surface that has none. Its own
    /// sentence, which carries the remedy.
    ChoiceRefused { said: String },
    /// The stored login was put to the venue. `ok` is the venue's answer and
    /// `summary` is what to show either way — the masked account and its buying
    /// power, or the reason it was refused.
    Tested { ok: bool, summary: String },
    /// The desk recorded an arming answer. `armed` is the owner's own account
    /// of what it now holds (`posture_payload`), never this client's echo of
    /// what it sent: the answer that decides whether the next keystroke can
    /// place an order is the one the owner persisted, and a receipt composed
    /// from the request would report an arming a failed write never made.
    Armed { armed: bool },
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
