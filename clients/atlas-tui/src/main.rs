//! atlas-tui — a Ratatui client for the qlab owner runtime.
//!
//! Read-only by construction. It talks to the owner over HTTP and has no order
//! path, no registry handle, and no way to acquire either — invariant 3 is
//! preserved by absence rather than by a check that could be removed. Paper
//! execution stays in the Textual client, where the confirm dialog lives.
//!
//! Runs alongside the Textual TUI rather than replacing it. Both read the same
//! `/api/tui` snapshot, so there is no cutover cliff and no window where the
//! desk has two disagreeing faces.

mod app;
mod bus;
mod client;
mod cmd;
mod format;
mod fx;
mod glyph;
mod input;
mod model;
mod net;
mod store;
mod theme;
mod ui;

use anyhow::Result;
use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};
use std::{
    io,
    time::{Duration, Instant},
};

/// Frame budget. 100ms is the honest ceiling for a cell-grid client: fast
/// enough that the glyph reads as motion, slow enough that a remote owner is
/// not polled into the ground.
const FRAME: Duration = Duration::from_millis(100);
/// Snapshots are far more expensive than frames, so they run on their own beat.
const REFRESH: Duration = Duration::from_secs(3);

fn main() -> Result<()> {
    let offline = !std::env::args().any(|a| a == "--live");
    let mut app = app::App::new(client::OwnerClient::from_env(), offline);
    if app.readiness.is_ready() {
        app.refresh();
    }

    enable_raw_mode()?;
    let mut out = io::stdout();
    execute!(out, EnterAlternateScreen)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(out))?;

    let result = run(&mut terminal, &mut app);

    // Restore the terminal even if the loop failed. A client that panics out of
    // raw mode leaves the operator with an unusable shell.
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    result
}

fn run<B: ratatui::backend::Backend>(terminal: &mut Terminal<B>, app: &mut app::App) -> Result<()>
where
    // ratatui 0.30 made the backend error an associated type; `anyhow` can only
    // absorb it once it is a real, thread-safe error.
    B::Error: std::error::Error + Send + Sync + 'static,
{
    let mut last_refresh = Instant::now();
    loop {
        terminal.draw(|f| ui::draw(f, app))?;

        if event::poll(FRAME)? {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press {
                    match key.code {
                        KeyCode::Char('q') | KeyCode::Esc => app.should_quit = true,
                        KeyCode::Char('r') => {
                            app.readiness = app.client.readiness();
                            app.refresh();
                            last_refresh = Instant::now();
                        }
                        _ => {}
                    }
                }
            }
        }
        if app.should_quit {
            return Ok(());
        }
        app.tick = app.tick.wrapping_add(1);
        if last_refresh.elapsed() >= REFRESH {
            app.refresh();
            last_refresh = Instant::now();
        }
    }
}
