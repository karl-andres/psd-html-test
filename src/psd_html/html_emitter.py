"""A RoutedTree -> an OFT-safe HTML bundle on disk.

Consumes a `layer_router.RoutedTree` (a solved `table_tree.TableTree` plus a per-leaf render verb)
together with two optional LATE-BINDING manifests -- `copy_manifest: {source_layer_id -> final str}`
and `link_manifest: {link_slot -> href}` -- and writes a bundle folder:

    <out_dir>/index.html            -- UTF-8, nested fixed-px <table>s, inline styles only
    <out_dir>/assets/*.png          -- every raster region + any cropped Background image
    <out_dir>/regions.json          -- one record per emitted LEAF region (feeds the visual gate)
    <out_dir>/_bundle_manifest.json -- {schema_version, entry_html, assets, bundle_hash} -- same
                                        field names as the downstream consumer
                                        (`Reference/Creative QA System/Service/bundle_intake.py`)
    <out_dir>/links_report.json     -- {bound, unbound} -- every hyperlink written vs every
                                        manifest promise, so an unbound promise fails link-verify

THE OFT-SAFE BUNDLE GRAMMAR (enforced by construction here, not just documented):
  - Rasters are referenced ONLY via `<img src="...">` or an inline `style="...url(...)"`.
  - NEVER a `<style>` block, `<link>`, `@import`, `srcset`, a `background=` HTML attribute, VML, or
    a shipped `.svg` (SVG only ever exists as an intermediate fed to resvg upstream in
    `rasterizer.py` -- this module only ever writes the resulting PNG paths).
  - Every asset path is bundle-root-relative, forward-slash style.
  - The classic-Outlook Word-engine DPI-lock `<xml>` block (`DPI_LOCK_BLOCK`) is always present.

THE EDITABILITY HARD RULE is enforced one layer down (`layer_router.route`/`EditabilityViolation`)
-- by the time a `RoutedTree` reaches this module a PROTECTED cell (merge/cta/body) can only ever
carry `verb == "live"`. This module does not re-derive that decision; it renders whatever
`layer_router.render_role()` + the routed verb say, using the SAME classification function so the
two modules can never disagree about what a cell is.

LATE COPY BINDING: a leaf's rendered copy/label is `copy_manifest[cell.source_layer_id]` (falling
back to `cell.image_source_layer_ids` for button cells, which carry no `source_layer_id`) if
present, else the PSD SAMPLE copy (`cell.text.content`) -- never silently blank. A brand-headline
raster leaf is re-typeset from that same final copy via `rasterizer.rasterize_brand_headline`
(never pixel-cropped). `link_slot` resolves through `link_manifest` the same way; when it resolves,
the rendered element (an `<img>` or the live text run) is wrapped in exactly one `<a href>` -- never
an image map, and never more than one clickable element per region.

COPY-OVERFLOW GUARD (EARS-209): a LIVE text region's `<td>` width is fixed (mirrors the PSD grid
column) but its height always reflows -- so the only way copy can genuinely fail to fit is an
unbreakable run (a single "word", no space to wrap on) wider than the region at its RENDERED
font size. `_has_unbreakable_overflow` measures exactly that (real glyph advances via
measure_text_px when the font file exists, the average-glyph-width heuristic only as fallback)
and, if it trips, this module NEVER clips or truncates the
copy -- it still renders the full string and additionally appends a flag to `overflow_flags` /
`regions.json` for human preview/override. A raster brand-headline's own overflow (computed by
`text_raster_adapter.build_headline_svg` and surfaced on `RasterResult.overflow`) is folded in the
same way.

Loud-but-safe degrade: every call into `rasterizer.rasterize_cell` is guarded. If the rasterizer
raises (`RasterizerUnavailable`, or a `ValueError` from a degenerate rect / missing composite) this
module does not abort the whole bundle -- it records a structured entry in the returned
`warnings` list and renders a plain-text placeholder `<td>` carrying the region's `alt` text
instead of an `<img>`, so one bad region never blocks the rest of the bake-off.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .font_resolver import measure_text_px, normalize_font_name, resolve
from .layer_router import (
    ROLE_BODY,
    ROLE_BRAND_HEADLINE,
    ROLE_CTA,
    ROLE_GRAPHIC,
    ROLE_IMAGE,
    ROLE_MERGE,
    RoutedTree,
    render_role,
)
from .layout_tree import BBox, TextInfo, TextRun
from .rasterizer import RasterizerUnavailable, composite_layer, open_layer_index, rasterize_cell
from .table_tree import Cell, Row, TableTree
from .text_raster_adapter import AVG_CHAR_WIDTH_RATIO, DEFAULT_FONT_SIZE, LINE_HEIGHT_RATIO, dominant_run

# --- constants ----------------------------------------------------------------------------------

# The classic-Outlook (Word engine) DPI-lock block -- must be present verbatim in every emitted
# index.html so the Word rendering engine does not silently rescale px measurements.
DPI_LOCK_BLOCK = (
    "<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96"
    "</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->"
)

DEFAULT_TD_PADDING = 8
DEFAULT_BUTTON_PADDING_V = 12
DEFAULT_BUTTON_PADDING_H = 24
DEFAULT_BUTTON_BG = "#0066cc"
DEFAULT_BUTTON_TEXT_COLOR = "#ffffff"

# The minimum size we would ever shrink live text to before calling it illegible -- the
# copy-overflow guard's "does this fit at ALL" floor (see module docstring EARS-209).
MIN_LEGIBLE_FONT_SIZE = 10.0
# Word's live-text glyph advances render measurably wider than PIL's measurement of the same
# font file (observed 2026-07-09 on a real classic-Outlook capture: systematic 1-2 char
# right-edge clipping on cells sized to PIL-measured ink). Every live-text fit decision
# (shrink-to-fit, button label fit, unbreakable-overflow) budgets this factor so Word gets
# breathing room; raster paths don't need it (resvg shares PIL's metrics).
WORD_METRIC_SAFETY = 1.06


class HtmlEmitterError(RuntimeError):
    """Loud failure for an unrecoverable emit()-time problem (an unrecognized leaf shape, a
    non-RoutedTree argument, ...). Never raised for a degraded/missing external tool -- that is
    the `warnings` loud-but-safe path instead."""


# --- emit context (mutable accumulator threaded through the recursive walk) ---------------------


@dataclass
class _EmitContext:
    out_root: Path
    assets_dir: Path
    assets_subdir: str
    copy_manifest: Optional[Mapping]
    link_manifest: Optional[Mapping]
    composite: Any
    layer_names: Optional[Mapping]
    registry: Optional[Mapping]
    layer_index: Optional[Mapping] = None
    density: float = 1.0
    assets: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    overflow_flags: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # region_id -> harmonized shrink size (see _harmonize_shrink): sibling cells sharing a design
    # type size must shrink by ONE factor, not each by their own.
    shrink_overrides: dict = field(default_factory=dict)
    # region_id -> harmonized line-height px for SINGLE-LINE sibling groups: with one lh and one
    # size, every member's baseline sits at the same offset from its design top, so the columns
    # align exactly as the design's own ink does (mixed 21/42 lh staggered baselines 8px).
    lh_overrides: dict = field(default_factory=dict)
    # Every hyperlink actually written into the HTML ({kind, region, url[, match]}) -- emit()
    # reconciles this against the manifest so an unbound URL is a LOUD failure, never a silently
    # dead link discovered after send.
    links_bound: list = field(default_factory=list)
    # BAND PAINT (probe-certified 2026-07-14): Word paints a cell's shading only over its line
    # boxes + padding -- everything else in the row box is WHITE, even with td bgcolor. While a
    # colored band's row/stack is being emitted these carry the band color + the row's design
    # height so every spacer emits the certified gap construct (font-size:1px;
    # line-height:{H}px; mso-line-height-rule:exactly; &nbsp;) instead of a 0-tall line box
    # Word stripes white.
    current_band_color: Optional[str] = None
    current_band_row_h: int = 0
    # S1.1 degrade dedup: font names we've already warned about for skipped line-count
    # certification (font file not installed on this machine) -- so the plain-language warning
    # fires once per font, not once per region.
    cert_skip_fonts: set = field(default_factory=set)


# --- small pure helpers ---------------------------------------------------------------------------


def _assets_relpath(subdir: str, filename: str) -> str:
    sub = (subdir or "").strip("/")
    return f"{sub}/{filename}" if sub else filename


def _stable_bg_name(*parts) -> str:
    key = "|".join(str(p) for p in parts)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"bg_{digest}.png"


def _final_copy_for(cell: Cell, copy_manifest: Optional[Mapping]) -> str:
    """The late-bound, authoritative string for `cell`: `copy_manifest[cell.source_layer_id]` if
    present, else the first `copy_manifest` hit among `cell.image_source_layer_ids` (button cells
    carry no `source_layer_id` -- see table_solver._cell_from_item), else the PSD SAMPLE copy."""
    manifest = copy_manifest or {}
    if cell.source_layer_id is not None and cell.source_layer_id in manifest:
        return manifest[cell.source_layer_id]
    for lid in cell.image_source_layer_ids or ():
        if lid in manifest:
            return manifest[lid]
    return cell.text.content if cell.text is not None else ""


def _link_sections(link_manifest: Optional[Mapping]) -> tuple:
    """links.json schema: {"slots": {link_slot: url}, "regions": {region_id: url},
    "inline": [{"match": <exact visible text>, "url": url}]}. A flat {slot: url} mapping (the
    original shape) still reads as slots -- nothing existing breaks. A PSD carries no hyperlink
    data, so this manifest is the single deterministic source of every link in the email."""
    m = link_manifest or {}
    if any(k in m for k in ("slots", "regions", "inline")):
        return (m.get("slots") or {}), (m.get("regions") or {}), (m.get("inline") or [])
    return m, {}, []


_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
_URL_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")


def _is_safe_href(url: Optional[str]) -> bool:
    """True iff `url` is safe to emit as an href: an allowlisted absolute scheme (http/https/
    mailto/tel) OR a scheme-less relative/anchor link. Blocks javascript:/data:/vbscript:/file:
    etc. -- inert under Outlook's Word engine but LIVE in the Chromium fidelity preview, and not
    something an email should ship regardless. Enforced at both binding chokepoints (_href_for and
    _bind_inline_links) so an unsafe href is NEVER emitted; the blocked URL then surfaces as an
    unbound manifest promise (loud failure in reconciliation), never a silent drop."""
    u = (url or "").strip()
    if not u:
        return False
    m = _URL_SCHEME_RE.match(u)
    if not m:
        return True  # no scheme -> relative / anchor / fragment, safe
    return m.group(1).lower() in _ALLOWED_URL_SCHEMES


def _href_for(cell: Cell, link_manifest: Optional[Mapping], region_id: Optional[str] = None) -> Optional[str]:
    slots, regions, _ = _link_sections(link_manifest)
    url = None
    if cell.link_slot is not None and cell.link_slot in slots:
        url = slots[cell.link_slot]
    elif region_id is not None and region_id in regions:
        url = regions[region_id]
    if url is not None and not _is_safe_href(url):
        return None  # unsafe scheme -> never emit; surfaces as unbound in reconciliation
    return url


def _alt_fallback(cell: Cell) -> str:
    if cell.text is not None and cell.text.content and cell.text.content.strip():
        return cell.text.content.strip()
    if cell.source_layer_id is not None:
        return f"layer #{cell.source_layer_id}"
    return f"{cell.role} region"


def _has_unbreakable_overflow(
    copy: Optional[str],
    width_px: int,
    *,
    font_name: Optional[str] = None,
    font_size: float = MIN_LEGIBLE_FONT_SIZE,
    registry: Optional[Mapping] = None,
    avg_char_width_ratio: float = AVG_CHAR_WIDTH_RATIO,
) -> bool:
    """True iff some whitespace-delimited run in `copy` is wider than the region at its RENDERED
    font size. A live text `<td>` is fixed-width but reflows in height, so this is the only genuine
    "can never fit" shape (see module docstring EARS-209) -- ordinary wrapping is not overflow.

    Measurement: real glyph advances via `font_resolver.measure_text_px` when the resolved font's
    file exists on this machine (deterministic, kerning-included -- kills the 0.55-guess false
    negatives/positives), else the documented avg-char-width heuristic. Text cells carry zero
    horizontal padding (see `_text_style`), so the full cell width is usable."""
    if not copy:
        return False

    usable = max(1, int(width_px))
    tokens = copy.split() or [copy]
    probe = measure_text_px("Mx", font_name, font_size, registry=registry)
    if probe is not None:
        for tok in tokens:
            w = measure_text_px(tok, font_name, font_size, registry=registry)
            if w is not None and w * WORD_METRIC_SAFETY > usable:
                return True
        return False
    avg_char_width = max(1.0, font_size * avg_char_width_ratio)
    max_chars = max(1, int(usable / avg_char_width))
    return any(len(tok) > max_chars for tok in tokens)


def _word_budgeted_line_count(copy: str, width_px: int, *, font_name, font_size, registry,
                              safety: float = WORD_METRIC_SAFETY) -> Optional[int]:
    """How many lines this copy needs when wrapped at real glyph metrics x `safety` (default:
    the Word budget; pass safety=1.0 for the Chromium-side count). Returns None when no font
    file is available -- the heuristic fallback is too coarse to certify line counts against,
    and a false rejection would block a good bundle."""
    if not copy:
        return None

    if measure_text_px("Mx", font_name, font_size, registry=registry) is None:
        return None
    usable = max(1, int(width_px))

    def _fits(candidate: str) -> bool:
        w = measure_text_px(candidate, font_name, font_size, registry=registry)
        # +2px slack: design boxes are ink-EXACT, so a border-line line sits ON the wrap
        # boundary and sub-pixel measurement jitter flips the count (line 2 of the sign-off
        # measured 530.1 in a 530 box -- live-caught). The Word budget's 6% margin dwarfs 2px.
        return (w if w is not None else float("inf")) * safety <= usable + 2.0

    from .text_raster_adapter import _wrap_text

    return len(_wrap_text(copy, _fits))


def _row_bbox(row: Row) -> Optional[BBox]:
    # The DESIGN span: the solver's band-expanded row.rect when present (a CTA label on a taller
    # button shape spans the shape, not just the label's ink), else the union of cell rects.
    if getattr(row, "rect", None) is not None:
        return row.rect
    rects = [c.rect for c in row.cells if c.rect is not None]
    if not rects:
        return None
    return BBox(
        left=min(r.left for r in rects),
        top=min(r.top for r in rects),
        right=max(r.right for r in rects),
        bottom=max(r.bottom for r in rects),
    )


def _flat_color_of(img) -> Optional[str]:
    """`#RRGGBB` when `img` is a (near-)uniform fill -- opaque pixels varying less than a hair --
    else None. Transparent padding (a band composited into a larger viewport) is ignored."""
    try:
        import numpy as np

        arr = np.asarray(img.convert("RGBA"), dtype="float32")
        opaque = arr[arr[..., 3] > 10]
        if opaque.size == 0:
            return None
        rgb = opaque[:, :3]
        if float(rgb.std(axis=0).max()) > 2.5:
            return None
        mean = rgb.mean(axis=0)
        return "#%02X%02X%02X" % tuple(int(round(c)) for c in mean)
    except Exception:
        return None


def _opaque_coverage_of(img):
    """`(fraction, None)` where fraction in [0..1] is `img`'s opaque-pixel (alpha > 10) share, or
    `(None, reason)` on failure. A background crop's coverage says whether the layer genuinely fills
    its rect or merely sits inside it (button shapes, icons) -- see the COVERAGE GUARD in
    `_crop_background_image`. The reason is surfaced so a real numpy/PIL error skips the guard
    LOUDLY instead of silently letting a partial-coverage fill through."""
    try:
        import numpy as np

        arr = np.asarray(img.convert("RGBA"), dtype="float32")
        total = arr.shape[0] * arr.shape[1]
        if total == 0:
            return None, "empty crop (zero pixels)"
        return float((arr[..., 3] > 10).sum()) / float(total), None
    except Exception as exc:
        return None, repr(exc)


def _mean_color_of(img) -> Optional[str]:
    """`#RRGGBB` mean of the opaque pixels regardless of texture, else None. Used as the FLAT
    FALLBACK color under a textured background image: classic Outlook's Word engine does not
    paint CSS `background-image` at all (probe-verified 2026-07-09 on a live classic-Outlook
    box, alongside legacy `background=` -- both blank), so every textured band ships a bgcolor
    approximation that Word CAN paint while Chromium-class clients layer the real texture on top."""
    try:
        import numpy as np

        arr = np.asarray(img.convert("RGBA"), dtype="float32")
        opaque = arr[arr[..., 3] > 10]
        if opaque.size == 0:
            return None
        mean = opaque[:, :3].mean(axis=0)
        return "#%02X%02X%02X" % tuple(int(round(c)) for c in mean)
    except Exception:
        return None


def _crop_background_image(rect: Optional[BBox], layer_id: int, ctx: _EmitContext, *, prefix: str) -> Optional[str]:
    """Render a Background's `image_source_layer_id` to a PNG under the bundle, in the absolute
    canvas coordinate space `rect` already lives in. Loud-but-safe: any failure records a
    `ctx.warnings` entry and returns None -- backgrounds are decorative, never worth aborting over.

    PREFERRED path (when `ctx.layer_index` is available, i.e. emit() was given `psd_path`): composite
    ONLY that background layer within the rect (`rasterizer.composite_layer`). This is the fix for
    the "doubled text" defect -- a band/highlight cropped out of the FLATTENED whole-PSD composite
    baked in the foreground text sitting on top of it, so the text then rendered twice (once in the
    background image, once as the live `<td>` copy). Compositing the single source layer yields just
    the band/highlight pixels, no foreground content.

    FALLBACK path (no `psd_path` given -- e.g. a direct emit() unit test): the legacy crop of
    `ctx.composite`. This can include foreground pixels, so it is flagged with a
    `background_image_may_include_foreground` warning; the real pipeline (cli/bakeoff) always passes
    `psd_path` and never hits this path."""
    if rect is None:
        ctx.warnings.append({"type": "background_image_unavailable", "layer_id": layer_id, "reason": "no rect to crop"})
        return None

    box = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        ctx.warnings.append({"type": "background_image_degenerate_rect", "layer_id": layer_id, "rect": rect.to_dict()})
        return None

    # The rect is in CSS space (the tree is pre-scaled by density); the source PSD/layer is full
    # resolution, so sample at the PSD-space viewport (box * density) and let the CSS-sized cell
    # scale it via background-size:cover -> a retina-crisp background. Identity at density 1.0.
    src_box = tuple(round(v * ctx.density) for v in box)

    img = None
    layer = ctx.layer_index.get(layer_id) if ctx.layer_index else None
    if layer is not None:
        # Preferred: only this layer's own pixels, clipped to the rect -- no baked foreground text.
        img, _comp_reason = composite_layer(layer, src_box)
        if img is None:
            ctx.warnings.append({"type": "background_image_unavailable", "layer_id": layer_id,
                                 "reason": f"single-layer composite failed: {_comp_reason}"})
            return None
    else:
        composite = ctx.composite
        if composite is None or isinstance(composite, dict):
            reason = composite.get("reason", "composite unavailable") if isinstance(composite, dict) else "no PSD composite or layer index supplied to emit()"
            ctx.warnings.append({"type": "background_image_unavailable", "layer_id": layer_id, "reason": reason})
            return None
        try:
            clamped = (max(0, src_box[0]), max(0, src_box[1]), min(int(composite.width), src_box[2]), min(int(composite.height), src_box[3]))
            if clamped[2] <= clamped[0] or clamped[3] <= clamped[1]:
                ctx.warnings.append({"type": "background_image_degenerate_rect", "layer_id": layer_id, "rect": rect.to_dict()})
                return None
            img = composite.crop(clamped)
            box = clamped
            ctx.warnings.append({
                "type": "background_image_may_include_foreground",
                "layer_id": layer_id,
                "reason": "no psd_path given to emit(); background cropped from the flattened composite and may include foreground pixels -- pass psd_path for a clean single-layer background",
            })
        except Exception as exc:  # pragma: no cover -- defensive: never let a bg crop kill the bundle
            ctx.warnings.append({"type": "background_image_crop_failed", "layer_id": layer_id, "reason": repr(exc)})
            return None

    # COVERAGE GUARD: a background layer that only partially covers its rect (e.g. a button shape
    # whose band expanded to the full row) must not paint the WHOLE rect its color -- that invents
    # fill where the design shows none (live-caught 2026-07-09: the CTA row rendered as a
    # full-width blue band). The partially-covering element paints itself through its own cell.
    coverage, _cov_reason = _opaque_coverage_of(img)
    if coverage is None:
        ctx.warnings.append({"type": "coverage_guard_skipped", "prefix": prefix, "layer_id": layer_id,
                             "reason": f"could not compute opaque coverage: {_cov_reason}"})
    if coverage is not None and coverage < 0.6:
        ctx.warnings.append({
            "type": "partial_coverage_background_skipped",
            "prefix": prefix,
            "layer_id": layer_id,
            "coverage": round(coverage, 3),
            "reason": "background layer covers under 60% of its rect; painting the full rect its "
                      "color would invent fill the design does not show -- skipped",
        })
        return None

    # A FLAT fill emits as a color, not pixels: email canon puts solid section fills in a bgcolor
    # attribute + background-color prop (renders everywhere, weighs nothing); an image crop of a
    # flat fill is pure bloat and can shift hue through PNG/screen. Only genuinely textured
    # backgrounds ship as images.
    flat = _flat_color_of(img)
    if flat is not None:
        return flat

    # Textured fill: `background-image` is OUTSIDE Grammar G (probe-verified 2026-07-09: classic
    # Outlook's Word engine paints neither CSS background-image nor legacy `background=`; VML
    # image fill is uncertified). Emitting it would make the browser preview show texture the
    # recipient never sees. Ship the flat mean-color approximation (renders identically in every
    # engine) and flag the loss loudly -- the designer's tier choice is to accept the flat fill
    # or re-author the band as a real image region.
    mean_hex = _mean_color_of(img)
    ctx.warnings.append({
        "type": "textured_background_flattened_to_color",
        "prefix": prefix,
        "layer_id": layer_id,
        "flat_color": mean_hex,
        "reason": "background-image is outside Grammar G (classic Outlook cannot paint it); "
                  "band ships as its mean color -- accept the flat fill or re-author the band "
                  "as an image region for full texture",
    })
    if mean_hex is None:
        return None
    return mean_hex


def _background_style_props(bg, rect: Optional[BBox], ctx: _EmitContext, prefix: str) -> tuple:
    """Return `(bgcolor_attr_fragment, style_props_str)` for a `table_tree.Background` -- a solid
    color becomes a `bgcolor` attribute (per the build spec) plus a matching `background-color`
    style prop; an image background becomes an inline `style="...url(...)"` fragment (never the
    legacy `background=` HTML attribute, never VML)."""
    if bg is None:
        return "", ""
    bgcolor_attr = f' bgcolor="{html.escape(bg.color, quote=True)}"' if bg.color else ""
    props = f"background-color:{bg.color};" if bg.color else ""
    if bg.image_source_layer_id is not None:
        fill = _crop_background_image(rect, bg.image_source_layer_id, ctx, prefix=prefix)
        if fill is not None:
            # _crop_background_image returns the hex fill directly (flat fills as-is, textured
            # fills flattened to their mean color; Grammar G excludes background-image, and the
            # crop function records the texture-loss warning).
            bgcolor_attr = f' bgcolor="{fill}"'
            props += f"background-color:{fill};"
    return bgcolor_attr, props


def _est_design_lines(rect_height: int, size: float, leading: Optional[float],
                      space_after_total: float = 0.0) -> int:
    """How many lines the DESIGN box holds. Photoshop ink-tight boxes obey
    leading*(lines-1) + size + sum(inter-paragraph SpaceAfter) (verified on the corpus: the
    53px sign-off box = 24 + 8.5 + 20 exactly), so with real leading the estimate is exact;
    without it, fall back to the nominal-ratio guess."""
    if leading and leading > 0 and size:
        return max(1, int(round((rect_height - size - space_after_total) / leading)) + 1)
    return max(1, round(rect_height / max(1.0, size * LINE_HEIGHT_RATIO)))


def _design_line_height(rect_height: int, size: float, rendered_lines: Optional[int] = None,
                        leading: Optional[float] = None) -> int:
    """RHYTHM-EXACT line-height: derive the px line-height from the DESIGN box itself so the
    rendered text block is exactly as tall as the box it came from (`mso-line-height-rule:exactly`
    makes Word honor it too). A nominal 1.25em leading renders every single-line ink box ~25%
    taller than designed, and those extra pixels accumulate down the stack as the whole email
    drifting/bloating. The box height is split evenly across the line count; clamped to sane
    bounds so a padded/outlier box falls back to nominal instead of overlapping glyphs.

    `rendered_lines` (when the caller measured the copy's ACTUAL wrap with real glyph metrics)
    outranks the nominal-leading estimate: CSS glyphs run slightly narrower than Photoshop's
    layout of the same face, so a paragraph the design wraps to 4 lines can render as 3 -- ink
    then ends a full line-height above the box bottom and the band scan reads a blank stripe
    (live-caught 2026-07-09). Splitting the box across the lines that will actually render
    keeps the ink spanning the full design box either way."""
    if leading and leading > 0:
        return max(1, int(round(leading)))  # the design's own leading IS the rhythm
    nominal = max(1, int(round(size * LINE_HEIGHT_RATIO)))
    if rect_height <= 0:
        return nominal
    lines = rendered_lines if (rendered_lines and rendered_lines > 0) \
        else max(1, round(rect_height / max(1.0, size * LINE_HEIGHT_RATIO)))
    derived = int(round(rect_height / lines))
    lo, hi = int(round(size * 0.85)), int(round(size * 1.8))
    if derived < lo or derived > hi:
        return nominal
    return derived


def _text_style(cell: Cell, ctx: _EmitContext, size_override: Optional[float] = None,
                rendered_lines: Optional[int] = None, line_height_px: Optional[int] = None) -> str:
    run = dominant_run(cell.text)
    font_name = run.font if run is not None else None
    resolution = resolve(font_name, registry=ctx.registry)
    size = float(size_override) if size_override else (float(run.size) if (run is not None and run.size) else DEFAULT_FONT_SIZE)
    color = (run.color if (run is not None and run.color) else None) or "#000000"
    align = (cell.text.align if (cell.text is not None and cell.text.align) else None) or "left"
    _lead = run.leading if (run is not None and getattr(run, "leading", None)) else None
    line_height = line_height_px if line_height_px else _design_line_height(
        int(cell.rect.height), size, rendered_lines, leading=_lead)
    # padding:0 -- a PSD text bbox is INK-TIGHT (the glyphs fill it exactly), so any interior
    # padding shrinks the usable width below what the design's own glyphs need and guarantees a
    # spurious wrap. Inter-element spacing is owned by the tiling spacers (real PSD gaps), never
    # by padding inside a text cell.
    _runs_all = cell.text.runs if (cell.text is not None and cell.text.runs) else []
    underline_prop = "text-decoration:underline;" if (
        _runs_all and all(getattr(r, "underline", False) for r in _runs_all)) else ""
    return (
        "padding:0;"
        f"font-family:{resolution.css_stack};{resolution.weight_css}font-size:{size:g}px;color:{color};"
        f"text-align:{align};line-height:{line_height:g}px;mso-line-height-rule:exactly;{underline_prop}"
    )


def _wrap_td(
    cell: Cell,
    inner_html: str,
    ctx: _EmitContext,
    *,
    colspan: int = 1,
    extra_style: str = "",
    min_height: bool = False,
    region_id: Optional[str] = None,
    top_offset: int = 0,
) -> str:
    """Wrap `inner_html` in this leaf's `<td>`, carrying its EXACT PSD rect as both a `width`/
    `height` attribute and matching inline style props (TILING BEHAVIOR item 4) so a leaf cell
    never collapses. `min_height=True` (live text/CTA leaves, which may genuinely reflow taller
    than the PSD rect once real copy lands) emits `min-height` instead of a hard `height` prop --
    the rect height is still a floor, never a ceiling, but the `height` HTML attribute is still
    set too (browsers already treat a `<td height=...>` attribute as advisory-minimum, never
    clipping); non-reflowing leaves (raster/rows-container) get an exact `height` prop since
    their content is always exactly the rect size."""
    width = int(cell.rect.width)
    height = int(cell.rect.height)
    bgcolor_attr, bg_props = _background_style_props(cell.background, cell.rect, ctx, "cellbg")
    _band = getattr(ctx, "current_band_color", None)
    if not bgcolor_attr and _band:
        bgcolor_attr = f' bgcolor="{_band}"'
        bg_props = f"background-color:{_band};"
    colspan_attr = f' colspan="{colspan}"' if colspan and colspan > 1 else ""
    # INTRA-ROW VERTICAL OFFSET: a cell whose design top sits BELOW its row's top (a chip's
    # content inside a taller card, a footer logo inside a taller band) carries that offset as
    # padding-top -- the grammar-canon spacing mechanism Word honors -- and the height attr grows
    # by the same amount so the td spans row-top..rect.bottom while the CONTENT box stays exactly
    # the design rect. Without this every such cell renders at the row top (measured -15..-22px
    # drifts on the chip stacks and footer band, 2026-07-09).
    off = max(0, int(top_offset) if top_offset else int(getattr(ctx, "current_top_offset", 0)))
    pad_prop = f"padding-top:{off}px;" if off else ""
    height_prop = f"min-height:{height}px;" if min_height else f"height:{height}px;"
    height_attr = f' height="{height + off}"' if (height + off) > 0 else ""
    # data-region: an inert DOM anchor (Word's engine ignores unknown attributes) so the fidelity
    # gate can locate this exact region in a browser render and measure it (bounding box, line
    # count, clip) without guessing from coordinates.
    region_attr = f' data-region="{html.escape(region_id, quote=True)}"' if region_id else ""
    style = f"width:{width}px;{height_prop}{bg_props}{extra_style}{pad_prop}"
    return f'<td{colspan_attr}{region_attr} width="{width}"{height_attr}{bgcolor_attr} valign="top" style="{style}">{inner_html}</td>'


def _wrap_table(rows_html: str, width: int) -> str:
    width = int(width)
    return (
        f'<table role="presentation" border="0" cellpadding="0" cellspacing="0" '
        f'width="{width}" style="width:{width}px;border-collapse:collapse;table-layout:fixed;">'
        f"{rows_html}</table>"
    )


# --- TILING (the S2 layout-projection fix) ------------------------------------------------------
#
# The bug this section fixes: the old emitter put every cell of a Row side by side with NO spacer
# for the gaps between/around them (cells jammed left, widths not summing to the row width) and
# put every TOP-LEVEL Row directly into ONE flat outer `table-layout:fixed` table (rows of
# DIFFERENT cell/column counts collide against the first row's column template -> collapse).
#
# The fix, per TILING BEHAVIOR items 1-5:
#   - Every Row (top-level, or inside a nested "rows" container) becomes its OWN single-column
#     `<tr><td width=W>` wrapping a nested table -- so the OUTER stack is always single-column and
#     `table-layout:fixed` can never collide across rows of different shapes.
#   - That nested per-row table has exactly ONE `<tr>` (the row's own cells tiled edge-to-edge with
#     spacer `<td>`s for every leading/inter-cell/trailing gap) -- again never a multi-row column
#     collision, because there is only ever one row in it.
#   - Vertical spacer `<tr>`s (leading + between rows) are inserted directly in the single-column
#     stack, sized from the REAL gap between consecutive row bboxes.
#   - A `role="rows"` container cell recurses through the exact same stack/tile logic, scoped to
#     its own rect, so the invariant holds at every nesting level.


def _spacer_td(width: int, band_color: Optional[str] = None, band_h: int = 0) -> str:
    """An empty horizontal spacer `<td>` filling exactly `width` px. On a WHITE row:
    `font-size:0;line-height:0;` so it never picks up default text metrics. Inside a COLORED
    band (`band_color`): the probe-certified gap construct -- bgcolor + a single line box
    stretched to the band height (`font-size:1px;line-height:{H}px;mso-line-height-rule:
    exactly;&nbsp;`) -- because Word paints cell shading only over line boxes, and a 0-tall
    line box leaves the spacer column WHITE clean across the band (owner-caught stripes)."""
    w = int(width)
    if w <= 0:
        return ""
    if band_color and band_h and band_h > 0:
        return (f'<td width="{w}" bgcolor="{band_color}" '
                f'style="width:{w}px;background-color:{band_color};font-size:1px;'
                f'line-height:{int(band_h)}px;mso-line-height-rule:exactly;" valign="top">&nbsp;</td>')
    return f'<td width="{w}" style="width:{w}px;font-size:0;line-height:0;" valign="top">&nbsp;</td>'


def _spacer_row(width: int, height: int, band_color: Optional[str] = None) -> str:
    """An empty full-width vertical spacer `<tr>` of exactly `height` px -- the PSD vertical rhythm
    between sections/rows (TILING BEHAVIOR item 3). Emits nothing for a non-positive height.
    Inside a COLORED band the spacer's single line box is stretched to the full gap height and
    shaded (probe-certified: Word paints shading only over line boxes -- a 0-tall line box left
    a white stripe clean across the hero band between headline and CTA, owner-caught)."""
    w = int(width)
    h = int(height)
    if h <= 0:
        return ""
    if band_color:
        return (
            f'<tr><td width="{w}" height="{h}" bgcolor="{band_color}" '
            f'style="width:{w}px;height:{h}px;background-color:{band_color};font-size:1px;'
            f'line-height:{h}px;mso-line-height-rule:exactly;" valign="top">&nbsp;</td></tr>'
        )
    return (
        f'<tr><td width="{w}" height="{h}" '
        f'style="width:{w}px;height:{h}px;font-size:0;line-height:0;" valign="top">&nbsp;</td></tr>'
    )


def _render_row_tiled(
    row: Row, row_idx: int, container_left: int, container_width: int, prefix: tuple, routed: RoutedTree, ctx: _EmitContext
) -> str:
    """Tile ONE Row's cells edge-to-edge across `[container_left, container_left+container_width)`
    (TILING BEHAVIOR item 2): a moving cursor walks the cells in their ORIGINAL order (never
    re-sorted -- that order is exactly the `cell_idx` layer_router's path-index keys are built
    from), emitting a leading/inter-cell/trailing `_spacer_td` for every real gap. The sum of every
    emitted `<td>` width (content + spacer) always equals `container_width` exactly, because the
    cursor walks left->right using each cell's own rect and closes the trailing gap against the
    container's right edge."""
    import dataclasses

    tds = []
    cursor = int(container_left)
    cells = list(row.cells)
    right_edge = int(container_left) + int(container_width)
    _band = getattr(ctx, "current_band_color", None)
    _band_h = 0
    if _band:
        if getattr(row, "rect", None) is not None:
            _band_h = max(1, int(row.rect.bottom) - int(row.rect.top))
        else:
            _band_h = max((int(c.rect.height) for c in cells if c.rect is not None), default=0)
        ctx.current_band_row_h = _band_h
    ctx.row_overhang = 0  # text leaves raise this; the stacker consumes it (OVERHANG ABSORPTION)
    # The row's TOP: the band-expanded design span when present, else the highest cell. A cell
    # whose own top sits below this renders with that delta as padding-top (see _wrap_td's
    # INTRA-ROW VERTICAL OFFSET) -- valign="top" alone parks every cell at the row top and loses
    # the offset (measured -15..-22px on chip stacks and the footer band).
    if getattr(row, "rect", None) is not None:
        row_top = int(row.rect.top)
    else:
        row_top = min((int(c.rect.top) for c in cells if c.rect is not None), default=int(container_left))
    for cell_idx, cell in enumerate(cells):
        gap = int(cell.rect.left) - cursor
        if gap > 0:
            tds.append(_spacer_td(gap, band_color=_band, band_h=_band_h))
        key = prefix + (row_idx, cell_idx)
        _cell_top_offset = max(0, int(cell.rect.top) - row_top)
        # WIDTH BUDGET (Grammar G containment): Word's glyph advances run WORD_METRIC_SAFETY
        # wider than the measurement the design box was cut to, so a live-text cell sized to its
        # ink clips its last glyph in classic Outlook. Carve the budget out of the cell's OWN
        # trailing gap -- spacer px become cell px, the neighbor cells never move, the row still
        # tiles to container_width exactly. Capped at the available slack: a text cell with no
        # trailing gap keeps its design width (the line-count certification still flags it).
        carve = 0
        if cell.role == "text" and cell.text_rect is None:
            next_left = int(cells[cell_idx + 1].rect.left) if cell_idx + 1 < len(cells) else right_edge
            slack = max(0, next_left - int(cell.rect.right))
            want = int(round(cell.rect.width * (WORD_METRIC_SAFETY - 1.0))) + 1
            carve = min(slack, want)
        _prev_off = getattr(ctx, "current_top_offset", 0)
        _prev_dw = getattr(ctx, "current_design_width", None)
        ctx.current_top_offset = _cell_top_offset
        try:
            if carve > 0:
                # DUAL-WIDTH: the td takes the carved (Word-budget) width; the DESIGN width rides
                # alongside so the text leaf can pin an inner block to it -- Chromium then wraps
                # at the design width (ink matches the proof 1:1) while Word, which ignores inner
                # block widths, uses the full carved td and never clips.
                ctx.current_design_width = int(cell.rect.width)
                widened = dataclasses.replace(
                    cell, rect=BBox(left=cell.rect.left, top=cell.rect.top,
                                    right=cell.rect.right + carve, bottom=cell.rect.bottom))
                tds.append(_render_cell(widened, key, routed, ctx))
                cursor = int(cell.rect.right) + carve
            else:
                ctx.current_design_width = None
                tds.append(_render_cell(cell, key, routed, ctx))
                cursor = int(cell.rect.right)
        finally:
            ctx.current_top_offset = _prev_off
            ctx.current_design_width = _prev_dw
    trailing = right_edge - cursor
    if trailing > 0:
        tds.append(_spacer_td(trailing, band_color=_band, band_h=_band_h))
    return "".join(tds)


def _row_backdrop_color(row_rect: Optional[BBox], ctx: _EmitContext) -> Optional[str]:
    """The FLAT color visible immediately around a row in the design, sampled from the FLATTEN's
    own pixels (left+right margin strips beside the row's content box). This is ground truth for
    "what backdrop does this row sit on": solver-side band assignment must otherwise guess paint
    order from psd_adapter's approximate z, and a wrong guess paints a section fill the design
    hides (the recurring invented-background defect). Returns None when the margins are textured
    (a photo backdrop) or no composite/margins are available -- caller falls back to band logic."""
    if row_rect is None:
        return None
    composite = ctx.composite
    if composite is None or isinstance(composite, dict):
        return None
    try:
        d = ctx.density
        w, h = composite.size
        t, b = int(row_rect.top * d), int(row_rect.bottom * d)
        strips = []
        left_edge = int(row_rect.left * d)
        right_edge = int(row_rect.right * d)
        if left_edge >= 8:
            strips.append((max(0, left_edge - 12), t, max(1, left_edge - 2), b))
        if right_edge <= w - 8:
            strips.append((min(w - 1, right_edge + 2), t, min(w, right_edge + 12), b))
        if not strips:
            return None
        import numpy as np

        pixels = []
        for box in strips:
            l_, t_, r_, b_ = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
            if r_ <= l_ or b_ <= t_:
                continue
            pixels.append(np.asarray(composite.convert("RGB").crop((l_, t_, r_, b_)), dtype="float32").reshape(-1, 3))
        if not pixels:
            return None
        allpx = np.concatenate(pixels, axis=0)
        if float(allpx.std(axis=0).max()) > 6.0:
            return None  # textured margin -- not a flat backdrop
        mean = allpx.mean(axis=0)
        return "#%02X%02X%02X" % tuple(int(round(c)) for c in mean)
    except Exception as exc:
        ctx.warnings.append({"type": "row_backdrop_sample_failed", "row_rect": row_rect.to_dict(), "reason": repr(exc)})
        return None


_RE_BGCOLOR_HEX = re.compile(r'bgcolor="(#[0-9A-Fa-f]{6})"')


def _crop_composite_rgb(rect: Optional[BBox], ctx: _EmitContext, dtype: str):
    """Shared crop primitive for the four flatten-sampling functions below: convert `rect` to
    device pixels at `ctx.density`, clamp to the composite's bounds, and return the cropped RGB
    pixels as a numpy array of `dtype`. None when no composite is available (unloaded, or a
    dict layer index) or the rect degenerates to an empty box after clamping -- callers each
    decide what "nothing to sample" means for them (permissive True, no color, no ink, no
    profile). Does not itself catch a real crop/convert exception -- that still propagates into
    the caller's own existing try/except, preserving each site's distinct sentinel/telemetry."""
    import numpy as np

    composite = ctx.composite
    if rect is None or composite is None or isinstance(composite, dict):
        return None
    box = tuple(int(round(v * ctx.density)) for v in (rect.left, rect.top, rect.right, rect.bottom))
    w, h = int(composite.width), int(composite.height)
    l_, t_, r_, b_ = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
    if r_ <= l_ or b_ <= t_:
        return None
    return np.asarray(composite.convert("RGB").crop((l_, t_, r_, b_)), dtype=dtype)


def _row_color_matches_design(color: str, row_rect: BBox, ctx: _EmitContext) -> bool:
    """Does `color` actually cover this row in the DESIGN? Crop the flatten at the row rect and
    measure the fraction of pixels within a small delta of `color`; under 50% means the color
    belongs to something smaller inside the row, not to the row itself. Errs permissive (True)
    when no composite is available -- this is a guard against inventing fill, not a validator."""
    try:
        import numpy as np

        arr = _crop_composite_rgb(row_rect, ctx, "float32")
        if arr is None:
            return True
        target = np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)], dtype="float32")
        close = (np.abs(arr - target).max(axis=-1) < 40.0)
        return float(close.mean()) >= 0.5
    except Exception:
        return True


def _render_banded_single_leaf(
    row: Row, row_idx: int, row_rect: Optional[BBox], bgcolor_attr: str,
    container_left: int, container_width: int, prefix: tuple, ctx: _EmitContext
) -> Optional[str]:
    """A BANDED row holding a single image leaf renders as the human OFT's own construct: ONE
    full-width td carrying the band fill, the image placed by td padding alone. The tiled form
    (spacer + leaf + spacer) is Chromium-correct, but classic Outlook paints every empty spacer's
    &nbsp; line box WHITE over the row shading -- a ~16px white stripe clean across the band at
    the content line (owner-reported three times; pixel-measured 2026-07-14: spacers grey at
    y2001-23 and y2040-61 but white at y2024-39, exactly one default line box). No spacer cells
    means nothing Word can stripe. The paddings also restore the band's FULL design height: the
    tiled leaf realized only offset+content (61px of an 83px band), leaving the logos
    bottom-flush instead of centered. Returns None when the row doesn't qualify -- caller falls
    through to the tiled form."""
    if row_rect is None or len(row.cells) != 1:
        return None
    m = _RE_BGCOLOR_HEX.search(bgcolor_attr or "")
    if not m:
        return None
    if m.group(1).upper() in ("#FFFFFF", "#FEFEFE"):
        # White rows keep the tiled form: Word's white spacer line boxes are invisible on a
        # white band, and the tiled markup is what the rest of the tooling pattern-matches.
        return None
    cell = row.cells[0]
    if cell.rect is None or render_role(cell) not in (ROLE_IMAGE, ROLE_GRAPHIC):
        return None
    key = prefix + (row_idx, 0)
    region_id = "_".join(str(p) for p in key)
    result = _rasterize_or_warn(cell, ctx, kind="image", region_id=region_id)
    if result is None:
        return None  # the tiled path still renders its loud degrade form
    color = m.group(1)
    pad_top = max(0, int(cell.rect.top) - int(row_rect.top))
    pad_left = max(0, int(cell.rect.left) - int(container_left))
    pad_bottom = max(0, int(row_rect.bottom) - int(cell.rect.bottom))
    width = int(container_width)
    _record_region(ctx, region_id, cell, render_role(cell), "raster", brand_font_rasterized=False, alt=result.alt)
    disp_w = round(result.width / ctx.density)
    disp_h = round(result.height / ctx.density)
    alt = html.escape(result.alt or "", quote=True)
    # Same attribute shape as _img_tag (src first -- downstream tooling and the tamper tests
    # pattern-match it), plus data-region on the img itself rather than the td: the gate crops
    # the rendered element against the truth at the CELL rect, and the td spans the full band.
    img = (f'<img src="{result.relpath}" width="{disp_w}" height="{disp_h}" '
           f'alt="{alt}" data-region="{html.escape(region_id, quote=True)}" '
           f'style="display:block;border:0;max-width:100%;">')
    href = _href_for(cell, ctx.link_manifest, region_id)
    if href:
        img = f'<a href="{html.escape(href, quote=True)}">{img}</a>'
        ctx.links_bound.append({"kind": "slot" if cell.link_slot else "region", "region": region_id, "url": href, "key": cell.link_slot if cell.link_slot else region_id})
    # NO height prop/attr: height and padding STACK in the td box model -- an 84px band emitted
    # with height:84px + 46px of padding rendered 130px in Word (measured vs the design's 82).
    # The paddings + the image ARE the band height. line-height/font-size zero kill the image
    # line box's descent slack.
    td = (f'<td width="{width}" bgcolor="{color}" valign="top" '
          f'style="width:{width}px;background-color:{color};line-height:0;font-size:0;'
          f'padding:{pad_top}px 0 {pad_bottom}px {pad_left}px;">{img}</td>')
    ctx.row_overhang = 0
    return f"<tr>{td}</tr>"


def _render_row_section(
    row: Row, row_idx: int, container_left: int, container_width: int, prefix: tuple, routed: RoutedTree, ctx: _EmitContext
) -> str:
    """Wrap ONE Row as a full-width single-column `<tr><td>` (TILING BEHAVIOR item 1) containing a
    nested table that tiles that row's own cells (item 2). Because this nested table only ever
    has the one `<tr>` produced here, `table-layout:fixed` is safe on it -- there is no second row
    of a different shape to collide with."""
    row_rect = _row_bbox(row)
    bgcolor_attr, bg_props = _background_style_props(row.background, row_rect, ctx, "rowbg")
    # Design-pixel override: for FLAT backdrops, the flatten sample outranks the band guess (see
    # _row_backdrop_color). Image backgrounds (textured) keep the band path.
    if "background-image:url(" not in bg_props:
        sampled = _row_backdrop_color(row_rect, ctx)
        if sampled is not None:
            bgcolor_attr = f' bgcolor="{sampled}"'
            bg_props = f"background-color:{sampled};"
    # DESIGN VERIFICATION (Layout Invariant): whatever color survived above must actually be what
    # the design shows across this row -- a band-assignment guess can hand a row the color of a
    # SMALL element inside it (live-caught 2026-07-09: the CTA row painted the button's blue
    # full-width). The flatten's own pixels inside the row rect are ground truth; a color that
    # matches under half of them is invented -- drop it loudly.
    if bgcolor_attr and row_rect is not None:
        # The <tr> paints the FULL container width, not just the row rect -- so the color must
        # match the design across the container-wide strip at this row's y-range (checking only
        # the row rect let a button's blue pass because the button fills its own bbox).
        _strip = BBox(left=container_left, top=row_rect.top,
                      right=container_left + container_width, bottom=row_rect.bottom)
        _m = _RE_BGCOLOR_HEX.search(bgcolor_attr)
        if _m and not _row_color_matches_design(_m.group(1), _strip, ctx):
            ctx.warnings.append({
                "type": "row_backdrop_color_rejected_by_design_pixels",
                "row_rect": row_rect.to_dict(),
                "color": _m.group(1),
                "reason": "the assigned row backdrop color matches under half of the design's own "
                          "pixels across this row -- painting it would invent a band; dropped",
            })
            bgcolor_attr, bg_props = "", ""
    banded = _render_banded_single_leaf(row, row_idx, row_rect, bgcolor_attr,
                                        container_left, container_width, prefix, ctx)
    if banded is not None:
        return banded
    _bm = _RE_BGCOLOR_HEX.search(bgcolor_attr or "")
    _band_color = _bm.group(1) if (_bm and _bm.group(1).upper() not in ("#FFFFFF", "#FEFEFE")) else None
    _prev_band = getattr(ctx, "current_band_color", None)
    ctx.current_band_color = _band_color or _prev_band
    try:
        tds_html = _render_row_tiled(row, row_idx, container_left, container_width, prefix, routed, ctx)
    finally:
        ctx.current_band_color = _prev_band
    style_attr = f' style="{bg_props}"' if bg_props else ""
    inner_tr = f"<tr{bgcolor_attr}{style_attr}>{tds_html}</tr>"
    nested_table = _wrap_table(inner_tr, container_width)
    width = int(container_width)
    outer_td = f'<td width="{width}" style="width:{width}px;padding:0;" valign="top">{nested_table}</td>'
    return f"<tr>{outer_td}</tr>"


def _render_stacked_rows(
    rows: list, container_top: int, container_left: int, container_width: int, prefix: tuple, routed: RoutedTree, ctx: _EmitContext
) -> str:
    """Stack `rows` vertically inside `[container_top, ...)` (TILING BEHAVIOR item 3): each Row
    becomes exactly one single-column `_render_row_section` `<tr>`, preceded by a `_spacer_row` for
    the leading top gap (the first row) and for every real gap between consecutive row bboxes --
    reproducing the PSD's vertical rhythm instead of letting rows stack flush against each other.
    Used for BOTH the top-level `tree.rows` stack and every nested `role=="rows"` container, so the
    same tiling invariant holds at every nesting level (item 5)."""
    parts = []
    cursor = int(container_top)
    prev_bg_id = None

    def _band_extent(bg) -> Optional[tuple]:
        # The band layer's own DESIGN bbox (top, bottom, hex color) -- lets the stack fill the
        # band's leading/trailing strips that no row covers (the footer band starts 30px above
        # its first row and runs 38px past its last; both rendered white, owner-caught).
        if bg is None or bg.image_source_layer_id is None or not ctx.layer_index:
            return None
        layer = ctx.layer_index.get(bg.image_source_layer_id)
        if layer is None:
            return None
        try:
            bb = layer.bbox  # psd-tools (left, top, right, bottom) at raw canvas px
            top = int(round(bb[1] / ctx.density))
            bottom = int(round(bb[3] / ctx.density))
        except Exception as exc:
            ctx.warnings.append({"type": "band_extent_unavailable", "layer_id": bg.image_source_layer_id, "reason": repr(exc)})
            return None
        _attr, _ = _background_style_props(
            bg, BBox(left=container_left, top=top, right=container_left + container_width,
                     bottom=bottom), ctx, "bandextent")
        m = _RE_BGCOLOR_HEX.search(_attr or "")
        if not m or m.group(1).upper() in ("#FFFFFF", "#FEFEFE"):
            return None
        return top, bottom, m.group(1)

    def _band_fill_row(height: int, color: str) -> str:
        h = int(height)
        return (f'<tr><td width="{int(container_width)}" height="{h}" bgcolor="{color}" '
                f'style="width:{int(container_width)}px;height:{h}px;background-color:{color};'
                f'font-size:1px;line-height:{h}px;mso-line-height-rule:exactly;" '
                f'valign="top">&nbsp;</td></tr>')

    prev_extent = None
    for row_idx, row in enumerate(rows):
        row_rect = _row_bbox(row)
        row_top = int(row_rect.top) if row_rect is not None else cursor
        row_bottom = int(row_rect.bottom) if row_rect is not None else cursor
        gap = row_top - cursor
        # consume the previous row's rendered overhang (see OVERHANG ABSORPTION)
        _pending = getattr(ctx, "pending_overhang", 0)
        if _pending > 0 and gap > 0:
            _absorb = min(_pending, gap - 1 if gap > 1 else 0)
            gap -= _absorb
        _bg_id = row.background.image_source_layer_id if row.background is not None else None
        if gap > 0:
            # A gap BETWEEN two rows that sit on the SAME background band is part of that band
            # (the footer's grey section reads grey through the gap between its logo row and its
            # legal row -- rendering it white split the band into bars, owner-caught twice).
            if _bg_id is not None and _bg_id == prev_bg_id:
                _attr, _props = _background_style_props(
                    row.background, BBox(left=container_left, top=cursor,
                                         right=container_left + container_width, bottom=row_top),
                    ctx, "gapbg")
                if _attr:
                    # A real band color resolved: give the gap a full-height line box
                    # (probe-certified -- fs0/lh0 painted 0px of the band and Word filled the
                    # rest of the gap row WHITE).
                    parts.append(
                        f'<tr><td width="{int(container_width)}" height="{gap}"{_attr} '
                        f'style="width:{int(container_width)}px;height:{gap}px;font-size:1px;'
                        f'line-height:{gap}px;mso-line-height-rule:exactly;{_props}" '
                        f'valign="top">&nbsp;</td></tr>')
                else:
                    # Shared background resolved to NO paintable color (a phantom canvas-size
                    # panel the coverage guard skipped): a plain white spacer, zero line box.
                    parts.append(_spacer_row(container_width, gap))
            else:
                # Band handoff: fill the OUTGOING band's trailing strip, the plain white gap,
                # then the INCOMING band's leading strip -- in design order.
                extent = _band_extent(row.background)
                trail = 0
                if prev_extent is not None:
                    trail = min(max(0, prev_extent[1] - cursor), gap)
                    if trail > 0:
                        parts.append(_band_fill_row(trail, prev_extent[2]))
                lead = 0
                if extent is not None:
                    lead = min(max(0, row_top - max(extent[0], cursor + trail)), gap - trail)
                white = gap - trail - lead
                if white > 0:
                    parts.append(_spacer_row(container_width, white,
                                             band_color=getattr(ctx, "current_band_color", None)))
                if lead > 0 and extent is not None:
                    parts.append(_band_fill_row(lead, extent[2]))
        parts.append(_render_row_section(row, row_idx, container_left, container_width, prefix, routed, ctx))
        cursor = max(cursor, row_bottom)
        prev_bg_id = _bg_id
        if row.background is None:
            prev_extent = None
        else:
            prev_extent = _band_extent(row.background) or prev_extent
        ctx.pending_overhang = getattr(ctx, "row_overhang", 0)
    # Trailing strip of the LAST band (the footer runs to the design's bottom edge; stopping at
    # the last row's ink rendered the band's tail white).
    if prev_extent is not None and prev_extent[1] > cursor:
        parts.append(_band_fill_row(prev_extent[1] - cursor, prev_extent[2]))
    return "".join(parts)


def _wrap_document(email_name: str, body_table_html: str) -> str:
    title = html.escape(email_name or "")
    return (
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:o="urn:schemas-microsoft-com:office:office">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"{DPI_LOCK_BLOCK}\n"
        f"<title>{title}</title>\n"
        "</head>\n"
        '<body style="margin:0;padding:0;">\n'
        f"<center>{body_table_html}</center>\n"
        "</body>\n"
        "</html>\n"
    )


def _record_region(
    ctx: _EmitContext,
    region_id: str,
    cell: Cell,
    role: str,
    render: str,
    *,
    brand_font_rasterized: bool,
    alt: Optional[str],
    warning: Optional[str] = None,
    design_lines: Optional[int] = None,
    space_after_px: Optional[float] = None,
    box: bool = False,
) -> None:
    entry = {
        "region_id": region_id,
        "source_layer_id": cell.source_layer_id,
        "image_source_layer_ids": list(cell.image_source_layer_ids) if cell.image_source_layer_ids else None,
        "role": role,
        "render": render,
        "brand_font_rasterized": brand_font_rasterized,
        "editable": bool(cell.editable),
        "link_slot": cell.link_slot,
        "alt": alt,
        # CSS-space rect (the tree is already density-scaled by emit()); the fidelity gate crops
        # the PSD-composite truth and locates the rendered region from this + the data-region DOM
        # anchor _wrap_td stamps on the leaf <td>.
        "rect": cell.rect.to_dict(),
    }
    if box:
        # BOX construct (fill shape containing its text): the td IS the shape, deliberately
        # taller than the text's line boxes -- the gate must not read that height as wrapping.
        entry["box"] = True
    if getattr(cell, "sub_highlights", None):
        # Substring highlight spans: the pink covers the WORDS only (brackets excluded, owner
        # ruling) while the design flatten's chip covers the whole token -- an intentional
        # deviation the fill comparison must not flag.
        entry["span_highlight"] = True
    if design_lines:
        # The emitter's own ink-model line count (real leading + SpaceAfter) for live text -- the
        # fidelity gate consumes this instead of re-deriving capacity by floor-dividing the box
        # height, which under-counts every ink-tight box (a 44px caption box at 24px leading holds
        # TWO lines: 24 + 20px of last-line ink; 44//24 said one).
        entry["design_lines"] = int(design_lines)
    if space_after_px:
        # Total inter-paragraph SpaceAfter rendered as real <p> margins inside this cell. The
        # gate's rendered-line count divides scroll height by line-height; without subtracting
        # these margins, 3 bullet lines + 2x8.5px paragraph gaps reads as a phantom 4th line.
        entry["space_after_px"] = float(space_after_px)
    if warning:
        entry["warning"] = warning
    ctx.regions.append(entry)


def _img_tag(result, density: float = 1.0) -> str:
    alt = html.escape(result.alt or "", quote=True)
    # The PNG is full resolution (density x pixels); DISPLAY it at CSS = pixels / density. At
    # density 1.0 this is exactly the PNG's own dims (unchanged), preserving any headline overflow
    # aspect; at 2x the 2x-pixel PNG displays at half its pixels = retina-crisp.
    disp_w = round(result.width / density)
    disp_h = round(result.height / density)
    return (
        f'<img src="{result.relpath}" width="{disp_w}" height="{disp_h}" '
        f'alt="{alt}" style="display:block;border:0;max-width:100%;">'
    )


def _uniform_bleed_rows(rect: BBox, ctx: _EmitContext, max_rows: int = 4) -> int:
    """How many rows directly above `rect` share ONE flat backdrop color (the design's own
    pixels). Bleeding further pulls a DIFFERENT surface into the crop -- the hero photo's
    4-row bleed crossed its band's top edge and rendered 2 white page rows as a box above
    the art (owner-caught). Returns 0..max_rows."""
    comp = ctx.composite
    if comp is None or isinstance(comp, dict):
        return 0
    try:
        import numpy as np

        d = ctx.density
        x0 = max(0, int(rect.left * d))
        x1 = min(comp.width, int(rect.right * d))
        if x1 <= x0:
            return 0
        ref = None
        rows = 0
        for k in range(1, max_rows + 1):
            y1 = int((rect.top - k + 1) * d)
            y0 = int((rect.top - k) * d)
            if y0 < 0:
                break
            band = np.asarray(comp.convert("RGB").crop((x0, max(0, y0), x1, max(1, y1))), dtype="float32")
            if band.size == 0:
                break
            if float(band.std(axis=(0, 1)).max()) > 6.0:
                break  # textured -- not a flat backdrop
            mean = band.mean(axis=(0, 1))
            if ref is None:
                ref = mean
                # The bleed rows must MATCH what Word paints behind this cell (its band/cell
                # fill, else the white page): the hero photo sits on an E9E9E9 band but the
                # rows above it are white PAGE -- bleeding them in rendered a pale box above
                # the art and un-anchored it (owner-caught). Same-color-as-backdrop only.
                _bg_hex = getattr(ctx, "current_band_color", None)
                if _bg_hex is None:
                    _bg_hex = "#FFFFFF"
                try:
                    _bg = np.array([int(_bg_hex[i:i + 2], 16) for i in (1, 3, 5)], dtype="float32")
                except Exception:
                    _bg = np.array([255.0, 255.0, 255.0], dtype="float32")
                if float(np.abs(mean - _bg).max()) > 10.0:
                    return 0
            elif float(np.abs(mean - ref).max()) > 8.0:
                break  # backdrop changed (band edge) -- stop before crossing it
            rows = k
        return rows
    except Exception as exc:
        ctx.warnings.append({"type": "uniform_bleed_uncomputed", "reason": repr(exc)})
        return 0


def _rasterize_or_warn(cell: Cell, ctx: _EmitContext, *, kind: str, region_id: str):
    """Call `rasterizer.rasterize_cell`, catching every documented failure mode so one bad region
    never aborts the whole bundle. Returns a `RasterResult`, or None if degraded (a warning has
    already been recorded on `ctx.warnings`)."""
    final_copy = _final_copy_for(cell, ctx.copy_manifest) if kind == "brand_headline" else None
    _bleed = 0
    _rcell = cell
    ctx.last_raster_bleed = 0
    if kind != "brand_headline" and cell.rect is not None \
            and min(int(cell.rect.width), int(cell.rect.height)) >= 12:
        import dataclasses as _dc

        # TOP-ONLY anti-aliasing bleed: psd-tools' layer bbox excludes faint alpha rows, so a
        # crop AT the bbox flattens rounded tops (the LinkedIn icon, owner-caught). The crop
        # takes extra rows ABOVE at true scale; the cell's intra-row offset shrinks by the
        # same amount, so the art stays design-anchored and the row's height doesn't change.
        # No bottom bleed: growing the image past its design row left the neighbor cells'
        # line boxes 2px short and Word painted a 1px white seam across the band under the
        # icons (owner-caught, measured at y1947). No resize (distorts aspect), no horizontal
        # bleed (would push the fixed-width tiling). 2 AA rows + 1 SACRIFICIAL row: the Word
        # engine drops the first rendered row of a display:block image (edge-marker probe,
        # 2026-07-14: red top-row marker vanished at every size; bottom/sides survived;
        # confirmed on-screen via BitBlt -- it is Word's rendering, not the capture. The drop
        # count jitters 2-4 rows, so up to 4 sacrificial backdrop rows cover the worst case; a
        # smaller drop just shows extra backdrop rows, invisible on the backdrop). Only rows
        # showing the SAME flat backdrop as directly above the image are eligible -- crossing
        # a band edge pulled foreign (white page) rows into the hero crop (owner-caught).
        _bleed = min(_uniform_bleed_rows(cell.rect, ctx), max(0, int(cell.rect.top)))
        _rcell = _dc.replace(cell, rect=BBox(
            left=int(cell.rect.left), top=int(cell.rect.top) - _bleed,
            right=int(cell.rect.right), bottom=int(cell.rect.bottom)))
    try:
        result = rasterize_cell(
            _rcell,
            ctx.out_root,
            final_copy=final_copy,
            composite=ctx.composite,
            layer_names=ctx.layer_names,
            assets_subdir=ctx.assets_subdir,
            filename=f"{region_id}.png",
            registry=ctx.registry,
            source_scale=ctx.density,
        )
    except (RasterizerUnavailable, ValueError) as exc:
        ctx.warnings.append(
            {"type": "rasterize_failed", "kind": kind, "region": region_id, "reason": str(exc)}
        )
        return None
    if _bleed:
        # The image is `bleed` px taller at the TOP than the design rect: pull the cell's
        # intra-row offset up by the same amount so the visible art stays design-anchored.
        # When the cell sits flush at the row top (no offset to consume), the extra rows
        # surface as ordinary overhang for the stacker to absorb.
        _off = max(0, int(getattr(ctx, "current_top_offset", 0)))
        ctx.current_top_offset = max(0, _off - _bleed)
        _short = max(0, _bleed - _off)
        if _short > getattr(ctx, "row_overhang", 0):
            ctx.row_overhang = _short
        ctx.last_raster_bleed = _bleed
    ctx.assets.append(result.relpath)
    return result


def _divider_ink_color(rect: Optional[BBox], ctx: _EmitContext) -> Optional[str]:
    """The INK color of a hairline rule: mean of the crop's darker-than-backdrop pixels. The
    MODE color of a 5px divider band is the white around the 1-2px line -- painting the mode
    rendered the rule invisible (gate-caught)."""
    composite = ctx.composite
    if rect is None or composite is None or isinstance(composite, dict):
        return None
    try:
        import numpy as np

        arr = _crop_composite_rgb(rect, ctx, "float32")
        if arr is None:
            return None
        luma = arr @ np.array([0.299, 0.587, 0.114], dtype="float32")
        bg = float(np.median(luma))
        ink = arr[luma < bg - 12]
        if ink.size == 0:
            ink = arr[luma <= luma.min() + 6]
        if ink.size == 0:
            return None
        mean = ink.reshape(-1, 3).mean(axis=0)
        return "#%02X%02X%02X" % tuple(int(round(c)) for c in mean)
    except Exception as exc:
        ctx.warnings.append({"type": "divider_ink_defaulted", "reason": repr(exc)})
        return None


def _dominant_color_of_rect(rect: Optional[BBox], ctx: _EmitContext) -> Optional[str]:
    """The MODE color (16-level quantized) of the flatten's pixels inside `rect` -- what the
    design actually paints there, robust to a minority of foreground glyphs. None when no
    composite is available or anything fails (callers fall back)."""
    try:
        import numpy as np

        arr = _crop_composite_rgb(rect, ctx, "int32")
        if arr is None:
            return None
        arr = arr.reshape(-1, 3)
        q = (arr // 16)
        keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
        vals, counts = np.unique(keys, return_counts=True)
        mode = int(vals[int(counts.argmax())])
        members = arr[keys == mode]
        mean = members.mean(axis=0)
        return "#%02X%02X%02X" % tuple(int(round(c)) for c in mean)
    except Exception as exc:
        # Record WHY, exactly like the sibling _divider_ink_color: a genuine crop/numpy error
        # here silently degrades a button fill to DEFAULT_BUTTON_BG, otherwise indistinguishable
        # from the legitimate no-composite case.
        ctx.warnings.append({"type": "dominant_color_defaulted", "reason": repr(exc)})
        return None


def _preserve_multispace(escaped: str) -> str:
    """Keep runs of 2+ spaces (designer bullet gaps: '\u2022  Infographic') -- HTML/Word
    collapse whitespace runs to one space, erasing the design's own spacing. Every space
    that is followed by another space becomes NBSP; count and width are preserved in both
    engines."""
    return re.sub(" (?= )", "\u00a0", escaped)


def _hex_luma(color: Optional[str]) -> Optional[float]:
    """Rec.601 luma of a #RRGGBB string (0-255); None when unparsable."""
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        return 0.299 * r + 0.587 * g + 0.114 * b
    except Exception:
        return None


def _render_button(cell: Cell, label: str, href: Optional[str], ctx: _EmitContext, *, wrap: bool = False, size_override: Optional[float] = None) -> str:
    """A bulletproof-button pattern: the OUTER grid `<td>` (built by the caller via `_wrap_td`)
    keeps the fixed pixel width that mirrors the PSD column geometry, but the button itself -- a
    single-cell nested `<table>` -- has constant padding and deliberately NO width attribute/style,
    so it grows with its label exactly as EARS calls for. No VML: this bundle grammar forbids it
    outright, so the button is a plain table cell, not a rounded VML shape.

    `wrap=True` (only when the caller has already determined the label does NOT fit its column at
    nowrap width -- see `_render_cta_leaf`'s own overflow estimate) swaps `white-space:nowrap` for
    `white-space:normal`: the label still renders in full (never clipped/truncated -- the
    copy-overflow guard is unchanged, and the overflow is still flagged), it just wraps across
    multiple lines within the button's ANCESTOR fixed-width `<td>` instead of forcing one huge
    unbreakable line that would blow the whole fixed-width canvas out to the label's full pixel
    width (the actual tiling-collapse shape this guard exists to prevent -- a real CTA's short
    label never trips this branch, so the classic bulletproof nowrap button is unchanged for it)."""
    run = dominant_run(cell.text) if cell.text is not None else None
    font_name = run.font if run is not None else None
    resolution = resolve(font_name, registry=ctx.registry)
    size = float(size_override) if size_override else (float(run.size) if (run is not None and run.size) else DEFAULT_FONT_SIZE)
    text_color = (run.color if (run is not None and run.color) else None) or DEFAULT_BUTTON_TEXT_COLOR
    # Button fill precedence: the cell's own assigned background color; else the DESIGN's own
    # pixels (dominant color of the button box in the flatten -- the label's glyphs are a
    # minority of the box, so the mode is the shape fill); else the default. The design sample
    # matters because the solver often attaches the button shape's color to the ROW (where the
    # full-width design check rightly strips it), leaving the cell itself colorless.
    bg_color = (cell.background.color if cell.background is not None else None) \
        or _dominant_color_of_rect(cell.rect, ctx) or DEFAULT_BUTTON_BG
    label_html = html.escape(label or "")

    white_space = "normal" if wrap else "nowrap"
    # Vertical padding derives from the DESIGN height when the solver expanded this cell/row to
    # its backing button shape -- the button then occupies exactly the shape's height instead of
    # nominal padding inflating the row (+30px of drift per CTA otherwise).
    _lh = int(round(size * LINE_HEIGHT_RATIO))
    _design_h = int(cell.rect.height)
    if _design_h > _lh + 4:
        pad_v = max(2, (_design_h - _lh) // 2)
    else:
        pad_v = DEFAULT_BUTTON_PADDING_V
    # Horizontal: fill the design shape. Measure the label and split the shape's remaining
    # width evenly; fall back to the nominal padding when the shape is unknown/too narrow.
    pad_h = DEFAULT_BUTTON_PADDING_H
    try:
        _lw = measure_text_px(label or "", font_name, size, registry=ctx.registry)
        if _lw:
            _lw *= WORD_METRIC_SAFETY
            _dw = int(cell.rect.width)
            if _dw > _lw + 16:
                pad_h = max(8, int(round((_dw - _lw) / 2)))
    except Exception:
        pass
    inner_style = (
        f"padding:{pad_v}px {pad_h}px;"
        f"background-color:{bg_color};font-family:{resolution.css_stack};{resolution.weight_css}"
        f"font-size:{size:g}px;color:{text_color};text-align:center;"
        f"mso-line-height-rule:exactly;white-space:{white_space};"
    )
    if href:
        content = (
            f'<a href="{html.escape(href, quote=True)}" '
            f'style="color:{text_color};text-decoration:none;display:inline-block;">{label_html}</a>'
        )
    else:
        content = f'<span style="color:{text_color};display:inline-block;">{label_html}</span>'

    button_td = f'<td bgcolor="{html.escape(bg_color, quote=True)}" style="{inner_style}">{content}</td>'
    # align=center: the button's rendered width varies per engine (its label's own metrics);
    # left-anchored in a design-width cell it read a few px LEFT of the design (owner-caught
    # on 'Review the toolkit'). Centered, the label sits on the shape's center in both engines.
    return (
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0" align="center" '
        f'style="border-collapse:collapse;"><tr>{button_td}</tr></table>'
    )


# --- leaf renderers (each returns a complete <td>...</td>) ---------------------------------------


def _runs_char_style(text, copy: str) -> Optional[list]:
    """Per-character (font, size) map from the style runs, aligned to `copy`. None when the runs
    can't be aligned (missing lengths/sizes, span-sum mismatch, late-bound copy)."""
    if text is None or not text.runs:
        return None
    if any(r.length is None or not r.size for r in text.runs):
        return None
    if copy != (text.content or "") or sum(int(r.length) for r in text.runs) != len(copy):
        return None
    style: list = []
    for r in text.runs:
        style.extend([(r.font, float(r.size))] * int(r.length))
    return style if len(style) == len(copy) else None


def _measure_span_px(copy: str, a: int, b: int, style: list, ctx: _EmitContext) -> Optional[float]:
    """Run-aware width of copy[a:b]: contiguous same-style stretches measured whole (kerning
    intact within a run) and summed."""

    total, i = 0.0, a
    while i < b:
        f, sz = style[i]
        j = i
        while j < b and style[j] == (f, sz):
            j += 1
        w = measure_text_px(copy[i:j], f, sz, registry=ctx.registry)
        if w is None:
            return None
        total += w
        i = j
    return total


def _flatten_line_profile(rect: BBox, ctx: _EmitContext) -> Optional[list]:
    """DESIGN-TRUTH line geometry: crop the flatten at the cell rect and project ink rows into
    line bands -> [(top, bottom, left, right), ...] in cell-local CSS px. The flatten is the one
    artifact that shows where Photoshop's own composer actually put every line (the PSD does not
    persist its line breaks) -- band count is the true line count, band widths are the true line
    widths. None when no composite is available or the crop is empty."""
    composite = ctx.composite
    if rect is None or composite is None or isinstance(composite, dict):
        return None
    try:
        import numpy as np

        d = ctx.density
        arr = _crop_composite_rgb(rect, ctx, "int16")
        if arr is None:
            return None
        border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
        bg = np.median(border.reshape(-1, 3), axis=0)
        ink = (np.abs(arr - bg).max(axis=-1) > 48)
        rows = ink.any(axis=1)
        bands = []
        y, H = 0, int(rows.shape[0])
        while y < H:
            if not rows[y]:
                y += 1
                continue
            y0 = y
            # bridge only a 1-row gap (broken glyph interiors); a wider bridge merged an
            # UNDERLINED line into its neighbor (the link underline narrows the inter-line gap)
            # and handed the DP a two-line profile for a three-line paragraph.
            while y < H and (rows[y] or (y + 1 < H and rows[y + 1])):
                y += 1
            seg = ink[y0:y]
            xs = np.where(seg.any(axis=0))[0]
            if len(xs) and (y - y0) >= 4:
                bands.append((y0 / d, y / d, float(xs[0]) / d, float(xs[-1] + 1) / d))
        return bands or None
    except Exception as exc:
        ctx.warnings.append({"type": "flatten_profile_failed", "reason": repr(exc)})
        return None


def _assign_words_to_bands(normalized: str, lo: int, hi: int, targets: list,
                           style: list, ctx: _EmitContext) -> Optional[list]:
    """Core least-squares DP: assign the words of normalized[lo:hi] to len(targets) lines so
    simulated widths best match the flatten's measured band widths (global scale fitted).
    Tabs measure as spaces (designers fake bullet gaps with literal tabs). Returns break
    indices (whitespace positions to freeze) or None."""
    measure_str = normalized.replace(chr(9), " ")
    spans = [(lo + m.start(), lo + m.end()) for m in re.finditer(r"\S+", normalized[lo:hi])]
    n_lines = len(targets)
    if len(spans) < n_lines:
        return None
    import functools

    @functools.lru_cache(maxsize=None)
    def seg_w(i: int, j: int) -> Optional[float]:
        return _measure_span_px(measure_str, spans[i][0], spans[j][1], style, ctx)

    total_sim = seg_w(0, len(spans) - 1)
    if total_sim is None or total_sim <= 0:
        return None
    scale = sum(targets) / total_sim if total_sim else 1.0
    W, N = len(spans), n_lines
    INF = float("inf")
    dp = [[INF] * (W + 1) for _ in range(N + 1)]
    nxt = [[-1] * (W + 1) for _ in range(N + 1)]
    dp[N][W] = 0.0
    for k in range(N - 1, -1, -1):
        for i in range(W - 1, -1, -1):
            for j in range(i + 1, W + 1):
                if (W - j) < (N - k - 1):
                    continue
                if k == N - 1 and j != W:
                    continue  # last line must take the rest
                w = seg_w(i, j - 1)
                if w is None:
                    return None
                cost = (w * scale - targets[k]) ** 2 + dp[k + 1][j]
                if cost < dp[k][i]:
                    dp[k][i] = cost
                    nxt[k][i] = j
    if dp[0][0] == INF:
        return None
    breaks = []
    i = 0
    for k in range(N):
        j = nxt[k][i]
        if j < 0:
            return None
        w = seg_w(i, j - 1) * scale
        if abs(w - targets[k]) > max(12.0, 0.08 * max(targets[k], 1.0)):
            return None
        if k < N - 1:
            idx = spans[j - 1][1]  # whitespace right after the line's last word
            if idx >= len(normalized) or normalized[idx] not in (" ", chr(9)):
                return None
            breaks.append(idx)
        i = j
    return breaks


def _profile_break_assignment(cell: Cell, normalized: str, style: list, ctx: _EmitContext) -> Optional[list]:
    """Assign words to lines so simulated line widths BEST MATCH the flatten's measured band
    widths -- this reproduces Photoshop's own composer output, which greedy wrapping cannot:
    PS's every-line composer balances lines (owner-caught on "powered / by", then again on the
    announcement's multi-paragraph body, one word early per line -- a firm client requirement).
    MULTI-PARAGRAPH cells partition the profile's bands into paragraph groups by the (P-1)
    LARGEST inter-band gaps (paragraph SpaceAfter widens exactly those gaps); the DP then runs
    per paragraph. Returns break indices (whitespace positions to freeze) or None."""
    profile = _flatten_line_profile(cell.rect, ctx)
    if not profile or len(profile) < 2:
        return None
    # LEADING SANITY: every band must be at most ~1.7x the design leading tall -- a taller band
    # is two merged lines (underlines/descenders bridging the gap), and matching words to a
    # merged profile freezes an impossible layout the shrink then "fixes" at 60% size.
    _run_ls = dominant_run(cell.text)
    _lead = float(getattr(_run_ls, "leading", 0) or 0) or (
        float(_run_ls.size) * LINE_HEIGHT_RATIO if (_run_ls is not None and _run_ls.size) else 0.0)
    if _lead and any((b - a) > 1.7 * _lead for a, b, _l, _r in profile):
        return None
    # paragraph segmentation: non-empty \n-separated parts (a blank line has no ink band)
    paras = []
    start = 0
    for i, ch in enumerate(normalized):
        if ch == "\n":
            paras.append((start, i))
            start = i + 1
    paras.append((start, len(normalized)))
    non_empty = [(a, b) for a, b in paras if normalized[a:b].strip()]
    if not non_empty:
        return None
    if len(non_empty) == 1:
        a, b = non_empty[0]
        targets = [r - l for (_, _, l, r) in profile]
        return _assign_words_to_bands(normalized, a, b, targets, style, ctx)
    P = len(non_empty)
    if len(profile) < P:
        return None
    # partition bands at the P-1 largest gaps (design order preserved)
    gap_idx = sorted(range(len(profile) - 1),
                     key=lambda i: (profile[i + 1][0] - profile[i][1]), reverse=True)[:P - 1]
    cuts = sorted(gap_idx)
    groups = []
    prev = 0
    for c in cuts:
        groups.append(profile[prev:c + 1])
        prev = c + 1
    groups.append(profile[prev:])
    breaks: list = []
    for (a, b), bands in zip(non_empty, groups, strict=False):
        if not bands:
            return None
        if len(bands) == 1:
            continue  # single-line paragraph: no interior breaks to freeze
        targets = [r - l for (_, _, l, r) in bands]
        r_ = _assign_words_to_bands(normalized, a, b, targets, style, ctx)
        if r_ is None:
            return None  # all-or-nothing: a partial freeze breaks the shrink certification
        breaks.extend(r_)
    return breaks or None


def _design_line_breaks(cell: Cell, copy: str, ctx: _EmitContext, design_width: int) -> Optional[str]:
    """PSD-VERBATIM FLOW: Photoshop does NOT persist its rendered line breaks (verified on this
    corpus: engine_dict Rendered.Shapes.Children[0].Lines is an empty list), so every engine that
    renders the copy re-derives its own wrap -- Photoshop, Chromium, and Word all measure glyphs
    a little differently and a border-line word drifts across lines per engine. This simulates
    Photoshop's greedy wrap at the DESIGN width with real per-run glyph metrics (PIL fractional
    measurement, NO Word safety factor -- we are reproducing Photoshop, not budgeting for Word);
    when the simulation lands exactly the design box's own ink line count, the breaks are frozen
    into the copy as U+2028 hard separators. Chromium and Word then break at EXACTLY those spots
    and the paragraph reads like the flatten. Returns the hard-broken copy or None (count
    mismatch / run misalignment / late-bound copy) -- caller keeps the natural-wrap machinery."""
    normalized = (copy or "").replace("\r\n", "\n")
    style = _runs_char_style(cell.text, normalized)
    if style is None:
        return None
    run = dominant_run(cell.text)
    if run is None or not run.size:
        return None
    target = _est_design_lines(int(cell.rect.height), float(run.size),
                               getattr(run, "leading", None), _space_after_total(cell, copy))
    if target <= 1:
        return None  # single-line boxes have no breaks to freeze

    # DESIGN-PROFILE FIRST: the flatten's own measured line widths pin Photoshop's exact breaks
    # (its every-line composer balances lines; greedy simulation cannot reproduce that).
    prof_breaks = _profile_break_assignment(cell, normalized, style, ctx)
    if prof_breaks is not None:
        chars = list(normalized)
        for idx in prof_breaks:
            chars[idx] = "\u2028"
        return "".join(chars)

    def _simulate(width):
        break_idxs = []
        total_lines = 0
        pos = 0
        for para in normalized.split("\n"):
            a = pos
            pos += len(para) + 1
            spans = [(m.start() + a, m.end() + a) for m in re.finditer(r"\S+", para)]
            if not spans:
                total_lines += 1
                continue
            line_start, line_end = spans[0]
            lines = []
            for ws, we in spans[1:]:
                w = _measure_span_px(normalized, line_start, we, style, ctx)
                if w is None:
                    return None
                if w <= width + 0.5:
                    line_end = we
                else:
                    lines.append((line_start, line_end))
                    line_start, line_end = ws, we
            lines.append((line_start, line_end))
            total_lines += len(lines)
            for (_, prev_end), (_nxt_start, _) in zip(lines, lines[1:], strict=False):
                # freeze the break on the whitespace char after the previous line's last word
                if prev_end < len(normalized) and normalized[prev_end] == " ":
                    break_idxs.append(prev_end)
                else:
                    return None
        return total_lines, break_idxs

    # PIL measures Photoshop's own faces ~1-3% wider than Photoshop lays them out, so a design
    # box measured at its exact width can simulate one line long. Retry at up to +4% width --
    # the FIRST factor that reproduces the design's own line count wins; bounded so the
    # simulation can never invent an arbitrary wrap.
    for tol in (1.0, 1.01, 1.02, 1.03, 1.04):
        sim = _simulate(design_width * tol)
        if sim is None:
            return None
        total_lines, break_idxs = sim
        if total_lines == target:
            chars = list(normalized)
            for idx in break_idxs:
                chars[idx] = "\u2028"
            return "".join(chars)
        if total_lines < target:
            return None  # already too few lines; a wider surface only wraps less
    return None


def _longest_hard_line_px(cell: Cell, hard_copy: str, ctx: _EmitContext) -> Optional[float]:
    """Shared measurement for the two hard-line-shrink variants below: resolve the frozen
    line's run style, split the frozen copy on its \u2028 breaks, and return the widest line's
    pixel width. None when the style can't be resolved or any line's width can't be measured --
    each caller decides what "can't measure" means for it (reject vs None)."""
    style = _runs_char_style(cell.text, hard_copy.replace("\u2028", " "))
    if style is None:
        return None
    plain = hard_copy.replace("\u2028", "\n")
    longest = 0.0
    pos = 0
    for line in plain.split("\n"):
        w = _measure_span_px(hard_copy.replace("\u2028", " "), pos, pos + len(line), style, ctx)
        if w is None:
            return None
        longest = max(longest, w)
        pos += len(line) + 1
    return longest


def _hard_line_shrink_or_reject(cell: Cell, hard_copy: str, ctx: _EmitContext):
    """_hard_line_shrink with the reject signal surfaced: returns None (every line fits), a
    bounded shrink size, or the string "reject" (the freeze needs >20% shrink, i.e. the frozen
    layout is wrong -- caller must abandon it for natural wrap)."""
    longest = _longest_hard_line_px(cell, hard_copy, ctx)
    if longest is None:
        return "reject"
    need = longest * WORD_METRIC_SAFETY
    avail = float(cell.rect.width)
    if need <= avail:
        return None
    run = dominant_run(cell.text)
    dom = float(run.size)
    factor = (avail / need) * 0.995
    if factor < 0.8 or dom * factor < MIN_LEGIBLE_FONT_SIZE:
        return "reject"
    return round(dom * factor, 1)


def _hard_line_shrink(cell: Cell, hard_copy: str, ctx: _EmitContext) -> Optional[float]:
    """A frozen hard-broken line wider than its td at Word-budgeted metrics would clip -- the
    faithful correction is a proportional shrink of every run (breaks preserved; widths scale
    linearly so the frozen breaks stay valid). None when every line fits."""
    longest = _longest_hard_line_px(cell, hard_copy, ctx)
    if longest is None:
        return None
    need = longest * WORD_METRIC_SAFETY
    avail = float(cell.rect.width)
    if need <= avail:
        return None
    run = dominant_run(cell.text)
    dom = float(run.size)
    factor = max(MIN_LEGIBLE_FONT_SIZE / dom, (avail / need) * 0.995)
    return round(dom * factor, 1)


def _measure_runs_px(text, copy: str, ctx: _EmitContext) -> Optional[float]:
    """True pixel width of SINGLE-LINE copy with MIXED-SIZE runs: each run's own segment measured
    at its own face/size and summed. Flat measurement at the dominant size lies in both directions
    on mixed cells ("60%" is a 40px "60" + a 20px "%" -- flat-at-40 over-measures by 25% and
    triggered a spurious quarter-size shrink). None when runs don't align with the copy or sizes
    are uniform (flat measurement is then identical) -- caller falls back."""
    if text is None or not text.runs or len(text.runs) < 2:
        return None
    if any(r.length is None or not r.size for r in text.runs):
        return None
    if copy != (text.content or "") or "\n" in copy:
        return None
    if sum(int(r.length) for r in text.runs) != len(copy):
        return None
    if len({float(r.size) for r in text.runs}) < 2:
        return None

    total, pos = 0.0, 0
    for r in text.runs:
        seg = copy[pos:pos + int(r.length)]
        pos += int(r.length)
        if not seg:
            continue
        w = measure_text_px(seg, r.font, float(r.size), registry=ctx.registry)
        if w is None:
            return None
        total += w
    return total


def _shrink_to_fit_size(cell: Cell, copy: str, ctx: _EmitContext) -> Optional[float]:
    """SHRINK-TO-FIT: the design proves the copy fits its box in Photoshop's own metrics; when our
    Word-safe metrics run wider (tracking differences, metric drift), the faithful correction is a
    proportional font-size reduction -- never a wrap or extra line the design doesn't have.

    Single-line boxes shrink on measured width (per-run summed when the runs carry mixed sizes --
    see _measure_runs_px). Multi-line boxes shrink until the Word-budgeted wrap holds the design's
    own line count, so line BREAKS land where the design shows them ("of organizations" / "have
    adopted AI(1)") instead of a third line the box never had. Floor at MIN_LEGIBLE_FONT_SIZE.
    Returns the adjusted dominant size or None; per-run sizes scale by the same factor at render
    (_segment_body_html's `scale`)."""
    run = dominant_run(cell.text)
    if run is None or not run.size:
        return None
    size = float(run.size)
    normalized = (copy or "").replace("\r\n", "\n")
    design_lines = _est_design_lines(int(cell.rect.height), size, getattr(run, "leading", None),
                                     _space_after_total(cell, copy))

    if design_lines == 1:
        longest = max(normalized.split("\n"), key=len, default="")
        if not longest:
            return None
        measured = _measure_runs_px(cell.text, normalized, ctx)
        if measured is None:
            measured = measure_text_px(longest, run.font, size, registry=ctx.registry)
        if measured is None:
            return None
        measured *= WORD_METRIC_SAFETY  # Word's glyph advances run wider than PIL's measurement
        if measured <= cell.rect.width:
            return None
        adjusted = max(MIN_LEGIBLE_FONT_SIZE, size * (cell.rect.width / measured) * 0.98)
        return round(adjusted, 1)

    # Multi-line: only act when the Word-budgeted wrap needs MORE lines than the design box.
    lines = _word_budgeted_line_count(normalized, int(cell.rect.width), font_name=run.font,
                                      font_size=size, registry=ctx.registry)
    if lines is None or lines <= design_lines:
        return None
    trial = size
    while trial > MIN_LEGIBLE_FONT_SIZE:
        trial = round(trial * 0.97, 1)
        lines = _word_budgeted_line_count(normalized, int(cell.rect.width), font_name=run.font,
                                          font_size=trial, registry=ctx.registry)
        if lines is not None and lines <= design_lines:
            return trial
    return float(MIN_LEGIBLE_FONT_SIZE)


def _segment_body_html(text, copy: str, ctx: _EmitContext, scale: float = 1.0,
                       bg_ranges: Optional[list] = None) -> Optional[str]:
    """PER-RUN body HTML: when the copy is the PSD's own sample text and every style run carries
    its span length, render each run as its own <span> so mixed sizing survives ("Over 70%" is
    20px/40px/20px in the design -- dominant-run styling flattened it) and baseline-1 runs become
    real <sup> (the footnote digits). `scale` is the cell's shrink-to-fit factor: every run size
    is multiplied by it so a proportional shrink preserves the design's size RATIOS instead of
    flattening the cell to the shrunk dominant. Returns None when run data can't be aligned to
    the copy (late-bound copy manifest, legacy runs without lengths, span-sum mismatch) -- caller
    falls back to the flat dominant-run body."""
    if text is None or not text.runs or len(text.runs) < 1:
        return None
    runs = text.runs
    if any(r.length is None for r in runs):
        return None
    if copy.replace("\u2028", " ") != (text.content or ""):
        return None  # late-bound copy: run spans describe the SAMPLE text, not this copy
        # (frozen U+2028 breaks replaced a space 1:1, so the compare sees the original copy)
    if sum(int(r.length) for r in runs) != len(copy):
        return None
    dom = dominant_run(text)
    dom_size = float(dom.size) if (dom is not None and dom.size) else DEFAULT_FONT_SIZE
    dom_color = (dom.color if (dom is not None and dom.color) else None) or "#000000"
    dom_res = resolve(dom.font if dom is not None else None, registry=ctx.registry)
    styled = (len({(r.size, r.color, r.baseline, r.font, getattr(r, "underline", False)) for r in runs}) > 1) \
        or any(r.baseline for r in runs) or any(getattr(r, "underline", False) for r in runs)
    if not styled:
        return None  # uniform styling: the flat path is identical and simpler

    parts = []
    pos = 0
    for r in runs:
        seg = copy[pos:pos + int(r.length)]
        seg_start = pos
        pos += int(r.length)
        if seg == "":
            continue

        def _esc(t: str) -> str:
            return _preserve_multispace(html.escape(t)).replace("\u2028", "<br/>").replace("\n", "<br>")

        if bg_ranges:
            # Substring highlight chips: split this run's span at range boundaries so the shaded
            # <span>s land exactly on the claimed characters, whatever run they sit in.
            pieces = []
            cur = seg_start
            seg_end = seg_start + len(seg)
            for a, b, colr in bg_ranges:
                if b <= cur or a >= seg_end:
                    continue
                a2, b2 = max(a, cur), min(b, seg_end)
                if a2 > cur:
                    pieces.append((cur, a2, None))
                pieces.append((a2, b2, colr))
                cur = b2
            if cur < seg_end:
                pieces.append((cur, seg_end, None))
            seg_html = "".join(
                (f'<span style="background:{c};">{_esc(copy[a3:b3])}</span>' if c else _esc(copy[a3:b3]))
                for a3, b3, c in pieces)
        else:
            seg_html = _esc(seg)
        props = ""
        if r.size and float(r.size) != dom_size:
            props += f"font-size:{round(float(r.size) * scale, 1):g}px;"
        if r.color and r.color != dom_color:
            props += f"color:{r.color};"
        # Per-run FONT: a run whose face resolves differently from the cell's dominant (a
        # semibold phrase inside regular body copy) carries its own live family/weight -- the
        # td-level font-family would silently flatten it to the dominant weight.
        if r.font != (dom.font if dom is not None else None):
            r_res = resolve(r.font, registry=ctx.registry)
            if r_res.css_stack != dom_res.css_stack:
                props += f"font-family:{r_res.css_stack};"
            if r_res.weight_css != dom_res.weight_css:
                props += r_res.weight_css or "font-weight:normal;font-style:normal;"
        if getattr(r, "underline", False):
            props += "text-decoration:underline;"
        if r.baseline == 1:
            # line-height:0 so the raised digit can't grow the line box (Word honors <sup>).
            parts.append(_wrap_run_seg(seg_html, f'<sup style="line-height:0;{props}">', "</sup>"))
        elif r.baseline == 2:
            parts.append(_wrap_run_seg(seg_html, f'<sub style="line-height:0;{props}">', "</sub>"))
        elif props:
            parts.append(_wrap_run_seg(seg_html, f'<span style="{props}">', "</span>"))
        else:
            parts.append(seg_html)
    return "".join(parts)


def _wrap_run_seg(seg_html: str, open_tag: str, close_tag: str) -> str:
    """Wrap a per-run segment in `open_tag`...`close_tag`, CLOSING and REOPENING the wrapper around
    every Photoshop PARAGRAPH break ("<br>") the segment spans. `_render_paragraph_blocks` splits
    the body on "<br>" into <p> blocks, so a wrapper (span/sup/sub) left straddling that split would
    emit a tag that crosses a </p><p> boundary -- structurally invalid and unpredictable in Word.
    Frozen intra-line breaks ("<br/>") are NOT paragraph splits (the "<br>" substring never matches
    inside "<br/>"), so they stay inside the wrapper untouched. Identity when the segment holds no
    paragraph break (the common case), so styled runs that don't span one are byte-unchanged."""
    if "<br>" not in seg_html:
        return f"{open_tag}{seg_html}{close_tag}"
    return "<br>".join(
        (f"{open_tag}{piece}{close_tag}" if piece else "") for piece in seg_html.split("<br>")
    )


def _space_after_total(cell: Cell, copy: str) -> float:
    if cell.text is None or copy != (cell.text.content or "") or not cell.text.paragraphs:
        return 0.0
    breaks = copy.count("\n")
    return float(sum(pp["space_after"] for pp in cell.text.paragraphs[:breaks]))


def _harmonize_shrink(routed: RoutedTree, ctx: _EmitContext) -> None:
    """SIBLING-CONSISTENT TYPE SIZING: shrink-to-fit is a per-cell computation, but the DESIGN
    assigns one type size to a whole band of siblings (the 3 stat numbers, the 3 stat captions).
    Shrinking each column by its own factor renders the same design size at three different px
    sizes -- inconsistent x-height across the stat columns, owner-caught. Group the text leaves
    by (top-level row, largest run size, leading, base font) and stash the group's SMALLEST
    shrink size per member in ctx.shrink_overrides, so siblings always move together."""
    from collections import defaultdict

    from .layer_router import iter_routed

    groups = defaultdict(list)
    for cell, key, _verb in iter_routed(routed):
        if cell.role == "rows" or cell.text is None or cell.text_rect is not None:
            continue
        role = render_role(cell)
        if role not in (ROLE_MERGE, ROLE_BODY):
            continue
        run = dominant_run(cell.text)
        if run is None or not run.size:
            continue
        copy = _final_copy_for(cell, ctx.copy_manifest)
        # Mirror the render-time decision: frozen-break cells size by their longest hard line,
        # everything else by natural-wrap shrink. (Design width here = the un-carved rect --
        # marginally conservative vs the carved td, but UNIFORMLY so within a group.)
        hard = _design_line_breaks(cell, copy, ctx, int(cell.rect.width))
        shrunk = _hard_line_shrink(cell, hard, ctx) if hard is not None else _shrink_to_fit_size(cell, copy, ctx)
        dom = float(run.size)
        factor = (shrunk / dom) if shrunk else 1.0
        max_run = max((float(r.size) for r in cell.text.runs if r.size), default=dom)
        # Keyed by the LARGEST run size (not the dominant's leading): the "Over 70%" cell is
        # 20px-dominant but belongs with the other 40px stat numbers, and keying on the
        # dominant's leading split that group in two (0.965 vs 0.873 -- the owner's
        # inconsistent columns).
        gkey = (key[0], round(max_run, 1), normalize_font_name(run.font or ""))
        groups[gkey].append(("_".join(str(p) for p in key), factor, dom,
                             float(run.leading or 0.0), max_run))
    for _gkey, members in groups.items():
        if len(members) < 2:
            continue
        fmin = min(f for _, f, _, _, _ in members)
        if fmin < 1.0:
            for region_id, _f, dom, _lead, _mr in members:
                ctx.shrink_overrides[region_id] = round(dom * fmin, 1)
        # Uniform line-height across the group: each member's would-be lh at the group factor,
        # group max wins for everyone (a 21px lh next to a 42px lh staggered the baselines).
        lhs = []
        for _region_id, _f, dom, lead, max_run in members:
            eff = fmin if fmin < 1.0 else 1.0
            lh_lead = int(round(lead * eff)) if lead else int(round(dom * eff * LINE_HEIGHT_RATIO))
            lh_floor = int(math.ceil(max(dom, max_run) * eff * 1.05))
            lhs.append(max(lh_lead, lh_floor))
        lh_group = max(lhs)
        for region_id, _f, _dom, _lead, _mr in members:
            ctx.lh_overrides[region_id] = lh_group


def _bind_inline_links(body: str, copy: str, region_id: str, ctx: _EmitContext) -> str:
    """INLINE links: wrap an exact visible-text match inside a text cell's rendered body in an
    <a> (the footnote-citation pattern: the human OFT links each citation title, and a PSD
    carries no hyperlink data, so the manifest supplies {match, url}). The match runs against
    the RENDERED html with each space also matching a frozen U+2028 break (<br/>), so a citation
    title that wraps lines still binds. color:inherit keeps the design's own run color."""
    _, _, inline = _link_sections(ctx.link_manifest)
    if not inline:
        return body
    for entry in inline:
        match_text = (entry.get("match") or "").strip()
        url = entry.get("url")
        if not match_text or not url or match_text not in (copy or ""):
            continue
        if not _is_safe_href(url):
            continue  # unsafe scheme -> don't bind; surfaces as unbound in reconciliation
        # re.escape escapes the space itself on this interpreter -- replace the ESCAPED form
        # so no orphan backslash is left behind (unbalanced-parenthesis PatternError otherwise).
        pat = re.escape(html.escape(match_text)).replace(re.escape(" "), "(?:\\s|<br/?>)+")
        def _wrap(m, _url=url):
            return (f'<a href="{html.escape(_url, quote=True)}" '
                    f'style="color:inherit;text-decoration:underline;">{m.group(0)}</a>')
        new_body, n = re.subn(pat, _wrap, body, count=1)
        if n:
            body = new_body
            ctx.links_bound.append({"kind": "inline", "region": region_id, "url": url,
                                    "match": match_text})
    return body


def _outer_band_td(bw: int, inner: str, ctx: _EmitContext, align: Optional[str] = None) -> str:
    """The outer wrapper `<td>` a BANDED box-text leaf renders `inner` (its own chip/label
    table) into: the row's shared band fill (`ctx.current_band_color`), if any, plus the
    band's own top-offset padding (`ctx.current_top_offset`) that positions this leaf inside
    it. Shared by the single-line, content-sized, and fallback chip-rendering branches below,
    which differ only in what `inner` markup they wrap and whether the td itself is centered."""
    _band = getattr(ctx, "current_band_color", None)
    _obg = f' bgcolor="{_band}"' if _band else ""
    _oprops = f"background-color:{_band};" if _band else ""
    _off = max(0, int(getattr(ctx, "current_top_offset", 0)))
    _pad = f"padding-top:{_off}px;" if _off else ""
    _align_attr = f' align="{align}"' if align else ""
    return (f'<td width="{bw}"{_obg} valign="top"{_align_attr} '
            f'style="width:{bw}px;{_pad}{_oprops}">{inner}</td>')


def _render_box_text_leaf(cell: Cell, region_id: str, role: str, ctx: _EmitContext) -> str:
    """A text leaf whose fill shape CONTAINS it (cell.rect = the SHAPE, cell.text_rect = the
    ink): the design's placeholder chip / unnamed CTA. Rendered as the probe-certified BOX
    construct (probe_paint2 R5/R6): ONE td at the shape's exact geometry, bgcolor fill,
    valign=top, design-anchored padding + the PSD's own text alignment. Sizing the td to the text bounds instead rendered every
    placeholder box at the wrong shape/size, and Word's +6% glyph advances wrapped the label
    (owner-caught: 'Partner Logo' on two lines in a half-width chip)."""
    import dataclasses as _dc

    copy = _final_copy_for(cell, ctx.copy_manifest)
    href = _href_for(cell, ctx.link_manifest, region_id)
    run = dominant_run(cell.text)
    size = float(run.size) if (run is not None and run.size) else DEFAULT_FONT_SIZE
    color = (run.color if (run is not None and run.color) else None) or "#000000"
    res = resolve(run.font if run is not None else None, registry=ctx.registry)
    bw, bh = int(cell.rect.width), int(cell.rect.height)
    bgcolor_attr, _bgp = _background_style_props(cell.background, cell.rect, ctx, "boxbg")
    _m = _RE_BGCOLOR_HEX.search(bgcolor_attr or "")
    fill = _m.group(1) if _m else (_dominant_color_of_rect(cell.rect, ctx) or "#FFFFFF")
    tr_ = cell.text_rect
    inset = min(max(0, int(tr_.left) - int(cell.rect.left)),
                max(0, int(cell.rect.right) - int(tr_.right)))
    avail = max(20, bw - 2 * min(inset, 12))  # centered text: symmetric usable width
    probe = _dc.replace(cell, rect=BBox(left=int(tr_.left), top=int(tr_.top),
                                        right=int(tr_.left) + avail, bottom=int(tr_.bottom)),
                        text_rect=None)
    shrunk = _shrink_to_fit_size(probe, copy, ctx)
    # DESIGN-WRAPPED box text (the disti hero headline: 3 lines of ink, no manual returns):
    # freeze the flatten's own breaks exactly like a normal text leaf -- the nowrap chip form
    # below is for single-line labels only (it collapsed the headline to one line).
    _lead_bx = float(getattr(run, "leading", 0) or 0) if run is not None else 0.0
    _dl_bx = _est_design_lines(int(tr_.height), size, _lead_bx or None, 0.0)
    if _dl_bx > 1 and "\n" not in copy and "\u2028" not in copy:
        _probe_bx = _dc.replace(cell, rect=BBox(left=int(tr_.left), top=int(tr_.top),
                                                right=int(tr_.right), bottom=int(tr_.bottom)),
                                text_rect=None)
        _hard_bx = _design_line_breaks(_probe_bx, copy, ctx, int(tr_.width))
        if _hard_bx is not None:
            copy = _hard_bx
            shrunk = None
    _single_line = ("\n" not in copy and "\u2028" not in copy)
    _mw = None
    if _single_line:
        _mw = _measure_runs_px(cell.text, copy, ctx)
        if _mw is None:
            _mw = measure_text_px(copy, run.font if run is not None else None, size, registry=ctx.registry)
    _content_sized = bool(_single_line and _mw and _mw >= 0.8 * bw)
    if _content_sized:
        shrunk = None  # the chip hugs the text; nothing to fit
    scale = (shrunk / size) if (shrunk and size) else 1.0
    n_lines = copy.count("\u2028") + copy.count("\n") + 1
    lead = float(getattr(run, "leading", 0) or 0) or size * LINE_HEIGHT_RATIO
    lh = max(int(round(lead * scale)), int(math.ceil(size * scale * 1.05)))
    normalized = (copy or "").replace("\r\n", "\n")
    body = _segment_body_html(cell.text, normalized, ctx, scale=scale)
    if body is None:
        body = _preserve_multispace(html.escape(normalized)).replace("\u2028", "<br/>").replace("\n", "<br/>")
    if href:
        body = (f'<a href="{html.escape(href, quote=True)}" '
                f'style="color:inherit;text-decoration:none;">{body}</a>')
        ctx.links_bound.append({"kind": "slot" if cell.link_slot else "region",
                                "region": region_id, "url": href,
                                "key": cell.link_slot if cell.link_slot else region_id})
    _record_region(ctx, region_id, cell, role, "live", brand_font_rasterized=False, alt=None,
                   design_lines=n_lines, space_after_px=0.0, box=True)
    fs = round(size * scale, 1)
    _ul = "text-decoration:underline;" if (cell.text is not None and cell.text.runs and all(
        getattr(r, "underline", False) for r in cell.text.runs)) else ""
    # DESIGN-ANCHORED placement: paddings carry the text's own offsets inside the shape and the
    # PSD's own alignment rules the lines -- hardcoded center/middle re-centered the disti
    # headline the design sets LEFT at +96px into its band (owner-caught). Bottom padding is
    # the remainder so paddings + line boxes tile the box height exactly (Word paints cell
    # shading over line boxes + padding; height props STACK with padding and are omitted).
    _align_bx = (cell.text.align if (cell.text is not None and cell.text.align) else None) or "left"
    _pad_t = max(0, int(tr_.top) - int(cell.rect.top))
    _pad_l = max(0, int(tr_.left) - int(cell.rect.left))
    _pad_r = max(0, int(cell.rect.right) - int(tr_.right))
    # Keep only the ANCHOR-side inset: the far-side padding squeezes the content box to the
    # ink's exact width and one engine px re-wraps a frozen line (the disti hero rendered a
    # 4th line box and shifted everything below +41px). The fill paints the whole td, so the
    # far-side padding never showed anyway.
    if _align_bx == "left":
        _pad_r = 0
    elif _align_bx == "right":
        _pad_l = 0
    # Anchor the strut block on the design ink's CENTER: line boxes run taller than ink
    # (n*lh vs the ink-tight text_rect), and leaving the surplus below sat the visual group
    # low of the design's position (owner-caught on the centered disti hero).
    if n_lines == 1:
        # Single line: the ink seats on the BASELINE near the line box's bottom (measured
        # identically in Chromium and Word: 8/2 gaps in a chip the design centers 6/6), so
        # center-of-strut under-corrects. ink_top-in-linebox ~= lh - 0.93*fs (descent
        # ~0.21fs below the baseline, cap height ~0.72fs above it).
        _ink_in_box = max(0, int(round(lh - 0.93 * fs)))
        _pad_t = max(0, _pad_t - _ink_in_box)
    else:
        _pad_t = max(0, _pad_t - int(round((n_lines * lh - int(tr_.height)) / 2)))
    _pad_b = max(0, bh - _pad_t - n_lines * lh)
    if n_lines == 1:
        # Single-line label: Word ignores nowrap on a fixed-width padded td (the design pads
        # squeeze the content to ink width and Word's wider glyphs wrapped 'Learn more',
        # owner-caught). The label rides in a width-free nested nowrap td -- the proven
        # button construct -- while the outer td keeps the shape's exact geometry and fill.
        _lbl_style = (f"font-family:{res.css_stack};{res.weight_css}font-size:{fs:g}px;"
                      f"color:{color};line-height:{lh}px;mso-line-height-rule:exactly;"
                      f"white-space:nowrap;{_ul}")
        _tbl_align = "center" if _align_bx == "center" else _align_bx
        _lbl_tbl = ('<table role="presentation" border="0" cellpadding="0" cellspacing="0" '
                    f'align="{_tbl_align}" style="border-collapse:collapse;">'
                    f'<tr><td style="{_lbl_style}">{body}</td></tr></table>')
        outer_style = (f"width:{bw}px;background-color:{fill};"
                       f"padding:{_pad_t}px {_pad_r}px {_pad_b}px {_pad_l}px;")
        inner_td = (f'<td data-region="{html.escape(region_id, quote=True)}" width="{bw}" '
                    f'bgcolor="{fill}" valign="top" style="{outer_style}">{_lbl_tbl}</td>')
        chip_table = ('<table role="presentation" border="0" cellpadding="0" cellspacing="0" '
                      f'style="border-collapse:collapse;"><tr>{inner_td}</tr></table>')
        return _outer_band_td(bw, chip_table, ctx)
    style = (f"width:{bw}px;background-color:{fill};"
             f"padding:{_pad_t}px {_pad_r}px {_pad_b}px {_pad_l}px;"
             f"font-family:{res.css_stack};{res.weight_css}font-size:{fs:g}px;color:{color};"
             f"text-align:{_align_bx};line-height:{lh}px;mso-line-height-rule:exactly;{_ul}")
    _over = max(0, n_lines * lh - bh)
    if _over > getattr(ctx, "row_overhang", 0):
        ctx.row_overhang = _over
    if _content_sized:
        # TEXT-HUGGING chip (the [Company Sign-Off] / [Device/Offer] class): no fixed width --
        # padding carries the design's own side margins and EACH ENGINE wraps its own text, so
        # the fill ends at the brackets in Chromium AND Word (a fixed width hugged only one).
        # The outer td keeps the design rect: the grid never moves (a widened td pushed the
        # sibling chips off their image boxes, owner-caught).
        _ph = max(2, int(round((bw - _mw) / 2)))
        _pv = max(0, int(round((bh - lh) / 2)))
        chip_style = (f"background-color:{fill};padding:{_pv}px {_ph}px;"
                      f"font-family:{res.css_stack};{res.weight_css}font-size:{fs:g}px;color:{color};"
                      f"text-align:center;line-height:{lh}px;mso-line-height-rule:exactly;"
                      f"white-space:nowrap;{_ul}")
        chip = (f'<table role="presentation" border="0" cellpadding="0" cellspacing="0" align="center" '
                f'style="border-collapse:collapse;"><tr>'
                f'<td data-region="{html.escape(region_id, quote=True)}" bgcolor="{fill}" '
                f'style="{chip_style}">{body}</td></tr></table>')
        return _outer_band_td(bw, chip, ctx, align="center")
    inner_td = (f'<td data-region="{html.escape(region_id, quote=True)}" width="{bw}" '
                f'bgcolor="{fill}" valign="top" style="{style}">{body}</td>')
    chip_table = ('<table role="presentation" border="0" cellpadding="0" cellspacing="0" '
                  f'style="border-collapse:collapse;"><tr>{inner_td}</tr></table>')
    return _outer_band_td(bw, chip_table, ctx)


def _sub_highlight_ranges(cell: Cell, normalized: str, ctx: _EmitContext) -> list:
    """Map each substring highlight chip (solver: a fill backing only PART of this text -- the
    merge-token highlights) to a character range of `normalized`. Per line (design leading walk,
    paragraph SpaceAfter honored), a chip claims every word whose measured x-center falls inside
    it; the claimed span snaps to [bracket] boundaries when present. Returns sorted
    [(start, end, hexcolor)] -- color sampled from the design's own pixels at the chip rect."""
    subs = cell.sub_highlights or []
    if not subs or cell.text is None or not normalized:
        return []
    _align = (cell.text.align or "left")
    if _align not in ("left", "center"):
        return []  # right/justify unmapped (none in corpus)
    plain = normalized.replace("\u2028", " ")
    style = _runs_char_style(cell.text, plain)
    if style is None:
        return []
    run = dominant_run(cell.text)
    size = float(run.size) if (run is not None and run.size) else DEFAULT_FONT_SIZE
    lead = float(getattr(run, "leading", 0) or 0) or size * LINE_HEIGHT_RATIO
    sa = [float(pp.get("space_after", 0.0)) for pp in (cell.text.paragraphs or [])]
    lines = []
    start = 0
    seps = []
    for i, ch in enumerate(normalized):
        if ch in ("\u2028", "\n"):
            lines.append((start, i))
            seps.append(ch)
            start = i + 1
    lines.append((start, len(normalized)))
    seps.append("")
    ranges = []
    y = float(cell.rect.top)
    para_i = 0
    for (a0, b0), sep in zip(lines, seps, strict=False):
        line_top, line_bot = y, y + lead
        for hb in subs:
            cy = (hb["top"] + hb["bottom"]) / 2.0
            if not (line_top - 2 <= cy < line_bot + 2):
                continue
            best = None
            _line_x0 = float(cell.rect.left)
            if _align == "center":
                _lw = _measure_span_px(plain, a0, b0, style, ctx)
                if _lw is not None:
                    _line_x0 += max(0.0, (float(cell.rect.width) - _lw) / 2.0)
            for m in re.finditer(r"\S+", normalized[a0:b0]):
                wa, wb = a0 + m.start(), a0 + m.end()
                x0 = _measure_span_px(plain, a0, wa, style, ctx)
                x1 = _measure_span_px(plain, a0, wb, style, ctx)
                if x0 is None or x1 is None:
                    continue
                cx = _line_x0 + (x0 + x1) / 2.0
                if hb["left"] - 3.0 <= cx <= hb["right"] + 3.0:
                    best = (wa if best is None else best[0], wb)
            if best is not None:
                ra, rb = best
                seg = normalized[ra:rb]
                if "[" in seg:
                    ra += seg.index("[") + 1  # bracket stays OUTSIDE the chip (design-measured)
                    seg = normalized[ra:rb]
                if "]" in seg:
                    rb = ra + seg.rindex("]")
                colr = _dominant_color_of_rect(
                    BBox(left=int(hb["left"]), top=int(hb["top"]),
                         right=int(hb["right"]), bottom=int(hb["bottom"])), ctx)
                if colr and colr.upper() not in ("#FFFFFF",):
                    ranges.append((ra, rb, colr))
        y += lead
        if sep == "\n":
            if para_i < len(sa):
                y += sa[para_i]
            para_i += 1
    ranges.sort()
    merged = []
    for r in ranges:
        if merged and r[0] < merged[-1][1]:
            continue  # overlapping claim -- first (leftmost) chip wins
        merged.append(r)
    return merged


def _escape_with_highlights(normalized: str, ranges: list) -> str:
    """html-escape `normalized` (frozen \u2028 -> <br/>, \n -> <br>) with each highlight
    range wrapped in a shaded <span> (probe-certified: arbitrary-hex character shading survives
    the OFT roundtrip and paints in Word). Ranges never straddle a line break (word-scoped)."""
    def esc(t: str) -> str:
        return _preserve_multispace(html.escape(t)).replace("\u2028", "<br/>").replace("\n", "<br>")

    out = []
    pos = 0
    for a, b, colr in ranges:
        if a < pos:
            continue
        out.append(esc(normalized[pos:a]))
        out.append(f'<span style="background:{colr};">{esc(normalized[a:b])}</span>')
        pos = b
    out.append(esc(normalized[pos:]))
    return "".join(out)


def _render_paragraph_blocks(paras, body: str, pre_run, pre_size: float, ctx: "_EmitContext") -> str:
    """Split BODY on Photoshop PARAGRAPH marks (rendered as `<br>`) into real `<p>` blocks, each
    carrying its paragraph's SpaceAfter as margin-bottom; tab-form bullet paragraphs become
    hanging indents with the bullet gap rebuilt from measured glyph widths. Returns BODY unchanged
    when there is nothing to split. Extracted verbatim from `_render_text_leaf` (behaviour-identical)."""
    if not (paras and "<br>" in body):
        return body
    _sas = [pp["space_after"] for pp in paras]
    _pieces = body.split("<br>")
    _out = []
    _hang = 36  # Photoshop's default tab stop -- the designers' bullet gap (measured 37px)
    for _i, _piece in enumerate(_pieces):
        _sa = _sas[_i] if _i < len(_sas) else 0.0
        _mb = f"{_sa:g}px" if (_i < len(_pieces) - 1 and _sa and _sa > 0.5) else "0"
        if _piece == "":
            # A designed BLANK LINE: keep its full line box in BOTH engines (Chromium
            # collapses a truly empty <p> to 0; Word does not -- probe-consistent form).
            _piece = "&nbsp;"
        if _piece.startswith("•" + chr(9)):
            # only the TAB form: space-separated bullets ("•  Infographic") are
            # plain text the normal path already renders design-true (a fixed 36px
            # gap over-wrapped them, gate-caught).
            # BULLET paragraph: the designer fakes the layout with literal tabs (PSD
            # paragraph indents are all 0) -- reproduce it as a REAL hanging indent, with
            # the bullet gap rebuilt from measured glyph widths so both engines land the
            # text at the design's tab stop.
            _bw2 = measure_text_px("•", pre_run.font if pre_run else None,
                                   pre_size, registry=ctx.registry) or 8.0
            _sw2 = measure_text_px(" ", pre_run.font if pre_run else None,
                                   pre_size, registry=ctx.registry) or (pre_size * 0.27)
            _n_nb = max(2, int(round((_hang - _bw2) / max(_sw2, 1.0))))
            _rest = _piece[1:].lstrip(" \t")
            _piece = "•" + ("&nbsp;" * _n_nb) + _rest
            _out.append(
                f'<p style="margin:0 0 {_mb} {_hang}px;text-indent:-{_hang}px;">{_piece}</p>')
            continue
        _out.append(f'<p style="margin:0 0 {_mb} 0;">{_piece}</p>')
    return "".join(_out)


def _render_text_leaf(cell: Cell, region_id: str, role: str, ctx: _EmitContext) -> str:
    if cell.text_rect is not None and cell.background is not None:
        return _render_box_text_leaf(cell, region_id, role, ctx)
    copy = _final_copy_for(cell, ctx.copy_manifest)
    _space_total = _space_after_total(cell, copy)
    href = _href_for(cell, ctx.link_manifest, region_id)
    _dw = getattr(ctx, "current_design_width", None)
    _chromium_w = int(_dw) if (_dw and _dw < int(cell.rect.width)) else int(cell.rect.width)
    # PSD-VERBATIM FLOW first: when the Photoshop wrap simulation reproduces the design's own
    # line count, its breaks are FROZEN into the copy (U+2028 -> <br>) and every engine breaks
    # identically -- no wrap-fit narrowing, no natural-wrap shrink, the design's own type size.
    _hard_copy = _design_line_breaks(cell, copy, ctx, _chromium_w)
    _hl = None
    if _hard_copy is not None:
        _hl = _hard_line_shrink_or_reject(cell, _hard_copy, ctx)
        if _hl == "reject":
            # The freeze itself is wrong (merged profile bands / bad assignment) -- crushing the
            # copy to fit it renders 60%-size text (drip-corpus-caught). Natural wrap instead.
            _hard_copy, _hl = None, None
    _shrunk = ctx.shrink_overrides.get(region_id)
    if _shrunk is None:
        _shrunk = _hl if _hard_copy is not None else _shrink_to_fit_size(cell, copy, ctx)
    _pre_run = dominant_run(cell.text)
    _pre_size = _shrunk if _shrunk is not None else (
        float(_pre_run.size) if (_pre_run is not None and _pre_run.size) else DEFAULT_FONT_SIZE)
    # WRAP-WIDTH FIT: CSS glyphs run slightly narrower than Photoshop's layout of the same face,
    # so at the design width Chromium can wrap a 4-line design paragraph as 3 lines -- ink then
    # stops a full leading above the box bottom (a blank stripe the design doesn't have). The
    # faithful correction narrows the INNER wrap width until the copy wraps to the design's own
    # line count: ink fills the box at natural leading, breaks land where the proof shows them.
    # Word ignores inner block widths (its budget is the carved td), so this is Chromium-only.
    # Skipped when the breaks are frozen -- there is nothing left to fit.
    _fit_w = _chromium_w
    _font = _pre_run.font if _pre_run is not None else None
    _design_lines_est = _est_design_lines(int(cell.rect.height), _pre_size, getattr(_pre_run, 'leading', None) if _pre_run else None, _space_total)
    if _hard_copy is None:
        _sim = _word_budgeted_line_count(copy, _fit_w, font_name=_font, font_size=_pre_size,
                                         registry=ctx.registry, safety=1.0)
        if _sim is not None and _sim < _design_lines_est:
            for _k in range(1, 16):
                _w_try = int(round(_chromium_w * (1.0 - 0.015 * _k)))
                if _w_try < 40:
                    break
                _s2 = _word_budgeted_line_count(copy, _w_try, font_name=_font, font_size=_pre_size,
                                                registry=ctx.registry, safety=1.0)
                if _s2 is not None and _s2 >= _design_lines_est:
                    _fit_w, _sim = _w_try, _s2
                    break
    # The RHYTHM needs the line count at the ACTUAL wrap surface: the pinned div width when the
    # fit loop narrowed, else the full (possibly carved) td -- a count taken at the design width
    # can disagree with what Chromium renders (an em-dash line measured 535 in a 530 design box
    # but wraps fine in its 563 carved td; rhythm at the wrong count squeezed the line-height).
    _wrap_surface = _fit_w if _fit_w < _chromium_w else int(cell.rect.width)
    if _hard_copy is not None:
        _render_lines = _hard_copy.count("\u2028") + _hard_copy.count("\n") + 1
    else:
        _render_lines = _word_budgeted_line_count(copy, _wrap_surface, font_name=_font,
                                                  font_size=_pre_size, registry=ctx.registry, safety=1.0)
    # LINE-HEIGHT FLOOR: mso-line-height-rule:exactly at a line-height below a run's glyph size
    # CLIPS that run vertically in both engines -- the design's own data produces this two ways:
    # a heading with tight fixed leading (24px type, leading 20 -- meaningless for its single
    # line) and a mixed cell whose dominant run is smaller than its display run ("Over 70%": td
    # is 20px-dominant but "70" is a 40px span). Floor the exact line-height at the tallest
    # SCALED run so nothing is ever trimmed; the box only grows (min-height) and the row stacker
    # absorbs the overhang from the following spacer like any other strut-vs-ink difference.
    _dom_design_size = float(_pre_run.size) if (_pre_run is not None and _pre_run.size) else DEFAULT_FONT_SIZE
    _scale = (_shrunk / _dom_design_size) if (_shrunk is not None and _dom_design_size) else 1.0
    _max_run_px = max((float(r.size) for r in (cell.text.runs if cell.text is not None else []) if r.size),
                      default=_pre_size) * _scale
    _lead_for_lh = getattr(_pre_run, "leading", None) if _pre_run else None
    _lh_base = _design_line_height(int(cell.rect.height), _pre_size, _render_lines, leading=_lead_for_lh)
    _lh_final = max(_lh_base, int(math.ceil(max(_pre_size, _max_run_px) * 1.05)))
    _lh_over = ctx.lh_overrides.get(region_id)
    if _lh_over:
        _lh_final = int(_lh_over)
    style = _text_style(cell, ctx, size_override=_shrunk, rendered_lines=_render_lines,
                        line_height_px=_lh_final)
    # OVERHANG ABSORPTION: CSS line boxes are strut-exact (n x line-height) while Photoshop's
    # design boxes are glyph-ink-exact (leading*(n-1) + actual ink), so rendered text runs a few
    # px taller than its box by construction. Record the estimated overhang; the row stacker
    # subtracts it from the FOLLOWING vertical spacer so the difference never cascades down the
    # email (measured +2..+7 per text row compounding to 14px of drift).
    _n_est = _render_lines if _render_lines else _est_design_lines(
        int(cell.rect.height), _pre_size, _lead_for_lh, _space_total)
    _est_h = _n_est * _lh_final + _space_total
    _over = max(0, int(round(_est_h - cell.rect.height)))
    if _over > getattr(ctx, "row_overhang", 0):
        ctx.row_overhang = _over
    normalized = ((_hard_copy if _hard_copy is not None else copy) or "").replace("\r\n", "\n")
    # Per-run segmentation SURVIVES a shrink: every run size scales by the same factor the
    # dominant size shrank by, so mixed sizing and real <sup> aren't flattened away just because
    # the cell needed a proportional fit (the stats digits lost their 2x size exactly this way).
    _bg_ranges = _sub_highlight_ranges(cell, normalized, ctx)
    body = _segment_body_html(cell.text, normalized, ctx, scale=_scale, bg_ranges=_bg_ranges)
    if body is None and _bg_ranges:
        # MERGE-TOKEN HIGHLIGHTS on a uniformly-styled cell (the footer legal block): shaded
        # <span>s at the design's own chip color (probe-certified char shading).
        body = _escape_with_highlights(normalized, _bg_ranges)
    if body is None:
        # Frozen hard breaks (U+2028) render as <br/> -- a token distinct from the "<br>" that
        # marks Photoshop PARAGRAPH ends, so the <p>-block split below never mistakes a frozen
        # line break for a paragraph boundary.
        body = _preserve_multispace(html.escape(normalized)).replace("\u2028", "<br/>").replace("\n", "<br>")
    # PARAGRAPH SPACING (owner-diagnosed): every \n here is a Photoshop PARAGRAPH mark carrying
    # that paragraph's SpaceAfter padding -- the design's inter-paragraph gaps are padding, not
    # blank lines. Rendered as real <p> blocks with margin-bottom (see _render_paragraph_blocks;
    # Word's own native mail dialect uses exactly this). Zero margins everywhere else.
    _paras = cell.text.paragraphs if (cell.text is not None and copy == (cell.text.content or "")) else []
    body = _render_paragraph_blocks(_paras, body, _pre_run, _pre_size, ctx)
    if href:
        body = f'<a href="{html.escape(href, quote=True)}" style="color:inherit;text-decoration:underline;">{body}</a>'
        ctx.links_bound.append({"kind": "slot" if cell.link_slot else "region", "region": region_id, "url": href, "key": cell.link_slot if cell.link_slot else region_id})
    body = _bind_inline_links(body, copy, region_id, ctx)
    # DUAL-WIDTH (see _render_row_tiled + WRAP-WIDTH FIT above): pin the visible text block to
    # the fitted wrap width so Chromium wraps exactly where the proof does; Word ignores inner
    # block widths and keeps the (possibly carved) td as its budget. Pin ONLY when the fit loop
    # actually narrowed -- pinning at the raw design width breaks BORDER-LINE lines (the
    # design's ink can measure a hair wider than its own box, and Chromium's glyphs run ~1-2%
    # wider than PIL's measurement of the same font), wrapping text the design shows on one line.
    if _fit_w < _chromium_w:
        body = f'<div style="width:{int(_fit_w)}px;">{body}</div>'
    # Measure overflow at the run's ACTUAL rendered size, not the theoretical minimum-legible size:
    # a live <td> is emitted at `font-size:{run.size}px` and is never shrunk, so a token that fits
    # at 10px but overflows at its real size is a genuine silent overflow the spec forbids (EARS-209)
    # -- and the raster path already measures at the real size, so measuring live at 10px let the
    # same copy flag on one policy and silently overflow on another.
    _design_size = _dom_design_size
    # The EFFECTIVE rendered size: shrink-to-fit's override when it fired (the <td> really is
    # emitted at that size), else the run's own size. Certifying at the pre-shrink size flags
    # copy the shrunk font actually fits -- a false rejection (caught live on region 2_0).
    _rendered_size = _shrunk if _shrunk is not None else _design_size
    _font_name = _font
    if _has_unbreakable_overflow(copy, cell.rect.width, font_name=_font_name, font_size=_rendered_size, registry=ctx.registry):
        ctx.overflow_flags.append(
            {
                "region": region_id,
                "bounds": cell.rect.to_dict(),
                "reason": "final copy contains a run with no break point wider than the region at its rendered font size",
            }
        )
    # LINE-COUNT CERTIFICATION (Grammar G containment): simulate this copy's wrap at
    # Word-budgeted metrics and assert it holds the DESIGN's line count. Word's wider glyph
    # advances can wrap a line earlier than Chromium shows -- a layout shift the Layout
    # Invariant forbids. Certified here, loudly, at authoring time; never discovered as a
    # shifted email after send.
    if _hard_copy is not None:
        _word_lines = None  # frozen breaks -> the line count is pinned; no wrap cert needed
    else:
        _word_lines = _word_budgeted_line_count(
            copy, cell.rect.width, font_name=_font_name,
            font_size=_rendered_size, registry=ctx.registry)
        if _word_lines is None and _font_name not in ctx.cert_skip_fonts:
            # DEGRADE (S1.1 plainspeak): _word_budgeted_line_count returned None because the font
            # file isn't installed on THIS machine, so we can't measure wrapping to certify the line
            # count -- and that skip was previously silent. Say it in plain language, once per font.
            ctx.cert_skip_fonts.add(_font_name)
            warnings.warn(
                f"The font {_font_name!r} is not installed on this machine, so line wrapping could "
                "not be checked. A long line may wrap differently in Outlook than in the preview -- "
                "check the capture proof.",
                UserWarning, stacklevel=2,
            )
    # design_lines reads the BOX at the design's own size (the box was drawn for that size);
    # the wrap simulation above runs at the effective rendered size.
    _design_lines = _est_design_lines(int(cell.rect.height), _design_size,
                                      getattr(_pre_run, 'leading', None) if _pre_run else None,
                                      _space_total)
    if _word_lines is not None and _word_lines > _design_lines:
        ctx.overflow_flags.append(
            {
                "region": region_id,
                "bounds": cell.rect.to_dict(),
                "reason": (
                    f"line-count certification failed: copy needs {_word_lines} line(s) at "
                    f"Word-budgeted metrics but the design box holds {_design_lines} -- "
                    "shorten the copy or widen the box"
                ),
            }
        )
    _record_region(ctx, region_id, cell, role, "live", brand_font_rasterized=False, alt=None,
                   design_lines=_design_lines, space_after_px=_space_total)
    # WHITE-COPY SAFETY: near-white live text with no backing fill renders INVISIBLE on the white
    # page (the drip corpus's un-slotted CTA label: white copy on a blue shape the solver did not
    # attach -- region rendered blank, gate delta 255). Sample what the design actually paints
    # behind the box; a clearly darker fill becomes the cell's bgcolor. Never guessed: the color
    # comes from the flatten's own pixels, and a light/unknown sample changes nothing.
    _txt_color = (_pre_run.color if (_pre_run is not None and _pre_run.color) else None) or "#000000"
    _luma = _hex_luma(_txt_color)
    if cell.background is None and _luma is not None and _luma > 232:
        _fill = _dominant_color_of_rect(cell.rect, ctx)
        _fill_luma = _hex_luma(_fill) if _fill else None
        if _fill_luma is not None and _fill_luma < 176:
            import dataclasses as _dc

            from .table_tree import Background as _Bg

            cell = _dc.replace(cell, background=_Bg(color=_fill, image_source_layer_id=None))
    return _wrap_td(cell, body, ctx, colspan=cell.colspan, extra_style=style, min_height=True, region_id=region_id)


def _render_cta_leaf(cell: Cell, region_id: str, role: str, ctx: _EmitContext) -> str:
    href = _href_for(cell, ctx.link_manifest, region_id)
    if href:
        ctx.links_bound.append({"kind": "slot" if cell.link_slot else "region", "region": region_id, "url": href, "key": cell.link_slot if cell.link_slot else region_id})
    label = _final_copy_for(cell, ctx.copy_manifest)
    # The bulletproof button's classic nowrap pattern grows with its label (by design) -- but a
    # late-bound label wider than its grid column silently widens the whole fixed table, so an
    # over-long CTA is still an overflow the spec must flag (EARS-209), not clip. nowrap means the
    # ENTIRE label is one unbreakable unit, so estimate its full width at the button's rendered
    # size + horizontal button padding against the column width BEFORE rendering, so the button
    # itself can fall back to a wrapping (still un-clipped, still full-copy) layout instead of
    # bursting the outer tiled table's exact-width invariant (TILING BEHAVIOR) out to the label's
    # full pixel width -- a real short CTA label never trips this, so it never wraps.
    _btn_run = dominant_run(cell.text) if cell.text is not None else None
    _btn_size = float(_btn_run.size) if (_btn_run is not None and _btn_run.size) else DEFAULT_FONT_SIZE
    _btn_font = _btn_run.font if _btn_run is not None else None

    _measured = measure_text_px(label or "", _btn_font, _btn_size, registry=ctx.registry)
    if _measured is None:  # no font file -- documented heuristic fallback
        _measured = len(label or "") * _btn_size * AVG_CHAR_WIDTH_RATIO
    _measured *= WORD_METRIC_SAFETY  # Word's glyph advances run wider than PIL's measurement
    # SHRINK-TO-FIT before wrapping: a single-line CTA label in the design stays single-line --
    # if our metrics run a few px wide, a small proportional size reduction is the faithful
    # correction (same rule as _shrink_to_fit_size for plain text); wrap only when even the
    # legible floor can't fit it.
    _size_override = None
    _avail = max(1, int(cell.rect.width) - 2 * DEFAULT_BUTTON_PADDING_H)
    if label and _measured > _avail:
        _fit = _btn_size * (_avail / _measured) * 0.98
        if _fit >= MIN_LEGIBLE_FONT_SIZE:
            _size_override = round(_fit, 1)
    _est_label_w = (_measured if _size_override is None else _measured * (_size_override / _btn_size)) + 2 * DEFAULT_BUTTON_PADDING_H
    _overflows = bool(label) and _est_label_w > cell.rect.width
    if _overflows:
        ctx.overflow_flags.append(
            {
                "region": region_id,
                "bounds": cell.rect.to_dict(),
                "reason": "CTA label is wider than its grid column at its rendered size; the nowrap button would widen the table",
            }
        )
    button_html = _render_button(cell, label, href, ctx, wrap=_overflows, size_override=_size_override)
    _record_region(ctx, region_id, cell, role, "live", brand_font_rasterized=False, alt=None)
    return _wrap_td(cell, button_html, ctx, colspan=cell.colspan, extra_style="padding:0;", min_height=True, region_id=region_id)


def _render_raster_leaf(cell: Cell, region_id: str, role: str, ctx: _EmitContext, *, kind: str) -> str:
    href = _href_for(cell, ctx.link_manifest, region_id)
    # DIVIDER RULE: a hairline-thin, very wide image (the classifier's divider gate shape) ships
    # as a flat color-fill td, never an <img> -- Word's image top-clip reduced the reseller's
    # 5px stat divider to a ghost sliver (owner-caught; the hand-built OFT draws its rules as
    # CSS borders for the same reason). The probe-certified band construct paints it exactly.
    _dh = int(cell.rect.height)
    _dw = int(cell.rect.width)
    if kind != "brand_headline" and 0 < _dh <= 8 and _dw >= 25 * _dh:
        _dc_color = _divider_ink_color(cell.rect, ctx) or "#D9D9D9"
        # ink thickness from the flatten (the rect includes AA headroom; a solid rect-height
        # bar over-painted the design's hairline, gate-caught). Border = the human OFT's own
        # rule construct -- no <img>, so Word's image top-clip can't ghost it.
        _ink_h = 1
        _prof = _flatten_line_profile(cell.rect, ctx)
        if _prof:
            _ink_h = max(1, min(3, int(round(_prof[0][1] - _prof[0][0]))))
        _record_region(ctx, region_id, cell, role, "raster", brand_font_rasterized=False,
                       alt=_alt_fallback(cell))
        _pad_top = max(0, (_dh - _ink_h) // 2)
        return (f'<td data-region="{html.escape(region_id, quote=True)}" width="{_dw}" '
                f'height="{_dh}" valign="top" '
                f'style="width:{_dw}px;height:{_dh}px;font-size:0;line-height:0;'
                f'padding-top:{_pad_top}px;border-bottom:{_ink_h}px solid {_dc_color};'
                f'mso-border-bottom-alt:solid {_dc_color} {_ink_h}px;">&nbsp;</td>')
    result = _rasterize_or_warn(cell, ctx, kind=kind, region_id=region_id)
    if result is None:
        alt = _alt_fallback(cell)
        _record_region(
            ctx, region_id, cell, role, "raster", brand_font_rasterized=False, alt=alt, warning="rasterize_failed"
        )
        placeholder = f'<span style="display:block;font-size:12px;color:#999999;">[{html.escape(alt)}]</span>'
        return _wrap_td(cell, placeholder, ctx, colspan=cell.colspan, extra_style="padding:4px;", min_height=True, region_id=region_id)

    if kind == "brand_headline" and result.overflow:
        ctx.overflow_flags.append(
            {
                "region": region_id,
                "bounds": cell.rect.to_dict(),
                "reason": "rasterized headline copy overflowed its minimum-height envelope",
            }
        )
    img_html = _img_tag(result, ctx.density)
    if href:
        img_html = f'<a href="{html.escape(href, quote=True)}">{img_html}</a>'
        ctx.links_bound.append({"kind": "slot" if cell.link_slot else "region", "region": region_id, "url": href, "key": cell.link_slot if cell.link_slot else region_id})
    _rb = int(getattr(ctx, "last_raster_bleed", 0) or 0)
    if _rb:
        # Word clips the img to the td's height box (measured: visible ink rows ==
        # td_height - 1 for every bleed tried): the td must span the BLED height or the
        # sacrificial top rows push real ink out of the window. The intra-row offset was
        # already reduced by the same amount, so the total cell footprint is unchanged.
        import dataclasses as _dc

        cell = _dc.replace(cell, rect=BBox(
            left=int(cell.rect.left), top=int(cell.rect.top) - _rb,
            right=int(cell.rect.right), bottom=int(cell.rect.bottom)))
    # (A nested-table anchor was tried here against Word's baseline pull and REVERTED:
    # Word renders a leading paragraph before a table at cell start, which pushed the hero
    # photo ~50px down -- worse than the pull it was meant to fix. The bare img td with the
    # backdrop-matched bleed is the correct form; measured flush in v5/v15.)
    # OVERHANG ABSORPTION: a re-typeset raster taller than its design box (never clipped, per
    # EARS-209) pushes rows below -- record the excess so the stacker's next spacer absorbs it.
    try:
        _actual_h = int(round(result.height / max(ctx.density, 1e-6))) if hasattr(result, "height") else 0
        _rover = max(0, _actual_h - int(cell.rect.height))
        if _rover > getattr(ctx, "row_overhang", 0):
            ctx.row_overhang = _rover
    except Exception:
        pass
    brand_font_rasterized = bool(kind == "brand_headline" and not result.degraded_brand_font)
    _record_region(ctx, region_id, cell, role, "raster", brand_font_rasterized=brand_font_rasterized, alt=result.alt)
    return _wrap_td(cell, img_html, ctx, colspan=cell.colspan, extra_style="padding:0;line-height:0;font-size:0;", region_id=region_id)


def _render_cell(cell: Cell, key: tuple, routed: RoutedTree, ctx: _EmitContext) -> str:
    if cell.role == "rows":
        # A CONTAINER cell recurses through the SAME stack+tile logic (TILING BEHAVIOR item 5),
        # scoped to this cell's own rect -- its nested rows tile [rect.left, rect.left+rect.width)
        # horizontally and stack vertically from rect.top, exactly like the top-level tree.
        # A SHADED container (the hero's grey column) is a band: its nested spacer rows/gap
        # cells must paint the band color (probe-certified; Word stripes them white otherwise).
        _attr, _ = _background_style_props(cell.background, cell.rect, ctx, "cellbg")
        _m = _RE_BGCOLOR_HEX.search(_attr or "")
        _cell_band = _m.group(1) if (_m and _m.group(1).upper() not in ("#FFFFFF", "#FEFEFE")) else None
        _prev = getattr(ctx, "current_band_color", None)
        ctx.current_band_color = _cell_band or _prev
        # Scope the OVERHANG ABSORPTION cursors to this container: the nested _render_stacked_rows
        # overwrites row_overhang/pending_overhang for its OWN inner rows, so snapshot + restore
        # them around the recursion -- a plain leaf still writes row_overhang live; only this
        # role=="rows" recursion is isolated, so its inner overhang can't leak into the enclosing
        # row's absorption (the leaked-container-overhang drift).
        _prev_overhang = getattr(ctx, "row_overhang", 0)
        _prev_pending = getattr(ctx, "pending_overhang", 0)
        try:
            inner_rows_html = _render_stacked_rows(
                cell.rows or [], cell.rect.top, cell.rect.left, cell.rect.width, key + ("rows",), routed, ctx
            )
            # INTRA-ROW OFFSET AS CONTENT, NOT PADDING (human-OFT form): Word couples every
            # sibling cell's content top to a padded container cell's content top -- the hero
            # photo rendered exactly padding-top (52px) below its band because the text column
            # carried that padding (owner-caught; the hand-built OFT spaces with content and
            # its photo sits flush). The offset becomes a leading spacer ROW inside the stack.
            _off = max(0, int(getattr(ctx, "current_top_offset", 0)))
            if _off:
                inner_rows_html = _spacer_row(
                    int(cell.rect.width), _off,
                    band_color=getattr(ctx, "current_band_color", None)) + inner_rows_html
        finally:
            ctx.current_band_color = _prev
            ctx.row_overhang = _prev_overhang
            ctx.pending_overhang = _prev_pending
        inner_table = _wrap_table(inner_rows_html, cell.rect.width)
        _prev_off2 = getattr(ctx, "current_top_offset", 0)
        ctx.current_top_offset = 0
        try:
            return _wrap_td(cell, inner_table, ctx, colspan=cell.colspan, extra_style="padding:0;")
        finally:
            ctx.current_top_offset = _prev_off2

    verb = routed.verbs[key]
    role = render_role(cell)
    region_id = "_".join(str(p) for p in key)

    if role in (ROLE_MERGE, ROLE_BODY) or (role == ROLE_BRAND_HEADLINE and verb == "live"):
        return _render_text_leaf(cell, region_id, role, ctx)
    if role == ROLE_CTA:
        return _render_cta_leaf(cell, region_id, role, ctx)
    if role == ROLE_BRAND_HEADLINE:
        return _render_raster_leaf(cell, region_id, role, ctx, kind="brand_headline")
    if role in (ROLE_IMAGE, ROLE_GRAPHIC):
        return _render_raster_leaf(cell, region_id, role, ctx, kind="image")

    raise HtmlEmitterError(f"html_emitter: unexpected leaf role {role!r} at key {key}")  # pragma: no cover


def _write_bundle_manifest(out_root: Path, entry_relpath: str, asset_relpaths: list) -> dict:
    """Write `_bundle_manifest.json` -- same field names as the downstream consumer
    (`bundle_intake.unpack_and_verify_bundle`): `schema_version`, `entry_html`, `assets`,
    `bundle_hash`. `bundle_hash` is in the same STABLE-CONTENT-HASH spirit (independent of mtime,
    ordering) but computed with plain sha256 over file bytes -- this package has no dependency on
    the downstream service's QuickXorHash implementation."""
    relpaths = sorted({entry_relpath, *asset_relpaths})
    digest = hashlib.sha256()
    for relpath in relpaths:
        file_hash = hashlib.sha256((out_root / relpath).read_bytes()).hexdigest()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("utf-8"))
        digest.update(b"\n")
    manifest = {
        "schema_version": 1,
        "entry_html": entry_relpath,
        "assets": sorted(set(asset_relpaths)),
        "bundle_hash": digest.hexdigest(),
    }
    (out_root / "_bundle_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


# --- density / retina scaling -------------------------------------------------------------------
#
# An email lays out at ~640 CSS px, but a PSD may be AUTHORED at N x that (e.g. 1280 = 2x) so image
# assets ship crisp on high-resolution screens. `scale_routed` divides all LAYOUT geometry (rects +
# text run sizes) by the density up front, producing a CSS-space tree the (unchanged) tiling logic
# then renders; the rasterizer is separately told the density (`source_scale`) so it samples image
# crops / resvg headlines at the FULL (d x) resolution, and `_img_tag` displays those PNGs at
# CSS = pixels / density. density == 1.0 is a strict no-op (round(x/1.0) == x, size/1.0 == size),
# so emit() skips scaling entirely then and output is byte-identical.


def _scale_bbox(b: BBox, d: float) -> BBox:
    left, top = round(b.left / d), round(b.top / d)
    right, bottom = round(b.right / d), round(b.bottom / d)
    return BBox(left=left, top=top, right=max(left, right), bottom=max(top, bottom))


def _scale_highlight(h: dict, d: float) -> dict:
    """Scale a sub_highlight bbox dict (design-coord left/top/right/bottom) to CSS px the SAME way
    _scale_bbox scales a rect -- rounded edges, clamped so right>=left / bottom>=top -- preserving
    any other keys unchanged."""
    left, top = round(h["left"] / d), round(h["top"] / d)
    right, bottom = round(h["right"] / d), round(h["bottom"] / d)
    return {**h, "left": left, "top": top, "right": max(left, right), "bottom": max(top, bottom)}


def _scale_textinfo(t: Optional[TextInfo], d: float) -> Optional[TextInfo]:
    if t is None:
        return None
    # Carry EVERY run field through unchanged except the px-valued ones (size, leading), which
    # scale by /d exactly like geometry. Dropping length/baseline/underline (the old bug) made
    # _runs_char_style/_segment_body_html bail on the first `length is None`, silently flattening
    # superscript/subscript and underline at any density != 1.0.
    runs = [
        TextRun(
            font=r.font,
            size=(r.size / d if r.size else r.size),
            color=r.color,
            length=r.length,
            baseline=r.baseline,
            leading=(r.leading / d if r.leading else r.leading),
            underline=r.underline,
        )
        for r in (t.runs or [])
    ]
    # Paragraphs carry per-paragraph SpaceAfter in px -- scale it too (char `length` is a count,
    # not px, so it is left alone), or every inter-paragraph gap renders at PSD scale in the
    # emitted CSS-px output.
    paragraphs = []
    for p in (t.paragraphs or []):
        q = dict(p)
        if q.get("space_after"):
            q["space_after"] = q["space_after"] / d
        paragraphs.append(q)
    return TextInfo(content=t.content, align=t.align, runs=runs, paragraphs=paragraphs)


def _scale_cell(c: Cell, d: float) -> Cell:
    return Cell(
        role=c.role,
        rect=_scale_bbox(c.rect, d),
        background=c.background,  # color / source-layer-id only -- no geometry to scale here
        editable=c.editable,
        link_slot=c.link_slot,
        colspan=c.colspan,
        text=_scale_textinfo(c.text, d),
        image_source_layer_ids=list(c.image_source_layer_ids) if c.image_source_layer_ids else c.image_source_layer_ids,
        rows=[_scale_row(r, d) for r in c.rows] if c.rows else c.rows,
        source_layer_id=c.source_layer_id,
        swallowed_editable_layer_ids=c.swallowed_editable_layer_ids,
        baked_text_layer_ids=c.baked_text_layer_ids,
        text_rect=_scale_bbox(c.text_rect, d) if c.text_rect is not None else None,
        sub_highlights=[_scale_highlight(h, d) for h in c.sub_highlights] if c.sub_highlights is not None else None,
    )


def _scale_row(r: Row, d: float) -> Row:
    return Row(
        background=r.background,
        cells=[_scale_cell(c, d) for c in r.cells],
        rect=_scale_bbox(r.rect, d) if getattr(r, "rect", None) is not None else None,
    )


def scale_routed(routed: RoutedTree, d: float) -> RoutedTree:
    """Return a copy of `routed` with all geometry scaled to CSS px (= PSD px / d): rects rebuilt
    from rounded edges, text run sizes divided. Routing verbs are preserved unchanged -- their keys
    are structural path indices, unaffected by scaling, and `render_role` never depends on geometry,
    so a scaled cell classifies identically. Never mutates the input tree."""
    t = routed.tree
    scaled = TableTree(email=t.email, width=round(t.width / d), rows=[_scale_row(r, d) for r in t.rows])
    return RoutedTree(policy=routed.policy, tree=scaled, verbs=routed.verbs)


def emit(
    routed: RoutedTree,
    out_dir,
    copy_manifest: Optional[Mapping] = None,
    link_manifest: Optional[Mapping] = None,
    *,
    composite: Any = None,
    layer_names: Optional[Mapping] = None,
    registry: Optional[Mapping] = None,
    assets_subdir: str = "assets",
    psd_path: Optional[str] = None,
    density: float = 1.0,
) -> dict:
    """Render `routed` (a `layer_router.RoutedTree`) into an OFT-safe bundle under `out_dir`.

    `copy_manifest`/`link_manifest` are the late-copy-binding manifests (see module docstring).
    `composite` (a Pillow image from `rasterizer.composite_psd()`) and `layer_names` are only needed
    if the tree has any image/graphic leaves or a Background image -- omit them to still get a
    valid bundle with loud-but-safe placeholders in `warnings` for those regions.

    Returns `{index_path, assets, regions, overflow_flags, warnings, bundle_manifest, links}`,
    where `links` is the reconciliation report `{bound, unbound}` (tests/test_links.py and
    cli.py's `_cmd_emit` both read it).
    """
    if not isinstance(routed, RoutedTree):
        raise HtmlEmitterError(f"html_emitter.emit: expected a layer_router.RoutedTree, got {type(routed)!r}")

    # Density scaling: divide the layout geometry down to CSS px up front (a strict no-op at 1.0),
    # then render + rasterize at full resolution via ctx.density (see scale_routed / _img_tag).
    density = float(density) if density else 1.0
    if density != 1.0:
        routed = scale_routed(routed, density)

    tree = routed.tree
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    assets_dir = out_root / assets_subdir

    # A per-layer index lets a Background be composited from ITS OWN layer alone (no foreground text
    # baked in). Built once from psd_path when supplied; without it, backgrounds fall back to the
    # legacy flatten crop (which can double the text -- see _crop_background_image).
    layer_index = None
    _emit_warnings: list = []
    if psd_path is not None:
        li = open_layer_index(psd_path)
        if li.get("available"):
            layer_index = li["index"]
        else:
            _emit_warnings.append({"type": "layer_index_unavailable", "reason": li.get("reason")})

    ctx = _EmitContext(
        out_root=out_root,
        assets_dir=assets_dir,
        assets_subdir=assets_subdir,
        copy_manifest=copy_manifest,
        link_manifest=link_manifest,
        composite=composite,
        layer_names=layer_names,
        registry=registry,
        layer_index=layer_index,
        density=density,
    )
    ctx.warnings.extend(_emit_warnings)

    # TILING BEHAVIOR item 1: the top-level stack starts at canvas (0, 0), spanning tree.width --
    # a leading spacer row absorbs any top margin, and every top-level Row becomes its own
    # full-width single-column <tr> (see _render_stacked_rows/_render_row_section above), so the
    # outer table is single-column and table-layout:fixed can never collide across rows of
    # different shapes.
    _harmonize_shrink(routed, ctx)
    rows_html = _render_stacked_rows(tree.rows, 0, 0, tree.width, (), routed, ctx)
    body_table = _wrap_table(rows_html, tree.width)
    doc = _wrap_document(tree.email, body_table)

    index_path = out_root / "index.html"
    index_path.write_text(doc, encoding="utf-8")

    asset_relpaths = sorted(set(ctx.assets))
    manifest = _write_bundle_manifest(out_root, "index.html", asset_relpaths)

    regions_path = out_root / "regions.json"
    regions_path.write_text(json.dumps(ctx.regions, indent=2), encoding="utf-8")

    # LINK RECONCILIATION: every URL the manifest promised must have bound somewhere, or the
    # bundle ships a silently dead link -- each miss is a loud warning AND lands in
    # links_report.json so the pipeline's link-verify stage fails deterministically.
    _slots, _regions_map, _inline = _link_sections(link_manifest)
    promised = ([("slot", k, v) for k, v in _slots.items()]
                + [("region", k, v) for k, v in _regions_map.items()]
                + [("inline", e.get("match", ""), e.get("url")) for e in _inline])
    # Reconcile per (kind, key) against what actually bound -- NOT by url value: two promises
    # sharing one url (two slots pointing at the same href) must not mask each other, and a
    # promise whose url is missing/empty can never bind, so it too is reported unbound rather
    # than silently dropped. A bound record carries its (kind, key): slot -> slot name, region ->
    # region id, inline -> the matched visible text.
    bound_keys = {(b["kind"], b["match"] if b["kind"] == "inline" else b["key"]) for b in ctx.links_bound}
    unbound = [{"kind": k, "key": key, "url": url} for k, key, url in promised
               if (k, key) not in bound_keys]
    for miss in unbound:
        # Distinguish a blocked unsafe-scheme URL from an ordinary no-match miss so the loud
        # failure names WHY (both still stop the pipeline via link-verify).
        if miss.get("url") and not _is_safe_href(miss["url"]):
            ctx.warnings.append({"type": "link_unbound", **miss,
                                 "reason": "unsafe URL scheme blocked -- only http/https/mailto/tel "
                                           "or relative links are emitted"})
        else:
            ctx.warnings.append({"type": "link_unbound", **miss,
                                 "reason": "manifest URL never bound to any region/slot/inline match"})
    links_report = {"bound": ctx.links_bound, "unbound": unbound}
    (out_root / "links_report.json").write_text(json.dumps(links_report, indent=2), encoding="utf-8")

    return {
        "index_path": str(index_path),
        "assets": asset_relpaths,
        "regions": ctx.regions,
        "overflow_flags": ctx.overflow_flags,
        "warnings": ctx.warnings,
        "bundle_manifest": manifest,
        "links": links_report,
    }
