//! The operator posture: what the default build cannot do, and what the
//! operator build may only do against one named plan.
//!
//! Two legs, and both are part of "done":
//!
//! * `cargo test` — the glass build. The assertions here are about the
//!   *artifact*: there is no POST call site outside the gated module, and the
//!   gate is spelled the way the compiler reads it. CLAUDE.md's claim about this
//!   client is "read-only by construction — no order path … so invariant 3 holds
//!   there by absence", and absence is a property a test can only pin
//!   structurally.
//! * `cargo test --features operator` — the write half. Every method is checked
//!   against a canned owner on a loopback socket, so the paths and bodies are
//!   pinned against what `qlab/ui/server.py` actually dispatches on rather than
//!   against a hand-copied note that can rot.

/// Where the crate's source lives, for the structural pins below.
const SRC: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/src");

fn source(relative: &str) -> String {
    std::fs::read_to_string(format!("{SRC}/{relative}"))
        .unwrap_or_else(|err| panic!("could not read {relative}: {err}"))
}

/// Every file under `src` that mentions `needle`, as paths relative to `src`.
///
/// Asserting the exact list rather than "nothing found" also proves the search
/// ran: a grep that cannot read the tree returns no matches, which would
/// otherwise read as a clean crate. The same reasoning as
/// `theme::tests::no_hardcoded_rgb_outside_theme`, which this follows.
fn files_mentioning(needle: &str) -> Vec<String> {
    let out = std::process::Command::new("grep")
        .args(["-rl", "--include=*.rs", needle, SRC])
        .output()
        .expect("grep is available");
    let mut found: Vec<String> = String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|line| {
            line.trim_start_matches(SRC)
                .trim_start_matches('/')
                .to_string()
        })
        .collect();
    found.sort();
    found
}

// -- the gate, asserted in both legs ---------------------------------------

#[test]
fn the_write_half_is_gated_the_way_the_compiler_reads_it() {
    // The whole posture rests on these two lines. A `cfg` that named the wrong
    // feature, or a `pub mod` that escaped its attribute, would compile the
    // write half into the monitoring build — and nothing else in this suite
    // would notice, because every other assertion here is about behaviour that
    // only exists once the feature is on.
    assert!(
        source("net/mod.rs").contains("#[cfg(feature = \"operator\")]\npub mod write;"),
        "net/mod.rs must gate `write` on the operator feature, verbatim"
    );
    assert!(
        source("ui/widgets/mod.rs").contains("#[cfg(feature = \"operator\")]\npub mod confirm;"),
        "widgets/mod.rs must gate `confirm` on the operator feature, verbatim"
    );
}

#[test]
fn no_write_call_site_exists_outside_the_gated_module() {
    // The artifact claim, structurally. Every way `reqwest` can be asked to
    // mutate something, not just the one this crate happens to use today: a
    // `.put(` or a `.request(Method::DELETE, …)` added to a view would be just
    // as reachable in the default build, and pinning only `.post(` would have
    // watched the wrong door. Each verb may appear in exactly one file — the one
    // the feature gate can remove.
    for verb in [
        r"\.post(",
        r"\.put(",
        r"\.patch(",
        r"\.delete(",
        r"\.request(",
        "Method::",
    ] {
        let found = files_mentioning(verb);
        assert!(
            found.is_empty() || found == vec!["net/write.rs".to_string()],
            "`{verb}` may only appear in the gated write module, found: {found:?}"
        );
    }
    // And the one this crate does use must really be there, or the loop above is
    // asserting nothing: a grep that cannot read the tree returns no matches,
    // which would otherwise read as a clean crate.
    assert_eq!(
        files_mentioning(r"\.post("),
        vec!["net/write.rs".to_string()],
        "the POST call site is in the gated write module"
    );
}

#[test]
fn no_view_or_widget_can_reach_the_writer() {
    // `ui/` renders and returns `Command`s; the runtime acts. A view holding a
    // `WriteClient` would put an order path behind a keystroke with no
    // composition root in between, which is the arrangement the confirm modal
    // exists to prevent. The modal itself is in `ui/` and knows nothing about
    // HTTP: it mints a token, and a token is not a request.
    // Both the type's name and the module path that reaches it: `use
    // crate::net::write::*` or a fully-qualified call would import the writer
    // without ever spelling `WriteClient`, and the name check alone would miss
    // it entirely.
    let mut reachers = files_mentioning("WriteClient");
    reachers.extend(files_mentioning("net::write"));
    // `dispatch::Writes` is the third door, and it opened after this pin was
    // written: lifting the dispatch seam out of `main.rs` made `Writes` a public
    // library type, so a view could now hold the thing that *drives* the writer
    // without ever naming the writer. It carries a client and dispatches
    // arbitrary `Command`s, which is the same authority one layer up.
    // Both spellings, for the reason the writer needs both: a glob import would
    // bring the type in without the module path ever appearing.
    reachers.extend(files_mentioning("dispatch::Writes"));
    reachers.extend(files_mentioning(r"\bWrites\b"));
    let escaped: Vec<&String> = reachers.iter().filter(|f| f.starts_with("ui/")).collect();
    assert!(
        escaped.is_empty(),
        "nothing under ui/ may name the writer or its dispatcher, found: {escaped:?}"
    );
    // And the search really ran. A grep that cannot read the tree returns no
    // matches, which would read as a clean crate — the same reasoning the
    // `.post(` pin above states, and the reason this one is asserted at all:
    // `Writes` became a public library type when the dispatch seam was lifted
    // out of the binary, so there is now a door here to watch.
    assert!(
        reachers.iter().any(|f| f == "main.rs"),
        "the writer-grep found nothing at all: {reachers:?}"
    );
}

// -- the glass build --------------------------------------------------------

#[cfg(not(feature = "operator"))]
mod glass {
    /// A compile-time assertion, not a runtime one: if this leg were ever built
    /// with the feature on, the crate would not compile rather than reporting a
    /// failure after the fact. Which is the right severity — a "glass" build
    /// that turned out to carry the writer is not a failing test, it is a
    /// binary that should not exist.
    const _THIS_LEG_IS_GLASS: () = assert!(!cfg!(feature = "operator"));

    #[test]
    fn the_default_build_has_no_write_module_to_reach() {
        // `atlas::net::write` cannot be named in this leg — the module is not in
        // the crate — so what is left to check is that the file the gate removes
        // is really the file that holds the writer, and that it says why.
        let src = super::source("net/write.rs");
        assert!(
            src.contains("pub struct WriteClient"),
            "the gated module is the one that holds the writer"
        );
        assert!(
            src.contains("holds there by absence"),
            "the write module states the invariant its gate preserves"
        );
    }

    #[test]
    fn the_posture_a_glass_build_can_hold_is_glass_and_only_glass() {
        // One inhabitant, not one branch. `Posture::Operator` does not exist in
        // this build, so there is no value a bug could assign that would put the
        // amber word on the status line of a monitoring box.
        use atlas::store::Posture;
        assert_eq!(Posture::default(), Posture::Glass);
        assert_eq!(Posture::default().label(), "GLASS");
    }
}

// -- the operator build -----------------------------------------------------

#[cfg(feature = "operator")]
mod operator {
    use atlas::model::Snapshot;
    use atlas::net::write::{Execution, WriteClient};
    use atlas::store::Posture;
    use atlas::ui::widgets::confirm::Modal;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    /// One request the client made, as the owner saw it.
    #[derive(Debug, Clone, PartialEq, Eq)]
    struct Seen {
        method: String,
        path: String,
        body: String,
    }

    struct Owner {
        base: String,
        seen: Arc<Mutex<Vec<Seen>>>,
    }

    impl Owner {
        fn only(&self) -> Seen {
            let seen = self.seen.lock().unwrap();
            assert_eq!(seen.len(), 1, "expected exactly one request: {seen:?}");
            seen[0].clone()
        }
    }

    /// A canned owner that answers every request with `status` and `body`, and
    /// records the method, path, and body it was sent.
    ///
    /// A real socket rather than a mocked transport, for the reason
    /// `http_poll.rs` gives: the thing worth pinning is that the bytes an owner
    /// would actually dispatch on are the bytes this client writes. A faked
    /// `reqwest` layer would pin the fake.
    fn spawn_owner(status: u16, body: &'static str) -> Owner {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        let base = format!("http://{}", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&seen);

        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let Ok(peek) = stream.try_clone() else {
                    continue;
                };
                let mut reader = BufReader::new(peek);

                let mut request_line = String::new();
                if reader.read_line(&mut request_line).is_err() || request_line.is_empty() {
                    continue;
                }
                let mut parts = request_line.split_whitespace();
                let method = parts.next().unwrap_or("").to_string();
                let path = parts.next().unwrap_or("/").to_string();

                // Headers, then exactly as many body bytes as were announced. A
                // read to EOF would block: the client keeps the socket open for
                // the response it is waiting for.
                let mut length = 0usize;
                loop {
                    let mut header = String::new();
                    match reader.read_line(&mut header) {
                        Ok(0) | Err(_) => break,
                        Ok(_) if header == "\r\n" => break,
                        Ok(_) => {
                            let lower = header.to_ascii_lowercase();
                            if let Some(value) = lower.strip_prefix("content-length:") {
                                length = value.trim().parse().unwrap_or(0);
                            }
                        }
                    }
                }
                let mut buf = vec![0u8; length];
                if length > 0 && reader.read_exact(&mut buf).is_err() {
                    continue;
                }
                recorded.lock().unwrap().push(Seen {
                    method,
                    path,
                    body: String::from_utf8_lossy(&buf).to_string(),
                });

                let response = format!(
                    "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\n\
                     content-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes());
                let _ = stream.flush();
            }
        });

        Owner { base, seen }
    }

    fn snapshot() -> Snapshot {
        serde_json::from_str(include_str!("fixtures/tui_snapshot.json")).unwrap()
    }

    /// The fixture's checked plan and the approval that covers it.
    fn checked_plan() -> (atlas::model::Plan, atlas::model::Approval) {
        let snap = snapshot();
        (snap.plans[0].clone(), snap.approvals[0].clone())
    }

    // -- the posture ------------------------------------------------------

    #[test]
    fn an_operator_build_can_say_either_word() {
        assert_eq!(
            Posture::default(),
            Posture::Glass,
            "the flag arms it, not the feature"
        );
        assert_eq!(Posture::Operator.label(), "OPERATOR");
        assert_eq!(Posture::Glass.label(), "GLASS");
    }

    #[test]
    fn the_status_line_says_operator_only_once_the_flag_armed_it() {
        // The chip is the operator's one continuous answer to "can this window
        // place an order". A featured build that was not armed must still read
        // GLASS, or the word means "which binary" instead of "what can happen
        // next".
        use atlas::store::Store;
        let mut store = Store::default();
        assert!(frame(&store).contains("GLASS"));
        store.posture = Posture::Operator;
        let armed = frame(&store);
        assert!(armed.contains("OPERATOR"), "{armed}");
        assert!(!armed.contains("GLASS"), "{armed}");
    }

    fn frame(store: &atlas::store::Store) -> String {
        use atlas::fx::Fx;
        use atlas::ui::views::Views;
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        let views = Views::new();
        let fx = Fx::default();
        let now = std::time::Instant::now();
        term.draw(|f| atlas::ui::shell::draw(f, store, &views, &fx, now))
            .unwrap();
        term.backend()
            .buffer()
            .content()
            .chunks(120)
            .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }

    // -- the modal contract -----------------------------------------------

    #[test]
    fn a_plan_modal_challenges_with_the_last_six_of_the_owners_targets_hash() {
        // The binding that makes this more than a keystroke. The referee's PASS
        // is bound to the exact `targets_hash`; so is the human's confirmation,
        // and the six characters are only ever on screen inside the modal — a
        // scripted replay of "y" cannot produce them, and a replay captured
        // against yesterday's plan produces the wrong six.
        let (plan, approval) = checked_plan();
        let modal = Modal::for_plan(&plan, &approval).expect("the approval covers the plan");
        assert_eq!(approval.targets_hash.as_deref(), Some("c4d5e6f708192a3b"));
        assert_eq!(modal.challenge(), "192a3b");
    }

    #[test]
    fn nothing_short_of_the_exact_challenge_mints_a_token() {
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        assert!(modal.token().is_none(), "an untouched modal is not armed");

        for c in "192a3".chars() {
            modal.push(c);
        }
        assert!(modal.token().is_none(), "a prefix is not the challenge");

        modal.push('X');
        assert!(
            modal.token().is_none(),
            "a wrong character is not the challenge"
        );
        modal.backspace();
        modal.push('b');

        let token = modal.token().expect("the exact challenge arms the modal");
        assert_eq!(token.plan_id(), "9661b0e88b4a669e");
        assert_eq!(token.approval_id(), "1a2b3c4d5e6f7081");
        assert_eq!(token.targets_hash(), "c4d5e6f708192a3b");
    }

    #[test]
    fn one_accepted_modal_yields_exactly_one_confirmation() {
        // Single use has to sit on the *consent*, not on the token. `token()`
        // minting on `&self` meant `loop { c.execute_plan(m.token().unwrap()) }`
        // compiled: the human confirmed once and the client could book any
        // number of times. Minting now spends the modal, so a second attempt
        // gets nothing to send.
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        for c in "192a3b".chars() {
            modal.push(c);
        }
        assert!(
            modal.token().is_some(),
            "the first mint is the confirmation"
        );
        assert!(
            modal.token().is_none(),
            "a spent modal must not mint a second confirmation"
        );
        // And it stays spent: retyping the challenge into a modal whose consent
        // was already used must not re-arm it.
        modal.backspace();
        modal.push('b');
        assert!(modal.token().is_none(), "a spent modal cannot be re-armed");
    }

    #[test]
    fn the_modal_shows_the_leg_count_the_owners_gate_will_check() {
        // The owner's `execute_plan_with_approval` takes `expected_legs` from
        // `stored["pre_trade"]["n_legs"]` (`server.py:1895`) and refuses the
        // plan if the persisted legs disagree. The approval's `summary` is a
        // different number written at a different time — in this fixture it says
        // 7 while the plan really has 2 — so showing the summary asked a human
        // to vouch for a seven-leg trade that the gate would evaluate as two.
        // The box must state what the gate will check.
        let (plan, approval) = checked_plan();
        assert_eq!(
            approval.summary.as_ref().unwrap()["n_legs"],
            serde_json::json!(7),
            "fixture guard: the approval summary must disagree, or this proves nothing"
        );
        assert_eq!(
            plan.pre_trade.as_ref().unwrap()["n_legs"],
            serde_json::json!(2)
        );

        let modal = Modal::for_plan(&plan, &approval).unwrap();
        let shown = modal.facts();
        let legs = shown
            .iter()
            .find(|(label, _)| label == "legs")
            .map(|(_, value)| value.clone());
        assert_eq!(legs, Some("2".to_string()), "shown facts: {shown:?}");

        // The hash still comes from the approval — that is the fact the approval
        // genuinely owns, and the one the referee's PASS is bound to.
        let hash = shown.iter().find(|(label, _)| label == "targets hash");
        assert_eq!(
            hash.map(|(_, v)| v.as_str()),
            Some("c4d5e6f708192a3b"),
            "shown facts: {shown:?}"
        );
    }

    #[test]
    fn an_approval_for_another_plan_cannot_be_used_to_confirm_this_one() {
        // The join is the governance-critical half. Binding the modal to an
        // approval that covers a *different* plan would show six characters that
        // arm an execution the human never reviewed — the exact substitution
        // `targets_hash` exists to prevent, reintroduced at the client.
        let (plan, mut approval) = checked_plan();
        approval.plan_id = Some("0000000000000000".into());
        assert!(Modal::for_plan(&plan, &approval).is_none());
    }

    #[test]
    fn a_plan_with_no_owner_computed_hash_cannot_be_confirmed_at_all() {
        // Refuse, never substitute. Falling back to the plan id would put a
        // six-character challenge on screen that binds to nothing the referee
        // ever checked — a confirmation ritual with no content, which is worse
        // than no ritual.
        let (plan, mut approval) = checked_plan();
        approval.targets_hash = None;
        assert!(Modal::for_plan(&plan, &approval).is_none());
        approval.targets_hash = Some("short".into());
        assert!(Modal::for_plan(&plan, &approval).is_none());
    }

    #[test]
    fn an_action_modal_challenges_with_the_static_word() {
        // Atlas's mode has no plan to bind to, so there is no hash to echo. It
        // still takes a typed word: the point is that the keystroke that changes
        // what Atlas may do is never one key away.
        let mut modal = Modal::action("SET ATLAS MODE", vec![("mode".into(), "propose".into())]);
        assert_eq!(modal.challenge(), "CONFIRM");
        for c in "CONFIRM".chars() {
            modal.push(c);
        }
        assert!(modal.armed());
        // An action modal binds no plan, so it can never mint the capability
        // that reaches `execute_plan`.
        assert!(
            modal.token().is_none(),
            "an action modal is not a plan token"
        );
    }

    #[test]
    fn the_modal_is_fifty_by_twelve_and_shows_the_facts_it_binds() {
        let (plan, approval) = checked_plan();
        let modal = Modal::for_plan(&plan, &approval).unwrap();
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        term.draw(|f| modal.draw(f, f.area())).unwrap();
        let text: Vec<String> = term
            .backend()
            .buffer()
            .content()
            .chunks(120)
            .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
            .collect();
        let painted: Vec<usize> = text
            .iter()
            .enumerate()
            .filter(|(_, row)| row.trim().len() > 1)
            .map(|(i, _)| i)
            .collect();
        assert_eq!(painted.len(), 12, "the modal is twelve rows: {painted:?}");
        let body = text.join("\n");
        assert!(
            body.contains("9661b0e88b4a669e"),
            "the plan id is a fact: {body}"
        );
        assert!(
            body.contains("c4d5e6f708192a3b"),
            "the hash is a fact: {body}"
        );
        // The challenge is shown only here — that is what makes typing it proof
        // the human looked at this plan.
        assert!(
            body.contains("192a3b"),
            "the challenge is in the modal: {body}"
        );
    }

    // -- the writer -------------------------------------------------------

    #[tokio::test]
    async fn approve_and_reject_hit_the_owners_two_verbs() {
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        client.approve("1a2b3c4d5e6f7081").await.unwrap();
        assert_eq!(
            owner.only(),
            Seen {
                method: "POST".into(),
                path: "/api/approvals/1a2b3c4d5e6f7081/approve".into(),
                body: "{}".into(),
            }
        );

        let owner = spawn_owner(200, r#"{"status": "rejected"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        client.reject("1a2b3c4d5e6f7081").await.unwrap();
        assert_eq!(owner.only().path, "/api/approvals/1a2b3c4d5e6f7081/reject");
    }

    #[tokio::test]
    async fn execution_carries_the_confirmation_the_owner_refuses_to_book_without() {
        // The owner returns 400 unless the body carries `human_confirmed: true`
        // *and* an `approval_id`: "a bare human_confirmed flag cannot book a
        // trade". Both come from the token, so neither can be supplied by a
        // caller that never opened the modal.
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        for c in "192a3b".chars() {
            modal.push(c);
        }
        let token = modal.token().unwrap();

        let owner = spawn_owner(200, r#"{"executed": true, "filled": 2}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        let outcome = client.execute_plan(token).await.unwrap();
        assert!(matches!(outcome, Execution::Executed(_)), "{outcome:?}");

        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/plans/execute");
        let body: serde_json::Value = serde_json::from_str(&seen.body).unwrap();
        assert_eq!(body["human_confirmed"], serde_json::json!(true));
        assert_eq!(body["plan_id"], serde_json::json!("9661b0e88b4a669e"));
        assert_eq!(body["approval_id"], serde_json::json!("1a2b3c4d5e6f7081"));
    }

    /// An armed token, for the outcome tests below.
    fn armed_token() -> atlas::ui::widgets::confirm::ConfirmToken {
        let (plan, approval) = checked_plan();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        for c in "192a3b".chars() {
            modal.push(c);
        }
        modal.token().unwrap()
    }

    #[tokio::test]
    async fn a_refused_fill_is_not_reported_as_a_booked_one() {
        // The bug this pins: the execution gate declines with **HTTP 200** and
        // `executed: false` — `server.py:2629` returns `200, result` whatever
        // the result, and the handler comment at :2613 says so. A client that
        // only errored on non-2xx therefore reported every governance refusal
        // as a successful fill, which is the single worst thing this surface
        // could tell an operator.
        //
        // Refusal is a third outcome: not an `Err` (the desk answered, and the
        // answer is legitimate) and emphatically not an `Ok(Executed)`.
        let owner = spawn_owner(
            200,
            r#"{"executed": false, "blocked_by": "approval",
                "reasons": ["approval has expired", "book moved since approval (revision mismatch)"]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match client.execute_plan(armed_token()).await.unwrap() {
            Execution::Refused {
                blocked_by,
                reasons,
            } => {
                assert_eq!(blocked_by, "approval");
                assert_eq!(reasons.len(), 2);
                assert!(reasons[0].contains("expired"), "{reasons:?}");
            }
            other => panic!("a refused fill must not read as booked: {other:?}"),
        }
    }

    #[tokio::test]
    async fn every_shape_the_gate_declines_with_is_a_refusal_and_none_is_a_fill() {
        // Four `executed: false` shapes exist in `server.py`, and one of them
        // carries **no** `blocked_by` at all (`:1909`, the mandate violation).
        // A client that keyed on that field alone would fall through to
        // "success" on exactly the refusal that means the plan broke the
        // mandate.
        let cases: Vec<(&'static str, &str)> = vec![
            (
                r#"{"executed": false, "blocked_by": "approval", "reasons": ["no approval record"]}"#,
                "approval",
            ),
            (
                r#"{"executed": false, "blocked_by": "data_revalidation", "data_health": {"blocked": true}}"#,
                "data_revalidation",
            ),
            (
                r#"{"executed": false, "mandate_violation": "position cap breached"}"#,
                "mandate_violation",
            ),
        ];
        for (body, want) in cases {
            let owner = spawn_owner(200, body);
            let client = WriteClient::new(&owner.base).unwrap();
            match client.execute_plan(armed_token()).await.unwrap() {
                Execution::Refused {
                    blocked_by,
                    reasons,
                } => {
                    assert_eq!(blocked_by, want, "{body}");
                    // Never silently empty: an operator told "refused" with no
                    // reason cannot act, and the mandate shape's reason is in a
                    // different key than the approval shape's.
                    assert!(!reasons.is_empty(), "a refusal must say why: {body}");
                }
                other => panic!("{body} must be a refusal, got {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn a_body_that_does_not_say_whether_it_executed_is_refused_not_assumed() {
        // The owner always sets `executed` on this route. A 200 without it is a
        // broken contract, and guessing either way is indefensible — one guess
        // invents a fill, the other hides one.
        let owner = spawn_owner(200, r#"{"approval_id": "1a2b3c4d5e6f7081"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert!(client.execute_plan(armed_token()).await.is_err());
    }

    #[tokio::test]
    async fn every_atlas_and_desk_verb_lands_on_the_route_the_owner_dispatches_on() {
        // One table rather than nine tests: the value is that each pair matches
        // `qlab/ui/server.py`'s dispatch, and a table makes a missing route
        // obvious rather than a missing test.
        let cases: Vec<(&str, &str, serde_json::Value)> = vec![
            (
                "/api/atlas/mode",
                "mode",
                serde_json::json!({"mode": "propose"}),
            ),
            ("/api/atlas/pause", "pause", serde_json::json!({})),
            (
                "/api/atlas/resume",
                "resume",
                serde_json::json!({"mode": "observe"}),
            ),
            (
                "/api/atlas/autonomy",
                "autonomy",
                serde_json::json!({"enabled": true}),
            ),
            (
                "/api/atlas/message",
                "message",
                serde_json::json!({"text": "why flat?"}),
            ),
            (
                "/api/workforce/fast",
                "fast",
                serde_json::json!({"enabled": false}),
            ),
            (
                "/api/desk_mode",
                "desk",
                serde_json::json!({"data": "synthetic", "book": "simulated_paper"}),
            ),
            (
                "/api/workflows/start",
                "workflow",
                serde_json::json!({"kind": "portfolio_review", "goal": "review the book"}),
            ),
        ];

        for (path, which, want) in cases {
            let owner = spawn_owner(200, r#"{"ok": true}"#);
            let client = WriteClient::new(&owner.base).unwrap();
            match which {
                "mode" => client.atlas_mode("propose").await.unwrap(),
                "pause" => client.atlas_pause().await.unwrap(),
                "resume" => client.atlas_resume("observe").await.unwrap(),
                "autonomy" => client.atlas_autonomy(true).await.unwrap(),
                "message" => client.atlas_message("why flat?").await.unwrap(),
                "fast" => client.workforce_fast(false).await.unwrap(),
                "desk" => client
                    .desk_mode("synthetic", "simulated_paper")
                    .await
                    .unwrap(),
                "workflow" => client
                    .start_workflow("portfolio_review", "review the book")
                    .await
                    .unwrap(),
                other => panic!("untested verb {other}"),
            };
            let seen = owner.only();
            assert_eq!(seen.method, "POST", "{path}");
            assert_eq!(seen.path, path);
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
                want,
                "{path}"
            );
        }
    }

    #[tokio::test]
    async fn a_non_2xx_is_reported_with_the_owners_own_words() {
        // A real 400 from a route that really returns one: `decide_approval`
        // raises `PermissionError("approval is 'rejected', not pending")`, which
        // the dispatcher turns into a 400. The previous version of this test
        // fired execute-gate text at `approve()` — a refusal that route cannot
        // produce — which made the suite look like it covered the execution gate
        // while the gate's actual 200-shaped refusals went unchecked.
        let owner = spawn_owner(400, r#"{"error": "approval is 'rejected', not pending"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        let err = client.approve("1a2b3c4d5e6f7081").await.unwrap_err();
        let said = err.to_string();
        assert!(said.contains("400"), "{said}");
        assert!(said.contains("not pending"), "{said}");
    }

    #[tokio::test]
    async fn an_owner_that_is_not_there_is_an_error_and_not_a_silent_success() {
        // Port 1 on loopback: nothing listens, and the connect fails fast.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        assert!(client.atlas_pause().await.is_err());
    }

    // -- the dispatch seam -------------------------------------------------
    //
    // What turns a confirmed `Command` into a request, and what a write outcome
    // owes the poller. This lived in `main.rs`, where nothing could reach it:
    // the routing that decides which owner verb a keystroke lands on, and the
    // predicate that decides whether a *failed* write refreshes the desk, both
    // shipped with no test at all. Invariant 10, one layer above the seams it
    // usually catches.

    use atlas::bus::{AppEvent, Wrote};
    use atlas::cmd::Command;
    use atlas::dispatch::{perform, refetches, Writes};

    /// An armed token for the fixture plan, minted the only way one can be.
    fn token() -> atlas::ui::widgets::confirm::ConfirmToken {
        armed_token()
    }

    #[tokio::test]
    async fn each_write_command_lands_on_the_owner_verb_it_names() {
        // The routing itself. A `Reject` that reached `/approve` would be an
        // operator's refusal recorded as consent, and nothing downstream could
        // tell — both answer 200 with a status the client does not re-read.
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Approve("1a2b3c4d5e6f7081".into())).await,
            Some(Wrote::Decided {
                approval_id: "1a2b3c4d5e6f7081".into(),
                decision: "approved",
            })
        );
        assert_eq!(owner.only().path, "/api/approvals/1a2b3c4d5e6f7081/approve");

        let owner = spawn_owner(200, r#"{"status": "rejected"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Reject("1a2b3c4d5e6f7081".into())).await,
            Some(Wrote::Decided {
                approval_id: "1a2b3c4d5e6f7081".into(),
                decision: "rejected",
            })
        );
        assert_eq!(owner.only().path, "/api/approvals/1a2b3c4d5e6f7081/reject");

        let owner = spawn_owner(200, r#"{"executed": true, "n_fills": 2}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Execute(token())).await,
            Some(Wrote::Executed {
                plan_id: "9661b0e88b4a669e".into(),
            })
        );
        let seen = owner.only();
        assert_eq!(seen.path, "/api/plans/execute");
        let body: serde_json::Value = serde_json::from_str(&seen.body).unwrap();
        assert_eq!(body["human_confirmed"], serde_json::json!(true));
        assert_eq!(body["approval_id"], serde_json::json!("1a2b3c4d5e6f7081"));
    }

    #[tokio::test]
    async fn a_gate_refusal_survives_the_seam_as_a_refusal() {
        // The 200-shaped decline, carried through the dispatch layer with the
        // owner's own words intact. Folded into `Executed` here it would reach
        // the toast and the card as a booked fill.
        let owner = spawn_owner(
            200,
            r#"{"executed": false, "blocked_by": "approval",
                "reasons": ["approval has expired"]}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(&client, Command::Execute(token())).await,
            Some(Wrote::Refused {
                plan_id: "9661b0e88b4a669e".into(),
                blocked_by: "approval".into(),
                reasons: vec!["approval has expired".into()],
            })
        );
    }

    #[tokio::test]
    async fn a_request_that_never_landed_names_what_it_was_and_what_was_said() {
        // Port 1: nothing listens. The outcome has to name the plan it was
        // about — an audit trail ending at "a write failed" cannot be matched
        // to the key that was pressed.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        match perform(&client, Command::Execute(token())).await {
            Some(Wrote::Failed { what, said }) => {
                assert!(what.contains("9661b0e88b4a669e"), "{what}");
                assert!(!said.is_empty(), "a failure must carry the reason");
            }
            other => panic!("an unreachable owner is a failure, got {other:?}"),
        }
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        match perform(&client, Command::Approve("a1".into())).await {
            Some(Wrote::Failed { what, .. }) => assert!(what.contains("a1"), "{what}"),
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn a_question_carries_the_owners_own_answer_about_whether_it_can_be_heard() {
        // `atlas_message` answers **200** whether or not a coordinator exists to
        // read the question — "coordinator unavailable; Atlas is degraded and
        // cannot answer" is a 200 with `received: true`. A seam that reported
        // the status code would tell an operator their question was asked of
        // something that cannot hear it, which is the same class of failure as
        // reading a 200-shaped execution refusal as a fill.
        let owner = spawn_owner(
            200,
            r#"{"received": true, "coordinator_available": false,
                "note": "coordinator unavailable; Atlas is degraded and cannot answer"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(&client, Command::Message("why are we flat?".into())).await {
            Some(Wrote::Asked { note }) => assert!(note.contains("degraded"), "{note}"),
            other => panic!("a question must carry the owner's note: {other:?}"),
        }
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/atlas/message");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"text": "why are we flat?"})
        );
    }

    #[tokio::test]
    async fn starting_a_run_names_the_template_and_reports_the_owners_own_handle() {
        let owner = spawn_owner(
            200,
            r#"{"workflow_id": "805e0729cfec4d67", "kind": "regime_review", "status": "running"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        assert_eq!(
            perform(
                &client,
                Command::StartWorkflow {
                    template: "regime_review".into(),
                    goal: "check the drift".into(),
                }
            )
            .await,
            Some(Wrote::Started {
                template: "regime_review".into(),
                workflow_id: "805e0729cfec4d67".into(),
            })
        );
        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/workflows/start");
        // `kind` and `goal` only. The owner reads `as_of`, `universe` and
        // `offline` too and defaults all three, and it refuses to take a phase
        // graph from a network caller at all — sending less is the narrower
        // surface, and the picker says so on screen.
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&seen.body).unwrap(),
            serde_json::json!({"kind": "regime_review", "goal": "check the drift"})
        );
    }

    #[tokio::test]
    async fn a_start_the_owner_did_not_hand_back_a_handle_for_is_a_failure() {
        // The owner answers with the workflow row it created. A 200 without a
        // `workflow_id` is a broken contract, and inventing a handle would put
        // a run on screen that an operator could never find in the registry —
        // the owner's own dispatch path refuses the same shape for the same
        // reason ("returning a handle with workflow_id=None is how a failed
        // dispatch used to be recorded as a completed task").
        let owner = spawn_owner(200, r#"{"status": "running"}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        match perform(
            &client,
            Command::StartWorkflow {
                template: "regime_review".into(),
                goal: "check the drift".into(),
            },
        )
        .await
        {
            Some(Wrote::Failed { what, said }) => {
                assert!(what.contains("regime_review"), "{what}");
                assert!(said.contains("without a workflow_id"), "{said}");
            }
            other => panic!("a start with no handle must not read as started: {other:?}"),
        }
    }

    #[tokio::test]
    async fn the_two_commands_the_runtime_handles_itself_send_nothing() {
        // A stray `Quit` reaching the writer must not put a meaningless row on
        // the bus — every `Wrote` raises a toast and refetches the desk.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        assert_eq!(perform(&client, Command::Quit).await, None);
        assert_eq!(perform(&client, Command::Refresh).await, None);
    }

    #[tokio::test]
    async fn a_dispatched_command_puts_its_outcome_on_the_bus() {
        // The other half of the seam: the runtime never awaits a write, so an
        // outcome that never reached the channel would be a key an operator
        // pressed and heard nothing back from.
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, Posture::Operator, tx).unwrap();
        assert!(writes.armed());
        writes.dispatch(Command::Approve("1a2b3c4d5e6f7081".into()));
        match rx.recv().await {
            Some(AppEvent::Wrote(Wrote::Decided { decision, .. })) => {
                assert_eq!(decision, "approved")
            }
            other => panic!("the outcome never reached the bus: {:?}", other.is_some()),
        }
    }

    #[tokio::test]
    async fn a_window_the_human_did_not_arm_holds_no_writer_and_sends_nothing() {
        // The flag arms the build, not the feature. A featured binary started
        // without `--operator` reads GLASS on the status line, and the runtime
        // has to agree with it rather than merely the renderer.
        let owner = spawn_owner(200, r#"{"status": "approved"}"#);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let writes = Writes::new(&owner.base, Posture::Glass, tx).unwrap();
        assert!(!writes.armed());
        writes.dispatch(Command::Approve("1a2b3c4d5e6f7081".into()));
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        assert!(
            rx.try_recv().is_err(),
            "an unarmed window wrote to the desk"
        );
        assert!(
            owner.seen.lock().unwrap().is_empty(),
            "an unarmed window reached the owner"
        );
    }

    #[test]
    fn every_write_outcome_brings_the_next_poll_forward_failures_included() {
        // The rule this pins is the counter-intuitive one. A refusal moved the
        // registry — the gate invalidates the approval it declined — and a
        // *failure* is the outcome where the desk's state is least knowable,
        // because the write shares the poller's timeout and a request that gave
        // up may still be booking. Suppressing the refetch there kept the least
        // trustworthy frame on screen at the moment an operator is most likely
        // to press the key again.
        for outcome in [
            Wrote::Executed {
                plan_id: "p1".into(),
            },
            Wrote::Refused {
                plan_id: "p1".into(),
                blocked_by: "approval".into(),
                reasons: vec!["expired".into()],
            },
            Wrote::Decided {
                approval_id: "a1".into(),
                decision: "approved",
            },
            Wrote::Failed {
                what: "execute p1".into(),
                said: "the owner did not answer".into(),
            },
            // Both of the workforce verbs move the registry too: a message is
            // recorded as an audit event, and a start writes a workflow and its
            // whole phase graph. A frame that waited out the poll interval
            // would show neither, which reads as a key that did nothing.
            Wrote::Asked {
                note: "queued for the interpreting agent".into(),
            },
            Wrote::Started {
                template: "regime_review".into(),
                workflow_id: "805e0729cfec4d67".into(),
            },
        ] {
            assert!(
                refetches(&AppEvent::Wrote(outcome.clone())),
                "{outcome:?} must bring the next poll forward"
            );
        }
    }

    #[test]
    fn nothing_but_a_write_outcome_refetches_from_here() {
        // The stream already nudges the poller for the durable kinds
        // (`net::sse::REFETCH_KINDS`), and a second rule for the same events in
        // a second place is how the two come to disagree.
        assert!(!refetches(&AppEvent::Tick));
        assert!(!refetches(&AppEvent::Resize));
        assert!(!refetches(&AppEvent::ConnUp(atlas::bus::Channel::Owner)));
        assert!(!refetches(&AppEvent::Sse(atlas::bus::SseEvent {
            kind: "plan_executed".into(),
            payload: serde_json::json!({}),
            ts: None,
            id: None,
        })));
        assert!(!refetches(&AppEvent::Snapshot(Box::new(snapshot()))));
    }
}
