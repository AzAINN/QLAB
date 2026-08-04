"""Runtime paths that work from both a source checkout and an installed wheel.

The repository keeps editable configuration at its root. Wheels install the
same files under ``share/qlab`` (see ``pyproject.toml``). Runtime state is
deliberately separate: a wheel must never try to write into ``site-packages``.
"""

from __future__ import annotations

import os
import sysconfig
from functools import lru_cache
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def source_checkout_root() -> Path | None:
    """Return the repository root when qlab is imported from a checkout."""
    candidate = _PACKAGE_DIR.parent
    if (candidate / "pyproject.toml").is_file() and (candidate / "qlab").is_dir():
        return candidate
    return None


def workspace_root() -> Path:
    """Directory for project-local outputs and orchestrator adapters."""
    override = os.environ.get("QLAB_WORKSPACE")
    if override:
        return Path(override).expanduser().resolve()
    return source_checkout_root() or Path.cwd().resolve()


def data_root() -> Path:
    """Directory containing ``mandate.yaml``, ``configs/``, and ``agents/``."""
    override = os.environ.get("QLAB_CONFIG_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    checkout = source_checkout_root()
    if checkout is not None:
        return checkout
    return Path(sysconfig.get_path("data")) / "share" / "qlab"


def data_path(*parts: str) -> Path:
    """Resolve a packaged configuration asset and fail with a useful message."""
    path = data_root().joinpath(*parts)
    if not path.exists():
        joined = "/".join(parts)
        raise FileNotFoundError(
            f"qlab data asset {joined!r} was not installed under {data_root()}; "
            "reinstall qlab or set QLAB_CONFIG_ROOT"
        )
    return path


def state_root() -> Path:
    """Writable root for the registry, cache, artifacts, and summaries."""
    override = os.environ.get("QLAB_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return workspace_root() / ".lab"


def state_path(*parts: str) -> Path:
    return state_root().joinpath(*parts)


def replace_file(src: "Path | str", dst: "Path | str", *,
                 attempts: int = 5, delay_s: float = 0.05) -> None:
    """``os.replace`` that survives Windows' open-handle window.

    POSIX rename replaces an open destination atomically; Windows refuses with
    PermissionError while ANY reader holds the destination open — and this
    codebase's atomic-write pattern (temp file, then replace) is exactly the
    shape that collides with a concurrent status poll reading the old file. A
    short bounded retry converts that platform difference back into the
    atomicity the call sites already reason about. On POSIX the first attempt
    succeeds and the loop is free.
    """
    import time as _time

    last: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if os.name != "nt":
                raise
            last = exc
            _time.sleep(delay_s * (attempt + 1))
    if last is not None:
        raise last
    raise PermissionError(f"could not replace {dst}; no attempts were made")
