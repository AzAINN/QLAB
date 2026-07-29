"""Resolve Alpaca paper credentials from the environment or the Alpaca CLI.

The Alpaca CLI's ``alpaca profile login`` is a browser OAuth flow that is
paper-only by construction (live requires its separate ``--api-key --live``
path), so consuming its profile is safer than asking an operator to paste keys.
This module only reads files — no network, no SDK import — so it stays cheap
enough to call from a UI probe and testable without credentials.

Secrets never leave this module in printable form: ``AlpacaCredentials`` has a
redacting ``repr`` and ``describe_credentials`` is the only operator-facing
renderer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml


class AlpacaAuthError(RuntimeError):
    """A credential source exists but is unusable. Never raised for absence."""


@dataclass(frozen=True)
class AlpacaCredentials:
    kind: Literal["api_key", "oauth"]
    api_key: str | None
    secret_key: str | None
    oauth_token: str | None
    profile_name: str | None
    source: str

    def __repr__(self) -> str:  # never leak the secret into a traceback
        return (f"AlpacaCredentials(kind={self.kind!r}, "
                f"profile_name={self.profile_name!r}, source={self.source!r})")

    __str__ = __repr__


def _yaml_parse_error(path: Path, exc: yaml.YAMLError) -> AlpacaAuthError:
    """Report where a parse failed, never what was on the line.

    PyYAML quotes the offending source line in its own message, and in a
    profile that line can be the ``access_token``. Callers must raise the
    result with ``from None`` so the chained PyYAML message — carrying the
    same snippet — cannot reach a traceback either.
    """
    mark = getattr(exc, "problem_mark", None)
    where = ""
    if mark is not None:
        where = f" at line {mark.line + 1} column {mark.column + 1}"
    return AlpacaAuthError(f"{path} is not valid YAML{where}")


def _load_yaml_mapping(path: Path) -> dict:
    """Parse a YAML file that must hold a mapping.

    Both the config and the profile need the same three outcomes, and neither
    may reach ``.get`` on a hand-edited file that parses to a list or a scalar:
    a broken source is an ``AlpacaAuthError``, never a raw ``AttributeError``.
    An empty file is an empty mapping.

    Reading is guarded as tightly as parsing. ``UnicodeDecodeError`` carries the
    entire decoded buffer in its ``args``, so one stray byte in a profile puts
    the ``access_token`` into any repr of it — and both callers of this module
    render an unexpected exception (a 500 body on ``/api/tui``, a startup
    traceback in the terminal). The read error is therefore re-rendered from the
    path alone, and ``OSError`` joins it so an unreadable profile is the same
    loud refusal instead of a raw ``PermissionError``.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # `from None`: the chained PyYAML error carries the source snippet.
        raise _yaml_parse_error(path, exc) from None
    except (OSError, UnicodeDecodeError):
        # Neither the exception nor its args may be interpolated or chained:
        # both routes to the operator would print the buffer they hold.
        raise AlpacaAuthError(f"{path} could not be read") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise AlpacaAuthError(f"{path} does not contain a YAML mapping")
    return loaded


def _config_dir() -> Path:
    override = os.environ.get("ALPACA_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "alpaca"


def _active_profile_name(config_dir: Path) -> str:
    explicit = os.environ.get("ALPACA_PROFILE", "").strip()
    if explicit:
        return explicit
    config = config_dir / "config.yaml"
    if config.exists():
        loaded = _load_yaml_mapping(config)
        name = str(loaded.get("default_profile") or "").strip()
        if name:
            return name
    return "paper"


def refuse_partial_env_credentials() -> None:
    """Refuse a half-set ``ALPACA_API_KEY``/``ALPACA_API_SECRET`` pair.

    Partial credentials signal intent with a broken setup: refuse rather than
    fall through to a profile — or a book — the operator did not ask for.
    Exposed on its own because a caller that needs no credential at all (the
    explicitly simulated book) must still make the misconfiguration loud
    instead of stepping over it.
    """
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET", "").strip()
    if bool(key) != bool(secret):
        missing = "ALPACA_API_SECRET" if key else "ALPACA_API_KEY"
        raise AlpacaAuthError(
            f"{missing} is not set; set both ALPACA_API_KEY and "
            "ALPACA_API_SECRET, or neither to use your `alpaca profile login` "
            "session instead")


def resolve_alpaca_credentials() -> AlpacaCredentials | None:
    """Env credentials, else the active CLI profile, else None."""
    refuse_partial_env_credentials()
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET", "").strip()
    if key and secret:
        return AlpacaCredentials("api_key", key, secret, None, None, "env")

    config_dir = _config_dir()
    name = _active_profile_name(config_dir)
    path = config_dir / "profiles" / f"{name}.yaml"
    if not path.exists():
        return None
    profile = _load_yaml_mapping(path)
    # Any truthy value refuses: `live: 1` and `live: true` both declare live, and
    # ambiguity in a paper-only desk has to resolve toward refusing.
    if bool(profile.get("live")):
        raise AlpacaAuthError(
            f"profile {name!r} at {path} is a live-trading profile; qlab is "
            "paper-only. Use a paper profile (`alpaca profile login`).")

    file_key = str(profile.get("api_key") or "").strip()
    file_secret = str(profile.get("secret_key") or "").strip()
    if file_key and file_secret:
        return AlpacaCredentials(
            "api_key", file_key, file_secret, None, name, str(path))
    token = str(profile.get("access_token") or "").strip()
    if token:
        return AlpacaCredentials("oauth", None, None, token, name, str(path))
    return None


def describe_credentials(creds: AlpacaCredentials | None) -> str:
    """One operator-facing line. Never includes the secret."""
    if creds is None:
        return ("no Alpaca credentials found — run `alpaca profile login` "
                "to authorize a paper account in your browser")
    if creds.source == "env":
        return "Alpaca API key from the environment"
    kind = "browser login" if creds.kind == "oauth" else "API key"
    return f"Alpaca {kind} from profile {creds.profile_name!r}"
