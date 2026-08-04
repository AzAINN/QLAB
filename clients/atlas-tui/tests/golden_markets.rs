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
//! this entire view. Where the pulse rail draws the same *kind* of thing this
//! view does — a ticker, an arrow, a signed percent — the pin narrows further to
//! `content`, which is the columns the view itself owns.

mod harness;

use atlas::bus::{AppEvent, Channel, SseEvent};
use atlas::fx::{FlashKey, Fx};
use atlas::model::Snapshot;
use atlas::store::Store;
use atlas::theme::Theme;
use crossterm::event::KeyCode;
use harness::{body, body_style_of, content, line_with, Client};
use std::time::{Duration, Instant};

/// The fixture desk, already switched to MARKETS.
fn markets() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('3'));
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
    harness::no_door(&mut store);
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
    assert!(
        line_with(&body, "661.73").contains('▼'),
        "QQQ fell:\n{body}"
    );
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
    // Found by the signed percent, which only the strip renders *in this view*:
    // the grid's own XLK row spells the same move as `▼ 2.08` under a `CHG%`
    // header. Read through `content`, since the pulse rail names the same worst
    // mover in the same units one column to the right.
    let body = content(&markets().frame(120, 36));
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
    client.press(KeyCode::Char('3'));
    let frame = client.frame(120, 36);
    assert!(
        frame.contains("sector map needs the extended universe"),
        "{frame}"
    );
    assert!(
        line_with(&frame, "sector map needs").contains("qlab prewarm --universe candidates"),
        "the remedy has to be nameable, not implied:\n{frame}"
    );

    // And it survives a narrow desk, because that is where it is most likely to
    // be read: unwrapped, this line is cut to `qlab prewar` at 95 columns — a
    // command an operator cannot run and might not notice is incomplete.
    let narrow = client.frame(95, 26);
    assert!(
        narrow.contains("candidates"),
        "the remedy was clipped rather than wrapped:\n{narrow}"
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

    client.press(KeyCode::Char('4'));
    // BOOK's own headline, which only that view renders: until Task 11 this
    // read `▌ BOOK` off the placeholder that used to stand there.
    assert!(
        client.frame(120, 36).contains("PORTFOLIO VALUE"),
        "left MARKETS"
    );
    client.press(KeyCode::Char('3'));
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
    client.fx.flashes.flash(FlashKey::change("SPY"), at);

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
    client.fx = Fx::default();
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
    assert!(
        !body.contains("729.46"),
        "the poll's price survived:\n{body}"
    );
}

#[test]
fn a_terminal_below_the_grids_floor_refuses_rather_than_drawing_wrong_numbers() {
    // The guard `head` cannot be: it is spent against each column's *declared*
    // width, and below the floor `Constraint::Min` yields and ratatui shrinks
    // the allocation underneath it. A right-aligned cell then loses its leading
    // characters — the arrow, then the sign, then the leading digit — so at 90
    // columns a -10.1% fall would draw as a gain. The pane says so instead.
    let mut client = markets();
    client.keys(&[KeyCode::Down, KeyCode::Right, KeyCode::Right]);
    // The columns this view owns: the arrows the rail's movers carry are not
    // grid cells that survived, and a pin that could not tell them apart would
    // fail on the rail rather than on the regression.
    let body = content(&client.frame(90, 30));
    // Read off the body rather than one line: the refusal is longer than the
    // pane that could not hold the grid, so it wraps — which is the whole
    // reason it is a wrapped `Paragraph` and not a `Line`.
    assert!(body.contains("markets grid needs 56 columns"), "{body}");
    assert!(
        body.contains("widen the terminal"),
        "the remedy has to be nameable, not implied:\n{body}"
    );
    // And not one digit-bearing row of the grid survives beside the refusal: a
    // half-drawn grid is exactly the reading the refusal exists to prevent.
    // The grid's arrow is `▼ ` with the cell's own space — the breadth strip
    // above the refusal still draws its `▲1▼4` and movers, which is the point:
    // the strip survives a grid that cannot.
    assert!(
        !body.contains("▼ ") && !body.contains("▲ "),
        "a CHG% cell survived below the floor:\n{body}"
    );
    for number in ["152.47", "729.46", "661.73", "-10.1", "6.3%"] {
        assert!(
            !body.contains(number),
            "{number} survived below the floor:\n{body}"
        );
    }
    // The crosshair chip goes with the chart it labelled.
    assert!(!body.contains('$'), "{body}");
}

#[test]
fn the_floor_is_the_grids_allocation_and_not_the_pane_it_was_split_from() {
    // One cell, and it is the whole finding: the pane is not the grid — the cell
    // of spacing that keeps the hero's gutter off `TGT` sits between them — so a
    // guard on the pane admits a grid one short of its own floor. At 99 columns
    // the pane is exactly `GRID_W` and the grid is `GRID_W - 1`, which drops the
    // leading bar off an eight-value spark whose colour is still computed from
    // all eight. A cell that draws seven bars and colours nine is the same
    // silently-wrong-number class the refusal exists for, one column narrower.
    let client = markets();

    // 100: the grid gets its floor exactly, and the spark is all eight bars.
    let admitted = body(&client.frame(100, 30));
    assert!(
        line_with(&admitted, "152.47").contains("▅██▄▄▅▅▁"),
        "the spark lost a bar off its head at the boundary:\n{admitted}"
    );

    // 99: one cell less, and the pane says so instead of drawing seven bars.
    let refused = body(&client.frame(99, 30));
    assert!(
        refused.contains("markets grid needs 56 columns"),
        "a pane at exactly GRID_W admitted a grid one cell short:\n{refused}"
    );
    assert!(
        !refused.contains("152.47") && !refused.contains("██▄▄▅▅▁"),
        "a truncated grid row survived one cell under the floor:\n{refused}"
    );
}

#[test]
fn a_narrow_terminal_still_renders_the_grid_without_panicking() {
    let mut client = markets();
    client.keys(&[KeyCode::Down, KeyCode::Right, KeyCode::Right]);
    for (w, h) in [
        (40u16, 12u16),
        (20, 8),
        (80, 10),
        (200, 60),
        (1, 1),
        (34, 3),
    ] {
        let _ = client.frame(w, h);
    }
}

#[test]
fn a_strip_too_narrow_for_its_sectors_says_so_rather_than_clipping_one_away() {
    // The same sub-floor class as the grid, one pane down. A `Paragraph` taller
    // than its area is clipped without complaint, and a sector missing from the
    // map reads as a sector that did not move.
    let mut client = Client::new(store_from(
        r#"{"market": {"assets": [
             {"ticker": "XLK",  "price": 285.24, "change_1d": -0.0208},
             {"ticker": "XLV",  "price": 140.11, "change_1d": 0.0031},
             {"ticker": "XLF",  "price": 52.40,  "change_1d": 0.0124},
             {"ticker": "XLE",  "price": 91.02,  "change_1d": -0.0077},
             {"ticker": "XLY",  "price": 220.30, "change_1d": -0.0154},
             {"ticker": "XLI",  "price": 155.68, "change_1d": 0.0042},
             {"ticker": "XLB",  "price": 88.19,  "change_1d": -0.0033},
             {"ticker": "XLU",  "price": 84.55,  "change_1d": 0.0066},
             {"ticker": "XLRE", "price": 41.27,  "change_1d": -0.0044},
             {"ticker": "XLC",  "price": 118.90, "change_1d": -0.0189},
             {"ticker": "XLP",  "price": 79.31,  "change_1d": 0.0017},
             {"ticker": "SOXX", "price": 312.66, "change_1d": -0.0295}
           ]}}"#,
    ));
    client.press(KeyCode::Char('3'));

    // Every cell rendered, and only the strip renders a signed percent — the
    // grid spells the same move as `▼ 2.08` under a `CHG%` header.
    let labels = [
        "XLK  -2.08%",
        "XLV  +0.31%",
        "XLF  +1.24%",
        "XLE  -0.77%",
        "XLY  -1.54%",
        "XLI  +0.42%",
        "XLB  -0.33%",
        "XLU  +0.66%",
        "XLRE -0.44%",
        "XLC  -1.89%",
        "XLP  +0.17%",
        "SOXX -2.95%",
    ];

    // Twelve cells over two rows is six to a row, and six cells is 72 columns.
    // This strip has 66 — wide enough for the grid above it, so the refusal is
    // the strip's own rather than a consequence of the grid's.
    let narrow = body(&client.frame(109, 30));
    assert!(narrow.contains("sector map needs 72 columns"), "{narrow}");
    assert!(narrow.contains("widen the terminal"), "{narrow}");
    assert!(
        narrow.contains("SYMBOL"),
        "the grid still fits at 109:\n{narrow}"
    );
    for label in labels {
        assert!(
            !narrow.contains(label),
            "{label} was drawn into a strip that cannot hold the map:\n{narrow}"
        );
    }

    // Wide enough, and all twelve are on the map rather than the ten that fit.
    let wide = body(&client.frame(160, 30));
    for label in labels {
        assert!(
            wide.contains(label),
            "{label} is missing from the map:\n{wide}"
        );
    }
}

#[test]
fn a_number_wider_than_its_column_keeps_its_sign_and_loses_its_tail() {
    // The clamp at its call site, not in isolation: `head` alone can pass every
    // unit test while nothing calls it. Deleting it from `cell` has to fail
    // *here*. A -12.5% short weight is six characters in the five-wide `WT`
    // column, and ratatui right-aligns an overlong line by dropping its leading
    // cells — so the unclamped rendering is `12.5%`, a short drawn as a long.
    let mut client = Client::new(store_from(
        r#"{"market": {"assets": [
             {"ticker": "SPY", "price": 729.46, "change_1d": -0.015,
              "history": [750.72, 729.46]}
           ]},
           "portfolio": {"weights": {"SPY": -0.125},
                         "target_weights": {"SPY": -0.125}}}"#,
    ));
    client.press(KeyCode::Char('3'));
    let body = body(&client.frame(120, 36));
    let row = line_with(&body, "729.46");
    assert!(
        row.contains("-12.5"),
        "the clamp is gone and the sign went with it: {row}"
    );
    assert!(
        !row.contains("12.5%"),
        "an overlong cell kept its tail and lost its head: {row}"
    );
}

#[test]
fn an_asset_the_book_does_not_hold_shows_no_weight_rather_than_zero() {
    // The fixture's book holds ACWI and not SPY. A `0.0%` target would read as
    // a deliberate exclusion, which is a decision nobody made.
    // Through `content`, not `body`: the pulse rail's `gross 100.0%` chip can
    // share a row with the grid, and a `0.0%` found there is not this view's.
    let body = content(&markets().frame(120, 36));
    let acwi = line_with(&body, "152.47");
    assert!(acwi.contains("6.3%"), "the held weight is missing: {acwi}");
    let spy = line_with(&body, "729.46");
    assert!(spy.contains("--"), "an unheld asset must read --: {spy}");
    assert!(!spy.contains("0.0%"), "{spy}");
}

#[test]
fn an_empty_universe_says_so_rather_than_drawing_an_empty_grid() {
    let mut client = Client::new(store_from(r#"{"atlas": {"mode": "research"}}"#));
    client.press(KeyCode::Char('3'));
    let frame = client.frame(120, 36);
    assert!(frame.contains("no market assets"), "{frame}");
}
