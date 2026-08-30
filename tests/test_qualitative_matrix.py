"""The qualitative matrix: counts of what the record says, per name."""

from qlab.news.grounding import Claim
from qlab.news.matrix import build_matrix


def claim(key, tickers, sources, tier="secondary"):
    return Claim(key=key, headline=key, tickers=tuple(tickers), sources=tuple(sources),
                 item_hashes=(key,), corroboration=len(set(sources)),
                 earliest_published="2026-08-27T12:00:00+00:00", tier=tier)


def test_the_matrix_counts_record_properties_and_carries_no_sign():
    claims = [
        claim("fed-8k", ["TLT", "BNDW"], ["SEC EDGAR"], tier="primary"),
        claim("gold-rally", ["GLD"], ["reuters.com", "ft.com"]),
        claim("gold-take", ["GLD"], ["benzinga"]),
    ]
    m = build_matrix(claims, ["TLT", "BNDW", "GLD", "USO"], "2026-08-28",
                     upcoming=[{"name": "FOMC", "days_ahead": 7, "tickers": ["TLT"]}])
    assert m.rows["GLD"].coverage == 2
    assert m.rows["GLD"].publishers == 3
    assert m.rows["GLD"].corroborated == 1
    assert m.rows["GLD"].primary_docs == 0
    assert m.rows["TLT"].primary_docs == 1 and m.rows["TLT"].corroborated == 1
    assert m.rows["TLT"].days_to_next_release == 7
    assert m.rows["USO"].coverage == 0 and m.rows["USO"].days_to_next_release is None
    assert m.rows["GLD"].claim_keys == ("gold-rally", "gold-take")
    # No column is signed: the shape of the row is the whole contract.
    assert set(m.rows["GLD"].to_dict()) == {
        "ticker", "coverage", "publishers", "corroborated", "primary_docs",
        "days_to_next_release", "claim_keys"}


def test_the_window_hash_is_a_function_of_the_claims_alone():
    a = build_matrix([claim("k", ["SPY"], ["x"])], ["SPY"], "2026-08-28", upcoming=[])
    b = build_matrix([claim("k", ["SPY"], ["x"])], ["SPY"], "2026-08-29", upcoming=[])
    assert a.window_hash == b.window_hash
