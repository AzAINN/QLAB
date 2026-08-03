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

    // The run ledger.
    assert_eq!(s.runs.len(), 3);
    assert_eq!(s.runs[0].kind.as_deref(), Some("ablation"));
    assert!(s.runs[0].created_at.is_some());
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
