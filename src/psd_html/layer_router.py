"""TableTree -> RoutedTree (render verb per leaf cell).

Walks a SOLVED TableTree (the S1 -> S2 contract, see table_tree.py) exactly once and assigns each
LEAF cell (role in "text"|"image"|"button"|"graphic" -- "rows" is a container, never a leaf) a
render verb, "live" or "raster", under one of three policies: "live", "hybrid" (the default), or
"raster". See THE EDITABILITY HARD RULE in the build context this module was speced from -- it is
reproduced here as code, not re-derived:

    is_merge(cell)          = role in ("text","button") AND (editable OR "[" in text.content)
    is_cta(cell)             = role == "button" OR (role == "text" AND link_slot is not None)
    is_brand_headline(cell)  = role == "text" AND not merge AND not cta AND
                               font_resolver.is_brand_mandatory(dominant run font)
    is_body(cell)            = role == "text" AND not merge AND not cta AND not brand_headline
    image/graphic cells are always raster (they are pixels); they may carry a link_slot (wrapped in
    an <a href> by the emitter -- raster and clickable are orthogonal, not this module's concern).

    PROTECTED = is_merge OR is_cta OR is_body -- NEVER raster, under ANY policy. Because is_body
    is the exact complement (all non-brand text that isn't merge/cta), PROTECTED exhausts every
    non-brand text cell, so "raster" computes verbs IDENTICAL to "hybrid" for every possible cell
    today; the three-way knob is kept for forward-compat / spec parity.

    live:   every text/button-label -> live; image/graphic -> raster.
    hybrid: is_brand_headline -> raster (re-typeset, never pixel-cropped); everything else text ->
            live; image/graphic -> raster.  (DEFAULT policy.)
    raster: is_brand_headline -> raster; PROTECTED text/button STILL live (EARS-202 is absolute);
            image/graphic -> raster.

AC-201 (no mutation): `route()` never writes to the tree it is given -- it only reads `Cell`/`Row`
fields and builds a separate `verbs: dict[key -> verb]` alongside the same tree object. The three
policies run against the identical tree produce byte-identical `tree.to_dict()` output; only the
verbs differ. Callers that want a guaranteed-untouched tree can pass a fresh `TableTree.from_dict`
copy per policy, but `route()` itself performs no writes regardless.

Cell identity across a walk: `id(cell)` is not a stable/serializable key (a fresh id() cycle across
runs, and a Cell is a plain dataclass with no id field of its own). Each leaf cell gets a
deterministic PATH-INDEX key instead -- a tuple of the row/cell/nested-row indices visited to reach
it, e.g. `(2, 0, "rows", 1, 3)` for "row 2, cell 0, into its nested rows, row 1, cell 3". This key is
stable for a given tree shape regardless of *which* Python object instance the walk visits, so verbs
computed for structurally-identical trees compare equal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Tuple

from .font_resolver import is_brand_mandatory
from .table_tree import Cell, TableTree

POLICIES = ("live", "hybrid", "raster")
VERBS = ("live", "raster")

# render_role() return values.
ROLE_MERGE = "merge"
ROLE_CTA = "cta"
ROLE_BRAND_HEADLINE = "brand_headline"
ROLE_BODY = "body"
ROLE_IMAGE = "image"
ROLE_GRAPHIC = "graphic"
ROLE_CONTAINER = "container"

RouteKey = Tuple  # tuple[int|str, ...] -- see module docstring "path-index key"


class EditabilityViolation(Exception):
    """FAIL LOUD: raised when a computed verb would raster a PROTECTED cell (a merge/placeholder
    field, a CTA, or plain body copy). By construction this never fires from `route()` itself --
    the guard exists to PROVE the rule holds; `_assign_verb(..., force_verb=...)` lets a test
    deliberately override the computed verb to demonstrate the guard is reachable.
    """


def _dominant_font(cell: Cell) -> "str | None":
    """The font of the first run carrying a non-empty font name, or None if the cell has no text
    / no run specifies a font. "Dominant" here just means "the one we ask font_resolver about" --
    a first-run-with-font heuristic. TextRun DOES carry per-run `length` (StyleRun.RunLengthArray),
    so a true length-weighted dominant font is possible; it is deferred because the heuristic has
    sufficed on the current corpus, not because the data is missing."""
    if cell.text is None:
        return None
    for run in cell.text.runs:
        if run.font:
            return run.font
    return None


def is_merge(cell: Cell) -> bool:
    if cell.role not in ("text", "button"):
        return False
    if cell.editable:
        return True
    return cell.text is not None and "[" in cell.text.content


def is_cta(cell: Cell) -> bool:
    if cell.role == "button":
        return True
    return cell.role == "text" and cell.link_slot is not None


def is_brand_headline(cell: Cell) -> bool:
    if cell.role != "text":
        return False
    if is_merge(cell) or is_cta(cell):
        return False
    return is_brand_mandatory(_dominant_font(cell))


def is_body(cell: Cell) -> bool:
    if cell.role != "text":
        return False
    return not (is_merge(cell) or is_cta(cell) or is_brand_headline(cell))


def is_protected(cell: Cell) -> bool:
    """PROTECTED = is_merge OR is_cta OR is_body -- never raster, under any policy."""
    if cell.role not in ("text", "button"):
        return False
    return is_merge(cell) or is_cta(cell) or is_body(cell)


def render_role(cell: Cell) -> str:
    """Classify a cell into exactly one descriptive role. Containers (role=="rows") get
    "container" -- render_role/route only assign verbs to LEAVES; a container's verb is not
    meaningful (the emitter recurses into its nested rows instead)."""
    if cell.role == "rows":
        return ROLE_CONTAINER
    if cell.role == "image":
        return ROLE_IMAGE
    if cell.role == "graphic":
        return ROLE_GRAPHIC
    # role in ("text", "button") by construction here (rows/image/graphic handled above). Note an
    # UNKNOWN role from Cell.from_dict would fall through to ROLE_BODY -> live with no warning;
    # CELL_ROLES validation is deliberately upstream's job.
    if is_merge(cell):
        return ROLE_MERGE
    if is_cta(cell):
        return ROLE_CTA
    if is_brand_headline(cell):
        return ROLE_BRAND_HEADLINE
    # role == "button" without merge/cta already covered above (is_cta is True for every button),
    # so the only remaining branch reachable for role=="text" is body copy.
    return ROLE_BODY


def _assign_verb(cell: Cell, policy: str, *, force_verb: "str | None" = None) -> str:
    """Compute (or, for tests, force) the verb for one leaf cell under `policy`, then enforce the
    guard: a PROTECTED cell may never come out "raster". `force_verb` exists solely so AC-202 tests
    can prove the guard is reachable -- normal callers never pass it."""
    role = render_role(cell)

    if force_verb is not None:
        verb = force_verb
    elif role in (ROLE_IMAGE, ROLE_GRAPHIC):
        verb = "raster"
    elif role == ROLE_BRAND_HEADLINE:
        verb = "live" if policy == "live" else "raster"
    else:
        # merge, cta, body -- always live, under every policy.
        verb = "live"

    if verb == "raster" and is_protected(cell):
        layer_desc = _describe_cell(cell)
        raise EditabilityViolation(
            f"EditabilityViolation: policy {policy!r} would raster a PROTECTED cell ({layer_desc}) "
            f"-- merge/cta/body text must never be routed to raster."
        )
    return verb


def _describe_cell(cell: Cell) -> str:
    name = None
    if cell.text is not None and cell.text.content:
        name = cell.text.content
    elif cell.source_layer_id is not None:
        name = f"layer #{cell.source_layer_id}"
    else:
        name = "<unnamed cell>"
    rect = cell.rect.to_dict() if cell.rect is not None else None
    return f"{name!r} rect={rect}"


@dataclass
class RoutedTree:
    policy: str
    tree: TableTree
    verbs: dict  # dict[RouteKey, str]


def _iter_leaf_cells(rows: list, prefix: tuple) -> Iterator[Tuple[Cell, RouteKey]]:
    """Depth-first walk yielding every LEAF cell with its deterministic path-index key. Containers
    (role=="rows") are walked into (key extended with the literal "rows" marker + nested-row index)
    but never themselves yielded -- only leaves get verbs."""
    for row_idx, row in enumerate(rows):
        for cell_idx, cell in enumerate(row.cells):
            key = prefix + (row_idx, cell_idx)
            if cell.role == "rows":
                nested = cell.rows or []
                yield from _iter_leaf_cells(nested, key + ("rows",))
            else:
                yield cell, key


def route(tree: TableTree, policy: str = "hybrid") -> RoutedTree:
    """Walk `tree` once, compute a render verb per leaf cell under `policy`, and return a
    RoutedTree. Never mutates `tree` (AC-201) -- only reads Cell/Row fields. Raises
    EditabilityViolation if (by construction, never) a computed verb would raster a PROTECTED
    cell."""
    if policy not in POLICIES:
        raise ValueError(f"unknown routing policy {policy!r} -- must be one of {POLICIES}")

    verbs: dict = {}
    for cell, key in _iter_leaf_cells(tree.rows, ()):
        verbs[key] = _assign_verb(cell, policy)
    return RoutedTree(policy=policy, tree=tree, verbs=verbs)


def iter_routed(routed: RoutedTree) -> Iterator[Tuple[Cell, RouteKey, str]]:
    """Yield (cell, key, verb) for every leaf cell in `routed.tree`, in the same deterministic
    walk order `route()` used to build `routed.verbs`."""
    for cell, key in _iter_leaf_cells(routed.tree.rows, ()):
        yield cell, key, routed.verbs[key]
