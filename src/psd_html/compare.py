"""A hands-on side-by-side viewer for one PSD.

Builds, under `out_dir`, everything needed to eyeball how a PSD converts and to run the D6
fidelity bake-off:

    <out_dir>/proof.jpg          -- the PSD's own rendered .jpg proof (ground truth), if present
    <out_dir>/preview/           -- the faithful region-overlay preview (truth pixels + extraction)
    <out_dir>/live/  hybrid/  raster/  -- the three OFT-safe bundles from the bake-off
    <out_dir>/BAKEOFF_RUNBOOK.md  -- how to round-trip each to .oft in classic Outlook (bakeoff)
    <out_dir>/compare.html        -- ONE page showing proof | preview | live | hybrid | raster
                                     side by side, with the oracle scores + the .oft next-steps

This is a VIEWING harness (browser = geometry preview, NOT Outlook). The actual D6 decision is made
by opening the round-tripped .oft in classic Outlook -- compare.html spells out that step.
"""

from __future__ import annotations

import html
import os
import shutil
import warnings
from pathlib import Path
from typing import Optional

from . import bakeoff as _bakeoff
from .preview import render_preview

# The bake-off policies, in the order we show them (live -> hybrid -> raster = most-editable to
# most-pixel-locked), so the columns read as a fidelity spectrum.
_POLICY_ORDER = ("live", "hybrid", "raster")

_POLICY_BLURB = {
    "live": "All text stays live/editable. Most flexible, least brand-font fidelity.",
    "hybrid": "Brand headlines are pictures; everything else live. The presumptive default.",
    "raster": "Headlines aggressively rasterized; merge/CTA/body still live (always).",
}


def _find_proof_jpg(psd_path: str) -> Optional[Path]:
    """The PSD's sibling rendered proof (same folder, a .jpg/.jpeg) -- ground truth to compare
    against. Returns the first match or None."""
    d = Path(psd_path).resolve().parent
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in (".jpg", ".jpeg") and p.is_file():
            return p
    return None


def _panel(title: str, subtitle: str, body_html: str) -> str:
    return (
        '<div class="col">'
        f'<div class="phead"><span class="ptitle">{html.escape(title)}</span>'
        f'<span class="psub">{html.escape(subtitle)}</span></div>'
        f'<div class="scaler">{body_html}</div>'
        "</div>"
    )


def _report_line(report: dict) -> str:
    bits = []
    for p in report.get("policies", []):
        score = p.get("oracle_score")
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
        bits.append(f"{p['policy']}: oracle {score_s}, protected_rastered {p.get('protected_rasterized')}")
    clean = report.get("editability_clean")
    geom = report.get("geometry_identical")
    return f"editability_clean={clean} · geometry_identical={geom} · " + " | ".join(bits)


def _compare_html(psd_name: str, report: dict, has_proof: bool) -> str:
    panels = []
    if has_proof:
        panels.append(_panel("Ground truth (PSD proof)", "the designer's rendered .jpg",
                             '<img src="proof.jpg" alt="PSD proof">'))
    panels.append(_panel("Preview (extraction)", "truth pixels + our region boxes",
                         '<iframe class="frame" src="preview/preview.html"></iframe>'))
    for policy in _POLICY_ORDER:
        panels.append(_panel(policy, _POLICY_BLURB.get(policy, ""),
                             f'<iframe class="frame" src="{policy}/index.html"></iframe>'))

    oft_cmd = (
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        "Tools\\PSD-HTML\\grammar\\Convert-HtmlToOft.ps1 "
        "-HtmlPath &lt;policy&gt;\\index.html"
    )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>Compare - {html.escape(psd_name)}</title>"
        "<style>"
        ":root{color-scheme:light dark;}"
        "*{box-sizing:border-box;}"
        "body{margin:0;font:13px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f4f6;color:#1a1a1e;}"
        "@media(prefers-color-scheme:dark){body{background:#16161a;color:#e8e8ea;}.bar,.panel{background:#202028;}}"
        ".bar{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid rgba(128,128,128,.3);padding:12px 18px;}"
        ".bar h1{font-size:15px;margin:0 0 4px;}"
        ".bar .rep{font:12px/1.4 ui-monospace,Consolas,monospace;opacity:.85;}"
        ".bar .note{font-size:12px;opacity:.7;margin-top:6px;}"
        ".bar code{background:rgba(128,128,128,.15);padding:1px 5px;border-radius:4px;}"
        # side-by-side columns that SHARE the viewport width (no horizontal page scroll); each
        # email is scaled to fit its column by fit() below, so the whole email shows with no inner
        # box scrollbars. The page scrolls vertically once, all columns aligned.
        ".row{display:flex;gap:10px;padding:12px;align-items:flex-start;}"
        ".col{flex:1 1 0;min-width:0;background:#fff;border:1px solid rgba(128,128,128,.25);border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1);}"
        ".phead{padding:8px 12px;border-bottom:1px solid rgba(128,128,128,.2);}"
        ".ptitle{font-weight:650;text-transform:capitalize;font-size:14px;}"
        ".psub{display:block;font-size:11px;opacity:.65;}"
        ".scaler{overflow:hidden;background:#fff;}"
        ".scaler img{display:block;width:100%;height:auto;}"
        ".frame{border:0;background:#fff;display:block;transform-origin:top left;}"
        "</style></head><body>"
        '<div class="bar">'
        f"<h1>Compare &mdash; {html.escape(psd_name)}</h1>"
        f'<div class="rep">{html.escape(_report_line(report))}</div>'
        '<div class="note">Browser = GEOMETRY preview, not Outlook. Panels sit side by side; scroll down within the page (proof, preview, live, hybrid, raster). '
        f"For the real D6 read, round-trip each bundle to .oft on this box (<code>{oft_cmd}</code>), open in CLASSIC Outlook, "
        "confirm every fill-in field is editable, compare the look, and record the default in BAKEOFF_RUNBOOK.md.</div>"
        "</div>"
        f'<div class="row">{"".join(panels)}</div>'
        "<script>"
        "function fit(){var cols=document.querySelectorAll('.col');"
        "for(var i=0;i<cols.length;i++){var col=cols[i];"
        "var frame=col.querySelector('iframe.frame');var sc=col.querySelector('.scaler');"
        "if(!frame||!sc){continue;}"
        "var cw=sc.clientWidth;var scale=cw/640;var h=2200;"
        "try{var d=frame.contentDocument;if(d&&d.body){h=Math.max(d.body.scrollHeight,d.documentElement.scrollHeight)||2200;}}catch(e){}"
        "frame.style.width='640px';frame.style.height=h+'px';frame.style.transform='scale('+scale+')';"
        "sc.style.height=(h*scale)+'px';}}"
        "window.addEventListener('resize',fit);"
        "var _fr=document.querySelectorAll('iframe.frame');"
        "for(var j=0;j<_fr.length;j++){_fr[j].addEventListener('load',fit);}"
        "window.addEventListener('load',function(){setTimeout(fit,200);setTimeout(fit,800);});"
        "</script>"
        "</body></html>"
    )


def build_comparison(psd_path: str, out_dir: str, *, density: float = 1.0) -> dict:
    """Emit the bake-off (3 policy bundles + runbook) + the preview + a compare.html into out_dir,
    copying the PSD's proof .jpg if present. Returns {compare_path, report, proof, preview_path}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    report = _bakeoff.run(psd_path, out, density=density)
    preview = render_preview(psd_path, str(out / "preview"))

    proof = _find_proof_jpg(psd_path)
    has_proof = False
    if proof is not None:
        try:
            shutil.copyfile(proof, out / "proof.jpg")
            has_proof = True
        except OSError as exc:
            has_proof = False
            warnings.warn(
                f"compare: could not copy PSD proof {proof!s} to {(out / 'proof.jpg')!s}: {exc!r} "
                "-- compare.html will omit the ground-truth panel (this is an I/O failure, not a "
                "genuinely proof-less PSD)",
                stacklevel=2,
            )

    psd_name = os.path.basename(psd_path)
    compare_path = out / "compare.html"
    compare_path.write_text(_compare_html(psd_name, report, has_proof), encoding="utf-8")

    return {
        "compare_path": str(compare_path),
        "report": report,
        "proof": str(proof) if proof else None,
        "preview_path": preview["preview_path"],
    }
