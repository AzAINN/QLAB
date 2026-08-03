//! DESK — the read as it types itself in, and the tiles that say what the desk holds.
//!
//! Two properties run through these. **The read is revealed, not animated**:
//! the fraction on screen is a function of two instants the test hands in, so a
//! mid-reveal frame is pinned by arithmetic rather than sampled by a sleep.
//! **A tile says which kind of nothing it has**: a section the owner has not
//! sent while it is reachable is in flight and throbs; the same section with no
//! owner behind it is missing and reads `--`; a number the owner declined to
//! compute reads `--` beside the reason it gave.
//!
//! Content assertions go through `content`, the columns this view owns. The
//! pulse rail carries its own summary of the same read — state, agreement,
//! conviction, news — so a pin on the whole frame could pass on the rail and
//! say nothing about the pane under test.

mod harness;

use atlas::bus::{AppEvent, Channel};
use atlas::model::Snapshot;
use atlas::store::Store;
use harness::{content, line_with, Client};
use std::time::{Duration, Instant};

/// A desk with every section the six tiles read, and a read with something in
/// each of its parts.
///
/// Hand-written rather than the captured fixture: that payload's read carries
/// no tensions and the owner sends no `decisions` in it, and a view test that
/// could only be written against sections that happen to be populated would
/// never exercise the parts an operator most needs to see.
const DESK: &str = r#"{
  "live_portfolio": {"equity": 10000.0, "cash": 0.0, "positions": []},
  "portfolio": {
    "weights": {"ACWI": 0.0628, "BNDW": 0.44, "GLD": 0.0786},
    "target_weights": {"ACWI": 0.0628, "BNDW": 0.4, "GLD": 0.0786}
  },
  "market": {"regime": {"regime": "calm", "robust_state": "uncertain",
                        "signal": 0.0916, "threshold": 0.1111,
                        "confidence": 0.6666, "method": "realized_vol_threshold"}},
  "stress": {
    "drawdown_tier": "warning",
    "leverage_headroom": 0.25,
    "stressed_vol": 0.1011,
    "stress_vol_limit": 0.3,
    "replays": {
      "2008": {"available": false, "return": null,
               "reason": "snapshot span does not cover the window"},
      "2020": {"available": true, "return": -0.173},
      "2022": {"available": true, "return": -0.1391}
    },
    "cost_gate_refusals": [{"ts": "2026-07-30T15:41:02Z", "plan_id": "0f1e2d3c",
                            "reasons": ["est_cost 41bp exceeds the 25bp gate"]}]
  },
  "decisions": [
    {"decision_id": "6cec32df", "as_of": "2026-07-30", "kind": "rebalance_gate",
     "verdict": null},
    {"decision_id": "6a1a5615", "as_of": "2026-07-30", "kind": "rebalance_gate",
     "verdict": {"verdict": "PASS", "source": "deterministic",
                 "reasons": ["drawdown tier none at 0.00%"]}}
  ],
  "atlas": {"mode": "propose", "current_task_id": "t-9"},
  "atlas_heartbeat": {"autonomous": true,
                      "coordinator": {"driving": false, "reason": "no CLI"}},
  "atlas_read": {
    "as_of": "2026-07-30",
    "quantitative_state": "stress",
    "agreement": "divergent",
    "conviction": 0.42,
    "tensions": ["Absorption is high while the tape is quiet."],
    "would_change_my_mind": ["Absorption falling back under its threshold."],
    "observations": ["Panel reads stress with 3 of 5 detectors agreeing."],
    "news": {"tone": "risk_off",
             "headlines": [{"headline": "Rate-path comments move futures",
                            "source": "wire-a", "tone": "risk_off"}]},
    "news_source": "alpaca",
    "read_hash": "c60c46006d926733",
    "grounding": {"window_hash": "45fbb3f32be5bef9"}
  }
}"#;

/// The last words of the read's body — the closing why line, which reveals
/// last. A sentinel for "the whole read is on screen" that no tile can
/// accidentally satisfy.
const LAST_WORDS: &str = "in flight.";
/// The first words of the read's body, which reveal first.
const FIRST_WORDS: &str = "CONVICTION";
/// The grounding hash, which is anchored to the foot of the pane and is
/// therefore *not* part of the reveal.
const PROVENANCE: &str = "45fbb3f32be5bef9";

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

/// A client on DESK — the view the workstation opens on, so no key is pressed.
fn desk_from(json: &str) -> Client {
    Client::new(store_from(json))
}

/// The same desk with nothing behind the owner: every section absent and the
/// runtime unreachable.
fn dark_desk(json: &str) -> Client {
    let mut store = Store::default();
    let now = Instant::now();
    store.apply(
        AppEvent::Snapshot(Box::new(serde_json::from_str::<Snapshot>(json).unwrap())),
        now,
    );
    Client::new(store)
}

#[test]
fn the_desk_renders_the_read_and_its_six_tiles_at_120x36() {
    insta::assert_snapshot!(desk_from(DESK).frame(120, 36));
}

#[test]
fn the_desk_at_the_narrow_baseline_is_the_read_alone() {
    // The workstation's other baseline frame — a laptop half-screen. There is
    // no arrangement of six tiles and a paragraph in 46 columns, so the frame
    // pins which of the two survives and what the other one leaves behind.
    insta::assert_snapshot!(desk_from(DESK).frame(90, 24));
}

// -- the read --------------------------------------------------------------

#[test]
fn the_read_carries_every_part_of_atlass_judgment() {
    let content = content(&desk_from(DESK).frame(120, 36));
    assert!(content.contains("CONVICTION"), "{content}");
    assert!(
        line_with(&content, "CONVICTION").contains("42.0%"),
        "{content}"
    );
    assert!(content.contains("DIVERGENT"), "the agreement:\n{content}");
    assert!(
        content.contains("STRESS"),
        "the quantitative state:\n{content}"
    );
    // Fragments rather than whole sentences: the read is wrapped to its column,
    // so a claim spans lines and no `contains` on the frame can hold the whole
    // of one. What each section says in full is pinned in `desk.rs`, against the
    // lines themselves.
    assert!(
        content.contains("Absorption is high"),
        "a tension:\n{content}"
    );
    assert!(
        content.contains("falling back"),
        "what would change it:\n{content}"
    );
    assert!(content.contains("RISK OFF"), "the news tone:\n{content}");
    assert!(content.contains("Rate-path"), "a headline:\n{content}");
    assert!(
        content.contains(PROVENANCE),
        "the grounding hash:\n{content}"
    );
    assert!(
        line_with(&content, "THE READ").contains("2026-07-30"),
        "which read this is:\n{content}"
    );
}

#[test]
fn a_broken_news_feed_says_so_and_does_not_summarise_a_window_it_never_read() {
    // An absent window and a broken feed are opposite facts about the same
    // silence, and only one of them is worth fixing — so the failure is loud.
    // What must not follow it is `-- · --`, which reads as a window that *was*
    // read and had nothing in it: the tone/source row is a summary of a window,
    // and with no window there is nothing for it to summarise.
    // Agreement and state are filled in so the read's own `WORD · WORD` header
    // cannot be mistaken for the row this test is about — with both absent it
    // renders `-- · --` too, three lines above.
    let broken = content(
        &desk_from(
            r#"{"atlas_read": {"as_of": "2026-07-30", "agreement": "aligned",
                 "quantitative_state": "calm",
                 "news_error": "alpaca returned 401 for the news window"}}"#,
        )
        .frame(120, 36),
    );
    assert!(broken.contains("FEED UNAVAILABLE"), "{broken}");
    assert!(broken.contains("returned 401"), "the reason:\n{broken}");
    assert!(
        !broken.contains("-- · --"),
        "a window that was never read got summarised anyway:\n{broken}"
    );

    // And a window that really was read still gets its row.
    let read = content(&desk_from(DESK).frame(120, 36));
    assert!(line_with(&read, "RISK OFF").contains("alpaca"), "{read}");
}

#[test]
fn the_narrow_frame_keeps_the_equity_the_dropped_grid_was_carrying() {
    // Dropping the tile grid dropped the book's value with it, so at the
    // workstation's other baseline DESK showed no equity at all. Every other
    // tile has a home on BOOK or MKTS; this number's only other home is the
    // hero that was just dropped.
    let narrow = content(&desk_from(DESK).frame(90, 24));
    assert!(
        line_with(&narrow, "EQUITY").contains("$10,000.00"),
        "{narrow}"
    );
    // The advice about the window survives beside it, on a row of its own —
    // at this width the two do not fit side by side.
    assert!(narrow.contains("▸ the tiles need"), "{narrow}");

    // Wide enough for the grid, and the hero owns the figure again — a second
    // copy on the read's last row would be the same number twice.
    let wide = content(&desk_from(DESK).frame(120, 36));
    assert!(!wide.contains("EQUITY $"), "{wide}");
    assert!(!wide.contains("▸ the tiles need"), "{wide}");
}

#[test]
fn the_read_types_itself_in_top_to_bottom() {
    // The signature motion. Time is data: the fraction on screen is computed
    // from the two instants handed in, so a mid-reveal frame is exact rather
    // than sampled — the flaky class this client refuses to write.
    let mut client = desk_from(DESK);
    let t0 = client.now;
    client.fx.flashes.reveal(t0);

    client.now = t0 + Duration::from_millis(300);
    let half = content(&client.frame(120, 36));
    assert!(
        half.contains(FIRST_WORDS),
        "the head is not up yet:\n{half}"
    );
    assert!(
        !half.contains(LAST_WORDS),
        "the whole read landed at once:\n{half}"
    );

    client.now = t0 + Duration::from_millis(600);
    let whole = content(&client.frame(120, 36));
    assert!(
        whole.contains(LAST_WORDS),
        "the read never finished:\n{whole}"
    );
}

#[test]
fn a_new_read_starts_the_reveal_over() {
    // A changed `as_of` is a new read, and a new read is typed in from the top.
    // Without the restart the second read would appear whole while the first
    // was still mid-reveal, which reads as a glitch rather than as news.
    let mut client = desk_from(DESK);
    let t0 = client.now;
    client.fx.flashes.reveal(t0);
    client.now = t0 + Duration::from_secs(5);
    assert!(content(&client.frame(120, 36)).contains(LAST_WORDS));

    let t1 = t0 + Duration::from_secs(5);
    client.fx.flashes.reveal(t1);
    let restarted = content(&client.frame(120, 36));
    assert!(
        !restarted.contains(FIRST_WORDS),
        "the reveal did not start over:\n{restarted}"
    );
}

#[test]
fn a_read_nobody_announced_is_already_on_screen() {
    // An operator who switches to DESK an hour after the last read must see it
    // whole. A reveal that had never been started rendering as zero characters
    // would leave the pane permanently blank on every frame that follows.
    let content = content(&desk_from(DESK).frame(120, 36));
    assert!(content.contains(LAST_WORDS), "{content}");
}

#[test]
fn what_the_read_is_grounded_in_never_types_itself_in_and_never_folds_away() {
    // Provenance is the label on the argument rather than a claim inside it. A
    // long read pushes its own tail behind a `▾ more` marker, and the one line
    // that may not go that way is the one that says which read this is.
    let mut client = desk_from(DESK);
    client.fx.flashes.reveal(client.now);
    let opening = content(&client.frame(120, 36));
    assert!(
        !opening.contains(FIRST_WORDS),
        "the body has not started yet:\n{opening}"
    );
    assert!(
        opening.contains(PROVENANCE),
        "the grounding waited for the reveal:\n{opening}"
    );
}

// -- the hero --------------------------------------------------------------

#[test]
fn the_hero_states_the_equity_in_glyphs_a_room_can_read() {
    // The figure is drawn from the same field BOOK's ribbon reads, so the two
    // surfaces cannot disagree about what the book is worth.
    let frame = desk_from(DESK).frame(120, 36);
    for row in big_text_rows("$10,000.00") {
        assert!(
            frame.lines().any(|line| line.contains(row.trim_end())),
            "the hero is not rendering {row:?}:\n{frame}"
        );
    }
}

#[test]
fn a_hero_with_no_marked_book_draws_no_figure_at_all() {
    let frame = dark_desk(r#"{"portfolio": {"equity": 12345.0}}"#).frame(120, 36);
    for row in big_text_rows("$12,345.00") {
        assert!(
            !frame.lines().any(|line| line.contains(row.trim_end())),
            "the registry's equity reached the hero:\n{frame}"
        );
    }
}

/// The rows `tui-big-text` renders for `figure`, as this view renders them.
///
/// Asserting against the widget's own output rather than against a hand-copied
/// glyph string: a pin on quadrant characters someone typed once would drift
/// the first time the font or the pixel size changed, and would prove nothing
/// about the number on screen.
fn big_text_rows(figure: &str) -> Vec<String> {
    use ratatui::widgets::Widget;
    let area = ratatui::layout::Rect::new(0, 0, 60, 4);
    let mut buf = ratatui::buffer::Buffer::empty(area);
    tui_big_text::BigText::builder()
        .pixel_size(tui_big_text::PixelSize::Quadrant)
        .lines(vec![figure.into()])
        .build()
        .render(area, &mut buf);
    (0..area.height)
        .map(|y| {
            (0..area.width)
                .map(|x| buf[(x, y)].symbol().to_string())
                .collect::<String>()
        })
        .collect()
}

// -- the tiles -------------------------------------------------------------

#[test]
fn the_tiles_carry_what_the_owner_measured() {
    let content = content(&desk_from(DESK).frame(120, 36));
    // The guarded state the desk acts on, not the raw detector label — the two
    // disagree on purpose and only one of them is a decision.
    assert!(
        line_with(&content, "STATE").contains("UNCERTAIN"),
        "{content}"
    );
    assert!(line_with(&content, "TIER").contains("WARNING"), "{content}");
    assert!(
        line_with(&content, "HEADROOM").contains("25.0%"),
        "{content}"
    );
    assert!(content.contains("PASS"), "the verdict:\n{content}");
    assert!(line_with(&content, "BNDW").contains("44.0%"), "{content}");
    assert!(line_with(&content, "BNDW").contains("40.0%"), "{content}");
    assert!(
        line_with(&content, "2020").contains("-17.3%"),
        "a replay return:\n{content}"
    );
}

#[test]
fn a_replay_the_owner_could_not_run_states_its_reason_and_never_a_number() {
    // The one thing a replay tile may not do: put a plausible number where a
    // window the snapshot does not cover would have gone.
    let content = content(&desk_from(DESK).frame(120, 36));
    let row = line_with(&content, "2008");
    assert!(row.contains("--"), "{content}");
    assert!(
        content.contains("snapshot span"),
        "the reason is missing:\n{content}"
    );
    assert!(
        !content.contains("40.0%") || !line_with(&content, "2008").contains("40.0%"),
        "the unavailable window's return reached the tile:\n{content}"
    );
}

#[test]
fn the_verdict_is_the_newest_one_the_referee_actually_recorded() {
    // The Textual client reads `decisions[0]` and prints "no verdicts yet"
    // whenever the newest row happens to be unadjudicated — which is what the
    // live desk looks like most of the time. The newest *verdict* is the fact
    // the tile is named for.
    let content = content(&desk_from(DESK).frame(120, 36));
    assert!(content.contains("PASS"), "{content}");
    assert!(content.contains("deterministic"), "the source:\n{content}");
}

#[test]
fn a_desk_with_no_verdicts_says_so_rather_than_going_quiet() {
    let content = content(&desk_from(r#"{"decisions": []}"#).frame(120, 36));
    assert!(content.contains("no verdict yet"), "{content}");
}

// -- absent, in flight, and refused ----------------------------------------

#[test]
fn a_section_the_reachable_owner_has_not_sent_is_in_flight_not_missing() {
    // The distinction the throbber exists for: an owner that is up and has not
    // described its stress yet is still working, and `--` would say it looked
    // and found nothing.
    let content = content(&desk_from("{}").frame(120, 36));
    assert!(
        content.contains("waiting for"),
        "nothing said it was in flight:\n{content}"
    );
    assert!(
        content
            .chars()
            .any(|c| ('\u{2800}'..='\u{28ff}').contains(&c)),
        "no throbber is turning:\n{content}"
    );
}

#[test]
fn the_same_desk_with_no_owner_behind_it_reads_missing() {
    // Nothing is in flight when there is nothing to be in flight from.
    let content = content(&dark_desk("{}").frame(120, 36));
    assert!(content.contains("--"), "{content}");
    assert!(
        !content.contains("waiting for"),
        "a dead owner is not a slow one:\n{content}"
    );
}

#[test]
fn a_throbber_turns_on_the_beat_rather_than_at_random() {
    // `Throbber`'s own `Widget::render` picks a random symbol every frame, so a
    // golden drawn through it would never be stable and a frozen client would
    // still look busy. The beat is the phase.
    let mut client = desk_from("{}");
    let first = content(&client.frame(120, 36));
    let same = content(&client.frame(120, 36));
    assert_eq!(first, same, "a repaint changed the throbber");
    for _ in 0..3 {
        client.store.apply(AppEvent::Tick, client.now);
    }
    assert_ne!(
        first,
        content(&client.frame(120, 36)),
        "the throbber never turns"
    );
}

#[test]
fn a_frame_too_narrow_for_both_columns_keeps_the_read_and_says_what_it_dropped() {
    // The read is the view's reason to exist, so the tiles are what goes. A
    // silently dropped column and one that was never built look identical.
    let frame = dark_desk(DESK).frame(96, 36);
    let content = content(&frame);
    assert!(content.contains("Absorption is high"), "{content}");
    assert!(
        content.contains("columns"),
        "the dropped tiles said nothing:\n{content}"
    );
}

#[test]
fn a_frame_too_short_for_the_hero_refuses_the_tiles_rather_than_clipping_them() {
    let content = content(&dark_desk(DESK).frame(120, 10));
    assert!(content.contains("rows"), "{content}");
}

#[test]
fn a_pane_under_its_floor_states_the_floor_and_under_that_draws_nothing() {
    // Both halves of the ledger's Task 13 finding, which this view applies from
    // the start rather than after someone reports a smear. Between the two
    // floors the pane says what it would take; below the width the sentence
    // itself needs, it draws nothing — a frame missing a panel is at least not
    // a frame lying about one.
    let stated = content(&desk_from(DESK).frame(62, 24));
    assert!(
        stated.contains("columns"),
        "the pane vanished instead of saying why:\n{stated}"
    );

    // The shell's own rules survive the crop, so the claim is that the view
    // wrote no words rather than that the columns are empty.
    let blank = content(&desk_from(DESK).frame(50, 24));
    assert!(
        !blank.chars().any(char::is_alphanumeric),
        "a refusal too narrow to read was drawn anyway:\n{blank:?}"
    );
}

#[test]
fn the_desk_draws_at_every_size_a_terminal_can_be() {
    // Every sub-floor bug on this branch has been an arithmetic one that only
    // appears a column or a row either side of a boundary, so the boundaries are
    // walked rather than sampled at the two sizes anyone develops against.
    let client = desk_from(DESK);
    for w in [1, 2, 8, 9, 12, 20, 31, 32, 44, 45, 68, 69, 70, 90, 120, 200] {
        for h in [1, 2, 3, 5, 6, 7, 9, 10, 11, 15, 16, 19, 24, 36, 60] {
            client.frame(w, h);
        }
    }
}
