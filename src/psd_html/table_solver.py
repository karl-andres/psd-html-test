"""Classified leaf items -> a nested TableTree (rows -> cells).

Reuses grid_analyzer._find_valid_cut (the verified guillotine cut test) directly and never forks
it. This module's only new logic is (a) building a TREE instead of a flat cell list -- alternating
row/column splits, recursing -- and (b) mapping the resulting tree, plus the peeled background/
highlight items, onto the TableTree IR (table_tree.py).

Cut-tree shape: a node is one of
    ("leaf", rect)            a single content rect (rect carries "idx" back into item_by_idx)
    ("row", [node, node...])  a horizontal (top/bottom) guillotine split -- stacked rows
    ("col", [node, node...])  a vertical (left/right) guillotine split -- side-by-side columns
    ("flatten", [rect, ...])  no valid cut exists -- a MUST-FLATTEN cluster (residual overlap)

`_find_valid_cut` doesn't itself report which axis it used, but the axis is fully determined by
the geometry of its own output: a valid top/bottom cut always leaves the "above" group strictly
above the "below" group (no y-overlap), and a valid left/right cut always leaves "above" strictly
left of "below" (no x-overlap). Row cuts are tried first inside `_find_valid_cut`, so checking the
row relationship first mirrors that same precedence -- this is reading the verified cut's result,
not re-deriving it.

The IR is NESTABLE (table_tree.Cell can be a role="rows" CONTAINER carrying its own nested
`rows:[Row]`), which matches how these PSDs are actually authored per the SOP: full-width stacked
sections and simple side-by-side columns are the common case (flat rows/cells), but a column often
itself contains a further vertical stack (e.g. a stat number over its caption) that needs a real
sub-table, not a rasterized image. `_rows_from_node` / `_cell_from_node` map the cut tree onto the
IR recursively -- a "row" node reached while building ONE cell (i.e. a column that needs a further
split) becomes a role="rows" CONTAINER cell wrapping its own nested rows, instead of being
flattened. Only a genuine "flatten" node (a true no-valid-cut residual overlap -- mutually
overlapping rects, not just deeper same-cuttable structure) ever collapses to one role="graphic"
cell -- see `_cell_from_flatten`.
"""

from __future__ import annotations

import re
import warnings
from typing import Optional

from . import grid_analyzer as G
from .layer_classifier import (
    ROLE_BACKGROUND,
    ROLE_BUTTON,
    ROLE_CONTENT,
    ROLE_GRAPHIC,
    ROLE_HIGHLIGHT,
    classify_artboard,
)
from .layout_tree import BBox
from .table_tree import Background, Cell, Row, TableTree

_CONTENT_ROLES = (ROLE_CONTENT, ROLE_GRAPHIC, ROLE_BUTTON)

class SafetyInvariantViolation(Exception):
    """FAIL LOUD: raised when solving a PSD would emit ANY live text -- editable or not -- baked
    into a rasterized role=graphic or role=button cell -- the one hard failure the whole IR exists
    to prevent (docs/PSD-for-HTML_Authoring-SOP.md #8; crew rule: never rasterize body copy). A
    graphic cell must be genuinely non-text decorative content only; a button cell may keep
    exactly one live text label. This can come from either a named `... graphic`/`... button`
    group that itself aggregated an extra text member (layer_classifier's aggregation), or a
    residual guillotine overlap with no valid cut that this module collapsed via
    `_cell_from_flatten`.

    S1.2 Defect C: this used to fire only on `editable` (bracketed/highlight-marked) text --
    plain, non-editable body copy baked into a graphic or a plain second text baked into a button
    passed silently. It now fires on ANY baked text, editable or not (`find_baked_text_cells`).

    Never caught internally by this module: `build_table_trees` raises it (so the CLI `build`
    command exits non-zero rather than writing a broken TableTree to disk); the intake validator
    (`intake_validator.py`) bypasses the raise entirely by calling the pure, non-raising
    find_baked_text_cells per-artboard, so a non-conforming PSD comes back with every offending
    layer at once rather than stopping at the first one.
    """

    def __init__(self, violations: list):
        self.violations = list(violations)
        detail = "; ".join(f"{v['email']!r} layers={v['layer_ids']} rect={v['rect']}" for v in self.violations)
        super().__init__(f"safety invariant violated -- live text baked into a rasterized cell: {detail}")


def _iter_cells(rows: list):
    """Walk every Cell in a TableTree's rows, recursively descending into role="rows" CONTAINER
    cells' own nested `rows` (Defect 1 made Cell nestable -- a rasterized graphic/button cell can
    now appear at ANY depth, not just the top level, so any safety scan over the tree must recurse
    to see it)."""
    for row in rows:
        for cell in row.cells:
            yield cell
            if cell.role == "rows" and cell.rows:
                yield from _iter_cells(cell.rows)


def find_baked_text_cells(tree: TableTree) -> list:
    """Scan one solved TableTree (at EVERY nesting depth, recursing through role="rows" CONTAINER
    cells) for the two rasterized-cell shapes that bake live text into an image with no live copy
    surviving (docs/PSD-for-HTML_Authoring-SOP.md #8; S1.2 Defect C):

      - role="graphic" cell with a non-empty `baked_text_layer_ids` -- a residual-overlap flatten
        (or a named `... graphic` group) whose source layers include ANY text/type layer, EDITABLE
        OR NOT. A graphic cell must be genuinely non-text decorative content only.
      - role="button" cell with a non-empty `baked_text_layer_ids` -- a named `... button` group
        (or a flatten that absorbed one) that kept only ONE text member live as its label and
        baked every OTHER text member -- editable or not, bracketed or not, highlight-marked or
        plain -- into the button image.

    S1.2 Defect C fix: the pre-fix version (`find_trapped_editable_cells`) only looked at
    `cell.editable` / `cell.swallowed_editable_layer_ids`, both of which are True/non-empty only
    for BRACKETED or HIGHLIGHT-MARKED text -- so plain, non-editable body copy baked into a
    graphic (e.g. a TYPE layer literally named "... graphic" per the SOP naming convention, or
    dragged into a `... graphic` group) or a second plain text baked into a button passed
    silently. `baked_text_layer_ids` (populated by layer_classifier/`_cell_from_flatten`) counts
    ANY text member, closing that gap.

    Pure/non-raising -- callers decide whether that means fail-loud (`enforce_safety_invariant`)
    or one entry in a larger report (`intake_validator`)."""
    found = []
    for cell in _iter_cells(tree.rows):
        if cell.role in ("graphic", "button") and cell.baked_text_layer_ids:
            found.append(
                {
                    "email": tree.email,
                    "layer_ids": list(cell.baked_text_layer_ids),
                    "rect": cell.rect.to_dict(),
                }
            )
    return found


# Backward-compat alias: S1.2 Defect C renamed/extended this check (see `find_baked_text_cells`'s
# docstring for exactly what widened). Kept only so any external caller written against the old
# name still resolves; do not add new call sites against this alias.
find_trapped_editable_cells = find_baked_text_cells


def enforce_safety_invariant(trees: list) -> None:
    """FAIL LOUD over a whole `build` batch (all artboards from one PSD): raise
    SafetyInvariantViolation naming every offending layer if ANY artboard's tree would emit live
    text baked into a graphic or button cell. Withholds the WHOLE batch rather than only the bad
    artboard, so a caller of `build_table_trees` can never partially trust an unvalidated result
    without explicitly going through `intake_validator` first."""
    violations = []
    for t in trees:
        violations.extend(find_baked_text_cells(t))
    if violations:
        raise SafetyInvariantViolation(violations)


_LINK_HINTS = (
    "link",
    "unsubscribe",
    "privacy",
    "terms",
    "view online",
    "view in browser",
    "footnote",
    "toolkit",
    "learn more",
    "read more",
)


def _slugify(name: str) -> Optional[str]:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s or None


# A link LABEL is short ("Review the toolkit", "Unsubscribe"); body copy that merely MENTIONS a
# hint word ("...sales and marketing toolkit...") is prose, not a link. Without this cap the
# announcement's paragraphs mentioning "toolkit" were classified CTA and rendered as giant blue
# buttons (the invented-blue-background defect the fidelity gate localized to region 8_0).
_LINK_LABEL_MAX_CHARS = 40


def _looks_like_link(name: str, content: str) -> bool:
    text = (content or "").strip()
    if text:
        # a text cell: the CONTENT is the would-be label -- must be label-short AND carry a hint.
        # (The layer NAME can't be trusted alone here: Photoshop auto-names text layers with their
        # own content, so a paragraph mentioning "toolkit" hits via the name too.)
        if len(text) > _LINK_LABEL_MAX_CHARS:
            return False
        hay = f"{name or ''} {text}".lower()
        return any(hint in hay for hint in _LINK_HINTS)
    # no content (an image/icon cell): the curated layer name is all there is
    return any(hint in (name or "").lower() for hint in _LINK_HINTS)


# --- cut-tree construction ------------------------------------------------------------------


def _is_row_split(above: list, below: list) -> bool:
    return max(r["bbox"]["bottom"] for r in above) <= min(r["bbox"]["top"] for r in below)


def _is_col_split(above: list, below: list) -> bool:
    return max(r["bbox"]["right"] for r in above) <= min(r["bbox"]["left"] for r in below)


# Defect B fix: `_find_valid_cut` (grid_analyzer, verified/untouched) is exact -- it treats ANY
# pixel of intersection as a genuine overlap with no valid guillotine cut. Real PSD authoring
# regularly leaves two rects that were clearly INTENDED to sit cleanly side-by-side (or stacked)
# overlapping by a hairline of authoring noise in one axis -- e.g. a bracketed field's bbox
# right=1080 and the next image's bbox left=1079 (1px x-overlap, full y-overlap). Left as-is that
# collapses the pair into a "flatten" cluster (table_solver._cell_from_flatten -> role="graphic"),
# which can trap an editable field and raise SafetyInvariantViolation for what is really authoring
# slop, not a genuine overlap.
#
# `_HAIRLINE_EPSILON` reuses grid_analyzer.EPSILON (2px) -- the same tolerance grid_analyzer's own
# docstring already names as "px tolerance for overlap detection" for its (separate, reporting-
# only) `overlaps()` check, just applied here as a real geometry snap instead of a fuzzy boolean.
_HAIRLINE_EPSILON = G.EPSILON


def _snap_hairline_overlaps(rects: list, epsilon: float = _HAIRLINE_EPSILON) -> list:
    """Pre-pass, run ONCE over one artboard's content rects before `_build_cut_tree` ever sees
    them: for every pair of rects whose 2D intersection is <= `epsilon` px wide in x OR <= epsilon
    px tall in y, snap the shared boundary (the two edges that form that hairline overlap) to their
    midpoint -- turning a hairline overlap into a clean touch or a small real gap, in ONE axis only
    (whichever axis's overlap is the smaller of the two, i.e. the hairline one). Note: when one
    rect's extent in the hairline axis is fully CONTAINED in the other's, both of its edges snap to
    the shared midpoint and it collapses to zero in that axis (a degenerate touch, not a gap).

    Deliberately does NOT touch pairs whose overlap exceeds `epsilon` in BOTH axes -- those are
    genuine overlaps (e.g. two mutually-overlapping decorative social icons) and must still reach
    `_find_valid_cut` unmodified so a true no-valid-cut cluster still flattens to a role="graphic"
    cell exactly as before. Also leaves alone any pair with no 2D intersection at all (nothing to
    snap).

    Returns a NEW list of rect-record dicts (same `idx`/`name`/`is_text`/`z` keys, only `bbox` is
    replaced by a snapped copy) -- never mutates the input records or the underlying ClassifiedItem
    bboxes, so this is purely a cut-tree-construction concern, upstream of the untouched, exact
    `_find_valid_cut`.
    """
    boxes = [dict(r["bbox"]) for r in rects]  # mutable per-rect copies, index-aligned with `rects`
    n = len(boxes)
    for i in range(n):
        a = boxes[i]
        for j in range(i + 1, n):
            b = boxes[j]
            ox = min(a["right"], b["right"]) - max(a["left"], b["left"])
            oy = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
            if ox <= 0 or oy <= 0:
                continue  # no 2D intersection at all -- already clean, nothing to snap
            if ox > epsilon and oy > epsilon:
                continue  # overlap exceeds the hairline tolerance in BOTH axes -- genuine overlap
            if ox <= oy:
                # Hairline in x: snap whichever rect's left/right edge forms the overlap's inner
                # boundary. Using independent `if`s (not if/else) so a tie (both edges equal) snaps
                # both sides instead of silently leaving one stale.
                lo = max(a["left"], b["left"])
                hi = min(a["right"], b["right"])
                mid = (lo + hi) / 2.0
                if a["right"] == hi:
                    a["right"] = mid
                if b["right"] == hi:
                    b["right"] = mid
                if a["left"] == lo:
                    a["left"] = mid
                if b["left"] == lo:
                    b["left"] = mid
            else:
                # Hairline in y: same snap, top/bottom.
                lo = max(a["top"], b["top"])
                hi = min(a["bottom"], b["bottom"])
                mid = (lo + hi) / 2.0
                if a["bottom"] == hi:
                    a["bottom"] = mid
                if b["bottom"] == hi:
                    b["bottom"] = mid
                if a["top"] == lo:
                    a["top"] = mid
                if b["top"] == lo:
                    b["top"] = mid
    return [dict(r, bbox=boxes[k]) for k, r in enumerate(rects)]


def _build_cut_tree(rects: list):
    if len(rects) == 1:
        return ("leaf", rects[0])
    cut = G._find_valid_cut(rects)
    if cut is None:
        return ("flatten", list(rects))
    above, below = cut
    axis = "row" if _is_row_split(above, below) else ("col" if _is_col_split(above, below) else "row")
    return (axis, [_build_cut_tree(above), _build_cut_tree(below)])


def _flatten_by_axis(node, axis: str) -> list:
    """Collapse consecutive same-axis nodes into one flat list of children (each of a DIFFERENT
    axis, or a leaf/flatten)."""
    if node[0] == axis:
        out: list = []
        for child in node[1]:
            out.extend(_flatten_by_axis(child, axis))
        return out
    return [node]


def _gather_rects(node) -> list:
    """Recursively collect every leaf rect dict under a cut-tree node."""
    kind = node[0]
    if kind == "leaf":
        return [node[1]]
    if kind == "flatten":
        return list(node[1])
    out: list = []
    for child in node[1]:
        out.extend(_gather_rects(child))
    return out


# --- cell / row construction ------------------------------------------------------------------


def _split_text_highlights(item, highlight_by_id: dict):
    """Split one text item's covering highlights into BOX fills (the chip CONTAINS the text --
    its geometry IS what the design shows, so the caller takes the SHAPE rect and keeps the
    text's own bounds separately as text_rect) and SUBSTRING chips (the chip backs only part of
    the text -- a merge-token highlight inside a paragraph; carried as sub_highlights rects for
    the emitter's shaded-<span> mapping). Returns (bg, box_rect, sub_highlights); the caller
    reassigns rect/text_rect from box_rect and builds the Cell."""
    bg = None
    sub_highlights: list = []
    box_rect = None
    _slop = 8
    hl_ids = list(getattr(item, "covered_by_highlight_ids", None) or [])
    if not hl_ids and item.covered_by_highlight_id is not None:
        hl_ids = [item.covered_by_highlight_id]
    _content_str = ((item.text.content or "") if item.text is not None else "").strip()
    _is_bracket_token = (
        _content_str.startswith("[") and _content_str.endswith("]")
        and chr(10) not in _content_str and "[" not in _content_str[1:-1]
    )
    for hid in hl_ids:
        hl = highlight_by_id.get(hid)
        if hl is None:
            continue
        hb = hl.bbox
        contains = (
            hb["left"] <= item.bbox["left"] + _slop and hb["top"] <= item.bbox["top"] + _slop
            and hb["right"] >= item.bbox["right"] - _slop and hb["bottom"] >= item.bbox["bottom"] - _slop
        )
        if contains and _is_bracket_token:
            # A whole-token bracketed field ([Company Sign-Off], [Device/Offer], [Sales
            # Representative]): the design highlights the WORDS, brackets outside -- the
            # same rule as the footer tokens, which render as shaded SPANS and never
            # regressed (owner ruling, 3 rounds). Chip form can't do bracket-exclusion;
            # route the highlight as a substring rect and keep the text cell's own rect.
            sub_highlights.append(dict(hb))
            continue
        if contains:
            # The SHAPE is the design truth: the box takes the highlight rect EXACTLY.
            # Unioning in the text bbox inflated chips by the type layer's loose bearings
            # and trailing spaces (sign-off chip rendered 173px vs the design's 163,
            # owner-caught); the +-slop containment plus type-layer bearing slack makes ink
            # overflow unlikely (not guaranteed: _slop tolerates the chip being up to 8px
            # smaller than the text bbox per side).
            box_rect = dict(hb) if box_rect is None else G._union_bbox([box_rect, hb])
            if bg is None:
                bg = Background(color=None, image_source_layer_id=hl.layer_ids[0])
        else:
            sub_highlights.append(dict(hb))
    return bg, box_rect, sub_highlights


def _cell_from_item(item, highlight_by_id: dict) -> Cell:
    rect = BBox.from_dict(item.bbox)

    if item.role == ROLE_GRAPHIC:
        baked_text = list(getattr(item, "text_member_layer_ids", None) or [])
        return Cell(
            role="graphic",
            rect=rect,
            editable=item.editable,
            image_source_layer_ids=list(item.layer_ids),
            baked_text_layer_ids=baked_text or None,
        )

    if item.role == ROLE_BUTTON:
        label = item.text
        swallowed = list(getattr(item, "swallowed_editable_layer_ids", None) or [])
        baked_text = list(getattr(item, "text_member_layer_ids", None) or [])
        return Cell(
            role="button",
            rect=rect,
            editable=item.editable,
            link_slot=_slugify(item.name),
            text=label,
            image_source_layer_ids=list(item.layer_ids),
            swallowed_editable_layer_ids=swallowed or None,
            baked_text_layer_ids=baked_text or None,
        )

    # role == ROLE_CONTENT
    if item.is_text:
        # Split this text's covering highlights into BOX fills and SUBSTRING chips -- see
        # _split_text_highlights. Sizing the td to the TEXT bounds instead of the shape rendered
        # every placeholder box at the wrong shape/size (owner-caught).
        text_rect = None
        bg, box_rect, sub_highlights = _split_text_highlights(item, highlight_by_id)
        if box_rect is not None:
            text_rect = rect
            rect = BBox.from_dict(box_rect)
        content = item.text.content if item.text is not None else ""
        link_slot = _slugify(item.name) if _looks_like_link(item.name, content) else None
        source_layer_id = item.layer_ids[0] if item.layer_ids else None
        return Cell(
            role="text",
            rect=rect,
            background=bg,
            editable=item.editable,
            link_slot=link_slot,
            text=item.text,
            source_layer_id=source_layer_id,
            text_rect=text_rect,
            sub_highlights=sub_highlights or None,
        )

    # standalone content image/icon
    return Cell(role="image", rect=rect, editable=False, image_source_layer_ids=list(item.layer_ids))


def _cell_from_flatten(rects: list, item_by_idx: dict) -> Cell:
    items = [item_by_idx[r["idx"]] for r in rects]
    bbox = G._union_bbox([r["bbox"] for r in rects])
    layer_ids = [lid for it in items for lid in it.layer_ids]
    # Faithfully surface if the collapsed cluster contained an editable member -- reporting
    # metadata only. The actual safety gate is baked_text_layer_ids (computed just below), not
    # this `editable` flag; rejection happens in build_table_trees (which raises
    # SafetyInvariantViolation) or, per-artboard, in the intake validator.
    any_editable = any(it.editable for it in items)
    # S1.2 Defect C: ANY text layer among the collapsed cluster's items is baked-in copy with no
    # live copy surviving -- not just editable ones. Each item's own `text_member_layer_ids`
    # already carries this (a plain text leaf reports itself; a nested graphic/button group -- the
    # rare case of one already-aggregated unit colliding with something else -- reports its own
    # text members), so summing it here needs no new logic.
    baked_text = [lid for it in items for lid in it.text_member_layer_ids]
    return Cell(
        role="graphic",
        rect=BBox.from_dict(bbox),
        editable=any_editable,
        image_source_layer_ids=layer_ids,
        baked_text_layer_ids=baked_text or None,
    )


def _rows_from_node(node, item_by_idx: dict, highlight_by_id: dict) -> list:
    """Map any cut-tree node representing a (possibly single) vertical stack into `list[Row]`.

    Reused for BOTH the top-level artboard tree and for nested CONTAINER cells alike -- this
    recursion (row-flatten -> per-row col-flatten -> one Cell per resulting node) is exactly what
    lets a column that itself needs a further split become a real nested sub-table instead of
    being rasterized to one image; see `_cell_from_node`.
    """
    row_nodes = _flatten_by_axis(node, "row")
    rows: list = []
    for row_node in row_nodes:
        cell_nodes = _flatten_by_axis(row_node, "col")
        cells = [_cell_from_node(cn, item_by_idx, highlight_by_id) for cn in cell_nodes]
        rows.append(Row(cells=cells))
    return rows


def _cell_from_node(node, item_by_idx: dict, highlight_by_id: dict) -> Cell:
    """One cell-tree position -> one Cell.

    `node` is always one of the 3 shapes a col-axis flatten hands back (see `_rows_from_node`): a
    single leaf item, a genuine no-valid-cut flatten cluster, or a nested "row" node (this column
    position itself needs a further vertical split -- e.g. a stat number stacked over its caption).
    Only the "row" case is nestable-structure-not-yet-a-leaf: it recurses into a CONTAINER cell
    (role="rows") instead of collapsing to one image, which is exactly Defect 1's fix -- a
    "flatten" node here means the geometry genuinely has no valid guillotine cut (mutually
    overlapping rects), not just deeper same-cuttable structure.
    """
    kind = node[0]
    if kind == "leaf":
        return _cell_from_item(item_by_idx[node[1]["idx"]], highlight_by_id)
    if kind == "flatten":
        return _cell_from_flatten(node[1], item_by_idx)
    # kind == "row": a nested vertical stack inside what would otherwise be a single column --
    # recurse into a real sub-table rather than flattening it away.
    nested_rows = _rows_from_node(node, item_by_idx, highlight_by_id)
    rect = BBox.from_dict(G._union_bbox([r["bbox"] for r in _gather_rects(node)]))
    return Cell(role="rows", rect=rect, rows=nested_rows)


def _row_bbox(row: Row) -> dict:
    """The row's DESIGN span: the explicit band-expanded `row.rect` when set (see solve_artboard),
    else the union bbox of the row's cells -- every Cell (leaf or CONTAINER) carries its own
    `.rect`, so this works uniformly without re-walking the raw cut-tree geometry."""
    if row.rect is not None:
        return row.rect.to_dict()
    return G._union_bbox([c.rect.to_dict() for c in row.cells])


# Band-to-row assignment tolerance: a band still "contains" a row if the row pokes out by up to
# this many px (authoring slop), matching the hairline philosophy used by _snap_hairline_overlaps.
_CONTAIN_TOLERANCE = 6


def _band_contains_row(band_bbox: dict, row_bbox: dict, tol: int = _CONTAIN_TOLERANCE) -> bool:
    return (
        band_bbox["left"] - tol <= row_bbox["left"]
        and band_bbox["top"] - tol <= row_bbox["top"]
        and band_bbox["right"] + tol >= row_bbox["right"]
        and band_bbox["bottom"] + tol >= row_bbox["bottom"]
    )


def _intersects(a: dict, r: dict) -> bool:
    return not (a["right"] <= r["left"] or a["left"] >= r["right"]
                or a["bottom"] <= r["top"] or a["top"] >= r["bottom"])


def _match_row_background(row_bbox: dict, bg_items: list) -> Optional[Background]:
    """CONTAINMENT assignment (replaces the max-vertical-overlap heuristic, which smeared one
    section's band across every row it merely touched -- the "invented blue background" defect):
    a band is this row's background only if the row sits INSIDE the band. Among containing bands
    the winner is the one PAINTED LAST (highest z) -- what is actually visible behind the row.
    Area is only the tie-break: a page fill under a card under a chip loses to the chip because
    the chip painted later, exactly as Photoshop composited it. Picking by smallest area instead
    is wrong whenever a big white card is painted OVER a colorful panel: the panel is smaller or
    z-lower but invisible behind the card."""
    containing = [b for b in bg_items if _band_contains_row(b.bbox, row_bbox)]
    if not containing:
        return None
    best = max(containing, key=lambda b: (b.z, -G._area(b.bbox)))
    return Background(color=None, image_source_layer_id=best.layer_ids[0])


# --- top-level solve --------------------------------------------------------------------------


def _expand_row_to_band_span(row: Row, row_bbox: dict, bg_items: list, matched_bg_ids: set,
                              content_top_by_cell: dict) -> None:
    """DESIGN-SPAN expansion: the row's visual span is its band, not its content ink (a 15px CTA
    label on a 46px button shape occupies 46px of the rhythm). This is a DIFFERENT question from
    the backdrop contest in solve_artboard (won by paint order -- highest z, what color is
    visible), but z is approximate, so a giant enclosing panel can outrank the actual button shape
    and starve the expansion (live-caught 2026-07-09: the CTA row kept its 15px label ink and
    pushed every row below +29px). The row-scale question is answered by TIGHTNESS instead: the
    smallest containing band that is row-scale (not a section panel) is the shape this row
    physically occupies. Mutates row.rect (and, for a lone linked/button cell, its cell's rect),
    matched_bg_ids, and content_top_by_cell in place; no-op when no row-scale band is found."""
    row_h = max(1, row_bbox["bottom"] - row_bbox["top"])
    row_scale = [
        b for b in bg_items
        if _band_contains_row(b.bbox, row_bbox)
        and (b.bbox["bottom"] - b.bbox["top"]) > row_h
        and (b.bbox["bottom"] - b.bbox["top"]) <= 3 * row_h + 60
    ]
    if not row_scale:
        return
    tight = min(row_scale, key=lambda b: G._area(b.bbox))
    matched_bg_ids.add(tight.layer_ids[0])
    row.rect = BBox.from_dict(G._union_bbox([row_bbox, tight.bbox]))
    # A lone linked/button cell on a taller shape owns the WHOLE shape: give the cell the band's
    # FULL rect (both axes -- the emitter sizes the bulletproof button from its cell rect) -- the
    # shape IS the button.
    if len(row.cells) == 1 and row.cells[0].role in ("text", "button") and row.cells[0].link_slot:
        c = row.cells[0]
        # keep the cell's true content top before the band overwrites it (read by the row-ordering
        # sort + overlap clamp below, which must not see the expanded rect).
        content_top_by_cell[id(c)] = int(c.rect.top)
        c.rect = BBox.from_dict(G._union_bbox([c.rect.to_dict(), tight.bbox]))


def _attach_unmatched_band(b, rows: list) -> None:
    """One unmatched background band, split by what it encloses:
      - contains >= 1 content row -> an ENCLOSING panel that lost every per-row z-contest (e.g.
        the page fill under every card). Its visible contribution is the backdrop of the rows no
        more-specific band claimed -- apply it as the FALLBACK background of its contained, still-
        background-less rows. NEVER an image row: baking an enclosing panel bakes the whole body
        (flatten crop) and the email renders twice.
      - contains NO row but INTERSECTS one -> a partial-width panel behind part of a row (the
        reseller hero's grey column). Attach it as the background of the CELLS it fully contains;
        if it contains none, DROP it loudly (UserWarning) -- never bake over live copy.
      - intersects NO content row -> standalone imagery (a hero banner, a full-bleed divider). The
        email-canon pattern is a FULL-WIDTH IMAGE ROW (classic Outlook renders background-image
        only via VML, which the bundle grammar forbids; a plain <img> row is the Cerberus-standard
        safe form). As a real image cell it flows through the same tiling/rasterizing/gate
        machinery as every other image -- the old cell-less Row shape had no geometry, rendered
        blank, and sorted to the END of the stack (the "missing hero" defect).
    Mutates `rows` in place (row/cell background assignment, or appending a new standalone image
    Row)."""
    contained = [row for row in rows if row.cells and _band_contains_row(b.bbox, _row_bbox(row))]
    if contained:
        for row in contained:
            if row.background is None:
                row.background = Background(color=None, image_source_layer_id=b.layer_ids[0])
        return
    # A band that contains no row but INTERSECTS one is a partial-width panel behind part of a row
    # (the reseller hero's grey column behind its live headline). Baking it as a standalone image
    # row re-renders every glyph it overlaps as pixels stacked above the live copy -- a duplicated
    # hero, owner-visible. Attach it as the background of the CELLS it fully contains instead; if
    # it contains none, drop it loudly -- never bake.
    overlapping = [row for row in rows if row.cells and _intersects(b.bbox, _row_bbox(row))]
    if overlapping:
        attached = 0
        for row in overlapping:
            for c in row.cells:
                if (c.role not in ("image", "graphic")
                        and _band_contains_row(b.bbox, c.rect.to_dict())
                        and c.background is None):
                    c.background = Background(color=None, image_source_layer_id=b.layer_ids[0])
                    attached += 1
        if not attached:
            warnings.warn(
                f"table_solver: background band layer(s) {b.layer_ids} intersect content rows "
                "but contain no attachable cell -- band DROPPED (never baked over live copy); "
                "check the design if a fill is missing there.", UserWarning, stacklevel=2)
        return
    rect = BBox.from_dict(b.bbox)
    band_cell = Cell(role="image", rect=rect, editable=False, image_source_layer_ids=list(b.layer_ids))
    rows.append(Row(cells=[band_cell]))


def _sort_rows_by_content_top(rows: list, content_top_by_cell: dict) -> None:
    """Order the stack by real geometry: band rows were appended last but belong at their vertical
    position (the emitter stacks rows in list order, sizing gaps from their bboxes). Sort by
    CONTENT top (union of cell rects), never the band-expanded row.rect: two rows sharing one band
    can both expand toward its top, and sorting on the expanded rect put the footer's divider
    AFTER the legal block it sits above (rendered 232px adrift, gate-caught). Mutates `rows` in
    place via list.sort."""
    def _row_top(row: Row) -> int:
        if row.cells:
            return int(min(content_top_by_cell.get(id(c), c.rect.top) for c in row.cells))
        return int(_row_bbox(row)["top"])

    rows.sort(key=_row_top)


def _clamp_expanded_row_tops(rows: list, content_top_by_cell: dict) -> None:
    """A band expansion may not swallow a NEIGHBOR row: clamp each expanded rect's top to the
    previous row's content bottom (the footer text row expanded up past the divider row that sits
    between it and the logo row -- overlapping row rects break the emitter's cursor walk). Mutates
    each row's `.rect` in place."""
    prev_bottom = None
    for row in rows:
        if row.cells:
            content_top = int(min(content_top_by_cell.get(id(c), c.rect.top) for c in row.cells))
            content_bottom = int(max(c.rect.bottom for c in row.cells))
        else:
            content_top = content_bottom = None
        if (row.rect is not None and prev_bottom is not None
                and int(row.rect.top) < prev_bottom and content_top is not None
                and content_top >= prev_bottom):
            row.rect = BBox(left=row.rect.left, top=prev_bottom,
                            right=row.rect.right, bottom=row.rect.bottom)
        if row.rect is not None:
            prev_bottom = max(int(row.rect.bottom), content_bottom or 0)
        elif content_bottom is not None:
            prev_bottom = content_bottom


def solve_artboard(items: list, email: str, width: int) -> TableTree:
    """Partition one artboard's classified items into a TableTree.

    `items` is the output of layer_classifier.classify_artboard(): background/highlight items are
    peeled (never occupy a grid cell themselves) -- except a standalone band with no content,
    which becomes a full-width image row (the "missing hero" fix below); content/graphic/button
    items are the grid-occupying rects the guillotine cutter partitions into rows -> cells.
    """
    content_items = [i for i in items if i.role in _CONTENT_ROLES]
    bg_items = [i for i in items if i.role == ROLE_BACKGROUND]
    highlight_by_id = {i.layer_ids[0]: i for i in items if i.role == ROLE_HIGHLIGHT}

    rows: list = []
    matched_bg_ids: set = set()
    # Pre-expansion CONTENT top (keyed by id(cell)) for any lone-link cell whose rect gets band-
    # expanded below. The row-ordering sort and the overlap clamp must read this TRUE content top,
    # not the band-expanded rect: when the band overlaps the previous row the expanded top sits
    # ABOVE prev_bottom, so the clamp guard `content_top >= prev_bottom` would wrongly fail and the
    # solver would emit overlapping row.rects (breaks the emitter's cursor walk).
    content_top_by_cell: dict = {}

    if content_items:
        item_by_idx = {}
        rects = []
        for idx, item in enumerate(content_items):
            item_by_idx[idx] = item
            rects.append({"bbox": dict(item.bbox), "idx": idx, "name": item.name, "is_text": item.is_text, "z": item.z})

        # Defect B fix: snap hairline (<= EPSILON px) overlaps BEFORE the cut tree is built --
        # _find_valid_cut itself (grid_analyzer, verified) stays untouched and exact.
        snapped_rects = _snap_hairline_overlaps(rects)
        top_node = _build_cut_tree(snapped_rects)
        content_rows = _rows_from_node(top_node, item_by_idx, highlight_by_id)

        for row in content_rows:
            row_bbox = _row_bbox(row)
            row_bg = _match_row_background(row_bbox, bg_items)
            if row_bg is not None:
                # record which background item won, so leftover (unmatched) bands can still be
                # surfaced below instead of silently dropped.
                matched_bg_ids.add(row_bg.image_source_layer_id)

            _expand_row_to_band_span(row, row_bbox, bg_items, matched_bg_ids, content_top_by_cell)

            row.background = row_bg
            rows.append(row)

    # Unmatched bands split by what they enclose -- see _attach_unmatched_band.
    for b in bg_items:
        if b.layer_ids[0] in matched_bg_ids:
            continue
        _attach_unmatched_band(b, rows)

    _sort_rows_by_content_top(rows, content_top_by_cell)
    _clamp_expanded_row_tops(rows, content_top_by_cell)

    return TableTree(email=email, width=width, rows=rows)


def build_table_trees(tree, email_override: Optional[str] = None) -> list:
    """PSD LayoutTree -> list[TableTree], one per artboard (or one for the whole canvas if the
    PSD declares no artboards). `email_override` names the single-artboard case explicitly; with
    multiple artboards it is used as a shared prefix so each email name stays distinguishable."""
    by_id = {l.id: l for l in tree.layers}
    artboard_ids = list(tree.artboards) if tree.artboards else [None]

    trees = []
    for ab_id in artboard_ids:
        items = classify_artboard(tree, ab_id, by_id)
        region_bbox = G._region_bbox_for(ab_id, by_id, tree.canvas)
        width = region_bbox["right"] - region_bbox["left"]
        name = by_id[ab_id].name if ab_id in by_id else tree.psd

        if email_override:
            email = email_override if len(artboard_ids) == 1 else f"{email_override} - {name}"
        else:
            email = name

        trees.append(solve_artboard(items, email, width))

    enforce_safety_invariant(trees)
    return trees
