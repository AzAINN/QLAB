//! SETTINGS: the facts this desk is configured by, and nothing that changes them.
//!
//! Every claim here is one an operator would otherwise have to read out of
//! `mandate.yaml`, `.mcp.json` and a shell prompt. The view's whole job is that
//! they agree with the owner, so the pins are on the owner's own words reaching
//! the frame — and on absence staying absent, because a constraint rendered as
//! `0.0%` is a mandate that forbids holding anything.
//!
//! Assertions read through `content`, the columns this view owns: the tape and
//! the pulse rail render words of their own, and the status line now carries a
//! desk-mode chip that would answer several of these questions for free.

mod harness;

use atlas::bus::{AppEvent, Channel};
use atlas::model::Snapshot;
use atlas::store::Store;
use atlas::theme::Theme;
use crossterm::event::KeyCode;
use harness::{body_style_of, content, line_with, Client};
use std::time::Instant;

/// The fixture desk, already switched to SETTINGS.
fn settings() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('7'));
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

fn settings_from(json: &str) -> Client {
    let mut client = Client::new(store_from(json));
    client.press(KeyCode::Char('7'));
    client
}

#[test]
fn the_settings_view_renders_its_cards_at_120x36() {
    insta::assert_snapshot!(settings().frame(120, 36));
}

#[test]
fn the_desk_card_states_which_desk_this_is_in_the_owners_own_words() {
    // The label is the owner's, never composed here from the pair: `/mode`
    // sends two words and the owner decides what they are called together.
    let frame = settings().frame(120, 36);
    assert!(line_with(&frame, "mode").contains("SYNTHETIC"), "{frame}");
    assert!(line_with(&frame, "data").contains("synthetic"), "{frame}");
    assert!(line_with(&frame, "book").contains("simulated"), "{frame}");
}

#[test]
fn a_book_this_desk_cannot_log_into_is_named_on_the_card_and_not_only_flagged() {
    // The status line's chip can say *that* the book is unreachable in one
    // amber word. This is the pane that has to say *why*, because the owner's
    // description is the only thing that names the missing credential.
    let client = settings_from(
        r#"{"desk_mode": {"data": "live", "book": "alpaca", "label": "LIVE · ALPACA BOOK",
             "credentials": "no ALPACA_API_KEY_ID in the environment",
             "credentials_ok": false}}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "ALPACA_API_KEY_ID");
    assert!(row.contains("login"), "{row}");
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "no ALPACA_API_KEY_ID").fg,
        Some(Theme::truecolor().warning),
        "a book that cannot be reached is not a quiet fact"
    );

    // The same broken login under the simulated book is not a warning: there is
    // no order this desk could fail to place.
    let sim = settings_from(
        r#"{"desk_mode": {"data": "live", "book": "simulated", "label": "LIVE · SIM BOOK",
             "credentials": "no ALPACA_API_KEY_ID in the environment",
             "credentials_ok": false}}"#,
    );
    assert_eq!(
        body_style_of(&sim.buffer(120, 36), "no ALPACA_API_KEY_ID").fg,
        Some(Theme::truecolor().text_secondary)
    );
}

#[test]
fn the_policy_card_carries_the_four_constraints_every_solve_is_held_to() {
    let frame = settings().frame(120, 36);
    assert!(line_with(&frame, "policy").contains("hrp"), "{frame}");
    assert!(line_with(&frame, "long only").contains("yes"), "{frame}");
    assert!(line_with(&frame, "budget").contains("100.0%"), "{frame}");
    // One row for the pair, because a floor without its ceiling is half a
    // mandate — and 0% is a real floor, not an absent one.
    let weights = line_with(&frame, "per asset");
    assert!(weights.contains("0.0%"), "{weights}");
    assert!(weights.contains("40.0%"), "{weights}");
}

#[test]
fn a_constraint_the_owner_did_not_send_is_absent_rather_than_zero() {
    // A `max_weight` defaulted to `0.0` renders a mandate that forbids holding
    // anything — a statement about this desk that nobody made.
    let client = settings_from(r#"{"policy": {"id": "hrp", "constraints": {}}}"#);
    let frame = client.frame(120, 36);
    assert!(line_with(&frame, "long only").contains("--"), "{frame}");
    assert!(line_with(&frame, "budget").contains("--"), "{frame}");
    assert!(line_with(&frame, "per asset").contains("--"), "{frame}");
    assert!(
        !content(&frame).contains("0.0%"),
        "an unsent constraint rendered as a number: {frame}"
    );
}

#[test]
fn the_system_card_states_the_provenance_and_the_authority_it_runs_under() {
    let frame = settings().frame(120, 36);
    assert!(
        line_with(&frame, "authority").contains("propose_only"),
        "{frame}"
    );
    let provenance = line_with(&frame, "provenance");
    assert!(provenance.contains("synthetic"), "{provenance}");
    assert!(provenance.contains("0 d"), "{provenance}");
    assert!(
        line_with(&frame, "mcp").contains("qlab-operator"),
        "{frame}"
    );
}

#[test]
fn a_broken_mcp_config_is_a_different_fact_from_no_config() {
    // The owner distinguishes them deliberately — a file that exists and does
    // not parse sent an operator to re-add a server entry that was already
    // there — so a client that collapsed both into "not configured" would undo
    // that.
    let client = settings_from(
        r#"{"system": {"mcp_configured": false, "mcp_servers": [],
             "mcp_config_error": "JSONDecodeError: Expecting ',' delimiter"}}"#,
    );
    let frame = client.frame(120, 36);
    assert!(
        line_with(&frame, "mcp").contains("JSONDecodeError"),
        "{frame}"
    );

    let absent = settings_from(r#"{"system": {"mcp_configured": false, "mcp_servers": []}}"#);
    let frame = absent.frame(120, 36);
    assert!(line_with(&frame, "mcp").contains("none"), "{frame}");
}

#[test]
fn the_universe_is_the_symbols_this_desk_is_actually_watching() {
    // The mandate's whitelist is not in the snapshot; what the desk polled is,
    // and that is what the row is labelled with. A client that called it "the
    // mandate universe" would be asserting a configuration it cannot see.
    let frame = settings().frame(120, 36);
    let body = content(&frame);
    assert!(body.contains("ACWI"), "{body}");
    assert!(body.contains("BNDW"), "{body}");
    // Five polled assets plus the two names the book holds outside them —
    // `BNDW` and the fixture's dust holding. A row that counted only the polled
    // universe would leave a held position off the list of what this desk is
    // watching.
    assert!(body.contains("DUST"), "{body}");
    assert!(line_with(&frame, "watching").contains("7"), "{frame}");
}

#[test]
fn the_theme_card_names_the_palette_this_terminal_actually_got() {
    // The fallback is not cosmetic: on a 256-colour terminal the depth ramp is
    // a different palette, and an operator comparing two screenshots has to be
    // able to tell which one they are looking at.
    let frame = settings().frame(120, 36);
    assert!(line_with(&frame, "palette").contains("obsidian"), "{frame}");
}

#[test]
fn a_window_that_cannot_switch_the_desk_does_not_offer_the_way_to() {
    // Operator affordances are absent, not greyed — the rule the whole client
    // is built on. `/mode` does not exist in a glass window, so naming it here
    // would be an instruction with nothing behind it.
    let frame = settings().frame(120, 36);
    assert!(!content(&frame).contains("/mode"), "{frame}");
    assert!(content(&frame).contains("read-only"), "{frame}");
}

#[test]
fn an_owner_that_sent_no_configuration_says_so_rather_than_drawing_empty_cards() {
    // "Nothing configured" and "this pane is broken" must not look the same.
    let client = settings_from(r#"{"portfolio": {"equity": 1.0}}"#);
    let body = content(&client.frame(120, 36));
    assert!(body.contains("the owner sent no desk mode"), "{body}");
    assert!(body.contains("the owner sent no policy"), "{body}");
    assert!(body.contains("the owner sent no system"), "{body}");
    assert!(body.contains("no universe"), "{body}");
}

#[test]
fn a_pane_too_narrow_for_two_columns_says_what_it_would_take() {
    // The rows here are label/value pairs that do not compress: a provenance
    // clipped to `synthe` is a source an operator has to guess at.
    let client = settings();
    let narrow = content(&client.frame(72, 36));
    assert!(narrow.contains("SETTINGS needs"), "{narrow}");
}

#[test]
fn settings_claims_no_key_at_all() {
    // Every fact on this pane is read-only and nothing on it scrolls, so a key
    // pressed here has to fall through to whatever claims it next. A view that
    // swallowed one would read as a hung client.
    let mut client = settings();
    let frame = client.frame(120, 36);
    for code in [
        KeyCode::Down,
        KeyCode::Up,
        KeyCode::Left,
        KeyCode::Right,
        KeyCode::Enter,
        KeyCode::Char('s'),
    ] {
        client.press(code);
        assert_eq!(client.frame(120, 36), frame, "{code:?} changed SETTINGS");
    }
}
