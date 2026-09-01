//! The child's screen, drawn on the desk, inside a border that says who is typing.
//!
//! `pty.rs` owns the child and the store owns the parser; this module is handed
//! a `vt100::Screen` that has already been advanced and turns it into cells. It
//! opens nothing, spawns nothing and reads no environment — `ui/` never does IO
//! (`tests/operator_gate.rs`), and a frame is a pure function of the store, so
//! the parser is advanced in the event arm and never here.
//!
//! **This is the one panel on the workstation that earns a full border.**
//! `panel_header` argues the opposite for every other pane, and it is right
//! there: four cells of chrome carrying no information is most of a data column
//! on a three-column frame. It does not apply here, because every cell *inside*
//! this pane belongs to somebody else. The child draws its own prompt, its own
//! status line and its own colours over the whole inner rect, so the border is
//! the only row the desk still owns — and the desk has something it must say on
//! every frame.
//!
//! **What it must say is who holds the keyboard and which key changes that.**
//! While the pane holds it, every key belongs to the child — digits, `/`, `q`
//! and Ctrl-C included — so a pane that stopped naming the way out would read
//! exactly like a hung client. That is why the sentence has two whole forms
//! rather than one that clips. The settled pane is 77 cells at a 120×36
//! terminal, which both long sentences fit — but a narrow terminal is not a
//! narrow pane it can refuse its way out of. The desk form needs a width of 64
//! and the child form 48, so anything under those is an ordinary 80- or
//! 60-column window with the board beside it, and `…give it to Cl` there would
//! teach half a key list while looking like a working desk. Below the narrower
//! form's own width the pane refuses outright — a terminal an operator cannot
//! leave is worse than a column that says why it is empty.
//!
//! **The copy here is the stream's.** Task A4 implements the keys this border
//! names and may not reword them; a border and a keymap that disagreed about
//! which key returns the keyboard would be discovered by an operator who had
//! already lost it.

use crate::format;
use crate::theme::theme;
use crate::ui::widgets::{panel_header, refuse};
use ratatui::{
    layout::Rect,
    style::Style,
    text::{Line, Span},
    widgets::{Block, Borders},
    Frame,
};
use tui_term::widget::{Cursor, PseudoTerminal};

/// The desk holds the keyboard, and both of the ways to hand it over.
const DESKS: &str = "the keyboard is the desk's · i or click to give it to Claude";
/// The child holds it, and the one key that takes it back.
const CHILDS: &str = "the keyboard is Claude's · ctrl-] returns it";
/// The same two facts for a border with no room for the sentences above.
///
/// Shorter, not clipped: each still names the holder and every key that changes
/// the holding, which is the whole content. What they drop is the part a reader
/// of a terminal pane can already see — that the other end is Claude.
const DESKS_SHORT: &str = "desk's keyboard · i or click to give it";
const CHILDS_SHORT: &str = "Claude's keyboard · ctrl-] returns it";

/// The chrome a sentence on the border spends on something other than itself:
/// the two corners, and a space either side so the words do not touch the line.
const CHROME_W: u16 = 4;

/// What joining an ending to the keys costs beyond the two of them: the three
/// cells of ` · `, and the one [`format::bounded`] spends on its `…`.
///
/// Its own constant despite sharing a value with [`CHROME_W`] — the two count
/// different cells, and a border that grew a cell of padding would otherwise
/// silently change what fits beside an ending.
const JOIN_W: usize = 4;

/// The narrowest pane that can still state the way out, and the shortest.
///
/// `MIN_W` is the **longer** short form — [`DESKS_SHORT`], 39 cells — plus
/// [`CHROME_W`]. The longer of the two on purpose: the floor has to hold for
/// whichever sentence the pane is showing, and deriving it from
/// [`CHILDS_SHORT`] would read as two cells of headroom that do not exist.
/// One below it there is no remaining phrasing that names the key, and the pane
/// refuses rather than drawing a terminal whose exit is unsaid. `MIN_H` is the
/// two border rows and three of the child's: a session showing fewer than
/// three rows shows a prompt and none of what it answered.
const MIN_W: u16 = 43;
const MIN_H: u16 = 5;

/// What the child is, named on the border so the pane is not anonymous.
///
/// A constant rather than a parameter because the design rules the child is
/// always the desk's own verb — `qlab cli`, never `claude` directly — and a
/// pane that could be told it was running something else would be the place
/// that claim quietly stopped being true.
const CHILD: &str = " qlab cli ";

/// The child's own rect inside a pane drawn at `area`.
///
/// The geometry contract as a function, so the two places that need it — the
/// column that publishes what to resize to, and the call that opens a session —
/// cannot spell it differently. `tui-term` renders into the block's inner rect,
/// so this is what the pty must be sized to.
pub fn inner(area: Rect) -> Rect {
    Rect {
        x: area.x.saturating_add(1),
        y: area.y.saturating_add(1),
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(2),
    }
}

/// Whether a pane of this size draws a terminal rather than the refusal below.
///
/// Asked by the column before it publishes a rect a click can land on: a click
/// hands the keyboard to the child, and the refusal has no border to say which
/// key takes it back — so a keyboard lost over one could not be found again.
pub fn fits(area: Rect) -> bool {
    area.width >= MIN_W && area.height >= MIN_H
}

/// Draw the child's screen, and the border that says who is typing into it.
///
/// **`area` is the whole pane, border included, and the pty must be sized to
/// what is inside it** — [`inner`], which is `(area.width - 2, area.height - 2)`.
/// `tui-term` renders into the block's inner rect, so a child told the outer
/// size wraps its output two columns wider than the pane it is drawn in, and the
/// fold lands two rows below the last one on screen.
///
/// `said` is the sentence a child that is no longer running left behind — the
/// store's `PtyState::Ended`, which already names what happened *and* how to
/// start another. It is `Some` exactly when there is no live child, and the
/// border then states the ending instead of offering a keyboard to a process
/// that has ended.
pub fn draw(f: &mut Frame, area: Rect, screen: &vt100::Screen, focused: bool, said: Option<&str>) {
    if !fits(area) {
        refuse(
            f,
            area,
            format!(
                "this pane is {}×{} and a terminal needs {MIN_W}×{MIN_H} here: two border rows \
                 and three of the child's, and a border wide enough to name the key that \
                 returns the keyboard.",
                area.width, area.height
            ),
        );
        return;
    }
    let t = theme();
    // The bar goes in the title rather than at the panel's left edge, which is
    // where `panel_header` puts it: the edge is the border. The padding is the
    // block's, not the vocabulary's — a title butted against the corner reads
    // as a broken line rather than as a name.
    let mut title = panel_header("atlas");
    title.spans.insert(0, Span::raw(" "));
    title.spans.push(Span::raw(" "));
    let tone = match (focused, said.is_some()) {
        // The live question outranks the ending: this pane is still taking
        // keystrokes, whatever it is taking them for.
        (true, _) => t.accent,
        (false, true) => t.text_secondary,
        (false, false) => t.text_dim,
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(match focused {
            true => t.accent,
            false => t.border_med,
        }))
        // `PseudoTerminal` clears the whole rect before it draws, so without a
        // style here the pane would punch a terminal-default hole in the desk
        // wherever the child has not written.
        .style(Style::default().bg(t.bg_base))
        .title_top(title)
        .title_top(
            Line::from(Span::styled(CHILD, Style::default().fg(t.text_tertiary))).right_aligned(),
        )
        .title_bottom(Line::from(Span::styled(
            format!(" {} ", footer(room(area.width), focused, said)),
            Style::default().fg(tone),
        )));
    // A block cursor is a claim about where the next keystroke lands, so it is
    // drawn only where one would land: not on a pane the desk holds, and not on
    // a child that has ended and will never move it again.
    let cursor = Cursor::default()
        .visibility(focused && said.is_none())
        .style(Style::default().fg(t.accent));
    f.render_widget(
        PseudoTerminal::new(screen).block(block).cursor(cursor),
        area,
    );
}

/// The cells a sentence on the bottom border actually has.
fn room(width: u16) -> usize {
    width.saturating_sub(CHROME_W) as usize
}

/// The border's bottom line: the ending if there is one, and otherwise — or as
/// well, when the pane is still holding a keyboard it should not be — who has
/// the keyboard and which key changes that.
///
/// **The key that returns the keyboard is the last thing to go.** Everything
/// else this line can carry is news about a child that has already ended; the
/// clause that says how to get out is the one an operator needs *because* the
/// news is bad. So the ending gives way to it, marked, rather than the other
/// way round.
fn footer(room: usize, focused: bool, said: Option<&str>) -> String {
    let (long, short) = match focused {
        true => (CHILDS, CHILDS_SHORT),
        false => (DESKS, DESKS_SHORT),
    };
    let keys = match long.chars().count() <= room {
        true => long,
        false => short,
    };
    let Some(said) = said else {
        return keys.to_string();
    };
    if !focused {
        // No child to hand a keyboard to, and nobody typing at the one that is
        // gone: `i or click to give it to Claude` would be an instruction with
        // nothing behind it, so the ending is the whole line. Bounded one under
        // the room so the `…` that says there was more is itself on screen.
        return match said.chars().count() <= room {
            true => said.to_string(),
            false => format::bounded(said, room.saturating_sub(1)),
        };
    }
    // Focused with no child is a state the runtime must not produce — A3 ends
    // the session and A4 hands the keyboard back — and is drawn correctly
    // anyway: a pane holding the keyboard with nothing to type into and no key
    // named is the hung client exactly, every keystroke vanishing, `q` too.
    for keys in [keys, short] {
        let both = format!("{said} · {keys}");
        if both.chars().count() <= room {
            return both;
        }
    }
    match room.checked_sub(short.chars().count() + JOIN_W) {
        Some(budget) if budget > 0 => format!("{} · {short}", format::bounded(said, budget)),
        _ => short.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The border room a 45-cell pane has. Not the baseline — at 120×36 the
    /// settled pane is 77 and both long sentences fit — but the narrow-TERMINAL
    /// case the short forms exist for, small enough that A5 has already dropped
    /// the board and given the pane the whole content width.
    const NARROW: usize = 41;

    #[test]
    fn the_rect_the_child_is_given_is_the_block_the_pane_actually_draws() {
        // The contract, checked against the block rather than restated: a
        // padding or a border this pane stopped drawing would change what is
        // inside it, and a child still told `(w-2, h-2)` would wrap to a
        // geometry nothing on screen has.
        let area = Rect::new(9, 1, 77, 34);
        assert_eq!(
            inner(area),
            Block::default().borders(Borders::ALL).inner(area)
        );
    }

    #[test]
    fn the_long_sentence_is_drawn_whole_or_not_at_all() {
        assert_eq!(footer(room(80), false, None), DESKS);
        assert_eq!(footer(room(80), true, None), CHILDS);
        // One cell under its own width, each falls to the other whole form
        // rather than to a fragment of itself.
        assert_eq!(footer(DESKS.chars().count() - 1, false, None), DESKS_SHORT);
        assert_eq!(footer(CHILDS.chars().count() - 1, true, None), CHILDS_SHORT);
    }

    #[test]
    fn the_settled_pane_shows_the_long_forms_and_the_crossovers_are_where_stated() {
        // The module doc names 77, 64 and 48. A number stated in prose and
        // checked nowhere is exactly how a stale "the column is 45 cells"
        // survived a whole task.
        assert_eq!(footer(room(77), false, None), DESKS);
        assert_eq!(footer(room(77), true, None), CHILDS);
        for (width, focused, long, short) in [
            (64u16, false, DESKS, DESKS_SHORT),
            (48u16, true, CHILDS, CHILDS_SHORT),
        ] {
            assert_eq!(footer(room(width), focused, None), long, "at {width}");
            let under = width - 1;
            assert_eq!(footer(room(under), focused, None), short, "at {under}");
        }
    }

    #[test]
    fn a_narrow_terminal_gets_a_whole_sentence() {
        // The regression this pair exists for: both long forms are wider than
        // an 80-column window's pane, so a renderer with one form would have
        // clipped every frame drawn on one.
        assert!(DESKS.chars().count() > NARROW);
        assert!(CHILDS.chars().count() > NARROW);
        assert_eq!(footer(NARROW, false, None), DESKS_SHORT);
        assert_eq!(footer(NARROW, true, None), CHILDS_SHORT);
    }

    #[test]
    fn every_form_fits_the_narrowest_pane_that_is_drawn_at_all() {
        // The floor is derived from the copy, so the copy may not outgrow it:
        // a re-worded short form one cell too long would be clipped on exactly
        // the pane the floor was set to protect.
        for form in [DESKS_SHORT, CHILDS_SHORT] {
            assert!(form.chars().count() <= room(MIN_W), "{form:?}");
        }
        for focused in [true, false] {
            let line = footer(room(MIN_W), focused, None);
            assert!(line.chars().count() <= room(MIN_W), "{line:?}");
        }
    }

    #[test]
    fn a_child_that_ended_takes_the_line_from_the_keys() {
        let said = "`qlab cli` ended on its own · /cli starts another";
        let line = footer(room(80), false, Some(said));
        assert_eq!(line, said);
        assert!(!line.contains("i or click"));
    }

    #[test]
    fn an_ending_wider_than_the_border_is_marked_rather_than_cut_in_silence() {
        let said = "`qlab cli` exited 3 and the desk kept every fact it had already written down";
        let line = footer(NARROW, false, Some(said));
        assert!(line.ends_with('…'), "{line:?}");
        assert!(line.chars().count() <= NARROW, "{line:?}");
    }

    #[test]
    fn a_focused_pane_keeps_the_way_out_whatever_else_it_has_to_drop() {
        let said = "`qlab cli` ended on its own · /cli starts another";
        // Wide enough for both, whole.
        let wide = footer(room(100), true, Some(said));
        assert!(wide.starts_with(said), "{wide:?}");
        assert!(wide.ends_with(CHILDS), "{wide:?}");
        // Not wide enough for both: the ending is what gives way, and it says
        // so — the key that returns the keyboard is still named in full.
        for width in [MIN_W, 60, 80] {
            let line = footer(room(width), true, Some(said));
            assert!(
                line.ends_with(CHILDS_SHORT) || line.ends_with(CHILDS),
                "{width}: {line:?}"
            );
            assert!(line.chars().count() <= room(width), "{width}: {line:?}");
        }
    }
}
