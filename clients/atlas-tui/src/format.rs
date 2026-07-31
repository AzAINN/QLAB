//! The number, percent, and money formatting vocabulary shared by every view.
//!
//! Two rules hold everywhere. **Absent is not zero**: a value the owner did not
//! send renders `MISSING`, and a value still in flight renders `PENDING` — the
//! operator must be able to tell "nothing there" from "nothing yet" from "flat".
//! **Sign is carried by the glyph**, so magnitudes are always absolute and the
//! arrow or the leading `+`/`-` says the direction.
//!
//! Percent helpers take *fractions*, because that is what the owner sends:
//! `change_1d`, `weight`, `drawdown`, and `unrealized_pnl_pct` are all ratios,
//! never pre-multiplied percentages.

use crate::theme::theme;
use ratatui::style::Color;

/// A value the owner did not send.
pub const MISSING: &str = "--";
/// A value that has been asked for and has not arrived. Deliberately distinct
/// from `MISSING`: "not yet" and "not there" are different facts about the desk.
pub const PENDING: &str = "…";

/// A string the owner actually set.
///
/// The owner serialises a string it never set as `""`, so absent and empty are
/// one fact here. Every surface that prints an optional string goes through
/// this: a view that told the two apart would print a blank where it means to
/// print `MISSING`, and the store would diff a flash onto every sparse payload.
pub fn text(value: Option<&String>) -> Option<&str> {
    value.map(String::as_str).filter(|s| !s.is_empty())
}

/// `text`, resolved for display.
pub fn or_missing(value: Option<&String>) -> &str {
    text(value).unwrap_or(MISSING)
}

/// A state word as the workstation prints one: uppercase, or `MISSING`.
///
/// Here rather than at each call site because a panel that disagreed with its
/// neighbours about casing would read as a different kind of thing — the same
/// reason `panel_header` uppercases its title.
pub fn upper(value: Option<&str>) -> String {
    value
        .map(str::to_uppercase)
        .unwrap_or_else(|| MISSING.to_string())
}

/// A fraction at 1 dp, or `MISSING` when the owner did not send one. The one
/// spelling of it: three surfaces wrote this out, which is how three of them end
/// up disagreeing about what an absent percentage looks like.
pub fn opt_pct(value: Option<f64>) -> String {
    value.map(pct1).unwrap_or_else(|| MISSING.to_string())
}

/// Thousands-grouped currency. Rust has no `{:,}`, and an ungrouped equity
/// figure is genuinely harder to read at a glance on a book this size.
pub fn money(value: f64) -> String {
    let Some((negative, digits)) = fixed(value, 2) else {
        return MISSING.to_string();
    };
    format!("{}${}", if negative { "-" } else { "" }, group(&digits))
}

/// Currency that always states its sign — P&L, flows, anything where `+` is
/// information rather than noise. Zero reads `+`, matching `change_tone`.
pub fn signed_money(value: f64) -> String {
    let Some((negative, digits)) = fixed(value, 2) else {
        return MISSING.to_string();
    };
    format!("{}${}", if negative { "-" } else { "+" }, group(&digits))
}

/// Money at ribbon altitude: three significant figures and a unit suffix.
///
/// `money` is unbounded in width — a nine-figure book is six cells wider than a
/// five-figure one — and a KPI chip is sized once, at build time, for the widest
/// thing it can hold. A line that grew past its cell would be clipped by the
/// `Paragraph` that draws it, which turns `$6,821.21` into `$6,821.` — a number
/// that is wrong rather than one that is coarse. So the headline figures use
/// `money` and the chips beside them use this.
///
/// `B` is the top band, as in `compact_volume`: a larger figure renders as
/// thousands of `B` rather than inventing a unit.
pub fn compact_money(value: f64) -> String {
    match compact(value) {
        Some((negative, digits)) => format!("{}{digits}", if negative { "-" } else { "" }),
        None => MISSING.to_string(),
    }
}

/// `compact_money` that always states its sign — a P&L column, where `+` is
/// information rather than noise, and where a nine-figure winner still has to
/// fit the cell the column was sized for. Zero reads `+`, matching
/// `signed_money` and `change_tone`.
///
/// Nine characters through `±$999.99B`, which is the range `compact_money`
/// bands into; past it both spellings grow, for the reason that function
/// documents — `B` is the top band and a larger figure renders as thousands of
/// `B` rather than inventing a unit.
pub fn signed_compact_money(value: f64) -> String {
    match compact(value) {
        Some((negative, digits)) => format!("{}{digits}", if negative { "-" } else { "+" }),
        None => MISSING.to_string(),
    }
}

/// Whether the *rounded* value is negative, and its banded magnitude with the
/// `$` and the unit suffix — everything but the sign, which the two callers
/// spell differently.
fn compact(value: f64) -> Option<(bool, String)> {
    // The sign comes off the *rounded* value, so a cash balance of -1e-13 — what
    // a fully-invested paper book actually carries — is not drawn as a debt.
    let (negative, _) = fixed(value, 2)?;
    let mut magnitude = value.abs();
    let mut suffix = "";
    // Banded on the rounded value: 999_999 would otherwise print `1000.00K`, a
    // number whose digits disagree with its own unit.
    for next in ["K", "M", "B"] {
        if magnitude < 999.995 {
            break;
        }
        magnitude /= 1000.0;
        suffix = next;
    }
    Some((negative, format!("${magnitude:.2}{suffix}")))
}

/// A fraction as a signed percentage: `0.0123` → `+1.23%`.
pub fn signed_pct(fraction: f64) -> String {
    let Some((negative, digits)) = fixed(fraction * 100.0, 2) else {
        return MISSING.to_string();
    };
    format!("{}{}%", if negative { "-" } else { "+" }, digits)
}

/// A fraction as a signed percentage at 1 dp: `0.0123` → `+1.2%`.
///
/// The heat cell's spelling. `signed_pct` runs to eight cells at three figures
/// and a holding cell is eleven wide including its ticker; the second decimal is
/// the one thing on that cell a colour ramp has not already said. Signed, unlike
/// `pct1`, because the direction is the whole question a P&L cell answers.
pub fn signed_pct1(fraction: f64) -> String {
    let Some((negative, digits)) = fixed(fraction * 100.0, 1) else {
        return MISSING.to_string();
    };
    format!("{}{}%", if negative { "-" } else { "+" }, digits)
}

/// A fraction as an unsigned-unless-negative percentage at 1 dp — weights,
/// allocations, exposures, where two decimals is more precision than the
/// number carries.
pub fn pct1(fraction: f64) -> String {
    let Some((negative, digits)) = fixed(fraction * 100.0, 1) else {
        return MISSING.to_string();
    };
    format!("{}{}%", if negative { "-" } else { "" }, digits)
}

/// Share counts at desk scale. Zero volume is not a measurement — the owner
/// sends it for assets it has no volume for — so it renders `MISSING`.
///
/// `B` is the top band: the payload has no volumes above a trillion, and a
/// larger one renders as thousands of `B` rather than silently inventing a unit.
pub fn compact_volume(shares: i64) -> String {
    if shares == 0 {
        return MISSING.to_string();
    }
    let sign = if shares < 0 { "-" } else { "" };
    let mut value = (shares as f64).abs();
    let mut suffix = "";
    // Banded on the *rounded* value: 999_999 would otherwise print "1000.00K",
    // a number whose digits disagree with its own unit.
    for next in ["K", "M", "B"] {
        if value < 999.995 {
            break;
        }
        value /= 1000.0;
        suffix = next;
    }
    if suffix.is_empty() {
        return format!("{sign}{value:.0}");
    }
    format!("{sign}{value:.2}{suffix}")
}

/// Whether a number still reads negative once rounded to the `dp` decimals it is
/// **printed** at.
///
/// The one rule behind both halves of a change on screen. `fixed` already takes
/// the digits' sign from the rounded value, so `-0.00001` prints `0.00%`; every
/// arrow and every colour beside it read the raw double, so the same number
/// rendered `▲0.00%` in red — a glyph and a shade contradicting each other about
/// one figure, which is the kind of thing an operator learns to distrust the
/// whole surface over.
///
/// Callers pass the value *and the precision they are formatting with*, which is
/// what keeps the two in step. The percent helpers all print `fraction * 100`,
/// so a percent call site passes that — the same expression the formatter beside
/// it uses internally.
pub fn negative_at(printed: f64, dp: usize) -> bool {
    matches!(fixed(printed, dp), Some((true, _)))
}

/// Whether a number rounds away to nothing at the `dp` decimals it is printed at.
///
/// The third state `change_tone` does not have. Zero is positive by contract for
/// a single hero figure — flat is not a loss — and wrong for a *column* of them:
/// a paper book that opened flat would render as ten green rows, a claim the desk
/// made money on all ten. Surfaces that need "neither" ask this first, and get
/// the answer at the precision they print rather than off the raw double.
///
/// Absent is the caller's to handle, exactly as with `negative_at`: a value that
/// is not finite is not flat, it is unmeasured, and both callers guard it before
/// they get here.
pub fn zero_at(printed: f64, dp: usize) -> bool {
    matches!(fixed(printed, dp), Some((_, digits)) if is_zero(&digits))
}

/// The colour of a change, decided at the precision it is printed to.
///
/// Replaces `Theme::change`, which read the raw double. Absent — anything that
/// is not finite — is neither a rise nor a fall, and takes the tone for "the
/// owner declined to say" rather than the green that `>= 0.0` would have given
/// it. Zero is positive by contract, unchanged: flat is not a loss.
pub fn change_tone(printed: f64, dp: usize) -> Color {
    let t = theme();
    match fixed(printed, dp) {
        Some((true, _)) => t.negative,
        Some((false, _)) => t.positive,
        None => t.text_secondary,
    }
}

/// A change as arrow-plus-magnitude, with the colour that goes with it. The
/// number is absolute: the glyph is the sign, and printing both reads as a
/// double negative.
pub fn arrow_chg(value: f64) -> (String, Color) {
    let t = theme();
    if !value.is_finite() {
        return (MISSING.to_string(), t.text_secondary);
    }
    let arrow = if negative_at(value, 2) { "▼" } else { "▲" };
    (format!("{arrow} {:.2}", value.abs()), change_tone(value, 2))
}

/// A change as arrow-plus-percent, tight enough for the ticker: `▲1.34%`.
///
/// No space after the glyph, unlike `arrow_chg`: the ticker separates its
/// triplets by three spaces, and a fourth inside one would break the rhythm the
/// eye reads the row by.
pub fn arrow_pct(fraction: f64) -> String {
    let Some((negative, digits)) = fixed(fraction * 100.0, 2) else {
        return MISSING.to_string();
    };
    format!("{}{}%", if negative { "▼" } else { "▲" }, digits)
}

/// How long ago, at the coarsest unit that still says it: `47s`, `9m`, `2h`.
///
/// `STALE 3600s` is a number an operator has to divide before it means anything,
/// and the thing it is competing with for attention is the desk. One unit rather
/// than two (`1h 3m`) because the chip is read at a glance and the question it
/// answers is "roughly how far behind", not "how far behind exactly" — the
/// second figure is precision nobody spends.
///
/// Truncating, never rounding: 119 seconds is `1m`, because a chip that says
/// `2m` about a mark ninety seconds old has aged it past what it is.
pub fn age(elapsed: std::time::Duration) -> String {
    let secs = elapsed.as_secs();
    match secs {
        0..=59 => format!("{secs}s"),
        60..=3_599 => format!("{}m", secs / 60),
        _ => format!("{}h", secs / 3_600),
    }
}

// -- the owner's clock -----------------------------------------------------
//
// This client has no wall clock in its render path — see `store::Store` on time
// as data — so anything it says about *when* has to come from stamps the owner
// wrote. Both helpers below read the owner's own ISO-8601 text and never
// convert it: the registry writes UTC with a fixed offset
// (`qlab/state/registry.py::_now`), a surface renders the time the owner
// stated, and a timezone conversion here would be this client inventing a clock
// the audit log does not have.

/// `HH:MM:SS` out of an ISO stamp, or absent.
///
/// A slice of the owner's string rather than a parse: the four surfaces that
/// print a registry timestamp must agree to the character, and a half-read
/// stamp is worse than none because it still looks like a time.
///
/// Promoted here from `views::audit` once BOOK and WORKFORCE wanted it too —
/// three copies of "which characters of an ISO stamp are the clock" is three
/// chances for one of them to slip a digit.
pub fn clock(stamp: Option<&String>) -> Option<String> {
    let text = text(stamp)?;
    let time: String = text.chars().skip(11).take(8).collect();
    (time.chars().count() == 8).then_some(time)
}

/// How long the owner's own two stamps are apart, at [`age`]'s coarseness.
///
/// **Not "how long ago".** There is no `now` in this expression: both ends are
/// stamps the owner wrote, so what this measures is the span the owner has
/// actually recorded — for a finished workflow its whole run, and for a live one
/// the time from its start to the last thing it did. A client that subtracted a
/// stamp from its own clock would be asserting agreement between two machines'
/// clocks *and* reading the time inside a renderer, and would drift silently on
/// both counts.
///
/// `None` unless both stamps parse **and carry the same offset**: two stamps in
/// different zones are not a duration this client may guess at. A `to` before
/// `from` is likewise refused rather than rendered as zero — a negative span is
/// a broken contract, and `0s` would hide it.
pub fn span(from: Option<&String>, to: Option<&String>) -> Option<String> {
    let (from, to) = (text(from)?, text(to)?);
    if offset_of(from) != offset_of(to) {
        return None;
    }
    let elapsed = civil_secs(to)?.checked_sub(civil_secs(from)?)?;
    (elapsed >= 0).then(|| age(std::time::Duration::from_secs(elapsed as u64)))
}

/// The zone suffix of an ISO stamp — `+00:00`, `Z`, or nothing at all.
///
/// Searched from the seconds field onward, because the date's own separators are
/// `-` too and a naive search would call `2026-07-30` an offset.
fn offset_of(stamp: &str) -> &str {
    match stamp
        .char_indices()
        .find(|(i, c)| *i >= 19 && matches!(c, '+' | '-' | 'Z' | 'z'))
    {
        Some((at, _)) => &stamp[at..],
        None => "",
    }
}

/// Seconds since the civil epoch, from the date and time fields of an ISO stamp.
///
/// Fixed offsets rather than a parser: the owner writes exactly
/// `YYYY-MM-DDTHH:MM:SS[.ffffff][±HH:MM]`, and anything that does not have
/// digits in those positions is refused rather than guessed at.
fn civil_secs(stamp: &str) -> Option<i64> {
    let at = |from: usize, len: usize| -> Option<i64> {
        let field: String = stamp.chars().skip(from).take(len).collect();
        (field.chars().count() == len && field.chars().all(|c| c.is_ascii_digit()))
            .then(|| field.parse().ok())
            .flatten()
    };
    let (y, m, d) = (at(0, 4)?, at(5, 2)?, at(8, 2)?);
    let (h, min, s) = (at(11, 2)?, at(14, 2)?, at(17, 2)?);
    Some(days_from_civil(y, m, d)? * 86_400 + h * 3_600 + min * 60 + s)
}

/// Days since 1970-01-01, by Howard Hinnant's civil-calendar algorithm.
///
/// Proleptic Gregorian and exact for every date the registry can write, which is
/// what a duration between two stamps needs — an approximation of a month would
/// put a run that crossed a month boundary hours out.
fn days_from_civil(y: i64, m: i64, d: i64) -> Option<i64> {
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    let y = y - i64::from(m <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some(era * 146_097 + doe - 719_468)
}

/// A quote. Two decimals above a dollar, four below it — sub-dollar instruments
/// carry their information in the third and fourth places, and truncating there
/// makes distinct prices look identical.
pub fn price(value: f64) -> String {
    if !value.is_finite() {
        return MISSING.to_string();
    }
    if value.abs() > 1.0 {
        format!("{value:.2}")
    } else {
        format!("{value:.4}")
    }
}

/// Absolute digits at `dp` decimals, plus whether the *rounded* value is
/// negative. Taking the sign from the rounded value is what keeps `-0.001`
/// from rendering as `-$0.00`, a loss that is not there.
fn fixed(value: f64, dp: usize) -> Option<(bool, String)> {
    if !value.is_finite() {
        return None;
    }
    let digits = format!("{:.*}", dp, value.abs());
    Some((value < 0.0 && !is_zero(&digits), digits))
}

/// Whether already-formatted digits are all zero — the test `fixed` makes to
/// decide a sign, and the one `zero_at` makes to decide there is no direction.
/// One spelling, because two would be two answers to "did this round away".
fn is_zero(digits: &str) -> bool {
    digits.bytes().all(|b| b == b'0' || b == b'.')
}

/// Comma-group the integer part of an already-formatted decimal string.
fn group(digits: &str) -> String {
    let (whole, frac) = digits.split_once('.').unwrap_or((digits, ""));
    let mut out = String::with_capacity(digits.len() + whole.len() / 3);
    for (i, ch) in whole.chars().enumerate() {
        if i > 0 && (whole.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    if !frac.is_empty() {
        out.push('.');
        out.push_str(frac);
    }
    out
}

#[cfg(test)]
mod clock_tests {
    use super::*;

    fn at(stamp: &str) -> Option<String> {
        clock(Some(&stamp.to_string()))
    }

    fn between(from: &str, to: &str) -> Option<String> {
        span(Some(&from.to_string()), Some(&to.to_string()))
    }

    #[test]
    fn a_clock_is_the_owners_own_wall_time_and_never_a_conversion() {
        assert_eq!(
            at("2026-07-30T18:12:18.773652+00:00"),
            Some("18:12:18".to_string())
        );
        // Absent, empty and malformed are all absent — a half-read stamp is
        // worse than none, because it looks like a time.
        assert_eq!(clock(None), None);
        assert_eq!(clock(Some(&String::new())), None);
        assert_eq!(at("2026-07-30"), None);
    }

    #[test]
    fn a_span_is_the_distance_between_two_stamps_the_owner_wrote() {
        assert_eq!(
            between(
                "2026-07-30T15:29:22.831208+00:00",
                "2026-07-30T15:58:50.091972+00:00"
            ),
            Some("29m".to_string())
        );
        assert_eq!(
            between("2026-07-30T15:29:22+00:00", "2026-07-30T15:29:59+00:00"),
            Some("37s".to_string())
        );
        assert_eq!(
            between("2026-07-30T09:00:00+00:00", "2026-07-30T15:29:22+00:00"),
            Some("6h".to_string())
        );
    }

    #[test]
    fn a_span_crossing_a_month_or_a_leap_day_is_exact_rather_than_approximated() {
        // The whole reason this is a civil-calendar conversion and not
        // arithmetic on the fields: a run that crossed midnight on the last of
        // the month would otherwise come out negative or wildly long.
        assert_eq!(
            between("2026-07-31T23:30:00+00:00", "2026-08-01T00:30:00+00:00"),
            Some("1h".to_string())
        );
        assert_eq!(
            between("2024-02-28T12:00:00+00:00", "2024-03-01T12:00:00+00:00"),
            Some("48h".to_string()),
            "2024 is a leap year, so that is two days and not three"
        );
        assert_eq!(
            between("2023-02-28T12:00:00+00:00", "2023-03-01T12:00:00+00:00"),
            Some("24h".to_string())
        );
    }

    #[test]
    fn a_span_this_client_cannot_be_sure_of_is_absent_rather_than_guessed() {
        // Different zones are not a duration — the client would be inventing
        // agreement between two clocks it cannot see.
        assert_eq!(
            between("2026-07-30T15:00:00+00:00", "2026-07-30T16:00:00+02:00"),
            None
        );
        // A `to` before a `from` is a broken contract, and `0s` would hide it.
        assert_eq!(
            between("2026-07-30T16:00:00+00:00", "2026-07-30T15:00:00+00:00"),
            None
        );
        // Absent, empty and unparseable, as everywhere else in this client.
        assert_eq!(
            span(None, Some(&"2026-07-30T15:00:00+00:00".to_string())),
            None
        );
        assert_eq!(span(Some(&String::new()), Some(&String::new())), None);
        assert_eq!(between("2026-07-30", "2026-07-30"), None);
        assert_eq!(
            between("not-a-stamp-at-all-x", "not-a-stamp-at-all-y"),
            None
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::theme;

    #[test]
    fn arrow_carries_sign_and_magnitude_is_absolute() {
        let (s, c) = arrow_chg(-1.234);
        assert_eq!(s, "▼ 1.23");
        assert_eq!(c, theme().negative);
    }

    #[test]
    fn arrow_pct_carries_the_sign_in_the_glyph_and_stays_tight() {
        assert_eq!(arrow_pct(-0.013394), "▼1.34%");
        assert_eq!(arrow_pct(0.0042), "▲0.42%");
        // A minus that rounds away is not a loss, matching `change_tone`.
        assert_eq!(arrow_pct(-0.00001), "▲0.00%");
        assert_eq!(arrow_pct(f64::NAN), MISSING);
    }

    #[test]
    fn compact_volume_bands() {
        assert_eq!(compact_volume(0), "--");
        assert_eq!(compact_volume(1_234), "1.23K");
        assert_eq!(compact_volume(5_600_000_000), "5.60B");
    }

    #[test]
    fn price_precision_flips_under_one() {
        assert_eq!(price(512.1), "512.10");
        assert_eq!(price(0.4321), "0.4321");
    }

    #[test]
    fn money_groups_thousands_and_leads_with_the_sign() {
        assert_eq!(money(10_000.0), "$10,000.00");
        assert_eq!(money(1_234_567.891), "$1,234,567.89");
        assert_eq!(money(999.5), "$999.50");
        assert_eq!(money(-1_500.25), "-$1,500.25");
        assert_eq!(money(0.0), "$0.00");
    }

    #[test]
    fn compact_money_bands_and_stays_inside_a_chip() {
        assert_eq!(compact_money(0.0), "$0.00");
        assert_eq!(compact_money(999.5), "$999.50");
        assert_eq!(compact_money(6_821.21), "$6.82K");
        assert_eq!(compact_money(1_234_567.0), "$1.23M");
        assert_eq!(compact_money(-1_500.25), "-$1.50K");
        assert_eq!(compact_money(f64::NAN), MISSING);
        // The cash a fully-invested paper book carries. Absent the rounded-sign
        // rule this is `-$0.00`, a debt of nothing.
        assert_eq!(compact_money(6.821210263296962e-13), "$0.00");
        assert_eq!(compact_money(-1e-13), "$0.00");
        // The property the chip is sized on: nine characters, whatever the book.
        for value in [0.0, 999.99, 1e4, 4.2e6, -7.7e9, 1e12, -1e-9] {
            assert!(
                compact_money(value).chars().count() <= 9,
                "{value} rendered {}",
                compact_money(value)
            );
        }
    }

    #[test]
    fn signed_compact_money_states_its_sign_and_stays_inside_a_column() {
        // The blotter's `P&L` column is sized once for the widest thing it can
        // hold, and `signed_money` is unbounded: `+$1,234,567.89` is thirteen
        // cells and clips to `+$1,234,5`, a number wrong by a factor of a
        // hundred. This is that column's spelling of the same fact.
        assert_eq!(signed_compact_money(1_234.56), "+$1.23K");
        assert_eq!(signed_compact_money(-1_234.56), "-$1.23K");
        assert_eq!(signed_compact_money(0.0), "+$0.00");
        assert_eq!(signed_compact_money(628.33), "+$628.33");
        assert_eq!(signed_compact_money(f64::NAN), MISSING);
        // A minus that rounds away is not a loss, exactly as elsewhere.
        assert_eq!(signed_compact_money(-1e-13), "+$0.00");
        // The property the column is sized on: nine characters across the range
        // the bands cover, sign included. Past `±$999.99B` this grows by the
        // one character `compact_money` grows by, which is the ceiling that
        // function documents rather than a second rule.
        assert_eq!(signed_compact_money(999.99e9), "+$999.99B");
        assert_eq!(signed_compact_money(1e12), "+$1000.00B");
        for value in [0.0, 999.99, 1e4, 4.2e6, -7.7e9, 999.99e9, -1e-9] {
            assert!(
                signed_compact_money(value).chars().count() <= 9,
                "{value} rendered {}",
                signed_compact_money(value)
            );
        }
        // The unsigned spelling is the same number without the leading `+`.
        for value in [0.0, 628.33, 1_234.56, 4.2e6] {
            assert_eq!(
                signed_compact_money(value),
                format!("+{}", compact_money(value))
            );
        }
    }

    #[test]
    fn signed_money_always_shows_its_sign() {
        assert_eq!(signed_money(1_234.56), "+$1,234.56");
        assert_eq!(signed_money(-1_234.56), "-$1,234.56");
        assert_eq!(signed_money(0.0), "+$0.00");
    }

    #[test]
    fn percents_take_fractions_because_that_is_what_the_owner_sends() {
        assert_eq!(signed_pct(0.0123), "+1.23%");
        assert_eq!(signed_pct(-0.0018), "-0.18%");
        assert_eq!(pct1(0.2534), "25.3%");
        assert_eq!(pct1(-0.125), "-12.5%");
        // One decimal and always signed — the heat cell's eleven columns hold a
        // ticker and this, and a `+` is the half of it a ramp cannot state.
        assert_eq!(signed_pct1(0.0123), "+1.2%");
        assert_eq!(signed_pct1(-0.1254), "-12.5%");
        assert_eq!(signed_pct1(0.0), "+0.0%");
    }

    #[test]
    fn a_rounded_away_minus_does_not_survive_as_a_sign() {
        assert_eq!(money(-0.001), "$0.00");
        assert_eq!(signed_pct(-0.00001), "+0.00%");
    }

    #[test]
    fn the_glyph_and_the_colour_are_decided_at_the_precision_the_number_prints_at() {
        let t = theme();
        // The bug, from both sides of zero: a magnitude that rounds away is not
        // a direction, and a colour that says otherwise contradicts the digits
        // it is painting. `Theme::change` read the raw double and got this
        // wrong on four surfaces at once.
        for tiny in [-1e-13, -0.004, -0.0049999] {
            assert!(!negative_at(tiny, 2), "{tiny}");
            assert_eq!(change_tone(tiny, 2), t.positive, "{tiny}");
        }
        // The other side of the same boundary: a magnitude that survives the
        // rounding still reads as the loss it is.
        for real in [-0.005, -0.006, -1.0] {
            assert!(negative_at(real, 2), "{real}");
            assert_eq!(change_tone(real, 2), t.negative, "{real}");
        }
        // Positive is positive at every size, and zero is positive by contract:
        // flat is not a loss, and the reference desk paints it green.
        for up in [0.0, 1e-13, 0.005, 1.0] {
            assert!(!negative_at(up, 2));
            assert_eq!(change_tone(up, 2), t.positive, "{up}");
        }

        // The precision is the caller's, because it is the one they print at.
        assert!(!negative_at(-0.04, 1), "-0.04 prints 0.0");
        assert!(negative_at(-0.05, 1));
        assert!(
            negative_at(-0.04, 2),
            "and at two decimals it is a loss again"
        );

        // Absent is neither a rise nor a fall.
        assert_eq!(change_tone(f64::NAN, 2), t.text_secondary);
        assert_eq!(change_tone(f64::INFINITY, 2), t.text_secondary);
        assert!(!negative_at(f64::NAN, 2));
    }

    #[test]
    fn the_arrow_and_its_colour_never_disagree() {
        // `arrow_chg` had the glyph off the rounded value and the colour off the
        // raw one, so this rendered a green-shaped `▲` in red.
        let (text, tone) = arrow_chg(-0.001);
        assert_eq!(text, "▲ 0.00");
        assert_eq!(tone, theme().positive);

        let (text, tone) = arrow_chg(-0.006);
        assert_eq!(text, "▼ 0.01");
        assert_eq!(tone, theme().negative);

        // And the whole vocabulary agrees about one number: the glyph the
        // percent helper prints and the colour beside it come off one rounding.
        for value in [-1e-9, -0.004999, 0.0, 0.004999, 12.5, -12.5] {
            let printed = arrow_pct(value / 100.0);
            let tone = change_tone(value, 2);
            assert_eq!(
                printed.starts_with('▼'),
                tone == theme().negative,
                "{value} printed {printed} in a colour that disagrees"
            );
        }
    }

    #[test]
    fn absent_reads_as_missing_never_as_zero() {
        assert_eq!(money(f64::NAN), "--");
        assert_eq!(price(f64::INFINITY), "--");
        assert_eq!(pct1(f64::NAN), "--");
        assert_eq!(signed_money(f64::NAN), "--");
        assert_eq!(signed_pct(f64::NAN), "--");
        assert_eq!(signed_pct1(f64::NAN), "--");
        assert_eq!(arrow_chg(f64::NAN).0, "--");
        assert_eq!(MISSING, "--");
        assert_ne!(MISSING, PENDING);
    }

    #[test]
    fn an_unset_owner_string_is_absent_rather_than_empty() {
        assert_eq!(text(Some(&String::new())), None);
        assert_eq!(text(None::<&String>), None);
        assert_eq!(text(Some(&"calm".to_string())), Some("calm"));
        assert_eq!(or_missing(Some(&String::new())), MISSING);
        assert_eq!(or_missing(Some(&"alpaca".to_string())), "alpaca");
        assert_eq!(upper(Some("calm")), "CALM");
        assert_eq!(upper(None), MISSING);
        assert_eq!(opt_pct(Some(-0.125)), "-12.5%");
        assert_eq!(opt_pct(None), MISSING);
    }

    #[test]
    fn an_age_states_the_coarsest_unit_that_still_says_it() {
        use std::time::Duration;
        let age = |secs| super::age(Duration::from_secs(secs));
        assert_eq!(age(0), "0s");
        assert_eq!(age(47), "47s");
        // The band edges, both sides. `STALE 3600s` was the finding.
        assert_eq!(age(59), "59s");
        assert_eq!(age(60), "1m");
        assert_eq!(
            age(119),
            "1m",
            "truncated, so a mark is never aged past itself"
        );
        assert_eq!(age(3_599), "59m");
        assert_eq!(age(3_600), "1h");
        assert_eq!(age(7_200), "2h");
        assert_eq!(
            age(86_400),
            "24h",
            "a day is hours, not a new unit nobody asked for"
        );
        // Four characters through a day, which is what lets the chip run sit at
        // a fixed width beside everything else on the status line.
        for secs in [0, 59, 60, 3_599, 3_600, 86_400] {
            assert!(
                age(secs).chars().count() <= 4,
                "{secs} rendered {}",
                age(secs)
            );
        }
    }

    #[test]
    fn compact_volume_promotes_at_the_band_edge() {
        assert_eq!(compact_volume(999), "999");
        assert_eq!(compact_volume(999_999), "1.00M");
        assert_eq!(compact_volume(-1_500_000), "-1.50M");
    }
}
