"""intake_validator: the fail-loud SOP gate's structured report.

Synthetic LayoutTree fixtures for the unit cases; a guarded pass over the real 5-PSD Intel corpus
for the end-to-end "real run" check (skips cleanly if that reference directory isn't present on
the machine running the suite).
"""

from __future__ import annotations

import glob
import os

import pytest

from psd_html.intake_validator import validate_tree
from psd_html.layout_tree import BBox, Canvas, Layer, LayoutTree, TextInfo
from psd_html.psd_adapter import psd_to_layout_tree
from psd_html.table_solver import SafetyInvariantViolation, build_table_trees, find_baked_text_cells

_INTEL_GLOB = r"c:/Users/KaiMallari/Documents/Kai-Intercept/Reference/2413101_Intel/PSDs/**/*.psd"


def _layer(id, name, kind, bbox, z, parent=None, visible=True, text=None):
    return Layer(
        id=id,
        name=name,
        kind=kind,
        visible=visible,
        opacity=1.0,
        bbox=BBox(*bbox) if bbox else None,
        z=z,
        is_group=(kind in ("group", "artboard")),
        parent=parent,
        text=text,
    )


# --- unit cases: synthetic fixtures --------------------------------------------------------------


def test_validate_passes_clean_psd():
    tree = LayoutTree(
        psd="clean.psd",
        path="C:/fake/clean.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "headline", "type", (0, 0, 300, 50), z=1, parent=1, text=TextInfo("Hello")),
            _layer(3, "body", "type", (0, 50, 300, 150), z=2, parent=1, text=TextInfo("World")),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is True
    assert report["violations"] == []
    assert report["info"] == []
    assert report["artboard_count"] == 1


def test_validate_flags_trapped_editable_field():
    tree = LayoutTree(
        psd="bad.psd",
        path="C:/fake/bad.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 300), z=0),
            _layer(2, "field text", "type", (0, 0, 100, 20), z=1, parent=1, text=TextInfo("[First Name]")),
            _layer(3, "icon", "shape", (50, 10, 150, 30), z=2, parent=1),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is False
    trapped = [v for v in report["violations"] if v["type"] == "baked_text_in_rasterized_cell"]
    assert len(trapped) == 1
    assert 2 in trapped[0]["layer_ids"]
    assert trapped[0]["artboard"] == "Artboard 1"
    # Defect 5/6: violations carry layer NAMES alongside ids.
    assert "field text" in trapped[0]["layer_names"]
    # Same shape the safety invariant itself would refuse to build.
    with pytest.raises(SafetyInvariantViolation):
        build_table_trees(tree)


def test_validate_flags_button_group_swallowing_extra_merge_field():
    tree = LayoutTree(
        psd="btn.psd",
        path="C:/fake/btn.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "CTA button", "group", None, z=1, parent=1),
            _layer(3, "bg", "shape", (0, 0, 200, 50), z=2, parent=2),
            _layer(4, "label", "type", (10, 10, 100, 40), z=3, parent=2, text=TextInfo("[Label]")),
            _layer(5, "extra field", "type", (110, 10, 190, 40), z=4, parent=2, text=TextInfo("[Extra]")),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is False
    kinds = {v["type"] for v in report["violations"]}
    assert "button_swallows_field" in kinds
    trapped = [v for v in report["violations"] if v["type"] == "baked_text_in_rasterized_cell"]
    assert trapped and "extra field" in trapped[0]["layer_names"]
    # S1.1 Defect 3 fix: find_baked_text_cells now inspects role=button cells too (not just
    # role=graphic), so build's narrower structural invariant catches this shape as well -- build
    # and validate agree instead of only the broader SOP validator catching it.
    with pytest.raises(SafetyInvariantViolation) as exc_info:
        build_table_trees(tree)
    trapped_ids = {lid for v in exc_info.value.violations for lid in v["layer_ids"]}
    assert 5 in trapped_ids


def test_validate_warns_on_unsupported_construct_but_still_passes():
    tree = LayoutTree(
        psd="warn.psd",
        path="C:/fake/warn.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1],
        layers=[
            _layer(1, "Artboard 1", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "headline", "type", (0, 0, 300, 50), z=1, parent=1, text=TextInfo("Hello")),
            _layer(3, "Layer 1", "smartobject", (0, 50, 300, 150), z=2, parent=1),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is True
    kinds = {w["type"] for w in report["warnings"]}
    assert "unsupported_construct" in kinds


def test_validate_multi_artboard_info():
    tree = LayoutTree(
        psd="multi.psd",
        path="C:/fake/multi.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[1, 10],
        layers=[
            _layer(1, "Email A", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "a text", "type", (0, 0, 300, 50), z=1, parent=1, text=TextInfo("A")),
            _layer(10, "Email B", "artboard", (0, 0, 300, 150), z=2),
            _layer(11, "b text", "type", (0, 0, 300, 50), z=3, parent=10, text=TextInfo("B")),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is True
    assert report["artboard_count"] == 2
    info_types = {i["type"] for i in report["info"]}
    assert "multi_artboard" in info_types


# --- real run: all 5 Intel PSDs -------------------------------------------------------------------
#
# S1.1 Defect 4 fix: this used to treat a SafetyInvariantViolation as a PASSING branch (a
# tautology -- `build_layer_ids <= trapped_violation_layer_ids | build_layer_ids` is true for ANY
# set), which is exactly what let 4/5 PSDs silently refuse to build behind a green checkmark.
#
# S1.2 Defect C fix: this used to assert zero EDITABLE fields trapped (`find_trapped_editable_cells`,
# which only fires on bracketed/highlight-marked text) -- that is a narrower, weaker claim than the
# S1.2 bar and would NOT have caught e.g. a plain non-editable caption baked into a `... graphic`
# group, or a plain second text baked into a `... button` group. This version asserts the tightened
# bar: every one of the 5 Intel PSDs BUILDS successfully, with ZERO baked text -- editable or not --
# left in any rasterized (graphic OR button) cell (`find_baked_text_cells`, at every nesting depth),
# and validate() independently PASSES on all 5. A SafetyInvariantViolation raised by build() is NOT
# caught here -- it is a genuine test failure (an uncaught exception), exactly as it should be, so
# this is not a tautology: a PSD that refuses to build, OR one that builds but leaves baked text
# behind, both fail this test for real.

_intel_paths = sorted(glob.glob(_INTEL_GLOB, recursive=True))


@pytest.mark.skipif(not _intel_paths, reason="Reference/2413101_Intel PSD corpus not present on this machine")
def test_real_corpus_all_five_psds_build_clean_and_validate_passes():
    assert len(_intel_paths) == 5, f"expected the 5-PSD Intel corpus, found {len(_intel_paths)}"

    per_psd_baked_text: dict = {}
    per_psd_pass: dict = {}

    for path in _intel_paths:
        name = os.path.basename(path)
        tree = psd_to_layout_tree(path)

        report = validate_tree(tree)
        per_psd_pass[name] = report["pass"]

        # No try/except around this call: a SafetyInvariantViolation here must fail the test, not
        # be swallowed into a "passing" branch.
        trees = build_table_trees(tree)

        baked_text = [c for t in trees for c in find_baked_text_cells(t)]
        per_psd_baked_text[name] = baked_text

    for name in per_psd_pass:
        assert per_psd_pass[name], f"{name}: validate() reported violations (build must not disagree)"
        assert per_psd_baked_text[name] == [], (
            f"{name}: build() emitted a tree with live text (editable or not) baked into a "
            f"rasterized cell: {per_psd_baked_text[name]}"
        )


# --- flat (no-artboard) PSD: a deliberately supported input shape (idx 38) ----------------------


def test_validate_flat_psd_with_no_artboards_passes_clean():
    # artboards=[] -> the [None] single-region path; every fixture elsewhere declares artboards=[1].
    tree = LayoutTree(
        psd="flat.psd",
        path="C:/fake/flat.psd",
        canvas=Canvas(width=300, height=150),
        artboards=[],
        layers=[
            _layer(2, "headline", "type", (0, 0, 300, 50), z=1, parent=None, text=TextInfo("Hello")),
            _layer(3, "body", "type", (0, 50, 300, 150), z=2, parent=None, text=TextInfo("World")),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is True
    assert report["artboard_count"] == 1
    assert report["info"] == []  # single region -> no multi_artboard info


def test_validate_flat_psd_violation_is_attributed_to_the_psd_filename():
    # On a no-artboard tree the region name falls back to tree.psd (or 'canvas'); a violation must
    # carry that fallback, not crash on a missing artboard layer.
    tree = LayoutTree(
        psd="flat.psd",
        path="C:/fake/flat.psd",
        canvas=Canvas(width=300, height=300),
        artboards=[],
        layers=[
            _layer(2, "field text", "type", (0, 0, 100, 20), z=1, parent=None, text=TextInfo("[First Name]")),
            _layer(3, "icon", "shape", (50, 10, 150, 30), z=2, parent=None),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is False
    trapped = [v for v in report["violations"] if v["type"] == "baked_text_in_rasterized_cell"]
    assert trapped and trapped[0]["artboard"] == "flat.psd"


# --- multi-artboard: EVERY offending layer across EVERY artboard, correctly attributed (39/40/41)


def test_validate_multi_artboard_reports_violations_from_both_artboards_attributed():
    tree = LayoutTree(
        psd="multi.psd",
        path="C:/fake/multi.psd",
        canvas=Canvas(width=300, height=320),
        artboards=[1, 10],
        layers=[
            # Artboard A: a bracketed field trapped under an overlapping shape -> baked_text.
            _layer(1, "Email A", "artboard", (0, 0, 300, 150), z=0),
            _layer(2, "field text", "type", (0, 0, 100, 20), z=1, parent=1, text=TextInfo("[First Name]")),
            _layer(3, "icon", "shape", (50, 10, 150, 30), z=2, parent=1),
            # Artboard B: a button group swallowing a SECOND bracketed field -> button_swallows_field.
            _layer(10, "Email B", "artboard", (0, 170, 300, 320), z=3),
            _layer(11, "CTA button", "group", None, z=4, parent=10),
            _layer(12, "bg", "shape", (0, 170, 200, 220), z=5, parent=11),
            _layer(13, "label", "type", (10, 180, 100, 210), z=6, parent=11, text=TextInfo("[Label]")),
            _layer(14, "extra field", "type", (110, 180, 190, 210), z=7, parent=11, text=TextInfo("[Extra]")),
        ],
    )
    report = validate_tree(tree)
    assert report["pass"] is False

    types = {v["type"] for v in report["violations"]}
    assert "baked_text_in_rasterized_cell" in types
    assert "button_swallows_field" in types

    # Each violation is attributed to the artboard it occurred on (not the last-seen name).
    artboards_with_violations = {v["artboard"] for v in report["violations"]}
    assert "Email A" in artboards_with_violations
    assert "Email B" in artboards_with_violations

    # ...and the per-artboard grouping in report['artboards'] matches, neither group empty.
    grouped = {r["artboard"]: r["violations"] for r in report["artboards"]}
    assert grouped["Email A"], "Email A group must carry its own violation"
    assert grouped["Email B"], "Email B group must carry its own violation"
