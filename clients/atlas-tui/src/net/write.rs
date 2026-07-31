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
//! **The owner's refusals are the message.** A non-2xx is an error carrying the
//! owner's own words, never a swallowed failure and never a default. "execution
//! requires an approval_id", "approval is 'expired', not pending" — those are
//! the sentences that tell an operator what to do next, and a client that
//! reduced them to "request failed" would leave them pressing the same key.
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

use crate::ui::widgets::confirm::ConfirmToken;
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

type Wrote = Result<Value, WriteError>;

/// The operator's end of the owner API.
///
/// Holds a base URL and an HTTP client and nothing else — no registry handle, no
/// broker, no plan. Every method below is one POST.
///
/// Constructed by the composition root when `--operator` is passed, and reached
/// only from there: `tests/operator_gate.rs` asserts that nothing under `ui/`
/// names this type. Task 18 gives the approvals and plans views their key
/// branches, which return `Command`s for the runtime to dispatch here; until
/// then the gate tests are the only callers, which is deliberate — a key path
/// wired to a view that does not exist yet would be the reachable-code-with-no-
/// caller shape invariant 10 exists to catch.
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
    pub async fn execute_plan(&self, token: ConfirmToken) -> Wrote {
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
        self.post(
            "/api/plans/execute",
            json!({
                "plan_id": token.plan_id(),
                "approval_id": token.approval_id(),
                "human_confirmed": true,
            }),
        )
        .await
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
    /// the message and answers only through the coordinator — which is why it
    /// carries no confirmation.
    pub async fn atlas_message(&self, text: &str) -> Wrote {
        self.post("/api/atlas/message", json!({ "text": text }))
            .await
    }

    pub async fn workforce_fast(&self, enabled: bool) -> Wrote {
        self.post("/api/workforce/fast", json!({ "enabled": enabled }))
            .await
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
        let url = format!("{}{path}", self.base);
        let resp = match self.client.post(&url).json(&body).send().await {
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
