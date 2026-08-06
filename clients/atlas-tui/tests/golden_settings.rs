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
    client.press(KeyCode::Char('8'));
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

fn settings_from(json: &str) -> Client {
    let mut client = Client::new(store_from(json));
    client.press(KeyCode::Char('8'));
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
fn every_card_answers_for_itself_and_no_card_is_marked_as_listening() {
    // The pane-level footer retired: one line under the desk card used to speak
    // for six cards, and once each card had its own keys that line was either
    // wrong about five of them or silent about all six. Each card now carries
    // its own, on the rule its block already reserves — a footer competing for
    // *rows* would be the first thing a short column dropped, which is the
    // failure the MODELS stamp was moved to the top over.
    let frame = settings().frame(120, 36);
    let body = content(&frame);
    // Six cards, six statements, and the desk's is the specific one: a glass
    // window's whole answer is that it points the desk nowhere.
    assert!(
        body.contains("read-only — this window cannot switch the desk"),
        "{body}"
    );
    assert_eq!(
        body.matches("read-only").count(),
        6,
        "one footer per card, and this pane draws six:\n{body}"
    );
    // And nothing is tinted. Focus is where a key would land, so a window with
    // no keys marks no card — a highlight that never moves under the arrows
    // reads as a hung client, which is why AUDIT's arrows decline rather than
    // swallow.
    // Read off the title rather than the bar: every panel on the workstation
    // opens with an accent `▌`, so the bar cannot say which card is listening.
    // Uppercase titles the nav rail does not also spell, or the rail's own
    // `DESK` would answer for the card's.
    let buf = settings().buffer(120, 36);
    for card in ["POLICY", "SYSTEM", "MODELS"] {
        assert_eq!(
            body_style_of(&buf, card).fg,
            Some(Theme::truecolor().text_primary),
            "{card} is marked as listening in a window that hears nothing"
        );
    }
}

// -- the models card --------------------------------------------------------
//
// The clock these read against is passed in, never read here: `Store::wall` is
// data the runtime stamps, so an age on a golden is a fact about the fixture
// rather than about how long the suite took to reach this line.

/// Twelve seconds after `llm.probed_at` in the fixture and in every inline
/// block below.
const NOW: i64 = 1_785_696_869;
const PROBED: &str = "2026-08-02T18:54:17.856581+00:00";

/// SETTINGS over an owner that answered about its models, at a fixed clock.
fn models_from(llm: &str) -> Client {
    let mut store = store_from(&format!(r#"{{"llm": {llm}}}"#));
    store.wall = Some(NOW);
    let mut client = Client::new(store);
    client.press(KeyCode::Char('8'));
    client
}

#[test]
fn the_models_card_names_the_backend_each_surface_actually_runs_on() {
    let client = models_from(&format!(
        r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "claude", "available": true, "reason": "claude CLI on PATH"}},
                              {{"name": "ollama", "available": true,
                                "reason": "ollama at 127.0.0.1:11434, 1 model pulled"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    let frame = client.frame(120, 36);
    let body = content(&frame);
    assert!(body.contains("MODELS"), "{body}");
    assert!(
        line_with(&frame, "reasoner").contains("ollama · granite3.3:8b"),
        "{frame}"
    );
    // The flag, not the choice: a model named for a reasoner nobody switched on
    // is a mind the desk is not using.
    assert!(line_with(&frame, "judgment").contains("on"), "{frame}");
    // The A3 carry — a claude surface never offers a model name, because the
    // tier map owns it and the picker would be offering a choice that does
    // nothing.
    assert!(
        line_with(&frame, "workforce").contains("claude · tiers decide"),
        "{frame}"
    );
    // And never a model name beside it: on the claude path the tier map owns
    // the model, so a name here would be an offer that changes nothing.
    assert!(
        !line_with(&frame, "workforce").contains("inherit"),
        "{frame}"
    );
    // A backend that can serve is not a quiet fact either.
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "ollama · granite3.3:8b").fg,
        Some(Theme::truecolor().positive)
    );
    // The A2 carry: the block is the LAST reading, so the card says how old it
    // is rather than presenting it as live.
    assert!(line_with(&frame, "probed").contains("12s ago"), "{frame}");
}

#[test]
fn a_backend_that_cannot_serve_is_toned_and_carries_the_owners_own_reason() {
    // The owner's sentence verbatim (`OllamaBackend._absent_reason`): it names
    // the remedy, and this client owns none of that wording.
    let client = models_from(&format!(
        r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "claude", "available": true, "reason": "claude CLI on PATH"}},
                              {{"name": "ollama", "available": false,
                                "reason": "ollama is not running at 127.0.0.1:11434 — start it with `ollama serve`"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    let frame = client.frame(120, 36);
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "ollama · granite3.3:8b").fg,
        Some(Theme::truecolor().warning),
        "a mind the desk cannot reach is not a quiet fact"
    );
    assert!(content(&frame).contains("ollama serve"), "{frame}");
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "ollama is not running").fg,
        Some(Theme::truecolor().text_dim),
        "the reason explains the tone; it does not compete with it"
    );
}

#[test]
fn an_owner_string_on_this_card_is_bounded_rather_than_left_to_run() {
    // Nothing on the wire is guaranteed to be the owner's — a proxy in front of
    // the desk answers with pages of its own — and the C2 rule is now uniform:
    // every foreign string a surface renders is collapsed and cut at the
    // boundary rather than pushing the rows under it off the card.
    let client = models_from(&format!(
        r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "ollama", "available": false,
                                "reason": "{}"}}],
             "probed_at": "{PROBED}"}}"#,
        "verbose ".repeat(400)
    ));
    let body = content(&client.frame(120, 36));
    assert!(body.contains('…'), "{body}");
    // The card ends where it always did — the theme card under it is still on
    // the frame, which is the property an unbounded reason would take away.
    assert!(body.contains("palette"), "{body}");
}

#[test]
fn a_desk_that_has_not_probed_says_so_rather_than_claiming_a_fresh_reading() {
    // A2's first state: the config is served from the moment the owner starts,
    // and `availability` stays null until something asks the backends. "Nothing
    // has been probed" and "everything is absent" are different facts.
    let client = models_from(
        r#"{"reasoner": {"backend": "claude", "model": "inherit"},
            "workforce": {"backend": "claude", "model": "inherit"},
            "reasoner_enabled": false,
            "availability": null, "probed_at": null}"#,
    );
    let frame = client.frame(120, 36);
    assert!(line_with(&frame, "probed").contains("not yet"), "{frame}");
    assert!(line_with(&frame, "judgment").contains("off"), "{frame}");
}

#[test]
fn a_short_column_never_shows_a_tone_the_stamp_has_not_dated() {
    // The pane's floor is twelve rows; the right column needs twenty-three, so
    // between those two the card is handed fewer rows than it asked for and a
    // `Paragraph` stops drawing partway down. What it used to stop drawing was
    // the stamp, leaving availability tones over a reading nobody could date —
    // the exact "reads as live" misreading `probed_at` exists to prevent.
    for height in [26, 24, 20] {
        let client = models_from(&format!(
            r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
                 "workforce": {{"backend": "claude", "model": "inherit"}},
                 "reasoner_enabled": true,
                 "availability": [{{"name": "ollama", "available": false,
                                    "reason": "ollama is not running at 127.0.0.1:11434 — start it with `ollama serve`"}}],
                 "probed_at": "{PROBED}"}}"#
        ));
        let body = content(&client.frame(120, height));
        let toned = body.contains("ollama · granite3.3:8b") || body.contains("claude · tiers");
        assert!(
            !toned || body.contains("probed"),
            "at {height} rows a tone rendered with no stamp to date it:\n{body}"
        );
        // And it is a refusal that names what it would take, not a blank. All
        // three of these heights are short, so this is what *exercises* the
        // card's own floor rather than merely permitting it.
        assert!(
            body.contains("the models card needs"),
            "at {height} rows the card drew neither the reading nor a refusal:\n{body}"
        );
        assert!(
            !toned,
            "at {height} rows a partial card still drew a tone:\n{body}"
        );
    }
    // The height where it stops refusing and draws the reading whole, so the
    // floor is pinned from both sides rather than only from below.
    let client = models_from(&format!(
        r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "ollama", "available": false,
                                "reason": "ollama is not running at 127.0.0.1:11434 — start it with `ollama serve`"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    let body = content(&client.frame(120, 30));
    assert!(body.contains("probed"), "{body}");
    assert!(body.contains("ollama · granite3.3:8b"), "{body}");
    assert!(!body.contains("the models card needs"), "{body}");
}

#[test]
fn a_reason_that_will_not_fit_whole_is_counted_rather_than_cut_in_half() {
    // The remedy is the last third of the owner's longest sentence, so half of
    // one is a fix an operator cannot run. `views::desk::fit` makes the same
    // trade for the same reason.
    let client = models_from(&format!(
        r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "ollama", "available": false,
                                "reason": "ollama is running at 127.0.0.1:11434 but no models are pulled — pull one with `ollama pull granite3.3:8b`"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    // Whole at the baseline: the remedy is on the frame.
    assert!(
        content(&client.frame(120, 36)).contains("ollama pull"),
        "{}",
        client.frame(120, 36)
    );
    // Counted where it will not fit. Unconditional and without an `||`: the
    // first version of this test allowed either outcome at a height where only
    // one is possible, and deleting the marker altogether still passed it — the
    // reason then vanished silently, which is the failure the count exists to
    // prevent.
    let short = content(&client.frame(120, 30));
    assert!(short.contains("▾ 1 more"), "{short}");
    // And no half of it survives beside the count.
    assert!(!short.contains("ollama is running at"), "{short}");
    // The four rows it qualifies are untouched — the reason is the section that
    // gives way, never the reading.
    assert!(short.contains("probed"), "{short}");
    assert!(short.contains("ollama · granite3.3:8b"), "{short}");

    // Two down backends, which is what makes the marker's *reservation* load
    // bearing rather than a no-op. With one reason the row is always affordable
    // after the fact; with two, a first reason costing exactly the slack would
    // be drawn, leave nothing, and push the count onto a row the card does not
    // have — one sentence on screen and no sign the other exists. Reachable the
    // moment the two surfaces sit on different daemons and both are down.
    let both = models_from(&format!(
        r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "ollama", "available": false,
                                "reason": "ollama is running at 127.0.0.1:11434 but no models are pulled — pull one with `ollama pull granite3.3:8b`"}},
                              {{"name": "claude", "available": false,
                                "reason": "the claude CLI exited 1: Invalid API key · Please run /login — the desk cannot start a workforce session"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    // At the baseline, where a single reason of this size fits whole.
    let body = content(&both.frame(120, 36));
    assert!(body.contains("▾ 2 more"), "{body}");
    // And neither half-sentence survives beside the count.
    assert!(!body.contains("ollama is running at"), "{body}");
    assert!(!body.contains("the claude CLI exited"), "{body}");
    assert!(body.contains("probed"), "{body}");
}

#[test]
fn a_real_model_id_never_wraps_out_of_its_column() {
    // The reviewer's exact id. A per-token bound left this at 41 cells against
    // a 24-cell column, wrapping onto an unindented row and spending the slack
    // the reasons need.
    let client = models_from(&format!(
        r#"{{"reasoner": {{"backend": "claude", "model": "claude-opus-4-5-20260101"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "claude", "available": true, "reason": "claude CLI on PATH"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "reasoner");
    // The word that says the name changes nothing survives the cut.
    assert!(row.contains("ignored"), "{frame}");
    assert!(
        row.contains('…'),
        "a silent cut reads as the whole id: {frame}"
    );
    // Nothing spilled onto a row of its own: the row under it is still the one
    // that dates the reading's neighbours.
    assert!(line_with(&frame, "judgment").contains("on"), "{frame}");
    assert!(content(&frame).contains("palette"), "{frame}");
}

#[test]
fn the_models_card_says_nothing_the_owner_did_not_send() {
    let client = settings_from(r#"{"portfolio": {"equity": 1.0}}"#);
    let body = content(&client.frame(120, 36));
    assert!(body.contains("the owner sent no model routing"), "{body}");
}

#[test]
fn a_stripped_store_renders_every_card_absent_and_never_a_zero() {
    // The all-`None` frame, goldened. A constraint that came back as `0.0%`, a
    // reasoner that came back `off` because nobody said, or an age of `0s` over
    // a stamp that was never written are all statements the owner never made.
    insta::assert_snapshot!(settings_from(
        r#"{"llm": {}, "policy": {"constraints": {}}, "system": {}, "desk_mode": {}}"#
    )
    .frame(120, 36));
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
fn an_unarmed_window_claims_no_key_and_offers_no_login_form() {
    // Every fact on this pane is read-only to a glass window, and nothing on it
    // scrolls, so a key pressed here has to fall through to whatever claims it
    // next. A view that swallowed one would read as a hung client — and the two
    // keys an armed window has are *absent* here rather than refused, which is
    // the rule the whole client is built on.
    let mut client = settings();
    let frame = client.frame(120, 36);
    assert!(!content(&frame).contains("types a login"), "{frame}");
    for code in [
        KeyCode::Down,
        KeyCode::Up,
        KeyCode::Left,
        KeyCode::Right,
        KeyCode::Enter,
        KeyCode::Char('s'),
        KeyCode::Char('a'),
        KeyCode::Char('t'),
    ] {
        client.press(code);
        assert_eq!(client.frame(120, 36), frame, "{code:?} changed SETTINGS");
    }
}

// -- the armed window -------------------------------------------------------

#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::bus::Wrote;
    use atlas::cmd::Command;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers};

    /// The owner's consent refusal, in its own words (`AlpacaConsentRequired`).
    const SAID: &str = "the active alpaca profile holds a browser login; storing a key pair \
                        discards its access token and refresh token, and they cannot be \
                        recovered without logging in again";

    fn armed() -> Client {
        let mut store = super::harness::fixture_store();
        store.posture = Posture::Operator;
        let mut client = Client::new(store);
        client.press(KeyCode::Char('8'));
        // One frame before any key, exactly as the runtime draws one before it
        // reads its first event: the pane publishes its area there, and the
        // form's floor is read off it.
        client.frame(120, 36);
        client
    }

    /// One keystroke through the real routing, returning what the runtime was
    /// asked to do — which is the half the view cannot do itself.
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

    /// The form with a pair in it, ready for Enter.
    fn filled() -> Client {
        let mut client = armed();
        press(&mut client, KeyCode::Char('a'));
        typed(&mut client, "PKTEST0123456789");
        press(&mut client, KeyCode::Tab);
        typed(&mut client, "s3cretABCDEFGHIJKLMNOPQRSTUV");
        client
    }

    #[test]
    fn the_alpaca_login_form_renders_masked_at_120x36() {
        insta::assert_snapshot!(filled().frame(120, 36));
    }

    #[test]
    fn the_consent_question_renders_the_owners_sentence_at_120x36() {
        let mut client = filled();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::LoginNeedsConsent {
            said: SAID.to_string(),
        });
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn both_fields_are_masked_and_only_a_complete_pair_is_sent() {
        let mut client = armed();
        assert_eq!(press(&mut client, KeyCode::Char('a')), None, "no command");
        // An empty form asks for nothing. The owner refuses it anyway, with a
        // sentence about a field this form can see is blank.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(content(&client.frame(120, 36)).contains("both the key and the secret"));

        typed(&mut client, "PKTEST0123456789");
        // Half a pair is still not a login.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        press(&mut client, KeyCode::Tab);
        // With a trailing space, which is what a paste carries. The field is
        // masked, so the operator cannot see it — and the owner's shape check
        // would refuse the pair for a character nobody could look at.
        typed(&mut client, "s3cretABCDEFGHIJKLMNOPQRSTUV ");

        let frame = client.frame(120, 36);
        // Neither value is anywhere on screen, and both fields are dots — the
        // key id is the less sensitive half, and masking only one of them would
        // make the other look like the secret.
        assert!(!frame.contains("PKTEST"), "{frame}");
        assert!(!frame.contains("s3cret"), "{frame}");
        assert!(line_with(&frame, " key ").contains("••••"), "{frame}");
        assert!(line_with(&frame, " secret ").contains("••••"), "{frame}");

        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::AlpacaLogin {
                key: atlas::secret::Secret::new("PKTEST0123456789".into()),
                secret: atlas::secret::Secret::new("s3cretABCDEFGHIJKLMNOPQRSTUV".into()),
                // Never on a login nobody has been asked about.
                replace: false,
            })
        );
        // One request at a time: a second Enter while the first is in flight
        // would put two credential writes on the owner's audit bus for one
        // decision.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(content(&client.frame(120, 36)).contains("asking the owner"));

        // Esc closes and clears, so nothing is held for a form the operator
        // walked away from.
        press(&mut client, KeyCode::Esc);
        let closed = client.frame(120, 36);
        assert!(!closed.contains("••••"), "{closed}");
        press(&mut client, KeyCode::Char('a'));
        let reopened = client.frame(120, 36);
        assert!(!reopened.contains("••••"), "a cleared field came back");
    }

    #[test]
    fn the_form_owns_every_key_including_the_ones_the_shell_claims() {
        // `q`, `r` and the digits are the workstation's, and a secret
        // containing them would otherwise refresh the desk, jump to BOOK and
        // quit before it was half typed.
        let mut client = armed();
        press(&mut client, KeyCode::Char('a'));
        assert_eq!(press(&mut client, KeyCode::Char('q')), None, "q quit");
        assert_eq!(press(&mut client, KeyCode::Char('r')), None, "r refreshed");
        assert_eq!(press(&mut client, KeyCode::Char('3')), None);
        assert_eq!(
            client.store.nav.view,
            atlas::store::ViewId::Settings,
            "a digit walked out of a field being typed into"
        );
        assert!(line_with(&client.frame(120, 36), " key ").contains("•••"));

        // And the one key that must still work: raw mode disables ISIG, so a
        // field that swallowed it would leave the operator's only exit reflex
        // dead in a fullscreen client.
        assert_eq!(
            atlas::ui::shell::on_key(
                KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
                &mut client.store,
                &mut client.views,
            ),
            Some(Command::Quit)
        );
    }

    #[test]
    fn a_login_the_owner_will_not_replace_is_a_question_before_it_is_a_write() {
        // Esc first: abandoning the question must write nothing at all.
        let mut client = filled();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::LoginNeedsConsent {
            said: SAID.to_string(),
        });
        let asking = client.frame(120, 36);
        assert!(asking.contains("refresh token"), "{asking}");
        assert!(asking.contains("CONFIRM"), "{asking}");
        press(&mut client, KeyCode::Esc);
        let gone = client.frame(120, 36);
        assert!(!gone.contains("refresh token"), "{gone}");

        // Then the answer. A wrong word is not the word, and an unarmed Enter
        // leaves the question up rather than closing it — a human who mistyped
        // has to see that they did.
        let mut client = filled();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::LoginNeedsConsent {
            said: SAID.to_string(),
        });
        typed(&mut client, "CONFIRX");
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(client.frame(120, 36).contains("refresh token"));
        press(&mut client, KeyCode::Backspace);
        typed(&mut client, "M");
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::AlpacaLogin {
                key: atlas::secret::Secret::new("PKTEST0123456789".into()),
                secret: atlas::secret::Secret::new("s3cretABCDEFGHIJKLMNOPQRSTUV".into()),
                replace: true,
            }),
            "the consented re-send carries the same pair and the flag"
        );
        // The consent is spent by the send that carried it: retyping the word
        // into a question that has already been answered cannot authorise a
        // second overwrite.
        typed(&mut client, "CONFIRM");
        assert_eq!(press(&mut client, KeyCode::Enter), None);
    }

    #[test]
    fn a_refusal_stays_in_the_form_and_an_answer_nobody_is_waiting_for_is_ignored() {
        let mut client = filled();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::LoginRefused {
            said: "that does not look like an alpaca key id".to_string(),
        });
        let refused = client.frame(120, 36);
        // Rendered under the fields rather than only in a toast, and the pair
        // is still there to be corrected: the operator fixes one character.
        assert!(refused.contains("alpaca key id"), "{refused}");
        assert!(line_with(&refused, " key ").contains("••••"), "{refused}");

        // The guard: an outcome that belongs to nothing this form sent must not
        // reopen it as a question over what is being typed now. The form is
        // back in editing, so a stray consent answer is ignored.
        client.views.wrote(&Wrote::LoginNeedsConsent {
            said: SAID.to_string(),
        });
        let quiet = client.frame(120, 36);
        assert!(!quiet.contains("refresh token"), "{quiet}");

        // And a request that never landed un-sticks the form rather than
        // leaving it refusing Enter forever after one timeout.
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::Failed {
            what: "store the alpaca login".into(),
            said: "the owner did not answer".into(),
        });
        assert!(client.frame(120, 36).contains("did not answer"));
        assert!(press(&mut client, KeyCode::Enter).is_some(), "still stuck");
    }

    #[test]
    fn a_pane_too_short_for_the_form_refuses_to_open_it_rather_than_arming_it() {
        // The state that must not exist: a box with room for a header and
        // nothing else — no fields, no footer — while the keyboard is still
        // held and Enter still stores a credential.
        let mut client = armed();
        let short = client.frame(120, 15);
        assert!(!short.contains("ALPACA LOGIN"), "{short}");
        press(&mut client, KeyCode::Char('a'));
        let refused = client.frame(120, 15);
        assert!(
            content(&refused).contains("the alpaca login form needs"),
            "{}",
            content(&refused)
        );
        assert!(!refused.contains("ALPACA LOGIN"), "the box opened anyway");
        assert!(
            !client.views.typing(client.store.nav.view),
            "an invisible form armed the keyboard"
        );
        assert_eq!(press(&mut client, KeyCode::Enter), None);

        // The floor itself draws every row the box cannot do without.
        let mut client = armed();
        client.frame(120, 16);
        press(&mut client, KeyCode::Char('a'));
        let at_floor = client.frame(120, 16);
        assert!(at_floor.contains("ALPACA LOGIN"), "{at_floor}");
        assert!(at_floor.contains("Enter stores"), "{at_floor}");
        assert!(client.views.typing(client.store.nav.view));

        // A terminal that shrinks under an open form retires it *and* what was
        // typed into it, so growing back cannot restore a credential the
        // operator has not looked at since.
        typed(&mut client, "PKTEST0123456789");
        client.frame(120, 15);
        assert!(!client.views.typing(client.store.nav.view));
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        client.frame(120, 36);
        assert!(!client.frame(120, 36).contains("••••"), "the pair survived");
    }

    #[test]
    fn testing_the_login_is_one_key_and_the_row_retones_from_the_owners_own_answer() {
        let mut client = armed();
        assert_eq!(
            press(&mut client, KeyCode::Char('t')),
            Some(Command::TestAlpaca)
        );
        // With the form open the key is a character, not a command: a secret
        // containing `t` must not fire the probe.
        press(&mut client, KeyCode::Char('a'));
        assert_eq!(press(&mut client, KeyCode::Char('t')), None);

        // The row is toned from the snapshot the write brings forward, never
        // from this client remembering what it sent. The unreachable case is
        // pinned above; this is the other half — a login the owner reports it
        // can read is not drawn as a warning.
        let mut store = super::store_from(
            r#"{"desk_mode": {"data": "live", "book": "alpaca", "label": "LIVE · ALPACA BOOK",
                 "credentials": "paper key ending 4f21", "credentials_ok": true}}"#,
        );
        store.posture = Posture::Operator;
        let mut ok = Client::new(store);
        ok.press(KeyCode::Char('8'));
        assert_eq!(
            body_style_of(&ok.buffer(120, 36), "paper key ending").fg,
            Some(Theme::truecolor().text_secondary),
            "a login the owner can read is not a warning"
        );
    }
}

// -- the cards an armed window can reach ------------------------------------

#[cfg(feature = "operator")]
mod cards {
    use super::*;
    use atlas::bus::AppEvent;
    use atlas::cmd::{Command, ModelChoice};
    use atlas::model::LlmCatalog;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers};

    /// SETTINGS on the captured desk, armed, with the catalog the picker reads
    /// already folded in — and one frame drawn, because every floor on this
    /// pane is read off the area the last frame published.
    fn armed() -> Client {
        let mut store = super::harness::fixture_store();
        store.posture = Posture::Operator;
        store.apply(
            AppEvent::Backends(
                serde_json::from_str::<LlmCatalog>(include_str!("fixtures/llm_backends.json"))
                    .unwrap(),
            ),
            std::time::Instant::now(),
        );
        let mut client = Client::new(store);
        client.press(KeyCode::Char('8'));
        client.frame(120, 36);
        client
    }

    fn press(client: &mut Client, code: KeyCode) -> Option<Command> {
        let acted = atlas::ui::shell::on_key(
            KeyEvent::new(code, KeyModifiers::NONE),
            &mut client.store,
            &mut client.views,
        );
        client.frame(120, 36);
        acted
    }

    /// Walk the focus down to the card whose title this names.
    fn focus_on(client: &mut Client, title: &str) {
        for _ in 0..8 {
            if body_style_of(&client.buffer(120, 36), title).fg == Some(Theme::truecolor().accent) {
                return;
            }
            press(client, KeyCode::Down);
        }
        panic!(
            "the focus never reached {title}:\n{}",
            client.frame(120, 36)
        );
    }

    #[test]
    fn the_arrows_walk_the_cards_and_each_one_answers_for_its_own_keys() {
        // The footer is on the card that is *listening*, never on the five that
        // are not: a line naming `a` under a card where `a` does nothing is an
        // instruction with nothing behind it, which is the same fault as
        // offering an operator key to a glass window.
        let mut client = armed();
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("a types a login"),
            "the desk card opens listening:\n{body}"
        );
        assert_eq!(body.matches("types a login").count(), 1, "{body}");

        // One card down, and the footer moves with the focus. POLICY has no
        // keys at all, and says so rather than going quiet — silence and "there
        // is nothing here" are the two readings this pane spends rows to keep
        // apart.
        press(&mut client, KeyCode::Down);
        let body = content(&client.frame(120, 36));
        assert!(!body.contains("types a login"), "{body}");
        assert!(body.contains("no keys on this card"), "{body}");
        assert_eq!(
            body_style_of(&client.buffer(120, 36), "POLICY").fg,
            Some(Theme::truecolor().accent),
            "the header did not follow the focus"
        );

        // And the card the picker lives on names the key that opens it.
        focus_on(&mut client, "MODELS");
        let body = content(&client.frame(120, 36));
        assert!(body.contains("m switches a model"), "{body}");
        // Both ends of the walk, because the clamp is a comparison and a case
        // that only reaches it proves nothing: a mutation that wrapped at the
        // *bottom* survived a version of this test that walked only to the
        // middle and back. Down past the last card stays on the last card —
        // an operator holding an arrow must not find themselves back at the
        // top, which reads as the list having scrolled rather than ended.
        for _ in 0..12 {
            press(&mut client, KeyCode::Down);
        }
        let body = content(&client.frame(120, 36));
        assert!(
            !body.contains("a types a login"),
            "holding ↓ wrapped round to the first card:\n{body}"
        );
        assert_eq!(
            body_style_of(&client.buffer(120, 36), "UNIVERSE").fg,
            Some(Theme::truecolor().accent),
            "the walk did not stop on the last card"
        );
        // And the top end, the same way.
        for _ in 0..12 {
            press(&mut client, KeyCode::Up);
        }
        assert!(content(&client.frame(120, 36)).contains("a types a login"));

        // Leaving the pane and coming back aims the keys at the desk card
        // again. The focus is the only thing on this client that decides what a
        // key *means* while being invisible from everywhere else, so a walk to
        // MODELS followed by a trip to BOOK left `a` silently dead on a pane
        // whose desk card the operator was reading — no cue, and nothing to
        // correct. Every other cursor here is worth keeping because it is drawn
        // where it was left.
        focus_on(&mut client, "MODELS");
        client.press(KeyCode::Char('4'));
        client.frame(120, 36);
        client.press(KeyCode::Char('8'));
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("a types a login"),
            "the focus survived a trip away from the pane:\n{body}"
        );
        // Not just the footer: the key it names works.
        press(&mut client, KeyCode::Char('a'));
        assert!(
            content(&client.frame(120, 36)).contains("ALPACA LOGIN"),
            "the card said `a` and `a` did nothing"
        );
    }

    #[test]
    fn a_key_reaches_the_card_that_owns_it_and_no_other() {
        // Both sides of the routing, because a pane that answered every key
        // from every card would pass a test that only pressed them where they
        // work — and the footers would be describing a rule the router does not
        // have.
        let mut client = armed();
        assert_eq!(
            press(&mut client, KeyCode::Char('m')),
            None,
            "m is not the desk card's"
        );
        assert!(!content(&client.frame(120, 36)).contains("WHICH MINDS"));

        focus_on(&mut client, "MODELS");
        assert_eq!(
            press(&mut client, KeyCode::Char('a')),
            None,
            "a opened the login form from a card that does not own it"
        );
        assert!(!content(&client.frame(120, 36)).contains("ALPACA LOGIN"));
        assert_eq!(
            press(&mut client, KeyCode::Char('t')),
            None,
            "t probed the venue from a card that does not own it"
        );
    }

    #[test]
    fn opening_the_switcher_asks_the_owner_what_it_can_run_and_shows_what_it_said() {
        // The catalog is a *reading*, and the one on the store may be an hour
        // old — so the key that opens the box refetches on the way in, exactly
        // as the door's first question does on its transition and as the
        // command line does when it enters the model scope.
        let mut client = armed();
        focus_on(&mut client, "MODELS");
        assert_eq!(
            press(&mut client, KeyCode::Char('m')),
            Some(Command::Backends)
        );
        let body = content(&client.frame(120, 36));
        assert!(body.contains("WHICH MINDS"), "{body}");
        // The workforce is offered `claude` and no tier beside it: the tier map
        // owns that model, so `claude · haiku` would be a choice that changed
        // nothing. Inherited from `cmd::offers` rather than restated here.
        assert!(body.contains("reasoner"), "{body}");
        assert!(body.contains("workforce"), "{body}");
        assert!(!body.contains("workforce  claude:haiku"), "{body}");
        // Esc leaves every surface as the desk has it.
        press(&mut client, KeyCode::Esc);
        assert!(!content(&client.frame(120, 36)).contains("WHICH MINDS"));
    }

    #[test]
    fn choosing_a_row_sends_the_pair_the_owner_named_and_a_down_backend_sends_nothing() {
        let mut client = armed();
        focus_on(&mut client, "MODELS");
        press(&mut client, KeyCode::Char('m'));
        // The cursor opens on what the surface is running, so Enter with no
        // arrows at all sends the desk's own pair — an operator who opens the
        // box and changes their mind changes nothing.
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetLlm {
                surface: "reasoner".into(),
                choice: ModelChoice::Pair {
                    backend: "ollama".into(),
                    model: "qwen2.5:7b".into()
                }
            }),
            "the cursor did not open on what the desk is using"
        );
        // And the box stays up, because both surfaces are chosen from it.
        assert!(content(&client.frame(120, 36)).contains("WHICH MINDS"));
    }

    /// A catalog with more offers than the box shows at once.
    ///
    /// Twenty ollama models, which is a real desk with a few families pulled —
    /// and, crucially, more than the eight-row window: every bug in this pair
    /// needs `top > 0` to be reachable, and the seven-offer fixture above can
    /// never produce it. `running` is what the caller asks the desk to be on,
    /// so the cursor can be opened deep in the list.
    fn deep_catalog(running: &str) -> Client {
        let models: Vec<String> = (0..20).map(|i| format!("m{i:02}")).collect();
        let mut store = super::store_from(&format!(
            r#"{{"llm": {{"reasoner": {{"backend": "ollama", "model": "{running}"}},
                          "workforce": {{"backend": "claude", "model": "inherit"}},
                          "reasoner_enabled": false}}}}"#
        ));
        store.posture = Posture::Operator;
        store.apply(
            AppEvent::Backends(
                serde_json::from_value(serde_json::json!({
                    "backends": [
                        {"name": "claude", "available": true, "reason": "claude CLI on PATH",
                         "models": ["inherit"]},
                        {"name": "ollama", "available": true,
                         "reason": "ollama at 127.0.0.1:11434, 20 models pulled",
                         "models": models}
                    ],
                    "probed_at": PROBED
                }))
                .unwrap(),
            ),
            std::time::Instant::now(),
        );
        let mut client = Client::new(store);
        client.press(KeyCode::Char('8'));
        client.frame(120, 36);
        client
    }

    /// The row the cursor is on, as an operator sees it, on single spaces.
    ///
    /// Collapsed so a caller can name the whole row rather than a word of it:
    /// the surface is padded into a column, and half this list carries
    /// `workforce` while half carries `m19` — a substring of either passes on a
    /// cursor eight rows from the one the assertion is about.
    fn under_cursor(client: &Client) -> String {
        let frame = client.frame(120, 36);
        line_with(&frame, "▸")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
    }

    /// The daemon has gone down: two claude offers and two refusals, where
    /// there were forty-two.
    fn daemon_down(client: &mut Client) {
        client.store.apply(
            AppEvent::Backends(
                serde_json::from_value(serde_json::json!({
                    "backends": [
                        {"name": "claude", "available": true, "reason": "claude CLI on PATH",
                         "models": ["inherit"]},
                        {"name": "ollama", "available": false,
                         "reason": "ollama is not running at http://127.0.0.1:11434 — start \
                                    it with `ollama serve`",
                         "models": []}
                    ],
                    "probed_at": PROBED
                }))
                .unwrap(),
            ),
            std::time::Instant::now(),
        );
    }

    #[test]
    fn a_scrolled_box_whose_catalog_shrinks_still_draws_the_offers_it_has() {
        // The half a seven-offer fixture cannot reach: with the list scrolled,
        // clamping the *cursor* alone left `top` past the end of the shrunk
        // list, so the box drew `▴ N above` and **no offer rows at all** — a
        // window onto nothing, over a desk that plainly had two models.
        let mut client = deep_catalog("m00");
        focus_on(&mut client, "MODELS");
        press(&mut client, KeyCode::Char('m'));
        for _ in 0..12 {
            press(&mut client, KeyCode::Down);
        }
        let scrolled = content(&client.frame(120, 36));
        assert!(
            scrolled.contains("▴"),
            "the list did not scroll, so this test cannot see the bug:\n{scrolled}"
        );
        daemon_down(&mut client);
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("claude:inherit"),
            "the box windowed onto nothing:\n{body}"
        );
        assert!(body.contains("▸"), "the cursor left the box:\n{body}");
        // A list shorter than the window is shown whole, so nothing claims
        // there are rows above it either.
        assert!(!body.contains("▴"), "{body}");
    }

    #[test]
    fn a_box_opened_on_a_row_past_the_first_window_opens_with_the_cursor_on_screen() {
        // `opened_on` puts the cursor on the row the surface is running and
        // left the window at the top, so a desk running the twentieth model
        // opened showing rows 0..8 with no ▸ anywhere — and the next Down
        // jumped the window, which means the operator could send a model they
        // had never had on screen.
        let mut client = deep_catalog("m19");
        focus_on(&mut client, "MODELS");
        press(&mut client, KeyCode::Char('m'));
        let body = content(&client.frame(120, 36));
        assert!(body.contains("▸"), "the box opened with no cursor:\n{body}");
        assert!(
            under_cursor(&client).contains("reasoner ollama:m19"),
            "the cursor is not on what the desk is running: {}",
            under_cursor(&client)
        );
        // And the first Down moves one row rather than a window: the row it
        // lands on is the next one in the list, which the operator was already
        // looking at.
        press(&mut client, KeyCode::Down);
        assert!(
            under_cursor(&client).contains("workforce claude"),
            "one Down skipped the rest of the list: {}",
            under_cursor(&client)
        );
    }

    #[test]
    fn a_catalog_that_shrinks_under_an_open_box_keeps_a_cursor_on_it() {
        // The box is rebuilt from the store on every keystroke and every frame,
        // and the store moves under it — the key that opened it asked for a
        // fresh catalog, and a daemon that has gone down since answers with
        // fewer models than the cursor was sitting past. Without the clamp the
        // list draws with no cursor at all and Enter reports an empty catalog
        // over a box plainly showing rows, which is a client an operator cannot
        // tell from one that has stopped taking keys.
        let mut client = armed();
        focus_on(&mut client, "MODELS");
        press(&mut client, KeyCode::Char('m'));
        for _ in 0..6 {
            press(&mut client, KeyCode::Down);
        }
        // The owner answers again, with the daemon down and claude thinned to
        // one tier: seven offers become two.
        client.store.apply(
            AppEvent::Backends(
                serde_json::from_value(serde_json::json!({
                    "backends": [
                        {"name": "claude", "available": true, "reason": "claude CLI on PATH",
                         "models": ["inherit"]}
                    ],
                    "probed_at": PROBED
                }))
                .unwrap(),
            ),
            std::time::Instant::now(),
        );
        let body = content(&client.frame(120, 36));
        assert!(body.contains("▸"), "the list lost its cursor:\n{body}");
        // And Enter chooses the last row rather than answering about a catalog
        // it can plainly see is not empty.
        assert!(
            press(&mut client, KeyCode::Enter).is_some(),
            "Enter refused a row the box was showing:\n{}",
            content(&client.frame(120, 36))
        );
    }

    #[test]
    fn a_backend_the_desk_cannot_reach_stays_on_the_list_with_the_owners_reason() {
        // Shown, never hidden, and refused in the owner's own sentence — the
        // rule `/model` and the startup door both submit to, held here because
        // all three read one producer.
        let mut store = super::harness::fixture_store();
        store.posture = Posture::Operator;
        store.apply(
            AppEvent::Backends(
                serde_json::from_value(serde_json::json!({
                    "backends": [
                        {"name": "claude", "available": true, "reason": "claude CLI on PATH",
                         "models": ["inherit", "haiku"]},
                        {"name": "ollama", "available": false,
                         "reason": "ollama is not running at http://127.0.0.1:11434 — start it \
                                    with `ollama serve`",
                         "models": []}
                    ],
                    "probed_at": PROBED
                }))
                .unwrap(),
            ),
            std::time::Instant::now(),
        );
        let mut client = Client::new(store);
        client.press(KeyCode::Char('8'));
        client.frame(120, 36);
        focus_on(&mut client, "MODELS");
        press(&mut client, KeyCode::Char('m'));
        let body = content(&client.frame(120, 36));
        assert!(body.contains("ollama"), "a down backend vanished:\n{body}");
        // The cursor opens at the top here — this desk runs a model the down
        // daemon can no longer serve, so no row is the one it is running — and
        // the reasoner's third offer is the daemon itself.
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Down);
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            None,
            "a backend the desk cannot reach was chosen"
        );
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("ollama serve"),
            "the owner's reason never showed:\n{body}"
        );
        // Both sides of that guard, in the same box: the row above is one the
        // desk can run, and Enter there sends rather than refusing.
        press(&mut client, KeyCode::Up);
        assert!(
            press(&mut client, KeyCode::Enter).is_some(),
            "the refusal swallowed a choice the owner can serve"
        );
    }

    #[test]
    fn the_focused_models_card_renders_at_120x36() {
        let mut client = armed();
        focus_on(&mut client, "MODELS");
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn the_model_switcher_renders_at_120x36() {
        let mut client = armed();
        focus_on(&mut client, "MODELS");
        press(&mut client, KeyCode::Char('m'));
        insta::assert_snapshot!(client.frame(120, 36));
    }
}
