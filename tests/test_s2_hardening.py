"""S2 post-verify hardening round (2026-07-09).

Regression tests locking the fixes for the medium/low findings the four Opus adversarial-verify
lenses raised against the first S2 build. Each test proves the gap the verifier described is now
closed, and pairs it with a control proving the fix did not introduce a false positive:

  - conformance_validator: the legacy `background=` attribute check was defeatable in its UNQUOTED
    form (`<td background=x.png>`), and an inline `data:image/svg+xml` reference slipped the
    zero-svg ban because the `data:` external-prefix short-circuit ran before the svg check.
  - html_emitter: the live-text COPY-OVERFLOW guard (EARS-209) measured fit at the 10px
    minimum-legible size while live text actually renders at the run's real size, so an unbreakable
    token could silently overflow on the live path; and a CTA label had no overflow guard at all.

No tautological asserts: every expectation is derived independently of the code under test (a
hand-built HTML string / a hand-built RoutedTree with known geometry), per the S1 discipline.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from psd_html.conformance_validator import validate_bundle, write_manifest
from psd_html.html_emitter import DPI_LOCK_BLOCK, emit
from psd_html.layer_router import route
from psd_html.layout_tree import BBox, TextInfo, TextRun
from psd_html.table_tree import Cell, Row, TableTree

# --- shared fixtures (self-contained; mirror the per-module test helpers) -------------------------


def _tiny_png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _write_bundle(root: Path, body: str) -> Path:
    """A minimal bundle whose <body> is `body`, with a real hero.png/band.png on disk + manifest +
    empty regions.json. The head carries the DPI-lock block so the only thing under test is the
    body grammar."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "hero.png").write_bytes(_tiny_png_bytes())
    (root / "assets" / "band.png").write_bytes(_tiny_png_bytes())
    html = (
        '<!DOCTYPE html>\n<html xmlns:o="urn:schemas-microsoft-com:office:office">\n<head>\n'
        '<meta charset="utf-8">\n' + DPI_LOCK_BLOCK + "\n<title>T</title>\n</head>\n"
        "<body style=\"margin:0;\">\n" + body + "\n</body>\n</html>\n"
    )
    (root / "index.html").write_text(html, encoding="utf-8")
    (root / "regions.json").write_text(json.dumps([]), encoding="utf-8")
    write_manifest(root)
    return root


def _rect(l=0, t=0, r=100, b=20):
    return BBox(left=l, top=t, right=r, bottom=b)


def _text_cell(content, *, font="Arial", editable=False, link_slot=None, role="text", size=14.0, rect=None):
    return Cell(
        role=role,
        rect=rect or _rect(),
        editable=editable,
        link_slot=link_slot,
        text=TextInfo(content=content, align="left", runs=[TextRun(font=font, size=size, color="#000000")]),
    )


def _tree(*cells, width=600):
    return TableTree(email="T", width=width, rows=[Row(cells=list(cells))])


def _types(report) -> set:
    return {v["type"] for v in report["violations"]}


# --- conformance: unquoted `background=` attribute ------------------------------------------------


def test_unquoted_background_attribute_now_fails(tmp_path):
    # `<td background=assets/band.png>` is valid HTML and classic Outlook honors the legacy attr.
    bundle = _write_bundle(tmp_path / "b", '<table><tr><td background=assets/band.png>x</td></tr></table>')
    report = validate_bundle(bundle)
    assert report["pass"] is False
    assert "background_attribute_forbidden" in _types(report)


def test_quoted_background_attribute_still_fails(tmp_path):
    bundle = _write_bundle(tmp_path / "b", '<table><tr><td background="assets/band.png">x</td></tr></table>')
    report = validate_bundle(bundle)
    assert report["pass"] is False
    assert "background_attribute_forbidden" in _types(report)


def test_inline_style_background_color_is_not_a_false_positive(tmp_path):
    # A legitimate CSS `background-color:` inline style must NOT trip the HTML-attribute check
    # (it uses `:` not `=`) -- the relaxed regex must stay attribute-only. (background-image was
    # removed from this fixture when Grammar G banned it outright, 2026-07-09 -- it now has its
    # own dedicated violation, asserted in the test below.)
    body = (
        '<table><tr><td bgcolor="#ffffff" style="background-color:#ffffff;">'
        '<img src="assets/hero.png" alt="h"></td></tr></table>'
    )
    bundle = _write_bundle(tmp_path / "b", body)
    report = validate_bundle(bundle)
    assert report["pass"] is True, report["violations"]


def test_inline_background_image_is_rejected_by_grammar_g(tmp_path):
    # Grammar G: CSS background-image never paints in classic Outlook (probe-verified 2026-07-09),
    # so the validator must reject it at intake with its own named violation.
    body = (
        '<table><tr><td style="background-image:url(\'assets/band.png\');">'
        '<img src="assets/hero.png" alt="h"></td></tr></table>'
    )
    bundle = _write_bundle(tmp_path / "b", body)
    report = validate_bundle(bundle)
    assert report["pass"] is False
    assert "background_image_forbidden" in _types(report)


# --- conformance: inline data:image/svg+xml -------------------------------------------------------


def test_data_uri_svg_img_now_fails(tmp_path):
    body = '<img src="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=" alt="x">'
    bundle = _write_bundle(tmp_path / "b", body)
    report = validate_bundle(bundle)
    assert report["pass"] is False
    assert "svg_reference" in _types(report)


def test_data_uri_svg_href_now_fails(tmp_path):
    body = '<a href="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">click</a>'
    bundle = _write_bundle(tmp_path / "b", body)
    report = validate_bundle(bundle)
    assert report["pass"] is False
    assert "svg_reference" in _types(report)


def test_data_uri_png_img_is_not_a_false_positive(tmp_path):
    # A data:image/png is off-bundle (Word ignores it) but is NOT an svg violation -- the header
    # check must key on the media type, not merely the presence of "svg" anywhere.
    body = '<img src="data:image/png;base64,iVBORw0KGgo=" alt="x">'
    bundle = _write_bundle(tmp_path / "b", body)
    report = validate_bundle(bundle)
    assert "svg_reference" not in _types(report)


# --- html_emitter: live-text overflow measured at the RENDERED size -------------------------------


def _emit(tree, policy, tmp_path, **kw):
    routed = route(tree, policy)
    return emit(routed, tmp_path / (policy + "_out"), **kw)


def test_live_text_overflow_flagged_at_rendered_size_not_min_legible(tmp_path):
    # rect width 100, one unbreakable 14-char token at 40px in a MULTI-LINE design box (height 120
    # = 2 design lines, so single-line shrink-to-fit does not apply -- a single-line box would now
    # legitimately shrink the font to fit, which is the faithful correction, not an overflow). At
    # the 10px min-legible size the token "fits" (the pre-fix bug), but the <td> renders at 40px
    # where it clearly cannot -- must flag.
    tree = _tree(_text_cell("Transformation", font="Arial", size=40.0, rect=_rect(0, 0, 100, 120)))
    result = _emit(tree, "live", tmp_path)
    assert result["overflow_flags"], "an unbreakable token far wider than the td at its rendered size must flag"


def test_small_live_text_in_wide_cell_does_not_flag(tmp_path):
    # Control: the same token at 8px in a 400px cell fits comfortably -- no false overflow.
    tree = _tree(_text_cell("Transformation", font="Arial", size=8.0, rect=_rect(0, 0, 400, 40)))
    result = _emit(tree, "live", tmp_path)
    assert result["overflow_flags"] == []


# --- html_emitter: CTA label overflow -------------------------------------------------------------


def test_long_cta_label_flags_overflow(tmp_path):
    # A role=button cell in a 120px column, late-bound to a long label. The nowrap button would
    # widen the whole fixed table -- must flag rather than silently overflow.
    cta = _text_cell("Register", role="button", link_slot="cta", size=16.0, rect=_rect(0, 0, 120, 44))
    cta.source_layer_id = 900
    tree = _tree(cta)
    label = "Register for the exclusive partner webinar today"
    result = _emit(
        tree, "hybrid", tmp_path,
        copy_manifest={900: label},
        link_manifest={"cta": "https://example.com/reg"},
    )
    assert result["overflow_flags"], "an over-long CTA label wider than its column must flag"
    # The PROTECTIVE behavior, not merely the flag: the overflowing button swaps nowrap for
    # white-space:normal so the full label WRAPS inside the fixed column instead of bursting the
    # tiled table's exact width -- and is never clipped/truncated (no overflow:hidden / text-overflow);
    # the full late-bound string still renders verbatim.
    html_text = Path(result["index_path"]).read_text(encoding="utf-8")
    assert "white-space:normal" in html_text
    assert label in html_text
    assert "overflow:hidden" not in html_text
    assert "text-overflow" not in html_text
    # Control: a short label that fits its column keeps the classic bulletproof nowrap button.
    short = _text_cell("Register", role="button", link_slot="cta", size=16.0, rect=_rect(0, 0, 400, 44))
    short.source_layer_id = 902
    short_result = _emit(
        _tree(short), "hybrid", tmp_path / "short",
        copy_manifest={902: "Register"},
        link_manifest={"cta": "https://example.com/reg"},
    )
    short_html = Path(short_result["index_path"]).read_text(encoding="utf-8")
    assert "white-space:nowrap" in short_html


def test_short_cta_label_does_not_flag(tmp_path):
    cta = _text_cell("Go", role="button", link_slot="cta", size=16.0, rect=_rect(0, 0, 200, 44))
    cta.source_layer_id = 901
    tree = _tree(cta)
    result = _emit(
        tree, "hybrid", tmp_path,
        copy_manifest={901: "Go"},
        link_manifest={"cta": "https://example.com/go"},
    )
    assert result["overflow_flags"] == []
