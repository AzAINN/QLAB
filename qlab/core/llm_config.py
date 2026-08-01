"""Which model answers for which surface: chosen, never inferred.

``desk_mode``'s philosophy applied to models. The desk asks questions in two
places — Atlas's own reasoning, and the governed five-role workforce — and they
are configured apart because they are asked different questions: a reasoner
composes a read, the workforce walks a gated pipeline. Keeping the two choices
separate is what lets a local model be tried on one without touching the other.

Explicit rather than inferred, for the same reason the desk mode is: discovering
that an Ollama daemon happens to be running must never re-route the workforce to
it. The persisted choice is the operator speaking; the environment only seeds a
desk that has never chosen; the default is exactly today's behaviour.

This module holds no opinion about whether a chosen backend *works*. That is a
live question with a network answer, and the owner runtime is the single place
that asks it (``UISession.llm_backends_catalog``) — a validator here would be a
second one, and the two would disagree the first time a daemon stopped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

from qlab.paths import state_path

_STATE_FILE = "llm_config.json"

# The two places a model answers. Not a free-form string: a surface the desk
# does not have is a typo, and a typo that silently created a third surface
# would configure a model nothing ever reads.
SURFACES = ("reasoner", "workforce")

_ENV_VARS = {
    "reasoner": "QLAB_LLM_REASONER",
    "workforce": "QLAB_LLM_WORKFORCE",
}


@dataclass(frozen=True)
class SurfaceModel:
    """One surface's provider and the model it asks for.

    ``backend`` is a name from the backend registry and ``model`` is whatever
    that backend serves; neither is validated against a live daemon here (see
    the module docstring).
    """

    backend: str
    model: str

    def __post_init__(self) -> None:
        if not self.backend.strip():
            raise ValueError("a surface needs a backend name")
        if not self.model.strip():
            raise ValueError(f"the {self.backend} backend needs a model name")

    def to_dict(self) -> dict:
        return {"backend": self.backend, "model": self.model}


@dataclass(frozen=True)
class LlmConfig:
    """The whole desk's model routing: one choice per surface, plus the switch."""

    reasoner: SurfaceModel
    workforce: SurfaceModel
    # The reasoner is a surface the desk did not have before this. Off by
    # default so an upgrade changes nothing until an operator turns it on.
    reasoner_enabled: bool = False

    def with_surface(self, surface: str, choice: SurfaceModel,
                     enabled: bool | None = None) -> "LlmConfig":
        """This config with one surface repointed. ``enabled=None`` leaves it."""
        if surface not in SURFACES:
            raise ValueError(
                f"unknown model surface {surface!r}; the desk has "
                f"{' and '.join(SURFACES)}")
        updated = replace(self, **{surface: choice})
        if enabled is None:
            return updated
        return replace(updated, reasoner_enabled=bool(enabled))

    def to_dict(self) -> dict:
        return {
            "reasoner": self.reasoner.to_dict(),
            "workforce": self.workforce.to_dict(),
            "reasoner_enabled": self.reasoner_enabled,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "LlmConfig":
        """Rebuild from a persisted mapping. Raises on anything else."""
        return cls(
            reasoner=SurfaceModel(str(raw["reasoner"]["backend"]),
                                  str(raw["reasoner"]["model"])),
            workforce=SurfaceModel(str(raw["workforce"]["backend"]),
                                   str(raw["workforce"]["model"])),
            reasoner_enabled=bool(raw.get("reasoner_enabled", False)),
        )


# Today's behaviour, written down: both surfaces on the Claude CLI, following
# the operator's own session model ("inherit"), and the reasoner switched off.
DEFAULT_LLM_CONFIG = LlmConfig(
    reasoner=SurfaceModel("claude", "inherit"),
    workforce=SurfaceModel("claude", "inherit"),
    reasoner_enabled=False,
)


def load_llm_config() -> LlmConfig | None:
    """The persisted choice, or None when absent or unusable.

    An unreadable or unrecognised file is "not chosen yet" rather than an error
    — ``desk_mode``'s rule. The operator is about to be offered the picker
    anyway, and refusing to start the owner over a scratch file would be worse
    than falling back to the default and re-asking.
    """
    path = state_path(_STATE_FILE)
    if not path.exists():
        return None
    try:
        return LlmConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_llm_config(config: LlmConfig) -> None:
    path = state_path(_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")


def _known_backend_names() -> tuple[str, ...]:
    """The registered backend NAMES. A static registry, never a probe.

    Imported lazily so ``qlab.core`` carries no import-time dependency on the
    operator layer. Reading the name set here does not make this a second
    validator: whether a backend can *serve* is a live question with a network
    answer, and the owner remains the only thing that asks it.
    """
    from qlab.operator.llm_backends import BACKENDS

    return tuple(sorted(BACKENDS))


def _parse_surface_spec(raw: str, var: str) -> SurfaceModel:
    """``backend:model`` — split on the FIRST colon only.

    An Ollama model name carries its own colon (``granite3.3:8b``), so
    everything after the first separator belongs to the model. Splitting on the
    last would ask for a backend called ``ollama:granite3.3``.

    The consequence is that ``QLAB_LLM_REASONER=granite3.3:8b`` — a model name
    with the backend forgotten — parses cleanly as backend ``granite3.3``. The
    form cannot disambiguate that, so the backend name is checked against the
    registry here, where the operator can still see which half they omitted.
    """
    backend, separator, model = raw.strip().partition(":")
    if not separator or not backend.strip() or not model.strip():
        raise ValueError(
            f"{var}={raw!r} is not backend:model — "
            f"e.g. {var}=ollama:granite3.3:8b")
    known = _known_backend_names()
    if backend.strip() not in known:
        raise ValueError(
            f"{var}={raw!r} names no known backend {backend.strip()!r}; "
            f"the form is backend:model with backend one of "
            f"{', '.join(known)} — e.g. {var}=ollama:granite3.3:8b")
    return SurfaceModel(backend.strip(), model.strip())


def env_llm_config() -> LlmConfig | None:
    """The environment's seed, or None when it says nothing.

    A variable that is set but malformed is refused loudly rather than ignored:
    an operator who exported it meant it, and silently running on a different
    model than the one they named is the failure this whole module exists to
    make impossible.

    Naming a reasoner model does NOT switch the reasoner on. Reading an
    on-switch out of a model name would be the same inference ``desk_mode``
    refuses when it declines to read a live desk out of a credential file.
    """
    config = DEFAULT_LLM_CONFIG
    seeded = False
    for surface, var in _ENV_VARS.items():
        raw = os.environ.get(var)
        if raw is None:
            continue
        config = config.with_surface(surface, _parse_surface_spec(raw, var))
        seeded = True
    return config if seeded else None


def startup_llm_config() -> LlmConfig:
    """The config to start with: the persisted choice, then the env, then default.

    Mirrors ``startup_desk_mode``'s precedence with one difference — there is no
    "ask the operator" state here, because unlike a book, an unchosen model
    routing has a correct answer (today's behaviour) that no one needs to
    confirm.
    """
    return load_llm_config() or env_llm_config() or DEFAULT_LLM_CONFIG
