//! The write half: the only code in this crate that can change the desk.
//!
//! It exists only under the `operator` feature. CLAUDE.md's claim about this
//! client — "read-only by construction … so invariant 3 holds there by absence"
//! — stays literally true of the default artifact, because this file is not in
//! it. A runtime `Option<WriteClient>` would have demoted that to a branch.
//!
//! Three rules shape everything below.
//!
//! **Nothing here decides anything.** Every method is a transcription of one
//! owner route. The governance lives in `qlab/ui/server.py` and the registry
//! behind it — the referee's PASS bound to a `targets_hash`, the persisted
//! approval a fill must consume, the mandate check — and this client's job is to
//! carry a human's decision to it and report back what it said. There is no
//! order construction here, no plan building, no retry that could book twice.
//!
//! **The owner's refusals are the message, and a refusal is not an error.** A
//! non-2xx is an error carrying the owner's own words, never a swallowed failure
//! — "approval is 'expired', not pending" is the sentence that tells an operator
//! what to do next, and reducing it to "request failed" leaves them pressing the
//! same key.
//!
//! But the *execution gate does not refuse with a status code*. It answers
//! **HTTP 200** with `{"executed": false, …}`: an expired approval, a book that
//! moved, a failed data revalidation, and a mandate violation are all 200s
//! (`server.py:1879`, `:1888`, `:1909`, returned by the handler at `:2629`).
//! That is correct of the owner — the request was well-formed and the desk
//! answered it — and it means a client that keys success off the status code
//! reports every governance refusal as a booked fill. So `execute_plan` returns
//! three outcomes, not two, and the caller must handle all three.
//!
//! **`execute_plan` cannot be called without going through the modal.** It takes
//! a `ConfirmToken`, which only `ui::widgets::confirm` can mint and only after a
//! human has typed six characters of the plan's own `targets_hash`. The token
//! carries the plan and approval ids it was minted for, so there is no argument
//! a caller can vary: a confirmation for one plan cannot execute another.
//!
//! Routes verified against `qlab/ui/server.py` at the dispatch table, not from
//! documentation. Two differ from the plan's Part IV sketch and the difference
//! matters — see `execute_plan` and `approve`.

use crate::secret::Secret;
use crate::ui::widgets::confirm::{BookToken, ConfirmToken};
use serde_json::{json, Value};

/// What the owner said when it would not do the thing.
///
/// The status and the body, both. A refusal from the execution gate is the most
/// important sentence this client ever relays, and it is only ever in the body.
#[derive(Debug)]
pub enum WriteError {
    /// The owner answered, and said no.
    Refused { status: u16, said: String },
    /// The request never got an answer: no owner, a timeout, a broken socket.
    Unreachable(String),
    /// The owner answered 2xx with something that is not JSON. Loud rather than
    /// ignored: the call may well have taken effect, and a client that shrugged
    /// would leave the operator unsure whether to press the key again.
    Unreadable(String),
    /// No HTTP client could be built, so this writer was never usable.
    NoClient(String),
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WriteError::Refused { status, said } => {
                write!(f, "the owner refused with {status}: {said}")
            }
            WriteError::Unreachable(why) => write!(f, "the owner did not answer: {why}"),
            WriteError::Unreadable(why) => write!(f, "the owner answered unreadably: {why}"),
            WriteError::NoClient(why) => write!(f, "no HTTP client for the owner: {why}"),
        }
    }
}

impl std::error::Error for WriteError {}

impl WriteError {
    /// The foreign text this error carries, whichever shape it is.
    ///
    /// Every variant holds exactly one string that came from outside this
    /// process — a refusal body, a transport message, a decode error — and a
    /// caller that has to sanitise one needs to reach it without matching on
    /// four arms and forgetting the fourth. That forgetting is precisely how
    /// the credential path leaked at every status but 400.
    fn said(&self) -> &str {
        match self {
            WriteError::Refused { said, .. } => said,
            WriteError::Unreachable(why)
            | WriteError::Unreadable(why)
            | WriteError::NoClient(why) => why,
        }
    }

    /// The same error with its foreign text replaced.
    ///
    /// The *inner* string, not the `Display` line: rebuilding `Unreachable`
    /// from its own rendering would nest the prefix on every pass.
    fn with_said(self, said: String) -> WriteError {
        match self {
            WriteError::Refused { status, .. } => WriteError::Refused { status, said },
            WriteError::Unreachable(_) => WriteError::Unreachable(said),
            WriteError::Unreadable(_) => WriteError::Unreadable(said),
            WriteError::NoClient(_) => WriteError::NoClient(said),
        }
    }
}

type Wrote = Result<Value, WriteError>;

/// What became of a confirmed plan. Three outcomes, and the caller must face all
/// three — which is the whole reason this is a type and not a `bool`.
///
/// `Refused` is neither an error nor a success. The desk answered, the answer is
/// legitimate and considered, and the answer is no. Folding it into `Err` would
/// lump a governance decision in with a broken socket; folding it into `Ok`
/// — which is what this client did before — tells an operator a trade was booked
/// when the gate declined it.
#[derive(Debug)]
pub enum Execution {
    /// The owner booked it. Carries the body: fills, ids, whatever it reported.
    Executed(Value),
    /// The owner declined to book it, and said why.
    Refused {
        /// `approval`, `data_revalidation`, or `mandate_violation`.
        blocked_by: String,
        /// Never empty — a refusal an operator cannot read is not actionable.
        reasons: Vec<String>,
    },
}

impl Execution {
    /// Read one 200 body from `/api/plans/execute`.
    ///
    /// The owner has four shapes here and they are not uniform: three refusals
    /// carry `blocked_by`, but the mandate violation (`server.py:1909`) carries
    /// only `mandate_violation`, and its reason lives in that key rather than in
    /// `reasons`. Keying on `blocked_by` alone would fall through to "executed"
    /// on precisely the refusal that means the plan broke the mandate.
    fn read(body: Value) -> Result<Execution, WriteError> {
        match body.get("executed").and_then(Value::as_bool) {
            Some(true) => Ok(Execution::Executed(body)),
            Some(false) => {
                if let Some(violation) = body.get("mandate_violation").and_then(Value::as_str) {
                    return Ok(Execution::Refused {
                        blocked_by: "mandate_violation".into(),
                        reasons: vec![violation.to_string()],
                    });
                }
                let blocked_by = body
                    .get("blocked_by")
                    .and_then(Value::as_str)
                    .unwrap_or("unstated")
                    .to_string();
                let mut reasons: Vec<String> = body
                    .get("reasons")
                    .and_then(Value::as_array)
                    .map(|rs| {
                        rs.iter()
                            .filter_map(Value::as_str)
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default();
                if reasons.is_empty() {
                    // `data_revalidation` refuses with a `data_health` object and
                    // no `reasons` list. An operator handed "refused" and nothing
                    // else cannot act, so the gate's own word for it is the
                    // reason of last resort — never an empty vec.
                    reasons.push(match body.get("data_health") {
                        Some(health) => format!("the desk blocked this fill: {health}"),
                        None => format!("the desk blocked this fill ({blocked_by})"),
                    });
                }
                Ok(Execution::Refused {
                    blocked_by,
                    reasons,
                })
            }
            // Fail loud. The owner always sets `executed` on this route, so a
            // body without it is a broken contract — and both guesses are
            // indefensible: one invents a fill, the other hides one.
            None => Err(WriteError::Unreadable(format!(
                "the owner answered 200 for an execution without saying whether it executed: {body}"
            ))),
        }
    }
}

/// What became of the desk's current proposal, booked in one confirmed call.
///
/// **Four outcomes, and three of them are 200s.** `POST /api/desk/proposal/book`
/// answers 200 with `booked: false` when the gate declined after the approval
/// was granted, and F2's corrected contract says those refusals are not one
/// fact: the approval is *gone* for a `blocked_by == "approval"` — the gate
/// invalidated it — and *still standing* for a `data_revalidation` or a
/// `mandate_violation`, which are refused before it is touched.
///
/// So the split here is on what the operator may do next, which is the only
/// thing the card can act on. Folding all three into one variant is what F2's
/// own fix round exists to prevent: a client reading every refusal as
/// "re-propose" discards a live approval in two cases out of three.
#[derive(Debug)]
pub enum Booked {
    /// The desk booked it. Carries the whole body — the execution, its
    /// approval id, whatever the owner reported.
    Filled(Value),
    /// The gate declined and consumed the question with it. Re-propose.
    Invalidated {
        blocked_by: String,
        /// Never empty — a refusal an operator cannot read is not actionable.
        reasons: Vec<String>,
    },
    /// The gate declined without spending the approval. The same proposal is
    /// still bookable once the reason clears.
    Standing {
        blocked_by: String,
        reasons: Vec<String>,
    },
    /// The owner said it did not book and did not say what stopped it.
    ///
    /// Its own variant rather than a default into either neighbour, and this is
    /// the deliberate part: both defaults are wrong in a way that costs
    /// something. "Re-propose" throws away an approval that may still be live;
    /// "retry" sends a human back at a question that no longer exists. Invariant
    /// 4 — say that the answer could not be read, and let the next poll settle
    /// it.
    Unstated {
        blocked_by: String,
        reasons: Vec<String>,
    },
}

impl Booked {
    /// Read one 200 body from `/api/desk/proposal/book`.
    ///
    /// Keyed on `booked`, never on the status code: the route's own refusals
    /// (`not the current proposal`, a hash mismatch, no covering PASS) are
    /// 400s and reach the caller as [`WriteError::Refused`], while a gate
    /// refusal *after* the approval was granted is this 200.
    fn read(body: Value) -> Result<Booked, WriteError> {
        match body.get("booked").and_then(Value::as_bool) {
            Some(true) => Ok(Booked::Filled(body)),
            Some(false) => {
                // The execution the owner composed the answer from. Absent is
                // not empty: a `booked: false` with no execution block has said
                // nothing about which of the three shapes it is.
                let execution = body.get("execution").unwrap_or(&Value::Null).clone();
                // The mandate violation carries its reason in its own key
                // rather than in `reasons` (`server.py:1909`), exactly as the
                // two-call execute path does — and it is one of the two shapes
                // that leave the approval alive, so keying on `blocked_by`
                // alone would call it a re-propose.
                if let Some(violation) = execution
                    .get("mandate_violation")
                    .and_then(Value::as_str)
                    .filter(|said| !said.is_empty())
                {
                    return Ok(Booked::Standing {
                        blocked_by: "mandate_violation".into(),
                        reasons: vec![violation.to_string()],
                    });
                }
                let blocked_by = execution
                    .get("blocked_by")
                    .and_then(Value::as_str)
                    .filter(|said| !said.is_empty())
                    .unwrap_or("unstated")
                    .to_string();
                let mut reasons: Vec<String> = execution
                    .get("reasons")
                    .and_then(Value::as_array)
                    .map(|rs| {
                        rs.iter()
                            .filter_map(Value::as_str)
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default();
                if reasons.is_empty() {
                    // `data_revalidation` refuses with a `data_health` object
                    // and no `reasons` list, exactly as on the execute route.
                    reasons.push(match execution.get("data_health") {
                        Some(health) => format!("the desk blocked this fill: {health}"),
                        None => format!("the desk blocked this fill ({blocked_by})"),
                    });
                }
                Ok(match blocked_by.as_str() {
                    "approval" => Booked::Invalidated {
                        blocked_by,
                        reasons,
                    },
                    "data_revalidation" => Booked::Standing {
                        blocked_by,
                        reasons,
                    },
                    _ => Booked::Unstated {
                        blocked_by,
                        reasons,
                    },
                })
            }
            // Fail loud, for `Execution::read`'s reason: the owner always sets
            // `booked` on this route, and both guesses are indefensible — one
            // invents a fill, the other hides one.
            None => Err(WriteError::Unreadable(format!(
                "the owner answered 200 for a booking without saying whether it booked: {body}"
            ))),
        }
    }
}

/// What became of an approved proposal. Three outcomes, and only one of them
/// is work that started.
///
/// **The start gate does not refuse with a status code either.** `start_task`
/// answers **HTTP 200** with `{"started": false, "blocked_by": …}` for an
/// authority refusal (`atlas.py:441`) and for an exhausted retry budget
/// (`:425`); a task that is no longer queued or failed is the one refusal that
/// does carry a status — `AtlasSupervisor.start_task` raises `PermissionError`
/// and the owner's dispatcher answers 400. So a client keying success off
/// the status code reports the mode gate's own "no" as work it started — the
/// same trap `Execution` exists for, on a second route.
///
/// `Failed` is the third: the owner answers `{"started": true, "completed":
/// false, "error": …}` when the runner raised inside the request. The task did
/// start and it is already over, so reporting it as a start would leave an
/// operator watching a pipeline that will never move.
#[derive(Debug)]
pub enum Start {
    /// The owner started it. `template_id` is its own word for what it started
    /// — never the one this client sent — and `workflow_id` is present only
    /// when durable work was registered, which is not the same as running.
    Started {
        template: Option<String>,
        workflow_id: Option<String>,
    },
    /// The owner declined to start it, and said what stopped it.
    Refused {
        /// `authority` or `retry_budget`, or `unstated` if it did not say.
        blocked_by: String,
        /// Never empty — a refusal an operator cannot read is not actionable.
        reason: String,
    },
    /// It started and immediately failed, inside the same 200.
    Failed(String),
}

impl Start {
    /// Read one 200 body from `/api/atlas/tasks/<id>/start`.
    fn read(body: Value) -> Result<Start, WriteError> {
        match body.get("started").and_then(Value::as_bool) {
            Some(true) => Ok(match field(&body, "error") {
                Some(said) => Start::Failed(crate::format::bounded(&said, SAID_MAX)),
                None => Start::Started {
                    template: field(&body, "template_id"),
                    workflow_id: field(&body, "workflow_id"),
                },
            }),
            Some(false) => {
                let blocked_by = field(&body, "blocked_by").unwrap_or_else(|| "unstated".into());
                Ok(Start::Refused {
                    // The retry-budget refusal carries no sentence at all, so
                    // the gate's own word for it is the reason of last resort:
                    // "refused" with nothing after it is not something an
                    // operator can act on.
                    reason: field(&body, "reason")
                        .map(|said| crate::format::bounded(&said, SAID_MAX))
                        .unwrap_or_else(|| format!("the desk would not start it ({blocked_by})")),
                    blocked_by,
                })
            }
            // Fail loud. The owner sets `started` on every answer this route
            // gives, so a body without it is a broken contract — and both
            // guesses are indefensible: one reports work nobody started, the
            // other hides work that is now running.
            None => Err(WriteError::Unreadable(format!(
                "the owner answered 200 for a task start without saying whether it started: {body}"
            ))),
        }
    }
}

/// What the desk said when it was asked what it would do.
///
/// **Counts, not the list.** The items themselves arrive on the next poll, in
/// `/api/tui`'s own `actionables` block, which is the surface the panel already
/// draws from — a second copy carried back through the write path would be this
/// client holding two accounts of one answer, and they would disagree the first
/// time an ask and a poll crossed.
///
/// Both halves, because the ask is not a request for work: a desk that would do
/// nothing has answered the question, and an outcome carrying only what was
/// offered could not tell that apart from an owner that refused everything.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Proposed {
    pub offered: usize,
    pub refused: usize,
}

impl Proposed {
    /// Read one 200 body from `/api/atlas/actionables`.
    ///
    /// The POST is where the gate speaks, so every item it serves carries a
    /// real verdict — `startable` is a boolean here, never the `null` the
    /// snapshot uses for "not ruled on". A body without an items list, or an
    /// item without a verdict, is a broken contract and says so: counting an
    /// unruled item as either would put a number on screen the desk did not
    /// say.
    fn read(body: Value) -> Result<Proposed, WriteError> {
        let Some(items) = body.get("items").and_then(Value::as_array) else {
            return Err(WriteError::Unreadable(format!(
                "the owner answered 200 for an ask without a list of actionables: {body}"
            )));
        };
        let mut proposed = Proposed {
            offered: 0,
            refused: 0,
        };
        for item in items {
            match item.get("startable").and_then(Value::as_bool) {
                Some(true) => proposed.offered += 1,
                Some(false) => proposed.refused += 1,
                None => {
                    return Err(WriteError::Unreadable(format!(
                        "the owner offered an actionable without saying whether it may \
                         start: {item}"
                    )))
                }
            }
        }
        Ok(proposed)
    }
}

/// What became of a login the operator typed. Three outcomes, and the middle
/// one is the whole reason this is a type.
///
/// The owner refuses with **400 twice**, for two things a client must do
/// differently: a stored login it would destroy (confirmable — ask, then re-POST
/// with `replace: true`), and a request that is simply wrong (not confirmable —
/// render the sentence and let the operator fix it). C1's contract puts a
/// `confirm` field on the first and deliberately leaves it off the second,
/// because the second's own sentence is "replace must be true or false" — a
/// client that sniffed the wording would offer to discard an operator's browser
/// login over a typo in a boolean.
#[derive(Debug)]
pub enum Login {
    /// Stored. Carries `desk_mode_payload()`, whose `credentials_ok` is how the
    /// caller learns whether the desk can now read what it just wrote.
    Stored(Value),
    /// A login already on disk would be lost. The owner's own sentence, which
    /// names what would go — never paraphrased here, and never acted on
    /// without the operator's word.
    ConsentNeeded(String),
    /// The request cannot be stored as sent. The owner's own sentence, which
    /// never quotes what was typed.
    Rejected(String),
}

/// What the owner did with a model choice. Two outcomes, and the second is not
/// an error.
///
/// The route refuses with **400 and a sentence** — an unknown backend, a model
/// it does not serve, a daemon that is not running, a switch on the surface
/// that has none — and every one of those is a considered answer about a
/// well-formed request, written for a human to read and act on. Folding them
/// into `Err` would put "the desk cannot reach ollama right now" in the same
/// shape as a broken socket, and the remedy is in the sentence.
///
/// Any other status is not a considered answer, so it stays an error: a 500 is
/// a broken owner and a 502 is something in front of it, and neither is the
/// desk saying no.
#[derive(Debug)]
pub enum Choice {
    /// The owner moved the surface, and this is its own sentence about what
    /// that means (`effect`) — never a receipt composed here out of the two
    /// words that were sent.
    Chosen(String),
    /// The owner would not point the surface there. Its own sentence, which for
    /// an unreachable backend is the catalog's own reason.
    Rejected(String),
}

/// What the owner did with a news stack. Two outcomes, and the second is not
/// an error — the same split [`Choice`] makes, for the same reason.
///
/// The route refuses with **400 and a sentence**: an unknown source name, a
/// source whose credential does not resolve, a missing or malformed EDGAR
/// contact. Every one of those is a considered answer about a well-formed
/// request, written for a human, and the remedy is inside it. Folding them
/// into `Err` would put "edgar needs a contact, as Your Name
/// <you@example.org>" in the same shape as a broken socket.
#[derive(Debug)]
pub enum News {
    /// Applied. Carries the stack the owner *resolves* afterwards, which is
    /// not always the list that was sent — an offline desk reads `synthetic`
    /// whatever is configured — plus the verify, per member, when one was
    /// asked for.
    ///
    /// **Per member, and never the `verify.ok` beside them.** That flag is
    /// any-member: it is true when one source answered, and a client that read
    /// it as whole-stack health would report a dead feed as a clean check.
    Applied {
        stack: Vec<String>,
        verified: Vec<crate::bus::NewsMember>,
    },
    /// The owner would not read the news that way. Its own sentence.
    Rejected(String),
}

/// What the owner did with a method or a cap. Two outcomes, and the second is
/// not an error — the same split [`News`] and [`Choice`] make, for the same
/// reason.
///
/// **Named for the mandate rather than for the method**, and not by taste:
/// `tests/operator_gate.rs` censuses `Method::` across `src` as an HTTP write
/// marker (`reqwest`'s own `Method::POST`), and every arm of an enum called
/// `Method` would read as a POST call site in whatever file matched on it.
/// Widening the census to tell the two apart would make it stop watching the
/// door it exists to watch.
///
/// The route refuses with **400 and a sentence**: a method it does not know, a
/// method that is still research stage ("promotion out of research takes
/// evidence and a catalog change, not a desk setting"), a cap outside the
/// mandated universe. Every one of those is a considered answer about a
/// well-formed request, written for a human, and the remedy is inside it.
///
/// `Applied` carries the **merged** pair the owner answers with rather than
/// what was sent, for the reason `News::Applied` carries the resolved stack: a
/// receipt composed from the request would report a value the mandate had
/// clamped as the one in force. `warning` rides with it because a cap the
/// effective method will refuse *applies* — the ruling is warn, not refuse —
/// and the plan it breaks is minutes away.
#[derive(Debug)]
pub enum Mandate {
    Applied {
        policy: String,
        cap: Option<i64>,
        warning: Option<String>,
    },
    /// The owner would not solve that way. Its own sentence.
    Rejected(String),
}

/// What the owner did with a predictor lane. Two outcomes, and the second is
/// not an error — the same split [`News`], [`Mandate`] and [`Choice`] make,
/// for the same reason.
///
/// The route refuses with **400 and a sentence**, and the one an operator will
/// actually hit names the lanes: `unknown model 'forest:deep'; available:
/// ('ridge:none', 'groupwise:angle', …)`. That is a considered answer about a
/// well-formed request and it carries the whole remedy, so it is rendered
/// rather than folded into a broken socket.
///
/// `Ran` carries the owner's own **answer**, never the request: `models` is
/// what it actually fitted — the operator's lane plus `ridge:none`, which the
/// route always appends, because a challenger without its control is not
/// evidence — and `champion` is `null` when nothing cleared admission. That
/// `None` is a result, not a missing value, and this client may not render it
/// as one.
#[derive(Debug)]
pub enum Board {
    Ran {
        run_id: Option<String>,
        /// The lanes the owner ran, in its own order. Not read as "what was
        /// asked for": the baseline is in here whether or not it was sent.
        models: Vec<String>,
        /// The admitted champion, or `None` — which is the board saying
        /// nothing cleared the bar, and is an answer.
        champion: Option<String>,
    },
    /// The owner would not run that lane. Its own sentence, which names the
    /// lanes it does serve.
    Rejected(String),
}

/// What the venue said about the stored login.
///
/// `/api/alpaca/test` answers **200 whatever happened** — a missing profile, a
/// rejected key, a silent venue and a captive portal are all results with a
/// sentence, and C1 states that plainly: "a client rendering 'did it work?'
/// reads one shape however the answer turned out". So `ok` is read from the
/// body and never from the status code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TestVerdict {
    pub ok: bool,
    /// What to show the operator: the masked account and its buying power when
    /// the login works, the owner's reason when it does not. Never empty.
    pub summary: String,
}

/// The operator's end of the owner API.
///
/// Holds a base URL and an HTTP client and nothing else — no registry handle, no
/// broker, no plan. Every method below is one POST.
///
/// Constructed by the composition root unless `--glass` vetoes it, and reached
/// only from there: `tests/operator_gate.rs` asserts that nothing under `ui/`
/// names this type. AUDIT's `a`/`R` and BOOK's `x` return `Command`s that
/// `main::Writes` dispatches here.
///
/// Most of the methods below have a key path now — the confirm modal's
/// execute, AUDIT's approve and reject, the desk verbs, `/model`, `/mode`, and
/// `/do`'s `start_task`. The Atlas mode verbs still do not, and they are the
/// reachable-code-with-no-caller shape invariant 10 names: held deliberately
/// because the routes were pinned against the owner's dispatch table in one
/// pass, and on that list until a surface presses them.
/// How long an ask may think: the owner's own reasoner budget (60s) plus the
/// margin a slow model needs to serialize its answer onto the bus.
const ASK_DEADLINE: std::time::Duration = std::time::Duration::from_secs(90);

/// How long a **checked** news save may take.
///
/// A verify is one live fetch per member, and the owner's own catalog puts
/// `gdelt` at 43–75s per request — five members at the bad end of that is over
/// six minutes, which is longer than this waits. The bound is deliberate: past
/// it, a wedged owner has to be reported rather than hung on, and the residual
/// is that a worst-case five-source check can still come back as a failed
/// write for a save that applied. What it buys is the common case, which the
/// poller's eight seconds got wrong every time: the request gave up while the
/// owner was still reading feeds, and this client reported a `.env` it had
/// already written as a write that failed.
///
/// **Only the checked save takes it.** A plain save is one `.env` write with
/// no network of its own, so it keeps the eight-second failure signal — five
/// minutes of silence for that one would hide a dead owner behind a card that
/// looks busy.
const NEWS_VERIFY_DEADLINE: std::time::Duration = std::time::Duration::from_secs(300);

/// How long a predictor board may take to fit.
///
/// The owner's route is **synchronous**: `POST /api/research/predictors/run`
/// returns when every fold of every lane has been fitted, and H1 measured that
/// in seconds for the offline synthetic panel — but a live 756-day lookback
/// opens a market fetch first and then fits the operator's lane *and* the
/// baseline, which is where "seconds" becomes "a minute". The poller's eight
/// seconds would give up on all of it, report a write that failed, and leave
/// the owner still fitting: the same mistake the news check made before
/// [`NEWS_VERIFY_DEADLINE`] existed.
///
/// Shorter than the news check, deliberately. That one is bounded by five live
/// feeds it does not control; this one is bounded by the owner's own CPU, and
/// past two minutes a board is not slow, it is wedged — at which point an
/// operator needs to be told rather than kept in front of a busy line.
const PREDICTOR_RUN_DEADLINE: std::time::Duration = std::time::Duration::from_secs(120);

pub struct WriteClient {
    base: String,
    client: reqwest::Client,
}

impl WriteClient {
    /// A writer aimed at one owner.
    ///
    /// Shares `http::TIMEOUT` with the poller rather than choosing its own: a
    /// write that gave up sooner than a read would report a plan as failed while
    /// the owner was still booking it, which is the one ambiguity an execution
    /// path may not have.
    ///
    /// Fallible rather than falling back to a default client. That default
    /// carries no timeout, so an `execute_plan` against a wedged owner would
    /// hang with no answer and no error — an execution left in exactly the
    /// unknown state this type exists to keep out of. Invariant 4: refuse
    /// loudly, never degrade quietly.
    pub fn new(base: &str) -> Result<Self, WriteError> {
        Ok(Self {
            base: base.trim_end_matches('/').to_string(),
            client: crate::net::http::build_client()
                .map_err(|err| WriteError::NoClient(crate::net::because(&err)))?,
        })
    }

    /// Where this writer points. The status line already says it; an operator
    /// reading a log after a fill needs the same fact beside the action.
    pub fn base(&self) -> &str {
        &self.base
    }

    // -- the execution gate ------------------------------------------------

    /// Approve one pending approval request.
    ///
    /// No note parameter, deliberately. The plan's sketch had `approve(id,
    /// note)`, but the owner's handler is `decide_approval(approval_id, action)`
    /// and reads nothing from the body — a `note` argument would be accepted,
    /// sent, and silently discarded, which is a lie told by a type signature.
    /// If the desk ever wants an operator's reason on the record, the owner
    /// needs the field first.
    pub async fn approve(&self, approval_id: &str) -> Wrote {
        self.post(&format!("/api/approvals/{approval_id}/approve"), json!({}))
            .await
    }

    /// Open an approval request bound to a checked plan.
    ///
    /// The request is the desk's question, not the answer: the owner binds it
    /// to the plan's exact `targets_hash`, and approving it is a second,
    /// confirmed write. Returns the owner's id for the request, which is what
    /// the approve box is then opened against.
    pub async fn request_approval(&self, plan_id: &str) -> Result<String, WriteError> {
        let said = self
            .post("/api/approvals", json!({ "plan_id": plan_id }))
            .await?;
        said.get("approval_id")
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .ok_or_else(|| {
                WriteError::Unreadable(format!(
                    "the owner answered 200 for an approval request without an approval_id: {said}"
                ))
            })
    }

    /// Reject one pending approval request.
    pub async fn reject(&self, approval_id: &str) -> Wrote {
        self.post(&format!("/api/approvals/{approval_id}/reject"), json!({}))
            .await
    }

    /// Book the plan the token was minted for.
    ///
    /// The only method that takes a capability rather than a string, and the
    /// only one whose arguments a caller cannot choose: the plan id, the
    /// approval id, and the hash the human typed against all come out of the
    /// token together. Passing "the wrong plan" is not a mistake that can be
    /// made here — it would have to be made in the modal, in front of the
    /// human, against the six characters of that plan's own `targets_hash`.
    ///
    /// `approval_id` rides in the body because the owner requires it: the
    /// handler returns 400 for a body carrying only `human_confirmed`, with the
    /// reason that "a bare human_confirmed flag cannot book a trade" — a
    /// self-attestation any local process could send. The persisted approval is
    /// what authorises the fill; the boolean only records that a human was the
    /// one who asked.
    ///
    /// Returns three outcomes. A 200 is *not* a fill: see [`Execution`].
    pub async fn execute_plan(&self, token: ConfirmToken) -> Result<Execution, WriteError> {
        // The one action in this crate that moves money, on the record before it
        // is attempted. The owner writes the authoritative audit event, but that
        // only exists if the request arrived — a request that timed out or was
        // aimed at the wrong port leaves no trace there, and this is then the
        // only record that the operator asked at all.
        tracing::warn!(
            plan = token.plan_id(),
            approval = token.approval_id(),
            targets_hash = token.targets_hash(),
            owner = %self.base,
            "human-confirmed plan execution requested"
        );
        let outcome = self
            .post(
                "/api/plans/execute",
                json!({
                    "plan_id": token.plan_id(),
                    "approval_id": token.approval_id(),
                    "human_confirmed": true,
                }),
            )
            .await
            .and_then(Execution::read);

        // The other half of the pair. A log that records the asking and never
        // the answer leaves every audit trail ending at "requested", which reads
        // as an attempt of unknown outcome — the one thing an execution record
        // may not be. All three outcomes land here, refusals included.
        match &outcome {
            Ok(Execution::Executed(_)) => {
                tracing::warn!(plan = token.plan_id(), "the owner booked the plan")
            }
            Ok(Execution::Refused {
                blocked_by,
                reasons,
            }) => tracing::warn!(
                plan = token.plan_id(),
                blocked_by,
                reasons = reasons.join("; "),
                "the desk refused the fill"
            ),
            Err(err) => tracing::error!(
                plan = token.plan_id(),
                error = %err,
                "the execution request failed; the fill's state is unknown"
            ),
        }
        outcome
    }

    /// Book the desk's current proposal, in one confirmed call.
    ///
    /// The second method in this file that takes a capability rather than
    /// strings, and for the same reason: the plan and the hash come out of a
    /// [`BookToken`] together, minted only by the box a human read them in.
    /// There is no argument a caller can vary, so "confirm one allocation and
    /// book another" is not a mistake that can be made here.
    ///
    /// **It sends no `approval_id`, unlike `execute_plan`, and that is the
    /// owner's contract rather than a shortcut.** The route resolves the
    /// current proposal itself and refuses a `plan_id` that is not it, so
    /// naming the approval here would be this client choosing which question it
    /// is answering. `human_confirmed: true` records that a human asked; the
    /// persisted approval the owner finds is what authorises the fill, and the
    /// owner re-validates the hash, the referee's PASS and the book revision
    /// before anything is placed.
    ///
    /// Returns four outcomes, and a 200 is not a fill: see [`Booked`].
    pub async fn book(&self, token: BookToken) -> Result<Booked, WriteError> {
        // On the record before it is attempted, exactly as `execute_plan` is:
        // the owner writes the authoritative audit event, but only if the
        // request arrived, and a request that timed out leaves this as the only
        // trace that the operator asked at all.
        tracing::warn!(
            plan = token.plan_id(),
            targets_hash = token.targets_hash(),
            owner = %self.base,
            "human-confirmed one-click booking requested"
        );
        let plan = token.plan_id().to_string();
        let outcome = self
            .post(
                "/api/desk/proposal/book",
                json!({
                    "plan_id": token.plan_id(),
                    "targets_hash": token.targets_hash(),
                    "human_confirmed": true,
                }),
            )
            .await
            .and_then(Booked::read);

        // The other half of the pair: an audit trail ending at "requested"
        // reads as an attempt of unknown outcome, which is the one thing an
        // execution record may not be. All four outcomes land here.
        match &outcome {
            Ok(Booked::Filled(_)) => tracing::warn!(plan, "the owner booked the proposal"),
            Ok(Booked::Invalidated {
                blocked_by,
                reasons,
            }) => tracing::warn!(
                plan,
                blocked_by,
                reasons = reasons.join("; "),
                "the desk refused the fill and withdrew the approval"
            ),
            Ok(Booked::Standing {
                blocked_by,
                reasons,
            }) => tracing::warn!(
                plan,
                blocked_by,
                reasons = reasons.join("; "),
                "the desk refused the fill; the proposal stands"
            ),
            Ok(Booked::Unstated {
                blocked_by,
                reasons,
            }) => tracing::error!(
                plan,
                blocked_by,
                reasons = reasons.join("; "),
                "the desk refused the fill without saying whether the proposal survives"
            ),
            Err(err) => tracing::error!(
                plan,
                error = %err,
                "the booking request failed; the fill's state is unknown"
            ),
        }
        outcome
    }

    // -- Atlas ------------------------------------------------------------

    /// Set the supervisor's mode. What Atlas may *research* is not what it may
    /// execute: the owner refuses every plan-creating template below `propose`,
    /// and this call cannot widen that.
    pub async fn atlas_mode(&self, mode: &str) -> Wrote {
        self.post("/api/atlas/mode", json!({ "mode": mode })).await
    }

    pub async fn atlas_pause(&self) -> Wrote {
        self.post("/api/atlas/pause", json!({})).await
    }

    /// Resume into a named mode. The owner defaults to `observe` when the body
    /// carries none; this asks explicitly, so a resume never silently restores
    /// a wider posture than the operator meant.
    pub async fn atlas_resume(&self, mode: &str) -> Wrote {
        self.post("/api/atlas/resume", json!({ "mode": mode }))
            .await
    }

    pub async fn atlas_autonomy(&self, enabled: bool) -> Wrote {
        self.post("/api/atlas/autonomy", json!({ "enabled": enabled }))
            .await
    }

    /// Put a question to the desk. It grants no authority — the owner records
    /// the question, answers it through the configured reasoner, and puts that
    /// answer back on the bus as a second `atlas_message` row, which the
    /// console renders like any other. So the reply arrives through the event
    /// stream, not through this response, and this call carries no
    /// confirmation: the `note` it returns says only whether the desk could
    /// answer at all.
    ///
    /// With its own deadline, because a question is allowed to think: the
    /// owner answers through the configured reasoner and budgets it
    /// `_ATLAS_REPLY_TIMEOUT_S` (60s). The shared 8-second write deadline is
    /// sized for plan writes, and an ask that outlived it was reported
    /// "WRITE FAILED — the owner did not answer" while the answer was mid-
    /// compose and landed on the bus seconds later — a false negative on the
    /// one surface whose whole job is telling the operator the truth.
    pub async fn atlas_message(&self, text: &str) -> Wrote {
        self.post_within("/api/atlas/message", json!({ "text": text }), ASK_DEADLINE)
            .await
    }

    /// Ask the desk what it would do, and let it write the proposals down.
    ///
    /// **The reason the desk has anything to show.** The owner mints
    /// `proposal`-origin task rows here and nowhere else, and the snapshot's
    /// `actionables` block is *composed from those rows* — it never mints, so
    /// a desk nobody has asked draws an empty panel and refuses every `/do`.
    /// This is the only call in this workstation that asks.
    ///
    /// A write, deliberately, and gated as one: it goes through
    /// `Writes::dispatch` like every other, so an unarmed window or a
    /// `--glass` one can read the panel somebody else filled and cannot put
    /// new rows in the desk's queue.
    ///
    /// **Never on a timer.** The owner composes this from `atlas_facts`, and
    /// `_atlas_regime_facts` latches the regime state it saw — a poll or a
    /// per-frame ask would consume a regime flip before the owner's own
    /// observe tick could raise a trigger from it. One press, one ask.
    ///
    /// It grants nothing. Every item comes back with the gate's verdict on it,
    /// and approving one re-runs `check_startable` at the start route: an ask
    /// in Research cannot produce an item that creates a paper plan.
    pub async fn actionables(&self) -> Result<Proposed, WriteError> {
        Proposed::read(self.post("/api/atlas/actionables", json!({})).await?)
    }

    /// Start the proposal a human approved.
    ///
    /// **The one write on this route, and the one the owner treats as the
    /// approval itself.** The beat passes over proposal-origin tasks, so
    /// nothing but an operator reaches this — which is why the owner records
    /// an `atlas_proposal_approved` row before it starts anything.
    ///
    /// `task_id` is one the owner served on the snapshot, never one that was
    /// typed: the parser resolves a template id to the task the payload
    /// carried and refuses an item with none, so there is no id here that this
    /// client composed.
    ///
    /// Grants nothing. The route re-runs `check_startable` — mode authority,
    /// the retry budget, the data preconditions the snapshot could not check —
    /// and refuses a plan-creating template below `propose` exactly as it
    /// would have refused the beat. Returns three outcomes, and a 200 is *not*
    /// a start: see [`Start`].
    pub async fn start_task(&self, task_id: &str) -> Result<Start, WriteError> {
        match self
            .post(&format!("/api/atlas/tasks/{task_id}/start"), json!({}))
            .await
        {
            Ok(said) => Start::read(said),
            // The **one** refusal this route does answer with a status: a task
            // that is no longer queued or failed raises `PermissionError` in
            // `AtlasSupervisor.start_task` and the owner's dispatcher turns it
            // into a 400 (the `/api/atlas/tasks/<id>/start` arm). It is the
            // same gate saying no about the same request as the 200s above —
            // "task X is 'completed'; only a queued or failed task may start"
            // — so it comes back as a refusal, exactly as `set_llm` treats its
            // own 400. Rendering it as a transport failure would bury the
            // sentence that says the day's proposal is already spent.
            //
            // `unstated`, because the owner names no category here and this
            // client may not invent one. Any other status is a broken owner
            // rather than a considered answer, and stays an error.
            Err(WriteError::Refused { status: 400, said }) => Ok(Start::Refused {
                blocked_by: "unstated".to_string(),
                reason: sentence(&said),
            }),
            Err(err) => Err(err),
        }
    }

    pub async fn workforce_fast(&self, enabled: bool) -> Wrote {
        self.post("/api/workforce/fast", json!({ "enabled": enabled }))
            .await
    }

    /// Point one surface at a model, or switch the reasoner.
    ///
    /// `pair` travels as one argument because the owner takes it as one:
    /// `backend` and `model` are optional *together*, and absent means "leave
    /// the pair alone" — which is what makes `{surface, enabled}` a switch. A
    /// signature with two independent `Option`s could express half a choice,
    /// and the owner answers that with "a model choice needs both a backend and
    /// a model".
    ///
    /// Absent is not empty. An empty string is a choice of nothing and the
    /// owner refuses it, so nothing here ever sends one — the parser will not
    /// build a pair with an empty half.
    ///
    /// Grants no authority. Which model answers a question is not permission to
    /// act on the answer: the owner pins the referee to claude in its own
    /// routing, and a fill still needs a persisted approval and a typed hash.
    pub async fn set_llm(
        &self,
        surface: &str,
        pair: Option<(&str, &str)>,
        enabled: Option<bool>,
    ) -> Result<Choice, WriteError> {
        let mut body = json!({ "surface": surface });
        if let Some((backend, model)) = pair {
            body["backend"] = json!(backend);
            body["model"] = json!(model);
        }
        if let Some(enabled) = enabled {
            body["enabled"] = json!(enabled);
        }
        match self.post("/api/llm", body).await {
            // The owner answers with what the change *means* ("Atlas reasons
            // with ollama qwen2.5:7b"). A 200 without one is a broken contract
            // and says so, rather than this client inventing a receipt out of
            // the words it just sent — the same rule as `desk_mode`'s label and
            // `start_workflow`'s handle.
            Ok(said) => match field(&said, "effect") {
                Some(effect) => Ok(Choice::Chosen(crate::format::bounded(&effect, SAID_MAX))),
                None => Err(WriteError::Unreadable(format!(
                    "the owner answered 200 for a model choice without saying what it did: {said}"
                ))),
            },
            Err(WriteError::Refused { status: 400, said }) => Ok(Choice::Rejected(sentence(&said))),
            Err(err) => Err(err),
        }
    }

    // -- the desk ----------------------------------------------------------

    /// Choose the data source and the book.
    ///
    /// Two arguments, not the one label the plan's sketch had: the owner builds
    /// `DeskMode(body["data"], body["book"])` and rejects the pair it cannot
    /// make. A single label would have to be split somewhere, and the client is
    /// the wrong place to decide which half of a desk mode a word belongs to.
    pub async fn desk_mode(&self, data: &str, book: &str) -> Wrote {
        self.post("/api/desk_mode", json!({ "data": data, "book": book }))
            .await
    }

    /// Answer the desk's arming question.
    ///
    /// A `bool` and nothing else: the owner refuses anything that is not one
    /// (`armed must be true or false`), because "yes", `1` and `[]` are not
    /// consent. The answer it returns is `posture_payload()` — its own account
    /// of what it now holds, which is the only thing worth reporting back.
    pub async fn set_posture(&self, armed: bool) -> Wrote {
        self.post("/api/desk/posture", json!({ "armed": armed }))
            .await
    }

    /// Start a governed workforce run.
    ///
    /// `kind` and `goal` only. The owner reads `as_of`, `universe`, and
    /// `offline` from the body too, but it defaults all three, and it refuses to
    /// read the phase graph from a network caller at all — "letting a network
    /// caller shape the phase graph would let it drop a gate phase". Sending
    /// less is the narrower surface.
    pub async fn start_workflow(&self, kind: &str, goal: &str) -> Wrote {
        self.post(
            "/api/workflows/start",
            json!({ "kind": kind, "goal": goal }),
        )
        .await
    }

    // -- the alpaca login --------------------------------------------------

    /// Store a paper login through the owner, which is the only thing in qlab
    /// that writes a credential file.
    ///
    /// The two values are `Secret`s rather than `&str` for the reason
    /// `crate::secret` states: everything between the form and this line
    /// formats what it is given, and this is the one place the plaintext is
    /// unwrapped.
    ///
    /// `replace` is an `Option` and not a `bool` so that "the operator has not
    /// been asked" and "the operator said no" are not the same wire body as
    /// "the operator said yes". `None` omits the field entirely and the owner
    /// defaults it; the flag is only ever set by a caller holding a consent it
    /// obtained for this exact pair, which is [`Login::ConsentNeeded`]'s whole
    /// purpose.
    ///
    /// Never switches the book. The owner is explicit about that — a login
    /// makes `LIVE·ALPACA` *choosable* and nothing more — so the answer's
    /// `credentials_ok` is a fact about the credential, not about the desk.
    pub async fn set_alpaca_credentials(
        &self,
        api_key: &Secret,
        api_secret: &Secret,
        replace: Option<bool>,
    ) -> Result<Login, WriteError> {
        // No tracing on this call. `execute_plan` logs because a fill's request
        // may be the only record it was ever attempted; a login leaves the
        // owner's own `alpaca.credentials_updated` row, which is written on the
        // side that succeeded, and a line here could only add a second place
        // that has to be reasoned about for key material.
        let mut body = json!({
            "api_key": api_key.expose(),
            "api_secret": api_secret.expose(),
        });
        if let Some(replace) = replace {
            body["replace"] = json!(replace);
        }
        let answered = self.post("/api/alpaca/credentials", body).await;
        let Err(err) = answered else {
            return answered.map(Login::Stored);
        };
        // **One gate over every way this call can fail**, before any branch
        // decides what kind of failure it was.
        //
        // The first version of this scrubbed inside the 400 arm and let every
        // other status through verbatim, which is the same per-call-site
        // reasoning about what can carry a credential that took four rounds to
        // fix in B1 — and it was worse than it looked: an interposing proxy
        // answers 401, 413, 502 or 504 far more readily than 400, and each of
        // those fell to `Err`, whose `Display` prints the body into
        // `Wrote::Failed`, the toast, and the form's own note.
        //
        // The discriminator is read off the raw body *first*, because it is a
        // field of that JSON and the scrub replaces the body with the sentence
        // inside it.
        let asked = matches!(&err, WriteError::Refused { status: 400, said } if confirmable(said));
        let confirmable_refusal = matches!(err, WriteError::Refused { status: 400, .. });
        let said = unquoted(sentence(err.said()), api_key, api_secret);
        // Both 400s are the owner answering a question about this request. Any
        // other status is a broken owner rather than a question: offering to
        // discard a browser login off the back of a 500 would be this client
        // inventing a consent prompt out of a traceback.
        if confirmable_refusal {
            return Ok(match asked {
                true => Login::ConsentNeeded(said),
                false => Login::Rejected(said),
            });
        }
        Err(err.with_said(said))
    }

    // -- the news stack ----------------------------------------------------

    /// Choose which sources the desk reads its news from.
    ///
    /// It changes what the desk *reads*, never what it can execute: the route
    /// writes `.env` and the process environment, takes no registry lock, and
    /// touches no plan, approval or posture. Every gate between a plan and a
    /// fill is unmoved by it.
    ///
    /// `contact` is an `Option` because "leave the stored one alone" and "use
    /// this one" are two different requests, and an empty string is neither —
    /// the owner would read one as a contact of nothing and refuse the shape.
    /// It is an identity the SEC asks callers to send, not a credential: it is
    /// carried as a plain `&str` rather than a [`Secret`], and it is still
    /// never rendered anywhere but the box it is typed into.
    ///
    /// `offline` travels explicitly. The route defaults it to the desk mode,
    /// and a window pointed at the other lane would then be told about a stack
    /// it is not reading.
    pub async fn set_news(
        &self,
        providers: &[String],
        contact: Option<&str>,
        verify: bool,
        offline: bool,
    ) -> Result<News, WriteError> {
        let mut body = json!({
            "providers": providers,
            "verify": verify,
            "offline": offline,
        });
        if let Some(contact) = contact {
            body["edgar_contact"] = json!(contact);
        }
        // The deadline follows the question, not the route: see
        // [`NEWS_VERIFY_DEADLINE`] for why a check gets minutes and a plain
        // save keeps the eight seconds that say the owner is gone.
        let answered = match verify {
            true => {
                self.post_within("/api/news/settings", body, NEWS_VERIFY_DEADLINE)
                    .await
            }
            false => self.post("/api/news/settings", body).await,
        };
        match answered {
            Ok(said) => Ok(News::Applied {
                // The owner's own resolution, read off the answer. A 200 with
                // no stack is not a contract failure worth refusing the whole
                // change over — the change *happened* — so it reports an empty
                // resolution and the refetch behind it says what the desk now
                // reads.
                stack: said
                    .get("stack")
                    .and_then(Value::as_array)
                    .map(|names| {
                        names
                            .iter()
                            .filter_map(Value::as_str)
                            .filter(|name| !name.is_empty())
                            .map(str::to_string)
                            .collect()
                    })
                    .unwrap_or_default(),
                verified: match verify {
                    true => verified(&said),
                    // Not asked, so not read: a members block on a request
                    // that did not ask for one is not this call's answer.
                    false => Vec::new(),
                },
            }),
            Err(WriteError::Refused { status: 400, said }) => Ok(News::Rejected(sentence(&said))),
            Err(err) => Err(err),
        }
    }

    // -- the research board ------------------------------------------------

    /// Fit one predictor lane against the board's own baseline.
    ///
    /// **A research run, and it can reach nothing else.** The route fits a risk
    /// model over a panel and writes one `predictor_board` run row; it opens no
    /// plan, touches no approval, moves no posture and books nothing, and every
    /// gate between a plan and a fill is unmoved by it. What it costs is the
    /// owner's CPU — see [`PREDICTOR_RUN_DEADLINE`].
    ///
    /// One lane per call, though the owner accepts a list. The picker sends
    /// what the operator chose, and a client that could send several would put
    /// one keystroke behind a board an operator did not read the shape of. The
    /// baseline rides along regardless: the owner appends `ridge:none` itself,
    /// which is why the answer's `models` is read back rather than echoed.
    ///
    /// `universe` and `lookback_days` are left to the route's own defaults
    /// (`core`, 756). Sending them from here would be this client asserting a
    /// research design it has no surface to choose one with — the day a pane
    /// offers the choice is the day they belong in the body.
    ///
    /// `offline` travels explicitly, for `set_news`' reason: the route defaults
    /// it to the desk mode, and a window pointed at the other lane would be
    /// told about a board it is not reading.
    pub async fn run_predictor(&self, model: &str, offline: bool) -> Result<Board, WriteError> {
        let body = json!({ "model": model, "offline": offline });
        // The one knob this route varies: an answer is allowed to take minutes,
        // because the fit is what it is waiting on. Everything else — the
        // refusal handling, the verbatim body — is the one implementation.
        match self
            .post_within("/api/research/predictors/run", body, PREDICTOR_RUN_DEADLINE)
            .await
        {
            Ok(said) => Ok(Board::Ran {
                run_id: field(&said, "run_id"),
                models: said
                    .get("models")
                    .and_then(Value::as_array)
                    .map(|names| {
                        names
                            .iter()
                            .filter_map(Value::as_str)
                            .filter(|name| !name.is_empty())
                            .map(str::to_string)
                            .collect()
                    })
                    .unwrap_or_default(),
                // Absent and `null` collapse here, and they mean the same
                // thing on this route: nothing cleared admission. `field`
                // already treats an empty string as absent, which is the third
                // spelling of the same answer.
                champion: field(&said, "champion"),
            }),
            Err(WriteError::Refused { status: 400, said }) => Ok(Board::Rejected(sentence(&said))),
            Err(err) => Err(err),
        }
    }

    // -- the method and the cap --------------------------------------------

    /// Choose the operational method this desk solves with, or its cap.
    ///
    /// One key per call, because [`crate::cmd::MethodChange`] can hold no more
    /// than one: the owner takes any subset of the two and writes one audit row
    /// per changed field, and a call that could send both would put two
    /// decisions behind one keystroke.
    ///
    /// `null` is a request, not an omission. Clearing the cap drops the
    /// override and puts the shipped mandate's own value back in force, which
    /// is a change the owner records — so the body carries an explicit
    /// `max_holdings: null` rather than leaving the key out.
    pub async fn set_method(
        &self,
        change: &crate::cmd::MethodChange,
    ) -> Result<Mandate, WriteError> {
        use crate::cmd::MethodChange;
        let body = match change {
            MethodChange::Policy(id) => json!({"operational_policy": id}),
            MethodChange::Cap(cap) => json!({"max_holdings": cap}),
        };
        match self.post("/api/desk/method", body).await {
            Ok(said) => Ok(Mandate::Applied {
                // The owner's own merge, read off `current`. A 200 that does
                // not say what is in force is not a contract failure worth
                // refusing the change over — the change *happened* — so it
                // reports the absence and the refetch behind it says what the
                // desk now solves with.
                policy: said
                    .get("current")
                    .and_then(|current| field(current, "operational_policy"))
                    .unwrap_or_else(|| "the owner did not say which method".to_string()),
                cap: said
                    .get("current")
                    .and_then(|current| current.get("max_holdings"))
                    .and_then(Value::as_i64),
                warning: field(&said, "warning"),
            }),
            Err(WriteError::Refused { status: 400, said }) => {
                Ok(Mandate::Rejected(sentence(&said)))
            }
            Err(err) => Err(err),
        }
    }

    /// Ask the owner to put the stored login to the venue.
    ///
    /// One outcome, not two: the route answers 200 for a rejected key, a silent
    /// venue and a desk that has never logged in, because every one of those is
    /// a sentence an operator can act on rather than a failure.
    pub async fn test_alpaca(&self) -> Result<TestVerdict, WriteError> {
        let said = self.post("/api/alpaca/test", json!({})).await?;
        TestVerdict::read(said)
    }

    // NOTE: no `halt()` / `resume()` of the book. The plan's Part IV lists them,
    // but `qlab/ui/server.py` has no HTTP route for either — `set_halt` is
    // reachable only from the `halt`/`resume` MCP tools in
    // `qlab/mcp/quant_trader.py` and from the autopilot's own kill switch. There
    // is nothing here to call, and inventing an endpoint would be a client
    // asserting a surface the owner does not serve. Whether the owner should
    // grow one is an owner-side decision; see the task report.

    // -- the one request ---------------------------------------------------

    /// Every method above goes through here, so there is exactly one place that
    /// can build a request and exactly one that decides what an answer means.
    async fn post(&self, path: &str, body: Value) -> Wrote {
        self.post_within(path, body, crate::net::http::TIMEOUT)
            .await
    }

    /// `post`, with the one knob a route may vary: how long an answer is
    /// allowed to take. Everything else — refusal handling, the verbatim
    /// body — stays the one implementation.
    async fn post_within(&self, path: &str, body: Value, deadline: std::time::Duration) -> Wrote {
        let url = format!("{}{path}", self.base);
        let resp = match self
            .client
            .post(&url)
            .timeout(deadline)
            .json(&body)
            .send()
            .await
        {
            Ok(resp) => resp,
            Err(err) => return Err(WriteError::Unreachable(crate::net::because(&err))),
        };
        let status = resp.status();
        let text = match resp.text().await {
            Ok(text) => text,
            Err(err) => return Err(WriteError::Unreachable(crate::net::because(&err))),
        };
        if !status.is_success() {
            // The body verbatim. The owner's refusals are written for a human to
            // read, and paraphrasing one here would lose the remedy.
            return Err(WriteError::Refused {
                status: status.as_u16(),
                said: text,
            });
        }
        serde_json::from_str(&text).map_err(|err| WriteError::Unreadable(err.to_string()))
    }
}

impl TestVerdict {
    /// Read one 200 body from `/api/alpaca/test`.
    fn read(body: Value) -> Result<TestVerdict, WriteError> {
        match body.get("ok").and_then(Value::as_bool) {
            Some(true) => Ok(TestVerdict {
                ok: true,
                summary: account_line(&body),
            }),
            Some(false) => Ok(TestVerdict {
                ok: false,
                // The owner's reason, and a sentence of last resort rather than
                // a blank box: "it did not work" with nothing after it is not
                // something an operator can act on.
                summary: field(&body, "reason")
                    .unwrap_or_else(|| "alpaca would not take the stored login".to_string()),
            }),
            // The route always sets `ok`. A 200 without it is a broken contract,
            // and both guesses are indefensible — one vouches for a credential
            // nobody checked, the other condemns a working one.
            None => Err(WriteError::Unreadable(format!(
                "the owner answered 200 for a credential test without saying whether it \
                 worked: {body}"
            ))),
        }
    }
}

/// What a working login is worth showing: the account it reached, its state,
/// and what it can buy with.
///
/// Every part is optional because every part comes from the venue's own JSON,
/// and an absent one is left out rather than rendered as a dash — the question
/// this line answers is "did it work", and it already has.
fn account_line(body: &Value) -> String {
    let mut parts: Vec<String> = Vec::new();
    parts.extend(field(body, "account_masked"));
    parts.extend(field(body, "status"));
    if let Some(buying_power) = body.get("buying_power").and_then(Value::as_f64) {
        parts.push(match field(body, "currency") {
            Some(currency) => format!("{} {currency}", crate::format::money(buying_power)),
            None => crate::format::money(buying_power),
        });
    }
    match parts.is_empty() {
        true => "alpaca accepted the stored login".to_string(),
        false => parts.join(" · "),
    }
}

/// What a verify said, one row per member.
///
/// **The `verify.ok` beside the members is deliberately not read.** It is
/// any-member — true when one source answered — so a client that reported it
/// as the answer would draw a dead feed as a clean check. Every member is
/// carried instead, including the ones that answered, because a member can be
/// `ok` *and* carry a `partial:` flag and that is a third state neither a
/// pass-list nor a fail-list can hold.
///
/// Empty when the answer carries no members at all. A save that was asked to
/// check and said nothing about what it checked is a broken contract, and the
/// caller reports it as one rather than as a clean check.
fn verified(said: &Value) -> Vec<crate::bus::NewsMember> {
    let Some(members) = said
        .get("verify")
        .and_then(|block| block.get("members"))
        .and_then(Value::as_object)
    else {
        return Vec::new();
    };
    members
        .iter()
        .map(|(name, member)| {
            // Absent is not "it answered". The owner always sends the flag, so
            // its absence is a contract this client cannot read — and silence
            // about a feed the desk is about to reason from must not pass as a
            // clean one. Invariant 4.
            let ok = member.get("ok").and_then(Value::as_bool);
            crate::bus::NewsMember {
                name: crate::format::bounded(name, SAID_MAX),
                ok: ok == Some(true),
                detail: match (ok, field(member, "detail")) {
                    (None, said) => said
                        .unwrap_or_else(|| "the owner did not say whether it answered".to_string()),
                    (_, said) => said.unwrap_or_default(),
                },
                quality_flags: member
                    .get("quality_flags")
                    .and_then(Value::as_array)
                    .map(|flags| {
                        flags
                            .iter()
                            .filter_map(Value::as_str)
                            .filter(|flag| !flag.is_empty())
                            .map(|flag| crate::format::bounded(flag, SAID_MAX))
                            .collect()
                    })
                    .unwrap_or_default(),
            }
        })
        .collect()
}

/// One string field of an owner answer, absent when it is empty — the
/// `Some("")`-is-absent rule this client holds everywhere.
fn field(body: &Value, key: &str) -> Option<String> {
    body.get(key)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .map(str::to_string)
}

/// Whether a 400 body is the owner asking a question rather than refusing.
///
/// The **field**, and the exact flag it names. Never the sentence: the owner's
/// other 400 reads "replace must be true or false", so a substring check would
/// offer to discard a browser login over a mistyped boolean. And never the
/// field's mere presence: the value is which flag would grant the request, and
/// this client knows how to set exactly one.
fn confirmable(body: &str) -> bool {
    serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|body| field(&body, "confirm"))
        .is_some_and(|flag| flag == "replace")
}

/// The operator-facing half of a refusal, bounded.
///
/// The owner writes these for a human and never quotes what was typed, so it is
/// rendered rather than paraphrased. Bounded and collapsed to one line because
/// the body on this path is not always the owner's: a proxy in front of the
/// desk answers with an HTML page, and a form field is not the place to
/// discover that.
///
/// **The bound is what caps the form's note.** A failed login is rendered into
/// `Form::note`, which wraps into whatever room the box has and is the one
/// surface here with no length of its own — an unbounded body would push the
/// footer off the box or clip mid-sentence. Capping at the boundary rather than
/// at the renderer means every surface downstream inherits it: the note, the
/// toast, and the `Wrote::Failed` a log line would carry.
///
/// The collapse-and-cut half now lives in `format::bounded`, because the rule
/// was never about refusals: SETTINGS' model card and the stored-login note
/// render foreign text too, and three copies of "how long is too long" is three
/// chances for one of them to be the one that forgot. What stays here is the
/// part that is about *this* route — which field of a JSON body is the half a
/// human reads.
fn sentence(body: &str) -> String {
    let said = serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|parsed| field(&parsed, "error"))
        .unwrap_or_else(|| body.to_string());
    crate::format::bounded(&said, SAID_MAX)
}

/// How much of an owner sentence this module passes on.
///
/// One bound for both directions, because the rule was never about refusals: a
/// 200 body is not proof the answer came from the owner either — a proxy in
/// front of the desk answers 200 with a page of its own — and the surfaces
/// downstream (a toast, a form note) are the same ones.
const SAID_MAX: usize = 240;

/// Foreign text, unless it handed back what was just sent.
///
/// The owner never quotes what was typed — C1 pins that at every one of its
/// refusals — but nothing on this path is guaranteed to be the owner's. A proxy
/// in front of the desk answers with a page of its own, at whichever status it
/// likes, and one that echoes the request would put the pair into a form note
/// and a toast without anything between here and the screen noticing.
///
/// This client is the only thing that knows what it just sent, so this is the
/// only place the check can be made — and it is made **once, over every failure
/// shape**, rather than in the arm whose status somebody happened to think of.
/// It refuses the whole sentence rather than cutting the value out of it: a
/// redacted refusal that still reads as a sentence invites an operator to trust
/// the rest of it.
fn unquoted(said: String, key: &Secret, secret: &Secret) -> String {
    let quoted = [key, secret]
        .into_iter()
        .any(|value| !value.expose().is_empty() && said.contains(value.expose()));
    match quoted {
        true => "the desk refused the login with a reply that quoted what was typed, so it is \
                 not shown here"
            .to_string(),
        false => said,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refusal_keeps_the_status_and_the_owners_sentence() {
        let err = WriteError::Refused {
            status: 400,
            said: r#"{"error": "human_confirmed=true is required"}"#.into(),
        };
        let said = err.to_string();
        assert!(said.contains("400"));
        assert!(said.contains("human_confirmed=true is required"));
    }

    #[test]
    fn a_verify_is_read_per_member_and_the_flag_beside_them_is_ignored() {
        // `verify.ok` is any-member: true when one source answered. Reading it
        // as the answer would report a dead feed as a clean check, so nothing
        // here reads it — and a member can be `ok` *and* carry a `partial:`
        // flag, which is a third state a pass/fail split cannot hold.
        let said = serde_json::json!({
            "verify": {
                "ok": true,
                "members": {
                    "macro": {"ok": true, "detail": "",
                              "quality_flags": ["partial: ecb: 502"]},
                    "rss": {"ok": false, "detail": "rss feed 503", "quality_flags": []},
                    "gdelt": {"detail": "", "quality_flags": []}
                }
            }
        });
        let members = verified(&said);
        assert_eq!(members.len(), 3);
        let of = |name: &str| {
            members
                .iter()
                .find(|member| member.name == name)
                .unwrap()
                .said()
        };
        assert_eq!(of("macro"), ("partial: ecb: 502".to_string(), false));
        assert_eq!(of("rss"), ("rss feed 503".to_string(), true));
        // No flag at all is not "it answered": the owner always sends one, so
        // its absence is a contract this client cannot read, and silence about
        // a feed the desk reasons from may not pass as a clean one.
        assert_eq!(
            of("gdelt"),
            (
                "the owner did not say whether it answered".to_string(),
                true
            )
        );
        // And a body with no members at all is empty rather than a clean
        // check invented here.
        assert!(verified(&serde_json::json!({"stack": ["macro"]})).is_empty());
        assert!(verified(&serde_json::json!({"verify": {"ok": true}})).is_empty());
    }

    #[test]
    fn the_base_is_normalised_so_a_trailing_slash_cannot_double_up() {
        // `//api/plans/execute` is a 404 on the owner, which would surface as
        // "the desk refused this trade" rather than "this client built a bad
        // url" — the worst possible reading of a failed execution.
        assert_eq!(
            WriteClient::new("http://127.0.0.1:8765/").unwrap().base(),
            "http://127.0.0.1:8765"
        );
    }
}
