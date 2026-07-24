"""layer_classifier: the 4-role classifier (graphic/button/background/highlight/content) plus the
editable-field flow (bracketed text, or text sitting on a highlight).

Synthetic LayoutTree fixtures only (no PSD required), same convention as test_grid_analyzer.py /
test_table_solver.py.
"""

from __future__ import annotations

from psd_html.layer_classifier import (
    ROLE_BACKGROUND,
    ROLE_BUTTON,
    ROLE_CONTENT,
    ROLE_GRAPHIC,
    ROLE_HIGHLIGHT,
    classify_artboard,
    classify_tree,
)
from psd_html.layout_tree import BBox, Canvas, Layer, LayoutTree, TextInfo


def _layer(id, name, kind, bbox, z, parent=None, visible=True, text=None):
    return Layer(
        id=id,
        name=name,
        kind=kind,
        visible=visible,
        opacity=1.0,
        bbox=BBox(*bbox) if bbox else None,
        z=z,
        is_group=(kind in ("group", "artboard")),
        parent=parent,
        text=text,
    )


def _by_role(items, role):
    return [i for i in items if i.role == role]


def _item_for(items, layer_id):
    for i in items:
        if layer_id in i.layer_ids:
            return i
    raise AssertionError(f"no classified item covers layer id {layer_id}")


# --- highlight: a non-text rect sitting behind text -> highlight role + text becomes editable -----


def test_highlight_rect_behind_text_marks_text_editable():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            # highlight fill sits almost entirely under the text -> _pair_is_bg() true.
            _layer(2, "name highlight", "shape", (10, 10, 190, 40), z=1, parent=1),
            _layer(3, "name field", "type", (10, 12, 185, 38), z=2, parent=1, text=TextInfo("First Last")),
        ],
    )
    items = classify_artboard(tree, 1)

    highlight_items = _by_role(items, ROLE_HIGHLIGHT)
    assert len(highlight_items) == 1
    assert highlight_items[0].layer_ids == [2]

    text_item = _item_for(items, 3)
    assert text_item.role == ROLE_CONTENT
    assert text_item.is_text is True
    assert text_item.editable is True
    assert text_item.covered_by_highlight_id == 2


# --- graphic: name contains "graphic" -> flatten-to-one-image unit --------------------------------


def test_named_social_graphic_group_becomes_one_graphic_item():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "social graphic", "group", None, z=1, parent=1),
            _layer(3, "fb icon", "shape", (0, 0, 30, 30), z=2, parent=2),
            _layer(4, "tw icon", "shape", (40, 0, 70, 30), z=3, parent=2),
        ],
    )
    items = classify_artboard(tree, 1)

    graphic_items = _by_role(items, ROLE_GRAPHIC)
    assert len(graphic_items) == 1
    assert graphic_items[0].kind == "group"
    assert set(graphic_items[0].layer_ids) == {3, 4}
    assert graphic_items[0].editable is False


# --- button: name contains "button" -> shape+label unit --------------------------------------------


def test_named_review_button_group_becomes_button_item_with_label():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "Review button", "group", None, z=1, parent=1),
            _layer(3, "cta shape", "shape", (0, 0, 150, 50), z=2, parent=2),
            _layer(4, "cta label", "type", (10, 10, 140, 40), z=3, parent=2, text=TextInfo("Review Now")),
        ],
    )
    items = classify_artboard(tree, 1)

    button_items = _by_role(items, ROLE_BUTTON)
    assert len(button_items) == 1
    btn = button_items[0]
    assert set(btn.layer_ids) == {3, 4}
    assert btn.text is not None
    assert btn.text.content == "Review Now"


# --- background: full-width shape -> a row/section background -------------------------------------


def test_full_width_shape_becomes_background():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=600, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            # >= 0.85 * artboard width (600) -> qualifies as a full-bleed band.
            _layer(2, "section band", "shape", (0, 0, 600, 100), z=1, parent=1),
            _layer(3, "headline", "type", (20, 20, 300, 80), z=2, parent=1, text=TextInfo("Hello")),
        ],
    )
    items = classify_artboard(tree, 1)

    bg_items = _by_role(items, ROLE_BACKGROUND)
    assert len(bg_items) == 1
    assert bg_items[0].layer_ids == [2]

    # the background band itself is excluded from content, and does NOT mark the headline editable
    # (it's a band, not a highlight sitting tightly behind the text). Note the coincidence being
    # guarded here: this band (600x100) ALSO satisfies _is_single_line_highlight's math against the
    # headline (fill_height=100 <= 1.8 * the fallback 60px line height=108), so is_single_line_chip /
    # confers_edit compute True internally -- it is only the Pass-2 gate
    # `if item["role"] != ROLE_HIGHLIGHT: continue` that stops a BACKGROUND-classified item from ever
    # conferring edit. Assert editable/covered_by_highlight_id explicitly so a regression that
    # drops/loosens that gate (silently turning a static full-width band's headline into a merge
    # field) fails this test instead of shipping green.
    headline = _item_for(items, 3)
    assert headline.role == ROLE_CONTENT
    assert headline.editable is False
    assert headline.covered_by_highlight_id is None


def test_name_contains_bg_token_becomes_background():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=600, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            # narrow (not a band by width), but named "... bg" -> still background.
            _layer(2, "card bg", "shape", (0, 0, 200, 100), z=1, parent=1),
        ],
    )
    items = classify_artboard(tree, 1)
    assert _by_role(items, ROLE_BACKGROUND)[0].layer_ids == [2]


# --- content: narrow standalone image -> content image ---------------------------------------------


def test_narrow_standalone_image_is_content():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=600, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            _layer(2, "product icon", "shape", (10, 10, 90, 90), z=1, parent=1),
        ],
    )
    items = classify_artboard(tree, 1)
    content_items = _by_role(items, ROLE_CONTENT)
    assert len(content_items) == 1
    assert content_items[0].is_text is False
    assert content_items[0].editable is False


# --- editable: bracketed text -> editable, independent of any highlight ---------------------------


def test_bracketed_merge_field_text_is_editable_without_a_highlight():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "first name field", "type", (0, 0, 200, 30), z=1, parent=1, text=TextInfo("[First Name]")),
        ],
    )
    items = classify_artboard(tree, 1)
    field = _item_for(items, 2)
    assert field.role == ROLE_CONTENT
    assert field.is_text is True
    assert field.editable is True
    assert field.covered_by_highlight_id is None


def test_plain_text_without_brackets_or_highlight_is_not_editable():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "static copy", "type", (0, 0, 200, 30), z=1, parent=1, text=TextInfo("Thanks for shopping")),
        ],
    )
    items = classify_artboard(tree, 1)
    assert _item_for(items, 2).editable is False


# --- precedence: graphic > button > background > highlight > content ------------------------------


def test_precedence_graphic_group_wins_even_if_member_looks_like_background():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=600, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            _layer(2, "hero graphic", "group", None, z=1, parent=1),
            # this member is itself full-bleed-band-shaped and named "... bg", but the nearest
            # named ancestor is the "graphic" group -> graphic wins per precedence.
            _layer(3, "hero bg", "shape", (0, 0, 600, 100), z=2, parent=2),
        ],
    )
    items = classify_artboard(tree, 1)
    assert _by_role(items, ROLE_BACKGROUND) == []
    graphic_items = _by_role(items, ROLE_GRAPHIC)
    assert len(graphic_items) == 1
    assert graphic_items[0].layer_ids == [3]


# --- classify_tree: multi-artboard fan-out ----------------------------------------------------------


def test_classify_tree_returns_one_entry_per_artboard():
    tree = LayoutTree(
        psd="multi.psd",
        path="C:/fake/multi.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1, 10],
        layers=[
            _layer(1, "Email A", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "a text", "type", (0, 0, 300, 50), z=1, parent=1, text=TextInfo("A")),
            _layer(10, "Email B", "artboard", (0, 0, 300, 150), z=2),
            _layer(11, "b text", "type", (0, 0, 300, 50), z=3, parent=10, text=TextInfo("B")),
        ],
    )
    result = classify_tree(tree)
    assert set(result.keys()) == {1, 10}
    assert len(result[1]) == 1
    assert len(result[10]) == 1


# --- Defect A: _name_has matches a trailing WHOLE token, never a substring (idx 42/43, CRITICAL) -
# The owner-caught bug: substring matching classified TYPE layers named "Get the infographic" as
# ROLE_GRAPHIC and baked their live text into a raster.

from psd_html.layer_classifier import _name_has  # noqa: E402


def test_name_has_matches_only_a_trailing_whole_token():
    # substring-but-not-token -> NO match (the exact Defect A cases)
    assert _name_has("Get the infographic", "graphic") is False
    assert _name_has("...Infographic Display Banners Social Posts", "graphic") is False
    assert _name_has("graphic banner", "graphic") is False   # keyword not the LAST token
    assert _name_has("Buttoned Up", "button") is False
    assert _name_has(None, "graphic") is False
    # genuine trailing keyword (any case) + bare single token -> match
    assert _name_has("Header Graphic", "graphic") is True
    assert _name_has("graphic", "graphic") is True
    assert _name_has("CTA Button", "button") is True
    assert _name_has("Footer bg", "bg") is True


def test_infographic_type_layer_stays_live_content_not_rasterized_graphic():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=300, height=150), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "Get the infographic", "type", (0, 0, 200, 30), z=1, parent=1,
                   text=TextInfo("Get the infographic")),
        ],
    )
    items = classify_artboard(tree, 1)
    item = _item_for(items, 2)
    assert item.role == ROLE_CONTENT
    assert item.is_text is True
    # live text: not folded into any rasterizing (graphic/button) unit -- the exact Defect A leak.
    assert _by_role(items, ROLE_GRAPHIC) == []
    assert _by_role(items, ROLE_BUTTON) == []


# --- DIVIDER gate: a hairline-wide rect backing no text is CONTENT, not a BACKGROUND band (idx 46)


def test_hairline_divider_rect_is_content_not_background():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=600, height=300), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            # 590x5: hairline (<=8px), very wide (>=25x height), no covered text -> a drawn rule.
            _layer(2, "Line 1", "shape", (5, 100, 595, 105), z=1, parent=1),
        ],
    )
    items = classify_artboard(tree, 1)
    item = _item_for(items, 2)
    assert item.role == ROLE_CONTENT           # divider wins over the full-width band classification
    assert _by_role(items, ROLE_BACKGROUND) == []


def test_rect_just_over_the_divider_height_cap_is_a_background_band():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=600, height=300), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            # 590x9: height 9 > the <=8 divider cap -> NOT a divider; a full-width band instead.
            _layer(2, "thick bar", "shape", (5, 100, 595, 109), z=1, parent=1),
        ],
    )
    items = classify_artboard(tree, 1)
    assert _item_for(items, 2).role == ROLE_BACKGROUND


# --- classify_tree whole-canvas path when there are no artboards (idx 54) -----------------------


def test_classify_tree_flat_no_artboards_uses_none_region():
    tree = LayoutTree(
        psd="flat.psd", path="C:/fake/flat.psd", canvas=Canvas(width=300, height=150), artboards=[],
        layers=[
            _layer(2, "headline", "type", (0, 0, 300, 50), z=1, parent=None, text=TextInfo("Hi")),
            _layer(3, "product icon", "shape", (10, 60, 90, 140), z=2, parent=None),
        ],
    )
    result = classify_tree(tree)
    assert set(result.keys()) == {None}
    items = result[None]
    assert _item_for(items, 2).is_text is True
    assert _item_for(items, 3).is_text is False


# --- Defect 2 demotion: a fill that FAILS the single-line-highlight gate falls back to
# ROLE_BACKGROUND and marks nothing editable (idx 44 / idx 47) --------------------------------------


def test_fill_covering_two_text_layers_demotes_to_background():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=800, height=400), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 800, 400), z=0),
            # panel is well under the 0.85*width band threshold (300/800) -- it can only become
            # ROLE_BACKGROUND via the Defect 2 "covers >1 text layer" demotion, not the band path.
            _layer(2, "content panel", "shape", (50, 50, 350, 250), z=1, parent=1),
            _layer(3, "big heading", "type", (60, 60, 340, 90), z=2, parent=1, text=TextInfo("Big Heading")),
            _layer(4, "body copy", "type", (60, 100, 340, 230), z=3, parent=1, text=TextInfo("Body copy text block")),
        ],
    )
    items = classify_artboard(tree, 1)

    panel = _item_for(items, 2)
    assert panel.role == ROLE_BACKGROUND

    heading = _item_for(items, 3)
    body = _item_for(items, 4)
    assert heading.editable is False
    assert body.editable is False
    assert heading.covered_by_highlight_id is None
    assert body.covered_by_highlight_id is None


def test_fill_taller_than_line_height_and_noncontaining_demotes_to_background():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=600, height=300), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            # fill height 40 > 1.8 * the text's 20px estimated line height (36) -- fails the
            # single-line gate on height. It also does NOT fully contain the text (its right edge
            # stops well short of the text's right edge, even with the 8px slop), so the
            # sole-tenant box rescue never fires either -- it must fall through to BACKGROUND.
            _layer(2, "narrow fill", "shape", (10, 40, 150, 80), z=1, parent=1),
            _layer(3, "section heading", "type", (10, 50, 190, 70), z=2, parent=1, text=TextInfo("Section Heading")),
        ],
    )
    items = classify_artboard(tree, 1)

    fill = _item_for(items, 2)
    assert fill.role == ROLE_BACKGROUND

    heading = _item_for(items, 3)
    assert heading.editable is False
    assert heading.covered_by_highlight_id is None


# --- sole-tenant BOX gate: a tall fill that fully CONTAINS exactly one text (and no other layer
# center) becomes a NON-CONFERRING highlight -- geometry only, the label stays non-editable
# (idx 45) ------------------------------------------------------------------------------------------


def test_tall_containing_sole_tenant_box_is_highlight_but_does_not_confer_edit():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=600, height=300), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            # box height 70 > 1.8 * the label's 30px line height (54) -- fails the single-line
            # gate -- but it fully CONTAINS the label (within the 8px slop) and no other layer's
            # center falls inside it (tenants == 1, just the label itself) -- the sole-tenant box
            # rescue admits it as ROLE_HIGHLIGHT, geometry only.
            _layer(2, "cta box", "shape", (10, 10, 190, 80), z=1, parent=1),
            _layer(3, "cta label", "type", (20, 20, 180, 50), z=2, parent=1, text=TextInfo("Review the toolkit")),
        ],
    )
    items = classify_artboard(tree, 1)

    box = _item_for(items, 2)
    assert box.role == ROLE_HIGHLIGHT

    label = _item_for(items, 3)
    assert label.editable is False  # non-conferring: geometry only, no merge-field drift
    assert label.covered_by_highlight_id == 2


def test_tall_containing_box_with_a_second_tenant_demotes_to_background():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=600, height=300), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 300), z=0),
            _layer(2, "cta box", "shape", (10, 10, 190, 80), z=1, parent=1),
            _layer(3, "cta label", "type", (20, 20, 180, 50), z=2, parent=1, text=TextInfo("Review the toolkit")),
            # a second layer whose CENTER also falls inside the box -- tenants == 2 now, so the
            # sole-tenant rescue must NOT fire; the box falls back to the Defect 2 demotion instead
            # (it still covers the one label per _pair_is_bg, just no longer the box's only tenant).
            _layer(4, "stray icon", "shape", (95, 60, 115, 75), z=3, parent=1),
        ],
    )
    items = classify_artboard(tree, 1)

    box = _item_for(items, 2)
    assert box.role == ROLE_BACKGROUND

    label = _item_for(items, 3)
    assert label.editable is False
    assert label.covered_by_highlight_id is None


# --- eligibility filter chain: full-canvas background peel + hidden + out-of-bounds leaves are
# never emitted as ClassifiedItems (idx 50) ----------------------------------------------------------


def test_full_canvas_fill_hidden_and_out_of_bounds_leaves_are_excluded():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=400, height=300), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 400, 300), z=0),
            # full-canvas fill, bottom z among the eligible candidates -- _find_background peels
            # this before the eligibility loop even runs.
            _layer(2, "page fill", "shape", (0, 0, 400, 300), z=1, parent=1),
            # invisible -- excluded by the `l.visible` check, never even reaches the background
            # scorer or the eligibility loop.
            _layer(3, "hidden shape", "shape", (50, 50, 150, 150), z=2, parent=1, visible=False),
            # entirely outside the artboard region -- clamps to zero area and is dropped.
            _layer(4, "offscreen icon", "shape", (1000, 1000, 1100, 1100), z=3, parent=1),
            # the only layer that should survive.
            _layer(5, "headline", "type", (20, 20, 200, 60), z=4, parent=1, text=TextInfo("Hello")),
        ],
    )
    items = classify_artboard(tree, 1)

    assert len(items) == 1
    assert items[0].layer_ids == [5]
    assert items[0].is_text is True
    # the full-canvas fill was peeled as background, not emitted as a spurious background item,
    # nor admitted as a content item.
    assert _by_role(items, ROLE_BACKGROUND) == []
    assert _by_role(items, ROLE_CONTENT) == [items[0]]


# --- stop_at guard: the artboard's OWN name is never matched by _nearest_named_ancestor, so a
# role-keyword-named artboard does not swallow its children into one graphic/button unit (idx 51) --


def test_artboard_named_with_trailing_role_keyword_does_not_aggregate_children():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=300, height=150), artboards=[1],
        layers=[
            # the ARTBOARD itself is named with a trailing "graphic" keyword -- _nearest_named_
            # ancestor must stop at cur.id == stop_at BEFORE checking the name, so this never
            # anchors a graphic aggregation for its children.
            _layer(1, "Social Graphic", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "icon", "shape", (10, 10, 50, 50), z=1, parent=1),
            _layer(3, "headline", "type", (60, 10, 290, 50), z=2, parent=1, text=TextInfo("Hi")),
        ],
    )
    items = classify_artboard(tree, 1)

    assert _by_role(items, ROLE_GRAPHIC) == []
    headline = _item_for(items, 3)
    assert headline.role == ROLE_CONTENT
    assert headline.is_text is True


# --- multiple highlight chips covering the same text: covered_by_highlight_ids carries ALL of
# them (order-insensitive), covered_by_highlight_id pins the deterministic first; uncovered text
# gets None, not [] (idx 52 / idx 53) -----------------------------------------------------------------


def test_two_highlight_chips_covering_same_text_yield_both_ids():
    tree = LayoutTree(
        psd="t.psd", path="C:/fake/t.psd", canvas=Canvas(width=500, height=400), artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 500, 400), z=0),
            # both chips independently pass _pair_is_bg + the single-line height gate against the
            # SAME text -- chip1 is listed first, so it is the deterministic tie-break winner for
            # the singular covered_by_highlight_id field.
            _layer(2, "chip one", "shape", (15, 15, 185, 55), z=1, parent=1),
            _layer(3, "chip two", "shape", (10, 10, 190, 60), z=2, parent=1),
            _layer(4, "merge token", "type", (20, 20, 180, 50), z=3, parent=1, text=TextInfo("Token")),
            # a second text with no covering highlight at all -- must read None, not [].
            _layer(5, "footer note", "type", (300, 300, 390, 330), z=4, parent=1, text=TextInfo("Footer")),
        ],
    )
    items = classify_artboard(tree, 1)

    assert set(_item_for(items, 2).layer_ids) == {2}
    assert _item_for(items, 2).role == ROLE_HIGHLIGHT
    assert _item_for(items, 3).role == ROLE_HIGHLIGHT

    token = _item_for(items, 4)
    assert token.covered_by_highlight_id == 2  # deterministic first (chip one, listed first)
    assert set(token.covered_by_highlight_ids) == {2, 3}

    footer = _item_for(items, 5)
    assert footer.covered_by_highlight_id is None
    assert footer.covered_by_highlight_ids is None
