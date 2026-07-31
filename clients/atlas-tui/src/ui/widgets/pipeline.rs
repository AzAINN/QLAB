//! The phase pipeline: `●──●──◉──○──○`, one workflow's graph as a line of cells.
//!
//! A governed run is a chain of phases that each have to complete before the
//! next can start, and the one question an operator asks about it is "where has
//! it got to". A status word cannot answer that — `running` is true of a
//! workflow on its first phase and of one on its last — so the shape is the
//! answer: solid behind, hollow ahead, and the amber node is where the desk is.
//!
//! **The dot is the only claim of motion this client makes.** It rides the link
//! beside the active phase and steps once per `store.tick`, which is what makes
//! a live coordinator distinguishable from a parked one at a glance. Time is
//! data here as everywhere: the beat is a counter the store advances, never a
//! clock this module reads, so a golden frame pins the dot at a stated position
//! rather than wherever the test happened to be scheduled.
//!
//! Note that a moving dot says *this phase is open*, not *someone is working on
//! it*. Registering a workflow is not running it — only the coordinator's own
//! `driving` flag says that — and the view draws the two facts side by side
//! rather than folding one into the other.

use crate::format::text;
use crate::model::WorkflowStep;
use crate::theme::theme;
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};

/// Cells between two nodes.
///
/// Three rather than two so the dot has somewhere to travel: at two the
/// animation is a flicker between two positions, which reads as a rendering
/// fault rather than as progress. At three the cycle is `LINK * store::TICK` —
/// 360 ms — which is a pace the eye follows without the pane feeling busy.
pub const LINK: usize = 3;

/// Where one phase has got to.
///
/// Six, not the registry's seven statuses: `failed` and `blocked` join
/// `abandoned` under [`Phase::Stopped`] because a pipeline is read for *shape*,
/// and all three mean the same thing about the shape — this phase is finished
/// and did not produce what the next one needs. Which of the three it was is a
/// word, and the console beside the pipeline carries words.
///
/// [`Phase::Unknown`] is not a catch-all for convenience. A status this client
/// has never heard of is a contract change with the owner, and the two
/// alternatives are both lies: rendering it hollow says the phase has not
/// started, and rendering it solid says it finished. Invariant 4 — say so.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Done,
    Active,
    Pending,
    /// Recoverable: `resume_workflow` puts an interrupted step back to work.
    Interrupted,
    /// Terminal: `abandoned`, `failed`, or `blocked`.
    Stopped,
    Unknown,
}

/// The owner's step status, as a shape.
///
/// The vocabulary is `qlab/state/registry.py`'s: a step is inserted `queued`,
/// worked as `working`, and lands on one of `done` / `failed` / `blocked`;
/// `interrupt_workflow` and `abandon_workflow` write the other two.
pub fn phase_of(status: Option<&str>) -> Phase {
    match status {
        Some("done") => Phase::Done,
        Some("working") => Phase::Active,
        Some("queued") => Phase::Pending,
        Some("interrupted") => Phase::Interrupted,
        Some("abandoned") | Some("failed") | Some("blocked") => Phase::Stopped,
        _ => Phase::Unknown,
    }
}

/// One node, as a cell.
///
/// Every glyph is one column wide, so a pipeline's width is arithmetic
/// ([`width`]) rather than a measurement — which is what lets the view refuse
/// before it draws instead of letting a `Paragraph` clip the tail off the graph.
pub fn glyph(phase: Phase) -> &'static str {
    match phase {
        Phase::Done => "●",
        Phase::Active => "◉",
        Phase::Pending => "○",
        Phase::Interrupted => "◌",
        Phase::Stopped => "✕",
        Phase::Unknown => "?",
    }
}

/// What a node's state means, in colour.
///
/// The pair that has to stay distinguishable is `Interrupted`/`Stopped`: one is
/// a run an operator can resume and the other is one they cannot, and on a
/// 256-colour terminal the glyphs are what carry it if the shades ever blur.
pub fn tone(phase: Phase) -> Color {
    let t = theme();
    match phase {
        Phase::Done => t.positive,
        Phase::Active => t.accent,
        Phase::Pending => t.border_med,
        Phase::Interrupted => t.warning,
        Phase::Stopped => t.negative,
        Phase::Unknown => t.text_dim,
    }
}

/// How many cells a pipeline of `nodes` phases occupies.
pub const fn width(nodes: usize) -> usize {
    match nodes {
        0 => 0,
        n => n + (n - 1) * LINK,
    }
}

/// The active phase's position in the graph, if one is open.
///
/// First match wins. Concurrent phases mean a panel workflow can have two open
/// at once, and the dot has one place to be — the earliest, which is where the
/// chain is actually waiting.
pub fn active_index(steps: &[WorkflowStep]) -> Option<usize> {
    steps
        .iter()
        .position(|step| phase_of(text(step.status.as_ref())) == Phase::Active)
}

/// One workflow's graph, drawn at beat `tick`.
///
/// The steps are taken in the order the owner served them, which is `seq` order
/// — `read_workflow` orders by it — because a pipeline reordered by the client
/// would be a different graph from the one the coordinator walks.
pub fn line(steps: &[WorkflowStep], tick: u64) -> Line<'static> {
    // The link that carries the dot: the one *into* the active phase, or the one
    // out of it when the active phase is first. Either way it is the link
    // between the work that is done and the work that is open, which is where
    // motion belongs.
    let carrier = active_index(steps).map(|at| at.saturating_sub(1));
    let mut spans: Vec<Span<'static>> = Vec::with_capacity(steps.len() * 2);
    for (i, step) in steps.iter().enumerate() {
        if i > 0 {
            spans.extend(link(carrier == Some(i - 1), tick));
        }
        let phase = phase_of(text(step.status.as_ref()));
        spans.push(Span::styled(
            glyph(phase),
            Style::default()
                .fg(tone(phase))
                .add_modifier(if phase == Phase::Active {
                    Modifier::BOLD
                } else {
                    Modifier::empty()
                }),
        ));
    }
    Line::from(spans)
}

/// One link between two nodes, with the dot on it when this is the live one.
fn link(carrying: bool, tick: u64) -> Vec<Span<'static>> {
    let t = theme();
    let rail = Style::default().fg(t.border_med);
    if !carrying {
        return vec![Span::styled("─".repeat(LINK), rail)];
    }
    let at = dot_at(tick);
    let mut out = Vec::with_capacity(3);
    if at > 0 {
        out.push(Span::styled("─".repeat(at), rail));
    }
    out.push(Span::styled(
        "•",
        Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
    ));
    if at + 1 < LINK {
        out.push(Span::styled("─".repeat(LINK - at - 1), rail));
    }
    out
}

/// Which cell of the live link the dot is on at beat `tick`.
///
/// A pure function of the beat, so the position is a value a test names rather
/// than a frame it has to catch. `store.tick` wraps rather than saturating, so
/// this cannot stall at the top of a `u64`.
fn dot_at(tick: u64) -> usize {
    (tick % LINK as u64) as usize
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The pipeline as an operator sees it — spans concatenated, styles dropped.
    fn drawn(steps: &[WorkflowStep], tick: u64) -> String {
        line(steps, tick)
            .spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect()
    }

    fn steps(statuses: &[&str]) -> Vec<WorkflowStep> {
        statuses
            .iter()
            .enumerate()
            .map(|(i, status)| WorkflowStep {
                step_id: Some(format!("wf1:p{i}")),
                phase: Some(format!("p{i}")),
                status: Some((*status).to_string()),
                ..WorkflowStep::default()
            })
            .collect()
    }

    #[test]
    fn every_status_the_registry_writes_has_a_shape_and_an_unknown_one_says_so() {
        // The seven the registry can write, mapped onto six shapes. `failed` and
        // `blocked` share `Stopped` with `abandoned` because a pipeline is read
        // for shape and all three mean "finished, and the next phase is not
        // getting what it needs".
        assert_eq!(phase_of(Some("done")), Phase::Done);
        assert_eq!(phase_of(Some("working")), Phase::Active);
        assert_eq!(phase_of(Some("queued")), Phase::Pending);
        assert_eq!(phase_of(Some("interrupted")), Phase::Interrupted);
        for terminal in ["abandoned", "failed", "blocked"] {
            assert_eq!(phase_of(Some(terminal)), Phase::Stopped, "{terminal}");
        }
        // Never guessed in either direction: hollow would say it has not
        // started and solid would say it finished.
        assert_eq!(phase_of(None), Phase::Unknown);
        assert_eq!(phase_of(Some("")), Phase::Unknown);
        assert_eq!(phase_of(Some("reticulating")), Phase::Unknown);
    }

    #[test]
    fn each_shape_is_its_own_glyph_and_its_own_tone() {
        // Six states that render as four would be a pipeline with two states an
        // operator cannot tell apart — and the pair that must never blur is
        // `Interrupted`/`Stopped`: one is resumable and the other is not.
        let all = [
            Phase::Done,
            Phase::Active,
            Phase::Pending,
            Phase::Interrupted,
            Phase::Stopped,
            Phase::Unknown,
        ];
        for (i, a) in all.iter().enumerate() {
            for b in &all[i + 1..] {
                assert_ne!(glyph(*a), glyph(*b), "{a:?} and {b:?} draw the same cell");
                assert_ne!(tone(*a), tone(*b), "{a:?} and {b:?} are the same colour");
            }
            // One column each, or `width` is arithmetic about the wrong thing.
            assert_eq!(
                glyph(*a).chars().count(),
                1,
                "{a:?} does not fit one cell: {}",
                glyph(*a)
            );
        }
    }

    #[test]
    fn a_pipeline_is_solid_behind_hollow_ahead_and_amber_where_the_desk_is() {
        let flow = steps(&["done", "done", "working", "queued", "queued"]);
        assert_eq!(drawn(&flow, 0), "●───●•──◉───○───○");
        // The whole point of the shape: two runs that both read `running` are
        // not the same picture.
        assert_ne!(
            drawn(&steps(&["working", "queued", "queued"]), 0),
            drawn(&steps(&["done", "done", "working"]), 0)
        );
    }

    #[test]
    fn the_dot_travels_the_link_into_the_active_phase_and_cycles_on_the_beat() {
        let flow = steps(&["done", "working", "queued"]);
        assert_eq!(drawn(&flow, 0), "●•──◉───○");
        assert_eq!(drawn(&flow, 1), "●─•─◉───○");
        assert_eq!(drawn(&flow, 2), "●──•◉───○");
        assert_eq!(drawn(&flow, 3), "●•──◉───○", "the beat cycles");
        // The store's beat wraps rather than saturating, and so must this.
        assert_eq!(drawn(&flow, u64::MAX), drawn(&flow, u64::MAX % LINK as u64));
    }

    #[test]
    fn an_active_first_phase_puts_the_dot_on_the_link_out_of_it() {
        // There is no link before node zero. The dot moves onto the one after
        // it — still the boundary between what is open and what is waiting —
        // rather than vanishing, which would render a running first phase as
        // motionless.
        let flow = steps(&["working", "queued", "queued"]);
        assert_eq!(drawn(&flow, 0), "◉•──○───○");
        assert_eq!(drawn(&flow, 2), "◉──•○───○");
    }

    #[test]
    fn a_pipeline_with_nothing_open_carries_no_dot_at_any_beat() {
        // A finished or abandoned run must not animate: a moving dot is this
        // client's one claim that something is still happening.
        for finished in [
            steps(&["done", "done", "done"]),
            steps(&["done", "abandoned", "queued"]),
            steps(&["done", "interrupted", "queued"]),
        ] {
            for tick in 0..6 {
                assert!(
                    !drawn(&finished, tick).contains('•'),
                    "a closed pipeline animated at beat {tick}: {}",
                    drawn(&finished, tick)
                );
            }
        }
    }

    #[test]
    fn one_phase_is_one_node_and_no_link() {
        let single = steps(&["working"]);
        assert_eq!(drawn(&single, 0), "◉");
        assert_eq!(drawn(&[], 0), "");
    }

    #[test]
    fn the_width_is_what_the_pipeline_actually_draws() {
        // The view refuses on this number before it draws, so a disagreement
        // here is a graph clipped by a `Paragraph` — which reads as a workflow
        // with fewer phases than it has.
        assert_eq!(width(0), 0);
        assert_eq!(width(1), 1);
        assert_eq!(width(5), 17);
        for n in 0..8 {
            assert_eq!(
                drawn(&steps(&vec!["queued"; n]), 0).chars().count(),
                width(n),
                "{n} phases"
            );
        }
    }

    #[test]
    fn the_earliest_open_phase_is_the_one_the_chain_is_waiting_on() {
        // Panel workflows run branches concurrently, so two steps can be
        // `working` at once. The dot has one place to be.
        assert_eq!(
            active_index(&steps(&["done", "working", "working"])),
            Some(1)
        );
        assert_eq!(active_index(&steps(&["done", "done"])), None);
    }
}
