//! The shell, pinned as a whole frame.
//!
//! One golden snapshot catches every layout regression at once; the targeted
//! asserts beside it say *which* facts the frame is not allowed to lose, so a
//! blessed-away golden cannot quietly take a governance statement with it.

mod harness;

use atlas::bus::{AppEvent, Channel, HttpResult};
use atlas::model::Snapshot;
use atlas::store::{Store, ViewId};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use harness::{fixture_store, frame_to_string, frame_to_string_at, line_with};
use std::time::{Duration, Instant};

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn store_from(json: &str) -> Store {
    let mut store = Store::default();
    let now = Instant::now();
    store.apply(AppEvent::ConnUp(Channel::Owner), now);
    store.apply(
        AppEvent::Snapshot(Box::new(serde_json::from_str::<Snapshot>(json).unwrap())),
        now,
    );
    store
}

/// A store that has seen the owner answer with something it cannot read.
fn malformed_store(with_snapshot: bool) -> Store {
    let mut store = if with_snapshot {
        fixture_store()
    } else {
        Store::default()
    };
    let now = store.last_snapshot_at.unwrap_or_else(Instant::now);
    store.apply(AppEvent::ConnUp(Channel::Owner), now);
    store.apply(
        AppEvent::Http(HttpResult::Malformed {
            url: "http://127.0.0.1:8765/api/tui".into(),
            error: "invalid type: string \"1e4\", expected f64".into(),
        }),
        now,
    );
    store
}

#[test]
fn the_shell_renders_the_desk_at_120x36() {
    insta::assert_snapshot!(frame_to_string(&fixture_store(), 120, 36));
}

#[test]
fn the_pulse_rail_is_headed_the_way_every_panel_is_headed() {
    // `▌ TITLE` is the panel vocabulary for the whole workstation; the rail is
    // where it first has to hold.
    let frame = frame_to_string(&fixture_store(), 120, 36);
    assert!(frame.contains("▌ PULSE"), "{frame}");
    assert!(frame.contains("▌ ATLAS READ"), "{frame}");
}

#[test]
fn the_status_line_states_the_posture() {
    // Supersedes `ui.rs::the_read_only_boundary_is_stated_on_screen`. Not
    // decoration: an operator must never have to wonder whether this surface
    // can place an order. It cannot, and it says so on every frame.
    let frame = frame_to_string(&fixture_store(), 120, 36);
    assert!(line_with(&frame, "GLASS").contains("GLASS"));
    // Even with no snapshot at all — the posture is a fact about the binary,
    // not about the data.
    assert!(frame_to_string(&Store::default(), 90, 24).contains("GLASS"));
}

#[test]
fn the_regime_reaches_the_frame_from_the_typed_snapshot() {
    // Ported from `ui.rs`. This line read "regime UNKNOWN" on every snapshot
    // the owner ever served, because the client dug a path that does not
    // exist. The pin is that the *rendered* line moves with the payload.
    let frame = frame_to_string(&fixture_store(), 120, 36);
    assert!(line_with(&frame, "regime").contains("CALM"), "{frame}");

    let stressed = store_from(r#"{"market": {"regime": {"regime": "stress"}}}"#);
    let frame = frame_to_string(&stressed, 120, 36);
    assert!(line_with(&frame, "regime").contains("STRESS"), "{frame}");
}

#[test]
fn the_nav_highlight_moves_with_the_number_keys() {
    let mut store = fixture_store();
    assert!(frame_to_string(&store, 120, 36).contains("▌1 DESK"));

    for (digit, marked) in [
        ('2', "▌2 MKTS"),
        ('3', "▌3 BOOK"),
        ('4', "▌4 RSCH"),
        ('5', "▌5 WORK"),
        ('6', "▌6 AUDIT"),
        ('7', "▌7 SETT"),
        ('1', "▌1 DESK"),
    ] {
        atlas::ui::shell::on_key(key(KeyCode::Char(digit)), &mut store);
        let frame = frame_to_string(&store, 120, 36);
        assert!(
            frame.contains(marked),
            "key {digit} did not mark {marked}:\n{frame}"
        );
        assert_eq!(
            frame.matches('▌').count() - frame.matches("▌ ").count(),
            1,
            "exactly one nav entry may be marked:\n{frame}"
        );
    }
}

#[test]
fn tab_cycles_the_views_and_wraps_in_both_directions() {
    let mut store = fixture_store();
    for expected in [
        ViewId::Markets,
        ViewId::Book,
        ViewId::Research,
        ViewId::Workforce,
        ViewId::Audit,
        ViewId::Settings,
        ViewId::Desk,
    ] {
        atlas::ui::shell::on_key(key(KeyCode::Tab), &mut store);
        assert_eq!(store.nav.view, expected);
    }
    atlas::ui::shell::on_key(key(KeyCode::BackTab), &mut store);
    assert_eq!(store.nav.view, ViewId::Settings, "BackTab wraps backwards");
}

#[test]
fn q_quits_and_r_refreshes_and_an_unclaimed_key_does_neither() {
    use atlas::cmd::Command;
    let mut store = fixture_store();
    assert_eq!(
        atlas::ui::shell::on_key(key(KeyCode::Char('q')), &mut store),
        Some(Command::Quit)
    );
    assert_eq!(
        atlas::ui::shell::on_key(key(KeyCode::Char('r')), &mut store),
        Some(Command::Refresh)
    );
    // Raw mode disables ISIG, so the reflex every operator has must be handled
    // as a keystroke or it does nothing at all.
    assert_eq!(
        atlas::ui::shell::on_key(
            KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
            &mut store
        ),
        Some(Command::Quit)
    );
    assert_eq!(
        atlas::ui::shell::on_key(key(KeyCode::Char('x')), &mut store),
        None
    );
    assert_eq!(
        store.nav.view,
        ViewId::Desk,
        "an unclaimed key moves nothing"
    );
}

#[test]
fn zen_and_fullscreen_are_claimed_even_though_they_do_nothing_yet() {
    // They are swallowed here so no later view can bind them and then have the
    // binding taken away when the layout modes land.
    let mut store = fixture_store();
    let before = frame_to_string(&store, 120, 36);
    for k in ['z', 'f'] {
        assert_eq!(
            atlas::ui::shell::on_key(key(KeyCode::Char(k)), &mut store),
            None
        );
    }
    assert_eq!(store.nav.view, ViewId::Desk);
    assert_eq!(
        frame_to_string(&store, 120, 36),
        before,
        "rendering is unchanged"
    );
}

#[test]
fn an_unreachable_owner_says_so_instead_of_rendering_an_empty_desk() {
    // Ported from `ui.rs`. The failure mode: a frame of `--` that reads as
    // "nothing is happening on your desk" when the truth is "I cannot see it".
    let frame = frame_to_string(&Store::default(), 100, 24);
    assert!(frame.contains("NO OWNER RUNTIME"), "{frame}");
    assert!(frame.contains("qlab tui"), "must name the remedy:\n{frame}");
}

#[test]
fn numbers_that_stopped_refreshing_say_so_rather_than_passing_as_current() {
    // A red connection chip says the owner is unreachable. It does not say the
    // prices, the drawdown and the read on screen are four minutes old, and an
    // operator reads the numbers, not the chip.
    let store = fixture_store();
    let arrived = store.last_snapshot_at.unwrap();

    let fresh = frame_to_string_at(&store, 120, 36, arrived + Duration::from_secs(9));
    assert!(
        !fresh.contains("STALE"),
        "one missed poll is not staleness:\n{fresh}"
    );

    let stale = frame_to_string_at(&store, 120, 36, arrived + Duration::from_secs(47));
    let status = line_with(&stale, "GLASS");
    assert!(
        status.contains("STALE 47s"),
        "the age itself, not just a flag: {status}"
    );
    // Beside the chip, not instead of it: reachable and current are two claims.
    assert!(status.contains("OWNER"), "{status}");
}

#[test]
fn a_desk_that_has_never_had_a_snapshot_is_not_stale() {
    // Nothing to be stale: the no-data panel is what speaks here, and a client
    // that opened a second ago must not accuse the owner of going quiet.
    let frame = frame_to_string_at(&Store::default(), 100, 24, Instant::now());
    assert!(!frame.contains("STALE"), "{frame}");
}

#[test]
fn a_payload_the_model_cannot_read_reaches_the_frame() {
    // The owner answers, so every connection chip is green and the old code
    // rendered "waiting for the first snapshot" for as long as it stayed
    // broken — an affirmative falsehood, with the reason only in the log.
    let frame = frame_to_string(&malformed_store(false), 100, 24);
    assert!(frame.contains("OWNER PAYLOAD MALFORMED"), "{frame}");
    assert!(
        frame.contains("expected f64"),
        "the reason has to reach the screen:\n{frame}"
    );
    assert!(!frame.contains("WAITING FOR THE FIRST SNAPSHOT"), "{frame}");
    assert!(line_with(&frame, "GLASS").contains("MALFORMED"), "{frame}");

    // With a desk already on screen the panel is not shown, so the status line
    // is the only thing that can say the newest payload was rejected.
    let frame = frame_to_string(&malformed_store(true), 120, 36);
    assert!(line_with(&frame, "GLASS").contains("MALFORMED"), "{frame}");
}

#[test]
fn a_payload_that_decodes_clears_the_accusation_from_the_frame() {
    let mut store = malformed_store(false);
    let now = Instant::now();
    store.apply(
        AppEvent::Snapshot(Box::new(
            serde_json::from_str::<Snapshot>(r#"{"atlas": {"mode": "research"}}"#).unwrap(),
        )),
        now,
    );
    let frame = frame_to_string(&store, 120, 36);
    assert!(
        !frame.contains("MALFORMED"),
        "a recovered owner stays accused:\n{frame}"
    );
}

#[test]
fn a_reachable_owner_with_no_snapshot_yet_says_pending_not_missing() {
    // "Not there" and "not yet" are different facts about the desk, and the
    // remedy for one is not the remedy for the other.
    let mut store = Store::default();
    store.apply(AppEvent::ConnUp(Channel::Owner), Instant::now());
    let frame = frame_to_string(&store, 100, 24);
    assert!(!frame.contains("NO OWNER RUNTIME"), "{frame}");
}

#[test]
fn the_desk_view_explains_why_the_desk_is_doing_what_it_is_doing() {
    // Ported from `app.rs::Desk::why`: a desk that is idle on purpose and one
    // that is broken look identical from outside, and a mode name answers
    // neither.
    // The panel is a wrapped paragraph, so the pins are on fragments that fit
    // one rendered line — asserting a whole sentence would fail on a resize
    // rather than on a regression.
    let frame = frame_to_string(&fixture_store(), 120, 36);
    assert!(frame.contains("Mode is PROPOSE"), "{frame}");
    assert!(frame.contains("No trigger has fired"), "{frame}");

    let blocked = store_from(
        r#"{"atlas": {"mode": "research"},
            "atlas_heartbeat": {"autonomous": true,
              "coordinator": {"driving": false,
                              "reason": "the `claude` CLI is not on PATH"}}}"#,
    );
    assert!(frame_to_string(&blocked, 120, 36).contains("not on PATH"));
}

#[test]
fn a_narrow_terminal_still_renders_without_panicking() {
    // Ported from `ui.rs`. Ratatui panics on some zero-area splits, and a
    // client that dies when the window is dragged narrow is worse than one
    // that truncates.
    let store = fixture_store();
    for (w, h) in [
        (40u16, 12u16),
        (20, 8),
        (80, 10),
        (200, 60),
        (1, 1),
        (34, 3),
    ] {
        let _ = frame_to_string(&store, w, h);
    }
}
