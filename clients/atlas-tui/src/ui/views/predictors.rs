//! PREDICTORS — the full board: every evaluated model, in the owner's ranking.
//!
//! RESEARCH keeps the one-row readout; this pane is the evaluation itself, for
//! the operator asking whether the quantum feature augmentation is earning its
//! place. Everything drawn here is the owner's own arithmetic and the owner's
//! own prose: the verdict sentence, the lane explainer, the champion call and
//! the |t| >= 2 convention all arrive on the payload, because two wordings of
//! an admission verdict would drift and the drifted one would be trusted.
//!
//! The rows render in the owner's ranking order, un-re-sorted, for the same
//! reason RESEARCH does not re-sort the leaderboard.
//!
//! An **armed** window can also run one lane from here — `r`, or the `run` word
//! on the title row, opens a picker over the board's own lanes and Enter posts
//! `/api/research/predictors/run`. That is a research write and reaches nothing
//! else: the route fits a risk model and writes one `predictor_board` run row,
//! and there is no plan, approval or posture anywhere on its path. What it does
//! spend is the owner's CPU, synchronously — which is why one run at a time is
//! refused out loud and the pane draws its own in-flight line. A glass window
//! has none of it: no key, no word, no box, and a board that reads exactly as
//! it did.
//!
//! The lanes on offer are the ones the **owner** named, read off the board this
//! pane is already drawing. A hard-coded list would be a second opinion about a
//! catalog the owner owns; see `FALLBACK_LANES` for the one case where there is
//! nothing to read.
//!
//! Nothing here scrolls. What does not fit is counted rather than dropped.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::{PredictorDetail, PredictorRow};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::table_cell::{cell, head, LEFT, RIGHT};
use crate::ui::widgets::{panel_block, panel_header, refuse, tristate_spark};
#[cfg(feature = "operator")]
use crossterm::event::{KeyCode, MouseButton, MouseEventKind};
use crossterm::event::{KeyEvent, MouseEvent};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Row, Table, Wrap},
    Frame,
};
use std::time::Instant;

/// The nine columns: the model, which lane of the experiment it sits in, its
/// six numbers, and the owner's marks.
///
/// `MODEL` is 18 because the longest id the board can serve is
/// `groupwise:angle_zz` (`research/board.py::MODEL_IDS`); at 17 it would render
/// without its final variant character, which is the character that
/// distinguishes it from `groupwise:angle`. `Δ IC` and `MEAN IC` are seven for
/// a sign, a zero, a point and three decimals — three because the admission
/// bar is 0.03 and two would round the gate itself away.
const COLS: [(&str, u16, bool); 9] = [
    ("MODEL", 18, LEFT),
    ("LANE", 5, LEFT),
    ("MEAN IC", 7, RIGHT),
    ("STD", 6, RIGHT),
    ("Δ IC", 7, RIGHT),
    ("t", 6, RIGHT),
    // Wins over the baseline and negative folds, one cell: both are counts
    // out of the same folds, and two four-wide columns did not fit beside
    // the spark — the desk pane is 77 columns after the rails.
    ("W/N", 5, RIGHT),
    // The fold-by-fold IC shape. Folds that change sign are not a skill
    // estimate, and a sign flip is visible in a spark long before it is
    // legible in a standard deviation.
    ("FOLDS", 8, LEFT),
    ("", 7, LEFT),
];

/// The pane's floor, derived from the columns so the two cannot drift.
const TABLE_W: u16 = table_w();

const fn table_w() -> u16 {
    let mut w = 0;
    let mut i = 0;
    while i < COLS.len() {
        w += COLS[i].1 + 1;
        i += 1;
    }
    w - 1
}

/// What an armed operator is in the middle of. A glass window retains nothing
/// at all — every field below is compiled out of it, and the pane is the
/// read-only table it has always been.
///
/// The fetch that fills the pane is still the runtime's, fired on arriving here
/// and again after a run lands — see `main::ingest`.
#[derive(Default)]
pub struct PredictorsView {
    /// The lane picker: whether it is on screen, and where its cursor is.
    #[cfg(feature = "operator")]
    switch: Switch,
    /// The lane the owner is fitting right now, if any.
    ///
    /// The client's own flag, and it has to be: the route is synchronous and
    /// nothing on `/api/tui` says a board is being fitted, so the only thing
    /// that knows a run is in flight is the process waiting on the answer.
    #[cfg(feature = "operator")]
    running: Option<String>,
    /// What the owner last said about a run, or what this pane refused.
    #[cfg(feature = "operator")]
    said: Option<Said>,
    /// The pane the last frame drew into, so the box can refuse rather than
    /// open where it cannot be read — and so `typing` can decline the keyboard
    /// on the same frame it refuses.
    #[cfg(feature = "operator")]
    area: std::cell::Cell<Rect>,
    /// Where the last frame drew the title row's `run` word.
    ///
    /// Published by `draw` and retracted by every frame, exactly as ATLAS's
    /// `book_word` is: a click is answered about the frame in front of the
    /// operator, never about one not yet painted.
    #[cfg(feature = "operator")]
    run_word: std::cell::Cell<Rect>,
}

/// The lane picker's state.
///
/// The cursor outlives the box deliberately. A refused run reopens on the lane
/// that was refused rather than at the top — the operator's next move is
/// usually to read the owner's sentence about *that* lane, and a cursor that
/// jumped home would make them find it again.
#[cfg(feature = "operator")]
#[derive(Default)]
struct Switch {
    open: bool,
    at: usize,
}

/// The last thing said about a run, and by whom.
///
/// Three variants rather than one string, because the three are toned
/// differently and must be: a board the owner fitted, a lane it declined, and
/// a request that never landed are not the same news, and a client that drew
/// them alike would let "the owner is gone" read as a result.
#[cfg(feature = "operator")]
enum Said {
    /// The owner's own account of a board it fitted.
    Ran(String),
    /// The owner declined the lane, or this pane refused to ask twice.
    Refused(String),
    /// The request never got an answer.
    Failed(String),
}

/// One lane the board knows about.
#[cfg(feature = "operator")]
struct Lane {
    id: String,
    /// The owner's own filing, never inferred here. The baseline is listed and
    /// cannot be chosen: the route runs it beside every lane whether or not it
    /// was asked for, so offering it as a choice would offer a run that is
    /// already happening.
    baseline: bool,
}

/// The lanes `qlab/research/board.py::MODEL_IDS` serves, in its own order.
///
/// **Reached only when the owner has served no board at all** — a desk that has
/// never run one, or a pane whose fetch has not landed. Every other frame reads
/// the ids off the payload, because a list hard-coded here is a second opinion
/// about a catalog the owner owns: a lane added there and not here would be a
/// lane an operator cannot run from the client that is drawing it. The owner
/// refuses an id it does not know, by name and with the lanes it does serve, so
/// the worst this fallback can do is earn that sentence.
#[cfg(feature = "operator")]
const FALLBACK_LANES: [(&str, bool); 7] = [
    ("ridge:none", true),
    ("groupwise:angle", false),
    ("groupwise:zz", false),
    ("groupwise:angle_zz", false),
    ("kernel:linear", false),
    ("kernel:angle", false),
    ("kernel:zz", false),
];

/// The lanes on offer, in the owner's own ranking order.
///
/// Deduplicated on the id, because the ranking is the owner's list and this
/// pane may not assume it holds each model once.
#[cfg(feature = "operator")]
fn lanes(store: &Store) -> Vec<Lane> {
    let mut out: Vec<Lane> = Vec::new();
    if let Some(board) = store.predictor_detail() {
        for row in &board.models {
            let Some(id) = format::text(row.model_id.as_ref()) else {
                continue;
            };
            if out.iter().any(|lane| lane.id == id) {
                continue;
            }
            out.push(Lane {
                id: id.to_string(),
                baseline: row.is_baseline == Some(true),
            });
        }
    }
    if out.is_empty() {
        // Nothing served yet. See `FALLBACK_LANES`.
        out = FALLBACK_LANES
            .iter()
            .map(|(id, baseline)| Lane {
                id: (*id).to_string(),
                baseline: *baseline,
            })
            .collect();
    }
    out
}

/// The lanes the cursor may land on: everything but the baseline.
#[cfg(feature = "operator")]
fn choosable(store: &Store) -> Vec<String> {
    lanes(store)
        .into_iter()
        .filter(|lane| !lane.baseline)
        .map(|lane| lane.id)
        .collect()
}

/// How wide the picker is drawn, and how much of a lane id fits on a row.
#[cfg(feature = "operator")]
const SWITCH_W: u16 = 68;
#[cfg(feature = "operator")]
const LANE_W: usize = 20;
/// The rows the box needs before it is worth opening: a header, a blank, one
/// lane, the baseline and a footer, plus its borders.
#[cfg(feature = "operator")]
const SWITCH_MIN_H: u16 = 8;
/// The clickable word on the title row, and the cells it occupies.
#[cfg(feature = "operator")]
const RUN_WORD: &str = "run";

impl View for PredictorsView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        // Retracted on every frame and re-published only by one that draws the
        // word, exactly as ATLAS's `book_word` is: a rect that outlived its
        // frame would let a click open a picker off a screen nobody is looking
        // at. Every path out of this function leaves it settled, including the
        // refusal below.
        self.no_run_word();
        if area.width < TABLE_W || area.height < 10 {
            refuse(
                f,
                area,
                format!(
                    "PREDICTORS needs {TABLE_W} columns to render the board without \
                     clipping a sign; this pane has {}.",
                    area.width
                ),
            );
            return;
        }

        let block = panel_block();
        let inner = block.inner(area);
        f.render_widget(block, area);
        self.publish(inner);
        self.board(f, inner, store);
        // Over the board rather than beside it, and last: the picker is a
        // question, and a question drawn under the answer it is about is one
        // an operator answers blind.
        self.draw_switch(f, inner, store);
    }

    // `r` is the shell's, and the shell keeps it: the pane is *shown* the key
    // (`ui::shell::on_key`) and the refresh still happens. Nothing here ever
    // returns a command from `r` — what it does is open the picker, and the
    // shell asserts as much.
    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        #[cfg(not(feature = "operator"))]
        {
            // A monitoring window binds nothing here at all: every key pressed
            // on this pane belongs to whoever claims it next.
            let _ = (k, store);
        }
        #[cfg(feature = "operator")]
        {
            // Ahead of the picker, deliberately. `r` while a run is in flight
            // has to be refused out loud whether or not the box is up, and a
            // box that swallowed it would make the busiest minute on this pane
            // the one where the key looks dead.
            if k.code == KeyCode::Char('r') && store.posture.writes() {
                self.offer_run(store);
                return None;
            }
            if self.switch.open {
                return self.picker_key(k, store);
            }
        }
        None
    }

    /// The box owns the keyboard while it is up, exactly as SETTINGS' pickers
    /// do — otherwise `Esc` quits the workstation instead of closing the
    /// question, and `q` walks away from it. Gated on the box *fitting*: a
    /// picker refused for want of rows must not take the keys with it.
    fn typing(&self) -> bool {
        #[cfg(feature = "operator")]
        {
            self.switch.open && self.box_fits()
        }
        #[cfg(not(feature = "operator"))]
        false
    }

    fn on_mouse(&mut self, m: MouseEvent, store: &mut Store) -> Option<Command> {
        #[cfg(not(feature = "operator"))]
        {
            let _ = (m, store);
        }
        // The title row's own word, which does exactly what `r` does and
        // nothing else — it opens the picker, it does not send. A glass frame
        // publishes no rect, so this cannot be reached there even if the
        // branch were compiled.
        #[cfg(feature = "operator")]
        if m.kind == MouseEventKind::Down(MouseButton::Left)
            && store.posture.writes()
            && self.run_word_at(m.column, m.row)
        {
            self.offer_run(store);
        }
        None
    }
}

impl PredictorsView {
    /// The board itself — the three answers this pane can honestly give.
    fn board(&self, f: &mut Frame, inner: Rect, store: &Store) {
        let Some(board) = store.predictor_detail() else {
            // Not-asked-yet and desk-has-no-board are different facts. This
            // one is the client's — the fetch fires on arriving here — so the
            // remedy named is the client's own key, not an owner command.
            let mut lines = vec![
                self.title(inner, store),
                Line::from(""),
                Line::from(Span::styled(
                    "asking the owner for the board — r asks again if this persists",
                    Style::default().fg(theme().text_dim),
                )),
            ];
            lines.extend(self.run_line());
            f.render_widget(Paragraph::new(lines), inner);
            return;
        };

        // A board that never ran or cannot be read carries its own sentence,
        // and the sentence is the whole pane: drawing an empty table under it
        // would render "no evaluation" as an evaluation of nothing.
        if board.status.as_deref() != Some("ok") {
            let mut lines = vec![
                self.title(inner, store),
                Line::from(""),
                Line::from(Span::styled(
                    format::text(board.reason.as_ref())
                        .unwrap_or("the owner served a board with no status")
                        .to_string(),
                    Style::default().fg(status_tone(board)),
                )),
            ];
            lines.extend(self.run_line());
            f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), inner);
            return;
        }

        // The verdict gets the rows the table does not need, and the lane
        // explainer takes what is left: the sentence that says whether this
        // is evidence outranks the paragraph that says what the lanes are.
        let head = self.header_lines(board, inner, store);
        let table_rows = (board.models.len() as u16 + 1).min(12);
        let bar_rows = match board.models.len() {
            0 => 0,
            n => (n as u16 + 2).min(11),
        };
        let rows = Layout::vertical([
            Constraint::Length(head.len() as u16),
            Constraint::Length(verdict_h(board, inner.width)),
            Constraint::Length(table_rows),
            Constraint::Length(bar_rows),
            Constraint::Min(0),
        ])
        .split(inner);

        f.render_widget(Paragraph::new(head), rows[0]);
        f.render_widget(
            Paragraph::new(verdict_line(board)).wrap(Wrap { trim: true }),
            rows[1],
        );
        draw_table(f, rows[2], board);
        draw_delta_bars(f, rows[3], board);
        f.render_widget(
            Paragraph::new(footer_lines(board)).wrap(Wrap { trim: true }),
            rows[4],
        );
    }

    /// The pane's title row, carrying the word that offers a run.
    ///
    /// The word is published as a rect here rather than composed twice: the
    /// three branches above each draw this line at the pane's own first row,
    /// so where it lands is arithmetic on the spans that precede it and never
    /// a second guess about the layout.
    fn title(&self, inner: Rect, store: &Store) -> Line<'static> {
        #[cfg(not(feature = "operator"))]
        let _ = (inner, store);
        // `mut` only in the armed build, where the word is pushed onto it.
        #[allow(unused_mut)]
        let mut spans = panel_header("predictor board").spans;
        #[cfg(feature = "operator")]
        if store.posture.writes() {
            let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
            spans.push(Span::styled(
                format!(" {RUN_WORD}"),
                Style::default()
                    .fg(theme().accent)
                    .add_modifier(Modifier::BOLD),
            ));
            let x = inner.x.saturating_add(used as u16 + 1);
            // Clamped to the pane. A rect past its right edge sits under a
            // rail, where a click would open the picker off a word nobody can
            // see — the rule ATLAS states for the same publication.
            if x + RUN_WORD.len() as u16 <= inner.x + inner.width {
                self.run_word
                    .set(Rect::new(x, inner.y, RUN_WORD.len() as u16, 1));
            }
        }
        Line::from(spans)
    }

    /// The header block: the title, the run it came from, the board's spec, and
    /// whatever a run last said.
    fn header_lines(
        &self,
        board: &PredictorDetail,
        inner: Rect,
        store: &Store,
    ) -> Vec<Line<'static>> {
        let t = theme();
        let note = format!(
            "as of {} · run {}",
            format::text(board.as_of.as_ref()).unwrap_or(MISSING),
            head(
                format::text(board.run_id.as_ref())
                    .unwrap_or(MISSING)
                    .to_string(),
                10
            ),
        );
        let mut spans = self.title(inner, store).spans;
        let used: usize = spans.iter().map(|s| s.content.chars().count()).sum();
        let pad = (inner.width as usize).saturating_sub(used + note.chars().count());
        spans.push(Span::raw(" ".repeat(pad)));
        spans.push(Span::styled(note, Style::default().fg(t.text_dim)));

        let spec = Line::from(Span::styled(
            format!(
                "target {} · horizon {}d · embargo {}d · {} obs · {} folds",
                format::text(board.target.as_ref()).unwrap_or(MISSING),
                count(board.horizon_days),
                count(board.embargo_days),
                count(board.n_obs),
                count(board.n_folds),
            ),
            Style::default().fg(t.text_tertiary),
        ));
        let mut lines = vec![Line::from(spans), spec];
        lines.extend(self.run_line());
        lines
    }
}

// -- running a lane ---------------------------------------------------------

/// The half a monitoring build does not have. Two bodies rather than a `cfg` at
/// every call site: a glass pane has no line to draw and no rect to clear, and
/// the board above should not have to ask which build it is in.
#[cfg(not(feature = "operator"))]
impl PredictorsView {
    fn run_line(&self) -> Option<Line<'static>> {
        None
    }

    fn no_run_word(&self) {}

    fn publish(&self, _inner: Rect) {}

    fn draw_switch(&self, _f: &mut Frame, _inner: Rect, _store: &Store) {}
}

#[cfg(feature = "operator")]
impl PredictorsView {
    /// The header's line: what is happening, or what a run last said.
    ///
    /// **In flight outranks everything here.** What the owner said about the
    /// *previous* board is not news while it is fitting the next one, and a
    /// header that kept showing the old champion would read as the answer to
    /// the key just pressed. The box's own footer takes the other order — see
    /// `note_line`, and why the two are not one line.
    fn run_line(&self) -> Option<Line<'static>> {
        // The pane is 77 cells and every sentence here is already bounded at
        // ingestion, so the header needs no cut of its own.
        self.running_parts()
            .or_else(|| self.said_parts())
            .map(|(text, style)| Line::from(Span::styled(text, style)))
    }

    /// The box's footer: what was last *said*, in preference to what is
    /// happening.
    ///
    /// The opposite order to `run_line`, and both are needed at once. The key
    /// an operator presses during a run is the one that gets refused — "one
    /// run at a time" — and if the box drew the in-flight line too it would
    /// answer a key with the same sentence it was already showing, which reads
    /// as a keystroke that did nothing. The header keeps saying what is
    /// running; the footer says what just happened.
    /// Bounded to the box, which wraps nothing: the owner's refusal is longer
    /// than 66 cells and the header above is where it is read in full. A cut
    /// here rather than a clip, because `format::bounded` ends on a word and
    /// says so, where the renderer would end mid-quote.
    fn note_line(&self, room: usize) -> Option<Line<'static>> {
        self.said_parts()
            .or_else(|| self.running_parts())
            .map(|(text, style)| {
                // One cell short of the room, because the cut adds an
                // ellipsis: bounded to the width exactly, that ellipsis is the
                // character the renderer drops, and the line reads as a
                // sentence that merely stopped.
                Line::from(Span::styled(
                    format::bounded(&text, room.saturating_sub(1)),
                    style,
                ))
            })
    }

    /// The in-flight sentence, if a lane is being fitted.
    fn running_parts(&self) -> Option<(String, Style)> {
        let lane = self.running.as_ref()?;
        Some((
            // The wait is stated because it is a documented property of the
            // route: the owner fits every fold of two lanes before it answers,
            // and an operator watching an unexplained still pane reaches for
            // the key again.
            format!("running {lane}… (this can take a minute)"),
            Style::default()
                .fg(theme().warning)
                .add_modifier(Modifier::BOLD),
        ))
    }

    /// Whatever was last said, toned by who said it.
    fn said_parts(&self) -> Option<(String, Style)> {
        let t = theme();
        let (text, tone) = match self.said.as_ref()? {
            Said::Ran(text) => (text, t.positive),
            Said::Refused(text) => (text, t.warning),
            Said::Failed(text) => (text, t.negative),
        };
        Some((text.clone(), Style::default().fg(tone)))
    }

    /// The pane this frame drew into, for the box that may be opened over it.
    fn publish(&self, inner: Rect) {
        self.area.set(inner);
    }

    /// Whether the last frame's pane had room for the box at all.
    fn box_fits(&self) -> bool {
        self.area.get().height >= SWITCH_MIN_H
    }

    /// This frame drew no `run` word.
    fn no_run_word(&self) {
        self.run_word.set(Rect::default());
    }

    /// Whether the last frame drew the `run` word under that cell.
    fn run_word_at(&self, column: u16, row: u16) -> bool {
        let rect = self.run_word.get();
        rect.height > 0
            && row == rect.y
            && column >= rect.x
            && column < rect.x.saturating_add(rect.width)
    }

    /// Offer the picker — the whole of what `r` and the `run` word do.
    ///
    /// It sends nothing. Opening a box is not a request, and the one keystroke
    /// that becomes one is Enter on a lane the operator has read.
    fn offer_run(&mut self, store: &Store) {
        // One run at a time. The route fits every fold of two lanes on the
        // owner's own CPU, and a held key would put two of those in flight for
        // one decision.
        //
        // Refused out loud. A run takes a minute, so this is the guard an
        // operator is most likely to hit, and a key that silently did nothing
        // there reads as a dead pane rather than a busy one.
        if let Some(lane) = &self.running {
            self.said = Some(Said::Refused(format!(
                "one run at a time — {lane} is still fitting"
            )));
            return;
        }
        // The last answer described the last run. Kept on screen through the
        // box that is about to ask for another, it would be a verdict about a
        // question nobody is asking any more.
        self.said = None;
        self.switch.open = true;
        self.switch.at = self.switch.at.min(choosable(store).len().saturating_sub(1));
    }

    /// One keystroke into the lane picker.
    // Every key claimed here owes a row in `input::KEYMAP` under its own
    // section, and a test reads this function to check it.
    fn picker_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        let rows = choosable(store).len();
        match k.code {
            KeyCode::Up => {
                self.switch.at = self.switch.at.saturating_sub(1);
                None
            }
            KeyCode::Down => {
                self.switch.at = (self.switch.at + 1).min(rows.saturating_sub(1));
                None
            }
            // Nothing was sent, so there is nothing to undo: the box closes and
            // the board underneath is exactly as the owner last served it.
            KeyCode::Esc => {
                self.switch.open = false;
                None
            }
            KeyCode::Enter => self.run(store),
            _ => None,
        }
    }

    /// Send the lane under the cursor.
    ///
    /// The box stays open across the run. It is what the in-flight line and the
    /// owner's refusal are drawn against — a 400 that closed the picker would
    /// take the operator's choice away with it, and the sentence they need to
    /// read is about the lane that is no longer on screen.
    fn run(&mut self, store: &Store) -> Option<Command> {
        if let Some(lane) = &self.running {
            self.said = Some(Said::Refused(format!(
                "one run at a time — {lane} is still fitting"
            )));
            return None;
        }
        let rows = choosable(store);
        let Some(lane) = rows.get(self.switch.at.min(rows.len().saturating_sub(1))) else {
            // A board of nothing but its own baseline. Said rather than
            // silently doing nothing: there is a lane list on screen and the
            // reason it holds no choice is the owner's, not this pane's.
            self.said = Some(Said::Refused(
                "the owner has named no lane but its baseline".to_string(),
            ));
            return None;
        };
        self.said = None;
        self.running = Some(lane.clone());
        Some(Command::RunPredictor {
            model: lane.clone(),
            // The lane this window is pointed at, which is the lane the board
            // in front of the operator was fitted on. The route defaults it to
            // the desk mode; a window pointed at the other one would be handed
            // a board about a panel it is not reading.
            offline: store
                .desk_mode()
                .and_then(|mode| mode.offline)
                .unwrap_or(true),
        })
    }

    /// What the owner said about the run this pane asked for.
    ///
    /// Called from the runtime's one drain point, because the answer arrives on
    /// the bus rather than out of the key that asked for it — see
    /// `views::Views::wrote`.
    ///
    /// **Only the three outcomes that are about a board.** SETTINGS retires its
    /// waits on any answer at all, and it can: its writes come back in
    /// milliseconds. A board is fitted for a minute, so an unrelated news save
    /// landing in the middle of one would clear an in-flight line for a run
    /// that is still going — and the pane would then offer to start a second.
    pub fn wrote(&mut self, outcome: &crate::bus::Wrote) {
        use crate::bus::Wrote;
        match outcome {
            // The board moved, and the refetch behind this outcome is what the
            // pane then draws. The box has done its work.
            Wrote::PredictorRan {
                run_id,
                champion,
                models,
            } => {
                self.running = None;
                self.switch.open = false;
                self.said = Some(Said::Ran(ran_line(run_id, champion, models)));
            }
            // Not confirmable and not a broken request: the owner considered
            // the lane and declined it, so the box stays up with the choice
            // still on it and the owner's sentence — which names the lanes it
            // does serve — under the title.
            Wrote::PredictorRefused { said } => {
                self.running = None;
                self.said = Some(Said::Refused(crate::format::bounded(said, SAID_MAX)));
            }
            // A request that never landed. The pane must not stay in flight
            // over it: a board that refuses `r` forever after one timeout is a
            // client that looks broken.
            Wrote::Failed { what, said } if self.running.is_some() => {
                self.running = None;
                self.said = Some(Said::Failed(crate::format::bounded(
                    &format!("{what} — {said}"),
                    SAID_MAX,
                )));
            }
            _ => {}
        }
    }

    /// The lane picker, drawn over the pane that opened it.
    fn draw_switch(&self, f: &mut Frame, inner: Rect, store: &Store) {
        use ratatui::widgets::{Block, Borders, Clear};
        if !self.switch.open {
            return;
        }
        // Refuse rather than open invisible, the rule every box on this
        // workstation is held to: below the floor there is room for a header
        // and nothing else, and a list an operator cannot see is one they
        // cannot choose from.
        if inner.height < SWITCH_MIN_H {
            let row = Rect {
                x: inner.x,
                y: inner.y + inner.height / 2,
                width: inner.width,
                height: 1,
            };
            f.render_widget(Clear, row);
            refuse(
                f,
                row,
                format!(
                    "the lane picker needs {SWITCH_MIN_H} rows; this pane has {}.",
                    inner.height
                ),
            );
            return;
        }
        let t = theme();
        let w = SWITCH_W.min(inner.width.saturating_sub(4)).max(3);
        let lines = self.switch_lines(store, w.saturating_sub(2) as usize);
        let rect = box_rect(inner, w, lines.len() as u16 + 2);
        f.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(t.accent))
            .style(Style::default().bg(t.bg_raised));
        let boxed = block.inner(rect);
        f.render_widget(block, rect);
        f.render_widget(Paragraph::new(lines), boxed);
    }

    /// The picker's lines: what may be run, then what runs anyway.
    fn switch_lines(&self, store: &Store, room: usize) -> Vec<Line<'static>> {
        let t = theme();
        let rows = lanes(store);
        let choices: Vec<&Lane> = rows.iter().filter(|lane| !lane.baseline).collect();
        let at = self.switch.at.min(choices.len().saturating_sub(1));
        let mut lines = vec![panel_header("run a predictor lane"), Line::from("")];
        if choices.is_empty() {
            lines.push(Line::from(Span::styled(
                " the owner has named no lane but its baseline",
                Style::default().fg(t.text_dim),
            )));
        }
        for (i, lane) in choices.iter().enumerate() {
            let on = i == at;
            lines.push(Line::from(vec![
                Span::styled(
                    if on { " ▸ " } else { "   " },
                    Style::default().fg(t.accent),
                ),
                Span::styled(
                    format!("{:<LANE_W$}", head(lane.id.clone(), LANE_W as u16 - 1)),
                    match on {
                        true => Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
                        false => Style::default().fg(t.text_primary),
                    },
                ),
            ]));
        }
        // Listed rather than hidden, and the cursor cannot reach it — the same
        // rule SETTINGS' method picker holds its research entries to. The
        // baseline is *not* a choice: the owner runs it beside every lane
        // whether or not it was asked for, because a challenger without its
        // control is not evidence, and offering it would offer a run that is
        // already happening.
        for lane in rows.iter().filter(|lane| lane.baseline) {
            lines.push(Line::from(vec![
                Span::raw("   "),
                Span::styled(
                    format!("{:<LANE_W$}", head(lane.id.clone(), LANE_W as u16 - 1)),
                    Style::default().fg(t.text_dim),
                ),
                Span::styled(
                    "always run — the control for the rest",
                    Style::default().fg(t.text_tertiary),
                ),
            ]));
        }
        lines.push(match self.note_line(room) {
            // What the owner said, or what is happening, on the box that asked.
            // It carries the remedy, and the toast that carries it too is gone
            // in four seconds.
            Some(line) => line,
            None => Line::from(Span::styled(
                " Enter fits it · ↑↓ moves · Esc leaves it",
                Style::default().fg(t.text_dim),
            )),
        });
        lines
    }
}

/// How much of an owner sentence this pane renders. The box is 60 cells and
/// wraps nothing, so a paragraph from a proxy in front of the desk would
/// otherwise be a footer nobody can read past.
#[cfg(feature = "operator")]
const SAID_MAX: usize = 200;

/// The owner's account of a board it fitted, as one line.
///
/// **The run and the verdict, and never a claim in between.** A `None` champion
/// is the board saying nothing cleared admission — a result — so it is rendered
/// as that rather than as an absence or, worse, as the lane that was asked for.
#[cfg(feature = "operator")]
fn ran_line(run_id: &Option<String>, champion: &Option<String>, models: &[String]) -> String {
    let run = match run_id.as_deref() {
        Some(id) => format!("run {}", head(id.to_string(), 8)),
        None => "the owner named no run".to_string(),
    };
    match (champion.as_deref(), models.len()) {
        (Some(champion), _) => format!("{run} · champion {champion}"),
        // The count is the honest half of "nothing won": a board of one lane
        // and a board of six that admitted none are different findings.
        (None, n) => format!("{run} · {n} fitted, nothing cleared admission"),
    }
}

/// The box's rect: centred, and never larger than the pane it is drawn over.
///
/// A third copy of this arithmetic on this workstation — the startup door and
/// SETTINGS' pickers have the others — and local rather than shared because
/// those two measure their *height* differently (they wrap; this box does not)
/// and unifying them would move goldens this task does not own.
#[cfg(feature = "operator")]
fn box_rect(area: Rect, w: u16, h: u16) -> Rect {
    let h = h.min(area.height.saturating_sub(2)).max(1);
    Rect {
        x: area.x + (area.width.saturating_sub(w)) / 2,
        y: area.y + (area.height.saturating_sub(h)) / 2,
        width: w,
        height: h,
    }
}

/// The owner's verdict sentence, verbatim, toned by the board's own flags.
fn verdict_line(board: &PredictorDetail) -> Line<'static> {
    Line::from(Span::styled(
        format::text(board.reason.as_ref())
            .unwrap_or("the board stated no verdict")
            .to_string(),
        Style::default().fg(verdict_tone(board)),
    ))
}

/// Rows the verdict needs at this width, so a long "NOT ESTABLISHED" sentence
/// is never silently clipped to its optimistic first clause.
fn verdict_h(board: &PredictorDetail, width: u16) -> u16 {
    let chars = board.reason.as_deref().map(str::len).unwrap_or(24);
    (chars as u16).div_ceil(width.max(1)).clamp(1, 4) + 1
}

/// What the verdict's flags mean, in colour.
///
/// `champion_established: None` with an admitted champion takes the warning
/// tone, not the positive one: "not tested against the selection null" must
/// not render as "passed it".
fn verdict_tone(board: &PredictorDetail) -> Color {
    let t = theme();
    match (board.admitted_any, board.champion_established) {
        (Some(true), Some(true)) => t.positive,
        (Some(true), _) => t.warning,
        _ => t.negative,
    }
}

/// `never_ran` is a fact about the desk; anything else unreadable is a broken
/// contract and takes the warning tone.
fn status_tone(board: &PredictorDetail) -> Color {
    let t = theme();
    match board.status.as_deref() {
        Some("never_ran") => t.text_dim,
        _ => t.warning,
    }
}

/// The board itself, one row per model, in the owner's ranking order.
fn draw_table(f: &mut Frame, area: Rect, board: &PredictorDetail) {
    let t = theme();
    let header = Row::new(
        COLS.map(|(name, width, right)| cell(name.to_string(), Style::default(), right, width)),
    )
    .style(Style::default().fg(t.text_secondary));

    let room = area.height.saturating_sub(1) as usize;
    let total = board.models.len();
    let shown = match total <= room {
        true => total,
        false => room.saturating_sub(1),
    };
    let table = Table::new(
        board.models.iter().take(shown).map(model_row),
        COLS.map(|(_, w, _)| Constraint::Length(w)),
    )
    .header(header)
    .column_spacing(1);
    f.render_widget(table, area);
    if shown < total {
        let at = area.y + 1 + shown as u16;
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!("… {} more", total - shown),
                Style::default().fg(t.text_dim),
            ))),
            Rect::new(area.x, at, area.width, 1),
        );
    }
}

/// One model's row: its id, its lane, its numbers, and the owner's marks.
fn model_row(row: &PredictorRow) -> Row<'static> {
    let t = theme();
    let (lane, lane_tone) = lane_word(row);
    let cells: [(String, Style); COLS.len()] = [
        (
            format::text(row.model_id.as_ref())
                .unwrap_or(MISSING)
                .to_string(),
            Style::default().fg(t.text_primary),
        ),
        (lane.to_string(), Style::default().fg(lane_tone)),
        toned(row.mean_ic),
        (
            row.ic_std
                .map(|v| decimals(v, 3))
                .unwrap_or_else(|| MISSING.to_string()),
            Style::default().fg(t.text_secondary),
        ),
        toned_signed(row.delta_mean_ic_vs_baseline),
        toned_at(row.paired_t_vs_baseline, 2),
        (
            format!(
                "{}/{}",
                row.wins_vs_baseline
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| MISSING.to_string()),
                row.negative_folds
                    .map(|v| v.to_string())
                    .unwrap_or_else(|| MISSING.to_string()),
            ),
            Style::default().fg(t.text_secondary),
        ),
        fold_spark(row),
        mark(row),
    ];
    Row::new(
        cells
            .into_iter()
            .zip(COLS)
            .map(|((text, style), (_, width, right))| cell(text, style, right, width)),
    )
}

/// The per-fold ICs as an eight-level spark, toned by the delta's own sign.
///
/// The quantizer is the desk's one (`tristate_spark`): two spellings of "the
/// tail of a series" is how bars and colour once came to disagree. An empty
/// series is absent, not flat — the summary payload serves fold dicts and the
/// detail serves plain numbers, and a decode drift would land here first.
fn fold_spark(row: &PredictorRow) -> (String, Style) {
    let t = theme();
    if row.per_fold.is_empty() {
        return (MISSING.to_string(), Style::default().fg(t.text_tertiary));
    }
    let tone = match row.delta_mean_ic_vs_baseline {
        Some(d) => format::change_tone(d, 3),
        None => t.text_secondary,
    };
    (
        tristate_spark::glyphs(&row.per_fold, 8),
        Style::default().fg(tone),
    )
}

/// Δ IC vs the baseline, as magnitude bars in the owner's ranking order.
///
/// The table's Δ column says the number; this pane says the *proportion* — a
/// mapped model earning a third of what another loses is legible at a glance
/// here and only at a squint in a column of signed decimals. Scaled to the
/// board's own largest |Δ|, and the baseline row is the axis itself.
fn draw_delta_bars(f: &mut Frame, area: Rect, board: &PredictorDetail) {
    if area.height < 2 {
        return;
    }
    let t = theme();
    let widest = board
        .models
        .iter()
        .filter_map(|m| m.delta_mean_ic_vs_baseline)
        .map(f64::abs)
        .fold(0.0_f64, f64::max);
    let mut lines = vec![Line::from(Span::styled(
        format!(
            "Δ IC vs {}",
            format::text(board.baseline.as_ref()).unwrap_or(MISSING)
        ),
        Style::default().fg(t.text_secondary),
    ))];
    let room = area.height.saturating_sub(1) as usize;
    for row in board.models.iter().take(room) {
        lines.push(delta_bar_line(row, widest));
    }
    f.render_widget(Paragraph::new(lines), area);
}

/// One model's bar: id, magnitude, signed value — toned by what is printed.
fn delta_bar_line(row: &PredictorRow, widest: f64) -> Line<'static> {
    let t = theme();
    const BAR_W: usize = 24;
    let id = format!(
        "{:<19}",
        head(
            format::text(row.model_id.as_ref())
                .unwrap_or(MISSING)
                .to_string(),
            18
        )
    );
    let Some(delta) = row.delta_mean_ic_vs_baseline else {
        return Line::from(vec![
            Span::styled(id, Style::default().fg(t.text_primary)),
            Span::styled(MISSING.to_string(), Style::default().fg(t.text_tertiary)),
        ]);
    };
    if row.is_baseline == Some(true) {
        return Line::from(vec![
            Span::styled(id, Style::default().fg(t.text_primary)),
            Span::styled(
                format!("{:<BAR_W$} baseline", "▏", BAR_W = BAR_W),
                Style::default().fg(t.text_dim),
            ),
        ]);
    }
    // Printed-value discipline, same as every signed cell: dust that rounds
    // to 0.000 draws the axis tick, not a one-cell bar claiming an edge.
    let printed = signed(delta, 3);
    let cells = if widest > 0.0 && !printed.chars().all(|c| c == '0' || c == '.') {
        ((delta.abs() / widest) * BAR_W as f64).round().max(1.0) as usize
    } else {
        0
    };
    let bar = format!("{:<BAR_W$}", "█".repeat(cells.min(BAR_W)), BAR_W = BAR_W);
    Line::from(vec![
        Span::styled(id, Style::default().fg(t.text_primary)),
        Span::styled(bar, Style::default().fg(format::change_tone(delta, 3))),
        Span::styled(
            format!(" {printed}"),
            Style::default().fg(format::change_tone(delta, 3)),
        ),
    ])
}

/// Which arm of the experiment a model sits in, as the owner filed it.
///
/// Read off `augmented` rather than the family: `kernel:linear` is a control
/// in the kernel family, and a lane inferred from the id would file it in the
/// treatment arm — the exact mistake the payload's `control_note` documents.
fn lane_word(row: &PredictorRow) -> (&'static str, Color) {
    let t = theme();
    match row.augmented {
        Some(true) => ("quant", t.accent),
        Some(false) => ("ctrl", t.text_dim),
        None => (MISSING, t.text_tertiary),
    }
}

/// The owner's marks on a row. `★CHAMP` outranks `BASE` outranks `sig`:
/// exactly one model can be champion, the baseline cannot be it, and a
/// significant loser is still worth a mark — |t| >= 2 *below* the baseline is
/// the augmentation measurably costing accuracy.
fn mark(row: &PredictorRow) -> (String, Style) {
    let t = theme();
    if row.is_champion == Some(true) {
        return (
            "★CHAMP".to_string(),
            Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
        );
    }
    if row.is_baseline == Some(true) {
        return ("BASE".to_string(), Style::default().fg(t.text_secondary));
    }
    match row.significant {
        Some(true) => ("sig".to_string(), Style::default().fg(t.cyan)),
        _ => (String::new(), Style::default()),
    }
}

/// Caveats first — the owner's own honesty — then the lane explainer.
fn footer_lines(board: &PredictorDetail) -> Vec<Line<'static>> {
    let t = theme();
    let mut lines = Vec::new();
    for caveat in &board.caveats {
        lines.push(Line::from(Span::styled(
            format!("· {caveat}"),
            Style::default().fg(t.warning),
        )));
    }
    if let Some(lane) = format::text(board.lane.as_ref()) {
        if !lines.is_empty() {
            lines.push(Line::from(""));
        }
        lines.push(Line::from(Span::styled(
            lane.to_string(),
            Style::default().fg(t.text_dim),
        )));
    }
    lines
}

/// An IC cell, toned on the printed value.
fn toned(value: Option<f64>) -> (String, Style) {
    let t = theme();
    match value {
        Some(v) => (
            decimals(v, 3),
            Style::default().fg(format::change_tone(v, 3)),
        ),
        None => (MISSING.to_string(), Style::default().fg(t.text_tertiary)),
    }
}

/// A delta cell: signed by what is printed, because `0.031` beside a baseline
/// does not say which side of it the model sits on.
fn toned_signed(value: Option<f64>) -> (String, Style) {
    let t = theme();
    match value {
        Some(v) => (signed(v, 3), Style::default().fg(format::change_tone(v, 3))),
        None => (MISSING.to_string(), Style::default().fg(t.text_tertiary)),
    }
}

/// A t-statistic cell at its own precision.
fn toned_at(value: Option<f64>, dp: usize) -> (String, Style) {
    let t = theme();
    match value {
        Some(v) => (
            signed(v, dp),
            Style::default().fg(format::change_tone(v, dp)),
        ),
        None => (MISSING.to_string(), Style::default().fg(t.text_tertiary)),
    }
}

/// A count that may be absent. Absent is not zero: a board with no stated fold
/// count has not said "no folds".
fn count(value: Option<i64>) -> String {
    value
        .map(|v| v.to_string())
        .unwrap_or_else(|| MISSING.to_string())
}

/// A plain decimal, signed by what is printed rather than by the double —
/// `{:.3}` on -1e-13 would draw a minus sign on a zero.
fn decimals(value: f64, dp: usize) -> String {
    if !value.is_finite() {
        return MISSING.to_string();
    }
    let digits = format!("{:.*}", dp, value.abs());
    match format::negative_at(value, dp) {
        true => format!("-{digits}"),
        false => digits,
    }
}

/// An explicit `+` on the positive side, signed by what is printed: a value
/// that rounds away prints `0.000` and carries no sign in either direction —
/// `+` on a zero would claim an edge the precision cannot see.
fn signed(value: f64, dp: usize) -> String {
    let printed = decimals(value, dp);
    let zero = printed.chars().all(|c| c == '0' || c == '.');
    match printed.starts_with('-') || zero || !value.is_finite() {
        true => printed,
        false => format!("+{printed}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_table_floor_is_the_columns_own_arithmetic() {
        let columns: u16 = COLS.iter().map(|(_, w, _)| w).sum();
        assert_eq!(TABLE_W, columns + COLS.len() as u16 - 1);
    }

    /// The longest id `research/board.py::MODEL_IDS` can serve, verbatim. At
    /// one column narrower it loses the character that distinguishes it from
    /// `groupwise:angle`.
    const LONGEST_MODEL: &str = "groupwise:angle_zz";

    #[test]
    fn the_model_column_holds_the_boards_longest_id() {
        let width = COLS[0].1;
        assert!(LONGEST_MODEL.chars().count() <= width as usize);
        assert_eq!(head(LONGEST_MODEL.to_string(), width), LONGEST_MODEL);
    }

    #[test]
    fn every_metric_column_fits_its_widest_honest_rendering() {
        // Right-aligned cells lose their *leading* characters when they do not
        // fit — the sign first, which inverts the number.
        let width = |name: &str| COLS.iter().find(|(n, _, _)| *n == name).unwrap().1 as usize;
        assert!(decimals(-0.123, 3).chars().count() <= width("MEAN IC"));
        assert!(signed(-0.123, 3).chars().count() <= width("Δ IC"));
        assert!(signed(-12.34, 2).chars().count() <= width("t"));
        assert!(decimals(-0.123, 3).chars().count() <= width("STD"));
    }

    #[test]
    fn the_lane_is_the_owners_filing_not_the_familys() {
        // `kernel:linear`: kernel family, no feature map. The lane must come
        // off `augmented`, or a control renders in the treatment arm.
        let control = PredictorRow {
            family: Some("kernel".into()),
            augmented: Some(false),
            ..Default::default()
        };
        assert_eq!(lane_word(&control).0, "ctrl");
        let mapped = PredictorRow {
            augmented: Some(true),
            ..Default::default()
        };
        assert_eq!(lane_word(&mapped).0, "quant");
        // Unstated is unstated — not a control by default.
        assert_eq!(lane_word(&PredictorRow::default()).0, MISSING);
    }

    #[test]
    fn a_champion_mark_outranks_baseline_outranks_significance() {
        let champion = PredictorRow {
            is_champion: Some(true),
            is_baseline: Some(false),
            significant: Some(true),
            ..Default::default()
        };
        assert_eq!(mark(&champion).0, "★CHAMP");
        let baseline = PredictorRow {
            is_baseline: Some(true),
            significant: Some(true),
            ..Default::default()
        };
        assert_eq!(mark(&baseline).0, "BASE");
        // A significant *loser* still earns the mark: |t| >= 2 below the
        // baseline is the augmentation measurably costing accuracy.
        let loser = PredictorRow {
            significant: Some(true),
            delta_mean_ic_vs_baseline: Some(-0.05),
            ..Default::default()
        };
        assert_eq!(mark(&loser).0, "sig");
        assert_eq!(mark(&PredictorRow::default()).0, "");
    }

    #[test]
    fn not_tested_against_the_null_does_not_render_as_passed() {
        let t = theme();
        let tested = PredictorDetail {
            admitted_any: Some(true),
            champion_established: Some(true),
            ..Default::default()
        };
        assert_eq!(verdict_tone(&tested), t.positive);
        // Admitted with the null untested (or withheld) is the warning tone.
        let untested = PredictorDetail {
            admitted_any: Some(true),
            champion_established: None,
            ..Default::default()
        };
        assert_eq!(verdict_tone(&untested), t.warning);
        let refuted = PredictorDetail {
            admitted_any: Some(true),
            champion_established: Some(false),
            ..Default::default()
        };
        assert_eq!(verdict_tone(&refuted), t.warning);
        let nothing = PredictorDetail {
            admitted_any: Some(false),
            ..Default::default()
        };
        assert_eq!(verdict_tone(&nothing), t.negative);
    }

    #[test]
    fn a_long_verdict_is_given_rows_rather_than_clipped() {
        // The pessimistic clause arrives *after* the optimistic one in the
        // owner's sentence, so clipping is not neutral: it keeps the good news.
        let board = PredictorDetail {
            reason: Some("x".repeat(300)),
            ..Default::default()
        };
        assert_eq!(verdict_h(&board, 100), 4);
        let short = PredictorDetail {
            reason: Some("admitted".into()),
            ..Default::default()
        };
        assert_eq!(verdict_h(&short, 100), 2);
    }

    #[test]
    fn signs_are_decided_on_what_is_printed() {
        assert_eq!(signed(0.031, 3), "+0.031");
        assert_eq!(signed(-0.031, 3), "-0.031");
        // Dust rounds to zero and must not carry a sign either way.
        assert_eq!(signed(-1e-13, 3), "0.000");
        assert_eq!(decimals(f64::NAN, 3), MISSING);
    }
}
