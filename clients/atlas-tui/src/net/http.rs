//! The owner poller: the whole boundary between this client and the desk.
//!
//! Invariant 1 says the owner HTTP runtime is the single DuckDB writer, and
//! every other surface reaches the registry over HTTP. That is not a Python
//! detail — it is why a second client in a second language is safe to build at
//! all. Nothing here takes a file path or a database handle, and there is no way
//! to acquire either: two GETs are the entire vocabulary.
//!
//! The poll runs as a tokio task rather than on a thread of its own, so a
//! refresh is a message instead of a channel timeout, and a fetch never blocks
//! the frame loop for the length of a request.

use crate::bus::{AppEvent, Channel, HttpResult, Tx};
use crate::model::{RegimePanel, Snapshot};
use serde::de::DeserializeOwned;
use std::time::Duration;
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};
use tokio::time::Instant;

/// Long enough to ride out an owner that is mid-valuation, short enough that a
/// dead owner is reported rather than hung on.
pub const TIMEOUT: Duration = Duration::from_secs(8);

/// The base cadence. Snapshots are far more expensive than frames, so they run
/// on their own beat rather than the render loop's.
///
/// Flat on purpose for now: Task 15's activity signal is what downshifts an idle
/// desk to ten seconds, and a downshift guessed here — with nothing that can say
/// whether the desk is idle — would slow a busy desk down.
pub const POLL_INTERVAL: Duration = Duration::from_secs(3);

/// The regime panel is a diagnostic over one snapshot, not the desk itself: it
/// moves when a detector run does, which is far slower than the tape.
pub const REGIME_INTERVAL: Duration = Duration::from_secs(30);

/// How often a client with no owner asks again. Faster than the poll cadence
/// because the operator is usually starting the owner while watching this.
const READY_RETRY: Duration = Duration::from_secs(2);

/// Grace on top of the missed polls, so a request that merely ran long is not
/// reported as a stopped feed.
const STALE_SLACK: Duration = Duration::from_secs(1);

/// The owner port every qlab surface agrees on.
const DEFAULT_PORT: u16 = 8765;

/// When numbers that stopped refreshing stop counting as current.
///
/// Derived from the cadence rather than stated beside it: the two were separate
/// literals once, and a poller that slowed down would have quietly widened the
/// window in which stale marks render as current. A desk that keeps drawing old
/// marks unmarked is the one failure a trading surface may not have.
pub fn stale_after(poll: Duration) -> Duration {
    poll * 3 + STALE_SLACK
}

/// Resolve the owner URL the same way every other qlab surface does, so a desk
/// started on a non-default port is found without extra flags.
pub fn base_from_env() -> String {
    let port = std::env::var("QLAB_UI_PORT")
        .ok()
        .and_then(|p| p.trim().parse::<u16>().ok())
        .unwrap_or(DEFAULT_PORT);
    format!("http://127.0.0.1:{port}")
}

/// Whether the owner is reachable, and what it said if not.
///
/// Kept as a value rather than a bare `bool` because the reason is the useful
/// half: "connection refused" and "404" mean different fixes, and a client that
/// renders an empty desk for both teaches the operator nothing.
#[derive(Debug, Clone)]
pub enum Readiness {
    Ready,
    Unreachable(String),
}

impl Readiness {
    pub fn is_ready(&self) -> bool {
        matches!(self, Readiness::Ready)
    }

    pub fn reason(&self) -> &str {
        match self {
            Readiness::Ready => "",
            Readiness::Unreachable(why) => why,
        }
    }
}

/// Probe before the screen is taken. A client that opens onto a blank frame and
/// only then discovers there is no owner has already lied to the operator once.
pub async fn readiness(base: &str) -> Readiness {
    match build_client() {
        Ok(client) => probe(&client, base.trim_end_matches('/')).await,
        Err(err) => Readiness::Unreachable(format!("no HTTP client ({err})")),
    }
}

/// What the runtime may ask the poller for.
///
/// One variant on purpose. A poller that accepted arbitrary requests would be a
/// fetch path anything holding the handle could aim anywhere; this one can only
/// bring the next scheduled poll forward.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refetch {
    Now,
}

/// The runtime's end of the poller.
pub struct PollerHandle {
    pub refetch: UnboundedSender<Refetch>,
}

impl PollerHandle {
    /// Jump the poll queue. A poller that has already stopped is not something
    /// the frame loop can act on — it is on its way out too.
    pub fn now(&self) {
        let _ = self.refetch.send(Refetch::Now);
    }
}

/// Start polling the owner. Every result arrives on `tx` as an `AppEvent`.
pub fn spawn_poller(base: String, offline: bool, tx: Tx) -> PollerHandle {
    let (refetch, rx) = tokio::sync::mpsc::unbounded_channel();
    tokio::spawn(async move {
        // The only exit is a closed bus, which means the loop this was feeding
        // is gone: there is no failure here worth reporting to nobody.
        let _ = poll_loop(base, offline, tx, rx).await;
    });
    PollerHandle { refetch }
}

/// The bus is closed: the frame loop that these events were for has stopped.
struct Gone;

fn emit(tx: &Tx, ev: AppEvent) -> Result<(), Gone> {
    tx.send(ev).map_err(|_| Gone)
}

/// Report a connection edge, never a repeat: a chip that redraws every poll
/// would dirty the frame three times a second for no news.
fn mark(tx: &Tx, up: &mut Option<bool>, next: bool) -> Result<(), Gone> {
    if *up == Some(next) {
        return Ok(());
    }
    *up = Some(next);
    emit(
        tx,
        if next {
            AppEvent::ConnUp(Channel::Owner)
        } else {
            AppEvent::ConnDown(Channel::Owner)
        },
    )
}

async fn poll_loop(
    base: String,
    offline: bool,
    tx: Tx,
    mut refetch: UnboundedReceiver<Refetch>,
) -> Result<(), Gone> {
    let client = match build_client() {
        Ok(client) => client,
        Err(err) => {
            // Nothing this task can do without one, and no frame it could fix.
            tracing::error!(%err, "could not build the owner HTTP client");
            return emit(&tx, AppEvent::ConnDown(Channel::Owner));
        }
    };
    let base = base.trim_end_matches('/').to_string();
    let lane = if offline { 1 } else { 0 };
    // The same endpoint the Textual client reads: two clients disagreeing about
    // the desk would be worse than having only one.
    let snapshot_url = format!("{base}/api/tui?offline={lane}");
    let regime_url = format!("{base}/api/regime/panel?offline={lane}");

    let mut up: Option<bool> = None;
    // Due immediately, then on its own slow beat.
    let mut regime_due = Instant::now();

    loop {
        // The readiness gate, only while the owner is not known to be answering.
        // A desk that is up does not need a second request per poll to prove it.
        let ready = up == Some(true) || {
            match probe(&client, &base).await {
                Readiness::Ready => true,
                Readiness::Unreachable(why) => {
                    tracing::warn!(%why, "owner is not reachable");
                    mark(&tx, &mut up, false)?;
                    false
                }
            }
        };

        if ready {
            match fetch::<Snapshot>(&client, &snapshot_url).await {
                // An answer is not a snapshot. `ConnUp` is only sent once one
                // decodes: reporting the channel up on any HTTP 200 is what let
                // an owner serving a payload this client cannot read render as
                // "waiting for the first snapshot" for as long as it stayed
                // broken, with the reason visible only in the log.
                Fetched::Decoded(snapshot) => {
                    mark(&tx, &mut up, true)?;
                    emit(&tx, AppEvent::Snapshot(Box::new(snapshot)))?;
                }
                // Fail loud, and on screen. A payload the model cannot read is a
                // broken contract with the owner, never a frame to skip.
                Fetched::Malformed(error) => emit(
                    &tx,
                    AppEvent::Http(HttpResult::Malformed {
                        url: snapshot_url.clone(),
                        error,
                    }),
                )?,
                Fetched::Failed(error) => {
                    // The reason goes to the log rather than to the screen: the
                    // status chip says the owner is down and the content area
                    // names the remedy, but "connection refused" versus "404" is
                    // a distinction Task 16's toasts will carry.
                    tracing::warn!(%error, "owner poll failed");
                    mark(&tx, &mut up, false)?;
                }
            }

            if up == Some(true) && Instant::now() >= regime_due {
                regime_due = Instant::now() + REGIME_INTERVAL;
                match fetch::<RegimePanel>(&client, &regime_url).await {
                    Fetched::Decoded(panel) => emit(&tx, AppEvent::RegimePanel(panel))?,
                    Fetched::Malformed(error) => emit(
                        &tx,
                        AppEvent::Http(HttpResult::Malformed {
                            url: regime_url.clone(),
                            error,
                        }),
                    )?,
                    // The panel is a diagnostic, not the desk: a failure here
                    // must not tell the operator the owner went away when the
                    // snapshot that decides that is still arriving.
                    Fetched::Failed(error) => {
                        tracing::warn!(%error, "regime panel poll failed")
                    }
                }
            }
        }

        // An owner this client cannot read is not up, so a malformed payload
        // retries at the readiness cadence rather than the desk's — the operator
        // is looking at a broken frame either way, and the sooner it recovers the
        // sooner the accusation on screen is retired.
        let delay = if up == Some(true) {
            POLL_INTERVAL
        } else {
            READY_RETRY
        };
        tokio::select! {
            _ = tokio::time::sleep(delay) => {}
            cmd = refetch.recv() => match cmd {
                // Coalesced the way the frame loop coalesces events: three
                // nudges that arrived while a fetch was in flight are one fetch,
                // not three.
                Some(Refetch::Now) => while refetch.try_recv().is_ok() {},
                // Every handle is gone, so nothing will ever nudge again — but
                // the cadence is still owed to the desk.
                None => tokio::time::sleep(delay).await,
            }
        }
    }
}

fn build_client() -> reqwest::Result<reqwest::Client> {
    reqwest::Client::builder().timeout(TIMEOUT).build()
}

async fn probe(client: &reqwest::Client, base: &str) -> Readiness {
    match client.get(format!("{base}/readyz")).send().await {
        Ok(resp) if resp.status().is_success() => Readiness::Ready,
        Ok(resp) => Readiness::Unreachable(format!("owner answered {}", resp.status().as_u16())),
        Err(err) => Readiness::Unreachable(format!(
            "no owner on {base} ({}) — start one with `qlab tui` or `qlab ui`",
            because(&err)
        )),
    }
}

/// The failure with its causes, not just its headline.
///
/// `reqwest`'s `Display` names the request that failed and stops there, so
/// "connection refused" and "timed out" arrive as the same sentence — and those
/// are different problems with different remedies. The chain is the half of the
/// message worth logging.
fn because(err: &(dyn std::error::Error + 'static)) -> String {
    let mut out = err.to_string();
    let mut cause = err.source();
    while let Some(next) = cause {
        out.push_str(&format!(": {next}"));
        cause = next.source();
    }
    out
}

/// The three outcomes of one GET, kept apart because they mean different things
/// to the operator: a desk, a broken contract, or an owner that did not answer.
enum Fetched<T> {
    Decoded(T),
    Malformed(String),
    Failed(String),
}

/// Read the body, then decode it separately from fetching it.
///
/// `reqwest`'s own `json()` folds a decode failure into a request error, which
/// is exactly the distinction this client exists to make: an owner that answers
/// with something unreadable is up, and saying it is down would send the
/// operator to restart a process that is running fine.
async fn fetch<T: DeserializeOwned>(client: &reqwest::Client, url: &str) -> Fetched<T> {
    let resp = match client.get(url).send().await {
        Ok(resp) => resp,
        Err(err) => return Fetched::Failed(because(&err)),
    };
    let status = resp.status();
    if !status.is_success() {
        return Fetched::Failed(format!("owner answered {} for {url}", status.as_u16()));
    }
    match resp.text().await {
        Ok(body) => match serde_json::from_str::<T>(&body) {
            Ok(value) => Fetched::Decoded(value),
            Err(err) => Fetched::Malformed(err.to_string()),
        },
        Err(err) => Fetched::Failed(because(&err)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readiness_carries_the_reason() {
        let r = Readiness::Unreachable("no owner on :8765".into());
        assert!(!r.is_ready());
        assert!(r.reason().contains("8765"));
        assert!(Readiness::Ready.reason().is_empty());
    }

    #[test]
    fn the_port_comes_from_the_same_env_var_the_rest_of_qlab_uses() {
        // Not a cosmetic default: a desk on a non-default port must be found by
        // every client, or one of them opens a second registry writer.
        std::env::set_var("QLAB_UI_PORT", "9123");
        assert_eq!(base_from_env(), "http://127.0.0.1:9123");
        std::env::remove_var("QLAB_UI_PORT");
        assert_eq!(base_from_env(), format!("http://127.0.0.1:{DEFAULT_PORT}"));
    }

    #[test]
    fn staleness_is_three_missed_polls_and_moves_with_the_cadence() {
        // The pin is the relation, not the number: a cadence change that left
        // the threshold behind would widen the window in which stale marks
        // render as current, silently.
        assert!(stale_after(POLL_INTERVAL) > POLL_INTERVAL * 3);
        assert!(stale_after(POLL_INTERVAL) <= POLL_INTERVAL * 4);
        assert_eq!(
            stale_after(Duration::from_secs(10)),
            Duration::from_secs(31),
            "a downshifted poll widens the threshold with it"
        );
    }
}
