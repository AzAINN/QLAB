//! AUDIT: the decision queue beside the durable record, and what each posture may do to it.
//!
//! Two claims run through everything here. The pane must show *both* actionable
//! approval statuses, because pending and approved answer different keys and a
//! client that showed only one could never offer the other. And what the keys
//! can do is a function of the posture, not of the build: a featured binary the
//! human did not arm reads GLASS on the status line, and this view has to agree
//! with it.
//!
//! Assertions read through `content`, the columns this view owns. The tape and
//! the pulse rail render words of their own, so a pin on the whole frame could
//! pass on chrome.

mod harness;

use atlas::bus::{AppEvent, Channel, SseEvent};
use atlas::model::Snapshot;
use atlas::store::Store;
use crossterm::event::KeyCode;
use harness::{content, line_with, Client};
use std::time::Instant;

/// The fixture desk, already switched to AUDIT.
fn audit() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('6'));
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

fn audit_from(json: &str) -> Client {
    let mut client = Client::new(store_from(json));
    client.press(KeyCode::Char('6'));
    client
}

#[test]
fn the_audit_view_renders_the_queue_and_the_stream_at_120x36() {
    // The unarmed frame, and therefore the one both legs produce: the fixture
    // store's posture is GLASS, in the operator build as well.
    insta::assert_snapshot!(audit().frame(120, 36));
}

#[test]
fn an_unarmed_window_says_view_only_in_both_builds() {
    // The claim the default artifact rests on, asserted where an operator would
    // read it. In the glass build the branches do not exist; in the operator
    // build they exist and the posture has not armed them — and the pane must
    // look identical either way.
    let mut client = audit();
    let frame = client.frame(120, 36);
    let header = line_with(&frame, "APPROVALS");
    assert!(header.contains("view-only"), "{header}");
    assert!(
        !header.contains("approve"),
        "an unarmed window offered a decision key: {header}"
    );

    // And no key on this pane does anything at all. The arrows included: the
    // only thing the cursor selects is a decision, so ungated they were
    // swallowed to move a marker an unarmed window never draws — a keystroke
    // with no visible effect reads as a hung client.
    for code in [
        KeyCode::Down,
        KeyCode::Down,
        KeyCode::Up,
        KeyCode::Char('a'),
        KeyCode::Char('R'),
    ] {
        client.press(code);
        assert_eq!(
            client.frame(120, 36),
            frame,
            "{code:?} changed an unarmed window"
        );
    }
}

#[test]
fn the_queue_shows_both_statuses_a_client_can_act_on() {
    // The owner serves pending *and* approved-unconsumed, because the execute
    // gate binds to the second. A pane that showed only the first would make
    // the approval a legal fill consumes invisible at exactly the moment it
    // became usable.
    let client = audit_from(
        r#"{"approvals": [
             {"approval_id": "aaaa111122223333", "plan_id": "pppp1111", "status": "pending",
              "expires_at": "2026-07-30T19:12:18+00:00"},
             {"approval_id": "bbbb444455556666", "plan_id": "pppp2222", "status": "approved",
              "expires_at": "2026-07-30T19:30:00+00:00"}]}"#,
    );
    let frame = client.frame(120, 36);
    // Ids are shown as prefixes — the full record is named in the confirm box,
    // which is where an operator is actually deciding.
    let pending = line_with(&frame, "aaaa1111222");
    assert!(pending.contains("pending"), "{pending}");
    assert!(pending.contains("pppp1111"), "{pending}");
    assert!(
        pending.contains("19:12:18"),
        "the expiry is the clock: {pending}"
    );
    let approved = line_with(&frame, "bbbb4444555");
    assert!(approved.contains("approved"), "{approved}");
}

#[test]
fn an_empty_queue_says_so_rather_than_drawing_a_blank_pane() {
    // "Nothing waiting" and "this pane is broken" must not look the same.
    let client = audit_from(r#"{"approvals": []}"#);
    assert!(
        content(&client.frame(120, 36)).contains("no approval is waiting"),
        "{}",
        content(&client.frame(120, 36))
    );
}

#[test]
fn the_stream_is_the_bus_newest_first_with_the_plan_each_row_was_about() {
    // The durable bus's first renderer in this client. Until now an operator
    // who looked away had no way to find out what they missed.
    //
    // Drawn wide, because the subject column is the one that gives at the
    // baseline width — the pane keeps the clock and the kind there, which is
    // what `a_narrow_stream_keeps_the_clock_and_the_kind` pins.
    let frame = audit().frame(160, 36);
    let body = content(&frame);
    let created = body
        .lines()
        .position(|line| line.contains("approval_created"))
        .unwrap_or_else(|| panic!("no approval_created row:\n{body}"));
    let consumed = body
        .lines()
        .position(|line| line.contains("approval_consumed"))
        .unwrap_or_else(|| panic!("no approval_consumed row:\n{body}"));
    assert!(
        created < consumed,
        "the newer event has to be above the older one:\n{body}"
    );
    // Each row names what it was about, which is the plan.
    assert!(
        line_with(&frame, "approval_created").contains("9661b0e88b4a669e"),
        "{body}"
    );
}

#[test]
fn an_event_that_arrives_on_the_stream_lands_on_the_pane() {
    // Invariant 10 at the seam it keeps biting: a ring nothing renders is
    // indistinguishable from a ring that does not exist.
    let mut client = audit();
    client.store.apply(
        AppEvent::Sse(SseEvent {
            kind: "referee_verdict".into(),
            payload: serde_json::json!({"decision_id": "dddd1111"}),
            ts: Some("2026-07-30T18:44:02+00:00".into()),
            id: Some("live-1".into()),
        }),
        client.now,
    );
    let frame = client.frame(160, 36);
    let row = line_with(&frame, "referee_verdict");
    assert!(row.contains("18:44:02"), "{row}");
    assert!(row.contains("dddd1111"), "{row}");
}

#[test]
fn a_narrow_stream_drops_the_subject_and_keeps_what_makes_a_row_findable() {
    // The baseline frame gives this pane about thirty columns. What has to
    // survive is the clock and the kind: an operator scanning for "when did the
    // desk halt" reads those two, and a row that lost its kind to fit an id is
    // a row they cannot find at all.
    let frame = audit().frame(120, 36);
    let row = line_with(&frame, "approval_created");
    assert!(row.contains("18:12:18"), "{row}");
}

#[test]
fn a_stream_with_nothing_on_it_says_so() {
    let client = audit_from(r#"{"events": []}"#);
    assert!(
        content(&client.frame(120, 36)).contains("nothing on the bus yet"),
        "{}",
        content(&client.frame(120, 36))
    );
}

#[test]
fn a_narrow_queue_drops_the_expiry_whole_rather_than_half_a_clock() {
    // `19:1` is worse than no expiry at all, because it still looks like a
    // time — and it is the deadline of the decision an operator is about to
    // make. Between the pane's floor and its design width the row drops the
    // whole column instead.
    let client = audit_from(
        r#"{"approvals": [{"approval_id": "aaaa111122223333", "plan_id": "pppp1111",
             "status": "pending", "expires_at": "2026-07-30T19:12:18+00:00"}]}"#,
    );
    let wide = client.frame(120, 36);
    assert!(line_with(&wide, "aaaa1111222").contains("19:12:18"));
    // 112 is the pane's refusal floor and 116 is where it reaches its design
    // width; in between is exactly the band the `Paragraph` used to clip.
    for width in 112u16..116 {
        let narrow = client.frame(width, 36);
        let row = line_with(&narrow, "aaaa1111222");
        assert!(
            row.contains("19:12:18") || !row.contains("19:"),
            "half a clock survived at {width}: {row}"
        );
        assert!(row.contains("pending"), "{row}");
    }
}

#[test]
fn a_pane_too_narrow_for_two_columns_says_what_it_would_take() {
    // A truncated approval id is not an approval id, and an operator about to
    // decide has to be able to read which record it is.
    let client = audit();
    let narrow = content(&client.frame(60, 36));
    assert!(narrow.contains("AUDIT needs"), "{narrow}");
}

// -- the armed window -------------------------------------------------------

#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::cmd::Command;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers};

    fn armed(json: &str) -> Client {
        let mut store = super::store_from(json);
        store.posture = Posture::Operator;
        let mut client = Client::new(store);
        client.press(KeyCode::Char('6'));
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

    fn queue() -> Client {
        armed(
            r#"{"approvals": [
                 {"approval_id": "aaaa111122223333", "plan_id": "pppp1111", "status": "pending",
                  "expires_at": "2026-07-30T19:12:18+00:00"},
                 {"approval_id": "bbbb444455556666", "plan_id": "pppp2222", "status": "approved"}]}"#,
        )
    }

    #[test]
    fn the_armed_audit_view_renders_its_keys_at_120x36() {
        insta::assert_snapshot!(queue().frame(120, 36));
    }

    #[test]
    fn an_armed_window_offers_the_two_decision_keys() {
        let frame = queue().frame(120, 36);
        let header = line_with(&frame, "APPROVALS");
        assert!(header.contains("a approve"), "{header}");
        // `R`, not `r`: the shell owns lowercase `r` for the refresh every view
        // depends on, and a pane that took it would leave an operator on AUDIT
        // unable to ask the owner for a fresh snapshot.
        assert!(header.contains("R reject"), "{header}");
    }

    #[test]
    fn a_decision_key_opens_a_confirm_box_rather_than_deciding() {
        // Approving is not booking, so there is no plan hash to bind — but a
        // decision that authorises a later fill is still not something a stray
        // keystroke may make.
        let mut client = queue();
        assert_eq!(
            press(&mut client, KeyCode::Char('a')),
            None,
            "no command yet"
        );
        let frame = client.frame(120, 36);
        assert!(frame.contains("APPROVE APPROVAL"), "{frame}");
        assert!(frame.contains("type CONFIRM"), "{frame}");
        assert!(
            frame.contains("aaaa111122223333"),
            "the box has to name what it is deciding: {frame}"
        );
    }

    #[test]
    fn only_the_typed_word_turns_a_confirm_box_into_a_command() {
        let mut client = queue();
        press(&mut client, KeyCode::Char('a'));
        // Enter before the word leaves the box up: a human who mistyped has to
        // see that they did.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(client.frame(120, 36).contains("APPROVE APPROVAL"));

        for c in "CONFIRM".chars() {
            press(&mut client, KeyCode::Char(c));
        }
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::Approve("aaaa111122223333".into()))
        );
        // Answering spends the box, so a second Enter cannot send the same
        // human decision again.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(!client.frame(120, 36).contains("APPROVE APPROVAL"));
    }

    #[test]
    fn reject_is_its_own_verb_and_its_own_command() {
        let mut client = queue();
        assert_eq!(
            press(&mut client, KeyCode::Char('r')),
            Some(Command::Refresh),
            "lowercase r is still the workstation's refresh, even here"
        );
        press(&mut client, KeyCode::Char('R'));
        assert!(client.frame(120, 36).contains("REJECT APPROVAL"));
        for c in "CONFIRM".chars() {
            press(&mut client, KeyCode::Char(c));
        }
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::Reject("aaaa111122223333".into()))
        );
    }

    #[test]
    fn a_decision_is_offered_only_on_an_approval_that_is_still_pending() {
        // The owner refuses to re-decide an approved record ("approval is
        // 'approved', not pending"), so offering the key would teach the
        // operator that the refusal is the client's fault.
        let mut client = queue();
        press(&mut client, KeyCode::Down); // onto the approved row
        assert_eq!(press(&mut client, KeyCode::Char('a')), None);
        let frame = client.frame(120, 36);
        assert!(
            !frame.contains("APPROVE APPROVAL"),
            "an approved record must not be re-decided: {frame}"
        );
    }

    #[test]
    fn a_modal_takes_every_key_including_the_ones_the_shell_owns() {
        // `q` quits and `3` switches views — both are characters the challenge
        // field has to accept, and a global key that walked away would leave a
        // human having half-answered a question about a decision.
        let mut client = queue();
        press(&mut client, KeyCode::Char('a'));
        assert_eq!(
            press(&mut client, KeyCode::Char('q')),
            None,
            "q must not quit"
        );
        assert_eq!(press(&mut client, KeyCode::Char('3')), None);
        assert_eq!(
            client.store.nav.view,
            atlas::store::ViewId::Audit,
            "a digit walked out of an open question"
        );
        // Esc is the way out, and it decides nothing.
        assert_eq!(press(&mut client, KeyCode::Esc), None);
        assert!(!client.frame(120, 36).contains("APPROVE APPROVAL"));
    }

    #[test]
    fn the_queue_scrolls_so_the_cursor_is_never_off_screen() {
        // The cursor is clamped to the whole queue, not to what fits. A fixed top
        // would leave `a`/`R` acting on a row nobody can see.
        //
        // Armed, because the cursor is: the only thing it selects is a decision, so
        // an unarmed window declines the arrows rather than moving a marker it
        // never draws.
        let rows: Vec<String> = (0..12)
            .map(|i| {
                format!(
                    r#"{{"approval_id": "ap{i:02}00000000", "plan_id": "pl{i:02}", "status": "pending"}}"#
                )
            })
            .collect();
        let mut client = armed(&format!(r#"{{"approvals": [{}]}}"#, rows.join(",")));
        for _ in 0..11 {
            client.press(KeyCode::Down);
        }
        // Shown as an eleven-character prefix, like every other id in this pane.
        let short = content(&client.frame(120, 14));
        assert!(short.contains("ap110000000"), "{short}");
    }

    #[test]
    fn an_empty_queue_has_nothing_to_decide() {
        let mut client = armed(r#"{"approvals": []}"#);
        assert_eq!(press(&mut client, KeyCode::Char('a')), None);
        assert!(!client.frame(120, 36).contains("APPROVE APPROVAL"));
    }
}
