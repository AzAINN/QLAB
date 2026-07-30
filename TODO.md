note - for Azain to keep track and jot things down

## done

* ~~making textual UI interface to see the working of the application~~ —
  Textual desk shipped, plus a second Ratatui client in `clients/atlas-tui`
* ~~Quantum Research components/parts to include quantum in~~ — offline
  QAOA / Ising lane, and quantum-inspired feature maps in `qlab/research/`.
  Measured: the feature maps hurt the forecast, so they stay research-stage
  (planning-docs/2026-07-30-ml-lane.md)
* ~~Tasks division and layout of progression~~ — five governed roles generated
  from `agents/`, phase DAG persisted in the registry and resumable

## next

* live-on-Alpaca-book has never been exercised end to end — the first real
  paper trade through the approval gate
* port more of the surface into `atlas-tui` (market chart, book, audit trail)
* explain *why* MVSK loses before adding solver complexity: lambda sweeps,
  estimator sensitivity
* if the ML lane is revived: group-wise ridge penalties or a kernel form — the
  diagnosed failure is variance inflation, so more data will not fix it
* emit real `.bob/custom_modes.yaml` from `agents/`
