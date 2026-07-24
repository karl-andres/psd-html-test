"""F-FIDELITY-BAKEOFF tests.

Adversarial hand-built fixtures (no tautological asserts) for the small pure helpers +
`_check_editability` (which is fed crafted-wrong-on-purpose regions.json/index.html to prove it
actually catches a mismatch, not just rubber-stamps), a synthetic-but-real full `run()` pass with
the three heavy S1/PSD-tools/oracle dependencies monkeypatched out for speed/determinism, an
adversarial ConformanceError-propagation test, PLUS the real Intel announcement PSD run through
`bakeoff.run()` and through the CLI end to end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

import psd_html.bakeoff as bakeoff_mod
import psd_html.table_solver as table_solver_mod
from psd_html import cli
from psd_html.bakeoff import (
    BakeoffError,
    _check_editability,
    _collect_link_slots,
    _default_link_manifest,
    _editability_proofs,
    _find_leaf_containing,
    _iter_leaf_cells,
    _region_counts,
    _region_id_for_key,
    run,
)
from psd_html.conformance_validator import ConformanceError
from psd_html.layer_router import POLICIES
from psd_html.layout_tree import BBox, Canvas, Layer, LayoutTree, TextInfo, TextRun
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
_has_real_psd = os.path.isfile(ANNOUNCEMENT_PSD)


# --- fixture builders ----------------------------------------------------------------------------


def _rect(l=0, t=0, r=600, b=20):
    return BBox(left=l, top=t, right=r, bottom=b)


def _text_cell(content, *, font="Arial", editable=False, link_slot=None, source_layer_id=None, rect=None):
    return Cell(
        role="text",
        rect=rect or _rect(),
        editable=editable,
        link_slot=link_slot,
        text=TextInfo(content=content, align="left", runs=[TextRun(font=font, size=14.0, color="#000000")]),
        source_layer_id=source_layer_id,
    )


def _image_cell(rect=None, source_layer_id=None, image_ids=None):
    return Cell(role="image", rect=rect or _rect(), image_source_layer_ids=image_ids or [201], source_layer_id=source_layer_id)


def _rows_cell(rows, rect=None):
    return Cell(role="rows", rect=rect or _rect(), rows=rows)


# Segoe routes LIVE since 2026-07-14 (human-OFT proof); policy-matrix tests exercise the
# brand->raster path with a registered fixture face Windows does not install.
BRAND_FIXTURE_FONT = "BrandDisplay"


@pytest.fixture(autouse=True)
def _register_brand_fixture_font():
    from psd_html.font_resolver import DEFAULT_REGISTRY, FontRegistryEntry

    DEFAULT_REGISTRY["branddisplay"] = FontRegistryEntry(
        family="Brand Display", fallback_stack=("sans-serif",), brand_mandatory=True, files=()
    )
    yield
    DEFAULT_REGISTRY.pop("branddisplay", None)


def _synthetic_tree(email="Announcement (synthetic)") -> TableTree:
    """A small hand-built TableTree that structurally mirrors the real Intel announcement
    email's editability-sensitive shape: an editable greeting merge field, an editable
    sender-choice merge field, a CTA with a link_slot, plain body copy, a brand-mandatory-font
    headline (BrandDisplay fixture face -- Segoe routes live since 2026-07-14), and a non-text
    image."""
    rows = [
        Row(cells=[_text_cell("Hi [First Name],", editable=True, source_layer_id=101, rect=_rect(0, 0, 600, 20))]),
        Row(
            cells=[
                _text_cell(
                    "Best regards,\n[Microsoft, Intel, or Microsoft & Intel-depending on sender]",
                    editable=True,
                    source_layer_id=102,
                    rect=_rect(0, 20, 600, 60),
                )
            ]
        ),
        Row(cells=[_text_cell("Review the toolkit", link_slot="review-the-toolkit", source_layer_id=103, rect=_rect(0, 60, 600, 80))]),
        Row(cells=[_text_cell("Some plain running body copy that never rasterizes.", source_layer_id=104, rect=_rect(0, 80, 600, 100))]),
        Row(cells=[_text_cell("Brand Headline Copy", font=BRAND_FIXTURE_FONT, source_layer_id=105, rect=_rect(0, 100, 600, 140))]),
        Row(cells=[_image_cell(rect=_rect(0, 140, 600, 240), source_layer_id=None, image_ids=[201])]),
    ]
    return TableTree(email=email, width=600, rows=rows)


def _solid_composite(width=600, height=240):
    return Image.new("RGBA", (width, height), (255, 255, 255, 255))


def _fake_layout_tree() -> LayoutTree:
    return LayoutTree(
        psd="synthetic.psd",
        path="synthetic.psd",
        canvas=Canvas(width=600, height=240),
        artboards=[],
        layers=[
            Layer(id=101, name="greeting", kind="type", visible=True, opacity=1.0, bbox=None, z=0, is_group=False, parent=None),
            Layer(id=102, name="sender choice", kind="type", visible=True, opacity=1.0, bbox=None, z=1, is_group=False, parent=None),
            Layer(id=103, name="cta", kind="type", visible=True, opacity=1.0, bbox=None, z=2, is_group=False, parent=None),
            Layer(id=104, name="body", kind="type", visible=True, opacity=1.0, bbox=None, z=3, is_group=False, parent=None),
            Layer(id=105, name="headline", kind="type", visible=True, opacity=1.0, bbox=None, z=4, is_group=False, parent=None),
            Layer(id=201, name="hero image", kind="pixel", visible=True, opacity=1.0, bbox=None, z=5, is_group=False, parent=None),
        ],
    )


def _patch_pipeline(monkeypatch, tree_or_trees, *, composite=None):
    """Monkeypatch the 3 heavy pipeline seams `bakeoff.run` calls -- psd_to_layout_tree,
    build_table_trees, composite_psd -- with fast, deterministic synthetic fixtures."""
    trees = tree_or_trees if isinstance(tree_or_trees, list) else [tree_or_trees]
    monkeypatch.setattr(bakeoff_mod, "psd_to_layout_tree", lambda psd_path: _fake_layout_tree())
    monkeypatch.setattr(bakeoff_mod, "build_table_trees", lambda layout: trees)
    monkeypatch.setattr(bakeoff_mod, "composite_psd", lambda psd_path: composite if composite is not None else _solid_composite())


# --- pure helper unit tests (adversarial, hand-checked expectations) -----------------------------


def test_iter_leaf_cells_walks_nested_rows_and_skips_containers():
    leaf_a = _text_cell("A")
    leaf_b = _text_cell("B")
    nested = _rows_cell([Row(cells=[leaf_b])])
    tree = TableTree(email="t", width=100, rows=[Row(cells=[leaf_a, nested])])

    found = list(_iter_leaf_cells(tree.rows))
    assert [cell.text.content for cell, _key in found] == ["A", "B"]
    assert found[0][1] == (0, 0)
    assert found[1][1] == (0, 1, "rows", 0, 0)


def test_region_id_for_key_matches_html_emitter_convention():
    assert _region_id_for_key((2, 0, "rows", 1, 3)) == "2_0_rows_1_3"


def test_collect_link_slots_dedupes_and_preserves_first_seen_order():
    tree = TableTree(
        email="t",
        width=100,
        rows=[
            Row(cells=[_text_cell("a", link_slot="slot-b")]),
            Row(cells=[_text_cell("b", link_slot="slot-a")]),
            Row(cells=[_text_cell("c", link_slot="slot-b")]),  # duplicate -- must not repeat
            Row(cells=[_text_cell("d")]),  # no link_slot -- must not appear
        ],
    )
    assert _collect_link_slots(tree) == ["slot-b", "slot-a"]


def test_default_link_manifest_maps_each_slot_to_a_hash_anchor():
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("cta", link_slot="review-the-toolkit")])])
    assert _default_link_manifest(tree) == {"review-the-toolkit": "#review-the-toolkit"}


def test_default_link_manifest_empty_when_no_link_slots():
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("plain")])])
    assert _default_link_manifest(tree) == {}


def test_find_leaf_containing_returns_first_match_in_walk_order():
    tree = TableTree(
        email="t",
        width=100,
        rows=[Row(cells=[_text_cell("nothing here")]), Row(cells=[_text_cell("Review the toolkit today")])],
    )
    cell, key = _find_leaf_containing(tree, "Review the toolkit")
    assert cell is not None and key == (1, 0)
    assert cell.text.content == "Review the toolkit today"


def test_find_leaf_containing_returns_none_when_absent():
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("nothing matches")])])
    cell, key = _find_leaf_containing(tree, "Review the toolkit")
    assert cell is None and key is None


def test_editability_proofs_only_includes_needles_the_tree_actually_carries():
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("Hi [First Name],", editable=True)])])
    proofs = _editability_proofs(tree)
    labels = {p["label"] for p in proofs}
    assert labels == {"greeting_merge_field"}  # sender-choice + CTA strings are NOT present here
    assert proofs[0]["expected_role"] == "merge"


def test_editability_proofs_empty_when_psd_carries_none_of_the_known_strings():
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("totally unrelated copy")])])
    assert _editability_proofs(tree) == []


def test_region_counts_flags_a_protected_rasterized_violation():
    regions = [
        {"role": "merge", "render": "live"},
        {"role": "body", "render": "raster"},  # the violation shape this exists to catch
        {"role": "cta", "render": "live"},
        {"role": "brand_headline", "render": "raster"},
        {"role": "image", "render": "raster"},
    ]
    counts = _region_counts(regions)
    assert counts == {"protected_kept_live": 2, "protected_rasterized": 1, "brand_rasters": 1}


def test_region_counts_clean_case_has_zero_protected_rasterized():
    regions = [
        {"role": "merge", "render": "live"},
        {"role": "body", "render": "live"},
        {"role": "cta", "render": "live"},
        {"role": "brand_headline", "render": "raster"},
        {"role": "image", "render": "raster"},
    ]
    counts = _region_counts(regions)
    assert counts["protected_rasterized"] == 0
    assert counts["protected_kept_live"] == 3
    assert counts["brand_rasters"] == 1


# --- _check_editability: prove it actually catches a mismatch, not a rubber stamp ----------------


def _write_bundle(tmp_path: Path, policy: str, *, region_id: str, role: str, render: str, html_body: str) -> None:
    bundle_dir = tmp_path / policy
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "index.html").write_text(f"<html><body>{html_body}</body></html>", encoding="utf-8")
    (bundle_dir / "regions.json").write_text(
        json.dumps([{"region_id": region_id, "role": role, "render": render}]), encoding="utf-8"
    )


def test_check_editability_passes_when_role_render_and_html_all_line_up(tmp_path):
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("Hi [First Name],", editable=True)])])
    key = (0, 0)
    region_id = _region_id_for_key(key)
    for policy in POLICIES:
        _write_bundle(tmp_path, policy, region_id=region_id, role="merge", render="live", html_body="Hi [First Name],")
    policies_report = [{"protected_rasterized": 0} for _ in POLICIES]

    result = _check_editability(tree, tmp_path, policies_report)
    assert result["clean"] is True
    assert all(p["ok"] for p in result["proofs"])
    assert len(result["proofs"]) == len(POLICIES)  # one proof (the greeting) x 3 policies


def test_check_editability_catches_a_role_mismatch(tmp_path):
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("Hi [First Name],", editable=True)])])
    key = (0, 0)
    region_id = _region_id_for_key(key)
    for policy in POLICIES:
        # WRONG role on purpose -- "body" instead of the expected "merge".
        _write_bundle(tmp_path, policy, region_id=region_id, role="body", render="live", html_body="Hi [First Name],")
    policies_report = [{"protected_rasterized": 0} for _ in POLICIES]

    result = _check_editability(tree, tmp_path, policies_report)
    assert result["clean"] is False
    assert all(not p["ok"] for p in result["proofs"])


def test_check_editability_catches_a_missing_html_needle(tmp_path):
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("Hi [First Name],", editable=True)])])
    key = (0, 0)
    region_id = _region_id_for_key(key)
    for policy in POLICIES:
        # region metadata says "live" but the literal string never made it into the HTML.
        _write_bundle(tmp_path, policy, region_id=region_id, role="merge", render="live", html_body="totally different text")
    policies_report = [{"protected_rasterized": 0} for _ in POLICIES]

    result = _check_editability(tree, tmp_path, policies_report)
    assert result["clean"] is False


def test_check_editability_fails_when_any_policy_reports_protected_rasterized(tmp_path):
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("Hi [First Name],", editable=True)])])
    key = (0, 0)
    region_id = _region_id_for_key(key)
    for policy in POLICIES:
        _write_bundle(tmp_path, policy, region_id=region_id, role="merge", render="live", html_body="Hi [First Name],")
    policies_report = [{"protected_rasterized": 0}, {"protected_rasterized": 1}, {"protected_rasterized": 0}]

    result = _check_editability(tree, tmp_path, policies_report)
    assert result["clean"] is False


def test_check_editability_fails_when_a_variant_directory_is_missing(tmp_path):
    tree = TableTree(email="t", width=100, rows=[Row(cells=[_text_cell("Hi [First Name],", editable=True)])])
    key = (0, 0)
    region_id = _region_id_for_key(key)
    # Only write the "hybrid" variant -- "live" and "raster" are simply absent from disk.
    _write_bundle(tmp_path, "hybrid", region_id=region_id, role="merge", render="live", html_body="Hi [First Name],")
    policies_report = [{"protected_rasterized": 0} for _ in POLICIES]

    result = _check_editability(tree, tmp_path, policies_report)
    assert result["clean"] is False


# --- run() with the 3 heavy pipeline seams monkeypatched (synthetic-but-real emit/validate/oracle) ---


def test_run_synthetic_end_to_end_geometry_identical_and_editability_clean(tmp_path, monkeypatch):
    tree = _synthetic_tree()
    _patch_pipeline(monkeypatch, tree)

    report = run("fake.psd", tmp_path)

    assert report["geometry_identical"] is True
    assert report["editability_clean"] is True
    assert len(report["editability_proof_definitions"]) == 3  # this fixture carries all 3 known needles
    assert report["other_emails_in_psd"] == []

    assert len(report["policies"]) == 3
    for entry in report["policies"]:
        assert entry["conformance_pass"] is True
        assert entry["protected_rasterized"] == 0
        assert Path(entry["bundle_dir"]).is_dir()
        # oracle_score is either a real float (tools available) or None with available False --
        # never anything else.
        assert entry["oracle_score"] is None or isinstance(entry["oracle_score"], float)
        assert entry["oracle_available"] in (True, False)

    by_policy = {e["policy"]: e for e in report["policies"]}
    # AC-202/EARS-208: the brand-mandatory headline rasterizes under hybrid/raster, stays live
    # under "live" -- exactly the policy matrix the router promises.
    assert by_policy["live"]["brand_rasters"] == 0
    assert by_policy["hybrid"]["brand_rasters"] == 1
    assert by_policy["raster"]["brand_rasters"] == 1

    assert (tmp_path / "BAKEOFF_RUNBOOK.md").is_file()
    on_disk_report = json.loads((tmp_path / "bakeoff_report.json").read_text(encoding="utf-8"))
    assert on_disk_report["geometry_identical"] is True

    # The CTA got a real placeholder href since no link_manifest was supplied.
    hybrid_html = (tmp_path / "hybrid" / "index.html").read_text(encoding="utf-8")
    assert 'href="#review-the-toolkit"' in hybrid_html


def test_run_reports_other_emails_but_only_bakes_off_the_first(tmp_path, monkeypatch):
    first = _synthetic_tree(email="First Email")
    second = _synthetic_tree(email="Second Email")
    _patch_pipeline(monkeypatch, [first, second])

    report = run("fake.psd", tmp_path)

    assert report["other_emails_in_psd"] == ["Second Email"]
    # only the 3 policy dirs exist under out_dir -- nothing baked off for "Second Email".
    children = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert children == {"live", "hybrid", "raster"}


def test_run_raises_bakeoff_error_when_psd_solves_to_zero_emails(tmp_path, monkeypatch):
    _patch_pipeline(monkeypatch, [])
    with pytest.raises(BakeoffError):
        run("fake.psd", tmp_path)


def test_run_propagates_conformance_error_loudly_and_stops_at_the_first_failure(tmp_path, monkeypatch):
    """Adversarial: force the FIRST policy's conformance check to fail and prove run() lets
    ConformanceError propagate unchanged (never swallowed into a warning) and never proceeds to
    bake off the remaining policies once one has failed loud."""
    tree = _synthetic_tree()
    _patch_pipeline(monkeypatch, tree)

    calls = []

    def _boom(bundle_dir):
        calls.append(str(bundle_dir))
        raise ConformanceError(f"synthetic forced failure for {bundle_dir}")

    monkeypatch.setattr(bakeoff_mod, "assert_bundle", _boom)

    with pytest.raises(ConformanceError):
        run("fake.psd", tmp_path)

    # POLICIES == ("live", "hybrid", "raster") -- "live" is processed first, so run() must have
    # stopped there and never reached "hybrid"/"raster".
    assert len(calls) == 1
    assert calls[0].endswith("live")


# --- the real Intel announcement PSD, full pipeline end to end -----------------------------------


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_real_announcement_psd_full_bakeoff(tmp_path):
    report = run(ANNOUNCEMENT_PSD, tmp_path)

    assert report["geometry_identical"] is True
    assert report["editability_clean"] is True

    # The real corpus carries all 3 known proof strings (verified against the real PSD content).
    assert {p["label"] for p in report["editability_proof_definitions"]} == {
        "greeting_merge_field",
        "sender_choice_merge_field",
        "review_toolkit_cta",
    }
    assert all(p["ok"] for p in report["editability_proofs"])

    assert len(report["policies"]) == 3
    for entry in report["policies"]:
        assert entry["conformance_pass"] is True
        assert entry["protected_rasterized"] == 0
        assert isinstance(entry["oracle_score"], float), entry["oracle_detail"]
        assert 0.0 <= entry["oracle_score"] <= 1.0

    by_policy = {e["policy"]: e for e in report["policies"]}
    # Decision 2026-07-14: the real corpus is 100% Segoe UI, which routes LIVE (human-OFT proof)
    # -- no policy has anything left to rasterize but true images.
    assert by_policy["live"]["brand_rasters"] == 0
    assert by_policy["hybrid"]["brand_rasters"] == 0
    assert by_policy["raster"]["brand_rasters"] == 0

    assert (tmp_path / "BAKEOFF_RUNBOOK.md").is_file()
    for policy in ("live", "hybrid", "raster"):
        assert (tmp_path / policy / "index.html").is_file()
        assert (tmp_path / policy / "_bundle_manifest.json").is_file()


# --- CLI wiring ------------------------------------------------------------------------------------


# Fixture-free companions to the @skipif(not _has_real_psd) tests below: those are gated away on
# any machine lacking the Reference/2413101_Intel PSD fixture -- the actual state of this checkout
# -- so a wiring regression in _cmd_route (wrong arg passed to route(), a broken zero-trees
# refusal message, or a broken iter_routed/render_role JSON assembly) would ship with a fully
# green suite. These monkeypatch the same two heavy seams `_cmd_route` calls
# (psd_to_layout_tree, table_solver.build_table_trees) so the wiring is proven on every machine.
def test_cli_route_wiring_prints_expected_json_without_a_real_psd(monkeypatch, capsys):
    tree = _synthetic_tree()
    monkeypatch.setattr(cli, "psd_to_layout_tree", lambda psd: _fake_layout_tree())
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [tree])

    exit_code = cli.main(["route", "x.psd", "--policy", "hybrid"])
    assert exit_code == 0

    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["policy"] == "hybrid"
    assert summary["email"] == tree.email
    assert summary["width"] == tree.width
    assert summary["regions"], "expected at least one routed region"
    assert all(r["verb"] in ("live", "raster") for r in summary["regions"])


def test_cli_route_refuses_when_psd_solves_to_zero_emails(monkeypatch, capsys):
    monkeypatch.setattr(cli, "psd_to_layout_tree", lambda psd: _fake_layout_tree())
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [])

    exit_code = cli.main(["route", "x.psd", "--policy", "hybrid"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "solved to zero" in err


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_cli_route_prints_a_json_routing_summary(capsys):
    exit_code = cli.main(["route", ANNOUNCEMENT_PSD, "--policy", "hybrid"])
    assert exit_code == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["policy"] == "hybrid"
    assert summary["regions"], "expected at least one routed region"
    assert all(r["verb"] in ("live", "raster") for r in summary["regions"])


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_cli_emit_writes_a_bundle(tmp_path, capsys):
    out_dir = tmp_path / "emitted"
    exit_code = cli.main(["emit", ANNOUNCEMENT_PSD, "--policy", "hybrid", "-o", str(out_dir)])
    assert exit_code == 0
    assert (out_dir / "index.html").is_file()
    assert (out_dir / "_bundle_manifest.json").is_file()
    printed = capsys.readouterr().out
    assert "index_path" in printed


@pytest.mark.skipif(not _has_real_psd, reason="Intel announcement PSD fixture not present")
def test_cli_bakeoff_exits_zero_and_prints_the_full_report(tmp_path, capsys):
    out_dir = tmp_path / "bakeoff"
    exit_code = cli.main(["bakeoff", ANNOUNCEMENT_PSD, "-o", str(out_dir)])
    assert exit_code == 0

    printed = capsys.readouterr().out
    report = json.loads(printed)
    assert report["geometry_identical"] is True
    assert report["editability_clean"] is True
    assert len(report["policies"]) == 3
