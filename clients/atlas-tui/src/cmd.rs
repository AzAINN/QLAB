//! The slash-scoped command parser: text the operator types becomes a typed command or an error.
//!
//! The type it produces lands here first, because it is the seam that keeps
//! views out of the IO path: a view asks, the runtime acts. Nothing in `ui/`
//! may hold a client, so the only way a keystroke reaches the network is a
//! `Command` the runtime chose to honour.

#[cfg(feature = "operator")]
use crate::ui::widgets::confirm::ConfirmToken;

/// What a surface asks the runtime to do.
///
/// Deliberately small. Task 20 grows it with the parsed slash verbs; a variant
/// added before something dispatches it would be exactly the reachable-code-
/// with-no-caller shape this crate keeps tripping over.
///
/// The three write verbs exist only under the `operator` feature, beside the
/// client that can serve them. A glass build has no `WriteClient` for the
/// runtime to dispatch against, so a variant it could still construct would be
/// a key path that reaches a match arm and stops — the same shape, one layer up.
#[derive(Debug)]
pub enum Command {
    Quit,
    /// Jump the poll queue. A synchronous fetch from the event loop froze the
    /// client for the length of the request, so the refresh nudges the poller
    /// rather than doing the work itself.
    Refresh,
    /// Approve one pending approval request, by id.
    #[cfg(feature = "operator")]
    Approve(String),
    /// Reject one pending approval request, by id.
    #[cfg(feature = "operator")]
    Reject(String),
    /// Book the plan a human confirmed. The token is the confirmation itself —
    /// see `ui::widgets::confirm` — so this variant cannot be constructed by a
    /// key handler that never put the box on screen.
    #[cfg(feature = "operator")]
    Execute(ConfirmToken),
    /// Put a question to the desk manager.
    ///
    /// The one write verb on this workstation with no confirmation ritual in
    /// front of it, and deliberately so: it grants no authority. The owner
    /// records the message and answers only through the coordinator, so the
    /// worst a stray keystroke can do here is add a line to the audit log —
    /// where a confirmation box would instead teach an operator that the ritual
    /// is chrome, which is exactly what must not happen before the boxes that
    /// do guard a fill.
    #[cfg(feature = "operator")]
    Message(String),
    /// Start one registered workflow template against a goal.
    ///
    /// Two strings the operator chose from what the owner served — the template
    /// out of `/api/atlas/templates`, the goal typed into the picker. Neither
    /// carries authority either: the owner refuses a plan-creating template
    /// below `propose`, and a plan a run does produce still needs a persisted
    /// human approval before anything books.
    #[cfg(feature = "operator")]
    StartWorkflow {
        template: String,
        goal: String,
    },
}

/// Hand-written rather than derived, and neither `Eq` nor `Clone`.
///
/// `ConfirmToken` is deliberately not `Clone`: a confirmation is spent by the
/// call it authorises, and a derive here would have made the whole command
/// copyable and handed a caller a second booking for one human decision. Two
/// confirmations are never equal for the same reason — comparing them is only
/// ever a prelude to substituting one for the other — which also means `Command`
/// is not reflexive and therefore cannot be `Eq`.
impl PartialEq for Command {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Command::Quit, Command::Quit) => true,
            (Command::Refresh, Command::Refresh) => true,
            #[cfg(feature = "operator")]
            (Command::Approve(a), Command::Approve(b)) => a == b,
            #[cfg(feature = "operator")]
            (Command::Reject(a), Command::Reject(b)) => a == b,
            #[cfg(feature = "operator")]
            (Command::Execute(_), Command::Execute(_)) => false,
            #[cfg(feature = "operator")]
            (Command::Message(a), Command::Message(b)) => a == b,
            #[cfg(feature = "operator")]
            (
                Command::StartWorkflow {
                    template: a,
                    goal: x,
                },
                Command::StartWorkflow {
                    template: b,
                    goal: y,
                },
            ) => a == b && x == y,
            _ => false,
        }
    }
}
