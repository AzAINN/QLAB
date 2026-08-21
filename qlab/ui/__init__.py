"""qlab.ui — the owner runtime.

A stdlib HTTP server ([`server.py`](server.py)) that is the desk's single
DuckDB writer and exposes every operation as a JSON API. Every client — the
Atlas workstation, the CLI verbs, the MCP proxy — observes it over HTTP.
`qlab` starts it under the workstation; `qlab owner` runs it headless.
"""
