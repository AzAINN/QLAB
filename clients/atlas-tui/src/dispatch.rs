//! The dispatch seam: where a `Command` becomes a request, and what a write outcome owes the poller.
//!
//! This is the one place in the crate that turns a human's decision into a
//! call. `ui/` decides what a keystroke *means* and hands back a `Command`;
//! nothing under `ui/` may hold a client, and `tests/operator_gate.rs` greps the
//! tree to keep it that way. So every order path in this workstation runs
//! through `Writes::dispatch` below, with the composition root above it.
//!
//! It lives in the library rather than in `main.rs` for the reason invariant 10
//! keeps teaching this crate: it sat in the binary, where there is no test
//! harness, and two things that decide what actually reaches the owner — which
//! method a `Command` routes to, and whether a failed write refreshes the desk —
//! had no test exercising them at all. A seam only the binary can reach is a
//! seam nothing can pin.
//!
//! Gated with the writer it drives. In the default build there is no
//! `WriteClient` to hold, no `Command::Execute` to route, and no `AppEvent::Wrote`
//! to answer — so what is left here is a unit struct that keeps the runtime loop
//! one shape, and the artifact's read-only claim stays a property of absence.

#[cfg(feature = "operator")]
mod armed {
    use crate::bus::{AppEvent, Tx, Wrote};
    use crate::cmd::Command;
    use crate::net::write::{Execution, WriteClient, WriteError};
    use crate::store::Posture;
    use std::sync::Arc;

    /// Whether one bus event means the desk moved and the next poll should not
    /// wait for its own beat.
    ///
    /// **Every** write outcome, the failures included — which is the half worth
    /// stating, because it is not obvious. A refusal moved the registry: the
    /// gate invalidates the approval it declined
    /// (`execute_plan_with_approval`, `server.py:1876`). And a *failure* is the
    /// outcome where the desk's state is least knowable — a write shares the
    /// poller's eight-second timeout precisely because a request that gave up
    /// may still be booking (`net::http::TIMEOUT`, and `execute_plan` logs "the
    /// fill's state is unknown") — so the snapshot on screen is then both stale
    /// and the one an operator is about to press the key against again.
    /// Suppressing the refetch there kept the least trustworthy frame.
    ///
    /// Nothing else refetches from here. The stream already nudges the poller
    /// for the durable kinds (`net::sse::REFETCH_KINDS`), and a second rule for
    /// the same events in a second place is how the two come to disagree.
    pub fn refetches(ev: &AppEvent) -> bool {
        matches!(ev, AppEvent::Wrote(_))
    }

    /// The runtime's end of the write path.
    ///
    /// Holds a client and a bus sender and nothing else — no store, no view, no
    /// decision. Every method is a transcription of a `Command` the human
    /// already confirmed.
    pub struct Writes {
        client: Option<Arc<WriteClient>>,
        tx: Tx,
    }

    impl Writes {
        /// Build the write half for one window.
        ///
        /// Fallible rather than degrading: a client armed with `--operator`
        /// whose writer could not be built would run *looking* armed and refuse
        /// every key at the moment it mattered. Invariant 4 — refuse loudly.
        ///
        /// A featured binary the human did not arm holds no writer at all, so
        /// the posture chip's claim — "this window cannot place an order" — is
        /// true of the runtime and not only of the status line.
        pub fn new(base: &str, posture: Posture, tx: Tx) -> Result<Self, WriteError> {
            let client = match posture {
                Posture::Operator => Some(Arc::new(WriteClient::new(base)?)),
                Posture::Glass => None,
            };
            Ok(Self { client, tx })
        }

        /// Whether this window can write at all. The runtime never asks; a test
        /// does, because "armed" and "featured" are the two gates in series that
        /// this crate has already confused once.
        pub fn armed(&self) -> bool {
            self.client.is_some()
        }

        /// Carry one confirmed decision to the owner, off the frame loop.
        ///
        /// Spawned rather than awaited: a write shares the poller's eight-second
        /// timeout, and eight seconds of a frozen terminal after pressing `x` is
        /// how an operator ends up pressing it again. The answer comes back on
        /// the bus, which is the one place that owns the store, the toasts and
        /// the poller — and every outcome comes back, refusals included.
        pub fn dispatch(&self, cmd: Command) {
            let Some(client) = self.client.clone() else {
                // Unreachable through the key path — a glass window has no view
                // that opens a modal, because `confirm` is gated too — and loud
                // rather than silent if that ever stops being true.
                tracing::error!("a write was requested by a window holding no writer");
                return;
            };
            let tx = self.tx.clone();
            tokio::spawn(async move {
                if let Some(outcome) = perform(&client, cmd).await {
                    let _ = tx.send(AppEvent::Wrote(outcome));
                }
            });
        }
    }

    /// One command against one owner, and what it turned into.
    ///
    /// Split out of `dispatch` so the routing can be awaited directly: which
    /// method a `Command` reaches, and which `Wrote` each answer becomes, is the
    /// part that decides what happens to money — and inside a `tokio::spawn` it
    /// could only be observed through the bus, with the send and the routing
    /// failing as one.
    ///
    /// `None` for the two commands the runtime handles itself. They cannot
    /// arrive here through the loop; the arm exists because the match is over
    /// the whole type, and returning nothing is what keeps a stray `Quit` from
    /// putting a meaningless row on the bus.
    pub async fn perform(client: &WriteClient, cmd: Command) -> Option<Wrote> {
        Some(match cmd {
            Command::Approve(id) => match client.approve(&id).await {
                Ok(_) => Wrote::Decided {
                    approval_id: id,
                    decision: "approved",
                },
                Err(err) => Wrote::Failed {
                    what: format!("approve {id}"),
                    said: err.to_string(),
                },
            },
            Command::Reject(id) => match client.reject(&id).await {
                Ok(_) => Wrote::Decided {
                    approval_id: id,
                    decision: "rejected",
                },
                Err(err) => Wrote::Failed {
                    what: format!("reject {id}"),
                    said: err.to_string(),
                },
            },
            Command::Execute(token) => {
                // Read before the token is spent by the call: the outcome has to
                // name the plan it was about whichever way it goes.
                let plan_id = token.plan_id().to_string();
                match client.execute_plan(token).await {
                    Ok(Execution::Executed(_)) => Wrote::Executed { plan_id },
                    // A 200 that says `executed: false` is a governance decision,
                    // not a fill and not a failure. It carries the gate's own
                    // words because those are the sentence an operator acts on.
                    Ok(Execution::Refused {
                        blocked_by,
                        reasons,
                    }) => Wrote::Refused {
                        plan_id,
                        blocked_by,
                        reasons,
                    },
                    Err(err) => Wrote::Failed {
                        what: format!("execute {plan_id}"),
                        said: err.to_string(),
                    },
                }
            }
            // Neither of the two below is a governance decision, so neither
            // reports one. What they *do* report is the owner's own answer
            // about whether the request could go anywhere: a message queued for
            // a coordinator that is not running, and a workflow registered but
            // not yet driven, are both requests that "succeeded" and did
            // nothing — the exact shape this client refuses to render as a
            // receipt.
            Command::Message(text) => match client.atlas_message(&text).await {
                Ok(said) => Wrote::Asked {
                    note: said
                        .get("note")
                        .and_then(|v| v.as_str())
                        .unwrap_or("the desk has the message")
                        .to_string(),
                },
                Err(err) => Wrote::Failed {
                    what: "ask the desk".to_string(),
                    said: err.to_string(),
                },
            },
            Command::StartWorkflow { template, goal } => {
                match client.start_workflow(&template, &goal).await {
                    // The owner answers with the workflow row it created, so
                    // the id here is the registry's own — which is what lets an
                    // operator find the pipeline this key started rather than
                    // guessing at the newest one. A 200 without it is a broken
                    // contract and says so rather than inventing a handle.
                    Ok(said) => match said.get("workflow_id").and_then(|v| v.as_str()) {
                        Some(id) if !id.is_empty() => Wrote::Started {
                            template,
                            workflow_id: id.to_string(),
                        },
                        _ => Wrote::Failed {
                            what: format!("start {template}"),
                            said: format!("the owner answered without a workflow_id: {said}"),
                        },
                    },
                    Err(err) => Wrote::Failed {
                        what: format!("start {template}"),
                        said: err.to_string(),
                    },
                }
            }
            Command::Quit | Command::Refresh => return None,
        })
    }
}

#[cfg(feature = "operator")]
pub use armed::{perform, refetches, Writes};

/// The default build's write half: nothing, and nothing it could hold.
///
/// A type in both builds so the runtime loop has one shape — a loop that forked
/// on a feature is a loop only one leg ever runs — but there is no
/// `WriteClient` in this crate for it to carry and no `Command` variant for it
/// to route. Absence, not a branch.
#[cfg(not(feature = "operator"))]
pub struct Writes;

#[cfg(not(feature = "operator"))]
impl Writes {
    pub fn new(
        _base: &str,
        _posture: crate::store::Posture,
        _tx: crate::bus::Tx,
    ) -> Result<Self, std::convert::Infallible> {
        Ok(Self)
    }
}
