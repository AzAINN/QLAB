# Alpaca OAuth Login and Explicit Desk Mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator run qlab against real Alpaca paper data using the Alpaca CLI's browser OAuth login (no API keys), choose data source and book explicitly at startup, and replace the cluttered bottom status banner with a compact bottom-right live indicator.

**Architecture:** A new no-network resolver reads the Alpaca CLI's own profile (`~/.config/alpaca/profiles/<name>.yaml`) and yields either OAuth-token or key/secret credentials, env vars taking precedence. A new `DeskMode` value object makes `data` (synthetic|live) and `book` (simulated|alpaca) an explicit, persisted operator decision instead of something inferred from whether credentials happen to exist. A startup modal collects it, CLI flags override it, and the status line shrinks to a single mode chip.

**Tech Stack:** Python 3.11+, alpaca-py 0.43.5 (`oauth_token` supported on both `TradingClient` and `StockHistoricalDataClient`), PyYAML, Textual, DuckDB, pytest.

**Spec:** `planning-docs/2026-07-25-alpaca-oauth-desk-mode-design.md`

## Global Constraints

- **Paper only.** `paper=True` stays hard-coded in `AlpacaPaperBroker`; never parameterised. A profile that declares itself live is refused.
- **Never log or echo a secret.** No token or secret key in logs, `repr`, `str`, exception messages, or the payloads any surface renders.
- **Fail loud (invariant 4).** Every refusal names the remedy (usually `alpaca profile login`). Never silently downgrade live → synthetic or alpaca-book → simulated-book; showing synthetic numbers to an operator who believes they are real is the exact failure this forbids.
- **One DuckDB writer (invariant 1).** All registry access stays inside the owner runtime.
- **Tests never open `.lab/registry.duckdb` (invariant 2)** — `Registry(":memory:")`; no test touches the network. The opt-in `tests/test_alpaca_integration.py` remains the sole exception and stays gated behind `QLAB_ALPACA_INTEGRATION=1`.
- **Resolve files through `qlab/paths.py`** (`state_path`), never `Path(__file__).parents[...]` (invariant 6).
- **Restart the owner after changing code it serves (invariant 8).**
- **Never weaken an existing assertion.** Where this plan relocates a fact from one widget to another, the assertion moves with it and keeps its exact expected string.
- Commit messages: imperative, conventional prefix + scope, **no AI-attribution trailers**.
- Comments state constraints the code cannot show; match existing density.
- Run tests with `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest` from the repo root (the PATH `python` is 3.14 without pytest).

## File Map

| File | Change |
|---|---|
| `qlab/trader/alpaca_auth.py` | **Create** — credential resolver |
| `tests/test_alpaca_auth.py` | **Create** |
| `qlab/core/desk_mode.py` | **Create** — `DeskMode` + persistence |
| `tests/test_desk_mode.py` | **Create** |
| `qlab/trader/broker.py` | `AlpacaPaperBroker` uses the resolver; `get_broker` gains explicit `book` |
| `tests/test_trader.py` | Broker selection + OAuth client-kind tests |
| `qlab/ui/server.py` | Mode-driven data policy and book; `desk_mode` in snapshot; `GET /api/desk_mode` |
| `tests/test_ui.py` | Owner-side mode tests |
| `qlab/tui/desk_mode_screen.py` | **Create** — startup modal |
| `qlab/tui/theme.py` | Modal CSS + bottom-row CSS |
| `qlab/tui/app.py` | Modal wiring; slim status line + mode chip; displaced facts into Settings |
| `qlab/autopilot/cli.py` | `--live` / `--alpaca-book` on `tui` and `ui` |
| `tests/test_tui.py` | Modal, mode chip, relocated Settings assertions |
| `tests/test_alpaca_integration.py` | Opt-in OAuth case |

---

### Task 1: Alpaca credential resolver

**Files:**
- Create: `qlab/trader/alpaca_auth.py`
- Test: `tests/test_alpaca_auth.py`

**Interfaces:**
- Produces: `AlpacaCredentials` (frozen dataclass: `kind: Literal["api_key","oauth"]`, `api_key: str | None`, `secret_key: str | None`, `oauth_token: str | None`, `profile_name: str | None`, `source: str`); `resolve_alpaca_credentials() -> AlpacaCredentials | None`; `describe_credentials(creds: AlpacaCredentials | None) -> str`; exception `AlpacaAuthError(RuntimeError)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alpaca_auth.py
"""Resolving Alpaca credentials from env or the Alpaca CLI's own profiles."""

from __future__ import annotations

import pytest

from qlab.trader.alpaca_auth import (
    AlpacaAuthError, describe_credentials, resolve_alpaca_credentials)


def _write_profile(tmp_path, name="paper", body=None):
    """Lay out an Alpaca CLI config dir the way the real CLI does."""
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        f"default_profile: {name}\noutput: json\n", encoding="utf-8")
    (tmp_path / "profiles" / f"{name}.yaml").write_text(
        body if body is not None else
        "api_key: ''\nsecret_key: ''\n"
        "access_token: tok-abcdefghijklmnopqrstuvwxyz012345\n"
        "scopes: account:write trading data\n",
        encoding="utf-8")
    return tmp_path


def test_env_credentials_win_over_a_cli_profile(tmp_path, monkeypatch):
    _write_profile(tmp_path)
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_API_KEY", "PKENVKEY")
    monkeypatch.setenv("ALPACA_API_SECRET", "envsecret")
    creds = resolve_alpaca_credentials()
    assert creds.kind == "api_key"
    assert (creds.api_key, creds.secret_key) == ("PKENVKEY", "envsecret")
    assert creds.source == "env"
    assert creds.profile_name is None


def test_oauth_profile_is_resolved_when_env_is_empty(tmp_path, monkeypatch):
    _write_profile(tmp_path)
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    assert creds.kind == "oauth"
    assert creds.oauth_token == "tok-abcdefghijklmnopqrstuvwxyz012345"
    assert creds.profile_name == "paper"
    assert creds.api_key is None and creds.secret_key is None


def test_api_key_profile_is_resolved_as_api_key(tmp_path, monkeypatch):
    _write_profile(tmp_path, body="api_key: PKFILE\nsecret_key: filesecret\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    assert creds.kind == "api_key"
    assert (creds.api_key, creds.secret_key) == ("PKFILE", "filesecret")


def test_alpaca_profile_env_overrides_the_default_profile(tmp_path, monkeypatch):
    _write_profile(tmp_path)  # default_profile: paper
    (tmp_path / "profiles" / "other.yaml").write_text(
        "access_token: tok-other\n", encoding="utf-8")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_PROFILE", "other")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    assert (creds.profile_name, creds.oauth_token) == ("other", "tok-other")


def test_no_config_and_no_env_resolves_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path / "absent"))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert resolve_alpaca_credentials() is None


def test_partial_env_credentials_refuse_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_API_KEY", "PKONLY")
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="ALPACA_API_SECRET"):
        resolve_alpaca_credentials()


def test_malformed_profile_raises_naming_the_path(tmp_path, monkeypatch):
    _write_profile(tmp_path, body="{{{ not yaml\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="paper.yaml"):
        resolve_alpaca_credentials()


def test_a_live_profile_is_refused(tmp_path, monkeypatch):
    _write_profile(tmp_path, body="access_token: tok-live\nlive: true\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="paper"):
        resolve_alpaca_credentials()


def test_secrets_never_appear_in_repr_or_description(tmp_path, monkeypatch):
    _write_profile(tmp_path)
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    secret = "tok-abcdefghijklmnopqrstuvwxyz012345"
    assert secret not in repr(creds)
    assert secret not in str(creds)
    assert secret not in describe_credentials(creds)
    assert "paper" in describe_credentials(creds)


def test_describe_credentials_handles_absence():
    assert "alpaca profile login" in describe_credentials(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_alpaca_auth.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'qlab.trader.alpaca_auth'`

- [ ] **Step 3: Write the module**

```python
# qlab/trader/alpaca_auth.py
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
            raise AlpacaAuthError(f"{config} is not valid YAML: {exc}") from exc
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
        raise AlpacaAuthError(f"{path} is not valid YAML: {exc}") from exc
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_alpaca_auth.py -q`
Expected: 10 passed

- [ ] **Step 5: Run the full suite, then commit**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest -q`

```bash
git add qlab/trader/alpaca_auth.py tests/test_alpaca_auth.py
git commit -m "feat(trader): resolve Alpaca credentials from env or the CLI profile"
```

---

### Task 2: DeskMode value object and persistence

**Files:**
- Create: `qlab/core/desk_mode.py`
- Test: `tests/test_desk_mode.py`

**Interfaces:**
- Produces: `DeskMode` (frozen dataclass, fields `data: Literal["synthetic","live"]`, `book: Literal["simulated","alpaca"]`; properties `offline: bool` (True when `data == "synthetic"`), `label: str`); `DEFAULT_DESK_MODE: DeskMode`; `load_desk_mode() -> DeskMode | None`; `save_desk_mode(mode: DeskMode) -> None`.
- Consumes: `qlab.paths.state_path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_desk_mode.py
"""The desk's data/book mode is an explicit, persisted operator decision."""

from __future__ import annotations

import pytest

from qlab.core.desk_mode import (
    DEFAULT_DESK_MODE, DeskMode, load_desk_mode, save_desk_mode)


def test_default_is_the_safe_offline_desk():
    assert DEFAULT_DESK_MODE == DeskMode("synthetic", "simulated")
    assert DEFAULT_DESK_MODE.offline is True


def test_live_data_can_carry_either_book():
    assert DeskMode("live", "simulated").offline is False
    assert DeskMode("live", "alpaca").offline is False


def test_synthetic_data_cannot_use_the_alpaca_book():
    # Unreachable via the UI's progressive disclosure; still refused in code so
    # a flag combination or a hand-edited state file cannot produce it.
    with pytest.raises(ValueError, match="synthetic"):
        DeskMode("synthetic", "alpaca")


def test_labels_distinguish_all_three_states():
    labels = {
        DeskMode("synthetic", "simulated").label,
        DeskMode("live", "simulated").label,
        DeskMode("live", "alpaca").label,
    }
    assert len(labels) == 3
    assert DeskMode("live", "alpaca").label != DeskMode("live", "simulated").label


def test_round_trips_through_the_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    assert load_desk_mode() is None          # nothing chosen yet
    save_desk_mode(DeskMode("live", "alpaca"))
    assert load_desk_mode() == DeskMode("live", "alpaca")


def test_unreadable_or_unknown_state_falls_back_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path))
    (tmp_path / "desk_mode.json").write_text("{ not json", encoding="utf-8")
    assert load_desk_mode() is None
    (tmp_path / "desk_mode.json").write_text(
        '{"data": "wormhole", "book": "simulated"}', encoding="utf-8")
    assert load_desk_mode() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_desk_mode.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'qlab.core.desk_mode'`

Note: confirm `QLAB_STATE_DIR` is what `qlab/paths.py:state_root()` honours before relying on it in the test; if the env var name differs, use the real one and keep the test's intent.

- [ ] **Step 3: Write the module**

```python
# qlab/core/desk_mode.py
"""What the desk is pointed at: which data, and whose book.

Kept explicit rather than inferred from whether credentials happen to exist —
otherwise discovering an Alpaca login on disk would silently route an operator
who only wanted to look at synthetic data to their real paper account.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

from qlab.paths import state_path

_STATE_FILE = "desk_mode.json"
_DATA = ("synthetic", "live")
_BOOK = ("simulated", "alpaca")


@dataclass(frozen=True)
class DeskMode:
    data: Literal["synthetic", "live"]
    book: Literal["simulated", "alpaca"]

    def __post_init__(self) -> None:
        if self.data not in _DATA:
            raise ValueError(f"unknown data source {self.data!r}")
        if self.book not in _BOOK:
            raise ValueError(f"unknown book {self.book!r}")
        if self.data == "synthetic" and self.book != "simulated":
            raise ValueError(
                "synthetic data cannot trade the Alpaca book; a synthetic desk "
                "always uses the simulated book")

    @property
    def offline(self) -> bool:
        return self.data == "synthetic"

    @property
    def label(self) -> str:
        if self.data == "synthetic":
            return "SYNTHETIC"
        return "LIVE · ALPACA BOOK" if self.book == "alpaca" else "LIVE · SIM BOOK"


DEFAULT_DESK_MODE = DeskMode("synthetic", "simulated")


def load_desk_mode() -> DeskMode | None:
    """The persisted choice, or None when absent//unusable.

    An unreadable or unrecognised file is treated as "not chosen yet" rather
    than an error: the operator is about to be asked anyway, and refusing to
    start the desk over a scratch file would be worse than re-asking.
    """
    path = state_path(_STATE_FILE)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DeskMode(str(raw["data"]), str(raw["book"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_desk_mode(mode: DeskMode) -> None:
    path = state_path(_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(mode), indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_desk_mode.py -q`
Expected: 6 passed

- [ ] **Step 5: Run the full suite, then commit**

```bash
git add qlab/core/desk_mode.py tests/test_desk_mode.py
git commit -m "feat(core): explicit desk mode for data source and book"
```

---

### Task 3: Broker accepts OAuth and an explicit book

**Files:**
- Modify: `qlab/trader/broker.py` (`AlpacaPaperBroker.__init__`; `get_broker`)
- Test: `tests/test_trader.py`

**Interfaces:**
- Consumes: `resolve_alpaca_credentials`, `AlpacaCredentials`, `AlpacaAuthError` (Task 1).
- Produces: `AlpacaPaperBroker(registry, credentials: AlpacaCredentials | None = None)` — resolves when not supplied; `get_broker(registry, *, offline=False, starting_cash=10000.0, seed=7, universe=None, book: Literal["simulated","alpaca"] | None = None)`. `book=None` preserves today's credential-inferred behaviour so existing callers keep working; `book="simulated"` forces the simulator even with valid credentials; `book="alpaca"` requires them and refuses loudly otherwise.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trader.py`:

```python
def test_simulated_book_wins_over_discoverable_credentials(monkeypatch):
    """The regression this design exists to prevent.

    A discoverable `alpaca profile login` token must not silently route an
    operator who chose the simulated book to their real paper account.
    """
    from qlab.trader import broker as broker_mod
    from qlab.trader.alpaca_auth import AlpacaCredentials

    monkeypatch.setattr(
        broker_mod, "resolve_alpaca_credentials",
        lambda: AlpacaCredentials("oauth", None, None, "tok", "paper", "/x"))
    got = broker_mod.get_broker(Registry(":memory:"), offline=True,
                                book="simulated")
    assert got.name == "simulated_paper"


def test_alpaca_book_without_credentials_refuses_with_the_remedy(monkeypatch):
    from qlab.trader import broker as broker_mod

    monkeypatch.setattr(broker_mod, "resolve_alpaca_credentials", lambda: None)
    with pytest.raises(RuntimeError, match="alpaca profile login"):
        broker_mod.get_broker(Registry(":memory:"), book="alpaca")


def test_oauth_credentials_build_the_clients_with_a_token(monkeypatch):
    """OAuth must reach BOTH the trading and the market-data client."""
    from qlab.trader import broker as broker_mod
    from qlab.trader.alpaca_auth import AlpacaCredentials

    seen = {}

    class FakeTrading:
        def __init__(self, *args, **kwargs):
            seen["trading"] = kwargs

    class FakeData:
        def __init__(self, *args, **kwargs):
            seen["data"] = kwargs

    monkeypatch.setitem(
        __import__("sys").modules, "alpaca.trading.client",
        type("M", (), {"TradingClient": FakeTrading}))
    monkeypatch.setitem(
        __import__("sys").modules, "alpaca.data.historical",
        type("M", (), {"StockHistoricalDataClient": FakeData}))

    creds = AlpacaCredentials("oauth", None, None, "tok-123", "paper", "/x")
    broker_mod.AlpacaPaperBroker(Registry(":memory:"), credentials=creds)
    assert seen["trading"]["oauth_token"] == "tok-123"
    assert seen["trading"]["paper"] is True      # never configurable
    assert seen["data"]["oauth_token"] == "tok-123"
    assert "api_key" not in seen["trading"] or seen["trading"]["api_key"] is None
```

(Match `tests/test_trader.py`'s existing import style for `Registry` and `pytest`; both are already imported there.)

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_trader.py -q -k "simulated_book_wins or alpaca_book_without or oauth_credentials_build"`
Expected: FAIL — `get_broker()` got an unexpected keyword argument `book`

- [ ] **Step 3: Implement**

In `qlab/trader/broker.py`, add the import near the other qlab imports:

```python
from qlab.trader.alpaca_auth import (
    AlpacaAuthError, AlpacaCredentials, resolve_alpaca_credentials)
```

Replace `AlpacaPaperBroker.__init__`'s credential handling:

```python
    def __init__(self, registry: Registry,
                 credentials: AlpacaCredentials | None = None):
        self.reg = registry
        creds = credentials or resolve_alpaca_credentials()
        if creds is None:
            raise AlpacaAuthError(
                "no Alpaca credentials found — run `alpaca profile login` or "
                "set ALPACA_API_KEY / ALPACA_API_SECRET")
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as exc:
            raise RuntimeError(f"alpaca-py not installed ({exc}); pip install qlab[trader]")
        # paper=True is NOT configurable here — this class only ever paper-trades.
        # An OAuth token from `alpaca profile login` is paper-only at the source,
        # which is why that flow is the preferred credential.
        if creds.kind == "oauth":
            self.trading = TradingClient(oauth_token=creds.oauth_token, paper=True)
            self.data = StockHistoricalDataClient(oauth_token=creds.oauth_token)
        else:
            self.trading = TradingClient(creds.api_key, creds.secret_key, paper=True)
            self.data = StockHistoricalDataClient(creds.api_key, creds.secret_key)
        self._asset_cache: dict[str, dict] = {}
```

Replace `get_broker`:

```python
def get_broker(registry: Registry, *, offline: bool = False,
               starting_cash: float = 10000.0, seed: int = 7,
               universe: list[str] | None = None,
               book: str | None = None) -> Broker:
    """Return the broker for the chosen ``book``.

    ``book`` is the operator's explicit decision (``"simulated"`` or
    ``"alpaca"``). ``None`` keeps the historical behaviour — Alpaca when
    credentials exist — so callers that have no mode yet are unaffected.
    Credential presence must never *by itself* select the real paper account.
    """
    if book == "simulated":
        return SimulatedPaperBroker(
            registry, default_price_provider(offline=offline, seed=seed),
            starting_cash, universe=universe)

    creds = resolve_alpaca_credentials()   # raises on partial env credentials
    if book == "alpaca" and creds is None:
        raise RuntimeError(
            "the Alpaca book was selected but no credentials were found — run "
            "`alpaca profile login`, or choose the simulated book")
    if book == "alpaca" or (book is None and creds is not None):
        # A failure here must be loud, never a silent downgrade to simulation
        # (which would book against the wrong venue without telling anyone).
        try:
            return AlpacaPaperBroker(registry, credentials=creds)
        except Exception as exc:
            raise RuntimeError(
                "the Alpaca paper broker could not be initialized "
                f"({exc}); refusing to silently fall back to the simulator"
            ) from exc
    return SimulatedPaperBroker(
        registry, default_price_provider(offline=offline, seed=seed),
        starting_cash, universe=universe)
```

- [ ] **Step 4: Run tests**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_trader.py -q`
Expected: all pass. Then the full suite — the previous partial-credential message moved into `alpaca_auth`, so if a test asserted the old wording, update it to assert the new message's `ALPACA_API_SECRET` / `ALPACA_API_KEY` mention (same guarantee, new home — do not drop the assertion).

- [ ] **Step 5: Commit**

```bash
git add qlab/trader/broker.py tests/test_trader.py
git commit -m "feat(trader): OAuth credentials and an explicit book selection"
```

---

### Task 4: Owner runtime honours the desk mode

**Files:**
- Modify: `qlab/ui/server.py` (`UISession.__init__`; `portfolio`; `current_book`; `tui_snapshot`; `handle_api` GET routes)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `DeskMode`, `DEFAULT_DESK_MODE`, `load_desk_mode` (Task 2); `get_broker(..., book=…)` (Task 3).
- Produces: `UISession(desk_mode: DeskMode | None = None)` keyword; `UISession.desk_mode` attribute; `UISession.set_desk_mode(mode: DeskMode) -> DeskMode`; `GET /api/desk_mode` returning `{"data","book","label","offline","credentials","credentials_ok"}`; `POST /api/desk_mode` accepting `{"data","book"}`; `tui_snapshot` gains `"desk_mode"`.

- [ ] **Step 1: Write the failing test**

```python
def test_desk_mode_defaults_to_synthetic_and_is_reported(session):
    status, payload = handle_api(session, "GET", "/api/desk_mode", {}, {})
    assert status == 200
    assert (payload["data"], payload["book"]) == ("synthetic", "simulated")
    assert payload["label"] == "SYNTHETIC"
    assert "credentials" in payload          # description string, never a secret


def test_setting_the_desk_mode_switches_the_book(session, monkeypatch):
    from qlab.trader import broker as broker_mod
    from qlab.trader.alpaca_auth import AlpacaCredentials
    monkeypatch.setattr(
        broker_mod, "resolve_alpaca_credentials",
        lambda: AlpacaCredentials("oauth", None, None, "tok", "paper", "/x"))

    status, payload = handle_api(
        session, "POST", "/api/desk_mode", {},
        {"data": "live", "book": "simulated"})
    assert status == 200 and payload["label"] == "LIVE · SIM BOOK"
    # The simulated book is honoured even though a credential is discoverable.
    assert session.portfolio(offline=False)["broker"] == "simulated_paper"


def test_an_impossible_desk_mode_is_refused(session):
    status, payload = handle_api(
        session, "POST", "/api/desk_mode", {},
        {"data": "synthetic", "book": "alpaca"})
    assert status == 400
    assert "synthetic" in payload["error"]


def test_tui_snapshot_carries_the_desk_mode(session):
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["desk_mode"]["label"] == "SYNTHETIC"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_ui.py -q -k desk_mode`
Expected: FAIL — no route for `GET /api/desk_mode` (404, so the status assertion fails)

- [ ] **Step 3: Implement**

`UISession.__init__` — after `self.offline_default = offline_default`:

```python
        # The operator's explicit choice; the persisted value is authoritative
        # when the caller passes none, and synthetic is the safe default.
        self.desk_mode = desk_mode or load_desk_mode() or DEFAULT_DESK_MODE
```

Add the keyword `desk_mode: DeskMode | None = None` to the signature and
`from qlab.core.desk_mode import DEFAULT_DESK_MODE, DeskMode, load_desk_mode, save_desk_mode` to the imports.

Add methods next to `current_book`:

```python
    def set_desk_mode(self, mode: DeskMode) -> DeskMode:
        self.desk_mode = mode
        save_desk_mode(mode)
        return mode

    def desk_mode_payload(self) -> dict:
        from qlab.trader.alpaca_auth import (
            AlpacaAuthError, describe_credentials, resolve_alpaca_credentials)

        try:
            creds = resolve_alpaca_credentials()
            description, ok = describe_credentials(creds), creds is not None
        except AlpacaAuthError as exc:
            # A broken credential source is not the same as absence: say so.
            description, ok = str(exc), False
        return {
            "data": self.desk_mode.data,
            "book": self.desk_mode.book,
            "label": self.desk_mode.label,
            "offline": self.desk_mode.offline,
            "credentials": description,
            "credentials_ok": ok,
        }
```

Pass the book at every `get_broker` call the session makes — `portfolio`,
`current_book`, `backfill_equity_history`, and the reconcile/plan paths — by
adding `book=self.desk_mode.book`. Locate them with
`grep -n "get_broker(" qlab/ui/server.py`; every call inside `UISession` gets
the argument.

Routes in `handle_api`:

```python
    if method == "GET" and path == "/api/desk_mode":
        return 200, session.desk_mode_payload()
```

```python
    if method == "POST" and path == "/api/desk_mode":
        try:
            mode = DeskMode(str(body.get("data")), str(body.get("book")))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        session.set_desk_mode(mode)
        return 200, session.desk_mode_payload()
```

`tui_snapshot`: add `"desk_mode": self.desk_mode_payload(),`.

Also add the two routes to the module docstring's route table.

- [ ] **Step 4: Run tests**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_ui.py -q` then the full suite.

- [ ] **Step 5: Commit**

```bash
git add qlab/ui/server.py tests/test_ui.py
git commit -m "feat(ui): owner runtime honours an explicit desk mode"
```

---

### Task 5: Startup modal

**Files:**
- Create: `qlab/tui/desk_mode_screen.py`
- Modify: `qlab/tui/theme.py` (modal CSS), `qlab/tui/app.py` (show on mount)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `DeskMode` (Task 2); `GET /api/desk_mode` (Task 4).
- Produces: `DeskModeScreen(ModalScreen[DeskMode])` with `__init__(self, credentials: str, credentials_ok: bool)`; widget ids `#desk-dialog`, `#desk-data-synthetic`, `#desk-data-live`, `#desk-book-row`, `#desk-book-simulated`, `#desk-book-alpaca`, `#desk-credentials`, `#desk-confirm`; `DESK_MODAL_CSS` in `theme.py`.

- [ ] **Step 1: Write the failing test**

```python
def test_desk_modal_hides_the_book_row_until_live_is_chosen():
    import asyncio
    from textual.app import App, ComposeResult
    from qlab.core.desk_mode import DeskMode
    from qlab.tui.desk_mode_screen import DeskModeScreen

    result = {}

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            return iter(())

        async def on_mount(self) -> None:
            def done(mode: DeskMode | None) -> None:
                result["mode"] = mode
            self.push_screen(
                DeskModeScreen(credentials="Alpaca browser login from profile "
                                          "'paper'", credentials_ok=True),
                done)

    async def run():
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            screen = app.screen
            # Synthetic is the default selection, so the book row is hidden.
            assert screen.query_one("#desk-book-row").styles.display == "none"
            await pilot.click("#desk-data-live")
            await pilot.pause(0.1)
            assert screen.query_one("#desk-book-row").styles.display != "none"
            await pilot.click("#desk-book-alpaca")
            await pilot.click("#desk-confirm")
            await pilot.pause(0.2)

    asyncio.run(run())
    assert result["mode"] == DeskMode("live", "alpaca")


def test_desk_modal_without_credentials_cannot_return_a_live_mode():
    import asyncio
    from textual.app import App, ComposeResult
    from qlab.core.desk_mode import DeskMode
    from qlab.tui.desk_mode_screen import DeskModeScreen

    result = {}

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            return iter(())

        async def on_mount(self) -> None:
            self.push_screen(
                DeskModeScreen(
                    credentials="no Alpaca credentials found — run "
                                "`alpaca profile login`",
                    credentials_ok=False),
                lambda mode: result.__setitem__("mode", mode))

    async def run():
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            screen = app.screen
            copy = str(screen.query_one("#desk-credentials").content)
            assert "alpaca profile login" in copy
            await pilot.click("#desk-data-live")     # must not select
            await pilot.click("#desk-confirm")
            await pilot.pause(0.2)

    asyncio.run(run())
    assert result["mode"] == DeskMode("synthetic", "simulated")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_tui.py -q -k desk_modal`
Expected: FAIL with `ModuleNotFoundError: No module named 'qlab.tui.desk_mode_screen'`

- [ ] **Step 3: Write the screen**

```python
# qlab/tui/desk_mode_screen.py
"""Startup choice: which data, and whose book.

Two steps in one screen. The book question only appears once LIVE is chosen, so
the nonsensical combination (synthetic data against the real paper account) is
unreachable by construction rather than rejected by a validation message.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from qlab.core.desk_mode import DeskMode
from qlab.tui.theme import DESK_MODAL_CSS


class DeskModeScreen(ModalScreen[DeskMode]):
    BINDINGS = [Binding("escape", "cancel", "Synthetic", show=False)]
    CSS = DESK_MODAL_CSS

    def __init__(self, credentials: str, credentials_ok: bool) -> None:
        super().__init__()
        self.credentials = credentials
        self.credentials_ok = credentials_ok
        self._data = "synthetic"
        self._book = "simulated"

    def compose(self) -> ComposeResult:
        with Vertical(id="desk-dialog"):
            yield Static("DATA SOURCE", id="desk-dialog-title")
            with Horizontal(id="desk-data-row"):
                yield Button("SYNTHETIC", id="desk-data-synthetic")
                yield Button("LIVE", id="desk-data-live",
                             disabled=not self.credentials_ok)
            yield Static(self.credentials, id="desk-credentials")
            with Vertical(id="desk-book-row"):
                yield Static("WHICH BOOK", id="desk-book-title")
                with Horizontal(id="desk-book-buttons"):
                    yield Button("SIMULATED", id="desk-book-simulated")
                    yield Button("ALPACA PAPER", id="desk-book-alpaca")
                yield Static(
                    "simulated uses real prices but never sends an order to "
                    "Alpaca. either way, executing a plan still needs your "
                    "explicit confirmation.",
                    id="desk-book-copy")
            with Horizontal(id="desk-actions"):
                yield Button("Start", id="desk-confirm", variant="warning")

    def on_mount(self) -> None:
        self._sync()

    def _sync(self) -> None:
        row = self.query_one("#desk-book-row")
        row.styles.display = "block" if self._data == "live" else "none"

    def action_cancel(self) -> None:
        self.dismiss(DeskMode("synthetic", "simulated"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        pressed = event.button.id or ""
        if pressed == "desk-data-synthetic":
            self._data, self._book = "synthetic", "simulated"
        elif pressed == "desk-data-live" and self.credentials_ok:
            self._data = "live"
        elif pressed == "desk-book-simulated":
            self._book = "simulated"
        elif pressed == "desk-book-alpaca":
            self._book = "alpaca"
        elif pressed == "desk-confirm":
            self.dismiss(DeskMode(self._data, self._book))
            return
        self._sync()
```

Add to `qlab/tui/theme.py`, following `PAPER_MODAL_CSS_TEMPLATE`'s shape (a
`Template` substituted with `TOKENS`, so no literal hex appears):

```python
DESK_MODAL_CSS_TEMPLATE = Template("""
DeskModeScreen { align: center middle; }
#desk-dialog {
    width: 62; height: auto; padding: 1 2;
    background: $BG_PANEL; border: round $BORDER_HI;
}
#desk-dialog-title { color: $AMBER; text-style: bold; }
#desk-data-row, #desk-book-buttons { height: auto; padding: 1 0; }
#desk-credentials { color: $MUTED; }
#desk-book-title { color: $AMBER; text-style: bold; padding-top: 1; }
#desk-book-copy { color: $MUTED; padding-top: 1; }
#desk-actions { height: auto; padding-top: 1; align-horizontal: right; }
""")
DESK_MODAL_CSS = DESK_MODAL_CSS_TEMPLATE.substitute(TOKENS)
```

(Check the token names actually present in `TOKENS` and use those; the existing
`PAPER_MODAL_CSS_TEMPLATE` is the reference for which are available.)

- [ ] **Step 4: Wire it into the app**

In `QlabTui.__init__`, accept `desk_mode: DeskMode | None = None` and store
`self.desk_mode = desk_mode`; store `self._desk_mode_prompted = False`.

In `on_mount`, after the first refresh is started, prompt only when no mode was
supplied by a flag:

```python
        if self.desk_mode is None and not self._desk_mode_prompted:
            self._desk_mode_prompted = True
            self._start_desk_mode_prompt()
```

```python
    def _start_desk_mode_prompt(self) -> None:
        """Fetch credential status off-thread, then ask. The probe must never
        block the UI, so it follows the same worker shape as the atlas fetch."""
        def run() -> None:
            try:
                payload = self.client.get("/api/desk_mode")
            except Exception as exc:
                payload = {"credentials": f"owner unreachable: {exc!r}",
                           "credentials_ok": False}
            self.call_from_thread(self._ask_desk_mode, payload)

        threading.Thread(target=run, daemon=True).start()

    def _ask_desk_mode(self, payload: dict) -> None:
        def chosen(mode: DeskMode | None) -> None:
            if mode is None:
                return
            self.desk_mode = mode
            self.offline = mode.offline
            self._post_desk_mode(mode)
            self._render_status()
            self._start_refresh()

        self.push_screen(
            DeskModeScreen(
                credentials=str(payload.get("credentials", "")),
                credentials_ok=bool(payload.get("credentials_ok")),
            ),
            chosen,
        )

    def _post_desk_mode(self, mode: DeskMode) -> None:
        def run() -> None:
            try:
                self.client.post("/api/desk_mode",
                                 {"data": mode.data, "book": mode.book})
            except Exception as exc:
                self.call_from_thread(
                    self._write_local_event, "desk_mode.error",
                    {"error": repr(exc)})

        threading.Thread(target=run, daemon=True).start()
```

Import `DeskMode` and `DeskModeScreen` at the top of `app.py`.

- [ ] **Step 5: Run tests**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_tui.py -q`
Expected: all pass. Existing tests construct `QlabTui(client, refresh_interval=0)` without a mode; pass `desk_mode=DeskMode("synthetic","simulated")` in the `StubClient`-based helper if the modal would otherwise appear and steal focus from those pilots — that keeps every existing assertion intact rather than adapting them to a modal they were not written for.

- [ ] **Step 6: Commit**

```bash
git add qlab/tui/desk_mode_screen.py qlab/tui/theme.py qlab/tui/app.py tests/test_tui.py
git commit -m "feat(tui): startup modal for data source and book"
```

---

### Task 6: CLI flags

**Files:**
- Modify: `qlab/autopilot/cli.py` (`tui` and `ui` parsers; `_cmd_tui`, `_cmd_ui`)
- Test: `tests/test_desk_cli.py`

**Interfaces:**
- Consumes: `DeskMode`, `save_desk_mode` (Task 2).
- Produces: `--live` and `--alpaca-book` on both `qlab tui` and `qlab ui`; helper `desk_mode_from_args(args) -> DeskMode | None` in `qlab/autopilot/cli.py` returning `None` when neither flag was given.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_desk_cli.py`:

```python
def test_desk_mode_from_args_maps_the_flags():
    import argparse

    from qlab.autopilot.cli import desk_mode_from_args
    from qlab.core.desk_mode import DeskMode

    def ns(**kw):
        return argparse.Namespace(live=False, alpaca_book=False, online=False, **kw)

    assert desk_mode_from_args(ns()) is None          # no flag: ask or persist
    assert desk_mode_from_args(argparse.Namespace(
        live=True, alpaca_book=False, online=False)) == DeskMode("live", "simulated")
    # --alpaca-book implies live; reaching the real book always takes the extra word.
    assert desk_mode_from_args(argparse.Namespace(
        live=False, alpaca_book=True, online=False)) == DeskMode("live", "alpaca")
    # legacy --online keeps working as "live data, simulated book"
    assert desk_mode_from_args(argparse.Namespace(
        live=False, alpaca_book=False, online=True)) == DeskMode("live", "simulated")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_desk_cli.py -q -k desk_mode_from_args`
Expected: FAIL with `ImportError: cannot import name 'desk_mode_from_args'`

- [ ] **Step 3: Implement**

```python
def desk_mode_from_args(args) -> DeskMode | None:
    """The mode an explicit flag selected, or None to ask / use the persisted one.

    ``--alpaca-book`` implies live: the real paper account is never reachable
    without naming it, and bare ``--live`` keeps the simulated book.
    """
    if getattr(args, "alpaca_book", False):
        return DeskMode("live", "alpaca")
    if getattr(args, "live", False) or getattr(args, "online", False):
        return DeskMode("live", "simulated")
    return None
```

Add to both parsers:

```python
    tui.add_argument("--live", action="store_true",
                     help="use live Alpaca market data (simulated book)")
    tui.add_argument("--alpaca-book", action="store_true",
                     help="trade your Alpaca paper book (implies --live)")
```

(and the same two lines on the `ui` parser).

In `_cmd_tui`, resolve the mode once and thread it through: persist it with
`save_desk_mode(mode)` when a flag supplied one, pass `desk_mode=mode` to
`QlabTui(...)`, and keep the owner subprocess consistent by appending `--live`
(and `--alpaca-book` when set) to `server_argv` instead of only `--online`.
In `_cmd_ui`, pass the resolved mode into `serve(...)` so `UISession` receives
`desk_mode=`; when no flag is given, pass `None` and let the session load the
persisted value.

- [ ] **Step 4: Run tests**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_desk_cli.py tests/test_ui.py -q` then the full suite.

- [ ] **Step 5: Commit**

```bash
git add qlab/autopilot/cli.py tests/test_desk_cli.py
git commit -m "feat(cli): --live and --alpaca-book select the desk mode"
```

---

### Task 7: Slim the bottom row to a mode chip, relocate the displaced facts

The bottom currently carries a full-width latest-event line plus an eight-token
status string (`PAPER · {source}/DAILY · {feed} · {mcp} · {claude} · {data} ·
{autopilot} · {bob}{approvals}`). Replace it with the command input and two
compact right-aligned chips: connection, and the desk mode. Nothing is deleted —
every displaced fact moves into the Settings view, which already renders system
cards, and the existing assertions move with it unchanged.

**Files:**
- Modify: `qlab/tui/app.py` (`compose` bottom block; `_render_status`; `_render_settings`), `qlab/tui/theme.py` (`APP_CSS` for `#mode-chip`, drop `#event-strip` rules)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: snapshot key `desk_mode` (Task 4).
- Produces: `#mode-chip` Static in the command row; `#settings-system` Static in the Settings view carrying the MCP / Claude / data / autopilot / feed / bob / approvals facts.

- [ ] **Step 1: Write the failing test**

```python
def test_bottom_row_shows_only_connection_and_mode():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0,
                      desk_mode=DeskMode("live", "alpaca"))
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            assert str(app.query_one("#mode-chip").content).strip() == (
                "LIVE · ALPACA BOOK")
            # the verbose banner and the full-width event line are gone
            assert not app.query("#system-status")
            assert not app.query("#event-strip")

    asyncio.run(run())


def test_settings_keeps_every_fact_the_banner_used_to_show():
    from qlab.tui.app import QlabTui

    class FactsClient(StubClient):
        def get(self, path, **params):
            snap = super().get(path, **params)
            if path == "/api/tui":
                snap["system"]["autopilot"] = {
                    "last_run_at": "2026-07-24T16:30:00+00:00",
                    "triggers_fired": 2,
                }
            return snap

    async def run():
        app = QlabTui(FactsClient(), refresh_interval=0,
                      desk_mode=DeskMode("synthetic", "simulated"))
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("8")            # Settings
            await pilot.pause(0.3)
            card = str(app.query_one("#settings-system").content)
            assert "AUTO 07-24 16:30·2" in card
            assert "CLAUDE READY" in card
            assert "DATA synthetic·0d" in card

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_tui.py -q -k "bottom_row or settings_keeps_every_fact"`
Expected: FAIL — `#mode-chip` does not exist

- [ ] **Step 3: Implement**

`compose` — the bottom block becomes:

```python
        yield RichLog(id="timeline", wrap=True, markup=False, max_lines=500)
        with Horizontal(id="command-row"):
            yield Input(placeholder=": command or Ctrl-P", id="command")
            yield Static("CONNECTING", id="conn-chip", markup=True)
            yield Static("SYNTHETIC", id="mode-chip", markup=True)
```

Rename `_render_status` to `_render_mode_chip` and reduce it to the mode alone,
tone-coded so a live book is unmistakable:

```python
    def _render_mode_chip(self) -> None:
        mode = (self.snapshot.get("desk_mode") or {}) if self.snapshot else {}
        label = str(mode.get("label") or (
            self.desk_mode.label if self.desk_mode else "SYNTHETIC"))
        # A real book must never read like the demo: colour carries that,
        # because this chip is the only always-visible answer to "whose money".
        tone = DOWN if mode.get("book") == "alpaca" else (
            AMBER if mode.get("data") == "live" else MUTED)
        self.query_one("#mode-chip", Static).update(f"[{tone}]{label}[/]")
        self.query_one("#chat-exit", Button).label = (
            "■ stop" if self.claude.running else "exit")
        self._sync_chat_input()
```

Move the displaced tokens into `_render_settings` as a new `#settings-system`
card, reusing the exact token strings the old `_render_status` built (`mcp`,
`claude`, `data_token`, `autopilot_token`, `feed_token`, `bob_token`,
`approval_token`) so the relocated assertions keep their expected text. Add the
`Static(id="settings-system", classes="settings-card", markup=True)` to the
Settings view's `compose`.

Update every `_render_status()` call site to `_render_mode_chip()`
(`grep -n "_render_status" qlab/tui/app.py`), and delete the `#event-strip`
widget plus its writes and its CSS rules. Its content is not lost: the timeline
(`~`) and the workforce console already carry the event stream.

Retarget the four existing `#system-status` assertions (`tests/test_tui.py`
around lines 605, 666, 1051, 1424, 3370, 3389) to `#settings-system`, keeping
each expected string byte-identical. The one `#event-strip` assertion (around
line 1421, `"verdict PASS"`) moves to the timeline widget the same way — assert
the same text where it now appears, do not drop it.

- [ ] **Step 4: Run tests**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest tests/test_tui.py -q` then the full suite.

- [ ] **Step 5: Commit**

```bash
git add qlab/tui/app.py qlab/tui/theme.py tests/test_tui.py
git commit -m "feat(tui): compact mode chip replaces the bottom status banner"
```

---

### Task 8: Opt-in OAuth integration case, docs, and full verification

**Files:**
- Modify: `tests/test_alpaca_integration.py`, `.env.example`, `README.md`
- Test: the whole suite

- [ ] **Step 1: Add the opt-in OAuth case**

```python
def test_oauth_profile_builds_a_paper_broker():
    """The `alpaca profile login` path, end to end. Opt-in like its neighbours."""
    from qlab.state.registry import Registry
    from qlab.trader.alpaca_auth import resolve_alpaca_credentials
    from qlab.trader.broker import AlpacaPaperBroker

    creds = resolve_alpaca_credentials()
    if creds is None or creds.kind != "oauth":
        pytest.skip("no OAuth profile; run `alpaca profile login`")
    registry = Registry(":memory:")
    try:
        broker = AlpacaPaperBroker(registry, credentials=creds)
        state = broker.portfolio_state([_PROBE_SYMBOL])
        assert state["equity"] > 0
    finally:
        registry.close()
```

- [ ] **Step 2: Update the docs**

In `.env.example`, under the Alpaca section, state that the keys are optional
because `alpaca profile login` is the preferred path, and that **nothing in the
codebase loads `.env`** — these must be exported into the environment. In
`README.md`, add a short "real market data" note: `alpaca profile login`, then
`qlab tui --live` (simulated book) or `qlab tui --alpaca-book` (your paper
book), and that the mode is also selectable from the startup modal.

- [ ] **Step 3: Full suite**

Run: `/Users/azainmac/codebases/quant-trading-agent/.venv/bin/python -m pytest -q`
Expected: all pass; skips stay at their current count plus the new opt-in case.

- [ ] **Step 4: Operator smoke (manual)**

1. Restart the owner (invariant 8). Use a free port if one is already running:
   `python -m qlab.autopilot.cli tui --port 8799`.
2. First launch shows the modal; it names your profile and account. Choose
   SYNTHETIC → chip reads `SYNTHETIC`, bottom row has no banner.
3. Relaunch → no modal (the choice persisted).
4. `qlab tui --alpaca-book` → chip reads `LIVE · ALPACA BOOK` in the alert tone;
   Book view shows your real paper equity.
5. `alpaca profile logout`, then `qlab tui --alpaca-book` → refuses, naming
   `alpaca profile login`; it does **not** quietly start synthetic.
6. Switch back to the simulated book and confirm the Book view's equity series
   scopes to that book and discloses the excluded marks rather than splicing.

- [ ] **Step 5: Commit**

```bash
git add tests/test_alpaca_integration.py .env.example README.md
git commit -m "docs(alpaca): document the browser-login path and desk modes"
```

## Self-review notes

- Spec coverage: resolver (T1), DeskMode + persistence (T2), OAuth clients and
  explicit book (T3), owner wiring and payload (T4), modal with off-thread probe
  (T5), flags and precedence (T6), status-strip slim-down — the operator's added
  request — (T7), integration case and docs (T8).
- The `synthetic` + `alpaca` combination is rejected in three places by
  construction: the modal never offers it, `DeskMode.__post_init__` raises, and
  `POST /api/desk_mode` returns 400.
- Nothing weakens an assertion: T3, T5 and T7 each relocate an existing
  assertion to the widget or message that now carries the same fact.
- Line-number anchors drift as tasks land — locate by symbol name.
