"""Desk-chosen mandate overrides: the operational method and the holdings cap.

The shipped `mandate.yaml` is never edited from the desk. The operator's two
choices live in `state_path("mandate_overrides.json")` and are merged by
`load_mandate` after the yaml read and before validation, so a cap the mandate
would refuse is refused at the door rather than persisted.
"""

from __future__ import annotations

import json

import pytest

from qlab.paths import state_path
from qlab.state.registry import Registry
from qlab.trader.mandate import MandateViolation, load_mandate
from qlab.ui.server import UISession, handle_api

OVERRIDES = "mandate_overrides.json"


@pytest.fixture
def session():
    return UISession(offline_default=True, registry=Registry(":memory:"))


def _get(session):
    status, payload = handle_api(session, "GET", "/api/desk/method", {}, {})
    assert status == 200, payload
    return payload


def _post(session, body):
    return handle_api(session, "POST", "/api/desk/method", {}, body)


def _written() -> dict:
    return json.loads(state_path(OVERRIDES).read_text(encoding="utf-8"))


def test_get_lists_the_operational_methods_and_marks_the_current_one(session):
    payload = _get(session)
    assert payload["current"]["operational_policy"] == "hrp"
    assert payload["current"]["max_holdings"] is None
    by_id = {row["id"]: row for row in payload["operational"]}
    assert set(by_id) == {"hrp", "risk_parity", "min_variance"}
    assert by_id["hrp"]["current"] is True
    assert by_id["min_variance"]["current"] is False
    assert by_id["hrp"]["arm_id"] == "B2"
    assert by_id["hrp"]["label"] and by_id["hrp"]["rationale"]
    assert payload["overrides"] == {}
    assert payload["warning"] is None


def test_get_lists_research_allocation_entries_as_not_choosable(session):
    research = {row["id"]: row for row in _get(session)["research"]}
    assert "cardinal_min_variance" in research
    entry = research["cardinal_min_variance"]
    assert entry["choosable"] is False
    assert entry["stage"] == "research"
    # A research entry must never leak into the choosable list.
    assert "cardinal_min_variance" not in {
        row["id"] for row in _get(session)["operational"]}


def test_post_policy_persists_and_the_session_and_a_reload_reflect_it(session):
    status, payload = _post(session, {"operational_policy": "min_variance"})
    assert status == 200, payload
    assert payload["current"]["operational_policy"] == "min_variance"
    assert payload["overrides"] == {"operational_policy": "min_variance"}
    assert _written() == {"operational_policy": "min_variance"}
    # The running session, not just the file: everything that governs a plan
    # reads `session.mandate`.
    assert session.mandate.operational_policy == "min_variance"
    assert load_mandate().operational_policy == "min_variance"
    assert session.registry.read_events_of_kind("mandate_override")


def test_the_audit_row_carries_the_field_the_value_and_what_it_replaced(session):
    _post(session, {"operational_policy": "min_variance"})
    rows = session.registry.read_events_of_kind("mandate_override")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["field"] == "operational_policy"
    assert payload["value"] == "min_variance"
    assert payload["previous"] == "hrp"


def test_post_a_research_stage_method_is_refused_naming_its_stage(session):
    status, payload = _post(session, {"operational_policy": "cardinal_min_variance"})
    assert status == 400
    assert "cardinal_min_variance" in payload["error"]
    assert "research" in payload["error"]
    assert not state_path(OVERRIDES).exists()
    assert session.mandate.operational_policy == "hrp"


def test_post_an_unknown_method_is_refused(session):
    status, payload = _post(session, {"operational_policy": "no_such_method"})
    assert status == 400
    assert "no_such_method" in payload["error"]


def test_post_a_cap_persists_and_binds_the_sessions_mandate(session):
    status, payload = _post(session, {"max_holdings": 5})
    assert status == 200, payload
    assert payload["current"]["max_holdings"] == 5
    assert _written() == {"max_holdings": 5}
    mandate = session.mandate
    assert mandate.max_holdings == 5
    six = {t: 1 / 6 for t in mandate.universe_whitelist[:6]}
    with pytest.raises(MandateViolation):
        mandate.check_targets(six)
    five = {t: 0.2 for t in mandate.universe_whitelist[:5]}
    mandate.check_targets(five)  # no raise
    # The mandated defensive basket is exempt, so the mandate still loads.
    assert len(load_mandate().defensive_targets) == 12


def test_a_cap_under_a_policy_that_holds_every_name_warns_but_persists(session):
    status, payload = _post(session, {"max_holdings": 5})
    assert status == 200
    warning = payload["warning"]
    assert warning and "5" in warning
    assert "holds every name" in warning
    assert session.mandate.max_holdings == 5


def test_a_cap_under_min_variance_carries_no_warning(session):
    _post(session, {"operational_policy": "min_variance"})
    status, payload = _post(session, {"max_holdings": 5})
    assert status == 200, payload
    assert payload["warning"] is None


def test_a_cap_outside_the_universe_is_refused(session):
    over = len(session.mandate.universe_whitelist) + 1
    for bad in (0, -1, over, "5", 2.5, True):
        status, payload = _post(session, {"max_holdings": bad})
        assert status == 400, (bad, payload)
    assert not state_path(OVERRIDES).exists()
    assert session.mandate.max_holdings is None


def test_null_clears_an_override(session):
    _post(session, {"max_holdings": 5, "operational_policy": "min_variance"})
    status, payload = _post(session, {"max_holdings": None})
    assert status == 200, payload
    assert payload["current"]["max_holdings"] is None
    assert payload["overrides"] == {"operational_policy": "min_variance"}
    assert _written() == {"operational_policy": "min_variance"}
    assert session.mandate.max_holdings is None
    assert session.mandate.operational_policy == "min_variance"


def test_an_unsupported_override_key_is_refused_by_the_route(session):
    status, payload = _post(session, {"paper_capital": 1000000.0})
    assert status == 400
    assert "paper_capital" in payload["error"]


def test_an_unsupported_override_key_refuses_at_load_naming_it():
    path = state_path(OVERRIDES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"paper_capital": 1000000.0}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_mandate()
    assert "paper_capital" in str(exc.value)


def test_overrides_survive_a_new_owner_runtime(session):
    _post(session, {"operational_policy": "min_variance", "max_holdings": 5})
    fresh = UISession(offline_default=True, registry=Registry(":memory:"))
    assert fresh.mandate.operational_policy == "min_variance"
    assert fresh.mandate.max_holdings == 5
    assert _get(fresh)["current"] == {
        "operational_policy": "min_variance", "max_holdings": 5}


def test_an_empty_post_is_refused(session):
    status, payload = _post(session, {})
    assert status == 400
    assert payload["error"]
