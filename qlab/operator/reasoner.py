"""Atlas's view formation and the ask seam: reasoning with narrow hands.

This is the judgment half of Atlas. It composes one prompt from the reasoning
surface (``atlas_context``) plus an archive extract it is *handed*, reads one
completion back, and turns it into a :class:`ReasonedView` — prose, citations
bound to real records, what the record does not establish, and at most one
registered template it would like started.

Three boundaries are structural rather than instructed, because an instruction
is a request and a boundary is a property:

* **It cannot fetch and it cannot query.** Its only news input is the
  :class:`ArchiveEvidence` the owner composed under its own lock. No MCP tool is
  added for it, so the coordinator's allowlist is unchanged and no authority is
  widened. This module imports neither the feed, the registry, the owner, nor an
  HTTP client — and a test parses the AST to keep it that way.
* **It cannot execute.** :class:`ReasonedView` has no field capable of carrying
  an order, a leg, a notional or a plan id, and every offer is passed through
  ``templates.check_startable`` and then re-checked for ``creates_plan``. Two
  gates in series, so widening what Atlas *researches* can never widen what it
  can *execute* even if a later template is mis-registered.
* **It cannot cite what the archive does not hold.** A cited hash outside the
  evidence set is a fabricated citation and raises; a synthetic row is storable
  but never citable, enforced here as well as at the registry writer, because
  ``include_synthetic=True`` is a legitimate debugging affordance and the
  guarantee must not rest on a query default.

``facts`` is a parameter and is never recomputed. The owner's ``atlas_facts``
ends by writing the regime-flip latch on a threaded runtime; a reasoner call
landing between a refresh and the next heartbeat tick would consume the flip and
``regime_review`` would never launch.

Absence is stated, never defaulted. A panel that failed, a panel that read
nothing, and an archive that holds nothing are three different facts, and the
prompt says which one it is. "The record is silent" and "we never looked" must
not read the same.

Nothing here is registered, persisted or dispatched. The production call site is
``UISession.atlas_reason``: it composes the evidence under the owner's lock,
resolves the model, calls :func:`reason`, and records the result as an
``atlas_view`` event plus one citation row per :class:`Citation`.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from qlab.news.archive import GROUNDING_VERSION, MIN_ARCHIVE_ITEMS
from qlab.operator.models import (Completion, CompletionRequest, ModelSpec,
                                  ProviderError, fits_context)
from qlab.operator.templates import TEMPLATES, TemplateNotAllowed, check_startable

# Bumped whenever the composed prompt changes meaning. Recorded on every view so
# a replay can refuse loudly rather than compare answers formed under different
# instructions.
REASONER_PROMPT_VERSION = "1"

# The chat answers beneath the question, inside a pane the client already draws.
ANSWER_MAX_LINES = 12
ANSWER_MAX_COLS = 96

# A twelve-line answer citing twenty records is a dump, not an answer; and
# silently keeping six of them would be the unmarked omission this module exists
# to prevent. Over the cap the whole view is refused.
MAX_CITATIONS = 6

TRUNCATION_MARKER = "… {n} more line(s) not shown"

_CITE_PREFIX = "CITE:"
_PROPOSE_PREFIX = "PROPOSE:"

# ProviderError is re-exported deliberately: callers of reason() must be able to
# catch the provider failure without importing the provider layer themselves.
__all__ = [
    "ANSWER_MAX_COLS", "ANSWER_MAX_LINES", "ArchiveEvidence", "Citation",
    "MAX_CITATIONS", "ParsedView", "ProviderError", "REASONER_PROMPT_VERSION",
    "ReasonedView", "ReasonerRefused", "TRUNCATION_MARKER", "answer",
    "compose_reasoner_prompt", "fit_answer", "offer_for", "parse_view", "reason",
]


class ReasonerRefused(RuntimeError):
    """The reasoner declined to produce a view, with the reason in the message.

    Distinct from :class:`ProviderError`: that means the model could not be
    reached, this means what came back may not be recorded as a view.
    """


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchiveEvidence:
    """The archive read, composed server-side and handed to the reasoner.

    ``matched_total`` is the size of the full match set, not of this page —
    a statistic that changed with ``offset`` would not be a fact about the
    record. ``as_of_source`` distinguishes an operator-supplied point in time
    from the owner's clock, because "as of the moment you asked" and "as of the
    decision being reviewed" are different reads of the same archive.
    """

    items: tuple[Mapping, ...]
    matched_total: int
    relevance: Mapping
    as_of: str
    as_of_source: str
    archive_begins: str | None = None

    def __post_init__(self) -> None:
        if self.as_of_source not in ("caller", "now"):
            raise ValueError(
                f"as_of_source must be 'caller' or 'now', not "
                f"{self.as_of_source!r}; an unattributed point in time is not a "
                "provenance record")
        if not str(self.as_of).strip():
            raise ValueError("as_of is required; a look-ahead boundary that is "
                             "absent is not a boundary")
        if int(self.matched_total) < len(self.items):
            raise ValueError(
                f"matched_total {self.matched_total} is below the {len(self.items)} "
                "row(s) handed over; the total must describe the full match set")

    @property
    def hashes(self) -> frozenset[str]:
        """Every record in the extract, citable or not."""
        return frozenset(_hash_of(i) for i in self.items if _hash_of(i))

    @property
    def citable_hashes(self) -> frozenset[str]:
        """The subset a view may cite: synthetic rows are excluded.

        Synthetic rows exist so the archive can be exercised without a provider.
        They are storable and searchable and they are never evidence.
        """
        return frozenset(_hash_of(i) for i in self.items
                         if _hash_of(i) and not bool(i.get("synthetic")))

    def item(self, item_hash: str) -> Mapping | None:
        for i in self.items:
            if _hash_of(i) == item_hash:
                return i
        return None

    @property
    def is_empty(self) -> bool:
        return not self.items and int(self.matched_total) == 0

    @property
    def not_established(self) -> tuple[str, ...]:
        """The relevance report's own statements, carried verbatim."""
        return tuple(str(s) for s in (self.relevance or {}).get(
            "not_established", ()) or ())

    @property
    def in_universe_tickers(self) -> tuple[str, ...] | None:
        """Holdings these records implicate, or ``None`` when unresolved.

        ``()`` means the report ran and found none — the Samsung case. ``None``
        means the report did not resolve holdings at all, which is a different
        fact and must not be read as "no holding".
        """
        rel = self.relevance or {}
        if "in_universe_tickers" not in rel:
            return None
        return tuple(str(t) for t in (rel.get("in_universe_tickers") or ()))


@dataclass(frozen=True)
class Citation:
    item_hash: str
    source: str
    published: str
    headline: str

    def to_dict(self) -> dict:
        return {"item_hash": self.item_hash, "source": self.source,
                "published": self.published, "headline": self.headline}


@dataclass(frozen=True)
class ParsedView:
    lines: tuple[str, ...]
    cited_hashes: tuple[str, ...]
    proposed_template: str | None


@dataclass(frozen=True)
class ReasonedView:
    """One formed view. Nothing here can carry an order.

    The field list is the invariant: no weight, no target, no notional, no leg,
    no plan id, no quantity. ``offer`` is a registered template id that
    ``check_startable`` has already permitted and that creates no plan.
    """

    question: str | None
    lines: tuple[str, ...]
    citations: tuple[Citation, ...]
    not_established: tuple[str, ...]
    offer: str | None
    offer_refused_reason: str | None
    model_id: str
    provider: str
    served_model: str | None
    prompt_version: str
    grounding_version: str
    stop_reason: str
    complete: bool

    def to_event_payload(self) -> dict:
        return {
            # None and "" are different facts: a question that was never asked
            # and a question that was empty.
            "question": self.question,
            "lines": list(self.lines),
            "citations": [c.to_dict() for c in self.citations],
            "citation_count": len(self.citations),
            "not_established": list(self.not_established),
            "offer": self.offer,
            "offer_refused_reason": self.offer_refused_reason,
            "model_id": self.model_id,
            "provider": self.provider,
            # Absent when the backend reported no served model. Not the
            # requested id — that would invent a fact the backend never gave.
            "served_model": self.served_model,
            "prompt_version": self.prompt_version,
            "grounding_version": self.grounding_version,
            "stop_reason": self.stop_reason,
            "complete": self.complete,
        }


def _hash_of(item: Mapping) -> str:
    return str(item.get("item_hash") or "")


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------

_SYSTEM = f"""\
You are Atlas, the desk manager of a governed ETF research desk. You form a view
from the desk context and an archive extract, and you say what the record does
and does not establish.

AUTHORITY — these are properties of the system, not requests:
- You have NO EXECUTION PATH. You cannot create, approve or execute a plan, you
  cannot place an order, and you cannot size or move a position. No tool you can
  reach does any of those things.
- You may propose at most ONE registered workflow template, or nothing. You
  cannot invent a template.
- NEVER state a weight, an allocation, a notional, a position size, or a
  percentage next to a ticker. An answer containing one is discarded in full,
  including its reasoning. Describe moves in words.

EVIDENCE:
- The ARCHIVE EXTRACT below is your only news. You cannot fetch and you cannot
  search. If it does not say something, the record does not establish it — say
  so plainly rather than reasoning past the gap.
- Cite only by the item_hash values listed in the extract, at most
  {MAX_CITATIONS} of them. A hash that is not listed is a fabricated citation.
- Records marked SYNTHETIC are fixtures. They may not be cited at all.
- Resolve relevance against the holdings named in the mandate. A story about a
  company the desk does not hold may still bear on a holding; say through what,
  or say that it does not.

FORMAT — plain lines, then optional markers, nothing else:
  <at most {ANSWER_MAX_LINES} lines of prose, each under {ANSWER_MAX_COLS} characters>
  CITE: <item_hash>[, <item_hash> ...]
  PROPOSE: <template_id>
Omit CITE when the record supports nothing. Omit PROPOSE when no registered
template fits; proposing nothing is a legitimate and frequent answer.
"""


def _fmt_optional(value, absent: str) -> str:
    if value is None:
        return absent
    if isinstance(value, str) and not value.strip():
        return absent
    return str(value)


def _regime_block(context: Mapping) -> list[str]:
    panel = context.get("regime_panel")
    if not isinstance(panel, Mapping):
        return ["REGIME PANEL: absent from the context — no panel state is "
                "established, and absence is not calm."]
    error = panel.get("error")
    if error:
        # A failed panel asserts nothing. Quoting the error and stopping is the
        # only honest rendering; a robust_state alongside it would be read.
        return [f"REGIME PANEL: FAILED — {error}",
                "  The panel asserts no state. Do not infer one from its silence."]
    readings = list(panel.get("readings") or ())
    if not readings:
        return ["REGIME PANEL: ran and read nothing — zero indicators reported.",
                "  This is not a calm reading; it is an absent one."]
    out = [f"REGIME PANEL: robust_state="
           f"{_fmt_optional(panel.get('robust_state'), 'not established')}"
           f" agreement={_fmt_optional(panel.get('agreement'), 'absent')}"
           f" disagreement={_fmt_optional(panel.get('disagreement'), 'absent')}"]
    for r in readings:
        out.append(
            f"  - {r.get('indicator')}: state="
            f"{_fmt_optional(r.get('state'), 'absent')} "
            f"signal={_fmt_optional(r.get('signal'), 'absent')} "
            f"threshold={_fmt_optional(r.get('threshold'), 'absent')} "
            f"percentile={_fmt_optional(r.get('percentile'), 'absent')}")
        reasoning = r.get("reasoning")
        if reasoning:
            out.append(f"    {reasoning}")
        flags = list(r.get("quality_flags") or ())
        if flags:
            out.append(f"    quality flags: {', '.join(str(f) for f in flags)}")
    return out


def _mandate_block(context: Mapping) -> list[str]:
    mandate = context.get("mandate")
    if not isinstance(mandate, Mapping):
        return ["MANDATE: absent from the context. The holdings are not "
                "established, so relevance cannot be resolved against them."]
    universe = list(mandate.get("universe") or ())
    out = [f"MANDATE: holdings = "
           f"{', '.join(str(t) for t in universe) if universe else 'none listed'}",
           f"  operational_policy = "
           f"{_fmt_optional(mandate.get('operational_policy'), 'not set')}",
           f"  max_weight_per_asset = "
           f"{_fmt_optional(mandate.get('max_weight_per_asset'), 'not set')}",
           f"  max_turnover_per_rebalance = "
           f"{_fmt_optional(mandate.get('max_turnover_per_rebalance'), 'not set')}"]
    profile = mandate.get("risk_profile")
    if profile is None:
        # Named rather than omitted: an omitted profile reads as a default the
        # reasoner may assume, and there is no default.
        out.append("  risk_profile = ABSENT — the operator has stated no risk "
                   "preference. Do not assume one.")
    else:
        out.append(f"  risk_profile = {profile}")
    return out


def _signals_block(context: Mapping) -> list[str]:
    qual = context.get("qualitative_signals")
    if not isinstance(qual, Mapping) or not qual:
        # The surface carried no block at all — a different fact from a block
        # that ran and produced nothing.
        return ["QUALITATIVE SIGNALS: the context carried none. Nothing about "
                "the record's shape is established here."]
    signals = list(qual.get("signals") or ())
    if not signals:
        return ["QUALITATIVE SIGNALS: the set is empty — the signals ran and "
                "reported nothing, which is not the same as reporting zero."]
    out = [f"QUALITATIVE SIGNALS (unsigned — they describe the record, never the "
           f"market; items={_fmt_optional(qual.get('item_count'), 'absent')} "
           f"sufficient={_fmt_optional(qual.get('sufficient'), 'absent')}):"]
    for s in signals:
        value = s.get("value")
        shown = "absent" if value is None else f"{value}"
        out.append(f"  - {s.get('name')}: {shown} [{s.get('state')}] "
                   f"{s.get('reason')}")
    return out


def _evidence_block(evidence: ArchiveEvidence) -> list[str]:
    begins = _fmt_optional(evidence.archive_begins,
                           "at a point the archive does not record")
    out = [f"ARCHIVE EXTRACT — as_of {evidence.as_of} "
           f"(source: {evidence.as_of_source}); "
           f"archive begins {begins}; "
           f"{evidence.matched_total} record(s) matched, "
           f"{len(evidence.items)} shown."]
    if not evidence.items:
        # The distinction the whole archive exists to preserve.
        out.append("  THE ARCHIVE HOLDS NOTHING FOR THIS QUESTION. This is the "
                   "record being empty, not the desk having failed to look. "
                   "Say so; do not reason from memory.")
    for item in evidence.items:
        mark = " [SYNTHETIC — NOT CITABLE]" if item.get("synthetic") else ""
        tickers = ", ".join(str(t) for t in (item.get("tickers") or ())) or "none"
        out.append(f"  - item_hash={_hash_of(item)}{mark}")
        out.append(f"    source={_fmt_optional(item.get('source'), 'absent')} "
                   f"published={_fmt_optional(item.get('published'), 'absent')} "
                   f"tickers={tickers}")
        out.append(f"    headline: {_fmt_optional(item.get('headline'), 'absent')}")
        body = item.get("body_text")
        if body:
            out.append(f"    body: {body}")
    return out


def _relevance_block(evidence: ArchiveEvidence) -> list[str]:
    rel = evidence.relevance or {}
    in_universe = evidence.in_universe_tickers
    if in_universe is None:
        head = ("RELEVANCE: the report did not resolve these records against the "
                "holdings. That is unresolved, not 'no holding'.")
    elif not in_universe:
        head = ("RELEVANCE: no holding in the mandate is implicated by these "
                "records.")
    else:
        head = ("RELEVANCE: holdings implicated — "
                + ", ".join(in_universe))
    out = [head]
    outside = rel.get("out_of_universe_terms")
    if outside:
        out.append("  searched for but not held: "
                   + ", ".join(str(t) for t in outside))
    state = rel.get("corroboration_state")
    if state:
        value = rel.get("corroboration_value")
        out.append(f"  corroboration: "
                   f"{'absent' if value is None else value} [{state}]")
    statements = evidence.not_established
    if statements:
        out.append("  WHAT THE RECORD DOES NOT ESTABLISH — carry these verbatim "
                   "into your answer where they bear on the question:")
        out.extend(f"    {s}" for s in statements)
    return out


def _startable_block(context: Mapping) -> list[str]:
    """The gate's verdict on the tasks already queued.

    ``startable_tasks`` reports queued tasks only, so an empty list means "no
    task is waiting", not "nothing may be started" — the two must not read the
    same, and whatever is proposed is re-checked by the gate regardless.
    """
    tail = ("Whatever you propose is re-checked by check_startable before "
            "anything starts.")
    startable = context.get("startable")
    if not startable:
        return ["QUEUED WORK: no task is waiting. That is not the same as "
                "nothing being startable.", f"  {tail}"]
    out = ["QUEUED WORK (the gate's own verdict on each waiting task):"]
    for entry in startable:
        if isinstance(entry, Mapping):
            allowed = entry.get("startable")
            out.append(
                f"  - {entry.get('template_id')}: "
                f"{'startable' if allowed else 'refused'}"
                + (f" — {entry.get('reason')}" if entry.get("reason") else ""))
        else:
            out.append(f"  - {entry}")
    out.append(f"  {tail}")
    return out


def _authority_block(context: Mapping) -> list[str]:
    """The deterministic gate's own verdict, carried verbatim.

    `atlas_context` documents this as "carried verbatim so the reasoner can see
    exactly what the deterministic layer will and will not permit" — and then
    the prompt dropped it. The reasoner argued outside its authority and the
    gate refused it afterwards, so the operator read a suggestion followed by
    a contradiction instead of one honest answer.

    Invariant 4 is enforced here rather than assumed: an ineligible desk whose
    gate stated no reason is rendered as an UNEXPLAINED refusal, so the view
    can say the refusal is unexplained instead of inventing a cause for it.
    """
    facts = context.get("gate_facts")
    if not isinstance(facts, Mapping) or not facts:
        return ["AUTHORITY: the gate's verdict did not reach this prompt. What "
                "the deterministic layer will permit is not established, so do "
                "not assert that anything is or is not allowed."]

    data = facts.get("data") if isinstance(facts.get("data"), Mapping) else {}
    eligible = data.get("eligible_for_paper_proposal")
    reason = data.get("reason")

    out = ["AUTHORITY — the deterministic gate's own verdict. You reason "
           "WITHIN this; it is not advice and you cannot argue past it:"]
    out.append(f"  data provider = {_fmt_optional(data.get('provider'), 'absent')}"
               f" blocked={_fmt_optional(data.get('blocked'), 'not established')}")

    if eligible:
        out.append("  paper proposals: ALLOWED by the gate right now.")
    elif eligible is None:
        out.append("  paper proposals: the gate did not say. Treat as not "
                   "established, never as allowed.")
    elif reason:
        out.append(f"  paper proposals: REFUSED — {reason}")
        out.append("  Say this reason plainly if the question bears on it; it "
                   "is the part the operator can act on.")
    else:
        # A refusal with no reason is the invariant-4 violation itself. Named
        # rather than smoothed over, so the answer can report the gap.
        out.append("  paper proposals: REFUSED, and the gate stated NO REASON. "
                   "This refusal is UNEXPLAINED. Do not invent a cause for it; "
                   "say that the desk is refusing without stating why.")

    out.append(f"  open workflows = "
               f"{_fmt_optional(facts.get('open_workflows'), 'absent')}"
               f"; pending approvals = "
               f"{_fmt_optional(facts.get('pending_approvals'), 'absent')}"
               f"; order anomaly = "
               f"{_fmt_optional(facts.get('order_anomaly'), 'not established')}")

    regime = facts.get("regime") if isinstance(facts.get("regime"), Mapping) else {}
    if regime:
        out.append(f"  the gate reads regime as "
                   f"{_fmt_optional(regime.get('robust_state'), 'not established')}"
                   f" (flip={_fmt_optional(regime.get('flip'), 'not established')})")
    return out


def _archive_depth_block(context: Mapping) -> list[str]:
    """How much record there is to reason from at all.

    A view drawn from nine rows and one drawn from nine thousand deserve
    different confidence, and only this number tells the model which it holds.
    Synthetic rows are counted separately because they are not citable.
    """
    stats = context.get("archive")
    if not isinstance(stats, Mapping) or not stats:
        return ["ARCHIVE DEPTH: not reported. How much record stands behind "
                "this extract is unknown."]
    rows = stats.get("rows")
    if not rows:
        return ["ARCHIVE DEPTH: the archive holds nothing — zero records, no "
                "span. Every answer here is unsupported by the record, and "
                "that is what to say."]
    out = [f"ARCHIVE DEPTH: {rows} record(s) in total, spanning "
           f"{_fmt_optional(stats.get('begins'), 'an unrecorded start')} to "
           f"{_fmt_optional(stats.get('newest_published'), 'an unrecorded end')}."]
    synthetic = stats.get("synthetic_rows")
    if synthetic:
        out.append(f"  {synthetic} of those are SYNTHETIC and are not citable "
                   f"evidence. Judge the record's thickness by the rest.")
    return out


def _book_block(context: Mapping) -> list[str]:
    """The desk itself — equity, drawdown, the kill switch, what is held.

    `atlas_context` composed this from the moment the surface was written and
    the prompt rendered none of it, so a model asked "how is my book doing"
    answered from news and a regime panel. The absence rules are the panel's:
    a book that could not be valued and a book that holds nothing are
    different facts and must not render alike.

    The weights are shown because a view formed without them is a view about
    a different desk, and withheld as vocabulary because `_refuse_weights`
    refuses any view that states one. The caution rides in the block itself:
    a model that only sees the numbers reads them as permission.
    """
    port = context.get("portfolio")
    if not isinstance(port, Mapping) or not port:
        return ["THE BOOK: absent — the context carried no portfolio. What the "
                "desk holds is NOT established. This is a valuation that did "
                "not arrive, never an empty or flat book; do not answer a "
                "question about the book from it."]

    equity = port.get("equity")
    drawdown = port.get("drawdown")
    halted = bool(port.get("halted"))
    kill = port.get("kill_switch_at")

    head = (f"THE BOOK: broker={_fmt_optional(port.get('broker'), 'absent')} "
            f"equity={_fmt_money(equity)} cash={_fmt_money(port.get('cash'))} "
            f"high_water_mark={_fmt_money(port.get('high_water_mark'))}")
    out = [head, f"  drawdown from the high-water mark: {_fmt_pct(drawdown)} "
                 f"(kill switch at {_fmt_pct(kill)})"]

    if halted:
        # The single most consequential fact on the desk. As a bare boolean it
        # reads as one field among twelve, so it is stated as a condition.
        out.append("  *** THE BOOK IS HALTED — the kill switch has fired. "
                   "Trading is stopped and no new risk may be taken. Any "
                   "answer that bears on positioning must say this first. ***")
    elif (isinstance(drawdown, (int, float))
          and isinstance(kill, (int, float)) and kill > 0):
        out.append(f"  the kill switch has NOT fired; distance to it: "
                   f"{_fmt_pct(kill - drawdown)}")

    positions = port.get("positions") or {}
    weights = port.get("weights") or {}
    if not positions:
        out.append("  the book holds no position — it is in cash. This is a "
                   "book that was read and found flat, not a missing one.")
    else:
        out.append("  held (weight, and unrealised P&L in the book's currency):")
        for ticker in sorted(positions):
            row = positions[ticker] if isinstance(positions[ticker], Mapping) else {}
            out.append(
                f"    - {ticker}: weight={_fmt_pct(weights.get(ticker))} "
                f"unrealised={_fmt_money(row.get('unrealized_pl'))}")

    targets = port.get("target_weights") or {}
    if targets:
        out.append("  the last recorded decision targeted: "
                   + ", ".join(f"{t} {_fmt_pct(w)}" for t, w in sorted(targets.items())))

    # Stated inside the block, next to the numbers it governs, because the
    # system prompt's rule is read once and this block is read as data.
    out.append("  These figures are CONTEXT for your judgment. Do not repeat "
               "any weight, allocation, notional or position size in your "
               "answer — describe the book in words. A view that states one "
               "is refused before the operator ever sees it.")
    return out


def _predictors_block(context: Mapping) -> list[str]:
    """The predictor board — the desk's forward-looking research evidence.

    Advisory by construction: the gate never reads it, and a champion here is
    an admitted model, never a promoted one. Rendering it lets an operator ask
    whether the augmented lane is earning its place and get an answer drawn
    from the board rather than from the news.
    """
    board = context.get("predictors")
    if not isinstance(board, Mapping) or not board:
        return ["PREDICTOR BOARD: the context carried none. Whether any "
                "predictor has been evaluated is not established."]

    status = board.get("status")
    if status == "never_ran":
        return ["PREDICTOR BOARD: never been run on this desk. No predictor "
                "has been evaluated, which is not the same as one having been "
                "evaluated and rejected."]
    if status == "unreadable":
        return [f"PREDICTOR BOARD: the newest board "
                f"(run_id={_fmt_optional(board.get('run_id'), 'absent')}) "
                f"could not be read. Its result is unknown, not absent."]

    admitted = board.get("admitted_any")
    out = [f"PREDICTOR BOARD (advisory — the authority gate never reads it; "
           f"an admitted model is not a promoted one): "
           f"as_of={_fmt_optional(board.get('as_of'), 'absent')} "
           f"age_days={_fmt_optional(board.get('age_days'), 'absent')} "
           f"source={_fmt_optional(board.get('source'), 'absent')}"]

    # What the lane IS, in the words an operator uses to ask about it. The
    # board speaks in model_ids; "is the quantum feature augmentation earning
    # its place" is answerable from `kernel:angle` only if something states
    # that the kernel and groupwise families ARE that augmentation and that
    # ridge:none is the unaugmented control they are measured against.
    out.append(
        "  the lane: `kernel:*` and `groupwise:*` are the quantum "
        "feature-map augmented models (angle and ZZ feature maps, simulated "
        "classically); `ridge:none` is the unaugmented control. A kernel "
        "model ranking above the baseline is the augmentation earning its "
        "place; below it, the augmentation is costing accuracy.")

    folds = board.get("n_folds")
    n_obs = board.get("n_obs")
    if folds is not None or n_obs is not None:
        out.append(
            f"  evaluated on {_fmt_optional(n_obs, 'an unrecorded number of')}"
            f" observations across "
            f"{_fmt_optional(folds, 'an unrecorded number of')} purged "
            f"walk-forward folds")

    admission = board.get("admission")
    if isinstance(admission, Mapping) and admission:
        out.append(
            f"  admission bar: mean_ic strictly above "
            f"{_fmt_optional(admission.get('mean_ic_strictly_above'), 'unknown')}"
            f" AND ic_stability strictly above "
            f"{_fmt_optional(admission.get('ic_stability_strictly_above'), 'unknown')}")
    else:
        # An older run predates the field. Rendering today's default would
        # state a threshold this run never used, which is worse than unknown.
        out.append("  admission bar: NOT RECORDED by this run. Whether a "
                   "model is `usable` was decided against a threshold that "
                   "is unknown here, so the verdict cannot be re-derived.")

    baseline = board.get("baseline")
    if isinstance(baseline, Mapping):
        out.append("  baseline: " + _predictor_line(baseline, board))
    else:
        out.append("  baseline: absent — nothing to measure a candidate against.")

    champion = board.get("champion")
    if isinstance(champion, Mapping):
        out.append("  champion: " + _predictor_line(champion, board))
        out.extend(_champion_caveats(champion, board))
    else:
        out.append("  champion: no model was admitted. The board ran and "
                   "admitted nothing — that is its result, not a missing value.")

    if not admitted and isinstance(champion, Mapping):
        out.append("  admitted_any is false despite a champion row; treat the "
                   "admission as not established.")

    delta = board.get("best_delta_vs_baseline")
    if delta is not None:
        out.append(f"  best delta in mean IC vs the baseline: {delta}"
                   + ("  (negative — no candidate beat the baseline)"
                      if isinstance(delta, (int, float)) and delta < 0 else ""))
    ranking = list(board.get("ranking") or ())
    if ranking:
        out.append("  ranked: " + ", ".join(str(m) for m in ranking))
    return out


def _champion_caveats(champion: Mapping, board: Mapping) -> list[str]:
    """Everything about the champion that its headline mean IC conceals.

    The live desk admitted `kernel:angle` on mean_ic 0.178 against a 0.03 bar,
    which reads as a decisive win for the augmented lane. The same run scored
    a paired t of 0.237 and was negative in two of its five folds. Both were
    computed; only the flattering one was rendered, so an operator asking
    whether the quantum lane works got a yes the evidence does not support.
    """
    out: list[str] = []

    admission = board.get("admission")
    if isinstance(admission, Mapping):
        # `usable: true` is a comparison whose threshold was invisible, so a
        # model that scraped the bar read identically to one that cleared it
        # by a mile. The margin is the difference between those two readings.
        for field, bar_key in (("mean_ic", "mean_ic_strictly_above"),
                               ("ic_stability", "ic_stability_strictly_above")):
            value, bar = champion.get(field), admission.get(bar_key)
            if not isinstance(value, (int, float)) or not isinstance(bar, (int, float)):
                continue
            margin = value - bar
            note = "  (scraped the bar)" if 0 < margin < 0.1 * max(abs(bar), 1e-9) else ""
            out.append(f"    {field} margin over the bar: {margin:+.4f}{note}")

    folds = board.get("n_folds")
    t_stat = champion.get("paired_t_vs_baseline")
    if isinstance(t_stat, (int, float)):
        # A t with no n is not evidence. Five folds cannot separate this
        # champion from the baseline at any conventional level, and a reader
        # shown the bare 0.237 will take it for a result.
        if isinstance(folds, int) and folds > 1:
            out.append(
                f"    paired t vs the baseline is {t_stat} over {folds} "
                f"folds. |t| below ~2 on {folds} folds is not significant: "
                f"this cannot distinguish the champion from the baseline.")
        else:
            out.append(
                f"    paired t vs the baseline is {t_stat}, over an "
                f"unrecorded number of folds — a t with no n is not "
                f"significant evidence, it is a ratio.")

    wins = champion.get("wins_vs_baseline")
    if isinstance(wins, int) and isinstance(folds, int) and folds:
        out.append(f"    beat the baseline in {wins} of {folds} folds")

    per_fold = [f.get("ic") for f in (champion.get("per_fold") or ())
                if isinstance(f, Mapping) and isinstance(f.get("ic"), (int, float))]
    if per_fold:
        negative = [ic for ic in per_fold if ic < 0]
        out.append("    per-fold IC: "
                   + ", ".join(f"{ic:+.3f}" for ic in per_fold))
        if negative:
            # A mean over folds that flip sign is not a skill estimate.
            out.append(
                f"    NEGATIVE in {len(negative)} of {len(per_fold)} folds. "
                f"The mean IC is an average over folds that changed sign, so "
                f"it is not a stable estimate of skill.")
    return out


def _predictor_line(entry: Mapping, board: Mapping | None = None) -> str:
    """One model's row.

    `usable` is the board's own admission verdict, so it is stated as such
    rather than as a property of the model: whether it is admissible depends
    on a threshold, and the threshold is rendered beside it.
    """
    usable = entry.get("usable")
    verdict = ("ADMITTED" if usable is True else
               "not admitted" if usable is False else "not established")
    return (f"{_fmt_optional(entry.get('model_id'), 'absent')} "
            f"mean_ic={_fmt_optional(entry.get('mean_ic'), 'absent')} "
            f"ic_std={_fmt_optional(entry.get('ic_std'), 'absent')} "
            f"ic_stability={_fmt_optional(entry.get('ic_stability'), 'absent')} "
            f"-> {verdict} "
            f"paired_t_vs_baseline="
            f"{_fmt_optional(entry.get('paired_t_vs_baseline'), 'not computed (it IS the baseline)')}")


def _decisions_block(context: Mapping) -> list[str]:
    """What the desk did lately, and what came of it.

    An outcome the reflection loop has not resolved yet is unresolved, never
    neutral: a model that reads a missing outcome as "it went fine" learns the
    opposite of the lesson.
    """
    decisions = list(context.get("recent_decisions") or ())
    if not decisions:
        return ["RECENT DECISIONS: none on the record. The desk has decided "
                "nothing yet, or its history did not load; either way there "
                "is no past decision to reason from."]
    out = ["RECENT DECISIONS (newest first; an absent outcome is UNRESOLVED, "
           "never a good one):"]
    for d in decisions:
        if not isinstance(d, Mapping):
            out.append(f"  - {d}")
            continue
        out.append(f"  - {_fmt_optional(d.get('as_of'), 'undated')} "
                   f"{_fmt_optional(d.get('kind'), 'unknown kind')}: "
                   f"{_fmt_optional(d.get('rationale'), 'no rationale recorded')}")
        outcome = d.get("outcome")
        out.append(f"    outcome: {outcome}" if outcome else
                   "    outcome: unresolved — the reflection loop has not "
                   "scored this decision yet.")
    return out


def _fmt_money(value) -> str:
    if value is None:
        return "absent"
    if isinstance(value, (int, float)):
        return f"{value:,.2f}"
    return str(value)


def _fmt_pct(value) -> str:
    if value is None:
        return "absent"
    if isinstance(value, (int, float)):
        return f"{value * 100:.2f}%"
    return str(value)


def compose_reasoner_prompt(*, context: Mapping, evidence: ArchiveEvidence,
                            question: str | None, spec: ModelSpec,
                            max_output_tokens: int = 1200) -> CompletionRequest:
    """Build the one request. Overflow refuses; it never trims the evidence.

    Dropping records to fit would silently unbind a citation from its evidence,
    which is worse than no answer at all.
    """
    if question is not None and not str(question).strip():
        raise ValueError(
            "question must be None (a standing view) or non-empty; an empty "
            "string is neither a question asked nor a question withheld")

    blocks: list[str] = []
    if question is None:
        blocks.append("QUESTION: no question was asked. Form the standing desk "
                      "view: what changed, what it bears on, and what is still "
                      "open.")
    else:
        blocks.append(f"QUESTION: {question}")
    blocks.append("")
    blocks.extend(_mandate_block(context))
    blocks.append("")
    # What the deterministic layer permits, before any judgment is formed on
    # top of it: a view argued outside its authority is refused anyway.
    blocks.extend(_authority_block(context))
    blocks.append("")
    # The desk itself, high up: most questions put to Atlas are about the book,
    # and a view formed from news alone is a view about someone else's desk.
    blocks.extend(_book_block(context))
    blocks.append("")
    blocks.extend(_regime_block(context))
    blocks.append("")
    blocks.extend(_signals_block(context))
    blocks.append("")
    blocks.extend(_archive_depth_block(context))
    blocks.append("")
    blocks.extend(_evidence_block(evidence))
    blocks.append("")
    blocks.extend(_relevance_block(evidence))
    blocks.append("")
    blocks.extend(_predictors_block(context))
    blocks.append("")
    blocks.extend(_decisions_block(context))

    tensions = list(context.get("tensions") or ())
    if tensions:
        blocks.append("")
        blocks.append("TENSIONS the desk has already recorded:")
        blocks.extend(f"  - {t}" for t in tensions)

    supported = list(context.get("supported_claims") or ())
    if supported:
        blocks.append("")
        blocks.append("CLAIMS THE GROUNDED WINDOW SUPPORTS:")
        for claim in supported:
            text = (claim.get("headline") if isinstance(claim, Mapping)
                    else claim)
            blocks.append(f"  - {text}")

    blocks.append("")
    blocks.extend(_startable_block(context))

    prompt = "\n".join(blocks)

    # The budget is checked against what the provider will actually send — the
    # system text folded in — and it refuses. Sampling the evidence to fit would
    # unbind a citation from its record, which is worse than no answer.
    fits, why = fits_context(f"{_SYSTEM}\n\n{prompt}", spec)
    if not fits:
        raise ReasonerRefused(
            f"the composed prompt does not fit {spec.id!r}: {why}")

    return CompletionRequest(
        model=spec,
        system=_SYSTEM,
        prompt=prompt,
        max_output_tokens=int(max_output_tokens),
    )


# --------------------------------------------------------------------------
# Parsing, and the things a view may not say
# --------------------------------------------------------------------------

# A number shaped like a portfolio weight: a percentage, or a bare fraction.
_WEIGHT_NUMBER = re.compile(r"\d{1,3}(?:\.\d+)?\s*%|(?<![\d.])0?\.\d{1,4}(?!\d)")

# Any number at all. A notional is a currency amount and a position size is a
# share count, so the keyword rule cannot require weight shape.
_ANY_NUMBER = re.compile(r"\d")

# Words that turn a number into an instruction about a position.
_WEIGHT_WORDS = re.compile(
    r"\b(?:target[ -]?weights?|weights?|allocations?|allocate[sd]?|allocating|"
    r"notional|position[ -]size[sd]?|overweight|underweight|rebalance to)\b",
    re.IGNORECASE)

# How close a number must be to a ticker to be read as that ticker's weight.
_ADJACENCY = 24


def _known_tickers(evidence: ArchiveEvidence) -> frozenset[str]:
    """Tickers this extract is about — the vocabulary the weight guard uses.

    Drawn from the relevance report and the rows themselves rather than from the
    mandate, because ``parse_view`` is handed evidence and nothing else.
    ``out_of_universe_terms`` is deliberately excluded: those are query words,
    not symbols, and treating "surge" as a ticker would refuse ordinary prose.
    """
    out: set[str] = set()
    rel = evidence.relevance or {}
    for key in ("in_universe_tickers", "universe"):
        for t in rel.get(key) or ():
            token = str(t).strip().upper()
            if len(token) >= 2:
                out.add(token)
    for item in evidence.items:
        for t in item.get("tickers") or ():
            token = str(t).strip().upper()
            if len(token) >= 2:
                out.add(token)
    return frozenset(out)


def _refuse_weights(text: str, evidence: ArchiveEvidence) -> None:
    """Raise when the text sizes a position, however it is phrased.

    Deliberately over-inclusive near a ticker: a legitimate "SPY fell 1.4%" is
    refused too. The system prompt tells the model to describe moves in words,
    and a false refusal costs an answer while a false pass costs the boundary.
    """
    tickers = _known_tickers(evidence)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        keyword = _WEIGHT_WORDS.search(line)
        if keyword and _ANY_NUMBER.search(line):
            raise ReasonerRefused(
                f"the view sizes a position — {keyword.group(0)!r} alongside a "
                f"number in {line!r}; a view may propose a registered template "
                "or nothing, never a weight")
        for ticker in tickers:
            for match in re.finditer(rf"\b{re.escape(ticker)}\b", line,
                                     re.IGNORECASE):
                if keyword:
                    raise ReasonerRefused(
                        f"the view applies {keyword.group(0)!r} to {ticker} in "
                        f"{line!r}; a view may propose a registered template or "
                        "nothing, never a weight")
                lo = max(0, match.start() - _ADJACENCY)
                hi = min(len(line), match.end() + _ADJACENCY)
                if _WEIGHT_NUMBER.search(line[lo:hi]):
                    raise ReasonerRefused(
                        f"the view puts a weight-shaped number next to {ticker} "
                        f"in {line!r}; a view may propose a registered template "
                        "or nothing, never a weight")


def parse_view(text: str, *, evidence: ArchiveEvidence) -> ParsedView:
    """Split one completion into prose, citations and at most one proposal.

    Every citation is validated against the evidence set here, before anything
    downstream can record it.
    """
    _refuse_weights(text, evidence)

    prose: list[str] = []
    cited: list[str] = []
    proposed: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        upper = line.upper()
        if upper.startswith(_CITE_PREFIX):
            for token in line[len(_CITE_PREFIX):].replace(";", ",").split(","):
                token = token.strip()
                if token and token not in cited:
                    cited.append(token)
            continue
        if upper.startswith(_PROPOSE_PREFIX):
            candidate = line[len(_PROPOSE_PREFIX):].strip()
            if proposed is not None and candidate and candidate != proposed:
                raise ReasonerRefused(
                    f"the view proposes both {proposed!r} and {candidate!r}; a "
                    "view carries at most one proposal")
            proposed = candidate or proposed
            continue
        prose.append(line)

    while prose and not prose[0]:
        prose.pop(0)
    while prose and not prose[-1]:
        prose.pop()

    if len(cited) > MAX_CITATIONS:
        raise ReasonerRefused(
            f"the view cites {len(cited)} records against a cap of "
            f"{MAX_CITATIONS}; keeping the first {MAX_CITATIONS} would be an "
            "unmarked omission, so the view is refused instead")

    known = evidence.hashes
    citable = evidence.citable_hashes
    for item_hash in cited:
        if item_hash not in known:
            raise ReasonerRefused(
                f"the view cites {item_hash!r}, which is not in the archive "
                "extract it was handed; a citation to a record the archive does "
                "not hold is a fabricated citation")
        if item_hash not in citable:
            raise ReasonerRefused(
                f"the view cites {item_hash!r}, a synthetic row; synthetic "
                "records are storable and searchable but are never evidence")

    return ParsedView(lines=tuple(prose), cited_hashes=tuple(cited),
                      proposed_template=proposed or None)


# --------------------------------------------------------------------------
# The offer: a fixed mapping, then the gate
# --------------------------------------------------------------------------

def offer_for(*, parsed: ParsedView, evidence: ArchiveEvidence,
              mode: str, facts: Mapping,
              context: Mapping | None = None,
              question: str | None = None) -> tuple[str | None, str | None]:
    """Choose at most one candidate template, then let the gate decide.

    The candidate rules are fixed and ordered — the reasoner argues *for* a
    registered template, it does not compose the choice. ``check_startable`` is
    then the authority, and its refusal is returned rather than swallowed.

    ``context`` and ``question`` are optional because the ordered rules need the
    panel readings and the subject of the question; without them the regime rule
    simply cannot fire, which is a refusal to guess rather than a default.
    """
    candidate, reason_text = _candidate_for(
        parsed=parsed, evidence=evidence, context=context, question=question)
    if candidate is None:
        return None, reason_text

    try:
        template = check_startable(candidate, mode, dict(facts))
    except TemplateNotAllowed as exc:
        return None, str(exc)

    # Second gate. check_startable already refuses a plan-creating template below
    # propose mode; this holds even in propose mode and even if a later template
    # is mis-registered. A reasoned view never offers to create a plan.
    if template.creates_plan:
        return None, (
            f"{candidate!r} creates a paper plan; a reasoned view never offers "
            "one in any mode, and this is checked after the gate as well as by it")
    return candidate, None


def _candidate_for(*, parsed: ParsedView, evidence: ArchiveEvidence,
                   context: Mapping | None,
                   question: str | None) -> tuple[str | None, str | None]:
    in_universe = evidence.in_universe_tickers

    # (1) Nothing held is implicated — the Samsung case. No template, and the
    # reason is the answer rather than a shrug.
    if in_universe is None:
        return None, ("the relevance report did not resolve these records "
                      "against the holdings, so no work can be justified by them")
    if not in_universe:
        return None, "no holding is implicated by these records"

    # (2) The model argued for a registered template.
    proposed = parsed.proposed_template
    if proposed:
        if proposed in TEMPLATES:
            return proposed, None
        # Falling through to a default here would silently substitute a template
        # the model did not ask for. Atlas may argue; it may not invent.
        return None, (f"the view proposes {proposed!r}, which is not a "
                      f"registered template; Atlas may argue for a registered "
                      f"template but cannot invent one")

    # (3) Enough of the record, about something held.
    if int(evidence.matched_total) >= MIN_ARCHIVE_ITEMS:
        return "news_read", None

    # (4) The question is about the regime and the panel actually read something.
    if question and "regime" in question.lower():
        panel = (context or {}).get("regime_panel")
        readings = (list(panel.get("readings") or ())
                    if isinstance(panel, Mapping) else [])
        if readings:
            return "regime_review", None

    # (5) Nothing fits, and proposing nothing is a legitimate answer.
    return None, None


# --------------------------------------------------------------------------
# Fitting the answer to the pane
# --------------------------------------------------------------------------

def fit_answer(lines: Sequence[str], *, max_lines: int = ANSWER_MAX_LINES,
               max_cols: int = ANSWER_MAX_COLS) -> tuple[str, ...]:
    """Hard-wrap and cap, marking any drop.

    The absence of :data:`TRUNCATION_MARKER` is itself a claim that nothing was
    dropped, so it is never omitted when something was and never present when
    nothing was.
    """
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    if max_cols < 8:
        raise ValueError("max_cols must leave room for a word")

    wrapped: list[str] = []
    for line in lines:
        text = str(line)
        if not text.strip():
            # A blank separator is content: dropping it would silently reflow
            # the answer into a different shape.
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(text, width=max_cols, break_long_words=True,
                                     break_on_hyphens=False) or [""])
    if len(wrapped) <= max_lines:
        return tuple(wrapped)
    kept = wrapped[:max_lines - 1]
    dropped = len(wrapped) - len(kept)
    return tuple(kept) + (TRUNCATION_MARKER.format(n=dropped),)


# --------------------------------------------------------------------------
# The two entry points
# --------------------------------------------------------------------------

def reason(*, context: Mapping, evidence: ArchiveEvidence,
           question: str | None, mode: str, facts: Mapping,
           spec: ModelSpec,
           complete: Callable[[CompletionRequest], Completion]) -> ReasonedView:
    """Form one view. One request, one completion, no streaming, no retry.

    ``ProviderError`` propagates untouched: a second model would produce a claim
    the audit could not attribute, and retrying the same model is the caller's
    decision to make and to record.
    """
    request = compose_reasoner_prompt(
        context=context, evidence=evidence, question=question, spec=spec)

    completion = complete(request)

    if completion.is_silent:
        raise ReasonerRefused(
            f"{spec.id} finished its turn saying nothing; a silent non-answer "
            "is recorded as a failed invocation, never as a view")
    if not (completion.text or "").strip():
        # `is_silent` is bound to end_turn, so an empty refusal and an empty
        # truncation slip past it — the same non-answer under another label.
        raise ReasonerRefused(
            f"{spec.id} returned no text with stop_reason="
            f"{completion.stop_reason!r}; there is no view to record")

    parsed = parse_view(completion.text, evidence=evidence)

    stop_reason = str(completion.stop_reason or "")
    is_complete = stop_reason != "max_tokens"

    if is_complete:
        offer, offer_refused = offer_for(
            parsed=parsed, evidence=evidence, mode=mode, facts=facts,
            context=context, question=question)
    else:
        # The rationale is the deliverable. A proposal whose reasoning was cut
        # off mid-sentence is not a proposal.
        offer, offer_refused = None, (
            f"the answer was truncated at the output-token limit "
            f"(stop_reason={stop_reason!r}); a proposal that lost its reasoning "
            "is not offered")

    citations = []
    for item_hash in parsed.cited_hashes:
        item = evidence.item(item_hash) or {}
        citations.append(Citation(
            item_hash=item_hash,
            source=str(item.get("source") or ""),
            published=str(item.get("published") or ""),
            headline=str(item.get("headline") or "")))

    lines = parsed.lines
    if evidence.is_empty:
        # Guaranteed here rather than trusted to the model: an empty archive
        # must read as an empty record, not as a quiet answer from memory.
        missing = tuple(s for s in evidence.not_established
                        if not any(s in line for line in lines))
        lines = missing + lines

    return ReasonedView(
        question=question,
        lines=tuple(lines),
        citations=tuple(citations),
        not_established=evidence.not_established,
        offer=offer,
        offer_refused_reason=offer_refused,
        model_id=spec.id,
        provider=spec.provider,
        # "" is not a served model; absence is recorded as absence.
        served_model=completion.raw_model or None,
        prompt_version=REASONER_PROMPT_VERSION,
        grounding_version=GROUNDING_VERSION,
        stop_reason=stop_reason,
        complete=is_complete,
    )


def answer(*, context: Mapping, evidence: ArchiveEvidence, question: str,
           mode: str, facts: Mapping, spec: ModelSpec,
           complete: Callable[[CompletionRequest], Completion]) -> ReasonedView:
    """The ask seam: a required question, and a reply bounded to the pane."""
    if question is None or not str(question).strip():
        raise ValueError(
            "answer() requires a question; a standing view with no question is "
            "reason(question=None), which is a different fact")
    view = reason(context=context, evidence=evidence, question=question,
                  mode=mode, facts=facts, spec=spec, complete=complete)
    return replace(view, lines=fit_answer(view.lines))
