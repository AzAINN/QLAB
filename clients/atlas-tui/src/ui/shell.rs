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

use crate::cmd::{self, CmdState, Command, Edit, Resolved};
use crate::format::{self, MISSING};
use crate::fx::{Fx, ShellRects};
use crate::glyph;
use crate::store::{Focus, Posture, Store, ViewId};
use crate::theme::theme;
use crate::theme::Theme;
use crate::ui::views::{book::PlanAt, Selected, Views};
use crate::ui::widgets::{help, panel_block, panel_header, pulse, ticker};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Wrap},
    Frame,
};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

/// Exactly wide enough for `▌6 AUDIT` — the active marker, the digit, a space,
/// and the longest label. Widening it would take cells from the content.
///
/// Public because the golden harness crops a frame down to the columns a view
/// owns, and a second spelling of the layout in the tests would drift from this
/// one the first time a rail is resized.
pub const NAV_W: u16 = 8;
/// The pulse rail. Wide enough for a label column and a value column at the
/// widths `format` produces; narrower and the money figures start truncating.
pub const PULSE_W: u16 = 34;
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

    // The suggestion strip is a row the command line borrows from the content
    // while it has focus, and gives back when it does not: a permanent strip
    // would cost every view a row for a hint nobody is reading. Below
    // `CMD_MIN_H` there is no row to borrow — see `draw_suggestions` for where
    // the line says what it would have said there.
    let strip = u16::from(store.nav.focus == Focus::Command && area.height >= CMD_MIN_H);
    let rows = Layout::vertical([
        Constraint::Length(1),     // ticker
        Constraint::Min(0),        // rails + content
        Constraint::Length(strip), // the suggestions, while the line is focused
        Constraint::Length(1),     // command line / status
    ])
    .split(area);
    let cols = Layout::horizontal([
        Constraint::Length(NAV_W),
        Constraint::Min(0),
        Constraint::Length(PULSE_W),
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

    let pulse = left_rule(f, cols[2], t);
    fill(f, pulse, t.bg_surface);
    draw_pulse(f, pulse, store, t, fx, now);

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

    fx.rects.frame.set(area);
    fx.rects.content.set(content);
    fx.rects.chips.set(chips(rows[3]));
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
    // Before everything, including `q` and the digits. A modal is a blocking
    // question, and a global key that walked away from it would leave a human
    // having half-answered a question about an order — worse, `3` and `q` are
    // both characters the challenge field has to be able to accept.
    #[cfg(feature = "operator")]
    if let Some(host) = views.confirm_mut(store.nav.view) {
        if host.showing().is_some() {
            return host.on_key(key);
        }
    }
    // Raw mode disables ISIG, so Ctrl-C arrives as a keystroke or not at all —
    // and the reflex every operator has has to work. Above the typing check
    // rather than inside the match below it, because this is the one key that
    // must reach the runtime even while a field owns the keyboard: a text field
    // that swallowed it would leave the operator's only exit reflex dead in a
    // fullscreen client.
    if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
        return Some(Command::Quit);
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
        KeyCode::Char('r') => return Some(Command::Refresh),
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

// -- the command line -------------------------------------------------------

/// One keystroke while the line has focus.
///
/// The field edits itself; everything that needs the desk happens here, because
/// the suggestions and the store are the shell's to read and the field is a
/// pure text model by design.
fn command_key(key: KeyEvent, store: &mut Store, views: &mut Views) -> Option<Command> {
    match store.cmd.edit(key) {
        Edit::Idle => None,
        Edit::Close => {
            store.cmd.clear();
            store.nav.focus = Focus::Content;
            None
        }
        Edit::Accept => {
            if let Some(first) = suggestions(store).first() {
                store.cmd.accept(first);
            }
            None
        }
        Edit::Submit => submit(store, views),
    }
}

/// What the line could become from here, for this window.
fn suggestions(store: &Store) -> Vec<String> {
    cmd::suggestions(&cmd::parse(store.cmd.text()), store, store.posture)
}

/// Act on the line, or say why it cannot be acted on.
///
/// Every arm either does something visible or leaves a sentence behind. A key
/// with no effect anyone can see is the hung-client reading this workstation
/// refuses everywhere else, and a command line is where an operator would meet
/// it first.
fn submit(store: &mut Store, views: &mut Views) -> Option<Command> {
    let state = cmd::parse(store.cmd.text());
    // A picker with one answer accepts rather than acts. Enter on `/tick` means
    // "that one", and rewriting the buffer to `/ticker ` puts the operator in
    // front of the values instead of guessing which one they meant.
    if let CmdState::Picker { .. } = state {
        let choices = suggestions(store);
        if let [only] = choices.as_slice() {
            store.cmd.accept(only);
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
    }
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
    let inner = rule.inner(area);
    f.render_widget(rule, area);
    inner
}

fn draw_nav(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    fill(f, area, t.bg_surface);
    let lines: Vec<Line> = ViewId::ALL
        .iter()
        .enumerate()
        .map(|(i, id)| {
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
                Span::styled(format!("{} {:<5}", i + 1, id.label()), style),
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
    for (i, choice) in choices.iter().enumerate() {
        spans.push(Span::styled(
            format!(" {choice}"),
            if i == 0 {
                // The one Tab and a lone Enter accept, so it cannot look like
                // the rest of the list.
                Style::default().fg(t.accent).add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(t.text_secondary)
            },
        ));
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
    if mode.book.as_deref() == Some("alpaca") && mode.credentials_ok != Some(true) {
        return Some(Span::styled(
            format!("{label}  "),
            Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
        ));
    }
    Some(Span::styled(
        format!("{label}  "),
        Style::default().fg(t.text_secondary),
    ))
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
