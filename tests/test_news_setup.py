"""The guided news-source setup: choosing sources, and writing the choice.

Every test drives the wizard through an injected ``ask``/``say`` pair, so the
prompts are exercised without a terminal, and nothing here touches the real
``.env``, the registry, or the network.
"""

from __future__ import annotations

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
    plan = news_setup.run_wizard(
        ask=_scripted([
            "maybe", "sure",          # read real news: re-asked, then yes
            "n", "n", "n", "n", "n",  # every source declined
            "n",                      # check live
        ]),
        env={}, say=lambda _line: None)
    # Nothing real was chosen, so the desk reads fixtures — and says so.
    assert plan.providers == ("synthetic",)
    assert plan.read_news is False


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
