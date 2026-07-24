"""A headless-Chromium GEOMETRY oracle.

This module renders an emitted `index.html` bundle with real headless Chromium (via `playwright`)
and pixel-diffs the screenshot against the PSD flatten ground truth (a Pillow image from
`rasterizer.composite_psd`, or a sibling proof `.jpg` from the corpus) to produce a
geometry-reconstruction confidence score in `[0, 1]`.

THIS IS EXPLICITLY NOT A WORD-ENGINE / CLASSIC-OUTLOOK RENDER. Chromium's layout engine and the
classic-Outlook Word engine that actually renders the shipped `.OFT` disagree on box model details,
font metrics, and table layout in ways this oracle cannot see. Every result this module returns
carries `note == GEOMETRY_PROXY_NOTE` naming that caveat explicitly -- treat the score as "did the
HTML/table geometry roughly reconstruct the PSD layout", never as "will this look right in Outlook".
The real per-region visual gate is a downstream concern (see `fidelity_gate.py` -- a strict-pixel
bar for raster regions and structural checks for live text); this oracle only ever informs S2
authoring, it is not that gate.

Pipeline (`score_bundle`):
  1. Launch headless Chromium (`playwright.sync_api.sync_playwright`), open `index_path` as a
     `file://` URL (so its sibling `assets/*.png` resolve normally), screenshot the FULL page
     (`full_page=True`) at a small fixed-width viewport, close the browser immediately -- one
     page, one browser, no persistent context.
  2. Load `psd_flatten_image` -- either an already-open `PIL.Image.Image` (typically
     `rasterizer.composite_psd(...)`'s return value) or a path to a flatten/proof image on disk.
  3. Normalize: resize the ground-truth image to the rendered screenshot's width (aspect-preserved)
     and crop both to their common (min) height, so the diff always compares like-for-like pixel
     grids regardless of which source is taller. The score reflects ONLY that common-height overlap
     -- any extra bottom band on the taller image is cropped away and does not affect the score; the
     height mismatch is not hidden but reported separately as `detail["height_mismatch"]`.
  4. Score = `1 - normalized mean absolute pixel delta` (RGB, 0..255 scale) via `numpy`, clamped to
     `[0, 1]`. This is a coarse geometry proxy, not a perceptual metric -- it is cheap, deterministic,
     and enough to catch "the table solver put things in wildly the wrong place".

Loud-but-safe degrade rule (matches every other S2 builder): if Chromium cannot be launched, the
screenshot fails, the ground-truth image cannot be loaded/decoded, or ANY other step raises,
`score_bundle` NEVER raises -- it returns `{"available": False, "score": None, "width": None,
"note": GEOMETRY_PROXY_NOTE, "detail": {"reason": ...}}` naming exactly what went wrong. Only a
genuinely working oracle run returns `{"available": True, "score": <float in [0,1]>, ...}`.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Optional

# --- optional-dependency guards (loud-but-safe: never crash the whole bake-off) -----------------

try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_AVAILABLE = True
    _PLAYWRIGHT_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # pragma: no cover -- playwright+chromium are installed in this environment
    sync_playwright = None
    _PLAYWRIGHT_AVAILABLE = False
    _PLAYWRIGHT_IMPORT_ERROR = _exc

try:
    from PIL import Image

    _PIL_AVAILABLE = True
    _PIL_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # pragma: no cover -- Pillow is installed in this environment
    Image = None
    _PIL_AVAILABLE = False
    _PIL_IMPORT_ERROR = _exc

try:
    import numpy as np

    _NUMPY_AVAILABLE = True
    _NUMPY_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # pragma: no cover -- numpy is installed in this environment
    np = None
    _NUMPY_AVAILABLE = False
    _NUMPY_IMPORT_ERROR = _exc


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

GEOMETRY_PROXY_NOTE = (
    "geometry proxy, NOT a Word-engine render -- headless Chromium approximates table/box layout "
    "only; classic Outlook renders via Word's own engine and WILL differ from this score."
)

# Kept small on purpose ("keep it fast" -- one page, small viewport, close the browser). Height is
# irrelevant beyond a minimal nonzero value: `full_page=True` captures the entire scrollable content
# regardless of the viewport height requested here.
DEFAULT_VIEWPORT_WIDTH = 800
DEFAULT_VIEWPORT_HEIGHT = 100
DEFAULT_TIMEOUT_MS = 15000


class OracleUnavailable(RuntimeError):
    """Internal-only: raised by the render/load helpers when a required backend (Chromium,
    Pillow) is unavailable or fails. Always caught inside `score_bundle` and folded into an
    `{"available": False, ...}` result -- never escapes to a caller."""


# --- rendering --------------------------------------------------------------------------------


def _render_html_to_png(
    index_path: Path, *, viewport_width: int, viewport_height: int, timeout_ms: int
) -> bytes:
    """Screenshot `index_path` (a `file://` URL) with headless Chromium and return PNG bytes.
    Raises `OracleUnavailable` (never anything else) on any failure -- import failure, launch
    failure, navigation failure, or a screenshot that is not a valid PNG."""
    if not _PLAYWRIGHT_AVAILABLE:
        raise OracleUnavailable(f"playwright unavailable: {_PLAYWRIGHT_IMPORT_ERROR!r}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": viewport_width, "height": viewport_height})
                try:
                    page.goto(index_path.resolve().as_uri(), timeout=timeout_ms)
                    png_bytes = page.screenshot(full_page=True)
                finally:
                    page.close()
            finally:
                browser.close()
    except Exception as exc:
        launch_failure_msg = f"chromium render of {index_path!s} failed: {exc!r}"
        raise OracleUnavailable(launch_failure_msg) from exc

    if not png_bytes or not png_bytes.startswith(PNG_MAGIC):
        raise OracleUnavailable(
            f"chromium screenshot of {index_path!s} did not produce valid PNG bytes"
        )
    return png_bytes


def _load_ground_truth(psd_flatten_image: Any) -> "Image.Image":
    """Accepts either an already-open `PIL.Image.Image` (e.g. `rasterizer.composite_psd(...)`'s
    return value) or a path to an image file (the corpus's sibling proof `.jpg`, or any png/jpg).
    Raises `OracleUnavailable` on anything that is not a usable RGB image."""
    if not _PIL_AVAILABLE:
        raise OracleUnavailable(f"Pillow unavailable: {_PIL_IMPORT_ERROR!r}")
    if isinstance(psd_flatten_image, Image.Image):
        return psd_flatten_image.convert("RGB")
    if isinstance(psd_flatten_image, dict):
        # A degraded rasterizer.composite_psd() result ({"available": False, "reason": ...}) --
        # not a usable image, name why rather than letting PIL.Image.open blow up on a dict.
        raise OracleUnavailable(
            f"psd_flatten_image is a degraded composite dict, not an image: {psd_flatten_image!r}"
        )
    try:
        with Image.open(psd_flatten_image) as opened:
            return opened.convert("RGB")
    except Exception as exc:
        load_failure_msg = f"could not load psd_flatten_image {psd_flatten_image!r}: {exc!r}"
        raise OracleUnavailable(load_failure_msg) from exc


# --- scoring ----------------------------------------------------------------------------------


def _mean_abs_pixel_delta(render_rgb: "Image.Image", truth_rgb: "Image.Image") -> float:
    """Mean absolute per-channel pixel delta (0..255 scale) over the two (already same-size)
    RGB images. Uses numpy when available; degrades to a pure-Pillow `ImageChops`/`ImageStat`
    computation (no numpy) otherwise -- either path is exact, numpy is just faster."""
    if _NUMPY_AVAILABLE:
        a = np.asarray(render_rgb, dtype=np.float64)
        b = np.asarray(truth_rgb, dtype=np.float64)
        if a.size == 0:
            return 0.0
        return float(np.abs(a - b).mean())

    from PIL import ImageChops, ImageStat  # part of Pillow, always available alongside Image

    diff = ImageChops.difference(render_rgb, truth_rgb)
    stat = ImageStat.Stat(diff)
    if not stat.mean:
        return 0.0
    return float(sum(stat.mean) / len(stat.mean))


def _normalized_pixel_score(render_img: "Image.Image", truth_img: "Image.Image") -> tuple:
    """Resize `truth_img` to `render_img`'s width (aspect-preserved), crop both to their common
    (min) height, and return `(score, detail)` where `score = clamp(1 - mean_abs_delta/255, 0, 1)`.
    The score is computed ONLY over that common-height overlap: any extra bottom band on the taller
    image is cropped away and does NOT affect the score. A height mismatch is therefore not folded
    into the score itself -- it is reported separately as `detail["height_mismatch"]` (with both
    source heights in `detail`) for the caller to weigh."""
    render_rgb = render_img.convert("RGB")
    truth_rgb = truth_img.convert("RGB")

    target_width = max(1, render_rgb.width)
    if truth_rgb.width != target_width:
        scale = target_width / float(max(1, truth_rgb.width))
        target_height = max(1, round(truth_rgb.height * scale))
        truth_rgb = truth_rgb.resize((target_width, target_height), Image.LANCZOS)

    compared_height = max(1, min(render_rgb.height, truth_rgb.height))
    render_crop = render_rgb.crop((0, 0, target_width, compared_height))
    truth_crop = truth_rgb.crop((0, 0, target_width, compared_height))

    mean_abs_delta = _mean_abs_pixel_delta(render_crop, truth_crop)
    score = max(0.0, min(1.0, 1.0 - (mean_abs_delta / 255.0)))

    detail = {
        "render_width": render_img.width,
        "render_height": render_img.height,
        "ground_truth_width": truth_img.width,
        "ground_truth_height": truth_img.height,
        "compared_width": target_width,
        "compared_height": compared_height,
        "mean_abs_pixel_delta": mean_abs_delta,
        "height_mismatch": render_rgb.height != truth_rgb.height,
    }
    return score, detail


# --- public API --------------------------------------------------------------------------------


def _unavailable(reason: str) -> dict:
    """Build the `available: False` result shape shared by every degrade path in `score_bundle`."""
    return {
        "available": False,
        "score": None,
        "width": None,
        "note": GEOMETRY_PROXY_NOTE,
        "detail": {"reason": reason},
    }


def score_bundle(
    index_path,
    psd_flatten_image,
    *,
    viewport_width: int = DEFAULT_VIEWPORT_WIDTH,
    viewport_height: int = DEFAULT_VIEWPORT_HEIGHT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Render `index_path` with headless Chromium and pixel-diff it against
    `psd_flatten_image` (a `PIL.Image.Image` -- e.g. `rasterizer.composite_psd(...)` -- or a path
    to a flatten/proof image). Call once per emitted template/artboard.

    Returns `{"available": bool, "score": float|None, "width": int|None, "note": str,
    "detail": dict}`. `note` is ALWAYS `GEOMETRY_PROXY_NOTE` -- read it before trusting the score
    for anything beyond "did the HTML geometry roughly land where the PSD had it". NEVER raises:
    any failure (missing file, Chromium unlaunchable, ground truth unloadable, ...) is reported as
    `available: False` with `detail: {"reason": ...}` instead.

    `viewport_width` matters for comparability: Chromium's full-page screenshot is always exactly
    `viewport_width` pixels wide (extra page width beyond the table's own width shows up as plain
    background, not cropped away), so pass the bundle's OWN authored width (e.g. the `TableTree`
    / `RoutedTree` `width` that `html_emitter.emit()` built the table at) rather than relying on
    the generic `DEFAULT_VIEWPORT_WIDTH` guess -- a mismatched viewport width will tank the score
    on background padding alone, not on any real geometry defect.
    """
    index_path = Path(index_path)
    if not index_path.is_file():
        return _unavailable(f"index_path does not exist or is not a file: {index_path}")

    try:
        png_bytes = _render_html_to_png(
            index_path,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            timeout_ms=timeout_ms,
        )
        if not _PIL_AVAILABLE:
            raise OracleUnavailable(f"Pillow unavailable to decode the screenshot: {_PIL_IMPORT_ERROR!r}")
        render_img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        truth_img = _load_ground_truth(psd_flatten_image)
        score, detail = _normalized_pixel_score(render_img, truth_img)
    except OracleUnavailable as exc:
        return _unavailable(str(exc))
    except Exception as exc:  # pragma: no cover -- defensive: score_bundle must never raise
        return _unavailable(f"unexpected oracle failure: {exc!r}")

    return {
        "available": True,
        "score": score,
        "width": detail["compared_width"],
        "note": GEOMETRY_PROXY_NOTE,
        "detail": detail,
    }
