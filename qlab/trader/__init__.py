"""qlab.trader — the execution gateway.

*The agent gets no raw order tool* (research-plan §8.1). The mandate is enforced
in deterministic code, not by prompt. Order plans are registry objects with a
state machine so a session dying mid-rebalance resumes instead of double-ordering.
Paper mode is hard-coded; live trading is *unimplemented*, not merely disabled.
"""

from qlab.trader.broker import Broker, SimulatedPaperBroker, get_broker
from qlab.trader.mandate import Mandate, MandateViolation, load_mandate
from qlab.trader.plan import OrderPlan, build_plan

__all__ = [
    "Broker",
    "Mandate",
    "MandateViolation",
    "OrderPlan",
    "SimulatedPaperBroker",
    "build_plan",
    "get_broker",
    "load_mandate",
]
