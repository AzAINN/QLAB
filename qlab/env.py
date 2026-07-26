"""Load ``.env`` so configured credentials actually reach the runtime.

``.env.example`` documented every integration variable, but nothing read the
``.env`` a user copied it to — so filling in Alpaca keys appeared to do nothing
and the desk silently stayed on synthetic data. This closes that gap with a
small stdlib parser rather than a dependency.

Two rules:

* **The real environment always wins.** A variable already exported is never
  overwritten by the file, so an explicit ``ALPACA_API_KEY=... qlab tui`` and CI
  secrets both behave predictably.
* **Never log a value.** Only names are ever reported, so a secret cannot reach
  a log, an event, or a prompt through this path.
"""

from __future__ import annotations

import os
from pathlib import Path

from qlab.paths import workspace_root

_LOADED: set[str] = set()


def parse_env(text: str) -> dict[str, str]:
    """Parse dotenv text. Supports ``export``, quotes, comments, blanks."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        # Strip one matching pair of surrounding quotes, then trailing comment
        # only when the value was NOT quoted (a quoted # is part of the value).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif "#" in value:
            value = value.split("#", 1)[0].strip()
        values[name] = value
    return values


def load_dotenv(path: str | Path | None = None, *,
                override: bool = False) -> list[str]:
    """Load ``.env`` into ``os.environ``; return the NAMES applied.

    Values are never returned or logged. Missing file is not an error — the
    whole local demo runs with no configuration at all.
    """
    target = Path(path) if path is not None else (workspace_root() / ".env")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    applied: list[str] = []
    for name, value in parse_env(text).items():
        if not value:
            continue
        if not override and os.environ.get(name):
            continue  # an explicitly exported variable outranks the file
        os.environ[name] = value
        applied.append(name)
    return applied


def load_once(path: str | Path | None = None) -> list[str]:
    """Load ``.env`` at most once per process, for entry points to call."""
    key = str(path or "default")
    if key in _LOADED:
        return []
    _LOADED.add(key)
    return load_dotenv(path)


def credential_status() -> dict:
    """Which integrations are configured. Reports presence, never values."""
    def present(*names: str) -> bool:
        return all(os.environ.get(n, "").strip() for n in names)

    alpaca = present("ALPACA_API_KEY", "ALPACA_API_SECRET")
    return {
        "alpaca_credentials": alpaca,
        "market_data_provider": os.environ.get("QLAB_DATA_PROVIDER") or "yfinance",
        "news_provider": os.environ.get("QLAB_NEWS_PROVIDER") or "synthetic",
        "alpaca_feed": os.environ.get("ALPACA_FEED") or "iex",
        "news_ready": (
            os.environ.get("QLAB_NEWS_PROVIDER") == "alpaca" and alpaca
            or os.environ.get("QLAB_NEWS_PROVIDER") == "rss"),
    }
