//! SETTINGS — what this desk is configured by, and the one thing an operator may type into it.
//!
//! Six cards of read-only facts an operator would otherwise have to assemble
//! from `mandate.yaml`, `.mcp.json`, a shell prompt and whatever the last
//! `/mode` did. Everything on the pane is the owner's own answer; nothing here
//! is composed, defaulted, or inferred.
//!
//! **MODELS is read-only too, and deliberately so for now.** It says which
//! minds the desk is using and how fresh that answer is; changing them is the
//! owner's own route, and the keys that reach it arrive with D4. Until then
//! this pane claims no key for it — an affordance that looked like a control
//! and did nothing would be worse than none.
//!
//! Absence is the rule this view is mostly about. A `max_weight` rendered as
//! `0.0%` because the owner did not send one is a mandate that forbids holding
//! anything, which is a statement about the desk that nobody made.
//!
//! **The exception is the alpaca login.** The pane still changes no desk
//! setting — switching the desk is `/mode`, and this form does not do it — but
//! it is where a credential is typed, because the owner's credential route is
//! the only writer of that file and a desk that can only be logged into from a
//! shell is one an operator will log into some other way. What the form sends
//! grants no authority: a stored login makes `LIVE·ALPACA` *choosable*, the
//! book is not switched by it, and every gate between a plan and a fill is
//! unmoved.
//!
//! Three rules shape it, and all three are about the value rather than the
//! layout. Both fields render as `•` — the key is the less sensitive half and
//! masking only one of them would make the other look like the secret. The
//! plaintext lives in this module's own `Form` and in the `Secret` it hands the
//! runtime, and nowhere else: no history, no tracing line, no toast, and no
//! `Debug` (see `Form`'s hand-written one, and `crate::secret`). And the box is
//! cleared on the way out, best effort and honestly so — a `String` that grew
//! may have left an earlier buffer behind that nothing here can reach.
//!
//! What the keys can do is the posture's decision, not the build's, exactly as
//! on AUDIT and WORKFORCE: in the glass build there is no `Command::AlpacaLogin`
//! and no writer to carry it, so the form does not exist at all and the alpaca
//! row is the status display it has always been.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::{Constraints, DeskMode, LlmConfig, LlmSurface, System};
use crate::store::Store;
use crate::theme::{palette, theme};
use crate::ui::views::View;
use crate::ui::widgets::{panel_block, panel_header, refuse};
#[cfg(feature = "operator")]
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::Style,
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
    Frame,
};
use std::time::Instant;

/// The label column, exactly wide enough for the longest label on the pane
/// (`alpaca login`) plus the space after it. Wider and the values start
/// wrapping at the baseline width; narrower and the label collides with them.
const LABEL_W: usize = 13;

/// One card's floor: the label column, a space, and enough value for the
/// longest one that may not be clipped — the credential description wraps, but
/// `propose_only` and a provenance pair do not.
const CARD_MIN: u16 = 34;

/// Two cards side by side, with a column of space between them.
const TWO_COL: u16 = CARD_MIN * 2 + 1;

/// The login form's own floor, in rows of the *view's* area.
///
/// Its own, and higher than the view's: SETTINGS draws its cards from twelve
/// rows, and a box drawn into what is left of those would have room for a
/// header and nothing else — no fields, no footer, no owner's sentence — while
/// still holding the keyboard and still sending on Enter. That is the state
/// WORKFORCE's picker already refuses at, and for the same reason: an armed
/// control an operator cannot see is worse than one that says what it would
/// take.
#[cfg(feature = "operator")]
const FORM_MIN_H: u16 = 14;

/// The box's width. Wide enough for the label column beside a masked field, and
/// for the owner's consent sentence to wrap in four lines rather than eight.
#[cfg(feature = "operator")]
const FORM_W: u16 = 60;

/// The word a login that would destroy another one asks for.
///
/// Static, unlike the execution modal's six characters of a `targets_hash`.
/// There is no hash here because there is no plan: what this consent authorises
/// is the destruction of a stored profile, and the thing it must not be is one
/// keystroke. A challenge that bound to nothing the desk had checked would be
/// worse than none — it teaches an operator that typing the characters means
/// something was verified — so this one binds nothing and says nothing about
/// having been verified.
#[cfg(feature = "operator")]
const CONFIRM: &str = "CONFIRM";

/// Where the operator is looking, and what they have typed. Never what the desk
/// says — that is the `Store`'s.
///
/// The cards retain nothing: no cursor, no page. The form is the one thing on
/// this pane an operator can be inside.
#[derive(Default)]
pub struct SettingsView {
    /// The login form, while the operator is asking for it.
    ///
    /// *Asking for*, not *showing*: below [`FORM_MIN_H`] the pane refuses to
    /// draw the box and says so instead, and this stays `Some` only until the
    /// next keystroke retires it. What must never happen is the third state — a
    /// box that is armed and invisible.
    #[cfg(feature = "operator")]
    form: Option<Form>,
    /// The view's area at the last frame, published by `draw`.
    ///
    /// Interior mutability for the reason WORKFORCE's height has it: `draw` is
    /// a `&self` renderer that publishes the layout it derived, and whether the
    /// form fits is a fact about that layout. Nothing *renders* from it — the
    /// frame stays a pure function of (store, effects, instant) — it only
    /// decides whether a key may open a box that would not fit.
    ///
    /// The whole rect rather than the height, because this pane refuses on
    /// width as well: at 68 columns the cards are a refusal message, and a form
    /// opened over that would be a box on a pane that is not drawn.
    #[cfg(feature = "operator")]
    area: std::cell::Cell<Rect>,
}

impl View for SettingsView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        // Published first, and on every frame including the ones that draw no
        // cards: this is what a later keystroke reads to decide whether the
        // form may open, and an area only recorded when the pane already fitted
        // could never report that it stopped fitting.
        self.publish(area);
        // Label/value rows do not compress: a provenance clipped to `synthe` is
        // a source an operator has to guess at, and an authority clipped to
        // `propose_` is a governance claim that has lost its qualifier. So the
        // pane refuses rather than drawing half of each.
        if area.width < TWO_COL || area.height < 12 {
            refuse(
                f,
                area,
                format!(
                    "SETTINGS needs {TWO_COL} columns for two cards of label/value rows; \
                     this pane has {}.",
                    area.width
                ),
            );
            return;
        }
        // DESK spans both columns. It is the headline fact — which desk this
        // is, and whether it can reach the book it is pointed at — and the
        // credential description under it is a sentence the owner wrote, which
        // a half-width card would clip mid-word.
        let bands = Layout::vertical([Constraint::Length(DESK_H), Constraint::Min(0)]).split(area);
        draw_desk(f, bands[0], store);

        let cols = Layout::horizontal([Constraint::Ratio(1, 2), Constraint::Ratio(1, 2)])
            .spacing(1)
            .split(bands[1]);

        // Fixed heights, then the rationale and the theme take what is left. A
        // `Paragraph` taller than its area is clipped silently, so every card
        // with a known row count states it rather than sharing a ratio that
        // would shorten one of them the first time a card grew a row.
        // THEME moved down here when MODELS arrived. The left column's slack is
        // the rationale, which is one wrapped sentence over ten empty rows, and
        // the right column had none to give a card whose whole point is that the
        // owner's reason for an unreachable backend is not clipped. Anchored to
        // the bottom rather than stacked under the rationale, which has no
        // height of its own to end at.
        let left = Layout::vertical([
            Constraint::Length(POLICY_H),
            Constraint::Min(0),
            Constraint::Length(THEME_H),
        ])
        .split(cols[0]);
        draw_policy(f, left[0], store);
        draw_rationale(f, left[1], store);
        draw_theme(f, left[2]);

        // MODELS sits directly under SYSTEM rather than growing SYSTEM a row.
        // The two answer the same question — what this desk is made of — and
        // the adjacency is the pointer: SYSTEM's `claude` row stays the owner's
        // own health fact about the CLI, and the card under it is where both
        // backends' availability, the choice per surface, and the age of that
        // reading are named. A row on SYSTEM could carry the names or the
        // reasons but not the stamp, and a summary without its stamp is the
        // exact thing A2 warned this payload would become.
        //
        // UNIVERSE takes the remainder because it is the one card here that
        // uses extra rows: its symbol list wraps, and a desk watching thirty
        // names has more of them on screen instead of a column of blanks.
        let right = Layout::vertical([
            Constraint::Length(SYSTEM_H),
            Constraint::Length(MODELS_H),
            Constraint::Min(UNIVERSE_H),
        ])
        .split(cols[1]);
        draw_system(f, right[0], store);
        draw_models(f, right[1], store);
        draw_universe(f, right[2], store);

        // Over the view rather than over the frame: the question it asks is
        // about this pane's own controls, unlike the confirm box, which asks
        // about an order and belongs to the whole workstation.
        self.draw_form(f, area, store);
    }

    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        self.keys(k, store)
    }

    /// Whether the login form currently owns the keyboard.
    ///
    /// True only while a form an operator can actually see is open. A form too
    /// tall for the pane is drawn as a refusal, not as a box, so it holds no
    /// keyboard — without that half the fields would swallow every key, and
    /// Enter would still store a credential, against a box nobody can see.
    fn typing(&self) -> bool {
        #[cfg(feature = "operator")]
        {
            self.form.is_some() && self.form_fits()
        }
        #[cfg(not(feature = "operator"))]
        false
    }
}

// -- the login form ---------------------------------------------------------

/// Which of the two fields the caret is in.
#[cfg(feature = "operator")]
#[derive(Default, Clone, Copy, PartialEq, Eq)]
enum Field {
    #[default]
    Key,
    Secret,
}

/// Where the form is in the one exchange it can have with the owner.
///
/// Three states, and the middle one is why the form does not close on Enter: a
/// login the owner will not store without consent has to be re-sent *with the
/// pair still in hand*, and a box that cleared itself on send would have to ask
/// the operator to type both values again to answer a question about the values
/// they just typed.
#[cfg(feature = "operator")]
#[derive(Default)]
enum Stage {
    /// Typing the pair.
    #[default]
    Editing,
    /// Sent, and waiting for the owner. Enter does nothing here: one request at
    /// a time, so a held key cannot queue a second write of a credential file.
    Sent,
    /// The owner will not overwrite what is already stored without being asked
    /// twice. `said` is its sentence, rendered verbatim; `typed` is the
    /// challenge so far.
    Consent { said: String, typed: String },
}

/// The two values, where the operator is in them, and what the desk last said.
///
/// **No `derive(Debug)`.** The hand-written one below is the reason: a derived
/// implementation would print both fields, and `{:?}` on anything holding this
/// — a view, a registry, a panic message — would print them with it. The
/// redaction is the default rather than a rule somebody has to remember.
#[cfg(feature = "operator")]
#[derive(Default)]
struct Form {
    key: String,
    secret: String,
    at: Field,
    stage: Stage,
    /// What the desk, or this form, last said back. Retired by the next
    /// keystroke: an answer beside a field the operator has since changed is an
    /// answer to a question they are no longer asking.
    note: Option<String>,
}

#[cfg(feature = "operator")]
impl std::fmt::Debug for Form {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Form(<redacted>)")
    }
}

/// However the form goes away — `Esc`, a stored login, the view being dropped —
/// the buffers are covered on the way out.
///
/// On `Drop` rather than at each exit, so there is no path that forgets. Best
/// effort, and stated as such at `secret::wipe`: a `String` that grew while it
/// was typed into may have left an earlier allocation behind, and that one is
/// already out of reach of any code here.
#[cfg(feature = "operator")]
impl Drop for Form {
    fn drop(&mut self) {
        crate::secret::wipe(&mut self.key);
        crate::secret::wipe(&mut self.secret);
        if let Stage::Consent { typed, .. } = &mut self.stage {
            crate::secret::wipe(typed);
        }
    }
}

#[cfg(feature = "operator")]
impl Form {
    /// The field the caret is in, while there is one to type into.
    fn field(&mut self) -> &mut String {
        match self.at {
            Field::Key => &mut self.key,
            Field::Secret => &mut self.secret,
        }
    }

    fn push(&mut self, c: char) {
        self.note = None;
        match &mut self.stage {
            Stage::Editing => self.field().push(c),
            // Bounded by the challenge's own length, so a held key cannot push
            // the correct prefix out of the field and leave a human looking at
            // an unarmed box they believe they filled in — the same rule the
            // execution modal's field has.
            Stage::Consent { typed, .. } => {
                if typed.chars().count() < CONFIRM.chars().count() {
                    typed.push(c);
                }
            }
            Stage::Sent => {}
        }
    }

    fn backspace(&mut self) {
        self.note = None;
        match &mut self.stage {
            Stage::Editing => {
                self.field().pop();
            }
            Stage::Consent { typed, .. } => {
                typed.pop();
            }
            Stage::Sent => {}
        }
    }

    /// The other field. Only while there are two — inside the consent question
    /// there is one thing to type and Tab has nowhere to go.
    fn focus_other(&mut self) {
        if matches!(self.stage, Stage::Editing) {
            self.at = match self.at {
                Field::Key => Field::Secret,
                Field::Secret => Field::Key,
            };
        }
    }

    /// The pair as it would be sent: trimmed, because the fields are masked and
    /// a trailing space from a paste is invisible in them. The owner would
    /// refuse the shape and the operator could not see why.
    fn pair(&self) -> (String, String) {
        (self.key.trim().to_string(), self.secret.trim().to_string())
    }
}

#[cfg(feature = "operator")]
impl SettingsView {
    /// Whether the last frame left room to draw the form.
    ///
    /// Read off the area `draw` published, because the floor is a fact about
    /// the pane and a key handler is never told one. Zero before the first
    /// frame, which refuses — a client that has not drawn cannot know it has
    /// room, and the runtime draws once before it reads its first event.
    fn form_fits(&self) -> bool {
        let area = self.area.get();
        area.height >= FORM_MIN_H && area.width >= TWO_COL
    }

    fn publish(&self, area: Rect) {
        self.area.set(area);
    }

    /// Every key this pane claims, gated on the posture rather than the build.
    ///
    /// The order is load-bearing: an open form outranks the two keys that reach
    /// the desk, or `t` would be untypeable inside a secret.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn keys(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        if !store.posture.writes() {
            // Same rule as AUDIT's arrows: a key with no visible effect reads
            // as a hung client, so an unarmed window declines rather than
            // swallows, and the keys stay free for whoever claims them next.
            return None;
        }
        // A terminal that shrank under an open form retires it, on the first
        // key after the resize — and the fields go with it, so growing back
        // cannot restore a half-typed credential the operator has not seen
        // since. The box is already refusing to draw and already holding no
        // keyboard by then; this is what keeps the *state* from outliving it.
        if self.form.is_some() && !self.form_fits() {
            self.close();
            return None;
        }
        if self.form.is_some() {
            return self.form_key(k);
        }
        match k.code {
            KeyCode::Char('a') => self.form = Some(Form::default()),
            // No client-side gate on there being a login to test. A desk that
            // has never logged in is one of the answers the route is built to
            // give ("no credentials are configured"), and a client that
            // pre-empted it would be a second, drifting copy of the owner's
            // own account of what it can read.
            KeyCode::Char('t') => return Some(Command::TestAlpaca),
            _ => {}
        }
        None
    }

    /// The form's own keys. One match over both stages, because they are one
    /// surface to an operator: the same four keys mean the same four things
    /// whether the box is asking for a pair or for a word.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn form_key(&mut self, k: KeyEvent) -> Option<Command> {
        let form = self.form.as_mut()?;
        match k.code {
            KeyCode::Char(c) => form.push(c),
            KeyCode::Backspace => form.backspace(),
            KeyCode::Tab => form.focus_other(),
            // Closes *and* clears. A form left holding a key pair behind
            // whichever view the operator moved to is a credential this client
            // is keeping for no reason.
            KeyCode::Esc => self.close(),
            KeyCode::Enter => return self.submit(),
            _ => {}
        }
        None
    }

    /// What Enter does, per stage.
    fn submit(&mut self) -> Option<Command> {
        let form = self.form.as_mut()?;
        match &form.stage {
            // One request at a time. The owner writes an audit row per stored
            // login, and a second Enter while the first is in flight would put
            // two of them on the bus for one decision.
            Stage::Sent => None,
            Stage::Editing => {
                let (key, secret) = form.pair();
                // An incomplete login is not a login. The owner refuses it
                // anyway — with a sentence about a field this form knows is
                // empty — which would reach the operator as a failed write
                // rather than as a slip.
                if key.is_empty() || secret.is_empty() {
                    form.note = Some("both the key and the secret are required".to_string());
                    return None;
                }
                form.stage = Stage::Sent;
                Some(Command::AlpacaLogin {
                    key: crate::secret::Secret::new(key),
                    secret: crate::secret::Secret::new(secret),
                    // Never on the first send. The flag is consent, and nobody
                    // has been asked yet.
                    replace: false,
                })
            }
            Stage::Consent { typed, .. } => {
                // An unarmed Enter leaves the question up rather than closing
                // it: a human who mistyped the challenge has to see that they
                // did.
                if typed != CONFIRM {
                    return None;
                }
                let (key, secret) = form.pair();
                // The consent is spent by the send that carries it. `Sent` is
                // terminal for this question — nothing moves the form back into
                // `Consent`, so retyping the word cannot authorise a second
                // overwrite, and the answer that comes back decides what
                // happens next.
                form.stage = Stage::Sent;
                Some(Command::AlpacaLogin {
                    key: crate::secret::Secret::new(key),
                    secret: crate::secret::Secret::new(secret),
                    replace: true,
                })
            }
        }
    }

    /// Close the form and clear it. The wipe is `Form`'s own `Drop`.
    fn close(&mut self) {
        self.form = None;
    }

    /// Open the login box from somewhere that is not this pane's `a` key.
    ///
    /// The startup door's third step is this call and nothing else. There is no
    /// second form: a credential is typed into exactly one box in this client,
    /// with one masking rule, one `Drop` that wipes it, one consent flow for
    /// the owner's question about destroying a stored profile, and one file
    /// `operator_gate` lets the plaintext be readable in. A door that grew
    /// fields of its own would be a second answer to every one of those.
    ///
    /// It opens rather than toggles: a door that had already handed over and a
    /// pane the operator opened by hand must not close each other's box.
    /// Whether it can be *drawn* is still the form's own floor — below it the
    /// pane refuses in place and the next keystroke retires the box.
    pub fn open_login(&mut self) {
        if self.form.is_none() {
            self.form = Some(Form::default());
        }
    }

    /// What the owner said about the login this form sent.
    ///
    /// Called from the runtime's one drain point, because the answer arrives on
    /// the bus rather than out of the key that asked for it — see
    /// `views::Views::wrote`. Only a form that is *waiting* reacts: an outcome
    /// belonging to another key, or one that arrived after the operator
    /// abandoned the box, must not reopen it or overwrite what is being typed
    /// into it now.
    pub fn wrote(&mut self, outcome: &crate::bus::Wrote) {
        use crate::bus::Wrote;
        if !matches!(
            self.form.as_ref().map(|form| &form.stage),
            Some(Stage::Sent)
        ) {
            return;
        }
        // Stored: the box has done its work and the toast reports it. Closing
        // is also what clears the pair — there is no state in which this client
        // holds a credential it has already sent.
        if matches!(outcome, Wrote::LoggedIn { .. }) {
            self.close();
            return;
        }
        let Some(form) = self.form.as_mut() else {
            return;
        };
        match outcome {
            // The owner's question, verbatim. It names what would be lost, and
            // this client owns none of that wording.
            Wrote::LoginNeedsConsent { said } => {
                form.stage = Stage::Consent {
                    said: said.clone(),
                    typed: String::new(),
                }
            }
            // Not confirmable: the request is wrong and the operator fixes it,
            // so the box stays up with the pair still in it and the owner's
            // sentence under the fields.
            Wrote::LoginRefused { said } => {
                form.stage = Stage::Editing;
                form.note = Some(said.clone());
            }
            // A request that never landed. The form must not stay in `Sent`
            // over it — a box that refuses Enter forever after one timeout is a
            // client that looks broken.
            Wrote::Failed { said, .. } => {
                form.stage = Stage::Editing;
                form.note = Some(said.clone());
            }
            _ => {}
        }
    }

    /// The form, drawn over the pane that opened it.
    fn draw_form(&self, f: &mut Frame, area: Rect, store: &Store) {
        use ratatui::widgets::{Block, Borders, Clear};
        let Some(form) = &self.form else {
            return;
        };
        // Refuse rather than open invisible, exactly as WORKFORCE's picker
        // does: below the floor the box would have room for a header and
        // nothing else, and `typing` declines the keyboard for the same frame,
        // so the refusal is the whole of what the operator gets.
        if !self.form_fits() {
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
                    "the alpaca login form needs {FORM_MIN_H} rows; this pane has {}.",
                    area.height
                ),
            );
            return;
        }
        let t = theme();
        let w = FORM_W.min(area.width.saturating_sub(4)).max(3);
        // Built before the box is sized, because how tall it has to be is a
        // fact about what is in it: the owner's sentences vary from four words
        // to four lines, and a fixed box would either clip the long ones or
        // draw eight blank rows under the short ones.
        let lines = match &form.stage {
            Stage::Consent { said, typed } => consent_lines(said, typed),
            _ => editing_lines(form, store, w - 2),
        };
        let rect = centred(area, w, wanted(&lines, w - 2));
        f.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(match form.stage {
                // The one stage that is a question about destroying something.
                Stage::Consent { .. } => t.warning,
                _ => t.accent,
            }))
            .style(Style::default().bg(t.bg_raised));
        let inner = block.inner(rect);
        f.render_widget(block, rect);
        // Wrapped, because the owner's sentences are sentences: half a refusal
        // about why a login cannot be stored is worse than one that took two
        // lines.
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
    }
}

/// How many rows the box needs for what it is about to draw, borders included.
///
/// An estimate, and it rounds up: `Paragraph` wraps at word boundaries, so a
/// line one cell past the width can take a whole row more than the division
/// says. The extra row when anything wraps is what keeps the last line of an
/// owner's refusal on screen rather than one cell under the border.
#[cfg(feature = "operator")]
fn wanted(lines: &[Line<'static>], inner_w: u16) -> u16 {
    let width = inner_w.max(1) as usize;
    let rows: usize = lines
        .iter()
        .map(|line| line.width().div_ceil(width).max(1))
        .sum();
    let wrapped = lines.iter().any(|line| line.width() > width);
    rows as u16 + u16::from(wrapped) + 2
}

/// The box's rect: centred, and never larger than the pane it is drawn over.
#[cfg(feature = "operator")]
fn centred(area: Rect, w: u16, h: u16) -> Rect {
    let h = h.min(area.height.saturating_sub(2)).max(1);
    Rect {
        x: area.x + (area.width.saturating_sub(w)) / 2,
        y: area.y + (area.height.saturating_sub(h)) / 2,
        width: w,
        height: h,
    }
}

/// The two masked fields, and what the box last said.
#[cfg(feature = "operator")]
fn editing_lines(form: &Form, store: &Store, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let room = (width as usize).saturating_sub(LABEL_W + 3);
    let sending = matches!(form.stage, Stage::Sent);
    let mut lines = vec![
        panel_header("alpaca login"),
        field_row("key", &form.key, form.at == Field::Key && !sending, room),
        field_row(
            "secret",
            &form.secret,
            form.at == Field::Secret && !sending,
            room,
        ),
        Line::from(""),
    ];
    lines.push(Line::from(Span::styled(
        match (&form.note, sending) {
            (_, true) => "asking the owner…".to_string(),
            (Some(note), _) => format!(" {note}"),
            // Stated because it is a documented property of the route this key
            // calls, and an operator who typed a login expecting the desk to
            // start trading it would otherwise read the unchanged book as a
            // bug: the owner stores the credential and switches nothing.
            (None, _) => " stored by the owner — the book is not switched by it".to_string(),
        },
        Style::default().fg(match form.note {
            Some(_) => t.warning,
            None => t.text_dim,
        }),
    )));
    lines.push(Line::from(Span::styled(
        " Enter stores · Tab the other field · Esc clears",
        Style::default().fg(t.text_dim),
    )));
    // What the desk currently reads, so an operator can tell "this replaced
    // nothing" from "this replaced something" before they are asked to.
    if let Some(mode) = store.desk_mode() {
        if let Some(said) = format::text(mode.credentials.as_ref()) {
            lines.push(Line::from(Span::styled(
                format!(" now: {said}"),
                Style::default().fg(t.text_tertiary),
            )));
        }
    }
    lines
}

/// One masked field. Both are masked: the key id is the less sensitive half,
/// and masking only one of them would make the other look like the secret.
#[cfg(feature = "operator")]
fn field_row(label: &str, value: &str, focused: bool, room: usize) -> Line<'static> {
    let t = theme();
    let typed = value.chars().count();
    // Clipped rather than run past the box, and marked when it is: a field that
    // silently stopped growing would read as one that stopped accepting keys.
    let dots = match typed > room {
        true => format!("{}…", "•".repeat(room.saturating_sub(1))),
        false => "•".repeat(typed),
    };
    Line::from(vec![
        Span::styled(
            format!(" {label:<LABEL_W$}"),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(dots, Style::default().fg(t.text_primary)),
        // A field with no caret is one an operator cannot tell from a label.
        Span::styled(
            if focused { "▏" } else { "" },
            Style::default().fg(t.accent),
        ),
    ])
}

/// The owner's question, and the word that answers it.
#[cfg(feature = "operator")]
fn consent_lines(said: &str, typed: &str) -> Vec<Line<'static>> {
    let t = theme();
    vec![
        panel_header("replace the stored login"),
        Line::from(Span::styled(
            format!(" {said}"),
            Style::default().fg(t.text_primary),
        )),
        Line::from(""),
        Line::from(vec![
            Span::styled(" type ", Style::default().fg(t.text_secondary)),
            Span::styled(
                CONFIRM,
                Style::default()
                    .fg(t.warning)
                    .add_modifier(ratatui::style::Modifier::BOLD),
            ),
            Span::styled(
                " to replace it · Esc leaves it alone",
                Style::default().fg(t.text_secondary),
            ),
        ]),
        Line::from(vec![
            Span::styled(" > ", Style::default().fg(t.text_tertiary)),
            Span::styled(
                typed.to_string(),
                Style::default()
                    .fg(match typed == CONFIRM {
                        true => t.positive,
                        false => t.text_primary,
                    })
                    .add_modifier(ratatui::style::Modifier::BOLD),
            ),
        ]),
    ]
}

/// The default build's half: no form, and no branch that could grow one — the
/// commands it would send are not in this build.
#[cfg(not(feature = "operator"))]
impl SettingsView {
    fn publish(&self, _area: Rect) {}
    fn draw_form(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}

    fn keys(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

/// Header, five rows, a blank, the posture line, one row of slack for a value
/// long enough to wrap, and the rule the block reserves.
const DESK_H: u16 = 10;
/// Header, eight rows, and the rule.
const POLICY_H: u16 = 10;
/// Header, seven rows, and the rule.
const SYSTEM_H: u16 = 9;
/// Header, four rows, four of slack, and the rule.
///
/// The slack is for the availability reasons, which are the owner's sentences
/// rather than values. "ollama is running at 127.0.0.1:11434 but no models are
/// pulled — pull one with `ollama pull granite3.3:8b`" is three wrapped rows in
/// a half-width card, and the remedy is the last third of it. A card sized for
/// the four fixed rows alone would clip exactly the state where the sentence is
/// the whole message, which is the class of refusal this pane spends rows to
/// avoid.
const MODELS_H: u16 = 10;
/// The card's own floor: the header, the four rows that may not be split from
/// one another, and the rule the block reserves.
///
/// **Those four are one section**, and the pane's own floor does not protect
/// them: SETTINGS refuses below twelve rows, but the right column needs
/// twenty-three, so at 120×26 the column simply handed this card fewer rows and
/// a `Paragraph` stopped drawing partway down. What it stopped drawing was the
/// stamp — leaving availability tones over a reading nobody could date, which
/// is the "reads as live" misreading `probed_at` exists to prevent.
///
/// So the card refuses in place below its own floor, the way the login form
/// does below [`FORM_MIN_H`], and the stamp moved to the top so that even a
/// clipped draw cannot show a tone the stamp has not already dated. Both, not
/// either: the ordering is what holds if a later row is ever added below.
const MODELS_MIN_H: u16 = 6;
/// Header, the count, the symbol list, and the rule. A floor rather than a
/// fixed height: this card is the one that takes the column's remainder.
const UNIVERSE_H: u16 = 4;
/// Header, the palette, and the rule.
const THEME_H: u16 = 3;

/// The bound every owner sentence on the MODELS card passes.
///
/// Sized so a bounded reason still **fits** the slack rather than merely being
/// shorter than it. [`MODELS_H`] leaves four rows; a 38-cell card wraps at most
/// 114 cells into three of them plus the row `wrapped_rows` reserves for a word
/// break; one cell is the leading space and one more is the mark `bounded`
/// adds. A bound the card could not show would send every long reason to the
/// `▾ 1 more` count and make the bound itself unreachable.
///
/// The longest sentence the owner actually writes — "ollama is running at
/// 127.0.0.1:11434 but no models are pulled — pull one with `ollama pull
/// granite3.3:8b`" — is 105 cells and survives uncut. Nothing on the wire is
/// guaranteed to be the owner's, which is the C2 rule now made uniform.
const SAID_MAX: usize = 112;

/// The bound a backend or model name passes. A name is a token, not a sentence:
/// `granite3.3:8b` is thirteen characters and anything past this is not one.
const NAME_MAX: usize = 36;

/// What the desk is pointed at, and whether it can reach it.
fn draw_desk(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(mode) = store.desk_mode() else {
        card(f, area, "desk", vec![absent("the owner sent no desk mode")]);
        return;
    };
    let mut rows = vec![
        kv("mode", or_missing(mode.label.as_ref()), t.text_primary),
        kv("data", or_missing(mode.data.as_ref()), t.text_secondary),
        kv("book", or_missing(mode.book.as_ref()), t.text_secondary),
        kv("lane", lane(mode), t.text_secondary),
    ];
    // The description is the only thing that names the missing credential, so
    // it is rendered whatever the verdict — and toned by the verdict, which is
    // the same rule the status line's chip is drawn by.
    rows.push(kv(
        "alpaca login",
        or_missing(mode.credentials.as_ref()),
        if mode.book_unreachable() {
            t.warning
        } else {
            t.text_secondary
        },
    ));
    rows.push(Line::from(""));
    rows.push(Line::from(Span::styled(
        // Posture, not the build: a featured binary the human did not arm reads
        // GLASS on the status line and must not be told about keys it would
        // refuse. The two the armed window has are named beside the command
        // that is *not* one of them — a login does not switch the desk, and the
        // line reads as one sentence about which is which.
        if store.posture.writes() {
            " /mode switches the desk · a types a login · t tests it"
        } else {
            " read-only — this window cannot switch the desk"
        },
        Style::default().fg(t.text_dim),
    )));
    card(f, area, "desk", rows);
}

/// Which lane the data comes down. Absent stays absent: a desk whose owner did
/// not say is not an offline one.
fn lane(mode: &DeskMode) -> String {
    match mode.offline {
        Some(true) => "offline · synthetic".to_string(),
        Some(false) => "online".to_string(),
        None => MISSING.to_string(),
    }
}

/// The policy every paper solve runs under, and the four limits it is held to.
fn draw_policy(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(policy) = store.policy() else {
        card(f, area, "policy", vec![absent("the owner sent no policy")]);
        return;
    };
    let limits = policy.constraints.clone().unwrap_or_default();
    let rows = vec![
        // Two rows rather than `id · label`: the pair is wider than a
        // half-width card, and the id is the one an operator types.
        kv("policy", or_missing(policy.id.as_ref()), t.accent),
        kv("method", or_missing(policy.label.as_ref()), t.text_primary),
        kv(
            "algorithm",
            or_missing(policy.algorithm_id.as_ref()),
            t.text_secondary,
        ),
        kv(
            "objective",
            or_missing(policy.objective.as_ref()),
            t.text_secondary,
        ),
        kv(
            "solver",
            or_missing(policy.solver.as_ref()),
            t.text_secondary,
        ),
        kv("long only", yes_no(limits.long_only), t.text_primary),
        kv("budget", opt_pct1(limits.budget), t.text_primary),
        // One row for the pair: a floor without its ceiling is half a mandate,
        // and 0% is a real floor rather than an absent one.
        kv("per asset", weight_band(&limits), t.text_primary),
    ];
    card(f, area, "policy", rows);
}

fn weight_band(limits: &Constraints) -> String {
    match (limits.min_weight, limits.max_weight) {
        (None, None) => MISSING.to_string(),
        (min, max) => format!("{} – {}", opt_pct1(min), opt_pct1(max)),
    }
}

/// Why the policy is the one the desk runs, in the owner's words. Wrapped,
/// because it is a sentence rather than a value, and clipped sentences are the
/// class of refusal this workstation spends rows to avoid.
fn draw_rationale(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(rationale) = store
        .policy()
        .and_then(|p| format::text(p.rationale.as_ref()))
    else {
        return;
    };
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" {rationale}"),
            Style::default().fg(t.text_tertiary),
        )))
        .wrap(Wrap { trim: false }),
        area,
    );
}

/// Health and authority, as the owner reports them.
fn draw_system(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(system) = store.system() else {
        card(
            f,
            area,
            "system",
            vec![absent("the owner sent no system status")],
        );
        return;
    };
    let rows = vec![
        kv("desk", or_missing(system.mode.as_ref()), t.text_primary),
        kv("provenance", provenance(system), t.text_secondary),
        // The owner's own health fact about the CLI, and only that. Since the
        // desk grew a second backend this row is no longer the whole answer to
        // "which minds can this desk reach" — the MODELS card directly under
        // this one is, because it can carry both backends, the owner's reason
        // for each, and the stamp that says how old the reading is. Left narrow
        // rather than generalized here: a summary of two backends without its
        // stamp would read as live, which is the exact misreading that stamp
        // exists to prevent.
        kv(
            "claude",
            availability(system.claude_available),
            t.text_secondary,
        ),
        kv("mcp", mcp(system), t.text_secondary),
        kv(
            "proxy",
            availability(system.mcp_proxy_available),
            t.text_secondary,
        ),
        kv(
            "workforce",
            availability(system.workforce_available),
            t.text_secondary,
        ),
        // The one row on this card that is a governance claim rather than a
        // health one: it is what bounds every agent on this desk.
        kv(
            "authority",
            or_missing(system.governed_authority.as_ref()),
            t.accent,
        ),
    ];
    card(f, area, "system", rows);
}

/// Where the cached panel came from and how old it is. Cache-only on the
/// owner's side — never a network fetch from a status poll — so an age here is
/// a fact about the cache rather than about the market.
fn provenance(system: &System) -> String {
    let source = or_missing(system.data_source.as_ref());
    match system.data_age_days {
        Some(days) => format!("{source} · {days} d"),
        None => source,
    }
}

/// Which MCP servers are configured — or why the answer is not a list.
///
/// A config file that exists and does not parse is not the same fact as no
/// file. The owner separates them deliberately (collapsing both into "not
/// configured" once sent an operator to re-add an entry that was already
/// there), so this client may not put them back together.
fn mcp(system: &System) -> String {
    if let Some(error) = format::text(system.mcp_config_error.as_ref()) {
        return error.to_string();
    }
    match system.mcp_servers.is_empty() {
        true => "none configured".to_string(),
        false => system.mcp_servers.join(" "),
    }
}

fn availability(flag: Option<bool>) -> String {
    match flag {
        Some(true) => "available".to_string(),
        Some(false) => "absent".to_string(),
        None => MISSING.to_string(),
    }
}

/// Which minds the desk is using, and how fresh that answer is.
///
/// Read-only, like every other card here. The choice is made through the
/// owner's own route, and D4 brings the keys that reach it; until then this
/// pane claims none, so a key pressed on it falls through to whoever claims it
/// next.
fn draw_models(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(llm) = store.llm() else {
        card(
            f,
            area,
            "models",
            vec![absent("the owner sent no model routing")],
        );
        return;
    };
    // The value column this card actually has, rather than the one it has at
    // the baseline. A composed value wider than this wraps onto an unindented
    // second row and spends a slack row the reasons need.
    let room = (area.width as usize).saturating_sub(LABEL_W + 1);
    let reasons = unreachable_reasons(llm);
    // A reason that exists is either shown whole or counted, and the count
    // needs a row of its own.
    let floor = MODELS_MIN_H + u16::from(!reasons.is_empty());
    if area.height < floor {
        refuse(
            f,
            area,
            format!(
                "the models card needs {floor} rows for the reading and the stamp \
                 that dates it; this column gave it {}.",
                area.height
            ),
        );
        return;
    }
    let mut rows = vec![
        // **First, and that is the point.** This row dates every tone under it,
        // and a column short of rows used to drop the last one — so a backend
        // toned `warning` could render over a reading nobody could date, which
        // is exactly the "reads as live" misreading `probed_at` exists to
        // prevent. Ordering makes a partial draw unreadable-as-live before
        // [`MODELS_MIN_H`] even gets to refuse one.
        kv("probed", probed_row(llm, store.wall), t.text_secondary),
        surface_row("reasoner", llm.reasoner.as_ref(), llm, room),
        // The flag rather than the choice. A model named for a reasoner nobody
        // switched on is a mind the desk is not using, and the owner refuses to
        // infer the switch from the name — so this row is the one that says
        // whether the row above it is in play at all.
        kv(
            "judgment",
            match llm.reasoner_enabled {
                Some(true) => "on".to_string(),
                Some(false) => "off".to_string(),
                None => MISSING.to_string(),
            },
            match llm.reasoner_enabled {
                Some(true) => t.accent,
                _ => t.text_secondary,
            },
        ),
        surface_row("workforce", llm.workforce.as_ref(), llm, room),
    ];
    // Whole sentences or a count, never half of one: the remedy is the last
    // third of the owner's longest reason, so a clipped one is a fix an
    // operator cannot run. The marker costs a row, and it is reserved before
    // anything is dropped rather than after — `views::desk::fit` makes the same
    // reservation, for the same reason.
    let mut budget = (area.height - MODELS_MIN_H) as usize;
    let inner_w = area.width.max(1) as usize;
    let cost = |reason: &String| wrapped_rows(reason.chars().count() + 1, inner_w);
    if reasons.iter().map(cost).sum::<usize>() > budget {
        budget = budget.saturating_sub(1);
    }
    let mut hidden = 0usize;
    for reason in &reasons {
        match cost(reason) <= budget {
            true => {
                budget -= cost(reason);
                rows.push(Line::from(Span::styled(
                    format!(" {reason}"),
                    // Dim: the reason explains the tone on the row above, and a
                    // second warning-coloured line would compete with it.
                    Style::default().fg(t.text_dim),
                )));
            }
            false => hidden += 1,
        }
    }
    if hidden > 0 {
        rows.push(Line::from(Span::styled(
            format!(" ▾ {hidden} more"),
            Style::default().fg(t.text_dim),
        )));
    }
    card(f, area, "models", rows);
}

/// The owner's reason for every backend a surface runs on and cannot reach.
///
/// **Only the backends a surface runs on**, and each of those once. A sentence
/// about a backend nothing here uses is noise beside one about a desk that
/// cannot do what it was configured to do — the catalog names every backend the
/// desk knows, not the two it is using — and a desk with both surfaces on the
/// same down daemon would otherwise print one sentence twice.
fn unreachable_reasons(llm: &LlmConfig) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    let mut out = Vec::new();
    for name in [
        surface_backend(llm.reasoner.as_ref()),
        surface_backend(llm.workforce.as_ref()),
    ]
    .into_iter()
    .flatten()
    {
        if seen.contains(&name) {
            continue;
        }
        seen.push(name.clone());
        let Some(entry) = llm.backend(&name) else {
            continue;
        };
        // `Some(false)` only: a backend nothing has probed has no reason to
        // explain, and one that answered yes has nothing to explain either.
        if entry.available != Some(false) {
            continue;
        }
        if let Some(reason) = format::text(entry.reason.as_ref()) {
            out.push(format::bounded(reason, SAID_MAX));
        }
    }
    out
}

/// How many rows a line of `width` cells takes when wrapped at `inner`.
///
/// Rounds up the way the login form's `wanted` does, and for the same reason:
/// `Paragraph` breaks at word boundaries, so a line one cell past the width can
/// cost a whole row more than the division says. Over-reserving costs a blank
/// row; under-reserving clips the remedy off a sentence, which is the failure
/// this card is sized to avoid.
fn wrapped_rows(width: usize, inner: usize) -> usize {
    width.div_ceil(inner.max(1)).max(1) + usize::from(width > inner)
}

/// One surface's row, toned by what the last reading said about its backend.
fn surface_row(
    label: &str,
    surface: Option<&LlmSurface>,
    llm: &LlmConfig,
    room: usize,
) -> Line<'static> {
    let t = theme();
    let available = surface_backend(surface)
        .and_then(|name| llm.backend(&name))
        .and_then(|entry| entry.available);
    kv(
        label,
        surface_value(surface, room),
        match available {
            Some(true) => t.positive,
            Some(false) => t.warning,
            // Nothing has asked. Unknown is not broken, and toning it as a
            // problem would train an operator to read past the row that is one.
            None => t.text_primary,
        },
    )
}

/// The backend one surface runs on, bounded, or nothing.
fn surface_backend(surface: Option<&LlmSurface>) -> Option<String> {
    format::text(surface?.backend.as_ref()).map(|name| format::bounded(name, NAME_MAX))
}

/// `said`, occupying at most `room` cells.
///
/// `format::bounded` bounds the *text* and adds a mark, so its result is one
/// cell longer than its argument; a column is a hard width, and this is the
/// spelling that counts the mark against it.
fn to_room(said: &str, room: usize) -> String {
    match said.chars().count() > room {
        // One cell of the room is the mark that says it was cut.
        true => format::bounded(said, room.saturating_sub(1)),
        false => said.to_string(),
    }
}

/// What one surface runs, in the owner's words — except on the claude path.
///
/// **The A3 fact this row exists to keep visible.** On claude the tier map owns
/// the model (`model_routing.TIER_MODEL` is "the one place"), so the surface's
/// model half is not a choice: `inherit` is what the desk stores, and the tiers
/// then pick per role. A bare `claude · haiku` would read as a setting an
/// operator could make, and making it would change nothing — which is the one
/// thing a picker must never offer.
///
/// So `inherit` renders as what it *means* rather than how it is spelled, the
/// way `lane` renders a bool as the words for it. A model that is **not**
/// `inherit` is named and marked ignored rather than quietly rewritten to
/// `inherit`: hiding it would be this client lying about the config, and
/// printing it bare would be the desk lying about what it does with it.
///
/// Both forms are short because the value column is 24 cells at the baseline
/// (`LABEL_W` inside the card's half width). `claude · inherit (tiers decide)`
/// is 31 and wraps onto an unindented second row, spending one of the slack
/// rows [`MODELS_H`] reserves for the availability reasons — the one thing on
/// this card that must not be clipped.
///
/// **The composed value is fitted to `room`, not just its tokens.** A per-token
/// bound is no bound at all here: a real Claude id makes
/// `claude · claude-opus-4-5-20260101 ignored` — 41 cells past a 24-cell
/// column, which wraps and spends the reason slack. What goes is the **id's
/// tail**, never the marker: a value cut to `claude · claude-… ignored` still
/// says the name changes nothing, while one cut to `claude · claude-opus-4-5…`
/// has lost the only word that said so, and reads as a setting.
fn surface_value(surface: Option<&LlmSurface>, room: usize) -> String {
    let Some(surface) = surface else {
        return MISSING.to_string();
    };
    // Half a pair is not a pair: a backend with no model names nothing that can
    // run, and a model with no backend names nothing that can run it.
    let (Some(backend), Some(model)) = (
        format::text(surface.backend.as_ref()),
        format::text(surface.model.as_ref()),
    ) else {
        return MISSING.to_string();
    };
    let (backend, model) = (
        format::bounded(backend, NAME_MAX),
        format::bounded(model, NAME_MAX),
    );
    // The marker only ever sits beside `claude`, so the head it is measured
    // against is a known nine cells and cannot itself overflow the room.
    let marker = match (backend.as_str(), model.as_str()) {
        ("claude", "inherit") => return to_room(&format!("{backend} · tiers decide"), room),
        ("claude", _) => " ignored",
        _ => "",
    };
    let head = format!("{backend} · ");
    let model = to_room(
        &model,
        room.saturating_sub(head.chars().count() + marker.chars().count()),
    );
    match marker.is_empty() {
        true => to_room(&format!("{head}{model}"), room),
        false => format!("{head}{model}{marker}"),
    }
}

/// How old the availability reading is.
///
/// Three answers, and they are three different facts. A stamp measured against
/// a clock the caller passed is an age; a stamp with no clock to measure it
/// against is the owner's own wall time, which asserts no duration at all; and
/// no stamp at all is a desk nothing has asked yet — A2's first state, which
/// the owner serves from startup until the picker's route probes once. A stamp
/// this client cannot read is `--`, because that is a contract failure rather
/// than any of the three.
fn probed_row(llm: &LlmConfig, now: Option<i64>) -> String {
    let Some(stamp) = format::text(llm.probed_at.as_ref()) else {
        return "not yet".to_string();
    };
    let stamp = stamp.to_string();
    match format::since(Some(&stamp), now) {
        Some(age) => format!("{age} ago"),
        None => format::clock(Some(&stamp))
            .map(|at| format!("at {at}"))
            .unwrap_or_else(|| MISSING.to_string()),
    }
}

/// What this desk is watching — the polled universe, named as such.
///
/// The mandate's whitelist is not in the snapshot. What *is* in it is the
/// market section the owner built from that whitelist, plus anything the book
/// holds outside it, and a client that labelled that "the mandate universe"
/// would be asserting a configuration it cannot see.
fn draw_universe(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let symbols = store.universe();
    if symbols.is_empty() {
        card(
            f,
            area,
            "universe",
            vec![absent("no universe in the last snapshot")],
        );
        return;
    }
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let rows = Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(inner);
    f.render_widget(
        Paragraph::new(vec![
            panel_header("universe"),
            kv(
                "watching",
                format!("{} symbols", symbols.len()),
                t.text_primary,
            ),
        ]),
        rows[0],
    );
    // Wrapped rather than clipped: a symbol list that lost its tail reads as a
    // smaller universe than the desk is actually holding.
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" {}", symbols.join(" ")),
            Style::default().fg(t.cyan),
        )))
        .wrap(Wrap { trim: false }),
        rows[1],
    );
}

fn draw_theme(f: &mut Frame, area: Rect) {
    let t = theme();
    card(
        f,
        area,
        "theme",
        vec![kv("palette", palette().to_string(), t.text_primary)],
    );
}

/// One headed card: the header, its rows, and the rule the block reserves.
///
/// Wrapped rather than clipped. Every row here is sized to fit at the baseline
/// width, but a value the owner made longer than this client expected — a
/// credential error, an MCP parse failure — is a sentence, and half a sentence
/// about why the desk cannot log in is worse than a row that took two lines.
/// `trim: false`, so a wrapped continuation keeps the indentation that says it
/// belongs to the row above rather than starting a new one.
fn card(f: &mut Frame, area: Rect, title: &str, rows: Vec<Line<'static>>) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines = vec![panel_header(title)];
    lines.extend(rows);
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

/// A label/value row, aligned so a column of them reads as a column.
fn kv(label: &str, value: String, tone: ratatui::style::Color) -> Line<'static> {
    let t = theme();
    Line::from(vec![
        Span::styled(
            format!(" {label:<LABEL_W$}"),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(value, Style::default().fg(tone)),
    ])
}

/// What a card says when the owner sent nothing for it. Stated rather than left
/// blank: "nothing configured" and "this pane is broken" must not look the same.
fn absent(what: &str) -> Line<'static> {
    Line::from(Span::styled(
        format!(" {what}"),
        Style::default().fg(theme().text_dim),
    ))
}

fn or_missing(value: Option<&String>) -> String {
    format::text(value).unwrap_or(MISSING).to_string()
}

fn yes_no(flag: Option<bool>) -> String {
    match flag {
        Some(true) => "yes".to_string(),
        Some(false) => "no".to_string(),
        None => MISSING.to_string(),
    }
}

/// A percent at one decimal, or `--`. Absent may not become a number: a
/// constraint the owner did not send is not a constraint of zero.
fn opt_pct1(value: Option<f64>) -> String {
    value
        .map(format::pct1)
        .unwrap_or_else(|| MISSING.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::LlmBackend;

    #[test]
    fn an_unsent_constraint_never_becomes_a_number() {
        // The whole reason every scalar in the model is an `Option`. `0.0%` in
        // the ceiling row is a mandate that forbids holding anything.
        assert_eq!(opt_pct1(None), MISSING);
        assert_eq!(opt_pct1(Some(0.0)), "0.0%");
        assert_eq!(yes_no(None), MISSING);
        assert_eq!(
            weight_band(&Constraints::default()),
            MISSING,
            "neither end sent is no band at all"
        );
        // One end sent is still a band — with the other end honestly absent,
        // rather than the whole row disappearing and hiding the half that is
        // known.
        assert_eq!(
            weight_band(&Constraints {
                max_weight: Some(0.4),
                ..Default::default()
            }),
            "-- – 40.0%"
        );
    }

    #[test]
    fn a_config_that_does_not_parse_reads_differently_from_no_config() {
        let broken = System {
            mcp_config_error: Some("JSONDecodeError: line 3".into()),
            ..Default::default()
        };
        assert!(mcp(&broken).contains("JSONDecodeError"));
        assert_eq!(mcp(&System::default()), "none configured");
        // `Some("")` is absent, as everywhere: an owner that sent an empty
        // error string is an owner reporting no error.
        let quiet = System {
            mcp_config_error: Some(String::new()),
            mcp_servers: vec!["qlab-operator".into()],
            ..Default::default()
        };
        assert_eq!(mcp(&quiet), "qlab-operator");
    }

    #[test]
    fn a_claude_surface_never_shows_a_model_name_the_tiers_will_ignore() {
        // The A3 fact this row exists to keep visible: on the claude path the
        // tier map owns the model, so a name in this column would read as a
        // choice an operator could make and would change nothing.
        // The card's value column at the baseline width.
        const ROOM: usize = 24;
        let claude = |model: &str| {
            surface_value(
                Some(&LlmSurface {
                    backend: Some("claude".into()),
                    model: Some(model.into()),
                }),
                ROOM,
            )
        };
        // `inherit` rendered as what it means, never as an editable-looking
        // name for a choice that does nothing.
        assert_eq!(claude("inherit"), "claude · tiers decide");
        // A model the desk will not honour is named *and* marked, rather than
        // hidden behind `inherit` — hiding it would be this client lying about
        // the config, and printing it bare would be the desk lying about what
        // it does with it.
        assert_eq!(claude("haiku"), "claude · haiku ignored");
        // Every other backend runs the model it names.
        assert_eq!(
            surface_value(
                Some(&LlmSurface {
                    backend: Some("ollama".into()),
                    model: Some("granite3.3:8b".into()),
                }),
                ROOM
            ),
            "ollama · granite3.3:8b"
        );
        // Absent stays absent, and half a pair is not a pair: a backend with no
        // model is a surface nobody can say what runs on.
        assert_eq!(surface_value(None, ROOM), MISSING);
        assert_eq!(surface_value(Some(&LlmSurface::default()), ROOM), MISSING);
        assert_eq!(
            surface_value(
                Some(&LlmSurface {
                    backend: Some("ollama".into()),
                    // `Some("")` is absent, as everywhere in this client.
                    model: Some(String::new()),
                }),
                ROOM
            ),
            MISSING
        );
    }

    #[test]
    fn a_real_model_id_is_cut_at_its_tail_and_never_at_the_word_that_matters() {
        // A per-token bound is no bound at all: `claude-opus-4-5-20260101` is
        // inside `NAME_MAX` and the value it composes is 41 cells against a
        // 24-cell column, which wraps and spends the reason slack.
        const ROOM: usize = 24;
        let long = surface_value(
            Some(&LlmSurface {
                backend: Some("claude".into()),
                model: Some("claude-opus-4-5-20260101".into()),
            }),
            ROOM,
        );
        assert!(long.chars().count() <= ROOM, "{long} is {}", long.len());
        // The marker is the honest half and survives the cut: without it the
        // row reads as a setting an operator could make.
        assert!(long.ends_with(" ignored"), "{long}");
        assert!(long.starts_with("claude · claude"), "{long}");
        assert!(
            long.contains('…'),
            "a silent cut reads as the whole id: {long}"
        );
        // A narrower card cuts further into the id and still keeps the word.
        let narrow = surface_value(
            Some(&LlmSurface {
                backend: Some("claude".into()),
                model: Some("claude-opus-4-5-20260101".into()),
            }),
            20,
        );
        assert!(narrow.chars().count() <= 20, "{narrow}");
        assert!(narrow.ends_with(" ignored"), "{narrow}");
    }

    #[test]
    fn only_the_backends_a_surface_runs_on_get_their_reason_rendered() {
        // The owner's catalog names every backend this desk knows, not the two
        // it is using. A sentence about one nothing here runs on is noise
        // beside one about a desk that cannot do what it was configured to do.
        let claude = || {
            Some(LlmSurface {
                backend: Some("claude".into()),
                model: Some("inherit".into()),
            })
        };
        let llm = LlmConfig {
            reasoner: claude(),
            workforce: claude(),
            availability: vec![
                LlmBackend {
                    name: Some("claude".into()),
                    available: Some(true),
                    reason: Some("claude CLI on PATH".into()),
                },
                LlmBackend {
                    name: Some("ollama".into()),
                    available: Some(false),
                    reason: Some("ollama is not running at 127.0.0.1:11434".into()),
                },
            ],
            ..Default::default()
        };
        assert!(
            unreachable_reasons(&llm).is_empty(),
            "no surface runs on ollama, so its outage is not this desk's problem"
        );
        // And a desk that *is* on it says so once, not once per surface.
        let ollama = || {
            Some(LlmSurface {
                backend: Some("ollama".into()),
                model: Some("granite3.3:8b".into()),
            })
        };
        let both = LlmConfig {
            reasoner: ollama(),
            workforce: ollama(),
            ..llm
        };
        assert_eq!(unreachable_reasons(&both).len(), 1);
    }

    #[test]
    fn a_desk_that_has_never_probed_reads_differently_from_one_whose_stamp_will_not_parse() {
        let probed = "2026-08-02T18:54:17.856581+00:00".to_string();
        let block = |stamp: Option<String>| LlmConfig {
            probed_at: stamp,
            ..Default::default()
        };
        // The A2 carry: this block is the LAST reading, so the row says how old
        // it is rather than presenting it as live.
        assert_eq!(
            probed_row(&block(Some(probed.clone())), Some(1_785_696_869)),
            "12s ago"
        );
        // No clock to measure against — the owner's own wall time, which
        // asserts nothing about durations between two machines.
        assert_eq!(probed_row(&block(Some(probed)), None), "at 18:54:17");
        // Nothing has asked the backends yet. Not the same fact as a reading
        // this client could not read, and `--` for both would merge them.
        assert_eq!(probed_row(&block(None), Some(1_785_696_869)), "not yet");
        assert_eq!(
            probed_row(&block(Some(String::new())), Some(1_785_696_869)),
            "not yet"
        );
        assert_eq!(
            probed_row(&block(Some("yesterday".into())), Some(1_785_696_869)),
            MISSING
        );
    }

    #[test]
    fn the_lane_and_the_login_are_read_off_the_owner_and_not_guessed() {
        assert_eq!(lane(&DeskMode::default()), MISSING);
        assert_eq!(
            lane(&DeskMode {
                offline: Some(false),
                ..Default::default()
            }),
            "online"
        );
        // The simulated book has no login to be broken.
        let sim = DeskMode {
            book: Some("simulated".into()),
            credentials_ok: Some(false),
            ..Default::default()
        };
        assert!(!sim.book_unreachable());
        // Silence about the book that can place real orders is not a clean
        // login — the owner always sends the flag.
        let quiet = DeskMode {
            book: Some("alpaca".into()),
            ..Default::default()
        };
        assert!(quiet.book_unreachable());
    }
}
