//! Key routing: one place decides what a keypress means in the current mode.
//!
//! The table below is that place, in the only sense that matters to an
//! operator: every key this workstation binds has a row here, and the help
//! overlay is generated from it. A binding cannot exist without appearing in
//! help — not by convention, but because [`tests::every_binding_the_router_has_is_in_the_keymap`]
//! reads the router's own source and refuses any `KeyCode` it cannot find a row
//! for, and any row whose `KeyCode` no router mentions.
//!
//! It is a table of *facts about* the routers rather than the routers
//! themselves. Matching on it directly would mean a `&'static str` in the key
//! path and a lookup that can miss, where today the match arms are compiled
//! patterns; what the table buys instead is that the two cannot drift, checked
//! mechanically at every `cargo test`.
//!
//! The check is a **multiset** over one *function* per section, not a set over
//! a whole file. Both halves were bought with a hole. A set collapses repeats,
//! so adding `Char(c) if c.is_ascii_uppercase()` beside the digit arm — a live
//! binding that swallows every capital letter, with no help row — left every
//! test green, because `Char(c)` was already in the set. Guard-differentiated
//! `Char(c)` is how every text field on this client routes, so that is the next
//! binding rather than a corner case. And counting per *file* cannot separate
//! WORKFORCE's question row from its picker, which are two routers with two key
//! sets in one file: the file's `Char(c)`×2 would have to be one row or two,
//! and either way one of the two fields is described by a row about the other.
//!
//! What the check still cannot see, in full:
//!
//! 1. **Modifiers.** It compares `KeyCode` spellings, so Ctrl-C is matched by
//!    its bare `Char('c')`. The modifier lives in `key`, which is what a human
//!    reads, not in the comparison.
//! 2. **Guard text.** Two arms with the same code and different guards are two
//!    occurrences and need two rows, which is the hole above closed — but which
//!    row describes which guard is not checked. A row can still say the wrong
//!    condition about the right key.
//! 3. **Where a region is.** `Source::region` is hand-maintained: a section
//!    pointed at a function that does not route would check nothing. The
//!    positive controls below are what keep that from being silent.
//! 4. **Literal braces.** The region scanner understands string and character
//!    literals well enough for this crate; a stranger one could still confuse
//!    its brace balance, and it panics rather than guessing.
//! 5. **Comments count.** The scrape is a plain text search over the routing
//!    function, so a comment inside one may not spell a `KeyCode::` variant —
//!    it reads as a binding and demands a row. The same constraint the
//!    never-IO grep puts on `cmd.rs`, and for the same reason: a pin that
//!    could be talked out of a match is a pin that can be talked out of a
//!    match. `markets.rs` learned this one the hard way.
//! 6. **Depth.** Narrowing from a file to a function opened an escape the old
//!    file-wide scrape did not have: a sibling function that binds a key and is
//!    *called* from the routing function is a live binding the section's own
//!    body never mentions. One level of local calls is followed for exactly
//!    that reason — a helper two hops down is still invisible, and closing that
//!    is a call graph rather than a keymap check. Anything reachable from a
//!    router but further than one call away needs a section of its own.

use crate::store::{Posture, ViewId};

/// Which router owns a binding — and therefore which section of the overlay it
/// appears in, and which file the equivalence check reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// The shell's own keys, claimed before any view sees them.
    Shell,
    /// The command line, while it has the keyboard.
    Command,
    /// This overlay.
    Help,
    /// One view's keys, while that view is on screen.
    View(ViewId),
    /// ATLAS's ask row, while it has the keyboard.
    ///
    /// Its own section for the reason WORKFORCE's fields are: the keys that
    /// scroll the conversation and the keys inside the row are two routers,
    /// and one section over both would describe one with rows about the other.
    AtlasAsk,
    /// ATLAS's terminal pane, while the child in it holds the keyboard.
    ///
    /// Its own section because it is the one surface here that outranks the
    /// shell: while it holds the keyboard the global keys are not merely
    /// yielded, they are gone, and a section that described the pane with
    /// ATLAS's rows would be listing keys the operator cannot press. It is also
    /// the shortest section on the overlay, and deliberately — the desk binds
    /// exactly one key inside a pane and hands over every other.
    ///
    /// What the child does with a forwarded key is not a binding of this
    /// workstation and owes no row. The bytes are decided by a codec that holds
    /// no state and returns no command, so there is nothing there that could
    /// bind anything; that the pane takes *everything* is stated on its own
    /// border, on every frame, which is where an operator who has just lost the
    /// keyboard is already looking.
    AtlasPane,
    /// WORKFORCE's question row, while it has the keyboard.
    ///
    /// Its own section rather than part of `View(Workforce)`, because it is its
    /// own router: `ask_key` and `picker_key` bind the same codes to different
    /// things, and one section over both would describe one field with a row
    /// about the other. Splitting them is also what lets the equivalence count
    /// occurrences instead of collapsing them.
    WorkforceAsk,
    /// WORKFORCE's template picker, while it is open.
    WorkforcePicker,
    /// SETTINGS' alpaca login form, while it is open.
    ///
    /// Its own section for the same reason the two WORKFORCE fields are: the
    /// keys that *open* it and the keys *inside* it are two routers, and one
    /// section over both would describe the form with a row about the pane.
    SettingsLogin,
    /// SETTINGS' model switcher, while it is open. The third router in that
    /// file, and the same rule again.
    SettingsModels,
    /// SETTINGS' lane picker, while it is open.
    ///
    /// The same *box* as the switcher above — one widget, two lists — and
    /// still its own section, because it is its own router: `switch_key`
    /// hands the lane list to `lane_key`, and a section that followed it would
    /// count every one of these four keys twice and describe the models list
    /// with rows about the desk. The same split the two WORKFORCE fields have,
    /// for the same mechanical reason.
    SettingsLane,
    /// SETTINGS' EDGAR contact box, while it is open. The fourth router in
    /// that file, and the same rule again.
    SettingsContact,
    /// SETTINGS' method picker, while it is open.
    ///
    /// The same *box* as the model switcher and the lane picker — one widget,
    /// three lists — and still its own section, for the lane picker's exact
    /// mechanical reason: `switch_key` hands this list to `method_key`, and a
    /// section that followed it would count these four keys twice and describe
    /// the models list with rows about the mandate.
    SettingsMethod,
    /// SETTINGS' holdings-cap box, while it is open. The sixth router in that
    /// file, and the same rule again.
    SettingsCap,
    /// PREDICTORS' lane picker, while it is open.
    ///
    /// Its own section for the reason every box on this workstation has one:
    /// `on_key` hands the open box to `picker_key`, and a section that followed
    /// it would describe the pane with rows about a list that is usually not on
    /// screen. Armed windows only — a monitoring one never opens it.
    PredictorsRun,
    /// The confirmation box, which outranks everything but Ctrl-C.
    Confirm,
    /// The startup door, which outranks even that — it is up before there is
    /// anything to confirm.
    Door,
}

impl Source {
    /// Every section, in the order the overlay lists them: the global keys
    /// first, then the two surfaces that take the keyboard, then the views in
    /// nav-rail order, then the box that outranks them all.
    pub const ALL: [Source; 26] = [
        Source::Shell,
        Source::Command,
        Source::Help,
        Source::View(ViewId::Atlas),
        Source::AtlasAsk,
        Source::AtlasPane,
        Source::View(ViewId::Desk),
        Source::View(ViewId::Markets),
        Source::View(ViewId::Book),
        Source::View(ViewId::Research),
        // PRED and its picker. The pane was absent from this list entirely
        // while it bound nothing — `region()` named it and the equivalence
        // check never asked — and a section missing from here is a router
        // nothing compares, which is how a key with no help row would ship.
        Source::View(ViewId::Predictors),
        Source::PredictorsRun,
        Source::View(ViewId::Workforce),
        Source::WorkforceAsk,
        Source::WorkforcePicker,
        Source::View(ViewId::Audit),
        Source::View(ViewId::Settings),
        Source::View(ViewId::Visuals),
        Source::SettingsLogin,
        Source::SettingsModels,
        Source::SettingsLane,
        Source::SettingsContact,
        Source::SettingsMethod,
        Source::SettingsCap,
        Source::Confirm,
        Source::Door,
    ];

    /// The section header.
    pub fn label(self) -> &'static str {
        match self {
            Source::Shell => "anywhere",
            Source::Command => "the command line",
            Source::Help => "this overlay",
            Source::View(id) => id.label(),
            Source::AtlasAsk => "ATLAS · the ask row",
            Source::AtlasPane => "ATLAS · the terminal pane",
            Source::WorkforceAsk => "WORK · the question row",
            Source::WorkforcePicker => "WORK · the template picker",
            Source::SettingsLogin => "SETT · the alpaca login form",
            Source::SettingsModels => "SETT · the model switcher",
            Source::SettingsLane => "SETT · the data lane picker",
            Source::SettingsContact => "SETT · the edgar contact box",
            Source::SettingsMethod => "SETT · the method picker",
            Source::SettingsCap => "SETT · the holdings cap box",
            Source::PredictorsRun => "PRED · the lane picker",
            Source::Confirm => "a confirmation box",
            Source::Door => "the startup door",
        }
    }

    /// The exact routing function this section describes: a file relative to
    /// `src`, an anchor to find it after, and the function's name.
    ///
    /// A *function*, not a file. Two routers in one file (WORKFORCE's question
    /// row and its picker) have to be counted apart or neither can be counted
    /// at all, and reading only the routing function also keeps a `KeyCode`
    /// mentioned anywhere else in the file out of the comparison.
    ///
    /// The anchor is for the case where the name is not unique in a file — two
    /// `impl`s of `on_key`, or a trait declaration above the impl that answers.
    /// No section needs one today (`views/mod.rs`'s placeholder view was the
    /// last, and every view is built now), which is why
    /// [`tests::the_anchor_picks_the_impl_it_names_and_not_the_one_above_it`]
    /// exists: an unexercised branch of this scanner is one that would be
    /// discovered broken by the next file that needs it.
    ///
    /// Data rather than a comment, because it is what the equivalence check
    /// reads — and hand-maintained, which is weakness 3 in this module's own
    /// list: a section pointed at a function that does not route would check
    /// nothing and pass.
    pub fn region(self) -> (&'static str, &'static str, &'static str) {
        match self {
            Source::Shell => ("ui/shell.rs", "", "on_key"),
            Source::Command => ("cmd.rs", "", "edit"),
            Source::Help => ("ui/widgets/help.rs", "", "on_key"),
            // ATLAS's `View::on_key` only forwards; `keys` is the router, and
            // the ask row under it is the section below.
            Source::View(ViewId::Atlas) => ("ui/views/atlas.rs", "", "keys"),
            Source::AtlasAsk => ("ui/views/atlas.rs", "", "ask_key"),
            // The pane's router is the shell's, not the view's, because it has
            // to claim a key *above* the shell — a section in `atlas.rs` would
            // describe a surface reached only after every global key had
            // already been taken. It is a sibling of `on_key` above and so is
            // read apart from it, exactly as the two WORKFORCE fields are.
            Source::AtlasPane => ("ui/shell.rs", "", "pty_key"),
            Source::View(ViewId::Desk) => ("ui/views/desk.rs", "", "on_key"),
            Source::View(ViewId::Markets) => ("ui/views/markets.rs", "", "on_key"),
            Source::View(ViewId::Book) => ("ui/views/book.rs", "", "on_key"),
            // `View::on_key` here only forwards; `keys` is the router, and the
            // two fields under it are the sections below.
            Source::View(ViewId::Workforce) => ("ui/views/workforce.rs", "", "keys"),
            Source::WorkforceAsk => ("ui/views/workforce.rs", "", "ask_key"),
            Source::WorkforcePicker => ("ui/views/workforce.rs", "", "picker_key"),
            Source::View(ViewId::Audit) => ("ui/views/audit.rs", "", "on_key"),
            // RSCH binds nothing: everything on it is read-only and nothing
            // scrolls, so a key pressed there belongs to whoever claims it
            // next. Pointed at its own router rather than at a shared
            // placeholder, which is what would catch a cursor added to it
            // without a help row.
            Source::View(ViewId::Research) => ("ui/views/research.rs", "", "on_key"),
            // PRED's `View::on_key` routes `r` itself and hands an open box to
            // `picker_key`, which is the section below.
            Source::View(ViewId::Predictors) => ("ui/views/predictors.rs", "", "on_key"),
            Source::PredictorsRun => ("ui/views/predictors.rs", "", "picker_key"),
            // SETT's `View::on_key` only forwards; `keys` is the router, and
            // the form under it is the section below.
            Source::View(ViewId::Settings) => ("ui/views/settings.rs", "", "keys"),
            // VIS routes everything from `on_key` itself: two panes, one
            // router, and every key on it a read.
            Source::View(ViewId::Visuals) => ("ui/views/visuals.rs", "", "on_key"),
            Source::SettingsLogin => ("ui/views/settings.rs", "", "form_key"),
            Source::SettingsModels => ("ui/views/settings.rs", "", "switch_key"),
            Source::SettingsLane => ("ui/views/settings.rs", "", "lane_key"),
            Source::SettingsContact => ("ui/views/settings.rs", "", "contact_key"),
            Source::SettingsMethod => ("ui/views/settings.rs", "", "method_key"),
            Source::SettingsCap => ("ui/views/settings.rs", "", "cap_key"),
            Source::Confirm => ("ui/widgets/confirm.rs", "", "on_key"),
            Source::Door => ("ui/door.rs", "", "on_key"),
        }
    }
}

/// One binding.
///
/// Five fields where the plan sketched three. `code` is the machine half — the
/// `KeyCode` spelling the router matches on, which is what makes the
/// equivalence check an equivalence rather than a count — and `writes` is the
/// posture gate, which cannot be folded into the human text without turning the
/// filter into a substring match on prose.
#[derive(Debug, Clone, Copy)]
pub struct Binding {
    /// Verbatim as the router spells it: `Char('q')`, `Tab`, `Char(c)`.
    pub code: &'static str,
    /// What an operator presses, as an operator would say it.
    pub key: &'static str,
    pub source: Source,
    pub action: &'static str,
    /// Whether this key only exists in a window that can change the desk.
    ///
    /// The posture, not the feature: a featured binary the human did not arm
    /// reads GLASS, and the overlay may not offer a key that window refuses.
    pub writes: bool,
}

/// Shorthand for the read-only rows, which are most of them.
const fn b(code: &'static str, key: &'static str, source: Source, action: &'static str) -> Binding {
    Binding {
        code,
        key,
        source,
        action,
        writes: false,
    }
}

/// The same shorthand for a key only an armed window has.
const fn w(code: &'static str, key: &'static str, source: Source, action: &'static str) -> Binding {
    Binding {
        code,
        key,
        source,
        action,
        writes: true,
    }
}

/// Every key this workstation binds.
///
/// One table in both builds, including the rows for keys only an operator build
/// compiles. They are `&'static str`s and carry no capability; keeping them
/// here means the glass leg checks the same equivalence against the same source
/// text, rather than checking a smaller table and passing for the wrong reason.
/// What a *window* is offered is [`bindings`], which filters on the posture.
pub const KEYMAP: &[Binding] = &[
    // -- anywhere ---------------------------------------------------------
    // The exception is stated on the row it is an exception to, rather than
    // only in the section that takes the key: an operator asking why Ctrl-C did
    // nothing reads this row, not the one about a pane they may not have
    // realised has the keyboard.
    b(
        "Char('c')",
        "Ctrl-C",
        Source::Shell,
        "quit — from any field, but not from a pane holding the keyboard",
    ),
    b("Char('q')", "q", Source::Shell, "quit"),
    b("Esc", "Esc", Source::Shell, "quit"),
    b(
        "Char('r')",
        "r",
        Source::Shell,
        "refresh now, without waiting for the poll",
    ),
    b("Char(c)", "1–9, 0", Source::Shell, "show that view"),
    b("Tab", "Tab", Source::Shell, "the next view"),
    b("BackTab", "Shift-Tab", Source::Shell, "the previous view"),
    b(
        "Char('z')",
        "z",
        Source::Shell,
        "reserved for zen mode — claimed so no view can take it",
    ),
    b(
        "Char('f')",
        "f",
        Source::Shell,
        "reserved for fullscreen — claimed so no view can take it",
    ),
    b("Char('/')", "/", Source::Shell, "focus the command line"),
    b("Char('?')", "?", Source::Shell, "this overlay"),
    // -- the command line -------------------------------------------------
    b(
        "Char(c)",
        "any key",
        Source::Command,
        "types — the line owns every printable key while it has focus",
    ),
    b(
        "Backspace",
        "Backspace",
        Source::Command,
        "deletes, and past the space reverts to the scope picker",
    ),
    b("Left", "←", Source::Command, "move the caret left"),
    b("Right", "→", Source::Command, "move the caret right"),
    b("Up", "↑", Source::Command, "an older line"),
    b("Down", "↓", Source::Command, "a newer line"),
    b("Tab", "Tab", Source::Command, "accept the first suggestion"),
    b("Enter", "Enter", Source::Command, "act on the line"),
    b("Esc", "Esc", Source::Command, "abandon the line"),
    // -- this overlay -----------------------------------------------------
    b("Up", "↑", Source::Help, "scroll up"),
    b("Down", "↓", Source::Help, "scroll down"),
    b("Esc", "Esc", Source::Help, "close"),
    b("Char('?')", "?", Source::Help, "close"),
    // -- ATLAS ------------------------------------------------------------
    b(
        "Up",
        "↑",
        Source::View(ViewId::Atlas),
        "the conversation, one line up",
    ),
    b(
        "Down",
        "↓",
        Source::View(ViewId::Atlas),
        "one line down — the bottom pins to the newest answer",
    ),
    b(
        "PageUp",
        "PgUp",
        Source::View(ViewId::Atlas),
        "a page of conversation up",
    ),
    b(
        "PageDown",
        "PgDn",
        Source::View(ViewId::Atlas),
        "a page down",
    ),
    w(
        "Char('b')",
        "b",
        Source::View(ViewId::Atlas),
        "book the desk's current proposal — only while the ask row is empty",
    ),
    w(
        "Char('i')",
        "i",
        Source::View(ViewId::Atlas),
        "give the keyboard to the pane — while a child is running there",
    ),
    // -- ATLAS, the ask row -----------------------------------------------
    w(
        "Char('i')",
        "i",
        Source::AtlasAsk,
        "focus the empty row — for a question starting with a key the shell claims",
    ),
    w(
        "Char(c)",
        "any key",
        Source::AtlasAsk,
        "types straight in — there is no mode key on this pane",
    ),
    w(
        "Backspace",
        "Backspace",
        Source::AtlasAsk,
        "deletes a character",
    ),
    w(
        "Enter",
        "Enter",
        Source::AtlasAsk,
        "puts the question to the desk — an empty one is not sent",
    ),
    w(
        "Esc",
        "Esc",
        Source::AtlasAsk,
        "clears the row and gives the keyboard back",
    ),
    // -- ATLAS, the terminal pane -----------------------------------------
    //
    // One row, and it is the whole surface: the desk keeps this key and hands
    // over every other, which is why the row says what it takes rather than
    // only what it does. Guard-differentiated, like the shell's digit arm — the
    // router matches the character and reads the modifier beside it.
    w(
        "Char(c)",
        "Ctrl-]",
        Source::AtlasPane,
        "returns the keyboard — every key but this one is the child's",
    ),
    // -- MKTS -------------------------------------------------------------
    b("Up", "↑", Source::View(ViewId::Markets), "the row above"),
    b("Down", "↓", Source::View(ViewId::Markets), "the row below"),
    b(
        "Left",
        "←",
        Source::View(ViewId::Markets),
        "the crosshair, one bar back",
    ),
    b(
        "Right",
        "→",
        Source::View(ViewId::Markets),
        "the crosshair, one bar forward",
    ),
    b(
        "Char('s')",
        "s",
        Source::View(ViewId::Markets),
        "cycle the grid's order — desk, change, vol, name",
    ),
    // -- BOOK -------------------------------------------------------------
    b("Up", "↑", Source::View(ViewId::Book), "the position above"),
    b(
        "Down",
        "↓",
        Source::View(ViewId::Book),
        "the position below",
    ),
    b(
        "Char('s')",
        "s",
        Source::View(ViewId::Book),
        "cycle the sort column",
    ),
    b(
        "Char(']')",
        "]",
        Source::View(ViewId::Book),
        "the next page of the blotter",
    ),
    b(
        "Char('[')",
        "[",
        Source::View(ViewId::Book),
        "the previous page",
    ),
    b(
        "Char('h')",
        "h",
        Source::View(ViewId::Book),
        "what the holdings rail is shaded by",
    ),
    b(
        "Char('p')",
        "p",
        Source::View(ViewId::Book),
        "the window the equity curve draws",
    ),
    w(
        "Char('n')",
        "n",
        Source::View(ViewId::Book),
        "the next plan card",
    ),
    w(
        "Char('x')",
        "x",
        Source::View(ViewId::Book),
        "ask to execute the selected plan — opens the confirmation box",
    ),
    w(
        "Char('b')",
        "b",
        Source::View(ViewId::Book),
        "book the desk's current proposal — opens the confirmation box",
    ),
    // -- PRED ---------------------------------------------------------------
    // The shell keeps `r` — it is still the desk refresh, and on this pane it
    // is also what re-asks the board. An armed window is *shown* the key on top
    // of that, which is why this row says both things.
    w(
        "Char('r')",
        "r",
        Source::View(ViewId::Predictors),
        "refresh the board, and offer to run one of its lanes",
    ),
    // -- PRED, the lane picker ----------------------------------------------
    w("Up", "↑", Source::PredictorsRun, "the lane above"),
    w("Down", "↓", Source::PredictorsRun, "the lane below"),
    w(
        "Enter",
        "Enter",
        Source::PredictorsRun,
        "fits that lane against the baseline — it books nothing",
    ),
    w(
        "Esc",
        "Esc",
        Source::PredictorsRun,
        "closes it — the board is left as the owner served it",
    ),
    // -- WORK -------------------------------------------------------------
    w(
        "Char('i')",
        "i",
        Source::View(ViewId::Workforce),
        "ask the desk a question",
    ),
    w(
        "Char('S')",
        "S",
        Source::View(ViewId::Workforce),
        "start a governed workflow",
    ),
    // -- WORK, the question row -------------------------------------------
    w(
        "Char(c)",
        "any key",
        Source::WorkforceAsk,
        "types the question",
    ),
    w(
        "Backspace",
        "Backspace",
        Source::WorkforceAsk,
        "deletes a character",
    ),
    w(
        "Enter",
        "Enter",
        Source::WorkforceAsk,
        "puts the question to the desk — an empty one is not sent",
    ),
    w("Esc", "Esc", Source::WorkforceAsk, "abandons the question"),
    // -- WORK, the template picker ----------------------------------------
    w("Up", "↑", Source::WorkforcePicker, "the template above"),
    w("Down", "↓", Source::WorkforcePicker, "the template below"),
    w(
        "Char(c)",
        "any key",
        Source::WorkforcePicker,
        "types the goal the run is for",
    ),
    w(
        "Backspace",
        "Backspace",
        Source::WorkforcePicker,
        "deletes a character",
    ),
    w(
        "Enter",
        "Enter",
        Source::WorkforcePicker,
        "starts the run — an empty goal keeps the box up",
    ),
    w("Esc", "Esc", Source::WorkforcePicker, "abandons the picker"),
    // -- AUDIT ------------------------------------------------------------
    w("Up", "↑", Source::View(ViewId::Audit), "the approval above"),
    w(
        "Down",
        "↓",
        Source::View(ViewId::Audit),
        "the approval below",
    ),
    w(
        "Char('a')",
        "a",
        Source::View(ViewId::Audit),
        "approve the selected request — opens the confirmation box",
    ),
    w(
        "Char('R')",
        "R",
        Source::View(ViewId::Audit),
        "reject it — R, because the shell owns lowercase r",
    ),
    // -- VIS --------------------------------------------------------------
    //
    // Every row here is `b` and not `w`. Reading a drawing the owner already
    // made changes nothing, so a glass window is offered all six — the posture
    // filter removes what a window would *refuse*, and this pane refuses none
    // of it.
    b("Up", "↑", Source::View(ViewId::Visuals), "the visual above"),
    b(
        "Down",
        "↓",
        Source::View(ViewId::Visuals),
        "the visual below",
    ),
    b(
        "Enter",
        "Enter",
        Source::View(ViewId::Visuals),
        "render the one under the cursor — the owner draws it, nothing is run",
    ),
    b(
        "PageUp",
        "PgUp",
        Source::View(ViewId::Visuals),
        "a page of the drawing up",
    ),
    b(
        "PageDown",
        "PgDn",
        Source::View(ViewId::Visuals),
        "a page down",
    ),
    b(
        "Char('k')",
        "k",
        Source::View(ViewId::Visuals),
        "the drawing, one line up",
    ),
    b(
        "Char('j')",
        "j",
        Source::View(ViewId::Visuals),
        "one line down",
    ),
    b(
        "Char('h')",
        "h",
        Source::View(ViewId::Visuals),
        "the drawing, one column left — art is never re-wrapped to fit",
    ),
    b(
        "Char('l')",
        "l",
        Source::View(ViewId::Visuals),
        "one column right",
    ),
    b(
        "Home",
        "Home",
        Source::View(ViewId::Visuals),
        "back to the left edge of the drawing",
    ),
    b(
        "End",
        "End",
        Source::View(ViewId::Visuals),
        "out to the right edge — where a caption wider than the pane ends",
    ),
    // -- SETT -------------------------------------------------------------
    //
    // The three keys below are the *cards'*, not the pane's: each is refused
    // unless the card that owns it is the one the arrows left the focus on, and
    // that card's own footer says so on screen.
    w(
        "Up",
        "↑",
        Source::View(ViewId::Settings),
        "the card above, the source above inside NEWS, or the right above inside MODELS",
    ),
    w(
        "Down",
        "↓",
        Source::View(ViewId::Settings),
        "the card below, the source below inside NEWS, or the right below inside MODELS",
    ),
    w(
        "Char('a')",
        "a",
        Source::View(ViewId::Settings),
        "on DESK: type an Alpaca paper login — the book is unchanged",
    ),
    w(
        "Char('t')",
        "t",
        Source::View(ViewId::Settings),
        "on DESK: ask the venue whether the stored login works",
    ),
    w(
        "Char('m')",
        "m",
        Source::View(ViewId::Settings),
        "on MODELS: choose which mind each surface runs",
    ),
    w(
        "Char('m')",
        "m",
        Source::View(ViewId::Settings),
        "on DESK: choose the data lane — live · alpaca asks for a login first",
    ),
    w(
        "Char('m')",
        "m",
        Source::View(ViewId::Settings),
        "on METHOD: choose the method this desk solves with",
    ),
    w(
        "Char('k')",
        "k",
        Source::View(ViewId::Settings),
        "on METHOD: how many names this desk may hold — 0 clears the cap",
    ),
    w(
        "Char(' ')",
        "Space",
        Source::View(ViewId::Settings),
        "on NEWS: tick the source under the cursor — nothing is sent",
    ),
    w(
        "Char(' ')",
        "Space",
        Source::View(ViewId::Settings),
        "on MODELS: grant or withdraw the right under the cursor — sent at once",
    ),
    w(
        "Char('c')",
        "c",
        Source::View(ViewId::Settings),
        "on NEWS: type the EDGAR contact the SEC asks callers to send",
    ),
    w(
        "Char('s')",
        "s",
        Source::View(ViewId::Settings),
        "on NEWS: save what is ticked — changes what this desk reads, nothing else",
    ),
    w(
        "Char('v')",
        "v",
        Source::View(ViewId::Settings),
        "on NEWS: save it and ask the owner to read one window per source first",
    ),
    // -- SETT, the alpaca login form --------------------------------------
    w(
        "Char(c)",
        "any key",
        Source::SettingsLogin,
        "types into the field — both are masked",
    ),
    w(
        "Backspace",
        "Backspace",
        Source::SettingsLogin,
        "deletes a character",
    ),
    w("Tab", "Tab", Source::SettingsLogin, "the other field"),
    w(
        "Enter",
        "Enter",
        Source::SettingsLogin,
        "stores the login — or answers the question about replacing one",
    ),
    w(
        "Esc",
        "Esc",
        Source::SettingsLogin,
        "closes the form and clears both fields",
    ),
    // -- SETT, the model switcher -----------------------------------------
    w("Up", "↑", Source::SettingsModels, "the offer above"),
    w("Down", "↓", Source::SettingsModels, "the offer below"),
    w(
        "Enter",
        "Enter",
        Source::SettingsModels,
        "points that surface at that model — a backend the desk cannot reach says why",
    ),
    w(
        "Esc",
        "Esc",
        Source::SettingsModels,
        "closes it — every surface is left as the desk has it",
    ),
    // -- SETT, the data lane picker ---------------------------------------
    w("Up", "↑", Source::SettingsLane, "the lane above"),
    w("Down", "↓", Source::SettingsLane, "the lane below"),
    w(
        "Enter",
        "Enter",
        Source::SettingsLane,
        "points the desk at that lane — live · alpaca asks for a login first",
    ),
    w(
        "Esc",
        "Esc",
        Source::SettingsLane,
        "closes it — the desk is left where it is",
    ),
    // -- SETT, the edgar contact box --------------------------------------
    w(
        "Char(c)",
        "any key",
        Source::SettingsContact,
        "types the contact — plain text, because it is an identity and not a secret",
    ),
    w(
        "Backspace",
        "Backspace",
        Source::SettingsContact,
        "deletes a character",
    ),
    w(
        "Enter",
        "Enter",
        Source::SettingsContact,
        "keeps it for the next save — nothing is sent by this key",
    ),
    w(
        "Esc",
        "Esc",
        Source::SettingsContact,
        "closes the box and leaves the stored contact alone",
    ),
    // -- SETT, the method picker ------------------------------------------
    w("Up", "↑", Source::SettingsMethod, "the method above"),
    w("Down", "↓", Source::SettingsMethod, "the method below"),
    w(
        "Enter",
        "Enter",
        Source::SettingsMethod,
        "solves with that method from the next run — it books nothing",
    ),
    w(
        "Esc",
        "Esc",
        Source::SettingsMethod,
        "closes it — the desk solves as it did",
    ),
    // -- SETT, the holdings cap box ---------------------------------------
    w(
        "Char(c)",
        "any key",
        Source::SettingsCap,
        "types the cap — digits only, and 0 clears it",
    ),
    w(
        "Backspace",
        "Backspace",
        Source::SettingsCap,
        "deletes a character",
    ),
    w(
        "Enter",
        "Enter",
        Source::SettingsCap,
        "sends it — an empty box or 0 puts the mandate's own cap back",
    ),
    w(
        "Esc",
        "Esc",
        Source::SettingsCap,
        "closes the box and leaves the cap alone",
    ),
    // -- the confirmation box ---------------------------------------------
    w(
        "Char(c)",
        "any key",
        Source::Confirm,
        "types the challenge the box asks for",
    ),
    w("Backspace", "Backspace", Source::Confirm, "deletes"),
    w(
        "Enter",
        "Enter",
        Source::Confirm,
        "answers, once the challenge matches",
    ),
    w("Esc", "Esc", Source::Confirm, "abandons — nothing is sent"),
    // -- the startup door -------------------------------------------------
    // Only an armed window is asked. A glass one is shown what the door would
    // have taken and dismisses it with any key, which claims no `KeyCode` and
    // therefore owes no row — the box says so itself.
    w("Up", "↑", Source::Door, "the row above"),
    w("Down", "↓", Source::Door, "the row below"),
    w(
        "Enter",
        "Enter",
        Source::Door,
        "chooses the row — the last one moves on",
    ),
    // Both halves in one row, because the row is clipped at the overlay's
    // width: the first question's Esc is the safe desk and never a live one,
    // and the second's leaves every surface as the desk has it.
    w(
        "Esc",
        "Esc",
        Source::Door,
        "synthetic · simulated, or the models left as they are",
    ),
];

/// The bindings a window in this posture actually has.
///
/// A glass window is not shown the operator keys at all. Greying them out would
/// say "this client could do that if you asked", which is the claim the posture
/// exists to make impossible: the keys are absent, not disabled.
pub fn bindings(posture: Posture) -> impl Iterator<Item = &'static Binding> {
    KEYMAP
        .iter()
        .filter(move |binding| !binding.writes || posture.writes())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One router file's source, with its test module cut off.
    ///
    /// The cut is deliberate: `confirm.rs`'s own tests press `KeyCode::Char('q')`
    /// to prove the box swallows it, and a scrape that counted it would demand a
    /// help row for a key nothing binds. It is at the test *module* rather than
    /// at the first `#[cfg(test)]`, because several views carry test-only
    /// accessors above their routing — cutting there read `markets.rs` as a view
    /// with no keys at all, and the check passed for exactly the wrong reason.
    fn routing_source(file: &str) -> String {
        let path = format!("{}/src/{file}", env!("CARGO_MANIFEST_DIR"));
        let whole = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
        match whole.find("#[cfg(test)]\nmod tests") {
            Some(at) => whole[..at].to_string(),
            None => whole,
        }
    }

    /// One function's body, as source text, or `None` when the file has no such
    /// function with a body.
    ///
    /// `None` rather than a panic, because the call-following below asks this
    /// about every identifier it sees and most of them are methods on other
    /// types. The *section* lookups panic on `None` themselves: an empty region
    /// matches an empty expectation, which is how a pin of this shape dies.
    fn body_of(src: &str, anchor: &str, name: &str) -> Option<String> {
        let src = match anchor.is_empty() {
            true => src,
            false => &src[src.find(anchor)?..],
        };
        let needle = format!("fn {name}(");
        let mut from = 0;
        while let Some(at) = src[from..].find(&needle) {
            let rest = &src[from + at..];
            let masked = mask_literals(rest);
            let open = masked.find('{');
            let semi = masked.find(';');
            match (open, semi) {
                // A trait's declaration has no body. `views/mod.rs` carries one
                // above the impl that actually answers.
                (Some(o), Some(s)) if s < o => from += at + needle.len(),
                (Some(o), _) => {
                    let mut depth = 0usize;
                    for (i, ch) in masked.char_indices().skip_while(|(i, _)| *i < o) {
                        match ch {
                            '{' => depth += 1,
                            '}' => {
                                depth -= 1;
                                if depth == 0 {
                                    return Some(rest[o..=i].to_string());
                                }
                            }
                            _ => {}
                        }
                    }
                    panic!("fn {name} has no balanced body");
                }
                _ => from += at + needle.len(),
            }
        }
        None
    }

    /// Every identifier called as a function inside `body`, in order.
    ///
    /// Path-qualified calls (`help::on_key`, `Picker::default`) are left out:
    /// they name something in another module, and resolving them against
    /// *this* file would follow whatever happened to share the name. Method
    /// calls on `self` are exactly what has to be followed, so a leading `.` is
    /// kept.
    fn local_calls(body: &str) -> Vec<String> {
        let chars: Vec<char> = mask_literals(body).chars().collect();
        let mut out = Vec::new();
        let mut i = 0;
        while i < chars.len() {
            if !(chars[i].is_alphabetic() || chars[i] == '_') {
                i += 1;
                continue;
            }
            let start = i;
            while i < chars.len() && (chars[i].is_alphanumeric() || chars[i] == '_') {
                i += 1;
            }
            if chars.get(i) != Some(&'(') {
                continue;
            }
            let qualified = start >= 2 && chars[start - 1] == ':' && chars[start - 2] == ':';
            if !qualified {
                out.push(chars[start..i].iter().collect());
            }
        }
        out
    }

    /// The same text with the *contents* of string and character literals
    /// blanked, byte for byte, so brace balancing cannot be fooled by `'{'`.
    ///
    /// Byte-for-byte because the offsets it yields are used to slice the
    /// original. Lifetimes (`&'a str`) are deliberately not treated as
    /// character literals — only `'x'` and `'\x'` are, which is what the router
    /// files actually contain.
    fn mask_literals(src: &str) -> String {
        let chars: Vec<char> = src.chars().collect();
        let mut out = String::with_capacity(src.len());
        let mut i = 0;
        // Each branch owns its own cursor: a shared `i += 1` at the foot lost
        // the closing quote of every string, which is one byte and therefore
        // every offset after it.
        while i < chars.len() {
            match chars[i] {
                '"' => {
                    out.push('"');
                    i += 1;
                    while i < chars.len() && chars[i] != '"' {
                        if chars[i] == '\\' && i + 1 < chars.len() {
                            out.push(' ');
                            i += 1;
                        }
                        out.push_str(&" ".repeat(chars[i].len_utf8()));
                        i += 1;
                    }
                    if i < chars.len() {
                        out.push('"');
                        i += 1;
                    }
                }
                // `'x'` or `'\x'`, and nothing longer — a lifetime has no
                // closing quote and must be left alone.
                '\'' if closes_char_literal(&chars, i).is_some() => {
                    let end = closes_char_literal(&chars, i).unwrap();
                    out.push('\'');
                    for c in &chars[i + 1..end] {
                        out.push_str(&" ".repeat(c.len_utf8()));
                    }
                    out.push('\'');
                    i = end + 1;
                }
                c => {
                    out.push(c);
                    i += 1;
                }
            }
        }
        out
    }

    /// Where a character literal starting at `at` closes, if it does.
    fn closes_char_literal(chars: &[char], at: usize) -> Option<usize> {
        [at + 2, at + 3]
            .into_iter()
            .find(|&end| chars.get(end) == Some(&'\''))
    }

    /// The whole scrape, over source text: find the routing function, follow one
    /// level of the local functions it calls, and count every `KeyCode::…` token
    /// in what that covers.
    ///
    /// `siblings` are the functions in this file that belong to *other* sections
    /// — WORKFORCE's question row and its picker are each other's — and are not
    /// followed: a section that swallowed another section's keys would describe
    /// one field with rows about the other, which is the split this check exists
    /// to keep.
    ///
    /// One level, not a call graph. Two hops deep is a keymap check that has
    /// become a compiler, and the depth is weakness 6 in this module's list.
    ///
    /// Every step is here rather than split across the file lookup, so a
    /// `dedup()` cannot be slipped into a wrapper the fixture tests do not
    /// exercise.
    fn codes_in_src(src: &str, anchor: &str, name: &str, siblings: &[&str]) -> Vec<String> {
        let body = body_of(src, anchor, name)
            .unwrap_or_else(|| panic!("no fn {name} with a body{}", suffix(anchor)));
        let mut text = body.clone();
        for callee in local_calls(&body) {
            if callee == name || siblings.contains(&callee.as_str()) {
                continue;
            }
            if let Some(more) = body_of(src, "", &callee) {
                text.push_str(&more);
            }
        }
        let mut found = scrape(&text);
        found.sort();
        found
    }

    fn suffix(anchor: &str) -> String {
        match anchor.is_empty() {
            true => String::new(),
            false => format!(" after {anchor:?}"),
        }
    }

    /// Every `KeyCode::…` token in some source text, **with repeats**.
    ///
    /// A multiset. Collapsing repeats is what let a second `Char(c)` arm — a
    /// live binding with a different guard and no help row — pass unnoticed;
    /// see this module's own header.
    fn scrape(src: &str) -> Vec<String> {
        let mut found = Vec::new();
        let bytes: Vec<char> = src.chars().collect();
        let needle: Vec<char> = "KeyCode::".chars().collect();
        let mut i = 0;
        while i + needle.len() <= bytes.len() {
            if bytes[i..i + needle.len()] != needle[..] {
                i += 1;
                continue;
            }
            let mut j = i + needle.len();
            let start = j;
            while j < bytes.len() && (bytes[j].is_alphanumeric() || bytes[j] == '_') {
                j += 1;
            }
            let mut token: String = bytes[start..j].iter().collect();
            // A payload, if the variant carries one: `Char('q')`, `Char(c)`.
            if bytes.get(j) == Some(&'(') {
                let open = j;
                let mut depth = 0;
                while j < bytes.len() {
                    match bytes[j] {
                        '(' => depth += 1,
                        ')' => {
                            depth -= 1;
                            if depth == 0 {
                                break;
                            }
                        }
                        _ => {}
                    }
                    j += 1;
                }
                token.push_str(
                    &bytes[open..=j.min(bytes.len() - 1)]
                        .iter()
                        .collect::<String>(),
                );
            }
            if !token.is_empty() {
                found.push(token);
            }
            i = j.max(i + 1);
        }
        found
    }

    /// One section's codes, read off the tree.
    ///
    /// A file read and the sibling lookup, and nothing else: every decision the
    /// check makes lives in `codes_in_src`, which the fixtures below exercise
    /// directly.
    fn codes_in(source: Source) -> Vec<String> {
        let (file, anchor, name) = source.region();
        let siblings: Vec<&str> = Source::ALL
            .iter()
            .filter(|other| **other != source && other.region().0 == file)
            .map(|other| other.region().2)
            .collect();
        codes_in_src(&routing_source(file), anchor, name, &siblings)
    }

    fn declared(source: Source) -> Vec<String> {
        let mut rows: Vec<String> = KEYMAP
            .iter()
            .filter(|binding| binding.source == source)
            .map(|binding| binding.code.to_string())
            .collect();
        rows.sort();
        rows
    }

    #[test]
    fn every_binding_the_router_has_is_in_the_keymap_and_the_reverse() {
        // The property the overlay's honesty rests on, checked in both
        // directions: a key added to a router with no row here would leave a
        // binding nobody can discover, and a row here with no arm would put a
        // key in the help that does nothing when pressed.
        //
        // Bidirectional, per-section, and by *count*. Per-section so a row filed
        // under the wrong one fails too — a `Char('x')` moved from BOOK to MKTS
        // is a different sentence in the overlay and would pass on totals. By
        // count because a second arm on a code that is already bound is the
        // likely next binding on this client: every text field routes through a
        // guarded `Char(c)`, and a set says the table already covers it.
        for source in Source::ALL {
            let (file, _, name) = source.region();
            assert_eq!(
                codes_in(source),
                declared(source),
                "{} ({file}::{name}) and its help rows disagree",
                source.label(),
            );
        }
    }

    /// A router with the reviewer's binding in it: two `Char(c)` arms told
    /// apart only by their guards, which is how every text field on this client
    /// routes.
    const TWO_GUARDED_ARMS: &str = r#"
        fn route(k: KeyEvent) -> Option<Command> {
            match k.code {
                KeyCode::Char(c) if c.is_ascii_digit() => jump(c),
                KeyCode::Char(c) if c.is_ascii_uppercase() => return None,
                KeyCode::Tab => next(),
                _ => {}
            }
            None
        }
    "#;

    /// A router that leaves the function the section points at.
    const CALLS_A_SIBLING: &str = r#"
        fn route(k: KeyEvent) -> Option<Command> {
            match k.code {
                KeyCode::Tab => next(),
                _ => self.sneaky(k),
            }
            None
        }
        fn sneaky(&mut self, k: KeyEvent) -> Option<Command> {
            if k.code == KeyCode::Char('Z') {
                return None;
            }
            None
        }
    "#;

    /// One name, two impls, and a trait declaration above both — the shape the
    /// anchor exists for.
    const TWO_IMPLS: &str = r#"
        trait View {
            fn route(k: KeyEvent) -> Option<Command>;
        }
        impl View for First {
            fn route(k: KeyEvent) -> Option<Command> {
                match k.code {
                    KeyCode::Tab => next(),
                    _ => {}
                }
                None
            }
        }
        impl View for Second {
            fn route(k: KeyEvent) -> Option<Command> {
                match k.code {
                    KeyCode::Esc => close(),
                    _ => {}
                }
                None
            }
        }
    "#;

    #[test]
    fn the_anchor_picks_the_impl_it_names_and_not_the_one_above_it() {
        // No section needs an anchor today, so without this the branch would sit
        // unexercised until the next file with two same-named routers — and be
        // discovered broken there, in a check whose whole value is that it
        // cannot be discovered broken.
        assert_eq!(
            codes_in_src(TWO_IMPLS, "impl View for Second", "route", &[]),
            vec!["Esc".to_string()]
        );
        // And with no anchor it reads the first one with a body, stepping over
        // the trait's body-less declaration rather than panicking on it.
        assert_eq!(
            codes_in_src(TWO_IMPLS, "", "route", &[]),
            vec!["Tab".to_string()]
        );
    }

    #[test]
    fn the_scrape_keeps_a_repeat_rather_than_collapsing_it() {
        // The property the whole comparison rests on, asserted on the scrape
        // itself rather than on a duplicate the test appends. Appending one
        // demonstrates that a multiset differs from a set; it does not defend
        // the scrape, and a `dedup()` put back into the pipeline left the old
        // version of this test green.
        assert_eq!(
            codes_in_src(TWO_GUARDED_ARMS, "", "route", &[]),
            vec![
                "Char(c)".to_string(),
                "Char(c)".to_string(),
                "Tab".to_string()
            ],
            "a repeated code was collapsed — the reviewer's binding is invisible again"
        );
    }

    #[test]
    fn the_scrape_follows_a_router_into_the_helper_it_calls() {
        // Narrowing from a file to a function opened this: a sibling that binds
        // a key and is called from the routing function is a live binding with
        // no help row, and the section's own body never mentions it. One level
        // is followed — see weakness 6 for what two would be.
        assert_eq!(
            codes_in_src(CALLS_A_SIBLING, "", "route", &[]),
            vec!["Char('Z')".to_string(), "Tab".to_string()],
            "a binding one call away from the router is invisible"
        );
        // Unless the sibling is another section's router, which owns its own
        // keys and its own rows.
        assert_eq!(
            codes_in_src(CALLS_A_SIBLING, "", "route", &["sneaky"]),
            vec!["Tab".to_string()],
        );
    }

    #[test]
    fn the_two_workforce_fields_are_read_apart_and_not_through_each_other() {
        // The live case of the rule above: `keys` calls both fields, and each is
        // its own section. A `keys` section that followed them would carry the
        // union of three routers and describe none of them.
        assert_eq!(
            codes_in(Source::View(ViewId::Workforce)),
            vec!["Char('S')".to_string(), "Char('i')".to_string()]
        );
    }

    #[test]
    fn the_cut_at_the_test_module_keeps_the_routing_above_it() {
        // The bug this cut already had once: several views declare `#[cfg(test)]`
        // accessors above their `on_key`, so cutting at the first occurrence
        // read the whole of MKTS as a view with no keys — an empty set that
        // matched an empty expectation and proved nothing.
        let markets = codes_in(Source::View(ViewId::Markets));
        assert!(
            markets.contains(&"Up".to_string()) && markets.contains(&"Left".to_string()),
            "{markets:?}"
        );
        // And the tests below really are excluded: `confirm.rs` presses `q` at
        // the box to prove it is swallowed, and nothing binds `q` there.
        assert!(!codes_in(Source::Confirm).contains(&"Char('q')".to_string()));
    }

    #[test]
    fn the_scrape_reads_one_routing_function_and_not_its_neighbours() {
        // A scanner that found nothing would make the check above pass for every
        // section with no rows, which is exactly how a pin of this shape dies
        // quietly. Two known tokens, one plain and one with a payload.
        let shell = codes_in(Source::Shell);
        assert!(shell.contains(&"Tab".to_string()), "{shell:?}");
        assert!(shell.contains(&"Char('q')".to_string()), "{shell:?}");
        assert!(!KEYMAP.is_empty());

        // And the split is real rather than two labels over one file: the
        // question row binds Enter and no arrows, the picker binds both, and
        // the keys that *open* them are in neither.
        let ask = codes_in(Source::WorkforceAsk);
        let picker = codes_in(Source::WorkforcePicker);
        let work = codes_in(Source::View(ViewId::Workforce));
        assert!(
            ask.contains(&"Enter".to_string()) && !ask.contains(&"Up".to_string()),
            "{ask:?}"
        );
        assert!(picker.contains(&"Up".to_string()), "{picker:?}");
        assert_eq!(work, vec!["Char('S')".to_string(), "Char('i')".to_string()]);

        // A view that binds nothing reads as nothing, not as unreadable.
        assert!(codes_in(Source::View(ViewId::Desk)).is_empty());
        assert!(codes_in(Source::View(ViewId::Research)).is_empty());
    }

    #[test]
    fn the_region_scanner_is_not_fooled_by_a_brace_in_a_literal() {
        // The scanner balances braces to find a function body, so a `'{'` in a
        // match arm would end it early and silently shorten a section. Nothing
        // in the routers spells one today; this is what keeps that from being
        // load-bearing.
        let src = "fn f() { match c { '{' => \"}}\", _ => {} } }";
        let masked = mask_literals(src);
        // Byte for byte, because the offsets it yields slice the original.
        assert_eq!(masked.len(), src.len(), "{masked}");
        // Three code braces of each: the one in the character literal and the
        // two in the string are gone, which is what stops a body ending at the
        // first `'}'` an arm happens to match on.
        assert_eq!(masked.matches('{').count(), 3, "{masked}");
        assert_eq!(masked.matches('}').count(), 3, "{masked}");
        // A lifetime is not a character literal and must survive untouched.
        assert_eq!(
            mask_literals("Vec<Span<'static>> {}"),
            "Vec<Span<'static>> {}"
        );
    }

    #[test]
    fn a_glass_window_is_offered_no_key_it_would_refuse() {
        let glass: Vec<&str> = bindings(Posture::Glass).map(|b| b.key).collect();
        assert!(glass.contains(&"q"));
        assert!(glass.contains(&"/"));
        // The keys that can move money, start work, or type into a process are
        // absent, not disabled: a greyed key says the client could do it if
        // asked. The pane's pair is here because a monitoring build compiles no
        // router that could take a keystroke and nothing that could open a
        // child to give one to — an overlay offering them would be describing a
        // surface that cannot exist in the artifact drawing it.
        for armed in ["x", "a", "R", "S", "Ctrl-]"] {
            assert!(
                !glass.contains(&armed),
                "{armed} is offered to a glass window"
            );
        }
        assert!(
            bindings(Posture::Glass).count() < KEYMAP.len(),
            "the posture filter is doing nothing"
        );
    }

    #[cfg(feature = "operator")]
    #[test]
    fn an_armed_window_is_offered_every_row() {
        assert_eq!(bindings(Posture::Operator).count(), KEYMAP.len());
    }

    #[test]
    fn every_row_says_something_and_every_section_is_reachable() {
        for binding in KEYMAP {
            assert!(!binding.key.is_empty(), "{binding:?}");
            assert!(!binding.action.is_empty(), "{binding:?}");
            assert!(
                Source::ALL.contains(&binding.source),
                "{binding:?} is filed under a section the overlay never draws"
            );
        }
    }
}
