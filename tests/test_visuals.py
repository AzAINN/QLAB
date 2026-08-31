"""The visuals registry and the first visual it carries.

Discovery is exercised against the real package, and the refusal against a
throwaway package written under ``tmp_path`` and imported by name — the same
``_discover`` the registry itself calls, on a package that genuinely lacks
``render``. Nothing is monkeypatched: a fake module walk would have proved
only that the fake refuses.
"""

from __future__ import annotations

import sys

import pytest

from qlab import visuals


@pytest.fixture(autouse=True)
def _forget_fake_packages():
    """A fake package imported by a test must not outlive it in sys.modules —
    the next test's discovery would otherwise see a module it never wrote."""
    before = set(sys.modules)
    yield
    for name in [n for n in sys.modules if n not in before and n.startswith("fake_visuals")]:
        sys.modules.pop(name, None)


def _write_package(tmp_path, name: str, modules: dict[str, str]):
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("")
    for module_name, source in modules.items():
        (root / f"{module_name}.py").write_text(source)
    sys.path.insert(0, str(tmp_path))
    try:
        return __import__(name)
    finally:
        sys.path.remove(str(tmp_path))


def test_catalog_discovers_the_quantum_circuit():
    catalog = visuals.catalog()
    assert "quantum_circuit" in catalog
    spec = catalog["quantum_circuit"]
    assert spec.name == "quantum_circuit"
    assert isinstance(spec.title, str) and spec.title
    assert callable(spec.render)


def test_module_with_title_but_no_render_is_refused_by_name(tmp_path):
    package = _write_package(
        tmp_path,
        "fake_visuals_missing_render",
        {"halfway": 'TITLE = "Halfway there"\n'},
    )
    with pytest.raises(RuntimeError) as excinfo:
        visuals._discover(package)
    message = str(excinfo.value)
    assert "halfway" in message
    assert "render" in message


def test_module_with_render_but_no_title_is_refused_by_name(tmp_path):
    package = _write_package(
        tmp_path,
        "fake_visuals_missing_title",
        {"nameless": "def render(params):\n    return 'x'\n"},
    )
    with pytest.raises(RuntimeError) as excinfo:
        visuals._discover(package)
    message = str(excinfo.value)
    assert "nameless" in message
    assert "TITLE" in message


def test_render_refuses_unknown_name_and_lists_the_known_ones():
    with pytest.raises(KeyError) as excinfo:
        visuals.render("nope", {})
    message = str(excinfo.value)
    assert "nope" in message
    assert "quantum_circuit" in message


def _wires(text: str) -> list[str]:
    return [line for line in text.splitlines() if "|0>" in line]


def _entangler(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("ZZ")]


def test_three_feature_angle_drawing_has_three_wires_and_no_entangler():
    text = visuals.render(
        "quantum_circuit",
        {"features": ["mom", "vol", "disp"], "kernel": "angle"},
    )
    wires = _wires(text)
    assert len(wires) == 3
    assert all("RY(" in line for line in wires)
    assert _entangler(text) == []
    assert "kernel=angle" in text
    assert "features=3" in text


def test_default_features_are_three_generic_wires():
    text = visuals.render("quantum_circuit", {})
    wires = _wires(text)
    assert len(wires) == 3
    for index in range(3):
        assert f"x{index}" in wires[index]


def test_zz_drawing_names_each_pair_once():
    text = visuals.render(
        "quantum_circuit",
        {"features": ["a", "b", "c"], "kernel": "zz"},
    )
    rows = _entangler(text)
    assert len(rows) == 1
    row = rows[0]
    listed = row.split("over:", 1)[1]
    for pair in ("(a,b)", "(a,c)", "(b,c)"):
        assert listed.count(pair) == 1
    # No pair invented, and none duplicated under another ordering.
    assert listed.count("(") == 3


def test_angles_render_to_two_decimals():
    text = visuals.render(
        "quantum_circuit",
        {"features": ["a", "b"], "angles": [0.7853981, -1.5707963]},
    )
    assert "RY(0.79)" in text
    assert "RY(-1.57)" in text


def test_missing_angles_render_as_theta_placeholders():
    text = visuals.render("quantum_circuit", {"features": ["a", "b"]})
    assert "RY(θ0)" in text
    assert "RY(θ1)" in text


def test_thirteen_wires_refuse():
    features = [f"f{i}" for i in range(13)]
    with pytest.raises(ValueError) as excinfo:
        visuals.render("quantum_circuit", {"features": features})
    message = str(excinfo.value)
    assert "13" in message
    assert "12" in message


def test_twelve_wires_are_allowed():
    features = [f"f{i}" for i in range(12)]
    text = visuals.render("quantum_circuit", {"features": features})
    assert len(_wires(text)) == 12


def test_angle_count_must_match_feature_count():
    with pytest.raises(ValueError) as excinfo:
        visuals.render(
            "quantum_circuit",
            {"features": ["a", "b"], "angles": [0.1]},
        )
    assert "angles" in str(excinfo.value)


def test_unknown_kernel_is_refused():
    with pytest.raises(ValueError) as excinfo:
        visuals.render(
            "quantum_circuit", {"features": ["a"], "kernel": "banana"}
        )
    assert "banana" in str(excinfo.value)


def test_gate_columns_line_up():
    text = visuals.render(
        "quantum_circuit",
        {"features": ["short", "a_much_longer_name"], "kernel": "zz"},
    )
    wires = _wires(text)
    starts = {line.index("[") for line in wires}
    ends = {line.index("]") for line in wires}
    assert len(starts) == 1 and len(ends) == 1


def test_the_prose_never_runs_wider_than_the_wires():
    """The wires cannot be re-wrapped without moving gates off them, so the
    sentences around them are the ones that must fit the drawing's width —
    a 52-column pane showed the wires and clipped the line that said no
    circuit was executed."""
    from qlab.visuals import quantum_circuit as qc
    text = qc.render({"features": ["a", "b", "c"]})
    lines = text.splitlines()
    wires = [l for l in lines if "|0>" in l]
    width = max(max(len(w) for w in wires), 40)
    prose = [l for l in lines if l and "|0>" not in l and not l.startswith("ZZ entangler")]
    assert prose and all(len(l) <= width for l in prose)
