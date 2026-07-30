//! Flash tracking and the motion rules that decide when a change is worth animating.

#[cfg(test)]
mod version_pair {
    use crate::theme::theme;
    use ratatui::{buffer::Buffer, layout::Rect, widgets::Widget};

    /// tachyonfx depends on `ratatui-core` rather than `ratatui`, so a ratatui
    /// bump can leave the two compiling against different `Buffer` types — the
    /// tree stays green and the effects silently apply to nothing. This fails
    /// the build instead. Retire it once real effect code covers the same seam.
    ///
    /// Every step asserts against the buffer it started from: a version-pair
    /// test that only checks it compiles is exactly the "code that exists but
    /// nothing exercises" shape it is here to catch.
    #[test]
    fn the_effect_and_widget_crates_write_to_ratatuis_own_buffer() {
        let area = Rect::new(0, 0, 40, 10);
        let mut buf = Buffer::empty(area);
        let empty = buf.clone();

        tui_big_text::BigText::builder()
            .lines(vec!["hi".into()])
            .build()
            .render(area, &mut buf);
        throbber_widgets_tui::Throbber::default().render(Rect::new(0, 0, 10, 1), &mut buf);
        assert_ne!(buf, empty, "the widget crates rendered nothing");

        // Fading needs something to fade, so the effect runs over the rendered
        // cells rather than an empty grid — on an empty grid a no-op and a
        // cross-crate Buffer mismatch look identical.
        let rendered = buf.clone();
        let mut effect =
            tachyonfx::fx::fade_to_fg(theme().accent, (100, tachyonfx::Interpolation::Linear));
        effect.process(tachyonfx::Duration::from_millis(16), &mut buf, area);
        assert_ne!(buf, rendered, "the tachyonfx effect rendered nothing");
    }
}
