from pathlib import Path

import pytest

from qlab.paths import data_path, replace_file, state_path, workspace_root


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


def test_replace_file_retries_permission_windows_open_handle(tmp_path, monkeypatch) -> None:
    src = tmp_path / "new.txt"
    dst = tmp_path / "old.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    calls = 0

    def flaky_replace(raw_src, raw_dst):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("destination is open")
        raw_dst.write_text(raw_src.read_text(encoding="utf-8"),
                           encoding="utf-8")

    import qlab.paths as paths_mod

    monkeypatch.setattr(paths_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(paths_mod.os, "replace", flaky_replace)

    replace_file(src, dst, attempts=3, delay_s=0)

    assert calls == 3
    assert dst.read_text(encoding="utf-8") == "new"


def test_replace_file_does_not_retry_permission_on_posix(tmp_path, monkeypatch) -> None:
    src = tmp_path / "new.txt"
    dst = tmp_path / "old.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    calls = 0

    def locked_replace(raw_src, raw_dst):
        nonlocal calls
        calls += 1
        raise PermissionError("destination is open")

    import qlab.paths as paths_mod

    monkeypatch.setattr(paths_mod.os, "name", "posix", raising=False)
    monkeypatch.setattr(paths_mod.os, "replace", locked_replace)

    with pytest.raises(PermissionError):
        replace_file(src, dst, attempts=3, delay_s=0)

    assert calls == 1
    assert dst.read_text(encoding="utf-8") == "old"
