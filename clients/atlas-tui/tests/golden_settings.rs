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
        client.press(KeyCode::Char('7'));
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
        ok.press(KeyCode::Char('7'));
        assert_eq!(
            body_style_of(&ok.buffer(120, 36), "paper key ending").fg,
            Some(Theme::truecolor().text_secondary),
            "a login the owner can read is not a warning"
        );
    }
}
