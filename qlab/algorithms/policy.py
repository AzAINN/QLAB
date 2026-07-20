"""Explicit operational allocation policy, separate from research arms.

The experiment catalog answers "what may be studied?". This module answers the
smaller deployment question: "which already-operational policy is the paper
desk configured to use?" Keeping those questions separate prevents a novel arm
from becoming the champion merely because it is the subject of the research.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from qlab.algorithms.catalog import get_algorithm
from qlab.arms import Arm


@dataclass(frozen=True)
class OperationalPolicy:
    id: str
    label: str
    arm_id: str
    objective: str
    solver: str
    algorithm_id: str
    rationale: str

    def to_dict(self) -> dict:
        return asdict(self)

    def arm(self) -> Arm:
        return Arm(self.arm_id, self.objective, self.solver)


_POLICIES = {
    "hrp": OperationalPolicy(
        id="hrp",
        label="Hierarchical risk parity",
        arm_id="B2",
        objective="hrp",
        solver="hrp",
        algorithm_id="hrp",
        rationale=(
            "Current evidence favors the robust HRP benchmark out of sample; "
            "MVSK remains a falsifiable research arm."
        ),
    ),
    "risk_parity": OperationalPolicy(
        id="risk_parity",
        label="Equal risk contribution",
        arm_id="B3",
        objective="risk_parity",
        solver="risk_parity",
        algorithm_id="risk_parity",
        rationale="Operational covariance-only alternative with equalized risk contribution.",
    ),
    "min_variance": OperationalPolicy(
        id="min_variance",
        label="Minimum variance",
        arm_id="A1",
        objective="min_variance",
        solver="classical",
        algorithm_id="min_variance",
        rationale="Operational convex covariance-only allocation.",
    ),
}


def get_operational_policy(policy_id: str) -> OperationalPolicy:
    try:
        policy = _POLICIES[policy_id]
    except KeyError as exc:
        raise ValueError(
            f"unknown operational policy {policy_id!r}; choose from {sorted(_POLICIES)}"
        ) from exc
    algorithm = get_algorithm(policy.algorithm_id)
    if not algorithm.agent_usable:
        raise RuntimeError(
            f"configured policy {policy_id!r} maps to non-operational algorithm "
            f"{algorithm.id!r} (stage={algorithm.stage})"
        )
    return policy


def list_operational_policies() -> list[dict]:
    return [policy.to_dict() for policy in _POLICIES.values()]
