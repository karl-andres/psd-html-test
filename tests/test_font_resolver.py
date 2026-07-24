"""font_resolver: PSD run-font name -> {family, css_stack, brand_mandatory, files}.

Covers the registry seeded from the real Intel PSD corpus (Segoe UI, brand-mandatory) plus the 7
web-safe families, the unknown-font default + loud warning, and the weight/style suffix
normalization ("SegoeUI-Bold" / "SegoeUI-Semibold" -> the same entry as "SegoeUI"). No tautological
asserts: every case is checked against a hand-picked expected shape, not against the function's
own output.
"""

from __future__ import annotations

import glob
import os

import pytest

from psd_html.font_resolver import (
    DEFAULT_FALLBACK_STACK,
    DEFAULT_REGISTRY,
    GENERIC_CSS_FAMILIES,
    FontRegistryEntry,
    FontResolution,
    build_default_registry,
    font_files_for,
    is_brand_mandatory,
    normalize_font_name,
    resolve,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ANNOUNCEMENT_PSD = os.path.join(
    REPO_ROOT,
    "Reference",
    "2413101_Intel",
    "PSDs",
    "Intel x Microsoft_Commercial Refresh_announcement email",
    "Intel_MsfT_Global BoM_Announcement Email.psd",
)


# --- Segoe UI family (what the real PSDs actually contain) ---------------------------------------
# brand_mandatory=False since 2026-07-14: the shipped human-built OFT renders live Segoe UI in
# classic Outlook (Word on Windows = installed face); rasterizing it caused the owner-visible
# top-trim / shrink-scaling / wrap-drift defects.


def test_segoeui_resolves_live_with_websafe_terminated_stack():
    res = resolve("SegoeUI")
    assert isinstance(res, FontResolution)
    assert res.family == "Segoe UI"
    assert res.brand_mandatory is False
    assert res.warning is None
    # must end in a CSS generic keyword, never on a bare/unquoted brand name
    stack_names = [p.strip().strip("'") for p in res.css_stack.split(",")]
    assert stack_names[-1] in GENERIC_CSS_FAMILIES
    assert stack_names[0] == "Segoe UI"
    assert "Times New Roman" not in res.css_stack  # the classic @font-face-fallback bug


@pytest.mark.parametrize("raw", ["SegoeUI-Bold", "SegoeUI-Semibold", "Segoe UI", "segoeui-BOLD"])
def test_segoeui_weight_variants_all_resolve_to_the_same_brand_entry(raw):
    res = resolve(raw)
    assert res.family == "Segoe UI"
    assert res.brand_mandatory is False


def test_segoeui_css_stack_never_ends_on_bare_brand_name():
    res = resolve("SegoeUI-Bold")
    # the LAST token must be a generic keyword, not "Segoe UI" itself
    last = res.css_stack.split(",")[-1].strip()
    assert last in GENERIC_CSS_FAMILIES


def test_segoeui_files_if_discovered_point_at_real_files_on_disk():
    # Best-effort discovery on this Windows box -- if it found anything, every path must be real.
    # SKIP LOUDLY (not pass vacuously) when discovery is empty, so a font-file-less host reads as
    # "not validated here" instead of a silent green that asserts nothing.
    files = font_files_for("SegoeUI")
    if not files:
        import pytest

        pytest.skip("no Segoe UI font files discovered on this host; font-file validation N/A here")
    for f in files:
        assert os.path.isfile(f), f"discovered font file does not exist: {f}"
        assert f.lower().endswith((".ttf", ".otf"))


def test_is_brand_mandatory_wrapper_matches_resolve():
    assert is_brand_mandatory("SegoeUI-Semibold") is False  # live since 2026-07-14 (human-OFT proof)
    assert is_brand_mandatory("Arial") is False


def test_semibold_variant_leads_stack_as_named_family():
    # The human OFT names the installed variant family directly: "Segoe UI Semibold".
    res = resolve("SegoeUI-Semibold")
    first = res.css_stack.split(",")[0].strip().strip("'")
    assert first == "Segoe UI Semibold"
    assert res.weight_css == ""


def test_multiword_family_is_emitted_css_quoted_in_the_literal_stack():
    # Word's/resvg's CSS parsers are stricter than browsers about multi-word font-family idents
    # (see module docstring) -- an unquoted multi-word brand name can fail to match and silently
    # fall back to a default face in classic Outlook. Assert the LITERAL quoted token, not the
    # quote-stripped one other tests here check, so a _format_css_stack regression that silently
    # drops the quoting can't hide behind `.strip("'")`.
    semibold = resolve("SegoeUI-Semibold")
    first_token = semibold.css_stack.split(",")[0].strip()
    assert first_token == "'Segoe UI Semibold'"

    # the plain (unvariant) Segoe UI family is also multi-word and must be quoted too
    plain = resolve("SegoeUI")
    assert plain.css_stack.split(",")[0].strip() == "'Segoe UI'"

    # control: a single-word family name is emitted UNQUOTED
    arial = resolve("Arial")
    assert arial.css_stack.split(",")[0].strip() == "Arial"

    # control: a CSS generic keyword is emitted UNQUOTED (it's not a family ident at all)
    last_token = plain.css_stack.split(",")[-1].strip()
    assert last_token == "sans-serif"
    assert not last_token.startswith("'")


def test_bold_variant_surfaces_as_weight_css_prop():
    res = resolve("SegoeUI-Bold")
    assert res.css_stack.split(",")[0].strip().strip("'") == "Segoe UI"
    assert res.weight_css == "font-weight:bold;"


def test_plain_family_has_empty_weight_css():
    assert resolve("SegoeUI").weight_css == ""
    assert resolve("Arial").weight_css == ""


# --- the 7 web-safe families: resolve to themselves + a single generic keyword -------------------


@pytest.mark.parametrize(
    "raw,expected_family,expected_generic",
    [
        ("Arial", "Arial", "sans-serif"),
        ("Helvetica", "Helvetica", "sans-serif"),
        ("Georgia", "Georgia", "serif"),
        ("Times New Roman", "Times New Roman", "serif"),
        ("TimesNewRoman", "Times New Roman", "serif"),
        ("Calibri", "Calibri", "sans-serif"),
        ("Verdana", "Verdana", "sans-serif"),
        ("Tahoma", "Tahoma", "sans-serif"),
    ],
)
def test_websafe_family_resolves_to_itself_plus_generic(raw, expected_family, expected_generic):
    res = resolve(raw)
    assert res.family == expected_family
    assert res.brand_mandatory is False
    assert res.warning is None
    assert res.css_stack.endswith(expected_generic)
    assert expected_family in res.css_stack


def test_websafe_bold_variant_still_resolves_non_brand():
    res = resolve("Arial-Bold")
    assert res.family == "Arial"
    assert res.brand_mandatory is False


# --- unknown fonts: documented default + LOUD structured warning (never silent) ------------------


def test_unknown_font_gets_default_fallback_and_structured_warning():
    with pytest.warns(UserWarning):
        res = resolve("Comic Sans MS Wingding Nonsense Font 9000")
    assert res.brand_mandatory is False
    assert res.css_stack == "Arial, Helvetica, sans-serif"
    assert list(DEFAULT_FALLBACK_STACK) == ["Arial", "Helvetica", "sans-serif"]
    assert res.warning is not None
    assert res.warning["type"] == "unknown_font"
    assert "Comic Sans MS Wingding Nonsense Font 9000" in res.warning["message"]
    assert res.files == []


@pytest.mark.parametrize("missing", [None, ""])
def test_missing_font_name_does_not_crash_and_gets_safe_default(missing):
    with pytest.warns(UserWarning):
        res = resolve(missing)
    assert res.brand_mandatory is False
    assert res.css_stack == "Arial, Helvetica, sans-serif"
    assert res.warning is not None


def test_font_files_for_unknown_font_is_empty_not_a_crash():
    with pytest.warns(UserWarning):
        assert font_files_for("Some Totally Unregistered Face") == []


# --- registry integrity: every built-in entry's fallback_stack terminates safely -----------------


def test_every_default_registry_entry_ends_in_a_generic_keyword():
    for key, entry in DEFAULT_REGISTRY.items():
        assert entry.fallback_stack, f"{key} has an empty fallback_stack"
        assert entry.fallback_stack[-1] in GENERIC_CSS_FAMILIES, (
            f"registry entry {key!r} ({entry.family!r}) does not terminate in a CSS generic "
            f"keyword: {entry.fallback_stack!r}"
        )


def test_registry_entry_with_bad_fallback_stack_is_rejected_loudly():
    # Adversarial: hand-construct a registry that violates the "must end web-safe" invariant and
    # prove build_default_registry's own validator (_entry/_validate_fallback_stack) would refuse
    # it -- exercised directly via the public factory shape, not by trusting the default registry.
    from psd_html.font_resolver import _entry

    with pytest.raises(ValueError):
        _entry("RogueFont", ("SomeOtherNamedFont",))  # doesn't end in a generic keyword

    with pytest.raises(ValueError):
        _entry("RogueFont", ())  # empty stack


def test_resolve_accepts_a_custom_registry_without_touching_the_default():
    custom = {"myfont": FontRegistryEntry(family="MyFont", fallback_stack=("sans-serif",), brand_mandatory=True)}
    res = resolve("MyFont", registry=custom)
    assert res.family == "MyFont"
    assert res.brand_mandatory is True
    # default registry (module-level) must be unaffected
    assert "myfont" not in DEFAULT_REGISTRY
    # and an unrelated lookup against the SAME custom registry still 404s to the safe default
    with pytest.warns(UserWarning):
        fallback = resolve("SegoeUI", registry=custom)
    assert fallback.brand_mandatory is False


def test_build_default_registry_is_a_fresh_dict_each_call():
    a = build_default_registry()
    b = build_default_registry()
    assert a is not b
    assert a.keys() == b.keys()


# --- normalize_font_name: the suffix-stripping / key-collapsing behavior itself -------------------


@pytest.mark.parametrize(
    "raw,expected_key",
    [
        ("SegoeUI", "segoeui"),
        ("SegoeUI-Bold", "segoeui"),
        ("SegoeUI-Semibold", "segoeui"),
        ("Segoe UI", "segoeui"),
        ("Segoe UI Bold", "segoeui"),
        ("Arial-Italic", "arial"),
        ("Times New Roman", "timesnewroman"),
    ],
)
def test_normalize_font_name(raw, expected_key):
    assert normalize_font_name(raw) == expected_key


# --- variant-aware css_stack / weight_css matrix (beyond the Bold/Semibold cells) ---------------


@pytest.mark.parametrize(
    "raw,expected_weight_css",
    [
        ("SegoeUI-Italic", "font-style:italic;"),
        ("SegoeUI-Bold Italic", "font-weight:bold;font-style:italic;"),  # token order: weight then style
        ("Arial-Medium", "font-weight:500;"),
        ("Arial-ExtraBold", "font-weight:800;"),
        ("Arial-Heavy", "font-weight:900;"),
    ],
)
def test_variant_weight_and_style_css(raw, expected_weight_css):
    assert resolve(raw).weight_css == expected_weight_css


@pytest.mark.parametrize("raw,named", [("SegoeUI-Light", "Segoe UI Light"), ("SegoeUI-Black", "Segoe UI Black")])
def test_named_family_variant_leads_stack_with_empty_weight_css(raw, named):
    res = resolve(raw)
    assert res.css_stack.split(",")[0].strip().strip("'") == named
    assert res.weight_css == ""


# --- _measurement_file variant selection + the file-hint consistency invariant ------------------


def test_measurement_file_picks_variant_file_else_first():
    from psd_html.font_resolver import _measurement_file

    files = ["/fonts/segoeui.ttf", "/fonts/seguisb.ttf"]
    assert _measurement_file("SegoeUI-Semibold", files) == "/fonts/seguisb.ttf"
    # an unknown/absent variant falls back to files[0] (the regular weight)
    assert _measurement_file("SegoeUI-Condensed", files) == "/fonts/segoeui.ttf"
    assert _measurement_file("SegoeUI", files) == "/fonts/segoeui.ttf"


def test_measurement_file_map_values_all_declared_in_filename_hints():
    # A typo in _MEASUREMENT_FILE_BY_VARIANT would silently never match a discovered file and
    # degrade to files[0] (regular weight, ~2-3% under-measured). Pin the cross-table invariant.
    from psd_html.font_resolver import _MEASUREMENT_FILE_BY_VARIANT, _WINDOWS_FONT_FILENAME_HINTS

    for (key, token), filename in _MEASUREMENT_FILE_BY_VARIANT.items():
        assert key in _WINDOWS_FONT_FILENAME_HINTS, f"unknown registry key {key!r}"
        assert filename in _WINDOWS_FONT_FILENAME_HINTS[key], (
            f"measurement file {filename!r} for {(key, token)!r} is not in the discovery hints"
        )


# --- measure_text_px: fractional-size linear scaling + empty/unknown guards ---------------------


def test_measure_text_px_scales_linearly_with_fractional_size():
    if not font_files_for("SegoeUI"):
        pytest.skip("no Segoe UI font files on this host; measure_text_px validation N/A here")
    from psd_html.font_resolver import measure_text_px

    s = "Hello world"
    at_195 = measure_text_px(s, "SegoeUI", 19.5)
    at_390 = measure_text_px(s, "SegoeUI", 39.0)
    at_19 = measure_text_px(s, "SegoeUI", 19.0)
    at_20 = measure_text_px(s, "SegoeUI", 20.0)
    assert at_195 == pytest.approx(at_390 / 2)           # linear in size (no integer-round erasure)
    assert at_19 < at_195 < at_20                        # 19.5 lands strictly between 19 and 20


def test_measure_text_px_empty_and_unknown_guards():
    from psd_html.font_resolver import measure_text_px

    assert measure_text_px("", "SegoeUI", 20.0) == 0.0            # empty -> 0.0 (no file needed)
    assert measure_text_px("x", "TotallyUnknownFace9000", 20.0) is None  # no file -> None (caller degrades)


# --- the real Intel PSD corpus: every distinct run font resolves sanely, none unknown ------------


@pytest.mark.skipif(not os.path.isfile(ANNOUNCEMENT_PSD), reason="Intel announcement PSD fixture not present")
def test_all_run_fonts_in_the_real_announcement_psd_resolve_known_and_brand_mandatory():
    from psd_html.psd_adapter import psd_to_layout_tree

    layout = psd_to_layout_tree(ANNOUNCEMENT_PSD)
    fonts_seen = set()
    for layer in layout.layers:
        if layer.text:
            for run in layer.text.runs:
                if run.font:
                    fonts_seen.add(run.font)

    assert fonts_seen, "expected at least one text run with a font in the announcement PSD"
    for font_name in fonts_seen:
        res = resolve(font_name)
        assert res.warning is None, f"real PSD font {font_name!r} unexpectedly resolved as unknown"
        assert res.brand_mandatory is False, f"real PSD font {font_name!r} expected LIVE (Segoe UI family, human-OFT proof 2026-07-14)"
        assert res.family == "Segoe UI"


@pytest.mark.skipif(
    not glob.glob(os.path.join(REPO_ROOT, "Reference", "2413101_Intel", "PSDs", "**", "*.psd"), recursive=True),
    reason="Intel PSD corpus not present",
)
def test_all_run_fonts_across_every_intel_psd_are_known_to_the_registry():
    from psd_html.psd_adapter import psd_to_layout_tree

    paths = glob.glob(os.path.join(REPO_ROOT, "Reference", "2413101_Intel", "PSDs", "**", "*.psd"), recursive=True)
    assert paths
    fonts_seen = set()
    for p in paths:
        layout = psd_to_layout_tree(p)
        for layer in layout.layers:
            if layer.text:
                for run in layer.text.runs:
                    if run.font:
                        fonts_seen.add(run.font)

    assert fonts_seen
    for font_name in fonts_seen:
        res = resolve(font_name)
        assert res.warning is None, f"unregistered font found in the Intel corpus: {font_name!r}"
