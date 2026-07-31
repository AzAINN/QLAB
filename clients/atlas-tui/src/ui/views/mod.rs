//! One file per view, and the registry that keeps each one alive between frames.
//!
//! Views used to be built on demand — a fresh `Box<dyn View>` per keystroke
//! *and* per frame. Nothing noticed while every view was a unit struct, but the
//! moment a view retains anything an operator moved (a selected row, a crosshair
//! index) that state is dropped between the keypress that set it and the frame
//! that would have drawn it. So the instances live in a `Views` registry built
//! once at startup, beside the `Store` in `main`, and the shell routes to them
//! by `ViewId`.
//!
//! A field per view rather than a map of boxes: routing is then a match the
//! compiler checks, there is no lookup that can miss in the key path, and a
//! test can read a view's cursor back without downcasting a trait object.
//!
//! What a view may hold is still narrow: *where the operator is looking*, never
//! *what the desk says*. Desk facts stay in the `Store`, so two surfaces cannot
//! disagree about the book, and a frame stays a pure function of (store,
//! effects, instant) — plus, now, the cursor the operator put somewhere.

pub mod book;
pub mod desk;
pub mod markets;

use crate::cmd::Command;
use crate::fx::FlashTracker;
use crate::store::{Store, ViewId};
use crate::theme::theme;
use crate::ui::widgets::{panel_block, panel_header};
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::Style,
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};
use std::time::Instant;

pub trait View {
    /// Draw into the region the shell hands over.
    ///
    /// `fx` and `now` ride alongside the store for the same reason `shell::draw`
    /// takes them: a decaying animation stamp is not a desk fact, and a renderer
    /// that read a clock could not be pinned by a golden frame. `&self` is
    /// deliberate — a draw that could move the cursor would make what is on
    /// screen a function of how many times it had been painted.
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, fx: &FlashTracker, now: Instant);

    /// A key the shell did not claim. Returning a `Command` asks the runtime to
    /// act; a view never acts itself, which is what keeps `ui/` free of IO.
    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command>;
}

/// The seven views, alive for the life of the process.
pub struct Views {
    desk: desk::DeskView,
    markets: markets::MarketsView,
    book: book::BookView,
    research: Unbuilt,
    workforce: Unbuilt,
    audit: Unbuilt,
    settings: Unbuilt,
}

impl Default for Views {
    fn default() -> Self {
        Self::new()
    }
}

impl Views {
    pub fn new() -> Self {
        Self {
            desk: desk::DeskView,
            markets: markets::MarketsView::default(),
            book: book::BookView::default(),
            research: Unbuilt(ViewId::Research),
            workforce: Unbuilt(ViewId::Workforce),
            audit: Unbuilt(ViewId::Audit),
            settings: Unbuilt(ViewId::Settings),
        }
    }

    pub fn draw(
        &self,
        id: ViewId,
        f: &mut Frame,
        area: Rect,
        store: &Store,
        fx: &FlashTracker,
        now: Instant,
    ) {
        self.at(id).draw(f, area, store, fx, now);
    }

    pub fn on_key(&mut self, id: ViewId, k: KeyEvent, store: &mut Store) -> Option<Command> {
        self.at_mut(id).on_key(k, store)
    }

    fn at(&self, id: ViewId) -> &dyn View {
        match id {
            ViewId::Desk => &self.desk,
            ViewId::Markets => &self.markets,
            ViewId::Book => &self.book,
            ViewId::Research => &self.research,
            ViewId::Workforce => &self.workforce,
            ViewId::Audit => &self.audit,
            ViewId::Settings => &self.settings,
        }
    }

    fn at_mut(&mut self, id: ViewId) -> &mut dyn View {
        match id {
            ViewId::Desk => &mut self.desk,
            ViewId::Markets => &mut self.markets,
            ViewId::Book => &mut self.book,
            ViewId::Research => &mut self.research,
            ViewId::Workforce => &mut self.workforce,
            ViewId::Audit => &mut self.audit,
            ViewId::Settings => &mut self.settings,
        }
    }
}

/// A view the shell can already reach and the plan has not built yet.
///
/// It names the task that fills it rather than rendering an empty pane: during
/// the weeks this branch is half-built, "nothing here yet" and "this broke"
/// have to be distinguishable at a glance.
struct Unbuilt(ViewId);

/// Which plan task builds a view. Here rather than on `ViewId` because the
/// store has no business knowing what the plan calls the work.
fn owner_task(id: ViewId) -> u8 {
    match id {
        ViewId::Desk => 14,
        ViewId::Markets => 9,
        ViewId::Book => 11,
        ViewId::Research => 21,
        ViewId::Workforce => 19,
        ViewId::Audit => 18,
        ViewId::Settings => 21,
    }
}

impl View for Unbuilt {
    fn draw(&self, f: &mut Frame, area: Rect, _store: &Store, _fx: &FlashTracker, _now: Instant) {
        // Header, note, rule — the same three rows a tile occupies. Given the
        // whole area the rule would land at the foot of the frame, a hundred
        // cells away from the title it belongs to.
        let head = Layout::vertical([Constraint::Length(3), Constraint::Min(0)]).split(area)[0];
        let block = panel_block();
        let inner = block.inner(head);
        f.render_widget(block, head);
        let body = vec![
            panel_header(self.0.label()),
            Line::from(Span::styled(
                format!("this view lands in Task {}", owner_task(self.0)),
                Style::default().fg(theme().text_secondary),
            )),
        ];
        f.render_widget(Paragraph::new(body), inner);
    }

    fn on_key(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bus::AppEvent;
    use crossterm::event::{KeyCode, KeyModifiers};

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn store_with_three_assets() -> Store {
        let mut store = Store::default();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_value(serde_json::json!({"market": {"assets": [
                    {"ticker": "ACWI", "price": 152.47, "change_1d": -0.013, "history": [1.0, 2.0]},
                    {"ticker": "SPY", "price": 729.46, "change_1d": -0.015, "history": [1.0, 2.0]},
                    {"ticker": "QQQ", "price": 661.73, "change_1d": -0.02, "history": [1.0, 2.0]}
                ]}}))
                .unwrap(),
            )),
            Instant::now(),
        );
        store
    }

    fn draw_once(views: &Views, store: &Store) {
        let mut term =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        let fx = FlashTracker::default();
        let now = Instant::now();
        term.draw(|f| {
            views.draw(
                store.nav.view,
                f,
                Rect::new(0, 0, 120, 36),
                store,
                &fx,
                now,
            )
        })
        .unwrap();
    }

    #[test]
    fn every_view_id_resolves_to_a_view_that_declines_unclaimed_keys() {
        // Routing is the shell's fallthrough target, so a missing arm would be a
        // panic in the key path rather than a compile error.
        let mut views = Views::new();
        for id in ViewId::ALL {
            let mut store = Store::default();
            assert_eq!(
                views.on_key(id, key(KeyCode::Char('x')), &mut store),
                None,
                "{id:?}"
            );
        }
    }

    #[test]
    fn a_view_keeps_what_the_operator_moved_across_a_switch_away_and_back() {
        // The regression the registry exists for: routing built a fresh view per
        // keystroke *and* per frame, so a selection was dropped between the key
        // that set it and the frame that would have drawn it. Every read here
        // goes through `Views`, because the view itself always worked — routing
        // was the broken half.
        let mut store = store_with_three_assets();
        let mut views = Views::new();

        views.on_key(ViewId::Markets, key(KeyCode::Down), &mut store);
        views.on_key(ViewId::Markets, key(KeyCode::Down), &mut store);
        assert_eq!(views.markets.selected(), 2);

        // Away and back, exactly as the shell does it on `3` then `2`.
        store.nav.view = ViewId::Book;
        views.on_key(ViewId::Book, key(KeyCode::Down), &mut store);
        draw_once(&views, &store);
        store.nav.view = ViewId::Markets;
        assert_eq!(
            views.markets.selected(),
            2,
            "the selection did not survive a view switch"
        );
    }

    #[test]
    fn the_blotters_sort_and_page_survive_a_switch_away_and_back() {
        // The same regression as the markets cursor, on the two things BOOK
        // retains. A registry that rebuilt the view would hand back a blotter
        // sorted by weight at page one, silently discarding the column the
        // operator chose and the page they scrolled to.
        let mut store = store_with_a_book(40);
        let mut views = Views::new();

        // The draw is what tells the blotter how many rows a page holds, so it
        // comes before the page turn exactly as the runtime's loop does it.
        store.nav.view = ViewId::Book;
        draw_once(&views, &store);
        views.on_key(ViewId::Book, key(KeyCode::Char('s')), &mut store);
        views.on_key(ViewId::Book, key(KeyCode::Char(']')), &mut store);
        let chosen = (views.book.sort(), views.book.top());
        assert_ne!(chosen.0, book::Sort::default(), "the sort key did not move");
        assert!(chosen.1 > 0, "the page did not turn");

        store.nav.view = ViewId::Markets;
        views.on_key(ViewId::Markets, key(KeyCode::Down), &mut store);
        draw_once(&views, &store);
        store.nav.view = ViewId::Book;
        draw_once(&views, &store);
        assert_eq!(
            (views.book.sort(), views.book.top()),
            chosen,
            "the blotter lost its sort or its page across a view switch"
        );
    }

    /// A store carrying a live book of `n` positions and no market assets.
    fn store_with_a_book(n: usize) -> Store {
        let positions: Vec<String> = (0..n)
            .map(|i| format!(r#"{{"ticker": "P{i:02}", "weight": {}}}"#, (n - i) as f64))
            .collect();
        let mut store = Store::default();
        store.apply(
            AppEvent::Snapshot(Box::new(
                serde_json::from_str(&format!(
                    r#"{{"live_portfolio": {{"positions": [{}]}}}}"#,
                    positions.join(",")
                ))
                .unwrap(),
            )),
            Instant::now(),
        );
        store
    }

    #[test]
    fn drawing_twice_leaves_the_cursor_where_the_operator_put_it() {
        let mut store = store_with_three_assets();
        let mut views = Views::new();
        views.on_key(ViewId::Markets, key(KeyCode::Down), &mut store);
        views.on_key(ViewId::Markets, key(KeyCode::Right), &mut store);
        let cursor = (views.markets.selected(), views.markets.crosshair());

        draw_once(&views, &store);
        draw_once(&views, &store);
        assert_eq!(
            (views.markets.selected(), views.markets.crosshair()),
            cursor,
            "a repaint moved the cursor"
        );
    }
}
