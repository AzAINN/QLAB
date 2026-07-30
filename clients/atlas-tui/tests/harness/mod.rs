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
use atlas::model::Snapshot;
use atlas::store::Store;

/// The captured owner payload, folded in the way the runtime folds it.
///
/// Through `Store::apply` rather than by assigning the field: a fixture that
/// bypassed the fold would render a state the running client can never be in.
pub fn fixture_store() -> Store {
    let snapshot: Snapshot =
        serde_json::from_str(include_str!("../fixtures/tui_snapshot.json")).unwrap();
    let mut store = Store::default();
    store.apply(AppEvent::ConnUp(Channel::Owner));
    store.apply(AppEvent::Snapshot(Box::new(snapshot)));
    store
}

/// One shell frame, rendered at `w`×`h` and read back as text.
///
/// The backend's `Display` quotes each row, so trailing space is visible in a
/// golden file instead of being silently trimmed by an editor.
pub fn frame_to_string(store: &Store, w: u16, h: u16) -> String {
    let backend = ratatui::backend::TestBackend::new(w, h);
    let mut term = ratatui::Terminal::new(backend).unwrap();
    term.draw(|f| atlas::ui::shell::draw(f, store)).unwrap();
    format!("{}", term.backend())
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
