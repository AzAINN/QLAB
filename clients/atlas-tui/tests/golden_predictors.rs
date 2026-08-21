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
