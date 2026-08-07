"""The template menu: what may run, and why the rest may not."""


def test_the_menu_carries_the_refusal_rather_than_dropping_the_template():
    """A desk that silently omits what it will not do teaches nothing about why."""
    from qlab.operator.templates import template_menu

    menu = template_menu("research", {"data": {"blocked": True}})
    by_id = {entry["template_id"]: entry for entry in menu}
    assert by_id["regime_review"]["startable"] is False
    assert "data plane is blocked" in by_id["regime_review"]["reason"]
    # desk_brief requires nothing and creates no plan, so a blocked data plane
    # does not reach it.
    assert by_id["desk_brief"]["startable"] is True
    assert by_id["desk_brief"]["reason"] is None


def test_the_authority_half_answers_without_facts_and_check_startable_uses_it():
    """A surface that cannot afford `atlas_facts` — the two-second snapshot —
    still has to be able to say "this mode may not start that". Splitting the
    mode rules out gives it an honest half-answer without a second copy of the
    gate: `check_startable` must delegate, or the two forks the first time a
    mode rule changes.
    """
    import pytest

    from qlab.operator.templates import (
        TEMPLATES, TemplateNotAllowed, check_authority, check_startable)

    with pytest.raises(TemplateNotAllowed, match="Observe mode"):
        check_authority("desk_brief", "observe")
    with pytest.raises(TemplateNotAllowed, match="paused"):
        check_authority("desk_brief", "paused")
    with pytest.raises(TemplateNotAllowed, match="Propose"):
        check_authority("desk_rebalance_review", "research")
    assert check_authority("desk_brief", "research").template_id == "desk_brief"

    # Delegation, asserted rather than assumed: every mode refusal
    # `check_startable` makes is one `check_authority` makes on its own, with
    # the same sentence, against facts rich enough that no precondition fires.
    facts = {"data": {"blocked": False, "eligible_for_paper_proposal": True},
             "operator_excerpts": ["x"], "news_window_items": 3}
    for mode in ("observe", "research", "propose", "paused"):
        for template_id in TEMPLATES:
            refusal = None
            try:
                check_authority(template_id, mode)
            except TemplateNotAllowed as exc:
                refusal = str(exc)
            if refusal is None:
                assert check_startable(template_id, mode, facts)
                continue
            with pytest.raises(TemplateNotAllowed) as caught:
                check_startable(template_id, mode, facts)
            assert str(caught.value) == refusal


def test_startable_templates_is_the_menus_permitted_half():
    """One authority, not two. If these ever disagree the gate has forked."""
    from qlab.operator.templates import startable_templates, template_menu

    facts = {"data": {"blocked": False}}
    for mode in ("observe", "research", "propose", "paused"):
        menu = template_menu(mode, facts)
        assert startable_templates(mode, facts) == {
            entry["template_id"]: entry["purpose"]
            for entry in menu if entry["startable"]
        }
