//! What `/cli` does to the desk, and what every frame after it owes the child.
//!
//! Three modules already hold a piece of the pane: `pty` owns the child,
//! `store` owns its screen and the state machine, and `ui::widgets::terminal`
//! draws it. This is the seam between those and the workstation — the two calls
//! the runtime makes, and the only place that decides how big a child's screen
//! is.
//!
//! It lives in the library rather than in `main.rs` for the reason `dispatch`
//! does, and the reason invariant 10 keeps teaching this crate: the binary has
//! no test harness, and a geometry that is right in the widget and wrong at the
//! call site is a Claude session wrapping to a screen nobody drew. What is left
//! in `main.rs` is one line per call.
//!
//! **Both calls belong on the runtime's own loop.** `Store::open_pty` spawns the
//! task that bridges the child's bytes onto the desk's bus, so called outside a
//! tokio runtime it panics rather than refusing. Every caller here is inside the
//! loop that drains that bus.
//!
//! Gated with everything else that can start a child: the monitoring build has
//! no `/cli`, no pane, and no column to give one.

use crate::bus::Tx;
use crate::store::{refuse_pty, PtyState, Store, CHILD};
use crate::ui::views::Views;
use crate::ui::widgets::terminal;
use ratatui::layout::Rect;

/// Start a child in the ATLAS column, or say why there is not one.
///
/// `area` is the whole pane — the column a frame would draw it in — and the
/// child is given what is *inside* it ([`terminal::inner`]). The two are
/// different rects, and a session told the outer one wraps its output two
/// columns wider than the screen its bytes are painted onto.
///
/// **A pane whose child has ended is replaced, not reopened beside.** `/cli` on
/// an ending closes first, so a retry that fails leaves no pane at all rather
/// than the dead child's sentence on a border under a toast about a different
/// child: one frame, one story about one child. A *running* child is not closed
/// — the store refuses the second open by name, and killing the first to make
/// room would be this client answering "one at a time" with a dead session.
///
/// The `Err` is a value and not a sentence to display: both refusals are also
/// posted on the bus, where `ui::widgets::toast` puts them on screen, so a
/// caller that showed this too would put one refusal in two boxes.
pub fn open(
    store: &mut Store,
    views: &mut Views,
    spawn: &dyn crate::pty::Spawn,
    area: Rect,
    tx: Tx,
) -> Result<(), String> {
    // A window with no room for a terminal is told so, rather than given a
    // child it cannot draw: below the widget's floor the pane renders a refusal
    // and no border, so the session behind it would be one an operator can
    // neither see, type at, nor read the way out of — and a screen with no
    // columns at all panics inside the parser rather than refusing.
    //
    // Measured on the column a pane would actually get (`shell::pane_column`),
    // which is wider than today's: the desk rail gives its own up for a pane
    // that would otherwise be unusable.
    if !terminal::fits(area) {
        return Err(refuse_pty(
            &tx,
            format!(
                "this window has no room for a `{CHILD}` pane: the column is {}×{} and a \
                 terminal needs more — widen the window and ask again",
                area.width, area.height
            ),
        ));
    }
    if matches!(store.pty_state(), PtyState::Ended { .. }) {
        store.close_pty();
    }
    let inner = terminal::inner(area);
    let opened = store.open_pty(spawn, inner.width, inner.height, tx);
    if opened.is_ok() {
        // Only on success: a `/cli` that started nothing has taken no column,
        // and a question cleared for a pane that never opened is a sentence the
        // operator has to type again for no reason.
        views.pane_opened();
    }
    opened
}

/// What quitting the workstation did to the child, when it did anything.
///
/// The kill needs no code — the session drops with the store at the end of
/// `main`, and `pty.rs` hangs the child up on `Drop` — so what is owed is the
/// *saying so*, and an operator who quit with Claude mid-answer should not have
/// to infer it. Not a toast: nothing draws another frame after the loop
/// returns, and anything printed before the terminal guard restores is wiped
/// with the alternate screen, so the runtime prints this on stderr afterwards.
///
/// Here rather than inline in `main.rs` for this module's own reason: the
/// binary is in no test binary, and both arms of a sentence are worth pinning.
/// An `Ended` pane gets none — that child is already gone, and it said so on
/// the pane's own border while the operator was looking at it.
pub fn quit_note(state: &PtyState) -> Option<&'static str> {
    match state {
        PtyState::Running => {
            Some("the Claude session in the ATLAS pane was ended with this window")
        }
        PtyState::Absent | PtyState::Ended { .. } => None,
    }
}

/// The frame moved the pane: tell the child what it is drawing on now.
///
/// Called after every frame with what the last one published, which is empty
/// whenever no terminal was drawn — another view on screen, a pane too narrow
/// for the widget, or no child at all. An empty rect resizes nothing: a child
/// given a screen with no cells on it would have nowhere to put its prompt.
///
/// Deliberately not "compare, then call": the store holds the only record of
/// what the child was actually told and returns early when the size has not
/// moved, and a second copy of that record here is the one that comes to
/// disagree with it.
pub fn resized(store: &mut Store, inner: Rect) {
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    store.pty_resize(inner.width, inner.height);
}
