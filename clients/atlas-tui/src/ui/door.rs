//! The startup door: whether this window may write, which desk, which minds, and the login those may need.
//!
//! Four steps over one modal, drawn over the shell the way a confirmation box
//! is and claiming the keyboard above everything but Ctrl-C. It is the flow the
//! Textual client has had since the beginning (`qlab/tui/desk_mode_screen.py`),
//! and the two virtues ported from it are the ones that are easy to lose:
//! **two-step disclosure**, so the `synthetic`/`alpaca` pair is unreachable by
//! construction rather than rejected by a message, and **Esc is
//! `synthetic · simulated`**, so the key a human presses to get out of the way
//! can never leave a desk pointed at a real venue. A third rule of that screen
//! is deliberately **not** ported, and the section below says why.
//!
//! ## What the owner says, and what it used to be unable to
//!
//! The trigger this door was specified against is "the desk has not been
//! chosen", and for one round **the owner could not report that**:
//! `UISession.__init__` resolved `desk_mode or load_desk_mode() or
//! DEFAULT_DESK_MODE` and served a concrete `data`/`book`/`label` either way,
//! so a desk nobody had ever chosen looked byte for byte like one chosen to be
//! `synthetic · simulated`. The store-driven arm was named for the only thing
//! it could actually observe — the owner **answered** and said **nothing** —
//! and `--pick` carried the feature.
//!
//! `desk_mode_payload()` now serves `chosen: bool`, computed where the
//! three-way `or` resolves and set again by `set_desk_mode`, so the arm means
//! what it was meant to mean. `Store::desk_unchosen` is where the two shapes of
//! "nobody chose this" are spelled out, including the one that is neither: an
//! owner too old to carry the field is silent, not negative, and its silence
//! keeps the reading it had before the field existed.
//!
//! The mind has the same flag now, and for the same reason: `llm_payload()`
//! serves `chosen: bool`, false only where no config file was ever written. It
//! is a second question and not a restatement of the first — a desk that named
//! its pair a year ago has still never been asked which mind runs Atlas — so
//! the walk opens on **the first question nobody has answered** rather than
//! always at the top. Before that, a chosen desk mode retired the whole door,
//! and the model question was unreachable on every desk that had one.
//!
//! The same fact drives the first question's marker. A row is `chosen` only
//! when something named that half — the operator with a keystroke, or an owner
//! that says so — and `assumed` otherwise; before the flag, a fallback the
//! owner had to invent was marked as though somebody had picked it, on the one
//! screen whose whole subject is that nobody has.
//!
//! ## The question in front of the other three
//!
//! The desk's posture is the owner's fact and is asked here first, because
//! the other three questions *are* writes: the pair and the models both reach
//! the owner through the dispatch seam, which refuses every command from a
//! window the desk has not armed. Asking them of an unarmed window would be
//! offering rows whose answers are refused one layer down.
//!
//! It is deliberately not the placement the plan sketched — after the models,
//! since arming is about *this client* and the credential is about the book —
//! and the reason is that ordering cannot exist without a client-side latch:
//! a keystroke carries one `Command`, the arming answer only takes effect
//! through the owner's next snapshot, and a door that arrived at the arming
//! question last would have spent the two questions before it emitting writes
//! nothing could accept.
//!
//! Answered by the *owner*, never here. `Store::asking_posture` reads the
//! question live off `posture.chosen`, so the keystroke that sends the answer
//! does not retire it — a door that closed on its own key would be this
//! client deciding it is armed, which is exactly the latch `Posture::from_desk`
//! exists to make impossible. Esc is `read-only`, the same rule that makes Esc
//! `synthetic · simulated` one question later.
//!
//! One consequence of that, stated because it is not obvious from the walk: a
//! `read-only` answer sets `closed`, so a desk that has *never chosen a desk
//! mode* and answers read-only is never asked the mode question at all. That is
//! the same behaviour a `--glass` window already gets — a window that cannot
//! write is shown a statement rather than three questions whose answers would be
//! refused — and the desk keeps serving whatever pair it was already serving
//! until somebody arms a window or uses `/mode`.
//!
//! ## The one rule that did not survive the port
//!
//! The Textual screen disables LIVE outright without a credential. This door
//! does not, and the reason is that the gate was never authority.
//! `set_desk_mode` validates the *shape* of the pair and nothing else;
//! `live · alpaca` on dead credentials is already reachable from `/mode` and
//! from the web client; and a desk pointed at a book it cannot reach fails loud
//! on every broker call and can produce no fill. What the gate bought was
//! honesty, and it bought it by removing a choice — which also removed the only
//! walk that reaches the login this client now has, since a book nobody can
//! choose is a book nobody is offered a login for.
//!
//! So the choice stays and the honesty moves to where it belongs: the row
//! carries the owner's own account of the credential, the outcome toast carries
//! its warning (`dispatch::credential_warning`, already the rule for every
//! desk-mode switch), and the third step offers the login. The virtue's core is
//! untouched — a live desk is never a *default* here and never a silent one.
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

use crate::cmd::{self, Command, ModelChoice};
use crate::format::{self, MISSING};
use crate::model::DeskMode;
use crate::store::{Store, Unchosen, ViewId};
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

/// The box's width, and it is set by one row.
///
/// **`ALPACA PAPER  chosen  — the paper account; a fill still needs you` is 68
/// cells, which is exactly the inner width this constant leaves.** Zero margin:
/// one more cell in that label, that marker or that sentence wraps it onto an
/// unindented second line, and the row that says a fill still needs a human is
/// the last one this box may lose half of. `tests` pins the equality and
/// `golden_door` renders the state, because a line-level pin alone never
/// reached it — the door opens on the synthetic rows, and the alpaca row is two
/// keystrokes further in.
///
/// It is not the row that moved this from 68: the *synthetic* one did, at 67
/// cells the moment [`ASSUMED`] became the honest word for a desk nobody had
/// chosen (one cell wider than [`CHOSEN`], which is what it had been marked
/// with). Both facts are here because they are two different rows and only one
/// of them binds today.
const DOOR_W: u16 = 70;

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

/// What the first question's marker says when something has actually named
/// this half of the pair — the owner in its payload, or the operator with a
/// keystroke.
const CHOSEN: &str = "chosen";

/// And when nothing has. The door still has to point somewhere, so it falls
/// back to the safe desk; marking that `chosen` claimed a decision nobody had
/// made, on the one screen whose whole subject is that nobody has. It is the
/// armed door's half of the sentence the read-only one says outright.
const ASSUMED: &str = "assumed";

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

/// The mind the door reserves and will not run, by the name an operator knows
/// it by.
///
/// **Named, not built** (design ruling). Omitting it would read as a desk that
/// had never heard of it, and an operator who came to this box to point Atlas
/// at Codex would leave without an answer; offering it would be this client
/// claiming a backend the owner has no id, launcher or MCP wiring for. So it is
/// on the list and refuses, which is the same shape a daemon the desk cannot
/// reach already has one row up.
const CODEX: &str = "codex";

/// What the row says, and what it says when it is chosen.
///
/// Two lengths for two places: [`DOOR_W`] leaves 68 cells for a row and 112 for
/// a footer note ([`SAID_MAX`]), and the whole reason is worth more than the
/// row has room for. Neither is the owner's sentence — there is no backend on
/// the wire to have one — so this client owns the words, which it may do
/// precisely because it is refusing rather than reporting.
const CODEX_ROW: &str = "not built: a backend, a launcher and MCP wiring";
const CODEX_REFUSAL: &str =
    "codex is named, not built: it needs its own backend id, a launcher and MCP \
     wiring in ~/.codex/config.toml";

/// The row that fetches the catalog the model question is built from.
///
/// It exists because of where the catalog comes from: nothing polls it, and the
/// one thing that asks is a keystroke ([`Command::Backends`], sent by the
/// `models →` row on the way in). A door that OPENED on the model question
/// crossed no such transition and `Store::consider_door` has nowhere to send a
/// command, so the ask becomes the first row — one keystroke, visible, and the
/// only thing on this question an operator could act on before the desk has
/// said what it can run.
const ASK: &str = "ask what this desk can run";
const ASK_SAID: &str = "nothing has asked yet";

/// Which question the door is on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    /// May a window like this one write to this desk at all?
    ///
    /// First, and not by taste. The other two questions *are* writes — the
    /// pair and the models both reach the owner through the dispatch seam —
    /// so on a desk nobody has armed they are two questions this window may
    /// not answer. Asking them first would be offering rows whose answers the
    /// chokepoint refuses.
    Posture,
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
    /// The question this door was opened *for* — the first one nobody had
    /// answered ([`Door::wanted`]).
    ///
    /// Held rather than re-derived, because `step` moves with the walk and this
    /// does not, and two things read it: a door opened only about the mind
    /// writes only the mind (it never put the pair to anybody), and its answer
    /// is the last thing it was waiting for, so answering retires it. Deriving
    /// it live would flip the moment the owner's next snapshot said the mind
    /// was chosen — which is the snapshot that arrives *because* the operator
    /// just answered.
    opened: Step,
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
            opened: Step::Mode,
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
    /// Whether a desk opening now needs a door, and which question it opens on.
    ///
    /// Two arms, and they answer different questions. `forced` is `--pick`: the
    /// operator started this run to choose, so the door opens whatever the desk
    /// says — and opens at the top, because what they asked for is the walk and
    /// not whichever half of it the desk happens not to have answered. The
    /// other is the store-driven one, and it means what it was specified to
    /// mean: the owner **answered** and something here is a thing nobody has
    /// chosen. [`Unchosen`] owns which payloads count, because that is a fact
    /// about the wire rather than about this box.
    ///
    /// **The step is the first question nobody has answered**, and that is the
    /// whole of what this task changed. Keying the walk on the desk *mode's*
    /// flag alone meant a desk whose pair was named long ago could never be
    /// asked about its mind at all: there was no state in which this returned
    /// true, and if there had been, the question would have opened two Enters
    /// away from the one that was actually open.
    ///
    /// `answered` is required on the second arm and not on the first. Without
    /// it every client would open a door in the second before its first poll
    /// lands, on a desk that was about to say exactly which one it is.
    pub fn wanted(forced: bool, answered: bool, unchosen: Unchosen) -> Option<Step> {
        if forced {
            return Some(Step::Mode);
        }
        if !answered || !unchosen.any() {
            return None;
        }
        Some(match unchosen.desk {
            true => Step::Mode,
            false => Step::Model,
        })
    }

    /// A door opening on one question.
    ///
    /// The cursor stays on the first row rather than on what the desk runs, and
    /// that is not a shortcut: the catalog the model question is built from is
    /// fetched by a keystroke and nothing has pressed one yet, so at the moment
    /// a door opens there is no list to place a cursor against. The first row of
    /// that list is [`ASK`], which is the one that fetches it, and
    /// [`Door::settle_model_cursor`] places the cursor properly when the answer
    /// lands.
    pub fn opening(step: Step) -> Self {
        Self {
            step,
            opened: step,
            ..Self::default()
        }
    }

    /// Re-open the model cursor on what the desk runs, now that the desk has
    /// said what it can run.
    ///
    /// Called by the store on the *first* catalog only. Two doors need it and
    /// for one reason: neither had a catalog at the moment its cursor was set —
    /// the walked-in one because `models →` asks for the catalog and advances in
    /// the same keystroke, the opened-on-it one because nothing had asked at
    /// all. A cursor left where it was would sit on whichever pair the catalog
    /// happens to list first, where Enter is a change nobody asked for, which is
    /// exactly what [`Door::current_model`] exists to prevent.
    pub fn settle_model_cursor(&mut self, store: &Store) {
        if self.step != Step::Model {
            return;
        }
        self.at = self.current_model(store);
        self.top = 0;
        self.scroll();
    }

    /// Which question is up. Public so a test can pin the walk rather than
    /// infer it from what was drawn.
    /// **Derived, not held.** The arming question is up for exactly as long as
    /// the owner has not said somebody answered it ([`Store::asking_posture`]),
    /// which is what keeps this client from latching an authority it has only
    /// requested: the keystroke that sends the answer does not retire the
    /// question, the owner's next snapshot does. `self.step` is where the walk
    /// is once that question is behind it.
    pub fn step(&self, store: &Store) -> Step {
        match store.asking_posture() {
            true => Step::Posture,
            false => self.step,
        }
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

    /// Whether anything has named the data source: the operator with a
    /// keystroke, or the owner in a payload this client can read. A word the
    /// owner sent that is neither of the two this client knows names nothing —
    /// `word` already refuses it, and so does this.
    ///
    /// The owner's half is two facts, not one: the word has to be there **and**
    /// the owner must not be saying that nobody chose it. A concrete
    /// `synthetic · simulated` with `chosen: false` is the fallback it has to
    /// serve when the state file is empty, and marking that `chosen` claimed a
    /// decision nobody had made.
    fn named_data(&self, store: &Store) -> bool {
        self.data.is_some()
            || (owner_chose(store)
                && word(store, |mode| mode.data.as_ref(), [SYNTHETIC, LIVE]).is_some())
    }

    /// The same question about the book, asked separately because the two
    /// halves are answered separately: choosing `live` names the data and
    /// leaves the book still assumed.
    fn named_book(&self, store: &Store) -> bool {
        self.book.is_some()
            || (owner_chose(store)
                && word(store, |mode| mode.book.as_ref(), [SIMULATED, ALPACA]).is_some())
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
        // First, and only on a door that opened here: the walked-in door asked
        // for the catalog with the keystroke that got it here, so a row asking
        // again would be an affordance for something already in flight — and it
        // would appear for the one frame between the two, taking the cursor with
        // it. See [`ASK`] for why the row exists at all.
        if self.opened == Step::Model && store.backends().is_none() {
            rows.push(ModelRow::Ask);
        }
        for surface in cmd::SURFACES {
            for offer in cmd::offers(surface, store) {
                rows.push(ModelRow::Offer {
                    surface,
                    current: offer.running(store, surface),
                    value: offer.value().to_string(),
                    refusal: offer.refusal().map(str::to_string),
                    choice: offer.choice(),
                });
            }
        }
        // Under the surfaces it would answer on if it existed, and above the
        // skip, because it is a mind and not a way out of the question.
        rows.push(ModelRow::Codex);
        rows.push(ModelRow::Keep);
        rows
    }

    /// How many rows the cursor may walk on this step.
    fn len(&self, store: &Store) -> usize {
        match self.step(store) {
            Step::Posture => POSTURE_ROWS.len(),
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
        // Two conjuncts, because there are now two kinds of window that cannot
        // write. One the desk has not *asked* yet is being asked here and its
        // keys mean something; one that can never be armed — a read-only
        // artifact, a `--glass` window, a desk deliberately left read-only — is
        // shown a statement, and any key retires it exactly as the help overlay
        // does.
        if !store.posture.writes() && !store.asking_posture() {
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
        match self.step(store) {
            // The answer goes to the owner and nothing here changes: the door
            // stays up on this question until a snapshot says the desk
            // recorded one. A step that advanced on its own keystroke would be
            // this client deciding it is armed, which is the latch
            // `Posture::from_desk` exists to make impossible.
            //
            // Except on `read-only`, which is an operator saying no to the
            // whole workstation — there is nothing further to ask, so it is
            // the end of the door as much as Esc is.
            Step::Posture => {
                let armed = *POSTURE_ROWS.get(self.at)?;
                if !armed {
                    self.closed = true;
                }
                self.at = 0;
                posture(armed)
            }
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
                    // The row that can point this desk at a real account, and
                    // deliberately ungated — see this module's header. The
                    // credential is stated on the row and warned about in the
                    // outcome; it is not a permission this door holds, and
                    // pretending otherwise cost the operator the one walk that
                    // ends at a login form.
                    ModeRow::Data(LIVE) => self.data = Some(LIVE),
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
                // The one row here that asks the owner something instead of
                // telling it something. It is not an answer, so the door stays
                // up — and the row goes as soon as the catalog lands.
                ModelRow::Ask => Some(Command::Backends),
                // Refused in this client's own words, unlike every other
                // refusal on this list: there is no backend on the wire to have
                // given one. Saying so is what makes it a reservation rather
                // than a gap.
                ModelRow::Codex => {
                    self.note = Some(CODEX_REFUSAL.to_string());
                    None
                }
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
                } => {
                    // A door opened only about the mind has just been answered,
                    // and a question that has been answered is not one to keep
                    // asking — the rule the arming step is derived by. The
                    // walked-in door stays up, because there the pair is still
                    // waiting to be applied on the way out and the *other*
                    // surface is still an open row.
                    if self.opened == Step::Model {
                        self.closed = true;
                    }
                    chose(surface, choice.clone())
                }
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
    /// the pair from the first question is applied on the way out. On a door
    /// that *opened* on the second, it is the same key with nothing left to
    /// skip — there is no pair waiting, so it means what the last row means,
    /// and [`Door::finish`] writes the mind the desk already runs for the
    /// reason it writes the pair.
    fn escape(&mut self, store: &mut Store, views: &mut Views) -> Option<Command> {
        // The same rule one question earlier, and the safe answer there is
        // read-only: a human pressing Escape to get out of the way must never
        // be what arms a workstation. It is *sent* rather than skipped, for
        // the reason the pair is — the POST is what makes the answer durable,
        // and a door that skipped it would leave the desk as unasked as it
        // found it and open again on the next run.
        if self.step(store) == Step::Posture {
            self.closed = true;
            return posture(false);
        }
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
    /// desk already reads: the POST is what makes the answer durable, so a door
    /// that skipped the write on an unchanged pair would leave the desk exactly
    /// as unchosen as it found it and open again on the next run. (The owner
    /// can now *say* whether anybody chose — that flag is what opens this door
    /// — but saying so is not the same as somebody having answered the question
    /// it is up for.)
    ///
    /// That rule is why a door opened on the model question writes the mind the
    /// desk already runs rather than nothing at all, and why it writes no pair:
    /// it never put the pair to anybody.
    fn finish(&mut self, store: &mut Store, views: &mut Views) -> Option<Command> {
        self.closed = true;
        // A door that never asked about the pair does not write one: the desk
        // named its own long ago, and a POST of it here would be this box
        // claiming a decision it put to nobody. What it writes instead is the
        // answer it *did* take, by the rule two paragraphs up — the mind the
        // desk is already running, sent so that "keep" is durable.
        if self.opened == Step::Model {
            return kept(store);
        }
        let (data, book) = (self.data(store), self.book(store));
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
    ///
    /// [`ASK`] outranks both when it is there, because it is there exactly when
    /// there is no list: a cursor parked on the skip of a question with no
    /// answers on it would hide the one row that can produce them.
    fn current_model(&self, store: &Store) -> usize {
        let rows = self.model_rows(store);
        rows.iter()
            .position(|row| matches!(row, ModelRow::Ask))
            .or_else(|| {
                rows.iter()
                    .position(|row| matches!(row, ModelRow::Offer { current: true, .. }))
            })
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
            // Three rows, like the help overlay's own refusal and for the
            // reason it states: a refusal has to survive its own floor, and at
            // 120 cells this sentence takes two — a single row would clip the
            // half that names the two scopes asking the same questions, which
            // is the only part an operator can act on.
            let row = Rect {
                x: area.x,
                y: area.y + area.height / 2,
                width: area.width,
                height: 3.min(area.height),
            };
            f.render_widget(Clear, row);
            refuse(
                f,
                row,
                format!(
                    "the startup door needs {DOOR_MIN_H} rows and {DOOR_MIN_W} columns; \
                     this terminal has {} rows and {} columns. Any key dismisses it — \
                     /mode and /model ask the same questions.",
                    area.height, area.width
                ),
            );
            return;
        }
        let t = theme();
        let w = DOOR_W.min(area.width.saturating_sub(4)).max(3);
        let lines = match self.step(store) {
            // A question this window may answer whatever its posture is — it
            // is the question about that posture.
            Step::Posture => self.posture_lines(),
            Step::Mode if store.posture.writes() => self.mode_lines(store),
            Step::Model if store.posture.writes() => self.model_lines(store),
            // Everything else a window that cannot write is shown: a
            // statement, never an affordance.
            _ => self.glass_lines(store),
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

    /// The arming question: two answers, and what each one means in a
    /// sentence.
    ///
    /// It takes no `Store`, which is the point: what this window may do is not
    /// a fact about the desk it is watching, and a row toned by the owner's
    /// current posture would be describing the state the operator is being
    /// asked to leave.
    fn posture_lines(&self) -> Vec<Line<'static>> {
        let t = theme();
        let mut lines = vec![panel_header("arm this desk"), section("which posture")];
        lines.push(self.row(
            0,
            &format!("{:<10}", "ARMED"),
            None,
            true,
            // 51 cells, which is exactly what [`DOOR_W`] leaves beside a
            // ten-cell label: one more wraps the row onto an unindented
            // second line, as the alpaca row's constant says of its own.
            Some("approvals, /mode, and a fill behind the confirm box"),
        ));
        lines.push(self.row(
            1,
            &format!("{:<10}", "READ-ONLY"),
            None,
            true,
            Some("the same desk, and this window writes nothing to it"),
        ));
        lines.push(Line::from(Span::styled(
            " The owner remembers the answer; a fill still needs you either way.",
            Style::default().fg(t.text_dim),
        )));
        lines.push(self.footer("Enter chooses · ↑↓ moves · Esc — read-only"));
        lines
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
                    (!live).then_some(mark(self.named_data(store))),
                    true,
                    Some("prices this desk makes; no order leaves it"),
                )),
                // Choosable whatever the login says, and never silent about
                // it: the note is a fact on a row an operator may take, not a
                // refusal of one they may not. The tone tells them apart — a
                // row the desk cannot serve is dim, and this one is not.
                ModeRow::Data(LIVE) => lines.push(self.row(
                    i,
                    "LIVE",
                    live.then_some(mark(self.named_data(store))),
                    true,
                    (!ok).then_some("no Alpaca login the desk can read"),
                )),
                ModeRow::Data(_) => {}
                ModeRow::Book(SIMULATED) => {
                    lines.push(section("which book"));
                    lines.push(self.row(
                        i,
                        "SIMULATED",
                        (self.book(store) == SIMULATED).then_some(mark(self.named_book(store))),
                        true,
                        Some("real prices; no order ever sent to Alpaca"),
                    ));
                }
                ModeRow::Book(_) => lines.push(self.row(
                    i,
                    "ALPACA PAPER",
                    (self.book(store) == ALPACA).then_some(mark(self.named_book(store))),
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
        // What the desk will read *about* those prices, and where to change
        // it. Display only: the door decides the pair and the posture and
        // nothing else, and this line is here because the lane above is the
        // question an operator is answering when they most want to know what
        // the answer implies.
        lines.push(Line::from(Span::styled(
            news_line(store),
            Style::default().fg(t.text_dim),
        )));
        lines.push(self.footer("Enter chooses · ↑↓ moves · Esc — synthetic · simulated"));
        lines
    }

    /// The second question: which mind answers on each surface.
    fn model_lines(&self, store: &Store) -> Vec<Line<'static>> {
        let t = theme();
        let rows = self.model_rows(store);
        let cap = self.cap();
        // What the list is *of*, and it is only "per surface" once the desk has
        // said what it can run. On the frame a door that opened here draws,
        // there is no surface row at all — the rows are the ask and the two
        // this client owns — and heading them "per surface" would caption a
        // list that is not there.
        let of = match rows.iter().any(|row| matches!(row, ModelRow::Offer { .. })) {
            true => "per surface",
            false => "the desk has not said what it can run",
        };
        let mut lines = vec![panel_header("which minds"), section(of)];
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
                ModelRow::Ask => lines.push(self.row(i, ASK, None, true, Some(ASK_SAID))),
                // Dim and unchoosable, which is the tone a row the desk cannot
                // serve already carries one line up — the operator learns what
                // this row is from the same signal, not from a new one.
                ModelRow::Codex => lines.push(self.row(i, CODEX, None, false, Some(CODEX_ROW))),
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
            // Three sentences for the three states, because the middle one is
            // what the owner's flag made observable and is the reason a window
            // that named its desk can still be looking at this box: a label
            // alone would read as a settled desk this door had opened over for
            // no reason.
            Line::from(Span::styled(
                match mode {
                    Some(mode) if !owner_chose(store) => format!(
                        " The owner reports {}, and says nobody has chosen it.",
                        format::text(mode.label.as_ref()).unwrap_or(MISSING)
                    ),
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
            // What it would take, and it is no longer a flag this window was
            // started with: the desk's own arming answer is what widens a
            // window, and a door still naming `--operator` would send an
            // operator after something that does not exist.
            Line::from(Span::styled(
                " The desk's own posture arms a window to choose the data, the",
                Style::default().fg(t.text_secondary),
            )),
            Line::from(Span::styled(
                " book and the models; --pick asks the desk questions again.",
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

/// The arming question's two rows, in the order they are drawn, as the answer
/// each one sends. Two `bool`s rather than an enum: there is no third posture
/// on the wire, and the owner refuses anything that is not one of these.
const POSTURE_ROWS: [bool; 2] = [true, false];

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
    /// Ask the owner what its backends serve. Present only while nothing has —
    /// see [`ASK`].
    Ask,
    /// The mind this door reserves and refuses. See [`CODEX`].
    Codex,
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

/// What this desk reads its news from, and where an operator changes it.
///
/// Three states, and they are three different facts. A resolved stack is the
/// owner's own answer for this lane; `not configured` is the owner saying there
/// is none; and `--` is a route nothing has answered yet, which must not read
/// as either of the other two — the same rule every value on this client is
/// drawn by.
fn news_line(store: &Store) -> String {
    let said = match store.news() {
        Some(news) if !news.stack.is_empty() => news.stack.join(" "),
        Some(_) => "not configured".to_string(),
        None => MISSING.to_string(),
    };
    format!(" news: {said} · change later: Settings ▸ NEWS")
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

/// Whether the pair the owner is serving is one it says somebody named.
///
/// `Some(false)` only is the negative — the same rule `Store::desk_unchosen`
/// states, read here for the marker rather than for the trigger. An owner too
/// old to carry the flag is silent, and its silence leaves the marker reading
/// exactly what it read before the field existed.
fn owner_chose(store: &Store) -> bool {
    store.desk_mode().and_then(|mode| mode.chosen) != Some(false)
}

/// Which word a first-question row's marker carries.
fn mark(named: bool) -> &'static str {
    match named {
        true => CHOSEN,
        false => ASSUMED,
    }
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

/// The arming answer, in the build that has somewhere to send it.
///
/// Unreachable in the default build for a second reason on top of `pointed`'s:
/// `Store::asking_posture` is false there — an artifact an answer could not
/// widen is never asked — so no keystroke reaches this step at all.
fn posture(armed: bool) -> Option<Command> {
    #[cfg(feature = "operator")]
    return Some(Command::Posture { armed });
    #[cfg(not(feature = "operator"))]
    {
        let _ = armed;
        None
    }
}

/// The mind the desk is already running, sent so that answer is durable.
///
/// The rule [`Door::finish`] writes the pair by, one question further in: the
/// POST is what makes an answer durable, so a door that skipped it on "keep
/// what the desk is using" would leave the mind exactly as unchosen as it found
/// it and open again on the next run. Nothing here *changes* — the values are
/// the owner's own, and re-sending them is what makes the owner stop reporting
/// that nobody has answered.
///
/// The reasoner, named as the surface that is not the workforce rather than by
/// its position in `cmd::SURFACES`, which is the owner's own order and not a
/// contract about which of the two this is. It is the surface this question's
/// own footnote is about and the one an operator means by "which mind runs
/// Atlas"; the workforce keeps whatever it had, because the owner replaces one
/// surface per POST and a keystroke carries one command.
///
/// A desk that named no pair composes nothing: this refuses rather than
/// inventing a backend, and the door closes having written nothing — which
/// leaves the question open, and open is what it is.
fn kept(store: &Store) -> Option<Command> {
    let surface = cmd::SURFACES.into_iter().find(|s| *s != cmd::WORKFORCE)?;
    let running = store.llm()?.reasoner.as_ref()?;
    let backend = format::text(running.backend.as_ref())?;
    let model = format::text(running.model.as_ref())?;
    chose(
        surface,
        ModelChoice::Pair {
            backend: backend.to_string(),
            model: model.to_string(),
        },
    )
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
        desk_said(data, book, credentials_ok, Some(true))
    }

    /// The same desk, with the owner's `chosen` flag in whichever of its three
    /// states the caller is asking about. Only the armed tests read the marker
    /// the flag moves; the glass build has no `mode_lines` to read it from.
    #[cfg(feature = "operator")]
    fn desk_chosen(data: &str, book: &str, chosen: Option<bool>) -> Store {
        desk_said(data, book, false, chosen)
    }

    fn desk_said(data: &str, book: &str, credentials_ok: bool, chosen: Option<bool>) -> Store {
        let mut store = Store::default();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({
                    "desk_mode": {
                        "data": data, "book": book,
                        "label": "SYNTHETIC", "offline": data == SYNTHETIC,
                        "chosen": chosen,
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
        assert_eq!(
            Door::wanted(false, true, unchosen(true, true)),
            Some(Step::Mode),
            "answered, and said nothing"
        );
        assert_eq!(
            Door::wanted(false, true, unchosen(false, false)),
            None,
            "the owner named the desk"
        );
        assert_eq!(
            Door::wanted(false, false, unchosen(true, true)),
            None,
            "nothing has answered yet"
        );
        // And the flag, which asks a desk that answered everything — the walk
        // from the top, because what `--pick` asks for is the walk and not
        // whichever half of it happens to be open.
        assert_eq!(
            Door::wanted(true, false, unchosen(false, false)),
            Some(Step::Mode)
        );
        assert_eq!(
            Door::wanted(true, true, unchosen(false, true)),
            Some(Step::Mode)
        );
    }

    /// The owner's two `chosen` flags, as this client reads them.
    fn unchosen(desk: bool, mind: bool) -> Unchosen {
        Unchosen { desk, mind }
    }

    #[test]
    fn the_door_opens_on_the_first_question_nobody_has_answered() {
        // The whole of what this task changed. Keying the walk on the desk
        // mode's flag alone meant a desk whose pair was named long ago could
        // never be asked about its mind at all — and if it had been, the
        // question would have opened two Enters away from the one that was
        // open.
        assert_eq!(
            Door::wanted(false, true, unchosen(false, true)),
            Some(Step::Model),
            "a desk that named its pair was not asked about its mind"
        );
        // The pair outranks it, because it is the question in front: a desk
        // that has answered neither is walked, not jumped.
        assert_eq!(
            Door::wanted(false, true, unchosen(true, true)),
            Some(Step::Mode)
        );
        // And a mind somebody chose is not a question. Both remaining corners,
        // so a pass cannot come from the other one.
        assert_eq!(
            Door::wanted(false, true, unchosen(true, false)),
            Some(Step::Mode)
        );
        assert_eq!(Door::wanted(false, true, unchosen(false, false)), None);
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

    /// A desk that named its pair long ago, with the owner's flag on the *mind*
    /// in whichever of its three states the caller is asking about.
    ///
    /// Its own fixture rather than a parameter on `desk_said`: every test that
    /// reads it is about the question the pair being settled leaves open, and
    /// the pair here is deliberately `chosen: true` so nothing about it is.
    #[cfg(feature = "operator")]
    fn mind_said(chosen: Option<bool>) -> Store {
        let mut store = Store::default();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({
                    "desk_mode": {
                        "data": SYNTHETIC, "book": SIMULATED,
                        "label": "SYNTHETIC", "offline": true, "chosen": true,
                        "credentials": "no ALPACA_API_KEY_ID in the environment or .env",
                        "credentials_ok": false,
                    },
                    "llm": {
                        "reasoner": {"backend": "ollama", "model": "qwen2.5:7b"},
                        "workforce": {"backend": "claude", "model": "inherit"},
                        "reasoner_enabled": false,
                        "chosen": chosen,
                    }
                }))
                .unwrap(),
            )),
            Instant::now(),
        );
        store.posture = crate::store::Posture::Operator;
        store
    }

    /// The second question's lines as an operator reads them.
    #[cfg(feature = "operator")]
    fn model_said(door: &Door, store: &Store) -> String {
        door.model_lines(store)
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// One box's lines as an operator reads them.
    #[cfg(feature = "operator")]
    fn said(door: &Door, store: &Store) -> String {
        door.mode_lines(store)
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_live_row_is_choosable_whatever_the_login_says_and_never_silent_about_it() {
        // The gate that used to sit on this row was authority the door never
        // had — `set_desk_mode` validates the pair's shape only, `/mode` and the
        // web client both reach the same switch, and a book with no login fails
        // loud on every broker call. Removing the choice also removed the one
        // walk that ends at a login form, so the choice stays.
        let mut store = desk(SYNTHETIC, SIMULATED, false);
        armed(&mut store);
        let mut door = cursor_on(1, Step::Mode);
        assert_eq!(press(&mut door, KeyCode::Enter, &mut store), None);
        assert_eq!(door.data(&store), LIVE, "a broken login blocked the choice");
        assert!(
            door.mode_rows(&store).contains(&ModeRow::Book(ALPACA)),
            "the book question is what makes the login reachable"
        );
        // What survives is the honesty, and it is said **twice, in two
        // places**, because they are two statements: the row itself carries
        // the fact that there is no login, and the line under the rows carries
        // the owner's own description of what it tried to read. Asserted
        // separately for the reason a mutation found — deleting either one
        // leaves the other, and a single assertion passes on the survivor.
        // The third statement is the outcome's warning, pinned in
        // `operator_gate` against a stub owner.
        let refused = said(&door, &store);
        assert!(
            refused.contains("no Alpaca login the desk can read"),
            "the row stopped saying it:\n{refused}"
        );
        assert!(
            refused.contains("no ALPACA_API_KEY_ID"),
            "the owner's own description stopped being rendered:\n{refused}"
        );
        // The other side of that comparison — a desk whose login reads says
        // nothing about one, so both are about the credential and not about
        // the row they sit on.
        let mut ok = desk(SYNTHETIC, SIMULATED, true);
        armed(&mut ok);
        let mut door = cursor_on(1, Step::Mode);
        press(&mut door, KeyCode::Enter, &mut ok);
        assert!(!said(&door, &ok).contains("no Alpaca login"));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_desk_nobody_has_named_is_not_marked_as_though_somebody_had() {
        // The armed door's half of what the read-only one says outright. With
        // no `desk_mode` block the door still has to point somewhere, and the
        // fallback it picks is not a decision anyone made.
        let mut nothing = Store::default();
        nothing.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({"portfolio": {"equity": 1.0}})).unwrap(),
            )),
            Instant::now(),
        );
        // After the snapshot: the posture is derived from every payload now, so
        // a store armed before one is applied would be disarmed by it.
        armed(&mut nothing);
        let mut door = Door::default();
        assert!(
            said(&door, &nothing).contains(ASSUMED),
            "{}",
            said(&door, &nothing)
        );
        // One keystroke is a decision, and the word changes with it.
        press(&mut door, KeyCode::Enter, &mut nothing);
        assert!(said(&door, &nothing).contains(CHOSEN));
        // And an owner that named the desk has already made it.
        let mut named = desk(SYNTHETIC, SIMULATED, false);
        armed(&mut named);
        assert!(said(&Door::default(), &named).contains(CHOSEN));
        assert!(!said(&Door::default(), &named).contains(ASSUMED));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_pair_the_owner_says_nobody_chose_is_marked_assumed_however_concrete_it_is() {
        // The state this marker had to guess at until the owner learned to say
        // it. `synthetic · simulated` with `chosen: false` is a fallback nobody
        // named, and the words being present is not the same fact as somebody
        // having picked them — which is the whole reason this door is up.
        let mut unchosen = desk_chosen(SYNTHETIC, SIMULATED, Some(false));
        armed(&mut unchosen);
        let door = Door::default();
        assert!(
            said(&door, &unchosen).contains(ASSUMED),
            "{}",
            said(&door, &unchosen)
        );
        assert!(!said(&door, &unchosen).contains(CHOSEN));
        // Both sides of the comparison, and the third state with them: `true`
        // is a choice, and an owner too old to carry the field leaves the
        // marker reading exactly what it read before this field existed.
        for chosen in [Some(true), None] {
            let mut named = desk_chosen(SYNTHETIC, SIMULATED, chosen);
            armed(&mut named);
            assert!(
                said(&door, &named).contains(CHOSEN),
                "chosen: {chosen:?} lost the marker"
            );
            assert!(!said(&door, &named).contains(ASSUMED));
        }
        // And one keystroke is a decision whatever the owner said: the operator
        // naming the half is the other producer this marker reads.
        let mut door = Door::default();
        press(&mut door, KeyCode::Enter, &mut unchosen);
        assert!(said(&door, &unchosen).contains(CHOSEN));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_row_that_sets_the_boxs_width_fills_it_exactly_and_is_the_alpaca_one() {
        // [`DOOR_W`]'s own claim, asserted rather than counted by hand. The
        // alpaca row is the widest thing this question draws and fits with
        // nothing to spare, so a cell added to its label, its marker or the
        // sentence beside it wraps the one row that says a fill still needs a
        // human — and would do it two keystrokes deep, where the frame the door
        // opens on cannot show it.
        let mut store = desk(LIVE, ALPACA, false);
        armed(&mut store);
        let lines = Door::default().mode_lines(&store);
        let widest = lines.iter().map(|line| line.width()).max().unwrap();
        assert_eq!(
            widest,
            (DOOR_W - 2) as usize,
            "the widest row no longer fills the box exactly:\n{}",
            said(&Door::default(), &store)
        );
        // And it is that row rather than some other one that happens to tie,
        // which is what makes the constant's doc a fact about the alpaca row.
        let alpaca = lines
            .iter()
            .find(|line| {
                line.spans
                    .iter()
                    .any(|span| span.content.contains("ALPACA PAPER"))
            })
            .expect("a live desk discloses the alpaca row");
        assert_eq!(alpaca.width(), widest);
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
        assert_eq!(door.step(&store), Step::Model);
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

    /// A door that has been given a box to draw in.
    ///
    /// `cap` is read off the frame the door was last handed, and one that has
    /// never been drawn has room for exactly one row — so a test that reads the
    /// second question's *lines* has to hand it a box first, exactly as the
    /// runtime does before it reads its first event.
    #[cfg(feature = "operator")]
    fn drawn(door: Door) -> Door {
        door.area.set(Rect::new(0, 0, DOOR_W, 30));
        door
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_desk_that_named_its_pair_and_no_mind_opens_on_the_mind_alone() {
        // The state this task exists for, through the store that opens the
        // door: the pair was answered long ago, so there is one question left
        // and the box opens on it rather than two Enters away from it.
        let store = mind_said(Some(false));
        let door = store.door().expect("an unchosen mind owes a door");
        assert_eq!(door.step(&store), Step::Model);
        // And the two readings that are not that. `true` is somebody's answer;
        // absence is an owner too old to carry the field, and reading its
        // silence as `false` would open this box on every launch of every desk
        // that already has a mind.
        for chosen in [Some(true), None] {
            assert!(
                mind_said(chosen).door().is_none(),
                "chosen: {chosen:?} opened a door"
            );
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_door_that_opened_on_the_mind_question_asks_what_the_desk_can_run() {
        // Nothing polls the catalog and the only thing that asks for it is a
        // keystroke — `models →`, on the way in. A door that OPENED here
        // crossed no such transition, so without this row the first question an
        // operator is ever asked about which mind runs Atlas offers no mind.
        let mut store = mind_said(Some(false));
        let mut door = store.take_door().unwrap();
        assert_eq!(door.model_rows(&store).first(), Some(&ModelRow::Ask));
        assert_eq!(door.at, 0, "the cursor opened past the only row that acts");
        assert_eq!(
            press(&mut door, KeyCode::Enter, &mut store),
            Some(Command::Backends)
        );
        assert!(door.standing(), "asking is not answering");
        // Gone the moment the desk has said, and absent on the walked-in path
        // whether it has or not: there the keystroke that arrived already
        // asked, and a row offering to ask again would appear for exactly the
        // frames between the two — taking the cursor with it.
        let answered = with_catalog(mind_said(Some(false)), true);
        assert!(!door.model_rows(&answered).contains(&ModelRow::Ask));
        let walked = cursor_on(0, Step::Model);
        assert!(!walked.model_rows(&store).contains(&ModelRow::Ask));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_catalog_settles_the_cursor_of_a_door_that_had_none_to_settle_it_on() {
        // Both doors that reach the model question set their cursor before the
        // catalog exists — the walked-in one because `models →` asks and
        // advances in one keystroke, this one because nothing had asked at all.
        // A cursor left where it was sits on whichever pair the catalog happens
        // to list first, and Enter there is a change nobody asked for.
        let store = with_catalog(mind_said(Some(false)), true);
        let door = store.door().expect("the door survived the catalog");
        let rows = door.model_rows(&store);
        assert!(
            matches!(&rows[door.at], ModelRow::Offer { surface, value, current: true, .. }
                     if *surface == "reasoner" && value == "ollama:qwen2.5:7b"),
            "the cursor did not open on what the desk runs: {:?}",
            rows.get(door.at)
        );
    }

    #[cfg(feature = "operator")]
    #[test]
    fn codex_is_on_the_list_and_refuses_by_name_with_the_reason() {
        // Named, not built — the design's own ruling. Leaving it off the list
        // reads as a desk that never heard of it; offering it would be this
        // client claiming a backend the owner has no id or launcher for.
        let mut store = with_catalog(mind_said(Some(false)), true);
        let mut door = drawn(store.take_door().unwrap());
        let rows = door.model_rows(&store);
        assert!(rows.contains(&ModelRow::Codex), "{rows:?}");
        let drawn_rows = model_said(&door, &store);
        assert!(drawn_rows.contains(CODEX), "{drawn_rows}");
        assert!(
            drawn_rows.contains(CODEX_ROW),
            "the row refuses without saying why:\n{drawn_rows}"
        );
        // Chosen, it says what it would take and changes nothing: no command,
        // and the question is still up.
        door.at = rows.iter().position(|row| row == &ModelRow::Codex).unwrap();
        assert_eq!(press(&mut door, KeyCode::Enter, &mut store), None);
        assert!(door.standing(), "a refusal closed the door");
        let refused = model_said(&door, &store);
        assert!(
            refused.contains("~/.codex/config.toml"),
            "the refusal did not name what codex is missing:\n{refused}"
        );
    }

    #[cfg(feature = "operator")]
    #[test]
    fn answering_the_mind_question_retires_the_door_that_opened_on_it() {
        // A door opened for one question has had its answer, and a question
        // that has been answered is not one to keep asking.
        let mut store = with_catalog(mind_said(Some(false)), true);
        let mut door = store.take_door().unwrap();
        // The cursor opens on what the desk runs, so the row above it is a
        // change — an answer rather than a re-statement.
        press(&mut door, KeyCode::Up, &mut store);
        let acted = press(&mut door, KeyCode::Enter, &mut store);
        assert!(matches!(acted, Some(Command::SetLlm { .. })), "{acted:?}");
        assert!(!door.standing(), "the answered question stayed up");
        // And the same key on the walked-in door does *not* retire it: there
        // the pair is still waiting to be applied on the way out, and the other
        // surface is still an open row.
        let mut walked = cursor_on(0, Step::Model);
        walked.at = walked.current_model(&store);
        assert!(matches!(
            press(&mut walked, KeyCode::Enter, &mut store),
            Some(Command::SetLlm { .. })
        ));
        assert!(walked.standing(), "choosing one surface closed the other");
    }

    #[cfg(feature = "operator")]
    #[test]
    fn keeping_the_mind_the_desk_runs_is_written_so_the_question_stays_answered() {
        // The rule the pair is written by, one question further in: the POST is
        // what makes an answer durable, so a door that skipped it on "keep"
        // would leave the mind exactly as unchosen as it found it and open
        // again on the next run. What it must NOT write is a pair — this door
        // never put one to anybody.
        let running = || Command::SetLlm {
            surface: "reasoner".to_string(),
            choice: ModelChoice::Pair {
                backend: "ollama".to_string(),
                model: "qwen2.5:7b".to_string(),
            },
        };
        let mut store = with_catalog(mind_said(Some(false)), true);
        let mut door = store.take_door().unwrap();
        door.at = door.model_rows(&store).len() - 1;
        assert_eq!(
            press(&mut door, KeyCode::Enter, &mut store),
            Some(running())
        );
        assert!(!door.standing());
        // Esc is the same answer by the same rule: there is no pair here to
        // skip, so the key that gets out of the way means what the last row
        // means.
        let mut store = with_catalog(mind_said(Some(false)), true);
        let mut door = store.take_door().unwrap();
        assert_eq!(press(&mut door, KeyCode::Esc, &mut store), Some(running()));
        assert!(!door.standing());
        // The walked-in door still applies the pair on the way out, which is
        // the behaviour the split above must not have taken away.
        let mut walked = cursor_on(0, Step::Model);
        walked.at = walked.model_rows(&store).len() - 1;
        assert_eq!(
            press(&mut walked, KeyCode::Enter, &mut store),
            Some(Command::DeskMode {
                data: SYNTHETIC.to_string(),
                book: SIMULATED.to_string()
            })
        );
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_two_rows_this_question_gained_fit_the_box() {
        // [`DOOR_W`]'s claim, applied to the rows nothing else measures: these
        // two are the only ones here whose words this client owns, so they are
        // the only ones a wrap could be introduced in by an edit. A row one
        // cell over wraps onto an unindented second line, exactly as the alpaca
        // row's constant says of the question before this one.
        let store = mind_said(Some(false));
        let lines = drawn(Door::opening(Step::Model)).model_lines(&store);
        for needle in [ASK, CODEX_ROW] {
            let row = lines
                .iter()
                .find(|line| line.spans.iter().any(|span| span.content.contains(needle)))
                .unwrap_or_else(|| panic!("{needle} is not on the question"));
            assert!(
                row.width() <= (DOOR_W - 2) as usize,
                "{needle} takes {} of the {} cells the box has",
                row.width(),
                DOOR_W - 2
            );
        }
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
