"""Resolving Alpaca credentials from env or the Alpaca CLI's own profiles.

The second half drives the owner's two credential routes end to end: the desk
takes a login, writes it through this module (the single secrets authority) and
tests it against a local stand-in for the paper API. No test reaches Alpaca.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qlab.trader import alpaca_auth
from qlab.trader.alpaca_auth import (
    AlpacaAuthError, AlpacaCredentials, describe_credentials,
    probe_credentials, resolve_alpaca_credentials)


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


def test_a_non_utf8_profile_never_echoes_the_token(tmp_path, monkeypatch):
    # UnicodeDecodeError carries the whole decoded buffer in ``args``, so its
    # repr — and therefore any 500 body or startup traceback built from it —
    # contains the token. The read must be re-rendered, not propagated.
    secret = "tok-abcdefghijklmnopqrstuvwxyz012345"
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "default_profile: paper\n", encoding="utf-8")
    (tmp_path / "profiles" / "paper.yaml").write_bytes(
        f"access_token: {secret}\nlive: false\n".encode("utf-8") + b"\xff\xfe\n")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(AlpacaAuthError) as caught:
        resolve_alpaca_credentials()
    message = str(caught.value)
    assert "paper.yaml" in message
    assert secret not in message
    assert secret.removeprefix("tok-") not in message
    assert "UnicodeDecodeError" not in message and "utf-8" not in message
    # Assert on the decoder's own leak markers too, not only on the codec name:
    # the path in this message is a tmp dir, so "utf-8" is absent partly by
    # luck. These two strings can only come from the exception itself.
    assert "codec" not in message and "invalid start byte" not in message
    assert "0xff" not in message and "position" not in message
    # A chained decode error would carry the same buffer into any traceback.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_an_unreadable_profile_is_refused_without_leaking(tmp_path, monkeypatch):
    # A PermissionError propagating raw out of the resolver 500s the whole
    # owner snapshot route; it has to arrive as the same loud AlpacaAuthError.
    secret = "tok-abcdefghijklmnopqrstuvwxyz012345"
    _write_profile(tmp_path, body=f"access_token: {secret}\n")
    profile = tmp_path / "profiles" / "paper.yaml"
    profile.chmod(0o000)
    # Two platforms cannot honour this: root ignores the mode bits, and Windows
    # `chmod` only toggles the read-only attribute — it has no read bit to clear
    # (and its `os.access` ignores R_OK outright). Skip rather than fail there.
    if os.access(profile, os.R_OK):
        profile.chmod(0o600)
        pytest.skip("this platform cannot make a file unreadable (root, or Windows)")
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    try:
        with pytest.raises(AlpacaAuthError) as caught:
            resolve_alpaca_credentials()
    finally:
        profile.chmod(0o600)
    message = str(caught.value)
    assert "paper.yaml" in message
    assert secret not in message
    assert "PermissionError" not in message and "Errno" not in message
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


# --------------------------------------------------------------------------
# The desk takes a login: the owner's two credential routes, driven end to end.
# --------------------------------------------------------------------------

# Shaped like the real thing without being anyone's: a 20-character key id and
# a 40-character base64-ish secret (the "/" is deliberate — it is ordinary in a
# real secret and it is what a YAML dumper has to quote correctly).
_KEY = "PK7QLABTESTKEY000ZQX"
_SECRET = "qlabTESTsecret/0123456789abcdefgh+ZQXVW9"


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """An owner session whose Alpaca config dir is a scratch directory."""
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession

    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path / "alpaca"))
    session = UISession(offline_default=True, registry=Registry(":memory:"))
    yield session
    session.registry.close()


def _post(session, path, body):
    from qlab.ui.server import handle_api

    return handle_api(session, "POST", path, {}, body)


@contextlib.contextmanager
def _alpaca_stub(status: int, body: str, seen: list | None = None):
    """A local stand-in for the paper API. Never leaves this machine."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if seen is not None:
                # The Message object, not a dict: header lookup is
                # case-insensitive and urllib capitalizes what it sends.
                seen.append({"path": self.path, "headers": self.headers})
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def _silent_venue():
    """A listener that completes the handshake and then says nothing."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)  # backlog answers the SYN; nothing ever accepts
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


def test_the_desk_takes_a_login_and_the_resolver_reads_it_back(
        desk, tmp_path, monkeypatch):
    status, payload = _post(desk, "/api/alpaca/credentials",
                            {"api_key": _KEY, "api_secret": _SECRET})
    assert status == 200

    profile = tmp_path / "alpaca" / "profiles" / "paper.yaml"
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.parent.stat().st_mode) & 0o077 == 0

    # The written profile is the shape the resolver already reads.
    creds = resolve_alpaca_credentials()
    assert creds.kind == "api_key"
    assert (creds.api_key, creds.secret_key) == (_KEY, _SECRET)
    assert creds.profile_name == "paper"

    # The response is the one pair every surface reads, and it says the desk
    # can now choose the Alpaca book — without having chosen it.
    assert payload["credentials_ok"] is True
    assert "paper" in payload["credentials"]
    assert (payload["data"], payload["book"]) == ("synthetic", "simulated")

    # An env pair outranks any profile, so a login typed here would be written
    # and then never used. Refuse instead of writing a file the desk ignores.
    monkeypatch.setenv("ALPACA_API_KEY", "PKENVKEY000000000000")
    monkeypatch.setenv("ALPACA_API_SECRET", "envsecret0123456789abcdefghijklmnopqrstu")
    status, refused = _post(desk, "/api/alpaca/credentials",
                            {"api_key": _KEY, "api_secret": _SECRET})
    assert status == 400
    assert "ALPACA_API_KEY" in refused["error"]

    # Half a pair is the same refusal in a write context, and it must point the
    # operator the way a writer needs to go — the resolver's own message tells
    # a *reader* to set the missing variable, which is the opposite.
    monkeypatch.delenv("ALPACA_API_SECRET")
    status, half = _post(desk, "/api/alpaca/credentials",
                         {"api_key": _KEY, "api_secret": _SECRET})
    assert status == 400
    assert "unset both first" in half["error"]
    # "or neither…" is the resolver's own sentence, which is right at read time
    # and points the wrong way here. Its direction must not leak into a write.
    assert "or neither" not in half["error"]


def test_an_obviously_wrong_login_is_refused_and_nothing_is_written(
        desk, tmp_path, monkeypatch):
    profiles = tmp_path / "alpaca" / "profiles"
    for bad in ({"api_key": "abc", "api_secret": _SECRET},   # truncated paste
                {"api_key": _SECRET, "api_secret": _KEY},    # boxes swapped
                {"api_key": _KEY, "api_secret": ""},         # half a login
                {"api_key": _KEY, "api_secret": None}):      # no secret at all
        status, refused = _post(desk, "/api/alpaca/credentials", bad)
        assert status == 400, bad
        # The refusal never quotes what was typed.
        assert _KEY not in refused["error"] and _SECRET not in refused["error"]
    assert not profiles.exists()

    # A directory whose mode will not stick (a fixed-permission mount) must
    # refuse *before* the secret is written, not write into a readable one.
    profiles.mkdir(parents=True)
    profiles.chmod(0o755)
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    status, refused = _post(desk, "/api/alpaca/credentials",
                            {"api_key": _KEY, "api_secret": _SECRET})
    assert status == 400
    assert "private" in refused["error"]
    assert not (profiles / "paper.yaml").exists()


def test_the_probe_reports_the_account_masked_to_its_last_four(desk, monkeypatch):
    assert _post(desk, "/api/alpaca/credentials",
                 {"api_key": _KEY, "api_secret": _SECRET})[0] == 200
    seen: list = []
    body = json.dumps({"account_number": "PA3ABCDEFG7788", "status": "ACTIVE",
                       "buying_power": "200000.55", "currency": "USD"})
    with _alpaca_stub(200, body, seen) as url:
        monkeypatch.setattr(alpaca_auth, "ALPACA_PAPER_API", url)
        status, out = _post(desk, "/api/alpaca/test", {})

        # The browser-login path is a bearer token, and the desk must be able
        # to test that credential too — it is the one the module prefers.
        probe_credentials(AlpacaCredentials(
            "oauth", None, None, "tok-abcdef", "paper", "profile"))

    assert (status, out["ok"]) == (200, True)
    assert out["account_masked"].endswith("7788")
    assert "PA3ABCDEFG" not in json.dumps(out)
    assert (out["status"], out["currency"]) == ("ACTIVE", "USD")
    assert out["buying_power"] == 200000.55

    assert seen[0]["path"] == "/v2/account"
    assert seen[0]["headers"].get("APCA-API-KEY-ID") == _KEY
    assert seen[0]["headers"].get("APCA-API-SECRET-KEY") == _SECRET
    assert seen[1]["headers"].get("Authorization") == "Bearer tok-abcdef"


def test_a_login_alpaca_will_not_take_is_a_result_not_an_error(
        desk, tmp_path, monkeypatch):
    # Pressing Test before storing anything is the first thing an operator
    # does, and it reaches the venue never — absence answers itself.
    status, nothing_yet = _post(desk, "/api/alpaca/test", {})
    assert (status, nothing_yet["ok"]) == (200, False)
    assert "alpaca profile login" in nothing_yet["reason"]

    # A credential source that exists and is unusable is the same shape: the
    # route has no body to reject, so every outcome it has is a result.
    _write_profile(tmp_path / "alpaca", body="access_token: tok\nlive: true\n")
    status, broken = _post(desk, "/api/alpaca/test", {})
    assert (status, broken["ok"]) == (200, False)
    assert "paper-only" in broken["reason"]

    # replace: the profile just written is a browser login, and trading it for
    # a key pair is the one thing this route will not do unasked.
    assert _post(desk, "/api/alpaca/credentials",
                 {"api_key": _KEY, "api_secret": _SECRET,
                  "replace": True})[0] == 200
    # The shipped deadline is the contract; the cases below shorten it so the
    # suite does not wait on it.
    assert alpaca_auth.PROBE_TIMEOUT_S == 10.0

    with _alpaca_stub(401, '{"message": "forbidden."}') as url:
        monkeypatch.setattr(alpaca_auth, "ALPACA_PAPER_API", url)
        status, rejected = _post(desk, "/api/alpaca/test", {})
    assert (status, rejected["ok"]) == (200, False)
    assert "rejected by alpaca" in rejected["reason"]

    # Something that is not the paper API answering 200 on that address.
    with _alpaca_stub(200, '{"hello": "world"}') as url:
        monkeypatch.setattr(alpaca_auth, "ALPACA_PAPER_API", url)
        status, stranger = _post(desk, "/api/alpaca/test", {})
    assert (status, stranger["ok"]) == (200, False)
    assert "account number" in stranger["reason"]

    monkeypatch.setattr(alpaca_auth, "PROBE_TIMEOUT_S", 0.25)
    with _silent_venue() as url:
        monkeypatch.setattr(alpaca_auth, "ALPACA_PAPER_API", url)
        status, silent = _post(desk, "/api/alpaca/test", {})
    assert (status, silent["ok"]) == (200, False)
    assert "did not answer" in silent["reason"]


def test_a_browser_login_is_never_traded_for_a_key_pair_unasked(desk, tmp_path):
    # `alpaca profile login` writes a token that cannot be recovered without
    # logging in again, so overwriting it is data loss, not a preference.
    config = tmp_path / "alpaca"
    _write_profile(config, body=(
        "access_token: tok-abcdefghijklmnopqrstuvwxyz012345\n"
        "refresh_token: ref-abcdefghijklmnopqrstuvwxyz01234\n"
        "endpoint: https://api.alpaca.markets\n"))
    profile = config / "profiles" / "paper.yaml"
    before = profile.read_text(encoding="utf-8")

    status, refused = _post(desk, "/api/alpaca/credentials",
                            {"api_key": _KEY, "api_secret": _SECRET})
    assert status == 400
    assert "replace" in refused["error"] and "refresh token" in refused["error"]
    assert profile.read_text(encoding="utf-8") == before   # nothing touched

    # Consent, and only consent, gets past it — and takes the dead token out
    # rather than leaving an unused credential at rest.
    status, payload = _post(desk, "/api/alpaca/credentials",
                            {"api_key": _KEY, "api_secret": _SECRET,
                             "replace": True})
    assert (status, payload["credentials_ok"]) == (200, True)
    creds = resolve_alpaca_credentials()
    assert (creds.kind, creds.api_key) == ("api_key", _KEY)
    assert "access_token" not in profile.read_text(encoding="utf-8")
    # A key the CLI owns and this module has never heard of survives the write.
    assert "endpoint" in profile.read_text(encoding="utf-8")

    # An ordinary re-login needs no consent, and still preserves the rest.
    status, _ = _post(desk, "/api/alpaca/credentials",
                      {"api_key": _KEY, "api_secret": _SECRET[:-1] + "Z"})
    assert status == 200
    stored = profile.read_text(encoding="utf-8")
    assert "endpoint" in stored and stored.rstrip().endswith("Z")

    # A profile nothing can parse is the same question — what would be lost is
    # unknown — and the same way past it. A refusal with no way forward would
    # leave one corrupt file blocking every future login.
    profile.write_text("{{{ not yaml\n", encoding="utf-8")
    status, unreadable = _post(desk, "/api/alpaca/credentials",
                               {"api_key": _KEY, "api_secret": _SECRET})
    assert status == 400 and "replace" in unreadable["error"]
    assert _post(desk, "/api/alpaca/credentials",
                 {"api_key": _KEY, "api_secret": _SECRET,
                  "replace": True})[0] == 200

    # "yes" is not consent: only a real boolean is.
    status, wrong = _post(desk, "/api/alpaca/credentials",
                          {"api_key": _KEY, "api_secret": _SECRET,
                           "replace": "yes"})
    assert (status, wrong["error"]) == (400, "replace must be true or false")


def test_a_credential_that_cannot_be_sent_as_a_header_never_reaches_a_500(
        desk, tmp_path):
    # A profile written by hand (or by an older CLI) can hold a value with a
    # line break in it. http.client refuses such a header by raising a
    # ValueError that QUOTES it, which is neither a network outcome nor
    # programmer error — it went out as a 500 body with the secret in it.
    poisoned = "qlabPOISON\nsecret0123456789abcdefghijkl"
    _write_profile(tmp_path / "alpaca", body=(
        f"api_key: {_KEY}\nsecret_key: {json.dumps(poisoned)}\n"))

    status, out = _post(desk, "/api/alpaca/test", {})
    assert (status, out["ok"]) == (200, False)
    assert "header" in out["reason"]
    blob = json.dumps(out)
    assert poisoned not in blob and "qlabPOISON" not in blob


def test_the_audit_row_carries_no_key_material(desk):
    assert _post(desk, "/api/alpaca/credentials",
                 {"api_key": _KEY, "api_secret": _SECRET})[0] == 200
    rows = desk.registry.read_events(50)
    updates = [r for r in rows if r["kind"] == "alpaca.credentials_updated"]
    assert len(updates) == 1
    assert updates[0]["payload"] == {"source": "tui"}

    # Event rows replay forever, so the whole bus is checked — and not even a
    # masked fragment of either value may be on it.
    bus = json.dumps(rows, default=str)
    assert _KEY not in bus and _SECRET not in bus
    assert _KEY[-4:] not in bus and _SECRET[-4:] not in bus
