//! Everything that leaves the process. The owner runtime is the only host this
//! client may talk to — it holds the sole registry handle, and this crate has none.

use crate::bus::{AppEvent, Channel, Tx};

pub mod http;
pub mod sse;
// The write half, and the only place in the crate that can POST. Gated so the
// default build has no such call site at all — see `write.rs` and the
// `[features]` note in Cargo.toml. The attribute and the declaration are pinned
// verbatim by `tests/operator_gate.rs`, because a gate that named the wrong
// feature would compile the writer into the monitoring build in silence.
#[cfg(feature = "operator")]
pub mod write;

/// The bus is closed: the frame loop that these events were for has stopped.
pub(crate) struct Gone;

pub(crate) fn emit(tx: &Tx, ev: AppEvent) -> Result<(), Gone> {
    tx.send(ev).map_err(|_| Gone)
}

/// What a feed's state is worth saying — the edge, never the beat.
///
/// Split out of `mark` so the rule is a function whose three cases a test can
/// enumerate. The alternative is pointing the client at a dead port and reading
/// a log file, which is how both feeds came to write about a line a second for
/// the whole of an outage without anyone noticing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Edge {
    /// The feed has gone away. Worth a line: it is what broke.
    Down,
    /// It came back. Worth a line too — a log that records the failure and
    /// never the repair reads as an outage that is still running.
    Recovered,
    /// The first success this client ever had on the feed. Not news: it is the
    /// client starting up, which the operator is watching happen.
    Opened,
}

pub(crate) fn edge(was: Option<bool>, next: bool) -> Option<Edge> {
    match (was, next) {
        (Some(held), _) if held == next => None,
        (_, false) => Some(Edge::Down),
        (None, true) => Some(Edge::Opened),
        // `Some(true), true` is caught by the guard above; the compiler cannot
        // see that, and a wildcard here would be a real arm rather than a
        // formality the day a third state is added.
        (Some(_), true) => Some(Edge::Recovered),
    }
}

/// Report a connection edge, never a repeat: a chip that redrew every poll
/// would dirty the frame three times a second for no news.
///
/// Shared by both feeds rather than written twice. The edge guard is the reason
/// the chips are cheap, and a second copy of it is a second place for the guard
/// to be forgotten — the stream is the noisier of the two.
///
/// The log rides the same guard. `why` is the reason the feed went down, and it
/// is written once per outage rather than once per retry: against a dead owner
/// the readiness probe and the stream reconnect between them wrote about a line
/// a second forever, which buries the one line that says what broke under ten
/// thousand that say it is still broken.
pub(crate) fn mark(
    tx: &Tx,
    channel: Channel,
    up: &mut Option<bool>,
    next: bool,
    why: &str,
) -> Result<(), Gone> {
    let Some(edge) = edge(*up, next) else {
        return Ok(());
    };
    *up = Some(next);
    match edge {
        Edge::Down => tracing::warn!(?channel, %why, "feed went away"),
        // At `warn` beside the failure, deliberately: both halves of an outage
        // belong in the same file at the same level, or the record only ever
        // shows things breaking.
        Edge::Recovered => tracing::warn!(?channel, "feed is back"),
        Edge::Opened => tracing::debug!(?channel, "feed opened"),
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_an_edge_is_news_and_a_first_success_is_not_a_recovery() {
        // The beat this guards is real and was live in both feeds: a dead owner
        // produced a warn every two seconds from the readiness probe and another
        // every two from the stream reconnect, for as long as it stayed dead.
        assert_eq!(edge(None, false), Some(Edge::Down));
        assert_eq!(edge(Some(true), false), Some(Edge::Down));
        assert_eq!(
            edge(Some(false), false),
            None,
            "still down is not news; it is the beat"
        );

        assert_eq!(edge(Some(false), true), Some(Edge::Recovered));
        assert_eq!(
            edge(None, true),
            Some(Edge::Opened),
            "the first payload is the client starting, not a repair"
        );
        assert_eq!(edge(Some(true), true), None);
    }
}
