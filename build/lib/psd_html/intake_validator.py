"""The fail-loud SOP gate (docs/PSD-for-HTML_Authoring-SOP.md #8).

Validates a PSD against the authoring SOP and returns a structured report. This module itself
never raises -- that is `table_solver.enforce_safety_invariant`'s job for the `build` fail-loud
path. `validate_psd()` / `validate_tree()` exist so a non-conforming PSD comes back with EVERY
offending layer at once (across every artboard), instead of `build` stopping at the first
exception. Conforming PSDs (or ones with only soft warnings/info) come back with pass=True.

Report shape::

    {
      psd, path, pass: bool, artboard_count: int,
      violations: [ {type, message, artboard, layer_ids, rect?} ... ],   # HARD -> pass=False
      warnings:   [ {type, message, artboard, layer_id, layer_name, kind} ... ],   # soft
      info:       [ {type, message, artboard_count} ... ],
      artboards:  [ {artboard, violations, warnings} ... ],   # same entries, grouped per-artboard
    }

Hard violations (reject, pass=False):
  - baked_text_in_rasterized_cell  live text -- editable OR NOT -- ends up baked into a
                            RASTERIZED cell with no live copy surviving -- either a role=graphic
                            cell (a named `... graphic` group, or a residual-overlap flatten via
                            table_solver._cell_from_flatten) whose source includes ANY text/type
                            layer, OR a role=button cell whose group baked a SECOND text member
                            into its image beyond the single label kept live. This is exactly
                            `table_solver.find_baked_text_cells()` (S1.2 Defect C; formerly
                            `find_trapped_editable_cells`, which only looked at editable/bracketed
                            text and missed plain baked copy) -- the same check
                            `enforce_safety_invariant` uses to fail loud on `build`, at ANY
                            nesting depth (Cell is nestable via role="rows").
  - button_swallows_field   a `... button` group aggregates more than one bracketed
                            (merge-field-looking) text layer; only one can survive as the
                            button's live label (layer_classifier picks the first text member) --
                            every other bracketed member would be silently dropped.

Soft warnings (do not reject):
  - unsupported_construct   a visible smart object / adjustment layer -- psd-tools can't carry it
                            live; SOP #5.7 says bake/flatten it before handoff.

Info (do not reject):
  - multi_artboard          the PSD declares N>1 artboards -> N emails (SOP #5.4).
"""

from __future__ import annotations

from . import grid_analyzer as G
from .layer_classifier import ROLE_BUTTON, classify_artboard
from .psd_adapter import psd_to_layout_tree
from .table_solver import find_baked_text_cells, solve_artboard

_UNSUPPORTED_KINDS = ("smartobject", "adjustment")


def _is_bracketed(layer) -> bool:
    if layer is None or layer.kind != "type" or layer.text is None:
        return False
    return "[" in str(layer.text.content or "")


def _names_for(layer_ids: list, by_id: dict) -> list:
    """Best-effort layer NAMES for a list of layer ids (Defect 5/6) -- `None` for any id that
    isn't in `by_id` (e.g. a synthetic negative id) rather than raising, so a violation always
    names every layer it can."""
    return [by_id[lid].name if lid in by_id else None for lid in layer_ids]


def _button_swallowed_fields(items: list, by_id: dict) -> list:
    """Hard violation: a `... button` group whose members include MORE than one bracketed
    merge-style text layer. layer_classifier keeps only the first text member as the button's
    live label; any additional bracketed members are silently dropped rather than trapped in a
    graphic (so `find_baked_text_cells` never sees them) -- this is the one violation shape
    that check can't catch on its own."""
    violations = []
    for item in items:
        if item.role != ROLE_BUTTON or len(item.layer_ids) <= 1:
            continue
        bracketed_ids = [lid for lid in item.layer_ids if _is_bracketed(by_id.get(lid))]
        if len(bracketed_ids) > 1:
            violations.append(
                {
                    "type": "button_swallows_field",
                    "message": (
                        f"button group {item.name!r} aggregates {len(bracketed_ids)} bracketed "
                        "merge fields; only one can stay live as the button label -- the rest "
                        "would be silently dropped"
                    ),
                    "layer_ids": bracketed_ids,
                    "layer_names": _names_for(bracketed_ids, by_id),
                }
            )
    return violations


def _unsupported_constructs(tree, artboard_id, by_id: dict) -> list:
    warnings = []
    for l in tree.layers:
        if not l.visible or l.kind not in _UNSUPPORTED_KINDS:
            continue
        if G._artboard_of(l, by_id) != artboard_id:
            continue
        warnings.append(
            {
                "type": "unsupported_construct",
                "message": (
                    f"{l.kind} layer {l.name!r} is not carried live by psd-tools; "
                    "bake/flatten before handoff (SOP #5.7)"
                ),
                "layer_id": l.id,
                "layer_name": l.name,
                "kind": l.kind,
            }
        )
    return warnings


def validate_tree(tree) -> dict:
    """Validate an already-loaded LayoutTree. Returns the structured report; never raises."""
    by_id = {l.id: l for l in tree.layers}
    artboard_ids = list(tree.artboards) if tree.artboards else [None]

    all_violations: list = []
    all_warnings: list = []
    artboard_reports: list = []

    for ab_id in artboard_ids:
        name = by_id[ab_id].name if ab_id in by_id else (tree.psd or "canvas")
        items = classify_artboard(tree, ab_id, by_id)
        region_bbox = G._region_bbox_for(ab_id, by_id, tree.canvas)
        width = region_bbox["right"] - region_bbox["left"]
        solved = solve_artboard(items, name, width)

        ab_violations: list = []
        for v in find_baked_text_cells(solved):
            names = _names_for(v["layer_ids"], by_id)
            ab_violations.append(
                {
                    "type": "baked_text_in_rasterized_cell",
                    "message": (
                        f"text layer(s) {v['layer_ids']} ({names}) are baked into a rasterized "
                        f"(graphic or button) cell at {v['rect']} with no live copy surviving -- "
                        f"never emit this (SOP #8)"
                    ),
                    "layer_ids": v["layer_ids"],
                    "layer_names": names,
                    "rect": v["rect"],
                }
            )
        ab_violations.extend(_button_swallowed_fields(items, by_id))
        for v in ab_violations:
            v["artboard"] = name

        ab_warnings = _unsupported_constructs(tree, ab_id, by_id)
        for w in ab_warnings:
            w["artboard"] = name

        all_violations.extend(ab_violations)
        all_warnings.extend(ab_warnings)
        artboard_reports.append({"artboard": name, "violations": ab_violations, "warnings": ab_warnings})

    info: list = []
    if len(artboard_ids) > 1:
        info.append(
            {
                "type": "multi_artboard",
                "message": f"PSD declares {len(artboard_ids)} artboards -> {len(artboard_ids)} emails (SOP #5.4)",
                "artboard_count": len(artboard_ids),
            }
        )

    return {
        "psd": tree.psd,
        "path": tree.path,
        "pass": len(all_violations) == 0,
        "artboard_count": len(artboard_ids),
        "violations": all_violations,
        "warnings": all_warnings,
        "info": info,
        "artboards": artboard_reports,
    }


def validate_psd(psd_path: str) -> dict:
    """Load a PSD and run the fail-loud SOP intake validator against it. Never raises on a
    non-conforming PSD -- the point is a complete structured report, not an exception."""
    tree = psd_to_layout_tree(psd_path)
    return validate_tree(tree)
