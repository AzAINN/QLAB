//! Frame layout: the chrome around the views — how one frame is carved into regions.
//!
//! Five regions, and only one of them belongs to a view: a ticker row, the nav
//! rail, the content area, the pulse rail, and the status line. The rails are
//! always on screen because the two questions an operator asks between glances
//! — *what is the market doing* and *what is Atlas doing* — must never be a
//! view switch away.
//!
//! `draw` is a pure function of the `Store` and the instant it is told it is
//! drawing at. Nothing here reads a clock, opens a socket, or holds a client,
//! which is what lets one golden frame pin the whole layout — including how a
//! frame looks forty seconds after the last snapshot — and what keeps the
//! read-only posture a property of the composition root rather than a rule
//! each view has to remember.
//!
//! It also *publishes* the regions it carved, into the `RefRect`s `Fx` holds, so
//! the effect rules can aim at panes without a second spelling of this layout
//! and without the shell growing state of its own. Publishing is not painting:
//! the frame is still a pure function of (store, effects, instant), and the
//! effect pass that reads those rects runs after `draw` returns.

use crate::cmd::{self, CmdState, Command, Edit, Resolved, Scope};
use crate::format::{self, MISSING};
use crate::fx::{Fx, ShellRects};
use crate::glyph;
use crate::store::{Focus, Posture, Store, ViewId};
use crate::theme::theme;
use crate::theme::Theme;
use crate::ui::views::{book::PlanAt, Selected, Views};
use crate::ui::widgets::{help, panel_block, panel_header, pulse, ticker};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Wrap},
    Frame,
};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

/// Exactly wide enough for `▌8 AUDIT` — the active marker, the digit, a space,
/// and the longest label. Widening it would take cells from the content.
///
/// Public because the golden harness crops a frame down to the columns a view
/// owns, and a second spelling of the layout in the tests would drift from this
/// one the first time a rail is resized.
pub const NAV_W: u16 = 8;
/// The pulse rail. Wide enough for a label column and a value column at the
/// widths `format` produces; narrower and the money figures start truncating.
pub const PULSE_W: u16 = 34;
/// The narrowest column a Claude session is worth running in.
///
/// Measured, not chosen: at 120×36 the settled pane is 77 cells, and the
/// widget's own floor is the width at which a border can still *name the key
/// that returns the keyboard* — which is a pane an operator can leave, not one
/// they can work in. Below this the desk rail gives up its column, because a
/// rail standing beside a terminal nobody can use is the wrong half to keep.
const PANE_MIN_W: u16 = 60;
/// The label column inside the rails.
const LABEL_W: usize = 11;
/// The read panel: header, four facts, and the rule its block reserves.
const READ_H: u16 = 6;
/// The glyph and the mood word under it.
const GLYPH_H: u16 = glyph::H as u16 + 1;
/// The right-hand end of the status line, as a target for the effect pass.
///
/// Deliberately *not* the width of everything the chips can say — at their
/// loudest, with both drop counters, a stream warning, staleness, MALFORMED and
/// the posture, the run is comfortably past seventy cells. This is the region an
/// amber pulse should cross, which is the quiet end nearest the badge. It is not
/// a layout constraint either: `draw_status` packs the line itself.
///
/// Still the neighbourhood rather than one chip's own rect. Narrowing it needs
/// the status line to carry an approvals chip to aim at, and this line has no
/// room for one until the layout modes land — a pulse aimed at a rect nothing
/// draws into would be an effect over empty cells.
const CHIPS_W: u16 = 40;

/// One frame.
///
/// `fx` rides alongside the store rather than inside it: the store is what the
/// owner said plus the diff of it, and a decaying animation stamp is neither.
/// Passing it here is what keeps the frame a pure function of (state, effects,
/// instant) — the property every golden test depends on.
///
/// `views` is borrowed rather than built: the instances outlive the frame, so
/// where an operator has put a selection or a crosshair survives the repaint.
pub fn draw(f: &mut Frame, store: &Store, views: &Views, fx: &Fx, now: Instant) {
    let t = theme();
    let area = f.area();
    fill(f, area, t.bg_base);
    let stale = stale_for(store, now);

    let strip = strip_rows(store, area.height);
    let rows = Layout::vertical([
        Constraint::Length(1),     // ticker
        Constraint::Min(0),        // rails + content
        Constraint::Length(strip), // the suggestions, while the line is focused
        Constraint::Length(1),     // command line / status
    ])
    .split(area);
    let rail = rail_width(rows[1].width, pane_showing(store));
    let cols = Layout::horizontal([
        Constraint::Length(NAV_W),
        Constraint::Min(0),
        Constraint::Length(rail),
    ])
    .split(rows[1]);

    // The tick count is the offset: one display cell per beat, so the tape's
    // position is state rather than a clock read inside a renderer.
    ticker::draw(
        f,
        rows[0],
        &store.asset_views(),
        store.tick as usize,
        store.stale_after,
        &fx.flashes,
        now,
    );
    draw_nav(f, cols[0], store, t);

    // The rules belong to the shell, not to the panes: a view that drew its own
    // left border would have to know it was not the leftmost thing on screen.
    let content = left_rule(f, cols[1], t);
    if store.snapshot.is_none() {
        draw_no_data(f, content, store, t);
    } else {
        views.draw(store.nav.view, f, content, store, &fx.flashes, now);
    }

    match rail > 0 {
        true => {
            let pulse = left_rule(f, cols[2], t);
            fill(f, pulse, t.bg_surface);
            draw_pulse(f, pulse, store, t, fx, now);
        }
        // The rail gave its column to a pane that could not work without it.
        // Its rects are published empty rather than left standing: an effect
        // over a zero rect is a no-op (`fx::ShellRects`), and one aimed at
        // where the rail used to be would sweep across the child's screen.
        false => publish_rail(&fx.rects, Rect::ZERO, Rect::ZERO, None),
    }

    draw_suggestions(f, rows[2], store);
    draw_status(f, rows[3], store, t, stale, strip == 0);

    // Last, and over the whole frame rather than the content rect: a question
    // about an order is not part of the view that asked it, and a box confined
    // to one pane would leave a live desk repainting three times a second
    // inside the border a human is reading before they commit.
    #[cfg(feature = "operator")]
    if let Some(host) = views.confirm(store.nav.view) {
        host.draw(f, area);
    }
    // Over everything, including the confirm box: it is what an operator opens
    // when they have lost the keyboard, and a key list half-covered by the
    // thing they are stuck in would answer the wrong question. It cannot be up
    // at the same time as a modal in practice — the modal claims every key
    // before `?` is read — but the ordering is what makes that true on screen
    // rather than only in the router.
    if store.nav.focus == Focus::Help {
        help::draw(f, area, store.posture, store.help_top);
    }
    // And over that. Last because the door is the first thing a workstation
    // opens on and claims every key but Ctrl-C while it does — so nothing can
    // arrive to be drawn over it. Defensive, like its place in the router: the
    // overlays below cannot be open at the same time, and this is what keeps
    // that true on screen rather than only in the argument for it.
    if let Some(door) = store.door() {
        door.draw(f, area, store);
    }

    fx.rects.frame.set(area);
    fx.rects.content.set(content);
    fx.rects.chips.set(chips(rows[3]));
}

/// The suggestion strip's height on this frame.
///
/// A row the command line borrows from the content while it has focus, and
/// gives back when it does not: a permanent strip would cost every view a row
/// for a hint nobody is reading. Below [`CMD_MIN_H`] there is no row to borrow
/// — see `draw_suggestions` for where the line says what it would have said
/// there.
///
/// One spelling, because [`pane_column`] has to answer about a frame that has
/// not been drawn yet: a row assumed rather than read is a `/cli` admitted into
/// a column the pane then refuses by one row.
fn strip_rows(store: &Store, height: u16) -> u16 {
    u16::from(store.nav.focus == Focus::Command && height >= CMD_MIN_H)
}

/// How wide the desk rail is, on a frame this wide, with or without a pane in
/// the column beside it.
///
/// [`PULSE_W`], except while a child is running in ATLAS's column and what
/// would be left for it is under [`PANE_MIN_W`]. WOULD DO has already gone by
/// then — the pane spans both of ATLAS's own columns, and that panel is a
/// proposal ranking DESK and BOOK also carry — so the rail is the last thing to
/// go, and it goes only when what it is standing beside would be unusable.
///
/// A shell decision rather than the view's, because a view is handed its column
/// and cannot widen it. Keyed on the *child* like everything else in this
/// stream: a desk with no session laid out differently would be a client whose
/// panels moved for a reason nothing on screen explains.
fn rail_width(width: u16, for_a_pane: bool) -> u16 {
    match for_a_pane && width.saturating_sub(NAV_W + PULSE_W + 1) < PANE_MIN_W {
        true => 0,
        false => PULSE_W,
    }
}

/// Whether this frame is drawing a child in ATLAS's column.
///
/// Two bodies rather than a `cfg` at the call site: the monitoring build has no
/// pane to lay a frame out around, so its layout has one shape.
#[cfg(feature = "operator")]
fn pane_showing(store: &Store) -> bool {
    store.nav.view == ViewId::Atlas && store.pty_state() != crate::store::PtyState::Absent
}

#[cfg(not(feature = "operator"))]
fn pane_showing(_store: &Store) -> bool {
    false
}

/// The column a terminal pane would be drawn in, on a frame this size.
///
/// Asked by `/cli`, one frame before there is a pane to measure: a child's
/// screen is sized at the moment it starts, and this is also where a window
/// with no room for a terminal is found out — which has to be right, because
/// the alternative is a child nobody can see running behind a refusal.
///
/// The same `Layout` calls `draw` makes, with the same rail and the same
/// borrowed strip, so what this measures and what that frame carves cannot
/// disagree. Both halves matter: the width decides the refusal, and the height
/// decides it too — the pane has a floor in rows as well as columns, and a row
/// assumed rather than read is a `/cli` admitted into a column the next frame
/// refuses.
#[cfg(feature = "operator")]
pub fn pane_column(frame: Rect, store: &Store) -> Rect {
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(0),
        Constraint::Length(strip_rows(store, frame.height)),
        Constraint::Length(1),
    ])
    .split(frame);
    let cols = Layout::horizontal([
        Constraint::Length(NAV_W),
        Constraint::Min(0),
        Constraint::Length(rail_width(rows[1].width, true)),
    ])
    .split(rows[1]);
    inside_rule(cols[1])
}

/// The frame height the suggestion strip needs: the tape, two rows of content
/// worth looking at, the strip itself, and the status line.
///
/// Below it the strip is dropped rather than taken out of a content area that
/// has nothing left to give. Nothing is lost silently: a refusal falls back
/// onto the command row itself (`draw_status`), because a sentence the line
/// said is a statement, and only the suggestions — a hint — may go.
const CMD_MIN_H: u16 = 5;

/// The columns of the status line the chips actually occupy.
fn chips(status: Rect) -> Rect {
    let width = CHIPS_W.min(status.width);
    Rect {
        x: status.x + status.width - width,
        width,
        ..status
    }
}

/// How long the numbers on screen have been unrefreshed, once that is long
/// enough to matter. `None` while they are current, or while there are none.
///
/// The threshold is carried by the store rather than stated here: it is a fact
/// about the poll cadence, and a renderer that owned its own copy of it would
/// keep that copy after the cadence moved.
fn stale_for(store: &Store, now: Instant) -> Option<Duration> {
    let age = now.saturating_duration_since(store.last_snapshot_at?);
    (age > store.stale_after).then_some(age)
}

/// Global keys, then the active view's.
///
/// The shell claims first so a view can never take a binding the whole
/// workstation depends on. Returns what the runtime should do; nav is store
/// state, so a view switch is applied here rather than travelling as a command.
///
/// The arrow keys are deliberately *not* claimed here: they mean "move the
/// cursor in whatever I am looking at", which only the active view can answer.
// Every key claimed here owes a row in `input::KEYMAP`, and a test reads
// this function to check it. That module's header lists what the check
// cannot see — including why a comment in here may not spell a key variant.
pub fn on_key(key: KeyEvent, store: &mut Store, views: &mut Views) -> Option<Command> {
    // **The one surface that outranks Ctrl-C, and the exchange that buys it.**
    // While the pane holds the keyboard every key is the child's. A terminal
    // whose child cannot be interrupted is not a terminal, and a desk that kept
    // Ctrl-C here would leave a runaway session stoppable only by killing the
    // workstation it is running inside. What the desk keeps instead is one key:
    // Ctrl-] returns the keyboard, and then Ctrl-C quits exactly as it always
    // has. Two keystrokes to leave, and the pane's own border names the first
    // of them on every frame — which is the half that makes this an exchange
    // rather than a binding quietly taken away.
    //
    // Routed on the focus and nothing else. A child that has ended cannot hold
    // the keyboard — the flag is a field of the *live* child, so an ending
    // takes it back along with the session — and that is what stops a dead pane
    // from answering every keystroke with the same sentence about a write that
    // went nowhere.
    #[cfg(feature = "operator")]
    if store.pty_focused() {
        pty_key(key, store);
        return None;
    }
    // Raw mode disables ISIG, so Ctrl-C arrives as a keystroke or not at all —
    // and the reflex every operator has has to work. Above every surface that
    // takes the keyboard *on the desk*, because this is the one key that must
    // reach the runtime whatever is on screen: a field, a modal, or the door
    // the workstation opens on. It sat under the confirmation box, where the
    // challenge field read it as a typed `c` — the box's own arm still swallows
    // every other key it is handed, which is what it is for.
    if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
        return Some(Command::Quit);
    }
    // Then the door. Its position above the confirmation box is **defensive
    // rather than a precedence anyone can reach**: a door is up only before it
    // has been answered once, and while it is up it claims every key, so no
    // view can open a modal underneath it. Nothing tests the two together
    // because nothing can produce the two together — deleting this ordering
    // changes no reachable behaviour, and saying it outranks the box would
    // claim a rule the client never exercises. What *is* reachable, and is
    // pinned, is Ctrl-C above both.
    //
    // Taken out of the store because answering it reads the whole desk; it goes
    // back unless the keystroke finished it, and a door too big for the
    // terminal is retired here rather than left armed and invisible — the rule
    // WORKFORCE's picker and SETTINGS' login form are already held to.
    if let Some(mut door) = store.take_door() {
        if !door.fits() {
            store.settle_door();
            return None;
        }
        let acted = door.on_key(key, store, views);
        match door.standing() {
            true => store.keep_door(door),
            false => store.settle_door(),
        }
        return acted;
    }
    // Before everything else, including `q` and the digits. A modal is a
    // blocking question, and a global key that walked away from it would leave
    // a human having half-answered a question about an order — worse, `3` and
    // `q` are both characters the challenge field has to be able to accept.
    #[cfg(feature = "operator")]
    if let Some(host) = views.confirm_mut(store.nav.view) {
        if host.showing().is_some() {
            return host.on_key(key);
        }
    }
    // The two surfaces that take the keyboard outright, before anything a view
    // or the shell claims. Both are things an operator opened deliberately, and
    // both contain characters the shell binds — `q` is a letter in a goal and
    // `r` is one in `/ticker`.
    match store.nav.focus {
        Focus::Command => return command_key(key, store, views),
        Focus::Help => {
            if !help::on_key(key, &mut store.help_top, help::rows(store.posture)) {
                store.nav.focus = Focus::Content;
            }
            return None;
        }
        Focus::Content => {}
    }
    // A view with a text field open owns every printable key, including the ones
    // claimed below. The shell claims `q`, `r` and the digits so that no view
    // can take a binding the whole workstation depends on; a goal being typed
    // into WORKFORCE's picker is the one case where that has to yield, or the
    // word "requote" would refresh the desk, jump to BOOK, and quit.
    if views.typing(store.nav.view) {
        return views.on_key(store.nav.view, key, store);
    }
    match key.code {
        // The line opens carrying the slash that opened it, so the first frame
        // an operator sees is already the picker rather than an empty field
        // they have to guess the grammar of.
        KeyCode::Char('/') => {
            store.cmd.clear();
            store.cmd.insert('/');
            store.nav.focus = Focus::Command;
            return None;
        }
        KeyCode::Char('?') => {
            store.help_top = 0;
            store.nav.focus = Focus::Help;
            return None;
        }
        KeyCode::Char('q') | KeyCode::Esc => return Some(Command::Quit),
        // `r` stays the workstation's refresh on every pane, including this
        // one — the shell claims it so no view can take a binding the whole
        // client depends on, and PREDICTORS is the pane whose board *only*
        // moves because this key asks for it (`main::ingest`).
        //
        // So the pane is shown the key rather than given it: an armed
        // PREDICTORS opens its run picker here and the refresh below is still
        // what `r` sends. The view returns nothing — it has state to move and
        // no request to make — and the assert is what keeps that true, because
        // a `Command` returned here would be silently dropped.
        KeyCode::Char('r') => {
            if store.nav.view == ViewId::Predictors {
                let offered = views.on_key(store.nav.view, key, store);
                debug_assert!(
                    offered.is_none(),
                    "PREDICTORS' `r` produced a command the shell would drop"
                );
            }
            return Some(Command::Refresh);
        }
        KeyCode::Char(c) if c.is_ascii_digit() => {
            if let Some(view) = ViewId::from_digit(c) {
                store.nav.view = view;
                return None;
            }
        }
        KeyCode::Tab => {
            store.nav.view = store.nav.view.next();
            return None;
        }
        KeyCode::BackTab => {
            store.nav.view = store.nav.view.prev();
            return None;
        }
        // `z` (zen: rails collapse) and `f` (fullscreen the focused pane) are
        // claimed here so no view can bind them and then lose the binding when
        // the layout modes land. Part I of the plan specifies them; no task
        // owns them yet, so they are swallowed rather than routed to a command
        // with nothing to dispatch it.
        KeyCode::Char('z') | KeyCode::Char('f') => return None,
        _ => {}
    }
    views.on_key(store.nav.view, key, store)
}

/// One keystroke while the child in the ATLAS pane holds the keyboard.
///
/// **It returns nothing, and the unit is the claim.** No key on this surface
/// can produce a `Command`, because the whole of what "the pane holds the
/// keyboard" means is that the desk acts on none of it. The one key the desk
/// keeps moves the focus, which is store state and never a request.
///
/// **Two spellings of one key.** 0x1D is what a terminal sends for Ctrl-], and
/// crossterm reports the C0 range by its legacy names — this client negotiates
/// no keyboard enhancement, so what actually arrives is `5` with control held.
/// The bracket is the spelling a kitty-protocol terminal would send, both are
/// the same byte, and both are honoured; the border says Ctrl-] because that is
/// the key an operator presses.
///
/// Everything else is the child's, verbatim, including the digits and the
/// slash. The codec decides what each key is on the wire; a key it has no wire
/// form for is not sent rather than approximated, and its own doc says why that
/// is not a swallowed keystroke.
// Every key claimed here owes a row in `input::KEYMAP`, and a test reads
// this function to check it. That module's header lists what the check
// cannot see — including why a comment in here may not spell a key variant.
#[cfg(feature = "operator")]
fn pty_key(key: KeyEvent, store: &mut Store) {
    let held = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Char(c) if held && (c == ']' || c == '5') => store.pty_focus(false),
        _ => {
            if let Some(bytes) = crate::pty::encode(key) {
                store.pty_write(&bytes);
            }
        }
    }
}

/// One mouse event: the nav rail's rows select views, everything else goes to
/// the active view.
///
/// Deliberately behind every surface that owns the keyboard: a door, a modal,
/// the command line and the help overlay all take the whole terminal, and a
/// click that switched views under one would move a pane the operator cannot
/// see. Minimal on purpose — wheel and click, no drag, no hover.
pub fn on_mouse(m: MouseEvent, store: &mut Store, views: &mut Views) -> Option<Command> {
    if store.door().is_some() || store.nav.focus != Focus::Content {
        return None;
    }
    #[cfg(feature = "operator")]
    if let Some(host) = views.confirm_mut(store.nav.view) {
        if host.showing().is_some() {
            // The box answers exactly one click — the words it drew to arm
            // itself — and swallows everything else. A wheel that scrolled the
            // pane underneath would let a human answer about a frame they can
            // no longer see, which is the reason this branch existed at all.
            return host.on_mouse(m);
        }
    }
    // The nav rail's geometry, restated from `draw`'s own constants rather
    // than published: the tape is one row and the rail starts under it, and
    // both facts are pinned by the goldens that pin the layout itself.
    if matches!(m.kind, MouseEventKind::Down(MouseButton::Left)) && m.column < NAV_W {
        let row = m.row as usize;
        if (1..=ViewId::ALL.len()).contains(&row) {
            store.nav.view = ViewId::ALL[row - 1];
            return None;
        }
    }
    views.on_mouse(store.nav.view, m, store)
}

// -- the command line -------------------------------------------------------

/// One keystroke while the line has focus.
///
/// The field edits itself; everything that needs the desk happens here, because
/// the suggestions and the store are the shell's to read and the field is a
/// pure text model by design.
fn command_key(key: KeyEvent, store: &mut Store, views: &mut Views) -> Option<Command> {
    // Which scope the line was pointed at before this keystroke. Entering one
    // is a transition rather than a state: a fetch fired on the scope itself
    // would ask the owner once per character typed inside it.
    let before = scope_of(store.cmd.text());
    let acted = match store.cmd.edit(key) {
        Edit::Idle => None,
        Edit::Close => {
            store.cmd.clear();
            store.nav.focus = Focus::Content;
            None
        }
        Edit::Accept => {
            // The first suggestion the desk can actually serve. The model strip
            // shows backends it cannot reach — hiding them would read as a desk
            // that never had one — and a Tab that pasted one would hand the
            // operator a line already refused.
            if let Some(first) = suggestions(store).iter().find(|s| s.choosable()) {
                store.cmd.accept(&first.value);
            }
            None
        }
        Edit::Submit => submit(store, views),
    };
    acted.or_else(|| entered_model_scope(before, store).then_some(Command::Backends))
}

/// What the line could become from here, for this window.
fn suggestions(store: &Store) -> Vec<cmd::Suggestion> {
    cmd::suggestions(&cmd::parse(store.cmd.text()), store, store.posture)
}

/// Which scope the line is pointed at, if it has been accepted into one.
fn scope_of(text: &str) -> Option<Scope> {
    match cmd::parse(text) {
        CmdState::Scoped { scope, .. } => Some(scope),
        _ => None,
    }
}

/// Whether this keystroke is what put the line into the model scope.
///
/// The catalog behind that scope is the one payload with no cadence: the route
/// probes every configured daemon, so it is asked for on the way in and not on
/// a beat. Only for a window that can choose — an unarmed one is offered
/// nothing there, and a request for a list it will never be shown is a round
/// trip nobody asked for.
fn entered_model_scope(before: Option<Scope>, store: &Store) -> bool {
    store.posture.writes()
        && before != Some(Scope::Model)
        && scope_of(store.cmd.text()) == Some(Scope::Model)
}

/// Act on the line, or say why it cannot be acted on.
///
/// Every arm either does something visible or leaves a sentence behind. A key
/// with no effect anyone can see is the hung-client reading this workstation
/// refuses everywhere else, and a command line is where an operator would meet
/// it first.
/// A palette line arriving from somewhere other than the palette.
///
/// The ATLAS chat box hands its slash lines here, so the chat has every scope
/// the palette has and never a second grammar. The line is put where the
/// palette would have it and submitted the same way — a refusal stays on the
/// command row carrying its sentence, exactly as if it had been typed there.
pub fn run_line(text: &str, store: &mut Store, views: &mut Views) -> Option<Command> {
    store.cmd.clear();
    for c in text.chars() {
        store.cmd.insert(c);
    }
    submit(store, views)
}

fn submit(store: &mut Store, views: &mut Views) -> Option<Command> {
    let state = cmd::parse(store.cmd.text());
    // A picker with one answer accepts rather than acts. Enter on `/tick` means
    // "that one", and rewriting the buffer to `/ticker ` puts the operator in
    // front of the values instead of guessing which one they meant.
    if let CmdState::Picker { typed, .. } = &state {
        let choices = suggestions(store);
        // An exact word beats an ambiguous prefix. `model` starts with `mode`,
        // so once the second scope arrived `/mode` + Enter answered "choose a
        // scope" about a word the operator had typed in full — and the two are
        // read off the *offered* list, so a window that is not shown a scope
        // cannot be accepted into it either.
        let named = format!("/{typed}");
        let hit = choices
            .iter()
            .find(|choice| choice.value.eq_ignore_ascii_case(&named))
            .or(match choices.as_slice() {
                [only] => Some(only),
                _ => None,
            });
        if let Some(hit) = hit {
            // Accepting `/mod` into `/model ` is a scope entry like any other,
            // and `command_key` is the one place that notices — it holds the
            // scope from before the keystroke, so nothing here has to.
            let value = hit.value.clone();
            store.cmd.accept(&value);
            return None;
        }
    }
    match cmd::resolve(&state, store, store.posture) {
        Resolved::Refused(said) => {
            // The line stays up carrying what it said, so the operator can fix
            // the character that was wrong rather than retype the whole thing.
            store.cmd.say(said);
            None
        }
        Resolved::View(id) => {
            store.nav.view = id;
            done(store);
            None
        }
        Resolved::Ticker(symbol) => {
            let hit = views.select_ticker(&symbol, store);
            match landing(store.nav.view, hit) {
                Some(view) => {
                    store.nav.view = view;
                    done(store);
                }
                // Unreachable through `resolve`, which only yields symbols the
                // desk is watching — and stated rather than assumed, because a
                // cursor that moved nowhere with no sentence beside it is the
                // phantom selection this scope exists to avoid.
                None => store
                    .cmd
                    .say(format!("{symbol} is on no pane this client draws")),
            }
            None
        }
        #[cfg(feature = "operator")]
        Resolved::OpenApprove(id) => {
            let modal = approval_modal(store, &id);
            store.nav.view = ViewId::Atlas;
            if let Some(host) = views.confirm_mut(ViewId::Atlas) {
                host.open(modal, crate::ui::widgets::confirm::Pending::Approve(id));
            }
            done(store);
            None
        }
        #[cfg(feature = "operator")]
        Resolved::RequestApproval(plan) => {
            store.nav.view = ViewId::Atlas;
            done(store);
            Some(Command::RequestApproval(plan))
        }
        #[cfg(feature = "operator")]
        Resolved::OpenExecute(plan_id) => {
            let modal = store
                .plans()
                .iter()
                .find(|p| p.plan_id.as_deref() == Some(plan_id.as_str()))
                .and_then(|plan| {
                    store.covering_approval(&plan_id).and_then(|approval| {
                        crate::ui::widgets::confirm::Modal::for_plan(plan, approval)
                    })
                });
            match modal {
                Some(modal) => {
                    store.nav.view = ViewId::Atlas;
                    if let Some(host) = views.confirm_mut(ViewId::Atlas) {
                        host.open(modal, crate::ui::widgets::confirm::Pending::Execute);
                    }
                    done(store);
                }
                // Resolved only past a covering approval, so this is the
                // approval lacking the hash the box is bound to — a desk
                // fact, said rather than swallowed.
                None => store.cmd.say(format!(
                    "plan {}'s approval carries no targets_hash to confirm against",
                    &plan_id[..8.min(plan_id.len())]
                )),
            }
            None
        }
        Resolved::ClearChat => {
            // Local: the window's own pane empties; the bus and AUDIT keep
            // every row, exactly like Claude Code's /clear keeps the log.
            store.clear_chat();
            done(store);
            None
        }
        // The two lines whose effect is a child process rather than a request.
        // Handed up as a `Command` like everything else, and acted on nowhere
        // near here: this module renders and resolves, and the terminal belongs
        // to the runtime that owns the screen.
        #[cfg(feature = "operator")]
        Resolved::Cli => {
            // ATLAS comes up for `Ask`'s reason: the pane is drawn in this
            // column and nowhere else, so a `/cli` typed from BOOK would start
            // a child on a frame nobody is looking at — and the first news of
            // it would be the desk's own keys behaving oddly.
            store.nav.view = ViewId::Atlas;
            done(store);
            Some(Command::OpenCli)
        }
        #[cfg(feature = "operator")]
        Resolved::Build(request) => {
            done(store);
            Some(Command::OpenBuild(request))
        }
        Resolved::Plan(id) => {
            // The jump happens either way: an operator who named a plan is
            // asking to look at the ledger, and BOOK is where it is drawn even
            // when the band cannot reach that card.
            store.nav.view = ViewId::Book;
            match views.select_plan(&id, store) {
                PlanAt::Card(_) => done(store),
                // The band is the newest few; a plan below it is on the desk
                // and off the screen. Named, not fixed — see `book::PlanAt`.
                PlanAt::Beyond { at, cards } => store.cmd.say(format!(
                    "plan {id} is #{} on the ledger and BOOK draws the newest {cards}",
                    at + 1
                )),
                PlanAt::NotHeld => store.cmd.say(format!("the desk is not holding plan {id}")),
            }
            None
        }
        #[cfg(feature = "operator")]
        Resolved::Mode { data, book } => {
            done(store);
            Some(Command::DeskMode { data, book })
        }
        #[cfg(feature = "operator")]
        Resolved::Model { surface, choice } => {
            done(store);
            Some(Command::SetLlm { surface, choice })
        }
        // The line that fills the panel. ATLAS comes up because that is where
        // the answer is drawn — the owner persists it and the next poll brings
        // it back, so a window left on BOOK would report "5 proposed" in a
        // toast and show the operator nothing they could read or approve.
        //
        // Nothing is awaited here: the dispatch seam spawns, and the outcome
        // refetches the desk. A line that blocked on the ask would freeze the
        // frame loop for as long as the owner took to compose it.
        #[cfg(feature = "operator")]
        Resolved::Ask => {
            store.nav.view = ViewId::Atlas;
            done(store);
            Some(Command::Actionables)
        }
        // The one line that starts work, so it may only start work the
        // operator is looking at. The would-do panel is capped at
        // `min(12, sidebar/2)` rows with no scrollback and no cursor — three
        // verbose proposals fill it at 120×36 — and a name typed for an item
        // it could not draw is an approval given blind, which is the same
        // failure as a confirm box nobody read.
        //
        // So the first `/do` on an unshown item is a *refusal that asks for
        // it*: ATLAS comes up, the panel is asked to draw that proposal first,
        // and the same line approves it next time. Refusing without asking
        // would leave a proposal beyond the cap unapprovable until the
        // terminal grew.
        //
        // The sentence says what this did, not what the next frame will show:
        // a proposal too tall for the whole budget is asked for and still not
        // drawn, and the panel's own note is what says so — a line here
        // promising it is "at the top" would be a claim about a frame nobody
        // has painted.
        #[cfg(feature = "operator")]
        Resolved::Approve { template, task } => {
            // Read before the ask: `drew` is what the *last* frame put on
            // screen, and pinning first would answer about a frame nobody has
            // seen yet. Both halves, because the desk moves under the frame —
            // a proposal re-minted between the paint and the keystroke keeps
            // its word and changes the task the word resolves to.
            let drawn = views.drew_proposal(&template, &task);
            views.ask_about_proposal(&template);
            store.nav.view = ViewId::Atlas;
            match drawn {
                true => {
                    done(store);
                    Some(Command::ApproveAction(task))
                }
                false => {
                    store.cmd.say(format!(
                        "{template} is not on the WOULD DO panel as the desk is serving it \
                         now — ATLAS is asking for it; read it there, then /do it again"
                    ));
                    None
                }
            }
        }
    }
}

/// The approve box the chat's `/approve` puts up — built by the same function
/// AUDIT's own `a` calls, so the two paths cannot come to say different things
/// about one request. An id the snapshot is no longer serving still gets a box
/// naming it: the owner decides what is decidable, and a client that silently
/// dropped the word would look like the key had missed.
#[cfg(feature = "operator")]
fn approval_modal(store: &Store, approval_id: &str) -> crate::ui::widgets::confirm::Modal {
    let approval = store
        .approvals()
        .iter()
        .find(|a| a.approval_id.as_deref() == Some(approval_id));
    crate::ui::widgets::confirm::Modal::for_approval(approval_id, approval, "approve")
}

/// Which view to show a symbol on: the one the operator is already looking at
/// when it holds the row, otherwise the one that does.
///
/// Staying put matters more than it sounds. An operator working through the
/// blotter who types a symbol is naming a position, and being thrown to MARKETS
/// for it would lose the column they had sorted by.
fn landing(current: ViewId, hit: Selected) -> Option<ViewId> {
    match (current, hit) {
        (ViewId::Markets, Selected { markets: true, .. }) => Some(ViewId::Markets),
        (ViewId::Book, Selected { blotter: true, .. }) => Some(ViewId::Book),
        (_, Selected { markets: true, .. }) => Some(ViewId::Markets),
        (_, Selected { blotter: true, .. }) => Some(ViewId::Book),
        _ => None,
    }
}

/// The line did what it was asked: record it and give the keyboard back.
fn done(store: &mut Store) {
    store.cmd.submitted();
    store.nav.focus = Focus::Content;
}

/// The depth ramp: a region reads as a surface stacked over the frame only if
/// something paints it. Widgets draw text, not backgrounds, so the shell does.
fn fill(f: &mut Frame, area: Rect, bg: Color) {
    f.render_widget(Block::default().style(Style::default().bg(bg)), area);
}

/// Draw the dim rule in the first column and return what is left.
fn left_rule(f: &mut Frame, area: Rect, t: &Theme) -> Rect {
    let rule = Block::default()
        .borders(Borders::LEFT)
        .border_style(Style::default().fg(t.border_dim));
    let inner = inside_rule(area);
    f.render_widget(rule, area);
    inner
}

/// What a rule leaves for the pane beside it, without drawing one.
///
/// The same block, so a layout asked about before the frame that draws it —
/// see [`pane_column`] — cannot answer one cell wider than the frame will.
fn inside_rule(area: Rect) -> Rect {
    Block::default().borders(Borders::LEFT).inner(area)
}

fn draw_nav(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    fill(f, area, t.bg_surface);
    let lines: Vec<Line> = ViewId::ALL
        .iter()
        .map(|id| {
            let active = *id == store.nav.view;
            // The marker is a glyph, not only a colour: on a 256-colour
            // terminal the highlight is a shade, and a shade is not an answer
            // to "which view am I looking at".
            let marker = if active { "▌" } else { " " };
            let style = if active {
                Style::default()
                    .fg(t.accent)
                    .bg(t.bg_hover)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(t.text_secondary)
            };
            Line::from(vec![
                Span::styled(
                    marker,
                    Style::default().fg(t.accent).bg(if active {
                        t.bg_hover
                    } else {
                        t.bg_surface
                    }),
                ),
                // `id.digit()`, not `i + 1`: the tenth pane's key is `0`, and
                // a rail that counted would print a two-character number into
                // a one-character column and offer a key nothing accepts.
                Span::styled(format!("{} {:<5}", id.digit(), id.label()), style),
            ])
        })
        .collect();
    f.render_widget(Paragraph::new(lines), area);
}

/// What this client can see, when it cannot see a desk.
///
/// A frame of `--` reads as "nothing is happening on your desk" when the truth
/// is "I cannot see it". Ported from `ui.rs::draw_unreachable`, split in two
/// because "not there" and "not yet" have different remedies.
fn draw_no_data(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    // A broken payload outranks both: the owner answered, so "no owner" is
    // wrong and "waiting" is worse — it says the desk is coming when it is not.
    let lines = if let Some(bad) = &store.malformed {
        vec![
            Line::from(Span::styled(
                "OWNER PAYLOAD MALFORMED",
                Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
            )),
            Line::from(""),
            Line::from(Span::styled(
                bad.error.clone(),
                Style::default().fg(t.text_primary),
            )),
            Line::from(""),
            Line::from(Span::styled(
                format!("from {}", bad.url),
                Style::default().fg(t.text_secondary),
            )),
            // One logical line, wrapped by the Paragraph: hand-split lines
            // re-wrap into orphans at rail widths this panel does not choose.
            Line::from(Span::styled(
                "The owner is answering and this client cannot read what it serves, \
                 so nothing below is current. Check the owner's version.",
                Style::default().fg(t.text_secondary),
            )),
        ]
    } else if store.conn.owner {
        vec![
            Line::from(Span::styled(
                "WAITING FOR THE FIRST SNAPSHOT",
                Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
            )),
            Line::from(""),
            Line::from(Span::styled(
                "The owner answered. Its first /api/tui payload has not arrived yet.",
                Style::default().fg(t.text_secondary),
            )),
        ]
    } else {
        vec![
            Line::from(Span::styled(
                "NO OWNER RUNTIME",
                Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
            )),
            Line::from(""),
            Line::from(Span::styled(
                "This client never opens the registry itself — the owner is the only",
                Style::default().fg(t.text_secondary),
            )),
            Line::from(Span::styled(
                "writer. Start one with `qlab tui` or `qlab ui`, then press r.",
                Style::default().fg(t.text_secondary),
            )),
        ]
    };
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), area);
}

/// The always-on rail: what the market is doing, and what Atlas makes of it.
///
/// The sections take the room that is left after the read panel and the glyph,
/// which are anchored to the foot of the rail: the rail reads top-down as market
/// → read → manager, and the manager is what an operator glances at last. Task
/// 14's read pane replaces the middle block; the pulse sections above it decide
/// for themselves how much of their own allocation they can honestly fill.
fn draw_pulse(f: &mut Frame, area: Rect, store: &Store, t: &Theme, fx: &Fx, now: Instant) {
    let rows = Layout::vertical([
        Constraint::Min(0),          // the pulse sections
        Constraint::Length(READ_H),  // ATLAS READ
        Constraint::Length(GLYPH_H), // the glyph
    ])
    .split(area);

    let regime = pulse::draw(f, rows[0], store, &fx.gauge, now);
    publish_rail(&fx.rects, rows[0], rows[1], regime);

    let snapshot = store.snapshot.as_ref();
    let read = snapshot.and_then(|s| s.atlas_read.as_ref());
    let read_lines = vec![
        panel_header("atlas read"),
        kv(
            "state",
            format::upper(read.and_then(|r| format::text(r.quantitative_state.as_ref()))),
        ),
        kv(
            "agreement",
            format::upper(read.and_then(|r| format::text(r.agreement.as_ref()))),
        ),
        kv(
            "conviction",
            format::opt_pct(read.and_then(|r| r.conviction)),
        ),
        kv(
            "news",
            format::or_missing(read.and_then(|r| r.news_source.as_ref())).to_string(),
        ),
    ];
    panel(f, rows[1], read_lines);

    draw_glyph(f, rows[2], store, t);
}

/// Hand the rail's two effect targets to `Fx`.
///
/// A rail too short for the regime strip drops it; the sweep then crosses the
/// section block that still carries the `regime`/`robust` line the diff actually
/// watches, rather than crossing nothing and reading as a dropped frame.
fn publish_rail(rects: &ShellRects, sections: Rect, read: Rect, regime: Option<Rect>) {
    rects.regime.set(regime.unwrap_or(sections));
    rects.read.set(read);
}

/// The Atlas glyph, on its mood's own tempo.
///
/// Ported from `ui.rs::draw_atlas`. A busy desk visibly moves faster than a
/// quiet one without anything else on screen changing, and a frozen automaton
/// is the one honest way a hung client can announce itself.
fn draw_glyph(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    // Bottom-anchored: the rail reads top-down as market → read → manager, and
    // the manager is the thing an operator glances at last.
    let area = Layout::vertical([Constraint::Min(0), Constraint::Length(glyph::H as u16 + 1)])
        .split(area)[1];
    let mood = store.mood();
    let tone = match mood {
        glyph::Mood::Working => t.positive,
        glyph::Mood::Alarmed => t.negative,
        glyph::Mood::Dormant => t.text_secondary,
        glyph::Mood::Idle => t.accent,
    };
    let mut lines: Vec<Line> = glyph::frame(mood, store.tick * mood.tempo() / 10)
        .into_iter()
        .map(|row| Line::from(Span::styled(format!(" {row}"), Style::default().fg(tone))))
        .collect();
    lines.push(Line::from(Span::styled(
        format!(" {}", mood.label()),
        Style::default().fg(tone).add_modifier(Modifier::BOLD),
    )));
    f.render_widget(Paragraph::new(lines), area);
}

/// The command row's own spans: the prompt, or the line being typed into it.
///
/// The caret is drawn *under* the character it is on rather than inserted
/// beside it, so moving it does not shift the text an operator is reading.
///
/// `orphaned` says the strip below has no row on this frame, which is the one
/// case where a refusal is carried here instead. The suggestions are a hint and
/// may be lost; a sentence the line said back is a statement and may not.
fn command_row(store: &Store, t: &Theme, orphaned: bool) -> Vec<Span<'static>> {
    if store.nav.focus != Focus::Command {
        return vec![Span::styled(
            " /command …",
            Style::default().fg(t.text_tertiary),
        )];
    }
    let text = store.cmd.text();
    let at = store.cmd.cursor();
    let before: String = text.chars().take(at).collect();
    let under: String = text.chars().skip(at).take(1).collect();
    let after: String = text.chars().skip(at + 1).collect();
    let typed = Style::default()
        .fg(t.text_primary)
        .add_modifier(Modifier::BOLD);
    let mut spans = vec![
        Span::styled(" ", Style::default()),
        Span::styled(before, typed),
        // At the end of the line there is no character to sit under, so the
        // caret is a bar of its own — a field with no caret is one an operator
        // cannot tell from a label.
        if under.is_empty() {
            Span::styled("▏", Style::default().fg(t.accent))
        } else {
            Span::styled(under, Style::default().fg(t.bg_base).bg(t.accent))
        },
        Span::styled(after, typed),
    ];
    if orphaned {
        if let Some(note) = store.cmd.note() {
            spans.push(Span::styled(
                format!("  {note}"),
                Style::default().fg(t.warning),
            ));
        }
    }
    spans
}

/// The one-line strip above the input: what the line said back, or what it
/// could become.
///
/// The note outranks the suggestions. An operator who has just been refused
/// needs the reason, not a list of what else they might have typed.
fn draw_suggestions(f: &mut Frame, area: Rect, store: &Store) {
    if area.height == 0 || store.nav.focus != Focus::Command {
        return;
    }
    let t = theme();
    fill(f, area, t.bg_raised);
    if let Some(note) = store.cmd.note() {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!(" {note}"),
                Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
            ))),
            area,
        );
        return;
    }

    let state = cmd::parse(store.cmd.text());
    let mut spans = Vec::new();
    // What the scope takes, while nothing has been typed into it. The words are
    // the scope's own (`Scope::hint`), so the strip cannot describe an argument
    // the parser would not accept.
    if let CmdState::Scoped { scope, query } = &state {
        if query.trim().is_empty() {
            spans.push(Span::styled(
                format!(" {} ·", scope.hint()),
                Style::default().fg(t.text_dim),
            ));
        }
    }
    let choices = cmd::suggestions(&state, store, store.posture);
    if choices.is_empty() && spans.is_empty() {
        spans.push(Span::styled(
            " nothing here answers that",
            Style::default().fg(t.text_dim),
        ));
    }
    // The one Tab and a lone Enter accept — which is the first *choosable* one,
    // not the first one drawn. The model strip shows backends the desk cannot
    // reach, and a highlight on one of those would point the accept key at a
    // line that is already refused.
    let accepts = choices.iter().position(cmd::Suggestion::choosable);
    for (i, choice) in choices.iter().enumerate() {
        match &choice.refusal {
            // Dim, and carrying the owner's own sentence: an entry the desk
            // cannot serve is shown rather than hidden, and the reason is the
            // half an operator can act on.
            Some(said) => spans.push(Span::styled(
                format!(" {} — {said}", choice.value),
                Style::default().fg(t.text_dim),
            )),
            None => spans.push(Span::styled(
                format!(" {}", choice.value),
                if Some(i) == accepts {
                    Style::default().fg(t.accent).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(t.text_secondary)
                },
            )),
        }
    }
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn draw_status(
    f: &mut Frame,
    area: Rect,
    store: &Store,
    t: &Theme,
    stale: Option<Duration>,
    orphaned: bool,
) {
    fill(f, area, t.bg_raised);
    let snapshot = store.snapshot.as_ref();
    let atlas = snapshot.and_then(|s| s.atlas.as_ref());
    let autonomous = snapshot
        .and_then(|s| s.atlas_heartbeat.as_ref())
        .and_then(|b| b.autonomous)
        .unwrap_or(false);
    let posture = format!(
        "{}·{}",
        atlas
            .and_then(|a| format::text(a.mode.as_ref()))
            .unwrap_or(MISSING),
        if autonomous { "auto" } else { "manual" }
    );

    let left = command_row(store, t, orphaned);
    // A green dot says the subscription is open, which is a different claim from
    // "every event it delivered was readable" — so a stream that is dropping
    // frames is degraded rather than up, and the count says how badly.
    let mut right = chip(
        "SSE",
        match (store.conn.stream, store.stream_malformed_count) {
            (false, _) => Health::Down,
            (true, 0) => Health::Up,
            (true, _) => Health::Degraded,
        },
        store.conn.stream_drops,
        t,
    );
    // Beside the chip rather than instead of it, for the same reason STALE sits
    // beside OWNER: two claims, and the dot only ever spoke to the first. Named
    // `SSE` because the chip it sits beside is — one feed with two names on one
    // line is a second feed as far as a reader is concerned.
    if store.stream_malformed_count > 0 {
        right.push(Span::styled(
            format!("SSE ⚠ {}  ", store.stream_malformed_count),
            Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
        ));
    }
    // An owner serving a payload this client cannot read is answering, so "down"
    // is wrong and green is worse — it is the affirmative falsehood the
    // malformed panel exists to correct, and the dot has to agree with it.
    right.extend(chip(
        "OWNER",
        match (store.conn.owner, store.malformed.is_some()) {
            (false, _) => Health::Down,
            (true, true) => Health::Degraded,
            (true, false) => Health::Up,
        },
        store.conn.owner_drops,
        t,
    ));
    // Beside the chip, not instead of it: a reachable owner and current numbers
    // are two claims, and the red dot only ever spoke to the first.
    if let Some(age) = stale {
        right.push(Span::styled(
            format!("STALE {}  ", format::age(age)),
            Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
        ));
    }
    if store.malformed.is_some() {
        right.push(Span::styled(
            "MALFORMED  ",
            Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
        ));
    }
    // What the desk is pointed at, before what Atlas is doing with it: the two
    // read left to right as "this book, that manager". Without it `/mode` could
    // switch the desk and nothing on screen said what it had switched to.
    if let Some(mode) = desk_mode_chip(store, t) {
        right.push(mode);
    }
    right.extend([
        Span::styled(
            format!("{posture}  "),
            Style::default().fg(t.text_secondary),
        ),
        // The posture, on every frame. An operator must never have to wonder
        // whether this surface can place an order. In the default build the
        // word can only be GLASS — `Posture::Operator` is not in the type —
        // so this is a statement about the artifact, not a rendering choice.
        Span::styled(
            format!("{} ", store.posture.label()),
            Style::default()
                .fg(match store.posture {
                    Posture::Glass => t.accent,
                    // Amber-on-warning rather than the accent: OPERATOR is the
                    // one chip that means "the next keystroke can move money",
                    // and it must not read as another piece of chrome in the
                    // desk's own colour.
                    #[cfg(feature = "operator")]
                    Posture::Operator => t.warning,
                })
                .add_modifier(Modifier::BOLD),
        ),
    ]);

    let mut spans = left;
    // The one thing on this line that gives way. Every chip beside it is a claim
    // about the desk; this is only *where the desk is*, and a `Paragraph` that
    // ran past the frame would clip the right-hand end — which is the posture
    // badge, the one statement that may never leave the frame.
    let used: usize = spans
        .iter()
        .chain(right.iter())
        .map(|s| s.content.width())
        .sum();
    let mut slack = (area.width as usize).saturating_sub(used);
    if let Some(base) = format::text(Some(&store.base)) {
        // Two cells of separation either side, or it reads as part of the
        // command prompt it sits after.
        if slack >= base.width() + 4 {
            spans.push(Span::styled(
                format!("   {base}"),
                Style::default().fg(t.text_tertiary),
            ));
            slack -= base.width() + 3;
        }
    }
    spans.push(Span::raw(" ".repeat(slack)));
    spans.append(&mut right);
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

/// Which desk this is, in the owner's own words — or nothing.
///
/// The label is the owner's `desk_mode.label`, never composed here from the two
/// words `/mode` sent: the owner is the authority on what a pair is called, and
/// a client that spelled it itself would report a switch the owner may have
/// refused. `Some("")` is absent, as everywhere in this client — an empty chip
/// would read as a desk pointed at nothing.
///
/// Amber when the book that can place real orders has no working login behind
/// it. That is the "succeeded and did nothing" shape: the desk is pointed at a
/// venue it cannot reach, and the toast that said so at the moment of the switch
/// is gone by the next glance. Absence of the flag is warned about rather than
/// assumed fine — the owner always sends it, so silence is a contract this
/// client cannot read, and about a real book unreadable may not render as clean.
fn desk_mode_chip(store: &Store, t: &Theme) -> Option<Span<'static>> {
    let mode = store.desk_mode()?;
    let label = format::text(mode.label.as_ref())?;
    // Bold with the warning, like STALE and MALFORMED beside it: the tone alone
    // is a shade on a 256-colour terminal, and a shade is not an answer to
    // "can this desk reach the book it is pointed at".
    let style = match mode.book_unreachable() {
        true => Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
        false => Style::default().fg(t.text_secondary),
    };
    Some(Span::styled(format!("{label}  "), style))
}

/// What one feed's chip can say.
///
/// Three states rather than two. A feed that is answering and handing this
/// client something it cannot use is neither up nor down, and painting it green
/// is exactly the affirmative falsehood the malformed panel was added to
/// correct — the dot has to be able to agree with the panel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Health {
    Up,
    Degraded,
    Down,
}

/// One connection chip: the dot, the feed's name, and how often it has gone away.
///
/// The drop count only appears once there is one. A chip carrying `↻0` on every
/// frame would train an operator to read past the whole run.
fn chip(label: &str, health: Health, drops: u32, t: &Theme) -> Vec<Span<'static>> {
    let tone = match health {
        Health::Up => t.positive,
        Health::Degraded => t.warning,
        Health::Down => t.negative,
    };
    let mut out = vec![
        Span::styled("● ", Style::default().fg(tone)),
        Span::styled(label.to_string(), Style::default().fg(t.text_secondary)),
    ];
    if drops > 0 {
        out.push(Span::styled(
            format!(" ↻{drops}"),
            Style::default().fg(t.text_tertiary),
        ));
    }
    out.push(Span::raw("  "));
    out
}

/// A titled panel: header line, body, and the rule the block reserves.
fn panel(f: &mut Frame, area: Rect, lines: Vec<Line<'static>>) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    f.render_widget(Paragraph::new(lines), inner);
}

/// A label/value row, aligned so a column of them reads as a column.
fn kv(label: &str, value: String) -> Line<'static> {
    let t = theme();
    Line::from(vec![
        Span::styled(
            format!(" {label:<LABEL_W$}"),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(value, Style::default().fg(t.text_primary)),
    ])
}
