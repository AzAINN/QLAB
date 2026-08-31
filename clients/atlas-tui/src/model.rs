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

/// A JSON object decoded into `T`, and anything else decoded as absent.
///
/// For a free-form column that is *usually* an object. `runs.spec` is JSON the
/// registry stored verbatim, so a row written by an older or stranger producer
/// can hold a string, a list, or a number — and a bare `Option<T>` would reject
/// the whole snapshot over one such row rather than losing one readout. The
/// Textual client guards the same column the same way (`if not isinstance(spec,
/// dict)`), so this is the owner-side rule kept rather than a new tolerance.
///
/// A well-formed object that does not match `T` also decodes as absent, which is
/// the point: every field of `T` here is already optional, so the only way to
/// fail is a type collision, and one of those is a spec this client cannot read.
fn object_or_none<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: serde::de::DeserializeOwned,
{
    Ok(match Option::<Value>::deserialize(deserializer)? {
        Some(object @ Value::Object(_)) => T::deserialize(object).ok(),
        _ => None,
    })
}

/// One `/api/tui` response.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Snapshot {
    /// What the desk is pointed at. Absent means the owner did not say, which
    /// is not the same as a synthetic desk — nothing here defaults.
    #[serde(default)]
    pub desk_mode: Option<DeskMode>,
    /// Whether the operator has armed this desk, and whether anyone has ever
    /// been asked. Absent is an owner that does not serve a posture, which is
    /// not the same as a desk answered "read-only" — see `Store::posture_armed`.
    #[serde(default)]
    pub posture: Option<PostureBlock>,
    /// Which minds the desk is using, and how fresh that answer is.
    #[serde(default)]
    pub llm: Option<LlmConfig>,
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
    /// The conversation with the desk manager, oldest first as the owner
    /// serves it (`read_events_of_kind("atlas_message", limit=60)`).
    ///
    /// Its own key rather than a filter over `events`, because the general
    /// window floods: a news-archive poll writes a row every 30 s, so an hour
    /// of idling pushes the chat out of any fixed window and the conversation
    /// a client renders would silently end an hour back.
    #[serde(default, deserialize_with = "null_or_default")]
    pub atlas_chat: Vec<Event>,
    /// The newest persisted predictor board, summarised — the same summary the
    /// reasoner is handed, so the operator can see the evidence base the desk
    /// reasons from.
    #[serde(default)]
    pub predictors: Option<Predictors>,
    /// Today's proposal set: what Atlas would do, and what the gate has already
    /// said it would not. Absent is an owner that serves no such block — see
    /// [`Actionables`].
    #[serde(default)]
    pub actionables: Option<Actionables>,
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

/// The desk's posture, as `posture_payload` serves it.
///
/// Both halves are `Option` like every other decoded field: the owner is the
/// authority on its own posture, and a client that defaulted a missing `armed`
/// to `false` would be indistinguishable from an owner that answered "no" —
/// which is the difference Task 3's door is built on. `chosen: false` is a desk
/// nobody has answered for; it is served alongside `armed: false` because the
/// safe default and the chosen read-only posture look identical otherwise.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct PostureBlock {
    pub armed: Option<bool>,
    pub chosen: Option<bool>,
}

// -- what the desk would do -------------------------------------------------

/// `atlas_actionables_snapshot`: today's proposals, read from the task table.
///
/// The whole block is optional and every field inside it is, on the rule this
/// module opens with and for the reason [`PostureBlock`] is shaped that way: an
/// owner that does not serve the block is *absent*, not a desk with nothing to
/// do, and a client that defaulted the two together would draw an empty panel
/// over an owner too old to have one.
///
/// A snapshot never *composes* the menu — asking is what proposes, drawing is
/// what reports — so what is here is what somebody already asked for, and it
/// keeps the whole trading day: running, completed and expired items stay in
/// the list so both clients agree about what was asked. `task_status` is what
/// tells them apart.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Actionables {
    #[serde(default, deserialize_with = "null_or_default")]
    pub items: Vec<ActionItem>,
}

/// One proposal, and whatever verdict the surface that served it could reach.
///
/// **`startable` is three-valued, and `true` cannot arrive on the snapshot.**
/// That surface must not call `atlas_facts` — it would latch the regime out
/// from under the observe tick — so it cannot check the data preconditions and
/// deliberately does not assert a verdict it did not compute. `false` is an
/// outright refusal with the owner's own sentence in `reason` (an unregistered
/// template, a spent task, a stale proposal, or a mode that forbids it); `None`
/// is "not ruled on here — the POST is where the gate speaks". `true` is the
/// POST's own answer, modelled because the client must not disagree with the
/// gate the day it arrives on a payload this decodes.
///
/// Absence and `""` are one fact for the strings, as everywhere else: they go
/// through `format::text` before anything renders them.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ActionItem {
    pub template_id: Option<String>,
    pub purpose: Option<String>,
    /// Three states. `None` is not `Some(false)` — see the type's own note.
    pub startable: Option<bool>,
    /// Why not, in the owner's words, whenever it said. Never composed here.
    pub reason: Option<String>,
    /// The persisted task an approval would bind to. Decoded rather than
    /// drawn: a 32-cell column has no room for an id, and the item is nothing
    /// to approve without one. `model_roundtrip` is what holds the shape until
    /// an approve path reads it.
    pub task_id: Option<String>,
    pub task_status: Option<String>,
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
    /// Whether anything ever *named* the pair above — a launcher flag, the
    /// state file, or a POST — as against the fallback the owner has to serve
    /// when nobody has.
    ///
    /// **Three states, and the third is an owner rather than a desk.** `None`
    /// is an owner too old to carry the field, which is every owner before D4;
    /// reading that silence as `false` would say "nobody chose this desk" about
    /// desks that had, on the one client that opens a modal over the answer. So
    /// absence keeps whatever the reader did before the field existed, and only
    /// `Some(false)` is the owner asserting the negative.
    pub chosen: Option<bool>,
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

// -- which minds the desk is using ------------------------------------------

/// `llm_payload()`: one model choice per surface, plus the last thing the owner
/// learned about whether those backends can serve.
///
/// **`availability` is a reading, not a probe.** `/api/tui` runs under the
/// owner's dispatch lock and is polled every two seconds, so the owner refuses
/// to probe there and serves whatever the picker's own route last saw, stamped
/// with `probed_at`. A surface that rendered this as live would be reporting a
/// daemon's health from an hour ago as current, which is why the stamp travels
/// with it and why SETTINGS renders the age rather than the reading alone.
///
/// `probed_at` absent is "nothing has asked yet" — a different fact from a
/// reading this client could not read, and the two must not render the same.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct LlmConfig {
    pub reasoner: Option<LlmSurface>,
    pub workforce: Option<LlmSurface>,
    /// Whether Atlas reasons with a model at all. Off is the desk's default, and
    /// naming a reasoner model does not switch it on — the owner refuses to
    /// infer one from the other, so nothing here may either.
    pub reasoner_enabled: Option<bool>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub availability: Vec<LlmBackend>,
    pub probed_at: Option<String>,
}

impl LlmConfig {
    /// What the last reading said about one backend, by the name the owner gave
    /// it. `None` for a backend nothing has asked about — which is not the same
    /// fact as one that answered no.
    pub fn backend(&self, name: &str) -> Option<&LlmBackend> {
        self.availability
            .iter()
            .find(|entry| entry.name.as_deref() == Some(name))
    }
}

/// One surface's choice. The pair travels together because neither half means
/// anything alone: a model with no backend names nothing that can run it, and a
/// backend with no model is a surface nobody can say what runs on.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct LlmSurface {
    pub backend: Option<String>,
    pub model: Option<String>,
}

/// One backend's entry in the compact summary — the catalog minus its model
/// lists, which an Ollama host can have dozens of and which do not ride in a
/// payload polled every two seconds.
///
/// The reason is populated on the happy path too: the owner treats a silent
/// `false` as the bug it once was, and every surface that renders availability
/// renders the sentence behind it.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct LlmBackend {
    pub name: Option<String>,
    pub available: Option<bool>,
    pub reason: Option<String>,
}

/// `GET /api/llm/backends`: every backend the desk knows, and what each serves
/// *right now*.
///
/// Its own payload rather than a section of the snapshot, because the owner
/// refuses to probe on the poll path: `/api/tui` runs under the dispatch lock
/// every two seconds and a hung daemon there would stall every other request,
/// so the snapshot carries the last reading and this route is the only prober.
/// It is fetched when the palette enters the model scope and at no other time —
/// there is no cadence here, deliberately.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct LlmCatalog {
    #[serde(default, deserialize_with = "null_or_default")]
    pub backends: Vec<CatalogEntry>,
    pub probed_at: Option<String>,
}

/// One backend as the catalog reports it: the summary plus the model list.
///
/// Deliberately **not** `LlmBackend` with a `models` field bolted on. The
/// snapshot's summary is the same three fields with the lists stripped, and one
/// struct for both would leave `store.llm()` handing out a permanently empty
/// `models` — a client reading its own silence as "this backend serves
/// nothing". Two payloads, two types, and the compiler decides which one a
/// caller is holding.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct CatalogEntry {
    pub name: Option<String>,
    pub available: Option<bool>,
    pub reason: Option<String>,
    /// What this backend can serve now — empty when it cannot serve at all
    /// (the owner does not ask an unavailable backend for its list).
    #[serde(default, deserialize_with = "null_or_default")]
    pub models: Vec<String>,
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

/// One row of the `runs` table, newest first as the owner serves it.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Run {
    pub run_id: Option<String>,
    pub kind: Option<String>,
    pub created_at: Option<String>,
    #[serde(default, deserialize_with = "object_or_none")]
    pub spec: Option<RunSpec>,
}

/// **A deliberate subset of `runs.spec`.**
///
/// The column is a whole research payload whose keys differ per run kind — the
/// live prediction spec alone runs to two kilobytes of per-fold diagnostics,
/// feature lists and index bounds, re-decoded on every three-second poll. Only
/// the fields the vol-forecast readout renders are named here. Everything else
/// is dropped on decode rather than carried; a surface that needs more should
/// add the field it needs and say why, not widen this to the whole blob.
///
/// Every field is optional because most runs are not predictions and carry none
/// of them — a `backtest` spec decodes into an all-absent `RunSpec`, which is
/// the honest reading of "this row is not a vol forecast".
#[derive(Debug, Clone, Default, Deserialize)]
pub struct RunSpec {
    /// Mean information coefficient across the purged walk-forward folds.
    pub mean_ic: Option<f64>,
    /// How much of that survives fold to fold. The forecast is admitted on
    /// both, not on the mean alone.
    pub ic_stability: Option<f64>,
    /// The owner's own admission verdict.
    pub usable: Option<bool>,
    /// The rule that verdict was reached by, as the run recorded it.
    pub admission: Option<Admission>,
    /// Present only on `predictor_board` runs: the paired evaluation of the
    /// augmented lane's rescue models against the ridge baseline.
    pub board: Option<BoardSpec>,
}

/// The predictor board a `predictor_board` run carries in its spec.
///
/// Same subset rule as `RunSpec`: only what the one-line readout renders.
/// The full board (per-fold ICs, hyperparameters, paired t-statistics) stays
/// in the registry row for surfaces that ask for it.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct BoardSpec {
    /// The fixed comparison arm — `ridge:none`, the admitted v1 forecaster.
    pub baseline: Option<String>,
    /// The first *admitted* model in the owner's ranking. Absent means the
    /// board ran and nothing cleared admission — an honest empty answer.
    pub champion: Option<String>,
    pub admitted_any: Option<bool>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub models: Vec<BoardModel>,
}

/// One evaluated model's summary row on the board.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct BoardModel {
    pub model_id: Option<String>,
    pub mean_ic: Option<f64>,
    pub usable: Option<bool>,
    pub delta_mean_ic_vs_baseline: Option<f64>,
}

/// The `predictors` section: `predictor_board_summary()` as `/api/tui` serves
/// it.
///
/// Deliberately **not** `BoardSpec` widened. That struct is the one-line
/// readout's subset of a *run row's* spec; this is the owner's own summary of
/// the newest board, with the baseline/champion rows expanded into full
/// metrics. One struct for both would leave whichever surface reads the other
/// shape decoding absent fields forever.
///
/// `status` is the only field the owner always sends: `never_ran` and
/// `unreadable` payloads carry nothing else worth a name, and every metric is
/// optional because a zero where a measurement is missing is a claim nobody
/// made.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Predictors {
    pub status: Option<String>,
    pub run_id: Option<String>,
    pub as_of: Option<String>,
    pub age_days: Option<i64>,
    pub admitted_any: Option<bool>,
    /// Whether the champion's edge survives the owner's selection null.
    /// `None` is a board that predates the null — neither established nor
    /// refuted, and it must not render as either.
    pub champion_established: Option<bool>,
    pub n_obs: Option<i64>,
    pub n_folds: Option<i64>,
    pub champion: Option<PredictorMetrics>,
    pub baseline: Option<PredictorMetrics>,
    pub best_delta_vs_baseline: Option<f64>,
}

/// One evaluated model's full metrics row on the board summary.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct PredictorMetrics {
    pub model_id: Option<String>,
    pub family: Option<String>,
    /// The quantum feature augmentation, or `"none"` for a classical lane.
    /// What earns the `q` badge — the same rule as the web UI's.
    pub variant: Option<String>,
    pub mean_ic: Option<f64>,
    pub ic_std: Option<f64>,
    pub ic_stability: Option<f64>,
    pub usable: Option<bool>,
    pub paired_t_vs_baseline: Option<f64>,
    pub wins_vs_baseline: Option<i64>,
    pub delta_mean_ic_vs_baseline: Option<f64>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub per_fold: Vec<PredictorFold>,
}

/// One purged walk-forward fold's IC. The folds are what make a mean
/// interpretable: folds that change sign are not a skill estimate.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct PredictorFold {
    pub fold: Option<i64>,
    pub ic: Option<f64>,
}

/// `/api/research/predictors` — the whole board, for the PREDICTORS view.
///
/// Deliberately **not** `Predictors` widened, for the same reason `Predictors`
/// is not `BoardSpec` widened: the summary is the reasoner's narrow feed and
/// this is the operator's full ranking. One struct for both would leave
/// whichever surface reads the other shape decoding absent fields forever.
///
/// `reason` and `lane` are the owner's own prose. This client renders them
/// verbatim rather than re-deriving a verdict from the numbers: the sentence
/// that says "cleared the bar" versus "NOT ESTABLISHED against the selection
/// null" is a judgment the *board* made, and two wordings of it would drift.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct PredictorDetail {
    pub status: Option<String>,
    pub run_id: Option<String>,
    pub as_of: Option<String>,
    pub universe: Option<String>,
    pub lane: Option<String>,
    pub reason: Option<String>,
    pub admitted_any: Option<bool>,
    /// `None` is a board that predates the selection null — neither
    /// established nor refuted, and it must not render as either.
    pub champion_established: Option<bool>,
    pub selection_null: Option<SelectionNull>,
    pub champion: Option<String>,
    pub baseline: Option<String>,
    pub n_obs: Option<i64>,
    pub n_folds: Option<i64>,
    pub target: Option<String>,
    pub horizon_days: Option<i64>,
    pub embargo_days: Option<i64>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub models: Vec<PredictorRow>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub caveats: Vec<String>,
}

/// One evaluated model on the full board, in the owner's ranking order.
///
/// `augmented` and `control_note` are stated by the owner rather than inferred
/// from the id here: `kernel:linear` sits in the kernel family and applies no
/// feature map, and a client that filed lanes by family would put a control in
/// the treatment arm.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct PredictorRow {
    pub model_id: Option<String>,
    pub family: Option<String>,
    pub variant: Option<String>,
    pub augmented: Option<bool>,
    pub control_note: Option<String>,
    pub is_baseline: Option<bool>,
    pub is_champion: Option<bool>,
    pub mean_ic: Option<f64>,
    pub ic_std: Option<f64>,
    pub ic_stability: Option<f64>,
    pub usable: Option<bool>,
    pub delta_mean_ic_vs_baseline: Option<f64>,
    pub wins_vs_baseline: Option<i64>,
    pub paired_t_vs_baseline: Option<f64>,
    /// The owner's stated |t| >= 2 convention, already folded with its n:
    /// `false` whenever the fold count is unknown, never `None`.
    pub significant: Option<bool>,
    /// Plain ICs, unlike the summary's `PredictorFold` rows: the detail route
    /// serves the series already flattened.
    #[serde(default, deserialize_with = "null_or_default")]
    pub per_fold: Vec<f64>,
    pub negative_folds: Option<i64>,
}

/// The board's selection null: the same champion-picking procedure run on
/// resampled noise, which is what turns "cleared a fixed bar" into evidence.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SelectionNull {
    pub trials: Option<i64>,
    pub p_value: Option<f64>,
    pub p_value_resolution: Option<f64>,
    pub underpowered_for_alpha: Option<bool>,
}

/// The admission gate a prediction run states about itself.
///
/// Read off the payload rather than duplicated as constants here. The owner
/// writes its thresholds into every spec (`research/prediction.py`), so a client
/// that hard-coded `0.03` and `0.5` would keep asserting an old gate after the
/// owner moved it — and this is a research-admission signal, which is exactly
/// the kind of number that must not drift silently.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct Admission {
    pub mean_ic_strictly_above: Option<f64>,
    pub ic_stability_strictly_above: Option<f64>,
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
    /// Same clock as `last_tick_at`; absent until a tick has failed.
    pub last_error_at: Option<f64>,
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

// -- /api/news/settings ----------------------------------------------------

/// What the desk reads the news from, and what it could read it from.
///
/// Its own payload rather than a section of the snapshot, for the reason
/// [`LlmCatalog`] is: the owner composes it from the environment and its own
/// setup catalog, and `/api/tui` carries none of it. Fetched when SETTINGS is
/// entered, like the predictor board.
///
/// Every field is optional or defaulted, including the two the owner always
/// sends. `configured: false` and "the owner did not say" are different facts
/// about a desk, and a client that folded them would claim a news stack nobody
/// configured — the same rule every other struct here is written by.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct NewsSettings {
    /// `synthetic` or `live`, following the request's own lane flag.
    pub lane: Option<String>,
    /// The stack the desk resolves *right now*. On an offline desk this is
    /// `["synthetic"]` whatever the catalog says, which is the owner's truth
    /// and not a state this client may talk it out of.
    #[serde(default, deserialize_with = "null_or_default")]
    pub stack: Vec<String>,
    pub configured: Option<bool>,
    /// Whether a contact is stored. Never the contact itself — the owner does
    /// not serve it, and this client has nowhere to put one.
    pub edgar_contact_set: Option<bool>,
    #[serde(default, deserialize_with = "null_or_default")]
    pub catalog: Vec<NewsSource>,
    /// What each member of the stack last did, in the owner's own words
    /// (`ok`, or a sentence). Empty before any fetch.
    #[serde(default, deserialize_with = "null_or_default")]
    pub outcomes: BTreeMap<String, String>,
    /// Anything the owner grew that this client does not model yet, kept whole
    /// for the reason `Snapshot::extra` is.
    #[serde(flatten)]
    pub extra: Value,
}

/// One source the desk could read, as the owner's setup catalog describes it.
///
/// `available` and `chosen` are two different claims and the owner makes both:
/// a source can be installable-and-unchosen, chosen-and-unavailable is what the
/// route refuses, and `default` is what the wizard would have picked. A client
/// that derived any of them from the others would be a second opinion about a
/// stack the owner owns.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct NewsSource {
    pub name: Option<String>,
    pub tier: Option<String>,
    /// What this source wants before it can be chosen — `""`,
    /// `QLAB_EDGAR_CONTACT`, or `an Alpaca credential`.
    pub needs: Option<String>,
    /// The one-line cost of reading it, in the catalog's own words.
    pub cost: Option<String>,
    pub available: Option<bool>,
    pub default: Option<bool>,
    /// Whether the name is in the resolved stack right now. Not the same claim
    /// as `default`.
    pub chosen: Option<bool>,
}
