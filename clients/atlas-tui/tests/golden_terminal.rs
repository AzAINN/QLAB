//! The pane that draws a child's screen, and the border that says who is typing.
//!
//! Two claims, and they are different kinds of claim.
//!
//! The first is that the child's own bytes reach the screen unchanged. A parser
//! is fed two known lines here and the golden shows them inside the border, so
//! a renderer that dropped a row, reflowed one, or drew the frame over the
//! child's first line fails here rather than in front of an operator.
//!
//! The second is the border's copy. This pane is the one surface on the
//! workstation where every inner cell belongs to somebody else, so the border
//! is the *only* place the desk can say who holds the keyboard and which key
//! changes that. A pane that stopped saying it would read exactly like a hung
//! client — `q` swallowed, `/` swallowed, no way back — which is the failure
//! the focus ruling exists to prevent. Every case here therefore asserts the
//! way out is on the border, including the two where the news is bad.
//!
//! Gated with the module it draws: a monitoring build never obtains a screen,
//! so it carries neither the parser nor the widget that would render one.
#![cfg(feature = "operator")]

use atlas::ui::widgets::terminal;
use ratatui::{backend::TestBackend, Terminal};

/// Two known lines, exactly as a child on a pty writes them.
///
/// CR-LF and not `\n`: a pty is not in cooked-output translation for this
/// process, so a child's own bytes carry both, and a fixture that fed only the
/// line feed would stairstep down the pane and pin a screen no child produces.
const FROM_THE_CHILD: &[u8] = b"the desk is up on :8765\r\n> what changed in the regime read?\r\n";

/// The sentence the store hands over once the child is gone — `pty.rs`'s own
/// wording for a clean exit, with the remedy `PtyState::Ended` adds to it.
const ENDED: &str = "`qlab cli` ended on its own · /cli starts another";

/// A pane of exactly this size, with a parser sized to what is inside its
/// border — which is how the runtime sizes one, because the pty is told the
/// inner rect and not the pane.
fn pane(w: u16, h: u16, focused: bool, said: Option<&str>) -> String {
    let mut parser = vt100::Parser::new(h.saturating_sub(2), w.saturating_sub(2), 0);
    parser.process(FROM_THE_CHILD);
    let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
    term.draw(|f| terminal::draw(f, f.area(), parser.screen(), focused, said))
        .unwrap();
    format!("{}", term.backend())
}

/// The border's last row — where the desk says how the keyboard changes hands.
fn footer(frame: &str) -> String {
    frame.lines().next_back().unwrap_or_default().to_string()
}

// -- the child's screen ------------------------------------------------------

#[test]
fn the_pane_draws_the_childs_own_lines_inside_the_border() {
    let frame = pane(80, 10, false, None);
    for line in [
        "the desk is up on :8765",
        "> what changed in the regime read?",
    ] {
        assert!(
            frame.contains(line),
            "the child's line is not on the pane:\n{frame}"
        );
    }
    insta::assert_snapshot!(frame);
}

// -- who holds the keyboard --------------------------------------------------

#[test]
fn an_unfocused_pane_says_the_desk_types_and_names_both_ways_in() {
    let frame = pane(80, 10, false, None);
    assert!(
        footer(&frame).contains("the keyboard is the desk's · i or click to give it to Claude"),
        "{frame}"
    );
}

#[test]
fn a_focused_pane_says_the_child_types_and_names_the_key_back() {
    let frame = pane(80, 10, true, None);
    assert!(
        footer(&frame).contains("the keyboard is Claude's · ctrl-] returns it"),
        "{frame}"
    );
    insta::assert_snapshot!(frame);
}

#[test]
fn the_cursor_is_drawn_only_where_the_typing_goes() {
    // The block cursor is a claim about where the next keystroke lands. On a
    // pane the desk holds it would be pointing at a child that is not being
    // typed into — the same lie a focus border would tell, one cell wide.
    let focused = pane(80, 10, true, None);
    let desks = pane(80, 10, false, None);
    assert!(
        focused.contains('█'),
        "a focused pane drew no cursor:\n{focused}"
    );
    assert!(
        !desks.contains('█'),
        "an unfocused pane drew the child's cursor:\n{desks}"
    );
}

// -- the narrow terminal ----------------------------------------------------

#[test]
fn a_narrow_terminal_gets_a_whole_sentence_rather_than_a_clipped_one() {
    // Not the baseline: at 120×36 the pane is 77 cells and both long sentences
    // fit. This is a small terminal, where A5 has already dropped the board and
    // the pane has the whole content width and still has only 45 — under the 64
    // the long desk form needs. A clipped `…give it to Cl` is the one rendering
    // worse than a short one: it teaches half a key list and reads as a working
    // desk.
    let frame = pane(45, 8, false, None);
    let footer = footer(&frame);
    assert!(
        footer.contains("desk's keyboard · i or click to give it"),
        "{frame}"
    );
    assert!(
        !footer.contains("Claude"),
        "the long sentence was drawn clipped:\n{frame}"
    );
    insta::assert_snapshot!(frame);
}

#[test]
fn a_focused_narrow_terminal_still_names_the_key_that_returns_the_keyboard() {
    let frame = pane(45, 8, true, None);
    assert!(footer(&frame).contains("ctrl-] returns it"), "{frame}");
}

// -- the child that is gone --------------------------------------------------

#[test]
fn a_child_that_ended_says_so_where_the_keys_were() {
    // The ending replaces the focus sentence rather than sharing the row with
    // it: `i or click to give it to Claude` is an instruction with nothing
    // behind it once there is no child to give the keyboard to.
    let frame = pane(80, 10, false, Some(ENDED));
    assert!(footer(&frame).contains(ENDED), "{frame}");
    assert!(
        !footer(&frame).contains("i or click"),
        "the pane offered a child that has ended the keyboard:\n{frame}"
    );
    // And the cursor goes with the child: a block still sitting at the prompt
    // would say the next keystroke lands somewhere, and none of them do.
    assert!(!frame.contains('█'), "{frame}");
    insta::assert_snapshot!(frame);
}

#[test]
fn a_focused_pane_whose_child_ended_still_says_how_to_get_the_keyboard_back() {
    // The state the runtime must not create, drawn correctly anyway. A pane
    // holding the keyboard with no child to type into and no key named is the
    // hung client exactly — every keystroke vanishing, `q` included.
    let frame = pane(100, 10, true, Some(ENDED));
    assert!(footer(&frame).contains(ENDED), "{frame}");
    assert!(footer(&frame).contains("ctrl-] returns it"), "{frame}");
}

#[test]
fn a_border_with_room_for_only_one_of_them_keeps_the_way_out() {
    // The ending is what gives way, and it says so. The other order would
    // trade the key an operator needs for news about a child that has already
    // ended.
    let frame = pane(80, 10, true, Some(ENDED));
    assert!(footer(&frame).contains("ctrl-] returns it"), "{frame}");
    assert!(footer(&frame).contains('…'), "{frame}");
}

#[test]
fn an_ending_too_long_for_the_border_is_marked_rather_than_silently_cut() {
    let long = "`qlab cli` exited 3 · the desk kept everything it had already \
                written down, and /cli starts another";
    let frame = pane(45, 8, false, Some(long));
    assert!(footer(&frame).contains('…'), "{frame}");
}

// -- the floor ---------------------------------------------------------------

#[test]
fn a_pane_too_narrow_to_state_the_way_out_refuses_and_says_what_it_would_take() {
    // Not a smaller border with a shorter sentence: below this width there is
    // no phrasing left that still names the key, and a terminal an operator
    // cannot leave is worse than a column that says why it is empty.
    let frame = pane(36, 8, false, None);
    assert!(frame.contains("43×5"), "{frame}");
    assert!(frame.contains("36×8"), "{frame}");
    assert!(!frame.contains("the desk is up on"), "{frame}");
}

#[test]
fn a_pane_too_short_to_read_refuses_for_its_own_reason() {
    let frame = pane(80, 4, false, None);
    assert!(frame.contains("43×5"), "{frame}");
    assert!(frame.contains("80×4"), "{frame}");
}
