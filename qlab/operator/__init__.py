"""Atlas: the always-present desk manager above deterministic controls.

The package hosts the *deterministic* supervisor (lifecycle, triggers, dedupe,
budgets, persistence). The interpreting ``atlas`` agent — which reads
facts and chooses workflows — is invoked through the coordinator seam and holds
no execution or proposal authority.
"""

from qlab.operator.atlas import AtlasConfig, AtlasSupervisor

__all__ = ["AtlasConfig", "AtlasSupervisor"]
