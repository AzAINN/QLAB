"""The model catalog, the provider seam, and the operator's selection.

Three things live here and nothing else: *what models exist* (a static table),
*who can serve them* (a registry of providers), and *which one the operator
picked for each slot* (a persisted, validated selection). A second engineer
adding IBM Granite / watsonx should need to touch this module and no other —
that is the whole point of the seam, and one test asserts it by importing
nothing else.

Two capabilities are declared separately because they fail differently.
``supports_workforce`` says a model may be the coordinator that dispatches the
Agent tool; ``serves_claude_subagent`` says its launch name may appear as
``model:`` frontmatter in an agent ``.md`` (claude.py:675-677). The deep and
quick slots become that frontmatter by way of ``resolve_route`` → ``_routed_model``
→ ``build_workforce_agents``, so a non-Claude id selected for either slot would
be handed to the Claude CLI as a model it cannot run, with nothing refusing.
Hence: **an HTTP provider is eligible for the reasoner and chat slots only.**
That is the extension story in one sentence — a new provider serves single-turn,
tool-free completions and is structurally barred from the referee's tier.

``check_eligible`` is authority-before-availability, in the same shape as
``templates.check_startable`` (templates.py:123): a return-or-raise gate whose
refusals name what was refused and why. Availability is deliberately *not* a
catalog field — the table is static, the environment is not, so a picker asks
``provider.configured()`` per request instead of reading a stale boolean.

Nothing here is agent-reachable. This module registers no HTTP route and no MCP
tool; a reasoner that could pick its own referee model could pick a weaker one.
It also holds no cached selection and no lock — ``resolve_selection`` reads the
file each call and returns a frozen value, and the threaded owner (invariant 9)
caches it under its own lock.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Protocol, get_args

from qlab.operator.model_routing import (  # tiers and the exemption live there
    DEEP,
    NONE,
    QUICK,
    REQUIRED_DEEP_ROLES,
    TIERS,
)
from qlab.paths import state_path, state_root  # invariant 6

Tier = Literal["deep", "quick", "none"]

# The Literal above cannot be built from the imported constants, so tie the two
# together loudly: a rename in model_routing must not silently leave this module
# validating against stale tier names.
if get_args(Tier) != TIERS:
    raise RuntimeError(
        f"tier names drifted: model_routing.TIERS={TIERS} but models.Tier="
        f"{get_args(Tier)}; update this module with model_routing")

# The four things a model can be selected for. deep/quick are role tiers that
# become agent frontmatter; reasoner/chat are single completions the owner makes
# itself. They are different authorities, not different sizes.
SLOTS: tuple[str, ...] = ("deep", "quick", "reasoner", "chat")

CLAUDE_CLI_PROVIDER = "anthropic_cli"
SELECTION_FILE = "model_selection.json"

# Prompts are refused at 90% of the window rather than 100%: the token estimate
# below is a heuristic, and the remaining tenth is the margin that keeps a
# refusal from turning into a server-side truncation.
CONTEXT_SAFETY = 0.9


class ModelError(RuntimeError):
    """A model could not be used, with the reason in the message."""


class UnknownModel(ModelError):
    """No catalog entry carries that id."""


class ModelNotEligible(ModelError):
    """A known model may not serve the requested slot."""


class SelectionUnreadable(ModelError):
    """The persisted selection exists but cannot be trusted."""


class ProviderError(RuntimeError):
    """A provider refused or failed to produce a completion."""


class ProviderAlreadyRegistered(RuntimeError):
    """A second provider claimed a name already taken."""


# --------------------------------------------------------------------------
# The catalog
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """One row of the static catalog.

    There is no ``available`` field on purpose: availability depends on the
    environment (a launcher on PATH, an API key) and is computed per call from
    ``provider.configured()``. Conflating the two makes the picker offer a row
    it will then refuse.
    """

    id: str
    provider: str
    label: str
    tiers: tuple[str, ...]
    context_window: int
    serves_claude_subagent: bool   # may appear as `model:` frontmatter in an agent .md
    supports_workforce: bool       # may dispatch the Agent tool as coordinator
    supports_tools: bool           # may be handed an MCP allowlist
    launch_name: str               # what reaches --model / the provider
    notes: str
    deprecated: bool = False

    def __post_init__(self) -> None:
        # Validated at construction so a new provider's first bad row fails on
        # import rather than at selection time, in someone else's stack trace.
        if not self.id or not self.provider or not self.launch_name:
            raise ValueError(
                f"model row {self.id!r} needs a non-empty id, provider and "
                "launch_name")
        if not self.tiers:
            raise ValueError(
                f"model {self.id!r} declares no tier; a row that serves no tier "
                "cannot be routed to and must not be listed")
        bad = [t for t in self.tiers if t not in TIERS]
        if bad:
            raise ValueError(f"model {self.id!r} declares unknown tier(s) {bad}")
        if self.context_window <= 0:
            raise ValueError(
                f"model {self.id!r} declares context_window="
                f"{self.context_window}; the refusal that protects citations "
                "needs a real number, not a placeholder")

    @property
    def coordinator_capable(self) -> bool:
        """May drive a governed workforce session."""
        return bool(self.supports_workforce and self.supports_tools)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "provider": self.provider, "label": self.label,
            "tiers": list(self.tiers), "context_window": self.context_window,
            "serves_claude_subagent": self.serves_claude_subagent,
            "supports_workforce": self.supports_workforce,
            "supports_tools": self.supports_tools,
            "coordinator_capable": self.coordinator_capable,
            "launch_name": self.launch_name, "notes": self.notes,
            "deprecated": self.deprecated,
        }


# The catalog. `launch_name` is the CLI alias, matching what TIER_MODEL already
# passes for the quick tier ("sonnet"); ids stay fully qualified so a persisted
# selection still names one model after an alias is repointed.
MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="claude-opus-5", provider=CLAUDE_CLI_PROVIDER, label="Claude Opus 5",
        tiers=(DEEP,), context_window=1_000_000,
        serves_claude_subagent=True, supports_workforce=True, supports_tools=True,
        launch_name="opus",
        notes="The desk's judgment tier: referee, challenger, moments analyst.",
    ),
    ModelSpec(
        id="claude-sonnet-5", provider=CLAUDE_CLI_PROVIDER, label="Claude Sonnet 5",
        tiers=(DEEP, QUICK), context_window=1_000_000,
        serves_claude_subagent=True, supports_workforce=True, supports_tools=True,
        launch_name="sonnet",
        notes="Mechanical roles and fast mode; also serviceable as the deep tier.",
    ),
    ModelSpec(
        id="claude-haiku-4-5", provider=CLAUDE_CLI_PROVIDER, label="Claude Haiku 4.5",
        tiers=(QUICK,), context_window=200_000,
        serves_claude_subagent=True, supports_workforce=False, supports_tools=True,
        launch_name="haiku",
        notes=(
            "Cheapest mechanical tier. Not offered as coordinator: walking a "
            "governed workflow's phases is judgment, and a stalled coordinator "
            "reads as a workflow that simply never advanced."),
    ),
    ModelSpec(
        id="claude-opus-4-1", provider=CLAUDE_CLI_PROVIDER, label="Claude Opus 4.1",
        tiers=(DEEP,), context_window=200_000,
        serves_claude_subagent=True, supports_workforce=True, supports_tools=True,
        launch_name="claude-opus-4-1",
        notes="Deprecated upstream (retires 2026-08-05); select claude-opus-5.",
        deprecated=True,
    ),
)


def list_models() -> tuple[ModelSpec, ...]:
    """Every catalog row, deprecated ones included.

    Hiding a deprecated row would make "not configured" and "I typo'd the id"
    look identical to the operator.
    """
    return tuple(MODELS)


def _catalog_ids() -> list[str]:
    return sorted(spec.id for spec in MODELS)


def get_model(model_id: str) -> ModelSpec:
    for spec in MODELS:
        if spec.id == model_id:
            return spec
    raise UnknownModel(
        f"no model {model_id!r} in the catalog; known ids: {_catalog_ids()}")


# --------------------------------------------------------------------------
# Slot requirements and the gate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotRequirement:
    """What a slot demands of whatever fills it."""

    slot: str
    tier: str | None            # None: the slot is a completion, not a role tier
    needs_claude_subagent: bool
    needs_workforce: bool
    needs_tools: bool
    requires_confirmation: bool  # the owner's setter demands confirm=true


SLOT_REQUIREMENTS: dict[str, SlotRequirement] = {
    # deep is the referee's tier and the referee's PASS is the execution gate,
    # so changing it is the one selection that asks the operator twice.
    "deep": SlotRequirement("deep", DEEP, True, True, True, True),
    "quick": SlotRequirement("quick", QUICK, True, False, True, False),
    # The reasoner forms a view from stored records; it is handed text and
    # returns text, with no tools and no tier.
    "reasoner": SlotRequirement("reasoner", None, False, False, False, False),
    # The chat agent's authority *is* its tools (claude.py:167-173).
    "chat": SlotRequirement("chat", None, False, False, True, False),
}


def _slot_requirement(slot: str) -> SlotRequirement:
    try:
        return SLOT_REQUIREMENTS[slot]
    except KeyError as exc:
        raise ValueError(
            f"unknown model slot {slot!r}; slots are {list(SLOTS)}") from exc


def _refusal(spec: ModelSpec, req: SlotRequirement, role: str,
             min_context: int) -> str:
    """Why ``spec`` may not fill ``req``, or "" when it may.

    Separate from ``check_eligible`` so ``eligible_models`` can filter without
    catching exceptions — and so the two can never disagree about the answer.
    """
    if req.slot in ("deep", "quick"):
        # These two become `model:` frontmatter in an agent file the Claude CLI
        # reads. A non-Claude id there is handed to a launcher that cannot run
        # it, and nothing downstream refuses.
        if spec.provider != CLAUDE_CLI_PROVIDER:
            return (
                f"{spec.id!r} is served by provider {spec.provider!r}; the "
                f"{req.slot!r} slot becomes Claude-subagent frontmatter in a "
                f"session agent file, so only {CLAUDE_CLI_PROVIDER!r} models "
                "may fill it")
        if not spec.serves_claude_subagent:
            return (
                f"{spec.id!r} does not declare serves_claude_subagent; the "
                f"{req.slot!r} slot is written as agent frontmatter and cannot "
                "hold a model that may not appear there")
    if req.tier is not None and req.tier not in spec.tiers:
        return (
            f"{spec.id!r} serves tiers {list(spec.tiers)}, not {req.tier!r}, "
            f"which the {req.slot!r} slot resolves through")
    if role and role in REQUIRED_DEEP_ROLES and req.slot != "deep":
        # REQUIRED_DEEP_ROLES is imported, never restated: one source of truth
        # for which roles may not be cheapened.
        return (
            f"role {role!r} may not be served from the {req.slot!r} slot; it is "
            "a required-deep role, so its model comes from the deep slot")
    if req.needs_workforce and not spec.coordinator_capable:
        return (
            f"{spec.id!r} does not declare supports_workforce; the {req.slot!r} "
            "slot may drive a governed session, and a coordinator that cannot "
            "dispatch the Agent tool registers a workflow that then sits at its "
            "first phase forever")
    if req.needs_tools and not spec.supports_tools:
        where = ("the read-only qlab MCP allowlist the chat agent grants "
                 "(portfolio.state, market.snapshot, research.runs, …)"
                 if req.slot == "chat" else
                 "the owner's MCP allowlist its session agent is granted")
        return (
            f"{spec.id!r} does not declare supports_tools; the {req.slot!r} "
            f"slot is handed {where}, and a model that cannot use them answers "
            "without live numbers while looking identical to one that did")
    if min_context and spec.context_window < min_context:
        return (
            f"{spec.id!r} has a context window of {spec.context_window} tokens, "
            f"below the {min_context} this call needs")
    if spec.deprecated:
        return f"{spec.id!r} is deprecated: {spec.notes}"
    return ""


def eligible_models(slot: str, *, role: str = "") -> tuple[ModelSpec, ...]:
    """Every catalog row that may fill ``slot`` (availability not consulted)."""
    req = _slot_requirement(slot)
    return tuple(spec for spec in MODELS
                 if not _refusal(spec, req, role, 0))


def check_eligible(model_id: str, *, slot: str, role: str = "",
                   min_context: int = 0) -> ModelSpec:
    """Return the spec if it may fill ``slot``, else raise.

    Authority first, then availability — the same order and the same
    return-or-raise contract as ``templates.check_startable``.
    """
    spec = get_model(model_id)
    req = _slot_requirement(slot)
    if not eligible_models(slot):
        # Never substitute. For "deep" this is the referee's tier, and a slot
        # nothing can fill is a catalog problem the operator has to see.
        # Asked without ``role`` on purpose: a role rule refusing *this* call
        # is not the catalog being empty, and reporting it as one would hide
        # which rule actually fired.
        raise ModelNotEligible(
            f"no model in the catalog can fill the {slot!r} slot; catalog is "
            f"{_catalog_ids()}")
    reason = _refusal(spec, req, role, min_context)
    if reason:
        raise ModelNotEligible(reason)
    return spec


# --------------------------------------------------------------------------
# Completions and the provider seam
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionRequest:
    model: ModelSpec
    system: str
    prompt: str
    max_output_tokens: int
    temperature: float | None = None
    timeout_s: float = 120.0


@dataclass(frozen=True)
class Completion:
    """One answer, with what the backend was willing to say about it.

    Everything after ``stop_reason`` defaults to None rather than to zero: a
    backend that does not report a token count has not reported zero tokens, and
    an unmeasured latency is not a fast one. ``text`` and ``stop_reason`` have
    no defaults — a completion that cannot say why it stopped cannot be judged
    silent, truncated, or refused.
    """

    text: str
    stop_reason: str                     # end_turn | max_tokens | refusal
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    raw_model: str | None = None         # None when the backend does not report it

    @property
    def is_silent(self) -> bool:
        """A finished turn that said nothing.

        Distinct from ``max_tokens``, which is a truncated answer — a different
        fact. The caller must check this: a silent success recorded as a
        successful invocation is worse than a recorded failure.
        """
        return self.stop_reason == "end_turn" and not self.text.strip()

    def to_dict(self) -> dict:
        return {
            "text": self.text, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "latency_ms": self.latency_ms,
            "stop_reason": self.stop_reason, "raw_model": self.raw_model,
            "is_silent": self.is_silent,
        }


class ModelProvider(Protocol):
    """The whole seam. ``register_provider`` checks these four by name.

    Not ``runtime_checkable``: a Protocol with data members cannot be used with
    ``isinstance`` anyway, and a decorator nothing calls is a seam that has
    never run.
    """

    name: str
    required_env: tuple[str, ...]

    def configured(self) -> tuple[bool, str]:
        """Whether this provider *could* run, and why — never raises."""

    def complete(self, request: CompletionRequest) -> Completion:
        """One single-turn completion, or ``ProviderError``."""


_PROVIDERS: dict[str, ModelProvider] = {}


def register_provider(p: ModelProvider) -> None:
    name = getattr(p, "name", "")
    if not name:
        raise ProviderError("a provider must carry a non-empty name")
    for attr in ("required_env", "configured", "complete"):
        if not hasattr(p, attr):
            raise ProviderError(
                f"provider {name!r} does not implement {attr!r}; the seam is "
                "name/required_env/configured/complete")
    if name in _PROVIDERS:
        # A silently replaced provider changes what actually served an
        # invocation already written to model_invocations.
        raise ProviderAlreadyRegistered(
            f"provider {name!r} is already registered by "
            f"{type(_PROVIDERS[name]).__name__}")
    _PROVIDERS[name] = p


def providers() -> Mapping[str, ModelProvider]:
    return MappingProxyType(_PROVIDERS)


def get_provider(name: str) -> ModelProvider:
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise ProviderError(
            f"no provider named {name!r} is registered; registered: "
            f"{sorted(_PROVIDERS)}") from exc


class AnthropicCliProvider:
    """The one built-in: the local ``claude`` CLI, run tool-free.

    Reuses ``build_claude_argv(..., governed=False, chat=False)`` — the existing
    third branch, which emits ``mcpServers {}`` with ``--tools ""`` — so there
    is exactly one place in the repo where tool authority is granted and this is
    not it. No new argv shape and no new authority surface.

    Two limits of this backend, stated rather than papered over: the CLI exposes
    no output-token cap, so ``max_output_tokens`` bounds nothing here (it is the
    HTTP providers' knob); and it does not report which model actually served
    the turn, so ``Completion.raw_model`` is None unless the stream says
    otherwise.
    """

    name = CLAUDE_CLI_PROVIDER
    required_env: tuple[str, ...] = ()

    def configured(self) -> tuple[bool, str]:
        try:
            from qlab.tui.claude import resolve_claude_executable
            executable = resolve_claude_executable()
        except Exception as exc:  # a probe that explodes is still an answer
            return (False, f"the `claude` launcher could not be probed: {exc!r}")
        if not executable:
            return (False, "no `claude` launcher on PATH; install Claude Code "
                           "or put it on PATH for this process")
        return (True, f"`claude` is configured at {executable}. Configured is "
                      "not reachable: nothing has been run, so this does not "
                      "promise the CLI can reach Anthropic or that the account "
                      "may use the selected model")

    def complete(self, request: CompletionRequest) -> Completion:
        ok, reason = self.configured()
        if not ok:
            raise ProviderError(reason)
        text = _compose(request)
        fits, why = fits_context(text, request.model)
        if not fits:
            # Refuse before the call, never truncate: a citation dropped by
            # truncation is a claim that silently lost its evidence.
            raise ProviderError(why)

        from qlab.tui.claude import (
            build_claude_argv,
            parse_stream_line,
            resolve_claude_executable,
        )
        argv = build_claude_argv(
            text, governed=False, chat=False,
            # Both are inert in the tool-free branch: it emits `mcpServers {}`,
            # so no runtime URL is handed out and the offline flag reaches no
            # server environment.
            runtime_url="", offline=True)
        argv = _with_model_flag(argv, request.model.launch_name)
        executable = resolve_claude_executable()
        argv[0] = executable
        if os.name == "nt" and str(executable).lower().endswith((".cmd", ".bat")):
            # Same launcher handling as ClaudeSession.start (claude.py:1012).
            argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", *argv]

        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=request.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"the claude CLI did not answer within {request.timeout_s}s"
            ) from exc
        except OSError as exc:
            raise ProviderError(f"the claude CLI could not be run: {exc!r}") from exc
        latency_ms = (time.monotonic() - started) * 1000.0

        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()[:400]
            raise ProviderError(
                f"the claude CLI exited {proc.returncode}: {detail or 'no stderr'}")

        parts: list[str] = []
        result_payload: dict = {}
        for line in (proc.stdout or "").splitlines():
            for event in parse_stream_line(line):
                if event.kind == "text":
                    # Only the completed blocks. `--include-partial-messages`
                    # also emits text_delta for the same content, and counting
                    # both would double the answer.
                    parts.append(event.text)
                elif event.kind == "error":
                    raise ProviderError(f"the claude CLI failed: {event.text}")
                elif event.kind == "result":
                    result_payload = dict(event.raw or {})

        usage = result_payload.get("usage") or {}
        return Completion(
            text="".join(parts),
            input_tokens=_opt_int(usage.get("input_tokens")),
            output_tokens=_opt_int(usage.get("output_tokens")),
            latency_ms=latency_ms,
            # The CLI reports failure as is_error (raised above) and success as
            # a completed result; it has no truncation signal to map.
            stop_reason="end_turn",
            raw_model=result_payload.get("model") or None,
        )


def _compose(request: CompletionRequest) -> str:
    """System text folded into the prompt.

    ``build_claude_argv`` exposes no system-prompt flag in the tool-free branch,
    and adding one would be a new argv shape this module is not allowed to
    invent.
    """
    system = (request.system or "").strip()
    prompt = request.prompt or ""
    return f"{system}\n\n---\n\n{prompt}" if system else prompt


def _with_model_flag(argv: list[str], launch_name: str) -> list[str]:
    """Splice ``--model`` in ahead of the ``--`` prompt terminator.

    ``build_claude_argv`` has no model parameter yet (the owner integration adds
    one). Without the flag the CLI picks its own model and the recorded
    invocation names one that never ran — so refuse loudly if the builder's
    shape changed rather than silently appending after the terminator.
    """
    try:
        cut = argv.index("--")
    except ValueError as exc:
        raise ProviderError(
            "build_claude_argv no longer ends with a `--` prompt terminator; "
            "the model flag has nowhere safe to go") from exc
    return [*argv[:cut], "--model", launch_name, *argv[cut:]]


def _opt_int(value) -> int | None:
    """None stays None: an unreported token count is not zero tokens."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _load_builtin_providers() -> None:
    """Register the providers that ship with qlab, by explicit import.

    Never entry-point scanning: a silently missing plugin is the opposite of
    fail-loud. Idempotent so a re-import is not an error, while a *second*
    provider claiming a taken name still raises.
    """
    if CLAUDE_CLI_PROVIDER not in _PROVIDERS:
        register_provider(AnthropicCliProvider())


_load_builtin_providers()


# --------------------------------------------------------------------------
# The operator's selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSelection:
    deep: str
    quick: str
    reasoner: str
    chat: str
    source: Literal["persisted", "default"]

    def slot(self, name: str) -> str:
        if name not in SLOTS:
            raise ValueError(f"unknown model slot {name!r}; slots are {list(SLOTS)}")
        return getattr(self, name)

    def to_dict(self) -> dict:
        return {**{name: self.slot(name) for name in SLOTS}, "source": self.source}


# What the desk runs before anyone chooses. Not a claim that the operator picked
# it — `source` carries that distinction.
DEFAULT_SELECTION = ModelSelection(
    deep="claude-opus-5",
    quick="claude-sonnet-5",
    reasoner="claude-sonnet-5",
    chat="claude-sonnet-5",
    source="default",
)


def _selection_path() -> Path:
    return state_path(SELECTION_FILE)


def load_selection() -> ModelSelection | None:
    """The persisted selection, or None when nobody has chosen yet.

    Deliberately diverges from ``load_desk_mode`` (core/desk_mode.py), which
    returns None on an unreadable file: there the operator is about to be asked
    anyway, so falling back to the prompt loses nothing. Here a silently
    substituted model produces answers attributed to a model that never ran, and
    the deep slot is the referee's tier — so a corrupt file, or one naming a
    model the catalog no longer has, raises ``SelectionUnreadable``.
    """
    path = _selection_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SelectionUnreadable(
            f"the model selection at {path} could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise SelectionUnreadable(
            f"the model selection at {path} is {type(raw).__name__}, not an object")
    chosen: dict[str, str] = {}
    for name in SLOTS:
        value = raw.get(name)
        if not isinstance(value, str) or not value:
            raise SelectionUnreadable(
                f"the model selection at {path} has no usable {name!r} slot "
                f"(found {value!r}); it names {sorted(raw)}")
        chosen[name] = value
    for name, model_id in chosen.items():
        try:
            get_model(model_id)
        except UnknownModel as exc:
            raise SelectionUnreadable(
                f"the persisted {name!r} slot names {model_id!r}, which is not "
                f"in the catalog {_catalog_ids()}; refusing to re-point a slot "
                "the operator chose") from exc
    return ModelSelection(**chosen, source="persisted")


def save_selection(sel: ModelSelection) -> None:
    """Validate every slot, then write atomically.

    A single rejection writes nothing. The write is a temp file in the same
    directory plus ``os.replace``: a partial write can leave a
    truncated-but-parseable file, which is exactly the corrupt case
    ``load_selection`` has to raise on.
    """
    for name in SLOTS:
        check_eligible(sel.slot(name), slot=name)
    path = _selection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: sel.slot(name) for name in SLOTS}
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(state_root()),
        prefix=f"{SELECTION_FILE}.", suffix=".tmp", delete=False)
    tmp_name = handle.name
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # A leftover temp file in the state root is a second parseable
        # selection nobody chose; take it with us.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def resolve_selection() -> ModelSelection:
    """The selection in force, validated against the catalog as it is now."""
    sel = load_selection() or DEFAULT_SELECTION
    for name in SLOTS:
        if not eligible_models(name):
            raise ModelNotEligible(
                f"no model in the catalog can fill the {name!r} slot; catalog "
                f"is {_catalog_ids()}")
        check_eligible(sel.slot(name), slot=name)
    return sel


def degraded_slots(sel: ModelSelection) -> tuple[tuple[str, str], ...]:
    """(slot, reason) for every slot that cannot serve right now.

    Never raises: this is the status line's input, and a status line that throws
    tells the operator less than one that says why.
    """
    out: list[tuple[str, str]] = []
    for name in SLOTS:
        try:
            spec = check_eligible(sel.slot(name), slot=name)
        except (ModelError, ValueError) as exc:
            out.append((name, str(exc)))
            continue
        try:
            provider = get_provider(spec.provider)
        except ProviderError as exc:
            out.append((name, str(exc)))
            continue
        ok, reason = provider.configured()
        if not ok:
            out.append((name, reason))
    return tuple(out)


# --------------------------------------------------------------------------
# Tier maps and the context budget
# --------------------------------------------------------------------------


def tier_model_map(sel: ModelSelection, *, fast: bool) -> dict[str, str]:
    """What an ordinary role resolves through.

    Fast mode drops the deep tier onto the quick model; ``NONE`` stays
    ``inherit`` because an unregistered role follows the session rather than
    naming a model of its own.
    """
    deep = get_model(sel.deep).launch_name
    quick = get_model(sel.quick).launch_name
    return {DEEP: quick if fast else deep, QUICK: quick, NONE: "inherit"}


def exempt_tier_model_map(sel: ModelSelection) -> dict[str, str]:
    """What REQUIRED_DEEP_ROLES resolve through — unaffected by fast mode.

    Today the exemption returns ``TIER_MODEL[DEEP] == "inherit"``
    (model_routing.py:111), which names no model at all in the audit row and
    ignores the operator's choice entirely. This map is the second one that fix
    consumes.
    """
    return {
        DEEP: get_model(sel.deep).launch_name,
        QUICK: get_model(sel.quick).launch_name,
        NONE: "inherit",
    }


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: ``ceil(len/3)``.

    Over-estimates English prose (~4 chars/token) by roughly 25%, and
    under-estimates only for majority-CJK or base64 text. Deliberately not a
    real tokenizer: the suite runs fully offline (invariant 2), so a downloaded
    or networked tokenizer is unavailable — and an over-estimate plus
    CONTEXT_SAFETY is the safe direction, because an under-estimate lets the
    provider truncate silently.
    """
    return math.ceil(len(text or "") / 3.0)


def fits_context(text: str, spec: ModelSpec) -> tuple[bool, str]:
    """Whether ``text`` fits ``spec`` with the safety margin, and why not."""
    estimate = estimate_tokens(text)
    budget = int(spec.context_window * CONTEXT_SAFETY)
    if estimate <= budget:
        return (True, f"{estimate} estimated tokens against a budget of {budget} "
                      f"({spec.context_window} window x {CONTEXT_SAFETY})")
    return (False,
            f"{estimate} estimated tokens exceeds the usable budget of {budget} "
            f"for {spec.id!r} (context_window {spec.context_window}, safety "
            f"{CONTEXT_SAFETY}); refusing rather than truncating, because a "
            "citation dropped by truncation is a claim that lost its evidence")


def with_slot(sel: ModelSelection, slot: str, model_id: str) -> ModelSelection:
    """One slot changed, the rest carried forward.

    The owner's setter receives ``{slot, model_id}`` and must not reach for
    ``getattr`` on a name that arrived over HTTP; this validates the slot and
    leaves eligibility to ``save_selection``.
    """
    if slot not in SLOTS:
        raise ValueError(f"unknown model slot {slot!r}; slots are {list(SLOTS)}")
    return replace(sel, **{slot: model_id})
