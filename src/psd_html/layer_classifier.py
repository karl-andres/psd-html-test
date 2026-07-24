"""Assign each eligible leaf layer, per artboard, exactly one of 4 roles.

    graphic    : layer/group name's last whitespace token is "graphic" -> a flatten-to-one-image unit.
    button     : layer/group name's last whitespace token is "button"  -> a shape+label unit (bg + live label + link).
    background : non-text full-bleed band (>= 0.85 * artboard width) OR name's last token is "bg" OR a
                 non-text rect that sits behind text but FAILS the single-line highlight gate below
                 (too tall, or spans more than one text layer) -- a section/panel background.
    highlight  : non-text rect that backs text and becomes a cell/inline background. Two sub-kinds:
                 (a) a CONFERRING single-line chip -- covers exactly ONE text layer, its own height
                 <= ~1.8x that layer's estimated single-line height (one line + padding); it ALSO
                 marks that text layer as an EDITABLE FIELD (the merge-field design language).
                 (b) a NON-CONFERRING sole-tenant box -- a fill of ANY height that contains exactly
                 one text layer and nothing else (an unnamed CTA rect, a placeholder panel); supplies
                 geometry only and marks NOTHING editable. A rect spanning MULTIPLE text layers is a
                 section background instead (see `background`), never a highlight.
    content    : everything else -> a cell (live text, or a standalone image/icon).

Precedence when more than one test matches the same leaf: graphic > button > divider > background >
highlight > content. The divider gate (a hairline-thin, very wide rect backing no text) is
DELIBERATELY checked ahead of background/highlight and resolves to the content role -- so for that
one shape content outranks background, keeping a drawn rule from being swallowed as a background
band (owner-caught).

This module reuses grid_analyzer's artboard-scoped bucketing primitives (_artboard_of,
_region_bbox_for, _find_background, _clamp_bbox, _area, _is_band, _LEAF_KINDS, _union_bbox) -- the exact same
eligibility + background exclusion analyze_layout_tree() applies -- and lifts the proven
_pair_is_bg() overlap test from spike/run_v3.py's fill_is_bg() (renamed here since it now backs
two different roles, not just "highlight"). It never touches _find_valid_cut/_partition (the
guillotine cut logic lives only in grid_analyzer and table_solver).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import grid_analyzer as G
from .layout_tree import TextInfo

ROLE_GRAPHIC = "graphic"
ROLE_BUTTON = "button"
ROLE_BACKGROUND = "background"
ROLE_HIGHLIGHT = "highlight"
ROLE_CONTENT = "content"

ROLES = (ROLE_GRAPHIC, ROLE_BUTTON, ROLE_BACKGROUND, ROLE_HIGHLIGHT, ROLE_CONTENT)

_NAME_GRAPHIC = "graphic"
_NAME_BUTTON = "button"
_NAME_BG = "bg"

# fill/text overlap thresholds, lifted verbatim from spike/run_v3.py's fill_is_bg(): a text bbox is
# "on" a fill if it is >= tfrac covered BY the fill, OR the fill is >= ffrac covered BY the text
# bbox (the small merge-field-highlight-under-a-text-line pattern). This overlap test alone only
# tells us the fill SITS BEHIND the text -- it does not distinguish a tight single-line highlight
# from a large section panel that happens to contain a heading; see `_is_single_line_highlight()`.
_TFRAC = 0.6
_FFRAC = 0.5

# `_is_single_line_highlight()` thresholds (Defect 2 fix): a fill only marks its covered text
# EDITABLE if it tightly backs exactly ONE text layer, roughly one line + padding tall.
# `_HIGHLIGHT_MAX_LINE_MULTIPLE`: the fill's own height must be <= this many multiples of the
# covered text's estimated single-line height to still read as "one highlighted line" rather than
# a multi-line/section panel.
_HIGHLIGHT_MAX_LINE_MULTIPLE = 1.8
# `_LINE_HEIGHT_FONT_FACTOR`: when the covered text layer has a known font size (from its style
# runs), estimate its single-line height as size * this factor (standard single-line leading
# approximation) rather than trusting the text layer's own bbox height, which can be inflated by
# multi-line wrapping within that one layer.
_LINE_HEIGHT_FONT_FACTOR = 1.3


@dataclass
class ClassifiedItem:
    """One classified grid unit for one artboard: either a single leaf layer, or (for graphic/
    button) the union of every leaf under a `... graphic` / `... button` named group."""

    role: str
    name: str
    kind: str  # underlying leaf kind, or "group" for an aggregated graphic/button unit
    bbox: dict  # region-clamped {left, top, right, bottom}
    is_text: bool
    z: int
    layer_ids: list = field(default_factory=list)  # >1 only for aggregated graphic/button units
    editable: bool = False
    text: Optional[TextInfo] = None
    # For role=="content" text items only: the layer id of a highlight rect (if any) COVERING this
    # text -- set for ANY covering highlight regardless of edit-conferral (a non-conferring
    # sole-tenant box populates this even on editable=False text), letting the solver attach that
    # highlight as the cell background.
    covered_by_highlight_id: Optional[int] = None
    # ALL highlight layer ids covering this text (a footer legal block carries one small
    # highlight chip per merge token); the singular field above keeps its legacy tie-break.
    covered_by_highlight_ids: Optional[list] = None
    # For role=="button" groups only: editable (bracketed or highlight-marked) member layer ids
    # OTHER than the one kept live as the button's label -- these get rasterized into the button
    # image with no live copy left behind (S1.1 Defect 3 -- the "button swallows a field" shape).
    swallowed_editable_layer_ids: list = field(default_factory=list)
    # S1.2 Defect C: the text-layer ids that would be silently BAKED into a rasterized unit if this
    # item is emitted as-is, regardless of editable/bracketed/highlight status -- the invariant this
    # backs is "never rasterize live copy", not "never rasterize an editable field".
    #   - role=="content" AND is_text: this item's OWN layer id (a plain text leaf, if it ever ends
    #     up folded into a `flatten` cluster with something else, bakes itself).
    #   - role=="graphic": EVERY text member's layer id -- a graphic cell must be genuinely
    #     non-text decorative content only; there is no "one label" exception like a button gets.
    #   - role=="button": every text member's layer id EXCEPT the one kept live as the button's
    #     label -- a button may keep exactly one live text label; any other text member (editable
    #     or not, bracketed or not, or a highlight-marked sibling) is a second string of copy
    #     silently rasterized with it.
    #   - background/highlight: always empty (never a rasterized content unit).
    text_member_layer_ids: list = field(default_factory=list)


# --- naming helpers -----------------------------------------------------------------------------


def _name_has(name: Optional[str], token: str) -> bool:
    """Defect A fix: match `token` as a trailing NAME TOKEN, never a plain substring.

    Per the SOP naming convention the role keyword is always the layer/group name's LAST
    whitespace-delimited word -- "<name> graphic", "<name> button", "<name> bg" -- so this matches
    only when the lowercased name's last token equals `token` exactly. That single check covers
    both "ends with whitespace + token" (the common multi-word case) and the name being just the
    bare keyword on its own (single-token case).

    A plain substring test (the pre-fix behavior) wrongly matched "graphic" inside unrelated words
    that merely CONTAIN it with no separating whitespace, e.g. TYPE layers named "Get the
    infographic" or "...Infographic Display Banners Social Posts" -- both got misclassified
    ROLE_GRAPHIC and had their live text baked into a rasterized image. Tokenizing and comparing
    the last word whole rejects "infographic" (last token) against the keyword "graphic" (no exact
    equality) while still matching a genuine "Header Graphic" / "CTA Button" / "Footer bg" name.
    """
    tokens = str(name or "").lower().split()
    return bool(tokens) and tokens[-1] == token


def _nearest_named_ancestor(layer, by_id: dict, token: str, stop_at):
    """Walk layer, then its ancestors (parent, grandparent, ...), looking for the nearest one
    whose name's last whitespace token is `token`. Never walks past (or considers) the enclosing artboard itself
    (`stop_at`). Returns that Layer, or None."""
    seen: set = set()
    cur = layer
    while cur is not None:
        if cur.id == stop_at:
            return None
        if _name_has(cur.name, token):
            return cur
        parent_id = cur.parent
        if parent_id is None or parent_id in seen:
            return None
        seen.add(parent_id)
        cur = by_id.get(parent_id)
    return None


# --- fill_is_bg(), lifted from spike/run_v3.py -----------------------------------------------


def _rect_area(b: dict) -> int:
    return max(1, (b["right"] - b["left"]) * (b["bottom"] - b["top"]))


def _pair_is_bg(fill_bbox: dict, text_bbox: dict, tfrac: float = _TFRAC, ffrac: float = _FFRAC) -> bool:
    left = max(text_bbox["left"], fill_bbox["left"])
    right = min(text_bbox["right"], fill_bbox["right"])
    top = max(text_bbox["top"], fill_bbox["top"])
    bottom = min(text_bbox["bottom"], fill_bbox["bottom"])
    inter = max(0, right - left) * max(0, bottom - top)
    return inter >= tfrac * _rect_area(text_bbox) or inter >= ffrac * _rect_area(fill_bbox)


def _text_line_height(text_info: Optional[TextInfo], text_bbox: dict) -> float:
    """Best-effort single-line-height estimate for one text layer, used only to size-gate the
    highlight-vs-background-panel decision below.

    Prefers the largest font size across the layer's style runs (line-height is approximated as
    size * `_LINE_HEIGHT_FONT_FACTOR`, a standard single-line leading multiple) since that is
    robust to a text layer's bbox itself spanning multiple wrapped lines. Falls back to the text
    layer's own bbox height when no run carries a size (e.g. missing/degraded engine data) -- a
    reasonable proxy for genuinely single-line captions and merge fields.
    """
    sizes = [r.size for r in (text_info.runs if text_info is not None else []) if r.size]
    if sizes:
        return max(sizes) * _LINE_HEIGHT_FONT_FACTOR
    return max(1.0, text_bbox["bottom"] - text_bbox["top"])


def _is_single_line_highlight(
    fill_bbox: dict,
    text_entries: list,
    tfrac: float = _TFRAC,
    ffrac: float = _FFRAC,
    max_line_multiple: float = _HIGHLIGHT_MAX_LINE_MULTIPLE,
) -> tuple:
    """Decide whether a non-text rect is a tight single-line HIGHLIGHT (Defect 2 fix).

    `text_entries` is `[(bbox, TextInfo|None), ...]` for every text layer in the region. Returns
    `(is_highlight, covered_indices)`:
      - `covered_indices` -- every text entry this fill sits behind per `_pair_is_bg()` (the same
        overlap test used before this fix marked ALL of them "highlight" unconditionally).
      - `is_highlight` is True only when the fill covers EXACTLY ONE text entry AND its own height
        is <= `max_line_multiple` times that one text layer's estimated single-line height.

    When the fill covers text but `is_highlight` is False (spans multiple text layers, or is too
    tall to be one highlighted line), the caller either demotes it to ROLE_BACKGROUND (a
    section/panel background) or admits it as a NON-CONFERRING highlight via the sole-tenant box
    gate; either way it marks NOTHING editable.
    """
    covered = [i for i, (tb, _ti) in enumerate(text_entries) if _pair_is_bg(fill_bbox, tb, tfrac, ffrac)]
    if len(covered) != 1:
        return False, covered
    text_bbox, text_info = text_entries[covered[0]]
    line_height = _text_line_height(text_info, text_bbox)
    fill_height = fill_bbox["bottom"] - fill_bbox["top"]
    return fill_height <= max_line_multiple * line_height, covered


# --- per-artboard classification -----------------------------------------------------------------


def classify_artboard(tree, artboard_id, by_id: Optional[dict] = None) -> list:
    """Classify every eligible leaf layer of one artboard (or the whole canvas if artboard_id is
    None) into a role, returning a flat list[ClassifiedItem] sorted by paint order (z)."""
    if by_id is None:
        by_id = {l.id: l for l in tree.layers}

    region_bbox = G._region_bbox_for(artboard_id, by_id, tree.canvas)
    region_area = G._area(region_bbox)
    region_width = region_bbox["right"] - region_bbox["left"]

    layers_in_ab = [
        l
        for l in tree.layers
        if l.visible
        and l.kind in G._LEAF_KINDS
        and l.bbox is not None
        and l.bbox.area > 0
        and G._artboard_of(l, by_id) == artboard_id
    ]
    background = G._find_background(layers_in_ab, region_area)
    eligible = [l for l in layers_in_ab if background is None or l.id != background.id]

    clamped: list = []  # list[(Layer, bbox_dict)]
    for l in eligible:
        cb = G._clamp_bbox(l.bbox.to_dict(), region_bbox)
        if G._area(cb) <= 0:
            continue
        clamped.append((l, cb))

    text_entries = [(cb, l.text) for (l, cb) in clamped if l.kind == "type"]
    text_layer_ids = [l.id for (l, cb) in clamped if l.kind == "type"]

    # Pass 1: raw per-leaf role (independent of cross-referencing which text a highlight covers).
    raw: list = []
    for l, cb in clamped:
        is_text = l.kind == "type"
        text_info = l.text if is_text else None
        is_bracketed = is_text and "[" in str((text_info.content if text_info else "") or "")

        graphic_anchor = _nearest_named_ancestor(l, by_id, _NAME_GRAPHIC, artboard_id)
        button_anchor = _nearest_named_ancestor(l, by_id, _NAME_BUTTON, artboard_id) if graphic_anchor is None else None
        bg_anchor = (
            None if is_text or graphic_anchor is not None or button_anchor is not None
            else _nearest_named_ancestor(l, by_id, _NAME_BG, artboard_id)
        )
        is_band = (not is_text) and G._is_band({"kind": l.kind, "is_text": False, "bbox": cb}, region_width)
        is_single_line_chip, covered_text_idx = (
            (False, []) if is_text else _is_single_line_highlight(cb, text_entries)
        )
        # Only a tight single-line chip marks its text EDITABLE (the merge-field design
        # language). The sole-tenant BOX gate below contributes geometry only -- a brand CTA
        # rect containing its label must not turn the label into a merge field (bakeoff
        # editability proof caught 'Review the toolkit' drifting editable).
        is_sole_tenant_box = False
        if not is_text and not is_single_line_chip and len(covered_text_idx) == 1:
            # SOLE-TENANT BOX gate (owner-caught, reseller corpus): a fill that CONTAINS exactly
            # one text layer and nothing else is that text's placeholder BOX (the pink
            # "[Image] 182x105" panels, the partner-logo chip, an unnamed CTA rect) no matter how
            # tall it is -- the single-line gate above only admits one-line chips. In the observed
            # corpora every section panel contains multiple items; a sparse sub-band-width panel
            # backing a single heading would misclassify here as a highlight box.
            tb, _ti = text_entries[covered_text_idx[0]]
            _slop = 8
            contains_text = (
                cb["left"] <= tb["left"] + _slop and cb["top"] <= tb["top"] + _slop
                and cb["right"] >= tb["right"] - _slop and cb["bottom"] >= tb["bottom"] - _slop
            )
            if contains_text:
                tenants = 0
                for l2, cb2 in clamped:
                    if l2.id == l.id:
                        continue
                    cx = (cb2["left"] + cb2["right"]) / 2.0
                    cy = (cb2["top"] + cb2["bottom"]) / 2.0
                    if cb["left"] <= cx <= cb["right"] and cb["top"] <= cy <= cb["bottom"]:
                        tenants += 1
                if tenants == 1:  # just the covered text itself
                    is_sole_tenant_box = True
        is_highlight = is_single_line_chip or is_sole_tenant_box
        confers_edit = is_single_line_chip
        # DIVIDER gate: a hairline-thin, very wide rect backing no text is CONTENT (a rule/
        # divider the design draws, e.g. the footer's grey 590x5 'Line 1'), never a background
        # band -- band classification dropped it and the divider vanished (owner-caught).
        _w_cb = cb["right"] - cb["left"]
        _h_cb = cb["bottom"] - cb["top"]
        is_divider = (not is_text) and 0 < _h_cb <= 8 and _w_cb >= 25 * _h_cb and not covered_text_idx

        role = None
        group_anchor = None
        if graphic_anchor is not None:
            role, group_anchor = ROLE_GRAPHIC, graphic_anchor
        elif button_anchor is not None:
            role, group_anchor = ROLE_BUTTON, button_anchor
        elif is_divider:
            role = ROLE_CONTENT
        elif not is_text and (bg_anchor is not None or is_band):
            role = ROLE_BACKGROUND
        elif not is_text and is_highlight:
            role = ROLE_HIGHLIGHT
        elif not is_text and covered_text_idx:
            # Sits behind text but failed the single-line gate (too tall, or spans more than one
            # text layer) -- a section/panel background, NOT a merge-field highlight marker.
            role = ROLE_BACKGROUND
        else:
            role = ROLE_CONTENT

        raw.append(
            {
                "layer": l,
                "bbox": cb,
                "is_text": is_text,
                "role": role,
                "group_anchor": group_anchor,
                "is_bracketed": is_bracketed,
                "text_info": text_info,
                "confers_edit": confers_edit,
                "covered_text_ids": [text_layer_ids[i] for i in covered_text_idx],
            }
        )

    # Pass 2: for every highlight, find which text item(s) it sits behind. Editability flows
    # only from CONFERRING (single-line merge-chip) highlights; sole-tenant boxes carry
    # geometry without changing what is editable.
    highlight_covers: dict = {}  # highlight layer id -> [covered text layer id, ...]
    covered_text_ids: set = set()
    for item in raw:
        if item["role"] != ROLE_HIGHLIGHT:
            continue
        highlight_covers[item["layer"].id] = item["covered_text_ids"]
        if item["confers_edit"]:
            covered_text_ids.update(item["covered_text_ids"])

    def _covering_highlight_id(text_layer_id: int) -> Optional[int]:
        for hid, ids in highlight_covers.items():
            if text_layer_id in ids:
                return hid  # arbitrary tie-break if more than one highlight covers the same text
        return None

    def _covering_highlight_ids(text_layer_id: int) -> list:
        return [hid for hid, ids in highlight_covers.items() if text_layer_id in ids]

    # Pass 3: text editable = bracketed OR sits on a CONFERRING highlight (see Pass 2).
    for item in raw:
        if item["is_text"]:
            item["editable"] = item["is_bracketed"] or (item["layer"].id in covered_text_ids)
            item["covering_highlight_id"] = _covering_highlight_id(item["layer"].id)
            item["covering_highlight_ids"] = _covering_highlight_ids(item["layer"].id)
        else:
            item["editable"] = False
            item["covering_highlight_id"] = None
            item["covering_highlight_ids"] = []

    # Pass 4: aggregate graphic/button members that share the same named-group anchor into ONE
    # ClassifiedItem each (layer_ids = all members); everything else stays 1:1.
    groups: dict = {}
    singles: list = []
    for item in raw:
        if item["role"] in (ROLE_GRAPHIC, ROLE_BUTTON):
            key = (item["role"], item["group_anchor"].id)
            groups.setdefault(key, []).append(item)
        else:
            singles.append(item)

    out: list = []
    for item in singles:
        out.append(
            ClassifiedItem(
                role=item["role"],
                name=item["layer"].name,
                kind=item["layer"].kind,
                bbox=item["bbox"],
                is_text=item["is_text"],
                z=item["layer"].z,
                layer_ids=[item["layer"].id],
                editable=item["editable"],
                text=item["text_info"],
                covered_by_highlight_id=item["covering_highlight_id"],
                covered_by_highlight_ids=item["covering_highlight_ids"] or None,
                # A single text leaf bakes itself if it is ever folded into a `flatten` cluster
                # with something else (table_solver._cell_from_flatten) -- see field docstring.
                text_member_layer_ids=[item["layer"].id] if item["is_text"] else [],
            )
        )

    for (role, anchor_id), members in groups.items():
        anchor_layer = by_id.get(anchor_id)
        bbox = G._union_bbox([m["bbox"] for m in members])
        layer_ids = [m["layer"].id for m in members]
        # Faithfully surface if any aggregated member was itself editable -- this is exactly the
        # safety-invariant violation shape (an editable field trapped in a graphic/button unit).
        # Enforcement (fail-loud rejection) is the intake validator's job, not this classifier's;
        # this module only reports what it sees.
        any_editable = any(m["editable"] for m in members)
        label_text = None
        swallowed_editable_ids: list = []
        text_member_ids: list = []
        if role == ROLE_BUTTON:
            label_member = next((m for m in members if m["is_text"]), None)
            if label_member is not None:
                label_text = label_member["text_info"]
            label_id = label_member["layer"].id if label_member is not None else None
            # Every OTHER editable member (bracketed or highlight-marked) gets baked into the
            # button image with no live copy left behind -- the label is the only survivor.
            swallowed_editable_ids = [
                m["layer"].id for m in members if m["editable"] and m["layer"].id != label_id
            ]
            # S1.2 Defect C: a button may keep exactly ONE live text label -- every OTHER text
            # member (editable or not) is baked-in copy with no live copy surviving.
            text_member_ids = [
                m["layer"].id for m in members if m["is_text"] and m["layer"].id != label_id
            ]
        else:
            # role == ROLE_GRAPHIC: a graphic cell has no "one label" exception -- EVERY text
            # member is baked-in copy (S1.2 Defect C).
            text_member_ids = [m["layer"].id for m in members if m["is_text"]]
        out.append(
            ClassifiedItem(
                role=role,
                name=anchor_layer.name if anchor_layer is not None else f"group-{anchor_id}",
                kind="group",
                bbox=bbox,
                is_text=False,
                z=max(m["layer"].z for m in members),
                layer_ids=layer_ids,
                editable=any_editable,
                text=label_text,
                covered_by_highlight_id=None,
                swallowed_editable_layer_ids=swallowed_editable_ids,
                text_member_layer_ids=text_member_ids,
            )
        )

    out.sort(key=lambda ci: ci.z)
    return out


def classify_tree(tree, by_id: Optional[dict] = None) -> dict:
    """Classify every artboard in a LayoutTree. Returns {artboard_id: [ClassifiedItem, ...]}."""
    if by_id is None:
        by_id = {l.id: l for l in tree.layers}
    artboard_ids = list(tree.artboards) if tree.artboards else [None]
    return {ab_id: classify_artboard(tree, ab_id, by_id) for ab_id in artboard_ids}
