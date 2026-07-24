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
    POST /conformance      PSD (+ optional links.json) in, Grammar-G conformance report out (bundle
                           is built the same way /convert builds it, but discarded, not returned).
                           NOT the pixel-fidelity gate -- that needs headless Chromium/Playwright,
                           which this image does not install; see fidelity_gate.py's module docstring.
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
from psd_html.conformance_validator import validate_bundle
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


def _emit_bundle(
    psd_path: Path,
    out_dir: Path,
    *,
    link_manifest: Optional[dict],
    policy: str,
    email: Optional[str],
    email_index: int,
    email_width: Optional[int],
    density: Optional[float],
):
    """PSD -> emitted bundle at `out_dir`, shared by /convert and /conformance so the two can
    never build a bundle differently. Raises HTTPException on any fail-loud refusal, exactly as
    /convert did before this was extracted. Returns (tree, resolved_density, emit_result)."""
    if policy not in _POLICY_CHOICES:
        raise HTTPException(400, f"policy must be one of {_POLICY_CHOICES}")

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
        raise HTTPException(422, f"{psd_path.name} solved to zero artboards/emails")
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

    return tree, resolved_density, result


async def _read_link_manifest(links: Optional[UploadFile]) -> Optional[dict]:
    if links is None:
        return None
    raw = await links.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"bad links manifest: {exc}") from exc


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


@app.post("/conformance")
async def conformance(
    psd: UploadFile = File(...),
    links: Optional[UploadFile] = File(None),
    policy: str = Form("hybrid"),
    email: Optional[str] = Form(None),
    email_index: int = Form(0),
    email_width: Optional[int] = Form(None),
    density: Optional[float] = Form(None),
) -> JSONResponse:
    """Build the HTML bundle (identical build path to /convert) and run the Grammar-G conformance
    lint over it -- report only, the bundle itself is discarded. Pairs with /convert the same way
    /validate does: a pre-check you can run before committing to downloading the zip.

    Checks: only <img src="">/inline url() raster references (no srcset/<link>/@import/<style>/
    background="".../CSS background-image/VML), zero .svg anywhere, every asset path relative and
    resolving under the bundle root, the classic-Outlook DPI-lock <xml> block present verbatim,
    and (when present) a well-formed _bundle_manifest.json. See conformance_validator.py.

    Does NOT run the pixel-fidelity gate (fidelity_gate.gate_bundle) -- that renders the bundle in
    headless Chromium via Playwright to pixel-diff it against the PSD, and Playwright/Chromium is
    not installed in this image (would add ~200-300MB and real per-request CPU/RAM cost on top of
    PSD compositing, which is already tight against Render's free-tier 512MB cap).
    """
    workdir = Path(tempfile.mkdtemp(prefix="psd_html_api_"))
    try:
        psd_path = workdir / (psd.filename or "upload.psd")
        psd_path.write_bytes(await psd.read())
        link_manifest = await _read_link_manifest(links)

        out_dir = workdir / "bundle"
        _emit_bundle(
            psd_path,
            out_dir,
            link_manifest=link_manifest,
            policy=policy,
            email=email,
            email_index=email_index,
            email_width=email_width,
            density=density,
        )

        report = validate_bundle(str(out_dir))
        status_code = 200 if report["pass"] else 422
        return JSONResponse(report, status_code=status_code)
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
    workdir = Path(tempfile.mkdtemp(prefix="psd_html_api_"))
    try:
        psd_path = workdir / (psd.filename or "upload.psd")
        psd_path.write_bytes(await psd.read())
        link_manifest = await _read_link_manifest(links)

        out_dir = workdir / "bundle"
        tree, resolved_density, result = _emit_bundle(
            psd_path,
            out_dir,
            link_manifest=link_manifest,
            policy=policy,
            email=email,
            email_index=email_index,
            email_width=email_width,
            density=density,
        )

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
