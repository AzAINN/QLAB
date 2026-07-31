//! The rendering half of the client: chrome, views, and the widgets they share.
//!
//! Nothing under here performs IO. A view is handed a `&Store` and a `Rect` and
//! returns pixels; a keystroke it wants acted on comes back as a `Command` for
//! the runtime to dispatch. That is what keeps "read-only by construction" a
//! property of the composition root rather than a rule every view has to
//! remember.

pub mod shell;
pub mod views;
pub mod widgets;
