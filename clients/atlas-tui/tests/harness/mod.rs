//! The golden-frame harness every view task reuses.
//!
//! A view is only trustworthy if the frame it produces can be read back as
//! text, so the pins are on what an operator sees rather than on the structs
//! behind it. `TestBackend` needs no terminal, which is why these run offline
//! and in parallel with everything else.
//!
//! Not a `tests/*.rs` target: files under a subdirectory are compiled into the
//! test crate that declares `mod harness;`, so the helpers are shared without
//! becoming a test binary of their own.
#![allow(dead_code)]

use atlas::bus::{AppEvent, Channel};
use atlas::fx::FlashTracker;
use atlas::model::Snapshot;
use atlas::store::Store;
use atlas::ui::views::Views;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::buffer::Buffer;
use ratatui::style::Style;
use std::time::Instant;

/// The three things `main` holds — the desk, the views, and the effects — so a
/// test can press keys and then draw the frame those keys produced.
///
/// Keystrokes go through the real `shell::on_key`, and frames through the real
/// `shell::draw`, because the bug this harness exists to catch lived in the
/// routing between them and not in either end.
pub struct Client {
    pub store: Store,
    pub views: Views,
    pub fx: FlashTracker,
    /// The instant every frame is drawn at. Fixed at the snapshot's arrival so
    /// a golden is never a function of how long the test took.
    pub now: Instant,
}

impl Client {
    pub fn fixture() -> Self {
        Self::new(fixture_store())
    }

    pub fn new(store: Store) -> Self {
        let now = store.last_snapshot_at.unwrap_or_else(Instant::now);
        Self {
            store,
            views: Views::new(),
            fx: FlashTracker::default(),
            now,
        }
    }

    /// One keystroke, routed exactly as the runtime routes it.
    pub fn press(&mut self, code: KeyCode) -> &mut Self {
        atlas::ui::shell::on_key(
            KeyEvent::new(code, KeyModifiers::NONE),
            &mut self.store,
            &mut self.views,
        );
        self
    }

    pub fn keys(&mut self, codes: &[KeyCode]) -> &mut Self {
        for code in codes {
            self.press(*code);
        }
        self
    }

    pub fn frame(&self, w: u16, h: u16) -> String {
        format!("{}", self.terminal(w, h).backend())
    }

    pub fn buffer(&self, w: u16, h: u16) -> Buffer {
        self.terminal(w, h).backend().buffer().clone()
    }

    fn terminal(&self, w: u16, h: u16) -> ratatui::Terminal<ratatui::backend::TestBackend> {
        let mut term =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
        term.draw(|f| {
            atlas::ui::shell::draw(f, &self.store, &self.views, &self.fx, self.now)
        })
        .unwrap();
        term
    }
}

/// The frame minus the ticker tape and the status line — the rows a view owns.
///
/// The tape repeats every symbol, price and arrow in the universe, so
/// `frame.contains("XLF")` passes on the tape alone and proves nothing about the
/// grid under it. Every assertion about a view's content goes through here.
pub fn body(frame: &str) -> String {
    let lines: Vec<&str> = frame.lines().collect();
    lines[1..lines.len().saturating_sub(1)].join("\n")
}

/// The style of the first cell of the first run of `needle` *in the body*.
///
/// A golden string cannot say what colour a column is, and "the amber one" is
/// exactly the kind of claim that quietly stops being true. Skips the tape for
/// the same reason `body` does: every price in the grid is also on the tape,
/// where it is deliberately styled differently.
pub fn body_style_of(buf: &Buffer, needle: &str) -> Style {
    for y in 1..buf.area.height.saturating_sub(1) {
        let row: Vec<String> = (0..buf.area.width)
            .map(|x| buf[(x, y)].symbol().to_string())
            .collect();
        let text = row.concat();
        if let Some(byte) = text.find(needle) {
            // Byte offset back to cell index: a cell may hold a multi-byte
            // symbol, so counting cells is the only way that survives `▼`.
            let mut at = 0;
            for (i, cell) in row.iter().enumerate() {
                if at == byte {
                    return buf[(i as u16, y)].style();
                }
                at += cell.len();
            }
        }
    }
    panic!("no rendered cell run matches {needle:?}:\n{buf:?}");
}

/// The captured owner payload, folded in the way the runtime folds it.
///
/// Through `Store::apply` rather than by assigning the field: a fixture that
/// bypassed the fold would render a state the running client can never be in.
pub fn fixture_store() -> Store {
    let snapshot: Snapshot =
        serde_json::from_str(include_str!("../fixtures/tui_snapshot.json")).unwrap();
    let mut store = Store::default();
    let now = Instant::now();
    store.apply(AppEvent::ConnUp(Channel::Owner), now);
    store.apply(AppEvent::Snapshot(Box::new(snapshot)), now);
    store
}

/// One shell frame, rendered at `w`×`h` and read back as text.
///
/// Drawn at the instant the snapshot arrived, so a frame is never a function of
/// how long the test took — a golden that started failing once a run crossed
/// the staleness threshold would be a very expensive lesson. Tests that care
/// about age call `frame_to_string_at`.
pub fn frame_to_string(store: &Store, w: u16, h: u16) -> String {
    let now = store.last_snapshot_at.unwrap_or_else(Instant::now);
    frame_to_string_at(store, w, h, now)
}

/// One shell frame, drawn at an instant the caller chooses, with nothing
/// flashing — the state every frame is in a second after the desk went quiet.
pub fn frame_to_string_at(store: &Store, w: u16, h: u16, now: Instant) -> String {
    frame_to_string_fx(store, &FlashTracker::default(), w, h, now)
}

/// One shell frame with effect state, for the tests that pin motion.
///
/// The backend's `Display` quotes each row, so trailing space is visible in a
/// golden file instead of being silently trimmed by an editor.
pub fn frame_to_string_fx(
    store: &Store,
    fx: &FlashTracker,
    w: u16,
    h: u16,
    now: Instant,
) -> String {
    let views = Views::new();
    let backend = ratatui::backend::TestBackend::new(w, h);
    let mut term = ratatui::Terminal::new(backend).unwrap();
    term.draw(|f| atlas::ui::shell::draw(f, store, &views, fx, now))
        .unwrap();
    format!("{}", term.backend())
}

/// The styled cells of one rendered row — what a golden string cannot say.
pub fn row_styles(
    store: &Store,
    fx: &FlashTracker,
    w: u16,
    h: u16,
    now: Instant,
    y: u16,
) -> Vec<(String, ratatui::style::Style)> {
    let views = Views::new();
    let backend = ratatui::backend::TestBackend::new(w, h);
    let mut term = ratatui::Terminal::new(backend).unwrap();
    term.draw(|f| atlas::ui::shell::draw(f, store, &views, fx, now))
        .unwrap();
    let buf = term.backend().buffer().clone();
    (0..w)
        .map(|x| {
            let cell = &buf[(x, y)];
            (cell.symbol().to_string(), cell.style())
        })
        .collect()
}

/// The one rendered line containing `needle`, panicking with the frame when
/// there is none — a pin that says which line it read is worth far more than
/// `assert!(frame.contains(..))` when it fails a year from now.
pub fn line_with<'a>(frame: &'a str, needle: &str) -> &'a str {
    frame
        .lines()
        .find(|line| line.contains(needle))
        .unwrap_or_else(|| panic!("no rendered line contains {needle:?}:\n{frame}"))
}
