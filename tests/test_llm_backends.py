"""The LLM backend layer: what a provider must answer before the desk trusts it.

Fully offline in both directions. The Ollama paths run against a threading mock
HTTP server on an ephemeral loopback port; the Claude path monkeypatches the
executable predicate and stubs ``subprocess.run``. No test reaches a live
daemon, and none bills a token.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from qlab.operator import llm_backends as backends
from qlab.operator.llm_backends import (
    BACKENDS,
    ClaudeCliBackend,
    LlmBackend,
    LlmBackendError,
    OllamaBackend,
    build_backend,
)

_TAGS_TWO = json.dumps({"models": [
    {"name": "granite3.3:8b", "size": 4_900_000_000},
    {"name": "qwen2.5:7b", "size": 4_100_000_000},
]})
_TAGS_NONE = json.dumps({"models": []})


@pytest.fixture
def ollama():
    """A programmable stand-in for the Ollama daemon on an ephemeral port.

    Routes are set per test; every POST body is captured so the request shape
    is asserted, not assumed.
    """
    routes: dict[str, tuple[int, str, str]] = {}
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):        # keep pytest output clean
            pass

        def _reply(self) -> None:
            status, ctype, body = routes.get(
                self.path, (404, "application/json", '{"error":"not found"}'))
            raw = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._reply()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length) if length else b"{}"
            seen.append({"path": self.path, "body": json.loads(payload)})
            self._reply()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{httpd.server_address[1]}",
            routes=routes, seen=seen)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def closed_port() -> int:
    """A loopback port with nothing listening on it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# -- the registry ------------------------------------------------------------

def test_registry_builds_the_known_backends():
    assert set(BACKENDS) == {"ollama", "claude"}
    assert isinstance(build_backend("ollama"), OllamaBackend)
    assert isinstance(build_backend("claude"), ClaudeCliBackend)
    assert build_backend("ollama", base_url="http://h:1").base_url == "http://h:1"


def test_an_unknown_backend_names_the_known_set():
    with pytest.raises(ValueError) as exc:
        build_backend("gpt5")
    message = str(exc.value)
    assert "gpt5" in message and "claude" in message and "ollama" in message


def test_both_backends_satisfy_the_protocol():
    for backend in (OllamaBackend(), ClaudeCliBackend()):
        assert isinstance(backend, LlmBackend)
        assert backend.name in BACKENDS


def test_the_error_type_is_reserved_for_a_backend_that_exists():
    assert issubclass(LlmBackendError, RuntimeError)
    assert "never raised" in (LlmBackendError.__doc__ or "").lower()


# -- ollama: absence is not a fault -----------------------------------------

def test_ollama_absence_reports_the_start_command_and_never_raises(closed_port):
    backend = OllamaBackend(base_url=f"http://127.0.0.1:{closed_port}")
    ok, reason = backend.available()
    assert ok is False
    assert "is not running" in reason
    assert f"127.0.0.1:{closed_port}" in reason
    assert "ollama serve" in reason
    # Absence serves nothing, and says so without an exception.
    assert backend.models() == []


def test_ollama_reports_what_it_can_serve_right_now(ollama):
    ollama.routes["/api/tags"] = (200, "application/json", _TAGS_TWO)
    backend = OllamaBackend(base_url=ollama.url)
    ok, reason = backend.available()
    assert ok is True
    # The reason is populated even on the happy path — every caller renders it.
    assert reason
    assert "2 model" in reason and ollama.url.split("//", 1)[1] in reason
    assert backend.models() == ["granite3.3:8b", "qwen2.5:7b"]


def test_ollama_running_with_nothing_pulled_cannot_serve(ollama):
    ollama.routes["/api/tags"] = (200, "application/json", _TAGS_NONE)
    backend = OllamaBackend(base_url=ollama.url)
    ok, reason = backend.available()
    assert ok is False
    assert "no models" in reason and "ollama pull" in reason
    assert backend.models() == []


def test_ollama_answering_with_garbage_is_a_loud_error(ollama):
    # A daemon that answers but is not Ollama is a misconfiguration to see,
    # not an absence to route around.
    ollama.routes["/api/tags"] = (200, "text/html", "<html>proxy login</html>")
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert "<html>proxy login</html>" in str(exc.value)
    with pytest.raises(LlmBackendError):
        backend.models()


def test_ollama_http_failure_carries_the_status_and_the_body_head(ollama):
    ollama.routes["/api/tags"] = (
        500, "application/json", '{"error":"internal explosion"}')
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert "500" in str(exc.value) and "internal explosion" in str(exc.value)


def test_ollama_json_without_a_model_list_is_an_error(ollama):
    ollama.routes["/api/tags"] = (200, "application/json", '{"ok": true}')
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert "/api/tags" in str(exc.value)


def test_a_configured_url_never_leaks_its_credentials_to_an_operator(closed_port):
    """A remote ollama is reached as http://user:token@host — the token is a
    secret, and every reason string here is served on /api/llm/backends and
    goldened by the Rust client. It may never carry one."""
    secret = "s3cr3t-token"
    host = f"127.0.0.1:{closed_port}"
    backend = OllamaBackend(base_url=f"http://desk:{secret}@{host}")

    ok, reason = backend.available()
    assert ok is False
    assert secret not in reason and "desk@" not in reason
    # The host survives: it is the part an operator needs in order to act.
    assert host in reason

    assert backend.models() == []

    with pytest.raises(LlmBackendError) as exc:
        backend.complete(system="s", user="u", model="granite3.3:8b")
    assert secret not in str(exc.value) and host in str(exc.value)

    # The connect URL keeps its credentials; only what we say out loud is safe.
    assert secret in backend.base_url
    assert secret not in backend.safe_url


@pytest.mark.parametrize("raw, expected", [
    ("http://desk:s3cr3t-token@10.0.0.5:11434", "http://10.0.0.5:11434"),
    ("http://token@10.0.0.5:11434", "http://10.0.0.5:11434"),
    # A password may itself contain "@" — the last one separates userinfo.
    ("https://u:p@ss@ollama.internal", "https://ollama.internal"),
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434/base", "http://127.0.0.1:11434/base"),
])
def test_safe_url_strips_userinfo_and_keeps_everything_else(raw, expected):
    assert backends._safe_url(raw) == expected


def test_every_daemon_message_names_the_safe_url_and_never_the_connect_url(ollama):
    """No operator-facing string may be built from the URL we connect with.

    Pinning the sentinel through every reachable-daemon message is what makes
    this inversion-proof: a future message that reaches for ``base_url`` fails
    here even when its own URL happens to be credential-free.
    """
    sentinel = "http://SAFE-SENTINEL:1234"
    messages: list[str] = []

    def probe(routes, then):
        backend = OllamaBackend(base_url=ollama.url)
        backend.safe_url = sentinel
        ollama.routes.clear()
        ollama.routes.update(routes)
        return then(backend)

    def reason_of(backend):
        return backend.available()[1]

    def error_of(backend):
        with pytest.raises(LlmBackendError) as exc:
            backend.complete(system="s", user="u", model="granite3.3:8b")
        return str(exc.value)

    # available: serving
    messages.append(probe(
        {"/api/tags": (200, "application/json", _TAGS_TWO)}, reason_of))
    # available: running but empty
    messages.append(probe(
        {"/api/tags": (200, "application/json", _TAGS_NONE)}, reason_of))
    # the unpulled-model pull command
    messages.append(probe(
        {"/api/chat": (404, "application/json", '{"error":"not found"}')},
        error_of))

    assert len(messages) == 3
    for message in messages:
        assert "SAFE-SENTINEL" in message, message
        assert ollama.url not in message, message


def test_the_not_ollama_message_also_names_the_safe_url_only(ollama):
    ollama.routes["/api/tags"] = (200, "application/json", '{"ok": true}')
    backend = OllamaBackend(base_url=ollama.url)
    backend.safe_url = "http://SAFE-SENTINEL:1234"
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert "SAFE-SENTINEL" in str(exc.value)
    assert ollama.url not in str(exc.value)


def test_an_oversized_body_is_refused_rather_than_buffered(ollama, monkeypatch):
    # The head is bounded but the read was not: a slow-drip endpoint that is not
    # ollama could stream into the owner's memory through a 2s probe.
    monkeypatch.setattr(backends, "_MAX_BODY_BYTES", 512)
    ollama.routes["/api/tags"] = (200, "application/json", "y" * 4096)
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert "512" in str(exc.value) and "refusing to buffer" in str(exc.value)


def test_an_oversized_error_body_is_refused_but_the_status_survives(ollama,
                                                                    monkeypatch):
    monkeypatch.setattr(backends, "_MAX_BODY_BYTES", 512)
    ollama.routes["/api/tags"] = (503, "application/json", "y" * 4096)
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert "503" in str(exc.value) and "body refused" in str(exc.value)


def test_a_body_exactly_at_the_ceiling_is_still_served(ollama, monkeypatch):
    payload = json.dumps({"models": [{"name": "a:1"}]})
    monkeypatch.setattr(backends, "_MAX_BODY_BYTES", len(payload))
    ollama.routes["/api/tags"] = (200, "application/json", payload)
    assert OllamaBackend(base_url=ollama.url).models() == ["a:1"]


def test_the_default_body_ceiling_is_real_and_not_only_patchable(ollama):
    ollama.routes["/api/tags"] = (
        200, "application/json", "y" * (backends._MAX_BODY_BYTES + 64))
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert "refusing to buffer" in str(exc.value)


def test_ollama_body_head_is_bounded(ollama):
    ollama.routes["/api/tags"] = (200, "text/plain", "x" * 5000)
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.available()
    assert len(str(exc.value)) < 600


# -- ollama: completion ------------------------------------------------------

def test_ollama_completion_sends_the_documented_shape_and_returns_content(ollama):
    ollama.routes["/api/tags"] = (200, "application/json", _TAGS_TWO)
    ollama.routes["/api/chat"] = (200, "application/json", json.dumps(
        {"message": {"role": "assistant", "content": "  regime is calm  "}}))
    backend = OllamaBackend(base_url=ollama.url)

    out = backend.complete(system="you are a desk analyst", user="what regime?",
                           model="granite3.3:8b", max_tokens=256)

    assert out == "regime is calm"
    sent = [row for row in ollama.seen if row["path"] == "/api/chat"]
    assert len(sent) == 1
    body = sent[0]["body"]
    assert body["model"] == "granite3.3:8b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "you are a desk analyst"},
        {"role": "user", "content": "what regime?"},
    ]
    # max_tokens is honoured, not accepted and dropped.
    assert body["options"]["num_predict"] == 256


def test_ollama_completion_of_an_unpulled_model_names_the_pull_command(ollama):
    ollama.routes["/api/chat"] = (404, "application/json", json.dumps(
        {"error": 'model "granite3.3:8b" not found, try pulling it first'}))
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.complete(system="s", user="u", model="granite3.3:8b")
    assert "ollama pull granite3.3:8b" in str(exc.value)


def test_ollama_completion_fault_other_than_a_missing_model_stays_verbatim(ollama):
    # Only 404 is translated into a pull command; every other status keeps the
    # daemon's own words rather than being guessed at.
    ollama.routes["/api/chat"] = (
        503, "application/json", '{"error":"server busy loading model"}')
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.complete(system="s", user="u", model="granite3.3:8b")
    assert "503" in str(exc.value) and "server busy" in str(exc.value)
    assert "ollama pull" not in str(exc.value)


def test_ollama_completion_without_a_daemon_fails_with_the_probe_sentence(
        closed_port):
    backend = OllamaBackend(base_url=f"http://127.0.0.1:{closed_port}")
    with pytest.raises(LlmBackendError) as exc:
        backend.complete(system="s", user="u", model="granite3.3:8b")
    assert "is not running" in str(exc.value) and "ollama serve" in str(exc.value)


def test_ollama_completion_without_content_is_an_error(ollama):
    ollama.routes["/api/chat"] = (200, "application/json", json.dumps({"done": True}))
    backend = OllamaBackend(base_url=ollama.url)
    with pytest.raises(LlmBackendError) as exc:
        backend.complete(system="s", user="u", model="granite3.3:8b")
    assert "/api/chat" in str(exc.value)


def test_ollama_read_timeout_is_absence_not_a_fault(ollama, monkeypatch):
    """A daemon that accepts the socket and never answers is unreachable."""
    class Stalling(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            threading.Event().wait(3.0)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stalling)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(backends, "PROBE_TIMEOUT_S", 0.3)
    try:
        backend = OllamaBackend(
            base_url=f"http://127.0.0.1:{httpd.server_address[1]}")
        started = time.monotonic()
        ok, reason = backend.available()
        elapsed = time.monotonic() - started
        assert ok is False and "is not running" in reason
        # The deadline is what returned, not the handler finishing: a probe
        # with no ceiling is how a threaded owner loses a worker permanently.
        assert elapsed < 2.0
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- ollama: where the URL comes from ---------------------------------------

def test_ollama_defaults_to_loopback_and_honours_the_env_override(monkeypatch):
    monkeypatch.delenv("QLAB_OLLAMA_URL", raising=False)
    assert OllamaBackend().base_url == "http://127.0.0.1:11434"
    monkeypatch.setenv("QLAB_OLLAMA_URL", "http://box:11434/")
    assert OllamaBackend().base_url == "http://box:11434"
    # An explicit argument means it; the env is only the default's override.
    assert OllamaBackend(base_url="http://other:1").base_url == "http://other:1"


# -- claude ------------------------------------------------------------------

@pytest.fixture
def claude_on_path(monkeypatch):
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable",
                        lambda: "/fake/bin/claude")


@pytest.fixture
def no_claude(monkeypatch):
    monkeypatch.setattr("qlab.tui.claude.resolve_claude_executable", lambda: None)


def test_claude_absence_uses_the_codebase_sentence(no_claude):
    backend = ClaudeCliBackend()
    ok, reason = backend.available()
    assert ok is False
    assert reason == "the `claude` CLI is not on PATH"
    assert backend.models() == []


def test_claude_available_still_explains_itself(claude_on_path):
    ok, reason = ClaudeCliBackend().available()
    assert ok is True
    assert "/fake/bin/claude" in reason


def test_claude_models_are_routing_tiers_not_api_ids(claude_on_path):
    from qlab.operator import model_routing

    models = ClaudeCliBackend().models()
    assert models == ["inherit", "sonnet", "opus", "haiku"]
    # The vocabulary the router already speaks — tiers, not brands.
    assert set(model_routing.TIER_MODEL.values()) <= set(models)
    assert set(model_routing.FAST_TIER_MODEL.values()) <= set(models)
    assert not any("claude-" in name for name in models)


def _stub_run(monkeypatch, *, returncode=0, stdout="", stderr="", raises=None):
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    return calls


def test_claude_completion_shells_a_toolless_one_shot(claude_on_path, monkeypatch):
    calls = _stub_run(monkeypatch, stdout="  the window is 60d  \n")
    out = ClaudeCliBackend().complete(
        system="you are a desk analyst", user="-what regime?", model="sonnet")

    assert out == "the window is 60d"
    argv = calls[0]["argv"]
    assert argv[0] == "/fake/bin/claude"
    assert "--print" in argv
    assert argv[argv.index("--output-format") + 1] == "text"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--system-prompt") + 1] == "you are a desk analyst"
    # No tools, no ambient MCP: the same authority posture as build_claude_argv.
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    # The throwaway cwd only stops CLAUDE.md discovery; the operator's own
    # ~/.claude settings and hooks still loaded, so a desk answer varied with
    # whose machine served it. `project` resolves inside that empty temp dir.
    assert argv[argv.index("--setting-sources") + 1] == "project"
    # "--" so a prompt beginning with a dash is never parsed as a flag.
    assert argv[-2:] == ["--", "-what regime?"]


def test_claude_completion_runs_outside_the_source_checkout(claude_on_path,
                                                            monkeypatch):
    calls = _stub_run(monkeypatch, stdout="ok")
    ClaudeCliBackend().complete(system="s", user="u", model="haiku")
    cwd = Path(calls[0]["kwargs"]["cwd"])
    # A one-shot completion must not inherit the repo's CLAUDE.md as context.
    assert not (cwd / "CLAUDE.md").exists()
    assert calls[0]["kwargs"]["timeout"] > 0


def test_claude_inherit_omits_the_model_flag(claude_on_path, monkeypatch):
    calls = _stub_run(monkeypatch, stdout="ok")
    ClaudeCliBackend().complete(system="s", user="u", model="inherit")
    assert "--model" not in calls[0]["argv"]


def test_claude_refuses_a_model_it_cannot_serve(claude_on_path, monkeypatch):
    calls = _stub_run(monkeypatch, stdout="ok")
    with pytest.raises(LlmBackendError) as exc:
        ClaudeCliBackend().complete(system="s", user="u", model="granite3.3:8b")
    assert "granite3.3:8b" in str(exc.value)
    assert calls == []


def test_claude_completion_without_the_cli_fails_with_the_probe_sentence(no_claude):
    with pytest.raises(LlmBackendError) as exc:
        ClaudeCliBackend().complete(system="s", user="u", model="sonnet")
    assert "not on PATH" in str(exc.value)


def test_claude_nonzero_exit_raises_with_the_stderr_head(claude_on_path,
                                                          monkeypatch):
    _stub_run(monkeypatch, returncode=2, stdout="", stderr="credit balance too low")
    with pytest.raises(LlmBackendError) as exc:
        ClaudeCliBackend().complete(system="s", user="u", model="sonnet")
    assert "2" in str(exc.value) and "credit balance too low" in str(exc.value)


def test_claude_empty_output_is_an_error_not_an_empty_answer(claude_on_path,
                                                              monkeypatch):
    _stub_run(monkeypatch, returncode=0, stdout="   \n")
    with pytest.raises(LlmBackendError) as exc:
        ClaudeCliBackend().complete(system="s", user="u", model="sonnet")
    assert "no output" in str(exc.value)


def test_claude_timeout_is_reported_as_a_stall(claude_on_path, monkeypatch):
    _stub_run(monkeypatch,
              raises=subprocess.TimeoutExpired(cmd="claude", timeout=600.0))
    with pytest.raises(LlmBackendError) as exc:
        ClaudeCliBackend().complete(system="s", user="u", model="sonnet")
    assert "did not answer" in str(exc.value)


def test_claude_launch_failure_is_a_backend_error(claude_on_path, monkeypatch):
    _stub_run(monkeypatch, raises=OSError("Exec format error"))
    with pytest.raises(LlmBackendError) as exc:
        ClaudeCliBackend().complete(system="s", user="u", model="sonnet")
    assert "Exec format error" in str(exc.value)


# -- the caller's deadline binds at the transport ----------------------------

def test_the_ollama_transport_gets_the_callers_clamped_deadline(monkeypatch):
    """`_deadline` is only real where a thread is actually pinned: the socket.

    A regression returning the module ceiling and ignoring the caller left every
    mocked-backend test green, because the promise the chat surface makes — 60s,
    not the 300s batch default — is a promise about the *transport* argument.
    Asserting it against a fake backend proves nothing about the wire.
    """
    seen: list[float] = []
    body = json.dumps({"message": {"content": "flat"}}).encode()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _n=None):
            return body

    def fake_urlopen(request, timeout=None):
        seen.append(timeout)
        return _Response()

    monkeypatch.setattr(backends.urllib.request, "urlopen", fake_urlopen)
    backend = OllamaBackend(base_url="http://127.0.0.1:11434")

    backend.complete(system="s", user="u", model="m", timeout=60)
    backend.complete(system="s", user="u", model="m")
    # A caller may lower the ceiling; it may never raise it.
    backend.complete(system="s", user="u", model="m", timeout=10_000)
    assert seen == [60.0, backends.COMPLETE_TIMEOUT_S,
                    backends.COMPLETE_TIMEOUT_S]


def test_the_claude_transport_gets_the_callers_clamped_deadline(claude_on_path,
                                                                monkeypatch):
    """The same promise, on the process the CLI runs in."""
    calls = _stub_run(monkeypatch, stdout="ok")
    backend = ClaudeCliBackend()

    backend.complete(system="s", user="u", model="haiku", timeout=60)
    backend.complete(system="s", user="u", model="haiku")
    backend.complete(system="s", user="u", model="haiku", timeout=10_000)
    assert [call["kwargs"]["timeout"] for call in calls] == [
        60.0, backends.CLAUDE_TIMEOUT_S, backends.CLAUDE_TIMEOUT_S]
