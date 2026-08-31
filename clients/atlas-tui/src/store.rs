//! Application state, and the diffing that decides which fields changed enough to trigger.
//!
//! Two jobs, deliberately in one place. The store holds what the owner said,
//! and it decides what *changed* — because a change is the only thing motion is
//! allowed to be about. An effect fired from a render pass would animate on
//! every repaint; an effect fired from a diff animates once, when the desk
//! actually moved.

use crate::bus::{AppEvent, Channel, HttpResult, SseEvent};
use crate::cmd::CmdLine;
use crate::format::text;
use crate::glyph::Mood;
use crate::model::{
    Algorithm, Approval, Asset, Coordinator, DeskMode, LeaderboardRow, LlmCatalog, LlmConfig,
    NewsSettings, Plan, Policy, PredictorDetail, QualitativeMatrix, RegimePanel, Run, Snapshot,
    System, Template, Workflow,
};
use crate::net::http;
use crate::ui::door::Door;
use serde_json::Value;
use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

/// The idle heartbeat. Three indicators on screen claim the client is alive —
/// the glyph, the throbbers, the quote ages — and a frame this often is what
/// makes that claim true when no event has arrived.
///
/// Public so `fx` can assert the ordering the effect cadence depends on. The
/// three beats belong together: this one and `TICK` are the floor a frame can
/// arrive on when nothing is moving, and `fx::FX_FRAME` is what replaces them
/// as the wake interval when something is.
pub const IDLE_FRAME: Duration = Duration::from_millis(100);

/// The animation beat: the ticker tape, the throbbers, and the Atlas glyph
/// advance on it. Beside `IDLE_FRAME` rather than in `main` because it is one of
/// the cadences the pacing rule is about, and the two drifting apart is how the
/// glyph would start stepping between frames that never get drawn.
pub const TICK: Duration = Duration::from_millis(120);

/// How long a fetched backend catalog is worth reusing.
///
/// The owner's own cache window (`_LLM_CATALOG_TTL_SECONDS = 5.0` in
/// `qlab/ui/server.py`), matched rather than chosen: inside it the route serves
/// the same reading whatever this client does, so a second request buys nothing
/// and costs a round trip. Stated here rather than inside the accessor so the
/// number and its provenance sit together.
const CATALOG_TTL: Duration = Duration::from_secs(5);

/// Whether the loop owes the terminal a frame.
///
/// `now` is a parameter, never a clock read: a rule that called `elapsed()`
/// would decide against a different instant than the caller measured, and its
/// own test would race a descheduled thread. It lives beside the store rather
/// than in `main` so it can be tested without a terminal. `fx_active` renders
/// unconditionally: effects are sampled per frame, and the cadence they want is
/// the loop's wake interval (`fx::Fx::budget`), not a gate here.
pub fn should_render(dirty: bool, fx_active: bool, last_frame: Instant, now: Instant) -> bool {
    dirty || fx_active || now.saturating_duration_since(last_frame) >= IDLE_FRAME
}

/// A desk change worth animating.
///
/// Named for what happened on the desk, never for the effect that renders it —
/// the motion vocabulary is Task 15's to change without touching this diff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Trigger {
    RegimeChanged,
    DrawdownTierWorse,
    Halted,
    Resumed,
    ApprovalCreated,
    PhaseAdvanced,
    PlanExecuted,
    QuoteTick(String),
    ReadChanged,
    /// One durable audit event arrived on the stream, keyed by its event id so
    /// the row it landed on is the row that lights. Only the stream fires this:
    /// the same events also ride in every snapshot, and seeding a ring from a
    /// poll would flash thirty rows that have been on the record for hours.
    AuditEvent(String),
}

/// The eight views, in nav-rail order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ViewId {
    /// The desk manager's own pane: the conversation with Atlas, beside the
    /// evidence base it reasons from. First on the rail because the chat is
    /// the surface this client is named for — an operator's question should be
    /// one keystroke away. The *opening* view stays DESK: which pane a
    /// workstation opens on is a separate decision from where a pane sits,
    /// and the opening frame is pinned by golden tests as the desk.
    Atlas,
    #[default]
    Desk,
    Markets,
    Book,
    Research,
    /// The predictor board's own pane: every evaluated model, where RESEARCH
    /// keeps a one-row readout. Beside RESEARCH because the board is a
    /// research artifact — its models are visible here and runnable only
    /// through the owner's governed tool, never from this client.
    Predictors,
    Workforce,
    Audit,
    Settings,
}

impl ViewId {
    /// Nav order. The digit keys index this, so it is also the numbering an
    /// operator sees — the two cannot drift apart.
    pub const ALL: [ViewId; 9] = [
        ViewId::Atlas,
        ViewId::Desk,
        ViewId::Markets,
        ViewId::Book,
        ViewId::Research,
        ViewId::Predictors,
        ViewId::Workforce,
        ViewId::Audit,
        ViewId::Settings,
    ];

    /// At most five cells: the nav rail is eight wide and spends three of them
    /// on the active marker and the digit.
    pub fn label(self) -> &'static str {
        match self {
            ViewId::Atlas => "ATLAS",
            ViewId::Desk => "DESK",
            ViewId::Markets => "MKTS",
            ViewId::Book => "BOOK",
            ViewId::Research => "RSCH",
            ViewId::Predictors => "PRED",
            ViewId::Workforce => "WORK",
            ViewId::Audit => "AUDIT",
            ViewId::Settings => "SETT",
        }
    }

    /// The one-based position the digit keys use.
    pub fn index(self) -> usize {
        ViewId::ALL.iter().position(|v| *v == self).unwrap_or(0)
    }

    pub fn from_digit(digit: char) -> Option<ViewId> {
        let n = digit.to_digit(10)? as usize;
        if n == 0 {
            return None;
        }
        ViewId::ALL.get(n - 1).copied()
    }

    /// Cycling wraps in both directions: an operator holding Tab must never
    /// reach a wall and wonder whether the client stopped responding.
    pub fn next(self) -> ViewId {
        ViewId::ALL[(self.index() + 1) % ViewId::ALL.len()]
    }

    pub fn prev(self) -> ViewId {
        ViewId::ALL[(self.index() + ViewId::ALL.len() - 1) % ViewId::ALL.len()]
    }
}

/// Which surface owns the next keystroke. A view-local pane index is not
/// modeled until a view has more than one focusable pane.
///
/// One field rather than a flag per surface: "the command line is focused" and
/// "the help overlay is up" are answers to the same question, and two booleans
/// could say yes to both — a client typing into a field nobody can see behind an
/// overlay. Ctrl-C is above all of them, in the shell.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Focus {
    #[default]
    Content,
    Command,
    /// The help overlay, which is modal: it is what an operator opens when they
    /// have lost the keyboard, so it may not leave any of it behind a view.
    Help,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Nav {
    pub view: ViewId,
    pub focus: Focus,
}

/// What this client can currently see. Both start down: a surface that assumes
/// its feeds are up renders stale numbers as current for one poll interval.
///
/// The counts are what a dot cannot say. A feed that has dropped eleven times in
/// a minute and is up *right now* renders exactly like one that has been up all
/// morning, and those are very different desks to be reading numbers off.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Conn {
    pub owner: bool,
    pub stream: bool,
    /// How many times each feed has gone away since this client opened.
    ///
    /// Drops rather than reconnects, because a drop is the event that actually
    /// happened: the feed that went away and has not come back yet is still
    /// counted, where a reconnect count would silently stop at the last repair.
    pub owner_drops: u32,
    pub stream_drops: u32,
}

/// A payload the owner served and the model could not read.
///
/// Held rather than only logged: an owner serving garbage is reachable, so the
/// connection chips say everything is fine while nothing on the desk is. The
/// frame has to be able to say which of the two it is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Malformed {
    pub url: String,
    pub error: String,
}

/// Serde's message carries the whole path and can run long; the panel has a
/// line or two for it. Truncating at the point of record also keeps a broken
/// owner from growing the store by a decode message every three seconds.
const MALFORMED_ERROR_MAX: usize = 200;

/// How many audit events this client keeps.
///
/// Bounded because the bus is not. A governed desk writes an event per
/// approval, phase, verdict, fill and halt, and a client left up overnight
/// would otherwise hold every one of them for a pane that can draw thirty. One
/// hundred is a comfortable multiple of what AUDIT shows at any terminal height
/// this workstation supports, so the ring never drops a row still on screen.
///
/// The ring is *not* a second account of the audit log — the registry is, and
/// `/api/events` serves it. This is the recent window a live pane renders.
pub const EVENTS_RING: usize = 100;

/// One row of the durable audit bus, from either feed.
///
/// The snapshot and the stream carry the same events in different shapes
/// (`model::Event` and `bus::SseEvent`), and a pane that read both would have
/// two orderings and two dedup rules. They are normalised here instead, keyed
/// by the owner's own `event_id` — which is the registry's primary key, and
/// therefore the only thing that can say two arrivals are one event.
#[derive(Debug, Clone, PartialEq)]
pub struct AuditEvent {
    /// `None` for a frame the owner published without one. Such a row still
    /// renders — it happened — but it can never be deduplicated against.
    pub id: Option<String>,
    pub ts: Option<String>,
    pub kind: String,
    pub payload: Value,
}

/// One price the quote stream reported, stamped with when this client saw it.
///
/// Held beside the snapshot and never merged into it. The periodic `/api/tui`
/// poll rebuilds `market.assets` from the owner's cached valuations, so an
/// in-place merge is silently overwritten by an *older* price seconds later —
/// the Textual client's `_apply_quote_event` (`qlab/tui/app.py:1900`) accepts
/// that regression; this does not.
#[derive(Debug, Clone, PartialEq)]
pub struct QuoteMark {
    pub price: f64,
    pub change_1d: f64,
    pub at: Instant,
    /// The owner's own stamp on the frame this came off, which is what orders
    /// two marks against each other.
    ///
    /// Arrival cannot: a reconnect resumes from the cursor and replays whatever
    /// the outage held, so frames arrive in the owner's order but *later* than
    /// frames this client already has. Without this an older price overwrites a
    /// newer one every time the stream heals. ISO-8601 with a fixed offset, so
    /// lexical order is chronological order — `qlab/state/registry.py` writes
    /// the column that way and the SSE cursor already relies on it.
    pub ts: Option<String>,
}

/// What one asset is worth right now, after the overlay has had its say.
///
/// The one way a price reaches a frame. A surface that read `market.assets`
/// directly would render the poll's price and silently lose every quote that
/// arrived since it — which is the whole reason the overlay exists.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AssetView<'a> {
    pub ticker: &'a str,
    pub price: Option<f64>,
    pub change_1d: Option<f64>,
    /// When this client learned *this* price — the winning feed's stamp, not the
    /// snapshot's. `None` while nothing has ever arrived.
    pub at: Option<Instant>,
}

impl AssetView<'_> {
    /// Whether this cell's price has stopped being refreshed.
    ///
    /// Per cell, and against the feed that actually fed it. Keying every cell to
    /// the snapshot's age dimmed a whole tape of live quotes whenever the poller
    /// died — the numbers were current to the second and rendered as four
    /// minutes old, which is the exact lie the dimming exists to prevent, told
    /// in the other direction.
    ///
    /// The aggregate `STALE` chip stays on the snapshot's age on purpose: that
    /// one is about the desk as a whole, and the stream speaks for five prices
    /// out of everything a snapshot carries.
    pub fn stale(&self, after: Duration, now: Instant) -> bool {
        self.at
            .is_some_and(|at| now.saturating_duration_since(at) > after)
    }
}

/// The half of an asset row the quote stream does not speak to.
///
/// `price` and `change_1d` are deliberately absent. The overlay owns those two
/// facts, and a struct that carried them beside the snapshot's history would be
/// a second way to read them — which is exactly how a surface ends up rendering
/// the poll's price and silently losing every quote since. A grid that wants
/// both halves takes this and `asset_view(ticker)`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AssetFacts<'a> {
    pub ticker: &'a str,
    pub change_20d: Option<f64>,
    pub realized_vol: Option<f64>,
    /// Closing prices, oldest first.
    pub history: &'a [f64],
}

/// What this window may do to the desk.
///
/// Two words, and in the default build only one of them exists. `Operator` is
/// behind the `operator` feature, so a monitoring box does not hold a value it
/// could be assigned by a bug, a config read, or a stray `..Default::default()`
/// — the amber word is unreachable because the variant is not in the type.
///
/// Which one is held is derived from the owner's own posture on every snapshot
/// ([`Posture::from_desk`]): the feature says what the binary is capable of, the
/// desk says whether the operator armed it, and `--glass` lets this window
/// decline. A featured build on an unarmed desk reads `GLASS`, because the
/// question the chip answers is "can the next keystroke place an order", not
/// "which binary is this".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Posture {
    #[default]
    Glass,
    #[cfg(feature = "operator")]
    Operator,
}

impl Posture {
    /// What this window may do, derived from the desk rather than declared at
    /// launch. The one place the decision is made.
    ///
    /// Three conjuncts, in this order, and the order is the argument:
    ///
    /// 1. `featured` — the Cargo gate, outermost because it is the only one a
    ///    running process cannot influence. It is passed in rather than read
    ///    from `cfg!` here so both sides of it are testable in one leg; the
    ///    composition root supplies `cfg!(feature = "operator")`. The `cfg`
    ///    below is what makes it structural: in a `--no-default-features`
    ///    artifact `Posture::Operator` is not a value that exists, so no
    ///    argument to this function can return it.
    /// 2. `forced_glass` — `--glass`, the operator's own veto. It is *this*
    ///    window declining an authority the desk offers, so it must beat the
    ///    desk's answer, not lose to it.
    /// 3. `armed == Some(true)` — the owner's persisted posture. `Some(false)`
    ///    and `None` are both glass, but for different reasons: the first is an
    ///    answered desk, the second an unasked one, and arming on absence would
    ///    make an owner that lost the field into an armed workstation.
    pub fn from_desk(featured: bool, forced_glass: bool, armed: Option<bool>) -> Posture {
        let armed_desk = featured && !forced_glass && armed == Some(true);
        #[cfg(feature = "operator")]
        if armed_desk {
            return Posture::Operator;
        }
        #[cfg(not(feature = "operator"))]
        let _ = armed_desk;
        Posture::Glass
    }

    /// Whether an answer of "armed" could widen this window at all.
    ///
    /// Asked of [`Posture::from_desk`] rather than of a second copy of its
    /// first two conjuncts, so the door cannot come to disagree with the
    /// derivation about what a `--glass` window or a read-only build is. It is
    /// the question the arming step has to ask before it offers itself: a row
    /// that changes nothing about what this window may do is exactly the
    /// affordance the glass door refuses to draw.
    pub fn armable(featured: bool, forced_glass: bool) -> bool {
        Posture::from_desk(featured, forced_glass, Some(true)).writes()
    }

    /// The word on the status line. Not `Display`: this is a fixed badge with a
    /// fixed width, and a formatting impl invites a caller to pad it.
    pub fn label(self) -> &'static str {
        match self {
            Posture::Glass => "GLASS",
            #[cfg(feature = "operator")]
            Posture::Operator => "OPERATOR",
        }
    }

    /// Whether this window's keys can change the desk.
    ///
    /// The question every surface actually asks, and the reason it is asked of
    /// the *posture* rather than of `cfg!(feature = "operator")`. The feature
    /// says what the binary is capable of; the desk's posture says whether the
    /// operator armed it. A pane that keyed its hints or its branches off the feature
    /// alone would offer the execute key in a featured binary started without
    /// on an unarmed desk — which is precisely the window the status line is
    /// promising reads GLASS.
    ///
    /// In the default build this is `false` for the only value that exists, so
    /// every branch behind it is dead there and the compiler removes it.
    pub fn writes(self) -> bool {
        match self {
            Posture::Glass => false,
            #[cfg(feature = "operator")]
            Posture::Operator => true,
        }
    }
}

#[derive(Debug)]
pub struct Store {
    pub snapshot: Option<Snapshot>,
    pub regime_panel: Option<RegimePanel>,
    /// What the owner is registered to start, from `/api/atlas/templates`.
    ///
    /// Private with one reader for the reason `events_ring` is: the picker must
    /// not be able to grow a template the owner never registered, and a public
    /// `Vec` is a field anything could push to.
    templates: Vec<Template>,
    /// What the owner's backends serve, from the palette's own fetch.
    ///
    /// Private with one reader for the reason `templates` is: the model strip
    /// must not be able to offer a pair the owner never said it could run, and
    /// a public field is one anything could push to.
    backends: Option<LlmCatalog>,
    /// When that catalog arrived — this client's own clock, not the owner's
    /// `probed_at`.
    ///
    /// Two stamps for two questions. `probed_at` is when the *daemons* were
    /// asked, which is what SETTINGS renders an age from; this is when *this
    /// client* last asked the owner, which is the only one that can say whether
    /// asking again would learn anything. Time as data, as an `Instant`: the
    /// comparison is against this machine's own monotonic clock, and the one
    /// place this client compares two machines' wall clocks stays the one place
    /// (`format::since`).
    backends_at: Option<Instant>,
    /// The full predictor board, fetched when the PREDICTORS view opens.
    ///
    /// No arrival stamp beside it: the fetch is edge-triggered on entering the
    /// view, so "would asking again learn anything" is answered by the
    /// operator pressing `r`, not by a TTL this client would have to invent.
    predictor_detail: Option<PredictorDetail>,
    /// What the desk reads the news from, fetched when SETTINGS opens.
    ///
    /// No arrival stamp beside it either, and for `predictor_detail`'s reason:
    /// the fetch is edge-triggered on entering the pane and on `r` there, so
    /// there is no TTL for this client to invent.
    news: Option<NewsSettings>,
    /// The qualitative matrix, fetched on its own slow beat.
    ///
    /// `None` is "not asked yet or not answered yet". RESEARCH keeps that
    /// apart from an answered window with no rows in it: one says this client
    /// has not looked, the other says the desk's record is empty.
    qualitative: Option<QualitativeMatrix>,
    /// The newest chat timestamp at the moment `/clear` ran. The bus keeps
    /// every row and AUDIT still draws them — this window just stops drawing
    /// rows at or before the mark. A timestamp rather than a count, because
    /// the owner serves a *bounded* chat window: counting rows would keep
    /// hiding new arrivals once the window rotates under the mark.
    chat_cleared_through: Option<String>,
    pub nav: Nav,
    /// What the operator has typed into the command line, and what it said back.
    ///
    /// Beside `nav` rather than inside a view, because the line belongs to the
    /// whole workstation — and beside `nav.focus` in particular, which is the
    /// flag that says whether it owns the keyboard: a focus with no buffer under
    /// it is a caret nothing is typing into. Keeping it here is also what leaves
    /// a frame a pure function of (store, effects, instant), which every golden
    /// on this branch depends on.
    ///
    /// It is where the *operator* is looking, never what the desk says — the
    /// same line every view's cursor is on the right side of.
    pub cmd: CmdLine,
    /// How far the help overlay is scrolled.
    ///
    /// Meaningful only while `nav.focus` is `Help`; opening resets it, so a
    /// stale offset from a previous look can never decide what the overlay
    /// shows. Not an `Option` beside the focus state, because two fields that
    /// both claim to say whether the overlay is up can disagree.
    pub help_top: usize,
    pub conn: Conn,
    /// How old the numbers on screen may get before the frame says so.
    ///
    /// Data, not a constant in the renderer: it was a literal `10 s` beside a
    /// `3 s` poll, two facts about the same cadence with nothing tying them
    /// together. It is set from the poller's interval at startup, so a cadence
    /// change carries the threshold with it instead of quietly widening the
    /// window in which stale marks render as current.
    pub stale_after: Duration,
    /// When the snapshot on screen arrived. Absent means none ever has.
    ///
    /// Time as data: `apply` is told the instant, and the shell is told the
    /// instant it is drawing at, so "these numbers are 40 seconds old" is a
    /// subtraction the golden frames can pin rather than a clock read buried
    /// in a renderer.
    pub last_snapshot_at: Option<Instant>,
    /// When this client last heard the workforce itself speak. Absent means it
    /// never has.
    ///
    /// Arrival-stamped like `last_snapshot_at`, and from the *stream* only: a
    /// poll's rows are history the owner is re-serving (see `apply_snapshot`),
    /// and a liveness clock seeded from history would report a run as live
    /// because a five-minute-old row arrived in a fresh snapshot.
    ///
    /// Private, because the one thing it is for is an age, and an age is only
    /// honest against a `now` the caller was given. `last_agent_event_at` is
    /// the reader; `activity_line` does the subtraction.
    last_agent_event_at: Option<Instant>,
    /// This machine's wall clock in unix seconds, stamped by the runtime beside
    /// the frame's `Instant` and never read in a renderer.
    ///
    /// An `Instant` is monotonic and says nothing about *when*, so it cannot be
    /// compared with a stamp the owner wrote. Exactly one row needs that
    /// comparison — how old the model availability reading is — and
    /// `format::since` states what measuring across two machines' clocks costs
    /// and what it refuses rather than guess. Data, so a golden pins the age it
    /// renders instead of blessing whatever the suite measured; absent before
    /// the runtime's first iteration, and in every test that does not set one.
    pub wall: Option<i64>,
    /// The last payload that did not decode, cleared by the next that does.
    pub malformed: Option<Malformed>,
    /// Where this client is looking — the owner base every request goes to.
    ///
    /// Data rather than an environment read inside the renderer, for the reason
    /// every other fact on the frame is: a status line that called
    /// `base_from_env` would be a frame that is not a pure function of the
    /// store, and two tests setting `QLAB_UI_PORT` would race each other.
    /// Empty means nothing set it, and renders as absent like every other unset
    /// string in this client.
    pub base: String,
    /// What this window may do to the desk, re-derived from every snapshot.
    ///
    /// Here beside `base` — the store's other composition-root fact — rather
    /// than read by the renderer, for the same reason: a status line that
    /// consulted a flag or a `cfg!` directly would be a frame that is not a pure
    /// function of the store, and no golden test could then pin either word.
    ///
    /// Derived rather than set once, because the authority is the owner's and
    /// the owner can withdraw it: a desk disarmed from another window must not
    /// leave this one holding a write scope it no longer has. It starts `Glass`
    /// and stays there until a snapshot says otherwise.
    pub posture: Posture,
    /// `--glass`: this window declining an authority the desk may be offering.
    ///
    /// A composition-root fact, and the reason [`Posture::from_desk`] takes it
    /// as an argument rather than reading a flag: an operator who wants to
    /// watch an armed desk should not have to disarm it for everyone else.
    pub forced_glass: bool,
    /// Live prices, by ticker — read through `asset_view`, never rendered from
    /// directly. See `QuoteMark` for why this is an overlay and not a merge.
    pub quote_overlay: HashMap<String, QuoteMark>,
    /// How many stream frames this client could not read.
    ///
    /// A count rather than the frame itself: the parser already logs each one
    /// whole, and what the desk needs on screen is that the audit stream is
    /// dropping events at all. Task 16 gives it the toast; until then the status
    /// line is the only thing that can say it happened.
    pub stream_malformed_count: u32,
    /// The recent audit bus, oldest first, bounded at `EVENTS_RING`.
    ///
    /// Private so there is one reader (`audit_events`) and one writer
    /// (`record_audit`): the ordering and the dedup rule are the whole value of
    /// this field, and a pane that pushed to it directly would be free to
    /// disagree with both.
    events_ring: VecDeque<AuditEvent>,
    /// What the desk refused to book, by plan id.
    ///
    /// Held because a refusal is not visible anywhere else: the owner declines
    /// with HTTP 200 and writes no plan-state change, so the next snapshot
    /// looks exactly like the one before it. Without this the card an operator
    /// just pressed `x` on would go back to offering the key, silently.
    ///
    /// Cleared for a plan the moment that plan books, so a card can never carry
    /// a stale refusal beside a fill.
    #[cfg(feature = "operator")]
    pub refusals: HashMap<String, String>,
    /// The animation beat, counted rather than read from a clock. Every frame
    /// the shell draws is a pure function of the store, so the phase an
    /// automaton is at has to be state — a renderer that called `Instant::now`
    /// could not be pinned by a golden frame.
    pub tick: u64,
    /// Private on purpose: `take_dirty` is the only reader, so the flag cannot
    /// be observed by something that then forgets to clear it.
    dirty: bool,
    /// The startup door, while it is up.
    ///
    /// Here beside `cmd` and `help_top` — *where the operator is looking* —
    /// rather than in the runtime, which is what keeps a frame a pure function
    /// of (store, effects, instant) and lets a golden pin the door the way it
    /// pins every other surface.
    ///
    /// Private, with `take_door`/`keep_door`/`settle_door` as the whole
    /// protocol: the door has to be driven against the desk it is asking about,
    /// which means being taken out of the store that holds it, and a public
    /// field would let a caller put one back that had already been answered.
    door: Option<Door>,
    /// Whether a door has already been up in this run.
    ///
    /// The latch is load-bearing rather than tidy: the store-driven condition —
    /// an owner that answered and never said which desk this is — stays true
    /// for as long as that owner is up, so without this the door an operator
    /// just dismissed would be back on the next poll, for ever.
    door_settled: bool,
    /// `--pick`: this run was started to choose, so the door opens whatever the
    /// desk says.
    door_forced: bool,
}

impl Default for Store {
    /// The desk as this client opens it: nothing seen, both feeds down, and the
    /// staleness threshold the poller's own cadence implies.
    fn default() -> Self {
        Self::new(http::stale_after(http::POLL_INTERVAL))
    }
}

impl Store {
    pub fn new(stale_after: Duration) -> Self {
        Self {
            snapshot: None,
            regime_panel: None,
            templates: Vec::new(),
            backends: None,
            backends_at: None,
            predictor_detail: None,
            news: None,
            qualitative: None,
            chat_cleared_through: None,
            nav: Nav::default(),
            cmd: CmdLine::default(),
            help_top: 0,
            conn: Conn::default(),
            stale_after,
            last_snapshot_at: None,
            last_agent_event_at: None,
            wall: None,
            malformed: None,
            base: String::new(),
            posture: Posture::Glass,
            forced_glass: false,
            quote_overlay: HashMap::new(),
            stream_malformed_count: 0,
            events_ring: VecDeque::new(),
            #[cfg(feature = "operator")]
            refusals: HashMap::new(),
            tick: 0,
            dirty: false,
            door: None,
            door_settled: false,
            door_forced: false,
        }
    }

    // -- the startup door --------------------------------------------------

    /// `--pick`: ask which desk this is, whatever the owner says it already is.
    ///
    /// Called by the composition root before the first frame. It considers the
    /// door itself rather than only setting a flag, so a run started to choose
    /// opens on the question instead of on one frame of a desk the operator has
    /// not answered for yet.
    pub fn pick(&mut self) {
        self.door_forced = true;
        self.consider_door();
    }

    /// Open the door, if this desk needs one and none has been up yet.
    ///
    /// The predicate is [`Door::wanted`]; what "unchosen" means on the wire is
    /// [`Store::desk_unchosen`], directly below.
    fn consider_door(&mut self) {
        if self.door_settled || self.door.is_some() {
            return;
        }
        let answered = self.last_snapshot_at.is_some();
        // Two questions, one door. The arming one has its own trigger because
        // a desk can have named itself long ago and never have been asked
        // whether a window may write to it — which is every desk upgraded onto
        // an owner that serves a posture — and a door that only opened for an
        // unchosen *desk mode* would leave those with no way to arm at all.
        //
        // `answered` gates it for the reason it gates the other: absence
        // before the first poll is not an owner saying nobody chose.
        if Door::wanted(self.door_forced, answered, self.desk_unchosen())
            || (answered && self.asking_posture())
        {
            self.door = Some(Door::default());
            self.dirty = true;
        }
    }

    /// Whether the desk on the wire is one nobody has chosen.
    ///
    /// Two shapes, and they are two different silences:
    ///
    /// * **no `desk_mode` block at all** — an owner too old to serve one, or a
    ///   payload missing it. The desk this client is watching has not been
    ///   named to it, which is the arm the door shipped with.
    /// * **`chosen: false`** — the owner saying, in as many words, that the
    ///   concrete pair it is serving is the fallback nobody picked. This is the
    ///   state the door was specified against and could not observe until the
    ///   owner learned to say it.
    ///
    /// **`chosen` absent on a block that *is* there is not unchosen.** That is
    /// every owner built before the field existed, and guessing would open a
    /// modal over every desk that has already answered — a regression loud
    /// enough to make the field unshippable. Silence keeps the old reading.
    fn desk_unchosen(&self) -> bool {
        match self.desk_mode() {
            None => true,
            Some(mode) => mode.chosen == Some(false),
        }
    }

    /// The door, if one is up.
    pub fn door(&self) -> Option<&Door> {
        self.door.as_ref()
    }

    /// The door, out of the store, so it can be driven against the desk it is
    /// asking about.
    ///
    /// Taken rather than borrowed because a keystroke into the door reads the
    /// whole store — the pair the owner reports, the credential description,
    /// the catalog — and `&mut self.door` would hold the store hostage for all
    /// of it. The caller must put it back with [`Store::keep_door`] or settle
    /// it with [`Store::settle_door`]; a door dropped on the floor would leave
    /// the latch clear and `consider_door` free to open a second one.
    pub fn take_door(&mut self) -> Option<Door> {
        self.door.take()
    }

    /// Put a door that is still asking back.
    pub fn keep_door(&mut self, door: Door) {
        self.door = Some(door);
    }

    /// The door is answered. One per run, whatever the owner keeps saying.
    pub fn settle_door(&mut self) {
        self.door = None;
        self.door_settled = true;
        self.dirty = true;
    }

    /// Fold one event in and report what changed on the desk.
    ///
    /// `now` is the caller's instant, never a clock read here: the loop stamps
    /// arrival with the same instant it paces the frame against, so an age on
    /// screen and the pacing decision behind it cannot disagree.
    pub fn apply(&mut self, ev: AppEvent, now: Instant) -> Vec<Trigger> {
        let triggers = self.fold(ev, now);
        // After the fold and on every event, because what opens the door is
        // what the fold just learned — the owner's first answer, and whether it
        // named the desk. Here rather than in the runtime's loop for the reason
        // invariant 10 keeps teaching this crate: `main.rs` is in no test
        // binary, and a trigger only the binary could reach is a trigger
        // nothing can pin.
        self.consider_door();
        triggers
    }

    /// One event into the state. Everything the fold decides; nothing it owes
    /// afterwards — see `apply`, which is the caller and the only one.
    fn fold(&mut self, ev: AppEvent, now: Instant) -> Vec<Trigger> {
        match ev {
            AppEvent::Snapshot(snap) => return self.apply_snapshot(*snap, now),
            AppEvent::RegimePanel(panel) => {
                self.regime_panel = Some(panel);
                self.dirty = true;
            }
            // Replaced wholesale rather than merged: the owner's registry is
            // the whole set, and a template it stopped serving is one a picker
            // must stop offering.
            AppEvent::Templates(templates) => {
                self.templates = templates;
                self.dirty = true;
            }
            // Replaced wholesale for the same reason, and stamped with the
            // instant it arrived rather than with a clock read here — the fold
            // is told the instant the frame is paced against.
            AppEvent::Backends(catalog) => {
                self.backends = Some(catalog);
                self.backends_at = Some(now);
                self.dirty = true;
            }
            // Replaced wholesale like the catalog: the route serves the whole
            // newest board, and merging two boards would render rows from two
            // different runs as one evaluation nobody performed.
            AppEvent::PredictorDetail(detail) => {
                self.predictor_detail = Some(*detail);
                self.dirty = true;
            }
            // Replaced wholesale for the same reason: the route serves the
            // whole answer, and merging two of them would draw a catalog from
            // one reading beside a stack from another.
            AppEvent::News(settings) => {
                self.news = Some(*settings);
                self.dirty = true;
            }
            // Replaced wholesale for the same reason again: the route serves
            // one window's whole matrix, and merging two of them would draw a
            // name counted in one window beside a name counted in another,
            // under a single `window_hash` that describes neither.
            AppEvent::Qualitative(matrix) => {
                self.qualitative = Some(*matrix);
                self.dirty = true;
            }
            // A keystroke may move a selection and a resize moves everything;
            // both owe a frame even though neither is desk news. A mouse event
            // is a keystroke's shape: a wheel moved a scroll, a click moved
            // the nav.
            AppEvent::Key(_) | AppEvent::Mouse(_) | AppEvent::Resize => self.dirty = true,
            // The beat advances but does not dirty: the glyph is redrawn by the
            // idle heartbeat in the pacing rule, and dirtying here would force a
            // frame every 120 ms and make that rule decorative.
            AppEvent::Tick => self.tick = self.tick.wrapping_add(1),
            AppEvent::ConnUp(channel) => self.set_conn(channel, true),
            AppEvent::ConnDown(channel) => self.set_conn(channel, false),
            AppEvent::Sse(event) => return self.apply_sse(event, now),
            AppEvent::Http(HttpResult::Malformed { url, error }) => {
                // Fail loud, on screen. The log alone was not enough: an owner
                // that answers with a payload the model cannot read is up, so
                // every chip stays green and the frame says "waiting for the
                // first snapshot" for as long as the owner stays broken.
                tracing::error!(%url, %error, "owner payload did not decode");
                let next = Malformed {
                    error: error_head(&error),
                    url,
                };
                if self.malformed.as_ref() != Some(&next) {
                    self.malformed = Some(next);
                    self.dirty = true;
                }
            }
            // The store records only the half a surface has to keep showing:
            // what the desk refused. The toast for every outcome is the
            // runtime's (`toast::for_event`), because a box that disappears
            // after four seconds is not something the owner said.
            #[cfg(feature = "operator")]
            AppEvent::Wrote(outcome) => {
                use crate::bus::Wrote;
                match outcome {
                    Wrote::Refused {
                        plan_id,
                        blocked_by,
                        ..
                    } => {
                        self.refusals.insert(plan_id, blocked_by);
                    }
                    Wrote::Executed { plan_id } => {
                        self.refusals.remove(&plan_id);
                    }
                    // Nothing for the store to hold. A decision, a question, a
                    // workflow handle and a failed request all leave their
                    // trace in the owner's own record, which the next poll
                    // brings back — unlike a refusal, which the owner declines
                    // with a 200 and no state change, and which would otherwise
                    // be visible nowhere at all.
                    //
                    // A desk mode is the same: the owner persists the pair and
                    // serves it back in the next snapshot, and a client copy
                    // would be a second account of which desk this is.
                    //
                    // So is a login. `credentials_ok` reflows through the
                    // snapshot the write brings forward, so the alpaca row
                    // retones from the owner's own account of the credential
                    // rather than from this client remembering what it sent —
                    // and the two answers a form has to act on (a consent
                    // question, a refusal) belong to the form, which is where
                    // the pair it would re-send still is.
                    //
                    // And so is a model choice: the owner persists it and the
                    // next snapshot's `llm` block is what SETTINGS draws, so a
                    // client copy would be a second account of which minds the
                    // desk is using. Its refusal changes nothing at all.
                    // And so is the posture, emphatically: what this window may
                    // do is derived from the owner's own `posture` block on
                    // every snapshot, so a store that armed itself on its own
                    // write outcome would be the client-side latch
                    // `Posture::from_desk` exists to prevent.
                    //
                    // A started proposal and a refused one are the same: the
                    // owner moves the task's own status and serves it back in
                    // the `actionables` block, so the panel retones from the
                    // desk's record rather than from this client remembering
                    // what it asked for — and a refusal there changed nothing
                    // to remember.
                    // And an ask holds nothing here either, emphatically: the
                    // owner persisted the proposals, the next poll brings the
                    // `actionables` block back, and a client-side copy of what
                    // it just asked for would be a second account of a list the
                    // desk already owns.
                    // And so is a news stack. The owner's answer to
                    // `/api/news/settings` is what the card draws, and the
                    // write brings a fresh one of those forward — a client
                    // copy would be a second account of what the desk reads,
                    // and a wrong one on an offline desk, which resolves
                    // `synthetic` whatever was chosen.
                    Wrote::NewsSaved { .. }
                    | Wrote::NewsRefused { .. }
                    | Wrote::Armed { .. }
                    | Wrote::Proposed { .. }
                    | Wrote::ProposalStarted { .. }
                    | Wrote::ProposalRefused { .. }
                    | Wrote::Chose { .. }
                    | Wrote::ChoiceRefused { .. }
                    | Wrote::ApprovalOpened { .. }
                    | Wrote::Decided { .. }
                    | Wrote::Asked { .. }
                    | Wrote::Started { .. }
                    | Wrote::Pointed { .. }
                    | Wrote::LoggedIn { .. }
                    | Wrote::LoginNeedsConsent { .. }
                    | Wrote::LoginRefused { .. }
                    | Wrote::Tested { .. }
                    | Wrote::Failed { .. } => {}
                }
                self.dirty = true;
            }
        }
        Vec::new()
    }

    /// When the workforce itself was last heard, if it ever has been.
    ///
    /// The one input to the activity line beyond `driving`. Time as data: this
    /// hands back the stamp and nothing else, so the age is computed against a
    /// `now` the frame was given rather than against a clock read in a view.
    pub fn last_agent_event_at(&self) -> Option<Instant> {
        self.last_agent_event_at
    }

    /// The recent audit bus, **newest first** — the order a log is read in.
    pub fn audit_events(&self) -> impl DoubleEndedIterator<Item = &AuditEvent> {
        self.events_ring.iter().rev()
    }

    /// Every approval the owner is currently serving.
    ///
    /// The owner sends both actionable statuses: `pending`, which approve and
    /// reject bind to, and `approved`-unconsumed, which is what the execute
    /// gate consumes. A surface that filtered to one of them here would be
    /// deciding which key an operator is allowed to see.
    pub fn approvals(&self) -> &[Approval] {
        self.snapshot
            .as_ref()
            .map(|s| s.approvals.as_slice())
            .unwrap_or_default()
    }

    pub fn plans(&self) -> &[Plan] {
        self.snapshot
            .as_ref()
            .map(|s| s.plans.as_slice())
            .unwrap_or_default()
    }

    /// The workflows the owner is serving — the ten most recent, newest first.
    pub fn workflows(&self) -> &[Workflow] {
        self.snapshot
            .as_ref()
            .map(|s| s.workflows.as_slice())
            .unwrap_or_default()
    }

    /// The conversation with the desk manager, oldest first as the owner
    /// serves it (`atlas_chat`, limit 60).
    pub fn atlas_chat(&self) -> &[crate::model::Event] {
        let chat = self
            .snapshot
            .as_ref()
            .map(|s| s.atlas_chat.as_slice())
            .unwrap_or_default();
        let Some(mark) = self.chat_cleared_through.as_deref() else {
            return chat;
        };
        // ISO-8601 compares lexicographically, so "after the mark" is a
        // string comparison. A row with no stamp cannot be placed against the
        // mark and is shown — hiding what cannot be dated would be the silent
        // drop this pane exists to avoid.
        let at = chat
            .iter()
            .position(|e| match e.ts.as_deref() {
                Some(ts) => ts > mark,
                None => true,
            })
            .unwrap_or(chat.len());
        &chat[at..]
    }

    /// `/clear`: stop drawing every chat row this window currently shows.
    /// New rows appear as they arrive — the mark is a moment, not a count.
    pub fn clear_chat(&mut self) {
        self.chat_cleared_through = self
            .snapshot
            .as_ref()
            .and_then(|s| s.atlas_chat.iter().filter_map(|e| e.ts.clone()).max());
        self.dirty = true;
    }

    /// The predictor board summary, if the owner served one.
    pub fn predictors(&self) -> Option<&crate::model::Predictors> {
        self.snapshot.as_ref()?.predictors.as_ref()
    }

    /// The full predictor board, if the PREDICTORS view has fetched one.
    ///
    /// `None` is "not asked yet or not answered yet", never "the desk has no
    /// board" — the payload itself says that, via `status`, and the view keeps
    /// the two apart.
    pub fn predictor_detail(&self) -> Option<&PredictorDetail> {
        self.predictor_detail.as_ref()
    }

    /// What the desk reads the news from, if SETTINGS has fetched it.
    ///
    /// `None` is "not asked yet or not answered yet", never "this desk reads
    /// no news" — the payload's own `configured` says that, and the card keeps
    /// the two apart.
    pub fn news(&self) -> Option<&NewsSettings> {
        self.news.as_ref()
    }

    /// The qualitative matrix, if the beat has brought one back.
    ///
    /// `None` is "not asked yet or not answered yet", never "the record is
    /// empty" — the payload's own `rows` says that, and RESEARCH keeps the two
    /// apart: a blank pane cannot say which one it is.
    pub fn qualitative(&self) -> Option<&QualitativeMatrix> {
        self.qualitative.as_ref()
    }

    /// Today's proposals, as the owner last served them.
    ///
    /// An owner that serves no block and one that has minted no proposals
    /// answer the same way here, because the panel draws nothing for either —
    /// an empty box over a desk nobody has asked would read as a desk with
    /// nothing to do. The two are still different facts and the model keeps
    /// them apart (`Snapshot::actionables` is an `Option`); this is the reader
    /// for which they happen to coincide.
    pub fn actionables(&self) -> &[crate::model::ActionItem] {
        self.snapshot
            .as_ref()
            .and_then(|s| s.actionables.as_ref())
            .map(|acts| acts.items.as_slice())
            .unwrap_or_default()
    }

    /// What the owner's coordinator is doing, if it said.
    ///
    /// Registering a workflow is not running it — `driving` is the only
    /// evidence that phases are actually advancing — so a pane that showed a
    /// pipeline without this would render a parked run and a working one
    /// identically.
    pub fn coordinator(&self) -> Option<&Coordinator> {
        self.snapshot
            .as_ref()?
            .atlas_heartbeat
            .as_ref()?
            .coordinator
            .as_ref()
    }

    /// The registered templates, in the owner's own order.
    pub fn templates(&self) -> &[Template] {
        &self.templates
    }

    /// What the desk is pointed at, if the owner said.
    ///
    /// One reader for a fact two surfaces draw — the status line's chip and the
    /// SETTINGS card — so they cannot disagree about which desk this is.
    pub fn desk_mode(&self) -> Option<&DeskMode> {
        self.snapshot.as_ref()?.desk_mode.as_ref()
    }

    /// Whether the owner says this desk is armed.
    ///
    /// `None` is absence, not "no": an owner too old to serve a posture block,
    /// a payload that lost it, or a field the owner left null. The decision
    /// table in [`Posture::from_desk`] treats absence and `Some(false)` the
    /// same way — glass — but they are different facts, and the door needs to
    /// tell them apart via [`Store::posture_chosen`].
    pub fn posture_armed(&self) -> Option<bool> {
        self.snapshot.as_ref()?.posture.as_ref()?.armed
    }

    /// Whether anyone has ever answered the posture question on this desk.
    ///
    /// `Some(false)` is the owner saying, in as many words, that the read-only
    /// posture it is serving is the default nobody picked — which is what
    /// Task 3's door asks about.
    pub fn posture_chosen(&self) -> Option<bool> {
        self.snapshot.as_ref()?.posture.as_ref()?.chosen
    }

    /// Whether the startup door still owes this desk the arming question.
    ///
    /// Three conjuncts, and each one removes a window the question would be a
    /// lie to:
    ///
    /// * `!posture.writes()` — a window the desk has already armed is not one
    ///   that has to be asked, and asking it would be a modal over a desk that
    ///   answered.
    /// * [`Posture::armable`] — a read-only artifact and a `--glass` window
    ///   are both windows an answer of "armed" would change nothing about.
    ///   Offering the row anyway is the claim the glass door exists to refuse.
    /// * `posture_chosen() == Some(false)` — the rule the door is specified by,
    ///   and the one reader of the owner's `chosen` flag. `Some(false)` is a
    ///   desk nobody answered for, which is the only case the question can be
    ///   answered in. `None` is *not* that: `posture_payload` always emits both
    ///   booleans, so absence means an owner too old to serve the block — which
    ///   is the same owner too old to serve `POST /api/desk/posture`. Asking it
    ///   is not harmless: the answer 404s, the door is kept because nothing set
    ///   `closed`, and the modal repeats the failure on every Enter.
    ///
    /// Read live rather than latched, which is what makes the answer the
    /// owner's: the question is up until a snapshot says somebody answered it,
    /// so the keystroke that sends the answer is not the thing that closes it.
    pub fn asking_posture(&self) -> bool {
        !self.posture.writes()
            && Posture::armable(cfg!(feature = "operator"), self.forced_glass)
            && self.posture_chosen() == Some(false)
    }

    /// The allocation policy the paper book is run under.
    pub fn policy(&self) -> Option<&Policy> {
        self.snapshot.as_ref()?.policy.as_ref()
    }

    /// Health and authority facts, as the owner reports them.
    pub fn system(&self) -> Option<&System> {
        self.snapshot.as_ref()?.system.as_ref()
    }

    /// Which minds the desk is using, and when it last asked whether they can
    /// serve. Absent is an owner that sent no routing at all — not a desk
    /// running on nothing.
    pub fn llm(&self) -> Option<&LlmConfig> {
        self.snapshot.as_ref()?.llm.as_ref()
    }

    /// What each backend serves, as the last fetch found it. Absent is a desk
    /// this client has not asked yet — which is not a desk with no backends.
    pub fn backends(&self) -> Option<&LlmCatalog> {
        self.backends.as_ref()
    }

    /// Whether asking the owner what its backends serve could learn anything.
    ///
    /// The window is the owner's own (`_LLM_CATALOG_TTL_SECONDS = 5.0`): inside
    /// it the route answers out of its cache, so a second request cannot return
    /// a different reading and is pure cost — a probe per daemon on the owner's
    /// side and a round trip on this one. Outside it, the palette asks again,
    /// because a daemon that has come up since is exactly what an operator
    /// opening the model scope is looking for.
    pub fn wants_backends(&self, now: Instant) -> bool {
        match self.backends_at {
            None => true,
            Some(at) => now.saturating_duration_since(at) >= CATALOG_TTL,
        }
    }

    /// The newest ablation, ranked by Sharpe as the owner ranked it.
    ///
    /// The owner's order is kept rather than re-sorted here: arms it could not
    /// score sort last instead of claiming a rank, and a client that sorted
    /// again would have to invent a rule for the absent ones.
    pub fn leaderboard(&self) -> &[LeaderboardRow] {
        self.snapshot
            .as_ref()
            .map(|s| s.leaderboard.as_slice())
            .unwrap_or_default()
    }

    /// The research run ledger, newest first.
    pub fn runs(&self) -> &[Run] {
        self.snapshot
            .as_ref()
            .map(|s| s.runs.as_slice())
            .unwrap_or_default()
    }

    /// The algorithm catalog, in the owner's own order — which is the catalog's
    /// declaration order, not a ranking.
    pub fn algorithms(&self) -> &[Algorithm] {
        self.snapshot
            .as_ref()
            .map(|s| s.algorithms.as_slice())
            .unwrap_or_default()
    }

    /// The approval that could authorise booking `plan_id`, if the owner is
    /// serving one.
    ///
    /// `approved` and unconsumed, which is exactly the gate's own precondition
    /// (`check_approval_for_execution`). This client does not re-check the
    /// expiry or the book revision: those are the owner's, they change under
    /// this client between polls, and a second copy of the gate here would
    /// drift from the one that actually decides.
    pub fn covering_approval(&self, plan_id: &str) -> Option<&Approval> {
        self.approvals().iter().find(|a| {
            a.plan_id.as_deref() == Some(plan_id)
                && a.status.as_deref() == Some("approved")
                && a.consumed_at.is_none()
        })
    }

    /// Any approval bound to this plan, whatever its state — what a card says
    /// when it cannot offer the key.
    pub fn approval_for(&self, plan_id: &str) -> Option<&Approval> {
        self.approvals()
            .iter()
            .find(|a| a.plan_id.as_deref() == Some(plan_id))
    }

    /// Record one audit row, and say whether it was new.
    ///
    /// Deduplicated on the owner's `event_id` because both feeds carry the same
    /// events: the stream delivers one and then nudges the poller, so the very
    /// next snapshot contains it again. A ring that took both would show every
    /// governance event twice, which reads as the desk having done it twice.
    fn record_audit(&mut self, event: AuditEvent) -> bool {
        if let Some(id) = event.id.as_deref() {
            if self
                .events_ring
                .iter()
                .any(|held| held.id.as_deref() == Some(id))
            {
                return false;
            }
        }
        self.events_ring.push_back(event);
        while self.events_ring.len() > EVENTS_RING {
            self.events_ring.pop_front();
        }
        self.dirty = true;
        true
    }

    /// Read the repaint flag and clear it in one move.
    pub fn take_dirty(&mut self) -> bool {
        std::mem::take(&mut self.dirty)
    }

    /// What one asset is worth right now.
    ///
    /// The overlay wins while its stamp is at least as new as the snapshot's
    /// arrival, and the snapshot takes over once a later one lands. "At least"
    /// rather than "after" is load-bearing: the loop stamps a whole drain with
    /// one instant, so a quote and a poll delivered in the same batch carry the
    /// same stamp — and the stream is the fresher account of a price.
    pub fn asset_view<'a>(&'a self, ticker: &'a str) -> AssetView<'a> {
        let asset = self.market_asset(ticker);
        let mark = self.quote_overlay.get(ticker).filter(|mark| {
            self.last_snapshot_at
                .is_none_or(|arrived| mark.at >= arrived)
        });
        match mark {
            Some(mark) => AssetView {
                ticker,
                price: Some(mark.price),
                change_1d: Some(mark.change_1d),
                at: Some(mark.at),
            },
            None => AssetView {
                ticker,
                price: asset.and_then(|a| a.price),
                change_1d: asset.and_then(|a| a.change_1d),
                at: self.last_snapshot_at,
            },
        }
    }

    /// Every asset the last snapshot carried, in its order, each resolved
    /// through `asset_view`.
    ///
    /// Quadratic in the universe on purpose: it is a dozen rows, and one
    /// resolution rule with one call site is worth more here than a map build
    /// that could drift from the rule `asset_view` applies.
    pub fn asset_views(&self) -> Vec<AssetView<'_>> {
        self.market_assets()
            .iter()
            .filter_map(|asset| text(asset.ticker.as_ref()))
            .map(|ticker| self.asset_view(ticker))
            .collect()
    }

    /// Every asset the last snapshot carried, in its order, minus the two
    /// fields the overlay owns. Same universe and same order as `asset_views`,
    /// so a grid can zip the two halves of a row by position.
    pub fn asset_facts(&self) -> Vec<AssetFacts<'_>> {
        self.market_assets()
            .iter()
            .filter_map(|asset| {
                Some(AssetFacts {
                    ticker: text(asset.ticker.as_ref())?,
                    change_20d: asset.change_20d,
                    realized_vol: asset.realized_vol,
                    history: &asset.history,
                })
            })
            .collect()
    }

    /// Every symbol this desk holds a cursor over: the quoted universe, in the
    /// owner's order, then anything the book holds that is not in it.
    ///
    /// Both halves, because both are selectable. A position outside the polled
    /// universe still has a blotter row — that is a book held wider than the
    /// data plane, which happens — and a command line that only knew the market
    /// section would refuse to select a row an operator can see.
    ///
    /// One rule with one reader, so "is this ticker on this desk" cannot be
    /// answered two ways by two surfaces.
    pub fn universe(&self) -> Vec<&str> {
        let mut out: Vec<&str> = self
            .market_assets()
            .iter()
            .filter_map(|asset| text(asset.ticker.as_ref()))
            .collect();
        for held in self
            .snapshot
            .as_ref()
            .and_then(|s| s.live_portfolio.as_ref())
            .map(|b| b.positions.as_slice())
            .unwrap_or_default()
            .iter()
            .filter_map(|p| text(p.ticker.as_ref()))
        {
            if !out.contains(&held) {
                out.push(held);
            }
        }
        out
    }

    fn market_assets(&self) -> &[Asset] {
        self.snapshot
            .as_ref()
            .and_then(|s| s.market.as_ref())
            .map(|m| m.assets.as_slice())
            .unwrap_or_default()
    }

    fn market_asset(&self, ticker: &str) -> Option<&Asset> {
        self.market_assets()
            .iter()
            .find(|a| text(a.ticker.as_ref()) == Some(ticker))
    }

    /// Fold one stream frame in.
    ///
    /// Three lanes. A `quote` is a price and lands in the overlay; a
    /// `stream.malformed` is a frame this client could not read and is counted;
    /// everything else is a durable audit row and lands in the ring AUDIT
    /// renders.
    ///
    /// The ring is not a second account of the desk. The events it holds still
    /// nudge the poller (`net::sse::REFETCH_KINDS`), and the snapshot that
    /// arrives is what says what the desk now *is* — this says what happened,
    /// in the order it happened, which no aggregate can.
    fn apply_sse(&mut self, event: SseEvent, now: Instant) -> Vec<Trigger> {
        match event.kind.as_str() {
            "quote" => self.apply_quote(&event.payload, event.ts.as_deref(), now),
            "stream.malformed" => {
                self.stream_malformed_count = self.stream_malformed_count.saturating_add(1);
                self.dirty = true;
                Vec::new()
            }
            _ => {
                let id = event.id.clone();
                let spoke = Self::is_agent_word(&event);
                let landed = self.record_audit(AuditEvent {
                    id: event.id,
                    ts: event.ts,
                    kind: event.kind,
                    payload: event.payload,
                });
                // A row that is not new is not a sign of life either: a replay
                // after a reconnect would otherwise report a dead run as having
                // just spoken.
                if landed && spoke {
                    self.last_agent_event_at = Some(now);
                }
                // Only a row that is actually new lights up. A replay after a
                // reconnect delivers events this client already holds, and
                // flashing those would announce old news as it arrived.
                match (landed, id) {
                    (true, Some(id)) => vec![Trigger::AuditEvent(id)],
                    _ => Vec::new(),
                }
            }
        }
    }

    /// Whether one stream row is the workforce speaking, rather than the desk
    /// recording something about it.
    ///
    /// `tool_start` and `text` only, and that is a deliberate floor rather than
    /// an oversight. The coordinator's `session` and `task_progress` kinds were
    /// removed from the durable bus because forty-two of sixty rows were
    /// liveness noise burying the reasoning; deriving liveness from a heartbeat
    /// would re-admit exactly that, one layer down, and a run that pinged
    /// forever while saying nothing would read as a working desk.
    fn is_agent_word(event: &SseEvent) -> bool {
        event.kind == "atlas_coordinator_event"
            && matches!(
                event.payload.get("event_kind").and_then(Value::as_str),
                Some("tool_start" | "text")
            )
    }

    /// Merge a quote frame into the overlay, and say which rows actually moved.
    ///
    /// Fail loud, not fatal: the owner publishes every row with a ticker, a
    /// price, and a change (`_publish_quote_event`, `qlab/ui/server.py:2129`),
    /// so a row missing one is a broken contract worth naming — but one bad row
    /// must not cost the frame the readable rows belong to, and must never be
    /// read as a price.
    ///
    /// `ts` is the owner's stamp on the frame, and it is what decides whether a
    /// row may land at all — see `QuoteMark::ts`. A frame older than the mark it
    /// would replace is dropped whole rather than merged: it is not new
    /// information about the price, it is the same feed catching up.
    fn apply_quote(&mut self, payload: &Value, ts: Option<&str>, now: Instant) -> Vec<Trigger> {
        let Some(rows) = payload.get("rows").and_then(Value::as_array) else {
            tracing::warn!(
                payload = %payload,
                "quote event carried no rows array"
            );
            return Vec::new();
        };

        let mut out = Vec::new();
        for row in rows {
            let ticker = row
                .get("ticker")
                .and_then(Value::as_str)
                .filter(|t| !t.is_empty());
            let price = finite(row.get("price"));
            let change_1d = finite(row.get("change_1d"));
            let (Some(ticker), Some(price), Some(change_1d)) = (ticker, price, change_1d) else {
                tracing::warn!(row = %row, "quote row needs ticker, price, and change_1d");
                continue;
            };

            let incumbent = self.quote_overlay.get(ticker);
            // A reconnect resumes from the cursor and replays the outage, so a
            // frame can arrive now and be about a moment before the price
            // already on screen. Only the owner's own stamp can tell, and only
            // when both sides carry one — an unstamped frame is not evidence of
            // anything and is taken at face value, as the cursor does.
            if let (Some(held), Some(next)) = (incumbent.and_then(|m| m.ts.as_deref()), ts) {
                if next < held {
                    tracing::debug!(%ticker, %next, %held, "a replayed quote is older than the mark it would replace");
                    continue;
                }
            }

            // Exact comparison on purpose: the question is whether the owner
            // sent a different number, not whether two numbers are close. The
            // owner republishes the same row on its own beat, and a tick per
            // publish rather than per move would leave every row permanently
            // lit — a highlight that never goes out says nothing.
            let moved =
                incumbent.is_none_or(|prev| prev.price != price || prev.change_1d != change_1d);
            if moved {
                out.push(Trigger::QuoteTick(ticker.to_string()));
                self.dirty = true;
            }
            // The stamp advances either way: an unchanged price that was just
            // reconfirmed is still the newest thing known about it, and letting
            // the stamp go stale would hand the row back to the poll.
            self.quote_overlay.insert(
                ticker.to_string(),
                QuoteMark {
                    price,
                    change_1d,
                    at: now,
                    ts: ts.map(str::to_string),
                },
            );
        }
        out
    }

    /// Whether the desk is halted, by the one rule the client has for it.
    ///
    /// The rule itself is `halted` below — the live book decides, and the
    /// reconciled book answers only when it has not been marked. Exposed rather
    /// than re-derived per surface for the reason `mood` states: the glyph and
    /// the BOOK ribbon must never disagree about whether the desk is halted, and
    /// two spellings of "the live book decides" is how they would come to.
    /// `None` is its own fact — no book has said either way.
    pub fn halted(&self) -> Option<bool> {
        self.snapshot.as_ref().and_then(halted)
    }

    /// The manager's mood, derived from desk facts rather than set.
    ///
    /// Ported from `Desk::mood`. Derived is the whole point: an animation that
    /// can say "working" while the book is halted is worse than no animation.
    /// It lives here because the halt rule (the live book decides) is a store
    /// rule, and a view that re-derived it would be a second source for it.
    pub fn mood(&self) -> Mood {
        let Some(snapshot) = &self.snapshot else {
            return Mood::Dormant;
        };
        Mood::from_desk(
            halted(snapshot).unwrap_or(false),
            snapshot
                .atlas_heartbeat
                .as_ref()
                .and_then(|b| b.coordinator.as_ref())
                .and_then(|c| c.driving)
                .unwrap_or(false),
            snapshot
                .atlas
                .as_ref()
                .and_then(|a| text(a.mode.as_ref()))
                .unwrap_or_default(),
        )
    }

    /// Diff the incoming snapshot against the one it replaces.
    ///
    /// A first snapshot is diffed against nothing, and nothing normalises to
    /// absent — so the first payload announces the state it arrives in. That is
    /// the point: an operator who opens this client onto a halted desk has to
    /// see the halt, not a quiet red number.
    fn apply_snapshot(&mut self, next: Snapshot, now: Instant) -> Vec<Trigger> {
        let mut out = Vec::new();
        let prev = self.snapshot.as_ref();

        // Absent halt means not halted — the same honest default the desk
        // renders. A flag that *stopped* being reported is missing information,
        // not a resume, so it clears nothing.
        let was_halted = prev.and_then(halted).unwrap_or(false);
        match halted(&next) {
            Some(true) if !was_halted => out.push(Trigger::Halted),
            Some(false) if was_halted => out.push(Trigger::Resumed),
            _ => {}
        }
        if changed(prev.and_then(robust_state), robust_state(&next)) {
            out.push(Trigger::RegimeChanged);
        }
        // Only worse. A guardrail relaxing is good news and needs no alarm, and
        // a desk oscillating across a threshold would otherwise fire on every
        // crossing in both directions.
        if tier_rank(drawdown_tier(&next)) > tier_rank(prev.and_then(drawdown_tier)) {
            out.push(Trigger::DrawdownTierWorse);
        }
        if changed(prev.and_then(read_as_of), read_as_of(&next)) {
            out.push(Trigger::ReadChanged);
        }
        // An approval id the last payload did not carry. Ids rather than a
        // count: a queue that gained one and lost one is still a new decision
        // waiting, and a status moving `pending` → `approved` keeps its id and
        // is therefore not a new request.
        //
        // The first snapshot announces whatever it arrives holding, exactly as
        // the halt does: the request was created before this client was
        // looking, and a human decision waiting is not something to open
        // quietly.
        if next.approvals.iter().any(|a| {
            let Some(id) = text(a.approval_id.as_ref()) else {
                return false;
            };
            !prev.is_some_and(|p| {
                p.approvals
                    .iter()
                    .any(|held| text(held.approval_id.as_ref()) == Some(id))
            })
        }) {
            out.push(Trigger::ApprovalCreated);
        }
        // A plan that left the checked state for a booked one. The registry has
        // no `executed` state — `qlab/trader/plan.py` walks a plan
        // `checked` → `submitted` → `filled` → `reconciled`, all three inside
        // one call — so the transition worth announcing is *into that set*,
        // whichever member the poll happens to catch it at.
        if next.plans.iter().any(|plan| {
            let Some(id) = text(plan.plan_id.as_ref()) else {
                return false;
            };
            booked(text(plan.state.as_ref()))
                && !prev.is_some_and(|p| {
                    p.plans.iter().any(|held| {
                        text(held.plan_id.as_ref()) == Some(id) && booked(text(held.state.as_ref()))
                    })
                })
        }) {
            out.push(Trigger::PlanExecuted);
        }
        // A phase of a workflow this client was already watching changed state.
        // Only workflows on *both* payloads count — see `phase_advanced` — which
        // is also what keeps the first snapshot quiet.
        if prev.is_some_and(|held| phase_advanced(held, &next)) {
            out.push(Trigger::PhaseAdvanced);
        }
        // The bus rows the payload carries, seeded silently: these are the same
        // events the stream delivers, and the stream is what says one *just*
        // arrived. Served oldest first (`registry.read_events`), which is the
        // order the ring holds them in.
        for event in &next.events {
            self.record_audit(AuditEvent {
                id: text(event.event_id.as_ref()).map(str::to_string),
                ts: text(event.ts.as_ref()).map(str::to_string),
                kind: text(event.kind.as_ref()).unwrap_or_default().to_string(),
                payload: event.payload.clone().unwrap_or(Value::Null),
            });
        }

        self.snapshot = Some(next);
        self.last_snapshot_at = Some(now);
        // Once per snapshot, from the payload just installed. The owner is the
        // authority on the desk's posture, so this window's scope follows it in
        // both directions — a desk disarmed from another client disarms this
        // frame at the next poll rather than at the next restart.
        self.posture = Posture::from_desk(
            cfg!(feature = "operator"),
            self.forced_glass,
            self.posture_armed(),
        );
        // A payload that decodes retires the last one that did not. Leaving it
        // set would leave a recovered owner accused on every later frame.
        self.malformed = None;
        self.dirty = true;
        out
    }

    fn set_conn(&mut self, channel: Channel, up: bool) {
        let (slot, drops) = match channel {
            Channel::Owner => (&mut self.conn.owner, &mut self.conn.owner_drops),
            Channel::Stream => (&mut self.conn.stream, &mut self.conn.stream_drops),
        };
        if *slot != up {
            // Counted on the edge down, so a flapping feed is counted once per
            // outage rather than once per poll that found it gone.
            if !up {
                *drops = drops.saturating_add(1);
            }
            *slot = up;
            self.dirty = true;
        }
    }
}

/// A JSON number this client can render. `NaN` and the infinities are not
/// prices, and letting one through would put `--` on the tape while the flash
/// said something had moved.
fn finite(value: Option<&Value>) -> Option<f64> {
    value?.as_f64().filter(|v| v.is_finite())
}

/// A trigger fires when the new value is present and differs. An absent new
/// value is the owner declining to say, which is not a transition.
fn changed(prev: Option<&str>, next: Option<&str>) -> bool {
    next.is_some() && prev != next
}

/// The first line of a decode error, short enough to render.
///
/// Serde reports the failure and then the path it walked; the first line is the
/// part that says what is wrong, and the rest is why the panel would otherwise
/// scroll off the frame.
fn error_head(error: &str) -> String {
    let line = error.lines().next().unwrap_or_default().trim();
    match line.char_indices().nth(MALFORMED_ERROR_MAX) {
        Some((cut, _)) => format!("{}…", &line[..cut]),
        None => line.to_string(),
    }
}

/// The live book decides the halt: it is the one marked to the tape, and the
/// reconciled book can lag it by a full valuation.
fn halted(snap: &Snapshot) -> Option<bool> {
    snap.live_portfolio
        .as_ref()
        .and_then(|p| p.halted)
        .or_else(|| snap.portfolio.as_ref().and_then(|p| p.halted))
}

/// The guarded state the desk acts on, not the raw detector label — the two
/// disagree on purpose, and only the guarded one is worth animating.
fn robust_state(snap: &Snapshot) -> Option<&str> {
    text(snap.market.as_ref()?.regime.as_ref()?.robust_state.as_ref())
}

fn read_as_of(snap: &Snapshot) -> Option<&str> {
    text(snap.atlas_read.as_ref()?.as_of.as_ref())
}

fn drawdown_tier(snap: &Snapshot) -> Option<&str> {
    text(snap.stress.as_ref()?.drawdown_tier.as_ref())
}

/// Whether a plan state means the desk sent it to a broker.
///
/// `checked` is a plan that passed the referee and is waiting on a human;
/// everything below is a plan that has been submitted. `qlab/trader/plan.py`
/// writes all three inside one `execute_plan`, so a three-second poll can land
/// on any of them and none is more "executed" than the others.
pub fn booked(state: Option<&str>) -> bool {
    matches!(
        state,
        Some("submitted") | Some("filled") | Some("reconciled")
    )
}

/// Whether any workflow **both** payloads carry has a step in a different state.
///
/// Three rules, and each of them is a thing that would otherwise fire wrongly.
///
/// *Only workflows on both sides.* A workflow the last payload did not carry is
/// a **start**, not an advance — and registering a workflow is not running it,
/// so a run whose phases are all still `queued` would announce progress it has
/// not made. It is also what keeps the first snapshot quiet without a special
/// case: diffed against nothing, every workflow is new. `workflow_started` on
/// the stream is what says one began.
///
/// *Keyed on `step_id`, not on `current_phase`.* Concurrent phases finish out of
/// seq order — the registry says so in `update_workflow_phase` — so the workflow
/// row's `current_phase` can stand still while a step underneath it completes.
///
/// *Any change of state, in either direction.* `working` → `interrupted` moved
/// the pipeline exactly as much as `working` → `done` did, and a pane that
/// animated only forward progress would be silent on the transition an operator
/// most needs to see.
fn phase_advanced(prev: &Snapshot, next: &Snapshot) -> bool {
    next.workflows.iter().any(|now| {
        let Some(id) = text(now.workflow_id.as_ref()) else {
            return false;
        };
        let Some(was) = prev
            .workflows
            .iter()
            .find(|held| text(held.workflow_id.as_ref()) == Some(id))
        else {
            return false;
        };
        now.steps.iter().any(|step| {
            let Some(step_id) = text(step.step_id.as_ref()) else {
                return false;
            };
            match was
                .steps
                .iter()
                .find(|held| text(held.step_id.as_ref()) == Some(step_id))
            {
                Some(held) => text(held.status.as_ref()) != text(step.status.as_ref()),
                // A step a known workflow did not have before. Phases are
                // written when the workflow is created, so this is a graph that
                // grew under the operator — which is news whatever caused it.
                None => true,
            }
        })
    })
}

/// The drawdown tiers in the owner's own order, worst last.
///
/// `qlab/trader/mandate.py::tier` classifies a trailing drawdown into exactly
/// these four, ascending: `none` below the warning threshold, then `warning`,
/// `control`, `breaker`. Ranking them is what lets the diff say *worse* rather
/// than merely *different* — the only direction that deserves an alarm.
///
/// An absent or unrecognised tier ranks with `none`, which means it can never
/// fire this trigger. A tier that stopped being reported is missing information
/// rather than a recovery, and a tier this client has never heard of is a
/// contract change that the rail already shows verbatim beside `tier`.
fn tier_rank(tier: Option<&str>) -> u8 {
    match tier {
        Some("warning") => 1,
        Some("control") => 2,
        Some("breaker") => 3,
        _ => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bus::{AppEvent, Channel};
    use crate::model::Snapshot;
    use serde_json::json;

    /// `apply` with an instant the test does not care about. The arrival-stamp
    /// and staleness tests pass their own; everything else only needs the fold.
    fn apply(store: &mut Store, ev: AppEvent) -> Vec<Trigger> {
        store.apply(ev, Instant::now())
    }

    fn snap(value: serde_json::Value) -> AppEvent {
        // Through the real decoder, not a hand-built struct: a diff that reads a
        // field the owner never fills is a bug the fixture path would hide.
        AppEvent::Snapshot(Box::new(serde_json::from_value::<Snapshot>(value).unwrap()))
    }

    // -- the posture decision table ----------------------------------------

    /// Both sides of every conjunct in [`Posture::from_desk`].
    ///
    /// Only in the featured leg: `Posture::Operator` is not a value that exists
    /// in a glass build, so the row that says "armed" cannot even be written
    /// there. The glass leg pins the same function from the other end, below.
    #[cfg(feature = "operator")]
    #[test]
    fn a_desk_arms_only_when_the_binary_can_and_the_operator_said_so() {
        use Posture::*;
        assert_eq!(Posture::from_desk(true, false, Some(true)), Operator);
        assert_eq!(Posture::from_desk(true, false, Some(false)), Glass); // owner says no
        assert_eq!(Posture::from_desk(true, false, None), Glass); // never asked
        assert_eq!(Posture::from_desk(true, true, Some(true)), Glass); // --glass wins
        assert_eq!(Posture::from_desk(false, false, Some(true)), Glass); // cargo gate wins
    }

    /// A glass artifact has one answer whatever the desk says.
    #[cfg(not(feature = "operator"))]
    #[test]
    fn a_glass_artifact_cannot_be_armed_by_any_desk() {
        assert_eq!(Posture::from_desk(true, false, Some(true)), Posture::Glass);
        assert_eq!(Posture::from_desk(false, true, None), Posture::Glass);
    }

    #[test]
    fn the_posture_block_is_read_from_the_snapshot_and_absence_is_not_a_no() {
        let mut store = Store::default();
        assert_eq!(store.posture_armed(), None);
        assert_eq!(store.posture_chosen(), None);

        apply(
            &mut store,
            snap(json!({"posture": {"armed": true, "chosen": true}})),
        );
        assert_eq!(store.posture_armed(), Some(true));
        assert_eq!(store.posture_chosen(), Some(true));

        apply(
            &mut store,
            snap(json!({"posture": {"armed": false, "chosen": false}})),
        );
        assert_eq!(store.posture_armed(), Some(false));
        assert_eq!(store.posture_chosen(), Some(false));

        // An owner that serves no block at all is absence, not a denial.
        apply(&mut store, snap(json!({})));
        assert_eq!(store.posture_armed(), None);
    }

    /// The posture is re-derived from every snapshot, not decided at startup.
    #[test]
    fn a_snapshot_that_disarms_the_desk_disarms_this_window() {
        let mut store = Store::default();
        assert_eq!(store.posture, Posture::Glass);

        apply(
            &mut store,
            snap(json!({"posture": {"armed": true, "chosen": true}})),
        );
        assert_eq!(store.posture.writes(), cfg!(feature = "operator"));

        apply(
            &mut store,
            snap(json!({"posture": {"armed": false, "chosen": true}})),
        );
        assert_eq!(store.posture, Posture::Glass);
    }

    /// `--glass` is the operator's own veto, and it survives a desk that says
    /// otherwise on every subsequent snapshot.
    #[test]
    fn a_window_started_glass_stays_glass_however_the_desk_answers() {
        let mut store = Store {
            forced_glass: true,
            ..Default::default()
        };
        apply(
            &mut store,
            snap(json!({"posture": {"armed": true, "chosen": true}})),
        );
        assert_eq!(store.posture, Posture::Glass);
    }

    #[test]
    fn a_halt_announces_itself_and_a_resume_is_a_separate_trigger() {
        let mut store = Store::default();
        assert_eq!(
            apply(&mut store, snap(json!({"portfolio": {"halted": false}}))),
            vec![]
        );
        assert_eq!(
            apply(&mut store, snap(json!({"portfolio": {"halted": true}}))),
            vec![Trigger::Halted]
        );
        // Still halted is not a new halt — the effect is already running.
        assert_eq!(
            apply(&mut store, snap(json!({"portfolio": {"halted": true}}))),
            vec![]
        );
        assert_eq!(
            apply(&mut store, snap(json!({"portfolio": {"halted": false}}))),
            vec![Trigger::Resumed]
        );
    }

    #[test]
    fn opening_onto_a_halted_desk_still_announces_the_halt() {
        // The transition happened before this client existed. Waiting for one
        // that will never be sent again would render a halted desk quietly.
        let mut store = Store::default();
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"live_portfolio": {"halted": true}}))
            ),
            vec![Trigger::Halted]
        );
    }

    #[test]
    fn the_live_book_decides_the_halt_and_a_vanished_flag_is_not_a_resume() {
        let mut store = Store::default();
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"live_portfolio": {"halted": true}, "portfolio": {"halted": false}}))
            ),
            vec![Trigger::Halted]
        );
        // The owner stopped reporting the flag. That is missing information,
        // not a resume, and clearing the HALT effect on it would be a lie.
        assert_eq!(
            apply(&mut store, snap(json!({"portfolio": {"equity": 1.0}}))),
            vec![]
        );
    }

    #[test]
    fn only_a_changed_robust_state_flips_the_regime() {
        let mut store = Store::default();
        let calm = json!({"market": {"regime": {"robust_state": "calm"}}});
        assert_eq!(
            apply(&mut store, snap(calm.clone())),
            vec![Trigger::RegimeChanged]
        );
        assert_eq!(apply(&mut store, snap(calm)), vec![]);
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"market": {"regime": {"robust_state": "stress"}}}))
            ),
            vec![Trigger::RegimeChanged]
        );
    }

    #[test]
    fn an_empty_string_is_absent_not_a_change() {
        // The owner serialises an unset string as `""`. Diffing it as a value
        // would fire a regime flip on every sparse payload.
        let mut store = Store::default();
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"market": {"regime": {"robust_state": ""}}}))
            ),
            vec![]
        );
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"market": {"regime": {"robust_state": "calm"}}}))
            ),
            vec![Trigger::RegimeChanged]
        );
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"market": {"regime": {"robust_state": ""}}}))
            ),
            vec![]
        );
    }

    #[test]
    fn a_worsening_drawdown_tier_announces_itself_and_a_recovery_does_not() {
        let tier = |name: &str| snap(json!({"stress": {"drawdown_tier": name}}));
        let mut store = Store::default();
        // `none` is the floor, so arriving at it is not a worsening.
        assert_eq!(apply(&mut store, tier("none")), vec![]);
        assert_eq!(
            apply(&mut store, tier("warning")),
            vec![Trigger::DrawdownTierWorse]
        );
        assert_eq!(apply(&mut store, tier("warning")), vec![]);
        assert_eq!(
            apply(&mut store, tier("breaker")),
            vec![Trigger::DrawdownTierWorse],
            "skipping a tier is still a worsening"
        );
        // Coming back down is good news. An alarm on it would teach an operator
        // that the red pulse means "the tier moved", which is not what it means.
        assert_eq!(apply(&mut store, tier("control")), vec![]);
        assert_eq!(
            apply(&mut store, tier("breaker")),
            vec![Trigger::DrawdownTierWorse]
        );
    }

    #[test]
    fn a_tier_that_stopped_being_reported_is_not_a_worsening_either_way() {
        let mut store = Store::default();
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"stress": {"drawdown_tier": "control"}}))
            ),
            vec![Trigger::DrawdownTierWorse]
        );
        // Missing information, so it ranks with `none` and fires nothing. It
        // also cannot re-fire when the field comes back at the same tier —
        // which it does here, because the rank it fell to was lower.
        assert_eq!(apply(&mut store, snap(json!({"stress": {}}))), vec![]);
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"stress": {"drawdown_tier": "control"}}))
            ),
            vec![Trigger::DrawdownTierWorse]
        );
        // A tier this client has never heard of ranks with `none` rather than
        // guessing. The rail renders it verbatim, so it is visible regardless.
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"stress": {"drawdown_tier": "molten"}}))
            ),
            vec![]
        );
    }

    #[test]
    fn opening_onto_a_breached_guardrail_announces_it() {
        // Same rule as the halt: the transition happened before this client
        // existed, and a desk in the control tier must not open quietly.
        let mut store = Store::default();
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"stress": {"drawdown_tier": "breaker"}}))
            ),
            vec![Trigger::DrawdownTierWorse]
        );
    }

    #[test]
    fn a_new_read_retriggers_the_reveal() {
        let mut store = Store::default();
        let first = json!({"atlas_read": {"as_of": "2026-07-30T12:00:00Z"}});
        assert_eq!(
            apply(&mut store, snap(first.clone())),
            vec![Trigger::ReadChanged]
        );
        assert_eq!(
            apply(&mut store, snap(first)),
            vec![],
            "the same read is not a new one"
        );
        assert_eq!(
            apply(
                &mut store,
                snap(json!({"atlas_read": {"as_of": "2026-07-30T12:30:00Z"}}))
            ),
            vec![Trigger::ReadChanged]
        );
    }

    #[test]
    fn one_snapshot_can_carry_several_transitions() {
        let mut store = Store::default();
        apply(
            &mut store,
            snap(json!({
                "portfolio": {"halted": false},
                "market": {"regime": {"robust_state": "calm"}},
                "atlas_read": {"as_of": "t0"}
            })),
        );
        let triggers = apply(
            &mut store,
            snap(json!({
                "portfolio": {"halted": true},
                "market": {"regime": {"robust_state": "stress"}},
                "atlas_read": {"as_of": "t1"}
            })),
        );
        assert!(triggers.contains(&Trigger::Halted));
        assert!(triggers.contains(&Trigger::RegimeChanged));
        assert!(triggers.contains(&Trigger::ReadChanged));
    }

    #[test]
    fn the_catalog_is_asked_for_again_only_once_the_owners_own_cache_has_lapsed() {
        // Both sides of the comparison, because one case that merely reaches
        // the code proves nothing about which way it went.
        let mut store = Store::default();
        let t0 = Instant::now();
        assert!(
            store.wants_backends(t0),
            "a desk that has never asked has nothing to reuse"
        );
        let catalog: crate::model::LlmCatalog = serde_json::from_value(json!({
            "backends": [{"name": "ollama", "available": true,
                          "reason": "ollama at 127.0.0.1:11434, 1 model pulled",
                          "models": ["qwen2.5:7b"]}],
            "probed_at": "2026-08-03T04:10:08.417505+00:00"
        }))
        .unwrap();
        store.apply(AppEvent::Backends(catalog), t0);
        assert_eq!(store.backends().unwrap().backends.len(), 1);
        assert!(store.take_dirty(), "a new catalog owes the strip a frame");
        assert!(
            !store.wants_backends(t0 + CATALOG_TTL - Duration::from_millis(1)),
            "inside the owner's own cache window the answer cannot have moved"
        );
        assert!(
            store.wants_backends(t0 + CATALOG_TTL),
            "a daemon that came up since is what the operator is looking for"
        );
    }

    #[test]
    fn a_snapshot_is_always_dirty_even_when_nothing_triggers() {
        // Numbers move without any of the motion triggers firing; the frame
        // still owes the operator the new ones.
        let mut store = Store::default();
        apply(&mut store, snap(json!({"portfolio": {"equity": 1.0}})));
        assert!(store.take_dirty());
        assert!(!store.take_dirty(), "taking the flag clears it");
        assert_eq!(
            apply(&mut store, snap(json!({"portfolio": {"equity": 2.0}}))),
            vec![]
        );
        assert!(store.take_dirty());
    }

    #[test]
    fn a_tick_advances_the_beat_without_being_a_state_change() {
        // The tick drives the glyph, not the desk. If it dirtied the store the
        // pacing rule would be decorative — every beat would force a frame.
        let mut store = Store::default();
        assert_eq!(apply(&mut store, AppEvent::Tick), vec![]);
        assert_eq!(
            store.tick, 1,
            "the beat has to advance or the glyph freezes"
        );
        assert!(!store.take_dirty());
    }

    #[test]
    fn the_mood_is_derived_from_the_desk_not_set() {
        let mut store = Store::default();
        // Nothing seen yet is not "idle": an empty client must not animate as
        // though it were watching a desk.
        assert_eq!(store.mood(), Mood::Dormant);

        apply(&mut store, snap(json!({"atlas": {"mode": "research"}})));
        assert_eq!(store.mood(), Mood::Idle);
        apply(
            &mut store,
            snap(json!({
                "atlas": {"mode": "research"},
                "atlas_heartbeat": {"coordinator": {"driving": true}}
            })),
        );
        assert_eq!(store.mood(), Mood::Working);
        // The live book decides, and a halt overrides a running coordinator.
        apply(
            &mut store,
            snap(json!({
                "atlas": {"mode": "research"},
                "atlas_heartbeat": {"coordinator": {"driving": true}},
                "live_portfolio": {"halted": true}
            })),
        );
        assert_eq!(store.mood(), Mood::Alarmed);
    }

    #[test]
    fn an_errored_heartbeat_still_decodes() {
        // `last_error_at` is a monotonic clock reading, like `last_tick_at`.
        // Every payload QA ever captured predated the first real tick error,
        // so the field shipped typed from its empty-string sentinel — and the
        // first float the owner served bricked every poll after it.
        let mut store = Store::default();
        apply(
            &mut store,
            snap(json!({
                "atlas_heartbeat": {
                    "errors": 1,
                    "last_error": "RuntimeError('desk read is unreachable')",
                    "last_error_at": 80444.857107791
                }
            })),
        );
    }

    #[test]
    fn the_default_desk_takes_its_staleness_threshold_from_the_poll_cadence() {
        // A derived `Default` would hand out `Duration::ZERO` here and mark
        // every frame ever drawn as stale — the failure mode this field's whole
        // point is to prevent, arriving as a one-line derive.
        assert_eq!(
            Store::default().stale_after,
            http::stale_after(http::POLL_INTERVAL)
        );
        assert!(Store::default().stale_after >= http::POLL_INTERVAL * 3);
    }

    #[test]
    fn a_snapshot_stamps_when_it_arrived() {
        // Without a stamp nothing downstream can say how old the numbers on
        // screen are, and a desk that draws four-minute-old marks unmarked is
        // the one failure a trading surface may not have.
        let mut store = Store::default();
        assert_eq!(store.last_snapshot_at, None, "nothing has arrived yet");

        let t0 = Instant::now();
        store.apply(snap(json!({"portfolio": {"equity": 1.0}})), t0);
        assert_eq!(store.last_snapshot_at, Some(t0));

        let t1 = t0 + Duration::from_secs(30);
        store.apply(snap(json!({"portfolio": {"equity": 2.0}})), t1);
        assert_eq!(
            store.last_snapshot_at,
            Some(t1),
            "the stamp is the newest arrival"
        );

        // Only a snapshot refreshes it: a tick or a keystroke is not news from
        // the owner, and letting either reset the clock would hide a dead feed.
        store.apply(AppEvent::Tick, t1 + Duration::from_secs(60));
        assert_eq!(store.last_snapshot_at, Some(t1));
    }

    #[test]
    fn a_malformed_payload_is_held_until_one_decodes() {
        // An owner serving garbage is reachable, so every connection chip stays
        // green. The log alone left the frame claiming it was waiting for a
        // first snapshot that was never going to arrive.
        let mut store = Store::default();
        let now = Instant::now();
        store.apply(
            AppEvent::Http(HttpResult::Malformed {
                url: "http://127.0.0.1:8765/api/tui".into(),
                error: "invalid type: string \"x\", expected f64\n  at line 4".into(),
            }),
            now,
        );
        let bad = store
            .malformed
            .clone()
            .expect("the failure must be held, not only logged");
        assert_eq!(bad.url, "http://127.0.0.1:8765/api/tui");
        assert_eq!(
            bad.error, "invalid type: string \"x\", expected f64",
            "first line only"
        );
        assert!(
            store.take_dirty(),
            "a new failure owes the operator a frame"
        );

        // The same failure every three seconds is not new news.
        store.apply(
            AppEvent::Http(HttpResult::Malformed {
                url: "http://127.0.0.1:8765/api/tui".into(),
                error: "invalid type: string \"x\", expected f64\n  at line 4".into(),
            }),
            now,
        );
        assert!(!store.take_dirty());

        store.apply(snap(json!({"portfolio": {"equity": 1.0}})), now);
        assert_eq!(
            store.malformed, None,
            "a payload that decodes retires the one that did not"
        );
    }

    #[test]
    fn a_long_decode_error_is_cut_rather_than_held_whole() {
        let mut store = Store::default();
        store.apply(
            AppEvent::Http(HttpResult::Malformed {
                url: "u".into(),
                error: "e".repeat(5_000),
            }),
            Instant::now(),
        );
        let held = store.malformed.unwrap().error;
        assert!(
            held.chars().count() <= MALFORMED_ERROR_MAX + 1,
            "{}",
            held.len()
        );
        assert!(held.ends_with('…'), "a cut error has to say it was cut");
    }

    /// One `quote` frame as the owner writes it (`qlab/ui/server.py:2129`):
    /// every row carries all three fields or the owner does not publish it.
    fn quote(rows: serde_json::Value) -> AppEvent {
        AppEvent::Sse(SseEvent {
            kind: "quote".into(),
            payload: json!({ "rows": rows }),
            ts: Some("2026-07-30T12:00:00+00:00".into()),
            id: Some("e1".into()),
        })
    }

    /// One `quote` frame carrying the owner's stamp, which is what orders two
    /// marks for the same ticker against each other.
    fn quote_at(rows: serde_json::Value, ts: &str) -> AppEvent {
        AppEvent::Sse(SseEvent {
            kind: "quote".into(),
            payload: json!({ "rows": rows }),
            ts: Some(ts.into()),
            id: Some("e1".into()),
        })
    }

    fn sse(kind: &str) -> AppEvent {
        AppEvent::Sse(SseEvent {
            kind: kind.into(),
            payload: json!({"raw": "{oops"}),
            ts: None,
            id: None,
        })
    }

    /// A snapshot carrying one asset at `price`.
    fn market(ticker: &str, price: f64, change: f64) -> AppEvent {
        snap(json!({"market": {"assets": [
            {"ticker": ticker, "price": price, "change_1d": change}
        ]}}))
    }

    #[test]
    fn a_quote_lands_in_the_overlay_and_never_in_the_snapshot() {
        // The snapshot is the owner's account of the desk. A client that edited
        // it would have no way left to tell what the owner actually said.
        let mut store = Store::default();
        let t0 = Instant::now();
        store.apply(market("SPY", 700.0, -0.01), t0);
        store.apply(
            quote(json!([{"ticker": "SPY", "price": 701.5, "change_1d": 0.002}])),
            t0 + Duration::from_millis(500),
        );

        let snapshot_price = store
            .snapshot
            .as_ref()
            .unwrap()
            .market
            .as_ref()
            .unwrap()
            .assets[0]
            .price
            .unwrap();
        assert_eq!(snapshot_price, 700.0, "the snapshot was mutated");
        let view = store.asset_view("SPY");
        assert_eq!(view.price, Some(701.5), "the overlay has to win here");
        assert_eq!(view.change_1d, Some(0.002));
    }

    #[test]
    fn with_no_overlay_an_asset_view_is_the_snapshot_verbatim() {
        let mut store = Store::default();
        store.apply(market("SPY", 700.0, -0.01), Instant::now());
        let view = store.asset_view("SPY");
        assert_eq!(view.ticker, "SPY");
        assert_eq!(view.price, Some(700.0));
        assert_eq!(view.change_1d, Some(-0.01));

        // A ticker nothing has ever reported is absent, not zero.
        let unknown = store.asset_view("GLD");
        assert_eq!(unknown.price, None);
        assert_eq!(unknown.change_1d, None);
    }

    #[test]
    fn a_snapshot_that_arrives_after_a_mark_wins_and_one_that_arrives_with_it_does_not() {
        // The loop stamps a whole drain with one instant, so a quote and a
        // snapshot delivered in the same batch carry the same stamp. The stream
        // is the fresher account of a price, so it must not be clobbered by the
        // aggregate it arrived alongside.
        let mut store = Store::default();
        let t0 = Instant::now();
        store.apply(market("SPY", 700.0, -0.01), t0);
        store.apply(
            quote(json!([{"ticker": "SPY", "price": 701.5, "change_1d": 0.002}])),
            t0 + Duration::from_secs(1),
        );

        store.apply(market("SPY", 699.0, -0.02), t0 + Duration::from_secs(1));
        assert_eq!(
            store.asset_view("SPY").price,
            Some(701.5),
            "a snapshot drained beside the quote clobbered it"
        );

        // A snapshot that genuinely arrives later is newer information and does
        // take over — the overlay is a lead on the poll, not a replacement.
        store.apply(market("SPY", 699.0, -0.02), t0 + Duration::from_secs(2));
        assert_eq!(store.asset_view("SPY").price, Some(699.0));
        assert!(
            store.quote_overlay.contains_key("SPY"),
            "the mark is superseded, not destroyed"
        );
    }

    #[test]
    fn a_price_is_stale_only_when_the_feed_that_actually_fed_it_is() {
        // Two feeds refresh a price and either can die alone. Keying every cell
        // to the snapshot's age dimmed a whole tape of second-old quotes the
        // moment the poller stopped — the same lie the dimming exists to
        // prevent, told the other way round.
        let mut store = Store::default();
        let after = store.stale_after;
        let t0 = Instant::now();
        store.apply(market("SPY", 700.0, -0.01), t0);

        // Dead poller, live stream: the snapshot is a minute old and the quote
        // on top of it is a second old, so the cell is current.
        let late = t0 + Duration::from_secs(60);
        store.apply(
            quote(json!([{"ticker": "SPY", "price": 701.5, "change_1d": 0.002}])),
            late,
        );
        let now = late + Duration::from_secs(1);
        assert!(!store.asset_view("SPY").stale(after, now));
        assert!(
            now.saturating_duration_since(store.last_snapshot_at.unwrap()) > after,
            "this case only means something while the aggregate is stale"
        );

        // Live poller, dead stream: the mark is older than the snapshot, so the
        // snapshot wins the row and its own age is the one that counts.
        let mut store = Store::default();
        store.apply(
            quote(json!([{"ticker": "SPY", "price": 701.5, "change_1d": 0.002}])),
            t0,
        );
        store.apply(market("SPY", 700.0, -0.01), late);
        assert!(!store
            .asset_view("SPY")
            .stale(after, late + Duration::from_secs(1)));

        // Both quiet: the row is stale, which is what the dimming is for.
        assert!(store.asset_view("SPY").stale(after, late + after * 2));

        // A ticker nothing has ever reported has no age to be stale at.
        let empty = Store::default();
        assert!(!empty.asset_view("GLD").stale(after, t0));
    }

    #[test]
    fn a_replayed_quote_older_than_the_mark_it_would_replace_is_refused() {
        // A reconnect resumes from the cursor and replays the outage, so a frame
        // arrives *now* about a moment before the price already on screen.
        // Arrival cannot order them; only the owner's own stamp can.
        let mut store = Store::default();
        let t0 = Instant::now();
        store.apply(
            quote_at(
                json!([{"ticker": "SPY", "price": 701.5, "change_1d": 0.002}]),
                "2026-07-30T12:00:02+00:00",
            ),
            t0,
        );
        store.take_dirty();

        let replayed = store.apply(
            quote_at(
                json!([{"ticker": "SPY", "price": 690.0, "change_1d": -0.01}]),
                "2026-07-30T12:00:01+00:00",
            ),
            t0 + Duration::from_millis(100),
        );
        assert_eq!(store.asset_view("SPY").price, Some(701.5));
        assert_eq!(replayed, vec![], "a replay is not a move");
        assert!(!store.take_dirty(), "and it owes no frame");

        // The next genuinely newer frame still lands.
        store.apply(
            quote_at(
                json!([{"ticker": "SPY", "price": 702.0, "change_1d": 0.003}]),
                "2026-07-30T12:00:03+00:00",
            ),
            t0 + Duration::from_millis(200),
        );
        assert_eq!(store.asset_view("SPY").price, Some(702.0));

        // An unstamped frame is not evidence of anything and is taken at face
        // value — the same rule the SSE cursor applies to a transient event.
        store.apply(
            AppEvent::Sse(SseEvent {
                kind: "quote".into(),
                payload: json!({"rows": [{"ticker": "SPY", "price": 3.0, "change_1d": 0.0}]}),
                ts: None,
                id: None,
            }),
            t0 + Duration::from_millis(300),
        );
        assert_eq!(store.asset_view("SPY").price, Some(3.0));
    }

    #[test]
    fn the_same_price_twice_is_not_a_tick() {
        // The owner republishes on its own beat. A trigger per frame rather than
        // per move would light every row permanently and mean nothing.
        let mut store = Store::default();
        let t0 = Instant::now();
        store.apply(market("SPY", 700.0, -0.01), t0);
        let rows = json!([{"ticker": "SPY", "price": 701.5, "change_1d": 0.002}]);

        assert_eq!(
            store.apply(quote(rows.clone()), t0 + Duration::from_millis(100)),
            vec![Trigger::QuoteTick("SPY".into())]
        );
        assert!(store.take_dirty(), "a moved price owes a frame");

        assert_eq!(
            store.apply(quote(rows), t0 + Duration::from_millis(200)),
            vec![],
            "the same number again is not news"
        );
        assert!(!store.take_dirty(), "and repaints nothing");
    }

    #[test]
    fn only_the_rows_that_moved_tick() {
        let mut store = Store::default();
        let t0 = Instant::now();
        store.apply(
            quote(json!([
                {"ticker": "SPY", "price": 700.0, "change_1d": 0.0},
                {"ticker": "QQQ", "price": 600.0, "change_1d": 0.0}
            ])),
            t0,
        );
        store.take_dirty();
        let triggers = store.apply(
            quote(json!([
                {"ticker": "SPY", "price": 700.0, "change_1d": 0.0},
                {"ticker": "QQQ", "price": 601.0, "change_1d": 0.01}
            ])),
            t0 + Duration::from_millis(100),
        );
        assert_eq!(triggers, vec![Trigger::QuoteTick("QQQ".into())]);
        assert!(store.take_dirty());
    }

    #[test]
    fn a_quote_row_the_client_cannot_read_is_skipped_rather_than_fatal() {
        // Fail loud, not fatal: one bad row must not cost the whole frame, and
        // must not be read as a price either. Every row the owner publishes
        // carries ticker, price, and change_1d (`_publish_quote_event`), so a
        // row missing one is a contract break worth logging and skipping.
        let mut store = Store::default();
        let t0 = Instant::now();
        let triggers = store.apply(
            quote(json!([
                {"ticker": "SPY", "price": 700.0},
                {"ticker": "", "price": 1.0, "change_1d": 0.0},
                {"price": 1.0, "change_1d": 0.0},
                {"ticker": "IWM", "price": "cheap", "change_1d": 0.0},
                "not even an object",
                {"ticker": "QQQ", "price": 600.0, "change_1d": 0.01}
            ])),
            t0,
        );
        assert_eq!(
            triggers,
            vec![Trigger::QuoteTick("QQQ".into())],
            "the readable row still has to land"
        );
        assert_eq!(store.quote_overlay.len(), 1);
        assert_eq!(store.asset_view("SPY").price, None);
    }

    #[test]
    fn a_quote_payload_with_no_rows_moves_nothing() {
        let mut store = Store::default();
        let now = Instant::now();
        assert_eq!(store.apply(quote(json!([])), now), vec![]);
        assert_eq!(
            store.apply(
                AppEvent::Sse(SseEvent {
                    kind: "quote".into(),
                    payload: json!({"assets": []}),
                    ts: None,
                    id: None,
                }),
                now
            ),
            vec![]
        );
        assert!(!store.take_dirty());
        assert!(store.quote_overlay.is_empty());
    }

    #[test]
    fn a_frame_the_stream_could_not_read_is_counted_on_the_desk() {
        // The parser logs each one whole; what the desk needs to know is that
        // the audit stream is dropping events at all, which a green STREAM chip
        // actively denies.
        let mut store = Store::default();
        let now = Instant::now();
        assert_eq!(store.stream_malformed_count, 0);
        store.apply(sse("stream.malformed"), now);
        store.apply(sse("stream.malformed"), now);
        assert_eq!(store.stream_malformed_count, 2);
        assert!(
            store.take_dirty(),
            "a dropped frame owes the operator a frame"
        );

        // Everything else on the bus is a durable audit row, which AUDIT
        // renders. It owes a frame, but counting it here would make the number
        // mean nothing.
        store.apply(sse("workflow_phase"), now);
        assert_eq!(store.stream_malformed_count, 2);
        assert!(
            store.take_dirty(),
            "a new audit row owes the operator a frame"
        );
    }

    #[test]
    fn the_views_are_the_snapshots_assets_in_the_snapshots_order() {
        // The snapshot decides the universe on screen. A ticker the stream
        // reported and the desk does not hold is not a row.
        let mut store = Store::default();
        let t0 = Instant::now();
        store.apply(
            snap(json!({"market": {"assets": [
                {"ticker": "ACWI", "price": 152.47, "change_1d": -0.013},
                {"ticker": "", "price": 1.0},
                {"ticker": "SPY", "price": 729.46, "change_1d": -0.015}
            ]}})),
            t0,
        );
        store.apply(
            quote(json!([
                {"ticker": "SPY", "price": 730.0, "change_1d": -0.01},
                {"ticker": "GLD", "price": 201.77, "change_1d": 0.01}
            ])),
            t0 + Duration::from_millis(1),
        );

        let views = store.asset_views();
        assert_eq!(
            views.iter().map(|v| v.ticker).collect::<Vec<_>>(),
            vec!["ACWI", "SPY"],
            "an unnamed asset is not a row, and the stream does not add one"
        );
        assert_eq!(views[0].price, Some(152.47));
        assert_eq!(views[1].price, Some(730.0));
    }

    #[test]
    fn clear_chat_hides_what_was_shown_and_new_rows_still_arrive() {
        // The mark is a moment, not a count: the owner serves a BOUNDED chat
        // window, and a count-based clear kept hiding new arrivals once the
        // window rotated under it.
        let mut store = Store::new(std::time::Duration::from_secs(9));
        let t0 = Instant::now();
        let snap = |rows: &str| -> AppEvent {
            AppEvent::Snapshot(Box::new(
                serde_json::from_str(&format!("{{\"atlas_chat\": {rows}}}")).unwrap(),
            ))
        };
        store.apply(
            snap(
                r#"[{"kind": "atlas_message", "ts": "2026-08-21T10:00:00+00:00"},
                     {"kind": "atlas_message", "ts": "2026-08-21T10:00:05+00:00"}]"#,
            ),
            t0,
        );
        assert_eq!(store.atlas_chat().len(), 2);
        store.clear_chat();
        assert_eq!(store.atlas_chat().len(), 0, "the pane empties");
        // The window rotates: one old row survives, one new row arrives.
        store.apply(
            snap(
                r#"[{"kind": "atlas_message", "ts": "2026-08-21T10:00:05+00:00"},
                     {"kind": "atlas_message", "ts": "2026-08-21T10:01:00+00:00"}]"#,
            ),
            t0,
        );
        let shown = store.atlas_chat();
        assert_eq!(shown.len(), 1, "only the row after the mark draws");
        assert_eq!(shown[0].ts.as_deref(), Some("2026-08-21T10:01:00+00:00"));
    }

    #[test]
    fn the_view_order_is_the_numbering_the_operator_sees() {
        assert_eq!(ViewId::from_digit('1'), Some(ViewId::Atlas));
        assert_eq!(ViewId::from_digit('2'), Some(ViewId::Desk));
        // PRED sits beside RSCH — the board is a research artifact — which is
        // the deliberate renumbering of everything after it, pinned here.
        assert_eq!(ViewId::from_digit('6'), Some(ViewId::Predictors));
        assert_eq!(ViewId::from_digit('9'), Some(ViewId::Settings));
        assert_eq!(ViewId::from_digit('0'), None, "there is no view zero");
        for (i, id) in ViewId::ALL.iter().enumerate() {
            assert_eq!(
                ViewId::from_digit(char::from_digit(i as u32 + 1, 10).unwrap()),
                Some(*id)
            );
            assert!(
                id.label().chars().count() <= 5,
                "{id:?} does not fit the rail"
            );
        }
        // Wrapping in both directions: a wall would read as a hung client.
        assert_eq!(ViewId::Settings.next(), ViewId::Atlas);
        assert_eq!(ViewId::Atlas.prev(), ViewId::Settings);
    }

    /// One durable bus frame, as `net::sse` hands it over.
    fn audit(kind: &str, id: &str) -> AppEvent {
        AppEvent::Sse(SseEvent {
            kind: kind.into(),
            payload: json!({"plan_id": "pl-1"}),
            ts: Some("2026-07-30T12:00:00+00:00".into()),
            id: Some(id.into()),
        })
    }

    #[test]
    fn the_audit_ring_holds_the_stream_newest_first_and_flashes_what_arrived() {
        let mut store = Store::default();
        let now = Instant::now();
        assert_eq!(store.audit_events().count(), 0);

        assert_eq!(
            store.apply(audit("approval_created", "e1"), now),
            vec![Trigger::AuditEvent("e1".into())],
            "the row that arrived is what lights, by the owner's own event id"
        );
        store.apply(audit("plan_executed", "e2"), now);

        let kinds: Vec<&str> = store.audit_events().map(|e| e.kind.as_str()).collect();
        assert_eq!(
            kinds,
            vec!["plan_executed", "approval_created"],
            "a log is read newest first"
        );
    }

    #[test]
    fn one_event_delivered_twice_is_one_row_and_flashes_once() {
        // Every durable kind nudges the poller, so the snapshot that arrives
        // carries the event the stream just delivered. A ring that took both
        // would show every governance event twice — which reads as the desk
        // having done it twice.
        let mut store = Store::default();
        let now = Instant::now();
        store.apply(audit("approval_created", "e1"), now);
        store.take_dirty();

        assert_eq!(
            store.apply(audit("approval_created", "e1"), now),
            vec![],
            "a replayed event is not news and must not flash"
        );
        assert!(!store.take_dirty(), "and owes no frame");
        assert_eq!(store.audit_events().count(), 1);

        // The same event arriving in a poll is also the same event.
        store.apply(
            snap(json!({"events": [
                {"event_id": "e1", "ts": "2026-07-30T12:00:00+00:00",
                 "kind": "approval_created", "payload": {}},
                {"event_id": "e2", "ts": "2026-07-30T12:00:01+00:00",
                 "kind": "plan_executed", "payload": {}}
            ]})),
            now,
        );
        let ids: Vec<Option<&str>> = store.audit_events().map(|e| e.id.as_deref()).collect();
        assert_eq!(ids, vec![Some("e2"), Some("e1")]);
    }

    #[test]
    fn a_snapshot_seeds_the_ring_without_announcing_events_that_are_hours_old() {
        // The bus rows a poll carries are history: flashing them would light
        // thirty rows every time a client opened, which teaches an operator
        // that a lit row means nothing.
        let mut store = Store::default();
        let triggers = store.apply(
            snap(json!({"events": [
                {"event_id": "e1", "ts": "t0", "kind": "halt", "payload": {}}
            ]})),
            Instant::now(),
        );
        assert!(!triggers.iter().any(|t| matches!(t, Trigger::AuditEvent(_))));
        assert_eq!(store.audit_events().count(), 1);
    }

    #[test]
    fn only_an_agents_own_word_stamps_the_liveness_clock() {
        let coord = |id: &str, event_kind: &str, at: Instant| {
            (
                AppEvent::Sse(SseEvent {
                    kind: "atlas_coordinator_event".into(),
                    payload: json!({"event_kind": event_kind, "agent": "referee", "text": "PASS"}),
                    ts: Some("2026-07-30T12:00:00+00:00".into()),
                    id: Some(id.into()),
                }),
                at,
            )
        };
        let mut store = Store::default();
        let t0 = Instant::now();
        assert_eq!(store.last_agent_event_at(), None, "nothing has been heard");

        // An unrelated bus row is not the workforce speaking.
        store.apply(audit("approval_created", "e0"), t0);
        assert_eq!(store.last_agent_event_at(), None);

        let (ev, at) = coord("c1", "text", t0);
        store.apply(ev, at);
        assert_eq!(store.last_agent_event_at(), Some(t0));

        // `tool_start` counts — the other half of what liveness is derived from.
        let t1 = t0 + Duration::from_secs(5);
        let (ev, at) = coord("c2", "tool_start", t1);
        store.apply(ev, at);
        assert_eq!(store.last_agent_event_at(), Some(t1));

        // And the kinds that were deliberately kept off the durable bus stay
        // off the liveness clock too: a progress ping is not a word.
        let t2 = t1 + Duration::from_secs(5);
        let (ev, at) = coord("c3", "task_progress", t2);
        store.apply(ev, at);
        assert_eq!(
            store.last_agent_event_at(),
            Some(t1),
            "noise moved the clock"
        );

        // The three kinds the owner really does publish beside the two above.
        // The floor is a choice, not an oversight: an error is a run failing
        // and a result is it having finished, and neither is the workforce
        // working — widening `is_agent_word` to any of them would let a run
        // that only errors read as a live desk.
        for (i, kind) in ["error", "result", "tool_result"].iter().enumerate() {
            let (ev, at) = coord(
                &format!("c-{kind}"),
                kind,
                t2 + Duration::from_secs(5 * (i as u64 + 1)),
            );
            store.apply(ev, at);
            assert_eq!(
                store.last_agent_event_at(),
                Some(t1),
                "{kind} moved the liveness clock"
            );
        }

        // A replay after a reconnect is not new news, so it cannot restart the
        // clock — the same rule the flash already obeys.
        let t3 = t2 + Duration::from_secs(5);
        let (ev, at) = coord("c2", "tool_start", t3);
        store.apply(ev, at);
        assert_eq!(store.last_agent_event_at(), Some(t1));
    }

    #[test]
    fn the_ring_is_bounded_and_drops_the_oldest_rather_than_growing() {
        // The bus is unbounded and this client is not. A ring that grew would
        // hold every event of a week-long session for a pane that shows thirty.
        let mut store = Store::default();
        let now = Instant::now();
        for i in 0..(EVENTS_RING + 25) {
            store.apply(audit("workflow_phase", &format!("e{i}")), now);
        }
        assert_eq!(store.audit_events().count(), EVENTS_RING);
        let newest = store.audit_events().next().unwrap().id.clone();
        assert_eq!(newest.as_deref(), Some("e124"));
        let oldest = store.audit_events().last().unwrap().id.clone();
        assert_eq!(oldest.as_deref(), Some("e25"), "the oldest rows left");
    }

    #[test]
    fn a_new_approval_announces_itself_and_a_status_change_is_not_a_new_one() {
        let queue = |rows: serde_json::Value| snap(json!({"approvals": rows}));
        let mut store = Store::default();
        // Opening onto a decision already waiting still announces it, exactly
        // as the halt does: the request was made before this client existed.
        assert!(store
            .apply(
                queue(json!([{"approval_id": "a1", "status": "pending"}])),
                Instant::now()
            )
            .contains(&Trigger::ApprovalCreated));
        // The same queue again is not news.
        assert!(!store
            .apply(
                queue(json!([{"approval_id": "a1", "status": "pending"}])),
                Instant::now()
            )
            .contains(&Trigger::ApprovalCreated));
        // A human decision keeps the id, so it is not a new request.
        assert!(!store
            .apply(
                queue(json!([{"approval_id": "a1", "status": "approved"}])),
                Instant::now()
            )
            .contains(&Trigger::ApprovalCreated));
        assert!(store
            .apply(
                queue(json!([{"approval_id": "a1", "status": "approved"},
                             {"approval_id": "a2", "status": "pending"}])),
                Instant::now()
            )
            .contains(&Trigger::ApprovalCreated));
    }

    #[test]
    fn a_plan_that_reaches_the_broker_announces_itself_once() {
        // The registry has no `executed` state: `qlab/trader/plan.py` walks a
        // plan checked → submitted → filled → reconciled inside one call, so a
        // three-second poll can catch it at any of the three.
        let ledger = |state: &str| snap(json!({"plans": [{"plan_id": "p1", "state": state}]}));
        let mut store = Store::default();
        assert!(!store
            .apply(ledger("checked"), Instant::now())
            .contains(&Trigger::PlanExecuted));
        assert!(store
            .apply(ledger("reconciled"), Instant::now())
            .contains(&Trigger::PlanExecuted));
        assert!(
            !store
                .apply(ledger("reconciled"), Instant::now())
                .contains(&Trigger::PlanExecuted),
            "a plan that is still booked did not book again"
        );
    }

    #[test]
    fn the_approval_a_fill_can_bind_to_is_approved_and_unspent_and_names_this_plan() {
        // The owner's own precondition, and nothing more: this client does not
        // re-check the expiry or the book revision, because those are the
        // gate's and a second copy here would drift from the one that decides.
        let mut store = Store::default();
        store.apply(
            snap(json!({"approvals": [
                {"approval_id": "a1", "plan_id": "p1", "status": "pending"},
                {"approval_id": "a2", "plan_id": "p2", "status": "approved"},
                {"approval_id": "a3", "plan_id": "p3", "status": "approved",
                 "consumed_at": "2026-07-30T12:00:00+00:00"},
                {"approval_id": "a4", "plan_id": "p4", "status": "rejected"}
            ]})),
            Instant::now(),
        );
        assert_eq!(
            store
                .covering_approval("p2")
                .and_then(|a| a.approval_id.clone()),
            Some("a2".to_string())
        );
        for plan in ["p1", "p3", "p4", "p5"] {
            assert!(
                store.covering_approval(plan).is_none(),
                "{plan} must not be executable"
            );
        }
        // The looser lookup still finds them, which is what a card says "why
        // not" from.
        assert_eq!(
            store.approval_for("p1").and_then(|a| a.status.clone()),
            Some("pending".to_string())
        );
    }

    /// One workflow carrying `steps`, as the owner serves it.
    fn flow(id: &str, steps: serde_json::Value) -> serde_json::Value {
        json!({"workflow_id": id, "status": "running", "steps": steps})
    }

    fn step(phase: &str, status: &str) -> serde_json::Value {
        json!({"step_id": format!("wf1:{phase}"), "phase": phase, "status": status})
    }

    #[test]
    fn a_step_that_changed_state_is_a_phase_advance_and_a_republished_one_is_not() {
        let mut store = Store::default();
        let queued = || {
            snap(json!({"workflows": [flow(
                "wf1",
                json!([step("analyst", "done"), step("challenger", "queued")]),
            )]}))
        };
        // The first snapshot is diffed against nothing, so every workflow on it
        // is new. Announcing a phase advance there would fire on whatever the
        // desk happened to be doing before this client existed.
        assert!(!store
            .apply(queued(), Instant::now())
            .contains(&Trigger::PhaseAdvanced));
        assert!(
            !store
                .apply(queued(), Instant::now())
                .contains(&Trigger::PhaseAdvanced),
            "the same steps republished are not an advance"
        );
        assert!(store
            .apply(
                snap(json!({"workflows": [flow(
                    "wf1",
                    json!([step("analyst", "done"), step("challenger", "working")]),
                )]})),
                Instant::now()
            )
            .contains(&Trigger::PhaseAdvanced));
    }

    #[test]
    fn a_workflow_this_client_has_not_seen_before_is_a_start_and_not_an_advance() {
        // Registering a workflow is not running it, and `workflow_started` is
        // the stream's own event for the first half. A diff that fired here
        // would announce a phase advance for a run whose phases are all still
        // queued — including on every reconnect that widens the ten-row window.
        let mut store = Store::default();
        store.apply(
            snap(json!({"workflows": [flow("wf1", json!([step("analyst", "done")]))]})),
            Instant::now(),
        );
        assert!(!store
            .apply(
                snap(json!({"workflows": [
                    flow("wf1", json!([step("analyst", "done")])),
                    flow("wf2", json!([step("analyst", "queued")]))
                ]})),
                Instant::now()
            )
            .contains(&Trigger::PhaseAdvanced));
    }

    #[test]
    fn an_interruption_is_a_phase_advance_because_the_pipeline_has_to_redraw() {
        // "Advance" is the name of the transition, not a direction: a step that
        // went `working` → `interrupted` moved, and the pane that draws it has
        // to say so as loudly as a completion does.
        let mut store = Store::default();
        store.apply(
            snap(json!({"workflows": [flow("wf1", json!([step("analyst", "working")]))]})),
            Instant::now(),
        );
        assert!(store
            .apply(
                snap(json!({"workflows": [flow(
                    "wf1",
                    json!([step("analyst", "interrupted")])
                )]})),
                Instant::now()
            )
            .contains(&Trigger::PhaseAdvanced));
    }

    #[test]
    fn a_workflow_with_no_id_cannot_be_diffed_against_anything() {
        // `Some("")` is absent here as everywhere else, so an unidentified row
        // can neither match a previous one nor fire on its own.
        let mut store = Store::default();
        let anonymous = || {
            snap(json!({"workflows": [
                {"status": "running", "steps": [step("analyst", "queued")]}
            ]}))
        };
        store.apply(anonymous(), Instant::now());
        assert!(!store
            .apply(anonymous(), Instant::now())
            .contains(&Trigger::PhaseAdvanced));
    }

    #[test]
    fn the_templates_poll_lands_beside_the_snapshot_rather_than_inside_it() {
        // Its own endpoint on its own cadence, exactly like the regime panel: a
        // client that had to wait for both would render an empty picker for the
        // whole first minute after startup.
        let mut store = Store::default();
        assert!(store.templates().is_empty());
        let templates: Vec<crate::model::Template> = serde_json::from_value(json!([
            {"template_id": "regime_review", "purpose": "Re-read the regime panel.",
             "phases": ["analyst", "referee"], "creates_plan": false}
        ]))
        .unwrap();
        apply(&mut store, AppEvent::Templates(templates));
        assert_eq!(store.templates().len(), 1);
        assert_eq!(
            store.templates()[0].template_id.as_deref(),
            Some("regime_review")
        );
        assert!(store.take_dirty(), "a picker that grew owes a frame");
    }

    #[test]
    fn a_connection_transition_is_dirty_once_not_every_poll() {
        let mut store = Store::default();
        assert!(!store.conn.owner);
        apply(&mut store, AppEvent::ConnUp(Channel::Owner));
        assert!(store.conn.owner);
        assert!(store.take_dirty());
        apply(&mut store, AppEvent::ConnUp(Channel::Owner));
        assert!(
            !store.take_dirty(),
            "a repeat of the same state repaints nothing"
        );
        apply(&mut store, AppEvent::ConnDown(Channel::Owner));
        assert!(store.take_dirty());
        assert!(!store.conn.owner && !store.conn.stream);
    }
}
