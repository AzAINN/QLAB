"""LLM backends: the one place a model provider becomes code.

``model_routing`` decided that *tiers, not brand names, are the architecture* —
a role asks for ``deep`` or ``quick`` and the concrete model is a deployment
detail. This module is the other half of that sentence: the concrete *provider*
is a deployment detail too. A backend answers three questions and nothing else —
can you run, what can you serve, and what do you say — so adding a provider
never touches a role's authority, its tools, or the referee gate.

Two rules the whole module is built around:

* **Absence is not a fault.** A daemon that is not running and a CLI that is not
  installed are ordinary states of an operator's machine: ``available()``
  reports them as ``(False, reason)`` and ``models()`` as ``[]``. A backend that
  *is* there and answers wrongly is a fault, and raises. This is
  ``AlpacaAuthError``'s philosophy (``alpaca_auth.py:24``) applied to models.
* **The reason is always populated**, including on the happy path. Every caller
  — the ``/api/llm/backends`` catalog, the picker, a refusal toast — renders it,
  and a silent ``False`` is what made the coordinator's old availability check
  unreadable (``coordinator.py:112-118``).

No model SDK is added for this: Ollama is plain HTTP over stdlib ``urllib``
(``httpx`` is the TUI *client's* dependency, and the owner stays lean), and
Claude is the CLI the repo already resolves in one place.

The owner is the only caller: ``UISession.llm_backends_catalog`` probes every
entry in ``BACKENDS`` for ``/api/llm/backends``, and ``set_llm_config`` refuses
a choice this module reports as unavailable. Availability is asked there and
nowhere else, so a second opinion about whether a daemon is up cannot exist.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol, runtime_checkable

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"

# Probing must stay cheap enough to run inside a UI request; a completion is
# real work and gets real time. Both are explicit because a hung socket with no
# deadline is how a threaded owner loses a worker thread permanently.
PROBE_TIMEOUT_S = 2.0
COMPLETE_TIMEOUT_S = 300.0
CLAUDE_TIMEOUT_S = 600.0

# Only ever applied to a *response* body or stderr — never to a request, which
# is the side that could carry a credential.
_HEAD_CHARS = 240

# A body is bounded before it is read, not after. The excerpt below is capped,
# but `read()` with no argument is not: a slow-drip endpoint that is not ollama
# would stream into the owner's memory through a probe that has a time limit and
# no size limit. Anything past this is not a model answer.
_MAX_BODY_BYTES = 1 << 20


class LlmBackendError(RuntimeError):
    """A backend exists but is unusable.

    Never raised for absence by a probe: ``available()`` reports a missing
    backend as ``(False, reason)`` and ``models()`` as ``[]``, because "not
    installed" is not a fault worth a traceback. ``complete()`` is the one
    place absence becomes an error — it was asked to do work it cannot do —
    and it then carries the same operator-facing sentence the probe would give.
    """


@runtime_checkable
class LlmBackend(Protocol):
    """What every provider must answer before the desk will route to it."""

    name: str

    def available(self) -> tuple[bool, str]:
        """Can this backend serve right now, and the reason either way."""

    def models(self) -> list[str]:
        """What this backend can serve RIGHT NOW — empty when it cannot."""

    def complete(self, system: str, user: str, model: str,
                 max_tokens: int = 1024, timeout: float | None = None) -> str:
        """One blocking, conclusions-only completion. Raises, never returns ''.

        ``timeout`` is the caller's own ceiling in seconds; ``None`` takes the
        backend's default. A caller with a human waiting on the answer sets it
        far below that default, and a backend clamps rather than trusts — a
        request handler that inherited a batch deadline would hold a worker
        thread for minutes on a question the operator gave up on.
        """


# A credential shape: a run of characters ending in "@", which is what
# userinfo looks like wherever it appears — in a URL this module built, in a
# URL an exception quoted back at it, or in a body a daemon echoed.
_USERINFO = re.compile(r"[^\s/@]+@")


def _redact(text: str) -> str:
    """Anything shaped like userinfo, removed.

    Deliberately over-broad. It cannot know whether ``a@b`` is a credential or
    an ordinary address, and the two costs are not symmetric: an over-redacted
    error message is mildly less useful, while an under-redacted one puts a
    token in a UI, an event row and a checked-in golden at once. So the rule is
    the shape, not the provenance — anything that *looks* like userinfo goes,
    and an email address in a model's answer will be redacted too. That is the
    accepted trade, not an oversight.
    """
    return _USERINFO.sub("…@", text)


def _head(raw: bytes | str) -> str:
    """A bounded, single-line, REDACTED excerpt of what a backend said back.

    The module's one gate for foreign text, and it is a gate in two senses:
    nothing arbitrary reaches an operator-facing message longer than
    ``_HEAD_CHARS``, and nothing reaches one carrying userinfo. Both live here
    rather than at the call sites for the same reason — there are six call
    sites and every future one gets the guarantee by construction. A fourth
    URL-shaped leak got in exactly because one new call site interpolated an
    exception it had reasoned about individually (``InvalidURL`` quotes the URL
    it rejected, userinfo included), which is the argument against ever
    deciding this per call site again.

    ``_safe_url`` is NOT a second gate, and an earlier version of this
    docstring claiming the two "cover every string between them" was the false
    half of the sentence that let shape six through. It is *best-effort
    pre-redaction* of the configured URL — better placed, because it keeps the
    host readable instead of collapsing it to ``…@host`` — and it decides per
    parse branch, which is the kind of judgment that has now leaked twice.

    So every message this module assembles is passed through here as well,
    including ones already built from a ``_safe_url``'d string. Redaction is
    idempotent on a clean string, so the cost is nothing. What that buys is
    honestly partial and worth stating exactly: a ``_safe_url`` regression is
    **narrowed** here, not erased — ``_USERINFO`` stops at ``/``, so
    ``desk:s3c/r3t@host`` loses its ``r3t@`` tail and keeps ``desk:s3c``. Full
    coverage of the URL is ``_safe_url``'s job; this is the floor under it, and
    the route test asserts on the whole payload rather than on ``@`` alone
    because of precisely that gap.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    # Collapse first: a control character is whitespace to `split()`, so the
    # redaction below scans exactly the string that will be shown, and a
    # credential broken by one cannot slip past on a technicality.
    text = _redact(" ".join(text.split()))
    if len(text) > _HEAD_CHARS:
        return text[:_HEAD_CHARS] + "…"
    return text


def _safe_url(url: str) -> str:
    """``url`` with any userinfo removed — scheme, host and port only.

    A remote or proxied Ollama is a deployment this module explicitly invites,
    and ``http://user:token@host:11434`` is how such a daemon is reached, so the
    configured URL is a credential carrier. Every operator-facing string here
    names the URL, A2 serves those strings on ``/api/llm/backends``, and Phase D
    goldens them — an un-stripped URL would put a token in a UI, an event row
    and a checked-in test fixture at once. Same rule as ``alpaca_auth``: the
    secret never leaves in printable form.

    Stripping rather than masking is deliberate: a mask still shows the shape of
    the credential, and the host is the only part an operator needs to act on.
    """
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError:
        # `http://[::1` — an unclosed IPv6 literal — is one of several strings
        # urlsplit refuses to parse at all. Refusing to parse is exactly when
        # this function knows least about what it is holding, so it redacts
        # hardest: keep only what follows the last `@` and never echo a string
        # whose structure we could not read.
        return url.rsplit("@", 1)[-1]
    if not split.netloc:
        # A schemeless URL is an ordinary config typo, and urlsplit finds no
        # authority in one: `desk:token@10.0.0.5:11434` parses as scheme
        # `desk` with the userinfo left in `path`, so the strip below never
        # ran and the token printed verbatim. Same rule, applied to the raw
        # string: keep the host, drop everything before the last `@`.
        return url.rsplit("@", 1)[-1] if "@" in url else url
    if "@" not in split.netloc:
        # The parse says there is no userinfo; the raw string can still hold
        # it. A "/" inside a password — ordinary in a base64 token —
        # terminates the authority early, so `http://desk:s3c/r3t@host:11434`
        # parses with netloc `desk:s3c` and the whole credential sitting in
        # `path`, and returning `url` here handed it back verbatim.
        #
        # The rule this function obeys, in every branch: it never returns a
        # string with `@`-prefixed userinfo in it, however the parse went.
        # When the parse and the raw string disagree about whether there is
        # any, the raw string wins, because it is what would be printed.
        return url.rsplit("@", 1)[-1] if "@" in url else url
    # rsplit: a password may itself contain "@".
    host = split.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit(
        (split.scheme, host, split.path, split.query, split.fragment))


class _Oversize(Exception):
    """Internal: a body larger than this module will buffer."""


def _read_bounded(stream) -> bytes:
    """Read a response body with a ceiling, detecting overflow rather than truncating.

    One byte past the ceiling is requested so a body that exactly fills it is
    still served; more than that is refused loudly instead of silently becoming
    a truncated "answer".
    """
    raw = stream.read(_MAX_BODY_BYTES + 1)
    if len(raw) > _MAX_BODY_BYTES:
        raise _Oversize(f"more than {_MAX_BODY_BYTES} bytes")
    return raw


class _Unreachable(Exception):
    """Internal: nothing answered on the socket. Absence, never a fault."""


def _deadline(asked: float | None, ceiling: float) -> float:
    """The caller's timeout, never above the backend's own ceiling.

    Clamped rather than trusted: a deadline is a promise about how long a
    thread is held, and a backend that honoured an arbitrary caller value would
    let one request pin an owner worker for as long as it liked.
    """
    if asked is None:
        return ceiling
    return min(float(asked), ceiling)


class _HttpFault(LlmBackendError):
    """A backend answered with an error status. Carries it as data.

    The status is an attribute rather than a substring of the message so a
    caller that treats one code specially (an unpulled model is a 404) reads
    the number instead of grepping its own sentence.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class OllamaBackend:
    """A local Ollama daemon, reached over plain HTTP with no SDK."""

    name = "ollama"

    def __init__(self, base_url: str = OLLAMA_DEFAULT_URL) -> None:
        # The env override replaces the *default*, not an explicit argument: a
        # caller that names a URL (a second host, a test's mock server) means it.
        resolved = base_url
        if base_url == OLLAMA_DEFAULT_URL:
            resolved = (os.environ.get("QLAB_OLLAMA_URL", "").strip()
                        or OLLAMA_DEFAULT_URL)
        self.base_url = resolved.rstrip("/")
        # Two URLs, deliberately: ``base_url`` is what we connect to and may
        # carry userinfo; ``safe_url`` is the ONLY one this module is allowed to
        # say out loud. Every operator-facing string below reads safe_url.
        self.safe_url = _safe_url(self.base_url)
        # A config typo is not a stopped daemon, and saying "ollama is not
        # running" about `10.0.0.5:11434` sends an operator to restart a service
        # that was never addressed. Worse, the two failed differently: urlopen
        # turns a bad *scheme* into an OSError (absence), while a string with no
        # scheme at all fails in ``Request.__init__`` with a ValueError — which
        # is neither absence nor ``LlmBackendError`` and escaped the catalog's
        # own except clause as a 500. One check, before the socket, for all of
        # them — including the strings urlsplit will not parse at all
        # (``http://[::1``, an unclosed IPv6 literal), which raised the same
        # ValueError one frame earlier, out of the constructor itself.
        try:
            split = urllib.parse.urlsplit(self.base_url)
            # Asking for the port IS the check: `.port` is parsed lazily and
            # raises on a non-numeric (`:11434junk`) or out-of-range
            # (`:99999999999`) one. Asking here moves both shapes to the same
            # answer as every other bad URL — before the socket, where the
            # advice can name the URL. Left to the socket, the overflow shape
            # surfaced as an OSError and drew "start it with `ollama serve`",
            # which is the wrong instruction for a daemon that is fine.
            _ = split.port
            usable = split.scheme in ("http", "https") and bool(split.netloc)
        except ValueError:
            usable = False
        self._malformed: str | None = (
            None if usable
            else _head(f"not a URL: {self.safe_url} — QLAB_OLLAMA_URL must "
                       f"be a parseable http(s) URL, e.g. "
                       f"{OLLAMA_DEFAULT_URL}"))

    # -- transport ----------------------------------------------------------

    @property
    def _absent_reason(self) -> str:
        return _head(f"ollama is not running at {self.safe_url} — "
                     "start it with `ollama serve`")

    def _request(self, path: str, *, payload: dict | None = None,
                 timeout: float) -> dict:
        """One JSON round trip. Absence raises ``_Unreachable``; a fault raises.

        ``HTTPError`` is caught before ``OSError`` because it is a ``URLError``
        subclass that means the opposite thing: the daemon *did* answer. Both
        ``URLError`` (refused, DNS) and a socket timeout are ``OSError``, so the
        single broad clause below is the absence case in full.
        """
        if self._malformed is not None:
            # Raised as absence so all three protocol methods report it in their
            # own shape — (False, reason), [], and LlmBackendError — from one
            # check, instead of each learning what a bad URL looks like.
            raise _Unreachable(self._malformed)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=body,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = _read_bounded(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = _head(_read_bounded(exc) or b"")
            except _Oversize as big:
                detail = f"body refused, {big}"
            raise _HttpFault(
                _head(f"ollama answered {path} with HTTP {exc.code}: {detail}"),
                exc.code) from None
        except _Oversize as big:
            raise LlmBackendError(_head(
                f"ollama answered {path} with {big} — refusing to buffer it; "
                "that is not a model answer")) from None
        except http.client.HTTPException as exc:
            # A URL that parses and still cannot be used. `http://host:11434junk`
            # has a scheme and an authority, so the not-a-URL guard passes it,
            # and urlopen rejects it only at connect time with InvalidURL — an
            # HTTPException, which is NOT an OSError, so the absence clause
            # below never saw it and it escaped the catalog as a 500.
            #
            # A fault rather than absence, deliberately: nothing is wrong with
            # the daemon, and "start it with `ollama serve`" would be the same
            # wrong advice the schemeless case used to give. The exception text
            # is bounded and carries no URL of its own ("nonnumeric port:
            # '11434junk'"), so the safe form is the only address named.
            raise LlmBackendError(_head(
                f"ollama cannot be reached at {self.safe_url}: "
                f"{_head(str(exc))}")) from None
        except OSError:
            raise _Unreachable(self._absent_reason) from None
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LlmBackendError(_head(
                f"ollama answered {path} with a non-JSON body: {_head(raw)}"
            )) from None
        if not isinstance(decoded, dict):
            raise LlmBackendError(_head(
                f"ollama answered {path} with {type(decoded).__name__}, "
                "not an object"))
        return decoded

    def _pulled(self, timeout: float) -> list[str]:
        payload = self._request("/api/tags", timeout=timeout)
        entries = payload.get("models")
        if not isinstance(entries, list):
            raise LlmBackendError(_head(
                "ollama answered /api/tags without a model list — "
                f"something other than ollama is on {self.safe_url}"))
        names = []
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
        return names

    # -- protocol -----------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        """Reachable *and* holding at least one model.

        A daemon with nothing pulled is running and can serve nothing, so it is
        not available — but the reason names the fix rather than hiding the
        distinction behind a bare False.
        """
        try:
            names = self._pulled(PROBE_TIMEOUT_S)
        except _Unreachable as exc:
            return False, str(exc)
        host = self.safe_url.split("//", 1)[-1]
        if not names:
            return False, _head(
                f"ollama is running at {host} but no models are pulled — "
                "pull one with `ollama pull granite3.3:8b`")
        plural = "" if len(names) == 1 else "s"
        return True, _head(
            f"ollama at {host}, {len(names)} model{plural} pulled")

    def models(self) -> list[str]:
        try:
            return self._pulled(PROBE_TIMEOUT_S)
        except _Unreachable:
            return []

    def _chat(self, payload: dict, model: str, timeout: float | None) -> dict:
        """The ``/api/chat`` round trip both public entry points share.

        Kept as one function so the two model-facing calls cannot drift on the
        transport's own vocabulary: the same clamp, and the same translation of
        the one actionable fault (an unpulled model) into the pull command.
        """
        try:
            return self._request(
                "/api/chat", payload=payload,
                timeout=_deadline(timeout, COMPLETE_TIMEOUT_S))
        except _Unreachable as exc:
            raise LlmBackendError(str(exc)) from None
        except _HttpFault as exc:
            # The one actionable fault: the model simply is not on this host.
            # The operator gets the command, not a 404.
            if exc.status == 404:
                raise LlmBackendError(_head(
                    f"ollama has no model {model!r} at {self.safe_url} — "
                    f"pull it with `ollama pull {model}`")) from None
            raise

    def chat(self, messages: list[dict], model: str, *,
             tools: list[dict] | None = None,
             timeout: float | None = None) -> dict:
        """One blocking tools-capable turn; returns the assistant message.

        Deliberately NOT part of ``LlmBackend``: tool calling is a capability,
        not a promise every provider makes, and the Claude backend here is a
        conclusions-only CLI with no tool path at all. A caller that needs
        tools asks for this concrete backend and says so in its own signature.

        The request and reply shapes were verified against a live Ollama
        0.31.2 daemon rather than taken from memory: ``tools`` is a list of
        ``{"type": "function", "function": {name, description, parameters}}``,
        and the reply's ``message`` carries ``tool_calls[].function.{name,
        arguments}`` with ``arguments`` already decoded into an object. A tool
        result is fed back as ``{"role": "tool", "tool_name": …, "content":
        …}``, which the same daemon answered with a plain final message.

        The message is returned whole, unread. Deciding what a tool call means
        is the role harness's job and doing any of it here would put a second
        opinion about authority in the transport.
        """
        payload: dict = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        response = self._chat(payload, model, timeout)
        message = response.get("message")
        if not isinstance(message, dict):
            raise LlmBackendError(_head(
                f"ollama answered /api/chat for {model!r} without a message: "
                f"{_head(json.dumps(response))}"))
        return message

    def complete(self, system: str, user: str, model: str,
                 max_tokens: int = 1024, timeout: float | None = None) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            # Ollama's name for a completion cap. Passed through rather than
            # accepted and dropped — a budget the caller sets must bind.
            "options": {"num_predict": max_tokens},
        }
        response = self._chat(payload, model, timeout)
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LlmBackendError(_head(
                f"ollama answered /api/chat for {model!r} with no content: "
                f"{_head(json.dumps(response))}"))
        return content.strip()


# ---------------------------------------------------------------------------
# Claude CLI
# ---------------------------------------------------------------------------

# The routing vocabulary, not API ids: ``inherit`` follows the operator's own
# session (model_routing.TIER_MODEL[DEEP]), the rest are the CLI's tier aliases.
# Pinning a dated model id here would put a brand name back in the architecture.
CLAUDE_MODELS = ("inherit", "sonnet", "opus", "haiku")

_NO_CLAUDE_REASON = "the `claude` CLI is not on PATH"


class ClaudeCliBackend:
    """One-shot ``claude --print`` completions with no tools and no MCP."""

    name = "claude"

    @staticmethod
    def _executable() -> str | None:
        """The codebase's single ``claude`` predicate, imported lazily.

        ``qlab.tui.__init__`` pulls in Textual — an optional extra — so a
        module-level import would make the owner's backend layer depend on the
        TUI's install. ``coordinator.py:126`` sets the same precedent.
        """
        from qlab.tui.claude import resolve_claude_executable
        return resolve_claude_executable()

    def available(self) -> tuple[bool, str]:
        try:
            executable = self._executable()
        except Exception as exc:      # pragma: no cover - import-time only
            return False, _head(f"claude support unavailable: {exc}")
        if not executable:
            return False, _NO_CLAUDE_REASON
        return True, _head(f"claude CLI at {executable}")

    def models(self) -> list[str]:
        ok, _ = self.available()
        return list(CLAUDE_MODELS) if ok else []

    def _argv(self, executable: str, system: str, user: str,
              model: str) -> list[str]:
        """The same authority posture as ``build_claude_argv``'s ask session.

        Empty MCP config plus ``--strict-mcp-config`` so no ambient server is
        inherited, ``--tools ""`` so no built-in tool exists, slash commands
        off. A conclusions-only completion needs none of them, and a backend
        that could reach a tool would be a second execution path (invariant 3).

        ``--system-prompt`` replaces rather than appends (``--append-…``): the
        caller's prompt is the whole instruction, and Claude Code's coding-agent
        preamble would otherwise answer a desk question as an engineer.

        ``--setting-sources project`` completes the isolation the throwaway cwd
        only started. That cwd stops CLAUDE.md discovery and nothing else: the
        operator's own ``~/.claude/settings.json`` — their hooks, their
        permissions, their status line — still loaded, so a desk answer varied
        with whose machine the owner ran on. Naming ``project`` alone drops the
        user and local sources, and the project source resolves inside the empty
        temp directory below, which has none. The flag is verified present in
        ``claude --help``; an unknown flag would make the CLI exit non-zero and
        every completion an ``LlmBackendError``.
        """
        argv = [
            executable,
            "--print",
            "--output-format", "text",
            "--system-prompt", system,
            "--strict-mcp-config",
            "--mcp-config", json.dumps({"mcpServers": {}}),
            "--setting-sources", "project",
            "--tools", "",
            "--disable-slash-commands",
            "--no-chrome",
        ]
        if model != "inherit":
            argv.extend(["--model", model])
        if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
            # CreateProcess rejects a shell script directly (claude.py:1013).
            argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", *argv]
        # "--" so a prompt beginning with a dash is never parsed as a flag.
        argv.extend(["--", user])
        return argv

    def complete(self, system: str, user: str, model: str,
                 max_tokens: int = 1024, timeout: float | None = None) -> str:
        # ``max_tokens`` has no CLI flag. It is part of the protocol so callers
        # can budget a backend that honours it; here it cannot bind, and saying
        # so in a comment beats pretending it does. ``timeout`` does bind —
        # subprocess enforces it — so the two are not the same kind of promise.
        if model not in CLAUDE_MODELS:
            raise LlmBackendError(_head(
                f"the claude backend cannot serve model {model!r}; "
                f"it serves {', '.join(CLAUDE_MODELS)}"))
        executable = self._executable()
        if not executable:
            raise LlmBackendError(_NO_CLAUDE_REASON)
        argv = self._argv(executable, system, user, model)
        deadline = _deadline(timeout, CLAUDE_TIMEOUT_S)
        try:
            # A throwaway cwd: run from the checkout and the CLI would discover
            # the repo's CLAUDE.md and answer with the desk's build context
            # loaded. The same isolation claude.py gives a governed session.
            with tempfile.TemporaryDirectory(prefix="qlab-llm-") as workdir:
                done = subprocess.run(
                    argv, cwd=workdir, capture_output=True, text=True,
                    # The CLI emits UTF-8; the OS locale would mojibake it and a
                    # stray byte would kill the read (claude.py:1022-1026).
                    encoding="utf-8", errors="replace",
                    timeout=deadline)
        except subprocess.TimeoutExpired:
            raise LlmBackendError(
                f"the claude CLI did not answer within {deadline:.0f}s"
            ) from None
        except OSError as exc:
            raise LlmBackendError(
                _head(f"the claude CLI could not be run: {exc}")) from None
        if done.returncode != 0:
            raise LlmBackendError(_head(
                f"the claude CLI exited {done.returncode}: "
                f"{_head(done.stderr or done.stdout or '')}"))
        text = (done.stdout or "").strip()
        if not text:
            raise LlmBackendError(_head(
                "the claude CLI exited 0 with no output: "
                f"{_head(done.stderr or '')}"))
        return text


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

BACKENDS: dict[str, type] = {
    "ollama": OllamaBackend,
    "claude": ClaudeCliBackend,
}


def build_backend(name: str, **kwargs) -> LlmBackend:
    """Construct a backend by registry name; an unknown name is loud."""
    backend = BACKENDS.get(name)
    if backend is None:
        raise ValueError(
            f"unknown LLM backend {name!r}; known backends: "
            f"{', '.join(sorted(BACKENDS))}")
    return backend(**kwargs)
