//! The desk's single current proposal, as one card — drawn identically wherever it appears.
//!
//! Two surfaces show it: BOOK, where the desk's own numbers are, and ATLAS's
//! sidebar, where the operator is reading what the desk means. It is the same
//! card on both, and it lives here rather than in either view by the rule
//! `widgets/mod.rs` states — a widget moves here once a second view needs it.
//! That is not tidiness in this case: the card is what a human reads before
//! confirming a fill, and two renderings of "no referee PASS covers these
//! targets" is two chances for one of them to offer a key the other refuses.
//!
//! **It renders in both builds.** A monitoring window shows the desk's open
//! question exactly as an armed one does — that is what a monitoring window is
//! *for* — and what the feature gate removes is the last line of the card, the
//! word that opens the box, and the box itself. Absence, not a branch.
//!
//! Nothing here decides anything. The owner composes the proposal, holds the
//! referee's verdict to the plan's own `targets_hash`, and re-validates every
//! one of these facts when the booking arrives; this module turns its answer
//! into rows and, in an armed build, into the facts the confirm box states.
//!
//! **There is no in-flight guard, and what stands in for one is the owner.**
//! A second `b` while the first POST is still outstanding opens a second box
//! and, answered, sends a second request. Nothing here can prevent that: the
//! flag would have to be raised by the view and cleared by the *outcome*, and
//! the outcome arrives on the bus into the store, which this module only
//! reads. What bounds the damage is the route itself — `book_current_proposal`
//! resolves the current proposal on the way in and refuses any `plan_id` that
//! is not it (`not the current proposal`, 400), and a successful book consumes
//! the approval — so the second request cannot produce a second fill; it
//! produces a refusal the card then renders. That is a real guarantee and it
//! is the owner's, not this client's, which is the right way round for
//! anything that moves money. It is still one round trip that did not need to
//! happen, and a client-side latch belongs in the store beside `booking`
//! whenever that file is next open.

use crate::format::{self, MISSING};
use crate::model::Proposal;
use crate::store::Store;
use crate::theme::theme;
use ratatui::{
    style::{Modifier, Style},
    text::{Line, Span},
};

/// How much of a plan id a row carries. Eleven, as the plan ledger and AUDIT
/// show one: far past what distinguishes two plans on any desk, and the confirm
/// box names the record in full.
const ID_W: usize = 11;

/// The most superseded plans a card names before it counts the rest.
///
/// Two, because the point of the rows is that an approval the operator had
/// *already given* was revoked — the desk says that once in the chat and
/// scrollback is not a record — and a card that spent six rows on withdrawals
/// would push the allocation it is asking about off the pane.
const SUPERSEDED_ROWS: usize = 2;

/// How many rows a booking note may wrap to. Two: the owner's reasons plus the
/// clause that says what to do next, which is the part that must not clip.
///
/// Gated with the note it bounds — a monitoring build has never booked
/// anything, so there is no note and nothing to bound.
#[cfg(feature = "operator")]
const NOTE_ROWS: usize = 2;

/// The card's rows, and where the word that arms a booking landed.
pub struct Card {
    pub lines: Vec<Line<'static>>,
    /// The row index of the `book` affordance, if this frame drew one.
    ///
    /// Published rather than recomputed for the reason ATLAS publishes its
    /// `/word` rects: a click is answered about the frame in front of the
    /// operator, and a second derivation of "where the word went" is free to
    /// disagree with the packer that put it there.
    ///
    /// `None` in a monitoring build always, and in an armed one whenever the
    /// last row is a sentence rather than the word — an unarmed window, an
    /// owner that has gone quiet, a proposal no referee PASS covers, or a card
    /// clamped so short the row fell off it. The card says why on that row
    /// instead, and a row that explains why there is no key may never *be*
    /// one.
    pub book_row: Option<usize>,
}

/// The whole card, packed into `room` rows.
///
/// The allocation diff is what gives: every other row is a single fact the
/// operator has to have — which plan, what the referee said, what the last
/// press did — and the diff is the one section that can be summarised without
/// becoming a different statement ("+4 more legs" is honest; half a referee
/// line is not). What is dropped is counted, never silently cut.
pub fn card(store: &Store, width: u16, room: usize) -> Card {
    let Some(proposal) = store.proposal() else {
        return Card {
            lines: vec![dim(
                "no proposal open — the desk is not waiting on you",
                width,
            )],
            book_row: None,
        };
    };

    // Built before the diff, because the diff is what is trimmed to fit them.
    let mut tail: Vec<Line<'static>> = Vec::new();
    tail.push(numbers_row(proposal));
    tail.push(referee_row(proposal, width));
    tail.extend(superseded_rows(proposal, width));
    tail.extend(note_rows(store, proposal, width));

    let head = plan_row(proposal, width);
    let action = action_row(store, proposal, width);
    // The head, the tail and the action row are never given up: at a `room`
    // too small for them the card is truncated at the end, which loses the
    // action row rather than a fact — a card that dropped "no referee PASS"
    // and kept the word that books would be the worst possible trim.
    let fixed = 1 + tail.len() + usize::from(action.is_some());
    let legs = diff(store, proposal);
    let mut lines = vec![head];
    let mut drawn = 0usize;
    if let Some(spare) = room.checked_sub(fixed) {
        // One row of the budget goes to the count when not every leg fits, so
        // an operator is never shown a partial allocation that reads as whole.
        let shown = match legs.len() > spare {
            true => spare.saturating_sub(1),
            false => legs.len(),
        };
        for (ticker, from, to) in legs.iter().take(shown) {
            lines.push(leg_row(ticker, *from, *to, width));
            drawn += 1;
        }
        if drawn < legs.len() && spare > 0 {
            lines.push(dim(&format!("+{} more legs", legs.len() - drawn), width));
        }
    }
    lines.extend(tail);
    // Only the row that actually carries `BOOK_WORD` is a click target. The
    // other two shapes of this row are sentences saying why there is no key,
    // and publishing a rect for those made them into buttons that contradict
    // themselves.
    let book_row = action.and_then(|(line, offers)| {
        lines.push(line);
        offers.then(|| lines.len() - 1)
    });
    // The clamp is last and takes from the end, so the action row goes before
    // any fact does. A `book_row` past the clamp is retracted with it: a word
    // that is not on screen may not be clickable.
    lines.truncate(room);
    let book_row = book_row.filter(|row| *row < lines.len());
    Card { lines, book_row }
}

/// `plan b92a58fa5c1  approved  expires 15:42`.
fn plan_row(proposal: &Proposal, width: u16) -> Line<'static> {
    let t = theme();
    let mut spans = vec![
        Span::styled("plan ", Style::default().fg(t.text_secondary)),
        Span::styled(
            clip(format::or_missing(proposal.plan_id.as_ref()), ID_W),
            Style::default()
                .fg(t.text_primary)
                .add_modifier(Modifier::BOLD),
        ),
    ];
    if let Some(state) = format::text(proposal.approval_state.as_ref()) {
        spans.push(Span::styled(
            format!(" {state}"),
            Style::default().fg(t.text_secondary),
        ));
    }
    if let Some(expires) = format::clock(proposal.expires_at.as_ref()) {
        spans.push(Span::styled(
            format!(" · expires {expires}"),
            Style::default().fg(t.text_tertiary),
        ));
    }
    clipped(spans, width)
}

/// One leg of the diff: what the book holds, and what the plan asks for.
fn leg_row(ticker: &str, from: Option<f64>, to: f64, width: u16) -> Line<'static> {
    let t = theme();
    // Absent is not zero — the owner's rule everywhere in this client. A name
    // the live book does not hold has no weight to move *from*, and drawing
    // that as `0.0%` would state a position the desk never reported.
    let held = match from {
        Some(weight) => format::pct1(weight),
        None => MISSING.to_string(),
    };
    let tone = match from {
        Some(weight) if to > weight => t.positive,
        Some(weight) if to < weight => t.negative,
        Some(_) => t.text_dim,
        None => t.positive,
    };
    clipped(
        vec![
            Span::styled(
                format!("{:<6}", clip(ticker, 6)),
                Style::default().fg(t.text_primary),
            ),
            Span::styled(format!("{held:>6}"), Style::default().fg(t.text_tertiary)),
            Span::styled(" → ", Style::default().fg(t.text_dim)),
            Span::styled(
                format!("{:>6}", format::pct1(to)),
                Style::default().fg(tone),
            ),
        ],
        width,
    )
}

/// `turnover 100.0%  20 legs`, from the plan's own `pre_trade`.
///
/// The *plan's*, because that is the record the owner's gate reads: it takes
/// `expected_legs` from `pre_trade.n_legs` and refuses the plan outright if the
/// persisted count disagrees. Nothing here is counted off the targets.
fn numbers_row(proposal: &Proposal) -> Line<'static> {
    let t = theme();
    let turnover = match proposal.pre_trade_f64("turnover") {
        Some(value) => format::pct1(value),
        None => MISSING.to_string(),
    };
    let legs = match proposal.pre_trade_i64("n_legs") {
        Some(n) => n.to_string(),
        None => MISSING.to_string(),
    };
    Line::from(vec![
        Span::styled("turnover ", Style::default().fg(t.text_secondary)),
        Span::styled(turnover, Style::default().fg(t.text_primary)),
        Span::styled(
            format!(" · {legs} legs"),
            Style::default().fg(t.text_secondary),
        ),
    ])
}

/// What the referee said, and the six characters the confirmation binds to.
///
/// One row for both because they are one fact: a PASS is only a PASS *of these
/// targets*, and the owner's own block carries the hash it was bound to. A card
/// that showed the word without the binding would be showing a verdict that
/// could have been about a different allocation.
fn referee_row(proposal: &Proposal, width: u16) -> Line<'static> {
    let t = theme();
    let hash = proposal.targets_hash.as_deref().unwrap_or_default();
    let tail: String = match hash.chars().count() >= 6 {
        true => hash.chars().skip(hash.chars().count() - 6).collect(),
        false => MISSING.to_string(),
    };
    if !proposal.referee_passed() {
        // Named rather than left blank, and in the refusal's colour: the owner
        // refuses to book without a PASS covering this hash, so a card that
        // said nothing here would leave an operator pressing a key at a
        // question the desk has already answered.
        return Line::from(Span::styled(
            match proposal.referee.as_ref().and_then(|r| r.verdict.as_deref()) {
                // A verdict that exists and does not bind is a different fact
                // from no verdict at all, and both are different from a FAIL:
                // "referee PASS does not cover 5a6978" is the sentence for a
                // PASS about some other allocation, which is the one shape a
                // card must never let read as approval.
                Some(verdict) => format!("referee {verdict} does not cover {tail}"),
                None => format!("no referee verdict covers {tail}"),
            },
            Style::default().fg(t.negative),
        ));
    }
    let source = proposal
        .referee
        .as_ref()
        .and_then(|r| format::text(r.source.as_ref()))
        .unwrap_or("referee");
    // The binding before the authority, deliberately: the row is clipped to the
    // pane and ATLAS's sidebar is 32 cells, so whichever comes last is what a
    // narrow window loses. Which hash the PASS covers is the load-bearing half
    // — a verdict with no binding is a verdict that could be about anything.
    clipped(
        vec![
            Span::styled(
                "referee PASS",
                Style::default().fg(t.positive).add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!(" · {tail} · {source}"),
                Style::default().fg(t.text_tertiary),
            ),
        ],
        width,
    )
}

/// The plans this proposal withdrew, struck through.
///
/// Struck *and* worded, never one or the other: `CROSSED_OUT` is a modifier a
/// good many terminals ignore, and an id that reads as live on those would be
/// exactly the wrong thing to leave on a card about booking.
fn superseded_rows(proposal: &Proposal, width: u16) -> Vec<Line<'static>> {
    let t = theme();
    let keep = proposal.superseded.iter().take(SUPERSEDED_ROWS);
    let new = clip(format::or_missing(proposal.plan_id.as_ref()), 8);
    let mut out: Vec<Line<'static>> = keep
        .map(|old| {
            clipped(
                vec![
                    Span::styled(
                        clip(old, ID_W),
                        Style::default()
                            .fg(t.text_dim)
                            .add_modifier(Modifier::CROSSED_OUT),
                    ),
                    Span::styled(
                        format!(" superseded by {new}"),
                        Style::default().fg(t.text_dim),
                    ),
                ],
                width,
            )
        })
        .collect();
    if proposal.superseded.len() > SUPERSEDED_ROWS {
        out.push(dim(
            &format!(
                "+{} more withdrawn",
                proposal.superseded.len() - SUPERSEDED_ROWS
            ),
            width,
        ));
    }
    out
}

/// What the last booking did, when it was about this proposal.
///
/// Gated with the write path: a monitoring window has never booked anything, so
/// there is no note for it to hold rather than a field carried and never
/// written.
#[cfg(feature = "operator")]
fn note_rows(store: &Store, proposal: &Proposal, width: u16) -> Vec<Line<'static>> {
    use crate::store::BookingKind;
    let t = theme();
    let Some(booking) = store.booking.as_ref() else {
        return Vec::new();
    };
    // Only about the plan on screen. The store retires a note when the desk's
    // question moves, and this is the second half of the same rule: a note is
    // never drawn beside a proposal it was not answered for.
    if Some(booking.plan_id.as_str()) != proposal.plan_id.as_deref() {
        return Vec::new();
    }
    let tone = match booking.kind {
        BookingKind::Filled => t.positive,
        BookingKind::Retry => t.warning,
        BookingKind::RePropose | BookingKind::Unclear | BookingKind::Failed => t.negative,
    };
    wrap(&booking.said, width as usize)
        .into_iter()
        .take(NOTE_ROWS)
        .map(|chunk| Line::from(Span::styled(chunk, Style::default().fg(tone))))
        .collect()
}

#[cfg(not(feature = "operator"))]
fn note_rows(_store: &Store, _proposal: &Proposal, _width: u16) -> Vec<Line<'static>> {
    Vec::new()
}

/// The last row: the word that books, or why there is none — **and which of the
/// two it is**.
///
/// The flag is the whole point of the pair. This row has three shapes and only
/// one of them is an affordance: the other two are sentences explaining why
/// there is no key (`view-only …`, `no referee PASS …`, `the owner is not
/// answering …`). A caller that published a click target for "whichever row
/// ended the card" made those sentences into buttons — a row that contradicts
/// its own text, and an operator told they may act being refused a layer later
/// is worse than one never offered the key. So the answer travels with the row
/// rather than being inferred from its presence.
///
/// `None` in a monitoring build — there is no key, no word and no box, and a
/// row that named one would be the binary claiming an authority it does not
/// have. In an armed one it is always *some* row, because a key that does
/// nothing in silence reads as a hung client.
#[cfg(feature = "operator")]
fn action_row(store: &Store, proposal: &Proposal, width: u16) -> Option<(Line<'static>, bool)> {
    let t = theme();
    if !store.posture.writes() {
        return Some((
            dim("view-only — booking needs an armed window", width),
            false,
        ));
    }
    // An owner that has stopped answering is the one refusal that is not about
    // the proposal at all. The poller deliberately does not retire a proposal
    // on a failed fetch — a lane that reported the desk gone every time one
    // request timed out would be worse than a stale card, and the panel, the
    // templates and the board all take the same line — so what the card holds
    // may be a question the desk has already withdrawn, and nothing in the
    // posture says so: `posture` comes from the last snapshot that *did*
    // arrive, and stays `Operator` for as long as the process lives. Offering
    // the word here would be offering to book against a screen nobody can
    // vouch for. The connection chip is already red; this row says what that
    // costs.
    if !store.conn.owner {
        return Some((
            dim(
                "the owner is not answering — this proposal may already be withdrawn",
                width,
            ),
            false,
        ));
    }
    Some(match bookable(proposal) {
        Ok(_) => (
            Line::from(vec![
                Span::styled(
                    BOOK_WORD,
                    Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    " ↵ or b — one confirm",
                    Style::default().fg(t.text_secondary),
                ),
            ]),
            true,
        ),
        Err(why) => (dim(&why, width), false),
    })
}

#[cfg(not(feature = "operator"))]
fn action_row(_store: &Store, _proposal: &Proposal, _width: u16) -> Option<(Line<'static>, bool)> {
    None
}

/// The word the card draws and a click on it runs.
///
/// Public so the views can measure it and a test can name it without spelling
/// the string twice — the same rule the confirm box's `ARM_WORD` follows.
#[cfg(feature = "operator")]
pub const BOOK_WORD: &str = "book";

/// What the confirm box would be opened against, or why it would not be.
///
/// **The refusals are the owner's own preconditions, not this client's
/// judgment.** `POST /api/desk/proposal/book` refuses a hash that does not
/// match and a proposal no PASS covers, so a box armed against either would ask
/// a human to vouch for a request the desk has already decided against — the
/// same rule `Modal::for_plan` holds to. What is deliberately *not* checked
/// here is anything that can change between this frame and the request: the
/// approval's expiry, the book's revision, the data plane. Those are the
/// owner's gate, and a client that pre-judged them would be a second, drifting
/// copy of it.
#[cfg(feature = "operator")]
pub fn bookable(proposal: &Proposal) -> Result<(String, String), String> {
    let Some(plan_id) = format::text(proposal.plan_id.as_ref()) else {
        return Err("the owner served a proposal with no plan id".to_string());
    };
    let hash = format::text(proposal.targets_hash.as_ref()).unwrap_or_default();
    if hash.chars().count() < 6 {
        return Err("this proposal carries no targets_hash to confirm against".to_string());
    }
    if !proposal.referee_passed() {
        return Err("no referee PASS covers these targets — the desk will not book it".to_string());
    }
    Ok((plan_id.to_string(), hash.to_string()))
}

/// The confirm box for the desk's current proposal, or the sentence that says
/// why there is none.
///
/// One producer for both surfaces: BOOK's `b` and ATLAS's `b` open the same
/// box, stating the same facts, and a second composition of them would be a
/// second account of what a human confirmed.
#[cfg(feature = "operator")]
pub fn modal(store: &Store) -> Result<crate::ui::widgets::confirm::Modal, String> {
    let Some(proposal) = store.proposal() else {
        return Err("the desk has no proposal open".to_string());
    };
    let (plan_id, hash) = bookable(proposal)?;
    let mut facts: Vec<(String, String)> = Vec::new();
    if let Some(state) = format::text(proposal.approval_state.as_ref()) {
        facts.push(("approval".to_string(), state.to_string()));
    }
    // The referee's word beside the hash it was bound to, because the box's
    // whole claim is that those two belong together.
    facts.push((
        "referee".to_string(),
        match proposal
            .referee
            .as_ref()
            .and_then(|r| format::text(r.source.as_ref()))
        {
            Some(source) => format!("PASS · {source}"),
            None => "PASS".to_string(),
        },
    ));
    if let Some(turnover) = proposal.pre_trade_f64("turnover") {
        facts.push(("turnover".to_string(), format::pct1(turnover)));
    }
    if let Some(legs) = proposal.pre_trade_i64("n_legs") {
        facts.push(("legs".to_string(), legs.to_string()));
    }
    if let Some(expires) = format::clock(proposal.expires_at.as_ref()) {
        facts.push(("expires".to_string(), expires));
    }
    // The allocation itself, one row per leg that moves. The box is as tall as
    // it needs to be, because a human may not be asked to vouch for legs it
    // did not draw.
    for (ticker, from, to) in diff(store, proposal) {
        facts.push((
            ticker,
            match from {
                Some(weight) => format!("{} → {}", format::pct1(weight), format::pct1(to)),
                None => format!("{} → {}", MISSING, format::pct1(to)),
            },
        ));
    }
    crate::ui::widgets::confirm::Modal::book(&plan_id, &hash, facts).ok_or_else(|| {
        // Unreachable through `bookable`, which has already checked both, and
        // loud rather than silent if that ever stops being true.
        format!("plan {plan_id} could not be bound to a confirmation")
    })
}

/// What the plan would move, in the owner's own order.
///
/// Every target the plan names, against the weight the *live* book reports for
/// it — the marked-to-tape view, which is the one the desk acts on. A name the
/// book does not hold has `None` rather than a zero: absent is not zero, and a
/// new position drawn as `0.0% → 7.5%` states a holding the owner never
/// reported.
///
/// Sorted by how much moves, largest first, so a card that can only draw four
/// rows draws the four that matter. Deliberately *not* the payload's order,
/// unlike every list this client renders whole: the trim is what makes the
/// order a decision at all, and "the biggest moves" is the only ranking that
/// makes a partial diff honest. The confirm box takes the same list and draws
/// all of it, so the ranking never decides what a human confirms.
fn diff(store: &Store, proposal: &Proposal) -> Vec<(String, Option<f64>, f64)> {
    // The *live* book — marked to the tape — and never the registry's
    // `portfolio` section beside it. The owner computes two P&L views and
    // documents that they must not be shown under one label; the desk acts on
    // this one.
    let held = |ticker: &str| -> Option<f64> {
        store
            .snapshot
            .as_ref()?
            .live_portfolio
            .as_ref()?
            .positions
            .iter()
            .find(|p| format::text(p.ticker.as_ref()) == Some(ticker))?
            .weight
    };
    let mut legs: Vec<(String, Option<f64>, f64)> = proposal
        .targets
        .iter()
        .map(|(ticker, to)| (ticker.clone(), held(ticker), *to))
        .collect();
    legs.sort_by(|a, b| {
        // A name the book does not hold moves by its whole target: there is
        // nothing there today, so all of it is new exposure.
        let moved = |leg: &(String, Option<f64>, f64)| (leg.2 - leg.1.unwrap_or(0.0)).abs();
        moved(b)
            .partial_cmp(&moved(a))
            .unwrap_or(std::cmp::Ordering::Equal)
            // Stable in the owner's own key order for a tie, so two legs that
            // move by the same amount do not swap places between frames.
            .then_with(|| a.0.cmp(&b.0))
    });
    legs
}

fn dim(text: &str, width: u16) -> Line<'static> {
    Line::from(Span::styled(
        clip(text, width as usize),
        Style::default().fg(theme().text_dim),
    ))
}

/// The head of a string, by characters rather than bytes.
fn clip(text: &str, width: usize) -> String {
    match text.char_indices().nth(width) {
        Some((cut, _)) => text[..cut].to_string(),
        None => text.to_string(),
    }
}

/// A row of spans, dropped from the right at `width` cells.
fn clipped(spans: Vec<Span<'static>>, width: u16) -> Line<'static> {
    let mut used = 0usize;
    let mut out = Vec::new();
    for span in spans {
        let room = (width as usize).saturating_sub(used);
        if room == 0 {
            break;
        }
        let text = clip(&span.content, room);
        used += text.chars().count();
        out.push(Span::styled(text, span.style));
    }
    Line::from(out)
}

/// Word wrap at `width` cells, hard-breaking a word longer than the line.
///
/// Gated with its one caller for `NOTE_ROWS`' reason: every other row of this
/// card is a single line, clipped rather than wrapped, because a fact that ran
/// onto a second row would make the card's height a function of the desk.
#[cfg(feature = "operator")]
pub(crate) fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut out = Vec::new();
    let mut line = String::new();
    let mut used = 0usize;
    for word in text.split_whitespace() {
        let mut word: Vec<char> = word.chars().collect();
        while !word.is_empty() {
            let sep = usize::from(used > 0);
            if used + sep + word.len() <= width {
                if sep == 1 {
                    line.push(' ');
                }
                line.extend(word.iter());
                used += sep + word.len();
                word.clear();
            } else if used == 0 {
                line.extend(word.drain(..width));
                out.push(std::mem::take(&mut line));
                used = 0;
            } else {
                out.push(std::mem::take(&mut line));
                used = 0;
            }
        }
    }
    if !line.is_empty() || out.is_empty() {
        out.push(line);
    }
    out
}
