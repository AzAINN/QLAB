//! The confirm modal, and the capability it mints.
//!
//! The desk's execution gate is a chain of bindings, and this is its last link.
//! The referee's PASS is bound to the exact `targets_hash` of the targets it
//! reviewed; the persisted approval carries that same hash; and here the human's
//! confirmation is bound to it too — by typing the last six characters of it,
//! shown nowhere else on the desk.
//!
//! That last step is not ceremony. `y` is one key, and one key can be sent by a
//! macro, a stuck terminal, a paste, or a script written against yesterday's
//! screen. Six characters that exist only inside this box, and that change with
//! every set of targets, cannot be replayed: a confirmation captured against one
//! plan produces the wrong six for the next. It is the same defence the referee
//! has, at the one point where a human is the referee.
//!
//! **Where the hash comes from.** From the *approval*, not the plan. The
//! registry's `plans` table has no `targets_hash` column — `plan_id`,
//! `decision_id`, `state`, `targets`, `pre_trade`, `created_at` — so the owner's
//! `/api/tui` plans payload cannot carry one. The `approval_requests` table
//! does, written by the owner from the canonical `targets_hash(targets)`, and
//! `/api/tui` serves it. Recomputing the hash here from `plan.targets` was the
//! alternative and is rejected: it would mean reimplementing a Python float
//! `repr` in Rust and agreeing with it to the last digit forever, and a client
//! that computed its own hash would be checking its own arithmetic rather than
//! binding to what the referee actually passed.
//!
//! A plan with no covering approval therefore has no modal. That is correct
//! rather than a limitation: the owner refuses to book without an `approval_id`
//! anyway, so a modal for such a plan could only ever arm a request the desk
//! would reject — after asking a human to vouch for it.
//!
//! Nothing here performs IO or holds a client. It renders, it reads keystrokes,
//! and it mints a `ConfirmToken`. The BOOK and AUDIT views own one `Host` each.

use crate::cmd::Command;
use crate::format::MISSING;
use crate::model::{Approval, Plan};
use crate::theme::theme;
use crate::ui::widgets::panel_header;
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};

/// The modal's width, and the height it never goes under. Wide enough for a
/// sixteen-character hash beside its label, tall enough for the title, four
/// facts, the challenge, and the rule.
///
/// `H` became a *floor* when the book box arrived: that one states an
/// allocation diff, and a fixed twelve rows would have shown a human three of
/// the legs they were vouching for and hidden the rest — a confirmation given
/// against numbers the box did not draw. Every box still asks for `H` at
/// minimum, so the execute box is the 50×12 it has always been.
const W: u16 = 50;
const H: u16 = 12;

/// The chrome a box spends on something other than its facts: the two borders,
/// the title, and the blank row above the prompt.
const CHROME_H: u16 = 4;

/// How much of the hash the human types. Six characters is 24 bits of the
/// owner's digest — far beyond what a mistyped or replayed confirmation lands on
/// by accident, and still short enough to be read off the line above and typed
/// without the operator reaching for a clipboard they would then paste from.
const CHALLENGE_LEN: usize = 6;

/// The word an action with no plan to bind to asks for.
const STATIC_CHALLENGE: &str = "CONFIRM";

/// Proof that a human confirmed one specific plan.
///
/// Constructible only inside this module: every field is private, so no code
/// outside `confirm` can write the struct literal, and there is no `new`, no
/// `Default`, and no `From`. The writer's `execute_plan` takes one, which is
/// what makes "the only path to a fill runs through a human reading this box" a
/// property the compiler enforces rather than a convention a future key handler
/// could forget.
///
/// The type is named here and nowhere else in `ui/` on purpose — `ui/` renders
/// and mints, the runtime acts, and `tests/operator_gate.rs` greps this tree for
/// the writer's name to keep it that way.
///
/// It carries the ids rather than letting the caller supply them. A token that
/// only said "someone confirmed something" would still allow the substitution
/// this whole chain exists to prevent — confirm the small plan, execute the
/// large one — so the plan, the approval, and the hash travel together with the
/// consent that was given for them.
///
/// Deliberately not `Clone`, and the writer takes it by value: a token is spent
/// by the call it authorises, so it cannot be held and fired twice. The owner
/// already refuses a second fill against a consumed approval — this is the
/// client's half of the same rule, and it is what stops a retry loop around a
/// timed-out request from booking the same plan again. A failed execution is
/// something an operator re-reads the desk about, never something a caller
/// quietly repeats.
#[derive(Debug)]
pub struct ConfirmToken {
    plan_id: String,
    approval_id: String,
    targets_hash: String,
}

impl ConfirmToken {
    pub fn plan_id(&self) -> &str {
        &self.plan_id
    }

    pub fn approval_id(&self) -> &str {
        &self.approval_id
    }

    /// The hash the human's six characters came from. Carried so the audit line
    /// written beside a fill names what was confirmed, not just that something
    /// was.
    pub fn targets_hash(&self) -> &str {
        &self.targets_hash
    }
}

/// Proof that a human confirmed the desk's current proposal.
///
/// A second capability rather than a reuse of [`ConfirmToken`], and the
/// difference is what each one can spend. `ConfirmToken` names an approval and
/// buys one `POST /api/plans/execute`; this names no approval at all, because
/// `POST /api/desk/proposal/book` resolves the current proposal itself and
/// refuses a plan that is not it. One type for both would have let a
/// confirmation given for one route be spent on the other, where the owner
/// checks a different set of things.
///
/// Constructible only inside this module, and — like its neighbour —
/// deliberately not `Clone`, taken by value by the writer, and minted at most
/// once per box.
///
/// **What the human confirmed is a hash they were shown, not one they typed.**
/// That is the whole distinction of this box, and it is a ruling rather than a
/// convenience: the desk's proposal card puts the allocation, the referee's
/// verdict and the hash's last six on screen together, and the box repeats them
/// immediately above the word that arms it. The replay a typed challenge
/// defends against is defended here by the binding instead — the token carries
/// the hash the box *displayed*, and the owner refuses it against anything but
/// the proposal it currently holds.
#[derive(Debug)]
pub struct BookToken {
    plan_id: String,
    targets_hash: String,
}

impl BookToken {
    pub fn plan_id(&self) -> &str {
        &self.plan_id
    }

    /// The hash the box showed the human. Carried rather than re-derived at the
    /// call site for `ConfirmToken::targets_hash`'s reason: the consent and what
    /// it was given for travel together.
    pub fn targets_hash(&self) -> &str {
        &self.targets_hash
    }
}

/// What the modal is asking about.
#[derive(Debug, Clone)]
enum Binding {
    /// An action with no plan behind it — Atlas's mode, the desk's book. It
    /// still takes a typed word, because a state change to what the supervisor
    /// may do should never be one key away, but there is nothing to bind to and
    /// so nothing it can mint.
    Action,
    /// The consent has been spent by the confirmation it authorised. Terminal:
    /// nothing moves a modal out of this state, so retyping the challenge does
    /// not re-arm it.
    Spent,
    /// One checked plan, through the approval that covers it.
    Plan {
        plan_id: String,
        approval_id: String,
        targets_hash: String,
    },
    /// The desk's current proposal, bound to the hash the box displays.
    ///
    /// The one binding that arms without a typed challenge. What it gives up in
    /// keystrokes it takes back in what is on screen: the box states the
    /// allocation, the verdict and the hash together, and the owner then
    /// re-derives the proposal from its own registry and refuses this plan if
    /// it is no longer the question being asked.
    Book {
        plan_id: String,
        targets_hash: String,
    },
}

/// A centred, blocking question.
///
/// Deliberately not `Clone`. Single use has to sit on the *consent*, not merely
/// on the token it mints: a clonable modal could be duplicated while armed and
/// each copy asked for a confirmation, which is the same "one human decision,
/// many bookings" hole as a re-mintable token wearing a different hat.
#[derive(Debug)]
pub struct Modal {
    title: String,
    facts: Vec<(String, String)>,
    challenge: String,
    typed: String,
    binding: Binding,
    /// One sentence under the facts, for a decision whose *consequence* is not
    /// readable off its own columns. A plan box needs none — its facts are the
    /// trade — but a universe change's facts are two ids and a ticker, and what
    /// approving does to the desk is nowhere in them.
    note: Option<String>,
    /// Where the last frame drew the words that arm this box, so a click on
    /// them can do exactly what Enter does and nothing else.
    ///
    /// Published by `draw` the way the views publish their own rects, and for
    /// the same reason: a click is answered about the frame in front of the
    /// operator, never about one not yet painted. `Rect::default()` — which no
    /// click can be inside — until a frame has drawn the row, and only ever set
    /// by a box that actually offers a click.
    arm_row: std::cell::Cell<Rect>,
}

impl Modal {
    /// The modal for booking one plan, or `None` if this approval cannot speak
    /// for this plan.
    ///
    /// Three refusals, and each is a real substitution it prevents:
    ///
    /// * the approval names a different plan — showing that hash here would arm
    ///   an execution against a plan the human never saw the numbers for;
    /// * either id is missing — there is nothing to bind the consent *to*;
    /// * the hash is absent or too short — there is nothing to bind it *with*,
    ///   and falling back to the plan id (the plan's own sketch allowed it)
    ///   would put six characters on screen that bind to nothing the referee
    ///   ever checked. A confirmation ritual with no content is worse than none:
    ///   it teaches the operator that typing the characters means the desk
    ///   checked something.
    ///
    /// Note what is deliberately *not* checked: the approval's `status`, its
    /// expiry, and whether it still covers the current book. Those are the
    /// owner's `check_approval_for_execution`, which invalidates rather than
    /// executes and says why — and a client that pre-judged them would be a
    /// second, drifting copy of the gate. This module decides one thing only:
    /// that the human is looking at the hash they are about to vouch for.
    pub fn for_plan(plan: &Plan, approval: &Approval) -> Option<Modal> {
        let plan_id = plan.plan_id.as_deref()?;
        if approval.plan_id.as_deref() != Some(plan_id) {
            return None;
        }
        let approval_id = approval.approval_id.as_deref()?;
        let hash = approval.targets_hash.as_deref()?;
        if hash.chars().count() < CHALLENGE_LEN {
            return None;
        }
        let challenge: String = hash
            .chars()
            .skip(hash.chars().count() - CHALLENGE_LEN)
            .collect();

        let mut facts = vec![
            ("plan".into(), plan_id.to_string()),
            ("targets hash".into(), hash.to_string()),
        ];
        // Every fact comes from the record the owner's gate will actually read,
        // never recomputed and never from whichever record happens to have the
        // field.
        //
        // Legs and turnover come from the *plan's* `pre_trade`, because that is
        // what execution checks: `execute_plan_with_approval` takes
        // `expected_legs` from `stored["pre_trade"]["n_legs"]`
        // (`server.py:1895`) and refuses the plan outright if the persisted legs
        // disagree. The approval's `summary` is a different number written at a
        // different moment, and the two can diverge — in the test fixture the
        // summary says 7 legs on a plan that has 2. Showing the summary asked a
        // human to vouch for a seven-leg trade the desk would evaluate as two,
        // which is the same class of error as an unbound hash: a confirmation
        // given against numbers nothing downstream is holding to.
        if let Some(pre) = plan.pre_trade.as_ref() {
            if let Some(turnover) = pre.get("turnover").and_then(|v| v.as_f64()) {
                facts.push(("turnover".into(), format!("{:.1}%", turnover * 100.0)));
            }
            if let Some(legs) = pre.get("n_legs").and_then(|v| v.as_i64()) {
                facts.push(("legs".into(), legs.to_string()));
            }
        }
        // What the approval genuinely owns: the hash above, the book it binds,
        // and its own state. Status is worth the row — an approval that is still
        // `pending` cannot authorise a fill, and an operator who can see that
        // before typing six characters is spared a refusal they could have
        // predicted.
        if let Some(broker) = approval.broker.as_deref() {
            facts.push(("book".into(), broker.to_string()));
        }
        if let Some(status) = approval.status.as_deref() {
            facts.push(("approval".into(), status.to_string()));
        }

        Some(Modal {
            title: "EXECUTE PLAN".into(),
            facts,
            challenge,
            typed: String::new(),
            binding: Binding::Plan {
                plan_id: plan_id.to_string(),
                approval_id: approval_id.to_string(),
                targets_hash: hash.to_string(),
            },
            note: None,
            arm_row: std::cell::Cell::new(Rect::default()),
        })
    }

    /// The modal for booking the desk's current proposal, or `None` if there is
    /// nothing here to bind the consent to.
    ///
    /// **The one box on this workstation that arms without a typed challenge**,
    /// and the reason is what is on screen rather than what is missing. The
    /// execute box asks for six characters because it is opened off a plan card
    /// that states an id and a turnover: the hash exists nowhere else, so typing
    /// it is what proves the human read *this* plan's. The proposal card states
    /// the allocation, the referee's verdict and the hash together, this box
    /// repeats all three immediately above the word that arms it, and the owner
    /// then re-derives which plan is the current proposal and refuses anything
    /// else. The binding is carried, not typed — and a click, like Enter, can
    /// only ever send the hash this box displayed.
    ///
    /// Two refusals, and each is a substitution it prevents:
    ///
    /// * no plan id — there is nothing to bind the consent *to*;
    /// * a hash absent or shorter than the six characters the box shows — there
    ///   is nothing to bind it *with*, and a box that armed against a hash it
    ///   could not display would be a ritual with no content.
    ///
    /// `facts` is the caller's: the card composes the diff, and a box that
    /// re-derived it would be a second account of the same numbers. What this
    /// function owns is the row that names the last six, which is prepended
    /// here so no caller can open a book box without it.
    pub fn book(plan_id: &str, targets_hash: &str, facts: Vec<(String, String)>) -> Option<Modal> {
        if plan_id.is_empty() {
            return None;
        }
        if targets_hash.chars().count() < CHALLENGE_LEN {
            return None;
        }
        let shown: String = targets_hash
            .chars()
            .skip(targets_hash.chars().count() - CHALLENGE_LEN)
            .collect();
        let mut rows = vec![
            ("plan".to_string(), plan_id.to_string()),
            ("targets hash".to_string(), targets_hash.to_string()),
            // The six characters the execute box would have asked for, shown
            // rather than requested. Its own row so it is beside the full hash
            // it is the tail of, and so a test can read what the box bound to
            // without parsing the prompt.
            ("confirming".to_string(), shown.clone()),
        ];
        rows.extend(facts);
        Some(Modal {
            title: "BOOK THE PROPOSAL".into(),
            facts: rows,
            // The characters the prompt displays. Not something to be typed —
            // `armed` never compares against it for this binding — but the box
            // has one string it is bound by, and two spellings of it would be
            // two chances to show one and send the other.
            challenge: shown,
            typed: String::new(),
            binding: Binding::Book {
                plan_id: plan_id.to_string(),
                targets_hash: targets_hash.to_string(),
            },
            note: None,
            arm_row: std::cell::Cell::new(Rect::default()),
        })
    }

    /// The modal for a desk action: a static word, and no capability at the end
    /// of it.
    pub fn action(title: &str, facts: Vec<(String, String)>) -> Modal {
        Modal {
            title: title.to_string(),
            facts,
            challenge: STATIC_CHALLENGE.into(),
            typed: String::new(),
            binding: Binding::Action,
            note: None,
            arm_row: std::cell::Cell::new(Rect::default()),
        }
    }

    /// Add the sentence that says what answering this box does.
    pub fn with_note(mut self, note: &str) -> Modal {
        self.note = Some(note.to_string());
        self
    }

    /// The confirm box for deciding one approval request, whichever kind it is.
    ///
    /// One function for both callers — the AUDIT key and the chat's `/approve`
    /// — because two spellings of "what does this box say" is how a universe
    /// change came to be drawn as a plan with everything missing.
    ///
    /// `approval` is optional: the chat resolves an id the snapshot may no
    /// longer be serving, and a box that named the id and nothing else is still
    /// the truth about what is being decided.
    ///
    /// A `universe_change` states its own columns rather than a plan's. It binds
    /// no plan and never expires, so `plan` and `expires` are not absent facts
    /// to be dashed — they do not apply, and the row that would carry them says
    /// what the request *is* instead.
    pub fn for_approval(approval_id: &str, approval: Option<&Approval>, verb: &str) -> Modal {
        let mut facts = vec![("approval".to_string(), approval_id.to_string())];
        let universe = approval.is_some_and(|a| a.is_universe_change());
        if universe {
            let approval = approval.expect("universe is only true for a row we hold");
            facts.push(("kind".to_string(), "universe change".to_string()));
            facts.push((
                "ticker".to_string(),
                approval
                    .summary_str("ticker")
                    .unwrap_or(MISSING)
                    .to_string(),
            ));
            if let Some(memo) = approval.summary_str("memo_decision_id") {
                facts.push(("memo".to_string(), memo.to_string()));
            }
            let ticker = approval.summary_str("ticker").unwrap_or("this ticker");
            return Modal::action(&format!("{} UNIVERSE CHANGE", verb.to_uppercase()), facts)
                .with_note(&format!(
                    "Approving admits {ticker} into the desk's research universe. It books nothing."
                ));
        }
        if let Some(approval) = approval {
            if let Some(plan) = crate::format::text(approval.plan_id.as_ref()) {
                facts.push(("plan".to_string(), plan.to_string()));
            }
            if let Some(expires) = crate::format::clock(approval.expires_at.as_ref()) {
                facts.push(("expires".to_string(), expires));
            }
        }
        Modal::action(&format!("{} APPROVAL", verb.to_uppercase()), facts)
    }

    /// Whether this box is answered by reading rather than by typing.
    ///
    /// Derived from the binding and settable nowhere, which is the point: a
    /// flag a caller could pass would let an execute box be opened without its
    /// challenge, and the difference between the two rituals would become an
    /// argument rather than a type.
    fn displayed_only(&self) -> bool {
        matches!(self.binding, Binding::Book { .. })
    }

    /// What the human has to type. Public so a test can pin that it is the
    /// hash's own last six and not something this module invented.
    pub fn challenge(&self) -> &str {
        &self.challenge
    }

    pub fn typed(&self) -> &str {
        &self.typed
    }

    /// The label/value rows the box states, in order.
    ///
    /// Public so a test can pin *which record each number came from* rather than
    /// only that the box rendered something — the leg count read off the wrong
    /// record still drew a perfectly good-looking modal.
    pub fn facts(&self) -> &[(String, String)] {
        &self.facts
    }

    /// Whether this box is ready to be answered.
    ///
    /// Three arms rather than one comparison, and the middle one is what keeps
    /// the whole thing honest:
    ///
    /// * a book box is armed from the moment it opens — its ritual is reading,
    ///   and Enter is the answer;
    /// * a **spent** box is never armed again, whatever it holds. Without this
    ///   arm a spent book box would compare an empty `typed` against an empty
    ///   challenge and read as armed for ever;
    /// * everything else is the typed challenge, case-sensitive and
    ///   whole-string. A case-insensitive compare would halve the entropy of a
    ///   hex challenge for no gain — the characters are on screen directly
    ///   above the field.
    pub fn armed(&self) -> bool {
        match self.binding {
            Binding::Book { .. } => true,
            Binding::Spent => false,
            Binding::Action | Binding::Plan { .. } => self.typed == self.challenge,
        }
    }

    /// One typed character. Bounded by the challenge's length so a held key
    /// cannot grow the buffer without limit, and so overtyping is visibly
    /// refused rather than silently accumulating behind the field.
    ///
    /// A book box takes none: it has no field, and a buffer filling up behind a
    /// box that shows one would be state nothing on screen accounts for.
    pub fn push(&mut self, c: char) {
        if self.displayed_only() {
            return;
        }
        if self.typed.chars().count() < self.challenge.chars().count() {
            self.typed.push(c);
        }
    }

    pub fn backspace(&mut self) {
        self.typed.pop();
    }

    /// The capability, if a human has met the challenge for a plan — **once**.
    ///
    /// Takes `&mut self` and moves the binding out, so minting *spends the
    /// consent*. A second call returns `None`, and no amount of retyping brings
    /// it back: the state it leaves behind is terminal.
    ///
    /// This is stricter than it looks, and the strictness is the point. With
    /// `&self` the loop
    ///
    /// ```ignore
    /// loop { client.execute_plan(modal.token().unwrap()).await; }
    /// ```
    ///
    /// compiled — one human confirmation authorising an unbounded number of
    /// bookings. Making the *token* single-use did not close that, because the
    /// modal simply minted another; the consent is what has to be consumed.
    ///
    /// A failed or refused execution therefore cannot be retried by re-firing
    /// the same modal, which is correct for an order path: after an ambiguous
    /// failure the operator re-reads the desk and confirms again against what it
    /// says now, rather than replaying a decision made against a stale screen.
    ///
    /// `None` for an action modal even when armed: there is no plan for a token
    /// to bind, and a token that bound nothing would be a key to the execution
    /// path handed out by a mode change.
    pub fn token(&mut self) -> Option<ConfirmToken> {
        if !self.armed() {
            return None;
        }
        match std::mem::replace(&mut self.binding, Binding::Spent) {
            Binding::Plan {
                plan_id,
                approval_id,
                targets_hash,
            } => Some(ConfirmToken {
                plan_id,
                approval_id,
                targets_hash,
            }),
            // An action modal is spent by the same call, so a confirmed mode
            // change cannot be replayed either. It never had a token to give.
            //
            // And a *book* binding is spent here too rather than passed over.
            // Enter routes by the `Pending` beside the modal, so this arm is
            // only reached if the two disagreed — a box bound to the proposal
            // and a pending that says "execute" — and the safe reading of that
            // is to consume the consent and mint nothing, never to hand the
            // execute path a token it was not given.
            Binding::Action | Binding::Book { .. } | Binding::Spent => None,
        }
    }

    /// The booking capability, if this box is bound to the desk's current
    /// proposal — **once**.
    ///
    /// The same contract as [`Modal::token`], for the same reason: `&mut self`,
    /// the binding moved out, and the state left behind terminal. A refused
    /// booking is something an operator re-reads the card about, never
    /// something a caller quietly repeats — and here that matters more than on
    /// the execute path, because two of the three refusals this route can
    /// return *leave the approval alive*, so a retry loop would find a
    /// genuinely bookable proposal waiting for it.
    ///
    /// `None` for every other binding, including a spent one: a token minted
    /// from an execute box would carry a hash the human typed against a
    /// different question.
    pub fn book_token(&mut self) -> Option<BookToken> {
        if !self.armed() {
            return None;
        }
        match std::mem::replace(&mut self.binding, Binding::Spent) {
            Binding::Book {
                plan_id,
                targets_hash,
            } => Some(BookToken {
                plan_id,
                targets_hash,
            }),
            Binding::Action | Binding::Plan { .. } | Binding::Spent => None,
        }
    }

    /// Draw centred in `area`, over whatever was there.
    ///
    /// `Clear` first: the frame underneath is a live desk that repaints three
    /// times a second, and a half-transparent question about an order would let
    /// a moving number sit inside the box a human is reading before they commit.
    pub fn draw(&self, f: &mut Frame, area: Rect) {
        // Retracted first, and on every path out of this function. A rect left
        // over from a frame that drew the row would let a click arm a box the
        // frame in front of the operator never offered one on.
        self.arm_row.set(Rect::default());
        let rect = centred(area, self.height());
        if rect.width == 0 || rect.height == 0 {
            return;
        }
        let t = theme();
        f.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(t.warning))
            .style(Style::default().bg(t.bg_raised));
        let inner = block.inner(rect);
        f.render_widget(block, rect);

        let mut lines = vec![panel_header(&self.title)];
        lines.extend(self.facts.iter().map(|(label, value)| {
            Line::from(vec![
                Span::styled(
                    format!("{label:<14}"),
                    Style::default().fg(t.text_secondary),
                ),
                Span::styled(value.clone(), Style::default().fg(t.text_primary)),
            ])
        }));
        for row in self.note_rows() {
            lines.push(Line::from(Span::styled(
                row,
                Style::default().fg(t.text_secondary),
            )));
        }
        lines.push(Line::default());
        if self.displayed_only() {
            // No field, and therefore one row rather than two. The words that
            // arm it are named as words because they are also the click target:
            // what a mouse can do here is exactly what Enter does, and the row
            // says so rather than leaving the click undiscoverable.
            self.arm_row.set(Rect {
                x: inner.x,
                y: inner.y + lines.len() as u16,
                width: inner.width,
                height: 1,
            });
            lines.push(Line::from(vec![
                Span::styled("↵ ", Style::default().fg(t.text_tertiary)),
                Span::styled(
                    ARM_WORD,
                    Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
                ),
                Span::styled(" · bound to ", Style::default().fg(t.text_secondary)),
                // The six characters, shown rather than asked for. Bold and in
                // the warning colour for the reason the typed challenge is:
                // they are the thing being read, not chrome.
                Span::styled(
                    self.challenge.clone(),
                    Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
                ),
                Span::styled(" · Esc", Style::default().fg(t.text_dim)),
            ]));
        } else {
            lines.push(Line::from(vec![
                Span::styled("type ", Style::default().fg(t.text_secondary)),
                // The one place these six characters appear. Bold and in the
                // warning colour because they are the thing being read off, not
                // chrome.
                Span::styled(
                    self.challenge.clone(),
                    Style::default().fg(t.warning).add_modifier(Modifier::BOLD),
                ),
                Span::styled(" to confirm", Style::default().fg(t.text_secondary)),
            ]));
            lines.push(Line::from(vec![
                Span::styled("> ", Style::default().fg(t.text_tertiary)),
                Span::styled(
                    self.typed.clone(),
                    Style::default()
                        .fg(if self.armed() {
                            t.positive
                        } else {
                            t.text_primary
                        })
                        .add_modifier(Modifier::BOLD),
                ),
            ]));
        }

        f.render_widget(Paragraph::new(lines), inner);
    }

    /// The rows this box needs, never under [`H`].
    ///
    /// Counted from what it actually draws rather than assumed, because the
    /// book box states an allocation diff and the number of legs is the desk's,
    /// not this module's.
    fn height(&self) -> u16 {
        let prompt = if self.displayed_only() { 1 } else { 2 };
        H.max(CHROME_H + self.facts.len() as u16 + self.note_rows().len() as u16 + prompt)
    }

    /// The note, wrapped to the box's own inner width.
    ///
    /// Wrapped against [`W`] rather than the area, because `height` is asked
    /// before the rect exists — and a note counted at one width and drawn at
    /// another is a box whose last row falls outside its own border.
    fn note_rows(&self) -> Vec<String> {
        let Some(note) = self.note.as_deref() else {
            return Vec::new();
        };
        let width = (W - 2) as usize;
        let mut rows = vec![String::new()];
        for word in note.split_whitespace() {
            let row = rows.last_mut().expect("seeded with one row");
            if row.is_empty() {
                row.push_str(word);
            } else if row.chars().count() + 1 + word.chars().count() <= width {
                row.push(' ');
                row.push_str(word);
            } else {
                rows.push(word.to_string());
            }
        }
        rows
    }

    /// Whether the last frame drew this box's arming words under that cell.
    ///
    /// `false` for every box that offers no click, and for every box before its
    /// first frame — a rect of zero width contains nothing.
    fn armed_at(&self, column: u16, row: u16) -> bool {
        let rect = self.arm_row.get();
        rect.height > 0
            && row == rect.y
            && column >= rect.x
            && column < rect.x.saturating_add(rect.width)
    }
}

/// The words that arm a book box, and the words a click on the card runs.
///
/// One constant for both because they are one affordance seen twice: the card
/// says `book`, the box says `book it`, and an operator who learns the word on
/// one surface has learned it on the other.
pub const ARM_WORD: &str = "book it";

/// What the runtime is asked to do once the human has typed the challenge.
///
/// Carried beside the modal rather than encoded in it, because two of the three
/// bind no plan: `Modal::action` mints nothing, so without this the shell would
/// know a human had confirmed *something* and not which approval it was about.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pending {
    Approve(String),
    Reject(String),
    /// The plan case. The command's payload is the token the modal mints, so
    /// there is nothing to carry here — the binding is inside the modal.
    Execute,
    /// The desk's current proposal, booked in one call. Carries nothing for
    /// `Execute`'s reason: the plan and the hash are inside the modal, and a
    /// pair repeated out here would be a second copy of the binding that could
    /// disagree with the one the token is minted from.
    Book,
}

/// One view's modal slot: at most one question on screen, and the keystrokes
/// that answer it.
///
/// A view owns this rather than the shell, because the view is what knows which
/// approval or plan the operator was looking at when they pressed the key. The
/// shell still routes *every* keystroke here first while a question is up, and
/// draws it over the whole frame — a modal confined to a view's own rect would
/// let `3` walk away from an unanswered question about an order.
///
/// The host is what makes the consent single-use in practice: answering takes
/// the modal out, so there is no armed box left for a second Enter to fire.
#[derive(Debug, Default)]
pub struct Host {
    open: Option<(Modal, Pending)>,
}

impl Host {
    /// Put a question up. A second `open` while one is showing is refused
    /// rather than stacked: two order confirmations on screen at once is a
    /// human answering the one they can see and arming the one they cannot.
    pub fn open(&mut self, modal: Modal, pending: Pending) {
        if self.open.is_none() {
            self.open = Some((modal, pending));
        }
    }

    /// The question on screen, for the shell to draw and a test to read.
    pub fn showing(&self) -> Option<&Modal> {
        self.open.as_ref().map(|(modal, _)| modal)
    }

    /// One keystroke into the box.
    ///
    /// `Esc` abandons, `Enter` answers once armed, everything printable types.
    /// An unarmed `Enter` leaves the box up rather than closing it: a human who
    /// mistyped the challenge has to see that they did.
    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    pub fn on_key(&mut self, k: KeyEvent) -> Option<Command> {
        self.open.as_ref()?;
        match k.code {
            KeyCode::Esc => {
                self.open = None;
                None
            }
            KeyCode::Enter => {
                if !self.open.as_ref().is_some_and(|(modal, _)| modal.armed()) {
                    return None;
                }
                // Taken, not borrowed: the consent is spent by this answer, and
                // a modal left in the slot is a second Enter away from a second
                // command carrying the same human decision.
                let (mut modal, pending) = self.open.take()?;
                match pending {
                    // `token()` is the mint *and* the spend. `None` here means
                    // the modal could not bind the plan after all, which is a
                    // refusal to execute rather than a fill with no consent.
                    Pending::Execute => modal.token().map(Command::Execute),
                    // The same mint-and-spend, on the other capability. `None`
                    // here means the box was not bound to the proposal after
                    // all, which is a refusal to book rather than a fill with
                    // no consent.
                    Pending::Book => modal.book_token().map(Command::Book),
                    Pending::Approve(id) => Some(Command::Approve(id)),
                    Pending::Reject(id) => Some(Command::Reject(id)),
                }
            }
            KeyCode::Backspace => {
                if let Some((modal, _)) = self.open.as_mut() {
                    modal.backspace();
                }
                None
            }
            KeyCode::Char(c) => {
                if let Some((modal, _)) = self.open.as_mut() {
                    modal.push(c);
                }
                None
            }
            _ => None,
        }
    }

    /// One click while a question is up.
    ///
    /// **The only mouse event a modal answers, and it answers exactly one
    /// thing**: a left click on the words the box drew to arm itself. Anything
    /// else — a click elsewhere in the box, a click on the desk behind it, a
    /// wheel — is swallowed, which is what a blocking question owes: a scroll
    /// that moved the pane underneath would let a human answer about a frame
    /// they can no longer see.
    ///
    /// It cannot reach a box that draws no such words. `armed_at` reads a rect
    /// only the book box ever publishes, so the typed-challenge boxes have no
    /// click path at all rather than one guarded by a condition.
    pub fn on_mouse(&mut self, m: crossterm::event::MouseEvent) -> Option<Command> {
        use crossterm::event::{MouseButton, MouseEventKind};
        if !matches!(m.kind, MouseEventKind::Down(MouseButton::Left)) {
            return None;
        }
        if !self
            .open
            .as_ref()
            .is_some_and(|(modal, _)| modal.armed_at(m.column, m.row))
        {
            return None;
        }
        // Taken, not borrowed, and routed by the same `Pending` Enter is: a
        // click and a keystroke are two spellings of one answer, and two
        // routings of it would be two chances for one to widen.
        let (mut modal, pending) = self.open.take()?;
        match pending {
            Pending::Book => modal.book_token().map(Command::Book),
            // Unreachable today — no other box publishes an arming rect — and
            // deliberately not an `unreachable!`: the safe reading of a box
            // that somehow offered a click it was not built for is to consume
            // the consent and send nothing.
            Pending::Execute | Pending::Approve(_) | Pending::Reject(_) => None,
        }
    }

    pub fn draw(&self, f: &mut Frame, area: Rect) {
        if let Some(modal) = self.showing() {
            modal.draw(f, area);
        }
    }
}

/// The modal's rect, centred and never larger than the frame.
///
/// Clamped rather than assumed: a 50×12 box drawn into a 40-column terminal
/// would be laid out off the right edge, and the field the human types into is
/// the part that would leave the screen.
fn centred(area: Rect, height: u16) -> Rect {
    let w = W.min(area.width);
    let h = height.min(area.height);
    Rect {
        x: area.x + (area.width.saturating_sub(w)) / 2,
        y: area.y + (area.height.saturating_sub(h)) / 2,
        width: w,
        height: h,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan_and_approval() -> (Plan, Approval) {
        let plan = Plan {
            plan_id: Some("9661b0e88b4a669e".into()),
            ..Plan::default()
        };
        let approval = Approval {
            approval_id: Some("1a2b3c4d5e6f7081".into()),
            plan_id: Some("9661b0e88b4a669e".into()),
            targets_hash: Some("c4d5e6f708192a3b".into()),
            ..Approval::default()
        };
        (plan, approval)
    }

    #[test]
    fn the_challenge_is_the_tail_of_the_owners_hash() {
        let (plan, approval) = plan_and_approval();
        let modal = Modal::for_plan(&plan, &approval).unwrap();
        assert_eq!(modal.challenge(), "192a3b");
        assert!(approval.targets_hash.unwrap().ends_with(modal.challenge()));
    }

    #[test]
    fn overtyping_is_refused_rather_than_buffered() {
        // A held key or a pasted line must not push the correct prefix out of
        // the field and leave the human looking at an unarmed box they believe
        // they filled in.
        let (plan, approval) = plan_and_approval();
        let mut modal = Modal::for_plan(&plan, &approval).unwrap();
        for c in "192a3bXXXX".chars() {
            modal.push(c);
        }
        assert_eq!(modal.typed(), "192a3b");
        assert!(modal.armed());
    }

    #[test]
    fn a_token_is_never_minted_for_a_binding_that_has_no_plan() {
        let mut modal = Modal::action("HALT", vec![]);
        for c in STATIC_CHALLENGE.chars() {
            modal.push(c);
        }
        assert!(modal.armed());
        assert!(modal.token().is_none());
    }

    #[test]
    fn a_host_answers_once_and_the_second_enter_has_nothing_to_send() {
        // The consent is spent by the answer, not by the token: a modal left in
        // the slot is one Enter away from a second command carrying the same
        // human decision.
        let (plan, approval) = plan_and_approval();
        let mut host = Host::default();
        host.open(Modal::for_plan(&plan, &approval).unwrap(), Pending::Execute);
        for c in "192a3b".chars() {
            host.on_key(key(KeyCode::Char(c)));
        }
        assert!(matches!(
            host.on_key(key(KeyCode::Enter)),
            Some(Command::Execute(_))
        ));
        assert!(host.showing().is_none(), "answering closes the box");
        assert!(host.on_key(key(KeyCode::Enter)).is_none());
    }

    #[test]
    fn an_unarmed_enter_leaves_the_question_up() {
        // A human who mistyped the challenge has to see that they did, rather
        // than have the box vanish and wonder what it did.
        let mut host = Host::default();
        host.open(Modal::action("HALT", vec![]), Pending::Approve("a1".into()));
        assert!(host.on_key(key(KeyCode::Enter)).is_none());
        assert!(host.showing().is_some());

        for c in "CONFIRX".chars() {
            host.on_key(key(KeyCode::Char(c)));
        }
        assert!(host.on_key(key(KeyCode::Enter)).is_none());
        host.on_key(key(KeyCode::Backspace));
        host.on_key(key(KeyCode::Char('M')));
        assert_eq!(
            host.on_key(key(KeyCode::Enter)),
            Some(Command::Approve("a1".into()))
        );
    }

    #[test]
    fn escape_abandons_and_decides_nothing() {
        let mut host = Host::default();
        host.open(Modal::action("HALT", vec![]), Pending::Reject("a1".into()));
        for c in "CONFIRM".chars() {
            host.on_key(key(KeyCode::Char(c)));
        }
        assert!(host.on_key(key(KeyCode::Esc)).is_none());
        assert!(host.showing().is_none());
    }

    #[test]
    fn a_second_question_cannot_stack_over_one_already_on_screen() {
        // Two order confirmations at once is a human answering the one they can
        // see and arming the one they cannot.
        let mut host = Host::default();
        host.open(
            Modal::action("FIRST", vec![]),
            Pending::Approve("a1".into()),
        );
        host.open(
            Modal::action("SECOND", vec![]),
            Pending::Reject("a2".into()),
        );
        for c in "CONFIRM".chars() {
            host.on_key(key(KeyCode::Char(c)));
        }
        assert_eq!(
            host.on_key(key(KeyCode::Enter)),
            Some(Command::Approve("a1".into())),
            "the box on screen is the one that was answered"
        );
    }

    #[test]
    fn a_host_with_nothing_up_claims_no_keys() {
        // The shell routes every keystroke here first while a question is up;
        // an empty host that swallowed them would freeze the workstation.
        let mut host = Host::default();
        for code in [KeyCode::Enter, KeyCode::Esc, KeyCode::Char('q')] {
            assert!(host.on_key(key(code)).is_none());
        }
        assert!(host.showing().is_none());
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, crossterm::event::KeyModifiers::NONE)
    }

    #[test]
    fn the_box_is_clamped_into_a_terminal_too_small_to_hold_it() {
        let tiny = centred(Rect::new(0, 0, 30, 6), H);
        assert_eq!((tiny.width, tiny.height), (30, 6));
        let roomy = centred(Rect::new(0, 0, 120, 36), H);
        assert_eq!((roomy.width, roomy.height), (W, H));
        assert_eq!((roomy.x, roomy.y), ((120 - W) / 2, (36 - H) / 2));
    }

    fn a_proposal_box(legs: usize) -> Modal {
        let facts = (0..legs)
            .map(|i| (format!("ASSET{i}"), format!("{i}.0% → {}.0%", i + 1)))
            .collect();
        Modal::book("b92a58fa5c1d4e7f", "0f1e2d3c4b5a6978", facts).unwrap()
    }

    #[test]
    fn a_book_box_arms_on_enter_and_its_token_carries_the_hash_it_displayed() {
        // The whole ruling in one test: no typing, and what the owner is sent
        // is what the box put on screen.
        let mut host = Host::default();
        host.open(a_proposal_box(2), Pending::Book);
        let shown = host
            .showing()
            .unwrap()
            .facts()
            .iter()
            .find(|(label, _)| label == "confirming")
            .map(|(_, value)| value.clone())
            .expect("the box names the six characters it is bound by");
        assert_eq!(shown, "5a6978", "the tail of the owner's own hash");
        assert!(host.showing().unwrap().armed(), "a book box opens armed");

        let Some(Command::Book(token)) = host.on_key(key(KeyCode::Enter)) else {
            panic!("Enter did not mint a booking")
        };
        assert_eq!(token.targets_hash(), "0f1e2d3c4b5a6978");
        assert!(token.targets_hash().ends_with(&shown));
        assert_eq!(token.plan_id(), "b92a58fa5c1d4e7f");
        assert!(host.showing().is_none(), "answering closes the box");
        assert!(host.on_key(key(KeyCode::Enter)).is_none());
    }

    #[test]
    fn a_spent_book_box_is_never_armed_again() {
        // The arm that stops an empty `typed` from matching an empty challenge
        // for ever. Read off the modal directly rather than through the host,
        // which takes the box out and would hide the state being pinned.
        let mut modal = a_proposal_box(1);
        assert!(modal.book_token().is_some());
        assert!(!modal.armed(), "a spent consent re-armed itself");
        assert!(modal.book_token().is_none());
        assert!(modal.token().is_none(), "and it minted no execute token");
    }

    #[test]
    fn a_book_box_takes_no_typing_and_an_execute_token_is_not_one_of_its_answers() {
        let mut modal = a_proposal_box(1);
        for c in "5a6978".chars() {
            modal.push(c);
        }
        assert_eq!(modal.typed(), "", "a box with no field buffered keystrokes");
        // And the two capabilities do not cross: this binding mints a booking
        // and never a fill against a named approval.
        assert!(modal.token().is_none());
    }

    #[test]
    fn a_hash_too_short_to_show_opens_no_book_box() {
        // Nothing to bind the consent *with*. A box that armed against five
        // characters would teach an operator that reading them means the desk
        // checked something.
        assert!(Modal::book("b92a58fa", "12345", vec![]).is_none());
        assert!(Modal::book("", "0f1e2d3c4b5a6978", vec![]).is_none());
        assert!(Modal::book("b92a58fa", "123456", vec![]).is_some());
    }

    #[test]
    fn a_book_box_grows_for_its_diff_and_never_shrinks_under_the_floor() {
        // A fixed twelve rows would have drawn three legs of a twelve-leg
        // allocation and hidden the rest — a confirmation given against numbers
        // the box did not show.
        assert_eq!(a_proposal_box(1).height(), H);
        let tall = a_proposal_box(12);
        assert!(tall.height() > H, "{}", tall.height());
        assert_eq!(tall.height(), CHROME_H + 15 + 1);
    }

    #[test]
    fn only_the_arming_row_of_a_book_box_answers_a_click() {
        // Published by the frame, so a box that has never been drawn answers
        // nothing — and a click anywhere else is swallowed rather than passed
        // to the desk underneath.
        let mut host = Host::default();
        host.open(a_proposal_box(2), Pending::Book);
        assert!(
            host.on_mouse(click(10, 10)).is_none(),
            "a box nobody has drawn armed on a click"
        );

        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        term.draw(|f| host.draw(f, f.area())).unwrap();
        let row = host.showing().unwrap().arm_row.get();
        assert!(row.height > 0, "the frame published no arming row");
        assert!(host.on_mouse(click(row.x + 2, row.y + 1)).is_none());
        assert!(matches!(
            host.on_mouse(click(row.x + 2, row.y)),
            Some(Command::Book(_))
        ));
        assert!(host.showing().is_none(), "a click did not spend the box");
    }

    #[test]
    fn a_typed_challenge_box_has_no_click_path_at_all() {
        // Absence, not a guard: only the book box publishes an arming rect, so
        // there is no cell of an EXECUTE box a click can answer.
        let (plan, approval) = plan_and_approval();
        let mut host = Host::default();
        host.open(Modal::for_plan(&plan, &approval).unwrap(), Pending::Execute);
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 36)).unwrap();
        term.draw(|f| host.draw(f, f.area())).unwrap();
        for y in 0..36 {
            for x in (0..120).step_by(7) {
                assert!(host.on_mouse(click(x, y)).is_none(), "({x},{y})");
            }
        }
        assert!(host.showing().is_some(), "a click answered a typed box");
    }

    fn click(column: u16, row: u16) -> crossterm::event::MouseEvent {
        crossterm::event::MouseEvent {
            kind: crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left),
            column,
            row,
            modifiers: crossterm::event::KeyModifiers::NONE,
        }
    }
}
