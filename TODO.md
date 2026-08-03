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
* `feat/atlas-full-desk` (2026-08-03, 10 commits, not pushed): the desk could
  not see itself. Thirteen findings where a producer computed the right thing
  and the consumer never got it — reasoner rendering 6 of 13 context keys,
  grounding deleting 46 of 50 fetched stories, the agent bus filtering on
  kinds the parser never emits. Full write-up with live numbers in
  planning-docs/2026-08-03-desk-visibility-audit.md. The predictor board now
  has a web panel, and `kernel:linear` turned out to be a **control** (no
  feature map — `quantum_gram` returns early for linear) that was being
  counted in the augmented arm.
  **The finding that matters most:** the board's champion is the argmax over
  7 tuned models judged against a fixed *per-model* bar, and **84 of 100
  pure-noise panels cleared that bar** (66 produced an admitted champion,
  median top IC +0.21 — above the live desk's own champion). The board now
  runs its whole selection procedure against a circular-shift null and
  reports `champion_established` (True/False/**None**). The live champion
  `groupwise:angle_zz` scores +0.1720 against a null median of +0.1722,
  p=0.52 — **not established**. This reinforces the "no edge" conclusion
  above and sharpens it: an *admitted champion* on this board is not weak
  evidence of augmentation, it is the expected output of the procedure on
  nothing at all. The binding constraint is the protocol, not the model:
  615 rows at a 21-day overlapping horizon carry ~18.5 effective
  observations, which cannot separate 7 candidates. Shorter horizon, longer
  history, or a non-overlapping target before any more model search.

* emit real `.bob/custom_modes.yaml` from `agents/`
