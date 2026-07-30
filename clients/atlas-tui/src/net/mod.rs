//! Everything that leaves the process. The owner runtime is the only host this
//! client may talk to — it holds the sole registry handle, and this crate has none.

use crate::bus::{AppEvent, Channel, Tx};

pub mod http;
pub mod sse;

/// The bus is closed: the frame loop that these events were for has stopped.
pub(crate) struct Gone;

pub(crate) fn emit(tx: &Tx, ev: AppEvent) -> Result<(), Gone> {
    tx.send(ev).map_err(|_| Gone)
}

/// Report a connection edge, never a repeat: a chip that redrew every poll
/// would dirty the frame three times a second for no news.
///
/// Shared by both feeds rather than written twice. The edge guard is the reason
/// the chips are cheap, and a second copy of it is a second place for the guard
/// to be forgotten — the stream is the noisier of the two.
pub(crate) fn mark(
    tx: &Tx,
    channel: Channel,
    up: &mut Option<bool>,
    next: bool,
) -> Result<(), Gone> {
    if *up == Some(next) {
        return Ok(());
    }
    *up = Some(next);
    emit(
        tx,
        if next {
            AppEvent::ConnUp(channel)
        } else {
            AppEvent::ConnDown(channel)
        },
    )
}

/// The failure with its causes, not just its headline.
///
/// `reqwest`'s `Display` names the request that failed and stops there, so
/// "connection refused" and "timed out" arrive as the same sentence — and those
/// are different problems with different remedies. The chain is the half of the
/// message worth logging.
pub(crate) fn because(err: &(dyn std::error::Error + 'static)) -> String {
    let mut out = err.to_string();
    let mut cause = err.source();
    while let Some(next) = cause {
        out.push_str(&format!(": {next}"));
        cause = next.source();
    }
    out
}
