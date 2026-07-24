"""link_scaffold tests: the starter-manifest generator.

Real-PSD-driven (skipif-gated) end-to-end tests -- the whole point of this module is that its
candidates come from the REAL pipeline's own region discovery (emit()'s returned `regions`), so a
synthetic fixture would just re-assert whatever this module computed, not prove it matches what
the pipeline actually produces. `_iter_text_contents` (pure tree-walk logic) gets a synthetic unit
test since it needs no PSD at all.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from psd_html.layout_tree import TextInfo
from psd_html.link_scaffold import (
    _iter_text_contents,
    build_starter_manifest,
    categorize_regions,
    discover_link_candidates,
    find_manifest_near_psd,
    manifest_from_form,
    write_starter_manifest,
)
from psd_html.table_tree import Cell, Row

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
_ann_only = pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")


def _rect(l=0, t=0, r=100, b=20):
    from psd_html.layout_tree import BBox

    return BBox(left=l, top=t, right=r, bottom=b)


def _text_cell(content):
    return Cell(role="text", rect=_rect(), text=TextInfo(content=content, align="left", runs=[]))


# --- _iter_text_contents: pure tree-walk, no PSD needed -----------------------------------------


def test_iter_text_contents_walks_nested_rows_containers_and_skips_empty():
    nested = [Row(cells=[_text_cell("Inner headline")])]
    top = [
        Row(cells=[Cell(role="rows", rect=_rect(), rows=nested), _text_cell("Sibling body copy")]),
        Row(cells=[_text_cell("")]),  # empty content -- must be skipped
        Row(cells=[Cell(role="image", rect=_rect(), image_source_layer_ids=[1])]),  # no text at all
    ]
    found = list(_iter_text_contents(top))
    assert found == ["Inner headline", "Sibling body copy"]


# --- find_manifest_near_psd: pure filesystem logic, no PSD parsing needed -----------------------


def test_find_manifest_near_psd_exact_stem_match_wins_even_with_others_present(tmp_path):
    psd = tmp_path / "MyDesign.psd"
    psd.write_bytes(b"")
    (tmp_path / "MyDesign.links.json").write_text("{}", encoding="utf-8")
    (tmp_path / "unrelated.links.json").write_text("{}", encoding="utf-8")  # must NOT surface as "others"

    canonical, others = find_manifest_near_psd(psd)
    assert canonical == tmp_path / "MyDesign.links.json"
    assert others == []  # canonical present -> no ambiguity to report, regardless of the folder


def test_find_manifest_near_psd_reports_the_sole_differently_named_file_as_an_other(tmp_path):
    psd = tmp_path / "MyDesign.psd"
    psd.write_bytes(b"")
    (tmp_path / "announcement.links.json").write_text("{}", encoding="utf-8")

    canonical, others = find_manifest_near_psd(psd)
    assert canonical is None
    assert others == [tmp_path / "announcement.links.json"]


def test_find_manifest_near_psd_reports_every_candidate_when_multiple_exist(tmp_path):
    psd = tmp_path / "MyDesign.psd"
    psd.write_bytes(b"")
    (tmp_path / "a.links.json").write_text("{}", encoding="utf-8")
    (tmp_path / "b.links.json").write_text("{}", encoding="utf-8")

    canonical, others = find_manifest_near_psd(psd)
    assert canonical is None
    assert others == [tmp_path / "a.links.json", tmp_path / "b.links.json"]


def test_find_manifest_near_psd_reports_none_found(tmp_path):
    psd = tmp_path / "MyDesign.psd"
    psd.write_bytes(b"")

    canonical, others = find_manifest_near_psd(psd)
    assert canonical is None
    assert others == []


# --- categorize_regions / manifest_from_form: pure logic, no PSD needed -------------------------


def _region(role, link_slot=None, region_id=None, alt=None):
    d = {"role": role, "link_slot": link_slot, "alt": alt}
    if region_id is not None:
        d["region_id"] = region_id
    return d


def test_categorize_regions_dedupes_slots_and_finds_slotless_image_candidates():
    regions = [
        _region("cta", link_slot="review-the-toolkit", alt=None),
        _region("button", link_slot="review-the-toolkit", alt=None),  # same slot -- de-duped
        _region("image", region_id="0_0", alt="Intel Logo"),
        _region("merge", region_id="2_0", alt=None),  # plain text, no slot -- NOT a candidate
    ]
    slots, region_candidates = categorize_regions(regions)
    assert [s["link_slot"] for s in slots] == ["review-the-toolkit"]
    assert [r["region_id"] for r in region_candidates] == ["0_0"]


def test_manifest_from_form_drops_blank_values_keeps_real_ones():
    manifest = manifest_from_form(
        slot_values={"review-the-toolkit": "https://example.com/toolkit", "unsubscribe": "  "},
        region_values={"0_0": "https://example.com/logo", "1_0": ""},
        inline_entries=[
            {"match": "The Business Opportunity of AI", "url": "https://example.com/report"},
            {"match": "no url here", "url": ""},
            {"match": "", "url": "https://example.com/orphan"},
        ],
    )
    assert manifest["slots"] == {"review-the-toolkit": "https://example.com/toolkit"}
    assert manifest["regions"] == {"0_0": "https://example.com/logo"}
    assert manifest["inline"] == [{"match": "The Business Opportunity of AI", "url": "https://example.com/report"}]
    json.dumps(manifest)  # must always be valid JSON


def test_manifest_from_form_with_everything_blank_is_a_safe_no_op_manifest():
    manifest = manifest_from_form({}, {}, [])
    assert manifest["slots"] == {}
    assert manifest["regions"] == {}
    assert manifest["inline"] == []


# --- real-PSD end-to-end: discovery, manifest shape, write/overwrite guard -----------------------


@_ann_only
def test_discover_link_candidates_finds_the_real_cta_slot_and_regions():
    tree, regions = discover_link_candidates(ANNOUNCEMENT_PSD)
    assert tree.email
    assert regions, "expected at least one region for the real announcement PSD"
    slots = {r["link_slot"] for r in regions if r.get("link_slot")}
    assert "review-the-toolkit" in slots  # the real CTA slot documented in grammar/PIPELINE.md


@_ann_only
def test_build_starter_manifest_leaves_real_sections_empty_with_populated_candidates():
    manifest = build_starter_manifest(ANNOUNCEMENT_PSD)
    # the REAL sections the pipeline reads must start empty -- never a placeholder value that
    # would silently bind as a real (broken) href.
    assert manifest["slots"] == {}
    assert manifest["regions"] == {}
    assert manifest["inline"] == []
    assert "_comment" in manifest and "psd-html linkgen" in manifest["_comment"]

    candidates = manifest["_candidates"]
    slot_names = {c["link_slot"] for c in candidates["slots"]}
    assert "review-the-toolkit" in slot_names
    assert candidates["regions"], "expected image/graphic region candidates"
    assert all(c["region_id"] for c in candidates["regions"])
    assert candidates["inline_text"], "expected live text content candidates for inline citations"
    assert any("[First Name]" in t for t in candidates["inline_text"])

    # the manifest itself must be valid JSON with no stray non-JSON-serializable content.
    json.dumps(manifest)


@_ann_only
def test_write_starter_manifest_refuses_to_overwrite_without_force(tmp_path):
    out = tmp_path / "x.links.json"
    out.write_text('{"slots": {"hand-authored": "https://example.com"}}', encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_starter_manifest(ANNOUNCEMENT_PSD, out)

    # refused -- the hand-authored file must survive untouched.
    assert "hand-authored" in out.read_text(encoding="utf-8")

    written = write_starter_manifest(ANNOUNCEMENT_PSD, out, force=True)
    assert written == out
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["slots"] == {}  # force=True really did overwrite with a fresh starter


@_ann_only
def test_write_starter_manifest_defaults_output_path_next_to_the_psd(tmp_path):
    # Copy the real PSD into a scratch dir so the default "<psd-stem>.links.json" write target
    # is disposable, not the real Reference/ corpus directory.
    import shutil

    psd_copy = tmp_path / "Announcement.psd"
    shutil.copyfile(ANNOUNCEMENT_PSD, psd_copy)

    written = write_starter_manifest(psd_copy)
    assert written == tmp_path / "Announcement.links.json"
    assert written.is_file()


def test_discover_link_candidates_reports_zero_solve_and_bad_email_index(monkeypatch):
    import psd_html.link_scaffold as link_scaffold_mod

    fake_layout = SimpleNamespace(layers=[])
    monkeypatch.setattr(link_scaffold_mod, "psd_to_layout_tree", lambda p: fake_layout)
    monkeypatch.setattr(link_scaffold_mod, "build_table_trees", lambda layout, email_override=None: [])
    with pytest.raises(ValueError, match="zero artboards"):
        discover_link_candidates("x.psd")

    fake_tree = object()
    monkeypatch.setattr(link_scaffold_mod, "build_table_trees", lambda layout, email_override=None: [fake_tree])
    with pytest.raises(ValueError, match="out of range"):
        discover_link_candidates("x.psd", email_index=5)


# --- CLI wiring -----------------------------------------------------------------------------
# Fixture-free companions (same rationale as test_bakeoff.py's own CLI-wiring tests): these
# monkeypatch the one seam _cmd_linkgen calls (link_scaffold.write_starter_manifest) so a wiring
# regression -- a wrong kwarg, a swallowed exception, a broken exit code -- shows up on every
# machine, not just ones with the real PSD fixture.


def test_cli_linkgen_wiring_prints_written_path(monkeypatch, capsys, tmp_path):
    import psd_html.link_scaffold as link_scaffold_mod
    from psd_html import cli

    out_path = tmp_path / "x.links.json"
    calls = {}

    def _fake_write(psd, out=None, *, force=False, email_override=None, email_index=0, policy="hybrid"):
        calls["args"] = (psd, out, force, email_override, email_index, policy)
        out_path.write_text("{}", encoding="utf-8")
        return out_path

    monkeypatch.setattr(link_scaffold_mod, "write_starter_manifest", _fake_write)
    exit_code = cli.main(["linkgen", "x.psd", "-o", str(out_path), "--policy", "raster", "--email-index", "1"])
    assert exit_code == 0
    assert str(out_path) in capsys.readouterr().out
    assert calls["args"] == ("x.psd", str(out_path), False, None, 1, "raster")


def test_cli_linkgen_refuses_on_existing_file_without_force(monkeypatch, capsys):
    import psd_html.link_scaffold as link_scaffold_mod
    from psd_html import cli

    def _boom(psd, out=None, *, force=False, email_override=None, email_index=0, policy="hybrid"):
        raise FileExistsError(f"{out} already exists")

    monkeypatch.setattr(link_scaffold_mod, "write_starter_manifest", _boom)
    exit_code = cli.main(["linkgen", "x.psd", "-o", "already-there.json"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "REFUSED" in err and "already exists" in err


def test_cli_linkgen_refuses_on_zero_solve(monkeypatch, capsys):
    import psd_html.link_scaffold as link_scaffold_mod
    from psd_html import cli

    def _boom(psd, out=None, *, force=False, email_override=None, email_index=0, policy="hybrid"):
        raise ValueError(f"{psd}: solved to zero artboards/emails")

    monkeypatch.setattr(link_scaffold_mod, "write_starter_manifest", _boom)
    exit_code = cli.main(["linkgen", "x.psd"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "REFUSED" in err and "zero artboards" in err
