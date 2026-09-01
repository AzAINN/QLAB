//! SETTINGS — what this desk is configured by, and where an operator changes it.
//!
//! Eight cards of facts an operator would otherwise have to assemble from
//! `mandate.yaml`, `.mcp.json`, a shell prompt and whatever the last `/mode`
//! did. Everything drawn is the owner's own answer; nothing here is composed,
//! defaulted, or inferred — with one deliberate exception, the NEWS draft,
//! which is marked as an edit precisely so it cannot be mistaken for one.
//!
//! **The cards are the routing.** Eight of them, and the four that carry keys
//! carry different ones, so a pane-level key list was either wrong about four
//! cards or silent about all eight. The arrows move a focus between cards, the
//! focused card's header is tinted, and a key means whatever *that* card says
//! it means — which is why each card states its own keys on the rule its block
//! already reserves rather than in a row it would have to win from its content.
//! A footer competing for rows is the first thing a short column drops, and
//! what it would drop here is the sentence that says whether there is a key at
//! all.
//!
//! **What an operator may change here, and what they may not.** Five things:
//! which login the owner stores, which mind each surface runs, the data lane,
//! the news stack, and — since this branch — the operational method this desk
//! solves with and how many names it may hold. Every one is the owner's own
//! route and none of them widens what this desk can *do*. A stored login makes
//! `LIVE·ALPACA` choosable; the lane picker sends exactly what `/mode` sends
//! and is refused by exactly the same owner; the news routes write `.env` and
//! the process environment; and the method route merges two keys into the
//! mandate. None takes a registry lock, none touches a plan, an approval or a
//! posture, and every gate between a plan and a fill is unmoved by all of it.
//!
//! The method is the one that can make the desk *refuse*, and deliberately: a
//! cap below what the chosen method holds applies, and the plan it breaks
//! arrives minutes later out of the owner's own mandate check. That ruling is
//! the owner's; what this pane owes it is that the sentence saying so is drawn
//! where it cannot be missed, which is why the METHOD card's body is a budget
//! and the warning is what the lists yield to.
//!
//! Three refusals are made *here* rather than left to the owner, and all for
//! the same reason: the remedy is a key or a box on this pane and the owner's
//! sentence cannot name one it does not know this pane has. Ticking a source
//! the desk cannot read is refused on the keystroke; saving a stack with
//! `edgar` in it and no contact points at `c`; and a cap above the universe
//! this desk watches is refused with the box still open and the number still in
//! it. Everything else travels and comes back in the owner's own words —
//! including every refusal about the method itself, which is the owner's
//! catalog and not this client's.
//!
//! Focus is a fact about an armed window. A glass one marks no card — a
//! highlight that never moves under the arrows reads as a hung client, which is
//! the reason AUDIT's arrows decline rather than swallow — and every card there
//! says `read-only` instead, which is the pane-level line the cards inherited.
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

use crate::bus::NewsMember;
use crate::cmd::Command;
#[cfg(feature = "operator")]
use crate::cmd::{self, ModelChoice};
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
/// Only the picker names one: the card reads the entries off the payload it was
/// handed, and a glass build has no picker to build a list for.
#[cfg(feature = "operator")]
use crate::model::MethodEntry;
use crate::model::{
    Constraints, DeskMode, LlmConfig, LlmSurface, MethodSettings, NewsSettings, NewsSource,
    RightsFlags, System,
};
use crate::store::Store;
use crate::theme::{palette, theme};
use crate::ui::views::View;
use crate::ui::widgets::{panel_block, panel_header, refuse};
#[cfg(feature = "operator")]
use crossterm::event::{KeyCode, KeyModifiers, MouseButton, MouseEventKind};
use crossterm::event::{KeyEvent, MouseEvent};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::Style,
    text::{Line, Span},
    widgets::{Block, Paragraph, Wrap},
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

/// The desk card's width in the top band, and the reason it is a length rather
/// than a ratio.
///
/// Every fixed row on DESK is a label and a short word; the one long value is
/// the credential description, which is a sentence that wraps into the slack
/// [`DESK_H`] already reserves for it. Width past this buys that card almost
/// nothing. NEWS beside it spends every cell it is given — a source's cost and
/// its last outcome are both sentences in a column — so the band gives DESK
/// what it needs and NEWS the rest.
///
/// It is also what keeps both cards' rules legible: a card states its own keys
/// on the block's bottom line, and a `Paragraph` clips that from the right, so
/// a card one cell too narrow loses the last key it names rather than wrapping
/// it.
const DESK_W: u16 = 36;

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

/// The switcher's own floor, in rows of the view's area.
///
/// Lower than [`FORM_MIN_H`] because it holds less: a header, a blank, at least
/// one offer, the reasoner's switch, and the key line. Below this the box would
/// have room for a header and nothing else while still taking every keystroke,
/// which is the armed-and-invisible state every box on this client refuses at.
#[cfg(feature = "operator")]
const SWITCH_MIN_H: u16 = 12;

/// Its width. Wide enough for the surface column, the longest `backend:model`
/// pair the owner serves, and the `now` marker beside it.
#[cfg(feature = "operator")]
const SWITCH_W: u16 = 58;

/// How many offers the box lists at once, before the `▾ n more` marker.
#[cfg(feature = "operator")]
const SWITCH_ROWS: usize = 8;

/// The contact box's own floor, in rows of the view's area.
///
/// Lower than [`FORM_MIN_H`] and higher than [`SWITCH_MIN_H`]: it holds one
/// field, the shape the SEC asks for, what is already stored, and a key line.
/// Below this it would be a box with room for a header and nothing else while
/// still taking every keystroke, which is the state every box here refuses at.
#[cfg(feature = "operator")]
const CONTACT_MIN_H: u16 = 11;

/// Its width. Wide enough for the example contact beside the label column.
#[cfg(feature = "operator")]
const CONTACT_W: u16 = 58;

/// The holdings-cap box's own floor, in rows of the view's area.
///
/// The same shape as the contact box and one row shorter: it holds one field,
/// the bound the owner will hold it to, what clearing it means, and a key line.
/// Below this it would be a box with room for a header and nothing else while
/// still taking every keystroke, which is the state every box here refuses at.
#[cfg(feature = "operator")]
const CAP_MIN_H: u16 = 10;

/// Its width. Wide enough for the bound sentence beside the label column.
#[cfg(feature = "operator")]
const CAP_W: u16 = 58;

/// The longest cap an operator may type before the box stops taking keys.
///
/// Three digits, not a parse bound: the owner's own limit is the size of the
/// mandated universe and this client does not own that number. What this stops
/// is a held key filling a `String` with digits that can no longer be read as
/// an `i64` at all — a refusal about arithmetic where the operator asked about
/// a mandate.
#[cfg(feature = "operator")]
const CAP_DIGITS: usize = 3;

/// The one source whose name this client has to know.
///
/// Not a second copy of the owner's catalog — every other rule about every
/// other source is read off the payload. This name is here because the *remedy*
/// for one refusal is a key on this card, and the owner's sentence cannot name
/// a key it does not know this pane has.
const EDGAR: &str = "edgar";

/// Where a source row's note starts: the cursor, the tick, the name and the
/// tier, which are fixed-width so a column of them reads as a column.
const NOTE_X: usize = 3 + 4 + 8 + 10;

/// The first catalog row's offset inside the NEWS card: the header, the lane
/// and the resolved stack above it.
///
/// Arithmetic rather than published per row, because the card's shape is fixed
/// and a click has to be answered about the frame that is on screen. It is the
/// one thing here that would drift silently if a row were inserted above the
/// catalog, which is why it is a constant with the rows named in its doc.
#[cfg(feature = "operator")]
const NEWS_TOP: u16 = 3;

/// Where the first rights row is drawn inside the MODELS card: the header, then
/// the four rows the reading and its stamp take.
///
/// A constant rather than a count the card returns, because these three rows
/// are inside [`MODELS_MIN_H`]: a card too short for them draws a refusal and
/// no rows at all, which is the one state the caller checks for.
///
/// Gated with the click it answers: the glass build draws the rows and records
/// no rectangle over them, because there is nothing a pointer could press.
#[cfg(feature = "operator")]
const RIGHTS_TOP: u16 = 5;

/// How many rights the owner has. Three, and the model is what says so — a
/// second list here is how a reader and a writer come to disagree about a key.
///
/// Ungated: the glass build draws the same three rows and simply cannot press
/// any of them.
const RIGHTS_ROWS: usize = RightsFlags::FIELDS.len();

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

/// What a card says while it is waiting, and what it says to the second press.
///
/// One string for both, because they are one statement: the card is busy, and
/// the key that was just pressed did nothing. Two wordings would let the wait
/// line and the refusal drift into disagreeing about whether anything was sent.
///
/// Ungated, unlike the keys that set it: `method_card` is one renderer for both
/// builds and it is the function that draws the wait line, so the constant has
/// to exist in a binary that can never be waiting. Gating it was how the glass
/// build stopped compiling — see the note on [`Card::Method`]'s own state.
const ASKING: &str = "asking the owner…";

/// One card of this pane, as a thing a key can be aimed at.
///
/// The order is the order they are drawn — the full-width card, then the left
/// column, then the right — because an operator walking the arrows is walking
/// what they can see. Up and Down rather than a grid walk: the pane is two
/// columns of *different* cards rather than a table, so Left and Right would
/// promise a geometry the layout does not keep (POLICY's neighbour is SYSTEM at
/// one height and UNIVERSE at another).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Card {
    #[default]
    Desk,
    News,
    /// How this desk solves, and how many names it may hold. The one card that
    /// changes what the desk *proposes* rather than what it reads or displays —
    /// and still not what it may book.
    Method,
    Policy,
    Theme,
    System,
    Models,
    Universe,
}

impl Card {
    /// The walk order, and only the walk has one: the glass build has no focus
    /// to move, so nothing there reads this.
    #[cfg(feature = "operator")]
    const ALL: [Card; 8] = [
        Card::Desk,
        Card::News,
        Card::Method,
        Card::Policy,
        Card::Theme,
        Card::System,
        Card::Models,
        Card::Universe,
    ];

    /// What this card offers the operator, in its own words.
    ///
    /// The posture, never the build: a featured binary the human did not arm
    /// reads GLASS and may not be told about keys it would refuse. Every card
    /// answers, including the four that have nothing — "there is nothing here"
    /// and a card that has gone quiet are two readings this pane spends a row
    /// of chrome to keep apart.
    fn footer(self, writes: bool) -> &'static str {
        match (writes, self) {
            // Terse since the card went half-width: the rule is drawn on the
            // block's own bottom line and a `Paragraph` clips it from the
            // right, so a sentence one cell too long loses the last key it
            // names rather than wrapping.
            (true, Card::Desk) => "a login · t test · m switch lane",
            (true, Card::News) => "space · c contact · s save · v verify",
            // Two keys at the card's own width. The rule has 38 cells here and
            // a `Paragraph` clips it from the right, so the longer sentence
            // ("m switches a model") lost the key that grants a right — which
            // is the one key on this card that changes the desk.
            (true, Card::Models) => "m model · ↑↓ space grants a right",
            // Two keys and two words, at the card's own width. `k` rather than
            // `c`, which the NEWS card already claims one card up, and rather
            // than `h` — the pane has no vim walk to collide with, but a key
            // that reads as "help" on a workstation with a help overlay is one
            // an operator presses expecting something else.
            (true, Card::Method) => "m method · k cap",
            (true, _) => "no keys on this card",
            // The pane-level line the cards inherited, kept whole on the two
            // cards it was always about.
            (false, Card::Desk) => "read-only — cannot switch the desk",
            // What this card changes is what the desk *reads*, which is not
            // what it can do — and the rule has 38 cells at this width, which
            // is why the sentence is this one rather than the longer one that
            // says "what this desk reads".
            (false, Card::News) => "read-only — cannot change what is read",
            // What this card changes is how the desk *solves*, which is still
            // not what it may book — and the rule has 38 cells here, which is
            // what picks this sentence over the longer one.
            (false, Card::Method) => "read-only — cannot change the method",
            // What this card changes is what Atlas is *offered*, which is not
            // what the owner will accept — and the rule has 38 cells here,
            // which is what picks this sentence over a longer one.
            (false, Card::Models) => "read-only — cannot grant a right",
            (false, _) => "read-only",
        }
    }

    /// The words on this card's own rule a click may press, and the key each
    /// one stands for.
    ///
    /// Read out of [`footer`](Card::footer) at draw time rather than given
    /// coordinates here: the rule is one string and the words are found in it,
    /// so a reworded footer moves its own affordances instead of leaving a
    /// rectangle over whatever the new sentence put there.
    ///
    /// A click sends the key. There is no second path — `on_mouse` hands the
    /// same `KeyCode` to the same router — so a word cannot come to mean
    /// something the card does not say.
    #[cfg(feature = "operator")]
    fn words(self) -> &'static [(&'static str, char)] {
        match self {
            Card::Desk => &[("switch", 'm')],
            Card::News => &[("contact", 'c'), ("save", 's'), ("verify", 'v')],
            Card::Models => &[("model", 'm')],
            Card::Method => &[("method", 'm'), ("cap", 'k')],
            _ => &[],
        }
    }
}

/// Where the operator is looking, and what they have typed. Never what the desk
/// says — that is the `Store`'s.
///
/// The cards retain no cursor and no page of their own; what this holds is
/// which card is listening, and whichever box the operator opened on it.
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
    /// The model switcher, while it is open. Same discipline as the form: below
    /// its floor the pane refuses to draw the box and the next keystroke
    /// retires it, so there is no armed-and-invisible state.
    #[cfg(feature = "operator")]
    switch: Option<Switch>,
    /// Which card the keys are aimed at.
    ///
    /// Gated with the keys it routes: the glass build has no card that answers
    /// anything, so it holds no focus rather than holding one that never moves.
    /// `Card::Desk` is where it opens, which is the card an operator arriving
    /// on this pane is already reading.
    #[cfg(feature = "operator")]
    focus: std::cell::Cell<Card>,
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
    /// What the operator has picked on the NEWS card and not yet sent.
    #[cfg(feature = "operator")]
    news: Draft,
    /// What the METHOD card is holding: the cap box while it is open, the one
    /// request in flight, and whatever was last said back.
    #[cfg(feature = "operator")]
    method: MethodDraft,
    /// Which right the cursor is on, the one toggle in flight, and whatever
    /// was last said back.
    #[cfg(feature = "operator")]
    rights: RightsDraft,
    /// How many source rows the last frame drew, published the way `area` is:
    /// the cursor is clamped against the catalog that is on screen, and the
    /// catalog moves under it every time the owner answers.
    #[cfg(feature = "operator")]
    news_rows: std::cell::Cell<usize>,
    /// Whether the login box was opened *by* the lane picker, so the picker
    /// can come back once the desk can read a credential.
    ///
    /// A one-shot rather than a mode: it is taken on the first answer, so a
    /// login stored from the `a` key later cannot reopen a picker nobody
    /// asked for.
    #[cfg(feature = "operator")]
    relane: bool,
    /// Where the last frame drew the things a pointer may press.
    ///
    /// Published by `draw` for the reason ATLAS publishes its clickable words:
    /// a click is answered about the frame in front of the operator, never
    /// about one not yet painted. Cleared at the top of every frame, including
    /// the ones that draw no cards — rectangles left over from a wider frame
    /// are the same lie one resize later.
    #[cfg(feature = "operator")]
    hits: std::cell::RefCell<Hits>,
}

/// The rectangles one frame of this pane left behind, by what a click on each
/// one means.
///
/// Three lists rather than one tagged one, because the three are recorded at
/// different points of the draw and read in a fixed order — a word outranks a
/// row and a row outranks a header, which is the order they are drawn in.
#[cfg(feature = "operator")]
#[derive(Default)]
struct Hits {
    headers: Vec<(Rect, Card)>,
    /// The card is carried beside the index because two cards now draw rows a
    /// click may press, and the index alone is ambiguous between them: a
    /// rectangle recorded by MODELS and read as NEWS' would tick a news source
    /// from a click on a right.
    rows: Vec<(Rect, Card, usize)>,
    words: Vec<(Rect, Card, char)>,
}

/// What the pointer is over.
#[cfg(feature = "operator")]
enum Hit {
    Header(Card),
    Row(Card, usize),
    Word(Card, char),
}

#[cfg(feature = "operator")]
impl Hits {
    fn clear(&mut self) {
        self.headers.clear();
        self.rows.clear();
        self.words.clear();
    }

    fn at(&self, column: u16, row: u16) -> Option<Hit> {
        let over = |r: &Rect| row == r.y && column >= r.x && column < r.x.saturating_add(r.width);
        if let Some((_, card, key)) = self.words.iter().find(|(r, _, _)| over(r)) {
            return Some(Hit::Word(*card, *key));
        }
        if let Some((_, card, at)) = self.rows.iter().find(|(r, _, _)| over(r)) {
            return Some(Hit::Row(*card, *at));
        }
        self.headers
            .iter()
            .find(|(r, _)| over(r))
            .map(|(_, card)| Hit::Header(*card))
    }
}

/// What the operator has picked on the NEWS card, before any of it is sent.
///
/// Nothing here is a fact about the desk. `picked` stays `None` until a key or
/// a click touches a row, so the card draws the *owner's* answer rather than a
/// copy made when the pane opened — a copy would go stale the first time the
/// stack moved under it, and would then be an edit mark over a change nobody
/// made.
#[cfg(feature = "operator")]
#[derive(Default)]
struct Draft {
    picked: Option<Vec<String>>,
    /// The EDGAR contact typed into the box, kept for the one POST that
    /// carries it. It is an identity a public archive is told rather than a
    /// secret, so it is a `String` and not a `Secret` — and it is still drawn
    /// nowhere but the box it is typed into, and named in no note or toast.
    contact: Option<String>,
    /// The contact box, while it is open.
    contact_box: Option<String>,
    at: usize,
    /// What the last verify said, per member.
    ///
    /// Held here rather than read back off the GET, because the two are
    /// different questions: `outcomes` is what the desk's own news window last
    /// did, and this is what the owner found when the operator *asked it to
    /// look*. Discarded by the next save, which is a request about a stack this
    /// answer may no longer describe.
    verified: Vec<NewsMember>,
    /// The request in flight, if there is one, and whether it asked for a
    /// check.
    ///
    /// One request at a time, so a held key cannot put two writes of the
    /// operator's `.env` on the wire — and the payload is what the card draws
    /// while it waits. `Some(false)` and `Some(true)` are two different waits:
    /// a save is one round trip, and a check is one live window per source,
    /// which the owner's own catalog puts at 43–75s for `gdelt` alone. A card
    /// that drew the same sentence for both would leave an operator watching a
    /// still frame for minutes with nothing saying why.
    sending: Option<bool>,
    /// What this card, or the owner, last said about a change. Retired by the
    /// next keystroke, like the switcher's note.
    note: Option<String>,
}

/// What the MODELS card is holding while the owner is asked about a right.
///
/// **No draft of the rights themselves.** Nothing here is staged: a right is
/// sent the moment `Space` is pressed on it, so there is nothing for `Esc` to
/// discard and nothing that can disagree with what the desk reports. The card
/// draws the owner's own answer and only the owner's, which is what lets a row
/// say what Atlas is being offered rather than what somebody pressed.
#[cfg(feature = "operator")]
#[derive(Default)]
struct RightsDraft {
    /// Which of the three rows the cursor is on.
    at: usize,
    /// The field of the toggle in flight, if there is one.
    ///
    /// The **field**, not a bool, so the wait is retired by its own answer and
    /// never by another key's broken request — the distinction
    /// `bus::Wrote::RightFailed` exists for. One at a time, so a held key
    /// cannot put two `desk.rights_changed` rows on the owner's audit bus for
    /// one decision — and refused *out loud*, because a key that silently did
    /// nothing reads as a dead card rather than a busy one.
    sending: Option<&'static str>,
    /// What this card, or the owner, last said about a change. Retired by the
    /// next keystroke, like the switcher's note.
    note: Option<String>,
}

/// What the METHOD card is holding, before and while the owner is asked.
///
/// **No draft of the choice itself.** Unlike NEWS, nothing here is staged: a
/// method is sent the moment Enter is pressed on it and a cap the moment Enter
/// leaves the box, so there is nothing for `Esc` to discard and nothing that
/// can disagree with what the desk reports. The card draws the owner's own
/// answer and only the owner's — which is what lets the cap row say what is in
/// force rather than what somebody typed.
#[cfg(feature = "operator")]
#[derive(Default)]
struct MethodDraft {
    /// The cap box, while it is open, holding what has been typed into it.
    cap_box: Option<String>,
    /// Whether a request is in flight.
    ///
    /// One at a time, so a held key cannot put two `mandate_override` rows on
    /// the owner's audit bus for one decision — and refused *out loud*, because
    /// a key that silently did nothing reads as a dead card rather than a busy
    /// one.
    sending: bool,
    /// What this card, or the owner, last said about a change. Retired by the
    /// next keystroke, like the switcher's note.
    note: Option<String>,
}

impl View for SettingsView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        // Published first, and on every frame including the ones that draw no
        // cards: this is what a later keystroke reads to decide whether the
        // form may open, and an area only recorded when the pane already fitted
        // could never report that it stopped fitting.
        self.publish(area);
        self.forget_hits();
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
        // DESK and NEWS share the top band. They are one fact between them —
        // which prices this desk reads and which words it reads about them —
        // and the lane row on the left is the thing the card on the right is
        // about, so the adjacency is the pointer.
        //
        // **DESK gave up the full width for it, and that was the choice.** Its
        // credential description is a sentence the owner wrote, and at half
        // the columns it wraps onto a second row rather than being clipped
        // (`Wrap`, `trim: false`) — which is what [`DESK_H`]'s three rows of
        // slack were always reserved for. The alternative was a band of its
        // own under this one, and the pane has no rows to give: the right
        // column already wants twenty-three of the twenty-two it has.
        // Which card is listening, if any. Read once and handed down, so seven
        // cards cannot disagree about it.
        let at = self.focused(store);
        let bands = Layout::vertical([Constraint::Length(DESK_H), Constraint::Min(0)]).split(area);
        let top = Layout::horizontal([Constraint::Length(DESK_W), Constraint::Min(0)])
            .spacing(1)
            .split(bands[0]);
        draw_desk(f, top[0], store, at);
        self.draw_news(f, top[1], store, at);

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
        //
        // METHOD leads the column, because the walk order is the draw order and
        // it is walked after NEWS — and because it is the actionable half of
        // the card under it: METHOD says which method is in force and what else
        // could be, POLICY says what every solve is then held to.
        //
        // **The rationale is what yields for it**, and it is the only slot on
        // this pane that could: the left column has twenty-two rows at the
        // baseline height, POLICY and THEME are fixed at nine and three, and
        // the `Min(0)` under them was nine rows carrying a three-line sentence.
        // At 120x36 the rationale is now drawn in no rows at all and returns as
        // the terminal grows — which is the trade this pane can make, because
        // the owner writes a rationale *per method* and the picker `m` opens
        // renders every one of them.
        let left = Layout::vertical([
            Constraint::Length(METHOD_H),
            Constraint::Length(POLICY_H),
            Constraint::Min(0),
            Constraint::Length(THEME_H),
        ])
        .split(cols[0]);
        self.draw_method(f, left[0], store, at);
        draw_policy(f, left[1], store, at);
        draw_rationale(f, left[2], store);
        draw_theme(f, left[3], at);

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
        draw_system(f, right[0], store, at);
        self.draw_models(f, right[1], store, at);
        draw_universe(f, right[2], store, at);

        // Last, and over every rect the cards were actually given: what a
        // click may press is a fact about the frame that was just drawn, and a
        // list built from the constants instead would answer about a layout
        // the terminal's width may have refused.
        self.record(
            &[
                (Card::Desk, top[0]),
                (Card::News, top[1]),
                (Card::Method, left[0]),
                (Card::Policy, left[1]),
                (Card::Theme, left[3]),
                (Card::System, right[0]),
                (Card::Models, right[1]),
                (Card::Universe, right[2]),
            ],
            at,
        );

        // Over the view rather than over the frame: the questions they ask are
        // about this pane's own controls, unlike the confirm box, which asks
        // about an order and belongs to the whole workstation.
        self.draw_form(f, area, store);
        self.draw_switch(f, area, store);
        self.draw_contact(f, area, store);
        self.draw_cap(f, area, store);
    }

    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        self.keys(k, store)
    }

    /// A click on what the last frame drew.
    ///
    /// Left button only, and no wheel: nothing on this pane scrolls, and a
    /// wheel that silently did nothing would read the same as one over a list
    /// that had stopped responding.
    fn on_mouse(&mut self, m: MouseEvent, store: &mut Store) -> Option<Command> {
        #[cfg(not(feature = "operator"))]
        {
            let _ = (m, store);
            None
        }
        #[cfg(feature = "operator")]
        self.clicks(m, store)
    }

    /// Back on this pane: the keys aim at the desk card again.
    ///
    /// The focus is the only thing here that decides what a key *means*, and it
    /// is invisible from anywhere else on the workstation — so a walk to MODELS
    /// followed by a trip to BOOK and back left `a` silently dead on a pane
    /// whose desk card the operator was reading. Every other cursor on this
    /// client is worth keeping across a switch because it is drawn where it
    /// was left; this one had no cue at all.
    fn entered(&self) {
        #[cfg(feature = "operator")]
        self.focus.set(Card::Desk);
    }

    /// Whether a box on this pane currently owns the keyboard.
    ///
    /// True only while one an operator can actually see is open. A box too tall
    /// for the pane is drawn as a refusal, not as a box, so it holds no
    /// keyboard — without that half the login fields would swallow every key
    /// and Enter would still store a credential against a box nobody can see.
    ///
    /// The switcher needs the claim as much as the form does, and for a
    /// different key: `Esc` is the shell's quit, so a picker that did not own
    /// the keyboard would be closed by the operator quitting the workstation.
    fn typing(&self) -> bool {
        #[cfg(feature = "operator")]
        {
            (self.form.is_some() && self.form_fits())
                || (self.switch.is_some() && self.box_fits())
                || (self.news.contact_box.is_some() && self.contact_fits())
                || (self.method.cap_box.is_some() && self.cap_fits())
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

    /// The same question for the switcher, which needs fewer rows: it has one
    /// list and no field, where the form has two fields, a note, a key line and
    /// whatever the desk last said under them.
    fn box_fits(&self) -> bool {
        let area = self.area.get();
        area.height >= SWITCH_MIN_H && area.width >= TWO_COL
    }

    /// The same question again for the contact box, which needs more rows than
    /// the switcher and fewer than the login form.
    fn contact_fits(&self) -> bool {
        let area = self.area.get();
        area.height >= CONTACT_MIN_H && area.width >= TWO_COL
    }

    /// And again for the cap box, which needs one row fewer than the contact.
    fn cap_fits(&self) -> bool {
        let area = self.area.get();
        area.height >= CAP_MIN_H && area.width >= TWO_COL
    }

    fn publish(&self, area: Rect) {
        self.area.set(area);
    }

    fn forget_hits(&self) {
        self.hits.borrow_mut().clear();
    }

    /// Where this frame put the things a pointer may press.
    ///
    /// Only in a window that can act. A glass one marks no card and its rules
    /// name no keys, so a rectangle recorded there would be an affordance over
    /// a sentence that says there is none — the greyed-key claim this client
    /// refuses everywhere.
    ///
    /// Words are recorded for the **focused** card only, because that is the
    /// only card whose rule is drawn: an unfocused card's footer is not on
    /// screen, and a hit rectangle over a line nobody can read is a button
    /// nobody can find.
    fn record(&self, cards: &[(Card, Rect)], at: Option<Card>) {
        if at.is_none() {
            return;
        }
        let mut hits = self.hits.borrow_mut();
        for (card, area) in cards {
            if area.height == 0 || area.width == 0 {
                continue;
            }
            hits.headers
                .push((Rect::new(area.x, area.y, area.width, 1), *card));
            if at != Some(*card) {
                continue;
            }
            // The block draws its rule on the card's last row and the footer
            // on it as ` {footer} `, left-aligned — so a word starts one cell
            // in from the card's own left edge plus its offset in the
            // sentence. Character offsets, because the sentences around the
            // words are not ASCII.
            let rule = area.y + area.height - 1;
            let footer = card.footer(true);
            for (word, key) in card.words() {
                let Some(byte) = footer.find(word) else {
                    continue;
                };
                let x = area.x + 1 + footer[..byte].chars().count() as u16;
                let w = word.chars().count() as u16;
                if x.saturating_add(w) <= area.x + area.width {
                    hits.words.push((Rect::new(x, rule, w, 1), *card, *key));
                }
            }
        }
    }

    /// One click on the frame the operator is looking at.
    ///
    /// A word does exactly what its key does — the same `KeyCode` through the
    /// same router — so a click can never mean something the card does not
    /// say. A row does two things at once on purpose: it takes the focus *and*
    /// picks, because a click that only moved a cursor would leave the
    /// operator reaching for the keyboard to finish what they started.
    fn clicks(&mut self, m: MouseEvent, store: &mut Store) -> Option<Command> {
        if !matches!(m.kind, MouseEventKind::Down(MouseButton::Left)) {
            return None;
        }
        // A box owns the pointer exactly as it owns the keyboard: a click that
        // moved the focus under an open form would leave the operator typing
        // into a box aimed at a card they can no longer see.
        if self.typing() {
            return None;
        }
        let hit = self.hits.borrow().at(m.column, m.row)?;
        match hit {
            Hit::Header(card) => {
                self.focus.set(card);
                self.news.note = None;
                self.method.note = None;
                None
            }
            // A click on a rights row does what `Space` on it does, for the
            // reason a news row does: a click that only moved a cursor would
            // leave the operator reaching for the keyboard to finish what they
            // started. It is the same `Command` through the same producer, so
            // a pointer can never grant what a key could not.
            Hit::Row(Card::Models, at) => {
                self.focus.set(Card::Models);
                self.rights.at = at;
                self.grant(store)
            }
            Hit::Row(_, at) => {
                self.focus.set(Card::News);
                self.news.at = at;
                self.pick(store);
                None
            }
            Hit::Word(card, key) => {
                self.focus.set(card);
                self.keys(KeyEvent::new(KeyCode::Char(key), KeyModifiers::NONE), store)
            }
        }
    }

    /// Which card the keys are aimed at, or `None` in a window that has none.
    ///
    /// The posture rather than the build, exactly as `keys` gates on it: a
    /// featured binary the human did not arm hears nothing, so marking a card
    /// as listening there would be the greyed-affordance claim this client
    /// refuses everywhere.
    fn focused(&self, store: &Store) -> Option<Card> {
        store.posture.writes().then_some(self.focus.get())
    }

    /// Every key this pane claims, gated on the posture rather than the build.
    ///
    /// The order is load-bearing twice over. An open box outranks the keys that
    /// reach the desk, or `t` would be untypeable inside a secret; and the card
    /// walk sits under both, or an arrow would move the focus out from under
    /// the picker the operator is looking at.
    ///
    /// Under that, a key means whatever the **focused** card says it means.
    /// `a` and `t` do exactly what C2 built them to do; what changed is that
    /// they are the desk card's rather than the pane's, which is what lets each
    /// card state its own keys without describing four cards it is not.
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
        // A terminal that shrank under an open box retires it, on the first key
        // after the resize — and the fields go with it, so growing back cannot
        // restore a half-typed credential the operator has not seen since. The
        // box is already refusing to draw and already holding no keyboard by
        // then; this is what keeps the *state* from outliving it.
        if self.form.is_some() && !self.form_fits() {
            self.close();
            return None;
        }
        if self.switch.is_some() && !self.box_fits() {
            self.switch = None;
            return None;
        }
        // The contact goes with the box, for the reason the credential does:
        // a value typed into something the operator has not been able to see
        // since the terminal shrank is not one this client may still be
        // holding.
        if self.news.contact_box.is_some() && !self.contact_fits() {
            self.news.contact_box = None;
            return None;
        }
        // And the cap goes with its box, for the same reason: a number typed
        // into something the operator has not been able to see since the
        // terminal shrank is not one this client may still be holding.
        if self.method.cap_box.is_some() && !self.cap_fits() {
            self.method.cap_box = None;
            return None;
        }
        if self.form.is_some() {
            return self.form_key(k);
        }
        if self.switch.is_some() {
            return self.switch_key(k, store);
        }
        if self.news.contact_box.is_some() {
            return self.contact_key(k);
        }
        if self.method.cap_box.is_some() {
            return self.cap_key(k, store);
        }
        match k.code {
            KeyCode::Up => self.step(-1),
            KeyCode::Down => self.step(1),
            KeyCode::Char('a') if self.focus.get() == Card::Desk => {
                self.form = Some(Form::default())
            }
            // No client-side gate on there being a login to test. A desk that
            // has never logged in is one of the answers the route is built to
            // give ("no credentials are configured"), and a client that
            // pre-empted it would be a second, drifting copy of the owner's
            // own account of what it can read.
            KeyCode::Char('t') if self.focus.get() == Card::Desk => {
                return Some(Command::TestAlpaca)
            }
            // The lane, on the card whose `lane` row names it. The same box
            // the models card opens with a different list behind it — a second
            // widget family for two rows would be two answers to every
            // question the first one already settled (what it refuses at, what
            // owns the keyboard, what Esc leaves behind).
            //
            // No catalog request rides this one: what the pair may be is the
            // owner's own `desk_mode` block, which the snapshot already
            // carries on the desk's cadence.
            KeyCode::Char('m') if self.focus.get() == Card::Desk => {
                self.switch = Some(Switch::lane(store))
            }
            // The catalog on the store is a *reading* and may be an hour old —
            // `/api/tui` refuses to probe under the dispatch lock — so the key
            // that opens the box asks for a fresh one on the way in, exactly as
            // the door's first question does on its transition.
            KeyCode::Char('m') if self.focus.get() == Card::Models => {
                self.switch = Some(Switch::opened_on(store));
                return Some(Command::Backends);
            }
            // The method, on the card that names it. The same box again with a
            // third list behind it — and no catalog request rides this one
            // either: what may be chosen is the owner's own `/api/desk/method`
            // answer, which this pane fetched on the way in.
            //
            // Both keys refuse out loud while a request is in flight. The route
            // writes the mandate override file and logs an audit row per
            // changed field, so a held key would put two decisions on the
            // record for one press.
            KeyCode::Char('m') if self.focus.get() == Card::Method => match self.method.sending {
                true => self.method.note = Some(ASKING.to_string()),
                false => {
                    self.method.note = None;
                    self.switch = Some(Switch::method(store));
                }
            },
            KeyCode::Char('k') if self.focus.get() == Card::Method => match self.method.sending {
                true => self.method.note = Some(ASKING.to_string()),
                false => {
                    self.method.note = None;
                    // Opened empty rather than on what is in force. The box's
                    // own sentence says what clearing means, and a field
                    // pre-filled with the current cap would make Enter on an
                    // untouched box a request that changes nothing — which is
                    // an audit row for a decision nobody made.
                    self.method.cap_box = Some(String::new());
                }
            },
            // Nothing is sent by a tick. The draft is this window's alone
            // until `s` or `v` carries it, which is what lets an operator
            // build a stack out of four keystrokes instead of four writes of
            // their `.env`.
            KeyCode::Char(' ') if self.focus.get() == Card::News => self.pick(store),
            // The one key on this card that reaches the owner. It is sent
            // immediately, for the reason the method picker's Enter is: the
            // owner writes the file and records one audit row per changed
            // field, so there is nothing to stage and nothing a second key
            // could confirm.
            KeyCode::Char(' ') if self.focus.get() == Card::Models => return self.grant(store),
            KeyCode::Char('c') if self.focus.get() == Card::News => {
                self.news.note = None;
                self.news.contact_box = Some(self.news.contact.clone().unwrap_or_default());
            }
            KeyCode::Char('s') if self.focus.get() == Card::News => return self.save(store, false),
            KeyCode::Char('v') if self.focus.get() == Card::News => return self.save(store, true),
            _ => {}
        }
        None
    }

    /// One arrow: a source row inside NEWS, a card everywhere else.
    ///
    /// The cursor walks *out* of the card at either end rather than stopping
    /// in it. A row cursor an operator cannot leave is the same fault as a
    /// card highlight that never moves — and NEWS sits second in the walk, so
    /// trapping it would put five cards beyond reach of the arrows.
    ///
    /// Entering the card puts the cursor on the edge it was entered from, so a
    /// held arrow reads as one continuous walk rather than as a jump to
    /// wherever the cursor was last left.
    fn step(&mut self, by: isize) {
        self.news.note = None;
        self.method.note = None;
        self.rights.note = None;
        let last = self.news_rows.get().saturating_sub(1);
        if self.focus.get() == Card::News && self.news_rows.get() > 0 {
            let at = self.news.at.min(last);
            self.news.at = at;
            if by > 0 && at < last {
                self.news.at = at + 1;
                return;
            }
            if by < 0 && at > 0 {
                self.news.at = at - 1;
                return;
            }
        }
        // The same walk over the three rights, and the same wall at each end:
        // the cursor stops rather than wrapping, and the arrow that would pass
        // the end walks out of the card instead. A row cursor an operator
        // cannot leave would put UNIVERSE beyond reach of the arrows.
        //
        // Not gated on the rows having been *drawn*, unlike NEWS': the rights
        // are three rows inside `MODELS_MIN_H` and a card too short for them
        // draws a refusal, which is a state `grant` refuses out loud rather
        // than one the cursor has to model.
        if self.focus.get() == Card::Models {
            let last = RIGHTS_ROWS - 1;
            let at = self.rights.at.min(last);
            self.rights.at = at;
            if by > 0 && at < last {
                self.rights.at = at + 1;
                return;
            }
            if by < 0 && at > 0 {
                self.rights.at = at - 1;
                return;
            }
        }
        self.walk(by);
        if self.focus.get() == Card::News {
            self.news.at = match by > 0 {
                true => 0,
                false => self.news_rows.get().saturating_sub(1),
            };
        }
        // Entering MODELS puts the cursor on the edge it was entered from, so
        // a held arrow reads as one continuous walk rather than as a jump to
        // wherever it was last left. The same rule NEWS states one card up.
        if self.focus.get() == Card::Models {
            self.rights.at = match by > 0 {
                true => 0,
                false => RIGHTS_ROWS - 1,
            };
        }
    }

    /// Grant or withdraw the right the cursor is on, and send it.
    ///
    /// **Nothing is inverted from a value this client made up.** A right the
    /// owner has not named yet has no opposite, so the key refuses out loud
    /// rather than sending `true` for it — a toggle computed from a default
    /// would be this client deciding what the desk's rights are and then
    /// writing that decision to the owner's file.
    fn grant(&mut self, store: &Store) -> Option<Command> {
        self.rights.note = None;
        if let Some(field) = self.rights.sending {
            // Out loud, and it names which one: two rights toggled in quick
            // succession would otherwise read as the second key doing nothing.
            self.rights.note = Some(format!("{ASKING} about {field}"));
            return None;
        }
        let at = self.rights.at.min(RIGHTS_ROWS - 1);
        self.rights.at = at;
        let right = cmd::Right::at(at)?;
        let Some(rights) = store.rights() else {
            // Said rather than swallowed. Nothing has answered, so there is no
            // value to invert and no request this client may compose — and a
            // key that silently did nothing reads as a dead card.
            self.rights.note = Some(format!("nothing has said where {} stands", right.as_str()));
            return None;
        };
        if let Some(said) = &rights.error {
            // The owner could not read the file it would be writing into.
            // Refused here rather than sent, because the change would land on
            // top of whatever is wrong with it — and the sentence already on
            // the card is the remedy.
            self.rights.note = Some(format::bounded(said, SAID_MAX));
            return None;
        }
        let Some(held) = rights.rights.get(right.as_str()) else {
            self.rights.note = Some(format!("nothing has said where {} stands", right.as_str()));
            return None;
        };
        self.rights.sending = Some(right.as_str());
        Some(Command::SetRight {
            field: right,
            value: !held,
        })
    }

    /// Tick or untick the row the cursor is on, in the draft and nowhere else.
    ///
    /// The one refusal made here rather than left to the owner: a source the
    /// desk cannot read is refused on the keystroke, because the owner answers
    /// the same 400 and the row already carries what it is waiting for. A tick
    /// that appeared and then vanished on the save would be this pane showing
    /// a state the desk can never be in.
    fn pick(&mut self, store: &Store) {
        self.news.note = None;
        let Some(news) = store.news() else {
            return;
        };
        // The rows the last frame *drew*, not the catalog: a card too short
        // for the whole of it stops the cursor at its last visible row, and a
        // tick against a source nobody can see is a change nobody chose.
        let at = self.news.at.min(self.news_rows.get().saturating_sub(1));
        self.news.at = at;
        let Some(source) = news.catalog.get(at) else {
            return;
        };
        let Some(name) = format::text(source.name.as_ref()).map(str::to_string) else {
            return;
        };
        let mut picked = self.picked(news);
        match picked.iter().position(|held| *held == name) {
            Some(held) => {
                picked.remove(held);
            }
            // Availability only. An unmet contact is not an unreadable
            // source: the box on this card provides one, so ticking edgar is
            // the first half of a change the operator is allowed to make.
            None if source.available == Some(false) => {
                self.news.note = Some(match format::text(source.needs.as_ref()) {
                    Some(needs) => format!("{name} needs {needs}"),
                    None => format!("the desk cannot read {name}"),
                });
                return;
            }
            None => picked.push(name),
        }
        // Sorted, so "the same set" and "the same list" are the same
        // comparison — which is what the edited mark is read off.
        picked.sort();
        self.news.picked = Some(picked);
    }

    /// The set the draft would send: what the operator picked, or what the
    /// owner says is chosen while they have picked nothing.
    fn picked(&self, news: &NewsSettings) -> Vec<String> {
        self.news.picked.clone().unwrap_or_else(|| chosen(news))
    }

    /// Whether the draft differs from the owner's own answer.
    fn edited(&self, store: &Store) -> bool {
        if self.news.contact.is_some() {
            return true;
        }
        let Some(news) = store.news() else {
            return false;
        };
        self.news
            .picked
            .as_ref()
            .is_some_and(|picked| *picked != chosen(news))
    }

    /// Send the draft, with or without asking the owner to check it first.
    ///
    /// The second local refusal, and it is here for `pick`'s reason: the owner
    /// answers the same 400 about edgar with no contact, and its sentence
    /// names the shape rather than the key on this card that provides one.
    /// Everything else — an unknown name, an empty list, a malformed
    /// contact — travels and comes back in the owner's own words, because this
    /// client owns none of those rules.
    fn save(&mut self, store: &Store, verify: bool) -> Option<Command> {
        self.news.note = None;
        let Some(news) = store.news() else {
            // Short enough for the card's own note row: at 38 cells the
            // longer sentence was cut mid-word, and this is a refusal an
            // operator has to read to know nothing was sent.
            self.news.note = Some("nothing to save — no answer yet".to_string());
            return None;
        };
        // One request at a time. The route writes the operator's `.env`, and a
        // held key would put two of those on the wire for one decision.
        //
        // Refused out loud. A checked save runs for minutes, so this is the
        // guard an operator is most likely to hit — and a key that silently
        // did nothing there reads as a dead card rather than as a busy one.
        if self.news.sending.is_some() {
            self.news.note = Some("one request at a time — still asking".to_string());
            return None;
        }
        let providers = self.picked(news);
        if providers.iter().any(|name| name == EDGAR)
            && news.edgar_contact_set != Some(true)
            && self.news.contact.is_none()
        {
            self.news.note = Some("edgar needs a contact — press c".to_string());
            return None;
        }
        self.news.sending = Some(verify);
        // The last check described the last request. Kept on screen through
        // one it no longer describes, it would be a verdict about a stack
        // nobody sent.
        self.news.verified.clear();
        Some(Command::NewsSettings {
            providers,
            contact: self.news.contact.clone().map(cmd::Contact::new),
            verify,
            // The lane this card is drawing, which is the lane the answer in
            // front of the operator is about. The route defaults it to the
            // desk mode, and a window pointed at the other one would then be
            // told about a stack it is not reading.
            offline: match news.lane.as_deref() {
                Some("live") => false,
                Some("synthetic") => true,
                // Neither word, so this client has nothing to assert. The
                // owner's own default is the desk mode, which is a better
                // answer than a guess made here.
                _ => store
                    .desk_mode()
                    .and_then(|mode| mode.offline)
                    .unwrap_or(true),
            },
        })
    }

    /// One keystroke into the contact box.
    ///
    /// Its own router for the reason the login form has one: the keys that
    /// *open* a box and the keys *inside* it are two sets, and one help
    /// section over both would describe the box with a row about the card.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn contact_key(&mut self, k: KeyEvent) -> Option<Command> {
        let typed = self.news.contact_box.as_mut()?;
        match k.code {
            KeyCode::Char(c) => typed.push(c),
            KeyCode::Backspace => {
                typed.pop();
            }
            // Kept, not sent. The contact travels on the save that needs it,
            // so an operator who types one and walks away has sent nothing —
            // and a trailing space from a paste goes here rather than at the
            // owner's shape check, which would refuse a contact that looks
            // right on screen.
            KeyCode::Enter => {
                let said = typed.trim().to_string();
                self.news.contact = (!said.is_empty()).then_some(said);
                self.news.contact_box = None;
            }
            KeyCode::Esc => self.news.contact_box = None,
            _ => {}
        }
        None
    }

    /// Move the focus, and stop at the ends.
    ///
    /// Clamped rather than wrapped: an operator holding an arrow to reach the
    /// last card must not find themselves back at the first one, which is the
    /// rule every cursor on this client walks by.
    fn walk(&mut self, by: isize) {
        let last = Card::ALL.len() - 1;
        let at = Card::ALL
            .iter()
            .position(|card| *card == self.focus.get())
            .unwrap_or(0);
        self.focus
            .set(Card::ALL[at.saturating_add_signed(by).min(last)]);
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
        // The rights wait is retired **before** the blanket retirement below
        // and only by its own three outcomes, each matched on the field it
        // carries. The card is one keystroke from a second write, and a wait
        // retired by an unrelated broken request would re-arm `Space` over a
        // toggle still in flight — one decision, two audit rows. `field` is
        // what makes the match structural rather than a comparison of prose.
        match outcome {
            Wrote::RightSet { field, .. } if self.rights.sending == Some(*field) => {
                self.rights.sending = None;
                // Nothing is copied onto the card: the refetch behind this
                // outcome is what the rows then draw, and a receipt composed
                // here would be a second account of a file only the owner read.
                self.rights.note = None;
                return;
            }
            // Not confirmable and not a broken request: the owner considered
            // the change and declined it, so its sentence stands on the card
            // beside the row that is still where it was.
            Wrote::RightRefused { field, said } if self.rights.sending == Some(*field) => {
                self.rights.sending = None;
                self.rights.note = Some(format::bounded(said, SAID_MAX));
                return;
            }
            // A request that never landed. The card must not stay waiting over
            // one — a key that refuses forever after a timeout is a client that
            // looks broken — and the sentence says which right it was about.
            Wrote::RightFailed { field, said } if self.rights.sending == Some(*field) => {
                self.rights.sending = None;
                self.rights.note = Some(format::bounded(said, SAID_MAX));
                return;
            }
            _ => {}
        }
        // Each of the two cards below retires its wait on its own three
        // outcomes and on nothing else, for the reason the rights wait above
        // does. The blanket retirement this replaced was harmless while every
        // write answered in milliseconds; a predictor board is fitted for up to
        // a minute, so a `PredictorRan` lands mid-flight, blanks the cap box's
        // "asking the owner…", and re-arms an Enter that writes a second
        // override row for one decision.
        match outcome {
            // The owner's answer replaces the draft wholesale, and the refetch
            // behind it is what the card then draws. A draft left standing
            // would mark as edited a change that has already landed.
            Wrote::NewsSaved {
                checked, verified, ..
            } => {
                self.news.sending = None;
                self.news.picked = None;
                self.news.contact = None;
                self.news.note = None;
                self.news.verified = verified.clone();
                // Asked to look and told nothing. Said rather than drawn as a
                // clean check: the rows would otherwise fall back to the
                // desk's own last outcomes, which is a different question than
                // the one the operator asked.
                if *checked && verified.is_empty() {
                    self.news.note = Some("the owner did not say what it checked".to_string());
                }
                return;
            }
            // Its own sentence, on the card that asked. It carries the remedy,
            // and the toast that carries it too is gone in four seconds.
            Wrote::NewsRefused { said } => {
                self.news.sending = None;
                self.news.note = Some(format::bounded(said, SAID_MAX));
                return;
            }
            // A request that never landed. The card must not stay waiting over
            // one — a card still saying "asking the owner…" after a timeout
            // reads as a client that has hung — and the sentence is the owner's
            // own words, because this client knows nothing about what happened.
            Wrote::NewsFailed { said } => {
                self.news.sending = None;
                self.news.note = Some(format::bounded(said, SAID_MAX));
                return;
            }
            // The desk moved. The box has done its work and the refetch behind
            // this outcome is what the card then draws — including the warning
            // the owner recomputed, which is why nothing here copies the pair
            // out of the answer onto the card.
            Wrote::MethodSet { .. } => {
                self.method.sending = false;
                self.method.cap_box = None;
                self.method.note = None;
                return;
            }
            // Not confirmable and not a broken request: the owner considered
            // the change and declined it, so the box stays up with the number
            // still in it and the owner's sentence under the field.
            Wrote::MethodRefused { said } => {
                self.method.sending = false;
                self.method.note = Some(format::bounded(said, SAID_MAX));
                return;
            }
            // A request that never landed, for `NewsFailed`'s reason. The box
            // stays up with the number still in it: nothing here says the
            // change did not land, so re-reading the card and pressing again is
            // the remedy.
            Wrote::MethodFailed { said } => {
                self.method.sending = false;
                self.method.note = Some(format::bounded(said, SAID_MAX));
                return;
            }
            _ => {}
        }
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
            // The picker steps aside for the login and comes back after it —
            // but only when the desk can now read one. Reopening on a
            // credential the owner still refuses would put the operator back
            // on the row that sent them here, which reads as a loop rather
            // than as a refusal.
            if std::mem::take(&mut self.relane)
                && matches!(outcome, Wrote::LoggedIn { usable: true, .. })
            {
                self.focus.set(Card::Desk);
                self.switch = Some(Switch::lane_at(LANE_LIVE));
            }
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

    /// One keystroke into the model switcher.
    ///
    /// Its own router, and its own help section, for the reason WORKFORCE's two
    /// fields have theirs: the keys that *open* a box and the keys *inside* it
    /// are two sets, and one section over both would describe the picker with a
    /// row about the card.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn switch_key(&mut self, k: KeyEvent, store: &Store) -> Option<Command> {
        if self.switch.as_ref().map(|switch| switch.kind) == Some(Picker::Lane) {
            return self.lane_key(k, store);
        }
        if self.switch.as_ref().map(|switch| switch.kind) == Some(Picker::Method) {
            return self.method_key(k, store);
        }
        let rows = choices(store);
        let switch = self.switch.as_mut()?;
        switch.note = None;
        // The list is rebuilt from the store every keystroke, and the store
        // moves under it: the key that opened this box asked for a fresh
        // catalog, and a daemon that has gone down since answers with fewer
        // models than the cursor was sitting past. Clamped here rather than
        // guarded at the read, so `Enter` on a shrunk list chooses the last row
        // rather than reporting an empty catalog it can plainly see is not.
        switch.at = switch.at.min(rows.len().saturating_sub(1));
        match k.code {
            KeyCode::Up => switch.at = switch.at.saturating_sub(1),
            KeyCode::Down => switch.at = (switch.at + 1).min(rows.len().saturating_sub(1)),
            // Leaves every surface as the desk has it. Nothing is staged here,
            // so there is nothing to discard: a choice is sent the moment it is
            // made, which is what the `now` marker on the rows is about.
            KeyCode::Esc => self.switch = None,
            KeyCode::Enter => {
                let Some(row) = rows.get(switch.at) else {
                    // An empty catalog. Stated rather than silent: a box that
                    // swallowed Enter would read as a desk refusing a choice it
                    // was never offered.
                    switch.note = Some("nothing has said what this desk can run".to_string());
                    return None;
                };
                return match (&row.refusal, &row.choice) {
                    // Shown on the list and refused here, in the owner's own
                    // sentence rather than a second opinion composed by this
                    // client — the rule `/model` and the door both submit to.
                    (Some(said), _) => {
                        switch.note = Some(said.clone());
                        None
                    }
                    (None, Some(choice)) => Some(Command::SetLlm {
                        surface: row.surface.to_string(),
                        choice: choice.clone(),
                    }),
                    // Cannot happen — `cmd::Offer` carries a choice for
                    // everything it does not refuse — and saying so is cheaper
                    // than a branch that silently does nothing.
                    (None, None) => {
                        switch.note = Some("the desk offered a model it cannot name".to_string());
                        None
                    }
                };
            }
            _ => {}
        }
        // The window follows the cursor, so a walk cannot leave the selection
        // off the box — the same failure the floor above refuses, one row down.
        let switch = self.switch.as_mut()?;
        let cap = cap(self.area.get());
        if switch.at < switch.top {
            switch.top = switch.at;
        } else if switch.at >= switch.top + cap {
            switch.top = switch.at + 1 - cap;
        }
        None
    }

    /// One keystroke into the lane picker.
    ///
    /// The same four keys as the model switcher and the same meanings, because
    /// it is the same box: what differs is the list behind it and what Enter
    /// does with a row. Routed from `switch_key` rather than from `keys`, so
    /// the help section that describes the box describes both of them.
    fn lane_key(&mut self, k: KeyEvent, store: &Store) -> Option<Command> {
        let rows = lanes(store);
        let switch = self.switch.as_mut()?;
        switch.note = None;
        switch.at = switch.at.min(rows.len().saturating_sub(1));
        match k.code {
            KeyCode::Up => switch.at = switch.at.saturating_sub(1),
            KeyCode::Down => switch.at = (switch.at + 1).min(rows.len().saturating_sub(1)),
            // Leaves the desk where it is. Nothing is staged here either: a
            // choice is sent the moment it is made.
            KeyCode::Esc => self.switch = None,
            KeyCode::Enter => {
                let at = switch.at;
                let Some(row) = rows.get(at) else {
                    switch.note = Some("the owner did not say which desk this is".to_string());
                    return None;
                };
                // The live lane needs a login the desk can read. Not sent and
                // not refused: the picker steps aside for the box that fixes
                // it, and `wrote` brings the picker back once the owner says
                // the credential works. Pre-empting the owner here is the one
                // place this pane does it, and it is because the remedy is a
                // box on this pane rather than a sentence.
                if row.book == ALPACA
                    && store.desk_mode().and_then(|mode| mode.credentials_ok) != Some(true)
                {
                    self.switch = None;
                    self.relane = true;
                    self.open_login();
                    return None;
                }
                let (data, book) = (row.data.to_string(), row.book.to_string());
                self.switch = None;
                // Exactly what `/mode` sends, so the owner's own refusals and
                // the veto path in front of them are one path rather than two.
                return Some(Command::DeskMode { data, book });
            }
            _ => {}
        }
        None
    }

    /// One keystroke into the method picker.
    ///
    /// The same four keys as the lane picker and the same meanings, because it
    /// is the same box again: what differs is the list behind it and what Enter
    /// does with a row.
    ///
    /// **The cursor walks the operational entries and nothing else.** The
    /// research entries are drawn under them, dimmed, with the stage that says
    /// why — and there is no index at which the cursor can land on one, so
    /// "not choosable" is a property of this list rather than a refusal it
    /// makes after the fact. The owner refuses one too, in a sentence about
    /// evidence and a catalog change; this box simply never asks it to.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn method_key(&mut self, k: KeyEvent, store: &Store) -> Option<Command> {
        let rows = operational(store);
        let switch = self.switch.as_mut()?;
        switch.note = None;
        // Rebuilt from the store every keystroke, and the store moves under it:
        // the pane's own refetch lands on the bus while this box is open, so
        // the cursor is clamped against the list that is about to be drawn
        // rather than the one that was there when it opened.
        switch.at = switch.at.min(rows.len().saturating_sub(1));
        match k.code {
            KeyCode::Up => switch.at = switch.at.saturating_sub(1),
            KeyCode::Down => switch.at = (switch.at + 1).min(rows.len().saturating_sub(1)),
            // Leaves the desk solving as it does. Nothing is staged here
            // either: a choice is sent the moment it is made, which is what the
            // `now` marker on the rows is about.
            KeyCode::Esc => self.switch = None,
            KeyCode::Enter => {
                let Some(row) = rows.get(switch.at) else {
                    // An empty list. Stated rather than silent, exactly as the
                    // model switcher states it: a box that swallowed Enter
                    // would read as a desk refusing a choice it never offered.
                    switch.note = Some("the desk has not said what it can solve with".to_string());
                    return None;
                };
                let Some(id) = format::text(row.id.as_ref()).map(str::to_string) else {
                    switch.note = Some("the desk offered a method it cannot name".to_string());
                    return None;
                };
                self.switch = None;
                self.method.note = None;
                self.method.sending = true;
                return Some(Command::SetMethod(cmd::MethodChange::Policy(id)));
            }
            _ => {}
        }
        None
    }

    /// One keystroke into the holdings-cap box.
    ///
    /// Its own router for the contact box's reason: the keys that *open* a box
    /// and the keys *inside* it are two sets.
    ///
    /// **One refusal is made here rather than left to the owner**, and it is
    /// the pane's third and last: a cap outside `1..N` is refused on Enter,
    /// with the box left open and the number still in it. The owner answers the
    /// same 400 — and its sentence names the universe rather than the key on
    /// this card — but the remedy is to type a different number into the box
    /// that is already open, and a round trip that closed nothing and changed
    /// nothing is a round trip the operator watches for no reason. `N` is the
    /// universe the *snapshot* carries; with none, nothing is asserted here and
    /// the owner's own bound is the only one.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn cap_key(&mut self, k: KeyEvent, store: &Store) -> Option<Command> {
        let universe = store.universe().len();
        // Read once, mutated through `as_mut` below. One binding held across
        // the whole match would borrow the draft the refusals also write to,
        // and splitting the arms to satisfy that is what put two `Enter`s in
        // this router — which the keymap equivalence reads as two bindings.
        let said = self.method.cap_box.as_ref()?.trim().to_string();
        match k.code {
            // Digits only, and bounded. A letter in a numeric field is a
            // refusal the operator would only hear about on Enter, and a held
            // key would push the digits they typed out of an `i64` entirely.
            //
            // **A non-digit is dropped in silence, deliberately.** The field is
            // one number wide: there is no sentence to write about `q` that is
            // not "this box takes digits", which the box already says on the
            // line under the field — and a note that appeared and retired on
            // every stray keystroke would flicker over the bound an operator is
            // reading. What must not happen is the field *changing*, and a test
            // pins that rather than this comment.
            KeyCode::Char(c) => {
                let typed = self.method.cap_box.as_mut()?;
                if c.is_ascii_digit() && typed.chars().count() < CAP_DIGITS {
                    typed.push(c);
                }
            }
            KeyCode::Backspace => {
                self.method.cap_box.as_mut()?.pop();
            }
            KeyCode::Enter => {
                // One request at a time, and refused out loud: the route writes
                // the override file and logs an audit row per changed field, so
                // a held Enter would put two decisions on the record for one
                // press. Typing still works — the operator may be correcting
                // the number they are waiting on.
                if self.method.sending {
                    self.method.note = Some(ASKING.to_string());
                    return None;
                }
                // Empty or zero is the clearing request, not an absent one: it
                // sends `max_holdings: null`, which drops the override and puts
                // the mandate's own cap back. Two spellings for one intent
                // because both are what an operator reaches for, and neither is
                // a cap of nothing.
                let cap = match said.is_empty() {
                    true => None,
                    false => match said.parse::<i64>() {
                        Ok(0) => None,
                        Ok(cap) => Some(cap),
                        // Unreachable while the field takes three digits and
                        // nothing else, and cheaper to say than to leave as a
                        // branch that silently does nothing.
                        Err(_) => {
                            self.method.note = Some("that is not a number of names".to_string());
                            return None;
                        }
                    },
                };
                // The one refusal made here rather than left to the owner, and
                // the pane's third: a cap above the universe this desk watches
                // is refused with the box still open and the number still in
                // it. The owner answers the same 400 — naming the universe
                // rather than the box that holds the number — and a round trip
                // that changed nothing is one the operator watches for no
                // reason. `N` is the *snapshot's* universe; with none, nothing
                // is asserted here and the owner's bound is the only one.
                if let Some(cap) = cap {
                    if universe > 0 && cap > universe as i64 {
                        self.method.note = Some(format!(
                            "this desk watches {universe} names — a cap above that holds \
                             nothing back"
                        ));
                        return None;
                    }
                }
                // The box stays open over the request, exactly as the login
                // form stays open over its own: a 400 has to land somewhere the
                // number that caused it is still visible, or the operator is
                // being asked to retype a value to answer a question about the
                // value they just typed. `wrote` is what closes it, and only on
                // an answer that says the desk moved.
                self.method.note = None;
                self.method.sending = true;
                return Some(Command::SetMethod(cmd::MethodChange::Cap(cap)));
            }
            KeyCode::Esc => self.method.cap_box = None,
            _ => {}
        }
        None
    }

    /// The cap box, drawn over the pane that opened it.
    ///
    /// The same discipline as the three boxes above — refuse rather than open
    /// invisible, and the next keystroke retires it.
    fn draw_cap(&self, f: &mut Frame, area: Rect, store: &Store) {
        use ratatui::widgets::{Block, Borders, Clear};
        let Some(typed) = &self.method.cap_box else {
            return;
        };
        if !self.cap_fits() {
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
                    "the holdings cap box needs {CAP_MIN_H} rows; this pane has {}.",
                    area.height
                ),
            );
            return;
        }
        let t = theme();
        let w = CAP_W.min(area.width.saturating_sub(4)).max(3);
        let lines = cap_lines(typed, store, self.method.note.as_deref());
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

    /// The METHOD card, and the state only an armed window can hold.
    fn draw_method(&self, f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
        method_card(
            f,
            area,
            store,
            at,
            self.method.note.as_deref(),
            self.method.sending,
        );
    }

    /// The switcher, drawn over the pane that opened it.
    fn draw_switch(&self, f: &mut Frame, area: Rect, store: &Store) {
        use ratatui::widgets::{Block, Borders, Clear};
        let Some(switch) = &self.switch else {
            return;
        };
        // Refuse rather than open invisible, exactly as the form does: below
        // the floor the box would have room for a header and nothing else, and
        // `typing` declines the keyboard for the same frame.
        if !self.box_fits() {
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
                    "the model switcher needs {SWITCH_MIN_H} rows; this pane has {}.",
                    area.height
                ),
            );
            return;
        }
        let t = theme();
        let w = SWITCH_W.min(area.width.saturating_sub(4)).max(3);
        let lines = match switch.kind {
            Picker::Models => switch.lines(store, cap(area)),
            Picker::Lane => switch.lane_lines(store),
            Picker::Method => switch.method_lines(store),
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

    /// The contact box, drawn over the pane that opened it.
    ///
    /// The same discipline as the two boxes above — refuse rather than open
    /// invisible, and the next keystroke retires it — and one deliberate
    /// difference: the field is plain text. The contact is an identity the SEC
    /// asks callers to send, and masking it would teach the wrong rule about
    /// the box beside it, which is the one that holds a credential.
    fn draw_contact(&self, f: &mut Frame, area: Rect, store: &Store) {
        use ratatui::widgets::{Block, Borders, Clear};
        let Some(typed) = &self.news.contact_box else {
            return;
        };
        if !self.contact_fits() {
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
                    "the contact box needs {CONTACT_MIN_H} rows; this pane has {}.",
                    area.height
                ),
            );
            return;
        }
        let t = theme();
        let w = CONTACT_W.min(area.width.saturating_sub(4)).max(3);
        let stored = store
            .news()
            .and_then(|news| news.edgar_contact_set)
            .unwrap_or(false);
        let lines = contact_lines(typed, stored, w - 2);
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

    /// The card, and the two things the last frame owes a click: how many
    /// rows the catalog drew, and where each of them landed.
    fn draw_news(&self, f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
        let drawn = news_card(
            f,
            area,
            store,
            at,
            self.news.picked.as_deref(),
            &self.news.verified,
            (at == Some(Card::News)).then_some(self.news.at),
            self.news.note.as_deref(),
            self.news.sending,
            self.edited(store),
        );
        self.news_rows.set(drawn);
        if at.is_none() {
            return;
        }
        let mut hits = self.hits.borrow_mut();
        for row in 0..drawn as u16 {
            let y = area.y + NEWS_TOP + row;
            // Never past the rule the block reserves: a rectangle over a row
            // the card had no room to draw is a click on something that is
            // not there.
            if y + 1 >= area.y + area.height {
                break;
            }
            hits.rows.push((
                Rect::new(area.x, y, area.width, 1),
                Card::News,
                row as usize,
            ));
        }
    }

    /// The MODELS card, with the cursor over its rights only while it is the
    /// card the keys are aimed at.
    ///
    /// The rectangles are recorded from the constant rather than from what the
    /// card returned, because these three rows are inside [`MODELS_MIN_H`]: a
    /// card that had no room for them drew a refusal instead and returns no
    /// rows at all, which the height check below is what rules out.
    fn draw_models(&self, f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
        let cursor = (at == Some(Card::Models)).then_some(self.rights.at);
        // The wait is a note like every refusal: a card that drew nothing while
        // a toggle was in flight reads as a key that did nothing, which is the
        // silent-key shape invariant 4 exists to close.
        let waiting = self
            .rights
            .sending
            .map(|field| format!("{ASKING} about {field}"));
        let note = self.rights.note.as_deref().or(waiting.as_deref());
        if !draw_models(f, area, store, at, cursor, note) || at.is_none() {
            return;
        }
        let mut hits = self.hits.borrow_mut();
        for row in 0..RIGHTS_ROWS as u16 {
            let y = area.y + RIGHTS_TOP + row;
            // Never past the rule the block reserves, for the reason NEWS
            // states: a rectangle over a row the card had no room to draw is a
            // click on something that is not there.
            if y + 1 >= area.y + area.height {
                break;
            }
            hits.rows.push((
                Rect::new(area.x, y, area.width, 1),
                Card::Models,
                row as usize,
            ));
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

// -- the model switcher -----------------------------------------------------

/// Where the cursor is in the switcher, and what it last said back.
///
/// No staged choice: a row is sent the moment Enter is pressed on it, so there
/// is nothing here for `Esc` to discard and nothing that can disagree with what
/// the desk reports. That is also why the marker on a row says what the surface
/// is running **now** rather than what the box would apply — the same split the
/// startup door had to make after a pty run caught it claiming otherwise.
#[cfg(feature = "operator")]
#[derive(Default)]
struct Switch {
    /// Which list is behind it. One box, two lists: a second widget family
    /// for two rows would be a second answer to every question this one has
    /// already settled — what it refuses at, what owns the keyboard, and what
    /// `Esc` leaves behind.
    kind: Picker,
    at: usize,
    top: usize,
    /// The owner's sentence about a row that cannot be chosen. Retired by the
    /// next keystroke, like the command line's own note.
    note: Option<String>,
}

/// Which list the box is showing.
#[cfg(feature = "operator")]
#[derive(Default, Clone, Copy, PartialEq, Eq)]
enum Picker {
    #[default]
    Models,
    /// The data lane: which prices this desk reads, and which book it values
    /// them against. Two rows, because the pair is not free — the owner
    /// refuses the combinations `DeskMode` cannot make — and the two this
    /// offers are the two the startup door offers.
    Lane,
    /// The operational method: how this desk solves, out of the entries the
    /// owner's catalog marks operational. The research entries are drawn under
    /// them and the cursor cannot reach one — see `method_key`.
    Method,
}

/// Where the live row sits in the lane list, so the box can be reopened on it
/// after a login without re-deriving what the operator was reaching for.
#[cfg(feature = "operator")]
const LANE_LIVE: usize = 1;

/// One row of the lane picker: a desk mode, in the owner's own spelling.
#[cfg(feature = "operator")]
struct LaneRow {
    data: &'static str,
    book: &'static str,
    label: &'static str,
    /// What taking this row means, in the words the startup door uses for the
    /// same two choices — one sentence about the desk, not two.
    said: &'static str,
}

/// The two halves of a desk mode, spelled as `qlab/core/desk_mode.py` spells
/// them. The same four words the door uses; a third spelling would be a
/// client inventing a desk the owner cannot make.
#[cfg(feature = "operator")]
const SYNTHETIC: &str = "synthetic";
#[cfg(feature = "operator")]
const LIVE: &str = "live";
#[cfg(feature = "operator")]
const SIMULATED: &str = "simulated";
#[cfg(feature = "operator")]
const ALPACA: &str = "alpaca";

/// The lanes this pane offers, in the order the door offers them.
#[cfg(feature = "operator")]
fn lanes(_store: &Store) -> Vec<LaneRow> {
    vec![
        LaneRow {
            data: SYNTHETIC,
            book: SIMULATED,
            label: "synthetic (demo)",
            said: "prices this desk makes",
        },
        LaneRow {
            data: LIVE,
            book: ALPACA,
            label: "live · alpaca",
            said: "a fill here still needs you",
        },
    ]
}

/// One row of the switcher: an offer, and what choosing it would send.
///
/// Built from `cmd::offers`, which is the producer the `/model` strip and the
/// startup door both read. Nothing here restates its rules — a backend the desk
/// cannot reach stays on the list with the owner's own sentence and no choice
/// behind it, and the workforce is offered `claude` alone because the tier map
/// owns its model — because a second copy of them is a second thing to keep
/// true.
#[cfg(feature = "operator")]
struct Choice {
    surface: &'static str,
    value: String,
    /// Whether this is what the surface runs now, per the owner's own answer.
    running: bool,
    refusal: Option<String>,
    choice: Option<ModelChoice>,
}

#[cfg(feature = "operator")]
fn choices(store: &Store) -> Vec<Choice> {
    let mut rows = Vec::new();
    for surface in cmd::SURFACES {
        for offer in cmd::offers(surface, store) {
            rows.push(Choice {
                surface,
                running: offer.running(store, surface),
                value: offer.value().to_string(),
                refusal: offer.refusal().map(str::to_string),
                choice: offer.choice(),
            });
        }
    }
    rows
}

/// The methods this desk may be pointed at, in the owner's own order.
///
/// Read off the payload rather than filtered here: `operational` is the owner's
/// list of what may be chosen, and a client that derived it — by stage, by
/// name, by anything — would be a second opinion about a catalog whose whole
/// point is that the desk owns it.
#[cfg(feature = "operator")]
fn operational(store: &Store) -> Vec<MethodEntry> {
    store
        .method()
        .map(|method| method.operational.clone())
        .unwrap_or_default()
}

/// How many rows of the list the box shows at once.
///
/// Bounded by a constant as well as by the pane: a desk holding thirty models
/// would otherwise compose a box taller than the area, and `centred` clamps the
/// rect — so the rows past the bottom would be clipped silently, with the `▾ n
/// more` marker claiming they were merely below.
#[cfg(feature = "operator")]
fn cap(area: Rect) -> usize {
    SWITCH_ROWS
        .min((area.height as usize).saturating_sub(8))
        .max(1)
}

#[cfg(feature = "operator")]
impl Switch {
    /// A box opened on the row the surface is already running.
    ///
    /// The default is the current config, so an operator who opens the picker
    /// and presses Enter changes nothing — and one who does not recognise their
    /// own choice on the list is being told something true about it. Falls back
    /// to the top when nothing matches, which is the state an empty catalog and
    /// a desk on a model the owner has stopped serving both reach.
    fn opened_on(store: &Store) -> Self {
        let at = choices(store)
            .iter()
            .position(|row| row.running)
            .unwrap_or(0);
        Self {
            kind: Picker::Models,
            at,
            top: 0,
            note: None,
        }
    }

    /// The lane box, opened on the lane the desk is already running.
    ///
    /// Same rule as `opened_on`: an operator who opens it and presses Enter
    /// changes nothing, and one who does not recognise their own desk on the
    /// list is being told something true about it.
    fn lane(store: &Store) -> Self {
        let at = lanes(store)
            .iter()
            .position(|row| running_lane(row, store))
            .unwrap_or(0);
        Self::lane_at(at)
    }

    fn lane_at(at: usize) -> Self {
        Self {
            kind: Picker::Lane,
            at,
            top: 0,
            note: None,
        }
    }

    /// The method box, opened on the method the desk already solves with.
    ///
    /// Same rule as `opened_on` and `lane`: an operator who opens it and
    /// presses Enter changes nothing, and one who does not recognise their own
    /// desk on the list is being told something true about it. On the owner's
    /// `current` mark rather than an id comparison made here — the mandate is
    /// merged from two places and the owner is the one that merged it.
    fn method(store: &Store) -> Self {
        let at = operational(store)
            .iter()
            .position(|row| row.current == Some(true))
            .unwrap_or(0);
        Self {
            kind: Picker::Method,
            at,
            top: 0,
            note: None,
        }
    }

    /// The method box's lines: what may be chosen, then what may not and why.
    ///
    /// No window and no `▾ n more`: the owner serves three operational entries
    /// and a handful of research ones, and the box is sized for the whole of
    /// both. If a catalog ever outgrows that, the box grows with it — `wanted`
    /// measures the lines rather than assuming a count — and `centred` clamps
    /// it to the pane, which is the one place a row could be lost silently.
    fn method_lines(&self, store: &Store) -> Vec<Line<'static>> {
        let t = theme();
        let rows = operational(store);
        let at = self.at.min(rows.len().saturating_sub(1));
        let mut lines = vec![panel_header("how this desk solves"), Line::from("")];
        if rows.is_empty() {
            lines.push(Line::from(Span::styled(
                " the desk has not said what it can solve with",
                Style::default().fg(t.text_dim),
            )));
        }
        for (i, row) in rows.iter().enumerate() {
            let on = i == at;
            let mut spans = vec![
                Span::styled(
                    if on { " ▸ " } else { "   " },
                    Style::default().fg(t.accent),
                ),
                Span::styled(
                    format!(
                        "{:<18}{:<4}",
                        to_room(&or_missing(row.id.as_ref()), 17),
                        to_room(&or_missing(row.arm_id.as_ref()), 3)
                    ),
                    match on {
                        true => Style::default()
                            .fg(t.accent)
                            .add_modifier(ratatui::style::Modifier::BOLD),
                        false => Style::default().fg(t.text_primary),
                    },
                ),
                Span::styled(
                    to_room(&or_missing(row.label.as_ref()), 26),
                    Style::default().fg(t.text_dim),
                ),
            ];
            if row.current == Some(true) {
                spans.push(Span::styled(
                    "  now",
                    Style::default()
                        .fg(t.positive)
                        .add_modifier(ratatui::style::Modifier::BOLD),
                ));
            }
            lines.push(Line::from(spans));
        }
        // The owner's own reason for the row the cursor is on. One line rather
        // than one per row: the rationales are sentences, and six of them would
        // be a box an operator scrolls instead of a list they choose from.
        if let Some(said) = rows
            .get(at)
            .and_then(|row| format::text(row.rationale.as_ref()))
        {
            lines.push(Line::from(Span::styled(
                format!(" {}", format::bounded(said, SAID_MAX)),
                Style::default().fg(t.text_tertiary),
            )));
        }
        let research = store
            .method()
            .map(|method| method.research.as_slice())
            .unwrap_or_default();
        if !research.is_empty() {
            lines.push(Line::from(""));
            // Listed rather than hidden, and the cursor cannot reach any of
            // them. A method the desk *has* and will not run is a fact an
            // operator looking for it needs — the alternative is a name they
            // read in the catalog and cannot find here at all.
            for row in research {
                lines.push(Line::from(vec![
                    Span::styled("   ", Style::default().fg(t.text_dim)),
                    Span::styled(
                        format!("{:<22}", to_room(&or_missing(row.id.as_ref()), 21)),
                        Style::default().fg(t.text_dim),
                    ),
                    Span::styled(
                        match format::text(row.stage.as_ref()) {
                            Some(stage) => format!("not choosable — {stage} stage"),
                            None => "not choosable".to_string(),
                        },
                        Style::default().fg(t.text_tertiary),
                    ),
                ]));
            }
        }
        lines.push(match &self.note {
            Some(note) => Line::from(Span::styled(
                format!(" {}", format::bounded(note, SAID_MAX)),
                Style::default()
                    .fg(t.warning)
                    .add_modifier(ratatui::style::Modifier::BOLD),
            )),
            None => Line::from(Span::styled(
                " Enter solves with it from the next run · ↑↓ moves · Esc leaves it",
                Style::default().fg(t.text_dim),
            )),
        });
        lines
    }

    /// The lane box's lines. No window and no `▾ n more`: there are two rows
    /// and there is no catalog behind them to grow.
    fn lane_lines(&self, store: &Store) -> Vec<Line<'static>> {
        let t = theme();
        let rows = lanes(store);
        let at = self.at.min(rows.len().saturating_sub(1));
        let mut lines = vec![panel_header("which data"), Line::from("")];
        for (i, row) in rows.iter().enumerate() {
            let on = i == at;
            let mut spans = vec![
                Span::styled(
                    if on { " ▸ " } else { "   " },
                    Style::default().fg(t.accent),
                ),
                Span::styled(
                    format!("{:<20}", row.label),
                    match on {
                        true => Style::default()
                            .fg(t.accent)
                            .add_modifier(ratatui::style::Modifier::BOLD),
                        false => Style::default().fg(t.text_primary),
                    },
                ),
                Span::styled(row.said, Style::default().fg(t.text_dim)),
            ];
            if running_lane(row, store) {
                spans.push(Span::styled(
                    "  now",
                    Style::default()
                        .fg(t.positive)
                        .add_modifier(ratatui::style::Modifier::BOLD),
                ));
            }
            lines.push(Line::from(spans));
        }
        // The owner's own description of the credential, whatever the verdict.
        // Stated rather than offered: a stored login makes the live row
        // *choosable* and books nothing, and an operator taking that row on a
        // login the desk cannot read is sent to the form rather than refused.
        lines.push(Line::from(Span::styled(
            format!(
                " {}",
                store
                    .desk_mode()
                    .and_then(|mode| format::text(mode.credentials.as_ref()))
                    .map(|said| format::bounded(said, SAID_MAX))
                    .unwrap_or_else(|| MISSING.to_string())
            ),
            Style::default().fg(
                match store.desk_mode().and_then(|mode| mode.credentials_ok) == Some(true) {
                    true => t.text_tertiary,
                    false => t.warning,
                },
            ),
        )));
        lines.push(match &self.note {
            Some(note) => Line::from(Span::styled(
                format!(" {}", format::bounded(note, SAID_MAX)),
                Style::default()
                    .fg(t.warning)
                    .add_modifier(ratatui::style::Modifier::BOLD),
            )),
            None => Line::from(Span::styled(
                " Enter points the desk · ↑↓ moves · Esc leaves it",
                Style::default().fg(t.text_dim),
            )),
        });
        lines
    }

    /// The box's lines: the header, a window onto the offers, and the footer.
    fn lines(&self, store: &Store, cap: usize) -> Vec<Line<'static>> {
        let t = theme();
        let rows = choices(store);
        // **The invariant this function keeps: the cursor is on screen.** Both
        // halves are derived here rather than read off the struct, because the
        // list is rebuilt from the store on every frame and the store moves
        // under it — `self.at` and `self.top` are where the walk left them, and
        // neither is a promise about a catalog that has since changed.
        //
        // The cursor is clamped to the list; the window then follows the
        // cursor. Clamping only the cursor was the bug this pair replaced: a
        // scrolled box whose catalog shrank kept `top` past the end and drew
        // `▴ N above` with no offer rows under it at all. And a box opened on a
        // row past the first window (`opened_on`, on a desk running the
        // twentieth model) drew no cursor at all, so the next Down jumped the
        // window and sent a model the operator had never seen.
        let at = self.at.min(rows.len().saturating_sub(1));
        let top = self
            .top
            // Never past the last full window, so a short list is shown whole.
            .min(rows.len().saturating_sub(cap))
            // Never below the cursor, and never so far above it that the cursor
            // falls off the bottom. These two are what make the invariant hold
            // for any `self.top` at all, including one no keystroke produced.
            .min(at)
            .max((at + 1).saturating_sub(cap));
        let mut lines = vec![panel_header("which minds"), Line::from("")];
        if rows.is_empty() {
            // Absence, stated. The catalog is fetched by the key that opened
            // this box, so an empty list is a desk that has not answered yet or
            // an owner that is not there — never a desk with no models.
            lines.push(Line::from(Span::styled(
                " the desk has not said what it can run",
                Style::default().fg(t.text_dim),
            )));
        }
        if top > 0 {
            lines.push(Line::from(Span::styled(
                format!(" ▴ {top} above"),
                Style::default().fg(t.text_dim),
            )));
        }
        for (i, row) in rows.iter().enumerate().skip(top).take(cap) {
            let on = i == at;
            let mut spans = vec![
                // A glyph and not only a colour: on a 256-colour terminal the
                // highlight is a shade, and a shade is not an answer to "which
                // row am I about to choose".
                Span::styled(
                    if on { " ▸ " } else { "   " },
                    Style::default().fg(t.accent),
                ),
                Span::styled(
                    format!("{:<10}{}", row.surface, row.value),
                    match (row.refusal.is_none(), on) {
                        (false, _) => Style::default().fg(t.text_dim),
                        (true, true) => Style::default()
                            .fg(t.accent)
                            .add_modifier(ratatui::style::Modifier::BOLD),
                        (true, false) => Style::default().fg(t.text_primary),
                    },
                ),
            ];
            if row.running {
                spans.push(Span::styled(
                    "  now",
                    Style::default()
                        .fg(t.positive)
                        .add_modifier(ratatui::style::Modifier::BOLD),
                ));
            }
            lines.push(Line::from(spans));
        }
        let hidden = rows.len().saturating_sub(top + cap);
        if hidden > 0 {
            lines.push(Line::from(Span::styled(
                format!(" ▾ {hidden} more"),
                Style::default().fg(t.text_dim),
            )));
        }
        // Stated rather than offered, exactly as the door's second question
        // states it: pointing the reasoner at a model does not switch it on —
        // the owner refuses to infer one from the other — so a box that said
        // nothing would leave an operator with a choice that changed nothing.
        lines.push(Line::from(Span::styled(
            match store.llm().and_then(|llm| llm.reasoner_enabled) {
                Some(true) => " judgment on — the reasoner uses the model chosen here",
                Some(false) => " judgment off — /model reasoner on is what puts a choice to work",
                None => " the owner did not say whether the reasoner is switched on",
            },
            Style::default().fg(t.text_dim),
        )));
        // The note outranks the key list, as the command line's does: an
        // operator who has just been refused needs the reason, not a reminder
        // of which arrow moves.
        lines.push(match &self.note {
            Some(note) => Line::from(Span::styled(
                format!(" {}", format::bounded(note, SAID_MAX)),
                Style::default()
                    .fg(t.warning)
                    .add_modifier(ratatui::style::Modifier::BOLD),
            )),
            None => Line::from(Span::styled(
                " Enter chooses · ↑↓ moves · Esc leaves them as they are",
                Style::default().fg(t.text_dim),
            )),
        });
        lines
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

/// The one field, and the two things an operator needs beside it: the shape
/// the SEC asks for, and whether this replaces something.
#[cfg(feature = "operator")]
fn contact_lines(typed: &str, stored: bool, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let room = (width as usize).saturating_sub(LABEL_W + 3);
    vec![
        panel_header("edgar contact"),
        Line::from(""),
        Line::from(vec![
            Span::styled(
                format!(" {:<LABEL_W$}", "contact"),
                Style::default().fg(t.text_secondary),
            ),
            // Plain, and clipped at the box rather than run past it. It is an
            // identity a public archive is told, not a secret — masking it
            // would teach the wrong rule about the box beside it, which is the
            // one that holds a credential.
            Span::styled(to_room(typed, room), Style::default().fg(t.text_primary)),
            Span::styled("▏", Style::default().fg(t.accent)),
        ]),
        Line::from(""),
        Line::from(Span::styled(
            " as Your Name <you@example.org>",
            Style::default().fg(t.text_dim),
        )),
        Line::from(Span::styled(
            " the SEC will not answer an anonymous caller",
            Style::default().fg(t.text_dim),
        )),
        Line::from(Span::styled(
            match stored {
                true => " a contact is already stored; this replaces it on the next save",
                false => " no contact is stored yet",
            },
            Style::default().fg(t.text_tertiary),
        )),
        Line::from(Span::styled(
            " Enter keeps it for the next save · Esc leaves it alone",
            Style::default().fg(t.text_dim),
        )),
    ]
}

/// The holdings-cap box's lines: what is typed, what it will be held to, and
/// what an empty box means.
///
/// Plain text and no mask, like the contact box: a number of names is neither a
/// secret nor an identity, and it is the one value on this pane an operator has
/// to be able to *read back* before pressing Enter.
///
/// The bound is stated rather than only enforced. The owner's own limit is the
/// size of the mandated universe, and this box says the number it will refuse
/// above — read off the snapshot's universe, or left unsaid when there is none,
/// because a bound this client invented would be a second mandate.
#[cfg(feature = "operator")]
fn cap_lines(typed: &str, store: &Store, note: Option<&str>) -> Vec<Line<'static>> {
    let t = theme();
    let universe = store.universe().len();
    let held = store
        .method()
        .and_then(|method| method.current.max_holdings);
    let mut lines = vec![panel_header("how many names"), Line::from("")];
    lines.push(Line::from(vec![
        Span::styled(
            format!(" {:<LABEL_W$}", "names"),
            Style::default().fg(t.text_secondary),
        ),
        // Plain, like the contact beside it and unlike the login form's two
        // fields: a number of names is neither a secret nor an identity, and it
        // is the one value on this pane an operator has to read back before
        // pressing Enter. `field_row` masks, and reusing it here would hide the
        // digits behind bullets in the box whose whole job is to show them.
        Span::styled(typed.to_string(), Style::default().fg(t.text_primary)),
        Span::styled("▏", Style::default().fg(t.accent)),
    ]));
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        match universe {
            0 => " the owner holds this to the size of the mandated universe".to_string(),
            n => format!(" between 1 and {n} — the names this desk watches"),
        },
        Style::default().fg(t.text_dim),
    )));
    lines.push(Line::from(Span::styled(
        match held {
            Some(cap) => format!(" this desk holds at most {cap} today"),
            None => " this desk has no cap today — the method holds what it holds".to_string(),
        },
        Style::default().fg(t.text_tertiary),
    )));
    // The note outranks the key line, as every other box on this pane has it:
    // an operator who has just been refused needs the reason rather than a
    // reminder of which key sends.
    lines.push(match note {
        Some(said) => Line::from(Span::styled(
            format!(" {}", format::bounded(said, SAID_MAX)),
            Style::default()
                .fg(t.warning)
                .add_modifier(ratatui::style::Modifier::BOLD),
        )),
        None => Line::from(Span::styled(
            " Enter sends it · an empty box or 0 clears the cap · Esc leaves it",
            Style::default().fg(t.text_dim),
        )),
    });
    lines
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

/// The default build's half: no form, no switcher, and no branch that could
/// grow one — the commands they would send are not in this build, and neither
/// is a focus for a key to be aimed by.
#[cfg(not(feature = "operator"))]
impl SettingsView {
    fn publish(&self, _area: Rect) {}
    fn draw_form(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}
    fn draw_switch(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}
    fn draw_contact(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}
    fn draw_cap(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}
    fn forget_hits(&self) {}

    /// The METHOD card with nothing over it. Every fact on it is the owner's,
    /// and in this build there is no note and no request that could hold a
    /// different answer.
    fn draw_method(&self, f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
        method_card(f, area, store, at, None, false);
    }
    fn record(&self, _cards: &[(Card, Rect)], _at: Option<Card>) {}

    /// The card without a draft over it. Every rule on it is the owner's, and
    /// in this build there is nothing that could hold a different answer.
    fn draw_news(&self, f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
        news_card(f, area, store, at, None, &[], None, None, None, false);
    }

    /// The MODELS card with the rights drawn and no cursor over them. The rows
    /// are the owner's answer and this build can post nothing, so there is no
    /// row to point at and no click to record.
    fn draw_models(&self, f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
        let _ = draw_models(f, area, store, at, None, None);
    }

    fn focused(&self, _store: &Store) -> Option<Card> {
        None
    }

    fn keys(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

/// Header, five rows, and three of slack for a credential description long
/// enough to wrap — the one value on this card that is a sentence — plus the
/// rule the block reserves, which is where the card's own footer is drawn.
const DESK_H: u16 = 10;
/// Header, eight rows, and the rule.
const POLICY_H: u16 = 10;

/// Header, seven rows, and the rule.
///
/// **Seven is what the left column had to give**, not what the card would like:
/// twenty-two rows at the baseline height, ten to POLICY, three to THEME, and
/// the `Min(0)` that used to carry the rationale is what yielded the rest.
///
/// So the body below is a budget rather than a fixed list of rows, and the
/// order of the budget is the order of what an operator cannot get any other
/// way. The pair in force is one row. The owner's warning takes as many as it
/// needs — it is nowhere else on this workstation, and half of it is a desk
/// somebody believes is fine. The entries take what is left and say how many
/// they did not draw, because every one of them is in the picker `m` opens,
/// whole, with the owner's rationale beside it.
const METHOD_H: u16 = 9;
/// Header, seven rows, and the rule.
const SYSTEM_H: u16 = 9;
/// Header, eight rows, one of slack, and the rule.
///
/// **Eleven is every row the right column has**, not what the card would like.
/// SYSTEM is nine, UNIVERSE's floor is four, and the column is twenty-four at
/// the baseline — so the rights arrived on the one spare row this pane had, and
/// took UNIVERSE down to the floor it states. A twelfth row here is not
/// available at 120x36 without pushing a card below its own floor, which is why
/// the rights are rows on this card rather than a card of their own: a separate
/// block would owe a header and a rule as well, and there are no two rows to
/// spend on chrome for three switches.
///
/// Five rows of slack, and the rights take four of them: three switches and
/// the line that says which of the three the owner actually enforces. The fifth
/// is what the availability reasons and the hand-off note share, and they are
/// counted rather than clipped: "ollama is running at 127.0.0.1:11434 but no
/// models are pulled — pull one with `ollama pull granite3.3:8b`" is three
/// wrapped rows in a half-width card and the remedy is the last third of it, so
/// a sentence that does not fit whole is replaced by the count that says how
/// many are hidden.
const MODELS_H: u16 = 11;
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
///
/// **The rights are not inside it**, and that is deliberate: the floor is what
/// the *reading* needs, and a card that refused outright because three switches
/// would not fit would take the reasoner row and its stamp off a 30-row
/// terminal to make room for a section that is not the card's first answer. So
/// the rights are the first claim on the slack instead, all four rows or none,
/// and a column too short for them is told what it is missing.
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

/// The bound the METHOD card's warning passes, and it is wider than
/// [`SAID_MAX`] on purpose.
///
/// A foreign-text guard rather than a layout device: the owner may join two
/// clauses with ` · ` — a method that holds every name, and a cap that cannot
/// reach the budget at the per-asset ceiling — and the second is the half an
/// operator acts on. This client does not parse the sentence, so it cannot cut
/// it at a clause; what decides how much is *drawn* is the card's own budget,
/// which marks the cut where it lands. Sized to the bound the module that reads
/// the owner's replies already holds foreign text to — which this file may not
/// name, by the rule `operator_gate`'s census states: the pin is a plain text
/// search, so even a comment here may not spell what is on the far side of the
/// seam.
const WARNING_MAX: usize = 240;

/// The bound a backend or model name passes. A name is a token, not a sentence:
/// `granite3.3:8b` is thirteen characters and anything past this is not one.
const NAME_MAX: usize = 36;

/// What the desk is pointed at, and whether it can reach it.
fn draw_desk(f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
    let t = theme();
    let Some(mode) = store.desk_mode() else {
        card(
            f,
            area,
            Card::Desk,
            "desk",
            at,
            vec![absent("the owner sent no desk mode")],
        );
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
    // The posture line that used to end this card is now the card's own footer,
    // on the rule below it — see `card`. It spoke for six cards from under one
    // of them, and once each card had different keys it was either wrong about
    // five or silent about all six.
    card(f, area, Card::Desk, "desk", at, rows);
}

/// What this desk reads its news from, and what it could read it from.
///
/// Returns how many source rows it drew, which is the number the cursor is
/// clamped against and the number of click rectangles the caller records. A
/// count returned rather than recomputed by the caller, because "how many rows
/// are on screen" is a fact about this function's own layout.
///
/// The draft is passed in rather than reached for, so the glass build draws the
/// same card from the same code with nothing to pass — the read-only half is an
/// absence of arguments rather than a second renderer.
#[allow(clippy::too_many_arguments)]
fn news_card(
    f: &mut Frame,
    area: Rect,
    store: &Store,
    at: Option<Card>,
    picked: Option<&[String]>,
    verified: &[NewsMember],
    cursor: Option<usize>,
    note: Option<&str>,
    sending: Option<bool>,
    edited: bool,
) -> usize {
    let t = theme();
    let wide = (area.width as usize).saturating_sub(2);
    let Some(news) = store.news() else {
        // The keys still route in this state and `save` still refuses out
        // loud, so this branch owes the same note row the drawn card does.
        // Without it the refusal was written and never painted, which is the
        // silent-key shape the wait line was added to close.
        //
        // Not "this desk reads nothing": the payload's own `configured` says
        // that, and it has not arrived. The two are a desk nobody set up and a
        // route nobody has answered yet.
        //
        // One line at the card's own width. Wrapped, the continuation landed
        // unindented beside DESK's value column and read as part of *that*
        // card's rows — and this is the state every desk is in until the first
        // fetch answers.
        let mut rows = vec![absent("nothing has said what this desk reads")];
        if let Some(said) = note {
            rows.push(Line::from(Span::styled(
                format!(" {}", to_room(&format::bounded(said, SAID_MAX), wide)),
                Style::default()
                    .fg(t.warning)
                    .add_modifier(ratatui::style::Modifier::BOLD),
            )));
        }
        card(f, area, Card::News, "news", at, rows);
        return 0;
    };
    // The mark goes in the title, where the focus tint already is: an edit this
    // window is holding and has not sent is the first thing an operator needs
    // to know about the card, and a row for it would be a row the catalog
    // needs.
    let title = match edited {
        true => "news ·edited",
        false => "news",
    };
    let ticked: Vec<String> = match picked {
        Some(picked) => picked.to_vec(),
        None => chosen(news),
    };
    let mut rows = vec![
        // The lane first, because it decides the row under it: an offline desk
        // resolves `synthetic` whatever the catalog holds, and a stack read
        // without its lane is a claim about a desk this one is not.
        kv("lane", or_missing(news.lane.as_ref()), t.text_secondary),
        kv(
            "stack",
            match news.stack.is_empty() {
                true => MISSING.to_string(),
                false => news.stack.join(" "),
            },
            t.text_primary,
        ),
    ];
    // **How many sources this card can actually draw**, with the note row
    // reserved before anything is laid out rather than after.
    //
    // The rows above are fixed ([`NEWS_TOP`] names them), the note row is one,
    // and the block reserves its own rule — so a card of `H` rows holds `H-5`
    // sources. Counting the catalog instead was the bug this replaces twice
    // over: a sixth entry pushed the note row (which is where every refusal
    // lands) off the bottom, and it let the cursor and the space bar address a
    // row no click rectangle covered, so the keyboard and the mouse disagreed
    // about what the catalog was.
    let budget = (area.height as usize).saturating_sub(5);
    let (shown, hidden) = match news.catalog.len() <= budget {
        true => (news.catalog.len(), 0),
        // The marker costs a row and is reserved *before* anything is dropped,
        // which is the reservation `draw_models` makes for the same reason.
        false => {
            let shown = budget.saturating_sub(1);
            (shown, news.catalog.len() - shown)
        }
    };
    let room = (area.width as usize).saturating_sub(NOTE_X);
    for (i, source) in news.catalog.iter().enumerate().take(shown) {
        rows.push(source_row(
            source,
            &ticked,
            news,
            verified,
            cursor == Some(i),
            room,
        ));
    }
    if hidden > 0 {
        rows.push(Line::from(Span::styled(
            format!(" ▾ {hidden} more"),
            Style::default().fg(t.text_dim),
        )));
    }
    // The last row is the note's, whether or not there is one to put in it: a
    // card that grew a row when it had something to say would move every row
    // above it, and the cursor and the click rectangles with them.
    //
    // Three things can claim it, in the operator's own order. A refusal or a
    // local message outranks everything — it is the answer to the key that was
    // just pressed. A request in flight comes next, and names its own cost,
    // because a checked save runs for minutes. And with neither, the row
    // carries the **focused source's own note, whole**: the column beside a
    // row is fifteen cells at this width, which is exactly where
    // `needs QLAB_EDGAR_CONTACT` and a verify detail get cut, and those are
    // the two strings an operator acts on.
    rows.push(
        match (note, sending, cursor.and_then(|at| news.catalog.get(at))) {
            (Some(said), _, _) => Line::from(Span::styled(
                format!(" {}", to_room(&format::bounded(said, SAID_MAX), wide)),
                Style::default()
                    .fg(t.warning)
                    .add_modifier(ratatui::style::Modifier::BOLD),
            )),
            (None, Some(checking), _) => Line::from(Span::styled(
                format!(
                    " {}",
                    to_room(
                        match checking {
                            // The cost, not just the wait. A check is one live
                            // fetch per source and the owner's catalog puts
                            // `gdelt` alone at 43–75s of it, so a line that said
                            // only "asking" would leave an operator watching a
                            // still frame with nothing saying why.
                            true => "reading one window per source… minutes",
                            false => "asking the owner…",
                        },
                        wide,
                    )
                ),
                Style::default().fg(t.accent),
            )),
            (None, None, Some(source)) => Line::from(Span::styled(
                format!(" {}", to_room(&source_note(source, news, verified).0, wide)),
                Style::default().fg(t.text_dim),
            )),
            (None, None, None) => Line::from(""),
        },
    );
    card(f, area, Card::News, title, at, rows);
    shown
}

/// How this desk solves, what it may hold, and what else it could be pointed
/// at.
///
/// Drawn from the owner's `/api/desk/method` answer and nothing else. The three
/// lists it carries are three different claims — what is in force, what may be
/// chosen, what exists and may not — and none of them is derived here: a client
/// that worked out "current" by matching a name, or "not choosable" by knowing
/// which ids are research, would be a second opinion about a mandate and a
/// catalog the owner merged.
///
/// **The warning outranks the lists, and that is the whole shape of this
/// function.** The owner's ruling is that a cap the effective method will
/// refuse *applies* — nothing is refused at set time and the failure arrives
/// later, out of `Mandate.check_targets`, as a plan that will not build. So the
/// sentence that says so is the one thing on this card an operator cannot get
/// any other way: the entries are all in the picker `m` opens, whole, with
/// their rationales, and the warning is nowhere else at all. It is drawn
/// directly under the pair it is about, before anything that could push it off
/// the bottom, and it takes as many rows as it needs — the owner may join two
/// clauses with ` · `, and half of that sentence is a desk an operator believes
/// is fine.
///
/// The draft state is passed in rather than reached for, so the glass build
/// draws the same card from the same code with nothing to pass.
fn method_card(
    f: &mut Frame,
    area: Rect,
    store: &Store,
    at: Option<Card>,
    note: Option<&str>,
    sending: bool,
) {
    let t = theme();
    let Some(method) = store.method() else {
        card(
            f,
            area,
            Card::Method,
            "method",
            at,
            vec![absent("nothing has said how this desk solves")],
        );
        return;
    };
    let wide = (area.width as usize).saturating_sub(1);
    // The card's own body, in rows: its height less the rule the block reserves
    // and the header the first line is.
    let mut budget = (area.height as usize).saturating_sub(2);
    let mut rows: Vec<Line<'static>> = Vec::new();
    let take = |rows: &mut Vec<Line<'static>>, budget: &mut usize, line: Line<'static>| {
        if *budget > 0 {
            *budget -= 1;
            rows.push(line);
        }
    };

    // The pair in force, in one row. Two rows read better and the column has
    // one: the id is what an operator types, the arm is what the predictor
    // board calls the same thing, and the cap is the number that decides
    // whether a plan builds at all.
    take(
        &mut rows,
        &mut budget,
        kv(
            // `solving` rather than `policy`, which the card below owns: two
            // rows spelling the same label is two rows an operator has to tell
            // apart, and this one carries the arm and the cap that POLICY does
            // not.
            "solving",
            to_room(
                &format!(
                    "{} · {} · {}",
                    or_missing(method.current.operational_policy.as_ref()),
                    arm_of(method),
                    match method.current.max_holdings {
                        // A cap of nothing is not no cap, and neither is a
                        // missing key: the owner sends `null` for "hold what
                        // the method holds", and `none` is this pane's word for
                        // that rather than a zero it would have invented.
                        Some(cap) => format!("cap {cap}"),
                        None => "cap none".to_string(),
                    }
                ),
                wide.saturating_sub(LABEL_W + 1),
            ),
            t.accent,
        ),
    );

    // Three things can claim the rows under it, in the operator's own order and
    // for the NEWS card's reasons. A refusal or a local message is the answer
    // to the key that was just pressed; a request in flight says why the card
    // has not moved; and with neither, the owner's warning stands — which is
    // the state it is in for as long as the mismatch lasts.
    let said = match (note, sending) {
        (Some(said), _) => Some(said.to_string()),
        (None, true) => Some(ASKING.to_string()),
        (None, false) => format::text(method.warning.as_ref()).map(str::to_string),
    };
    if let Some(said) = said {
        // Bounded as foreign text and not as a layout device — nothing on this
        // path is guaranteed to be the owner's — and bounded *wide*, because
        // the owner joins two clauses with ` · ` and the second one ("every
        // plan will refuse") is the half an operator acts on. What decides how
        // much is drawn is the budget below, and a sentence the rows cannot
        // hold is marked rather than trailing off.
        let bounded = format::bounded(&said, WARNING_MAX);
        let lines = wrap_to(&bounded, wide.max(1));
        // One row held back for the lists, always. The warning outranks them —
        // it is on this card and nowhere else — but "there are methods here"
        // is what tells an operator a key is worth pressing, and a warning long
        // enough to eat the whole body would otherwise leave a card that looks
        // like it has nothing else on it at all.
        let room = match method.operational.is_empty() && method.research.is_empty() {
            true => budget,
            false => budget.saturating_sub(1),
        };
        let cut = lines.len() > room;
        let shown = lines.len().min(room);
        for (i, line) in lines.into_iter().take(shown).enumerate() {
            take(
                &mut rows,
                &mut budget,
                Line::from(Span::styled(
                    match cut && i + 1 == shown {
                        true => format!(" {}…", to_room(&line, wide.saturating_sub(1))),
                        false => format!(" {line}"),
                    },
                    Style::default()
                        // Toned by what the line *says*, not by which flag put
                        // it there. A request in flight is not a warning, and
                        // it reaches this row two ways — as the wait itself,
                        // and as the note a second press leaves — so a tone
                        // read off `sending` alone drew the same sentence in
                        // two colours depending on how the operator got there.
                        .fg(match said == ASKING {
                            true => t.accent,
                            false => t.warning,
                        })
                        .add_modifier(ratatui::style::Modifier::BOLD),
                )),
            );
        }
    }

    // Then the two lists, in three tiers of how much room is left. **The
    // warning took what it needed first**, deliberately: every entry below is
    // in the picker `m` opens — whole, with the owner's rationale beside it —
    // and the warning is on this card and nowhere else on the workstation.
    //
    // Tier three is one line for both lists rather than none for the second:
    // "there are methods here" and "some of them are research stage" are the
    // two facts an operator needs to know a key is worth pressing, and they fit
    // in one row together.
    let entries = &method.operational;
    let research = &method.research;
    let stages = stages_of(research);
    if entries.is_empty() && research.is_empty() {
        take(
            &mut rows,
            &mut budget,
            absent("the owner named no method this desk can solve with"),
        );
    } else if budget <= 1 && !entries.is_empty() && !research.is_empty() {
        take(
            &mut rows,
            &mut budget,
            Line::from(Span::styled(
                to_room(
                    &format!(
                        " ▾ {} to choose · {} {stages} stage",
                        entries.len(),
                        research.len()
                    ),
                    wide,
                ),
                Style::default().fg(t.text_dim),
            )),
        );
    } else {
        // What may be chosen. One row each while there is room, and one line
        // naming the count when there is not — never a silent short list.
        if budget >= entries.len() && !entries.is_empty() {
            for entry in entries {
                let current = entry.current == Some(true);
                take(
                    &mut rows,
                    &mut budget,
                    Line::from(vec![
                        Span::styled(
                            if current { " ▸ " } else { "   " },
                            Style::default().fg(t.accent),
                        ),
                        Span::styled(
                            format!(
                                "{:<20}{}",
                                to_room(&or_missing(entry.id.as_ref()), 19),
                                to_room(&or_missing(entry.arm_id.as_ref()), 4)
                            ),
                            match current {
                                true => Style::default().fg(t.text_primary),
                                false => Style::default().fg(t.text_secondary),
                            },
                        ),
                        Span::styled(
                            match current {
                                true => "  now",
                                false => "",
                            },
                            Style::default()
                                .fg(t.positive)
                                .add_modifier(ratatui::style::Modifier::BOLD),
                        ),
                    ]),
                );
            }
        } else if !entries.is_empty() {
            take(
                &mut rows,
                &mut budget,
                Line::from(Span::styled(
                    to_room(
                        &format!(" ▾ {} methods this desk can solve with", entries.len()),
                        wide,
                    ),
                    Style::default().fg(t.text_dim),
                )),
            );
        }

        // And what may not, with the stage that says why. **This is how the desk
        // says why a research-stage method cannot be picked** — the owner
        // refuses one with a sentence about evidence and a catalog change, and
        // an operator who never sees the name here would go looking for the key
        // that chooses it. So the *reason* survives down to the last row: at one
        // row it is a count and a stage, and only the names go.
        if !research.is_empty() && budget > 1 {
            take(
                &mut rows,
                &mut budget,
                Line::from(Span::styled(
                    to_room(&format!(" not choosable — {stages} stage"), wide),
                    Style::default().fg(t.text_tertiary),
                )),
            );
            // The marker costs a row and is reserved *before* anything is
            // dropped, which is the reservation the NEWS card and MODELS both
            // make for the same reason.
            let shown = match research.len() <= budget {
                true => research.len(),
                false => budget.saturating_sub(1),
            };
            for entry in research.iter().take(shown) {
                take(
                    &mut rows,
                    &mut budget,
                    Line::from(Span::styled(
                        format!("   {}", to_room(&or_missing(entry.id.as_ref()), wide)),
                        Style::default().fg(t.text_dim),
                    )),
                );
            }
            let hidden = research.len() - shown;
            if hidden > 0 {
                take(
                    &mut rows,
                    &mut budget,
                    Line::from(Span::styled(
                        format!("   ▾ {hidden} more"),
                        Style::default().fg(t.text_dim),
                    )),
                );
            }
        } else if !research.is_empty() {
            take(
                &mut rows,
                &mut budget,
                Line::from(Span::styled(
                    to_room(
                        &format!(
                            " ▾ {} more are {stages} stage — not choosable",
                            research.len()
                        ),
                        wide,
                    ),
                    Style::default().fg(t.text_dim),
                )),
            );
        }
    }
    card(f, area, Card::Method, "method", at, rows);
}

/// The stage the unchoosable entries share, or `research` when they do not.
///
/// The owner's own word, never this client's: the catalog has `offline` as well
/// as `research`, and a card that hard-coded one would tell an operator the
/// wrong thing about why a method is not on offer. Falls back to `research`
/// only when the owner named no stage at all, and the picker lists each entry's
/// own word beside it either way.
fn stages_of(research: &[crate::model::ResearchEntry]) -> String {
    let mut named: Vec<&str> = research
        .iter()
        .filter_map(|entry| format::text(entry.stage.as_ref()))
        .collect();
    named.sort_unstable();
    named.dedup();
    match named.len() {
        1 => named[0].to_string(),
        _ => "research".to_string(),
    }
}

/// The research arm the desk's current method is registered as, or `--`.
///
/// Read off the entry the owner *marked* current rather than matched by id: the
/// mark is the owner's own merge of the mandate and its overrides, and a
/// comparison made here would disagree with it the first time one of them
/// spelled a name differently.
fn arm_of(method: &MethodSettings) -> String {
    method
        .operational
        .iter()
        .find(|entry| entry.current == Some(true))
        .and_then(|entry| format::text(entry.arm_id.as_ref()))
        .unwrap_or(MISSING)
        .to_string()
}

/// `said`, broken into lines of at most `room` cells, on word boundaries where
/// there is one.
///
/// The card needs the *lines*, not the count: it draws each as its own `Line`
/// so a row can be dropped at the budget rather than clipped by `Paragraph`
/// halfway through a sentence nobody can then finish reading.
fn wrap_to(said: &str, room: usize) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut line = String::new();
    for word in said.split_whitespace() {
        let width = line.chars().count();
        if width > 0 && width + 1 + word.chars().count() > room {
            lines.push(std::mem::take(&mut line));
        }
        if !line.is_empty() {
            line.push(' ');
        }
        // A single word longer than the room is cut rather than dropped: it is
        // one of the owner's own ids, and a blank row says less than a cut one.
        line.push_str(&to_room(word, room));
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines
}

/// What one source has to say for itself, and whether it is a problem.
///
/// **Four facts in one column, in the operator's own order.** What the source
/// is *waiting for* outranks what a check just *found*, which outranks what
/// the desk's own window last *did*, which outranks what it *costs* — a source
/// the desk cannot read at all is not the place to print its price, and a
/// verify the operator just asked for is newer than the window's last outcome.
///
/// Split out of the row because two surfaces draw it: the fifteen-cell column
/// beside the row, and the full-width line under the catalog that carries the
/// focused row's copy whole. Two derivations of "what is wrong with this
/// source" is two chances for one of them to be the one that drifted.
///
/// The `bool` is *a problem*, not *unavailable*: a `partial:` flag on a source
/// that answered is a fact about a feed rather than a warning about the desk,
/// and toning it as one would train an operator to read past the rows that are.
/// Whether this source is waiting for something before the desk can read it at
/// all.
///
/// **Not the same question as "is anything wrong with it".** A feed that
/// answered a check with one of its own sources missing has something wrong
/// with it and is perfectly readable; a source with no credential is neither
/// readable nor wrong. The row draws them in two different places — one dims
/// the tick and the name, the other tints the note — and merging them was the
/// bug this split fixes: a verify that came back 503 dimmed the row as if the
/// desk could not reach the source at all, which is a claim about the
/// configuration rather than about today's fetch.
///
/// One definition, read by the row and by the note it carries.
///
/// `available` is `Some(false)` only: a source nothing has answered about is
/// not one the desk has refused. The second half is the one thing this client
/// can tell the owner has not been given — edgar's contact. The owner reports
/// the *want* (`needs`) and separately whether one is stored, and a row that
/// printed a cost over an unmet requirement would leave the operator ticking a
/// source that cannot be saved.
fn wanting(source: &NewsSource, news: &NewsSettings) -> bool {
    source.available == Some(false)
        || (or_missing(source.name.as_ref()) == EDGAR
            && news.edgar_contact_set != Some(true)
            && format::text(source.needs.as_ref()).is_some())
}

fn source_note(
    source: &NewsSource,
    news: &NewsSettings,
    verified: &[NewsMember],
) -> (String, bool) {
    let name = or_missing(source.name.as_ref());
    if wanting(source, news) {
        return (
            format::text(source.needs.as_ref())
                .map(|needs| format!("needs {needs}"))
                .unwrap_or_else(|| "the desk cannot read it".to_string()),
            true,
        );
    }
    if let Some(member) = verified.iter().find(|member| member.name == name) {
        return member.said();
    }
    if let Some(outcome) = news.outcomes.get(&name) {
        return (outcome.clone(), false);
    }
    if name == EDGAR && news.edgar_contact_set == Some(true) {
        return ("contact set".to_string(), false);
    }
    (
        format::text(source.cost.as_ref()).unwrap_or("").to_string(),
        false,
    )
}

/// One source, as the owner describes it and as the draft has it.
#[allow(clippy::too_many_arguments)]
fn source_row(
    source: &NewsSource,
    ticked: &[String],
    news: &NewsSettings,
    verified: &[NewsMember],
    on: bool,
    room: usize,
) -> Line<'static> {
    let t = theme();
    let name = or_missing(source.name.as_ref());
    let held = ticked.contains(&name);
    // Two different facts, drawn in two different places. `out` is whether the
    // desk can read this source at all, which dims the tick and the name;
    // `bad` is whether something is wrong with it right now, which tints the
    // note. One `bool` for both said that a feed which answered a check with a
    // 503 was a source this desk cannot reach — a claim about the
    // configuration rather than about today's fetch.
    let out = wanting(source, news);
    let (said, bad) = source_note(source, news, verified);
    Line::from(vec![
        // A glyph and not only a colour, for the switcher's reason: on a
        // 256-colour terminal a highlight is a shade, and a shade is not an
        // answer to "which row is the space bar about".
        Span::styled(
            if on { " ▸ " } else { "   " },
            Style::default().fg(t.accent),
        ),
        Span::styled(
            match held {
                true => "[x] ",
                false => "[ ] ",
            },
            Style::default().fg(match (held, out) {
                (_, true) => t.text_dim,
                (true, _) => t.positive,
                (false, _) => t.text_dim,
            }),
        ),
        Span::styled(
            format!("{name:<8}"),
            Style::default().fg(match out {
                true => t.text_dim,
                false => t.text_primary,
            }),
        ),
        Span::styled(
            format!("{:<10}", or_missing(source.tier.as_ref())),
            Style::default().fg(t.text_secondary),
        ),
        // Dim unless it is a problem: a `partial:` flag on a source that
        // answered is a fact about a feed, not a warning about the desk, and
        // toning it as one would train an operator to read past the rows that
        // are.
        Span::styled(
            to_room(&said, room),
            Style::default().fg(match bad {
                true => t.warning,
                false => t.text_dim,
            }),
        ),
    ])
}

/// The sources the owner says are chosen right now, sorted.
///
/// Read off `chosen` and never off `stack`: they are different claims. A desk
/// on the synthetic lane *resolves* `["synthetic"]` whatever is configured, and
/// a draft seeded from that would un-tick every source the operator has set up
/// the moment they touched a row.
fn chosen(news: &NewsSettings) -> Vec<String> {
    let mut names: Vec<String> = news
        .catalog
        .iter()
        .filter(|source| source.chosen == Some(true))
        .filter_map(|source| format::text(source.name.as_ref()))
        .map(str::to_string)
        .collect();
    names.sort();
    names
}

/// Whether the desk is on this lane now, by the owner's own two words.
#[cfg(feature = "operator")]
fn running_lane(row: &LaneRow, store: &Store) -> bool {
    let Some(mode) = store.desk_mode() else {
        return false;
    };
    format::text(mode.data.as_ref()) == Some(row.data)
        && format::text(mode.book.as_ref()) == Some(row.book)
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
fn draw_policy(f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
    let t = theme();
    let Some(policy) = store.policy() else {
        card(
            f,
            area,
            Card::Policy,
            "policy",
            at,
            vec![absent("the owner sent no policy")],
        );
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
    card(f, area, Card::Policy, "policy", at, rows);
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
fn draw_system(f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
    let t = theme();
    let Some(system) = store.system() else {
        card(
            f,
            area,
            Card::System,
            "system",
            at,
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
    card(f, area, Card::System, "system", at, rows);
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
fn draw_models(
    f: &mut Frame,
    area: Rect,
    store: &Store,
    at: Option<Card>,
    cursor: Option<usize>,
    note: Option<&str>,
) -> bool {
    let t = theme();
    let Some(llm) = store.llm() else {
        card(
            f,
            area,
            Card::Models,
            "models",
            at,
            vec![absent("the owner sent no model routing")],
        );
        return false;
    };
    // The value column this card actually has, rather than the one it has at
    // the baseline. A composed value wider than this wraps onto an unindented
    // second row and spends a slack row the reasons need.
    let room = (area.width as usize).saturating_sub(LABEL_W + 1);
    let inner_w = area.width.max(1) as usize;
    let cost = |reason: &String| wrapped_rows(reason.chars().count() + 1, inner_w);
    let mut notes = unreachable_reasons(llm);
    // What shape the tab ATLAS draws takes on this desk, in the one row this
    // card has for it. Posture, not build — see [`atlas_note`].
    let tab = atlas_note(llm, store.posture.writes());
    // A note that exists is either shown whole or counted, and the count needs
    // a row of its own.
    let floor = MODELS_MIN_H + u16::from(!notes.is_empty() || tab.is_some());
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
        return false;
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
    // The rights take their four rows out of the slack, before the sentences
    // and never instead of the reading: three switches drawn over a reasoner
    // row this column had to drop would be a card that answered the *second*
    // question and not the first.
    //
    // All four or none of them. Two rights and a cut qualifier is the one shape
    // this section may not take — a right shown as granted, with the sentence
    // that says the owner enforces only one of the three left off the bottom,
    // reads as a gate the desk holds shut. So a column that cannot hold the
    // section is told what it is missing, in the count this card already uses
    // for everything it could not draw whole.
    let mut budget = (area.height - MODELS_MIN_H) as usize;
    let (rights, spent) = rights_rows(store, cursor, note, inner_w);
    // **The deferral owns a row before anything else is cut.** It was note zero
    // in the queue once, which meant the marker swallowed it: the card said
    // `▾ 2 more` and named neither the rights nor the reason, so a short column
    // hid the whole section behind a number that described nothing. What is
    // reserved here is the *fact that the rights are not on screen*, which is
    // the one thing this card cannot leave to a count.
    let deferred = match spent <= budget {
        true => {
            rows.extend(rights);
            budget -= spent;
            None
        }
        false => Some(deferral(store, spent)),
    };
    // Whole sentences or a count, never half of one: the remedy is the last
    // third of the owner's longest reason, so a clipped one is a fix an
    // operator cannot run. The marker costs a row, and it is reserved before
    // anything is dropped rather than after — `views::desk::fit` makes the same
    // reservation, for the same reason.
    let mut left = budget.saturating_sub(usize::from(deferred.is_some()));
    // The tab note joins the queue last, and only when the sentences the
    // *owner* wrote have room to spare. It is this client's own copy about a
    // configuration that will read the same on the next frame, and a card with
    // one row of slack and a down daemon would otherwise count both and draw
    // neither — trading the remedy an operator can act on for a line that is
    // still there after they have run it.
    if let Some(said) = tab {
        let wanted: usize = notes.iter().map(cost).sum();
        if wanted + cost(&said) <= left {
            notes.push(said);
        }
    }
    let all_fit = notes.iter().map(cost).sum::<usize>() <= left;
    // A row of its own for the count, and only when there is one to spare: a
    // marker pushed into a row the card does not have is clipped by the
    // `Paragraph` without a word.
    let marker_row = !all_fit && left > 0;
    if !all_fit {
        left = left.saturating_sub(1);
    }
    let mut hidden = 0usize;
    let mut drawn: Vec<Line<'static>> = Vec::new();
    for reason in &notes {
        match cost(reason) <= left {
            true => {
                left -= cost(reason);
                drawn.push(Line::from(Span::styled(
                    format!(" {reason}"),
                    // Dim: the reason explains the tone on the row above, and a
                    // second warning-coloured line would compete with it.
                    Style::default().fg(t.text_dim),
                )));
            }
            false => hidden += 1,
        }
    }
    // Where the rows would have been, above the sentences rather than under
    // them. With no row left for a separate count it carries that count too:
    // the alternative is a card that names the rights and then hides a reason
    // silently, which is the failure the marker exists to prevent.
    if let Some(said) = deferred.as_ref().filter(|_| budget > 0) {
        rows.push(Line::from(Span::styled(
            match hidden > 0 && !marker_row {
                true => format!(" ▾ {} more, incl. the rights", hidden + 1),
                false => format!(" {said}"),
            },
            Style::default().fg(t.text_dim),
        )));
    }
    rows.extend(drawn);
    if hidden > 0 && marker_row {
        rows.push(Line::from(Span::styled(
            format!(" ▾ {hidden} more"),
            Style::default().fg(t.text_dim),
        )));
    }
    card(f, area, Card::Models, "models", at, rows);
    // Whether the three rows a click may press are on screen. Answered by the
    // draw rather than derived from the constants by the caller, for the reason
    // `record` states: a rectangle list built from what the layout *asked for*
    // would answer about a frame the column's height refused.
    deferred.is_none() && !broken_rights(store)
}

/// What the card says in place of the rights when the column cannot hold them.
///
/// Two sentences, because the two states are not the same fact and one of them
/// misdescribes the other. A card short of rows has three switches it could not
/// draw; a desk whose rights file the owner refused to read has none to draw at
/// all, and telling that operator the section "needs four rows" would send them
/// to resize a terminal over a file they have to fix.
fn deferral(store: &Store, wanted: usize) -> String {
    match broken_rights(store) {
        true => "▾ the rights file could not be read".to_string(),
        false => format!("▾ {RIGHTS_ROWS} rights need {wanted} rows"),
    }
}

/// Whether the owner could not read the rights file at all.
///
/// One reader for the two places that ask — the card, which puts the sentence
/// where the switches would be, and the click map, which must record nothing
/// over it.
fn broken_rights(store: &Store) -> bool {
    store.rights().is_some_and(|rights| rights.error.is_some())
}

/// The three authorities the operator lends Atlas, and one line saying how far
/// any of it goes.
///
/// **Four rows inside [`MODELS_MIN_H`], never in the slack.** A right drawn as
/// granted, with nothing beside it, reads as a gate the desk holds shut — and
/// two of the three are not gates at all. So the sentence that says which one
/// the owner enforces is as fixed as the rows it qualifies, and a column too
/// short for both refuses the card rather than dropping the qualifier.
///
/// The rights the owner could not read are the one state where the rows
/// themselves are dropped: a file this desk did not write is refused by the
/// owner's reader with the remedy in the sentence, and three `--` rows over it
/// would bury the one thing an operator can act on. Invariant 4.
fn rights_rows(
    store: &Store,
    cursor: Option<usize>,
    note: Option<&str>,
    inner_w: usize,
) -> (Vec<Line<'static>>, usize) {
    let t = theme();
    let Some(rights) = store.rights() else {
        // Nothing has answered. The rows still draw — the card says which
        // authorities exist whether or not the desk has named them — and the
        // mark is the missing one rather than the owner's default, which would
        // be this client asserting a desk state nobody confirmed.
        return unanswered_rights(cursor, note, inner_w);
    };
    if let Some(said) = &rights.error {
        // The owner's sentence, bounded like every other foreign text on this
        // card, in the four rows the rights would have taken. Warning-toned:
        // the desk is running on a rights file nobody can read, and what Atlas
        // is actually being offered is unknown until it is fixed.
        // Bounded to [`WARNING_MAX`] rather than to [`SAID_MAX`], and the
        // METHOD card's warning states the reason: the guard is against foreign
        // text running away, and what decides how much is *drawn* is the card's
        // own budget. 112 cells cuts this sentence off mid-path, and the remedy
        // — which file to delete, and that this panel can set the rights
        // instead — is its last third.
        //
        // **Whole, or not at all.** Fitting it to whatever slack there happened
        // to be is what produced `the owner answered 500: /state/atlas…` on a
        // short column: the status kept and the remedy gone, which is the
        // clipped-sentence failure this card spends rows to avoid. The cost
        // returned here is the whole sentence's, so a column that cannot hold
        // it defers the section and says so in one row instead.
        let said = format::bounded(said, WARNING_MAX);
        return (
            vec![Line::from(Span::styled(
                format!(" {said}"),
                Style::default().fg(t.warning),
            ))],
            // One line, as many rows as it wraps to. It takes the section's
            // place whole: there is nothing to say about three switches whose
            // file cannot be read.
            wrapped_rows(said.chars().count() + 1, inner_w),
        );
    }
    let mut rows: Vec<Line<'static>> = RightsFlags::FIELDS
        .iter()
        .enumerate()
        .map(|(at, field)| right_row(field, rights.rights.get(field), cursor == Some(at)))
        .collect();
    let (line, spent) = last_rights_line(note, inner_w);
    rows.push(line);
    (rows, RIGHTS_ROWS + spent)
}

/// The line under the three rows: the asymmetry, or whatever was last said
/// about a change.
///
/// The note **replaces** the asymmetry rather than being drawn under it. The
/// row is the one this card has, and a sentence that had to win a row from the
/// availability reasons would be the first thing a short column dropped — which
/// is the one thing a refusal must never be silent about.
///
/// Bounded to two rows rather than to [`SAID_MAX`] because that is what the
/// card can actually give it: one row of its own and one out of the slack. The
/// whole sentence is on the toast the same outcome raised.
fn last_rights_line(note: Option<&str>, inner_w: usize) -> (Line<'static>, usize) {
    let t = theme();
    let Some(said) = note else {
        return (
            Line::from(Span::styled(
                format!(" {ASYMMETRY}"),
                // Dim: it qualifies the three rows above it rather than
                // competing with them, and a warning colour on a desk nobody
                // has narrowed would tone the default state as a problem.
                Style::default().fg(t.text_dim),
            )),
            1,
        );
    };
    let said = to_room(said, inner_w.saturating_mul(2).saturating_sub(1));
    let rows = wrapped_rows(said.chars().count() + 1, inner_w);
    (
        Line::from(Span::styled(
            format!(" {said}"),
            Style::default()
                .fg(t.warning)
                .add_modifier(ratatui::style::Modifier::BOLD),
        )),
        rows,
    )
}

/// The rights rows before anything has answered, with the same line under them.
fn unanswered_rights(
    cursor: Option<usize>,
    note: Option<&str>,
    inner_w: usize,
) -> (Vec<Line<'static>>, usize) {
    let mut rows: Vec<Line<'static>> = RightsFlags::FIELDS
        .iter()
        .enumerate()
        .map(|(at, field)| right_row(field, None, cursor == Some(at)))
        .collect();
    let (line, spent) = last_rights_line(note, inner_w);
    rows.push(line);
    (rows, RIGHTS_ROWS + spent)
}

/// One right: the cursor mark, the name, where it stands, and what it shapes.
///
/// The tone is on the **state token alone**. What the right shapes is a fact
/// about the desk whichever way the switch is thrown, so colouring the whole
/// value would make "off · the /build key" read as a broken hand-off rather
/// than as one the operator closed.
fn right_row(field: &str, held: Option<bool>, on_cursor: bool) -> Line<'static> {
    let t = theme();
    Line::from(vec![
        // The mark takes the leading space every other row on this card has, so
        // the value column stays where `kv` puts it — a cursor that shifted the
        // rows sideways would make the card jump under the arrows.
        Span::styled(
            // `▸` where NEWS puts it, and a space of the same width where it
            // is not: a mark that changed the row's width would make the value
            // column jump under the arrows.
            format!(
                "{}{field:<w$}",
                if on_cursor { "▸" } else { " " },
                w = LABEL_W
            ),
            Style::default().fg(match on_cursor {
                true => t.accent,
                false => t.text_secondary,
            }),
        ),
        Span::styled(
            match held {
                Some(true) => "on".to_string(),
                Some(false) => "off".to_string(),
                None => MISSING.to_string(),
            },
            Style::default().fg(match held {
                Some(true) => t.accent,
                _ => t.text_secondary,
            }),
        ),
        Span::styled(shapes(field).to_string(), Style::default().fg(t.text_dim)),
    ])
}

/// What one right actually reaches, in the owner's own division of it.
///
/// **`workflows` says "for chat" and may not say more.** The owner's gate
/// (`_refuse_without_workflows_right`) returns early on anything that is not
/// the desk chat: a human-started `qlab workforce run`, the owner's own
/// `Coordinator.drive`, the heartbeat's autonomous dispatch and this
/// workstation's own buttons carry another origin or none, and none of them is
/// gated. A granite or ollama reasoner is ungated by construction as well — it
/// reaches the owner with its own client and stamps no origin header at all —
/// which is a live reading of this very card, drawn one row above the note that
/// says the reasoner is not claude.
///
/// So "owner-enforced" was an overclaim: it invited an operator to read a
/// withdrawn right as a gate the desk holds shut against every caller, and it
/// is shut against exactly one.
fn shapes(field: &str) -> &'static str {
    match field {
        "web" => " · chat, /cli tools",
        // Refused by name — `workflow.start`, `resume` and `atlas.task.create`
        // — and only when the chat is what asked.
        "workflows" => " · refused for chat",
        "build" => " · the /build key",
        _ => "",
    }
}

/// The line the three rows may not be drawn without.
///
/// **It states the half of the asymmetry no row can carry.** Each row says what
/// its own right reaches — `web` and `build` shape the tool grant a chat or a
/// hand-off is launched with and nothing else; `workflows` is refused, and
/// refused for chat. What is left over is the scope those three share, and it
/// is the thing an operator would otherwise assume the opposite of: a
/// `qlab workforce run`, the owner's own coordinator, the heartbeat's dispatch
/// and a granite reasoner making its own owner call are none of them bound by
/// anything on this card.
///
/// Rights are an operator's stated intent, exactly like the posture, and this
/// line is what keeps the card from reading as more than that.
///
/// Written to one row at the card's own width: the block draws 38 cells here,
/// and a second row is one this column does not have (see [`MODELS_H`]).
const ASYMMETRY: &str = "nothing here binds a non-chat caller";

/// What shape the ATLAS tab takes on this desk, and which mind decides it.
///
/// **The tab has two bodies, and the mind decides whether it can hold the
/// second.** `/cli` no longer hands the workstation to a child: it runs the
/// desk's own Claude verb inside ATLAS's own column, so a claude desk's tab is
/// the chat or a live terminal. A desk reasoning on anything else has one body
/// — that verb refuses by name there — while the chat itself still answers, on
/// the local model. Neither half is on any row of this card: `reasoner` names
/// the mind without saying which surface it drives, and `build` names a right
/// without saying which mind the key opens.
///
/// **The posture, never the build**, exactly as [`Card::footer`] gates on it.
/// `/cli` and `/build` are both `writes` scopes, so a window the desk has not
/// armed is offered neither and may not be told what they would do. It is also
/// what keeps the read-only card one frame in both artifacts: the sentence that
/// names a pane cannot render in a `--no-default-features` build, which has no
/// pane and no posture that passes this — by construction rather than by
/// wording.
///
/// One row or none. This card has a single slack row for it and a note past 37
/// cells is counted rather than drawn, which is what bounds both sentences —
/// and why the claude one does not repeat the backend name the `reasoner` row
/// two above already carries.
///
/// Silent when nothing has named a mind: the startup door is what asks, and a
/// note on an unanswered desk would be a distinction this client invented.
fn atlas_note(llm: &LlmConfig, writes: bool) -> Option<String> {
    if !writes {
        return None;
    }
    let backend = surface_backend(llm.reasoner.as_ref())?;
    if backend == CLAUDE {
        return Some("ATLAS: the chat, or a /cli terminal".to_string());
    }
    // The mirror of the refusal the key itself gives, which sends the operator
    // to this card: the chat is the local model's, the verb is claude's.
    Some(format!("chat: {backend} · /cli: {CLAUDE}'s verb"))
}

/// The one mind the desk's Claude verbs run, whatever this desk reasons with.
///
/// Its own constant beside the row that renders `claude · tiers decide`, and
/// the same string: the owner's `model_routing.CLAUDE_BACKEND`. `/build`
/// launches it however the reasoner is configured; `/cli` is the same verb and
/// therefore refuses rather than opening a second mind's terminal in the tab
/// Atlas already runs in, which is the asymmetry [`atlas_note`] states.
const CLAUDE: &str = "claude";

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
/// is 31 and wraps onto an unindented second row, spending the card's *one*
/// remaining slack row — the rights took the other four (see [`MODELS_H`]), so
/// what a wrapped value costs here is the whole of what is left for the
/// availability reasons, and a reason that does not fit whole is counted rather
/// than drawn.
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
fn draw_universe(f: &mut Frame, area: Rect, store: &Store, at: Option<Card>) {
    let t = theme();
    let symbols = store.universe();
    if symbols.is_empty() {
        card(
            f,
            area,
            Card::Universe,
            "universe",
            at,
            vec![absent("no universe in the last snapshot")],
        );
        return;
    }
    let (block, header) = chrome(Card::Universe, "universe", at);
    let inner = block.inner(area);
    f.render_widget(block, area);
    let rows = Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(inner);
    f.render_widget(
        Paragraph::new(vec![
            header,
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

fn draw_theme(f: &mut Frame, area: Rect, at: Option<Card>) {
    let t = theme();
    card(
        f,
        area,
        Card::Theme,
        "theme",
        at,
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
fn card(
    f: &mut Frame,
    area: Rect,
    which: Card,
    title: &str,
    at: Option<Card>,
    rows: Vec<Line<'static>>,
) {
    let (block, header) = chrome(which, title, at);
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines = vec![header];
    lines.extend(rows);
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

/// One card's block and its header line — the two halves that say whether this
/// card is listening and what it would do about it.
///
/// Split out because UNIVERSE cannot use [`card`]: its symbol list is a second
/// wrapped region under the fixed rows, so it lays out its own inner rect. It
/// still owes the operator the same two statements, and a card that built its
/// own chrome would be the one that quietly stopped making them.
///
/// **The footer goes on the rule, not into a row.** The block already reserves
/// that line, so a card states its keys without spending content on it — and a
/// footer that had to win a row would be the first thing a short column
/// dropped, which is the one thing it must never be silent about (the same
/// lesson that moved the MODELS stamp to the top of its card).
///
/// Drawn on **every** card in a glass window and on the **focused** card only
/// in an armed one. With no focus there is nothing to point at and every card's
/// answer is the same sentence; with a focus, a key list under a card that
/// would not act on it is an instruction with nothing behind it.
fn chrome(which: Card, title: &str, at: Option<Card>) -> (Block<'static>, Line<'static>) {
    let t = theme();
    let focused = at == Some(which);
    let mut block = panel_block();
    if focused || at.is_none() {
        block = block.title_bottom(Line::from(Span::styled(
            format!(" {} ", which.footer(at.is_some())),
            Style::default().fg(match focused {
                true => t.accent,
                false => t.text_dim,
            }),
        )));
    }
    // The title carries the focus, never the bar: every panel on this
    // workstation opens with an accent `▌`, so a bar that changed colour would
    // be one shade against another rather than an answer.
    let mut header = panel_header(title);
    if focused {
        if let Some(span) = header.spans.get_mut(1) {
            span.style = span.style.fg(t.accent);
        }
    }
    (block, header)
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
    fn what_is_chosen_is_read_off_the_catalog_and_never_off_the_resolved_stack() {
        // Two different claims, and the owner makes both. A desk on the
        // synthetic lane *resolves* `["synthetic"]` whatever is configured, so
        // a draft seeded from `stack` would un-tick every source the operator
        // has set up the moment they touched a row.
        let news: NewsSettings = serde_json::from_value(serde_json::json!({
            "lane": "synthetic",
            "stack": ["synthetic"],
            "catalog": [
                {"name": "macro", "chosen": true},
                {"name": "rss", "chosen": true},
                {"name": "gdelt", "chosen": false},
                // A name the owner sent empty is absent, as everywhere here.
                {"name": "", "chosen": true}
            ]
        }))
        .unwrap();
        assert_eq!(chosen(&news), vec!["macro".to_string(), "rss".to_string()]);
        // And an owner that sent no catalog has chosen nothing, rather than
        // this client inferring a stack from the lane it resolved.
        assert!(chosen(&NewsSettings::default()).is_empty());
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
