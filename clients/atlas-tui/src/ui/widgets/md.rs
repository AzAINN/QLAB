//! Markdown-light for the chat: the three marks a reasoner actually emits.
//!
//! The desk's answers arrive as prose with `**bold**`, `` `code` `` spans and
//! `- ` bullets, and a pane that printed the markers verbatim buried the
//! reading in asterisks. This is deliberately not a Markdown engine: three
//! inline marks and one line mark, chosen because they are what the reply
//! prompt produces — anything else passes through untouched, so a false parse
//! cannot eat an answer.
//!
//! The wrap is span-aware and hand-rolled for the same reason the chat's
//! plain wrap is: the scroll offset counts rendered rows, and a renderer-side
//! wrap would scroll by an amount that disagrees with what moved.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;

/// One styled fragment of a physical line, marks already consumed.
#[derive(Debug, Clone, PartialEq)]
enum Seg {
    Plain(String),
    Bold(String),
    Code(String),
}

/// A physical line tokenized into segments.
///
/// An unclosed mark is not a mark: `**thick` renders as the literal
/// asterisks, because eating a marker that never closes would silently delete
/// characters from an answer this pane exists to show verbatim.
fn segments(line: &str) -> Vec<Seg> {
    let mut out = Vec::new();
    let mut plain = String::new();
    let chars: Vec<char> = line.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '*' && i + 1 < chars.len() && chars[i + 1] == '*' {
            if let Some(close) = find(&chars, i + 2, "**") {
                if !plain.is_empty() {
                    out.push(Seg::Plain(std::mem::take(&mut plain)));
                }
                out.push(Seg::Bold(chars[i + 2..close].iter().collect()));
                i = close + 2;
                continue;
            }
        }
        if chars[i] == '`' {
            if let Some(close) = find(&chars, i + 1, "`") {
                if !plain.is_empty() {
                    out.push(Seg::Plain(std::mem::take(&mut plain)));
                }
                out.push(Seg::Code(chars[i + 1..close].iter().collect()));
                i = close + 1;
                continue;
            }
        }
        plain.push(chars[i]);
        i += 1;
    }
    if !plain.is_empty() {
        out.push(Seg::Plain(plain));
    }
    out
}

fn find(chars: &[char], from: usize, mark: &str) -> Option<usize> {
    let m: Vec<char> = mark.chars().collect();
    (from..=chars.len().saturating_sub(m.len())).find(|&at| chars[at..at + m.len()] == m[..])
}

/// The text as wrapped, styled rows: `base` is the message's own tone; bold
/// keeps it and adds weight, code takes the accent the desk uses for words
/// the operator can type back.
pub fn rows(text: &str, width: usize, base: Color, code: Color) -> Vec<Vec<Span<'static>>> {
    let width = width.max(8);
    let mut out: Vec<Vec<Span<'static>>> = Vec::new();
    for raw in text.lines() {
        // A blank source line is a paragraph break and stays one.
        if raw.trim().is_empty() {
            out.push(vec![Span::raw(String::new())]);
            continue;
        }
        let (lead, rest) = match raw.trim_start().strip_prefix("- ") {
            Some(rest) => ("· ", rest),
            None => ("", raw),
        };
        let mut row: Vec<Span<'static>> = Vec::new();
        let mut used = 0usize;
        if !lead.is_empty() {
            row.push(Span::styled(lead.to_string(), Style::default().fg(code)));
            used = lead.chars().count();
        }
        let indent = if lead.is_empty() { 0 } else { 2 };
        for seg in segments(rest) {
            let (body, style) = match seg {
                Seg::Plain(s) => (s, Style::default().fg(base)),
                Seg::Bold(s) => (s, Style::default().fg(base).add_modifier(Modifier::BOLD)),
                Seg::Code(s) => (s, Style::default().fg(code)),
            };
            for word in body.split_inclusive(' ') {
                let w = word.trim_end().chars().count();
                if used + w > width && used > indent {
                    out.push(std::mem::take(&mut row));
                    if indent > 0 {
                        row.push(Span::raw(" ".repeat(indent)));
                    }
                    used = indent;
                }
                row.push(Span::styled(word.to_string(), style));
                used += word.chars().count();
            }
        }
        if !row.is_empty() {
            out.push(row);
        }
    }
    if out.is_empty() {
        out.push(vec![Span::raw(String::new())]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flat(rows: &[Vec<Span<'_>>]) -> Vec<String> {
        rows.iter()
            .map(|r| r.iter().map(|s| s.content.as_ref()).collect::<String>())
            .collect()
    }

    #[test]
    fn the_marks_are_consumed_and_the_words_survive() {
        let out = rows("**bold** and `code` stay", 80, Color::White, Color::Cyan);
        let text = flat(&out).join("\n");
        assert!(!text.contains("**"), "{text}");
        assert!(!text.contains('`'), "{text}");
        assert!(text.contains("bold") && text.contains("code"), "{text}");
    }

    #[test]
    fn an_unclosed_mark_is_literal_rather_than_eaten() {
        // Deleting characters from an answer would be worse than showing the
        // marker: the pane's job is the words, verbatim when in doubt.
        let out = rows("**thick but thin", 80, Color::White, Color::Cyan);
        assert!(flat(&out)[0].contains("**thick"), "{:?}", flat(&out));
        let out = rows("a `dangling span", 80, Color::White, Color::Cyan);
        assert!(flat(&out)[0].contains('`'), "{:?}", flat(&out));
    }

    #[test]
    fn a_bullet_gets_its_glyph_and_its_hanging_indent() {
        let out = rows(
            "- first thing that is long enough to wrap onto another row",
            20,
            Color::White,
            Color::Cyan,
        );
        let text = flat(&out);
        assert!(text[0].starts_with("· "), "{text:?}");
        assert!(text[1].starts_with("  "), "wrap keeps the hang: {text:?}");
    }

    #[test]
    fn newlines_are_paragraphs_not_spaces() {
        let out = rows("one\n\ntwo", 80, Color::White, Color::Cyan);
        let text = flat(&out);
        assert_eq!(text.len(), 3, "{text:?}");
        assert_eq!(text[1], "");
    }
}
