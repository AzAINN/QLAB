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

use atlas::bus::{AppEvent, Channel};
use atlas::model::Snapshot;
use atlas::store::Store;
use atlas::theme::Theme;
use crossterm::event::KeyCode;
use harness::{body_style_of, content, line_with, Client};
use ratatui::style::Modifier;
use std::time::Instant;

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
fn the_rest_of_the_book_names_the_tasks_that_fill_it() {
    // "Nothing here yet" and "this broke" have to be distinguishable at a
    // glance for as long as this branch is half-built.
    let content = content(&book().frame(120, 36));
    assert!(content.contains("Task 12"), "{content}");
    assert!(content.contains("Task 13"), "{content}");
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
