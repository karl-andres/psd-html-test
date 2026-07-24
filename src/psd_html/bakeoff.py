"""F-FIDELITY-BAKEOFF (builder 7 of 7): run the SAME solved PSD through all three
`layer_router.POLICIES` and report whether the fidelity/editability trade-off holds.

`run(psd_path, out_dir, copy_manifest=None, link_manifest=None, density=1.0)` (density scales layout for retina/2x PSDs, forwarded to emit()):

  1. Solve the PSD ONCE (`psd_adapter.psd_to_layout_tree` -> `table_solver.build_table_trees`) and
     composite it ONCE (`rasterizer.composite_psd`) -- both are the expensive steps, and geometry
     never differs across policies, so there is no reason to repeat either per policy.
  2. For each of `layer_router.POLICIES` ("live", "hybrid", "raster"), route a FRESH
     `TableTree.from_dict(tree.to_dict())` copy (never the same object across policies -- this is
     what makes AC-201 below a real check of `route()`'s no-mutation guarantee, not a tautology
     against a single shared object) and emit a bundle to `out_dir/<policy>/`.
  3. AC-201: assert the 3 policies' solved geometry is byte-identical (`TableTree.to_dict()`
     equality) -- policies differ ONLY in verbs, never in geometry.
  4. Every bundle is run through `conformance_validator.assert_bundle` (FAIL LOUD -- a bake-off
     bundle that is not OFT-safe is a bug, not a warning) and `oracle.score_bundle` (a geometry
     proxy score; never raises, degrades to `available: False` if Chromium/Pillow are unusable).
  5. AC-202/205 AUTOMATED EDITABILITY TEST: scan every emitted variant's `regions.json` +
     `index.html` on disk and assert no PROTECTED (merge/cta/body) region ever resolved to a
     raster region, AND specifically prove that whichever known merge/cta fields this PSD happens
     to carry (a personalization greeting, a sender-choice bracket token, a "review the toolkit"
     CTA -- the exact strings the Intel announcement email corpus carries) are live text/links in
     EVERY variant. A PSD that does not carry one of those exact strings simply contributes no
     proof for it (this module never invents content); the general merge/cta/body-never-raster
     count check still applies to every PSD regardless.

The actual `.OFT` round-trip needs classic Outlook COM automation, which cannot run in this build
environment -- it is OPERATOR-GATED. `run()` never attempts it; it only writes a runbook
(`BAKEOFF_RUNBOOK.md` + the same text in the returned report's `"runbook"` key) explaining the
remaining manual step.
"""

from __future__ import annotations

import json
import re as _re
from pathlib import Path
from typing import Mapping, Optional

from .conformance_validator import assert_bundle
from .html_emitter import emit
from .layer_router import POLICIES, ROLE_BODY, ROLE_CTA, ROLE_MERGE, route
from .oracle import score_bundle
from .psd_adapter import psd_to_layout_tree
from .rasterizer import composite_psd
from .table_solver import build_table_trees
from .table_tree import TableTree

# `render_role()` OUTPUT values (the `role` field each region carries in regions.json, NOT the
# raw Cell.role input) that `layer_router.is_protected` treats as PROTECTED -- a region with one
# of these roles must NEVER resolve to "raster", under any policy, in any variant.
_PROTECTED_ROLES = (ROLE_MERGE, ROLE_CTA, ROLE_BODY)

# The specific known merge/cta strings the Intel announcement email bake-off target carries (see
# the F-FIDELITY-BAKEOFF build spec). `_editability_proofs` looks these up in the SOLVED tree by
# content, so a PSD that doesn't carry one of them simply contributes no proof entry for it --
# nothing here is invented/assumed about a PSD's actual content.
_KNOWN_PROOF_NEEDLES = (
    ("greeting_merge_field", "Hi [First Name]", "merge"),
    ("sender_choice_merge_field", "depending on sender", "merge"),
    ("review_toolkit_cta", "Review the toolkit", "cta"),
)

# SAFETY: this is a plain-text markdown template filled with filesystem paths via str.format() --
# not a SQL string. No query is ever built from this.
_RUNBOOK_TEMPLATE = """\
BAKEOFF RUNBOOK -- operator-gated .OFT round-trip
==================================================

Source PSD: {psd}
Bundles written under: {out_dir}

This bake-off produced 3 OFT-safe HTML bundles (raster/, live/, hybrid/) under the directory
above, each already conformance-validated (`conformance_validator.assert_bundle`) and geometry-
scored by the headless-Chromium geometry oracle (`oracle.score_bundle` -- a GEOMETRY PROXY only,
see that module's docstring; it is not a Word-engine render).

The .OFT round-trip itself needs classic Outlook COM automation, which cannot run in this build
environment -- it is OPERATOR-GATED. This tool does NOT attempt to invoke Outlook. To finish the
bake-off by hand:

  1. On an authoring box with CLASSIC (non-New) Outlook + Word installed, for EACH policy
     (raster/, live/, hybrid/) run:

         Tools\\PSD-HTML\\grammar\\Convert-HtmlToOft.ps1 -HtmlPath <policy>/index.html

  2. Open each resulting .oft in CLASSIC Outlook specifically -- New Outlook / OWA use a
     different, non-Word rendering engine and would not exercise the Word-engine LCD this
     bundle was authored against.

  3. For each policy, confirm by hand:
       - EDITABILITY: every merge/placeholder field, every body-copy region, and the CTA link
         are genuinely live/editable text in the opened draft -- never a picture.
       - VISUAL RESIDUAL: how close the rendered draft looks to the original PSD comp, especially
         for any raster brand-headline region (re-typeset from final copy, never pixel-cropped).

  4. Record the D6 decision: the owner picks the shipping default policy -- "hybrid" is the
     PRESUMPTIVE default (it rasterizes only brand-mandatory headlines and keeps every merge/cta/
     body field live) -- with a one-line rationale, informed by what step 3 showed.

Do NOT attempt to invoke Outlook automatically from this tool: COM automation of a real Outlook
client is out of scope for this build and this environment.
"""


class BakeoffError(RuntimeError):
    """Loud failure for a bake-off-level problem that is not one specific bundle's conformance
    (that is `conformance_validator.ConformanceError`, which `run()` lets propagate unchanged) --
    e.g. a PSD that solves to zero artboards/emails at all."""


def _iter_leaf_cells(rows: list, prefix: tuple = ()):
    """Depth-first walk yielding every LEAF cell with its deterministic path-index key -- the
    SAME walk shape `layer_router._iter_leaf_cells` uses, reproduced here (not imported) because
    it is the one bit of tree-walking this module needs before a `RoutedTree` exists yet (to
    build the default link manifest and to locate the known proof needles in the freshly solved,
    not-yet-routed tree)."""
    for row_idx, row in enumerate(rows):
        for cell_idx, cell in enumerate(row.cells):
            key = prefix + (row_idx, cell_idx)
            if cell.role == "rows":
                nested = cell.rows or []
                yield from _iter_leaf_cells(nested, key + ("rows",))
            else:
                yield cell, key


def _region_id_for_key(key: tuple) -> str:
    """The SAME region_id `html_emitter._render_cell` derives from a leaf's path-index key --
    reproduced here so this module can look up a specific leaf's emitted region record by content
    without importing an html_emitter internal."""
    return "_".join(str(p) for p in key)


def _collect_link_slots(tree: TableTree) -> list:
    slots: list = []
    seen: set = set()
    for cell, _key in _iter_leaf_cells(tree.rows):
        if cell.link_slot and cell.link_slot not in seen:
            seen.add(cell.link_slot)
            slots.append(cell.link_slot)
    return slots


def _default_link_manifest(tree: TableTree) -> dict:
    """When the caller supplies no `link_manifest`, resolve every distinct `link_slot` this tree
    carries to a placeholder anchor (`#<slot>`) instead of leaving every CTA/linked-text region
    href-less. This is what lets the editability proof below demonstrate a REAL `<a href>`
    (not just a live, unlinked `<span>`) for "live text/links" -- late link binding stays fully
    optional for real callers (pass an explicit `link_manifest` to override), but a bake-off with
    no manifest at all should still show the CTA behaving like a link."""
    return {slot: f"#{slot}" for slot in _collect_link_slots(tree)}


def _find_leaf_containing(tree: TableTree, needle: str):
    """First leaf cell (in walk order) whose `text.content` contains `needle`, or `(None, None)`
    if no leaf carries it. Cell identity is the stable path-index key (see layer_router module
    docstring), so this key is valid to look up in ANY separately-routed copy of a structurally
    identical tree."""
    for cell, key in _iter_leaf_cells(tree.rows):
        if cell.text is not None and cell.text.content and needle in cell.text.content:
            return cell, key
    return None, None


def _editability_proofs(tree: TableTree) -> list:
    """Resolve `_KNOWN_PROOF_NEEDLES` against the solved tree once. Returns a list of
    `{"label", "needle", "expected_role", "key"}` -- only for needles this PSD actually carries."""
    proofs = []
    for label, needle, expected_role in _KNOWN_PROOF_NEEDLES:
        cell, key = _find_leaf_containing(tree, needle)
        if cell is None:
            continue
        proofs.append({"label": label, "needle": needle, "expected_role": expected_role, "key": key})
    return proofs


def _region_counts(regions: list) -> dict:
    protected_kept_live = 0
    protected_rasterized = 0
    brand_rasters = 0
    for region in regions:
        role = region.get("role")
        if role in _PROTECTED_ROLES:
            if region.get("render") == "live":
                protected_kept_live += 1
            else:
                protected_rasterized += 1
        if role == "brand_headline" and region.get("render") == "raster":
            brand_rasters += 1
    return {
        "protected_kept_live": protected_kept_live,
        "protected_rasterized": protected_rasterized,
        "brand_rasters": brand_rasters,
    }


def _check_editability(tree: TableTree, out_root: Path, policies_report: list) -> dict:
    """AC-202/205: read every emitted variant's `regions.json` + `index.html` BACK OFF DISK (not
    just the in-memory `emit()` return value -- this proves the written artifact matches, not
    just what this process happened to hold in memory) and check:
      - the aggregate merge/cta/body-never-raster count (already computed into
        `policies_report[*]["protected_rasterized"]`, must sum to zero across every policy), and
      - each `_editability_proofs(tree)` entry: the named region resolved as `render == "live"`
        under its expected role, AND its known content string is actually present in the
        rendered `index.html` -- in every policy variant.
    """
    proofs = _editability_proofs(tree)
    proof_results: list = []
    all_ok = sum(p["protected_rasterized"] for p in policies_report) == 0

    for policy in POLICIES:
        bundle_dir = out_root / policy
        regions_path = bundle_dir / "regions.json"
        index_path = bundle_dir / "index.html"
        if not regions_path.is_file() or not index_path.is_file():
            all_ok = False
            continue
        regions = json.loads(regions_path.read_text(encoding="utf-8"))
        regions_by_id = {r["region_id"]: r for r in regions}
        html_text = index_path.read_text(encoding="utf-8")
        # The needle proves the CONTENT survived live, not any markup shape: a merge-token
        # highlight <span> legitimately splits a needle mid-string ("Hi <span ...>[First
        # Name],</span>"), so also match against the tag-stripped text.
        text_only = _re.sub(r"<[^>]+>", "", html_text)

        for proof in proofs:
            region_id = _region_id_for_key(proof["key"])
            region = regions_by_id.get(region_id)
            ok = (
                region is not None
                and region.get("role") == proof["expected_role"]
                and region.get("render") == "live"
                and (proof["needle"] in html_text or proof["needle"] in text_only)
            )
            proof_results.append({"policy": policy, "label": proof["label"], "needle": proof["needle"], "ok": ok})
            if not ok:
                all_ok = False

    # Surface the basis of `clean` loudly: with zero known proof needles present, `clean` rests on
    # the protected-never-rasterized invariant ALONE, not on any positive per-field editability
    # proof -- never let a proof-less PSD read as "editability positively proven".
    result = {
        "clean": all_ok,
        "proofs": proof_results,
        "proofs_found": len(proofs),
        "proof_definitions": [
            {"label": p["label"], "needle": p["needle"], "expected_role": p["expected_role"]} for p in proofs
        ],
    }
    if not proofs:
        result["note"] = (
            "no known editability-proof needles present in this PSD; `clean` reflects the "
            "protected-never-rasterized invariant only, not a positive per-field editability proof"
        )
    return result


def run(
    psd_path,
    out_dir,
    copy_manifest: Optional[Mapping] = None,
    link_manifest: Optional[Mapping] = None,
    density: float = 1.0,
) -> dict:
    """Run the raster/live/hybrid bake-off for `psd_path` into `out_dir/<policy>/`.

    Solves the PSD ONCE and composites it ONCE, then routes+emits a bundle per
    `layer_router.POLICIES`. Only the FIRST solved artboard/email is baked off (this module's
    target is one email at a time, matching the announcement-email bake-off spec) -- a
    multi-artboard PSD's other emails are reported by name in `"other_emails_in_psd"` but not
    baked off; call `run()` again against a PSD/email that isolates just that artboard if you
    need those too.

    `copy_manifest`/`link_manifest` are the same late-copy-binding manifests `html_emitter.emit`
    takes. If `link_manifest` is omitted, every distinct `link_slot` this PSD's cells carry is
    given a placeholder `#<slot>` anchor (see `_default_link_manifest`) so CTAs demonstrably
    render as real links even with no manifest supplied.

    Returns a report dict:
        {"psd", "out_dir", "policies": [{"policy", "bundle_dir", "conformance_pass",
         "oracle_score", "oracle_available", "protected_kept_live", "protected_rasterized",
         "brand_rasters", "warnings", "overflow_flags"}, ...],
         "geometry_identical": bool, "editability_clean": bool, "editability_proofs": [...],
         "other_emails_in_psd": [...], "runbook": str}

    Raises `conformance_validator.ConformanceError` (FAIL LOUD, propagated unchanged) if any
    emitted bundle is not OFT-safe -- a bake-off bundle failing conformance is a real bug, not a
    warning. Raises `BakeoffError` if the PSD solves to zero emails at all.
    """
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    layout = psd_to_layout_tree(psd_path)
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout)
    if not trees:
        raise BakeoffError(f"bakeoff.run: {psd_path!s} solved to zero artboards/emails to bake off")
    tree = trees[0]

    composite = composite_psd(psd_path)

    effective_link_manifest = link_manifest if link_manifest is not None else _default_link_manifest(tree)

    policies_report: list = []
    serialized_by_policy: dict = {}

    for policy in POLICIES:
        tree_copy = TableTree.from_dict(tree.to_dict())
        routed = route(tree_copy, policy)
        serialized_by_policy[policy] = routed.tree.to_dict()

        bundle_dir = out_root / policy
        result = emit(
            routed,
            bundle_dir,
            copy_manifest=copy_manifest,
            link_manifest=effective_link_manifest,
            composite=composite,
            layer_names=layer_names,
            psd_path=psd_path,
            density=density,
        )

        conformance = assert_bundle(bundle_dir)  # FAIL LOUD: raises ConformanceError on failure.
        oracle_result = score_bundle(result["index_path"], composite, viewport_width=tree.width)

        counts = _region_counts(result["regions"])
        policies_report.append(
            {
                "policy": policy,
                "bundle_dir": str(bundle_dir),
                "conformance_pass": bool(conformance["pass"]),
                "oracle_score": oracle_result["score"],
                "oracle_available": oracle_result["available"],
                "oracle_detail": oracle_result["detail"],
                "warnings": result["warnings"],
                "overflow_flags": result["overflow_flags"],
                **counts,
            }
        )

    # AC-201: the 3 policies' solved geometry is identical -- they differ ONLY in verbs. Each
    # policy routed a FRESH TableTree.from_dict copy (never the same object), so this equality is
    # a real check of route()'s no-mutation guarantee, not a tautology against a shared object.
    geometry_identical = len({json.dumps(d, sort_keys=True) for d in serialized_by_policy.values()}) == 1

    editability = _check_editability(tree, out_root, policies_report)

    other_emails_in_psd = [t.email for t in trees[1:]]

    runbook_text = _RUNBOOK_TEMPLATE.format(psd=str(psd_path), out_dir=str(out_root))
    (out_root / "BAKEOFF_RUNBOOK.md").write_text(runbook_text, encoding="utf-8")

    report = {
        "psd": str(psd_path),
        "out_dir": str(out_root),
        "policies": policies_report,
        "geometry_identical": geometry_identical,
        "editability_clean": editability["clean"],
        "editability_proofs": editability["proofs"],
        "editability_proof_definitions": editability["proof_definitions"],
        "other_emails_in_psd": other_emails_in_psd,
        "runbook": runbook_text,
    }
    (out_root / "bakeoff_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
