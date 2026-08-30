"""Choosing what the desk reads, as a conversation rather than a variable.

The stack (``QLAB_NEWS_PROVIDERS``) and the SEC contact
(``QLAB_EDGAR_CONTACT``) were env vars an operator discovered by reading docs
or by hitting a refusal. This is the guided path to the same two lines: which
sources, what each one costs, whether the operator wants real news at all —
and the one question EDGAR cannot work without.

The logic lives here, with the prompts injected, so the whole wizard is
testable without a terminal and the CLI verb and the startup door can share
one code path. Nothing here writes anything until :func:`apply_plan` is
called, and the only values it ever writes or echoes are the two names above:
no other ``.env`` line is read, printed, or reordered.
"""

from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Sequence

CONTACT_SHAPE = "Your Name <you@example.org>"
_MAX_CONTACT = 200
_ASK_TRIES = 3


class SetupRefused(RuntimeError):
    """The operator could not give an answer the desk can act on."""


@dataclass(frozen=True)
class SourceChoice:
    """One source the operator may take, and what taking it costs."""

    name: str
    tier: str            # "primary" | "secondary" | "fixtures"
    needs: str           # "" | "QLAB_EDGAR_CONTACT" | "an Alpaca credential"
    cost: str
    available: bool
    default: bool


@dataclass(frozen=True)
class SetupPlan:
    """What the operator chose. Nothing is written until it is applied."""

    read_news: bool
    providers: tuple[str, ...]
    edgar_contact: str | None
    verify: bool


# Order is the order the wizard asks in, and the order a chosen stack is
# written in. `synthetic` is deliberately absent: it is the answer to "read
# real news at all?", not a source to add beside real ones.
_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("alpaca", "secondary", "an Alpaca credential",
     "symbol-tagged wire copy; needs a sign-in, no per-request cost"),
    ("edgar", "primary", "QLAB_EDGAR_CONTACT",
     "SEC filings, dated by acceptance; one request per issuer, rate-limited "
     "to the SEC's 10/second"),
    ("macro", "primary", "", ""),   # cost derived: see _macro_cost
    ("rss", "secondary", "",
     "public feeds keyword-matched to the universe; cheap, and single-source"),
    ("gdelt", "secondary", "",
     "many publishers, one article per domain — but measured at 43s and past "
     "75s per request on 2026-08-28, and a stack member's fetch runs on the "
     "owner heartbeat"),
)


def _macro_cost() -> str:
    """The macro line, named from the feeds that actually ship.

    Hardcoding the publishers meant offering BLS and Treasury for weeks after
    both were removed from the config (403 and 404 on 2026-08-28). Deriving it
    cannot go stale.
    """
    from qlab.news.feed import load_news_sources

    try:
        feeds = (load_news_sources().get("macro") or {}).get("feeds") or []
        names = [str(f.get("name")) for f in feeds if f.get("name")]
    except Exception:
        # A config this process cannot read is the feed's error to raise where
        # it is actionable, not a reason the wizard cannot describe the source.
        names = []
    if not names:
        return "official releases; the configured feed list is empty, no key"
    return f"official releases — {', '.join(names)}; no key"


def _alpaca_resolves(env: Mapping[str, str]) -> bool:
    """Whether a credential is available now: env pair, then a CLI profile."""
    if env.get("ALPACA_API_KEY", "").strip() and env.get(
            "ALPACA_API_SECRET", "").strip():
        return True
    try:
        from qlab.trader.alpaca_auth import resolve_alpaca_credentials

        return resolve_alpaca_credentials() is not None
    except Exception:
        # A broken or absent profile is simply "no credential" here; the
        # resolver reports the detail where it is actionable.
        return False


def catalog(env: Mapping[str, str]) -> list[SourceChoice]:
    """The sources on offer, in ask order, with what resolves right now.

    Defaults are what a desk should read unasked: both primary sources, plus
    Alpaca when a credential already resolves. `rss` is single-source and
    `gdelt` is slow, so neither is preselected.
    """
    alpaca = _alpaca_resolves(env)
    out: list[SourceChoice] = []
    for name, tier, needs, cost in _SOURCES:
        if name == "alpaca":
            available = alpaca
            default = alpaca
        elif name == "edgar":
            available = bool(env.get("QLAB_EDGAR_CONTACT", "").strip())
            default = True
        else:
            available = True
            default = name == "macro"
        out.append(SourceChoice(
            name=name, tier=tier, needs=needs,
            cost=_macro_cost() if name == "macro" else cost,
            available=available, default=default))
    return out


def validate_contact(value: str) -> str:
    """Normalise a contact to ``Name <email>``; refuse what the SEC would not.

    A bare email is kept bare rather than given an invented name — the SEC
    wants a way to reach a human, and this desk does not fabricate one.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"the EDGAR contact is required, as {CONTACT_SHAPE}")
    if "'" in raw or '"' in raw:
        raise ValueError(
            f"a quote in {raw!r} cannot be written to .env, which has no "
            f"escape syntax here; the shape is {CONTACT_SHAPE}")
    if len(raw) > _MAX_CONTACT:
        raise ValueError(
            f"the EDGAR contact is longer than {_MAX_CONTACT} characters; "
            f"it goes in a User-Agent, as {CONTACT_SHAPE}")
    if raw.endswith(">") and "<" in raw:
        name, _, rest = raw.partition("<")
        email = rest[:-1].strip()
        name = name.strip()
    else:
        parts = raw.split()
        emails = [p for p in parts if "@" in p]
        if len(emails) != 1:
            raise ValueError(
                f"{raw!r} is not a contact the SEC can use; give one email, "
                f"as {CONTACT_SHAPE}")
        email = emails[0]
        name = " ".join(parts[:parts.index(email)]
                        + parts[parts.index(email) + 1:]).strip()
    local, _, domain = email.partition("@")
    if not local or not domain or " " in email:
        raise ValueError(
            f"{raw!r} has no usable email address; the shape is {CONTACT_SHAPE}")
    return f"{name} <{email}>" if name else email


def _ask_bool(ask: Callable[[str], str], question: str, default: bool) -> bool:
    """Ask a yes/no question. An unparseable answer is re-asked, never guessed."""
    suffix = "[Y/n]" if default else "[y/N]"
    for _ in range(_ASK_TRIES):
        answer = (ask(f"{question} {suffix} ") or "").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "true"):
            return True
        if answer in ("n", "no", "false"):
            return False
    raise SetupRefused(
        f"{question!r} needs a yes or a no; nothing was changed.")


def ask_contact(ask: Callable[[str], str], say: Callable[[str], None]) -> str:
    """Collect the EDGAR contact, saying first where the string goes."""
    say("  The SEC asks that automated requests identify a contact in the")
    say("  User-Agent. qlab sends this string to the SEC and nowhere else.")
    last = ""
    for _ in range(_ASK_TRIES):
        try:
            return validate_contact(ask(f"  Contact ({CONTACT_SHAPE}): "))
        except ValueError as exc:
            last = str(exc)
            say(f"  {last}")
    raise SetupRefused(
        f"no usable EDGAR contact after {_ASK_TRIES} tries ({last}); "
        f"the shape is {CONTACT_SHAPE}")


def run_wizard(*, ask: Callable[[str], str], env: Mapping[str, str],
               say: Callable[[str], None]) -> SetupPlan:
    """Walk the operator through the choice and return it. Writes nothing."""
    say("")
    say("  Which sources should this desk read? Nothing is saved until the end.")
    if not _ask_bool(ask, "  Read real news on the live desk?", True):
        say("  The desk will read deterministic fixtures and label its")
        say("  narrative 'synthetic (demo)' everywhere it appears.")
        return SetupPlan(read_news=False, providers=("synthetic",),
                         edgar_contact=None, verify=False)

    chosen: list[str] = []
    for choice in catalog(env):
        say("")
        state = "ready" if choice.available else (
            f"needs {choice.needs}" if choice.needs else "ready")
        say(f"  {choice.name}  ({choice.tier}, {state})")
        say(f"    {choice.cost}")
        if not _ask_bool(ask, f"  Read {choice.name}?", choice.default):
            continue
        if choice.name == "alpaca" and not choice.available:
            # Fail loud: a source that cannot answer is refused with the fix,
            # never carried into the stack to die on the first heartbeat.
            say("  Refused: no Alpaca credential resolves. Run "
                "`alpaca profile login` for a")
            say("  paper-only browser session, or put ALPACA_API_KEY and "
                "ALPACA_API_SECRET")
            say("  in .env at the workspace root, then run this again.")
            continue
        chosen.append(choice.name)

    if not chosen:
        say("")
        say("  No real source chosen — the desk will read deterministic")
        say("  fixtures, labelled 'synthetic (demo)'.")
        return SetupPlan(read_news=False, providers=("synthetic",),
                         edgar_contact=None, verify=False)

    contact: str | None = None
    if "edgar" in chosen:
        say("")
        existing = (env.get("QLAB_EDGAR_CONTACT") or "").strip()
        if existing and _ask_bool(
                ask, "  Keep the EDGAR contact already on file?", True):
            contact = None
        else:
            contact = ask_contact(ask, say)

    if "gdelt" in chosen:
        say("")
        say("  gdelt measured 43s and past 75s for one request on 2026-08-28,")
        say("  and its fetch runs on the owner's heartbeat: ticks can take "
            "minutes.")
        if not _ask_bool(ask, "  Keep gdelt in the stack?", False):
            chosen.remove("gdelt")

    say("")
    question = "  Check the chosen sources live before saving?"
    if "gdelt" in chosen:
        # The check pays the same latency the fetch does; a default-yes
        # question that hides a minute-long wait is a surprise, not a default.
        question += " (this includes gdelt: expect a minute or more)"
    verify = _ask_bool(ask, question, True)
    return SetupPlan(read_news=True, providers=tuple(chosen),
                     edgar_contact=contact, verify=verify)


def _env_line(name: str, value: str, *, export: bool) -> str:
    """One ``.env`` line both readers agree on: this parser, and ``sh``.

    Single quotes, because `$`, a backtick and `"` are all literal inside them
    and :func:`qlab.env.parse_env` strips exactly one surrounding pair. A value
    containing a single quote has no spelling both readers would agree on —
    ``.env`` here has no escape syntax — so it is refused rather than written
    as something one of them would misread.
    """
    if "'" in value:
        raise ValueError(
            f"{name} cannot hold a single quote: .env has no escape syntax "
            "here, and a quoted value would be read back wrong")
    quoted = value if value and not any(
        c in value for c in " \t\"#<>$`\\") else f"'{value}'"
    return f"{'export ' if export else ''}{name}={quoted}"


def apply_plan(plan: SetupPlan, *, root: Path,
               environ: MutableMapping[str, str]) -> list[str]:
    """Write the choice to ``root/.env`` and into ``environ``; return the names.

    Exactly two names are ever touched. Every other line survives byte for
    byte, in place, including an ``export`` prefix on a line being replaced —
    a rewrite that reformatted an operator's file would be this desk editing
    configuration it does not own.
    """
    values: list[tuple[str, str]] = [
        ("QLAB_NEWS_PROVIDERS", ",".join(plan.providers))]
    if plan.edgar_contact:
        values.append(("QLAB_EDGAR_CONTACT", plan.edgar_contact))
    return write_env_values(values, root=root, environ=environ)


def apply_contact(contact: str, *, root: Path,
                  environ: MutableMapping[str, str]) -> list[str]:
    """Persist only the EDGAR contact.

    The startup door's ``enter`` answer settles what EDGAR needs and nothing
    else: the stack the operator already configured is not this door's to
    rewrite.
    """
    return write_env_values(
        [("QLAB_EDGAR_CONTACT", validate_contact(contact))],
        root=root, environ=environ)


def write_env_values(values: Sequence[tuple[str, str]], *, root: Path,
                     environ: MutableMapping[str, str]) -> list[str]:
    """Set ``values`` in ``root/.env`` and ``environ``; return the names."""
    target = root / ".env"
    try:
        text = target.read_text(encoding="utf-8", newline="")
    except FileNotFoundError:
        text = ""
    # `splitlines` is the wrong tool for a file promised back byte for byte: it
    # also breaks on \x0b, \x0c and \x85, and drops the \r of a CRLF file. Each
    # line keeps its own terminator, so a mostly-LF file with one CRLF line is
    # still read line by line rather than fused between the two CRLFs.
    parts = re.split(r"(\r\n|\n|\r)", text) if text else []
    rows = [(parts[i], parts[i + 1] if i + 1 < len(parts) else "")
            for i in range(0, len(parts), 2)]
    if rows and rows[-1] == ("", ""):
        rows.pop()  # the file ended with a terminator, not with an empty line
    terminators = Counter(sep for _, sep in rows if sep)
    newline = terminators.most_common(1)[0][0] if terminators else "\n"
    ends_with_newline = bool(rows and rows[-1][1]) or not text

    def names_line(raw: str) -> str:
        stripped = raw.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        return stripped.split("=", 1)[0].strip() if "=" in stripped else ""

    for name, value in values:
        line = None
        kept: list[tuple[str, str]] = []
        for raw, sep in rows:
            if names_line(raw) != name:
                kept.append((raw, sep))
                continue
            if line is None:
                # The first occurrence keeps its place, its `export` and its
                # own terminator.
                line = _env_line(name, value,
                                 export=raw.strip().startswith("export "))
                kept.append((line, sep))
            # Every later copy is dropped: `parse_env` is last-wins, so a
            # surviving stale duplicate would beat the line just written.
        rows = kept
        if line is None:
            if rows and not rows[-1][1]:
                rows[-1] = (rows[-1][0], newline)
            rows.append((_env_line(name, value, export=False),
                         newline if ends_with_newline else ""))
        environ[name] = value

    root.mkdir(parents=True, exist_ok=True)
    rendered = "".join(raw + sep for raw, sep in rows)
    # Atomic: a half-written .env is an operator's whole configuration. And
    # the file's own mode survives the swap — a 0600 .env holds API keys, and
    # a fresh tmp file would otherwise hand it the umask's 0644.
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(rendered, encoding="utf-8", newline="")
    if target.exists():
        shutil.copymode(target, tmp)
    os.replace(tmp, target)
    return [name for name, _ in values]


def verify_plan(plan: SetupPlan, universe: Sequence[str], *,
                environ: MutableMapping[str, str] | None = None) -> dict:
    """One live check of the chosen stack, with the contact exported first.

    The contact has to be in the environment the provider reads or EDGAR
    refuses on its own terms and the check would report a configuration the
    operator did not choose.
    """
    from qlab.news import check as news_check

    target = os.environ if environ is None else environ
    if plan.edgar_contact:
        target["QLAB_EDGAR_CONTACT"] = plan.edgar_contact
    return news_check.check_news(list(universe),
                                 provider=",".join(plan.providers))


def failed_members(report: Mapping) -> list[str]:
    """Which stack members did not answer, in the order the report holds them."""
    members = report.get("members") or {}
    return [name for name, member in members.items() if not member.get("ok")]
