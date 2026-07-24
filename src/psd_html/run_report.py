"""Run observability: a per-run logger + a `run_summary.json` collector.

S1 (see `prds/S1-observability-operator-errors.md`). The pipeline is fail-loud on hard
errors but was silent on soft degrades (missing font -> line-count cert skipped; absent
composite -> design-truth checks off) and left no trace of a run. This module gives the
Python stages a namespaced, run-id-tagged logger and a machine-readable summary that is
written even when the run fails -- so an operator sees what happened and a maintainer can
reconstruct a past run without re-running it.

Status: ACTIVE. Scope is the Python stages (emit/gate/build). Aggregating the full
6-stage PowerShell/COM run into one summary is deferred to S2 (the capture-leg rework),
where the orchestrator that knows all six stages lives.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

_LOGGER_ROOT = "psd_html"


def get_logger(name: str) -> logging.Logger:
    """A logger under the `psd_html` root. Modules call `get_logger(__name__)`; a bare name
    (e.g. a test) is namespaced under the root so one `configure_logging` governs all of them."""
    if name == _LOGGER_ROOT or name.startswith(_LOGGER_ROOT + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")


def configure_logging(level: int = logging.INFO, run_id: str | None = None, stream=None) -> None:
    """Attach a single run-id-tagged handler to the `psd_html` root logger.

    Idempotent: replaces (never stacks) the root's handlers, so calling it again for a new
    run re-tags cleanly. `stream=None` logs to stderr, keeping stdout free for the CLI's
    output contract (JSON/text a caller may pipe). Diagnostics go here; command *output*
    stays on `print`."""
    root = logging.getLogger(_LOGGER_ROOT)
    root.setLevel(level)
    root.propagate = False  # our handler owns psd_html output; don't double-log via the root logger
    tag = f" run={run_id}" if run_id else ""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(f"%(levelname)s %(name)s{tag}: %(message)s"))
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)


def new_run_id() -> str:
    """A sortable, human-readable run id. One process = one run for the CLI, so a timestamp
    (to the millisecond) is unique enough and reads well in a log line or filename."""
    return time.strftime("%Y%m%dT%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"


def operator_unexpected_message(run_id: str, exc: BaseException) -> str:
    """A bounded, operator-facing message for an UNEXPECTED failure (S1.1).

    Distinct from a command's own REFUSED paths, which name the exact layer/URL to fix: this is for
    a fault that is NOT the operator's PSD -- a bug or an environment problem. It tells a
    non-technical operator that, and what to hand the maintainer, instead of dumping a raw
    traceback. The full traceback is preserved in the log (and re-raised under PSD_HTML_DEBUG=1)."""
    return (
        f"Unexpected error (id {run_id}). This is not a problem with your PSD -- it's a fault in the "
        f"tool or its environment. Send the maintainer this id and, if one was written, the "
        f"run_summary.json in your output folder. Detail: {type(exc).__name__}: {exc}"
    )


@dataclass
class StageRecord:
    """One pipeline stage's outcome. `status`: ok | failed | skipped."""

    name: str
    status: str = "ok"
    duration_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "duration_ms": self.duration_ms, "warnings": list(self.warnings)}


@dataclass
class RunSummary:
    """Collects per-stage status/timing/warnings for one run and serializes `run_summary.json`.

    The summary is written even on failure (the `stage` context manager records the failing
    stage + reason and re-raises, so the caller's `finally: summary.write(...)` still fires).
    A fail-loud raise is never swallowed here -- observability augments the hard stop, it does
    not replace it (EARS-007)."""

    run_id: str
    result: str = "ok"  # ok | failed
    failed_stage: str | None = None
    reason: str | None = None
    stages: list[StageRecord] = field(default_factory=list)
    _current: StageRecord | None = field(default=None, repr=False)

    @contextmanager
    def stage(self, name: str) -> Iterator[StageRecord]:
        """Time a stage; on an exception, mark it (and the run) failed, record the reason, and
        re-raise so the fail-loud behavior is preserved."""
        rec = StageRecord(name=name)
        self.stages.append(rec)
        self._current = rec
        t0 = time.perf_counter()
        try:
            yield rec
        except Exception as exc:
            rec.status = "failed"
            self.result = "failed"
            self.failed_stage = name
            self.reason = str(exc)
            raise
        finally:
            rec.duration_ms = int((time.perf_counter() - t0) * 1000)
            self._current = None

    def warn(self, message: str, logger: logging.Logger | None = None) -> None:
        """Record a degrade/warning on the current stage (or run-level if outside a stage) and
        log it at WARN, so a silent fidelity loss now leaves both a log line and a summary entry."""
        if self._current is not None:
            self._current.warnings.append(message)
        else:
            self.stages.append(StageRecord(name="(run)", warnings=[message]))
        (logger or get_logger(_LOGGER_ROOT)).warning(message)

    def fail(self, stage: str, reason: str) -> None:
        """Mark the run failed without raising -- for the CLI's `return 1` refusal paths that
        report to the user themselves but should still leave a failed summary on disk."""
        self.result = "failed"
        self.failed_stage = stage
        self.reason = reason
        if not any(s.name == stage for s in self.stages):
            self.stages.append(StageRecord(name=stage, status="failed"))

    def to_dict(self) -> dict:
        return {
            "run": {"id": self.run_id, "result": self.result,
                    "failed_stage": self.failed_stage, "reason": self.reason},
            "stages": [s.to_dict() for s in self.stages],
        }

    def write(self, path: str | Path) -> str:
        """Write `run_summary.json`. Creates the parent dir if needed; returns the path written."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return str(p)
