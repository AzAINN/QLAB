"""qlab.governance — the referee gate, as code.

Nothing trades without a logged PASS verdict (research-plan §3). See
:mod:`qlab.governance.referee` for the deterministic gatekeeper.
"""

from qlab.governance.referee import deterministic_referee

__all__ = ["deterministic_referee"]
