//! WORKFORCE: the pipelines, the console under them, and what each posture may ask of the desk.
//!
//! Three claims run through everything here. A pipeline has to say *where a run
//! has got to* — the thing a status word cannot say, since `running` is true of
//! a workflow on its first phase and of one on its last. The console has to be
//! the workforce's own slice of the bus and not a second copy of AUDIT. And the
//! input row is a function of the *posture*: a featured binary the human did not
//! arm reads GLASS on the status line, and this view has to agree with it by
//! drawing no row at all.
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

/// The fixture desk, already switched to WORKFORCE.
fn workforce() -> Client {
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
    harness::no_door(&mut store);
    store
}

/// The rendered line for one workflow, read through the columns this view owns.
///
/// Not `line_with(frame, id)`: the header carries the *driving* run's id too, so
/// a bare search finds the chip and passes on a pane with no rows at all.
fn flow_row(frame: &str, id: &str) -> String {
    let body = content(frame);
    body.lines()
        .find(|line| line.contains(id) && !line.contains("WORKFLOWS"))
        .unwrap_or_else(|| panic!("no workflow row for {id:?}:\n{body}"))
        // The backend quotes each row and the shell draws a rule down the left
        // of the content area, so the view's own first column — where the
        // coordinator marker lives — is two characters in.
        .trim_start_matches(['"', '│'])
        .to_string()
}

fn workforce_from(json: &str) -> Client {
    let mut client = Client::new(store_from(json));
    client.press(KeyCode::Char('6'));
    client
}

/// A desk with one five-phase run, three phases in.
fn running() -> &'static str {
    r#"{"workflows": [{
         "workflow_id": "805e0729cfec4d67", "kind": "portfolio_review", "status": "running",
         "current_phase": "referee",
         "request": {"goal": "Review the current portfolio and market read."},
         "created_at": "2026-07-30T15:29:22.831208+00:00",
         "updated_at": "2026-07-30T15:58:50.091972+00:00",
         "steps": [
           {"step_id": "w:analyst", "seq": 0, "phase": "analyst", "status": "done",
            "summary": "Regime NEUTRAL (fragile calm, 2 of 5 disagree)."},
           {"step_id": "w:challenger", "seq": 1, "phase": "challenger", "status": "done"},
           {"step_id": "w:optimizer", "seq": 2, "phase": "optimizer", "status": "done"},
           {"step_id": "w:referee", "seq": 3, "phase": "referee", "status": "working"},
           {"step_id": "w:reporter", "seq": 4, "phase": "reporter", "status": "queued"}]}],
       "atlas_heartbeat": {"coordinator": {"driving": true, "workflow_id": "805e0729cfec4d67"}}}"#
}

#[test]
fn the_workforce_view_renders_its_pipelines_and_console_at_120x36() {
    // The unarmed frame, and therefore the one both legs produce: the fixture
    // store's posture is GLASS, in the operator build as well.
    insta::assert_snapshot!(workforce().frame(120, 36));
}

#[test]
fn a_pipeline_says_where_a_run_has_got_to_and_a_status_word_cannot() {
    let client = workforce_from(running());
    let frame = client.frame(120, 36);
    let graph = line_with(&frame, "◉");
    // Three done, one open, one waiting — the shape a `running` status hides.
    assert!(graph.contains("●───●───●"), "{graph}");
    assert!(graph.contains("◉───○"), "{graph}");
    // And the run says what it last said, which nothing else on the desk does.
    assert!(graph.contains("referee"), "{graph}");
    let head = flow_row(&frame, "805e0729");
    assert!(head.contains("running"), "{head}");
    assert!(
        head.contains("Review the current"),
        "the goal is what a run was asked for: {head}"
    );
}

#[test]
fn the_span_is_the_owners_own_two_stamps_and_never_this_clients_clock() {
    // 15:29:22 → 15:58:50 is twenty-nine minutes of the owner's own record.
    // Subtracting a stamp from this client's clock would assert agreement
    // between two machines' clocks *and* read a clock inside a renderer.
    let client = workforce_from(running());
    let frame = client.frame(120, 36);
    let row = flow_row(&frame, "805e0729");
    assert!(row.contains("29m"), "{row}");
}

#[test]
fn the_marker_says_a_coordinator_is_on_this_run_not_merely_that_it_is_running() {
    // Registering a workflow is not running it. A row that showed only
    // `running` would render a parked run and a live one identically — which is
    // the bug the owner's own coordinator loop was built to close.
    let driven = workforce_from(running());
    let frame = driven.frame(120, 36);
    assert!(
        line_with(&frame, "WORKFLOWS").contains("driving 805e0729"),
        "{}",
        line_with(&frame, "WORKFLOWS")
    );
    assert!(
        flow_row(&frame, "805e0729").starts_with('▌'),
        "the driven run wears no marker: {:?}",
        flow_row(&frame, "805e0729")
    );

    // And an owner that is *not* driving says why, rather than leaving an
    // operator watching a still pipeline with no way to tell idle from broken.
    let parked = workforce_from(
        r#"{"workflows": [{"workflow_id": "aaaa1111bbbb2222", "status": "running",
             "steps": [{"step_id": "s1", "phase": "analyst", "status": "queued"}]}],
            "atlas_heartbeat": {"coordinator": {"driving": false,
             "reason": "no claude binary on PATH"}}}"#,
    );
    let quiet = parked.frame(120, 36);
    let header = line_with(&quiet, "WORKFLOWS");
    assert!(header.contains("no claude binary"), "{header}");
    assert!(
        !flow_row(&quiet, "aaaa1111").starts_with('▌'),
        "an undriven run wore the coordinator's marker"
    );
}

#[test]
fn a_run_the_owner_never_stamped_renders_absent_rather_than_zero() {
    // `Some("")` is absent here as everywhere else, and a span of `0s` would
    // say the owner measured something it did not.
    let client = workforce_from(
        r#"{"workflows": [{"workflow_id": "cccc3333dddd4444", "status": "running",
             "created_at": "", "updated_at": null,
             "steps": [{"step_id": "s1", "phase": "analyst", "status": "queued"}]}]}"#,
    );
    let frame = client.frame(120, 36);
    let row = flow_row(&frame, "cccc3333");
    assert!(row.contains("--"), "{row}");
    assert!(!row.contains("0s"), "{row}");
}

#[test]
fn a_status_the_client_has_never_heard_of_still_draws_rather_than_vanishing() {
    // A contract change with the owner must be visible, not silently rendered
    // as an empty pipeline. The unknown node is `?`.
    let client = workforce_from(
        r#"{"workflows": [{"workflow_id": "eeee5555ffff6666", "status": "reticulating",
             "steps": [{"step_id": "s1", "phase": "analyst", "status": "reticulating"},
                       {"step_id": "s2", "phase": "referee", "status": "queued"}]}]}"#,
    );
    let frame = client.frame(120, 36);
    assert!(flow_row(&frame, "eeee5555").contains("reticulating"));
    assert!(content(&frame).contains("?───○"), "{}", content(&frame));
}

#[test]
fn an_empty_desk_says_no_workflow_has_run_rather_than_drawing_a_blank_pane() {
    let client = workforce_from(r#"{"workflows": []}"#);
    let body = content(&client.frame(120, 36));
    assert!(body.contains("no workflow has run"), "{body}");
    assert!(
        body.contains("the workforce has said nothing yet"),
        "{body}"
    );
}

#[test]
fn the_console_is_the_workforce_slice_of_the_bus_and_not_a_second_audit() {
    // Six kinds, newest first. The fixture's bus is approvals, which belong to
    // AUDIT — a console that took them too would be a worse copy of that pane
    // sitting under the pipelines.
    let mut client = workforce();
    for (kind, payload) in [
        (
            "workflow_phase",
            serde_json::json!({"workflow_id": "805e0729", "phase": "referee"}),
        ),
        ("atlas_message", serde_json::json!({"text": "why flat?"})),
    ] {
        client.store.apply(
            AppEvent::Sse(SseEvent {
                kind: kind.into(),
                payload,
                ts: Some("2026-07-30T18:44:02+00:00".into()),
                id: Some(format!("live-{kind}")),
            }),
            client.now,
        );
    }
    let frame = client.frame(160, 36);
    let body = content(&frame);
    assert!(line_with(&frame, "workflow_phase").contains("805e0729"));
    // A message is a sentence, not JSON: `{"text":"why flat?"}` in front of an
    // operator is the payload leaking through the pane.
    let asked = line_with(&frame, "atlas_message");
    assert!(asked.contains("why flat?"), "{asked}");
    assert!(!asked.contains("{\"text\""), "{asked}");
    assert!(asked.contains("18:44:02"), "{asked}");
    // Newest first, exactly as a log is read.
    let message = body.lines().position(|l| l.contains("atlas_message"));
    let phase = body.lines().position(|l| l.contains("workflow_phase"));
    assert!(message < phase, "the console is not newest first:\n{body}");
    // And the governance bus stays where it belongs.
    assert!(
        !body.contains("approval_created"),
        "the console took AUDIT's rows:\n{body}"
    );
}

/// One coordinator row, delivered over the stream exactly as the owner writes
/// it (`qlab/operator/coordinator.py::_on_event`).
fn agent_event(client: &mut Client, id: &str, event_kind: &str, agent: &str, text: &str) {
    client.store.apply(
        AppEvent::Sse(SseEvent {
            kind: "atlas_coordinator_event".into(),
            payload: serde_json::json!({
                "workflow_id": "805e0729cfec4d67", "event_kind": event_kind,
                "agent": agent, "tool": "", "text": text}),
            ts: Some("2026-07-30T18:44:02+00:00".into()),
            id: Some(id.into()),
        }),
        client.now,
    );
}

#[test]
fn the_console_carries_the_agents_own_words() {
    // The whole point of the pane: an operator watching a governed run reads
    // what the agent said, not a status word the desk inferred.
    let mut client = workforce_from(running());
    agent_event(
        &mut client,
        "live-coord-1",
        "tool_start",
        "moments-analyst",
        "calling moments_estimate",
    );
    let frame = client.frame(160, 36);
    let row = line_with(&frame, "moments-analyst");
    assert!(row.contains("calling moments_estimate"), "{row}");
    // The payload's own id must not win the subject: a coordinator row keyed to
    // its workflow would render every agent's turn as the same eight hex
    // characters, which is the pane saying nothing at all.
    assert!(!row.contains("805e0729"), "{row}");
    assert!(!row.contains("{\"agent\""), "{row}");
}

#[test]
fn silence_is_reported_rather_than_animated_on_the_pane() {
    // Pinned at the rendered surface, not at the constructor: the frame is what
    // an operator reads, and a spinner that spun regardless is the failure this
    // line exists to refuse.
    use atlas::ui::views::workforce::SILENCE_AFTER;

    let mut client = workforce_from(running());
    agent_event(
        &mut client,
        "live-coord-1",
        "text",
        "moments-analyst",
        "the window is fragile calm",
    );
    let heard = content(&client.frame(160, 36));
    assert!(heard.contains("spoke 0s ago"), "{heard}");

    client.now += SILENCE_AFTER;
    let quiet = content(&client.frame(160, 36));
    assert!(quiet.contains("no word for 45s"), "{quiet}");
    assert!(!quiet.contains("spoke"), "{quiet}");
}

#[test]
fn a_parked_desk_claims_no_activity_at_all() {
    // Derived from `driving`, so a run nobody is walking says nothing rather
    // than reporting the age of the last thing it said as if it were live.
    let mut client = workforce_from(
        r#"{"workflows": [{"workflow_id": "805e0729cfec4d67", "status": "running",
             "steps": [{"step_id": "s1", "phase": "analyst", "status": "queued"}]}],
            "atlas_heartbeat": {"coordinator": {"driving": false, "reason": "nothing to do",
             "workflow_id": "805e0729cfec4d67"}}}"#,
    );
    agent_event(&mut client, "live-coord-1", "text", "referee", "PASS");
    let body = content(&client.frame(160, 36));
    assert!(!body.contains("spoke"), "{body}");
    assert!(!body.contains("no word for"), "{body}");
}

#[test]
fn a_driving_coordinator_with_no_run_id_still_reports_its_activity() {
    // The owner reads `driver.busy` and `current_workflow_id` separately, so a
    // dispatch can be seen between them. A derived fact dropped because there
    // was no row to hang it on is the desk going quiet about itself.
    let client = workforce_from(
        r#"{"workflows": [{"workflow_id": "805e0729cfec4d67", "status": "running",
             "steps": [{"step_id": "s1", "phase": "analyst", "status": "working"}]}],
            "atlas_heartbeat": {"coordinator": {"driving": true, "workflow_id": ""}}}"#,
    );
    let body = content(&client.frame(160, 36));
    assert!(body.contains("no word yet"), "{body}");
    // And `Some("")` is absent: an absent coordinator id must not *match* a
    // flow whose own id is empty. Both become `None` under `format::text`, so
    // an equality test alone would hang the line under a run the coordinator
    // never named — and, since the header already carries it, say it twice.
    let orphan = workforce_from(
        r#"{"workflows": [{"workflow_id": "", "status": "running",
             "steps": [{"step_id": "s1", "phase": "analyst", "status": "working"}]}],
            "atlas_heartbeat": {"coordinator": {"driving": true}}}"#,
    );
    let once = content(&orphan.frame(160, 36));
    assert_eq!(
        once.matches("no word yet").count(),
        1,
        "the activity line was drawn against a run the coordinator did not name:\n{once}"
    );
}

#[test]
fn an_arriving_console_row_lights_and_the_log_under_it_does_not() {
    // The AUDIT flash, reused rather than re-derived: keyed on the owner's own
    // event id, so the row that arrived is the row that lights.
    use atlas::fx::{FlashKey, FlashTracker};
    use atlas::theme::theme;

    let mut fx = FlashTracker::default();
    let now = Instant::now();
    fx.flash(FlashKey::audit("live-1"), now);
    let base = ratatui::style::Style::default();
    assert_eq!(
        fx.style_for(&FlashKey::audit("live-1"), now, base).bg,
        Some(theme().accent_dim)
    );
    assert_eq!(
        fx.style_for(&FlashKey::audit("older"), now, base),
        base,
        "the arrival lit a row it was not about"
    );
}

#[test]
fn a_goal_too_long_for_its_column_is_cut_at_a_word_and_says_it_was_cut() {
    // A hard slice turns one goal into a different one. The column narrows as
    // the terminal does, so this is checked across the band where it bites.
    let client = workforce_from(running());
    for width in [96u16, 104, 120, 160] {
        let frame = client.frame(width, 36);
        let row = flow_row(&frame, "805e0729");
        assert!(row.contains("Review the"), "{width}: {row}");
        assert!(
            !row.contains("portfo "),
            "{width}: a goal was cut mid-word: {row}"
        );
    }
}

#[test]
fn a_pane_too_narrow_for_a_pipeline_beside_its_goal_says_what_it_would_take() {
    let client = workforce();
    let narrow = content(&client.frame(60, 36));
    assert!(narrow.contains("WORKFORCE needs"), "{narrow}");
}

#[test]
fn an_unarmed_window_draws_no_input_row_and_answers_no_key() {
    // The claim the default artifact rests on, asserted where an operator would
    // read it. In the glass build the branches do not exist; in the operator
    // build they exist and the posture has not armed them — and the pane must
    // look identical either way. Hidden, not disabled: a prompt that cannot be
    // typed into is a client that looks broken.
    let mut client = workforce();
    let frame = client.frame(120, 36);
    let body = content(&frame);
    assert!(!body.contains("ask the desk"), "{body}");
    assert!(!body.contains("start a workflow"), "{body}");

    for code in [KeyCode::Char('i'), KeyCode::Char('S'), KeyCode::Down] {
        client.press(code);
        assert_eq!(
            client.frame(120, 36),
            frame,
            "{code:?} changed an unarmed window"
        );
    }
}

// -- the armed window -------------------------------------------------------

#[cfg(feature = "operator")]
mod armed {
    use super::*;
    use atlas::cmd::Command;
    use atlas::model::Template;
    use atlas::store::Posture;
    use crossterm::event::{KeyEvent, KeyModifiers};

    fn armed(json: &str) -> Client {
        let mut store = super::store_from(json);
        store.posture = Posture::Operator;
        store.apply(AppEvent::Templates(templates()), Instant::now());
        let mut client = Client::new(store);
        client.press(KeyCode::Char('6'));
        // One frame before any key, exactly as the runtime draws one before it
        // reads its first event. The pane publishes its height there, and the
        // picker's floor is read off it — a client that has never drawn cannot
        // know it has room, and refuses.
        client.frame(120, 36);
        client
    }

    /// The owner's own registry, as `/api/atlas/templates` serves it.
    fn templates() -> Vec<Template> {
        serde_json::from_value(serde_json::json!([
            {"template_id": "regime_review",
             "purpose": "Re-read the regime panel and challenge the current estimate.",
             "phases": ["analyst", "challenger", "optimizer", "referee", "reporter"],
             "creates_plan": false, "needs_coordinator": true},
            {"template_id": "desk_rebalance_review",
             "purpose": "Full review ending in a checked plan proposed for approval.",
             "phases": ["analyst", "challenger", "optimizer", "referee", "reporter"],
             "creates_plan": true, "needs_coordinator": true}
        ]))
        .unwrap()
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

    fn desk() -> Client {
        armed(super::running())
    }

    #[test]
    fn the_armed_workforce_view_renders_its_input_row_at_120x36() {
        insta::assert_snapshot!(desk().frame(120, 36));
    }

    #[test]
    fn an_armed_window_offers_the_two_things_a_human_may_ask_of_the_desk() {
        let body = content(&desk().frame(120, 36));
        assert!(body.contains("i ask the desk"), "{body}");
        assert!(body.contains("S start a workflow"), "{body}");
    }

    #[test]
    fn a_question_reaches_the_desk_only_once_the_operator_presses_enter() {
        let mut client = desk();
        assert_eq!(
            press(&mut client, KeyCode::Char('i')),
            None,
            "no command yet"
        );
        typed(&mut client, "why are we flat?");
        assert!(
            content(&client.frame(120, 36)).contains("why are we flat?"),
            "the field does not show what was typed"
        );
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::Message("why are we flat?".into()))
        );
        // Sending spends the field, so a second Enter cannot repeat the
        // question — and the row goes back to its hints.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(content(&client.frame(120, 36)).contains("i ask the desk"));
    }

    #[test]
    fn an_empty_question_is_not_a_question() {
        // The owner refuses it anyway ("message text is required"), which would
        // reach the operator as a failed write rather than as a slip.
        let mut client = desk();
        press(&mut client, KeyCode::Char('i'));
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        typed(&mut client, "   ");
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        // Esc abandons it without asking anything.
        press(&mut client, KeyCode::Esc);
        assert!(content(&client.frame(120, 36)).contains("i ask the desk"));
    }

    #[test]
    fn a_field_owns_every_key_including_the_ones_the_shell_claims() {
        // The seam this pane needed: `q`, `r` and the digits are the
        // workstation's, and a goal containing "requote" would otherwise
        // refresh the desk, jump to BOOK and quit before its third character.
        let mut client = desk();
        press(&mut client, KeyCode::Char('i'));
        assert_eq!(press(&mut client, KeyCode::Char('q')), None, "q quit");
        assert_eq!(press(&mut client, KeyCode::Char('r')), None, "r refreshed");
        assert_eq!(press(&mut client, KeyCode::Char('3')), None);
        assert_eq!(
            client.store.nav.view,
            atlas::store::ViewId::Workforce,
            "a digit walked out of a field being typed into"
        );
        assert!(content(&client.frame(120, 36)).contains("qr3"));

        // And the one key that must still work. Raw mode disables ISIG, so this
        // arrives as a keystroke — a field that swallowed it would leave the
        // operator's only exit reflex dead in a fullscreen client.
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
    fn an_unfocused_pane_leaves_the_workstations_keys_alone() {
        // The other half: `typing` is a claim about *right now*. A view that
        // answered true because it merely has a field would cost the
        // workstation its navigation permanently.
        let mut client = desk();
        assert_eq!(
            press(&mut client, KeyCode::Char('r')),
            Some(Command::Refresh)
        );
        press(&mut client, KeyCode::Char('4'));
        assert_eq!(client.store.nav.view, atlas::store::ViewId::Book);
    }

    #[test]
    fn the_picker_lists_the_owners_templates_and_marks_the_ones_that_make_plans() {
        let mut client = desk();
        assert_eq!(
            press(&mut client, KeyCode::Char('S')),
            None,
            "no command yet"
        );
        let frame = client.frame(120, 36);
        assert!(frame.contains("START A WORKFLOW"), "{frame}");
        assert!(frame.contains("regime_review"), "{frame}");
        // The authority boundary, where an operator meets it: the owner refuses
        // a plan-creating template below `propose`, so the request that is going
        // to be declined has to be visible before it is made.
        assert!(
            line_with(&frame, "desk_rebalance_review").contains("plan"),
            "{}",
            line_with(&frame, "desk_rebalance_review")
        );
        assert!(!line_with(&frame, "regime_review").contains(" plan "));
        // The documented property of the route this key calls. An operator who
        // picked a template with its own declared graph would otherwise read
        // the owner's standard five phases as a bug.
        assert!(
            frame.contains("the owner runs its own phase graph"),
            "{frame}"
        );
    }

    #[test]
    fn a_picked_template_and_a_typed_goal_are_what_start_a_run() {
        let mut client = desk();
        press(&mut client, KeyCode::Char('S'));
        press(&mut client, KeyCode::Down);
        typed(&mut client, "check the drift");
        assert_eq!(
            press(&mut client, KeyCode::Enter),
            Some(Command::StartWorkflow {
                template: "desk_rebalance_review".into(),
                goal: "check the drift".into(),
            })
        );
        assert!(
            !client.frame(120, 36).contains("START A WORKFLOW"),
            "the picker stayed up after starting a run"
        );
    }

    #[test]
    fn a_run_with_no_stated_goal_is_not_started() {
        // The owner would default it, and a run nobody stated a purpose for is
        // one whose record cannot later be read for intent. The goal is also
        // the *only* thing this key contributes — the owner picks the graph.
        let mut client = desk();
        press(&mut client, KeyCode::Char('S'));
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(
            client.frame(120, 36).contains("START A WORKFLOW"),
            "box closed"
        );
        // Esc abandons it, and starts nothing.
        press(&mut client, KeyCode::Esc);
        assert!(!client.frame(120, 36).contains("START A WORKFLOW"));
    }

    /// Whether the active view is holding a text field open — the shell's own
    /// question, asked the way the shell asks it.
    fn typing(client: &Client) -> bool {
        client.views.typing(client.store.nav.view)
    }

    #[test]
    fn a_pane_too_short_for_the_picker_refuses_to_open_it_rather_than_arming_it() {
        // The state that must not exist: the box drawn two rows tall with an
        // inner height of zero — no template list, no goal field, no
        // phase-graph disclosure — while the field still held the keyboard and
        // Enter still started a governed run against it.
        let mut client = desk();
        // The frame is what publishes the height a key handler reads, exactly
        // as the runtime's loop does it.
        let short = client.frame(120, 8);
        assert!(!short.contains("START A WORKFLOW"), "{short}");

        press(&mut client, KeyCode::Char('S'));
        let refused = client.frame(120, 8);
        assert!(
            content(&refused).contains("the template picker needs"),
            "{}",
            content(&refused)
        );
        assert!(
            !refused.contains("START A WORKFLOW"),
            "the box opened anyway: {refused}"
        );
        // It holds no keyboard, so the workstation's keys still work...
        assert!(!typing(&client), "an invisible picker armed the keyboard");
        // ...and Enter cannot start the run it never showed.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
    }

    #[test]
    fn a_terminal_that_shrinks_under_an_open_picker_closes_it() {
        // Never an armed state an operator cannot see. Opening at a height that
        // fits and then shrinking must not leave the goal field holding the
        // keyboard behind a box that is no longer drawn.
        let mut client = desk();
        client.frame(120, 36);
        press(&mut client, KeyCode::Char('S'));
        typed(&mut client, "check the drift");
        assert!(client.frame(120, 36).contains("START A WORKFLOW"));
        assert!(typing(&client), "the open picker should hold the keyboard");

        let shrunk = client.frame(120, 8);
        assert!(
            content(&shrunk).contains("the template picker needs"),
            "{}",
            content(&shrunk)
        );
        assert!(!typing(&client), "the shrunk picker kept the keyboard");
        // Enter retires it rather than starting the run whose goal is still
        // typed into a box nobody can see.
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        // And the half-typed goal is gone with it, so growing back cannot
        // restore a field the operator has not looked at since.
        client.frame(120, 36);
        let back = client.frame(120, 36);
        assert!(!back.contains("check the drift"), "{back}");
        assert!(!typing(&client));
    }

    #[test]
    fn the_pickers_floor_is_bracketed_on_both_sides() {
        // One row below it refuses, and the floor itself draws every row the
        // box cannot do without — the header, a template, the goal field, and
        // the sentence about whose phase graph runs.
        let mut client = desk();
        client.frame(120, 9);
        press(&mut client, KeyCode::Char('S'));
        assert!(content(&client.frame(120, 9)).contains("the template picker needs"));
        assert!(!typing(&client));

        // The view's area is two rows shorter than the terminal (the tape and
        // the status line), so the floor of ten lands at a terminal of twelve.
        let mut client = desk();
        client.frame(120, 12);
        press(&mut client, KeyCode::Char('S'));
        let at_floor = client.frame(120, 12);
        assert!(at_floor.contains("START A WORKFLOW"), "{at_floor}");
        assert!(at_floor.contains("regime_review"), "{at_floor}");
        assert!(at_floor.contains("goal >"), "{at_floor}");
        assert!(
            at_floor.contains("the owner runs its own phase graph"),
            "{at_floor}"
        );
        assert!(typing(&client), "the box at its floor holds the keyboard");
    }

    #[test]
    fn the_picker_draws_at_every_width_the_view_itself_admits() {
        // An arithmetic underflow in a render path is a panic behind the
        // alternate screen, which is the one failure a fullscreen client cannot
        // report. The box is drawn over the view's own area, so its floor is
        // the view's — swept from the refusal threshold upward.
        let mut client = desk();
        press(&mut client, KeyCode::Char('S'));
        for width in 44u16..130 {
            for height in [8u16, 16, 36] {
                let _ = client.frame(width, height);
            }
        }
    }

    #[test]
    fn a_picker_with_no_templates_says_so_rather_than_starting_nothing() {
        // The templates poll runs on a sixty-second beat, so an empty list is a
        // real state a client opens in — and Enter there must not send a
        // template id this client invented.
        let mut store = super::store_from(super::running());
        store.posture = Posture::Operator;
        let mut client = Client::new(store);
        client.press(KeyCode::Char('6'));
        // The frame that publishes the pane's height, as `armed` does it.
        client.frame(120, 36);
        press(&mut client, KeyCode::Char('S'));
        typed(&mut client, "anything");
        assert_eq!(press(&mut client, KeyCode::Enter), None);
        assert!(client
            .frame(120, 36)
            .contains("the owner has served no templates yet"));
    }
}
