"""C-FIDELITY-ORACLE tests.

Exercises the REAL headless-Chromium path (playwright + chromium are verified installed in this
environment) against tiny hand-built HTML bundles and hand-built ground-truth images -- never a
tautological "score equals its own output" assertion. Also exercises the real end-to-end pipeline
(psd_adapter -> table_solver -> layer_router -> html_emitter -> rasterizer.composite_psd ->
oracle.score_bundle) against the real announcement PSD when the corpus fixture is present.

Degrade-path tests simulate an unavailable Chromium/Pillow backend via monkeypatch and assert
`score_bundle` never raises and always reports `available: False` with a `reason`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from psd_html import oracle
from psd_html.oracle import GEOMETRY_PROXY_NOTE, OracleUnavailable, score_bundle

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ANNOUNCEMENT_PSD = os.path.join(
    REPO_ROOT,
    "Reference",
    "2413101_Intel",
    "PSDs",
    "Intel x Microsoft_Commercial Refresh_announcement email",
    "Intel_MsfT_Global BoM_Announcement Email.psd",
)
_has_real_psd = os.path.isfile(ANNOUNCEMENT_PSD)


def _write_bundle(tmp_path: Path, *, width: int, height: int, color_css: str) -> Path:
    """A minimal, hand-built OFT-safe-shaped bundle: one fixed-px table cell filled with a solid
    background color -- just enough for the real Chromium render to produce a deterministic,
    known-color screenshot we can hand-verify against a hand-built ground-truth image.

    Mirrors the real `html_emitter.emit()` output shape closely enough to matter for this test:
    a `<body style="margin:0;padding:0;">` wrapper -- WITHOUT it, Chromium's default 8px body
    margin would offset the table and any pixel-diff against a same-size ground-truth image would
    be dominated by that margin rather than by real content mismatch.
    """
    html = f"""<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
<html><body style="margin:0;padding:0;">
<table width="{width}" cellpadding="0" cellspacing="0" border="0" style="width:{width}px;">
  <tr>
    <td width="{width}" height="{height}" style="width:{width}px;height:{height}px;background-color:{color_css};font-size:0;line-height:0;">&nbsp;</td>
  </tr>
</table>
</body></html>
"""
    index_path = tmp_path / "index.html"
    index_path.write_text(html, encoding="utf-8")
    return index_path


def _solid_image(width: int, height: int, rgb: tuple):
    from PIL import Image

    return Image.new("RGB", (width, height), rgb)


# --- real Chromium path: numeric score in [0,1] + geometry-proxy note --------------------------


def test_identical_solid_color_bundle_scores_near_perfect(tmp_path):
    index_path = _write_bundle(tmp_path, width=200, height=100, color_css="#336699")
    ground_truth = _solid_image(200, 100, (0x33, 0x66, 0x99))

    # A real caller passes viewport_width matching the bundle's own authored width (e.g.
    # TableTree.width) -- the default is a generic guess and is NOT expected to match every
    # hand-built fixture's width.
    result = score_bundle(index_path, ground_truth, viewport_width=200, viewport_height=100)

    assert result["available"] is True
    assert result["note"] == GEOMETRY_PROXY_NOTE
    assert isinstance(result["score"], float)
    assert 0.0 <= result["score"] <= 1.0
    # A pixel-identical render vs. ground truth should score very close to 1 -- not a tautology
    # (the score comes from a real Chromium screenshot pixel-diffed against a hand-built image,
    # not from re-deriving the same value the function itself produced).
    assert result["score"] > 0.95, result


def test_wildly_different_color_scores_much_lower_than_identical(tmp_path):
    """Same geometry, deliberately WRONG ground-truth color -- proves the score is a real signal,
    not a constant/placeholder. Comparing against the identical-color case above is the adversarial
    check: if this ever scored as high as the identical case, the diff math would be broken."""
    index_path = _write_bundle(tmp_path, width=200, height=100, color_css="#000000")
    ground_truth = _solid_image(200, 100, (255, 255, 255))  # pure white vs. pure black

    result = score_bundle(index_path, ground_truth, viewport_width=200, viewport_height=100)

    assert result["available"] is True
    assert 0.0 <= result["score"] <= 1.0
    assert result["score"] < 0.1, result  # near-total mismatch -> near-zero score


def test_score_reflects_partial_mismatch_between_the_two_extremes(tmp_path):
    """A half red / half matching-blue ground truth against an all-blue render should land strictly
    between the identical and the totally-mismatched cases -- another non-tautological, hand-graded
    check on the actual pixel math."""
    index_path = _write_bundle(tmp_path, width=200, height=100, color_css="#0000ff")

    from PIL import Image

    half_mismatch = Image.new("RGB", (200, 100), (0, 0, 255))
    for y in range(50):  # top half wrong (red), bottom half matches (blue)
        for x in range(200):
            half_mismatch.putpixel((x, y), (255, 0, 0))

    kwargs = {"viewport_width": 200, "viewport_height": 100}
    identical = score_bundle(index_path, Image.new("RGB", (200, 100), (0, 0, 255)), **kwargs)["score"]
    mismatched = score_bundle(index_path, Image.new("RGB", (200, 100), (255, 0, 0)), **kwargs)["score"]
    partial = score_bundle(index_path, half_mismatch, **kwargs)["score"]

    assert mismatched < partial < identical


def test_ground_truth_path_on_disk_is_accepted(tmp_path):
    """`psd_flatten_image` may be a path (str) to a proof image on disk, not only a live PIL
    Image -- exercise that real branch."""
    index_path = _write_bundle(tmp_path, width=120, height=60, color_css="#00ff00")
    truth_path = tmp_path / "ground_truth.png"
    _solid_image(120, 60, (0, 255, 0)).save(truth_path)

    result = score_bundle(index_path, str(truth_path), viewport_width=120, viewport_height=60)

    assert result["available"] is True
    assert result["score"] > 0.95


def test_ground_truth_height_mismatch_is_reported_not_hidden(tmp_path):
    """A ground truth that is a very different aspect ratio still gets scored (never crashes) and
    the detail dict records the mismatch rather than silently pretending the shapes matched."""
    index_path = _write_bundle(tmp_path, width=200, height=400, color_css="#336699")
    ground_truth = _solid_image(200, 50, (0x33, 0x66, 0x99))  # much shorter than the render

    result = score_bundle(index_path, ground_truth, viewport_width=200, viewport_height=400)

    assert result["available"] is True
    assert result["detail"]["height_mismatch"] is True
    assert result["detail"]["render_height"] == 400
    assert result["detail"]["compared_height"] == min(result["detail"]["render_height"], 50)


# --- degrade paths: NEVER raise, always {"available": False, ...} ------------------------------


def test_missing_index_path_degrades_loud_but_safe(tmp_path):
    missing = tmp_path / "does_not_exist" / "index.html"
    result = score_bundle(missing, _solid_image(10, 10, (0, 0, 0)))

    assert result == {
        "available": False,
        "score": None,
        "width": None,
        "note": GEOMETRY_PROXY_NOTE,
        "detail": {"reason": result["detail"]["reason"]},
    }
    assert "does not exist" in result["detail"]["reason"]


def test_chromium_launch_failure_is_simulated_unavailable(tmp_path, monkeypatch):
    """Simulate Chromium being unlaunchable at runtime (e.g. the binary missing) by monkeypatching
    `sync_playwright` to a stub whose `.chromium.launch()` raises -- proves the degrade path is
    real code, not just an untested branch."""
    index_path = _write_bundle(tmp_path, width=50, height=50, color_css="#ffffff")

    class _FakeChromium:
        def launch(self):
            raise RuntimeError("simulated: chromium executable not found")

    class _FakeBrowserType:
        chromium = _FakeChromium()

    class _FakeContextManager:
        def __enter__(self):
            return _FakeBrowserType()

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(oracle, "sync_playwright", lambda: _FakeContextManager())
    monkeypatch.setattr(oracle, "_PLAYWRIGHT_AVAILABLE", True)

    result = score_bundle(index_path, _solid_image(50, 50, (255, 255, 255)))

    assert result["available"] is False
    assert result["score"] is None
    assert result["width"] is None
    assert result["note"] == GEOMETRY_PROXY_NOTE
    assert "simulated: chromium executable not found" in result["detail"]["reason"]


def test_playwright_unimportable_degrades_loud_but_safe(tmp_path, monkeypatch):
    """Simulate the playwright package itself being unavailable at import time (the flag flip
    `rasterizer.py`/`text_raster_adapter.py` use elsewhere in this package)."""
    index_path = _write_bundle(tmp_path, width=50, height=50, color_css="#ffffff")

    monkeypatch.setattr(oracle, "_PLAYWRIGHT_AVAILABLE", False)
    monkeypatch.setattr(oracle, "_PLAYWRIGHT_IMPORT_ERROR", RuntimeError("simulated missing playwright"))

    result = score_bundle(index_path, _solid_image(50, 50, (255, 255, 255)))

    assert result["available"] is False
    assert result["score"] is None
    assert "simulated missing playwright" in result["detail"]["reason"]


def test_unusable_ground_truth_path_degrades_loud_but_safe(tmp_path):
    index_path = _write_bundle(tmp_path, width=50, height=50, color_css="#ffffff")

    result = score_bundle(index_path, str(tmp_path / "not_a_real_image.png"))

    assert result["available"] is False
    assert result["score"] is None
    assert "could not load psd_flatten_image" in result["detail"]["reason"]


def test_degraded_composite_psd_dict_is_rejected_not_crashed(tmp_path):
    """`rasterizer.composite_psd` degrades to `{"available": False, "reason": ...}` on failure --
    feeding that dict straight through as `psd_flatten_image` must degrade loud-but-safe, not blow
    up trying to treat a dict as an image."""
    index_path = _write_bundle(tmp_path, width=50, height=50, color_css="#ffffff")
    degraded_composite = {"available": False, "reason": "simulated: psd-tools composite failed"}

    result = score_bundle(index_path, degraded_composite)

    assert result["available"] is False
    assert "degraded composite dict" in result["detail"]["reason"]


def test_score_bundle_never_raises_on_unexpected_internal_error(tmp_path, monkeypatch):
    """Force an unexpected exception deep in the scoring path (not one of the named
    OracleUnavailable branches) and confirm the outer defensive catch-all still returns a dict."""
    index_path = _write_bundle(tmp_path, width=50, height=50, color_css="#ffffff")

    def _boom(*_args, **_kwargs):
        raise ZeroDivisionError("simulated unexpected failure")

    monkeypatch.setattr(oracle, "_normalized_pixel_score", _boom)

    result = score_bundle(index_path, _solid_image(50, 50, (255, 255, 255)))

    assert result["available"] is False
    assert result["score"] is None
    assert "unexpected oracle failure" in result["detail"]["reason"]


# --- pure-Pillow (no-numpy) pixel-delta fallback is byte-exact vs the numpy path ---------------


def test_numpy_absent_fallback_scores_like_numpy_path(tmp_path, monkeypatch):
    """`_mean_abs_pixel_delta` degrades to a pure-Pillow ImageChops/ImageStat computation when
    numpy is unavailable. numpy is always present here, so that branch is otherwise dead -- force
    it off and confirm it produces the same identical->~1 / mismatch->~0 signal as numpy."""
    monkeypatch.setattr(oracle, "_NUMPY_AVAILABLE", False)

    (tmp_path / "id").mkdir()
    (tmp_path / "mm").mkdir()
    identical_index = _write_bundle(tmp_path / "id", width=200, height=100, color_css="#336699")
    identical = score_bundle(identical_index, _solid_image(200, 100, (0x33, 0x66, 0x99)),
                             viewport_width=200, viewport_height=100)
    mismatch_index = _write_bundle(tmp_path / "mm", width=200, height=100, color_css="#000000")
    mismatch = score_bundle(mismatch_index, _solid_image(200, 100, (255, 255, 255)),
                            viewport_width=200, viewport_height=100)

    assert identical["available"] is True and mismatch["available"] is True
    assert identical["score"] > 0.95
    assert mismatch["score"] < 0.1


# --- OracleUnavailable is internal-only: never escapes score_bundle ----------------------------


def test_oracle_unavailable_exception_class_exists_and_is_a_runtime_error():
    assert issubclass(OracleUnavailable, RuntimeError)


# --- real end-to-end: real PSD -> real bundle -> real Chromium score ---------------------------


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_real_announcement_psd_end_to_end_scores_available_with_numeric_result(tmp_path):
    from psd_html.html_emitter import emit
    from psd_html.layer_router import route
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.rasterizer import composite_psd
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout, email_override="Announcement")
    tree = trees[0]
    routed = route(tree, "hybrid")
    composite = composite_psd(ANNOUNCEMENT_PSD)
    assert not isinstance(composite, dict), f"composite_psd degraded unexpectedly: {composite}"

    emit_result = emit(routed, tmp_path, composite=composite, layer_names=layer_names)
    index_path = Path(emit_result["index_path"])
    assert index_path.is_file()

    result = score_bundle(index_path, composite)

    assert result["note"] == GEOMETRY_PROXY_NOTE
    # The real corpus PSD is a large, complex multi-region template -- Chromium's table layout
    # will NOT match the PSD flatten pixel-for-pixel (that is the entire point of this being a
    # geometry PROXY, not a Word-engine gate), so this only asserts the pipeline produced a real,
    # numeric, in-range score rather than crashing or silently degrading.
    assert result["available"] is True, result["detail"]
    assert isinstance(result["score"], float)
    assert 0.0 <= result["score"] <= 1.0
    assert result["detail"]["render_width"] > 0
    assert result["detail"]["ground_truth_width"] > 0
