//! ATLAS: the conversation with the desk manager, and the board it reasons from.
//!
//! Four claims run through everything here. The chat renders both voices and
//! keeps their order, wrapped to the pane rather than clipped. The sidebar is
//! the owner's own board summary — champion against baseline with every number
//! the verdict was derived from, and absence named the way the owner names it.
//! Over it sits today's proposal list, where the gate's three answers stay three
//! answers and a refusal is rendered rather than dropped. And the ask row is the
//! posture's: straight-in typing in an armed window, no row at all in glass, and
//! the workstation's own keys still live over an empty row.
//!
//! Assertions read through `content`, the columns this view owns. The tape and
//! the pulse rail render words of their own, so a pin on the whole frame could
//! pass on chrome.

mod harness;

use atlas::bus::{AppEvent, Channel};
use atlas::model::Snapshot;
use atlas::store::Store;
use crossterm::event::KeyCode;
use harness::{content, line_with, Client};
use std::time::Instant;

/// The fixture desk, already switched to ATLAS.
fn atlas_view() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('1'));
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

fn atlas_from(json: &str) -> Client {
    let mut client = Client::new(store_from(json));
    client.press(KeyCode::Char('1'));
    client
}

/// A desk whose owner served exactly these proposals and nothing else.
fn acts_from(items: &str) -> Client {
    atlas_from(&format!(
        r#"{{"actionables": {{"trading_date": "2026-06-02", "items": [{items}]}}}}"#
    ))
}

/// The owner's own refusal for a plan-creating template below Propose mode
/// (`check_authority`, qlab/operator/templates.py). Verbatim, because what the
/// panel is for is the sentence the gate wrote.
const REFUSED: &str = "'desk_rebalance_review' creates a paper plan, which requires \
                       Propose mode; current mode is 'research'";

/// The reason the snapshot attaches to an item it did not rule on
/// (`atlas_actionables_snapshot`). Also verbatim.
const NOT_RULED: &str = "the data preconditions were not checked here; POST \
                         /api/atlas/actionables asks the gate for today's verdict";

#[test]
fn the_atlas_view_renders_the_chat_and_the_board_at_120x36() {
    // The unarmed frame, and therefore the one both legs produce: the fixture
    // store's posture is GLASS, in the operator build as well.
    insta::assert_snapshot!(atlas_view().frame(120, 36));
}

#[test]
fn the_conversation_shows_both_voices_in_order_with_their_clocks() {
    let client = atlas_view();
    let frame = client.frame(120, 36);
    let body = content(&frame);
    // The operator's question and the desk's answer, as their own voices.
    let question = line_with(&frame, "why is the book flat");
    assert!(question.contains("YOU"), "{question}");
    assert!(question.contains("15:31:02"), "{question}");
    let answer = line_with(&frame, "The regime read");
    assert!(answer.contains("ATLAS"), "{answer}");
    // In the owner's order: the question is above its answer.
    let q_at = body.lines().position(|l| l.contains("why is the book"));
    let a_at = body.lines().position(|l| l.contains("The regime read"));
    assert!(q_at < a_at, "the conversation lost its order:\n{body}");
}

#[test]
fn a_long_answer_wraps_to_the_pane_rather_than_clipping() {
    // The fixture's second answer runs to ~250 characters; a chat pane ~80
    // wide has to spend several lines on it, and the last clause has to be
    // readable rather than cut off the right edge.
    let client = atlas_view();
    let body = content(&client.frame(120, 36));
    assert!(
        body.contains("re-propose"),
        "the tail of a long answer is missing — clipped, not wrapped:\n{body}"
    );
}

#[test]
fn a_failed_answer_carries_the_owners_error_under_it() {
    let client = atlas_view();
    let frame = client.frame(120, 36);
    line_with(&frame, "reasoner backend");
    line_with(&frame, "ollama timed out after 45s");
}

#[test]
fn an_empty_conversation_says_so_rather_than_drawing_a_blank_pane() {
    let client = atlas_from(r#"{"atlas_chat": []}"#);
    let body = content(&client.frame(120, 36));
    assert!(body.contains("nothing has been asked"), "{body}");
}

#[test]
fn scrolling_up_reaches_the_oldest_message_and_walls_there() {
    let mut client = atlas_view();
    // One frame first: the scroll clamps against what the last frame drew.
    client.frame(120, 36);
    for _ in 0..200 {
        client.press(KeyCode::Up);
    }
    let top = content(&client.frame(120, 36));
    assert!(
        top.contains("why is the book flat"),
        "the oldest message is unreachable:\n{top}"
    );
    // And back down: the bottom is the newest answer again.
    for _ in 0..200 {
        client.press(KeyCode::Down);
    }
    let bottom = content(&client.frame(120, 36));
    assert!(bottom.contains("ollama timed out"), "{bottom}");
}

// -- the predictor board ----------------------------------------------------

#[test]
fn the_board_reads_champion_against_baseline_with_the_q_badge() {
    let client = atlas_view();
    let frame = client.frame(120, 36);
    let champion = line_with(&frame, "krr:quantum_gram");
    assert!(champion.contains("CHAMPION"), "{champion}");
    // The quantum badge, on the augmented lane and not the classical one.
    assert!(champion.contains(" q"), "{champion}");
    let baseline = line_with(&frame, "ridge:none");
    assert!(baseline.contains("BASELINE"), "{baseline}");
    assert!(!baseline.contains(" q"), "{baseline}");
    // The numbers the verdict was derived from, not the verdict alone.
    line_with(&frame, "0.1412");
    line_with(&frame, "±0.0631");
    line_with(&frame, "Δ+0.0187");
    line_with(&frame, "wins 4");
    line_with(&frame, "t 1.94");
    line_with(&frame, "board 2d old");
    // The selection null's own verdict: `usable` alone cannot carry the claim.
    line_with(&frame, "edge not established");
}

#[test]
fn a_board_that_never_ran_names_the_tool_that_runs_one() {
    let client = atlas_from(r#"{"predictors": {"status": "never_ran"}}"#);
    let frame = client.frame(120, 36);
    line_with(&frame, "board never ran");
    line_with(&frame, "research.predictor_board");
}

#[test]
fn an_unreadable_board_says_so_with_its_run_id() {
    let client =
        atlas_from(r#"{"predictors": {"status": "unreadable", "run_id": "deadbeefdeadbeef"}}"#);
    let frame = client.frame(120, 36);
    line_with(&frame, "unreadable");
    line_with(&frame, "deadbeefdead");
}

#[test]
fn a_narrow_pane_drops_the_board_whole_and_keeps_the_chat() {
    // A board clipped to half its metrics misreads as a different board; a
    // narrower chat is the same conversation in shorter lines.
    let client = atlas_view();
    let frame = client.frame(96, 36);
    let body = content(&frame);
    assert!(!body.contains("CHAMPION"), "{body}");
    assert!(body.contains("why is the book flat"), "{body}");
}

// -- what the desk would do -------------------------------------------------

#[test]
fn the_sidebar_lists_what_the_desk_would_do_and_why_it_would_not() {
    let client = acts_from(&format!(
        r#"{{"template_id": "regime_review", "purpose": "Re-read the regime panel.",
             "creates_plan": false, "needs_coordinator": true,
             "startable": true, "reason": null,
             "task_id": "t1", "task_status": "queued"}},
           {{"template_id": "desk_rebalance_review", "purpose": "Propose a rebalance.",
             "creates_plan": true, "needs_coordinator": true,
             "startable": false, "reason": "{REFUSED}",
             "task_id": null, "task_status": "queued"}}"#
    ));
    let body = content(&client.frame(120, 36));
    assert!(body.contains("regime_review"), "{body}");
    // The refusal is the product: a template silently dropped teaches nothing.
    assert!(body.contains("desk_rebalance_review"), "{body}");
    assert!(body.contains("requires Propose mode"), "{body}");
}

#[test]
fn an_owner_that_serves_no_actionables_draws_no_panel() {
    // Absence is not an error, and not an empty box either — and an owner that
    // serves the block with nothing in it is the same nothing to draw.
    for json in [
        r#"{"actionables": {"items": []}}"#,
        r#"{"actionables": null}"#,
        "{}",
    ] {
        let body = content(&atlas_from(json).frame(120, 36));
        assert!(!body.contains("WOULD DO"), "{json}\n{body}");
    }
}

#[test]
fn an_item_the_snapshot_did_not_rule_on_reads_as_neither_offered_nor_refused() {
    // `startable` is tri-state and `true` is unreachable on this surface: the
    // snapshot reports what it checked, and the verdict lives at the POST. A
    // client that drew `null` as either answer would be inventing one.
    let client = acts_from(&format!(
        r#"{{"template_id": "regime_review", "purpose": "Re-read the regime panel.",
             "startable": null, "reason": "{NOT_RULED}",
             "task_id": "t1", "task_status": "queued"}}"#
    ));
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "regime_review");
    assert!(row.contains('?'), "{row}");
    assert!(!row.contains('✓') && !row.contains('✗'), "{row}");
    let body = content(&frame);
    // What it would do is the item's sentence; where the verdict lives is said
    // once, for the marker, rather than four wrapped rows per item.
    assert!(body.contains("Re-read the regime panel"), "{body}");
    assert!(body.contains("not checked on this surface"), "{body}");
    // A list with nothing refused says so as a count of proposals, not as a
    // refusal count of zero.
    assert!(body.contains("1 proposed"), "{body}");
    // And the three verdicts are three tones, not two.
    let buf = client.buffer(120, 36);
    let t = atlas::theme::theme();
    let pending = harness::body_style_of(&buf, "regime_review").fg;
    assert_eq!(
        pending,
        Some(t.text_secondary),
        "pending took another verdict's tone"
    );
    assert_ne!(pending, Some(t.text_primary));
    assert_ne!(pending, Some(t.text_dim));
}

#[test]
fn a_spent_proposal_carries_the_status_that_says_so() {
    // The list keeps the whole trading day, running and completed items with
    // it, so both surfaces agree about what was asked. `task_status` is the
    // only thing that tells a live proposal from a spent one.
    let client = acts_from(
        r#"{"template_id": "desk_brief", "purpose": "Summarize the desk.",
            "startable": false, "reason": "today's proposal for desk_brief is already running",
            "task_id": "t9", "task_status": "running"}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "desk_brief");
    assert!(row.contains("running"), "{row}");
}

#[test]
fn a_busy_day_keeps_the_board_and_counts_what_it_could_not_draw() {
    // The list only grows over a day. The board is what the sidebar is for, so
    // the panel is capped — and what does not fit is counted, never dropped in
    // silence.
    let items = (0..9)
        .map(|i| {
            format!(
                r#"{{"template_id": "template_{i}", "purpose": "Purpose {i}.",
                     "startable": false, "reason": "{REFUSED}",
                     "task_id": "t{i}", "task_status": "queued"}}"#
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let body = content(&acts_from(&items).frame(120, 36));
    assert!(
        body.contains("PREDICTOR BOARD"),
        "the board was pushed off:\n{body}"
    );
    assert!(
        body.contains("more"),
        "the unshown items were dropped in silence:\n{body}"
    );
    // Every item here is refused, so there is no `?` on screen and no row is
    // spent explaining one. The chip counts them all.
    assert!(
        !body.contains("not checked on this surface"),
        "a legend for a marker nothing drew:\n{body}"
    );
    assert!(body.contains("9 of 9 refused"), "{body}");
}

#[test]
fn a_sentence_the_owner_did_not_write_is_cut_rather_than_costing_its_item_a_row() {
    // Nothing on the wire is guaranteed to be the owner's: a proxy in front of
    // the desk answers with a page, and `purpose` is where it would land.
    // Unbounded, that item is taller than the whole panel, so the cap drops it
    // whole and the operator never learns the proposal exists. Bounded, it
    // renders — cut, and marked as cut.
    let flood = "lorem ".repeat(200);
    let client = acts_from(&format!(
        r#"{{"template_id": "regime_review", "purpose": "{flood}",
             "startable": null, "reason": null, "task_id": "t1", "task_status": "queued"}},
           {{"template_id": "desk_rebalance_review", "purpose": "Propose a rebalance.",
             "startable": false, "reason": "{REFUSED}", "task_id": "t2", "task_status": "queued"}}"#
    ));
    let body = content(&client.frame(120, 36));
    assert!(
        body.contains("regime_review"),
        "the flooded item was dropped:\n{body}"
    );
    assert!(body.contains("…"), "the cut was not marked:\n{body}");
    // And what it crowded out is counted rather than vanishing.
    assert!(body.contains("+1 more, unshown"), "{body}");
}

// -- the ask row ------------------------------------------------------------

#[test]
fn an_unarmed_window_draws_no_ask_row_and_says_why() {
    let client = atlas_view();
    let body = content(&client.frame(120, 36));
    assert!(!body.contains("ask ›"), "{body}");
    assert!(body.contains("read-only in this posture"), "{body}");
}

#[test]
fn the_workstations_keys_survive_an_empty_unfocused_row() {
    // The tradeoff `typing` documents, pinned end to end: over an empty row
    // the digits still navigate and `r` still refreshes — in both builds,
    // because the fixture posture is GLASS and the armed leg re-checks below.
    use atlas::cmd::Command;
    use crossterm::event::{KeyEvent, KeyModifiers};
    let mut client = atlas_view();
    assert_eq!(
        atlas::ui::shell::on_key(
            KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE),
            &mut client.store,
            &mut client.views,
        ),
        Some(Command::Refresh)
    );
    client.press(KeyCode::Char('3'));
    assert_eq!(client.store.nav.view, atlas::store::ViewId::Markets);
}

// -- the armed window -------------------------------------------------------

#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::cmd::Command;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers};

    fn armed() -> Client {
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        let mut client = Client::new(store);
        client.press(KeyCode::Char('1'));
        client
    }

    fn press(client: &mut Client, code: KeyCode) -> Option<Command> {
        atlas::ui::shell::on_key(
            KeyEvent::new(code, KeyModifiers::NONE),
            &mut client.store,
            &mut client.views,
        )
    }

    fn typed(client: &mut Client, text: &str) {
        for c in text.chars() {
            press(client, KeyCode::Char(c));
        }
    }

    #[test]
    fn typing_goes_straight_in_and_enter_asks_the_desk() {
        let mut client = armed();
        let body = content(&client.frame(120, 36));
        assert!(body.contains("Ask Atlas"), "{body}");

        // No mode key: the first printable character is the question's first
        // character, and from there the shell's own keys type too.
        typed(&mut client, "why so flat");
        let body = content(&client.frame(120, 36));
        assert!(body.contains("why so flat"), "{body}");

        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::Message("why so flat".into()))
        );
        // Sent is done: the row is empty and the keyboard is given back.
        assert!(!client.views.typing(client.store.nav.view));
    }

    #[test]
    fn once_the_row_holds_text_the_shell_keys_type_rather_than_navigate() {
        let mut client = armed();
        typed(&mut client, "wq3r");
        assert_eq!(client.store.nav.view, atlas::store::ViewId::Atlas);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("wq3r"), "{body}");
        // Esc clears and unfocuses; the next digit navigates again.
        press(&mut client, KeyCode::Esc);
        press(&mut client, KeyCode::Char('3'));
        assert_eq!(client.store.nav.view, atlas::store::ViewId::Markets);
    }

    #[test]
    fn the_focus_key_claims_an_empty_row_for_a_question_starting_with_a_shell_key() {
        let mut client = armed();
        // `i` focuses rather than types: the row was empty and unfocused.
        assert_eq!(press(&mut client, KeyCode::Char('i')), None);
        assert!(client.views.typing(client.store.nav.view));
        // Now `q` is the question's first letter, not quit.
        assert_eq!(press(&mut client, KeyCode::Char('q')), None);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("ask › q"), "{body}");
        // And a second `i` is a letter now.
        press(&mut client, KeyCode::Char('i'));
        assert!(content(&client.frame(120, 36)).contains("qi"));
    }

    #[test]
    fn an_empty_question_is_not_sent() {
        let mut client = armed();
        press(&mut client, KeyCode::Char('i'));
        assert_eq!(press(&mut client, KeyCode::Enter), None);
    }

    #[test]
    fn the_armed_atlas_view_renders_its_ask_row_at_120x36() {
        insta::assert_snapshot!(armed().frame(120, 36));
    }

    #[test]
    fn the_would_do_list_is_the_same_list_in_an_armed_window() {
        // Reading what the desk would do is not a write, so the panel is the
        // posture's business only in that a glass window cannot act on it. The
        // read-only leg of this claim is every test above; this is the other.
        let mut store = super::store_from(&format!(
            r#"{{"actionables": {{"items": [
                 {{"template_id": "desk_rebalance_review", "purpose": "Propose a rebalance.",
                   "startable": false, "reason": "{}", "task_id": "t2",
                   "task_status": "queued"}}]}}}}"#,
            super::REFUSED
        ));
        store.posture = Posture::Operator;
        let mut client = Client::new(store);
        client.press(KeyCode::Char('1'));
        let body = content(&client.frame(120, 36));
        assert!(body.contains("WOULD DO"), "{body}");
        assert!(body.contains("requires Propose mode"), "{body}");
    }
}

// -- the desk's current proposal, mirrored ----------------------------------

/// The fixture desk with a live proposal on it, on ATLAS.
fn with_proposal() -> Client {
    let mut store = harness::fixture_store();
    harness::with_proposal(&mut store, harness::PROPOSAL);
    let mut client = Client::new(store);
    client.press(KeyCode::Char('1'));
    client
}

/// The mirrored card, in a binary that could book but has not been armed.
///
/// One golden per leg for the reason BOOK's is: the last row of the card
/// states this window's posture in an armed build and is absent altogether in
/// a monitoring one, and a shared snapshot could only be blessed for one.
#[cfg(feature = "operator")]
#[test]
fn the_sidebar_mirrors_the_proposal_card_at_120x36() {
    insta::assert_snapshot!(with_proposal().frame(120, 36));
}

#[cfg(not(feature = "operator"))]
mod glass {
    use super::*;

    #[test]
    fn the_sidebar_mirrors_the_card_with_no_booking_row_at_120x36() {
        insta::assert_snapshot!(with_proposal().frame(120, 36));
    }
}

#[test]
fn the_proposal_replaces_the_your_call_list_rather_than_sitting_beside_it() {
    // It *is* the your-call item — one question, stated with the numbers it is
    // about instead of as a word to type. Two lists under one header would be
    // two accounts of what the desk is waiting for.
    let client = with_proposal();
    let frame = client.frame(120, 36);
    let body = content(&frame);
    assert_eq!(
        body.matches("YOUR CALL").count(),
        1,
        "the sidebar drew the slot twice:\n{body}"
    );
    line_with(&frame, "b92a58fa5c1");
    // The fixture desk has a checked plan whose word the old list drew. It is
    // gone while there is a proposal, and the card is what stands in its place.
    assert!(!body.contains("/execute 9661b0e8"), "{body}");
}

#[test]
fn the_mirrored_card_is_the_same_card_book_draws() {
    // Same verdict row, same binding, same numbers — one widget, so a rule
    // that changes changes on both panes at once.
    let frame = with_proposal().frame(120, 36);
    // The binding survives the sidebar's 32 cells; the authority behind it is
    // what a narrow pane drops, which is the right way round — a verdict with
    // no binding could be about any allocation at all.
    let referee = line_with(&frame, "referee PASS");
    assert!(referee.contains("5a6978"), "{referee}");
    line_with(&frame, "turnover 42.0%");
}

#[cfg(feature = "operator")]
mod booking {
    use super::*;
    use atlas::cmd::Command;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseButton, MouseEventKind};

    fn armed() -> Client {
        let mut store = harness::fixture_store();
        harness::with_proposal(&mut store, harness::PROPOSAL);
        store.posture = Posture::Operator;
        let mut client = Client::new(store);
        client.press(KeyCode::Char('1'));
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
    fn b_opens_the_same_box_here_as_it_does_on_book() {
        let mut client = armed();
        assert_eq!(press(&mut client, KeyCode::Char('b')), None);
        let frame = client.frame(120, 36);
        assert!(frame.contains("BOOK THE PROPOSAL"), "{frame}");
        assert!(
            line_with(&frame, "confirming").contains("5a6978"),
            "{frame}"
        );
    }

    #[test]
    fn a_question_being_typed_keeps_its_own_letter() {
        // The corner this pane already lives with: the ask row claims every
        // printable key once it holds text or has been focused, and the
        // booking key is claimed only while the row is idle. A question that
        // starts with it is focused first.
        let mut client = armed();
        press(&mut client, KeyCode::Char('i'));
        for c in "buy or sell".chars() {
            press(&mut client, KeyCode::Char(c));
        }
        let body = content(&client.frame(120, 36));
        assert!(body.contains("buy or sell"), "{body}");
        assert!(!body.contains("BOOK THE PROPOSAL"), "{body}");
    }

    #[test]
    fn a_word_the_short_sidebar_clipped_off_is_not_clickable() {
        // The sidebar renders `lines.into_iter().take(inner.height)`, so a card
        // whose rows run past the pane is drawn clipped — and the rect that
        // says where the booking word went is computed from the *unclipped*
        // index. Left unclamped it lands below the pane, over the footer, and a
        // click there opens the box for a word nobody can see. The same rule
        // the acts panel's `drew()` already documents: absence is not
        // permission, and neither is a word off the screen.
        let mut client = armed();
        for height in 8..40u16 {
            let frame = client.frame(120, height);
            if frame.contains("book ↵") {
                continue;
            }
            for row in 0..height {
                atlas::ui::shell::on_mouse(
                    crossterm::event::MouseEvent {
                        kind: MouseEventKind::Down(MouseButton::Left),
                        column: 90,
                        row,
                        modifiers: KeyModifiers::NONE,
                    },
                    &mut client.store,
                    &mut client.views,
                );
                assert!(
                    !client.frame(120, height).contains("BOOK THE PROPOSAL"),
                    "at {height} rows a click at row {row} booked off a clipped word"
                );
            }
        }
    }

    #[test]
    fn a_click_on_the_cards_word_opens_the_box_here_too() {
        let mut client = armed();
        let frame = client.frame(120, 36);
        let row = frame
            .lines()
            .position(|line| line.contains("book ↵"))
            .expect("the sidebar drew no book word") as u16;
        let column = frame
            .lines()
            .nth(row as usize)
            .unwrap()
            .find("book ↵")
            .unwrap() as u16;
        client.mouse(MouseEventKind::Down(MouseButton::Left), column, row);
        assert!(
            client.frame(120, 36).contains("BOOK THE PROPOSAL"),
            "the click on `book` opened no box"
        );
    }
}

// -- the universe changes the scout asks for --------------------------------

/// The contender scout's widening, waiting on a human. It binds no plan, so it
/// is not answered by the proposal card and cannot be gated behind one.
const UNIVERSE: &str = r#"{"approvals": [
     {"approval_id": "uc11223344556677", "kind": "universe_change", "status": "pending",
      "plan_id": null, "expires_at": null,
      "summary": {"ticker": "NVDA", "memo_decision_id": "memo1234abcd"}}]}"#;

/// A desk with both a live proposal and a pending widening.
fn proposal_and_universe() -> Client {
    let mut store = store_from(UNIVERSE);
    harness::with_proposal(&mut store, harness::PROPOSAL);
    let mut client = Client::new(store);
    client.press(KeyCode::Char('1'));
    client
}

#[test]
fn a_universe_change_is_pointed_at_even_while_a_proposal_is_up() {
    // The your-call list is gated on there being no proposal, because the card
    // *is* the your-call item for a plan. A universe change is a second
    // question the card does not answer — gated the same way it would sit in
    // the queue with nothing on any pane saying so.
    let client = proposal_and_universe();
    let body = content(&client.frame(120, 36));
    assert!(
        body.contains("universe change: +NVDA"),
        "the widening is invisible beside a proposal:\n{body}"
    );
    assert!(body.contains("/approve uc112233"), "{body}");
    assert!(body.contains("a on AUDIT"), "{body}");
    // Under the header the card is already under: two your-call slots would be
    // two accounts of what the desk is waiting for.
    assert_eq!(body.matches("YOUR CALL").count(), 1, "{body}");
}

#[test]
fn a_universe_change_is_a_your_call_line_with_no_proposal_too() {
    let mut client = Client::new(store_from(UNIVERSE));
    client.press(KeyCode::Char('1'));
    let body = content(&client.frame(120, 36));
    assert!(body.contains("universe change: +NVDA"), "{body}");
    assert_eq!(body.matches("YOUR CALL").count(), 1, "{body}");
}

/// One golden per leg for the reason the proposal card's is: the card's last
/// row states this window's posture in an armed build and is absent in a
/// monitoring one.
#[cfg(feature = "operator")]
#[test]
fn the_sidebar_points_at_a_universe_change_beside_the_proposal_at_120x36() {
    insta::assert_snapshot!(proposal_and_universe().frame(120, 36));
}

#[cfg(not(feature = "operator"))]
mod glass_universe {
    use super::*;

    #[test]
    fn the_sidebar_points_at_a_universe_change_with_no_booking_row_at_120x36() {
        insta::assert_snapshot!(proposal_and_universe().frame(120, 36));
    }
}

/// The chat's own path to the same box.
///
/// `/approve <id>` and AUDIT's `a` are two ways to decide one request, and the
/// pointer above tells an operator to use either. They call one builder, so a
/// box that said different things about the same row is not something this
/// client can be asked to draw.
#[cfg(feature = "operator")]
#[test]
fn the_chat_approve_word_opens_the_universe_box_the_audit_key_opens() {
    let mut store = store_from(UNIVERSE);
    store.posture = atlas::store::Posture::Operator;
    let mut client = Client::new(store);
    client.press(KeyCode::Char('1'));
    client.press(KeyCode::Char('/'));
    for c in "approve uc11223344556677".chars() {
        client.press(KeyCode::Char(c));
    }
    client.press(KeyCode::Enter);
    let frame = client.frame(120, 36);
    assert!(frame.contains("APPROVE UNIVERSE CHANGE"), "{frame}");
    assert!(frame.contains("NVDA"), "{frame}");
    assert!(frame.contains("Approving admits NVDA"), "{frame}");
    assert!(
        !frame.contains("APPROVE APPROVAL"),
        "the chat drew the plan box for a universe change: {frame}"
    );
}

/// A desk with nothing proposed and two questions of different kinds waiting:
/// one plan approval, one widening. Both draw a `/word` in the your-call list,
/// which is what the clamp below is about.
const YOUR_CALL: &str = r#"{"approvals": [
     {"approval_id": "uc11223344556677", "kind": "universe_change", "status": "pending",
      "plan_id": null, "expires_at": null,
      "summary": {"ticker": "NVDA", "memo_decision_id": "memo1234abcd"}},
     {"approval_id": "ap99887766554433", "kind": "plan", "status": "pending",
      "plan_id": "5a6978aabbccddee", "expires_at": null, "summary": {}}]}"#;

/// The rule `book_word` is clamped by, on the three rects the your-call list
/// registers: the sidebar is drawn with a `take(inner.height)`, so a rect
/// computed from the unclipped line list lands *under* the pane — over the
/// footer — and a click there runs a word the frame never drew.
///
/// Asserted on the line the click would run rather than on a box, because a
/// clicked `/word` leaves this view as a `RunLine` for the runtime to resolve —
/// and written as "a click may only run a word that was on screen" rather than
/// "nothing runs at short heights", because both halves matter: the affordance
/// has to keep working wherever the list did fit.
///
/// Ungated: the rects are registered in both builds, and so is the bug.
#[test]
fn a_your_call_word_the_short_sidebar_clipped_off_is_not_clickable() {
    use atlas::cmd::Command;
    use crossterm::event::{MouseButton, MouseEventKind};

    let mut client = Client::new(store_from(YOUR_CALL));
    client.press(KeyCode::Char('1'));
    let mut ran = 0;
    for height in 8..40u16 {
        let frame = client.frame(120, height);
        for row in 0..height {
            let acted = atlas::ui::shell::on_mouse(
                crossterm::event::MouseEvent {
                    kind: MouseEventKind::Down(MouseButton::Left),
                    // Inside the sidebar column at this width, where the
                    // rects are; the pulse rail to its right registers none.
                    column: 60,
                    row,
                    modifiers: crossterm::event::KeyModifiers::NONE,
                },
                &mut client.store,
                &mut client.views,
            );
            let Some(Command::RunLine(line)) = acted else {
                continue;
            };
            assert!(
                frame.contains(&line),
                "at {height} rows a click on row {row} would run `{line}`, which the frame \
                 had clipped:\n{frame}"
            );
            ran += 1;
        }
    }
    // And the search really ran: a click path that produced nothing anywhere
    // would satisfy the assertion above while proving nothing at all.
    assert!(ran > 0, "no click ever ran a word");
}
