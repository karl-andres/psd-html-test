"""Tests for the per-region fidelity gate.

The load-bearing assertion is TAMPER-BASED: emit a clean corpus bundle, break it deliberately
(delete a raster region, inflate a text region), and assert the gate flags each defect class
unprompted. This replaced the original pin against the then-defective build once those defects
were fixed -- a gate's teeth are proven by controlled sabotage, never by hoping the product
stays broken, and never by loosening the gate to make red turn green.
"""
from __future__ import annotations

import pathlib

import pytest

from psd_html.fidelity_gate import gate_bundle
from psd_html.html_emitter import emit
from psd_html.layer_router import route
from psd_html.psd_adapter import psd_to_layout_tree
from psd_html.rasterizer import composite_psd
from psd_html.table_solver import build_table_trees

_REPO = pathlib.Path(__file__).resolve().parents[3]
_ANN = _REPO / "Reference" / "2413101_Intel" / "PSDs" / \
    "Intel x Microsoft_Commercial Refresh_announcement email" / "Intel_MsfT_Global BoM_Announcement Email.psd"
_ann_only = pytest.mark.skipif(not _ANN.is_file(), reason="announcement corpus PSD not present on this host")


def _emit_announcement(tmp_path):
    layout = psd_to_layout_tree(str(_ANN))
    tree = build_table_trees(layout, email_override="Announcement")[0]
    routed = route(tree, "hybrid")
    out = tmp_path / "bundle"
    emit(routed, out, composite=composite_psd(str(_ANN)),
         layer_names={l.id: l.name for l in layout.layers}, psd_path=str(_ANN))
    return out


def test_gate_unavailable_on_missing_bundle(tmp_path):
    report = gate_bundle(str(_ANN) if _ANN.is_file() else "x.psd", tmp_path / "nope")
    assert report.get("available") is False


# SAFETY: the string concatenation below builds REGEX patterns over local HTML test fixtures
# (re.escape'd region ids) -- there is no SQL and no untrusted input anywhere in this test.
@_ann_only
def test_gate_flags_tampered_defect_classes(tmp_path):
    """TAMPER-BASED gate proof (replaces the original pin against the then-defective build --
    those defects were fixed, so the gate's teeth are now proven by BREAKING a clean bundle
    deliberately and asserting each defect class is caught, unprompted). Never prove a gate by
    hoping the product stays broken."""
    import json
    import re

    bundle = _emit_announcement(tmp_path)
    html_path = bundle / "index.html"
    html_text = html_path.read_text(encoding="utf-8")
    regions = json.loads((bundle / "regions.json").read_text(encoding="utf-8"))

    # Tamper 1 -- DELETE a raster region's <img> entirely (dropped-content class): the region
    # loses its DOM anchor or renders blank; the gate must object either way.
    img_regions = [r for r in regions if r.get("render") == "raster" and r.get("rect")]
    assert img_regions, "corpus bundle should carry at least one raster region"
    victim = img_regions[0]["region_id"]
    tampered = re.sub(
        r'<td[^>]*data-region="' + re.escape(victim) + r'"[^>]*>.*?</td>',
        '<td style="font-size:0;line-height:0;">&nbsp;</td>',
        html_text, count=1, flags=re.DOTALL)
    assert tampered != html_text, "tamper 1 failed to apply"

    # Tamper 2 -- stuff a live text region with copy far beyond its design box (an inflated
    # font shrinks BOTH sides of the wrap ratio and slips through; extra copy cannot).
    text_regions = [r for r in regions if r.get("render") == "live" and r.get("rect")]
    if text_regions:
        tid = text_regions[0]["region_id"]
        stuffing = " overflow" * 120
        m = re.search(r'<td[^>]*data-region="' + re.escape(tid) + r'"[^>]*>', tampered)
        if m:
            tampered = tampered[:m.end()] + stuffing + tampered[m.end():]

    html_path.write_text(tampered, encoding="utf-8")

    report = gate_bundle(str(_ANN), bundle)
    assert report["available"], report.get("reason")
    checks = {f["check"] for f in report["findings"]}

    # The deleted raster region must surface as one of the dropped-content classes.
    dropped_classes = {"region_missing_in_dom", "raster_blank", "raster_mismatch", "band_blank", "band_mismatch"}
    assert checks & dropped_classes, f"gate missed the deleted raster region entirely: {checks}"
    # The inflated text must surface as wrap/clip/drift (any of the text-metric classes).
    if text_regions:
        text_classes = {"text_wraps_beyond_design", "text_clipped", "layout_drift", "page_height_bloat"}
        assert checks & text_classes, f"gate missed the inflated text region: {checks}"
    # A tampered build must never quietly pass, and the report artifacts must land.
    assert report["pass"] is False
    assert (bundle / "gate_report.json").is_file()
    assert (bundle / "gate_render.png").is_file()


@_ann_only
def test_gate_regions_all_locatable_in_dom(tmp_path):
    """Every region with a rect must be findable via its data-region anchor -- if this fails the
    emitter's anchor contract broke, and the gate would be measuring nothing."""
    bundle = _emit_announcement(tmp_path)
    report = gate_bundle(str(_ANN), bundle)
    missing = [f for f in report["findings"] if f["check"] == "region_missing_in_dom"]
    assert missing == [], f"regions lost their DOM anchors: {[f['region'] for f in missing]}"


def test_gate_flags_offset_live_text_that_wraps(tmp_path, monkeypatch):
    """FIX-1 REGRESSION -- the padding-top double-subtraction fail-open.

    A live-text region carries its intra-row offset as `padding-top` on the measured <td>, so the
    injected JS hands the Python side `scroll_h = el.scrollHeight - padTop` (padTop ALREADY removed).
    The defective code then subtracted the FULL vertical padding again (`pad_v = padTop + padBottom`),
    dropping padTop a SECOND time: a region that genuinely wraps to two lines under-counted to one and
    PASSED the gate (real overflow shipped). We drive `gate_bundle` with synthetic DOM metrics (no
    chromium, no corpus PSD) so the line-fit arithmetic is exercised deterministically.

    Geometry: a box the emitter tagged design_lines=1, padTop=20, padBottom=0, lh=20, and content
    that truly fills two 20px line boxes -> el.scrollHeight=60 -> scroll_h=40.
      * post-fix subtracts padBottom only:  round((40 - 0)/20)  = 2 lines -> text_wraps flagged.
      * pre-fix subtracted pad_v (=padTop):  round((40 - 20)/20) = 1 line  -> no finding (fail-open).
    The fixture supplies BOTH `pad_v` and `pad_bottom` (the same td described two ways) so it fails
    against the old field and passes against the new one."""
    import json

    from PIL import Image

    import psd_html.fidelity_gate as fg

    rid = "offset-wrap"
    lh, pad_top, scroll_h = 20.0, 20.0, 40.0
    regions = [{
        "region_id": rid, "role": "body", "render": "live",
        "rect": {"left": 0, "top": 0, "right": 200, "bottom": 40},
        "design_lines": 1,
    }]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("<table></table>", encoding="utf-8")
    (bundle / "regions.json").write_text(json.dumps(regions), encoding="utf-8")

    truth = Image.new("RGB", (200, 40), (255, 255, 255))
    monkeypatch.setattr("psd_html.rasterizer.composite_psd", lambda _p: truth)

    def _fake_render(index_path, shot_path, viewport_width):
        Image.new("RGB", (max(1, viewport_width), 100), (255, 255, 255)).save(shot_path)
        return {
            "table_box": {"x": 0.0, "y": 0.0, "width": 200.0, "height": 40.0},
            "regions": {rid: {
                "x": 0.0, "y": pad_top, "w": 200.0, "h": 40.0,
                "raw_y": 0.0, "raw_h": 40.0 + pad_top,
                "scroll_w": 200, "client_w": 200,
                "scroll_h": scroll_h, "line_height": lh,
                "pad_v": pad_top, "pad_bottom": 0.0,  # same td, both ways (padTop+padBottom vs padBottom)
            }},
        }

    monkeypatch.setattr(fg, "_render_page", _fake_render)

    report = gate_bundle("ignored.psd", bundle)
    assert report["available"], report.get("reason")
    wraps = [f for f in report["findings"] if f["check"] == "text_wraps_beyond_design"]
    assert wraps, (
        "gate missed a genuinely-wrapping offset region -- the padding-top double-subtraction "
        f"fail-open regressed: {[f['check'] for f in report['findings']]}")
    assert wraps[0]["measured"] == 2 and wraps[0]["threshold"] == 1
    assert report["pass"] is False


# --- _fill_delta / _mode_color: the flagship "dominant FILL, robust to glyphs" metric (idx 15) --
# Unit-level (no gate_bundle needed). This is the metric the module was rebuilt around: a plain
# mean-pixel delta mis-failed correct text (glyph antialiasing) and would have MISSED a wrong fill.


def test_fill_delta_is_zero_for_identical_fills():
    from PIL import Image

    from psd_html.fidelity_gate import TEXT_BG_DELTA_MAX, _fill_delta

    white_a = Image.new("RGB", (100, 100), (255, 255, 255))
    white_b = Image.new("RGB", (100, 100), (255, 255, 255))
    assert _fill_delta(white_a, white_b) < TEXT_BG_DELTA_MAX
    assert _fill_delta(white_a, white_b) == pytest.approx(0.0, abs=0.5)


def test_fill_delta_ignores_minority_glyph_pixels():
    # A mostly-white region with a black text band (a heading) must still read as a WHITE fill --
    # this is exactly what a mean-pixel delta got wrong (measuring antialiasing, not background).
    from PIL import Image, ImageDraw

    from psd_html.fidelity_gate import TEXT_BG_DELTA_MAX, _fill_delta

    glyphy = Image.new("RGB", (100, 100), (255, 255, 255))
    ImageDraw.Draw(glyphy).rectangle([0, 0, 99, 14], fill=(0, 0, 0))  # ~15% black "text"
    pure_white = Image.new("RGB", (100, 100), (255, 255, 255))
    assert _fill_delta(glyphy, pure_white) < TEXT_BG_DELTA_MAX  # dominant fill is still white


def test_fill_delta_flags_a_genuinely_wrong_fill():
    from PIL import Image

    from psd_html.fidelity_gate import TEXT_BG_DELTA_MAX, _fill_delta

    white = Image.new("RGB", (100, 100), (255, 255, 255))
    gray_band = Image.new("RGB", (100, 100), (128, 128, 128))  # a wrong/missing-fill band
    assert _fill_delta(white, gray_band) > TEXT_BG_DELTA_MAX


# --- TIER 2 band scan: the +/-16px shift-tolerance loop (idx 16, fidelity_gate.py:333) --------
# Corpus-free: drive gate_bundle with monkeypatched _render_page/composite_psd (same pattern as
# test_gate_flags_offset_live_text_that_wraps) so the band-scan arithmetic is exercised
# deterministically, with no chromium and no corpus PSD required.


def test_gate_band_scan_tolerates_small_vertical_shift(tmp_path, monkeypatch):
    """FIX-REGRESSION guard -- the +/-16px band shift-tolerance loop (lines 344-350).

    TIER 2's band scan slices the page into strict 48px-tall horizontal strips. Chromium's
    strut-exact line boxes routinely land real content a handful of pixels below (or above)
    where the PSD-exact design put it -- live-caught case: a footnote's first line sitting at
    +11px, sliced exactly at a band boundary, read as "blank" in its home band. Without the
    shift search, ANY content that merely slid (not dropped) would spuriously fail as
    `band_blank`. Here the SAME thin busy band sits at truth rows 44-47 and render rows 56-59 --
    a clean +12px slide -- so the first band (offset 0) reads truth-busy/render-blank verbatim,
    and only the shift-tolerance loop (checking offset+12) finds the content still there and
    holds back the finding."""
    import json

    from PIL import Image, ImageDraw

    import psd_html.fidelity_gate as fg

    rid = "hero-band"
    rect = {"left": 0, "top": 0, "right": 200, "bottom": 100}
    regions = [{"region_id": rid, "role": "hero", "render": "raster", "rect": rect}]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("<table></table>", encoding="utf-8")
    (bundle / "regions.json").write_text(json.dumps(regions), encoding="utf-8")

    truth = Image.new("RGB", (200, 100), (255, 255, 255))
    ImageDraw.Draw(truth).rectangle([0, 44, 199, 47], fill=(0, 0, 0))  # busy band at y=44..47
    monkeypatch.setattr("psd_html.rasterizer.composite_psd", lambda _p: truth)

    def _fake_render(index_path, shot_path, viewport_width):
        render = Image.new("RGB", (200, 100), (255, 255, 255))
        # the SAME band, slid down 12px -- content merely moved, nothing was dropped.
        ImageDraw.Draw(render).rectangle([0, 56, 199, 59], fill=(0, 0, 0))
        render.save(shot_path)
        return {
            "table_box": {"x": 0.0, "y": 0.0, "width": 200.0, "height": 100.0},
            "regions": {rid: {"x": 0.0, "y": 0.0, "w": 200.0, "h": 100.0, "raw_y": 0.0, "raw_h": 100.0}},
        }

    monkeypatch.setattr(fg, "_render_page", _fake_render)

    report = fg.gate_bundle("ignored.psd", bundle)
    assert report["available"], report.get("reason")
    checks = {f["check"] for f in report["findings"]}
    assert "band_blank" not in checks, (
        "a merely-shifted (not dropped) band false-failed band_blank -- the +/-16px shift "
        f"tolerance loop regressed: {report['findings']}")


# --- TIER 1 raster regions: the `delta > threshold` guard on raster_blank (idx 16, :333) -------


def test_gate_raster_matches_flat_render_despite_busy_truth_std(tmp_path, monkeypatch):
    """FIX-REGRESSION guard -- the `delta > threshold` guard on the raster_blank branch
    (lines 254-255).

    Divider rules routinely ship as a flat CSS color-fill <td> (Word's image top-clip ghosts the
    thin img form), while the PSD truth crop for the same divider is a two-tone dithered strip
    whose per-pixel variance reads as "busy" even though its OVERALL mean color is identical to
    the render's flat fill. `_std` alone would call this truth-busy/render-blank -- the classic
    dropped-content signature -- but the mean colors match exactly, so it is not a defect. Only
    the `delta > threshold` conjunct on the raster_blank branch tells them apart; drop it and this
    flat, correctly-matching divider td false-fails."""
    import json

    from PIL import Image, ImageDraw

    import psd_html.fidelity_gate as fg

    rid = "divider-1"
    rect = {"left": 0, "top": 0, "right": 200, "bottom": 20}
    regions = [{"region_id": rid, "role": "divider", "render": "raster", "rect": rect}]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("<table></table>", encoding="utf-8")
    (bundle / "regions.json").write_text(json.dumps(regions), encoding="utf-8")

    # TRUTH: a two-tone dithered strip, mean (160+200)/2 = 180, std = 20 (> BAND_BUSY_STD=18).
    truth = Image.new("RGB", (200, 20), (160, 160, 160))
    ImageDraw.Draw(truth).rectangle([0, 10, 199, 19], fill=(200, 200, 200))
    monkeypatch.setattr("psd_html.rasterizer.composite_psd", lambda _p: truth)

    def _fake_render(index_path, shot_path, viewport_width):
        # RENDER: the SAME divider shipped as a flat CSS fill at the truth's own mean -- std=0
        # (< BAND_BLANK_STD=6), but the color matches exactly (delta ~0).
        render = Image.new("RGB", (200, 20), (180, 180, 180))
        render.save(shot_path)
        return {
            "table_box": {"x": 0.0, "y": 0.0, "width": 200.0, "height": 20.0},
            "regions": {rid: {"x": 0.0, "y": 0.0, "w": 200.0, "h": 20.0, "raw_y": 0.0, "raw_h": 20.0}},
        }

    monkeypatch.setattr(fg, "_render_page", _fake_render)

    report = fg.gate_bundle("ignored.psd", bundle)
    assert report["available"], report.get("reason")
    checks = {f["check"] for f in report["findings"]}
    assert "raster_blank" not in checks, (
        "a flat color-fill divider td (mean-matching a busy-std truth crop) false-failed "
        f"raster_blank -- the delta > threshold guard regressed: {report['findings']}")
    assert "raster_mismatch" not in checks
