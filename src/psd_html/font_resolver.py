"""Font-name -> {family, css_stack, brand_mandatory, files}.

Every text run in the S1 IR (layout_tree.TextRun.font / table_tree.TextRun.font) carries a raw
PostScript-ish font name straight out of Photoshop (e.g. "SegoeUI-Bold"). Two later builders
depend on resolving that name deterministically:

  - the router (builder 2) needs `is_brand_mandatory()` to decide `is_brand_headline` -- the ONLY
    text role ever allowed to be routed to raster (see EARS-202 in the S2 build spec).
  - the rasterizer (builder 3) needs `font_files_for()` -- absolute ttf/otf paths handed to
    resvg_py.svg_to_bytes(font_files=[...]) so a brand headline's font is actually embedded into
    the PNG, instead of silently degrading to whatever resvg's system-font fallback happens to be.

Design:

  - A small registry (`DEFAULT_REGISTRY`) maps a NORMALIZED base-family key (weight/style suffixes
    like "-Bold" / "-Semibold" / "-Italic" stripped, then lower-cased and de-spaced) to a
    `FontRegistryEntry` -- one entry can therefore answer for every weight variant Photoshop
    emits ("SegoeUI", "SegoeUI-Bold", "SegoeUI-Semibold", ... all normalize to the same key).
  - Seeded from what the real Intel PSDs actually contain (ran `psd_to_layout_tree` over all 5
    PSDs under Reference/2413101_Intel/PSDs/**/*.psd and collected the distinct run fonts: only
    "SegoeUI", "SegoeUI-Bold", "SegoeUI-Semibold" turned up -- no separate Intel display face is
    used in this Microsoft co-branded collateral). Segoe UI is registered `brand_mandatory=False`
    (decision 2026-07-14): the shipped human-built OFT for this corpus renders every word as live
    "Segoe UI"/"Segoe UI Semibold" text, and classic Outlook is Word on Windows where Segoe UI is
    an installed system face -- rasterizing it caused every owner-visible text defect (top-trim,
    shrink-scaling, PIL-vs-Word wrap drift). `brand_mandatory=True` remains the right registration
    for faces Windows does NOT install.
  - resolve() is VARIANT-AWARE: named-family variants ("SegoeUI-Semibold") lead css_stack as
    their own installed family ('Segoe UI Semibold', Segoe UI, ...); weight/style variants
    ("SegoeUI-Bold") surface as `weight_css` props emitters append after font-family.
  - The seven web-safe families resolve to themselves + a single generic keyword, per spec.
  - Any other font name (including None/"" -- no run font specified) is unknown: it gets the
    documented default resolution (Arial, Helvetica, sans-serif; brand_mandatory=False) AND a
    structured warning, surfaced two ways so it can never be silently swallowed: (1) a real
    `warnings.warn(...)` at the call site, (2) a `warning` dict on the returned `FontResolution`
    the caller can inspect programmatically without depending on the warnings filter.

Every css_stack is built so it can NEVER end in a bare, unquoted-assumption brand name: each
registry entry's fallback_stack is validated at registry-build time to end in one of the CSS
generic keywords (sans-serif/serif/monospace/cursive/fantasy/system-ui). This is what defuses the
classic Outlook "@font-face silently becomes Times New Roman" failure mode -- a brand-mandatory
headline never even reaches live CSS (the router rasterizes it), but every OTHER text run that
happens to share the brand font (body copy, CTAs, merge fields -- all PROTECTED, forced live by
EARS-202) still gets a safe, terminating font-family stack.
"""

from __future__ import annotations

import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

# --- CSS generic family keywords -----------------------------------------------------------------
# A fallback_stack MUST end in one of these -- enforced by _validate_fallback_stack() below.
GENERIC_CSS_FAMILIES = frozenset({"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"})

# The documented default fallback for any font name the registry doesn't recognize.
DEFAULT_FALLBACK_STACK: tuple = ("Arial", "Helvetica", "sans-serif")


@dataclass(frozen=True)
class FontRegistryEntry:
    """One registered font family.

    `fallback_stack` is the cascade that follows `family` in the CSS font-family list -- it does
    NOT repeat `family` itself -- and MUST end in a GENERIC_CSS_FAMILIES keyword (validated by
    `_validate_fallback_stack` wherever entries are constructed).
    """

    family: str
    fallback_stack: tuple = ()  # tuple[str, ...], last element always a CSS generic keyword
    brand_mandatory: bool = False
    files: tuple = ()  # tuple[str, ...] -- abs paths to installed ttf/otf, best-effort


@dataclass(frozen=True)
class FontResolution:
    """What `resolve()` hands back. The 4 fields the build spec names are `family`, `css_stack`,
    `brand_mandatory`, `files`. `font_name` (the raw input) and `warning` are additions that let a
    caller trace *why* a resolution came out the way it did without re-deriving it -- ignore them
    if you only need the 4 spec fields.
    """

    family: str
    css_stack: str
    brand_mandatory: bool
    files: list
    font_name: Optional[str] = None
    warning: Optional[dict] = None
    # Extra inline CSS props carrying the raw name's weight/style variant ("font-weight:bold;",
    # "font-style:italic;", possibly both). Empty for plain/named-family variants. Emitters append
    # this verbatim after font-family so "SegoeUI-Bold" body copy stays bold when rendered live.
    weight_css: str = ""


# --- name normalization -----------------------------------------------------------------------
# Photoshop emits weight/style baked into the PostScript name ("SegoeUI-Bold", "SegoeUI-Semibold").
# Strip a trailing weight/style token (optionally preceded by "-" or a space) repeatedly, then
# collapse to a bare lower-case alnum key so "Segoe UI", "SegoeUI", "SegoeUI-Bold" all match one
# registry entry.
_SUFFIX_RE = re.compile(
    r"[-\s]?(extrabold|semibold|bold|black|heavy|light|medium|regular|italic|oblique|condensed)$",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _split_variant_suffixes(name: str) -> tuple:
    """Walk `name` stripping trailing weight/style suffix tokens (repeatedly, via _SUFFIX_RE),
    returning `(base, tokens)`: `base` is the name with every suffix stripped; `tokens` is the
    lower-cased suffix tokens found, in name order ("SegoeUI-Bold Italic" -> ["bold", "italic"]).
    Single walk shared by `_strip_variant_suffixes` (base only) and `_variant_tokens` (tokens only)
    so the stripping rule -- which suffixes strip, and the m.start()==0 leading-token guard -- lives
    in exactly one place."""
    base = name.strip()
    found = []
    while True:
        m = _SUFFIX_RE.search(base)
        if not m or m.start() == 0:
            break
        found.append(m.group(1).lower())
        base = base[: m.start()].strip()
    found.reverse()
    return base, found


def _strip_variant_suffixes(name: str) -> str:
    return _split_variant_suffixes(name)[0]


# Variants Windows installs as SEPARATE font families (seguisb.ttf registers as "Segoe UI
# Semibold", not as a 600 weight of "Segoe UI") -- the shipped human OFT names them directly:
# font-family:"Segoe UI Semibold",sans-serif renders natively in classic Outlook's Word engine.
# These lead the css_stack as '<family> <Variant>'; everything else falls through below.
_NAMED_FAMILY_VARIANTS = {"semibold": "Semibold", "light": "Light", "black": "Black"}
# Variants expressed as plain CSS props on the base family (Word honors these live).
_WEIGHT_PROP_VARIANTS = {"bold": "font-weight:bold;", "extrabold": "font-weight:800;",
                         "heavy": "font-weight:900;", "medium": "font-weight:500;"}
_STYLE_PROP_VARIANTS = {"italic": "font-style:italic;", "oblique": "font-style:italic;"}


def _variant_tokens(name: str) -> list:
    """The weight/style tokens `_strip_variant_suffixes` removes, in name order ("SegoeUI-Bold
    Italic" -> ["bold", "italic"]). Drives variant-aware css_stack / weight_css in resolve()."""
    return _split_variant_suffixes(name)[1]


def normalize_font_name(name: str) -> str:
    """Collapse a raw font name to the registry lookup key: strip weight/style suffixes, then
    lower-case and drop everything that isn't a-z0-9 (so spaces/hyphens/case never matter)."""
    base = _strip_variant_suffixes(name)
    return _NON_ALNUM_RE.sub("", base.lower())


def _format_css_stack(names: Sequence[str]) -> str:
    """Join family names into a CSS font-family value, quoting any multi-word, non-generic name."""
    parts = []
    for n in names:
        if n in GENERIC_CSS_FAMILIES or " " not in n:
            parts.append(n)
        else:
            parts.append(f"'{n}'")
    return ", ".join(parts)


def _validate_fallback_stack(family: str, fallback_stack: Sequence[str]) -> tuple:
    if not fallback_stack:
        raise ValueError(f"font registry entry for {family!r} has an empty fallback_stack (must end web-safe)")
    if fallback_stack[-1] not in GENERIC_CSS_FAMILIES:
        raise ValueError(
            f"font registry entry for {family!r} has fallback_stack {fallback_stack!r} not ending in a "
            f"CSS generic keyword ({sorted(GENERIC_CSS_FAMILIES)}) -- css_stack must never dead-end on a "
            "named font that might be missing."
        )
    return tuple(fallback_stack)


def _entry(
    family: str,
    fallback_stack: Sequence[str],
    *,
    brand_mandatory: bool = False,
    files: Sequence[str] = (),
) -> FontRegistryEntry:
    return FontRegistryEntry(
        family=family,
        fallback_stack=_validate_fallback_stack(family, fallback_stack),
        brand_mandatory=brand_mandatory,
        files=tuple(files),
    )


# --- best-effort installed-font-file discovery (Windows box) ------------------------------------
# Never raises: any failure (non-Windows, missing dir, permissions) just yields an empty list, per
# the "loud-but-safe degrade" rule for external/environment-dependent lookups.

_WINDOWS_FONT_FILENAME_HINTS: dict = {
    "segoeui": ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "segoeuii.ttf", "segoeuil.ttf", "segoeuisl.ttf", "segoeuiz.ttf"),
    "arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "timesnewroman": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "tahoma": ("tahoma.ttf", "tahomabd.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf", "calibril.ttf"),
}


def _windows_fonts_dir() -> Optional[Path]:
    try:
        windir = os.environ.get("WINDIR") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
        d = Path(windir) / "Fonts"
        return d if d.is_dir() else None
    except OSError:
        return None


def _discover_font_files(registry_key: str) -> tuple:
    filenames = _WINDOWS_FONT_FILENAME_HINTS.get(registry_key)
    if not filenames:
        return ()
    fonts_dir = _windows_fonts_dir()
    if fonts_dir is None:
        return ()
    found = []
    try:
        for fname in filenames:
            candidate = fonts_dir / fname
            if candidate.is_file():
                found.append(str(candidate.resolve()))
    except OSError:
        return tuple(found)
    return tuple(found)


def build_default_registry() -> dict:
    """Build the module's default registry. A plain factory (not a global singleton mutated in
    place) so tests can call it fresh, or build their own registry dict of the same shape and pass
    it to resolve()/is_brand_mandatory()/font_files_for() via the `registry=` kwarg."""
    registry: dict = {}

    # --- Segoe UI: LIVE, not brand-mandatory (decision 2026-07-14) ------------------------------
    # The shipped human-built OFT for this exact corpus renders every word as live
    # font-family:"Segoe UI"/"Segoe UI Semibold" text -- classic Outlook is Word on Windows, and
    # Segoe UI has shipped with Windows since Vista (Semibold since 7), so the brand face IS the
    # installed face on the certification surface. Rasterizing it (the old brand_mandatory=True)
    # caused every owner-visible text defect: ascender top-trim, shrink-to-box size distortion,
    # PIL wrap points diverging from Word's. Non-Windows clients degrade to the sans-serif
    # fallback exactly as the human build does. brand_mandatory=True remains correct for faces
    # Windows does NOT install (e.g. a licensed display font) -- register those as such.
    registry["segoeui"] = _entry(
        "Segoe UI",
        ("Arial", "Helvetica", "sans-serif"),
        brand_mandatory=False,
        files=_discover_font_files("segoeui"),
    )

    # --- the 7 web-safe families named by the build spec: resolve to themselves + generic -------
    websafe = (
        ("arial", "Arial", "sans-serif"),
        ("helvetica", "Helvetica", "sans-serif"),
        ("georgia", "Georgia", "serif"),
        ("timesnewroman", "Times New Roman", "serif"),
        ("calibri", "Calibri", "sans-serif"),
        ("verdana", "Verdana", "sans-serif"),
        ("tahoma", "Tahoma", "sans-serif"),
    )
    for key, family, generic in websafe:
        registry[key] = _entry(family, (generic,), brand_mandatory=False, files=_discover_font_files(key))

    return registry


DEFAULT_REGISTRY: dict = build_default_registry()


def _default_resolution(font_name: Optional[str]) -> FontResolution:
    message = (
        f"font_resolver: unknown font {font_name!r} -- falling back to "
        f"{', '.join(DEFAULT_FALLBACK_STACK)}. Register it in font_resolver.DEFAULT_REGISTRY if it "
        "is a real family this project should know about."
    )
    warning = {"type": "unknown_font", "message": message, "font_name": font_name}
    warnings.warn(message, UserWarning, stacklevel=3)
    return FontResolution(
        family=font_name or "",
        css_stack=_format_css_stack(DEFAULT_FALLBACK_STACK),
        brand_mandatory=False,
        files=[],
        font_name=font_name,
        warning=warning,
    )


def resolve(font_name: Optional[str], *, registry: Optional[Mapping[str, FontRegistryEntry]] = None) -> FontResolution:
    """Resolve a raw PSD font name to a FontResolution. Never raises -- an unrecognized or missing
    name gets the documented default (Arial/Helvetica/sans-serif, brand_mandatory=False) plus a
    loud, structured warning (both `warnings.warn` and `.warning` on the return value)."""
    reg = DEFAULT_REGISTRY if registry is None else registry
    if not font_name:
        return _default_resolution(font_name)
    key = normalize_font_name(font_name)
    entry = reg.get(key)
    if entry is None:
        return _default_resolution(font_name)
    # VARIANT-AWARE STACK (2026-07-14): the raw PSD name's weight/style tokens survive into CSS.
    # Named-family variants ("SegoeUI-Semibold") lead the stack as their own installed family --
    # exactly how the shipped human OFT names them -- so the base family and web-safe cascade
    # still terminate the stack. Prop variants ("SegoeUI-Bold") come back as weight_css.
    stack_head: list = []
    weight_css = ""
    for token in _variant_tokens(font_name):
        named = _NAMED_FAMILY_VARIANTS.get(token)
        if named:
            stack_head.append(f"{entry.family} {named}")
        else:
            weight_css += _WEIGHT_PROP_VARIANTS.get(token, "") + _STYLE_PROP_VARIANTS.get(token, "")
    return FontResolution(
        family=entry.family,
        css_stack=_format_css_stack(tuple(stack_head) + (entry.family,) + entry.fallback_stack),
        brand_mandatory=entry.brand_mandatory,
        files=list(entry.files),
        font_name=font_name,
        warning=None,
        weight_css=weight_css,
    )


def is_brand_mandatory(font_name: Optional[str], *, registry: Optional[Mapping[str, FontRegistryEntry]] = None) -> bool:
    """Convenience wrapper the router uses to decide `is_brand_headline`."""
    return resolve(font_name, registry=registry).brand_mandatory


def font_files_for(font_name: Optional[str], *, registry: Optional[Mapping[str, FontRegistryEntry]] = None) -> list:
    """Convenience wrapper the rasterizer uses for resvg_py's `font_files=` argument."""
    return resolve(font_name, registry=registry).files


# Cache of loaded PIL fonts keyed by (file, 100px reference size) -- ImageFont.truetype re-parses the file each
# call, and the emitter measures every text run.
_MEASURE_FONT_CACHE: dict = {}

# Which discovered file carries a given weight/style variant, per registry key. Measuring a
# semibold run with the regular file under-reports its advances (~2-3%) -- enough to certify a
# wrap the render doesn't have on a border-line heading.
_MEASUREMENT_FILE_BY_VARIANT: dict = {
    ("segoeui", "semibold"): "seguisb.ttf",
    ("segoeui", "bold"): "segoeuib.ttf",
    ("segoeui", "light"): "segoeuil.ttf",
    ("segoeui", "italic"): "segoeuii.ttf",
    ("arial", "bold"): "arialbd.ttf",
    ("timesnewroman", "bold"): "timesbd.ttf",
    ("georgia", "bold"): "georgiab.ttf",
    ("verdana", "bold"): "verdanab.ttf",
    ("tahoma", "bold"): "tahomabd.ttf",
    ("calibri", "bold"): "calibrib.ttf",
}


def _measurement_file(font_name: Optional[str], files: list) -> str:
    """The best file in `files` for measuring `font_name`'s variant: exact variant file when the
    raw name carries a known token and that file was discovered, else the first (regular) file."""
    if font_name:
        key = normalize_font_name(font_name)
        for token in _variant_tokens(font_name):
            wanted = _MEASUREMENT_FILE_BY_VARIANT.get((key, token))
            if wanted:
                for f in files:
                    if f.lower().endswith(wanted):
                        return f
    return files[0]


def measure_text_px(text: str, font_name: Optional[str], size: float, *, registry: Optional[Mapping[str, FontRegistryEntry]] = None) -> Optional[float]:
    """Measure the REAL rendered width of `text` at `size` px using the resolved font's actual
    file (PIL `ImageFont.getlength` -- deterministic glyph advances, kerning included). Returns
    None when no font file is discoverable or PIL is unavailable -- the caller falls back to its
    heuristic, never crashes. This is the replacement for the avg-char-width guess that caused
    both false overflow flags and real undetected wraps."""
    if not text:
        return 0.0
    files = font_files_for(font_name, registry=registry)
    if not files:
        return None
    try:
        from PIL import ImageFont
    except Exception:
        return None
    # Measure at a fixed 100px reference and scale linearly: PIL font sizes are integers, so
    # measuring at round(size) silently erases fractional sizes (a 19.5px shrink measured at
    # 20px reported a wrap the render didn't have -- live-caught). Glyph advances scale linearly
    # with size to well under the budgets this feeds.
    _REF = 100
    measure_file = _measurement_file(font_name, files)
    key = (measure_file, _REF)
    font = _MEASURE_FONT_CACHE.get(key)
    if font is None:
        try:
            font = ImageFont.truetype(measure_file, _REF)
        except Exception:
            return None
        _MEASURE_FONT_CACHE[key] = font
    try:
        return float(font.getlength(text)) * (float(size) / _REF)
    except Exception:
        return None
