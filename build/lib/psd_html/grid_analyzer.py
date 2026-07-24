"""Guillotine classifier -> report.

Kept PSD-agnostic: the core `analyze_region()` function operates on plain "rect record" dicts
(``{name, kind, bbox:{left,top,right,bottom}, is_text, z}``) and never imports psd-tools or
LayoutTree types, so it can be exercised with synthetic data in tests. `analyze_layout_tree()` is
the tree-aware wrapper used by the CLI: it understands the artboard hierarchy, does
background/adjustment-layer detection, and rolls per-artboard `analyze_region()` results up into
the per-PSD report shape described in the spike spec.
"""

from __future__ import annotations

import math
from typing import Optional

EPSILON = 2  # px tolerance for overlap detection (not used for guillotine cut validity, which is
             # exact: candidate cut lines are real rect edges, so no fuzz is needed there).

_LEAF_KINDS = {"type", "pixel", "shape", "smartobject", "other"}


def _pct_clean(numer: float, denom: float) -> float:
    """Vacuous 1.0 when denom <= 0 (e.g. a region/psd with zero eligible rects) -- nothing to
    flatten, so trivially "clean"."""
    return (numer / denom) if denom > 0 else 1.0


# --- geometry helpers -------------------------------------------------------------------------


def _area(bbox: dict) -> int:
    w = bbox["right"] - bbox["left"]
    h = bbox["bottom"] - bbox["top"]
    return max(0, w) * max(0, h)


def overlaps(a: dict, b: dict, eps: float = EPSILON) -> bool:
    """Two rect records OVERLAP if their intersection is wider AND taller than eps px."""
    ba, bb = a["bbox"], b["bbox"]
    left = max(ba["left"], bb["left"])
    right = min(ba["right"], bb["right"])
    top = max(ba["top"], bb["top"])
    bottom = min(ba["bottom"], bb["bottom"])
    return (right - left) > eps and (bottom - top) > eps


def _any_pairwise_overlap(rects: list, eps: float = EPSILON) -> bool:
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            if overlaps(a, b, eps):
                return True
    return False


def _union_bbox(bboxes: list) -> dict:
    return {
        "left": min(b["left"] for b in bboxes),
        "top": min(b["top"] for b in bboxes),
        "right": max(b["right"] for b in bboxes),
        "bottom": max(b["bottom"] for b in bboxes),
    }


def _clamp_bbox(bbox: dict, region: dict) -> dict:
    return {
        "left": max(bbox["left"], region["left"]),
        "top": max(bbox["top"], region["top"]),
        "right": min(bbox["right"], region["right"]),
        "bottom": min(bbox["bottom"], region["bottom"]),
    }


# --- guillotine partition ----------------------------------------------------------------------
#
# A cut candidate is a real rect edge (not a midpoint): a horizontal cut at y is VALID if no rect
# has top < y < bottom (does not cross a rect's open interior) and at least one rect lies fully
# above (bottom <= y) and at least one lies fully below (top >= y). Using raw edge values (rather
# than midpoints between them) means touching rects (e.g. a header whose bottom == a body's top)
# correctly cut cleanly with zero gap, not just rects separated by whitespace.


def _find_valid_cut(rects: list) -> Optional[tuple]:
    for lo_key, hi_key in (("top", "bottom"), ("left", "right")):
        edges = sorted({r["bbox"][lo_key] for r in rects} | {r["bbox"][hi_key] for r in rects})
        for y in edges:
            crosses = any(r["bbox"][lo_key] < y < r["bbox"][hi_key] for r in rects)
            if crosses:
                continue
            above = [r for r in rects if r["bbox"][hi_key] <= y]
            below = [r for r in rects if r["bbox"][lo_key] >= y]
            if above and below and len(above) + len(below) == len(rects):
                return above, below
    return None


def _partition(rects: list) -> list:
    """Recursively guillotine-partition rects into cells. Returns a list of cells, where each
    cell is a list of rect records: len==1 means a CLEAN cell, len>=2 means a MUST-FLATTEN
    cluster (no valid cut exists to separate its members further)."""
    if len(rects) <= 1:
        return [[r] for r in rects]
    cut = _find_valid_cut(rects)
    if cut is None:
        return [list(rects)]
    above, below = cut
    return _partition(above) + _partition(below)


# --- cluster classification --------------------------------------------------------------------


def _classify_cluster(members: list, eps: float = EPSILON) -> str:
    """"text_over_fill" (RECOVERABLE) if the cluster is exactly one non-text layer plus one or
    more non-overlapping TEXT layers; otherwise "hard_overlap"."""
    text_members = [r for r in members if r.get("is_text")]
    non_text_members = [r for r in members if not r.get("is_text")]
    if len(non_text_members) == 1 and len(text_members) >= 1 and not _any_pairwise_overlap(text_members, eps):
        return "text_over_fill"
    return "hard_overlap"


# --- core, PSD-agnostic entry point --------------------------------------------------------------


def analyze_region(rects: list, eps: float = EPSILON) -> dict:
    """Run the guillotine classifier over one region's (already-filtered) leaf rect records.

    `rects` should already exclude group containers, adjustment layers, the background layer (if
    any), and zero-area / fully-out-of-bounds rects -- this function only implements the
    guillotine-partition + cluster-classification metric itself.
    """
    valid_rects = [r for r in rects if _area(r["bbox"]) > 0]
    cells = _partition(valid_rects)

    grid_clean_count = 0
    flatten_count = 0
    text_in_flatten = 0
    text_over_fill_member_count = 0
    hard_overlap_cluster_count = 0
    clusters: list = []

    for cell in cells:
        if len(cell) <= 1:
            grid_clean_count += len(cell)
            continue
        flatten_count += len(cell)
        text_in_flatten += sum(1 for r in cell if r.get("is_text"))
        kind = _classify_cluster(cell, eps)
        if kind == "text_over_fill":
            text_over_fill_member_count += len(cell)
        else:
            hard_overlap_cluster_count += 1
        clusters.append(
            {
                "kind": kind,
                "members": [{"name": r["name"], "bbox": r["bbox"]} for r in cell],
                "cluster_bbox": _union_bbox([r["bbox"] for r in cell]),
            }
        )

    denom = grid_clean_count + flatten_count
    pct_grid_clean = _pct_clean(grid_clean_count, denom)
    pct_grid_clean_adjusted = _pct_clean(grid_clean_count + text_over_fill_member_count, denom)

    return {
        "rect_count": len(valid_rects),
        "grid_clean_count": grid_clean_count,
        "flatten_count": flatten_count,
        "pct_grid_clean": pct_grid_clean,
        "pct_grid_clean_adjusted": pct_grid_clean_adjusted,
        "text_in_flatten": text_in_flatten,
        "clusters": clusters,
        "hard_overlap_cluster_count": hard_overlap_cluster_count,
    }


# --- tree-aware wrapper: artboard scoping + background/adjustment detection --------------------


def _artboard_of(layer, by_id: dict) -> Optional[int]:
    """Walk the parent chain to find the nearest ancestor artboard's id, if any."""
    seen: set = set()
    cur = layer
    while cur is not None and cur.parent is not None and cur.parent not in seen:
        seen.add(cur.parent)
        parent = by_id.get(cur.parent)
        if parent is None:
            return None
        if parent.kind == "artboard":
            return parent.id
        cur = parent
    return None


def _region_bbox_for(artboard_id, by_id: dict, canvas) -> dict:
    whole = {"left": 0, "top": 0, "right": canvas.width, "bottom": canvas.height}
    if artboard_id is None:
        return whole
    layer = by_id.get(artboard_id)
    if layer is None or layer.bbox is None:
        return whole
    return _clamp_bbox(layer.bbox.to_dict(), whole)


def _find_background(candidates: list, region_area: int, area_ratio_threshold: float = 0.95, bottom_fraction: float = 0.25):
    """candidates: list of Layer objects (already filtered to visible leaf layers in one region).

    A layer is BACKGROUND if its bbox covers >= area_ratio_threshold of the region area AND it
    sits within the bottom `bottom_fraction` of the region's z-order (paint order 0 = bottom-most).
    Returns the Layer (or None) -- at most one background layer per region.
    """
    if not candidates or region_area <= 0:
        return None
    n = len(candidates)
    by_z = sorted(candidates, key=lambda l: l.z)
    bottom_rank_count = max(1, math.ceil(bottom_fraction * n))
    bottom_ids = {l.id for l in by_z[:bottom_rank_count]}

    scored = []
    for layer in candidates:
        if layer.bbox is None:
            continue
        ratio = layer.bbox.area / region_area
        if ratio >= area_ratio_threshold and layer.id in bottom_ids:
            scored.append((layer, ratio))
    if not scored:
        return None
    return min(scored, key=lambda t: (t[0].z, -t[1]))[0]


_VALID_MODELS = ("v1", "v2", "both")

_BAND_MIN_WIDTH_FRACTION = 0.85
_BAND_ELIGIBLE_KINDS = {"shape", "pixel", "smartobject", "other"}  # non-text leaf kinds


def _is_band(rect_record: dict, region_width: int, min_width_fraction: float = _BAND_MIN_WIDTH_FRACTION) -> bool:
    """v2 band-peel classifier for ONE eligible leaf rect record (post background-exclusion,
    region-clamped -- the same records v1's `analyze_region()` consumes).

    BAND (peel to a background, NOT a grid rect): a non-text layer (kind in
    shape|pixel|smartobject|other) whose width covers >= `min_width_fraction` of the enclosing
    artboard/region width. These are full-bleed section fills, hero backgrounds, and header/footer
    bars -- in HTML email they become a row/cell background (bgcolor / background-image), not a
    grid cell, with content layered on top.

    CONTENT (grid rect, handed to the guillotine partitioner): everything else -- ALL text layers
    (never peeled, regardless of width), plus narrower non-text layers (in-flow images, chips,
    buttons, logos, icons).
    """
    if rect_record.get("is_text"):
        return False
    if rect_record.get("kind") not in _BAND_ELIGIBLE_KINDS:
        return False
    if region_width <= 0:
        return False
    width = rect_record["bbox"]["right"] - rect_record["bbox"]["left"]
    return width >= min_width_fraction * region_width


def _split_bands(rect_records: list, region_width: int, min_width_fraction: float = _BAND_MIN_WIDTH_FRACTION) -> tuple:
    """Partition one region's eligible rect records into (bands, content) per `_is_band()`."""
    bands: list = []
    content: list = []
    for r in rect_records:
        (bands if _is_band(r, region_width, min_width_fraction) else content).append(r)
    return bands, content


def analyze_layout_tree(tree, rects: Optional[list] = None, model: str = "both") -> dict:
    """Tree-aware entry point: buckets visible leaf layers per artboard (or the whole canvas if
    there are none), detects the background layer and counts adjustment layers per region, runs
    `analyze_region()` on each region, and rolls the results up into the per-PSD report shape.

    `rects` is accepted for interface compatibility with the CLI (which computes a flat
    PSD-agnostic rect projection via `layout_tree_to_rects`) but is not required: this function
    re-derives per-artboard rect buckets directly from `tree.layers`, since only the tree carries
    the parent hierarchy needed to scope rects to their enclosing artboard.

    `model` selects which metric(s) to compute and attach, and defaults to "both" so v1 and v2
    are always available side by side for comparison:
      - "v1": the original raw-rect guillotine metric only -- every rect (text or not, any width)
        is a grid-occupying rect. Top-level keys (`pct_grid_clean` etc) are exactly as before this
        parameter existed.
      - "v2": the band-peel metric only -- full-bleed non-text "band" rects (see `_is_band()`) are
        peeled to a background instead of occupying a grid cell; the guillotine metric then runs
        on the remaining CONTENT rects only. Reported under the `v2` key (`content_*` fields).
      - "both" (default): compute and attach both v1 (top-level, unchanged) and v2 (`v2` key), so
        the two can be compared PSD-by-PSD without losing v1.
    """
    if model not in _VALID_MODELS:
        raise ValueError(f"model must be one of {_VALID_MODELS}, got {model!r}")
    want_v1 = model in ("v1", "both")
    want_v2 = model in ("v2", "both")

    by_id = {l.id: l for l in tree.layers}

    buckets: dict = {}
    adjustment_counts: dict = {}

    for layer in tree.layers:
        if not layer.visible:
            continue
        if layer.kind == "adjustment":
            ab = _artboard_of(layer, by_id)
            adjustment_counts[ab] = adjustment_counts.get(ab, 0) + 1
            continue
        if layer.kind not in _LEAF_KINDS:
            continue
        if layer.bbox is None or layer.bbox.area <= 0:
            continue
        ab = _artboard_of(layer, by_id)
        buckets.setdefault(ab, []).append(layer)

    # Make sure every declared artboard shows up even if it happened to have zero leaf layers.
    for ab_id in tree.artboards:
        buckets.setdefault(ab_id, [])
    if not buckets:
        buckets[None] = []

    artboard_reports = []
    for ab_id, layers in buckets.items():
        region_bbox = _region_bbox_for(ab_id, by_id, tree.canvas)
        region_area = _area(region_bbox)
        region_width = region_bbox["right"] - region_bbox["left"]

        background = _find_background(layers, region_area)
        eligible = [l for l in layers if background is None or l.id != background.id]

        rect_records = []
        for l in eligible:
            clamped = _clamp_bbox(l.bbox.to_dict(), region_bbox)
            if _area(clamped) <= 0:
                continue
            rect_records.append(
                {
                    "name": l.name,
                    "kind": l.kind,
                    "bbox": clamped,
                    "is_text": l.kind == "type",
                    "z": l.z,
                }
            )

        region_report: dict = {
            "artboard_id": ab_id,
            "artboard_name": by_id[ab_id].name if ab_id in by_id else None,
            "region_bbox": region_bbox,
            "background_layer": {"name": background.name, "bbox": background.bbox.to_dict()} if background else None,
            "adjustment_layer_count": adjustment_counts.get(ab_id, 0),
        }

        if want_v1:
            region_report.update(analyze_region(rect_records))
        else:
            # model == "v2" only: v1's guillotine never ran, but surface the raw count for
            # reference alongside the v2 content_rect_count.
            region_report["rect_count"] = len(rect_records)

        if want_v2:
            bands, content = _split_bands(rect_records, region_width)
            content_result = analyze_region(content)
            region_report["v2"] = {
                "n_bands_peeled": len(bands),
                "bands_peeled": [{"name": r["name"], "bbox": r["bbox"]} for r in bands],
                "content_rect_count": content_result["rect_count"],
                "content_grid_clean_count": content_result["grid_clean_count"],
                "content_flatten_count": content_result["flatten_count"],
                "content_pct_grid_clean": content_result["pct_grid_clean"],
                "content_pct_grid_clean_adjusted": content_result["pct_grid_clean_adjusted"],
                "content_text_in_flatten": content_result["text_in_flatten"],
                "content_hard_overlap_clusters": content_result["hard_overlap_cluster_count"],
                "content_clusters": content_result["clusters"],
            }

        artboard_reports.append(region_report)

    report: dict = {
        "psd": tree.psd,
        "path": tree.path,
        "artboard_count": len(tree.artboards) if tree.artboards else 1,
        "layers_total": len(tree.layers),
        "artboards": artboard_reports,
        "adjustment_layer_count": sum(a["adjustment_layer_count"] for a in artboard_reports),
    }

    if want_v1:
        total_grid_clean = sum(a["grid_clean_count"] for a in artboard_reports)
        total_flatten = sum(a["flatten_count"] for a in artboard_reports)
        denom = total_grid_clean + total_flatten
        text_over_fill_members = sum(
            sum(len(c["members"]) for c in a["clusters"] if c["kind"] == "text_over_fill") for a in artboard_reports
        )
        report.update(
            {
                "grid_clean_count": total_grid_clean,
                "flatten_count": total_flatten,
                "pct_grid_clean": _pct_clean(total_grid_clean, denom),
                "pct_grid_clean_adjusted": _pct_clean(total_grid_clean + text_over_fill_members, denom),
                "text_in_flatten": sum(a["text_in_flatten"] for a in artboard_reports),
                "total_clusters": sum(len(a["clusters"]) for a in artboard_reports),
                "hard_overlap_clusters": sum(a["hard_overlap_cluster_count"] for a in artboard_reports),
            }
        )

    if want_v2:
        total_content_grid_clean = sum(a["v2"]["content_grid_clean_count"] for a in artboard_reports)
        total_content_flatten = sum(a["v2"]["content_flatten_count"] for a in artboard_reports)
        content_denom = total_content_grid_clean + total_content_flatten
        content_text_over_fill_members = sum(
            sum(len(c["members"]) for c in a["v2"]["content_clusters"] if c["kind"] == "text_over_fill")
            for a in artboard_reports
        )
        report["v2"] = {
            "n_bands_peeled": sum(a["v2"]["n_bands_peeled"] for a in artboard_reports),
            "content_rect_count": sum(a["v2"]["content_rect_count"] for a in artboard_reports),
            "content_grid_clean_count": total_content_grid_clean,
            "content_flatten_count": total_content_flatten,
            "content_pct_grid_clean": _pct_clean(total_content_grid_clean, content_denom),
            "content_pct_grid_clean_adjusted": _pct_clean(
                total_content_grid_clean + content_text_over_fill_members, content_denom
            ),
            "content_text_in_flatten": sum(a["v2"]["content_text_in_flatten"] for a in artboard_reports),
            "content_total_clusters": sum(len(a["v2"]["content_clusters"]) for a in artboard_reports),
            "content_hard_overlap_clusters": sum(a["v2"]["content_hard_overlap_clusters"] for a in artboard_reports),
        }

    return report


def aggregate_corpus(per_psd_reports: list) -> dict:
    """Roll a list of `analyze_layout_tree()` results up into the corpus-wide summary.

    v1 fields (unchanged) are included whenever the per-psd reports carry v1 output; a `v2`
    sub-dict is added alongside whenever they carry v2 output (i.e. whatever `model` was passed to
    `analyze_layout_tree()` -- with the default "both", every report has both, exactly as before
    this parameter existed for v1).
    """
    n_psd = len(per_psd_reports)
    if n_psd == 0:
        return {
            "n_psd": 0,
            "n_artboards": 0,
            "mean_pct_grid_clean": 0.0,
            "mean_pct_grid_clean_adjusted": 0.0,
            "total_clusters": 0,
            "total_hard_overlap_clusters": 0,
            "total_text_in_flatten": 0,
        }

    has_v1 = all("pct_grid_clean" in p for p in per_psd_reports)
    has_v2 = all("v2" in p for p in per_psd_reports)

    corpus: dict = {
        "n_psd": n_psd,
        "n_artboards": sum(p["artboard_count"] for p in per_psd_reports),
    }

    if has_v1:
        corpus.update(
            {
                "mean_pct_grid_clean": sum(p["pct_grid_clean"] for p in per_psd_reports) / n_psd,
                "mean_pct_grid_clean_adjusted": sum(p["pct_grid_clean_adjusted"] for p in per_psd_reports) / n_psd,
                "total_clusters": sum(p["total_clusters"] for p in per_psd_reports),
                "total_hard_overlap_clusters": sum(p["hard_overlap_clusters"] for p in per_psd_reports),
                "total_text_in_flatten": sum(p["text_in_flatten"] for p in per_psd_reports),
            }
        )

    if has_v2:
        corpus["v2"] = {
            "n_bands_peeled": sum(p["v2"]["n_bands_peeled"] for p in per_psd_reports),
            "content_rect_count": sum(p["v2"]["content_rect_count"] for p in per_psd_reports),
            "mean_content_pct_grid_clean": sum(p["v2"]["content_pct_grid_clean"] for p in per_psd_reports) / n_psd,
            "mean_content_pct_grid_clean_adjusted": (
                sum(p["v2"]["content_pct_grid_clean_adjusted"] for p in per_psd_reports) / n_psd
            ),
            "total_content_hard_overlap_clusters": sum(
                p["v2"]["content_hard_overlap_clusters"] for p in per_psd_reports
            ),
            "total_content_text_in_flatten": sum(p["v2"]["content_text_in_flatten"] for p in per_psd_reports),
        }

    return corpus
