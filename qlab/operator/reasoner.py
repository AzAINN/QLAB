"""Atlas's template judgment: the reasoner proposes, the lookup remains.

Step 3 of ``planning-docs/2026-07-31-atlas-as-llm.md``, scoped to the one
judgment it names. ``template_for_trigger`` maps ``regime_flip →
regime_review``: that is a judgment call — "given everything happening, what is
worth investigating?" — implemented as a lookup table, and it has been mistaken
for an invariant because it happens to be deterministic. ``check_startable``,
which refuses a template the current mode does not permit, is rigor. The two
were never the same thing and they are already separate functions; this module
fills in the first and does not touch the second.

Three rules the module is built around:

* **It never widens authority.** The menu handed to the model is derived from
  ``check_startable`` itself (``templates.startable_templates``), and the gate
  runs *again* on whatever comes back — in ``AtlasSupervisor.observe``, code
  this module cannot reach. A reply naming work the desk may not do is
  discarded, never negotiated down.
* **A reply is foreign input.** The parse is strict and never guesses: no
  nearest-match, no case folding, no "it probably meant". An id that is not on
  the menu it was given yields ``None`` and the table's answer stands, because
  a desk that starts the template a model *nearly* named is worse than one
  that starts the template the table named.
* **It writes nothing, and it may always fail.** The registry is the owner's;
  the reason a choice was refused goes to ``note`` for the call site to record.
  Every failure path returns ``None``, so the deterministic path completes
  whether or not a model answered.

The lookup's own answer is deliberately withheld from the prompt. The doc's
condition for ever removing the table is that both run against identical facts
for a period and be compared — and a comparison in which one side was shown the
other's answer measures nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:      # annotations only; a runtime import would be a cycle
    from qlab.operator.atlas import Trigger
    from qlab.operator.llm_backends import LlmBackend

# This sits on the observe loop's path. No human is watching a heartbeat tick,
# but the owner's own loop is what makes the desk look alive, so the ceiling is
# under `atlas_message`'s 60s rather than the backends' batch defaults.
REASONER_TIMEOUT_S = 45.0
# One id and two or three sentences. A longer budget buys narration the parser
# discards anyway.
REASONER_MAX_TOKENS = 400

# Where the caller puts the menu on the context. The set MUST come from
# `templates.startable_templates`; composing it any other way here would make
# this module a second opinion about authority. `UISession.atlas_context`
# writes the key; a drift between the two names would silently mean "nothing is
# startable" forever, which is why the wiring test asserts the menu reached the
# prompt and the choice came back rather than only that a call was made.
STARTABLE_KEY = "startable_templates"

# What a refusal note may quote back of a reply. A model answer is untrusted
# text on its way to an event row and a client.
_NOTE_CHARS = 200

REASONER_SYSTEM = """\
You are Atlas, the desk manager of qlab — a governed, single-operator quant \
research desk. Something on the desk changed and woke you. You are choosing \
which registered workflow template to start in response to it.

You never execute. You hold no order path, no approval, and no way to create a \
plan. Choose exactly one template id from the startable list below the \
context: every other template is refused by deterministic code you cannot \
reach, the refusal reasons are in the context, and naming refused work is an \
error rather than a preference. You cannot decline and you cannot invent a \
template — if none is a good fit, choose the least bad one and say so.

Answer with ONE JSON object and nothing else. No prose before it, no code \
fence, no second object:

{"template_id": "<one id from the startable list>", "rationale": "<2-3 sentences>"}

The rationale names the readings you actually used — a level, a percentile, a \
headline — and names what argues against your choice."""


@dataclass(frozen=True)
class ReasonerChoice:
    """One template the reasoner chose, and why. Not yet authorized."""

    template_id: str
    rationale: str


def choose_template(context: dict, trigger: "Trigger", backend: "LlmBackend",
                    model: str, *,
                    note: Callable[[str], None] | None = None
                    ) -> ReasonerChoice | None:
    """The reasoner's template choice for one trigger, or None.

    ``None`` for everything this will not use — a menu that is empty, a backend
    that failed, a reply that does not parse, an id outside the menu — and
    ``note`` receives the sentence saying which. The caller records it: this
    module holds no registry handle, and the owner is the single writer.

    ``context`` must carry ``STARTABLE_KEY``, the ``check_startable``-derived
    menu. Its absence is treated as "nothing is startable" rather than as
    "anything is": a caller that forgot to compose it gets no choice at all,
    which is the direction a mistake here has to fail.
    """
    startable = context.get(STARTABLE_KEY) or {}
    if not isinstance(startable, dict) or not startable:
        _say(note, "no registered template is startable right now, so there "
                   "was nothing for the reasoner to choose among")
        return None

    from qlab.operator.llm_backends import LlmBackendError

    try:
        reply = backend.complete(
            system=REASONER_SYSTEM,
            user=_user_prompt(context, trigger, startable),
            model=model, max_tokens=REASONER_MAX_TOKENS,
            timeout=REASONER_TIMEOUT_S)
    except LlmBackendError as exc:
        # Asked and could not answer. An ordinary state of an operator's
        # machine, not a reason to stop the desk observing.
        _say(note, f"the reasoner could not answer: {_head(str(exc))}")
        return None
    return _parse(reply, startable, note)


def _user_prompt(context: dict, trigger: "Trigger",
                 startable: dict[str, str]) -> str:
    """The desk, the trigger, and the menu — in that order.

    The trigger is rendered from its own fields rather than passed whole so
    ``template_id`` (the table's answer) cannot leak into the prompt.
    """
    menu = "\n".join(f"- {template_id}: {purpose}"
                     for template_id, purpose in sorted(startable.items()))
    woke = {"kind": trigger.kind, "action": trigger.action,
            "payload": trigger.payload}
    return (
        "The desk right now, as JSON:\n\n"
        # Compact: this is ~12KB of context and every separator is a token.
        f"{json.dumps(context, default=str, separators=(',', ':'))}\n\n"
        "What woke you:\n\n"
        f"{json.dumps(woke, default=str, separators=(',', ':'))}\n\n"
        f"The templates you may choose from, and no others:\n\n{menu}")


def _parse(reply: str, startable: dict[str, str],
           note: Callable[[str], None] | None) -> ReasonerChoice | None:
    """Strict: one object, two string fields, an id that is on the menu."""
    decoded = _object(reply)
    if decoded is None:
        _say(note, "the reasoner did not answer with the one JSON object it "
                   f"was asked for: {_head(reply)}")
        return None
    template_id = decoded.get("template_id")
    rationale = decoded.get("rationale")
    if not isinstance(template_id, str) or not isinstance(rationale, str):
        _say(note, "the reasoner's object is missing a string template_id or "
                   f"rationale: {_head(reply)}")
        return None
    template_id = template_id.strip()
    rationale = " ".join(rationale.split())
    if not rationale:
        _say(note, f"the reasoner chose {template_id!r} and gave no reason "
                   "for it; an unexplained choice is not a judgment")
        return None
    if template_id not in startable:
        # No nearest match and no normalisation beyond stripping whitespace.
        # The menu came from the gate, so an id that is not on it is either
        # out of authority or invented, and both are the same answer here.
        from qlab.operator.templates import TEMPLATES

        known = ("is registered but not startable right now"
                 if template_id in TEMPLATES else "is not a registered template")
        _say(note, f"the reasoner named {template_id!r}, which {known}; the "
                   f"startable templates were "
                   f"{', '.join(sorted(startable))}")
        return None
    return ReasonerChoice(template_id=template_id, rationale=rationale)


def _object(reply: str) -> dict | None:
    """The single JSON object in ``reply``, or None.

    A fenced block is the one deviation tolerated, because every local model
    emits one sooner or later and a fence is a formatting habit rather than a
    different answer. Everything past that is refused: the outermost braces are
    read as-is, so a reply carrying two objects or none yields nothing.
    """
    text = (reply or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) < 2:
            return None
        text = parts[1].lstrip()
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        decoded = json.loads(text[start:end + 1])
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _head(text: str) -> str:
    """A bounded single-line excerpt of untrusted text for a refusal note."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) > _NOTE_CHARS:
        return collapsed[:_NOTE_CHARS] + "…"
    return collapsed


def _say(note: Callable[[str], None] | None, message: str) -> None:
    if note is not None:
        note(message)
