//! Application event bus: every input, tick, and network result flows through one channel.
use crate::model::{
    AtlasRights, DeskAuthority, LlmCatalog, MethodSettings, NewsSettings, PredictorDetail,
    Proposal, QualitativeMatrix, RegimePanel, Snapshot, Template, VisualAnswer, VisualEntry,
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
    /// Which method this desk solves with and how many names it may hold, from
    /// `/api/desk/method`.
    ///
    /// Fetched on the same terms as `News` and for the same reason: it changes
    /// when an operator changes it, so a cadence would spend a request per poll
    /// re-reading an answer this client already holds. Asked for when SETTINGS
    /// is entered, when `r` is pressed there, and after the card's own POST —
    /// the last one because the owner recomputes its cap warning on the way
    /// out, and a card left drawing the pre-change answer would be showing a
    /// mandate nobody is running.
    Method(Box<MethodSettings>),
    /// The three authorities the operator lends Atlas, from
    /// `/api/atlas/rights`.
    ///
    /// Fetched at startup — the file is read at the moment a chat session is
    /// launched, so the card must be able to say what is in force before the
    /// operator opens SETTINGS — and again after the card's own POST, because
    /// the owner writes the **full** three-key object and answers with what is
    /// now on disk.
    ///
    /// **Its 500 arrives here too**, as `AtlasRights::error` rather than as a
    /// dropped fetch: a rights file this desk did not write is refused by the
    /// owner's reader with the remedy in the sentence, and a client that showed
    /// three granted rights over it would be inventing a desk state nobody set.
    Rights(Box<AtlasRights>),
    /// The standing grant a fill may happen under, from
    /// `GET /api/desk/authority`.
    ///
    /// It rides the **snapshot's own beat** rather than a pane entry, unlike
    /// the rights beside it, and for `Proposal`'s reason: what is *left* of a
    /// grant — the books it has spent today, the days it has left, and the
    /// anomalies suspending it — all move on the owner's own heartbeat with
    /// nothing here to prompt a refetch. A card fetched once on entering
    /// SETTINGS would go on offering to revoke a grant that had already
    /// expired, and would show a full day's budget over a desk that had spent
    /// it.
    Authority(Box<DeskAuthority>),
    /// The qualitative matrix, from `/api/research/qualitative`.
    ///
    /// The one of these four that rides a beat, and deliberately: the window
    /// underneath it is refreshed by the *owner's* heartbeat rather than by
    /// anything an operator does here, so a payload fetched once on entering
    /// RESEARCH would sit unchanged on screen while the record moved. Boxed
    /// like the board: one row per universe name, each carrying its claim keys.
    Qualitative(Box<QualitativeMatrix>),
    /// The desk's single current proposal, from `GET /api/desk/proposal`.
    ///
    /// `None` is the owner's own `{"proposal": null}` — a desk with no open
    /// question — and never "not fetched": the poller only sends this once the
    /// route answered and decoded.
    ///
    /// It rides the **snapshot's own beat** rather than a pane entry, unlike
    /// the board and the news settings. Two reasons, and both are about where
    /// the card is: it is mirrored on ATLAS, which is the view this client
    /// opens on, so there is no entry to hang a first fetch on; and what makes
    /// a proposal stop being the proposal — a newer plan superseding it, an
    /// expiry, an orphan withdrawn — happens on the *owner's* heartbeat, with
    /// nothing this client does to prompt a refetch. A card fetched once on
    /// entry would go on offering to book a question the desk had already
    /// withdrawn. Boxed because the payload carries the whole target vector.
    Proposal(Option<Box<Proposal>>),
    /// What the owner can draw, from `/api/visuals`.
    ///
    /// Fetched when the VISUALS view is entered and on `r` there, on
    /// `PredictorDetail`'s terms and for a stronger version of its reason: the
    /// registry is a walk over the owner's own package, so it changes when the
    /// owner is deployed and a cadence would re-read one answer forever.
    Visuals(Vec<VisualEntry>),
    /// One rendered visual, from `/api/visuals/<name>`.
    ///
    /// Asked for by a keystroke and never by a beat: rendering is work the
    /// owner does per request, and the operator chose which drawing they
    /// wanted. It carries a refusal as an answer rather than as a failure —
    /// a 404 for an unknown name and a 400 for params the drawer would not
    /// take are both the owner having *considered* the request, and folding
    /// either into "the owner did not answer" would send the operator to
    /// restart a process that is working exactly as designed.
    Visual(Box<VisualAnswer>),
    Sse(SseEvent),
    Http(HttpResult),
    ConnUp(Channel),
    ConnDown(Channel),
    /// What the child in the ATLAS pane wrote, or the fact that it is over.
    ///
    /// On the bus rather than read where it is produced, for `Wrote`'s reason
    /// and one more. A pty is drained by a blocking thread — this crate's tokio
    /// is built without `io-util`, and a pane is not worth a second process API
    /// — so the bytes have to cross into the async loop somewhere, and the one
    /// drain point that owns the store is where every other producer already
    /// crosses. It is also what keeps a frame a pure function of
    /// `(store, fx, instant)`: the `vt100::Parser` lives in the store and
    /// advances here, on an event, never inside a `draw`.
    ///
    /// **It says which pane said it, because the bus outlives the pane.**
    /// `close_pty` signals the child and returns — it does not join the reader
    /// thread — so a closed session's last bytes and its ending are still in
    /// flight while the next `/cli` is opening. Anonymous, they land on
    /// whatever pane is open when they arrive: the desk reports a live child as
    /// ended, and the store *drops that live session to say it*, killing the
    /// Claude the sentence was never about. The staleness lives in this queue,
    /// so the identity has to travel on the event.
    ///
    /// **Gated with the module that produces it.** `PtyEvent` lives in `pty`,
    /// which the monitoring build does not compile at all: it has no command
    /// that opens a pane, so it has no child to hear from.
    #[cfg(feature = "operator")]
    Pty {
        /// The pane this came from, stamped by the forwarder that opened it.
        /// `store::NO_PANE` for a refusal, which belongs to no pane at all.
        pane: u64,
        event: crate::pty::PtyEvent,
    },
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
    /// The desk booked its current proposal in one confirmed call.
    ///
    /// `summary` is composed from the owner's own execution body — what it
    /// reported, never a receipt written from the request. Separate from
    /// `Executed` because the two are different routes with different
    /// afterwards: `POST /api/desk/proposal/book` approves and executes inside
    /// one lock, and its refusals do not all mean the same thing (see
    /// `BookRefused`).
    Booked { plan_id: String, summary: String },
    /// The desk answered the one-click book with **200 and `booked: false`**.
    ///
    /// **Not a failure, and not one fact.** F2's corrected contract has three
    /// shapes behind this status, and they part company on what happens to the
    /// approval:
    ///
    /// * `blocked_by == "approval"` — the gate invalidated it. The authority is
    ///   gone and the plan has to be re-proposed.
    /// * `blocked_by == "data_revalidation"`, or a `mandate_violation` — the
    ///   refusal landed *before* the approval was touched, so the proposal
    ///   stands and the same click is valid again once the reason clears.
    ///
    /// `survives` carries that, and it is an `Option` because a blocker the
    /// owner has not named is a third answer rather than a guess between the
    /// two: a client that read every refusal as "re-propose" throws away a live
    /// approval in two cases out of three, and one that read them all as
    /// "retry" sends the operator back at a question that no longer exists.
    BookRefused {
        plan_id: String,
        blocked_by: String,
        reasons: Vec<String>,
        survives: Option<bool>,
    },
    /// The one-click book never got an answer: no owner, a timeout, a non-2xx.
    ///
    /// **Its own variant rather than a `Failed` the card matches by name**, for
    /// the reason `RightFailed` and `PredictorFailed` give, and here with the
    /// most at stake of the three: the fill's state is *unknown*, and the card
    /// an operator is about to press the key on again is the surface that says
    /// so. `plan_id` is what makes the match structural — the card compares it
    /// with the proposal on screen, where the old shape recovered the plan by
    /// slicing `dispatch::names`' prose, so any rewording of that sentence
    /// silently retargeted the note.
    BookFailed { plan_id: String, said: String },
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
    /// The request to change the news stack never got an answer. Its own
    /// variant for `MethodFailed`'s reason: the NEWS card retires its wait on
    /// this outcome and no other.
    NewsFailed { said: String },
    /// The owner changed which method this desk solves with, or its cap.
    ///
    /// **The owner's own account of what is now in force**, read off the answer
    /// and never echoed from the request: the route merges the override into
    /// the shipped mandate and answers with the merged pair, so a receipt
    /// composed here would report a cap the mandate had clamped as the one the
    /// desk is holding to.
    ///
    /// `warning` rides with it because it is recomputed by the same answer and
    /// is the whole reason this change is a warn rather than a refusal: a cap
    /// below what the effective method holds applies, and the plan it will
    /// refuse is minutes away.
    MethodSet {
        policy: String,
        cap: Option<i64>,
        warning: Option<String>,
    },
    /// The owner would not solve that way — a method it does not know, one that
    /// is still research stage, a cap outside the universe. Its own sentence,
    /// which carries the remedy.
    MethodRefused { said: String },
    /// The request to change the method never got an answer.
    ///
    /// Its own variant for `RightFailed`'s reason: the METHOD card retires its
    /// wait on this outcome and no other, and a wait retired by some unrelated
    /// broken request would re-arm the cap box over a change still in flight —
    /// one decision, two override rows.
    MethodFailed { said: String },
    /// The owner recorded a right.
    ///
    /// **The owner's own answer, never the request's echo**, for the reason
    /// `MethodSet` carries the merged pair: the route writes all three keys and
    /// answers with the object that is now on disk, so a receipt composed here
    /// would report a grant a partial write never made.
    ///
    /// `field` rides along so the card can retire the wait it started, and so
    /// this outcome cannot be confused with the two beside it.
    RightSet {
        field: &'static str,
        rights: crate::model::RightsFlags,
    },
    /// The owner would not record it — a key it has no right by, a value that
    /// is not a bool, or the chat asking for its own authority back. Its own
    /// sentence, which names the rights this desk does have.
    RightRefused { field: &'static str, said: String },
    /// The request to record a right never got an answer.
    ///
    /// **Its own variant rather than a `Failed` the card matches by name**, for
    /// the reason `PredictorFailed` states: the card retires its `sending` wait
    /// on this outcome, and a generic failure would retire it over some other
    /// key's broken request — re-arming Space over a toggle still in flight.
    /// `field` is what makes the match structural rather than a comparison of
    /// `dispatch::names`' prose.
    RightFailed { field: &'static str, said: String },
    /// The owner revoked the standing grant.
    ///
    /// **The owner's own account of what it revoked**, never the request's
    /// echo: this client sends a reason and no grant id — there is one live
    /// grant and the owner is what knows which — so the id comes back off the
    /// answer or not at all.
    ///
    /// Revocation is the one write on this workstation that *narrows* what can
    /// happen next, which is why it is a plain `Info` and why no confirmation
    /// stands between the key and it.
    AuthorityRevoked { grant_id: Option<String> },
    /// The owner would not revoke it — no grant to revoke, or the chat asking.
    /// Its own sentence, which carries the remedy.
    AuthorityRefused { said: String },
    /// The request to revoke never got an answer.
    ///
    /// **Its own variant rather than a `Failed` the card matches by name**, for
    /// `RightFailed`'s reason: the AUTHORITY card retires its wait on this
    /// outcome and no other, and a generic failure would re-arm `R` over a
    /// revocation still in flight.
    AuthorityFailed { said: String },
    /// The owner fitted a predictor board.
    ///
    /// **The owner's own answer, never the request's echo.** `models` is what
    /// it actually ran — the chosen lane *plus* `ridge:none`, which the route
    /// appends whether or not it was asked for, because a challenger without
    /// its control is not evidence — so a receipt composed here would report
    /// one lane where two were fitted.
    ///
    /// `champion` is an `Option` because the owner's is: `null` means nothing
    /// cleared admission, which is a **result** and not a missing value. A
    /// client that rendered it as the chosen lane, or as absent, would report
    /// a refuted board as a successful one.
    ///
    /// `run_id` and `models` are not optional, and `models` is never empty:
    /// `net::write::Board::read` refuses a 200 that lacks either, because a
    /// board with no run and no lanes is a broken contract and would otherwise
    /// be drawn in the tone reserved for a finding.
    PredictorRan {
        run_id: String,
        models: Vec<String>,
        champion: Option<String>,
    },
    /// The owner would not run that lane. Its own sentence — which, for the
    /// refusal an operator will actually hit, names every lane it does serve.
    PredictorRefused { said: String },
    /// The request to fit a lane never got an answer.
    ///
    /// **Its own variant rather than a `Failed` the pane matches by name**, and
    /// this is the one place on the bus where that distinction is load-bearing.
    /// Every other write answers in milliseconds; a board is fitted for up to a
    /// minute, so *some other* write will fail while one is in flight — and a
    /// pane that read any `Failed` as its own would retire its in-flight line,
    /// paint a news save's timeout as a board's, and re-arm the key that starts
    /// a second run over the first. `lane` is what makes the match structural:
    /// the pane compares it with what it is waiting on, and no change to
    /// `dispatch::names`' prose can silently break that.
    PredictorFailed { lane: String, said: String },
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
