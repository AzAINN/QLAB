"""The guided news-source setup: choosing sources, and writing the choice.

Every test drives the wizard through an injected ``ask``/``say`` pair, so the
prompts are exercised without a terminal, and nothing here touches the real
``.env``, the registry, or the network.
"""

from __future__ import annotations

import pathlib

import pytest

from qlab.news import setup as news_setup


def _scripted(answers):
    """An ``ask`` that replays answers and names the prompt it ran out on."""
    remaining = list(answers)

    def ask(prompt: str) -> str:
        if not remaining:
            raise AssertionError(f"the wizard asked one question too many: {prompt}")
        return remaining.pop(0)

    return ask


def _no_alpaca(monkeypatch):
    """No credential resolves — including the operator's real profile."""
    import qlab.trader.alpaca_auth as alpaca_auth

    monkeypatch.setattr(alpaca_auth, "resolve_alpaca_credentials", lambda: None)


def test_catalog_reports_what_each_source_costs_and_what_resolves_now(monkeypatch):
    _no_alpaca(monkeypatch)
    choices = {c.name: c for c in news_setup.catalog({})}

    assert [c.name for c in news_setup.catalog({})] == [
        "alpaca", "edgar", "macro", "rss", "gdelt"]
    assert "synthetic" not in choices, (
        "synthetic is the 'no real sources' answer, not a source to choose")

    assert choices["alpaca"].available is False
    assert choices["alpaca"].default is False
    assert "Alpaca" in choices["alpaca"].needs
    assert choices["edgar"].default is True
    assert choices["edgar"].needs == "QLAB_EDGAR_CONTACT"
    assert choices["macro"].default is True
    assert choices["macro"].needs == ""
    assert choices["rss"].default is False
    assert choices["gdelt"].default is False
    # The latency is the whole reason gdelt is opt-in; a blank cost would hide it.
    assert choices["gdelt"].cost.strip()
    assert choices["edgar"].tier == "primary"
    assert choices["macro"].tier == "primary"
    assert choices["alpaca"].tier == "secondary"

    # A contact already on file is availability, not a default change.
    with_contact = {c.name: c for c in news_setup.catalog(
        {"QLAB_EDGAR_CONTACT": "Jane <j@x.io>"})}
    assert with_contact["edgar"].available is True

    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: object())
    armed = {c.name: c for c in news_setup.catalog({})}
    assert armed["alpaca"].available is True
    assert armed["alpaca"].default is True


def test_declining_real_news_selects_the_labelled_fixtures(monkeypatch):
    _no_alpaca(monkeypatch)
    said: list[str] = []
    plan = news_setup.run_wizard(
        ask=_scripted(["n"]), env={}, say=said.append)

    assert plan.read_news is False
    assert plan.providers == ("synthetic",)
    assert plan.edgar_contact is None
    assert plan.verify is False
    assert any("synthetic (demo)" in line for line in said), (
        "the operator must be told what the desk will label its narrative")


def test_accepting_the_defaults_takes_the_primary_sources_and_a_contact(monkeypatch):
    _no_alpaca(monkeypatch)
    said: list[str] = []
    plan = news_setup.run_wizard(
        ask=_scripted([
            "",                       # read real news: default yes
            "",                       # alpaca: default no (no credential)
            "",                       # edgar: default yes
            "",                       # macro: default yes
            "",                       # rss: default no
            "",                       # gdelt: default no
            "Jane Doe jane@x.io",     # the EDGAR contact
            "n",                      # do not check live
        ]),
        env={}, say=said.append)

    assert plan.read_news is True
    assert plan.providers == ("edgar", "macro")
    assert plan.edgar_contact == "Jane Doe <jane@x.io>"
    assert plan.verify is False
    assert any("SEC" in line for line in said), (
        "the contact prompt must say where the string is sent")


def test_choosing_alpaca_without_a_credential_is_refused_with_the_fix(monkeypatch):
    _no_alpaca(monkeypatch)
    said: list[str] = []
    plan = news_setup.run_wizard(
        ask=_scripted([
            "y",                      # read real news
            "y",                      # alpaca — chosen, but nothing resolves
            "n",                      # edgar
            "y",                      # macro
            "n",                      # rss
            "n",                      # gdelt
            "y",                      # check live
        ]),
        env={}, say=said.append)

    assert "alpaca" not in plan.providers
    assert plan.providers == ("macro",)
    assert any("alpaca profile login" in line for line in said), (
        "a source that cannot work is refused with the fix, never kept quietly")


def test_gdelt_is_taken_only_on_a_second_confirmation(monkeypatch):
    _no_alpaca(monkeypatch)
    said: list[str] = []
    answers = ["y", "n", "n", "n", "n", "y", "{gdelt}", "n"]

    declined = news_setup.run_wizard(
        ask=_scripted([a if a != "{gdelt}" else "n" for a in answers]),
        env={}, say=said.append)
    assert "gdelt" not in declined.providers

    taken = news_setup.run_wizard(
        ask=_scripted([a if a != "{gdelt}" else "y" for a in answers]),
        env={}, say=said.append)
    assert taken.providers == ("gdelt",)
    assert any("43" in line for line in said), (
        "the measured latency is the cost the second confirmation is about")


def test_an_unparseable_answer_is_re_asked_rather_than_defaulted(monkeypatch):
    _no_alpaca(monkeypatch)
    asked: list[str] = []
    answers = iter(["maybe", "y", "n", "n", "n", "n", "n", "n"])

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(answers)

    plan = news_setup.run_wizard(ask=ask, env={}, say=lambda _line: None)
    # The first question was put twice: 'maybe' answered nothing, and the
    # wizard neither guessed a default nor fell through to the next question.
    first = [p for p in asked if "Read real news" in p]
    assert len(first) == 2, asked
    assert asked[2].startswith("  Read alpaca?"), asked
    # Every source was then declined, so the desk reads fixtures — and says so.
    assert plan.providers == ("synthetic",)
    assert plan.read_news is False


def test_three_unparseable_answers_refuse_rather_than_default(monkeypatch):
    _no_alpaca(monkeypatch)
    with pytest.raises(news_setup.SetupRefused) as refusal:
        news_setup.run_wizard(
            ask=_scripted(["maybe", "perhaps", "dunno"]),
            env={}, say=lambda _line: None)
    assert "yes or a no" in str(refusal.value)
    assert "nothing was changed" in str(refusal.value)


def test_three_invalid_contacts_refuse_with_the_expected_shape(monkeypatch):
    _no_alpaca(monkeypatch)
    with pytest.raises(news_setup.SetupRefused) as refusal:
        news_setup.run_wizard(
            ask=_scripted([
                "y", "n", "y", "n", "n", "n",   # edgar only
                "Jane Doe", "nobody", "  ",     # three refusals
            ]),
            env={}, say=lambda _line: None)
    assert "<" in str(refusal.value) and "@" in str(refusal.value)


def test_an_existing_contact_is_kept_unless_the_operator_changes_it(monkeypatch):
    _no_alpaca(monkeypatch)
    env = {"QLAB_EDGAR_CONTACT": "Old Name <old@x.io>"}
    kept = news_setup.run_wizard(
        ask=_scripted(["y", "n", "y", "n", "n", "n", "", "n"]),
        env=env, say=lambda _line: None)
    assert kept.providers == ("edgar",)
    assert kept.edgar_contact is None, "an unchanged contact is not rewritten"

    changed = news_setup.run_wizard(
        ask=_scripted(["y", "n", "y", "n", "n", "n", "n",
                       "New Name <new@x.io>", "n"]),
        env=env, say=lambda _line: None)
    assert changed.edgar_contact == "New Name <new@x.io>"


@pytest.mark.parametrize("raw,expected", [
    ("Jane Doe <jane@x.io>", "Jane Doe <jane@x.io>"),
    ("  Jane Doe   jane@x.io ", "Jane Doe <jane@x.io>"),
    ("jane@x.io", "jane@x.io"),
    ("<jane@x.io>", "jane@x.io"),
])
def test_validate_contact_normalises(raw, expected):
    assert news_setup.validate_contact(raw) == expected


@pytest.mark.parametrize("raw", ["Jane Doe", "", "   ", "x" * 201 + "@y.io"])
def test_validate_contact_refuses_anything_the_sec_would_not_accept(raw):
    with pytest.raises(ValueError) as refusal:
        news_setup.validate_contact(raw)
    assert "@" in str(refusal.value)


def test_apply_plan_rewrites_two_names_and_leaves_the_rest_byte_identical(tmp_path):
    from qlab.env import parse_env

    original = (
        "# my desk\n"
        "export ALPACA_API_KEY=pk-not-a-real-key\n"
        "\n"
        "QLAB_NEWS_PROVIDERS=rss\n"
        "export QLAB_EDGAR_CONTACT=\"Old Name <old@x.io>\"\n"
        "QLAB_DATA_PROVIDER=yfinance\n"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(original, encoding="utf-8")

    plan = news_setup.SetupPlan(
        read_news=True, providers=("edgar", "macro"),
        edgar_contact="Jane Doe <jane@x.io>", verify=False)
    environ: dict[str, str] = {}
    written = news_setup.apply_plan(plan, root=tmp_path, environ=environ)

    assert written == ["QLAB_NEWS_PROVIDERS", "QLAB_EDGAR_CONTACT"]
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# my desk"
    assert lines[1] == "export ALPACA_API_KEY=pk-not-a-real-key"
    assert lines[2] == ""
    assert lines[3] == "QLAB_NEWS_PROVIDERS=edgar,macro"
    # The `export ` prefix on the line being replaced survives.
    assert lines[4].startswith("export QLAB_EDGAR_CONTACT=")
    assert lines[5] == "QLAB_DATA_PROVIDER=yfinance"
    parsed = parse_env(env_file.read_text(encoding="utf-8"))
    assert parsed["QLAB_EDGAR_CONTACT"] == "Jane Doe <jane@x.io>"
    assert parsed["ALPACA_API_KEY"] == "pk-not-a-real-key"
    assert environ == {
        "QLAB_NEWS_PROVIDERS": "edgar,macro",
        "QLAB_EDGAR_CONTACT": "Jane Doe <jane@x.io>",
    }


def test_apply_plan_creates_the_file_and_leaves_a_contact_it_was_not_given(tmp_path):
    from qlab.env import parse_env

    plan = news_setup.SetupPlan(
        read_news=False, providers=("synthetic",), edgar_contact=None, verify=False)
    written = news_setup.apply_plan(plan, root=tmp_path, environ={})
    assert written == ["QLAB_NEWS_PROVIDERS"]
    assert parse_env((tmp_path / ".env").read_text(encoding="utf-8")) == {
        "QLAB_NEWS_PROVIDERS": "synthetic"}


def test_verify_plan_checks_the_whole_stack_with_the_contact_exported(monkeypatch):
    seen: dict = {}

    def fake_check(universe, *, provider=None, **kwargs):
        seen["universe"] = list(universe)
        seen["provider"] = provider
        seen["contact"] = environ.get("QLAB_EDGAR_CONTACT")
        return {"ok": True, "members": {"edgar": {"ok": True}, "macro": {"ok": False}}}

    monkeypatch.setattr("qlab.news.check.check_news", fake_check)
    environ: dict[str, str] = {}
    plan = news_setup.SetupPlan(
        read_news=True, providers=("edgar", "macro"),
        edgar_contact="Jane Doe <jane@x.io>", verify=True)

    report = news_setup.verify_plan(plan, ["SPY"], environ=environ)
    assert seen == {
        "universe": ["SPY"], "provider": "edgar,macro",
        "contact": "Jane Doe <jane@x.io>"}
    assert report["members"]["macro"]["ok"] is False


def test_failed_members_are_named_so_the_caller_can_offer_to_drop_them():
    report = {"members": {"edgar": {"ok": True}, "gdelt": {"ok": False,
                                                          "error": "timeout"}}}
    assert news_setup.failed_members(report) == ["gdelt"]


def test_the_macro_line_names_the_publishers_that_actually_ship(monkeypatch):
    """A hardcoded publisher list goes stale the day a feed is removed.

    BLS (403) and Treasury (404) were removed from the shipped config on
    2026-08-28; the wizard must not still be offering them.
    """
    _no_alpaca(monkeypatch)
    macro = {c.name: c for c in news_setup.catalog({})}["macro"]
    assert "Bureau of Economic Analysis" in macro.cost
    assert "BLS" not in macro.cost and "Treasury" not in macro.cost

    monkeypatch.setattr(
        "qlab.news.feed.load_news_sources",
        lambda *a, **k: {"macro": {"feeds": [{"name": "Statistics Canada"}]}})
    renamed = {c.name: c for c in news_setup.catalog({})}["macro"]
    assert "Statistics Canada" in renamed.cost


def test_the_verify_question_restates_what_gdelt_costs(monkeypatch):
    _no_alpaca(monkeypatch)
    asked: list[str] = []

    def run(answers):
        asked.clear()
        remaining = list(answers)

        def ask(prompt: str) -> str:
            asked.append(prompt)
            return remaining.pop(0)

        return news_setup.run_wizard(ask=ask, env={}, say=lambda _line: None)

    # gdelt taken: the live check will pay its latency, so the prompt says so.
    plan = run(["y", "n", "n", "n", "n", "y", "y", "n"])
    assert plan.providers == ("gdelt",)
    assert "gdelt" in asked[-1] and "minute" in asked[-1]

    # Without it the question stays the short one.
    run(["y", "n", "n", "y", "n", "n", "n"])
    assert "gdelt" not in asked[-1]


def test_a_crlf_file_round_trips_byte_identical_outside_the_two_lines(tmp_path):
    original = (
        "# desk\r\n"
        "export ALPACA_API_KEY=pk-not-a-real-key\r\n"
        "QLAB_NEWS_PROVIDERS=rss\r\n"
        "OTHER=1\r\n")
    (tmp_path / ".env").write_bytes(original.encode("utf-8"))
    plan = news_setup.SetupPlan(
        read_news=True, providers=("macro",), edgar_contact=None, verify=False)
    news_setup.apply_plan(plan, root=tmp_path, environ={})

    raw = (tmp_path / ".env").read_bytes().decode("utf-8")
    assert raw == (
        "# desk\r\n"
        "export ALPACA_API_KEY=pk-not-a-real-key\r\n"
        "QLAB_NEWS_PROVIDERS=macro\r\n"
        "OTHER=1\r\n")


def test_a_file_whose_last_line_has_no_newline_keeps_it_that_way(tmp_path):
    (tmp_path / ".env").write_text("OTHER=1", encoding="utf-8")
    plan = news_setup.SetupPlan(
        read_news=True, providers=("macro",), edgar_contact=None, verify=False)
    news_setup.apply_plan(plan, root=tmp_path, environ={})
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "OTHER=1\nQLAB_NEWS_PROVIDERS=macro")


def test_a_value_with_a_form_feed_is_not_treated_as_a_line_break(tmp_path):
    """`str.splitlines` breaks on \x0b/\x0c/\x85; a .env line does not."""
    (tmp_path / ".env").write_text("NOTE=one\x0ctwo\nOTHER=1\n", encoding="utf-8")
    plan = news_setup.SetupPlan(
        read_news=True, providers=("macro",), edgar_contact=None, verify=False)
    news_setup.apply_plan(plan, root=tmp_path, environ={})
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "NOTE=one\x0ctwo\nOTHER=1\nQLAB_NEWS_PROVIDERS=macro\n")


def test_a_duplicated_name_is_replaced_once_and_the_stale_copy_removed(tmp_path):
    """`parse_env` is last-wins, so a surviving later copy would win.

    Replacing only the first line left the desk reading the stale value it was
    just told to change — the silent kind of wrong this desk refuses.
    """
    from qlab.env import parse_env

    (tmp_path / ".env").write_text(
        "QLAB_NEWS_PROVIDERS=rss\n"
        "#QLAB_NEWS_PROVIDERS=commented-out\n"
        "OTHER=1\n"
        "export QLAB_NEWS_PROVIDERS=alpaca\n",
        encoding="utf-8")
    plan = news_setup.SetupPlan(
        read_news=True, providers=("macro",), edgar_contact=None, verify=False)
    news_setup.apply_plan(plan, root=tmp_path, environ={})

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert text == (
        "QLAB_NEWS_PROVIDERS=macro\n"
        "#QLAB_NEWS_PROVIDERS=commented-out\n"
        "OTHER=1\n")
    assert parse_env(text)["QLAB_NEWS_PROVIDERS"] == "macro"


def test_a_shell_special_value_survives_both_readers(tmp_path):
    """The file is `source`d by operators and parsed by qlab.env; both must agree."""
    import subprocess

    from qlab.env import parse_env

    news_setup.write_env_values(
        [("QLAB_EDGAR_CONTACT", 'Jane $USER `id` <j@x.io>')],
        root=tmp_path, environ={})
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert parse_env(text)["QLAB_EDGAR_CONTACT"] == 'Jane $USER `id` <j@x.io>'
    shell = subprocess.run(
        ["sh", "-c", f'. "{tmp_path / ".env"}"; printf %s "$QLAB_EDGAR_CONTACT"'],
        capture_output=True, text=True, check=True)
    assert shell.stdout == 'Jane $USER `id` <j@x.io>'


def test_a_quote_in_a_contact_is_refused_rather_than_written_wrong():
    """`.env` here has no escape syntax; a value it cannot hold is refused."""
    with pytest.raises(ValueError):
        news_setup.validate_contact("Jane O'Neill <jane@x.io>")
    with pytest.raises(ValueError):
        news_setup.write_env_values(
            [("QLAB_EDGAR_CONTACT", "Jane 'J' <j@x.io>")],
            root=pathlib.Path("/nonexistent"), environ={})
