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

pub mod audit;
pub mod book;
pub mod desk;
pub mod markets;
pub mod research;
pub mod settings;
pub mod workforce;

use crate::cmd::Command;
use crate::fx::FlashTracker;
use crate::store::{Store, ViewId};
#[cfg(feature = "operator")]
use crate::ui::widgets::confirm;
use crossterm::event::KeyEvent;
use ratatui::{layout::Rect, Frame};
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

    /// This view's modal slot, if it has one.
    ///
    /// Two accessors over one field rather than a `modal()`/`modal_key()` pair,
    /// because those two can disagree: a view that reported a box on screen and
    /// then declined its keystrokes would show a question nothing could answer.
    /// Both of these are `Some(&self.confirm)` in every view that overrides
    /// them, so there is no state to keep in step.
    #[cfg(feature = "operator")]
    fn confirm(&self) -> Option<&confirm::Host> {
        None
    }

    #[cfg(feature = "operator")]
    fn confirm_mut(&mut self) -> Option<&mut confirm::Host> {
        None
    }

    /// This pane has just come back on screen.
    ///
    /// Called by the registry on the first frame after the nav moved here, and
    /// not on the frames after it. `&self`, because what it resets is where the
    /// operator is looking rather than anything the owner said — the same
    /// interior mutability `draw` already uses to publish a rect.
    ///
    /// It exists because a cursor an operator cannot see is a cursor they
    /// cannot correct: SETTINGS routes its keys by which card has focus, and a
    /// focus left on MODELS across a trip to BOOK makes `a` silently dead on a
    /// pane whose desk card is the one being read. A default of nothing, since
    /// every other view either retains something worth keeping across a switch
    /// (a blotter page, a crosshair) or retains nothing at all.
    fn entered(&self) {}

    /// Whether this view is holding a text field open, and therefore owns every
    /// keystroke — including the ones the shell claims for the whole
    /// workstation.
    ///
    /// The shell claims `q`, `r` and the digits precisely so no view can take a
    /// binding the workstation depends on. A field an operator is typing a
    /// sentence into is the one case where that rule has to yield: a goal
    /// containing "requote" would otherwise refresh the desk, jump to BOOK and
    /// quit before its third character.
    ///
    /// Narrow on purpose. It is a claim about *right now*, not about the view —
    /// a surface that answered `true` because it merely has a field would cost
    /// the workstation its navigation permanently. Ctrl-C is exempt in the
    /// shell, because the reflex every operator has must work even here.
    ///
    /// Ungated by feature, because the shell that asks is: in the default build
    /// there is no `Command` a field could produce, so every implementation
    /// returns `false` and the branch is dead.
    fn typing(&self) -> bool {
        false
    }
}

/// Which panes took a symbol the operator named.
///
/// Both flags, because both are possible on their own: a position held outside
/// the polled universe has a blotter row and no grid row, and every quoted
/// asset the desk does not hold is the other way round. A caller that only knew
/// "something moved" could not say which pane to show.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Selected {
    pub markets: bool,
    pub blotter: bool,
}

/// The seven views, alive for the life of the process.
pub struct Views {
    desk: desk::DeskView,
    markets: markets::MarketsView,
    book: book::BookView,
    research: research::ResearchView,
    workforce: workforce::WorkforceView,
    audit: audit::AuditView,
    settings: settings::SettingsView,
    /// The pane the last frame drew, so a view can be told it has been
    /// re-entered.
    ///
    /// Here rather than in the shell because the shell changes the nav from
    /// seven places — three keys, the command line's `/view`, a ticker
    /// selection, and the startup door's handoff — and an entry hook wired at
    /// each of them is one refactor away from missing one. The registry sees
    /// every switch by construction: it is asked to draw exactly one pane per
    /// frame, and a different answer than last frame *is* the switch.
    shown: std::cell::Cell<Option<ViewId>>,
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
            research: research::ResearchView,
            workforce: workforce::WorkforceView::default(),
            audit: audit::AuditView::default(),
            settings: settings::SettingsView::default(),
            // `None`, not the first view: the frame that draws the pane a
            // client opens on is an entry too, and a pane that assumed it was
            // already showing would skip its own reset exactly once.
            shown: std::cell::Cell::new(None),
        }
    }

    /// Hand one write outcome to the surface that is waiting for it.
    ///
    /// Not routed by `ViewId`, deliberately: the answer arrives on the bus
    /// while the operator may be looking anywhere, and a form that only heard
    /// about its own request when SETTINGS happened to be on screen would sit
    /// in "asking the owner…" forever. SETTINGS is the only view that awaits an
    /// answer at all — every other outcome is a toast and a refetch — so this
    /// names it rather than asking seven views a question six of them have no
    /// state for.
    #[cfg(feature = "operator")]
    pub fn wrote(&mut self, outcome: &crate::bus::Wrote) {
        self.settings.wrote(outcome);
    }

    /// Open the one box in this client a credential is typed into.
    ///
    /// Named here rather than reached for, so the startup door's third step is
    /// a call to SETTINGS' own form instead of a second one built beside it —
    /// see `settings::SettingsView::open_login`. The caller is responsible for
    /// putting the operator in front of it; this registry does not move the
    /// nav, because which pane is on screen is the shell's to decide.
    #[cfg(feature = "operator")]
    pub fn open_login(&mut self) {
        self.settings.open_login();
    }

    /// The default build's half: there is no form to open, because there is no
    /// `Command::AlpacaLogin` for it to produce and no writer to carry one.
    #[cfg(not(feature = "operator"))]
    pub fn open_login(&mut self) {}

    /// The modal the active view is showing, if any.
    ///
    /// Routed through the registry rather than reached for on a view, so the
    /// shell has one question to ask and cannot ask it of the wrong surface.
    #[cfg(feature = "operator")]
    pub fn confirm(&self, id: ViewId) -> Option<&confirm::Host> {
        self.at(id).confirm()
    }

    #[cfg(feature = "operator")]
    pub fn confirm_mut(&mut self, id: ViewId) -> Option<&mut confirm::Host> {
        self.at_mut(id).confirm_mut()
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
        // The switch, seen the only place it cannot be missed: the registry is
        // asked for exactly one pane per frame, so an id that differs from the
        // last one it drew *is* the operator having moved.
        if self.shown.replace(Some(id)) != Some(id) {
            self.at(id).entered();
        }
        self.at(id).draw(f, area, store, fx, now);
    }

    pub fn on_key(&mut self, id: ViewId, k: KeyEvent, store: &mut Store) -> Option<Command> {
        self.at_mut(id).on_key(k, store)
    }

    /// Whether the active view is holding a text field open. Routed through the
    /// registry for the same reason `confirm` is: the shell has one question to
    /// ask, and cannot ask it of the wrong surface.
    pub fn typing(&self, id: ViewId) -> bool {
        self.at(id).typing()
    }

    /// Put every cursor that holds a symbol on this one, and say which panes
    /// actually moved.
    ///
    /// Both panes, not the active one: an operator who names a symbol means the
    /// desk's symbol, and a selection that only applied to whichever view
    /// happened to be up would leave the other one pointing somewhere else.
    /// Which panes hold it is the answer, not a detail — a book held wider than
    /// the quoted universe has rows MARKETS cannot show, and the reverse.
    pub fn select_ticker(&mut self, symbol: &str, store: &Store) -> Selected {
        Selected {
            markets: self.markets.select_ticker(symbol, store),
            blotter: self.book.select_ticker(symbol, store),
        }
    }

    /// Aim BOOK's plan cursor, and say where its band left it.
    pub fn select_plan(&mut self, plan_id: &str, store: &Store) -> book::PlanAt {
        self.book.select_plan(plan_id, store)
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
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        let fx = FlashTracker::default();
        let now = Instant::now();
        term.draw(|f| views.draw(store.nav.view, f, Rect::new(0, 0, 120, 36), store, &fx, now))
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
    fn everything_the_book_retains_survives_a_switch_away_and_back() {
        // The same regression as the markets cursor, on the four things BOOK
        // retains. A registry that rebuilt the view would hand back a blotter
        // sorted by weight at page one, shaded by P&L over the whole series —
        // silently discarding the column the operator chose, the page they
        // scrolled to, the quantity they asked the rail to shade by and the
        // window they sliced the curve to.
        let mut store = store_with_a_book(40);
        let mut views = Views::new();

        // The draw is what tells the blotter how many rows a page holds, so it
        // comes before the page turn exactly as the runtime's loop does it.
        store.nav.view = ViewId::Book;
        draw_once(&views, &store);
        for k in ['s', ']', 'h', 'p'] {
            views.on_key(ViewId::Book, key(KeyCode::Char(k)), &mut store);
        }
        let chosen = (
            views.book.sort(),
            views.book.top(),
            views.book.heat(),
            views.book.period(),
        );
        assert_ne!(chosen.0, book::Sort::default(), "the sort key did not move");
        assert!(chosen.1 > 0, "the page did not turn");
        assert_ne!(
            chosen.2,
            book::Heat::default(),
            "the heat mode did not move"
        );
        assert_ne!(chosen.3, book::Period::default(), "the period did not move");

        store.nav.view = ViewId::Markets;
        views.on_key(ViewId::Markets, key(KeyCode::Down), &mut store);
        draw_once(&views, &store);
        store.nav.view = ViewId::Book;
        draw_once(&views, &store);
        assert_eq!(
            (
                views.book.sort(),
                views.book.top(),
                views.book.heat(),
                views.book.period()
            ),
            chosen,
            "BOOK lost something the operator moved across a view switch"
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
