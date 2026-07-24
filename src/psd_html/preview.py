"""A faithful, absolutely-positioned diagnostic view of a PSD.

This is NOT the shippable Outlook bundle. It exists to answer one question the OFT-safe table
emitter cannot answer at a glance -- "did we read the PSD's geometry (placement / scale /
alignment) correctly, and how does the classifier see each region?"

It renders, per artboard:
  - the real PSD composite (the ground-truth pixels) as an absolutely-positioned background image,
    at 1:1 canvas scale, and
  - one absolutely-positioned OUTLINE box per classified region, at its exact bbox, coloured by the
    5-role classifier's verdict (content-text / editable-field / cta / image / graphic / background)
    and labelled -- so a misread bbox shows as a box that does not hug its pixels, and a misread
    role shows as a wrongly-coloured box.

Absolute positioning is deliberately used here (and ONLY here): it is pixel-faithful in a browser,
which is exactly what a placement/scale check needs. It is the same approach the generic PSD->HTML
converters use -- and the same reason their output is dead in classic Outlook's Word engine, which
is why the SHIPPABLE path is tables, not this. Treat this page as a design proof, never as email
output. JS (an overlay toggle) is fine here precisely because this is not an email.
"""

from __future__ import annotations

import html
import os

from . import grid_analyzer as G
from .layer_classifier import (
    ROLE_BACKGROUND,
    ROLE_BUTTON,
    ROLE_CONTENT,
    ROLE_GRAPHIC,
    ROLE_HIGHLIGHT,
    classify_tree,
)
from .psd_adapter import psd_to_layout_tree


class PreviewError(RuntimeError):
    """Raised only for a hard, up-front failure (bad path / unreadable PSD). A degraded COMPOSITE
    (e.g. an odd layer psd-tools cannot flatten) never raises -- the page still renders the region
    boxes over a blank ground, with a loud banner, so the geometry is still inspectable."""


# Map a region's role to its kind string. A content text region splits by editability so a
# merge/fill-in field reads differently from plain live copy at a glance.
def _kind_of(item) -> str:
    if item.role == ROLE_GRAPHIC:
        return "graphic"
    if item.role == ROLE_BUTTON:
        return "cta"
    if item.role == ROLE_BACKGROUND:
        return "background"
    if item.role == ROLE_HIGHLIGHT:
        return "field"  # the backing rect that MARKS an editable field
    if item.role == ROLE_CONTENT:
        if item.is_text:
            return "editable" if item.editable else "text"
        return "image"
    return "other"


# Region kind -> (border colour, fill, label): the overlay style each kind gets.
_KIND_STYLE = {
    # kind:      (border-colour,   fill-rgba,                 label)
    "editable": ("#d81b8c", "rgba(216,27,140,0.14)", "editable field"),
    "field": ("#d81b8c", "rgba(216,27,140,0.10)", "field highlight"),
    "text": ("#0b8f8f", "rgba(11,143,143,0.10)", "live text"),
    "cta": ("#1a56db", "rgba(26,86,219,0.16)", "CTA / link"),
    "image": ("#2e9e3f", "rgba(46,158,63,0.10)", "image"),
    "graphic": ("#e07b00", "rgba(224,123,0,0.12)", "graphic (flattened)"),
    "background": ("#8a8a8a", "rgba(138,138,138,0.06)", "background"),
    "other": ("#8a8a8a", "rgba(138,138,138,0.08)", "other"),
}

# Plain-language, end-user meaning of each region kind -- what it means for the final Outlook
# template (shown as a hover tooltip on each legend item; mirrors README "What the regions mean").
_KIND_MEANING = {
    "editable": "A fill-in-the-blank / merge field (e.g. [First Name]). Stays editable text in Outlook -- personalized per recipient. Never baked into a picture.",
    "field": "The colored rectangle a designer draws behind a fill-in field to mark it. Becomes the field's background color.",
    "text": "Real headings / body copy. Ships as live, selectable text and reflows if the copy length changes.",
    "cta": "A button or clickable link. Stays a working link; the button grows with its label.",
    "image": "A photo / screenshot / icon / logo. Ships as a picture (PNG) -- looks exact, but is not editable text.",
    "graphic": "Overlapping decorative art the tool can't cleanly separate. Ships as one flattened picture -- anything inside is pixels, not editable text.",
    "background": "A section band or panel behind content. Becomes a cell/row background (decoration).",
    "other": "Unclassified region.",
}


def _composite_image(psd_path: str):
    """Best-effort full-canvas PIL composite of the PSD. Returns `(img, None)` on success or
    `(None, reason)` on any failure (loud-safe: the caller renders boxes over a blank ground and
    shows a banner naming the reason -- mirrors rasterizer.composite_psd threading its cause)."""
    try:
        from psd_tools import PSDImage

        psd = PSDImage.open(os.path.abspath(psd_path))
        img = psd.composite()
        if img is None:
            return None, "PSDImage.composite() returned None"
        return img.convert("RGBA"), None
    except Exception as exc:
        return None, repr(exc)


def _crop(img, region: dict):
    """Crop a full-canvas composite to an artboard region, clamped to the image bounds."""
    if img is None:
        return None
    w, h = img.size
    box = (
        max(0, min(int(region["left"]), w)),
        max(0, min(int(region["top"]), h)),
        max(0, min(int(region["right"]), w)),
        max(0, min(int(region["bottom"]), h)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return img.crop(box)


def _overlay_div(item, region: dict) -> str:
    kind = _kind_of(item)
    border, fill, _label = _KIND_STYLE.get(kind, _KIND_STYLE["other"])
    b = item.bbox
    left = int(b["left"]) - int(region["left"])
    top = int(b["top"]) - int(region["top"])
    width = max(0, int(b["right"]) - int(b["left"]))
    height = max(0, int(b["bottom"]) - int(b["top"]))
    name = html.escape(str(item.name or ""))
    label = html.escape(kind)
    style = (
        f"position:absolute;left:{left}px;top:{top}px;width:{width}px;height:{height}px;"
        f"border:2px solid {border};background:{fill};box-sizing:border-box;"
    )
    return (
        f'<div class="ov" data-kind="{kind}" title="{name} [{kind}] {width}x{height}" style="{style}">'
        f'<span class="lbl" style="background:{border};">{label}</span></div>'
    )


def _legend_html() -> str:
    items = []
    for kind, (border, _fill, label) in _KIND_STYLE.items():
        meaning = html.escape(_KIND_MEANING.get(kind, ""))
        items.append(
            f'<span class="leg" title="{meaning}"><span class="sw" style="background:{border};"></span>'
            f"{html.escape(label)}</span>"
        )
    return '<div class="legend">' + "".join(items) + "</div>"


_PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 13px/1.4 -apple-system, Segoe UI, Roboto, Arial, sans-serif; background: #f4f4f6; color: #1a1a1e; }
@media (prefers-color-scheme: dark) { body { background: #16161a; color: #e8e8ea; } .bar { background:#202028; } .ab-wrap { background:#202028; } }
.bar { position: sticky; top: 0; z-index: 50; background: #fff; border-bottom: 1px solid rgba(128,128,128,.3); padding: 10px 16px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.bar h1 { font-size: 14px; margin: 0; font-weight: 650; }
.bar .note { font-size: 12px; opacity: .7; }
.legend { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; }
.leg { display: inline-flex; align-items: center; gap: 5px; }
.sw { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
.toggle { font: inherit; padding: 5px 10px; border: 1px solid rgba(128,128,128,.4); border-radius: 6px; background: transparent; color: inherit; cursor: pointer; }
.leg { cursor: help; }
.rule { flex-basis: 100%; font-size: 12px; opacity: .85; padding-top: 6px; border-top: 1px dashed rgba(128,128,128,.25); }
.wrap { padding: 20px; display: flex; flex-direction: column; gap: 28px; align-items: flex-start; }
.ab-wrap { background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.15); }
.ab-title { font-size: 12px; opacity: .7; padding: 6px 0; }
.ab { position: relative; }
.ab img { display: block; }
.ov .lbl { position: absolute; left: 0; top: 0; font-size: 9px; line-height: 1; color: #fff; padding: 1px 3px; white-space: nowrap; border-radius: 0 0 3px 0; }
body.hide-ov .ov { display: none; }
"""

_TOGGLE_JS = """
document.getElementById('toggle').addEventListener('click', function () {
  document.body.classList.toggle('hide-ov');
  this.textContent = document.body.classList.contains('hide-ov') ? 'Show regions' : 'Hide regions';
});
"""


def render_preview(psd_path: str, out_dir: str) -> dict:
    """Render a PSD to a faithful, absolutely-positioned preview.html (+ per-artboard composite
    PNGs) under out_dir. Returns {preview_path, artboards, assets, composite_available}."""
    if not os.path.isfile(psd_path):
        raise PreviewError(f"PSD not found: {psd_path}")

    tree = psd_to_layout_tree(psd_path)
    by_id = {l.id: l for l in tree.layers}
    classified = classify_tree(tree, by_id)

    composite, composite_reason = _composite_image(psd_path)
    composite_available = composite is not None

    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    assets: list = []
    sections: list = []
    ab_ids = list(classified.keys())

    for idx, ab_id in enumerate(ab_ids):
        items = classified[ab_id]
        region = G._region_bbox_for(ab_id, by_id, tree.canvas)
        rw = max(1, int(region["right"]) - int(region["left"]))
        rh = max(1, int(region["bottom"]) - int(region["top"]))

        bg_html = ""
        crop = _crop(composite, region)
        if crop is not None:
            fname = f"preview_{idx}.png"
            crop.save(os.path.join(assets_dir, fname), format="PNG")
            rel = f"assets/{fname}"
            assets.append(rel)
            # use the real pixel size of the crop for the <img>, and size the container to match
            cw, ch = crop.size
            rw, rh = cw, ch
            bg_html = f'<img src="{rel}" width="{cw}" height="{ch}" alt="composite">'

        boxes = "".join(_overlay_div(it, region) for it in items)
        ab_name = by_id[ab_id].name if ab_id in by_id else (tree.psd or "canvas")
        title = html.escape(f"{ab_name}  ({rw}x{rh}px, {len(items)} regions)")
        sections.append(
            f'<div class="ab-title">{title}</div>'
            f'<div class="ab-wrap"><div class="ab" style="width:{rw}px;height:{rh}px;">{bg_html}{boxes}</div></div>'
        )

    banner = "" if composite_available else (
        '<div class="note" style="color:#c0392b;">composite unavailable for this PSD '
        f"({html.escape(composite_reason or 'unknown cause')}) -- region boxes "
        "shown over a blank ground (geometry is still inspectable)</div>"
    )

    page = (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>PSD preview - {html.escape(tree.psd)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        '<div class="bar">'
        f"<h1>{html.escape(tree.psd)}</h1>"
        '<span class="note">Faithful geometry PREVIEW (truth pixels + extracted regions) &mdash; NOT the Outlook output.</span>'
        f"{banner}"
        '<button id="toggle" class="toggle">Hide regions</button>'
        f"{_legend_html()}"
        '<div class="rule">Golden rule: <b>pink / teal / blue stays editable text</b> in Outlook; '
        "<b>green / orange is a fixed picture</b>. A fill-in field can never become a picture "
        "(hover a legend item for what each means).</div>"
        "</div>"
        f'<div class="wrap">{"".join(sections)}</div>'
        f"<script>{_TOGGLE_JS}</script>"
        "</body></html>"
    )

    preview_path = os.path.join(out_dir, "preview.html")
    with open(preview_path, "w", encoding="utf-8") as f:
        f.write(page)

    return {
        "preview_path": preview_path,
        "artboards": len(ab_ids),
        "assets": assets,
        "composite_available": composite_available,
    }
