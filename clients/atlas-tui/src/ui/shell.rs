//! Frame layout: the chrome around the views — how one frame is carved into regions.
//!
//! Five regions, and only one of them belongs to a view: a ticker row, the nav
//! rail, the content area, the pulse rail, and the status line. The rails are
//! always on screen because the two questions an operator asks between glances
//! — *what is the market doing* and *what is Atlas doing* — must never be a
//! view switch away.
//!
//! `draw` is a pure function of the `Store` and the instant it is told it is
//! drawing at. Nothing here reads a clock, opens a socket, or holds a client,
//! which is what lets one golden frame pin the whole layout — including how a
//! frame looks forty seconds after the last snapshot — and what keeps the
//! read-only posture a property of the composition root rather than a rule
//! each view has to remember.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::glyph;
use crate::model::Snapshot;
use crate::store::{Store, ViewId};
use crate::theme::theme;
use crate::theme::Theme;
use crate::ui::views;
use crate::ui::widgets::{panel_block, panel_header, ticker};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Wrap},
    Frame,
};
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

/// Exactly wide enough for `▌6 AUDIT` — the active marker, the digit, a space,
/// and the longest label. Widening it would take cells from the content.
const NAV_W: u16 = 8;
/// The pulse rail. Wide enough for a label column and a value column at the
/// widths `format` produces; narrower and the money figures start truncating.
const PULSE_W: u16 = 34;
/// The label column inside the rails.
const LABEL_W: usize = 11;

/// One frame.
///
/// `fx` rides alongside the store rather than inside it: the store is what the
/// owner said plus the diff of it, and a decaying animation stamp is neither.
/// Passing it here is what keeps the frame a pure function of (state, effects,
/// instant) — the property every golden test depends on.
pub fn draw(f: &mut Frame, store: &Store, fx: &FlashTracker, now: Instant) {
    let t = theme();
    let area = f.area();
    fill(f, area, t.bg_base);
    let stale = stale_for(store, now);

    let rows = Layout::vertical([
        Constraint::Length(1), // ticker
        Constraint::Min(0),    // rails + content
        Constraint::Length(1), // command line / status
    ])
    .split(area);
    let cols = Layout::horizontal([
        Constraint::Length(NAV_W),
        Constraint::Min(0),
        Constraint::Length(PULSE_W),
    ])
    .split(rows[1]);

    // The tick count is the offset: one display cell per 120 ms beat, so the
    // tape's position is state rather than a clock read inside a renderer.
    ticker::draw(
        f,
        rows[0],
        &store.asset_views(),
        store.tick as usize,
        stale.is_some(),
        fx,
        now,
    );
    draw_nav(f, cols[0], store, t);

    // The rules belong to the shell, not to the panes: a view that drew its own
    // left border would have to know it was not the leftmost thing on screen.
    let content = left_rule(f, cols[1], t);
    if store.snapshot.is_none() {
        draw_no_data(f, content, store, t);
    } else {
        views::for_id(store.nav.view).draw(f, content, store);
    }

    let pulse = left_rule(f, cols[2], t);
    fill(f, pulse, t.bg_surface);
    draw_pulse(f, pulse, store, t);

    draw_status(f, rows[2], store, t, stale);
}

/// How long the numbers on screen have been unrefreshed, once that is long
/// enough to matter. `None` while they are current, or while there are none.
///
/// The threshold is carried by the store rather than stated here: it is a fact
/// about the poll cadence, and a renderer that owned its own copy of it would
/// keep that copy after the cadence moved.
fn stale_for(store: &Store, now: Instant) -> Option<Duration> {
    let age = now.saturating_duration_since(store.last_snapshot_at?);
    (age > store.stale_after).then_some(age)
}

/// Global keys, then the active view's.
///
/// The shell claims first so a view can never take a binding the whole
/// workstation depends on. Returns what the runtime should do; nav is store
/// state, so a view switch is applied here rather than travelling as a command.
pub fn on_key(key: KeyEvent, store: &mut Store) -> Option<Command> {
    match key.code {
        KeyCode::Char('q') | KeyCode::Esc => return Some(Command::Quit),
        // Raw mode disables ISIG, so Ctrl-C arrives as a keystroke or not at
        // all — and the reflex every operator has has to work.
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            return Some(Command::Quit)
        }
        KeyCode::Char('r') => return Some(Command::Refresh),
        KeyCode::Char(c) if c.is_ascii_digit() => {
            if let Some(view) = ViewId::from_digit(c) {
                store.nav.view = view;
                return None;
            }
        }
        KeyCode::Tab => {
            store.nav.view = store.nav.view.next();
            return None;
        }
        KeyCode::BackTab => {
            store.nav.view = store.nav.view.prev();
            return None;
        }
        // `z` (zen: rails collapse) and `f` (fullscreen the focused pane) are
        // claimed here so no view can bind them and then lose the binding when
        // the layout modes land. Part I of the plan specifies them; no task
        // owns them yet, so they are swallowed rather than routed to a command
        // with nothing to dispatch it.
        KeyCode::Char('z') | KeyCode::Char('f') => return None,
        _ => {}
    }
    views::for_id(store.nav.view).on_key(key, store)
}

/// The depth ramp: a region reads as a surface stacked over the frame only if
/// something paints it. Widgets draw text, not backgrounds, so the shell does.
fn fill(f: &mut Frame, area: Rect, bg: Color) {
    f.render_widget(Block::default().style(Style::default().bg(bg)), area);
}

/// Draw the dim rule in the first column and return what is left.
fn left_rule(f: &mut Frame, area: Rect, t: &Theme) -> Rect {
    let rule = Block::default()
        .borders(Borders::LEFT)
        .border_style(Style::default().fg(t.border_dim));
    let inner = rule.inner(area);
    f.render_widget(rule, area);
    inner
}

fn draw_nav(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    fill(f, area, t.bg_surface);
    let lines: Vec<Line> = ViewId::ALL
        .iter()
        .enumerate()
        .map(|(i, id)| {
            let active = *id == store.nav.view;
            // The marker is a glyph, not only a colour: on a 256-colour
            // terminal the highlight is a shade, and a shade is not an answer
            // to "which view am I looking at".
            let marker = if active { "▌" } else { " " };
            let style = if active {
                Style::default()
                    .fg(t.accent)
                    .bg(t.bg_hover)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(t.text_secondary)
            };
            Line::from(vec![
                Span::styled(
                    marker,
                    Style::default().fg(t.accent).bg(if active {
                        t.bg_hover
                    } else {
                        t.bg_surface
                    }),
                ),
                Span::styled(format!("{} {:<5}", i + 1, id.label()), style),
            ])
        })
        .collect();
    f.render_widget(Paragraph::new(lines), area);
}

/// What this client can see, when it cannot see a desk.
///
/// A frame of `--` reads as "nothing is happening on your desk" when the truth
/// is "I cannot see it". Ported from `ui.rs::draw_unreachable`, split in two
/// because "not there" and "not yet" have different remedies.
fn draw_no_data(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    // A broken payload outranks both: the owner answered, so "no owner" is
    // wrong and "waiting" is worse — it says the desk is coming when it is not.
    let lines = if let Some(bad) = &store.malformed {
        vec![
            Line::from(Span::styled(
                "OWNER PAYLOAD MALFORMED",
                Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
            )),
            Line::from(""),
            Line::from(Span::styled(
                bad.error.clone(),
                Style::default().fg(t.text_primary),
            )),
            Line::from(""),
            Line::from(Span::styled(
                format!("from {}", bad.url),
                Style::default().fg(t.text_secondary),
            )),
            // One logical line, wrapped by the Paragraph: hand-split lines
            // re-wrap into orphans at rail widths this panel does not choose.
            Line::from(Span::styled(
                "The owner is answering and this client cannot read what it serves, \
                 so nothing below is current. Check the owner's version.",
                Style::default().fg(t.text_secondary),
            )),
        ]
    } else if store.conn.owner {
        vec![
            Line::from(Span::styled(
                "WAITING FOR THE FIRST SNAPSHOT",
                Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
            )),
            Line::from(""),
            Line::from(Span::styled(
                "The owner answered. Its first /api/tui payload has not arrived yet.",
                Style::default().fg(t.text_secondary),
            )),
        ]
    } else {
        vec![
            Line::from(Span::styled(
                "NO OWNER RUNTIME",
                Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
            )),
            Line::from(""),
            Line::from(Span::styled(
                "This client never opens the registry itself — the owner is the only",
                Style::default().fg(t.text_secondary),
            )),
            Line::from(Span::styled(
                "writer. Start one with `qlab tui` or `qlab ui`, then press r.",
                Style::default().fg(t.text_secondary),
            )),
        ]
    };
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), area);
}

/// The always-on rail: what the market is doing, and what Atlas makes of it.
///
/// Placeholder shape. Task 10 replaces the numbers with the stress gauge,
/// breadth bar, and movers; Task 14's read pane takes the lower half. The
/// facts below are the ones the old single-screen client stated, kept on
/// screen so the rewrite never renders less than what it replaced.
fn draw_pulse(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    let rows = Layout::vertical([
        Constraint::Length(7), // PULSE
        Constraint::Length(6), // ATLAS READ
        Constraint::Min(0),    // the glyph, bottom-anchored
    ])
    .split(area);

    let snapshot = store.snapshot.as_ref();
    let regime = snapshot
        .and_then(|s| s.market.as_ref())
        .and_then(|m| m.regime.as_ref());
    let stress = snapshot.and_then(|s| s.stress.as_ref());

    let pulse = vec![
        panel_header("pulse"),
        kv(
            "regime",
            upper(regime.and_then(|r| format::text(r.regime.as_ref()))),
        ),
        kv(
            "robust",
            upper(regime.and_then(|r| format::text(r.robust_state.as_ref()))),
        ),
        kv("drawdown", opt_pct(drawdown(snapshot))),
        kv(
            "tier",
            upper(stress.and_then(|s| format::text(s.drawdown_tier.as_ref()))),
        ),
        kv("gross", opt_pct(stress.and_then(|s| s.gross_exposure))),
    ];
    panel(f, rows[0], pulse);

    let read = snapshot.and_then(|s| s.atlas_read.as_ref());
    let read_lines = vec![
        panel_header("atlas read"),
        kv(
            "state",
            upper(read.and_then(|r| format::text(r.quantitative_state.as_ref()))),
        ),
        kv(
            "agreement",
            upper(read.and_then(|r| format::text(r.agreement.as_ref()))),
        ),
        kv("conviction", opt_pct(read.and_then(|r| r.conviction))),
        kv(
            "news",
            format::or_missing(read.and_then(|r| r.news_source.as_ref())).to_string(),
        ),
    ];
    panel(f, rows[1], read_lines);

    draw_glyph(f, rows[2], store, t);
}

/// The Atlas glyph, on its mood's own tempo.
///
/// Ported from `ui.rs::draw_atlas`. A busy desk visibly moves faster than a
/// quiet one without anything else on screen changing, and a frozen automaton
/// is the one honest way a hung client can announce itself.
fn draw_glyph(f: &mut Frame, area: Rect, store: &Store, t: &Theme) {
    // Bottom-anchored: the rail reads top-down as market → read → manager, and
    // the manager is the thing an operator glances at last.
    let area = Layout::vertical([Constraint::Min(0), Constraint::Length(glyph::H as u16 + 1)])
        .split(area)[1];
    let mood = store.mood();
    let tone = match mood {
        glyph::Mood::Working => t.positive,
        glyph::Mood::Alarmed => t.negative,
        glyph::Mood::Dormant => t.text_secondary,
        glyph::Mood::Idle => t.accent,
    };
    let mut lines: Vec<Line> = glyph::frame(mood, store.tick * mood.tempo() / 10)
        .into_iter()
        .map(|row| Line::from(Span::styled(format!(" {row}"), Style::default().fg(tone))))
        .collect();
    lines.push(Line::from(Span::styled(
        format!(" {}", mood.label()),
        Style::default().fg(tone).add_modifier(Modifier::BOLD),
    )));
    f.render_widget(Paragraph::new(lines), area);
}

fn draw_status(f: &mut Frame, area: Rect, store: &Store, t: &Theme, stale: Option<Duration>) {
    fill(f, area, t.bg_raised);
    let snapshot = store.snapshot.as_ref();
    let atlas = snapshot.and_then(|s| s.atlas.as_ref());
    let autonomous = snapshot
        .and_then(|s| s.atlas_heartbeat.as_ref())
        .and_then(|b| b.autonomous)
        .unwrap_or(false);
    let posture = format!(
        "{}·{}",
        atlas
            .and_then(|a| format::text(a.mode.as_ref()))
            .unwrap_or(MISSING),
        if autonomous { "auto" } else { "manual" }
    );

    // Task 20 turns this into the slash-scoped input; it is the prompt an
    // operator will type into, so the space is reserved now rather than moving
    // the whole status line later.
    let left = vec![Span::styled(
        " /command …",
        Style::default().fg(t.text_tertiary),
    )];
    let mut right = vec![
        dot(store.conn.stream, t),
        Span::styled("SSE  ", Style::default().fg(t.text_secondary)),
    ];
    // Beside the stream chip, for the same reason STALE sits beside OWNER: a
    // green dot says the subscription is open, which is a different claim from
    // "every event it delivered was readable". Task 16 gives this a toast; the
    // count is what keeps a dropping stream from being invisible until then.
    if store.stream_malformed_count > 0 {
        right.push(Span::styled(
            format!("STREAM ⚠ {}  ", store.stream_malformed_count),
            Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
        ));
    }
    right.extend([
        dot(store.conn.owner, t),
        Span::styled("OWNER  ", Style::default().fg(t.text_secondary)),
    ]);
    // Beside the chip, not instead of it: a reachable owner and current numbers
    // are two claims, and the red dot only ever spoke to the first.
    if let Some(age) = stale {
        right.push(Span::styled(
            format!("STALE {}s  ", age.as_secs()),
            Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
        ));
    }
    if store.malformed.is_some() {
        right.push(Span::styled(
            "MALFORMED  ",
            Style::default().fg(t.negative).add_modifier(Modifier::BOLD),
        ));
    }
    right.extend([
        Span::styled(
            format!("{posture}  "),
            Style::default().fg(t.text_secondary),
        ),
        // The posture, on every frame. An operator must never have to wonder
        // whether this surface can place an order: it holds no writer, and
        // Task 17 is what makes the other word possible.
        Span::styled(
            "GLASS ",
            Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
        ),
    ]);

    let used: usize = left
        .iter()
        .chain(right.iter())
        .map(|s| s.content.width())
        .sum();
    let mut spans = left;
    spans.push(Span::raw(
        " ".repeat((area.width as usize).saturating_sub(used)),
    ));
    spans.append(&mut right);
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn dot(up: bool, t: &Theme) -> Span<'static> {
    Span::styled(
        "● ",
        Style::default().fg(if up { t.positive } else { t.negative }),
    )
}

/// A titled panel: header line, body, and the rule the block reserves.
fn panel(f: &mut Frame, area: Rect, lines: Vec<Line<'static>>) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    f.render_widget(Paragraph::new(lines), inner);
}

/// A label/value row, aligned so a column of them reads as a column.
fn kv(label: &str, value: String) -> Line<'static> {
    let t = theme();
    Line::from(vec![
        Span::styled(
            format!(" {label:<LABEL_W$}"),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(value, Style::default().fg(t.text_primary)),
    ])
}

fn upper(value: Option<&str>) -> String {
    value
        .map(str::to_uppercase)
        .unwrap_or_else(|| MISSING.to_string())
}

fn opt_pct(value: Option<f64>) -> String {
    value
        .map(format::pct1)
        .unwrap_or_else(|| MISSING.to_string())
}

/// The live book decides the drawdown for the same reason it decides the halt:
/// it is the one marked to the tape.
fn drawdown(snapshot: Option<&Snapshot>) -> Option<f64> {
    let snapshot = snapshot?;
    snapshot
        .live_portfolio
        .as_ref()
        .and_then(|p| p.drawdown)
        .or_else(|| snapshot.portfolio.as_ref().and_then(|p| p.drawdown))
}
