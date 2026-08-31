//! RESEARCH: the ablation's ranking, the run ledger, and the staged catalog.
//!
//! Three panes that carry the whole of what the Textual reference view showed,
//! because the cutover replaces that surface: anything missing here is a
//! surface that silently disappears from the desk rather than one that moved.
//!
//! Two properties run through the pins. A metric the ablation could not produce
//! must never render as a number — a `0.00` Sharpe is a measurement nobody
//! made, and it sorts and reads like a real one. And the catalog's `stage` is
//! not decoration: it is the boundary `algorithms.solve` enforces in code, so
//! it has to be on screen beside every id rather than implied by absence.

mod harness;

use atlas::bus::{AppEvent, Channel};
use atlas::model::{QualitativeMatrix, Snapshot};
use atlas::store::Store;
use atlas::theme::Theme;
use crossterm::event::KeyCode;
use harness::{body_style_of, content, line_with, Client};
use std::time::Instant;

/// The fixture desk, already switched to RESEARCH.
fn research() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('5'));
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

fn research_from(json: &str) -> Client {
    let mut client = Client::new(store_from(json));
    client.press(KeyCode::Char('5'));
    client
}

#[test]
fn the_research_view_renders_its_panes_at_120x36() {
    // No window fetched yet, which is the state every RESEARCH opens in: the
    // matrix rides a beat, and the frame before its first tick has to say that
    // rather than draw an empty table.
    insta::assert_snapshot!(research().frame(120, 36));
}

#[test]
fn the_leaderboard_keeps_the_owners_order_and_marks_the_champion() {
    // The owner ranks by Sharpe and sorts unscored arms last; a client that
    // sorted again would have to invent a rule for the arms with no number.
    // The champion is the arm `mandate.operational_policy` names — the one the
    // paper book is actually run under — and it is the only row that gets it.
    let frame = research().frame(120, 36);
    let body = content(&frame);
    let rows: Vec<&str> = body
        .lines()
        .filter(|l| l.contains("HRP") || l.contains("Equal weight") || l.contains("MVSK"))
        .collect();
    assert_eq!(rows.len(), 3, "{body}");
    assert!(rows[0].contains("HRP"), "{body}");
    assert!(rows[0].contains('★'), "the champion is unmarked: {body}");
    assert!(
        !rows[2].contains('★'),
        "a second row wore the champion mark: {body}"
    );
    // A benchmark is not a champion but is not an ordinary arm either: it is
    // what every hypothesis has to beat.
    assert!(
        line_with(&frame, "Equal weight").contains("BENCH"),
        "{frame}"
    );
}

#[test]
fn the_five_columns_are_the_owners_overlay_metrics_and_nothing_else() {
    // `OVERLAY_METRICS` is one definition on the owner and both its leaderboard
    // and its reference overlay report exactly this subset. A client that added
    // a sixth would be reporting a metric the desk does not judge arms on.
    let frame = research().frame(120, 36);
    let header = line_with(&frame, "SHARPE");
    for column in ["SHARPE", "RET", "MAXDD", "CVAR95", "DSR"] {
        assert!(header.contains(column), "{header}");
    }
    let champion = line_with(&frame, "HRP");
    assert!(champion.contains("0.84"), "{champion}");
    assert!(champion.contains("7.1%"), "{champion}");
    assert!(champion.contains("11.3%"), "{champion}");
    assert!(champion.contains("1.89%"), "{champion}");
    assert!(champion.contains("0.61"), "{champion}");
}

#[test]
fn an_arm_the_ablation_could_not_score_keeps_its_row_with_nothing_in_it() {
    // The owner sorts these last rather than dropping them, so the row is the
    // evidence that an arm ran and produced no comparable number. Rendering
    // `0.00` there would put a measurement nobody made into the ranking.
    let frame = research().frame(120, 36);
    let unscored = line_with(&frame, "Scenario CVaR");
    assert!(unscored.contains("--"), "{unscored}");
    assert!(
        !unscored.contains("0.00"),
        "an unscored arm rendered a number: {unscored}"
    );
}

#[test]
fn a_metric_too_small_to_print_is_toned_by_what_is_printed() {
    // Dust magnitudes are real in a leaderboard: an arm at -1e-13 Sharpe prints
    // `0.00` and taking the negative tone from the raw double would paint a red
    // cell over a zero. The colour has to be decided at the precision the cell
    // is drawn at, exactly as the sector map is.
    let t = Theme::truecolor();
    let client = research_from(
        r#"{"leaderboard": [{"arm_id": "D0", "name": "Dust", "sharpe": -0.0000000001,
             "ann_return": -0.00000001, "max_drawdown": null, "cvar_95": null,
             "deflated_sharpe": null}]}"#,
    );
    let buffer = client.buffer(120, 36);
    assert_ne!(
        body_style_of(&buffer, "0.00").fg,
        Some(t.negative),
        "a cell printed as zero was painted as a loss"
    );
}

#[test]
fn a_desk_with_no_ablation_says_how_to_run_one() {
    // "No ablation yet" and "this pane is broken" must not look the same, and
    // the remedy is a command an operator can actually run.
    let client = research_from(r#"{"runs": []}"#);
    let body = content(&client.frame(120, 36));
    assert!(body.contains("no ablation"), "{body}");
    assert!(body.contains("qlab batch"), "{body}");
}

#[test]
fn the_catalog_names_the_stage_beside_every_id() {
    // The stage is the boundary `algorithms.solve` enforces in code: research
    // and offline entries are visible and not agent-runnable. A catalog that
    // listed ids alone would show nineteen methods and imply the desk can run
    // all of them.
    let frame = research().frame(120, 36);
    assert!(
        line_with(&frame, "equal_weight").contains("OPERATIONAL"),
        "{frame}"
    );
    assert!(
        line_with(&frame, "mvsk_multistart").contains("RESEARCH"),
        "{frame}"
    );
    assert!(
        line_with(&frame, "qaoa_selection").contains("OFFLINE"),
        "{frame}"
    );
}

#[test]
fn the_three_stages_are_told_apart_by_colour_as_well_as_by_word() {
    // The word is the answer; the colour is what makes a screenful of ids
    // scannable. Two stages rendering identically would make the boundary
    // invisible at a glance, which is exactly when it matters.
    let client = research();
    let buffer = client.buffer(120, 36);
    let operational = body_style_of(&buffer, "OPERATIONAL").fg;
    let research = body_style_of(&buffer, "RESEARCH").fg;
    let offline = body_style_of(&buffer, "OFFLINE").fg;
    assert_ne!(operational, research);
    assert_ne!(research, offline);
    assert_ne!(operational, offline);
    // The one the desk can actually run is the one that reads as available.
    assert_eq!(operational, Some(Theme::truecolor().positive));
}

#[test]
fn the_catalog_counts_what_the_desk_may_run_against_what_it_may_not() {
    // Six of nineteen is the fact an operator needs before reading any of the
    // ids: most of this catalog is evidence, not runtime.
    let frame = research().frame(120, 36);
    let head = line_with(&frame, "CATALOG");
    assert!(
        head.contains('3'),
        "3 of the 6 fixture rows are operational: {head}"
    );
}

#[test]
fn the_runs_list_is_newest_first_with_the_kind_that_wrote_it() {
    // Rows are found by their ids, not by their kinds: "ablation" is also the
    // word the leaderboard's own header uses, and a position read off that
    // would pass without the runs pane rendering at all.
    let frame = research().frame(120, 36);
    let body = content(&frame);
    let newest = body.lines().position(|l| l.contains("805e0729cf")).unwrap();
    let oldest = body.lines().position(|l| l.contains("3f2a91cc70")).unwrap();
    assert!(newest < oldest, "the newest run is not at the top:\n{body}");
    assert!(
        line_with(&frame, "805e0729cf").contains("ablation"),
        "{frame}"
    );
    assert!(
        line_with(&frame, "3f2a91cc70").contains("backtest"),
        "{frame}"
    );
}

#[test]
fn a_ledger_longer_than_the_pane_counts_what_it_could_not_draw() {
    // The owner serves thirty runs and nothing here scrolls. A pane that drew
    // the first few and stopped would read as a desk with a short history.
    let rows: Vec<String> = (0..30)
        .map(|i| {
            format!(
                r#"{{"run_id": "r{i:02}00000000", "kind": "ablation",
                     "created_at": "2026-07-30T1{}:00:00+00:00"}}"#,
                i % 10
            )
        })
        .collect();
    let client = research_from(&format!(r#"{{"runs": [{}]}}"#, rows.join(",")));
    let body = content(&client.frame(120, 36));
    assert!(body.contains("more"), "{body}");
}

#[test]
fn a_pane_too_narrow_for_the_metrics_says_what_it_would_take() {
    // Ratatui right-aligns an overlong line by dropping its leading cells, and
    // the leading cell of a return is its sign — a loss drawn as a gain. The
    // pane refuses instead.
    let client = research();
    let narrow = content(&client.frame(70, 36));
    assert!(narrow.contains("RESEARCH needs"), "{narrow}");
}

// -- the vol-forecast readout ------------------------------------------------
//
// Parity with `qlab/tui/app.py::_render_research`. Left behind at the cutover
// this would be a research-admission signal that silently disappeared from the
// desk — the one number that says whether the forecast may be used at all.

#[test]
fn the_vol_forecast_states_its_ic_and_whether_the_desk_may_use_it() {
    let frame = research().frame(120, 36);
    let row = line_with(&frame, "vol forecast");
    // Three decimals, as the Textual readout reports it: the admission
    // threshold is 0.03, so two would round the gate away.
    assert!(row.contains("-0.121"), "{row}");
    assert!(row.contains("unstable"), "{row}");
    assert!(row.contains("not usable"), "{row}");
}

#[test]
fn a_forecast_that_clears_its_own_stated_gate_reads_as_usable() {
    let t = Theme::truecolor();
    let client = research_from(
        r#"{"runs": [{"run_id": "r1", "kind": "prediction",
             "spec": {"mean_ic": 0.081, "ic_stability": 0.63, "usable": true,
                      "admission": {"mean_ic_strictly_above": 0.03,
                                    "ic_stability_strictly_above": 0.5}}}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "vol forecast");
    assert!(row.contains("stable") && !row.contains("unstable"), "{row}");
    assert!(
        row.contains("usable") && !row.contains("not usable"),
        "{row}"
    );
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "usable").fg,
        Some(t.positive)
    );
}

#[test]
fn the_gate_is_the_runs_own_thresholds_and_not_a_copy_of_them() {
    // The owner writes its admission rule into every spec, so this client reads
    // it rather than carrying `0.03` and `0.5` of its own — a hard-coded gate
    // keeps asserting an old threshold after the owner moves it, and this is a
    // research-admission signal, which is exactly the number that must not
    // drift quietly.
    //
    // The same evidence under a stricter stated rule is not usable.
    let strict = research_from(
        r#"{"runs": [{"run_id": "r1", "kind": "prediction",
             "spec": {"mean_ic": 0.081, "ic_stability": 0.63, "usable": true,
                      "admission": {"mean_ic_strictly_above": 0.09,
                                    "ic_stability_strictly_above": 0.5}}}]}"#,
    );
    let frame = strict.frame(120, 36);
    assert!(
        line_with(&frame, "vol forecast").contains("not usable"),
        "the client kept a threshold of its own"
    );

    // And a run whose own flag says no is not usable however good the numbers
    // look: the owner made that call, and this pane reports it.
    let refused = research_from(
        r#"{"runs": [{"run_id": "r1", "kind": "prediction",
             "spec": {"mean_ic": 0.4, "ic_stability": 0.9, "usable": false,
                      "admission": {"mean_ic_strictly_above": 0.03,
                                    "ic_stability_strictly_above": 0.5}}}]}"#,
    );
    let frame = refused.frame(120, 36);
    assert!(line_with(&frame, "vol forecast").contains("not usable"));
}

#[test]
fn a_forecast_with_no_evidence_is_not_admitted_on_a_flag_alone() {
    // `usable: true` with no IC behind it is a verdict nothing supports. Absent
    // evidence is not a pass, and the number renders `--` rather than zero.
    let client = research_from(
        r#"{"runs": [{"run_id": "r1", "kind": "prediction", "spec": {"usable": true}}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "vol forecast");
    assert!(row.contains("--"), "{row}");
    assert!(row.contains("not usable"), "{row}");
    assert!(
        !row.contains("0.000"),
        "an absent IC became a number: {row}"
    );
}

#[test]
fn a_dust_sized_ic_is_signed_by_what_is_printed() {
    // At three decimals a -1e-9 IC prints `0.000`; `{:.3}` would write `-0.000`,
    // a minus sign on a zero in the one column that says which way the forecast
    // leans. The same rule the leaderboard's ratios take.
    let client = research_from(
        r#"{"runs": [{"run_id": "r1", "kind": "prediction",
             "spec": {"mean_ic": -0.000000001, "ic_stability": 0.6, "usable": false}}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "vol forecast");
    assert!(row.contains(" 0.000"), "{row}");
    assert!(!row.contains("-0.000"), "a sign was drawn on a zero: {row}");
}

#[test]
fn a_desk_that_has_never_forecast_says_so_rather_than_showing_nothing() {
    let client = research_from(r#"{"runs": [{"run_id": "r1", "kind": "ablation"}]}"#);
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "vol forecast");
    assert!(row.contains("no prediction run yet"), "{row}");
}

#[test]
fn a_spec_that_is_not_an_object_costs_one_readout_and_not_the_snapshot() {
    // `runs.spec` is JSON the registry stored verbatim, so a row from an older
    // producer can hold anything. The Textual client guards the same column the
    // same way; a client that refused the payload over it would blank the whole
    // desk to lose one line.
    let client = research_from(
        r#"{"runs": [{"run_id": "r1", "kind": "prediction", "spec": "legacy string"},
                     {"run_id": "r2", "kind": "ablation", "created_at": "2026-07-30T17:58:41+00:00"}]}"#,
    );
    let frame = client.frame(120, 36);
    // And it is said as its own fact. "The desk has never forecast" and "there
    // is a run whose record this client cannot read" have different remedies,
    // so they may not share a sentence — the Textual client folds the second
    // into an empty dict and reports `IC 0.000`, a number nobody computed.
    let row = line_with(&frame, "vol forecast");
    assert!(row.contains("no readable spec"), "{row}");
    assert!(row.contains("r1"), "the unreadable run is named: {row}");
    assert!(!row.contains("0.000"), "{row}");
    // The rest of the pane is unharmed.
    assert!(content(&frame).contains("r2"), "{frame}");
}

#[test]
fn the_newest_prediction_is_the_one_reported() {
    // `runs` is newest-first as the owner serves it, and a stale forecast shown
    // beside a newer one is a gate an operator would read as current.
    let client = research_from(
        r#"{"runs": [
             {"run_id": "new", "kind": "prediction", "spec": {"mean_ic": 0.222, "usable": false}},
             {"run_id": "old", "kind": "prediction", "spec": {"mean_ic": 0.111, "usable": true}}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "vol forecast");
    assert!(row.contains("0.222"), "{row}");
    assert!(!row.contains("0.111"), "{row}");
}

#[test]
fn research_claims_no_key_at_all() {
    // Every pane here is read-only and nothing selects, so a key pressed on it
    // has to fall through to whatever claims it next.
    let mut client = research();
    let frame = client.frame(120, 36);
    for code in [
        KeyCode::Down,
        KeyCode::Up,
        KeyCode::Enter,
        KeyCode::Char('s'),
    ] {
        client.press(code);
        assert_eq!(client.frame(120, 36), frame, "{code:?} changed RESEARCH");
    }
}

// -- the predictor board readout --------------------------------------------
//
// Parity with the Textual research summary's board lines. The champion is the
// owner's call — the first *admitted* model in its own ranking — and this
// client repeats it rather than re-deriving it.

#[test]
fn the_predictor_board_names_its_champion_and_the_edge_over_baseline() {
    let t = Theme::truecolor();
    let client = research_from(
        r#"{"runs": [{"run_id": "b1", "kind": "predictor_board",
             "spec": {"board": {"baseline": "ridge:none", "champion": "kernel:zz",
                      "admitted_any": true,
                      "models": [{"model_id": "kernel:zz", "mean_ic": 0.091,
                                  "usable": true,
                                  "delta_mean_ic_vs_baseline": 0.031}]}}}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "predictors");
    assert!(row.contains("champion kernel:zz"), "{row}");
    // Signed, because an edge over the baseline has a side.
    assert!(row.contains("+0.031"), "{row}");
    assert!(row.contains("vs ridge:none"), "{row}");
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "kernel:zz").fg,
        Some(t.positive)
    );
}

#[test]
fn a_board_with_no_admitted_model_says_so_rather_than_promoting_one() {
    let t = Theme::truecolor();
    let client = research_from(
        r#"{"runs": [{"run_id": "b1", "kind": "predictor_board",
             "spec": {"board": {"baseline": "ridge:none", "champion": null,
                      "admitted_any": false, "models": []}}}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "predictors");
    assert!(row.contains("no admitted model"), "{row}");
    assert!(row.contains("vs ridge:none"), "{row}");
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "no admitted model").fg,
        Some(t.negative)
    );
}

#[test]
fn a_board_run_with_an_unreadable_spec_is_loud_not_absent() {
    // "Never ran" and "ran, and this client cannot read the answer" have
    // different remedies and must not share a sentence.
    let client = research_from(
        r#"{"runs": [{"run_id": "b1", "kind": "predictor_board",
             "spec": {"board": 17}}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "predictors");
    assert!(row.contains("no readable board"), "{row}");
}

#[test]
fn a_desk_that_never_ran_the_board_says_so() {
    let client = research_from(r#"{"runs": []}"#);
    let frame = client.frame(120, 36);
    let row = line_with(&frame, "predictors");
    assert!(row.contains("no predictor board run yet"), "{row}");
}

// -- the qualitative matrix --------------------------------------------------
//
// What the grounded news window says about each name, as counts. The pane is
// read-only in both postures and claims no key: it is evidence the owner logged
// per window, and nothing on it can be acted on from here.
//
// Two properties run through these pins. Held names lead, because the question
// this table is read for is what the record says about what the desk is already
// carrying — a held name with one publisher behind it is a position with
// nothing under it. And the three ways it can have nothing to draw are three
// different sentences: not fetched, an empty window, and a window whose feed
// broke are three different remedies, and a blank pane is none of them.

/// The captured window, folded in the way the runtime folds it.
fn with_matrix(client: &mut Client, json: &str) {
    let matrix = serde_json::from_str::<QualitativeMatrix>(json).unwrap();
    let now = client.store.last_snapshot_at.unwrap_or_else(Instant::now);
    client
        .store
        .apply(AppEvent::Qualitative(Box::new(matrix)), now);
}

/// One matrix row, read out of the rows the *view* owns.
///
/// Not `line_with`: the ticker tape repeats every symbol in the universe and
/// the pulse rail draws movers of its own, so `frame.contains("SPY")` matches
/// two lines this pane did not draw. Every assertion below reads through here.
fn matrix_row_of<'a>(body: &'a str, name: &str) -> &'a str {
    body.lines()
        .find(|line| starts_the_row(line, name))
        .unwrap_or_else(|| panic!("no matrix row for {name}:\n{body}"))
}

/// Whether a rendered line is `name`'s row — the rule `matrix_row_of` and the
/// ordering pin share, so the two cannot disagree about which line is whose.
///
/// The leading cell of a content row is the pane rule the shell draws between
/// the rails; a name is the first thing the *view* put on the line.
fn starts_the_row(line: &str, name: &str) -> bool {
    line.trim_start()
        .trim_start_matches('\u{2502}')
        .trim_start()
        .starts_with(name)
}

/// The fixture desk on RESEARCH, holding the window the poller's beat brings.
fn with_window() -> Client {
    let mut client = research();
    with_matrix(
        &mut client,
        include_str!("fixtures/qualitative_matrix.json"),
    );
    client
}

#[test]
fn the_qualitative_matrix_renders_the_window_at_120x36() {
    insta::assert_snapshot!(with_window().frame(120, 36));
}

#[test]
fn the_matrix_names_the_window_it_is_a_reading_of() {
    // Two facts, because they answer different questions: `as_of` says when the
    // record was read, and the hash says *which* record — the owner hashes the
    // claims and not the day, so two readings of one window are recognisable as
    // one window rather than as a record that changed overnight.
    let frame = with_window().frame(120, 36);
    let head = line_with(&frame, "QUALITATIVE MATRIX");
    assert!(head.contains("2026-08-31"), "{head}");
    assert!(head.contains("9f2c41ab"), "{head}");
}

#[test]
fn the_matrix_columns_are_the_owners_own_counts_and_nothing_signed() {
    // `qlab/news/matrix.py` refuses to serve a signed column because a signed
    // qualitative column is a return forecast wearing a qualitative name. A
    // client that added one on the way out would put the sign back.
    let frame = with_window().frame(120, 36);
    let header = line_with(&frame, "CORROB");
    for column in ["NAME", "COVER", "PUBS", "CORROB", "PRIMARY", "RELEASE"] {
        assert!(header.contains(column), "{header}");
    }
    let body = content(&frame);
    let spy = matrix_row_of(&body, "SPY");
    for count in ["14", "9", "11", "4", "21"] {
        assert!(spy.contains(count), "{spy}");
    }
    assert!(!spy.contains('+') && !spy.contains('%'), "{spy}");
}

#[test]
fn the_books_own_names_lead_and_wear_the_mark() {
    // Held first, and the mark is the book's answer rather than a name-shaped
    // guess: the owner serves the whole universe here, so nothing in the row
    // itself says whether the desk is carrying it.
    let mut client = research_from(
        r#"{"live_portfolio": {"positions": [
             {"ticker": "XLF", "qty": 12.0},
             {"ticker": "SPY", "qty": 3.0},
             {"ticker": "GLD", "qty": 0.0}]}}"#,
    );
    with_matrix(
        &mut client,
        include_str!("fixtures/qualitative_matrix.json"),
    );
    let body = content(&client.frame(120, 36));
    let at = |name: &str| {
        body.lines()
            .position(|line| starts_the_row(line, name))
            .unwrap_or_else(|| panic!("no matrix row for {name}:\n{body}"))
    };
    let row_of = |name: &str| matrix_row_of(&body, name);
    assert!(
        at("SPY") < at("ACWI"),
        "an unheld name led the table:\n{body}"
    );
    assert!(at("XLF") < at("ACWI"), "{body}");
    // Alphabetical inside each group — the owner's own key order, which this
    // client re-sorts no further than it had to.
    assert!(at("SPY") < at("XLF"), "{body}");
    assert!(at("ACWI") < at("BNDW"), "{body}");
    assert!(row_of("SPY").contains("HELD"), "{body}");
    assert!(!row_of("ACWI").contains("HELD"), "{body}");
    // A closed row is not a held one: marking flat names would put most of the
    // universe in the held group and retire the ordering.
    assert!(!row_of("GLD").contains("HELD"), "{body}");
}

#[test]
fn a_name_with_no_scheduled_release_is_absent_rather_than_due_today() {
    // The one nullable count, and it is nullable in the owner too: no release
    // ahead is not zero days to one. A `0` in that column would put a name the
    // calendar says nothing about at the top of tomorrow's reading.
    let body = content(&with_window().frame(120, 36));
    let bndw = matrix_row_of(&body, "BNDW");
    assert!(bndw.contains("--"), "{bndw}");
    // And a real zero elsewhere in the same row still renders as a zero: BNDW
    // has no coverage, which is a count the window actually made.
    assert!(bndw.contains(" 0"), "{bndw}");
}

#[test]
fn a_matrix_nobody_has_fetched_is_not_an_empty_window() {
    // Three states, three sentences. "This client has not been told" and "the
    // desk has nothing to tell" have different remedies, and a blank pane is
    // neither of them.
    let unfetched = content(&research().frame(120, 36));
    assert!(unfetched.contains("has not been fetched"), "{unfetched}");
    assert!(!unfetched.contains("record is empty"), "{unfetched}");

    let mut client = research();
    with_matrix(
        &mut client,
        r#"{"as_of": "2026-08-31", "window_hash": "9f2c41ab77de0135", "rows": {}}"#,
    );
    let empty = content(&client.frame(120, 36));
    assert!(empty.contains("the record is empty"), "{empty}");
    assert!(!empty.contains("has not been fetched"), "{empty}");
    // Still under its own header: an empty window is a window, and the pane
    // says which one it read.
    assert!(empty.contains("9f2c41ab"), "{empty}");
}

#[test]
fn the_windows_failures_are_named_on_the_pane_one_line_each() {
    // Zero coverage on a broken feed is not a quiet tape, and a calendar nobody
    // extended leaves every count intact. Two claims, two lines: folded into
    // one sentence the loud one would read as the quiet one.
    let t = Theme::truecolor();
    let mut client = research();
    with_matrix(
        &mut client,
        r#"{"as_of": "2026-08-31", "window_hash": "9f2c41ab77de0135",
            "news_error": "alpaca refused: 401",
            "calendar_error": "the release calendar ends 2026-08-01",
            "rows": {"SPY": {"ticker": "SPY", "coverage": 0, "publishers": 0,
                             "corroborated": 0, "primary_docs": 0,
                             "days_to_next_release": null}}}"#,
    );
    let frame = client.frame(120, 36);
    let body = content(&frame);
    assert!(body.contains("alpaca refused: 401"), "{body}");
    assert!(body.contains("the release calendar ends"), "{body}");
    // Dim, not alarming: the pane still drew every count it has, and the rows
    // under a broken feed are the evidence that there is nothing behind them.
    assert_eq!(
        body_style_of(&client.buffer(120, 36), "alpaca refused").fg,
        Some(t.text_dim)
    );
    // The counts survive the failures rather than the pane refusing wholesale.
    assert!(matrix_row_of(&body, "SPY").contains('0'), "{body}");
}

#[test]
fn a_universe_longer_than_the_pane_counts_what_it_could_not_draw() {
    // Nothing here scrolls. A matrix that drew the names that fit and stopped
    // would read as a narrower universe than the desk covers.
    let rows: Vec<String> = (0..40)
        .map(|i| {
            format!(
                r#""T{i:02}": {{"ticker": "T{i:02}", "coverage": 1, "publishers": 1,
                     "corroborated": 0, "primary_docs": 0, "days_to_next_release": 3}}"#
            )
        })
        .collect();
    let mut client = research();
    with_matrix(
        &mut client,
        &format!(
            r#"{{"as_of": "2026-08-31", "window_hash": "9f2c41ab77de0135",
                 "rows": {{{}}}}}"#,
            rows.join(",")
        ),
    );
    let body = content(&client.frame(120, 36));
    assert!(body.contains("more"), "{body}");
    // And the ledger below it keeps its floor rather than being squeezed out
    // by a universe that grew.
    assert!(body.contains("RUNS"), "{body}");
    assert!(body.contains("CATALOG"), "{body}");
}

#[test]
fn a_flat_live_book_holds_nothing_whatever_the_registry_row_says() {
    // BOOK reads `live_portfolio.positions` and nothing else, so a matrix that
    // fell back to the reconciled book would mark a name HELD off a stale
    // registry row while the pane next door drew a flat desk — two panes of one
    // workstation disagreeing about what the desk owns. A flat live book is an
    // answer: nothing is held.
    let mut client = research_from(
        r#"{"live_portfolio": {"positions": []},
            "portfolio": {"positions": {"SPY": {"qty": 10.0}, "XLF": {"qty": 4.0}}}}"#,
    );
    with_matrix(
        &mut client,
        include_str!("fixtures/qualitative_matrix.json"),
    );
    let body = content(&client.frame(120, 36));
    assert!(
        !body.contains("HELD"),
        "a flat live book marked a holding:\n{body}"
    );
    // And the rows are all there — a desk that holds nothing still reads the
    // record for its whole universe.
    assert!(matrix_row_of(&body, "SPY").contains("14"), "{body}");
}

#[test]
fn a_terminal_too_short_for_the_matrix_refuses_rather_than_dropping_it() {
    // The pane reserves its rows out of the view's height, which raised the
    // floor when it landed. One row under it the view refuses as a whole; at
    // it, all four panes draw. A height that quietly dropped the matrix would
    // be the record disappearing from the desk without saying so.
    let client = with_window();
    let short = content(&client.frame(120, 19));
    assert!(short.contains("RESEARCH needs"), "{short}");
    let exact = content(&client.frame(120, 20));
    assert!(exact.contains("QUALITATIVE MATRIX"), "{exact}");
    assert!(exact.contains("LEADERBOARD"), "{exact}");
    assert!(exact.contains("RUNS"), "{exact}");
    assert!(exact.contains("CATALOG"), "{exact}");
}
