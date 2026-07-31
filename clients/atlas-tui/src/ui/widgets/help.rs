//! The help overlay: every binding this workstation has, from the one table it has them in.
//!
//! Generated from `input::KEYMAP` rather than written beside it, because a
//! hand-written help screen is a second account of the keyboard and the two
//! diverge on the first binding somebody adds in a hurry. The table is checked
//! against the routers' own source (`input::tests`), so what is drawn here is
//! what the client will actually do.
//!
//! Posture-filtered like every other affordance: a glass window is shown no key
//! it would refuse. Absent, not greyed — a disabled row says "this client could
//! do that if you asked it differently", which is the claim the posture exists
//! to make impossible.

use crate::input::{bindings, Source};
use crate::store::Posture;
use crate::theme::theme;
use crate::ui::widgets::{panel_header, refuse};
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};

/// The key column. `Backspace` and `Shift-Tab` are the longest an operator
/// presses; anything narrower puts the action against the key.
const KEY_W: usize = 11;

/// The overlay's floor.
///
/// Below it the box would carry a border and a header and no readable row,
/// which is the invisible-control shape the workforce picker refuses at. Here
/// the refusal has to name its own exit as well: unlike that picker, this
/// surface holds the keyboard by *being* the focus, so an operator who cannot
/// see it needs the way out written down.
const MIN_W: u16 = 46;
const MIN_H: u16 = 8;

/// One keystroke into the overlay. Returns whether it stays open.
///
/// `rows` is what the frame would draw, so the scroll cannot run past the end
/// of the list. It is clamped again at draw time against the height actually
/// allocated — the key handler is never told a geometry.
pub fn on_key(k: KeyEvent, top: &mut usize, rows: usize) -> bool {
    match k.code {
        // `?` closes as well as opens: the key an operator pressed to get here
        // is the one they reach for to leave, and a modal with one exit is a
        // modal somebody gets stuck in.
        KeyCode::Esc | KeyCode::Char('?') => return false,
        KeyCode::Up => *top = top.saturating_sub(1),
        KeyCode::Down => *top = (*top + 1).min(rows.saturating_sub(1)),
        _ => {}
    }
    true
}

/// How many lines the overlay would draw.
///
/// Independent of the width — a narrow box shortens sentences, it never drops
/// rows — which is what lets the key handler clamp the scroll without being
/// told a geometry it cannot know.
pub fn rows(posture: Posture) -> usize {
    lines(posture, u16::MAX).len()
}

/// Every line the overlay would draw, in section order, at `width` cells.
pub fn lines(posture: Posture, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let room = (width as usize).saturating_sub(KEY_W + 1);
    let mut out = Vec::new();
    for source in Source::ALL {
        let mut section = bindings(posture).filter(|b| b.source == source).peekable();
        if section.peek().is_none() {
            // A view with no keys of its own, or a section this posture has
            // none of. A header over nothing reads as a binding that failed to
            // render.
            continue;
        }
        if !out.is_empty() {
            out.push(Line::from(""));
        }
        out.push(panel_header(source.label()));
        for binding in section {
            out.push(Line::from(vec![
                Span::styled(
                    format!(" {:<KEY_W$}", binding.key),
                    Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    clip(binding.action, room),
                    Style::default().fg(t.text_secondary),
                ),
            ]));
        }
    }
    out
}

/// `text` in at most `width` cells, and marked when it was cut.
///
/// A `Paragraph` would clip it silently, which turns "claimed so no view can
/// take it" into "claimed so no view can take" — a shortened sentence that
/// reads as a complete one. The ellipsis is what says otherwise.
fn clip(text: &str, width: usize) -> String {
    if text.chars().count() <= width {
        return text.to_string();
    }
    match width {
        0 => String::new(),
        _ => text.chars().take(width - 1).collect::<String>() + "…",
    }
}

/// Draw the overlay over whatever is behind it.
pub fn draw(f: &mut Frame, area: Rect, posture: Posture, top: usize) {
    if area.width < MIN_W || area.height < MIN_H {
        // Three rows and a short sentence, because the refusal has to survive
        // its own floor: at these widths a long one wraps past the rows it was
        // given and is clipped to a fragment, which is the failure the whole
        // refusal discipline exists to avoid.
        let row = Rect {
            x: area.x,
            y: area.y + area.height / 2,
            width: area.width,
            height: 3.min(area.height),
        };
        f.render_widget(Clear, row);
        refuse(
            f,
            row,
            format!("the key list needs {MIN_W}×{MIN_H} — Esc closes"),
        );
        return;
    }
    let t = theme();
    let rect = centred(area);
    f.render_widget(Clear, rect);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(t.accent))
        .style(Style::default().bg(t.bg_raised));
    let inner = block.inner(rect);
    f.render_widget(block, rect);

    let all = lines(posture, inner.width);
    // One row of the box is the footer, which says how to leave and whether
    // there is more below. A list that scrolled with no marker would look like
    // the whole keyboard to an operator who cannot see the rows under it.
    let room = inner.height.saturating_sub(1) as usize;
    let top = top.min(all.len().saturating_sub(room.max(1)));
    let shown: Vec<Line> = all.iter().skip(top).take(room).cloned().collect();
    let below = all.len().saturating_sub(top + shown.len());
    let mut body = shown;
    body.push(Line::from(Span::styled(
        if below > 0 {
            format!(" ↓ {below} more · Esc closes")
        } else if top > 0 {
            " ↑ ↓ scrolls · Esc closes".to_string()
        } else {
            " Esc closes".to_string()
        },
        Style::default().fg(t.text_dim),
    )));
    f.render_widget(Paragraph::new(body), inner);
}

/// Most of the frame, centred, and never larger than it.
fn centred(area: Rect) -> Rect {
    // Wide enough for the longest action beside the key column, so the box
    // does not spend its life abbreviating itself on a terminal that has the
    // room. Narrower frames clip with an ellipsis rather than silently.
    let w = area.width.saturating_sub(8).clamp(1, 78);
    let h = area.height.saturating_sub(4).max(1);
    Rect {
        x: area.x + (area.width.saturating_sub(w)) / 2,
        y: area.y + (area.height.saturating_sub(h)) / 2,
        width: w,
        height: h,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::input::KEYMAP;
    use crossterm::event::KeyModifiers;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn the_overlay_lists_every_binding_this_window_has() {
        // The property the whole table exists for: a key that is in the router
        // is in the table (`input::tests`), and a key in the table is on this
        // screen. Break either half and a binding becomes undiscoverable.
        let drawn: Vec<String> = lines(Posture::Glass, 120)
            .iter()
            .map(|line| {
                line.spans
                    .iter()
                    .map(|s| s.content.as_ref())
                    .collect::<String>()
            })
            .collect();
        let text = drawn.join("\n");
        for binding in KEYMAP.iter().filter(|b| !b.writes) {
            assert!(
                drawn
                    .iter()
                    .any(|row| row.contains(binding.key) && row.contains(binding.action)),
                "{} / {} is bound and undiscoverable",
                binding.key,
                binding.action
            );
        }
        // And the sections an operator navigates by. `panel_header` uppercases
        // its title, which is why these are matched case-blind.
        for section in ["anywhere", "the command line", "BOOK"] {
            assert!(
                text.to_lowercase().contains(&section.to_lowercase()),
                "{section} is not a section of the overlay:\n{text}"
            );
        }
    }

    #[test]
    fn a_glass_overlay_shows_no_key_that_could_move_money() {
        let text: String = lines(Posture::Glass, 120)
            .iter()
            .flat_map(|line| line.spans.iter().map(|s| s.content.to_string()))
            .collect::<Vec<_>>()
            .join(" ");
        for gone in ["execute", "approve", "reject", "start a governed workflow"] {
            assert!(!text.contains(gone), "a glass overlay offers `{gone}`");
        }
    }

    #[cfg(feature = "operator")]
    #[test]
    fn an_armed_overlay_shows_them_and_says_the_box_comes_first() {
        let text: String = lines(Posture::Operator, 120)
            .iter()
            .flat_map(|line| line.spans.iter().map(|s| s.content.to_string()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(text.contains("confirmation box"), "{text}");
        assert!(text.contains("approve"), "{text}");
        assert!(lines(Posture::Operator, 120).len() > lines(Posture::Glass, 120).len());
    }

    #[test]
    fn the_scroll_walks_the_list_and_stops_at_both_ends() {
        let rows = lines(Posture::Glass, 120).len();
        let mut top = 0;
        assert!(on_key(key(KeyCode::Down), &mut top, rows));
        assert_eq!(top, 1);
        for _ in 0..rows * 2 {
            on_key(key(KeyCode::Down), &mut top, rows);
        }
        assert_eq!(top, rows - 1, "the scroll ran past the end of the list");
        for _ in 0..rows * 2 {
            on_key(key(KeyCode::Up), &mut top, rows);
        }
        assert_eq!(top, 0);
    }

    #[test]
    fn both_exits_close_it_and_nothing_else_does() {
        let mut top = 0;
        assert!(!on_key(key(KeyCode::Esc), &mut top, 10));
        assert!(!on_key(key(KeyCode::Char('?')), &mut top, 10));
        assert!(on_key(key(KeyCode::Char('q')), &mut top, 10));
        assert!(on_key(key(KeyCode::Enter), &mut top, 10));
    }
}
