//! BOOK's four panes, pinned as a whole frame plus the facts a golden cannot state.
//!
//! The ribbon is the desk's single account of its own headline numbers, the
//! blotter is the book position by position, the rail shades it and the curve
//! draws where it has been. Most of these tests are about what those must *not*
//! do: invent a zero for a number the owner never sent, state the equity twice,
//! keep drawing when a pane is too small for the digits in it, or stay silent
//! about a halted book.
//!
//! Content assertions go through `content`, the columns this view owns. The
//! ticker tape along the top and the pulse rail down the right render prices,
//! percentages and `--` of their own, so a pin on the whole frame could pass on
//! chrome and say nothing about the view under it.

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
fn the_book_view_renders_its_four_panes_at_120x36() {
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
    assert!(content.contains("3 pos"), "the position count:\n{content}");
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
    //
    // The rule is about *this view* and about the KPI vocabulary the ribbon
    // owns — cash, P&L, exposure, drawdown, the window's change. DESK's equity
    // hero is the one other place the figure renders, deliberately: it is the
    // same `live_portfolio.equity` at a different altitude, on a view an
    // operator reaches instead of this one rather than alongside it. What would
    // break this rule is a *second* panel on BOOK, or a hero that read the
    // registry's `portfolio` and quietly meant something else.
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
fn the_book_names_no_task_because_every_pane_of_it_is_built() {
    // BOOK carried a placeholder line naming the task that would fill it for as
    // long as it was half-built. All four panes are here now, so the line is
    // gone — and a view that still advertised pending work would be the same
    // "nothing here yet" ambiguity in reverse.
    let content = content(&book().frame(120, 36));
    assert!(
        !content.contains("Task"),
        "a built view still names a task:\n{content}"
    );
    for pane in ["PORTFOLIO VALUE", "POSITIONS", "EQUITY", "HOLDINGS"] {
        assert!(content.contains(pane), "{pane} is missing:\n{content}");
    }
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
        // Stopping on a blank row is not enough once the footer sits directly
        // under a full blotter: at the heights the pager tests use there is no
        // blank between them, and the heat tiles would be counted as positions.
        // `▌ ` is a panel header — a selected blotter row's marker is `▌BNDW`,
        // with no space — so it is the one prefix that says "a new pane".
        .take_while(|l| !l.is_empty() && !l.contains("sort") && !l.starts_with("▌ "))
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
    // Read off the blotter's own column header rather than the whole view: the
    // curve at the foot is headed EQUITY, and it is a *series* rather than a
    // second statement of today's number — its scale runs $9,987.10 to
    // $10,012.40 and states the hero figure nowhere.
    let columns = line_with(&content, "SYMBOL");
    for header in ["EQUITY", "CASH", "TOTAL"] {
        assert!(
            !columns.contains(header),
            "the blotter grew a {header} column: {columns}"
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
    // `format::change_tone` paints zero green, which is right for a single hero
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

    client.fx.flashes.flash(FlashKey::price("ACWI"), at);
    assert_eq!(
        cell_style_on(&client.buffer(120, 36), "ACWI", "153.99").bg,
        Some(Theme::truecolor().accent_dim),
        "a moved mark did not light its cell"
    );

    // And it ends. A highlight that never goes out stops meaning "this moved".
    client.fx = atlas::fx::Fx::default();
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
    // What an operator asks the blotter first: where is the money. `DUST` is
    // the fixture's dust holding at a tenth of a percent — last by weight, and
    // there for the reason `the_fixtures_dust_holding_is_neither` states.
    assert_eq!(
        blotter_symbols(&book(), 120, 36),
        vec!["BNDW", "ACWI", "DUST"]
    );
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

/// The frame height at which the blotter gets exactly five rows for positions.
///
/// The tape and the status line take one each, the ribbon four, the plans band
/// five, the footer ten, and the blotter spends two of what is left on its panel
/// header and its column header. Spelled once here because Task 13's footer and
/// Task 18's plan cards each moved every one of these numbers, and a page size
/// hard-coded in six tests is six places to re-derive it the next time a band
/// changes height.
const PAGE_5: u16 = 28;

#[test]
fn the_pager_appears_only_when_the_rows_outrun_the_rows_they_were_given() {
    // Twelve positions in a pane with five rows for them: three pages.
    let client = book_of(12);
    let tall = content(&client.frame(120, 36));
    assert!(
        !tall.contains("1/"),
        "a blotter that fits drew a pager anyway:\n{tall}"
    );
    let short = content(&client.frame(120, PAGE_5));
    assert!(short.contains("1/3"), "{short}");
    assert!(short.contains('»'), "{short}");
}

#[test]
fn the_page_size_is_the_rows_the_blotter_was_given_and_not_the_frame() {
    // Task 9's lesson: the pane is not the allocation. Sized off the frame the
    // page would be nineteen rows too long and the pager would claim one page.
    let client = book_of(12);
    assert_eq!(blotter_symbols(&client, 120, PAGE_5).len(), 5);
    assert_eq!(blotter_symbols(&client, 120, PAGE_5 + 1).len(), 6);
}

#[test]
fn the_bracket_keys_walk_the_pages_and_stop_at_both_ends() {
    let mut client = book_of(12);
    let _ = client.frame(120, PAGE_5); // the draw tells the view its page size
    assert_eq!(blotter_symbols(&client, 120, PAGE_5)[0], "P00");

    client.press(KeyCode::Char(']'));
    assert_eq!(blotter_symbols(&client, 120, PAGE_5)[0], "P05");
    assert!(content(&client.frame(120, PAGE_5)).contains("2/3"));

    client.press(KeyCode::Char(']'));
    assert_eq!(blotter_symbols(&client, 120, PAGE_5)[0], "P10");
    client.press(KeyCode::Char(']'));
    assert_eq!(
        blotter_symbols(&client, 120, PAGE_5)[0],
        "P10",
        "the last page wrapped"
    );

    client.press(KeyCode::Char('['));
    assert_eq!(blotter_symbols(&client, 120, PAGE_5)[0], "P05");
    client.keys(&[KeyCode::Char('['); 3]);
    assert_eq!(
        blotter_symbols(&client, 120, PAGE_5)[0],
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
    let _ = client.frame(120, PAGE_5);
    client.press(KeyCode::Char(']'));
    assert_eq!(blotter_symbols(&client, 120, PAGE_5)[0], "P05");

    // Five rows a page becomes ten. A page index of 2 would put P10 on top.
    assert_eq!(
        blotter_symbols(&client, 120, PAGE_5 + 5)[0],
        "P05",
        "the resize moved the operator somewhere they did not scroll to"
    );
}

#[test]
fn a_book_that_shrinks_under_the_cursor_cannot_page_off_the_end() {
    let mut client = book_of(12);
    let _ = client.frame(120, PAGE_5);
    client.keys(&[KeyCode::Char(']'); 2]);
    assert_eq!(blotter_symbols(&client, 120, PAGE_5)[0], "P10");
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
    assert_eq!(blotter_symbols(&client, 120, PAGE_5), vec!["P00", "P01"]);
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
    //
    // Eight rows is where that now bites: the footer's height is arithmetic
    // rather than a constraint, so the blotter keeps its own three rows until
    // there are not three left to keep — a footer that survived by pushing the
    // book off the screen would be a trade-off nobody chose.
    let refused = content(&book().frame(120, 8));
    assert!(refused.contains("positions blotter needs"), "{refused}");
    assert!(refused.contains("taller"), "{refused}");
    // Bracketed: one row more and the blotter has its floor back.
    let admitted = content(&book().frame(120, 9));
    assert!(
        admitted.contains("SYMBOL"),
        "the blotter refused a pane it fits in:\n{admitted}"
    );
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

// -- the holdings rail -----------------------------------------------------

/// A book of four holdings, one of each thing a tile can be: a runaway winner,
/// a loser inside the ramp, a position that has not moved, and one the owner
/// sent no P&L for at all.
#[test]
fn a_debt_of_nothing_is_not_drawn_as_a_loss_on_either_surface() {
    // The magnitude a fully-invested paper book actually carries — `desk.rs`
    // cites -2.2e-16 off a real one. Both surfaces round it away in the digits
    // (`+$0.00`, `+0.0%`) and both used to threshold the raw double, so the
    // shade and the tone contradicted the number they were painting.
    let t = Theme::truecolor();
    let client = book_from(
        r#"{"live_portfolio": {"equity": 1000.0, "unrealized_pnl": 5.0, "positions": [
             {"ticker": "REAL", "qty": 1.0, "value": 400.0, "weight": 0.4,
              "unrealized_pnl": -8.0, "unrealized_pnl_pct": -0.02},
             {"ticker": "DUST", "qty": 1.0, "value": 100.0, "weight": 0.1,
              "unrealized_pnl": -1e-13, "unrealized_pnl_pct": -1e-13}]}}"#,
    );
    let buf = client.buffer(120, 36);

    // The blotter row: both money cells take one tone, and `+$0.00` beside a
    // red one is a row arguing with itself.
    assert_eq!(
        cell_style_on(&buf, "DUST", "+$0.00").fg,
        Some(t.text_primary),
        "a debt of nothing drew red"
    );
    assert_eq!(
        cell_style_on(&buf, "DUST", "+0.00%").fg,
        Some(t.text_primary)
    );
    // The holdings rail's tile, at its own one-decimal precision.
    let tile = cell_style_on(&buf, "DUST  +0.0%", "DUST");
    assert_eq!(tile.fg, Some(t.text_primary), "the tile shaded a loss");
    assert_eq!(tile.bg, Some(t.bg_base));

    // And the neighbour that really did lose money still reads as it: the rule
    // must round a nothing away, not round a loss away.
    assert_eq!(
        cell_style_on(&buf, "REAL", "-$8.00").fg,
        Some(t.negative),
        "the rounding swallowed a real loss"
    );
    assert_eq!(
        cell_style_on(&buf, "REAL  -2.0%", "REAL").fg,
        Some(t.negative_dim),
        "the tile left the negative ramp"
    );
}

#[test]
fn the_fixtures_dust_holding_is_neither_a_gain_nor_a_loss_on_any_surface() {
    // The tripwire, and the reason the shared fixture carries a `DUST` row at
    // all. The test above builds its own book, so it only ever catches the
    // class on the two surfaces it happens to name; the captured payload every
    // other golden reads carried no magnitude below its own printed precision,
    // which is exactly why a threshold on the raw double could be added to a
    // *new* surface with every golden still green.
    //
    // The fixture's row is `-1e-13` in both fields — the residue a
    // fully-invested paper book actually carries. Every surface that draws it
    // must round it away in the digits *and* in the colour beside them.
    let t = Theme::truecolor();
    let buf = book().buffer(120, 36);

    // The blotter's paired money cells.
    assert_eq!(
        cell_style_on(&buf, "DUST", "+$0.00").fg,
        Some(t.text_primary),
        "a debt of nothing drew as a direction"
    );
    assert_eq!(
        cell_style_on(&buf, "DUST", "+0.00%").fg,
        Some(t.text_primary)
    );
    // The holdings rail's tile, at its own one-decimal precision.
    let tile = cell_style_on(&buf, "DUST  +0.0%", "DUST");
    assert_eq!(tile.fg, Some(t.text_primary), "the tile shaded a nothing");
    assert_eq!(tile.bg, Some(t.bg_base));
    // And the movers footer, which the dust row is now the bottom of: a book
    // whose worst name did not move gets the flat glyph and the flat tone, not
    // a red ▼ over `+0.0%`.
    assert_eq!(
        cell_style_on(&buf, "worst DUST", "·").fg,
        Some(t.text_primary),
        "the worst mover claimed a direction it does not have"
    );
    assert_eq!(
        cell_style_on(&buf, "worst DUST", "+0.0%").fg,
        Some(t.text_primary)
    );

    // The ribbon counts it as neither, which is what keeps the two panes from
    // saying different things about one row.
    assert!(
        content(&book().frame(120, 36)).contains("▲0 ▼0"),
        "the dust row was counted as a gainer or a loser"
    );
}

fn shaded_book() -> Client {
    book_from(
        r#"{"live_portfolio": {"equity": 1000.0, "positions": [
             {"ticker": "UPUP", "qty": 1.0, "value": 400.0, "weight": 0.4,
              "unrealized_pnl": 80.0, "unrealized_pnl_pct": 0.25},
             {"ticker": "DOWN", "qty": 1.0, "value": 200.0, "weight": 0.2,
              "unrealized_pnl": -7.0, "unrealized_pnl_pct": -0.034},
             {"ticker": "EVEN", "qty": 1.0, "value": 100.0, "weight": 0.1,
              "unrealized_pnl": 0.0, "unrealized_pnl_pct": 0.0},
             {"ticker": "QUIET", "qty": 1.0, "value": 50.0}]}}"#,
    )
}

/// The view read back as one line, with the wrapping taken back out.
///
/// A refusal is a wrapped `Paragraph` by design — a remedy clipped to `make the
/// terminal ta` is one an operator cannot act on — so in a 23-cell rail its
/// sentence is spread over five rows. Pinning it needs the sentence, not the
/// row it happened to break on.
fn flat(content: &str) -> String {
    content
        .replace('│', " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// The two rows under the rail's `TOP MOVERS` header, trimmed.
///
/// Read by position rather than by searching for a ticker: the movers restate
/// names the blotter and the heat grid have already drawn, so a search would
/// find whichever of the three came first in the frame.
fn movers_lines(content: &str) -> Vec<String> {
    let lines: Vec<&str> = content.lines().collect();
    let at = lines
        .iter()
        .position(|l| l.contains("TOP MOVERS"))
        .unwrap_or_else(|| panic!("the rail drew no movers footer:\n{content}"));
    lines[at + 1..]
        .iter()
        .take(2)
        // `content` keeps the shell's own rules on both edges of every row, so
        // an otherwise blank row reads as `│  …  │` rather than as empty.
        .map(|l| l.trim_matches(|c| c == '│' || c == ' ').to_string())
        .collect()
}

#[test]
fn the_rail_shades_every_holding_by_the_open_pnl_it_carries() {
    // Eleven cells a tile: a five-cell ticker and the six the percentage gets.
    // The pins are on whole tiles rather than on tickers, because every one of
    // these names is also a blotter row two panes up.
    let client = shaded_book();
    let content = content(&client.frame(120, 36));
    for tile in ["UPUP +25.0%", "DOWN  -3.4%", "EVEN  +0.0%", "QUIET    --"] {
        assert!(content.contains(tile), "no tile {tile:?}:\n{content}");
    }

    let buf = client.buffer(120, 36);
    let t = Theme::truecolor();
    // A quarter is past the twenty-percent full scale, so it saturates at the
    // brightest step rather than setting a scale of its own.
    assert_eq!(
        cell_style_on(&buf, "UPUP +25.0%", "UPUP").bg,
        Some(t.positive)
    );
    // 3.4% is one band in: the dim tone on the second depth level, and it is
    // the *negative* pair, so magnitude and direction are two separate reads.
    assert_eq!(
        cell_style_on(&buf, "DOWN  -3.4%", "DOWN").fg,
        Some(t.negative_dim)
    );
    // Flat is neither. `format::change_tone` paints zero green, which would make a
    // paper book that opened flat a rail of green tiles.
    let even = cell_style_on(&buf, "EVEN  +0.0%", "EVEN");
    assert_eq!(even.fg, Some(t.text_primary));
    assert_eq!(even.bg, Some(t.bg_base));
    // And absent is not flat: the tone for "the owner declined to say".
    assert_eq!(
        cell_style_on(&buf, "QUIET    --", "QUIET").fg,
        Some(t.text_secondary)
    );
}

#[test]
fn the_h_key_swaps_the_pnl_ramp_for_an_amber_allocation_one() {
    // Two questions, one rail: what is winning, and where is the money. The
    // second has no direction, so it cannot borrow the semantic pair.
    let mut client = shaded_book();
    client.press(KeyCode::Char('h'));
    let weights = content(&client.frame(120, 36));
    for tile in ["UPUP  40.0%", "DOWN  20.0%", "EVEN  10.0%"] {
        assert!(weights.contains(tile), "no tile {tile:?}:\n{weights}");
    }
    assert!(
        !weights.contains("+40.0%"),
        "a weight was drawn with a P&L's sign:\n{weights}"
    );

    let buf = client.buffer(120, 36);
    let t = Theme::truecolor();
    assert_eq!(
        cell_style_on(&buf, "UPUP  40.0%", "UPUP").bg,
        Some(t.accent)
    );
    assert_ne!(
        cell_style_on(&buf, "UPUP  40.0%", "UPUP").bg,
        Some(t.positive),
        "an allocation was drawn as a gain"
    );
    // The header says which of the two is live, so the operator is never
    // reading a percentage without knowing what it measures.
    assert!(weights.contains("h WT"), "{weights}");

    // And back: the toggle is a cycle, not a one-way switch.
    client.press(KeyCode::Char('h'));
    let back = content(&client.frame(120, 36));
    assert!(back.contains("UPUP +25.0%"), "{back}");
    assert!(back.contains("h P&L"), "{back}");
}

#[test]
fn the_movers_name_the_two_ends_of_the_whole_book() {
    // The ends of the book, not of the page: an operator turning to page two
    // has not changed which name is winning.
    let content = content(&shaded_book().frame(120, 36));
    let movers = movers_lines(&content);
    assert!(movers[0].contains("best"), "{movers:?}");
    assert!(movers[0].contains("UPUP"), "{movers:?}");
    assert!(movers[0].contains("+25.0%"), "{movers:?}");
    assert!(movers[0].contains('▲'), "{movers:?}");
    assert!(movers[1].contains("worst"), "{movers:?}");
    assert!(movers[1].contains("DOWN"), "{movers:?}");
    assert!(movers[1].contains("-3.4%"), "{movers:?}");
    assert!(movers[1].contains('▼'), "{movers:?}");
}

#[test]
fn a_book_of_one_position_is_its_own_only_mover() {
    // Two identical rows would read as two movers, which is a desk this client
    // is not looking at. The rail borrows the pulse's own wording for it.
    let content = content(
        &book_from(
            r#"{"live_portfolio": {"equity": 100.0, "positions": [
                 {"ticker": "SOLO", "qty": 1.0, "value": 100.0, "weight": 1.0,
                  "unrealized_pnl": 5.0, "unrealized_pnl_pct": 0.05}]}}"#,
        )
        .frame(120, 36),
    );
    let movers = movers_lines(&content);
    assert!(movers[0].contains("only"), "{movers:?}");
    assert!(movers[0].contains("SOLO"), "{movers:?}");
    assert!(
        !movers[1].contains("SOLO"),
        "one holding was drawn as two movers:\n{movers:?}"
    );
}

#[test]
fn a_flat_book_gets_no_arrow_it_did_not_earn() {
    // A ▲ over `+0.0%` is a rise the desk did not make, and the fixture book —
    // a paper desk that has just opened — is exactly that book.
    let movers = movers_lines(&content(&book().frame(120, 36)));
    assert!(movers[0].contains('·'), "{movers:?}");
    assert!(
        !movers[0].contains('▲') && !movers[0].contains('▼'),
        "a book that has not moved was given a direction:\n{movers:?}"
    );
}

#[test]
fn a_book_with_nothing_in_it_has_no_movers_rather_than_a_zero() {
    // `--`, not `+0.00%`: a book with no positions has no best one, and a
    // percentage here would be a measurement of an empty set.
    let content = content(
        &book_from(r#"{"live_portfolio": {"equity": 1000.0, "positions": []}}"#).frame(120, 36),
    );
    let movers = movers_lines(&content);
    assert!(movers[0].ends_with("--"), "{movers:?}");
    // And the grid above it says nothing at all: the blotter two panes up
    // already names which kind of nothing this is, and a second copy of the
    // same sentence reads as two separate failures.
    assert_eq!(
        content.matches("no positions").count(),
        1,
        "the empty book is stated twice:\n{content}"
    );
}

#[test]
fn a_heatmap_with_more_holdings_than_rows_says_so_rather_than_dropping_one() {
    // The same sub-floor class as the sector strip: a `Paragraph` taller than
    // its area is clipped without complaint, and a book missing its last two
    // names is a rail that says the desk does not hold them.
    let refused = flat(&content(&book_of(20).frame(120, 36)));
    assert!(
        refused.contains("holdings heatmap needs 10 rows for 20 positions"),
        "{refused}"
    );
    assert!(refused.contains("this pane has 6"), "{refused}");
    assert!(refused.contains("taller"), "{refused}");
    // Bracketed: twelve fit the six rows the rail gives the grid, two to a row.
    assert!(
        !content(&book_of(12).frame(120, 36)).contains("holdings heatmap needs"),
        "a book that fits was refused"
    );
}

// -- the equity curve ------------------------------------------------------

/// A book whose owner has booked `n` marks, each one a dollar above the last.
///
/// Rising monotonically so that every period slice has a different *bottom* to
/// its scale: a window that silently drew the whole series would otherwise be
/// indistinguishable from one that drew the trailing part of it.
fn book_with_marks(n: usize) -> Client {
    let points: Vec<String> = (0..n)
        .map(|i| format!(r#"{{"ts": "2026-01-01", "equity": {}.0}}"#, 1000 + i))
        .collect();
    book_from(&format!(
        r#"{{"live_portfolio": {{"equity": 1.0, "positions": []}},
             "performance": {{"series": [{}]}}}}"#,
        points.join(",")
    ))
}

#[test]
fn the_curve_draws_the_owners_series_against_a_money_scale() {
    // The gutter is the series' own range, spelled as money: `compact_money`
    // would render four identical `$10.00K` labels on a book that moved a
    // hundred dollars, which is a scale with no scale on it.
    let content = content(&book().frame(120, 36));
    assert!(
        content.contains("$10,012.40"),
        "the top of the scale:\n{content}"
    );
    assert!(
        content.contains("$9,987.10"),
        "the bottom of the scale:\n{content}"
    );
    // And the line itself: a scale with nothing plotted against it is a gutter.
    assert!(
        content
            .chars()
            .any(|c| ('\u{2801}'..='\u{28ff}').contains(&c)),
        "the curve drew no line:\n{content}"
    );
}

#[test]
fn the_p_key_cycles_the_slice_and_the_strip_says_which_one_is_live() {
    // Four hundred marks, so every window is a different slice of them: at
    // three of the four, a curve that quietly drew the whole series would show
    // a bottom label it has no business showing.
    let mut client = book_with_marks(400);
    let t = Theme::truecolor();
    for (period, floor) in [
        ("ALL", "$1,000.00"),
        ("1Y", "$1,035.00"),
        ("3M", "$1,337.00"),
        ("1M", "$1,379.00"),
    ] {
        let content = content(&client.frame(120, 36));
        assert!(
            content.contains(floor),
            "{period} did not slice to {floor}:\n{content}"
        );
        // The top of the scale is the same mark whatever the window: every
        // slice is a *trailing* one, so they all end at the latest mark.
        assert!(content.contains("$1,399.00"), "{content}");
        let buf = client.buffer(120, 36);
        assert_eq!(
            cell_style_on(&buf, "p period", period).fg,
            Some(t.text_primary),
            "{period} is live and the strip does not say so:\n{content}"
        );
        client.press(KeyCode::Char('p'));
    }
    // The cycle closes: a fifth press is back at the whole series.
    assert!(content(&client.frame(120, 36)).contains("$1,000.00"));
}

#[test]
fn a_slice_with_one_mark_asks_for_more_history_rather_than_drawing_a_dot() {
    // The honest analogue of a greyed-out period button. One mark rendered as a
    // point in the middle of an empty pane is a chart of nothing, and a flat
    // line through it is a chart of something that did not happen.
    let content = content(&book_with_marks(1).frame(120, 36));
    assert!(content.contains("needs more history"), "{content}");
    assert!(content.contains("daily marks only"), "{content}");
    assert!(
        !content.contains("$1,000.00"),
        "a scale was drawn for a series with nothing to plot:\n{content}"
    );
}

#[test]
fn a_desk_that_has_booked_no_series_says_that_rather_than_asking_for_history() {
    // Two different facts: the owner sent no performance section at all, and
    // the owner sent one the window is too short to draw. One sentence for both
    // would hide a broken poll behind a young book.
    let content = content(&book_from(r#"{"live_portfolio": {"equity": 1.0}}"#).frame(120, 36));
    assert!(content.contains("no equity series"), "{content}");
    assert!(!content.contains("needs more history"), "{content}");
}

#[test]
fn a_footer_below_its_floor_refuses_rather_than_handing_the_curve_no_columns() {
    // Split without a floor, a narrow footer gives the rail its fixed 23 and
    // the curve whatever is left — which at some widths is nothing, and a pane
    // with no cells in it has nowhere to print the refusal it owes. So the band
    // says it once, before the split.
    let refused = flat(&content(&book().frame(85, 30)));
    assert!(
        refused.contains("book footer needs 43 columns for an equity curve"),
        "{refused}"
    );
    assert!(refused.contains("this pane has 42"), "{refused}");
    assert!(refused.contains("widen"), "{refused}");
    assert!(
        !refused.contains("HOLDINGS") && !refused.contains("$10,012.40"),
        "half a footer survived below the floor:\n{refused}"
    );

    // Bracketed: one column more and both panes draw, curve scale included.
    let admitted = content(&book().frame(86, 30));
    assert!(admitted.contains("HOLDINGS"), "{admitted}");
    assert!(
        admitted.contains("$10,012.40"),
        "the curve lost its scale at the boundary:\n{admitted}"
    );
    // At the floor the curve's pane is 19 cells and its period strip is 30, so
    // the strip goes whole rather than rendering as `ALL 1Y 3` — a control an
    // operator reads as broken rather than as absent.
    assert!(
        !admitted.contains("p period"),
        "the period strip was clipped instead of dropped:\n{admitted}"
    );
    assert!(admitted.contains("EQUITY"), "{admitted}");
}

#[test]
fn a_narrow_terminal_still_renders_the_book_without_panicking() {
    let mut client = book();
    client.keys(&[KeyCode::Char('h'), KeyCode::Char('p')]);
    for (w, h) in [
        (40u16, 12u16),
        (20, 8),
        (80, 10),
        (200, 60),
        (1, 1),
        (34, 3),
        (44, 9),
        (86, 9),
    ] {
        let _ = client.frame(w, h);
    }
}

// -- the plan ledger --------------------------------------------------------

/// A book with one checked plan and the approval state a test wants for it.
fn ledger(plan_state: &str, approvals: &str) -> Client {
    book_from(&format!(
        r#"{{"live_portfolio": {{"equity": 1.0, "positions": []}},
             "plans": [{{"plan_id": "9661b0e88b4a669e", "state": "{plan_state}",
                         "created_at": "2026-07-30T18:12:18+00:00",
                         "pre_trade": {{"turnover": 0.42, "n_legs": 2}}}}],
             "approvals": [{approvals}]}}"#
    ))
}

/// The approval the owner serves once a human has authorised the fixture plan.
const APPROVED: &str = r#"{"approval_id": "1a2b3c4d5e6f7081", "plan_id": "9661b0e88b4a669e",
                           "status": "approved", "targets_hash": "c4d5e6f708192a3b",
                           "broker": "simulated_paper"}"#;

#[test]
fn a_plan_card_states_the_plan_its_state_its_turnover_and_when_it_was_proposed() {
    let frame = ledger("checked", "").frame(120, 36);
    let card = line_with(&frame, "9661b0e88b4");
    assert!(card.contains("checked"), "{card}");
    assert!(card.contains("42.0%"), "{card}");
    assert!(card.contains("18:12:18"), "{card}");
}

#[test]
fn a_card_with_no_approval_says_what_it_is_waiting_for() {
    // Not a missing key. "Awaiting approval" is a remedy an operator can act
    // on; an execute hint that does nothing is one they press twice and then
    // distrust.
    let frame = ledger("checked", "").frame(120, 36);
    let card = line_with(&frame, "9661b0e88b4");
    assert!(card.contains("awaiting approval"), "{card}");
    assert!(!card.contains("execute"), "{card}");
}

#[test]
fn a_card_reads_its_reason_off_the_approvals_actual_status() {
    // Every status the owner's transition table can produce is a different
    // sentence about what to do next, and the card says which one it is.
    for (approval, expected) in [
        (
            r#"{"approval_id": "a1", "plan_id": "9661b0e88b4a669e", "status": "pending"}"#,
            "approval pending",
        ),
        (
            r#"{"approval_id": "a1", "plan_id": "9661b0e88b4a669e", "status": "pending",
                "challenge_digest": "0f1e2d3c"}"#,
            "approval pending challenge",
        ),
        (
            r#"{"approval_id": "a1", "plan_id": "9661b0e88b4a669e", "status": "rejected"}"#,
            "approval rejected",
        ),
        (
            r#"{"approval_id": "a1", "plan_id": "9661b0e88b4a669e", "status": "expired"}"#,
            "approval expired",
        ),
    ] {
        let frame = ledger("checked", approval).frame(120, 36);
        let card = line_with(&frame, "9661b0e88b4");
        assert!(card.contains(expected), "wanted {expected:?} in: {card}");
    }
}

#[test]
fn a_plan_the_registry_has_moved_past_cannot_be_fixed_by_approving_it() {
    // "Awaiting approval" on a superseded plan is a remedy that does not exist:
    // making one would not help, and the operator would go and make one.
    let frame = ledger("superseded", "").frame(120, 36);
    let card = line_with(&frame, "9661b0e88b4");
    assert!(card.contains("not a checked plan"), "{card}");
}

#[test]
fn a_booked_plan_says_so_whatever_its_approval_now_reads() {
    // The approval is consumed by the fill, so the card must not fall through
    // to "awaiting approval" the moment the plan actually executes.
    let frame = ledger("reconciled", "").frame(120, 36);
    let card = line_with(&frame, "9661b0e88b4");
    assert!(card.contains("booked"), "{card}");
}

#[test]
fn an_unarmed_window_shows_the_cards_and_offers_no_key() {
    let frame = ledger("checked", APPROVED).frame(120, 36);
    let header = line_with(&frame, "PLANS");
    assert!(header.contains("view-only"), "{header}");
    assert!(!header.contains("x execute"), "{header}");
    // The card is still drawn — a glass window watches the same desk.
    assert!(line_with(&frame, "9661b0e88b4").contains("ready to execute"));
}

#[test]
fn a_ledger_with_nothing_on_it_says_so() {
    let client = book_from(r#"{"live_portfolio": {"equity": 1.0}, "plans": []}"#);
    assert!(
        content(&client.frame(120, 36)).contains("no plan on the ledger"),
        "{}",
        content(&client.frame(120, 36))
    );
}

#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::bus::Wrote;
    use atlas::cmd::Command;
    use atlas::store::Posture;
    use atlas::ui::widgets::toast::{self, Level};
    use crossterm::event::{KeyEvent, KeyModifiers};

    fn armed(plan_state: &str, approvals: &str) -> Client {
        let mut client = super::ledger(plan_state, approvals);
        client.store.posture = Posture::Operator;
        client
    }

    fn press(client: &mut Client, code: KeyCode) -> Option<Command> {
        atlas::ui::shell::on_key(
            KeyEvent::new(code, KeyModifiers::NONE),
            &mut client.store,
            &mut client.views,
        )
    }

    #[test]
    fn an_armed_window_offers_the_execute_key_on_a_covered_plan() {
        let frame = armed("checked", APPROVED).frame(120, 36);
        let header = line_with(&frame, "PLANS");
        assert!(header.contains("x execute"), "{header}");
        assert!(
            line_with(&frame, "9661b0e88b4").contains("ready to execute"),
            "{frame}"
        );
    }

    #[test]
    fn the_execute_key_opens_the_hash_bound_box_and_sends_nothing_yet() {
        let mut client = armed("checked", APPROVED);
        assert_eq!(press(&mut client, KeyCode::Char('x')), None);
        let frame = client.frame(120, 36);
        assert!(frame.contains("EXECUTE PLAN"), "{frame}");
        // The challenge is the tail of the *owner's* targets_hash, shown only
        // here — a replay captured against another plan produces the wrong six.
        assert!(frame.contains("type 192a3b"), "{frame}");
        // And the facts come from the plan's own pre_trade, which is what the
        // owner's gate checks.
        assert!(line_with(&frame, "legs").contains('2'), "{frame}");
    }

    #[test]
    fn only_an_approved_unspent_approval_opens_the_box() {
        // Every other state the owner can serve. A modal armed against a
        // request the gate would refuse teaches an operator that typing the
        // six characters means the desk checked something.
        for approval in [
            "",
            r#"{"approval_id": "a1", "plan_id": "9661b0e88b4a669e", "status": "pending",
                "targets_hash": "c4d5e6f708192a3b"}"#,
            r#"{"approval_id": "a1", "plan_id": "9661b0e88b4a669e", "status": "rejected",
                "targets_hash": "c4d5e6f708192a3b"}"#,
            r#"{"approval_id": "a1", "plan_id": "9661b0e88b4a669e", "status": "approved",
                "targets_hash": "c4d5e6f708192a3b",
                "consumed_at": "2026-07-30T18:20:00+00:00"}"#,
            r#"{"approval_id": "a1", "plan_id": "0000000000000000", "status": "approved",
                "targets_hash": "c4d5e6f708192a3b"}"#,
        ] {
            let mut client = armed("checked", approval);
            assert_eq!(press(&mut client, KeyCode::Char('x')), None);
            assert!(
                !client.frame(120, 36).contains("EXECUTE PLAN"),
                "a box opened for {approval}"
            );
        }
    }

    #[test]
    fn a_confirmed_box_yields_exactly_one_execution_command() {
        let mut client = armed("checked", APPROVED);
        press(&mut client, KeyCode::Char('x'));
        for c in "192a3b".chars() {
            press(&mut client, KeyCode::Char(c));
        }
        let cmd = press(&mut client, KeyCode::Enter).expect("the challenge arms it");
        match cmd {
            Command::Execute(token) => {
                assert_eq!(token.plan_id(), "9661b0e88b4a669e");
                assert_eq!(token.approval_id(), "1a2b3c4d5e6f7081");
                assert_eq!(token.targets_hash(), "c4d5e6f708192a3b");
            }
            other => panic!("{other:?}"),
        }
        // The consent was spent by that answer: the box is gone and a second
        // Enter cannot book the same human decision again.
        assert!(press(&mut client, KeyCode::Enter).is_none());
        assert!(!client.frame(120, 36).contains("EXECUTE PLAN"));
    }

    #[test]
    fn a_booked_fill_is_a_positive_toast_and_brings_the_next_poll_forward() {
        // The wording matches the stream's own `plan_executed` toast on
        // purpose: both arrive, and the queue drops an identical box that is
        // already up, so one fill is one box.
        let executed = toast::for_event(&atlas::bus::AppEvent::Wrote(Wrote::Executed {
            plan_id: "9661b0e88b4a669e".into(),
        }))
        .expect("a booked fill is a moment");
        assert_eq!(executed.level, Level::Info);
        assert!(
            executed.message.contains("9661b0e88b4a669e"),
            "{executed:?}"
        );
        let from_stream = toast::for_event(&atlas::bus::AppEvent::Sse(SseEvent {
            kind: "plan_executed".into(),
            payload: serde_json::json!({"plan_id": "9661b0e88b4a669e"}),
            ts: None,
            id: None,
        }))
        .unwrap();
        assert_eq!(
            executed, from_stream,
            "two boxes for one fill: the queue can only collapse identical toasts"
        );
    }

    #[test]
    fn a_refused_fill_is_as_loud_as_an_error_and_lands_on_the_card() {
        // The rule this whole path exists for. The gate declines with HTTP 200,
        // so a refusal that read like a receipt would tell an operator a trade
        // was booked when the desk declined it.
        let refusal = Wrote::Refused {
            plan_id: "9661b0e88b4a669e".into(),
            blocked_by: "approval".into(),
            reasons: vec!["approval has expired".into()],
        };
        let toast = toast::for_event(&atlas::bus::AppEvent::Wrote(refusal.clone()))
            .expect("a refusal is a moment");
        assert_eq!(toast.level, Level::Alarm, "a refusal is never Info");
        assert!(toast.message.contains("approval"), "{toast:?}");
        assert!(toast.message.contains("has expired"), "{toast:?}");

        // And the card says it, because nothing in the next snapshot will: the
        // gate writes no plan-state change when it refuses.
        let mut client = armed("checked", APPROVED);
        client
            .store
            .apply(atlas::bus::AppEvent::Wrote(refusal), client.now);
        let frame = client.frame(120, 36);
        let card = line_with(&frame, "9661b0e88b4");
        assert!(card.contains("refused: approval"), "{card}");
        assert!(
            !card.contains("ready to execute"),
            "a refused card still offered the key: {card}"
        );
    }

    #[test]
    fn a_refusal_is_drawn_in_the_colour_of_an_error_and_never_of_a_fill() {
        let mut client = armed("checked", APPROVED);
        client.store.apply(
            atlas::bus::AppEvent::Wrote(Wrote::Refused {
                plan_id: "9661b0e88b4a669e".into(),
                blocked_by: "mandate_violation".into(),
                reasons: vec!["position cap breached".into()],
            }),
            client.now,
        );
        let buf = client.buffer(120, 36);
        let style = body_style_of(&buf, "refused: mandate_violation");
        assert_eq!(style.fg, Some(Theme::truecolor().negative));
        assert_ne!(style.fg, Some(Theme::truecolor().positive));
    }

    #[test]
    fn a_write_that_never_landed_is_an_error_and_not_a_refusal() {
        // Three outcomes, three sentences. "The owner did not answer" and "the
        // desk said no" have completely different remedies.
        let failed = toast::for_event(&atlas::bus::AppEvent::Wrote(Wrote::Failed {
            what: "execute 9661b0e88b4a669e".into(),
            said: "the owner did not answer: connection refused".into(),
        }))
        .expect("a failed write is a moment");
        assert_eq!(failed.level, Level::Alarm);
        assert_eq!(failed.title, "write failed");
        assert!(failed.message.contains("connection refused"), "{failed:?}");
    }

    #[test]
    fn a_refused_card_can_still_ask_the_desk_again() {
        // The refusal is the last thing the desk *said*, not a state it is in:
        // `data_revalidation` recovers when the data plane does, and a mandate
        // violation leaves the approval untouched. Gating the key on the held
        // refusal made the card a dead end whose only exit was booking the plan
        // — which the card no longer offered. The owner is the authority on
        // whether it will book, and the red label is what stops a blind retry.
        let mut client = armed("checked", APPROVED);
        client.store.apply(
            atlas::bus::AppEvent::Wrote(Wrote::Refused {
                plan_id: "9661b0e88b4a669e".into(),
                blocked_by: "data_revalidation".into(),
                reasons: vec!["the desk blocked this fill".into()],
            }),
            client.now,
        );
        let frame = client.frame(120, 36);
        assert!(
            line_with(&frame, "9661b0e88b4").contains("refused: data_revalidation"),
            "{frame}"
        );
        assert_eq!(press(&mut client, KeyCode::Char('x')), None);
        assert!(
            client.frame(120, 36).contains("EXECUTE PLAN"),
            "a refused card must still be able to re-ask a governed gate"
        );
    }

    #[test]
    fn a_booked_fill_clears_a_refusal_the_same_plan_carried() {
        // A card must never show a stale refusal beside a fill.
        let mut client = armed("checked", APPROVED);
        for outcome in [
            Wrote::Refused {
                plan_id: "9661b0e88b4a669e".into(),
                blocked_by: "approval".into(),
                reasons: vec!["expired".into()],
            },
            Wrote::Executed {
                plan_id: "9661b0e88b4a669e".into(),
            },
        ] {
            client
                .store
                .apply(atlas::bus::AppEvent::Wrote(outcome), client.now);
        }
        let frame = client.frame(120, 36);
        let card = line_with(&frame, "9661b0e88b4");
        assert!(!card.contains("refused"), "{card}");
    }

    #[test]
    fn the_cursor_walks_the_cards_and_x_acts_on_the_one_it_is_on() {
        let mut client = Client::new({
            let mut store = super::store_from(
                r#"{"live_portfolio": {"equity": 1.0},
                    "plans": [
                      {"plan_id": "aaaaaaaaaaaaaaaa", "state": "superseded"},
                      {"plan_id": "9661b0e88b4a669e", "state": "checked",
                       "pre_trade": {"turnover": 0.42, "n_legs": 2}}],
                    "approvals": [{"approval_id": "1a2b3c4d5e6f7081",
                                   "plan_id": "9661b0e88b4a669e", "status": "approved",
                                   "targets_hash": "c4d5e6f708192a3b"}]}"#,
            );
            store.posture = Posture::Operator;
            store
        });
        client.press(KeyCode::Char('3'));
        // The cursor opens on the newest card, which here cannot execute.
        assert_eq!(press(&mut client, KeyCode::Char('x')), None);
        assert!(!client.frame(120, 36).contains("EXECUTE PLAN"));

        press(&mut client, KeyCode::Char('n'));
        assert_eq!(press(&mut client, KeyCode::Char('x')), None);
        assert!(
            client.frame(120, 36).contains("EXECUTE PLAN"),
            "n did not move the cursor onto the executable card"
        );
    }
}
