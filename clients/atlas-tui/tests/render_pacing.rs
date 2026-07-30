//! The frame budget, as a pure function of state rather than of the clock.
//!
//! Pacing is the one loop decision that has to be testable without a terminal:
//! `should_render` takes the last frame's stamp as data, so the idle-heartbeat
//! branch is exercised by arithmetic instead of by a sleep and a prayer.

use atlas::store::should_render;
use std::time::{Duration, Instant};

#[test]
fn renders_on_dirty_or_fx_or_idle_heartbeat() {
    let t0 = Instant::now();
    assert!(should_render(true, false, t0)); // dirty
    assert!(should_render(false, true, t0)); // effects running
    assert!(!should_render(false, false, Instant::now())); // idle, frame fresh
    assert!(should_render(false, false, t0 - Duration::from_millis(150))); // heartbeat
}
