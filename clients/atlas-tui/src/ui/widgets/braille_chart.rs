//! The hero chart: one price series in braille, with a money gutter and a keyboard crosshair.
//!
//! Braille because it is the only marker on a cell grid that carries sub-cell
//! resolution — a 20-cell pane draws 40 x-samples and 80 y-samples, which is the
//! difference between a shape an operator can read a trend off and a staircase.
//!
//! The crosshair is a keyboard translation of the reference desk's mouse
//! crosshair. This client has no mouse handling and is not getting one: a
//! read-only surface an operator drives from the home row is the point.

use crate::format;
use crate::theme::theme;
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::Style,
    symbols::Marker,
    text::{Line, Span},
    widgets::{
        canvas::{Canvas, Line as CanvasLine, Points},
        Paragraph,
    },
    Frame,
};

/// The money gutter. Six cells for a quote plus the one that keeps the label
/// off the line it is labelling.
const GUTTER_W: u16 = 7;

/// Two labels make a range; four make a scale.
const LABELS: usize = 4;

/// Draw `history` into `area`, with an optional crosshair at a series index.
///
/// The crosshair is an *index*, not a date: the owner's `market.assets[].history`
/// is a bare array of closes with no timestamps beside it (`model::Asset`), so a
/// `dd MMM` chip here would be a date this client invented. The index is the
/// honest label until the owner serves the dates that go with the prices.
pub fn draw(f: &mut Frame, area: Rect, history: &[f64], crosshair: Option<usize>) {
    let t = theme();
    if area.width == 0 || area.height == 0 {
        return;
    }
    if history.is_empty() {
        // Not a flat line at an invented level — that is the one rendering of
        // "nothing here" that looks exactly like a market that did not move.
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "no history for this asset",
                Style::default().fg(t.text_tertiary),
            ))),
            area,
        );
        return;
    }

    let lo = history.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = history.iter().copied().fold(f64::NEG_INFINITY, f64::max);

    // The gutter is dropped rather than crushed when the pane is too short to
    // separate four labels: three of them on top of each other is not a scale.
    let (gutter, plot) = if area.height as usize >= LABELS && area.width > GUTTER_W {
        let cols = Layout::horizontal([Constraint::Length(GUTTER_W), Constraint::Min(0)]).split(area);
        (Some(cols[0]), cols[1])
    } else {
        (None, area)
    };
    if let Some(gutter) = gutter {
        draw_gutter(f, gutter, lo, hi);
    }
    if plot.width == 0 || plot.height == 0 {
        return;
    }

    // A flat series is a zero-height window, and a canvas asked for one has
    // nowhere to put its line. The padding moves the *plot*, never the gutter:
    // the labels still report the prices the owner actually sent.
    let (plot_lo, plot_hi) = if hi > lo { (lo, hi) } else { (lo - 0.5, hi + 0.5) };
    let x_max = (history.len() - 1).max(1) as f64;
    let chip = crosshair
        .filter(|i| *i < history.len())
        .map(|i| (i, format!("{i} ${}", format::price(history[i]))));
    // One cell of the plot, in series units — what the chip has to be nudged by
    // to stay inside the pane when the crosshair is near the right edge.
    let per_cell = x_max / plot.width as f64;

    let canvas = Canvas::default()
        .marker(Marker::Braille)
        .x_bounds([0.0, x_max])
        .y_bounds([plot_lo, plot_hi])
        .paint(|ctx| {
            if history.len() == 1 {
                ctx.draw(&Points {
                    coords: &[(0.0, history[0])],
                    color: t.accent,
                });
            }
            for (i, pair) in history.windows(2).enumerate() {
                ctx.draw(&CanvasLine {
                    x1: i as f64,
                    y1: pair[0],
                    x2: (i + 1) as f64,
                    y2: pair[1],
                    color: t.accent,
                });
            }
            if let Some((idx, chip)) = &chip {
                // Its own layer: braille cells merge within one, so a rule drawn
                // beside the line would take the line's colour with it.
                ctx.layer();
                ctx.draw(&CanvasLine {
                    x1: *idx as f64,
                    y1: plot_lo,
                    x2: *idx as f64,
                    y2: plot_hi,
                    color: t.border_bright,
                });
                let width = chip.chars().count() as f64 * per_cell;
                let x = if *idx as f64 + width > x_max {
                    (*idx as f64 - width).max(0.0)
                } else {
                    *idx as f64
                };
                ctx.print(
                    x,
                    plot_hi,
                    Line::from(Span::styled(
                        chip.clone(),
                        Style::default().fg(t.text_primary).bg(t.bg_hover),
                    )),
                );
            }
        });
    f.render_widget(canvas, plot);
}

/// Four right-aligned quotes down the left edge, top to bottom.
fn draw_gutter(f: &mut Frame, area: Rect, lo: f64, hi: f64) {
    let t = theme();
    let rows = area.height as usize;
    let mut lines = vec![Line::from(""); rows];
    for i in 0..LABELS {
        // Evenly spread over the pane, ends included: the first label marks the
        // top of the range and the last marks the bottom, so the scale the eye
        // interpolates is the one the canvas actually drew.
        let row = i * (rows - 1) / (LABELS - 1);
        let value = hi - (hi - lo) * i as f64 / (LABELS - 1) as f64;
        lines[row] = Line::from(Span::styled(
            format!("{:>width$} ", format::price(value), width = GUTTER_W as usize - 1),
            Style::default().fg(t.text_tertiary),
        ));
    }
    f.render_widget(Paragraph::new(lines), area);
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};

    /// The chart rendered into `w`×`h`, read back row by row.
    fn rows(history: &[f64], crosshair: Option<usize>, w: u16, h: u16) -> Vec<String> {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, Rect::new(0, 0, w, h), history, crosshair))
            .unwrap();
        let buf = term.backend().buffer().clone();
        (0..h)
            .map(|y| {
                (0..w)
                    .map(|x| buf[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect()
    }

    #[test]
    fn the_gutter_is_the_series_own_money_range() {
        // A y-axis whose labels are not the data's own extremes is a chart that
        // cannot be read against the tape it came from.
        let history = [750.72, 743.29, 742.09, 748.28, 729.46];
        let rows = rows(&history, None, 30, 12);
        let text = rows.join("\n");
        assert!(text.contains("750.72"), "no top label:\n{text}");
        assert!(text.contains("729.46"), "no bottom label:\n{text}");
        // Four, not two: two labels make a range, four make a scale.
        let labelled = rows.iter().filter(|r| r.contains('.')).count();
        assert_eq!(labelled, 4, "the gutter carried {labelled} labels:\n{text}");
    }

    #[test]
    fn a_series_the_owner_did_not_send_says_so_rather_than_drawing_a_flat_line() {
        // A single horizontal rule at an invented level is the worst available
        // rendering of "no history": it looks exactly like a flat market.
        let text = rows(&[], None, 30, 12).join("\n");
        assert!(text.contains("no history"), "{text}");
    }

    #[test]
    fn a_flat_series_still_draws_inside_its_own_pane() {
        // Degenerate bounds: `y_bounds([x, x])` is a zero-height window, and a
        // canvas asked for one has nowhere to put the line. The gutter reports
        // the price the owner sent, not the padding the plot needed.
        let text = rows(&[100.0, 100.0, 100.0], None, 30, 12).join("\n");
        assert!(text.contains("100.00"), "{text}");
        assert!(!text.contains("100.50"), "the padding reached the gutter:\n{text}");
    }

    #[test]
    fn the_crosshair_chip_stays_inside_the_pane_at_either_end() {
        // Anchored on the rule it would run off the right edge and render as
        // half a price — a number that is wrong rather than absent.
        let history = [750.72, 743.29, 742.09, 748.28, 729.46];
        let text = rows(&history, Some(4), 30, 12).join("\n");
        assert!(text.contains("4 $729.46"), "{text}");
        let text = rows(&history, Some(0), 30, 12).join("\n");
        assert!(text.contains("0 $750.72"), "{text}");
    }

    #[test]
    fn a_crosshair_past_the_end_of_the_series_draws_nothing() {
        // The index is the view's, the series is the owner's, and the two can
        // disagree for one frame after the universe changes under a cursor.
        let text = rows(&[750.72, 743.29], Some(9), 30, 12).join("\n");
        assert!(!text.contains('$'), "{text}");
    }

    #[test]
    fn a_pane_too_small_to_chart_renders_without_panicking() {
        for (w, h) in [(1u16, 1u16), (2, 3), (8, 2), (30, 1), (7, 12)] {
            let _ = rows(&[750.72, 743.29, 729.46], Some(1), w, h);
        }
    }
}
