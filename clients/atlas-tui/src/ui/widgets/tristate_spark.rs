//! Sparklines drawn as text, and the three things a trend cell can honestly say.
//!
//! Ratatui's `Sparkline` is a `Widget` and a `Table` cell holds `Text`, so a
//! real sparkline cannot live in a table column. These are the same eight
//! levels drawn as text.
//!
//! Promoted out of `views/markets.rs` when BOOK's blotter grew a `TREND`
//! column, by the rule `widgets/mod.rs` states. Two views quantizing "the tail
//! of a series" would be two chances to disagree about what a rising window
//! looks like — and the colour rule (the *window's* slope, never the whole
//! series') is one that had to be fixed once already.

use crate::format::MISSING;
use crate::theme::theme;
use ratatui::style::Style;

/// How many bars of the tail a spark cell draws. Eight, matching the eight
/// levels: a window narrower than the ramp cannot use all of it.
pub const SPARK_W: usize = 8;

/// Eight levels, low to high.
const SPARK_GLYPHS: [char; 8] = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];

/// The ticker is on the desk and its closes are not — an empty series, or one
/// the owner stopped sending. A *broken* line, because at this dimness a solid
/// one is the one shape that could be misread as a price that did not move.
const STALE: &str = "╌╌╌╌";

/// The ticker is not in the market section at all, so there is no series to be
/// stale about. A held position outside the polled universe is a real state —
/// the owner values it and does not chart it — so it is said in the ramp step
/// this client uses everywhere else for "nobody measured this".
const ABSENT: &str = "────";

/// The window a spark is drawn from: the last `width` closes, or all of them.
///
/// One definition, because the glyphs and the colour must be reading the same
/// slice. Two spellings of "the tail" is how the bars came to say one thing and
/// the colour another.
pub fn tail(history: &[f64], width: usize) -> &[f64] {
    &history[history.len().saturating_sub(width)..]
}

/// The last `width` closes, quantized into the eight block glyphs.
///
/// Scaled to the window rather than the whole series: the cell's job is the
/// recent shape, and an outlier twenty bars back would flatten every bar the
/// operator is actually looking at. `history` is finite by construction — JSON
/// carries no NaN, so the model cannot decode one.
pub fn glyphs(history: &[f64], width: usize) -> String {
    if history.is_empty() || width == 0 {
        return String::new();
    }
    let tail = tail(history, width);
    let lo = tail.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = tail.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let span = hi - lo;
    let top = SPARK_GLYPHS.len() - 1;
    tail.iter()
        .map(|value| {
            let level = if span > 0.0 {
                (((value - lo) / span) * top as f64).round() as usize
            } else {
                // A series with no range is neither at the top of its range nor
                // at the bottom of it. A row of `▁` claims one and a row of `█`
                // claims the other; the middle claims neither.
                SPARK_GLYPHS.len() / 2 - 1
            };
            SPARK_GLYPHS[level.min(top)]
        })
        .collect()
}

/// The 8-level quantize of a series tail, and the tone that says which way it
/// went.
///
/// Two states: a series, or the one spelling of a value there is none of. A
/// column whose rows can also be *missing a ticker* wants `tristate` instead.
pub fn cell(history: &[f64]) -> (String, Style) {
    let t = theme();
    let bars = glyphs(history, SPARK_W);
    if bars.is_empty() {
        return (MISSING.to_string(), Style::default().fg(t.text_tertiary));
    }
    // Slope over the window the cell actually draws — the same slice `glyphs`
    // quantized, not the whole series. The reference desk colours a sparkline by
    // its own visible direction, and it has to: a tail climbing out of a crash
    // painted red says the bars on screen are falling, which they are not.
    let window = tail(history, SPARK_W);
    let rising = match (window.first(), window.last()) {
        (Some(first), Some(last)) => last >= first,
        _ => true,
    };
    (
        bars,
        Style::default().fg(if rising { t.positive } else { t.negative }),
    )
}

/// A trend cell over a history that may not exist at all.
///
/// Three states, three shapes, because collapsing any two of them loses a fact
/// the operator needs: bars mean the desk charted this and it moved that way,
/// `╌╌╌╌` means the ticker is polled and carries no closes, and `────` means the
/// position is held outside the polled universe. A single `--` for the last two
/// would say "no data" where the honest answer is "no *series*, and here is
/// which kind".
pub fn tristate(history: Option<&[f64]>) -> (String, Style) {
    let t = theme();
    match history {
        None => (ABSENT.to_string(), Style::default().fg(t.text_tertiary)),
        Some([]) => (STALE.to_string(), Style::default().fg(t.border_med)),
        Some(history) => cell(history),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_spark_quantizes_a_known_series_into_the_eight_glyphs() {
        // A `Sparkline` cannot live inside a `Table` cell, so the row draws its
        // own. Pinning the exact string is the only way a quantizer that is
        // subtly off by a level fails loudly rather than looking plausible.
        assert_eq!(
            glyphs(&[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], 8),
            "▁▂▃▄▅▆▇█"
        );
        assert_eq!(
            glyphs(&[8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0], 8),
            "█▇▆▅▄▃▂▁"
        );
    }

    #[test]
    fn the_spark_reads_the_tail_and_scales_to_it() {
        // The window is the recent shape, so an old outlier must not flatten
        // every bar the operator is actually looking at.
        assert_eq!(glyphs(&[100.0, 1.0, 2.0, 3.0], 3), "▁▅█");
    }

    #[test]
    fn the_spark_takes_its_colour_from_the_window_it_draws() {
        // The bars are the window, so the colour has to be the window's. Read
        // off the whole series instead, a tail climbing out of a crash paints
        // red — the cell then says the eight bars on screen are falling while
        // they visibly rise, which is the one thing a sparkline must not do.
        let crashed_then_climbing = [100.0, 50.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0];
        let (bars, style) = cell(&crashed_then_climbing);
        assert_eq!(bars, "▁▂▃▄▅▆▇█", "the window is the last eight closes");
        assert_eq!(
            style.fg,
            Some(theme().positive),
            "a rising window painted as a fall"
        );

        // The mirror, so this cannot be satisfied by colouring everything green.
        let rallied_then_sliding = [1.0, 2.0, 100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0];
        assert_eq!(cell(&rallied_then_sliding).1.fg, Some(theme().negative));
    }

    #[test]
    fn a_flat_series_reads_from_the_middle_rather_than_the_floor() {
        // A row of `▁` reads as "at the bottom of its range" and a row of `█` as
        // "at the top". A series with no range is neither.
        assert_eq!(glyphs(&[5.0, 5.0, 5.0], 3), "▄▄▄");
        assert_eq!(glyphs(&[5.0], 4), "▄");
    }

    #[test]
    fn a_series_the_owner_did_not_send_draws_nothing_at_all() {
        assert_eq!(glyphs(&[], 8), "");
    }

    #[test]
    fn the_three_trend_states_are_three_distinct_renderings() {
        let t = theme();
        // Charted: the bars and the window's own direction.
        let (bars, style) = tristate(Some(&[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]));
        assert_eq!(bars, "▁▂▃▄▅▆▇█");
        assert_eq!(style.fg, Some(t.positive));

        // Polled and carrying no closes: a broken line, at the dimmest step.
        assert_eq!(
            tristate(Some(&[])),
            ("╌╌╌╌".to_string(), Style::default().fg(t.border_med))
        );

        // Not in the market section at all: a solid rule, one step brighter.
        assert_eq!(
            tristate(None),
            ("────".to_string(), Style::default().fg(t.text_tertiary))
        );
    }

    #[test]
    fn the_two_empty_states_are_not_the_same_cell() {
        // The bug this shape exists to prevent: collapsing "polled, no closes"
        // and "not polled at all" into one `--` says "no data" where the honest
        // answer is which of the two kinds of nothing it is.
        assert_ne!(tristate(Some(&[])), tristate(None));
        assert_ne!(tristate(Some(&[])).0, MISSING);
        assert_ne!(tristate(None).0, MISSING);
        // And neither is drawable as a series: a `▄▄▄▄` here would read as a
        // price that was measured and did not move.
        for (bars, _) in [tristate(Some(&[])), tristate(None)] {
            assert!(
                !bars.chars().any(|c| SPARK_GLYPHS.contains(&c)),
                "{bars} draws as a measured series"
            );
        }
    }

    #[test]
    fn a_one_point_history_is_a_series_and_not_an_empty_one() {
        // One close is a measurement. Routing it to the stale line would call a
        // fresh single mark "no data".
        let (bars, _) = tristate(Some(&[7.0]));
        assert_eq!(bars, "▄");
    }
}
