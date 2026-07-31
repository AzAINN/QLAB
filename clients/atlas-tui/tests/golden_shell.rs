//! The shell, pinned as a whole frame.
//!
//! One golden snapshot catches every layout regression at once; the targeted
//! asserts beside it say *which* facts the frame is not allowed to lose, so a
//! blessed-away golden cannot quietly take a governance statement with it.

mod harness;

use atlas::bus::{AppEvent, Channel, HttpResult, SseEvent};
use atlas::fx::{FlashKey, Fx, TWEEN};
use atlas::model::Snapshot;
use atlas::store::{Store, ViewId};
use atlas::theme::Theme;
use atlas::ui::shell::{NAV_W, PULSE_W};
use atlas::ui::views::Views;
use atlas::ui::widgets::pulse;
use atlas::ui::widgets::toast::{self, ToastQueue};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use harness::{
    body_style_of, fixture_store, fixture_store_with_panel, frame_to_string, frame_to_string_at,
    frame_to_string_fx, frame_with_toasts, line_with, row_styles, Client,
};
use ratatui::layout::Rect;
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
        let digit = marked
            .chars()
            .nth(1)
            .expect("every entry carries its digit");
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

    // And an hour later it is still a number an operator can read at a glance.
    // `STALE 3600s` is arithmetic homework beside the thing it is competing with
    // for attention, which is the desk.
    let old = frame_to_string_at(&store, 120, 36, arrived + Duration::from_secs(3_600));
    assert!(line_with(&old, "GLASS").contains("STALE 1h"), "{old}");
    let hour = frame_to_string_at(&store, 120, 36, arrived + Duration::from_secs(600));
    assert!(line_with(&hour, "GLASS").contains("STALE 10m"), "{hour}");
}

#[test]
fn the_status_line_says_which_owner_it_is_looking_at_and_drops_it_before_a_chip() {
    // An operator with two desks up — or one owner on a port they did not
    // choose — otherwise reads a chip run that names no host and has to guess
    // which desk it is about.
    let store = fixture_store();
    let status_of = |w: u16| {
        let frame = frame_to_string(&store, w, 36);
        line_with(&frame, "GLASS").to_string()
    };
    assert!(status_of(120).contains("http://127.0.0.1:8765"));

    // It is the one thing on the line that gives way. Every chip beside it is a
    // claim about the desk; this is only where the desk is — and a line that ran
    // past the frame would clip the GLASS badge off the right-hand end, which is
    // the statement that may never leave the frame.
    let tight = status_of(60);
    assert!(!tight.contains("127.0.0.1"), "{tight}");
    assert!(tight.contains("GLASS"), "{tight}");

    // A client that was never told where it is looking says nothing rather than
    // inventing a default — `Some("")` is absent here as everywhere.
    let frame = frame_to_string(&Store::default(), 120, 36);
    assert!(!frame.contains("http://"), "{frame}");
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
fn a_live_stream_keeps_its_own_prices_bright_while_the_aggregate_goes_stale() {
    // Two feeds refresh a price and either can die alone. The tape dimmed on the
    // snapshot's age alone, so a dead poller greyed out a row of quotes that
    // were a second old — the exact lie the dimming exists to prevent, in the
    // other direction. The `STALE` chip stays on the aggregate: it is about the
    // desk as a whole, and the stream speaks for five prices out of it.
    let t = Theme::truecolor();
    let mut store = fixture_store();
    let arrived = store.last_snapshot_at.unwrap();
    let quoted = arrived + Duration::from_secs(60);
    store.apply(
        quote(serde_json::json!([{"ticker": "SPY", "price": 731.11, "change_1d": 0.0042}])),
        quoted,
    );
    let now = quoted + Duration::from_secs(1);

    let frame = frame_to_string_at(&store, 120, 36, now);
    assert!(
        line_with(&frame, "GLASS").contains("STALE"),
        "the aggregate really is stale here:\n{frame}"
    );

    let tape = row_styles(&store, &Fx::default(), 120, 36, now, 0);
    let toned = |needle: &str| -> Vec<ratatui::style::Color> {
        let row: String = tape.iter().map(|(symbol, _)| symbol.as_str()).collect();
        let at = row.find(needle).expect(needle);
        // Byte offset back to a cell index — the row carries `▲` and `▼`.
        let mut cell = 0;
        let mut seen = 0;
        for (i, (symbol, _)) in tape.iter().enumerate() {
            if seen == at {
                cell = i;
                break;
            }
            seen += symbol.len();
        }
        tape[cell..cell + needle.chars().count()]
            .iter()
            .filter_map(|(_, style)| style.fg)
            .collect()
    };
    assert!(
        toned("731.11").iter().all(|fg| *fg == t.text_primary),
        "the stream's own price was dimmed by the poller's silence"
    );
    assert!(
        toned("ACWI").iter().all(|fg| *fg == t.text_tertiary),
        "a row only the dead poller feeds still has to dim"
    );
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
    let mut fx = Fx::default();
    fx.flashes.flash(FlashKey::price("SPY"), at);

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
    assert!(!clean.contains('⚠'), "{clean}");
    assert_eq!(
        line_with(&clean, "GLASS").matches("SSE").count(),
        1,
        "a healthy stream is one chip:\n{clean}"
    );

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
    // One feed, one name. The count used to be headed `STREAM` beside a chip
    // headed `SSE`, which is two feeds as far as a reader is concerned.
    assert!(status.contains("SSE ⚠ 3"), "{status}");
    assert!(!status.contains("STREAM"), "{status}");

    // And the dot goes with it: a subscription that is open and losing rows is
    // degraded, which is neither of the two colours a bool can carry.
    let t = Theme::truecolor();
    let dots: Vec<_> = row_styles(&store, &Fx::default(), 120, 36, now, 35)
        .into_iter()
        .filter(|(symbol, _)| symbol == "●")
        .map(|(_, style)| style.fg)
        .collect();
    assert_eq!(
        dots,
        vec![Some(t.warning), Some(t.positive)],
        "the SSE dot stayed green over a stream that is dropping frames"
    );
}

#[test]
fn an_owner_answering_with_something_unreadable_is_degraded_not_up() {
    // The affirmative falsehood Task 6 named, now in the dot as well as the
    // panel: the owner is reachable, so red is wrong, and green says the desk
    // on screen is current when nothing on it is.
    let t = Theme::truecolor();
    let store = malformed_store(true);
    let now = store.last_snapshot_at.unwrap();
    let owner = row_styles(&store, &Fx::default(), 120, 36, now, 35)
        .into_iter()
        .filter(|(symbol, _)| symbol == "●")
        .map(|(_, style)| style.fg)
        .next_back()
        .expect("the owner chip is the second dot on the line");
    assert_eq!(owner, Some(t.warning));
}

#[test]
fn a_feed_that_keeps_going_away_says_how_often_rather_than_only_that_it_is_back() {
    // A dot has one bit. A feed that has dropped eleven times in a minute and is
    // up right now renders exactly like one that has been up all morning, and
    // those are very different desks to be reading numbers off.
    let mut store = fixture_store();
    let now = store.last_snapshot_at.unwrap();
    assert!(
        !frame_to_string_at(&store, 120, 36, now).contains('↻'),
        "a feed that has never dropped carries no counter"
    );

    for _ in 0..2 {
        store.apply(AppEvent::ConnDown(Channel::Owner), now);
        store.apply(AppEvent::ConnUp(Channel::Owner), now);
    }
    store.apply(AppEvent::ConnUp(Channel::Stream), now);
    store.apply(AppEvent::ConnDown(Channel::Stream), now);

    let frame = frame_to_string_at(&store, 120, 36, now);
    let status = line_with(&frame, "GLASS");
    assert!(status.contains("SSE ↻1"), "{status}");
    assert!(status.contains("OWNER ↻2"), "{status}");
    assert_eq!(
        store.conn.owner_drops, 2,
        "one count per outage, not one per poll that found it gone"
    );
}

// -- toasts ----------------------------------------------------------------

fn toast_queue(now: Instant) -> ToastQueue {
    let mut toasts = ToastQueue::default();
    for (kind, payload) in [
        ("halt", serde_json::json!({"by": "tool"})),
        (
            "approval_created",
            serde_json::json!({"approval_id": "ap-1", "plan_id": "pl-42"}),
        ),
        ("stream.malformed", serde_json::json!({"raw": "{oops"})),
    ] {
        let ev = AppEvent::Sse(SseEvent {
            kind: kind.into(),
            payload,
            ts: None,
            id: None,
        });
        toasts.push(toast::for_event(&ev).expect(kind), now);
    }
    toasts
}

#[test]
fn the_toast_stack_renders_over_the_desk_at_120x36() {
    let store = fixture_store();
    let now = store.last_snapshot_at.unwrap();
    insta::assert_snapshot!(frame_with_toasts(
        &store,
        &toast_queue(now),
        120,
        36,
        now + Duration::from_millis(1_500)
    ));
}

#[test]
fn a_toast_sits_top_right_under_the_tape_and_leaves_when_it_expires() {
    let store = fixture_store();
    let now = store.last_snapshot_at.unwrap();
    let toasts = toast_queue(now);

    let frame = frame_with_toasts(&store, &toasts, 120, 36, now + Duration::from_secs(1));
    // The tape is one of the three indicators claiming this client is alive, so
    // nothing may sit on it.
    assert_eq!(
        frame_to_string_at(&store, 120, 36, now).lines().next(),
        frame.lines().next(),
        "a toast covered the ticker tape"
    );
    let boxed = line_with(&frame, "STREAM FRAME DROPPED");
    assert!(boxed.contains("● STREAM FRAME DROPPED"), "{boxed}");
    assert!(boxed.contains("1s"), "the box says how old it is: {boxed}");
    // The backend's `Display` quotes each row, so the frame's last cell is the
    // character before the closing quote.
    assert!(
        boxed.trim_end().ends_with("│\""),
        "the box is right-aligned against the frame edge: {boxed:?}"
    );
    // Newest first: the dropped frame was pushed last.
    assert!(
        frame
            .lines()
            .position(|l| l.contains("STREAM FRAME DROPPED"))
            < frame.lines().position(|l| l.contains("DESK HALTED")),
        "{frame}"
    );

    // And they go. A box that never leaves is a chip, which is a claim about a
    // condition rather than about a moment.
    let after = frame_with_toasts(&store, &toasts, 120, 36, now + Duration::from_secs(4));
    assert!(!after.contains("DESK HALTED"), "{after}");
    assert_eq!(
        after,
        frame_to_string_at(&store, 120, 36, now + Duration::from_secs(4)),
        "an expired stack has to leave the frame exactly as it found it"
    );
}

#[test]
fn a_terminal_too_small_for_a_box_draws_no_box_rather_than_a_hole() {
    // The allocated-rect rule this workstation holds everywhere: a bordered box
    // in a handful of columns is two rules and no message, which reads as a
    // rendering fault rather than as news.
    let store = fixture_store();
    let now = store.last_snapshot_at.unwrap();
    let toasts = toast_queue(now);
    for (w, h) in [(19u16, 24u16), (120, 3), (1, 1), (40, 4)] {
        let bare = frame_to_string_at(&store, w, h, now);
        let over = frame_with_toasts(&store, &toasts, w, h, now);
        if w < 20 || h < 5 {
            assert_eq!(over, bare, "{w}x{h} drew a box it had no room for");
        } else {
            assert_ne!(over, bare, "{w}x{h} had room and drew nothing");
        }
    }
}

/// A desk whose posterior and whose panel tell the same story.
///
/// Hand-built rather than assembled from the two captured fixtures: those are
/// internally contradictory where they were edited by hand (the snapshot carries
/// an HMM posterior while the panel says `hmmlearn` never ran), so a frame drawn
/// from both would pin a gauge reading that no owner can actually produce. The
/// numbers here are one consistent desk — a mixed panel, an uncertain robust
/// state, three advancers and two decliners.
fn rail_store() -> Store {
    let mut store = Store::default();
    let now = Instant::now();
    store.apply(AppEvent::ConnUp(Channel::Owner), now);
    store.apply(
        AppEvent::Snapshot(Box::new(
            serde_json::from_str::<Snapshot>(
                r#"{"market": {"regime": {"regime": "stress", "robust_state": "uncertain",
                                          "posterior": {"calm": 0.5, "normal": 0.3, "stress": 0.2}},
                               "assets": [
                     {"ticker": "SPY", "price": 729.46, "change_1d": 0.0042},
                     {"ticker": "QQQ", "price": 661.73, "change_1d": 0.011},
                     {"ticker": "XLF", "price": 52.40, "change_1d": 0.0015},
                     {"ticker": "GLD", "price": 201.77, "change_1d": -0.0088},
                     {"ticker": "VNQ", "price": 88.12, "change_1d": -0.0163}]},
                    "stress": {"drawdown_tier": "warning", "gross_exposure": 0.94},
                    "live_portfolio": {"drawdown": -0.031},
                    "atlas": {"mode": "research"}}"#,
            )
            .unwrap(),
        )),
        now,
    );
    store.apply(
        AppEvent::RegimePanel(
            serde_json::from_str(
                r#"{"readings": [
                     {"indicator_id": "turbulence", "state": "stress", "percentile": 0.88},
                     {"indicator_id": "absorption", "state": "calm", "percentile": 0.41},
                     {"indicator_id": "drawdown", "state": "stress", "percentile": 0.93},
                     {"indicator_id": "tail_risk", "state": "calm", "percentile": 0.55},
                     {"indicator_id": "volatility_term_structure", "state": "calm",
                      "percentile": 0.3}],
                    "robust_state": "uncertain"}"#,
            )
            .unwrap(),
        ),
        now,
    );
    store
}

#[test]
fn the_pulse_rail_renders_every_section_at_120x36() {
    insta::assert_snapshot!(frame_to_string(&rail_store(), 120, 36));
}

#[test]
fn the_gauge_needle_eases_toward_a_new_reading_and_lands_exactly_on_it() {
    // The bar is drawn out of full blocks, so the eased ratio is legible in the
    // frame text itself rather than only in a style.
    let store = rail_store();
    let at = store.last_snapshot_at.unwrap();
    let score = pulse::desk_stress_of(&store).expect("this rail has a reading");

    // A tween nobody ever set draws the reading verbatim — which is what every
    // golden in this crate holds, and why none of them can catch a mid-tween
    // frame by accident.
    let settled = frame_to_string_fx(&store, &Fx::default(), 120, 36, at);
    assert!(line_with(&settled, "NEUTRAL").contains("█"), "{settled}");

    let mut fx = Fx::default();
    fx.gauge.set(0.0, at); // a previous reading to set off from
    fx.gauge.set(score, at);
    let leaving = frame_to_string_fx(&store, &fx, 120, 36, at);
    let mid = frame_to_string_fx(&store, &fx, 120, 36, at + Duration::from_millis(80));

    let bar = |frame: &str| line_with(frame, "NEUTRAL").matches('█').count();
    assert_eq!(
        bar(&leaving),
        0,
        "the needle did not set off from where it was"
    );
    assert!(
        bar(&mid) > bar(&leaving) && bar(&mid) < bar(&settled),
        "80 ms in the bar should be part way: {} → {} → {}",
        bar(&leaving),
        bar(&mid),
        bar(&settled)
    );
    assert_eq!(
        frame_to_string_fx(&store, &fx, 120, 36, at + TWEEN),
        settled,
        "the tween has to land on the reading, not near it"
    );
}

#[test]
fn the_number_beside_the_gauge_never_tweens_with_the_bar() {
    // A sliding bar is the desk moving. A printed score counting up through
    // readings that were never taken is the rail inventing measurements — and
    // the band word would flicker across its threshold on the way.
    let store = rail_store();
    let at = store.last_snapshot_at.unwrap();
    let score = pulse::desk_stress_of(&store).unwrap();
    let mut fx = Fx::default();
    fx.gauge.set(0.0, at);
    fx.gauge.set(score, at);
    let mid = frame_to_string_fx(&store, &fx, 120, 36, at + Duration::from_millis(80));
    assert!(
        line_with(&mid, "NEUTRAL").contains(&format!("{score:.0}  NEUTRAL")),
        "{mid}"
    );
}

#[test]
fn drawing_a_frame_publishes_the_rects_the_rules_aim_at() {
    // Invariant 10 at the seam it is easiest to lose: `Fx::rules` aims at these
    // rects, and a shell that computed its layout without publishing it would
    // leave every effect running over a zero rect — visibly nothing, silently
    // fine, and green in every other test in this file.
    let store = rail_store();
    let at = store.last_snapshot_at.unwrap();
    let fx = Fx::default();
    assert_eq!(fx.rects.content.get(), Rect::ZERO, "nothing has drawn yet");

    let _ = frame_to_string_fx(&store, &fx, 120, 36, at);

    assert_eq!(fx.rects.frame.get(), Rect::new(0, 0, 120, 36));
    let content = fx.rects.content.get();
    assert_eq!(
        content.x,
        NAV_W + 1,
        "content starts after the nav rail's rule"
    );
    assert_eq!(content.right(), 120 - PULSE_W, "and ends at the pulse rail");

    // The rail's two targets live inside the rail, and nowhere else.
    for (name, rect) in [
        ("regime", fx.rects.regime.get()),
        ("read", fx.rects.read.get()),
    ] {
        assert_eq!(rect.x, 120 - PULSE_W + 1, "{name} is not in the pulse rail");
        assert_eq!(rect.right(), 120, "{name} does not reach the rail's edge");
        assert!(rect.height > 0, "{name} was published empty");
    }

    // The chips are the right-hand end of the status line, which is the last row.
    let chips = fx.rects.chips.get();
    assert_eq!(chips.bottom(), 36);
    assert_eq!(chips.height, 1);
    assert_eq!(chips.right(), 120);
}

#[test]
fn a_terminal_too_narrow_for_the_chip_run_publishes_what_it_has() {
    // The chip rect is a fixed width carved off the right of the status line;
    // on a terminal narrower than that the subtraction is what would underflow.
    let store = rail_store();
    let at = store.last_snapshot_at.unwrap();
    let fx = Fx::default();
    let _ = frame_to_string_fx(&store, &fx, 24, 12, at);
    let chips = fx.rects.chips.get();
    assert_eq!(chips.x, 0);
    assert_eq!(chips.width, 24);
}

#[test]
fn the_rail_counts_the_breadth_of_the_tape_it_is_looking_at() {
    // The bar and the caption are one fact drawn twice; a bar that disagreed
    // with its own caption would be worse than either alone.
    let store = rail_store();
    let frame = frame_to_string(&store, 120, 36);
    assert!(
        line_with(&frame, "adv ").contains("adv 3 / dec 2"),
        "{frame}"
    );

    // The row above the caption, not "the first line with a block in it" — the
    // gauge is also drawn out of full blocks, three rows higher.
    let rows: Vec<&str> = frame.lines().collect();
    let caption = rows
        .iter()
        .position(|row| row.contains("adv 3 / dec 2"))
        .expect("the caption is what the bar is read against");
    let bar = rows[caption - 1];
    assert_eq!(
        bar.matches('█').count(),
        31,
        "the bar has to fill the rail it was given: {bar}"
    );

    // Three up and two down of 31 cells, in the two semantic colours: a golden
    // string cannot tell one █ from another, and a bar drawn in one colour is
    // not a breadth bar.
    let t = Theme::truecolor();
    let cells = row_styles(
        &store,
        &Fx::default(),
        120,
        36,
        store.last_snapshot_at.unwrap(),
        caption as u16 - 1,
    );
    let painted = |tone| {
        cells
            .iter()
            .filter(|(symbol, style)| symbol == "█" && style.fg == Some(tone))
            .count()
    };
    assert_eq!((painted(t.positive), painted(t.negative)), (19, 12));

    // And it moves with the tape rather than being drawn once: the fixture desk
    // is four-fifths red.
    let frame = frame_to_string(&fixture_store(), 120, 36);
    assert!(
        line_with(&frame, "adv ").contains("adv 1 / dec 4"),
        "{frame}"
    );
}

#[test]
fn the_rail_names_the_best_and_the_worst_of_the_universe() {
    let store = rail_store();
    let frame = frame_to_string(&store, 120, 36);
    let best = line_with(&frame, "best");
    assert!(best.contains("QQQ") && best.contains("+1.10%"), "{best}");
    let worst = line_with(&frame, "worst");
    assert!(worst.contains("VNQ") && worst.contains("-1.63%"), "{worst}");

    // The arrow carries the direction in a colour *and* a glyph, so the row is
    // still readable where the two greens are one shade apart.
    let buf = Client::new(store).buffer(120, 36);
    assert_eq!(
        body_style_of(&buf, "▲ best").fg,
        Some(Theme::truecolor().positive)
    );
    assert_eq!(
        body_style_of(&buf, "▼ worst").fg,
        Some(Theme::truecolor().negative)
    );
}

#[test]
fn the_gauge_reads_the_panel_and_says_pending_until_one_arrives() {
    // The first reader `store.regime_panel` has ever had. Before this the poller
    // fetched the panel every 30 s into a field nothing on screen consumed.
    let waiting = frame_to_string(&fixture_store(), 120, 36);
    assert!(
        line_with(&waiting, "desk stress").contains('…'),
        "a desk with no panel must say not-yet, not 50 NEUTRAL:\n{waiting}"
    );

    let frame = frame_to_string(&rail_store(), 120, 36);
    // 50 + 50·(0.50 − 0.20) − 10 for the stressed drawdown reading. The band is
    // the pin; the arithmetic itself is pinned in the widget's own tests.
    assert!(line_with(&frame, "NEUTRAL").contains("55"), "{frame}");
}

#[test]
fn a_panel_whose_detectors_all_failed_reads_missing_rather_than_neutral() {
    // "Not yet" and "nothing measurable" are different facts about the desk, and
    // both are different from a confident NEUTRAL. The owner keeps a row per
    // detector and marks it `failed`, so this is the frame an operator sees when
    // the panel ran against a snapshot it could not use.
    // No posterior either — the owner ships without `hmmlearn` more often than
    // with it, and that is the desk where the panel is the only input there was.
    let mut store = store_from(r#"{"market": {"regime": {"regime": "calm"}}}"#);
    let now = store.last_snapshot_at.unwrap();
    store.apply(
        AppEvent::RegimePanel(
            serde_json::from_str(
                r#"{"readings": [
                     {"indicator_id": "turbulence", "state": "failed"},
                     {"indicator_id": "drawdown", "state": "failed"}],
                    "robust_state": "uncertain"}"#,
            )
            .unwrap(),
        ),
        now,
    );
    let frame = frame_to_string(&store, 120, 36);
    let stress = line_with(&frame, "desk stress");
    assert!(stress.contains("--"), "{stress}");
    assert!(
        !stress.contains('…'),
        "the panel arrived; this is not a wait: {stress}"
    );
}

#[test]
fn the_regime_strip_gives_every_reading_a_row_including_one_that_did_not_run() {
    // The panel's own rule: a detector that failed still occupies a row, so the
    // strip shows what did not run rather than quietly shortening.
    let frame = frame_to_string(&fixture_store_with_panel(), 120, 36);
    // By the bar rather than by the name: `drawdown` is also a desk fact in the
    // section above, and a pin that matched the first line saying the word would
    // pass on the wrong row.
    let rows: Vec<&str> = frame
        .lines()
        .filter(|line| line.contains('▰') || line.contains('▱'))
        .collect();
    assert_eq!(rows.len(), 5, "one row per reading that ran:\n{frame}");
    // In the panel's own order, which is the owner's.
    for (row, indicator) in rows.iter().zip([
        "absorption",
        "drawdown",
        "tail risk",
        "turbulence",
        "vol term",
    ]) {
        assert!(row.contains(indicator), "{indicator} is not on {row}");
    }
    let failed = line_with(&frame, "hmm");
    assert!(
        failed.contains("--") && !failed.contains('▰'),
        "a detector that did not run has no percentile to draw: {failed}"
    );
}

#[test]
fn a_rail_too_short_for_the_next_section_says_how_many_are_below() {
    // The sub-floor rule this workstation holds everywhere: a section that
    // cannot render whole refuses out loud. A clipped breadth bar and a working
    // one are indistinguishable, and this rail is read at a glance.
    let frame = frame_to_string(&rail_store(), 120, 24);
    assert!(frame.contains("▾ 3 more below"), "{frame}");
    assert!(
        !frame.contains("▌ BREADTH") && !frame.contains("adv "),
        "half a section is worse than none of it:\n{frame}"
    );
    // What did fit is still whole.
    assert!(
        frame.contains("▌ PULSE") && frame.contains("NEUTRAL"),
        "{frame}"
    );
}

#[test]
fn a_narrow_terminal_still_renders_without_panicking() {
    // Ported from `ui.rs`. Ratatui panics on some zero-area splits, and a
    // client that dies when the window is dragged narrow is worse than one
    // that truncates.
    //
    // Both stores, because the rail's sections are only *built* once the panel
    // has arrived: a degenerate rect that the empty rail never reaches is a rect
    // the populated one meets on the first 30-second poll.
    for store in [fixture_store(), rail_store()] {
        for (w, h) in [
            (40u16, 12u16),
            (20, 8),
            (80, 10),
            (200, 60),
            (1, 1),
            (34, 3),
            (120, 15),
            (120, 21),
        ] {
            let _ = frame_to_string(&store, w, h);
        }
    }
}
