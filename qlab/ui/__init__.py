"""qlab.ui — an elegant, dependency-free single-page control surface.

A tiny stdlib HTTP server ([`server.py`](server.py)) exposes every operation the
`qlab` CLI offers as a JSON API and serves one self-contained HTML page
([`index.html`](index.html), vanilla JS + inline CSS, no CDN — so it works fully
offline). Launch it with ``qlab ui``.

The server is intentionally **single-threaded** so Qiskit runs on the main thread
(the documented Aer/BLAS constraint), and it shares one DuckDB book with the
autopilot so the UI reflects the same paper portfolio.
"""

from qlab.ui.server import UISession, serve

__all__ = ["UISession", "serve"]
