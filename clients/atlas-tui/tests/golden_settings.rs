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
    client.press(KeyCode::Char('9'));
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
    client.press(KeyCode::Char('9'));
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
    // Nine cards, nine statements, and the ones that carry keys in an armed
    // window get the specific ones: a glass window points the desk nowhere and
    // changes nothing about what it reads.
    assert!(
        body.contains("read-only — cannot switch the desk"),
        "{body}"
    );
    assert!(
        body.contains("read-only — cannot change what is read"),
        "{body}"
    );
    assert_eq!(
        body.matches("read-only").count(),
        9,
        "one footer per card, and this pane draws nine:\n{body}"
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
    client.press(KeyCode::Char('9'));
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
    // Two hundred columns rather than the baseline's hundred and twenty, and
    // the width is the point: this sentence is 74 cells and the card at 120 is
    // 38, so it wraps to three rows — more than the one row of slack the card
    // has left once the rights have taken theirs (see `MODELS_H`). At the
    // baseline it is *counted*, which is what
    // `a_reason_that_will_not_fit_whole_is_counted_rather_than_cut_in_half`
    // pins; what this test is about is the tone the reason carries when it is
    // drawn, and a wider card is where that happens.
    // The tone is a fact about the *row*, and it is owed at the baseline
    // whether or not the sentence under it fitted: an operator reading a
    // counted reason still has to see that the mind is unreachable, and the
    // 200-column assertion below would not have caught a tone that only
    // survived on a wide card.
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "ollama · granite3.3:8b").fg,
        Some(Theme::truecolor().warning),
        "the row lost its tone at the baseline, where the reason is counted"
    );
    let frame = client.frame(200, 36);
    assert_eq!(
        body_style_of(&client.buffer(200, 36), "ollama · granite3.3:8b").fg,
        Some(Theme::truecolor().warning),
        "a mind the desk cannot reach is not a quiet fact"
    );
    assert!(content(&frame).contains("ollama serve"), "{frame}");
    assert_eq!(
        body_style_of(&client.buffer(200, 36), "ollama is not running").fg,
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
    // Wide enough that the *bounded* sentence is one row, so what is being
    // pinned is the bound rather than the count: at 120 columns a 112-cell
    // sentence wraps past the card's one row of slack (see `MODELS_H`) and is
    // counted, which says nothing about whether it was cut at the boundary.
    let body = content(&client.frame(280, 36));
    assert!(body.contains('…'), "{body}");
    // The card ends where it always did — the theme card under it is still on
    // the frame, which is the property an unbounded reason would take away.
    assert!(body.contains("palette"), "{body}");
    // And at the baseline the unbounded sentence still cannot push a row off
    // the card, which is the other half of the same claim.
    let narrow = content(&client.frame(120, 36));
    assert!(narrow.contains("palette"), "{narrow}");
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
fn a_window_with_neither_key_is_told_what_neither_key_does() {
    // The tab note is entirely about `/cli`, and `/cli` is a `writes` scope: a
    // window the desk has not armed is offered the word nowhere, so a card that
    // described it here would be the greyed-affordance claim this pane refuses.
    //
    // This test is deliberately ungated, and that is the point. It is the one
    // assertion that runs in BOTH legs, and the monitoring artifact — which has
    // no pane at all — passes it by holding no posture that writes, rather than
    // by any wording. The read-only card is one frame in either binary.
    let client = models_from(&format!(
        r#"{{"reasoner": {{"backend": "ollama", "model": "granite3.3:8b"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "probed_at": "{PROBED}"}}"#
    ));
    let body = content(&client.frame(120, 36));
    // The row itself is empty, rather than merely free of two known sentences:
    // the note is the last thing this card draws, so what is asserted is that
    // *nothing* is under the line the rights end with. A wording check would
    // pass on any third sentence, including the one this replaced.
    let rows: Vec<&str> = body.lines().collect();
    let at = rows
        .iter()
        .position(|row| row.contains(ASYMMETRY))
        .unwrap_or_else(|| panic!("the rights line is not on this frame:\n{body}"));
    // Char offsets, not byte ones: the rules and separators on these rows are
    // multi-byte and a byte index would cut into one.
    let column = rows[at]
        .chars()
        .collect::<Vec<_>>()
        .windows(ASYMMETRY.chars().count())
        .position(|w| w.iter().collect::<String>() == ASYMMETRY)
        .unwrap();
    // The pane's own right border sits at the end of every one of these rows
    // and is not the card's content, so it is dropped rather than trimmed —
    // trimming would also swallow a sentence that ended in one.
    let under: String = rows[at + 1]
        .chars()
        .skip(column)
        .filter(|c| *c != '│')
        .collect();
    assert!(
        under.trim().is_empty(),
        "under the rights: {under:?}\n{body}"
    );
    // The reading itself is untouched — this is a sentence the card drops, not
    // a card that stopped answering.
    assert!(body.contains("ollama · granite3.3:8b"), "{body}");
    assert!(body.contains(ASYMMETRY), "{body}");
}

/// The last line of the rights section, which the tab note is drawn under.
const ASYMMETRY: &str = "nothing here binds a non-chat caller";

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
    // Counted at the baseline, where the card's one row of slack cannot hold
    // three wrapped rows of sentence.
    //
    // **This is where the rights were paid for.** The card had four slack rows
    // and this sentence took three of them; the three switches and the line
    // that qualifies them took four, and the right column had exactly one spare
    // row to give (see `MODELS_H`). So the longest reason the owner writes is
    // now counted at 120x36 rather than drawn, and the count is what says so —
    // which is the trade, stated rather than discovered.
    let baseline = content(&client.frame(120, 36));
    assert!(baseline.contains("▾"), "{baseline}");
    assert!(!baseline.contains("ollama is running at"), "{baseline}");
    // And whole wherever it does fit, which is the other half of the property:
    // a card that had quietly stopped drawing reasons altogether would pass the
    // count assertion above on its own.
    // The cliff itself, from both sides. `wrapped_rows` reserves a row for a
    // word break, so a sentence costs one row at or under the card's width and
    // *three* past it — there is no two-row reason to test, and the card's one
    // slack row means only the first is ever drawn. Pinning both sides is what
    // stops "drawn whole" from being a claim about short strings: 37 cells is
    // the widest sentence this card can show, and 38 is counted.
    let cliff = |reason: &str| {
        let client = models_from(&format!(
            r#"{{"reasoner": {{"backend": "claude", "model": "inherit"}},
                 "workforce": {{"backend": "claude", "model": "inherit"}},
                 "reasoner_enabled": true,
                 "availability": [{{"name": "claude", "available": false,
                                    "reason": "{reason}"}}],
                 "probed_at": "{PROBED}"}}"#
        ));
        content(&client.frame(120, 36))
    };
    let widest = "x".repeat(37);
    assert!(
        cliff(&widest).contains(&widest),
        "the widest one-row reason was not drawn"
    );
    let over = "y".repeat(38);
    let counted = cliff(&over);
    assert!(counted.contains("▾ 1 more"), "{counted}");
    assert!(!counted.contains("yyyy"), "{counted}");

    // And a sentence nobody would call short, drawn whole where the card is
    // wide enough to hold it in one row: the property is about the *row*, not
    // about the sentence being small.
    let long = "the claude CLI exited 1: Invalid API key · Please run /login";
    let wide = models_from(&format!(
        r#"{{"reasoner": {{"backend": "claude", "model": "inherit"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "claude", "available": false,
                                "reason": "{long}"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    let body = content(&wide.frame(200, 36));
    assert!(body.contains(long), "{body}");
    assert!(!body.contains("▾"), "{body}");

    // A claude reasoner, so the hand-off note is silent and the one row of
    // slack is the reason's alone.
    let brief = models_from(&format!(
        r#"{{"reasoner": {{"backend": "claude", "model": "inherit"}},
             "workforce": {{"backend": "claude", "model": "inherit"}},
             "reasoner_enabled": true,
             "availability": [{{"name": "claude", "available": false,
                                "reason": "no claude CLI on PATH"}}],
             "probed_at": "{PROBED}"}}"#
    ));
    let whole = content(&brief.frame(120, 36));
    assert!(whole.contains("no claude CLI on PATH"), "{whole}");
    assert!(!whole.contains("▾"), "{whole}");
    // Counted where it will not fit. Unconditional and without an `||`: the
    // first version of this test allowed either outcome at a height where only
    // one is possible, and deleting the marker altogether still passed it — the
    // reason then vanished silently, which is the failure the count exists to
    // prevent.
    // Two at thirty rows, not one: the column hands the card seven rows there,
    // so the three switches do not fit in its one row of slack either and are
    // counted beside the reason. The hand-off note is not among them — it is
    // this client's own copy and yields to the owner's sentences. The number is
    // exact on purpose (see above).
    let short = content(&client.frame(120, 30));
    // Named, not merely counted: the deferral owns a row ahead of the cut, so
    // the operator is told the rights are the section that is off screen. It
    // was `▾ 2 more` — a number describing nothing — until the reservation was
    // moved in front of the marker.
    assert!(short.contains("▾ 2 more, incl. the rights"), "{short}");
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
    assert!(!content(&frame).contains("a login · t test"), "{frame}");
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
        client.press(KeyCode::Char('9'));
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
        ok.press(KeyCode::Char('9'));
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
        client.press(KeyCode::Char('9'));
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
        for _ in 0..12 {
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
            body.contains("a login · t test"),
            "the desk card opens listening:\n{body}"
        );
        assert_eq!(body.matches("a login · t test").count(), 1, "{body}");

        // One card down is NEWS, which has keys of its own; two down is
        // AUTHORITY, which has the one key that takes an authority away; three
        // down is METHOD, which has two of its own; and four down is POLICY,
        // which has none at all and says so rather than going quiet: silence
        // and "there is nothing here" are the two readings this pane spends
        // rows to keep apart.
        press(&mut client, KeyCode::Down);
        assert!(content(&client.frame(120, 36)).contains("s save"));
        press(&mut client, KeyCode::Down);
        assert!(content(&client.frame(120, 36)).contains("R revokes"));
        press(&mut client, KeyCode::Down);
        assert!(content(&client.frame(120, 36)).contains("m method · k cap"));
        press(&mut client, KeyCode::Down);
        let body = content(&client.frame(120, 36));
        assert!(!body.contains("a login · t test"), "{body}");
        assert!(body.contains("no keys on this card"), "{body}");
        assert_eq!(
            body_style_of(&client.buffer(120, 36), "POLICY").fg,
            Some(Theme::truecolor().accent),
            "the header did not follow the focus"
        );

        // And the card the picker lives on names the key that opens it.
        focus_on(&mut client, "MODELS");
        let body = content(&client.frame(120, 36));
        assert!(body.contains("m model · ↑↓ space grants a right"), "{body}");
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
            !body.contains("a login · t test"),
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
        assert!(content(&client.frame(120, 36)).contains("a login · t test"));

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
        client.press(KeyCode::Char('9'));
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("a login · t test"),
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
        // `m` belongs to two cards now and means a different box on each, so
        // the keys that are *not* the desk card's are the news card's.
        for key in ['s', 'v', ' '] {
            assert_eq!(
                press(&mut client, KeyCode::Char(key)),
                None,
                "{key} is not the desk card's"
            );
        }
        // And the box `m` opens here is the lane picker rather than the model
        // one — same widget, different list, and the card that owns each says
        // which.
        press(&mut client, KeyCode::Char('m'));
        let body = content(&client.frame(120, 36));
        assert!(body.contains("WHICH DATA"), "{body}");
        assert!(!body.contains("WHICH MINDS"), "{body}");
        press(&mut client, KeyCode::Esc);

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
        client.press(KeyCode::Char('9'));
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
        client.press(KeyCode::Char('9'));
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

// -- what the desk reads, and the lane it reads it on ------------------------

/// A glass window still reads the card, and still cannot touch it.
#[test]
fn the_news_card_is_read_only_in_a_glass_window() {
    let mut store = harness::fixture_store();
    store.apply(
        AppEvent::News(Box::new(
            serde_json::from_str(include_str!("fixtures/news_settings.json")).unwrap(),
        )),
        Instant::now(),
    );
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    let frame = client.frame(120, 36);
    let body = content(&frame);
    // The owner's own resolution and its catalog, both.
    assert!(line_with(&frame, "stack").contains("macro rss"), "{frame}");
    assert!(body.contains("gdelt"), "{body}");
    // And the rule says there is nothing to press, rather than naming keys
    // this window would refuse.
    assert!(
        body.contains("read-only — cannot change what is read"),
        "{body}"
    );
    assert!(!body.contains("s save"), "{body}");
}

#[test]
fn the_news_card_renders_read_only_at_120x36() {
    let mut store = harness::fixture_store();
    store.apply(
        AppEvent::News(Box::new(
            serde_json::from_str(include_str!("fixtures/news_settings.json")).unwrap(),
        )),
        Instant::now(),
    );
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    insta::assert_snapshot!(client.frame(120, 36));
}

#[cfg(feature = "operator")]
mod news {
    use super::*;
    use atlas::bus::Wrote;
    use atlas::cmd::Command;
    use atlas::model::NewsSettings;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseButton, MouseEventKind};

    fn payload() -> NewsSettings {
        serde_json::from_str(include_str!("fixtures/news_settings.json")).unwrap()
    }

    /// SETTINGS on the captured desk, armed, with the news answer folded in and
    /// one frame drawn — every floor and every click rectangle on this pane is
    /// read off the area the last frame published.
    fn armed_with(news: NewsSettings) -> Client {
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        store.apply(AppEvent::News(Box::new(news)), Instant::now());
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        client.frame(120, 36);
        client
    }

    fn armed() -> Client {
        armed_with(payload())
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

    fn click(client: &mut Client, column: u16, row: u16) -> Option<Command> {
        let acted = atlas::ui::shell::on_mouse(
            crossterm::event::MouseEvent {
                kind: MouseEventKind::Down(MouseButton::Left),
                column,
                row,
                modifiers: KeyModifiers::NONE,
            },
            &mut client.store,
            &mut client.views,
        );
        client.frame(120, 36);
        acted
    }

    /// The screen row a line is drawn on, so a click can be aimed at what the
    /// operator is actually looking at rather than at a coordinate this test
    /// worked out for itself.
    fn row_of(client: &Client, needle: &str) -> u16 {
        client
            .frame(120, 36)
            .lines()
            .position(|line| line.contains(needle))
            .unwrap_or_else(|| panic!("no row contains {needle}")) as u16
    }

    /// Put the focus on NEWS, which is the card after DESK.
    fn on_news(client: &mut Client) {
        press(client, KeyCode::Down);
    }

    /// Type a pair into the open login form and send it, so the form is
    /// *waiting* — which is the only state an owner's answer reaches it in.
    fn fill(client: &mut Client) {
        for c in "PKTEST0123456789".chars() {
            press(client, KeyCode::Char(c));
        }
        press(client, KeyCode::Tab);
        for c in "s3cretABCDEFGHIJKLMNOPQRSTUV".chars() {
            press(client, KeyCode::Char(c));
        }
        press(client, KeyCode::Enter);
    }

    #[test]
    fn a_tick_is_a_draft_and_the_card_says_so_until_it_is_sent() {
        let mut client = armed();
        on_news(&mut client);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("s save"), "the card names its keys:\n{body}");
        assert!(
            !body.contains("EDITED"),
            "a fresh card is not an edit:\n{body}"
        );

        // The cursor opens on the first source and Space ticks the one under
        // it. Nothing is sent: the draft is this window's until `s` carries it.
        press(&mut client, KeyCode::Down); // alpaca -> edgar
        press(&mut client, KeyCode::Down); // edgar -> macro
        press(&mut client, KeyCode::Down); // macro -> rss
        press(&mut client, KeyCode::Down); // rss -> gdelt
        assert_eq!(press(&mut client, KeyCode::Char(' ')), None, "a tick sent");
        let frame = client.frame(120, 36);
        assert!(content(&frame).contains("EDITED"), "{frame}");
        assert!(line_with(&frame, "gdelt").contains("[x]"), "{frame}");
        // And the owner's own answer under it is untouched — the draft is not
        // a copy of the stack.
        assert!(line_with(&frame, "stack").contains("macro rss"), "{frame}");

        // Ticked off again, and the mark goes with it: back to the owner's set
        // is not an edit.
        press(&mut client, KeyCode::Char(' '));
        assert!(!content(&client.frame(120, 36)).contains("EDITED"));
    }

    #[test]
    fn a_source_this_desk_cannot_read_is_refused_on_the_keystroke() {
        // The owner answers the same 400, and the row already carries what it
        // is waiting for. A tick that appeared and then vanished on the save
        // would be this pane showing a state the desk can never be in.
        let mut client = armed();
        on_news(&mut client);
        assert_eq!(press(&mut client, KeyCode::Char(' ')), None);
        let frame = client.frame(120, 36);
        assert!(
            content(&frame).contains("alpaca needs an Alpaca credential"),
            "{frame}"
        );
        assert!(
            !line_with(&frame, " alpaca").contains("[x]"),
            "a source the desk cannot read was ticked:\n{frame}"
        );
        // Nothing is claimed as edited by a refusal.
        assert!(!content(&frame).contains("EDITED"), "{frame}");
    }

    #[test]
    fn edgar_without_a_contact_is_not_sent_and_the_card_points_at_the_key() {
        // The second refusal made here rather than by the owner, and for the
        // same reason: the remedy is a key on this card, and the owner's
        // sentence names the shape of a contact rather than the box that takes
        // one.
        let mut client = armed();
        on_news(&mut client);
        press(&mut client, KeyCode::Down); // edgar
        press(&mut client, KeyCode::Char(' '));
        assert_eq!(
            press(&mut client, KeyCode::Char('s')),
            None,
            "a stack the desk cannot honour was sent"
        );
        let body = content(&client.frame(120, 36));
        assert!(body.contains("edgar needs a contact — press c"), "{body}");
        // The same for the checked save: `v` is `s` with one more question.
        assert_eq!(press(&mut client, KeyCode::Char('v')), None);

        // The contact box takes one, keeps it in the draft, and sends nothing
        // by itself.
        press(&mut client, KeyCode::Char('c'));
        for c in "Jane Quant <jane@x.io>".chars() {
            assert_eq!(press(&mut client, KeyCode::Char(c)), None);
        }
        let box_frame = client.frame(120, 36);
        assert!(content(&box_frame).contains("EDGAR CONTACT"), "{box_frame}");
        assert!(content(&box_frame).contains("jane@x.io"), "{box_frame}");
        assert_eq!(press(&mut client, KeyCode::Enter), None, "the box sent");
        // Closed, and the contact is nowhere on the pane: it lives in the draft
        // and in the one POST that carries it.
        let closed = client.frame(120, 36);
        assert!(!content(&closed).contains("jane@x.io"), "{closed}");

        // And now the save carries both halves.
        assert_eq!(
            press(&mut client, KeyCode::Char('s')),
            Some(Command::NewsSettings {
                providers: vec!["edgar".into(), "macro".into(), "rss".into()],
                contact: Some(atlas::cmd::Contact::new("Jane Quant <jane@x.io>".into())),
                verify: false,
                // The lane the card is drawing, which is the lane the answer in
                // front of the operator is about.
                offline: false,
            })
        );
        // One request at a time: the route writes the operator's `.env`.
        assert_eq!(press(&mut client, KeyCode::Char('s')), None);
    }

    #[test]
    fn the_checked_save_is_the_same_request_with_one_more_question() {
        let mut client = armed();
        on_news(&mut client);
        assert_eq!(
            press(&mut client, KeyCode::Char('v')),
            Some(Command::NewsSettings {
                // Untouched, so the draft is the owner's own chosen set.
                providers: vec!["macro".into(), "rss".into()],
                contact: None,
                verify: true,
                offline: false,
            })
        );
    }

    #[test]
    fn the_owners_answer_replaces_the_draft_and_its_refusal_stays_on_the_card() {
        let mut client = armed();
        on_news(&mut client);
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Char(' ')); // edgar, with no contact
        press(&mut client, KeyCode::Char('c'));
        for c in "Jane <jane@x.io>".chars() {
            press(&mut client, KeyCode::Char(c));
        }
        press(&mut client, KeyCode::Enter);
        assert!(press(&mut client, KeyCode::Char('s')).is_some());

        // A refusal is the owner's sentence, on the card that asked — the
        // toast that carries it too is gone in four seconds.
        client.views.wrote(&Wrote::NewsRefused {
            said: "edgar needs a contact, as Your Name <you@example.org>".into(),
        });
        let body = content(&client.frame(120, 36));
        assert!(body.contains("as Your Name"), "{body}");
        assert!(
            body.contains("EDITED"),
            "a refusal discarded the draft:\n{body}"
        );

        // And an applied change discards it: the refetch behind the outcome is
        // what the card then draws, so an edit mark over a landed change would
        // be this pane disagreeing with the desk.
        client.views.wrote(&Wrote::NewsSaved {
            stack: vec!["edgar".into(), "macro".into(), "rss".into()],
            checked: false,
            verified: Vec::new(),
        });
        let body = content(&client.frame(120, 36));
        assert!(!body.contains("EDITED"), "{body}");
        assert!(!body.contains("as Your Name"), "{body}");
    }

    #[test]
    fn a_card_with_no_answer_yet_says_so_rather_than_claiming_an_empty_desk() {
        // "the owner has not answered" and "this desk reads nothing" are two
        // facts, and a blank card would merge them.
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        client.frame(120, 36);
        let mut client = client;
        on_news(&mut client);
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("nothing has said what this desk reads"),
            "{body}"
        );
        // And every key on the card declines rather than pretending — out
        // loud. The refusal used to be written into a note this branch never
        // drew, which is the same silent-key shape the wait line closed.
        assert_eq!(press(&mut client, KeyCode::Char(' ')), None);
        assert_eq!(press(&mut client, KeyCode::Char('s')), None);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("nothing has said"), "{body}");
        assert!(
            body.contains("nothing to save — no answer yet"),
            "a refusal was written and never painted:\n{body}"
        );
    }

    #[test]
    fn a_click_lands_on_the_row_the_operator_can_see() {
        // One click, one visible change: the row takes the focus *and* is
        // picked, because a click that only moved a cursor would leave the
        // operator reaching for the keyboard to finish what they started.
        let mut client = armed();
        let row = row_of(&client, "gdelt");
        assert_eq!(click(&mut client, 70, row), None);
        let frame = client.frame(120, 36);
        assert!(line_with(&frame, "gdelt").contains("[x]"), "{frame}");
        assert!(content(&frame).contains("EDITED"), "{frame}");
        // The focus went with it, so the keys now mean what the card says.
        assert!(content(&frame).contains("s save"), "{frame}");

        // A click on another card's header moves the focus and picks nothing.
        let header = row_of(&client, "▌ POLICY");
        assert_eq!(click(&mut client, 12, header), None);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("no keys on this card"), "{body}");
        assert!(!body.contains("s save"), "{body}");
    }

    #[test]
    fn a_click_on_a_stated_key_word_does_exactly_what_the_key_does() {
        // The words are found in the card's own rule, so a reworded footer
        // moves its own affordances rather than leaving a rectangle over
        // whatever the new sentence put there.
        let mut client = armed();
        on_news(&mut client);
        let rule = row_of(&client, "s save");
        let line = line_with(&client.frame(120, 36), "s save").to_string();
        // Cells, not bytes: the frame is full of box-drawing characters and a
        // byte offset would aim the click several columns short of the word.
        let column = |word: &str| {
            let byte = line
                .find(word)
                .unwrap_or_else(|| panic!("no {word} on {line}"));
            line[..byte].chars().count() as u16
        };

        assert_eq!(
            click(&mut client, column("save"), rule),
            Some(Command::NewsSettings {
                providers: vec!["macro".into(), "rss".into()],
                contact: None,
                verify: false,
                offline: false,
            })
        );
        // And `contact` opens the box the key opens.
        client.views.wrote(&Wrote::NewsSaved {
            stack: vec!["macro".into(), "rss".into()],
            checked: false,
            verified: Vec::new(),
        });
        client.frame(120, 36);
        assert_eq!(click(&mut client, column("contact"), rule), None);
        assert!(content(&client.frame(120, 36)).contains("EDGAR CONTACT"));
    }

    #[test]
    fn the_lane_picker_sends_what_slash_mode_sends() {
        let mut client = armed();
        // DESK is where the focus opens, and the card names the key.
        let body = content(&client.frame(120, 36));
        assert!(body.contains("m switch lane"), "{body}");
        press(&mut client, KeyCode::Char('m'));
        let body = content(&client.frame(120, 36));
        assert!(body.contains("WHICH DATA"), "{body}");
        assert!(body.contains("synthetic (demo)"), "{body}");
        // The fixture desk is synthetic · simulated, so the box opens on the
        // row it is already running and Enter there changes nothing anyone can
        // see — the same rule the model switcher opens by.
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::DeskMode {
                data: "synthetic".into(),
                book: "simulated".into()
            })
        );
    }

    #[test]
    fn choosing_live_with_no_login_opens_the_form_and_comes_back_after_it() {
        // Not sent and not refused: the picker steps aside for the box that
        // fixes it. The fixture desk has no credential the owner can read.
        let mut client = armed();
        press(&mut client, KeyCode::Char('m'));
        press(&mut client, KeyCode::Down);
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            None,
            "a lane the desk cannot reach was sent"
        );
        let body = content(&client.frame(120, 36));
        assert!(body.contains("ALPACA LOGIN"), "{body}");
        assert!(!body.contains("WHICH DATA"), "{body}");

        // A login the owner cannot read does not bring the picker back: that
        // would put the operator on the row that sent them here.
        fill(&mut client);
        client.views.wrote(&Wrote::LoggedIn {
            usable: false,
            note: "stored, but no credential resolves".into(),
        });
        assert!(!content(&client.frame(120, 36)).contains("WHICH DATA"));

        // One that works does, on the row they were reaching for.
        press(&mut client, KeyCode::Char('m'));
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Enter);
        fill(&mut client);
        client.views.wrote(&Wrote::LoggedIn {
            usable: true,
            note: "PKTEST… · ACTIVE".into(),
        });
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("WHICH DATA"),
            "the picker did not come back:\n{body}"
        );
        assert!(body.contains("live · alpaca"), "{body}");
    }

    #[test]
    fn the_arrows_walk_through_the_news_rows_and_out_of_the_card() {
        // A row cursor an operator cannot leave is the same fault as a card
        // highlight that never moves — and NEWS sits second in the walk, so
        // trapping it would put five cards beyond reach of the arrows.
        let mut client = armed();
        for _ in 0..7 {
            press(&mut client, KeyCode::Down);
        }
        let body = content(&client.frame(120, 36));
        assert!(
            !body.contains("s save"),
            "the walk never left NEWS:\n{body}"
        );
        // And it landed somewhere rather than nowhere. Seven arrows is one into
        // the card, four through its rows, one out onto AUTHORITY and one more
        // onto METHOD — which states its own two keys, so a frame that reached
        // it cannot be confused with one still inside NEWS.
        assert!(body.contains("m method · k cap"), "{body}");
        // And back up through it to the desk card.
        for _ in 0..7 {
            press(&mut client, KeyCode::Up);
        }
        assert!(content(&client.frame(120, 36)).contains("m switch lane"));
    }

    #[test]
    fn a_check_is_read_per_member_and_never_off_the_flag_beside_them() {
        // `verify.ok` is any-member: it is true when *one* source answered. A
        // card that drew it as whole-stack health would report a dead feed as
        // a clean check, so nothing here reads it — every row carries its own
        // member's verdict.
        //
        // And a member can answer *and* be degraded, which is a third state
        // neither a pass-list nor a fail-list can hold: `partial:` is a fact
        // about one feed rather than a warning about the desk, and it is drawn
        // dim beside the row that earned it.
        let mut client = armed();
        on_news(&mut client);
        assert!(press(&mut client, KeyCode::Char('v')).is_some());
        client.views.wrote(&Wrote::NewsSaved {
            stack: vec!["macro".into(), "rss".into()],
            checked: true,
            verified: vec![
                atlas::bus::NewsMember {
                    name: "macro".into(),
                    ok: true,
                    detail: String::new(),
                    quality_flags: vec!["partial: ecb: 502 from the calendar".into()],
                },
                atlas::bus::NewsMember {
                    name: "rss".into(),
                    ok: false,
                    detail: "rss feed returned 503".into(),
                    quality_flags: Vec::new(),
                },
            ],
        });
        let frame = client.frame(120, 36);
        // The catalog row, not the stack row above it, which names macro too.
        let macro_row = line_with(&frame, "[x] macro");
        assert!(macro_row.contains("partial"), "{frame}");
        assert!(line_with(&frame, "[x] rss").contains("rss feed"), "{frame}");
        // The degraded member is not toned as a problem and the dead one is:
        // a `partial:` drawn amber would train an operator to read past the
        // row that is actually a failure.
        let buffer = client.buffer(120, 36);
        assert_eq!(
            body_style_of(&buffer, "partial").fg,
            Some(Theme::truecolor().text_dim)
        );
        assert_eq!(
            body_style_of(&buffer, "rss feed").fg,
            Some(Theme::truecolor().warning)
        );
        // And the *row* is a different claim from its note. A feed that
        // answered a check with a 503 is a source this desk can read and did
        // not get an answer from today; one with no credential is a source it
        // cannot read at all. Merging the two dimmed the first as if it were
        // the second, which is a statement about the configuration rather than
        // about this fetch.
        assert_eq!(
            body_style_of(&buffer, "[x] rss").fg,
            Some(Theme::truecolor().positive),
            "a source that merely did not answer was dimmed as unreadable"
        );
        assert_eq!(
            body_style_of(&buffer, "[x] macro").fg,
            Some(Theme::truecolor().positive)
        );
        assert_eq!(
            body_style_of(&buffer, "[ ] alpaca").fg,
            Some(Theme::truecolor().text_dim),
            "a source the desk cannot read at all is not drawn as available"
        );

        // A check that came back saying nothing is not a clean one.
        assert!(press(&mut client, KeyCode::Char('v')).is_some());
        client.views.wrote(&Wrote::NewsSaved {
            stack: vec!["macro".into(), "rss".into()],
            checked: true,
            verified: Vec::new(),
        });
        let body = content(&client.frame(120, 36));
        assert!(body.contains("did not say what it checked"), "{body}");
        assert!(
            !body.contains("partial"),
            "a stale check outlived its request:\n{body}"
        );
    }

    #[test]
    fn a_save_in_flight_is_drawn_and_a_second_press_says_why() {
        // The state that was set, guarded on, and never painted: a checked
        // save is one live fetch per source and can run for minutes, and a
        // card that drew nothing for it left an operator pressing the key
        // again at a request that was still going.
        let mut client = armed();
        on_news(&mut client);
        assert!(press(&mut client, KeyCode::Char('s')).is_some());
        let body = content(&client.frame(120, 36));
        assert!(body.contains("asking the owner"), "{body}");

        // A second press is refused *out loud*. Silence here reads as a dead
        // key, which is the failure invariant 4 exists for.
        assert_eq!(press(&mut client, KeyCode::Char('s')), None);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("one request at a time"), "{body}");

        // And the checked save names its own cost, because it is a different
        // wait: the owner reads one window per source, and `gdelt` alone is
        // 43-75s of it.
        client.views.wrote(&Wrote::NewsSaved {
            stack: vec!["macro".into(), "rss".into()],
            checked: false,
            verified: Vec::new(),
        });
        client.frame(120, 36);
        assert!(press(&mut client, KeyCode::Char('v')).is_some());
        let body = content(&client.frame(120, 36));
        assert!(body.contains("one window per source"), "{body}");
        assert!(body.contains("minutes"), "{body}");
    }

    #[test]
    fn the_save_wait_is_retired_by_its_own_answer_and_never_by_another_panes() {
        // Every outcome is fanned to every surface, and this card used to
        // retire its wait on any of them. A predictor board is fitted for up to
        // a minute, so a `PredictorRan` lands while a checked save is still
        // running, blanks "asking the owner…", and re-arms the key that sends a
        // second save for one decision.
        let mut client = armed();
        on_news(&mut client);
        assert!(press(&mut client, KeyCode::Char('s')).is_some());
        client.views.wrote(&Wrote::PredictorRan {
            run_id: "pb-1".to_string(),
            models: vec!["ridge:none".to_string()],
            champion: None,
        });
        assert_eq!(
            press(&mut client, KeyCode::Char('s')),
            None,
            "another pane's answer retired this card's wait"
        );
        // Refused out loud, in the card's own words for a request still out.
        assert!(
            content(&client.frame(120, 36)).contains("still asking"),
            "{}",
            content(&client.frame(120, 36))
        );

        // Its own failure does retire it — a card still asking after a timeout
        // reads as a client that has hung — and the sentence is the owner's.
        client.views.wrote(&Wrote::NewsFailed {
            said: "no answer from the owner".to_string(),
        });
        let body = content(&client.frame(120, 36));
        assert!(body.contains("no answer from the owner"), "{body}");
        assert!(press(&mut client, KeyCode::Char('s')).is_some());
    }

    #[test]
    fn a_catalog_taller_than_the_card_keeps_its_note_row_and_bounds_the_cursor() {
        // The card has room for five sources at this height. A sixth used to
        // push the note row off the bottom — which is where every refusal
        // lands — and left the cursor addressing a row no click rectangle
        // covered, so the keyboard and the mouse disagreed about the catalog.
        let mut news = payload();
        news.catalog.push(atlas::model::NewsSource {
            name: Some("newswire".into()),
            tier: Some("wide".into()),
            available: Some(true),
            ..Default::default()
        });
        let mut client = armed_with(news);
        on_news(&mut client);
        let frame = client.frame(120, 36);
        // The overflow is stated rather than silent, exactly as the model
        // switcher states its own.
        // Two hidden, not one: the marker costs a row of its own, which is
        // reserved before anything is dropped rather than after.
        assert!(content(&frame).contains("▾ 2 more"), "{frame}");
        assert!(!content(&frame).contains("newswire"), "{frame}");
        assert!(!content(&frame).contains("gdelt"), "{frame}");

        // The note row survived, and a refusal still reaches it.
        assert_eq!(press(&mut client, KeyCode::Char(' ')), None);
        assert!(
            content(&client.frame(120, 36)).contains("alpaca needs an Alpaca credential"),
            "the note row was pushed off the card:\n{}",
            client.frame(120, 36)
        );

        // The cursor walks the rows the card drew, and stops on the last of
        // them. Four are drawn here, so three arrows from the top lands on
        // `rss` — asserted by the name the frame shows rather than by an
        // index, because the index is exactly what was wrong.
        for _ in 0..3 {
            press(&mut client, KeyCode::Down);
        }
        let frame = client.frame(120, 36);
        // The catalog row, not the stack row above it, which names rss too.
        assert!(
            line_with(&frame, "] rss").contains('▸'),
            "the cursor is not on the fourth drawn row:\n{frame}"
        );

        // And the mouse addresses the same rows the keyboard does. `rss` is
        // the last one drawn; a click on it unticks the name the frame shows
        // there, which is the half that used to disagree — the cursor could
        // run past the click rectangles, so the two pointed at different
        // sources.
        let last = row_of(&client, "[x] rss");
        assert_eq!(click(&mut client, 70, last), None);
        let frame = client.frame(120, 36);
        assert!(line_with(&frame, "] rss").contains("[ ]"), "{frame}");
        assert!(content(&frame).contains("EDITED"), "{frame}");

        // One more arrow leaves the card rather than stepping onto a row
        // nobody can see. This is the half the two used to disagree about:
        // with the cursor bounded by the catalog instead of by the frame, a
        // fifth Down landed on a hidden source and Space would have ticked it.
        press(&mut client, KeyCode::Down);
        let body = content(&client.frame(120, 36));
        assert!(
            !body.contains("s save"),
            "the cursor stepped onto a row the card never drew:\n{body}"
        );
    }

    #[test]
    fn the_first_source_row_is_where_the_card_draws_it() {
        // The offset from the card's top to its first source is arithmetic —
        // the header, the lane and the stack — and a row added above the
        // catalog would move every click rectangle without moving the
        // constant. This pins the two together by name: the row the frame
        // says is alpaca's is the row a click ticks alpaca on.
        let mut client = armed();
        let row = row_of(&client, "alpaca  secondary");
        assert_eq!(click(&mut client, 70, row), None);
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("alpaca needs an Alpaca credential"),
            "a click on alpaca's row addressed some other row:\n{body}"
        );
    }

    #[test]
    fn the_focused_rows_whole_note_is_drawn_where_its_column_cut_it() {
        // The note column is fifteen cells at this width, which is exactly
        // where `needs QLAB_EDGAR_CONTACT` and a verify detail get cut — the
        // two strings an operator acts on. The always-reserved row under the
        // catalog carries the focused row's note whole when it has nothing
        // more urgent to say, so the remedy is one arrow away rather than one
        // resize.
        let mut client = armed();
        on_news(&mut client);
        press(&mut client, KeyCode::Down); // edgar
        let frame = client.frame(120, 36);
        assert!(
            line_with(&frame, "[ ] edgar").contains("needs QLAB_EDG…"),
            "{frame}"
        );
        assert!(
            content(&frame).contains("needs QLAB_EDGAR_CONTACT"),
            "the whole note never reached the row that has room for it:\n{frame}"
        );
        // A refusal outranks it: an operator who has just been refused needs
        // the reason, not a description of the row they are standing on.
        press(&mut client, KeyCode::Up);
        press(&mut client, KeyCode::Char(' '));
        let body = content(&client.frame(120, 36));
        assert!(body.contains("alpaca needs an Alpaca credential"), "{body}");
    }

    #[test]
    fn the_news_card_renders_armed_at_120x36() {
        let mut client = armed();
        on_news(&mut client);
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn the_edited_news_card_renders_at_120x36() {
        let mut client = armed();
        on_news(&mut client);
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Char(' '));
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn a_catalog_taller_than_the_card_renders_at_120x36() {
        let mut news = payload();
        news.catalog.push(atlas::model::NewsSource {
            name: Some("newswire".into()),
            tier: Some("wide".into()),
            available: Some(true),
            ..Default::default()
        });
        let mut client = armed_with(news);
        on_news(&mut client);
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn a_checked_save_in_flight_renders_at_120x36() {
        let mut client = armed();
        on_news(&mut client);
        press(&mut client, KeyCode::Char('v'));
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn the_lane_picker_renders_at_120x36() {
        let mut client = armed();
        press(&mut client, KeyCode::Char('m'));
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn the_contact_box_renders_at_120x36() {
        let mut client = armed();
        on_news(&mut client);
        press(&mut client, KeyCode::Char('c'));
        insta::assert_snapshot!(client.frame(120, 36));
    }
}

// -- how this desk solves, and how many names it may hold --------------------

/// The method payload as the owner serves it, with no cap and nothing wrong.
fn method_payload() -> atlas::model::MethodSettings {
    serde_json::from_str(include_str!("fixtures/desk_method.json")).unwrap()
}

fn with_method(store: &mut Store, method: atlas::model::MethodSettings) {
    store.apply(AppEvent::Method(Box::new(method)), Instant::now());
}

/// A glass window reads the whole card, and still cannot touch it.
#[test]
fn the_method_card_is_read_only_in_a_glass_window() {
    let mut store = harness::fixture_store();
    with_method(&mut store, method_payload());
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    let frame = client.frame(120, 36);
    let body = content(&frame);
    // What is in force: the id, the arm the predictor board calls it by, and
    // the cap — which is `none` rather than a zero this pane invented.
    let now = line_with(&frame, "solving");
    assert!(now.contains("hrp"), "{now}");
    assert!(now.contains("B2"), "{now}");
    assert!(now.contains("cap none"), "{now}");
    // What may be chosen, with the owner's own mark on the one in force.
    assert!(body.contains("risk_parity"), "{body}");
    assert!(body.contains("min_variance"), "{body}");
    // And why the rest may not be. The stage is the owner's word, not this
    // client's: the catalog has `offline` entries too.
    assert!(
        body.contains("not choosable — research stage"),
        "the desk never said why a research method cannot be picked:\n{body}"
    );
    // The rule names no key, because this window would refuse every one.
    assert!(
        body.contains("read-only — cannot change the method"),
        "{body}"
    );
    assert!(!body.contains("m method"), "{body}");
}

/// A cap of nothing is not the absence of a cap, and neither is a missing key.
#[test]
fn a_cap_the_owner_did_not_set_is_none_and_never_a_zero() {
    let mut store = harness::fixture_store();
    let mut method = method_payload();
    method.current.max_holdings = Some(8);
    with_method(&mut store, method);
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    let frame = client.frame(120, 36);
    assert!(line_with(&frame, "solving").contains("cap 8"), "{frame}");

    // And with none, the word rather than a number.
    let mut store = harness::fixture_store();
    with_method(&mut store, method_payload());
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    let frame = client.frame(120, 36);
    assert!(line_with(&frame, "solving").contains("cap none"), "{frame}");
    assert!(!line_with(&frame, "solving").contains("cap 0"), "{frame}");
}

/// The owner's warning is the one thing on this card that is nowhere else, so
/// it is what the lists yield to rather than the other way round.
#[test]
fn the_owners_cap_warning_is_drawn_whole_and_outranks_the_lists() {
    let mut store = harness::fixture_store();
    let mut method = method_payload();
    method.current.max_holdings = Some(5);
    method.warning = Some(
        "Hierarchical risk parity holds every name; a cap of 5 will refuse its plans · a cap \
         of 5 cannot reach 100% at a 40% per-asset cap; every plan will refuse"
            .to_string(),
    );
    with_method(&mut store, method);
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    let buf = client.buffer(120, 36);
    let body = content(&client.frame(120, 36));
    // Both clauses. The owner joins them with ` · ` and this client does not
    // parse the sentence — half of it is a desk an operator believes is fine.
    assert!(body.contains("holds every"), "{body}");
    assert!(
        body.contains("per-asset cap"),
        "the second clause was cut:\n{body}"
    );
    // And it is toned as a warning rather than sitting in the run of the card.
    assert_eq!(
        body_style_of(&buf, "holds every").fg,
        Some(Theme::truecolor().warning),
        "the sentence that says the next plan will be refused is not a quiet fact"
    );
    // The entries gave way for it, and said in one line that they are there
    // and that some of them are research stage — the picker carries every one
    // of them whole, with the owner's rationale beside it.
    assert!(
        body.contains("3 to choose · 3 research stage"),
        "the lists went silent instead of collapsing:\n{body}"
    );
}

/// Nothing has answered yet is not a desk with no method.
#[test]
fn a_card_with_no_answer_yet_says_so_rather_than_inventing_a_method() {
    let frame = settings().frame(120, 36);
    assert!(
        content(&frame).contains("nothing has said how this desk solves"),
        "{frame}"
    );
}

#[test]
fn the_method_card_renders_read_only_at_120x36() {
    let mut store = harness::fixture_store();
    with_method(&mut store, method_payload());
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    insta::assert_snapshot!(client.frame(120, 36));
}

#[cfg(feature = "operator")]
mod method {
    use super::*;
    use atlas::bus::Wrote;
    use atlas::cmd::{Command, MethodChange};
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseButton, MouseEventKind};

    fn armed_with(method: atlas::model::MethodSettings) -> Client {
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        with_method(&mut store, method);
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        client.frame(120, 36);
        client
    }

    fn armed() -> Client {
        armed_with(method_payload())
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

    fn click(client: &mut Client, column: u16, row: u16) -> Option<Command> {
        let acted = atlas::ui::shell::on_mouse(
            crossterm::event::MouseEvent {
                kind: MouseEventKind::Down(MouseButton::Left),
                column,
                row,
                modifiers: KeyModifiers::NONE,
            },
            &mut client.store,
            &mut client.views,
        );
        client.frame(120, 36);
        acted
    }

    /// The screen row a line is drawn on, so a click is aimed at what the
    /// operator is looking at rather than at a coordinate worked out here.
    fn row_of(client: &Client, needle: &str) -> u16 {
        client
            .frame(120, 36)
            .lines()
            .position(|line| line.contains(needle))
            .unwrap_or_else(|| panic!("no row contains {needle}")) as u16
    }

    /// METHOD is the card after NEWS, which is the card after DESK.
    fn on_method(client: &mut Client) {
        // Three, since AUTHORITY leads the left column: DESK, NEWS, AUTHORITY,
        // METHOD.
        press(client, KeyCode::Down);
        press(client, KeyCode::Down);
        press(client, KeyCode::Down);
    }

    #[test]
    fn the_card_states_its_own_two_keys_and_the_pane_walks_onto_it() {
        let mut client = armed();
        on_method(&mut client);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("m method · k cap"), "{body}");
    }

    #[test]
    fn the_picker_sends_the_id_the_operator_chose_and_nothing_else() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('m'));
        let body = content(&client.frame(120, 36));
        // It opens on what the desk already solves with, so Enter here changes
        // nothing — the owner's own `current` mark, never an id match made in
        // this client.
        assert!(body.contains("HOW THIS DESK SOLVES"), "{body}");
        press(&mut client, KeyCode::Down);
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetMethod(MethodChange::Policy(
                "risk_parity".to_string()
            ))),
        );
    }

    /// The cursor cannot land on a research entry at all, which is a stronger
    /// claim than a refusal made after Enter.
    #[test]
    fn a_research_entry_is_listed_and_can_never_be_chosen() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('m'));
        let body = content(&client.frame(120, 36));
        // Named, with the owner's own stage beside it — an operator who read
        // the id in the catalog has to find it here rather than nowhere.
        assert!(body.contains("cardinal_min_variance"), "{body}");
        assert!(body.contains("not choosable — research stage"), "{body}");
        // And the walk stops at the last operational entry, however far it is
        // held down.
        for _ in 0..12 {
            press(&mut client, KeyCode::Down);
        }
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetMethod(MethodChange::Policy(
                "min_variance".to_string()
            ))),
            "the cursor reached a method the owner will not run"
        );
    }

    #[test]
    fn the_cap_box_sends_a_number_and_clears_on_an_empty_one() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        press(&mut client, KeyCode::Char('5'));
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetMethod(MethodChange::Cap(Some(5)))),
        );

        // An empty box clears the override rather than sending nothing: the
        // owner reads `null` as "put the mandate's own cap back".
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetMethod(MethodChange::Cap(None))),
        );

        // And so does a typed zero, which is the other spelling an operator
        // reaches for — and is never a cap of nothing.
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        press(&mut client, KeyCode::Char('0'));
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetMethod(MethodChange::Cap(None))),
        );
    }

    #[test]
    fn a_cap_above_the_universe_is_refused_here_with_the_number_still_in_the_box() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        // The fixture desk watches seven names.
        press(&mut client, KeyCode::Char('9'));
        press(&mut client, KeyCode::Char('9'));
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            None,
            "a cap above the universe reached the owner"
        );
        let body = content(&client.frame(120, 36));
        assert!(body.contains("watches 7 names"), "{body}");
        // The box is still open with what was typed in it: the remedy is to
        // type a different number, not to reopen and retype.
        assert!(body.contains("99"), "the box lost what was typed:\n{body}");
        // A number inside the bound goes — both sides of the guard, in the
        // same box, because a refusal that also swallowed the good case would
        // pass a test that only pressed the bad one.
        press(&mut client, KeyCode::Backspace);
        press(&mut client, KeyCode::Backspace);
        press(&mut client, KeyCode::Char('5'));
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetMethod(MethodChange::Cap(Some(5)))),
            "the refusal swallowed a cap the desk can hold"
        );
    }

    /// A key the field cannot take changes nothing — and *nothing* is the
    /// claim: not the value, not the caret, not the bound under it.
    #[test]
    fn a_key_the_cap_field_cannot_take_leaves_it_exactly_as_it_was() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        press(&mut client, KeyCode::Char('4'));
        assert!(line_with(&client.frame(120, 36), "names").contains("4▏"));
        let before = client.frame(120, 36);
        for stray in ['q', '-', '.', ' '] {
            assert_eq!(press(&mut client, KeyCode::Char(stray)), None);
        }
        assert_eq!(
            client.frame(120, 36),
            before,
            "a key the field cannot take moved something on the screen"
        );
        // And Enter still sends what survived, so the guard did not wedge it.
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetMethod(MethodChange::Cap(Some(4)))),
        );
    }

    #[test]
    fn one_request_at_a_time_and_the_card_says_it_is_waiting() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        press(&mut client, KeyCode::Char('4'));
        assert!(press(&mut client, KeyCode::Enter).is_some());
        // A second Enter is refused out loud rather than queueing a second
        // `mandate_override` row for one decision — and the box says so, which
        // is where the operator is looking.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("asking the owner"),
            "a second press said nothing:\n{body}"
        );
        // The card behind it is waiting too, so closing the box does not lose
        // the fact that a request is out.
        press(&mut client, KeyCode::Esc);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("asking the owner"), "{body}");
        // And the key that would open a second decision is refused for the
        // same reason, on the card rather than in a box.
        assert_eq!(press(&mut client, KeyCode::Char('m')), None);
        assert!(content(&client.frame(120, 36)).contains("asking the owner"));
    }

    #[test]
    fn the_cap_wait_is_retired_by_its_own_answer_and_never_by_another_panes() {
        // The same defect as NEWS', and worse where it lands: a wait retired by
        // another pane's answer re-arms the Enter that writes a second
        // `mandate_override` row for one decision.
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        press(&mut client, KeyCode::Char('4'));
        assert!(press(&mut client, KeyCode::Enter).is_some());
        client.views.wrote(&Wrote::PredictorRan {
            run_id: "pb-1".to_string(),
            models: vec!["ridge:none".to_string()],
            champion: None,
        });
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            None,
            "another pane's answer retired this card's wait"
        );
        assert!(
            content(&client.frame(120, 36)).contains("asking the owner"),
            "{}",
            content(&client.frame(120, 36))
        );

        // Its own failure does retire it, and the box keeps what was typed:
        // nothing here says the change did not land, so pressing again is the
        // remedy.
        client.views.wrote(&Wrote::MethodFailed {
            said: "the owner did not answer: connection refused".to_string(),
        });
        let body = content(&client.frame(120, 36));
        assert!(body.contains("connection refused"), "{body}");
        assert!(press(&mut client, KeyCode::Enter).is_some());
    }

    #[test]
    fn the_owners_refusal_stays_on_the_card_and_keeps_what_was_typed() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        press(&mut client, KeyCode::Char('6'));
        assert!(press(&mut client, KeyCode::Enter).is_some());
        client.views.wrote(&Wrote::MethodRefused {
            said: "max_holdings must be between 1 and 20 (the size of the mandated universe)"
                .to_string(),
        });
        let frame = client.frame(120, 36);
        let body = content(&frame);
        assert!(body.contains("between 1 and 20"), "{body}");
        // The box's own field row, and the caret that follows the value — not
        // `contains("6")` over the whole frame, which is a digit on nine other
        // rows of this desk and an assertion that could never fail. Every cap
        // the fixture universe allows is one character wide, so *where* the
        // digit is drawn is the only thing that can carry the claim.
        assert!(
            line_with(&frame, "names").contains("6▏"),
            "the box lost what was typed:\n{frame}"
        );

        // And an answer that says the desk moved closes it — what the card then
        // draws is the owner's own refetched answer, not a copy of the request.
        client.views.wrote(&Wrote::MethodSet {
            policy: "hrp".to_string(),
            cap: Some(3),
            warning: None,
        });
        let body = content(&client.frame(120, 36));
        assert!(!body.contains("between 1 and 20"), "{body}");
        assert!(!body.contains("asking the owner"), "{body}");
    }

    #[test]
    fn a_click_on_a_stated_key_word_does_exactly_what_the_key_does() {
        let mut client = armed();
        on_method(&mut client);
        let rule = row_of(&client, "m method · k cap");
        let line = line_with(&client.frame(120, 36), "m method · k cap").to_string();
        // Cells, not bytes: the frame is full of box-drawing characters and a
        // byte offset would aim the click several columns short of the word.
        let column = |word: &str| {
            let byte = line
                .find(word)
                .unwrap_or_else(|| panic!("no {word} on {line}"));
            line[..byte].chars().count() as u16
        };

        // The word `method` opens the picker, exactly as `m` does — the same
        // `KeyCode` through the same router, so a click can never mean
        // something the card does not say.
        click(&mut client, column("method"), rule);
        assert!(
            content(&client.frame(120, 36)).contains("HOW THIS DESK SOLVES"),
            "clicking `method` did not open the picker"
        );
        press(&mut client, KeyCode::Esc);

        // And `cap` opens the box the key opens.
        click(&mut client, column("cap"), rule);
        assert!(
            content(&client.frame(120, 36)).contains("HOW MANY NAMES"),
            "clicking `cap` did not open the box:\n{}",
            client.frame(120, 36)
        );
    }

    /// Both keys exist only in a window the desk armed, and the overlay says so.
    #[test]
    fn the_two_keys_are_offered_only_where_they_would_be_answered() {
        for code in ["Char('m')", "Char('k')"] {
            assert!(
                atlas::input::KEYMAP
                    .iter()
                    .filter(
                        |binding| binding.code == code && binding.action.starts_with("on METHOD")
                    )
                    .all(|binding| binding.writes),
                "{code} on METHOD is offered to a window that would refuse it"
            );
        }
        // And `m` is claimed three times on this pane — the lane, the models
        // and the method — which is legal because a key means whatever the
        // focused card says it means, and is pinned here because it is the one
        // collision on the workstation.
        assert_eq!(
            atlas::input::KEYMAP
                .iter()
                .filter(|binding| binding.code == "Char('m')"
                    && binding.source == atlas::input::Source::View(atlas::store::ViewId::Settings))
                .count(),
            3,
        );
        // An unarmed window claims neither.
        let mut client = armed();
        client.store.posture = Posture::Glass;
        assert_eq!(press(&mut client, KeyCode::Char('m')), None);
        assert_eq!(press(&mut client, KeyCode::Char('k')), None);
    }

    #[test]
    fn the_focused_method_card_renders_at_120x36() {
        let mut client = armed();
        on_method(&mut client);
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn the_method_picker_renders_at_120x36() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('m'));
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn the_cap_box_renders_at_120x36() {
        let mut client = armed();
        on_method(&mut client);
        press(&mut client, KeyCode::Char('k'));
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn the_warned_method_card_renders_at_120x36() {
        let mut method = method_payload();
        method.current.max_holdings = Some(5);
        method.warning = Some(
            "Hierarchical risk parity holds every name; a cap of 5 will refuse its plans · a \
             cap of 5 cannot reach 100% at a 40% per-asset cap; every plan will refuse"
                .to_string(),
        );
        let mut client = armed_with(method);
        on_method(&mut client);
        insta::assert_snapshot!(client.frame(120, 36));
    }
}

// -- the three authorities the operator lends atlas --------------------------

/// The owner's own answer, with every right granted.
fn rights_payload(web: bool, workflows: bool, build: bool) -> atlas::model::AtlasRights {
    serde_json::from_str(&format!(
        r#"{{"rights": {{"web": {web}, "workflows": {workflows}, "build": {build}}},
             "path": "/state/atlas_rights.json"}}"#
    ))
    .unwrap()
}

fn with_rights(store: &mut Store, rights: atlas::model::AtlasRights) {
    store.apply(AppEvent::Rights(Box::new(rights)), Instant::now());
}

/// A glass window reads all three switches, and still cannot touch one.
#[test]
fn the_rights_are_read_only_in_a_glass_window() {
    let mut store = harness::fixture_store();
    with_rights(&mut store, rights_payload(true, false, true));
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    let frame = client.frame(120, 36);
    assert!(line_with(&frame, "web").contains("on"), "{frame}");
    assert!(line_with(&frame, "workflows").contains("off"), "{frame}");
    assert!(line_with(&frame, "build").contains("on"), "{frame}");
    // The card says what it cannot do rather than offering a key it would
    // refuse — the greyed-affordance claim this client declines everywhere.
    let body = content(&frame);
    assert!(body.contains("read-only — cannot grant a right"), "{body}");
    assert!(!body.contains("space grants a right"), "{body}");
}

#[cfg(feature = "operator")]
mod rights {
    use super::*;
    use atlas::bus::Wrote;
    use atlas::cmd::{Command, Right};
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseButton, MouseEventKind};

    fn armed_with(rights: Option<atlas::model::AtlasRights>) -> Client {
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        if let Some(rights) = rights {
            with_rights(&mut store, rights);
        }
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        client.frame(120, 36);
        client
    }

    fn armed() -> Client {
        armed_with(Some(rights_payload(true, true, true)))
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

    fn click(client: &mut Client, column: u16, row: u16) -> Option<Command> {
        let acted = atlas::ui::shell::on_mouse(
            crossterm::event::MouseEvent {
                kind: MouseEventKind::Down(MouseButton::Left),
                column,
                row,
                modifiers: KeyModifiers::NONE,
            },
            &mut client.store,
            &mut client.views,
        );
        client.frame(120, 36);
        acted
    }

    fn row_of(client: &Client, needle: &str) -> u16 {
        client
            .frame(120, 36)
            .lines()
            .position(|line| line.contains(needle))
            .unwrap_or_else(|| panic!("no row contains {needle}")) as u16
    }

    /// Walk the focus onto MODELS. Down rather than a jump, because that is the
    /// only way an operator reaches it — and it crosses NEWS' row cursor on the
    /// way, which is what makes the count more than the card index.
    fn on_models(client: &mut Client) {
        for _ in 0..24 {
            if body_style_of(&client.buffer(120, 36), "MODELS").fg
                == Some(Theme::truecolor().accent)
            {
                return;
            }
            press(client, KeyCode::Down);
        }
        panic!("the focus never reached MODELS:\n{}", client.frame(120, 36));
    }

    #[test]
    fn each_right_reads_where_it_stands_and_a_granted_one_is_not_a_quiet_fact() {
        let client = armed_with(Some(rights_payload(true, false, true)));
        let buffer = client.buffer(120, 36);
        // The accent is the tone of an authority Atlas is *carrying*, and the
        // secondary is the one it is not: a card that toned both alike would
        // make the operator read the word rather than the card.
        assert_eq!(
            body_style_of(&buffer, "on · chat").fg,
            Some(Theme::truecolor().accent)
        );
        assert_eq!(
            body_style_of(&buffer, "off · refused for chat").fg,
            Some(Theme::truecolor().text_secondary)
        );
        assert_eq!(
            body_style_of(&buffer, "on · the /build key").fg,
            Some(Theme::truecolor().accent)
        );
        // And the line that keeps the three rows honest. Without it a right
        // shown as granted reads as a gate the desk holds shut, and two of the
        // three are not gates at all.
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("nothing here binds a non-chat caller"),
            "{body}"
        );
        // And the row is scoped to the caller the owner actually gates. Its
        // gate returns early on anything that is not the desk chat, and this
        // very card is drawn one row above a note saying the reasoner is not
        // claude — a reasoner that reaches the owner with no origin header at
        // all. "owner-enforced" invited exactly the wrong reading.
        assert!(
            line_with(&client.frame(120, 36), "workflows").contains("refused for chat"),
            "{body}"
        );
    }

    #[test]
    fn nothing_has_answered_is_the_missing_mark_and_never_the_owners_default() {
        // The owner's documented default is all three granted. Drawing it
        // before the fetch answers would be this client asserting a desk state
        // nobody confirmed — the `max_weight` lesson one card over.
        let client = armed_with(None);
        let frame = client.frame(120, 36);
        for field in ["web", "workflows", "build"] {
            assert!(line_with(&frame, field).contains("--"), "{field}: {frame}");
        }
    }

    #[test]
    fn a_rights_file_the_owner_cannot_read_is_its_sentence_and_never_three_dashes() {
        // Invariant 4. The reader's refusal names the file and the remedy, and
        // it is the whole message: three `--` rows over it would bury the one
        // thing an operator can act on, and three `on` rows would be a lie
        // about a file nobody could read.
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        let mut broken = rights_payload(true, true, true);
        broken.rights = Default::default();
        broken.error = Some(
            "the owner answered 500: /state/atlas_rights.json is not readable as JSON; \
             delete it to restore the defaults"
                .to_string(),
        );
        with_rights(&mut store, broken);
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        let body = content(&client.frame(120, 36));
        assert!(body.contains("atlas_rights.json"), "{body}");
        assert!(body.contains("delete it"), "{body}");
        assert!(!body.contains("nothing here binds"), "{body}");

        // And the key refuses rather than writing on top of a file the owner
        // could not read.
        let mut client = client;
        on_models(&mut client);
        assert_eq!(press(&mut client, KeyCode::Char(' ')), None);
    }

    #[test]
    fn a_rights_sentence_that_will_not_fit_is_replaced_and_never_cut_mid_path() {
        // The card's own rule, applied to its own longest sentence: a clipped
        // remedy is a fix an operator cannot run. Fitting the reader's refusal
        // to whatever slack there happened to be produced
        // `the owner answered 500: /state/atlas…` on a short column — the
        // status kept and the path, the reason and the remedy all gone.
        let said = "the owner answered 500: /state/qlab/atlas_rights.json is not readable \
                    as JSON (Expecting value: line 1 column 1 (char 0)); delete it to restore \
                    the defaults ([build, web, workflows] all true), or set it from the \
                    desk's rights panel";
        let broken = |width: u16, height: u16| {
            let mut store = harness::fixture_store();
            store.posture = Posture::Operator;
            let mut rights = rights_payload(true, true, true);
            rights.rights = Default::default();
            rights.error = Some(said.to_string());
            with_rights(&mut store, rights);
            let mut client = Client::new(store);
            client.press(KeyCode::Char('9'));
            content(&client.frame(width, height))
        };

        // Where it fits, whole — the file, the reason and the remedy.
        let roomy = broken(200, 36);
        assert!(roomy.contains("atlas_rights.json"), "{roomy}");
        assert!(roomy.contains("rights panel"), "{roomy}");

        // Where it does not, the fixed sentence in its place. Never the
        // section's other deferral — "3 rights need 4 rows" would send this
        // operator to resize a terminal over a file they have to fix.
        let tight = broken(120, 30);
        assert!(
            tight.contains("the rights file could not be read"),
            "{tight}"
        );
        assert!(!tight.contains("rights need"), "{tight}");
        // And no half of the owner's sentence survives beside it: a path cut
        // mid-way is a file an operator cannot go and look at.
        assert!(!tight.contains("atlas_rights"), "{tight}");
        assert!(!tight.contains("answered 500"), "{tight}");
    }

    #[test]
    fn space_sends_the_opposite_of_what_the_owner_said_for_the_row_under_the_cursor() {
        // Three presses, one per row, because the cursor is an index and a test
        // that only ever pressed the first row would pass on a card that had
        // wired every row to `web`.
        for (steps, right, held) in [
            (0, Right::Web, true),
            (1, Right::Workflows, false),
            (2, Right::Build, true),
        ] {
            let mut client = armed_with(Some(rights_payload(true, false, true)));
            on_models(&mut client);
            for _ in 0..steps {
                press(&mut client, KeyCode::Down);
            }
            assert_eq!(
                press(&mut client, KeyCode::Char(' ')),
                Some(Command::SetRight {
                    field: right,
                    value: !held,
                }),
                "row {steps} sent the wrong right"
            );
        }
    }

    #[test]
    fn a_right_the_owner_has_not_named_has_no_opposite_and_is_refused_out_loud() {
        // Nothing is inverted from a value this client made up: a toggle
        // computed from the owner's documented default would be this client
        // deciding what the desk's rights are and then writing that decision
        // into the owner's file.
        let mut client = armed_with(None);
        on_models(&mut client);
        assert_eq!(press(&mut client, KeyCode::Char(' ')), None);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("nothing has said where web stands"), "{body}");
    }

    #[test]
    fn one_toggle_at_a_time_and_the_card_says_which_one_it_is_waiting_on() {
        // The owner records one audit row per changed field, so a held key
        // would put two decisions on the record for one press. Refused *out
        // loud*: a key that silently did nothing reads as a dead card.
        let mut client = armed();
        on_models(&mut client);
        assert!(press(&mut client, KeyCode::Char(' ')).is_some());
        let body = content(&client.frame(120, 36));
        assert!(body.contains("asking the owner"), "{body}");
        assert!(body.contains("web"), "{body}");
        assert_eq!(
            press(&mut client, KeyCode::Char(' ')),
            None,
            "a second press queued a write while one was in flight"
        );
    }

    #[test]
    fn the_wait_is_retired_by_its_own_answer_and_never_by_another_keys_failure() {
        // The defect `bus::Wrote::PredictorFailed` was added for, on a card one
        // keystroke from a second write: a wait retired by an unrelated broken
        // request re-arms Space over a toggle still in flight — one decision,
        // two audit rows.
        let mut client = armed();
        on_models(&mut client);
        assert!(press(&mut client, KeyCode::Char(' ')).is_some());
        client.views.wrote(&Wrote::Failed {
            what: "save the news sources".to_string(),
            said: "the owner did not answer".to_string(),
        });
        client.views.wrote(&Wrote::RightFailed {
            field: "build",
            said: "the owner did not answer".to_string(),
        });
        assert_eq!(
            press(&mut client, KeyCode::Char(' ')),
            None,
            "another request's failure retired this card's wait"
        );
        // Its own outcome does retire it, and the sentence says which right it
        // was about.
        client.views.wrote(&Wrote::RightFailed {
            field: "web",
            said: "the owner did not answer".to_string(),
        });
        client.frame(120, 36);
        assert!(press(&mut client, KeyCode::Char(' ')).is_some());
    }

    #[test]
    fn the_owners_refusal_stands_on_the_card_and_the_row_is_left_where_it_was() {
        let mut client = armed();
        on_models(&mut client);
        press(&mut client, KeyCode::Char(' '));
        client.views.wrote(&Wrote::RightRefused {
            field: "web",
            said: "banana is not a right this desk has".to_string(),
        });
        let body = content(&client.frame(120, 36));
        assert!(body.contains("banana is not a right"), "{body}");
        // Nothing was copied onto the row: the owner's answer is what the card
        // draws, and it still says the right stands where it did.
        assert!(
            line_with(&client.frame(120, 36), "web").contains("on"),
            "{body}"
        );
    }

    #[test]
    fn the_cursor_walks_the_three_rows_and_walks_out_of_the_card_at_either_end() {
        // Both ends, because the clamp is a comparison and a case that only
        // reaches one of them proves nothing. A cursor that wrapped would send
        // an operator holding an arrow back to `web` from `build`; one that
        // trapped would put UNIVERSE beyond reach of the arrows entirely.
        let mut client = armed();
        on_models(&mut client);
        for _ in 0..2 {
            press(&mut client, KeyCode::Down);
        }
        assert_eq!(
            press(&mut client, KeyCode::Char(' ')),
            Some(Command::SetRight {
                field: Right::Build,
                value: false,
            }),
            "two downs did not land on the last right"
        );
        // One more Down leaves the card rather than wrapping to `web`.
        let mut client = armed();
        on_models(&mut client);
        for _ in 0..3 {
            press(&mut client, KeyCode::Down);
        }
        assert_eq!(
            body_style_of(&client.buffer(120, 36), "UNIVERSE").fg,
            Some(Theme::truecolor().accent),
            "the cursor did not walk out of the bottom of the card"
        );
        // And back in from below puts it on the edge it was entered from, so a
        // held arrow reads as one continuous walk.
        press(&mut client, KeyCode::Up);
        assert_eq!(
            press(&mut client, KeyCode::Char(' ')),
            Some(Command::SetRight {
                field: Right::Build,
                value: false,
            }),
            "re-entering from below did not land on the last right"
        );
    }

    #[test]
    fn a_click_on_a_row_grants_it_rather_than_only_moving_the_cursor() {
        // A click that only moved a cursor would leave the operator reaching
        // for the keyboard to finish what they started — the rule the NEWS
        // rows already state. And it is the same `Command` through the same
        // producer, so a pointer can never grant what a key could not.
        let mut client = armed_with(Some(rights_payload(true, false, true)));
        on_models(&mut client);
        let row = row_of(&client, "workflows    off");
        assert_eq!(
            click(&mut client, 60, row),
            Some(Command::SetRight {
                field: Right::Workflows,
                value: true,
            })
        );
    }

    #[test]
    fn a_click_on_a_rights_row_is_never_read_as_a_news_row() {
        // Two cards now record row rectangles, and the index alone is ambiguous
        // between them: a MODELS row read as NEWS' would tick a news source
        // from a click on a right, which is a change nobody chose.
        let mut client = armed_with(Some(rights_payload(true, true, true)));
        on_models(&mut client);
        let before = content(&client.frame(120, 36));
        let row = row_of(&client, "build");
        assert_eq!(
            click(&mut client, 60, row),
            Some(Command::SetRight {
                field: Right::Build,
                value: false,
            })
        );
        // The news draft is untouched — no edit mark appeared on the card one
        // band up.
        assert_eq!(
            before.contains("news ·edited"),
            content(&client.frame(120, 36)).contains("news ·edited")
        );
    }

    /// An armed desk reasoning on whatever `backend` names.
    ///
    /// **Both orderings here are load-bearing, and the test this replaced had
    /// neither.** `Store::apply` recomputes the posture from the payload it
    /// just installed, so a window armed *before* a snapshot carrying no desk
    /// mode is glass again by the time the card draws; and that same snapshot
    /// opens the startup door, which swallows the keystroke that reaches
    /// SETTINGS. Its claude case therefore asserted the absence of a sentence
    /// on a read-only DESK view — an assertion no wording could have failed.
    fn armed_reasoning_on(backend: &str) -> Client {
        armed_reasoning_with(backend, "")
    }

    /// The same desk, plus whatever `extra` adds to the `llm` payload.
    fn armed_reasoning_with(backend: &str, extra: &str) -> Client {
        let mut store = harness::fixture_store();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_str::<Snapshot>(&format!(
                    r#"{{"llm": {{"reasoner": {{"backend": "{backend}", "model": "inherit"}},
                                  "workforce": {{"backend": "claude", "model": "inherit"}},
                                  "reasoner_enabled": true{extra}}}}}"#
                ))
                .unwrap(),
            )),
            Instant::now(),
        );
        store.posture = Posture::Operator;
        with_rights(&mut store, rights_payload(true, true, true));
        harness::no_door(&mut store);
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        client
    }

    /// One backend the owner reports as down, with the reason it gave.
    fn down(backend: &str, reason: &str) -> String {
        format!(
            r#", "availability": [{{"name": "{backend}", "available": false, "reason": "{reason}"}}]"#
        )
    }

    #[test]
    fn a_note_the_card_cannot_hold_is_counted_and_never_dropped() {
        // The cliff, from the side that used to be silent. A backend name long
        // enough to take the sentence past the card's own width costs three
        // rows against the one slack row there is, and the note used to be
        // struck from the queue *before* the count could see it — so the card
        // drew the reading, said nothing, and an operator had no way to know a
        // sentence existed. It is counted now, like every reason.
        let body = content(&armed_reasoning_with("ollamaollama", "").frame(120, 36));
        assert!(body.contains("▾ 1 more"), "{body}");
        assert!(!body.contains("chat: ollamaollama"), "{body}");
        // The reading it qualifies is untouched: the note is what gives way.
        assert!(body.contains("ollamaollama · inherit"), "{body}");
    }

    #[test]
    fn the_count_is_every_sentence_off_screen_and_not_only_the_owners() {
        // One slack row, an owner sentence that fits it, and the note. Both are
        // off screen — the marker takes the row — and the number says two. It
        // said `▾ 1 more` while hiding both, which is the same silence with a
        // number in front of it.
        let said = "no ollama at 127.0.0.1:11434";
        let body = content(&armed_reasoning_with("ollama", &down("ollama", said)).frame(120, 36));
        assert!(body.contains("▾ 2 more"), "{body}");
        assert!(!body.contains(said), "{body}");
        assert!(!body.contains("chat: ollama ·"), "{body}");
    }

    #[test]
    fn the_width_that_wraps_the_rights_line_defers_the_section_rather_than_clipping_the_note() {
        // 118 columns is the last width at which the card is as wide as the
        // line the rights end with. At 117 it is one narrower, that line wraps,
        // and the section costs five rendered rows against the four it used to
        // charge — so the card overran its own inner rect and the `Paragraph`
        // clipped the last thing on it, which was this note. No count, no
        // marker, nothing: the exact failure the marker exists to prevent,
        // reached through the one cost on this card that was a literal.
        let wide = content(&armed_reasoning_on("ollama").frame(118, 36));
        assert!(
            wide.contains("nothing here binds a non-chat caller"),
            "{wide}"
        );
        assert!(
            wide.contains("chat: ollama · /cli: claude's verb"),
            "{wide}"
        );
        assert!(!wide.contains('▾'), "{wide}");

        // One column narrower, the section says what it could not draw and the
        // note is still on the card. Both, and in that order: a frame that had
        // merely stopped drawing the rights would pass the second assertion.
        let tight = content(&armed_reasoning_on("ollama").frame(117, 36));
        assert!(tight.contains("▾ 3 rights need 6 rows"), "{tight}");
        assert!(
            tight.contains("chat: ollama · /cli: claude's verb"),
            "{tight}"
        );
        // And the reading above it is untouched at either width.
        assert!(tight.contains("ollama · inherit"), "{tight}");
    }

    #[test]
    fn a_local_reasoner_desk_is_told_which_half_it_keeps_and_which_it_cannot_have() {
        // The fixture desk reasons on ollama, and `/cli` runs the desk's own
        // Claude verb — so that key is refused by name here while the chat is
        // answered by the local model regardless. The card is where that
        // refusal sends the operator, so it says the same thing from this side.
        let client = armed();
        let body = content(&client.frame(120, 36));
        assert!(
            body.contains("chat: ollama · /cli: claude's verb"),
            "{body}"
        );
        // And not the sentence it replaced, which claimed the key opens claude
        // on a desk where it opens nothing.
        assert!(!body.contains("/cli, /build:"), "{body}");
    }

    #[test]
    fn a_claude_desk_is_told_the_tab_has_two_bodies() {
        // The shape ruling, on the one desk that can hold both: `/cli` is a
        // terminal inside the tab now, not a hand-off that takes the screen,
        // so the card says ATLAS is either — where before it said nothing at
        // all to a claude desk.
        let body = content(&armed_reasoning_on("claude").frame(120, 36));
        assert!(
            body.contains("ATLAS: the chat, or a /cli terminal"),
            "{body}"
        );
        // One line, not two: the local desk's sentence is the other branch of
        // the same row, and both on one card would be one row past the slack.
        assert!(!body.contains("chat: claude ·"), "{body}");
    }

    #[test]
    fn neither_sentence_costs_more_than_the_one_row_the_card_has_for_it() {
        // 37 cells is the widest this card draws whole (the cliff is pinned by
        // `a_reason_that_will_not_fit_whole_is_counted_rather_than_cut_in_half`),
        // and a note past it is counted — which would replace the sentence with
        // a number on every desk rather than on a badly-configured one.
        for (desk, said) in [
            ("ollama", "chat: ollama · /cli: claude's verb"),
            ("claude", "ATLAS: the chat, or a /cli terminal"),
        ] {
            let body = content(&armed_reasoning_on(desk).frame(120, 36));
            // The anchor first, or the count assertion below passes on a card
            // that drew no sentence at all.
            assert!(body.contains(said), "{desk}: {body}");
            assert!(!body.contains('▾'), "{desk}: {body}");
        }
    }

    #[test]
    fn an_unarmed_window_sends_nothing_on_space_however_the_rows_are_drawn() {
        // The posture, not the build: a featured binary the desk has not armed
        // hears nothing, and the rows it draws are a reading rather than an
        // affordance.
        let mut store = harness::fixture_store();
        with_rights(&mut store, rights_payload(true, true, true));
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        client.frame(120, 36);
        for _ in 0..10 {
            assert_eq!(press(&mut client, KeyCode::Down), None);
            assert_eq!(press(&mut client, KeyCode::Char(' ')), None);
        }
        let body = content(&client.frame(120, 36));
        assert!(body.contains("web"), "{body}");
    }

    #[test]
    fn the_focused_card_renders_its_rights_at_120x36() {
        let mut client = armed_with(Some(rights_payload(true, false, true)));
        on_models(&mut client);
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn a_rights_file_the_owner_could_not_read_renders_at_120x36() {
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        let mut broken = rights_payload(true, true, true);
        broken.rights = Default::default();
        broken.error = Some(
            "the owner answered 500: /state/atlas_rights.json is not readable as JSON \
             (Expecting value: line 1 column 1); delete it to restore the defaults"
                .to_string(),
        );
        with_rights(&mut store, broken);
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
        insta::assert_snapshot!(client.frame(120, 36));
    }
}

// -- the standing authority a fill may happen under --------------------------

/// The owner's own answer: one live grant and nothing suspending it.
///
/// Every field the route names, so a card that quietly read one of them as
/// absent fails here rather than rendering `--` on a desk that books itself.
fn authority_payload(books_today: i64, anomalies: &[&str]) -> atlas::model::DeskAuthority {
    let said: Vec<String> = anomalies.iter().map(|s| format!("{s:?}")).collect();
    serde_json::from_str(&format!(
        r#"{{"grant": {{"grant_id": "9f31c0aa4b7d2e61", "mode": "PAPER_AUTO",
              "allowed_universe": ["ACWI", "SPY", "QQQ", "XLK", "XLF"],
              "max_notional": 25000.0, "max_turnover": 0.35, "max_orders": 8,
              "max_books_per_day": 3, "valid_from": "2026-08-30T14:00:00+00:00",
              "expires_at": "2026-09-06T14:00:00+00:00", "granted_by": "operator",
              "books_today": {books_today}, "days_left": 6}},
            "anomalies": [{}]}}"#,
        said.join(", ")
    ))
    .unwrap()
}

/// The owner answered and holds no grant at all.
fn no_grant_payload(anomalies: &[&str]) -> atlas::model::DeskAuthority {
    let said: Vec<String> = anomalies.iter().map(|s| format!("{s:?}")).collect();
    serde_json::from_str(&format!(
        r#"{{"grant": null, "anomalies": [{}]}}"#,
        said.join(", ")
    ))
    .unwrap()
}

fn with_authority(store: &mut Store, authority: atlas::model::DeskAuthority) {
    store.apply(AppEvent::Authority(Box::new(authority)), Instant::now());
}

fn settings_with_authority(authority: atlas::model::DeskAuthority) -> Client {
    let mut store = harness::fixture_store();
    with_authority(&mut store, authority);
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    client
}

#[test]
fn a_desk_with_no_standing_grant_says_so_and_names_where_one_comes_from() {
    // Absence stated, never blank: "nothing books itself" and "this card has
    // gone quiet" are two readings an operator may not have to tell apart.
    let frame = settings_with_authority(no_grant_payload(&[])).frame(120, 36);
    let body = content(&frame);
    assert!(body.contains("AUTHORITY"), "{body}");
    assert!(line_with(&frame, "standing").contains("none"), "{frame}");
    assert!(body.contains("nothing books itself"), "{body}");
    // The remedy, because this client does not compose a grant: the route is
    // where the ceilings are set, and a card that said only "none" would leave
    // an operator with no next step at all.
    assert!(body.contains("/api/desk/authority"), "{body}");
}

#[test]
fn a_live_grant_shows_what_is_left_of_it_and_not_only_what_was_set() {
    // What an operator decides on. `max_books_per_day` is what was granted;
    // "one left today" is what is about to happen, and a card that drew only
    // the ceiling would say a desk with nothing left could still book.
    let frame = settings_with_authority(authority_payload(2, &[])).frame(120, 36);
    let books = line_with(&frame, "books");
    assert!(books.contains("1 of 3"), "{books}");
    assert!(line_with(&frame, "standing").contains("6 d"), "{frame}");
    // And the ceilings themselves, all four.
    let per_book = line_with(&frame, "per book");
    assert!(per_book.contains("$25.00K"), "{per_book}");
    assert!(per_book.contains("35.0%"), "{per_book}");
    assert!(per_book.contains("8 legs"), "{per_book}");
    assert!(
        line_with(&frame, "universe").contains("ACWI SPY QQQ"),
        "{frame}"
    );
    assert!(
        line_with(&frame, "standing").contains("PAPER_AUTO"),
        "{frame}"
    );
}

#[test]
fn a_grant_with_no_books_left_today_says_zero_rather_than_going_quiet() {
    let frame = settings_with_authority(authority_payload(3, &[])).frame(120, 36);
    assert!(line_with(&frame, "books").contains("0 of 3"), "{frame}");
}

#[test]
fn an_anomaly_is_named_on_the_card_and_never_reads_as_a_revocation() {
    // A suspended grant is still there and resumes when the condition clears.
    // A card that said "revoked" would send an operator to re-grant something
    // they still hold.
    let frame =
        settings_with_authority(authority_payload(1, &["the book is halted"])).frame(120, 36);
    let body = content(&frame);
    let paused = line_with(&frame, "paused by");
    assert!(paused.contains("the book is halted"), "{paused}");
    assert!(!body.contains("revoked"), "{body}");
    // Still granted, and it still says so — the days it has left are what says
    // the grant outlives the pause.
    assert!(line_with(&frame, "standing").contains("6 d"), "{frame}");
}

#[test]
fn several_anomalies_are_counted_rather_than_dropped() {
    let frame = settings_with_authority(authority_payload(
        1,
        &[
            "the book is halted",
            "the reconcile is dirty",
            "an order was rejected",
        ],
    ))
    .frame(120, 36);
    let paused = line_with(&frame, "paused by");
    assert!(paused.contains("the book is halted"), "{paused}");
    assert!(paused.contains("+2"), "{paused}");
}

#[test]
fn an_anomaly_with_no_grant_says_what_it_would_pause_rather_than_what_it_paused() {
    // Nothing is suspended when nothing was granted, and a card that said
    // "paused by" over an empty grant would report a state the desk is not in.
    let frame = settings_with_authority(no_grant_payload(&["the book is halted"])).frame(120, 36);
    let row = line_with(&frame, "would pause");
    assert!(row.contains("the book is halted"), "{row}");
}

#[test]
fn a_card_nothing_has_answered_for_says_so_rather_than_drawing_a_grant() {
    // Invariant 4 in the frame: a route that has not answered is not a desk
    // with no grant, and the two must not render the same.
    let frame = settings().frame(120, 36);
    let body = content(&frame);
    assert!(
        body.contains("nothing has said what may book itself"),
        "{body}"
    );
    assert!(!body.contains("PAPER_AUTO"), "{body}");
}

#[test]
fn a_ceiling_the_owner_did_not_send_is_absent_rather_than_zero() {
    // The pane's own rule, on the card where breaking it is worst: a
    // `max_notional` defaulted to `0.0` reads as a grant that authorises
    // nothing, and a `books_today` defaulted to `0` reads as a full day's
    // budget still to spend. Both are statements about what happens on the next
    // beat that nobody made.
    let bare: atlas::model::DeskAuthority =
        serde_json::from_str(r#"{"grant": {"grant_id": "abc"}, "anomalies": []}"#).unwrap();
    let mut store = harness::fixture_store();
    with_authority(&mut store, bare);
    let mut client = Client::new(store);
    client.press(KeyCode::Char('9'));
    let frame = client.frame(120, 36);
    assert!(line_with(&frame, "books").contains("--"), "{frame}");
    let per_book = line_with(&frame, "per book");
    assert!(per_book.contains("-- · -- · --"), "{per_book}");
    // And no invented zero anywhere on the card.
    assert!(!per_book.contains("0.0%"), "{per_book}");
    assert!(!line_with(&frame, "books").contains("0 of"), "{frame}");
}

#[test]
fn the_universe_names_yield_to_the_pause_and_come_back_with_it() {
    // The card's one contested row, pinned in both directions. A pause changes
    // what happens on the next beat and the names do not, so the anomaly wins
    // the row while it lasts — and the names return the moment it clears,
    // rather than being lost for the rest of the grant.
    // On the label, not on the names: the UNIVERSE card in the other column
    // draws the same symbols, and a frame-wide search would pass on that one.
    let paused =
        settings_with_authority(authority_payload(1, &["the book is halted"])).frame(120, 36);
    assert!(
        !content(&paused).contains(" universe "),
        "the names did not yield to the pause:\n{paused}"
    );
    let clear = settings_with_authority(authority_payload(1, &[])).frame(120, 36);
    assert!(
        line_with(&clear, "universe").contains("ACWI SPY QQQ XLK XLF"),
        "{clear}"
    );
}

#[test]
fn the_authority_card_is_read_only_in_a_glass_window() {
    // The posture, not the build. A window the desk did not arm is told what
    // the card cannot do rather than offered a key it would refuse.
    let body = content(&settings_with_authority(authority_payload(1, &[])).frame(120, 36));
    assert!(body.contains("read-only — cannot revoke a grant"), "{body}");
    assert!(!body.contains("R revokes"), "{body}");
}

#[cfg(feature = "operator")]
mod authority {
    use super::*;
    use atlas::bus::Wrote;
    use atlas::cmd::Command;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseButton, MouseEventKind};

    fn armed_with(authority: Option<atlas::model::DeskAuthority>) -> Client {
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        if let Some(authority) = authority {
            with_authority(&mut store, authority);
        }
        let mut client = Client::new(store);
        client.press(KeyCode::Char('9'));
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

    /// Walk the focus onto AUTHORITY. Down rather than a jump, because that is
    /// the only way an operator reaches it.
    fn on_authority(client: &mut Client) {
        for _ in 0..30 {
            press(client, KeyCode::Down);
        }
        for _ in 0..30 {
            press(client, KeyCode::Up);
        }
        // DESK, NEWS, then AUTHORITY — the card leads the left column.
        press(client, KeyCode::Down);
        press(client, KeyCode::Down);
    }

    #[test]
    fn one_keystroke_revokes_and_nothing_is_typed_to_confirm_it() {
        // **The deliberate opposite of the BOOK box.** Withdrawing authority is
        // the safe direction: the modal exists to stop a fill nobody checked,
        // and a box between an operator and "stop" is a hazard, not a guard.
        let mut client = armed_with(Some(authority_payload(1, &[])));
        on_authority(&mut client);
        let acted = press(&mut client, KeyCode::Char('R'));
        assert!(
            matches!(acted, Some(Command::RevokeAuthority { .. })),
            "{acted:?}"
        );
        // And no box came up on the way. A modal on this workstation owns the
        // keyboard and draws its own border, so both are checked: a card that
        // opened one would swallow the next keystroke and would be visible.
        assert!(
            !client.views.typing(atlas::store::ViewId::Settings),
            "a box took the keyboard between R and stop"
        );
        let frame = client.frame(120, 36);
        assert!(
            !frame.contains('┌'),
            "a box opened between R and stop:\n{frame}"
        );
    }

    #[test]
    fn the_key_belongs_to_the_card_and_not_to_the_pane() {
        // On any other card `R` is nobody's, so it stays free for whoever
        // claims it next rather than revoking from a card that says nothing
        // about a grant.
        let mut client = armed_with(Some(authority_payload(1, &[])));
        assert_eq!(press(&mut client, KeyCode::Char('R')), None);
    }

    #[test]
    fn nothing_is_revoked_when_the_desk_holds_no_grant() {
        // Said out loud rather than swallowed: a key that silently did nothing
        // reads as a dead card.
        let mut client = armed_with(Some(no_grant_payload(&[])));
        on_authority(&mut client);
        assert_eq!(press(&mut client, KeyCode::Char('R')), None);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("no grant to revoke"), "{body}");
    }

    #[test]
    fn one_request_at_a_time_and_the_second_press_says_so() {
        let mut client = armed_with(Some(authority_payload(1, &[])));
        on_authority(&mut client);
        assert!(press(&mut client, KeyCode::Char('R')).is_some());
        assert_eq!(press(&mut client, KeyCode::Char('R')), None);
        let body = content(&client.frame(120, 36));
        assert!(body.contains("asking the owner"), "{body}");
    }

    #[test]
    fn the_owners_refusal_stands_on_the_card_that_asked() {
        let mut client = armed_with(Some(authority_payload(1, &[])));
        on_authority(&mut client);
        press(&mut client, KeyCode::Char('R'));
        client.views.wrote(&Wrote::AuthorityRefused {
            said: "a chat may not revoke a grant".to_string(),
        });
        let body = content(&client.frame(120, 36));
        assert!(body.contains("a chat may not revoke a grant"), "{body}");
        // And the wait is retired, so the key works again.
        assert!(press(&mut client, KeyCode::Char('R')).is_some());
    }

    #[test]
    fn a_request_that_never_landed_does_not_leave_the_card_waiting() {
        let mut client = armed_with(Some(authority_payload(1, &[])));
        on_authority(&mut client);
        press(&mut client, KeyCode::Char('R'));
        client.views.wrote(&Wrote::AuthorityFailed {
            said: "connection refused".to_string(),
        });
        let body = content(&client.frame(120, 36));
        assert!(body.contains("connection refused"), "{body}");
        assert!(press(&mut client, KeyCode::Char('R')).is_some());
    }

    #[test]
    fn a_click_on_the_word_does_exactly_what_the_key_does() {
        let mut client = armed_with(Some(authority_payload(1, &[])));
        on_authority(&mut client);
        let frame = client.frame(120, 36);
        let row = frame
            .lines()
            .position(|line| line.contains("R revokes"))
            .expect("the rule names the key") as u16;
        let column = frame
            .lines()
            .nth(row as usize)
            .and_then(|line| line.find("revokes"))
            .expect("the word is on the rule") as u16;
        let acted = atlas::ui::shell::on_mouse(
            crossterm::event::MouseEvent {
                kind: MouseEventKind::Down(MouseButton::Left),
                column,
                row,
                modifiers: KeyModifiers::NONE,
            },
            &mut client.store,
            &mut client.views,
        );
        assert!(
            matches!(acted, Some(Command::RevokeAuthority { .. })),
            "{acted:?}"
        );
    }

    #[test]
    fn the_armed_card_renders_at_120x36() {
        let mut client = armed_with(Some(authority_payload(2, &["the book is halted"])));
        on_authority(&mut client);
        insta::assert_snapshot!(client.frame(120, 36));
    }
}
