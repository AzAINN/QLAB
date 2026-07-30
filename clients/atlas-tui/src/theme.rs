//! The one colour contract: every `Color` in the client is named here, nowhere else.
//!
//! Two constructors, one shape. `Theme::truecolor()` carries the Obsidian hex
//! ramp; `Theme::indexed()` is the same contract in xterm-256 indices. The
//! fallback is not cosmetic: Terminal.app has no truecolor, and without it the
//! four-level depth ramp collapses into a single grey and the desk loses its
//! sense of layer. Which one the process uses is decided once, in `main`, by
//! `init(detect())` — truecolor is detected, never assumed.

use ratatui::style::Color;
use std::sync::OnceLock;

/// Every colour the client is allowed to draw with.
///
/// `Copy` on purpose: views read tokens in tight render loops, and a clone per
/// cell would be the wrong shape for a 60 fps frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    /// Four-level depth ramp — the illusion of stacked surfaces on a flat grid.
    pub bg_base: Color,
    pub bg_surface: Color,
    pub bg_raised: Color,
    pub bg_hover: Color,
    /// Three-level line ramp.
    pub border_dim: Color,
    pub border_med: Color,
    pub border_bright: Color,
    /// Four-level text ramp.
    pub text_primary: Color,
    pub text_secondary: Color,
    pub text_tertiary: Color,
    pub text_dim: Color,
    /// Amber — the only theme-defining colour. Everything else is semantic.
    pub accent: Color,
    pub accent_dim: Color,
    pub positive: Color,
    pub positive_dim: Color,
    pub negative: Color,
    pub negative_dim: Color,
    pub warning: Color,
    pub info: Color,
    pub cyan: Color,
    /// Multi-series charts. Six is the ceiling: a seventh series on a cell grid
    /// is not distinguishable, so views bucket rather than extend this.
    pub chart: [Color; 6],
}

impl Theme {
    /// The Obsidian palette at full 24-bit fidelity.
    pub const fn truecolor() -> Self {
        Self {
            bg_base: Color::Rgb(0x08, 0x08, 0x08),
            bg_surface: Color::Rgb(0x0a, 0x0a, 0x0a),
            bg_raised: Color::Rgb(0x11, 0x11, 0x11),
            bg_hover: Color::Rgb(0x16, 0x16, 0x16),
            border_dim: Color::Rgb(0x1a, 0x1a, 0x1a),
            border_med: Color::Rgb(0x22, 0x22, 0x22),
            border_bright: Color::Rgb(0x33, 0x33, 0x33),
            text_primary: Color::Rgb(0xe5, 0xe5, 0xe5),
            text_secondary: Color::Rgb(0x80, 0x80, 0x80),
            text_tertiary: Color::Rgb(0x52, 0x52, 0x52),
            text_dim: Color::Rgb(0x40, 0x40, 0x40),
            accent: Color::Rgb(0xd9, 0x77, 0x06),
            accent_dim: Color::Rgb(0x78, 0x35, 0x0f),
            positive: Color::Rgb(0x16, 0xa3, 0x4a),
            positive_dim: Color::Rgb(0x14, 0x53, 0x2d),
            negative: Color::Rgb(0xdc, 0x26, 0x26),
            negative_dim: Color::Rgb(0x7f, 0x1d, 0x1d),
            warning: Color::Rgb(0xca, 0x8a, 0x04),
            info: Color::Rgb(0x25, 0x63, 0xeb),
            cyan: Color::Rgb(0x08, 0x91, 0xb2),
            chart: [
                Color::Rgb(0xd9, 0x77, 0x06),
                Color::Rgb(0x08, 0x91, 0xb2),
                Color::Rgb(0x16, 0xa3, 0x4a),
                Color::Rgb(0xdc, 0x26, 0x26),
                Color::Rgb(0x25, 0x63, 0xeb),
                Color::Rgb(0xca, 0x8a, 0x04),
            ],
        }
    }

    /// The same contract in xterm-256 indices, for terminals without truecolor.
    ///
    /// The approximations are chosen once, here, so no view ever has to guess.
    /// `bg_base` and `bg_surface` both land on 232 — the 256-colour cube simply
    /// has no second near-black — so the depth ramp is three levels deep here
    /// rather than four. Every *semantic* pair stays distinguishable; that is
    /// the property the fallback must not lose.
    pub const fn indexed() -> Self {
        Self {
            bg_base: Color::Indexed(232),
            bg_surface: Color::Indexed(232),
            bg_raised: Color::Indexed(233),
            bg_hover: Color::Indexed(234),
            border_dim: Color::Indexed(234),
            border_med: Color::Indexed(235),
            border_bright: Color::Indexed(237),
            text_primary: Color::Indexed(254),
            text_secondary: Color::Indexed(244),
            text_tertiary: Color::Indexed(240),
            text_dim: Color::Indexed(238),
            accent: Color::Indexed(172),
            accent_dim: Color::Indexed(94),
            positive: Color::Indexed(35),
            positive_dim: Color::Indexed(22),
            negative: Color::Indexed(160),
            negative_dim: Color::Indexed(88),
            warning: Color::Indexed(136),
            info: Color::Indexed(27),
            cyan: Color::Indexed(31),
            chart: [
                Color::Indexed(172),
                Color::Indexed(31),
                Color::Indexed(35),
                Color::Indexed(160),
                Color::Indexed(27),
                Color::Indexed(136),
            ],
        }
    }

    /// The colour of a change. Zero counts as positive — flat is not a loss,
    /// and the reference desk paints it green. Callers pass finite numbers:
    /// absent data becomes `format::MISSING` before it ever reaches a colour.
    pub fn change(&self, v: f64) -> Color {
        if v >= 0.0 {
            self.positive
        } else {
            self.negative
        }
    }

    #[cfg(test)]
    fn all_colors(&self) -> Vec<Color> {
        let mut out = vec![
            self.bg_base,
            self.bg_surface,
            self.bg_raised,
            self.bg_hover,
            self.border_dim,
            self.border_med,
            self.border_bright,
            self.text_primary,
            self.text_secondary,
            self.text_tertiary,
            self.text_dim,
            self.accent,
            self.accent_dim,
            self.positive,
            self.positive_dim,
            self.negative,
            self.negative_dim,
            self.warning,
            self.info,
            self.cyan,
        ];
        out.extend_from_slice(&self.chart);
        out
    }
}

static THEME: OnceLock<Theme> = OnceLock::new();

/// Resolve the process-wide theme. Call once, first thing in `main`.
///
/// Panics if the theme was already resolved — either `init` ran twice, or
/// something read `theme()` during startup and locked in the truecolor
/// fallback. Both mean the detected terminal capability was silently
/// discarded, which on a 256-colour terminal renders an unreadable ramp.
pub fn init(truecolor: bool) {
    let resolved = if truecolor {
        Theme::truecolor()
    } else {
        Theme::indexed()
    };
    if THEME.set(resolved).is_err() {
        panic!("theme::init called after the theme was already resolved");
    }
}

/// The resolved theme.
///
/// Before `init`, this falls back to truecolor and locks it in — a reader that
/// beats startup gets the full-fidelity palette rather than a wrong one. Tests
/// construct `Theme::truecolor()` / `Theme::indexed()` directly instead of
/// touching this global, since a `OnceLock` cannot be reset between them.
pub fn theme() -> &'static Theme {
    THEME.get_or_init(Theme::truecolor)
}

/// Whether this terminal can render 24-bit colour.
pub fn detect() -> bool {
    let colorterm = std::env::var("COLORTERM").ok();
    let term = std::env::var("TERM").ok();
    truecolor_from(colorterm.as_deref(), term.as_deref())
}

/// The detection rule, separated from the environment so it can be tested
/// without a process-global mutation that would race the other tests.
fn truecolor_from(colorterm: Option<&str>, term: Option<&str>) -> bool {
    let colorterm = colorterm.unwrap_or_default().to_ascii_lowercase();
    if colorterm.contains("truecolor") || colorterm.contains("24bit") {
        return true;
    }
    // `TERM=*-direct` is the terminfo convention for direct-colour entries.
    // `xterm-256color` deliberately does not qualify: that is what Terminal.app
    // reports, and it is exactly the case the fallback exists for.
    let term = term.unwrap_or_default().to_ascii_lowercase();
    term.contains("truecolor") || term.contains("direct")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_change_is_positive_by_contract() {
        assert_eq!(theme().change(0.0), theme().positive);
    }

    #[test]
    fn a_loss_colours_negative() {
        assert_eq!(theme().change(-0.0001), theme().negative);
    }

    #[test]
    fn the_truecolor_theme_is_rgb_and_the_fallback_is_indexed() {
        for c in Theme::truecolor().all_colors() {
            assert!(matches!(c, Color::Rgb(..)), "truecolor theme carried {c:?}");
        }
        for c in Theme::indexed().all_colors() {
            assert!(
                matches!(c, Color::Indexed(_)),
                "fallback theme carried {c:?}"
            );
        }
    }

    #[test]
    fn the_fallback_keeps_the_distinctions_state_is_read_from() {
        let t = Theme::indexed();
        assert_ne!(t.positive, t.negative);
        assert_ne!(t.accent, t.warning);
        assert_ne!(t.text_primary, t.text_secondary);
        assert_ne!(t.border_dim, t.border_bright);
        for i in 0..t.chart.len() {
            for j in (i + 1)..t.chart.len() {
                assert_ne!(t.chart[i], t.chart[j], "chart[{i}] and chart[{j}] collide");
            }
        }
    }

    #[test]
    fn truecolor_is_detected_not_assumed() {
        assert!(truecolor_from(Some("truecolor"), Some("xterm-256color")));
        assert!(truecolor_from(Some("24bit"), None));
        assert!(truecolor_from(None, Some("xterm-direct")));
        assert!(!truecolor_from(None, Some("xterm-256color")));
        assert!(!truecolor_from(None, None));
    }

    /// The whole `src` tree, not just `src/ui` — a hex triple is as wrong in a
    /// widget as in a view, and this survives the file moves Task 5 makes.
    #[test]
    fn no_hardcoded_rgb_outside_theme() {
        let src = concat!(env!("CARGO_MANIFEST_DIR"), "/src");
        let out = std::process::Command::new("grep")
            .args(["-rl", "Color::Rgb", src])
            .output()
            .unwrap();
        let found: Vec<String> = String::from_utf8_lossy(&out.stdout)
            .lines()
            .map(str::to_string)
            .collect();
        // Asserting the exact list rather than "nothing found" also proves the
        // search ran: a grep that could not read the tree returns no matches,
        // which would otherwise read as a clean crate.
        assert_eq!(
            found,
            vec![format!("{src}/theme.rs")],
            "every Color::Rgb belongs in theme.rs; grep said: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
}
