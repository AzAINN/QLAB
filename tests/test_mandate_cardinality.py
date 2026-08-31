"""Cardinality: `max_holdings` caps how many names a plan may hold.

The cap is a mandate limit, not a solver preference — it is checked by
`Mandate.check_targets`, so it binds every policy including HRP.
"""

from __future__ import annotations

import pytest
import yaml

from qlab.paths import data_path
from qlab.trader.mandate import Mandate, MandateViolation, load_mandate

REFUSED = (MandateViolation, ValueError)

UNIVERSE = [f"T{i:02d}" for i in range(20)]


def _mandate(**kwargs) -> Mandate:
    params = {
        "universe_whitelist": list(UNIVERSE),
        "max_weight_per_asset": 1.0,
    }
    params.update(kwargs)
    return Mandate(**params)


def _equal_weights(n: int) -> dict[str, float]:
    return {UNIVERSE[i]: 1.0 / n for i in range(n)}


def test_nine_names_refused_under_a_cap_of_eight():
    mandate = _mandate(max_holdings=8)
    with pytest.raises(MandateViolation) as exc:
        mandate.check_targets(_equal_weights(9))
    message = str(exc.value)
    assert "9" in message, message
    assert "8" in message, message


def test_eight_names_pass_under_a_cap_of_eight():
    _mandate(max_holdings=8).check_targets(_equal_weights(8))  # no raise


def test_zero_weights_are_not_holdings():
    targets = _equal_weights(8)
    targets.update({UNIVERSE[i]: 0.0 for i in range(8, 20)})
    _mandate(max_holdings=8).check_targets(targets)  # no raise


def test_none_means_no_cap():
    _mandate(max_holdings=None).check_targets(_equal_weights(20))  # no raise


def test_default_mandate_has_no_cap():
    assert Mandate().max_holdings is None


def test_cap_of_zero_refused_at_load():
    with pytest.raises(REFUSED):
        _mandate(max_holdings=0)


def test_cap_above_the_universe_refused_at_load():
    with pytest.raises(REFUSED):
        _mandate(max_holdings=len(UNIVERSE) + 1)


def test_cap_equal_to_the_universe_is_allowed():
    assert _mandate(max_holdings=len(UNIVERSE)).max_holdings == len(UNIVERSE)


def test_dust_below_the_tolerance_is_not_a_holding():
    # Pins `> tol` over `!= 0`: a nonzero weight the mandate's own tolerance
    # treats as noise must not consume a slot under the cap.
    targets = _equal_weights(8)
    targets[UNIVERSE[8]] = 5e-5
    _mandate(max_holdings=8).check_targets(targets)  # no raise


def _shipped_yaml_with(tmp_path, **constraints):
    raw = yaml.safe_load(data_path("mandate.yaml").read_text(encoding="utf-8"))
    for key, value in constraints.items():
        if value is _ABSENT:
            raw["constraints"].pop(key, None)
        else:
            raw["constraints"][key] = value
    path = tmp_path / "mandate.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


_ABSENT = object()


def test_yaml_cap_is_loaded_and_enforced(tmp_path):
    # The shipped 12-name defensive_targets stays in place: the cap governs
    # proposals, and the mandated safety basket must not refuse the mandate.
    path = _shipped_yaml_with(tmp_path, max_holdings=8)
    mandate = load_mandate(path)

    assert mandate.max_holdings == 8
    assert len(mandate.defensive_targets) == 12
    names = mandate.universe_whitelist[:9]
    with pytest.raises(MandateViolation):
        mandate.check_targets({t: 1.0 / 9 for t in names})


def test_defensive_basket_is_exempt_from_the_cap():
    mandate = load_mandate()
    basket = load_mandate().defensive_targets
    assert len(basket) > 3
    mandate.max_holdings = 3
    with pytest.raises(MandateViolation):
        mandate.check_targets(basket)
    mandate.check_targets(basket, check_holdings=False)  # no raise


def test_absent_key_loads_uncapped(tmp_path):
    path = _shipped_yaml_with(tmp_path, max_holdings=_ABSENT)
    assert load_mandate(path).max_holdings is None


def test_yaml_cap_of_zero_refused_at_load(tmp_path):
    raw = yaml.safe_load(data_path("mandate.yaml").read_text(encoding="utf-8"))
    raw["constraints"]["max_holdings"] = 0
    path = tmp_path / "mandate.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(REFUSED):
        load_mandate(path)


def test_shipped_mandate_ships_uncapped():
    # RULING (G1): the shipped mandate ships `max_holdings: null`. The
    # operational policy is HRP, which holds every whitelisted name, so a
    # shipped cap would make every reporter plan fail the mandate. The cap is
    # set from the desk alongside a cardinality-aware policy.
    assert load_mandate().max_holdings is None
