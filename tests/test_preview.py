"""Tests for the faithful absolute-positioned diagnostic preview (C-PREVIEW).

Independent of the OFT-safe table emitter -- exercises the real announcement PSD end to end
(psd_to_layout_tree -> classify -> composite -> overlay HTML). Assertions check observable output
(the written preview.html + assets), not the renderer's own internals.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from psd_html.preview import PreviewError, render_preview

_REPO = pathlib.Path(__file__).resolve().parents[3]
_ANN = _REPO / "Reference" / "2413101_Intel" / "PSDs" / \
    "Intel x Microsoft_Commercial Refresh_announcement email" / "Intel_MsfT_Global BoM_Announcement Email.psd"

_have_ann = _ANN.is_file()
_ann_only = pytest.mark.skipif(not _have_ann, reason="announcement corpus PSD not present on this host")


def test_missing_psd_raises(tmp_path):
    with pytest.raises(PreviewError):
        render_preview(str(tmp_path / "nope.psd"), str(tmp_path / "out"))


@_ann_only
def test_announcement_preview_renders_faithfully(tmp_path):
    out = tmp_path / "prev"
    result = render_preview(str(_ANN), str(out))

    # a preview.html + at least one composite crop are written
    assert os.path.isfile(result["preview_path"])
    assert result["artboards"] >= 1
    assert result["composite_available"] is True
    assert len(result["assets"]) >= 1
    for rel in result["assets"]:
        assert (out / rel).is_file()

    html = pathlib.Path(result["preview_path"]).read_text(encoding="utf-8")
    # the composite background image is referenced
    assert "preview_0.png" in html
    # the overlay reflects REAL classification (not an empty page): the announcement carries merge
    # fields ([First Name] + the sender-choice token), imagery, and live body copy, so those region
    # kinds must all appear as overlay boxes. (NB: this is CLASSIFIER-level -- a "CTA" is a
    # router-level notion via link_slot; the announcement's "Review the toolkit" is authored as
    # live text on a colored band, not a `... button` group, so it reads as text+background here.)
    assert 'data-kind="editable"' in html, "expected >=1 editable merge field ([First Name]/sender-choice)"
    assert 'data-kind="image"' in html, "expected image regions (laptop / chip / tablet)"
    assert 'data-kind="text"' in html, "expected live body-copy regions"
    # and this is explicitly NOT presented as the shippable Outlook output
    assert "NOT the Outlook output" in html


# --- _kind_of role->kind mapping: the branches the real-PSD test doesn't assert (idx 61) --------

from types import SimpleNamespace  # noqa: E402

from psd_html import preview as _preview  # noqa: E402
from psd_html.layer_classifier import (  # noqa: E402
    ROLE_BACKGROUND,
    ROLE_BUTTON,
    ROLE_CONTENT,
    ROLE_GRAPHIC,
    ROLE_HIGHLIGHT,
)
from psd_html.preview import _kind_of  # noqa: E402


def _item(role, *, is_text=False, editable=False):
    return SimpleNamespace(role=role, is_text=is_text, editable=editable)


def test_kind_of_covers_every_role_branch():
    assert _kind_of(_item(ROLE_GRAPHIC)) == "graphic"
    assert _kind_of(_item(ROLE_BUTTON)) == "cta"
    assert _kind_of(_item(ROLE_BACKGROUND)) == "background"
    assert _kind_of(_item(ROLE_HIGHLIGHT)) == "field"
    assert _kind_of(_item(ROLE_CONTENT, is_text=True, editable=True)) == "editable"
    assert _kind_of(_item(ROLE_CONTENT, is_text=True, editable=False)) == "text"
    assert _kind_of(_item(ROLE_CONTENT, is_text=False)) == "image"


# --- composite-unavailable degrade: loud banner, no crops, geometry still shown (idx 62) --------


@_ann_only
def test_preview_composite_unavailable_degrades_loud_but_safe(tmp_path, monkeypatch):
    # PSD opens fine, but psd-tools cannot flatten it: _composite_image returns (None, reason).
    monkeypatch.setattr(_preview, "_composite_image", lambda psd_path: (None, "simulated flatten failure"))
    out = tmp_path / "prev"
    result = render_preview(str(_ANN), str(out))

    assert result["composite_available"] is False
    assert result["assets"] == []            # no crops written when there is no composite
    assert os.path.isfile(result["preview_path"])
    html = pathlib.Path(result["preview_path"]).read_text(encoding="utf-8")
    assert "composite unavailable" in html   # the loud red banner
    assert "data-kind=" in html              # region-box overlays still emitted (geometry inspectable)
