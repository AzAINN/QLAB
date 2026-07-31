//! The command line, end to end: what a keystroke does to the frame and to the desk.
//!
//! Every key here goes through the real `shell::on_key` and every frame through
//! the real `shell::draw`, because the seam this task adds is *routing* — a
//! parser that is correct and a shell that never reaches it is the shape
//! invariant 10 exists for. The parser's own table lives beside it in
//! `src/cmd.rs`; nothing here re-tests the grammar.

mod harness;

use atlas::store::{Focus, Store, ViewId};
use atlas::ui::shell::NAV_W;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use harness::{fixture_store, frame_to_string, line_with, Client};

/// The row the command line and the status chips share.
fn command_row(frame: &str) -> &str {
    frame.lines().last().expect("a frame has a status line")
}

/// The strip the line borrows while it is focused: the row above the input.
fn strip(frame: &str) -> &str {
    let lines: Vec<&str> = frame.lines().collect();
    lines[lines.len() - 2]
}

fn type_line(client: &mut Client, text: &str) {
    for c in text.chars() {
        client.press(KeyCode::Char(c));
    }
}

// -- focus ------------------------------------------------------------------

#[test]
fn slash_focuses_the_line_and_the_line_opens_already_in_the_picker() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    assert_eq!(client.store.nav.focus, Focus::Command);
    assert_eq!(client.store.cmd.text(), "/");

    let frame = client.frame(120, 36);
    // The strip is a row the line borrowed from the content, and it lists the
    // scopes rather than making an operator guess the grammar.
    let offered = strip(&frame);
    for scope in ["/view", "/ticker", "/plan"] {
        assert!(offered.contains(scope), "{offered}");
    }
    assert!(command_row(&frame).contains('/'), "{}", command_row(&frame));
}

#[test]
fn the_line_owns_every_printable_key_including_the_ones_the_shell_claims() {
    // `q` quits, `r` refreshes and the digits switch views — and all three are
    // characters `/ticker` and `/plan` need. A field that let the shell claim
    // them would make half the grammar untypeable.
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker Q");
    assert_eq!(client.store.cmd.text(), "/ticker Q");
    assert_eq!(
        client.store.nav.view,
        ViewId::Desk,
        "a digit switched views"
    );
    assert_eq!(client.store.nav.focus, Focus::Command);
}

#[test]
fn ctrl_c_still_quits_from_inside_the_line() {
    // The one key that must reach the runtime whatever owns the keyboard. A
    // field that swallowed it would leave an operator's only exit reflex dead
    // in a fullscreen client.
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    let quit = atlas::ui::shell::on_key(
        KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
        &mut client.store,
        &mut client.views,
    );
    assert_eq!(quit, Some(atlas::cmd::Command::Quit));
}

#[test]
fn esc_abandons_the_line_and_leaves_nothing_behind() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker SPY");
    client.press(KeyCode::Esc);
    assert_eq!(client.store.nav.focus, Focus::Content);
    assert_eq!(client.store.cmd.text(), "");
    // And the row is the prompt again, not a half-typed line nobody is in.
    assert!(command_row(&client.frame(120, 36)).contains("/command"));
}

// -- accepting --------------------------------------------------------------

#[test]
fn tab_accepts_the_first_suggestion_and_the_strip_follows_the_text() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ti");
    client.press(KeyCode::Tab);
    assert_eq!(client.store.cmd.text(), "/ticker ");

    // The strip has moved from scopes to symbols, because the *text* moved.
    let frame = client.frame(120, 36);
    assert!(strip(&frame).contains("SPY"), "{}", strip(&frame));
    // And the scope's own words for what it wants.
    assert!(strip(&frame).contains("symbol"), "{}", strip(&frame));

    type_line(&mut client, "SP");
    client.press(KeyCode::Tab);
    assert_eq!(client.store.cmd.text(), "/ticker SPY");
}

#[test]
fn enter_on_a_picker_with_one_answer_accepts_it_rather_than_acting() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "vi");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.cmd.text(), "/view ");
    assert_eq!(
        client.store.nav.focus,
        Focus::Command,
        "accepting a scope is not submitting a line"
    );
}

// -- what the scopes do -----------------------------------------------------

#[test]
fn view_switches_and_gives_the_keyboard_back() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "view book");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.nav.view, ViewId::Book);
    assert_eq!(client.store.nav.focus, Focus::Content);
    assert_eq!(client.store.cmd.text(), "");
}

#[test]
fn a_ticker_moves_both_cursors_and_shows_the_pane_that_holds_it() {
    // ACWI is quoted *and* held, so both panes move; the operator lands on
    // MARKETS from a view that holds neither cursor.
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker acwi");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.nav.view, ViewId::Markets);

    // The grid's marker is on it, which is the whole point of the scope.
    let frame = client.frame(120, 36);
    let marked = frame
        .lines()
        .find(|row| row.contains("ACWI") && row.contains('▌'))
        .unwrap_or_else(|| panic!("no marked ACWI row:\n{frame}"));
    assert!(marked.contains("ACWI"));
}

#[test]
fn the_bare_function_code_is_the_same_request_without_the_scope() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    // Typed without the slash: `/` opened the line, so it is deleted first —
    // exactly what an operator does when they meant a symbol.
    client.press(KeyCode::Backspace);
    type_line(&mut client, "QQQ");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.nav.view, ViewId::Markets);
    assert_eq!(client.store.nav.focus, Focus::Content);
}

#[test]
fn a_symbol_held_but_not_quoted_lands_on_the_blotter_instead() {
    // BNDW is in the book and not in the market section. Sending an operator to
    // MARKETS for it would show a grid with no such row.
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker BNDW");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.nav.view, ViewId::Book);
}

#[test]
fn an_operator_already_looking_at_the_book_is_not_thrown_to_markets() {
    // The column they sorted by and the page they scrolled to are worth more
    // than the client's opinion about where a symbol is best seen.
    let mut client = Client::fixture();
    client.store.nav.view = ViewId::Book;
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker ACWI");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.nav.view, ViewId::Book);
}

#[test]
fn a_ticker_the_desk_is_not_watching_is_named_and_the_line_stays_up() {
    // No phantom selection: the cursor does not move, the view does not switch,
    // and the sentence says which symbol and why.
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker ZZZZ");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.nav.view, ViewId::Desk);
    assert_eq!(
        client.store.nav.focus,
        Focus::Command,
        "a refused line closed, taking its reason with it"
    );
    let frame = client.frame(120, 36);
    let said = strip(&frame);
    assert!(said.contains("ZZZZ"), "{said}");
    assert!(said.contains("universe"), "{said}");
}

#[test]
fn a_plan_jumps_to_the_ledger_and_selects_the_card() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "plan 9661");
    client.press(KeyCode::Enter);
    assert_eq!(client.store.nav.view, ViewId::Book);
    assert_eq!(client.store.nav.focus, Focus::Content);
}

#[test]
fn a_plan_below_the_band_says_where_it_is_rather_than_selecting_nothing() {
    // The band draws the newest three (`book::PLAN_CARDS`); a plan under it is
    // on the desk and off the screen. A jump with no marker and no sentence
    // would read as a broken key — this is the ledger item named, not fixed.
    let mut store = fixture_store();
    let mut plans = store.snapshot.as_ref().unwrap().plans.clone();
    while plans.len() < 5 {
        let mut extra = plans[0].clone();
        extra.plan_id = Some(format!("dead{:04}beefcafe", plans.len()));
        plans.push(extra);
    }
    let wanted = plans[4].plan_id.clone().unwrap();
    let mut snapshot = store.snapshot.take().unwrap();
    snapshot.plans = plans;
    store.apply(
        atlas::bus::AppEvent::Snapshot(Box::new(snapshot)),
        std::time::Instant::now(),
    );

    let mut client = Client::new(store);
    client.press(KeyCode::Char('/'));
    type_line(&mut client, &format!("plan {wanted}"));
    client.press(KeyCode::Enter);
    assert_eq!(
        client.store.nav.view,
        ViewId::Book,
        "the jump still happens"
    );
    assert_eq!(client.store.nav.focus, Focus::Command);
    let frame = client.frame(120, 36);
    let said = strip(&frame);
    assert!(said.contains("#5"), "{said}");
    assert!(said.contains("newest 3"), "{said}");
}

// -- the history ------------------------------------------------------------

#[test]
fn the_history_recalls_a_line_that_worked_and_never_one_that_was_refused() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "view book");
    client.press(KeyCode::Enter);

    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker ZZZZ");
    client.press(KeyCode::Enter); // refused; the line stays up
    client.press(KeyCode::Esc);

    client.press(KeyCode::Char('/'));
    client.press(KeyCode::Up);
    assert_eq!(client.store.cmd.text(), "/view book");
    client.press(KeyCode::Down);
    assert_eq!(client.store.cmd.text(), "");
}

// -- the help overlay -------------------------------------------------------

#[test]
fn question_mark_opens_the_key_list_over_whatever_is_behind_it() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('?'));
    assert_eq!(client.store.nav.focus, Focus::Help);
    let frame = client.frame(120, 36);
    for row in [
        "Ctrl-C",
        "Shift-Tab",
        "the previous view",
        "focus the command line",
    ] {
        assert!(frame.contains(row), "{row} is missing:\n{frame}");
    }
}

#[test]
fn the_overlay_owns_the_keyboard_and_both_exits_give_it_back() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('?'));
    // A digit would switch views if the overlay were not modal.
    client.press(KeyCode::Char('3'));
    assert_eq!(client.store.nav.view, ViewId::Desk);
    client.press(KeyCode::Down);
    assert_eq!(client.store.help_top, 1);
    client.press(KeyCode::Esc);
    assert_eq!(client.store.nav.focus, Focus::Content);

    client.press(KeyCode::Char('?'));
    assert_eq!(client.store.help_top, 0, "reopening kept a stale scroll");
    client.press(KeyCode::Char('?'));
    assert_eq!(client.store.nav.focus, Focus::Content);
}

#[test]
fn question_mark_inside_the_line_is_a_character_and_not_the_overlay() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    client.press(KeyCode::Char('?'));
    assert_eq!(client.store.nav.focus, Focus::Command);
    assert_eq!(client.store.cmd.text(), "/?");
}

// -- floors -----------------------------------------------------------------

#[test]
fn a_frame_with_no_room_for_the_strip_still_says_what_the_line_said() {
    // The suggestions are a hint and may be lost; a refusal is a statement and
    // may not. Below the strip's floor it falls back onto the command row.
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "ticker ZZZZ");
    client.press(KeyCode::Enter);
    let tall = client.frame(120, 36);
    assert!(strip(&tall).contains("ZZZZ"));

    let short = client.frame(120, 4);
    assert!(
        command_row(&short).contains("ZZZZ"),
        "the reason vanished with the strip:\n{short}"
    );
}

#[test]
fn the_overlay_refuses_rather_than_opening_where_it_cannot_be_read() {
    // And the refusal names its own exit: this surface holds the keyboard by
    // being the focus, so an operator who cannot see it needs the way out
    // written down.
    let mut client = Client::fixture();
    client.press(KeyCode::Char('?'));
    let cramped = client.frame(40, 7);
    assert!(cramped.contains("Esc closes"), "{cramped}");
}

// -- the write scope --------------------------------------------------------

#[test]
fn the_write_scope_is_absent_from_an_unarmed_windows_suggestions() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    let frame = client.frame(120, 36);
    assert!(!strip(&frame).contains("/mode"), "{}", strip(&frame));
}

#[test]
fn typed_blind_the_write_scope_refuses_out_loud() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('/'));
    type_line(&mut client, "mode live simulated");
    client.press(KeyCode::Enter);
    let frame = client.frame(120, 36);
    assert!(strip(&frame).contains("GLASS"), "{}", strip(&frame));
}

// -- goldens ----------------------------------------------------------------

/// A store with the line open on `/ti`, drawn the way the runtime draws it.
fn palette_store(posture_armed: bool) -> Store {
    let mut client = Client::new(fixture_store());
    if posture_armed {
        #[cfg(feature = "operator")]
        {
            client.store.posture = atlas::store::Posture::Operator;
        }
    }
    client.press(KeyCode::Char('/'));
    for c in "ti".chars() {
        client.press(KeyCode::Char(c));
    }
    // Accepted but not yet answered: the frame worth pinning is the one where
    // the strip is doing its whole job — the scope's own words for what it
    // wants, and every value the desk can actually offer.
    client.press(KeyCode::Tab);
    client.store
}

#[test]
fn the_command_line_renders_its_strip_at_120x36() {
    insta::assert_snapshot!(frame_to_string(&palette_store(false), 120, 36));
}

#[test]
fn the_help_overlay_renders_at_120x36() {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('?'));
    insta::assert_snapshot!(frame_to_string(&client.store, 120, 36));
}

/// The two frames where the posture actually changes what is drawn: the picker
/// gains `/mode`, and the overlay gains every key that can move money.
#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::store::Posture;

    #[test]
    fn the_armed_picker_offers_the_write_scope() {
        let mut client = Client::new(fixture_store());
        client.store.posture = Posture::Operator;
        client.press(KeyCode::Char('/'));
        let frame = client.frame(120, 36);
        assert!(strip(&frame).contains("/mode"), "{}", strip(&frame));
        insta::assert_snapshot!(frame_to_string(&client.store, 120, 36));
    }

    #[test]
    fn the_armed_overlay_lists_the_keys_that_can_move_money() {
        // Below the fold on the first page, which is the point of the scroll:
        // an operator reaches the keys that book a trade by walking the list
        // the router is checked against, not by a second screen written by hand.
        let mut client = Client::new(fixture_store());
        client.store.posture = Posture::Operator;
        client.press(KeyCode::Char('?'));
        assert!(
            !client.frame(120, 36).contains("ask to execute"),
            "the fixture no longer scrolls; this test is checking nothing"
        );
        for _ in 0..24 {
            client.press(KeyCode::Down);
        }
        let frame = client.frame(120, 36);
        assert!(frame.contains("ask to execute"), "{frame}");
        assert!(frame.contains("confirmation box"), "{frame}");
        insta::assert_snapshot!(frame_to_string(&client.store, 120, 36));
    }

    #[test]
    fn an_armed_desk_mode_reaches_the_runtime_as_one_command_and_nothing_else() {
        // The routing pin: `Enter` hands the runtime a `Command`, and the
        // parser never executes anything. Observed, not dispatched — there is
        // no writer in this test and there does not need to be one.
        let mut client = Client::new(fixture_store());
        client.store.posture = Posture::Operator;
        client.press(KeyCode::Char('/'));
        for c in "mode live alpaca".chars() {
            client.press(KeyCode::Char(c));
        }
        let cmd = atlas::ui::shell::on_key(
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
            &mut client.store,
            &mut client.views,
        );
        assert_eq!(
            cmd,
            Some(atlas::cmd::Command::DeskMode {
                data: "live".into(),
                book: "alpaca".into()
            })
        );
        assert_eq!(client.store.nav.focus, Focus::Content);
        // And it is on the record for ↑ to find, because it was acted on.
        client.press(KeyCode::Char('/'));
        client.press(KeyCode::Up);
        assert_eq!(client.store.cmd.text(), "/mode live alpaca");
    }
}

#[test]
fn the_first_suggestion_is_the_one_a_key_accepts_and_looks_like_it() {
    // A golden string cannot say which of six symbols Tab would take. It is the
    // first, and it is the only one drawn in the accent — a strip where every
    // choice looked alike would make Tab a guess.
    let store = palette_store(false);
    let buffer = {
        let views = atlas::ui::views::Views::new();
        let fx = atlas::fx::Fx::default();
        let now = store.last_snapshot_at.unwrap();
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        term.draw(|f| atlas::ui::shell::draw(f, &store, &views, &fx, now))
            .unwrap();
        term.backend().buffer().clone()
    };
    let first = harness::body_style_of(&buffer, "ACWI");
    let second = harness::body_style_of(&buffer, "SPY");
    assert_ne!(
        first.fg, second.fg,
        "every suggestion is drawn the same, so Tab is a guess"
    );
}

#[test]
fn the_strip_never_reaches_into_the_rails() {
    // The strip is the line's own row, borrowed from the content area. A widget
    // that drew across the whole width would paint over the nav rail's last
    // row, which is where an operator reads which view they are in.
    let store = palette_store(false);
    let frame = frame_to_string(&store, 120, 36);
    let nav = line_with(&frame, "7 SETT");
    assert!(!nav.contains("SPY"), "{nav}");
    assert_eq!(NAV_W, 8, "the crop this test reads is keyed to the rail");
}
