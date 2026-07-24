"""link_scaffold: generate a starter links.json for a PSD.

Designers/producers currently have to guess the manifest schema and hand-discover every real
`link_slot` (from `table_solver`'s layer-name slugging) or `region_id` (from `html_emitter`'s
region walk) a PSD actually exposes. This module removes the guessing: it runs the SAME
routing + emit pipeline `cli emit` runs -- into a scratch directory, discarded after -- and reads
back the `regions` list `emit()` already returns, so a candidate here can never drift from what
the real pipeline would produce. See grammar/PIPELINE.md for the links.json schema itself.

The generated manifest's real `slots`/`regions`/`inline` sections are left genuinely EMPTY (safe
to run as-is, binds nothing) -- never pre-populated with a placeholder value, since an
empty-string href would still satisfy `region_id in regions` at emit time and bind as a real,
broken link. Discovered candidates go in an advisory `_candidates` block instead, which the
pipeline's own manifest loader ignores (it only reads `slots`/`regions`/`inline`).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

from .html_emitter import emit
from .layer_router import route
from .psd_adapter import psd_to_layout_tree
from .rasterizer import composite_psd
from .table_solver import build_table_trees


def _iter_text_contents(rows):
    """Every non-empty text-cell content in `rows`, depth-first (role="rows" containers walked
    into). Used only to surface inline-citation CANDIDATE text -- inline entries match by
    substring, not region id, so no id correlation is needed here."""
    for row in rows:
        for cell in row.cells:
            if cell.role == "rows" and cell.rows:
                yield from _iter_text_contents(cell.rows)
            elif cell.text is not None and cell.text.content:
                yield cell.text.content


def discover_link_candidates(
    psd_path,
    *,
    email_override: Optional[str] = None,
    email_index: int = 0,
    policy: str = "hybrid",
):
    """Return (tree, regions) for the selected email. `regions` is the exact list `emit()` would
    write to regions.json for this PSD/policy -- the authoritative source for every slot/region
    id the pipeline recognizes. Raises ValueError on zero-solve (message matching both the CLI's
    emit and route commands) or an out-of-range email_index (matching the CLI's emit command --
    route has no --email-index flag and always uses the first email, so there is no route
    equivalent for that case)."""
    layout = psd_to_layout_tree(str(psd_path))
    layer_names = {l.id: l.name for l in layout.layers}
    trees = build_table_trees(layout, email_override=email_override)
    if not trees:
        raise ValueError(f"{psd_path}: solved to zero artboards/emails")
    if email_index < 0 or email_index >= len(trees):
        raise ValueError(
            f"--email-index {email_index} out of range; {psd_path} solved to {len(trees)} email(s)"
        )
    tree = trees[email_index]
    routed = route(tree, policy)
    composite = composite_psd(str(psd_path))
    with tempfile.TemporaryDirectory(prefix="psd_html_linkgen_") as scratch:
        result = emit(routed, scratch, composite=composite, layer_names=layer_names, psd_path=str(psd_path))
    return tree, result["regions"]


def find_manifest_near_psd(psd_path) -> tuple:
    """Classify what links.json manifest(s) sit next to `psd_path` -- pure filesystem
    classification, no resolution or user-facing text. Returns (canonical, others):
      - canonical: the exact "<psd-stem>.links.json" path IF it exists as a file, else None. This
        is the enforced naming convention (see grammar/PIPELINE.md and the authoring SOP) -- when
        it's present, that's authoritative and unambiguous no matter what else sits in the folder.
      - others: every OTHER "*.links.json" file in the same directory (sorted), populated only
        when `canonical` is None. This function never picks among them, not even when there is
        exactly one -- a manifest not named after its PSD is a real possibility (manifests are
        commonly named/located independently), but it is also exactly how two designers/PSDs can
        accidentally collide on one folder, so deciding whether to use it (and which one, if
        several) is a call for the caller to make explicitly (e.g. the GUI's confirm-or-pick
        prompt), never something this function guesses on the caller's behalf."""
    psd_path = Path(psd_path)
    canonical = psd_path.with_suffix("").with_suffix(".links.json")
    if canonical.is_file():
        return canonical, []
    others = sorted(psd_path.parent.glob("*.links.json"))
    return None, others


def categorize_regions(regions) -> tuple:
    """Split emit()'s `regions` list into (slot_candidates, region_candidates): every
    non-null `link_slot` (de-duped, first-seen order) vs. every slot-less image/graphic/cta
    region (a candidate for the "regions" section -- logos, icons, anything without a slot).
    Shared by `build_starter_manifest` (the JSON-scaffold path) and the GUI link editor (the
    form path) so the two never categorize a region differently."""
    seen_slots: dict = {}
    region_candidates = []
    for r in regions:
        slot = r.get("link_slot")
        if slot:
            if slot not in seen_slots:
                seen_slots[slot] = {"link_slot": slot, "role": r.get("role"), "alt": r.get("alt")}
        elif r.get("role") in ("image", "graphic", "cta"):
            # Whole-element candidates for elements WITHOUT a link_slot (per PIPELINE.md's own
            # "regions" use case: footer logos, social icons -- non-text things a producer may
            # still want clickable).
            region_candidates.append({"region_id": r["region_id"], "role": r.get("role"), "alt": r.get("alt")})
    return list(seen_slots.values()), region_candidates


def manifest_from_form(slot_values: dict, region_values: dict, inline_entries: list) -> dict:
    """Build the final links.json purely from user-entered values (the GUI link editor) -- no
    JSON authoring, no advisory scaffolding to strip out afterward. Any slot/region whose value
    is blank/whitespace-only is DROPPED, never written as an empty-string href -- html_emitter's
    `_href_for` only checks `region_id in regions`, so an empty string would still satisfy that
    check and bind as a real, broken link. Same for an inline entry missing either half."""
    slots = {k: v.strip() for k, v in slot_values.items() if v and v.strip()}
    regions = {k: v.strip() for k, v in region_values.items() if v and v.strip()}
    inline = [
        {"match": e["match"], "url": e["url"]}
        for e in inline_entries
        if e.get("match", "").strip() and e.get("url", "").strip()
    ]
    return {
        "_comment": "Generated by the PsdDropper link editor -- edit through that GUI, not by hand.",
        "slots": slots,
        "regions": regions,
        "inline": inline,
    }


def build_starter_manifest(psd_path, **kwargs) -> dict:
    """The full starter-manifest dict: real (empty) slots/regions/inline sections plus an
    advisory `_candidates` checklist built from the actual pipeline's own region discovery."""
    tree, regions = discover_link_candidates(psd_path, **kwargs)
    slot_candidates, region_candidates = categorize_regions(regions)

    seen_text: dict = {}
    for content in _iter_text_contents(tree.rows):
        seen_text.setdefault(content, None)  # de-dup, keep first-seen (document) order

    return {
        "_comment": (
            f"Starter link manifest for {Path(psd_path).name}, generated by `psd-html linkgen`. "
            '"slots"/"regions"/"inline" are the REAL manifest the pipeline reads -- copy entries '
            'in from "_candidates" below with a real URL, then delete "_candidates" (advisory '
            "only, ignored by the pipeline). See grammar/PIPELINE.md for the schema."
        ),
        "slots": {},
        "regions": {},
        "inline": [],
        "_candidates": {
            "slots": slot_candidates,
            "regions": region_candidates,
            "inline_text": list(seen_text.keys()),
        },
    }


def write_starter_manifest(psd_path, out_path=None, *, force: bool = False, **kwargs) -> Path:
    """Write the starter manifest next to `psd_path` (default: "<psd-stem>.links.json") or to
    `out_path`. Refuses to overwrite an existing file unless force=True -- a hand-authored
    manifest is exactly the kind of uncommitted work this must never silently clobber."""
    psd_path = Path(psd_path)
    out_path = Path(out_path) if out_path else psd_path.with_suffix("").with_suffix(".links.json")
    if out_path.is_file() and not force:
        raise FileExistsError(
            f"{out_path} already exists -- pass force=True (CLI: --force) to overwrite, "
            "or choose a different output path."
        )
    manifest = build_starter_manifest(psd_path, **kwargs)
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out_path
