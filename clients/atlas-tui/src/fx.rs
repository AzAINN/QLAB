//! Flash tracking and the motion rules that decide when a change is worth animating.
//!
//! Time is data all the way through: `flash` is told the instant a change
//! arrived and `style_for` is told the instant the frame is being drawn at.
//! Nothing here reads a clock, which is what lets the decay be pinned by
//! arithmetic instead of by a sleep — the flaky class the Textual client
//! suffered (`test_quote_event_repaints_only_market_pulse_and_universe` racing a
//! 50 ms timer margin) cannot be written in this shape.
//!
//! The tracker lives beside the `Store` in `main`, never inside it. The store is
//! what the owner said plus the diff of it; a decaying animation stamp is
//! neither, and a store that carried one would no longer be a plain record of
//! the desk that a golden frame can be a pure function of.
//!
//! Three kinds of motion live here, and they are different because a cell grid
//! makes them different:
//!
//! * **Flashes and the reveal** are *styles*, chosen at draw time from an
//!   arrival stamp. They are part of the frame `shell::draw` paints.
//! * **tachyonfx effects** are a *pass over the painted buffer*, applied after
//!   every widget has rendered. They are stateful, created once when the desk
//!   moves, and driven by elapsed time rather than by a stamp. Deliberately not
//!   part of `shell::draw`: a golden frame calls `draw` and never `process`, so
//!   no snapshot can capture a half-finished effect.
//! * **Value easing** is the gauge needle — `Interpolation` eases effects, not
//!   application numbers, so the tween is hand-rolled (`ease_out_cubic`).

use crate::store::Trigger;
use crate::theme::theme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use std::collections::HashMap;
use std::time::{Duration, Instant};
use tachyonfx::fx::RepeatMode;
use tachyonfx::{fx, Effect, EffectManager, Interpolation, Motion, RefRect};

/// How long a flash lives. Long enough to catch the eye on a glance-away
/// surface, short enough that a fast tape does not leave the row permanently
/// lit — at which point the highlight stops meaning "this just moved".
pub const FLASH: Duration = Duration::from_millis(600);

/// How long the Atlas read takes to type itself in.
///
/// Its own constant rather than `FLASH`: a flash is a cell saying it moved and
/// a reveal is a paragraph arriving, and the day one of them wants a different
/// tempo the other must not follow it silently. That they are equal today is a
/// coincidence of taste, not a shared fact.
pub const REVEAL: Duration = Duration::from_millis(600);

/// The decay is stepped, not continuous. Three discrete styles at 200 ms each
/// beat a per-frame interpolation here: a cell grid has no alpha, so a "fade"
/// would quantize into steps anyway — and a stepped decay is a value a test can
/// assert rather than a curve it has to sample.
const STEP: Duration = Duration::from_millis(200);

/// Which cell of a row a flash belongs to.
///
/// The key is a (ticker, column) pair so two cells of the same row decay on
/// their own clocks: the ticker tape lights the price, the markets grid lights
/// `CHG%`, and one quote that moved both must not make either cell's decay a
/// function of where else the same ticker happens to be on screen.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Column {
    Price,
    Change,
    /// One row of the audit stream. Keyed by event id rather than by ticker —
    /// `FlashKey::ticker` is really "which thing", and an event's identity is
    /// the owner's `event_id`.
    Audit,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct FlashKey {
    pub ticker: String,
    pub column: Column,
}

impl FlashKey {
    pub fn price(ticker: &str) -> Self {
        Self {
            ticker: ticker.to_string(),
            column: Column::Price,
        }
    }

    pub fn change(ticker: &str) -> Self {
        Self {
            ticker: ticker.to_string(),
            column: Column::Change,
        }
    }

    /// The row one audit event landed on, by the owner's own event id.
    pub fn audit(event_id: &str) -> Self {
        Self {
            ticker: event_id.to_string(),
            column: Column::Audit,
        }
    }
}

/// When each cell last moved, and when the read last changed.
#[derive(Debug, Default)]
pub struct FlashTracker {
    map: HashMap<FlashKey, Instant>,
    /// When the Atlas read on screen arrived. `None` means none ever announced
    /// itself to this client — which is a fully revealed read, not an empty one.
    revealed_at: Option<Instant>,
}

impl FlashTracker {
    /// Turn one desk transition into motion.
    ///
    /// The vocabulary lives here rather than in `main` so it has a test that
    /// exercises it: the translation used to be a `match` inside the runtime
    /// loop, where the only way to prove a trigger reached its effect was to
    /// run the client and look. It stays out of the `Store` for the reason this
    /// module's doc gives — the store is what the owner said, and an animation
    /// stamp is not.
    pub fn on_trigger(&mut self, trigger: &Trigger, now: Instant) {
        match trigger {
            // One move, two lit cells: the tape carries the price and the
            // markets grid carries CHG%, and they are separate keys so neither
            // cell's decay depends on whether the other is on screen.
            Trigger::QuoteTick(ticker) => {
                self.flash(FlashKey::price(ticker), now);
                self.flash(FlashKey::change(ticker), now);
            }
            Trigger::ReadChanged => self.reveal(now),
            // The audit row that just arrived, and only that row. A sweep over
            // the pane would light thirty events that happened hours ago.
            Trigger::AuditEvent(id) => self.flash(FlashKey::audit(id), now),
            // The rest are the tachyonfx lane's (`Fx::on_trigger`): a sweep, a
            // pulse or a coalesce is a pass over the painted buffer, not a
            // style a renderer can pick. The two lanes are kept apart rather
            // than merged because only one of them can appear in a golden.
            _ => {}
        }
    }

    /// Start the read's reveal at `now`. A second read restarts it: the pane is
    /// typing in *this* read, and continuing the last one would show the new
    /// text arriving from wherever the old one had got to.
    pub fn reveal(&mut self, now: Instant) {
        self.revealed_at = Some(now);
    }

    /// How much of the read is on screen at `now`, from 0.0 to 1.0.
    ///
    /// A read nothing announced is already whole. An operator who opens DESK an
    /// hour after the last read must see it, not wait out a reveal that
    /// happened before this client was looking — and a golden frame that never
    /// called `reveal` must pin the read, not a blank pane.
    pub fn revealed(&self, now: Instant) -> f64 {
        let Some(start) = self.revealed_at else {
            return 1.0;
        };
        let elapsed = now.saturating_duration_since(start).as_millis() as f64;
        (elapsed / REVEAL.as_millis() as f64).min(1.0)
    }
    /// Mark a cell as having just changed. A second flash restarts the decay:
    /// a tape that keeps moving keeps the cell lit, which is the honest
    /// rendering of a price that keeps moving.
    pub fn flash(&mut self, key: FlashKey, now: Instant) {
        // Entries that have decayed out are dropped as new ones land, so the map
        // holds what is currently moving rather than every ticker ever quoted.
        self.map
            .retain(|_, start| now.saturating_duration_since(*start) < FLASH);
        self.map.insert(key, now);
    }

    /// The base style, plus whatever is left of this cell's flash at `now`.
    ///
    /// The ramp spends the amber rather than fading it: the wash first, then the
    /// wash retreating one step down the depth ramp while the digits keep the
    /// colour, then the digits alone. A cell grid has no alpha, so this is what
    /// "fading out" can actually mean here — and each step is a value the tests
    /// name instead of a curve they have to sample.
    pub fn style_for(&self, key: &FlashKey, now: Instant, base: Style) -> Style {
        let t = theme();
        match self.step(key, now) {
            Some(0) => base
                .bg(t.accent_dim)
                .fg(t.text_primary)
                .add_modifier(Modifier::BOLD),
            Some(1) => base.bg(t.bg_hover).fg(t.accent),
            Some(_) => base.fg(t.accent),
            None => base,
        }
    }

    /// Whether any cell is still decaying or the read is still arriving, and
    /// the frame therefore owes another paint. The pacing rule renders
    /// unconditionally while this is true: a decay the loop only samples on the
    /// idle heartbeat would step visibly late, and one it never samples would
    /// freeze mid-flash — or, for the read, mid-sentence.
    pub fn active(&self, now: Instant) -> bool {
        self.map.values().any(|start| alive(*start, now)) || self.revealing(now)
    }

    fn revealing(&self, now: Instant) -> bool {
        self.revealed_at
            .is_some_and(|start| now.saturating_duration_since(start) < REVEAL)
    }

    /// Which of the three steps a flash is in, or `None` once it is spent.
    ///
    /// `saturating_duration_since` on purpose: the loop stamps a whole drain
    /// with one instant, so a frame can legitimately be drawn at — or in a
    /// resumed test, before — the instant a flash was recorded, and the other
    /// subtraction panics in debug.
    fn step(&self, key: &FlashKey, now: Instant) -> Option<u32> {
        let start = *self.map.get(key)?;
        alive(start, now)
            .then(|| (now.saturating_duration_since(start).as_millis() / STEP.as_millis()) as u32)
    }
}

fn alive(start: Instant, now: Instant) -> bool {
    now.saturating_duration_since(start) < FLASH
}

// -- value easing ----------------------------------------------------------

/// Ease-out cubic, on the unit interval.
///
/// tachyonfx's `Interpolation` eases *effects* — it decides how far through a
/// dissolve the buffer is — and cannot be asked where a number is on its way to
/// another number. The gauge needle is a number, so it gets this.
///
/// One curve, not a library of them: a companion easing with no caller is
/// exactly the shape invariant 10 names. The next one arrives with the widget
/// that needs it.
pub fn ease_out_cubic(t: f64) -> f64 {
    let t = t.clamp(0.0, 1.0);
    1.0 - (1.0 - t).powi(3)
}

/// How long a tweened value takes to arrive.
pub const TWEEN: Duration = Duration::from_millis(400);

/// A number on its way to another number.
///
/// The pulse rail's stress gauge jumped between readings, which on a 0–100 scale
/// reads as a different desk rather than as the same desk moving. This holds the
/// value it left, the value it is going to, and when it set off — nothing else,
/// and no clock.
#[derive(Debug, Default)]
pub struct Tween {
    from: f64,
    to: f64,
    started_at: Option<Instant>,
}

impl Tween {
    /// Tell the tween what the desk now reads.
    ///
    /// The first value lands instantly. A needle that swept up from a zero
    /// nobody measured would be animating a number the desk never held — and an
    /// operator opening onto a stressed desk would watch it look calm first.
    pub fn set(&mut self, to: f64, now: Instant) {
        match self.started_at {
            None => {
                self.from = to;
                self.to = to;
                self.started_at = Some(now);
            }
            // Interrupting mid-flight leaves from where the needle actually is,
            // not from where the last leg started: the alternative snaps
            // backwards before setting off again.
            Some(_) if self.to != to => {
                self.from = self.at(now);
                self.to = to;
                self.started_at = Some(now);
            }
            Some(_) => {}
        }
    }

    /// Where the needle is at `now`, given the value the rail actually reads.
    ///
    /// Takes the true value rather than owning it, because that is what keeps a
    /// golden frame deterministic: a tween nobody ever `set` — every golden — has
    /// no claim to be part way anywhere, so it renders the number verbatim. The
    /// only frames this moves are the ones the runtime drew after a snapshot it
    /// told the tween about.
    pub fn shown(&self, value: f64, now: Instant) -> f64 {
        if self.started_at.is_none() || self.to != value {
            return value;
        }
        self.at(now)
    }

    /// Whether the needle still owes frames.
    pub fn active(&self, now: Instant) -> bool {
        self.from != self.to
            && self
                .started_at
                .is_some_and(|start| now.saturating_duration_since(start) < TWEEN)
    }

    fn at(&self, now: Instant) -> f64 {
        let Some(start) = self.started_at else {
            return self.to;
        };
        // Saturating for the reason `style_for` saturates: the loop stamps a
        // whole drain with one instant, so a frame can be drawn at the instant
        // the value was set.
        let elapsed = now.saturating_duration_since(start).as_secs_f64();
        let t = elapsed / TWEEN.as_secs_f64();
        if t >= 1.0 {
            return self.to;
        }
        self.from + (self.to - self.from) * ease_out_cubic(t)
    }
}

// -- the tachyonfx lane ----------------------------------------------------

/// How often the loop wakes while anything is moving — 60 fps.
///
/// Load-bearing, and the reason this constant exists at all. `should_render`
/// renders unconditionally while effects are active, but nothing woke the loop
/// to act on it: the idle heartbeat is 100 ms and the animation beat 120 ms, so
/// an "active" effect was sampled at whatever rate the owner's feeds happened to
/// deliver. Measured against a live owner, the read's 600 ms reveal arrived in
/// 11 repaints of up to 96 characters each; with this it arrives in about 30 of
/// 5–29. The loop waits with this as a timeout instead of blocking on the
/// channel while anything is moving, and blocks again the moment nothing is.
pub const FX_FRAME: Duration = Duration::from_millis(FX_FRAME_MS);
const FX_FRAME_MS: u64 = 16;

/// The full-frame lane's cadence — Part I's 30 fps cap, expressed as the thing
/// a 60 fps loop can actually do: one step every second wake, so 31 fps.
///
/// Two wakes rather than a flat 33 ms because 33 is not a whole number of 16 ms
/// frames — a 33 ms floor fed 16 ms wakes pays out on every *third* one, and the
/// halt would pulse at 20 fps while claiming 30. Deriving it from `FX_FRAME`
/// keeps that arithmetic from silently coming apart again.
///
/// **This caps the animation, not the painting.** A post-render effect pass runs
/// over a buffer the frame repainted from scratch, so the tint has to be
/// reapplied every frame or it is simply absent from the frames in between —
/// `Fx::process` therefore always calls the lane and varies only the duration it
/// hands over. That means the cap saves no cells and no bytes; what pays for a
/// long halt is `HALT_FRAME`, which cuts the number of frames drawn at all.
///
/// The lane is separate rather than throttled in place because one
/// `EffectManager` advances every effect it holds with one elapsed duration —
/// there is no per-effect step.
pub const FULL_FRAME: Duration = Duration::from_millis(2 * FX_FRAME_MS);

/// The cadence of a halted desk, when the halt breath is the *only* thing moving.
///
/// This is the one steady state the client can sit in for hours — an operator
/// walks away from an incident, possibly over ssh — so it is the one that must
/// not cost 31 fps of full-buffer rewrites. A breath is not a transition: it can
/// be sampled far more slowly than a sweep without reading as stepped.
///
/// 100 ms is chosen against two facts rather than picked round:
///
/// * **It equals `store::IDLE_FRAME`.** The heartbeat already wakes the loop
///   every 100 ms to keep the glyph and the quote ages honest, so a halt costs
///   *no extra wakes at all* over a quiet desk — only the effect pass on frames
///   that were being drawn anyway. It must never exceed the heartbeat either: a
///   client repainting slower than 10 fps during an incident is the one moment
///   an operator would read a live desk as a hung one.
/// * **It divides `HALT_BREATH` exactly, 14 ways.** `SineInOut` has a maximum
///   slope of π/2, so the worst per-frame alpha step is (π/2)/14 ≈ 0.112. The
///   widest channel span the fade crosses is `text_primary` → `negative_dim`
///   (0xe5 → 0x1d, 200 levels), so the worst per-frame colour step is ≈ 22 of
///   255 — under 9%, and only at the midpoint of the breath where the eye is
///   tracking motion rather than shade. Dividing exactly also means the peak and
///   the trough land on frames, so the breath does not beat against the
///   sampling.
///
/// Any other effect running alongside the halt takes the budget back to
/// `FX_FRAME`, and the breath then advances at `FULL_FRAME` through the debt in
/// `spend` — the cap still applies, it simply stops being the thing that decides
/// how often the loop wakes.
pub const HALT_FRAME: Duration = Duration::from_millis(100);

/// The most time one frame may advance a *pane* effect.
///
/// While pane effects run the loop wakes every `FX_FRAME`, so a gap wider than
/// this can only mean the lane was idle — and an effect registered in the very
/// iteration that ended the idle has not been playing for the length of it.
/// Unclamped, a coalesce fired by a keystroke 120 ms after the last heartbeat
/// frame would open 40% of the way through itself, and the transition an
/// operator sees would be the tail of one.
///
/// The trade is deliberate: under a terminal so slow that frames are genuinely
/// dropped, an effect plays for its full count of frames and takes longer in
/// wall time. On a cell grid that is the better failure — a transition that
/// skips is worse than one that lingers.
const MAX_PANE_STEP: Duration = FULL_FRAME;

/// The same, for the full-frame lane, whose slowest *designed* wake is the halt.
///
/// Two clamps rather than one because the lanes no longer share a cadence. A
/// single `FULL_FRAME` clamp would bind on every frame of the halt-only regime —
/// 32 ms of effect time for 100 ms of wall time — and the breath would run at a
/// third of its stated period while every test still passed. The clamp must be
/// each lane's own slowest legitimate wake, not the fastest of them.
///
/// It is close to a formality here: the only two effects in this lane are a
/// repeating breath, where opening part way through is invisible, and the 400 ms
/// restore. What it still buys is the genuinely idle gap — a `Halted` arriving
/// on a desk that had been quiet for a whole heartbeat.
const MAX_FULL_STEP: Duration = HALT_FRAME;

/// Part I's motion vocabulary, in milliseconds.
///
/// Durations rather than magic numbers at the call sites so the table below
/// reads as the vocabulary it is meant to be.
const COALESCE: u32 = 300;
const REGIME_SWEEP: u32 = 400;
const REGIME_SETTLE: u32 = 400;
const ALERT_HALF: u32 = 200;
const PLAN_SWEEP: u32 = 500;
/// Half a breath — 2.8 s in and out. Slower than the rest of the vocabulary on
/// purpose: everything else marks a transition and is over, while this marks a
/// condition that is still true. It is also what makes `HALT_FRAME` honest —
/// fourteen samples per half, rather than the seven a 700 ms breath would have
/// left, which is the difference between a breath and a stair.
///
/// Public as a `Duration` because it is half of the pair `HALT_FRAME` was chosen
/// against, and a test that had to restate the period would be free to disagree
/// with it.
pub const HALT_BREATH: Duration = Duration::from_millis(HALT_HALF as u64);
const HALT_HALF: u32 = 1400;
const RESTORE: u32 = 400;
const CHIP_HALF: u32 = 150;
const READ_WASH: u32 = 400;

/// Which effect an add replaces.
///
/// The key *is* the lifecycle: `add_unique_effect` cancels whatever was running
/// under the same key, so a HALT that runs forever is retired by adding the
/// restore fade under `Halt`, and a second view switch restarts the coalesce
/// instead of racing the first one. Two desk events share a key only when they
/// share a region and the later one genuinely supersedes the earlier.
///
/// `Default` is derived only because `EffectManager<K>`'s own derived `Default`
/// demands `K: Default` — a quirk of that derive, not a decision here. Nothing
/// reads the default key, and no rule may come to depend on which variant it is.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum FxKey {
    /// The content rect materialising after a nav change.
    #[default]
    ViewSwitch,
    /// The regime strip in the pulse rail.
    Regime,
    /// The whole frame, while the desk is halted — and its restore.
    Halt,
    /// The read panel, behind DESK's typewriter.
    Read,
    /// The content rect, when a plan executed.
    PlanCard,
    /// The content rect, when a guardrail worsened.
    ///
    /// Not in the brief's key set. It is here because a key is a cancellation
    /// lane: sharing `PlanCard` would mean a plan executing silently killed the
    /// drawdown alarm that had just fired, which is precisely the alert an
    /// operator must not lose.
    Alert,
    /// The status line's chip run — approvals and workflow phases.
    Toast,
}

/// Where the rules may aim.
///
/// `RefRect` rather than plain rects: the shell republishes what it computed on
/// every frame, so an effect created three frames ago follows its pane through a
/// resize instead of sweeping a strip that has moved out from under it. Views do
/// not write here — only `shell::draw`, which already computes every one of
/// these, so the layout keeps exactly one spelling.
///
/// Interior mutability is what lets `shell::draw` stay a `&self` renderer: it
/// publishes the layout it derived, and the frame it paints is still a pure
/// function of (store, effects, instant).
#[derive(Debug, Clone)]
pub struct ShellRects {
    /// The whole terminal.
    pub frame: RefRect,
    /// The view's area, inside both rails.
    pub content: RefRect,
    /// The regime strip in the pulse rail.
    pub regime: RefRect,
    /// The read panel in the pulse rail.
    pub read: RefRect,
    /// The right-hand chip run of the status line.
    pub chips: RefRect,
}

impl Default for ShellRects {
    fn default() -> Self {
        // Zero rects until the first frame publishes real ones. An effect over a
        // zero rect is a no-op, which is the honest behaviour for a client that
        // has not drawn yet — and cannot happen in the runtime, which draws one
        // frame before it reads its first event.
        Self {
            frame: RefRect::new(Rect::ZERO),
            content: RefRect::new(Rect::ZERO),
            regime: RefRect::new(Rect::ZERO),
            read: RefRect::new(Rect::ZERO),
            chips: RefRect::new(Rect::ZERO),
        }
    }
}

/// Every kind of motion the client owns, in one place beside the `Store`.
///
/// Two managers, not one, because they run at different cadences — see
/// `FULL_FRAME`. Both are keyed by `FxKey`, so a key names one lane and the
/// replace-on-add semantics hold within it.
#[derive(Debug, Default)]
pub struct Fx {
    /// Per-cell flashes and the read's reveal — styles, drawn by the widgets.
    pub flashes: FlashTracker,
    /// The pulse rail's stress needle.
    pub gauge: Tween,
    /// Where the last frame put things.
    pub rects: ShellRects,
    /// Pane-scoped effects, at 60 fps.
    panes: EffectManager<FxKey>,
    /// Full-frame effects, at 30 fps.
    full: EffectManager<FxKey>,
    /// Time the full-frame lane has been owed but not yet paid.
    debt: Duration,
    /// Whether what is registered under `FxKey::Halt` is the repeating breath
    /// rather than the restore fade that replaces it.
    ///
    /// Tracked here because the manager cannot be asked: `is_running` says
    /// *something* is running, never *which key*, and the two effects that share
    /// this key want opposite cadences — a breath that can be sampled at 10 fps,
    /// and a 400 ms restore that at 10 fps would be four frames.
    halt_pulsing: bool,
}

impl Fx {
    /// Turn one desk transition into motion.
    ///
    /// `opening` is true while the client's very first snapshot is being folded
    /// in. That snapshot is diffed against nothing, so it announces the state it
    /// arrived in — deliberately, because an operator opening onto a halted desk
    /// has to see the halt. What it must not do is stack: the regime sweep is
    /// suppressed on the open and nothing else is, because the sweep is the one
    /// arm that would fight the frame arriving underneath it.
    pub fn on_trigger(&mut self, trigger: &Trigger, now: Instant, opening: bool) {
        self.flashes.on_trigger(trigger, now);
        self.rules(trigger, opening);
    }

    /// The vocabulary. One arm per `Trigger` variant, no catch-all: a variant
    /// added to the diff cannot reach the runtime without someone deciding here
    /// what it looks like — including deciding it looks like nothing.
    fn rules(&mut self, trigger: &Trigger, opening: bool) {
        let t = theme();
        match trigger {
            // A sweep across the strip, then the colour settling out of it.
            Trigger::RegimeChanged => {
                if opening {
                    return;
                }
                self.panes.add_unique_effect(
                    FxKey::Regime,
                    aim(
                        &self.rects.regime,
                        fx::sequence(&[
                            fx::sweep_in(
                                Motion::LeftToRight,
                                6,
                                0,
                                t.bg_surface,
                                (REGIME_SWEEP, Interpolation::CubicInOut),
                            ),
                            fx::fade_from_fg(t.accent, (REGIME_SETTLE, Interpolation::CubicInOut)),
                        ]),
                    ),
                );
            }
            // Two red pulses over the view. The book's own alert pane is a rect
            // only BOOK knows; the content rect is the coarsest true target, and
            // the guardrail is desk news whichever view is up. Tasks 18/19 give
            // the panes that own it a published rect to narrow this to.
            Trigger::DrawdownTierWorse => {
                self.panes.add_unique_effect(
                    FxKey::Alert,
                    aim(
                        &self.rects.content,
                        fx::repeat(
                            fx::ping_pong(fx::fade_to_fg(
                                t.negative,
                                (ALERT_HALF, Interpolation::CubicInOut),
                            )),
                            RepeatMode::Times(2),
                        ),
                    ),
                );
            }
            // The one effect that does not end. It runs until `Resumed` replaces
            // it under the same key, which is the whole reason the manager is
            // keyed — a halt that stopped pulsing on its own would say the desk
            // had recovered.
            //
            // Full-frame fade rather than a border ring: the frame's outer rows
            // are the ticker tape and the status line, and reddening those makes
            // two always-on chrome elements lie about themselves.
            Trigger::Halted => {
                self.halt_pulsing = true;
                self.full.add_unique_effect(
                    FxKey::Halt,
                    fx::repeating(fx::ping_pong(fx::fade_to_fg(
                        t.negative_dim,
                        (HALT_HALF, Interpolation::SineInOut),
                    ))),
                );
            }
            Trigger::Resumed => {
                // Cleared before the add, so the very frame that starts the
                // restore is already budgeted as a transition rather than as a
                // breath: a 400 ms fade sampled at the halt's cadence would be
                // four frames, which is a blink and not a recovery.
                self.halt_pulsing = false;
                self.full.add_unique_effect(
                    FxKey::Halt,
                    fx::fade_from_fg(t.negative_dim, (RESTORE, Interpolation::CubicOut)),
                );
            }
            // Fired when the snapshot carries an approval id the last one did
            // not (`Store::apply_snapshot`), which is a human decision arriving
            // at the queue.
            Trigger::ApprovalCreated => {
                self.panes.add_unique_effect(
                    FxKey::Toast,
                    aim(
                        &self.rects.chips,
                        fx::repeat(
                            fx::ping_pong(fx::fade_to_fg(
                                t.warning,
                                (CHIP_HALF, Interpolation::CubicInOut),
                            )),
                            RepeatMode::Times(2),
                        ),
                    ),
                );
            }
            // Not emitted yet — Task 19 diffs the workflow phase. Shares
            // `Toast` with the approval on purpose: one chip region, one pulse,
            // and the newer piece of governance news is the one to show.
            // Invariant 10 is owed a caller here, and Task 19 is where it lands.
            Trigger::PhaseAdvanced => {
                self.panes.add_unique_effect(
                    FxKey::Toast,
                    aim(
                        &self.rects.chips,
                        fx::fade_from_fg(t.accent, (CHIP_HALF * 2, Interpolation::CubicOut)),
                    ),
                );
            }
            // Fired when a plan leaves `checked` for a state the broker has
            // seen (`Store::apply_snapshot`).
            Trigger::PlanExecuted => {
                self.panes.add_unique_effect(
                    FxKey::PlanCard,
                    aim(
                        &self.rects.content,
                        fx::sweep_in(
                            Motion::LeftToRight,
                            10,
                            0,
                            t.positive,
                            (PLAN_SWEEP, Interpolation::CubicOut),
                        ),
                    ),
                );
            }
            // No tachyonfx rule: a quote is a cell, and `FlashTracker` already
            // lights exactly the two cells that moved. A sweep over the grid
            // would animate the rows that did not.
            Trigger::QuoteTick(_) => {}
            // Same reason, one pane over: an audit row is a row, and the
            // tracker lights the one that arrived. A pass over the stream pane
            // would animate the whole log every time anything happened on the
            // desk, which is most of the time.
            Trigger::AuditEvent(_) => {}
            // The reveal is DESK's, hand-rolled, and stays hand-rolled: it types
            // a substring in, which is a render of different text rather than an
            // effect over the same text. All this adds is the wash behind the
            // rail's copy of the read, so the panel that is on screen under
            // every view acknowledges the change too.
            Trigger::ReadChanged => {
                self.panes.add_unique_effect(
                    FxKey::Read,
                    aim(
                        &self.rects.read,
                        fx::fade_from_fg(t.accent_dim, (READ_WASH, Interpolation::CubicOut)),
                    ),
                );
            }
        }
    }

    /// The new view materialising.
    ///
    /// Fired from the runtime rather than from a `Trigger`, because which pane
    /// an operator is looking at is not something the owner said — and the store
    /// diffs only what the owner said.
    pub fn on_view_switch(&mut self) {
        self.panes.add_unique_effect(
            FxKey::ViewSwitch,
            aim(
                &self.rects.content,
                fx::coalesce((COALESCE, Interpolation::CubicOut)),
            ),
        );
    }

    /// Whether anything owes the terminal a frame.
    pub fn active(&self, now: Instant) -> bool {
        self.budget(now).is_some()
    }

    /// How long the loop may sleep before it owes the next frame, or `None` to
    /// block on the channel.
    ///
    /// Three cadences, not one, because a desk can sit in any of them for a very
    /// different length of time. A transition is over in 300 ms and can afford
    /// 60 fps. A halt lasts as long as the incident does — hours, possibly over
    /// ssh — and is the one steady state where the cost of a frame is a bill
    /// somebody pays.
    pub fn budget(&self, now: Instant) -> Option<Duration> {
        budget(
            self.flashes.active(now) || self.gauge.active(now) || self.panes.is_running(),
            self.full.is_running(),
            self.halt_pulsing,
        )
    }

    /// Advance every effect over the painted buffer.
    ///
    /// Called after the widgets have rendered and never from inside
    /// `shell::draw` — that separation is what makes a golden frame structurally
    /// incapable of catching a half-finished effect.
    ///
    /// Pane effects first, then the full frame over the top: a halted desk
    /// reddens everything, including whatever else is moving.
    ///
    /// `elapsed` is clamped per lane: see `MAX_PANE_STEP` and `MAX_FULL_STEP`.
    pub fn process(&mut self, elapsed: Duration, buf: &mut Buffer, area: Rect) {
        self.panes
            .process_effects(elapsed.min(MAX_PANE_STEP).into(), buf, area);
        // The full lane is *painted* on every frame and *advanced* only on the
        // frames it owes a step — a zero duration renders the tint it is already
        // at without moving it on.
        //
        // Not `if let Some(…) { process }`. The buffer underneath is repainted
        // from scratch every frame, so a frame the lane sat out would show the
        // desk with no halt on it at all: a 30 Hz strobe between tinted and
        // clean, which is worse than no cap. That was live for the whole of the
        // first implementation and every test passed, because the tests fed one
        // buffer through repeated calls and the tint accumulated in it.
        let step =
            spend(&mut self.debt, elapsed.min(MAX_FULL_STEP), FULL_FRAME).unwrap_or(Duration::ZERO);
        self.full.process_effects(step.into(), buf, area);
    }
}

/// Bind an effect to a rect that may move under it.
fn aim(rect: &RefRect, effect: Effect) -> Effect {
    fx::dynamic_area(rect.clone(), effect)
}

/// The loop's wait, given which lanes are running.
///
/// Separated from `Fx` so the choice that fixes the starved-effect bug — and the
/// one that keeps a halted desk cheap — is a function of three booleans a test
/// can enumerate, rather than something only reachable by driving a terminal.
///
/// `halt_only` is a claim about *which* full-frame effect is registered, not
/// about whether the halt is the only thing on screen: `fast` already outranks
/// it, so any concurrent pane effect takes the whole loop back to `FX_FRAME` and
/// the breath rides along at `FULL_FRAME` through `spend`'s debt.
pub const fn budget(fast: bool, full: bool, halt_only: bool) -> Option<Duration> {
    match (fast, full, halt_only) {
        (true, _, _) => Some(FX_FRAME),
        // The steady state an incident leaves a terminal in.
        (false, true, true) => Some(HALT_FRAME),
        (false, true, false) => Some(FULL_FRAME),
        (false, false, _) => None,
    }
}

/// Whether the capped lane owes a step, and how much time to tell it about.
///
/// A debt that only pays out in whole `floor`s is what keeps the cap from also
/// *losing* time: woken at 16 ms, the lane steps on every second frame and is
/// told about 32 ms, so a 700 ms pulse still takes 700 ms.
fn spend(debt: &mut Duration, elapsed: Duration, floor: Duration) -> Option<Duration> {
    *debt += elapsed;
    (*debt >= floor).then(|| std::mem::take(debt))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> Style {
        Style::default().fg(theme().text_primary)
    }

    /// The style this tracker hands back for `ticker` at `start + ms`.
    fn at(fx: &FlashTracker, ticker: &str, start: Instant, ms: u64) -> Style {
        fx.style_for(
            &FlashKey::price(ticker),
            start + Duration::from_millis(ms),
            base(),
        )
    }

    #[test]
    fn a_flash_decays_through_three_steps_and_then_stops_existing() {
        let t = theme();
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        fx.flash(FlashKey::price("SPY"), start);

        // Step 0: the wash. Loud on purpose — this is the frame the eye catches.
        for ms in [0, 1, 199] {
            assert_eq!(at(&fx, "SPY", start, ms).bg, Some(t.accent_dim), "{ms}ms");
        }
        // Step 1: the wash retreats down the depth ramp, the digits keep the amber.
        for ms in [200, 399] {
            let style = at(&fx, "SPY", start, ms);
            assert_eq!(style.bg, Some(t.bg_hover), "{ms}ms");
            assert_eq!(style.fg, Some(t.accent), "{ms}ms");
        }
        // Step 2: only the digits are left lit.
        for ms in [400, 599] {
            let style = at(&fx, "SPY", start, ms);
            assert_eq!(style.bg, None, "{ms}ms");
            assert_eq!(style.fg, Some(t.accent), "{ms}ms");
        }
        // Decayed out: the base style, byte for byte. A flash that left anything
        // behind would accumulate over a session until every row was lit.
        for ms in [600, 700, 60_000] {
            assert_eq!(at(&fx, "SPY", start, ms), base(), "{ms}ms");
        }
    }

    #[test]
    fn a_cell_that_never_flashed_renders_exactly_the_base_style() {
        let fx = FlashTracker::default();
        assert_eq!(at(&fx, "QQQ", Instant::now(), 0), base());
    }

    #[test]
    fn a_second_flash_restarts_the_decay() {
        // A price that keeps moving keeps its cell lit. Without this the second
        // move of a fast tape would render dimmer than the first.
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        fx.flash(FlashKey::price("SPY"), start);
        fx.flash(FlashKey::price("SPY"), start + Duration::from_millis(500));
        assert_eq!(
            at(&fx, "SPY", start, 500).bg,
            Some(theme().accent_dim),
            "the restart has to reset the ramp, not continue it"
        );
        assert_eq!(at(&fx, "SPY", start, 1_100), base());
    }

    #[test]
    fn two_cells_decay_on_their_own_clocks() {
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        fx.flash(FlashKey::price("SPY"), start);
        fx.flash(FlashKey::price("QQQ"), start + Duration::from_millis(400));
        assert_eq!(at(&fx, "SPY", start, 500).fg, Some(theme().accent));
        assert_eq!(at(&fx, "QQQ", start, 500).bg, Some(theme().accent_dim));
    }

    #[test]
    fn two_columns_of_one_row_are_two_cells_not_one() {
        // The tape lights the price and the grid lights CHG%. Keying on the
        // ticker alone would make either cell's decay a function of where else
        // the same ticker happens to be on screen.
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        fx.flash(FlashKey::price("SPY"), start);
        assert_eq!(
            fx.style_for(&FlashKey::change("SPY"), start, base()),
            base(),
            "the price flash reached the change cell"
        );
        fx.flash(FlashKey::change("SPY"), start + Duration::from_millis(400));
        assert_eq!(
            fx.style_for(
                &FlashKey::change("SPY"),
                start + Duration::from_millis(400),
                base()
            )
            .bg,
            Some(theme().accent_dim)
        );
    }

    #[test]
    fn active_is_what_keeps_the_loop_painting_while_a_flash_is_alive() {
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        assert!(!fx.active(start), "nothing has moved yet");
        fx.flash(FlashKey::price("SPY"), start);
        assert!(fx.active(start + Duration::from_millis(599)));
        assert!(
            !fx.active(start + FLASH),
            "a decayed flash must not pin the loop at the effect cadence forever"
        );
    }

    #[test]
    fn a_frame_drawn_before_the_stamp_is_not_a_negative_age() {
        // The loop stamps a whole drain with one instant, so a frame can be
        // drawn at the same instant a flash was recorded — and a subtraction the
        // other way round panics in debug.
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        fx.flash(FlashKey::price("SPY"), start);
        let earlier = start - Duration::from_millis(50);
        assert_eq!(
            fx.style_for(&FlashKey::price("SPY"), earlier, base()).bg,
            Some(theme().accent_dim)
        );
    }

    #[test]
    fn a_read_nobody_announced_is_whole_and_an_announced_one_arrives_over_600ms() {
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        assert_eq!(
            fx.revealed(start),
            1.0,
            "a read this client never saw arrive must render whole"
        );

        fx.reveal(start);
        assert_eq!(fx.revealed(start), 0.0);
        assert_eq!(fx.revealed(start + Duration::from_millis(300)), 0.5);
        assert_eq!(fx.revealed(start + Duration::from_millis(600)), 1.0);
        assert_eq!(
            fx.revealed(start + Duration::from_secs(60)),
            1.0,
            "the fraction is clamped, not unbounded"
        );
        // Same reason `style_for` saturates: the loop stamps a whole drain with
        // one instant, so a frame can be drawn at — or before — the stamp.
        assert_eq!(fx.revealed(start - Duration::from_millis(50)), 0.0);
    }

    #[test]
    fn a_second_read_types_itself_in_from_the_top() {
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        fx.reveal(start);
        fx.reveal(start + Duration::from_secs(30));
        assert_eq!(fx.revealed(start + Duration::from_secs(30)), 0.0);
    }

    #[test]
    fn a_reveal_keeps_the_loop_painting_exactly_as_a_flash_does() {
        // Without this the read would type itself in at the 100 ms idle
        // heartbeat — six visible steps instead of a reveal.
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        fx.reveal(start);
        assert!(fx.active(start));
        assert!(fx.active(start + Duration::from_millis(599)));
        assert!(
            !fx.active(start + REVEAL),
            "a finished reveal must not pin the loop at the effect cadence forever"
        );
    }

    #[test]
    fn every_trigger_with_motion_behind_it_reaches_its_effect() {
        // Invariant 10, at the one seam where it has bitten three times: a
        // translation that exists and is never called is indistinguishable from
        // one that does not exist.
        let start = Instant::now();
        let mut fx = FlashTracker::default();

        fx.on_trigger(&Trigger::QuoteTick("SPY".into()), start);
        assert_eq!(
            fx.style_for(&FlashKey::price("SPY"), start, base()).bg,
            Some(theme().accent_dim)
        );
        assert_eq!(
            fx.style_for(&FlashKey::change("SPY"), start, base()).bg,
            Some(theme().accent_dim),
            "one move lights both cells of the row"
        );

        fx.on_trigger(&Trigger::ReadChanged, start + Duration::from_secs(1));
        assert_eq!(fx.revealed(start + Duration::from_secs(1)), 0.0);

        // A trigger Task 15 has not given motion to yet moves nothing, rather
        // than falling through to whatever the previous arm did.
        let mut quiet = FlashTracker::default();
        quiet.on_trigger(&Trigger::Halted, start);
        assert!(!quiet.active(start));
    }

    #[test]
    fn the_map_holds_what_is_moving_rather_than_every_ticker_ever_quoted() {
        let start = Instant::now();
        let mut fx = FlashTracker::default();
        for i in 0..50 {
            fx.flash(
                FlashKey::price(&format!("T{i}")),
                start + Duration::from_secs(i),
            );
        }
        assert_eq!(
            fx.map.len(),
            1,
            "expired stamps are dropped as new ones land"
        );
    }

    // -- value easing ------------------------------------------------------

    #[test]
    fn ease_out_cubic_starts_fast_ends_flat_and_is_clamped() {
        assert_eq!(ease_out_cubic(0.0), 0.0);
        assert_eq!(ease_out_cubic(1.0), 1.0);
        // Ease-*out*: more than half the distance is covered in the first half.
        assert!(ease_out_cubic(0.5) > 0.5, "{}", ease_out_cubic(0.5));
        assert!((ease_out_cubic(0.5) - 0.875).abs() < 1e-12);
        // Monotone, so a needle never doubles back on its way.
        let mut prev = 0.0;
        for i in 0..=100 {
            let v = ease_out_cubic(f64::from(i) / 100.0);
            assert!(v >= prev, "not monotone at {i}");
            prev = v;
        }
        // Out of range is clamped rather than extrapolated: the alternative
        // overshoots the target on a frame drawn late.
        assert_eq!(ease_out_cubic(-1.0), 0.0);
        assert_eq!(ease_out_cubic(4.0), 1.0);
    }

    #[test]
    fn the_first_value_lands_instantly_rather_than_sweeping_up_from_zero() {
        let start = Instant::now();
        let mut gauge = Tween::default();
        gauge.set(62.0, start);
        assert_eq!(gauge.shown(62.0, start), 62.0);
        assert!(
            !gauge.active(start),
            "an opening frame must not owe the loop 400 ms of animation"
        );
    }

    #[test]
    fn a_moved_value_eases_over_the_tween_and_then_stops() {
        let start = Instant::now();
        let mut gauge = Tween::default();
        gauge.set(20.0, start);
        gauge.set(60.0, start);

        assert_eq!(
            gauge.shown(60.0, start),
            20.0,
            "it sets off from where it was"
        );
        let half = gauge.shown(60.0, start + Duration::from_millis(200));
        assert!(
            (half - (20.0 + 40.0 * 0.875)).abs() < 1e-9,
            "half way through the tween is 87.5% of the way there, not 50%: {half}"
        );
        assert_eq!(gauge.shown(60.0, start + TWEEN), 60.0);
        assert_eq!(gauge.shown(60.0, start + Duration::from_secs(60)), 60.0);
        assert!(gauge.active(start + Duration::from_millis(399)));
        assert!(
            !gauge.active(start + TWEEN),
            "a finished tween must not pin the loop at the effect cadence forever"
        );
    }

    #[test]
    fn a_value_the_tween_was_never_told_about_renders_verbatim() {
        // This is what keeps a golden frame deterministic: every golden builds
        // an `Fx` nobody ever set, so the rail draws the number the store holds.
        let start = Instant::now();
        let untouched = Tween::default();
        assert_eq!(untouched.shown(37.5, start), 37.5);

        // And a tween that is mid-flight toward 60 does not claim to know where
        // 90 came from — it renders 90 until it is told.
        let mut gauge = Tween::default();
        gauge.set(20.0, start);
        gauge.set(60.0, start);
        assert_eq!(gauge.shown(90.0, start + Duration::from_millis(100)), 90.0);
    }

    #[test]
    fn interrupting_a_tween_leaves_from_where_the_needle_actually_is() {
        let start = Instant::now();
        let mut gauge = Tween::default();
        gauge.set(0.0, start);
        gauge.set(100.0, start);
        let mid = start + Duration::from_millis(100);
        let seen = gauge.shown(100.0, mid);
        gauge.set(10.0, mid);
        assert_eq!(
            gauge.shown(10.0, mid),
            seen,
            "the needle jumped when the target changed"
        );
    }

    #[test]
    fn a_tween_told_the_same_value_twice_does_not_restart() {
        let start = Instant::now();
        let mut gauge = Tween::default();
        gauge.set(10.0, start);
        gauge.set(50.0, start);
        gauge.set(50.0, start + Duration::from_millis(300));
        assert_eq!(
            gauge.shown(50.0, start + TWEEN),
            50.0,
            "a republished snapshot restarted the animation"
        );
    }

    // -- pacing ------------------------------------------------------------

    #[test]
    fn the_wait_is_the_fastest_lane_that_is_running() {
        // (fast, full, halt_only)
        assert_eq!(
            budget(false, false, false),
            None,
            "idle blocks on the channel"
        );
        assert_eq!(budget(true, false, false), Some(FX_FRAME));
        assert_eq!(budget(true, true, false), Some(FX_FRAME));
        assert_eq!(
            budget(false, true, false),
            Some(FULL_FRAME),
            "a full-frame effect that is not the halt is still a transition"
        );
        // `halt_only` cannot resurrect a lane that is not running — a stale flag
        // must never be able to pin the loop on its own.
        assert_eq!(budget(false, false, true), None);
    }

    #[test]
    fn a_halted_desk_is_the_one_steady_state_and_is_budgeted_as_one() {
        assert_eq!(
            budget(false, true, true),
            Some(HALT_FRAME),
            "a halt lasts as long as the incident does; 31 fps is a bill nobody agreed to"
        );
        // Anything else moving takes the loop back off the breath's cadence —
        // the breath then advances through `spend`'s debt at `FULL_FRAME`, so
        // the cap still applies, it just stops deciding how often we wake.
        assert_eq!(budget(true, true, true), Some(FX_FRAME));
        // And a restore fade is a transition again the moment it replaces the
        // breath, rather than four frames at the breath's cadence.
        assert_eq!(budget(false, true, false), Some(FULL_FRAME));
    }

    #[test]
    fn the_halt_cadence_is_pinned_to_the_two_facts_it_was_chosen_from() {
        // *Equal* to the heartbeat, not merely no slower than it. Both halves
        // of the choice this constant documents need the equality: slower and
        // a client repainting under 10 fps during an incident reads as a hung
        // one, faster and a halt costs wakes a quiet desk does not — which is
        // the whole reason the halt has a cadence of its own. `<=` let the
        // second half drift away silently, and a mutation to 50 ms passed it.
        assert_eq!(HALT_FRAME, crate::store::IDLE_FRAME);
        assert!(
            FULL_FRAME < HALT_FRAME,
            "the cheap lane must be the slow one"
        );

        // And it divides the breath exactly, so the peak and the trough land on
        // frames instead of beating against the sampling.
        let per_half = HALT_BREATH.as_millis() / HALT_FRAME.as_millis();
        assert_eq!(HALT_BREATH.as_millis() % HALT_FRAME.as_millis(), 0);
        assert_eq!(per_half, 14);

        // The smoothness claim, as arithmetic rather than as an assertion of
        // taste: `SineInOut` peaks at slope π/2, and the widest channel the fade
        // crosses is text_primary → negative_dim (0xe5 → 0x1d).
        let worst_alpha_step = std::f64::consts::FRAC_PI_2 / per_half as f64;
        let span = f64::from(0xe5 - 0x1d);
        assert!(
            worst_alpha_step * span < 24.0,
            "the breath steps {} colour levels a frame",
            worst_alpha_step * span
        );
    }

    #[test]
    fn each_lane_is_clamped_to_its_own_slowest_wake_not_to_the_fastest() {
        // The regression this pins: with one shared `FULL_FRAME` clamp, the
        // halt-only regime's 100 ms wake was cut to 32 ms of effect time every
        // frame, and the breath ran at a third of its stated period while every
        // other test still passed.
        assert_eq!(MAX_FULL_STEP, HALT_FRAME);
        assert_eq!(MAX_PANE_STEP, FULL_FRAME);
        assert!(
            MAX_FULL_STEP >= HALT_FRAME,
            "the full lane's clamp must not bind on its own designed wake"
        );
        assert!(MAX_PANE_STEP >= FX_FRAME, "nor the pane lane's on its own");
    }

    #[test]
    fn the_fast_wake_is_faster_than_the_beats_that_starved_it() {
        // The bug this constant exists for: `should_render` said "effects are
        // active, paint" and the only things waking the loop were a 100 ms
        // heartbeat and a 120 ms tick.
        assert!(FX_FRAME < crate::store::IDLE_FRAME);
        assert!(FX_FRAME < crate::store::TICK);
        assert!(FULL_FRAME < crate::store::IDLE_FRAME);
        assert!(FX_FRAME < FULL_FRAME);
    }

    #[test]
    fn the_capped_lane_steps_every_other_frame_and_loses_no_time() {
        let floor = FULL_FRAME;
        let mut debt = Duration::ZERO;
        // Woken at 60 fps, the capped lane steps on every second wake. This is
        // the assertion the constant is derived for: a 33 ms floor pays out on
        // every third 16 ms wake, which is 20 fps calling itself 30.
        assert_eq!(spend(&mut debt, FX_FRAME, floor), None);
        assert_eq!(spend(&mut debt, FX_FRAME, floor), Some(FULL_FRAME));
        assert_eq!(spend(&mut debt, FX_FRAME, floor), None);
        assert_eq!(spend(&mut debt, FX_FRAME, floor), Some(FULL_FRAME));

        // Over a second of 16 ms wakes the lane is told about every millisecond
        // that passed — a cap that dropped the skipped frames' time would make
        // the breath take twice its stated period.
        let mut debt = Duration::ZERO;
        let mut told = Duration::ZERO;
        for _ in 0..60 {
            told += spend(&mut debt, FX_FRAME, floor).unwrap_or_default();
        }
        assert_eq!(told + debt, FX_FRAME * 60);
        assert_eq!(
            told,
            FX_FRAME * 60,
            "with an even count nothing is left owed"
        );

        // A wake slower than the floor pays out every time.
        let mut debt = Duration::ZERO;
        assert_eq!(
            spend(&mut debt, Duration::from_millis(120), floor),
            Some(Duration::from_millis(120))
        );
    }
}

#[cfg(test)]
mod version_pair {
    use crate::theme::theme;
    use ratatui::{buffer::Buffer, layout::Rect, widgets::Widget};

    /// tachyonfx depends on `ratatui-core` rather than `ratatui`, so a ratatui
    /// bump can leave the two compiling against different `Buffer` types — the
    /// tree stays green and the effects silently apply to nothing. This fails
    /// the build instead. Retire it once real effect code covers the same seam.
    ///
    /// Every step asserts against the buffer it started from: a version-pair
    /// test that only checks it compiles is exactly the "code that exists but
    /// nothing exercises" shape it is here to catch.
    #[test]
    fn the_effect_and_widget_crates_write_to_ratatuis_own_buffer() {
        let area = Rect::new(0, 0, 40, 10);
        let mut buf = Buffer::empty(area);
        let empty = buf.clone();

        tui_big_text::BigText::builder()
            .lines(vec!["hi".into()])
            .build()
            .render(area, &mut buf);
        throbber_widgets_tui::Throbber::default().render(Rect::new(0, 0, 10, 1), &mut buf);
        assert_ne!(buf, empty, "the widget crates rendered nothing");

        // Fading needs something to fade, so the effect runs over the rendered
        // cells rather than an empty grid — on an empty grid a no-op and a
        // cross-crate Buffer mismatch look identical.
        let rendered = buf.clone();
        let mut effect =
            tachyonfx::fx::fade_to_fg(theme().accent, (100, tachyonfx::Interpolation::Linear));
        effect.process(tachyonfx::Duration::from_millis(16), &mut buf, area);
        assert_ne!(buf, rendered, "the tachyonfx effect rendered nothing");
    }
}
