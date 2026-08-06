//! ATLAS: the conversation with the desk manager, and the board it reasons from.
//!
//! Three claims run through everything here. The chat renders both voices and
//! keeps their order, wrapped to the pane rather than clipped. The sidebar is
//! the owner's own board summary — champion against baseline with every number
//! the verdict was derived from, and absence named the way the owner names it.
//! And the ask row is the posture's: straight-in typing in an armed window, no
//! row at all in glass, and the workstation's own keys still live over an
//! empty row.
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
}
