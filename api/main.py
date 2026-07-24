"""FastAPI wrapper around the psd_html HTML-emit leg only (no OFT/Outlook/COM leg -- that half
needs classic Outlook on Windows and can't run in a Linux container). Give it a `.psd` (+ an
optional `links.json`) and get back a zipped Word-safe HTML bundle (`index.html` + `assets/`).

DELIBERATELY NO AUTH / RATE-LIMITING / INPUT HARDENING -- per request, this is a quick cloud
deploy for internal use, not a production-hardened surface. Do not expose this publicly without
adding at least an API key check and an upload size limit.

Run locally:
    uvicorn api.main:app --reload --port 8000

Endpoints:
    GET  /health           liveness check
    POST /validate         PSD in, SOP intake report out (no HTML produced)
    POST /link-candidates  PSD in, starter links.json + discovered slot/region/inline
                           candidates out (no URLs -- a PSD carries none; this only finds
                           what needs one, for a UI to turn into a fill-in-the-URL form)
    POST /convert          PSD (+ optional links.json) in, zipped HTML bundle out
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from psd_html.config import ConfigError, load_config, resolve_density
from psd_html.html_emitter import HtmlEmitterError, emit
from psd_html.intake_validator import validate_psd
from psd_html.layer_router import EditabilityViolation, route
from psd_html.link_scaffold import build_starter_manifest
from psd_html.psd_adapter import psd_to_layout_tree
from psd_html.rasterizer import composite_psd
from psd_html.table_solver import SafetyInvariantViolation, build_table_trees

app = FastAPI(
    title="psd-html API",
    description=(
        "PSD -> Word-safe HTML email bundle converter (the HTML leg of "
        "KaiMallari_From_PSD_To_HTML-OFT). Does not produce .oft -- that leg needs classic "
        "Outlook on Windows."
    ),
    version="0.1.0",
)

_POLICY_CHOICES = ("live", "hybrid", "raster")


def _cleanup(workdir: Path) -> None:
    shutil.rmtree(workdir, ignore_errors=True)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/validate")
async def validate(psd: UploadFile = File(...)) -> JSONResponse:
    """Run the fail-loud SOP intake validator on an uploaded PSD; no HTML is produced."""
    workdir = Path(tempfile.mkdtemp(prefix="psd_html_api_"))
    try:
        psd_path = workdir / (psd.filename or "upload.psd")
        psd_path.write_bytes(await psd.read())
        report = validate_psd(str(psd_path))
        status_code = 200 if report["pass"] else 422
        return JSONResponse(report, status_code=status_code)
    finally:
        _cleanup(workdir)


@app.post("/link-candidates")
async def link_candidates(
    psd: UploadFile = File(...),
    policy: str = Form("hybrid"),
    email: Optional[str] = Form(None),
    email_index: int = Form(0),
) -> JSONResponse:
    """Discover every link_slot/region/inline-text a links.json for this PSD could bind, without
    requiring anyone to already know the manifest schema. Runs the SAME routing+emit `/convert`
    would, into a scratch dir (discarded after), so a candidate can never drift from what a real
    convert would actually produce -- see `link_scaffold.py`'s module docstring.

    Returns no URLs (a PSD carries none) -- a UI should render one input per candidate (mirroring
    the desktop GUI's "Edit links..." form), then POST the filled-in {slots, regions, inline}
    object to /convert as the `links` file, same shape build_starter_manifest's real (empty)
    sections use.
    """
    if policy not in _POLICY_CHOICES:
        raise HTTPException(400, f"policy must be one of {_POLICY_CHOICES}")

    workdir = Path(tempfile.mkdtemp(prefix="psd_html_api_"))
    try:
        psd_path = workdir / (psd.filename or "upload.psd")
        psd_path.write_bytes(await psd.read())
        try:
            manifest = build_starter_manifest(
                psd_path, email_override=email, email_index=email_index, policy=policy
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse(manifest)
    finally:
        _cleanup(workdir)


@app.post("/convert")
async def convert(
    psd: UploadFile = File(...),
    links: Optional[UploadFile] = File(None),
    policy: str = Form("hybrid"),
    email: Optional[str] = Form(None),
    email_index: int = Form(0),
    email_width: Optional[int] = Form(None),
    density: Optional[float] = Form(None),
) -> FileResponse:
    """Convert an uploaded PSD into a zipped OFT-safe HTML bundle.

    Form fields mirror the `psd-html emit` CLI: `policy` (live/hybrid/raster), `email` (name
    override), `email_index` (which artboard in a multi-email PSD), `email_width`/`density`
    (emit-size overrides). `links` is an optional links.json manifest upload (same shape the CLI's
    `--links` takes).
    """
    if policy not in _POLICY_CHOICES:
        raise HTTPException(400, f"policy must be one of {_POLICY_CHOICES}")

    workdir = Path(tempfile.mkdtemp(prefix="psd_html_api_"))
    try:
        psd_path = workdir / (psd.filename or "upload.psd")
        psd_path.write_bytes(await psd.read())

        link_manifest = None
        if links is not None:
            raw = await links.read()
            try:
                link_manifest = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(400, f"bad links manifest: {exc}") from exc

        layout = psd_to_layout_tree(str(psd_path))
        layer_names = {l.id: l.name for l in layout.layers}

        try:
            trees = build_table_trees(layout, email_override=email)
        except SafetyInvariantViolation as exc:
            raise HTTPException(
                422,
                f"REFUSED -- editable text is trapped in a graphic/button cell (SOP #8): "
                f"{[v['email'] for v in exc.violations]}",
            ) from exc
        if not trees:
            raise HTTPException(422, f"{psd.filename} solved to zero artboards/emails")
        if email_index < 0 or email_index >= len(trees):
            raise HTTPException(
                422,
                f"email_index {email_index} out of range; this PSD solved to {len(trees)} "
                f"email(s): {[t.email for t in trees]}",
            )
        tree = trees[email_index]

        try:
            cfg = load_config(None)
            resolved_density, _source, _warning = resolve_density(
                cfg, tree.width, cli_density=density, cli_email_width=email_width
            )
        except ConfigError as exc:
            raise HTTPException(400, f"bad config: {exc}") from exc

        try:
            routed = route(tree, policy)
        except EditabilityViolation as exc:
            raise HTTPException(
                422,
                f"REFUSED -- a protected region (merge field / CTA / body copy) would be "
                f"flattened to a picture: {exc}",
            ) from exc

        composite = composite_psd(str(psd_path))

        out_dir = workdir / "bundle"
        try:
            result = emit(
                routed,
                str(out_dir),
                link_manifest=link_manifest,
                composite=composite,
                layer_names=layer_names,
                psd_path=str(psd_path),
                density=resolved_density,
            )
        except HtmlEmitterError as exc:
            raise HTTPException(422, f"REFUSED -- emit failed: {exc}") from exc

        zip_path = workdir / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in out_dir.rglob("*"):
                if p.is_file():
                    zf.write(p, p.relative_to(out_dir))
            zf.writestr(
                "convert_result.json",
                json.dumps(
                    {
                        "email": tree.email,
                        "density": resolved_density,
                        "assets": result["assets"],
                        "warnings": result["warnings"],
                        "overflow_flags": result["overflow_flags"],
                        "links": result.get("links"),
                    },
                    indent=2,
                ),
            )

        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename="bundle.zip",
            background=BackgroundTask(_cleanup, workdir),
        )
    except HTTPException:
        _cleanup(workdir)
        raise
    except Exception:
        _cleanup(workdir)
        raise
