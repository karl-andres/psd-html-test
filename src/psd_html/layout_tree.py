"""LayoutTree: the IR produced by the PSD adapter.

Schema (per PSD), see spec:

    {
      psd: <filename>, path: <abs>, canvas: {width, height},
      artboards: [<int layer ids or names>],
      layers: [ {
        id, name, kind, visible, opacity,
        bbox: {left, top, right, bottom} | null,
        z, is_group, parent,
        text: null | { content, align, paragraphs, runs: [{font, size, color, length, baseline, leading, underline}] }
      } ]
    }

Kept dependency-free (stdlib only, dataclasses + json) so it can be imported by both the PSD
adapter and the grid analyzer without pulling in psd-tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# Valid layer "kind" values per spec.
LAYER_KINDS = ("type", "pixel", "shape", "smartobject", "group", "adjustment", "artboard", "other")


@dataclass
class BBox:
    left: int
    top: int
    right: int
    bottom: int

    def to_dict(self) -> dict:
        return {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["BBox"]:
        if d is None:
            return None
        return BBox(left=int(d["left"]), top=int(d["top"]), right=int(d["right"]), bottom=int(d["bottom"]))

    # width/height/area all CLAMP a degenerate (right<left or bottom<top) box to 0 rather than
    # returning a negative size. Degenerate boxes occur legitimately and transiently in the layout
    # math (empty unions, over-clamped crops) and are tolerated -- `.area` already clamped, but
    # `.width`/`.height` did not, so a negative dimension could flow into padding/wrap/font-shrink
    # arithmetic in html_emitter. Clamping all three keeps the size contract consistent and
    # guarantees no consumer ever multiplies by a negative extent. (The raw edges stay untouched,
    # so the degenerate-rect guards that test `right <= left` directly still fire.)
    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass
class TextRun:
    font: Optional[str] = None
    size: Optional[float] = None
    color: Optional[str] = None
    # Photoshop engine-data run span: how many characters of TextInfo.content this run styles
    # (StyleRun.RunLengthArray). None on legacy/synthetic runs -- consumers must fall back to
    # dominant-run styling when any length is missing.
    length: Optional[int] = None
    # FontBaseline from StyleSheetData: 0 = normal, 1 = superscript, 2 = subscript. The design's
    # footnote digits ("AI(1)") are baseline-1 runs -- dropping this renders them as plain text.
    baseline: int = 0
    # RESOLVED leading in px: the fixed StyleSheetData.Leading when AutoLeading is off, else
    # size x the paragraph's auto-leading factor (1.2 default). NOTE: when AutoLeading is ON the
    # stored Leading value is STALE GARBAGE (measured 17.0/14.4 on 20-40px runs) -- never read it
    # raw. The design's ink boxes obey leading*(lines-1) + size, so this drives all rhythm math.
    leading: Optional[float] = None
    # StyleSheetData.Underline: the design's link affordance ("Connect", "Get the infographic").
    # Dropping it rendered every designed underline as plain text (owner-caught).
    underline: bool = False

    def to_dict(self) -> dict:
        return {"font": self.font, "size": self.size, "color": self.color,
                "length": self.length, "baseline": self.baseline, "leading": self.leading,
                "underline": self.underline}

    @staticmethod
    def from_dict(d: dict) -> "TextRun":
        return TextRun(font=d.get("font"), size=d.get("size"), color=d.get("color"),
                       length=d.get("length"), baseline=int(d.get("baseline", 0) or 0),
                       leading=d.get("leading"), underline=bool(d.get("underline", False)))


@dataclass
class TextInfo:
    content: str
    align: Optional[str] = None
    runs: list = field(default_factory=list)  # list[TextRun]
    # Per-paragraph spacing from ParagraphRun: [{"length": chars-of-paragraph-incl-its-return,
    # "space_after": px}]. Photoshop paragraph marks (\r, normalized to \n) carry SpaceAfter
    # padding the design shows as inter-paragraph gaps -- NOT blank lines (owner-diagnosed).
    paragraphs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "align": self.align,
            "runs": [r.to_dict() for r in self.runs],
            "paragraphs": [dict(p) for p in self.paragraphs],
        }

    @staticmethod
    def from_dict(d: dict) -> "TextInfo":
        return TextInfo(
            content=d.get("content", ""),
            align=d.get("align"),
            runs=[TextRun.from_dict(r) for r in d.get("runs", [])],
            paragraphs=[dict(p) for p in d.get("paragraphs", [])],
        )


@dataclass
class Layer:
    id: int
    name: str
    kind: str
    visible: bool
    opacity: float
    bbox: Optional[BBox]
    z: int
    is_group: bool
    parent: Optional[int]
    text: Optional[TextInfo] = None

    def __post_init__(self) -> None:
        # Same contract-at-the-boundary guard as Cell.role: reject a kind outside LAYER_KINDS at
        # construction/deserialization instead of letting it degrade silently downstream. The PSD
        # adapter's _map_kind already normalizes every psd-tools kind into this set, so this only
        # ever fires on a hand-authored/corrupted/version-drifted on-disk LayoutTree.
        if self.kind not in LAYER_KINDS:
            raise ValueError(f"Layer.kind must be one of {LAYER_KINDS}, got {self.kind!r}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "visible": self.visible,
            "opacity": self.opacity,
            "bbox": self.bbox.to_dict() if self.bbox is not None else None,
            "z": self.z,
            "is_group": self.is_group,
            "parent": self.parent,
            "text": self.text.to_dict() if self.text is not None else None,
        }

    @staticmethod
    def from_dict(d: dict) -> "Layer":
        return Layer(
            id=int(d["id"]),
            name=d.get("name", ""),
            kind=d.get("kind", "other"),
            visible=bool(d.get("visible", True)),
            opacity=float(d.get("opacity", 1.0)),
            bbox=BBox.from_dict(d.get("bbox")),
            z=int(d.get("z", 0)),
            is_group=bool(d.get("is_group", False)),
            parent=d.get("parent"),
            text=TextInfo.from_dict(d["text"]) if d.get("text") is not None else None,
        )


@dataclass
class Canvas:
    width: int
    height: int

    def to_dict(self) -> dict:
        return {"width": self.width, "height": self.height}

    @staticmethod
    def from_dict(d: dict) -> "Canvas":
        return Canvas(width=int(d["width"]), height=int(d["height"]))


@dataclass
class LayoutTree:
    psd: str
    path: str
    canvas: Canvas
    artboards: list = field(default_factory=list)  # list[int|str]
    layers: list = field(default_factory=list)  # list[Layer]

    def to_dict(self) -> dict:
        return {
            "psd": self.psd,
            "path": self.path,
            "canvas": self.canvas.to_dict(),
            "artboards": list(self.artboards),
            "layers": [l.to_dict() for l in self.layers],
        }

    def to_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("indent", 2)
        return json.dumps(self.to_dict(), **kwargs)

    @staticmethod
    def from_dict(d: dict) -> "LayoutTree":
        return LayoutTree(
            psd=d.get("psd", ""),
            path=d.get("path", ""),
            canvas=Canvas.from_dict(d["canvas"]),
            artboards=list(d.get("artboards", [])),
            layers=[Layer.from_dict(l) for l in d.get("layers", [])],
        )

    @staticmethod
    def from_json(s: str) -> "LayoutTree":
        return LayoutTree.from_dict(json.loads(s))


# --- Rect records: the PSD-agnostic projection consumed by grid_analyzer -----------------------
#
# A "rect record" is the minimal shape the guillotine grid analyzer needs. It intentionally
# carries no psd-tools types so the analyzer can be exercised with synthetic data in tests.
#
#   { name: str, kind: str, bbox: {left,top,right,bottom}, is_text: bool, z: int }


def layout_tree_to_rects(tree: LayoutTree, artboard_id: Optional[int] = None) -> list:
    """Project a LayoutTree's visible leaf layers into plain rect-record dicts.

    If artboard_id is given, restrict to descendants of that artboard (best-effort: the
    artboard_id arg is currently IGNORED -- every visible leaf-layer rect record is returned
    regardless, since artboard scoping is done by the grid_analyzer / spike runner which
    understands the artboard hierarchy). Left here as the single seam so grid_analyzer never
    needs to import psd-tools types.
    """
    leaf_kinds = {"type", "pixel", "shape", "smartobject", "other"}
    rects = []
    for layer in tree.layers:
        if not layer.visible:
            continue
        if layer.kind not in leaf_kinds:
            continue
        if layer.bbox is None:
            continue
        rects.append(
            {
                "name": layer.name,
                "kind": layer.kind,
                "bbox": layer.bbox.to_dict(),
                "is_text": layer.kind == "type",
                "z": layer.z,
            }
        )
    return rects
