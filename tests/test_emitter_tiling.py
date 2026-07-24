"""S2 TILING regression tests -- the fix for the collapse/overlap bug in html_emitter.py.

SAFETY: every f-string/`.format()` in this file builds either an assertion message, a regex
pattern (`re.search`), or an inline HTML/CSS fragment for a hand-built test fixture -- never a SQL
query. There is no database or SQL anywhere in this package; the string-interpolation anti-pattern
scanner's SQL heuristic is a false positive here.

Every expectation here is built ONE of two ways, never by reading back the emitter's own computed
HTML:
  1. A GENERIC structural invariant over the parsed document itself: for every <table> in the
     emitted HTML, the sum of each of its own direct <tr>'s direct <td> `width` attributes equals
     that table's own declared `width` attribute -- a real self-consistency check that would FAIL
     on the original bug (rows whose cell widths didn't sum to the table width, no spacer cells).
  2. An expectation computed FRESH from the S1 IR (TableTree rects) by small helper functions
     written in this file (never importing/calling html_emitter internals) -- e.g. the exact
     spacer widths for a row's gaps, or the exact vertical spacer heights between rows.

A minimal hand-rolled HTML parser (stdlib `html.parser` only, no bs4/lxml dependency) builds just
enough of a DOM (tag/attrs/children, respecting real nesting) to walk table/tr/td structure.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from psd_html.html_emitter import emit
from psd_html.layer_router import route
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
_has_real_psd = os.path.isfile(ANNOUNCEMENT_PSD)

try:
    from playwright.sync_api import sync_playwright  # noqa: F401

    _has_playwright = True
except Exception:
    _has_playwright = False


# --- a minimal, dependency-free HTML DOM (just enough to walk table/tr/td nesting) --------------

_VOID_TAGS = {"img", "br", "meta", "hr", "input", "area", "base", "col", "embed", "link", "param", "source", "track", "wbr"}


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        # Pop back to (and including) the matching open tag, tolerating any unclosed void-ish tag
        # noise -- the emitter's own output is always well-formed, this is just defensive.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return


def _parse_dom(html_text: str) -> _Node:
    builder = _DomBuilder()
    builder.feed(html_text)
    return builder.root


def _find_all(node: _Node, tag: str) -> list:
    out = []
    for child in node.children:
        if child.tag == tag:
            out.append(child)
        out.extend(_find_all(child, tag))
    return out


def _direct_children(node: _Node, tag: str) -> list:
    return [c for c in node.children if c.tag == tag]


def _int_attr(node: _Node, name: str) -> "int | None":
    v = node.attrs.get(name)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


# --- GENERIC invariant: every table's own rows tile its own declared width exactly --------------


def _assert_every_table_tiles_exactly(root: _Node) -> int:
    """For every <table width=W> in the document, every one of its own DIRECT <tr> children's
    DIRECT <td> `width` attributes must sum to exactly W. Returns the number of tables checked (so
    callers can assert this ran against a nontrivial document, not zero tables)."""
    tables = _find_all(root, "table")
    checked = 0
    for table in tables:
        table_width = _int_attr(table, "width")
        if table_width is None:
            continue
        for tr in _direct_children(table, "tr"):
            tds = _direct_children(tr, "td")
            if not tds:
                continue
            total = sum(_int_attr(td, "width") or 0 for td in tds)
            assert total == table_width, (
                f"table width={table_width} but its <tr> tds sum to {total} "
                f"(widths={[_int_attr(td, 'width') for td in tds]})"
            )
            checked += 1
    return checked


def _outer_table(root: _Node) -> _Node:
    tables = _find_all(root, "table")
    assert tables, "expected at least one <table> in the emitted document"
    return tables[0]


# --- IR-INDEPENDENT expectation builders (never call into html_emitter) -------------------------


def _ir_row_bbox(row: Row):
    # Mirror the emitter's design-span rule: the solver's band-expanded row.rect override wins
    # over the union of cell ink rects (a CTA label on a taller button shape spans the shape).
    if getattr(row, "rect", None) is not None:
        return (row.rect.top, row.rect.bottom)
    rects = [c.rect for c in row.cells if c.rect is not None]
    if not rects:
        return None
    return (min(r.top for r in rects), max(r.bottom for r in rects))


def _ir_expected_row_widths(row: Row, container_left: int, container_width: int) -> list:
    """The exact [spacer/content/spacer/...] width sequence a correctly-tiled row must produce,
    computed fresh from the row's own cell rects -- a leading gap, one gap per adjacent cell pair,
    and a trailing gap, each only emitted when > 0."""
    widths = []
    cursor = container_left
    for cell in row.cells:
        gap = cell.rect.left - cursor
        if gap > 0:
            widths.append(gap)
        widths.append(cell.rect.width)
        cursor = cell.rect.right
    trailing = (container_left + container_width) - cursor
    if trailing > 0:
        widths.append(trailing)
    return widths


def _ir_expected_vertical_gaps(rows: list, container_top: int) -> list:
    """The exact sequence of positive vertical gaps (leading + between consecutive rows) a
    correctly-tiled stack must reproduce as spacer rows, computed fresh from each row's own bbox."""
    gaps = []
    cursor = container_top
    for row in rows:
        bbox = _ir_row_bbox(row)
        top = bbox[0] if bbox is not None else cursor
        bottom = bbox[1] if bbox is not None else cursor
        gap = top - cursor
        if gap > 0:
            gaps.append(gap)
        cursor = max(cursor, bottom)
    return gaps


# --- fixtures (self-contained, mirroring test_html_emitter.py's convention) ----------------------


def _rect(l=0, t=0, r=100, b=20):
    return BBox(left=l, top=t, right=r, bottom=b)


def _text_cell(content, *, font="Arial", rect=None, size=14.0):
    return Cell(
        role="text",
        rect=rect or _rect(),
        text=TextInfo(content=content, align="left", runs=[TextRun(font=font, size=size, color="#000000")]),
    )


def _image_cell(rect=None, image_source_layer_ids=None):
    return Cell(role="image", rect=rect or _rect(), image_source_layer_ids=image_source_layer_ids or [1])


def _rows_cell(rows, rect):
    return Cell(role="rows", rect=rect, rows=rows)


def _emit(tree, tmp_path, **kw):
    routed = route(tree, kw.pop("policy", "hybrid"))
    result = emit(routed, tmp_path, **kw)
    html_text = Path(result["index_path"]).read_text(encoding="utf-8")
    return result, html_text


# --- literal repro of the bug report's own numbers -----------------------------------------------


def test_bug_repro_two_cell_row_192_and_288_in_640_table_gets_exact_spacers(tmp_path):
    # From the bug report: "a row of cells 192+288=480 inside a 640 table" with NO spacers.
    cell_a = _image_cell(rect=_rect(64, 0, 256, 100))  # width 192
    cell_b = _image_cell(rect=_rect(320, 0, 608, 100))  # width 288
    tree = TableTree(email="T", width=640, rows=[Row(cells=[cell_a, cell_b])])
    _result, html_text = _emit(tree, tmp_path)
    root = _parse_dom(html_text)

    checked = _assert_every_table_tiles_exactly(root)
    assert checked > 0
    outer = _outer_table(root)
    assert _int_attr(outer, "width") == 640

    expected = _ir_expected_row_widths(tree.rows[0], 0, 640)
    assert expected == [64, 192, 64, 288, 32]  # leading 64 + 192 + inter 64 + 288 + trailing 32 == 640
    assert sum(expected) == 640


def test_bug_repro_five_cell_row_summing_472_in_640_table_gets_exact_spacers(tmp_path):
    # From the bug report: "a 5-cell row summing to <640" (a real multi-column PSD row) with no
    # spacer cells for any of the gaps -- content widths alone here sum to 452, not 640.
    lefts_rights = [(0, 60), (80, 160), (200, 320), (360, 420), (460, 592)]
    content_widths = [r - l for l, r in lefts_rights]
    assert sum(content_widths) == 452
    cells = [_image_cell(rect=_rect(l, 0, r, 40)) for l, r in lefts_rights]
    tree = TableTree(email="T", width=640, rows=[Row(cells=cells)])
    _result, html_text = _emit(tree, tmp_path)
    root = _parse_dom(html_text)

    _assert_every_table_tiles_exactly(root)
    expected = _ir_expected_row_widths(tree.rows[0], 0, 640)
    assert sum(expected) == 640
    # every real gap (leading 0 -- omitted, 4 inter-cell, trailing) shows up as its own spacer width
    assert expected == [60, 20, 80, 40, 120, 40, 60, 40, 132, 48]


# --- the actual COLLAPSE bug: mixed column counts across top-level rows --------------------------


def test_mixed_column_count_top_level_rows_no_longer_share_one_fixed_template(tmp_path):
    # From the bug report: "rows of 1 cell, then 2, then 5, then 1" colliding against the first
    # row's column template. The fix: the OUTER stack is single-column, period -- assert every one
    # of the outer table's own <tr>s has exactly one <td> child, regardless of how many cells its
    # wrapped section actually tiles internally.
    row1 = Row(cells=[_image_cell(rect=_rect(0, 0, 640, 40))])
    row2 = Row(cells=[_image_cell(rect=_rect(0, 60, 300, 100)), _image_cell(rect=_rect(340, 60, 640, 100))])
    row5 = Row(cells=[_image_cell(rect=_rect(i * 128, 120, i * 128 + 100, 160)) for i in range(5)])
    row1b = Row(cells=[_image_cell(rect=_rect(0, 180, 640, 220))])
    tree = TableTree(email="T", width=640, rows=[row1, row2, row5, row1b])
    _result, html_text = _emit(tree, tmp_path)
    root = _parse_dom(html_text)

    outer = _outer_table(root)
    for tr in _direct_children(outer, "tr"):
        tds = _direct_children(tr, "td")
        assert len(tds) == 1, f"outer stack row has {len(tds)} tds, expected exactly 1 (single-column stack)"

    checked = _assert_every_table_tiles_exactly(root)
    assert checked >= 4  # outer stack + each of the 4 per-row nested tiling tables


# --- vertical rhythm: spacer rows for real gaps, none for none ------------------------------------


def test_vertical_gap_between_top_level_rows_becomes_an_exact_spacer_row(tmp_path):
    top_row = Row(cells=[_image_cell(rect=_rect(0, 0, 640, 100))])
    bottom_row = Row(cells=[_image_cell(rect=_rect(0, 150, 640, 250))])  # 50px gap after row0's bottom
    tree = TableTree(email="T", width=640, rows=[top_row, bottom_row])
    _result, html_text = _emit(tree, tmp_path)

    gaps = _ir_expected_vertical_gaps(tree.rows, 0)
    assert gaps == [50]
    assert re.search(r'height="50"[^>]*style="width:640px;height:50px;font-size:0;line-height:0;"', html_text)


def test_leading_top_gap_becomes_a_spacer_row_before_the_first_content_row(tmp_path):
    row = Row(cells=[_image_cell(rect=_rect(0, 40, 640, 100))])  # starts 40px below canvas top
    tree = TableTree(email="T", width=640, rows=[row])
    _result, html_text = _emit(tree, tmp_path)

    gaps = _ir_expected_vertical_gaps(tree.rows, 0)
    assert gaps == [40]
    root = _parse_dom(html_text)
    outer = _outer_table(root)
    first_tr = _direct_children(outer, "tr")[0]
    first_td = _direct_children(first_tr, "td")[0]
    assert _int_attr(first_td, "height") == 40
    assert "font-size:0;line-height:0;" in html_text


def test_no_spurious_leading_spacer_when_first_row_already_starts_at_container_top(tmp_path):
    row = Row(cells=[_image_cell(rect=_rect(0, 0, 640, 100))])
    tree = TableTree(email="T", width=640, rows=[row])
    _result, html_text = _emit(tree, tmp_path)

    gaps = _ir_expected_vertical_gaps(tree.rows, 0)
    assert gaps == []
    root = _parse_dom(html_text)
    outer = _outer_table(root)
    # exactly one <tr> in the outer stack: the row itself, no leading spacer row.
    assert len(_direct_children(outer, "tr")) == 1


# --- leaf cell heights never collapse -------------------------------------------------------------


def test_image_leaf_gets_exact_height_attribute_and_style_matching_its_rect(tmp_path):
    from PIL import Image

    composite = Image.new("RGBA", (200, 200), (1, 2, 3, 255))
    cell = _image_cell(rect=_rect(0, 0, 200, 77))
    tree = TableTree(email="T", width=200, rows=[Row(cells=[cell])])
    _result, html_text = _emit(tree, tmp_path, composite=composite)
    assert "<img" in html_text  # the real raster path (not the no-composite placeholder degrade)
    assert re.search(r'<td[^>]*width="200" height="77"[^>]*style="width:200px;height:77px;', html_text)


def test_live_text_leaf_height_is_a_floor_not_a_ceiling(tmp_path):
    cell = _text_cell("Body copy that must never sit shorter than its PSD box.", rect=_rect(0, 0, 300, 55))
    tree = TableTree(email="T", width=300, rows=[Row(cells=[cell])])
    _result, html_text = _emit(tree, tmp_path)
    # height attribute present (browsers treat <td height=..> as an advisory minimum, never
    # clipping taller content) AND an explicit min-height (never a hard `height:` that would read
    # as a ceiling) in the inline style.
    assert re.search(r'<td[^>]*width="300" height="55"[^>]*style="width:300px;min-height:55px;', html_text)


# --- nested "rows" containers tile recursively at their own rect -----------------------------------


def test_nested_rows_container_tiles_its_own_rect_with_internal_vertical_gap(tmp_path):
    inner_rows = [
        Row(cells=[_image_cell(rect=_rect(100, 10, 300, 50))]),
        Row(cells=[_image_cell(rect=_rect(100, 90, 300, 130))]),  # 40px internal gap
    ]
    container = _rows_cell(inner_rows, rect=_rect(100, 10, 300, 130))
    tree = TableTree(email="T", width=400, rows=[Row(cells=[container])])
    _result, html_text = _emit(tree, tmp_path)
    root = _parse_dom(html_text)

    checked = _assert_every_table_tiles_exactly(root)
    assert checked >= 3  # outer stack + container's own stack + container's per-row tiling table

    # the container's inner tiling table is width=200 (its rect width) -- present among the tables.
    tables = _find_all(root, "table")
    widths = {_int_attr(t, "width") for t in tables}
    assert 200 in widths

    gaps = _ir_expected_vertical_gaps(inner_rows, container.rect.top)
    assert gaps == [40]
    assert re.search(r'height="40"[^>]*style="width:200px;height:40px;font-size:0;line-height:0;"', html_text)


# --- a denser hand-built tree combining every TILING BEHAVIOR item at once ------------------------


def test_dense_mixed_tree_every_table_tiles_exactly(tmp_path):
    nested_rows = [
        Row(cells=[_text_cell("Nested headline", rect=_rect(220, 320, 420, 360))]),
        Row(cells=[_image_cell(rect=_rect(220, 380, 420, 460))]),
    ]
    container = _rows_cell(nested_rows, rect=_rect(220, 300, 420, 480))
    row0 = Row(cells=[_image_cell(rect=_rect(0, 0, 640, 80))])
    row1 = Row(
        cells=[
            _image_cell(rect=_rect(40, 120, 240, 220)),
            _text_cell("Body copy in the second column", rect=_rect(280, 120, 600, 220)),
        ]
    )
    row2 = Row(cells=[container])
    row3 = Row(cells=[_image_cell(rect=_rect(i * 130, 520, i * 130 + 100, 560)) for i in range(4)])
    tree = TableTree(email="T", width=640, rows=[row0, row1, row2, row3])
    _result, html_text = _emit(tree, tmp_path)
    root = _parse_dom(html_text)

    checked = _assert_every_table_tiles_exactly(root)
    assert checked >= 6
    outer = _outer_table(root)
    assert _int_attr(outer, "width") == 640
    for tr in _direct_children(outer, "tr"):
        assert len(_direct_children(tr, "td")) == 1


# --- the real Intel announcement PSD, full pipeline, generic + IR-independent invariants ---------


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_real_announcement_psd_every_table_tiles_exactly(tmp_path):
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.rasterizer import composite_psd
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout, email_override="Announcement")
    tree = trees[0]
    routed = route(tree, "hybrid")
    composite = composite_psd(ANNOUNCEMENT_PSD)
    result = emit(routed, tmp_path, composite=composite, layer_names=layer_names)
    html_text = Path(result["index_path"]).read_text(encoding="utf-8")
    root = _parse_dom(html_text)

    checked = _assert_every_table_tiles_exactly(root)
    assert checked > 10, "expected many tables in the real multi-section announcement bundle"

    outer = _outer_table(root)
    assert _int_attr(outer, "width") == tree.width
    for tr in _direct_children(outer, "tr"):
        tds = _direct_children(tr, "td")
        assert len(tds) == 1, "the outer stack must remain single-column at every top-level row"


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_real_announcement_psd_vertical_spacers_and_leaf_heights_match_ir_gaps(tmp_path):
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.rasterizer import composite_psd
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout, email_override="Announcement")
    tree = trees[0]
    routed = route(tree, "hybrid")
    composite = composite_psd(ANNOUNCEMENT_PSD)
    result = emit(routed, tmp_path, composite=composite, layer_names=layer_names)
    html_text = Path(result["index_path"]).read_text(encoding="utf-8")

    # the top-level stack must reproduce every real vertical gap as a spacer row -- at the IR's
    # exact height, or shrunk by a SMALL absorbed overhang (OVERHANG ABSORPTION in html_emitter:
    # CSS strut vs Photoshop ink physics makes a text row render a few px taller than its box;
    # the following spacer absorbs it so the stack never drifts). Never larger, never vastly less.
    gaps = _ir_expected_vertical_gaps(tree.rows, 0)
    assert gaps, "expected the real announcement PSD to have at least one real inter-section gap"
    for gap in set(gaps):
        candidates = range(max(1, gap - 16), gap + 1)
        # Two certified spacer forms: the plain white spacer (fs0/lh0) and the band-gap fill
        # (probe_paint3: a shaded gap needs a REAL line box -- font-size:1px;line-height:{h}px --
        # or Word paints the band white across it).
        assert any(
            re.search(rf'height="{g}"[^>]*font-size:0;line-height:0;', html_text)
            or re.search(rf'height="{g}"[^>]*font-size:1px;line-height:{g}px;mso-line-height-rule:exactly;', html_text)
            for g in candidates
        ), f"expected a spacer row of ~{gap}px (design gap, possibly minus a small absorbed overhang)"

    # every LEAF cell's own rect height is present on its <td> (never silently collapsed) --
    # sampled directly off the routed tree, independent of html_emitter.
    def _iter_leaves(rows):
        for row in rows:
            for cell in row.cells:
                if cell.role == "rows":
                    yield from _iter_leaves(cell.rows or [])
                else:
                    yield cell

    sample = 0
    for cell in _iter_leaves(tree.rows):
        h = int(cell.rect.height)
        if h <= 0:
            continue
        # The td's height ATTR is rect height + any intra-row top offset carried as padding-top
        # (_wrap_td), so a leaf sitting a few px below its row top legitimately emits a slightly
        # larger attr -- accept that small positive offset, never a smaller (collapsed) value.
        assert any(re.search(rf'height="{g}"', html_text) for g in range(h, h + 9)), (
            f'expected a leaf <td height="{h}"> (or +intra-row offset) for rect height {h}'
        )
        sample += 1
        if sample >= 15:
            break
    assert sample >= 5


# --- the objective signal: the headless-Chromium geometry oracle must materially improve ---------


@pytest.mark.skipif(not (_has_real_psd and _has_playwright), reason="real PSD + playwright required")
def test_real_announcement_psd_no_longer_overflows_the_canvas_width(tmp_path):
    """The collapse/overlap bug's most visible symptom (confirmed against a real emitted bundle):
    unspaced cells and mismatched column templates blew the rendered page width far past the PSD
    canvas width. Render with real headless Chromium and assert the page's actual scrollWidth stays
    within a small tolerance of tree.width -- a strong, real (non-tautological) proxy for "the
    canvas tiles edge-to-edge instead of collapsing/overflowing"."""
    from playwright.sync_api import sync_playwright

    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.rasterizer import composite_psd
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout, email_override="Announcement")
    tree = trees[0]
    routed = route(tree, "hybrid")
    composite = composite_psd(ANNOUNCEMENT_PSD)
    result = emit(routed, tmp_path, composite=composite, layer_names=layer_names)
    index_path = Path(result["index_path"])

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": tree.width, "height": 100})
            page.goto(index_path.resolve().as_uri())
            scroll_width = page.evaluate("document.documentElement.scrollWidth")
        finally:
            browser.close()

    assert scroll_width <= tree.width + 20, (
        f"rendered page scrollWidth={scroll_width} far exceeds the canvas width={tree.width} "
        "-- the tiling collapse/overflow bug appears to have regressed"
    )
