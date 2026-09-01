

def test_a_spec_may_not_claim_two_universes():
    """Both keys set makes the run unreproducible from the file alone."""
    import pytest

    from qlab.experiment import run_ablation
    from qlab.state.registry import Registry

    reg = Registry(":memory:")
    try:
        with pytest.raises(ValueError, match="pick one"):
            run_ablation(
                {"name": "x", "seed": 7,
                 "data": {"tickers": ["ACWI"], "universe": "core"},
                 "arms": []},
                offline=True, registry=reg)
    finally:
        reg.close()


def test_the_published_ablation_pins_its_tickers_by_value():
    """configs/specs/ablation_v1.yaml carries the submission numbers.

    Resolving `universe: core` at runtime meant a universe edit silently
    redefined them — and because the synthetic generator draws factor loadings
    per panel, growing `core` does not add assets to an existing arm, it changes
    the data underneath the ones already there. That is not visible as a diff.
    """
    import yaml

    from qlab.paths import workspace_root

    spec = yaml.safe_load(
        (workspace_root() / "configs/specs/ablation_v1.yaml").read_text(encoding="utf-8"))
    assert spec["data"]["tickers"] == [
        "ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]
    assert "universe" not in spec["data"]
