"""Deterministic link surface (links.json manifest): slots / regions / inline binding, loud
unbound reconciliation, and the real-corpus pin that codifies the v17 converged behaviors."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from psd_html.html_emitter import _bind_inline_links, _EmitContext, emit
from psd_html.layer_router import route
from psd_html.layout_tree import BBox, TextInfo, TextRun
from psd_html.table_tree import Cell, Row, TableTree

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ANNOUNCEMENT_PSD = os.path.join(
    REPO_ROOT,
    "Reference",
    "2413101_Intel",
    "PSDs",
    "Intel x Microsoft_Commercial Refresh_announcement email",
    "Intel_MsfT_Global BoM_Announcement Email.psd",
)
ANNOUNCEMENT_LINKS = os.path.join(
    REPO_ROOT, "Tools", "PSD-HTML", "grammar", "links", "announcement.links.json"
)


def _rect(l=0, t=0, r=200, b=30):
    return BBox(left=l, top=t, right=r, bottom=b)


def _text_cell(content, *, link_slot=None, role="text"):
    return Cell(
        role=role,
        rect=_rect(),
        link_slot=link_slot,
        text=TextInfo(content=content, align="left",
                      runs=[TextRun(font="Arial", size=16.0, color="#000000", length=len(content))]),
    )


def _tree(cells):
    return TableTree(email="Links", width=600, rows=[Row(cells=[c]) for c in cells])


def _emit(tmp_path, cells, link_manifest):
    routed = route(_tree(cells), "hybrid")
    return emit(routed, tmp_path / "bundle", link_manifest=link_manifest)


# --- slots / legacy ------------------------------------------------------------------------------


def test_structured_slots_bind_a_cta_and_report_it(tmp_path):
    cta = _text_cell("Shop Now", link_slot="shop-now", role="button")
    result = _emit(tmp_path, [cta], {"slots": {"shop-now": "https://example.com/shop"}})
    html_text = (tmp_path / "bundle" / "index.html").read_text(encoding="utf-8")
    assert 'href="https://example.com/shop"' in html_text
    assert result["links"]["bound"] and result["links"]["bound"][0]["url"] == "https://example.com/shop"
    assert result["links"]["unbound"] == []


def test_unsafe_scheme_url_is_blocked_and_reported_not_emitted(tmp_path):
    # javascript:/data:/vbscript: are inert under Outlook's Word engine but LIVE in the Chromium
    # fidelity preview -- the emitter must NEVER wrap an element in such an href. A blocked URL
    # surfaces as an unbound manifest promise (loud failure), never a silent drop.
    cta = _text_cell("Click", link_slot="danger", role="button")
    result = _emit(tmp_path, [cta], {"slots": {"danger": "javascript:alert(1)"}})
    html_text = (tmp_path / "bundle" / "index.html").read_text(encoding="utf-8")
    assert "javascript:" not in html_text  # not emitted at all, not even html-escaped
    assert result["links"]["bound"] == []
    assert result["links"]["unbound"] == [{"kind": "slot", "key": "danger", "url": "javascript:alert(1)"}]
    assert any(w.get("type") == "link_unbound" and "unsafe URL scheme" in w.get("reason", "")
               for w in result["warnings"])


def test_allowlisted_schemes_still_bind(tmp_path):
    # The allowlist is not overzealous: mailto:/tel:/http(s)/relative bind normally.
    cta = _text_cell("Mail us", link_slot="m", role="button")
    result = _emit(tmp_path, [cta], {"slots": {"m": "mailto:hi@example.com"}})
    html_text = (tmp_path / "bundle" / "index.html").read_text(encoding="utf-8")
    assert 'href="mailto:hi@example.com"' in html_text
    assert result["links"]["unbound"] == []


def test_legacy_flat_manifest_still_reads_as_slots(tmp_path):
    cta = _text_cell("Shop Now", link_slot="shop-now", role="button")
    result = _emit(tmp_path, [cta], {"shop-now": "https://example.com/flat"})
    html_text = (tmp_path / "bundle" / "index.html").read_text(encoding="utf-8")
    assert 'href="https://example.com/flat"' in html_text
    assert result["links"]["unbound"] == []


def test_unbound_manifest_url_is_a_loud_warning_and_reported(tmp_path):
    body = _text_cell("Plain body copy with no slot.")
    result = _emit(tmp_path, [body], {"slots": {"missing-slot": "https://example.com/dead"}})
    assert result["links"]["unbound"] == [
        {"kind": "slot", "key": "missing-slot", "url": "https://example.com/dead"}
    ]
    assert any(w.get("type") == "link_unbound" for w in result["warnings"])
    report = json.loads((tmp_path / "bundle" / "links_report.json").read_text(encoding="utf-8"))
    assert report["unbound"] and report["unbound"][0]["url"] == "https://example.com/dead"


def test_unbound_inline_match_absent_from_copy_is_reported(tmp_path):
    # The "match" text does not appear anywhere in the cell copy -- the inline binder can
    # never find it, so it must be reconciled as unbound (kind "inline") rather than silently
    # dropped. Guards the asymmetric bound_keys lookup (b["match"] for inline vs b["key"] for
    # slot/region): a typo'd citation URL must still surface as a dead link.
    body = _text_cell("Plain body copy with no matching phrase.")
    result = _emit(
        tmp_path, [body],
        {"inline": [{"match": "The Missing Phrase", "url": "https://example.com/dead-inline"}]},
    )
    assert result["links"]["unbound"] == [
        {"kind": "inline", "key": "The Missing Phrase", "url": "https://example.com/dead-inline"}
    ]
    assert any(w.get("type") == "link_unbound" and w.get("kind") == "inline" for w in result["warnings"])
    report = json.loads((tmp_path / "bundle" / "links_report.json").read_text(encoding="utf-8"))
    assert report["unbound"] and report["unbound"][0]["url"] == "https://example.com/dead-inline"


def test_unbound_region_manifest_id_is_reported(tmp_path):
    # A {"regions": {region_id: url}} promise whose region_id never matches any emitted cell's
    # region id must be reconciled as unbound (kind "region"), same as a typo'd slot/inline key.
    body = _text_cell("Plain body copy, no region binds here.")
    result = _emit(tmp_path, [body], {"regions": {"bad-id": "https://example.com/dead-region"}})
    assert result["links"]["unbound"] == [
        {"kind": "region", "key": "bad-id", "url": "https://example.com/dead-region"}
    ]
    assert any(w.get("type") == "link_unbound" and w.get("kind") == "region" for w in result["warnings"])
    report = json.loads((tmp_path / "bundle" / "links_report.json").read_text(encoding="utf-8"))
    assert report["unbound"] and report["unbound"][0]["url"] == "https://example.com/dead-region"


# --- inline --------------------------------------------------------------------------------------


def test_inline_link_binds_exact_visible_text(tmp_path):
    body = _text_cell("Read The Annual Report today.")
    result = _emit(
        tmp_path, [body],
        {"inline": [{"match": "The Annual Report", "url": "https://example.com/report"}]},
    )
    html_text = (tmp_path / "bundle" / "index.html").read_text(encoding="utf-8")
    assert '<a href="https://example.com/report"' in html_text
    assert ">The Annual Report</a>" in html_text
    assert result["links"]["unbound"] == []


def test_inline_match_spans_a_frozen_line_break():
    # The rendered body carries a frozen U+2028 break as <br/> INSIDE the match text -- the
    # binder's space pattern must bridge it (citation titles wrap lines in the real corpus).
    ctx = _EmitContext(out_root=None, assets_dir=None, assets_subdir="assets",
                       copy_manifest=None, link_manifest={"inline": [
                           {"match": "Annual Report of Records", "url": "https://example.com/r"}]},
                       composite=None, layer_names=None, registry=None)
    body = "See Annual Report<br/>of Records now"
    out = _bind_inline_links(body, "See Annual Report of Records now", "r1", ctx)
    assert '<a href="https://example.com/r"' in out
    assert "Annual Report<br/>of Records</a>" in out
    assert ctx.links_bound and ctx.links_bound[0]["kind"] == "inline"


# --- the real-corpus pin: codifies the v17 converged behaviors -----------------------------------


@pytest.mark.skipif(not os.path.isfile(ANNOUNCEMENT_PSD), reason="Intel announcement PSD fixture not present")
def test_real_announcement_emit_pins_v17_behaviors_and_binds_all_links(tmp_path):
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.rasterizer import composite_psd
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    trees = build_table_trees(layout, email_override="Announcement")
    routed = route(trees[0], "hybrid")
    link_manifest = json.loads(Path(ANNOUNCEMENT_LINKS).read_text(encoding="utf-8"))
    result = emit(
        routed, tmp_path / "bundle",
        link_manifest=link_manifest,
        composite=composite_psd(ANNOUNCEMENT_PSD),
        layer_names={l.id: l.name for l in layout.layers},
        psd_path=ANNOUNCEMENT_PSD,
    )
    html_text = (tmp_path / "bundle" / "index.html").read_text(encoding="utf-8")

    # LINKS: CTA slot + 4 footnote citations all bound, nothing dead.
    assert result["links"]["unbound"] == [], result["links"]["unbound"]
    kinds = sorted(b["kind"] for b in result["links"]["bound"])
    assert kinds.count("inline") == 4 and "slot" in kinds

    # FOOTER (v17): single banded td -- band fill + paddings, NO height prop (height+padding
    # stack; emitting both rendered a 130px band against the design's 82).
    import re

    footer = re.search(r'<td[^>]*bgcolor="#F2F2F2"[^>]*>', html_text)
    assert footer, "expected the single-td footer band"
    assert not re.search(r'(?<![-a-z])height[:=]', footer.group(0)), footer.group(0)
    assert "padding:" in footer.group(0)

    # STAT COLUMNS (v17): harmonized -- the three caption cells share one font-size.
    sizes = set()
    for rid in ("5_0_rows_1_0", "5_2_rows_1_0", "5_4_rows_1_0"):
        m = re.search(rf'data-region="{rid}"[^>]*style="[^"]*font-size:([\d.]+)px', html_text)
        if m:
            sizes.add(m.group(1))
    assert len(sizes) <= 1, f"caption sizes diverged across columns: {sizes}"

    # PSD-VERBATIM FLOW (v17): the opening paragraph carries frozen breaks, including the
    # design's own "powered / by" break.
    m = re.search(r'data-region="3_1".*?</td>', html_text, re.DOTALL)
    assert m and "powered<br/>by" in m.group(0).replace("&#x27;", "'"), \
        "expected the PSD's frozen 'powered / by' break in the opening paragraph"


RESELLER_PSD = os.path.join(
    REPO_ROOT,
    "Reference",
    "2413101_Intel",
    "PSDs",
    "Intel x Microsoft_Commercial Refresh_reseller to customer",
    "Intel_MsfT_Global BoM_reseller to customer.psd",
)


@pytest.mark.skipif(not os.path.isfile(RESELLER_PSD), reason="Intel reseller PSD fixture not present")
def test_real_reseller_chip_pins_label_table_construct(tmp_path):
    """REGRESSION PIN (owner rounds 3-17 converged): the Partner Logo chip ships as the
    LABEL-TABLE construct -- exact-geometry filled outer td (115px wide, near-balanced
    vertical padding) wrapping a width-free nested nowrap label td. Every earlier form
    regressed in one engine: bare box td (row-stretch painted it 37px tall), fixed-width
    nowrap (Word ignores nowrap on padded fixed tds), strut-centered pads (label sat low
    of the design's baseline)."""
    import re

    from psd_html.html_emitter import emit
    from psd_html.layer_router import route
    from psd_html.psd_adapter import psd_to_layout_tree
    from psd_html.rasterizer import composite_psd
    from psd_html.table_solver import build_table_trees

    layout = psd_to_layout_tree(RESELLER_PSD)
    tree = build_table_trees(layout, email_override="Reseller")[0]
    routed = route(tree, "hybrid")
    composite = composite_psd(RESELLER_PSD)
    result = emit(routed, tmp_path, composite=composite,
                  layer_names={l.id: l.name for l in layout.layers}, psd_path=RESELLER_PSD)
    html_text = Path(result["index_path"]).read_text(encoding="utf-8")

    m = re.search(r'data-region="0_0"[^>]*style="([^"]*)"[^>]*>(.*?)</table>', html_text, re.DOTALL)
    assert m, "header Partner Logo chip (region 0_0) missing"
    outer_style, inner = m.group(1), m.group(2)
    assert 'width="115"' in html_text[m.start() - 60:m.end()], "chip must keep the design shape's 115px width"
    pads = re.search(r"padding:(\d+)px \d+px (\d+)px \d+px", outer_style)
    assert pads, f"chip outer td must carry explicit vertical padding: {outer_style}"
    top, bottom = int(pads.group(1)), int(pads.group(2))
    assert abs(top - bottom) <= 3, f"chip label must sit near-centered (pads {top}/{bottom})"
    assert "white-space:nowrap" in inner, "chip label must ride a nested nowrap td (Word ignores nowrap on fixed-width tds)"
