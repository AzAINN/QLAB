# qlab continuation ledger — 2026-07-19

This is the current handoff for work resumed after the Claude Code session was
interrupted. It separates recovered review evidence, completed cleanup, the
algorithm deployment boundary, and future integration work.

## Baseline recovered

- Starting commit: 06f9596 on main, 28 commits ahead of origin/main.
- Starting tree: no modified tracked files; CLAUDE.md and TODO.md were untracked
  user files and were preserved.
- Starting verification: 118 passed, 1 skipped.
- The completed high-effort review was recovered from the local Claude workflow
  output, including all ten synthesized findings and verifier verdicts.
- Each finding was then checked against commit 06f9596 before edits began.

## Recovered review findings

| # | Finding | Resolution |
|---|---|---|
| 1 | Wheel omitted mandate, universe, and agent data; runtime wrote state relative to site-packages | Central path module, packaged share/qlab data, writable workspace/state overrides, install tests |
| 2 | README and env file claimed an IBM hardware path no code implemented | Removed claim and phantom IBM variables; offline implementation is explicitly Aer-only |
| 3 | Retired standalone MCP module mains bypassed the owner guard | Both module mains delegate to the guarded combined server |
| 4 | Skew-only Dirac payload declared degree two while emitting degree-three terms | Degree derives from active skew/kurtosis terms; regression test added |
| 5 | TUI hard-coded the former seven-asset resource headline | Resource panel removed; TUI shows the deployment catalog instead |
| 6 | qlab.mcp public exports omitted server and tui_proxy | Public export list corrected |
| 7 | Project claimed MIT but had no license grant | LICENSE added |
| 8 | .env.example advertised unused Alpha Vantage and Alpaca mode variables | Phantom variables removed; real runtime path variables documented |
| 9 | UI server docstring claimed main-thread handling and omitted live routes | Thread/lock behavior and complete staged routes documented |
| 10 | Planning documents contradicted delivered state | Historical plans now carry explicit implemented, archived, or superseded banners |

## Algorithm deployment boundary

qlab/algorithms/catalog.py is now the source of truth for method deployment.

- operational: visible and runnable through staged agent tools.
- research: visible as controlled experimental work, not directly agent-runnable.
- offline: retained implementation with no CLI, HTTP, TUI, default-ablation, or
  MCP execution path.

The normal solver registry excludes offline QAOA and the resource estimator.
Explicit offline imports remain available for isolated tests and research.

The owner API and TUI expose the full catalog so operators can see what exists
without confusing visibility with authority. The Claude optimization-runner now
uses algorithms.list, algorithms.describe, and algorithms.solve. The server
enforces the stage check; this is not prompt-only policy.

## Current Claude Code topology

The Textual path is now the primary operator workflow:

1. Headless Claude Code launches the single qlab MCP server when the owner
   runtime is absent. Research and trader namespaces share one Registry writer.
2. `qlab tui` owns the book and offers to launch the Claude CLI as an inline
   `qlab-coordinator`. Its only built-in capability is the Agent dispatcher,
   allowlisted to moments-analyst, challenger, optimization-runner, referee,
   and reporter. It has no developer/file/shell tools.
3. Each role receives only its owner-backed qlab MCP tools plus one role-bound
   phase-update tool. The proxy never opens DuckDB and exposes no paper
   execution tool.
4. Workflow and step rows persist analyst → challenger → optimizer → referee →
   reporter state in the owner registry. The Textual Workforce view shows that
   state; `workforce resume ID` continues an interrupted run in a new CLI
   session without creating a second owner.

Agent definitions remain neutral in agents/ and are generated into both
.claude/agents and .bob/personas. Algorithm stage metadata now gives those
agents a stable discovery layer instead of hard-coded solver lore.

## Operational policy correction

MVSK is the mean-variance-skewness-kurtosis research hypothesis: the qlab form
is risk-only (no expected-return forecast) and exists to test whether shrunk
higher moments add out-of-sample value, while also exercising the degree-four
polynomial compiler. It is not supported as the paper champion by the current
results.

- `mandate.yaml` now selects `hrp` as the explicit operational allocation
  policy.
- HRP, ERC, minimum variance, and scenario CVaR remain operational catalog
  entries.
- `mvsk_multistart` is a research catalog entry: visible in the ablation but
  rejected by the staged `algorithms.solve` boundary.
- `recommend`, `run-once`, the owner API, agents, Textual copy, and web copy now
  use or report the configured policy instead of hard-coding A3/MVSK.

## Quantum direction

The former staged QAOA controls and fixed gate-resource headline were removed.
The implementation is retained only under the explicit offline algorithm lane:

- qlab/algorithms/offline/quantum.py is the public offline entry point.
- qlab/solvers/quantum.py remains the low-level Aer implementation.
- tests/test_quantum.py and the offline scaling script exercise it in isolation.
- No IBM token is consumed and no real-hardware capability is claimed.

Promotion back into any staged surface requires applicable evidence, a catalog
stage change, tool-authority review, and regression tests.

## Verification record

- Baseline suite before edits: 118 passed, 1 skipped.
- Final suite after the continuation: 143 passed, 1 optional-dependency skip.
- Python bytecode compilation and whitespace/error checks pass.
- A real-terminal Textual smoke run rendered the quiet no-header workstation,
  Workforce view, and Claude-ready owner state, then shut down cleanly.
- A clean wheel contains the mandate, universe, specifications, agent sources,
  license, catalog, and offline modules.
- Installing that wheel into a fresh environment and running `qlab recommend
  --offline --as-of 2021-06-30` from outside the checkout succeeds; writable
  state is created in the configured runtime workspace, not site-packages.

## Next work, in order

1. Exercise one authenticated Claude workforce run end to end from Textual and
   capture the phase/event trace; automated tests cover the command and owner
   contracts but do not spend a live Claude session.
2. Implement true Alpaca paper data/order lifecycle separately; current online
   data remains daily yfinance/cache and current Alpaca execution is partial.
3. Run the August research program (views, lambda sweep, larger-universe stress)
   without promoting MVSK unless new evidence clears the catalog gate.
4. Exercise the generated IBM Bob personas when access is available.

No merge, push, or live-money action is authorized by this ledger.
