//! PREDICTORS: the full board, and the three answers the pane can honestly give.
//!
//! The claims: a pane that has not heard from the owner says it is asking — not
//! that the desk has no board; a desk that never ran the board says so in the
//! *owner's* words; and a served board renders every model with the lane the
//! owner filed it under, in the owner's ranking order. The wire shape is
//! exercised too — every fixture arrives through serde, so a payload field the
//! model stopped decoding fails here rather than rendering as absent.

mod harness;

use atlas::bus::AppEvent;
use atlas::model::PredictorDetail;
use crossterm::event::KeyCode;
use harness::{content, line_with, Client};
use std::time::Instant;

/// The fixture desk, switched to PREDICTORS.
fn predictors() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('6'));
    client
}

/// A board handed to the store the way the poller hands one over.
fn with_board(json: &str) -> Client {
    let mut client = predictors();
    let board = serde_json::from_str::<PredictorDetail>(json).unwrap();
    client
        .store
        .apply(AppEvent::PredictorDetail(Box::new(board)), Instant::now());
    client
}

#[test]
fn the_pane_says_it_is_asking_rather_than_claiming_an_empty_desk() {
    // No `PredictorDetail` has arrived. "Not asked yet" and "the desk has no
    // board" are different facts, and only the owner may state the second.
    let frame = predictors().frame(120, 36);
    assert!(
        content(&frame).contains("asking the owner for the board"),
        "{frame}"
    );
}

#[test]
fn a_desk_that_never_ran_the_board_says_so_in_the_owners_own_words() {
    let frame = with_board(
        r#"{"status": "never_ran", "models": [],
            "reason": "no predictor board has been run on this desk"}"#,
    )
    .frame(120, 36);
    assert!(
        content(&frame).contains("no predictor board has been run"),
        "{frame}"
    );
}

/// One served board: the unaugmented baseline, a mapped champion, and the
/// control that sits in the kernel family — the row a lane-by-family client
/// would misfile.
const BOARD: &str = r#"{
    "status": "ok", "run_id": "abcd1234", "as_of": "2026-08-20",
    "admitted_any": true, "champion_established": true,
    "champion": "groupwise:angle_zz", "baseline": "ridge:none",
    "n_obs": 420, "n_folds": 12, "target": "realized_vol",
    "horizon_days": 21, "embargo_days": 5,
    "reason": "groupwise:angle_zz was admitted and beat its own selection null (p=0.04).",
    "lane": "The augmented lane is the angle and ZZ feature maps.",
    "caveats": ["evaluated on the offline synthetic panel"],
    "models": [
        {"model_id": "groupwise:angle_zz", "augmented": true, "is_champion": true,
         "mean_ic": 0.061, "delta_mean_ic_vs_baseline": 0.024,
         "paired_t_vs_baseline": 2.4, "significant": true,
         "wins_vs_baseline": 9, "negative_folds": 2,
         "per_fold": [0.02, 0.08, -0.01, 0.06, 0.09, 0.05, -0.02, 0.07,
                      0.1, 0.04, 0.06, 0.08]},
        {"model_id": "ridge:none", "augmented": false, "is_baseline": true,
         "mean_ic": 0.037, "delta_mean_ic_vs_baseline": 0.0},
        {"model_id": "kernel:linear", "family": "kernel", "augmented": false,
         "mean_ic": 0.037, "delta_mean_ic_vs_baseline": 0.0}
    ]
}"#;

#[test]
fn the_board_renders_every_model_with_the_lane_the_owner_filed() {
    let client = with_board(BOARD);
    let frame = client.frame(120, 36);

    // Anchored on the marks rather than the ids: the verdict sentence above
    // the table also names the champion, and the row is the claim under test.
    let champion = line_with(&frame, "★CHAMP");
    assert!(champion.contains("groupwise:angle_zz"), "{champion}");
    assert!(champion.contains("quant"), "{champion}");
    assert!(champion.contains("+0.024"), "{champion}");
    assert!(champion.contains("9/2"), "{champion}");
    // The fold spark drew from the per-fold series rather than absence.
    assert!(
        champion.contains('▁') || champion.contains('█'),
        "{champion}"
    );

    let baseline = line_with(&frame, "BASE");
    assert!(baseline.contains("ridge:none"), "{baseline}");

    // The misfiling this pane exists to avoid: kernel family, control lane.
    let control = line_with(&frame, "kernel:linear");
    assert!(control.contains("ctrl"), "{control}");
    assert!(!control.contains("quant"), "{control}");

    // The owner's verdict and its honesty rider, verbatim.
    assert!(
        content(&frame).contains("beat its own selection null"),
        "{frame}"
    );
    assert!(
        content(&frame).contains("offline synthetic panel"),
        "{frame}"
    );
}

#[test]
fn the_predictors_view_renders_the_board_at_120x36() {
    insta::assert_snapshot!(with_board(BOARD).frame(120, 36));
}

#[test]
fn a_glass_pane_offers_no_run_word_at_120x36() {
    // The monitoring frame, pinned in both legs. `--no-default-features`
    // compiles no picker, no key and no word; a featured build the desk has
    // not armed must draw the *same* pane, because the posture is what decides
    // and not the binary. One golden, checked by both.
    let mut client = with_board(BOARD);
    client.store.posture = atlas::store::Posture::Glass;
    let frame = client.frame(120, 36);
    // The title row's own word, anchored to the title so the right-aligned
    // "· run <id>" note beside it — which every posture draws — cannot pass
    // for the affordance this asserts is absent.
    assert!(
        !line_with(&frame, "PREDICTOR BOARD").contains("PREDICTOR BOARD run"),
        "{frame}"
    );
    insta::assert_snapshot!(frame);
}

// -- running a lane ---------------------------------------------------------

/// What an armed operator can do from this pane, and what it refuses.
///
/// The claims: the picker offers the lanes the *owner* named and marks the
/// baseline as one it runs anyway; Enter sends exactly the lane under the
/// cursor; a second `r` while the owner is fitting is refused out loud rather
/// than putting a second board on the wire; a declined lane keeps the choice
/// and renders the owner's own sentence; and a board that came back closes the
/// box and says what won.
#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::bus::Wrote;
    use atlas::cmd::Command;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers, MouseButton, MouseEventKind};

    /// The fixture desk, armed, on PREDICTORS, with a board served.
    fn armed() -> Client {
        let mut client = with_board(BOARD);
        client.store.posture = Posture::Operator;
        // One frame before any key, exactly as the runtime draws one before it
        // reads its first event: the title row publishes the `run` word there.
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

    /// The picker, open on the board's top-ranked lane.
    fn picking() -> Client {
        let mut client = armed();
        press(&mut client, KeyCode::Char('r'));
        client
    }

    #[test]
    fn the_picker_offers_the_lanes_the_owner_named_and_not_a_hard_coded_list() {
        let frame = picking().frame(120, 36);
        let body = content(&frame);
        // The board's two non-baseline lanes, both choosable.
        assert!(body.contains("groupwise:angle_zz"), "{frame}");
        assert!(body.contains("kernel:linear"), "{frame}");
        // The baseline is listed and is not a choice: the owner runs it beside
        // every lane, so offering it would offer a run already happening.
        assert!(
            line_with(&frame, "always run").contains("ridge:none"),
            "{frame}"
        );
        // And nothing from the fallback list the owner did not name. If this
        // ever passes for `kernel:zz`, the picker has stopped reading the
        // payload and gone back to asserting a catalog it does not own.
        assert!(!body.contains("kernel:zz"), "{frame}");
    }

    #[test]
    fn r_still_refreshes_the_desk_while_it_offers_the_picker() {
        // The shell keeps `r`. The board on this pane only moves because this
        // key asks for it (`main::ingest`), so a picker that took the key
        // would have made the board unrefreshable to get itself opened.
        let mut client = armed();
        assert_eq!(
            press(&mut client, KeyCode::Char('r')),
            Some(Command::Refresh)
        );
        assert!(content(&client.frame(120, 36)).contains("RUN A PREDICTOR LANE"));
    }

    #[test]
    fn enter_sends_the_lane_under_the_cursor_and_nothing_else() {
        let mut client = picking();
        // The top-ranked lane is the default, so a cursor that never moved
        // sends the board's own first choice.
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::RunPredictor {
                model: "groupwise:angle_zz".to_string(),
                // The fixture desk is synthetic. The route defaults this to
                // the desk mode; sending it is what stops a window pointed at
                // one lane being handed a board about the other.
                offline: true,
            })
        );

        // And the cursor is what chooses: one row down is the other lane, and
        // never the baseline listed under them.
        let mut client = picking();
        press(&mut client, KeyCode::Down);
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::RunPredictor {
                model: "kernel:linear".to_string(),
                offline: true,
            })
        );
        // The list has two choices, so the wall holds rather than reaching the
        // baseline drawn beneath them.
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Down);
    }

    #[test]
    fn a_second_run_while_the_owner_is_fitting_is_refused_out_loud() {
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        let frame = client.frame(120, 36);
        assert!(
            content(&frame).contains("running groupwise:angle_zz…"),
            "{frame}"
        );
        assert!(
            content(&frame).contains("this can take a minute"),
            "{frame}"
        );

        // `r` again. The box is up and owns the keyboard (`View::typing`), so
        // this one does not reach the shell's refresh — and it must not reach
        // the owner either.
        assert_eq!(press(&mut client, KeyCode::Char('r')), None);
        assert!(
            content(&client.frame(120, 36)).contains("one run at a time"),
            "{}",
            client.frame(120, 36)
        );
        // Enter, at the box that is still up, is refused the same way and
        // sends nothing.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(content(&client.frame(120, 36)).contains("one run at a time"));
    }

    #[test]
    fn a_declined_lane_keeps_the_choice_and_renders_the_owners_sentence() {
        let mut client = picking();
        press(&mut client, KeyCode::Down);
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::PredictorRefused {
            said: "unknown model 'kernel:linear'; available: ('ridge:none', 'kernel:zz')"
                .to_string(),
        });
        let frame = client.frame(120, 36);
        // The owner's words, verbatim — they name the lanes it does serve,
        // which is the whole remedy.
        assert!(
            content(&frame).contains("available: ('ridge:none'"),
            "{frame}"
        );
        // The box is still up on the lane that was refused: the sentence is
        // about *that* lane, and a picker that closed would take it away.
        assert!(content(&frame).contains("RUN A PREDICTOR LANE"), "{frame}");
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::RunPredictor {
                model: "kernel:linear".to_string(),
                offline: true,
            })
        );
    }

    #[test]
    fn a_board_that_came_back_closes_the_box_and_says_what_won() {
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::PredictorRan {
            run_id: Some("9f3c1d77aa20".to_string()),
            models: vec!["groupwise:angle_zz".to_string(), "ridge:none".to_string()],
            champion: Some("groupwise:angle_zz".to_string()),
        });
        let frame = client.frame(120, 36);
        let said = line_with(&frame, "champion groupwise:angle_zz");
        // The run, cut to the eight characters an operator quotes.
        assert!(said.contains("run 9f3c1d77"), "{said}");
        assert!(!said.contains("9f3c1d77aa20"), "{said}");
        // The box has done its work and the refetch behind this outcome is
        // what the pane then draws.
        assert!(!content(&frame).contains("RUN A PREDICTOR LANE"), "{frame}");
        assert!(!content(&frame).contains("running "), "{frame}");
    }

    #[test]
    fn a_board_that_admitted_nothing_is_a_result_and_not_an_absence() {
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::PredictorRan {
            run_id: Some("9f3c1d77aa20".to_string()),
            models: vec!["kernel:zz".to_string(), "ridge:none".to_string()],
            champion: None,
        });
        let frame = client.frame(120, 36);
        // Never a blank, and never the lane that was asked for: a `null`
        // champion is the board saying nothing cleared the bar.
        assert!(
            content(&frame).contains("2 fitted, nothing cleared admission"),
            "{frame}"
        );
    }

    #[test]
    fn a_request_that_never_landed_does_not_leave_the_pane_in_flight() {
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::Failed {
            what: "run groupwise:angle_zz".to_string(),
            said: "the owner did not answer: connection refused".to_string(),
        });
        let frame = client.frame(120, 36);
        assert!(content(&frame).contains("connection refused"), "{frame}");
        assert!(!content(&frame).contains("running groupwise"), "{frame}");
        // And the key works again. A pane that refused `r` forever after one
        // timeout is a client that looks broken.
        press(&mut client, KeyCode::Char('r'));
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::RunPredictor {
                model: "groupwise:angle_zz".to_string(),
                offline: true,
            })
        );
    }

    #[test]
    fn an_unrelated_write_does_not_retire_a_run_that_is_still_going() {
        // A board is fitted for a minute, so another pane's answer will land
        // in the middle of one. SETTINGS clears its waits on any outcome and
        // can; this pane must not, or it would offer to start a second run
        // while the first is still on the owner's CPU.
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::MethodSet {
            policy: "hrp".to_string(),
            cap: None,
            warning: None,
        });
        assert!(content(&client.frame(120, 36)).contains("running groupwise:angle_zz…"));
    }

    #[test]
    fn the_run_word_opens_the_picker_from_a_click_and_sends_nothing() {
        let mut client = armed();
        let frame = client.frame(120, 36);
        // The word, where the frame actually drew it — found rather than
        // assumed, so this pins the published rect and not a guess about the
        // header's width.
        let row = frame
            .lines()
            .position(|line| line.contains("PREDICTOR BOARD run"))
            .expect("no title row") as u16;
        // Counted in *cells*, not bytes: the nav rail and the panel bar are
        // multi-byte glyphs, so a byte offset is several columns adrift. The
        // backend quotes each row, which is the one cell subtracted at the end.
        let line = frame.lines().nth(row as usize).unwrap();
        let at = line.find("PREDICTOR BOARD run").expect("no run word");
        let column = (line[..at].chars().count() + "PREDICTOR BOARD ".len()) as u16 - 1;
        client.mouse(MouseEventKind::Down(MouseButton::Left), column, row);
        assert!(
            content(&client.frame(120, 36)).contains("RUN A PREDICTOR LANE"),
            "{}",
            client.frame(120, 36)
        );
    }

    #[test]
    fn esc_closes_the_box_and_leaves_the_board_as_the_owner_served_it() {
        let mut client = picking();
        assert_eq!(press(&mut client, KeyCode::Esc), None);
        let frame = client.frame(120, 36);
        assert!(!content(&frame).contains("RUN A PREDICTOR LANE"), "{frame}");
        assert!(content(&frame).contains("★CHAMP"), "{frame}");
    }

    #[test]
    fn a_desk_with_no_board_still_offers_the_lanes_the_owner_serves() {
        // Nothing to read the ids off, which is the one case the fallback
        // exists for — and the one an operator is most likely to be in, since
        // a desk that never ran a board is exactly where a first run starts.
        let mut client = predictors();
        client.store.posture = Posture::Operator;
        client.frame(120, 36);
        press(&mut client, KeyCode::Char('r'));
        let frame = client.frame(120, 36);
        assert!(content(&frame).contains("kernel:zz"), "{frame}");
        assert!(
            line_with(&frame, "always run").contains("ridge:none"),
            "{frame}"
        );
    }

    #[test]
    fn the_lane_picker_renders_at_120x36() {
        insta::assert_snapshot!(picking().frame(120, 36));
    }

    #[test]
    fn a_run_in_flight_renders_at_120x36() {
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn a_finished_run_renders_at_120x36() {
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::PredictorRan {
            run_id: Some("9f3c1d77aa20".to_string()),
            models: vec!["groupwise:angle_zz".to_string(), "ridge:none".to_string()],
            champion: Some("groupwise:angle_zz".to_string()),
        });
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn a_refused_lane_renders_at_120x36() {
        let mut client = picking();
        press(&mut client, KeyCode::Enter);
        client.views.wrote(&Wrote::PredictorRefused {
            said: "unknown model 'groupwise:angle_zz'; available: ('ridge:none', 'kernel:zz')"
                .to_string(),
        });
        insta::assert_snapshot!(client.frame(120, 36));
    }
}
