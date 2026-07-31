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
//! and it mints a `ConfirmToken`. Task 18's approvals and plans views own it.

use crate::model::{Approval, Plan};
use crate::theme::theme;
use crate::ui::widgets::panel_header;
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};

/// The modal's fixed size. Wide enough for a sixteen-character hash beside its
/// label, tall enough for the title, four facts, the challenge, and the rule.
const W: u16 = 50;
const H: u16 = 12;

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
        }
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

    /// Whether the challenge has been met exactly.
    ///
    /// Case-sensitive and whole-string. A case-insensitive compare would halve
    /// the entropy of a hex challenge for no gain — the characters are on screen
    /// directly above the field.
    pub fn armed(&self) -> bool {
        self.typed == self.challenge
    }

    /// One typed character. Bounded by the challenge's length so a held key
    /// cannot grow the buffer without limit, and so overtyping is visibly
    /// refused rather than silently accumulating behind the field.
    pub fn push(&mut self, c: char) {
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
            Binding::Action | Binding::Spent => None,
        }
    }

    /// Draw centred in `area`, over whatever was there.
    ///
    /// `Clear` first: the frame underneath is a live desk that repaints three
    /// times a second, and a half-transparent question about an order would let
    /// a moving number sit inside the box a human is reading before they commit.
    pub fn draw(&self, f: &mut Frame, area: Rect) {
        let rect = centred(area);
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
        lines.push(Line::default());
        lines.push(Line::from(vec![
            Span::styled("type ", Style::default().fg(t.text_secondary)),
            // The one place these six characters appear. Bold and in the warning
            // colour because they are the thing being read off, not chrome.
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

        f.render_widget(Paragraph::new(lines), inner);
    }
}

/// The modal's rect, centred and never larger than the frame.
///
/// Clamped rather than assumed: a 50×12 box drawn into a 40-column terminal
/// would be laid out off the right edge, and the field the human types into is
/// the part that would leave the screen.
fn centred(area: Rect) -> Rect {
    let w = W.min(area.width);
    let h = H.min(area.height);
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
    fn the_box_is_clamped_into_a_terminal_too_small_to_hold_it() {
        let tiny = centred(Rect::new(0, 0, 30, 6));
        assert_eq!((tiny.width, tiny.height), (30, 6));
        let roomy = centred(Rect::new(0, 0, 120, 36));
        assert_eq!((roomy.width, roomy.height), (W, H));
        assert_eq!((roomy.x, roomy.y), ((120 - W) / 2, (36 - H) / 2));
    }
}
