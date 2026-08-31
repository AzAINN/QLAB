//! VISUALS — what a build produced, drawn.
//!
//! The owner registers text visuals in `qlab/visuals/`, each a module that
//! turns a plain dict of parameters into ASCII. The first is the angle/ZZ
//! feature map behind the kernel lane the PREDICTORS board evaluates: one wire
//! per feature, an `RY` gate on each, and a named ZZ entangler row. Nothing is
//! executed to draw it — the kernels are closed form, the drawing is a picture
//! of the map, and no claim about a speedup is made or implied anywhere on
//! this pane.
//!
//! Two panes. The list is the owner's registry in the owner's own order, and
//! the render pane is whichever entry the operator pressed Enter on. Both are
//! reads: `GET /api/visuals` and `GET /api/visuals/<name>`, and there is no
//! key here that changes anything on the desk. The pane therefore draws
//! identically in both postures, and the glass build compiles every line of it.
//!
//! The drawing is rendered **verbatim, line for line**. The owner's drawer pads
//! its gate columns so `[` and `]` align across wires of uneven name length, so
//! a pane that wrapped or trimmed would draw a circuit whose gates no longer
//! sit over the qubits they act on. What does not fit scrolls; nothing is
//! reflowed.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::{VisualEntry, VisualResult};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::table_cell::head;
use crate::ui::widgets::{panel_block, panel_header, refuse};
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
    Frame,
};
use std::cell::Cell;
use std::time::Instant;

/// The list column: the widest name the owner's registry can serve is a Python
/// module name, and `quantum_circuit` is fifteen. Two more for the cursor.
const LIST_W: u16 = 24;

/// The floor. The list plus a render column that can hold the circuit's own
/// widest line — a twelve-character feature name, `|0>`, the gate box and the
/// entangler marker — without clipping a gate off the right-hand end.
const MIN_W: u16 = LIST_W + 2 + 40;

/// One `PgUp`/`PgDn`, in rendered lines. Deliberately not the pane height: a
/// page that moved exactly one screen leaves nothing in common between the two
/// frames, and a circuit read in strips is easiest to follow with an overlap.
const PAGE: i64 = 10;

/// The operator's cursor and their scroll — where they are looking, never what
/// the desk said. The list, the drawing and the refusal all live in the store.
#[derive(Default)]
pub struct VisualsView {
    /// Which entry the list cursor sits on, as an index into the owner's own
    /// order. Clamped at draw against the list actually served, so a registry
    /// that shrank under a parked cursor cannot leave it past the end.
    cursor: usize,
    /// How far down the drawing the operator has scrolled, in rendered lines.
    offset: usize,
    /// How far right the operator has scrolled, in columns.
    ///
    /// A drawing may not be re-wrapped — the drawer aligns its gate columns —
    /// so a line wider than the pane has to be *reachable* rather than folded.
    /// Clipping it silently would drop the circuit's own caption, which is the
    /// line that says no circuit was executed.
    hoffset: usize,
    /// The furthest that scroll may go, published by `draw`.
    ///
    /// Published rather than computed in the key handler for the reason ATLAS
    /// publishes its own: only the frame knows how many rows the pane got, and
    /// clamping against a guess would let the scroll run off the end of a
    /// drawing and leave the operator staring at empty cells.
    max_scroll: Cell<usize>,
    /// The furthest the horizontal scroll may go, published by `draw` for
    /// `max_scroll`'s reason: only the frame knows both the pane's width and
    /// the drawing's widest line.
    max_hscroll: Cell<usize>,
}

impl View for VisualsView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        if area.width < MIN_W || area.height < 8 {
            refuse(
                f,
                area,
                format!(
                    "VISUALS needs {MIN_W} columns to draw a circuit without clipping \
                     a gate off its wire; this pane has {}.",
                    area.width
                ),
            );
            return;
        }

        let block = panel_block();
        let inner = block.inner(area);
        f.render_widget(block, area);

        // With no registry there is nothing to render either, so the pane is
        // one statement across its whole width rather than a sentence squeezed
        // into a 24-column list beside an empty drawing. The two sentences are
        // long because they have to name their own remedy, and a remedy
        // clipped at the list's edge is one an operator cannot act on.
        let absent = match store.visuals() {
            // This client's own fact, and its remedy is this client's own key.
            None => Some("asking the owner what it can draw — r asks again if this persists"),
            // And this one is the *owner's* fact: it answered, and it
            // registers nothing. Two sentences because they have two different
            // fixes, and one over both would send an operator to restart an
            // owner that has already told them the truth.
            Some([]) => Some("this owner registers no visuals"),
            Some(_) => None,
        };
        if let Some(said) = absent {
            f.render_widget(
                Paragraph::new(vec![panel_header("visuals"), Line::from(""), dim(said)])
                    .wrap(Wrap { trim: true }),
                inner,
            );
            return;
        }

        let cols = Layout::horizontal([
            Constraint::Length(LIST_W),
            Constraint::Length(1),
            Constraint::Min(0),
        ])
        .split(inner);
        self.draw_list(f, cols[0], store);
        self.draw_render(f, cols[2], store);
    }

    /// Every key here is a read. Live in both postures and compiled into both
    /// builds, because looking at a drawing the owner already made changes
    /// nothing a glass window is being kept away from.
    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        use crossterm::event::KeyCode;
        match k.code {
            KeyCode::Up => {
                self.cursor = self.cursor.saturating_sub(1);
                None
            }
            KeyCode::Down => {
                // Clamped against the list the owner served rather than left
                // to run: an unbounded cursor on an empty registry would need
                // the draw to guess what it meant.
                let last = store.visuals().map(|v| v.len()).unwrap_or(0);
                self.cursor = (self.cursor + 1).min(last.saturating_sub(1));
                None
            }
            KeyCode::Enter => self.render_here(store),
            // The scroll keys. `j`/`k` beside the page keys because a drawing
            // taller than the pane is read a line at a time, and the arrows
            // belong to the list — one pane's cursor and another's scroll on
            // the same two keys is how an operator loses both.
            KeyCode::PageUp => {
                self.scroll(-PAGE);
                None
            }
            KeyCode::PageDown => {
                self.scroll(PAGE);
                None
            }
            KeyCode::Char('k') => {
                self.scroll(-1);
                None
            }
            KeyCode::Char('j') => {
                self.scroll(1);
                None
            }
            // The other axis. A drawing is art rather than prose: it may not
            // be re-wrapped, so a wire wider than the pane is reached rather
            // than folded — and the caption that says no circuit was executed
            // is the widest line the first visual draws.
            KeyCode::Char('h') => {
                self.hscroll(-1);
                None
            }
            KeyCode::Char('l') => {
                self.hscroll(1);
                None
            }
            // The two edges, in one press each. A caption twenty columns off
            // the right of the pane is twenty presses of `l` away, and a
            // reader who wanted the end of a line should not have to count.
            KeyCode::Home => {
                self.hoffset = 0;
                None
            }
            KeyCode::End => {
                self.hoffset = self.max_hscroll.get();
                None
            }
            _ => None,
        }
    }

    /// Nothing to reset. The cursor and the scroll are where the operator left
    /// them, which is worth keeping across a trip to another pane — see the
    /// registry's own note on `entered`. The list fetch is edge-triggered by
    /// the runtime on arriving here, exactly as PREDICTORS' board is.
    fn entered(&self) {}
}

impl VisualsView {
    /// Ask the owner for the entry under the cursor.
    ///
    /// The two halves are one act: the store records *which* drawing is being
    /// waited on, and the command asks for it. Recording it here rather than
    /// beside the dispatch in `main.rs` is deliberate — that module is in no
    /// test binary, and a pane state only the binary could set is one nothing
    /// can pin.
    fn render_here(&mut self, store: &mut Store) -> Option<Command> {
        let name = store
            .visuals()?
            .get(self.cursor)
            .and_then(|entry| format::text(entry.name.as_ref()))?
            .to_string();
        self.offset = 0;
        self.hoffset = 0;
        store.ask_visual(&name);
        Some(Command::RenderVisual(name))
    }

    /// Move the scroll, walled at both ends against what the last frame drew.
    fn scroll(&mut self, delta: i64) {
        let next = self.offset as i64 + delta;
        self.offset = next.clamp(0, self.max_scroll.get() as i64) as usize;
    }

    /// The same, sideways.
    fn hscroll(&mut self, delta: i64) {
        let next = self.hoffset as i64 + delta;
        self.hoffset = next.clamp(0, self.max_hscroll.get() as i64) as usize;
    }

    /// The registry, two rows per visual, in the owner's own order.
    fn draw_list(&self, f: &mut Frame, area: Rect, store: &Store) {
        let mut lines = vec![panel_header("visuals"), Line::from("")];
        // Non-empty by construction: `draw` has already answered the two
        // absent cases across the whole pane, because neither leaves anything
        // for the render column beside this one to hold.
        if let Some(entries) = store.visuals().filter(|list| !list.is_empty()) {
            let cursor = self.cursor.min(entries.len() - 1);
            // Two rows per entry — the name the route takes, then the owner's
            // own title under it — so the count is halved before it is
            // compared against the pane.
            let room = ((area.height as usize).saturating_sub(3)) / 2;
            for (i, entry) in entries.iter().take(room).enumerate() {
                lines.extend(entry_lines(entry, i == cursor));
            }
            if entries.len() > room {
                lines.push(dim(&format!("… {} more", entries.len() - room)));
            }
        }
        // Unwrapped, deliberately: a name folded onto a second row would put
        // the cursor bar beside half a word, and `head` has already decided
        // what fits.
        f.render_widget(Paragraph::new(lines), area);
    }

    /// The drawing, or the one honest sentence about why there is none.
    fn draw_render(&self, f: &mut Frame, area: Rect, store: &Store) {
        let t = theme();
        // Whatever the pane says, it is one thing: a stale circuit under a
        // fresh refusal would make the refusal invisible, and a drawing under
        // "asking the owner" would answer a question nobody has heard back on.
        //
        // `wrap` rides with them because the two kinds of body have opposite
        // needs: a sentence too long for the pane must fold or its remedy is
        // lost, and a drawing that folded would put a gate on the wrong wire.
        let (title, body, wrap) = match (store.visual_asking(), store.visual()) {
            (Some(name), _) => (
                format!("rendering {name}"),
                vec![dim("asking the owner to draw it…")],
                true,
            ),
            (None, None) => (
                "no drawing yet".to_string(),
                vec![dim(
                    "pick a visual on the left and press Enter to render it",
                )],
                true,
            ),
            (None, Some(answer)) => match &answer.result {
                VisualResult::Drawn(visual) => (
                    format::text(visual.title.as_ref())
                        .unwrap_or(&answer.asked)
                        .to_string(),
                    // Verbatim, line for line: the drawer aligns its gate
                    // columns, and a re-wrap here would move a gate off its
                    // wire.
                    format::text(visual.text.as_ref())
                        .unwrap_or("the owner served a visual with no drawing in it")
                        .lines()
                        .map(|line| {
                            Line::from(Span::styled(
                                line.to_string(),
                                Style::default().fg(t.text_primary),
                            ))
                        })
                        .collect(),
                    false,
                ),
                // The owner's own sentence, verbatim, with the status beside
                // it: a 404 names the visuals that do exist and a 400 names
                // the parameter it would not take, and neither is a sentence
                // this client could write.
                VisualResult::Refused { status, said } => (
                    format!("{} · refused {status}", answer.asked),
                    vec![Line::from(Span::styled(
                        said.clone(),
                        Style::default().fg(t.warning),
                    ))],
                    true,
                ),
                // **Not "refused", in the header or the tone.** The owner
                // broke while drawing this, which is nothing the operator can
                // fix by asking for something else — and a word that implied
                // they could would send them to edit the one thing that was
                // not wrong. Negative rather than warning: a desk whose owner
                // is throwing 500s is not a desk to read numbers off.
                VisualResult::Failed { status, said } => (
                    format!("{} · owner failed {status}", answer.asked),
                    vec![Line::from(Span::styled(
                        said.clone(),
                        Style::default().fg(t.negative),
                    ))],
                    true,
                ),
                // Nothing came back, or nothing readable did. Still an answer
                // and still drawn: the version of this pane that logged these
                // and emitted nothing left "asking the owner…" on screen until
                // the client was restarted.
                VisualResult::Unanswered { said } => (
                    format!("{} · no answer", answer.asked),
                    vec![Line::from(Span::styled(
                        said.clone(),
                        Style::default().fg(t.negative),
                    ))],
                    true,
                ),
            },
        };

        let rows = Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(area);
        let room = rows[1].height as usize;
        self.max_scroll.set(body.len().saturating_sub(room));
        // Only an unwrapped body can run off the right-hand edge; a wrapped
        // sentence has already been folded to fit, and offering a sideways key
        // for it would be an affordance for a thing that cannot happen.
        let widest = match wrap {
            true => 0,
            false => body.iter().map(|line| line.width()).max().unwrap_or(0),
        };
        self.max_hscroll
            .set(widest.saturating_sub(rows[1].width as usize));

        // What is still off the right-hand edge *from here*, not what the
        // widest line always was: a count that never moved read as a control
        // that does nothing, and said "+20 cols" with the operator already at
        // the right edge looking at the end of the line.
        let hoffset = self.hoffset.min(self.max_hscroll.get());
        f.render_widget(
            Paragraph::new(vec![
                panel_header(&title),
                Line::from(Span::styled(
                    keys_note(self.max_hscroll.get() - hoffset),
                    Style::default().fg(t.text_dim),
                )),
            ]),
            rows[0],
        );

        let offset = self.offset.min(self.max_scroll.get());
        // Windowed here rather than by `Paragraph::scroll`, so the offset is
        // in the same units the wall above is measured in: the widget scrolls
        // by *wrapped* rows, and these lines are deliberately not wrapped.
        let shown: Vec<Line> = body.into_iter().skip(offset).take(room).collect();
        let paragraph = Paragraph::new(shown);
        f.render_widget(
            match wrap {
                true => paragraph.wrap(Wrap { trim: true }),
                // `scroll` on the horizontal axis only, where the widget's own
                // unit *is* a column and cannot disagree with the wall above.
                false => paragraph.scroll((0, hoffset as u16)),
            },
            rows[1],
        );
    }
}

/// One registry row: the name the route takes, and the title it carries.
///
/// A row with no name is drawn and never selected-into a request: there is
/// nothing to put in the path, and `render_here` declines it rather than
/// asking the owner to draw `""`.
fn entry_lines(entry: &VisualEntry, selected: bool) -> Vec<Line<'static>> {
    let t = theme();
    let name = format::text(entry.name.as_ref()).unwrap_or(MISSING);
    let style = match selected {
        true => Style::default()
            .fg(t.accent)
            .bg(t.bg_hover)
            .add_modifier(Modifier::BOLD),
        false => Style::default().fg(t.text_primary),
    };
    vec![
        Line::from(vec![
            // The marker is a glyph and not only a colour, for the nav rail's
            // reason: on a 256-colour terminal the highlight is a shade, and a
            // shade is not an answer to "which one will Enter render".
            Span::styled(
                match selected {
                    true => "▌ ",
                    false => "  ",
                },
                Style::default().fg(t.accent),
            ),
            Span::styled(head(name.to_string(), LIST_W - 2), style),
        ]),
        Line::from(Span::styled(
            format!(
                "  {}",
                head(
                    format::text(entry.title.as_ref()).unwrap_or("").to_string(),
                    LIST_W - 2
                )
            ),
            Style::default().fg(t.text_tertiary),
        )),
    ]
}

/// What the keys on this pane do, and how far off the right-hand edge the
/// drawing runs.
///
/// The width is stated rather than implied: a line clipped at the pane's edge
/// with nothing said about it reads as the whole line, and the first visual's
/// widest line is the caption that says no circuit was executed. Named only
/// while there is something for the keys to act on — a scroll hint over a pane
/// with no drawing in it is an affordance for a thing that is not there.
fn keys_note(hidden: usize) -> String {
    // Short enough to survive the render column at the 120-cell baseline: a
    // hint clipped mid-word is one an operator reads as broken, and the count
    // is the half that would be lost.
    match hidden {
        0 => "↑↓ pick · Enter render · PgUp/PgDn · j/k ⇅".to_string(),
        n => format!("↑↓ pick · Enter render · j/k ⇅ · h/l ⇄ +{n} cols"),
    }
}

fn dim(text: &str) -> Line<'static> {
    Line::from(Span::styled(
        text.to_string(),
        Style::default().fg(theme().text_dim),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bus::AppEvent;
    use crate::model::VisualAnswer;
    use crossterm::event::{KeyCode, KeyModifiers};

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    /// A store holding the registry the owner serves.
    fn with_list() -> Store {
        let mut store = Store::default();
        store.apply(
            AppEvent::Visuals(
                serde_json::from_str::<Vec<VisualEntry>>(
                    r#"[{"name": "quantum_circuit", "title": "angle/ZZ feature map"},
                        {"name": "regime_ribbon", "title": "regime over the window"}]"#,
                )
                .unwrap(),
            ),
            Instant::now(),
        );
        store
    }

    #[test]
    fn enter_asks_for_the_entry_under_the_cursor_and_says_it_is_waiting() {
        let mut store = with_list();
        let mut view = VisualsView::default();
        // The second row, which is what makes this a test of the cursor
        // rather than of the first element of a list.
        view.on_key(key(KeyCode::Down), &mut store);
        let asked = view.on_key(key(KeyCode::Enter), &mut store);
        assert_eq!(asked, Some(Command::RenderVisual("regime_ribbon".into())));
        // And the pane records what it is waiting on, in the same act.
        assert_eq!(store.visual_asking(), Some("regime_ribbon"));
    }

    #[test]
    fn the_cursor_walls_at_both_ends_of_the_registry() {
        let mut store = with_list();
        let mut view = VisualsView::default();
        for _ in 0..5 {
            view.on_key(key(KeyCode::Down), &mut store);
        }
        assert_eq!(view.cursor, 1, "the cursor ran past the last visual");
        for _ in 0..5 {
            view.on_key(key(KeyCode::Up), &mut store);
        }
        assert_eq!(view.cursor, 0, "the cursor ran past the first visual");
    }

    #[test]
    fn enter_on_a_registry_that_has_not_arrived_asks_for_nothing() {
        // A pane that sent an empty name would have the owner refuse a request
        // this client should never have made.
        let mut store = Store::default();
        let mut view = VisualsView::default();
        assert_eq!(view.on_key(key(KeyCode::Enter), &mut store), None);
        assert_eq!(store.visual_asking(), None);
    }

    #[test]
    fn the_scroll_walls_at_both_ends_of_the_drawing() {
        let mut view = VisualsView::default();
        let mut store = with_list();
        view.max_scroll.set(4);
        for _ in 0..3 {
            view.on_key(key(KeyCode::Char('j')), &mut store);
        }
        assert_eq!(view.offset, 3);
        view.on_key(key(KeyCode::PageDown), &mut store);
        assert_eq!(view.offset, 4, "the scroll ran past the last line");
        view.on_key(key(KeyCode::PageUp), &mut store);
        assert_eq!(view.offset, 0, "PgUp did not reach the top");
        view.on_key(key(KeyCode::Char('k')), &mut store);
        assert_eq!(view.offset, 0, "the scroll ran past the first line");
    }

    #[test]
    fn a_fresh_render_starts_at_the_top_of_the_new_drawing() {
        // Otherwise a short second circuit opens scrolled past its own end,
        // showing empty cells where a drawing was asked for.
        let mut store = with_list();
        let mut view = VisualsView::default();
        view.max_scroll.set(9);
        view.on_key(key(KeyCode::PageDown), &mut store);
        assert_eq!(view.offset, 9);
        view.on_key(key(KeyCode::Enter), &mut store);
        assert_eq!(view.offset, 0);
    }

    #[test]
    fn an_answer_retires_the_question_whichever_shape_it_came_in() {
        // All four, because all four are answers. The version of this pane
        // that emitted nothing for the last two left `asking` set and drew
        // "asking the owner…" until the client was restarted.
        let mut store = with_list();
        for result in [
            VisualResult::Drawn(Box::default()),
            VisualResult::Refused {
                status: 400,
                said: "angles must be one per feature".into(),
            },
            VisualResult::Failed {
                status: 500,
                said: "the owner failed at 500 while drawing this — Enter asks again".into(),
            },
            VisualResult::Unanswered {
                said: "the owner did not answer — Enter asks again".into(),
            },
        ] {
            store.ask_visual("quantum_circuit");
            assert_eq!(store.visual_asking(), Some("quantum_circuit"));
            store.apply(
                AppEvent::Visual(Box::new(VisualAnswer {
                    asked: "quantum_circuit".into(),
                    result,
                })),
                Instant::now(),
            );
            assert_eq!(store.visual_asking(), None);
        }
    }

    #[test]
    fn home_and_end_reach_both_edges_in_one_press_each() {
        let mut store = with_list();
        let mut view = VisualsView::default();
        view.max_hscroll.set(20);
        view.on_key(key(KeyCode::End), &mut store);
        assert_eq!(view.hoffset, 20, "End did not reach the right edge");
        view.on_key(key(KeyCode::Home), &mut store);
        assert_eq!(view.hoffset, 0, "Home did not reach the left edge");
        // A drawing that fits has no edges to jump between, and End must not
        // invent one: `max_hscroll` is zero and the pane does not move.
        view.max_hscroll.set(0);
        view.on_key(key(KeyCode::End), &mut store);
        assert_eq!(view.hoffset, 0);
    }
}
