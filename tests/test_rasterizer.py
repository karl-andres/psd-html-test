"""C-RASTERIZER tests.

Adversarial hand-built Cell fixtures for the routing/degrade-path logic (no tautological asserts),
PLUS the real announcement PSD run end-to-end (psd_adapter -> table_solver -> layer_router ->
rasterizer) for both backends -- resvg text rendering and Pillow composite cropping. Both external
tools (resvg_py, Pillow via psd-tools) are verified installed in this environment, so those real
paths are exercised, not mocked; the degrade branches (resvg missing / Pillow missing) are
exercised separately via monkeypatched module flags, since we cannot uninstall a real dependency
mid-suite.
"""

from __future__ import annotations

import os
import struct

import pytest

from psd_html import rasterizer
from psd_html.layer_router import EditabilityViolation, route
from psd_html.layout_tree import BBox, TextInfo, TextRun
from psd_html.rasterizer import (
    PNG_MAGIC,
    RasterizerUnavailable,
    RasterResult,
    composite_psd,
    rasterize_brand_headline,
    rasterize_cell,
    rasterize_image_cell,
)
from psd_html.table_tree import Cell

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


def _rect(l=0, t=0, r=200, b=60):
    return BBox(left=l, top=t, right=r, bottom=b)


# Segoe routes LIVE since 2026-07-14 (human-OFT proof); raster-path tests exercise
# brand_headline with a registered fixture face Windows does not install.
BRAND_FIXTURE_FONT = "BrandDisplay-Semibold"


@pytest.fixture(autouse=True)
def _register_brand_fixture_font():
    from psd_html.font_resolver import DEFAULT_REGISTRY, FontRegistryEntry

    DEFAULT_REGISTRY["branddisplay"] = FontRegistryEntry(
        family="Brand Display", fallback_stack=("sans-serif",), brand_mandatory=True,
        files=DEFAULT_REGISTRY["segoeui"].files,  # measure/render with a real installed file
    )
    yield
    DEFAULT_REGISTRY.pop("branddisplay", None)


def _headline_cell(content="Sample Headline", *, font=BRAND_FIXTURE_FONT, rect=None, source_layer_id=101):
    return Cell(
        role="text",
        rect=rect or _rect(),
        editable=False,
        link_slot=None,
        source_layer_id=source_layer_id,
        text=TextInfo(content=content, align="left", runs=[TextRun(font=font, size=24.0, color="#000000")]),
    )


def _image_cell(rect=None, image_source_layer_ids=None):
    return Cell(role="image", rect=rect or _rect(), image_source_layer_ids=image_source_layer_ids or [1])


def _body_cell():
    return Cell(
        role="text",
        rect=_rect(),
        editable=False,
        link_slot=None,
        text=TextInfo(content="plain body copy", align="left", runs=[TextRun(font="Arial", size=14.0, color="#000")]),
    )


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# --- resvg text path: real PNG bytes -----------------------------------------------------------


def test_resvg_text_path_renders_real_png(tmp_path):
    cell = _headline_cell()
    result = rasterize_brand_headline(cell, tmp_path, final_copy="Announcing the New Lineup")
    assert isinstance(result, RasterResult)
    assert result.backend == "resvg"
    out_file = tmp_path / result.relpath
    assert out_file.is_file()
    data = _read_bytes(out_file)
    assert data.startswith(PNG_MAGIC)
    assert result.width > 0 and result.height > 0
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (result.width, result.height)
    assert result.alt  # never empty


def test_resvg_backend_embeds_discovered_brand_font_files_not_degraded(tmp_path):
    cell = _headline_cell(font="BrandDisplay-Bold")
    result = rasterize_brand_headline(cell, tmp_path, final_copy="Bold Brand Headline")
    # On this Windows box segoeui*.ttf is discoverable -- the brand-font guarantee should hold.
    assert result.degraded_brand_font is False
    assert result.warning is None


def test_svg_bytes_deterministic_across_two_render_calls(tmp_path):
    cell = _headline_cell()
    r1 = rasterize_brand_headline(cell, tmp_path, final_copy="Deterministic Copy", filename="a.png")
    r2 = rasterize_brand_headline(cell, tmp_path, final_copy="Deterministic Copy", filename="b.png")
    bytes_a = _read_bytes(tmp_path / r1.relpath)
    bytes_b = _read_bytes(tmp_path / r2.relpath)
    assert bytes_a == bytes_b


def test_final_copy_used_over_sample_copy(tmp_path):
    cell = _headline_cell(content="STALE SAMPLE")
    result = rasterize_brand_headline(cell, tmp_path, final_copy="FRESH FINAL COPY")
    assert result.warning is None


def test_missing_final_copy_falls_back_to_sample_with_warning(tmp_path):
    cell = _headline_cell(content="STALE SAMPLE")
    result = rasterize_brand_headline(cell, tmp_path, final_copy=None)
    assert result.warning is not None
    assert result.warning["type"] == "sample_copy_used"


def test_overflowing_headline_still_produces_a_full_height_png_not_clipped(tmp_path):
    tiny_rect = _rect(0, 0, 60, 15)  # far too small for the copy below
    cell = _headline_cell(rect=tiny_rect)
    result = rasterize_brand_headline(
        cell, tmp_path, final_copy="This headline is far too long for its original tiny box"
    )
    assert result.overflow is True
    assert result.height > 15  # grew past the original rect height, never clipped down to it


def test_alt_text_never_empty_even_without_content(tmp_path):
    cell = _headline_cell(content="")
    cell.text = TextInfo(content="", align="left", runs=[TextRun(font="BrandDisplay", size=20.0, color="#000")])
    result = rasterize_brand_headline(cell, tmp_path, final_copy="")
    assert result.alt  # falls back to "text layer #<id>"


def test_non_text_cell_rejected_by_rasterize_brand_headline(tmp_path):
    with pytest.raises(ValueError):
        rasterize_brand_headline(_image_cell(), tmp_path, final_copy="x")


# --- Pillow composite crop: real PSD ------------------------------------------------------------


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_pillow_composite_crop_real_psd_produces_matching_dims(tmp_path):
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout, email_override="Announcement")
    tree = trees[0]

    def find_image_cell(rows):
        for row in rows:
            for cell in row.cells:
                if cell.role == "image":
                    return cell
                if cell.role == "rows" and cell.rows:
                    found = find_image_cell(cell.rows)
                    if found is not None:
                        return found
        return None

    image_cell = find_image_cell(tree.rows)
    assert image_cell is not None, "expected at least one image cell in the real announcement tree"

    composite = composite_psd(ANNOUNCEMENT_PSD)
    assert not isinstance(composite, dict), f"composite_psd degraded unexpectedly: {composite}"

    result = rasterize_image_cell(image_cell, composite, tmp_path, layer_names=layer_names)
    out_file = tmp_path / result.relpath
    assert out_file.is_file()
    data = _read_bytes(out_file)
    assert data.startswith(PNG_MAGIC)
    assert result.width == image_cell.rect.width
    assert result.height == image_cell.rect.height
    assert result.backend == "pillow-crop"
    assert result.alt  # never empty, sourced from the PSD layer name
    assert result.alt != f"image region rect={image_cell.rect.to_dict()}"  # a real name was found


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_composite_psd_real_file_returns_usable_image():
    composite = composite_psd(ANNOUNCEMENT_PSD)
    assert not isinstance(composite, dict)
    assert composite.size[0] > 0 and composite.size[1] > 0


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_full_pipeline_every_raster_leaf_gets_a_real_png_and_nonempty_alt():
    from psd_html.layer_router import iter_routed
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout, email_override="Announcement")
    routed = route(trees[0], "hybrid")
    composite = composite_psd(ANNOUNCEMENT_PSD)

    import tempfile

    seen_backends = set()
    with tempfile.TemporaryDirectory() as tmp:
        raster_leaves = [(c, k) for c, k, v in iter_routed(routed) if v == "raster"]
        assert raster_leaves, "expected at least one raster-routed leaf in the real tree"
        for cell, key in raster_leaves:
            result = rasterize_cell(
                cell,
                tmp,
                final_copy=(cell.text.content if cell.text else None),
                composite=composite,
                layer_names=layer_names,
                filename=f"{'_'.join(str(k) for k in key)}.png",
            )
            assert result.alt, f"empty alt for cell at key {key}"
            out_file = os.path.join(tmp, *result.relpath.split("/"))
            assert os.path.isfile(out_file)
            assert _read_bytes(out_file).startswith(PNG_MAGIC)
            seen_backends.add(result.backend)
    # Decision 2026-07-14: the real corpus is 100% Segoe UI, which routes LIVE (human-OFT proof)
    # -- no brand headline remains, so the only raster backend the real tree exercises is the
    # image crop. The resvg re-typeset path is covered by the synthetic fixture tests above.
    assert seen_backends == {"pillow-crop"}


# --- _png_dims: the "is this really a PNG" guard + IHDR parse ----------------------------------


def test_png_dims_rejects_bad_or_missing_magic():
    from psd_html.rasterizer import _png_dims

    with pytest.raises(ValueError):
        _png_dims(b"")
    with pytest.raises(ValueError):
        _png_dims(b"notpng-notpng-notpng-notpng")  # right length, wrong magic


def test_png_dims_rejects_truncated_before_ihdr():
    from psd_html.rasterizer import _png_dims

    with pytest.raises(ValueError):
        _png_dims(PNG_MAGIC + b"\x00" * 5)  # valid magic but < 24 bytes


def test_png_dims_parses_ihdr_width_height():
    from psd_html.rasterizer import _png_dims

    data = PNG_MAGIC + b"\x00" * 8 + struct.pack(">II", 123, 45)  # dims live at bytes 16:24
    assert _png_dims(data) == (123, 45)


# --- _alt_for_image_cell: the two fallback branches (no name / no ids) --------------------------


def test_alt_for_image_cell_falls_back_to_layer_id_when_no_name():
    from psd_html.rasterizer import _alt_for_image_cell

    cell = _image_cell(image_source_layer_ids=[1])
    assert _alt_for_image_cell(cell, {}) == "image region (layer #1)"
    assert _alt_for_image_cell(cell, None) == "image region (layer #1)"


def test_alt_for_image_cell_falls_back_to_role_rect_when_no_ids():
    from psd_html.rasterizer import _alt_for_image_cell

    cell = Cell(role="graphic", rect=_rect(), image_source_layer_ids=None, source_layer_id=None)
    alt = _alt_for_image_cell(cell, {})
    assert alt.startswith("graphic")
    assert "rect=" in alt


# --- resvg present but brand-font files NOT discovered -> degraded, still resvg (idx 70) --------


def test_resvg_present_but_no_brand_font_files_degrades_but_stays_resvg(tmp_path):
    from psd_html.font_resolver import DEFAULT_REGISTRY, FontRegistryEntry

    # Override the autouse fixture entry with a brand-mandatory face that discovered NO files.
    DEFAULT_REGISTRY["branddisplay"] = FontRegistryEntry(
        family="Brand Display", fallback_stack=("sans-serif",), brand_mandatory=True, files=(),
    )
    cell = _headline_cell(font="BrandDisplay-Semibold")
    result = rasterize_brand_headline(cell, tmp_path, final_copy="No Font Files Headline")
    assert result.backend == "resvg"  # resvg IS available -- this is not the resvg-down fallback
    assert result.degraded_brand_font is True
    assert result.warning is not None
    assert result.warning["type"] == "brand_font_files_unavailable"


# --- composite_psd loud-but-safe degrade --------------------------------------------------------


def test_composite_psd_missing_file_degrades_loud_but_safe():
    result = composite_psd("this/path/does/not/exist.psd")
    assert isinstance(result, dict)
    assert result["available"] is False
    assert "reason" in result and result["reason"]


def test_rasterize_image_cell_raises_when_composite_unavailable(tmp_path):
    unavailable = {"available": False, "reason": "psd-tools import failed"}
    with pytest.raises(RasterizerUnavailable):
        rasterize_image_cell(_image_cell(), unavailable, tmp_path)


def test_rasterize_image_cell_raises_when_composite_none(tmp_path):
    with pytest.raises(RasterizerUnavailable):
        rasterize_image_cell(_image_cell(), None, tmp_path)


def test_rasterize_image_cell_wrong_role_raises(tmp_path):
    with pytest.raises(ValueError):
        rasterize_image_cell(_headline_cell(), object(), tmp_path)


# --- dispatcher: rasterize_cell routes by render_role -------------------------------------------


def test_dispatcher_routes_brand_headline_to_resvg(tmp_path):
    result = rasterize_cell(_headline_cell(), tmp_path, final_copy="Dispatched Headline")
    assert result.backend == "resvg"


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_dispatcher_routes_image_to_pillow_crop(tmp_path):
    composite = composite_psd(ANNOUNCEMENT_PSD)
    cell = _image_cell(rect=_rect(0, 0, 50, 50))
    result = rasterize_cell(cell, tmp_path, composite=composite)
    assert result.backend == "pillow-crop"


def test_dispatcher_rejects_non_raster_eligible_role(tmp_path):
    # A plain body-copy cell (Arial, no bracket, not editable, no link) is PROTECTED -- it must
    # never reach the rasterizer at all. rasterize_cell must refuse it loudly, not silently
    # "helpfully" rasterize it.
    with pytest.raises(ValueError):
        rasterize_cell(_body_cell(), tmp_path)


def test_protected_cell_that_somehow_reached_here_is_still_refused_not_rastered(tmp_path):
    # Belt-and-suspenders companion to layer_router's own EditabilityViolation guard: even if a
    # caller mistakenly hands the rasterizer a PROTECTED cell directly (bypassing route()
    # entirely), rasterize_cell's role check refuses it -- it never produces a raster PNG for
    # body/merge/cta content under any circumstance.
    body = _body_cell()
    with pytest.raises(ValueError):
        rasterize_cell(body, tmp_path)


# --- EditabilityViolation still holds one level up (layer_router), rasterizer never overrides it -


def test_editability_violation_from_router_prevents_ever_reaching_rasterizer():
    from psd_html.layer_router import _assign_verb

    body = _body_cell()
    with pytest.raises(EditabilityViolation):
        _assign_verb(body, "hybrid", force_verb="raster")


# --- degrade paths: resvg unavailable / neither backend available -------------------------------


def test_resvg_unavailable_falls_back_to_pillow_text(tmp_path, monkeypatch):
    monkeypatch.setattr(rasterizer, "_RESVG_AVAILABLE", False)
    monkeypatch.setattr(rasterizer, "_RESVG_IMPORT_ERROR", ImportError("simulated: resvg_py not installed"))
    cell = _headline_cell()
    result = rasterize_brand_headline(cell, tmp_path, final_copy="Fallback Headline")
    assert result.backend == "pillow-text-fallback"
    assert result.degraded_brand_font is True
    assert result.warning is not None
    assert result.warning["type"] == "resvg_unavailable"
    out_file = tmp_path / result.relpath
    data = _read_bytes(out_file)
    assert data.startswith(PNG_MAGIC)


def test_resvg_raising_at_call_time_also_falls_back_to_pillow(tmp_path, monkeypatch):
    class _BoomResvg:
        @staticmethod
        def svg_to_bytes(**kwargs):
            raise RuntimeError("simulated resvg crash")

    monkeypatch.setattr(rasterizer, "_resvg", _BoomResvg)
    cell = _headline_cell()
    result = rasterize_brand_headline(cell, tmp_path, final_copy="Crash Recovery Headline")
    assert result.backend == "pillow-text-fallback"
    assert result.degraded_brand_font is True


def test_pillow_fallback_honors_density_scale_at_hi_dpi(tmp_path, monkeypatch):
    # Companion to the scale==1.0 fallback tests above: when resvg is down and the Pillow text
    # fallback runs at density > 1, it must render at scale x -- the RasterText is authored at 1x
    # (CSS px) and the emitter divides the raster's pixel dims by density downstream. Regression for
    # the retina mismatch where the fallback ignored scale and the headline rendered at 1/density.
    class _BoomResvg:
        @staticmethod
        def svg_to_bytes(**kwargs):
            raise RuntimeError("simulated resvg crash")

    monkeypatch.setattr(rasterizer, "_resvg", _BoomResvg)
    scale = 3

    base = rasterize_brand_headline(
        _headline_cell(), tmp_path, final_copy="Density Aware Headline",
        filename="fallback_1x.png", source_scale=1.0,
    )
    hi = rasterize_brand_headline(
        _headline_cell(), tmp_path, final_copy="Density Aware Headline",
        filename="fallback_hi.png", source_scale=scale,
    )

    assert base.backend == "pillow-text-fallback"
    assert hi.backend == "pillow-text-fallback"
    # The hi-dpi fallback raster is ~scale x the 1x fallback in both dimensions (within rounding),
    # so the emitter's divide-by-density restores the correct display size instead of 1/scale.
    assert hi.width == pytest.approx(base.width * scale, abs=2)
    assert hi.height == pytest.approx(base.height * scale, abs=2)


def test_neither_backend_available_raises_rasterizer_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(rasterizer, "_RESVG_AVAILABLE", False)
    monkeypatch.setattr(rasterizer, "_RESVG_IMPORT_ERROR", ImportError("simulated"))
    monkeypatch.setattr(rasterizer, "_PIL_AVAILABLE", False)
    monkeypatch.setattr(rasterizer, "_PIL_IMPORT_ERROR", ImportError("simulated: Pillow not installed"))
    cell = _headline_cell()
    with pytest.raises(RasterizerUnavailable):
        rasterize_brand_headline(cell, tmp_path, final_copy="No Backend Left")


def test_pillow_unavailable_for_image_crop_raises_rasterizer_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(rasterizer, "_PIL_AVAILABLE", False)
    monkeypatch.setattr(rasterizer, "_PIL_IMPORT_ERROR", ImportError("simulated: Pillow not installed"))

    class _FakeComposite:
        width = 500
        height = 500
        size = (500, 500)

        def crop(self, box):  # pragma: no cover -- should never be reached, Pillow guard fires first
            raise AssertionError("crop() must not be called once Pillow is marked unavailable")

    with pytest.raises(RasterizerUnavailable):
        rasterize_image_cell(_image_cell(), _FakeComposite(), tmp_path)


def test_degenerate_out_of_bounds_rect_raises_value_error(tmp_path):
    class _FakeComposite:
        width = 10
        height = 10
        size = (10, 10)

        def crop(self, box):  # pragma: no cover -- guard should fire before this is called
            raise AssertionError("crop() must not be called for a degenerate box")

    # Rect entirely outside the composite's bounds -> clamped box collapses to zero area.
    out_of_bounds = _image_cell(rect=_rect(500, 500, 600, 600))
    with pytest.raises(ValueError):
        rasterize_image_cell(out_of_bounds, _FakeComposite(), tmp_path)
