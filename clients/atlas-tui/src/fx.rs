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

use crate::store::Trigger;
use crate::theme::theme;
use ratatui::style::{Modifier, Style};
use std::collections::HashMap;
use std::time::{Duration, Instant};

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
            // Task 15 gives the rest their effects. A trigger with no motion
            // behind it is not a bug — the diff is deliberately ahead of the
            // vocabulary — but one with motion and no test would be.
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
