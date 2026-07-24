"""BobTheQuant: the always-present desk manager above deterministic controls.

The package hosts the *deterministic* supervisor (lifecycle, triggers, dedupe,
budgets, persistence). The interpreting ``bob-the-quant`` agent — which reads
facts and chooses workflows — is invoked through the coordinator seam and holds
no execution or proposal authority.
"""

from qlab.operator.bob import BobConfig, BobSupervisor

__all__ = ["BobConfig", "BobSupervisor"]
