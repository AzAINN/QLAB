"""Resolve, write and test Alpaca paper credentials.

The Alpaca CLI's ``alpaca profile login`` is a browser OAuth flow that is
paper-only by construction (live requires its separate ``--api-key --live``
path), so consuming its profile is safer than asking an operator to paste keys.
An operator who has a key pair anyway can hand it to the desk, and
``write_credentials`` puts it in the same profile the CLI writes — this module
is the ONLY thing in qlab that writes a credential file, so the hygiene below
(0600, a private parent, values never echoed) has one place to live rather than
one per surface that grows a login form.

Secrets never leave this module in printable form: ``AlpacaCredentials`` has a
redacting ``repr``, ``describe_credentials`` is the only operator-facing
renderer, and no refusal here quotes the value it refused.

Reading and writing touch files only. ``probe_credentials`` is the one function
that opens a socket, and it is a single bounded GET against the paper account
endpoint — no SDK import, so the desk can answer "does this login work?"
without ``alpaca-py`` installed.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

# Paper, hard-coded, for the same reason ``AlpacaPaperBroker`` hard-codes
# ``paper=True``: nothing in qlab may address the live venue, not even to look.
ALPACA_PAPER_API = "https://paper-api.alpaca.markets"
_ACCOUNT_PATH = "/v2/account"

# A probe runs while an operator watches a spinner. Explicit because a socket
# with no deadline is how a threaded owner loses a worker thread permanently.
PROBE_TIMEOUT_S = 10.0

# Bounds on foreign text, mirroring ``operator/llm_backends.py``'s ``_head``
# (mirrored, not imported: the trader package must not depend on the LLM
# stack). Same two jobs — nothing arbitrary reaches an operator-facing string
# longer than this, and nothing reaches one carrying userinfo.
_HEAD_CHARS = 240
_MAX_BODY_BYTES = 1 << 20
_USERINFO = re.compile(r"[^\s/@]+@")

# What may be sent as an HTTP header value: printable ASCII, nothing else.
# See ``_header_safe`` — the two ways http.client refuses a header both quote
# the value they refused, so the check has to happen where the value is held.
# Matched with ``fullmatch``: ``$`` also matches *before* a trailing newline,
# so ``^…$`` passed exactly the character this gate exists to refuse.
_HEADER_SAFE = re.compile(r"[\x20-\x7e]+")


class AlpacaAuthError(RuntimeError):
    """A credential source exists but is unusable. Never raised for absence."""


class AlpacaConsentRequired(AlpacaAuthError):
    """A write refused because it would destroy a credential already stored.

    Its own class, not a sentence for a caller to sniff: a route turns this
    into a machine-readable ``confirm`` key while the message stays purely the
    human half — what would be lost. ``_HttpFault`` in ``llm_backends`` carries
    its status the same way and for the same reason: a caller that treats one
    refusal specially should read a type, not grep our own wording.
    """


@dataclass(frozen=True)
class AlpacaCredentials:
    kind: Literal["api_key", "oauth"]
    api_key: str | None
    secret_key: str | None
    oauth_token: str | None
    profile_name: str | None
    source: str

    def __repr__(self) -> str:  # never leak the secret into a traceback
        return (f"AlpacaCredentials(kind={self.kind!r}, "
                f"profile_name={self.profile_name!r}, source={self.source!r})")

    __str__ = __repr__


def _yaml_parse_error(path: Path, exc: yaml.YAMLError) -> AlpacaAuthError:
    """Report where a parse failed, never what was on the line.

    PyYAML quotes the offending source line in its own message, and in a
    profile that line can be the ``access_token``. Callers must raise the
    result with ``from None`` so the chained PyYAML message — carrying the
    same snippet — cannot reach a traceback either.
    """
    mark = getattr(exc, "problem_mark", None)
    where = ""
    if mark is not None:
        where = f" at line {mark.line + 1} column {mark.column + 1}"
    return AlpacaAuthError(f"{path} is not valid YAML{where}")


def _load_yaml_mapping(path: Path) -> dict:
    """Parse a YAML file that must hold a mapping.

    Both the config and the profile need the same three outcomes, and neither
    may reach ``.get`` on a hand-edited file that parses to a list or a scalar:
    a broken source is an ``AlpacaAuthError``, never a raw ``AttributeError``.
    An empty file is an empty mapping.

    Reading is guarded as tightly as parsing. ``UnicodeDecodeError`` carries the
    entire decoded buffer in its ``args``, so one stray byte in a profile puts
    the ``access_token`` into any repr of it — and both callers of this module
    render an unexpected exception (a 500 body on ``/api/tui``, a startup
    traceback in the terminal). The read error is therefore re-rendered from the
    path alone, and ``OSError`` joins it so an unreadable profile is the same
    loud refusal instead of a raw ``PermissionError``.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # `from None`: the chained PyYAML error carries the source snippet.
        raise _yaml_parse_error(path, exc) from None
    except (OSError, UnicodeDecodeError):
        # Neither the exception nor its args may be interpolated or chained:
        # both routes to the operator would print the buffer they hold.
        raise AlpacaAuthError(f"{path} could not be read") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise AlpacaAuthError(f"{path} does not contain a YAML mapping")
    return loaded


def _config_dir() -> Path:
    override = os.environ.get("ALPACA_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "alpaca"


def _active_profile_name(config_dir: Path) -> str:
    explicit = os.environ.get("ALPACA_PROFILE", "").strip()
    if explicit:
        return explicit
    config = config_dir / "config.yaml"
    if config.exists():
        loaded = _load_yaml_mapping(config)
        name = str(loaded.get("default_profile") or "").strip()
        if name:
            return name
    return "paper"


def refuse_partial_env_credentials() -> None:
    """Refuse a half-set ``ALPACA_API_KEY``/``ALPACA_API_SECRET`` pair.

    Partial credentials signal intent with a broken setup: refuse rather than
    fall through to a profile — or a book — the operator did not ask for.
    Exposed on its own because a caller that needs no credential at all (the
    explicitly simulated book) must still make the misconfiguration loud
    instead of stepping over it.
    """
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET", "").strip()
    if bool(key) != bool(secret):
        missing = "ALPACA_API_SECRET" if key else "ALPACA_API_KEY"
        raise AlpacaAuthError(
            f"{missing} is not set; set both ALPACA_API_KEY and "
            "ALPACA_API_SECRET, or neither to use your `alpaca profile login` "
            "session instead")


def resolve_alpaca_credentials() -> AlpacaCredentials | None:
    """Env credentials, else the active CLI profile, else None."""
    refuse_partial_env_credentials()
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_API_SECRET", "").strip()
    if key and secret:
        return AlpacaCredentials("api_key", key, secret, None, None, "env")

    config_dir = _config_dir()
    name = _active_profile_name(config_dir)
    path = config_dir / "profiles" / f"{name}.yaml"
    if not path.exists():
        return None
    profile = _load_yaml_mapping(path)
    # Any truthy value refuses: `live: 1` and `live: true` both declare live, and
    # ambiguity in a paper-only desk has to resolve toward refusing.
    if bool(profile.get("live")):
        raise AlpacaAuthError(
            f"profile {name!r} at {path} is a live-trading profile; qlab is "
            "paper-only. Use a paper profile (`alpaca profile login`).")

    file_key = str(profile.get("api_key") or "").strip()
    file_secret = str(profile.get("secret_key") or "").strip()
    if file_key and file_secret:
        return AlpacaCredentials(
            "api_key", file_key, file_secret, None, name, str(path))
    token = str(profile.get("access_token") or "").strip()
    if token:
        return AlpacaCredentials("oauth", None, None, token, name, str(path))
    return None


# ---------------------------------------------------------------------------
# Writing a login the desk was handed
# ---------------------------------------------------------------------------

# Deliberately loose. Alpaca key ids are 20 characters of upper-case letters
# and digits and secrets are ~40 base64-ish ones, but pinning those exactly
# would refuse a valid credential the day Alpaca lengthens either — and the two
# costs are not symmetric: a wrongly-refused key is recoverable in one sentence
# ("use `alpaca profile login`"), while a wrongly-*accepted* one is a file that
# looks like a login and fails at the venue with a 401 the operator has to
# decode. So these catch the mistakes that actually happen — a truncated paste,
# a placeholder, the two boxes filled the wrong way round (a secret carries
# "/" or "+", which the key pattern refuses; a key is shorter than the secret
# floor) — and let anything plausible through to the venue, which is the real
# authority on whether a credential works.
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9]{16,64}$")
_SECRET_SHAPE = re.compile(r"^[A-Za-z0-9+/=_-]{24,128}$")


def _clean(value: object, label: str) -> str:
    """One typed-in field, stripped — or a refusal that never quotes it."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AlpacaAuthError(f"the {label} is required")
    if not isinstance(value, str):
        raise AlpacaAuthError(f"the {label} must be text")
    return value.strip()


def _ensure_private_dir(path: Path) -> None:
    """Make a directory that only its owner can enter, or refuse to use it.

    Checked after the chmod rather than assumed from it: a filesystem that does
    not carry POSIX modes (a mounted share, a container volume with fixed
    permissions) accepts the call and keeps the old mode, and writing a secret
    into a world-readable directory because the syscall returned zero is
    exactly the silent fallback this codebase refuses.

    On Windows the POSIX bits are decorative (chmod toggles read-only and
    ``st_mode`` reads back 0o777-ish regardless of the real ACL), so the check
    above would refuse every profile and the desk could never store a login.
    The honest equivalent there is inheritance: a directory under the user's
    profile inherits %USERPROFILE%'s owner-only ACL. That containment is what
    gets checked — a config dir pointed OUTSIDE the profile (a share, a
    world-readable data drive) is refused loudly rather than trusted.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            resolved = path.resolve()
            home = Path.home().resolve()
            if not resolved.is_relative_to(home):
                raise AlpacaAuthError(
                    f"{path} is outside your user profile; on Windows a "
                    "credential directory must live under it to inherit a "
                    "private ACL. Set ALPACA_CONFIG_DIR to a folder inside "
                    f"{home}")
            return
        os.chmod(path, 0o700)
        mode = stat.S_IMODE(path.stat().st_mode)
    except AlpacaAuthError:
        raise
    except OSError:
        # Re-rendered from the path: an OSError's own message is not worth the
        # risk of interpolating something that carries more than it should.
        raise AlpacaAuthError(f"{path} could not be created") from None
    if mode & 0o077:
        raise AlpacaAuthError(
            f"{path} cannot be made private (its mode is {mode:04o}); refusing "
            "to write a credential where others can read it")


def write_credentials(api_key: str, api_secret: str,
                      *, replace: bool = False) -> None:
    """Store an API key pair as the profile ``resolve_alpaca_credentials`` reads.

    The active profile, not a private one of qlab's own: that path is the one
    thing the resolver looks at, so writing anywhere else would mean also
    rewriting ``config.yaml``'s ``default_profile`` — a second file, owned by
    the CLI. This writes the same profile ``alpaca profile login`` writes, and
    marks it ``live: false`` because a key pair typed into a paper-only desk is
    a paper credential by declaration.

    ``replace`` is the operator's consent to destroy a login that is already
    there. Without it a browser-authorized profile is never overwritten — its
    ``access_token`` and ``refresh_token`` cannot be recovered without logging
    in again, so silently trading them for a pasted key pair is a data loss the
    desk has no business performing on its own. The guard lives here rather
    than at the route because every future caller would otherwise have to
    re-acquire it.

    Refuses rather than writes when the result would be ignored or unsafe: env
    credentials outrank every profile, a directory that cannot be made private
    is not a place for a secret, and a value that cannot be a credential is a
    typo. Nothing reaches disk in any of those cases, and no refusal quotes
    what it refused.
    """
    key = _clean(api_key, "API key")
    secret = _clean(api_secret, "API secret")
    if not _KEY_SHAPE.match(key):
        raise AlpacaAuthError(
            "that does not look like an Alpaca API key id (about 20 letters "
            "and digits, e.g. PK…); nothing was written")
    if not _SECRET_SHAPE.match(secret):
        raise AlpacaAuthError(
            "that does not look like an Alpaca API secret (about 40 letters, "
            "digits and +/= characters); nothing was written")

    # Either half is disqualifying: a full env pair silently wins over anything
    # written here, and a half pair makes resolution refuse outright — so a
    # login stored now would never be used either way. Said in the *write*
    # direction deliberately: `refuse_partial_env_credentials` tells a reader
    # to set the missing variable, which is the right advice when resolving and
    # exactly the wrong advice to give someone storing a profile.
    if (os.environ.get("ALPACA_API_KEY", "").strip()
            or os.environ.get("ALPACA_API_SECRET", "").strip()):
        raise AlpacaAuthError(
            "ALPACA_API_KEY / ALPACA_API_SECRET are set in this desk's "
            "environment and take precedence over any profile, so a login "
            "stored here would never be used; unset both first")

    config_dir = _config_dir()
    name = _active_profile_name(config_dir)
    profiles = config_dir / "profiles"
    _ensure_private_dir(profiles)
    path = profiles / f"{name}.yaml"

    existing: dict = {}
    if path.exists():
        try:
            existing = _load_yaml_mapping(path)
        except AlpacaAuthError:
            # Unreadable is treated exactly like a browser login: there may be
            # a credential in there that the operator wants back, and the same
            # explicit consent is the way past it. Refusing outright would
            # leave a desk with one corrupt profile unable to store a login at
            # all — a refusal with no way forward is not a refusal, it is a
            # dead end.
            if not replace:
                raise AlpacaConsentRequired(
                    f"{path} could not be read, so what a login stored here "
                    "would overwrite is unknown") from None
            existing = {}
    # "Holds a token", not "holds a token and no key pair". Every write here
    # ends in an api_key profile and takes the tokens out (below), so a file
    # carrying both shapes loses them too — and it was reaching that outcome
    # without being asked, which is the case this refusal exists for.
    if not replace and (existing.get("access_token")
                        or existing.get("refresh_token")):
        raise AlpacaConsentRequired(
            f"profile {name!r} holds a browser login (`alpaca profile login`); "
            "storing a key pair here discards its access token and refresh "
            "token, and they cannot be recovered without logging in again")

    # Merged, not rewritten: the CLI owns this file too and puts keys in it
    # that this module has never heard of (endpoint, scopes, output). Dropping
    # them because we do not read them would break the operator's `alpaca` CLI
    # as a side effect of storing a login in qlab.
    merged = dict(existing)
    # The tokens go out with the login they belong to — keyed on what is being
    # superseded, not on the flag. `resolve_alpaca_credentials` prefers the key
    # pair in the same file, so a surviving token is a credential at rest that
    # nothing will ever use: the refresh token in particular still mints new
    # access tokens for anyone who reads the file. Leaving them behind is the
    # dead-credential hazard the refusal above exists to prevent, so the
    # cleanup has to be unconditional or the shape that skips the refusal
    # skips the cleanup too.
    merged.pop("access_token", None)
    merged.pop("refresh_token", None)
    merged.update({"api_key": key, "secret_key": secret, "live": False})

    # Dumped rather than formatted: a secret containing ":" or starting with a
    # YAML sigil has to come back out of the file byte-identical, and the
    # dumper is what knows when to quote.
    document = yaml.safe_dump(
        merged, sort_keys=True, default_flow_style=False)

    # Written to a temp file in the same directory and moved into place, so the
    # destination is never an existing file whose looser mode we would inherit
    # (O_CREAT does not change the mode of a file that already exists) and
    # never a half-written profile the resolver could read.
    tmp = profiles / f".{name}.yaml.qlab-tmp"
    try:
        # O_EXCL keeps this from following a symlink someone left here; the
        # unlink first is what stops a crashed earlier write from making the
        # desk permanently unable to store a login.
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise AlpacaAuthError(f"{path} could not be written") from None
    written = False
    try:
        # POSIX only: on Windows st_mode is decorative and fchmod cannot make
        # the file private — the containment check in _ensure_private_dir is
        # the ACL story there, so a decorative 0o666 read-back must not refuse
        # a write the directory already guards.
        if os.name != "nt":
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode != 0o600:
                # A umask can only clear bits, so this is the unusual
                # filesystem again. Fix it and re-read: the secret is written
                # only once the file it goes into is private, never before.
                os.fchmod(fd, 0o600)
                mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode != 0o600:
                raise AlpacaAuthError(
                    f"{tmp} cannot be made private (its mode is {mode:04o}); "
                    "refusing to write a credential to it")
        # Buffered writer rather than ``os.write``: the raw call may write
        # fewer bytes than it was given, and a truncated profile is the worst
        # possible outcome here — it still parses as a mapping, just one whose
        # secret_key is missing, which the resolver reports as "no credentials"
        # rather than as damage.
        with open(fd, "wb", closefd=False) as handle:
            handle.write(document.encode("utf-8"))
        written = True
    except OSError:
        raise AlpacaAuthError(f"{path} could not be written") from None
    finally:
        os.close(fd)
        if not written:
            # Nothing half-written survives a refusal, on any exit from here.
            tmp.unlink(missing_ok=True)
    try:
        from qlab.paths import replace_file
        replace_file(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise AlpacaAuthError(f"{path} could not be written") from None

    if os.name != "nt":
        final = stat.S_IMODE(path.stat().st_mode)
        if final != 0o600:
            raise AlpacaAuthError(
                f"{path} is mode {final:04o} after writing, not 0600 — this "
                "filesystem cannot keep a credential private")


# ---------------------------------------------------------------------------
# Testing a login against the venue
# ---------------------------------------------------------------------------

def _head(raw: bytes | str) -> str:
    """A bounded, single-line, redacted excerpt of what the venue said back.

    The one gate for foreign text here, for the reason ``llm_backends`` learned
    over four rounds: deciding per call site whether a particular exception or
    body can carry a credential is the thing that keeps being wrong. Collapse
    first so a control character cannot hide a credential from the scan, redact
    second, bound last so a cut can never expose half of one.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = _USERINFO.sub("…@", " ".join(text.split()))
    return text[:_HEAD_CHARS] + "…" if len(text) > _HEAD_CHARS else text


@dataclass(frozen=True)
class ProbeResult:
    """What one round trip to the paper account endpoint established.

    ``ok`` answers the only question the probe was asked — do these credentials
    work — and the account fields are what it can show alongside. A network
    outcome is never an exception: an operator testing a login is entitled to a
    sentence, not a traceback.
    """

    ok: bool
    reason: str | None = None
    account_masked: str | None = None
    status: str | None = None
    buying_power: float | None = None
    currency: str | None = None

    def to_dict(self) -> dict:
        if not self.ok:
            return {"ok": False, "reason": self.reason}
        return {"ok": True, "account_masked": self.account_masked,
                "status": self.status, "buying_power": self.buying_power,
                "currency": self.currency}


def _header_safe(value: str, label: str) -> str:
    """A credential that can be sent as a header, or a refusal that never quotes it.

    ``http.client.putheader`` refuses a value containing a line break and then
    encodes what is left as latin-1 — and **both** refusals put the value in
    the exception: ``ValueError("Invalid header value %r")`` and
    ``UnicodeEncodeError``, whose ``object`` is the whole header. Both are
    ``ValueError``s, which this module reserves for programmer error, so
    neither was caught by the probe and both reached do_POST's blanket
    ``500 {"error": repr(exc)}`` with the secret inside it.

    Checked here, before the socket, because this is the module that holds the
    value: the only thing said out loud is which field was unusable. Printable
    ASCII is the whole permitted set — Alpaca key ids, secrets and OAuth
    bearer tokens are all base64-ish by construction, so anything else is a
    damaged credential source rather than an exotic valid one.
    """
    if not _HEADER_SAFE.fullmatch(value):
        raise AlpacaAuthError(
            f"the resolved {label} contains characters that cannot be sent in "
            "an HTTP header (a line break, a tab, or a non-ASCII character) — "
            "the credential source needs fixing, not the desk")
    return value


def _auth_headers(creds: AlpacaCredentials) -> dict[str, str]:
    if creds.kind == "oauth":
        # The browser-login credential, which is the one this module prefers;
        # a desk that could only test pasted keys would be unable to test the
        # login it recommends.
        return {"Authorization":
                f"Bearer {_header_safe(creds.oauth_token or '', 'OAuth token')}"}
    return {"APCA-API-KEY-ID": _header_safe(creds.api_key or "", "API key"),
            "APCA-API-SECRET-KEY": _header_safe(
                creds.secret_key or "", "API secret")}


def _mask_account(number: str) -> str:
    """The last four digits only — enough to recognise, never enough to use."""
    return "…" + number[-4:]


def probe_credentials(creds: AlpacaCredentials | None) -> ProbeResult:
    """One GET against the paper account endpoint. Never raises for a network outcome.

    Absence, a rejection, a silent venue and something-else-answering are all
    results with a sentence. The only exceptions that leave here are programmer
    errors — a credential object that carries neither a key pair nor a token.
    """
    if creds is None:
        return ProbeResult(False, describe_credentials(None))
    if creds.kind == "oauth":
        if not creds.oauth_token:
            raise ValueError("oauth credentials carry no token")
    elif not (creds.api_key and creds.secret_key):
        raise ValueError("api_key credentials carry no key pair")

    try:
        headers = _auth_headers(creds)
    except AlpacaAuthError as exc:
        # A credential this module cannot even send is a result, like absence
        # or a rejection — the operator asked "does my login work?" and the
        # answer is no, with a sentence. Caught here and nowhere wider: the
        # ValueErrors above are programmer error and must still escape.
        return ProbeResult(False, str(exc))

    request = urllib.request.Request(
        ALPACA_PAPER_API.rstrip("/") + _ACCOUNT_PATH,
        headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_S) as response:
            raw = response.read(_MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # The one answer the operator most needs in their own words: the
            # venue read the credential and would not have it.
            return ProbeResult(False, "rejected by alpaca — that key and "
                                      "secret are not a valid paper login")
        detail = _head(exc.read(_HEAD_CHARS + 1) or b"")
        return ProbeResult(False, _head(
            f"alpaca answered HTTP {exc.code}: {detail}"))
    except TimeoutError:
        return ProbeResult(False, f"alpaca did not answer in {PROBE_TIMEOUT_S:g}s")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return ProbeResult(
                False, f"alpaca did not answer in {PROBE_TIMEOUT_S:g}s")
        return ProbeResult(False, _head(
            f"alpaca could not be reached at {ALPACA_PAPER_API}: {exc.reason}"))
    except (OSError, http.client.HTTPException) as exc:
        # An OSError here is a refused connection or DNS; an HTTPException is a
        # URL that parses and still cannot be used. Neither is an OSError of
        # the other's kind, and both mean the same thing to an operator.
        return ProbeResult(False, _head(
            f"alpaca could not be reached at {ALPACA_PAPER_API}: {exc}"))

    if len(raw) > _MAX_BODY_BYTES:
        return ProbeResult(False, "alpaca answered with more than "
                                  f"{_MAX_BODY_BYTES} bytes; that is not an account")
    try:
        account = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ProbeResult(False, "alpaca answered with a body that is not JSON")
    number = (account.get("account_number") if isinstance(account, dict) else None)
    if not isinstance(number, str) or not number:
        # A 200 with no account in it is not a working login; it is something
        # else on that address (a captive portal, a proxy) and saying "ok"
        # would be the desk vouching for a credential nobody checked.
        return ProbeResult(False, _head(
            f"something other than the alpaca paper API answered at "
            f"{ALPACA_PAPER_API} — no account number in its reply"))
    return ProbeResult(
        True,
        account_masked=_mask_account(_head(number)),
        status=_head(str(account.get("status") or "")) or None,
        buying_power=_as_float(account.get("buying_power")),
        currency=_head(str(account.get("currency") or "")) or None)


def _as_float(value: object) -> float | None:
    """A venue-supplied number, or absent. Never a guess.

    Alpaca sends these as strings. An unreadable one is reported as missing
    rather than as zero, and never withdraws the ``ok`` the 200 established:
    the question was whether the credentials work, and they did.
    """
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def describe_credentials(creds: AlpacaCredentials | None) -> str:
    """One operator-facing line. Never includes the secret."""
    if creds is None:
        return ("no Alpaca credentials found — run `alpaca profile login` "
                "to authorize a paper account in your browser")
    if creds.source == "env":
        return "Alpaca API key from the environment"
    kind = "browser login" if creds.kind == "oauth" else "API key"
    return f"Alpaca {kind} from profile {creds.profile_name!r}"
