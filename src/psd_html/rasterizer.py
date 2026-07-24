"""A raster-routed leaf `Cell` -> real PNG bytes on disk.

Two backends, selected by the cell's `layer_router.render_role()`:

  - "brand_headline" (a re-typeset text region, EARS-208): `text_raster_adapter.build_headline_svg`
    builds the SVG, then resvg (`resvg_py.svg_to_bytes`) rasterizes it with the brand font's actual
    files embedded via `font_files=` -- the brand-font raster GUARANTEE. Text is NEVER pixel-
    cropped from the PSD (that would bake in stale sample copy).
  - "image" / "graphic" (non-text pixels): the cell's own `rect` is cropped straight out of a
    once-composited PSD raster (Pillow). `rect` IS the union bbox `table_solver` already computed
    for this cell's source layer(s) -- re-deriving a union from `image_source_layer_ids` would just
    reproduce the same box with more code and a chance of drifting from what the emitter actually
    lays the `<td>` out as, so this module trusts `rect` directly.

External-tool degrade rule: resvg and Pillow are BOTH verified installed in this environment (see
build context), so the tests here exercise the real paths. But every external call is still
guarded -- if resvg raises or was unimportable, `rasterize_brand_headline` degrades to a Pillow
`ImageFont` text render (still produces a PNG) and sets `degraded_brand_font=True` plus a
`warning` dict; it never crashes the whole bake-off over one region. If NEITHER backend is usable
(Pillow also missing) it raises `RasterizerUnavailable` -- loud, not a silent skip. Missing Pillow
for the image-crop backend has no fallback (there is nothing left to crop with) and also raises
`RasterizerUnavailable`.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .font_resolver import font_files_for
from .layer_router import ROLE_BRAND_HEADLINE, ROLE_GRAPHIC, ROLE_IMAGE, render_role
from .table_tree import Cell
from .text_raster_adapter import RasterText, build_headline_svg, dominant_font_name

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

try:
    import resvg_py as _resvg

    _RESVG_AVAILABLE = True
    _RESVG_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # pragma: no cover -- resvg_py is installed in this environment
    _resvg = None
    _RESVG_AVAILABLE = False
    _RESVG_IMPORT_ERROR = _exc

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
    _PIL_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # pragma: no cover -- Pillow is installed in this environment
    Image = ImageDraw = ImageFont = None
    _PIL_AVAILABLE = False
    _PIL_IMPORT_ERROR = _exc


class RasterizerUnavailable(RuntimeError):
    """Raised only when EVERY backend for a required render path is unavailable (resvg AND
    Pillow both missing for a text raster, or Pillow missing for an image crop). Loud-but-safe:
    a caller doing a whole-bundle bake-off can catch this per-region and keep going, but it is
    never swallowed inside this module."""


@dataclass(frozen=True)
class RasterResult:
    """What every rasterize_* function returns. `relpath` is bundle-root-relative, forward-slash
    (POSIX) style, matching the downstream bundle manifest convention (see
    `Reference/Creative QA System/Service/bundle_intake.py`)."""

    relpath: str
    alt: str
    width: int
    height: int
    backend: str  # "resvg" | "pillow-text-fallback" | "pillow-crop"
    degraded_brand_font: bool = False
    overflow: bool = False
    warning: Optional[dict] = None


# --- shared helpers -------------------------------------------------------------------------


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _ensure_assets_dir(bundle_root, assets_subdir: str) -> Path:
    assets_dir = Path(bundle_root) / assets_subdir
    _ensure_dir(assets_dir)
    return assets_dir


def _assets_relpath(assets_subdir: str, filename: str) -> str:
    sub = (assets_subdir or "").strip("/")
    return f"{sub}/{filename}" if sub else filename


def _stable_name(*parts, ext: str) -> str:
    """A deterministic filename from identifying parts (layer ids / rect / role) -- same cell,
    same name, every run. Callers with a naming scheme of their own should pass `filename=`
    explicitly; this is only the no-collision-by-construction default."""
    key = "|".join(str(p) for p in parts if p is not None)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.{ext}"


def _png_dims(data: bytes) -> tuple:
    """Parse width/height straight out of the PNG IHDR chunk (fixed offset in every valid PNG) --
    also doubles as the "is this really a PNG" assertion the build spec calls for, without
    depending on Pillow just to verify magic bytes."""
    if not data or not data.startswith(PNG_MAGIC):
        raise ValueError("rasterizer: produced bytes are not a valid PNG (bad/missing magic)")
    if len(data) < 24:
        raise ValueError("rasterizer: PNG bytes truncated before IHDR")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _hex_to_rgba(color: Optional[str]) -> tuple:
    if not color or not color.startswith("#") or len(color) not in (7, 9):
        return (0, 0, 0, 255)
    try:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        a = int(color[7:9], 16) if len(color) == 9 else 255
        return (r, g, b, a)
    except ValueError:
        return (0, 0, 0, 255)


def _alt_for_text_cell(cell: Cell) -> str:
    if cell.text is not None and cell.text.content and cell.text.content.strip():
        return cell.text.content.strip()
    if cell.source_layer_id is not None:
        return f"text layer #{cell.source_layer_id}"
    return "text region"


def _alt_for_image_cell(cell: Cell, layer_names: Optional[Mapping]) -> str:
    names = layer_names or {}
    ids = list(cell.image_source_layer_ids or [])
    if cell.source_layer_id is not None:
        ids = [cell.source_layer_id] + ids
    for lid in ids:
        name = names.get(lid)
        if name:
            return name
    if ids:
        return f"image region (layer #{ids[0]})"
    rect = cell.rect.to_dict() if cell.rect is not None else None
    return f"{cell.role} region rect={rect}"


# --- backend: resvg / Pillow text fallback --------------------------------------------------


def _pillow_text_fallback(raster_text: RasterText, scale: float = 1.0) -> bytes:
    if not _PIL_AVAILABLE:  # pragma: no cover -- guarded by caller, kept for direct-call safety
        raise RasterizerUnavailable(f"Pillow unavailable for text fallback: {_PIL_IMPORT_ERROR!r}")
    # `scale` mirrors the resvg `zoom` path: the RasterText is authored at 1x (CSS px) and the
    # emitter divides the raster's pixel dims by density downstream, so density > 1 must scale the
    # WHOLE coordinate system -- canvas, font size, pen positions -- up here too, else the fallback
    # headline lands at 1/scale of its correct size. Strict no-op at 1.0.
    s = float(scale) if scale else 1.0
    canvas_w = max(1, int(round(raster_text.width * s)))
    canvas_h = max(1, int(round(raster_text.height * s)))
    img = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", max(1, int(round(raster_text.font_size * s))))
    except Exception:
        font = ImageFont.load_default()
    fill = _hex_to_rgba(raster_text.color)
    for i, line in enumerate(raster_text.lines):
        y = (raster_text.padding + i * raster_text.line_height) * s
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            text_w = bbox[2] - bbox[0]
        except Exception:
            text_w = 0
        if raster_text.align == "center":
            x = (raster_text.width * s - text_w) / 2.0
        elif raster_text.align == "right":
            x = raster_text.width * s - raster_text.padding * s - text_w
        else:
            x = raster_text.padding * s
        draw.text((x, y), line, font=font, fill=fill)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_svg_to_png(raster_text: RasterText, *, font_name: Optional[str], registry, scale: float = 1.0) -> tuple:
    """Returns (png_bytes, backend, degraded_brand_font, warning). `scale` > 1 renders the SVG at
    that zoom so the PNG carries `scale` x the pixels (retina) -- the emitter then displays it at
    1/scale. `scale == 1.0` is byte-identical to before (no `zoom` passed)."""
    files = font_files_for(font_name, registry=registry)
    brand_mandatory = raster_text.font_resolution.brand_mandatory

    fallback_reason: Optional[str] = None
    if _RESVG_AVAILABLE:
        try:
            _resvg_kwargs = {"svg_string": raster_text.svg, "font_files": list(files)}
            if scale and float(scale) != 1.0:
                _resvg_kwargs["zoom"] = float(scale)
            data = _resvg.svg_to_bytes(**_resvg_kwargs)
            png_bytes = bytes(data)
            degraded = bool(brand_mandatory and not files)
            warning = None
            if degraded:
                warning = {
                    "type": "brand_font_files_unavailable",
                    "message": (
                        f"resvg rendered {font_name!r} with no embedded font_files discovered on this "
                        "machine -- the brand-font raster guarantee is degraded to resvg's system-font "
                        "fallback."
                    ),
                    "font_name": font_name,
                }
            return png_bytes, "resvg", degraded, warning
        except Exception as exc:
            fallback_reason = f"resvg_py.svg_to_bytes raised {exc!r}"
    else:
        fallback_reason = f"resvg_py unavailable at import time: {_RESVG_IMPORT_ERROR!r}"

    if not _PIL_AVAILABLE:
        raise RasterizerUnavailable(
            f"rasterize_brand_headline: resvg failed ({fallback_reason}) AND Pillow is unavailable "
            f"({_PIL_IMPORT_ERROR!r}) -- no backend left to render this brand headline."
        )
    png_bytes = _pillow_text_fallback(raster_text, scale)
    warning = {
        "type": "resvg_unavailable",
        "message": f"degraded to Pillow text fallback ({fallback_reason}) -- brand font is NOT guaranteed embedded.",
        "font_name": font_name,
    }
    return png_bytes, "pillow-text-fallback", True, warning


def rasterize_brand_headline(
    cell: Cell,
    bundle_root,
    *,
    final_copy: Optional[str] = None,
    assets_subdir: str = "assets",
    filename: Optional[str] = None,
    alt: Optional[str] = None,
    registry=None,
    source_scale: float = 1.0,
) -> RasterResult:
    """Rasterize one brand-headline text `Cell` (EARS-208 re-typeset path) into `bundle_root/
    assets_subdir/`. `final_copy` is the late-bound, authoritative string; if omitted, the PSD
    SAMPLE copy (`cell.text.content`) is used as a fallback and a `sample_copy_used` warning is
    attached -- this is meant for previewing before a copy manifest exists, never for shipping."""
    if cell.role != "text" or cell.text is None:
        raise ValueError(f"rasterize_brand_headline: expects a text Cell with .text set, got role={cell.role!r}")

    copy_warning = None
    copy = final_copy
    if copy is None:
        copy = cell.text.content
        copy_warning = {
            "type": "sample_copy_used",
            "message": (
                "rasterize_brand_headline: no final_copy supplied -- rasterized the PSD SAMPLE copy, "
                "which is non-authoritative per the late-copy-binding rule. Bind a copy manifest entry "
                "before shipping this bundle."
            ),
            "source_layer_id": cell.source_layer_id,
        }

    width = int(cell.rect.width)
    min_height = int(cell.rect.height)
    # max_height == the design box: the re-typeset block SHRINKS-TO-BOX rather than growing past
    # it (growth pushes every row below down -- the reflow drift the Layout Invariant forbids).
    raster_text = build_headline_svg(cell.text, width, copy, min_height=min_height,
                                     max_height=min_height, registry=registry)

    font_name = dominant_font_name(cell.text)
    png_bytes, backend, degraded, render_warning = _render_svg_to_png(
        raster_text, font_name=font_name, registry=registry, scale=source_scale
    )
    actual_w, actual_h = _png_dims(png_bytes)

    assets_dir = _ensure_assets_dir(bundle_root, assets_subdir)
    fname = filename or _stable_name("headline", cell.source_layer_id, width, min_height, ext="png")
    (assets_dir / fname).write_bytes(png_bytes)

    return RasterResult(
        relpath=_assets_relpath(assets_subdir, fname),
        alt=alt or _alt_for_text_cell(cell),
        width=actual_w,
        height=actual_h,
        backend=backend,
        degraded_brand_font=degraded,
        overflow=raster_text.overflow,
        warning=render_warning or copy_warning,
    )


# --- backend: PSD composite crop (image / graphic cells) ------------------------------------


def composite_psd(psd_path, *, color=1.0, alpha: bool = False):
    """Composite a whole PSD to one Pillow RGBA image, ONCE -- callers should cache/reuse this
    across every image/graphic cell in the same PSD rather than re-opening per cell. Loud-but-safe
    degrade: on ANY failure (psd-tools missing, corrupt file, compositing error) this returns a
    `{"available": False, "reason": ...}` dict instead of raising -- `rasterize_image_cell` checks
    for exactly that shape and raises `RasterizerUnavailable` naming the reason."""
    try:
        from psd_tools import PSDImage
    except Exception as exc:  # pragma: no cover -- psd-tools is installed in this environment
        return {"available": False, "reason": f"psd-tools import failed: {exc!r}"}
    try:
        psd = PSDImage.open(os.path.abspath(str(psd_path)))
        image = psd.composite(color=color, alpha=alpha)
        if image is None:
            return {"available": False, "reason": "PSDImage.composite() returned None"}
        return image.convert("RGBA")
    except Exception as exc:
        return {"available": False, "reason": f"PSDImage composite failed: {exc!r}"}


def open_layer_index(psd_path):
    """Open a PSD ONCE and return `{"available": True, "index": {layer_id: layer}}` (or
    `{"available": False, "reason": ...}` on any failure -- loud-but-safe, never raises).

    Used so a Background's `image_source_layer_id` can be composited from ITS OWN layer alone,
    instead of cropping the flattened whole-PSD composite (which bakes in the foreground text/content
    sitting on top of the band or highlight -- the "doubled text" defect). Layers whose
    `layer_id` is missing/None (not int-convertible) are skipped -- their backgrounds fall back to the
    legacy flatten crop, documented at the call site."""
    try:
        from psd_tools import PSDImage
    except Exception as exc:  # pragma: no cover -- psd-tools is installed in this environment
        return {"available": False, "reason": f"psd-tools import failed: {exc!r}"}
    try:
        psd = PSDImage.open(os.path.abspath(str(psd_path)))
        index = {}
        for layer in psd.descendants():
            try:
                index[int(layer.layer_id)] = layer
            except Exception:
                continue
        return {"available": True, "index": index}
    except Exception as exc:
        return {"available": False, "reason": f"open_layer_index failed: {exc!r}"}


def composite_layer(layer, viewport):
    """Composite ONE layer within a canvas-coordinate `viewport` box `(left, top, right, bottom)`.
    Returns `(image, None)` on success (a Pillow RGBA of only that layer's own pixels -- no other
    layers, so no foreground text baked on top -- transparent where the layer does not cover the
    viewport), or `(None, reason)` on failure. The reason preserves the real cause (repr(exc), or
    the fact that psd-tools returned None) so the caller's warning names WHY -- a corrupt layer, a
    psd-tools bug, and a legitimately empty composite are otherwise indistinguishable, exactly the
    silent-failure the sibling composite_psd/open_layer_index avoid by returning a reason too."""
    try:
        box = tuple(int(v) for v in viewport)
        img = layer.composite(viewport=box)
        if img is None:
            return None, "layer.composite(viewport=...) returned None"
        return img.convert("RGBA"), None
    except Exception as exc:
        return None, repr(exc)


def rasterize_image_cell(
    cell: Cell,
    composite,
    bundle_root,
    *,
    assets_subdir: str = "assets",
    filename: Optional[str] = None,
    alt: Optional[str] = None,
    layer_names: Optional[Mapping] = None,
    source_scale: float = 1.0,
) -> RasterResult:
    """Crop `cell.rect` out of `composite` (a Pillow image from `composite_psd()`, in the SAME
    absolute canvas coordinate space `table_solver` produced `rect` in -- no offset needed) and
    save it as a PNG under `bundle_root/assets_subdir/`. Non-text imagery is always cropped, never
    re-typeset -- the inverse rule from `rasterize_brand_headline`.

    `source_scale` > 1 means `cell.rect` is in CSS px (the tree was density-scaled) while `composite`
    is full resolution -- so the crop box is multiplied back up by `source_scale` to sample the full
    PSD pixels (retina); the emitter displays the result at 1/source_scale. Identity at 1.0."""
    if cell.role not in ("image", "graphic"):
        raise ValueError(f"rasterize_image_cell: expects role in ('image','graphic'), got {cell.role!r}")
    if isinstance(composite, dict) or composite is None:
        reason = composite.get("reason") if isinstance(composite, dict) else "composite is None"
        raise RasterizerUnavailable(f"rasterize_image_cell: no usable PSD composite available ({reason})")
    if not _PIL_AVAILABLE:  # pragma: no cover -- guarded above via composite_psd, kept for safety
        raise RasterizerUnavailable(f"rasterize_image_cell: Pillow unavailable ({_PIL_IMPORT_ERROR!r})")

    rect = cell.rect
    s = float(source_scale) if source_scale else 1.0
    box = (
        max(0, round(rect.left * s)),
        max(0, round(rect.top * s)),
        min(composite.width, round(rect.right * s)),
        min(composite.height, round(rect.bottom * s)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(
            f"rasterize_image_cell: degenerate/out-of-bounds rect {rect.to_dict()} against composite "
            f"size {composite.size}"
        )

    crop = composite.crop(box)

    assets_dir = _ensure_assets_dir(bundle_root, assets_subdir)
    fname = filename or _stable_name(
        "img", cell.source_layer_id, tuple(cell.image_source_layer_ids or ()), box, ext="png"
    )
    out_path = assets_dir / fname
    crop.save(out_path, format="PNG")
    png_bytes = out_path.read_bytes()
    actual_w, actual_h = _png_dims(png_bytes)

    return RasterResult(
        relpath=_assets_relpath(assets_subdir, fname),
        alt=alt or _alt_for_image_cell(cell, layer_names),
        width=actual_w,
        height=actual_h,
        backend="pillow-crop",
        degraded_brand_font=False,
        overflow=False,
        warning=None,
    )


# --- dispatcher ------------------------------------------------------------------------------


def rasterize_cell(
    cell: Cell,
    bundle_root,
    *,
    final_copy: Optional[str] = None,
    composite=None,
    layer_names: Optional[Mapping] = None,
    assets_subdir: str = "assets",
    filename: Optional[str] = None,
    alt: Optional[str] = None,
    registry=None,
    source_scale: float = 1.0,
) -> RasterResult:
    """Backend selector: classify `cell` via `layer_router.render_role()` and dispatch to the
    matching rasterize_* function. Raises ValueError for any role that is not raster-eligible
    (merge/cta/body text, or a "rows" container) -- those must never reach the rasterizer at all;
    a caller should only invoke this for cells `layer_router.iter_routed()` marked verb=="raster".

    `source_scale` (density) is forwarded so both backends emit FULL-resolution assets even when the
    cell geometry has been scaled to CSS px -- identity at 1.0."""
    role = render_role(cell)
    if role == ROLE_BRAND_HEADLINE:
        return rasterize_brand_headline(
            cell, bundle_root, final_copy=final_copy, assets_subdir=assets_subdir, filename=filename, alt=alt,
            registry=registry, source_scale=source_scale,
        )
    if role in (ROLE_IMAGE, ROLE_GRAPHIC):
        return rasterize_image_cell(
            cell, composite, bundle_root, assets_subdir=assets_subdir, filename=filename, alt=alt,
            layer_names=layer_names, source_scale=source_scale,
        )
    raise ValueError(
        f"rasterize_cell: render_role {role!r} is not raster-eligible -- only brand_headline/image/graphic "
        "cells may ever be routed to raster (merge/cta/body/container must stay live)."
    )
