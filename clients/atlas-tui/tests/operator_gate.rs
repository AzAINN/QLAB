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
    // The artifact claim, structurally. `reqwest`'s POST builder is the only way
    // this crate can mutate the desk, and it may appear in exactly one file —
    // the one the feature gate can remove. A second call site anywhere else
    // would survive into the default build.
    assert_eq!(
        files_mentioning(r"\.post("),
        vec!["net/write.rs".to_string()],
        "the only POST call site in the crate is the gated write module"
    );
}

#[test]
fn no_view_or_widget_can_reach_the_writer() {
    // `ui/` renders and returns `Command`s; the runtime acts. A view holding a
    // `WriteClient` would put an order path behind a keystroke with no
    // composition root in between, which is the arrangement the confirm modal
    // exists to prevent. The modal itself is in `ui/` and knows nothing about
    // HTTP: it mints a token, and a token is not a request.
    let reachers = files_mentioning("WriteClient");
    let escaped: Vec<&String> = reachers.iter().filter(|f| f.starts_with("ui/")).collect();
    assert!(
        escaped.is_empty(),
        "nothing under ui/ may name the writer, found: {escaped:?}"
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
    use atlas::net::write::WriteClient;
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

        let owner = spawn_owner(200, r#"{"executed": true}"#);
        let client = WriteClient::new(&owner.base).unwrap();
        client.execute_plan(&token).await.unwrap();

        let seen = owner.only();
        assert_eq!(seen.method, "POST");
        assert_eq!(seen.path, "/api/plans/execute");
        let body: serde_json::Value = serde_json::from_str(&seen.body).unwrap();
        assert_eq!(body["human_confirmed"], serde_json::json!(true));
        assert_eq!(body["plan_id"], serde_json::json!("9661b0e88b4a669e"));
        assert_eq!(body["approval_id"], serde_json::json!("1a2b3c4d5e6f7081"));
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
    async fn a_refusal_is_reported_with_the_owners_own_words() {
        // Fail loud. The owner's refusals are the governance messages — "human_
        // confirmed=true is required", "approval is 'expired', not pending" — and
        // a client that swallowed them into a bare error would leave the operator
        // pressing the same key against a gate that already explained itself.
        let owner = spawn_owner(
            400,
            r#"{"error": "execution requires an approval_id: a bare human_confirmed flag cannot book a trade"}"#,
        );
        let client = WriteClient::new(&owner.base).unwrap();
        let err = client.approve("1a2b3c4d5e6f7081").await.unwrap_err();
        let said = err.to_string();
        assert!(said.contains("400"), "{said}");
        assert!(said.contains("cannot book a trade"), "{said}");
    }

    #[tokio::test]
    async fn an_owner_that_is_not_there_is_an_error_and_not_a_silent_success() {
        // Port 1 on loopback: nothing listens, and the connect fails fast.
        let client = WriteClient::new("http://127.0.0.1:1").unwrap();
        assert!(client.atlas_pause().await.is_err());
    }
}
