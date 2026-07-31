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
/// the fastest an "active" effect could be sampled was roughly eight times a
/// second. The read's 600 ms reveal typed itself in in five visible chunks. The
/// loop waits with this as a timeout instead of blocking on the channel while
/// anything is moving, and blocks again the moment nothing is.
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
/// A full-frame effect rewrites every cell of the terminal, and the halt
/// treatment repeats until the desk resumes; at 60 fps that is a permanent
/// full-buffer rewrite at twice the rate anyone can see. The lane is separate
/// rather than throttled in place because one `EffectManager` advances every
/// effect it holds with one elapsed duration — there is no per-effect skip.
pub const FULL_FRAME: Duration = Duration::from_millis(2 * FX_FRAME_MS);

/// The most time one frame may advance an effect.
///
/// While anything is moving the loop wakes every `FX_FRAME`, so a gap wider than
/// this can only mean the loop was idle — and an effect registered in the very
/// iteration that ended the idle has not been playing for the length of it.
/// Unclamped, a coalesce fired by a keystroke 120 ms after the last heartbeat
/// frame would open a third of the way through itself, and the transition an
/// operator sees would be the tail of one.
///
/// The trade is deliberate: under a terminal so slow that frames are genuinely
/// dropped, an effect plays for its full count of frames and takes longer in
/// wall time. On a cell grid that is the better failure — a transition that
/// skips is worse than one that lingers.
const MAX_STEP: Duration = FULL_FRAME;

/// Part I's motion vocabulary, in milliseconds.
///
/// Durations rather than magic numbers at the call sites so the table below
/// reads as the vocabulary it is meant to be.
const COALESCE: u32 = 300;
const REGIME_SWEEP: u32 = 400;
const REGIME_SETTLE: u32 = 400;
const ALERT_HALF: u32 = 200;
const PLAN_SWEEP: u32 = 500;
const HALT_HALF: u32 = 700;
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
/// `Default` is derived only because `EffectManager<K>`'s own derived `Default`
/// demands `K: Default` — a quirk of the derive, not a decision. Nothing reads
/// the default key, and no rule may come to depend on which variant it is.
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
                self.full.add_unique_effect(
                    FxKey::Halt,
                    fx::repeating(fx::ping_pong(fx::fade_to_fg(
                        t.negative_dim,
                        (HALT_HALF, Interpolation::SineInOut),
                    ))),
                );
            }
            Trigger::Resumed => {
                self.full.add_unique_effect(
                    FxKey::Halt,
                    fx::fade_from_fg(t.negative_dim, (RESTORE, Interpolation::CubicOut)),
                );
            }
            // Not emitted yet — Task 18 diffs the approvals queue. The rule is
            // here so the match stays exhaustive and the arm is one line of
            // wiring rather than a design decision on that task's critical path.
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
            Trigger::PhaseAdvanced => {
                self.panes.add_unique_effect(
                    FxKey::Toast,
                    aim(
                        &self.rects.chips,
                        fx::fade_from_fg(t.accent, (CHIP_HALF * 2, Interpolation::CubicOut)),
                    ),
                );
            }
            // Not emitted yet — Task 18 diffs the plan ledger.
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
    /// The two cadences are not interchangeable: a desk whose *only* motion is
    /// the halt pulse must not be repainted at 60 fps for a lane that advances
    /// at 30.
    pub fn budget(&self, now: Instant) -> Option<Duration> {
        budget(
            self.flashes.active(now) || self.gauge.active(now) || self.panes.is_running(),
            self.full.is_running(),
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
    /// `elapsed` is clamped: see `MAX_STEP`.
    pub fn process(&mut self, elapsed: Duration, buf: &mut Buffer, area: Rect) {
        let elapsed = elapsed.min(MAX_STEP);
        self.panes.process_effects(elapsed.into(), buf, area);
        if let Some(spent) = spend(&mut self.debt, elapsed, FULL_FRAME) {
            self.full.process_effects(spent.into(), buf, area);
        }
    }
}

/// Bind an effect to a rect that may move under it.
fn aim(rect: &RefRect, effect: Effect) -> Effect {
    fx::dynamic_area(rect.clone(), effect)
}

/// The loop's wait, given which lanes are running.
///
/// Separated from `Fx` so the choice that fixes the starved-effect bug is a
/// function of two booleans that a test can enumerate, rather than something
/// only reachable by driving a terminal.
pub const fn budget(fast: bool, full: bool) -> Option<Duration> {
    match (fast, full) {
        (true, _) => Some(FX_FRAME),
        (false, true) => Some(FULL_FRAME),
        (false, false) => None,
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
        assert_eq!(budget(false, false), None, "idle blocks on the channel");
        assert_eq!(budget(true, false), Some(FX_FRAME));
        assert_eq!(budget(true, true), Some(FX_FRAME));
        assert_eq!(
            budget(false, true),
            Some(FULL_FRAME),
            "a desk whose only motion is the halt pulse is repainted at 30 fps"
        );
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
        // a 700 ms pulse take 1.4 s.
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
