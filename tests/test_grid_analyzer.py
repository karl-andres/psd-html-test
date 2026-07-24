"""Guillotine grid analyzer: synthetic rect-record cases a-e, no PSD required.

analyze_region() is PSD-agnostic (plain rect-record dicts in, report dict out), which is exactly
the seam the spec calls for: feed the analyzer synthetic rects without ever touching a PSD file.
"""

import pytest

from psd_html.grid_analyzer import (
    _is_band,
    _split_bands,
    aggregate_corpus,
    analyze_layout_tree,
    analyze_region,
    overlaps,
)
from psd_html.layout_tree import BBox, Canvas, Layer, LayoutTree


def rect(name, left, top, right, bottom, is_text=False, kind=None, z=0):
    return {
        "name": name,
        "kind": kind or ("type" if is_text else "shape"),
        "bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
        "is_text": is_text,
        "z": z,
    }


# --- (a) 2x2 clean grid -> pct=1.0, 0 clusters --------------------------------------------------


def test_case_a_2x2_clean_grid():
    rects = [
        rect("tl", 0, 0, 100, 100, z=0),
        rect("tr", 100, 0, 200, 100, z=1),
        rect("bl", 0, 100, 100, 200, z=2),
        rect("br", 100, 100, 200, 200, z=3),
    ]
    report = analyze_region(rects)
    assert report["pct_grid_clean"] == 1.0
    assert report["pct_grid_clean_adjusted"] == 1.0
    assert report["grid_clean_count"] == 4
    assert report["flatten_count"] == 0
    assert report["clusters"] == []


# --- (b) two overlapping rects -> 1 cluster, both flatten ---------------------------------------


def test_case_b_two_overlapping_rects():
    rects = [
        rect("a", 0, 0, 100, 100, z=0),
        rect("b", 50, 50, 150, 150, z=1),
    ]
    report = analyze_region(rects)
    assert report["grid_clean_count"] == 0
    assert report["flatten_count"] == 2
    assert report["pct_grid_clean"] == 0.0
    assert len(report["clusters"]) == 1
    assert report["clusters"][0]["kind"] == "hard_overlap"
    member_names = {m["name"] for m in report["clusters"][0]["members"]}
    assert member_names == {"a", "b"}


# --- (c) header row + 3-col body (nested guillotine) -> pct=1.0 --------------------------------


def test_case_c_header_plus_three_col_body_nested_guillotine():
    rects = [
        rect("header", 0, 0, 300, 50, z=0),
        rect("col1", 0, 50, 100, 150, z=1),
        rect("col2", 100, 50, 200, 150, z=2),
        rect("col3", 200, 50, 300, 150, z=3),
    ]
    report = analyze_region(rects)
    assert report["pct_grid_clean"] == 1.0
    assert report["pct_grid_clean_adjusted"] == 1.0
    assert report["grid_clean_count"] == 4
    assert report["flatten_count"] == 0
    assert report["clusters"] == []


# --- (d) one small text rect inside one big fill rect -> text_over_fill, pct_adjusted=1.0 -------


def test_case_d_text_over_fill_recoverable():
    rects = [
        rect("fill", 0, 0, 300, 300, is_text=False, z=0),
        rect("headline", 50, 50, 150, 100, is_text=True, z=1),
    ]
    report = analyze_region(rects)
    assert report["grid_clean_count"] == 0
    assert report["flatten_count"] == 2
    assert report["pct_grid_clean"] == 0.0
    assert report["pct_grid_clean_adjusted"] == 1.0
    assert len(report["clusters"]) == 1
    assert report["clusters"][0]["kind"] == "text_over_fill"
    assert report["text_in_flatten"] == 1


# --- (e) two overlapping text rects over a fill -> hard_overlap --------------------------------


def test_case_e_two_overlapping_text_rects_over_fill_hard_overlap():
    rects = [
        rect("fill", 0, 0, 300, 300, is_text=False, z=0),
        rect("text1", 40, 40, 160, 100, is_text=True, z=1),
        rect("text2", 100, 60, 220, 120, is_text=True, z=2),
    ]
    report = analyze_region(rects)
    assert report["flatten_count"] == 3
    assert report["pct_grid_clean"] == 0.0
    assert report["pct_grid_clean_adjusted"] == 0.0
    assert len(report["clusters"]) == 1
    assert report["clusters"][0]["kind"] == "hard_overlap"
    assert report["text_in_flatten"] == 2


# --- extra coverage: overlaps() helper, epsilon boundary ----------------------------------------


def test_overlaps_helper_respects_epsilon():
    a = rect("a", 0, 0, 100, 100)
    touching = rect("b", 100, 0, 200, 100)  # shares an edge only: zero-width intersection
    assert overlaps(a, touching) is False
    barely = rect("c", 99, 0, 199, 100)  # 1px intersection, <= epsilon(2)
    assert overlaps(a, barely) is False
    real = rect("d", 95, 0, 195, 100)  # 5px intersection, > epsilon
    assert overlaps(a, real) is True


def test_empty_region_is_vacuously_clean():
    report = analyze_region([])
    assert report["pct_grid_clean"] == 1.0
    assert report["pct_grid_clean_adjusted"] == 1.0
    assert report["rect_count"] == 0
    assert report["clusters"] == []


# --- tree-aware wrapper: artboard scoping + background detection -------------------------------


def _layer(id, name, kind, bbox, z, parent=None, visible=True):
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
    )


def test_analyze_layout_tree_excludes_background_and_scopes_by_artboard():
    tree = LayoutTree(
        psd="synthetic.psd",
        path="C:/fake/synthetic.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 300), z=0),
            # Full-bleed background: >=95% of artboard area, bottom of z-order -> excluded.
            _layer(2, "bg", "pixel", (0, 0, 300, 300), z=1, parent=1),
            _layer(3, "tl", "shape", (0, 0, 150, 150), z=2, parent=1),
            _layer(4, "tr", "shape", (150, 0, 300, 150), z=3, parent=1),
            _layer(5, "bl", "shape", (0, 150, 150, 300), z=4, parent=1),
            _layer(6, "br", "shape", (150, 150, 300, 300), z=5, parent=1),
            _layer(7, "hidden", "shape", (10, 10, 20, 20), z=6, parent=1, visible=False),
            _layer(8, "curves adjustment", "adjustment", None, z=7, parent=1),
        ],
    )
    report = analyze_layout_tree(tree)
    assert report["artboard_count"] == 1
    assert len(report["artboards"]) == 1
    ab = report["artboards"][0]
    assert ab["background_layer"]["name"] == "bg"
    assert ab["adjustment_layer_count"] == 1
    # Background + hidden layer excluded; remaining 4 shape rects form a clean 2x2 grid.
    assert ab["rect_count"] == 4
    assert report["pct_grid_clean"] == 1.0
    assert report["grid_clean_count"] == 4
    assert report["adjustment_layer_count"] == 1


def test_aggregate_corpus_rolls_up_multiple_psd_reports():
    tree_a = LayoutTree(
        psd="a.psd",
        path="a.psd",
        canvas=Canvas(width=100, height=100),
        artboards=[],
        layers=[
            _layer(1, "x", "shape", (0, 0, 50, 100), z=0),
            _layer(2, "y", "shape", (50, 0, 100, 100), z=1),
        ],
    )
    tree_b = LayoutTree(
        psd="b.psd",
        path="b.psd",
        canvas=Canvas(width=100, height=100),
        artboards=[],
        layers=[
            _layer(1, "x", "shape", (0, 0, 60, 100), z=0),
            _layer(2, "y", "shape", (40, 0, 100, 100), z=1),  # overlaps x -> hard_overlap cluster
        ],
    )
    report_a = analyze_layout_tree(tree_a)
    report_b = analyze_layout_tree(tree_b)
    corpus = aggregate_corpus([report_a, report_b])
    assert corpus["n_psd"] == 2
    assert corpus["mean_pct_grid_clean"] == (report_a["pct_grid_clean"] + report_b["pct_grid_clean"]) / 2
    assert corpus["total_hard_overlap_clusters"] == report_a["hard_overlap_clusters"] + report_b["hard_overlap_clusters"]
    assert corpus["total_hard_overlap_clusters"] == 1


# --- v2 band-peel classification: _is_band() / _split_bands() ----------------------------------
#
# Real-corpus motivation (Announcement Email PSD): "Rectangle 1" bbox {left:0,top:343,right:640,
# bottom:1795} is full canvas width (640) and ~71% tall -- a section-background panel, not a grid
# cell. v1 treats it as a grid-occupying rect, so it overlaps everything else in its vertical span
# and collapses the whole artboard into one hard_overlap cluster. v2 peels it to a background
# before guillotining.


def test_is_band_full_width_shape_is_peeled():
    band = rect("Rectangle 1", 0, 343, 640, 1795, kind="shape")
    assert _is_band(band, region_width=640) is True


def test_is_band_narrow_image_stays_content():
    logo = rect("logo", 20, 20, 220, 120, kind="pixel")  # width 200 << 0.85 * 640 = 544
    assert _is_band(logo, region_width=640) is False


def test_is_band_text_layer_is_always_content_even_at_full_width():
    # Full-width headline text: still CONTENT -- text is never peeled, regardless of width.
    headline = rect("Full width headline", 0, 100, 640, 160, is_text=True)
    assert _is_band(headline, region_width=640) is False


def test_is_band_threshold_boundary():
    at_threshold = rect("banner", 0, 0, 544, 100, kind="shape")  # 544 / 640 == 0.85 exactly
    assert _is_band(at_threshold, region_width=640) is True
    just_under = rect("not quite banner", 0, 0, 543, 100, kind="shape")
    assert _is_band(just_under, region_width=640) is False


def test_is_band_zero_region_width_is_never_a_band():
    band = rect("Rectangle 1", 0, 0, 640, 100, kind="shape")
    assert _is_band(band, region_width=0) is False


def test_split_bands_partitions_bands_and_content():
    band = rect("footer bar", 0, 0, 640, 100, kind="shape")
    chip = rect("chip", 0, 0, 200, 50, kind="pixel")
    headline = rect("headline", 0, 0, 640, 40, is_text=True)
    bands, content = _split_bands([band, chip, headline], region_width=640)
    assert [r["name"] for r in bands] == ["footer bar"]
    assert {r["name"] for r in content} == {"chip", "headline"}


# --- v2 integration: analyze_layout_tree(..., model=...) ---------------------------------------


def _banded_tree() -> LayoutTree:
    """One artboard: a full-bleed non-text 'Section band' (640/640 = 100% width) that fully
    contains 3 non-overlapping content chips tiling its width. Under v1 the band overlaps all 3
    chips and no guillotine cut is possible (the band spans the interior at every candidate cut
    line) -> single hard_overlap cluster, pct_grid_clean == 0.0. Under v2 the band is peeled and
    the 3 chips guillotine cleanly on their own -> content_pct_grid_clean == 1.0.
    """
    return LayoutTree(
        psd="banded.psd",
        path="C:/fake/banded.psd",
        canvas=Canvas(width=640, height=800),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 640, 800), z=0),
            _layer(2, "Section band", "shape", (0, 300, 640, 500), z=1, parent=1),
            _layer(3, "chip A", "shape", (0, 300, 200, 400), z=2, parent=1),
            _layer(4, "chip B", "shape", (200, 300, 400, 400), z=3, parent=1),
            _layer(5, "chip C", "shape", (400, 300, 640, 400), z=4, parent=1),
        ],
    )


def test_analyze_layout_tree_v2_peels_band_that_collapses_v1_to_hard_overlap():
    report = analyze_layout_tree(_banded_tree())  # default model="both"
    ab = report["artboards"][0]

    # v1 (unchanged): the full-bleed band blocks every guillotine cut in its vertical span.
    assert ab["pct_grid_clean"] == 0.0
    assert ab["flatten_count"] == 4
    assert len(ab["clusters"]) == 1
    assert ab["clusters"][0]["kind"] == "hard_overlap"

    # v2: band peeled to a background; the 3 remaining content chips guillotine cleanly.
    v2 = ab["v2"]
    assert v2["n_bands_peeled"] == 1
    assert v2["bands_peeled"] == [{"name": "Section band", "bbox": {"left": 0, "top": 300, "right": 640, "bottom": 500}}]
    assert v2["content_rect_count"] == 3
    assert v2["content_grid_clean_count"] == 3
    assert v2["content_pct_grid_clean"] == 1.0
    assert v2["content_pct_grid_clean_adjusted"] == 1.0
    assert v2["content_hard_overlap_clusters"] == 0

    # psd-level v2 rollup agrees (single artboard).
    assert report["v2"]["n_bands_peeled"] == 1
    assert report["v2"]["content_rect_count"] == 3
    assert report["v2"]["content_pct_grid_clean"] == 1.0


def test_analyze_layout_tree_model_v1_omits_v2_key():
    report = analyze_layout_tree(_banded_tree(), model="v1")
    assert "v2" not in report
    assert report["artboards"][0].get("v2") is None
    assert "pct_grid_clean" in report  # v1 behavior untouched


def test_analyze_layout_tree_model_v2_omits_v1_top_level_keys():
    report = analyze_layout_tree(_banded_tree(), model="v2")
    assert "pct_grid_clean" not in report
    assert "v2" in report
    assert report["v2"]["n_bands_peeled"] == 1
    assert report["v2"]["content_pct_grid_clean"] == 1.0


def test_analyze_layout_tree_rejects_unknown_model():
    tree = LayoutTree(psd="x.psd", path="x.psd", canvas=Canvas(width=10, height=10), artboards=[], layers=[])
    with pytest.raises(ValueError):
        analyze_layout_tree(tree, model="bogus")


def test_aggregate_corpus_includes_v2_rollup_when_present():
    report = analyze_layout_tree(_banded_tree())
    corpus = aggregate_corpus([report])
    assert corpus["v2"]["n_bands_peeled"] == 1
    assert corpus["v2"]["mean_content_pct_grid_clean"] == 1.0
    assert corpus["v2"]["total_content_hard_overlap_clusters"] == 0
    # v1 rollup (unchanged) still present alongside v2.
    assert corpus["mean_pct_grid_clean"] == 0.0


# --- _find_background z-gate NEGATIVE: a full-bleed layer at the TOP of z-order is NOT peeled ----
# (_find_background is reused by layer_classifier, so a z-gate regression changes product
# classification, not just this analyzer.)


def _bg_layer(id, l, t, r, b, z, kind="pixel"):
    return Layer(id=id, name=f"L{id}", kind=kind, visible=True, opacity=1.0,
                 bbox=BBox(left=l, top=t, right=r, bottom=b), z=z, is_group=False, parent=None)


def test_find_background_ignores_full_bleed_layer_at_top_z():
    from psd_html.grid_analyzer import _find_background

    region_area = 1000 * 1000
    chips = [_bg_layer(1, 0, 0, 100, 100, z=0), _bg_layer(2, 100, 0, 200, 100, z=1),
             _bg_layer(3, 0, 100, 100, 200, z=2)]
    # a >=95%-area non-text layer painted LAST (highest z) -- a hero/scrim, not a background band.
    top_full_bleed = _bg_layer(99, 0, 0, 1000, 1000, z=10)
    assert _find_background(chips + [top_full_bleed], region_area) is None


def test_find_background_peels_full_bleed_layer_at_bottom_z():
    from psd_html.grid_analyzer import _find_background

    region_area = 1000 * 1000
    chips = [_bg_layer(1, 0, 0, 100, 100, z=0), _bg_layer(2, 100, 0, 200, 100, z=1),
             _bg_layer(3, 0, 100, 100, 200, z=2)]
    # positive control: the SAME full-bleed layer at the BOTTOM of z-order IS the background.
    bottom_full_bleed = _bg_layer(99, 0, 0, 1000, 1000, z=-1)
    assert _find_background(chips + [bottom_full_bleed], region_area) is bottom_full_bleed
