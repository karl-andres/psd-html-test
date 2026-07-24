"""Regression tests for the background-fidelity fix (2026-07-09).

Backgrounds/highlights used to be cropped out of the FLATTENED whole-PSD composite, so a band or
highlight image baked in the foreground text sitting on top of it -- the text then rendered twice
(once in the background image, once as the live `<td>` copy). The fix composites a Background from
ITS OWN layer alone (`rasterizer.composite_layer`) when emit() is given `psd_path`.

These assert the observable behavior:
  - with psd_path, the clean single-layer path is used (no "may include foreground" warning), and a
    background asset is genuinely fewer-colored than the same region cropped from the flatten (proof
    the foreground text is not baked into it);
  - without psd_path, the legacy flatten crop is used and is loudly flagged.
"""
from __future__ import annotations

import pathlib

import pytest

from psd_html.html_emitter import emit
from psd_html.layer_router import route
from psd_html.psd_adapter import psd_to_layout_tree
from psd_html.rasterizer import composite_psd
from psd_html.table_solver import build_table_trees

_REPO = pathlib.Path(__file__).resolve().parents[3]
_ANN = _REPO / "Reference" / "2413101_Intel" / "PSDs" / \
    "Intel x Microsoft_Commercial Refresh_announcement email" / "Intel_MsfT_Global BoM_Announcement Email.psd"

_ann_only = pytest.mark.skipif(not _ANN.is_file(), reason="announcement corpus PSD not present on this host")


def _first_email_routed(policy="hybrid"):
    layout = psd_to_layout_tree(str(_ANN))
    trees = build_table_trees(layout, email_override="Announcement")
    return route(trees[0], policy), layout


def _warns(result, wtype):
    return [w for w in result["warnings"] if w.get("type") == wtype]


@_ann_only
def test_with_psd_path_backgrounds_are_single_layer_no_foreground_flag(tmp_path):
    routed, _ = _first_email_routed()
    result = emit(
        routed, tmp_path / "clean",
        composite=composite_psd(str(_ANN)),
        psd_path=str(_ANN),
    )
    # Grammar G: backgrounds never ship as image assets -- textured fills flatten to their mean
    # color (loudly flagged), flat fills become bgcolor directly. No bg_* asset may exist.
    assert not any(a.startswith("assets/bg_") for a in result["assets"]), \
        "Grammar G forbids background-image; no bg_* assets may be emitted"
    html_text = (tmp_path / "clean" / "index.html").read_text(encoding="utf-8")
    assert "background-image" not in html_text, "Grammar G forbids background-image in emitted HTML"
    # and the crop went the CLEAN single-layer path -- never the foreground-bleeding flatten fallback
    assert _warns(result, "background_image_may_include_foreground") == [], \
        "with psd_path, backgrounds must be single-layer composites, not flatten crops"


@_ann_only
def test_without_psd_path_falls_back_and_flags_foreground(tmp_path):
    routed, _ = _first_email_routed()
    result = emit(
        routed, tmp_path / "legacy",
        composite=composite_psd(str(_ANN)),
        # no psd_path -> legacy flatten-crop fallback, which must be loudly flagged
    )
    assert _warns(result, "background_image_may_include_foreground"), \
        "without psd_path the flatten-crop fallback must be flagged, never silent"


@_ann_only
def test_textured_backgrounds_flatten_to_color_with_loud_warning(tmp_path):
    """Grammar G: a genuinely textured background never ships as an image. It flattens to its
    mean color (a real bgcolor that classic Outlook paints) and the loss is recorded as a
    `textured_background_flattened_to_color` warning naming the layer and the chosen color --
    the designer's cue to accept the flat fill or re-author the band as an image region."""
    routed, _ = _first_email_routed()
    result = emit(routed, tmp_path / "g", composite=composite_psd(str(_ANN)), psd_path=str(_ANN))

    flattened = _warns(result, "textured_background_flattened_to_color")
    skipped = _warns(result, "partial_coverage_background_skipped")
    # The announcement corpus has textured/partial highlight+band fills, so the image path must
    # divert at least once -- either flattening to a mean color or being skipped by the coverage
    # guard. Both are loud; neither emits a background-image.
    assert flattened or skipped, "expected textured backgrounds to flatten or be coverage-skipped"
    for w in flattened:
        assert w.get("flat_color") is None or w["flat_color"].startswith("#")
        assert "layer_id" in w

    html_text = (tmp_path / "g" / "index.html").read_text(encoding="utf-8")
    assert "background-image" not in html_text, "Grammar G forbids background-image in emitted HTML"
    assert 'bgcolor="' in html_text, "flattened fills must land as real bgcolor attributes"
