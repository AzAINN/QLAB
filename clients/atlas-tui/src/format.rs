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

/// Thousands-grouped currency. Rust has no `{:,}`, and an ungrouped equity
/// figure is genuinely harder to read at a glance on a book this size.
pub fn money(value: f64) -> String {
    let Some((negative, digits)) = fixed(value, 2) else {
        return MISSING.to_string();
    };
    format!("{}${}", if negative { "-" } else { "" }, group(&digits))
}

/// Currency that always states its sign — P&L, flows, anything where `+` is
/// information rather than noise. Zero reads `+`, matching `Theme::change`.
pub fn signed_money(value: f64) -> String {
    let Some((negative, digits)) = fixed(value, 2) else {
        return MISSING.to_string();
    };
    format!("{}${}", if negative { "-" } else { "+" }, group(&digits))
}

/// A fraction as a signed percentage: `0.0123` → `+1.23%`.
pub fn signed_pct(fraction: f64) -> String {
    let Some((negative, digits)) = fixed(fraction * 100.0, 2) else {
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

/// A change as arrow-plus-magnitude, with the colour that goes with it. The
/// number is absolute: the glyph is the sign, and printing both reads as a
/// double negative.
pub fn arrow_chg(value: f64) -> (String, Color) {
    let t = theme();
    if !value.is_finite() {
        return (MISSING.to_string(), t.text_secondary);
    }
    let arrow = if value >= 0.0 { "▲" } else { "▼" };
    (format!("{arrow} {:.2}", value.abs()), t.change(value))
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
    let is_zero = digits.bytes().all(|b| b == b'0' || b == b'.');
    Some((value < 0.0 && !is_zero, digits))
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
    }

    #[test]
    fn a_rounded_away_minus_does_not_survive_as_a_sign() {
        assert_eq!(money(-0.001), "$0.00");
        assert_eq!(signed_pct(-0.00001), "+0.00%");
    }

    #[test]
    fn absent_reads_as_missing_never_as_zero() {
        assert_eq!(money(f64::NAN), "--");
        assert_eq!(price(f64::INFINITY), "--");
        assert_eq!(pct1(f64::NAN), "--");
        assert_eq!(signed_money(f64::NAN), "--");
        assert_eq!(signed_pct(f64::NAN), "--");
        assert_eq!(arrow_chg(f64::NAN).0, "--");
        assert_eq!(MISSING, "--");
        assert_ne!(MISSING, PENDING);
    }

    #[test]
    fn compact_volume_promotes_at_the_band_edge() {
        assert_eq!(compact_volume(999), "999");
        assert_eq!(compact_volume(999_999), "1.00M");
        assert_eq!(compact_volume(-1_500_000), "-1.50M");
    }
}
