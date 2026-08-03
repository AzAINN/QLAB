//! The startup door: which desk, which minds, and the login the first two may need.
//!
//! Three steps over one modal, drawn over the shell the way a confirmation box
//! is and claiming the keyboard above everything but Ctrl-C. It is the flow the
//! Textual client has had since the beginning (`qlab/tui/desk_mode_screen.py`),
//! and the two virtues ported from it are the ones that are easy to lose:
//! **two-step disclosure**, so `synthetic` + `alpaca` is unreachable by
//! construction rather than rejected by a message, and **Esc is
//! `synthetic · simulated`**, so the key a human presses to get out of the way
//! can never leave a desk pointed at a real venue.
//!
//! ## What the owner can and cannot say
//!
//! The trigger this door was specified against is "the desk has not been
//! chosen". **The owner cannot report that.** `UISession.__init__` resolves
//! `desk_mode` as `desk_mode or load_desk_mode() or DEFAULT_DESK_MODE`, and
//! `desk_mode_payload()` then serves a concrete `data`/`book`/`label` with no
//! flag saying whether anything was persisted — so a desk nobody has ever
//! chosen is served, byte for byte, as a desk chosen to be `synthetic ·
//! simulated`.
//!
//! So the store-driven arm is the honest half of that predicate and is named
//! for what it actually observes: the owner **answered** and said **nothing**
//! about which desk this is (`last_snapshot_at.is_some() && desk_mode().is_none()`
//! — an owner too old to serve the block, or a payload missing it). The
//! operator-driven arm is `--pick`, which is what asks the question on a desk
//! that did answer. Serving `chosen: bool` from the owner would let the first
//! arm mean what it was meant to mean; that is an owner-side change and is on
//! the ledger rather than in this file.
//!
//! ## What it may do
//!
//! Nothing here books anything. Step 1 points the desk at a data source and a
//! book — `DeskMode` grants no authority, every gate downstream is unmoved, and
//! the owner refuses the pair it cannot make. Step 2 chooses which mind answers
//! a question, which the owner pins the referee out of whatever it says. Step 3
//! does not exist as a form at all: it hands the keyboard to SETTINGS' own
//! login box, which is the single place in this client a credential is typed.
//!
//! A window that cannot write gets a door that says so and offers nothing —
//! the same rule as every other operator affordance here. It is a statement,
//! not a question, so any key dismisses it, exactly as the help overlay does.

use crate::cmd::{self, Command, ModelChoice, Offer};
use crate::format::{self, MISSING};
use crate::model::DeskMode;
use crate::store::{Store, ViewId};
use crate::theme::theme;
use crate::ui::views::Views;
use crate::ui::widgets::{panel_header, refuse};
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
    Frame,
};
use std::cell::Cell;

/// The box's width. Wide enough for a row's label beside the owner's reason for
/// refusing it, and for the credential description to wrap in two lines.
const DOOR_W: u16 = 68;

/// The door's own floor, in rows of the frame.
///
/// Its own, and stated rather than derived: with the book row disclosed the
/// step draws a header, two data rows, the credential description, a book
/// title, two book rows, the sentence about what the simulated book does, the
/// row that moves on, a note, and a footer. A box drawn into less would hold
/// the header and the first question while still taking every keystroke, which
/// is the armed-and-invisible state WORKFORCE's picker and SETTINGS' login form
/// both refuse at.
const DOOR_MIN_H: u16 = 16;

/// The same, in columns. Below this the rows and the owner's sentences wrap
/// into each other and the two-step disclosure stops being legible as two
/// steps.
const DOOR_MIN_W: u16 = 48;

/// How much of an owner sentence a row may carry. D1's `SAID_MAX`, for the
/// reason it gives: nothing on the wire is guaranteed to be the owner's, and
/// the longest sentence it actually writes survives uncut.
const SAID_MAX: usize = 112;

/// What the first question's marker says: the pair this door would apply,
/// which is the desk's own until the operator moves it.
const CHOSEN: &str = "chosen";

/// What the second's says: the model that surface is running now. A different
/// claim from `CHOSEN` — a choice there is sent the moment it is made, so the
/// row that carries this is a fact about the desk rather than about the door.
const RUNNING: &str = "now";

/// The two halves of a desk mode, in the owner's own spelling
/// (`qlab/core/desk_mode.py`).
const SYNTHETIC: &str = "synthetic";
const LIVE: &str = "live";
const SIMULATED: &str = "simulated";
const ALPACA: &str = "alpaca";

/// Which question the door is on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    /// Which data, and whose book.
    Mode,
    /// Which model each surface runs.
    Model,
}

/// The door, while it is up.
///
/// Held by the `Store` beside the command line and the help offset, because it
/// is *where the operator is looking* rather than anything the owner said —
/// which is also what keeps a frame a pure function of (store, effects,
/// instant).
#[derive(Debug)]
pub struct Door {
    step: Step,
    /// What the operator has chosen, while they have.
    ///
    /// Absent is **the desk's own pair, read live**. A door opened before the
    /// first snapshot would otherwise pin `synthetic · simulated` as if the
    /// desk had said so, and then keep showing it after the owner answered
    /// `live · alpaca` — which is the one desk this door's third step exists
    /// for.
    data: Option<&'static str>,
    book: Option<&'static str>,
    /// Which row the cursor is on, and the first row the box has room to draw.
    at: usize,
    top: usize,
    /// What the door last said back — a row that cannot be chosen, and why.
    /// Retired by the next keystroke, like the command line's own note.
    note: Option<String>,
    /// Set by the keystroke that finishes the door. The shell settles it.
    closed: bool,
    /// The frame the door was last drawn into, published by [`Door::draw`].
    ///
    /// Interior mutability for the reason `SettingsView` has it: `draw` is a
    /// `&self` renderer that publishes the layout it derived, and whether the
    /// box fits is a fact about that layout. Nothing renders from it — it only
    /// decides whether a keystroke may reach a box nobody can see. Zero before
    /// the first frame, which refuses.
    area: Cell<Rect>,
}

impl Default for Door {
    fn default() -> Self {
        Self {
            step: Step::Mode,
            data: None,
            book: None,
            at: 0,
            top: 0,
            note: None,
            closed: false,
            area: Cell::new(Rect::default()),
        }
    }
}

impl Door {
    /// Whether a desk opening now needs a door.
    ///
    /// Two arms, and they answer different questions. `forced` is `--pick`: the
    /// operator started this run to choose, so the door opens whatever the desk
    /// says. The other is the store-driven one — the owner **answered** and
    /// said **nothing** about which desk this is. See this module's header for
    /// why that is not the same claim as "nobody has chosen": the owner cannot
    /// make that one.
    ///
    /// `answered` is required on the second arm and not on the first. Without
    /// it every client would open a door in the second before its first poll
    /// lands, on a desk that was about to say exactly which one it is.
    pub fn wanted(forced: bool, answered: bool, unsaid: bool) -> bool {
        forced || (answered && unsaid)
    }

    /// Which question is up. Public so a test can pin the walk rather than
    /// infer it from what was drawn.
    pub fn step(&self) -> Step {
        self.step
    }

    /// Whether the door is still asking. `false` once a keystroke finished it.
    pub fn standing(&self) -> bool {
        !self.closed
    }

    /// Whether the last frame left room to draw the box.
    ///
    /// Read off the published area, because the floor is a fact about the frame
    /// and a key handler is never told one.
    pub fn fits(&self) -> bool {
        let area = self.area.get();
        area.height >= DOOR_MIN_H && area.width >= DOOR_MIN_W
    }

    // -- what the door is pointing at --------------------------------------

    /// The data source the door would apply: what the operator chose, or what
    /// the desk already is.
    fn data(&self, store: &Store) -> &'static str {
        self.data
            .or_else(|| word(store, |mode| mode.data.as_ref(), [SYNTHETIC, LIVE]))
            .unwrap_or(SYNTHETIC)
    }

    fn book(&self, store: &Store) -> &'static str {
        self.book
            .or_else(|| word(store, |mode| mode.book.as_ref(), [SIMULATED, ALPACA]))
            .unwrap_or(SIMULATED)
    }

    /// Whether the desk reports a login the Alpaca book could be reached with.
    ///
    /// `Some(true)` only, which is the rule the whole client reads this flag
    /// by: an owner that did not say is not an owner that said yes, and this is
    /// the gate in front of the one row that reaches a real account.
    fn credentials_ok(&self, store: &Store) -> bool {
        store.desk_mode().and_then(|mode| mode.credentials_ok) == Some(true)
    }

    /// The owner's description of the credential it can read, bounded.
    fn credentials(&self, store: &Store) -> Option<String> {
        let mode = store.desk_mode()?;
        format::text(mode.credentials.as_ref()).map(|said| format::bounded(said, SAID_MAX))
    }

    // -- the rows ----------------------------------------------------------

    /// Step 1's rows, in the order they are drawn.
    ///
    /// **The book rows appear only once `live` is what the door is pointing
    /// at.** That is the two-step disclosure, and it is what makes the
    /// `synthetic`/`alpaca` pair unreachable rather than refused: no keystroke
    /// reaches the alpaca row from a synthetic desk, so the pair the owner's
    /// `DeskMode.__post_init__` raises on cannot be composed here at all.
    fn mode_rows(&self, store: &Store) -> Vec<ModeRow> {
        let mut rows = vec![ModeRow::Data(SYNTHETIC), ModeRow::Data(LIVE)];
        if self.data(store) == LIVE {
            rows.push(ModeRow::Book(SIMULATED));
            rows.push(ModeRow::Book(ALPACA));
        }
        rows.push(ModeRow::Next);
        rows
    }

    /// Step 2's rows: what each surface can be pointed at, out of the catalog
    /// and nowhere else.
    ///
    /// Built by `cmd::offers`, which is the same function the `/model` strip
    /// reads — so the honesty rules are inherited rather than restated: a
    /// backend the desk cannot reach stays on the list with the owner's own
    /// sentence and cannot be chosen, a backend that says it can serve and
    /// names nothing gets its own sentence, and the workforce is offered
    /// `claude` alone because the tier map owns its model.
    fn model_rows(&self, store: &Store) -> Vec<ModelRow> {
        let mut rows = Vec::new();
        for surface in cmd::SURFACES {
            for offer in cmd::offers(surface, store) {
                rows.push(ModelRow::Offer {
                    surface,
                    current: is_current(store, surface, &offer),
                    value: offer.value().to_string(),
                    refusal: offer.refusal().map(str::to_string),
                    choice: offer.choice(),
                });
            }
        }
        rows.push(ModelRow::Keep);
        rows
    }

    /// How many rows the cursor may walk on this step.
    fn len(&self, store: &Store) -> usize {
        match self.step {
            Step::Mode => self.mode_rows(store).len(),
            Step::Model => self.model_rows(store).len(),
        }
    }

    // -- the keys ----------------------------------------------------------

    /// One keystroke into the door.
    ///
    /// Returns what the runtime should do, exactly as every other surface here:
    /// this module decides what a key *means* and never acts. The store is
    /// borrowed mutably only for the two things a keystroke can move that are
    /// not the door's own — the nav, when the third step hands the keyboard to
    /// SETTINGS.
    ///
    /// A window that cannot write is shown a statement rather than a question,
    /// so any key dismisses it — the help overlay's rule, and the reason no key
    /// is swallowed there with no visible effect.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    pub fn on_key(&mut self, k: KeyEvent, store: &mut Store, views: &mut Views) -> Option<Command> {
        self.note = None;
        if !store.posture.writes() {
            self.closed = true;
            return None;
        }
        match k.code {
            KeyCode::Up => self.step_by(-1, store),
            KeyCode::Down => self.step_by(1, store),
            KeyCode::Enter => return self.enter(store, views),
            KeyCode::Esc => return self.escape(store, views),
            _ => {}
        }
        None
    }

    /// Move the cursor, and bring the window with it.
    fn step_by(&mut self, by: isize, store: &Store) {
        let last = self.len(store).saturating_sub(1);
        self.at = self.at.min(last).saturating_add_signed(by).min(last);
        self.scroll();
    }

    /// Keep the cursor inside the rows the box has room to draw.
    ///
    /// A walk that left the cursor off the box would be a selection an operator
    /// cannot see — the same failure the floors above refuse at, one row down.
    fn scroll(&mut self) {
        let cap = self.cap();
        if self.at < self.top {
            self.top = self.at;
        } else if self.at >= self.top + cap {
            self.top = self.at + 1 - cap;
        }
    }

    /// How many rows the box can draw at the frame it was last given.
    ///
    /// The fixed lines are the header, the note and the footer, the two
    /// markers that say rows are off the box, and the border. One row is the
    /// floor: a box that could draw none of the list would be a question with
    /// no answers on it.
    fn cap(&self) -> usize {
        (self.area.get().height as usize).saturating_sub(8).max(1)
    }

    /// What Enter does on the row the cursor is on.
    fn enter(&mut self, store: &mut Store, views: &mut Views) -> Option<Command> {
        match self.step {
            Step::Mode => {
                match *self.mode_rows(store).get(self.at)? {
                    ModeRow::Data(SYNTHETIC) => {
                        // Both halves, because the owner allows the synthetic
                        // desk exactly one book. Leaving `alpaca` staged under
                        // a synthetic data source would compose the pair
                        // `DeskMode.__post_init__` raises on.
                        self.data = Some(SYNTHETIC);
                        self.book = Some(SIMULATED);
                    }
                    // The one row that can point this desk at a real account,
                    // and the gate in front of it is the owner's own reading of
                    // the credential file. A desk that is *already* live
                    // arrives with this row current and may keep it — what is
                    // refused is *newly* pointing a desk at a venue it has no
                    // login for.
                    ModeRow::Data(LIVE) => match self.credentials_ok(store) {
                        true => self.data = Some(LIVE),
                        false => {
                            self.note = Some(self.credentials(store).unwrap_or_else(|| {
                                "the desk reports no usable Alpaca login".to_string()
                            }))
                        }
                    },
                    ModeRow::Data(_) => {}
                    ModeRow::Book(book) => self.book = Some(book),
                    ModeRow::Next => {
                        self.step = Step::Model;
                        self.at = self.current_model(store);
                        self.top = 0;
                        self.scroll();
                        // The catalog behind the next step, asked for on the
                        // way in and not on a beat — `shell::command_key` asks
                        // for it the same way when the palette enters the model
                        // scope, and the runtime drops the request inside the
                        // owner's own cache window.
                        return Some(Command::Backends);
                    }
                }
                None
            }
            Step::Model => match self.model_rows(store).get(self.at)? {
                // Shown on the list and refused here, in the owner's own
                // sentence rather than a second opinion composed by this
                // client — the rule `/model` already submits by.
                ModelRow::Offer {
                    refusal: Some(said),
                    ..
                } => {
                    self.note = Some(said.clone());
                    None
                }
                ModelRow::Offer {
                    surface,
                    choice: Some(choice),
                    ..
                } => chose(surface, choice.clone()),
                // A pair with no choice behind it cannot happen — `cmd::Offer`
                // carries one for everything it does not refuse — and saying so
                // is cheaper than a branch that silently does nothing.
                ModelRow::Offer { choice: None, .. } => {
                    self.note = Some("the desk offered a model it cannot name".to_string());
                    None
                }
                ModelRow::Keep => self.finish(store, views),
            },
        }
    }

    /// What Esc does, per step.
    ///
    /// On the first question it is **the safe desk, always** — the Textual
    /// door's own binding. A human pressing Escape to get out of the way must
    /// never be what leaves a desk pointed at a real venue, so this is the one
    /// key here whose meaning does not depend on where the cursor is.
    ///
    /// On the second it is the skip: the models stay as the desk has them, and
    /// the pair from the first question is applied on the way out.
    fn escape(&mut self, store: &mut Store, views: &mut Views) -> Option<Command> {
        if self.step == Step::Mode {
            self.data = Some(SYNTHETIC);
            self.book = Some(SIMULATED);
        }
        self.finish(store, views)
    }

    /// Apply the desk the door settled on, and decide what the operator lands
    /// in.
    ///
    /// The pair is written **here**, at the end, rather than by the keystroke
    /// that chose it. Two reasons, and the second is the load-bearing one: the
    /// door is one question to an operator, so a walk that never reached the
    /// last row should not have moved the desk; and a keystroke can carry
    /// exactly one `Command`, so a step that both applied a pair and asked for
    /// the catalog would have to drop one of them.
    ///
    /// It is written **unconditionally**, including when the pair is what the
    /// desk already reads. This client cannot tell a persisted choice from the
    /// owner's default — `desk_mode_payload` serves both identically — and the
    /// POST is what makes the answer durable, so a door that skipped the write
    /// on an unchanged pair would leave the desk exactly as unchosen as it
    /// found it.
    fn finish(&mut self, store: &mut Store, views: &mut Views) -> Option<Command> {
        let (data, book) = (self.data(store), self.book(store));
        self.closed = true;
        // The third step, and the only condition it has: the desk is about to
        // be pointed at the real book and the owner cannot read a login for it.
        // Not a form of its own — SETTINGS owns the one box in this client a
        // credential is typed into, and its consent flow is already the owner's
        // own question about destroying a stored profile.
        if book == ALPACA && !self.credentials_ok(store) {
            store.nav.view = ViewId::Settings;
            views.open_login();
        }
        pointed(data, book)
    }

    /// Which model row the desk is already on, or the row that keeps it there.
    ///
    /// The default is the current config, so an operator who presses Enter
    /// twice changes nothing — and one who does not recognise their own choice
    /// on the list is being told something true about it.
    fn current_model(&self, store: &Store) -> usize {
        let rows = self.model_rows(store);
        rows.iter()
            .position(|row| matches!(row, ModelRow::Offer { current: true, .. }))
            .unwrap_or(rows.len().saturating_sub(1))
    }

    // -- the frame ---------------------------------------------------------

    /// The door, over the whole frame.
    ///
    /// Over the frame rather than over a pane, for the reason the confirmation
    /// box is: the question is about the whole workstation, and a box confined
    /// to one region would leave a live desk repainting inside the border a
    /// human is reading.
    pub fn draw(&self, f: &mut Frame, area: Rect, store: &Store) {
        // Published first, and on every frame including the ones that refuse:
        // this is what the next keystroke reads to decide whether it may reach
        // a box nobody can see, and an area only recorded when the door already
        // fitted could never report that it stopped fitting.
        self.area.set(area);
        if !self.fits() {
            let row = Rect {
                x: area.x,
                y: area.y + area.height / 2,
                width: area.width,
                height: 1,
            };
            f.render_widget(Clear, row);
            refuse(
                f,
                row,
                format!(
                    "the startup door needs {DOOR_MIN_H} rows and {DOOR_MIN_W} columns; \
                     this terminal has {}×{}. Any key dismisses it — /mode and /model ask \
                     the same questions.",
                    area.width, area.height
                ),
            );
            return;
        }
        let t = theme();
        let w = DOOR_W.min(area.width.saturating_sub(4)).max(3);
        let lines = match store.posture.writes() {
            true => match self.step {
                Step::Mode => self.mode_lines(store),
                Step::Model => self.model_lines(store),
            },
            false => self.glass_lines(store),
        };
        let rect = centred(area, w, wanted(&lines, w - 2));
        f.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(t.accent))
            .style(Style::default().bg(t.bg_raised));
        let inner = block.inner(rect);
        f.render_widget(block, rect);
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
    }

    /// The first question: which data, and — once that answer allows one —
    /// whose book.
    fn mode_lines(&self, store: &Store) -> Vec<Line<'static>> {
        let t = theme();
        let rows = self.mode_rows(store);
        let live = self.data(store) == LIVE;
        let ok = self.credentials_ok(store);
        let mut lines = vec![panel_header("this desk"), section("which data")];
        for (i, row) in rows.iter().enumerate() {
            match *row {
                ModeRow::Data(SYNTHETIC) => lines.push(self.row(
                    i,
                    "SYNTHETIC",
                    (!live).then_some(CHOSEN),
                    true,
                    Some("prices this desk makes; no order leaves it"),
                )),
                ModeRow::Data(LIVE) => lines.push(self.row(
                    i,
                    "LIVE",
                    live.then_some(CHOSEN),
                    // The Textual door disables this button outright without a
                    // credential; here the row stays and carries the reason,
                    // which is the same discipline the model list is drawn by.
                    ok || live,
                    (!ok).then_some("no Alpaca login the desk can read"),
                )),
                ModeRow::Data(_) => {}
                ModeRow::Book(SIMULATED) => {
                    lines.push(section("which book"));
                    lines.push(self.row(
                        i,
                        "SIMULATED",
                        (self.book(store) == SIMULATED).then_some(CHOSEN),
                        true,
                        Some("real prices; no order ever sent to Alpaca"),
                    ));
                }
                ModeRow::Book(_) => lines.push(self.row(
                    i,
                    "ALPACA PAPER",
                    (self.book(store) == ALPACA).then_some(CHOSEN),
                    true,
                    Some("the paper account; a fill still needs you"),
                )),
                ModeRow::Next => lines.push(self.row(i, "models →", None, true, None)),
            }
        }
        // The owner's own description of what it can read, whatever the
        // verdict, and toned by it — the rule SETTINGS' desk card is drawn by.
        // Rendered as plain text: it carries exception reprs and paths, and the
        // Textual screen has to pass `markup=False` for exactly that reason.
        lines.push(Line::from(Span::styled(
            format!(
                " {}",
                self.credentials(store).unwrap_or(MISSING.to_string())
            ),
            Style::default().fg(match ok {
                true => t.text_tertiary,
                false => t.warning,
            }),
        )));
        lines.push(self.footer("Enter chooses · ↑↓ moves · Esc — synthetic · simulated"));
        lines
    }

    /// The second question: which mind answers on each surface.
    fn model_lines(&self, store: &Store) -> Vec<Line<'static>> {
        let t = theme();
        let rows = self.model_rows(store);
        let cap = self.cap();
        let mut lines = vec![panel_header("which minds"), section("per surface")];
        if self.top > 0 {
            lines.push(marker(format!(" ▴ {} above", self.top)));
        }
        for (i, row) in rows.iter().enumerate().skip(self.top).take(cap) {
            match row {
                ModelRow::Offer {
                    surface,
                    value,
                    current,
                    refusal,
                    ..
                } => lines.push(self.row(
                    i,
                    &format!("{surface:<10}{value}"),
                    current.then_some(RUNNING),
                    refusal.is_none(),
                    refusal.as_deref(),
                )),
                ModelRow::Keep => {
                    lines.push(self.row(i, "keep what the desk is using", None, true, None))
                }
            }
        }
        let hidden = rows.len().saturating_sub(self.top + cap);
        if hidden > 0 {
            lines.push(marker(format!(" ▾ {hidden} more")));
        }
        // Stated rather than offered. Pointing the reasoner at a model does not
        // switch it on — the owner refuses to infer one from the other — so a
        // door that said nothing here would leave an operator with a choice
        // that changed nothing, which is the shape this client refuses
        // everywhere else. The switch itself stays on the line that owns it.
        lines.push(Line::from(Span::styled(
            match store.llm().and_then(|llm| llm.reasoner_enabled) {
                Some(true) => " judgment on — the reasoner uses the model chosen here",
                Some(false) => " judgment off — /model reasoner on is what puts a choice to work",
                None => " the owner did not say whether the reasoner is switched on",
            },
            Style::default().fg(t.text_dim),
        )));
        lines.push(self.footer("Enter chooses · ↑↓ moves · Esc keeps them as they are"));
        lines
    }

    /// The door a window that cannot choose is shown.
    ///
    /// A statement, never an affordance: the rows above are absent rather than
    /// disabled, because a greyed row says "this client could do that if you
    /// asked" — which is the claim the posture exists to make impossible.
    fn glass_lines(&self, store: &Store) -> Vec<Line<'static>> {
        let t = theme();
        let mode = store.desk_mode();
        vec![
            panel_header("this desk"),
            Line::from(Span::styled(
                match mode {
                    Some(mode) => format!(
                        " The owner reports {}.",
                        format::text(mode.label.as_ref()).unwrap_or(MISSING)
                    ),
                    None => " The owner answered and did not say which desk this is.".to_string(),
                },
                Style::default().fg(t.text_primary),
            )),
            Line::from(""),
            // Hand-broken rather than wrapped: at this width a `Paragraph`
            // breaks mid-sentence into an unindented continuation, and the two
            // flags are the half an operator acts on.
            Line::from(Span::styled(
                " This window is GLASS — it watches the desk and points it nowhere.",
                Style::default().fg(t.text_secondary),
            )),
            Line::from(Span::styled(
                " --operator arms a window to choose the data, the book and the",
                Style::default().fg(t.text_secondary),
            )),
            Line::from(Span::styled(
                " models; --pick asks again on a desk that has already answered.",
                Style::default().fg(t.text_secondary),
            )),
            Line::from(""),
            Line::from(Span::styled(
                " Any key dismisses this.",
                Style::default().fg(t.text_dim),
            )),
        ]
    }

    /// One row: the cursor, the label, a marker, and why it cannot be chosen.
    ///
    /// **The marker's word is the caller's, because the two steps mark two
    /// different facts.** The first question marks what this door would
    /// *apply* — which starts as what the desk reports and moves with the
    /// operator — and the second marks what each surface is running *now*,
    /// because a model choice there is sent the moment it is made. Writing
    /// `now` on both was the first thing a pty run caught: on a desk whose
    /// owner had said nothing at all, the fallback the door has to pick made
    /// the synthetic row claim the desk was already on it.
    fn row(
        &self,
        i: usize,
        label: &str,
        marker: Option<&str>,
        choosable: bool,
        said: Option<&str>,
    ) -> Line<'static> {
        let t = theme();
        let on = i == self.at;
        let mut spans = vec![
            // A glyph and not only a colour: on a 256-colour terminal the
            // highlight is a shade, and a shade is not an answer to "which row
            // am I about to choose".
            Span::styled(
                if on { " ▸ " } else { "   " },
                Style::default().fg(t.accent),
            ),
            Span::styled(
                label.to_string(),
                match (choosable, on) {
                    (false, _) => Style::default().fg(t.text_dim),
                    (true, true) => Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
                    (true, false) => Style::default().fg(t.text_primary),
                },
            ),
        ];
        if let Some(marker) = marker {
            spans.push(Span::styled(
                format!("  {marker}"),
                Style::default().fg(t.positive).add_modifier(Modifier::BOLD),
            ));
        }
        if let Some(said) = said {
            spans.push(Span::styled(
                format!("  — {}", format::bounded(said, SAID_MAX)),
                Style::default().fg(t.text_dim),
            ));
        }
        Line::from(spans)
    }

    /// The keys, and whatever the door last said back.
    ///
    /// The note outranks the key list, exactly as the command line's does: an
    /// operator who has just been refused needs the reason, not a reminder of
    /// which arrow moves.
    fn footer(&self, keys: &str) -> Line<'static> {
        let t = theme();
        match &self.note {
            Some(note) => Line::from(Span::styled(
                format!(" {}", format::bounded(note, SAID_MAX)),
                Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
            )),
            None => Line::from(Span::styled(
                format!(" {keys}"),
                Style::default().fg(t.text_dim),
            )),
        }
    }
}

/// One row of the first question.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ModeRow {
    Data(&'static str),
    Book(&'static str),
    /// Move on to the models. The pair is applied when the door finishes, never
    /// here — see [`Door::finish`].
    Next,
}

/// One row of the second.
#[derive(Debug, Clone, PartialEq, Eq)]
enum ModelRow {
    Offer {
        surface: &'static str,
        value: String,
        /// Whether this is what the surface runs now.
        current: bool,
        /// The owner's sentence about why it cannot be chosen.
        refusal: Option<String>,
        choice: Option<ModelChoice>,
    },
    /// Leave every surface as the desk has it.
    Keep,
}

/// Whether one offer is what a surface is already pointed at.
fn is_current(store: &Store, surface: &str, offer: &Offer) -> bool {
    let Some(ModelChoice::Pair { backend, model }) = offer.choice() else {
        return false;
    };
    let Some(llm) = store.llm() else {
        return false;
    };
    let held = match surface {
        cmd::WORKFORCE => llm.workforce.as_ref(),
        _ => llm.reasoner.as_ref(),
    };
    let Some(held) = held else {
        return false;
    };
    // The owner's own spelling on both sides — this compares two answers it
    // gave, not an answer against something typed here.
    format::text(held.backend.as_ref()) == Some(backend.as_str())
        && format::text(held.model.as_ref()) == Some(model.as_str())
}

/// A desk mode word the owner sent, matched against the two this client knows.
///
/// Cased on both sides, and `None` for anything else: a desk pointed at a word
/// this client cannot read is one it may not silently render as the other.
fn word(
    store: &Store,
    of: impl Fn(&DeskMode) -> Option<&String>,
    known: [&'static str; 2],
) -> Option<&'static str> {
    let mode = store.desk_mode()?;
    let said = format::text(of(mode))?;
    known
        .into_iter()
        .find(|word| word.eq_ignore_ascii_case(said))
}

/// A dim heading inside the box.
fn section(title: &str) -> Line<'static> {
    Line::from(Span::styled(
        format!(" {title}"),
        Style::default().fg(theme().text_secondary),
    ))
}

/// The count of rows the box had no room for.
fn marker(said: String) -> Line<'static> {
    Line::from(Span::styled(said, Style::default().fg(theme().text_dim)))
}

/// The pair, in the build that has somewhere to send it.
///
/// Unreachable in the default build: `posture.writes()` is false for every
/// `Posture` it has, so `on_key` dismissed the door before anything could get
/// here. The arm exists because the walk is one walk in both builds.
fn pointed(data: &str, book: &str) -> Option<Command> {
    #[cfg(feature = "operator")]
    return Some(Command::DeskMode {
        data: data.to_string(),
        book: book.to_string(),
    });
    #[cfg(not(feature = "operator"))]
    {
        let _ = (data, book);
        None
    }
}

/// The model choice, in the build that has somewhere to send it.
fn chose(surface: &str, choice: ModelChoice) -> Option<Command> {
    #[cfg(feature = "operator")]
    return Some(Command::SetLlm {
        surface: surface.to_string(),
        choice,
    });
    #[cfg(not(feature = "operator"))]
    {
        let _ = (surface, choice);
        None
    }
}

/// How many rows the box needs for what it is about to draw, borders included.
///
/// Rounds up, the way SETTINGS' login form does and for the same reason:
/// `Paragraph` wraps at word boundaries, so a line one cell past the width can
/// take a whole row more than the division says.
fn wanted(lines: &[Line<'static>], inner_w: u16) -> u16 {
    let width = inner_w.max(1) as usize;
    let rows: usize = lines
        .iter()
        .map(|line| line.width().div_ceil(width).max(1))
        .sum();
    let wrapped = lines.iter().filter(|line| line.width() > width).count();
    (rows + wrapped) as u16 + 2
}

/// The box's rect: centred, and never larger than the frame it is drawn over.
fn centred(area: Rect, w: u16, h: u16) -> Rect {
    let h = h.min(area.height.saturating_sub(2)).max(1);
    Rect {
        x: area.x + (area.width.saturating_sub(w)) / 2,
        y: area.y + (area.height.saturating_sub(h)) / 2,
        width: w,
        height: h,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bus::AppEvent;
    use std::time::Instant;

    /// A desk whose owner answered with the `desk_mode` block it always sends.
    fn desk(data: &str, book: &str, credentials_ok: bool) -> Store {
        let mut store = Store::default();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({
                    "desk_mode": {
                        "data": data, "book": book,
                        "label": "SYNTHETIC", "offline": data == SYNTHETIC,
                        "credentials": match credentials_ok {
                            true => "ALPACA_API_KEY_ID PK…4T2C from the environment",
                            false => "no ALPACA_API_KEY_ID in the environment or .env",
                        },
                        "credentials_ok": credentials_ok,
                    },
                    "llm": {
                        "reasoner": {"backend": "claude", "model": "inherit"},
                        "workforce": {"backend": "claude", "model": "inherit"},
                        "reasoner_enabled": false,
                    }
                }))
                .unwrap(),
            )),
            Instant::now(),
        );
        store
    }

    /// The catalog the second question is built from — the live shape, with the
    /// daemon either up or refusing in the owner's own sentence.
    fn with_catalog(mut store: Store, ollama_up: bool) -> Store {
        let ollama = match ollama_up {
            true => serde_json::json!({"name": "ollama", "available": true,
                                       "reason": "ollama at 127.0.0.1:11434, 1 model pulled",
                                       "models": ["qwen2.5:7b"]}),
            false => serde_json::json!({"name": "ollama", "available": false,
                                        "reason": "ollama is not running at \
                                                   http://127.0.0.1:11499 — start it with \
                                                   `ollama serve`",
                                        "models": []}),
        };
        let catalog = serde_json::from_value(serde_json::json!({
            "backends": [
                {"name": "claude", "available": true, "reason": "claude CLI on PATH",
                 "models": ["inherit", "sonnet", "opus", "haiku"]},
                ollama
            ],
            "probed_at": "2026-08-03T04:10:08.417505+00:00"
        }))
        .unwrap();
        store.apply(AppEvent::Backends(catalog), Instant::now());
        store
    }

    #[test]
    fn a_door_opens_for_a_desk_that_answered_and_said_nothing_and_for_pick() {
        // The store-driven arm, both sides of the conjunction it is: an owner
        // that answered and said nothing is the state this door was specified
        // against, and an owner that has not answered *yet* is not — a client
        // that opened one in the second before its first poll would ask about a
        // desk that was about to name itself.
        assert!(
            Door::wanted(false, true, true),
            "answered, and said nothing"
        );
        assert!(
            !Door::wanted(false, true, false),
            "the owner named the desk"
        );
        assert!(
            !Door::wanted(false, false, true),
            "nothing has answered yet"
        );
        // And the flag, which is the arm that works on a desk that did answer —
        // the only way to ask again, because the owner cannot report that
        // nobody has chosen (see this module's header).
        assert!(Door::wanted(true, false, false));
        assert!(Door::wanted(true, true, false));
    }

    #[test]
    fn a_door_that_has_not_been_drawn_holds_no_keyboard() {
        // Zero before the first frame, which refuses: a client that has not
        // drawn cannot know it has room, and the runtime draws once before it
        // reads its first event.
        let door = Door::default();
        assert!(!door.fits());
        door.area.set(Rect::new(0, 0, DOOR_MIN_W, DOOR_MIN_H));
        assert!(door.fits());
        // Each floor on its own, so a pass cannot come from the other one.
        door.area.set(Rect::new(0, 0, DOOR_MIN_W - 1, DOOR_MIN_H));
        assert!(!door.fits(), "narrower than the floor");
        door.area.set(Rect::new(0, 0, DOOR_MIN_W, DOOR_MIN_H - 1));
        assert!(!door.fits(), "shorter than the floor");
    }

    #[test]
    fn the_book_question_is_absent_until_the_desk_is_live() {
        // The Textual door's first virtue, ported: the nonsensical pair is
        // unreachable rather than rejected, because there is no keystroke that
        // reaches the alpaca row while the door is pointing at synthetic data.
        let door = Door::default();
        let synthetic = desk(SYNTHETIC, SIMULATED, true);
        assert_eq!(
            door.mode_rows(&synthetic),
            vec![ModeRow::Data(SYNTHETIC), ModeRow::Data(LIVE), ModeRow::Next],
            "a synthetic desk was offered a book"
        );
        // Live, and the two books are disclosed. Read off the desk rather than
        // off a field the door set, which is the case that matters: a `--pick`
        // door opened before the first snapshot must show the desk that
        // arrived, not the default it started on.
        assert!(door
            .mode_rows(&desk(LIVE, ALPACA, true))
            .contains(&ModeRow::Book(ALPACA)));
    }

    /// A door already walked to one row of one step.
    ///
    /// The cursor is the door's own state and a test that reached a row by
    /// pressing Down would be pinning the walk twice — once here and once in
    /// the test that is about the walk.
    #[cfg(feature = "operator")]
    fn cursor_on(at: usize, step: Step) -> Door {
        Door {
            at,
            step,
            ..Door::default()
        }
    }

    #[cfg(feature = "operator")]
    fn armed(store: &mut Store) {
        store.posture = crate::store::Posture::Operator;
    }

    #[cfg(feature = "operator")]
    fn press(door: &mut Door, code: KeyCode, store: &mut Store) -> Option<Command> {
        door.on_key(
            KeyEvent::new(code, crossterm::event::KeyModifiers::NONE),
            store,
            &mut Views::new(),
        )
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_live_row_is_refused_in_the_owners_own_words_until_it_can_read_a_login() {
        // The gate is on the *choice*: this is the one row that can newly point
        // a desk at a real account, and `Some(true)` is the only answer that
        // opens it — an owner that did not say is not an owner that said yes.
        let mut store = desk(SYNTHETIC, SIMULATED, false);
        armed(&mut store);
        let mut door = cursor_on(1, Step::Mode);
        assert_eq!(press(&mut door, KeyCode::Enter, &mut store), None);
        assert_eq!(door.data(&store), SYNTHETIC, "a broken login went live");
        assert_eq!(
            door.note.as_deref(),
            Some("no ALPACA_API_KEY_ID in the environment or .env"),
            "the refusal is the owner's description, not a sentence composed here"
        );
        // And with a login the desk can read, the same keystroke discloses the
        // book question. Both sides of the guard, because one case that merely
        // reaches the code proves nothing about the comparison.
        let mut ok = desk(SYNTHETIC, SIMULATED, true);
        armed(&mut ok);
        let mut door = cursor_on(1, Step::Mode);
        press(&mut door, KeyCode::Enter, &mut ok);
        assert_eq!(door.data(&ok), LIVE);
        assert!(door.mode_rows(&ok).contains(&ModeRow::Book(ALPACA)));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn escape_is_the_safe_desk_and_never_a_live_one() {
        // The Textual door's second virtue. Whatever the cursor was on and
        // whatever the desk already is, the key a human presses to get out of
        // the way applies `synthetic · simulated` — and it is *applied*, not
        // skipped, because the POST is what makes the answer durable.
        let mut store = desk(LIVE, ALPACA, true);
        armed(&mut store);
        let mut door = cursor_on(3, Step::Mode);
        assert_eq!(
            press(&mut door, KeyCode::Esc, &mut store),
            Some(Command::DeskMode {
                data: SYNTHETIC.to_string(),
                book: SIMULATED.to_string()
            })
        );
        assert!(!door.standing(), "Esc left the door up");
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_second_question_asks_for_the_catalog_on_the_way_in_and_opens_on_what_the_desk_runs() {
        let mut store = with_catalog(desk(SYNTHETIC, SIMULATED, true), true);
        armed(&mut store);
        let mut door = cursor_on(2, Step::Mode);
        assert_eq!(
            press(&mut door, KeyCode::Enter, &mut store),
            Some(Command::Backends),
            "the catalog is asked for on the transition, not on a beat"
        );
        assert_eq!(door.step(), Step::Model);
        // The default is the current config: this desk runs claude · inherit on
        // the reasoner, which is the first row.
        let rows = door.model_rows(&store);
        assert!(
            matches!(&rows[door.at], ModelRow::Offer { surface, value, current: true, .. }
                     if *surface == "reasoner" && value == "claude:inherit"),
            "{:?}",
            rows.get(door.at)
        );
    }

    #[test]
    fn the_second_question_inherits_the_lines_offer_rules_and_writes_none_of_its_own() {
        // Built by `cmd::offers`, so the honesty rules hold here because they
        // hold there: the workforce is offered `claude` alone (the tier map
        // owns its model), and a daemon the desk cannot reach stays on the list
        // with the owner's own sentence and no choice behind it.
        let store = with_catalog(desk(SYNTHETIC, SIMULATED, true), false);
        let rows = Door::default().model_rows(&store);
        let workforce: Vec<&str> = rows
            .iter()
            .filter_map(|row| match row {
                ModelRow::Offer {
                    surface: "workforce",
                    value,
                    ..
                } => Some(value.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(workforce, vec!["claude", "ollama"]);
        let down = rows
            .iter()
            .find(|row| matches!(row, ModelRow::Offer { value, .. } if value == "ollama"))
            .unwrap();
        match down {
            ModelRow::Offer {
                refusal: Some(said),
                choice,
                ..
            } => {
                assert!(said.contains("ollama serve"), "{said}");
                assert!(choice.is_none(), "a down daemon was choosable");
            }
            other => panic!("{other:?}"),
        }
        // The last row is always the skip, so a walk that recognises nothing on
        // the list still has an answer that changes nothing.
        assert_eq!(rows.last(), Some(&ModelRow::Keep));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn only_a_desk_that_cannot_reach_the_alpaca_book_is_sent_to_the_login() {
        // The third step's whole condition, on both sides of it. The book the
        // door settled on is the real one and the owner cannot read a login for
        // it — anything else lands on the desk.
        for (data, book, ok, wanted) in [
            (LIVE, ALPACA, false, ViewId::Settings),
            (LIVE, ALPACA, true, ViewId::Desk),
            (LIVE, SIMULATED, false, ViewId::Desk),
            (SYNTHETIC, SIMULATED, false, ViewId::Desk),
        ] {
            let mut store = desk(data, book, ok);
            armed(&mut store);
            let mut views = Views::new();
            let mut door = cursor_on(0, Step::Model);
            door.at = door.model_rows(&store).len() - 1;
            let acted = door.on_key(
                KeyEvent::new(KeyCode::Enter, crossterm::event::KeyModifiers::NONE),
                &mut store,
                &mut views,
            );
            assert_eq!(
                acted,
                Some(Command::DeskMode {
                    data: data.to_string(),
                    book: book.to_string()
                }),
                "the pair is applied on the way out whatever happens next"
            );
            assert_eq!(
                store.nav.view, wanted,
                "{data} · {book}, login readable: {ok}"
            );
            assert!(!door.standing());
        }
    }
}
