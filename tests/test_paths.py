from pathlib import Path

from qlab.paths import data_path, state_path, workspace_root


def test_default_data_assets_resolve() -> None:
    assert data_path("mandate.yaml").is_file()
    assert data_path("configs", "universe.yaml").is_file()
    assert list(data_path("agents").glob("*.md"))


def test_runtime_paths_honor_explicit_overrides(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    monkeypatch.setenv("QLAB_WORKSPACE", str(workspace))
    monkeypatch.setenv("QLAB_STATE_DIR", str(state))

    assert workspace_root() == workspace
    assert state_path("registry.duckdb") == state / "registry.duckdb"


def test_packaged_config_root_override(tmp_path, monkeypatch) -> None:
    root = tmp_path / "qlab-data"
    (root / "configs").mkdir(parents=True)
    asset = root / "configs" / "universe.yaml"
    asset.write_text("core: []\n", encoding="utf-8")
    monkeypatch.setenv("QLAB_CONFIG_ROOT", str(root))

    assert data_path("configs", "universe.yaml") == Path(asset)
