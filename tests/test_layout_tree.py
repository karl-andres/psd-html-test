"""LayoutTree IR: dataclass round-trips to/from dict/JSON."""

from psd_html.layout_tree import (
    BBox,
    Canvas,
    Layer,
    LayoutTree,
    TextInfo,
    TextRun,
    layout_tree_to_rects,
)


def _sample_tree() -> LayoutTree:
    return LayoutTree(
        psd="sample.psd",
        path="C:/fake/sample.psd",
        canvas=Canvas(width=640, height=800),
        artboards=[1],
        layers=[
            Layer(
                id=1,
                name="Artboard 1",
                kind="artboard",
                visible=True,
                opacity=1.0,
                bbox=BBox(left=0, top=0, right=640, bottom=800),
                z=0,
                is_group=True,
                parent=None,
            ),
            Layer(
                id=2,
                name="Background",
                kind="pixel",
                visible=True,
                opacity=1.0,
                bbox=BBox(left=0, top=0, right=640, bottom=800),
                z=1,
                is_group=False,
                parent=1,
            ),
            Layer(
                id=3,
                name="Headline",
                kind="type",
                visible=True,
                opacity=0.8,
                bbox=BBox(left=20, top=20, right=300, bottom=60),
                z=2,
                is_group=False,
                parent=1,
                text=TextInfo(
                    content="Hello world",
                    align="center",
                    runs=[TextRun(font="Arial", size=24.0, color="#FFFFFF")],
                ),
            ),
            Layer(
                id=4,
                name="Hidden layer",
                kind="shape",
                visible=False,
                opacity=1.0,
                bbox=None,
                z=3,
                is_group=False,
                parent=1,
            ),
        ],
    )


def test_bbox_round_trip():
    b = BBox(left=1, top=2, right=11, bottom=22)
    d = b.to_dict()
    assert d == {"left": 1, "top": 2, "right": 11, "bottom": 22}
    b2 = BBox.from_dict(d)
    assert b2 == b
    assert b2.width == 10
    assert b2.height == 20
    assert b2.area == 200
    assert BBox.from_dict(None) is None


def test_text_info_round_trip():
    t = TextInfo(content="hi", align="left", runs=[TextRun(font="Helvetica", size=12.0, color="#000000")])
    d = t.to_dict()
    t2 = TextInfo.from_dict(d)
    assert t2.content == t.content
    assert t2.align == t.align
    assert len(t2.runs) == 1
    assert t2.runs[0].font == "Helvetica"
    assert t2.runs[0].size == 12.0
    assert t2.runs[0].color == "#000000"


def test_text_info_round_trip_all_fields():
    # The whole tree is round-tripped in production (bakeoff.py) and the emitter reads
    # underline/leading/length -- so EVERY TextRun + TextInfo field must survive exactly.
    t = TextInfo(
        content="Hello\nworld",
        align="justify",
        runs=[
            TextRun(font="Arial", size=24.0, color="#FF0000", length=5,
                    baseline=1, leading=28.8, underline=True),
            TextRun(font="Georgia", size=12.5, color="#00FF00", length=6,
                    baseline=2, leading=15.0, underline=False),
        ],
        paragraphs=[{"length": 6, "space_after": 4.0}, {"length": 5, "space_after": 0.0}],
    )
    t2 = TextInfo.from_dict(t.to_dict())
    assert t2.content == t.content
    assert t2.align == t.align
    assert t2.paragraphs == t.paragraphs
    assert len(t2.runs) == len(t.runs) == 2
    for r2, r in zip(t2.runs, t.runs, strict=True):
        assert r2.font == r.font
        assert r2.size == r.size
        assert r2.color == r.color
        assert r2.length == r.length
        assert r2.baseline == r.baseline
        assert r2.leading == r.leading
        assert r2.underline == r.underline


def test_layer_round_trip_with_and_without_text():
    tree = _sample_tree()
    for layer in tree.layers:
        d = layer.to_dict()
        layer2 = Layer.from_dict(d)
        assert layer2.id == layer.id
        assert layer2.name == layer.name
        assert layer2.kind == layer.kind
        assert layer2.visible == layer.visible
        assert layer2.opacity == layer.opacity
        assert layer2.z == layer.z
        assert layer2.is_group == layer.is_group
        assert layer2.parent == layer.parent
        if layer.bbox is None:
            assert layer2.bbox is None
        else:
            assert layer2.bbox == layer.bbox
        if layer.text is None:
            assert layer2.text is None
        else:
            assert layer2.text.content == layer.text.content


def test_layout_tree_dict_round_trip():
    tree = _sample_tree()
    d = tree.to_dict()
    tree2 = LayoutTree.from_dict(d)
    assert tree2.psd == tree.psd
    assert tree2.path == tree.path
    assert tree2.canvas == tree.canvas
    assert tree2.artboards == tree.artboards
    assert len(tree2.layers) == len(tree.layers)
    assert [l.name for l in tree2.layers] == [l.name for l in tree.layers]


def test_layout_tree_json_round_trip():
    tree = _sample_tree()
    s = tree.to_json()
    tree2 = LayoutTree.from_json(s)
    assert tree2.to_dict() == tree.to_dict()


def test_layout_tree_to_rects_filters_groups_hidden_and_bboxless():
    tree = _sample_tree()
    rects = layout_tree_to_rects(tree)
    names = {r["name"] for r in rects}
    # Artboard (group-ish container) excluded, hidden shape excluded (no bbox anyway too).
    assert "Artboard 1" not in names
    assert "Hidden layer" not in names
    assert "Background" in names
    assert "Headline" in names
    headline = next(r for r in rects if r["name"] == "Headline")
    assert headline["is_text"] is True
    assert headline["bbox"] == {"left": 20, "top": 20, "right": 300, "bottom": 60}
    background = next(r for r in rects if r["name"] == "Background")
    assert background["is_text"] is False
