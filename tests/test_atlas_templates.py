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
