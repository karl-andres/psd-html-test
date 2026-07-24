"""C-VALIDATOR tests.

Hand-built bundle fixtures (a compliant one, and one deliberately-broken variant per forbidden
construct) PLUS the real emitter's own output on the announcement PSD, end to end. No tautological
asserts: every case is checked against a hand-picked expected violation/pass shape, never against
the function's own output on the same input.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from psd_html.conformance_validator import (
    ConformanceError,
    assert_bundle,
    validate_bundle,
    write_manifest,
)
from psd_html.html_emitter import DPI_LOCK_BLOCK

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


def _tiny_png_bytes() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _write_compliant_bundle(root: Path) -> Path:
    """A hand-built, minimal, fully-compliant bundle: index.html (DPI-lock block, two <img src>
    tags, bgcolor + background-color flat fill -- Grammar G forbids background-image, so a
    compliant bundle carries none) + two real PNG assets on disk + a manifest + regions.json."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "hero.png").write_bytes(_tiny_png_bytes())
    (root / "assets" / "band.png").write_bytes(_tiny_png_bytes())

    html = f"""<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
{DPI_LOCK_BLOCK}
<title>Compliant</title>
</head>
<body style="margin:0;padding:0;">
<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="600">
<tr>
<td bgcolor="#ffffff" style="background-color:#ffffff;">
<img src="assets/hero.png" width="100" height="100" alt="Hero">
<img src="assets/band.png" width="100" height="20" alt="Band">
<a href="https://example.com/shop" style="color:#000;">Shop Now</a>
</td>
</tr>
</table>
</body>
</html>
"""
    (root / "index.html").write_text(html, encoding="utf-8")
    (root / "regions.json").write_text(json.dumps([]), encoding="utf-8")
    write_manifest(root)
    return root


# --- the compliant bundle passes ------------------------------------------------------------------


def test_compliant_bundle_passes_with_no_violations(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    result = validate_bundle(bundle)
    assert result["violations"] == []
    assert result["pass"] is True


def test_compliant_bundle_assert_bundle_does_not_raise(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    result = assert_bundle(bundle)
    assert result["pass"] is True


# --- each forbidden construct is a precise FAIL ---------------------------------------------------


def test_svg_reference_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    (bundle / "assets" / "hero.svg").write_text("<svg></svg>", encoding="utf-8")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace('src="assets/hero.png"', 'src="assets/hero.svg"')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "svg_reference" in types
    messages = " ".join(v["message"] for v in result["violations"])
    assert "hero.svg" in messages


def test_webp_asset_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    (bundle / "assets" / "extra.webp").write_bytes(b"not-a-real-webp-but-extension-is-what-matters")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "disallowed_asset_extension" in types
    messages = " ".join(v["message"] for v in result["violations"])
    assert "extra.webp" in messages


def test_srcset_attribute_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        '<img src="assets/hero.png" width="100" height="100" alt="Hero">',
        '<img src="assets/hero.png" srcset="assets/hero.png 1x" width="100" height="100" alt="Hero">',
    )
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "srcset_forbidden" in types


def test_link_rel_stylesheet_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace("<title>", '<link rel="stylesheet" href="theme.css"><title>')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "link_tag_forbidden" in types


def test_at_import_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace("<title>", "<style>@import url('theme.css');</style><title>")
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "import_forbidden" in types
    assert "style_block_forbidden" in types  # the wrapping <style> tag is its own violation too


def test_style_block_alone_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace("<title>", "<style>td { background: #fff; }</style><title>")
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "style_block_forbidden" in types


def test_background_html_attribute_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace("<td bgcolor=", '<td background="assets/band.png" bgcolor=')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "background_attribute_forbidden" in types


def test_href_with_background_query_param_is_not_flagged(tmp_path):
    # An href whose URL query carries `background=` (e.g. a tracking param) is NOT the forbidden
    # legacy `background=` tag attribute -- the anchored check must not hard-reject a valid bundle.
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'href="https://example.com/shop"',
        'href="https://example.com/shop?background=1"',
    )
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    types = {v["type"] for v in result["violations"]}
    assert "background_attribute_forbidden" not in types
    assert result["pass"] is True


def test_real_background_attribute_flagged_even_with_href_background_query(tmp_path):
    # Control: a genuine `<td background="...">` attribute IS still flagged even when an href
    # elsewhere legitimately carries `?background=` in its query.
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'href="https://example.com/shop"',
        'href="https://example.com/shop?background=1"',
    )
    html = html.replace("<td bgcolor=", '<td background="assets/band.png" bgcolor=')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "background_attribute_forbidden" in types


def test_vml_tag_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace("<title>", '<v:roundrect fillcolor="#fff"></v:roundrect><title>')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "vml_forbidden" in types


def test_out_of_root_asset_path_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    # a real file that genuinely sits outside the bundle root, referenced by traversal.
    (tmp_path / "outside.png").write_bytes(_tiny_png_bytes())
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace('src="assets/hero.png"', 'src="../outside.png"')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert ("unsafe_asset_path" in types) or ("asset_out_of_root" in types)
    messages = " ".join(v["message"] for v in result["violations"])
    assert "outside.png" in messages


def test_img_without_src_attribute_fails(tmp_path):
    # The img-missing-src branch (src_match is None) is a distinct code path from the tested
    # forbidden-construct scans; an <img> with no src at all must be flagged.
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace('<img src="assets/hero.png" width="100" height="100" alt="Hero">', '<img alt="x">')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    assert "img_missing_src" in {v["type"] for v in result["violations"]}


def test_href_to_local_svg_fails(tmp_path):
    # The <a href="...svg"> ban is a SEPARATE scan from the img/style .svg checks; exercise it.
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="https://example.com/shop"', 'href="docs/guide.svg"')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    assert "svg_reference" in {v["type"] for v in result["violations"]}


def test_missing_dpi_lock_block_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace(DPI_LOCK_BLOCK, "")
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "missing_dpi_lock_block" in types


# --- more structural failures --------------------------------------------------------------------


def test_missing_entry_html_fails(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "assets").mkdir()
    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "missing_entry" in types


def test_entry_not_utf8_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    (bundle / "index.html").write_bytes(b"\xff\xfe\x00broken")
    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "entry_not_utf8" in types


def test_missing_asset_file_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace('src="assets/hero.png"', 'src="assets/does_not_exist.png"')
    (bundle / "index.html").write_text(html, encoding="utf-8")

    result = validate_bundle(bundle)
    assert result["pass"] is False
    types = {v["type"] for v in result["violations"]}
    assert "asset_missing" in types


def test_assert_bundle_raises_conformance_error_naming_violations(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace(DPI_LOCK_BLOCK, "")
    (bundle / "index.html").write_text(html, encoding="utf-8")

    with pytest.raises(ConformanceError) as excinfo:
        assert_bundle(bundle)
    assert "dpi" in str(excinfo.value).lower() or "DPI" in str(excinfo.value)


def test_validate_bundle_never_raises_even_on_a_totally_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = validate_bundle(empty)  # must not raise
    assert result["pass"] is False


def test_validate_bundle_never_raises_on_nonexistent_dir(tmp_path):
    result = validate_bundle(tmp_path / "does_not_exist_at_all")
    assert result["pass"] is False
    assert result["violations"]


# --- manifest -------------------------------------------------------------------------------------


def test_write_manifest_has_expected_schema(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    manifest = write_manifest(bundle)
    assert manifest["schema_version"] == 1
    assert manifest["entry_html"] == "index.html"
    assert manifest["assets"] == sorted(manifest["assets"])
    assert isinstance(manifest["bundle_hash"], str) and len(manifest["bundle_hash"]) == 64

    on_disk = json.loads((bundle / "_bundle_manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest


def test_write_manifest_same_content_same_hash(tmp_path):
    bundle_a = _write_compliant_bundle(tmp_path / "a")
    bundle_b = _write_compliant_bundle(tmp_path / "b")
    manifest_a = write_manifest(bundle_a)
    manifest_b = write_manifest(bundle_b)
    assert manifest_a["bundle_hash"] == manifest_b["bundle_hash"]


def test_write_manifest_different_content_different_hash(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    manifest_before = write_manifest(bundle)
    html = (bundle / "index.html").read_text(encoding="utf-8")
    html = html.replace("Shop Now", "Buy Now")
    (bundle / "index.html").write_text(html, encoding="utf-8")
    manifest_after = write_manifest(bundle)
    assert manifest_before["bundle_hash"] != manifest_after["bundle_hash"]


def test_write_manifest_raises_conformance_error_with_no_html(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConformanceError):
        write_manifest(empty)


# --- manifest CORRUPTION (external tamper): write_manifest always emits valid, so only a corrupted
#     _bundle_manifest.json reaches these branches; the downstream Service trusts this gate. --------


def test_corrupt_manifest_invalid_json_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    (bundle / "_bundle_manifest.json").write_text("{not valid json", encoding="utf-8")
    result = validate_bundle(bundle)
    assert result["pass"] is False
    assert "invalid_manifest_json" in {v["type"] for v in result["violations"]}


def test_corrupt_manifest_wrong_schema_version_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    manifest = json.loads((bundle / "_bundle_manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    (bundle / "_bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_bundle(bundle)
    assert result["pass"] is False
    assert "invalid_manifest" in {v["type"] for v in result["violations"]}


def test_corrupt_manifest_missing_required_key_fails(tmp_path):
    bundle = _write_compliant_bundle(tmp_path / "bundle")
    manifest = json.loads((bundle / "_bundle_manifest.json").read_text(encoding="utf-8"))
    del manifest["bundle_hash"]
    (bundle / "_bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_bundle(bundle)
    assert result["pass"] is False
    assert "invalid_manifest" in {v["type"] for v in result["violations"]}


# --- the real emitter's own output on the announcement PSD (hybrid) -> pass -----------------------


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_real_emitter_hybrid_bundle_on_announcement_psd_passes(tmp_path):
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

    emit(routed, tmp_path, composite=composite, layer_names=layer_names)

    result = validate_bundle(tmp_path)
    assert result["violations"] == [], f"unexpected violations on real emitter output: {result['violations']}"
    assert result["pass"] is True

    # assert_bundle must agree and not raise.
    assert_bundle(tmp_path)


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_real_emitter_bundle_manifest_matches_written_manifest(tmp_path):
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

    result = emit(routed, tmp_path, composite=composite, layer_names=layer_names)
    emitted_manifest = result["bundle_manifest"]

    recomputed = write_manifest(tmp_path)
    assert recomputed["bundle_hash"] == emitted_manifest["bundle_hash"]
    assert set(recomputed["assets"]) == set(emitted_manifest["assets"])
