"""Design-system invariants: palette contrast, glyph safety, render alignment.

These are pure tests. Nothing here constructs a Textual app, opens a registry,
or touches the network. The contrast assertions are the authority for the
palette; the numbers in planning-docs are a snapshot of this test's output.
"""

from __future__ import annotations

import pytest

from qlab.tui.design import glyphs, primitives, tokens


# ---------------------------------------------------------------------------
# palette contrast
# ---------------------------------------------------------------------------

def test_relative_luminance_matches_wcag_reference_points():
    # WCAG defines pure black as 0.0 and pure white as 1.0 exactly.
    assert tokens.relative_luminance("#000000") == pytest.approx(0.0)
    assert tokens.relative_luminance("#ffffff") == pytest.approx(1.0)


def test_contrast_ratio_is_symmetric_and_bounded():
    # The ratio is defined on ordered luminances, so argument order must not
    # matter; 21:1 is the maximum achievable.
    assert tokens.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert tokens.contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)


def test_every_theme_defines_every_role():
    for name, roles in tokens.ROLES.items():
        missing = set(tokens.ROLE_NAMES) - set(roles)
        assert not missing, f"{name} is missing roles: {sorted(missing)}"


def test_every_role_meets_its_contrast_target_on_canvas_and_panel():
    # A role must be legible on the canvas AND on a raised panel. The panel is
    # the stricter of the two and is where the retired palette's dim tier
    # failed, so both bases are asserted rather than just the canvas.
    failures = []
    for theme_name, roles in tokens.ROLES.items():
        for role, target in tokens.CONTRAST_TARGETS.items():
            for base in ("bg", "panel"):
                ratio = tokens.contrast_ratio(roles[role], roles[base])
                if ratio < target:
                    failures.append(
                        f"{theme_name}: {role} on {base} = {ratio:.2f} < {target}")
    assert not failures, "contrast failures:\n" + "\n".join(failures)


def test_dark_themes_keep_sign_distinguishable_without_hue():
    # Someone who cannot resolve hue reads up/down by luminance alone. The
    # retired amber palette achieved 1.82:1; a future edit must not regress
    # below that on a dark canvas, where the headroom exists.
    for theme_name, roles in tokens.ROLES.items():
        if not tokens.THEMES[theme_name].dark:
            continue
        separation = tokens.contrast_ratio(roles["up"], roles["down"])
        assert separation >= 1.8, (
            f"{theme_name}: up/down separation {separation:.2f}:1 is below 1.8:1")


def test_light_themes_document_their_unavoidable_sign_weakness():
    # On a light canvas both signs must be dark to pass contrast, so luminance
    # is not available as a channel. This is accepted, not fixed -- the
    # guarantee is the explicit sign character asserted in the primitives
    # tests. This test pins the known limitation so it cannot silently become
    # an assumption that light themes encode sign by colour.
    for theme_name, roles in tokens.ROLES.items():
        if tokens.THEMES[theme_name].dark:
            continue
        separation = tokens.contrast_ratio(roles["up"], roles["down"])
        assert separation < 1.8


def test_themes_are_registrable_textual_themes():
    from textual.theme import Theme

    assert set(tokens.THEMES) == set(tokens.ROLES)
    for name, theme in tokens.THEMES.items():
        assert isinstance(theme, Theme)
        assert theme.name == name


def test_default_theme_is_dark():
    assert tokens.DEFAULT_THEME in tokens.THEMES
    assert tokens.THEMES[tokens.DEFAULT_THEME].dark


def test_chrome_roles_are_excluded_from_text_contrast_targets():
    # Borders and surfaces are chrome, never text. Giving them a text target
    # would force them brighter and reintroduce coloured chrome.
    for chrome in ("bg", "surface", "panel", "border"):
        assert chrome not in tokens.CONTRAST_TARGETS


# ---------------------------------------------------------------------------
# state glyphs
# ---------------------------------------------------------------------------

def test_unicode_and_ascii_tables_describe_the_same_states():
    for state, spec in glyphs.STATES.items():
        assert spec.unicode, f"{state} has no unicode glyph"
        assert spec.ascii, f"{state} has no ascii glyph"
        assert spec.role, f"{state} has no colour role"


def test_glyph_roles_resolve_in_every_theme():
    for state, spec in glyphs.STATES.items():
        for theme_name in tokens.ROLES:
            assert tokens.role(theme_name, spec.role), (
                f"{state} role {spec.role} missing from {theme_name}")


def test_no_glyph_is_east_asian_wide():
    # A wide glyph consumes two cells and silently shifts every column after
    # it, which breaks the 14-column label contract. Ambiguous-width glyphs are
    # permitted (they are single-width outside CJK locales) and the ASCII table
    # covers the locales where they are not.
    import unicodedata

    for state, spec in glyphs.STATES.items():
        assert len(spec.unicode) == 1, f"{state} glyph is not one codepoint"
        width = unicodedata.east_asian_width(spec.unicode)
        assert width not in ("W", "F"), (
            f"{state} glyph {spec.unicode!r} is {width}-width and would break alignment")


def test_ascii_glyphs_are_single_cell_ascii():
    for state, spec in glyphs.STATES.items():
        assert len(spec.ascii) == 1, f"{state} ascii glyph is not one character"
        assert spec.ascii.isascii(), f"{state} ascii glyph is not ascii"


def test_glyphs_are_distinct_within_each_table():
    # Two states sharing a character is indistinguishable at the point of use,
    # and the ASCII table is where collisions are easy to introduce.
    for attribute in ("unicode", "ascii"):
        seen: dict[str, str] = {}
        for state, spec in glyphs.STATES.items():
            char = getattr(spec, attribute)
            assert char not in seen, (
                f"{attribute}: {state} and {seen[char]} both render as {char!r}")
            seen[char] = state


def test_glyph_table_covers_every_state_the_legacy_theme_defines():
    # The retiring theme.STATE_STYLE is still driving the live app. If the new
    # table does not cover every state it uses, migrating a view would silently
    # lose a status.
    from qlab.tui import theme as legacy

    missing = set(legacy.STATE_STYLE) - set(glyphs.STATES)
    assert not missing, f"states dropped in the new table: {sorted(missing)}"


def test_unicode_support_probe_accepts_utf8_and_rejects_legacy_code_pages():
    assert glyphs.supports_unicode("utf-8") is True
    assert glyphs.supports_unicode("cp437") is False


def test_badge_falls_back_to_ascii_when_unicode_is_unavailable():
    unicode_glyph, unicode_role = glyphs.badge("working", ascii_only=False)
    ascii_glyph, ascii_role = glyphs.badge("working", ascii_only=True)

    assert unicode_glyph == glyphs.STATES["working"].unicode
    assert ascii_glyph == glyphs.STATES["working"].ascii
    # The role must not change with the glyph set: only the character degrades.
    assert unicode_role == ascii_role


def test_badge_refuses_an_unknown_state():
    # Fail loud: a status the design system has no glyph for must not silently
    # render as blank.
    with pytest.raises(KeyError):
        glyphs.badge("not-a-real-state")


# ---------------------------------------------------------------------------
# render primitives: the alignment contract
# ---------------------------------------------------------------------------

def test_value_column_is_fixed_at_fifteen():
    # Cross-pane alignment is the point of the contract; per-pane optimisation
    # is not permitted. Column 15 one-indexed is index 14 zero-indexed.
    assert primitives.LABEL_WIDTH == 14


def test_field_starts_the_value_at_the_contract_column():
    rendered = primitives.field("regime", "elevated turbulence")

    assert rendered.plain[:14] == "regime".ljust(14)
    assert rendered.plain[14:] == "elevated turbulence"


def test_field_truncates_a_long_label_rather_than_shifting_the_value():
    # A label that overflows must lose characters, never push the value column,
    # or one long label would misalign an entire pane.
    rendered = primitives.field("a" * 30, "value")

    assert len(rendered.plain[:14]) == 14
    assert rendered.plain[14:] == "value"


def test_field_right_aligns_meta_to_the_pane_edge():
    rendered = primitives.field("regime", "elevated", meta="since 09:14", width=40)

    assert len(rendered.plain) == 40
    assert rendered.plain.endswith("since 09:14")
    assert rendered.plain[14:22] == "elevated"


def test_field_styles_the_label_muted_and_the_meta_faint():
    theme = tokens.DEFAULT_THEME
    rendered = primitives.field("regime", "elevated", meta="09:14", width=40)
    styles = {str(span.style) for span in rendered.spans}

    assert tokens.role(theme, "muted") in styles
    assert tokens.role(theme, "faint") in styles


def test_section_heading_is_upper_case_and_accented():
    rendered = primitives.section("workforce")

    assert rendered.plain == "WORKFORCE"
    assert tokens.role(tokens.DEFAULT_THEME, "accent") in str(rendered.style)
    assert "bold" in str(rendered.style)


def test_rule_fills_exactly_the_requested_width():
    rendered = primitives.rule(24)

    assert len(rendered.plain) == 24
    assert set(rendered.plain) == {"─"}


# ---------------------------------------------------------------------------
# render primitives: numbers
# ---------------------------------------------------------------------------

def test_percent_is_rendered_from_a_fraction_with_two_decimals():
    assert primitives.num(-0.042, "pct").plain == "-4.20%"


def test_money_uses_thousands_separators_and_two_decimals():
    assert primitives.num(1234567.5, "money").plain == "1,234,567.50"


def test_numbers_right_align_within_a_given_width():
    rendered = primitives.num(4.2, "ratio", width=10)

    assert len(rendered.plain) == 10
    assert rendered.plain == "      4.20"


def test_a_signed_positive_delta_carries_an_explicit_plus():
    # The sign character is the guarantee, not the colour: the light themes
    # cannot separate up from down by luminance at all.
    assert primitives.num(0.012, "pct", signed=True).plain == "+1.20%"


def test_a_signed_negative_delta_carries_an_explicit_minus():
    assert primitives.num(-0.012, "pct", signed=True).plain == "-1.20%"


def test_a_zero_delta_is_unsigned_and_neutral():
    # Zero has no direction. Rendering it as "+0.00%" would assert a gain that
    # was not measured.
    rendered = primitives.num(0.0, "pct", signed=True)

    assert rendered.plain == "0.00%"
    up = tokens.role(tokens.DEFAULT_THEME, "up")
    down = tokens.role(tokens.DEFAULT_THEME, "down")
    assert up not in str(rendered.style)
    assert down not in str(rendered.style)


def test_signed_deltas_are_coloured_by_direction():
    theme = tokens.DEFAULT_THEME
    assert tokens.role(theme, "up") in str(primitives.num(0.01, "pct", signed=True).style)
    assert tokens.role(theme, "down") in str(primitives.num(-0.01, "pct", signed=True).style)


def test_unsigned_levels_are_never_coloured_by_sign():
    # A price is not an opinion. Sign colour applies to deltas only, so an
    # unsigned render must carry neither direction colour.
    theme = tokens.DEFAULT_THEME
    rendered = primitives.num(-412.55, "money")

    assert tokens.role(theme, "down") not in str(rendered.style)
    assert tokens.role(theme, "up") not in str(rendered.style)


def test_num_refuses_an_unknown_unit():
    with pytest.raises(KeyError):
        primitives.num(1.0, "furlongs")


# ---------------------------------------------------------------------------
# render primitives: absent values
# ---------------------------------------------------------------------------

def test_unknown_is_never_rendered_as_zero():
    # The fail-loud invariant applies to presentation too: an uncomputed value
    # displayed as 0.00 is a false measurement.
    assert primitives.absent("unknown").plain != "0.00"
    assert primitives.absent("unknown").plain == "—"


def test_gated_is_distinct_from_unknown_and_carries_the_blocked_role():
    gated = primitives.absent("gated")

    assert gated.plain == "gated"
    assert gated.plain != primitives.absent("unknown").plain
    assert tokens.role(tokens.DEFAULT_THEME, "blocked") in str(gated.style)


def test_absent_refuses_an_unknown_kind():
    with pytest.raises(KeyError):
        primitives.absent("probably-fine")


def test_state_badge_pairs_the_glyph_with_its_role_colour():
    rendered = primitives.state_badge("working")

    assert rendered.plain == glyphs.STATES["working"].unicode
    assert tokens.role(tokens.DEFAULT_THEME, "accent") in str(rendered.style)


# ---------------------------------------------------------------------------
# legacy alias layer: the existing stylesheet and markup keep their vocabulary
# ---------------------------------------------------------------------------

def test_every_legacy_token_name_resolves_as_a_theme_variable():
    # The 645-line stylesheet and ~500 inline markup sites already speak this
    # vocabulary. Exposing each name as a theme variable makes all of them
    # theme-reactive without editing a single call site.
    from qlab.tui import theme as legacy

    for theme_name, theme in tokens.THEMES.items():
        missing = sorted(set(legacy.TOKENS) - set(theme.variables))
        assert not missing, f"{theme_name} does not alias: {missing}"


def test_alias_variables_carry_colours_not_placeholders():
    for theme_name, theme in tokens.THEMES.items():
        for name, value in theme.variables.items():
            assert value.startswith("#"), (
                f"{theme_name}.{name} is {value!r}, not a colour")


def test_text_strong_is_stronger_than_body_text():
    # $text_hi is used for the wordmark and selected rows. Mapping it onto plain
    # body text would flatten a hierarchy the stylesheet depends on.
    for theme_name, roles in tokens.ROLES.items():
        strong = tokens.contrast_ratio(roles["text_strong"], roles["bg"])
        body = tokens.contrast_ratio(roles["text"], roles["bg"])
        assert strong > body, f"{theme_name}: text_strong is not stronger than text"


def test_selected_row_text_is_legible_on_the_selection_background():
    # A selection colour is chrome, but text sits on it, so it needs checking
    # as a base in its own right.
    for theme_name, roles in tokens.ROLES.items():
        ratio = tokens.contrast_ratio(roles["text_strong"], roles["selection"])
        assert ratio >= 4.5, (
            f"{theme_name}: selected text on selection = {ratio:.2f}:1")


def test_stylesheets_carry_variable_references_not_baked_colours():
    # Pre-substituted hex in the stylesheet is why a theme switch could not
    # repaint chrome. The CSS must reach Textual with its variables intact.
    import re

    from qlab.tui import theme as legacy

    pattern = re.compile(r"#[0-9a-fA-F]{6}\b")
    for name in ("APP_CSS", "PAPER_MODAL_CSS", "ATLAS_DRAWER_CSS",
                 "WORKFORCE_MODAL_CSS"):
        baked = sorted(set(pattern.findall(getattr(legacy, name))))
        assert not baked, f"{name} still has baked colours: {baked}"


def test_every_variable_the_stylesheets_reference_exists_in_every_theme():
    # An unresolved variable is a hard Textual stylesheet error at startup, so
    # this is the test that keeps the app bootable.
    import re

    from qlab.tui import theme as legacy

    referenced = set()
    for name in ("APP_CSS", "PAPER_MODAL_CSS", "ATLAS_DRAWER_CSS",
                 "WORKFORCE_MODAL_CSS"):
        referenced |= set(re.findall(r"\$([a-zA-Z0-9_\-]+)", getattr(legacy, name)))

    for theme_name, theme in tokens.THEMES.items():
        # Textual supplies its own built-ins; only the qlab vocabulary is ours.
        unresolved = sorted(
            name for name in referenced
            if name not in theme.variables and not hasattr(theme, name)
        )
        assert not unresolved, f"{theme_name} cannot resolve: {unresolved}"


def test_markup_module_binds_colour_names_to_theme_variables():
    # app.py keeps its ~500 existing f-string markup sites; only the import
    # source changes, so every one of them becomes theme-reactive at once.
    from qlab.tui.design import markup

    for name in ("MUTED", "DIM", "TEXT", "TEXT_HI", "LABEL_GOLD", "AMBER",
                 "GOLD", "CYAN", "UP", "DOWN", "BORDER", "SEL_BG"):
        value = getattr(markup, name)
        assert value.startswith("$"), f"{name} is {value!r}, not a variable ref"


def test_markup_variables_all_resolve_in_every_theme():
    from qlab.tui.design import markup

    for name in markup.__all__:
        variable = getattr(markup, name).lstrip("$")
        for theme_name, theme in tokens.THEMES.items():
            assert variable in theme.variables, (
                f"{theme_name} cannot resolve ${variable} (markup.{name})")


def test_rich_markup_resolution_produces_real_colours_for_a_log_line():
    # RichLog renders through Rich, which knows nothing about theme variables,
    # so the console path must resolve them itself before writing.
    from qlab.tui.design import markup

    line = f"[{markup.MUTED}]quiet[/] [{markup.UP}]good[/]"
    resolved = markup.resolve(line, theme="qlab-light")

    assert "$" not in resolved
    assert tokens.role("qlab-light", "muted") in resolved
    assert tokens.role("qlab-light", "up") in resolved


def test_rich_markup_resolution_refuses_an_unknown_variable():
    from qlab.tui.design import markup

    with pytest.raises(KeyError):
        markup.resolve("[$not-a-token]x[/]", theme="qlab-dark")


# ---------------------------------------------------------------------------
# containment: the token boundary must not erode
# ---------------------------------------------------------------------------

def _tui_source_files():
    # Locating source for a lint is not the resource resolution that paths.py
    # governs; there is no state or config being resolved here.
    from pathlib import Path

    import qlab.tui

    return Path(qlab.tui.__file__).parent


def test_colour_literals_are_confined_to_token_modules():
    import re

    # theme.py is the retiring palette and still drives the live app, so it is
    # a declared migration exception rather than a permanent one. Every other
    # module asks for a semantic role.
    allowed = {"theme.py", "tokens.py"}
    pattern = re.compile(r"#[0-9a-fA-F]{6}\b")
    root = _tui_source_files()

    offenders = {}
    for path in sorted(root.rglob("*.py")):
        if path.name in allowed:
            continue
        found = sorted(set(pattern.findall(path.read_text(encoding="utf-8"))))
        if found:
            offenders[str(path.relative_to(root))] = found

    assert not offenders, f"colour literals outside token modules: {offenders}"


def test_design_package_uses_no_inline_colour_markup():
    import re

    # Rich markup naming a colour directly bypasses the theme, so a theme
    # switch would leave it stranded. Restricted to the design package: the
    # legacy app.py is migrated view by view.
    names = ("red", "green", "blue", "yellow", "cyan", "magenta", "white",
             "black", "orange", "amber")
    pattern = re.compile(r"\[/?(?:bold |dim |italic )*(?:" + "|".join(names) + r")\]")
    design = _tui_source_files() / "design"

    offenders = {}
    for path in sorted(design.rglob("*.py")):
        found = sorted(set(pattern.findall(path.read_text(encoding="utf-8"))))
        if found:
            offenders[path.name] = found

    assert not offenders, f"inline colour markup in the design package: {offenders}"


def test_state_badge_accepts_a_glyph_override_and_keeps_the_role_colour():
    # The working state animates through pulse frames. The character changes
    # every tick; the role colour must not.
    rendered = primitives.state_badge("working", glyph="⠙")

    assert rendered.plain == "⠙"
    assert tokens.role(tokens.DEFAULT_THEME, "accent") in str(rendered.style)


def test_state_badge_is_strict_by_default():
    with pytest.raises(KeyError):
        primitives.state_badge("not-a-real-state")


def test_state_badge_degrades_to_an_explicit_fallback():
    # A live view must not crash on an owner status the design system has not
    # seen. The state *name* is rendered as text beside the glyph, so an
    # unmapped state stays visible to the operator rather than being hidden.
    rendered = primitives.state_badge("some-future-owner-status", fallback="idle")

    assert rendered.plain == glyphs.STATES["idle"].unicode


def test_a_fallback_state_must_itself_be_known():
    # Silently accepting a bad fallback would defeat the point of the strict
    # default.
    with pytest.raises(KeyError):
        primitives.state_badge("unknown-state", fallback="also-unknown")


def test_primitives_render_in_every_theme():
    # A role that resolves in one theme and not another would surface as a
    # crash mid-session on a theme switch.
    for theme_name in tokens.THEMES:
        assert primitives.num(-0.01, "pct", signed=True, theme=theme_name).plain
        assert primitives.state_badge("failed", theme=theme_name).plain
        assert primitives.field("label", "value", theme=theme_name).plain
        assert primitives.absent("gated", theme=theme_name).plain
