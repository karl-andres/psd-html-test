"""TextInfo + bounds + FINAL copy -> a deterministic SVG.

This is the RE-TYPESET path (EARS-208): a brand-headline `Cell` is never pixel-cropped from the
PSD (that would bake in stale sample copy -- see the module-level "LATE COPY BINDING" rule this
builder was speced from). Instead this module re-derives run styling (font/size/color/align) from
the Cell's `TextInfo` and lays the FINAL (late-bound) copy string out fresh into a small, self-
contained SVG string with `<text>`/`<tspan>` elements. `rasterizer.py` feeds that SVG to resvg to
get real, brand-font-embedded PNG bytes.

Determinism: `build_headline_svg()` is a pure function of its inputs -- no wall-clock, no
randomness, no dict-ordering hazards (every collection walked here is already an ordered
tuple/list). Same `(text, width, final_copy, ...)` in -> byte-identical SVG string out, always.

Word-wrap uses REAL glyph measurement on the primary Windows path: when the resolved font's file
is on disk, line breaks come from font_resolver.measure_text_px (PIL ImageFont.getlength --
kerning-accurate, deterministic). Only when no font file is available does it fall back to the
average-glyph-width-per-font-size ratio (`AVG_CHAR_WIDTH_RATIO`), a heuristic good enough to
produce a plausible re-typeset layout and detect gross overflow (the COPY-OVERFLOW GUARD) but not
kerning-accurate. resvg itself always renders the *actual* glyphs; only the fallback path's
*line-break decisions* are heuristic.

COPY-OVERFLOW GUARD: `RasterText.overflow` is True when either (a) a single wrapped line is
estimated wider than the available width (an unbreakable long word/phrase), or (b) the natural
content height exceeds a supplied `min_height` floor. The SVG is still emitted at its natural
(grown) height in either case -- this module NEVER silently clips; it only flags. A caller
(`rasterizer.py`, or the emitter above it) decides what to do with an overflowing region (surface
it for human review, per the build spec).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple, Optional
from xml.sax.saxutils import escape as _xml_escape

from .font_resolver import DEFAULT_REGISTRY, FontResolution, measure_text_px, resolve
from .layout_tree import TextInfo, TextRun

DEFAULT_FONT_SIZE = 16.0
DEFAULT_COLOR = "#000000"
DEFAULT_ALIGN = "left"
LINE_HEIGHT_RATIO = 1.25
# Heuristic average glyph advance width as a fraction of font-size, for a typical sans-serif face
# (Segoe UI / Arial / Helvetica are all close to this). See module docstring -- not real shaping.
AVG_CHAR_WIDTH_RATIO = 0.55
DEFAULT_PADDING = 0

_ALIGNS = ("left", "center", "right", "justify")
_BOLD_HINTS = ("bold", "black", "heavy", "extrabold")
_SEMIBOLD_HINTS = ("semibold", "medium")
_ITALIC_HINTS = ("italic", "oblique")


@dataclass(frozen=True)
class RasterText:
    """The result of `build_headline_svg()`. `svg` is the primary testable artifact; the other
    fields expose the BLOCK-LEVEL layout decisions in structured form so a Pillow-fallback
    renderer (`rasterizer.py`, used only if resvg is unavailable at runtime) can reproduce the
    layout at block granularity without re-parsing the SVG string. Granularity caveat: on the
    per-run path these fields are FLATTENED -- `font_size`/`color` collapse to the block's
    dominant values and `line_height` is the average across lines -- so the fallback replays the
    block, NOT each fragment's own size/color/baseline (those survive only in the `svg` tspans)."""

    svg: str
    width: int
    height: int
    lines: tuple  # tuple[str, ...] -- the wrapped final-copy lines, in order
    line_count: int
    overflow: bool
    font_resolution: FontResolution
    font_size: float
    color: str
    align: str
    weight: str
    style: str
    padding: int
    line_height: float
    final_copy: str


def dominant_run(text: Optional[TextInfo]) -> Optional[TextRun]:
    """The run whose styling governs the whole cell: the first run carrying a non-empty font
    name, or the first run at all if none specify a font, or None if `text` has no runs. Mirrors
    `layer_router._dominant_font`'s "first run with a font wins" convention so the router's
    `is_brand_headline` decision and this adapter's actual rendered font never disagree."""
    if text is None or not text.runs:
        return None
    for run in text.runs:
        if run.font:
            return run
    return text.runs[0]


def dominant_font_name(text: Optional[TextInfo]) -> Optional[str]:
    """Convenience: the raw PSD font name `resolve()`/`font_files_for()` should be asked about
    for this cell's text, or None if the cell has no usable run."""
    run = dominant_run(text)
    return run.font if run is not None else None


def _font_style_attrs(raw_font_name: Optional[str]) -> tuple:
    """Best-effort (weight, style) CSS/SVG attribute values from a raw Photoshop PostScript-ish
    font name (e.g. "SegoeUI-Bold", "SegoeUI-Semibold", "SegoeUI-Italic"). Photoshop bakes
    weight/style into the name rather than carrying separate fields in the S1 IR, so this is a
    substring sniff -- deliberately crude, deliberately deterministic."""
    if not raw_font_name:
        return "normal", "normal"
    low = raw_font_name.lower()
    weight = "normal"
    # "semibold" contains the substring "bold" -- check the semibold/medium hints FIRST so
    # "SegoeUI-Semibold" resolves to 600, not "bold".
    if any(h in low for h in _SEMIBOLD_HINTS):
        weight = "600"
    elif any(h in low for h in _BOLD_HINTS):
        weight = "bold"
    style = "italic" if any(h in low for h in _ITALIC_HINTS) else "normal"
    return weight, style


def _resolve_style(text: Optional[TextInfo], registry) -> tuple:
    run = dominant_run(text)
    font_name = run.font if run is not None else None
    size = float(run.size) if (run is not None and run.size) else DEFAULT_FONT_SIZE
    color = (run.color if (run is not None and run.color) else None) or DEFAULT_COLOR
    align = (text.align if (text is not None and text.align) else None) or DEFAULT_ALIGN
    if align not in _ALIGNS:
        align = DEFAULT_ALIGN
    weight, style = _font_style_attrs(font_name)
    resolution = resolve(font_name, registry=registry)
    return resolution, size, color, align, weight, style


def _wrap_paragraph(paragraph: str, fits) -> list:
    """Greedy word-wrap driven by a `fits(candidate_line) -> bool` predicate, so the SAME wrapper
    serves both REAL glyph measurement (font_resolver.measure_text_px, when a font file exists)
    and the char-count fallback -- the wrap policy never forks from the measurement policy."""
    words = paragraph.split(" ")
    lines: list = []
    cur = ""
    for word in words:
        if not cur:
            cur = word
            continue
        candidate = f"{cur} {word}"
        if fits(candidate):
            cur = candidate
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def _wrap_text(copy: str, fits) -> list:
    """Split `copy` on explicit newlines first (an authored hard line-break always wins), then
    wrap each paragraph with the `fits` predicate. `copy == ""` still yields one (empty) line so
    callers always get a non-empty `lines` tuple to lay out."""
    lines: list = []
    for para in (copy or "").split("\n"):
        lines.extend(_wrap_paragraph(para.rstrip("\r"), fits))
    return lines or [""]


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _svg_doc(width: int, height: int, text_el: str) -> str:
    """The outer SVG document envelope shared by both the per-run and flat rendering paths."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">{text_el}</svg>'
    )


class Fragment(NamedTuple):
    """A per-style-run text fragment: FINAL-copy text plus the run styling the per-run raster
    path lays out. Named (not a bare positional tuple) so a future field reorder can't silently
    swap the size/leading physics the layout math reads by index -- it is still a NamedTuple, so
    the existing unpacking/iteration over fragment lists keeps working unchanged."""

    text: str
    size: Optional[float]
    color: Optional[str]
    baseline: int
    leading: Optional[float]


def _segment_runs(text: Optional[TextInfo], final_copy: str) -> Optional[list]:
    """Split `final_copy` into per-style-run `Fragment`s (text, size, color, baseline, leading)
    using the runs' engine-data span lengths. None when spans can't be aligned (late-bound copy,
    missing lengths, sum mismatch) or when styling is uniform (flat path is identical)."""
    if text is None or not text.runs:
        return None
    runs = text.runs
    if any(r.length is None for r in runs):
        return None
    if final_copy != (text.content or ""):
        return None
    if sum(int(r.length) for r in runs) != len(final_copy):
        return None
    if len({(r.size, r.color, r.baseline) for r in runs}) <= 1 \
            and not any(r.baseline for r in runs):
        return None
    frags = []
    pos = 0
    for r in runs:
        seg = final_copy[pos:pos + int(r.length)]
        pos += int(r.length)
        if seg:
            frags.append(Fragment(seg, float(r.size) if r.size else None, r.color,
                                  int(r.baseline or 0),
                                  float(r.leading) if r.leading else None))
    return frags or None


_SUP_SCALE = 0.62   # superscript glyph size vs its base run size (matches browser/Word ~58-65%)
_SUP_RAISE = 0.33   # superscript baseline raise as a fraction of the base size


def _build_runs_svg(frags, *, width, padding, usable_width, resolution, default_color, align,
                    weight, style, font_name, registry, base_size, min_height, max_height,
                    line_height_ratio, final_copy):
    """Lay out per-run fragments as SVG tspans: explicit \\n splits lines, each line's fragments
    flow inline with their own font-size/color; baseline-1 fragments render raised (superscript)
    and baseline-2 fragments render lowered (subscript), both scaled by _SUP_SCALE with a dy shift.
    Returns a RasterText, or None when any line overflows the box (caller falls back to the flat
    single-style wrap, which can re-break lines)."""
    def eff_size(sz, baseline):
        s = sz if sz else base_size
        return s * _SUP_SCALE if baseline in (1, 2) else s

    # Tokenize fragments into words/whitespace runs carrying their style, then wrap GREEDILY
    # across style boundaries -- a caption like "of organizations have adopted AI(sup)" wraps to
    # its design's two lines with the superscript riding the last line (bailing to the flat path
    # here dropped the raised digits on every multi-line region).
    def tokenize():
        toks = []
        for frag in frags:
            style = (frag.size if frag.size else base_size, frag.color or default_color, frag.baseline,
                     frag.leading if frag.leading else (frag.size if frag.size else base_size) * line_height_ratio)
            parts = frag.text.split("\n")
            for i, part in enumerate(parts):
                if i > 0:
                    toks.append(("\n", None))
                for piece in re.findall(r"\S+|\s+", part):
                    toks.append((piece, style))
        return toks

    def measure_piece(piece, style, scale):
        sz, _c, bl = style[0], style[1], style[2]
        return measure_text_px(piece, font_name, eff_size(sz, bl) * scale, registry=registry)

    def layout(scale, wrap_width=None):
        # wrap_width defaults to the real usable width; GROW-TO-BOX passes a NARROWER width to
        # force the copy onto more lines (the canvas stays `width` -- only the break point moves).
        wrap_cap = usable_width if wrap_width is None else max(1, int(wrap_width))
        lines_ = [[]]
        x = 0.0
        for piece, style in tokenize():
            if style is None:  # explicit newline
                lines_.append([])
                x = 0.0
                continue
            w = measure_piece(piece, style, scale)
            if w is None:
                return None
            if x > 0 and not piece.isspace() and x + w > wrap_cap + 2.0:
                lines_.append([])
                x = 0.0
            if x == 0.0 and piece.isspace():
                continue  # never lead a wrapped line with whitespace
            lines_[-1].append(Fragment(piece, style[0], style[1], style[2], style[3]))
            x += w
        return [ln for ln in lines_ if ln] or None

    def measure_line(ln, scale):
        total = 0.0
        for frag in ln:
            w = measure_text_px(frag.text, font_name, eff_size(frag.size, frag.baseline) * scale, registry=registry)
            if w is None:
                return None
            total += w
        return total

    def natural_of(lines_, scale):
        # Photoshop's ink-tight boxes obey leading*(lines-1) + last-line ink budget: advance by
        # each line's leading, then the last line contributes cap-ink + descender room (0.93 x
        # size, ascent 0.72 + descent 0.21) -- the SAME certified budget the flat path uses, so
        # the two paths seat the last line identically (the old 0.74 single / 1.0 multi split
        # seated the descender ~0.16 x size low and clipped it when the box was ~= natural height).
        total = padding * 2
        for ln in lines_[:-1]:
            total += max(frag.leading for frag in ln) * scale
        last = lines_[-1]
        ink = 0.93
        total += max(frag.size for frag in last) * scale * ink
        return total

    scale = 1.0
    lines = layout(scale)
    if not lines:
        return None
    if max_height is not None and int(max_height) > 0:
        nat = natural_of(lines, scale)
        if nat > int(max_height):
            scale = max(0.4, int(max_height) / nat)
            lines = layout(scale)
            if not lines:
                return None

    for ln in lines:
        w = measure_line(ln, scale)
        if w is None or w > usable_width + 2.0:
            return None  # an unbreakable token overflows -- flat path flags it

    natural_height = int(round(natural_of(lines, scale)))

    # GROW-TO-BOX (mirror of build_headline_svg's flat path): PIL/CSS glyphs run slightly narrower
    # than Photoshop's own layout, so copy the design wraps to N lines can wrap here to N-1 -- the
    # block then ends a leading short of the design box and the render leaves a blank stripe at the
    # bottom (idx 97). Narrow the WRAP width (never the canvas) until the copy reaches the design's
    # estimated line count, so the breaks land lower and the ink spans the box; a block that can't
    # break that far (one long token) keeps its natural wrap. Leading is left unscaled -- inflating
    # it to force-fill overshoots the tspan baselines past the canvas, so the small residual slack
    # a short block leaves is accepted exactly as the flat path accepts it (height = max below).
    if min_height is not None and int(min_height) > 0 and natural_height < int(min_height):
        design_est = max(1, round(int(min_height) / max(1.0, base_size * line_height_ratio)))
        if len(lines) < design_est:
            for _k in range(1, 41):
                _w_try = int(round(usable_width * (1.0 - 0.02 * _k)))
                if _w_try < 40:
                    break
                _l2 = layout(scale, _w_try)
                if _l2 is None:
                    continue
                if len(_l2) >= design_est and all(
                    (measure_line(ln, scale) or 0.0) <= usable_width + 2.0 for ln in _l2
                ):
                    lines = _l2
                    natural_height = int(round(natural_of(lines, scale)))
                    break

    height = natural_height
    if min_height is not None and int(min_height) > 0:
        height = max(natural_height, int(min_height))

    tspans = []
    y = float(padding)
    for li, ln in enumerate(lines):
        line_base = max(frag.size for frag in ln) * scale
        # first baseline at 0.72 x cap height (mirrors the flat path, was 0.95 on multi-line);
        # later baselines advance by the PREVIOUS line's leading
        if li == 0:
            y += line_base * 0.72
        else:
            y += max(frag.leading for frag in lines[li - 1]) * scale
        line_w = measure_line(ln, scale)
        if align == "center":
            x0 = padding + (usable_width - line_w) / 2.0
        elif align == "right":
            x0 = width - padding - line_w
        else:
            x0 = float(padding)
        first = True
        for frag in ln:
            fs = eff_size(frag.size, frag.baseline) * scale
            attrs = f'font-size="{_fmt(fs)}" fill="{frag.color}"'
            if first:
                attrs = f'x="{_fmt(x0)}" y="{_fmt(y)}" text-anchor="start" ' + attrs
                first = False
            if frag.baseline == 1:
                attrs += f' dy="{_fmt(-_SUP_RAISE * frag.size * scale)}"'
            elif frag.baseline == 2:
                attrs += f' dy="{_fmt(0.15 * frag.size * scale)}"'
            tspans.append(f'<tspan {attrs}>{_xml_escape(frag.text)}</tspan>')
            if frag.baseline in (1, 2):
                # reset the baseline for whatever follows on this line
                tspans.append(f'<tspan dy="{_fmt((_SUP_RAISE if frag.baseline == 1 else -0.15) * frag.size * scale)}"> </tspan>')

    text_el = (
        f'<text font-family="{_xml_escape(resolution.css_stack)}" '
        f'font-weight="{weight}" font-style="{style}">' + "".join(tspans) + "</text>"
    )
    svg = _svg_doc(width, height, text_el)
    line_texts = tuple("".join(frag.text for frag in ln) for ln in lines)
    avg_lh = (natural_height - 2 * padding) / max(1, len(lines))
    return RasterText(
        svg=svg, width=width, height=height, lines=line_texts, line_count=len(lines),
        overflow=False, font_resolution=resolution, font_size=base_size * scale,
        color=default_color, align=align, weight=weight, style=style, padding=padding,
        line_height=avg_lh, final_copy=final_copy,
    )


def build_headline_svg(
    text: Optional[TextInfo],
    width: int,
    final_copy: str,
    *,
    min_height: Optional[int] = None,
    max_height: Optional[int] = None,
    padding: int = DEFAULT_PADDING,
    align: Optional[str] = None,
    registry=None,
    line_height_ratio: float = LINE_HEIGHT_RATIO,
    avg_char_width_ratio: float = AVG_CHAR_WIDTH_RATIO,
) -> RasterText:
    """Re-typeset `final_copy` (the late-bound, authoritative string -- NEVER the PSD sample
    copy `text.content`) using `text`'s run styling, into an SVG sized to `width` x a height that
    GROWS to fit the content (never below `min_height` if given, but never clipped above it
    either). Deterministic: identical inputs always produce an identical `RasterText.svg`.

    Raises ValueError on a non-positive `width` or a `None` `final_copy` -- both are caller bugs,
    not degradable conditions (unlike the external-tool failures `rasterizer.py` guards).
    """
    if width is None or int(width) <= 0:
        raise ValueError(f"build_headline_svg: width must be a positive int, got {width!r}")
    if final_copy is None:
        raise ValueError(
            "build_headline_svg: final_copy is required and must be the LATE-BOUND copy string "
            "-- never None. Pass text.content explicitly if the caller truly intends to render "
            "the PSD sample copy (e.g. no copy manifest entry yet), so that choice is visible at "
            "the call site rather than implicit here."
        )

    width = int(width)
    padding = int(padding)
    reg = DEFAULT_REGISTRY if registry is None else registry
    resolution, size, color, resolved_align, weight, style = _resolve_style(text, reg)
    effective_align = align if (align in _ALIGNS) else resolved_align

    usable_width = max(1, width - 2 * padding)

    # Line-break decisions: REAL glyph measurement when the resolved font's file is on disk
    # (font_resolver.measure_text_px -- kerning-accurate, deterministic), else the documented
    # avg-char-width heuristic. The measured path is what kills the "0.55 guess" wrap/clip bugs.
    font_name = dominant_font_name(text)
    _probe = measure_text_px("Mx", font_name, size, registry=reg)

    def _make_fits(sz: float, ww: float):
        # Branches once on the precomputed `_probe` (real glyph measurement available for this
        # font, vs the avg-char-width fallback) so every wrap-fits call site (the initial layout
        # below and each shrink/grow rebind in `_layout_at`) shares one formula instead of drifting.
        if _probe is not None:
            def fits(candidate: str) -> bool:
                w = measure_text_px(candidate, font_name, sz, registry=reg)
                return (w if w is not None else float("inf")) <= ww
        else:
            mc = max(1, int(ww / max(1.0, sz * avg_char_width_ratio)))

            def fits(candidate: str) -> bool:
                return len(candidate) <= mc
        return fits

    _fits = _make_fits(size, usable_width)
    if _probe is not None:
        def _line_overflows(line: str) -> bool:
            w = measure_text_px(line, font_name, size, registry=reg)
            return (w if w is not None else 0.0) > usable_width
    else:
        avg_char_width = max(1.0, size * avg_char_width_ratio)
        max_chars = max(1, int(usable_width / avg_char_width))

        def _line_overflows(line: str) -> bool:
            return len(line) > max_chars

    _dom = dominant_run(text)
    _dom_lead = float(_dom.leading) if (_dom is not None and _dom.leading) else None

    def _ink_height(n_lines: int, lh: float, sz: float) -> int:
        # INK model (shared by the flat path and the shrink/grow rebinds): leading advances the
        # first n-1 lines, then the last line contributes cap-ink + descender room (0.93 x size),
        # NOT a full strut em (len x size x ratio) that over-shrank heads and false-flagged copy.
        return int(round(padding * 2 + (n_lines - 1) * lh + 0.93 * sz))

    def _layout_at(sz: float, wrap_w: Optional[int] = None):
        ww = usable_width if wrap_w is None else max(1, int(wrap_w))
        fits = _make_fits(sz, ww)
        wrapped = _wrap_text(final_copy, fits)
        # Mirror the flat path: design leading when set (else size*ratio) + the ink-model height,
        # so a shrink/grow rebind measures the block the SAME way the flat path does (the strut
        # len*lh discarded the design leading and the 0.93 last-line budget, over-shrinking heads).
        lh = _dom_lead if _dom_lead else sz * line_height_ratio
        return wrapped, lh, _ink_height(len(wrapped), lh, sz)

    # PER-RUN path: mixed run sizes ("Over 70%" = 20/40/20px) and baseline-1 superscript runs
    # (footnote digits) render as per-fragment tspans -- the flat path below styles the whole
    # block at one size and drops the raised digits (live-caught 2026-07-13). Falls back to the
    # flat path whenever spans can't be aligned or a line would overflow.
    _frags = _segment_runs(text, final_copy)
    if _frags is not None and _probe is not None:
        _runs_svg = _build_runs_svg(
            _frags, width=width, padding=padding, usable_width=usable_width,
            resolution=resolution, default_color=color, align=effective_align,
            weight=weight, style=style, font_name=font_name, registry=reg,
            base_size=size, min_height=min_height, max_height=max_height,
            line_height_ratio=line_height_ratio, final_copy=final_copy)
        if _runs_svg is not None:
            return _runs_svg

    lines = _wrap_text(final_copy, _fits)
    line_height = _dom_lead if _dom_lead else size * line_height_ratio
    # INK model, not strut model: Photoshop's design boxes measure glyph ink -- leading*(n-1)
    # plus the LAST line's cap-ink + descender room (0.93 x size, ascent 0.72 + descent 0.21),
    # not a full em strut (len x size x ratio). A 4-line 20px paragraph at 24px leading is
    # ~91px = 3*24 + 0.93*20, not the 92px strut; the old 0.74 cap-only budget clipped descenders
    # on tight-leading display lines (gate/owner-caught). The strut model made every raster
    # paragraph a few px taller than its box and the difference cascaded down the stack.
    natural_height = _ink_height(len(lines), line_height, size)

    # SHRINK-TO-BOX (Layout Invariant): a re-typeset block taller than its DESIGN box pushes
    # every row below it down -- the exact reflow drift the invariant forbids (measured live
    # 2026-07-09: +9..+31px per headline accumulating to ~84px). The faithful correction is the
    # same one live text gets: reduce the font proportionally until the block fits its box,
    # floored at legibility (10px); a block that cannot fit even at the floor keeps the floor
    # size and stays flagged via the height overflow below.
    if max_height is not None and int(max_height) > 0 and natural_height > int(max_height):
        target = int(max_height)
        shrunk = size
        for _ in range(8):
            if natural_height <= target or shrunk <= 10.0:
                break
            shrunk = max(10.0, round(shrunk * max(0.75, target / max(1, natural_height)), 1))
            lines, line_height, natural_height = _layout_at(shrunk)
        size = shrunk

    # GROW-TO-BOX (the mirror case): PIL/CSS glyphs run slightly narrower than Photoshop's own
    # layout, so copy the design wraps to N lines can wrap here to N-1 -- the block then ends a
    # full leading above the design box and the render shows a blank stripe the design doesn't
    # have. Narrow the WRAP width (never the SVG canvas) until the copy reaches the design's
    # line count; breaks land where the proof shows them, ink fills the box.
    if min_height is not None and int(min_height) > 0 and natural_height < int(min_height):
        design_est = max(1, round(int(min_height) / max(1.0, size * line_height_ratio)))
        if len(lines) < design_est:
            for _k in range(1, 16):
                _w_try = int(round(usable_width * (1.0 - 0.015 * _k)))
                if _w_try < 40:
                    break
                _l2, _lh2, _nh2 = _layout_at(size, _w_try)
                if len(_l2) >= design_est:
                    lines, line_height, natural_height = _l2, _lh2, _nh2
                    break
        # Deficit persists (copy simply shorter/denser than the design's) -- distribute the
        # leading so the ink still spans the design box instead of leaving a blank stripe.
        if natural_height < int(min_height) and lines:
            line_height = max(line_height, (int(min_height) - 2 * padding) / len(lines))
            natural_height = _ink_height(len(lines), line_height, size)

    if min_height is not None and int(min_height) > 0:
        height_floor = int(min_height)
        height = max(natural_height, height_floor)
        height_overflow = natural_height > height_floor
    else:
        height = natural_height
        height_overflow = False

    width_overflow = any(_line_overflows(line) for line in lines)
    overflow = bool(width_overflow or height_overflow)

    if effective_align == "center":
        x = width / 2.0
        anchor = "middle"
    elif effective_align == "right":
        x = width - padding
        anchor = "end"
    else:  # "left" or "justify" -- justify is treated as left (no reliable SVG text-justify)
        x = padding
        anchor = "start"

    # Baseline at CAP height (0.72 x size), not a full em: with real (often tight) leading the
    # old full-em baseline pushed the last line's ink past the ink-sized canvas and the SVG
    # clipped descenders/half-lines (live-caught on every 24px/20-leading heading).
    first_baseline = padding + 0.72 * size
    tspans = []
    for i, line in enumerate(lines):
        y = first_baseline + i * line_height
        tspans.append(f'<tspan x="{_fmt(x)}" y="{_fmt(y)}" text-anchor="{anchor}">{_xml_escape(line)}</tspan>')

    text_el = (
        f'<text font-family="{_xml_escape(resolution.css_stack)}" font-size="{_fmt(size)}" '
        f'font-weight="{weight}" font-style="{style}" fill="{color}">' + "".join(tspans) + "</text>"
    )
    svg = _svg_doc(width, height, text_el)

    return RasterText(
        svg=svg,
        width=width,
        height=height,
        lines=tuple(lines),
        line_count=len(lines),
        overflow=overflow,
        font_resolution=resolution,
        font_size=size,
        color=color,
        align=effective_align,
        weight=weight,
        style=style,
        padding=padding,
        line_height=line_height,
        final_copy=final_copy,
    )
