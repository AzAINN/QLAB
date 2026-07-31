//! The shell, pinned as a whole frame.
//!
//! One golden snapshot catches every layout regression at once; the targeted
//! asserts beside it say *which* facts the frame is not allowed to lose, so a
//! blessed-away golden cannot quietly take a governance statement with it.

mod harness;

use atlas::bus::{AppEvent, Channel, HttpResult, SseEvent};
use atlas::fx::{FlashKey, FlashTracker};
use atlas::model::Snapshot;
use atlas::store::{Store, ViewId};
use atlas::ui::views::Views;
use atlas::theme::Theme;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use harness::{fixture_store, frame_to_string, frame_to_string_at, line_with, row_styles};
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

/// A store carrying its own staleness threshold, holding one snapshot that
/// arrived at `at`.
fn store_with_threshold(stale_after: Duration, at: Instant) -> Store {
    let mut store = Store::new(stale_after);
    store.apply(AppEvent::ConnUp(Channel::Owner), at);
    store.apply(
        AppEvent::Snapshot(Box::new(
            serde_json::from_str::<Snapshot>(r#"{"portfolio": {"equity": 1.0}}"#).unwrap(),
        )),
        at,
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

/// The seven nav entries as the rail renders them once the shell has marked one.
///
/// Spelled out rather than counted off the frame's `▌` glyphs: `▌` is the
/// workstation's accent bar everywhere — panel headers wear it, and so does the
/// markets grid's selected row — so the only reading that stays about *the nav*
/// is the entries themselves.
const NAV_MARKED: [&str; 7] = [
    "▌1 DESK",
    "▌2 MKTS",
    "▌3 BOOK",
    "▌4 RSCH",
    "▌5 WORK",
    "▌6 AUDIT",
    "▌7 SETT",
];

#[test]
fn the_nav_highlight_moves_with_the_number_keys() {
    let mut store = fixture_store();
    let mut views = Views::new();
    assert!(frame_to_string(&store, 120, 36).contains(NAV_MARKED[0]));

    // Starting one past the entry the shell opens on, so the seventh press is
    // the one that has to come back to DESK.
    for marked in NAV_MARKED.iter().cycle().skip(1).take(NAV_MARKED.len()) {
        let digit = marked.chars().nth(1).expect("every entry carries its digit");
        atlas::ui::shell::on_key(key(KeyCode::Char(digit)), &mut store, &mut views);
        let frame = frame_to_string(&store, 120, 36);
        assert!(
            frame.contains(marked),
            "key {digit} did not mark {marked}:\n{frame}"
        );
        assert_eq!(
            NAV_MARKED.iter().filter(|e| frame.contains(*e)).count(),
            1,
            "exactly one nav entry may be marked:\n{frame}"
        );
    }
}

#[test]
fn tab_cycles_the_views_and_wraps_in_both_directions() {
    let mut store = fixture_store();
    let mut views = Views::new();
    for expected in [
        ViewId::Markets,
        ViewId::Book,
        ViewId::Research,
        ViewId::Workforce,
        ViewId::Audit,
        ViewId::Settings,
        ViewId::Desk,
    ] {
        atlas::ui::shell::on_key(key(KeyCode::Tab), &mut store, &mut views);
        assert_eq!(store.nav.view, expected);
    }
    atlas::ui::shell::on_key(key(KeyCode::BackTab), &mut store, &mut views);
    assert_eq!(store.nav.view, ViewId::Settings, "BackTab wraps backwards");
}

#[test]
fn q_quits_and_r_refreshes_and_an_unclaimed_key_does_neither() {
    use atlas::cmd::Command;
    let mut store = fixture_store();
    let mut views = Views::new();
    assert_eq!(
        atlas::ui::shell::on_key(key(KeyCode::Char('q')), &mut store, &mut views),
        Some(Command::Quit)
    );
    assert_eq!(
        atlas::ui::shell::on_key(key(KeyCode::Char('r')), &mut store, &mut views),
        Some(Command::Refresh)
    );
    // Raw mode disables ISIG, so the reflex every operator has must be handled
    // as a keystroke or it does nothing at all.
    assert_eq!(
        atlas::ui::shell::on_key(
            KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
            &mut store,
            &mut views
        ),
        Some(Command::Quit)
    );
    assert_eq!(
        atlas::ui::shell::on_key(key(KeyCode::Char('x')), &mut store, &mut views),
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
    let mut views = Views::new();
    let before = frame_to_string(&store, 120, 36);
    for k in ['z', 'f'] {
        assert_eq!(
            atlas::ui::shell::on_key(key(KeyCode::Char(k)), &mut store, &mut views),
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
fn the_staleness_threshold_is_the_pollers_fact_not_the_renderers() {
    // It was a literal `10 s` in the shell beside a `3 s` poll, with nothing
    // tying them together: a cadence change would have widened the window in
    // which stale marks render as current, and no test would have noticed. The
    // pin is that the *frame* moves with the threshold it is given.
    let arrived = Instant::now();
    let patient = store_with_threshold(Duration::from_secs(60), arrived);
    let frame = frame_to_string_at(&patient, 120, 36, arrived + Duration::from_secs(47));
    assert!(
        !frame.contains("STALE"),
        "the renderer is still deciding for itself:\n{frame}"
    );

    let twitchy = store_with_threshold(Duration::from_secs(2), arrived);
    let frame = frame_to_string_at(&twitchy, 120, 36, arrived + Duration::from_secs(9));
    assert!(line_with(&frame, "GLASS").contains("STALE 9s"), "{frame}");
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

/// One `quote` frame as the owner writes it.
fn quote(rows: serde_json::Value) -> AppEvent {
    AppEvent::Sse(SseEvent {
        kind: "quote".into(),
        payload: serde_json::json!({ "rows": rows }),
        ts: Some("2026-07-30T12:00:01+00:00".into()),
        id: Some("q1".into()),
    })
}

#[test]
fn a_quote_reaches_the_ticker_without_a_new_snapshot() {
    // The whole point of the overlay: a price that moved on the stream is on
    // screen before the next three-second poll, and the snapshot behind it is
    // untouched.
    let mut store = fixture_store();
    let arrived = store.last_snapshot_at.unwrap();
    let at = arrived + Duration::from_millis(900);
    store.apply(
        quote(serde_json::json!([{"ticker": "SPY", "price": 731.11, "change_1d": 0.0042}])),
        at,
    );

    let frame = frame_to_string_at(&store, 120, 36, at);
    let row = frame.lines().next().unwrap();
    assert!(row.contains("SPY 731.11 ▲0.42%"), "{row}");
    assert!(!row.contains("729.46"), "the poll's price survived: {row}");
    assert!(
        row.contains("ACWI 152.47"),
        "an asset the stream did not mention still comes off the snapshot: {row}"
    );
}

#[test]
fn a_quote_tick_flashes_the_price_cell_and_the_flash_decays_out() {
    // The flash is what makes a moving tape readable at a glance. It has to be
    // on the price *cell* — a whole-row highlight says the row is selected,
    // which is a different fact — and it has to end.
    let mut store = fixture_store();
    let arrived = store.last_snapshot_at.unwrap();
    let at = arrived + Duration::from_millis(900);
    store.apply(
        quote(serde_json::json!([{"ticker": "SPY", "price": 731.11, "change_1d": 0.0042}])),
        at,
    );
    let mut fx = FlashTracker::default();
    fx.flash(FlashKey::price("SPY"), at);

    let t = Theme::truecolor();
    let lit = row_styles(&store, &fx, 120, 36, at, 0);
    let flashed: String = lit
        .iter()
        .filter(|(_, style)| style.bg == Some(t.accent_dim))
        .map(|(symbol, _)| symbol.as_str())
        .collect();
    // The tape tiles, so the lit cells are the price once per repetition —
    // never a neighbouring symbol, an arrow, or a gap.
    assert!(!flashed.is_empty(), "the quote did not light anything");
    assert_eq!(
        flashed.replace("731.11", ""),
        "",
        "only the price cell may carry the flash: {flashed}"
    );

    // And 700 ms later the row is back to a plain quote.
    let cooled = row_styles(&store, &fx, 120, 36, at + Duration::from_millis(700), 0);
    assert!(
        cooled
            .iter()
            .all(|(_, style)| style.bg != Some(t.accent_dim)),
        "a flash that never decays lights the row for the session"
    );
}

#[test]
fn the_tape_moves_one_cell_per_beat() {
    // The ticker is one of the three indicators that claim this client is
    // alive. A row that does not move while ticks arrive is the exact shape a
    // hung client has.
    let store = fixture_store();
    let now = store.last_snapshot_at.unwrap();
    let first = frame_to_string_at(&store, 120, 36, now);

    let mut moved = fixture_store();
    moved.apply(AppEvent::Tick, now);
    let second = frame_to_string_at(&moved, 120, 36, now);
    assert_ne!(
        first.lines().next(),
        second.lines().next(),
        "the tape did not advance on a tick"
    );
    // And the desk under it did not move: a beat is not news. (The glyph is the
    // one other thing a tick drives, on its mood's own slower tempo.)
    assert_eq!(line_with(&first, "GLASS"), line_with(&second, "GLASS"));
    assert_eq!(line_with(&first, "▌1 DESK"), line_with(&second, "▌1 DESK"));
}

#[test]
fn a_stream_dropping_frames_says_so_beside_the_chip() {
    // A green SSE dot says the subscription is open. It does not say every
    // event it delivered was readable, and an audit stream quietly losing rows
    // is exactly the failure a governed desk may not have.
    let mut store = fixture_store();
    let now = store.last_snapshot_at.unwrap();
    store.apply(AppEvent::ConnUp(Channel::Stream), now);
    let clean = frame_to_string_at(&store, 120, 36, now);
    assert!(!clean.contains("STREAM"), "{clean}");

    for _ in 0..3 {
        store.apply(
            AppEvent::Sse(SseEvent {
                kind: "stream.malformed".into(),
                payload: serde_json::json!({"raw": "{oops"}),
                ts: None,
                id: None,
            }),
            now,
        );
    }
    let frame = frame_to_string_at(&store, 120, 36, now);
    let status = line_with(&frame, "GLASS");
    assert!(status.contains("STREAM ⚠ 3"), "{status}");
    assert!(
        status.contains("SSE"),
        "beside the chip, not instead: {status}"
    );
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
