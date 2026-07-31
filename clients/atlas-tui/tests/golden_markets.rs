//! MARKETS, pinned as a whole frame plus the facts a golden cannot state.
//!
//! Every keystroke here goes through the real `shell::on_key` and every frame
//! through the real `shell::draw`, so these also stand as the regression tests
//! for the view registry: a client that rebuilt its views per keystroke would
//! render the same first frame and lose every one of the cursor assertions.
//!
//! Content assertions go through `body`, never the whole frame. The ticker tape
//! repeats every symbol, price and arrow in the universe along the top row, so
//! `frame.contains("XLF")` passes on the tape alone and says nothing about the
//! grid under it — a test that read the tape by accident would survive deleting
//! this entire view.

mod harness;

use atlas::bus::{AppEvent, Channel, SseEvent};
use atlas::fx::{FlashKey, FlashTracker};
use atlas::model::Snapshot;
use atlas::store::Store;
use atlas::theme::Theme;
use crossterm::event::KeyCode;
use harness::{body, body_style_of, line_with, Client};
use std::time::{Duration, Instant};

/// The fixture desk, already switched to MARKETS.
fn markets() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('2'));
    client
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

/// One `quote` frame as the owner writes it.
fn quote(ticker: &str, price: f64, change: f64) -> AppEvent {
    AppEvent::Sse(SseEvent {
        kind: "quote".into(),
        payload: serde_json::json!({"rows": [
            {"ticker": ticker, "price": price, "change_1d": change}
        ]}),
        ts: None,
        id: None,
    })
}

#[test]
fn the_markets_view_renders_the_grid_at_120x36() {
    insta::assert_snapshot!(markets().frame(120, 36));
}

#[test]
fn the_grid_carries_the_change_arrows_rather_than_a_bare_minus() {
    // The sign is the glyph, per `format`. A column of `-1.34` reads as a
    // subtraction on a surface where every other number is a level.
    // Rows are found by their own price, because the SYMBOL column's text is
    // also the hero header's and the sector strip's.
    let body = body(&markets().frame(120, 36));
    assert!(line_with(&body, "52.40").contains('▲'), "XLF rose:\n{body}");
    assert!(line_with(&body, "661.73").contains('▼'), "QQQ fell:\n{body}");
}

#[test]
fn the_last_column_is_the_amber_one() {
    // Amber is the workstation's only theme-defining colour and it is spent on
    // the number an operator actually trades against. A golden string cannot
    // say this, so a re-blessed golden cannot quietly take it away.
    let client = markets();
    let buf = client.buffer(120, 36);
    let t = Theme::truecolor();
    assert_eq!(body_style_of(&buf, "729.46").fg, Some(t.accent));
    // And the symbol beside it is not — two columns in one colour is one column.
    assert_eq!(body_style_of(&buf, "SPY").fg, Some(t.cyan));
}

#[test]
fn the_sector_strip_names_the_spdrs_the_snapshot_actually_carried() {
    // Found by the signed percent, which only the strip renders: the grid's own
    // XLK row spells the same move as `▼ 2.08` under a `CHG%` header.
    let body = body(&markets().frame(120, 36));
    let strip = line_with(&body, "-2.08%");
    assert!(strip.contains("XLK"), "{body}");
    assert!(
        strip.contains("XLF"),
        "both present SPDRs share the row: {strip}"
    );
    // The label is the move, not just the symbol: a heat cell whose number is
    // off screen is a colour with nothing to check it against.
    assert!(strip.contains("+1.24%"), "{strip}");
    // A sector the payload does not carry is absent, never a zero cell.
    assert!(!body.contains("XLE"), "{body}");
}

#[test]
fn the_heat_cells_are_lit_by_how_far_the_sector_moved() {
    // XLK fell 2.08% and XLF rose 1.24% — different steps of the ramp *and*
    // different sides of it, so neither the magnitude nor the direction can be
    // dropped without this failing.
    let client = markets();
    let buf = client.buffer(120, 36);
    let t = Theme::truecolor();
    assert_eq!(
        body_style_of(&buf, "XLK  -2.08%").bg,
        Some(t.negative_dim),
        "2.08% is step 5"
    );
    assert_eq!(
        body_style_of(&buf, "XLF  +1.24%").bg,
        Some(t.bg_hover),
        "1.24% is step 3"
    );
}

#[test]
fn a_desk_with_no_sector_etfs_says_what_to_prewarm_instead_of_nothing() {
    // Fail loud. An empty strip reads as "every sector is flat", which is a
    // statement about the market that nobody made.
    let mut client = Client::new(store_from(
        r#"{"market": {"assets": [
             {"ticker": "SPY", "price": 729.46, "change_1d": -0.015,
              "history": [750.72, 743.29, 742.09, 729.46]}
           ]}}"#,
    ));
    client.press(KeyCode::Char('2'));
    let frame = client.frame(120, 36);
    assert!(
        frame.contains("sector map needs the extended universe"),
        "{frame}"
    );
    assert!(
        line_with(&frame, "sector map needs").contains("qlab prewarm --universe candidates"),
        "the remedy has to be nameable, not implied:\n{frame}"
    );
}

#[test]
fn the_hero_follows_the_selected_row() {
    let mut client = markets();
    // The grid opens on the first asset the snapshot carried; nothing about the
    // universe's order is this view's to reinterpret.
    assert!(client.frame(120, 36).contains("▌ ACWI"));

    client.press(KeyCode::Down);
    assert!(client.frame(120, 36).contains("▌ SPY"));
    client.press(KeyCode::Down);
    assert!(client.frame(120, 36).contains("▌ QQQ"));

    // And both ends are walls, not wraps: an operator holding an arrow must land
    // on the first or last row, never at the other end of a universe they did
    // not scroll to.
    client.keys(&[KeyCode::Up; 6]);
    assert!(client.frame(120, 36).contains("▌ ACWI"));
    client.keys(&[KeyCode::Down; 20]);
    assert!(client.frame(120, 36).contains("▌ XLF"));
}

#[test]
fn the_selection_survives_a_trip_through_another_view() {
    // The registry regression, end to end. Views were rebuilt per keystroke and
    // per frame, so this assertion is the one a fresh `Box<dyn View>` fails.
    let mut client = markets();
    client.keys(&[KeyCode::Down, KeyCode::Down]);
    assert!(client.frame(120, 36).contains("▌ QQQ"));

    client.press(KeyCode::Char('3'));
    assert!(client.frame(120, 36).contains("▌ BOOK"), "left MARKETS");
    client.press(KeyCode::Char('2'));
    assert!(
        client.frame(120, 36).contains("▌ QQQ"),
        "the selection was rebuilt away:\n{}",
        client.frame(120, 36)
    );
}

#[test]
fn two_right_presses_park_a_crosshair_on_the_second_bar() {
    // The keyboard translation of the reference desk's mouse crosshair. The
    // chip is indexed rather than dated because the owner's `history` is a bare
    // price array — inventing a date beside a price would be worse than an index.
    let mut client = markets();
    client.press(KeyCode::Down); // SPY
    assert!(
        !client.frame(120, 36).contains("$743.29"),
        "a crosshair nobody asked for"
    );

    client.keys(&[KeyCode::Right, KeyCode::Right]);
    let frame = client.frame(120, 36);
    assert!(
        frame.contains("1 $743.29"),
        "no crosshair chip after two → presses:\n{frame}"
    );

    // The rule under the chip is the other half: a chip with no rule states a
    // value without saying where on the series it was read.
    let buf = client.buffer(120, 36);
    let bright = Theme::truecolor().border_bright;
    let rule = (0..buf.area.width)
        .flat_map(|x| (0..buf.area.height).map(move |y| (x, y)))
        .any(|(x, y)| {
            let cell = &buf[(x, y)];
            cell.style().fg == Some(bright)
                && cell
                    .symbol()
                    .chars()
                    .next()
                    .is_some_and(|c| ('\u{2800}'..='\u{28ff}').contains(&c))
        });
    assert!(rule, "the crosshair drew no vertical rule");
}

#[test]
fn the_crosshair_stops_at_the_ends_of_the_series() {
    let mut client = markets();
    client.press(KeyCode::Down); // SPY: a ten-point history
    client.keys(&[KeyCode::Right; 40]);
    assert!(
        client.frame(120, 36).contains("9 $729.46"),
        "→ ran off the end of the series:\n{}",
        client.frame(120, 36)
    );
    client.keys(&[KeyCode::Left; 40]);
    assert!(
        client.frame(120, 36).contains("0 $750.72"),
        "← ran off the start:\n{}",
        client.frame(120, 36)
    );
}

#[test]
fn moving_the_selection_drops_a_crosshair_that_belonged_to_another_asset() {
    // Index 1 of SPY's history and index 1 of QQQ's are different points on
    // different series. Carrying the index across would keep the chip on screen
    // and silently change what it means.
    let mut client = markets();
    client.press(KeyCode::Down);
    client.keys(&[KeyCode::Right, KeyCode::Right]);
    assert!(client.frame(120, 36).contains("$743.29"));

    client.press(KeyCode::Down);
    let frame = client.frame(120, 36);
    assert!(frame.contains("▌ QQQ"), "{frame}");
    // `$` reaches this frame only through the chip: the gutter renders bare
    // quotes and the rails render percentages.
    assert!(
        !frame.contains('$'),
        "the crosshair followed the selection onto another series:\n{frame}"
    );
}

#[test]
fn a_quote_tick_flashes_the_change_cell_and_not_the_whole_row() {
    // The grid's flash is on CHG%, the tape's is on the price. One quote lights
    // both, on their own keys, so neither decays because of where the other is.
    let mut client = markets();
    let at = client.now;
    client.store.apply(quote("SPY", 731.11, 0.0042), at);
    client.fx.flash(FlashKey::change("SPY"), at);

    let buf = client.buffer(120, 36);
    let t = Theme::truecolor();
    // `arrow_chg` renders the magnitude in percent units under a `CHG%` header.
    assert_eq!(body_style_of(&buf, "▲ 0.42").bg, Some(t.accent_dim));
    assert_ne!(
        body_style_of(&buf, "731.11").bg,
        Some(t.accent_dim),
        "a flash keyed on the row would light the price cell too"
    );

    // And it ends. A highlight that never goes out stops meaning "this moved".
    client.fx = FlashTracker::default();
    client.now = at + Duration::from_millis(700);
    let cooled = client.buffer(120, 36);
    assert_ne!(body_style_of(&cooled, "▲ 0.42").bg, Some(t.accent_dim));
}

#[test]
fn a_quote_reaches_the_grid_without_a_new_snapshot() {
    // The overlay's whole point, on this surface: a price that moved on the
    // stream is in the grid before the next three-second poll. A row that read
    // `market.assets` directly would still be showing the poll's number.
    let mut client = markets();
    let at = client.now + Duration::from_millis(900);
    client.now = at;
    client.store.apply(quote("SPY", 731.11, 0.0042), at);
    let body = body(&client.frame(120, 36));
    assert!(body.contains("731.11"), "{body}");
    assert!(!body.contains("729.46"), "the poll's price survived:\n{body}");
}

#[test]
fn a_narrow_terminal_still_renders_the_grid_without_panicking() {
    let mut client = markets();
    client.keys(&[KeyCode::Down, KeyCode::Right, KeyCode::Right]);
    for (w, h) in [(40u16, 12u16), (20, 8), (80, 10), (200, 60), (1, 1), (34, 3)] {
        let _ = client.frame(w, h);
    }
}

#[test]
fn an_asset_the_book_does_not_hold_shows_no_weight_rather_than_zero() {
    // The fixture's book holds ACWI and not SPY. A `0.0%` target would read as
    // a deliberate exclusion, which is a decision nobody made.
    let body = body(&markets().frame(120, 36));
    let acwi = line_with(&body, "152.47");
    assert!(acwi.contains("6.3%"), "the held weight is missing: {acwi}");
    let spy = line_with(&body, "729.46");
    assert!(spy.contains("--"), "an unheld asset must read --: {spy}");
    assert!(!spy.contains("0.0%"), "{spy}");
}

#[test]
fn an_empty_universe_says_so_rather_than_drawing_an_empty_grid() {
    let mut client = Client::new(store_from(r#"{"atlas": {"mode": "research"}}"#));
    client.press(KeyCode::Char('2'));
    let frame = client.frame(120, 36);
    assert!(frame.contains("no market assets"), "{frame}");
}
