//! BOOK's stats ribbon, pinned as a whole frame plus the facts a golden cannot state.
//!
//! The ribbon is the desk's single account of its own headline numbers, so most
//! of these tests are about what it must *not* do: invent a zero for a number
//! the owner never sent, state the equity twice, keep drawing when its cells are
//! too narrow for the digits in them, or stay silent about a halted book.
//!
//! Content assertions go through `content`, the columns this view owns. The
//! ticker tape along the top and the pulse rail down the right render prices,
//! percentages and `--` of their own, so a pin on the whole frame could pass on
//! chrome and say nothing about the ribbon.

mod harness;

use atlas::bus::{AppEvent, Channel, SseEvent};
use atlas::fx::FlashKey;
use atlas::model::Snapshot;
use atlas::store::Store;
use atlas::theme::Theme;
use crossterm::event::KeyCode;
use harness::{body_style_of, content, line_with, Client};
use ratatui::buffer::Buffer;
use ratatui::style::{Modifier, Style};
use std::time::{Duration, Instant};

/// The fixture desk, already switched to BOOK.
fn book() -> Client {
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
    store
}

fn book_from(json: &str) -> Client {
    let mut client = Client::new(store_from(json));
    client.press(KeyCode::Char('3'));
    client
}

#[test]
fn the_book_view_renders_the_ribbon_at_120x36() {
    insta::assert_snapshot!(book().frame(120, 36));
}

#[test]
fn the_ribbon_states_the_book_the_owner_marked_to_the_tape() {
    // Every headline number in one place, read off the live book rather than
    // the registry's: the two are different views of the same desk and the
    // ribbon must never show them under one label.
    let content = content(&book().frame(120, 36));
    assert!(
        content.contains("$10,000.00"),
        "the equity is missing:\n{content}"
    );
    assert!(
        line_with(&content, "$10,000.00").contains("+$0.00"),
        "the P&L hero sits beside the equity:\n{content}"
    );
    assert!(content.contains("2 pos"), "the position count:\n{content}");
    assert!(content.contains("cash $0.00"), "the cash split:\n{content}");
    // The window's own change, and what the window is made of.
    assert!(content.contains("5 marks"), "{content}");
}

#[test]
fn the_ribbon_reads_the_live_book_and_not_the_brokers() {
    // The registry-booked view and the mark-to-market view disagree by
    // construction — that disagreement is a reconciliation finding, not a
    // display choice — so a ribbon that read whichever section was present
    // would silently change what its hero number means.
    let content = content(
        &book_from(
            r#"{"portfolio": {"equity": 12345.0, "cash": 500.0},
                "live_portfolio": {"equity": 9999.0, "cash": 1.0, "positions": []}}"#,
        )
        .frame(120, 36),
    );
    assert!(content.contains("$9,999.00"), "{content}");
    assert!(
        !content.contains("$12,345.00"),
        "the broker's equity won:\n{content}"
    );
}

#[test]
fn the_ribbon_states_the_equity_exactly_once() {
    // The anti-duplication rule the ribbon exists for: a second panel repeating
    // the headline is a second account of it, and two accounts of one number is
    // how a desk ends up trusting neither.
    let content = content(&book().frame(120, 36));
    assert_eq!(
        content.matches("$10,000.00").count(),
        1,
        "the equity is stated more than once:\n{content}"
    );
}

#[test]
fn the_risk_chips_carry_the_metrics_the_owner_computed() {
    let content = content(&book().frame(120, 36));
    for (label, value) in [
        ("SHARPE", "0.27"),
        ("VOL", "15.3%"),
        ("MDD", "-2.5%"),
        ("GROSS", "100.0%"),
        ("NET", "100.0%"),
        ("CVAR95", "-2.3%"),
    ] {
        let row = line_with(&content, label);
        assert!(
            row.contains(value),
            "{label} does not carry {value}: {row}\n{content}"
        );
    }
}

#[test]
fn the_gainers_and_losers_count_the_positions_that_actually_moved() {
    // Flat is neither, exactly as the rail's breadth counts a tape: a position
    // that has not moved is not a gainer, and counting it as one would tilt
    // every quiet book green.
    let content = content(
        &book_from(
            r#"{"live_portfolio": {"equity": 1000.0, "unrealized_pnl": 40.0, "positions": [
                 {"ticker": "SPY", "qty": 2.0, "avg_price": 100.0, "unrealized_pnl": 50.0},
                 {"ticker": "QQQ", "qty": 1.0, "avg_price": 100.0, "unrealized_pnl": -10.0},
                 {"ticker": "GLD", "qty": 1.0, "avg_price": 100.0, "unrealized_pnl": 0.0}]}}"#,
        )
        .frame(120, 36),
    );
    assert!(content.contains("▲1 ▼1"), "{content}");
    assert!(content.contains("3 pos"), "{content}");
    // +$40 on a $400 cost basis, which is the positions' own arithmetic and not
    // a share of the equity.
    assert!(content.contains("+10.00%"), "{content}");
}

#[test]
fn a_halted_book_says_so_under_its_own_equity() {
    // A halted book renders exactly like a live one otherwise — same equity,
    // same P&L — so without the word the most consequential fact about the desk
    // is the one thing on screen that nothing states.
    let client = book_from(
        r#"{"live_portfolio": {"equity": 9000.0, "cash": 10.0, "halted": true,
                               "positions": [{"ticker": "SPY", "unrealized_pnl": 1.0}]}}"#,
    );
    let halted = content(&client.frame(120, 36));
    assert!(halted.contains("HALTED"), "{halted}");
    let style = body_style_of(&client.buffer(120, 36), "HALTED");
    assert_eq!(style.fg, Some(Theme::truecolor().negative));
    assert!(style.add_modifier.contains(Modifier::BOLD), "{style:?}");

    // And a book that is not halted says nothing of the kind.
    assert!(!content(&book().frame(120, 36)).contains("HALTED"));

    // The halt rule is the store's, not this cell's: the reconciled book
    // answers when the live one has not been marked, which is the same rule the
    // glyph in the rail animates. Re-derived here, the two surfaces could
    // disagree about whether the desk is trading.
    let reconciled = content(
        &book_from(r#"{"portfolio": {"halted": true}, "live_portfolio": {"equity": 10.0}}"#)
            .frame(120, 36),
    );
    assert!(reconciled.contains("HALTED"), "{reconciled}");
}

#[test]
fn a_blocked_live_book_names_the_state_rather_than_a_ribbon_of_dashes() {
    // The owner's blocked report carries no equity, no cash and no positions —
    // by design, since valuing a book on prices it could not fetch is the thing
    // it refuses to do. Every cell is honestly `--`; the word is what separates
    // "the desk has no numbers" from "this client lost them".
    let content = content(&book_from(r#"{"live_portfolio": {"blocked": true}}"#).frame(120, 36));
    assert!(content.contains("BLOCKED"), "{content}");
    assert!(
        !content.contains('$'),
        "a blocked report has no money in it:\n{content}"
    );
}

#[test]
fn a_desk_the_owner_sent_no_numbers_for_reads_missing_rather_than_zero() {
    // Absent is not zero. A ribbon of `$0.00` and `0.0%` is a statement about
    // the book — flat, unlevered, no drawdown — that nobody made.
    let content = content(&book_from(r#"{"atlas": {"mode": "research"}}"#).frame(120, 36));
    assert!(content.contains("PORTFOLIO VALUE"), "{content}");
    assert!(content.contains("-- pos · cash --"), "{content}");
    assert!(content.contains("-- marks"), "{content}");
    assert!(
        !content.contains('$'),
        "a number nobody computed was rendered as money:\n{content}"
    );
    assert!(
        !content.contains('%'),
        "a number nobody computed was rendered as a percent:\n{content}"
    );
    for label in ["SHARPE", "VOL", "MDD", "GROSS", "NET", "CVAR95"] {
        assert!(
            line_with(&content, label).contains("--"),
            "{label} did not read missing:\n{content}"
        );
    }
}

#[test]
fn a_pane_below_the_ribbons_floor_refuses_rather_than_clipping_a_number() {
    // The same class as the markets grid: below the floor the cells shrink
    // underneath the numbers held to them and a `Paragraph` drops what does not
    // fit, so `$10,000.0` and `100.0` are the renderings on offer — a book
    // stated wrong rather than not stated.
    let content = content(&book().frame(100, 30));
    assert!(
        content.contains("book ribbon needs 74 columns"),
        "{content}"
    );
    assert!(
        content.contains("widen the terminal"),
        "the remedy has to be nameable:\n{content}"
    );
    for number in ["$10,000.00", "100.0%", "0.27", "2 pos"] {
        assert!(
            !content.contains(number),
            "{number} survived below the floor:\n{content}"
        );
    }
}

#[test]
fn the_floor_is_the_cells_own_allocations_and_not_the_pane_they_came_from() {
    // Bracketed, because a floor that is only ever tested from far below is a
    // floor nobody knows the height of. The pane is not the cell: the risk
    // chips need 27 of the 36% they are allocated, and at one column less the
    // pane is still wide enough for every other cell in the ribbon.
    let client = book();

    // 117 columns: the ribbon's pane is exactly 74 and every cell clears.
    let admitted = content(&client.frame(117, 30));
    assert!(
        admitted.contains("$10,000.00"),
        "the floor refused itself:\n{admitted}"
    );
    assert!(
        admitted.contains("CVAR95"),
        "the risk chips lost a column:\n{admitted}"
    );

    // 116: one cell less, and the chips would lose a digit off a percentage.
    let refused = content(&client.frame(116, 30));
    assert!(
        refused.contains("book ribbon needs 74 columns"),
        "a pane one cell under the floor drew the ribbon anyway:\n{refused}"
    );
    assert!(!refused.contains("$10,000.00"), "{refused}");
}

#[test]
fn a_pane_too_short_for_three_rows_refuses_rather_than_dropping_one() {
    // A `Paragraph` clipped from the bottom loses the sub-line silently, and the
    // sub-line is where the cash split, the gainers and the window live.
    let content = content(&book().frame(120, 5));
    assert!(content.contains("book ribbon needs"), "{content}");
    assert!(content.contains("rows"), "{content}");
}

#[test]
fn the_rest_of_the_book_names_the_task_that_fills_it() {
    // "Nothing here yet" and "this broke" have to be distinguishable at a
    // glance for as long as this branch is half-built. Task 12 is no longer
    // named because the blotter under the ribbon *is* Task 12.
    let content = content(&book().frame(120, 36));
    assert!(content.contains("Task 13"), "{content}");
    assert!(
        !content.contains("Task 12"),
        "the blotter is built and still advertised as pending:\n{content}"
    );
}

// -- the blotter -----------------------------------------------------------

/// A live book of `n` positions, weighted so the default sort has an order to
/// find: `P00` is the heaviest and `P{n-1}` the lightest.
///
/// Tickers the market section does not carry, on purpose — the pager and sort
/// tests are about row order, and a fixture that also had to be in the universe
/// would tie them to the market fixture's five assets.
fn book_of(n: usize) -> Client {
    let positions: Vec<String> = (0..n)
        .map(|i| {
            format!(
                r#"{{"ticker": "P{i:02}", "qty": 1.0, "avg_price": 10.0, "price": 10.0,
                     "value": {value}, "weight": {weight},
                     "unrealized_pnl": 0.0, "unrealized_pnl_pct": 0.0}}"#,
                value = 100 * (n - i),
                weight = (n - i) as f64 / 100.0,
            )
        })
        .collect();
    book_from(&format!(
        r#"{{"live_portfolio": {{"equity": 1000.0, "positions": [{}]}}}}"#,
        positions.join(",")
    ))
}

/// The style of the first `needle` on the rendered row that carries `row`.
///
/// `body_style_of` takes the first match anywhere in the frame, which is the
/// wrong cell here more often than it is the right one: the ribbon's rule is a
/// long run of `─`, so a pin on the blotter's absent-trend glyph would read the
/// panel border instead. Narrowing to a row is what makes the answer the one the
/// test is about.
fn cell_style_on(buf: &Buffer, row: &str, needle: &str) -> Style {
    for y in 1..buf.area.height.saturating_sub(1) {
        let cells: Vec<String> = (0..buf.area.width)
            .map(|x| buf[(x, y)].symbol().to_string())
            .collect();
        let text = cells.concat();
        if !text.contains(row) {
            continue;
        }
        let Some(byte) = text.find(needle) else {
            continue;
        };
        let mut at = 0;
        for (i, cell) in cells.iter().enumerate() {
            if at == byte {
                return buf[(i as u16, y)].style();
            }
            at += cell.len();
        }
    }
    panic!("no row carrying {row:?} has a cell run {needle:?}:\n{buf:?}");
}

/// The rendered rows of the blotter — the lines between its column header and
/// its footer, in the order they were drawn.
fn blotter_rows(client: &Client, w: u16, h: u16) -> Vec<String> {
    let frame = client.frame(w, h);
    let content = content(&frame);
    // `content` keeps the shell's own rules on both edges of every row, so an
    // otherwise blank row reads as `│  …  │` rather than as empty.
    let lines: Vec<String> = content
        .lines()
        .map(|line| line.trim_matches(|c| c == '│' || c == ' ').to_string())
        .collect();
    let header = lines
        .iter()
        .position(|l| l.contains("SYMBOL"))
        .unwrap_or_else(|| panic!("the blotter drew no column header:\n{content}"));
    lines[header + 1..]
        .iter()
        .take_while(|l| !l.is_empty() && !l.contains("sort"))
        .cloned()
        .collect()
}

/// The tickers of the rendered rows, in the order the blotter drew them.
fn blotter_symbols(client: &Client, w: u16, h: u16) -> Vec<String> {
    blotter_rows(client, w, h)
        .iter()
        .filter_map(|row| row.split_whitespace().next())
        .map(|s| s.trim_start_matches('▌').to_string())
        .collect()
}

#[test]
fn the_blotter_states_every_position_the_owner_marked() {
    // Nine columns, and not one of them a headline the ribbon already owns.
    let content = content(&book().frame(120, 36));
    for header in [
        "SYMBOL", "QTY", "LAST", "AVG", "WT%", "MKTVAL", "P&L", "TREND",
    ] {
        assert!(content.contains(header), "{header} is missing:\n{content}");
    }
    let bndw = line_with(&content, "BNDW");
    assert!(bndw.contains("59.19"), "the qty:\n{bndw}");
    assert!(bndw.contains("67.58"), "the mark:\n{bndw}");
    assert!(bndw.contains("40.0%"), "the weight:\n{bndw}");
    assert!(bndw.contains("$4.00K"), "the market value:\n{bndw}");
    let acwi = line_with(&content, "ACWI");
    assert!(acwi.contains("6.3%"), "{acwi}");
    assert!(acwi.contains("$628.33"), "{acwi}");
    assert!(acwi.contains("4.12"), "the fractional qty at 2dp:\n{acwi}");
}

#[test]
fn the_blotter_does_not_restate_a_headline_the_ribbon_owns() {
    // The rule the ribbon's module doc states: one panel repeating the equity
    // is a second account of it. The blotter's money is per position.
    let content = content(&book().frame(120, 36));
    assert_eq!(content.matches("$10,000.00").count(), 1, "{content}");
    for header in ["EQUITY", "CASH", "TOTAL"] {
        assert!(
            !content.contains(header),
            "the blotter grew a {header} column:\n{content}"
        );
    }
}

#[test]
fn the_pnl_and_its_percent_are_one_colour_decision_and_not_two() {
    // The paired-axis rule: both cells answer "did this position make money",
    // and a percentage coloured off its own sign would be a second axis that
    // can disagree with the first — a green `+1.20%` beside a red `-$4.00`.
    let client = book_from(
        r#"{"live_portfolio": {"equity": 1000.0, "positions": [
             {"ticker": "WIN", "qty": 1.0, "value": 100.0, "weight": 0.1,
              "unrealized_pnl": 50.0, "unrealized_pnl_pct": 0.5},
             {"ticker": "LOSE", "qty": 1.0, "value": 100.0, "weight": 0.09,
              "unrealized_pnl": -40.0, "unrealized_pnl_pct": -0.4}]}}"#,
    );
    let buf = client.buffer(120, 36);
    let t = Theme::truecolor();
    assert_eq!(body_style_of(&buf, "+$50.00").fg, Some(t.positive));
    assert_eq!(body_style_of(&buf, "+50.00%").fg, Some(t.positive));
    assert_eq!(body_style_of(&buf, "-$40.00").fg, Some(t.negative));
    assert_eq!(body_style_of(&buf, "-40.00%").fg, Some(t.negative));
    assert_eq!(
        body_style_of(&buf, "+$50.00"),
        body_style_of(&buf, "+50.00%"),
        "the pair disagreed about one row"
    );
}

#[test]
fn a_position_that_has_not_moved_is_neither_a_gain_nor_a_loss() {
    // `Theme::change` paints zero green, which is right for a single hero
    // number and wrong for a column of them: a paper book that opened flat
    // renders as ten green rows, a claim that the desk made money on all ten.
    // The book is up overall so the ribbon's own `+$5.00` cannot be mistaken
    // for the row this reads.
    let client = book_from(
        r#"{"live_portfolio": {"equity": 1000.0, "unrealized_pnl": 5.0, "positions": [
             {"ticker": "MOVED", "qty": 1.0, "value": 200.0, "weight": 0.2,
              "unrealized_pnl": 5.0, "unrealized_pnl_pct": 0.025},
             {"ticker": "FLAT", "qty": 1.0, "value": 100.0, "weight": 0.1,
              "unrealized_pnl": 0.0, "unrealized_pnl_pct": 0.0}]}}"#,
    );
    let buf = client.buffer(120, 36);
    let t = Theme::truecolor();
    let flat = cell_style_on(&buf, "FLAT", "+$0.00");
    assert_eq!(flat.fg, Some(t.text_primary), "flat drew as a gain");
    assert_eq!(
        cell_style_on(&buf, "FLAT", "+0.00%").fg,
        Some(t.text_primary)
    );
    // And a row that did move still takes the semantic pair, so this is not
    // satisfied by painting the whole column one colour.
    assert_eq!(cell_style_on(&buf, "MOVED", "+$5.00").fg, Some(t.positive));
}

#[test]
fn a_pnl_the_owner_never_sent_is_not_a_flat_one() {
    // Absent is not zero, and it is not flat either: the pair takes the tone
    // for "the owner declined to say" rather than the tone for "unchanged".
    let client = book_from(
        r#"{"live_portfolio": {"equity": 1000.0, "positions": [
             {"ticker": "QUIET", "qty": 1.0, "avg_price": 10.0, "price": 10.0,
              "value": 100.0, "weight": 0.1}]}}"#,
    );
    let content = content(&client.frame(120, 36));
    let row = line_with(&content, "QUIET");
    assert_eq!(
        row.matches("--").count(),
        2,
        "an absent P&L and its percent must both read missing:\n{row}"
    );
    assert!(!row.contains("$0.00"), "absent rendered as flat:\n{row}");
    assert_eq!(
        cell_style_on(&client.buffer(120, 36), "QUIET", "--").fg,
        Some(Theme::truecolor().text_secondary),
        "an unmeasured row took the flat tone"
    );
}

#[test]
fn the_trend_column_says_which_of_the_three_things_it_knows() {
    // ACWI is in the market section with ten closes; BNDW is held and not
    // polled; STALE is polled and carries no closes. One `--` for the last two
    // would say "no data" where the honest answer is which kind of nothing.
    let client = book_from(
        r#"{"market": {"assets": [
              {"ticker": "ACWI", "price": 152.47, "history": [1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0]},
              {"ticker": "STALE", "price": 10.0, "history": []}]},
            "live_portfolio": {"equity": 1000.0, "positions": [
              {"ticker": "ACWI", "qty": 1.0, "value": 300.0, "weight": 0.3, "unrealized_pnl": 0.0},
              {"ticker": "STALE", "qty": 1.0, "value": 200.0, "weight": 0.2, "unrealized_pnl": 0.0},
              {"ticker": "BNDW", "qty": 1.0, "value": 100.0, "weight": 0.1, "unrealized_pnl": 0.0}]}}"#,
    );
    let content = content(&client.frame(120, 36));
    let t = Theme::truecolor();
    assert!(
        line_with(&content, "ACWI").contains("▁▂▃▄▅▆▇█"),
        "{content}"
    );
    assert!(line_with(&content, "STALE").contains("╌╌╌╌"), "{content}");
    assert!(line_with(&content, "BNDW").contains("────"), "{content}");

    let buf = client.buffer(120, 36);
    assert_eq!(cell_style_on(&buf, "ACWI", "▁▂▃▄▅▆▇█").fg, Some(t.positive));
    assert_eq!(cell_style_on(&buf, "STALE", "╌╌╌╌").fg, Some(t.border_med));
    assert_eq!(
        cell_style_on(&buf, "BNDW", "────").fg,
        Some(t.text_tertiary)
    );
}

#[test]
fn the_last_column_takes_the_stream_price_and_flashes_when_it_moves() {
    // The blotter is a price surface, so it reads through `asset_view` like
    // every other one: a row that read `position.price` would render the poll's
    // number and lose every quote that arrived since.
    let mut client = book();
    let at = client.now + Duration::from_millis(900);
    client.now = at;
    client.store.apply(
        AppEvent::Sse(SseEvent {
            kind: "quote".into(),
            payload: serde_json::json!({"rows": [
                {"ticker": "ACWI", "price": 153.99, "change_1d": 0.01}
            ]}),
            ts: None,
            id: None,
        }),
        at,
    );
    let content = content(&client.frame(120, 36));
    assert!(line_with(&content, "ACWI").contains("153.99"), "{content}");

    client.fx.flash(FlashKey::price("ACWI"), at);
    assert_eq!(
        cell_style_on(&client.buffer(120, 36), "ACWI", "153.99").bg,
        Some(Theme::truecolor().accent_dim),
        "a moved mark did not light its cell"
    );

    // And it ends. A highlight that never goes out stops meaning "this moved".
    client.fx = atlas::fx::FlashTracker::default();
    client.now = at + Duration::from_millis(700);
    assert_ne!(
        cell_style_on(&client.buffer(120, 36), "ACWI", "153.99").bg,
        Some(Theme::truecolor().accent_dim)
    );
}

#[test]
fn a_ticker_outside_the_polled_universe_still_states_its_own_mark() {
    // `asset_view` has nothing to say about BNDW — it is held and not polled —
    // and the position's own price is then the only account of it. A `--` here
    // beside a `$4.00K` market value would read as a value with no price.
    let content = content(&book().frame(120, 36));
    assert!(line_with(&content, "BNDW").contains("67.58"), "{content}");
}

#[test]
fn the_blotter_sorts_on_the_number_and_never_on_the_string() {
    // `$2.50M` sorts *below* `$999.00K` as text and above it as money. The
    // display string is what a lazy sort reaches for, so the key is kept beside
    // it rather than parsed back out of it.
    let mut client = book_from(
        r#"{"live_portfolio": {"equity": 1.0, "positions": [
             {"ticker": "SMALL", "qty": 1.0, "value": 999000.0, "weight": 0.1, "unrealized_pnl": 0.0},
             {"ticker": "BIG", "qty": 1.0, "value": 2500000.0, "weight": 0.2, "unrealized_pnl": 0.0}]}}"#,
    );
    client.press(KeyCode::Char('s')); // WT% → MKTVAL
    let content = content(&client.frame(120, 36));
    assert!(
        content.contains("$2.50M") && content.contains("$999.00K"),
        "{content}"
    );
    assert_eq!(
        blotter_symbols(&client, 120, 36),
        vec!["BIG", "SMALL"],
        "a $2.5M position sorted under a $999K one:\n{content}"
    );
}

#[test]
fn the_default_sort_is_the_heaviest_position_first() {
    // What an operator asks the blotter first: where is the money.
    assert_eq!(blotter_symbols(&book(), 120, 36), vec!["BNDW", "ACWI"]);
}

#[test]
fn a_sort_is_stable_so_equal_keys_keep_the_owners_order() {
    // Five positions the owner sent at one weight. An unstable sort reorders
    // them on a repaint, which is a blotter that will not hold still.
    let client = book_from(
        r#"{"live_portfolio": {"equity": 1.0, "positions": [
             {"ticker": "AAA", "qty": 1.0, "value": 1.0, "weight": 0.2, "unrealized_pnl": 0.0},
             {"ticker": "BBB", "qty": 1.0, "value": 1.0, "weight": 0.2, "unrealized_pnl": 0.0},
             {"ticker": "CCC", "qty": 1.0, "value": 1.0, "weight": 0.2, "unrealized_pnl": 0.0},
             {"ticker": "DDD", "qty": 1.0, "value": 1.0, "weight": 0.2, "unrealized_pnl": 0.0},
             {"ticker": "EEE", "qty": 1.0, "value": 1.0, "weight": 0.2, "unrealized_pnl": 0.0}]}}"#,
    );
    let order = blotter_symbols(&client, 120, 36);
    assert_eq!(order, vec!["AAA", "BBB", "CCC", "DDD", "EEE"]);
    assert_eq!(
        order,
        blotter_symbols(&client, 120, 36),
        "a repaint reshuffled"
    );
}

#[test]
fn a_position_the_owner_sent_no_number_for_sorts_last_in_either_direction() {
    // Absent is not zero and it is not the biggest either: a `--` at the head of
    // a heaviest-first list is the one row an operator would read as the answer.
    let client = book_from(
        r#"{"live_portfolio": {"equity": 1.0, "positions": [
             {"ticker": "UNSAID", "qty": 1.0, "value": 1.0, "unrealized_pnl": 0.0},
             {"ticker": "TINY", "qty": 1.0, "value": 1.0, "weight": 0.01, "unrealized_pnl": 0.0}]}}"#,
    );
    assert_eq!(blotter_symbols(&client, 120, 36), vec!["TINY", "UNSAID"]);
}

#[test]
fn the_sort_key_cycles_and_the_header_says_which_one_is_live() {
    // A grid that reorders itself with nothing on screen saying why is a grid
    // an operator stops trusting.
    let mut client = book();
    for column in ["WT%", "MKTVAL", "P&L", "P&L%", "SYMBOL", "WT%"] {
        let content = content(&client.frame(120, 36));
        let header = line_with(&content, "SYMBOL");
        assert!(
            header.contains(&format!("{column}▾")) || header.contains(&format!("{column}▴")),
            "{column} is sorting and the header does not say so:\n{header}"
        );
        client.press(KeyCode::Char('s'));
    }
}

#[test]
fn sorting_by_symbol_is_alphabetical_and_the_rest_are_biggest_first() {
    // The two orders are opposites in this fixture on purpose: a symbol sort
    // that quietly kept the weight order would pass against any book whose
    // heaviest position happens to sort first alphabetically.
    let mut client = book_from(
        r#"{"live_portfolio": {"equity": 1.0, "positions": [
             {"ticker": "AAA", "qty": 1.0, "value": 1.0, "weight": 0.1, "unrealized_pnl": 0.0},
             {"ticker": "ZZZ", "qty": 1.0, "value": 9.0, "weight": 0.5, "unrealized_pnl": 0.0}]}}"#,
    );
    assert_eq!(blotter_symbols(&client, 120, 36), vec!["ZZZ", "AAA"]);
    client.keys(&[KeyCode::Char('s'); 4]); // WT% → … → SYMBOL
    assert_eq!(blotter_symbols(&client, 120, 36), vec!["AAA", "ZZZ"]);
}

#[test]
fn the_pager_appears_only_when_the_rows_outrun_the_rows_they_were_given() {
    // Twelve positions in a pane with five rows for them: three pages.
    let client = book_of(12);
    let tall = content(&client.frame(120, 36));
    assert!(
        !tall.contains("1/"),
        "a blotter that fits drew a pager anyway:\n{tall}"
    );
    let short = content(&client.frame(120, 15));
    assert!(short.contains("1/3"), "{short}");
    assert!(short.contains('»'), "{short}");
}

#[test]
fn the_page_size_is_the_rows_the_blotter_was_given_and_not_the_frame() {
    // Task 9's lesson: the pane is not the allocation. Sized off the frame the
    // page would be eleven rows too long and the pager would claim one page.
    let client = book_of(12);
    assert_eq!(blotter_symbols(&client, 120, 15).len(), 5);
    assert_eq!(blotter_symbols(&client, 120, 16).len(), 6);
}

#[test]
fn the_bracket_keys_walk_the_pages_and_stop_at_both_ends() {
    let mut client = book_of(12);
    let _ = client.frame(120, 15); // the draw is what tells the view its page size
    assert_eq!(blotter_symbols(&client, 120, 15)[0], "P00");

    client.press(KeyCode::Char(']'));
    assert_eq!(blotter_symbols(&client, 120, 15)[0], "P05");
    assert!(content(&client.frame(120, 15)).contains("2/3"));

    client.press(KeyCode::Char(']'));
    assert_eq!(blotter_symbols(&client, 120, 15)[0], "P10");
    client.press(KeyCode::Char(']'));
    assert_eq!(
        blotter_symbols(&client, 120, 15)[0],
        "P10",
        "the last page wrapped"
    );

    client.press(KeyCode::Char('['));
    assert_eq!(blotter_symbols(&client, 120, 15)[0], "P05");
    client.keys(&[KeyCode::Char('['); 3]);
    assert_eq!(
        blotter_symbols(&client, 120, 15)[0],
        "P00",
        "the first page wrapped"
    );
}

#[test]
fn a_resize_keeps_the_row_that_was_at_the_top_at_the_top() {
    // The regression a stored *page index* ships with: multiplied by a new page
    // size it lands somewhere the operator never scrolled to. The top row is
    // what is kept, and the page number is recomputed from it.
    let mut client = book_of(12);
    let _ = client.frame(120, 15);
    client.press(KeyCode::Char(']'));
    assert_eq!(blotter_symbols(&client, 120, 15)[0], "P05");

    // Five rows a page becomes ten. A page index of 2 would put P10 on top.
    assert_eq!(
        blotter_symbols(&client, 120, 20)[0],
        "P05",
        "the resize moved the operator somewhere they did not scroll to"
    );
}

#[test]
fn a_book_that_shrinks_under_the_cursor_cannot_page_off_the_end() {
    let mut client = book_of(12);
    let _ = client.frame(120, 15);
    client.keys(&[KeyCode::Char(']'); 2]);
    assert_eq!(blotter_symbols(&client, 120, 15)[0], "P10");
    // The owner closes everything but two positions.
    client.store.apply(
        AppEvent::Snapshot(Box::new(
            serde_json::from_str::<Snapshot>(
                r#"{"live_portfolio": {"equity": 1.0, "positions": [
                     {"ticker": "P00", "qty": 1.0, "value": 1.0, "weight": 0.2, "unrealized_pnl": 0.0},
                     {"ticker": "P01", "qty": 1.0, "value": 1.0, "weight": 0.1, "unrealized_pnl": 0.0}]}}"#,
            )
            .unwrap(),
        )),
        client.now,
    );
    assert_eq!(blotter_symbols(&client, 120, 15), vec!["P00", "P01"]);
}

#[test]
fn a_cell_too_narrow_for_its_number_loses_a_digit_and_never_the_sign() {
    // Task 9's hard lesson, at this call site. `-123.45%` is eight characters in
    // a seven-cell column; ratatui right-aligns by dropping *leading* cells, so
    // without the clamp the rendered cell is `123.45%` — a position down more
    // than its cost, drawn as one that doubled.
    let client = book_from(
        r#"{"live_portfolio": {"equity": 1.0, "positions": [
             {"ticker": "BAD", "qty": 1.0, "value": 1.0, "weight": 0.1,
              "unrealized_pnl": -12.0, "unrealized_pnl_pct": -1.2345}]}}"#,
    );
    let content = content(&client.frame(120, 36));
    let row = line_with(&content, "BAD");
    assert!(
        row.contains("-123.45"),
        "the sign went over the side:\n{row}"
    );
    assert!(
        !row.contains("123.45%"),
        "a 123% loss rendered as a gain:\n{row}"
    );
}

#[test]
fn a_pane_below_the_blotters_floor_refuses_rather_than_clipping_a_number() {
    // Below the floor `Table` shrinks the columns underneath the widths every
    // cell was held to, and `head` cannot see it: it is spent against the
    // declared width, and the allocation is what shrank.
    let refused = content(&book().frame(117, 36));
    assert!(refused.contains("positions blotter needs"), "{refused}");
    // The remedy wraps at this width, so the pin is on the word that names it.
    assert!(refused.contains("widen"), "{refused}");
    assert!(!refused.contains("$4.00K"), "{refused}");
    // Bracketed: a floor only ever tested from far below is a floor nobody
    // knows the height of. One column wider and every cell clears.
    assert!(content(&book().frame(118, 36)).contains("$4.00K"));
}

#[test]
fn a_blotter_with_no_room_for_a_row_says_so_rather_than_drawing_a_header() {
    // A header and a pager over zero rows is a blotter that says the book is
    // empty. It says the pane is instead.
    let content = content(&book().frame(120, 10));
    assert!(content.contains("positions blotter needs"), "{content}");
    assert!(content.contains("taller"), "{content}");
}

#[test]
fn a_book_with_no_positions_says_which_kind_of_nothing_it_is() {
    let empty = content(
        &book_from(r#"{"live_portfolio": {"equity": 1000.0, "positions": []}}"#).frame(120, 36),
    );
    assert!(empty.contains("no positions"), "{empty}");
    let unmarked = content(&book_from(r#"{"portfolio": {"equity": 1.0}}"#).frame(120, 36));
    assert!(unmarked.contains("no live book"), "{unmarked}");
    assert_ne!(
        empty.contains("no live book"),
        unmarked.contains("no live book"),
        "an empty book and an unmarked one read the same"
    );
}

#[test]
fn a_position_the_owner_sent_no_ticker_for_is_still_a_row() {
    // Dropping it would leave the blotter's row count disagreeing with the
    // ribbon's `N pos`, which is the sort of quiet gap nobody reconciles.
    let content = content(
        &book_from(
            r#"{"live_portfolio": {"equity": 1000.0, "positions": [
                 {"qty": 1.0, "value": 500.0, "weight": 0.5, "unrealized_pnl": 0.0}]}}"#,
        )
        .frame(120, 36),
    );
    assert!(
        content.contains("$500.00"),
        "the money went missing with the name:\n{content}"
    );
}

#[test]
fn a_narrow_terminal_still_renders_the_book_without_panicking() {
    let client = book();
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
