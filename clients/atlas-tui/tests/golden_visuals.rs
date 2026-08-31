//! VISUALS: the registry, the drawing, and the four answers the pane can give.
//!
//! The claims: a pane that has not heard from the owner says it is *asking* —
//! not that the desk registers nothing, which is a different fact with a
//! different fix; a served registry is offered in the owner's own order; Enter
//! asks for the entry under the cursor and says which one it is waiting on; a
//! served drawing is rendered line for line, unwrapped, so a gate stays over
//! its wire; and a refusal is drawn as the owner's own sentence rather than as
//! a dead owner.
//!
//! The wire shape is exercised too — every fixture arrives through serde and
//! through `Store::apply`, so a payload field the model stopped decoding fails
//! here rather than rendering as absent.

mod harness;

use atlas::bus::AppEvent;
use atlas::model::{Visual, VisualAnswer, VisualEntry, VisualResult};
use crossterm::event::KeyCode;
use harness::{content, line_with, Client};
use std::time::Instant;

/// The fixture desk, switched to VISUALS. `0` and not a tenth digit: the rail
/// has nine numbers and one zero, which is pinned in `store::ViewId`.
fn visuals() -> Client {
    let mut client = Client::fixture();
    client.press(KeyCode::Char('0'));
    client
}

/// The registry the owner serves, handed over the way the poller hands it.
const LIST: &str = r#"[
    {"name": "quantum_circuit", "title": "angle/ZZ feature map"},
    {"name": "regime_ribbon", "title": "regime over the window"}
]"#;

fn with_list() -> Client {
    let mut client = visuals();
    let list = serde_json::from_str::<Vec<VisualEntry>>(LIST).unwrap();
    client.store.apply(AppEvent::Visuals(list), Instant::now());
    client
}

/// The circuit as `qlab/visuals/quantum_circuit.py` draws it with no params —
/// the symbolic rendering this half asks for, verbatim from the K3a report.
const CIRCUIT: &str = "angle encoding: one wire per feature, θi = (π/2) · unit-scaled feature\n\
                       \n\
                       x0 |0> --[ RY(θ0) ]------\n\
                       x1 |0> --[ RY(θ1) ]------\n\
                       x2 |0> --[ RY(θ2) ]------\n\
                       \n\
                       kernel=angle  features=3  (no angles recorded; gates shown symbolically)";

/// A drawn answer, folded exactly as the poller folds one.
fn with_drawing(client: &mut Client, text: &str) {
    let visual = serde_json::from_value::<Visual>(serde_json::json!({
        "name": "quantum_circuit",
        "title": "angle/ZZ feature map",
        "text": text,
        "params": {"kernel": "angle", "features": ["x0", "x1", "x2"]},
    }))
    .unwrap();
    client.store.apply(
        AppEvent::Visual(Box::new(VisualAnswer {
            asked: "quantum_circuit".into(),
            result: VisualResult::Drawn(Box::new(visual)),
        })),
        Instant::now(),
    );
}

#[test]
fn the_pane_says_it_is_asking_rather_than_claiming_an_empty_registry() {
    // "Not asked yet" and "this owner registers no visuals" are different
    // facts with different remedies, and only the owner may state the second.
    let frame = visuals().frame(120, 36);
    assert!(
        content(&frame).contains("asking the owner what it can draw"),
        "{frame}"
    );
    assert!(
        !content(&frame).contains("registers no visuals"),
        "a client that had not asked claimed the owner's answer:\n{frame}"
    );
}

#[test]
fn an_owner_that_registers_nothing_says_so_in_its_own_right() {
    let mut client = visuals();
    client
        .store
        .apply(AppEvent::Visuals(Vec::new()), Instant::now());
    let frame = client.frame(120, 36);
    assert!(
        content(&frame).contains("this owner registers no visuals"),
        "{frame}"
    );
    assert!(
        !content(&frame).contains("asking the owner what it can draw"),
        "an answered registry still read as unasked:\n{frame}"
    );
}

#[test]
fn the_registry_renders_in_the_owners_own_order_with_its_own_titles() {
    let frame = with_list().frame(120, 36);
    let body = content(&frame);
    assert!(body.contains("quantum_circuit"), "{frame}");
    assert!(body.contains("angle/ZZ feature map"), "{frame}");
    assert!(body.contains("regime_ribbon"), "{frame}");
    // The owner sorts by name and this client does not re-sort: a second
    // ordering rule here would be an opinion about a registry the owner owns.
    let first = body.find("quantum_circuit").unwrap();
    let second = body.find("regime_ribbon").unwrap();
    assert!(first < second, "the registry was re-ordered:\n{frame}");
}

#[test]
fn enter_asks_the_owner_for_the_entry_under_the_cursor() {
    // Down first, so this pins the *cursor* rather than the first element of
    // a list. Routed through `shell::on_key`, which is where the bug this
    // harness exists for lived.
    let mut client = with_list();
    client.press(KeyCode::Down);
    client.press(KeyCode::Enter);
    assert_eq!(client.store.visual_asking(), Some("regime_ribbon"));
    // And the pane says which one it is waiting on, rather than going quiet.
    let frame = client.frame(120, 36);
    // Uppercased by the panel header, like every other title on the desk.
    assert!(
        content(&frame).contains("RENDERING REGIME_RIBBON"),
        "{frame}"
    );
    assert!(
        content(&frame).contains("asking the owner to draw it"),
        "{frame}"
    );
}

#[test]
fn the_path_one_render_is_asked_on_is_the_owners_own_route() {
    // The other half of the key above: what the runtime turns that request
    // into. Built by the poller and never by a caller — see `net::http`.
    assert_eq!(
        atlas::net::http::visual_url("http://127.0.0.1:8765", "quantum_circuit"),
        "http://127.0.0.1:8765/api/visuals/quantum_circuit"
    );
    // A trailing slash on the base is not a second path segment.
    assert_eq!(
        atlas::net::http::visual_url("http://127.0.0.1:8765/", "quantum_circuit"),
        "http://127.0.0.1:8765/api/visuals/quantum_circuit"
    );
    // And a name that is not a bare module name is escaped into the segment
    // rather than reaching the owner as a route of its own.
    assert_eq!(
        atlas::net::http::visual_url("http://x", "a/b"),
        "http://x/api/visuals/a%2Fb"
    );
}

#[test]
fn a_drawing_renders_line_for_line_with_its_gates_still_over_their_wires() {
    let mut client = with_list();
    with_drawing(&mut client, CIRCUIT);
    let frame = client.frame(120, 36);
    let body = content(&frame);
    // The owner's own title heads the pane.
    assert!(
        body.to_uppercase().contains("ANGLE/ZZ FEATURE MAP"),
        "{frame}"
    );
    // Three wires, each with its symbolic gate — the render with no params.
    for wire in ["x0 |0>", "x1 |0>", "x2 |0>"] {
        assert!(body.contains(wire), "{wire} is missing:\n{frame}");
    }
    assert_eq!(body.matches("RY(").count(), 3, "{frame}");
    // The alignment claim, which is the whole reason nothing here is wrapped:
    // the gate box opens at the same column on every wire.
    let columns: Vec<usize> = ["x0 |0>", "x1 |0>", "x2 |0>"]
        .iter()
        .map(|wire| {
            let row = line_with(&frame, wire);
            row.find("[ RY").expect("a wire with no gate box")
        })
        .collect();
    assert!(
        columns.windows(2).all(|pair| pair[0] == pair[1]),
        "the gate columns drifted apart: {columns:?}\n{frame}"
    );
    // The caption is wider than this pane, and the pane says so rather than
    // letting the clip read as the whole line.
    assert!(body.contains("cols"), "{frame}");
    // And the honesty the module docstring insists on is *reachable*: the
    // caption that says no circuit was executed is off the right-hand edge,
    // and `l` walks to it rather than the pane folding a wire to fit.
    for _ in 0..40 {
        client.press(KeyCode::Char('l'));
        client.frame(120, 36);
    }
    let scrolled = content(&client.frame(120, 36));
    assert!(
        scrolled.contains("gates shown symbolically"),
        "the caption is unreachable:\n{scrolled}"
    );
    // And the wires travelled with it — a horizontal scroll that moved only
    // the prose would have re-wrapped the art after all.
    assert!(!scrolled.contains("x0 |0>"), "{scrolled}");
}

#[test]
fn a_refused_render_draws_the_owners_sentence_and_never_a_dead_owner() {
    // Both refusals the route makes, because they have different fixes: a 404
    // names the visuals that exist, a 400 names the parameter it would not
    // take. Neither is the owner having gone away.
    // The sentence is checked in fragments because it wraps: it is a *remedy*
    // and must fold to fit, unlike the drawing beside it.
    for (status, said, fragments) in [
        (
            404u16,
            "no visual named circut; this owner draws quantum_circuit",
            ["no visual named circut", "quantum_circuit"],
        ),
        (
            400u16,
            "angles must be one per feature: 2 angles, 3 features",
            ["angles must be one per feature", "3 features"],
        ),
    ] {
        let mut client = with_list();
        client.store.apply(
            AppEvent::Visual(Box::new(VisualAnswer {
                asked: "circut".into(),
                result: VisualResult::Refused {
                    status,
                    said: said.into(),
                },
            })),
            Instant::now(),
        );
        let frame = client.frame(120, 36);
        let body = content(&frame);
        for fragment in fragments {
            assert!(
                body.contains(fragment),
                "{status} lost {fragment:?}:\n{frame}"
            );
        }
        assert!(body.contains(&format!("REFUSED {status}")), "{frame}");
        // The window is not waiting any more: an answer retires the question
        // whichever shape it came in.
        assert_eq!(client.store.visual_asking(), None);
        assert!(
            !body.contains("asking the owner to draw it"),
            "a refusal still read as in flight:\n{frame}"
        );
    }
}

#[test]
fn a_drawing_taller_than_the_pane_scrolls_and_walls_at_both_ends() {
    let tall: String = (0..80).map(|i| format!("row {i}\n")).collect();
    let mut client = with_list();
    with_drawing(&mut client, &tall);
    // The draw is what tells the pane how many rows it has, so the frame comes
    // before the scroll exactly as the runtime's loop does it.
    assert!(content(&client.frame(120, 36)).contains("row 0"));

    client.press(KeyCode::PageDown);
    let paged = content(&client.frame(120, 36));
    assert!(!paged.contains("row 0"), "PgDn did not move:\n{paged}");
    assert!(paged.contains("row 10"), "{paged}");

    // Walled at the bottom: pressing past the end must not leave the operator
    // looking at empty cells where a drawing was asked for.
    for _ in 0..40 {
        client.press(KeyCode::PageDown);
        client.frame(120, 36);
    }
    let bottom = content(&client.frame(120, 36));
    assert!(bottom.contains("row 79"), "the last row is gone:\n{bottom}");

    for _ in 0..40 {
        client.press(KeyCode::PageUp);
        client.frame(120, 36);
    }
    assert!(
        content(&client.frame(120, 36)).contains("row 0"),
        "PgUp did not reach the top"
    );
}

#[test]
fn the_keys_this_pane_binds_are_offered_to_a_glass_window_too() {
    // Every key here is a read, so the posture filter must remove none of
    // them: a monitoring window that could not scroll a drawing would be a
    // window that cannot read what it is for.
    let offered: Vec<&str> = atlas::input::bindings(atlas::store::Posture::Glass)
        .filter(|b| b.source == atlas::input::Source::View(atlas::store::ViewId::Visuals))
        .map(|b| b.key)
        .collect();
    for key in ["↑", "↓", "Enter", "PgUp", "PgDn", "j", "k"] {
        assert!(offered.contains(&key), "{key} is not offered: {offered:?}");
    }
}

#[test]
fn the_visuals_view_renders_the_list_at_120x36() {
    insta::assert_snapshot!(with_list().frame(120, 36));
}

#[test]
fn the_visuals_view_renders_the_circuit_at_120x36() {
    let mut client = with_list();
    with_drawing(&mut client, CIRCUIT);
    insta::assert_snapshot!(client.frame(120, 36));
}

/// The monitoring artifact's own pin.
///
/// Its own module rather than a second assertion, because the claim is about
/// the *binary*: the `--no-default-features` build has no `net::write`, no
/// confirm modal and no `Posture::Operator`, and this pane is drawn by every
/// line of the same source in it. A golden here that matched the armed one
/// would be saying so by construction — which is exactly what is worth pinning
/// for a pane whose whole vocabulary is two GETs.
#[cfg(not(feature = "operator"))]
mod glass {
    use super::*;

    #[test]
    fn the_circuit_renders_identically_in_a_monitoring_build_at_120x36() {
        let mut client = with_list();
        with_drawing(&mut client, CIRCUIT);
        insta::assert_snapshot!(client.frame(120, 36));
    }

    #[test]
    fn a_monitoring_window_can_ask_for_a_drawing_because_asking_writes_nothing() {
        // The pane's whole vocabulary is two GETs, so the posture takes none of
        // it away. A glass window that could not render a visual would be a
        // window that cannot read what it exists to read.
        let mut client = with_list();
        client.press(KeyCode::Enter);
        assert_eq!(client.store.visual_asking(), Some("quantum_circuit"));
    }
}
