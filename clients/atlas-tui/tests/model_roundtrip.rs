//! The owner's payload shape, pinned against a captured fixture.
//!
//! Fixtures only — a client test that reached the owner would pass or fail on
//! whatever the desk happened to be holding, and would not run offline.

use atlas::model::{LlmCatalog, RegimePanel, Snapshot};

fn snapshot() -> Snapshot {
    serde_json::from_str(include_str!("fixtures/tui_snapshot.json")).unwrap()
}

#[test]
fn the_catalog_is_the_shape_the_backends_route_actually_serves() {
    // Captured from a live worktree owner, so the model lists are the owner's
    // own vocabulary rather than a guess: `CLAUDE_MODELS` is the routing
    // vocabulary (`inherit` first, and it is what "the tiers decide" is
    // spelled), and ollama reports whatever is pulled.
    let catalog: LlmCatalog =
        serde_json::from_str(include_str!("fixtures/llm_backends.json")).unwrap();
    assert_eq!(catalog.backends.len(), 2);
    assert_eq!(catalog.backends[0].name.as_deref(), Some("claude"));
    assert_eq!(catalog.backends[0].available, Some(true));
    assert_eq!(catalog.backends[0].models[0], "inherit");
    assert_eq!(catalog.backends[1].models, vec!["qwen2.5:7b"]);
    assert!(catalog.probed_at.is_some());

    // A backend that cannot serve is asked for no list at all, so `models` is
    // absent-or-empty on exactly the entries whose reason matters most — and
    // `null` may not reject the payload the strip is drawn from.
    let down: LlmCatalog = serde_json::from_str(
        r#"{"backends": [{"name": "ollama", "available": false,
                          "reason": "ollama is not running at http://127.0.0.1:11499 — start it with `ollama serve`",
                          "models": null}], "probed_at": null}"#,
    )
    .unwrap();
    assert!(down.backends[0].models.is_empty());
    assert_eq!(down.probed_at, None);
    let empty: LlmCatalog = serde_json::from_str(r#"{"backends": null}"#).unwrap();
    assert!(empty.backends.is_empty());
}

#[test]
fn snapshot_fixture_deserializes_and_regime_is_nested_under_market() {
    let s = snapshot();
    assert!(
        s.market.as_ref().unwrap().regime.is_some(),
        "regime lives under market — the old client read it top-level and always showed unknown"
    );
    assert!(!s.market.unwrap().assets[0].history.is_empty());
}

#[test]
fn every_modeled_section_is_present_in_the_fixture() {
    let s = snapshot();
    assert!(s.portfolio.is_some(), "portfolio");
    assert!(s.live_portfolio.is_some(), "live_portfolio");
    assert!(s.market.is_some(), "market");
    assert!(s.stress.is_some(), "stress");
    assert!(s.performance.is_some(), "performance");
    assert!(s.quotes.is_some(), "quotes");
    assert!(s.atlas.is_some(), "atlas");
    assert!(s.atlas_heartbeat.is_some(), "atlas_heartbeat");
    assert!(s.atlas_read.is_some(), "atlas_read");
    assert!(!s.approvals.is_empty(), "approvals");
    assert!(!s.plans.is_empty(), "plans");
    assert!(!s.orders.is_empty(), "orders");
    assert!(!s.workflows.is_empty(), "workflows");
    assert!(!s.events.is_empty(), "events");
    assert!(s.desk_mode.is_some(), "desk_mode");
    assert!(s.policy.is_some(), "policy");
    assert!(s.system.is_some(), "system");
    assert!(s.llm.is_some(), "llm");
    assert!(!s.leaderboard.is_empty(), "leaderboard");
    assert!(!s.runs.is_empty(), "runs");
    assert!(!s.algorithms.is_empty(), "algorithms");
    assert!(!s.atlas_chat.is_empty(), "atlas_chat");
    assert!(s.predictors.is_some(), "predictors");
    assert!(s.actionables.is_some(), "actionables");
}

#[test]
fn the_chat_and_the_board_decode_the_shapes_the_owner_serves() {
    let s = snapshot();
    // The conversation, oldest first, with both voices and the failure shape.
    let chat = &s.atlas_chat;
    let actor = |i: usize| {
        chat[i]
            .payload
            .as_ref()
            .and_then(|p| p.get("actor"))
            .and_then(|a| a.as_str())
    };
    assert_eq!(actor(0), Some("operator"));
    assert_eq!(actor(1), Some("atlas"));
    assert!(
        chat.last()
            .and_then(|e| e.payload.as_ref())
            .and_then(|p| p.get("error"))
            .is_some(),
        "the failed answer lost its error"
    );

    // The board: the champion's metrics arrive whole, per-fold included, and
    // the tri-state null verdict stays a tri-state.
    let board = s.predictors.as_ref().unwrap();
    assert_eq!(board.status.as_deref(), Some("ok"));
    let champion = board.champion.as_ref().unwrap();
    assert_eq!(champion.variant.as_deref(), Some("quantum_gram"));
    assert_eq!(champion.mean_ic, Some(0.1412));
    assert_eq!(champion.per_fold.len(), 5);
    assert_eq!(board.champion_established, Some(false));
    // A payload the owner serves for a desk with no board yet must not reject
    // the whole snapshot, and its absent metrics must stay absent.
    let bare: Snapshot =
        serde_json::from_str(r#"{"predictors": {"status": "never_ran"}}"#).unwrap();
    let bare_board = bare.predictors.unwrap();
    assert_eq!(bare_board.status.as_deref(), Some("never_ran"));
    assert!(bare_board.champion.is_none());
    assert!(bare_board.age_days.is_none(), "missing age is not day zero");
}

#[test]
fn the_actionables_block_keeps_its_verdict_three_valued() {
    // `atlas_actionables_snapshot` serves `false` (known refused) or `null`
    // (not checked here — the verdict lives at the POST), and never `true`. A
    // model that decoded the third state as either of the other two would make
    // this client claim a verdict the owner did not compute.
    let s = snapshot();
    let acts = s.actionables.as_ref().expect("actionables");
    assert_eq!(acts.items.len(), 2);
    assert_eq!(acts.items[0].template_id.as_deref(), Some("regime_review"));
    assert_eq!(
        acts.items[0].startable, None,
        "an unchecked item is not a permitted one"
    );
    assert!(acts.items[0].reason.is_some());
    assert_eq!(acts.items[1].startable, Some(false));
    assert_eq!(acts.items[1].task_status.as_deref(), Some("running"));
    assert_eq!(acts.items[1].task_id.as_deref(), Some("3b7e05c19a4d6612"));

    // `true` is the POST's own answer. Unreachable on the snapshot today, and
    // decoded rather than rejected so the client cannot disagree with the gate
    // the day it arrives.
    let posted: Snapshot = serde_json::from_str(
        r#"{"actionables": {"items": [{"template_id": "regime_review", "startable": true}]}}"#,
    )
    .unwrap();
    assert_eq!(posted.actionables.unwrap().items[0].startable, Some(true));

    // Absent whole, and an empty-or-null list, are all a desk nobody has asked
    // — none of them may reject the payload the whole workstation is drawn from.
    assert!(serde_json::from_str::<Snapshot>("{}")
        .unwrap()
        .actionables
        .is_none());
    let null: Snapshot = serde_json::from_str(r#"{"actionables": {"items": null}}"#).unwrap();
    assert!(null.actionables.unwrap().items.is_empty());
}

#[test]
fn absent_sections_decode_as_none_not_zero() {
    // The owner omits sections freely. Absent must stay distinguishable from
    // zero all the way to the renderer, or an empty desk reads as a flat one.
    let s: Snapshot = serde_json::from_str("{}").unwrap();
    assert!(s.portfolio.is_none());
    assert!(s.market.is_none());
    assert!(s.performance.is_none());
    assert!(s.events.is_empty());

    // The owner serves `availability: null` from startup until the picker's own
    // route probes once, and a bare `#[serde(default)]` would reject the whole
    // snapshot over it — the `null_or_default` rule, which is why every desk
    // that has not yet asked its backends still renders.
    let s: Snapshot =
        serde_json::from_str(r#"{"llm": {"availability": null, "probed_at": null}}"#).unwrap();
    let llm = s.llm.unwrap();
    assert!(llm.availability.is_empty());
    assert_eq!(llm.probed_at, None);
    assert!(
        llm.reasoner_enabled.is_none(),
        "an owner that did not say is not a reasoner that is switched off"
    );

    let s: Snapshot = serde_json::from_str(r#"{"live_portfolio": {}}"#).unwrap();
    let live = s.live_portfolio.unwrap();
    assert_eq!(live.equity, None, "missing equity is not $0.00");
    assert_eq!(live.drawdown, None, "missing drawdown is not a flat book");
    assert!(live.positions.is_empty());
}

#[test]
fn unmodeled_top_level_keys_survive_in_extra() {
    // The owner ships ~26 top-level sections; the model names 20. The rest must
    // stay reachable rather than being dropped on decode. `agents` is the
    // witness now that `desk_mode` is modelled — the work rail's roster is one
    // of the sections no view on this client has ever drawn.
    let s = snapshot();
    assert_eq!(
        s.extra
            .get("agents")
            .and_then(|a| a.get(0))
            .and_then(|a| a.get("authority"))
            .and_then(|b| b.as_str()),
        Some("RESEARCH")
    );
}

#[test]
fn the_desk_mode_is_the_shape_the_owner_actually_serves() {
    // `desk_mode_payload` (server.py) sends data/book/label/offline plus the
    // credential pair. The fixture carried `{mode, book, live_capable}` — a
    // shape nothing in the owner produces — for eighteen tasks, unnoticed
    // because nothing decoded it. Modelling it is what caught that.
    let mode = snapshot().desk_mode.unwrap();
    assert_eq!(mode.data.as_deref(), Some("synthetic"));
    assert_eq!(mode.book.as_deref(), Some("simulated"));
    assert_eq!(mode.label.as_deref(), Some("SYNTHETIC"));
    assert_eq!(mode.offline, Some(true));
    // The credential pair travels together: a description with no verdict is a
    // sentence nobody can act on, and a verdict with no description cannot say
    // what is wrong.
    assert_eq!(mode.credentials_ok, Some(false));
    assert!(mode.credentials.is_some());

    // Absent stays absent. An owner that sent no mode section must not render
    // as a desk pointed at synthetic data by default.
    let none: Snapshot = serde_json::from_str("{}").unwrap();
    assert!(none.desk_mode.is_none());
    let empty: Snapshot = serde_json::from_str(r#"{"desk_mode": {}}"#).unwrap();
    let empty = empty.desk_mode.unwrap();
    assert_eq!(
        empty.offline, None,
        "missing offline is not a synthetic desk"
    );
    assert_eq!(empty.credentials_ok, None, "missing is not a working login");
}

#[test]
fn the_research_sections_decode_with_the_columns_that_view_renders() {
    let s = snapshot();

    // The policy the paper book is actually run under, plus the four
    // constraints the owner attaches to it.
    let policy = s.policy.unwrap();
    assert_eq!(policy.id.as_deref(), Some("hrp"));
    assert_eq!(policy.algorithm_id.as_deref(), Some("hrp"));
    let limits = policy.constraints.unwrap();
    assert_eq!(limits.long_only, Some(true));
    assert_eq!(limits.budget, Some(1.0));
    assert_eq!(limits.max_weight, Some(0.4));

    // Provenance, the quiet half of SETTINGS.
    let system = s.system.unwrap();
    assert_eq!(system.mode.as_deref(), Some("paper"));
    assert_eq!(system.governed_authority.as_deref(), Some("propose_only"));
    assert_eq!(system.data_source.as_deref(), Some("synthetic"));
    assert_eq!(system.mcp_servers, vec!["qlab-operator".to_string()]);

    // The leaderboard's five metrics are `OVERLAY_METRICS`, and an arm the
    // ablation could not score keeps its row with every metric absent rather
    // than being dropped or zeroed.
    assert_eq!(s.leaderboard.len(), 4);
    let champion = &s.leaderboard[0];
    assert_eq!(champion.champion, Some(true));
    assert_eq!(champion.sharpe, Some(0.8412));
    assert!(champion.deflated_sharpe.is_some());
    let unscored = s.leaderboard.last().unwrap();
    assert_eq!(
        unscored.sharpe, None,
        "an unscored arm is not a zero Sharpe"
    );
    assert_eq!(unscored.max_drawdown, None);

    // The catalog, with the stage that decides what an agent may run.
    assert_eq!(s.algorithms.len(), 6);
    assert_eq!(s.algorithms[0].id.as_deref(), Some("equal_weight"));
    let stages: Vec<&str> = s
        .algorithms
        .iter()
        .filter_map(|a| a.stage.as_deref())
        .collect();
    assert!(stages.contains(&"operational"));
    assert!(stages.contains(&"research"));
    assert!(stages.contains(&"offline"));

    // The run ledger, newest first: the predictor board leads it.
    assert_eq!(s.runs.len(), 4);
    assert_eq!(s.runs[0].kind.as_deref(), Some("predictor_board"));
    assert_eq!(s.runs[1].kind.as_deref(), Some("ablation"));
    assert!(s.runs[0].created_at.is_some());

    // The board decodes with the fields its readout renders, and an
    // unadmitted board carries `champion: null` rather than dropping the key.
    let board = s.runs[0].spec.as_ref().unwrap().board.as_ref().unwrap();
    assert_eq!(board.baseline.as_deref(), Some("ridge:none"));
    assert_eq!(board.champion, None);
    assert_eq!(board.admitted_any, Some(false));
    assert_eq!(board.models.len(), 3);
    assert_eq!(board.models[1].delta_mean_ic_vs_baseline, Some(-0.023));
}

#[test]
fn positions_carry_the_fields_the_book_view_renders() {
    let s = snapshot();
    let live = s.live_portfolio.unwrap();
    let first = &live.positions[0];
    assert_eq!(first.ticker.as_deref(), Some("ACWI"));
    assert!(first.avg_price.is_some());
    assert!(first.weight.is_some());
    assert!(first.unrealized_pnl_pct.is_some());
    assert!(live.marks.unwrap().live == Some(false));
}

#[test]
fn performance_series_is_objects_not_tuples() {
    let perf = snapshot().performance.unwrap();
    assert_eq!(perf.series.len(), 5);
    assert_eq!(perf.series[0].ts.as_deref(), Some("2026-07-26"));
    assert_eq!(perf.series[0].equity, Some(10000.0));
    assert!(perf.metrics.unwrap().sharpe.is_some());
}

#[test]
fn replay_return_is_reachable_despite_the_keyword_name() {
    let stress = snapshot().stress.unwrap();
    let crash = &stress.replays["2020"];
    assert_eq!(crash.available, Some(true));
    assert!(crash.ret.unwrap() < 0.0);
    assert_eq!(stress.replays["2008"].ret, None);
}

#[test]
fn a_null_collection_decodes_as_empty_rather_than_refusing_the_snapshot() {
    // Registry-backed lists serialise as `null` when unset — a real plan in the
    // owner's registry carries `"legs": null`. A bare `#[serde(default)]` covers
    // an absent key only, and rejected the whole 520 KB snapshot over this one.
    let s = snapshot();
    assert_eq!(s.plans.len(), 2);
    assert!(
        s.plans[1].legs.is_empty(),
        "null legs are no legs, not a decode error"
    );
    assert_eq!(s.plans[0].legs.len(), 2);

    let s: Snapshot =
        serde_json::from_str(r#"{"events": null, "market": {"assets": null}}"#).unwrap();
    assert!(s.events.is_empty());
    assert!(s.market.unwrap().assets.is_empty());
}

#[test]
fn coordinator_drive_state_is_nested_under_the_heartbeat() {
    let beat = snapshot().atlas_heartbeat.unwrap();
    let coord = beat.coordinator.unwrap();
    assert_eq!(coord.driving, Some(false));
    assert_eq!(coord.can_drive, Some(true));
}

#[test]
fn regime_panel_fixture_deserializes_with_partial_readings() {
    let panel: RegimePanel =
        serde_json::from_str(include_str!("fixtures/regime_panel.json")).unwrap();
    assert_eq!(panel.robust_state.as_deref(), Some("stress"));
    assert_eq!(panel.readings.len(), 6);
    assert_eq!(panel.readings[0].indicator_id, "absorption");
    assert!(panel.readings[0].percentile.is_some());
    // A detector that did not run still occupies a row; every field but its id
    // may be absent.
    let hmm = panel.readings.last().unwrap();
    assert_eq!(hmm.indicator_id, "hmm");
    assert!(hmm.state.is_none() && hmm.signal.is_none() && hmm.reasoning.is_none());
}

#[test]
fn a_universe_change_approval_carries_its_kind_and_no_plan_columns() {
    // The row `build_universe_change_request` files: every plan column null,
    // because it binds no plan and never expires, and the ticker it asks for
    // lives in `summary` beside the memo decision that argued for it.
    let approval: atlas::model::Approval = serde_json::from_str(
        r#"{"approval_id": "uc11223344556677", "kind": "universe_change",
             "plan_id": null, "plan_digest": null, "decision_id": null,
             "targets_hash": null, "data_permit_id": null, "broker": null,
             "book_revision": null, "expected_cost": null, "expires_at": null,
             "status": "pending",
             "summary": {"ticker": "NVDA", "memo_decision_id": "memo1234abcd"}}"#,
    )
    .unwrap();
    assert!(approval.is_universe_change());
    assert_eq!(approval.plan_id, None);
    assert_eq!(approval.expires_at, None);
    assert_eq!(approval.summary_str("ticker"), Some("NVDA"));
    assert_eq!(
        approval.summary_str("memo_decision_id"),
        Some("memo1234abcd")
    );

    // A row from before the migration carries no `kind` at all, and the owner
    // stamps those `plan`. Read as a third kind they would lose their plan
    // column to a widening the desk was never asked for.
    let legacy: atlas::model::Approval =
        serde_json::from_str(r#"{"approval_id": "aaaa1111", "plan_id": "pppp1111"}"#).unwrap();
    assert!(!legacy.is_universe_change());
    assert_eq!(legacy.summary_str("ticker"), None);
}

#[test]
fn the_rights_decode_whole_and_a_key_the_owner_left_out_is_unknown_rather_than_off() {
    // The owner writes all three keys on every change, so a body missing one is
    // not a shape it produces — but absence must still be *unknown* rather than
    // withdrawn. A `false` invented here would tell an operator their desk is
    // narrower than it is, which is the same fault as a `max_weight` defaulted
    // to zero one payload up.
    let whole: atlas::model::AtlasRights = serde_json::from_str(
        r#"{"rights": {"web": true, "workflows": false, "build": true},
            "path": "/state/atlas_rights.json"}"#,
    )
    .unwrap();
    assert_eq!(whole.rights.web, Some(true));
    assert_eq!(whole.rights.workflows, Some(false));
    assert_eq!(whole.rights.build, Some(true));
    assert_eq!(whole.path.as_deref(), Some("/state/atlas_rights.json"));
    // Never deserialised: the 200 payload has no such key, and a body that grew
    // one must not be able to fake a desk whose rights cannot be read.
    assert_eq!(whole.error, None);

    let partial: atlas::model::AtlasRights =
        serde_json::from_str(r#"{"rights": {"web": false}, "error": "invented"}"#).unwrap();
    assert_eq!(partial.rights.web, Some(false));
    assert_eq!(partial.rights.workflows, None);
    assert_eq!(partial.rights.build, None);
    assert_eq!(partial.path, None);
    assert_eq!(
        partial.error, None,
        "a body may not compose this client's own failure"
    );

    // And the accessor the card reads rows by agrees with the list it draws
    // them from — a reader and a writer that disagree about a key is a right
    // the operator believes they set and nothing honours.
    for field in atlas::model::RightsFlags::FIELDS {
        assert!(
            whole.rights.get(field).is_some(),
            "{field} is drawn but cannot be read"
        );
    }
}
