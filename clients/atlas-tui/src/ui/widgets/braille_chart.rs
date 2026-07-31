//! One series in braille, with a scale down the left and a keyboard crosshair.
//!
//! Braille because it is the only marker on a cell grid that carries sub-cell
//! resolution — a 20-cell pane draws 40 x-samples and 80 y-samples, which is the
//! difference between a shape an operator can read a trend off and a staircase.
//!
//! The crosshair is a keyboard translation of the reference desk's mouse
//! crosshair. This client has no mouse handling and is not getting one: a
//! read-only surface an operator drives from the home row is the point.
//!
//! Two surfaces draw through here — the markets hero and BOOK's equity curve —
//! and they disagree about exactly one thing, which is how a gutter label is
//! spelled. A quote is `750.72`; a book's equity is `$10,012.40`, and the two
//! sharing seven cells is how one of them loses a digit. So the spelling is the
//! caller's and the *width* is derived from it, which is also what gives this
//! pane a floor it can state.

use crate::theme::theme;
use crate::ui::widgets::refuse;
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
use unicode_width::UnicodeWidthStr;

/// Two labels make a range; four make a scale.
const LABELS: usize = 4;

/// The narrowest plot that is a chart rather than an artifact.
///
/// A braille cell is two x-samples wide, so eight cells is sixteen samples. A
/// year of daily marks drawn into fewer than that is compressed past twenty to
/// one, and the "shape" is then a fact about which marks happened to land on a
/// cell boundary rather than about the series. Below this the pane says so;
/// there is no honest smaller rendering, because a line with no scale beside it
/// and no resolution under it looks exactly like a chart.
const PLOT_MIN_W: u16 = 8;

/// What to draw, and how the surface spells the numbers beside it.
pub struct Chart<'a> {
    /// What this pane is called, for the refusal below its floor. Every other
    /// refusal on the workstation names the pane that could not draw; a chart
    /// that said only "this pane" would be the one an operator has to hunt for.
    pub name: &'a str,
    /// Oldest first, and never empty — what "nothing to draw" means is the
    /// caller's fact (an asset with no history, a period slice with one mark),
    /// and one sentence here for both would hide the difference.
    pub series: &'a [f64],
    /// An index into `series`, or no crosshair at all.
    ///
    /// An *index*, not a date: the owner's `market.assets[].history` is a bare
    /// array of closes with no timestamps beside it (`model::Asset`), so a
    /// `dd MMM` chip here would be a date this client invented.
    ///
    /// The chip it draws states a quote and prefixes the `$` itself, so a
    /// surface whose `label` already carries a currency mark must not ask for
    /// one — BOOK's curve is dated by its period, not by an index into it.
    pub crosshair: Option<usize>,
    /// How one gutter label is spelled — `format::price` for a quote,
    /// `format::money` for an equity.
    pub label: fn(f64) -> String,
}

/// Draw `chart` into `area`, or say why it could not be drawn.
pub fn draw(f: &mut Frame, area: Rect, chart: Chart) {
    let t = theme();
    // A pane with no cells has nowhere to say anything, refusal included.
    if area.width == 0 || area.height == 0 {
        return;
    }
    if chart.series.is_empty() {
        // Not a flat line at an invented level — that is the one rendering of
        // "nothing here" that looks exactly like a market that did not move.
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!("{} has no marks to draw", chart.name),
                Style::default().fg(t.text_tertiary),
            ))),
            area,
        );
        return;
    }

    let lo = chart.series.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = chart
        .series
        .iter()
        .copied()
        .fold(f64::NEG_INFINITY, f64::max);
    let labels = labels(lo, hi, chart.label);
    let gutter_w = gutter_w(&labels);

    // Guarded on the allocation this pane was handed, never on the pane it was
    // split from — Task 9's lesson, at the one call site that had escaped it.
    // What used to happen below the floor was that the gutter was quietly
    // dropped and the line drawn anyway: a curve with no numbers beside it, off
    // which an operator reads a level that is not there. A shape with no scale
    // is not a degraded chart, it is a different and wrong one.
    if (area.height as usize) < LABELS {
        refuse(
            f,
            area,
            format!(
                "{} needs {LABELS} rows for its scale — this pane has {}, make the terminal taller",
                chart.name, area.height
            ),
        );
        return;
    }
    let floor_w = gutter_w + PLOT_MIN_W;
    if area.width < floor_w {
        refuse(
            f,
            area,
            format!(
                "{} needs {floor_w} columns for a {} scale and a line under it — this pane has \
                 {}, widen the terminal",
                chart.name,
                widest(&labels),
                area.width
            ),
        );
        return;
    }

    let cols = Layout::horizontal([Constraint::Length(gutter_w), Constraint::Min(0)]).split(area);
    draw_gutter(f, cols[0], &labels);
    let plot = cols[1];

    // A flat series is a zero-height window, and a canvas asked for one has
    // nowhere to put its line. The padding moves the *plot*, never the gutter:
    // the labels still report the values the owner actually sent.
    let (plot_lo, plot_hi) = if hi > lo {
        (lo, hi)
    } else {
        (lo - 0.5, hi + 0.5)
    };
    let x_max = (chart.series.len() - 1).max(1) as f64;
    let series = chart.series;
    // The chip states a quote, so it carries the `$` the gutter's own spelling
    // may or may not — `format::price` renders a bare number, and a chip that
    // said `1 743.29` states a figure without saying it is money.
    let chip = chart
        .crosshair
        .filter(|i| *i < series.len())
        .map(|i| (i, format!("{i} ${}", (chart.label)(series[i]))));
    // One cell of the plot, in series units — what the chip has to be nudged by
    // to stay inside the pane when the crosshair is near the right edge.
    let per_cell = x_max / plot.width as f64;

    let canvas = Canvas::default()
        .marker(Marker::Braille)
        .x_bounds([0.0, x_max])
        .y_bounds([plot_lo, plot_hi])
        .paint(|ctx| {
            if series.len() == 1 {
                ctx.draw(&Points {
                    coords: &[(0.0, series[0])],
                    color: t.accent,
                });
            }
            for (i, pair) in series.windows(2).enumerate() {
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

/// The four labels the gutter would carry, top to bottom.
///
/// Spelled before the floor is checked, because the floor is a fact about them:
/// a scale of `$10,012.40` needs four more columns than one of `750.72`, and a
/// constant width would clip one of the two into a number that is wrong.
fn labels(lo: f64, hi: f64, label: fn(f64) -> String) -> [String; LABELS] {
    std::array::from_fn(|i| label(hi - (hi - lo) * i as f64 / (LABELS - 1) as f64))
}

/// The gutter's own width: its widest label, plus the cell that keeps the label
/// off the line it is labelling.
fn gutter_w(labels: &[String; LABELS]) -> u16 {
    labels.iter().map(|l| l.width()).max().unwrap_or(0) as u16 + 1
}

/// The label the gutter is sized by, for the refusal to quote.
///
/// The *first* of the widest rather than the last, which is the top of the
/// scale: two interpolated labels routinely tie on width, and `$10,003.97` is a
/// number the operator never asked about — the range they did.
fn widest(labels: &[String; LABELS]) -> &str {
    labels
        .iter()
        .fold(None::<&String>, |widest, label| match widest {
            Some(w) if w.width() >= label.width() => Some(w),
            _ => Some(label),
        })
        .map(String::as_str)
        .unwrap_or_default()
}

/// The labels down the left edge, right-aligned, top to bottom.
fn draw_gutter(f: &mut Frame, area: Rect, labels: &[String; LABELS]) {
    let t = theme();
    let rows = area.height as usize;
    let width = area.width as usize - 1;
    let mut lines = vec![Line::from(""); rows];
    for (i, label) in labels.iter().enumerate() {
        // Evenly spread over the pane, ends included: the first label marks the
        // top of the range and the last marks the bottom, so the scale the eye
        // interpolates is the one the canvas actually drew.
        let row = i * (rows - 1) / (LABELS - 1);
        lines[row] = Line::from(Span::styled(
            format!("{label:>width$} "),
            Style::default().fg(t.text_tertiary),
        ));
    }
    f.render_widget(Paragraph::new(lines), area);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::format;
    use ratatui::{backend::TestBackend, Terminal};

    /// A price chart rendered into `w`×`h`, read back row by row.
    fn rows(series: &[f64], crosshair: Option<usize>, w: u16, h: u16) -> Vec<String> {
        chart_rows(
            Chart {
                name: "price chart",
                series,
                crosshair,
                label: format::price,
            },
            w,
            h,
        )
    }

    /// The rendered pane as one line, with the wrapping taken back out.
    ///
    /// A refusal is a wrapped `Paragraph` by design — a remedy clipped to
    /// `widen the term` is one an operator cannot run — so at the widths these
    /// tests use, the sentence is spread over a dozen rows. Pinning it needs
    /// the sentence, not the row it happened to break on.
    fn flat(rows: Vec<String>) -> String {
        rows.join(" ")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
    }

    /// Whether anything in the pane is a plotted cell. Braille is the marker,
    /// so its block is the one run of characters only the canvas draws.
    fn has_braille(text: &str) -> bool {
        text.chars().any(|c| ('\u{2801}'..='\u{28ff}').contains(&c))
    }

    fn chart_rows(chart: Chart, w: u16, h: u16) -> Vec<String> {
        let mut term = Terminal::new(TestBackend::new(w, h)).unwrap();
        term.draw(|f| draw(f, Rect::new(0, 0, w, h), chart))
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
    fn the_gutter_is_the_series_own_range() {
        // A y-axis whose labels are not the data's own extremes is a chart that
        // cannot be read against the tape it came from.
        let series = [750.72, 743.29, 742.09, 748.28, 729.46];
        let rows = rows(&series, None, 30, 12);
        let text = rows.join("\n");
        assert!(text.contains("750.72"), "no top label:\n{text}");
        assert!(text.contains("729.46"), "no bottom label:\n{text}");
        // Four, not two: two labels make a range, four make a scale.
        let labelled = rows.iter().filter(|r| r.contains('.')).count();
        assert_eq!(labelled, 4, "the gutter carried {labelled} labels:\n{text}");
    }

    #[test]
    fn the_gutter_is_as_wide_as_the_surfaces_own_spelling() {
        // The reason the width is derived rather than fixed: at seven cells a
        // `$10,012.40` label renders `$10,012` — a book stated ten thousand
        // times small — and the chart around it looks perfectly healthy.
        let equity = [9987.1, 10012.4, 10000.0];
        let text = chart_rows(
            Chart {
                name: "equity curve",
                series: &equity,
                crosshair: None,
                label: format::money,
            },
            40,
            12,
        )
        .join("\n");
        assert!(text.contains("$10,012.40"), "the top of the scale:\n{text}");
        assert!(
            text.contains("$9,987.10"),
            "the bottom of the scale:\n{text}"
        );
    }

    #[test]
    fn a_pane_under_the_floor_says_so_rather_than_dropping_its_scale() {
        // What this used to do: drop the gutter and draw the line anyway. A
        // curve with no numbers beside it is not a degraded chart — an operator
        // reads a level off it, and the level is not there.
        let series = [750.72, 743.29, 729.46];
        let text = flat(rows(&series, None, 14, 12));
        assert!(text.contains("price chart needs 15 columns"), "{text}");
        assert!(text.contains("widen the terminal"), "{text}");
        // The refusal quotes the scale it could not draw — that is the point of
        // it — so what must not survive is the *line*: a curve beside a sentence
        // saying the pane cannot hold one is the half-drawn rendering this
        // guard exists to prevent.
        assert!(
            !has_braille(&text),
            "a line survived below the floor:\n{text}"
        );

        // Bracketed: one column more and the whole chart draws, scale included.
        let text = rows(&series, None, 15, 12).join("\n");
        assert!(text.contains("750.72"), "the floor refused itself:\n{text}");
    }

    #[test]
    fn a_pane_too_short_for_a_scale_says_so_rather_than_crushing_one() {
        // Three labels on top of each other is not a scale, and three rows of
        // braille is not a trend.
        let series = [750.72, 743.29, 729.46];
        let text = rows(&series, None, 30, 3).join("\n");
        assert!(text.contains("price chart needs 4 rows"), "{text}");
        assert!(text.contains("taller"), "{text}");
        assert!(text.contains("4"), "{text}");
        // Bracketed at the other side of the boundary.
        let text = rows(&series, None, 30, 4).join("\n");
        assert!(text.contains("750.72"), "the floor refused itself:\n{text}");
    }

    #[test]
    fn the_floor_moves_with_the_spelling_it_is_derived_from() {
        // The same pane, two surfaces: a quote scale fits where a book's does
        // not. A constant floor would either refuse a working price chart or
        // admit an equity curve whose gutter is clipped.
        let equity = [9987.1, 10012.4];
        let text = flat(chart_rows(
            Chart {
                name: "equity curve",
                series: &equity,
                crosshair: None,
                label: format::money,
            },
            18,
            12,
        ));
        assert!(text.contains("equity curve needs 19 columns"), "{text}");
        assert!(
            text.contains("$10,012.40"),
            "the refusal states the scale it could not draw:\n{text}"
        );
        // A price chart of the same shape draws in the same eighteen columns.
        let text = rows(&[750.72, 729.46], None, 18, 12).join("\n");
        assert!(text.contains("750.72"), "{text}");
    }

    #[test]
    fn a_series_the_caller_left_empty_says_so_rather_than_drawing_a_flat_line() {
        // A single horizontal rule at an invented level is the worst available
        // rendering of "no marks": it looks exactly like a flat market.
        let text = rows(&[], None, 30, 12).join("\n");
        assert!(text.contains("price chart has no marks"), "{text}");
    }

    #[test]
    fn a_flat_series_still_draws_inside_its_own_pane() {
        // Degenerate bounds: `y_bounds([x, x])` is a zero-height window, and a
        // canvas asked for one has nowhere to put the line. The gutter reports
        // the price the owner sent, not the padding the plot needed.
        let text = rows(&[100.0, 100.0, 100.0], None, 30, 12).join("\n");
        assert!(text.contains("100.00"), "{text}");
        assert!(
            !text.contains("100.50"),
            "the padding reached the gutter:\n{text}"
        );
    }

    #[test]
    fn the_crosshair_chip_stays_inside_the_pane_at_either_end() {
        // Anchored on the rule it would run off the right edge and render as
        // half a price — a number that is wrong rather than absent.
        let series = [750.72, 743.29, 742.09, 748.28, 729.46];
        let text = rows(&series, Some(4), 30, 12).join("\n");
        assert!(text.contains("4 $729.46"), "{text}");
        let text = rows(&series, Some(0), 30, 12).join("\n");
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
