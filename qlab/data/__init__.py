"""Operational data plane: freshness, eligibility, and data permits.

Kept separate from ``qlab.core.data`` (fetch/cache/snapshot) so the *integrity
verdict* on a snapshot — is it real, fresh, and eligible to move paper money —
lives in one auditable place rather than being re-derived per caller.
"""

from qlab.data.health import DataHealth, evaluate_panel_health
from qlab.data.permit import DataPermit, build_permit

__all__ = ["DataHealth", "evaluate_panel_health", "DataPermit", "build_permit"]
