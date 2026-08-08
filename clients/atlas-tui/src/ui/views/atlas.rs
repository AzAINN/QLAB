//! ATLAS — the conversation with the desk manager, beside the evidence it reasons from.
//!
//! Chat first, deliberately. Every other pane renders what the desk *is*; this
//! one is where an operator asks what it *means*, and the answer arrives on the
//! same bus the question went out on (`atlas_message` rows, served whole in the
//! snapshot's `atlas_chat` key). The sidebar is today's proposals over the
//! predictor board — what the desk would do, and the same board summary the
//! reasoner itself is handed — so the words on the left can be read against the
//! evidence they were reasoned from on the right.
//!
//! The would-do list holds no key. It renders the gate's verdicts and offers
//! no way to act on one: approving is a write, and writes live behind the
//! posture and the command line, not in a panel. What it does owe the approval
//! path is an account of *what it drew* — `/do` may not start work the
//! operator cannot see, and the panel is the only surface that knows which
//! proposals fit — so `draw` publishes that, exactly as it publishes the ask
//! row's rect for the click test. Publishing is not acting.
//!
//! The input row has no mode key: in an armed window a printable character goes
//! straight into the ask row. The cost of that is stated where it is paid — see
//! [`AtlasView::typing`] — and the posture decides everything, as on WORKFORCE:
//! a glass window renders the conversation read-only and draws no row at all.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::{ActionItem, Event, PredictorMetrics, Predictors};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::{panel_block, panel_header, refuse};
use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};
use std::time::Instant;

/// The sidebar's fixed width: a model id (`krr:quantum_gram` is 16), its badge,
/// and metric pairs at the widths the board serves. The chat takes the rest,
/// because sentences compress and numbers do not.
const SIDEBAR_W: u16 = 32;
/// Under this the chat cannot hold a clock, an actor and a readable clause.
const CHAT_MIN: u16 = 30;
/// A page of chat, for the paging keys.
const CHAT_PAGE: i64 = 10;
/// One wheel notch, matching what a terminal emulator usually scrolls.
const WHEEL: i64 = 3;
/// The clock, the actor column and the separator in front of every message.
const PREFIX_W: usize = 8 + 1 + 5 + 3;

/// Where the operator is looking and what they have typed. Never what the desk
/// says — that is the `Store`'s.
#[derive(Default)]
pub struct AtlasView {
    /// How far up the conversation the operator has scrolled, in rendered
    /// lines from the bottom. Zero is pinned-to-bottom, which is where a new
    /// answer must land without being asked for.
    offset: usize,
    /// The highest offset the last frame could honour, published by `draw` the
    /// way WORKFORCE publishes its height: a key handler is never told a
    /// geometry, and clamping against a guess would let the scroll run off the
    /// top of the log.
    max_scroll: std::cell::Cell<usize>,
    /// What is typed into the ask row.
    #[cfg(feature = "operator")]
    ask: String,
    /// Whether the ask row holds the keyboard even while empty — set by a
    /// click on the row or by its focus key, cleared by Esc and by sending.
    ///
    /// Separate from the text on purpose: an empty unfocused row yields the
    /// shell's own keys, and this flag is what an operator uses to say "no,
    /// the next `q` is the start of a question".
    #[cfg(feature = "operator")]
    focused: bool,
    /// The ask row's rect on the last frame, published for the click test.
    #[cfg(feature = "operator")]
    input_row: std::cell::Cell<Rect>,
    /// The proposals the would-do panel actually drew on the last frame,
    /// published by `draw` the way `max_scroll` and `input_row` are.
    ///
    /// It exists because approving is the one line on this workstation that
    /// starts work, and the panel is capped at
    /// `min(ACTS_MAX_H, sidebar_height / 2)` rows with no scrollback and no
    /// cursor: three verbose proposals fill it at 120×36. Which of them are on
    /// screen is a fact about geometry, and `cmd::resolve` is a pure function
    /// of (text, desk, posture) that cannot know it — so the surface that does
    /// know publishes it, exactly as BOOK publishes where its band left a plan.
    ///
    /// **The template and the task, as a pair.** The panel draws a template id
    /// and `/do` sends a task id, and the two are bound by one snapshot: a
    /// re-mint or a day roll between the frame that drew a proposal and the
    /// Enter that approves it gives the same word a different task. Keyed on
    /// the template alone, that Enter would send a task no frame ever drew.
    ///
    /// **Every frame THIS VIEW draws writes it, including the frames that draw
    /// no panel** — a list left over from a wider frame is the same lie one
    /// resize later. The shell draws only the active view, so those are the
    /// only frames that reach here at all; a trip to BOOK would otherwise
    /// leave the last ATLAS frame's list standing while the desk moved under
    /// it, and `/do` from another pane reads this before switching back. So
    /// leaving the view clears it too (`View::left`), and the first `/do` from
    /// elsewhere is the refusal that asks for the item rather than an approval
    /// off a frame nobody is looking at.
    #[cfg(feature = "operator")]
    drew: std::cell::RefCell<Vec<(String, String)>>,
    /// The proposal the operator has asked about, drawn first.
    ///
    /// The panel's answer to having no cursor. `/do` on an item the cap hid
    /// refuses and sets this, so the next frame draws that item at the top and
    /// the second `/do` can approve something the operator has read. Without
    /// it the refusal above is a dead end: the hidden item stays hidden, and
    /// the only remedy is a taller terminal.
    #[cfg(feature = "operator")]
    asked: Option<String>,
}

impl View for AtlasView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        if area.width < CHAT_MIN || area.height < 4 {
            // Nothing is on screen, so nothing may be approved off the back of
            // it. Every path out of this function settles `drew`, because the
            // one that did not was the resize: a pane narrowed after a wide
            // frame kept the wide frame's list and `/do` went on approving
            // proposals that had left the screen.
            self.drew_nothing();
            refuse(
                f,
                area,
                format!(
                    "ATLAS needs {CHAT_MIN} columns for a clock, an actor and a readable \
                     clause; this pane has {}.",
                    area.width
                ),
            );
            return;
        }
        // The sidebar is dropped whole before the chat is squeezed: a board
        // clipped to half its metrics misreads as a different board, while a
        // narrower chat is the same conversation in shorter lines.
        let boarded = area.width > CHAT_MIN + SIDEBAR_W;
        let cols = Layout::horizontal([
            Constraint::Min(0),
            Constraint::Length(if boarded { SIDEBAR_W } else { 0 }),
        ])
        .spacing(1)
        .split(area);
        self.draw_chat(f, cols[0], store);
        match boarded {
            true => self.draw_sidebar(f, cols[1], store),
            // The sidebar is dropped whole on a narrow pane, so the would-do
            // panel drew nothing — and a stale list here is the resize hole.
            false => self.drew_nothing(),
        }
    }

    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        self.keys(k, store)
    }

    /// Off screen, so this pane vouches for nothing.
    ///
    /// `drew` is a claim about the frame in front of the operator, and the
    /// approval path reads it from wherever they happen to be typing. Left
    /// standing across a trip to BOOK it would answer about a frame painted
    /// before the desk moved — so it is retracted here, and the first `/do`
    /// from another pane brings ATLAS up and asks for the item instead.
    fn left(&self) {
        self.drew_nothing();
    }

    /// Whether the ask row currently owns the keyboard.
    ///
    /// **The tradeoff this view chose, stated where it is paid.** There is no
    /// mode key: a printable character in an armed window goes straight into
    /// the row, which is what "chat first" costs. But an always-true claim
    /// would take `q`, `r` and the digits from the whole workstation for a
    /// field nobody is using — so the claim is made only once the row holds
    /// text, or once the operator focused it deliberately (a click, or its
    /// focus key). The corner that leaves: the *first* character of a question
    /// cannot be one the shell claims (`q`, `r`, a digit, and the reserved
    /// pair), because with the row empty and unfocused those still navigate.
    /// Focus first when the question starts with one.
    fn typing(&self) -> bool {
        #[cfg(feature = "operator")]
        {
            self.focused || !self.ask.is_empty()
        }
        #[cfg(not(feature = "operator"))]
        false
    }

    fn on_mouse(&mut self, m: MouseEvent, store: &mut Store) -> Option<Command> {
        #[cfg(not(feature = "operator"))]
        let _ = store;
        match m.kind {
            MouseEventKind::ScrollUp => self.scroll(WHEEL),
            MouseEventKind::ScrollDown => self.scroll(-WHEEL),
            MouseEventKind::Down(MouseButton::Left) => {
                // A click on the ask row focuses it — the mouse's answer to
                // the focus key. Armed windows only: a glass frame publishes
                // an empty rect, so the branch is unreachable there.
                #[cfg(feature = "operator")]
                {
                    let row = self.input_row.get();
                    if store.posture.writes()
                        && row.height > 0
                        && m.row == row.y
                        && m.column >= row.x
                        && m.column < row.x.saturating_add(row.width)
                    {
                        self.focused = true;
                    }
                }
            }
            _ => {}
        }
        None
    }
}

// -- the keys ---------------------------------------------------------------

impl AtlasView {
    /// The scroll keys, live in every posture — reading is what a glass window
    /// is for — and everything else to the ask row, which only an armed window
    /// has.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn keys(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command> {
        #[cfg(not(feature = "operator"))]
        let _ = store;
        match k.code {
            KeyCode::Up => {
                self.scroll(1);
                None
            }
            KeyCode::Down => {
                self.scroll(-1);
                None
            }
            KeyCode::PageUp => {
                self.scroll(CHAT_PAGE);
                None
            }
            KeyCode::PageDown => {
                self.scroll(-CHAT_PAGE);
                None
            }
            _ => {
                #[cfg(feature = "operator")]
                if store.posture.writes() {
                    return self.ask_key(k);
                }
                None
            }
        }
    }

    /// Move the conversation window, walls at both ends like every other
    /// cursor on this workstation. Positive is towards older messages.
    fn scroll(&mut self, delta: i64) {
        let next = self.offset as i64 + delta;
        self.offset = next.clamp(0, self.max_scroll.get() as i64) as usize;
    }
}

#[cfg(feature = "operator")]
impl AtlasView {
    /// The ask row's keys. Everything printable types; Enter sends; Esc gives
    /// the keyboard back.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    fn ask_key(&mut self, k: KeyEvent) -> Option<Command> {
        match k.code {
            // The focus key, for a question whose first character the shell
            // would otherwise claim. Only an empty unfocused row treats it as
            // focus; a row being typed into needs the letter itself.
            KeyCode::Char('i') if self.ask.is_empty() && !self.focused => self.focused = true,
            KeyCode::Char(c) => self.ask.push(c),
            KeyCode::Backspace => {
                self.ask.pop();
            }
            KeyCode::Esc => {
                self.ask.clear();
                self.focused = false;
            }
            KeyCode::Enter => {
                let text = self.ask.trim().to_string();
                // An empty question is not a question: the owner would refuse
                // it ("message text is required") and the refusal would reach
                // the operator as a failed write rather than as a slip.
                if text.is_empty() {
                    return None;
                }
                self.ask.clear();
                self.focused = false;
                return Some(Command::Message(text));
            }
            _ => {}
        }
        None
    }
}

// -- the conversation -------------------------------------------------------

impl AtlasView {
    fn draw_chat(&self, f: &mut Frame, area: Rect, store: &Store) {
        let t = theme();
        let block = panel_block();
        let inner = block.inner(area);
        f.render_widget(block, area);

        // The input row is a *row*, taken out of the layout before the log
        // claims it, and absent whole in a glass window — a prompt that cannot
        // be typed into is a client that looks broken. The hint row is always
        // there: it is where this pane says what the keys do, and, unarmed,
        // why there is no row to type into.
        let input = u16::from(self.input_row_shown(store));
        let rows = Layout::vertical([
            Constraint::Length(1), // header
            Constraint::Min(0),    // the conversation
            Constraint::Length(input),
            Constraint::Length(1), // the hint row
        ])
        .split(inner);

        let chip = match store.atlas_chat().len() {
            0 => "no conversation yet".to_string(),
            n => format!("{n} messages"),
        };
        f.render_widget(Paragraph::new(head("ATLAS", &chip, rows[0].width)), rows[0]);

        let lines = chat_lines(store.atlas_chat(), rows[1].width);
        let room = rows[1].height as usize;
        // Publish what the keys may clamp against, then window from the
        // bottom: zero offset is the newest line on the last row.
        self.max_scroll.set(lines.len().saturating_sub(room));
        let offset = self.offset.min(self.max_scroll.get());
        let end = lines.len() - offset;
        let start = end.saturating_sub(room);
        f.render_widget(Paragraph::new(lines[start..end].to_vec()), rows[1]);

        self.draw_input(f, rows[2], store);
        let hint = if self.input_row_shown(store) {
            "Enter sends · Esc clears · ↑↓ wheel scroll"
        } else {
            "read-only in this posture — asking needs an operator window"
        };
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                hint,
                Style::default().fg(t.text_dim),
            ))),
            rows[3],
        );
    }

    #[cfg(feature = "operator")]
    fn input_row_shown(&self, store: &Store) -> bool {
        store.posture.writes()
    }

    #[cfg(not(feature = "operator"))]
    fn input_row_shown(&self, _store: &Store) -> bool {
        false
    }

    #[cfg(feature = "operator")]
    fn draw_input(&self, f: &mut Frame, area: Rect, store: &Store) {
        // Published on every frame, including the ones that draw no row: the
        // click test reads it, and a rect left over from an armed frame would
        // let a click focus a row a glass frame is not drawing.
        self.input_row.set(if self.input_row_shown(store) {
            area
        } else {
            Rect::default()
        });
        if !self.input_row_shown(store) || area.height == 0 {
            return;
        }
        let t = theme();
        let line = if self.focused || !self.ask.is_empty() {
            Line::from(vec![
                Span::styled("ask › ", Style::default().fg(t.accent)),
                Span::styled(
                    self.ask.clone(),
                    Style::default()
                        .fg(t.text_primary)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled("▏", Style::default().fg(t.accent)),
            ])
        } else {
            Line::from(vec![
                Span::styled("ask › ", Style::default().fg(t.accent)),
                Span::styled(
                    "Ask Atlas — type to ask, Enter sends",
                    Style::default().fg(t.text_dim),
                ),
            ])
        };
        f.render_widget(Paragraph::new(line), area);
    }

    #[cfg(not(feature = "operator"))]
    fn draw_input(&self, _f: &mut Frame, _area: Rect, _store: &Store) {}
}

/// The whole conversation as rendered lines, oldest first, wrapped to `width`.
///
/// Built whole and windowed by the caller, because the scroll offset is in
/// *rendered* lines: a message is as many lines as its wrap needed, and an
/// offset counted in messages would jump by paragraphs.
fn chat_lines(chat: &[Event], width: u16) -> Vec<Line<'static>> {
    let t = theme();
    if chat.is_empty() {
        return vec![Line::from(Span::styled(
            "nothing has been asked on this desk yet",
            Style::default().fg(t.text_dim),
        ))];
    }
    let room = (width as usize).saturating_sub(PREFIX_W).max(8);
    let mut out = Vec::new();
    for event in chat {
        let payload = event.payload.as_ref();
        let actor = payload
            .and_then(|p| p.get("actor"))
            .and_then(|a| a.as_str())
            .unwrap_or("");
        let text = payload
            .and_then(|p| p.get("text"))
            .and_then(|x| x.as_str())
            .unwrap_or(MISSING);
        let error = payload
            .and_then(|p| p.get("error"))
            .and_then(|e| e.as_str());
        let stamp = format::clock(event.ts.as_ref()).unwrap_or_else(|| MISSING.to_string());
        // The operator's rows read as their own voice, the desk's as the
        // desk's, and a failed answer is neither — it is a refusal, in the
        // refusal's colour, with the owner's own error under it.
        let (name, name_tone) = match actor {
            "operator" => ("YOU", t.accent),
            _ => ("ATLAS", t.positive),
        };
        let body_tone = if error.is_some() {
            t.negative
        } else if actor == "operator" {
            t.text_primary
        } else {
            t.text_secondary
        };
        for (i, chunk) in wrap(text, room).into_iter().enumerate() {
            let lead = if i == 0 {
                vec![
                    Span::styled(format!("{stamp} "), Style::default().fg(t.text_tertiary)),
                    Span::styled(
                        format!("{name:>5} "),
                        Style::default().fg(name_tone).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled("› ", Style::default().fg(t.text_dim)),
                ]
            } else {
                vec![Span::raw(" ".repeat(PREFIX_W))]
            };
            let mut spans = lead;
            spans.push(Span::styled(chunk, Style::default().fg(body_tone)));
            out.push(Line::from(spans));
        }
        if let Some(error) = error {
            for chunk in wrap(error, room) {
                out.push(Line::from(vec![
                    Span::raw(" ".repeat(PREFIX_W)),
                    Span::styled(chunk, Style::default().fg(t.text_dim)),
                ]));
            }
        }
    }
    out
}

/// Word wrap at `width` cells, hard-breaking a word longer than the line.
///
/// Hand-rolled rather than `Paragraph`'s own `Wrap`, because the scroll offset
/// is counted in rendered lines and the renderer's wrap happens after the
/// window has already been cut — a pane that let the widget wrap would scroll
/// by an amount that disagrees with what moved.
fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut out = Vec::new();
    let mut line = String::new();
    let mut used = 0usize;
    for word in text.split_whitespace() {
        let mut word: Vec<char> = word.chars().collect();
        // A word longer than the line is cut rather than looping forever;
        // URLs and hashes are the words this actually happens to.
        while !word.is_empty() {
            let sep = usize::from(used > 0);
            if used + sep + word.len() <= width {
                if sep == 1 {
                    line.push(' ');
                }
                line.extend(word.iter());
                used += sep + word.len();
                word.clear();
            } else if used == 0 {
                line.extend(word.drain(..width));
                out.push(std::mem::take(&mut line));
                used = 0;
            } else {
                out.push(std::mem::take(&mut line));
                used = 0;
            }
        }
    }
    if !line.is_empty() || out.is_empty() {
        out.push(line);
    }
    out
}

// -- what the desk would do -------------------------------------------------

/// How many characters of the owner's prose one item carries.
///
/// Wide enough for the longest sentence the owner actually writes here (the
/// spent-proposal refusal runs to ~120), so bounding cuts a flood and not the
/// desk's own words.
const SAID_MAX: usize = 160;

/// How much of `task_status` a row will carry.
///
/// Its own budget, because it shares a row with the template id rather than
/// owning lines of its own: `reconciled` is the longest status the registry
/// writes, and anything past that is a status nothing produced. It goes through
/// the same gate all the same — a status is wire data like any other, and one
/// long enough to take the whole row would leave the id with nowhere to render.
const STATUS_MAX: usize = 12;

/// The most rows the would-do list may take out of the sidebar.
///
/// The board is what the sidebar is for, and this list only grows: items stay
/// for the whole trading day, so an uncapped panel would push the evidence off
/// the pane by mid-afternoon. What does not fit is counted on the last row.
const ACTS_MAX_H: usize = 12;

/// The indent a wrapped sentence sits at, matching the board's own rows.
const ACT_INDENT: usize = 2;

/// What the `?` marker means, said once for the panel.
///
/// The owner attaches its own sentence to every unruled item — "the data
/// preconditions were not checked here; POST /api/atlas/actionables asks the
/// gate for today's verdict" — and four wrapped rows of it per item would bury
/// the list it is about. The marker carries the state; this carries what the
/// marker means.
const NOT_RULED: &str = "? = not checked on this surface";

/// Foreign text, bounded once on the way in.
///
/// **Every string the owner sends this panel comes through here** — `purpose`,
/// `reason` and `task_status` alike — for the reason `format::bounded` states:
/// nothing on the wire is guaranteed to be the owner's, and bounding per call
/// site is how a row added later becomes the one that forgot. The budget is the
/// caller's because a sentence and a one-word status are not the same row; the
/// gate is not. `format::text` first, because the owner serialises a string it
/// never set as `""`.
fn said(value: Option<&String>, max: usize) -> Option<String> {
    format::text(value).map(|said| format::bounded(said, max))
}

/// One proposal's rows: what it is, and the one sentence that matters about it.
///
/// Three verdicts, three renderings — a glyph *and* a step of the text ramp, so
/// the distinction survives a terminal nobody can read colour on. `None` is not
/// drawn as either neighbour: it is the surface saying it did not rule, and a
/// panel that painted it as an offer would be inviting an approval the gate has
/// not been asked about.
fn act_rows(item: &ActionItem, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let (mark, mark_tone, id_tone, said_tone) = match item.startable {
        Some(true) => ("✓", t.positive, t.text_primary, t.text_secondary),
        None => ("?", t.text_tertiary, t.text_secondary, t.text_tertiary),
        Some(false) => ("✗", t.negative_dim, t.text_dim, t.text_dim),
    };
    // `queued` is every fresh proposal and says nothing. The rest — running,
    // completed, failed, expired — is the only thing that tells a live proposal
    // from one the day has already spent, and the list keeps both all day.
    let status = said(item.task_status.as_ref(), STATUS_MAX)
        .filter(|status| status != "queued")
        .map(|status| format!(" {status}"));
    let width = width as usize;
    let room = width.saturating_sub(2 + status.as_ref().map_or(0, |s| s.chars().count()));
    let mut head = vec![
        Span::styled(format!("{mark} "), Style::default().fg(mark_tone)),
        Span::styled(
            clip(format::or_missing(item.template_id.as_ref()), room),
            Style::default().fg(id_tone),
        ),
    ];
    if let Some(status) = status {
        head.push(Span::styled(status, Style::default().fg(t.text_dim)));
    }
    let mut out = vec![Line::from(head)];
    // A refusal renders the owner's own sentence, because that sentence is what
    // the panel is for; anything else renders what the template would do.
    let sentence = match item.startable {
        Some(false) => said(item.reason.as_ref(), SAID_MAX),
        _ => said(item.purpose.as_ref(), SAID_MAX),
    };
    if let Some(sentence) = sentence {
        for chunk in wrap(&sentence, width.saturating_sub(ACT_INDENT)) {
            out.push(Line::from(Span::styled(
                format!("{}{chunk}", " ".repeat(ACT_INDENT)),
                Style::default().fg(said_tone),
            )));
        }
    }
    out
}

/// The panel's rows, and which proposals they are about.
///
/// Two halves of one answer rather than two functions: what the packer below
/// decided to draw is the same fact the approval path has to check against,
/// and a second function deriving it again is two accounts of one frame — the
/// exact shape that made the old note and the old count disagree.
struct Acts {
    lines: Vec<Line<'static>>,
    /// The proposals drawn, as `(template_id, task_id)`, in the order they
    /// were drawn.
    ///
    /// **The pair, not the word.** What the panel draws is a template id and
    /// what `/do` sends is a task id; they are bound by the snapshot the frame
    /// was drawn from, and a re-mint or a day roll rebinds them. An item the
    /// owner named nothing, or served no task for, is left out — neither can
    /// be approved, so neither is something this list may vouch for.
    ///
    /// Gated with the approval path it exists for. A glass build has no `/do`
    /// and no writer, so there is nothing to check this against — absence,
    /// not a field carried and never read.
    #[cfg(feature = "operator")]
    shown: Vec<(String, String)>,
}

/// The whole panel, or nothing at all.
///
/// Nothing at all when the list is empty: an owner that serves no block and one
/// that has minted no proposals are both "nobody has asked today", and an empty
/// box would read as a desk with nothing it could do.
///
/// `room` is the rows the panel may spend, and it never spends more. Items are
/// drawn whole or not at all — half an item is a sentence that reads as
/// finished — and whatever is left over is counted rather than dropped. Even at
/// a `room` too small for one item the header's own chip still says how many
/// there are.
///
/// `asked` is the one proposal the operator has named on the command line, and
/// it is drawn first. That is the panel's whole answer to having no cursor:
/// the gate's own order is otherwise untouched, and reordering it for anything
/// short of an explicit request would be this client deciding which proposal
/// matters.
fn acts_lines(items: &[ActionItem], width: u16, room: usize, asked: Option<&str>) -> Acts {
    if items.is_empty() {
        return Acts {
            lines: Vec::new(),
            #[cfg(feature = "operator")]
            shown: Vec::new(),
        };
    }
    // The asked-about item, then the gate's own order with it removed. Stable
    // in both halves, so nothing else moves. Matched on `asked` rather than on
    // the item's own emptiness: `format::text` is `None` for an item the owner
    // named nothing, and a comparison against an absent `asked` would pull
    // exactly those to the top of a panel nobody asked anything of.
    let named = |item: &ActionItem| match asked {
        Some(asked) => format::text(item.template_id.as_ref()) == Some(asked),
        None => false,
    };
    let items: Vec<&ActionItem> = items
        .iter()
        .filter(|item| named(item))
        .chain(items.iter().filter(|item| !named(item)))
        .collect();
    let refused = items
        .iter()
        .filter(|item| item.startable == Some(false))
        .count();
    let chip = match refused {
        0 => format!("{} proposed", items.len()),
        _ => format!("{refused} of {} refused", items.len()),
    };
    // Where each drawn item starts, so one can be taken back off whole. Filled
    // against the header alone; the note and the legend are settled below,
    // because whether either is needed is not known until it is.
    let mut marks: Vec<usize> = Vec::new();
    let mut body: Vec<Line<'static>> = Vec::new();
    for item in &items {
        let rows = act_rows(item, width);
        if 1 + body.len() + rows.len() > room {
            break;
        }
        marks.push(body.len());
        body.extend(rows);
    }
    // Neither trailing row may be paid for out of an item already drawn: a
    // refusal whose last line was overwritten by the note ends mid-clause and
    // reads as finished, which is the one thing this panel must not do. So an
    // item is handed back whole until all of it fits.
    //
    // And the legend is decided over what is *shown*, not over the list: a `?`
    // beyond the cap is not a marker on screen, and a row explaining one is a
    // row spent on nothing.
    let (note, legend) = loop {
        let note = marks.len() < items.len();
        let legend = items[..marks.len()]
            .iter()
            .any(|item| item.startable.is_none());
        if 1 + body.len() + usize::from(note) + usize::from(legend) <= room {
            break (note, legend);
        }
        match marks.pop() {
            Some(mark) => body.truncate(mark),
            // Nothing left to hand back. The header's chip is what still says
            // how many proposals there are, and the clamp below drops the rest.
            None => break (note, legend),
        }
    };
    let mut out = vec![head("would do", &chip, width)];
    out.append(&mut body);
    if note {
        // The unshown count, and — when the operator has *asked* for one of
        // them — which one. The command line refuses an item the panel did not
        // draw and asks for it; if it still does not fit, that refusal repeats
        // with nothing on screen to explain why. This row is the explanation,
        // and it costs nothing: the note was already budgeted for.
        let unshown = items.len() - marks.len();
        let asked_unshown = asked.filter(|asked| {
            items[marks.len()..]
                .iter()
                .any(|item| format::text(item.template_id.as_ref()) == Some(asked))
        });
        out.push(dim(&clip(
            &match asked_unshown {
                Some(asked) => format!("+{unshown} more; {asked} needs more rows"),
                None => format!("+{unshown} more, unshown"),
            },
            width as usize,
        )));
    }
    if legend {
        out.push(dim(NOT_RULED));
    }
    // The board below is not this panel's to spend. Every path above respects
    // `room` except the one that ran out of items to hand back, and a panel one
    // row over budget takes that row from the evidence.
    out.truncate(room);
    // What is on screen, read off the marks the packer kept rather than
    // recomputed: the approval path refuses an item this list does not name,
    // and a second derivation of "what fits" is how that check would come to
    // permit an item nobody drew.
    //
    // Exact, including after the clamp: the settle loop only exits over budget
    // once it has handed *every* item back, so a truncated panel has an empty
    // `marks` and names nothing. An item the owner sent without a template id,
    // or without a task, is dropped here because it cannot be approved either
    // — `/do` has no word for the first and nothing to send for the second.
    Acts {
        lines: out,
        #[cfg(feature = "operator")]
        shown: items[..marks.len()]
            .iter()
            .filter_map(|item| {
                Some((
                    format::text(item.template_id.as_ref())?.to_string(),
                    format::text(item.task_id.as_ref())?.to_string(),
                ))
            })
            .collect(),
    }
}

// -- the predictor board ----------------------------------------------------

/// The sidebar: what the desk would do, over the evidence it would reason from.
impl AtlasView {
    fn draw_sidebar(&self, f: &mut Frame, area: Rect, store: &Store) {
        let t = theme();
        let block = panel_block();
        let inner = block.inner(area);
        f.render_widget(block, area);

        // Half the sidebar at most. The board below is why this column exists, and
        // a day's proposals only ever accumulate.
        let acts = acts_lines(
            store.actionables(),
            inner.width,
            ACTS_MAX_H.min(inner.height as usize / 2),
            self.asked(),
        );
        // Published, not returned: `draw` is `&self` because a paint may not move
        // what the operator is looking at, and what it drew is a fact about this
        // frame that the approval path has no other way to learn.
        #[cfg(feature = "operator")]
        self.drew.replace(acts.shown);
        let mut lines = acts.lines;
        if !lines.is_empty() {
            lines.push(Line::from(""));
        }
        lines.push(panel_header("predictor board"));
        match store.predictors() {
            // Absence is named, exactly as the owner names it: a desk that never
            // ran the board and one whose newest row cannot be read are different
            // facts with different remedies.
            None => lines.push(dim("the owner served no predictor summary")),
            Some(board) => match board.status.as_deref() {
                Some("never_ran") => {
                    lines.push(dim("board never ran — /api docs name"));
                    lines.push(dim("research.predictor_board"));
                }
                Some("unreadable") => {
                    lines.push(Line::from(Span::styled(
                        "newest board row is unreadable",
                        Style::default().fg(t.negative),
                    )));
                    lines.push(dim(&format!(
                        "run {}",
                        short_id(board.run_id.as_deref(), 12)
                    )));
                }
                _ => board_lines(&mut lines, board, inner.width),
            },
        }

        // The tiny status: what the manager itself is doing, under the evidence
        // it would reason from. Two or three lines, not a second DESK.
        lines.push(Line::from(""));
        lines.push(panel_header("atlas"));
        let atlas = store.snapshot.as_ref().and_then(|s| s.atlas.as_ref());
        lines.push(kv(
            "state",
            format::upper(atlas.and_then(|a| format::text(a.state.as_ref()))),
        ));
        lines.push(kv(
            "mode",
            format::or_missing(atlas.and_then(|a| a.mode.as_ref())).to_string(),
        ));
        if let Some(blocked) = atlas.and_then(|a| format::text(a.blocked_reason.as_ref())) {
            for chunk in wrap(blocked, (inner.width as usize).saturating_sub(2)) {
                lines.push(Line::from(Span::styled(
                    format!("  {chunk}"),
                    Style::default().fg(t.warning),
                )));
            }
        }

        f.render_widget(
            Paragraph::new(
                lines
                    .into_iter()
                    .take(inner.height as usize)
                    .collect::<Vec<_>>(),
            ),
            inner,
        );
    }

    /// The proposal the operator asked the panel to show, if any.
    ///
    /// Two bodies rather than a field a glass build carries and never reads:
    /// there is no `/do` without a writer, so in that build there is nothing
    /// to ask about and nothing to publish.
    #[cfg(feature = "operator")]
    fn asked(&self) -> Option<&str> {
        self.asked.as_deref()
    }

    #[cfg(not(feature = "operator"))]
    fn asked(&self) -> Option<&str> {
        None
    }

    /// Whether the would-do panel drew this exact proposal on the last frame.
    ///
    /// The question the approval path asks before it starts anything, and the
    /// reason `drew` is published at all. `false` for a proposal beyond the
    /// panel's row budget, for one on a pane too narrow to hold the sidebar,
    /// for every proposal before the first frame that drew any, and for a
    /// template whose **task id has moved** since it was drawn — absence is
    /// not permission, and neither is a matching word over a task the operator
    /// never saw.
    #[cfg(feature = "operator")]
    pub fn drew(&self, template: &str, task: &str) -> bool {
        self.drew
            .borrow()
            .iter()
            .any(|(shown, bound)| shown == template && bound == task)
    }

    /// This frame drew no proposals at all.
    ///
    /// Two bodies rather than a cfg at each call site, and called from every
    /// path in `draw` that does not reach the sidebar. A glass build has
    /// nothing to publish and nothing to clear.
    #[cfg(feature = "operator")]
    fn drew_nothing(&self) {
        self.drew.borrow_mut().clear();
    }

    #[cfg(not(feature = "operator"))]
    fn drew_nothing(&self) {}

    /// Draw this proposal first from the next frame on.
    ///
    /// What makes the refusal above recoverable rather than a dead end: an
    /// item the cap hid is one `/do` away from the top of the panel, where it
    /// can be read and then approved. It moves nothing else and grants
    /// nothing — the gate's own order holds for every other item, and being
    /// visible is not being startable.
    #[cfg(feature = "operator")]
    pub fn ask_about(&mut self, template: &str) {
        self.asked = Some(template.to_string());
    }
}

/// The readable board: champion against baseline, every number the verdict
/// was derived from, and the per-fold bars that say whether a mean is a skill
/// estimate or an average over folds that changed sign.
fn board_lines(lines: &mut Vec<Line<'static>>, board: &Predictors, width: u16) {
    let t = theme();
    // One scale across both models, so two fold rows are comparable bars
    // rather than two separately-stretched pictures of the same axis.
    let folds: Vec<f64> = [&board.champion, &board.baseline]
        .into_iter()
        .flatten()
        .flat_map(|m| m.per_fold.iter().filter_map(|f| f.ic))
        .collect();
    let lo = folds.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = folds.iter().copied().fold(f64::NEG_INFINITY, f64::max);

    model_lines(lines, "CHAMPION", board.champion.as_ref(), lo, hi, width);
    model_lines(lines, "BASELINE", board.baseline.as_ref(), lo, hi, width);

    let age = board
        .age_days
        .map(|d| format!("{d}d old"))
        .unwrap_or_else(|| MISSING.to_string());
    lines.push(Line::from(vec![
        Span::styled(format!("board {age}"), Style::default().fg(t.text_tertiary)),
        Span::styled(
            format!(" · run {}", short_id(board.run_id.as_deref(), 8)),
            Style::default().fg(t.text_dim),
        ),
    ]));
    // `None` is a board that predates the null — neither established nor
    // refuted, and it must not render as either.
    if let Some(established) = board.champion_established {
        lines.push(match established {
            true => Line::from(Span::styled(
                "edge survives selection null",
                Style::default().fg(t.positive),
            )),
            false => Line::from(Span::styled(
                "edge not established vs null",
                Style::default().fg(t.warning),
            )),
        });
    }
}

/// One model's block: who it is, what it measured, and how it compares.
fn model_lines(
    lines: &mut Vec<Line<'static>>,
    label: &str,
    metrics: Option<&PredictorMetrics>,
    lo: f64,
    hi: f64,
    width: u16,
) {
    let t = theme();
    let Some(m) = metrics else {
        lines.push(dim(&format!("{label}: absent from the board")));
        return;
    };
    let id = format::or_missing(m.model_id.as_ref());
    // The quantum badge, by the web UI's own rule: an augmented variant or the
    // quantum gram family wears `q`, everything classical wears nothing.
    let quantum = m.variant.as_deref().is_some_and(|v| v != "none")
        || m.family.as_deref() == Some("quantum_gram");
    let mut head = vec![
        Span::styled(
            format!("{label:<9}"),
            Style::default()
                .fg(t.text_secondary)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            clip(id, (width as usize).saturating_sub(12)),
            Style::default()
                .fg(t.text_primary)
                .add_modifier(Modifier::BOLD),
        ),
    ];
    if quantum {
        head.push(Span::styled(
            " q",
            Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
        ));
    }
    lines.push(Line::from(head));

    lines.push(Line::from(vec![
        Span::styled("  IC ", Style::default().fg(t.text_tertiary)),
        Span::styled(num4(m.mean_ic), Style::default().fg(t.text_primary)),
        Span::styled(
            format!(" ±{}", num4(m.ic_std)),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled("  stab ", Style::default().fg(t.text_tertiary)),
        Span::styled(num2(m.ic_stability), Style::default().fg(t.text_primary)),
    ]));

    let usable = match m.usable {
        Some(true) => Span::styled("✓ usable", Style::default().fg(t.positive)),
        Some(false) => Span::styled("✗ not usable", Style::default().fg(t.negative)),
        None => Span::styled(MISSING, Style::default().fg(t.text_dim)),
    };
    let mut verdict = vec![Span::raw("  "), usable];
    if let Some(delta) = m.delta_mean_ic_vs_baseline {
        // The baseline's own delta is identically zero and says nothing.
        if delta != 0.0 {
            let tone = if delta > 0.0 { t.positive } else { t.negative };
            verdict.push(Span::styled(
                format!("  Δ{delta:+.4}"),
                Style::default().fg(tone),
            ));
        }
    }
    lines.push(Line::from(verdict));
    // Its own line: beside the verdict the three ran past the sidebar's width
    // and the t-statistic was clipped mid-number — a truncated number is a
    // different number.
    let mut versus = Vec::new();
    if let Some(wins) = m.wins_vs_baseline.filter(|w| *w > 0) {
        versus.push(Span::styled(
            format!("  wins {wins}"),
            Style::default().fg(t.text_secondary),
        ));
    }
    if let Some(paired) = m.paired_t_vs_baseline {
        versus.push(Span::styled(
            format!("  t {paired:.2}"),
            Style::default().fg(t.text_secondary),
        ));
    }
    if !versus.is_empty() {
        lines.push(Line::from(versus));
    }

    if !m.per_fold.is_empty() {
        lines.push(Line::from(vec![
            Span::styled("  folds ", Style::default().fg(t.text_tertiary)),
            Span::styled(spark(m, lo, hi), Style::default().fg(t.accent)),
        ]));
    }
}

/// The per-fold ICs as unicode bars on one shared scale.
fn spark(m: &PredictorMetrics, lo: f64, hi: f64) -> String {
    const BLOCKS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
    m.per_fold
        .iter()
        .map(|fold| match fold.ic {
            None => '·',
            Some(_) if hi <= lo => BLOCKS[3],
            Some(ic) => {
                let at = ((ic - lo) / (hi - lo) * 7.0).round().clamp(0.0, 7.0) as usize;
                BLOCKS[at]
            }
        })
        .collect()
}

// -- small helpers ----------------------------------------------------------

fn num4(value: Option<f64>) -> String {
    value
        .map(|v| format!("{v:.4}"))
        .unwrap_or_else(|| MISSING.to_string())
}

fn num2(value: Option<f64>) -> String {
    value
        .map(|v| format!("{v:.2}"))
        .unwrap_or_else(|| MISSING.to_string())
}

fn short_id(id: Option<&str>, len: usize) -> String {
    match id {
        Some(id) => id.chars().take(len).collect(),
        None => MISSING.to_string(),
    }
}

fn clip(text: &str, width: usize) -> String {
    if text.chars().count() <= width {
        return text.to_string();
    }
    let mut cut: String = text.chars().take(width.saturating_sub(1)).collect();
    cut.push('…');
    cut
}

fn dim(text: &str) -> Line<'static> {
    Line::from(Span::styled(
        text.to_string(),
        Style::default().fg(theme().text_dim),
    ))
}

fn kv(label: &str, value: String) -> Line<'static> {
    let t = theme();
    Line::from(vec![
        Span::styled(format!("{label:<11}"), Style::default().fg(t.text_tertiary)),
        Span::styled(value, Style::default().fg(t.text_primary)),
    ])
}

/// A header with a right-aligned chip, exactly as WORKFORCE draws its own.
fn head(title: &str, chip: &str, width: u16) -> Line<'static> {
    let t = theme();
    let title_span = panel_header(title);
    let used: usize = title_span
        .spans
        .iter()
        .map(|s| s.content.chars().count())
        .sum();
    let pad = (width as usize)
        .saturating_sub(used + chip.chars().count())
        .max(1);
    let mut spans = title_span.spans;
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(
        chip.to_string(),
        Style::default().fg(t.text_tertiary),
    ));
    Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn long_text_wraps_at_the_width_and_loses_nothing() {
        let text = "The regime read is fragile calm with two of five detectors disagreeing, \
                    so the operational policy holds the defensive tilt.";
        let lines = wrap(text, 30);
        assert!(lines.len() > 2, "{lines:?}");
        for line in &lines {
            assert!(line.chars().count() <= 30, "{line:?}");
        }
        assert_eq!(
            lines.join(" ").split_whitespace().collect::<Vec<_>>(),
            text.split_whitespace().collect::<Vec<_>>(),
            "wrapping changed the words"
        );
    }

    #[test]
    fn a_word_longer_than_the_line_is_cut_rather_than_looping() {
        let lines = wrap("targets_hash=0123456789abcdef0123456789abcdef", 10);
        assert!(lines.iter().all(|l| l.chars().count() <= 10), "{lines:?}");
        assert_eq!(
            lines.concat(),
            "targets_hash=0123456789abcdef0123456789abcdef"
        );
    }

    #[test]
    fn empty_text_is_one_empty_line_not_zero_lines() {
        // A message with no text still occupies a row: a rendered offset that
        // skipped it would disagree with the log the owner holds.
        assert_eq!(wrap("", 20), vec![String::new()]);
    }

    #[test]
    fn the_unfocused_empty_input_yields_the_digits_to_the_shell() {
        // The routing half of the tradeoff `typing` documents: with nothing
        // typed and no focus, a digit is the shell's and switches views.
        use crate::store::{Store, ViewId};
        use crate::ui::views::Views;
        use crossterm::event::KeyModifiers;

        let mut store = Store::default();
        store.settle_door();
        store.nav.view = ViewId::Atlas;
        let mut views = Views::new();
        atlas_key(&mut store, &mut views, KeyCode::Char('3'));
        assert_eq!(store.nav.view, ViewId::Markets, "a digit did not navigate");

        fn atlas_key(store: &mut Store, views: &mut Views, code: KeyCode) {
            crate::ui::shell::on_key(
                crossterm::event::KeyEvent::new(code, KeyModifiers::NONE),
                store,
                views,
            );
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_focused_or_non_empty_row_claims_the_keyboard_and_esc_gives_it_back() {
        let mut view = AtlasView::default();
        assert!(!view.typing(), "an idle view claimed the keyboard");
        view.focused = true;
        assert!(view.typing(), "the focus flag did not claim it");
        view.focused = false;
        view.ask.push('w');
        assert!(view.typing(), "typed text did not claim it");
        view.ask_key(crossterm::event::KeyEvent::new(
            KeyCode::Esc,
            crossterm::event::KeyModifiers::NONE,
        ));
        assert!(!view.typing(), "the escape key did not give it back");
        assert!(view.ask.is_empty());
    }

    #[test]
    fn the_three_verdicts_are_three_distinct_renderings() {
        // Collapsing any two loses a fact the operator needs: `true` is the
        // gate's yes, `false` is its no with a sentence, and `None` is a
        // verdict this surface did not compute. Reading the third as either of
        // the others is a client asserting something the owner did not.
        let item = |startable: Option<bool>| ActionItem {
            template_id: Some("regime_review".into()),
            purpose: Some("Re-read the regime panel.".into()),
            startable,
            reason: Some("creates a paper plan".into()),
            ..ActionItem::default()
        };
        let marks: Vec<(String, Option<ratatui::style::Color>)> = [Some(true), None, Some(false)]
            .into_iter()
            .map(|verdict| {
                let rows = act_rows(&item(verdict), 32);
                let head = &rows[0];
                (
                    head.spans[0].content.trim().to_string(),
                    head.spans[1].style.fg,
                )
            })
            .collect();
        let glyphs: Vec<&str> = marks.iter().map(|(g, _)| g.as_str()).collect();
        assert_eq!(glyphs, vec!["✓", "?", "✗"], "two verdicts share a glyph");
        let tones: Vec<_> = marks.iter().map(|(_, tone)| *tone).collect();
        assert_eq!(tones.len(), 3);
        assert!(
            tones[0] != tones[1] && tones[1] != tones[2] && tones[0] != tones[2],
            "two verdicts share a tone: {tones:?}"
        );

        // And the sentence follows the verdict: a refusal renders the owner's
        // own reason, anything else renders what the template would do.
        let refused = act_rows(&item(Some(false)), 32);
        assert!(refused[1].spans[0].content.contains("creates a paper plan"));
        let pending = act_rows(&item(None), 32);
        assert!(pending[1].spans[0].content.contains("Re-read the regime"));
    }

    #[test]
    fn foreign_prose_is_bounded_once_before_any_row_is_built() {
        // One gate, not one per call site: `purpose` and `reason` are both the
        // wire's, and a row added later must not be the one that forgot.
        let flood = "x".repeat(400);
        let cut = said(Some(&flood), SAID_MAX).unwrap();
        assert_eq!(cut.chars().count(), SAID_MAX + 1, "{cut}");
        assert!(cut.ends_with('…'), "the cut was not marked: {cut}");
        assert_eq!(
            said(Some(&"two   lines\nof it".to_string()), SAID_MAX),
            Some("two lines of it".to_string())
        );
        // The owner serialises a string it never set as `""`, which is absence.
        assert_eq!(said(Some(&String::new()), SAID_MAX), None);
    }

    #[test]
    fn a_status_the_owner_did_not_write_cannot_take_the_row_from_its_id() {
        // `task_status` is wire data like the prose is, and it shares a row
        // rather than owning one: unbounded, a long status leaves the id no
        // cells at all and the proposal loses its own name.
        let rows = act_rows(
            &ActionItem {
                template_id: Some("regime_review".into()),
                purpose: Some("Re-read the regime panel.".into()),
                task_status: Some("r".repeat(200)),
                ..ActionItem::default()
            },
            32,
        );
        let head: String = rows[0].spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(
            head.contains("regime_review"),
            "the id was clipped away: {head}"
        );
        assert!(
            head.chars().count() <= 32,
            "the row ran past the column: {head}"
        );
    }

    #[test]
    fn an_empty_list_draws_no_panel_at_all() {
        // Not an empty box: a desk nobody has asked has nothing to say here.
        assert!(acts_lines(&[], 32, 12, None).lines.is_empty());
    }

    /// A proposal that costs exactly two rows: its id, and one short sentence.
    #[cfg(test)]
    fn two_row_item(id: &str, startable: Option<bool>) -> ActionItem {
        ActionItem {
            template_id: Some(id.into()),
            purpose: Some("Short.".into()),
            startable,
            reason: Some("Nope.".into()),
            // The owner binds every proposal it serves to a persisted task,
            // and the panel vouches for the pair rather than for the word.
            task_id: Some(format!("t-{id}")),
            ..ActionItem::default()
        }
    }

    /// What the panel vouched for, as the approval path reads it.
    #[cfg(feature = "operator")]
    fn shown(acts: &Acts) -> Vec<(&str, &str)> {
        acts.shown
            .iter()
            .map(|(template, task)| (template.as_str(), task.as_str()))
            .collect()
    }

    #[test]
    fn an_item_is_drawn_whole_or_it_is_not_drawn() {
        // The budget landing exactly on an item's last row is not a licence to
        // overwrite it: a refusal that ends mid-clause reads as finished, and
        // the count that replaced it would under-report by the item it ate.
        let items = [
            two_row_item("alpha", Some(false)),
            two_row_item("beta", Some(false)),
        ];
        let text = rendered(&acts_lines(&items, 32, 3, None).lines);
        assert!(
            !text.iter().any(|line| line.contains("alpha")),
            "an item was drawn without its sentence: {text:?}"
        );
        assert!(
            text.iter().any(|line| line.contains("+2 more")),
            "the count disagrees with what was drawn: {text:?}"
        );
    }

    #[test]
    fn the_panel_never_spends_more_rows_than_it_was_given() {
        // Every row past `room` is a row taken off the board below, and the
        // narrow end is where that happens: a header and a legend are two rows
        // on a one-row budget.
        let items = [
            two_row_item("alpha", None),
            two_row_item("beta", Some(false)),
        ];
        for room in 0..8 {
            let lines = acts_lines(&items, 32, room, None).lines;
            assert!(
                lines.len() <= room,
                "{room} rows given, {} drawn",
                lines.len()
            );
        }
        assert!(acts_lines(&items, 32, 0, None).lines.is_empty());
    }

    #[test]
    fn the_legend_explains_a_marker_that_is_on_screen() {
        // Computed over the shown items, not the list: a `?` beyond the cap is
        // not a marker an operator can see, and a row explaining one is a row
        // taken from the board to say nothing.
        let refused = ActionItem {
            template_id: Some("desk_rebalance_review".into()),
            startable: Some(false),
            reason: Some(
                "creates a paper plan, which requires Propose mode; current mode is research"
                    .into(),
            ),
            ..ActionItem::default()
        };
        let items = [refused, two_row_item("regime_review", None)];
        let text = rendered(&acts_lines(&items, 32, 6, None).lines);
        assert!(
            !text.iter().any(|line| line.contains(NOT_RULED)),
            "a legend for a marker nothing drew: {text:?}"
        );
        assert!(text.iter().any(|line| line.contains("+1 more")), "{text:?}");
        // And it is there when the marker is.
        let text = rendered(&acts_lines(&items, 32, 12, None).lines);
        assert!(text.iter().any(|line| line.contains(NOT_RULED)), "{text:?}");
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_panel_names_exactly_the_proposals_it_drew() {
        // What `shown` is for: the approval path refuses an item this list
        // does not name, so a list longer than the panel would approve
        // something nobody could read, and a shorter one would refuse an item
        // sitting on screen.
        let items = [
            two_row_item("alpha", None),
            two_row_item("beta", None),
            two_row_item("gamma", None),
        ];
        // Header, two items, the note and the legend: room for one of three.
        let acts = acts_lines(&items, 32, 5, None);
        assert_eq!(shown(&acts), vec![("alpha", "t-alpha")]);
        let text = rendered(&acts.lines);
        assert!(text.iter().any(|line| line.contains("alpha")), "{text:?}");
        assert!(!text.iter().any(|line| line.contains("beta")), "{text:?}");
        // And a budget too small for the header's own row names nothing at
        // all, rather than naming what it would have drawn.
        assert!(acts_lines(&items, 32, 1, None).shown.is_empty());
        // An item the owner served no task for is drawn and **not** vouched
        // for: there is nothing to approve, so there is nothing to name.
        let taskless = [ActionItem {
            task_id: None,
            ..two_row_item("alpha", None)
        }];
        let acts = acts_lines(&taskless, 32, 12, None);
        assert!(acts.shown.is_empty(), "{:?}", acts.shown);
        assert!(rendered(&acts.lines).iter().any(|l| l.contains("alpha")));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_proposal_the_operator_asked_about_is_the_one_that_gets_drawn() {
        // The panel has no cursor, so this is what keeps an item beyond the
        // cap from being unapprovable: naming it on the command line puts it
        // at the top of the next frame, where it can be read.
        let items = [
            two_row_item("alpha", None),
            two_row_item("beta", None),
            two_row_item("gamma", None),
        ];
        let acts = acts_lines(&items, 32, 5, Some("gamma"));
        assert_eq!(shown(&acts), vec![("gamma", "t-gamma")]);
        let text = rendered(&acts.lines);
        assert!(text.iter().any(|line| line.contains("gamma")), "{text:?}");
        // Nothing else moves: the gate's own order holds for the rest, and a
        // name nothing answers to leaves the list exactly as it was.
        assert_eq!(
            shown(&acts_lines(&items, 32, 12, Some("gamma"))),
            vec![
                ("gamma", "t-gamma"),
                ("alpha", "t-alpha"),
                ("beta", "t-beta")
            ]
        );
        assert_eq!(
            acts_lines(&items, 32, 12, Some("nobody")).shown,
            acts_lines(&items, 32, 12, None).shown
        );
    }

    #[test]
    fn a_proposal_asked_for_and_still_unshown_is_named_by_the_note() {
        // The command line refuses an item the panel did not draw and asks for
        // it. When it *still* does not fit, that refusal would otherwise
        // repeat with nothing on screen to explain why — so the note says
        // which item needs the rows, in the row it was already spending.
        let items = [two_row_item("alpha", None), two_row_item("beta", None)];
        // Two rows: the header and the note. Nothing fits, `beta` least of all.
        let text = rendered(&acts_lines(&items, 32, 2, Some("beta")).lines);
        assert!(
            text.iter()
                .any(|line| line.contains("beta needs more rows")),
            "the panel did not say why the asked item is missing: {text:?}"
        );
        // And the ordinary note is unchanged when nothing was asked for.
        let text = rendered(&acts_lines(&items, 32, 2, None).lines);
        assert!(
            text.iter().any(|line| line.contains("+2 more, unshown")),
            "{text:?}"
        );
        // Nor when the asked item is the one that *did* fit: the note is about
        // what is missing, and naming a drawn item there would send an
        // operator looking for rows they do not need.
        // Five rows: the header, `alpha` whole, the note and the legend.
        let text = rendered(&acts_lines(&items, 32, 5, Some("alpha")).lines);
        assert!(
            text.iter().any(|line| line.contains("+1 more, unshown")),
            "{text:?}"
        );
    }

    /// Rendered lines as plain strings — what the operator would read.
    #[cfg(test)]
    fn rendered(lines: &[Line<'static>]) -> Vec<String> {
        lines
            .iter()
            .map(|line| line.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect()
    }

    #[test]
    fn the_wheel_scrolls_and_walls_at_both_ends() {
        let mut view = AtlasView::default();
        view.max_scroll.set(5);
        let mouse = |kind| MouseEvent {
            kind,
            column: 10,
            row: 10,
            modifiers: crossterm::event::KeyModifiers::NONE,
        };
        let mut store = Store::default();
        view.on_mouse(mouse(MouseEventKind::ScrollUp), &mut store);
        assert_eq!(view.offset, 3);
        view.on_mouse(mouse(MouseEventKind::ScrollUp), &mut store);
        assert_eq!(view.offset, 5, "the scroll ran past the oldest line");
        for _ in 0..3 {
            view.on_mouse(mouse(MouseEventKind::ScrollDown), &mut store);
        }
        assert_eq!(view.offset, 0, "the scroll ran past the newest line");
    }
}
