"""Resolving Alpaca credentials from env or the Alpaca CLI's own profiles."""

from __future__ import annotations

import pytest

from qlab.trader.alpaca_auth import (
    AlpacaAuthError, describe_credentials, resolve_alpaca_credentials)


@pytest.fixture(autouse=True)
def _forget_ambient_profile(monkeypatch):
    """Most tests write a profile named `paper` and rely on it being the default,
    so an operator or CI shell exporting ALPACA_PROFILE must not reach us."""
    monkeypatch.delenv("ALPACA_PROFILE", raising=False)


def _write_profile(tmp_path, name="paper", body=None):
    """Lay out an Alpaca CLI config dir the way the real CLI does."""
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        f"default_profile: {name}\noutput: json\n", encoding="utf-8")
    (tmp_path / "profiles" / f"{name}.yaml").write_text(
        body if body is not None else
        "api_key: ''\nsecret_key: ''\n"
        "access_token: tok-abcdefghijklmnopqrstuvwxyz012345\n"
        "scopes: account:write trading data\n",
        encoding="utf-8")
    return tmp_path


def test_env_credentials_win_over_a_cli_profile(tmp_path, monkeypatch):
    _write_profile(tmp_path)
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_API_KEY", "PKENVKEY")
    monkeypatch.setenv("ALPACA_API_SECRET", "envsecret")
    creds = resolve_alpaca_credentials()
    assert creds.kind == "api_key"
    assert (creds.api_key, creds.secret_key) == ("PKENVKEY", "envsecret")
    assert creds.source == "env"
    assert creds.profile_name is None


def test_oauth_profile_is_resolved_when_env_is_empty(tmp_path, monkeypatch):
    _write_profile(tmp_path)
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    assert creds.kind == "oauth"
    assert creds.oauth_token == "tok-abcdefghijklmnopqrstuvwxyz012345"
    assert creds.profile_name == "paper"
    assert creds.api_key is None and creds.secret_key is None


def test_api_key_profile_is_resolved_as_api_key(tmp_path, monkeypatch):
    _write_profile(tmp_path, body="api_key: PKFILE\nsecret_key: filesecret\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    assert creds.kind == "api_key"
    assert (creds.api_key, creds.secret_key) == ("PKFILE", "filesecret")


def test_alpaca_profile_env_overrides_the_default_profile(tmp_path, monkeypatch):
    _write_profile(tmp_path)  # default_profile: paper
    (tmp_path / "profiles" / "other.yaml").write_text(
        "access_token: tok-other\n", encoding="utf-8")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_PROFILE", "other")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    assert (creds.profile_name, creds.oauth_token) == ("other", "tok-other")


def test_no_config_and_no_env_resolves_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path / "absent"))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert resolve_alpaca_credentials() is None


def test_partial_env_credentials_refuse_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_API_KEY", "PKONLY")
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="ALPACA_API_SECRET"):
        resolve_alpaca_credentials()


def test_malformed_profile_raises_naming_the_path(tmp_path, monkeypatch):
    _write_profile(tmp_path, body="{{{ not yaml\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="paper.yaml"):
        resolve_alpaca_credentials()


def test_a_malformed_profile_error_never_echoes_the_token(tmp_path, monkeypatch):
    # PyYAML quotes the offending source line in its own message, so a token on
    # that line reaches the error text unless the parse error is re-rendered.
    secret = "tok-abcdefghijklmnopqrstuvwxyz012345"
    _write_profile(tmp_path, body=f"access_token: {secret}: oops\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError) as caught:
        resolve_alpaca_credentials()
    assert "paper.yaml" in str(caught.value)
    assert secret not in str(caught.value)
    assert secret.removeprefix("tok-") not in str(caught.value)
    # A chained PyYAML error would print the same snippet in any traceback.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_a_live_profile_is_refused(tmp_path, monkeypatch):
    _write_profile(tmp_path, body="access_token: tok-live\nlive: true\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="paper"):
        resolve_alpaca_credentials()


def test_any_truthy_live_flag_is_refused(tmp_path, monkeypatch):
    # `live: 1` is truthy but is not the string "true"; a paper-only desk has to
    # refuse every declaration of live, not just the ones YAML types as a bool.
    _write_profile(tmp_path, body="access_token: tok-live\nlive: 1\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="paper"):
        resolve_alpaca_credentials()


def test_a_non_mapping_profile_is_refused(tmp_path, monkeypatch):
    _write_profile(tmp_path, body="- paper\n- not a mapping\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="mapping"):
        resolve_alpaca_credentials()


def test_a_non_mapping_config_is_refused(tmp_path, monkeypatch):
    # A hand-edited config.yaml that parses but is not a mapping must still be a
    # loud AlpacaAuthError, not a raw AttributeError from `.get`.
    _write_profile(tmp_path)
    (tmp_path / "config.yaml").write_text("- paper\n- other\n", encoding="utf-8")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="mapping"):
        resolve_alpaca_credentials()


def test_malformed_config_raises_naming_the_config(tmp_path, monkeypatch):
    _write_profile(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "default_profile: '{{{\n", encoding="utf-8")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError, match="config.yaml"):
        resolve_alpaca_credentials()


def test_secrets_never_appear_in_repr_or_description(tmp_path, monkeypatch):
    _write_profile(tmp_path)
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = resolve_alpaca_credentials()
    secret = "tok-abcdefghijklmnopqrstuvwxyz012345"
    assert secret not in repr(creds)
    assert secret not in str(creds)
    assert secret not in describe_credentials(creds)
    assert "paper" in describe_credentials(creds)


def test_describe_credentials_handles_absence():
    assert "alpaca profile login" in describe_credentials(None)
