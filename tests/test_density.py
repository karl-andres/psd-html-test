"""Tests for density/retina scaling in the emitter.

Self-contained (synthetic trees + a synthetic composite) so the TREE and the pixel SOURCE stay
consistent -- a doubled tree is a 2x-authored PSD, and its composite must also be 2x (a real 2x PSD
has a 2x composite; feeding a 2x tree against a 1x composite would crop out of bounds, which is a
test artifact, not a product path).

The load-bearing proof is EQUIVALENCE: a tree DOUBLED emitted at density=2 must reproduce the SAME
~1x CSS layout as the original at density=1 -- only asset PIXEL dims differ. Plus a strict
default-1.0 no-op and a retina-asset dimension check.
"""
from __future__ import annotations

import pathlib
import re

from psd_html.html_emitter import emit
from psd_html.layer_router import route
from psd_html.layout_tree import BBox, TextInfo, TextRun
from psd_html.table_tree import Background, Cell, Row, TableTree

_WIDTHS = re.compile(r'width="(\d+)"')
_HEIGHTS = re.compile(r'height="(\d+)"')
_FONTS = re.compile(r"font-size:([\d.]+)px")


def _geometry(html: str):
    """The ordered layout numbers that must be scale-invariant across an equivalent emit."""
    return (_WIDTHS.findall(html), _HEIGHTS.findall(html), _FONTS.findall(html))


def _text(content, font="Arial", size=20.0):
    return TextInfo(content=content, align="left", runs=[TextRun(font=font, size=size, color="#000000")])


# --- default density is a strict no-op ------------------------------------------------------------


def _tiny_tree():
    cell = Cell(role="text", rect=BBox(0, 0, 200, 40), text=_text("Hello world"))
    return TableTree(email="T", width=600, rows=[Row(cells=[cell])])


def test_density_1_is_byte_identical_to_no_arg(tmp_path):
    a = pathlib.Path(emit(route(_tiny_tree(), "hybrid"), tmp_path / "a")["index_path"]).read_text(encoding="utf-8")
    b = pathlib.Path(emit(route(_tiny_tree(), "hybrid"), tmp_path / "b", density=1.0)["index_path"]).read_text(encoding="utf-8")
    assert a == b


# --- equivalence: doubled tree @ density 2 == original @ density 1 (pure layout, no rasters) ------


def _layout_tree():
    """A text-only tree (no image/graphic/background -> needs no composite) exercising the layout
    scaling: side-by-side cells with a gap, a leading gap, a vertical gap, and a nested rows
    container (a number stacked over a caption -- the stat pattern the earlier bug lived in).

    Row 3/4 add the two S1.2 geometry fields the flagship equivalence test used to skip: a
    band-expanded row (Row.rect spans a taller button shape than the label's own cell rect -- and
    the FOLLOWING row's vertical gap is measured off that expanded bottom, so a dropped/mis-scaled
    Row.rect shows up as a wrong spacer height) and a box-backed text cell (cell.rect = the fill
    shape, cell.text_rect = the label's own inset ink bounds, which feeds the shrink-to-fit width
    budget -- a dropped/mis-scaled text_rect shows up as a wrong shrunk font-size) plus a substring
    sub_highlight chip on the same cell (idx100/idx101: _double_cell/_double_row must double these
    the same way _double_bbox doubles Cell.rect)."""
    row1 = Row(cells=[
        Cell(role="text", rect=BBox(40, 20, 240, 60), text=_text("Left column copy")),
        Cell(role="text", rect=BBox(300, 20, 560, 60), text=_text("Right column copy")),
    ])
    nested = Cell(role="rows", rect=BBox(40, 120, 240, 240), rows=[
        Row(cells=[Cell(role="text", rect=BBox(40, 120, 240, 180), text=_text("70%", size=40.0))]),
        Row(cells=[Cell(role="text", rect=BBox(40, 190, 240, 240), text=_text("of orgs"))]),
    ])
    row2 = Row(cells=[nested, Cell(role="text", rect=BBox(300, 120, 560, 240), text=_text("Body copy here"))])
    row3 = Row(
        cells=[Cell(role="text", rect=BBox(40, 300, 300, 360),
                    background=Background(color="#FFCC00"),
                    text=_text("Confirm and continue today", size=32.0),
                    text_rect=BBox(44, 310, 296, 336),
                    sub_highlights=[{"left": 60, "top": 312, "right": 140, "bottom": 334}])],
        rect=BBox(40, 280, 300, 380),  # band-expanded: taller than the cell's own rect above
    )
    row4 = Row(cells=[Cell(role="text", rect=BBox(40, 420, 300, 460), text=_text("Fine print"))])
    return TableTree(email="T", width=600, rows=[row1, row2, row3, row4])


def _double_bbox(b):
    return BBox(left=b.left * 2, top=b.top * 2, right=b.right * 2, bottom=b.bottom * 2)


def _double_text(t):
    if t is None:
        return None
    return TextInfo(content=t.content, align=t.align,
                    runs=[TextRun(font=r.font, size=(r.size * 2 if r.size else r.size), color=r.color) for r in (t.runs or [])])


def _double_highlight(h):
    """Double a sub_highlight bbox dict (design-coord left/top/right/bottom) the same way
    _double_bbox doubles a BBox -- preserving any other keys unchanged, mirroring
    html_emitter._scale_highlight's own shape."""
    return {**h, "left": h["left"] * 2, "top": h["top"] * 2, "right": h["right"] * 2, "bottom": h["bottom"] * 2}


def _double_cell(c):
    return Cell(role=c.role, rect=_double_bbox(c.rect), background=c.background, editable=c.editable,
                link_slot=c.link_slot, colspan=c.colspan, text=_double_text(c.text),
                image_source_layer_ids=c.image_source_layer_ids,
                rows=[_double_row(r) for r in c.rows] if c.rows else c.rows,
                source_layer_id=c.source_layer_id,
                text_rect=_double_bbox(c.text_rect) if c.text_rect is not None else None,
                sub_highlights=([_double_highlight(h) for h in c.sub_highlights]
                                if c.sub_highlights is not None else None))


def _double_row(r):
    return Row(background=r.background, cells=[_double_cell(c) for c in r.cells],
               rect=_double_bbox(r.rect) if r.rect is not None else None)


def _double_tree(t):
    return TableTree(email=t.email, width=t.width * 2, rows=[_double_row(r) for r in t.rows])


def test_doubled_tree_at_density_2_matches_original_at_density_1(tmp_path):
    tree = _layout_tree()
    html1 = pathlib.Path(emit(route(tree, "hybrid"), tmp_path / "d1", density=1.0)["index_path"]).read_text(encoding="utf-8")
    html2 = pathlib.Path(emit(route(_double_tree(tree), "hybrid"), tmp_path / "d2", density=2.0)["index_path"]).read_text(encoding="utf-8")
    w1, h1, f1 = _geometry(html1)
    w2, h2, f2 = _geometry(html2)
    assert w1 == w2, "table/cell/spacer widths must match between original@1x and doubled@2x"
    assert h1 == h2, "heights must match"
    assert f1 == f2, "font-sizes must match"


def test_doubled_tree_outer_table_emits_at_1x_width(tmp_path):
    tree = _layout_tree()
    html = pathlib.Path(emit(route(_double_tree(tree), "hybrid"), tmp_path / "d2", density=2.0)["index_path"]).read_text(encoding="utf-8")
    outer = re.search(r'<table[^>]*width="(\d+)"[^>]*table-layout:fixed', html)
    assert outer and int(outer.group(1)) == tree.width, "a 2x tree at density 2 must lay out at the ~1x email width"


def test_scale_routed_scales_row_rect_text_rect_and_sub_highlights_like_cell_rect():
    """Direct guard on the scaling helpers themselves (html_emitter._scale_cell/_scale_row, invoked
    via scale_routed): Row.rect (band-expansion), Cell.text_rect (box-backed ink bounds), and
    Cell.sub_highlights (substring highlight chips) must divide by density exactly like Cell.rect
    does. The end-to-end equivalence test above exercises these fields through a real emit(), but
    can't cleanly discriminate a sub_highlights-scaling regression from the rendered HTML alone (a
    substring highlight only ever shows up as a background-color span, invisible to the
    width/height/font-size geometry the equivalence test checks) -- so assert the scaled tree's
    fields directly instead. Before this test, no test anywhere fed scale_routed a tree with these
    fields set, so a future dataclass-reconstruction refactor that forgot one of these kwargs (the
    same failure class as the idx24 TextRun/TextInfo regression) would silently stop scaling it --
    band spacing or a highlight chip would land at the wrong position on a real 2x-authored PSD --
    with the whole suite green."""
    from psd_html.html_emitter import scale_routed

    boxed = Cell(
        role="text",
        rect=BBox(300, 260, 560, 330),
        background=Background(color="#FFCC00"),
        text=_text("Boxed CTA"),
        text_rect=BBox(304, 270, 556, 300),
        sub_highlights=[{"left": 320, "top": 272, "right": 400, "bottom": 296}],
    )
    row = Row(cells=[boxed], rect=BBox(260, 260, 600, 340))
    tree = TableTree(email="T", width=600, rows=[row])

    scaled = scale_routed(route(tree, "hybrid"), 2.0)
    srow = scaled.tree.rows[0]
    scell = srow.cells[0]

    assert srow.rect == BBox(130, 130, 300, 170), "Row.rect must scale like Cell.rect"
    assert scell.text_rect == BBox(152, 135, 278, 150), "Cell.text_rect must scale like Cell.rect"
    assert scell.sub_highlights == [{"left": 160, "top": 136, "right": 200, "bottom": 148}], (
        "Cell.sub_highlights must scale like Cell.rect"
    )


# --- retina: at density 2, a raster asset carries ~2x the pixels it displays ----------------------


def _synthetic_composite(w, h):
    from PIL import Image

    return Image.new("RGBA", (w, h), (10, 120, 200, 255))


def test_density_2_asset_is_higher_resolution_than_display(tmp_path):
    from PIL import Image

    # a 2x-authored image cell (rect in 2x px) against a matching 2x composite
    img_cell = Cell(role="image", rect=BBox(0, 0, 120, 80), image_source_layer_ids=[1])
    tree = TableTree(email="T", width=240, rows=[Row(cells=[img_cell])])
    comp = _synthetic_composite(240, 160)  # covers the 2x rect

    out = tmp_path / "d2"
    result = emit(route(tree, "hybrid"), out, composite=comp, density=2.0)
    html = pathlib.Path(result["index_path"]).read_text(encoding="utf-8")
    m = re.search(r'<img src="(assets/[^"]+\.png)" width="(\d+)" height="(\d+)"', html)
    assert m, "expected an <img> asset"
    rel, disp_w = m.group(1), int(m.group(2))
    with Image.open(out / rel) as im:
        actual_w = im.width
    # display is the CSS (1x) size; the PNG carries ~2x the pixels
    assert disp_w == 60, f"expected 120px 2x rect to DISPLAY at 60 CSS px, got {disp_w}"
    assert actual_w >= disp_w * 2 - 2, f"density=2 asset should carry ~2x pixels (actual={actual_w}, display={disp_w})"

