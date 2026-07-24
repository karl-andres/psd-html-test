"""TableTree: the S1 -> S2 contract's dataclass round-trips to/from dict/JSON.

bakeoff.py:309 runs TableTree.from_dict(tree.to_dict()) on every real `bakeoff` CLI invocation, and
cli.py's `build` command writes trees_to_json to disk for a later S2 consumer to reload with
trees_from_json/from_dict. Both paths must carry every field -- especially the S1.1/S1.2 fields
(source_layer_id, swallowed_editable_layer_ids, baked_text_layer_ids, text_rect, sub_highlights,
Row.rect) the module's own docstring calls out as the exact class of prior field-drift defects --
or a future field silently dropped from to_dict/from_dict would corrupt real emitted bundles
(e.g. a dropped Row.rect reintroduces the documented band-under-spacing drift) while leaving
find_baked_text_cells blind on anything reloaded from disk.
"""

from __future__ import annotations

from psd_html.layout_tree import BBox, TextInfo, TextRun
from psd_html.table_solver import find_baked_text_cells
from psd_html.table_tree import Background, Cell, Row, TableTree


def _maximal_tree() -> TableTree:
    """One TableTree exercising every optional field on Cell/Row, including:
      - a nested role="rows" CONTAINER cell (Cell.rows -> Row.from_dict recursion), with a
        role="graphic" cell baked two levels deep (so find_baked_text_cells' recursive walk has
        something to find below the top level too);
      - a box-backed text cell carrying text_rect + sub_highlights + a Background;
      - a Row.rect band-expansion override;
      - a role="button" cell carrying both swallowed_editable_layer_ids and baked_text_layer_ids.
    """
    nested_rows = [
        Row(cells=[Cell(role="text", rect=BBox(0, 0, 300, 50), text=TextInfo(content="Nested A"),
                         source_layer_id=201)]),
        Row(cells=[Cell(role="graphic", rect=BBox(300, 0, 600, 50), image_source_layer_ids=[210],
                         baked_text_layer_ids=[211, 212], source_layer_id=None)]),
    ]

    row_container = Row(cells=[Cell(role="rows", rect=BBox(0, 0, 600, 50), rows=nested_rows)])

    boxed_text_row = Row(
        background=Background(color="#EEEEEE", image_source_layer_id=None),
        cells=[
            Cell(
                role="text",
                rect=BBox(0, 50, 600, 120),
                background=Background(color="#FFCC00", image_source_layer_id=301),
                text=TextInfo(content="Boxed text", align="center",
                              runs=[TextRun(font="Arial", size=14.0, color="#000000")]),
                text_rect=BBox(10, 60, 590, 100),
                sub_highlights=[
                    {"left": 20, "top": 65, "right": 100, "bottom": 95},
                    {"left": 200, "top": 65, "right": 280, "bottom": 95},
                ],
                source_layer_id=303,
            )
        ],
        rect=BBox(0, 50, 600, 130),  # band-expansion override, taller than the cell's own rect
    )

    button_row = Row(
        cells=[
            Cell(
                role="button",
                rect=BBox(0, 130, 200, 180),
                editable=False,
                link_slot="cta-slot",
                colspan=2,
                image_source_layer_ids=[401, 402],
                swallowed_editable_layer_ids=[403],
                baked_text_layer_ids=[403, 404],
                source_layer_id=None,
            )
        ]
    )

    return TableTree(email="maximal-fixture", width=600, rows=[row_container, boxed_text_row, button_row])


def test_maximal_tree_dict_round_trip_is_lossless():
    tree = _maximal_tree()
    d = tree.to_dict()
    reloaded = TableTree.from_dict(d)
    assert reloaded.to_dict() == d


def test_maximal_tree_json_round_trip_is_lossless():
    tree = _maximal_tree()
    s = tree.to_json()
    reloaded = TableTree.from_json(s)
    assert reloaded.to_dict() == tree.to_dict()


def test_maximal_tree_round_trip_preserves_every_s1_1_s1_2_field():
    tree = _maximal_tree()
    reloaded = TableTree.from_dict(tree.to_dict())

    # Row 0: the nested role="rows" container survived, cells reachable via .rows on both sides.
    container_cell = reloaded.rows[0].cells[0]
    assert container_cell.role == "rows"
    assert container_cell.rows is not None
    assert len(container_cell.rows) == 2
    nested_text = container_cell.rows[0].cells[0]
    assert nested_text.source_layer_id == 201
    nested_graphic = container_cell.rows[1].cells[0]
    assert nested_graphic.role == "graphic"
    assert nested_graphic.baked_text_layer_ids == [211, 212]

    # Row 1: box-backed text cell -- text_rect, sub_highlights, background, and the Row.rect
    # band-expansion override all round-trip exactly.
    boxed_row = reloaded.rows[1]
    assert boxed_row.background is not None
    assert boxed_row.background.color == "#EEEEEE"
    assert boxed_row.rect == BBox(0, 50, 600, 130)
    boxed_cell = boxed_row.cells[0]
    assert boxed_cell.background is not None
    assert boxed_cell.background.color == "#FFCC00"
    assert boxed_cell.background.image_source_layer_id == 301
    assert boxed_cell.text_rect == BBox(10, 60, 590, 100)
    assert boxed_cell.sub_highlights == [
        {"left": 20, "top": 65, "right": 100, "bottom": 95},
        {"left": 200, "top": 65, "right": 280, "bottom": 95},
    ]
    assert boxed_cell.source_layer_id == 303

    # Row 2: button cell -- swallowed_editable_layer_ids and baked_text_layer_ids both survive,
    # distinctly (they are not the same field and must not collapse into each other).
    button_cell = reloaded.rows[2].cells[0]
    assert button_cell.role == "button"
    assert button_cell.link_slot == "cta-slot"
    assert button_cell.colspan == 2
    assert button_cell.swallowed_editable_layer_ids == [403]
    assert button_cell.baked_text_layer_ids == [403, 404]


def test_find_baked_text_cells_gives_identical_results_before_and_after_round_trip():
    """The exact safety scan the module docstring says exists for this reason: a tree rebuilt
    from disk (cli.py's `build` -> trees_to_json, or bakeoff.py:309's from_dict(to_dict())) must
    leave find_baked_text_cells just as sighted as the in-memory tree it was built from --
    including the baked cell nested two levels deep inside a role="rows" container."""
    tree = _maximal_tree()
    reloaded = TableTree.from_dict(tree.to_dict())

    before = find_baked_text_cells(tree)
    after = find_baked_text_cells(reloaded)

    assert before == after
    # Both the depth-2 nested graphic cell and the top-level button cell are found.
    assert len(before) == 2
    layer_id_sets = [set(v["layer_ids"]) for v in before]
    assert {211, 212} in layer_id_sets
    assert {403, 404} in layer_id_sets
