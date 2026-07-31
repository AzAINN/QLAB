//! The `/api/stream` reader: the owner's SSE audit bus turned into `AppEvent`s.
//!
//! The subscription heals itself. Transport failures and owner restarts
//! reconnect *after the last exact event tuple*, so an outage costs a gap in
//! time and not a gap in the record — a plain retry would resubscribe from the
//! primer backlog and silently drop everything past it. That rule and its
//! failure mode are `qlab/tui/client.py:70-134`; this is the same contract read
//! by a second client, so the two cannot disagree about what was seen.
//!
//! The parsing is a pure function over bytes, separate from the socket that
//! produced them. Frames arrive split across reads and interleaved with the
//! owner's `: ping` keepalives, and that reassembly is where the bugs are — it
//! is tested here without a runtime, a port, or a clock.

use crate::bus::{AppEvent, Channel, SseEvent, Tx};
use crate::net::http::PollerHandle;
use crate::net::{because, emit, mark, Gone};
use futures_util::StreamExt;
use serde_json::{json, Map, Value};
use std::time::Duration;

/// Pause between reconnect attempts while the owner is unreachable. The Textual
/// client's `STREAM_RETRY_WAIT_S`: an owner restart takes about this long, and a
/// tighter loop would spend it hammering a socket that is not listening yet.
pub const RETRY: Duration = Duration::from_secs(2);

/// How long a read may find nothing before the subscription is replaced.
///
/// The owner proves liveness with `: ping` whenever its dispatch lock is held
/// past `_STREAM_LOCK_WAIT_SECONDS` (2 s), and every ten seconds while the desk
/// is merely quiet. Silence past this is a dead socket, not a quiet desk.
/// `STREAM_READ_TIMEOUT_S` in the Textual client, and it must stay well above
/// the owner's ping interval or every long action costs a reconnect.
const READ_TIMEOUT: Duration = Duration::from_secs(15);

/// Long enough for a loopback owner that is busy accepting; short enough that a
/// vanished one is reported rather than hung on.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// The largest frame this client will reassemble.
///
/// An owner that writes an unterminated line must not be able to grow this
/// process without bound. Registry event payloads are kilobytes; a megabyte is
/// already evidence the contract broke, and invariant 4 says say so rather than
/// keep swallowing.
const MAX_FRAME: usize = 1 << 20;

/// How much of a frame that could not be read travels with the complaint.
/// The Python client's `payload[:120]` — enough to recognise, short enough to
/// render in a panel.
const RAW_PREVIEW: usize = 120;

/// The kind a frame gets when it could not be read at all.
const MALFORMED: &str = "stream.malformed";

/// Events whose arrival means the aggregate snapshot is already out of date.
///
/// Plan Part III (adaptive polling): the poll cadence is a floor on how stale
/// the desk may look, and these are the moments where waiting it out is visibly
/// wrong — a plan the operator just executed, a phase that just advanced, an
/// approval that just appeared. Each nudges the poller instead of the client
/// inventing the new state from the event, so the snapshot stays the single
/// account of the desk.
///
/// `quote` is deliberately absent: it arrives on its own beat and is rendered
/// from the overlay, so listing it here would turn the ticker into a poll
/// amplifier.
pub const REFETCH_KINDS: &[&str] = &[
    "workflow_started",
    "workflow_phase",
    "workflow_interrupted",
    "workflow_resumed",
    "workflow_abandoned",
    "plan_checked",
    "plan_executed",
    "approval_created",
    "approval_challenged",
    "approval_approved",
    "approval_rejected",
    "approval_consumed",
    "halt",
    "resume",
    "atlas_mode",
    "referee_verdict",
];

/// The resume point: the exact `(ts, event_id)` pair of the last event seen.
///
/// Both halves, never just the timestamp — the owner resumes strictly after the
/// pair, and a timestamp alone re-delivers every event that shares it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cursor {
    pub after: String,
    pub after_id: String,
}

/// The reassembly buffer and the cursor, together.
///
/// They live in one value because they have different lifetimes on purpose: the
/// partial frame belongs to one connection and dies with it, the cursor crosses
/// the outage. Keeping them apart is how a reconnect loses one or fuses the
/// other onto the next connection's first bytes.
#[derive(Debug, Default)]
pub struct SseBuf {
    pending: Vec<u8>,
    cursor: Option<Cursor>,
    /// Discarding the tail of a frame already refused, so one over-long frame
    /// is one complaint rather than one per read.
    overflowed: bool,
}

impl SseBuf {
    pub fn cursor(&self) -> Option<&Cursor> {
        self.cursor.as_ref()
    }

    /// A new connection never inherits a half-frame from the dead one.
    pub fn reconnect(&mut self) {
        self.pending.clear();
        self.overflowed = false;
    }
}

/// Turn whatever bytes arrived into whole events. Pure: no clock, no socket.
///
/// Line-oriented rather than frame-oriented, matching the owner's writer and the
/// Textual client's reader: one `data:` line per event, `: ping` comments and
/// blank lines between them, and no guarantee that a read ends on any of it.
pub fn feed(buf: &mut SseBuf, bytes: &[u8]) -> Vec<SseEvent> {
    let SseBuf {
        pending,
        cursor,
        overflowed,
    } = buf;
    pending.extend_from_slice(bytes);

    let mut out = Vec::new();
    let mut start = 0;
    while let Some(rel) = pending[start..].iter().position(|b| *b == b'\n') {
        let line = &pending[start..start + rel];
        start += rel + 1;
        if *overflowed {
            // The remainder of a frame that was already refused. Its newline is
            // the resync point, not the start of anything readable.
            *overflowed = false;
            continue;
        }
        if let Some(event) = read_line(cursor, line) {
            out.push(event);
        }
    }
    pending.drain(..start);

    if *overflowed {
        pending.clear(); // still discarding, and not while growing
    } else if pending.len() > MAX_FRAME {
        out.push(malformed(&String::from_utf8_lossy(
            &pending[..RAW_PREVIEW.min(pending.len())],
        )));
        pending.clear();
        *overflowed = true;
    }
    out
}

/// One line of the stream. `None` for everything that is not a frame.
fn read_line(cursor: &mut Option<Cursor>, line: &[u8]) -> Option<SseEvent> {
    let line = line.strip_suffix(b"\r").unwrap_or(line);
    // Comments (`: ping`), blank lines, and any SSE field other than `data` are
    // not events. The owner writes exactly one `data:` line per event.
    let body = line.strip_prefix(b"data:")?;
    // Lossy on purpose: a `data:` line that is not UTF-8 is a broken frame the
    // desk has to hear about, and the decode below is what says so. Refusing to
    // read it here would drop it silently instead.
    let text = String::from_utf8_lossy(body);
    let raw = text.trim();
    if raw.is_empty() {
        return None;
    }
    Some(decode(cursor, raw))
}

/// One frame's JSON, and the cursor it moves.
fn decode(cursor: &mut Option<Cursor>, raw: &str) -> SseEvent {
    let Ok(Value::Object(event)) = serde_json::from_str::<Value>(raw) else {
        // Parity with the Python client: a frame that is not an object is
        // surfaced, never swallowed and never a reason to tear the stream down.
        return malformed(raw);
    };

    let ts = cursor_half(&event, "ts");
    let id = cursor_half(&event, "event_id");
    // Advanced before anything can reject the event, and only when both halves
    // are there. A row this client cannot read must not wedge the resume point
    // and replay forever; a transient one with no id must not advance past the
    // durable events it was interleaved with.
    if let (Some(after), Some(after_id)) = (&ts, &id) {
        *cursor = Some(Cursor {
            after: after.clone(),
            after_id: after_id.clone(),
        });
    }

    let Some(kind) = event
        .get("kind")
        .and_then(Value::as_str)
        .filter(|kind| !kind.is_empty())
    else {
        return malformed(raw);
    };
    SseEvent {
        kind: kind.to_string(),
        // The event's own payload, not the envelope: `kind`, `ts`, and
        // `event_id` are already fields here, and nesting them again would make
        // every reader downstream unwrap twice.
        payload: event.get("payload").cloned().unwrap_or(Value::Null),
        ts,
        id,
    }
}

/// One half of the resume pair. `event_id` is a `VARCHAR` uuid in the registry,
/// but an ordinal would still order, so a number reads as its own text rather
/// than as nothing.
fn cursor_half(event: &Map<String, Value>, key: &str) -> Option<String> {
    match event.get(key)? {
        Value::String(text) if !text.is_empty() => Some(text.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn malformed(raw: &str) -> SseEvent {
    SseEvent {
        kind: MALFORMED.to_string(),
        payload: json!({ "raw": head(raw) }),
        ts: None,
        id: None,
    }
}

/// The first `RAW_PREVIEW` characters, cut on a character rather than a byte.
fn head(raw: &str) -> String {
    match raw.char_indices().nth(RAW_PREVIEW) {
        Some((cut, _)) => raw[..cut].to_string(),
        None => raw.to_string(),
    }
}

// -- the task --------------------------------------------------------------

/// Subscribe to the owner's stream. Every frame arrives on `tx` as an
/// `AppEvent::Sse`; the ones that change the desk also nudge `poller`.
pub fn spawn_sse(base: String, tx: Tx, poller: PollerHandle) {
    tokio::spawn(async move {
        // The only exit is a closed bus, which means the loop this was feeding
        // is gone: there is no failure here worth reporting to nobody.
        let _ = stream_loop(base, tx, poller).await;
    });
}

async fn stream_loop(base: String, tx: Tx, poller: PollerHandle) -> Result<(), Gone> {
    let client = match build_client() {
        Ok(client) => client,
        Err(err) => {
            tracing::error!(%err, "could not build the owner stream client");
            return emit(&tx, AppEvent::ConnDown(Channel::Stream));
        }
    };
    let url = format!("{}/api/stream", base.trim_end_matches('/'));
    let mut buf = SseBuf::default();
    let mut up: Option<bool> = None;

    loop {
        // The cursor is the only thing that crosses the gap. Whatever half of a
        // frame the dead connection left behind belonged to it.
        buf.reconnect();
        subscribe(&client, &url, &tx, &poller, &mut buf, &mut up).await?;
        tokio::time::sleep(RETRY).await;
    }
}

/// One subscription, from connect to the socket going away.
async fn subscribe(
    client: &reqwest::Client,
    url: &str,
    tx: &Tx,
    poller: &PollerHandle,
    buf: &mut SseBuf,
    up: &mut Option<bool>,
) -> Result<(), Gone> {
    let mut request = client.get(url);
    if let Some(cursor) = buf.cursor() {
        // Encoded by the query serialiser, never formatted into the url: an ISO
        // timestamp carries `+00:00`, and a raw `+` in a query decodes to a
        // space on the owner — a cursor silently off by the whole offset.
        request = request.query(&[
            ("after", cursor.after.as_str()),
            ("after_id", cursor.after_id.as_str()),
        ]);
    }

    // Every failure below reports through `mark`, which writes the reason once
    // per outage rather than once per retry. This loop reconnects every two
    // seconds forever against a dead owner, and a line per attempt buried what
    // actually broke under an hour of identical sentences.
    let resp = match request.send().await {
        Ok(resp) => resp,
        Err(err) => {
            return mark(
                tx,
                Channel::Stream,
                up,
                false,
                &format!("unreachable: {}", because(&err)),
            );
        }
    };
    if !resp.status().is_success() {
        return mark(
            tx,
            Channel::Stream,
            up,
            false,
            &format!(
                "the owner refused the stream with {}",
                resp.status().as_u16()
            ),
        );
    }
    // An answer is not a stream. An owner serving something else on this route
    // would otherwise hold a green STREAM chip over a subscription that can
    // never deliver an event.
    let content_type = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_string();
    if !content_type.starts_with("text/event-stream") {
        return mark(
            tx,
            Channel::Stream,
            up,
            false,
            &format!("the owner answered the stream route with {content_type}"),
        );
    }

    mark(tx, Channel::Stream, up, true, "")?;
    let mut body = resp.bytes_stream();
    // Why this subscription ended, for the one line `mark` writes about it. A
    // body that simply runs out is the owner closing the stream, which is a
    // different thing from a read deadline or a dropped socket.
    let mut ended = "the owner closed the stream".to_string();
    while let Some(chunk) = body.next().await {
        let chunk = match chunk {
            Ok(chunk) => chunk,
            Err(err) => {
                // A read deadline or a dropped socket. Both mean the same thing
                // to the desk, and both are resumable from the cursor.
                ended = because(&err);
                break;
            }
        };
        for event in feed(buf, &chunk) {
            // Plan Part III: the snapshot is stale the moment one of these
            // lands, so the poll is brought forward rather than waited out.
            if REFETCH_KINDS.contains(&event.kind.as_str()) {
                poller.now();
            }
            tracing::debug!(kind = %event.kind, "stream event");
            emit(tx, AppEvent::Sse(event))?;
        }
    }
    mark(tx, Channel::Stream, up, false, &ended)
}

fn build_client() -> reqwest::Result<reqwest::Client> {
    // No total request timeout: this request is meant to stay open for hours.
    // The poller's 8 s deadline here would tear the subscription down mid-desk
    // and turn a healthy stream into a reconnect loop.
    reqwest::Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .read_timeout(READ_TIMEOUT)
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One event as the owner writes it: `{event_id, ts, kind, payload}`.
    const QUOTE: &str = concat!(
        r#"data: {"event_id":"e1","kind":"quote","payload":{"rows":[]},"#,
        r#""ts":"2026-07-30T12:00:00+00:00"}"#,
        "\n\n"
    );

    /// What a parser case asserts: what each chunk yielded, the kinds in order,
    /// and where the resume cursor ended up.
    struct Case {
        name: &'static str,
        chunks: &'static [&'static str],
        /// Events produced per chunk — the half that pins *when* a frame
        /// completes, which a flat total would hide.
        yields: &'static [usize],
        kinds: &'static [&'static str],
        cursor: Option<(&'static str, &'static str)>,
    }

    const CASES: &[Case] = &[
        Case {
            name: "one whole frame",
            chunks: &[QUOTE],
            yields: &[1],
            kinds: &["quote"],
            cursor: Some(("2026-07-30T12:00:00+00:00", "e1")),
        },
        Case {
            // The owner's keepalive while its dispatch lock is held. A client
            // that treated one as data would emit an event per two seconds of
            // owner silence.
            name: "a ping-only chunk is not an event",
            chunks: &[": ping\n\n"],
            yields: &[0],
            kinds: &[],
            cursor: None,
        },
        Case {
            name: "a frame split across reads reassembles",
            chunks: &[
                r#"data: {"event_id":"e2","kind":"halt","#,
                "\"payload\":{\"why\":\"drawdown\"},\"ts\":\"t2\"}\n\n",
            ],
            yields: &[0, 1],
            kinds: &["halt"],
            cursor: Some(("t2", "e2")),
        },
        Case {
            name: "a frame split mid-newline still terminates once",
            chunks: &[
                "data: {\"event_id\":\"e3\",\"kind\":\"resume\",\"ts\":\"t3\"}\r",
                "\n\n",
            ],
            yields: &[0, 1],
            kinds: &["resume"],
            cursor: Some(("t3", "e3")),
        },
        Case {
            // Parity with the Python client: a bad frame is said out loud and
            // the subscription lives on. Dropping it silently is the one option
            // that leaves the desk unable to know it happened.
            name: "bad json is stream.malformed, not silence",
            chunks: &["data: {not json at all}\n\n"],
            yields: &[1],
            kinds: &["stream.malformed"],
            cursor: None,
        },
        Case {
            name: "a frame that is not an object is malformed too",
            chunks: &["data: [1, 2, 3]\n\n"],
            yields: &[1],
            kinds: &["stream.malformed"],
            cursor: None,
        },
        Case {
            name: "an event with no kind is malformed",
            chunks: &["data: {\"event_id\":\"e4\",\"ts\":\"t4\"}\n\n"],
            yields: &[1],
            kinds: &["stream.malformed"],
            // The cursor still moves: a row this client cannot read must not
            // wedge the resume point and replay forever.
            cursor: Some(("t4", "e4")),
        },
        Case {
            name: "several events in one chunk all arrive",
            chunks: &[concat!(
                "data: {\"event_id\":\"a\",\"kind\":\"workflow_started\",\"ts\":\"t1\"}\n\n",
                ": ping\n\n",
                "data: {\"event_id\":\"b\",\"kind\":\"workflow_phase\",\"ts\":\"t2\"}\n\n",
                "data: {\"event_id\":\"c\",\"kind\":\"referee_verdict\",\"ts\":\"t3\"}\n\n",
            )],
            yields: &[3],
            kinds: &["workflow_started", "workflow_phase", "referee_verdict"],
            cursor: Some(("t3", "c")),
        },
        Case {
            // Only a durable event carries a resume point. One that does not
            // must leave the cursor where it was, or the reconnect asks to
            // resume from nothing and replays the primer backlog.
            name: "the cursor advances only on events that carry ts and id",
            chunks: &[
                "data: {\"event_id\":\"d1\",\"kind\":\"halt\",\"ts\":\"t9\"}\n\n",
                "data: {\"kind\":\"atlas_mode\",\"payload\":{\"mode\":\"research\"}}\n\n",
                "data: {\"event_id\":\"d2\",\"kind\":\"resume\"}\n\n",
            ],
            yields: &[1, 1, 1],
            kinds: &["halt", "atlas_mode", "resume"],
            cursor: Some(("t9", "d1")),
        },
        Case {
            name: "blank lines and unknown fields are not frames",
            chunks: &["\n", "event: message\n", "id: 7\n", ": ping\n", "\n"],
            yields: &[0, 0, 0, 0, 0],
            kinds: &[],
            cursor: None,
        },
    ];

    #[test]
    fn the_parser_reads_the_owners_frames() {
        for case in CASES {
            let mut buf = SseBuf::default();
            let mut kinds = Vec::new();
            assert_eq!(
                case.chunks.len(),
                case.yields.len(),
                "{}: every chunk needs an expectation",
                case.name
            );
            for (chunk, want) in case.chunks.iter().zip(case.yields) {
                let events = feed(&mut buf, chunk.as_bytes());
                assert_eq!(events.len(), *want, "{}: chunk {chunk:?}", case.name);
                kinds.extend(events.into_iter().map(|e| e.kind));
            }
            assert_eq!(kinds, case.kinds, "{}", case.name);
            assert_eq!(
                buf.cursor()
                    .map(|c| (c.after.as_str(), c.after_id.as_str())),
                case.cursor,
                "{}: resume point",
                case.name
            );
        }
    }

    #[test]
    fn a_malformed_frame_carries_what_could_not_be_read() {
        let mut buf = SseBuf::default();
        let events = feed(&mut buf, b"data: {oops\n\n");
        let bad = events.first().expect("the bad frame has to reach the bus");
        assert_eq!(bad.kind, "stream.malformed");
        assert_eq!(bad.payload["raw"], "{oops");
        assert!(bad.ts.is_none() && bad.id.is_none());
    }

    #[test]
    fn a_reconnect_keeps_the_cursor_and_drops_the_half_frame() {
        // The two halves of this belong to different connections. Gluing them
        // would hand the desk one invented event; losing the cursor would
        // resubscribe from the primer and silently drop everything past it.
        let mut buf = SseBuf::default();
        feed(&mut buf, QUOTE.as_bytes());
        feed(&mut buf, b"data: {\"kind\":\"halt\",");
        buf.reconnect();
        let events = feed(
            &mut buf,
            b"\"event_id\":\"e9\",\"ts\":\"t9\",\"payload\":{}}\n\n",
        );
        assert!(
            events.is_empty(),
            "the tail of a dead connection is not an event: {:?}",
            events.iter().map(|e| &e.kind).collect::<Vec<_>>()
        );
        assert_eq!(
            buf.cursor().map(|c| c.after_id.as_str()),
            Some("e1"),
            "the resume point survives the outage"
        );
    }

    #[test]
    fn a_frame_that_never_ends_is_refused_rather_than_grown() {
        // An owner that writes an unterminated line forever must not be able to
        // grow this client's memory without bound, and must not do it quietly.
        let mut buf = SseBuf::default();
        let mut events = Vec::new();
        for _ in 0..12 {
            events.extend(feed(&mut buf, "data: {\"pad\":\"".as_bytes()));
            events.extend(feed(&mut buf, "x".repeat(100_000).as_bytes()));
        }
        assert_eq!(
            events
                .iter()
                .filter(|e| e.kind == "stream.malformed")
                .count(),
            1,
            "one refusal for one over-long frame, not one per chunk"
        );
        // And the stream resyncs: the next whole frame after the junk arrives.
        let events = feed(&mut buf, format!("\"}}\n\n{QUOTE}").as_bytes());
        assert_eq!(
            events.iter().map(|e| e.kind.as_str()).collect::<Vec<_>>(),
            vec!["quote"],
            "the tail of the refused frame is discarded, the next one is read"
        );
    }

    #[test]
    fn the_refetch_kinds_are_owner_facts_the_snapshot_shows() {
        // Plan Part III: these are the kinds whose arrival means the aggregate
        // snapshot is already out of date. A kind listed here that the owner
        // never emits is a nudge that never fires; `quote` listed here would
        // turn the ticker into a poll amplifier.
        assert!(REFETCH_KINDS.contains(&"plan_executed"));
        assert!(REFETCH_KINDS.contains(&"workflow_phase"));
        assert!(REFETCH_KINDS.contains(&"approval_created"));
        assert!(!REFETCH_KINDS.contains(&"quote"));
        assert!(!REFETCH_KINDS.contains(&"stream.malformed"));
        let mut sorted = REFETCH_KINDS.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), REFETCH_KINDS.len(), "a kind is listed twice");
    }
}
