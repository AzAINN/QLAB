"""Registry-enforced bounded debate and adjudication (P5 gap 3)."""

from __future__ import annotations

import pytest

from qlab.governance.debate import (
    ADJUDICATED,
    MAX_ROUNDS,
    DebateViolation,
    adjudicate,
    open_debate,
    open_debates_for,
    record_turn,
    validate_claim,
)
from qlab.state.registry import Registry


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def _open(reg, claims=("estimation_window",)):
    return open_debate(reg, workflow_id="wf-1", original_decision_id="dec-1",
                       material_claims=list(claims),
                       panel_snapshot_id="snap-1")


# --- claim allowlist ---------------------------------------------------------


def test_target_weight_arguments_are_refused():
    for forbidden in ("target_weights", "targets", "execution", "referee_verdict"):
        with pytest.raises(DebateViolation, match="not debatable"):
            validate_claim(forbidden)


def test_unknown_claim_is_refused():
    with pytest.raises(DebateViolation, match="not an allowlisted"):
        validate_claim("vibes")


def test_allowlisted_claims_pass():
    for allowed in ("estimation_window", "shrinkage", "regime_read"):
        assert validate_claim(allowed) == allowed


def test_opening_a_debate_on_a_forbidden_claim_is_refused(reg):
    with pytest.raises(DebateViolation, match="not debatable"):
        open_debate(reg, workflow_id="wf-1", original_decision_id="dec-1",
                    material_claims=["target_weights"])


# --- round ceiling -----------------------------------------------------------


def test_a_third_round_is_impossible_in_code(reg):
    did = _open(reg)
    for _ in range(MAX_ROUNDS):
        record_turn(reg, did, role="challenger", claim_id="estimation_window",
                    position="rebut", argument="the window is too short")
    with pytest.raises(DebateViolation, match="rounds"):
        record_turn(reg, did, role="challenger", claim_id="estimation_window",
                    position="rebut", argument="one more for the road")


def test_each_role_gets_its_own_rounds(reg):
    did = _open(reg)
    record_turn(reg, did, role="challenger", claim_id="estimation_window",
                position="rebut", argument="too short")
    # The analyst's reply is its own first round, not the challenger's second.
    turn = record_turn(reg, did, role="moments-analyst",
                       claim_id="estimation_window", position="defend",
                       argument="the window matches the regime")
    assert turn.round == 1
    assert len(reg.list_debate_turns(did)) == 2


def test_turn_on_an_unopened_claim_is_refused(reg):
    did = _open(reg, claims=("estimation_window",))
    with pytest.raises(DebateViolation, match="not opened with this debate"):
        record_turn(reg, did, role="challenger", claim_id="shrinkage",
                    position="rebut", argument="shrink harder")


def test_turn_needs_an_argument_and_a_valid_position(reg):
    did = _open(reg)
    with pytest.raises(DebateViolation, match="needs an argument"):
        record_turn(reg, did, role="challenger", claim_id="estimation_window",
                    position="rebut", argument="   ")
    with pytest.raises(DebateViolation, match="position must be"):
        record_turn(reg, did, role="challenger", claim_id="estimation_window",
                    position="shout", argument="valid text")


# --- adjudication ------------------------------------------------------------


def test_adjudication_closes_the_debate_and_references_exact_ids(reg):
    did = _open(reg)
    record_turn(reg, did, role="challenger", claim_id="estimation_window",
                position="rebut", argument="too short")
    result = adjudicate(reg, did, decided_by="referee",
                        resolution="the shorter window is justified by the regime",
                        winning_claim_positions={"estimation_window": "defend"},
                        amended_decision_id="dec-2")
    assert result["original_decision_id"] == "dec-1"
    assert result["panel_snapshot_id"] == "snap-1"
    assert result["amended_decision_id"] == "dec-2"
    stored = reg.get_debate(did)
    assert stored["status"] == ADJUDICATED
    assert stored["adjudication"]["decided_by"] == "referee"
    # No further turns after closing.
    with pytest.raises(DebateViolation, match="no further turns"):
        record_turn(reg, did, role="challenger", claim_id="estimation_window",
                    position="rebut", argument="late")


def test_adjudication_must_decide_every_claim(reg):
    did = _open(reg, claims=("estimation_window", "shrinkage"))
    with pytest.raises(DebateViolation, match="leaves claims undecided"):
        adjudicate(reg, did, decided_by="referee", resolution="partial",
                   winning_claim_positions={"estimation_window": "defend"})


def test_debate_events_are_durable(reg):
    did = _open(reg)
    record_turn(reg, did, role="challenger", claim_id="estimation_window",
                position="rebut", argument="too short")
    adjudicate(reg, did, decided_by="referee", resolution="resolved",
               winning_claim_positions={"estimation_window": "amend"})
    kinds = [e["kind"] for e in reg.read_events(20)]
    for expected in ("debate.started", "debate.turn_recorded",
                     "debate.adjudicated", "debate.closed"):
        assert expected in kinds


def test_open_debates_for_tracks_unresolved_disagreement(reg):
    did = _open(reg)
    assert [d["debate_id"] for d in open_debates_for(reg, "wf-1")] == [did]
    adjudicate(reg, did, decided_by="referee", resolution="done",
               winning_claim_positions={"estimation_window": "defend"})
    assert open_debates_for(reg, "wf-1") == []


# --- the reporter gate -------------------------------------------------------


def test_reporter_cannot_complete_with_an_unadjudicated_debate(reg):
    wf = reg.start_workflow("portfolio_review", {"goal": "test"})
    workflow_id = wf["workflow_id"] if isinstance(wf, dict) else wf
    did = open_debate(reg, workflow_id=workflow_id,
                      original_decision_id="dec-1",
                      material_claims=["estimation_window"])
    with pytest.raises(RuntimeError, match="unadjudicated debates"):
        reg.update_workflow_phase(
            workflow_id, "reporter", "done", "summary",
            {"recommendation": "hold"})
    # Adjudicating clears the gate (the phase then fails only on its own deps).
    adjudicate(reg, did, decided_by="referee", resolution="resolved",
               winning_claim_positions={"estimation_window": "defend"})
    with pytest.raises(RuntimeError, match="cannot start before"):
        reg.update_workflow_phase(
            workflow_id, "reporter", "done", "summary",
            {"recommendation": "hold"})
