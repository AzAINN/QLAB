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
* the ML lane was revived and measured (2026-07-31): group-wise ridge and the
  closed-form kernels stop the augmentation from *hurting* (explicit ZZ was
  0/12 wins, t −4.53; kernel ZZ is 7/12, t −0.56) but produce no edge over the
  ridge baseline. The predictor board (`research.predictor_board`) keeps the
  paired comparison honest and is Atlas-readable; next candidates, if any:
  story-backed feature pairs, not more data
* emit real `.bob/custom_modes.yaml` from `agents/`
