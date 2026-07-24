"""C-TEXT-RASTER-ADAPTER tests.

Adversarial hand-built fixtures only (no tautological asserts -- every case is checked against a
hand-picked expected value, never against the function's own output). This module has no PSD
dependency of its own (it operates on TextInfo, not a PSD file), so there is no real-PSD test
here -- that integration is exercised in test_rasterizer.py instead, which does run the real
announcement PSD through the full pipeline.
"""

from __future__ import annotations

import re

import pytest

from psd_html.font_resolver import DEFAULT_REGISTRY
from psd_html.layout_tree import TextInfo, TextRun
from psd_html.text_raster_adapter import (
    LINE_HEIGHT_RATIO,
    build_headline_svg,
    dominant_font_name,
    dominant_run,
)


def _text(content, *, font="SegoeUI-Semibold", size=24.0, color="#000000", align="left"):
    return TextInfo(content=content, align=align, runs=[TextRun(font=font, size=size, color=color)])


# --- determinism -------------------------------------------------------------------------------


def test_deterministic_across_two_calls():
    text = _text("SAMPLE COPY")
    r1 = build_headline_svg(text, 400, "Announcing the new lineup", min_height=60)
    r2 = build_headline_svg(text, 400, "Announcing the new lineup", min_height=60)
    assert r1.svg == r2.svg
    assert r1 == r2


def test_deterministic_svg_bytes_stable_object_identity_not_required():
    # Two independently-built TextInfo objects (not the same instance) with equal content must
    # still produce byte-identical SVGs -- proves determinism is about VALUES, not object identity.
    r1 = build_headline_svg(_text("X"), 200, "Hello", min_height=40)
    r2 = build_headline_svg(_text("X"), 200, "Hello", min_height=40)
    assert r1.svg == r2.svg


# --- late copy binding: final copy renders, sample copy never does ---------------------------


def test_final_copy_appears_verbatim_sample_copy_does_not():
    text = _text("THIS IS THE STALE PSD SAMPLE COPY")
    result = build_headline_svg(text, 900, "THIS IS THE FINAL COPY", min_height=40)
    assert "THIS IS THE FINAL COPY" in result.svg
    assert "STALE PSD SAMPLE COPY" not in result.svg
    assert result.final_copy == "THIS IS THE FINAL COPY"


def test_wrapping_never_drops_words_from_final_copy():
    text = _text("sample")
    final_copy = "one two three four five six seven eight nine ten eleven twelve"
    result = build_headline_svg(text, 80, final_copy, min_height=20)
    reconstructed = " ".join(result.lines)
    assert reconstructed == final_copy
    assert result.line_count == len(result.lines)
    assert result.svg.count("<tspan") == result.line_count


# --- word wrap behavior --------------------------------------------------------------------


def test_narrow_width_forces_multiple_lines():
    text = _text("sample", size=20.0)
    result = build_headline_svg(text, 100, "a fairly long headline that must wrap onto several lines", min_height=20)
    assert result.line_count > 1


def test_generous_width_stays_single_line():
    text = _text("sample", size=16.0)
    result = build_headline_svg(text, 2000, "short headline", min_height=20)
    assert result.line_count == 1
    assert result.lines == ("short headline",)


def test_explicit_newline_in_final_copy_forces_a_line_break():
    text = _text("sample", size=16.0)
    result = build_headline_svg(text, 2000, "line one\nline two", min_height=20)
    assert result.lines == ("line one", "line two")


def test_windows_crlf_final_copy_strips_carriage_return():
    # _wrap_text splits on "\n" then does para.rstrip("\r") -- locking that deliberate strip: a
    # Windows-authored late-bound copy string (copy_manifest entries can arrive with \r\n) must
    # never leak a stray "\r" into an SVG tspan. xml.sax.saxutils.escape does NOT strip \r, so if
    # the rstrip("\r") call were ever removed, the character would survive verbatim into the SVG.
    text = _text("sample", size=16.0)
    result = build_headline_svg(text, 2000, "line one\r\nline two", min_height=20)
    assert result.lines == ("line one", "line two")
    assert "\r" not in result.svg


# --- COPY-OVERFLOW GUARD: never clip, only flag -----------------------------------------------


def test_height_grows_beyond_min_height_and_flags_overflow_never_clips():
    text = _text("sample", size=20.0)
    # A tiny min_height that cannot possibly fit the wrapped content.
    result = build_headline_svg(text, 150, "this headline is much too long for such a small box", min_height=10)
    assert result.overflow is True
    assert result.height > 10
    # Every line the copy wrapped into must still appear as its own tspan -- nothing truncated.
    for line in result.lines:
        assert line in result.svg


def test_generous_min_height_no_overflow():
    text = _text("sample", size=16.0)
    result = build_headline_svg(text, 2000, "short headline", min_height=1000)
    assert result.overflow is False
    assert result.height == 1000


def test_single_unbreakable_word_flags_width_overflow():
    text = _text("sample", size=40.0)
    # One giant word, a width far too narrow for it -- cannot be wrapped further.
    result = build_headline_svg(text, 20, "Supercalifragilisticexpialidocious", min_height=20)
    assert result.overflow is True
    assert result.lines == ("Supercalifragilisticexpialidocious",)


def test_no_min_height_supplied_means_no_height_overflow_possible():
    text = _text("sample", size=16.0)
    result = build_headline_svg(text, 400, "a fine day for a walk in the sun", min_height=None)
    assert result.height >= 1
    # No min_height given -> the only overflow signal left is the width-overflow path, and this
    # short-word copy wraps fine at width=400 -- so overflow should be False here.
    assert result.overflow is False


# --- style resolution: font/size/color/align/weight/style come from the dominant run ----------


def test_bold_font_name_resolves_bold_weight():
    text = _text("sample", font="SegoeUI-Bold")
    result = build_headline_svg(text, 400, "Bold Headline", min_height=20)
    assert result.weight == "bold"
    assert 'font-weight="bold"' in result.svg


def test_semibold_font_name_resolves_600_weight():
    text = _text("sample", font="SegoeUI-Semibold")
    result = build_headline_svg(text, 400, "Semibold Headline", min_height=20)
    assert result.weight == "600"


def test_italic_font_name_resolves_italic_style():
    text = _text("sample", font="SegoeUI-Italic")
    result = build_headline_svg(text, 400, "Italic Headline", min_height=20)
    assert result.style == "italic"
    assert 'font-style="italic"' in result.svg


def test_bold_italic_font_name_resolves_both_weight_and_style_together():
    # Today's independent if/elif(weight) + separate style-check logic already produces the
    # correct combined result -- a refactor that nests the italic check INSIDE the weight elif
    # would silently drop italic style on bold-italic headlines (or vice versa). Pin both
    # resolving TOGETHER on one font name, not just each individually (the two tests above).
    text = _text("sample", font="SegoeUI-BoldItalic")
    result = build_headline_svg(text, 400, "Bold Italic Headline", min_height=20)
    assert result.weight == "bold"
    assert result.style == "italic"
    assert 'font-weight="bold"' in result.svg
    assert 'font-style="italic"' in result.svg


def test_black_and_extrabold_font_names_resolve_bold_weight():
    from psd_html.text_raster_adapter import _font_style_attrs

    assert _font_style_attrs("SegoeUI-Black")[0] == "bold"
    assert _font_style_attrs("SegoeUI-ExtraBold")[0] == "bold"


def test_medium_font_name_resolves_600_weight():
    from psd_html.text_raster_adapter import _font_style_attrs

    assert _font_style_attrs("SegoeUI-Medium")[0] == "600"


def test_segoe_font_resolution_carries_css_stack_with_fallback():
    text = _text("sample", font="SegoeUI-Bold")
    result = build_headline_svg(text, 400, "Headline", min_height=20)
    assert result.font_resolution.brand_mandatory is False  # live since 2026-07-14 (human-OFT proof)
    assert result.font_resolution.family == "Segoe UI"
    assert "Arial" in result.font_resolution.css_stack
    # Pin the EXACT emitted font-family attribute value (quoting, order, and fallback all
    # together) -- "'Segoe UI'" is a superstring of "Segoe UI", so a loose `in` check on either
    # disjunct reduces to a bare substring search that can't tell a swap of css_stack for the
    # bare family (dropping the Arial/Helvetica/sans-serif fallback), or a reordered/truncated
    # stack, from the real thing -- "Segoe UI" would still appear as a substring either way.
    assert 'font-family="\'Segoe UI\', Arial, Helvetica, sans-serif"' in result.svg


def test_websafe_font_is_not_brand_mandatory():
    text = _text("sample", font="Arial-Bold")
    result = build_headline_svg(text, 400, "Headline", min_height=20)
    assert result.font_resolution.brand_mandatory is False


def test_custom_color_and_size_carried_into_svg():
    text = _text("sample", size=32.0, color="#FF00AA")
    result = build_headline_svg(text, 400, "Headline", min_height=20)
    assert result.font_size == 32.0
    assert result.color == "#FF00AA"
    assert 'fill="#FF00AA"' in result.svg
    assert 'font-size="32.00"' in result.svg


def test_missing_run_falls_back_to_defaults_and_warns():
    text = TextInfo(content="no runs here", align=None, runs=[])
    with pytest.warns(UserWarning):
        result = build_headline_svg(text, 400, "Headline", min_height=20)
    assert result.font_resolution.brand_mandatory is False
    assert result.color == "#000000"


def test_none_text_falls_back_to_defaults_and_warns():
    with pytest.warns(UserWarning):
        result = build_headline_svg(None, 400, "Headline", min_height=20)
    assert result.align == "left"
    assert result.font_size == 16.0


def test_dominant_run_prefers_first_run_with_a_font_name():
    text = TextInfo(
        content="mixed",
        align="left",
        runs=[TextRun(font=None, size=10.0, color="#111111"), TextRun(font="Arial-Bold", size=20.0, color="#222222")],
    )
    run = dominant_run(text)
    assert run.font == "Arial-Bold"
    assert dominant_font_name(text) == "Arial-Bold"


def test_dominant_font_name_none_when_no_text():
    assert dominant_font_name(None) is None


# --- alignment ----------------------------------------------------------------------------------


def test_left_align_anchors_start_at_padding():
    text = _text("sample", align="left")
    result = build_headline_svg(text, 300, "Left", min_height=20, padding=10)
    assert 'text-anchor="start"' in result.svg
    assert 'x="10.00"' in result.svg


def test_center_align_anchors_middle_at_half_width():
    text = _text("sample", align="center")
    result = build_headline_svg(text, 300, "Center", min_height=20)
    assert 'text-anchor="middle"' in result.svg
    assert 'x="150.00"' in result.svg


def test_right_align_anchors_end_at_width_minus_padding():
    text = _text("sample", align="right")
    result = build_headline_svg(text, 300, "Right", min_height=20, padding=5)
    assert 'text-anchor="end"' in result.svg
    assert 'x="295.00"' in result.svg


def test_explicit_align_override_wins_over_text_align():
    text = _text("sample", align="left")
    result = build_headline_svg(text, 300, "Overridden", min_height=20, align="right")
    assert result.align == "right"
    assert 'text-anchor="end"' in result.svg


def test_invalid_text_align_value_falls_back_to_default():
    text = TextInfo(content="x", align="diagonal!?", runs=[TextRun(font="Arial", size=16.0, color="#000")])
    result = build_headline_svg(text, 300, "X", min_height=20)
    assert result.align == "left"


# --- input validation (never-degrades, caller-bug errors) -------------------------------------


def test_zero_width_raises_value_error():
    with pytest.raises(ValueError):
        build_headline_svg(_text("x"), 0, "copy", min_height=20)


def test_negative_width_raises_value_error():
    with pytest.raises(ValueError):
        build_headline_svg(_text("x"), -10, "copy", min_height=20)


def test_none_final_copy_raises_value_error():
    with pytest.raises(ValueError):
        build_headline_svg(_text("x"), 400, None, min_height=20)


def test_empty_final_copy_does_not_crash_and_yields_one_empty_line():
    result = build_headline_svg(_text("x"), 400, "", min_height=20)
    assert result.lines == ("",)
    assert result.line_count == 1
    assert isinstance(result.svg, str) and "<svg" in result.svg


# --- output shape: valid SVG envelope ----------------------------------------------------------


def test_svg_has_valid_envelope_and_matching_declared_dimensions():
    result = build_headline_svg(_text("sample"), 250, "Envelope check", min_height=45)
    assert result.svg.startswith("<svg ")
    assert result.svg.endswith("</svg>")
    assert f'width="{result.width}"' in result.svg
    assert f'height="{result.height}"' in result.svg


def test_xml_special_characters_in_final_copy_are_escaped():
    text = _text("sample")
    result = build_headline_svg(text, 900, "Save 10% <today> & save & more", min_height=20)
    assert "<today>" not in result.svg
    assert "&lt;today&gt;" in result.svg
    assert "&amp;" in result.svg


def test_registry_override_changes_resolution():
    from psd_html.font_resolver import FontRegistryEntry

    custom_registry = dict(DEFAULT_REGISTRY)
    custom_registry["myspecialfont"] = FontRegistryEntry(
        family="My Special Font", fallback_stack=("sans-serif",), brand_mandatory=True, files=()
    )
    text = _text("sample", font="MySpecialFont")
    result = build_headline_svg(text, 400, "Headline", min_height=20, registry=custom_registry)
    assert result.font_resolution.family == "My Special Font"
    assert result.font_resolution.brand_mandatory is True


def test_fontless_heuristic_wrap_pins_avg_char_width_line_split():
    # A font with no discoverable files (files=()) forces measure_text_px() to return None, so
    # build_headline_svg falls back to the AVG_CHAR_WIDTH_RATIO heuristic for its wrap decisions.
    # Unlike test_registry_override_changes_resolution above (which only checks font RESOLUTION),
    # this pins the actual wrap CONTRACT: max_chars = max(1, int(usable_width / (size *
    # AVG_CHAR_WIDTH_RATIO))). At size=20, width=200, padding=0 (default): usable_width=200,
    # size*ratio=11.0, max_chars=int(200/11.0)=18 -- an exact boundary the first line lands on.
    from psd_html.font_resolver import FontRegistryEntry

    custom_registry = dict(DEFAULT_REGISTRY)
    custom_registry["myspecialfont"] = FontRegistryEntry(
        family="My Special Font", fallback_stack=("sans-serif",), brand_mandatory=True, files=()
    )
    text = _text("sample", font="MySpecialFont", size=20.0)
    copy = "one two three four five six seven"
    result = build_headline_svg(text, 200, copy, min_height=None, registry=custom_registry)
    # "one two three four" is exactly 18 chars (the max_chars boundary); adding " five" (23 chars)
    # would overflow it -- a regression to the max_chars formula (int-truncation tweak, ratio
    # swap) would shift this exact split.
    assert result.lines == ("one two three four", "five six seven")
    assert result.line_count == 2
    assert " ".join(result.lines) == copy

    # Tiny-width case: usable_width=1 forces max_chars = max(1, int(1/11.0)) = max(1, 0) = 1 --
    # the max(1, ...) floor must keep every line non-empty (never a zero-char degenerate line)
    # and must not crash, even though no candidate line of 2+ chars can ever "fit" at mc=1.
    tiny = build_headline_svg(text, 1, copy, min_height=None, registry=custom_registry)
    assert tiny.line_count == 7
    assert all(len(line) > 0 for line in tiny.lines)
    assert " ".join(tiny.lines) == copy


# --- CERTIFIED INK GEOMETRY (numeric regression pins) -----------------------------------------
# These pin the PROBE-CERTIFIED Word-engine ink model on the flat path so a silent revert of the
# certified constants cannot pass green: the first baseline seats at 0.72*size (cap height) and
# the last line is budgeted 0.93*size (ascent 0.72 + descent 0.21). Reverting either back to an
# old full-em or 0.74 cap-only value reintroduces descender clipping -- and must FAIL here.
# Expected values are hardcoded from the 0.72/0.93 model (never read back from the code under
# test); a large size keeps even a small constant tweak well outside the <=1px tolerance. Strut
# heights (len*size*ratio) are deliberately NOT asserted.


def _tspan_baseline_ys(svg):
    """Ordered baseline y-positions of the flat path's per-line <tspan>s, parsed from the SVG."""
    return [float(m) for m in re.findall(r'<tspan[^>]*\sy="([\d.]+)"', svg)]


def test_flat_single_line_ink_height_and_first_baseline_are_pinned():
    size = 100.0
    text = _text("sample", size=size)
    # No min/max height -> the canvas is exactly the natural ink height (no grow/shrink rebind),
    # isolating the certified single-line geometry.
    result = build_headline_svg(text, 2000, "Short")
    assert result.line_count == 1
    # Single-line ink height = 0.93*size (no inter-line leading term).
    assert abs(result.height - round(0.93 * size)) <= 1
    ys = _tspan_baseline_ys(result.svg)
    assert len(ys) == 1
    # First (only) baseline seats at 0.72*size cap height.
    assert abs(ys[0] - 0.72 * size) <= 1


def test_flat_multi_line_ink_height_and_baselines_are_pinned():
    size = 100.0
    text = _text("sample", size=size)
    # Explicit newline forces exactly two lines regardless of measurement vs. char-count wrap.
    result = build_headline_svg(text, 2000, "line one\nline two")
    assert result.line_count == 2
    leading = size * LINE_HEIGHT_RATIO  # no explicit design leading -> the size*ratio advance
    # Multi-line ink height = (n-1)*leading + 0.93*size last-line budget (NOT the n*leading strut).
    assert abs(result.height - round((2 - 1) * leading + 0.93 * size)) <= 1
    ys = _tspan_baseline_ys(result.svg)
    assert len(ys) == 2
    # First baseline at 0.72*size; each later baseline advances by exactly one leading.
    assert abs(ys[0] - 0.72 * size) <= 1
    assert abs(ys[1] - (0.72 * size + leading)) <= 1


# --- per-run re-typeset path: _segment_runs slicing + its four bail-out guards (idx 86/87) ------
# The whole per-run subsystem (mixed sizes + superscript) had zero coverage: every other fixture
# is a single uniform run, so _segment_runs always returned None and this path never executed.

from psd_html.font_resolver import font_files_for  # noqa: E402
from psd_html.text_raster_adapter import Fragment, _segment_runs  # noqa: E402


def _run(size, length, *, baseline=0, color="#000000", font="SegoeUI-Semibold"):
    return TextRun(font=font, size=size, color=color, length=length, baseline=baseline)


def test_segment_runs_happy_path_slices_by_run_length():
    text = TextInfo(content="Over 70%!", align="left",
                    runs=[_run(20.0, 5), _run(40.0, 3), _run(20.0, 1, baseline=1)])
    frags = _segment_runs(text, "Over 70%!")
    assert frags is not None
    assert [f.text for f in frags] == ["Over ", "70%", "!"]     # exact slice boundaries
    assert [f.size for f in frags] == [20.0, 40.0, 20.0]
    assert [f.baseline for f in frags] == [0, 0, 1]
    assert all(isinstance(f, Fragment) for f in frags)


def test_segment_runs_bails_on_missing_length():
    text = TextInfo(content="AB", align="left", runs=[_run(20.0, None), _run(40.0, None)])
    assert _segment_runs(text, "AB") is None


def test_segment_runs_bails_on_late_bound_copy():
    # final_copy != text.content -> stale run lengths must NOT be sliced onto the new copy.
    text = TextInfo(content="ABCD", align="left", runs=[_run(20.0, 2), _run(40.0, 2)])
    assert _segment_runs(text, "WXYZ") is None


def test_segment_runs_bails_on_length_sum_mismatch():
    text = TextInfo(content="ABCD", align="left", runs=[_run(20.0, 2), _run(40.0, 3)])  # sums to 5
    assert _segment_runs(text, "ABCD") is None


def test_segment_runs_bails_on_uniform_styling():
    text = TextInfo(content="ABCD", align="left", runs=[_run(20.0, 2), _run(20.0, 2)])  # same size, no sup
    assert _segment_runs(text, "ABCD") is None


def test_per_run_path_renders_mixed_sizes_and_superscript():
    if not font_files_for("SegoeUI"):
        pytest.skip("no Segoe UI font files on this host; per-run path needs a real measure probe")
    text = TextInfo(content="Over 70%!", align="left",
                    runs=[_run(20.0, 5), _run(40.0, 3), _run(20.0, 1, baseline=1)])
    r = build_headline_svg(text, 600, "Over 70%!", min_height=60)
    assert "70%" in r.svg
    assert 'dy="' in r.svg          # baseline-1 superscript raise -- only the per-run path emits this
    sizes = set(re.findall(r'font-size="([0-9.]+)"', r.svg))
    assert len(sizes) >= 2, f"expected >=2 distinct per-fragment font sizes, got {sizes}"


# --- DISCOVERED GAP (idx 97): per-run path has no GROW-TO-BOX mirror ----------------------------
# _build_runs_svg computes height = max(natural_height, min_height) but, unlike the flat path
# below, never narrows the wrap width to reach the design's line count and never redistributes
# leading when a deficit persists. This test pins the DESIRED behavior (ink should still reach
# near the bottom of a grown canvas, mirroring the flat path) -- it currently fails, so it is
# marked xfail rather than asserting the buggy behavior as if it were correct.


def test_per_run_path_grow_to_box_should_not_leave_blank_stripe():
    if not font_files_for("SegoeUI"):
        pytest.skip("no Segoe UI font files on this host; per-run path needs a real measure probe")
    # Two runs (40px stat + 20px label) whose combined copy wraps to a single PIL-measured line at
    # this width -- min_height encodes a 2-line design box, so a properly-fixed per-run path
    # should either re-wrap to 2 lines or redistribute leading so the ink reaches near the bottom.
    content = "BIG STAT 70 percent"
    text = TextInfo(
        content=content, align="left",
        runs=[_run(40.0, 9), _run(20.0, len(content) - 9)],
    )
    result = build_headline_svg(text, 400, content, min_height=100)
    ys = _tspan_baseline_ys(result.svg)
    assert result.height == 100  # the canvas DOES grow to the design min_height...
    # ...but today the single baseline stays pinned near the top (no re-wrap, no leading
    # redistribution) -- the last baseline should sit within a small descent budget of the
    # bottom, mirroring the flat path's GROW-TO-BOX comment ("ink fills the box"), not ~70px shy.
    if result.height - ys[-1] > 0.93 * 40.0 + 5:
        pytest.xfail(
            "idx 97 DISCOVERED GAP: _build_runs_svg has no GROW-TO-BOX counterpart -- a "
            "multi-run headline whose natural wrap falls short of min_height grows the canvas "
            "but leaves every tspan at its original y, so ink stays pinned at the top and the "
            "design box's bottom is left as an unaccounted blank stripe (the exact defect the "
            "flat path's GROW-TO-BOX comment says was fixed there)."
        )


# --- SHRINK-TO-BOX: max_height shrinks the block to fit its design box (idx 90) -----------------


def test_shrink_to_box_reduces_font_to_fit_max_height():
    # A 40px block naturally needs ~37px of ink; a 20px box forces a proportional shrink that fits.
    text = _text("Big", size=40.0)
    r = build_headline_svg(text, 400, "Big", min_height=20, max_height=20)
    assert r.font_size < 40.0          # shrunk below the original run size
    assert r.height <= 20              # fits the design box
    assert r.overflow is False         # min==max box, fits -> not flagged


def test_shrink_to_box_floors_at_legibility_and_stays_flagged_when_impossible():
    # A box far too small for even the 10px legibility floor keeps the floor size AND flags overflow.
    text = _text("Big", size=40.0)
    r = build_headline_svg(text, 400, "Big", min_height=5, max_height=5)
    assert r.font_size == pytest.approx(10.0)  # floored at the 10px legibility minimum
    assert r.overflow is True                  # cannot fit even at the floor -> flagged


# --- GROW-TO-BOX line-count reconciliation pinned (idx 91) --------------------------------------
# The flat path's GROW-TO-BOX block (build_headline_svg, ~line 505) narrows the WRAP width (never
# the SVG canvas) to reach the design's estimated line count when a natural wrap falls short --
# and, when even that can't reach it, redistributes leading so ink still spans the box. Neither
# half had a test: this pins both, against real glyph measurement (skip-if-no-font, mirroring
# test_shrink_to_box_* / test_per_run_path_renders_mixed_sizes_and_superscript above).


def test_grow_to_box_narrows_wrap_width_to_reach_design_line_count():
    if not font_files_for("SegoeUI"):
        pytest.skip("no Segoe UI font files on this host; needs the real measure-text-px probe")
    size = 20.0
    text = _text("sample", size=size)
    copy = "Announcing our newest lineup today"
    # Confirm the premise: at this width, with no min_height in play, PIL wraps it to ONE line.
    unconstrained = build_headline_svg(text, 400, copy, min_height=None)
    assert unconstrained.line_count == 1
    # min_height == 2 * size * LINE_HEIGHT_RATIO exactly -> design_est rounds to 2.
    design_est = 2
    min_height = round(design_est * size * LINE_HEIGHT_RATIO)  # 50
    result = build_headline_svg(text, 400, copy, min_height=min_height)
    # GROW-TO-BOX must narrow usable_width (in ~1.5% steps) until the copy actually breaks into a
    # real 2nd line -- a regression that swaps min_height for width in design_est, or that
    # silently disables the narrowing loop, would leave this at line_count == 1 with a blank
    # stripe below (the canvas still grows to min_height either way, so height alone can't catch
    # that regression -- line_count is the signal that must move).
    assert result.line_count == 2
    assert result.height == min_height
    assert result.overflow is False


def test_grow_to_box_deficit_persists_stretches_leading_toward_min_height():
    if not font_files_for("SegoeUI"):
        pytest.skip("no Segoe UI font files on this host; needs the real measure-text-px probe")
    # Two single-word tokens ("Big"/"Data") can NEVER wrap past 2 lines no matter how far the
    # wrap width is narrowed (neither word can be split) -- design_est (4, from min_height=110,
    # since round(110 / (20*1.25)) == 4) is UNREACHABLE. The width-narrowing loop must exhaust
    # without finding a 3rd/4th line, and the deficit-persists tail must still stretch the leading
    # toward the design box instead of leaving the SVG's tspans at their original, tighter spacing.
    size = 20.0
    text = _text("Big Data", size=size)
    result = build_headline_svg(text, 45, "Big Data", min_height=110)
    assert result.line_count == 2   # narrowing genuinely could not add a 3rd/4th line
    assert result.height == 110     # canvas still grows to the full design min_height
    assert result.overflow is False
    # The redistribution formula: leading is stretched to (min_height - 2*padding) / line_count
    # (here padding=0) when that exceeds the natural size*ratio leading -- pinned exactly, not
    # just asserted "greater than", so a formula regression (wrong divisor, wrong operand) fails.
    assert result.line_height == pytest.approx(110 / 2)
    assert result.line_height > size * LINE_HEIGHT_RATIO
    # And the stretched leading actually reaches the SVG's tspans (not just the returned field):
    # the 2nd baseline sits at first_baseline (0.72*size) + the stretched line_height.
    ys = _tspan_baseline_ys(result.svg)
    assert len(ys) == 2
    assert ys[1] == pytest.approx(0.72 * size + result.line_height)
