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
    use crate::cmd::{Command, ModelChoice};
    use crate::net::write::{
        Choice, Execution, Login, News, Proposed, Start, WriteClient, WriteError,
    };
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
        /// Fallible rather than degrading, and built before the screen is
        /// taken: a client whose writer could not be built would run *looking*
        /// capable and refuse every key at the moment it mattered. Invariant 4
        /// — refuse loudly.
        ///
        /// Built on `forced_glass` rather than on the posture, because the
        /// posture is no longer known at startup: it arrives with the owner's
        /// first snapshot. A window the *operator* vetoed with `--glass` holds
        /// no writer at all; a window that merely has not been armed by the
        /// desk yet holds one it never reaches, because `Posture::from_desk`
        /// keeps every modal and every write scope shut until the owner says
        /// the desk is armed.
        pub fn new(base: &str, forced_glass: bool, tx: Tx) -> Result<Self, WriteError> {
            let client = match forced_glass {
                true => None,
                false => Some(Arc::new(WriteClient::new(base)?)),
            };
            Ok(Self { client, tx })
        }

        /// Whether this window holds a writer at all. The runtime never asks; a
        /// test does, because the artifact gate, the operator's veto and the
        /// desk's answer are gates in series this crate has already confused
        /// once.
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
        ///
        /// The posture is checked *here*, at the one chokepoint every write
        /// passes through, and not only at the 33 places in `src/ui/` that
        /// decide whether to offer a key. Two reasons, and both are about the
        /// posture now being a value that can change mid-session:
        ///
        /// * A view gates on the posture when it *opens* a modal. A desk
        ///   disarmed between opening the confirm box and typing the last six
        ///   of the hash would otherwise still dispatch, because the keystroke
        ///   that emits `Command::Execute` is not the keystroke that was
        ///   checked.
        /// * "Every one of 33 call sites is correct, forever" is not a
        ///   guarantee, it is a hope. This restores the gate in series that the
        ///   old `client: None` gave for free when the posture was known at
        ///   startup: the runtime, not the renderer, has the last word.
        ///
        /// Refused loudly rather than dropped — the log line and a `Failed` row
        /// on the bus, which is how every other refusal in this crate reaches
        /// the operator. A key that silently did nothing is the failure mode
        /// invariant 4 exists for.
        pub fn dispatch(&self, cmd: Command, posture: Posture) {
            if !posture.writes() && !arms(&cmd) {
                let what = names(&cmd);
                tracing::error!(
                    command = %what,
                    "a write was requested by a window the desk has not armed"
                );
                let _ = self.tx.send(AppEvent::Wrote(Wrote::Failed {
                    what,
                    said: "this window is not armed — the desk's posture is read-only".to_string(),
                }));
                return;
            }
            let Some(client) = self.client.clone() else {
                // `--glass`: the operator's own veto, which holds no writer at
                // all. Unreachable through the key path, because a vetoed
                // window can never derive a writing posture either — and loud
                // rather than silent if that ever stops being true.
                tracing::error!("a write was requested by a window holding no writer");
                let _ = self.tx.send(AppEvent::Wrote(Wrote::Failed {
                    what: names(&cmd),
                    said: "this window was started with --glass and holds no writer".to_string(),
                }));
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

    /// The one command a window the desk has not armed may still send.
    ///
    /// Not a second dispatch path, and not a hole in the chokepoint: it is the
    /// *answer to the question the chokepoint is about*. Every window that can
    /// arm a desk is by construction one the desk has not armed, so a gate
    /// with no exemption here would make the door's arming question
    /// unanswerable and the posture unreachable from this client at all.
    ///
    /// It widens nothing by itself. The owner records the answer; this window
    /// keeps whatever posture the *next snapshot* derives, and the `--glass`
    /// veto below still refuses it — a window that declined its own authority
    /// may not vote itself back into it.
    fn arms(cmd: &Command) -> bool {
        matches!(cmd, Command::Posture { .. })
    }

    /// What a refused command was, in the same words `perform` names it by, so
    /// a refusal at the gate and a refusal from the owner read alike.
    ///
    /// Written out rather than derived from `Debug`: `AlpacaLogin` carries a
    /// typed credential, and a formatter that walked the whole value would put
    /// whatever `Secret`'s `Debug` happens to do today into a log line and a
    /// toast. The one thing this crate never renders is what was typed.
    fn names(cmd: &Command) -> String {
        match cmd {
            Command::Quit => "quit".to_string(),
            Command::Refresh => "refresh".to_string(),
            Command::Backends => "read the backends".to_string(),
            Command::RunLine(_) => "run a palette line".to_string(),
            Command::Approve(id) => format!("approve {id}"),
            Command::RequestApproval(plan) => format!("open an approval for {plan}"),
            Command::Reject(id) => format!("reject {id}"),
            Command::Execute(token) => format!("execute {}", token.plan_id()),
            Command::Message(_) => "ask the desk".to_string(),
            Command::StartWorkflow { template, .. } => format!("start {template}"),
            Command::DeskMode { data, book } => format!("point the desk at {data} · {book}"),
            Command::AlpacaLogin { .. } => "store the alpaca login".to_string(),
            Command::TestAlpaca => "test the alpaca login".to_string(),
            // What was asked for, never what was typed into it: the contact is
            // in this value and `names` is rendered into a log line and a
            // toast, which is the one place a `Debug` derive would have put it.
            Command::NewsSettings { verify, .. } => match verify {
                true => "check and save the news sources".to_string(),
                false => "save the news sources".to_string(),
            },
            Command::SetLlm { surface, .. } => format!("point {surface} at a model"),
            // The task, because that is what was approved. The template id is
            // the word an operator typed; the task is the row the owner
            // refuses or starts, and a refusal naming the other one cannot be
            // matched against the desk's own record of what happened.
            Command::ApproveAction(task) => format!("approve {task}"),
            Command::Actionables => "ask what the desk would do".to_string(),
            // One name for both answers. What failed is the act of recording
            // the desk's posture, and a refusal that read "leave this desk
            // read-only — refused" would be a sentence an operator has to
            // parse twice to see nothing changed.
            Command::Posture { .. } => "arm this desk".to_string(),
            // Named here because the match is over the whole type, exactly like
            // `Quit`. Neither reaches `perform`: the runtime hands the terminal
            // to a child and sends no request, so neither has an owner verb
            // that could refuse it.
            Command::OpenCli => "open the Claude CLI".to_string(),
            Command::OpenBuild(_) => "open Claude Code on this checkout".to_string(),
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
            Command::RequestApproval(plan) => match client.request_approval(&plan).await {
                Ok(approval_id) => Wrote::ApprovalOpened {
                    approval_id,
                    plan_id: plan,
                },
                Err(err) => Wrote::Failed {
                    what: format!("open an approval for {plan}"),
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
            // Not a governance decision either, and it books nothing: it
            // chooses which data the desk reads and which book it values
            // against. The owner is the authority on the pair — it refuses the
            // ones `DeskMode` cannot make — so whatever the operator typed is
            // sent whole and the owner's own words come back on a refusal.
            Command::DeskMode { data, book } => match client.desk_mode(&data, &book).await {
                // The owner answers with `desk_mode_payload()`, whose `label`
                // is the sentence *it* makes of the pair. A 200 without one is
                // a broken contract and says so rather than this client
                // inventing a receipt out of the two words it just sent.
                Ok(said) => match said.get("label").and_then(|v| v.as_str()) {
                    Some(label) if !label.is_empty() => Wrote::Pointed {
                        label: label.to_string(),
                        warning: credential_warning(&said, &book),
                    },
                    _ => Wrote::Failed {
                        what: format!("point the desk at {data} · {book}"),
                        said: format!("the owner answered without a label: {said}"),
                    },
                },
                Err(err) => Wrote::Failed {
                    what: format!("point the desk at {data} · {book}"),
                    said: err.to_string(),
                },
            },
            // A login is not a governance decision either, and it books
            // nothing: the owner writes the credential file and does not switch
            // the book. Three answers, and the middle one is a question — the
            // form is what puts it to the operator, so it travels back as its
            // own outcome rather than as a failure.
            Command::AlpacaLogin {
                key,
                secret,
                replace,
            } => {
                // `Some(true)` only. The flag is consent, so "not asked" must
                // not be spelled the same way on the wire as "answered no".
                let consented = replace.then_some(true);
                match client
                    .set_alpaca_credentials(&key, &secret, consented)
                    .await
                {
                    Ok(Login::Stored(said)) => match credentials_of(&said) {
                        Ok(note) => Wrote::LoggedIn { usable: true, note },
                        Err(note) => Wrote::LoggedIn {
                            usable: false,
                            note,
                        },
                    },
                    Ok(Login::ConsentNeeded(said)) => Wrote::LoginNeedsConsent { said },
                    Ok(Login::Rejected(said)) => Wrote::LoginRefused { said },
                    Err(err) => Wrote::Failed {
                        // Names the action and never the pair: `what` is
                        // rendered in a toast and in the form's own note.
                        what: "store the alpaca login".to_string(),
                        said: err.to_string(),
                    },
                }
            }
            // Not a governance decision either, and it books nothing: it
            // chooses which mind answers a question. The owner is the authority
            // on what its backends can serve — it refuses an unreachable daemon
            // and a model it does not hold — so what the operator named is sent
            // and the owner's own sentence comes back either way.
            Command::SetLlm { surface, choice } => {
                // Named before the call, because the outcome has to say what it
                // was about whichever way it goes. Model ids are inert — no
                // credential is nameable here — so this quotes what was sent,
                // unlike the login path.
                let what = match &choice {
                    ModelChoice::Pair { backend, model } => {
                        format!("point {surface} at {backend} {model}")
                    }
                    ModelChoice::Enabled(on) => {
                        format!("switch the {surface} {}", if *on { "on" } else { "off" })
                    }
                };
                let (pair, enabled) = match &choice {
                    ModelChoice::Pair { backend, model } => {
                        (Some((backend.as_str(), model.as_str())), None)
                    }
                    ModelChoice::Enabled(on) => (None, Some(*on)),
                };
                match client.set_llm(&surface, pair, enabled).await {
                    Ok(Choice::Chosen(said)) => Wrote::Chose { said },
                    // A considered no, not a broken request: the owner's 400
                    // carries the remedy, and rendering it as a failure would
                    // bury "start it with `ollama serve`" under a transport
                    // error nobody can act on.
                    Ok(Choice::Rejected(said)) => Wrote::ChoiceRefused { said },
                    Err(err) => Wrote::Failed {
                        what,
                        said: err.to_string(),
                    },
                }
            }
            // The desk's own posture, and the only write a window the desk has
            // not armed may send. What comes back is the owner's account of
            // what it now holds — a 200 that does not say is a broken contract
            // and says so, rather than this client reporting the arming it
            // asked for as the arming that happened.
            Command::Posture { armed } => match client.set_posture(armed).await {
                Ok(said) => match said.get("armed").and_then(|v| v.as_bool()) {
                    Some(armed) => Wrote::Armed { armed },
                    None => Wrote::Failed {
                        what: "arm this desk".to_string(),
                        said: format!("the owner answered without saying whether it armed: {said}"),
                    },
                },
                Err(err) => Wrote::Failed {
                    what: "arm this desk".to_string(),
                    said: err.to_string(),
                },
            },
            // Not a governance decision either, and it books nothing: it
            // chooses what the desk *reads*. The owner owns the catalog — it
            // refuses a name it does not know, a source it cannot reach and a
            // contact it cannot parse — so what the operator ticked is sent
            // whole and the owner's own sentence comes back either way.
            Command::NewsSettings {
                providers,
                contact,
                verify,
                offline,
            } => {
                let what = names(&Command::NewsSettings {
                    providers: providers.clone(),
                    contact: None,
                    verify,
                    offline,
                });
                match client
                    .set_news(
                        &providers,
                        contact.as_ref().map(crate::cmd::Contact::as_str),
                        verify,
                        offline,
                    )
                    .await
                {
                    Ok(News::Applied { stack, verified }) => Wrote::NewsSaved {
                        stack,
                        checked: verify,
                        verified,
                    },
                    // A considered no, not a broken request: the owner's 400
                    // carries the remedy, and rendering it as a failure would
                    // bury "edgar needs a contact" under a transport error
                    // nobody can act on.
                    Ok(News::Rejected(said)) => Wrote::NewsRefused { said },
                    Err(err) => Wrote::Failed {
                        what,
                        said: err.to_string(),
                    },
                }
            }
            Command::TestAlpaca => match client.test_alpaca().await {
                Ok(verdict) => Wrote::Tested {
                    ok: verdict.ok,
                    summary: verdict.summary,
                },
                Err(err) => Wrote::Failed {
                    what: "test the alpaca login".to_string(),
                    said: err.to_string(),
                },
            },
            // The operator asking what the desk would do. A write because it
            // is one — the owner mints a `proposal`-origin task per startable
            // template — and the only caller of the route that makes the WOULD
            // DO panel non-empty. Nothing is granted: the items come back
            // gate-checked and are checked again at approval, and this cannot
            // start any of them.
            //
            // The answer it reports is a pair of counts. The list itself lands
            // in the next snapshot, which the refetch below already asks for —
            // a second copy carried here would be two accounts of one answer.
            Command::Actionables => match client.actionables().await {
                Ok(Proposed { offered, refused }) => Wrote::Proposed { offered, refused },
                Err(err) => Wrote::Failed {
                    what: "ask what the desk would do".to_string(),
                    said: err.to_string(),
                },
            },
            // The operator approving one of today's proposals. Governance
            // still lives at the owner — the route re-runs `check_startable`,
            // and a plan-creating template is refused below `propose` — so the
            // three answers are reported as three things. A 200 that says
            // `started: false` is a *refusal*, not a start: reading one of
            // those as work in flight is exactly what shipped once on the
            // execution path, and the pipeline pane would then be waiting on a
            // run nobody began.
            Command::ApproveAction(task_id) => match client.start_task(&task_id).await {
                Ok(Start::Started {
                    template,
                    workflow_id,
                }) => Wrote::ProposalStarted {
                    task_id,
                    template,
                    workflow_id,
                },
                Ok(Start::Refused { blocked_by, reason }) => Wrote::ProposalRefused {
                    task_id,
                    blocked_by,
                    reason,
                },
                // Started and already over, inside one 200. A failure rather
                // than a refusal: nothing declined it, and there is nothing
                // for the operator to move the desk's mode about.
                Ok(Start::Failed(said)) => Wrote::Failed {
                    what: format!("approve {task_id}"),
                    said,
                },
                Err(err) => Wrote::Failed {
                    what: format!("approve {task_id}"),
                    said: err.to_string(),
                },
            },
            // The ones the runtime handles itself. They cannot arrive here
            // through the loop; the arm exists because the match is over the
            // whole type, and `Backends` is a *read* the poller serves — a
            // write outcome for it would put a row on the bus about a request
            // that changed nothing. The two hand-offs are here for the same
            // reason and a stronger one: their effect is a child process on the
            // operator's terminal, and there is no request for this seam to
            // make on their behalf at all.
            Command::Quit
            | Command::Refresh
            | Command::Backends
            | Command::RunLine(_)
            | Command::OpenCli
            | Command::OpenBuild(_) => return None,
        })
    }

    /// What the owner said about the credentials of the book it just accepted.
    ///
    /// Only for the real book: the simulated one needs no login, and a warning
    /// beside it would train an operator to read past the one that matters. The
    /// owner sets `credentials_ok` false both when there is no login and when
    /// there is an unusable one, and `credentials` is its description of which.
    ///
    /// An owner that does not say at all is warned about rather than assumed
    /// fine. `desk_mode_payload` always carries the flag, so its absence is a
    /// contract this client cannot read — and silence about the book that can
    /// place real orders is the one answer that must not pass as a clean
    /// switch. Invariant 4: refuse loudly rather than degrade quietly.
    fn credential_warning(said: &serde_json::Value, book: &str) -> Option<String> {
        if book != "alpaca" {
            return None;
        }
        credentials_of(said).err()
    }

    /// What a `desk_mode_payload()` says about the credentials behind it.
    ///
    /// `Ok` is the owner's description of a login it can read; `Err` is its
    /// description of one it cannot. One definition because two surfaces ask —
    /// the desk-mode switch, which warns about a book it cannot reach, and the
    /// login form, which reports what the desk made of what it just stored —
    /// and two copies of "what counts as a working login" is how the two come
    /// to disagree.
    ///
    /// An owner that does not say at all is an `Err`. `desk_mode_payload`
    /// always carries the flag, so its absence is a contract this client cannot
    /// read, and silence about the credential that reaches a real venue must
    /// not pass as a clean one. Invariant 4: refuse loudly rather than degrade
    /// quietly.
    ///
    /// The description is **bounded**, which the refusal path already was and
    /// this one was not. It is the 200 body rather than a 400, so C2 read it as
    /// the owner's own and safe; but nothing on this path is guaranteed to be
    /// the owner's — a proxy answering 200 with a page of its own reaches the
    /// same toast — and the rule is cheaper to keep uniform than to reason
    /// about per call site. `NOTE_MAX` is the toast's own room: two wrapped
    /// lines of a box that has to stay readable over whatever pane is under it.
    fn credentials_of(said: &serde_json::Value) -> Result<String, String> {
        const NOTE_MAX: usize = 160;
        let described = said
            .get("credentials")
            .and_then(|v| v.as_str())
            // `Some("")` is absent, as everywhere in this client.
            .filter(|said| !said.is_empty())
            .map(|said| crate::format::bounded(said, NOTE_MAX));
        match said.get("credentials_ok").and_then(|v| v.as_bool()) {
            Some(true) => {
                Ok(described.unwrap_or_else(|| "the owner reports a usable Alpaca login".into()))
            }
            Some(false) => Err(described
                .unwrap_or_else(|| "the owner reports no usable Alpaca credentials".into())),
            None => Err("the owner did not say whether the Alpaca credentials work".to_string()),
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn a_stored_login_reports_the_owners_description_bounded_like_every_other() {
            // The C2 residual: the 200 path rendered the description straight
            // into a toast. A 200 is not proof the answer came from the owner.
            let said = serde_json::json!({
                "credentials_ok": true,
                "credentials": "a ".repeat(400),
            });
            let note = credentials_of(&said).unwrap();
            assert!(note.ends_with('…'), "{note}");
            assert!(note.chars().count() <= 161, "{note}");
            // And the ordinary sentence is untouched — the bound is a ceiling,
            // not a reformatting.
            let said = serde_json::json!({
                "credentials_ok": false,
                "credentials": "no ALPACA_API_KEY_ID in the environment or .env",
            });
            assert_eq!(
                credentials_of(&said),
                Err("no ALPACA_API_KEY_ID in the environment or .env".to_string())
            );
        }
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
        _forced_glass: bool,
        _tx: crate::bus::Tx,
    ) -> Result<Self, std::convert::Infallible> {
        Ok(Self)
    }
}
