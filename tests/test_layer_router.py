"""C-LAYER-ROUTER tests.

Adversarial hand-built fixtures (no tautological asserts -- every case is checked against a
hand-picked expected verb, never against the function's own output) plus the real announcement
PSD run through the full S1 pipeline (psd_adapter -> table_solver -> layer_router).
"""

from __future__ import annotations

import os

import pytest

from psd_html.font_resolver import DEFAULT_REGISTRY, FontRegistryEntry
from psd_html.layer_router import (
    EditabilityViolation,
    RoutedTree,
    _assign_verb,
    is_brand_headline,
    is_cta,
    is_merge,
    is_protected,
    iter_routed,
    render_role,
    route,
)
from psd_html.layout_tree import BBox, TextInfo, TextRun
from psd_html.table_tree import Cell, Row, TableTree

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ANNOUNCEMENT_PSD = os.path.join(
    REPO_ROOT,
    "Reference",
    "2413101_Intel",
    "PSDs",
    "Intel x Microsoft_Commercial Refresh_announcement email",
    "Intel_MsfT_Global BoM_Announcement Email.psd",
)


# Segoe UI routes LIVE since 2026-07-14 (human-OFT proof) -- brand->raster routing is still real
# behavior for faces Windows does not install, so these tests exercise it with a registered
# fixture face instead of Segoe.
BRAND_FIXTURE_FONT = "BrandDisplay-Bold"


@pytest.fixture(autouse=True)
def _register_brand_fixture_font():
    DEFAULT_REGISTRY["branddisplay"] = FontRegistryEntry(
        family="Brand Display", fallback_stack=("sans-serif",), brand_mandatory=True, files=()
    )
    yield
    DEFAULT_REGISTRY.pop("branddisplay", None)


def _rect(l=0, t=0, r=100, b=20):
    return BBox(left=l, top=t, right=r, bottom=b)


def _text_cell(content, *, font="SegoeUI", editable=False, link_slot=None, role="text", size=14.0):
    return Cell(
        role=role,
        rect=_rect(),
        editable=editable,
        link_slot=link_slot,
        text=TextInfo(content=content, align="left", runs=[TextRun(font=font, size=size, color="#000000")]),
    )


def _image_cell():
    return Cell(role="image", rect=_rect(), image_source_layer_ids=[1])


def _graphic_cell():
    return Cell(role="graphic", rect=_rect(), image_source_layer_ids=[2])


def _rows_cell(rows):
    return Cell(role="rows", rect=_rect(), rows=rows)


def _tree(cells_per_row):
    return TableTree(email="Test", width=600, rows=[Row(cells=cells) for cells in cells_per_row])


# --- render_role / is_* classification (pure function unit tests) --------------------------------


def test_bracketed_merge_field_classified_as_merge_and_protected():
    cell = _text_cell("Hi [First Name],", font="Arial")
    assert is_merge(cell) is True
    assert is_protected(cell) is True
    assert render_role(cell) == "merge"


def test_editable_non_bracketed_field_is_still_merge():
    cell = _text_cell("Best regards, Team", font="Arial", editable=True)
    assert is_merge(cell) is True
    assert render_role(cell) == "merge"


def test_link_slot_text_cell_is_cta_and_protected():
    cell = _text_cell("Review the toolkit", font="Arial", link_slot="review-the-toolkit")
    assert is_cta(cell) is True
    assert is_protected(cell) is True
    assert render_role(cell) == "cta"


def test_button_role_is_always_cta_regardless_of_link_slot():
    cell = _text_cell("Shop Now", font="Arial", role="button", link_slot=None)
    assert is_cta(cell) is True
    assert render_role(cell) == "cta"


def test_plain_websafe_body_copy_is_body_and_protected():
    cell = _text_cell("This is plain running body copy.", font="Arial")
    assert is_merge(cell) is False
    assert is_cta(cell) is False
    assert is_brand_headline(cell) is False
    assert is_protected(cell) is True
    assert render_role(cell) == "body"


def test_brand_mandatory_headline_text_is_brand_headline_not_protected():
    assert "branddisplay" in DEFAULT_REGISTRY  # sanity: fixture actually exercises a brand-mandatory font
    cell = _text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)
    assert is_merge(cell) is False
    assert is_cta(cell) is False
    assert is_brand_headline(cell) is True
    assert is_protected(cell) is False
    assert render_role(cell) == "brand_headline"


def test_bracket_check_wins_over_brand_font_for_merge_classification():
    # A bracketed merge field set in the brand font must still classify as merge (protected),
    # never brand_headline -- merge/cta checks run before the brand-font check.
    cell = _text_cell("Hi [First Name],", font=BRAND_FIXTURE_FONT)
    assert render_role(cell) == "merge"
    assert is_protected(cell) is True


def test_segoe_text_routes_live_as_body():
    # Decision 2026-07-14: the shipped human OFT renders live Segoe in classic Outlook, so Segoe
    # cells are BODY (live text), never brand_headline/raster -- rasterizing Segoe caused the
    # owner-visible top-trim / shrink-scaling / wrap-drift defects.
    for raw in ("SegoeUI", "SegoeUI-Bold", "SegoeUI-Semibold"):
        cell = _text_cell("What drives the demand for AI PCs?", font=raw)
        assert is_brand_headline(cell) is False
        assert render_role(cell) == "body"


def test_image_and_graphic_cells_are_not_protected():
    assert is_protected(_image_cell()) is False
    assert is_protected(_graphic_cell()) is False
    assert render_role(_image_cell()) == "image"
    assert render_role(_graphic_cell()) == "graphic"


def test_container_cell_role_is_container():
    container = _rows_cell([Row(cells=[_text_cell("nested", font="Arial")])])
    assert render_role(container) == "container"


def test_unknown_font_defaults_non_brand_mandatory_so_text_is_body_not_headline():
    with pytest.warns(UserWarning):
        cell = _text_cell("Some copy", font="Some Totally Unregistered Face")
        assert is_brand_headline(cell) is False
        assert render_role(cell) == "body"


def test_no_text_content_cell_is_not_brand_headline():
    cell = Cell(role="text", rect=_rect(), text=None)
    with pytest.warns(UserWarning):
        assert is_brand_headline(cell) is False
    assert render_role(cell) == "body"


# --- route(): per-policy verb assignment on hand-built adversarial trees --------------------------


def test_merge_cta_body_are_live_in_all_three_policies():
    merge = _text_cell("Hi [First Name],", font="Arial")
    cta = _text_cell("Review the toolkit", font="Arial", link_slot="review-the-toolkit")
    body = _text_cell("Plain body copy.", font="Arial")
    tree = _tree([[merge, cta, body]])

    for policy in ("live", "hybrid", "raster"):
        routed = route(tree, policy)
        verbs = [verb for _cell, _key, verb in iter_routed(routed)]
        assert verbs == ["live", "live", "live"], f"policy={policy} verbs={verbs}"


def test_bracketed_button_label_is_live_in_all_three_policies():
    # A bracketed button label (e.g. an account-customizable CTA like "[Shop Now]") must classify
    # as merge, not cta -- is_merge's role allowlist explicitly includes "button" (layer_router.py
    # is_merge), and render_role() checks is_merge() BEFORE is_cta() (layer_router.py render_role),
    # so the merge branch wins. Both classifications route "live" under every policy, but they are
    # NOT the same role: html_emitter.py dispatches ROLE_MERGE -> _render_text_leaf (plain
    # paragraph) vs ROLE_CTA -> _render_cta_leaf (bulletproof-button construct). Pin the actual role
    # here, not just the verb, so a merge-before-cta ordering regression fails this test.
    cta_button = _text_cell("[Shop Now]", font=BRAND_FIXTURE_FONT, role="button")
    assert render_role(cta_button) == "merge"
    tree = _tree([[cta_button]])
    for policy in ("live", "hybrid", "raster"):
        routed = route(tree, policy)
        verbs = [verb for _c, _k, verb in iter_routed(routed)]
        assert verbs == ["live"]


def test_brand_headline_is_raster_in_hybrid_and_raster_but_live_in_live_policy():
    headline = _text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)
    tree = _tree([[headline]])

    live_routed = route(tree, "live")
    hybrid_routed = route(tree, "hybrid")
    raster_routed = route(tree, "raster")

    assert list(live_routed.verbs.values()) == ["live"]
    assert list(hybrid_routed.verbs.values()) == ["raster"]
    assert list(raster_routed.verbs.values()) == ["raster"]


def test_image_and_graphic_cells_are_raster_under_every_policy():
    tree = _tree([[_image_cell(), _graphic_cell()]])
    for policy in ("live", "hybrid", "raster"):
        routed = route(tree, policy)
        verbs = list(routed.verbs.values())
        assert verbs == ["raster", "raster"], f"policy={policy} verbs={verbs}"


def test_nested_rows_container_is_walked_and_leaf_verbs_assigned():
    nested = _rows_cell(
        [
            Row(cells=[_text_cell("Nested headline", font=BRAND_FIXTURE_FONT), _image_cell()]),
        ]
    )
    top_merge = _text_cell("Hi [Name],", font="Arial")
    tree = _tree([[top_merge, nested]])

    routed = route(tree, "hybrid")
    # 3 leaves total: top_merge, nested-headline, nested-image. The container itself gets no verb.
    pairs = [(render_role(cell), verb) for cell, _key, verb in iter_routed(routed)]
    assert pairs == [("merge", "live"), ("brand_headline", "raster"), ("image", "raster")]
    assert len(routed.verbs) == 3


def test_route_rejects_unknown_policy():
    tree = _tree([[_text_cell("x", font="Arial")]])
    with pytest.raises(ValueError):
        route(tree, "not-a-real-policy")


# --- AC-201: routing never mutates the tree; geometry identical across all 3 policies -------------


def test_route_does_not_mutate_the_tree():
    headline = _text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)
    merge = _text_cell("Hi [Name],", font="Arial")
    tree = _tree([[headline, merge, _image_cell()]])
    before = tree.to_dict()

    for policy in ("live", "hybrid", "raster"):
        route(tree, policy)
        after = tree.to_dict()
        assert after == before, f"tree mutated by route() under policy={policy}"


def test_three_policies_yield_identical_geometry_differing_only_in_verbs():
    headline = _text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)
    merge = _text_cell("Hi [Name],", font="Arial")
    body = _text_cell("Plain body copy.", font="Arial")
    tree = _tree([[headline, merge, body, _image_cell()]])

    routed_by_policy = {p: route(tree, p) for p in ("live", "hybrid", "raster")}

    serialized = {p: r.tree.to_dict() for p, r in routed_by_policy.items()}
    assert serialized["live"] == serialized["hybrid"] == serialized["raster"]

    verb_sets = {p: list(r.verbs.values()) for p, r in routed_by_policy.items()}
    assert verb_sets["live"] != verb_sets["hybrid"]  # headline differs
    assert verb_sets["hybrid"] == verb_sets["raster"]  # same for this fixture (no live-only case)


# --- AC-202: the EditabilityViolation guard is reachable and names the layer ----------------------


def test_forcing_raster_on_a_bracketed_merge_field_raises_editability_violation_naming_the_layer():
    merge = _text_cell("Hi [First Name],", font="Arial")
    with pytest.raises(EditabilityViolation) as excinfo:
        _assign_verb(merge, "hybrid", force_verb="raster")
    message = str(excinfo.value)
    assert "[First Name]" in message
    assert "rect=" in message


def test_forcing_raster_on_a_cta_text_cell_raises_editability_violation():
    cta = _text_cell("Review the toolkit", font="Arial", link_slot="review-the-toolkit")
    with pytest.raises(EditabilityViolation) as excinfo:
        _assign_verb(cta, "raster", force_verb="raster")
    assert "Review the toolkit" in str(excinfo.value)


def test_forcing_raster_on_plain_body_copy_raises_editability_violation():
    body = _text_cell("Plain body copy.", font="Arial")
    with pytest.raises(EditabilityViolation) as excinfo:
        _assign_verb(body, "hybrid", force_verb="raster")
    assert "Plain body copy." in str(excinfo.value)


def test_forcing_raster_on_a_button_cta_raises_editability_violation():
    button = _text_cell("Shop Now", font="Arial", role="button")
    with pytest.raises(EditabilityViolation):
        _assign_verb(button, "raster", force_verb="raster")


def test_forcing_raster_on_brand_headline_does_not_raise_because_it_is_not_protected():
    headline = _text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)
    # Sanity: forcing the SAME verb hybrid would already choose is a no-op, not a guard test --
    # what matters is that forcing raster on a genuinely non-protected cell is permitted.
    assert _assign_verb(headline, "hybrid", force_verb="raster") == "raster"


def test_forcing_raster_on_image_or_graphic_does_not_raise():
    assert _assign_verb(_image_cell(), "hybrid", force_verb="raster") == "raster"
    assert _assign_verb(_graphic_cell(), "hybrid", force_verb="raster") == "raster"


# --- the never-bracket/never-CTA-to-raster guarantee, stated directly as the spec's own examples --


def test_bracketed_field_never_raster_in_any_policy_end_to_end():
    merge = _text_cell("[First Name]", font="Arial")
    tree = _tree([[merge]])
    for policy in ("live", "hybrid", "raster"):
        routed = route(tree, policy)
        assert list(routed.verbs.values()) == ["live"]


def test_button_cta_never_raster_in_any_policy_end_to_end():
    button = _text_cell("Shop Now", font="Arial", role="button")
    tree = _tree([[button]])
    for policy in ("live", "hybrid", "raster"):
        routed = route(tree, policy)
        assert list(routed.verbs.values()) == ["live"]


# --- iter_routed / RoutedTree shape ---------------------------------------------------------------


def test_iter_routed_yields_cell_key_verb_tuples_matching_verbs_dict():
    tree = _tree([[_text_cell("Hi [Name],", font="Arial"), _image_cell()]])
    routed = route(tree, "hybrid")
    assert isinstance(routed, RoutedTree)
    seen = list(iter_routed(routed))
    assert len(seen) == 2
    for _cell, key, verb in seen:
        assert routed.verbs[key] == verb
        assert isinstance(key, tuple)


def test_routing_key_is_a_stable_path_index_not_python_id():
    # Two structurally-identical trees (distinct Cell instances) must produce the same key set.
    tree_a = _tree([[_text_cell("Hi [Name],", font="Arial"), _image_cell()]])
    tree_b = _tree([[_text_cell("Hi [Name],", font="Arial"), _image_cell()]])
    keys_a = {key for _c, key, _v in iter_routed(route(tree_a, "hybrid"))}
    keys_b = {key for _c, key, _v in iter_routed(route(tree_b, "hybrid"))}
    assert keys_a == keys_b


# --- the real Intel announcement PSD, through the full S1 pipeline -------------------------------


@pytest.mark.skipif(not os.path.isfile(ANNOUNCEMENT_PSD), reason="Intel announcement PSD fixture not present")
def test_real_announcement_psd_three_policies_same_geometry_and_protected_cells_always_live():
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    trees = build_table_trees(layout, email_override="Announcement")
    assert trees, "expected at least one solved TableTree from the announcement PSD"
    tree = trees[0]

    routed_by_policy = {p: route(tree, p) for p in ("live", "hybrid", "raster")}

    # AC-201: identical geometry across all three policies.
    serialized = {p: r.tree.to_dict() for p, r in routed_by_policy.items()}
    assert serialized["live"] == serialized["hybrid"] == serialized["raster"]

    # Every protected leaf (merge/cta/body) is "live" under every policy.
    for policy, routed in routed_by_policy.items():
        for cell, _key, verb in iter_routed(routed):
            if is_protected(cell):
                assert verb == "live", (
                    f"policy={policy} rastered a protected cell: {render_role(cell)} "
                    f"content={getattr(cell.text, 'content', None)!r}"
                )

    # Decision 2026-07-14: this corpus is 100% Segoe UI, which routes LIVE (human-OFT proof) --
    # no cell is brand_headline, so all three policies agree verb-for-verb and the only raster
    # verbs left are true image/graphic cells.
    live_verbs = routed_by_policy["live"].verbs
    hybrid_verbs = routed_by_policy["hybrid"].verbs
    raster_verbs = routed_by_policy["raster"].verbs
    assert live_verbs == hybrid_verbs == raster_verbs
    for cell, key, verb in iter_routed(routed_by_policy["hybrid"]):
        if verb == "raster":
            assert render_role(cell) in ("image", "graphic"), (
                f"non-image cell rastered under hybrid: {render_role(cell)} at {key}"
            )


@pytest.mark.skipif(not os.path.isfile(ANNOUNCEMENT_PSD), reason="Intel announcement PSD fixture not present")
def test_real_announcement_psd_bracketed_and_link_slot_cells_never_raster():
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    trees = build_table_trees(layout, email_override="Announcement")
    tree = trees[0]

    found_bracket = False
    found_link_slot = False
    for policy in ("live", "hybrid", "raster"):
        routed = route(tree, policy)
        for cell, _key, verb in iter_routed(routed):
            if cell.role == "text" and cell.text and "[" in cell.text.content:
                found_bracket = True
                assert verb == "live"
            if cell.link_slot is not None:
                found_link_slot = True
                assert verb == "live"

    assert found_bracket, "expected the real PSD to contain at least one bracketed merge field"
    assert found_link_slot, "expected the real PSD to contain at least one link_slot (CTA) cell"


# --- _dominant_font: skip runs with no font, fall through on all-empty (idx 55) -----------------


def test_dominant_font_uses_first_run_that_carries_a_font():
    # First run has no font; the brand face on the SECOND run must still drive brand_headline.
    cell = Cell(
        role="text", rect=_rect(), editable=False, link_slot=None,
        text=TextInfo(content="Big Brand News", align="left", runs=[
            TextRun(font=None, size=24.0, color="#000000"),
            TextRun(font=BRAND_FIXTURE_FONT, size=24.0, color="#000000"),
        ]),
    )
    assert is_brand_headline(cell) is True
    assert _assign_verb(cell, "hybrid") == "raster"


def test_dominant_font_all_runs_without_font_falls_to_body_live():
    cell = Cell(
        role="text", rect=_rect(), editable=False, link_slot=None,
        text=TextInfo(content="plain copy", align="left", runs=[
            TextRun(font=None, size=14.0, color="#000000"),
            TextRun(font="", size=14.0, color="#000000"),
        ]),
    )
    assert is_brand_headline(cell) is False       # loop exhausts -> None font -> not brand
    assert _assign_verb(cell, "hybrid") == "live"  # body copy, live, no crash
