"""Authenticate and diagnose the news integration.

A news lane that fails silently is worse than one that is absent: an empty
window is indistinguishable from a quiet market. This performs the one live
call that proves the configuration works and reports exactly what went wrong
when it does not — without ever printing a credential.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def check_news(universe: list[str], *, provider: str | None = None,
               lookback_hours: int = 72) -> dict:
    """Fetch one live window and report what the configuration actually does.

    Returns a structured diagnosis rather than raising, so a caller can render
    it. ``ok`` is true only when real records were fetched AND grounded.
    """
    from qlab.env import credential_status
    from qlab.news.feed import fetch_news
    from qlab.news.grounding import ground

    creds = credential_status()
    name = (provider or os.environ.get("QLAB_NEWS_PROVIDER")
            or "synthetic").strip().lower()
    now = datetime.now(timezone.utc)
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


def render(report: dict) -> str:
    """Human-readable diagnosis. Never includes a credential."""
    lines = []
    status = "OK" if report.get("ok") else "NOT WORKING"
    lines.append(f"news integration: {status}")
    lines.append(f"  provider           {report.get('provider')}")
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
