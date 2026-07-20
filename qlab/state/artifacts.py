"""Content-addressed JSON artifact store (.lab/artifacts/<hash>.json).

Large diagnostics (weight vectors, per-arm result blobs, moment summaries) live
here as immutable, content-hashed JSON; the registry stores only the hash. Two
writes of the same content collapse to one file — free deduplication and
provenance (research-plan §2.2 invariant 3).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from qlab.core.types import _jsonable
from qlab.paths import state_path


class ArtifactStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else state_path("artifacts")
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, obj: Any) -> str:
        """Store ``obj`` as canonical JSON; return its content hash."""
        blob = json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"))
        h = hashlib.sha256(blob.encode()).hexdigest()[:16]
        path = self.root / f"{h}.json"
        if not path.exists():
            path.write_text(blob, encoding="utf-8")
        return h

    def get(self, h: str) -> Any:
        path = self.root / f"{h}.json"
        if not path.exists():
            raise KeyError(f"artifact {h} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def exists(self, h: str) -> bool:
        return (self.root / f"{h}.json").exists()
