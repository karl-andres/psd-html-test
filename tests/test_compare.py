"""Test the hands-on comparison harness (C-COMPARE) on the real announcement PSD."""
from __future__ import annotations

import pathlib

import pytest

from psd_html.compare import build_comparison

_REPO = pathlib.Path(__file__).resolve().parents[3]
_ANN = _REPO / "Reference" / "2413101_Intel" / "PSDs" / \
    "Intel x Microsoft_Commercial Refresh_announcement email" / "Intel_MsfT_Global BoM_Announcement Email.psd"
_ann_only = pytest.mark.skipif(not _ANN.is_file(), reason="announcement corpus PSD not present on this host")


@_ann_only
def test_build_comparison_assembles_all_panels(tmp_path):
    result = build_comparison(str(_ANN), str(tmp_path / "cmp"))
    out = tmp_path / "cmp"

    # the three bundles + preview + compare page + proof all landed
    for policy in ("live", "hybrid", "raster"):
        assert (out / policy / "index.html").is_file(), f"missing {policy} bundle"
    assert (out / "preview" / "preview.html").is_file()
    assert (out / "proof.jpg").is_file(), "announcement PSD has a sibling .jpg proof -- should be copied"
    assert pathlib.Path(result["compare_path"]).is_file()

    html = pathlib.Path(result["compare_path"]).read_text(encoding="utf-8")
    # the page frames every panel
    assert 'src="preview/preview.html"' in html
    for policy in ("live", "hybrid", "raster"):
        assert f'src="{policy}/index.html"' in html
    assert 'src="proof.jpg"' in html
    # and surfaces the bake-off verdict + the D6 next-step
    assert "editability_clean=True" in html
    assert "CLASSIC Outlook" in html

    # the underlying bake-off is genuinely clean
    assert result["report"]["editability_clean"] is True
    assert result["report"]["geometry_identical"] is True
