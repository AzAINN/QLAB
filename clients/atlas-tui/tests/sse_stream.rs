//! The stream reader, against a canned owner on a loopback socket.
//!
//! The parser is unit-tested without a runtime; what is left is everything the
//! socket adds — that a resume actually asks for the cursor, that a dropped
//! connection comes back, and that an event the snapshot would show brings the
//! poll forward. A mocked transport would pin the mock instead of the bytes an
//! owner writes, so these serve real HTTP on port 0 and run offline.

use atlas::bus::{AppEvent, Channel, Rx};
use atlas::net::http::spawn_poller;
use atlas::net::sse::spawn_sse;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// What arrived on the bus, reduced to the fact each assertion is about.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Seen {
    StreamUp,
    StreamDown,
    Event(String),
    Other,
}

fn seen(ev: &AppEvent) -> Seen {
    match ev {
        AppEvent::ConnUp(Channel::Stream) => Seen::StreamUp,
        AppEvent::ConnDown(Channel::Stream) => Seen::StreamDown,
        AppEvent::Sse(event) => Seen::Event(event.kind.clone()),
        _ => Seen::Other,
    }
}

// -- fixtures: bytes an owner would actually write -------------------------

const PHASE: &str = concat!(
    r#"data: {"event_id":"e1","kind":"workflow_phase","payload":{"phase":"referee"},"#,
    r#""ts":"2026-07-30T12:00:01+00:00"}"#,
    "\n\n"
);
/// The owner's keepalive while its dispatch lock is held.
const PING: &str = ": ping\n\n";
/// One frame, arriving as two reads.
const HALT_HEAD: &str = r#"data: {"event_id":"e2","kind":"halt","payload":{"why":"drawdown"},"#;
const HALT_TAIL: &str = "\"ts\":\"2026-07-30T12:00:02+00:00\"}\n\n";
const PLAN_EXECUTED: &str = concat!(
    r#"data: {"event_id":"e3","kind":"plan_executed","payload":{"plan_id":"p1"},"#,
    r#""ts":"2026-07-30T12:00:03+00:00"}"#,
    "\n\n"
);
const QUOTE: &str = concat!(
    r#"data: {"event_id":"e4","kind":"quote","payload":{"rows":[]},"#,
    r#""ts":"2026-07-30T12:00:04+00:00"}"#,
    "\n\n"
);

// -- the canned owner ------------------------------------------------------

struct Owner {
    base: String,
    asked: Arc<Mutex<Vec<(String, Instant)>>>,
}

impl Owner {
    /// Every request whose path (query stripped) matches, in arrival order,
    /// with the instant it arrived — the backoff is a gap between two of these.
    fn asked_for(&self, path: &str) -> Vec<(String, Instant)> {
        self.asked
            .lock()
            .unwrap()
            .iter()
            .filter(|(target, _)| target.split('?').next() == Some(path))
            .cloned()
            .collect()
    }

    /// Wait for the nth request to `path`, so an assertion about a fetch is not
    /// racing the fetch it is about.
    ///
    /// Async, and the sleep is tokio's: `#[tokio::test]` is single-threaded, so
    /// a blocking wait here parks the runtime and starves the very tasks it is
    /// waiting on — which reads as a client that never reconnected.
    async fn wait_for(&self, path: &str, count: usize, budget: Duration) -> Vec<(String, Instant)> {
        let deadline = tokio::time::Instant::now() + budget;
        loop {
            let asked = self.asked_for(path);
            if asked.len() >= count || tokio::time::Instant::now() >= deadline {
                return asked;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
}

/// A canned owner whose `/api/stream` writes `script(connection_index)` chunk by
/// chunk and then closes — the disconnect the resume path needs.
fn spawn_owner(script: fn(usize) -> Vec<&'static str>) -> Owner {
    spawn_owner_with("text/event-stream", script)
}

fn spawn_owner_with(stream_type: &'static str, script: fn(usize) -> Vec<&'static str>) -> Owner {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let base = format!("http://{}", listener.local_addr().unwrap());
    let asked: Arc<Mutex<Vec<(String, Instant)>>> = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&asked);
    let connections = Arc::new(AtomicUsize::new(0));

    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { return };
            let recorded = Arc::clone(&recorded);
            let connections = Arc::clone(&connections);
            // A thread per connection: a subscription stays open for the length
            // of its script, and one handler thread would make every poll wait
            // behind it — which is the deadlock this client exists to avoid.
            std::thread::spawn(move || serve(stream, recorded, connections, stream_type, script));
        }
    });
    Owner { base, asked }
}

fn serve(
    mut stream: TcpStream,
    recorded: Arc<Mutex<Vec<(String, Instant)>>>,
    connections: Arc<AtomicUsize>,
    stream_type: &'static str,
    script: fn(usize) -> Vec<&'static str>,
) {
    let Ok(peek) = stream.try_clone() else { return };
    let mut reader = BufReader::new(peek);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() || request_line.is_empty() {
        return;
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

    // Drain the headers, or the client's write never completes.
    loop {
        let mut header = String::new();
        match reader.read_line(&mut header) {
            Ok(0) | Err(_) => break,
            Ok(_) if header == "\r\n" => break,
            Ok(_) => {}
        }
    }

    let path = target.split('?').next().unwrap_or("/");
    if path == "/api/stream" {
        // No content-length: the body ends when the socket does, exactly as the
        // owner writes it.
        let head = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: {stream_type}\r\n\
             cache-control: no-cache\r\nconnection: close\r\n\r\n"
        );
        if stream.write_all(head.as_bytes()).is_err() {
            return;
        }
        let index = connections.fetch_add(1, Ordering::SeqCst);
        for chunk in script(index) {
            if stream.write_all(chunk.as_bytes()).is_err() || stream.flush().is_err() {
                return;
            }
            // Chunk boundaries the client cannot control for: a frame split
            // across two reads is the case the parser exists for.
            std::thread::sleep(Duration::from_millis(30));
        }
        return; // the drop is the disconnect
    }

    let body = match path {
        "/readyz" => r#"{"ready": true}"#.to_string(),
        "/api/tui" => include_str!("fixtures/tui_snapshot.json").to_string(),
        "/api/regime/panel" => include_str!("fixtures/regime_panel.json").to_string(),
        _ => String::new(),
    };
    let response = if body.is_empty() {
        "HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n".to_string()
    } else {
        format!(
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\
             content-length: {}\r\nconnection: close\r\n\r\n{body}",
            body.len()
        )
    };
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

/// Drain until an event `stop` accepts arrives, returning everything seen on the
/// way. A predicate rather than a sentinel: `Event` carries a kind, and waiting
/// for a placeholder one silently never matches — which reads as a passing test
/// that waited out its whole budget.
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

/// A poller handle with no poller behind it, for the tests that are about the
/// stream rather than the fetch. The receiver comes back so the caller holds it:
/// a dropped one turns every `now()` into a silent no-op, which is the opposite
/// of what the refetch tests below measure.
fn detached_poller() -> (
    atlas::net::http::PollerHandle,
    tokio::sync::mpsc::UnboundedReceiver<atlas::net::http::Refetch>,
) {
    let (refetch, nudges) = tokio::sync::mpsc::unbounded_channel();
    (atlas::net::http::PollerHandle { refetch }, nudges)
}

// -- tests -----------------------------------------------------------------

#[tokio::test]
async fn a_dropped_stream_comes_back_asking_for_the_cursor_it_last_saw() {
    fn script(connection: usize) -> Vec<&'static str> {
        match connection {
            // An event, a keepalive, then one frame torn across two reads —
            // and then the socket goes away mid-subscription.
            0 => vec![PHASE, PING, HALT_HEAD, HALT_TAIL],
            // The second subscription's request line is the assertion.
            _ => vec![PING],
        }
    }
    let owner = spawn_owner(script);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let (poller, _nudges) = detached_poller();
    spawn_sse(owner.base.clone(), tx, poller);

    let seen = drain_until(
        &mut rx,
        |ev| *ev == Seen::Event("halt".into()),
        Duration::from_secs(5),
    )
    .await;
    assert_eq!(
        seen.first(),
        Some(&Seen::StreamUp),
        "the chip goes green when the subscription opens: {seen:?}"
    );
    assert_eq!(
        seen.iter()
            .filter_map(|s| match s {
                Seen::Event(kind) => Some(kind.as_str()),
                _ => None,
            })
            .collect::<Vec<_>>(),
        vec!["workflow_phase", "halt"],
        "the keepalive is not an event and the split frame is one: {seen:?}"
    );

    // The disconnect is news the operator gets, then the resume happens.
    let seen = drain_until(
        &mut rx,
        |ev| *ev == Seen::StreamDown,
        Duration::from_secs(5),
    )
    .await;
    assert!(seen.contains(&Seen::StreamDown), "{seen:?}");

    let asked = owner
        .wait_for("/api/stream", 2, Duration::from_secs(6))
        .await;
    assert_eq!(
        asked.len(),
        2,
        "the subscription has to come back: {asked:?}"
    );
    assert_eq!(asked[0].0, "/api/stream", "the first one has no cursor yet");
    let resumed = &asked[1].0;
    assert!(
        resumed.contains("after=2026-07-30T") && resumed.contains("after_id=e2"),
        "the resume asks for the last exact pair, not the newest timestamp: {resumed}"
    );
    // The half that breaks silently: `+00:00` formatted raw into a query decodes
    // to a space on the owner, and the resume point is off by the whole offset.
    assert!(
        resumed.contains("%2B00") && !resumed.contains('+'),
        "the cursor's offset has to survive the url: {resumed}"
    );

    // And it waited: a hot reconnect loop would hammer an owner that is still
    // coming back up, which is the failure the backoff exists to prevent.
    assert!(
        asked[1].1.duration_since(asked[0].1) >= atlas::net::sse::RETRY,
        "the resume must wait out the backoff, not spin on a refused socket"
    );
}

#[tokio::test]
async fn a_broken_frame_is_said_out_loud_and_the_subscription_lives_on() {
    fn script(_connection: usize) -> Vec<&'static str> {
        vec!["data: {not json at all}\n\n", PHASE]
    }
    let owner = spawn_owner(script);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let (poller, _nudges) = detached_poller();
    spawn_sse(owner.base.clone(), tx, poller);

    let seen = drain_until(
        &mut rx,
        |ev| *ev == Seen::Event("workflow_phase".into()),
        Duration::from_secs(5),
    )
    .await;
    assert!(
        seen.contains(&Seen::Event("stream.malformed".into())),
        "a frame that does not parse has to reach the desk: {seen:?}"
    );
    assert!(
        seen.contains(&Seen::Event("workflow_phase".into())),
        "and must not tear the subscription down: {seen:?}"
    );
}

#[tokio::test]
async fn an_owner_that_answers_the_stream_route_with_json_is_not_a_stream() {
    // An answer is not a stream. Marking the chip up on any HTTP 200 would hold
    // it green over a subscription that can never deliver an event.
    fn script(_connection: usize) -> Vec<&'static str> {
        vec![r#"{"error": "not a stream"}"#]
    }
    let owner = spawn_owner_with("application/json", script);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let (poller, _nudges) = detached_poller();
    spawn_sse(owner.base.clone(), tx, poller);

    let seen = drain_until(
        &mut rx,
        |ev| *ev == Seen::StreamDown,
        Duration::from_secs(5),
    )
    .await;
    assert_eq!(seen.last(), Some(&Seen::StreamDown), "{seen:?}");
    assert!(
        !seen.contains(&Seen::StreamUp),
        "the chip must not go green on the wrong content type: {seen:?}"
    );
}

#[tokio::test]
async fn an_event_the_snapshot_would_show_brings_the_poll_forward() {
    // Plan Part III. Without this the desk shows an executed plan up to a whole
    // poll interval after the operator watched it happen.
    fn script(_connection: usize) -> Vec<&'static str> {
        vec![PLAN_EXECUTED]
    }
    let owner = spawn_owner(script);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let poller = spawn_poller(owner.base.clone(), true, tx.clone());
    spawn_sse(owner.base.clone(), tx, poller.clone());

    let seen = drain_until(
        &mut rx,
        |ev| *ev == Seen::Event("plan_executed".into()),
        Duration::from_secs(5),
    )
    .await;
    assert!(
        seen.contains(&Seen::Event("plan_executed".into())),
        "{seen:?}"
    );

    let asked = owner.wait_for("/api/tui", 2, Duration::from_secs(2)).await;
    assert_eq!(
        asked.len(),
        2,
        "the event has to jump the poll queue, not wait it out: {asked:?}"
    );
    assert!(
        atlas::net::http::POLL_INTERVAL > Duration::from_secs(2),
        "this test only means something while the cadence outlasts its budget"
    );
}

#[tokio::test]
async fn a_quote_does_not_bring_the_poll_forward() {
    // The ticker is rendered from the event itself. A quote that refetched the
    // aggregate snapshot would turn the tape into a poll amplifier.
    fn script(_connection: usize) -> Vec<&'static str> {
        vec![QUOTE]
    }
    let owner = spawn_owner(script);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let poller = spawn_poller(owner.base.clone(), true, tx.clone());
    spawn_sse(owner.base.clone(), tx, poller.clone());

    // Wait for the quote to have been handled, so "no extra fetch" is a fact
    // rather than a race the assertion won.
    let seen = drain_until(
        &mut rx,
        |ev| *ev == Seen::Event("quote".into()),
        Duration::from_secs(5),
    )
    .await;
    assert!(seen.contains(&Seen::Event("quote".into())), "{seen:?}");
    owner.wait_for("/api/tui", 1, Duration::from_secs(2)).await;

    tokio::time::sleep(Duration::from_millis(800)).await;
    assert_eq!(
        owner.asked_for("/api/tui").len(),
        1,
        "only the cadence polls, and it has not come round yet"
    );
}
