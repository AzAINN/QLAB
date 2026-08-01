"""Which model answers for which surface: the choice, the catalog, the routes.

Fully offline in both directions. Every backend the owner probes here is a
stand-in installed over ``BACKENDS``; no test reaches a live Ollama daemon or
the ``claude`` CLI, and the registry is always in memory.
"""

from __future__ import annotations

import pytest

from qlab.core.llm_config import (
    DEFAULT_LLM_CONFIG,
    SURFACES,
    LlmConfig,
    SurfaceModel,
    env_llm_config,
    load_llm_config,
    save_llm_config,
    startup_llm_config,
)
from qlab.state.registry import Registry


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeBackend:
    """A backend stand-in. Zero-arg constructible, like every BACKENDS entry."""

    name = "fake"
    ok = True
    why = "the fake backend is up"
    served: tuple[str, ...] = ("m-1",)
    # What available()/models() raise instead of answering. The real probes
    # raise on a garbage response by design, which is the case the catalog
    # must turn into a reason rather than a 500.
    boom: Exception | None = None
    probes: list[str] = []
    # What a completion answers, and the log of what it was asked. The chat
    # surface is the first caller that wants words back rather than a reason.
    said: str = "Flat because nothing cleared the gate; turbulence is the reason."
    fails: Exception | None = None
    calls: list[dict] = []

    def available(self) -> tuple[bool, str]:
        type(self).probes.append("available")
        if self.boom is not None:
            raise self.boom
        return bool(self.ok), self.why

    def models(self) -> list[str]:
        type(self).probes.append("models")
        if self.boom is not None:
            raise self.boom
        return list(self.served)

    def complete(self, system: str, user: str, model: str,
                 max_tokens: int = 1024, timeout: float | None = None) -> str:
        type(self).calls.append({"system": system, "user": user, "model": model,
                                 "max_tokens": max_tokens, "timeout": timeout})
        if self.fails is not None:
            raise self.fails
        return self.said


def _fake(name: str, **attrs) -> type:
    """One fake backend class with its own probe log."""
    return type(f"Fake_{name}", (_FakeBackend,),
                {"name": name, "probes": [], "calls": [], **attrs})


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """An owner whose state directory and backend registry are both disposable.

    QLAB_STATE_DIR is redirected first: ``set_llm_config`` persists, and a test
    that wrote into the checkout's ``.lab`` would change the developer's desk.
    """
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("QLAB_LLM_REASONER", raising=False)
    monkeypatch.delenv("QLAB_LLM_WORKFORCE", raising=False)

    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        yield session
    finally:
        session.registry.close()


def _install(monkeypatch, **classes) -> None:
    from qlab.operator import llm_backends

    monkeypatch.setattr(llm_backends, "BACKENDS", dict(classes))


# ---------------------------------------------------------------------------
# the persisted choice
# ---------------------------------------------------------------------------

def test_the_default_is_exactly_todays_behaviour():
    assert DEFAULT_LLM_CONFIG == LlmConfig(
        reasoner=SurfaceModel("claude", "inherit"),
        workforce=SurfaceModel("claude", "inherit"))
    # The reasoner surface does not exist yet for an operator who never chose;
    # a default that turned it on would change what the desk does on upgrade.
    assert DEFAULT_LLM_CONFIG.reasoner_enabled is False


def test_the_desk_has_exactly_two_model_surfaces():
    assert SURFACES == ("reasoner", "workforce")


def test_a_surface_model_needs_both_halves():
    with pytest.raises(ValueError, match="backend"):
        SurfaceModel("", "granite3.3:8b")
    with pytest.raises(ValueError, match="model"):
        SurfaceModel("ollama", "  ")


def test_round_trips_through_the_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    assert load_llm_config() is None            # nothing chosen yet
    chosen = LlmConfig(
        reasoner=SurfaceModel("ollama", "granite3.3:8b"),
        workforce=SurfaceModel("claude", "sonnet"),
        reasoner_enabled=True)
    save_llm_config(chosen)
    assert load_llm_config() == chosen


def test_an_unreadable_or_unrecognised_file_is_not_chosen_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    state = tmp_path / "llm_config.json"
    state.write_text("{ not json", encoding="utf-8")
    assert load_llm_config() is None
    state.write_text('{"reasoner": {"backend": "ollama"}}', encoding="utf-8")
    assert load_llm_config() is None            # half a surface is not a choice
    state.write_text('{"reasoner": "ollama", "workforce": "claude"}',
                     encoding="utf-8")
    assert load_llm_config() is None


def test_the_env_seed_splits_on_the_first_colon_only(monkeypatch):
    # An Ollama tag carries its own colon. Splitting on the last one would ask
    # for a backend called "ollama:granite3.3".
    monkeypatch.setenv("QLAB_LLM_REASONER", "ollama:granite3.3:8b")
    monkeypatch.delenv("QLAB_LLM_WORKFORCE", raising=False)
    seeded = env_llm_config()
    assert seeded.reasoner == SurfaceModel("ollama", "granite3.3:8b")
    # One var seeds one surface; the other keeps the default.
    assert seeded.workforce == DEFAULT_LLM_CONFIG.workforce


def test_no_env_var_seeds_nothing(monkeypatch):
    monkeypatch.delenv("QLAB_LLM_REASONER", raising=False)
    monkeypatch.delenv("QLAB_LLM_WORKFORCE", raising=False)
    assert env_llm_config() is None


def test_naming_a_reasoner_model_does_not_switch_the_reasoner_on(monkeypatch):
    """The desk_mode rule: explicit, never inferred.

    Reading an on-switch out of a model name is the same mistake as reading a
    live desk out of a credential that happens to exist on disk.
    """
    monkeypatch.setenv("QLAB_LLM_REASONER", "ollama:granite3.3:8b")
    assert env_llm_config().reasoner_enabled is False


@pytest.mark.parametrize("bad", ["ollama:", ":granite", "", "  "])
def test_a_malformed_env_seed_is_refused_loudly(monkeypatch, bad):
    monkeypatch.setenv("QLAB_LLM_REASONER", bad)
    with pytest.raises(ValueError, match="QLAB_LLM_REASONER"):
        env_llm_config()


def test_a_model_name_with_the_backend_forgotten_is_caught_at_the_env(monkeypatch):
    """`QLAB_LLM_REASONER=granite3.3:8b` parses cleanly as backend granite3.3.

    The form cannot tell that from a real pair, so the name is checked against
    the backend registry — a static lookup, not a probe — where the operator
    can still see which half they left out.
    """
    monkeypatch.setenv("QLAB_LLM_REASONER", "granite3.3:8b")
    with pytest.raises(ValueError, match="granite3.3") as caught:
        env_llm_config()
    assert "ollama" in str(caught.value)      # what it could have meant


def test_precedence_is_the_file_then_the_env_then_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("QLAB_LLM_REASONER", raising=False)
    monkeypatch.delenv("QLAB_LLM_WORKFORCE", raising=False)
    assert startup_llm_config() == DEFAULT_LLM_CONFIG

    monkeypatch.setenv("QLAB_LLM_WORKFORCE", "ollama:granite3.3:8b")
    assert startup_llm_config().workforce == SurfaceModel("ollama", "granite3.3:8b")

    # A chosen file wins over the env for the same reason a persisted desk mode
    # wins: it is the operator's own decision, not the shell they started in.
    save_llm_config(LlmConfig(
        reasoner=SurfaceModel("claude", "opus"),
        workforce=SurfaceModel("claude", "haiku")))
    assert startup_llm_config().workforce == SurfaceModel("claude", "haiku")


# ---------------------------------------------------------------------------
# the owner: the catalog
# ---------------------------------------------------------------------------

def test_the_catalog_reports_every_backend_with_a_reason(owner, monkeypatch):
    _install(monkeypatch,
             up=_fake("up", ok=True, why="up is up", served=("a", "b")),
             down=_fake("down", ok=False, why="down is not installed"))
    catalog = owner.llm_backends_catalog()
    entries = {entry["name"]: entry for entry in catalog["backends"]}
    assert entries["up"] == {"name": "up", "available": True,
                             "reason": "up is up", "models": ["a", "b"]}
    # Absence is a reason, never a bare False.
    assert entries["down"]["available"] is False
    assert entries["down"]["reason"] == "down is not installed"
    assert entries["down"]["models"] == []
    assert catalog["probed_at"]


def test_a_backend_that_answers_wrongly_becomes_a_reason_not_a_crash(owner, monkeypatch):
    """available() raises by design when something answers but answers wrongly.

    An uncaught one is a 500 on the picker's own route, which is the least
    useful place for a misconfiguration to surface.
    """
    from qlab.operator.llm_backends import LlmBackendError

    _install(monkeypatch, bogus=_fake(
        "bogus", boom=LlmBackendError("something other than ollama is on :11434")))
    entries = owner.llm_backends_catalog()["backends"]
    assert entries == [{"name": "bogus", "available": False, "models": [],
                        "reason": "something other than ollama is on :11434"}]


def test_an_unavailable_backend_is_never_asked_for_its_models(owner, monkeypatch):
    """models() on a dead backend is a second round trip for a known []."""
    down = _fake("down", ok=False, why="not installed")
    _install(monkeypatch, down=down)
    owner.llm_backends_catalog()
    assert down.probes == ["available"]


def test_the_catalog_is_cached_briefly_and_refresh_bypasses_it(owner, monkeypatch):
    up = _fake("up")
    _install(monkeypatch, up=up)
    owner.llm_backends_catalog()
    owner.llm_backends_catalog()
    assert up.probes.count("available") == 1        # the second read was cached

    owner.llm_backends_catalog(refresh=True)
    assert up.probes.count("available") == 2

    # The TTL is what expires it, and it is short enough that `ollama pull`
    # finishing shows up almost at once.
    monkeypatch.setattr("qlab.ui.server._LLM_CATALOG_TTL_SECONDS", 0.0)
    owner.llm_backends_catalog()
    assert up.probes.count("available") == 3


# ---------------------------------------------------------------------------
# the owner: the setter
# ---------------------------------------------------------------------------

def test_the_setter_refuses_an_uncataloged_backend_and_names_what_is_offered(
        owner, monkeypatch):
    _install(monkeypatch, up=_fake("up"))
    with pytest.raises(ValueError, match="wormhole") as caught:
        owner.set_llm_config("workforce", "wormhole", "m-1")
    assert "up" in str(caught.value)
    assert owner.llm_config == DEFAULT_LLM_CONFIG      # nothing moved


def test_the_setter_refuses_an_unavailable_backend_with_the_catalogs_own_reason(
        owner, monkeypatch):
    _install(monkeypatch, down=_fake(
        "down", ok=False, why="ollama is not running at 127.0.0.1:11434"))
    with pytest.raises(ValueError, match="not running at 127.0.0.1:11434"):
        owner.set_llm_config("workforce", "down", "m-1")


def test_the_setter_refuses_a_model_the_backend_does_not_serve(owner, monkeypatch):
    _install(monkeypatch, up=_fake("up", served=("m-1", "m-2")))
    with pytest.raises(ValueError, match="m-9") as caught:
        owner.set_llm_config("workforce", "up", "m-9")
    assert "m-1" in str(caught.value)          # what it does serve


def test_the_setter_refuses_a_surface_the_desk_does_not_have(owner, monkeypatch):
    _install(monkeypatch, up=_fake("up"))
    with pytest.raises(ValueError, match="referee"):
        owner.set_llm_config("referee", "up", "m-1")


def test_a_chosen_model_is_persisted_recorded_and_explained(owner, monkeypatch):
    _install(monkeypatch, up=_fake("up", served=("granite3.3:8b",)))
    payload = owner.set_llm_config("workforce", "up", "granite3.3:8b")

    assert payload["surface"] == "workforce"
    assert payload["workforce"] == {"backend": "up", "model": "granite3.3:8b"}
    assert payload["effect"]                              # explained, not bare
    assert owner.llm_config.workforce == SurfaceModel("up", "granite3.3:8b")

    kinds = [event["kind"] for event in owner.registry.read_events(20, None)]
    assert "llm.config_changed" in kinds
    recorded = [event for event in owner.registry.read_events(20, None)
                if event["kind"] == "llm.config_changed"][0]["payload"]
    assert recorded == {"surface": "workforce", "backend": "up",
                        "model": "granite3.3:8b", "enabled": None}

    assert load_llm_config().workforce == SurfaceModel("up", "granite3.3:8b")


def test_the_reasoner_stays_off_until_it_is_switched_on(owner, monkeypatch):
    _install(monkeypatch, up=_fake("up"))
    # Choosing a model is not turning the surface on.
    assert owner.set_llm_config("reasoner", "up", "m-1")["reasoner_enabled"] is False
    assert owner.set_llm_config(
        "reasoner", "up", "m-1", enabled=True)["reasoner_enabled"] is True
    assert load_llm_config().reasoner_enabled is True


def test_only_the_reasoner_surface_can_be_switched_off(owner, monkeypatch):
    """The workforce is what the desk already is; there is no 'off' for it."""
    _install(monkeypatch, up=_fake("up"))
    with pytest.raises(ValueError, match="reasoner"):
        owner.set_llm_config("workforce", "up", "m-1", enabled=False)


def test_the_chosen_workforce_backend_reaches_the_coordinator(owner, monkeypatch):
    """A config nothing consults is a config nothing honours (invariant 10)."""
    _, before = owner.coordinator_driver.available()
    assert "role harness" not in before        # today's desk, whatever PATH says

    _install(monkeypatch, up=_fake("up", served=("granite3.3:8b",)))
    owner.set_llm_config("workforce", "up", "granite3.3:8b")

    # Re-read on access, like fast mode: the choice binds on the next dispatch
    # rather than the next owner restart.
    ok, reason = owner.coordinator_driver.available()
    assert ok is False
    assert "role harness is not built" in reason
    assert "workforce runs on claude" in reason


def test_a_desk_restart_keeps_the_chosen_models(owner, monkeypatch):
    _install(monkeypatch, up=_fake("up", served=("granite3.3:8b",)))
    owner.set_llm_config("workforce", "up", "granite3.3:8b")

    from qlab.ui.server import UISession

    restarted = UISession(offline_default=True, registry=Registry(":memory:"))
    try:
        assert restarted.llm_config.workforce == SurfaceModel("up", "granite3.3:8b")
    finally:
        restarted.registry.close()


# ---------------------------------------------------------------------------
# the owner: what /api/tui carries
# ---------------------------------------------------------------------------

def test_the_snapshot_carries_the_config_and_never_probes(owner, monkeypatch):
    """`/api/tui` is polled every two seconds under the dispatch lock.

    A probe there would block every other request on a daemon that may be a
    network hop away — the same rule that keeps the news fetch off this path.
    """
    up = _fake("up")
    _install(monkeypatch, up=up)
    block = owner.tui_snapshot(True)["llm"]
    assert up.probes == []                       # nothing was asked
    assert block["workforce"] == {"backend": "claude", "model": "inherit"}
    assert block["reasoner_enabled"] is False
    assert block["availability"] is None          # never probed, and says so
    assert block["probed_at"] is None


def test_the_snapshot_summary_carries_reasons_but_not_the_model_lists(
        owner, monkeypatch):
    _install(monkeypatch, up=_fake("up", served=("a", "b", "c")))
    owner.llm_backends_catalog()
    block = owner.tui_snapshot(True)["llm"]
    assert block["availability"] == [
        {"name": "up", "available": True, "reason": "the fake backend is up"}]
    assert "models" not in block["availability"][0]
    assert block["probed_at"]


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------

def test_the_catalog_route_serves_the_backends(owner, monkeypatch):
    from qlab.ui.server import handle_api

    up = _fake("up")
    _install(monkeypatch, up=up)
    status, payload = handle_api(owner, "GET", "/api/llm/backends", {}, {})
    assert status == 200
    assert [entry["name"] for entry in payload["backends"]] == ["up"]

    handle_api(owner, "GET", "/api/llm/backends", {}, {})
    assert up.probes.count("available") == 1               # cached
    handle_api(owner, "GET", "/api/llm/backends", {"refresh": ["1"]}, {})
    assert up.probes.count("available") == 2


def test_the_choice_route_refuses_with_the_reason_and_accepts_with_the_payload(
        owner, monkeypatch):
    from qlab.ui.server import handle_api

    _install(monkeypatch, up=_fake("up", served=("granite3.3:8b",)))

    status, refused = handle_api(owner, "POST", "/api/llm", {},
                                 {"surface": "workforce", "backend": "up",
                                  "model": "nope"})
    assert status == 400 and "nope" in refused["error"]

    status, accepted = handle_api(owner, "POST", "/api/llm", {},
                                  {"surface": "workforce", "backend": "up",
                                   "model": "granite3.3:8b"})
    assert status == 200
    assert accepted["workforce"] == {"backend": "up", "model": "granite3.3:8b"}


def test_the_catalog_route_answers_while_the_dispatch_lock_is_held(owner, monkeypatch):
    """A settings panel must not be able to freeze the desk.

    Probing a backend is network I/O bounded only by a timeout, so under the
    owner's dispatch lock an unreachable daemon would stall every other request
    for it. Holding the lock here and still demanding an answer is the only way
    to prove the route runs outside it — the same rule the news refresh follows.
    """
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    import qlab.ui.server as server_module

    _install(monkeypatch, up=_fake("up"))
    handler = type("H", (server_module._Handler,), {"session": owner})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with server_module._LOCK:
            response = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/llm/backends", timeout=5)
            payload = json.loads(response.read())
            response.close()
        assert [entry["name"] for entry in payload["backends"]] == ["up"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_choice_route_refuses_a_non_boolean_enabled(owner, monkeypatch):
    from qlab.ui.server import handle_api

    _install(monkeypatch, up=_fake("up"))
    status, out = handle_api(owner, "POST", "/api/llm", {},
                             {"surface": "reasoner", "backend": "up",
                              "model": "m-1", "enabled": "yes"})
    assert status == 400 and "true or false" in out["error"]


# ---------------------------------------------------------------------------
# the gate must not block the way out
# ---------------------------------------------------------------------------

def test_the_reasoner_can_be_switched_off_while_the_daemon_is_down(owner, monkeypatch):
    """Enabled on ollama, ollama dies, turn it off.

    An off-switch that requires the thing to be reachable strands the operator
    with a reasoner they cannot disable: the gate blocks the one action that
    fixes the situation, and protects nothing, because the surface it guards is
    on its way to being unused.
    """
    _install(monkeypatch, ollama=_fake("ollama", served=("granite3.3:8b",)))
    owner.set_llm_config("reasoner", "ollama", "granite3.3:8b", enabled=True)
    assert owner.llm_config.reasoner_enabled is True

    # The daemon goes away, and the catalog is re-probed rather than cached.
    _install(monkeypatch, ollama=_fake(
        "ollama", ok=False, why="ollama is not running at 127.0.0.1:11434"))
    monkeypatch.setattr("qlab.ui.server._LLM_CATALOG_TTL_SECONDS", 0.0)

    payload = owner.set_llm_config("reasoner", "ollama", "granite3.3:8b",
                                   enabled=False)
    assert payload["reasoner_enabled"] is False
    assert load_llm_config().reasoner_enabled is False
    # Turning it off is not forgetting what it was pointed at.
    assert owner.llm_config.reasoner == SurfaceModel("ollama", "granite3.3:8b")

    # A pair change may ride along with the way out — nothing about it is asked
    # while the surface is off, and the enable step is where it is asked.
    owner.set_llm_config("reasoner", "ollama", "granite3.3:2b", enabled=False)
    assert owner.llm_config.reasoner == SurfaceModel("ollama", "granite3.3:2b")
    with pytest.raises(ValueError, match="not running"):
        owner.set_llm_config("reasoner", enabled=True)


def test_disabling_asks_the_catalog_nothing_at_all(owner, monkeypatch):
    up = _fake("up", served=("m-1",))
    _install(monkeypatch, up=up)
    owner.set_llm_config("reasoner", "up", "m-1", enabled=True)
    # No TTL, so anything that consults the catalog has to probe for it and
    # cannot pass on a cache warmed by the call above.
    monkeypatch.setattr("qlab.ui.server._LLM_CATALOG_TTL_SECONDS", 0.0)
    before = len(up.probes)
    owner.set_llm_config("reasoner", "up", "m-1", enabled=False)
    assert up.probes[before:] == []


def test_choosing_a_pair_while_off_is_still_caught_when_it_is_switched_on(
        owner, monkeypatch):
    """The two-step bypass: choose while off (unvalidated), then enable.

    Neither step changed both things at once, so a gate that only watched pair
    changes let an unservable pair reach an on surface. Turning a surface ON
    validates the pair it turns on, whether or not that pair is new.
    """
    _install(monkeypatch, down=_fake("down", ok=False, why="down is not running"))
    # Step one is allowed: a surface that is off is not going to ask anything.
    owner.set_llm_config("reasoner", "down", "m-1")
    assert owner.llm_config.reasoner == SurfaceModel("down", "m-1")

    # Step two is where it bites, with the catalog's own sentence.
    with pytest.raises(ValueError, match="down is not running"):
        owner.set_llm_config("reasoner", enabled=True)
    assert owner.llm_config.reasoner_enabled is False


def test_switching_on_an_available_pair_still_succeeds(owner, monkeypatch):
    _install(monkeypatch, up=_fake("up", served=("m-1",)))
    owner.set_llm_config("reasoner", "up", "m-1")
    assert owner.set_llm_config("reasoner", enabled=True)["reasoner_enabled"] is True


def test_changing_to_an_unavailable_backend_is_still_refused(owner, monkeypatch):
    """The gate is skipped for a state that reduces use, never for a new choice."""
    _install(monkeypatch, down=_fake("down", ok=False, why="down is not running"))
    with pytest.raises(ValueError, match="down is not running"):
        owner.set_llm_config("workforce", "down", "m-1")


def test_the_enabled_only_form_switches_without_touching_the_pair(owner, monkeypatch):
    """`{surface, enabled}` — the form an off-switch actually needs.

    Without it the only way to reach the switch was to re-send the pair, which
    is exactly the request the availability gate used to refuse.
    """
    from qlab.ui.server import handle_api

    _install(monkeypatch, up=_fake("up", served=("m-1",)))
    owner.set_llm_config("reasoner", "up", "m-1")

    status, payload = handle_api(owner, "POST", "/api/llm", {},
                                 {"surface": "reasoner", "enabled": True})
    assert status == 200
    assert payload["reasoner_enabled"] is True
    assert payload["reasoner"] == {"backend": "up", "model": "m-1"}

    status, payload = handle_api(owner, "POST", "/api/llm", {},
                                 {"surface": "reasoner", "enabled": False})
    assert status == 200
    assert payload["reasoner_enabled"] is False
    assert payload["reasoner"] == {"backend": "up", "model": "m-1"}


def test_a_half_choice_or_an_empty_change_is_refused(owner, monkeypatch):
    from qlab.ui.server import handle_api

    _install(monkeypatch, up=_fake("up"))
    status, out = handle_api(owner, "POST", "/api/llm", {},
                             {"surface": "reasoner", "backend": "up"})
    assert status == 400 and "both" in out["error"]
    status, out = handle_api(owner, "POST", "/api/llm", {},
                             {"surface": "reasoner"})
    assert status == 400 and "nothing to change" in out["error"]


def test_a_failed_write_never_leaves_memory_ahead_of_disk(owner, monkeypatch):
    """A choice the desk cannot persist is a choice it must not act on."""
    _install(monkeypatch, up=_fake("up", served=("m-1",)))

    def refuse(_config):
        raise OSError("read-only state directory")

    monkeypatch.setattr("qlab.ui.server.save_llm_config", refuse)
    with pytest.raises(OSError):
        owner.set_llm_config("workforce", "up", "m-1")
    assert owner.llm_config == DEFAULT_LLM_CONFIG
    # Nothing was announced that did not happen.
    assert not [event for event in owner.registry.read_events(20, None)
                if event["kind"] == "llm.config_changed"]


def test_the_choice_route_probes_outside_the_dispatch_lock(owner, monkeypatch):
    """A cold cache plus an unreachable daemon must not freeze the desk.

    The GET catalog route takes no dispatch lock at all; this one needs it for
    the registry write, so the probe is warmed before the lock is taken. The
    backend records whether the lock was held while it was asked — the only
    way to see the ordering from outside.
    """
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    import qlab.ui.server as server_module

    held: list[bool] = []

    class Watching(_FakeBackend):
        name = "up"
        probes: list[str] = []
        served = ("m-1",)

        def available(self):
            held.append(server_module._LOCK.locked())
            return super().available()

    _install(monkeypatch, up=Watching)
    handler = type("H", (server_module._Handler,), {"session": owner})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def post(body: dict) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/llm", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        response = urllib.request.urlopen(request, timeout=10)
        try:
            return json.loads(response.read())
        finally:
            response.close()

    try:
        chosen = post({"surface": "workforce", "backend": "up", "model": "m-1"})
        # An enable validates the pair it turns on, so it needs the warm cache
        # for the same reason a pair change does.
        owner.set_llm_config("reasoner", "up", "m-1")
        # A cold cache is the whole concern: with one warm from the POST above
        # the enable would validate without probing at all and prove nothing.
        owner._llm_catalog = None
        held.clear()
        switched = post({"surface": "reasoner", "enabled": True})
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert chosen["workforce"] == {"backend": "up", "model": "m-1"}
    assert switched["reasoner_enabled"] is True
    assert held and not any(held)


# ---------------------------------------------------------------------------
# the environment is validated on every desk, not only a virgin one
# ---------------------------------------------------------------------------

def test_a_malformed_env_is_refused_even_on_a_desk_that_has_already_chosen(
        tmp_path, monkeypatch):
    """Loudness that depends on state is a silent lie on every settled desk.

    `refuse_partial_env_credentials` raises on a half-set environment whether
    or not a profile exists on disk. A model routing var is no different: a
    typo that is ignored because someone once used the picker is a desk running
    on a model its operator did not name.
    """
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    save_llm_config(LlmConfig(reasoner=SurfaceModel("claude", "opus"),
                              workforce=SurfaceModel("claude", "haiku")))
    monkeypatch.setenv("QLAB_LLM_WORKFORCE", "not-a-pair")
    with pytest.raises(ValueError, match="QLAB_LLM_WORKFORCE"):
        startup_llm_config()


def test_a_valid_env_still_loses_to_a_valid_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    save_llm_config(LlmConfig(reasoner=SurfaceModel("claude", "opus"),
                              workforce=SurfaceModel("claude", "haiku")))
    monkeypatch.setenv("QLAB_LLM_WORKFORCE", "ollama:granite3.3:8b")
    assert startup_llm_config().workforce == SurfaceModel("claude", "haiku")


def test_the_owner_refuses_to_start_on_a_malformed_env(tmp_path, monkeypatch):
    """The seam where it bites: the owner `qlab tui` and `qlab ui` construct."""
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QLAB_LLM_REASONER", "wormhole:m-1")

    from qlab.ui.server import UISession

    registry = Registry(":memory:")
    try:
        with pytest.raises(ValueError, match="QLAB_LLM_REASONER"):
            UISession(offline_default=True, registry=registry)
    finally:
        registry.close()


# ---------------------------------------------------------------------------
# A1 residual: a schemeless URL is still a credential carrier
# ---------------------------------------------------------------------------

def test_a_schemeless_url_never_prints_its_userinfo():
    """`QLAB_OLLAMA_URL=desk:token@10.0.0.5:11434` is an ordinary typo.

    urlsplit finds no authority in it, so the userinfo stayed in `path` and the
    strip that protects every other form did not run — the one config mistake
    that would have put the token in a reason string, an event row and a
    checked-in golden.
    """
    from qlab.operator.llm_backends import OllamaBackend, _safe_url

    assert _safe_url("desk:s3cr3t@10.0.0.5:11434") == "10.0.0.5:11434"
    assert _safe_url("10.0.0.5:11434") == "10.0.0.5:11434"
    assert _safe_url("localhost:11434/api") == "localhost:11434/api"

    backend = OllamaBackend("desk:s3cr3t@10.0.0.5:11434")
    ok, reason = backend.available()
    assert ok is False
    assert "s3cr3t" not in reason and "10.0.0.5:11434" in reason
    # The connect URL keeps what it needs to connect with.
    assert "s3cr3t" in backend.base_url
    # ...and the sentence names the actual fault. "ollama is not running" sent
    # an operator to restart a daemon that was never addressed.
    assert "not a URL" in reason and "is not running" not in reason
    assert backend.models() == []

    # The form that did not merely mislead: with no scheme at all, urlopen
    # never runs — `Request.__init__` raises ValueError, which is neither
    # absence nor LlmBackendError, so it escaped the catalog's except clause
    # and 500'd the one route the picker needs to render itself.
    bare = OllamaBackend("10.0.0.5:11434")
    assert bare.available() == (False, bare._malformed)
    assert bare.models() == []


@pytest.mark.parametrize("bad_url, shape", [
    ("desk:s3cr3t@10.0.0.5:11434", "schemeless"),
    ("http://desk:s3cr3t@[::1", "unparseable IPv6 literal"),
    ("http://desk:s3cr3t@[::1]:11434junk", "non-numeric port"),
    ("http://desk:s3cr3t@127.0.0.1\x0b:11434", "control character in the host"),
    ("http://desk:s3cr3t@127.0.0.1:99999999999", "out-of-range port"),
])
def test_the_catalog_route_survives_the_whole_hostile_url_family(
        owner, monkeypatch, bad_url, shape):
    """One bad `QLAB_OLLAMA_URL` must never 500 the route, leak, or misdirect.

    Five shapes, found one at a time across three review rounds, each failing at
    a different depth: two are parsed oddly, one raises in `urlsplit`, one
    raises in `http.client` at connect time, and one only fails at the socket.
    Catching them individually is what kept producing a next one, so the
    properties are asserted over the family rather than the instance.

    The three properties, together, are the whole contract of this route:

    * **200** — the picker must be able to render itself and show the operator
      what is wrong. A 500 hides the config error behind a broken settings
      panel, which is the one place it could have been fixed.
    * **no secret** — `s3cr3t` must not appear anywhere in the payload. It
      escaped once through an exception that quoted the URL back at us, so this
      checks the serialized whole and not a single field.
    * **no `ollama serve`** — every one of these is a broken URL, not a stopped
      daemon, and telling an operator to restart a healthy service is advice
      that costs them the actual answer.
    """
    import json

    from qlab.ui.server import handle_api

    monkeypatch.setenv("QLAB_OLLAMA_URL", bad_url)
    status, payload = handle_api(owner, "GET", "/api/llm/backends", {}, {})

    assert status == 200, f"{shape} 500'd the picker's own route"
    entry, = [e for e in payload["backends"] if e["name"] == "ollama"]
    assert entry["available"] is False
    assert entry["models"] == []
    assert "s3cr3t" not in json.dumps(payload), f"{shape} leaked the userinfo"
    assert "ollama serve" not in entry["reason"], (
        f"{shape} is a URL fault; restarting the daemon fixes nothing")


# ---------------------------------------------------------------------------
# B1: the desk answers through the configured reasoner
# ---------------------------------------------------------------------------

def _ask(owner, text: str = "why are we flat?") -> tuple[int, dict]:
    from qlab.ui.server import handle_api

    return handle_api(owner, "POST", "/api/atlas/message", {}, {"text": text})


def _messages(owner) -> list[dict]:
    return [event for event in owner.registry.read_events(50, None)
            if event["kind"] == "atlas_message"]


def test_the_desk_answers_through_the_configured_reasoner(owner, monkeypatch):
    """The operator's question and the model's answer are two rows on one bus.

    The reply rides the EXISTING `atlas_message` kind with the text under
    `text`, because that is the key both clients already render — the Rust
    console's `subject` and the Textual timeline. A new kind would have been an
    answer no client shows.
    """
    up = _fake("up", served=("granite3.3:8b",),
               said="Flat is the right call: turbulence is at its 92nd pctile.")
    _install(monkeypatch, up=up)
    owner.set_llm_config("reasoner", "up", "granite3.3:8b")

    status, out = _ask(owner)
    assert status == 200
    assert out["answered"] is True
    assert out["backend"] == "up" and out["model"] == "granite3.3:8b"
    assert out["reply"].startswith("Flat is the right call")

    asked, answered = _messages(owner)
    assert asked["payload"]["actor"] == "operator"
    assert asked["payload"]["text"] == "why are we flat?"
    assert answered["payload"]["actor"] == "atlas"
    assert answered["payload"]["text"] == out["reply"]
    assert "error" not in answered["payload"]

    # The desk's own state is the question's context, and the budget binds.
    call, = up.calls
    assert "never" in call["system"] and "execute" in call["system"]
    assert "why are we flat?" in call["user"]
    assert '"regime_panel"' in call["user"] and '"startable"' in call["user"]
    assert call["max_tokens"] <= 700
    # A chat completion cannot ride the 300s default: the operator is waiting.
    assert call["timeout"] is not None and call["timeout"] <= 60

    # A long question reaches the model whole, and the bounded audit row says
    # it was cut rather than ending mid-sentence as though that were the ask.
    long = "why are we flat? " + "x" * 900
    _ask(owner, long)
    assert long in up.calls[-1]["user"]
    row = _messages(owner)[-2]["payload"]
    assert row["actor"] == "operator"
    assert row["text"].endswith(f"…[truncated from {len(long)} chars]")
    assert row["text"].startswith("why are we flat? xxx")

    # And the answer is cut the same way in both places it is shown: an HTTP
    # caller and the bus must not see two different cuts, one of them silent.
    up.said = "y" * 4500
    _, big = _ask(owner)
    assert big["reply"].endswith("…[truncated from 4500 chars]")
    assert _messages(owner)[-1]["payload"]["text"] == big["reply"]


def test_the_reasoner_answers_whether_or_not_the_template_flag_is_on(
        owner, monkeypatch):
    """`reasoner_enabled` gates template judgment, not the chat surface.

    Two different questions share one config: which model answers the operator,
    and whether Atlas's own template choice stops being a lookup table. Chatting
    with the desk grants no authority, so it does not wait on that switch.
    """
    up = _fake("up", served=("m-1",))
    _install(monkeypatch, up=up)
    owner.set_llm_config("reasoner", "up", "m-1")
    assert owner.llm_config.reasoner_enabled is False

    status, out = _ask(owner)
    assert status == 200 and out["answered"] is True and up.calls


def test_an_unavailable_reasoner_refuses_with_the_catalogs_own_reason(
        owner, monkeypatch):
    """No model was asked, so no answer is invented — and the reason is the
    catalog's own sentence rather than a second opinion composed here."""
    up = _fake("up", served=("m-1",))
    _install(monkeypatch, up=up)
    owner.set_llm_config("reasoner", "up", "m-1")

    down = _fake("up", ok=False,
                 why="ollama is not running at 127.0.0.1:11434 — "
                     "start it with `ollama serve`")
    _install(monkeypatch, up=down)
    monkeypatch.setattr("qlab.ui.server._LLM_CATALOG_TTL_SECONDS", 0.0)

    status, out = _ask(owner)
    assert status == 200
    assert out["answered"] is False
    assert "ollama serve" in out["note"]
    # The Rust toast reads Warn out of this word; a degraded desk must not
    # render as a receipt.
    assert "unavailable" in out["note"]
    assert "reply" not in out
    # Nothing was asked of the backend, so nothing can have been fabricated.
    assert down.calls == []
    asked, refused = _messages(owner)
    assert asked["payload"]["actor"] == "operator"
    assert refused["payload"]["actor"] == "atlas"
    assert "ollama serve" in refused["payload"]["error"]


def test_a_backend_that_fails_mid_answer_is_recorded_not_raised(owner, monkeypatch):
    """A chat failure is a desk event, never a 500 on the operator's question."""
    from qlab.operator.llm_backends import LlmBackendError

    up = _fake("up", served=("m-1",),
               fails=LlmBackendError("ollama did not answer within 60s"))
    _install(monkeypatch, up=up)
    owner.set_llm_config("reasoner", "up", "m-1")

    status, out = _ask(owner)
    assert status == 200
    assert out["answered"] is False
    assert "unavailable" in out["note"] and "60s" in out["note"]
    assert "reply" not in out
    asked, refused = _messages(owner)
    assert asked["payload"]["actor"] == "operator"
    assert "60s" in refused["payload"]["error"]
    # The refusal says the desk could not answer; it does not say anything
    # about the market.
    assert "60s" in refused["payload"]["text"]


def test_the_completion_never_runs_under_the_dispatch_lock(owner, monkeypatch):
    """A model call is the longest network wait the owner makes on request.

    Under the dispatch lock a single question would freeze every other request
    — the snapshot poll, the SSE poll, an approval — for up to the completion
    timeout. The backend proves the ordering from outside: while it is
    answering, another thread must be able to take the lock and let it go.
    """
    import json
    import threading
    import time
    import urllib.request
    from http.server import ThreadingHTTPServer

    import qlab.ui.server as server_module

    rival_took: list[bool] = []

    class Watching(_FakeBackend):
        name = "up"
        probes: list[str] = []
        calls: list[dict] = []
        served = ("m-1",)

        def complete(self, *args, **kwargs):
            took = threading.Event()

            def rival() -> None:
                if server_module._LOCK.acquire(timeout=3.0):
                    took.set()
                    server_module._LOCK.release()

            thread = threading.Thread(target=rival)
            thread.start()
            time.sleep(0.1)
            thread.join(timeout=5.0)
            rival_took.append(took.is_set())
            return super().complete(*args, **kwargs)

    _install(monkeypatch, up=Watching)
    owner.set_llm_config("reasoner", "up", "m-1")

    handler = type("H", (server_module._Handler,), {"session": owner})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/atlas/message",
            data=json.dumps({"text": "why are we flat?"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        response = urllib.request.urlopen(request, timeout=20)
        try:
            payload = json.loads(response.read())
        finally:
            response.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert payload["answered"] is True
    assert rival_took == [True], "the dispatch lock was held across the model call"
