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
use crate::model::{
    LlmCatalog, MethodSettings, NewsSettings, PredictorDetail, ProposalPayload, QualitativeMatrix,
    RegimePanel, Snapshot, Templates, Visual, VisualAnswer, VisualError, VisualResult, VisualsList,
};
use crate::net::{because, emit, mark, Gone};
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

/// The qualitative matrix is a reading of the owner's news window, which the
/// owner's own heartbeat refreshes — so it moves without anybody here touching
/// a key, and it is the one research payload that owes a beat rather than a
/// pane entry. Slower than the panel because a news window turns over in
/// minutes, not in seconds, and every fetch costs the owner a build over the
/// whole universe under its dispatch lock.
pub const QUALITATIVE_INTERVAL: Duration = Duration::from_secs(60);

/// The registered workflow templates change when the owner is *deployed*, not
/// when the desk moves — `qlab/operator/templates.py` is a module-level table —
/// so this is the slowest beat on the boundary. It exists at all rather than
/// being fetched once because an operator restarts the owner under a running
/// client, and a picker that had cached the old set would offer a template the
/// owner no longer registers.
pub const TEMPLATE_INTERVAL: Duration = Duration::from_secs(60);

/// How often a client with no owner asks again. Faster than the poll cadence
/// because the operator is usually starting the owner while watching this.
///
/// Public beside `POLL_INTERVAL` because the gap between the two is what the
/// `reachable` split is measured by: a test that restated either number would be
/// free to disagree with the poller about which loop it was watching.
pub const READY_RETRY: Duration = Duration::from_secs(2);

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
/// Three variants, and still not a fetch path anything can aim anywhere: each
/// names a request this module already knows how to build, so the handle can
/// choose *which* of the owner's routes is asked and never *what* is asked for.
///
/// `Backends` is the one payload with no beat behind it. The route probes every
/// backend, so polling it would move a network round trip per daemon onto a
/// cadence — which is precisely the cost the owner refuses to pay on
/// `/api/tui`. It is asked for when the palette enters the model scope and at
/// no other time.
/// `Clone` and not `Copy`: [`Refetch::Visual`] names *which* drawing was asked
/// for, which is the one request on this boundary whose target the operator
/// chooses. It is still a closed set of routes — the name goes into a path
/// segment this module builds and percent-encodes, never into a url a caller
/// hands over.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refetch {
    Now,
    Backends,
    /// The full predictor board. No beat behind it either, for the opposite
    /// reason to `Backends`: the route is one registry read, but the board it
    /// reads changes when a run lands — days apart — so a cadence would be
    /// almost entirely re-fetching an answer the client already holds. It is
    /// asked for when the PREDICTORS view opens and when `r` is pressed there.
    Predictors,
    /// What the desk reads the news from. No beat behind it for the same
    /// reason as the board: it changes when an operator changes it, so a
    /// cadence would spend a request per poll re-reading an answer this client
    /// already holds. Asked for when SETTINGS opens, when `r` is pressed
    /// there, and after the pane's own POST.
    News,
    /// Which method the desk solves with, and its holdings cap. No beat behind
    /// it for the news answer's reason — it changes when an operator changes
    /// it — and asked for at the same three moments, the last of which is the
    /// METHOD card's own POST: the owner recomputes the cap warning as it
    /// merges the override, so the answer to the write is the only thing that
    /// can say what the desk is now holding to.
    Method,
    /// What the owner can draw. No beat behind it, and the strongest version
    /// of the board's reason: the registry is a walk over the owner's own
    /// `qlab/visuals/` package, so it changes when the owner is *deployed*.
    /// Asked for when the VISUALS view opens and when `r` is pressed there.
    Visuals,
    /// One visual, rendered. The only request here whose target the operator
    /// picks, and it rides a keystroke rather than any beat: the owner does
    /// the drawing per request, and nothing about it changes until somebody
    /// asks for a different one.
    Visual(String),
}

/// The runtime's end of the poller.
///
/// Clonable because two callers hold one: the frame loop's `r` key and the SSE
/// task, which brings the next poll forward when an event says the snapshot is
/// already out of date. Cloning cannot widen what the handle can ask for —
/// `Refetch` names a closed set of requests.
#[derive(Clone)]
pub struct PollerHandle {
    pub refetch: UnboundedSender<Refetch>,
}

impl PollerHandle {
    /// Jump the poll queue. A poller that has already stopped is not something
    /// the frame loop can act on — it is on its way out too.
    pub fn now(&self) {
        let _ = self.refetch.send(Refetch::Now);
    }

    /// Ask what the owner's backends serve, once. Not a poll and not a nudge:
    /// it fetches the one payload the desk cadence deliberately never carries,
    /// and leaves the snapshot's own beat where it was.
    pub fn backends(&self) {
        let _ = self.refetch.send(Refetch::Backends);
    }

    /// Ask for the full predictor board, once. Same shape as `backends`: an
    /// action's fetch, not a beat's, and the snapshot cadence stays where it
    /// was.
    pub fn predictors(&self) {
        let _ = self.refetch.send(Refetch::Predictors);
    }

    /// Ask what the desk reads the news from, once. Same shape again.
    pub fn news(&self) {
        let _ = self.refetch.send(Refetch::News);
    }

    /// Ask which method the desk solves with, once. Same shape again.
    pub fn method(&self) {
        let _ = self.refetch.send(Refetch::Method);
    }

    /// Ask what the owner can draw, once. Same shape again.
    pub fn visuals(&self) {
        let _ = self.refetch.send(Refetch::Visuals);
    }

    /// Ask the owner to render one visual. The only request on this handle
    /// that carries a value, and it is a *name* out of the list the owner
    /// itself served — never a path and never a url.
    pub fn visual(&self, name: &str) {
        let _ = self.refetch.send(Refetch::Visual(name.to_string()));
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
    // No `offline` lane: the template registry is a table in the owner's own
    // module, identical in both lanes, and a query parameter the route does not
    // read would be this client claiming a distinction the owner does not make.
    let templates_url = format!("{base}/api/atlas/templates");
    // No `offline` lane either, and no `refresh=1`: the owner's own five-second
    // cache is what keeps a palette opened twice from probing twice, and asking
    // it to bypass that would make every scope entry a round trip per daemon.
    let backends_url = format!("{base}/api/llm/backends");
    // No lane here either: the board is a registry row, identical in both.
    let predictors_url = format!("{base}/api/research/predictors");
    // The lane, because the route reads it: `stack` and `lane` both follow the
    // flag, and an offline desk resolves `synthetic` whatever is configured.
    // Asking without it would let the owner answer about a desk mode this
    // window is not pointed at.
    let news_url = format!("{base}/api/news/settings?offline={lane}");
    // No lane: the mandate and the algorithm catalog are the same in both, and
    // a query parameter the route does not read would be this client claiming a
    // distinction the owner does not make.
    let method_url = format!("{base}/api/desk/method");
    // The lane, because the route reads it and the answer differs: an offline
    // desk reads the synthetic feed, and a matrix fetched without the flag
    // would be a reading of a window this client is not pointed at.
    let qualitative_url = format!("{base}/api/research/qualitative?offline={lane}");
    // No lane: the proposal is a plan, an approval request and a verdict, all
    // registry rows, identical whichever data the desk reads.
    let proposal_url = format!("{base}/api/desk/proposal");
    // No lane: what the owner can draw is a walk over its own package, and the
    // drawing itself is a pure function of the params — neither reads the feed.
    let visuals_url = format!("{base}/api/visuals");

    // Two facts, not one. `up` is what the chips read — a payload this client
    // could actually use — and `reachable` is what the socket said.
    //
    // They were one field, and an owner serving a payload the model cannot read
    // left it unset: every cycle then paid a readiness probe *and* a full
    // `/api/tui` at the retry cadence, three times the request rate of a healthy
    // desk, aimed at the owner that is already struggling. A reachable owner
    // polls at the desk's own cadence whether or not this client can read what
    // it serves — the frame already says loudly that it cannot.
    let mut up: Option<bool> = None;
    let mut reachable: Option<bool> = None;
    // Due immediately, then on their own slow beats.
    let mut regime_due = Instant::now();
    let mut templates_due = Instant::now();
    let mut qualitative_due = Instant::now();

    loop {
        // The readiness gate, only while the owner is not known to be answering.
        // A desk that is up does not need a second request per poll to prove it.
        let ready = reachable == Some(true) || {
            match probe(&client, &base).await {
                Readiness::Ready => {
                    reachable = Some(true);
                    true
                }
                Readiness::Unreachable(why) => {
                    reachable = Some(false);
                    mark(&tx, Channel::Owner, &mut up, false, &why)?;
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
                    mark(&tx, Channel::Owner, &mut up, true, "")?;
                    emit(&tx, AppEvent::Snapshot(Box::new(snapshot)))?;
                }
                // Fail loud, and on screen. A payload the model cannot read is a
                // broken contract with the owner, never a frame to skip — and
                // the owner *answered*, so `reachable` stands and the cadence
                // stays the desk's.
                Fetched::Malformed(error) => emit(
                    &tx,
                    AppEvent::Http(HttpResult::Malformed {
                        url: snapshot_url.clone(),
                        error,
                    }),
                )?,
                Fetched::Failed(error) => {
                    // The reason goes to the log rather than to the screen: the
                    // status chip says the owner is down, the content area names
                    // the remedy, and the toast carries the distinction between
                    // "connection refused" and "404".
                    reachable = Some(false);
                    mark(&tx, Channel::Owner, &mut up, false, &error)?;
                }
            }

            // The desk's single open question, on the snapshot's own beat
            // rather than on a pane entry or a beat of its own.
            //
            // The card is mirrored on ATLAS, which is the view this client
            // opens on, so there is no entry to hang a first fetch on. And what
            // makes a proposal stop being the proposal — a newer plan
            // superseding it, an expiry, an orphan withdrawn — happens on the
            // *owner's* heartbeat, with nothing here to prompt a refetch: a
            // card fetched once would go on offering to book a question the
            // desk had already withdrawn. One registry read per poll beside a
            // `/api/tui` that costs far more, and `Refetch::Now` — the `r` key
            // and every write outcome — brings it forward with the snapshot,
            // which is why this owes no `Refetch` variant of its own.
            if up == Some(true) {
                match fetch::<ProposalPayload>(&client, &proposal_url).await {
                    Fetched::Decoded(payload) => {
                        emit(&tx, AppEvent::Proposal(payload.proposal.map(Box::new)))?
                    }
                    Fetched::Malformed(error) => emit(
                        &tx,
                        AppEvent::Http(HttpResult::Malformed {
                            url: proposal_url.clone(),
                            error,
                        }),
                    )?,
                    // A question the desk is asking is not whether the desk is
                    // there: a failure here must not tell the operator the
                    // owner went away when the snapshot that decides that is
                    // still arriving. Same reasoning as the panel and the
                    // templates — and the card says for itself that it holds
                    // no proposal.
                    Fetched::Failed(error) => {
                        tracing::warn!(%error, "desk proposal poll failed")
                    }
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

            if up == Some(true) && Instant::now() >= templates_due {
                templates_due = Instant::now() + TEMPLATE_INTERVAL;
                match fetch::<Templates>(&client, &templates_url).await {
                    Fetched::Decoded(payload) => emit(&tx, AppEvent::Templates(payload.templates))?,
                    Fetched::Malformed(error) => emit(
                        &tx,
                        AppEvent::Http(HttpResult::Malformed {
                            url: templates_url.clone(),
                            error,
                        }),
                    )?,
                    // The template set is what the desk can be *asked* for, not
                    // what it is: a failure here must not tell the operator the
                    // owner went away when the snapshot that decides that is
                    // still arriving. Same reasoning as the panel above.
                    Fetched::Failed(error) => {
                        tracing::warn!(%error, "workflow template poll failed")
                    }
                }
            }

            if up == Some(true) && Instant::now() >= qualitative_due {
                qualitative_due = Instant::now() + QUALITATIVE_INTERVAL;
                match fetch::<QualitativeMatrix>(&client, &qualitative_url).await {
                    Fetched::Decoded(matrix) => emit(&tx, AppEvent::Qualitative(Box::new(matrix)))?,
                    Fetched::Malformed(error) => emit(
                        &tx,
                        AppEvent::Http(HttpResult::Malformed {
                            url: qualitative_url.clone(),
                            error,
                        }),
                    )?,
                    // The record is evidence, not the desk: a failure here must
                    // not tell the operator the owner went away when the
                    // snapshot that decides that is still arriving. Same
                    // reasoning as the panel and the templates — and the pane
                    // says for itself that it holds no matrix.
                    Fetched::Failed(error) => {
                        tracing::warn!(%error, "qualitative matrix poll failed")
                    }
                }
            }
        }

        // Keyed on reachability, not readability. The retry cadence exists for an
        // owner that is not there yet — the operator is usually starting one
        // while watching this — and an owner that is answering is not that,
        // whatever it is answering with.
        let delay = if reachable == Some(true) {
            POLL_INTERVAL
        } else {
            READY_RETRY
        };
        // A deadline rather than a sleep, because the two requests the handle
        // can ask for mean different things to the beat. `Now` *is* the next
        // poll brought forward; a catalog request is a different route
        // entirely, and serving it by falling out of the wait would make every
        // palette entry also poll the desk — a hidden second meaning for a key
        // that only asked what the backends serve.
        let due = Instant::now() + delay;
        loop {
            let left = due.saturating_duration_since(Instant::now());
            tokio::select! {
                _ = tokio::time::sleep(left) => break,
                cmd = refetch.recv() => match cmd {
                    Some(first) => {
                        // Coalesced the way the frame loop coalesces events —
                        // three nudges that arrived while a fetch was in flight
                        // are one fetch — but coalesced *by kind*: draining the
                        // queue wholesale would swallow a catalog request that
                        // happened to arrive behind a nudge.
                        //
                        // The render is coalesced to the *last* name asked
                        // for rather than to a flag: an operator walking the
                        // list with Enter means the drawing they stopped on,
                        // and rendering every name they passed through would
                        // finish on whichever answer happened to land last.
                        let mut jump = first == Refetch::Now;
                        let mut catalog = first == Refetch::Backends;
                        let mut predictors = first == Refetch::Predictors;
                        let mut news = first == Refetch::News;
                        let mut method = first == Refetch::Method;
                        let mut visuals = first == Refetch::Visuals;
                        let mut visual = match &first {
                            Refetch::Visual(name) => Some(name.clone()),
                            _ => None,
                        };
                        while let Ok(next) = refetch.try_recv() {
                            jump |= next == Refetch::Now;
                            catalog |= next == Refetch::Backends;
                            predictors |= next == Refetch::Predictors;
                            news |= next == Refetch::News;
                            method |= next == Refetch::Method;
                            visuals |= next == Refetch::Visuals;
                            if let Refetch::Visual(name) = next {
                                visual = Some(name);
                            }
                        }
                        if catalog {
                            match fetch::<LlmCatalog>(&client, &backends_url).await {
                                Fetched::Decoded(payload) => {
                                    emit(&tx, AppEvent::Backends(payload))?
                                }
                                Fetched::Malformed(error) => emit(
                                    &tx,
                                    AppEvent::Http(HttpResult::Malformed {
                                        url: backends_url.clone(),
                                        error,
                                    }),
                                )?,
                                // What the backends serve is not whether the
                                // desk is there: a failure here must not tell
                                // the operator the owner went away when the
                                // snapshot that decides that is still arriving.
                                // Same reasoning as the panel and the templates.
                                Fetched::Failed(error) => {
                                    tracing::warn!(%error, "model backend catalog fetch failed")
                                }
                            }
                        }
                        if predictors {
                            match fetch::<PredictorDetail>(&client, &predictors_url).await {
                                Fetched::Decoded(payload) => {
                                    emit(&tx, AppEvent::PredictorDetail(Box::new(payload)))?
                                }
                                Fetched::Malformed(error) => emit(
                                    &tx,
                                    AppEvent::Http(HttpResult::Malformed {
                                        url: predictors_url.clone(),
                                        error,
                                    }),
                                )?,
                                // The board is evidence, not the desk: a
                                // failure here must not tell the operator the
                                // owner went away when the snapshot that
                                // decides that is still arriving. Same
                                // reasoning as the panel and the templates.
                                Fetched::Failed(error) => {
                                    tracing::warn!(%error, "predictor board fetch failed")
                                }
                            }
                        }
                        if news {
                            match fetch::<NewsSettings>(&client, &news_url).await {
                                Fetched::Decoded(payload) => {
                                    emit(&tx, AppEvent::News(Box::new(payload)))?
                                }
                                Fetched::Malformed(error) => emit(
                                    &tx,
                                    AppEvent::Http(HttpResult::Malformed {
                                        url: news_url.clone(),
                                        error,
                                    }),
                                )?,
                                // What the desk reads is not whether the desk
                                // is there: a failure here must not tell the
                                // operator the owner went away when the
                                // snapshot that decides that is still
                                // arriving. Same reasoning as the panel, the
                                // templates and the board.
                                Fetched::Failed(error) => {
                                    tracing::warn!(%error, "news settings fetch failed")
                                }
                            }
                        }
                        if method {
                            match fetch::<MethodSettings>(&client, &method_url).await {
                                Fetched::Decoded(payload) => {
                                    emit(&tx, AppEvent::Method(Box::new(payload)))?
                                }
                                Fetched::Malformed(error) => emit(
                                    &tx,
                                    AppEvent::Http(HttpResult::Malformed {
                                        url: method_url.clone(),
                                        error,
                                    }),
                                )?,
                                // How the desk solves is not whether the desk
                                // is there, and the same reasoning again: a
                                // failure here must not tell the operator the
                                // owner went away when the snapshot that
                                // decides that is still arriving.
                                Fetched::Failed(error) => {
                                    tracing::warn!(%error, "desk method fetch failed")
                                }
                            }
                        }
                        if visuals {
                            match fetch::<VisualsList>(&client, &visuals_url).await {
                                Fetched::Decoded(payload) => {
                                    emit(&tx, AppEvent::Visuals(payload.visuals))?
                                }
                                Fetched::Malformed(error) => emit(
                                    &tx,
                                    AppEvent::Http(HttpResult::Malformed {
                                        url: visuals_url.clone(),
                                        error,
                                    }),
                                )?,
                                // What the owner can draw is not whether the
                                // desk is there. Same reasoning as the panel,
                                // the templates and the board.
                                Fetched::Failed(error) => {
                                    tracing::warn!(%error, "visuals list fetch failed")
                                }
                            }
                        }
                        if let Some(name) = visual {
                            let url = visual_url(&base, &name);
                            let rendered = render_visual(&client, &url).await;
                            // The chip first, because it is about the owner and
                            // the answer below is about one drawing.
                            if let Some(error) = rendered.malformed {
                                emit(&tx, AppEvent::Http(HttpResult::Malformed { url, error }))?;
                            }
                            // And always an answer. This emit is unconditional
                            // on purpose: the two arms that used to return
                            // without one left the pane waiting on a reply that
                            // was never coming, with only a log line to say so.
                            emit(
                                &tx,
                                AppEvent::Visual(Box::new(VisualAnswer {
                                    asked: name,
                                    result: rendered.result,
                                })),
                            )?;
                        }
                        if jump {
                            break;
                        }
                    }
                    // Every handle is gone, so nothing will ever ask again — but
                    // the cadence is still owed to the desk.
                    None => {
                        tokio::time::sleep(left).await;
                        break;
                    }
                }
            }
        }
    }
}

/// The path one render is asked for on.
///
/// Built here rather than by the caller for the reason the module header
/// gives: nothing outside this file may aim a request. The name is
/// percent-encoded because it goes into a *path segment* — every visual the
/// owner registers today is a Python module name and therefore already safe,
/// and the encoding is what keeps that from being load-bearing the first time
/// one is not.
pub fn visual_url(base: &str, name: &str) -> String {
    format!(
        "{}/api/visuals/{}",
        base.trim_end_matches('/'),
        encode_segment(name)
    )
}

/// Percent-encode everything that is not unreserved, per RFC 3986.
///
/// Hand-rolled rather than pulled in: `reqwest`'s `query` feature encodes query
/// *values*, and this is a path segment — a `/` in one is a different route,
/// not an escaped character.
fn encode_segment(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    for byte in name.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(*byte as char)
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

/// The retry this pane owns, named in every sentence that says there is no
/// drawing.
///
/// A refusal, a broken owner and a dropped request all end the same way for
/// the operator: press the key again. Saying so is not decoration — the pane
/// has no automatic retry and no beat behind it, so a sentence that stopped at
/// what went wrong would leave a window that looks permanently stuck.
const RETRY: &str = "Enter asks again";

/// One render attempt, and what the frame owes for it.
///
/// Two fields because the two are different audiences. `result` is what the
/// pane draws and is **never absent** — every way a render can end reaches the
/// operator as an answer, which is what retires the "asking the owner…" line.
/// `malformed` is the status line's own MALFORMED chip, set only when the
/// owner answered 2xx with a body this client could not decode: that is a
/// broken contract with the owner and invariant 4 says it fails loud, beside
/// the pane's own quieter sentence rather than instead of it.
struct Rendered {
    result: VisualResult,
    malformed: Option<String>,
}

/// One render, with every non-2xx body read rather than thrown away.
///
/// Its own fetch rather than `fetch::<Visual>`, and this is the whole reason:
/// the shared one turns every non-2xx into `Failed` **without reading the
/// body**, and the body is precisely where the owner puts the sentence that
/// says what to fix. A pane built on the shared path could only ever say
/// "owner answered 400", which names no remedy at all.
///
/// The 4xx/5xx split is the second reason. `is_client_error` is the whole test:
/// a 4xx is the owner having considered the request, a 5xx is the owner
/// breaking while drawing, and only the first is something the operator can fix
/// by asking for something else.
async fn render_visual(client: &reqwest::Client, url: &str) -> Rendered {
    let resp = match client.get(url).send().await {
        Ok(resp) => resp,
        Err(err) => return unanswered(format!("the owner did not answer ({})", because(&err))),
    };
    let status = resp.status();
    let body = match resp.text().await {
        Ok(body) => body,
        // The headers arrived and the body did not. Still nothing to draw, and
        // still not a refusal: the owner never got as far as deciding.
        Err(err) => {
            return unanswered(format!(
                "the owner's answer stopped mid-body ({})",
                because(&err)
            ))
        }
    };
    if status.is_success() {
        return match serde_json::from_str::<Visual>(&body) {
            Ok(visual) => Rendered {
                result: VisualResult::Drawn(Box::new(visual)),
                malformed: None,
            },
            // Both halves: the chip says the contract broke, and the pane says
            // there is no drawing and how to ask again. Either alone leaves one
            // of the two surfaces lying — a chip over a pane that still claims
            // to be waiting, or a pane that quietly gave up on a broken owner.
            Err(err) => Rendered {
                result: VisualResult::Unanswered {
                    said: format!(
                        "the owner answered with something this client cannot read — {RETRY}"
                    ),
                },
                malformed: Some(err.to_string()),
            },
        };
    }
    Rendered {
        result: not_drawn(status, &body),
        malformed: None,
    }
}

/// What a non-2xx answer means, split on the one test that decides it.
///
/// Its own function so the split has a caller a test can reach without a
/// socket: the canned owner in `tests/http_poll.rs` answers 200 or 404 and has
/// no way to produce a 500, and a test that restated this branch instead of
/// calling it would be pinning its own copy.
fn not_drawn(status: reqwest::StatusCode, body: &str) -> VisualResult {
    let code = status.as_u16();
    let said = owner_said(body);
    match status.is_client_error() {
        // The owner considered it and said no. Its sentence, verbatim — the
        // 404 body names the visuals that do exist and the 400 body names the
        // parameter that was wrong.
        true => VisualResult::Refused {
            status: code,
            said: match said {
                Some(said) => said,
                None => format!("the owner answered {code} and said nothing about why"),
            },
        },
        // The owner broke. Never the word "refused": a 500 is a traceback in
        // somebody else's process, and an operator sent to edit their request
        // would be fixing the one thing that was not wrong.
        false => VisualResult::Failed {
            status: code,
            said: match said {
                Some(said) => {
                    format!("the owner failed at {code} while drawing this: {said} — {RETRY}")
                }
                None => format!("the owner failed at {code} while drawing this — {RETRY}"),
            },
        },
    }
}

/// A render that produced nothing to draw and no verdict either.
fn unanswered(what: String) -> Rendered {
    Rendered {
        result: VisualResult::Unanswered {
            said: format!("{what} — {RETRY}"),
        },
        malformed: None,
    }
}

/// The owner's own sentence out of an error body, if it sent one.
///
/// `None` covers a body nobody sent, an empty sentence, and a body this client
/// cannot read — three shapes of "it said nothing", and the caller composes the
/// stand-in. Inventing a remedy for any of them is how a client starts teaching
/// an operator something the owner never said.
fn owner_said(body: &str) -> Option<String> {
    serde_json::from_str::<VisualError>(body)
        .ok()
        .and_then(|payload| payload.error)
        .filter(|said| !said.trim().is_empty())
}

/// One client builder for both halves of the boundary.
///
/// `pub(crate)` so `net::write` shares this timeout rather than choosing its
/// own: a write that gave up sooner than a read would report a plan as failed
/// while the owner was still booking it.
pub(crate) fn build_client() -> reqwest::Result<reqwest::Client> {
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
    fn an_owners_error_body_is_read_and_an_empty_one_is_not_invented() {
        // The 404 body names the visuals that *do* exist and the 400 body
        // names the parameter that was wrong. Neither is a sentence this
        // client could compose, and both are the whole remedy.
        assert_eq!(
            owner_said(r#"{"error": "no visual named circut; known: quantum_circuit"}"#).as_deref(),
            Some("no visual named circut; known: quantum_circuit")
        );
        // A body nobody sent, an empty sentence, one this client cannot read,
        // and one with no sentence in it are all "it said nothing" — the
        // caller composes the stand-in rather than this inventing one.
        for body in ["", r#"{"error": ""}"#, "<html>502</html>", "{}"] {
            assert_eq!(owner_said(body), None, "{body:?}");
        }
    }

    #[test]
    fn every_sentence_this_module_composes_names_the_key_that_asks_again() {
        // The pane has no automatic retry and no beat behind it, so a sentence
        // that stopped at what went wrong would leave a window that looks
        // permanently stuck.
        //
        // A refusal is deliberately not here: its sentence is the *owner's*,
        // verbatim, and appending a remedy to somebody else's words is how a
        // client starts editing what the desk said. The two shapes this module
        // writes itself are the two that carry the key.
        let said = |result: &VisualResult| match result {
            VisualResult::Refused { said, .. }
            | VisualResult::Failed { said, .. }
            | VisualResult::Unanswered { said } => said.clone(),
            VisualResult::Drawn(_) => panic!("a drawing has no sentence"),
        };
        assert!(said(&unanswered("the owner did not answer".into()).result).contains(RETRY));
        let broken = render_failure(500, r#"{"error": "KeyError: angles"}"#);
        assert!(said(&broken).contains(RETRY), "{}", said(&broken));
    }

    #[test]
    fn a_4xx_is_the_owner_refusing_and_a_5xx_is_the_owner_breaking() {
        // The split the whole variant exists for. A refusal is a decision an
        // operator acts on by asking for something else; a 500 is a traceback
        // in somebody else's process, and "the desk said no" would send them
        // to edit the one thing that was not wrong.
        let refused = render_failure(404, r#"{"error": "no visual named circut"}"#);
        match &refused {
            VisualResult::Refused { status, said } => {
                assert_eq!(*status, 404);
                // Verbatim, and with nothing of this client's prepended.
                assert_eq!(said, "no visual named circut");
            }
            other => panic!("a 404 was not a refusal: {other:?}"),
        }

        let broke = render_failure(500, r#"{"error": "KeyError: angles"}"#);
        match &broke {
            VisualResult::Failed { status, said } => {
                assert_eq!(*status, 500);
                assert!(said.contains("500"), "{said}");
                assert!(said.contains("the owner failed"), "{said}");
                // The word an operator would act on wrongly.
                assert!(
                    !said.to_lowercase().contains("refus"),
                    "a 5xx was drawn as a refusal: {said}"
                );
                // The owner's own detail is kept beside it rather than lost.
                assert!(said.contains("KeyError: angles"), "{said}");
            }
            other => panic!("a 500 was not a failure: {other:?}"),
        }

        // A 5xx with nothing in the body still says the status and the remedy.
        let quiet = render_failure(503, "");
        match &quiet {
            VisualResult::Failed { said, .. } => {
                assert!(said.contains("503") && said.contains(RETRY), "{said}");
                assert!(!said.to_lowercase().contains("refus"), "{said}");
            }
            other => panic!("a 503 was not a failure: {other:?}"),
        }
    }

    /// The production split, called rather than restated.
    fn render_failure(status: u16, body: &str) -> VisualResult {
        not_drawn(reqwest::StatusCode::from_u16(status).unwrap(), body)
    }

    #[test]
    fn a_visual_name_is_one_path_segment_and_never_a_route_of_its_own() {
        assert_eq!(
            visual_url("http://127.0.0.1:8765", "quantum_circuit"),
            "http://127.0.0.1:8765/api/visuals/quantum_circuit"
        );
        // The escapes that matter: a separator, a traversal, and a space.
        assert_eq!(encode_segment("a/b"), "a%2Fb");
        assert_eq!(encode_segment(".."), "..");
        assert_eq!(encode_segment("a b?x=1"), "a%20b%3Fx%3D1");
    }

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
