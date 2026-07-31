//! The slash-scoped command parser: text the operator types becomes a typed command or an error.
//!
//! The type it produces lands here first, because it is the seam that keeps
//! views out of the IO path: a view asks, the runtime acts. Nothing in `ui/`
//! may hold a client, so the only way a keystroke reaches the network is a
//! `Command` the runtime chose to honour.
//!
//! **The parser is the input model.** Mode is derived from the text on every
//! keystroke rather than held as a flag beside it: `/` is the picker because the
//! buffer starts with a slash and carries no space yet, and backspacing the
//! space out of `/ticker ` is the *same* rule running again, which is why it
//! reverts to the picker without anything having to remember that it should.
//! A flag would be a second account of the buffer, and the two would disagree
//! the first time an edit moved the text without going through the accessor
//! that set it. It is also why the field is a hand-rolled buffer rather than a
//! rich editor widget — a widget that owns the text hides the one thing this
//! model is a function of.
//!
//! Nothing here executes. `parse` is text in, state out; `resolve` is state and
//! desk facts in, a typed intent out. Applying that intent — moving a cursor,
//! switching a view, handing a `Command` to the runtime — belongs to the shell
//! and the runtime above it, and `tests/operator_gate.rs` greps this file to
//! keep it that way.

use crate::store::{Posture, Store, ViewId};
#[cfg(feature = "operator")]
use crate::ui::widgets::confirm::ConfirmToken;
use crossterm::event::{KeyCode, KeyEvent};

/// What a surface asks the runtime to do.
///
/// Deliberately small. Task 20 grows it with the parsed slash verbs; a variant
/// added before something dispatches it would be exactly the reachable-code-
/// with-no-caller shape this crate keeps tripping over.
///
/// The write verbs exist only under the `operator` feature, beside the client
/// that can serve them. A glass build has no writer for the runtime to dispatch
/// against, so a variant it could still construct would be a key path that
/// reaches a match arm and stops — the same shape, one layer up.
///
/// This file names none of the types on the far side of that seam, and
/// `tests/operator_gate.rs` greps it to keep that true: the pin is a plain text
/// search, deliberately, so it cannot be talked out of a match — which means
/// even a comment here may not spell them.
#[derive(Debug)]
pub enum Command {
    Quit,
    /// Jump the poll queue. A synchronous fetch from the event loop froze the
    /// client for the length of the request, so the refresh nudges the poller
    /// rather than doing the work itself.
    Refresh,
    /// Approve one pending approval request, by id.
    #[cfg(feature = "operator")]
    Approve(String),
    /// Reject one pending approval request, by id.
    #[cfg(feature = "operator")]
    Reject(String),
    /// Book the plan a human confirmed. The token is the confirmation itself —
    /// see `ui::widgets::confirm` — so this variant cannot be constructed by a
    /// key handler that never put the box on screen.
    #[cfg(feature = "operator")]
    Execute(ConfirmToken),
    /// Put a question to the desk manager.
    ///
    /// The one write verb on this workstation with no confirmation ritual in
    /// front of it, and deliberately so: it grants no authority. The owner
    /// records the message and answers only through the coordinator, so the
    /// worst a stray keystroke can do here is add a line to the audit log —
    /// where a confirmation box would instead teach an operator that the ritual
    /// is chrome, which is exactly what must not happen before the boxes that
    /// do guard a fill.
    #[cfg(feature = "operator")]
    Message(String),
    /// Start one registered workflow template against a goal.
    ///
    /// Two strings the operator chose from what the owner served — the template
    /// out of `/api/atlas/templates`, the goal typed into the picker. Neither
    /// carries authority either: the owner refuses a plan-creating template
    /// below `propose`, and a plan a run does produce still needs a persisted
    /// human approval before anything books.
    #[cfg(feature = "operator")]
    StartWorkflow {
        template: String,
        goal: String,
    },
    /// Point the desk at a data source and a book.
    ///
    /// Two strings rather than the one label an operator types, because the
    /// owner builds `DeskMode(data, book)` and refuses the pair it cannot make
    /// (`qlab/core/desk_mode.py`). The split happens in `resolve`, in front of
    /// the human, so this variant cannot carry a word nobody decided the half
    /// of. It authorises nothing on its own: which desk the client is pointed
    /// at is not permission to trade it, and every gate downstream is unmoved.
    #[cfg(feature = "operator")]
    DeskMode {
        data: String,
        book: String,
    },
}

/// Hand-written rather than derived, and neither `Eq` nor `Clone`.
///
/// `ConfirmToken` is deliberately not `Clone`: a confirmation is spent by the
/// call it authorises, and a derive here would have made the whole command
/// copyable and handed a caller a second booking for one human decision. Two
/// confirmations are never equal for the same reason — comparing them is only
/// ever a prelude to substituting one for the other — which also means `Command`
/// is not reflexive and therefore cannot be `Eq`.
impl PartialEq for Command {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Command::Quit, Command::Quit) => true,
            (Command::Refresh, Command::Refresh) => true,
            #[cfg(feature = "operator")]
            (Command::Approve(a), Command::Approve(b)) => a == b,
            #[cfg(feature = "operator")]
            (Command::Reject(a), Command::Reject(b)) => a == b,
            #[cfg(feature = "operator")]
            (Command::Execute(_), Command::Execute(_)) => false,
            #[cfg(feature = "operator")]
            (Command::Message(a), Command::Message(b)) => a == b,
            #[cfg(feature = "operator")]
            (
                Command::StartWorkflow {
                    template: a,
                    goal: x,
                },
                Command::StartWorkflow {
                    template: b,
                    goal: y,
                },
            ) => a == b && x == y,
            #[cfg(feature = "operator")]
            (Command::DeskMode { data: a, book: x }, Command::DeskMode { data: b, book: y }) => {
                a == b && x == y
            }
            _ => false,
        }
    }
}

// -- the grammar ------------------------------------------------------------

/// What the command line can be pointed at.
///
/// Four, and the two that are missing are the point: the plan's Part IV lists
/// `/halt` and `/resume`, and `qlab/ui/server.py` serves no HTTP route for
/// either — `set_halt` is reachable only from the MCP tools and the autopilot's
/// own kill switch (the gated writer module carries the same note). A scope
/// whose Enter key
/// reached a match arm and stopped is the caller-less shape this crate keeps
/// tripping over, one layer up from the `Command` variant it would need. They
/// return here when the owner grows the routes, not before.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    View,
    Ticker,
    Plan,
    /// Which data source and which book the desk is pointed at.
    ///
    /// The one scope that writes. It is in the grammar in both builds so the
    /// parser table is one table — a grammar that changed shape with a Cargo
    /// feature would be two grammars, and only one of them ever tested — while
    /// what it *resolves to* is gated with the writer, and what it offers is
    /// gated on the posture.
    Mode,
}

impl Scope {
    /// Picker order: the three a glass window can use, then the one it cannot.
    pub const ALL: [Scope; 4] = [Scope::View, Scope::Ticker, Scope::Plan, Scope::Mode];

    /// The word an operator types, and the word the suggestions show. One
    /// spelling, so the strip cannot offer something the parser will not accept.
    pub fn word(self) -> &'static str {
        match self {
            Scope::View => "view",
            Scope::Ticker => "ticker",
            Scope::Plan => "plan",
            Scope::Mode => "mode",
        }
    }

    /// What the scope takes, for the strip to say before anything is typed.
    pub fn hint(self) -> &'static str {
        match self {
            Scope::View => "a view, by the label the nav rail shows",
            Scope::Ticker => "a symbol this desk is watching",
            Scope::Plan => "a plan id, or enough of one to be unambiguous",
            Scope::Mode => "a data source and a book",
        }
    }

    /// Whether using this scope changes the desk. Offered only to a window that
    /// can, exactly as every other operator affordance on this workstation.
    pub fn writes(self) -> bool {
        matches!(self, Scope::Mode)
    }

    fn from_word(word: &str) -> Option<Scope> {
        Scope::ALL
            .into_iter()
            .find(|scope| scope.word().eq_ignore_ascii_case(word))
    }
}

/// The bare function-code grammar: what a token with no slash in front of it
/// means.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verb {
    /// An uppercase token short enough to be a symbol. Bloomberg's function
    /// codes, and every terminal since: `SPY` is a request, not prose.
    Ticker(String),
}

/// What the buffer means, right now.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CmdState {
    /// Nothing typed.
    Empty,
    /// A slash and the scope word so far, with the scopes that still answer it.
    ///
    /// A complete word with no space after it is *still* this: the space is the
    /// accept, which is what makes backspacing over it a revert.
    Picker { typed: String, matches: Vec<Scope> },
    /// A scope accepted, and the argument typed since.
    Scoped { scope: Scope, query: String },
    /// A complete request the grammar could read without a scope.
    Verb(Verb),
    /// Text that is not a command and cannot become one by typing more.
    Unknown(String),
}

/// The longest token the function-code grammar will read as a symbol.
///
/// Six because the blotter's own ticker column is six cells wide: a token this
/// grammar accepted and no column could render would be a selection an operator
/// cannot see. `BRK.B` and `SOXX` fit; a sentence does not.
const TICKER_MAX: usize = 6;

/// Text in, meaning out. No state, no clock, no desk.
///
/// The whole mode machine is this function: there is nowhere else for a mode to
/// be kept, so there is nothing to fall out of step with the text on screen.
pub fn parse(buf: &str) -> CmdState {
    let trimmed = buf.trim();
    if trimmed.is_empty() {
        return CmdState::Empty;
    }
    if let Some(rest) = buf.strip_prefix('/') {
        // The *first* space only. `/mode live alpaca` is one scope and one
        // argument of two words — splitting on every space would make the
        // grammar decide how many halves a desk mode has, which is the owner's
        // decision and not this client's.
        return match rest.split_once(' ') {
            Some((word, query)) => match Scope::from_word(word) {
                Some(scope) => CmdState::Scoped {
                    scope,
                    query: query.to_string(),
                },
                None => CmdState::Unknown(buf.to_string()),
            },
            None => CmdState::Picker {
                typed: rest.to_string(),
                matches: Scope::ALL
                    .into_iter()
                    .filter(|scope| starts_with_fold(scope.word(), rest))
                    .collect(),
            },
        };
    }
    if is_function_code(trimmed) {
        return CmdState::Verb(Verb::Ticker(trimmed.to_string()));
    }
    CmdState::Unknown(buf.to_string())
}

/// Whether a bare token is a symbol rather than prose.
///
/// Uppercase and short. Lowercase is deliberately *not* accepted: the line is
/// also where a human types words, and a grammar that read `spy` as a request
/// would make the two indistinguishable — the scoped form `/ticker spy` is
/// there for anyone who does not want to reach for shift.
fn is_function_code(token: &str) -> bool {
    let len = token.chars().count();
    len > 0
        && len <= TICKER_MAX
        && token.chars().any(|c| c.is_ascii_uppercase())
        && token
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '.' || c == '-')
}

/// Case-blind prefix match, safe for a prefix an operator pasted.
///
/// The boundary check is not decoration: `prefix` is whatever is in the buffer,
/// so a multi-byte character makes `haystack[..prefix.len()]` a slice through
/// the middle of one — a panic in the key path, which behind the alternate
/// screen is a crash this client cannot report. Every haystack here is ASCII
/// today; the guard is what keeps that from being load-bearing.
fn starts_with_fold(haystack: &str, prefix: &str) -> bool {
    haystack.len() >= prefix.len()
        && haystack.is_char_boundary(prefix.len())
        && haystack[..prefix.len()].eq_ignore_ascii_case(prefix)
}

// -- resolution -------------------------------------------------------------

/// What the line asks for, once the desk has been consulted.
///
/// Still not an action: every variant is a statement about what the operator
/// meant, and applying it is the shell's (a cursor, a view) or the runtime's (a
/// request). `Refused` is a first-class outcome rather than a `None`, because a
/// line that does nothing and says nothing is the hung-client reading this
/// workstation refuses everywhere else.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Resolved {
    /// Show this view.
    View(ViewId),
    /// Put every cursor that holds a symbol on this one. Uppercased, and known
    /// to be in the universe — a ticker this desk is not watching never gets
    /// this far.
    Ticker(String),
    /// The plan's full id. Whether it is on screen is the band's question, and
    /// the band belongs to BOOK.
    Plan(String),
    /// Point the desk somewhere. Only in a build that has a writer to carry it.
    #[cfg(feature = "operator")]
    Mode { data: String, book: String },
    /// The line cannot be acted on, and this is the sentence that says why.
    Refused(String),
}

/// The pairs the owner can build, in the order a desk widens through them.
///
/// Offered, not enforced: `DeskMode.__post_init__` refuses `synthetic` data on
/// the Alpaca book, and a second copy of that rule here would drift from the one
/// that decides. What an operator types is sent, and the owner's own refusal is
/// what comes back.
const DESK_MODES: [(&str, &str); 3] = [
    ("synthetic", "simulated"),
    ("live", "simulated"),
    ("live", "alpaca"),
];

/// One parsed line against one desk. Pure: the store is read, never touched.
pub fn resolve(state: &CmdState, store: &Store, posture: Posture) -> Resolved {
    match state {
        CmdState::Empty => Resolved::Refused("type / for the scopes".into()),
        // Counted as this window would *see* them: a glass operator typing `/m`
        // is offered nothing, and "choose a scope" beside an empty strip would
        // be an instruction they cannot follow. The scope they cannot use is
        // still answered when it is typed in full — that is `mode` below.
        CmdState::Picker { typed, matches } => {
            match matches
                .iter()
                .filter(|scope| !scope.writes() || posture.writes())
                .count()
            {
                0 => Resolved::Refused(format!("no scope is called {typed}")),
                // One match is an accept, and the shell does that rather than
                // acting: rewriting the buffer to `/view ` is what puts the
                // operator in front of the values before anything happens.
                _ => Resolved::Refused("choose a scope".into()),
            }
        }
        CmdState::Scoped { scope, query } => scoped(*scope, query.trim(), store, posture),
        CmdState::Verb(Verb::Ticker(symbol)) => ticker(symbol, store),
        CmdState::Unknown(text) => Resolved::Refused(format!(
            "{} is not a command — press / for the scopes",
            text.trim()
        )),
    }
}

fn scoped(scope: Scope, query: &str, store: &Store, posture: Posture) -> Resolved {
    match scope {
        Scope::View => view(query),
        Scope::Ticker => ticker(query, store),
        Scope::Plan => plan(query, store),
        Scope::Mode => mode(query, posture),
    }
}

fn view(query: &str) -> Resolved {
    if query.is_empty() {
        return Resolved::Refused("name a view — the strip below lists them".into());
    }
    // The labels the nav rail draws, and nothing else. An alias table would be
    // a second set of names for the seven views, and the rail is where an
    // operator reads them off.
    let hits: Vec<ViewId> = ViewId::ALL
        .into_iter()
        .filter(|id| starts_with_fold(id.label(), query))
        .collect();
    match hits.as_slice() {
        [id] => Resolved::View(*id),
        [] => Resolved::Refused(format!("no view is called {query}")),
        many => Resolved::Refused(format!(
            "{query} names {} views — type another character",
            many.len()
        )),
    }
}

fn ticker(query: &str, store: &Store) -> Resolved {
    if query.is_empty() {
        return Resolved::Refused("name a symbol — the strip below lists them".into());
    }
    let wanted = query.to_ascii_uppercase();
    // No phantom selection. A cursor moved to a symbol the desk is not watching
    // draws a row of `--` and reads as a measurement, which is the one thing a
    // selection may never be.
    //
    // Matched case-blind and returned in the *owner's* spelling. The owner's
    // universe is uppercase today, and a client that assumed so would tell an
    // operator their own symbol is not on a desk that is holding it — while a
    // cursor set from the typed spelling would then match no row at all.
    match store
        .universe()
        .into_iter()
        .find(|t| t.eq_ignore_ascii_case(&wanted))
    {
        Some(found) => Resolved::Ticker(found.to_string()),
        None => Resolved::Refused(format!(
            "{wanted} is not in the universe this desk is watching"
        )),
    }
}

fn plan(query: &str, store: &Store) -> Resolved {
    let wanted = query.to_ascii_lowercase();
    let hits: Vec<&str> = store
        .plans()
        .iter()
        .filter_map(|plan| crate::format::text(plan.plan_id.as_ref()))
        .filter(|id| id.to_ascii_lowercase().starts_with(&wanted))
        .collect();
    match hits.as_slice() {
        [id] => Resolved::Plan((*id).to_string()),
        [] if query.is_empty() => Resolved::Refused("the desk is holding no plan".into()),
        [] => Resolved::Refused(format!("no plan on this desk starts with {query}")),
        many => Resolved::Refused(format!(
            "{} plans start with that — type more of the id",
            many.len()
        )),
    }
}

fn mode(query: &str, posture: Posture) -> Resolved {
    // The posture, not the feature. A featured binary the human did not arm
    // reads GLASS on the status line, and this may not disagree with it.
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/mode changes the desk; this window is {} — start it with --operator",
            posture.label()
        ));
    }
    let mut words = query.split_whitespace();
    let (Some(data), Some(book)) = (words.next(), words.next()) else {
        return Resolved::Refused(
            "a desk mode is a data source and a book: /mode live simulated".into(),
        );
    };
    if words.next().is_some() {
        return Resolved::Refused("a desk mode is two words, not three".into());
    }
    #[cfg(feature = "operator")]
    return Resolved::Mode {
        data: data.to_string(),
        book: book.to_string(),
    };
    // Unreachable: `posture.writes()` above is false for every `Posture` this
    // build has. The arm exists because the function is total over the type,
    // and returning the refusal rather than a variant that does not exist here
    // is what keeps the grammar one grammar in both builds.
    #[cfg(not(feature = "operator"))]
    {
        let _ = (data, book);
        Resolved::Refused("this build has no write path".into())
    }
}

// -- the strip --------------------------------------------------------------

/// What the line could become from here — the one-line strip above the input.
///
/// A pure function of (state, desk, posture), like everything else on this
/// surface. Values come off the store rather than out of a list this file
/// keeps, so the strip can never offer a symbol the desk is not watching or a
/// plan the owner is not serving.
pub fn suggestions(state: &CmdState, store: &Store, posture: Posture) -> Vec<String> {
    match state {
        CmdState::Empty => scopes(posture),
        CmdState::Picker { matches, .. } => matches
            .iter()
            .filter(|scope| !scope.writes() || posture.writes())
            .map(|scope| format!("/{}", scope.word()))
            .collect(),
        CmdState::Scoped { scope, query } => values(*scope, query.trim(), store, posture),
        CmdState::Verb(Verb::Ticker(symbol)) => values(Scope::Ticker, symbol, store, posture),
        CmdState::Unknown(_) => Vec::new(),
    }
}

fn scopes(posture: Posture) -> Vec<String> {
    Scope::ALL
        .into_iter()
        .filter(|scope| !scope.writes() || posture.writes())
        .map(|scope| format!("/{}", scope.word()))
        .collect()
}

fn values(scope: Scope, query: &str, store: &Store, posture: Posture) -> Vec<String> {
    match scope {
        Scope::View => ViewId::ALL
            .into_iter()
            .filter(|id| starts_with_fold(id.label(), query))
            .map(|id| id.label().to_string())
            .collect(),
        Scope::Ticker => {
            let wanted = query.to_ascii_uppercase();
            store
                .universe()
                .into_iter()
                .filter(|t| starts_with_fold(t, &wanted))
                .map(str::to_string)
                .collect()
        }
        Scope::Plan => {
            let wanted = query.to_ascii_lowercase();
            store
                .plans()
                .iter()
                .filter_map(|plan| crate::format::text(plan.plan_id.as_ref()))
                .filter(|id| id.to_ascii_lowercase().starts_with(&wanted))
                .map(str::to_string)
                .collect()
        }
        // Hidden rather than shown-and-refused: an operator affordance is
        // absent from an unarmed window everywhere else on this workstation,
        // and a strip that offered one would teach that the posture is chrome.
        Scope::Mode if !posture.writes() => Vec::new(),
        Scope::Mode => DESK_MODES
            .into_iter()
            .map(|(data, book)| format!("{data} {book}"))
            .filter(|pair| starts_with_fold(pair, query))
            .collect(),
    }
}

// -- the field --------------------------------------------------------------

/// How many submitted lines the field remembers.
///
/// In memory only, and gone with the process: a command history on disk is a
/// record of what an operator did, which is the registry's job and not a
/// client's. Bounded because nothing else bounds it.
const HISTORY_MAX: usize = 50;

/// The hand-rolled input: a buffer, a cursor, and what has been submitted.
///
/// Hand-rolled rather than a text-area widget, per the plan: the parser is the
/// input model, and it needs the raw buffer on every keystroke. A widget that
/// owned the text would hide exactly that, and the mode would have to be
/// remembered beside it.
///
/// No mode field, deliberately. Whether this is a picker or a scoped line is
/// `parse(self.text())`, always.
#[derive(Debug, Default)]
pub struct CmdLine {
    buf: String,
    /// A *character* index, not a byte one. A pasted `régime` would panic a
    /// byte slice, and a panic in the key path is a crash behind the alternate
    /// screen — the one failure a fullscreen client cannot report.
    cursor: usize,
    /// Submitted lines, oldest first.
    history: Vec<String>,
    /// Where ↑/↓ has walked to. `None` is the live line.
    at: Option<usize>,
    /// What the line last said back: a refusal, or what it did. Retired by the
    /// next edit, because an answer beside a line the operator has since
    /// changed is an answer to a question they are no longer asking.
    note: Option<String>,
}

/// What one keystroke asks of the surface above the field.
///
/// The field edits itself; anything that needs the desk — accepting a
/// suggestion, acting on the line — is a decision the shell makes, because the
/// suggestions and the store are its to read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edit {
    /// The buffer changed, and nothing else has to happen.
    Idle,
    /// The operator abandoned the line.
    Close,
    /// The operator asked for the line to be acted on.
    Submit,
    /// The operator asked for the first suggestion.
    Accept,
}

impl CmdLine {
    /// One keystroke into the field.
    ///
    /// Every printable key is the field's while it has focus, including the
    /// ones the shell claims for the whole workstation — `q` and the digits are
    /// characters a symbol needs. Ctrl-C is exempt above this, in the shell,
    /// because the reflex every operator has must work even here.
    pub fn edit(&mut self, k: KeyEvent) -> Edit {
        match k.code {
            KeyCode::Esc => return Edit::Close,
            KeyCode::Enter => return Edit::Submit,
            KeyCode::Tab => return Edit::Accept,
            KeyCode::Up => self.back(),
            KeyCode::Down => self.forward(),
            KeyCode::Left => self.left(),
            KeyCode::Right => self.right(),
            KeyCode::Backspace => self.backspace(),
            KeyCode::Char(c) => self.insert(c),
            _ => {}
        }
        Edit::Idle
    }

    pub fn text(&self) -> &str {
        &self.buf
    }

    /// The caret's position, in characters from the start.
    pub fn cursor(&self) -> usize {
        self.cursor
    }

    pub fn note(&self) -> Option<&str> {
        self.note.as_deref()
    }

    /// Say something back. Whatever the line last said, until the next edit.
    pub fn say(&mut self, note: String) {
        self.note = Some(note);
    }

    pub fn insert(&mut self, c: char) {
        let at = self.byte_at(self.cursor);
        self.buf.insert(at, c);
        self.cursor += 1;
        self.touched();
    }

    pub fn backspace(&mut self) {
        if self.cursor == 0 {
            return;
        }
        let at = self.byte_at(self.cursor - 1);
        self.buf.remove(at);
        self.cursor -= 1;
        self.touched();
    }

    pub fn left(&mut self) {
        self.cursor = self.cursor.saturating_sub(1);
    }

    pub fn right(&mut self) {
        self.cursor = (self.cursor + 1).min(self.len());
    }

    /// Replace what has been typed with a suggestion.
    ///
    /// A scope brings its trailing space with it — that space *is* the accept,
    /// and handing it back without one would leave the line in the picker it
    /// just left. A value replaces only the argument, so the scope word stays.
    pub fn accept(&mut self, choice: &str) {
        let text = match choice.strip_prefix('/') {
            Some(word) => format!("/{word} "),
            None => match self.buf.find(' ') {
                Some(at) => format!("{}{choice}", &self.buf[..=at]),
                None => choice.to_string(),
            },
        };
        self.set(text);
    }

    /// Record this line and empty the field.
    ///
    /// Only a line that was acted on. A refusal leaves the text in place for
    /// the operator to fix, and never reaches the history — recalling a line
    /// the desk already declined would offer the same mistake back as a
    /// suggestion.
    pub fn submitted(&mut self) {
        let line = self.buf.trim().to_string();
        if !line.is_empty() && self.history.last() != Some(&line) {
            self.history.push(line);
            while self.history.len() > HISTORY_MAX {
                self.history.remove(0);
            }
        }
        self.clear();
    }

    /// Empty the field without recording anything.
    pub fn clear(&mut self) {
        self.set(String::new());
        self.note = None;
    }

    /// One line older.
    pub fn back(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let at = match self.at {
            None => self.history.len() - 1,
            // A wall, not a wrap: an operator holding ↑ must land on the oldest
            // line, never at the other end of a history they did not walk to.
            Some(0) => 0,
            Some(i) => i - 1,
        };
        // Set after `set`, not before: `set` puts the field back on the live
        // line — which is what every *edit* wants and is exactly wrong here,
        // because a recall that forgot where it was would restart at the newest
        // line on the next ↑.
        let line = self.history[at].clone();
        self.set(line);
        self.at = Some(at);
    }

    /// One line newer, and past the newest is the empty line again.
    pub fn forward(&mut self) {
        let Some(at) = self.at else { return };
        match self.history.get(at + 1) {
            Some(line) => {
                let line = line.clone();
                self.set(line);
                self.at = Some(at + 1);
            }
            None => self.set(String::new()),
        }
    }

    fn set(&mut self, text: String) {
        self.buf = text;
        self.cursor = self.len();
        self.at = None;
        self.note = None;
    }

    /// An edit retires whatever the line last said, and steps off the history.
    fn touched(&mut self) {
        self.at = None;
        self.note = None;
    }

    fn len(&self) -> usize {
        self.buf.chars().count()
    }

    /// The byte offset of character `index`, clamped to the end.
    fn byte_at(&self, index: usize) -> usize {
        self.buf
            .char_indices()
            .nth(index)
            .map_or(self.buf.len(), |(at, _)| at)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bus::AppEvent;
    use std::time::Instant;

    /// A desk carrying three market assets, a book of two, and two plans.
    fn desk() -> Store {
        let mut store = Store::default();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({
                    "market": {"assets": [
                        {"ticker": "SPY", "price": 729.46, "history": [1.0, 2.0]},
                        {"ticker": "QQQ", "price": 661.73, "history": [1.0, 2.0]},
                        {"ticker": "SOXX", "price": 301.10, "history": [1.0, 2.0]}
                    ]},
                    "live_portfolio": {"positions": [
                        {"ticker": "SPY", "weight": 0.6},
                        {"ticker": "TLT", "weight": 0.4}
                    ]},
                    "plans": [
                        {"plan_id": "9f3ac1d20b7e4a51", "state": "checked"},
                        {"plan_id": "0c17b8e4419d2f60", "state": "superseded"}
                    ]
                }))
                .unwrap(),
            )),
            Instant::now(),
        );
        store
    }

    fn picker_of(state: &CmdState) -> Vec<Scope> {
        match state {
            CmdState::Picker { matches, .. } => matches.clone(),
            other => panic!("expected a picker, got {other:?}"),
        }
    }

    // -- the table --------------------------------------------------------

    #[test]
    fn the_mode_is_derived_from_the_text_and_from_nothing_else() {
        // The whole grammar, as a table. Each row is a buffer an operator could
        // have typed and the only reading of it — no flag, no history, nothing
        // held between keystrokes.
        let cases: Vec<(&str, CmdState)> = vec![
            ("", CmdState::Empty),
            ("   ", CmdState::Empty),
            // A bare slash is every scope offered.
            (
                "/",
                CmdState::Picker {
                    typed: String::new(),
                    matches: Scope::ALL.to_vec(),
                },
            ),
            // Filtered as it is typed.
            (
                "/v",
                CmdState::Picker {
                    typed: "v".into(),
                    matches: vec![Scope::View],
                },
            ),
            (
                "/ti",
                CmdState::Picker {
                    typed: "ti".into(),
                    matches: vec![Scope::Ticker],
                },
            ),
            // A complete scope word with no space is *still* the picker: the
            // space is what accepts it, and this is the state the backspace
            // below has to land back in.
            (
                "/view",
                CmdState::Picker {
                    typed: "view".into(),
                    matches: vec![Scope::View],
                },
            ),
            // The trailing space is the accept.
            (
                "/view ",
                CmdState::Scoped {
                    scope: Scope::View,
                    query: String::new(),
                },
            ),
            (
                "/view mk",
                CmdState::Scoped {
                    scope: Scope::View,
                    query: "mk".into(),
                },
            ),
            (
                "/ticker SP",
                CmdState::Scoped {
                    scope: Scope::Ticker,
                    query: "SP".into(),
                },
            ),
            // Only the first space splits: the rest is the argument, whole.
            (
                "/mode live alpaca",
                CmdState::Scoped {
                    scope: Scope::Mode,
                    query: "live alpaca".into(),
                },
            ),
            // The scope word is matched case-blind; the argument is not touched.
            (
                "/VIEW BooK",
                CmdState::Scoped {
                    scope: Scope::View,
                    query: "BooK".into(),
                },
            ),
            // A prefix nothing answers is an empty picker, not a scope.
            (
                "/zz",
                CmdState::Picker {
                    typed: "zz".into(),
                    matches: Vec::new(),
                },
            ),
            // Once accepted, an unknown word is a line that cannot become one.
            ("/zz x", CmdState::Unknown("/zz x".into())),
            // The function-code grammar: a bare uppercase token is a ticker.
            ("SPY", CmdState::Verb(Verb::Ticker("SPY".into()))),
            ("BRK.B", CmdState::Verb(Verb::Ticker("BRK.B".into()))),
            // Lowercase is prose, not a function code.
            ("spy", CmdState::Unknown("spy".into())),
            // Seven cells is past what a ticker column can hold.
            ("TOOLONG", CmdState::Unknown("TOOLONG".into())),
            // Two words are a sentence, not a code.
            ("SPY NOW", CmdState::Unknown("SPY NOW".into())),
        ];
        for (buf, want) in cases {
            assert_eq!(parse(buf), want, "parsing {buf:?}");
        }
    }

    #[test]
    fn backspacing_past_the_space_reverts_to_the_picker() {
        // The revert is not a special case in the editor — the editor only
        // removes a character. The mode moves because the *text* moved, which
        // is the whole reason mode is derived rather than held.
        let mut line = CmdLine::default();
        for c in "/view ".chars() {
            line.insert(c);
        }
        assert!(matches!(
            parse(line.text()),
            CmdState::Scoped {
                scope: Scope::View,
                ..
            }
        ));
        line.backspace();
        assert_eq!(line.text(), "/view");
        assert_eq!(picker_of(&parse(line.text())), vec![Scope::View]);
        // And all the way back out again: four more leave the bare slash, which
        // is every scope offered, and the fifth leaves nothing at all.
        for _ in 0..4 {
            line.backspace();
        }
        assert_eq!(line.text(), "/");
        assert_eq!(picker_of(&parse(line.text())), Scope::ALL.to_vec());
        line.backspace();
        assert_eq!(parse(line.text()), CmdState::Empty);
    }

    #[test]
    fn accepting_a_scope_rewrites_the_buffer_with_its_trailing_space() {
        let mut line = CmdLine::default();
        line.insert('/');
        line.insert('t');
        line.accept("/ticker");
        assert_eq!(line.text(), "/ticker ");
        assert_eq!(line.cursor(), "/ticker ".chars().count());
        // A value accepted inside a scope keeps the scope word.
        line.accept("SPY");
        assert_eq!(line.text(), "/ticker SPY");
    }

    // -- the editor -------------------------------------------------------

    #[test]
    fn the_cursor_edits_where_it_sits_and_counts_cells_not_bytes() {
        let mut line = CmdLine::default();
        for c in "/ticker SPY".chars() {
            line.insert(c);
        }
        line.left();
        line.left();
        line.insert('X');
        assert_eq!(line.text(), "/ticker SXPY");
        line.backspace();
        assert_eq!(line.text(), "/ticker SPY");
        // A pasted multi-byte character must not panic a slice.
        let mut wide = CmdLine::default();
        for c in "/ticker régime".chars() {
            wide.insert(c);
        }
        wide.left();
        wide.backspace();
        assert_eq!(wide.text(), "/ticker régie");
    }

    #[test]
    fn the_history_recalls_what_was_submitted_and_only_that() {
        let mut line = CmdLine::default();
        for text in ["/view book", "/ticker SPY"] {
            for c in text.chars() {
                line.insert(c);
            }
            line.submitted();
        }
        assert_eq!(line.text(), "", "a submitted line leaves the field empty");
        line.back();
        assert_eq!(line.text(), "/ticker SPY");
        line.back();
        assert_eq!(line.text(), "/view book");
        line.back();
        assert_eq!(line.text(), "/view book", "the far end is a wall");
        line.forward();
        assert_eq!(line.text(), "/ticker SPY");
        line.forward();
        assert_eq!(line.text(), "", "forward past the newest is the live line");
    }

    #[test]
    fn a_line_that_was_never_submitted_is_never_recalled() {
        // A refusal does not go on the record: recalling "/ticker ZZZZ" after
        // the desk said it is not in the universe would offer the operator the
        // same mistake as a suggestion.
        let mut line = CmdLine::default();
        for c in "/ticker ZZZZ".chars() {
            line.insert(c);
        }
        line.clear();
        line.back();
        assert_eq!(line.text(), "");
    }

    // -- resolution -------------------------------------------------------

    #[test]
    fn a_view_resolves_by_the_label_the_nav_rail_shows() {
        let store = desk();
        for (query, want) in [
            ("book", ViewId::Book),
            ("BOOK", ViewId::Book),
            ("bo", ViewId::Book),
            ("mk", ViewId::Markets),
            ("audit", ViewId::Audit),
        ] {
            assert_eq!(
                resolve(&parse(&format!("/view {query}")), &store, Posture::Glass),
                Resolved::View(want),
                "{query}"
            );
        }
    }

    #[test]
    fn a_view_nothing_answers_says_so_rather_than_jumping_somewhere() {
        let store = desk();
        let refusal = resolve(&parse("/view zz"), &store, Posture::Glass);
        match refusal {
            Resolved::Refused(said) => assert!(said.contains("zz"), "{said}"),
            other => panic!("{other:?}"),
        }
        // An empty scope is a prompt, not an error — the strip is already
        // listing every view.
        assert!(matches!(
            resolve(&parse("/view "), &store, Posture::Glass),
            Resolved::Refused(_)
        ));
    }

    #[test]
    fn a_ticker_outside_the_universe_is_named_rather_than_selected() {
        // The FinceptTerminal rule: no phantom selection. A client that moved a
        // cursor to a symbol the desk is not watching would show an empty row
        // as a reading.
        let store = desk();
        assert_eq!(
            resolve(&parse("/ticker SPY"), &store, Posture::Glass),
            Resolved::Ticker("SPY".into())
        );
        // The book counts as the universe too: TLT is held but unquoted.
        assert_eq!(
            resolve(&parse("/ticker tlt"), &store, Posture::Glass),
            Resolved::Ticker("TLT".into())
        );
        match resolve(&parse("/ticker ZZZZ"), &store, Posture::Glass) {
            Resolved::Refused(said) => {
                assert!(said.contains("ZZZZ"), "{said}");
                assert!(said.contains("universe"), "{said}");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn a_symbol_comes_back_in_the_owners_spelling_and_not_the_operators() {
        // A cursor is set by comparing against the owner's own rows, so a
        // client that handed back what was typed would select nothing on a desk
        // whose universe is not uppercase.
        let mut store = desk();
        let mut snapshot = store.snapshot.take().unwrap();
        snapshot.market.as_mut().unwrap().assets[0].ticker = Some("spy".into());
        store.apply(AppEvent::Snapshot(Box::new(snapshot)), Instant::now());
        assert_eq!(
            resolve(&parse("/ticker SPY"), &store, Posture::Glass),
            Resolved::Ticker("spy".into())
        );
    }

    #[test]
    fn a_prefix_the_operator_pasted_cannot_panic_the_key_path() {
        // Byte-sliced, this is a slice through the middle of a character —
        // a panic behind the alternate screen, which is the one failure a
        // fullscreen client cannot report.
        let store = desk();
        for pasted in ["/vé", "/ticker é", "/plan ré", "é"] {
            let _ = resolve(&parse(pasted), &store, Posture::Glass);
            let _ = suggestions(&parse(pasted), &store, Posture::Glass);
        }
    }

    #[test]
    fn a_scope_this_window_cannot_see_is_not_a_scope_it_is_told_to_choose() {
        // On glass `/m` matches only the hidden scope, so the strip is empty.
        // "choose a scope" beside an empty strip is an instruction with nothing
        // to follow — the honest answer is that no scope answers to `m`.
        let store = desk();
        assert!(suggestions(&parse("/m"), &store, Posture::Glass).is_empty());
        match resolve(&parse("/m"), &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(said.contains("no scope"), "{said}"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn the_bare_function_code_resolves_exactly_as_the_scoped_form() {
        let store = desk();
        assert_eq!(
            resolve(&parse("QQQ"), &store, Posture::Glass),
            resolve(&parse("/ticker QQQ"), &store, Posture::Glass),
        );
    }

    #[test]
    fn a_plan_resolves_by_prefix_and_an_ambiguous_one_refuses() {
        let store = desk();
        assert_eq!(
            resolve(&parse("/plan 9f3a"), &store, Posture::Glass),
            Resolved::Plan("9f3ac1d20b7e4a51".into())
        );
        assert_eq!(
            resolve(&parse("/plan 9f3ac1d20b7e4a51"), &store, Posture::Glass),
            Resolved::Plan("9f3ac1d20b7e4a51".into())
        );
        match resolve(&parse("/plan nope"), &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(said.contains("nope"), "{said}"),
            other => panic!("{other:?}"),
        }
        // Two plans answer an empty prefix, and picking one for the operator
        // would be this client choosing which trade they meant.
        match resolve(&parse("/plan "), &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(!said.is_empty(), "{said}"),
            other => panic!("{other:?}"),
        }
    }

    // -- the write scope --------------------------------------------------

    /// Arity is checked *behind* the posture: an unarmed window is refused for
    /// being unarmed whatever it typed, which is why this leg is armed.
    #[cfg(feature = "operator")]
    #[test]
    fn a_desk_mode_needs_both_halves_because_the_owner_builds_both() {
        let store = desk();
        for short in ["/mode ", "/mode live"] {
            match resolve(&parse(short), &store, Posture::Operator) {
                Resolved::Refused(said) => assert!(said.contains("book"), "{short}: {said}"),
                other => panic!("{short}: {other:?}"),
            }
        }
        match resolve(
            &parse("/mode live simulated extra"),
            &store,
            Posture::Operator,
        ) {
            Resolved::Refused(said) => assert!(said.contains("two words"), "{said}"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn an_unarmed_window_refuses_the_write_scope_rather_than_ignoring_it() {
        // Hidden from the suggestions, but a key with no visible effect reads as
        // a hung client — so a line typed blind is answered, not swallowed.
        let store = desk();
        match resolve(&parse("/mode live simulated"), &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(said.contains("GLASS"), "{said}"),
            other => panic!("{other:?}"),
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn an_armed_window_resolves_a_desk_mode_into_its_two_halves() {
        let store = desk();
        assert_eq!(
            resolve(&parse("/mode live alpaca"), &store, Posture::Operator),
            Resolved::Mode {
                data: "live".into(),
                book: "alpaca".into()
            }
        );
        // The pair the owner forbids is still sent: the owner is the authority
        // on its own desk mode, and a second copy of that rule here would drift
        // from the one that decides.
        assert_eq!(
            resolve(&parse("/mode synthetic alpaca"), &store, Posture::Operator),
            Resolved::Mode {
                data: "synthetic".into(),
                book: "alpaca".into()
            }
        );
    }

    // -- the strip --------------------------------------------------------

    #[test]
    fn the_picker_offers_the_write_scope_only_to_a_window_that_can_use_it() {
        let store = desk();
        let glass = suggestions(&parse("/"), &store, Posture::Glass);
        assert!(glass.contains(&"/view".to_string()), "{glass:?}");
        assert!(glass.contains(&"/ticker".to_string()), "{glass:?}");
        assert!(glass.contains(&"/plan".to_string()), "{glass:?}");
        assert!(
            !glass.contains(&"/mode".to_string()),
            "an operator affordance is hidden on glass: {glass:?}"
        );
        // And the two scopes the owner serves no route for are absent from the
        // grammar entirely — see `Scope::ALL`.
        for absent in ["/halt", "/resume"] {
            assert!(!glass.iter().any(|s| s == absent), "{glass:?}");
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn an_armed_picker_offers_the_write_scope() {
        let store = desk();
        let armed = suggestions(&parse("/"), &store, Posture::Operator);
        assert!(armed.contains(&"/mode".to_string()), "{armed:?}");
    }

    #[test]
    fn the_strip_offers_what_the_desk_actually_holds() {
        let store = desk();
        assert_eq!(
            suggestions(&parse("/ticker S"), &store, Posture::Glass),
            vec!["SPY".to_string(), "SOXX".to_string()]
        );
        // The book's own holding is offered too, after the quoted universe.
        assert!(
            suggestions(&parse("/ticker "), &store, Posture::Glass).contains(&"TLT".to_string())
        );
        assert_eq!(
            suggestions(&parse("/view b"), &store, Posture::Glass),
            vec!["BOOK".to_string()]
        );
        assert_eq!(
            suggestions(&parse("/plan 9"), &store, Posture::Glass),
            vec!["9f3ac1d20b7e4a51".to_string()]
        );
        assert!(suggestions(&parse("/mode "), &store, Posture::Glass).is_empty());
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_mode_strip_offers_the_pairs_the_owner_can_make() {
        let store = desk();
        let pairs = suggestions(&parse("/mode "), &store, Posture::Operator);
        assert_eq!(
            pairs,
            vec![
                "synthetic simulated".to_string(),
                "live simulated".to_string(),
                "live alpaca".to_string()
            ]
        );
    }
}
