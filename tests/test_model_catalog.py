"""The provider-agnostic model catalog, its gate, and the operator's selection.

Top-level imports are stdlib plus ``qlab.operator.models`` and the tier
constants it already reuses. That is the contract under test: adding a provider
must not require knowing about Atlas, the Claude session, or the coordinator.
The two tests that exercise the *built-in* CLI provider import ``qlab.tui.claude``
inside the test body — it is that provider's backend, not the extension seam.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import textwrap

import pytest

from qlab.operator import models
from qlab.operator.model_routing import DEEP, NONE, QUICK, REQUIRED_DEEP_ROLES, TIERS
from qlab.operator.models import (
    CLAUDE_CLI_PROVIDER,
    DEFAULT_SELECTION,
    SLOTS,
    SLOT_REQUIREMENTS,
    Completion,
    ModelNotEligible,
    ModelSelection,
    ModelSpec,
    ProviderAlreadyRegistered,
    ProviderError,
    SelectionUnreadable,
    UnknownModel,
    check_eligible,
    degraded_slots,
    eligible_models,
    estimate_tokens,
    exempt_tier_model_map,
    fits_context,
    get_model,
    get_provider,
    list_models,
    load_selection,
    providers,
    register_provider,
    resolve_selection,
    save_selection,
    tier_model_map,
    with_slot,
)
from qlab.paths import state_root


# --------------------------------------------------------------------------
# fixtures and stand-ins
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_provider_registry():
    """The registry is module state; no test may leak a provider into another."""
    saved = dict(models._PROVIDERS)
    yield
    models._PROVIDERS.clear()
    models._PROVIDERS.update(saved)


@pytest.fixture
def catalog() -> tuple[ModelSpec, ...]:
    return tuple(models.MODELS)


class FakeHttpProvider:
    """The shape a Granite/watsonx provider must have — nothing more."""

    name = "watsonx"
    required_env = ("WATSONX_API_KEY",)

    def __init__(self, ok: bool = True, reason: str = "credentials present"):
        self._ok, self._reason = ok, reason

    def configured(self) -> tuple[bool, str]:
        return (self._ok, self._reason)

    def complete(self, request):  # pragma: no cover - no test needs a completion
        raise ProviderError("the fake provider does not complete")


class StubApp:
    """Framework-independent stand-in for FastMCP: records tool names."""

    def __init__(self):
        self.names: list[str] = []

    def tool(self, name: str):
        def deco(fn):
            self.names.append(name)
            return fn

        return deco


def http_spec(**overrides) -> ModelSpec:
    base = dict(
        id="watsonx-granite-fake", provider="watsonx", label="Granite (fake)",
        tiers=(QUICK,), context_window=128_000,
        serves_claude_subagent=False, supports_workforce=False,
        supports_tools=True, launch_name="granite-fake",
        notes="test double for a provider added without editing Atlas",
    )
    base.update(overrides)
    return ModelSpec(**base)


def use_catalog(monkeypatch, *specs: ModelSpec) -> None:
    monkeypatch.setattr(models, "MODELS", tuple(specs))


# Modules a provider author must never need. Assembled from parts so the set is
# data rather than a literal this file's own import scan would match.
_FORBIDDEN_MODULES = {
    "qlab.operator." + "atlas",
    "qlab.tui." + "claude",
    "qlab.operator." + "coordinator",
}


def _imports_in(func) -> set[str]:
    """Every module name imported inside ``func``'s body."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _module_level_imports() -> set[str]:
    """Every module name this test file imports at the top level."""
    tree = ast.parse(inspect.getsource(inspect.getmodule(_imports_in)))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


# --------------------------------------------------------------------------
# the catalog
# --------------------------------------------------------------------------


def test_catalog_is_internally_consistent():
    rows = list_models()
    assert rows, "an empty catalog offers the operator nothing to select"
    ids = [spec.id for spec in rows]
    assert len(ids) == len(set(ids)), f"duplicate catalog ids: {ids}"
    for spec in rows:
        assert spec.launch_name, f"{spec.id} has no launch name to pass on"
        assert spec.tiers, f"{spec.id} serves no tier"
        assert set(spec.tiers) <= set(TIERS), spec.tiers
        assert spec.context_window > 0
        # A row naming a provider nobody registered is a picker entry that
        # cannot be used.
        assert spec.provider in providers(), (
            f"{spec.id} names provider {spec.provider!r}, which "
            "_load_builtin_providers() did not register")
        assert spec.to_dict()["coordinator_capable"] == spec.coordinator_capable


def test_unknown_model_names_the_catalog():
    with pytest.raises(UnknownModel) as exc:
        get_model("nope")
    message = str(exc.value)
    for spec in list_models():
        assert spec.id in message


def test_slot_requirements_cover_every_slot_and_only_deep_asks_twice():
    assert tuple(SLOT_REQUIREMENTS) == SLOTS
    assert SLOT_REQUIREMENTS["deep"].requires_confirmation is True
    for name in ("quick", "reasoner", "chat"):
        assert SLOT_REQUIREMENTS[name].requires_confirmation is False


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def test_deep_slot_refuses_a_non_claude_provider(monkeypatch, catalog):
    register_provider(FakeHttpProvider())
    use_catalog(monkeypatch, *catalog, http_spec(tiers=(DEEP, QUICK)))
    with pytest.raises(ModelNotEligible) as exc:
        check_eligible("watsonx-granite-fake", slot="deep")
    message = str(exc.value)
    assert "watsonx" in message
    assert "frontmatter" in message


def test_quick_slot_refuses_a_non_claude_provider(monkeypatch, catalog):
    register_provider(FakeHttpProvider())
    use_catalog(monkeypatch, *catalog, http_spec(tiers=(DEEP, QUICK)))
    with pytest.raises(ModelNotEligible) as exc:
        check_eligible("watsonx-granite-fake", slot="quick")
    message = str(exc.value)
    assert "watsonx" in message
    assert "frontmatter" in message


def test_reasoner_and_chat_slots_accept_an_http_provider(monkeypatch, catalog):
    register_provider(FakeHttpProvider())
    spec = http_spec()
    use_catalog(monkeypatch, *catalog, spec)
    assert check_eligible(spec.id, slot="reasoner") is spec
    assert check_eligible(spec.id, slot="chat") is spec
    assert spec in eligible_models("reasoner")
    assert spec not in eligible_models("deep")


def test_chat_slot_refuses_a_tool_less_model(monkeypatch, catalog):
    register_provider(FakeHttpProvider())
    use_catalog(monkeypatch, *catalog, http_spec(supports_tools=False))
    with pytest.raises(ModelNotEligible) as exc:
        check_eligible("watsonx-granite-fake", slot="chat")
    message = str(exc.value)
    assert "supports_tools" in message
    assert "allowlist" in message and "portfolio.state" in message


def test_referee_role_requires_the_deep_slot():
    role = sorted(REQUIRED_DEEP_ROLES)[0]
    # sonnet legitimately serves the quick tier, so only the role rule can refuse.
    assert QUICK in get_model("claude-sonnet-5").tiers
    with pytest.raises(ModelNotEligible) as exc:
        check_eligible("claude-sonnet-5", slot="quick", role=role)
    assert role in str(exc.value)
    assert check_eligible("claude-sonnet-5", slot="deep", role=role).id == "claude-sonnet-5"


def test_coordinator_requires_supports_workforce(monkeypatch, catalog):
    lame = ModelSpec(
        id="claude-cli-no-workforce", provider=CLAUDE_CLI_PROVIDER,
        label="CLI model that may not coordinate", tiers=(DEEP,),
        context_window=200_000, serves_claude_subagent=True,
        supports_workforce=False, supports_tools=True, launch_name="lame",
        notes="test double")
    use_catalog(monkeypatch, *catalog, lame)
    assert SLOT_REQUIREMENTS["deep"].needs_workforce is True
    with pytest.raises(ModelNotEligible) as exc:
        check_eligible(lame.id, slot="deep")
    assert "supports_workforce" in str(exc.value)


def test_deprecated_model_is_visible_but_refused():
    deprecated = [spec for spec in list_models() if spec.deprecated]
    assert deprecated, "the catalog should keep a deprecated row visible"
    spec = deprecated[0]
    with pytest.raises(ModelNotEligible) as exc:
        check_eligible(spec.id, slot="deep")
    assert spec.notes in str(exc.value)


def test_no_eligible_deep_model_refuses_loudly(monkeypatch):
    register_provider(FakeHttpProvider())
    spec = http_spec()
    use_catalog(monkeypatch, spec)
    assert eligible_models("deep") == ()
    with pytest.raises(ModelNotEligible) as exc:
        check_eligible(spec.id, slot="deep")
    assert "deep" in str(exc.value) and spec.id in str(exc.value)
    with pytest.raises(ModelNotEligible):
        resolve_selection()


# --------------------------------------------------------------------------
# context budget
# --------------------------------------------------------------------------


def test_context_refusal_names_both_numbers():
    small = http_spec(id="tiny", context_window=1_000, provider=CLAUDE_CLI_PROVIDER,
                      launch_name="tiny")
    text = "x" * 100_000
    ok, message = fits_context(text, small)
    assert ok is False
    assert str(estimate_tokens(text)) in message
    assert str(small.context_window) in message
    assert fits_context("a short question", small)[0] is True


def test_estimate_tokens_over_estimates_prose():
    prose = ("The record does not establish that the move was caused by any "
             "single headline; it establishes only what was published. ")
    text = (prose * 40)[:4000]
    assert len(text) == 4000
    assert len(text) / 4 <= estimate_tokens(text) <= len(text) / 2
    assert estimate_tokens("") == 0


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def test_absent_selection_file_is_none_not_default():
    assert not (state_root() / models.SELECTION_FILE).exists()
    assert load_selection() is None
    resolved = resolve_selection()
    assert resolved == DEFAULT_SELECTION
    assert resolved.source == "default"
    assert resolved.to_dict()["source"] == "default"


def test_corrupt_selection_file_raises_rather_than_substituting():
    path = state_root() / models.SELECTION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    # Deliberate divergence from load_desk_mode, which returns None here.
    with pytest.raises(SelectionUnreadable):
        load_selection()
    with pytest.raises(SelectionUnreadable):
        resolve_selection()


def test_selection_naming_a_vanished_model_raises():
    path = state_root() / models.SELECTION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: DEFAULT_SELECTION.slot(name) for name in SLOTS}
    payload["deep"] = "claude-retired-9"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SelectionUnreadable) as exc:
        load_selection()
    message = str(exc.value)
    assert "claude-retired-9" in message
    assert "claude-opus-5" in message  # the catalog it was checked against


def test_selection_round_trips_and_is_marked_persisted():
    chosen = with_slot(DEFAULT_SELECTION, "quick", "claude-haiku-4-5")
    save_selection(chosen)
    loaded = load_selection()
    assert loaded.quick == "claude-haiku-4-5"
    assert loaded.source == "persisted"
    assert resolve_selection() == loaded


def test_save_selection_validates_every_slot_before_writing():
    path = state_root() / models.SELECTION_FILE
    bad = with_slot(DEFAULT_SELECTION, "deep", "claude-haiku-4-5")
    with pytest.raises(ModelNotEligible):
        save_selection(bad)
    assert not path.exists()


def test_save_selection_is_atomic(monkeypatch):
    path = state_root() / models.SELECTION_FILE
    save_selection(DEFAULT_SELECTION)
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk gave up mid-rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_selection(with_slot(DEFAULT_SELECTION, "quick", "claude-haiku-4-5"))
    assert path.read_bytes() == before
    assert list(state_root().glob("*.tmp")) == []


def test_with_slot_rejects_a_slot_name_that_arrived_over_the_wire():
    assert with_slot(DEFAULT_SELECTION, "chat", "claude-haiku-4-5").chat == "claude-haiku-4-5"
    with pytest.raises(ValueError):
        with_slot(DEFAULT_SELECTION, "referee", "claude-haiku-4-5")
    with pytest.raises(ValueError):
        ModelSelection(**{n: "x" for n in SLOTS}, source="default").slot("referee")


def test_degraded_slots_names_the_slot_and_the_provider_reason():
    models._PROVIDERS[CLAUDE_CLI_PROVIDER] = FakeHttpProvider(
        ok=False, reason="no `claude` launcher on PATH")
    degraded = dict(degraded_slots(DEFAULT_SELECTION))
    assert set(degraded) == set(SLOTS)
    assert all("launcher" in reason for reason in degraded.values())
    models._PROVIDERS[CLAUDE_CLI_PROVIDER] = FakeHttpProvider(ok=True)
    assert degraded_slots(DEFAULT_SELECTION) == ()


# --------------------------------------------------------------------------
# tier maps
# --------------------------------------------------------------------------


def test_exempt_tier_map_is_unaffected_by_fast_mode():
    sel = DEFAULT_SELECTION
    deep_launch = get_model(sel.deep).launch_name
    quick_launch = get_model(sel.quick).launch_name

    assert tier_model_map(sel, fast=False)[DEEP] == deep_launch
    assert tier_model_map(sel, fast=True)[DEEP] == quick_launch
    assert tier_model_map(sel, fast=True)[QUICK] == quick_launch
    assert tier_model_map(sel, fast=True)[NONE] == "inherit"

    exempt = exempt_tier_model_map(sel)
    assert exempt[DEEP] == deep_launch
    assert exempt == exempt_tier_model_map(sel)
    # The point of the second map: the referee's model does not move when the
    # operator turns fast mode on, and it names a model rather than "inherit".
    assert exempt[DEEP] != "inherit"
    assert exempt[DEEP] != tier_model_map(sel, fast=True)[DEEP]


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


def test_registering_a_provider_twice_is_refused():
    register_provider(FakeHttpProvider())
    with pytest.raises(ProviderAlreadyRegistered):
        register_provider(FakeHttpProvider())
    assert get_provider("watsonx").name == "watsonx"


def test_unknown_provider_names_the_registered_ones():
    with pytest.raises(ProviderError) as exc:
        get_provider("granite-cloud")
    assert CLAUDE_CLI_PROVIDER in str(exc.value)


def test_configured_never_raises_and_always_gives_a_reason(monkeypatch):
    def explode():
        raise RuntimeError("PATH lookup blew up")

    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable", explode)
    ok, reason = models.AnthropicCliProvider().configured()
    assert ok is False
    assert reason.strip()
    assert "blew up" in reason


def test_configured_reports_configured_not_reachable(tmp_path, monkeypatch):
    name = "claude.cmd" if os.name == "nt" else "claude"
    fake = tmp_path / name
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    ok, reason = models.AnthropicCliProvider().configured()
    assert ok is True
    assert "configured" in reason.lower()
    # The picker must not promise reachability nothing has tested.
    assert "not reachable" in reason.lower()


def test_silent_completion_is_distinguishable_from_a_truncated_one():
    silent = Completion(text="   ", input_tokens=None, output_tokens=0,
                        latency_ms=12.0, stop_reason="end_turn", raw_model=None)
    truncated = Completion(text="   ", input_tokens=None, output_tokens=4000,
                           latency_ms=12.0, stop_reason="max_tokens", raw_model=None)
    answered = Completion(text="the record is silent on Samsung", input_tokens=10,
                          output_tokens=8, latency_ms=12.0, stop_reason="end_turn",
                          raw_model=None)
    assert silent.is_silent is True
    assert truncated.is_silent is False
    assert answered.is_silent is False
    assert silent.to_dict()["is_silent"] is True


def test_complete_reuses_build_claude_argv_read_only_shape(monkeypatch):
    from qlab.tui import claude as claude_mod

    seen: dict = {}
    real_builder = claude_mod.build_claude_argv

    def spy(prompt, **kwargs):
        seen["kwargs"] = kwargs
        seen["prompt"] = prompt
        return real_builder(prompt, **kwargs)

    monkeypatch.setattr(claude_mod, "build_claude_argv", spy)
    monkeypatch.setattr(claude_mod, "resolve_claude_executable",
                        lambda: "/opt/bin/claude")

    stdout = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the record does not establish that"}]}}),
        json.dumps({"type": "result", "result": "done",
                    "usage": {"input_tokens": 120, "output_tokens": 34}}),
    ])

    def fake_run(argv, **_kwargs):
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    spec = get_model("claude-sonnet-5")
    completion = models.AnthropicCliProvider().complete(models.CompletionRequest(
        model=spec, system="You are the desk reasoner.",
        prompt="What does the archive say about Samsung?", max_output_tokens=800))

    assert seen["kwargs"]["governed"] is False
    assert seen["kwargs"]["chat"] is False
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--agent" not in argv
    assert "--allowedTools" not in argv
    assert argv[argv.index("--model") + 1] == spec.launch_name
    assert argv.index("--model") < argv.index("--")
    assert argv[0] == "/opt/bin/claude"
    assert completion.text == "the record does not establish that"
    assert (completion.input_tokens, completion.output_tokens) == (120, 34)
    assert completion.raw_model is None  # the CLI does not report what served it
    assert completion.is_silent is False


def test_complete_refuses_an_overlong_prompt_before_running_anything(monkeypatch):
    def never(*_a, **_k):  # pragma: no cover - the point is that it is not called
        raise AssertionError("the CLI must not be launched for a refused prompt")

    monkeypatch.setattr(subprocess, "run", never)
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: "/opt/bin/claude")
    tiny = ModelSpec(
        id="claude-tiny", provider=CLAUDE_CLI_PROVIDER, label="tiny window",
        tiers=(QUICK,), context_window=1_000, serves_claude_subagent=True,
        supports_workforce=False, supports_tools=True, launch_name="tiny",
        notes="test double")
    with pytest.raises(ProviderError) as exc:
        models.AnthropicCliProvider().complete(models.CompletionRequest(
            model=tiny, system="", prompt="x" * 100_000, max_output_tokens=100))
    assert "1000" in str(exc.value)


# --------------------------------------------------------------------------
# the two boundaries this module exists to hold
# --------------------------------------------------------------------------


def test_no_agent_reachable_route_can_change_the_selection():
    from qlab.mcp import tui_proxy

    app = StubApp()
    tui_proxy.register_proxy_tools(app, object())
    assert app.names, "the proxy registers tools; an empty list proves nothing"
    for name in app.names:
        assert "model" not in name and "selection" not in name, name
    # The proxy does not know this module exists, and this module serves no HTTP.
    assert "qlab.operator.models" not in inspect.getsource(tui_proxy)
    for attr in vars(models):
        assert not attr.startswith("do_")
        assert "handler" not in attr.lower() and "route" not in attr.lower()


def test_a_new_provider_needs_no_edit_outside_this_module(monkeypatch, catalog):
    register_provider(FakeHttpProvider())
    granite = ModelSpec(
        id="watsonx-granite-4", provider="watsonx", label="IBM Granite 4 (watsonx)",
        tiers=(QUICK,), context_window=128_000, serves_claude_subagent=False,
        supports_workforce=False, supports_tools=True, launch_name="ibm/granite-4",
        notes="single-turn completions only; never Claude-subagent frontmatter")
    use_catalog(monkeypatch, *catalog, granite)

    assert granite in list_models()
    assert check_eligible(granite.id, slot="reasoner") is granite
    for slot in ("deep", "quick"):
        with pytest.raises(ModelNotEligible):
            check_eligible(granite.id, slot=slot)
    assert granite.coordinator_capable is False
    assert get_provider(granite.provider).configured() == (True, "credentials present")

    # If this test ever needs another module, the seam has failed and the design
    # should be revised rather than worked around. Checked on the parse tree
    # rather than the text, so the check cannot trip over its own literals.
    assert _imports_in(test_a_new_provider_needs_no_edit_outside_this_module) == set()
    assert _module_level_imports() & _FORBIDDEN_MODULES == set()
