"""The one provenance check every risk view passes, whoever built it.

Two callers, one rule. ``research.apply_views`` runs it over an extractor's
views against the operator's excerpt and the archive's claim keys; the
ablation's A5 arm runs it over its own rule-built views against the claim keys
of the matrix it just logged. A second implementation would be a second
definition of "grounded", and the arm would then be asserting its provenance
rather than deriving it — which is exactly the bug this module exists to close.
"""

from __future__ import annotations


def verify_view_provenance(
    canonical_views: list[dict],
    excerpt: str,
    claim_keys: set[str] | None = None,
) -> bool:
    """Every view must be grounded — in the operator's text, or in the archive.

    A view carrying ``source_quote`` is checked against ``excerpt``. A view
    carrying ``source_claims`` is checked against ``claim_keys``, the claim
    keys of the qualitative matrix it was counted from: a rule-built view cites
    the records behind it rather than pasting one of them.

    Returns False (unverified, but permitted) when the ground truth is absent —
    no excerpt supplied, or no matrix logged — so the audit trail is explicit;
    raises when the ground truth exists and a view does not match it, so a
    fabricated quote or an invented claim key cannot reach the analyst.
    """
    normalized_excerpt = " ".join(excerpt.split()).lower()
    known = {str(key) for key in (claim_keys or ())}
    verified = True
    for index, view in enumerate(canonical_views, start=1):
        claims = view.get("source_claims")
        # A view may carry both. Each field is then checked against its own
        # ground truth: citing the archive must not smuggle an unchecked quote
        # into the persisted spec.
        if "source_quote" in view and normalized_excerpt:
            quote = " ".join(str(view["source_quote"]).split()).lower()
            if not quote or quote not in normalized_excerpt:
                raise ValueError(
                    f"view {index} source_quote is not found in the supplied "
                    "excerpt; every risk view must quote the operator's text"
                )
        elif not claims:
            # A quote with no excerpt to check it against is unverified.
            verified = False
        if claims:
            if not known:
                verified = False
                continue
            unknown = sorted({str(claim) for claim in claims} - known)
            if unknown:
                raise ValueError(
                    f"view {index} cites claim keys {unknown} not in the "
                    "archive; a rule-built view must cite the qualitative "
                    "matrix it was counted from"
                )
    return verified
