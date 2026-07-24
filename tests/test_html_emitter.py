"""C-HTML-EMITTER tests.

Adversarial hand-built RoutedTree fixtures (no tautological asserts -- every case is checked
against a hand-picked expected HTML/JSON shape, never against the function's own output) PLUS the
real announcement PSD run end-to-end (psd_adapter -> table_solver -> layer_router -> html_emitter)
for a full bundle smoke test.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from psd_html.html_emitter import DPI_LOCK_BLOCK, HtmlEmitterError, emit
from psd_html.layer_router import route
from psd_html.layout_tree import BBox, TextInfo, TextRun
from psd_html.table_tree import Background, Cell, Row, TableTree

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

# Grammar the OFT-safe bundle must NEVER contain (case-insensitive substring check).
_FORBIDDEN_PATTERNS = ("srcset=", "@import", "<link", "<style", " background=", "<v:", ".svg")


def _rect(l=0, t=0, r=100, b=20):
    return BBox(left=l, top=t, right=r, bottom=b)


# Segoe routes LIVE since 2026-07-14 (human-OFT proof); brand-raster emitter tests use a
# registered fixture face Windows does not install.
BRAND_FIXTURE_FONT = "BrandDisplay-Bold"

# A registered family with NO discoverable font file (files=()) -- exercises the documented
# avg-char-width fallback in _has_unbreakable_overflow / the None-return fail-open in
# _word_budgeted_line_count, independent of what fonts happen to be installed on the test box.
NO_FILE_FIXTURE_FONT = "NoFileFixture"


@pytest.fixture(autouse=True)
def _register_brand_fixture_font():
    from psd_html.font_resolver import DEFAULT_REGISTRY, FontRegistryEntry

    DEFAULT_REGISTRY["branddisplay"] = FontRegistryEntry(
        family="Brand Display", fallback_stack=("sans-serif",), brand_mandatory=True,
        files=DEFAULT_REGISTRY["segoeui"].files,
    )
    DEFAULT_REGISTRY["nofilefixture"] = FontRegistryEntry(
        family="No File Fixture", fallback_stack=("sans-serif",), brand_mandatory=False, files=(),
    )
    yield
    DEFAULT_REGISTRY.pop("branddisplay", None)
    DEFAULT_REGISTRY.pop("nofilefixture", None)


def _text_cell(content, *, font="Arial", editable=False, link_slot=None, role="text", size=14.0, rect=None, source_layer_id=None):
    return Cell(
        role=role,
        rect=rect or _rect(),
        editable=editable,
        link_slot=link_slot,
        source_layer_id=source_layer_id,
        text=TextInfo(content=content, align="left", runs=[TextRun(font=font, size=size, color="#000000")]),
    )


def _image_cell(rect=None, image_source_layer_ids=None, link_slot=None):
    return Cell(role="image", rect=rect or _rect(), image_source_layer_ids=image_source_layer_ids or [1], link_slot=link_slot)


def _rows_cell(rows, rect=None):
    return Cell(role="rows", rect=rect or _rect(), rows=rows)


def _tree(cells_per_row, width=600):
    return TableTree(email="Test Email", width=width, rows=[Row(cells=cells) for cells in cells_per_row])


def _emit_html(tree, policy="hybrid", **emit_kwargs):
    """Route + emit into a fresh tmp bundle dir passed via emit_kwargs['out_dir'], returning the
    (result_dict, html_text) pair -- the shape almost every test below needs."""
    out_dir = emit_kwargs.pop("out_dir")
    routed = route(tree, policy)
    result = emit(routed, out_dir, **emit_kwargs)
    html_text = Path(result["index_path"]).read_text(encoding="utf-8")
    return result, html_text


# --- OFT-safe bundle grammar -----------------------------------------------------------------------


def test_dpi_lock_block_present_verbatim(tmp_path):
    tree = _tree([[_text_cell("Hi [First Name],")]])
    _result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert DPI_LOCK_BLOCK in html_text


def test_index_html_is_valid_utf8_with_no_bom(tmp_path):
    tree = _tree([[_text_cell("Hi [First Name],")]])
    result, _html = _emit_html(tree, out_dir=tmp_path)
    raw = Path(result["index_path"]).read_bytes()
    raw.decode("utf-8")  # raises UnicodeDecodeError on any invalid byte sequence
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_forbidden_asset_grammar_patterns_absent(tmp_path):
    from PIL import Image

    composite = Image.new("RGBA", (200, 200), (10, 20, 30, 255))
    merge = _text_cell("Hi [First Name],", font="Arial")
    cta = Cell(role="button", rect=_rect(), text=TextInfo(content="Shop Now", align="center", runs=[TextRun(font="Arial", size=16.0, color="#ffffff")]))
    body = _text_cell("Plain running body copy.", font="Arial")
    headline = _text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)
    image = _image_cell(rect=_rect(0, 0, 50, 50))
    tree = _tree([[merge, cta, body], [headline, image]])

    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)
    low = html_text.lower()
    for pattern in _FORBIDDEN_PATTERNS:
        assert pattern.lower() not in low, f"forbidden pattern {pattern!r} found in emitted HTML"
    # every asset that DID get referenced is a PNG on disk (grammar: raster assets are png/jpg/gif)
    assert result["assets"], "expected at least one rasterized asset (headline + image cells)"
    for relpath in result["assets"]:
        assert relpath.endswith(".png")
        assert (tmp_path / relpath).is_file()


def test_all_asset_and_img_src_paths_are_relative(tmp_path):
    from PIL import Image

    composite = Image.new("RGBA", (200, 200), (5, 5, 5, 255))
    tree = _tree([[_image_cell(rect=_rect(0, 0, 40, 40))]])
    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)

    for relpath in result["assets"]:
        assert not relpath.startswith("/")
        assert ":" not in relpath
        assert "\\" not in relpath
        assert ".." not in relpath.split("/")

    for m in re.finditer(r'src="([^"]+)"', html_text):
        src = m.group(1)
        assert not src.startswith(("http://", "https://", "/"))
        assert ":" not in src


def test_emit_rejects_non_routed_tree_argument(tmp_path):
    with pytest.raises(HtmlEmitterError):
        emit(object(), tmp_path)


# --- per-role rendering -----------------------------------------------------------------------------


def test_live_merge_cell_renders_editable_td_with_merge_token(tmp_path):
    tree = _tree([[_text_cell("Hi [First Name],", font="Arial")]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert "[First Name]" in html_text
    assert "<img" not in html_text  # never rastered
    assert result["regions"][0]["role"] == "merge"
    assert result["regions"][0]["render"] == "live"


def test_body_copy_cell_renders_live_full_text_never_rastered(tmp_path):
    tree = _tree([[_text_cell("This is plain running body copy.", font="Arial")]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert "This is plain running body copy." in html_text
    assert "<img" not in html_text
    assert result["regions"][0]["role"] == "body"
    assert result["regions"][0]["render"] == "live"


def test_brand_headline_hybrid_renders_as_img_with_nonempty_alt(tmp_path):
    tree = _tree([[_text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)]])
    result, html_text = _emit_html(tree, policy="hybrid", out_dir=tmp_path)
    assert "<img" in html_text
    m = re.search(r'<img[^>]*alt="([^"]*)"', html_text)
    assert m is not None and m.group(1).strip()
    assert result["regions"][0]["role"] == "brand_headline"
    assert result["regions"][0]["render"] == "raster"
    assert result["regions"][0]["brand_font_rasterized"] is True


def test_brand_headline_live_policy_stays_live_text_not_img(tmp_path):
    tree = _tree([[_text_cell("Big Bold Headline", font=BRAND_FIXTURE_FONT)]])
    result, html_text = _emit_html(tree, policy="live", out_dir=tmp_path)
    assert "<img" not in html_text
    assert "Big Bold Headline" in html_text
    assert result["regions"][0]["render"] == "live"
    assert result["regions"][0]["brand_font_rasterized"] is False


def test_cta_renders_bulletproof_button_with_padding_and_no_fixed_inner_width(tmp_path):
    cta = Cell(
        role="button",
        rect=_rect(0, 0, 160, 40),
        text=TextInfo(content="Shop Now", align="center", runs=[TextRun(font="Arial", size=16.0, color="#ffffff")]),
    )
    tree = _tree([[cta]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)

    assert result["regions"][0]["role"] == "cta"
    assert result["regions"][0]["render"] == "live"
    assert "Shop Now" in html_text

    m = re.search(r'<td bgcolor="[^"]*" style="([^"]*)"', html_text)
    assert m is not None, "expected a bulletproof-button <td> (bgcolor first attribute, no width)"
    button_style = m.group(1)
    assert "padding" in button_style
    assert "width" not in button_style


def test_cta_from_link_slot_text_cell_also_renders_as_button(tmp_path):
    # is_cta() covers role=="button" OR (role=="text" AND link_slot is not None) -- both shapes
    # must render through the SAME bulletproof-button path, per layer_router.render_role.
    cta_text = _text_cell("Review the toolkit", font="Arial", link_slot="review-the-toolkit")
    tree = _tree([[cta_text]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert result["regions"][0]["role"] == "cta"
    m = re.search(r'<td bgcolor="[^"]*" style="([^"]*)"', html_text)
    assert m is not None
    assert "padding" in m.group(1)
    assert "width" not in m.group(1)


def test_image_and_graphic_cells_render_as_raster_img(tmp_path):
    from PIL import Image

    composite = Image.new("RGBA", (200, 200), (1, 2, 3, 255))
    image = _image_cell(rect=_rect(0, 0, 40, 40))
    graphic = Cell(role="graphic", rect=_rect(40, 0, 90, 40), image_source_layer_ids=[9])
    tree = _tree([[image, graphic]])
    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)
    assert html_text.count("<img") == 2
    roles = {r["role"] for r in result["regions"]}
    assert roles == {"image", "graphic"}
    for region in result["regions"]:
        assert region["render"] == "raster"


# --- late copy / link binding ------------------------------------------------------------------------


def test_copy_manifest_binds_final_copy_over_psd_sample(tmp_path):
    cell = _text_cell("STALE SAMPLE COPY", font="Arial", source_layer_id=42)
    tree = _tree([[cell]])
    result, html_text = _emit_html(tree, out_dir=tmp_path, copy_manifest={42: "FRESH FINAL COPY"})
    assert "FRESH FINAL COPY" in html_text
    assert "STALE SAMPLE COPY" not in html_text


def test_missing_copy_manifest_entry_falls_back_to_psd_sample(tmp_path):
    cell = _text_cell("Sample copy only", font="Arial", source_layer_id=42)
    tree = _tree([[cell]])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, copy_manifest={7: "unrelated entry"})
    assert "Sample copy only" in html_text


def test_link_slot_with_resolved_href_wraps_body_text_in_anchor(tmp_path):
    # link_slot on a role=="text" cell makes it a CTA (button-pattern) per layer_router, so use it
    # here purely to prove the <a href> wrapping mechanism independent of the button shape by
    # checking the href lands on the actual rendered anchor.
    cta_text = _text_cell("Review the toolkit", font="Arial", link_slot="review-the-toolkit")
    tree = _tree([[cta_text]])
    result, html_text = _emit_html(
        tree, out_dir=tmp_path, link_manifest={"review-the-toolkit": "https://example.com/toolkit"}
    )
    assert '<a href="https://example.com/toolkit"' in html_text
    assert html_text.count("<a href=") == 1  # exactly one clickable element, never an image map
    assert "<map" not in html_text.lower()


def test_link_slot_with_resolved_href_wraps_raster_image_in_anchor(tmp_path):
    from PIL import Image

    composite = Image.new("RGBA", (200, 200), (10, 20, 30, 255))
    cell = _image_cell(rect=_rect(0, 0, 50, 50), image_source_layer_ids=[7], link_slot="shop-now")
    tree = _tree([[cell]])
    result, html_text = _emit_html(
        tree, out_dir=tmp_path, composite=composite, link_manifest={"shop-now": "https://example.com/shop"}
    )
    assert '<a href="https://example.com/shop">' in html_text
    assert "<img" in html_text
    assert result["assets"]


def test_link_slot_with_no_manifest_entry_renders_unwrapped(tmp_path):
    cta_text = _text_cell("Review the toolkit", font="Arial", link_slot="review-the-toolkit")
    tree = _tree([[cta_text]])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, link_manifest={})
    assert "<a href=" not in html_text


# --- COPY-OVERFLOW GUARD (EARS-209) -------------------------------------------------------------------


def test_long_unbreakable_copy_triggers_overflow_flag_not_clipped(tmp_path):
    long_token = "A" * 300  # one giant unbreakable "word" -- no space to wrap on
    narrow_rect = _rect(0, 0, 100, 30)  # far too narrow even at minimum legible size
    cell = _text_cell(long_token, font="Arial", rect=narrow_rect)
    tree = _tree([[cell]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)

    assert result["overflow_flags"], "expected an overflow flag for an unbreakable run wider than the region"
    flag = result["overflow_flags"][0]
    assert flag["region"] == "0_0"
    # The flag reports the box the copy actually failed against: the design rect, possibly
    # widened by the Grammar G width-budget carve (right edge only -- position never moves).
    bounds = flag["bounds"]
    expected = narrow_rect.to_dict()
    assert bounds["left"] == expected["left"]
    assert bounds["top"] == expected["top"]
    assert bounds["bottom"] == expected["bottom"]
    assert bounds["right"] >= expected["right"]

    # NEVER clipped/truncated: the full string still appears verbatim in the emitted HTML.
    assert long_token in html_text
    assert "overflow:hidden" not in html_text
    assert "text-overflow" not in html_text


def test_ordinary_wrapping_copy_does_not_trigger_overflow_flag(tmp_path):
    cell = _text_cell("This is ordinary body copy that wraps across several lines just fine.", font="Arial", rect=_rect(0, 0, 300, 30))
    tree = _tree([[cell]])
    result, _html = _emit_html(tree, out_dir=tmp_path)
    assert result["overflow_flags"] == []


def test_brand_headline_raster_overflow_surfaces_in_overflow_flags(tmp_path):
    tiny_rect = _rect(0, 0, 60, 15)  # far too small for the copy below
    cell = _text_cell(
        "This headline is far too long for its original tiny box", font=BRAND_FIXTURE_FONT, rect=tiny_rect
    )
    tree = _tree([[cell]])
    result, _html = _emit_html(tree, policy="hybrid", out_dir=tmp_path)
    assert result["overflow_flags"], "expected the rasterized headline's own overflow to surface"
    assert result["overflow_flags"][0]["region"] == "0_0"


# --- backgrounds -----------------------------------------------------------------------------------


def test_row_and_cell_solid_backgrounds_render_as_bgcolor(tmp_path):
    cell = _text_cell("Body copy", font="Arial")
    cell.background = Background(color="#ff0000")
    row = Row(background=Background(color="#00ff00"), cells=[cell])
    tree = TableTree(email="Test", width=300, rows=[row])
    _result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert 'bgcolor="#00ff00"' in html_text
    assert 'bgcolor="#ff0000"' in html_text


def test_cell_background_never_emits_background_image(tmp_path):
    """Grammar G (2026-07-09): classic Outlook paints neither CSS background-image nor legacy
    `background=` (probe-verified), so an image-backed cell background must land as a flat
    bgcolor -- a flat composite resolves to its exact color, a textured one to its mean color
    with a loud `textured_background_flattened_to_color` warning. Never a background-image."""
    from PIL import Image

    composite = Image.new("RGBA", (200, 200), (7, 7, 7, 255))
    cell = _text_cell("Body copy over a band", font="Arial")
    cell.background = Background(image_source_layer_id=99)
    tree = _tree([[cell]])
    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)
    assert "background-image" not in html_text
    assert 'bgcolor="#070707"' in html_text  # the flat composite's own color, painted everywhere
    assert not any(a.startswith("assets/bg_") for a in result["assets"])
    assert "<style" not in html_text.lower()  # still inline style attrs, never a <style> block


def test_cell_background_textured_composite_flattens_to_mean_color_with_warning(tmp_path):
    """The flat-color test above only ever exercises _flat_color_of (a uniform composite, std=0).
    A genuinely textured composite (a real gradient, std>2.5) must instead resolve through
    _mean_color_of and record a loud textured_background_flattened_to_color warning naming the
    source layer_id and the flat_color actually painted -- never a `background-image`."""
    from PIL import Image

    # A horizontal gradient across the cell's exact rect (0,0,100,20): R=G=B=x*2 for x in
    # 0..99, replicated down all 20 rows. std of {0,2,4,...,198} is far above 2.5, so
    # _flat_color_of must bail (None) and the textured path must take over. Mean of that
    # arithmetic sequence is exactly 99 -> "#636363", computed here independently of the
    # source's own _mean_color_of so the test cannot be tautological against it.
    composite = Image.new("RGBA", (100, 20), (0, 0, 0, 255))
    for x in range(100):
        shade = x * 2
        for y in range(20):
            composite.putpixel((x, y), (shade, shade, shade, 255))
    expected_mean_hex = "#636363"

    cell = _text_cell("Body copy over a textured band", font="Arial")
    cell.background = Background(image_source_layer_id=99)
    tree = _tree([[cell]])
    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)

    assert "background-image" not in html_text
    assert f'bgcolor="{expected_mean_hex}"' in html_text

    textured_warnings = [w for w in result["warnings"] if w["type"] == "textured_background_flattened_to_color"]
    assert textured_warnings, "expected a textured_background_flattened_to_color warning"
    assert textured_warnings[0]["layer_id"] == 99
    assert textured_warnings[0]["flat_color"] == expected_mean_hex


def test_cell_background_image_without_composite_degrades_loud_but_safe(tmp_path):
    cell = _text_cell("Body copy over a band", font="Arial")
    cell.background = Background(image_source_layer_id=99)
    tree = _tree([[cell]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert "background-image" not in html_text
    assert result["warnings"], "expected a loud-but-safe warning when no composite is supplied for a bg image"


# --- loud-but-safe rasterizer degrade -----------------------------------------------------------------


def test_image_cell_without_composite_degrades_to_placeholder_and_records_warning(tmp_path):
    tree = _tree([[_image_cell()]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert result["warnings"], "expected a loud-but-safe warning when no composite is supplied"
    assert "<img" not in html_text  # degraded to a text placeholder, not a broken <img>
    assert result["regions"][0]["warning"] == "rasterize_failed"
    assert result["regions"][0]["render"] == "raster"


# --- nested "rows" containers -------------------------------------------------------------------------


def test_container_rows_cell_produces_a_real_nested_table_and_only_leaves_get_regions(tmp_path):
    nested = _rows_cell([Row(cells=[_text_cell("Nested body copy", font="Arial")])])
    top_merge = _text_cell("Hi [Name],", font="Arial")
    tree = _tree([[top_merge, nested]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)

    assert html_text.count("<table") >= 2  # outer grid table + the nested container's own table
    assert "Nested body copy" in html_text
    # exactly 2 leaves (top_merge + the one nested body cell) -- the container itself never gets a
    # region entry (render_role() reports containers as ROLE_CONTAINER, walked into not yielded).
    assert len(result["regions"]) == 2
    region_ids = {r["region_id"] for r in result["regions"]}
    assert region_ids == {"0_0", "0_1_rows_0_0"}


# --- bundle manifest + regions.json on-disk sidecars ---------------------------------------------------


def test_bundle_manifest_schema_matches_downstream_contract(tmp_path):
    tree = _tree([[_text_cell("Hi [Name],", font="Arial")]])
    result, _html = _emit_html(tree, out_dir=tmp_path)

    manifest_path = tmp_path / "_bundle_manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == result["bundle_manifest"]
    assert manifest["schema_version"] == 1
    assert manifest["entry_html"] == "index.html"
    assert isinstance(manifest["assets"], list)
    assert isinstance(manifest["bundle_hash"], str) and len(manifest["bundle_hash"]) == 64


def test_regions_json_written_with_expected_fields(tmp_path):
    # width=100 matches the cell's own rect.right exactly -- zero trailing slack, so the
    # WIDTH BUDGET carve (html_emitter.py's _render_row_tiled, which would otherwise widen a
    # lone text cell's td by a few px past its design rect) never fires, and the recorded
    # region rect is the cell's own untouched design rect.
    cell = _text_cell("Hi [Name],", font="Arial")
    tree = _tree([[cell]], width=100)
    result, _html = _emit_html(tree, out_dir=tmp_path)

    regions_path = tmp_path / "regions.json"
    assert regions_path.is_file()
    regions_on_disk = json.loads(regions_path.read_text(encoding="utf-8"))
    assert regions_on_disk == result["regions"]
    expected_keys = {
        "region_id", "source_layer_id", "image_source_layer_ids", "role", "render",
        "brand_font_rasterized", "editable", "link_slot", "alt", "rect",
    }
    assert expected_keys <= set(regions_on_disk[0].keys())
    assert regions_on_disk[0]["rect"] == cell.rect.to_dict()


def test_bundle_hash_stable_across_two_identical_emits(tmp_path):
    tree = _tree([[_text_cell("Hi [Name],", font="Arial")]])
    r1, _h1 = _emit_html(tree, out_dir=tmp_path / "a")
    r2, _h2 = _emit_html(tree, out_dir=tmp_path / "b")
    assert r1["bundle_manifest"]["bundle_hash"] == r2["bundle_manifest"]["bundle_hash"]


# --- the never-raster-a-protected-cell guarantee holds one more layer up (belt-and-suspenders) ---------


def test_protected_regions_are_never_render_raster_under_any_policy(tmp_path):
    merge = _text_cell("Hi [Name],", font="Arial")
    cta = _text_cell("Review the toolkit", font="Arial", link_slot="review-the-toolkit")
    body = _text_cell("Plain running body copy.", font="Arial")
    for policy in ("live", "hybrid", "raster"):
        tree = _tree([[merge, cta, body]])
        result, _html = _emit_html(tree, policy=policy, out_dir=tmp_path / policy)
        for region in result["regions"]:
            assert region["role"] in ("merge", "cta", "body")
            assert region["render"] == "live"


# --- the real Intel announcement PSD, full S1+S2 pipeline end to end -----------------------------------


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_real_announcement_psd_end_to_end_bundle_is_oft_safe(tmp_path):
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

    result = emit(routed, tmp_path, composite=composite, layer_names=layer_names)

    index_path = Path(result["index_path"])
    assert index_path.is_file()
    html_text = index_path.read_text(encoding="utf-8")
    assert DPI_LOCK_BLOCK in html_text
    assert "<img" in html_text

    low = html_text.lower()
    for pattern in _FORBIDDEN_PATTERNS:
        assert pattern.lower() not in low

    assert result["assets"], "expected real rasterized assets from the real PSD"
    for relpath in result["assets"]:
        assert (tmp_path / relpath).is_file()
        assert relpath.endswith(".png")

    manifest = json.loads((tmp_path / "_bundle_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert set(manifest["assets"]) == set(result["assets"])

    # Belt-and-suspenders on top of layer_router's own EditabilityViolation guard: every
    # merge/cta/body region in the REAL corpus rendered live, never raster.
    protected_seen = False
    for region in result["regions"]:
        if region["role"] in ("merge", "cta", "body"):
            protected_seen = True
            assert region["render"] == "live"
    assert protected_seen, "expected at least one protected region in the real announcement PSD"


# --- NO-FONT-FILE DEGRADE (a registered family with files=()) -----------------------------------


def test_no_font_file_unbreakable_overflow_uses_avg_char_width_heuristic(tmp_path):
    # No discoverable font file -> measure_text_px returns None for every probe, so
    # _has_unbreakable_overflow MUST fall back to the documented avg-char-width heuristic
    # (avg = size * AVG_CHAR_WIDTH_RATIO, max_chars = usable / avg) instead of crashing or
    # silently reporting no overflow.
    long_token = "A" * 20  # unbreakable; well over max_chars at a 60px box, size 14
    narrow_rect = _rect(0, 0, 60, 30)
    cell = _text_cell(long_token, font=NO_FILE_FIXTURE_FONT, size=14.0, rect=narrow_rect)
    tree = _tree([[cell]])
    result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert result["overflow_flags"], "expected the avg-char-width heuristic to flag this overflow"
    assert long_token in html_text  # never clipped


def test_no_font_file_fitting_token_does_not_trigger_overflow(tmp_path):
    fitting_token = "Hi"
    rect = _rect(0, 0, 60, 30)
    cell = _text_cell(fitting_token, font=NO_FILE_FIXTURE_FONT, size=14.0, rect=rect)
    tree = _tree([[cell]])
    result, _html = _emit_html(tree, out_dir=tmp_path)
    assert result["overflow_flags"] == []


def test_no_font_file_line_count_certification_fails_open_without_crashing(tmp_path):
    from psd_html.html_emitter import _word_budgeted_line_count

    # Direct contract check: with no font file discoverable, the line-count certification must
    # return None (skip), never raise and never guess via the coarse heuristic.
    assert _word_budgeted_line_count(
        "some ordinary wrapping copy", 100, font_name=NO_FILE_FIXTURE_FONT, font_size=14.0, registry=None
    ) is None

    # End-to-end: ordinary short-word wrapping copy (no single token over the heuristic's
    # max_chars) in a box too short for its natural line count -- the fail-open must mean this
    # NEVER raises and NEVER emits a "line-count certification failed" flag, because the
    # certification simply has no metrics to certify against.
    cell = _text_cell(
        "This is ordinary body copy that wraps across several lines just fine.",
        font=NO_FILE_FIXTURE_FONT, size=14.0, rect=_rect(0, 0, 300, 20),
    )
    tree = _tree([[cell]])
    result, _html = _emit_html(tree, out_dir=tmp_path)
    assert result["overflow_flags"] == []


# --- INTRA-ROW VERTICAL OFFSET (idx27) -----------------------------------------------------------


def test_intra_row_vertical_offset_applies_padding_top_to_lower_cell_only(tmp_path):
    # cellA sits flush with the row's own top; cellB's design top is 18px lower (a chip/footer-logo
    # sitting inside a taller card/band) -- only cellB may carry padding-top, and only cellB's
    # height attr grows by the offset; cellA gets neither.
    cell_a = _text_cell("A", font="Arial", rect=BBox(left=0, top=120, right=100, bottom=140))
    cell_b = _text_cell("B", font="Arial", rect=BBox(left=100, top=138, right=200, bottom=158))
    row = Row(cells=[cell_a, cell_b], rect=BBox(left=0, top=120, right=200, bottom=158))
    tree = TableTree(email="Test", width=200, rows=[row])
    _result, html_text = _emit_html(tree, out_dir=tmp_path)

    def _td_tag(region_id):
        m = re.search(rf'<td[^>]*data-region="{region_id}"[^>]*>', html_text)
        assert m is not None, f"expected a <td> for region {region_id}"
        return m.group(0)

    td_a = _td_tag("0_0")
    td_b = _td_tag("0_1")
    assert "padding-top:" not in td_a
    assert 'height="20"' in td_a
    assert "padding-top:18px;" in td_b
    assert 'height="38"' in td_b


# --- BAND PAINT PHYSICS (idx23) -------------------------------------------------------------------


def test_band_row_spacer_tds_use_certified_full_height_line_box(tmp_path):
    # Word paints a cell's shading only over its own line boxes -- a 0-tall spacer line box
    # inside a colored band strips WHITE across it. Every spacer around/between this row's cells
    # (leading, inter-cell, trailing) must carry the band color plus the certified
    # font-size:1px;line-height:{H}px;mso-line-height-rule:exactly construct, never fs0/lh0.
    cell_a = _text_cell("A", font="Arial", rect=_rect(20, 0, 100, 20))
    cell_b = _text_cell("B", font="Arial", rect=_rect(150, 0, 300, 20))
    banded_row = Row(background=Background(color="#3355AA"), cells=[cell_a, cell_b])
    tree = TableTree(email="Test", width=400, rows=[banded_row])
    _result, html_text = _emit_html(tree, out_dir=tmp_path)

    spacer_tds = re.findall(r'<td width="\d+" bgcolor="#3355AA"[^>]*>', html_text)
    assert len(spacer_tds) == 3, "expected leading + inter-cell + trailing band spacer tds"
    for td in spacer_tds:
        assert "font-size:1px;" in td
        assert re.search(r"line-height:20px;mso-line-height-rule:exactly;", td)
        assert "font-size:0;line-height:0;" not in td

    # CONTROL: the identical shape over a WHITE row keeps the zero-height form.
    white_cell_a = _text_cell("A", font="Arial", rect=_rect(20, 0, 100, 20))
    white_cell_b = _text_cell("B", font="Arial", rect=_rect(150, 0, 300, 20))
    white_row = Row(background=Background(color="#FFFFFF"), cells=[white_cell_a, white_cell_b])
    white_tree = TableTree(email="Test", width=400, rows=[white_row])
    _result2, html_text2 = _emit_html(white_tree, out_dir=tmp_path / "white")
    white_spacers = re.findall(r'<td width="\d+" style="width:\d+px;font-size:0;line-height:0;"', html_text2)
    assert len(white_spacers) == 3
    assert 'bgcolor="#FFFFFF"' not in "".join(re.findall(r'<td width="20"[^>]*>', html_text2))


def test_same_band_row_gap_fills_with_band_color_not_white_spacer(tmp_path):
    from PIL import Image

    # Two top-level rows sharing the SAME background image_source_layer_id, separated by a real
    # vertical gap -- the gap between them is part of that band (the footer's grey section reads
    # grey through the gap between its logo row and legal row), never a white split.
    composite = Image.new("RGBA", (200, 400), (51, 85, 170, 255))  # flat -> resolves to #3355AA
    row1 = Row(background=Background(image_source_layer_id=42),
               cells=[_text_cell("A", font="Arial", rect=_rect(0, 0, 100, 20))])
    row2 = Row(background=Background(image_source_layer_id=42),
               cells=[_text_cell("B", font="Arial", rect=_rect(0, 50, 100, 70))])
    tree = TableTree(email="Test", width=200, rows=[row1, row2])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)

    m = re.search(r'<td width="200" height="30" bgcolor="#3355AA"[^>]*>', html_text)
    assert m is not None, "expected the same-band inter-row gap to carry the band color"
    assert "font-size:1px;line-height:30px;mso-line-height-rule:exactly;" in m.group(0)
    assert "font-size:0;line-height:0;" not in m.group(0)


# --- BANDED SINGLE-LEAF CONSTRUCT (idx28) ---------------------------------------------------------


def test_banded_single_image_leaf_renders_as_one_td_with_band_padding(tmp_path):
    from PIL import Image

    # A colored row backing exactly one image/graphic cell renders as ONE full-width td carrying
    # the band fill (no spacer td/tr Word could stripe white), the image placed by padding alone,
    # NO height on the td (height+padding would stack and over-render the band in Word).
    band_color = "#0A141E"
    composite = Image.new("RGBA", (400, 400), (10, 20, 30, 255))  # flat -> resolves to band_color
    img_cell = Cell(role="image", rect=_rect(40, 30, 220, 70), image_source_layer_ids=[7])
    row = Row(background=Background(color=band_color), cells=[img_cell], rect=_rect(0, 0, 400, 100))
    tree = TableTree(email="Test", width=400, rows=[row])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)

    trs = re.findall(r"<tr[^>]*>", html_text)
    assert len(trs) == 1, f"expected exactly one <tr> (no spacer rows), got {trs}"
    tds = re.findall(r"<td[^>]*>", html_text)
    assert len(tds) == 1, f"expected exactly one <td> (no spacer tds), got {tds}"
    td = tds[0]
    assert f'bgcolor="{band_color}"' in td
    assert "padding:30px 0 30px 40px;" in td  # top(30) right(0) bottom(30) left(40)
    assert not re.search(r"(?<!line-)height:\d", td)  # no standalone height: prop (line-height:0 is fine)
    assert not re.search(r'\sheight="\d+"', td)
    assert re.search(r'<img[^>]*data-region="0_0"', html_text)


def test_white_band_single_image_leaf_falls_through_to_tiled_form(tmp_path):
    # CONTROL: a #FFFFFF band never uses the single-td construct -- Word's white spacer line
    # boxes are invisible on a white band anyway, so the tiled (spacer + leaf + spacer) form is
    # kept, matching the rest of the tooling's pattern-matching.
    img_cell = Cell(role="image", rect=_rect(40, 30, 220, 70), image_source_layer_ids=[7])
    row = Row(background=Background(color="#FFFFFF"), cells=[img_cell], rect=_rect(0, 0, 400, 100))
    tree = TableTree(email="Test", width=400, rows=[row])
    _result, html_text = _emit_html(tree, out_dir=tmp_path)

    tds = re.findall(r"<td[^>]*>", html_text)
    # outer wrap td + leading spacer + the leaf td + (trailing spacer if any) -- more than one td
    # and at least one plain fs0/lh0 spacer confirms the tiled form fired, not the banded single-leaf.
    assert len(tds) > 2
    assert any("font-size:0;line-height:0;" in td for td in tds)


# --- ROW BACKDROP DESIGN-PIXEL VERIFICATION (idx29) -----------------------------------------------


def test_row_backdrop_color_rejected_when_it_covers_under_half_the_design(tmp_path):
    from PIL import Image, ImageDraw

    # Full-width row (no side margins, so _row_backdrop_color's own sampling stays out of the way)
    # assigned blue, but the composite only shows blue in a SMALL inner element (well under half
    # the row strip) -- painting the assigned color would invent a band the design never shows.
    composite = Image.new("RGBA", (400, 400), (255, 255, 255, 255))
    draw = ImageDraw.Draw(composite)
    draw.rectangle([150, 10, 250, 90], fill=(0, 0, 255, 255))  # 100x80 of a 400x100 strip = 20%
    cell = _text_cell("Body", font="Arial", rect=_rect(0, 0, 400, 100))
    row = Row(background=Background(color="#0000FF"), cells=[cell], rect=_rect(0, 0, 400, 100))
    tree = TableTree(email="Test", width=400, rows=[row])
    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)

    trs = re.findall(r"<tr[^>]*>", html_text)
    assert not any("#0000FF" in tr for tr in trs), "invented full-width band must not survive"
    rejects = [w for w in result["warnings"] if w["type"] == "row_backdrop_color_rejected_by_design_pixels"]
    assert rejects, "expected a row_backdrop_color_rejected_by_design_pixels warning"
    assert rejects[0]["color"] == "#0000FF"


def test_row_backdrop_color_survives_when_it_genuinely_fills_the_strip(tmp_path):
    from PIL import Image

    composite = Image.new("RGBA", (400, 400), (0, 0, 255, 255))  # genuinely full blue
    cell = _text_cell("Body", font="Arial", rect=_rect(0, 0, 400, 100))
    row = Row(background=Background(color="#0000FF"), cells=[cell], rect=_rect(0, 0, 400, 100))
    tree = TableTree(email="Test", width=400, rows=[row])
    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)

    trs = re.findall(r"<tr[^>]*>", html_text)
    assert any('bgcolor="#0000FF"' in tr for tr in trs)
    assert not any(w["type"] == "row_backdrop_color_rejected_by_design_pixels" for w in result["warnings"])


# --- BAND EXTENT (leading/trailing band-fill strips) (idx30) --------------------------------------


def test_band_extent_fills_leading_and_trailing_strips_and_degrades_loudly_on_bad_bbox(tmp_path):
    from PIL import Image

    import psd_html.html_emitter as he

    def _row(top, bottom, width=400):
        cell = _text_cell("Body", font="Arial", rect=BBox(left=0, top=top, right=width, bottom=bottom))
        return Row(background=Background(image_source_layer_id=99), cells=[cell],
                   rect=BBox(left=0, top=top, right=width, bottom=bottom))

    tree = TableTree(email="Test", width=400, rows=[_row(30, 60)])
    routed = route(tree, "hybrid")

    class _StubLayer:
        bbox = (0, 0, 400, 100)  # design extent taller than the row's own rect -- (0,100) vs (30,60)

    flat = Image.new("RGBA", (50, 50), (34, 68, 102, 255))
    orig_composite_layer = he.composite_layer
    # composite_layer now returns (image, reason) so the caller can surface WHY a composite failed;
    # a successful composite is (image, None).
    he.composite_layer = lambda layer, viewport: (flat, None)
    try:
        ctx = he._EmitContext(
            out_root=tmp_path, assets_dir=tmp_path / "assets", assets_subdir="assets",
            copy_manifest=None, link_manifest=None, composite=None, layer_names=None,
            registry=None, layer_index={99: _StubLayer()}, density=1.0,
        )
        html_frag = he._render_stacked_rows(tree.rows, 0, 0, 400, (), routed, ctx)
    finally:
        he.composite_layer = orig_composite_layer

    band_fill_trs = re.findall(r'<tr><td width="400" height="\d+" bgcolor="#224466"[^>]*>&nbsp;</td></tr>', html_frag)
    assert len(band_fill_trs) == 2, "expected a leading AND a trailing band-fill strip"
    leading, trailing = band_fill_trs
    assert 'height="30"' in leading  # row top(30) - band top(0)
    assert 'height="40"' in trailing  # band bottom(100) - row bottom(60)
    for tr in band_fill_trs:
        assert "font-size:1px;" in tr and "mso-line-height-rule:exactly;" in tr
    assert not ctx.warnings

    # DEGRADE: a stub layer whose .bbox is non-indexable (a psd-tools API break, or any bug in the
    # strip math's own input) must record band_extent_unavailable and emit NO band strips -- never
    # crash, never silently drop the fill without saying so.
    class _BrokenLayer:
        bbox = None

    ctx2 = he._EmitContext(
        out_root=tmp_path, assets_dir=tmp_path / "assets", assets_subdir="assets",
        copy_manifest=None, link_manifest=None, composite=None, layer_names=None,
        registry=None, layer_index={99: _BrokenLayer()}, density=1.0,
    )
    tree2 = TableTree(email="Test", width=400, rows=[_row(30, 60)])
    routed2 = route(tree2, "hybrid")
    html_frag2 = he._render_stacked_rows(tree2.rows, 0, 0, 400, (), routed2, ctx2)
    assert "#224466" not in html_frag2
    assert any(w["type"] == "band_extent_unavailable" and w["layer_id"] == 99 for w in ctx2.warnings)


# --- FROZEN-BREAK MACHINERY (idx31) ----------------------------------------------------------------


def test_frozen_break_lands_at_word_boundary_and_rejects_when_shrink_exceeds_threshold(tmp_path):
    from psd_html.html_emitter import _design_line_breaks, _EmitContext, _hard_line_shrink_or_reject

    # ACCEPT: a hand-built 2-line paragraph whose natural greedy wrap (real Arial glyph metrics)
    # matches the design box's own line count -- the freeze must land the <br/> at the whitespace
    # boundary between the two lines (never mid-word) and the copy must round-trip verbatim.
    content = "Review the updated onboarding guide today"
    cell = Cell(role="text", rect=_rect(0, 0, 150, 35),
                text=TextInfo(content=content, align="left",
                              runs=[TextRun(font="Arial", size=14.0, color="#000000", length=len(content))]))
    tree = TableTree(email="Test", width=200, rows=[Row(cells=[cell])])
    _result, html_text = _emit_html(tree, out_dir=tmp_path / "accept")
    m = re.search(r'<td data-region="0_0"[^>]*>(.*?)</td>', html_text, re.S)
    body = m.group(1)
    assert body.count("<br/>") == 1
    before, after = body.split("<br/>")
    assert before and after
    assert before[-1] != " " and after[0] != " "  # break sits ON the space, not duplicated
    assert f"{before} {after}" == content  # verbatim round-trip once the frozen break is stripped

    # REJECT: the frozen longest line is a single giant unbreakable word needing far more than a
    # 20% shrink to fit its column -- _hard_line_shrink_or_reject must abandon the freeze so the
    # emitter falls back to natural wrap (no <br/>), never shipping an impossibly-shrunk hard line.
    reject_content = "Supercalifragilisticexpialidocious short"
    reject_cell = Cell(
        role="text", rect=_rect(0, 0, 100, 35),
        text=TextInfo(content=reject_content, align="left",
                      runs=[TextRun(font="Arial", size=14.0, color="#000000", length=len(reject_content))]),
    )
    ctx = _EmitContext(out_root=tmp_path, assets_dir=tmp_path / "assets", assets_subdir="assets",
                       copy_manifest=None, link_manifest=None, composite=None, layer_names=None,
                       registry=None, density=1.0)
    hard = _design_line_breaks(reject_cell, reject_content, ctx, 100)
    assert hard is not None, "expected the freeze to succeed (matches the 2-line design box)"
    assert _hard_line_shrink_or_reject(reject_cell, hard, ctx) == "reject"

    reject_tree = TableTree(email="Test", width=150, rows=[Row(cells=[reject_cell])])
    _result2, html_text2 = _emit_html(reject_tree, out_dir=tmp_path / "reject")
    assert "<br/>" not in html_text2
    assert reject_content in html_text2  # never clipped even though it can't fit at all


# --- SHRINK-TO-FIT BRANCHES (idx32) -----------------------------------------------------------------


def test_shrink_to_fit_single_line_and_multiline_branches(tmp_path):
    from psd_html.html_emitter import MIN_LEGIBLE_FONT_SIZE

    def _font_size_of(html_text, region_id="0_0"):
        m = re.search(rf'<td data-region="{region_id}"[^>]*style="([^"]*)"', html_text)
        assert m is not None
        fs = re.search(r"font-size:([\d.]+)px", m.group(1))
        assert fs is not None
        return float(fs.group(1))

    # SINGLE-LINE: copy just exceeds the box at design size -- shrinks strictly below the design
    # size but never below MIN_LEGIBLE_FONT_SIZE, and the shrunk size must genuinely fit (no
    # overflow flag).
    content_1l = "Quarterly Regional Overview"
    cell_1l = Cell(role="text", rect=_rect(0, 0, 190, 20),
                   text=TextInfo(content=content_1l, align="left",
                                 runs=[TextRun(font="Arial", size=16.0, color="#000000")]))
    tree_1l = TableTree(email="Test", width=250, rows=[Row(cells=[cell_1l])])
    result_1l, html_1l = _emit_html(tree_1l, out_dir=tmp_path / "single")
    fs_1l = _font_size_of(html_1l)
    assert MIN_LEGIBLE_FONT_SIZE <= fs_1l < 16.0
    assert result_1l["overflow_flags"] == []

    # MULTI-LINE: natural wrap at the design size needs MORE lines than the design box -- shrinks
    # iteratively until the Word-budgeted wrap holds the design's own line count, never wrapping
    # to a phantom extra line, no overflow flag.
    content_ml = "Annual performance review meeting notes today"
    cell_ml = Cell(role="text", rect=_rect(0, 0, 170, 35),
                   text=TextInfo(content=content_ml, align="left",
                                 runs=[TextRun(font="Arial", size=16.0, color="#000000")]))
    tree_ml = TableTree(email="Test", width=220, rows=[Row(cells=[cell_ml])])
    result_ml, html_ml = _emit_html(tree_ml, out_dir=tmp_path / "multi")
    assert "<br/>" not in html_ml  # natural wrap, not a frozen hard break
    fs_ml = _font_size_of(html_ml)
    assert MIN_LEGIBLE_FONT_SIZE <= fs_ml < 16.0
    assert result_ml["overflow_flags"] == []


# --- SIBLING-CONSISTENT TYPE SIZING (idx33) ---------------------------------------------------------


def test_sibling_stat_cells_harmonize_to_one_shared_font_size_and_line_height(tmp_path):
    # Three sibling stat cells, SAME font/max-run size, different copy/rect widths that would each
    # independently need a DIFFERENT shrink factor -- _harmonize_shrink must move them all together
    # to the group's smallest factor so the design's x-height stays consistent across the row.
    def _stat_cell(content, rect_):
        return Cell(role="text", rect=rect_,
                    text=TextInfo(content=content, align="left",
                                  runs=[TextRun(font="Arial", size=40.0, color="#000000")]))

    c1 = _stat_cell("88%", _rect(0, 0, 80, 50))
    c2 = _stat_cell("1,024", _rect(80, 0, 175, 50))
    c3 = _stat_cell("Q3 2026", _rect(175, 0, 315, 50))
    row = Row(cells=[c1, c2, c3])
    tree = TableTree(email="Test", width=315, rows=[row])
    result, html_text = _emit_html(tree, out_dir=tmp_path)
    assert result["overflow_flags"] == []  # all three fit once shrunk

    sizes, line_heights = set(), set()
    for region_id in ("0_0", "0_1", "0_2"):
        m = re.search(rf'<td data-region="{region_id}"[^>]*style="([^"]*)"', html_text)
        assert m is not None
        style = m.group(1)
        sizes.add(re.search(r"font-size:([\d.]+)px", style).group(1))
        line_heights.add(re.search(r"line-height:([\d.]+)px", style).group(1))
    assert len(sizes) == 1, f"expected one shared font-size across siblings, got {sizes}"
    assert len(line_heights) == 1, f"expected one shared line-height across siblings, got {line_heights}"
    assert float(next(iter(sizes))) < 40.0  # the group actually shrank, not a no-op


# --- PARAGRAPH BLOCKS: SpaceAfter margins, blank lines, bullet hanging indent (idx34) ---------------


def test_paragraph_blocks_space_after_blank_line_and_bullet_hanging_indent(tmp_path):
    para1 = "First paragraph line."
    para3 = "\u2022\tBullet item text"  # designer bullet: U+2022 escape + tab (kept file ASCII-only)
    para4 = "Final paragraph line."
    content = f"{para1}\n\n{para3}\n{para4}"
    paragraphs = [
        {"length": len(para1) + 1, "space_after": 8.5},
        {"length": 1, "space_after": 0.0},  # the blank line
        {"length": len(para3) + 1, "space_after": 8.5},
        {"length": len(para4), "space_after": 8.5},  # LAST -- must still render "0" regardless
    ]
    text = TextInfo(
        content=content, align="left",
        runs=[TextRun(font="Arial", size=14.0, color="#000000", length=len(content))],
        paragraphs=paragraphs,
    )
    cell = Cell(role="text", rect=_rect(0, 0, 300, 150), text=text)
    tree = TableTree(email="Test", width=300, rows=[Row(cells=[cell])])
    _result, html_text = _emit_html(tree, out_dir=tmp_path)
    m = re.search(r'<td data-region="0_0"[^>]*>(.*?)</td>', html_text, re.S)
    body = m.group(1)

    assert '<p style="margin:0 0 8.5px 0;">First paragraph line.</p>' in body
    assert '<p style="margin:0 0 0 0;">&nbsp;</p>' in body  # blank line, zero-sa
    assert "text-indent:-36px" in body and "margin:0 0 8.5px 36px" in body  # bullet hanging indent
    assert "\u2022&nbsp;" in body  # rebuilt bullet + nbsp gap run
    assert "Bullet item text" in body
    assert '<p style="margin:0 0 0 0;">Final paragraph line.</p>' in body  # LAST -> forced "0"


# --- WHITE-COPY (INVISIBLE TEXT) SAFETY GUARD (idx35) -----------------------------------------------


def test_white_text_over_dark_composite_gets_sampled_dark_bgcolor(tmp_path):
    from PIL import Image

    dark_composite = Image.new("RGBA", (150, 200), (20, 20, 20, 255))
    cell = Cell(role="text", rect=_rect(0, 0, 150, 20),
                text=TextInfo(content="Invisible copy", align="left",
                              runs=[TextRun(font="Arial", size=14.0, color="#FFFFFF")]))
    tree = TableTree(email="Test", width=150, rows=[Row(cells=[cell])])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, composite=dark_composite)
    m = re.search(r'<td data-region="0_0"[^>]*>', html_text)
    assert 'bgcolor="#141414"' in m.group(0)


def test_white_text_over_light_composite_gets_no_bgcolor(tmp_path):
    from PIL import Image

    light_composite = Image.new("RGBA", (150, 200), (245, 245, 245, 255))
    cell = Cell(role="text", rect=_rect(0, 0, 150, 20),
                text=TextInfo(content="Visible copy", align="left",
                              runs=[TextRun(font="Arial", size=14.0, color="#FFFFFF")]))
    tree = TableTree(email="Test", width=150, rows=[Row(cells=[cell])])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, composite=light_composite)
    m = re.search(r'<td data-region="0_0"[^>]*>', html_text)
    assert "bgcolor=" not in m.group(0)


# --- DIVIDER CONSTRUCT (idx37) -----------------------------------------------------------------------


def test_thin_wide_image_cell_renders_as_border_not_raster_image(tmp_path):
    from PIL import Image, ImageDraw

    composite = Image.new("RGBA", (400, 400), (255, 255, 255, 255))
    draw = ImageDraw.Draw(composite)
    draw.line([(0, 101), (300, 101)], fill=(60, 60, 60, 255), width=2)

    divider = Cell(role="image", rect=_rect(0, 100, 300, 104), image_source_layer_ids=[3])
    tree = TableTree(email="Test", width=300, rows=[Row(cells=[divider])])
    result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)

    m = re.search(r'<td[^>]*data-region="0_0"[^>]*>', html_text)
    td = m.group(0)
    assert "<img" not in html_text
    assert re.search(r"border-bottom:[1-3]px solid #[0-9A-Fa-f]{6};", td)
    assert re.search(r"mso-border-bottom-alt:solid #[0-9A-Fa-f]{6} [1-3]px;", td)
    assert result["regions"][0]["render"] == "raster"


def test_thick_image_cell_control_still_renders_as_img(tmp_path):
    from PIL import Image

    composite = Image.new("RGBA", (400, 400), (255, 255, 255, 255))
    img_cell = Cell(role="image", rect=_rect(0, 0, 300, 40), image_source_layer_ids=[3])
    tree = TableTree(email="Test", width=300, rows=[Row(cells=[img_cell])])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, composite=composite)
    assert "<img" in html_text


# --- DENSITY / RETINA SCALING (idx24) -----------------------------------------------------------------


def test_density_2x_preserves_superscript_underline_and_paragraph_spacing(tmp_path):
    # Two paragraphs (a real \n paragraph mark) so the SpaceAfter gap between them actually
    # renders as a <p> margin -- inter-paragraph SpaceAfter only applies BETWEEN paragraphs, so a
    # single-paragraph fixture never emits one. The first paragraph carries the superscript
    # footnote + an underlined run; the second is plain body copy.
    p1 = "Adopted AI1 footnote"
    content = p1 + "\nSecond paragraph"
    runs = [
        TextRun(font="Arial", size=48.0, color="#000000", length=len("Adopted AI"), baseline=0),
        TextRun(font="Arial", size=48.0, color="#000000", length=len("1"), baseline=1),
        TextRun(font="Arial", size=48.0, color="#000000", length=len(" footnote"), baseline=0, underline=True),
        TextRun(font="Arial", size=48.0, color="#000000", length=len("\nSecond paragraph"), baseline=0),
    ]
    text = TextInfo(content=content, align="left", runs=runs,
                    paragraphs=[{"length": len(p1) + 1, "space_after": 17.0},
                                {"length": len("Second paragraph"), "space_after": 0.0}])
    cell = Cell(role="text", rect=BBox(left=0, top=0, right=800, bottom=240), text=text)
    tree = TableTree(email="Test", width=800, rows=[Row(cells=[cell])])
    _result, html_text = _emit_html(tree, out_dir=tmp_path, density=2.0)
    m = re.search(r'<td data-region="0_0"[^>]*>(.*?)</td>', html_text, re.S)
    body = m.group(1)
    assert "<sup" in body, "the baseline-1 footnote run must still render as <sup> at density=2.0"
    assert "text-decoration:underline;" in body, "the underlined run must survive density scaling"
    # SpaceAfter is px in PSD space; at density=2.0 it scales /2 exactly like the font size
    # (48->24), so the 17px design gap must render as an 8.5px margin -- present AND scaled, not
    # dropped (the old bug) and not left at the unscaled 17.
    assert "margin:0 0 8.5" in body, "the paragraph's SpaceAfter margin must survive density scaling AND be scaled /d"


def test_styled_run_spanning_paragraph_break_stays_balanced_per_block(tmp_path):
    # A NON-dominant styled run (its own <span>) that spans a Photoshop paragraph mark must not
    # emit a <span> straddling the <p> split _render_paragraph_blocks makes -- a wrapper crossing a
    # </p><p> boundary is structurally invalid and renders unpredictably in Word. The wrapper must
    # close and reopen per paragraph, so every <p> block has balanced span/sup/sub tags.
    content = "Xddd\nEEEfff"
    runs = [
        TextRun(font="Arial", size=20.0, color="#000000", length=1, baseline=0),  # dominant (20px)
        TextRun(font="Arial", size=40.0, color="#000000", length=len("ddd\nEEE"), baseline=0),  # spans the \n
        TextRun(font="Arial", size=20.0, color="#000000", length=3, baseline=0),
    ]
    text = TextInfo(content=content, align="left", runs=runs,
                    paragraphs=[{"length": len("Xddd") + 1, "space_after": 12.0},
                                {"length": len("EEEfff"), "space_after": 0.0}])
    cell = Cell(role="text", rect=BBox(left=0, top=0, right=600, bottom=140), text=text)
    tree = TableTree(email="Test", width=600, rows=[Row(cells=[cell])])
    _result, html_text = _emit_html(tree, out_dir=tmp_path)
    body = re.search(r'<td data-region="0_0"[^>]*>(.*?)</td>', html_text, re.S).group(1)
    blocks = re.findall(r"<p [^>]*>(.*?)</p>", body, re.S)
    assert len(blocks) == 2, f"expected two <p> paragraph blocks, got: {body}"
    for b in blocks:
        assert b.count("<span") == b.count("</span>"), f"a <span> crosses a <p> boundary: {body}"
        assert b.count("<sup") == b.count("</sup>"), f"a <sup> crosses a <p> boundary: {body}"


def test_missing_font_file_emits_plain_degrade_warning_once(tmp_path, monkeypatch):
    """S1.1 degrade plainspeak: when the font file isn't installed, line-count certification is
    skipped -- previously silent. It now emits a plain-language warning (no jargon), once per font."""
    import warnings as _w

    import psd_html.html_emitter as html_emitter_mod

    # Force "font file not installed": every glyph measurement returns None, so
    # _word_budgeted_line_count returns None and the cert is skipped.
    monkeypatch.setattr(html_emitter_mod, "measure_text_px", lambda *a, **k: None)
    # Two live body-text cells in the SAME font -> the warning must fire ONCE (deduped per font).
    tree = _tree([[_text_cell("Some body copy that would wrap", font="Segoe UI", size=16.0)],
                  [_text_cell("More body copy that would wrap", font="Segoe UI", size=16.0)]])
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        _emit_html(tree, policy="live", out_dir=tmp_path)
    hits = [str(w.message) for w in caught if "not installed on this machine" in str(w.message)]
    assert len(hits) == 1, f"expected one deduped degrade warning, got {len(hits)}: {hits}"
    assert "check the capture proof" in hits[0]  # plain-language action, no 'line-count cert' jargon
