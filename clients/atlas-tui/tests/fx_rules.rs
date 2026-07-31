//! The motion vocabulary: that every desk transition reaches an effect, that
//! the effects end, and that the one which does not end is retired by hand.
//!
//! These drive the real `EffectManager` over a real `Buffer` with a mocked
//! clock — elapsed time is handed in, never read — so a rule can be pinned by
//! arithmetic rather than by watching the client. The bug class this file exists
//! for is invariant 10's: three seams have shipped as code that existed and
//! nothing invoked, and a motion rule is the easiest kind to write that way,
//! because the only symptom of a rule with no call site is a desk that is calm.

use atlas::fx::{Fx, FLASH, FULL_FRAME, FX_FRAME, REVEAL, TWEEN};
use atlas::store::Trigger;
use atlas::theme::theme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use std::time::{Duration, Instant};

/// The frame these tests pretend to be drawing into — the same 120×36 the
/// goldens use, so the rects below are the ones the shell really publishes.
const FRAME: Rect = Rect {
    x: 0,
    y: 0,
    width: 120,
    height: 36,
};
const CONTENT: Rect = Rect {
    x: 9,
    y: 1,
    width: 76,
    height: 34,
};
const REGIME: Rect = Rect {
    x: 87,
    y: 20,
    width: 33,
    height: 7,
};

/// Long enough for every finite effect in the vocabulary to be spent.
const PAST_EVERYTHING: Duration = Duration::from_secs(3);

/// An `Fx` with a frame's worth of layout already published, so the rules aim
/// at real cells instead of at the zero rects a client that has not drawn holds.
///
/// `theme::init` is deliberately never called: `theme()` locks in truecolor on
/// first read, so these tests get a deterministic palette without a
/// process-global mutation that would race whichever test in this binary
/// happened to paint a buffer first.
fn staged() -> Fx {
    let fx = Fx::default();
    fx.rects.frame.set(FRAME);
    fx.rects.content.set(CONTENT);
    fx.rects.regime.set(REGIME);
    fx.rects.read.set(Rect::new(87, 28, 33, 6));
    fx.rects.chips.set(Rect::new(80, 35, 40, 1));
    fx
}

/// A buffer with something in every cell.
///
/// Effects that fade or dissolve are no-ops on an empty grid, where "the rule
/// never ran" and "the rule ran over nothing" look identical.
fn painted() -> Buffer {
    let mut buf = Buffer::empty(FRAME);
    for y in FRAME.y..FRAME.bottom() {
        for x in FRAME.x..FRAME.right() {
            buf[(x, y)].set_symbol("#").set_style(
                Style::default()
                    .fg(theme().text_primary)
                    .bg(theme().bg_base),
            );
        }
    }
    buf
}

/// Drive the loop's effect pass for `span`, in the 16 ms wakes it really uses.
fn run_for(fx: &mut Fx, span: Duration) {
    let mut buf = painted();
    for _ in 0..(span.as_millis() / FX_FRAME.as_millis()) {
        fx.process(FX_FRAME, &mut buf, FRAME);
    }
}

/// Every variant of the diff, so the list below cannot quietly go short.
fn every_trigger() -> Vec<Trigger> {
    vec![
        Trigger::RegimeChanged,
        Trigger::DrawdownTierWorse,
        Trigger::Halted,
        Trigger::Resumed,
        Trigger::ApprovalCreated,
        Trigger::PhaseAdvanced,
        Trigger::PlanExecuted,
        Trigger::QuoteTick("SPY".into()),
        Trigger::ReadChanged,
    ]
}

/// Whether the vocabulary gives this transition a tachyonfx effect.
///
/// An exhaustive `match`, deliberately: adding a variant to the diff fails to
/// compile here as well as in `Fx::rules`, so nobody can add a desk transition
/// without both writing its motion and saying out loud what it should be.
fn has_effect(trigger: &Trigger) -> bool {
    match trigger {
        Trigger::RegimeChanged
        | Trigger::DrawdownTierWorse
        | Trigger::Halted
        | Trigger::Resumed
        | Trigger::ApprovalCreated
        | Trigger::PhaseAdvanced
        | Trigger::PlanExecuted
        | Trigger::ReadChanged => true,
        // The flash lane owns a quote: it lights the two cells that moved, and
        // a sweep over the grid would animate every row that did not.
        Trigger::QuoteTick(_) => false,
    }
}

#[test]
fn every_trigger_variant_reaches_the_rule_that_moves_it() {
    let now = Instant::now();
    // Past both style-lane animations, so what is left is the effect managers
    // alone — otherwise a quote's flash would read as a tachyonfx rule.
    let after = now + FLASH.max(REVEAL) + Duration::from_millis(1);

    for trigger in every_trigger() {
        let mut fx = staged();
        fx.on_trigger(&trigger, now, false);
        assert_eq!(
            fx.active(after),
            has_effect(&trigger),
            "{trigger:?} did not reach the effect its rule promises"
        );
    }
}

#[test]
fn a_quote_still_moves_the_only_lane_that_should_move_it() {
    // The other half of the arm above: `QuoteTick` has no tachyonfx rule, which
    // must not be confused with having no motion at all.
    let now = Instant::now();
    let mut fx = staged();
    fx.on_trigger(&Trigger::QuoteTick("SPY".into()), now, false);
    assert!(fx.active(now), "the flash lane never saw the quote");
    assert!(!fx.active(now + FLASH), "and it has to decay out");
}

#[test]
fn the_managers_go_idle_once_the_longest_effect_is_spent() {
    // Every rule fired at once, then three seconds of frames. Anything still
    // running after that is an effect with no end, and an end is what lets the
    // loop go back to blocking on the channel instead of waking at 60 fps for
    // the rest of the session.
    let now = Instant::now();
    let mut fx = staged();
    for trigger in every_trigger() {
        fx.on_trigger(&trigger, now, false);
    }
    assert!(fx.active(now), "nothing started");
    run_for(&mut fx, PAST_EVERYTHING);
    assert!(
        !fx.active(now + PAST_EVERYTHING + FLASH + REVEAL + TWEEN),
        "an effect outlived every duration in the vocabulary"
    );
}

#[test]
fn a_halted_desk_keeps_pulsing_until_a_resume_replaces_it() {
    // The HALT lifecycle *is* the keyed replace: the effect repeats forever on
    // purpose, because one that stopped on its own would say the desk had
    // recovered. Only `Resumed`, adding under the same key, can retire it.
    let now = Instant::now();
    let mut fx = staged();
    fx.on_trigger(&Trigger::Halted, now, false);
    run_for(&mut fx, Duration::from_secs(10));
    assert!(
        fx.active(now + Duration::from_secs(10)),
        "the halt treatment ended on its own"
    );

    fx.on_trigger(&Trigger::Resumed, now, false);
    run_for(&mut fx, PAST_EVERYTHING);
    assert!(
        !fx.active(now + PAST_EVERYTHING),
        "the restore fade did not cancel the halt it replaced"
    );
}

#[test]
fn the_opening_snapshot_announces_the_desk_without_stacking_a_sweep() {
    // The first snapshot is diffed against nothing, so it fires the state it
    // arrived in — wanted, because an operator opening onto a halted desk has
    // to see the halt. The regime sweep is the one arm suppressed: it would
    // cross a strip in the same frame the whole desk is arriving in.
    let now = Instant::now();
    let after = now + FLASH.max(REVEAL) + Duration::from_millis(1);

    let mut opening = staged();
    opening.on_trigger(&Trigger::RegimeChanged, now, true);
    assert!(!opening.active(after), "the open stacked a regime sweep");

    let mut later = staged();
    later.on_trigger(&Trigger::RegimeChanged, now, false);
    assert!(
        later.active(after),
        "suppressing on the open must not suppress the real thing"
    );

    // Everything else the first snapshot fires still moves.
    let mut halted = staged();
    halted.on_trigger(&Trigger::Halted, now, true);
    assert!(
        halted.active(after),
        "opening onto a halted desk has to show the halt"
    );

    let mut read = staged();
    read.on_trigger(&Trigger::ReadChanged, now, true);
    assert_eq!(
        read.flashes.revealed(now),
        0.0,
        "the read still types itself in on the open"
    );
    assert!(read.active(after), "and the rail's read still washes");
}

#[test]
fn an_effect_paints_only_the_rect_its_rule_aimed_at() {
    // The seam this asserts is `aim`: without it every rule would run over the
    // whole frame, and a regime sweep would cross the book.
    let now = Instant::now();
    let mut fx = staged();
    fx.on_trigger(&Trigger::RegimeChanged, now, false);

    let mut buf = painted();
    let before = buf.clone();
    fx.process(FX_FRAME, &mut buf, FRAME);
    assert_ne!(buf, before, "the regime sweep rendered nothing at all");

    for y in FRAME.y..FRAME.bottom() {
        for x in FRAME.x..FRAME.right() {
            if REGIME.contains((x, y).into()) {
                continue;
            }
            assert_eq!(
                buf[(x, y)],
                before[(x, y)],
                "the strip's sweep reached ({x}, {y}), outside {REGIME:?}"
            );
        }
    }
}

#[test]
fn a_running_effect_follows_its_pane_through_a_resize() {
    // Why the rects are `RefRect`s rather than the plain rects the effect was
    // built with: a terminal resized mid-sweep would otherwise leave the effect
    // painting a strip that has moved out from under it.
    let now = Instant::now();
    let mut fx = staged();
    fx.on_trigger(&Trigger::RegimeChanged, now, false);

    let moved = Rect::new(4, 4, 20, 6);
    fx.rects.regime.set(moved);
    let mut buf = painted();
    let before = buf.clone();
    fx.process(FX_FRAME, &mut buf, FRAME);

    assert_ne!(
        buf[(moved.x, moved.y)],
        before[(moved.x, moved.y)],
        "the sweep did not move with the strip"
    );
    assert_eq!(
        buf[(REGIME.x, REGIME.y)],
        before[(REGIME.x, REGIME.y)],
        "and it kept painting where the strip used to be"
    );
}

#[test]
fn an_effect_never_opens_part_way_through_itself() {
    // The gap this guards is real: while idle the loop draws on the 100 ms
    // heartbeat, so a coalesce fired by a keystroke arrives with up to that much
    // wall time since the last frame — and an effect that has existed for none
    // of it would jump a third of the way in on its first paint.
    let now = Instant::now();
    let mut fx = staged();
    fx.on_view_switch();

    let mut buf = painted();
    fx.process(Duration::from_secs(1), &mut buf, FRAME);
    assert!(
        fx.active(now),
        "one long frame swallowed a 300 ms transition whole"
    );
}

#[test]
fn the_full_frame_lane_steps_at_half_the_rate_the_panes_do() {
    // Part I's cap, observed on the buffer rather than on a counter. A halted
    // desk repaints every cell of the terminal until it resumes, and at 60 fps
    // that is twice the rate anyone can see.
    let now = Instant::now();
    let mut fx = staged();
    fx.on_trigger(&Trigger::Halted, now, false);

    let mut buf = painted();
    let before = buf.clone();
    fx.process(FX_FRAME, &mut buf, FRAME);
    assert_eq!(
        buf, before,
        "the full-frame lane advanced on the first 16 ms wake"
    );
    fx.process(FX_FRAME, &mut buf, FRAME);
    assert_ne!(
        buf, before,
        "and never advanced at all — a cap is not a mute"
    );
}

#[test]
fn the_loop_blocks_when_nothing_moves_and_wakes_fast_when_something_does() {
    // The finding this task opened on: `should_render` had always said "paint
    // while effects are active", and nothing woke the loop faster than the
    // 100 ms heartbeat, so a 600 ms reveal arrived in five visible chunks.
    let now = Instant::now();
    let mut fx = staged();
    assert_eq!(
        fx.budget(now),
        None,
        "an idle desk must block on the channel"
    );

    fx.on_trigger(&Trigger::RegimeChanged, now, false);
    assert_eq!(fx.budget(now), Some(FX_FRAME));
    run_for(&mut fx, PAST_EVERYTHING);
    assert_eq!(
        fx.budget(now + PAST_EVERYTHING),
        None,
        "a spent effect has to hand the loop back to the channel"
    );

    // A desk whose only motion is the halt pulse is repainted at the rate that
    // pulse advances, not at 60 fps for a lane that steps at 30.
    fx.on_trigger(&Trigger::Halted, now, false);
    assert_eq!(fx.budget(now + PAST_EVERYTHING), Some(FULL_FRAME));
}

#[test]
fn a_view_switch_coalesces_the_content_and_is_not_a_desk_trigger() {
    // Which pane an operator is looking at is not something the owner said, so
    // it is not a `Trigger`. The rule is still here, and still has a caller —
    // `ingest` fires it at the one place that can see the nav move.
    let now = Instant::now();
    let mut fx = staged();
    fx.on_view_switch();

    let mut buf = painted();
    let before = buf.clone();
    fx.process(FX_FRAME, &mut buf, FRAME);
    // Cell by cell rather than one probe: a coalesce brings cells back on their
    // own thresholds, so which particular cell has arrived by frame one is not
    // a fact worth pinning — that the content moved and the chrome did not is.
    let moved = |rect: Rect| {
        rect.rows()
            .flat_map(|row| row.columns())
            .any(|c| buf[(c.x, c.y)] != before[(c.x, c.y)])
    };
    assert!(moved(CONTENT), "the new view did not materialise");
    assert!(
        !moved(Rect::new(0, 0, FRAME.width, 1)),
        "the coalesce reached the ticker tape"
    );
    assert!(
        !moved(REGIME),
        "the coalesce reached the pulse rail beside the content"
    );

    run_for(&mut fx, Duration::from_millis(400));
    assert!(
        !fx.active(now + Duration::from_millis(400)),
        "a 300 ms coalesce that is still running at 400 ms is not a transition"
    );
}
