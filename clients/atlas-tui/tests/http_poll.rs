//! The owner poller, against a canned owner on a loopback socket.
//!
//! A real `TcpListener` rather than a mocked transport: the thing worth pinning
//! is that bytes an owner would actually write reach the bus as typed events,
//! and a fake `reqwest` layer would pin the mock instead. The listener binds
//! port 0 and serves fixture bytes, so these run offline and in parallel with
//! everything else.

use atlas::bus::{AppEvent, Channel, HttpResult, Rx};
use atlas::net::http::{spawn_poller, Refetch, POLL_INTERVAL, READY_RETRY};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// What arrived on the bus, reduced to the fact each assertion is about.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Seen {
    ConnUp,
    ConnDown,
    Snapshot,
    Regime,
    Malformed(String),
    Other,
}

fn seen(ev: &AppEvent) -> Seen {
    match ev {
        AppEvent::ConnUp(Channel::Owner) => Seen::ConnUp,
        AppEvent::ConnDown(Channel::Owner) => Seen::ConnDown,
        AppEvent::Snapshot(_) => Seen::Snapshot,
        AppEvent::RegimePanel(_) => Seen::Regime,
        AppEvent::Http(HttpResult::Malformed { url, .. }) => Seen::Malformed(url.clone()),
        _ => Seen::Other,
    }
}

/// A canned owner: fixed bytes per path, and a record of what was asked for.
struct Owner {
    base: String,
    asked: Arc<Mutex<Vec<(String, Instant)>>>,
}

impl Owner {
    /// Every request whose path (query stripped) matches, in arrival order.
    fn asked_for(&self, path: &str) -> Vec<Instant> {
        self.asked
            .lock()
            .unwrap()
            .iter()
            .filter(|(target, _)| target.split('?').next() == Some(path))
            .map(|(_, at)| *at)
            .collect()
    }

    fn targets(&self) -> Vec<String> {
        self.asked
            .lock()
            .unwrap()
            .iter()
            .map(|(target, _)| target.clone())
            .collect()
    }
}

/// Serve `bodies` keyed by path; anything else 404s.
///
/// `connection: close` per response so each request is its own accept — the
/// handler is single-threaded, and a pooled keep-alive connection would let one
/// idle socket hold the next request hostage.
fn spawn_owner(bodies: Vec<(&'static str, String)>) -> Owner {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let base = format!("http://{}", listener.local_addr().unwrap());
    let asked = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&asked);

    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { return };
            let Ok(peek) = stream.try_clone() else {
                continue;
            };
            let mut reader = BufReader::new(peek);

            let mut request_line = String::new();
            if reader.read_line(&mut request_line).is_err() || request_line.is_empty() {
                continue;
            }
            let target = request_line
                .split_whitespace()
                .nth(1)
                .unwrap_or("/")
                .to_string();
            recorded
                .lock()
                .unwrap()
                .push((target.clone(), Instant::now()));

            // Drain the headers, or the client's write never completes and the
            // response races the request it is answering.
            loop {
                let mut header = String::new();
                match reader.read_line(&mut header) {
                    Ok(0) | Err(_) => break,
                    Ok(_) if header == "\r\n" => break,
                    Ok(_) => {}
                }
            }

            let path = target.split('?').next().unwrap_or("/");
            let response = match bodies.iter().find(|(p, _)| *p == path) {
                Some((_, body)) => format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
                     content-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                ),
                None => "HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\
                         connection: close\r\n\r\n"
                    .to_string(),
            };
            let _ = stream.write_all(response.as_bytes());
            let _ = stream.flush();
        }
    });

    Owner { base, asked }
}

fn ready() -> (&'static str, String) {
    ("/readyz", r#"{"ready": true}"#.to_string())
}

fn snapshot_fixture() -> (&'static str, String) {
    (
        "/api/tui",
        include_str!("fixtures/tui_snapshot.json").to_string(),
    )
}

fn regime_fixture() -> (&'static str, String) {
    (
        "/api/regime/panel",
        include_str!("fixtures/regime_panel.json").to_string(),
    )
}

/// Drain the bus until `want` events have arrived or the budget runs out.
async fn drain(rx: &mut Rx, want: usize, budget: Duration) -> Vec<Seen> {
    let deadline = tokio::time::Instant::now() + budget;
    let mut out = Vec::new();
    while out.len() < want {
        let left = deadline.saturating_duration_since(tokio::time::Instant::now());
        if left.is_zero() {
            break;
        }
        match tokio::time::timeout(left, rx.recv()).await {
            Ok(Some(ev)) => out.push(seen(&ev)),
            Ok(None) | Err(_) => break,
        }
    }
    out
}

/// Drain until an event `stop` accepts arrives, returning everything seen on the
/// way. A predicate rather than a sentinel value: `Malformed` carries a url, and
/// comparing against a placeholder one silently never matches — which reads as a
/// passing test that waited out its whole budget.
async fn drain_until(rx: &mut Rx, stop: impl Fn(&Seen) -> bool, budget: Duration) -> Vec<Seen> {
    let deadline = tokio::time::Instant::now() + budget;
    let mut out = Vec::new();
    loop {
        let left = deadline.saturating_duration_since(tokio::time::Instant::now());
        if left.is_zero() {
            return out;
        }
        match tokio::time::timeout(left, rx.recv()).await {
            Ok(Some(ev)) => {
                let ev = seen(&ev);
                let done = stop(&ev);
                out.push(ev);
                if done {
                    return out;
                }
            }
            Ok(None) | Err(_) => return out,
        }
    }
}

#[tokio::test]
async fn a_payload_that_decodes_brings_the_owner_up_and_reaches_the_bus() {
    let owner = spawn_owner(vec![ready(), snapshot_fixture(), regime_fixture()]);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let _poller = spawn_poller(owner.base.clone(), true, tx);

    let seen = drain(&mut rx, 3, Duration::from_secs(5)).await;
    assert_eq!(
        seen.first(),
        Some(&Seen::ConnUp),
        "the owner comes up on the first payload that decodes: {seen:?}"
    );
    assert!(
        seen.contains(&Seen::Snapshot),
        "the snapshot has to reach the bus typed: {seen:?}"
    );
    assert!(
        seen.contains(&Seen::Regime),
        "the regime panel rides the same poller: {seen:?}"
    );
    assert!(
        !seen.iter().any(|s| matches!(s, Seen::Malformed(_))),
        "the committed fixture is what the owner serves: {seen:?}"
    );

    // The readiness gate is asked before the desk is, and the lane the operator
    // chose travels with every request — a synthetic client that silently asked
    // for live data would be the worst kind of quiet.
    let targets = owner.targets();
    assert_eq!(
        targets.first().map(String::as_str),
        Some("/readyz"),
        "{targets:?}"
    );
    assert!(
        targets.iter().any(|t| t == "/api/tui?offline=1"),
        "{targets:?}"
    );
}

#[tokio::test]
async fn a_payload_the_model_cannot_read_fails_loud_and_never_marks_the_owner_up() {
    // An owner serving garbage is reachable, so every connection chip would stay
    // green while nothing on the desk is current. The decode failure is the
    // event; skipping the frame is not an option.
    let owner = spawn_owner(vec![
        ready(),
        (
            "/api/tui",
            r#"{"live_portfolio": {"equity": "a lot, honestly"}}"#.to_string(),
        ),
    ]);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let _poller = spawn_poller(owner.base.clone(), true, tx);

    let seen = drain_until(
        &mut rx,
        |ev| matches!(ev, Seen::Malformed(_)),
        Duration::from_secs(5),
    )
    .await;
    let bad = seen
        .iter()
        .find_map(|s| match s {
            Seen::Malformed(url) => Some(url.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("the decode failure must reach the bus: {seen:?}"));
    assert!(bad.contains("/api/tui"), "the failure names its url: {bad}");
    assert!(
        !seen.contains(&Seen::ConnUp),
        "an answer is not a snapshot: {seen:?}"
    );
    assert!(
        !seen.contains(&Seen::Snapshot),
        "nothing decoded, so nothing may render: {seen:?}"
    );
}

#[tokio::test]
async fn an_owner_answering_with_garbage_is_polled_at_the_desks_cadence_not_the_retrys() {
    // Reachable and readable are two facts and were one field. An owner serving
    // a payload the model cannot read left it unset, so every cycle paid a
    // readiness probe *and* a full `/api/tui` at the two-second retry cadence —
    // three times the request rate of a healthy desk, aimed at the owner that is
    // already struggling. The pin is the probe count: a reachable owner is not
    // asked to prove it again.
    let owner = spawn_owner(vec![
        ready(),
        (
            "/api/tui",
            r#"{"live_portfolio": {"equity": "a lot, honestly"}}"#.to_string(),
        ),
    ]);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let _poller = spawn_poller(owner.base.clone(), true, tx);

    // Past one whole poll interval, and past two of the retry interval — which
    // is what makes the two cadences tell each other apart here.
    let budget = POLL_INTERVAL + Duration::from_millis(600);
    let seen = drain(&mut rx, 99, budget).await;
    assert!(
        seen.iter().any(|s| matches!(s, Seen::Malformed(_))),
        "the decode failure still has to reach the bus: {seen:?}"
    );
    assert_eq!(
        owner.asked_for("/readyz").len(),
        1,
        "an owner that answered is not asked to prove it again every cycle: {:?}",
        owner.targets()
    );
    assert!(
        owner.asked_for("/api/tui").len() >= 2,
        "the desk's own cadence still has to run: {:?}",
        owner.targets()
    );
    // What makes the counts above discriminate: the retry is the tighter loop,
    // and the budget covers a second probe under it as well as a second poll
    // under the desk's cadence. Without both, this passes on the bug it is about.
    assert!(
        READY_RETRY < POLL_INTERVAL,
        "the retry is not the tight loop"
    );
    assert!(READY_RETRY < budget && POLL_INTERVAL < budget);
}

#[tokio::test]
async fn a_manual_refresh_fetches_ahead_of_the_cadence() {
    let owner = spawn_owner(vec![ready(), snapshot_fixture(), regime_fixture()]);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let poller = spawn_poller(owner.base.clone(), true, tx);

    let seen = drain_until(&mut rx, |ev| *ev == Seen::Snapshot, Duration::from_secs(5)).await;
    assert!(seen.contains(&Seen::Snapshot), "{seen:?}");
    let before = owner.asked_for("/api/tui");
    assert_eq!(before.len(), 1, "one poll so far");

    // The seam Task 7's stream nudges and the `r` key drives: a refresh must not
    // wait out the cadence, or a keystroke reads as a hung client.
    poller.refetch.send(Refetch::Now).expect("poller is alive");
    let jumped = Duration::from_secs(1);
    let seen = drain_until(&mut rx, |ev| *ev == Seen::Snapshot, jumped).await;
    assert!(
        seen.contains(&Seen::Snapshot),
        "the refresh has to jump the queue: {seen:?}"
    );
    let after = owner.asked_for("/api/tui");
    assert_eq!(after.len(), 2, "the nudge is a fetch, not a replay");
    assert!(
        after[1].duration_since(after[0]) < POLL_INTERVAL,
        "the second poll waited out the cadence instead of jumping it"
    );
    assert!(
        jumped < POLL_INTERVAL,
        "this test only means something while the cadence is longer than its budget"
    );
}

#[tokio::test]
async fn an_owner_that_never_answers_reports_down_rather_than_hanging_quiet() {
    // Nothing is listening on this port. The readiness gate is what turns that
    // into a chip the operator can read, on the first attempt rather than after
    // a timeout's worth of blank frame.
    let dead = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", dead.local_addr().unwrap());
    drop(dead);

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let _poller = spawn_poller(base, true, tx);

    let seen = drain(&mut rx, 1, Duration::from_secs(5)).await;
    assert_eq!(seen.first(), Some(&Seen::ConnDown), "{seen:?}");
}
