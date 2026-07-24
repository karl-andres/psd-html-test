"""PSDImage -> LayoutTree.

Guards every psd-tools field access -- odd/legacy layers should degrade to nulls/defaults
rather than crash the dumper.
"""

from __future__ import annotations

import os
import warnings
from typing import Any, Optional

from psd_tools import PSDImage

from .layout_tree import BBox, Canvas, Layer, LayoutTree, TextInfo, TextRun

# Map psd-tools' layer.kind values onto our IR's kind vocabulary. Anything unrecognized falls
# back to "other" rather than raising.
_KIND_MAP = {
    "type": "type",
    "pixel": "pixel",
    "shape": "shape",
    "smartobject": "smartobject",
    "group": "group",
    "psdimage": "group",
    "adjustment": "adjustment",
    "artboard": "artboard",
}

# psd-tools reports several distinct adjustment/fill-layer kinds; anything containing one of
# these substrings is treated as an "adjustment" layer per spec.
_ADJUSTMENT_HINTS = (
    "brightnesscontrast",
    "curves",
    "exposure",
    "levels",
    "vibrance",
    "huesaturation",
    "colorbalance",
    "blackwhite",
    "photofilter",
    "channelmixer",
    "colorlookup",
    "invert",
    "posterize",
    "threshold",
    "selectivecolor",
    "gradientmap",
    "solidcolor",
    "patternfill",
    "gradientfill",
)


def _safe(fn, default=None, *, context: Optional[str] = None):
    """Call a zero-arg callable, swallowing any exception and returning default instead.

    When `context` names a load-bearing value (geometry / identity / structure), a swallowed
    exception ALSO emits a warning so a degraded parse stays visible -- a silently dropped bbox
    or layer id can otherwise let the pipeline certify a near-empty email. Routine optional reads
    pass no context and stay quiet to avoid per-layer spam; the message carries the context plus
    the exception repr, so the warnings module's default "once per (message, location)" filter
    collapses runs of identical failures. The returned default is identical either way.
    """
    try:
        return fn()
    except Exception as exc:
        if context is not None:
            warnings.warn(f"psd_adapter: could not read {context}: {exc!r}", stacklevel=2)
        return default


def _map_kind(raw_kind: Optional[str]) -> str:
    if not raw_kind:
        return "other"
    k = str(raw_kind).lower()
    if k in _KIND_MAP:
        return _KIND_MAP[k]
    if any(hint in k for hint in _ADJUSTMENT_HINTS):
        return "adjustment"
    return "other"


def _bbox_of(layer: Any) -> Optional[BBox]:
    bbox = _safe(lambda: layer.bbox, context="layer.bbox")
    if not bbox:
        return None
    try:
        left, top, right, bottom = bbox
        return BBox(left=int(left), top=int(top), right=int(right), bottom=int(bottom))
    except Exception:
        return None


def _parent_id(layer: Any) -> Optional[int]:
    parent = _safe(lambda: layer.parent)
    if parent is None or isinstance(parent, PSDImage):
        return None
    pid = _safe(lambda: int(parent.layer_id), None)
    if pid is None:
        # Parent exists but its layer_id is unreadable. The parent still receives a synthetic
        # negative id in psd_to_layout_tree, so returning None here ORPHANS this child -- it
        # escapes both artboard-scoped classify and validate_tree. Emit telemetry so the orphan
        # is visible rather than silently dropped from the scoped tree.
        warnings.warn(
            "psd_adapter: parent layer_id unreadable; child orphaned (parent set to None despite "
            "an existing parent layer)",
            stacklevel=2,
        )
    return pid


def _opacity_of(layer: Any) -> float:
    raw = _safe(lambda: layer.opacity, 255)
    if raw is None:
        return 1.0
    try:
        return max(0.0, min(1.0, float(raw) / 255.0))
    except Exception:
        return 1.0


def _rgba_to_hex(values) -> Optional[str]:
    """Best-effort convert an engine-data color Values array to a #RRGGBB hex string.

    Photoshop text engine color data is typically [A, R, G, B] floats in 0..1 for RGB text
    ("Type": 1). Only a 4-element RGBA array is decoded; any other length (e.g. a CMYK 5-tuple)
    returns None rather than guess wrong.
    """
    try:
        vals = list(values)
    except Exception:
        return None
    if len(vals) != 4:
        return None
    try:
        _a, r, g, b = vals[0], vals[1], vals[2], vals[3]
        r, g, b = (max(0.0, min(1.0, float(c))) for c in (r, g, b))
        return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))
    except Exception:
        return None


_JUSTIFICATION_MAP = {0: "left", 1: "right", 2: "center", 3: "justify"}


def _extract_text(layer: Any) -> Optional[TextInfo]:
    content = _safe(lambda: layer.text)
    if content is None:
        return None

    align: Optional[str] = None
    runs: list = []

    engine_dict = _safe(lambda: layer.engine_dict)
    resource_dict = _safe(lambda: layer.resource_dict)
    font_set = None
    if resource_dict is not None:
        font_set = _safe(lambda: resource_dict.get("FontSet"))

    auto_leading_factor = 1.2  # Photoshop default; per-paragraph AutoLeading property overrides
    if engine_dict is not None:
        # Paragraph alignment + auto-leading factor: best-effort, first paragraph run only.
        try:
            para_runs = engine_dict.get("ParagraphRun", {}).get("RunArray", [])
            if para_runs:
                props = para_runs[0].get("ParagraphSheet", {}).get("Properties", {})
                justification = props.get("Justification")
                if justification is not None:
                    align = _JUSTIFICATION_MAP.get(int(justification))
                auto_leading_factor = _safe(lambda: float(props.get("AutoLeading", 1.2)), 1.2)
        except Exception:
            align = None

        # Style runs: per-run font/size/color + span length + baseline flag, best-effort.
        style_runs = _safe(lambda: engine_dict.get("StyleRun", {}).get("RunArray", []), [])
        run_lengths = _safe(
            lambda: [int(v) for v in engine_dict.get("StyleRun", {}).get("RunLengthArray", [])], []
        )

        for run_idx, run in enumerate(style_runs or []):
            font_name = None
            size = None
            color = None
            style_data = _safe(lambda: run.get("StyleSheet", {}).get("StyleSheetData", {}), {})
            try:
                font_idx = style_data.get("Font")
                if font_idx is not None and font_set is not None:
                    raw_name = font_set[int(font_idx)].get("Name")
                    if raw_name is not None:
                        font_name = str(raw_name).strip().strip("'\"") or None
            except Exception:
                font_name = None
            size = _safe(lambda: float(style_data.get("FontSize")), None)
            color = _safe(lambda: _rgba_to_hex(style_data.get("FillColor", {}).get("Values")), None)
            # RunLengthArray is parallel to RunArray: how many chars this run styles.
            length = run_lengths[run_idx] if run_idx < len(run_lengths) else None
            # FontBaseline: 0 normal, 1 superscript, 2 subscript (the footnote digits).
            baseline = _safe(lambda: int(style_data.get("FontBaseline", 0) or 0), 0)
            # RESOLVED leading: fixed StyleSheetData.Leading only when AutoLeading is OFF --
            # with AutoLeading on, the stored Leading is stale garbage (measured 17.0/14.4 on
            # 20/40px runs); the real value is size x the paragraph auto-leading factor (1.2).
            leading = None
            try:
                is_auto = bool(style_data.get("AutoLeading", True))
                if not is_auto and style_data.get("Leading") is not None:
                    leading = float(style_data.get("Leading"))
                elif size is not None:
                    leading = round(float(size) * auto_leading_factor, 2)
            except Exception:
                leading = None
            underline = _safe(lambda: bool(style_data.get("Underline", False)), False)
            runs.append(TextRun(font=font_name, size=size, color=color, length=length,
                                baseline=baseline, leading=leading, underline=underline))

    # Photoshop separates lines with bare carriage returns (\r) -- and U+2028/U+2029 on some
    # exports. Normalize ALL of them to \n here, at the single entry point, so every consumer
    # (emitter <br> conversion, SVG re-typeset wrapping, copy overflow checks) sees real line
    # breaks. Leaving \r intact silently JOINS lines: bullets ran together, "Best regards," lost
    # its line break, adjacent paragraphs merged (live-caught 2026-07-13).
    normalized = str(content).replace("\r\n", "\n").replace("\r", "\n") \
        .replace("\u2028", "\n").replace("\u2029", "\n")

    # Photoshop terminates the LAST paragraph with a return too -- a paragraph-end artifact,
    # not an authored blank line; keeping it grows every box by a line (gate-caught: +54px
    # cascade). Interior newlines (real breaks / blank lines) are preserved.
    normalized = normalized.rstrip("\n")

    # Photoshop's text engine leaks C0 control characters into layer.text (live-caught: an ETX
    # \x03 mid-copy rendered as a tofu box in Word). Swap each for a SPACE -- length-preserving,
    # so the RunLengthArray reconciliation below still aligns run spans to the content.
    normalized = "".join(" " if (ord(ch) < 32 and ch not in "\n\t") else ch for ch in normalized)

    # Reconcile run spans to the normalized content: Photoshop's RunLengthArray counts the text
    # engine's trailing terminator (which psd-tools already strips from layer.text) plus any
    # trailing returns trimmed above -- so the raw spans always sum PAST the content (measured:
    # +1 on every corpus layer). Consumers slice content by these lengths; walk from the last
    # run and absorb the surplus so the spans sum exactly.
    if runs and all(r.length is not None for r in runs):
        surplus = sum(r.length for r in runs) - len(normalized)
        for r in reversed(runs):
            if surplus <= 0:
                break
            take = min(surplus, r.length)
            r.length -= take
            surplus -= take

    # Per-paragraph SpaceAfter: ParagraphRun spans are parallel to the same char stream as the
    # style runs (same phantom-terminator caveat; reconciled below against the normalized text).
    paragraphs = []
    if engine_dict is not None:
        try:
            pr = engine_dict.get("ParagraphRun", {})
            p_lens = [int(v) for v in pr.get("RunLengthArray", [])]
            p_runs = pr.get("RunArray", [])
            for i, plen in enumerate(p_lens):
                try:
                    sa = float(p_runs[i].get("ParagraphSheet", {}).get("Properties", {}).get("SpaceAfter", 0.0))
                except Exception:
                    sa = 0.0
                paragraphs.append({"length": plen, "space_after": sa})
        except Exception:
            paragraphs = []
    if paragraphs:
        surplus = sum(pp["length"] for pp in paragraphs) - len(normalized)
        for pp in reversed(paragraphs):
            if surplus <= 0:
                break
            take = min(surplus, pp["length"])
            pp["length"] -= take
            surplus -= take
        paragraphs = [pp for pp in paragraphs if pp["length"] > 0]
        if sum(pp["length"] for pp in paragraphs) != len(normalized):
            paragraphs = []  # can't align -- consumers fall back to plain breaks

    return TextInfo(content=normalized, align=align, runs=runs, paragraphs=paragraphs)


def psd_to_layout_tree(psd_path: str) -> LayoutTree:
    """Load a PSD and produce its LayoutTree IR. Never raises on odd individual layers."""
    abs_path = os.path.abspath(psd_path)
    psd = PSDImage.open(abs_path)

    width = int(_safe(lambda: psd.width, 0, context="psd.width") or 0)
    height = int(_safe(lambda: psd.height, 0, context="psd.height") or 0)

    descendants = list(_safe(lambda: list(psd.descendants()), [], context="psd.descendants()") or [])
    total = len(descendants)

    layers: list = []
    artboards: list = []

    for index, layer in enumerate(descendants):
        raw_kind = _safe(lambda: layer.kind, "other")
        kind = _map_kind(raw_kind)
        is_group = bool(_safe(lambda: layer.is_group(), False))

        layer_id = _safe(lambda: int(layer.layer_id), None, context="layer.layer_id")
        if layer_id is None:
            # Fall back to a synthetic id so downstream code always has something stable+unique.
            layer_id = -(index + 1)

        name = str(_safe(lambda: layer.name, "") or "")
        # Use is_visible() (cascades through ancestor group visibility), NOT the bare .visible
        # flag (own-layer-only). A layer can carry visible=True on itself while sitting inside a
        # hidden ancestor group -- psd-tools' own .visible ignores that, which let hidden-group
        # descendants leak into the eligible-layer set as phantom overlapping rects (e.g. a
        # "Bullets" group toggled off wholesale, whose child text/shapes each still read
        # visible=True individually).
        visible = bool(_safe(lambda: layer.is_visible(), True))
        opacity = _opacity_of(layer)
        bbox = _bbox_of(layer)
        parent = _parent_id(layer)
        # Descendants() yields layers top-first (Photoshop panel order), which is the reverse
        # of paint order within a stacking context. Reversing the flat traversal index is a
        # best-effort global approximation of paint order (0 = bottom-most) -- it is not exact
        # across sibling groups at different depths, but is good enough for the grid analyzer,
        # which only needs a "background is near the bottom" signal.
        z = total - 1 - index

        text = _extract_text(layer) if kind == "type" else None

        layers.append(
            Layer(
                id=layer_id,
                name=name,
                kind=kind,
                visible=visible,
                opacity=opacity,
                bbox=bbox,
                z=z,
                is_group=is_group,
                parent=parent,
                text=text,
            )
        )

        if kind == "artboard":
            artboards.append(layer_id)

    return LayoutTree(
        psd=os.path.basename(abs_path),
        path=abs_path,
        canvas=Canvas(width=width, height=height),
        artboards=artboards,
        layers=layers,
    )
