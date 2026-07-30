//! Atlas-first layout.
//!
//! The Textual client puts Atlas in a right-hand rail beside eight peer views.
//! This one inverts that: Atlas and its reasoning own the frame, and the desk
//! numbers support it. That is the whole point of the rewrite — the app is
//! meant to be fronted by a personal quant, not to be a view switcher that
//! happens to include one.

use crate::app::App;
use crate::glyph;
use ratatui::{
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Wrap},
    Frame,
};

// Kept in step with qlab/tui/theme.py. Two clients drifting apart on colour
// would read as two different products.
const AMBER: Color = Color::Rgb(0xE8, 0xB3, 0x39);
const TEXT_HI: Color = Color::Rgb(0xE6, 0xE9, 0xEF);
const DIM: Color = Color::Rgb(0x7A, 0x83, 0x94);
const UP: Color = Color::Rgb(0x4E, 0xC9, 0x8A);
const DOWN: Color = Color::Rgb(0xE0, 0x5A, 0x5A);
const BORDER: Color = Color::Rgb(0x2A, 0x31, 0x3D);

pub fn draw(f: &mut Frame, app: &App) {
    let area = f.area();
    if !app.readiness.is_ready() {
        draw_unreachable(f, area, app);
        return;
    }

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(9),   // Atlas: glyph + verdict
            Constraint::Min(8),      // why / workflows
            Constraint::Length(1),   // status bar
        ])
        .split(area);

    draw_atlas(f, rows[0], app);
    let body = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(58), Constraint::Percentage(42)])
        .split(rows[1]);
    draw_why(f, body[0], app);
    draw_desk(f, body[1], app);
    draw_status(f, rows[2], app);
}

fn draw_unreachable(f: &mut Frame, area: Rect, app: &App) {
    // A blank desk would imply "nothing is happening". The difference between
    // that and "this client cannot see the desk" is the whole message.
    let lines = vec![
        Line::from(Span::styled(
            "NO OWNER RUNTIME",
            Style::default().fg(DOWN).add_modifier(Modifier::BOLD),
        )),
        Line::from(""),
        Line::from(Span::styled(app.readiness.reason(), Style::default().fg(TEXT_HI))),
        Line::from(""),
        Line::from(Span::styled(
            "This client never opens the registry itself — the owner is the only",
            Style::default().fg(DIM),
        )),
        Line::from(Span::styled(
            "writer. Start one with `qlab tui` or `qlab ui`, then press r.",
            Style::default().fg(DIM),
        )),
    ];
    f.render_widget(
        Paragraph::new(lines)
            .block(bordered(" atlas "))
            .alignment(Alignment::Left)
            .wrap(Wrap { trim: true }),
        area,
    );
}

fn draw_atlas(f: &mut Frame, area: Rect, app: &App) {
    let mood = app.desk.mood();
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(16), Constraint::Min(20)])
        .split(area);

    let tone = match mood {
        glyph::Mood::Working => UP,
        glyph::Mood::Alarmed => DOWN,
        glyph::Mood::Dormant => DIM,
        glyph::Mood::Idle => AMBER,
    };
    // The glyph advances on its mood's own tempo, so a busy desk visibly moves
    // faster than a quiet one without anything else changing.
    let phase = app.tick * mood.tempo() / 10;
    let art: Vec<Line> = glyph::frame(mood, phase)
        .into_iter()
        .map(|row| Line::from(Span::styled(row, Style::default().fg(tone))))
        .collect();
    f.render_widget(Paragraph::new(art).block(bordered(" atlas ")), cols[0]);

    let d = &app.desk;
    let body = vec![
        Line::from(vec![
            Span::styled(mood.label(), Style::default().fg(tone).add_modifier(Modifier::BOLD)),
            Span::styled("   mode ", Style::default().fg(DIM)),
            Span::styled(
                format!("{}/{}", d.mode.to_uppercase(), d.state.to_uppercase()),
                Style::default().fg(TEXT_HI),
            ),
            Span::styled("   autonomy ", Style::default().fg(DIM)),
            Span::styled(
                if d.autonomous { "ON" } else { "OFF" },
                Style::default().fg(if d.autonomous { UP } else { DIM }),
            ),
            Span::styled("   tier ", Style::default().fg(DIM)),
            Span::styled(
                if d.fast { "FAST" } else { "FULL" },
                Style::default().fg(if d.fast { AMBER } else { TEXT_HI }),
            ),
        ]),
        Line::from(""),
        Line::from(Span::styled(
            if d.driving {
                format!("driving workflow {}", d.driving_workflow)
            } else {
                "no coordinator running".to_string()
            },
            Style::default().fg(if d.driving { UP } else { DIM }),
        )),
        Line::from(Span::styled(
            format!("regime {} · desk {} · news {}",
                    d.regime.to_uppercase(),
                    d.desk_label,
                    if d.news_source.is_empty() { "—" } else { &d.news_source }),
            Style::default().fg(DIM),
        )),
    ];
    f.render_widget(
        Paragraph::new(body).block(bordered(" desk manager ")).wrap(Wrap { trim: true }),
        cols[1],
    );
}

fn draw_why(f: &mut Frame, area: Rect, app: &App) {
    let lines: Vec<Line> = app
        .desk
        .why()
        .into_iter()
        .flat_map(|w| {
            vec![
                Line::from(vec![
                    Span::styled("• ", Style::default().fg(AMBER)),
                    Span::styled(w, Style::default().fg(TEXT_HI)),
                ]),
                Line::from(""),
            ]
        })
        .collect();
    f.render_widget(
        Paragraph::new(lines)
            .block(bordered(" why the desk is doing what it is doing "))
            .wrap(Wrap { trim: true }),
        area,
    );
}

fn draw_desk(f: &mut Frame, area: Rect, app: &App) {
    let d = &app.desk;
    let mut lines = vec![Line::from(vec![
        Span::styled("equity   ", Style::default().fg(DIM)),
        Span::styled(
            d.equity.map(money).unwrap_or_else(|| "—".into()),
            Style::default().fg(TEXT_HI).add_modifier(Modifier::BOLD),
        ),
    ])];
    lines.push(Line::from(vec![
        Span::styled("drawdown ", Style::default().fg(DIM)),
        Span::styled(
            d.drawdown.map(|v| format!("{:.2}%", v * 100.0)).unwrap_or_else(|| "—".into()),
            Style::default().fg(match d.drawdown {
                Some(v) if v > 0.10 => DOWN,
                Some(v) if v > 0.05 => AMBER,
                _ => TEXT_HI,
            }),
        ),
    ]));
    if d.halted {
        lines.push(Line::from(Span::styled(
            "HALTED — the mandate kill switch is tripped",
            Style::default().fg(DOWN).add_modifier(Modifier::BOLD),
        )));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("recent runs", Style::default().fg(AMBER))));
    let runs = app.workflows();
    if runs.is_empty() {
        lines.push(Line::from(Span::styled("none yet", Style::default().fg(DIM))));
    }
    for (id, status, goal) in runs {
        lines.push(Line::from(vec![
            Span::styled(
                format!("{status:<10}"),
                Style::default().fg(match status.as_str() {
                    "done" => UP,
                    "failed" | "blocked" => DOWN,
                    "working" => AMBER,
                    _ => DIM,
                }),
            ),
            Span::styled(format!("{id}  "), Style::default().fg(DIM)),
            Span::styled(goal.chars().take(30).collect::<String>(),
                         Style::default().fg(TEXT_HI)),
        ]));
    }
    f.render_widget(
        Paragraph::new(lines).block(bordered(" book ")),
        area,
    );
}

fn draw_status(f: &mut Frame, area: Rect, app: &App) {
    let hint = if app.last_error.is_empty() {
        format!("q quit · r refresh · {} · read-only — no order path exists here",
                app.client.base())
    } else {
        format!("! {}", app.last_error)
    };
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            hint,
            Style::default().fg(if app.last_error.is_empty() { DIM } else { DOWN }),
        ))),
        area,
    );
}

/// Thousands-grouped currency. Rust has no `{:,}`, and an ungrouped equity
/// figure is genuinely harder to read at a glance on a book this size.
pub fn money(value: f64) -> String {
    let negative = value < 0.0;
    let cents = format!("{:.2}", value.abs());
    let (whole, frac) = cents.split_once('.').unwrap_or((cents.as_str(), "00"));
    let mut grouped = String::new();
    for (i, ch) in whole.chars().enumerate() {
        if i > 0 && (whole.len() - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(ch);
    }
    format!("{}${grouped}.{frac}", if negative { "-" } else { "" })
}

#[cfg(test)]
mod tests {
    use super::money;

    #[test]
    fn money_groups_thousands() {
        assert_eq!(money(10_000.0), "$10,000.00");
        assert_eq!(money(1_234_567.891), "$1,234,567.89");
    }

    #[test]
    fn money_handles_small_and_negative_values() {
        // A negative mark is possible on a short book; the sign must lead the
        // currency symbol rather than land inside the number.
        assert_eq!(money(0.0), "$0.00");
        assert_eq!(money(999.5), "$999.50");
        assert_eq!(money(-1_500.25), "-$1,500.25");
    }
}

fn bordered(title: &str) -> Block<'_> {
    Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(BORDER))
        .title(Span::styled(title.to_string(), Style::default().fg(AMBER)))
}

#[cfg(test)]
mod render_tests {
    use crate::app::App;
    use crate::client::OwnerClient;
    use ratatui::{backend::TestBackend, Terminal};
    use serde_json::json;

    fn render(app: &App, w: u16, h: u16) -> String {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| super::draw(f, app)).unwrap();
        let buf = term.backend().buffer().clone();
        (0..buf.area.height)
            .map(|y| {
                (0..buf.area.width)
                    .map(|x| buf[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn app_with(snap: serde_json::Value) -> App {
        let mut app = App::new(OwnerClient::new("http://127.0.0.1:1"), true);
        app.readiness = crate::client::Readiness::Ready;
        app.desk = crate::app::Desk::from_snapshot(&snap);
        app.snapshot = Some(snap);
        app
    }

    #[test]
    fn an_unreachable_owner_says_so_instead_of_rendering_an_empty_desk() {
        // The failure mode this prevents: a blank frame that reads as "nothing
        // is happening on your desk" when the truth is "I cannot see it".
        let app = App::new(OwnerClient::new("http://127.0.0.1:1"), true);
        let out = render(&app, 90, 20);
        assert!(out.contains("NO OWNER RUNTIME"));
        assert!(out.contains("qlab tui"), "must name the remedy");
    }

    #[test]
    fn a_driving_desk_shows_the_workflow_and_the_working_glyph() {
        let app = app_with(json!({
            "atlas": {"mode": "research", "state": "coordinating"},
            "atlas_heartbeat": {
                "autonomous": true, "fast": false,
                "coordinator": {"driving": true, "workflow_id": "wf-42"}
            },
            "portfolio": {"equity": 10450.0, "drawdown": 0.021, "halted": false},
            "desk_mode": {"label": "SYNTHETIC"}
        }));
        let out = render(&app, 100, 24);
        assert!(out.contains("WORKING"));
        assert!(out.contains("wf-42"));
        assert!(out.contains("RESEARCH/COORDINATING"));
        assert!(out.contains("$10,450.00"));
    }

    #[test]
    fn a_halted_desk_is_unmistakable() {
        let app = app_with(json!({
            "atlas": {"mode": "propose", "state": "blocked"},
            "portfolio": {"equity": 6000.0, "drawdown": 0.40, "halted": true}
        }));
        let out = render(&app, 100, 24);
        assert!(out.contains("HALTED"));
        assert!(out.contains("kill switch"));
    }

    #[test]
    fn the_read_only_boundary_is_stated_on_screen() {
        // Not decoration. An operator must never wonder whether this surface
        // can place an order — it cannot, and it says so.
        let app = app_with(json!({"atlas": {"mode": "research"}}));
        assert!(render(&app, 110, 20).contains("no order path"));
    }

    #[test]
    fn a_narrow_terminal_still_renders_without_panicking() {
        // Ratatui panics on some zero-area splits; a client that dies when the
        // window is dragged narrow is worse than one that truncates.
        let app = app_with(json!({"atlas": {"mode": "research"}}));
        for (w, h) in [(40u16, 12u16), (20, 8), (80, 10), (200, 60)] {
            let _ = render(&app, w, h);
        }
    }
}

