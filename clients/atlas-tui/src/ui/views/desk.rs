//! DESK — Atlas's read of the desk, and the tiles that say what the desk holds.
//!
//! Two columns. **THE READ** on the left is the view's reason to exist: the
//! conviction and the agreement, the state the numbers are in, what the evidence
//! disagrees about, what would change the conclusion, what the qualitative
//! record actually said, and — under all of it — why the desk is doing what it
//! is doing. It is judgment, not data, so it is stated in sentences and it says
//! what it is grounded in. **The tiles** on the right are the six facts an
//! operator glances at between reads: what the book is worth, what regime the
//! desk thinks it is in, where the guardrails stand, what the referee last said,
//! how the book replays through three crises, and how far its weights sit from
//! their targets.
//!
//! A changed `atlas_read.as_of` types the read back in over 600 ms. The reveal
//! is a substring render, not an effect: `FlashTracker::revealed` turns two
//! instants into a fraction and this slices the body to it, top to bottom, so a
//! mid-reveal frame is exact arithmetic rather than a sampled animation. The
//! panel header does not participate — chrome does not type itself in, and a
//! pane whose title arrived letter by letter would read as a rendering fault.
//!
//! Where this overlaps its neighbours, and why that is not two accounts of one
//! fact:
//!
//! - The **pulse rail** carries a four-line summary of the same read on every
//!   view, because "what does Atlas make of this" must never be a view switch
//!   away. Both read the same `atlas_read` fields, so they cannot disagree; this
//!   is the whole read, the rail is the glance.
//! - The **equity hero** is the one other place the book's value renders. BOOK's
//!   ribbon owns the KPI vocabulary — cash, P&L, exposure, drawdown — and none
//!   of it is repeated here. Both read `live_portfolio.equity`, never the
//!   registry's `portfolio`, for the reason BOOK's module doc states: the two
//!   are different views of the same desk and their disagreement is a
//!   reconciliation finding, not a display choice.
//! - The **why lines** were the whole of this view before Task 14. They survive
//!   as a section of THE READ: the status line's `propose·auto` chip says which
//!   authority Atlas holds, and only these sentences say what has fired under it
//!   and why a dispatch cannot be driven.

use crate::cmd::Command;
use crate::format::{self, text, MISSING};
use crate::fx::FlashTracker;
use crate::model::{AtlasRead, Decision, Replay, Snapshot};
use crate::store::Store;
use crate::theme::theme;
use crate::ui::views::View;
use crate::ui::widgets::{panel_block, panel_header, refuse};
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};
use serde_json::Value;
use std::collections::BTreeSet;
use std::time::Instant;
use throbber_widgets_tui::{Throbber, ThrobberState};
use tui_big_text::{BigText, PixelSize};
use unicode_width::UnicodeWidthStr;

/// The read's floor. Below this, prose stops being paragraphs: a 20-cell column
/// wraps a sentence into a column of two-word fragments, which is the vertical
/// smear the ledger's Task 13 finding names.
const READ_W: u16 = 24;

/// One tile column: a nine-cell label and the widest value beside it
/// (`-100.0% / 100.0%`), plus the space between them.
const TILE_W: u16 = 22;

/// The tile grid: two columns and the cell between them.
const GRID_W: u16 = TILE_W * 2 + 1;

/// The hero's own band — the panel header, four rows of quadrant glyphs, and the
/// rule the block reserves.
const HERO_H: u16 = 6;

/// A tile at its floor: header, two lines, and the rule.
const TILE_H: u16 = 4;

/// The allocation strip at its floor: header, two holdings, and the rule. Wider
/// than a tile row because a holding is a row and a book has several.
const ALLOC_H: u16 = 5;

/// The label column inside a tile, plus the space `kv` puts after it. Eight
/// cells holds `HEADROOM` and `DETECTOR`, the longest labels any tile uses, and
/// leaves `VOL/LIM  10.1% / 30.0%` — the widest row on the view — at exactly
/// `TILE_W`. A ninth would push that row a cell into its neighbour.
const LABEL_W: usize = 8;

/// How many characters of a reason a tile will carry before it is quoting an
/// essay at a column. The referee's `reasons` run to thousands; Task 18's audit
/// view is where the whole record belongs.
const REASON_MAX: usize = 220;

pub struct DeskView;

impl View for DeskView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant) {
        // Three fifths to the tiles, but never less than the grid's own floor:
        // a percentage alone puts the split's cliff at whatever width 60% of the
        // frame happens to cross the floor at, which on this layout is a column
        // either side of the workstation's baseline frame.
        let grid_w = (area.width * 3 / 5).max(GRID_W);
        if area.width < grid_w.saturating_add(READ_W) {
            // Not wide enough to sit them side by side. The read keeps the frame
            // — it is the view's reason to exist — and the grid says what it
            // would have taken, because a dropped column and one that was never
            // built look identical from outside.
            draw_read(f, area, store, fx, now, Some(grid_w));
            return;
        }
        // The gutter comes out of the read's column rather than the grid's: the
        // grid is sized to its contents to the cell, and the read is prose that
        // one column narrower still wraps.
        let cols = Layout::horizontal([Constraint::Min(0), Constraint::Length(grid_w)])
            .spacing(1)
            .split(area);
        draw_read(f, cols[0], store, fx, now, None);
        draw_grid(f, cols[1], store);
    }

    fn on_key(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

// -- THE READ --------------------------------------------------------------

/// The read, revealed to wherever it has got to at `now`.
///
/// `dropped_grid` is the width the tiles would have needed, when the frame was
/// too narrow to carry them.
fn draw_read(
    f: &mut Frame,
    area: Rect,
    store: &Store,
    fx: &FlashTracker,
    now: Instant,
    dropped_grid: Option<u16>,
) {
    if area.width < READ_W {
        refuse_or_drop(
            f,
            area,
            format!(
                "THE READ needs {READ_W} columns for a sentence; this pane has {}.",
                area.width
            ),
        );
        return;
    }
    // The note about the dropped tiles is layout chrome, not read: it sits on
    // the last row and the read wraps above it, so a long read never pushes the
    // explanation off the frame.
    let (body_area, note_area) = match dropped_grid {
        Some(_) if area.height > 1 => (
            Rect {
                height: area.height - 1,
                ..area
            },
            Some(Rect {
                y: area.y + area.height - 1,
                height: 1,
                ..area
            }),
        ),
        _ => (area, None),
    };

    // The header carries the read's date and the footer its hashes, and neither
    // types itself in or falls into the fold: they are the label on the argument
    // rather than part of it, and a long read that pushed its own provenance off
    // the pane would leave every claim on screen untraceable.
    let read = store.snapshot.as_ref().and_then(|s| s.atlas_read.as_ref());
    let footer = grounding_lines(read);
    let body_rows = body_area.height.saturating_sub(1 + footer.len() as u16) as usize;

    let mut lines = vec![header_line(read, body_area.width)];
    // Fit first, reveal second: the pane's shape is then stable while the text
    // arrives, and the `▾ more` marker types itself in last rather than
    // flickering its own count on every frame of the reveal.
    let body = fit(read_lines(store, body_area.width as usize), body_rows);
    let shown = reveal_chars(char_count(&body), fx.revealed(now));
    lines.extend(revealed(body, shown));
    f.render_widget(Paragraph::new(lines), body_area);
    let footer_h = footer.len() as u16;
    if footer_h > 0 && body_area.height > footer_h {
        f.render_widget(
            Paragraph::new(footer),
            Rect {
                y: body_area.y + body_area.height - footer_h,
                height: footer_h,
                ..body_area
            },
        );
    }

    if let (Some(note), Some(grid_w)) = (note_area, dropped_grid) {
        let t = theme();
        // The shortfall, not the grid's width: an operator resizing a terminal
        // needs to know how many columns to add, and the tiles' own width is
        // not that number once the read is already holding some of them.
        let short = (grid_w + READ_W).saturating_sub(area.width);
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!("▸ the tiles need {short} more columns"),
                Style::default().fg(t.text_dim),
            ))),
            note,
        );
    }
}

/// Everything under the header, wrapped to `w` and ready to be revealed.
fn read_lines(store: &Store, w: usize) -> Vec<Line<'static>> {
    let t = theme();
    let snapshot = store.snapshot.as_ref();
    let Some(read) = snapshot.and_then(|s| s.atlas_read.as_ref()) else {
        let mut out = pending(store, "an Atlas read");
        out.push(Line::from(""));
        out.extend(why_lines(snapshot, w));
        return out;
    };

    let mut out = vec![
        // The conviction chip. Bold and coloured by band, because the number an
        // operator most needs off this pane is how much of it to believe.
        Line::from(vec![
            Span::styled("▮ ", Style::default().fg(conviction_tone(read.conviction))),
            Span::styled(
                format::opt_pct(read.conviction),
                Style::default()
                    .fg(conviction_tone(read.conviction))
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(" CONVICTION", Style::default().fg(t.text_secondary)),
        ]),
        Line::from(vec![
            Span::styled(
                format::upper(text(read.agreement.as_ref())),
                Style::default()
                    .fg(agreement_tone(text(read.agreement.as_ref())))
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(" · ", Style::default().fg(t.text_dim)),
            Span::styled(
                format::upper(text(read.quantitative_state.as_ref())),
                Style::default()
                    .fg(state_tone(text(read.quantitative_state.as_ref())))
                    .add_modifier(Modifier::BOLD),
            ),
        ]),
    ];

    // Amber for what the evidence argues about, cyan for what would settle it —
    // the two are different kinds of claim and a reader has to be able to tell
    // them apart before reading either.
    section(&mut out, "tensions", &read.tensions, t.accent, w);
    section(
        &mut out,
        "would change this",
        &read.would_change_my_mind,
        t.cyan,
        w,
    );
    section(
        &mut out,
        "observations",
        &read.observations,
        t.text_tertiary,
        w,
    );
    news_section(&mut out, read, w);
    out.push(Line::from(""));
    out.extend(why_lines(snapshot, w));
    out
}

/// A labelled block of claims, each led by a coloured bar. Nothing at all when
/// there are none: a heading over an empty list reads as data that failed to
/// arrive rather than as a section the read did not need.
fn section(out: &mut Vec<Line<'static>>, label: &str, items: &[String], bar: Color, w: usize) {
    let items: Vec<&str> = items.iter().filter_map(|s| text(Some(s))).collect();
    if items.is_empty() {
        return;
    }
    let t = theme();
    out.push(Line::from(""));
    out.push(Line::from(Span::styled(
        label.to_uppercase(),
        Style::default().fg(t.text_tertiary),
    )));
    for item in items {
        for (i, wrapped) in wrap(item, w.saturating_sub(2)).into_iter().enumerate() {
            out.push(Line::from(vec![
                Span::styled(if i == 0 { "▌ " } else { "  " }, Style::default().fg(bar)),
                Span::styled(wrapped, Style::default().fg(t.text_primary)),
            ]));
        }
    }
}

/// What the qualitative record said, and — loudly — when there is none because
/// the feed broke rather than because the tape was quiet.
fn news_section(out: &mut Vec<Line<'static>>, read: &AtlasRead, w: usize) {
    let t = theme();
    let news = read.news.as_ref();
    let tone = news.and_then(|n| text(n.tone.as_ref()));
    let headlines = news.map(|n| n.headlines.as_slice()).unwrap_or_default();
    if tone.is_none() && headlines.is_empty() && read.news_error.is_none() {
        return;
    }

    out.push(Line::from(""));
    out.push(Line::from(Span::styled(
        "NEWS",
        Style::default().fg(t.text_tertiary),
    )));
    if let Some(error) = text(read.news_error.as_ref()) {
        // Fail loud. An absent window and a broken feed are opposite facts about
        // the same silence, and the read is missing its qualitative half either
        // way — but only one of them is worth fixing.
        out.push(Line::from(Span::styled(
            "FEED UNAVAILABLE",
            Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
        )));
        for wrapped in wrap(error, w) {
            out.push(Line::from(Span::styled(
                wrapped,
                Style::default().fg(t.text_secondary),
            )));
        }
    }
    out.push(Line::from(vec![
        Span::styled(
            // `risk_off` is the owner's spelling, not a word: the underscore is
            // a key separator and reads as one on screen.
            format::upper(tone).replace('_', " "),
            Style::default()
                .fg(tone_colour(tone))
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(" · ", Style::default().fg(t.text_dim)),
        Span::styled(
            format::or_missing(read.news_source.as_ref()).to_string(),
            Style::default().fg(t.text_secondary),
        ),
    ]));
    for headline in headlines.iter().take(4) {
        let Some(title) = text(headline.headline.as_ref()) else {
            continue;
        };
        for (i, wrapped) in wrap(title, w.saturating_sub(2)).into_iter().enumerate() {
            out.push(Line::from(vec![
                Span::styled(
                    if i == 0 { "› " } else { "  " },
                    Style::default().fg(tone_colour(text(headline.tone.as_ref()))),
                ),
                Span::styled(wrapped, Style::default().fg(t.text_primary)),
            ]));
        }
        if let Some(source) = text(headline.source.as_ref()) {
            out.push(Line::from(Span::styled(
                format!("  {source}"),
                Style::default().fg(t.text_dim),
            )));
        }
    }
}

/// The panel title, and which read this is.
///
/// The date rides the header rather than the footer because it is the one thing
/// on the pane that says *whether this is new* — and because it is what the
/// reveal fires on, an operator watching the read type itself in has to be able
/// to see it change.
fn header_line(read: Option<&AtlasRead>, width: u16) -> Line<'static> {
    let mut line = panel_header("the read");
    let Some(as_of) = read.and_then(|r| text(r.as_of.as_ref())) else {
        return line;
    };
    let stamp = format!("  {as_of}");
    if line.width() + stamp.width() <= width as usize {
        line.spans
            .push(Span::styled(stamp, Style::default().fg(theme().text_dim)));
    }
    line
}

/// What the read is grounded in, stated so it can be looked up.
///
/// The hashes are printed whole rather than shortened: a truncated hash is not
/// the hash, and this footer exists so a claim on screen can be traced back to
/// the exact window and read that produced it. One fact per line for the same
/// reason — a wrapped hash is a hash an operator has to reassemble by eye.
fn grounding_lines(read: Option<&AtlasRead>) -> Vec<Line<'static>> {
    let t = theme();
    let Some(read) = read else {
        return Vec::new();
    };
    ["read", "window"]
        .into_iter()
        .zip([
            format::or_missing(read.read_hash.as_ref()),
            grounding_hash(read.grounding.as_ref()).unwrap_or(MISSING),
        ])
        .map(|(label, hash)| {
            Line::from(Span::styled(
                format!("{label} {hash}"),
                Style::default().fg(t.text_dim),
            ))
        })
        .collect()
}

/// The window the news was grounded in. `grounding` is free-form — the owner
/// replaces the whole object with `{"hashes": []}` when it has to zero it — so
/// the one field a footer can rely on is dug rather than modelled.
fn grounding_hash(grounding: Option<&Value>) -> Option<&str> {
    grounding?
        .get("window_hash")?
        .as_str()
        .filter(|s| !s.is_empty())
}

/// The why lines, as a labelled block.
fn why_lines(snapshot: Option<&Snapshot>, w: usize) -> Vec<Line<'static>> {
    let t = theme();
    let mut out = vec![Line::from(Span::styled(
        "ATLAS",
        Style::default().fg(t.text_tertiary),
    ))];
    for bullet in why(snapshot) {
        for (i, wrapped) in wrap(&bullet, w.saturating_sub(2)).into_iter().enumerate() {
            out.push(Line::from(vec![
                Span::styled(
                    if i == 0 { "· " } else { "  " },
                    Style::default().fg(t.text_secondary),
                ),
                Span::styled(wrapped, Style::default().fg(t.text_secondary)),
            ]));
        }
    }
    out
}

// -- the reveal ------------------------------------------------------------

/// How many characters of the read are on screen at `fraction` of the reveal.
///
/// Truncating rather than rounding: a fraction of a character is not a
/// character, and rounding up would put the last letter of the read on screen
/// one frame before the reveal finished.
fn reveal_chars(total: usize, fraction: f64) -> usize {
    (total as f64 * fraction.clamp(0.0, 1.0)) as usize
}

/// The first `budget` characters of `lines`, top to bottom, styles intact.
///
/// Cut at character boundaries, never at byte offsets: a headline carries `—`
/// and `’`, and slicing one in half panics rather than rendering.
fn revealed(lines: Vec<Line<'static>>, mut budget: usize) -> Vec<Line<'static>> {
    let mut out = Vec::new();
    for line in lines {
        if budget == 0 {
            break;
        }
        let mut spans = Vec::new();
        for span in line.spans {
            let count = span.content.chars().count();
            if count <= budget {
                budget -= count;
                spans.push(span);
                continue;
            }
            let cut = span
                .content
                .char_indices()
                .nth(budget)
                .map(|(at, _)| at)
                .unwrap_or(span.content.len());
            spans.push(Span::styled(span.content[..cut].to_string(), span.style));
            budget = 0;
            break;
        }
        out.push(Line::from(spans));
    }
    out
}

fn char_count(lines: &[Line<'static>]) -> usize {
    lines
        .iter()
        .flat_map(|line| line.spans.iter())
        .map(|span| span.content.chars().count())
        .sum()
}

// -- the tiles -------------------------------------------------------------

/// The hero, the two tile rows, and the allocation strip.
fn draw_grid(f: &mut Frame, area: Rect, store: &Store) {
    if area.height < HERO_H + 1 {
        refuse_or_drop(
            f,
            area,
            format!(
                "the tiles need {} rows for the equity hero; this pane has {}.",
                HERO_H + 1,
                area.height
            ),
        );
        return;
    }
    let (heights, dropped) = grid_rows(area.height);
    let mut constraints: Vec<Constraint> = heights.iter().map(|h| Constraint::Length(*h)).collect();
    if !dropped.is_empty() {
        constraints.push(Constraint::Length(1));
    }
    let rows = Layout::vertical(constraints).split(area);

    // A cell of gutter, or the widest row in the left tile abuts the label
    // column of the right one and the two read as one wrapped line.
    let pair = |row: Rect| {
        Layout::horizontal([Constraint::Ratio(1, 2); 2])
            .spacing(1)
            .split(row)
    };
    draw_hero(f, rows[0], store);
    if let Some(row) = rows.get(1) {
        let cells = pair(*row);
        tile(f, cells[0], "regime", regime_body(store));
        tile(f, cells[1], "alerts", alerts_body(store, cells[1].width));
    }
    if let Some(row) = rows.get(2) {
        tile(f, *row, "allocation", allocation_body(store, row.width));
    }
    if let Some(row) = rows.get(3) {
        let cells = pair(*row);
        tile(f, cells[0], "verdict", verdict_body(store, cells[0].width));
        tile(f, cells[1], "replay", replay_body(store, cells[1].width));
    }
    if !dropped.is_empty() {
        let t = theme();
        // A count and what it would take, not a list of names: the names run
        // past the pane and clip exactly the part that says `rows`, which is
        // the only actionable half of the sentence.
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!(
                    "▾ {} more tile{} need {} rows",
                    dropped.len(),
                    if dropped.len() == 1 { "" } else { "s" },
                    dropped_rows(&dropped)
                ),
                Style::default().fg(t.text_dim),
            ))),
            rows[rows.len() - 1],
        );
    }
}

/// How many more rows the dropped tiles would have taken.
fn dropped_rows(dropped: &[&'static str]) -> u16 {
    dropped
        .iter()
        .map(|name| match *name {
            "allocation" => ALLOC_H,
            _ => TILE_H,
        })
        .sum()
}

/// How tall each grid row is at `h`, and which rows would not fit.
///
/// Arithmetic rather than constraints, for the reason BOOK's footer states: a
/// short pane resolved by the solver shrinks whichever row the solver prefers,
/// which is not the same as the row the desk can most afford to lose. Rows go
/// from the bottom, so the last thing dropped is the equity hero and the first
/// is the pair an operator can reach on another view.
fn grid_rows(h: u16) -> (Vec<u16>, Vec<&'static str>) {
    let mut heights = vec![HERO_H];
    let mut dropped = Vec::new();
    let mut left = h - HERO_H;
    for (name, floor) in [
        ("regime and alerts", TILE_H),
        ("allocation", ALLOC_H),
        ("verdict and replay", TILE_H),
    ] {
        if left >= floor {
            heights.push(floor);
            left -= floor;
        } else {
            dropped.push(name);
        }
    }
    // The note about what was dropped costs a row of its own, taken from the
    // slack if there is any and from the last surviving row if there is not — a
    // tile one row shorter still holds a header, a fact and its rule.
    if !dropped.is_empty() {
        match left {
            0 => {
                if let Some(last) = heights.last_mut().filter(|h| **h > TILE_H) {
                    *last -= 1;
                }
            }
            _ => left -= 1,
        }
    }
    // The slack goes to the rows that hold lists, never to the hero: the hero's
    // glyphs are a fixed four rows tall and everything else is prose.
    let flexible = heights.len().saturating_sub(1);
    if flexible > 0 {
        let each = left / flexible as u16;
        for (i, height) in heights.iter_mut().skip(1).enumerate() {
            *height += each;
            if i + 1 == flexible {
                *height += left % flexible as u16;
            }
        }
    }
    (heights, dropped)
}

/// The book's value, at the size of the room rather than the size of the cell.
fn draw_hero(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(0)]).split(inner);
    f.render_widget(Paragraph::new(panel_header("equity")), rows[0]);

    let live = store
        .snapshot
        .as_ref()
        .and_then(|s| s.live_portfolio.as_ref());
    let Some(live) = live else {
        f.render_widget(Paragraph::new(pending(store, "a marked book")), rows[1]);
        return;
    };
    let Some(equity) = live.equity else {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                MISSING,
                Style::default()
                    .fg(t.text_secondary)
                    .add_modifier(Modifier::BOLD),
            ))),
            rows[1],
        );
        return;
    };

    let figure = format::money(equity);
    let glyph_w = figure.chars().count() as u16 * 4;
    if glyph_w <= rows[1].width && rows[1].height >= 4 {
        // Vertically centred in whatever the row was given: the glyphs are four
        // rows and the band grows with the frame, and a hero pinned to the top
        // of a tall band reads as a pane that failed to finish drawing.
        let pad = (rows[1].height - 4) / 2;
        let area = Rect {
            y: rows[1].y + pad,
            height: 4,
            ..rows[1]
        };
        f.render_widget(
            BigText::builder()
                .pixel_size(PixelSize::Quadrant)
                .style(Style::default().fg(t.text_primary))
                .lines(vec![figure.into()])
                .build(),
            area,
        );
        return;
    }
    if figure.width() as u16 <= rows[1].width {
        // Too narrow for the glyphs, wide enough for the number. Small is not a
        // lie; a clipped figure would be — `$1,234,567.89` cut to `$1,234,5` is
        // a number wrong by a factor of a hundred.
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                figure,
                Style::default()
                    .fg(t.text_primary)
                    .add_modifier(Modifier::BOLD),
            ))),
            rows[1],
        );
        return;
    }
    refuse_or_drop(
        f,
        rows[1],
        format!(
            "the equity needs {} columns; this pane has {}.",
            figure.width(),
            rows[1].width
        ),
    );
}

/// The guarded state the desk acts on, and the detector reading behind it.
fn regime_body(store: &Store) -> Vec<Line<'static>> {
    let regime = store
        .snapshot
        .as_ref()
        .and_then(|s| s.market.as_ref())
        .and_then(|m| m.regime.as_ref());
    let Some(regime) = regime else {
        return pending(store, "a regime read");
    };
    // `robust_state` and `regime` disagree on purpose — the guarded state is
    // what the desk acts on and the raw label is what one detector said — so
    // both are on screen rather than whichever happened to be present.
    let state = text(regime.robust_state.as_ref());
    vec![
        kv("state", format::upper(state), state_tone(state)),
        kv(
            "detector",
            format::upper(text(regime.regime.as_ref())),
            theme().text_secondary,
        ),
        kv(
            "sig/thr",
            format!(
                "{} / {}",
                format::opt_pct(regime.signal),
                format::opt_pct(regime.threshold)
            ),
            theme().text_primary,
        ),
        kv(
            "conf",
            format::opt_pct(regime.confidence),
            theme().text_primary,
        ),
    ]
}

/// Where the guardrails stand, and what the cost gate last refused.
fn alerts_body(store: &Store, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let Some(stress) = store.snapshot.as_ref().and_then(|s| s.stress.as_ref()) else {
        return pending(store, "a stress report");
    };
    let tier = text(stress.drawdown_tier.as_ref());
    let vol = stress.stressed_vol;
    let limit = stress.stress_vol_limit;
    let mut out = vec![
        kv("tier", format::upper(tier), tier_tone(tier)),
        kv(
            "headroom",
            format::opt_pct(stress.leverage_headroom),
            // Absent is neither over nor under the limit, so it takes neither
            // colour: a grey `--` is the only honest rendering of a headroom
            // nobody computed.
            match stress.leverage_headroom {
                Some(h) if negative_at_1dp(h) => t.negative,
                Some(_) => t.positive,
                None => t.text_secondary,
            },
        ),
        kv(
            "vol/lim",
            format!("{} / {}", format::opt_pct(vol), format::opt_pct(limit)),
            match (vol, limit) {
                (Some(vol), Some(limit)) if vol > limit => t.negative,
                (Some(_), Some(_)) => t.positive,
                _ => t.text_secondary,
            },
        ),
    ];
    match stress.cost_gate_refusals.first() {
        Some(refusal) => {
            out.push(kv(
                "gate",
                format!("{} REFUSED", stress.cost_gate_refusals.len()),
                t.negative,
            ));
            // What the gate said, not only that it spoke: "REFUSED" alone sends
            // an operator to another surface to learn whether the desk is broken
            // or the trade was simply too expensive.
            if let Some(reason) = refusal.reasons.first() {
                out.extend(note(&clip(reason, REASON_MAX), width, t.text_secondary));
            }
        }
        None => out.push(kv("gate", "CLEAR".to_string(), t.positive)),
    }
    out
}

/// The referee's last word, and which authority said it.
fn verdict_body(store: &Store, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let decisions: &[Decision] = store
        .snapshot
        .as_ref()
        .map(|s| s.decisions.as_slice())
        .unwrap_or_default();
    // The newest decision that carries a verdict, not the newest decision. Most
    // rows are logged before anything adjudicates them, so reading `[0]` prints
    // "no verdicts yet" over a desk whose referee passed a plan a minute ago —
    // which is what the Textual client does today.
    let latest = decisions
        .iter()
        .find_map(|d| d.verdict.as_ref().map(|v| (d, v)));
    let Some((decision, verdict)) = latest else {
        return vec![Line::from(Span::styled(
            "no verdict yet",
            Style::default().fg(t.text_secondary),
        ))];
    };
    let word = text(verdict.verdict.as_ref());
    let mut out = vec![Line::from(vec![
        Span::styled("▮ ", Style::default().fg(verdict_tone(word))),
        Span::styled(
            format::upper(word),
            Style::default()
                .fg(verdict_tone(word))
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("  {}", format::or_missing(verdict.source.as_ref())),
            Style::default().fg(t.text_secondary),
        ),
    ])];
    // What was adjudicated, and when. Wrapped rather than clipped: `PASS` over
    // a truncated `rebalance_ga` is a verdict about nothing nameable.
    for line in wrap(
        &format!(
            "{} {}",
            format::or_missing(decision.kind.as_ref()),
            format::or_missing(decision.as_of.as_ref())
        ),
        width as usize,
    ) {
        out.push(Line::from(Span::styled(
            line,
            Style::default().fg(t.text_dim),
        )));
    }
    if let Some(reason) = verdict.reasons.first() {
        out.extend(note(&clip(reason, REASON_MAX), width, t.text_secondary));
    }
    out
}

/// What this book would have done through three crises — and, where it cannot
/// be replayed, why not.
fn replay_body(store: &Store, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let Some(stress) = store.snapshot.as_ref().and_then(|s| s.stress.as_ref()) else {
        return pending(store, "a stress report");
    };
    let mut out = Vec::new();
    for window in ["2008", "2020", "2022"] {
        let replay = stress.replays.get(window);
        match replay_return(replay) {
            Some(ret) => out.push(kv(
                window,
                format::signed_pct1(ret),
                match negative_at_1dp(ret) {
                    true => t.negative,
                    false => t.positive,
                },
            )),
            None => {
                out.push(kv(window, MISSING.to_string(), t.text_secondary));
                // A window the owner could not replay says so in its own words.
                // The one thing this tile may never do is put a plausible
                // number where a refusal belongs.
                if let Some(reason) = replay.and_then(|r| text(r.reason.as_ref())) {
                    out.extend(note(reason, width, t.text_dim));
                }
            }
        }
    }
    out
}

/// A replay's return, but only when the owner said the window was available.
/// A return beside `available: false` is a number about a window that was not
/// covered, which is worse than no number at all.
fn replay_return(replay: Option<&Replay>) -> Option<f64> {
    let replay = replay?;
    (replay.available == Some(true))
        .then_some(replay.ret)
        .flatten()
        .filter(|r| r.is_finite())
}

/// Where the book sits against its targets, holding by holding.
fn allocation_body(store: &Store, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let Some(portfolio) = store.snapshot.as_ref().and_then(|s| s.portfolio.as_ref()) else {
        return pending(store, "a book to weigh");
    };
    // The union, not the held names: a target the book has not bought yet is
    // the most interesting row on this tile, and keying off `weights` alone
    // would hide exactly the drift a rebalance exists to close.
    let tickers: BTreeSet<&String> = portfolio
        .weights
        .keys()
        .chain(portfolio.target_weights.keys())
        .collect();
    if tickers.is_empty() {
        return vec![Line::from(Span::styled(
            "no weights recorded",
            Style::default().fg(t.text_secondary),
        ))];
    }
    // Ticker (5 and a space), bar and a space, then `100.0%→100.0%` at six
    // cells a side. The bar is what gives way when the pane narrows: two cells
    // of bar is not a bar, and the numbers beside it carry the whole fact on
    // their own.
    let bar_w = match width.saturating_sub(20) {
        w if w >= 4 => w as usize,
        _ => 0,
    };
    tickers
        .into_iter()
        .map(|ticker| {
            let current = portfolio.weights.get(ticker).copied();
            let target = portfolio.target_weights.get(ticker).copied();
            let mut spans = vec![Span::styled(
                format!("{:<5.5} ", ticker),
                Style::default().fg(t.text_primary),
            )];
            if bar_w > 0 {
                spans.extend(weight_bar(current, target, bar_w));
                spans.push(Span::raw(" "));
            }
            spans.extend([
                Span::styled(
                    format!("{:>6}", format::opt_pct(current)),
                    Style::default().fg(t.text_primary),
                ),
                Span::styled("→", Style::default().fg(t.text_dim)),
                Span::styled(
                    format!("{:>6}", format::opt_pct(target)),
                    Style::default().fg(t.text_secondary),
                ),
            ]);
            Line::from(spans)
        })
        .collect()
}

/// `▰▱` — what is held, how far it is from its target, and which side of it.
///
/// Three bands rather than one: solid amber to the smaller of the two, the
/// difference in dim amber when the book is under its target and in red when it
/// is over, and the track beyond. A single filled bar would say how much is held
/// and nothing about the only question a rebalance asks.
fn weight_bar(current: Option<f64>, target: Option<f64>, width: usize) -> Vec<Span<'static>> {
    let t = theme();
    let cells = |value: Option<f64>| -> usize {
        value
            .filter(|v| v.is_finite())
            .map(|v| (v.clamp(0.0, 1.0) * width as f64).round() as usize)
            .unwrap_or(0)
    };
    let held = cells(current);
    let wanted = target.map(|_| cells(target)).unwrap_or(held);
    let common = held.min(wanted);
    let (gap, gap_tone) = match held > wanted {
        // Over target is the direction that costs money to fix, so it is the
        // one that gets the loud colour.
        true => (held - wanted, t.negative),
        false => (wanted - held, t.accent_dim),
    };
    vec![
        Span::styled("▰".repeat(common), Style::default().fg(t.accent)),
        Span::styled(
            "▰".repeat(gap.min(width - common)),
            Style::default().fg(gap_tone),
        ),
        Span::styled(
            "▱".repeat(width.saturating_sub(common + gap)),
            Style::default().fg(t.text_dim),
        ),
    ]
}

// -- tile chrome -----------------------------------------------------------

/// A titled tile: header, body, and the rule the block reserves.
fn tile(f: &mut Frame, area: Rect, title: &str, body: Vec<Line<'static>>) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines = vec![panel_header(title)];
    lines.extend(fit(body, inner.height.saturating_sub(1) as usize));
    f.render_widget(Paragraph::new(lines), inner);
}

/// What a pane draws when the section behind it is not in the payload.
///
/// The distinction the throbber is for: an owner that is up and has not
/// described this yet is still working, and `--` would claim it looked and found
/// nothing. With no owner behind it there is nothing to be in flight from, so
/// the honest rendering is the missing one.
fn pending(store: &Store, what: &str) -> Vec<Line<'static>> {
    let t = theme();
    if !store.conn.owner {
        return vec![Line::from(Span::styled(
            MISSING,
            Style::default()
                .fg(t.text_secondary)
                .add_modifier(Modifier::BOLD),
        ))];
    }
    // The beat is the phase. `Throbber`'s own `Widget::render` picks a random
    // symbol on every frame, which would make a golden unpinnable and — worse —
    // leave a frozen client still looking busy.
    let mut state = ThrobberState::default();
    // Never zero: `calc_step(0)` is documented as choosing at random.
    state.calc_step(1 + (store.tick % 8) as i8);
    vec![Throbber::default()
        .throbber_style(Style::default().fg(t.accent))
        .label(Span::styled(
            format!("waiting for {what}"),
            Style::default().fg(t.text_secondary),
        ))
        .to_line(&state)]
}

/// A pane refusing to draw, unless the refusal would itself be a smear.
///
/// The ledger's Task 13 finding: `refuse` wraps, so in a handful of columns the
/// sentence renders as a vertical stack of single words, and in one or two rows
/// it truncates mid-clause. Both read as a broken pane rather than as a refusal,
/// so under that the pane draws nothing — a frame missing a panel is at least
/// not a frame lying about one.
fn refuse_or_drop(f: &mut Frame, area: Rect, message: String) {
    // Fifteen because the finding's own numbers say so: one to fourteen cells is
    // where a wrapped refusal becomes a column of single words. Deliberately
    // below `READ_W`, so a read pane between the two floors states its floor
    // rather than vanishing — a threshold equal to `READ_W` would make this
    // branch unreachable from the pane that needs it most.
    const LEGIBLE_W: u16 = 15;
    const LEGIBLE_H: u16 = 2;
    if area.width < LEGIBLE_W || area.height < LEGIBLE_H {
        return;
    }
    refuse(f, area, message);
}

/// A label/value row, aligned so a column of them reads as a column.
fn kv(label: &str, value: String, tone: Color) -> Line<'static> {
    Line::from(vec![
        Span::styled(
            format!("{:<LABEL_W$} ", label.to_uppercase()),
            Style::default().fg(theme().text_secondary),
        ),
        Span::styled(value, Style::default().fg(tone)),
    ])
}

/// A tile's prose, wrapped and indented under the row it belongs to.
///
/// Wrapped rather than clipped: a `Paragraph` cuts at the cell edge, and the
/// half of a cost-gate refusal that survives is not the half that says why.
fn note(text: &str, width: u16, tone: Color) -> Vec<Line<'static>> {
    wrap(text, (width as usize).saturating_sub(2))
        .into_iter()
        .map(|line| Line::from(Span::styled(format!("  {line}"), Style::default().fg(tone))))
        .collect()
}

/// Fit `lines` into `rows`, saying how many did not fit rather than clipping.
///
/// A `Paragraph` given more lines than rows simply stops drawing, so a tile that
/// overflowed and one that had nothing more to say look identical. This is the
/// one place that is decided, for every pane on the view.
fn fit(mut lines: Vec<Line<'static>>, rows: usize) -> Vec<Line<'static>> {
    if lines.len() <= rows {
        return lines;
    }
    if rows == 0 {
        return Vec::new();
    }
    let hidden = lines.len() - (rows - 1);
    lines.truncate(rows - 1);
    lines.push(Line::from(Span::styled(
        format!("▾ {hidden} more"),
        Style::default().fg(theme().text_dim),
    )));
    lines
}

/// Word-wrap to `width` cells, hard-splitting a word too long to ever fit.
///
/// Hand-rolled rather than `Paragraph::wrap` because the reveal has to know how
/// many characters are on screen and the overflow marker has to know how many
/// lines there are — and a widget that wraps at draw time can answer neither.
fn wrap(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut line = String::new();
    for word in text.split_whitespace() {
        let mut word = word;
        loop {
            let needed = word.width() + if line.is_empty() { 0 } else { 1 };
            if line.width() + needed <= width {
                if !line.is_empty() {
                    line.push(' ');
                }
                line.push_str(word);
                break;
            }
            if !line.is_empty() {
                out.push(std::mem::take(&mut line));
                continue;
            }
            // Longer than the pane on its own — a hash, or a URL. Split it
            // rather than leaving a line that can never be laid.
            let cut = word
                .char_indices()
                .nth(width)
                .map(|(at, _)| at)
                .unwrap_or(word.len());
            out.push(word[..cut].to_string());
            word = &word[cut..];
        }
    }
    if !line.is_empty() {
        out.push(line);
    }
    out
}

/// A reason cut to a length a tile can hold, with the cut visible.
fn clip(text: &str, max: usize) -> String {
    match text.char_indices().nth(max) {
        Some((at, _)) => format!("{}…", &text[..at]),
        None => text.to_string(),
    }
}

// -- tones -----------------------------------------------------------------

/// Whether a fraction is still negative once printed at one decimal of a
/// percent — the precision every number on this view is rendered to.
///
/// `Theme::change` takes the sign off the raw double, so a leverage headroom of
/// -2.2e-16 (what a fully invested paper book actually carries) draws `0.0%` in
/// red: the colour contradicting the digits beside it. `format` already takes
/// its sign off the *rounded* value for exactly this reason; this is that rule
/// where a colour is chosen rather than a string.
fn negative_at_1dp(fraction: f64) -> bool {
    fraction.is_finite() && (fraction * 1000.0).round() < 0.0
}

/// How much of the read to believe, in three bands. Ported from the Textual
/// client's conviction chip so the two surfaces band the same number the same
/// way.
fn conviction_tone(conviction: Option<f64>) -> Color {
    let t = theme();
    match conviction {
        Some(c) if c >= 0.6 => t.positive,
        Some(c) if c >= 0.35 => t.accent,
        Some(_) => t.text_secondary,
        None => t.text_secondary,
    }
}

fn agreement_tone(agreement: Option<&str>) -> Color {
    let t = theme();
    match agreement {
        Some("aligned") => t.positive,
        Some("divergent") => t.accent,
        // `quiet`, and anything the owner adds later: a word this client does
        // not know is not a state it may colour.
        _ => t.text_secondary,
    }
}

/// The state ramp, matching the pulse rail's: a desk that coloured `stress`
/// differently in two places would be two desks.
fn state_tone(state: Option<&str>) -> Color {
    let t = theme();
    match state.unwrap_or_default() {
        "calm" | "ok" => t.positive,
        "stress" | "stressed" => t.negative,
        _ => t.warning,
    }
}

fn tier_tone(tier: Option<&str>) -> Color {
    let t = theme();
    match tier.unwrap_or_default() {
        "none" => t.positive,
        "warning" => t.warning,
        "control" | "breaker" => t.negative,
        _ => t.text_secondary,
    }
}

fn tone_colour(tone: Option<&str>) -> Color {
    let t = theme();
    match tone.unwrap_or_default() {
        "risk_off" => t.negative,
        "risk_on" => t.positive,
        "mixed" => t.accent,
        _ => t.text_secondary,
    }
}

fn verdict_tone(verdict: Option<&str>) -> Color {
    let t = theme();
    match verdict.unwrap_or_default() {
        "PASS" => t.positive,
        "FAIL" => t.negative,
        _ => t.text_secondary,
    }
}

/// Why the desk is not doing anything, stated rather than implied.
///
/// Ported from the Textual client through `app.rs::Desk::why`: a desk that is
/// idle on purpose and one that is broken look identical from outside, and a
/// mode name answers neither. Never returns empty — an empty explainer is the
/// black-box failure this section exists to fix.
///
/// The old version had a fourth branch keyed on `atlas.open_tasks`. The owner
/// has never served that field (`Atlas.status()` does not build it; the queue
/// is a separate `atlas_tasks` list the model does not carry), so the branch
/// was dead and the count was always zero. `current_task_id` is the fact the
/// owner does report about work in flight.
pub fn why(snapshot: Option<&Snapshot>) -> Vec<String> {
    let atlas = snapshot.and_then(|s| s.atlas.as_ref());
    let beat = snapshot.and_then(|s| s.atlas_heartbeat.as_ref());
    let coordinator = beat.and_then(|b| b.coordinator.as_ref());

    let mode = atlas.and_then(|a| text(a.mode.as_ref()));
    let autonomous = beat.and_then(|b| b.autonomous).unwrap_or(false);
    let driving = coordinator.and_then(|c| c.driving).unwrap_or(false);
    let workflow = coordinator.and_then(|c| text(c.workflow_id.as_ref()));
    let reason = coordinator.and_then(|c| text(c.reason.as_ref()));

    let mut out = Vec::new();
    match mode {
        Some("observe") => out.push("Mode is OBSERVE: Atlas may start no workflow at all.".into()),
        Some("paused") => {
            out.push("Mode is PAUSED: monitoring continues, no new work is created.".into())
        }
        Some(mode) if !autonomous => out.push(format!(
            "Mode is {} but autonomy is OFF — Atlas queues work and waits.",
            mode.to_uppercase()
        )),
        Some(mode) => out.push(format!(
            "Mode is {} and autonomy is ON: Atlas starts what its mode permits.",
            mode.to_uppercase()
        )),
        // Absent is its own fact. Rendering "Mode is --" would read as a mode.
        None => out.push("The owner has not reported a mode for Atlas.".into()),
    }
    if driving {
        out.push(format!(
            "A coordinator is driving workflow {} right now.",
            workflow.unwrap_or(MISSING)
        ));
    } else if let Some(reason) = reason {
        out.push(format!("Cannot drive a run: {reason}"));
    }
    match atlas.and_then(|a| text(a.current_task_id.as_ref())) {
        Some(task) => out.push(format!("Task {task} is in flight.")),
        None => out.push(
            "No trigger has fired — no drawdown tier, drift breach, regime flip, \
             or data outage. Nothing to act on is not idle by accident."
                .into(),
        ),
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot(json: &str) -> Snapshot {
        serde_json::from_str(json).unwrap()
    }

    fn store(json: &str) -> Store {
        let mut store = Store::default();
        store.apply(
            crate::bus::AppEvent::Snapshot(Box::new(snapshot(json))),
            Instant::now(),
        );
        store
    }

    #[test]
    fn why_never_returns_nothing() {
        assert!(!why(None).is_empty());
        assert!(!why(Some(&snapshot("{}"))).is_empty());
    }

    #[test]
    fn why_explains_an_undrivable_dispatch() {
        let snap = snapshot(
            r#"{"atlas": {"mode": "research", "current_task_id": "t-9"},
                "atlas_heartbeat": {"autonomous": true,
                  "coordinator": {"driving": false,
                                  "reason": "the `claude` CLI is not on PATH"}}}"#,
        );
        let why = why(Some(&snap));
        assert!(why.iter().any(|l| l.contains("not on PATH")));
        assert!(why.iter().any(|l| l.contains("t-9 is in flight")));
    }

    #[test]
    fn why_names_the_workflow_a_coordinator_is_actually_walking() {
        // Registering a workflow is not running it, so `driving` is the fact
        // worth stating — a workflow row on screen does not answer it.
        let snap = snapshot(
            r#"{"atlas": {"mode": "propose"},
                "atlas_heartbeat": {"autonomous": true,
                  "coordinator": {"driving": true, "workflow_id": "wf-42"}}}"#,
        );
        assert!(why(Some(&snap))
            .iter()
            .any(|l| l.contains("driving workflow wf-42")));
    }

    #[test]
    fn why_separates_a_mode_that_forbids_work_from_autonomy_that_is_off() {
        let observe = snapshot(r#"{"atlas": {"mode": "observe"}}"#);
        assert!(why(Some(&observe))[0].contains("OBSERVE"));
        let paused = snapshot(r#"{"atlas": {"mode": "paused"}}"#);
        assert!(why(Some(&paused))[0].contains("PAUSED"));
        let waiting = snapshot(
            r#"{"atlas": {"mode": "research"}, "atlas_heartbeat": {"autonomous": false}}"#,
        );
        assert!(why(Some(&waiting))[0].contains("autonomy is OFF"));
    }

    #[test]
    fn an_unset_string_from_the_owner_is_absent_not_a_value() {
        // The owner serialises what it never set as `""`. A run that read it as
        // a value would announce "driving workflow " with nothing after it.
        let snap = snapshot(
            r#"{"atlas": {"mode": "", "current_task_id": ""},
                "atlas_heartbeat": {"coordinator": {"driving": false, "reason": ""}}}"#,
        );
        let why = why(Some(&snap));
        assert!(why[0].contains("has not reported a mode"));
        assert!(why.iter().any(|l| l.contains("No trigger has fired")));
        assert!(!why.iter().any(|l| l.contains("Cannot drive")));
    }

    // -- the reveal --------------------------------------------------------

    /// A read whose body is long enough for halves to be distinguishable.
    fn read_store() -> Store {
        store(
            r#"{"atlas_read": {"as_of": "2026-07-30", "conviction": 0.42,
                 "agreement": "divergent", "quantitative_state": "stress",
                 "tensions": ["Absorption is high while the tape is quiet."],
                 "would_change_my_mind": ["Absorption falling back under its threshold."],
                 "read_hash": "c60c46006d926733",
                 "grounding": {"window_hash": "45fbb3f32be5bef9"}}}"#,
        )
    }

    #[test]
    fn at_half_the_reveal_exactly_half_the_read_is_on_screen() {
        // The brief's property, stated as arithmetic rather than sampled from a
        // frame: the reveal is a substring render, so "half revealed" has an
        // exact meaning and a test can hold it to that.
        let lines = read_lines(&read_store(), 30);
        let total = char_count(&lines);
        assert!(
            total > 100,
            "the fixture read is too short to halve: {total}"
        );

        let half = revealed(lines.clone(), reveal_chars(total, 0.5));
        assert_eq!(char_count(&half), total / 2);

        let whole = revealed(lines.clone(), reveal_chars(total, 1.0));
        assert_eq!(char_count(&whole), total);
        assert_eq!(whole, lines, "a finished reveal is the read itself");

        assert!(revealed(lines, reveal_chars(total, 0.0)).is_empty());
    }

    #[test]
    fn a_reveal_cuts_characters_and_keeps_the_style_of_what_survives() {
        let styled = vec![
            Line::from(vec![
                Span::styled("▌ ", Style::default().fg(theme().accent)),
                Span::styled("absorption", Style::default().fg(theme().text_primary)),
            ]),
            Line::from("never seen"),
        ];
        let cut = revealed(styled, 5);
        assert_eq!(cut.len(), 1, "the second line is past the budget");
        assert_eq!(cut[0].spans.len(), 2);
        assert_eq!(cut[0].spans[1].content, "abs");
        assert_eq!(cut[0].spans[1].style.fg, Some(theme().text_primary));
    }

    #[test]
    fn a_reveal_never_splits_a_character_in_half() {
        // Byte slicing panics here rather than rendering: the read carries em
        // dashes and curly quotes straight out of a headline.
        let line = vec![Line::from("— ’ ▌ absorption")];
        for budget in 0..8 {
            let cut = revealed(line.clone(), budget);
            assert_eq!(char_count(&cut), budget.min(16));
        }
    }

    #[test]
    fn reveal_chars_truncates_rather_than_rounding_up() {
        assert_eq!(reveal_chars(101, 0.5), 50);
        assert_eq!(reveal_chars(100, 0.999), 99);
        assert_eq!(reveal_chars(100, 1.0), 100);
        // Clamped at both ends: a fraction outside [0, 1] is a bug upstream,
        // and slicing past the end would panic rather than render.
        assert_eq!(reveal_chars(100, 4.0), 100);
        assert_eq!(reveal_chars(100, -1.0), 0);
    }

    // -- layout ------------------------------------------------------------

    #[test]
    fn the_grid_keeps_the_hero_and_drops_from_the_bottom() {
        // At the baseline frame everything fits and the slack goes to the rows
        // that hold lists.
        let (heights, dropped) = grid_rows(34);
        assert_eq!(heights.len(), 4);
        assert_eq!(heights[0], HERO_H, "the hero is a fixed four rows of glyph");
        assert_eq!(heights.iter().sum::<u16>(), 34);
        assert!(dropped.is_empty());

        // Squeezed, rows go from the bottom — the pair an operator can reach on
        // another view is the first thing to leave.
        let (heights, dropped) = grid_rows(16);
        assert_eq!(dropped, vec!["verdict and replay"]);
        assert_eq!(heights.iter().sum::<u16>(), 15, "one row for the note");

        let (heights, dropped) = grid_rows(11);
        assert_eq!(dropped, vec!["allocation", "verdict and replay"]);
        assert_eq!(heights.iter().sum::<u16>(), 10);

        // The floor: the hero and the note, and nothing else claiming to be a
        // tile.
        let (heights, dropped) = grid_rows(HERO_H + 1);
        assert_eq!(heights, vec![HERO_H]);
        assert_eq!(dropped.len(), 3);
    }

    #[test]
    fn a_pane_that_overflows_says_how_much_it_is_hiding() {
        let lines: Vec<Line<'static>> = (0..10).map(|i| Line::from(format!("row {i}"))).collect();
        assert_eq!(fit(lines.clone(), 10).len(), 10);
        let cut = fit(lines.clone(), 4);
        assert_eq!(cut.len(), 4);
        assert_eq!(cut[3].spans[0].content, "▾ 7 more");
        assert!(fit(lines, 0).is_empty());
    }

    #[test]
    fn wrapping_breaks_on_words_and_splits_only_what_can_never_fit() {
        assert_eq!(
            wrap("absorption is high while the tape is quiet", 20),
            vec!["absorption is high", "while the tape is", "quiet"]
        );
        assert_eq!(
            wrap("45fbb3f32be5bef9", 8),
            vec!["45fbb3f3", "2be5bef9"],
            "a hash longer than the pane is split, not dropped"
        );
        assert!(wrap("anything", 0).is_empty());
    }

    // -- the tiles ---------------------------------------------------------

    #[test]
    fn a_replay_beside_an_unavailable_window_is_never_a_number() {
        // The owner sends a `return` alongside `available: false` for windows
        // its snapshot does not cover. Rendering it would be a crisis number
        // for a crisis this book was never replayed through.
        let store = store(
            r#"{"stress": {"replays": {
                 "2008": {"available": false, "return": -0.4, "reason": "span too short"},
                 "2020": {"available": true, "return": -0.173},
                 "2022": {"available": true, "return": null}}}}"#,
        );
        let body: Vec<String> = replay_body(&store, TILE_W)
            .iter()
            .map(|line| line.spans.iter().map(|s| s.content.to_string()).collect())
            .collect();
        assert!(body[0].contains(MISSING), "{body:?}");
        assert!(!body[0].contains("40.0"), "{body:?}");
        assert!(body[1].contains("span too short"), "{body:?}");
        assert!(body[2].contains("-17.3%"), "{body:?}");
        assert!(
            body.last().unwrap().contains(MISSING),
            "an available window with no number is still absent: {body:?}"
        );
    }

    #[test]
    fn the_weight_bar_says_which_side_of_its_target_a_holding_is_on() {
        let t = theme();
        let over: Vec<(String, Color)> = weight_bar(Some(0.5), Some(0.25), 8)
            .iter()
            .map(|s| (s.content.to_string(), s.style.fg.unwrap()))
            .collect();
        assert_eq!(over[0], ("▰▰".into(), t.accent));
        assert_eq!(over[1], ("▰▰".into(), t.negative), "the overshoot is loud");
        assert_eq!(over[2], ("▱▱▱▱".into(), t.text_dim));

        let under = weight_bar(Some(0.25), Some(0.5), 8);
        assert_eq!(under[1].style.fg, Some(t.accent_dim), "the gap is quiet");

        // Absent is not zero and not full: an unheld target draws only its gap.
        let unheld = weight_bar(None, Some(0.5), 8);
        assert_eq!(unheld[0].content, "");
        assert_eq!(unheld[1].content, "▰▰▰▰");

        // The bar never runs past its own width, whatever the owner sends.
        for (current, target) in [(9.0, 0.1), (-3.0, 0.5), (f64::NAN, 0.5)] {
            let bar = weight_bar(Some(current), Some(target), 8);
            let width: usize = bar.iter().map(|s| s.content.chars().count()).sum();
            assert_eq!(width, 8, "{current} against {target} drew {width} cells");
        }
    }

    #[test]
    fn the_verdict_is_the_newest_one_recorded_not_the_newest_decision() {
        let store = store(
            r#"{"decisions": [
                 {"decision_id": "new", "kind": "rebalance_gate", "verdict": null},
                 {"decision_id": "old", "kind": "estimation_window", "as_of": "2026-07-30",
                  "verdict": {"verdict": "PASS", "source": "referee-agent",
                              "reasons": ["constraints hold"]}}]}"#,
        );
        let body: Vec<String> = verdict_body(&store, TILE_W)
            .iter()
            .map(|line| line.spans.iter().map(|s| s.content.to_string()).collect())
            .collect();
        assert!(body[0].contains("PASS"), "{body:?}");
        assert!(body[0].contains("referee-agent"), "{body:?}");
        assert!(body[1].contains("estimation_window"), "{body:?}");
    }
}
