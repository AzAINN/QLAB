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
