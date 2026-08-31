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
    Backends,
    Proposal(bool),
    Malformed(String),
    Other,
}

fn seen(ev: &AppEvent) -> Seen {
    match ev {
        AppEvent::ConnUp(Channel::Owner) => Seen::ConnUp,
        AppEvent::ConnDown(Channel::Owner) => Seen::ConnDown,
        AppEvent::Snapshot(_) => Seen::Snapshot,
        AppEvent::RegimePanel(_) => Seen::Regime,
        AppEvent::Backends(_) => Seen::Backends,
        // Both answers, kept apart: `{"proposal": null}` is the owner saying
        // the desk has no open question, which is what retires a card, and a
        // test that could not tell it from a payload that never arrived could
        // not pin that at all.
        AppEvent::Proposal(proposal) => Seen::Proposal(proposal.is_some()),
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

fn proposal_fixture() -> (&'static str, String) {
    (
        "/api/desk/proposal",
        r#"{"proposal": {"plan_id": "b92a58fa5c1d4e7f", "approval_state": "pending",
             "targets": {"ACWI": 0.3}, "targets_hash": "0f1e2d3c4b5a6978",
             "referee": {"verdict": "PASS", "source": "referee-agent",
                         "targets_hash": "0f1e2d3c4b5a6978"}}}"#
            .to_string(),
    )
}

fn backends_fixture() -> (&'static str, String) {
    (
        "/api/llm/backends",
        include_str!("fixtures/llm_backends.json").to_string(),
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

/// How long a predicate drain waits before it calls the poller hung.
///
/// A deadlock guard rather than a measurement, so it is set absurdly wide: no
/// assertion is made *about* this number, and a run that reaches it has found a
/// poller that stopped rather than a machine that was slow.
const CEILING: Duration = Duration::from_secs(30);

/// How often a predicate drain re-reads a fact the bus does not announce.
///
/// The owner records a request before it writes the response, so the fact and
/// the event that follows it are not simultaneous. Waking on this tick as well
/// as on events means the condition is seen even when the event that would have
/// carried it never arrives.
const RECHECK: Duration = Duration::from_millis(25);

/// Drain until `settled` — a fact about the *owner*, not about any one event —
/// holds, or the ceiling runs out.
///
/// The predicate replaces a fixed-budget drain, and the difference is the whole
/// point: a budget has to be wide enough for the slowest machine and is still a
/// coin toss on a loaded one (`POLL_INTERVAL + 1.5 s` against a 3 s cadence,
/// observed to fail about one run in six). Waiting on the condition itself takes
/// as long as the machine needs and stops the instant it holds — slower where it
/// has to be, and faster than the budget everywhere else.
async fn drain_until_owner(
    rx: &mut Rx,
    settled: impl Fn() -> bool,
    ceiling: Duration,
) -> Vec<Seen> {
    let deadline = tokio::time::Instant::now() + ceiling;
    let mut out = Vec::new();
    while !settled() {
        let left = deadline.saturating_duration_since(tokio::time::Instant::now());
        if left.is_zero() {
            return out;
        }
        match tokio::time::timeout(left.min(RECHECK), rx.recv()).await {
            Ok(Some(ev)) => out.push(seen(&ev)),
            // The bus closed: nothing further can arrive, so the condition can
            // no longer become true and waiting out the ceiling proves nothing.
            Ok(None) => return out,
            // The recheck tick, not the ceiling — the loop condition decides.
            Err(_) => {}
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

    // Wait for the *second* desk poll rather than for a stretch of wall time
    // long enough to contain one. The window is then exactly as wide as this
    // machine needed, and on a machine that needed longer it is wider — where a
    // fixed `POLL_INTERVAL + 1.5 s` budget was a 1.5 s margin over a 3 s cadence
    // and failed about one run in six under load.
    let seen = drain_until_owner(&mut rx, || owner.asked_for("/api/tui").len() >= 2, CEILING).await;
    assert!(
        owner.asked_for("/api/tui").len() >= 2,
        "the desk's own cadence never ran a second time inside {CEILING:?}: {:?}",
        owner.targets()
    );
    assert!(
        seen.iter().any(|s| matches!(s, Seen::Malformed(_))),
        "the decode failure still has to reach the bus: {seen:?}"
    );
    // The pin. The window just waited out holds two desk polls, so it is at
    // least one `POLL_INTERVAL` wide — and the retry is the tighter loop, so a
    // client that had gone back to the retry cadence would have probed again
    // inside it. One probe is the whole finding.
    assert_eq!(
        owner.asked_for("/readyz").len(),
        1,
        "an owner that answered is not asked to prove it again every cycle: {:?}",
        owner.targets()
    );
    // What makes that count discriminate, stated rather than assumed: without
    // this the window a second poll defines could be narrower than a retry, and
    // the assertion above would pass on the bug it is about.
    assert!(
        READY_RETRY < POLL_INTERVAL,
        "the retry is not the tight loop"
    );
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
async fn the_backend_catalog_is_fetched_when_it_is_asked_for_and_never_on_the_beat() {
    // The route probes every configured daemon, so a cadence here would put a
    // network round trip per backend behind a beat — the cost the owner refuses
    // to pay on `/api/tui`, moved rather than avoided. The palette entering the
    // model scope is the only thing that asks.
    let owner = spawn_owner(vec![
        ready(),
        snapshot_fixture(),
        regime_fixture(),
        backends_fixture(),
    ]);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let poller = spawn_poller(owner.base.clone(), true, tx);

    // Two desk polls' worth of the beat, which is where a fourth `*_INTERVAL`
    // would have shown up.
    drain_until_owner(&mut rx, || owner.asked_for("/api/tui").len() >= 2, CEILING).await;
    assert!(
        owner.asked_for("/api/llm/backends").is_empty(),
        "the catalog rode a poll: {:?}",
        owner.targets()
    );
    let polls = owner.asked_for("/api/tui").len();

    poller
        .refetch
        .send(Refetch::Backends)
        .expect("poller alive");
    let seen = drain_until(&mut rx, |ev| *ev == Seen::Backends, Duration::from_secs(5)).await;
    assert!(
        seen.contains(&Seen::Backends),
        "the catalog has to reach the bus typed: {seen:?}"
    );
    assert_eq!(owner.asked_for("/api/llm/backends").len(), 1);
    // And it did not drag the desk's own beat forward with it. `Refetch::Now`
    // means "poll the desk"; this one means "ask what the backends serve", and
    // a client that conflated them would poll on every palette entry.
    //
    // Waited out rather than read straight after the event: the bus carries the
    // catalog before the poll loop reaches its next request, so an assertion
    // made on arrival would pass whether or not the beat was dragged. The
    // window is a second — long enough that a loop that fell through to the top
    // has issued its poll, and short enough to sit well inside the cadence.
    let window = Duration::from_secs(1);
    drain_until_owner(
        &mut rx,
        || owner.asked_for("/api/tui").len() > polls,
        window,
    )
    .await;
    assert_eq!(
        owner.asked_for("/api/tui").len(),
        polls,
        "asking what the backends serve also polled the desk: {:?}",
        owner.targets()
    );
    assert!(
        window < POLL_INTERVAL,
        "the window has to sit inside the cadence, or the beat itself answers it"
    );
}

#[tokio::test]
async fn a_nudge_and_a_catalog_request_that_arrive_together_are_both_served() {
    // The queue is drained on every wake so that three nudges in flight are one
    // fetch. Draining it *wholesale* would coalesce the two kinds into each
    // other — whichever arrived first would swallow the other, and a palette
    // opened right after `r` would offer an empty catalog with nothing saying
    // why. The coalesce is per kind for that reason.
    // Both orders, because the coalesce has two sides: whichever kind arrives
    // first is the one a wholesale drain would keep.
    for (first, second) in [
        (Refetch::Backends, Refetch::Now),
        (Refetch::Now, Refetch::Backends),
    ] {
        let owner = spawn_owner(vec![
            ready(),
            snapshot_fixture(),
            regime_fixture(),
            backends_fixture(),
        ]);
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let poller = spawn_poller(owner.base.clone(), true, tx);

        drain_until(&mut rx, |ev| *ev == Seen::Snapshot, Duration::from_secs(5)).await;
        let polls = owner.asked_for("/api/tui").len();
        poller.refetch.send(first).expect("poller alive");
        poller.refetch.send(second).expect("poller alive");

        // Inside the cadence, deliberately: waited out to the beat the desk
        // poll arrives on its own, and the nudge assertion would pass on a
        // request that was swallowed.
        let window = Duration::from_secs(1);
        drain_until_owner(
            &mut rx,
            || {
                !owner.asked_for("/api/llm/backends").is_empty()
                    && owner.asked_for("/api/tui").len() > polls
            },
            window,
        )
        .await;
        assert_eq!(
            owner.asked_for("/api/llm/backends").len(),
            1,
            "{first:?} then {second:?}: the catalog request was swallowed: {:?}",
            owner.targets()
        );
        assert!(
            owner.asked_for("/api/tui").len() > polls,
            "{first:?} then {second:?}: the nudge was swallowed: {:?}",
            owner.targets()
        );
        assert!(window < POLL_INTERVAL, "the beat would answer this itself");
    }
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

#[tokio::test]
async fn the_desks_open_question_rides_the_snapshot_beat_and_carries_no_lane() {
    // The ruling this pins, and it is not the obvious one. The board and the
    // news settings are fetched on a *pane entry*; the proposal is not, because
    // its card is mirrored on ATLAS — the view this client opens on, with no
    // entry to hang a first fetch on — and because what retires a proposal
    // happens on the owner's heartbeat rather than in response to anything an
    // operator does here. A card fetched once would go on offering to book a
    // question the desk had already withdrawn.
    //
    // And no `offline` lane: the proposal is a plan, an approval request and a
    // verdict, all registry rows, identical whichever data the desk reads. A
    // query parameter the route does not read would be this client claiming a
    // distinction the owner does not make.
    let owner = spawn_owner(vec![ready(), snapshot_fixture(), proposal_fixture()]);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let _poller = spawn_poller(owner.base.clone(), true, tx);

    let seen = drain(&mut rx, 3, Duration::from_secs(5)).await;
    assert!(
        seen.contains(&Seen::Proposal(true)),
        "the proposal never reached the bus: {seen:?}"
    );
    let targets = owner.targets();
    assert!(
        targets.iter().any(|t| t == "/api/desk/proposal"),
        "{targets:?}"
    );
    assert!(
        !targets.iter().any(|t| t.starts_with("/api/desk/proposal?")),
        "the proposal was asked for with a lane it does not read: {targets:?}"
    );
}

#[tokio::test]
async fn a_desk_with_no_open_question_says_so_on_the_bus() {
    // `{"proposal": null}` is an answer, not a missing one: the card retires a
    // proposal the owner has withdrawn, and it can only do that if the absence
    // reaches it.
    let owner = spawn_owner(vec![
        ready(),
        snapshot_fixture(),
        ("/api/desk/proposal", r#"{"proposal": null}"#.to_string()),
    ]);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let _poller = spawn_poller(owner.base.clone(), true, tx);

    let seen = drain(&mut rx, 3, Duration::from_secs(5)).await;
    assert!(
        seen.contains(&Seen::Proposal(false)),
        "an empty proposal never reached the bus: {seen:?}"
    );
}
