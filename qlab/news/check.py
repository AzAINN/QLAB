"""Authenticate and diagnose the news integration.

A news lane that fails silently is worse than one that is absent: an empty
window is indistinguishable from a quiet market. This performs the one live
call that proves the configuration works and reports exactly what went wrong
when it does not — without ever printing a credential.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def _check_one(name: str, universe: list[str], *, lookback_hours: int,
               now: datetime, creds: dict) -> dict:
    """Diagnose one provider. Never raises; the failure IS the report."""
    from qlab.news.feed import fetch_news
    from qlab.news.grounding import ground

    report: dict = {
        "provider": name,
        "alpaca_credentials": creds["alpaca_credentials"],
        "universe": list(universe),
        "ok": False,
    }

    if name == "synthetic":
        report["error"] = (
            "provider is 'synthetic' — these are deterministic fixtures, not "
            "real headlines. Set QLAB_NEWS_PROVIDER=alpaca (or rss) in .env.")
        return report
    if name == "alpaca" and not creds["alpaca_credentials"]:
        report["error"] = (
            "provider is 'alpaca' but no credential resolves. Run "
            "`alpaca profile login` for a paper-only browser session, or put "
            "ALPACA_API_KEY and ALPACA_API_SECRET in .env at the workspace "
            "root.")
        return report

    try:
        items = fetch_news(now, universe, lookback_hours=lookback_hours,
                           provider=name, offline=False)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    grounded = ground(items, as_of=now.isoformat(), provider=name,
                      universe=universe)
    supported = grounded.corroborated_claims
    report.update({
        "ok": bool(grounded.items),
        "fetched": len(items),
        "kept": len(grounded.items),
        "claims": len(grounded.claims),
        "well_supported": len(supported),
        "primary_sources": sum(1 for c in grounded.claims if c.tier == "primary"),
        "publishers": sorted({str(getattr(i, "source", "")) for i in grounded.items}),
        "window_hash": grounded.window_hash,
        "quality_flags": list(grounded.quality_flags),
        "sample": [
            {"headline": c.headline[:110], "support": c.support,
             "tickers": list(c.tickers)}
            for c in (supported or grounded.claims)[:5]
        ],
    })
    if not grounded.items:
        report["error"] = (
            f"provider {name!r} returned no usable records for {universe}. "
            "The feed is reachable but nothing mapped to the universe.")
    return report


def check_news(universe: list[str], *, provider: str | None = None,
               lookback_hours: int = 72) -> dict:
    """Fetch one live window per stack member and report each member.

    Returns a structured diagnosis rather than raising, so a caller can render
    it. Each member is diagnosed on its own terms under ``members``: a source
    that has gone away is named, not absorbed into a smaller window. ``ok`` is
    true when ANY member fetched and grounded real records — one living member
    is still a record — and the top-level fields stay the first member's, so a
    stack of one reads exactly as a single-provider check always did.
    """
    from qlab.env import credential_status
    from qlab.news.feed import parse_provider_stack

    creds = credential_status()
    names = parse_provider_stack(provider)
    now = datetime.now(timezone.utc)
    members = {
        name: _check_one(name, universe, lookback_hours=lookback_hours,
                         now=now, creds=creds)
        for name in names
    }
    report = dict(members[names[0]])
    report["members"] = members
    report["providers"] = list(names)
    if len(names) > 1:
        report["provider"] = ",".join(names)
    report["ok"] = any(m["ok"] for m in members.values())
    return report


def render(report: dict) -> str:
    """Human-readable diagnosis. Never includes a credential.

    A stack gets one block per member. The overall line reads OK when any
    member answered, and each block still says which member did not — the
    whole point of reading several sources is knowing which one went away.
    """
    members = report.get("members") or {}
    if len(members) > 1:
        status = "OK" if report.get("ok") else "NOT WORKING"
        lines = [f"news integration: {status}",
                 f"  stack              {report.get('provider')}"]
        for member in members.values():
            lines.append("")
            lines.append(_render_member(member))
        return "\n".join(lines)
    return _render_member(report)


def _render_member(report: dict) -> str:
    """One provider's diagnosis. Never includes a credential."""
    lines = []
    status = "OK" if report.get("ok") else "NOT WORKING"
    lines.append(f"  provider           {report.get('provider')}  [{status}]")
    lines.append(
        f"  alpaca credentials {'present' if report.get('alpaca_credentials') else 'absent'}")
    if report.get("error"):
        lines.append(f"  error              {report['error']}")
    if report.get("ok"):
        lines.append(f"  fetched/kept       {report['fetched']}/{report['kept']}")
        lines.append(
            f"  claims             {report['claims']} "
            f"({report['well_supported']} well-supported, "
            f"{report['primary_sources']} primary)")
        lines.append(f"  publishers         {', '.join(report['publishers'][:6])}")
        lines.append(f"  window hash        {report['window_hash']}")
        for flag in report.get("quality_flags", []):
            lines.append(f"  ! {flag}")
        if report.get("sample"):
            lines.append("  sample:")
            for item in report["sample"]:
                lines.append(
                    f"    - {item['headline'][:78]}  ({item['support']})")
    return "\n".join(lines)
