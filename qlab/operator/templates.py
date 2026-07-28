"""The registered workflow templates Atlas may start, and nothing else.

Atlas does not compose arbitrary work. In Research mode it may start one of these
named templates, chosen deterministically by the trigger that woke it; the
interpreting agent may argue for a different registered template but cannot
invent one. Each template declares what it needs before it can start and what
authority it carries — and none of them carry execution authority.

``creates_plan`` is the authority boundary: in Research mode a template that
would create a paper plan is refused. Propose mode (a later phase) is what
admits ``desk_rebalance_review``, and even then the plan is a *proposal* that
still requires a persisted human approval to execute.

``phases`` is an executable contract, not a description. Every declared graph is
validated against the registry's dependency DAG by the test suite, because a
graph that omits a dependency creates a workflow that can never terminate and an
Atlas task that waits on it forever. A research-only graph that stops before the
optimizer is *not* expressible: the referee checks targets, so it structurally
depends on the optimizer producing them. Templates that want a referee therefore
declare the full chain, which is what actually runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkflowTemplate:
    template_id: str
    purpose: str
    phases: tuple[str, ...]
    requires: tuple[str, ...] = ()
    creates_plan: bool = False
    needs_coordinator: bool = True
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id, "purpose": self.purpose,
            "phases": list(self.phases), "requires": list(self.requires),
            "creates_plan": self.creates_plan,
            "needs_coordinator": self.needs_coordinator, "notes": self.notes,
        }


TEMPLATES: dict[str, WorkflowTemplate] = {
    "desk_brief": WorkflowTemplate(
        template_id="desk_brief",
        purpose="Summarize data health, book, drift, regime, and open items.",
        phases=(),
        needs_coordinator=False,
        notes="Deterministic: assembled from owner facts, no LLM required."),
    "regime_review": WorkflowTemplate(
        template_id="regime_review",
        purpose="Re-read the regime panel and challenge the current estimate.",
        phases=("analyst", "challenger", "optimizer", "referee", "reporter"),
        requires=("data_eligible_for_research",)),
    "estimation_panel": WorkflowTemplate(
        template_id="estimation_panel",
        purpose="Run competing estimator variants and adjudicate a winner.",
        phases=("analyst", "challenger", "optimizer", "judge", "referee",
                "reporter"),
        requires=("data_eligible_for_research",)),
    "research_review": WorkflowTemplate(
        template_id="research_review",
        purpose="Review a terminal research run against the champion policy.",
        phases=("analyst", "challenger", "optimizer", "referee", "reporter"),
        requires=("data_eligible_for_research",)),
    "risk_event": WorkflowTemplate(
        template_id="risk_event",
        purpose="Analyze a drawdown-tier or kill-switch event and brief the human.",
        phases=("analyst", "challenger", "optimizer", "referee", "reporter"),
        requires=("data_eligible_for_research",)),
    "news_read": WorkflowTemplate(
        template_id="news_read",
        purpose="Interpret the grounded news window: what the record supports, "
                "what is merely circulating, and what it bears on.",
        phases=("news-analyst",),
        requires=("grounded_news_window",),
        notes="Atlas's qualitative helper. Reads a window it is handed; it "
              "cannot fetch, forecast, or propose."),
    "news_risk_review": WorkflowTemplate(
        template_id="news_risk_review",
        purpose="Turn operator-pasted excerpts into dry, corroborated risk views.",
        phases=("analyst", "challenger", "optimizer", "referee", "reporter"),
        requires=("data_eligible_for_research", "operator_supplied_excerpts")),
    "desk_rebalance_review": WorkflowTemplate(
        template_id="desk_rebalance_review",
        purpose="Full review ending in a checked plan proposed for approval.",
        phases=("analyst", "challenger", "optimizer", "referee", "reporter"),
        requires=("data_eligible_for_paper_proposal",),
        creates_plan=True,
        notes="Propose mode only; the plan still requires a human approval."),
}

# Which template each deterministic trigger maps to. A trigger with no mapping
# never starts a workflow.
TRIGGER_TEMPLATE: dict[str, str] = {
    "owner_startup": "desk_brief",
    "data_recovered": "desk_brief",
    "regime_flip": "regime_review",
    "drift_breach": "desk_rebalance_review",
    "drawdown_warning": "risk_event",
    "drawdown_control": "risk_event",
    "kill_switch": "risk_event",
    "new_research_run": "research_review",
}


class TemplateNotAllowed(PermissionError):
    """A template is not startable in the current mode or state."""


def get_template(template_id: str) -> WorkflowTemplate:
    try:
        return TEMPLATES[template_id]
    except KeyError as exc:
        raise TemplateNotAllowed(
            f"unknown workflow template {template_id!r}; Atlas may start only "
            f"registered templates: {sorted(TEMPLATES)}") from exc


def check_startable(template_id: str, mode: str, facts: dict) -> WorkflowTemplate:
    """Return the template if Atlas may start it now, else raise.

    Authority first, then preconditions — so a mode violation is never masked
    by a data problem.
    """
    template = get_template(template_id)
    if mode == "observe":
        raise TemplateNotAllowed(
            f"Observe mode may not start workflows; {template_id!r} refused")
    if mode == "paused":
        raise TemplateNotAllowed("Atlas is paused; no autonomous work starts")
    if template.creates_plan and mode != "propose":
        raise TemplateNotAllowed(
            f"{template_id!r} creates a paper plan, which requires Propose "
            f"mode; current mode is {mode!r}")
    data = facts.get("data", {})
    if "data_eligible_for_research" in template.requires:
        if data.get("blocked"):
            raise TemplateNotAllowed(
                f"{template_id!r} needs usable data; the data plane is blocked")
    if "data_eligible_for_paper_proposal" in template.requires:
        if not data.get("eligible_for_paper_proposal"):
            raise TemplateNotAllowed(
                f"{template_id!r} needs paper-proposal-eligible data; the "
                "current permit does not allow it")
    if "operator_supplied_excerpts" in template.requires:
        if not facts.get("operator_excerpts"):
            raise TemplateNotAllowed(
                f"{template_id!r} needs operator-pasted excerpts; Atlas does not "
                "fetch news on its own")
    if "grounded_news_window" in template.requires:
        # The analyst interprets a window it is handed. Refusing when there is
        # nothing to read keeps it from narrating an empty record.
        if not facts.get("news_window_items"):
            raise TemplateNotAllowed(
                f"{template_id!r} needs a non-empty grounded news window; "
                "there is nothing to interpret right now")
    return template


def template_for_trigger(trigger_kind: str) -> str | None:
    return TRIGGER_TEMPLATE.get(trigger_kind)
