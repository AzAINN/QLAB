//! Typed decode of the owner's payloads.
//!
//! Two rules run through every struct here.
//!
//! Absent is not zero. Every scalar is an `Option` and every collection is
//! defaulted, because the owner omits whole sections when a subsystem did not
//! run — no equity marks yet, no live stream, no HMM. A `f64` that defaults to
//! `0.0` renders a flat book and a zero drawdown, which is a statement the
//! owner never made. `--` is the only honest rendering of a number nobody
//! computed.
//!
//! The owner's shape wins. These structs mirror `/api/tui` as it is served, not
//! as it was specified: `regime` is nested under `market` (a client that read
//! it top-level rendered "unknown" forever), and `performance.series` carries
//! objects rather than pairs. Unmodeled keys survive in `Snapshot::extra`.

use serde::{Deserialize, Deserializer};
use serde_json::Value;
use std::collections::BTreeMap;

/// Absent and explicitly `null` are the same fact for a collection.
///
/// `#[serde(default)]` alone covers only a missing key. The owner's JSON comes
/// out of Python and DuckDB, where an unset list column serialises as `null` —
/// a real plan in the registry carries `"legs": null` — and a bare default
/// would reject the whole snapshot over it.
fn null_or_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}

/// One `/api/tui` response.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Snapshot {
    /// What the desk is pointed at. Absent means the owner did not say, which
    /// is not the same as a synthetic desk — nothing here defaults.
    #[serde(default)]
    pub desk_mode: Option<DeskMode>,
    #[serde(default)]
    pub portfolio: Option<Portfolio>,
    #[serde(default)]
    pub live_portfolio: Option<LivePortfolio>,
    #[serde(default)]
    pub market: Option<Market>,
    #[serde(default)]
    pub stress: Option<Stress>,
    #[serde(default)]
    pub performance: Option<Performance>,
    #[serde(default)]
    pub quotes: Option<Quotes>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub approvals: Vec<Approval>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub plans: Vec<Plan>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub orders: Vec<Order>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub workflows: Vec<Workflow>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub decisions: Vec<Decision>,
    #[serde(default)]
    pub atlas: Option<Atlas>,
    #[serde(default)]
    pub atlas_heartbeat: Option<AtlasHeartbeat>,
    #[serde(default)]
    pub atlas_read: Option<AtlasRead>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub events: Vec<Event>,
    /// The allocation policy the paper book is run under, with the mandate's
    /// constraints attached by the owner.
    #[serde(default)]
    pub policy: Option<Policy>,
    /// Health and authority facts — the quiet half of SETTINGS.
    #[serde(default)]
    pub system: Option<System>,
    /// The newest ablation, ranked. Empty is a desk that has not run one, which
    /// is a different fact from a desk whose arms all scored zero.
    #[serde(default, deserialize_with = "null_or_default")]
    pub leaderboard: Vec<LeaderboardRow>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub runs: Vec<Run>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub algorithms: Vec<Algorithm>,
    /// The sections nothing renders yet (`agents`, `news`, `atlas_tasks`,
    /// `equilibrium_returns`, …). Kept whole so a new view is a struct, not a
    /// re-capture of the payload.
    #[serde(flatten)]
    pub extra: Value,
}

// -- what the desk is pointed at -------------------------------------------

/// The chosen data lane and book, as `desk_mode_payload` serves them.
///
/// The credential pair travels together because neither half means anything
/// alone: a description with no verdict is a sentence nobody can act on, and a
/// verdict with no description cannot say what is wrong. Absence of
/// `credentials_ok` is *not* a working login — the owner always sends the flag,
/// so silence is a contract this client cannot read, and silence about the book
/// that can place real orders is the one answer that must not pass as clean.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct DeskMode {
    pub data: Option<String>,
    pub book: Option<String>,
    /// The owner's own words for the pair. Never composed here: the client does
    /// not re-spell a label the owner is the authority on.
    pub label: Option<String>,
    pub offline: Option<bool>,
    pub credentials: Option<String>,
    pub credentials_ok: Option<bool>,
}

impl DeskMode {
    /// Whether this desk is pointed at a book it cannot actually reach.
    ///
    /// One definition, because two surfaces draw it — the status line's chip
    /// and the SETTINGS card — and a desk that read as unreachable on one and
    /// fine on the other would be worse than either alone.
    ///
    /// Only the Alpaca book: the simulated book has no login to be broken, so a
    /// failing credential source there is not a desk that cannot trade. And
    /// silence is unreachable rather than fine — the owner always sends the
    /// flag, so its absence is a contract this client cannot read.
    pub fn book_unreachable(&self) -> bool {
        self.book.as_deref() == Some("alpaca") && self.credentials_ok != Some(true)
    }
}

// -- the policy and its limits ---------------------------------------------

/// `allocation_policy()`: the operational policy plus the mandate's constraints.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Policy {
    pub id: Option<String>,
    pub label: Option<String>,
    pub arm_id: Option<String>,
    pub objective: Option<String>,
    pub solver: Option<String>,
    pub algorithm_id: Option<String>,
    pub rationale: Option<String>,
    pub constraints: Option<Constraints>,
}

/// The four limits every paper solve is held to. Each is optional for the
/// reason every scalar here is: a `max_weight` defaulted to `0.0` would render
/// a mandate that forbids holding anything.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Constraints {
    pub long_only: Option<bool>,
    pub budget: Option<f64>,
    pub min_weight: Option<f64>,
    pub max_weight: Option<f64>,
}

// -- provenance and authority ----------------------------------------------

/// `system_status()`. Only the fields a surface renders; the row also carries
/// `claude_role`, `governed_lock_reason` and the autopilot block, which SETTINGS
/// does not draw.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct System {
    pub mode: Option<String>,
    pub offline: Option<bool>,
    pub claude_available: Option<bool>,
    pub mcp_configured: Option<bool>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub mcp_servers: Vec<String>,
    /// A config file that exists and does not parse is not the same fact as no
    /// file: the owner reports the parse error here rather than as absence.
    pub mcp_config_error: Option<String>,
    pub mcp_proxy_available: Option<bool>,
    pub governed_available: Option<bool>,
    pub governed_authority: Option<String>,
    pub workforce_available: Option<bool>,
    /// Cache-only provenance: what the last cached panel came from, and how old
    /// it is. Never a network fetch from a status poll.
    pub data_source: Option<String>,
    pub data_age_days: Option<i64>,
}

// -- research --------------------------------------------------------------

/// One ranked ablation arm. The five metrics are the owner's `OVERLAY_METRICS`,
/// in its reading order — one definition, two surfaces.
///
/// Every metric is optional because an arm the ablation could not score keeps
/// its row: the owner sorts those last rather than dropping them, and a zero
/// where a measurement is missing is a claim nobody made.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct LeaderboardRow {
    pub arm_id: Option<String>,
    pub name: Option<String>,
    /// The arm the mandate's `operational_policy` currently names.
    pub champion: Option<bool>,
    pub benchmark: Option<bool>,
    pub sharpe: Option<f64>,
    pub ann_return: Option<f64>,
    pub max_drawdown: Option<f64>,
    pub cvar_95: Option<f64>,
    pub deflated_sharpe: Option<f64>,
}

/// One row of the `runs` table, newest first as the owner serves it. The row
/// also carries `spec`, which is a whole research payload — thousands of
/// characters no list can hold, and re-decoded on every three-second poll.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Run {
    pub run_id: Option<String>,
    pub kind: Option<String>,
    pub created_at: Option<String>,
}

/// One catalog entry. `stage` is the boundary `algorithms.solve` enforces in
/// code — research and offline entries are visible and not agent-runnable — so
/// it is the field this client draws, not a decoration on the id.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Algorithm {
    pub id: Option<String>,
    pub label: Option<String>,
    pub category: Option<String>,
    pub stage: Option<String>,
    pub solver: Option<String>,
    pub agent_tool: Option<String>,
    /// What an offline entry needs installed before it can run at all.
    pub requires: Option<String>,
    pub agent_usable: Option<bool>,
}

// -- book ------------------------------------------------------------------

/// The reconciled book: positions keyed by ticker, as the registry holds them.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Portfolio {
    pub broker: Option<String>,
    pub cash: Option<f64>,
    pub equity: Option<f64>,
    pub high_water_mark: Option<f64>,
    pub drawdown: Option<f64>,
    pub kill_switch_at: Option<f64>,
    pub kill_switch_distance: Option<f64>,
    pub halted: Option<bool>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub positions: BTreeMap<String, PortfolioPosition>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub weights: BTreeMap<String, f64>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub target_weights: BTreeMap<String, f64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct PortfolioPosition {
    pub qty: Option<f64>,
    pub price: Option<f64>,
    pub value: Option<f64>,
    pub unrealized_pl: Option<f64>,
}

/// The same book marked to the live tape, with exposure and P&L derived.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct LivePortfolio {
    pub blocked: Option<bool>,
    pub broker: Option<String>,
    pub cash: Option<f64>,
    pub equity: Option<f64>,
    pub high_water_mark: Option<f64>,
    pub drawdown: Option<f64>,
    pub kill_switch_at: Option<f64>,
    pub kill_switch_distance: Option<f64>,
    pub halted: Option<bool>,
    pub gross_exposure: Option<f64>,
    pub net_exposure: Option<f64>,
    pub unrealized_pnl: Option<f64>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub positions: Vec<Position>,
    pub marks: Option<Marks>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Position {
    pub ticker: Option<String>,
    pub qty: Option<f64>,
    pub avg_price: Option<f64>,
    pub price: Option<f64>,
    pub value: Option<f64>,
    pub weight: Option<f64>,
    pub unrealized_pnl: Option<f64>,
    pub unrealized_pnl_pct: Option<f64>,
}

/// Where the marks came from. `execution_grade` is the whole point: a book
/// priced off synthetic bars must never be displayed as if it could be traded.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Marks {
    pub live: Option<bool>,
    pub source: Option<String>,
    pub feed: Option<String>,
    pub execution_grade: Option<bool>,
    pub quotes_fresh: Option<bool>,
    pub quote_health: Option<Value>,
}

// -- market ----------------------------------------------------------------

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Market {
    pub source: Option<String>,
    pub as_of: Option<String>,
    pub bar_age_days: Option<i64>,
    pub frequency: Option<String>,
    pub regime: Option<Regime>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub assets: Vec<Asset>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Regime {
    /// The threshold detector's label.
    pub regime: Option<String>,
    pub signal: Option<f64>,
    pub threshold: Option<f64>,
    pub window: Option<i64>,
    pub method: Option<String>,
    /// The guarded state the desk actually acts on — it disagrees with `regime`
    /// on purpose when the detectors do not agree.
    pub robust_state: Option<String>,
    pub confidence: Option<f64>,
    pub effective_risk_fraction: Option<f64>,
    /// Present only when `hmmlearn` is installed on the owner.
    pub posterior: Option<BTreeMap<String, f64>>,
    pub hmm_label: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Asset {
    pub ticker: Option<String>,
    pub price: Option<f64>,
    pub change_1d: Option<f64>,
    pub change_20d: Option<f64>,
    pub realized_vol: Option<f64>,
    /// Closing prices, oldest first — the sparkline series.
    #[serde(default, deserialize_with = "null_or_default")]
    pub history: Vec<f64>,
}

// -- guardrails ------------------------------------------------------------

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Stress {
    pub drawdown_tier: Option<String>,
    pub drawdown_thresholds: Option<DrawdownThresholds>,
    pub gross_exposure: Option<f64>,
    pub max_gross_exposure: Option<f64>,
    pub leverage_headroom: Option<f64>,
    pub stressed_vol: Option<f64>,
    pub stress_vol_limit: Option<f64>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub replays: BTreeMap<String, Replay>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub cost_gate_refusals: Vec<CostGateRefusal>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct DrawdownThresholds {
    pub warning: Option<f64>,
    pub control: Option<f64>,
    pub breaker: Option<f64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Replay {
    pub available: Option<bool>,
    pub start: Option<String>,
    pub end: Option<String>,
    /// `return` is a Rust keyword; this is the only renamed field in the model.
    #[serde(rename = "return")]
    pub ret: Option<f64>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct CostGateRefusal {
    pub ts: Option<String>,
    pub plan_id: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub reasons: Vec<String>,
}

// -- performance -----------------------------------------------------------

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Performance {
    #[serde(default, deserialize_with = "null_or_default")]
    pub series: Vec<EquityPoint>,
    pub metrics: Option<Metrics>,
    pub since_start: Option<f64>,
    pub window_change: Option<f64>,
    pub cadence: Option<Cadence>,
    pub note: Option<String>,
    pub book: Option<String>,
    pub marks: Option<i64>,
    pub marks_total: Option<i64>,
    pub marks_capped: Option<bool>,
    pub mark_limit: Option<i64>,
    pub excluded_marks: Option<i64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct EquityPoint {
    pub ts: Option<String>,
    pub equity: Option<f64>,
}

/// The owner sends this only once it has enough marks for a full bundle — a
/// partial one is withheld rather than shipped with holes.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Metrics {
    pub n_obs: Option<i64>,
    pub ann_return: Option<f64>,
    pub ann_vol: Option<f64>,
    pub sharpe: Option<f64>,
    pub sortino: Option<f64>,
    pub downside_deviation: Option<f64>,
    pub omega_ratio: Option<f64>,
    pub max_drawdown: Option<f64>,
    pub cvar_95: Option<f64>,
    pub realized_skew: Option<f64>,
    pub realized_kurtosis: Option<f64>,
    pub deflated_sharpe: Option<f64>,
    pub turnover: Option<f64>,
}

/// How the series was annualized. Rendering a Sharpe without it would hide
/// which calendar produced the number.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Cadence {
    pub periods_per_year: Option<f64>,
    pub observed_span_days: Option<f64>,
    pub mean_step_days: Option<f64>,
    pub basis: Option<String>,
}

// -- live quotes -----------------------------------------------------------

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Quotes {
    pub live_stream: Option<bool>,
    /// Why there is no stream, when there is none.
    pub reason: Option<String>,
    pub feed: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub quotes: BTreeMap<String, Quote>,
    pub health: Option<StreamHealth>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Quote {
    pub price: Option<f64>,
    pub age_seconds: Option<f64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct StreamHealth {
    pub state: Option<String>,
    pub feed: Option<String>,
    pub connected: Option<bool>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub symbols: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub stale_symbols: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub quote_ages: BTreeMap<String, f64>,
    pub reconnects: Option<i64>,
    pub fresh: Option<bool>,
    pub last_error: Option<String>,
}

// -- the execution gate ----------------------------------------------------

/// A plan-bound, expiring approval request. The client renders these; only the
/// operator posture may act on one, and only through the owner.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Approval {
    pub approval_id: Option<String>,
    pub task_id: Option<String>,
    pub plan_id: Option<String>,
    pub plan_digest: Option<String>,
    pub decision_id: Option<String>,
    pub targets_hash: Option<String>,
    pub data_permit_id: Option<String>,
    pub broker: Option<String>,
    pub book_revision: Option<String>,
    pub expected_cost: Option<Value>,
    pub summary: Option<Value>,
    pub status: Option<String>,
    pub challenge_digest: Option<String>,
    pub expires_at: Option<String>,
    pub decided_at: Option<String>,
    pub consumed_at: Option<String>,
    pub invalidated_reason: Option<String>,
    pub created_at: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Plan {
    pub plan_id: Option<String>,
    pub decision_id: Option<String>,
    pub state: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub targets: BTreeMap<String, f64>,
    pub pre_trade: Option<Value>,
    pub created_at: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub legs: Vec<Leg>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Leg {
    pub client_order_id: Option<String>,
    pub ticker: Option<String>,
    pub side: Option<String>,
    pub notional: Option<f64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Order {
    pub client_order_id: Option<String>,
    pub plan_id: Option<String>,
    pub ticker: Option<String>,
    pub side: Option<String>,
    pub notional: Option<f64>,
    pub state: Option<String>,
    pub created_at: Option<String>,
    pub filled_qty: Option<f64>,
    pub avg_fill_price: Option<f64>,
    pub fee: Option<f64>,
}

// -- the decision log ------------------------------------------------------

/// One row of the registry's decision log, newest first as the owner serves it.
///
/// Only the fields a surface renders. The row also carries `choice`,
/// `rationale`, and `challenger_view` — the referee's reasoning runs to
/// thousands of characters, and a client that decoded all of it on every
/// three-second poll would be paying for text no tile can hold. Task 18's audit
/// view is where the whole record belongs.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Decision {
    pub decision_id: Option<String>,
    pub as_of: Option<String>,
    pub kind: Option<String>,
    pub created_at: Option<String>,
    /// The referee's word on this decision, attached by the owner from the
    /// verdict table. `None` is the common case: most rows are logged before
    /// anything adjudicates them, and a missing verdict is not a failed one.
    pub verdict: Option<Verdict>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Verdict {
    pub verdict: Option<String>,
    pub verdict_id: Option<String>,
    /// `deterministic` or `referee-agent` — which authority said it, which is
    /// half of what the word means.
    pub source: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub reasons: Vec<String>,
}

// -- the workforce ---------------------------------------------------------

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Workflow {
    pub workflow_id: Option<String>,
    pub kind: Option<String>,
    pub status: Option<String>,
    pub current_phase: Option<String>,
    pub request: Option<Value>,
    pub result: Option<Value>,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub steps: Vec<WorkflowStep>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct WorkflowStep {
    pub step_id: Option<String>,
    pub workflow_id: Option<String>,
    pub seq: Option<i64>,
    pub phase: Option<String>,
    pub agent: Option<String>,
    pub status: Option<String>,
    pub summary: Option<String>,
    /// Free-form per-role output; every role writes a different set of keys.
    pub artifacts: Option<Value>,
    pub started_at: Option<String>,
    pub completed_at: Option<String>,
    pub updated_at: Option<String>,
}

// -- /api/atlas/templates --------------------------------------------------

/// The registered workflow templates, from the owner's own registry.
///
/// Its own endpoint rather than a section of `/api/tui`: the snapshot carries
/// what the desk *is*, and the template set is what the desk can be *asked for*
/// — it changes when the owner is deployed, not when the market moves. The
/// poller fetches it on a cadence of its own for that reason.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Templates {
    #[serde(default, deserialize_with = "null_or_default")]
    pub templates: Vec<Template>,
}

/// One template Atlas — or a human — may start.
///
/// `phases` is the graph the template *declares*. It is not necessarily the
/// graph a workflow started over HTTP runs: `/api/workflows/start` refuses to
/// read phases from a network caller at all ("letting a network caller shape
/// the phase graph would let it drop a gate phase"), so the owner runs its
/// standard graph for anything started from here. Kept because it is what the
/// owner said, and shown as a declaration rather than as a promise.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Template {
    pub template_id: Option<String>,
    pub purpose: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub phases: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub requires: Vec<String>,
    /// The authority boundary: a template that would create a paper plan is
    /// refused below `propose` mode. The owner decides; the picker only says so.
    pub creates_plan: Option<bool>,
    pub needs_coordinator: Option<bool>,
    pub notes: Option<String>,
}

// -- Atlas -----------------------------------------------------------------

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Atlas {
    pub manager_id: Option<String>,
    pub mode: Option<String>,
    pub state: Option<String>,
    pub current_task_id: Option<String>,
    pub last_wake_reason: Option<String>,
    pub last_brief_at: Option<String>,
    pub blocked_reason: Option<String>,
    pub coordinator_available: Option<bool>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct AtlasHeartbeat {
    pub running: Option<bool>,
    pub interval_s: Option<f64>,
    pub ticks: Option<i64>,
    pub errors: Option<i64>,
    /// A monotonic clock reading, not a wall-clock timestamp — only differences
    /// against the owner's own clock mean anything.
    pub last_tick_at: Option<f64>,
    pub last_error: Option<String>,
    pub last_error_at: Option<String>,
    pub last_state: Option<String>,
    pub autonomous: Option<bool>,
    pub coordinator: Option<Coordinator>,
    pub fast: Option<bool>,
}

/// Registering a workflow is not running it: `driving` is the only evidence
/// that phases are actually advancing.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Coordinator {
    pub driving: Option<bool>,
    pub workflow_id: Option<String>,
    pub can_drive: Option<bool>,
    pub reason: Option<String>,
}

/// Atlas's advisory read of the desk. Advisory is a fact about it, not a
/// disclaimer: nothing here authorises anything.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct AtlasRead {
    pub as_of: Option<String>,
    pub quantitative_state: Option<String>,
    pub news: Option<ReadNews>,
    pub agreement: Option<String>,
    pub conviction: Option<f64>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub tensions: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub observations: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub would_change_my_mind: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub evidence_refs: Vec<String>,
    pub read_hash: Option<String>,
    pub advisory: Option<bool>,
    pub news_source: Option<String>,
    /// Why there is no news window, when there is none. The owner sets this
    /// rather than shipping an empty window, because "the feed is down" and
    /// "the tape is quiet" are opposite facts about the same silence.
    pub news_error: Option<String>,
    pub grounding: Option<Value>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub supported_claims: Vec<Value>,
    pub qualitative_signals: Option<Value>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct ReadNews {
    pub item_count: Option<i64>,
    pub risk_off_hits: Option<i64>,
    pub risk_on_hits: Option<i64>,
    pub tone: Option<String>,
    pub intensity: Option<f64>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub top_tickers: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub headlines: Vec<Headline>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Headline {
    pub headline: Option<String>,
    pub source: Option<String>,
    pub published: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub tickers: Vec<String>,
    pub tone: Option<String>,
}

// -- audit bus -------------------------------------------------------------

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Event {
    pub event_id: Option<String>,
    pub ts: Option<String>,
    pub kind: Option<String>,
    pub payload: Option<Value>,
}

// -- /api/regime/panel -----------------------------------------------------

/// Every indicator read off ONE snapshot. A diagnostic of market state — never
/// a signal, a forecast, or a weight.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct RegimePanel {
    pub panel_version: Option<i64>,
    pub snapshot_id: Option<String>,
    pub as_of: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub universe: Vec<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub readings: Vec<Reading>,
    pub robust_state: Option<String>,
    pub agreement_count: Option<i64>,
    pub disagreement_count: Option<i64>,
    pub failed_count: Option<i64>,
    pub uncertainty_reason: Option<String>,
    pub fingerprint: Option<Value>,
}

/// A detector that failed still occupies a row, so the panel shows what did not
/// run rather than quietly shortening. Only the id is guaranteed.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Reading {
    pub indicator_id: String,
    pub version: Option<i64>,
    pub state: Option<String>,
    pub signal: Option<f64>,
    pub threshold: Option<f64>,
    pub percentile: Option<f64>,
    pub window: Option<i64>,
    pub reasoning: Option<String>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub quality_flags: Vec<String>,
}
