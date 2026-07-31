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
//! mechanically at every `cargo test`. The disclosure that goes with it: the
//! check compares `KeyCode` spellings, so a binding that differs only by a
//! modifier (Ctrl-C is the one) is matched by its bare code — the modifier
//! lives in `key`, which is what a human reads, not in the comparison.

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
    /// The confirmation box, which outranks everything but Ctrl-C.
    Confirm,
}

impl Source {
    /// Every section, in the order the overlay lists them: the global keys
    /// first, then the two surfaces that take the keyboard, then the views in
    /// nav-rail order, then the box that outranks them all.
    pub const ALL: [Source; 11] = [
        Source::Shell,
        Source::Command,
        Source::Help,
        Source::View(ViewId::Desk),
        Source::View(ViewId::Markets),
        Source::View(ViewId::Book),
        Source::View(ViewId::Research),
        Source::View(ViewId::Workforce),
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
            Source::Confirm => "a confirmation box",
        }
    }

    /// The file whose `match` arms this section describes, relative to `src`.
    ///
    /// Data rather than a comment, because it is what the equivalence check
    /// reads: a section pointed at the wrong file would check nothing and pass.
    /// The two unbuilt views point at the registry that answers for them.
    pub fn file(self) -> &'static str {
        match self {
            Source::Shell => "ui/shell.rs",
            Source::Command => "cmd.rs",
            Source::Help => "ui/widgets/help.rs",
            Source::View(ViewId::Desk) => "ui/views/desk.rs",
            Source::View(ViewId::Markets) => "ui/views/markets.rs",
            Source::View(ViewId::Book) => "ui/views/book.rs",
            Source::View(ViewId::Workforce) => "ui/views/workforce.rs",
            Source::View(ViewId::Audit) => "ui/views/audit.rs",
            // RSCH and SETT are `Unbuilt`, which declines every key.
            Source::View(ViewId::Research) | Source::View(ViewId::Settings) => "ui/views/mod.rs",
            Source::Confirm => "ui/widgets/confirm.rs",
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
    w(
        "Char(c)",
        "any key",
        Source::View(ViewId::Workforce),
        "types into the question or the goal",
    ),
    w(
        "Backspace",
        "Backspace",
        Source::View(ViewId::Workforce),
        "deletes a character",
    ),
    w(
        "Up",
        "↑",
        Source::View(ViewId::Workforce),
        "the template above, in the picker",
    ),
    w(
        "Down",
        "↓",
        Source::View(ViewId::Workforce),
        "the template below",
    ),
    w(
        "Enter",
        "Enter",
        Source::View(ViewId::Workforce),
        "send the question, or start the run",
    ),
    w(
        "Esc",
        "Esc",
        Source::View(ViewId::Workforce),
        "abandon the question or the picker",
    ),
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
    use std::collections::BTreeSet;

    /// Every `KeyCode::…` token in one source file, up to its test module.
    ///
    /// The tests are cut off deliberately: `confirm.rs`'s own tests press
    /// `KeyCode::Char('q')` to prove the box swallows it, and a scrape that
    /// counted that would demand a help row for a key nothing binds. The cut is
    /// at the test *module* rather than at the first `#[cfg(test)]`, because
    /// several views carry test-only accessors above their routing — cutting
    /// there read `markets.rs` as a view with no keys at all, and the check
    /// passed for exactly the wrong reason.
    fn codes_in(file: &str) -> BTreeSet<String> {
        let path = format!("{}/src/{file}", env!("CARGO_MANIFEST_DIR"));
        let src = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
        let src = match src.find("#[cfg(test)]\nmod tests") {
            Some(at) => &src[..at],
            None => &src[..],
        };
        let mut found = BTreeSet::new();
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
                found.insert(token);
            }
            i = j.max(i + 1);
        }
        found
    }

    fn declared(source: Source) -> BTreeSet<String> {
        KEYMAP
            .iter()
            .filter(|binding| binding.source == source)
            .map(|binding| binding.code.to_string())
            .collect()
    }

    #[test]
    fn every_binding_the_router_has_is_in_the_keymap_and_the_reverse() {
        // The property the overlay's honesty rests on, checked in both
        // directions: a key added to a router with no row here would leave a
        // binding nobody can discover, and a row here with no arm would put a
        // key in the help that does nothing when pressed.
        //
        // Bidirectional and per-source, so a row filed under the wrong section
        // fails too — a `Char('x')` moved from BOOK to MKTS is a different
        // sentence in the overlay and would otherwise pass on totals.
        for source in Source::ALL {
            assert_eq!(
                codes_in(source.file()),
                declared(source),
                "{} ({}) and its help rows disagree",
                source.label(),
                source.file()
            );
        }
    }

    #[test]
    fn the_cut_at_the_test_module_keeps_the_routing_above_it() {
        // The bug this cut already had once: several views declare `#[cfg(test)]`
        // accessors above their `on_key`, so cutting at the first occurrence
        // read the whole of MKTS as a view with no keys — an empty set that
        // matched an empty expectation and proved nothing.
        let markets = codes_in("ui/views/markets.rs");
        assert!(
            markets.contains("Up") && markets.contains("Left"),
            "{markets:?}"
        );
        // And the tests below really are excluded: `confirm.rs` presses `q` at
        // the box to prove it is swallowed, and nothing binds `q` there.
        assert!(!codes_in("ui/widgets/confirm.rs").contains("Char('q')"));
    }

    #[test]
    fn the_scrape_can_actually_read_the_tree() {
        // A scanner that found nothing would make the test above pass for every
        // section with no rows, which is exactly how a grep-shaped pin dies
        // quietly. Two known tokens, one plain and one with a payload.
        let shell = codes_in("ui/shell.rs");
        assert!(shell.contains("Tab"), "{shell:?}");
        assert!(shell.contains("Char('q')"), "{shell:?}");
        assert!(!KEYMAP.is_empty());
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
