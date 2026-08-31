//! Application event bus: every input, tick, and network result flows through one channel.
use crate::model::{
    LlmCatalog, NewsSettings, PredictorDetail, QualitativeMatrix, RegimePanel, Snapshot, Template,
};

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
    /// The full predictor board, from `/api/research/predictors`.
    ///
    /// Fetched when the operator opens the PREDICTORS view rather than on a
    /// beat: the board changes when a run lands — days apart — and the
    /// snapshot's own `predictors` summary already carries the headline on the
    /// desk cadence. Boxed like the snapshot: the payload carries every
    /// model's per-fold series, and the bus should not grow to its widest
    /// passenger.
    PredictorDetail(Box<PredictorDetail>),
    /// What the desk reads the news from, from `/api/news/settings`.
    ///
    /// Fetched when SETTINGS is entered rather than on a beat, for
    /// `PredictorDetail`'s reason: it changes when an operator changes it, and
    /// a cadence would be re-fetching an answer this client already holds.
    News(Box<NewsSettings>),
    /// The qualitative matrix, from `/api/research/qualitative`.
    ///
    /// The one of these four that rides a beat, and deliberately: the window
    /// underneath it is refreshed by the *owner's* heartbeat rather than by
    /// anything an operator does here, so a payload fetched once on entering
    /// RESEARCH would sit unchanged on screen while the record moved. Boxed
    /// like the board: one row per universe name, each carrying its claim keys.
    Qualitative(Box<QualitativeMatrix>),
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
    /// An approval request now exists for the plan. Nothing is decided: the
    /// ATLAS view opens the approve box on hearing this, so `/approve <plan>`
    /// is one command and one confirm.
    ApprovalOpened {
        approval_id: String,
        plan_id: String,
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
    /// The desk was asked what it would do, and answered.
    ///
    /// Counts rather than the list: the items land in the next snapshot's
    /// `actionables` block, which is the surface the WOULD DO panel already
    /// draws from. Both halves, because "nothing to do" is an answer — an
    /// outcome carrying only the offers could not tell a quiet desk from one
    /// that refused everything, and the second is a mode the operator can fix.
    Proposed { offered: usize, refused: usize },
    /// The desk started work the operator approved.
    ///
    /// `template` and `workflow_id` are the owner's own, and both are optional
    /// because both are: a deterministic template completes inside the request
    /// and registers no workflow at all. What is never optional is the task —
    /// that is what was approved, and it is the handle an audit reads back.
    ProposalStarted {
        task_id: String,
        template: Option<String>,
        workflow_id: Option<String>,
    },
    /// The owner would not start it, and this is what stopped it.
    ///
    /// **Not `Failed`, and not a start.** The gate declines with HTTP 200 and
    /// `started: false` — an authority refusal, an exhausted retry budget —
    /// which is the same shape that once let an execution refusal read as a
    /// booked fill. It carries the owner's own `blocked_by` and sentence,
    /// because those are what tell an operator what to do next.
    ProposalRefused {
        task_id: String,
        blocked_by: String,
        reason: String,
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
    /// The owner applied a news stack.
    ///
    /// `stack` is what it resolves *after* the change — its own answer, never
    /// the list that was sent, because an offline desk resolves `synthetic`
    /// whatever was chosen and a receipt echoing the request would hide that.
    ///
    /// `checked` is whether a verify was asked for at all, and `verified` is
    /// what came back — per member, never the any-member flag beside it. The
    /// two are separate because a save that asked and got nothing back is a
    /// broken contract rather than a clean check, and one flag cannot say both.
    NewsSaved {
        stack: Vec<String>,
        checked: bool,
        verified: Vec<NewsMember>,
    },
    /// The owner would not read the news that way — an unknown name, a source
    /// it cannot reach, a missing or malformed contact. Its own sentence,
    /// which carries the remedy and never the contact.
    NewsRefused { said: String },
    /// The request itself failed: no owner, a timeout, a non-2xx. `said` is the
    /// owner's words verbatim when there were any.
    Failed { what: String, said: String },
}

/// One member of a news verify, as the owner reports it.
///
/// **`ok` and `quality_flags` are separate claims, and the owner makes both.**
/// A source can answer *and* be degraded — `partial: <feed>: <err>` — which is
/// why the top-level `verify.ok` is any-member and may not be drawn as
/// whole-stack health. Nothing in this client reads that flag: every surface
/// that shows a verify shows it per member, which is the only shape that can
/// say "three answered, one of them half".
///
/// Ungated, unlike the outcome that carries it: the NEWS card renders these
/// beside its rows in both builds, and the glass one simply never has any.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct NewsMember {
    pub name: String,
    pub ok: bool,
    /// The owner's own sentence about a member that did not answer. Empty when
    /// it did.
    pub detail: String,
    pub quality_flags: Vec<String>,
}

impl NewsMember {
    /// What to show beside this member, and whether it is a problem.
    ///
    /// Three answers rather than two, because there are three states: a member
    /// that did not answer, one that answered whole, and one that answered
    /// with a feed missing. The third is not a failure and is not health, and
    /// folding it either way is exactly what the any-member `ok` flag would do.
    pub fn said(&self) -> (String, bool) {
        if !self.ok {
            return (
                match self.detail.is_empty() {
                    true => "did not answer".to_string(),
                    false => self.detail.clone(),
                },
                true,
            );
        }
        match self
            .quality_flags
            .iter()
            .find(|flag| flag.starts_with("partial:"))
        {
            Some(flag) => (flag.clone(), false),
            None => ("ok".to_string(), false),
        }
    }
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
