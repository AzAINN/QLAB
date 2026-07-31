//! The quantized heat ramp: six steps, one arithmetic, however many surfaces.
//!
//! Promoted out of `views/markets.rs` when BOOK grew a holdings heatmap. What is
//! shared is the *spend* — how six steps of intensity are drawn on a grid that
//! has no alpha — and not the bands, which are a fact about the quantity each
//! surface is measuring: 2% is a large move for a sector and a rounding error
//! for a position's P&L. Two copies of the spend is how two heat surfaces end up
//! disagreeing about what "brightest" looks like. The quantizer a surface needs
//! lands here when that surface does; nothing is exported ahead of its caller.
//!
//! A cell grid has no alpha, so "six alpha steps" is spent as three levels of
//! the depth ramp and then the semantic pair itself — the same technique
//! `fx::style_for` uses to fade a flash without a fade.

use ratatui::style::{Color, Modifier, Style};

/// How many steps a ramp has.
///
/// Six because a 256-colour terminal cannot render a seventh distinctly: past
/// this the ramp saturates rather than inventing a shade the fallback theme
/// would collapse into its neighbour.
pub const STEPS: u8 = 6;

/// Which step a normalized intensity lands on, `1..=STEPS`.
///
/// The intensity is the caller's arithmetic — `|pnl| / 20` for a holding,
/// `weight / 40` for an allocation — because the divisor is the scale of the
/// quantity and nothing here knows what is being measured. What is here is the
/// quantization, which the surfaces must share or two of them will band the
/// same intensity differently.
///
/// Clamped at both ends: an intensity past 1 saturates rather than indexing off
/// the ramp, and one the caller could not compute (a NaN out of a division by
/// zero) reads as the dimmest step rather than as the brightest — a failed
/// measurement must not render as the loudest cell on the surface.
pub fn step(intensity: f64) -> u8 {
    if !intensity.is_finite() {
        return 1;
    }
    let crossed = (intensity.clamp(0.0, 1.0) * STEPS as f64) as u8;
    (1 + crossed).min(STEPS)
}

/// Which step a magnitude lands on against explicit band edges, `1..=STEPS`.
///
/// The last edge only bites through the clamp, which is the intent: the ramp
/// saturates rather than inventing a seventh shade.
pub fn step_at(magnitude: f64, edges: &[f64]) -> u8 {
    let crossed = edges.iter().filter(|edge| magnitude >= **edge).count();
    (1 + crossed).min(STEPS as usize) as u8
}

/// One step, drawn as depth and then as the semantic pair itself.
///
/// `dim` and `bright` are the pair the surface picked — positive/negative for a
/// change, the accent pair for a magnitude that has no direction. Every step is
/// distinct from its neighbours under both themes; a ramp that renders as four
/// bands has two decorative ones.
pub fn style(step: u8, dim: Color, bright: Color) -> Style {
    let t = crate::theme::theme();
    let base = Style::default();
    match step {
        1 => base.bg(t.bg_base).fg(dim),
        2 => base.bg(t.bg_raised).fg(dim),
        3 => base.bg(t.bg_hover).fg(bright),
        4 => base.bg(dim).fg(t.text_primary),
        5 => base.bg(dim).fg(t.text_primary).add_modifier(Modifier::BOLD),
        _ => base.bg(bright).fg(t.bg_base).add_modifier(Modifier::BOLD),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme::theme;

    #[test]
    fn an_intensity_quantizes_into_six_even_steps_and_then_saturates() {
        // The sixth band is entered at five sixths and never left: an intensity
        // the caller already clamped at 1 must not index a seventh step.
        assert_eq!(step(0.0), 1);
        assert_eq!(step(1.0 / 6.0 - 1e-9), 1);
        assert_eq!(step(1.0 / 6.0), 2);
        assert_eq!(step(0.5), 4);
        assert_eq!(step(5.0 / 6.0), 6);
        assert_eq!(step(1.0), 6);
        // Neither end runs off the ramp, whatever the caller's arithmetic did.
        assert_eq!(step(9.0), 6);
        assert_eq!(step(-1.0), 1);
        assert_eq!(step(f64::NAN), 1);
        assert_eq!(step(f64::INFINITY), 1);
    }

    #[test]
    fn explicit_edges_band_where_they_are_written_and_then_saturate() {
        let edges = [0.5, 1.0, 1.5, 2.0, 2.5, 3.3];
        assert_eq!(step_at(0.0, &edges), 1);
        assert_eq!(step_at(0.49, &edges), 1);
        assert_eq!(step_at(0.5, &edges), 2);
        assert_eq!(step_at(2.5, &edges), 6);
        assert_eq!(step_at(3.3, &edges), 6);
        assert_eq!(step_at(99.0, &edges), 6);
    }

    #[test]
    fn every_step_is_visually_distinct_from_every_other() {
        // Six steps that render as four is a ramp with two decorative bands.
        let t = theme();
        let styles: Vec<Style> = (1..=STEPS)
            .map(|s| style(s, t.positive_dim, t.positive))
            .collect();
        for i in 0..styles.len() {
            for j in (i + 1)..styles.len() {
                assert_ne!(
                    styles[i],
                    styles[j],
                    "step {} and step {} collide",
                    i + 1,
                    j + 1
                );
            }
        }
    }

    #[test]
    fn the_pair_the_caller_picked_is_what_says_which_way() {
        // Brightness says *how much*; the pair says which way. Two cells of the
        // same magnitude and opposite direction must not share a style, or the
        // surface says nothing about direction at all.
        let t = theme();
        for s in 1..=STEPS {
            assert_ne!(
                style(s, t.positive_dim, t.positive),
                style(s, t.negative_dim, t.negative),
                "step {s} reads the same up and down"
            );
        }
    }
}
