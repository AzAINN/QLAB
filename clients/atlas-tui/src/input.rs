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
    /// The confirmation box, which outranks everything but Ctrl-C.
    Confirm,
}

impl Source {
    /// Every section, in the order the overlay lists them: the global keys
    /// first, then the two surfaces that take the keyboard, then the views in
    /// nav-rail order, then the box that outranks them all.
    pub const ALL: [Source; 13] = [
        Source::Shell,
        Source::Command,
        Source::Help,
        Source::View(ViewId::Desk),
        Source::View(ViewId::Markets),
        Source::View(ViewId::Book),
        Source::View(ViewId::Research),
        Source::View(ViewId::Workforce),
        Source::WorkforceAsk,
        Source::WorkforcePicker,
        Source::View(ViewId::Audit),
        Source::View(ViewId::Settings),
        Source::Confirm,
    ];

    /// The section header.
    pub fn label(self) -> &'static str {
        match self {
            Source::Shell => "anywhere",
            Source::Command => "the command line",
            Source::Help => "this overlay",
            Source::View(id) => id.label(),
            Source::WorkforceAsk => "WORK · the question row",
            Source::WorkforcePicker => "WORK · the template picker",
            Source::Confirm => "a confirmation box",
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
    /// The anchor is for the one case where the name is not unique: `views/mod.rs`
    /// carries the trait's declaration, the registry's dispatcher, and the
    /// `Unbuilt` view that actually answers for RSCH and SETT.
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
            Source::View(ViewId::Desk) => ("ui/views/desk.rs", "", "on_key"),
            Source::View(ViewId::Markets) => ("ui/views/markets.rs", "", "on_key"),
            Source::View(ViewId::Book) => ("ui/views/book.rs", "", "on_key"),
            // `View::on_key` here only forwards; `keys` is the router, and the
            // two fields under it are the sections below.
            Source::View(ViewId::Workforce) => ("ui/views/workforce.rs", "", "keys"),
            Source::WorkforceAsk => ("ui/views/workforce.rs", "", "ask_key"),
            Source::WorkforcePicker => ("ui/views/workforce.rs", "", "picker_key"),
            Source::View(ViewId::Audit) => ("ui/views/audit.rs", "", "on_key"),
            // RSCH and SETT are `Unbuilt`, which declines every key.
            Source::View(ViewId::Research) | Source::View(ViewId::Settings) => {
                ("ui/views/mod.rs", "impl View for Unbuilt", "on_key")
            }
            Source::Confirm => ("ui/widgets/confirm.rs", "", "on_key"),
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
    b(
        "Char('c')",
        "Ctrl-C",
        Source::Shell,
        "quit — works even while a field owns the keyboard",
    ),
    b("Char('q')", "q", Source::Shell, "quit"),
    b("Esc", "Esc", Source::Shell, "quit"),
    b(
        "Char('r')",
        "r",
        Source::Shell,
        "refresh now, without waiting for the poll",
    ),
    b("Char(c)", "1–7", Source::Shell, "show that view"),
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

    /// One routing function's source, found by (file, anchor, name).
    ///
    /// The test module is cut off first, deliberately: `confirm.rs`'s own tests
    /// press `KeyCode::Char('q')` to prove the box swallows it, and a scrape
    /// that counted it would demand a help row for a key nothing binds. The cut
    /// is at the test *module* rather than at the first `#[cfg(test)]`, because
    /// several views carry test-only accessors above their routing — cutting
    /// there read `markets.rs` as a view with no keys at all, and the check
    /// passed for exactly the wrong reason.
    ///
    /// Panics rather than returning empty when the function cannot be found:
    /// an empty region matches an empty expectation, which is how a pin of this
    /// shape dies quietly.
    fn body_of(file: &str, anchor: &str, name: &str) -> String {
        let path = format!("{}/src/{file}", env!("CARGO_MANIFEST_DIR"));
        let whole = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
        let src = match whole.find("#[cfg(test)]\nmod tests") {
            Some(at) => &whole[..at],
            None => &whole[..],
        };
        let src = match anchor.is_empty() {
            true => src,
            false => {
                let at = src
                    .find(anchor)
                    .unwrap_or_else(|| panic!("{file}: no anchor {anchor:?}"));
                &src[at..]
            }
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
                                    return rest[o..=i].to_string();
                                }
                            }
                            _ => {}
                        }
                    }
                    panic!("{file}: fn {name} has no balanced body");
                }
                _ => from += at + needle.len(),
            }
        }
        panic!(
            "{file}: no fn {name} with a body{}",
            match anchor.is_empty() {
                true => String::new(),
                false => format!(" after {anchor:?}"),
            }
        );
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

    /// Every `KeyCode::…` token in one routing function, **with repeats**.
    ///
    /// A multiset, sorted. Collapsing repeats is what let a second `Char(c)`
    /// arm — a live binding with a different guard and no help row — pass
    /// unnoticed; see this module's own header.
    fn codes_in(source: Source) -> Vec<String> {
        let (file, anchor, name) = source.region();
        let src = body_of(file, anchor, name);
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
        found.sort();
        found
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

    #[test]
    fn a_second_arm_on_a_bound_code_is_a_binding_the_help_does_not_have() {
        // The mutation this check was rebuilt for, without planting it in the
        // router: `KeyCode::Char(c) if c.is_ascii_uppercase() => return None`
        // beside the digit arm swallows every capital letter workstation-wide,
        // and `Char(c)` is already in the shell's codes. A set comparison stays
        // green. A multiset cannot.
        let mut mutated = codes_in(Source::Shell);
        mutated.push("Char(c)".to_string());
        mutated.sort();
        assert_ne!(
            mutated,
            declared(Source::Shell),
            "a duplicate code is invisible to this comparison — the hole is open again"
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
        // The four keys that can move money or start work are absent, not
        // disabled: a greyed key says the client could do it if asked.
        for armed in ["x", "a", "R", "S"] {
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
