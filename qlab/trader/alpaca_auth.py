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
        try:
            loaded = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise _yaml_parse_error(config, exc) from None
        name = str(loaded.get("default_profile") or "").strip()
        if name:
            return name
    return "paper"


def resolve_alpaca_credentials() -> AlpacaCredentials | None:
    """Env credentials, else the active CLI profile, else None."""
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET", "").strip()
    if bool(key) != bool(secret):
        # Partial credentials signal intent with a broken setup: refuse rather
        # than fall through to a profile the operator did not ask for.
        missing = "ALPACA_API_SECRET" if key else "ALPACA_API_KEY"
        raise AlpacaAuthError(
            f"{missing} is not set; set both ALPACA_API_KEY and "
            "ALPACA_API_SECRET, or neither to use your `alpaca profile login` "
            "session instead")
    if key and secret:
        return AlpacaCredentials("api_key", key, secret, None, None, "env")

    config_dir = _config_dir()
    name = _active_profile_name(config_dir)
    path = config_dir / "profiles" / f"{name}.yaml"
    if not path.exists():
        return None
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise _yaml_parse_error(path, exc) from None
    if not isinstance(profile, dict):
        raise AlpacaAuthError(f"{path} does not contain a profile mapping")
    if str(profile.get("live", "")).strip().lower() == "true":
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
