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
use crate::ui::widgets::confirm::{BookToken, ConfirmToken};
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
    /// A palette line typed somewhere other than the palette — the ATLAS
    /// chat box, where `/approve …` or `/clear` lands beside the questions.
    /// The runtime hands it to the shell's own resolver, so the chat gains
    /// every scope the palette has and never a second grammar.
    RunLine(String),
    /// Approve one pending approval request, by id.
    #[cfg(feature = "operator")]
    Approve(String),
    /// Open an approval request for a checked plan that has none. The desk's
    /// tick opens one itself; this is the operator asking first. Grants
    /// nothing — a request is a question, and the confirm box that follows
    /// is the answer.
    #[cfg(feature = "operator")]
    RequestApproval(String),
    /// Reject one pending approval request, by id.
    #[cfg(feature = "operator")]
    Reject(String),
    /// Book the plan a human confirmed. The token is the confirmation itself —
    /// see `ui::widgets::confirm` — so this variant cannot be constructed by a
    /// key handler that never put the box on screen.
    #[cfg(feature = "operator")]
    Execute(ConfirmToken),
    /// Book the desk's current proposal, in one confirmed call.
    ///
    /// The token is the confirmation itself — see `ui::widgets::confirm` — so
    /// this variant, like `Execute`, cannot be constructed by a key handler
    /// that never put the box on screen. It carries no approval id, because
    /// the owner's route resolves the current proposal itself and refuses a
    /// plan that is not it: naming one here would be this client choosing
    /// which question it is answering.
    #[cfg(feature = "operator")]
    Book(BookToken),
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
    /// Store an Alpaca paper login the operator typed.
    ///
    /// The two values are `Secret`s, so this variant — which `Command` derives
    /// `Debug` for, and which the dispatcher hands to a tracing-capable layer —
    /// cannot print what it carries. See `crate::secret`.
    ///
    /// `replace` is consent and nothing else. It is `false` on every login the
    /// operator has not been asked about, and `true` only on the re-send that
    /// follows a confirmable refusal the owner raised for this exact pair: the
    /// owner destroys a stored browser login on that flag, and the flag is
    /// therefore never a default and never a retry's convenience.
    ///
    /// It authorises no trade. A stored credential makes the Alpaca book
    /// *choosable*; the desk is switched by `/mode`, every gate downstream is
    /// unmoved, and a fill still needs a persisted approval and a typed hash.
    #[cfg(feature = "operator")]
    AlpacaLogin {
        key: crate::secret::Secret,
        secret: crate::secret::Secret,
        replace: bool,
    },
    /// Ask the owner to put the stored login to the venue. Reads nothing back
    /// into the desk and changes nothing — the answer is a sentence.
    #[cfg(feature = "operator")]
    TestAlpaca,
    /// Point one surface at a model, or switch the reasoner.
    ///
    /// It grants no authority and books nothing: which mind answers a question
    /// is not permission to trade on the answer. Every gate downstream is
    /// unmoved — the referee is pinned to claude in the owner's own routing
    /// whatever this says, and a fill still needs a persisted approval and a
    /// typed hash.
    ///
    /// The surface is a word out of this client's own grammar; the choice is
    /// whatever the operator named, and the owner validates it. See
    /// [`ModelChoice`].
    #[cfg(feature = "operator")]
    SetLlm {
        surface: String,
        choice: ModelChoice,
    },
    /// Answer the desk's arming question: may a window like this one write?
    ///
    /// The one write verb a window the desk has *not* armed may still send,
    /// and the reason the dispatch seam carries an exemption for it: every
    /// window that can answer this question is by construction one the desk
    /// has not armed, so a chokepoint with no exception would make the
    /// question unanswerable and the posture unreachable.
    ///
    /// It grants nothing by itself. The owner records the answer and this
    /// window widens only when a later snapshot says the desk is armed —
    /// which is what keeps the posture the owner's fact rather than a latch
    /// this client sets on its own keystroke. Every gate downstream is
    /// unmoved: an armed window still needs a persisted approval and a typed
    /// hash before anything books.
    #[cfg(feature = "operator")]
    Posture {
        armed: bool,
    },
    /// Start the proposal a human approved, by the id of the task the owner
    /// bound to it.
    ///
    /// **The task id, never the template id.** The owner mints one task per
    /// proposal and `/api/atlas/tasks/<id>/start` re-runs `check_startable`
    /// against it; a client that named the template instead would be asking
    /// the owner to find "some task like this", which is how one keystroke
    /// starts a proposal a different day already spent.
    ///
    /// It carries no authority of its own. The route is the same one that
    /// refuses on mode, on the retry budget, and on a task that is no longer
    /// queued — and it cannot create a paper plan below `propose`, whatever
    /// this asks for. What it *does* carry is the human's decision, which the
    /// owner records as an `atlas_proposal_approved` row before it starts
    /// anything: the beat passes over proposal-origin tasks, so this path is
    /// the only way one runs at all.
    #[cfg(feature = "operator")]
    ApproveAction(String),
    /// Ask the desk what it would do, and let it write the proposals down.
    ///
    /// **The one caller of `POST /api/atlas/actionables`, and the reason that
    /// route is not dead.** Nothing else in this workstation asks: the panel
    /// reads a snapshot block that is *composed from what an ask already
    /// persisted*, so with no ask the desk mints no proposal, the panel draws
    /// nothing, and `/do` answers "not a proposal this desk is offering"
    /// forever.
    ///
    /// A write, and gated as one. It mints `proposal`-origin task rows, so it
    /// travels through the runtime's one write chokepoint like every other and an
    /// unarmed or `--glass` window cannot ask — which is correct: a read-only
    /// window may look at what somebody else asked for and may not put new
    /// rows in the desk's queue.
    ///
    /// **A keystroke, never a timer.** `atlas_actionables` calls `atlas_facts`,
    /// and `_atlas_regime_facts` latches the regime it saw — so a poll or a
    /// per-frame ask would consume a regime flip before the owner's own
    /// observe tick saw it and silently suppress the desk's `regime_flip`
    /// trigger. Once per press, because the operator asked.
    ///
    /// It grants nothing. Every item comes back gate-checked and is checked
    /// *again* by `start_task` when one is approved; asking in Research
    /// cannot produce an item that creates a paper plan.
    #[cfg(feature = "operator")]
    Actionables,
    /// Choose which sources the desk reads its news from.
    ///
    /// **It widens what the desk reads and never what it can do.** The owner's
    /// route writes `.env` and the process environment; it takes no registry
    /// lock and touches no plan, approval or posture, and every gate between a
    /// plan and a fill is unmoved by it. Naming a wire feed here is not
    /// permission to trade on what it says.
    ///
    /// `providers` is whatever the operator ticked, sent whole: the owner owns
    /// the catalog and refuses a name it does not know, a source whose
    /// credential does not resolve, and an empty list — an explicit "no real
    /// sources" is `["synthetic"]`, exactly as its wizard writes it.
    ///
    /// `contact` is an identity the SEC asks EDGAR callers to send rather than
    /// a credential, so it is not a [`crate::secret::Secret`] and there is no
    /// `expose` on it. It is still a newtype and not a `String`, for the one
    /// reason [`Contact`] gives: this enum derives `Debug`, a derive prints
    /// every field of every variant, and a claim that the value lives in two
    /// places is not a claim while a `{:?}` anywhere can falsify it.
    #[cfg(feature = "operator")]
    NewsSettings {
        providers: Vec<String>,
        contact: Option<Contact>,
        /// Whether to ask the owner to fetch one live window per member before
        /// reporting. Off by default: it is minutes with `gdelt` chosen.
        verify: bool,
        /// The lane this window is pointed at, so the owner answers about the
        /// desk being read rather than about its own default.
        offline: bool,
    },
    /// Choose the operational method this desk solves with, or the number of
    /// names it may hold.
    ///
    /// **It changes what the desk *proposes*, never what it may book.** The
    /// owner's route merges one of two keys into the mandate and answers with
    /// the merged pair; it opens no plan, touches no approval and moves no
    /// posture, and every gate between a plan and a fill is unmoved by it. What
    /// it *can* do is make the next solve refuse — a cap below what the chosen
    /// method holds is a warning here and a refused plan later — which is why
    /// the owner's warning is carried back rather than dropped.
    ///
    /// One key per command, because the card sends one decision at a time and
    /// the owner reports one `mandate_override` audit row per changed field: a
    /// command that could carry both would let one keystroke produce two rows
    /// nobody can tell apart afterwards.
    #[cfg(feature = "operator")]
    SetMethod(MethodChange),
    /// Grant or withdraw one of the three authorities the operator lends Atlas.
    ///
    /// **It changes what Atlas is offered, never what the owner will accept.**
    /// Rights are an operator's stated intent, exactly like the desk posture:
    /// two of the three (`web`, `build`) only shape the tool grant a chat or a
    /// hand-off is launched with, and the owner enforces neither. It opens no
    /// plan, touches no approval and moves no posture, and every gate between a
    /// plan and a fill is unmoved by it.
    ///
    /// One field per command, because the owner records one
    /// `desk.rights_changed` audit row per changed field: a command that could
    /// carry two would put two decisions behind one keystroke, which is the
    /// reason [`MethodChange`] carries one key too.
    #[cfg(feature = "operator")]
    SetRight {
        field: Right,
        value: bool,
    },
    /// Withdraw the standing grant the owner is holding.
    ///
    /// **The one write on this workstation that only ever narrows what can
    /// happen next.** Every other command here asks the desk to do something;
    /// this one asks it to stop. That asymmetry is why it is reachable in a
    /// single keystroke with no typed challenge — the hash-bound BOOK box
    /// exists to put a human between an unchecked plan and a fill, and a box
    /// between an operator and "stop" would delay the only action that can
    /// never make things worse.
    ///
    /// It carries a reason and **no grant id**: the owner holds one live grant
    /// and is the only thing that knows which, and a client that named one
    /// could revoke a grant it read seconds ago rather than the one that is
    /// live now. Revoking whatever stands is the safe reading of a stale card.
    ///
    /// It creates nothing. There is no `GrantAuthority` beside it, and that is
    /// deliberate rather than unfinished: every ceiling a grant carries is a
    /// number no client may default, and the owner's own route is where they
    /// are set.
    #[cfg(feature = "operator")]
    RevokeAuthority {
        reason: String,
    },
    /// Fit one predictor lane, against the board's own baseline.
    ///
    /// **A research run, and the only write on this workstation whose cost is
    /// the owner's CPU rather than a row in the book.** It writes one
    /// `predictor_board` run and nothing else: no plan, no approval, no
    /// posture, and every gate between a plan and a fill is unmoved by it.
    ///
    /// Gated with the writes even so. It is a POST, it spends real seconds of
    /// an owner shared with whoever else is at the desk, and a window the desk
    /// declined authority to is not where one gets started from — the same
    /// rule the two hand-offs are held to.
    ///
    /// `model` is a lane id the *owner* served, read off the board this client
    /// is already drawing. Nothing here composes one, so there is no name this
    /// command can carry that the owner did not name first.
    #[cfg(feature = "operator")]
    RunPredictor {
        model: String,
        /// The lane this window is pointed at, so the fit reads the panel the
        /// operator is looking at rather than the route's own default.
        offline: bool,
    },
    /// Open the real Claude CLI as Atlas, on this terminal.
    ///
    /// Not a request and not a write: the runtime hands the screen to a child
    /// process (`crate::handoff`) and takes it back when it exits, so this
    /// variant never reaches the dispatch seam at all. What that child may do
    /// is decided by the desk's own `qlab cli` verb — the owner-backed proxy
    /// tools plus read-only web, no shell and no filesystem — and this client
    /// neither composes that command line nor can widen it.
    ///
    /// Gated with the writes even so, and offered only to an armed window: the
    /// session it opens can start research on this desk, and a window the desk
    /// declined authority to is not where one gets opened from. It books
    /// nothing either way — the fill gate is untouched, and the child has no
    /// execution tool to reach it with.
    #[cfg(feature = "operator")]
    OpenCli,
    /// Open Claude Code on this checkout with a request, on this terminal.
    ///
    /// The one command on this workstation that changes source rather than
    /// desk state, and the operator is in the loop for all of it: Claude Code's
    /// own interactive permission prompts are the gate, answered by the human
    /// sitting in front of the terminal the child now owns. On return, a build
    /// that touched the desk's own trees is *offered* `qlab --restart runtime`
    /// and never given it — invariant 8 says the restart is needed, not that a
    /// keystroke may perform one on a live desk.
    #[cfg(feature = "operator")]
    OpenBuild(String),
    /// Ask the owner what its backends serve.
    ///
    /// A read, and the only one a keystroke asks for: the route probes daemons,
    /// so it may not ride a poll (`net::http::Refetch`). Produced when the
    /// palette enters the model scope — which only an armed window can do, the
    /// same way `Scope::Mode`'s value list is compiled into a glass build and
    /// never reached there.
    Backends,
    /// Ask the owner to draw one of its registered visuals.
    ///
    /// A read, and ungated for that reason: the route renders text from
    /// parameters and touches no registry row, no plan and no approval, so a
    /// glass window may ask for it exactly as it may ask what the backends
    /// serve. It carries a *name* the owner itself served in
    /// `/api/visuals` — never a path — and the poller is what turns one into a
    /// request, so nothing here can aim this client anywhere.
    RenderVisual(String),
}

/// The one field of the mandate a METHOD change carries.
///
/// A sum rather than a struct of two `Option`s, because the owner accepts a
/// *subset* and the two absent-shapes are not the same fact: "leave the method
/// alone" and "clear the cap override" are both `None` in a struct, and the
/// second is a request that changes the desk. The enum makes the pair
/// unrepresentable, so nothing downstream can send an empty body the owner
/// answers 400 to.
///
/// [`MethodChange::Cap`]'s own `None` is the clearing one: it sends
/// `max_holdings: null`, which drops the override and puts the shipped
/// mandate's value back in force.
#[cfg(feature = "operator")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MethodChange {
    Policy(String),
    Cap(Option<i64>),
}

/// One of the three authorities the operator lends Atlas.
///
/// A closed set rather than a `String`, because the owner refuses an unknown
/// key **by name** rather than ignoring it: an operator who thought they
/// withdrew something would otherwise be left holding an authority they
/// believe is gone. A typed field cannot compose a fourth name, so that
/// refusal is one this client can never provoke.
///
/// Not gated, unlike the command that carries it: the glass build draws the
/// three rows, and the names they are drawn by are one list in both builds.
/// [`Right::as_str`] is what puts one on the wire, and it agrees with
/// `model::RightsFlags::FIELDS` by test.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Right {
    Web,
    Workflows,
    Build,
}

impl Right {
    /// The owner's own key for this right.
    pub fn as_str(self) -> &'static str {
        match self {
            Right::Web => "web",
            Right::Workflows => "workflows",
            Right::Build => "build",
        }
    }

    /// The right at one row of the card, in the order the owner declares them.
    pub fn at(row: usize) -> Option<Right> {
        [Right::Web, Right::Workflows, Right::Build]
            .get(row)
            .copied()
    }
}

/// An EDGAR contact on its way to the owner.
///
/// A newtype for exactly one reason, and it is the same reason
/// [`crate::secret::Secret`] is one: `Command` derives `Debug`, a derive prints
/// every field of every variant, and this variant's own documentation claims
/// the contact lives in the box it was typed into and in the request body and
/// nowhere else. A claim a `{:?}` on a `Command` could falsify — in a tracing
/// line, a panic message, a toast — is not a claim, it is a hope. Nothing
/// formats a `Command` today; the redaction is what keeps that from being the
/// load-bearing half.
///
/// **Not a `Secret`, deliberately.** A contact is an identity a public archive
/// is told, not a credential: it needs no `expose`, and `operator_gate`'s
/// census of where plaintext credentials are readable stays a census of one
/// file. `as_str` is the whole reader, and the module that puts a body on the
/// wire is its whole caller — which this file may not name, by the same rule
/// the header states: the pin is a plain text search, so even a comment here
/// may not spell what is on the far side of the seam.
///
/// Gated with the command that carries it: the glass build has no route to
/// send one to.
#[cfg(feature = "operator")]
#[derive(Clone, PartialEq, Eq)]
pub struct Contact(String);

#[cfg(feature = "operator")]
impl Contact {
    pub fn new(said: String) -> Self {
        Self(said)
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Hand-written, and that is the point of the type. A derive here would print
/// the contact, and would do it from inside whatever holds a `Command`.
#[cfg(feature = "operator")]
impl std::fmt::Debug for Contact {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Contact(<redacted>)")
    }
}

/// What one `/model` line asks for.
///
/// Two shapes because the owner's route takes two: a pair, or the reasoner's
/// switch. They are one type rather than three optional fields for the reason
/// `LlmSurface` gives — `backend`/`model` are optional *together*, and a
/// signature able to express half a choice is one that invites the owner's 400.
///
/// Not gated, unlike the command that carries it: this is what a line *means*,
/// and the grammar is one grammar in both builds. What is gated is the variant
/// that reaches a writer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelChoice {
    /// A backend and the model it should run, as the operator named them.
    Pair { backend: String, model: String },
    /// The reasoner's own switch. Naming a model does not turn it on — the
    /// owner refuses to infer one from the other, so nothing here may either.
    Enabled(bool),
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
            // Never equal, for `Execute`'s reason: comparing two confirmations
            // is only ever a prelude to substituting one for the other.
            #[cfg(feature = "operator")]
            (Command::Book(_), Command::Book(_)) => false,
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
            // Compared, unlike a confirmation, because there is nothing to
            // substitute: a login is the pair itself rather than a capability
            // minted for one plan, and a test that could not read back what a
            // keystroke built could not pin the form at all.
            #[cfg(feature = "operator")]
            (
                Command::AlpacaLogin {
                    key: a,
                    secret: x,
                    replace: p,
                },
                Command::AlpacaLogin {
                    key: b,
                    secret: y,
                    replace: q,
                },
            ) => a == b && x == y && p == q,
            #[cfg(feature = "operator")]
            (Command::TestAlpaca, Command::TestAlpaca) => true,
            #[cfg(feature = "operator")]
            (
                Command::SetLlm {
                    surface: a,
                    choice: x,
                },
                Command::SetLlm {
                    surface: b,
                    choice: y,
                },
            ) => a == b && x == y,
            #[cfg(feature = "operator")]
            (
                Command::NewsSettings {
                    providers: a,
                    contact: x,
                    verify: p,
                    offline: o,
                },
                Command::NewsSettings {
                    providers: b,
                    contact: y,
                    verify: q,
                    offline: n,
                },
            ) => a == b && x == y && p == q && o == n,
            #[cfg(feature = "operator")]
            (Command::SetMethod(a), Command::SetMethod(b)) => a == b,
            #[cfg(feature = "operator")]
            (
                Command::SetRight { field: a, value: x },
                Command::SetRight { field: b, value: y },
            ) => a == b && x == y,
            #[cfg(feature = "operator")]
            (
                Command::RunPredictor {
                    model: a,
                    offline: x,
                },
                Command::RunPredictor {
                    model: b,
                    offline: y,
                },
            ) => a == b && x == y,
            #[cfg(feature = "operator")]
            (Command::Posture { armed: a }, Command::Posture { armed: b }) => a == b,
            #[cfg(feature = "operator")]
            (Command::ApproveAction(a), Command::ApproveAction(b)) => a == b,
            #[cfg(feature = "operator")]
            (Command::Actionables, Command::Actionables) => true,
            #[cfg(feature = "operator")]
            (Command::OpenCli, Command::OpenCli) => true,
            #[cfg(feature = "operator")]
            (Command::OpenBuild(a), Command::OpenBuild(b)) => a == b,
            (Command::Backends, Command::Backends) => true,
            (Command::RenderVisual(a), Command::RenderVisual(b)) => a == b,
            _ => false,
        }
    }
}

// -- the grammar ------------------------------------------------------------

/// What the command line can be pointed at.
///
/// Seven, and the two that are missing are the point: the plan's Part IV lists
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
    // NOTE: `Clear` is declared at the tail; the doc comments between keep
    // their variants.
    /// Which data source and which book the desk is pointed at.
    ///
    /// The first scope that writes. It is in the grammar in both builds so the
    /// parser table is one table — a grammar that changed shape with a Cargo
    /// feature would be two grammars, and only one of them ever tested — while
    /// what it *resolves to* is gated with the writer, and what it offers is
    /// gated on the posture.
    Mode,
    /// Which model each surface runs on, and whether Atlas reasons at all.
    ///
    /// Gated exactly like `Mode`, and for a narrower reason than it looks:
    /// choosing a model changes *who answers a question*, never what may be
    /// executed. The owner pins the referee to claude whatever this says.
    Model,
    /// Ask the desk what it would do, which is what puts proposals on the
    /// panel at all.
    ///
    /// It sits beside `/do` because it is the other half of one act: this asks
    /// and the desk writes down what it would do; `/do` approves one of those
    /// and the desk starts it. A scope rather than a key on ATLAS, because
    /// that pane is chat-first — a printable character in an armed window goes
    /// into the question row — so a letter there would either be stolen from
    /// the operator's sentence or hidden behind a modifier nothing else uses.
    ///
    /// It writes, and is gated accordingly: an ask mints `proposal`-origin
    /// task rows, so an unarmed or `--glass` window can read a panel somebody
    /// else filled and cannot fill one. It widens nothing — every item comes
    /// back gate-checked and is checked again at approval.
    Ask,
    /// Approve one of today's proposals, which starts the work the owner
    /// offered.
    ///
    /// The only scope that puts the desk to work rather than pointing it
    /// somewhere, and it still widens nothing: the owner re-runs
    /// `check_startable` on the POST, so a template that would create a paper
    /// plan is refused below `propose` whatever this sends. What it adds is a
    /// route the *operator's approval* travels on — which the owner records,
    /// so a started proposal is a decision with a human behind it rather than
    /// a task that appeared in the queue.
    Do,
    /// Approve a pending approval by id — or, named a checked plan with no
    /// request yet, ask the desk to open one and approve that. Either way the
    /// decision itself happens in the confirm box this opens; the word alone
    /// decides nothing.
    Approve,
    /// Book a checked plan whose approval is on the record: opens the box
    /// that takes the last six of the plan's own `targets_hash`. The chat's
    /// spelling of BOOK's `x`, held to the identical ritual.
    Execute,
    /// Open the real Claude CLI as Atlas, on this terminal.
    ///
    /// The two scopes below are the only ones whose effect is a child process
    /// rather than a request. They are in the grammar in both builds for the
    /// reason `Mode` is — one parser table, not two, only one of which is ever
    /// tested — and what they resolve to is gated with everything else an
    /// unarmed window may not reach.
    Cli,
    /// Open Claude Code on this checkout with a request.
    Build,
    /// Empty this window's chat pane. The one scope that acts without touching
    /// the desk, so it is offered in every posture.
    Clear,
}

impl Scope {
    /// Picker order: the three a glass window can use, then the four it
    /// cannot. `/ask` before `/do`, because that is the order they happen in —
    /// there is nothing to approve until the desk has been asked.
    pub const ALL: [Scope; 12] = [
        Scope::View,
        Scope::Ticker,
        Scope::Plan,
        Scope::Mode,
        Scope::Model,
        Scope::Ask,
        Scope::Do,
        Scope::Approve,
        Scope::Execute,
        Scope::Cli,
        Scope::Build,
        Scope::Clear,
    ];

    /// The word an operator types, and the word the suggestions show. One
    /// spelling, so the strip cannot offer something the parser will not accept.
    pub fn word(self) -> &'static str {
        match self {
            Scope::View => "view",
            Scope::Ticker => "ticker",
            Scope::Plan => "plan",
            Scope::Mode => "mode",
            Scope::Model => "model",
            Scope::Ask => "ask",
            Scope::Do => "do",
            Scope::Approve => "approve",
            Scope::Execute => "execute",
            Scope::Cli => "cli",
            Scope::Build => "build",
            Scope::Clear => "clear",
        }
    }

    /// What the scope takes, for the strip to say before anything is typed.
    pub fn hint(self) -> &'static str {
        match self {
            Scope::View => "a view, by the label the nav rail shows",
            Scope::Ticker => "a symbol this desk is watching",
            Scope::Plan => "a plan id, or enough of one to be unambiguous",
            Scope::Mode => "a data source and a book",
            Scope::Model => "a surface, and the model it should run",
            Scope::Ask => "nothing — Enter asks the desk what it would do",
            Scope::Do => "a proposal the desk is offering, in full",
            Scope::Approve => "a pending approval id, or a checked plan id — opens the approve box",
            Scope::Execute => "a checked plan id with its approval on record — opens the hash box",
            // "on this terminal" until the pane landed, and it was the whole
            // of what changed: the child no longer takes the alternate screen
            // and hands it back on exit. `/build` still does, which is why only
            // one of these two lines moved.
            Scope::Cli => {
                "nothing — Enter runs the Claude CLI as Atlas, in the tab beside the desk"
            }
            Scope::Build => {
                "opens Claude Code on this checkout — it can edit files, with your approval"
            }
            Scope::Clear => "nothing — Enter empties this window's chat pane",
        }
    }

    /// Whether using this scope changes the desk. Offered only to a window that
    /// can, exactly as every other operator affordance on this workstation.
    pub fn writes(self) -> bool {
        // `/clear` is deliberately absent: it empties this window's chat pane
        // and touches nothing on the desk, so a glass window may use it.
        //
        // `/cli` and `/build` are deliberately present. Neither sends a request
        // — the runtime spawns a child — but one opens a session that can start
        // research on this desk and the other edits the checkout the desk runs
        // on, and a window the desk declined authority to is not where either
        // belongs. "It is not an HTTP write" is not the test; "can it change
        // what the desk does" is.
        matches!(
            self,
            Scope::Mode
                | Scope::Model
                | Scope::Ask
                | Scope::Do
                | Scope::Approve
                | Scope::Execute
                | Scope::Cli
                | Scope::Build
        )
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
    /// Point one surface at a model, or switch the reasoner.
    #[cfg(feature = "operator")]
    Model {
        surface: String,
        choice: ModelChoice,
    },
    /// Ask the desk what it would do. No payload: the question is always the
    /// same one, and the answer arrives on the next poll rather than through
    /// the line that asked.
    #[cfg(feature = "operator")]
    Ask,
    /// Approve one proposal: start the task the owner bound to it.
    ///
    /// **Both halves, and neither is redundant.** `task` is what gets sent —
    /// the owner's own id for the persisted proposal — and `template` is the
    /// word the operator typed and the panel draws, which is the only thing
    /// the surface above can check against what it actually put on screen.
    /// A variant carrying the id alone would have made "was this item drawn"
    /// unanswerable without re-deriving the pairing that produced it.
    #[cfg(feature = "operator")]
    Approve { template: String, task: String },
    /// Open the approve box for one pending approval. The box is the
    /// decision; this only puts it in front of the operator.
    #[cfg(feature = "operator")]
    OpenApprove(String),
    /// Ask the owner to open an approval request for a checked plan that has
    /// none. The approve box follows on the owner's answer — see
    /// `AtlasView::wrote` — so `/approve <plan>` is one command, one confirm.
    #[cfg(feature = "operator")]
    RequestApproval(String),
    /// Open the hash box for a checked plan whose approval is on the record.
    #[cfg(feature = "operator")]
    OpenExecute(String),
    /// Open the real Claude CLI as Atlas on this terminal.
    #[cfg(feature = "operator")]
    Cli,
    /// Open Claude Code on this checkout with this request as its first turn.
    /// The string is whatever the operator typed, sent whole: what a build is
    /// allowed to do is Claude Code's own permission prompts to ask, and a
    /// client that pre-judged the sentence would be a second opinion nobody
    /// can see.
    #[cfg(feature = "operator")]
    Build(String),
    /// Empty this window's chat pane. Local: the bus keeps every row and the
    /// AUDIT view still shows them — what clears is what this window draws,
    /// exactly like Claude Code's own `/clear`.
    ClearChat,
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

/// The surfaces a model can be chosen for, in the owner's own order and
/// spelling (`qlab/core/llm_config.py::SURFACES`).
///
/// Refused here rather than sent, unlike a desk-mode pair: which surfaces exist
/// is the *grammar* of this scope — the same kind of fixed vocabulary as the
/// nav rail's view labels, which `/view` also answers from a list this client
/// holds — while which model a surface can run is a fact about the desk, and
/// that stays the owner's.
///
/// `pub(crate)` because the startup door offers the same two surfaces: one
/// vocabulary, so the door cannot ask about a surface the line would refuse.
pub(crate) const SURFACES: [&str; 2] = [REASONER, WORKFORCE];
/// The surface that answers the operator and reasons over templates — what
/// "which mind runs Atlas" means, and the one the startup door writes when it
/// is told to keep the mind the desk already has. `pub(crate)` for that caller:
/// naming it by exclusion from [`WORKFORCE`] read as a fact about the owner's
/// ordering, which is exactly what this pair is not.
pub(crate) const REASONER: &str = "reasoner";
pub(crate) const WORKFORCE: &str = "workforce";

/// The reasoner's switch, spelled the way an operator says it out loud. The
/// owner's route takes a boolean; these are the two words that become one.
const SWITCH: [&str; 2] = ["on", "off"];

/// The owner's own `model_routing.CLAUDE_BACKEND`: the backend whose model the
/// workforce's routing ignores, and the one `qlab cli` launches whatever else a
/// desk is configured with — which is why `/cli` refuses on any other.
const CLAUDE: &str = "claude";

/// What the claude backend calls "let the tiers decide" — the first entry of
/// its own `CLAUDE_MODELS`, and the desk's default.
///
/// Named here rather than read off the catalog's ordering: if the owner ever
/// stops serving it, this client sends a model by name and gets the owner's own
/// refusal back, where taking "whatever is first" would silently pick a tier
/// nobody chose.
const CLAUDE_INHERIT: &str = "inherit";

/// How much of an owner sentence the one-row strip and the line's note can
/// carry.
///
/// The bound is D1's `SAID_MAX` and for the same reason: nothing on the wire is
/// guaranteed to be the owner's, and the longest sentence it actually writes
/// (the `ollama pull` remedy, 105 cells) survives uncut. Bounded here, at the
/// boundary, so both surfaces that render it inherit it.
const REASON_MAX: usize = 112;

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
        Scope::Model => model(query, store, posture),
        Scope::Ask => ask(query, posture),
        Scope::Do => act(query, store, posture),
        Scope::Approve => approve(query, store, posture),
        Scope::Execute => execute(query, store, posture),
        Scope::Cli => hand_off(Scope::Cli, query, store, posture),
        Scope::Build => hand_off(Scope::Build, query, store, posture),
        Scope::Clear => match query.is_empty() {
            true => Resolved::ClearChat,
            false => {
                Resolved::Refused("/clear takes no argument — Enter empties the chat pane".into())
            }
        },
    }
}

/// The backend the owner says Atlas reasons with, when it has named one.
///
/// An empty string is not a name: the owner serves the pair whole and a surface
/// nobody has chosen for arrives with its halves absent or blank, which is the
/// same fact and must read as one.
fn reasoner_backend(store: &Store) -> Option<&str> {
    let backend = store.llm()?.reasoner.as_ref()?.backend.as_deref()?;
    (!backend.trim().is_empty()).then_some(backend)
}

/// The two lines that hand this terminal to a child.
///
/// One function for both, because the posture rule and the shape of the
/// refusal are identical and only the argument differs: `/cli` takes none and
/// `/build` takes all of it. Split into two would have been two copies of the
/// gate, which is how one of them comes to be missing it.
fn hand_off(scope: Scope, query: &str, store: &Store, posture: Posture) -> Resolved {
    // The posture first, exactly as `/mode` and `/model`: an unarmed window is
    // refused for being unarmed, whatever it typed.
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/{} opens a Claude session on this desk; this window is {} — the desk is not armed",
            scope.word(),
            posture.label()
        ));
    }
    // Then which mind this desk reasons with, and only for the pane. `/cli`
    // puts a Claude session in the tab Atlas already runs in, so a desk
    // configured for a local reasoner would be running two minds in one column
    // — and the operator's own answer to "which mind runs Atlas" is the one
    // that decides. `/build` is a different question (Claude Code editing this
    // checkout) and the MODELS card says so, which is why this is not shared.
    //
    // Silent when nothing has named a backend: that desk has not answered the
    // question yet — the startup door is what asks it — and a refusal there
    // would be a distinction this client invented.
    if scope == Scope::Cli {
        if let Some(backend) = reasoner_backend(store) {
            if backend != CLAUDE {
                return Resolved::Refused(format!(
                    "`qlab cli` is a Claude verb and this desk reasons with {backend} — \
                     SETTINGS ▸ MODELS is where that changes"
                ));
            }
        }
    }
    match scope {
        Scope::Cli if query.is_empty() => {
            #[cfg(feature = "operator")]
            return Resolved::Cli;
            // Unreachable: `posture.writes()` above is false for every
            // `Posture` this build has. Total over the type, like `mode`.
            #[cfg(not(feature = "operator"))]
            Resolved::Refused("this build has no hand-off".into())
        }
        Scope::Cli => {
            Resolved::Refused("/cli takes no argument — Enter opens the Claude CLI as Atlas".into())
        }
        Scope::Build if query.is_empty() => {
            Resolved::Refused("say what to build: /build add a heatmap visual to the desk".into())
        }
        Scope::Build => {
            #[cfg(feature = "operator")]
            return Resolved::Build(query.to_string());
            #[cfg(not(feature = "operator"))]
            Resolved::Refused("this build has no hand-off".into())
        }
        // Unreachable: `scoped` calls this with exactly the two scopes above.
        // Stated rather than assumed — a third scope routed here by a later
        // edit would otherwise resolve to whichever arm it fell through to.
        other => Resolved::Refused(format!("/{} is not a hand-off", other.word())),
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
    // The posture, not the feature. A window on a desk the owner has not armed
    // reads GLASS on the status line, and this may not disagree with it.
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/mode changes the desk; this window is {} — the desk is not armed",
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

/// One `/model` line: a surface, and a model or a switch.
///
/// The posture first, exactly as `/mode`: an unarmed window is refused for
/// being unarmed whatever it typed.
fn model(query: &str, store: &Store, posture: Posture) -> Resolved {
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/model changes the desk; this window is {} — the desk is not armed",
            posture.label()
        ));
    }
    let mut words = query.split_whitespace();
    let (Some(surface), Some(choice)) = (words.next(), words.next()) else {
        return Resolved::Refused(
            "a model choice is a surface and a model: /model reasoner ollama:granite3.3:8b".into(),
        );
    };
    if words.next().is_some() {
        return Resolved::Refused("a model choice is two words, not three".into());
    }
    let Some(surface) = SURFACES
        .into_iter()
        .find(|known| known.eq_ignore_ascii_case(surface))
    else {
        return Resolved::Refused(format!(
            "no model surface is called {surface}; the desk has {}",
            SURFACES.join(" and ")
        ));
    };
    // The switch, whichever surface it was typed against. The owner refuses it
    // on the workforce in its own words — "only the reasoner surface can be
    // switched on or off" — and a second copy of that rule here would be this
    // client deciding which surfaces have an off state, which is the same
    // reason `/mode` sends the pair `DeskMode` forbids.
    if let Some(flag) = SWITCH
        .into_iter()
        .find(|word| word.eq_ignore_ascii_case(choice))
    {
        return chose(surface, ModelChoice::Enabled(flag == SWITCH[0]));
    }
    // What the desk said it can run, which is what the strip offered.
    if let Some(offer) = offers(surface, store)
        .into_iter()
        .find(|offer| offer.matches(choice))
    {
        return match offer {
            Offer::Runs { backend, model, .. } => {
                chose(surface, ModelChoice::Pair { backend, model })
            }
            // Shown in the strip and refused here, in the owner's own sentence
            // rather than a second opinion composed by this client. It is not
            // an availability check either: the entry names no model *because*
            // the owner could not ask for one, so there is no pair to send.
            Offer::Down { said, .. } => Resolved::Refused(said),
        };
    }
    // Typed by hand, and sent. The owner is the authority on what it can serve
    // — a model pulled since the last fetch is one this client has never heard
    // of — so anything naming both halves goes, and the owner's refusal is what
    // comes back. Split on the *first* colon: a model id carries its own
    // (`qwen2.5:7b`, `granite3.3:8b`), and splitting on the last would name a
    // backend called `ollama:qwen2.5`.
    match choice.split_once(':') {
        Some((backend, model)) if !backend.is_empty() && !model.is_empty() => chose(
            surface,
            ModelChoice::Pair {
                backend: backend.to_string(),
                model: model.to_string(),
            },
        ),
        _ => Resolved::Refused(format!(
            "{choice} names no model — a choice is a backend and a model, like \
             ollama:granite3.3:8b"
        )),
    }
}

/// The parsed choice, in the build that has somewhere to send it.
fn chose(surface: &str, choice: ModelChoice) -> Resolved {
    #[cfg(feature = "operator")]
    return Resolved::Model {
        surface: surface.to_string(),
        choice,
    };
    // Unreachable: `posture.writes()` is false for every `Posture` this build
    // has, so `model` refused above. The arm exists because the function is
    // total, which is what keeps the grammar one grammar in both builds.
    #[cfg(not(feature = "operator"))]
    {
        let _ = (surface, choice);
        Resolved::Refused("this build has no write path".into())
    }
}

/// One `/ask` line: put the question to the desk and let it write down what it
/// would do.
///
/// The posture first, exactly as every other write scope. Nothing else is
/// decided here — there is no argument to validate, because the question is
/// always the same one and the desk composes the answer from its own facts.
///
/// **No argument, and a typed one is refused rather than dropped.** A word
/// after `/ask` can only be an operator meaning something this does not do
/// (asking about one template, asking a question in prose — that is the ATLAS
/// chat row), and sending the ask anyway would answer a question nobody put.
/// `/approve`: a pending approval by id, or a checked plan that needs one.
///
/// Prefix-matched on what the desk is serving, never on what was typed
/// alone: an id this desk is not holding resolves to nothing, and the
/// sentence says which of the two things the word answers was looked for.
fn approve(query: &str, store: &Store, posture: Posture) -> Resolved {
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/approve decides for this desk; this window is {} — the desk is not armed",
            posture.label()
        ));
    }
    if query.is_empty() {
        return Resolved::Refused(
            "/approve takes a pending approval id, or a checked plan id".into(),
        );
    }
    #[cfg(feature = "operator")]
    {
        let wanted = query.to_ascii_lowercase();
        if let Some(id) = store
            .approvals()
            .iter()
            .filter(|a| a.status.as_deref() == Some("pending"))
            .filter_map(|a| a.approval_id.as_deref())
            .find(|id| id.to_ascii_lowercase().starts_with(&wanted))
        {
            return Resolved::OpenApprove(id.to_string());
        }
        if let Some(id) = store
            .plans()
            .iter()
            .filter(|p| p.state.as_deref() == Some("checked"))
            .filter_map(|p| p.plan_id.as_deref())
            .find(|id| id.to_ascii_lowercase().starts_with(&wanted))
        {
            return match store.approval_for(id).and_then(|a| a.status.as_deref()) {
                None => Resolved::RequestApproval(id.to_string()),
                Some("approved") => Resolved::Refused(format!(
                    "plan {} is already approved — /execute {} books it",
                    &id[..8.min(id.len())],
                    &id[..8.min(id.len())]
                )),
                Some(state) => Resolved::Refused(format!(
                    "plan {}'s approval is {state}; ask again for a fresh one",
                    &id[..8.min(id.len())]
                )),
            };
        }
    }
    let _ = store;
    Resolved::Refused(format!(
        "no pending approval or checked plan on this desk starts with {query}"
    ))
}

/// `/execute`: BOOK's `x`, spelled in the chat and held to the same ritual.
fn execute(query: &str, store: &Store, posture: Posture) -> Resolved {
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/execute books on this desk; this window is {} — the desk is not armed",
            posture.label()
        ));
    }
    if query.is_empty() {
        return Resolved::Refused("/execute takes a checked plan id".into());
    }
    let wanted = query.to_ascii_lowercase();
    let Some(id) = store
        .plans()
        .iter()
        .filter(|p| p.state.as_deref() == Some("checked"))
        .filter_map(|p| p.plan_id.as_deref())
        .find(|id| id.to_ascii_lowercase().starts_with(&wanted))
    else {
        return Resolved::Refused(format!("no checked plan on this desk starts with {query}"));
    };
    let short = &id[..8.min(id.len())];
    if store.covering_approval(id).is_none() {
        return Resolved::Refused(format!(
            "plan {short} has no approval on record — /approve {short} opens one"
        ));
    }
    // The `return` is what makes the two cfg arms one function: without it the
    // operator block is a statement and the glass line below it would be dead
    // code in this build. Clippy reads it as needless because it only ever sees
    // one arm — see the `allow`, which is about the pair rather than the line.
    #[cfg(feature = "operator")]
    #[allow(clippy::needless_return)]
    {
        return Resolved::OpenExecute(id.to_string());
    }
    #[cfg(not(feature = "operator"))]
    Resolved::Refused("this build carries no writer".into())
}

fn ask(query: &str, posture: Posture) -> Resolved {
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/ask puts proposals in this desk's queue; this window is {} — the desk is not \
             armed",
            posture.label()
        ));
    }
    if !query.is_empty() {
        return Resolved::Refused(
            "/ask takes no argument — Enter asks the desk what it would do next".into(),
        );
    }
    asked()
}

/// The ask, in the build that has somewhere to send it.
fn asked() -> Resolved {
    #[cfg(feature = "operator")]
    return Resolved::Ask;
    // Unreachable: `posture.writes()` is false for every `Posture` this build
    // has, so `ask` refused above. The arm exists because the function is
    // total, which is what keeps the grammar one grammar in both builds.
    #[cfg(not(feature = "operator"))]
    Resolved::Refused("this build has no write path".into())
}

/// One `/do` line: the proposal an operator approved.
///
/// The posture first, exactly as `/mode` and `/model`: an unarmed window is
/// refused for being unarmed whatever it typed. Nothing here decides whether
/// the work may run — the owner re-runs `check_startable` on the POST, and its
/// answer is the one that counts. What this decides is whether there is
/// anything to *ask about*: a proposal the desk is not offering, one the gate
/// has already refused, and one the owner served no task for are all answered
/// here rather than sent.
fn act(query: &str, store: &Store, posture: Posture) -> Resolved {
    if !posture.writes() {
        return Resolved::Refused(format!(
            "/do starts work on this desk; this window is {} — the desk is not armed",
            posture.label()
        ));
    }
    if query.is_empty() {
        return Resolved::Refused("name a proposal — the strip below lists them".into());
    }
    // **The whole id, never a prefix.** `/plan` completes on a prefix because
    // naming a plan moves a cursor; this one starts work, and today's list
    // grows all day — `/do desk` is unambiguous in the morning and names two
    // proposals by the afternoon, at which point the same keystroke that
    // started a brief starts a rebalance review. Case-blind, because the
    // template ids are words this client spells for the operator.
    let Some(item) = store.actionables().iter().find(|item| {
        crate::format::text(item.template_id.as_ref())
            .is_some_and(|id| id.eq_ignore_ascii_case(query))
    }) else {
        return Resolved::Refused(format!(
            "{query} is not a proposal this desk is offering — type the id in full"
        ));
    };
    // The owner's own spelling, not what was typed: the surface above matches
    // this against what it drew, and the panel draws the owner's.
    let template = crate::format::text(item.template_id.as_ref())
        .unwrap_or(query)
        .to_string();
    // One check, and it hands back the id: there is no branch here that could
    // ask "may this be approved" and then answer with an id of its own.
    match approvable(item, &template) {
        Ok(task) => approved(template, task.to_string()),
        Err(said) => Resolved::Refused(said),
    }
}

/// The approval, in the build that has somewhere to send it.
fn approved(template: String, task: String) -> Resolved {
    #[cfg(feature = "operator")]
    return Resolved::Approve { template, task };
    // Unreachable: `posture.writes()` is false for every `Posture` this build
    // has, so `act` refused above. The arm exists because the function is
    // total, which is what keeps the grammar one grammar in both builds.
    #[cfg(not(feature = "operator"))]
    {
        let _ = (template, task);
        Resolved::Refused("this build has no write path".into())
    }
}

/// The task an approval would start, or the sentence saying why there is none.
///
/// One producer for the line and the strip, exactly as [`Offer`] is for
/// `/model`: two copies of "which items can be started" is two chances for the
/// strip to offer something the line then refuses. It hands back the **task
/// id** rather than a yes, so the caller cannot ask the question and then
/// compose an id of its own — which is precisely the fall-back that would POST
/// to `/api/atlas/tasks/desk_brief/start`.
///
/// **`Some(false)` is the only refusal on this payload.** `None` is not a
/// verdict — the snapshot surface cannot call `atlas_facts`, so it reports
/// what it could not check rather than asserting a permit it did not compute
/// (`model::ActionItem`), and `true` never arrives there at all. Reading `None`
/// as refused would leave every live proposal unapprovable; reading it as
/// permitted would be this client agreeing with a gate nobody asked. It is
/// neither: it is sent, and the owner's own answer comes back.
fn approvable<'a>(item: &'a crate::model::ActionItem, template: &str) -> Result<&'a str, String> {
    if item.startable == Some(false) {
        return Err(match crate::format::text(item.reason.as_ref()) {
            Some(said) => crate::format::bounded(said, REASON_MAX),
            // The owner attaches a sentence to every refusal it makes. One
            // without is a contract this client cannot read, and it says which
            // silence it met rather than offering the item anyway.
            None => format!("the desk refused {template} and did not say why"),
        });
    }
    // Absent is not the template id. `task_id` is optional on the wire and
    // `Some("")` is absence, so the one thing that must not happen here is a
    // fall-back to the word the operator typed.
    crate::format::text(item.task_id.as_ref()).ok_or_else(|| {
        format!("the owner served no task for {template}; there is nothing to approve")
    })
}

/// What one surface can be pointed at, out of the last catalog this client
/// fetched.
///
/// Values come off that catalog and nowhere else, so the strip cannot offer a
/// pair the owner never said it could run. Absent — nothing fetched yet — is an
/// empty list rather than a guess.
pub(crate) fn offers(surface: &str, store: &Store) -> Vec<Offer> {
    let Some(catalog) = store.backends() else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for entry in &catalog.backends {
        let Some(name) = crate::format::text(entry.name.as_ref()) else {
            continue;
        };
        // `None` is not `true`. A backend the owner did not vouch for is one
        // this client may not offer a pair on.
        let models: Vec<&str> = match entry.available {
            Some(true) => entry
                .models
                .iter()
                .filter_map(|model| crate::format::text(Some(model)))
                .collect(),
            _ => Vec::new(),
        };
        if models.is_empty() {
            out.push(Offer::Down {
                value: name.to_string(),
                said: unservable(entry, name),
            });
            continue;
        }
        // The workforce's claude model is ignored by the owner's own routing —
        // the tier map owns it, and `("claude", "haiku")` routes exactly as
        // `("claude", "inherit")` does. Offering the four tiers here would be
        // offering three choices the desk would not make.
        if name == CLAUDE && surface == WORKFORCE {
            out.push(Offer::Runs {
                value: CLAUDE.to_string(),
                backend: CLAUDE.to_string(),
                model: CLAUDE_INHERIT.to_string(),
            });
            continue;
        }
        out.extend(models.into_iter().map(|model| Offer::Runs {
            value: format!("{name}:{model}"),
            backend: name.to_string(),
            model: model.to_string(),
        }));
    }
    out
}

/// Why a backend has nothing to offer, in the owner's own words where it gave
/// any.
///
/// The owner populates `reason` on every entry, the happy path included, so the
/// two fallbacks are contract failures rather than ordinary states — and each
/// says which one it is instead of reading as a backend that answered.
fn unservable(entry: &crate::model::CatalogEntry, name: &str) -> String {
    match (entry.available, crate::format::text(entry.reason.as_ref())) {
        (Some(true), _) => format!("the owner says {name} can serve and never said what"),
        (_, Some(said)) => crate::format::bounded(said, REASON_MAX),
        (_, None) => format!("the owner did not say why {name} cannot serve"),
    }
}

/// One thing `/model` can be pointed at, or one backend it cannot.
///
/// `pub(crate)` because the startup door draws the same list. One producer for
/// both surfaces, so a rule the strip keeps — a down backend shown with the
/// owner's reason, the workforce offered `claude` alone — cannot hold in one
/// place and be quietly dropped in the other.
pub(crate) enum Offer {
    /// A pair the desk said it can run. `value` is what an operator types or
    /// accepts; the pair is what gets sent.
    Runs {
        value: String,
        backend: String,
        model: String,
    },
    /// A backend the last reading says cannot serve, and the owner's reason.
    ///
    /// Kept in the list rather than filtered out of it: a backend that vanishes
    /// from the strip reads as a desk that never had one, and "why not" is
    /// exactly the question the reason answers.
    Down { value: String, said: String },
}

impl Offer {
    pub(crate) fn value(&self) -> &str {
        match self {
            Offer::Runs { value, .. } | Offer::Down { value, .. } => value,
        }
    }

    /// The owner's sentence about why this cannot be chosen — `None` for one
    /// the desk can run.
    pub(crate) fn refusal(&self) -> Option<&str> {
        match self {
            Offer::Runs { .. } => None,
            Offer::Down { said, .. } => Some(said),
        }
    }

    /// What choosing this sends, or `None` for a backend the desk cannot serve
    /// — which names no model, so there is no pair to send.
    pub(crate) fn choice(&self) -> Option<ModelChoice> {
        match self {
            Offer::Runs { backend, model, .. } => Some(ModelChoice::Pair {
                backend: backend.clone(),
                model: model.clone(),
            }),
            Offer::Down { .. } => None,
        }
    }

    /// Whether this offer is what a surface is already pointed at.
    ///
    /// Here rather than beside either caller: the startup door and SETTINGS'
    /// switcher both mark the row a surface is running, and two copies of
    /// "which held pair equals this offer" is two chances for one of them to
    /// start marking the wrong one — the same reason `offers` itself is one
    /// producer for three surfaces.
    ///
    /// The owner's own spelling on both sides. This compares two answers it
    /// gave, never an answer against something typed here, so nothing is folded.
    pub(crate) fn running(&self, store: &Store, surface: &str) -> bool {
        let Some(ModelChoice::Pair { backend, model }) = self.choice() else {
            return false;
        };
        let held = match store.llm() {
            Some(llm) => match surface {
                WORKFORCE => llm.workforce.as_ref(),
                _ => llm.reasoner.as_ref(),
            },
            None => return false,
        };
        let Some(held) = held else {
            return false;
        };
        crate::format::text(held.backend.as_ref()) == Some(backend.as_str())
            && crate::format::text(held.model.as_ref()) == Some(model.as_str())
    }

    /// Whether a typed word names this offer.
    ///
    /// **The backend word is matched case-blind and the model id is not.** A
    /// backend is a name this client already spells for the operator — it is on
    /// the strip, in the owner's reason, on the MODELS card — so `OLLAMA`
    /// naming `ollama` costs nothing and buys the one thing that matters here:
    /// a down backend typed in the wrong case reaches its own entry and comes
    /// back with *the owner's reason*, where an exact compare fell through to
    /// the hand-typed path and answered "OLLAMA names no model" about a daemon
    /// the desk had already explained.
    ///
    /// A model id stays exact. `qwen2.5:7b` and `granite3.3:8b` are tags a
    /// daemon holds byte for byte, and a client that folded their case would be
    /// deciding, on the owner's behalf, that two different tags are one.
    ///
    /// What is *sent* is always the offer's own spelling, never what was typed.
    fn matches(&self, typed: &str) -> bool {
        match self {
            // A bare backend name, whichever variant carries it: the down entry
            // names no model, and the workforce's claude entry is the backend
            // alone because the tier map owns its model.
            Offer::Down { value, .. } => value.eq_ignore_ascii_case(typed),
            Offer::Runs {
                value,
                backend,
                model,
            } => match typed.split_once(':') {
                Some((named, id)) => backend.eq_ignore_ascii_case(named) && model == id,
                None => value.eq_ignore_ascii_case(typed),
            },
        }
    }
}

// -- the strip --------------------------------------------------------------

/// What the line could become from here — the one-line strip above the input.
///
/// A pure function of (state, desk, posture), like everything else on this
/// surface. Values come off the store rather than out of a list this file
/// keeps, so the strip can never offer a symbol the desk is not watching or a
/// plan the owner is not serving.
pub fn suggestions(state: &CmdState, store: &Store, posture: Posture) -> Vec<Suggestion> {
    match state {
        CmdState::Empty => scopes(posture),
        CmdState::Picker { matches, .. } => matches
            .iter()
            .filter(|scope| !scope.writes() || posture.writes())
            .map(|scope| Suggestion::offered(format!("/{}", scope.word())))
            .collect(),
        CmdState::Scoped { scope, query } => values(*scope, query.trim(), store, posture),
        CmdState::Verb(Verb::Ticker(symbol)) => values(Scope::Ticker, symbol, store, posture),
        CmdState::Unknown(_) => Vec::new(),
    }
}

/// One thing the line could become, and why it could not.
///
/// A type rather than a string because one scope has something to say about a
/// value it is still going to show. A backend the desk cannot reach is left in
/// the strip — hiding it reads as a desk that never had one — and the sentence
/// beside it is the owner's own, which is also the sentence submitting it gets
/// back. Every other scope offers nothing but choices, so `refusal` is `None`
/// everywhere else.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Suggestion {
    /// What the line becomes when this one is accepted.
    pub value: String,
    /// Why it cannot be chosen — `None` for everything the desk can serve.
    pub refusal: Option<String>,
}

impl Suggestion {
    pub fn offered(value: impl Into<String>) -> Suggestion {
        Suggestion {
            value: value.into(),
            refusal: None,
        }
    }

    /// Whether accepting it can lead anywhere. Tab takes the first one this is
    /// true of, so a key can never paste a line the desk has already refused.
    pub fn choosable(&self) -> bool {
        self.refusal.is_none()
    }
}

fn scopes(posture: Posture) -> Vec<Suggestion> {
    Scope::ALL
        .into_iter()
        .filter(|scope| !scope.writes() || posture.writes())
        .map(|scope| Suggestion::offered(format!("/{}", scope.word())))
        .collect()
}

fn values(scope: Scope, query: &str, store: &Store, posture: Posture) -> Vec<Suggestion> {
    match scope {
        // Takes nothing; the hint row already says Enter acts.
        Scope::Clear => Vec::new(),
        // Neither has anything to offer: `/cli` takes no argument, and what to
        // build is a sentence only the operator has. The hint row carries both.
        Scope::Cli | Scope::Build => Vec::new(),
        // What can be approved: pending requests first, then checked plans
        // with no request yet — the two things the word answers.
        Scope::Approve => {
            let wanted = query.to_ascii_lowercase();
            let mut out: Vec<Suggestion> = store
                .approvals()
                .iter()
                .filter(|a| a.status.as_deref() == Some("pending"))
                .filter_map(|a| a.approval_id.clone())
                .filter(|id| id.to_ascii_lowercase().starts_with(&wanted))
                .map(Suggestion::offered)
                .collect();
            out.extend(
                store
                    .plans()
                    .iter()
                    .filter(|p| p.state.as_deref() == Some("checked"))
                    .filter_map(|p| p.plan_id.clone())
                    .filter(|id| store.approval_for(id).is_none())
                    .filter(|id| id.to_ascii_lowercase().starts_with(&wanted))
                    .map(Suggestion::offered),
            );
            out
        }
        Scope::Execute => {
            let wanted = query.to_ascii_lowercase();
            store
                .plans()
                .iter()
                .filter(|p| p.state.as_deref() == Some("checked"))
                .filter_map(|p| p.plan_id.clone())
                .filter(|id| store.covering_approval(id).is_some())
                .filter(|id| id.to_ascii_lowercase().starts_with(&wanted))
                .map(Suggestion::offered)
                .collect()
        }
        Scope::View => ViewId::ALL
            .into_iter()
            .filter(|id| starts_with_fold(id.label(), query))
            .map(|id| Suggestion::offered(id.label()))
            .collect(),
        Scope::Ticker => {
            let wanted = query.to_ascii_uppercase();
            store
                .universe()
                .into_iter()
                .filter(|t| starts_with_fold(t, &wanted))
                .map(Suggestion::offered)
                .collect()
        }
        Scope::Plan => {
            let wanted = query.to_ascii_lowercase();
            store
                .plans()
                .iter()
                .filter_map(|plan| crate::format::text(plan.plan_id.as_ref()))
                .filter(|id| id.to_ascii_lowercase().starts_with(&wanted))
                .map(Suggestion::offered)
                .collect()
        }
        // Hidden rather than shown-and-refused: an operator affordance is
        // absent from an unarmed window everywhere else on this workstation,
        // and a strip that offered one would teach that the posture is chrome.
        Scope::Mode if !posture.writes() => Vec::new(),
        Scope::Mode => DESK_MODES
            .into_iter()
            .map(|(data, book)| Suggestion::offered(format!("{data} {book}")))
            .filter(|pair| starts_with_fold(&pair.value, query))
            .collect(),
        Scope::Model if !posture.writes() => Vec::new(),
        // The whole argument, both words, exactly as `Scope::Mode` offers both
        // halves of a desk mode: accepting a value replaces everything after
        // the scope word, so a suggestion that carried only the second half
        // would rewrite the line without the surface it was about.
        Scope::Model => {
            let mut out = Vec::new();
            for surface in SURFACES {
                for offer in offers(surface, store) {
                    out.push(Suggestion {
                        value: format!("{surface} {}", offer.value()),
                        refusal: offer.refusal().map(str::to_string),
                    });
                }
                // The flag, and only where the desk has one: the owner refuses
                // `enabled` on the workforce, so offering it there would be
                // this client advertising a switch that does not exist.
                if surface == REASONER {
                    out.extend(SWITCH.map(|flag| Suggestion::offered(format!("{surface} {flag}"))));
                }
            }
            out.retain(|choice| starts_with_fold(&choice.value, query));
            out
        }
        // Nothing to offer and nothing to hide: the scope takes no argument, so
        // the strip carries only `Scope::hint`'s own line saying so.
        Scope::Ask => Vec::new(),
        Scope::Do if !posture.writes() => Vec::new(),
        // Every proposal the owner served, in the owner's own order — the
        // refused ones shown carrying their reason rather than hidden, exactly
        // as `/model` shows a backend the desk cannot reach. An item that
        // vanished from the strip would read as a desk that never proposed it,
        // and "why not" is the question the sentence answers.
        Scope::Do => store
            .actionables()
            .iter()
            .filter_map(|item| {
                let value = crate::format::text(item.template_id.as_ref())?.to_string();
                let refusal = approvable(item, &value).err();
                Some(Suggestion { value, refusal })
            })
            .filter(|choice| starts_with_fold(&choice.value, query))
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
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
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

    /// A desk holding one checked plan with no request, one checked plan
    /// with a pending request, and one with an approved-unspent approval.
    #[cfg(feature = "operator")]
    fn desk_with_plans() -> Store {
        use crate::bus::AppEvent;
        let mut store = Store::new(std::time::Duration::from_secs(9));
        let snap: crate::model::Snapshot = serde_json::from_str(
            r#"{"plans": [
                    {"plan_id": "aaaa1111bbbb2222", "state": "checked"},
                    {"plan_id": "cccc3333dddd4444", "state": "checked"},
                    {"plan_id": "eeee5555ffff6666", "state": "checked"}],
                "approvals": [
                    {"approval_id": "pend0001", "plan_id": "cccc3333dddd4444",
                     "status": "pending", "targets_hash": "0123456789abcdef"},
                    {"approval_id": "appr0002", "plan_id": "eeee5555ffff6666",
                     "status": "approved", "targets_hash": "fedcba9876543210"}]}"#,
        )
        .unwrap();
        store.apply(
            AppEvent::Snapshot(Box::new(snap)),
            std::time::Instant::now(),
        );
        store
    }

    #[cfg(feature = "operator")]
    #[test]
    fn approve_answers_the_two_things_the_word_can_mean() {
        let store = desk_with_plans();
        // A pending request, by prefix: the box opens on it.
        assert_eq!(
            resolve(&parse("/approve pend"), &store, Posture::Operator),
            Resolved::OpenApprove("pend0001".into())
        );
        // A checked plan with no request: the desk is asked to open one.
        assert_eq!(
            resolve(&parse("/approve aaaa"), &store, Posture::Operator),
            Resolved::RequestApproval("aaaa1111bbbb2222".into())
        );
        // Already approved: the word is /execute, and the refusal says so.
        match resolve(&parse("/approve eeee"), &store, Posture::Operator) {
            Resolved::Refused(said) => assert!(said.contains("/execute eeee5555"), "{said}"),
            other => panic!("{other:?}"),
        }
        // Nothing this desk holds: named, not silently nothing.
        assert!(matches!(
            resolve(&parse("/approve zzzz"), &store, Posture::Operator),
            Resolved::Refused(_)
        ));
        // Glass windows are told why, not offered a box they cannot answer.
        assert!(matches!(
            resolve(&parse("/approve pend"), &store, Posture::Glass),
            Resolved::Refused(_)
        ));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn execute_needs_the_approval_on_record_and_says_so_when_it_is_missing() {
        let store = desk_with_plans();
        assert_eq!(
            resolve(&parse("/execute eeee"), &store, Posture::Operator),
            Resolved::OpenExecute("eeee5555ffff6666".into())
        );
        // The silent-x case, now a sentence with the remedy in it.
        match resolve(&parse("/execute aaaa"), &store, Posture::Operator) {
            Resolved::Refused(said) => {
                assert!(said.contains("no approval on record"), "{said}");
                assert!(said.contains("/approve aaaa1111"), "{said}");
            }
            other => panic!("{other:?}"),
        }
        // Pending is not approved: still no covering approval to book on.
        assert!(matches!(
            resolve(&parse("/execute cccc"), &store, Posture::Operator),
            Resolved::Refused(_)
        ));
    }

    #[test]
    fn clear_is_offered_to_every_posture_and_takes_no_argument() {
        // Local like /view: it empties this window's pane and touches nothing
        // on the desk, so a glass window may use it — and an argument is
        // refused rather than ignored, because a swallowed word is a command
        // the operator believes did something else.
        let store = desk();
        assert_eq!(
            resolve(&parse("/clear "), &store, Posture::Glass),
            Resolved::ClearChat
        );
        assert!(matches!(
            resolve(&parse("/clear everything"), &store, Posture::Glass),
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

    // -- the ask scope ------------------------------------------------------

    #[cfg(feature = "operator")]
    #[test]
    fn asking_takes_no_argument_and_an_armed_window_gets_the_ask() {
        let store = desk();
        assert_eq!(
            resolve(&parse("/ask "), &store, Posture::Operator),
            Resolved::Ask
        );
        // A word after it can only be an operator meaning something this does
        // not do — one template, or a question in prose, which is ATLAS's own
        // row. Sending the ask anyway would answer a question nobody put.
        match resolve(&parse("/ask regime_review"), &store, Posture::Operator) {
            Resolved::Refused(said) => assert!(said.contains("no argument"), "{said}"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn an_unarmed_window_may_not_ask_and_is_not_offered_the_scope() {
        // An ask mints proposal rows in the desk's queue, so it is a write and
        // is gated exactly like `/mode`, `/model` and `/do`: hidden from the
        // strip, and refused for being unarmed when it is typed in full.
        let store = desk();
        match resolve(&parse("/ask "), &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(said.contains("not armed"), "{said}"),
            other => panic!("{other:?}"),
        }
        assert!(suggestions(&parse("/a"), &store, Posture::Glass).is_empty());
        // And it offers no values of its own: the strip carries the scope's
        // hint instead, which says there is nothing to type.
        assert!(suggestions(&parse("/ask "), &store, Posture::Glass).is_empty());
        #[cfg(feature = "operator")]
        {
            assert!(suggestions(&parse("/a"), &store, Posture::Operator)
                .iter()
                .any(|choice| choice.value == "/ask"));
            assert!(suggestions(&parse("/ask "), &store, Posture::Operator).is_empty());
        }
    }

    // -- the approval scope -----------------------------------------------

    /// A desk holding today's proposals, in the three shapes the owner serves.
    ///
    /// `startable` is `null` on the offered one because that is the only thing
    /// `UISession.atlas_actionables_snapshot` can say — `true` is unreachable
    /// there, and the verdict lives at the POST it names in its own reason.
    /// The refusal sentence is the owner's own, copied from
    /// `qlab/operator/templates.py::check_authority`.
    fn desk_with_proposals() -> Store {
        let mut store = desk();
        let mut snapshot = store.snapshot.take().unwrap();
        snapshot.actionables = Some(
            serde_json::from_value(serde_json::json!({"items": [
                {"template_id": "regime_review",
                 "purpose": "Re-read the regime panel.",
                 "startable": null, "reason": "the data preconditions were not \
                  checked here; POST /api/atlas/actionables asks the gate for \
                  today's verdict",
                 "task_id": "8f21a0c4de3b1157", "task_status": "queued"},
                // Refused **and** carrying a task id, which is the shape the
                // owner actually serves: the task exists, it is simply not
                // startable. An item with no id would be refused by the second
                // rule whatever the first one said, which is a fixture that
                // cannot tell the two apart.
                {"template_id": "desk_rebalance_review",
                 "purpose": "Propose a rebalance.",
                 "startable": false,
                 "reason": "desk_rebalance_review creates a paper plan, which \
                            requires Propose mode; Atlas is in Research",
                 "task_id": "3b7e05c19a4d6612", "task_status": "running"},
                // The owner mints the row before it serves it, so this shape is
                // a contract failure rather than a state — and it is exactly
                // what a fall-back to the template id would start blind.
                {"template_id": "desk_brief", "purpose": "Write the brief.",
                 "startable": null, "reason": null,
                 "task_id": "", "task_status": "queued"},
                // The other contract failure: refused, with no sentence. The
                // owner attaches one to every refusal it makes, so this cannot
                // arrive from it — but a proxy in front of the desk answers
                // with whatever it likes, and the arm that says which silence
                // this is has to be reachable to be trusted.
                {"template_id": "news_scan", "purpose": "Read the tape.",
                 "startable": false, "reason": null,
                 "task_id": "c41d90fe27ba8836", "task_status": "queued"}
            ]}))
            .unwrap(),
        );
        store.apply(AppEvent::Snapshot(Box::new(snapshot)), Instant::now());
        store
    }

    #[cfg(feature = "operator")]
    #[test]
    fn approving_names_the_task_the_owner_served_not_the_template() {
        // The client must not invent an id: what is approved is the persisted
        // task the owner bound to this proposal, and the template id is only
        // the word an operator can read off the panel.
        let store = desk_with_proposals();
        assert_eq!(
            resolve(&parse("/do regime_review"), &store, Posture::Operator),
            Resolved::Approve {
                template: "regime_review".into(),
                task: "8f21a0c4de3b1157".into(),
            }
        );
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_refused_item_cannot_be_approved_and_says_so_in_the_owners_words() {
        let store = desk_with_proposals();
        match resolve(
            &parse("/do desk_rebalance_review"),
            &store,
            Posture::Operator,
        ) {
            Resolved::Refused(said) => assert!(said.contains("Propose mode"), "{said}"),
            other => panic!("a refused proposal must not resolve: {other:?}"),
        }
        // And a refusal the owner attached no sentence to is still a refusal,
        // saying which silence it met rather than borrowing the reason of the
        // item beside it or offering the proposal anyway.
        match resolve(&parse("/do news_scan"), &store, Posture::Operator) {
            Resolved::Refused(said) => {
                assert_eq!(said, "the desk refused news_scan and did not say why")
            }
            other => panic!("a refusal with no sentence must still refuse: {other:?}"),
        }
        // And it is not offered either — a refused item is on the strip
        // carrying its reason, which is what the `/model` scope does with a
        // backend the desk cannot reach.
        let strip = suggestions(&parse("/do desk_re"), &store, Posture::Operator);
        assert_eq!(strip.len(), 1, "{strip:?}");
        assert!(!strip[0].choosable(), "{strip:?}");
    }

    #[cfg(feature = "operator")]
    #[test]
    fn an_item_the_owner_served_no_task_for_is_refused_rather_than_started_by_name() {
        // `task_id` is optional on the wire and `Some("")` is absent. Falling
        // back to the template id would POST /api/atlas/tasks/desk_brief/start
        // — a 404 at best, and at worst somebody else's task.
        let store = desk_with_proposals();
        match resolve(&parse("/do desk_brief"), &store, Posture::Operator) {
            Resolved::Refused(said) => {
                assert!(said.contains("desk_brief"), "{said}");
                assert!(said.contains("no task"), "{said}");
            }
            other => panic!("an item with no task must not resolve: {other:?}"),
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_proposal_this_desk_is_not_offering_is_named_rather_than_sent() {
        let store = desk_with_proposals();
        match resolve(&parse("/do portfolio_review"), &store, Posture::Operator) {
            Resolved::Refused(said) => assert!(said.contains("portfolio_review"), "{said}"),
            other => panic!("{other:?}"),
        }
        // A prefix is not a name. This is the one scope whose value starts
        // work, and today's list grows all day — `/do desk` was unambiguous
        // this morning and names two proposals by the afternoon.
        match resolve(&parse("/do regime"), &store, Posture::Operator) {
            Resolved::Refused(said) => assert!(said.contains("regime"), "{said}"),
            other => panic!("a prefix must not start work: {other:?}"),
        }
        // An empty scope is a prompt, not an error: the strip is already
        // listing what the desk would do.
        assert!(matches!(
            resolve(&parse("/do "), &store, Posture::Operator),
            Resolved::Refused(_)
        ));
    }

    #[test]
    fn a_glass_window_is_not_offered_the_approval_scope() {
        // The posture, not the feature: `/do` writes, so an unarmed window is
        // refused for being unarmed whatever it typed — and is offered nothing
        // to type, exactly as with `/mode` and `/model`.
        let store = desk_with_proposals();
        match resolve(&parse("/do regime_review"), &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(said.contains("GLASS"), "{said}"),
            other => panic!("{other:?}"),
        }
        assert!(suggestions(&parse("/do "), &store, Posture::Glass).is_empty());
        assert!(!offered(&parse("/"), &store, Posture::Glass).contains(&"/do".to_string()));
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_approval_strip_offers_what_the_desk_is_holding_and_why_it_cannot() {
        let store = desk_with_proposals();
        let strip = suggestions(&parse("/do "), &store, Posture::Operator);
        assert_eq!(
            strip.iter().map(|s| s.value.clone()).collect::<Vec<_>>(),
            vec![
                "regime_review",
                "desk_rebalance_review",
                "desk_brief",
                "news_scan"
            ],
            "every proposal is on the strip, in the owner's own order"
        );
        assert_eq!(strip[0].refusal, None, "{strip:?}");
        for refused in &strip[1..] {
            assert!(refused.refusal.is_some(), "{refused:?}");
            assert!(
                !refused.choosable(),
                "a key must not accept it: {refused:?}"
            );
        }
        // And an owner sentence is bounded here like every other.
        let flooded = {
            let mut store = desk_with_proposals();
            let mut snapshot = store.snapshot.take().unwrap();
            let items = &mut snapshot.actionables.as_mut().unwrap().items;
            items[1].reason = Some("a ".repeat(400));
            store.apply(AppEvent::Snapshot(Box::new(snapshot)), Instant::now());
            store
        };
        let said = suggestions(&parse("/do desk_re"), &flooded, Posture::Operator)[0]
            .refusal
            .clone()
            .unwrap();
        assert!(said.ends_with('…'), "{said}");
        assert!(said.chars().count() <= REASON_MAX + 1, "{said}");
    }

    // -- the strip --------------------------------------------------------

    /// The strip's values, without the sentences one scope attaches to them.
    fn offered(state: &CmdState, store: &Store, posture: Posture) -> Vec<String> {
        suggestions(state, store, posture)
            .into_iter()
            .map(|choice| choice.value)
            .collect()
    }

    #[test]
    fn the_picker_offers_the_write_scopes_only_to_a_window_that_can_use_them() {
        let store = desk();
        let glass = offered(&parse("/"), &store, Posture::Glass);
        assert!(glass.contains(&"/view".to_string()), "{glass:?}");
        assert!(glass.contains(&"/ticker".to_string()), "{glass:?}");
        assert!(glass.contains(&"/plan".to_string()), "{glass:?}");
        for hidden in ["/mode", "/model"] {
            assert!(
                !glass.contains(&hidden.to_string()),
                "an operator affordance is hidden on glass: {glass:?}"
            );
        }
        // And the two scopes the owner serves no route for are absent from the
        // grammar entirely — see `Scope::ALL`.
        for absent in ["/halt", "/resume"] {
            assert!(!glass.iter().any(|s| s == absent), "{glass:?}");
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn an_armed_picker_offers_the_write_scopes() {
        let store = desk();
        let armed = offered(&parse("/"), &store, Posture::Operator);
        assert!(armed.contains(&"/mode".to_string()), "{armed:?}");
        assert!(armed.contains(&"/model".to_string()), "{armed:?}");
    }

    #[test]
    fn the_strip_offers_what_the_desk_actually_holds() {
        let store = desk();
        assert_eq!(
            offered(&parse("/ticker S"), &store, Posture::Glass),
            vec!["SPY".to_string(), "SOXX".to_string()]
        );
        // The book's own holding is offered too, after the quoted universe.
        assert!(offered(&parse("/ticker "), &store, Posture::Glass).contains(&"TLT".to_string()));
        assert_eq!(
            offered(&parse("/view b"), &store, Posture::Glass),
            vec!["BOOK".to_string()]
        );
        assert_eq!(
            offered(&parse("/plan 9"), &store, Posture::Glass),
            vec!["9f3ac1d20b7e4a51".to_string()]
        );
        assert!(offered(&parse("/mode "), &store, Posture::Glass).is_empty());
        assert!(offered(&parse("/model "), &store, Posture::Glass).is_empty());
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_mode_strip_offers_the_pairs_the_owner_can_make() {
        let store = desk();
        assert_eq!(
            offered(&parse("/mode "), &store, Posture::Operator),
            vec![
                "synthetic simulated".to_string(),
                "live simulated".to_string(),
                "live alpaca".to_string()
            ]
        );
    }

    // -- the model scope --------------------------------------------------

    /// A desk that has fetched a catalog: claude serving its four tiers, and
    /// one ollama daemon holding one model. The live shape, verbatim — see
    /// `tests/fixtures/llm_backends.json`.
    fn desk_with_backends(ollama: serde_json::Value) -> Store {
        let mut store = desk();
        let catalog = serde_json::from_value(serde_json::json!({
            "backends": [
                {"name": "claude", "available": true,
                 "reason": "claude CLI at /Users/azainmac/.local/bin/claude",
                 "models": ["inherit", "sonnet", "opus", "haiku"]},
                ollama
            ],
            "probed_at": "2026-08-03T04:10:08.417505+00:00"
        }))
        .unwrap();
        store.apply(AppEvent::Backends(catalog), Instant::now());
        store
    }

    /// The daemon as it answers when it is running and holding one model.
    fn ollama_up() -> serde_json::Value {
        serde_json::json!({"name": "ollama", "available": true,
                           "reason": "ollama at 127.0.0.1:11434, 1 model pulled",
                           "models": ["qwen2.5:7b"]})
    }

    /// And as it answers when it is not. The owner's own sentence, captured
    /// from a live desk pointed at a port with nothing on it.
    ///
    /// Only the armed leg has a scope to offer it to — a glass window resolves
    /// `/model` to the posture sentence before the catalog is ever consulted.
    #[cfg(feature = "operator")]
    fn ollama_down() -> serde_json::Value {
        serde_json::json!({"name": "ollama", "available": false,
                           "reason": "ollama is not running at http://127.0.0.1:11499 — \
                                      start it with `ollama serve`",
                           "models": []})
    }

    #[cfg(feature = "operator")]
    fn model_line(line: &str, store: &Store) -> Resolved {
        resolve(&parse(line), store, Posture::Operator)
    }

    #[cfg(feature = "operator")]
    fn pair(backend: &str, model: &str) -> ModelChoice {
        ModelChoice::Pair {
            backend: backend.into(),
            model: model.into(),
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_model_line_reads_as_a_surface_and_a_pair_split_at_the_first_colon() {
        // The whole grammar of the scope, as a table. The colon rule is the one
        // that would look right and be wrong: a granite or qwen id carries its
        // own colon, so splitting anywhere but the first would name a backend
        // called `ollama:qwen2.5`.
        let store = desk_with_backends(ollama_up());
        let cases: Vec<(&str, &str, ModelChoice)> = vec![
            (
                "/model reasoner ollama:qwen2.5:7b",
                "reasoner",
                pair("ollama", "qwen2.5:7b"),
            ),
            // Not in this desk's catalog, and sent anyway: the owner is the
            // authority on what it can serve, and a model pulled since the last
            // fetch is one this client has never heard of.
            (
                "/model reasoner ollama:granite3.3:8b",
                "reasoner",
                pair("ollama", "granite3.3:8b"),
            ),
            (
                "/model reasoner claude:haiku",
                "reasoner",
                pair("claude", "haiku"),
            ),
            // The A3 honesty rule, from the other end: the workforce's own
            // spelling of claude is the bare word, and it sends `inherit` —
            // which is what "the tiers decide" is called.
            (
                "/model workforce claude",
                "workforce",
                pair("claude", "inherit"),
            ),
            ("/model reasoner on", "reasoner", ModelChoice::Enabled(true)),
            (
                "/model reasoner off",
                "reasoner",
                ModelChoice::Enabled(false),
            ),
            // The surface word is matched case-blind and comes back in the
            // owner's spelling; a model id is never touched.
            (
                "/model REASONER OFF",
                "reasoner",
                ModelChoice::Enabled(false),
            ),
            // The switch on the surface that has none is still sent: only the
            // owner may say which surfaces can be switched off, and it does.
            (
                "/model workforce off",
                "workforce",
                ModelChoice::Enabled(false),
            ),
        ];
        for (line, surface, choice) in cases {
            assert_eq!(
                model_line(line, &store),
                Resolved::Model {
                    surface: surface.to_string(),
                    choice,
                },
                "{line}"
            );
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_model_line_that_names_nothing_the_desk_has_says_which_half_is_wrong() {
        let store = desk_with_backends(ollama_up());
        for (line, expected) in [
            ("/model ", "surface and a model"),
            ("/model reasoner", "surface and a model"),
            ("/model reasoner ollama:qwen2.5:7b extra", "two words"),
            // The surfaces are the grammar of this scope, like the nav rail's
            // labels are `/view`'s — a fixed vocabulary this client holds — so
            // an unknown one is answered here rather than sent.
            (
                "/model banana claude:haiku",
                "no model surface is called banana",
            ),
            // A backend with no model names nothing that can run — and half a
            // pair is the same absence with a colon in it, which the owner
            // would answer with "a model choice needs both a backend and a
            // model". Answered here instead: it is the form of the line, not a
            // fact about the desk.
            ("/model reasoner ollama", "names no model"),
            ("/model reasoner claude", "names no model"),
            ("/model reasoner ollama:", "names no model"),
            ("/model reasoner :qwen2.5:7b", "names no model"),
        ] {
            match model_line(line, &store) {
                Resolved::Refused(said) => assert!(said.contains(expected), "{line}: {said}"),
                other => panic!("{line}: {other:?}"),
            }
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_backend_the_desk_cannot_reach_is_refused_in_the_owners_own_sentence() {
        // The bare backend name is what the strip offers for a daemon that is
        // down — it has no models to pair with — and submitting it gets back
        // the catalog's own reason rather than a second opinion composed here.
        let store = desk_with_backends(ollama_down());
        match model_line("/model reasoner ollama", &store) {
            Resolved::Refused(said) => {
                assert_eq!(
                    said,
                    "ollama is not running at http://127.0.0.1:11499 — start it with \
                     `ollama serve`"
                );
            }
            other => panic!("{other:?}"),
        }
        // And naming a pair on that same backend is still *sent*: the owner
        // accepts a choice staged for a surface that is switched off, and a
        // client that refused it would block the one way to set a daemon up
        // before starting it.
        assert_eq!(
            model_line("/model reasoner ollama:granite3.3:8b", &store),
            Resolved::Model {
                surface: "reasoner".into(),
                choice: pair("ollama", "granite3.3:8b"),
            }
        );
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_backend_named_in_the_wrong_case_still_gets_the_owners_reason_and_a_model_id_does_not() {
        // D2's fold. The lookup was an exact compare, so `OLLAMA` missed the
        // catalog entry that had the answer and fell through to the hand-typed
        // path — which answered "OLLAMA names no model" about a daemon the desk
        // had already explained. The backend word is one this client spells for
        // the operator everywhere; folding its case costs nothing.
        let store = desk_with_backends(ollama_down());
        assert_eq!(
            model_line("/model reasoner OLLAMA", &store),
            model_line("/model reasoner ollama", &store),
            "the same daemon answered two different ways"
        );
        // A model id is not a word this client spells: `qwen2.5:7b` is a tag a
        // daemon holds byte for byte, so the case stays exact and a mismatched
        // one goes to the owner to be refused rather than being quietly read as
        // the tag beside it.
        let up = desk_with_backends(ollama_up());
        assert_eq!(
            model_line("/model reasoner OLLAMA:qwen2.5:7b", &up),
            Resolved::Model {
                surface: "reasoner".into(),
                choice: pair("ollama", "qwen2.5:7b"),
            },
            "the offer's own spelling is what gets sent"
        );
        assert_eq!(
            model_line("/model reasoner ollama:QWEN2.5:7B", &up),
            Resolved::Model {
                surface: "reasoner".into(),
                choice: pair("ollama", "QWEN2.5:7B"),
            },
            "a model id was folded into one the catalog happened to hold"
        );
    }

    #[test]
    fn an_unarmed_window_refuses_the_model_scope_rather_than_ignoring_it() {
        let store = desk_with_backends(ollama_up());
        match resolve(&parse("/model reasoner on"), &store, Posture::Glass) {
            Resolved::Refused(said) => assert!(said.contains("GLASS"), "{said}"),
            other => panic!("{other:?}"),
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn the_model_strip_offers_the_catalog_and_never_a_tier_the_workforce_would_ignore() {
        let store = desk_with_backends(ollama_up());
        assert_eq!(
            offered(&parse("/model "), &store, Posture::Operator),
            vec![
                "reasoner claude:inherit".to_string(),
                "reasoner claude:sonnet".to_string(),
                "reasoner claude:opus".to_string(),
                "reasoner claude:haiku".to_string(),
                "reasoner ollama:qwen2.5:7b".to_string(),
                "reasoner on".to_string(),
                "reasoner off".to_string(),
                // One entry for claude, not four: the tier map owns the model
                // on the workforce path, so `claude:haiku` would be a choice
                // the desk would not make.
                "workforce claude".to_string(),
                "workforce ollama:qwen2.5:7b".to_string(),
            ]
        );
        // And the switch is offered only where the desk has one.
        assert!(
            !offered(&parse("/model workforce "), &store, Posture::Operator)
                .iter()
                .any(|value| value.ends_with(" on") || value.ends_with(" off"))
        );
        // A model this desk never pulled is not on the strip, whatever the
        // owner would make of it: the client offers only what the catalog
        // serves.
        assert!(offered(
            &parse("/model reasoner ollama:g"),
            &store,
            Posture::Operator
        )
        .is_empty());
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_backend_that_cannot_serve_stays_on_the_strip_carrying_its_reason() {
        // Never hidden. A backend that vanished from the strip would read as a
        // desk that never had one, and the operator's next question — why not —
        // is exactly what the sentence answers.
        let store = desk_with_backends(ollama_down());
        // `oll` rather than `o`, which the reasoner's own `on`/`off` also
        // answer to — the pin is about the backend row, not about the switch.
        let strip = suggestions(&parse("/model reasoner oll"), &store, Posture::Operator);
        let down = strip
            .iter()
            .find(|choice| choice.value == "reasoner ollama")
            .unwrap_or_else(|| panic!("the down backend left the strip: {strip:?}"));
        assert_eq!(
            down.refusal.as_deref(),
            Some(
                "ollama is not running at http://127.0.0.1:11499 — start it with \
                 `ollama serve`"
            )
        );
        assert!(!down.choosable(), "a key must not accept it: {down:?}");
        // No pair is offered on it either — the owner asks an unavailable
        // backend for no model list, so there is nothing to pair.
        assert_eq!(strip.len(), 1, "{strip:?}");
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_backend_that_says_it_serves_and_names_nothing_is_a_contract_failure_not_a_gap() {
        // `available: true` with an empty list is a shape the owner does not
        // produce — a daemon with nothing pulled reports *unavailable*, with
        // the pull command in its reason. If it ever does, the backend still
        // occupies a row and says which kind of silence it is, rather than
        // disappearing or borrowing the happy-path sentence beside it.
        let store = desk_with_backends(serde_json::json!({
            "name": "ollama", "available": true,
            "reason": "ollama at 127.0.0.1:11434, 1 model pulled", "models": []
        }));
        let strip = suggestions(&parse("/model reasoner oll"), &store, Posture::Operator);
        assert_eq!(strip.len(), 1, "{strip:?}");
        assert_eq!(
            strip[0].refusal.as_deref(),
            Some("the owner says ollama can serve and never said what")
        );
        // And the other silence: a backend that cannot serve and did not say
        // why. The owner populates `reason` on every entry, so this is a
        // contract failure too — and it says so rather than showing a row with
        // an empty half. `Some("")` is absent here as everywhere.
        for quiet in [
            serde_json::json!({"name": "ollama", "available": false, "models": []}),
            serde_json::json!({"name": "ollama", "available": false, "reason": "",
                               "models": []}),
        ] {
            let store = desk_with_backends(quiet);
            let strip = suggestions(&parse("/model reasoner oll"), &store, Posture::Operator);
            assert_eq!(
                strip[0].refusal.as_deref(),
                Some("the owner did not say why ollama cannot serve")
            );
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn a_desk_that_has_not_asked_what_it_serves_offers_nothing_rather_than_guessing() {
        // The catalog is fetched when the palette enters this scope; until it
        // arrives there is nothing to offer, and inventing `claude:sonnet` from
        // a constant would be this client asserting a backend the owner never
        // mentioned.
        let store = desk();
        assert!(store.backends().is_none());
        let strip = offered(&parse("/model "), &store, Posture::Operator);
        assert_eq!(
            strip,
            vec!["reasoner on".to_string(), "reasoner off".to_string()],
            "only the switch, which is the desk's own grammar and not a backend"
        );
    }

    #[cfg(feature = "operator")]
    #[test]
    fn an_owner_sentence_on_the_strip_is_bounded_like_every_other() {
        // Nothing on the wire is guaranteed to be the owner's: a proxy in front
        // of the desk answers with a page of its own, and a one-row strip is
        // not where that should be discovered.
        let store = desk_with_backends(serde_json::json!({
            "name": "ollama", "available": false,
            "reason": "a ".repeat(400), "models": []
        }));
        let strip = suggestions(&parse("/model reasoner oll"), &store, Posture::Operator);
        let said = strip[0].refusal.clone().unwrap();
        assert!(said.ends_with('…'), "{said}");
        assert!(said.chars().count() <= REASON_MAX + 1, "{said}");
    }
}
