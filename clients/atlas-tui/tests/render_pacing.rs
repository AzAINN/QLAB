//! The frame budget, as a pure function of state rather than of the clock.
//!
//! Pacing is the one loop decision that has to be testable without a terminal:
//! `should_render` takes both the last frame's stamp and `now` as data, so the
//! idle-heartbeat branch is exercised by arithmetic rather than by a sleep — and
//! cannot fail because the test thread was descheduled between two clock reads.

use atlas::store::should_render;
use std::time::{Duration, Instant};

#[test]
fn renders_on_dirty_or_fx_or_idle_heartbeat() {
    let t0 = Instant::now();
    let now = t0 + Duration::from_millis(50);
    assert!(should_render(true, false, t0, now)); // dirty
    assert!(should_render(false, true, t0, now)); // effects running
    assert!(!should_render(false, false, t0, now)); // idle, frame fresh
    assert!(should_render(
        false,
        false,
        t0,
        t0 + Duration::from_millis(150)
    )); // heartbeat
}
