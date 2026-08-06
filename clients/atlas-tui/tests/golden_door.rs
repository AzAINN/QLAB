//! The startup door, pinned as the frame an operator actually opens on.
//!
//! The door is the one surface that is up before anything else, so the pins are
//! on the whole frame it draws over the shell — and on the two postures, which
//! draw different doors for the same desk.

mod harness;

use atlas::bus::{AppEvent, Channel};
use atlas::model::Snapshot;
use atlas::store::Store;
use crossterm::event::KeyCode;
use harness::Client;
use std::time::Instant;

/// A desk that has answered, carrying whatever `desk_mode` block is given —
/// watched by a window that declined its authority.
///
/// `--glass` rather than a bare featured window, because the two are no longer
/// the same door: a window that *could* be armed and is watching a desk nobody
/// has answered the arming question for is asked it, and these are the tests
/// about the door for a window that cannot be armed at all. The vetoed window
/// is that door in both legs, which is what keeps these pins one pair of eyes
/// rather than two.
fn answered(desk_mode: &str) -> Store {
    let json = format!(r#"{{"portfolio": {{"equity": 1.0}}{desk_mode}}}"#);
    let mut store = Store::default();
    let now = Instant::now();
    store.forced_glass = true;
    store.apply(AppEvent::ConnUp(Channel::Owner), now);
    store.apply(
        AppEvent::Snapshot(Box::new(serde_json::from_str::<Snapshot>(&json).unwrap())),
        now,
    );
    store
}

/// The desk the store-driven door is about: the owner answered and said nothing
/// about which desk this is.
fn unsaid() -> Store {
    answered("")
}

#[test]
fn a_desk_that_has_not_said_what_it_is_pointed_at_opens_a_door() {
    let client = Client::new(unsaid());
    let frame = client.frame(120, 36);
    assert!(frame.contains("THIS DESK"), "{frame}");
}

#[test]
fn a_desk_that_said_what_it_is_pointed_at_opens_no_door() {
    let client = Client::new(answered(
        r#", "desk_mode": {"data": "synthetic", "book": "simulated", "label": "SYNTHETIC"}"#,
    ));
    let frame = client.frame(120, 36);
    assert!(!frame.contains("THIS DESK"), "{frame}");
}

#[test]
fn the_owners_own_word_for_unchosen_is_what_opens_the_door_and_true_is_what_shuts_it() {
    // The arm this door was specified against, and could not have until the
    // owner learned to say it: `chosen: false` is a desk serving the fallback
    // nobody named, which is byte-identical to a chosen `synthetic · simulated`
    // in every other field.
    let named = |chosen: &str| {
        format!(
            r#", "desk_mode": {{"data": "synthetic", "book": "simulated",
                                "label": "SYNTHETIC"{chosen}}}"#
        )
    };
    assert!(
        Client::new(answered(&named(r#", "chosen": false"#)))
            .frame(120, 36)
            .contains("THIS DESK"),
        "the owner said nobody chose this desk and nothing asked"
    );
    assert!(
        !Client::new(answered(&named(r#", "chosen": true"#)))
            .frame(120, 36)
            .contains("THIS DESK"),
        "a desk something named was asked about anyway"
    );
    // The third arm, and the one that makes this additive: an owner too old to
    // carry the field is not an owner reporting an unchosen desk. Reading its
    // silence as `false` would open a door on every desk that has already
    // answered, which is the loudest possible regression on an old owner.
    assert!(
        !Client::new(answered(&named("")))
            .frame(120, 36)
            .contains("THIS DESK"),
        "an owner that cannot say was read as one saying no"
    );
}

#[test]
fn a_read_only_door_says_whether_the_desk_it_names_was_ever_chosen() {
    // The glass door's two sentences, which the owner's flag split in two. It
    // used to have one for a payload with no block at all; a desk that names
    // itself *and* says nobody chose it would otherwise read as a settled desk
    // this window had opened a door over for no reason.
    let unchosen = Client::new(answered(
        r#", "desk_mode": {"data": "synthetic", "book": "simulated",
                           "label": "SYNTHETIC", "chosen": false}"#,
    ))
    .frame(120, 36);
    assert!(unchosen.contains("SYNTHETIC"), "{unchosen}");
    assert!(
        unchosen.contains("nobody has chosen"),
        "the door names a desk without saying it is the fallback:\n{unchosen}"
    );
    // And the payload that says nothing at all keeps its own sentence.
    let silent = Client::new(unsaid()).frame(120, 36);
    assert!(
        silent.contains("did not say which desk this is"),
        "{silent}"
    );
    assert!(!silent.contains("nobody has chosen"), "{silent}");
}

#[test]
fn the_read_only_door_names_what_could_answer_it() {
    // A glass window cannot choose, so the door states what it would take
    // rather than offering rows that do nothing. What it would take is no
    // longer a launch flag: the desk's own arming answer is, and a door that
    // still named `--operator` would send an operator after a flag that no
    // longer exists.
    let mut client = Client::new(unsaid());
    let frame = client.frame(120, 36);
    assert!(frame.contains("arms a window"), "{frame}");
    assert!(
        !frame.contains("--operator"),
        "a retired flag survives:\n{frame}"
    );
    // And any key dismisses it, the way the help overlay does.
    client.press(KeyCode::Char('x'));
    assert!(!client.frame(120, 36).contains("THIS DESK"));
}

#[test]
fn the_flag_asks_again_on_a_desk_that_did_answer() {
    // The arm that carries the whole feature on a real owner: the payload
    // cannot say whether anybody ever chose, so `--pick` is what asks.
    let mut store = harness::fixture_store();
    assert!(store.door().is_none(), "the fixture desk named itself");
    store.pick();
    assert!(store.door().is_some());
    // And it is one door per run, whatever the owner keeps saying: the
    // store-driven condition stays true for as long as that owner is up.
    let mut client = Client::new(unsaid());
    client.frame(120, 36);
    client.press(KeyCode::Esc);
    let frame = client.frame(120, 36);
    assert!(!frame.contains("THIS DESK"), "{frame}");
    client.store.apply(
        AppEvent::Snapshot(Box::new(
            serde_json::from_str::<Snapshot>(r#"{"portfolio": {"equity": 2.0}}"#).unwrap(),
        )),
        Instant::now(),
    );
    assert!(
        client.store.door().is_none(),
        "the next poll re-opened a door that was answered"
    );
}

#[test]
fn a_terminal_too_small_for_the_door_is_told_what_it_would_take() {
    // Refuse-to-open, then retire-on-shrink: the same discipline WORKFORCE's
    // picker and SETTINGS' login form are held to, because an armed control an
    // operator cannot see is worse than one that says what it would cost.
    let mut client = Client::new(unsaid());
    let frame = client.frame(120, 15);
    // Both floors and both actual dimensions, in that order — the sentence read
    // "has 120×15" of a terminal 15 rows tall, which is the pair the two
    // numbers before it are compared against.
    assert!(
        frame.contains("needs 16 rows and 48 columns; this terminal has 15 rows and 120 columns"),
        "{frame}"
    );
    // The remedy, which wraps onto the second of the three rows the refusal
    // takes — asserted in the piece that survives the wrap, because a fragment
    // spanning the break would fail on a resize rather than on a regression.
    assert!(
        frame.contains("and /model ask the same questions"),
        "{frame}"
    );
    // The keystroke that arrives while it does not fit takes it away, rather
    // than being swallowed by a box nobody can see.
    client.press(KeyCode::Down);
    assert!(client.store.door().is_none());
    assert!(!client.frame(120, 36).contains("THIS DESK"));
}

#[test]
fn the_read_only_door_renders_at_120x36() {
    insta::assert_snapshot!(Client::new(unsaid()).frame(120, 36));
}

#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::cmd::{Command, ModelChoice};
    use atlas::model::LlmCatalog;
    use atlas::store::{Posture, ViewId};
    use crossterm::event::{KeyEvent, KeyModifiers};

    /// The captured desk, asked again with `--pick` by a window that can
    /// answer — and drawn once, because the door reads its floor off the frame
    /// it was last given, exactly as the runtime draws before its first event.
    fn door() -> Client {
        let mut store = harness::fixture_store();
        store.posture = Posture::Operator;
        store.apply(
            AppEvent::Backends(
                serde_json::from_str::<LlmCatalog>(include_str!("fixtures/llm_backends.json"))
                    .unwrap(),
            ),
            Instant::now(),
        );
        store.pick();
        let client = Client::new(store);
        client.frame(120, 36);
        client
    }

    /// One keystroke through the shell's own router, then the frame the runtime
    /// would have drawn after it.
    fn press(client: &mut Client, code: KeyCode) -> Option<Command> {
        let acted = atlas::ui::shell::on_key(
            KeyEvent::new(code, KeyModifiers::NONE),
            &mut client.store,
            &mut client.views,
        );
        client.frame(120, 36);
        acted
    }

    #[test]
    fn the_first_question_renders_at_120x36() {
        insta::assert_snapshot!(door().frame(120, 36));
    }

    #[test]
    fn an_armed_door_too_big_for_the_terminal_is_retired_by_the_key_it_cannot_answer() {
        // The glass half of this is above, and it proves the *message*; this is
        // the half that proves the *retire*. A read-only door is dismissed by
        // any key whatever the frame, so without an armed window the guard that
        // takes an unfittable question away could be deleted and every test
        // would stay green — which is exactly what a mutation found.
        let mut client = door();
        let frame = client.frame(120, 15);
        assert!(frame.contains("the startup door needs"), "{frame}");
        assert_eq!(
            atlas::ui::shell::on_key(
                KeyEvent::new(KeyCode::Down, KeyModifiers::NONE),
                &mut client.store,
                &mut client.views,
            ),
            None
        );
        assert!(
            client.store.door().is_none(),
            "a question nobody can see is still holding the keyboard"
        );
        // And the other side of the floor: at a frame that fits, the same key
        // moves the cursor and the door stays up.
        let mut roomy = door();
        press(&mut roomy, KeyCode::Down);
        assert!(roomy.store.door().is_some());
    }

    #[test]
    fn the_book_question_is_disclosed_by_the_answer_above_it() {
        // The frame's half of the two-step disclosure, which is the virtue that
        // survives: the books do not exist while the door points at synthetic
        // data, so the pair the owner's `__post_init__` raises on cannot be
        // composed here however the keys are pressed.
        let mut client = door();
        let frame = client.frame(120, 36);
        assert!(frame.contains("SYNTHETIC"), "{frame}");
        assert!(!frame.contains("ALPACA PAPER"), "{frame}");
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Enter);
        let live = client.frame(120, 36);
        assert!(live.contains("ALPACA PAPER"), "{live}");
        // And the fixture desk has no login the owner can read, which the row
        // says without refusing the choice — the gate that used to refuse it
        // was authority this door never had.
        assert!(
            live.contains("no Alpaca login the desk can read"),
            "the owner's fact did not survive the gate's removal:\n{live}"
        );
        assert!(
            live.contains("no ALPACA_API_KEY_ID"),
            "the owner's own description is missing:\n{live}"
        );
    }

    #[test]
    fn the_first_question_with_the_real_book_chosen_renders_at_120x36() {
        // The state no golden reached, and the one the box's width is set by:
        // `ALPACA PAPER  chosen  — the paper account; a fill still needs you`
        // fills the inner width exactly, so a cell more anywhere in it wraps
        // the row that says a fill still needs a human. Two keystrokes past the
        // frame the door opens on, which is why the line-level pins alone never
        // saw it.
        let mut client = door();
        for code in [
            KeyCode::Down,  // the LIVE row
            KeyCode::Enter, // choose it — the two books are disclosed by it
            KeyCode::Down,  // SIMULATED
            KeyCode::Down,  // ALPACA PAPER
            KeyCode::Enter,
        ] {
            press(&mut client, code);
        }
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn ctrl_c_quits_from_under_the_door_and_from_under_a_confirmation_box() {
        // The one precedence claim in the router that is *reachable*, and the
        // behaviour change that came with hoisting it: Ctrl-C used to sit below
        // the confirmation box, whose challenge field read it as a typed `c`.
        // Raw mode disables ISIG, so this key arrives as a keystroke or not at
        // all, and the reflex every operator has must work whatever is up.
        //
        // The door and the box together are not a state this client can reach —
        // a door claims every key while it is up, so no view can open a modal
        // underneath it — which is exactly why the ordering between *those two*
        // is defensive and is not claimed as precedence anywhere.
        let ctrl_c = |client: &mut Client| {
            atlas::ui::shell::on_key(
                KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
                &mut client.store,
                &mut client.views,
            )
        };
        let open_a_box = |client: &mut Client| {
            client.store.nav.view = ViewId::Audit;
            client
                .views
                .confirm_mut(ViewId::Audit)
                .expect("AUDIT owns a modal slot")
                .open(
                    atlas::ui::widgets::confirm::Modal::action("HALT", vec![]),
                    atlas::ui::widgets::confirm::Pending::Approve("a1".into()),
                );
        };

        let mut both = door();
        open_a_box(&mut both);
        assert!(both.store.door().is_some());
        assert_eq!(ctrl_c(&mut both), Some(Command::Quit), "under the door");

        // And under the box alone, which is the state an operator does meet —
        // the door has been answered and BOOK or AUDIT has a question up.
        let mut boxed = door();
        boxed.store.settle_door();
        open_a_box(&mut boxed);
        assert_eq!(
            ctrl_c(&mut boxed),
            Some(Command::Quit),
            "the challenge field ate the operator's only exit reflex"
        );
    }

    #[test]
    fn the_second_question_renders_what_the_backends_serve_at_120x36() {
        let mut client = door();
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Down);
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::Backends),
            "moving on did not ask what the backends serve"
        );
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn keeping_the_models_sends_the_desk_and_nothing_else() {
        // The skip: Enter on the last row applies the pair the first question
        // settled and asks for no model change at all.
        let mut client = door();
        for code in [KeyCode::Down, KeyCode::Down, KeyCode::Enter] {
            press(&mut client, code);
        }
        let rows = 6; // four claude tiers, one ollama model, and the claude row
        for _ in 0..rows + 2 {
            press(&mut client, KeyCode::Down);
        }
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::DeskMode {
                data: "synthetic".into(),
                book: "simulated".into()
            })
        );
        assert!(client.store.door().is_none());
        // The desk it was already on, and the login it did not need.
        assert_eq!(client.store.nav.view, ViewId::Desk);
    }

    #[test]
    fn choosing_a_model_sends_the_pair_the_catalog_named() {
        let mut client = door();
        for code in [KeyCode::Down, KeyCode::Down, KeyCode::Enter] {
            press(&mut client, code);
        }
        // The default is the current config: this desk's reasoner runs
        // ollama · qwen2.5:7b, so Enter on the row the cursor opened on sends
        // exactly what the desk already has.
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
        // And the row above it is the last claude tier — the pair comes off the
        // catalog, never out of anything typed.
        press(&mut client, KeyCode::Up);
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::SetLlm {
                surface: "reasoner".into(),
                choice: ModelChoice::Pair {
                    backend: "claude".into(),
                    model: "haiku".into()
                }
            })
        );
        assert!(
            client.store.door().is_some(),
            "choosing one surface closed the question about the other"
        );
    }

    #[test]
    fn a_desk_that_cannot_reach_its_own_book_lands_in_the_login_form() {
        // The third step, end to end and through the shell: the door hands the
        // keyboard to SETTINGS' own box rather than drawing one of its own.
        let mut store = harness::fixture_store();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({
                    // The desk arms this window; the posture is re-derived from
                    // every snapshot, so setting it before one would be undone.
                    "posture": {"armed": true, "chosen": true},
                    "desk_mode": {"data": "live", "book": "alpaca",
                                  "label": "LIVE · ALPACA BOOK", "offline": false,
                                  "credentials": "no ALPACA_API_KEY_ID in the environment or .env",
                                  "credentials_ok": false}
                }))
                .unwrap(),
            )),
            Instant::now(),
        );
        store.pick();
        let mut client = Client::new(store);
        client.frame(120, 36);
        // Keep the desk as it is — four rows down is the one that moves on,
        // because a live desk has both book rows disclosed — and then keep the
        // models as they are.
        for code in [
            KeyCode::Down,
            KeyCode::Down,
            KeyCode::Down,
            KeyCode::Down,
            KeyCode::Enter,
        ] {
            press(&mut client, code);
        }
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::DeskMode {
                data: "live".into(),
                book: "alpaca".into()
            })
        );
        assert_eq!(client.store.nav.view, ViewId::Settings);
        let frame = client.frame(120, 36);
        assert!(
            frame.contains("ALPACA LOGIN"),
            "the door did not hand over:\n{frame}"
        );
        assert!(!frame.contains("THIS DESK"), "{frame}");
    }

    /// A desk that has named itself, watched by a window that could be armed,
    /// with the arming question in whichever state the caller is asking about.
    ///
    /// Deliberately not `door()`: that window is already armed, and the whole
    /// subject here is the one that is not. The desk mode is `chosen` so the
    /// only question left on this door is the arming one.
    fn asked_about(posture: &str) -> Client {
        let mut store = Store::default();
        let now = Instant::now();
        store.apply(AppEvent::ConnUp(Channel::Owner), now);
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_str::<Snapshot>(&format!(
                    r#"{{"portfolio": {{"equity": 1.0}},
                         "desk_mode": {{"data": "synthetic", "book": "simulated",
                                        "label": "SYNTHETIC", "chosen": true}}{posture}}}"#
                ))
                .unwrap(),
            )),
            now,
        );
        let client = Client::new(store);
        client.frame(120, 36);
        client
    }

    /// The three states of the owner's `posture` block, as it serves them.
    const NEVER_ASKED: &str = r#", "posture": {"armed": false, "chosen": false}"#;
    const READ_ONLY: &str = r#", "posture": {"armed": false, "chosen": true}"#;

    #[test]
    fn a_desk_never_asked_about_posture_is_asked_once() {
        let frame = asked_about(NEVER_ASKED).frame(120, 36);
        assert!(frame.contains("ARM THIS DESK"), "{frame}");
        assert!(
            frame.contains("read-only"),
            "the safe answer is named, not implied:\n{frame}"
        );
    }

    #[test]
    fn escape_leaves_the_desk_read_only() {
        // The door's own rule, one question further in: the key a human presses
        // to get out of the way can never be the one that arms a workstation.
        let mut client = asked_about(NEVER_ASKED);
        assert_eq!(
            press(&mut client, KeyCode::Esc),
            Some(Command::Posture { armed: false })
        );
        assert!(client.store.door().is_none(), "Esc left the door up");
    }

    #[test]
    fn a_desk_that_answered_is_not_asked_again() {
        // `chosen: true` with `armed: false` is a desk somebody deliberately
        // left read-only. It gets the statement every unarmable window gets,
        // and never the question again.
        let frame = asked_about(READ_ONLY).frame(120, 36);
        assert!(!frame.contains("ARM THIS DESK"), "{frame}");
        assert!(frame.contains("GLASS"), "{frame}");
    }

    #[test]
    fn arming_the_desk_sends_the_answer_and_waits_for_the_owner_to_say_so() {
        // No client-side latch: the answer goes to the owner, the owner
        // records it, and the *next snapshot* is what arms this window. A door
        // that closed on its own keystroke would be claiming an authority
        // nothing had granted yet.
        let mut client = asked_about(NEVER_ASKED);
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::Posture { armed: true })
        );
        assert!(
            client.frame(120, 36).contains("ARM THIS DESK"),
            "the door answered for the owner"
        );
        // And when the owner says so, the same door moves on to the questions
        // the window may now answer.
        client.store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_str::<Snapshot>(
                    r#"{"portfolio": {"equity": 1.0},
                        "posture": {"armed": true, "chosen": true},
                        "desk_mode": {"data": "synthetic", "book": "simulated",
                                      "label": "SYNTHETIC", "chosen": true}}"#,
                )
                .unwrap(),
            )),
            Instant::now(),
        );
        let frame = client.frame(120, 36);
        assert!(!frame.contains("ARM THIS DESK"), "{frame}");
        assert!(frame.contains("SYNTHETIC"), "{frame}");
    }

    #[test]
    fn a_window_that_vetoed_its_own_authority_is_told_rather_than_asked() {
        // The other conjunct: `--glass` is this window declining an authority
        // the desk may be offering, so asking it to arm the desk would be
        // offering a row that changes nothing about what it may do.
        let mut client = asked_about(NEVER_ASKED);
        client.store.forced_glass = true;
        let frame = client.frame(120, 36);
        assert!(!frame.contains("ARM THIS DESK"), "{frame}");
        assert!(frame.contains("GLASS"), "{frame}");
    }

    #[test]
    fn a_window_the_desk_has_already_armed_is_never_asked_to_arm_it() {
        // The third conjunct, which is not implied by the flag beside it: an
        // owner can serve `armed: true` with a `chosen` this client cannot
        // read as an answer, and a door that asked anyway would put a modal
        // over a workstation that is already writing — asking an operator to
        // grant authority they are visibly holding.
        let client = asked_about(r#", "posture": {"armed": true, "chosen": false}"#);
        assert!(client.store.posture.writes(), "the desk armed this window");
        assert!(
            client.store.door().is_none(),
            "an armed window was asked to arm itself"
        );
        assert!(!client.frame(120, 36).contains("ARM THIS DESK"));
    }

    #[test]
    fn the_arming_question_renders_at_120x36() {
        insta::assert_snapshot!(asked_about(NEVER_ASKED).frame(120, 36));
    }

    #[test]
    fn escape_out_of_the_first_question_never_lands_in_the_login() {
        // The inversion of the walk above, and the reason the third step's
        // condition is about the book that was *settled on* rather than the one
        // the desk arrived with: Esc points this desk at the simulated book,
        // which has no login to be broken.
        let mut store = harness::fixture_store();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({
                    // The desk arms this window; the posture is re-derived from
                    // every snapshot, so setting it before one would be undone.
                    "posture": {"armed": true, "chosen": true},
                    "desk_mode": {"data": "live", "book": "alpaca",
                                  "label": "LIVE · ALPACA BOOK", "offline": false,
                                  "credentials": "no ALPACA_API_KEY_ID in the environment or .env",
                                  "credentials_ok": false}
                }))
                .unwrap(),
            )),
            Instant::now(),
        );
        store.pick();
        let mut client = Client::new(store);
        client.frame(120, 36);
        assert_eq!(
            press(&mut client, KeyCode::Esc),
            Some(Command::DeskMode {
                data: "synthetic".into(),
                book: "simulated".into()
            })
        );
        assert_eq!(client.store.nav.view, ViewId::Desk);
    }
}
