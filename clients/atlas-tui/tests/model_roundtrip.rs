//! The owner's payload shape, pinned against a captured fixture.
//!
//! Fixtures only — a client test that reached the owner would pass or fail on
//! whatever the desk happened to be holding, and would not run offline.

use atlas::model::{RegimePanel, Snapshot};

fn snapshot() -> Snapshot {
    serde_json::from_str(include_str!("fixtures/tui_snapshot.json")).unwrap()
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

    let s: Snapshot = serde_json::from_str(r#"{"live_portfolio": {}}"#).unwrap();
    let live = s.live_portfolio.unwrap();
    assert_eq!(live.equity, None, "missing equity is not $0.00");
    assert_eq!(live.drawdown, None, "missing drawdown is not a flat book");
    assert!(live.positions.is_empty());
}

#[test]
fn unmodeled_top_level_keys_survive_in_extra() {
    // The owner ships ~26 top-level sections; the model names 14. The rest must
    // stay reachable rather than being dropped on decode.
    let s = snapshot();
    assert_eq!(
        s.extra
            .get("desk_mode")
            .and_then(|d| d.get("book"))
            .and_then(|b| b.as_str()),
        Some("simulated_paper")
    );
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
