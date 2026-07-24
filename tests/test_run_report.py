"""Tests for run_report: the S1 per-run logger + run_summary collector."""

from __future__ import annotations

import json
import logging

import pytest

from psd_html.run_report import (
    RunSummary,
    StageRecord,
    configure_logging,
    get_logger,
    new_run_id,
)


def test_stage_records_ok_with_duration_and_warnings():
    s = RunSummary(run_id="r1")
    with s.stage("emit") as rec:
        rec.warnings.append("shrunk a cell")
    assert s.result == "ok"
    assert len(s.stages) == 1
    assert s.stages[0].name == "emit"
    assert s.stages[0].status == "ok"
    assert s.stages[0].duration_ms >= 0
    assert s.stages[0].warnings == ["shrunk a cell"]


def test_failing_stage_marks_run_failed_and_reraises():
    s = RunSummary(run_id="r1")
    with pytest.raises(ValueError):
        with s.stage("convert"):
            raise ValueError("COM refused")
    # fail-loud preserved (the raise propagated) AND recorded
    assert s.result == "failed"
    assert s.failed_stage == "convert"
    assert s.reason == "COM refused"
    assert s.stages[0].status == "failed"


def test_warn_inside_stage_attaches_to_stage_and_logs(caplog):
    s = RunSummary(run_id="r1")
    with caplog.at_level(logging.WARNING, logger="psd_html"):
        with s.stage("emit"):
            s.warn("font 'Foo' not installed -- line-count cert skipped")
    assert s.stages[0].warnings == ["font 'Foo' not installed -- line-count cert skipped"]
    assert any("not installed" in r.message for r in caplog.records)


def test_warn_outside_stage_lands_at_run_level():
    s = RunSummary(run_id="r1")
    s.warn("no composite -- design-truth checks off")
    assert s.stages[-1].name == "(run)"
    assert "no composite -- design-truth checks off" in s.stages[-1].warnings[0]


def test_fail_marks_failed_without_raising():
    s = RunSummary(run_id="r1")
    s.fail("intake", "layer 'Name' covers a fill-in spot")
    assert s.result == "failed"
    assert s.failed_stage == "intake"
    assert any(st.name == "intake" and st.status == "failed" for st in s.stages)


def test_write_produces_schema_valid_json(tmp_path):
    s = RunSummary(run_id="20260717T120000123")
    with s.stage("emit") as rec:
        rec.warnings.append("w1")
    out = tmp_path / "run_summary.json"
    written = s.write(out)
    assert written == str(out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run"] == {"id": "20260717T120000123", "result": "ok",
                           "failed_stage": None, "reason": None}
    assert data["stages"][0]["name"] == "emit"
    assert data["stages"][0]["warnings"] == ["w1"]
    assert "duration_ms" in data["stages"][0]


def test_write_creates_parent_dir(tmp_path):
    s = RunSummary(run_id="r1")
    out = tmp_path / "nested" / "dir" / "run_summary.json"
    s.write(out)
    assert out.exists()


def test_configure_logging_is_idempotent():
    configure_logging(run_id="a")
    configure_logging(run_id="b")
    root = logging.getLogger("psd_html")
    # re-config must not stack handlers
    assert len(root.handlers) == 1
    assert root.propagate is False


def test_get_logger_namespaces_bare_names():
    assert get_logger("psd_html").name == "psd_html"
    assert get_logger("psd_html.emit").name == "psd_html.emit"
    assert get_logger("widget").name == "psd_html.widget"


def test_new_run_id_is_nonempty_string():
    rid = new_run_id()
    assert isinstance(rid, str) and len(rid) >= 15


def test_operator_unexpected_message_is_bounded_and_names_the_id():
    """S1.1: the top-level fallback message names the run id, disclaims the PSD, carries the
    error type/detail for the maintainer, and never contains a raw traceback."""
    from psd_html.run_report import operator_unexpected_message

    msg = operator_unexpected_message("20260717T120000123", ValueError("boom"))
    assert "20260717T120000123" in msg
    assert "not a problem with your PSD" in msg
    assert "ValueError: boom" in msg
    assert "Traceback" not in msg


def test_run_summary_json_is_whitelisted_by_conformance(tmp_path):
    """run_summary.json in a bundle dir must not trip the 'png/jpg/gif only' asset sweep -- it's
    metadata like regions.json/gate_report.json (S1 wiring)."""
    from psd_html.conformance_validator import _scan_filesystem_assets

    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "run_summary.json").write_text("{}", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")  # control: a real off-grammar file
    messages = " ".join(v["message"] for v in _scan_filesystem_assets(tmp_path))
    assert "run_summary.json" not in messages  # whitelisted
    assert "stray.txt" in messages  # still caught, so the sweep itself is working
