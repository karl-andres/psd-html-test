"""psd_adapter unit coverage: the probe-certified text-metric + normalization rules that,
until now, had ONLY corpus-gated integration coverage (skipped when the proprietary Intel PSD is
absent). Every test here is synthetic (fake duck-typed layers / a monkeypatched PSDImage) so it
runs in the default suite and pins the exact live-caught behaviors documented inline in the module:
alpha-first color ordering, AutoLeading resolution, line-break normalization, length-preserving
C0 scrubbing, RunLengthArray surplus reconciliation, and the is_visible()-over-.visible choice.
"""

from __future__ import annotations

from types import SimpleNamespace

from psd_html import psd_adapter
from psd_html.layout_tree import layout_tree_to_rects
from psd_html.psd_adapter import _extract_text, _rgba_to_hex, psd_to_layout_tree

# --- _rgba_to_hex: Photoshop [A, R, G, B] ordering + 0..1 clamp + len guard (idx 68) ------------


def test_rgba_to_hex_is_alpha_first_not_rgba():
    # [A, R, G, B] = opaque red. A naive RGBA read of vals[0..3] would yield '#FFFF00' (yellow);
    # alpha-first correctly drops vals[0] and reads r=1,g=0,b=0.
    assert _rgba_to_hex([1.0, 1.0, 0.0, 0.0]) == "#FF0000"


def test_rgba_to_hex_clamps_out_of_range_channels():
    # r=1.5 -> 1.0 (FF), g=-0.2 -> 0.0 (00), b=0.5 -> 0x80. Alpha (0.9) ignored.
    assert _rgba_to_hex([0.9, 1.5, -0.2, 0.5]) == "#FF0080"


def test_rgba_to_hex_rejects_non_four_length_and_non_iterable():
    assert _rgba_to_hex([0.5, 0.5]) is None            # len 2
    assert _rgba_to_hex([0.1, 0.2, 0.3, 0.4, 0.5]) is None  # CMYK-style 5-tuple
    assert _rgba_to_hex(5) is None                     # non-iterable -> list() raises -> None


# --- fake engine-data builders -----------------------------------------------------------------


def _style_run(*, size, auto_leading=True, leading=None):
    data = {"FontSize": size, "AutoLeading": auto_leading}
    if leading is not None:
        data["Leading"] = leading
    return {"StyleSheet": {"StyleSheetData": data}}


def _engine_dict(content, style_runs, *, para_props=None, para_lens=None, para_space_after=None):
    n = len(content)
    ed = {
        "StyleRun": {"RunArray": style_runs, "RunLengthArray": [n]},
        "ParagraphRun": {
            "RunArray": [{"ParagraphSheet": {"Properties": para_props or {}}}],
            "RunLengthArray": [n] if para_lens is None else para_lens,
        },
    }
    if para_space_after is not None:
        # rebuild ParagraphRun with per-paragraph SpaceAfter + explicit lengths
        ed["ParagraphRun"] = {
            "RunArray": [
                {"ParagraphSheet": {"Properties": {"SpaceAfter": sa}}} for sa in para_space_after
            ],
            "RunLengthArray": para_lens,
        }
    return ed


def _type_layer(content, engine_dict=None):
    return SimpleNamespace(text=content, engine_dict=engine_dict, resource_dict=None)


# --- resolved leading: AutoLeading ON => size x factor (stored Leading is stale) (idx 63) -------


def test_auto_leading_ignores_stale_stored_leading():
    # AutoLeading True + a bogus stored Leading=17.0 on a 20px run -> 20 * 1.2 (default factor).
    ed = _engine_dict("hello", [_style_run(size=20.0, auto_leading=True, leading=17.0)])
    info = _extract_text(_type_layer("hello", ed))
    assert info.runs[0].leading == 24.0


def test_fixed_leading_used_when_auto_leading_off():
    ed = _engine_dict("hello", [_style_run(size=20.0, auto_leading=False, leading=30.0)])
    info = _extract_text(_type_layer("hello", ed))
    assert info.runs[0].leading == 30.0


def test_paragraph_auto_leading_factor_scales_size():
    # Paragraph-level AutoLeading factor (2.0) drives the size x factor product for auto runs.
    ed = _engine_dict("hello", [_style_run(size=10.0, auto_leading=True)],
                      para_props={"AutoLeading": 2.0})
    info = _extract_text(_type_layer("hello", ed))
    assert info.runs[0].leading == 20.0


# --- line-break normalization at the single entry point (idx 64) -------------------------------


def test_carriage_returns_and_unicode_separators_normalize_to_newline():
    assert _extract_text(_type_layer("a\rb")).content == "a\nb"
    assert _extract_text(_type_layer("a\r\nb")).content == "a\nb"
    assert _extract_text(_type_layer("a b")).content == "a\nb"  # LINE SEPARATOR
    assert _extract_text(_type_layer("a b")).content == "a\nb"  # PARAGRAPH SEPARATOR


def test_interior_blank_line_preserved():
    assert _extract_text(_type_layer("a\n\nb")).content == "a\n\nb"


# --- trailing paragraph-return strip + length-preserving C0 scrub (idx 65) ---------------------


def test_trailing_paragraph_return_stripped_interior_kept():
    # rstrip('\n') removes ONLY trailing returns; a real trailing paragraph artifact is dropped.
    assert _extract_text(_type_layer("para\r")).content == "para"
    assert _extract_text(_type_layer("a\n\nb\n\n")).content == "a\n\nb"


def test_c0_control_replaced_by_space_length_preserving():
    # A stray ETX (\x03) mid-copy must become a single space, NOT be dropped -- length must hold so
    # the RunLengthArray reconciliation stays aligned.
    out = _extract_text(_type_layer("x\x03y")).content
    assert out == "x y"
    assert len(out) == 3


# --- RunLengthArray surplus reconciliation absorbs from the trailing run (idx 66) --------------


def test_run_length_surplus_absorbed_from_last_run():
    content = "abcd"  # len 4
    ed = {
        "StyleRun": {
            "RunArray": [_style_run(size=10.0), _style_run(size=10.0)],
            "RunLengthArray": [2, 3],  # sums to 5 -> +1 surplus
        },
        "ParagraphRun": {"RunArray": [{"ParagraphSheet": {"Properties": {}}}], "RunLengthArray": [4]},
    }
    info = _extract_text(_type_layer(content, ed))
    lengths = [r.length for r in info.runs]
    assert lengths == [2, 2]                       # surplus taken off the LAST run only
    assert sum(lengths) == len(info.content)       # spans now sum exactly to content


# --- paragraph-span reconciliation: surplus, zero-drop, can't-align fallback (idx 69) ----------


def test_paragraph_surplus_absorbed_from_trailing_paragraph():
    content = "abcde"  # len 5
    ed = _engine_dict(content, [_style_run(size=10.0)],
                      para_lens=[2, 4], para_space_after=[6.0, 0.0])  # sums to 6 -> +1 surplus
    info = _extract_text(_type_layer(content, ed))
    assert sum(p["length"] for p in info.paragraphs) == len(content)
    assert [p["length"] for p in info.paragraphs] == [2, 3]  # trailing paragraph absorbed the +1


def test_paragraph_zero_length_dropped():
    content = "abcde"  # len 5
    ed = _engine_dict(content, [_style_run(size=10.0)],
                      para_lens=[0, 5], para_space_after=[0.0, 0.0])
    info = _extract_text(_type_layer(content, ed))
    assert len(info.paragraphs) == 1
    assert info.paragraphs[0]["length"] == 5


def test_paragraphs_cleared_when_they_cannot_align():
    content = "abcde"  # len 5
    ed = _engine_dict(content, [_style_run(size=10.0)],
                      para_lens=[2], para_space_after=[0.0])  # sums SHORT (2 < 5)
    info = _extract_text(_type_layer(content, ed))
    assert info.paragraphs == []  # can't align -> consumers fall back to plain breaks


# --- is_visible() cascade beats bare .visible for hidden-group descendants (idx 67) ------------


class _FakeLayer:
    kind = "pixel"
    layer_id = 7
    name = "child-of-hidden-group"
    opacity = 255
    bbox = (0, 0, 10, 10)
    parent = None
    visible = True             # own flag says visible...

    def is_group(self):
        return False

    def is_visible(self):
        return False           # ...but the ancestor cascade says hidden


class _FakePSD:
    width = 100
    height = 100

    def descendants(self):
        return [_FakeLayer()]


class _FakePSDImage:
    @staticmethod
    def open(path):
        return _FakePSD()


def test_hidden_group_descendant_uses_is_visible_not_visible(monkeypatch):
    monkeypatch.setattr(psd_adapter, "PSDImage", _FakePSDImage)
    tree = psd_to_layout_tree("ignored.psd")
    layer = next(l for l in tree.layers if l.id == 7)
    assert layer.visible is False  # is_visible() (False) won over .visible (True)
    # ...and it is therefore excluded from the eligible-rect set (no phantom overlap): the
    # invisible layer projects to no rect at all.
    rect_names = {r["name"] for r in layout_tree_to_rects(tree)}
    assert "child-of-hidden-group" not in rect_names
