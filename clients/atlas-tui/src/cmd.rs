//! The slash-scoped command parser: text the operator types becomes a typed command or an error.
//!
//! The type it produces lands here first, because it is the seam that keeps
//! views out of the IO path: a view asks, the runtime acts. Nothing in `ui/`
//! may hold a client, so the only way a keystroke reaches the network is a
//! `Command` the runtime chose to honour.

/// What a surface asks the runtime to do.
///
/// Deliberately small. Task 20 grows it with the parsed slash verbs; a variant
/// added before something dispatches it would be exactly the reachable-code-
/// with-no-caller shape this crate keeps tripping over.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Quit,
    /// Jump the poll queue. A synchronous fetch from the event loop froze the
    /// client for the length of the request, so the refresh nudges the poller
    /// rather than doing the work itself.
    Refresh,
}
