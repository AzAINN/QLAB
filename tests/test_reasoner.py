"""The reasoner: what a formed view may say, cite, and offer.

Every test is offline and touches no registry — the reasoner has no handle on
one by construction, which is half of what these tests police. The provider is
always a local callable that records what it was handed and replays one canned
completion; no backend, no subprocess, no socket.
"""

from __future__ import annotations

import ast
import copy
import inspect
from collections.abc import Mapping
from dataclasses import fields

import pytest

from qlab.news import archive
from qlab.operator import reasoner
from qlab.operator.models import Completion, ModelSpec, ProviderError
from qlab.operator.reasoner import (ANSWER_MAX_COLS, ANSWER_MAX_LINES,
                                    MAX_CITATIONS, TRUNCATION_MARKER,
                                    ArchiveEvidence, ParsedView, ReasonerRefused,
                                    answer, compose_reasoner_prompt, fit_answer,
                                    offer_for, parse_view, reason)
from qlab.operator.reasoner import _startable_block
from qlab.operator.templates import TEMPLATES, TemplateNotAllowed

AS_OF = "2026-07-31T09:00:00+00:00"


# ---------------------------------------------------------------------------
# Builders — plain functions, so each test states exactly what it depends on
# ---------------------------------------------------------------------------

def spec(context_window: int = 200_000) -> ModelSpec:
    return ModelSpec(
        id="granite-3.3-8b", provider="ollama", label="Granite 3.3 8B",
        tiers=("quick",), context_window=context_window,
        serves_claude_subagent=False, supports_workforce=False,
        supports_tools=False, launch_name="granite3.3:8b",
        notes="test row; never registered with a provider")


def item(item_hash: str, *, headline: str, source: str = "Reuters",
         tickers=("SPY",), synthetic: bool = False,
         published: str = "2026-07-31T06:00:00+00:00",
         body_text: str | None = None) -> dict:
    return {"item_hash": item_hash, "headline": headline, "source": source,
            "published": published, "tickers": list(tickers),
            "body_text": body_text, "synthetic": synthetic}


def evidence(*, items=(), matched_total=None, in_universe=("SPY",),
             out_of_universe_terms=(), not_established=(),
             archive_begins="2026-07-01T00:00:00+00:00",
             relevance=None) -> ArchiveEvidence:
    """An extract shaped exactly like ``RelevanceReport.to_dict()`` supplies."""
    rel = relevance if relevance is not None else {
        "in_universe_tickers": list(in_universe),
        "out_of_universe_terms": list(out_of_universe_terms),
        "universe": ["SPY", "QQQ", "ACWI"],
        "corroboration_value": None,
        "corroboration_state": "insufficient",
        "archive_lag_hours": 3.0,
        "not_established": list(not_established),
    }
    return ArchiveEvidence(
        items=tuple(items),
        matched_total=len(items) if matched_total is None else matched_total,
        relevance=rel, as_of=AS_OF, as_of_source="caller",
        archive_begins=archive_begins)


def context(*, panel_error=None, readings=None, risk_profile=None,
            universe=("SPY", "QQQ", "ACWI"), policy="equal_risk") -> dict:
    if readings is None:
        readings = [{"indicator": "turbulence", "state": "elevated",
                     "signal": "risk_off", "threshold": 0.8, "percentile": 0.93,
                     "reasoning": "turbulence sits above its trailing band",
                     "quality_flags": []}]
    return {
        "as_of": AS_OF,
        "mandate": {"universe": list(universe), "operational_policy": policy,
                    "max_weight_per_asset": 0.4,
                    "max_turnover_per_rebalance": 0.2,
                    "risk_profile": risk_profile},
        "regime_panel": {"error": panel_error, "robust_state": "risk_off",
                         "agreement": 3, "disagreement": 2,
                         "readings": list(readings)},
        "qualitative_signals": {"signals": [
            {"name": "corroboration_ratio", "value": 0.15, "state": "ok",
             "reason": "3 of 20 claims are corroborated"},
            {"name": "coverage_breadth", "value": None, "state": "no_window",
             "reason": "no window"}], "item_count": 20, "sufficient": True},
        "tensions": ["the panel reads risk-off while the record is calm"],
        "supported_claims": [],
        "recent_decisions": [],
        "startable": [{"template_id": "news_read", "startable": True,
                       "purpose": "interpret the grounded window"}],
    }


def facts(*, news_items: int = 19, paper_eligible: bool = False) -> dict:
    """The narrow gate input, exactly as ``atlas_facts`` returns it."""
    return {
        "universe": ["SPY", "QQQ", "ACWI"],
        "data": {"provider": "synthetic", "blocked": False,
                 "eligible_for_paper_proposal": paper_eligible},
        "portfolio": {"equity": 10000.0, "drawdown": 0.0},
        "regime": {"robust_state": "risk_off", "flip": False},
        "open_workflows": 0, "pending_approvals": 0, "order_anomaly": False,
        "news_window_sufficient": True, "news_window_items": news_items,
    }


class Recorder:
    """A ``complete`` that records what it was handed and replays one answer."""

    def __init__(self, text: str, *, stop_reason: str = "end_turn",
                 raw_model: str | None = None, raises: Exception | None = None):
        self.requests: list = []
        self._completion = Completion(
            text=text, input_tokens=None, output_tokens=None, latency_ms=1.0,
            stop_reason=stop_reason, raw_model=raw_model)
        self._raises = raises

    def __call__(self, request):
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return self._completion

    @property
    def request(self):
        assert len(self.requests) == 1, f"expected one call, got {len(self.requests)}"
        return self.requests[0]


def module_ast() -> ast.Module:
    return ast.parse(inspect.getsource(reasoner))


def one_view(**kwargs):
    defaults = dict(context=context(),
                    evidence=evidence(items=[item("h1", headline="Rates hold")]),
                    question="what is new?", mode="research", facts=facts(),
                    spec=spec())
    defaults.update(kwargs)
    return reason(**defaults)


# ---------------------------------------------------------------------------
# 1. The reasoner cannot fetch and cannot query
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORTS = ("qlab.news.feed", "qlab.state.registry", "qlab.ui.server",
                     "httpx", "subprocess", "requests", "urllib", "socket")


def test_reasoner_cannot_fetch_or_query():
    """Its only news input is the evidence the owner hands it.

    No MCP tool is added for it either, so the coordinator's allowlist is
    unchanged and no authority is widened by making Atlas reason.
    """
    imported: set[str] = set()
    for node in ast.walk(module_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for name in imported:
        for forbidden in FORBIDDEN_IMPORTS:
            assert not (name == forbidden or name.startswith(forbidden + ".")), (
                f"reasoner imports {name!r}; the archive extract is its only "
                "news source")

    held = item("h1", headline="Chip demand steadies into the quarter")
    complete = Recorder("The record supports steady demand.\nCITE: h1")
    reason(context=context(), evidence=evidence(items=[held]),
           question="what is new?", mode="research", facts=facts(),
           spec=spec(), complete=complete)

    prompt = complete.request.prompt
    assert "Chip demand steadies into the quarter" in prompt
    assert "Samsung Electronics guides higher" not in prompt, (
        "the prompt must contain only the evidence it was handed")


# ---------------------------------------------------------------------------
# 2-4. Citations, synthetic rows, and weights
# ---------------------------------------------------------------------------

def test_citation_outside_the_evidence_set_is_refused():
    ev = evidence(items=[item("h1", headline="Rates hold")])
    with pytest.raises(ReasonerRefused, match="deadbeefdeadbeef"):
        parse_view("The record holds.\nCITE: deadbeefdeadbeef", evidence=ev)


def test_synthetic_row_cannot_be_cited():
    """Storable and searchable, never evidence — not left to a query default."""
    ev = evidence(items=[item("real1", headline="Rates hold"),
                         item("fake1", headline="Fixture headline",
                              synthetic=True)])
    assert "fake1" in ev.hashes
    assert ev.citable_hashes == frozenset({"real1"})
    with pytest.raises(ReasonerRefused, match="synthetic"):
        parse_view("A view.\nCITE: fake1", evidence=ev)


@pytest.mark.parametrize("text", [
    "SPY 0.35, QQQ 0.25 is where this leaves the book.",
    "allocate 35% to SPY on this read.",
    "target weight for QQQ should move.",
    "the notional here is 12000.00 against the book.",
    "spy: 60% and the rest in cash.",
])
def test_a_view_containing_a_weight_is_refused(text):
    ev = evidence(items=[item("h1", headline="Rates hold", tickers=("SPY", "QQQ"))],
                  in_universe=("SPY", "QQQ"))
    with pytest.raises(ReasonerRefused):
        parse_view(text, evidence=ev)

    banned = {"weight", "weights", "target", "target_weight", "notional", "leg",
              "legs", "plan_id", "quantity", "qty", "order", "orders", "size"}
    names = {f.name for f in fields(reasoner.ReasonedView)}
    assert not (names & banned), (
        f"ReasonedView must carry no field capable of an order; found "
        f"{names & banned}")


def test_a_view_that_merely_names_a_holding_is_not_refused():
    """The guard must not swallow ordinary prose about the book."""
    ev = evidence(items=[item("h1", headline="Rates hold", tickers=("SPY",))])
    parsed = parse_view("SPY is named by the record; nothing here moves the book.",
                        evidence=ev)
    assert parsed.lines


# ---------------------------------------------------------------------------
# 5. The headline case: a question about something the desk does not hold
# ---------------------------------------------------------------------------

SAMSUNG_STATEMENT = ("Samsung Electronics is outside the mandate universe and "
                     "is not tradable on this desk.")


def samsung_evidence() -> ArchiveEvidence:
    return evidence(
        items=[item("s1", headline="Samsung guides memory pricing higher",
                    tickers=(), source="Reuters"),
               item("s2", headline="Memory suppliers lift second-half outlook",
                    tickers=(), source="Bloomberg")],
        matched_total=2, in_universe=(), out_of_universe_terms=("samsung",),
        not_established=(
            SAMSUNG_STATEMENT,
            "The archive holds no record connecting these stories to a holding.",
        ))


def test_out_of_universe_question_offers_nothing_and_says_why():
    complete = Recorder("Two records name Samsung; both are single-outlet takes.\n"
                        "CITE: s1, s2")
    view = reason(context=context(), evidence=samsung_evidence(),
                  question="what would have made Samsung surge this morning?",
                  mode="research", facts=facts(), spec=spec(), complete=complete)

    assert view.offer is None
    assert view.offer_refused_reason == "no holding is implicated by these records"
    assert SAMSUNG_STATEMENT in view.not_established
    assert [c.item_hash for c in view.citations] == ["s1", "s2"]
    assert view.citations[0].source == "Reuters"
    assert view.citations[0].headline == "Samsung guides memory pricing higher"
    # The refusal statement must reach the model too, not only the payload.
    assert SAMSUNG_STATEMENT in complete.request.prompt


def test_the_real_relevance_report_drives_the_same_refusal():
    """The seam against `archive.relevance_report`, not a hand-shaped stub.

    The owner composes evidence from that function; if its keys or its
    statements move, this fails here rather than in production.
    """
    page = [item("s1", headline="Samsung guides memory pricing higher",
                 tickers=())]
    report = archive.relevance_report(
        terms=archive.normalise_terms("what would have made Samsung surge"),
        universe=("SPY", "QQQ", "ACWI"), matched_total=1, page=page,
        single_secondary_total=1, synthetic_excluded=0,
        newest_published="2026-07-31T06:00:00+00:00",
        archive_begins="2026-07-01T00:00:00+00:00",
        providers_in_window=("alpaca",), as_of=AS_OF,
        now="2026-07-31T09:00:00+00:00")

    ev = ArchiveEvidence(items=tuple(page), matched_total=1,
                         relevance=report.to_dict(), as_of=AS_OF,
                         as_of_source="caller",
                         archive_begins="2026-07-01T00:00:00+00:00")
    assert ev.in_universe_tickers == ()

    view = reason(context=context(), evidence=ev,
                  question="what would have made Samsung surge this morning?",
                  mode="research", facts=facts(), spec=spec(),
                  complete=Recorder("One record names Samsung.\nCITE: s1"))
    assert view.offer is None
    assert view.offer_refused_reason == "no holding is implicated by these records"
    assert any("SAMSUNG is not in the mandate universe" in s
               for s in view.not_established)


# ---------------------------------------------------------------------------
# 6-8. The offer, and the two gates behind it
# ---------------------------------------------------------------------------

def test_offer_is_never_a_plan_creating_template():
    parsed = ParsedView(lines=("a view",), cited_hashes=(),
                        proposed_template="desk_rebalance_review")
    ev = evidence(items=[item("h1", headline="Rates hold")], matched_total=9)
    assert TEMPLATES["desk_rebalance_review"].creates_plan is True

    for mode in ("observe", "research", "propose", "paused"):
        offer, refused = offer_for(parsed=parsed, evidence=ev, mode=mode,
                                   facts=facts(paper_eligible=True))
        assert offer is None, f"{mode!r} offered a plan-creating template"
        assert refused, f"{mode!r} refused without saying why"

    # Propose mode is the case check_startable itself permits; the second
    # assertion is what stops it there, so name that path explicitly.
    _, refused = offer_for(parsed=parsed, evidence=ev, mode="propose",
                           facts=facts(paper_eligible=True))
    assert "creates a paper plan" in refused


def test_the_gates_refusal_survives_into_the_offer(monkeypatch):
    """An un-startable template is never offered and its refusal never vanishes."""
    def refuse(template_id, mode, gate_facts):
        raise TemplateNotAllowed("nope")

    monkeypatch.setattr(reasoner, "check_startable", refuse)
    parsed = ParsedView(lines=("a view",), cited_hashes=(),
                        proposed_template="news_read")
    offer, refused = offer_for(parsed=parsed,
                               evidence=evidence(items=[item("h1", headline="x")]),
                               mode="research", facts=facts())
    assert offer is None
    assert "nope" in refused


def test_paused_and_observe_modes_offer_nothing():
    parsed = ParsedView(lines=("a view",), cited_hashes=(),
                        proposed_template="news_read")
    ev = evidence(items=[item(f"h{i}", headline=f"story {i}") for i in range(8)],
                  matched_total=40)
    for mode in ("observe", "paused"):
        offer, refused = offer_for(parsed=parsed, evidence=ev, mode=mode,
                                   facts=facts())
        assert offer is None
        assert refused


def test_news_read_is_offered_when_the_record_is_thick_and_holds_a_name():
    parsed = ParsedView(lines=("a view",), cited_hashes=(), proposed_template=None)
    ev = evidence(items=[item(f"h{i}", headline=f"story {i}") for i in range(6)],
                  matched_total=archive.MIN_ARCHIVE_ITEMS + 1)
    offer, refused = offer_for(parsed=parsed, evidence=ev, mode="research",
                               facts=facts())
    assert offer == "news_read"
    assert refused is None
    assert TEMPLATES[offer].creates_plan is False


def test_a_thin_record_offers_nothing_rather_than_reaching_for_work():
    parsed = ParsedView(lines=("a view",), cited_hashes=(), proposed_template=None)
    ev = evidence(items=[item("h1", headline="one story")], matched_total=1)
    offer, refused = offer_for(parsed=parsed, evidence=ev, mode="research",
                               facts=facts())
    assert offer is None
    assert refused is None, "no rule fired; that is not a refusal needing words"


def test_regime_question_offers_regime_review_only_when_the_panel_read_something():
    parsed = ParsedView(lines=("a view",), cited_hashes=(), proposed_template=None)
    ev = evidence(items=[item("h1", headline="one story")], matched_total=1)
    offer, _ = offer_for(parsed=parsed, evidence=ev, mode="research",
                         facts=facts(), context=context(),
                         question="has the regime turned?")
    assert offer == "regime_review"

    offer, _ = offer_for(parsed=parsed, evidence=ev, mode="research",
                         facts=facts(), context=context(readings=[]),
                         question="has the regime turned?")
    assert offer is None, "a panel that read nothing cannot justify a re-read"


def test_an_unregistered_proposal_is_refused_not_silently_substituted():
    parsed = ParsedView(lines=("a view",), cited_hashes=(),
                        proposed_template="buy_the_dip")
    ev = evidence(items=[item(f"h{i}", headline=f"s{i}") for i in range(8)],
                  matched_total=40)
    offer, refused = offer_for(parsed=parsed, evidence=ev, mode="research",
                               facts=facts())
    assert offer is None
    assert "buy_the_dip" in refused and "not a registered template" in refused


def test_unresolved_relevance_is_not_read_as_no_holding():
    """`()` means the report found none; a missing key means it never resolved."""
    ev = evidence(items=[item("h1", headline="x")], matched_total=8,
                  relevance={"not_established": []})
    assert ev.in_universe_tickers is None
    offer, refused = offer_for(
        parsed=ParsedView(lines=(), cited_hashes=(), proposed_template=None),
        evidence=ev, mode="research", facts=facts())
    assert offer is None
    assert "did not resolve" in refused


def test_offer_surfaces_the_gates_own_data_refusal():
    """news_read needs a non-empty window; the gate's words must not vanish."""
    parsed = ParsedView(lines=("a view",), cited_hashes=(),
                        proposed_template="news_read")
    ev = evidence(items=[item("h1", headline="x")], matched_total=9)
    offer, refused = offer_for(parsed=parsed, evidence=ev, mode="research",
                               facts=facts(news_items=0))
    assert offer is None
    assert "non-empty grounded news window" in refused

    offer, refused = offer_for(parsed=parsed, evidence=ev, mode="research",
                               facts={})
    assert offer is None and refused


# ---------------------------------------------------------------------------
# 9-11. Truncation, silence, provider failure
# ---------------------------------------------------------------------------

def test_max_tokens_marks_the_view_incomplete_and_drops_the_offer():
    complete = Recorder("The record is thick and names holdings.\n"
                        "PROPOSE: news_read", stop_reason="max_tokens")
    ev = evidence(items=[item(f"h{i}", headline=f"s{i}") for i in range(8)],
                  matched_total=40)
    view = reason(context=context(), evidence=ev, question="what is new?",
                  mode="research", facts=facts(), spec=spec(), complete=complete)
    assert view.complete is False
    assert view.offer is None
    assert "truncated" in view.offer_refused_reason
    payload = view.to_event_payload()
    assert payload["complete"] is False
    assert payload["stop_reason"] == "max_tokens"
    assert payload["offer"] is None


def test_silent_completion_is_refused_not_recorded_as_a_view():
    complete = Recorder("", stop_reason="end_turn")
    with pytest.raises(ReasonerRefused, match="silent|no text"):
        one_view(complete=complete)


def test_an_empty_refusal_is_also_refused():
    """`is_silent` is bound to end_turn; an empty refusal is the same non-answer."""
    with pytest.raises(ReasonerRefused, match="no text"):
        one_view(complete=Recorder("   ", stop_reason="refusal"))


def test_provider_error_propagates_without_trying_another_model():
    boom = ProviderError("ollama is not listening on 11434")
    complete = Recorder("unused", raises=boom)
    with pytest.raises(ProviderError) as excinfo:
        one_view(complete=complete)
    assert excinfo.value is boom
    assert len(complete.requests) == 1, "no second model, no second provider"


# ---------------------------------------------------------------------------
# 12. The context budget refuses; it never trims
# ---------------------------------------------------------------------------

def test_context_overflow_refuses_and_never_truncates_evidence():
    items = [item(f"h{i}", headline=f"story number {i} " + "detail " * 40)
             for i in range(30)]
    ev = evidence(items=items, matched_total=30)
    complete = Recorder("never reached")
    small = spec(context_window=64)

    with pytest.raises(ReasonerRefused) as excinfo:
        reason(context=context(), evidence=ev, question="what is new?",
               mode="research", facts=facts(), spec=small, complete=complete)

    message = str(excinfo.value)
    assert "64" in message and "token" in message
    assert "granite-3.3-8b" in message
    assert complete.requests == [], "the model must never be called on overflow"
    assert len(ev.items) == 30, "the evidence set must not be shortened to fit"


# ---------------------------------------------------------------------------
# 13. An empty archive is a fact, not a failure
# ---------------------------------------------------------------------------

EMPTY_STATEMENT = ("The archive holds no record in this window; nothing was "
                   "stored, so nothing is established.")


def test_empty_archive_still_produces_a_view_that_says_so():
    ev = evidence(items=(), matched_total=0, in_universe=(),
                  not_established=(EMPTY_STATEMENT,), archive_begins=None)
    assert ev.is_empty
    complete = Recorder("There is nothing on file to read here.")
    view = reason(context=context(), evidence=ev,
                  question="what drove the move?", mode="research",
                  facts=facts(), spec=spec(), complete=complete)

    assert EMPTY_STATEMENT in view.lines
    assert view.citations == ()
    assert view.offer is None
    assert EMPTY_STATEMENT in complete.request.prompt
    assert "THE ARCHIVE HOLDS NOTHING" in complete.request.prompt


# ---------------------------------------------------------------------------
# 14-15. Bounded to the screen
# ---------------------------------------------------------------------------

def test_answer_is_bounded_to_the_screen():
    long_answer = "\n".join(("lorem ipsum dolor sit amet " * 8).strip()
                            for _ in range(60))
    complete = Recorder(long_answer)
    view = answer(context=context(),
                  evidence=evidence(items=[item("h1", headline="x")]),
                  question="summarise the record", mode="research",
                  facts=facts(), spec=spec(), complete=complete)
    assert len(view.lines) <= ANSWER_MAX_LINES
    assert all(len(line) <= ANSWER_MAX_COLS for line in view.lines)
    assert "more line(s) not shown" in view.lines[-1]


def test_fit_answer_marks_truncation_and_never_cuts_silently():
    short = ["one", "two", "three"]
    assert fit_answer(short) == ("one", "two", "three")
    assert not any("not shown" in line for line in fit_answer(short))

    long = [f"line {i} " + "word " * 40 for i in range(60)]
    fitted = fit_answer(long)
    assert len(fitted) == ANSWER_MAX_LINES
    marker = fitted[-1]
    assert "more line(s) not shown" in marker
    dropped = int(marker.split()[1])
    assert dropped > 0
    # The marker's count is the real count, not a decoration.
    assert len(fitted) - 1 + dropped == len(fit_answer(long, max_lines=10_000))
    assert marker == TRUNCATION_MARKER.format(n=dropped)


def test_fit_answer_wraps_a_single_unbreakable_run():
    """A 300-character run with no spaces still has to fit the pane."""
    fitted = fit_answer(["x" * 300], max_lines=ANSWER_MAX_LINES, max_cols=40)
    assert all(len(line) <= 40 for line in fitted)
    assert len(fitted) == 8
    assert not any("not shown" in line for line in fitted), (
        "nothing was dropped, so the marker must be absent")

    cut = fit_answer(["x" * 300], max_lines=3, max_cols=40)
    assert len(cut) == 3
    assert cut[-1] == TRUNCATION_MARKER.format(n=6)


# ---------------------------------------------------------------------------
# 16-17. Provenance
# ---------------------------------------------------------------------------

def test_view_payload_records_prompt_and_grounding_versions():
    payload = one_view(complete=Recorder("A view.")).to_event_payload()
    assert payload["prompt_version"] == reasoner.REASONER_PROMPT_VERSION
    assert payload["grounding_version"] == archive.GROUNDING_VERSION


def test_view_payload_records_provider_and_distinguishes_requested_from_served():
    payload = one_view(complete=Recorder("A view.")).to_event_payload()
    assert payload["model_id"] == "granite-3.3-8b"
    assert payload["provider"] == "ollama"
    assert payload["served_model"] is None, (
        "a backend that reported no served model must not have one invented")

    served = one_view(complete=Recorder("A view.", raw_model="granite3.3:8b-instruct"))
    assert served.served_model == "granite3.3:8b-instruct"
    assert served.model_id == "granite-3.3-8b"

    blank = one_view(complete=Recorder("A view.", raw_model=""))
    assert blank.served_model is None, "'' is absence, recorded as absence"


# ---------------------------------------------------------------------------
# 18. facts is a parameter, never recomputed
# ---------------------------------------------------------------------------

def test_reasoner_never_recomputes_atlas_facts(monkeypatch):
    """Recomputing would consume the owner's regime-flip latch mid-heartbeat."""
    seen: list[dict] = []
    real = reasoner.check_startable

    def spy(template_id, mode, gate_facts):
        seen.append(gate_facts)
        return real(template_id, mode, gate_facts)

    monkeypatch.setattr(reasoner, "check_startable", spy)

    sentinel_facts = facts()
    sentinel_facts["sentinel"] = "facts-were-handed-in"
    sentinel_context = context(policy="sentinel_policy_marker")
    before_facts = copy.deepcopy(sentinel_facts)
    before_context = copy.deepcopy(sentinel_context)

    ev = evidence(items=[item(f"h{i}", headline=f"s{i}") for i in range(8)],
                  matched_total=40)
    complete = Recorder("A view.\nPROPOSE: news_read")
    view = one_view(context=sentinel_context, evidence=ev,
                    facts=sentinel_facts, complete=complete)

    assert view.offer == "news_read"
    assert seen and seen[0]["sentinel"] == "facts-were-handed-in"
    assert seen[0]["news_window_items"] == sentinel_facts["news_window_items"]
    assert sentinel_facts == before_facts, "facts must not be mutated"
    assert sentinel_context == before_context, "context must not be mutated"
    assert "sentinel_policy_marker" in complete.request.prompt

    for node in ast.walk(module_ast()):
        if isinstance(node, ast.Name):
            assert node.id != "atlas_facts"
        if isinstance(node, ast.Attribute):
            assert node.attr != "atlas_facts"


# ---------------------------------------------------------------------------
# 19. Absence is stated, never defaulted
# ---------------------------------------------------------------------------

def test_prompt_states_absence_rather_than_defaulting_it():
    ev = evidence(items=[item("h1", headline="Rates hold")])

    failed = compose_reasoner_prompt(
        context=context(panel_error="turbulence needs 60 sessions; got 12"),
        evidence=ev, question="how is the regime?", spec=spec())
    assert "turbulence needs 60 sessions; got 12" in failed.prompt
    assert "FAILED" in failed.prompt
    assert "risk_off" not in failed.prompt, (
        "a failed panel asserts no state; carrying one would be read as one")

    silent = compose_reasoner_prompt(context=context(readings=[]), evidence=ev,
                                     question="how is the regime?", spec=spec())
    assert "read nothing" in silent.prompt
    assert "not a calm reading" in silent.prompt
    # A panel that read nothing still ran, so it is not reported as FAILED.
    assert "FAILED" not in silent.prompt

    absent_profile = compose_reasoner_prompt(
        context=context(), evidence=ev, question="what is new?", spec=spec())
    assert "risk_profile = ABSENT" in absent_profile.prompt
    assert "Do not assume one." in absent_profile.prompt

    # `startable_tasks` lists queued tasks only, so an empty list is "nothing is
    # waiting", never "nothing may be started".
    idle = dict(context())
    idle["startable"] = []
    empty_queue = compose_reasoner_prompt(context=idle, evidence=ev,
                                          question="what is new?", spec=spec())
    assert "no task is waiting" in empty_queue.prompt
    assert "not the same as nothing being startable" in empty_queue.prompt


# ---------------------------------------------------------------------------
# 20. A question that was not asked
# ---------------------------------------------------------------------------

def test_standing_view_with_no_question_is_valid_and_empty_question_is_not():
    complete = Recorder("The desk stands where it did; nothing new is established.")
    view = one_view(question=None, complete=complete)
    assert view.question is None
    assert view.to_event_payload()["question"] is None
    assert view.lines
    assert "no question was asked" in complete.request.prompt.lower()

    with pytest.raises(ValueError):
        answer(context=context(),
               evidence=evidence(items=[item("h1", headline="x")]),
               question="", mode="research", facts=facts(), spec=spec(),
               complete=Recorder("unused"))

    with pytest.raises(ValueError):
        compose_reasoner_prompt(
            context=context(), evidence=evidence(items=[item("h1", headline="x")]),
            question="   ", spec=spec())


# ---------------------------------------------------------------------------
# 21. No execution surface, in the prompt or in the view
# ---------------------------------------------------------------------------

def test_no_execution_surface_reaches_the_prompt_or_the_view():
    request = compose_reasoner_prompt(
        context=context(), evidence=evidence(items=[item("h1", headline="x")]),
        question="what is new?", spec=spec())
    system = request.system
    assert "NO EXECUTION PATH" in system
    assert "cannot create, approve or execute a plan" in system
    assert "at most ONE registered workflow template" in system
    assert "NEVER state a weight" in system

    names = {f.name for f in fields(reasoner.ReasonedView)}
    assert names == {
        "question", "lines", "citations", "not_established", "offer",
        "offer_refused_reason", "model_id", "provider", "served_model",
        "prompt_version", "grounding_version", "stop_reason", "complete"}


# ---------------------------------------------------------------------------
# Evidence and parsing, policed on their own
# ---------------------------------------------------------------------------

def test_archive_evidence_refuses_an_unattributed_point_in_time():
    with pytest.raises(ValueError, match="as_of_source"):
        ArchiveEvidence(items=(), matched_total=0, relevance={}, as_of=AS_OF,
                        as_of_source="guessed")
    with pytest.raises(ValueError, match="as_of"):
        ArchiveEvidence(items=(), matched_total=0, relevance={}, as_of="",
                        as_of_source="now")
    with pytest.raises(ValueError, match="matched_total"):
        ArchiveEvidence(items=(item("h1", headline="x"),), matched_total=0,
                        relevance={}, as_of=AS_OF, as_of_source="now")


def test_parse_view_separates_prose_citations_and_one_proposal():
    ev = evidence(items=[item("h1", headline="a"), item("h2", headline="b")],
                  matched_total=9)
    parsed = parse_view(
        "Coverage is broad but shallow.\n"
        "Most of it is single-outlet.\n"
        "CITE: h1, h2\n"
        "PROPOSE: news_read\n", evidence=ev)
    assert parsed.lines == ("Coverage is broad but shallow.",
                            "Most of it is single-outlet.")
    assert parsed.cited_hashes == ("h1", "h2")
    assert parsed.proposed_template == "news_read"


def test_two_proposals_are_refused():
    ev = evidence(items=[item("h1", headline="a")])
    with pytest.raises(ReasonerRefused, match="at most one proposal"):
        parse_view("A view.\nPROPOSE: news_read\nPROPOSE: regime_review",
                   evidence=ev)


def test_more_citations_than_the_cap_are_refused_not_quietly_dropped():
    items = [item(f"h{i}", headline=f"s{i}") for i in range(MAX_CITATIONS + 2)]
    ev = evidence(items=items, matched_total=len(items))
    cites = ", ".join(f"h{i}" for i in range(MAX_CITATIONS + 2))
    with pytest.raises(ReasonerRefused, match="cap"):
        parse_view(f"A view.\nCITE: {cites}", evidence=ev)


# --- the production call site -------------------------------------------------
#
# These stub the provider. atlas_reason spawns a real `claude` CLI otherwise —
# measured at 51s per call, billed, and non-deterministic, which is three
# separate ways to violate "tests pass fully offline" (invariant 2). The first
# version of these did exactly that and flapped on the third run.


ANSWER = """
The archive holds nothing for this question: an empty search, not a failed one.
Samsung sits outside the mandate universe, so no holding is implicated.
"""


class _StubProvider:
    """A provider whose completion is fixed, so an assertion means something."""

    name = "stub"
    required_env = ()

    def __init__(self, text=ANSWER, configured=(True, "")):
        self.recorder = Recorder(text, raw_model="stub-1")
        self._configured = configured

    def configured(self):
        return self._configured

    def complete(self, request):
        return self.recorder(request)


def _owner(provider=None, monkeypatch=None):
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession
    import qlab.operator.models as models

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    session.archive_desk_news(session.fetch_desk_news(True))
    stub = provider or _StubProvider()
    monkeypatch.setattr(models, "get_provider", lambda name: stub)
    return session, stub


def test_atlas_reason_is_reachable_and_answers(monkeypatch):
    """reasoner.py had no caller until this existed. Dead code has shipped
    three times in this repo; a seam without a call site is the same shape."""
    session, stub = _owner(monkeypatch=monkeypatch)
    try:
        out = session.atlas_reason(question="what moved credit?", offline=True)
        assert out["available"] is True
        assert out["lines"] and out["model_id"]
        # The model was actually asked, with the question in the prompt.
        assert stub.recorder.requests
        assert "credit" in stub.recorder.request.prompt.lower()
    finally:
        session.registry.close()


def test_a_synthetic_only_archive_yields_no_citations(monkeypatch):
    """Storable, never citable. Every fixture row stays out of the evidence an
    answer rests on, so an answer over a synthetic archive cites nothing."""
    session, _ = _owner(monkeypatch=monkeypatch)
    try:
        out = session.atlas_reason(question="credit spreads", offline=True)
        assert list(out["citations"]) == []
    finally:
        session.registry.close()


def test_an_unconfigured_provider_is_a_named_refusal_not_a_substitution(monkeypatch):
    """Invariant 4. An answer served by a model the operator did not choose is
    worse than no answer."""
    session, _ = _owner(
        provider=_StubProvider(configured=(False, "ANTHROPIC_API_KEY is unset")),
        monkeypatch=monkeypatch)
    try:
        out = session.atlas_reason(question="anything", offline=True)
        assert out["available"] is False
        assert "ANTHROPIC_API_KEY is unset" in out["reason"]
        # The refused model is named, so the operator can act on it.
        assert out["model_id"]
    finally:
        session.registry.close()


def test_a_failure_resolving_the_model_refuses_rather_than_raising(monkeypatch):
    """resolve_selection validates every slot and can raise. Left outside the
    guard it turned a bad selection into a 500 instead of a named refusal."""
    import qlab.operator.models as models

    session, _ = _owner(monkeypatch=monkeypatch)
    try:
        def boom():
            raise models.ModelNotEligible("deep slot names an unknown model")

        monkeypatch.setattr(models, "resolve_selection", boom)
        out = session.atlas_reason(question="anything", offline=True)
        assert out["available"] is False
        assert "unknown model" in out["reason"]
    finally:
        session.registry.close()


def test_a_question_of_only_stopwords_has_no_terms():
    """Every word became a candidate ticker, so an answer opened with 'WHAT is
    not in the mandate universe' before saying anything useful."""
    from qlab.news.archive import normalise_terms

    assert normalise_terms("what would have made Samsung surge?") == ("samsung", "surge")
    # No subject is not the same as matching nothing.
    assert normalise_terms("what is the") == ()


# ---------------------------------------------------------------------------
# 24. The book — the desk Atlas is asked about
# ---------------------------------------------------------------------------
#
# `atlas_context` composed a portfolio, a predictor board and a decision
# history, handed them over, and the prompt rendered none of them. An operator
# asking "how is my book doing" got an answer formed from news and a regime
# panel by a model that could not see the book. These pin the three blocks and
# the absence wording each one owes.

def book(*, equity=10_000.0, drawdown=0.0, halted=False, positions=None,
         weights=None, kill_switch_at=0.15, cash=0.0,
         broker="simulated_paper", high_water_mark=None) -> dict:
    """Exactly the payload `SessionOwner.portfolio` returns."""
    return {
        "broker": broker,
        "cash": cash,
        "equity": equity,
        "high_water_mark": equity if high_water_mark is None else high_water_mark,
        "drawdown": drawdown,
        "kill_switch_at": kill_switch_at,
        "kill_switch_distance": round(kill_switch_at - drawdown, 4),
        "halted": halted,
        "positions": dict(positions or {}),
        "weights": dict(weights or {}),
        "target_weights": {},
    }


def position(qty=10.0, price=100.0, unrealized_pl=0.0) -> dict:
    return {"qty": qty, "price": price, "value": qty * price,
            "unrealized_pl": unrealized_pl}


def test_the_book_reaches_the_prompt_so_a_portfolio_question_can_be_answered():
    """The operator's whole question is usually about the book. A prompt
    without it answers a different question than the one that was asked."""
    ev = evidence(items=[item("h1", headline="Rates hold")])
    ctx = dict(context())
    ctx["portfolio"] = book(equity=10_007.58, drawdown=0.5929, halted=True,
                            high_water_mark=24_584.34,
                            positions={"SPY": position(unrealized_pl=-412.0)},
                            weights={"SPY": 0.31})
    request = compose_reasoner_prompt(context=ctx, evidence=ev,
                                      question="how is my book doing?",
                                      spec=spec())
    assert "THE BOOK" in request.prompt
    # The three facts that change what an honest answer says.
    assert "10,007.58" in request.prompt or "10007.58" in request.prompt
    assert "59.29%" in request.prompt or "0.5929" in request.prompt
    assert "HALTED" in request.prompt
    assert "SPY" in request.prompt


def test_a_halted_book_says_so_in_words_the_model_cannot_read_past():
    """A breached kill switch is the single most consequential fact on the
    desk. Reported as a bare boolean it reads as one field among twelve."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["portfolio"] = book(drawdown=0.5929, halted=True, kill_switch_at=0.15)
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="should we rebalance?",
                                     spec=spec()).prompt
    assert "HALTED" in prompt
    assert "kill switch" in prompt.lower()
    # Not a suggestion the model may weigh: trading is stopped as a fact.
    assert "no new risk" in prompt.lower() or "trading is stopped" in prompt.lower()


def test_an_absent_book_is_named_rather_than_read_as_a_flat_desk():
    """Zero equity and no book are different facts. A desk whose valuation
    failed must never render as a desk that holds nothing."""
    ev = evidence(items=[item("h1", headline="x")])
    prompt = compose_reasoner_prompt(context=context(), evidence=ev,
                                     question="how is my book?",
                                     spec=spec()).prompt
    assert "THE BOOK: absent" in prompt
    assert "not established" in prompt.lower()
    assert "empty" in prompt.lower() or "flat" in prompt.lower()


def test_a_book_that_holds_nothing_is_not_the_same_as_an_absent_one():
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["portfolio"] = book(positions={})
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="what do we hold?",
                                     spec=spec()).prompt
    assert "THE BOOK:" in prompt
    assert "absent" not in prompt.split("THE BOOK:")[1].split("\n")[0]
    assert "holds no position" in prompt.lower()


def test_the_predictor_board_reaches_the_prompt_with_its_own_absence_states():
    """The board is the desk's forward-looking research evidence and the whole
    point of the augmented lane. Handed over and never rendered, an operator
    asking "is the quantum lane working" got an answer from news."""
    ev = evidence(items=[item("h1", headline="x")])

    never = dict(context())
    never["predictors"] = {"status": "never_ran"}
    prompt = compose_reasoner_prompt(context=never, evidence=ev,
                                     question="is the augmented lane working?",
                                     spec=spec()).prompt
    assert "PREDICTOR BOARD" in prompt
    assert "never been run" in prompt or "never run" in prompt

    # `predictor_board_summary`'s own shape, admitting nothing.
    ran = dict(context())
    ran["predictors"] = {
        "status": "ok", "run_id": "r1", "as_of": "2026-07-31",
        "source": "synthetic", "age_days": 3, "admitted_any": False,
        "champion": None,
        "baseline": {"model_id": "ridge:none", "mean_ic": 0.069,
                     "ic_stability": 0.31, "usable": True,
                     "paired_t_vs_baseline": None},
        "best_delta_vs_baseline": -0.0012,
        "ranking": ["ridge:none", "kernel:zz"],
    }
    prompt = compose_reasoner_prompt(context=ran, evidence=ev,
                                     question="is the augmented lane working?",
                                     spec=spec()).prompt
    assert "ridge:none" in prompt
    assert "kernel:zz" in prompt
    # No admitted model is the board's honest answer, not a missing value.
    assert "no model" in prompt.lower() and "admitted" in prompt.lower()
    # The board is advisory; the gate never reads it and the prompt says so.
    assert "advisory" in prompt.lower()


def test_an_unreadable_predictor_board_is_not_reported_as_a_result():
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["predictors"] = {"status": "unreadable", "run_id": "r9"}
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="how did the board do?",
                                     spec=spec()).prompt
    assert "could not be read" in prompt.lower()
    assert "r9" in prompt


# The live board admitted `kernel:angle` -- a quantum angle feature map -- as
# champion on mean_ic 0.178 against a 0.03 bar. Read alone that is a triumph
# for the augmented lane. The same run also scored a paired t of 0.237 across
# five folds, and was negative in two of them. Both facts are computed. Only
# the flattering one reached Atlas.


def _live_shaped_board(**over) -> dict:
    """The champion row exactly as the live desk produced it."""
    board = {
        "status": "ok", "run_id": "r1", "as_of": "2026-07-30",
        "source": "yfinance", "age_days": 4, "admitted_any": True,
        "n_obs": 671, "n_folds": 5,
        "admission": {"mean_ic_strictly_above": 0.03,
                      "ic_stability_strictly_above": 0.5},
        "champion": {
            "model_id": "kernel:angle", "family": "kernel", "variant": "angle",
            "mean_ic": 0.17838927712223623, "ic_std": 0.33001754801785516,
            "ic_stability": 0.5405448231273591, "usable": True,
            "paired_t_vs_baseline": 0.23657619605499555,
            "wins_vs_baseline": 3,
            "per_fold": [{"fold": 1, "ic": 0.3243502051983584},
                         {"fold": 2, "ic": 0.5307406683603674},
                         {"fold": 3, "ic": 0.470805226661635},
                         {"fold": 4, "ic": -0.2389876880984952},
                         {"fold": 5, "ic": -0.19496202651068445}],
        },
        "baseline": {"model_id": "ridge:none", "mean_ic": 0.11049604765723449,
                     "ic_stability": 0.26858064964853823, "usable": False,
                     "paired_t_vs_baseline": None},
        "best_delta_vs_baseline": 0.06789322946500173,
        "ranking": ["kernel:angle", "ridge:none"],
    }
    board.update(over)
    return board


def test_an_admitted_model_arrives_with_the_bar_it_cleared():
    """`usable: true` is a comparison, and the prompt carried the verdict
    without the threshold. A reader cannot tell a model that cleared the bar
    by a mile from one that scraped it, so both read as "admitted"."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["predictors"] = _live_shaped_board()
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="is the quantum lane working?",
                                     spec=spec()).prompt
    assert "0.03" in prompt, "the mean_ic admission bar"
    assert "0.5" in prompt, "the ic_stability admission bar"
    # kernel:angle cleared stability by 0.04. That margin is the whole story.
    assert "margin" in prompt.lower() or "scraped" in prompt.lower()


def test_a_t_statistic_never_arrives_without_the_folds_it_was_computed_over():
    """0.237 sounds like a number until you learn it came from five folds.
    A paired t with no n is not evidence, and a model shown one will read it
    as one."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["predictors"] = _live_shaped_board()
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="is the quantum lane working?",
                                     spec=spec()).prompt
    t_at = prompt.find("0.2365")
    assert t_at > 0, "the paired t reaches the prompt"
    nearby = prompt[t_at:t_at + 400]
    assert "5" in nearby and "fold" in nearby.lower(), nearby
    # And it is named as not significant rather than left to be read as a win.
    assert "not significant" in prompt.lower() or "cannot distinguish" in prompt.lower()


def test_a_champion_that_lost_in_some_folds_says_so():
    """Three wins in five is the same headline mean_ic as five in five, and a
    mean over folds that flip sign is not a skill estimate. The live champion
    was negative in two of its five folds and the prompt said only 0.178."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["predictors"] = _live_shaped_board()
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="is the quantum lane working?",
                                     spec=spec()).prompt
    low = prompt.lower()
    assert "3 of 5" in prompt or "3/5" in prompt, "wins vs the baseline"
    assert "negative in 2" in low or "2 of 5" in prompt, \
        "folds where the champion was worse than useless"


def test_the_quantum_lane_is_named_as_such_so_the_question_can_be_answered():
    """An operator asks "is the quantum feature augmentation earning its
    place". `kernel:angle` answers that only if the prompt says the kernel
    family IS the augmented lane and ridge:none is the unaugmented control.

    The question deliberately avoids the word, so this cannot pass on the
    echoed question text -- the *board block* has to say it."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["predictors"] = _live_shaped_board()
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="how is research going?",
                                     spec=spec()).prompt
    low = prompt.lower()
    assert "quantum" in low or "feature map" in low, \
        "the augmented lane must be identifiable by the name an operator uses"
    assert "ridge:none" in prompt and "baseline" in low


def test_a_board_that_admitted_nothing_is_not_softened_by_the_new_detail():
    """The added rigour must not turn "nothing was admitted" into a hedge."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["predictors"] = _live_shaped_board(champion=None, admitted_any=False,
                                           best_delta_vs_baseline=-0.0012)
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="is the quantum lane working?",
                                     spec=spec()).prompt
    low = prompt.lower()
    assert "no model" in low and "admitted" in low
    assert "no candidate beat the baseline" in low


def test_a_board_missing_its_admission_bar_says_so_rather_than_assuming_one():
    """An older run predates the field. Rendering a default bar would state a
    threshold the run never used -- worse than saying it is unknown."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    board = _live_shaped_board()
    board.pop("admission")
    board.pop("n_folds")
    ctx["predictors"] = board
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="is the quantum lane working?",
                                     spec=spec()).prompt
    assert "0.03" not in prompt, "must not invent a bar the run did not record"
    low = prompt.lower()
    assert "not recorded" in low or "unknown" in low


def test_recent_decisions_reach_the_prompt_with_unresolved_outcomes_named():
    """A decision whose outcome the reflection loop has not resolved is
    unresolved, never neutral — the same absence rule the panel gets."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["recent_decisions"] = [
        {"decision_id": "d1", "as_of": "2026-07-30", "kind": "rebalance",
         "rationale": "turbulence cleared", "outcome": "drawdown widened"},
        {"decision_id": "d2", "as_of": "2026-07-31", "kind": "hold",
         "rationale": "record too thin", "outcome": None},
    ]
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="what did we decide?",
                                     spec=spec()).prompt
    assert "RECENT DECISIONS" in prompt
    assert "turbulence cleared" in prompt
    assert "drawdown widened" in prompt
    assert "unresolved" in prompt.lower()


def test_the_book_block_never_invites_a_weight_the_guard_would_refuse():
    """The prompt now shows weights, and the view guard refuses a view that
    states one. Without an explicit instruction the model reads the block as
    permission and every answer it gives is refused — so the block must carry
    the caution in its own words. Proved rather than asserted: the rendered
    block is fed to the very guard that judges views, and it is refused."""
    ev = evidence(items=[item("h1", headline="x", tickers=("SPY",))])
    ctx = dict(context())
    ctx["portfolio"] = book(positions={"SPY": position()}, weights={"SPY": 0.31})
    request = compose_reasoner_prompt(context=ctx, evidence=ev,
                                      question="how is my book?", spec=spec())
    assert "NEVER state a weight" in request.system

    rendered = request.prompt.split("THE BOOK:")[1].split("\n\n")[0]
    assert "31.00%" in rendered, (
        "the test is vacuous unless the block really does carry a weight")
    with pytest.raises(ReasonerRefused):
        reasoner._refuse_weights(rendered, ev)
    # So the block must say, in the prompt, that these are context not vocabulary.
    assert "do not repeat" in rendered.lower()


def test_the_gates_own_verdict_reaches_the_prompt_with_its_refusal_reason():
    """`gate_facts` is the deterministic layer's verbatim view — what it will
    and will not permit right now. Dropped from the prompt, the reasoner argued
    outside its authority and the gate refused it afterwards, so the operator
    saw a suggestion followed by a refusal instead of one honest answer."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["gate_facts"] = facts(paper_eligible=False)
    ctx["gate_facts"]["data"] = {
        "provider": "yfinance", "blocked": False,
        "eligible_for_paper_proposal": False,
        "reason": "yfinance is not an execution-grade provider"}
    ctx["gate_facts"]["pending_approvals"] = 2
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="can we trade?",
                                     spec=spec()).prompt
    assert "AUTHORITY" in prompt
    # The refusal reason is the part an operator can act on.
    assert "not an execution-grade provider" in prompt
    assert "yfinance" in prompt
    assert "2" in prompt.split("AUTHORITY")[1].split("\n\n")[0]


def test_an_ineligible_desk_with_no_stated_reason_is_flagged_as_unexplained():
    """Invariant 4: a refusal always states why. When the gate hands over a
    bare `false` with no reason, the prompt must not launder it into a fact —
    it says the refusal is unexplained so the view can say so too."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["gate_facts"] = facts(paper_eligible=False)
    ctx["gate_facts"]["data"] = {"provider": "alpaca", "blocked": False,
                                 "eligible_for_paper_proposal": False,
                                 "reason": None}
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="can we trade?",
                                     spec=spec()).prompt
    assert "no reason" in prompt.lower() or "unexplained" in prompt.lower()


def test_the_archive_depth_reaches_the_prompt_so_thinness_can_be_named():
    """An answer drawn from a nine-row archive and one drawn from a nine
    thousand row archive deserve different confidence, and only the second
    number tells the model which it is holding."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["archive"] = {"rows": 61, "begins": "2026-07-28T00:00:00Z",
                      "newest_published": "2026-08-02T00:00:00Z",
                      "synthetic_rows": 12}
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="what is new?", spec=spec()).prompt
    assert "ARCHIVE DEPTH" in prompt
    assert "61" in prompt
    assert "12" in prompt          # synthetic rows are not citable evidence
    assert "2026-07-28" in prompt


def test_an_empty_archive_is_named_as_empty_rather_than_omitted():
    ev = evidence(items=[])
    ctx = dict(context())
    ctx["archive"] = {"rows": 0, "begins": None, "newest_published": None,
                      "synthetic_rows": 0}
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="what is new?", spec=spec()).prompt
    assert "ARCHIVE DEPTH" in prompt
    assert "holds nothing" in prompt.lower() or "no records" in prompt.lower()


def test_every_key_atlas_context_composes_is_rendered_by_the_prompt():
    """The bug this whole section exists for: `atlas_context` composed ten
    keys, `compose_reasoner_prompt` rendered six, and the four it dropped were
    the four an operator actually asks about. Nothing warned. This test fails
    the moment a new key is added to the context and not to the prompt."""
    composed = {
        "as_of", "gate_facts", "mandate", "regime_panel", "qualitative_signals",
        "news", "supported_claims", "tensions", "archive", "recent_decisions",
        "predictors", "startable", "portfolio",
    }
    # `as_of` rides on the evidence extract, and `news` reaches the model as
    # the grounded evidence rows themselves; both are rendered, not dropped.
    rendered_elsewhere = {"as_of", "news"}

    ev = evidence(items=[item("h1", headline="x")])
    marker = "ZZUNIQUEMARKERZZ"
    for key in sorted(composed - rendered_elsewhere):
        ctx = dict(context())
        ctx["gate_facts"] = facts()
        ctx["archive"] = {"rows": 3, "begins": None, "newest_published": None,
                          "synthetic_rows": 0}
        ctx["portfolio"] = book()
        ctx["predictors"] = {"status": "never_ran"}
        ctx["recent_decisions"] = []
        base = compose_reasoner_prompt(context=ctx, evidence=ev,
                                       question="q", spec=spec()).prompt
        # Perturb exactly one key and require the prompt to change.
        ctx[key] = _perturb(ctx.get(key), marker)
        after = compose_reasoner_prompt(context=ctx, evidence=ev,
                                        question="q", spec=spec()).prompt
        assert after != base, (
            f"context key {key!r} is composed by atlas_context and reaches the "
            f"model unchanged — it is being silently dropped")


def _perturb(value, marker: str):
    """Change a context value in a way any honest renderer must show."""
    if isinstance(value, Mapping):
        out = dict(value)
        out["reason"] = marker
        if "status" in out:
            out["status"] = "unreadable"
            out["run_id"] = marker
        if "rows" in out:
            out["rows"] = 987654
        if "equity" in out:
            out["equity"] = 987654.0
        if "universe" in out:
            out["universe"] = [marker]
        if "readings" in out:
            out["robust_state"] = marker
        if "signals" in out:
            out["item_count"] = 987654
        if "data" in out:
            out["data"] = dict(out["data"], reason=marker)
        return out
    if isinstance(value, list):
        return list(value) + [{"decision_id": marker, "as_of": marker,
                               "kind": marker, "rationale": marker,
                               "outcome": None, "template_id": marker,
                               "startable": False, "reason": marker,
                               "headline": marker}]
    return marker


def test_the_linear_kernel_is_not_sold_to_atlas_as_quantum():
    """`kernel:linear` applies no feature map: `quantum_gram` returns before
    the map term, so it is the plain ridge baseline in dual form and comes
    back bit-identical to `ridge:none` on the live board (both 0.11049604765).

    A prompt that says "`kernel:*` are the quantum feature-map augmented
    models" therefore hands Atlas a control filed as treatment, and an
    operator asking whether the quantum lane earns its place can be answered
    with a row that contains no quantum anything. The lane must be defined by
    the map, not by the family prefix."""
    ev = evidence(items=[item("h1", headline="x")])
    ctx = dict(context())
    ctx["predictors"] = _live_shaped_board()
    prompt = compose_reasoner_prompt(context=ctx, evidence=ev,
                                     question="how is research going?",
                                     spec=spec()).prompt
    assert "`kernel:*`" not in prompt, (
        "the whole kernel family is not the augmented lane; kernel:linear "
        "carries no feature map")
    low = prompt.lower()
    assert "kernel:linear" in low and "no" in low
    # The maps themselves are what must be named.
    assert "angle" in low and "zz" in low


# --- the workforce block ----------------------------------------------------
#
# Live gap: /api/atlas/context carried 12 keys and none named a workflow,
# step, phase or agent, while the desk held ten runs -- three blocked at the
# reporter, two interrupted mid-debate, one abandoned. Adding the key to the
# context is only half of it; a key the prompt never renders is a key Atlas
# cannot read.


def _workforce(**over):
    wf = {
        "workflows": [{
            "workflow_id": "368e327533734c03", "kind": "portfolio_review",
            "status": "blocked", "current_phase": "reporter",
            "as_of": "2026-08-03",
            "goal": "[risk_event] Analyze a drawdown-tier event",
            "completed_phases": ["analyst", "challenger", "optimizer",
                                 "referee"],
            "pending_phases": [],
            "stalled_at": {
                "phase": "reporter", "agent": "reporter", "status": "blocked",
                "summary": "Memo compiled and referee PASS reported, but the "
                           "paper-trade preview is blocked: the permit does "
                           "not allow it"},
        }],
        "counts": {"blocked": 1},
        "needs_attention": 1,
        "reason": "1 of 1 recent runs stopped short and can be resumed",
    }
    wf.update(over)
    return wf


def test_the_prompt_tells_atlas_what_its_own_agents_are_doing():
    """Atlas manages this workforce. The prompt named the market in detail and
    never named the desk's own runs."""
    ctx = dict(context())
    ctx["workforce"] = _workforce()
    prompt = compose_reasoner_prompt(context=ctx,
                                     evidence=evidence(items=[item("h1", headline="x")]),
                                     question="why is the desk stuck?",
                                     spec=spec()).prompt
    assert "368e327533734c03" in prompt
    assert "portfolio_review" in prompt


def test_a_stalled_run_carries_the_agent_s_own_words_into_the_prompt():
    """"3 blocked" is a tally. The sentence the reporter wrote is the reason,
    and it is the only thing that answers "why"."""
    ctx = dict(context())
    ctx["workforce"] = _workforce()
    prompt = compose_reasoner_prompt(context=ctx,
                                     evidence=evidence(items=[item("h1", headline="x")]),
                                     question="why is the desk stuck?",
                                     spec=spec()).prompt
    assert "the permit does not allow it" in prompt
    assert "reporter" in prompt
    # How far it got, not only that it stopped.
    assert "referee" in prompt


def test_an_idle_workforce_is_stated_rather_than_left_blank():
    """A desk with no runs must not read like a desk whose runs are unknown."""
    ctx = dict(context())
    ctx["workforce"] = _workforce(
        workflows=[], counts={}, needs_attention=0,
        reason="no workflow has ever run on this desk")
    prompt = compose_reasoner_prompt(context=ctx,
                                     evidence=evidence(items=[item("h1", headline="x")]),
                                     question="what is running?",
                                     spec=spec()).prompt
    assert "no workflow has ever run on this desk" in prompt


def test_a_missing_workforce_key_is_named_as_unknown_not_as_idle():
    """An older context without the key at all is an ABSENT reading, and the
    difference from "idle" is the whole point of invariant 4."""
    ctx = dict(context())
    ctx.pop("workforce", None)
    prompt = compose_reasoner_prompt(context=ctx,
                                     evidence=evidence(items=[item("h1", headline="x")]),
                                     question="what is running?",
                                     spec=spec()).prompt
    low = prompt.lower()
    assert "workforce" in low
    assert "not reported" in low or "unknown" in low or "absent" in low


def test_the_queued_block_groups_repeats_instead_of_reciting_them():
    """The live desk queued fifteen `drift_breach` tasks carrying one sentence.

    Reciting them cost fifteen lines of prompt to say one thing, and buried any
    other queued template in the repetition. Identical refusals collapse to one
    line that states how many there were.
    """
    startable = [{"task_id": f"t{i}", "template_id": "desk_rebalance_review",
                  "startable": False, "stale": True,
                  "reason": "stale: this trigger fired on 2026-07-19"}
                 for i in range(15)]
    startable.append({"task_id": "fresh", "template_id": "regime_review",
                      "startable": True, "stale": False})
    text = "\n".join(_startable_block({"startable": startable}))
    assert text.count("desk_rebalance_review") == 1
    assert "15" in text
    # The one startable template must not be lost in the crowd.
    assert "regime_review" in text
    assert "startable" in text


def test_the_queued_block_says_stale_rather_than_implying_a_permit_problem():
    """A stale task's refusal must not read as 'widen the permit and it runs'."""
    text = "\n".join(_startable_block({"startable": [
        {"task_id": "t1", "template_id": "desk_rebalance_review",
         "startable": False, "stale": True, "age_days": 15,
         "reason": "stale: this trigger fired on 2026-07-19, 15 days before"}]}))
    assert "stale" in text.lower()
    assert "2026-07-19" in text
