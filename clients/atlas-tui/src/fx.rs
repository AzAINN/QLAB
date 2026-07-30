//! Flash tracking and the motion rules that decide when a change is worth animating.

#[cfg(test)]
mod version_pair {
    use ratatui::{buffer::Buffer, layout::Rect, style::Color, widgets::Widget};

    /// tachyonfx depends on `ratatui-core` rather than `ratatui`, so a ratatui
    /// bump can leave the two compiling against different `Buffer` types — the
    /// tree stays green and the effects silently apply to nothing. This fails
    /// the build instead. Retire it once real effect code covers the same seam.
    #[test]
    fn the_effect_and_widget_crates_write_to_ratatuis_own_buffer() {
        let area = Rect::new(0, 0, 40, 10);
        let mut buf = Buffer::empty(area);

        let mut effect =
            tachyonfx::fx::fade_to_fg(Color::Red, (100, tachyonfx::Interpolation::Linear));
        effect.process(tachyonfx::Duration::from_millis(16), &mut buf, area);

        tui_big_text::BigText::builder()
            .lines(vec!["hi".into()])
            .build()
            .render(area, &mut buf);
        throbber_widgets_tui::Throbber::default().render(Rect::new(0, 0, 10, 1), &mut buf);
    }
}
