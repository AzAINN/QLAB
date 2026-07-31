//! SETTINGS — what this desk is configured by, and nothing that changes it.
//!
//! Five cards of read-only facts an operator would otherwise have to assemble
//! from `mandate.yaml`, `.mcp.json`, a shell prompt and whatever the last
//! `/mode` did. Everything on the pane is the owner's own answer; nothing here
//! is composed, defaulted, or inferred.
//!
//! It changes nothing on purpose. Switching the desk is `/mode`, which is an
//! operator affordance and therefore *absent* from a glass window rather than
//! greyed — a pane that named the command in a build that has no writer would
//! be an instruction with nothing behind it. So the card says which of the two
//! this window is instead.
//!
//! Absence is the rule this view is mostly about. A `max_weight` rendered as
//! `0.0%` because the owner did not send one is a mandate that forbids holding
//! anything, which is a statement about the desk that nobody made.

use crate::cmd::Command;
use crate::format::{self, MISSING};
use crate::fx::FlashTracker;
use crate::model::{Constraints, DeskMode, System};
use crate::store::Store;
use crate::theme::{palette, theme};
use crate::ui::views::View;
use crate::ui::widgets::{panel_block, panel_header, refuse};
use crossterm::event::KeyEvent;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::Style,
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
    Frame,
};
use std::time::Instant;

/// The label column, exactly wide enough for the longest label on the pane
/// (`alpaca login`) plus the space after it. Wider and the values start
/// wrapping at the baseline width; narrower and the label collides with them.
const LABEL_W: usize = 13;

/// One card's floor: the label column, a space, and enough value for the
/// longest one that may not be clipped — the credential description wraps, but
/// `propose_only` and a provenance pair do not.
const CARD_MIN: u16 = 34;

/// Two cards side by side, with a column of space between them.
const TWO_COL: u16 = CARD_MIN * 2 + 1;

/// Nothing to retain: no cursor, no page, no field. The pane is a rendering of
/// what the owner said and there is nowhere for an operator to be looking
/// inside it.
#[derive(Default)]
pub struct SettingsView;

impl View for SettingsView {
    fn draw(&self, f: &mut Frame, area: Rect, store: &Store, _fx: &FlashTracker, _now: Instant) {
        // Label/value rows do not compress: a provenance clipped to `synthe` is
        // a source an operator has to guess at, and an authority clipped to
        // `propose_` is a governance claim that has lost its qualifier. So the
        // pane refuses rather than drawing half of each.
        if area.width < TWO_COL || area.height < 12 {
            refuse(
                f,
                area,
                format!(
                    "SETTINGS needs {TWO_COL} columns for two cards of label/value rows; \
                     this pane has {}.",
                    area.width
                ),
            );
            return;
        }
        // DESK spans both columns. It is the headline fact — which desk this
        // is, and whether it can reach the book it is pointed at — and the
        // credential description under it is a sentence the owner wrote, which
        // a half-width card would clip mid-word.
        let bands = Layout::vertical([Constraint::Length(DESK_H), Constraint::Min(0)]).split(area);
        draw_desk(f, bands[0], store);

        let cols = Layout::horizontal([Constraint::Ratio(1, 2), Constraint::Ratio(1, 2)])
            .spacing(1)
            .split(bands[1]);

        // Fixed heights, then the rationale and the theme take what is left. A
        // `Paragraph` taller than its area is clipped silently, so every card
        // with a known row count states it rather than sharing a ratio that
        // would shorten one of them the first time a card grew a row.
        let left =
            Layout::vertical([Constraint::Length(POLICY_H), Constraint::Min(0)]).split(cols[0]);
        draw_policy(f, left[0], store);
        draw_rationale(f, left[1], store);

        let right = Layout::vertical([
            Constraint::Length(SYSTEM_H),
            Constraint::Length(UNIVERSE_H),
            Constraint::Min(0),
        ])
        .split(cols[1]);
        draw_system(f, right[0], store);
        draw_universe(f, right[1], store);
        draw_theme(f, right[2]);
    }

    // Every key claimed here owes a row in `input::KEYMAP`, and a test reads
    // this function to check it. That module's header lists what the check
    // cannot see — including why a comment in here may not spell a key variant.
    //
    // This one claims none. Nothing on the pane scrolls or selects, so a key
    // pressed here belongs to whoever claims it next; swallowing one would read
    // as a hung client.
    fn on_key(&mut self, _k: KeyEvent, _store: &mut Store) -> Option<Command> {
        None
    }
}

/// Header, five rows, a blank, the posture line, one row of slack for a value
/// long enough to wrap, and the rule the block reserves.
const DESK_H: u16 = 10;
/// Header, eight rows, and the rule.
const POLICY_H: u16 = 10;
/// Header, seven rows, and the rule.
const SYSTEM_H: u16 = 9;
/// Header, the count, the symbol list, and the rule.
const UNIVERSE_H: u16 = 4;

/// What the desk is pointed at, and whether it can reach it.
fn draw_desk(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(mode) = store.desk_mode() else {
        card(f, area, "desk", vec![absent("the owner sent no desk mode")]);
        return;
    };
    let mut rows = vec![
        kv("mode", or_missing(mode.label.as_ref()), t.text_primary),
        kv("data", or_missing(mode.data.as_ref()), t.text_secondary),
        kv("book", or_missing(mode.book.as_ref()), t.text_secondary),
        kv("lane", lane(mode), t.text_secondary),
    ];
    // The description is the only thing that names the missing credential, so
    // it is rendered whatever the verdict — and toned by the verdict, which is
    // the same rule the status line's chip is drawn by.
    rows.push(kv(
        "alpaca login",
        or_missing(mode.credentials.as_ref()),
        if mode.book_unreachable() {
            t.warning
        } else {
            t.text_secondary
        },
    ));
    rows.push(Line::from(""));
    rows.push(Line::from(Span::styled(
        // Posture, not the build: a featured binary the human did not arm reads
        // GLASS on the status line and must not be told about a command it
        // would refuse.
        if store.posture.writes() {
            " /mode switches the desk — this pane only reports it"
        } else {
            " read-only — this window cannot switch the desk"
        },
        Style::default().fg(t.text_dim),
    )));
    card(f, area, "desk", rows);
}

/// Which lane the data comes down. Absent stays absent: a desk whose owner did
/// not say is not an offline one.
fn lane(mode: &DeskMode) -> String {
    match mode.offline {
        Some(true) => "offline · synthetic".to_string(),
        Some(false) => "online".to_string(),
        None => MISSING.to_string(),
    }
}

/// The policy every paper solve runs under, and the four limits it is held to.
fn draw_policy(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(policy) = store.policy() else {
        card(f, area, "policy", vec![absent("the owner sent no policy")]);
        return;
    };
    let limits = policy.constraints.clone().unwrap_or_default();
    let rows = vec![
        // Two rows rather than `id · label`: the pair is wider than a
        // half-width card, and the id is the one an operator types.
        kv("policy", or_missing(policy.id.as_ref()), t.accent),
        kv("method", or_missing(policy.label.as_ref()), t.text_primary),
        kv(
            "algorithm",
            or_missing(policy.algorithm_id.as_ref()),
            t.text_secondary,
        ),
        kv(
            "objective",
            or_missing(policy.objective.as_ref()),
            t.text_secondary,
        ),
        kv(
            "solver",
            or_missing(policy.solver.as_ref()),
            t.text_secondary,
        ),
        kv("long only", yes_no(limits.long_only), t.text_primary),
        kv("budget", opt_pct1(limits.budget), t.text_primary),
        // One row for the pair: a floor without its ceiling is half a mandate,
        // and 0% is a real floor rather than an absent one.
        kv("per asset", weight_band(&limits), t.text_primary),
    ];
    card(f, area, "policy", rows);
}

fn weight_band(limits: &Constraints) -> String {
    match (limits.min_weight, limits.max_weight) {
        (None, None) => MISSING.to_string(),
        (min, max) => format!("{} – {}", opt_pct1(min), opt_pct1(max)),
    }
}

/// Why the policy is the one the desk runs, in the owner's words. Wrapped,
/// because it is a sentence rather than a value, and clipped sentences are the
/// class of refusal this workstation spends rows to avoid.
fn draw_rationale(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(rationale) = store
        .policy()
        .and_then(|p| format::text(p.rationale.as_ref()))
    else {
        return;
    };
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" {rationale}"),
            Style::default().fg(t.text_tertiary),
        )))
        .wrap(Wrap { trim: false }),
        area,
    );
}

/// Health and authority, as the owner reports them.
fn draw_system(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let Some(system) = store.system() else {
        card(
            f,
            area,
            "system",
            vec![absent("the owner sent no system status")],
        );
        return;
    };
    let rows = vec![
        kv("desk", or_missing(system.mode.as_ref()), t.text_primary),
        kv("provenance", provenance(system), t.text_secondary),
        kv(
            "claude",
            availability(system.claude_available),
            t.text_secondary,
        ),
        kv("mcp", mcp(system), t.text_secondary),
        kv(
            "proxy",
            availability(system.mcp_proxy_available),
            t.text_secondary,
        ),
        kv(
            "workforce",
            availability(system.workforce_available),
            t.text_secondary,
        ),
        // The one row on this card that is a governance claim rather than a
        // health one: it is what bounds every agent on this desk.
        kv(
            "authority",
            or_missing(system.governed_authority.as_ref()),
            t.accent,
        ),
    ];
    card(f, area, "system", rows);
}

/// Where the cached panel came from and how old it is. Cache-only on the
/// owner's side — never a network fetch from a status poll — so an age here is
/// a fact about the cache rather than about the market.
fn provenance(system: &System) -> String {
    let source = or_missing(system.data_source.as_ref());
    match system.data_age_days {
        Some(days) => format!("{source} · {days} d"),
        None => source,
    }
}

/// Which MCP servers are configured — or why the answer is not a list.
///
/// A config file that exists and does not parse is not the same fact as no
/// file. The owner separates them deliberately (collapsing both into "not
/// configured" once sent an operator to re-add an entry that was already
/// there), so this client may not put them back together.
fn mcp(system: &System) -> String {
    if let Some(error) = format::text(system.mcp_config_error.as_ref()) {
        return error.to_string();
    }
    match system.mcp_servers.is_empty() {
        true => "none configured".to_string(),
        false => system.mcp_servers.join(" "),
    }
}

fn availability(flag: Option<bool>) -> String {
    match flag {
        Some(true) => "available".to_string(),
        Some(false) => "absent".to_string(),
        None => MISSING.to_string(),
    }
}

/// What this desk is watching — the polled universe, named as such.
///
/// The mandate's whitelist is not in the snapshot. What *is* in it is the
/// market section the owner built from that whitelist, plus anything the book
/// holds outside it, and a client that labelled that "the mandate universe"
/// would be asserting a configuration it cannot see.
fn draw_universe(f: &mut Frame, area: Rect, store: &Store) {
    let t = theme();
    let symbols = store.universe();
    if symbols.is_empty() {
        card(
            f,
            area,
            "universe",
            vec![absent("no universe in the last snapshot")],
        );
        return;
    }
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let rows = Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(inner);
    f.render_widget(
        Paragraph::new(vec![
            panel_header("universe"),
            kv(
                "watching",
                format!("{} symbols", symbols.len()),
                t.text_primary,
            ),
        ]),
        rows[0],
    );
    // Wrapped rather than clipped: a symbol list that lost its tail reads as a
    // smaller universe than the desk is actually holding.
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" {}", symbols.join(" ")),
            Style::default().fg(t.cyan),
        )))
        .wrap(Wrap { trim: false }),
        rows[1],
    );
}

fn draw_theme(f: &mut Frame, area: Rect) {
    let t = theme();
    card(
        f,
        area,
        "theme",
        vec![kv("palette", palette().to_string(), t.text_primary)],
    );
}

/// One headed card: the header, its rows, and the rule the block reserves.
///
/// Wrapped rather than clipped. Every row here is sized to fit at the baseline
/// width, but a value the owner made longer than this client expected — a
/// credential error, an MCP parse failure — is a sentence, and half a sentence
/// about why the desk cannot log in is worse than a row that took two lines.
/// `trim: false`, so a wrapped continuation keeps the indentation that says it
/// belongs to the row above rather than starting a new one.
fn card(f: &mut Frame, area: Rect, title: &str, rows: Vec<Line<'static>>) {
    let block = panel_block();
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines = vec![panel_header(title)];
    lines.extend(rows);
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

/// A label/value row, aligned so a column of them reads as a column.
fn kv(label: &str, value: String, tone: ratatui::style::Color) -> Line<'static> {
    let t = theme();
    Line::from(vec![
        Span::styled(
            format!(" {label:<LABEL_W$}"),
            Style::default().fg(t.text_secondary),
        ),
        Span::styled(value, Style::default().fg(tone)),
    ])
}

/// What a card says when the owner sent nothing for it. Stated rather than left
/// blank: "nothing configured" and "this pane is broken" must not look the same.
fn absent(what: &str) -> Line<'static> {
    Line::from(Span::styled(
        format!(" {what}"),
        Style::default().fg(theme().text_dim),
    ))
}

fn or_missing(value: Option<&String>) -> String {
    format::text(value).unwrap_or(MISSING).to_string()
}

fn yes_no(flag: Option<bool>) -> String {
    match flag {
        Some(true) => "yes".to_string(),
        Some(false) => "no".to_string(),
        None => MISSING.to_string(),
    }
}

/// A percent at one decimal, or `--`. Absent may not become a number: a
/// constraint the owner did not send is not a constraint of zero.
fn opt_pct1(value: Option<f64>) -> String {
    value
        .map(format::pct1)
        .unwrap_or_else(|| MISSING.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unsent_constraint_never_becomes_a_number() {
        // The whole reason every scalar in the model is an `Option`. `0.0%` in
        // the ceiling row is a mandate that forbids holding anything.
        assert_eq!(opt_pct1(None), MISSING);
        assert_eq!(opt_pct1(Some(0.0)), "0.0%");
        assert_eq!(yes_no(None), MISSING);
        assert_eq!(
            weight_band(&Constraints::default()),
            MISSING,
            "neither end sent is no band at all"
        );
        // One end sent is still a band — with the other end honestly absent,
        // rather than the whole row disappearing and hiding the half that is
        // known.
        assert_eq!(
            weight_band(&Constraints {
                max_weight: Some(0.4),
                ..Default::default()
            }),
            "-- – 40.0%"
        );
    }

    #[test]
    fn a_config_that_does_not_parse_reads_differently_from_no_config() {
        let broken = System {
            mcp_config_error: Some("JSONDecodeError: line 3".into()),
            ..Default::default()
        };
        assert!(mcp(&broken).contains("JSONDecodeError"));
        assert_eq!(mcp(&System::default()), "none configured");
        // `Some("")` is absent, as everywhere: an owner that sent an empty
        // error string is an owner reporting no error.
        let quiet = System {
            mcp_config_error: Some(String::new()),
            mcp_servers: vec!["qlab-operator".into()],
            ..Default::default()
        };
        assert_eq!(mcp(&quiet), "qlab-operator");
    }

    #[test]
    fn the_lane_and_the_login_are_read_off_the_owner_and_not_guessed() {
        assert_eq!(lane(&DeskMode::default()), MISSING);
        assert_eq!(
            lane(&DeskMode {
                offline: Some(false),
                ..Default::default()
            }),
            "online"
        );
        // The simulated book has no login to be broken.
        let sim = DeskMode {
            book: Some("simulated".into()),
            credentials_ok: Some(false),
            ..Default::default()
        };
        assert!(!sim.book_unreachable());
        // Silence about the book that can place real orders is not a clean
        // login — the owner always sends the flag.
        let quiet = DeskMode {
            book: Some("alpaca".into()),
            ..Default::default()
        };
        assert!(quiet.book_unreachable());
    }
}
