"""table_solver: nested-table basics, the overlap-flattener fallback, and the safety invariant.

Synthetic LayoutTree fixtures only (no PSD required), same convention as test_grid_analyzer.py.
"""

from __future__ import annotations

import warnings

import pytest

from psd_html.layer_classifier import ROLE_BACKGROUND, ROLE_CONTENT, ClassifiedItem, classify_artboard
from psd_html.layout_tree import BBox, Canvas, Layer, LayoutTree, TextInfo
from psd_html.table_solver import (
    SafetyInvariantViolation,
    build_table_trees,
    find_baked_text_cells,
    solve_artboard,
)
from psd_html.table_tree import Cell, Row, TableTree


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


# --- solver basics: nested rows/cells -----------------------------------------------------------


def test_solve_header_row_plus_three_col_body():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=640, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 640, 300), z=0),
            _layer(2, "header", "type", (0, 0, 640, 50), z=1, parent=1, text=TextInfo("Header")),
            _layer(3, "col1", "type", (0, 50, 200, 150), z=2, parent=1, text=TextInfo("Col 1")),
            _layer(4, "col2", "type", (200, 50, 400, 150), z=3, parent=1, text=TextInfo("Col 2")),
            _layer(5, "col3", "type", (400, 50, 640, 150), z=4, parent=1, text=TextInfo("Col 3")),
        ],
    )
    trees = build_table_trees(tree, email_override="Test Email")
    assert len(trees) == 1
    t = trees[0]
    assert len(t.rows) == 2
    assert len(t.rows[0].cells) == 1
    assert t.rows[0].cells[0].text.content == "Header"
    assert len(t.rows[1].cells) == 3
    assert [c.text.content for c in t.rows[1].cells] == ["Col 1", "Col 2", "Col 3"]


def test_solve_stat_block_one_row_three_cells():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=600, height=200),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 200), z=0),
            _layer(2, "stat1", "type", (0, 0, 200, 100), z=1, parent=1, text=TextInfo("50%")),
            _layer(3, "stat2", "type", (200, 0, 400, 100), z=2, parent=1, text=TextInfo("30%")),
            _layer(4, "stat3", "type", (400, 0, 600, 100), z=3, parent=1, text=TextInfo("20%")),
        ],
    )
    trees = build_table_trees(tree)
    t = trees[0]
    assert len(t.rows) == 1
    assert len(t.rows[0].cells) == 3


# --- fallback: overlap with NO editable member collapses cleanly, no error -----------------------


def test_fallback_two_overlapping_icons_no_field_collapses_to_one_graphic_cell():
    tree = LayoutTree(
        psd="t.psd",
        path="C:/fake/t.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 300), z=0),
            _layer(2, "icon a", "shape", (0, 0, 100, 100), z=1, parent=1),
            _layer(3, "icon b", "shape", (50, 50, 150, 150), z=2, parent=1),
        ],
    )
    trees = build_table_trees(tree)  # must NOT raise
    t = trees[0]
    assert len(t.rows) == 1
    assert len(t.rows[0].cells) == 1
    cell = t.rows[0].cells[0]
    assert cell.role == "graphic"
    assert cell.editable is False
    assert set(cell.image_source_layer_ids) == {2, 3}


# --- the safety invariant: editable trapped in a graphic cell -> FAIL LOUD -----------------------


def test_invariant_bracketed_field_trapped_in_overlap_fails_loud():
    """A bracketed merge field genuinely overlaps a non-text icon (not a highlight -- the icon
    doesn't sit far enough under the text to peel as a fill) -> no valid guillotine cut -> the
    overlap-flattener would collapse them into one graphic cell. That cell must never be emitted."""
    tree = LayoutTree(
        psd="bad.psd",
        path="C:/fake/bad.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 300), z=0),
            _layer(2, "field text", "type", (0, 0, 100, 20), z=1, parent=1, text=TextInfo("[First Name]")),
            _layer(3, "icon", "shape", (50, 10, 150, 30), z=2, parent=1),
        ],
    )
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Bad Email", 300)

    # solve_artboard itself only REPORTS what the geometry produced (per table_solver's own
    # design) -- it still builds the (unsafe) graphic cell so the violation is inspectable...
    violations = find_baked_text_cells(solved)
    assert len(violations) == 1
    # baked_text_layer_ids only ever names the TEXT layer(s) in the cluster (id 3 is a plain
    # shape icon, not text) -- id 2 is the bracketed field.
    assert set(violations[0]["layer_ids"]) == {2}

    # ...but the pipeline that actually emits trees must never let it through.
    with pytest.raises(SafetyInvariantViolation) as exc_info:
        build_table_trees(tree)
    trapped_ids = {lid for v in exc_info.value.violations for lid in v["layer_ids"]}
    assert 2 in trapped_ids


def test_invariant_bracketed_field_inside_named_graphic_group_fails_loud():
    """The OTHER trap route: an editable field aggregated into a named `... graphic` group by
    the classifier (not a geometric overlap-flatten) must also fail loud."""
    tree = LayoutTree(
        psd="bad2.psd",
        path="C:/fake/bad2.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 300), z=0),
            _layer(2, "Badge graphic", "group", None, z=1, parent=1),
            _layer(3, "badge shape", "shape", (0, 0, 100, 100), z=2, parent=2),
            _layer(4, "badge field", "type", (10, 10, 90, 30), z=3, parent=2, text=TextInfo("[Name]")),
        ],
    )
    with pytest.raises(SafetyInvariantViolation) as exc_info:
        build_table_trees(tree)
    trapped_ids = {lid for v in exc_info.value.violations for lid in v["layer_ids"]}
    assert 4 in trapped_ids


def test_plain_non_editable_text_inside_named_graphic_group_fails_loud():
    """S1.2 Defect C: a `... graphic` group's member text does NOT need to be bracketed or
    highlight-marked to be a violation -- ANY live text baked into a graphic cell is forbidden
    (a graphic cell must be genuinely non-text decorative content only). Pre-Defect-C, this passed
    silently because `find_trapped_editable_cells` only looked at `editable`, and a plain caption
    like "Powered by Acme" is neither bracketed nor sitting on a highlight fill."""
    tree = LayoutTree(
        psd="bad3.psd",
        path="C:/fake/bad3.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 300), z=0),
            _layer(2, "Footer graphic", "group", None, z=1, parent=1),
            _layer(3, "footer icon", "shape", (0, 0, 100, 100), z=2, parent=2),
            _layer(4, "caption", "type", (10, 110, 90, 130), z=3, parent=2, text=TextInfo("Powered by Acme")),
        ],
    )
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Bad3 Email", 300)

    graphic_cells = [c for row in solved.rows for c in row.cells if c.role == "graphic"]
    assert len(graphic_cells) == 1
    assert graphic_cells[0].editable is False  # confirms this rides the NEW check, not the old one

    found = find_baked_text_cells(solved)
    assert len(found) == 1
    assert found[0]["layer_ids"] == [4]

    with pytest.raises(SafetyInvariantViolation) as exc_info:
        build_table_trees(tree)
    trapped_ids = {lid for v in exc_info.value.violations for lid in v["layer_ids"]}
    assert 4 in trapped_ids


def test_enforce_safety_invariant_withholds_whole_batch_not_just_bad_artboard():
    """A multi-artboard PSD where only ONE artboard is unsafe still raises for the whole build
    call (fail loud over the batch), rather than silently returning the other clean artboards."""
    tree = LayoutTree(
        psd="mixed.psd",
        path="C:/fake/mixed.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1, 10],
        layers=[
            _layer(1, "Email A", "artboard", (0, 0, 300, 300), z=0),
            _layer(2, "field text", "type", (0, 0, 100, 20), z=1, parent=1, text=TextInfo("[First Name]")),
            _layer(3, "icon", "shape", (50, 10, 150, 30), z=2, parent=1),
            _layer(10, "Email B", "artboard", (0, 0, 300, 300), z=3),
            _layer(11, "clean text", "type", (0, 0, 100, 20), z=4, parent=10, text=TextInfo("Hello")),
        ],
    )
    with pytest.raises(SafetyInvariantViolation):
        build_table_trees(tree)


# --- S1.1 Defect 3 / S1.2 Defect C: find_baked_text_cells inspects EVERY rasterized cell ---------


def test_find_baked_text_cells_recurses_into_nested_rows_container():
    """Defect 1 made Cell nestable (role="rows" wraps its own nested rows). A baked-text graphic
    cell buried two levels deep inside a CONTAINER cell must still be found -- the top-level scan
    alone (pre-Defect-3 behavior) would miss it entirely."""
    trapped_graphic = Cell(
        role="graphic",
        rect=BBox(0, 0, 50, 20),
        editable=True,
        image_source_layer_ids=[99],
        baked_text_layer_ids=[99],
    )
    nested_row = Row(cells=[trapped_graphic])
    container = Cell(role="rows", rect=BBox(0, 0, 50, 40), rows=[nested_row])
    tree = TableTree(email="Nested Email", width=300, rows=[Row(cells=[container])])

    found = find_baked_text_cells(tree)
    assert len(found) == 1
    assert found[0]["layer_ids"] == [99]


def test_button_swallowing_editable_field_is_trapped_and_fails_loud():
    """A `... button` group with TWO editable text members: layer_classifier keeps only the first
    as the live label and bakes the rest into the button image. Defect 3: this must be caught by
    find_baked_text_cells (not just the separate bracket-only intake_validator check) AND fail
    build_table_trees loud, exactly like a trapped graphic cell."""
    tree = LayoutTree(
        psd="btn.psd",
        path="C:/fake/btn.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "CTA button", "group", None, z=1, parent=1),
            _layer(3, "bg", "shape", (0, 0, 200, 50), z=2, parent=2),
            _layer(4, "label", "type", (10, 10, 100, 40), z=3, parent=2, text=TextInfo("[Label]")),
            _layer(5, "extra field", "type", (110, 10, 190, 40), z=4, parent=2, text=TextInfo("[Extra]")),
        ],
    )
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Btn Email", 300)

    found = find_baked_text_cells(solved)
    assert len(found) == 1
    assert found[0]["layer_ids"] == [5]

    with pytest.raises(SafetyInvariantViolation) as exc_info:
        build_table_trees(tree)
    trapped_ids = {lid for v in exc_info.value.violations for lid in v["layer_ids"]}
    assert trapped_ids == {5}


def test_button_swallowing_plain_non_editable_second_text_fails_loud():
    """S1.2 Defect C counterexample from the re-verify: a `... button` group with a live label
    ("Click") plus a SECOND, perfectly plain text layer ("SAVE10") that is NEITHER bracketed NOR
    highlight-marked -- the pre-Defect-C invariant (editable-only) let this through silently
    because neither text member trips `editable`. The tightened invariant must catch it: a button
    may keep exactly ONE live text label, so "SAVE10" is baked-in copy with no live copy
    surviving, regardless of its own editable status."""
    tree = LayoutTree(
        psd="btn2.psd",
        path="C:/fake/btn2.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "CTA button", "group", None, z=1, parent=1),
            _layer(3, "bg", "shape", (0, 0, 200, 50), z=2, parent=2),
            _layer(4, "label", "type", (10, 10, 100, 40), z=3, parent=2, text=TextInfo("Click")),
            _layer(5, "promo code", "type", (110, 10, 190, 40), z=4, parent=2, text=TextInfo("SAVE10")),
        ],
    )
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Btn2 Email", 300)

    # Neither text member is bracketed or highlight-marked -- confirm the pre-Defect-C signal
    # (editable / swallowed_editable_layer_ids) really is silent here, so this test is actually
    # exercising the NEW baked-text check and not accidentally riding the old one.
    button_cells = [c for row in solved.rows for c in row.cells if c.role == "button"]
    assert len(button_cells) == 1
    assert button_cells[0].editable is False
    assert not button_cells[0].swallowed_editable_layer_ids

    found = find_baked_text_cells(solved)
    assert len(found) == 1
    assert found[0]["layer_ids"] == [5]

    with pytest.raises(SafetyInvariantViolation) as exc_info:
        build_table_trees(tree)
    trapped_ids = {lid for v in exc_info.value.violations for lid in v["layer_ids"]}
    assert trapped_ids == {5}


# --- artboard slicing: build reliably yields one TableTree per artboard --------------------------


def test_build_table_trees_one_tree_per_artboard():
    tree = LayoutTree(
        psd="multi.psd",
        path="C:/fake/multi.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1, 2, 3],
        layers=[
            _layer(1, "Email A", "artboard", (0, 0, 300, 300), z=0),
            _layer(2, "a text", "type", (0, 0, 100, 20), z=1, parent=1, text=TextInfo("A")),
            _layer(10, "Email B", "artboard", (0, 0, 300, 300), z=2),
            _layer(11, "b text", "type", (0, 0, 100, 20), z=3, parent=10, text=TextInfo("B")),
        ],
    )
    # Only artboards 1 and 10 actually declared/populated (id 2 in artboards list has no matching
    # layer -- exercises the "declared but no eligible content" path too).
    tree.artboards = [1, 10]
    trees = build_table_trees(tree)
    assert len(trees) == 2
    assert {t.email for t in trees} == {"Email A", "Email B"}
    for t in trees:
        assert t.width == 300


# --- band expansion must never push a lone-link row's rect over the previous row -----------------


def test_lone_link_cell_in_band_does_not_overlap_previous_row():
    """A row whose ONLY cell is a link label sitting on a taller background band gets its rect
    band-expanded to the band's extent (the shape IS the button). When that band also overlaps the
    BOTTOM of the row above it, the overlap clamp must still fire -- and to fire it has to read the
    link cell's TRUE content top (its ink, inside the band), NOT the band-expanded rect. Reading
    the expanded rect puts content_top above the previous row's bottom, so the guard
    `content_top >= prev_bottom` fails, the clamp is skipped, and the solver emits overlapping
    row.rects (which break the emitter's cursor walk). Regression for that exact case."""
    tree = LayoutTree(
        psd="band.psd",
        path="C:/fake/band.psd",
        canvas=Canvas(width=600, height=400),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 600, 400), z=0),
            # full-width fill => background BAND. Its top (80) pokes up into the heading row above
            # (0..100), and it fully contains the link label below (130..160).
            _layer(2, "cta band", "shape", (0, 80, 600, 180), z=1, parent=1),
            _layer(3, "heading", "type", (0, 0, 600, 100), z=2, parent=1, text=TextInfo("Section Heading")),
            # a lone link label (content is a link hint, so it gets a link_slot) whose ink sits well
            # below the heading, but on the band that starts at 80.
            _layer(4, "Learn more", "type", (200, 130, 400, 160), z=3, parent=1, text=TextInfo("Learn more")),
        ],
    )
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Band Email", 600)

    rows = solved.rows
    assert len(rows) == 2

    # the link row must actually have been band-expanded (row.rect set to the band's span) --
    # otherwise this test would pass vacuously without exercising the expansion/clamp interaction.
    link_row = next(r for r in rows if len(r.cells) == 1 and r.cells[0].link_slot)
    assert link_row.rect is not None

    def _span(row):
        if row.rect is not None:
            return row.rect.top, row.rect.bottom
        tops = [c.rect.top for c in row.cells if c.rect is not None]
        bots = [c.rect.bottom for c in row.cells if c.rect is not None]
        return min(tops), max(bots)

    # No row may start above the previous row's bottom (hairline tolerance == grid_analyzer.EPSILON).
    hairline = 2
    for prev, cur in zip(rows, rows[1:], strict=False):
        assert _span(cur)[0] >= _span(prev)[1] - hairline


# --- _looks_like_link: the 40-char label cap + hint-word matching (idx 82) ----------------------
# The label cap is the "giant blue buttons" defect fix: body copy that merely MENTIONS a hint word
# ("...sales and marketing toolkit...") is prose, not a CTA.

from psd_html.table_solver import _LINK_LABEL_MAX_CHARS, _looks_like_link  # noqa: E402


def test_looks_like_link_rejects_long_prose_even_with_a_hint_word():
    prose = "Our sales and marketing toolkit has everything your team needs to close more deals."
    assert len(prose) > _LINK_LABEL_MAX_CHARS
    assert _looks_like_link("body copy", prose) is False


def test_looks_like_link_accepts_a_short_label_carrying_a_hint():
    assert _looks_like_link("cta", "Review the toolkit") is True     # 18 chars, hint "toolkit"
    assert _looks_like_link("footer", "Unsubscribe") is True


def test_looks_like_link_short_label_without_hint_is_not_a_link():
    assert _looks_like_link("headline", "Welcome aboard") is False


def test_looks_like_link_image_cell_uses_curated_name_only():
    # no content -> the layer NAME is all there is (image/icon cells).
    assert _looks_like_link("unsubscribe icon", "") is True
    assert _looks_like_link("product photo", "") is False


# --- hairline overlap snapping vs genuine overlap flattening (idx 74) ---------------------------


def test_hairline_x_overlap_snaps_to_valid_cut_not_flatten_even_with_bracket_field():
    """A ~1px x-overlap with full y-overlap (authoring hairline slop -- e.g. a bracketed field's
    bbox right=100 and the next image's bbox left=99) must snap to a clean guillotine cut -- two
    side-by-side cells -- not collapse into a role=graphic flatten. Uses a BRACKETED field so a
    regression that re-collapses this (e.g. changing `ox <= epsilon` to `<`, or collapsing the
    independent edge-snap ifs into if/else) would raise SafetyInvariantViolation for what is really
    authoring slop, taking CLI `build` non-zero on a conforming PSD. The genuine ~50px-overlap
    control (still flattens) is already covered by
    test_fallback_two_overlapping_icons_no_field_collapses_to_one_graphic_cell above."""
    tree = LayoutTree(
        psd="hairline.psd",
        path="C:/fake/hairline.psd",
        canvas=Canvas(width=300, height=100),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 100), z=0),
            _layer(2, "field text", "type", (0, 0, 100, 20), z=1, parent=1, text=TextInfo("[First Name]")),
            _layer(3, "photo", "pixel", (99, 0, 200, 20), z=2, parent=1),
        ],
    )
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    before = {i.layer_ids[0]: dict(i.bbox) for i in items}

    trees = build_table_trees(tree)  # must NOT raise -- the hairline snap prevents the flatten
    t = trees[0]
    assert len(t.rows) == 1
    assert len(t.rows[0].cells) == 2
    roles = [c.role for c in t.rows[0].cells]
    assert "graphic" not in roles
    assert set(roles) == {"text", "image"}

    # the snap pre-pass must never mutate the ClassifiedItem bboxes it was handed (it builds NEW
    # rect-record dicts internally, upstream of the untouched, exact _find_valid_cut).
    after = {i.layer_ids[0]: dict(i.bbox) for i in items}
    assert after == before


# --- highlight box_rect union / bracket-token routing (idx 75, 83) -------------------------------


def _highlight_tree(chip_bbox, text_bbox, content, width=300, height=100):
    return LayoutTree(
        psd="chip.psd",
        path="C:/fake/chip.psd",
        canvas=Canvas(width=width, height=height),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, width, height), z=0),
            _layer(2, "chip", "shape", chip_bbox, z=1, parent=1),
            _layer(3, "label", "type", text_bbox, z=2, parent=1, text=TextInfo(content)),
        ],
    )


def test_highlight_chip_containing_plain_text_boxes_the_chip_not_the_text_union():
    """A highlight/chip that fully CONTAINS a plain (non-bracket) text: the cell takes the CHIP's
    own rect EXACTLY (not unioned with the text bbox -- that re-inflates placeholder chips), keeps
    the text's own ink bounds in text_rect, and its background points at the chip's layer id."""
    tree = _highlight_tree((5, 35, 115, 65), (10, 40, 110, 60), "Steve Rogers")
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Chip Email", 300)

    assert len(solved.rows) == 1
    cell = solved.rows[0].cells[0]
    assert cell.role == "text"
    assert cell.rect == BBox(5, 35, 115, 65)
    assert cell.text_rect == BBox(10, 40, 110, 60)
    assert cell.background is not None
    assert cell.background.image_source_layer_id == 2


def test_highlight_chip_containing_bracket_whole_token_routes_to_sub_highlights():
    """A whole-token bracketed field ('[Company Sign-Off]') fully contained by a chip must NOT take
    the box path (an _is_bracket_token regression would bake the brackets inside the shaded box):
    the cell keeps its OWN text rect and the chip lands in sub_highlights instead."""
    tree = _highlight_tree((5, 35, 115, 65), (10, 40, 110, 60), "[Company Sign-Off]")
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Bracket Chip Email", 300)

    assert len(solved.rows) == 1
    cell = solved.rows[0].cells[0]
    assert cell.role == "text"
    assert cell.rect == BBox(10, 40, 110, 60)  # the text's OWN rect, not the chip's
    assert cell.text_rect is None
    assert cell.sub_highlights is not None
    assert any(BBox(**hb) == BBox(5, 35, 115, 65) for hb in cell.sub_highlights)


def test_highlight_partial_substring_lands_in_sub_highlights_rect_unchanged():
    """A highlight that backs only PART of a longer text (a merge-token chip inside a paragraph --
    not whole-cell containment) lands in sub_highlights and leaves the cell's own rect untouched
    (no box_rect applied)."""
    tree = _highlight_tree((100, 35, 180, 65), (10, 40, 300, 60), "Alpha Beta Gamma")
    by_id = {l.id: l for l in tree.layers}
    items = classify_artboard(tree, 1, by_id)
    solved = solve_artboard(items, "Substring Email", 300)

    assert len(solved.rows) == 1
    cell = solved.rows[0].cells[0]
    assert cell.role == "text"
    assert cell.rect == BBox(10, 40, 300, 60)  # unchanged -- no box path taken
    assert cell.text_rect is None
    assert cell.sub_highlights is not None
    assert any(BBox(**hb) == BBox(100, 35, 180, 65) for hb in cell.sub_highlights)


# --- nested "row" branch inside a column: the stat-block sub-table (idx 76, 77) ------------------


def test_stacked_pair_beside_tall_text_becomes_nested_rows_container():
    """A left column of two stacked type layers (a stat number over its caption) beside one tall
    right-column text: the solved tree is ONE row with TWO cells -- cells[0] is a role='rows'
    CONTAINER wrapping 2 nested single-cell rows (both carrying live text), cells[0].rect is the
    union bbox of its members, and cells[1] is the tall right column's own live text. Pins the
    'row' cut-tree branch (_cell_from_node / _rows_from_node / _flatten_by_axis) plus the
    _is_row_split/_is_col_split axis detection -- a regression here could collapse the further-
    splittable column into one graphic image, or mis-structure/mis-size the nested sub-table.
    No text is ever baked."""
    tree = LayoutTree(
        psd="stat.psd",
        path="C:/fake/stat.psd",
        canvas=Canvas(width=400, height=100),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 400, 100), z=0),
            _layer(2, "number", "type", (0, 0, 200, 50), z=1, parent=1, text=TextInfo("87%")),
            _layer(3, "caption", "type", (0, 50, 200, 100), z=2, parent=1, text=TextInfo("Retention")),
            _layer(4, "body", "type", (200, 0, 400, 100), z=3, parent=1, text=TextInfo("Body copy about the stat.")),
        ],
    )
    trees = build_table_trees(tree)  # must NOT raise -- all live text, nothing baked
    t = trees[0]

    assert len(t.rows) == 1
    assert len(t.rows[0].cells) == 2
    stat_col, body_cell = t.rows[0].cells

    assert stat_col.role == "rows"
    assert stat_col.rect == BBox(0, 0, 200, 100)
    assert len(stat_col.rows) == 2
    assert [len(r.cells) for r in stat_col.rows] == [1, 1]
    assert stat_col.rows[0].cells[0].text.content == "87%"
    assert stat_col.rows[1].cells[0].text.content == "Retention"

    assert body_cell.role == "text"
    assert body_cell.text.content == "Body copy about the stat."

    assert find_baked_text_cells(t) == []


# --- band-to-row background: highest z wins among CONTAINING bands (idx 78) ----------------------


def test_band_background_winner_is_highest_z_not_smallest_area_or_touch():
    """Among several bands that CONTAIN one row, the winner is the one painted LAST (highest z) --
    not the smallest-area band, not the largest (page-fill), and never a band that merely TOUCHES
    the row's edge without containing it."""
    row_bbox = {"left": 50, "top": 50, "right": 250, "bottom": 90}
    items = [
        ClassifiedItem(role=ROLE_CONTENT, name="body", kind="type", bbox=row_bbox,
                        is_text=True, z=10, layer_ids=[1], text=TextInfo("Row Text"),
                        text_member_layer_ids=[1]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="page fill", kind="shape",
                        bbox={"left": 0, "top": 0, "right": 600, "bottom": 400},
                        is_text=False, z=1, layer_ids=[2]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="tiny", kind="shape",
                        bbox={"left": 40, "top": 40, "right": 260, "bottom": 100},
                        is_text=False, z=2, layer_ids=[3]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="card", kind="shape",
                        bbox={"left": 20, "top": 20, "right": 300, "bottom": 120},
                        is_text=False, z=5, layer_ids=[4]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="touch", kind="shape",
                        bbox={"left": 240, "top": 50, "right": 260, "bottom": 90},
                        is_text=False, z=3, layer_ids=[5]),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        solved = solve_artboard(items, "Band Contest Email", 600)

    assert len(solved.rows) == 1
    row = solved.rows[0]
    assert row.background is not None
    assert row.background.image_source_layer_id == 4  # the card -- highest z among containing bands
    assert row.background.image_source_layer_id != 5  # the merely-touching band is never assigned


# --- row-scale band expansion must not be starved by an enclosing section panel (idx 79) ---------


def test_tight_button_band_wins_over_enclosing_section_panel():
    """A ~15px link_slot text cell sits on a tight ~46px row-scale band, which itself sits inside a
    much larger section panel (panel height > 3*row_h+60). The row (and its lone cell) must expand
    to the TIGHT band's span, not the whole enclosing panel -- otherwise the CTA row over-expands
    and pushes every row below it tens of px adrift."""
    items = [
        ClassifiedItem(role=ROLE_CONTENT, name="Learn more link", kind="type",
                        bbox={"left": 100, "top": 200, "right": 300, "bottom": 215}, is_text=True,
                        z=10, layer_ids=[1], text=TextInfo("Learn more"), text_member_layer_ids=[1]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="cta shape", kind="shape",
                        bbox={"left": 80, "top": 190, "right": 320, "bottom": 236},
                        is_text=False, z=5, layer_ids=[2]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="section panel", kind="shape",
                        bbox={"left": 0, "top": 0, "right": 600, "bottom": 800},
                        is_text=False, z=1, layer_ids=[3]),
    ]
    solved = solve_artboard(items, "CTA Email", 600)

    assert len(solved.rows) == 1
    row = solved.rows[0]
    tight_span = BBox(80, 190, 320, 236)
    assert row.rect == tight_span
    assert row.rect.bottom - row.rect.top == 46
    assert row.cells[0].rect == tight_span
    assert row.background is not None
    assert row.background.image_source_layer_id == 2  # the tight band, not the panel (id 3)


# --- standalone/enclosing/partial background bands never bake or duplicate live rows (idx 80) ---


def test_standalone_fullbleed_band_with_no_content_becomes_one_image_row():
    """A full-bleed ROLE_BACKGROUND band with NO content items at all must become exactly one
    image Row spanning the band's own bbox (the 'missing hero' fix)."""
    items = [
        ClassifiedItem(role=ROLE_BACKGROUND, name="hero bg", kind="shape",
                        bbox={"left": 0, "top": 0, "right": 600, "bottom": 300},
                        is_text=False, z=1, layer_ids=[9]),
    ]
    solved = solve_artboard(items, "Hero Email", 600)

    assert len(solved.rows) == 1
    cell = solved.rows[0].cells[0]
    assert cell.role == "image"
    assert cell.rect == BBox(0, 0, 600, 300)
    assert cell.image_source_layer_ids == [9]


def test_enclosing_panel_fallback_covers_only_the_bare_row_no_extra_image_row():
    """A page-fill band enclosing two content rows, where one row already has its own tighter
    (higher-z) background: the enclosing panel must never overwrite the already-backed row, must
    end up as the background of the bare row instead, and must never ALSO emit a stray image row
    for itself (that would render the section twice)."""
    items = [
        ClassifiedItem(role=ROLE_CONTENT, name="row1", kind="type",
                        bbox={"left": 0, "top": 0, "right": 300, "bottom": 50}, is_text=True, z=2,
                        layer_ids=[1], text=TextInfo("Row 1"), text_member_layer_ids=[1]),
        ClassifiedItem(role=ROLE_CONTENT, name="row2", kind="type",
                        bbox={"left": 0, "top": 50, "right": 300, "bottom": 100}, is_text=True, z=3,
                        layer_ids=[2], text=TextInfo("Row 2"), text_member_layer_ids=[2]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="row1 tight bg", kind="shape",
                        bbox={"left": 0, "top": 0, "right": 300, "bottom": 50},
                        is_text=False, z=5, layer_ids=[10]),
        # tall enough that it can never win the row-scale (shape-sizing) contest either --
        # this test is purely about background PAINTING, not shape sizing.
        ClassifiedItem(role=ROLE_BACKGROUND, name="page fill", kind="shape",
                        bbox={"left": 0, "top": 0, "right": 300, "bottom": 300},
                        is_text=False, z=1, layer_ids=[20]),
    ]
    solved = solve_artboard(items, "Fallback Email", 300)

    assert len(solved.rows) == 2
    row1 = next(r for r in solved.rows if r.cells[0].text.content == "Row 1")
    row2 = next(r for r in solved.rows if r.cells[0].text.content == "Row 2")
    assert row1.background.image_source_layer_id == 10  # its own tighter band, untouched
    assert row2.background.image_source_layer_id == 20  # the page-fill, as fallback
    # never baked into a standalone image row of its own
    assert all(c.role != "image" for r in solved.rows for c in r.cells)


def test_partial_width_band_attaches_to_the_cell_it_fully_contains():
    """A partial-width band that intersects a row but doesn't contain the WHOLE row lands as the
    background of whichever cell it DOES fully contain, never as a new image row, and never on a
    cell it does not contain."""
    items = [
        ClassifiedItem(role=ROLE_CONTENT, name="left text", kind="type",
                        bbox={"left": 0, "top": 50, "right": 200, "bottom": 90}, is_text=True, z=2,
                        layer_ids=[1], text=TextInfo("Left"), text_member_layer_ids=[1]),
        ClassifiedItem(role=ROLE_CONTENT, name="right text", kind="type",
                        bbox={"left": 220, "top": 50, "right": 400, "bottom": 90}, is_text=True, z=3,
                        layer_ids=[2], text=TextInfo("Right"), text_member_layer_ids=[2]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="left panel", kind="shape",
                        bbox={"left": 0, "top": 55, "right": 200, "bottom": 85},
                        is_text=False, z=1, layer_ids=[30]),
    ]
    solved = solve_artboard(items, "Partial Band Email", 400)

    assert len(solved.rows) == 1
    row = solved.rows[0]
    assert len(row.cells) == 2
    left_cell = next(c for c in row.cells if c.text.content == "Left")
    right_cell = next(c for c in row.cells if c.text.content == "Right")
    assert left_cell.background is not None
    assert left_cell.background.image_source_layer_id == 30
    assert right_cell.background is None
    assert all(c.role != "image" for c in row.cells)


# --- row ordering uses CONTENT top, never a band-expanded rect top (idx 81) ----------------------


def test_hero_band_first_then_two_shared_lower_rows_kept_in_design_order():
    """A full-bleed hero band strictly ABOVE (no bbox overlap with) two content rows, each of which
    gets its own row-scale band expansion -- engineered so the second row's raw band top (175)
    sits ABOVE the first row's raw band top (180), which would reorder them under a naive
    rect.top-based sort. The emitted order must still be: hero image row first, then the two rows
    in their real CONTENT-top order, laid out without overlap."""
    items = [
        ClassifiedItem(role=ROLE_BACKGROUND, name="hero bg", kind="shape",
                        bbox={"left": 0, "top": 0, "right": 600, "bottom": 150},
                        is_text=False, z=1, layer_ids=[100]),
        ClassifiedItem(role=ROLE_CONTENT, name="Learn more link", kind="type",
                        bbox={"left": 0, "top": 200, "right": 300, "bottom": 215}, is_text=True, z=2,
                        layer_ids=[1], text=TextInfo("Learn more"), text_member_layer_ids=[1]),
        ClassifiedItem(role=ROLE_CONTENT, name="Unsubscribe link", kind="type",
                        bbox={"left": 0, "top": 230, "right": 600, "bottom": 245}, is_text=True, z=3,
                        layer_ids=[2], text=TextInfo("Unsubscribe"), text_member_layer_ids=[2]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="A shape", kind="shape",
                        bbox={"left": 0, "top": 180, "right": 300, "bottom": 225},
                        is_text=False, z=5, layer_ids=[10]),
        ClassifiedItem(role=ROLE_BACKGROUND, name="B shape", kind="shape",
                        bbox={"left": 0, "top": 175, "right": 600, "bottom": 250},
                        is_text=False, z=6, layer_ids=[20]),
    ]
    solved = solve_artboard(items, "Hero Order Email", 600)

    assert len(solved.rows) == 3
    hero_row, row_a, row_b = solved.rows
    assert hero_row.cells[0].role == "image"
    assert hero_row.cells[0].rect == BBox(0, 0, 600, 150)
    assert row_a.cells[0].text.content == "Learn more"
    assert row_b.cells[0].text.content == "Unsubscribe"

    # the real, design-order rows must still be laid out without overlap (hairline tolerance ==
    # grid_analyzer.EPSILON, matching the existing lone-link-cell test's convention above).
    hairline = 2
    assert row_a.rect.top >= hero_row.cells[0].rect.bottom - hairline
    assert row_b.rect.top >= row_a.rect.bottom - hairline
