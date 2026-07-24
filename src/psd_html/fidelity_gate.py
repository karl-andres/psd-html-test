"""The per-region visual-fidelity comparator.

Why this exists: the whole-image oracle (oracle.py) reduces fidelity to ONE number, so a localized
defect (an invented background band, a wrapped stat line, a blank hero) barely moves the score and
sails through. This gate compares TRUTH vs RENDER **per region**, with role-aware rules, and
reports every violation by region id -- so a human never has to hand-QA what a machine can catch.

Two tiers:

  TIER 1 -- per-region (from `regions.json`, which carries each leaf's rect + role + render mode,
  and the `data-region` DOM anchor the emitter stamps on each leaf <td>):
    - raster regions ("render": "raster"): the rendered pixels must MATCH the PSD-composite crop
      (they came from the same pixels, so the bar is strict): mean-color delta + busy-vs-blank.
    - live text regions: structural fidelity, not pixel identity (Word-safe fonts never
      pixel-match a PSD):
        * line count: rendered line count (scrollHeight / line-height, via the DOM) must not
          exceed what the design box implies (rect height / line-height) -- catches single-line
          text wrapping to 2-3 lines.
        * clip: scrollWidth must not exceed clientWidth -- catches horizontal clipping.
        * background: the mean color behind the render must be near the truth crop's mean --
          catches an INVENTED background (blue band behind copy the design shows on white) and a
          MISSING one.
    - every region: layout drift -- the DOM box's position vs the emitted rect.

  TIER 2 -- full-page band scan (needs no region model, so it catches what regions.json cannot
  see, e.g. a dropped cell-less background band like a hero image): slice the truth composite and
  the rendered page into horizontal strips and compare each strip's mean color + busyness --
  "truth busy / render blank" is exactly a missing band; strip color inversion is an invented one.

Usage:
    from psd_html.fidelity_gate import gate_bundle
    report = gate_bundle(psd_path, bundle_dir, density=1.0)
    # report = {"pass": bool, "findings": [...], "counts": {...}, "screenshot": path}

Loud-but-safe: if chromium is unavailable the gate returns {"available": False, ...} rather than
raising -- but callers should treat that as NOT-verified, never as a pass.

SAFETY: every f-string in this module builds a human-readable finding/report MESSAGE. There is no
SQL, shell, or other interpreter sink anywhere here -- pure image/DOM measurement. (Noted for the
repo security-reminder hook, which pattern-matches f-strings.)
"""

from __future__ import annotations

import json
from pathlib import Path

# --- calibratable thresholds (v1 -- tuned on the announcement build; widen only with evidence) ---

# TIER 1: raster regions. A pixel-CROP region (image/graphic) ships the design's own pixels, so
# the bar is strict; a RE-TYPESET brand headline is a re-render (deliberately not a crop -- late
# copy binding), so glyph rasterization differences are expected and the bar is looser -- it still
# catches clipped/blank/mis-colored headlines.
RASTER_MEAN_DELTA_MAX = 48.0  # mean |truth-render| per channel, 0-255 scale (pixel crops)
RETYPESET_MEAN_DELTA_MAX = 95.0  # re-typeset brand headlines
# TIER 1: live text -- structural.
TEXT_BG_DELTA_MAX = 15.0  # max per-channel distance between truth/render DOMINANT fills (_fill_delta)
TEXT_EXTRA_LINES_MAX = 0  # rendered lines may not exceed design lines by more than this
LAYOUT_DRIFT_MAX = 40  # px -- DOM box vs emitted rect (vertical drift accumulates with reflow)
# TIER 2: band scan.
BAND_HEIGHT = 48  # px per strip
BAND_MEAN_DELTA_MAX = 70.0  # mean-color distance per strip
BAND_BUSY_STD = 18.0  # a strip with pixel-std above this is "busy" (has real content)
BAND_BLANK_STD = 6.0  # a strip with pixel-std below this is "blank"
PAGE_HEIGHT_RATIO_MAX = 1.25  # rendered page may not be taller than truth by more than this


def _mean_color_delta(a, b) -> float:
    """Mean per-channel absolute difference between two same-size RGB images (0-255)."""
    import numpy as np

    x = np.asarray(a, dtype="float32")
    y = np.asarray(b, dtype="float32")
    return float(np.mean(np.abs(x - y)))


def _mode_color(img):
    """The dominant (mode) color of an RGB image, quantized to 16 levels/channel -- the FILL of a
    region, robust to a minority of foreground glyph pixels."""
    import numpy as np

    arr = np.asarray(img.convert("RGB"), dtype="int32").reshape(-1, 3)
    q = arr // 16
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    mode = int(vals[int(counts.argmax())])
    members = arr[keys == mode]
    return members.mean(axis=0)


def _fill_delta(a, b) -> float:
    """Distance between two regions' DOMINANT fill colors (max per-channel difference). This is
    the right metric for "invented or missing fill": a mean-pixel delta over a glyph-heavy region
    (a 24px-tall semibold heading) measures antialiasing and subpixel shift, not the background,
    and mis-failed correct text at delta 61-71 while a missing #F2F2F2 band (mean delta ~13 vs
    white) would have sailed under any glyph-tolerant threshold."""
    import numpy as np

    return float(np.max(np.abs(_mode_color(a) - _mode_color(b))))


def _std(img) -> float:
    import numpy as np

    return float(np.asarray(img.convert("L"), dtype="float32").std())


def _crop(img, box):
    w, h = img.size
    l = max(0, min(int(box[0]), w))
    t = max(0, min(int(box[1]), h))
    r = max(l + 1, min(int(box[2]), w))
    b = max(t + 1, min(int(box[3]), h))
    return img.crop((l, t, r, b))


def _finding(check: str, severity: str, note: str, **extra) -> dict:
    out = {"check": check, "severity": severity, "note": note}
    out.update(extra)
    return out


def _render_page(index_path: Path, shot_path: Path, viewport_width: int) -> dict:
    """Render the bundle in headless chromium; screenshot the full page and measure the outer
    table plus every [data-region] element. On success returns {table_box, regions: {id: {x,y,w,h,
    scroll_w, client_w, scroll_h, line_height, pad_bottom}}}; on failure returns {"error": <reason>}
    naming exactly what went wrong (mirrors oracle.py, which preserves repr(exc) so a real bug in
    the QA render is never indistinguishable from a benign missing-dependency environment)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"error": f"playwright import failed: {exc!r}"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": viewport_width + 80, "height": 1200})
            page.goto(index_path.resolve().as_uri())
            page.wait_for_timeout(400)
            table = page.query_selector("table")
            if table is None:
                browser.close()
                return {"error": "no <table> element in the rendered bundle"}
            table_box = table.bounding_box()
            metrics = page.evaluate(
                """() => {
                    const out = {};
                    for (const el of document.querySelectorAll('[data-region]')) {
                        const r = el.getBoundingClientRect();
                        const cs = getComputedStyle(el);
                        const lh = parseFloat(cs.lineHeight);
                        // y is the CONTENT-BOX top: the emitter's intra-row vertical offset
                        // rides as padding-top on the td, so the border-box top sits at the ROW
                        // top while the design rect describes where the content starts.
                        const padTop = parseFloat(cs.paddingTop) || 0;
                        out[el.getAttribute('data-region')] = {
                            x: r.x, y: r.y + padTop, w: r.width, h: r.height - padTop,
                            raw_y: r.y, raw_h: r.height,
                            scroll_w: el.scrollWidth, client_w: el.clientWidth,
                            scroll_h: el.scrollHeight - padTop,
                            line_height: isNaN(lh) ? null : lh,
                            // scroll_h ABOVE already excludes padTop, so the only padding still
                            // inside it is padBottom. Expose padBottom ALONE (not padTop+padBottom)
                            // so the Python line-fit removes padding exactly once, never twice.
                            pad_bottom: (parseFloat(cs.paddingBottom) || 0),
                        };
                    }
                    return out;
                }"""
            )
            page.screenshot(path=str(shot_path), full_page=True)
            browser.close()
        return {"table_box": table_box, "regions": metrics}
    except Exception as exc:
        return {"error": repr(exc)}


def gate_bundle(psd_path: str, bundle_dir, *, density: float = 1.0) -> dict:
    """Run the two-tier fidelity gate for one emitted bundle against its source PSD."""
    from PIL import Image

    from .rasterizer import composite_psd

    bundle = Path(bundle_dir)
    index_path = bundle / "index.html"
    regions_path = bundle / "regions.json"
    if not index_path.is_file() or not regions_path.is_file():
        return {"available": False, "reason": "bundle missing index.html or regions.json"}

    regions = json.loads(regions_path.read_text(encoding="utf-8"))
    rects = [r["rect"] for r in regions if r.get("rect")]
    if not rects:
        return {"available": False, "reason": "regions.json carries no rects (re-emit with the current emitter)"}

    truth_full = composite_psd(psd_path)
    if isinstance(truth_full, dict):
        return {"available": False, "reason": f"PSD composite unavailable: {truth_full.get('reason')}"}
    truth_rgb = truth_full.convert("RGB")

    # Coordinate frames: regions.json rects are CSS-space, absolute to the PSD canvas (the tree was
    # density-scaled at emit); the truth frame is those same rects * density. The emitted stack
    # starts at canvas (0,0), so canvas CSS coords map into the screenshot at the table's origin.
    left = min(r["left"] for r in rects)
    top = min(r["top"] for r in rects)
    right = max(r["right"] for r in rects)
    bottom = max(r["bottom"] for r in rects)
    css_w = int(right - left)

    shot_path = bundle / "gate_render.png"
    rendered = _render_page(index_path, shot_path, css_w + int(left))
    if rendered.get("error"):
        return {"available": False, "reason": f"chromium render unavailable: {rendered['error']}"}

    shot = Image.open(shot_path).convert("RGB")
    table_box = rendered["table_box"]
    findings: list = []

    # --- TIER 1: per-region ----------------------------------------------------------------------
    dom = rendered["regions"]
    for reg in regions:
        rid = reg["region_id"]
        rect = reg.get("rect")
        if not rect:
            continue
        role = reg.get("role")
        render_mode = reg.get("render")
        m = dom.get(rid)
        if m is None:
            findings.append(_finding(
                "region_missing_in_dom", "fail",
                f"region {rid} ({role}) has no rendered element", region=rid, role=role))
            continue

        # truth crop (full-res canvas space), normalized to the rendered crop's size for deltas
        tbox = tuple(v * density for v in (rect["left"], rect["top"], rect["right"], rect["bottom"]))
        truth = _crop(truth_rgb, tbox)
        render_crop = _crop(shot, (m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"]))
        truth_n = truth.resize(render_crop.size) if truth.size != render_crop.size else truth

        # layout drift: where the element actually sits vs where the design put it. BOX
        # regions compare at the BORDER box: their padding is interior design placement (the
        # text's offsets inside the shape), not the intra-row offset the content-box
        # adjustment above was built for.
        expected_y = table_box["y"] + rect["top"]
        _meas_y = m.get("raw_y", m["y"]) if reg.get("box") else m["y"]
        drift_y = abs(_meas_y - expected_y)
        if drift_y > LAYOUT_DRIFT_MAX:
            findings.append(_finding(
                "layout_drift", "warn",
                f"region {rid} sits {drift_y:.0f}px from its design position (reflow accumulates)",
                region=rid, role=role, measured=round(drift_y, 1), threshold=LAYOUT_DRIFT_MAX))

        if render_mode == "raster":
            delta = _mean_color_delta(truth_n, render_crop)
            threshold = RETYPESET_MEAN_DELTA_MAX if reg.get("brand_font_rasterized") else RASTER_MEAN_DELTA_MAX
            if _std(truth_n) > BAND_BUSY_STD and _std(render_crop) < BAND_BLANK_STD \
                    and delta > threshold:
                # A flat render is only BLANK if it is also the WRONG color: divider rules
                # deliberately ship as flat color-fill tds (Word's image top-clip ghosted the
                # 5px img form) and match the design crop's mean exactly.
                findings.append(_finding(
                    "raster_blank", "fail",
                    f"raster region {rid} renders blank but the design has content there",
                    region=rid, role=role))
            elif delta > threshold:
                findings.append(_finding(
                    "raster_mismatch", "fail",
                    f"raster region {rid} differs from the design crop (mean delta {delta:.0f})",
                    region=rid, role=role, measured=round(delta, 1), threshold=threshold))
        else:
            # live text: structural checks
            lh = m.get("line_height") or 0
            if lh > 0:
                # The emitter records its ink-model line count (real leading + SpaceAfter) in
                # regions.json -- exact for Photoshop's ink-tight boxes. The floor-division form
                # below is only a fallback for bundles emitted before design_lines existed; it
                # UNDER-counts every ink-tight box (a 44px box at 24px leading holds 2 lines:
                # 24 + 20px last-line ink; 44//24 says 1) and mis-fails correct renders.
                design_lines = int(reg.get("design_lines") or 0) or max(
                    1, round(rect["bottom"] - rect["top"]) // max(1, round(lh)))
                # PADDING REMOVED EXACTLY ONCE: the JS scroll_h is (el.scrollHeight - padTop), so
                # padTop is already gone -- subtract ONLY padBottom here. Subtracting the full
                # vertical padding (padTop + padBottom) would drop padTop a SECOND time, under-count
                # the height, and let a region that genuinely wraps pass the gate (a fail-open).
                pad_bottom = m.get("pad_bottom") or 0
                # Paragraph SpaceAfter renders as real <p> margins INSIDE scroll_h; without
                # subtracting it, 3 bullet lines + 2x8.5px gaps reads as a phantom 4th line.
                space_after = float(reg.get("space_after_px") or 0.0)
                rendered_lines = max(1, round((m["scroll_h"] - pad_bottom - space_after) / lh))
                # BOX regions: the td is the fill SHAPE, deliberately taller than the text's
                # line boxes (probe-certified construct) -- height/lh is not a line count there.
                if rendered_lines > design_lines + TEXT_EXTRA_LINES_MAX and not reg.get("box"):
                    findings.append(_finding(
                        "text_wraps_beyond_design", "fail",
                        f"live text {rid} renders {rendered_lines} line(s); the design box holds {design_lines}",
                        region=rid, role=role, measured=rendered_lines, threshold=design_lines))
            if m["scroll_w"] > m["client_w"] + 1:
                findings.append(_finding(
                    "text_clipped", "fail",
                    f"live text {rid} overflows its box horizontally ({m['scroll_w']}px in {m['client_w']}px)",
                    region=rid, role=role, measured=m["scroll_w"], threshold=m["client_w"]))
            bg_delta = _fill_delta(truth_n, render_crop)
            if reg.get("span_highlight"):
                bg_delta = 0.0  # partial fill by design-intent (bracket-excluded token spans)
            if bg_delta > TEXT_BG_DELTA_MAX:
                findings.append(_finding(
                    "region_background_mismatch", "fail",
                    f"region {rid} renders on a different background than the design (mean delta {bg_delta:.0f}) "
                    "-- an invented or missing fill",
                    region=rid, role=role, measured=round(bg_delta, 1), threshold=TEXT_BG_DELTA_MAX))

    # --- TIER 2: full-page band scan --------------------------------------------------------------
    truth_email = _crop(truth_rgb, (left * density, top * density, right * density, bottom * density))
    if density != 1.0:
        truth_email = truth_email.resize((css_w, max(1, int(truth_email.height / density))))
    # Mirror truth_email's x-window: it crops canvas [left, right], so the render must start at the
    # same content-left. table_box["x"] + left maps canvas x=left into the screenshot (the +left
    # mirrors the +top on the vertical origin); else an inset left column compares shifted x-bands.
    render_email = _crop(
        shot, (table_box["x"] + left, table_box["y"] + top, table_box["x"] + left + css_w, table_box["y"] + table_box["height"])
    )

    height_ratio = render_email.height / max(1, truth_email.height)
    if height_ratio > PAGE_HEIGHT_RATIO_MAX:
        findings.append(_finding(
            "page_height_bloat", "warn",
            f"rendered page is {height_ratio:.2f}x the design height (reflow/spacing drift)",
            measured=round(height_ratio, 2), threshold=PAGE_HEIGHT_RATIO_MAX))

    scan_h = min(truth_email.height, render_email.height)
    offset = 0
    while offset + BAND_HEIGHT <= scan_h:
        tband = truth_email.crop((0, offset, css_w, offset + BAND_HEIGHT))
        rband = render_email.crop((0, offset, css_w, offset + BAND_HEIGHT))
        t_std, r_std = _std(tband), _std(rband)
        delta = _mean_color_delta(tband, rband)
        band_y = int(top) + offset
        if t_std > BAND_BUSY_STD and r_std < BAND_BLANK_STD:
            # SHIFT TOLERANCE: this check exists to catch DROPPED content, and Chromium's
            # strut-exact line boxes legitimately land ink a few px below the design's
            # ink-exact boxes (large shifts are layout_drift's job, threshold 40px). Before
            # failing, look for the band's content within +/-16px in the render -- content
            # that merely slid is not a dropped band (live-caught: the footnote first line at
            # +11px sliced exactly at a band boundary read as "blank").
            shifted_found = False
            for dy in (-16, -12, -8, 8, 12, 16):
                y0 = offset + dy
                if y0 < 0 or y0 + BAND_HEIGHT > render_email.height:
                    continue
                if _std(render_email.crop((0, y0, css_w, y0 + BAND_HEIGHT))) >= BAND_BLANK_STD:
                    shifted_found = True
                    break
            if not shifted_found:
                findings.append(_finding(
                    "band_blank", "fail",
                    f"band y={band_y}..{band_y + BAND_HEIGHT}: design has content, render is blank "
                    "(a dropped image/background band)",
                    band_y=band_y, measured=round(r_std, 1)))
        elif delta > BAND_MEAN_DELTA_MAX:
            findings.append(_finding(
                "band_mismatch", "fail",
                f"band y={band_y}..{band_y + BAND_HEIGHT}: rendered colors diverge from the design "
                f"(mean delta {delta:.0f})",
                band_y=band_y, measured=round(delta, 1), threshold=BAND_MEAN_DELTA_MAX))
        offset += BAND_HEIGHT

    fails = [f for f in findings if f["severity"] == "fail"]
    warns = [f for f in findings if f["severity"] == "warn"]
    report = {
        "available": True,
        "pass": not fails,
        "counts": {"fail": len(fails), "warn": len(warns), "regions": len(regions)},
        "findings": findings,
        "screenshot": str(shot_path),
        "note": "browser-proxy gate: catches projection defects; the classic-Outlook read remains the Word-engine truth",
    }
    (bundle / "gate_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
