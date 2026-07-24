"""TableTree: the S1 -> S2 contract.

The nested rows/cells IR the HTML emitter (S2) consumes. JSON-serializable dataclasses only
(stdlib dataclasses + json), mirroring the layout_tree.py convention: no psd-tools types cross
this seam, and every shape has explicit to_dict/from_dict so the tree can be written to disk
between S1 (this build) and S2 (a later build) without either side importing the other's internals.

Schema (see the S1 build spec):

    TableTree { email:str, width:int, rows:[Row] }
    Row  { background: Background|null, cells:[Cell], rect: BBox|null }
    Cell { role:"text"|"image"|"button"|"graphic"|"rows", rect:BBox, background: Background|null,
           editable:bool, link_slot:str|null, colspan:int(default 1),
           text: {content, align, paragraphs, runs} | null, image_source_layer_ids: [int] | null,
           rows: [Row] | null, source_layer_id: int|null,
           swallowed_editable_layer_ids: [int] | null, text_rect: BBox|null,
           sub_highlights: [BBox] | null, baked_text_layer_ids: [int] | null }
    Background { color:str|null, image_source_layer_id:int|null }

A Cell is NESTABLE: it is either a LEAF (role in text|image|button|graphic, carrying its own
content/image_source_layer_ids/editable/link_slot) or a CONTAINER (role="rows", carrying a nested
`rows:[Row]` sub-table and nothing else -- e.g. a headline stacked over a CTA inside one column of
a wider row). This is what lets a column that itself needs a further split become a real nested
sub-table instead of being rasterized to one image (table_solver._cell_from_node). A genuine
no-valid-cut residual (mutually overlapping rects with no clean guillotine cut between them) is
COLLAPSED to role="graphic" by the solver; explicitly-named `... graphic` groups also arrive as
role="graphic" cells. True nested-but-cuttable structure becomes role="rows" instead.

Two more fields close the S1.1 defect-3/5/6 gaps:
  - `source_layer_id` (text Cells): the originating PSD layer id, so S2/S3 can bind an edited
    copy string or a resolved link_slot back to the exact layer it came from.
  - `swallowed_editable_layer_ids` (button Cells only): editable text member layer ids that a
    `... button` group rasterized into its image WITHOUT keeping them live (every group member
    beyond the single label that layer_classifier keeps as the button's live text). Non-empty
    here is exactly the "button swallows a field" safety-invariant violation shape -- detected by
    `table_solver.find_baked_text_cells` (via baked_text_layer_ids; `find_trapped_editable_cells`
    is a deprecated alias), the same as an editable graphic cell.

S1.2 Defect C adds one more field, superseding the editable-only invariant above with a broader
one: never rasterize ANY live text (editable or not), not just editable fields.
  - `baked_text_layer_ids` (graphic and button Cells only): the text-layer ids that were folded
    into this rasterized unit's source with no live copy surviving.
      - role="graphic": EVERY text-layer id among its source layers -- a graphic cell must be
        genuinely non-text decorative content only, so ANY text member at all is a violation.
      - role="button": every text-layer id among its source layers EXCEPT the one kept live as
        the button's label -- a button may keep exactly one live text label; any additional text
        member (editable or not, bracketed or not, or a highlight-marked sibling) is a violation.
    Non-empty here is exactly the "baked text" safety-invariant violation shape --
    `table_solver.find_baked_text_cells` (formerly `find_trapped_editable_cells`, which only
    looked at `editable`/`swallowed_editable_layer_ids` and missed non-editable baked copy).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .layout_tree import BBox, TextInfo

# Valid Cell.role values. "rows" is the nestable CONTAINER role (see module docstring); the rest
# are leaves.
CELL_ROLES = ("text", "image", "button", "graphic", "rows")


@dataclass
class Background:
    """A peeled highlight/band, attached to whatever it sits behind (a Cell or a Row)."""

    color: Optional[str] = None
    image_source_layer_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {"color": self.color, "image_source_layer_id": self.image_source_layer_id}

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["Background"]:
        if d is None:
            return None
        return Background(color=d.get("color"), image_source_layer_id=d.get("image_source_layer_id"))


@dataclass
class Cell:
    role: str
    rect: BBox
    background: Optional[Background] = None
    editable: bool = False
    link_slot: Optional[str] = None
    colspan: int = 1
    text: Optional[TextInfo] = None
    image_source_layer_ids: Optional[list] = None  # list[int] | None
    rows: Optional[list] = None  # list[Row] | None -- set only when role == "rows" (CONTAINER)
    source_layer_id: Optional[int] = None  # text Cells only -- the originating PSD layer id
    # BOX-backed text (solver): the fill shape CONTAINS this text, so `rect` is the SHAPE's
    # geometry (what the design shows) and `text_rect` keeps the text's own ink bounds for
    # type metrics. None for ordinary text cells.
    text_rect: Optional[BBox] = None
    # Substring highlights (solver): fill chips that back only PART of this text (merge-token
    # highlights inside a paragraph). List of bbox dicts in design coords; the emitter maps
    # each to a character range and renders a shaded <span>.
    sub_highlights: Optional[list] = None
    swallowed_editable_layer_ids: Optional[list] = None  # button Cells only -- see module docstring
    baked_text_layer_ids: Optional[list] = None  # graphic/button Cells only -- see module docstring

    def __post_init__(self) -> None:
        # Fail loud at the construction/deserialization boundary rather than let an out-of-band
        # role slip through to layer_router.render_role(), which silently falls any unknown role
        # to ROLE_BODY (live plain text). This is the only place that gap was ever "validated
        # upstream"; from_dict goes through __init__, so a typo'd/version-drifted on-disk role is
        # caught here too. See CELL_ROLES and the module docstring for the role contract.
        if self.role not in CELL_ROLES:
            raise ValueError(f"Cell.role must be one of {CELL_ROLES}, got {self.role!r}")

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "rect": self.rect.to_dict(),
            "background": self.background.to_dict() if self.background is not None else None,
            "editable": self.editable,
            "link_slot": self.link_slot,
            "colspan": self.colspan,
            "text": self.text.to_dict() if self.text is not None else None,
            "image_source_layer_ids": (
                list(self.image_source_layer_ids) if self.image_source_layer_ids is not None else None
            ),
            "rows": [r.to_dict() for r in self.rows] if self.rows is not None else None,
            "source_layer_id": self.source_layer_id,
            "swallowed_editable_layer_ids": (
                list(self.swallowed_editable_layer_ids) if self.swallowed_editable_layer_ids is not None else None
            ),
            "baked_text_layer_ids": (
                list(self.baked_text_layer_ids) if self.baked_text_layer_ids is not None else None
            ),
            "text_rect": self.text_rect.to_dict() if self.text_rect is not None else None,
            "sub_highlights": list(self.sub_highlights) if self.sub_highlights is not None else None,
        }

    @staticmethod
    def from_dict(d: dict) -> "Cell":
        return Cell(
            role=d["role"],
            rect=BBox.from_dict(d["rect"]),
            background=Background.from_dict(d.get("background")),
            editable=bool(d.get("editable", False)),
            link_slot=d.get("link_slot"),
            colspan=int(d.get("colspan", 1)),
            text=TextInfo.from_dict(d["text"]) if d.get("text") is not None else None,
            image_source_layer_ids=(
                list(d["image_source_layer_ids"]) if d.get("image_source_layer_ids") is not None else None
            ),
            rows=[Row.from_dict(r) for r in d["rows"]] if d.get("rows") is not None else None,
            source_layer_id=d.get("source_layer_id"),
            swallowed_editable_layer_ids=(
                list(d["swallowed_editable_layer_ids"]) if d.get("swallowed_editable_layer_ids") is not None else None
            ),
            baked_text_layer_ids=(
                list(d["baked_text_layer_ids"]) if d.get("baked_text_layer_ids") is not None else None
            ),
            text_rect=BBox.from_dict(d["text_rect"]) if d.get("text_rect") is not None else None,
            sub_highlights=list(d["sub_highlights"]) if d.get("sub_highlights") is not None else None,
        )


@dataclass
class Row:
    background: Optional[Background] = None
    cells: list = field(default_factory=list)  # list[Cell]
    # Optional DESIGN-SPAN override (BBox). A row's visual span is its backing band, not just its
    # content ink: a 15px-tall CTA label on a 46px button shape occupies 46px of the design's
    # vertical rhythm. When set (solver, band-expansion), stacking/spacing uses this instead of
    # the union of cell rects -- without it every band-taller-than-content row under-spaces the
    # stack and the whole email drifts upward.
    rect: Optional[BBox] = None

    def to_dict(self) -> dict:
        return {
            "background": self.background.to_dict() if self.background is not None else None,
            "cells": [c.to_dict() for c in self.cells],
            "rect": self.rect.to_dict() if self.rect is not None else None,
        }

    @staticmethod
    def from_dict(d: dict) -> "Row":
        return Row(
            background=Background.from_dict(d.get("background")),
            cells=[Cell.from_dict(c) for c in d.get("cells", [])],
            rect=BBox.from_dict(d.get("rect")) if d.get("rect") is not None else None,
        )


@dataclass
class TableTree:
    email: str
    width: int
    rows: list = field(default_factory=list)  # list[Row]

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "width": self.width,
            "rows": [r.to_dict() for r in self.rows],
        }

    def to_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("indent", 2)
        return json.dumps(self.to_dict(), **kwargs)

    @staticmethod
    def from_dict(d: dict) -> "TableTree":
        return TableTree(
            email=d.get("email", ""),
            width=int(d.get("width", 0)),
            rows=[Row.from_dict(r) for r in d.get("rows", [])],
        )

    @staticmethod
    def from_json(s: str) -> "TableTree":
        return TableTree.from_dict(json.loads(s))


def trees_to_json(trees: list, **kwargs: Any) -> str:
    """Serialize a LIST of TableTree (one per artboard) -- the shape the `build` CLI command
    writes: 1 PSD -> N emails."""
    kwargs.setdefault("indent", 2)
    return json.dumps([t.to_dict() for t in trees], **kwargs)


def trees_from_json(s: str) -> list:
    return [TableTree.from_dict(d) for d in json.loads(s)]
