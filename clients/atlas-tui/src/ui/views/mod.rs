//! One file per view; each renders into the region the shell hands it and owns no state.
//!
//! Views are unit structs on purpose. Everything a view needs to draw is in the
//! `Store`, so there is no per-view state to lose across a rebuild and no way
//! for two surfaces to disagree about the desk. `on_key` still takes `&mut
//! self` because the trait is the shape a stateful view would need, and a
//! signature change later would touch every view at once.

pub mod desk;

use crate::cmd::Command;
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

pub trait View {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store);

    /// A key the shell did not claim. Returning a `Command` asks the runtime to
    /// act; a view never acts itself, which is what keeps `ui/` free of IO.
    fn on_key(&mut self, k: KeyEvent, store: &mut Store) -> Option<Command>;
}

/// The view a `ViewId` names.
///
/// `Box<dyn View>` of a unit struct allocates nothing, so this is a match and a
/// vtable rather than a per-frame heap trip.
pub fn for_id(id: ViewId) -> Box<dyn View> {
    match id {
        ViewId::Desk => Box::new(desk::DeskView),
        ViewId::Markets => Box::new(Unbuilt(ViewId::Markets)),
        ViewId::Book => Box::new(Unbuilt(ViewId::Book)),
        ViewId::Research => Box::new(Unbuilt(ViewId::Research)),
        ViewId::Workforce => Box::new(Unbuilt(ViewId::Workforce)),
        ViewId::Audit => Box::new(Unbuilt(ViewId::Audit)),
        ViewId::Settings => Box::new(Unbuilt(ViewId::Settings)),
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
    fn draw(&self, f: &mut Frame, area: Rect, _store: &Store) {
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
    use crossterm::event::{KeyCode, KeyModifiers};

    #[test]
    fn every_view_id_resolves_to_a_view_that_declines_unclaimed_keys() {
        // The lookup is the shell's fallthrough target, so a missing arm would
        // be a panic in the key path rather than a compile error.
        for id in ViewId::ALL {
            let mut store = Store::default();
            let key = KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE);
            assert_eq!(for_id(id).on_key(key, &mut store), None, "{id:?}");
        }
    }
}
