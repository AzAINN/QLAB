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


def _fake(name: str, **attrs) -> type:
    """One fake backend class with its own probe log."""
    return type(f"Fake_{name}", (_FakeBackend,),
                {"name": name, "probes": [], **attrs})


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
    try:
        body = json.dumps({"surface": "workforce", "backend": "up",
                           "model": "m-1"}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/llm", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        response = urllib.request.urlopen(request, timeout=10)
        payload = json.loads(response.read())
        response.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert payload["workforce"] == {"backend": "up", "model": "m-1"}
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
