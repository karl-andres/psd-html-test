"""CLI exit-code + fail-loud contract coverage.

These lock the CLI's *translation* layer -- the exit codes, REFUSED messages, and file-write
suppression each command promises -- which the module-level tests (test_config, test_fidelity_gate,
test_bakeoff) never exercise. Every pipeline seam is monkeypatched (the test_bakeoff discipline) so
no real PSD, render, or Adobe dependency is needed; the assertions are on return codes and stderr.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import psd_html.bakeoff as bakeoff_mod
import psd_html.cli as cli
import psd_html.compare as compare_mod
import psd_html.fidelity_gate as fidelity_gate_mod
import psd_html.grid_analyzer as grid_analyzer_mod
import psd_html.html_emitter as html_emitter_mod
import psd_html.intake_validator as intake_validator_mod
import psd_html.layer_router as layer_router_mod
import psd_html.preview as preview_mod
import psd_html.rasterizer as rasterizer_mod
import psd_html.table_solver as table_solver_mod
from psd_html.table_solver import SafetyInvariantViolation
from psd_html.table_tree import TableTree


def _fake_layout():
    return SimpleNamespace(layers=[])


def _tree(email="e0", width=640):
    return TableTree(email=email, width=width, rows=[])


def _patch_load(monkeypatch):
    monkeypatch.setattr(cli, "psd_to_layout_tree", lambda psd: _fake_layout())


def _emit_stub(*args, **kwargs):
    return {"index_path": "index.html", "assets": [], "warnings": [],
            "overflow_flags": [], "bundle_manifest": {}, "links": None}


# --- build: fail-loud intake gate + SafetyInvariantViolation catch (idx 2) ---------------------


def test_build_refuses_and_writes_nothing_when_intake_fails(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(intake_validator_mod, "validate_tree", lambda tree: {
        "pass": False,
        "violations": [{"type": "baked_text_in_rasterized_cell", "artboard": "AB1",
                        "message": "text baked into a graphic cell"}],
    })
    out = tmp_path / "out.json"
    rc = cli.main(["build", "x.psd", "-o", str(out)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED -- intake validator" in err
    assert "[baked_text_in_rasterized_cell] 'AB1'" in err
    assert not out.exists()  # a refused build must never write a broken TableTree


def test_build_refuses_on_safety_invariant_violation(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(intake_validator_mod, "validate_tree", lambda tree: {"pass": True, "violations": []})

    def _boom(tree, email_override=None):
        raise SafetyInvariantViolation([{"email": "e0", "layer_ids": [5], "rect": {"left": 0, "top": 0}}])

    monkeypatch.setattr(table_solver_mod, "build_table_trees", _boom)
    out = tmp_path / "out.json"
    rc = cli.main(["build", "x.psd", "-o", str(out)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "safety invariant violated" in err
    assert not out.exists()


# --- emit: zero-solve refusal + --email-index range + selection (idx 4) ------------------------


def _patch_emit_seams(monkeypatch, trees, route_store):
    _patch_load(monkeypatch)
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: trees)

    def _route(tree, policy):
        route_store["tree"] = tree
        return SimpleNamespace()

    monkeypatch.setattr(layer_router_mod, "route", _route)
    monkeypatch.setattr(html_emitter_mod, "emit", _emit_stub)
    monkeypatch.setattr(rasterizer_mod, "composite_psd", lambda psd: None)


def test_emit_refuses_when_psd_solves_to_zero_emails(monkeypatch, tmp_path, capsys):
    _patch_emit_seams(monkeypatch, [], {})
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "solved to zero" in err


def test_emit_email_index_out_of_range_refuses_cleanly(monkeypatch, tmp_path, capsys):
    _patch_emit_seams(monkeypatch, [_tree("e0"), _tree("e1")], {})
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path), "--email-index", "2"])
    err = capsys.readouterr().err
    assert rc == 1  # clean REFUSED, not an IndexError
    assert "out of range" in err and "solved to 2 email(s)" in err


def test_emit_negative_email_index_refuses(monkeypatch, tmp_path):
    _patch_emit_seams(monkeypatch, [_tree("e0"), _tree("e1")], {})
    assert cli.main(["emit", "x.psd", "-o", str(tmp_path), "--email-index", "-1"]) == 1


def test_emit_email_index_selects_that_tree(monkeypatch, tmp_path):
    store = {}
    _patch_emit_seams(monkeypatch, [_tree("e0"), _tree("e1")], store)
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path), "--email-index", "1"])
    assert rc == 0
    assert store["tree"].email == "e1"  # index 1 selected, not the off-by-one default 0


# --- emit: density derives from the EMAIL artboard width, not the canvas (idx 3) ---------------


def test_emit_density_uses_artboard_width_not_canvas(monkeypatch, tmp_path, capsys):
    store = {}
    # Two 500px-wide emails; a canvas-derived density would use ~1500 and mis-scale.
    _patch_emit_seams(monkeypatch, [_tree("e0", width=500), _tree("e1", width=500)], store)
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path), "--email-index", "1"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "for email width 500px" in err  # resolved against tree.width (artboard), not the canvas


# --- emit: S1 run_summary.json is written on success AND on refusal ----------------------------


def test_emit_writes_run_summary_on_success(monkeypatch, tmp_path):
    _patch_emit_seams(monkeypatch, [_tree("e0")], {})
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path)])
    assert rc == 0
    data = json.loads((tmp_path / "run_summary.json").read_text(encoding="utf-8"))
    assert data["run"]["result"] == "ok"
    assert any(s["name"] == "emit" for s in data["stages"])


def test_emit_writes_failed_run_summary_on_refusal(monkeypatch, tmp_path):
    _patch_emit_seams(monkeypatch, [], {})  # zero solved emails -> REFUSED
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path)])
    assert rc == 1
    data = json.loads((tmp_path / "run_summary.json").read_text(encoding="utf-8"))
    assert data["run"]["result"] == "failed"
    assert data["run"]["failed_stage"] == "solve"


def test_emit_friendly_message_on_trapped_editable_text(monkeypatch, tmp_path, capsys):
    # SafetyInvariantViolation reaches the emit path via build_table_trees -- it must present a
    # plain-language REFUSED (name the layers), not a raw traceback, and leave a failed summary.
    _patch_load(monkeypatch)

    def _boom(layout, email_override=None):
        raise SafetyInvariantViolation([{"email": "e0", "layer_ids": [5], "rect": {"left": 0, "top": 0}}])

    monkeypatch.setattr(table_solver_mod, "build_table_trees", _boom)
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "trapped in a graphic/button cell" in err
    data = json.loads((tmp_path / "run_summary.json").read_text(encoding="utf-8"))
    assert data["run"]["result"] == "failed"
    assert data["run"]["failed_stage"] == "solve"


def test_emit_captures_degrade_warning_into_summary(monkeypatch, tmp_path):
    # A warnings.warn degrade fired anywhere in the run (here: during route) is captured into
    # run_summary.json, so a structurally-fine-but-lower-fidelity run is no longer silent.
    import warnings as _w

    def _warn_route(tree, policy):
        _w.warn("font 'Foo' not installed -- line-count cert skipped", UserWarning)
        return SimpleNamespace()

    _patch_load(monkeypatch)
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [_tree("e0")])
    monkeypatch.setattr(layer_router_mod, "route", _warn_route)
    monkeypatch.setattr(html_emitter_mod, "emit", _emit_stub)
    monkeypatch.setattr(rasterizer_mod, "composite_psd", lambda psd: None)
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path)])
    assert rc == 0
    data = json.loads((tmp_path / "run_summary.json").read_text(encoding="utf-8"))
    all_warnings = [w for s in data["stages"] for w in s["warnings"]]
    assert any("not installed" in w for w in all_warnings)


# --- bakeoff: geometry / editability FAIL branches each exit 1 (idx 5) -------------------------


def _patch_bakeoff_seams(monkeypatch, report):
    _patch_load(monkeypatch)
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [_tree()])
    monkeypatch.setattr(bakeoff_mod, "run", lambda psd, out, density=None: report)


def test_bakeoff_fails_on_non_identical_geometry(monkeypatch, tmp_path, capsys):
    _patch_bakeoff_seams(monkeypatch, {"geometry_identical": False, "editability_clean": True})
    rc = cli.main(["bakeoff", "x.psd", "-o", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "AC-201 violated" in err


def test_bakeoff_fails_on_rasterized_protected_region(monkeypatch, tmp_path, capsys):
    _patch_bakeoff_seams(monkeypatch, {"geometry_identical": True, "editability_clean": False})
    rc = cli.main(["bakeoff", "x.psd", "-o", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "AC-202/205 violated" in err


# --- gate: three-way exit contract unavailable=2 / pass=0 / fail=1 (idx 6) ---------------------


def test_gate_unavailable_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fidelity_gate_mod, "gate_bundle",
                        lambda psd, bundle: {"available": False, "reason": "Adobe not installed"})
    rc = cli.main(["gate", "x.psd", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "GATE UNAVAILABLE" in err


def test_gate_pass_exits_0(monkeypatch, tmp_path):
    monkeypatch.setattr(fidelity_gate_mod, "gate_bundle", lambda psd, bundle: {
        "available": True, "pass": True,
        "counts": {"fail": 0, "warn": 0, "regions": 3}, "findings": [],
    })
    assert cli.main(["gate", "x.psd", str(tmp_path)]) == 0


def test_gate_fail_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fidelity_gate_mod, "gate_bundle", lambda psd, bundle: {
        "available": True, "pass": False,
        "counts": {"fail": 1, "warn": 0, "regions": 3},
        "findings": [{"severity": "fail", "check": "text_offset", "note": "region 2 wraps"}],
    })
    rc = cli.main(["gate", "x.psd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[text_offset]" in out  # the fail finding is listed


# --- bad --config -> ConfigError -> "REFUSED -- bad config" exit 1 in emit/bakeoff/compare (idx 7)


def _bad_config(tmp_path):
    p = tmp_path / "bad.config.json"
    p.write_text(json.dumps({"email_width": 0}), encoding="utf-8")
    return str(p)


def test_emit_bad_config_refuses(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [_tree()])
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path), "--config", _bad_config(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED -- bad config" in err


def test_bakeoff_bad_config_refuses(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [_tree()])
    rc = cli.main(["bakeoff", "x.psd", "-o", str(tmp_path), "--config", _bad_config(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED -- bad config" in err


def test_compare_bad_config_refuses(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [_tree()])
    rc = cli.main(["compare", "x.psd", "-o", str(tmp_path), "--config", _bad_config(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED -- bad config" in err


# --- validate: exit-code contract 0=pass / 1=fail (the SOP-compliance gate scripts shell out to)
# This is the highest-risk previously-uncovered wiring: an inverted/dropped exit-code branch here
# would silently let a non-conforming PSD (baked-in live text / PII) pass `validate` in CI.


def test_validate_exits_0_on_pass_and_writes_report(monkeypatch, tmp_path):
    _patch_load(monkeypatch)
    monkeypatch.setattr(intake_validator_mod, "validate_psd",
                        lambda psd: {"pass": True, "violations": []})
    out = tmp_path / "report.json"
    rc = cli.main(["validate", "x.psd", "-o", str(out)])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["pass"] is True


def test_validate_exits_1_on_fail(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(intake_validator_mod, "validate_psd",
                        lambda psd: {"pass": False, "violations": [{"type": "baked_text", "artboard": "AB1",
                                                                    "message": "live text baked into a graphic"}]})
    rc = cli.main(["validate", "x.psd"])
    out = capsys.readouterr().out
    assert rc == 1  # a non-conforming PSD must exit non-zero, never silently 0
    assert '"pass": false' in out


# --- preview / dump / analyze: success-path wiring (return 0, seam called once) ----------------


def test_preview_wiring_prints_paths_and_exits_0(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    calls = {}

    def _fake_render(psd, out_dir):
        calls["args"] = (psd, out_dir)
        return {"preview_path": str(tmp_path / "preview.html"), "artboards": 2,
                "assets": ["a.png"], "composite_available": True}

    monkeypatch.setattr(preview_mod, "render_preview", _fake_render)
    rc = cli.main(["preview", "x.psd", "-o", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["args"] == ("x.psd", str(tmp_path))
    assert "preview.html" in out and "2 artboard(s)" in out


def test_preview_warns_when_composite_unavailable(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(preview_mod, "render_preview", lambda psd, out_dir: {
        "preview_path": "p.html", "artboards": 1, "assets": [], "composite_available": False})
    rc = cli.main(["preview", "x.psd", "-o", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 0
    assert "composite unavailable" in err


def test_dump_wiring_writes_layout_json(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "psd_to_layout_tree",
                        lambda psd: SimpleNamespace(to_dict=lambda: {"canvas": {"width": 640, "height": 480}, "layers": []}))
    out = tmp_path / "layout.json"
    rc = cli.main(["dump", "x.psd", "-o", str(out)])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["canvas"]["width"] == 640


def test_analyze_wiring_passes_model_and_writes_report(monkeypatch, tmp_path):
    _patch_load(monkeypatch)
    calls = {}

    def _fake_analyze(tree, model="both"):
        calls["model"] = model
        return {"model": model, "regions": []}

    monkeypatch.setattr(grid_analyzer_mod, "analyze_layout_tree", _fake_analyze)
    out = tmp_path / "analysis.json"
    rc = cli.main(["analyze", "x.psd", "-o", str(out), "--model", "v2"])
    assert rc == 0
    assert calls["model"] == "v2"  # the --model choice is threaded through, not hardcoded
    assert json.loads(out.read_text(encoding="utf-8"))["model"] == "v2"


# --- compare: SUCCESS path (only the ConfigError branch was covered before) --------------------


def test_compare_success_prints_compare_path_and_exits_0(monkeypatch, tmp_path, capsys):
    _patch_load(monkeypatch)
    monkeypatch.setattr(table_solver_mod, "build_table_trees", lambda layout, email_override=None: [_tree()])
    monkeypatch.setattr(compare_mod, "build_comparison",
                        lambda psd, out, density=1.0: {"compare_path": str(tmp_path / "compare.html")})
    rc = cli.main(["compare", "x.psd", "-o", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "compare.html" in out


# --- emit --links: a malformed manifest is a clean REFUSED (exit 1), not an unhandled traceback


def test_emit_bad_links_manifest_refuses(monkeypatch, tmp_path, capsys):
    _patch_emit_seams(monkeypatch, [_tree()], {})
    bad = tmp_path / "bad.links.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path), "--links", str(bad)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED -- bad --links manifest" in err


def test_emit_missing_links_manifest_refuses(monkeypatch, tmp_path, capsys):
    _patch_emit_seams(monkeypatch, [_tree()], {})
    rc = cli.main(["emit", "x.psd", "-o", str(tmp_path), "--links", str(tmp_path / "nope.json")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED -- bad --links manifest" in err


# --- top-level operator guard (S1.1): an UNEXPECTED error is a bounded message, not a traceback ---


def test_main_unexpected_exception_gives_bounded_operator_message(monkeypatch, capsys):
    # Not a command's own REFUSED path -- an unexpected fault (psd load blows up). The operator must
    # get a bounded, actionable line and NEVER a raw traceback at default verbosity.
    def _boom(psd):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(cli, "psd_to_layout_tree", _boom)
    rc = cli.main(["dump", "x.psd"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Unexpected error" in err
    assert "not a problem with your PSD" in err
    assert "Traceback" not in err


def test_main_psd_html_debug_reraises_raw_traceback(monkeypatch):
    import pytest

    def _boom(psd):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(cli, "psd_to_layout_tree", _boom)
    monkeypatch.setenv("PSD_HTML_DEBUG", "1")
    # With the debug flag the raw exception propagates verbatim (the guard never swallows it).
    with pytest.raises(RuntimeError, match="disk exploded"):
        cli.main(["dump", "x.psd"])
