"""A thin FAIL-LOUD lint over an EMITTED BUNDLE FOLDER.

This module sits at the S2 output boundary -- the same one `html_emitter.emit()` writes to. It
never transforms a bundle, only inspects one that already exists on disk and reports whether it
is OFT-safe (see the classic-Outlook Word-engine grammar in `html_emitter`'s module docstring).

Checks performed by `validate_bundle()` (each violation names the offending file/line/reference):
  1. `index.html` is present at the bundle root and UTF-8-decodable.
  2. Every file under the bundle root (other than the six bundle-contract files `index.html`,
     `_bundle_manifest.json`, `regions.json`, `gate_report.json`, `gate_render.png`,
     `links_report.json`) has a raster extension in `ALLOWED_ASSET_EXTS` (png/jpg/jpeg/gif) --
     ZERO `.svg` anywhere, and no other format (webp, bmp, ...).
  3. Every raster reference inside `index.html` uses ONLY the `<img src="...">` or inline
     `style="...url(...)"` grammar -- `srcset`, `<link>`, `@import`, a `<style>` block, the legacy
     HTML `background="..."` attribute, CSS `background-image:` (probe-verified blank in classic
     Outlook), and VML (`<v:...>` tags) are each a distinct, named violation.
  4. Every asset path referenced from `index.html` is relative (no absolute path, no drive
     letter, no `..` traversal) and resolves to a real file under the bundle root.
  5. The classic-Outlook DPI-lock `<xml>` block (`html_emitter.DPI_LOCK_BLOCK`) is present
     verbatim.
  6. The bundle manifest, when present, is well-formed: `_bundle_manifest.json` must be valid JSON
     with `schema_version == 1` and the required keys `entry_html`/`assets`/`bundle_hash` (each a
     distinct violation). A MISSING `_bundle_manifest.json` or `regions.json` is a warning, not a
     hard fail.

`validate_bundle(bundle_dir)` NEVER raises -- it always returns
`{"pass": bool, "violations": [...], "warnings": [...]}`. `assert_bundle(bundle_dir)` is the
fail-loud gate: it calls `validate_bundle` and raises `ConformanceError` (naming every violation)
if `pass` is False, otherwise returns the same result dict.

`write_manifest(bundle_dir)` computes and writes `_bundle_manifest.json`
(`{schema_version, entry_html, assets, bundle_hash}`) at the bundle root -- the SAME field names
and the same stable-content-hash spirit as the downstream consumer
`Reference/Creative QA System/Service/bundle_intake.py` (`unpack_and_verify_bundle` /
`compute_bundle_hash`). That module is NOT imported here: it lives in a different, unrelated
top-level project tree (a separate service package with its own `store`/`quickxor` dependencies
that are not on this package's import path), so its exact rules are reimplemented locally --
documented duplication, not an oversight. The asset-discovery walk reuses the same regex grammar
`validate_bundle` already enforces, and the hash algorithm (sha256 over `relpath + "\0" +
sha256(file bytes) + "\n"`, reduced over the sorted relpath set) is the same one
`html_emitter._write_bundle_manifest` already uses for its own manifest, so re-running
`write_manifest` against an emitter-produced bundle reproduces the identical hash.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Optional

from .html_emitter import DPI_LOCK_BLOCK

# Rasters this bundle grammar ever ships -- everything else (webp, bmp, svg, ...) is a violation
# whether or not it is ever referenced from index.html.
ALLOWED_ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif")

# Root-level filenames that are part of the bundle CONTRACT, not raster assets -- never subject to
# the "png/jpg/gif only" extension sweep.
_NON_ASSET_ROOT_FILES = {
    "index.html", "_bundle_manifest.json", "regions.json",
    # fidelity-gate instrumentation (fidelity_gate.gate_bundle writes its report + render
    # screenshot into the bundle it verified) -- metadata, never shipped assets.
    "gate_report.json", "gate_render.png",
    # link reconciliation report (html_emitter.emit) -- metadata, never a shipped asset.
    "links_report.json",
    # per-run observability summary (cli emit -> run_report.RunSummary) -- metadata, never shipped.
    "run_summary.json",
}

# A local (non-external) reference never needs resolving -- these prefixes point off-bundle
# (network, inline data, mail, phone, a same-page anchor) or are not a filesystem path at all.
_EXTERNAL_PREFIXES = ("http://", "https://", "//", "data:", "mailto:", "tel:", "#")

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r"""src\s*=\s*(["'])(?P<url>[^"']*)\1""", re.IGNORECASE)
_HREF_ATTR_RE = re.compile(r"""href\s*=\s*(["'])(?P<url>[^"']*)\1""", re.IGNORECASE)
_URL_FUNC_RE = re.compile(r"""url\(\s*(["']?)(?P<url>[^"')]*)\1\s*\)""", re.IGNORECASE)

_SRCSET_RE = re.compile(r"\bsrcset\s*=", re.IGNORECASE)
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_STYLE_TAG_RE = re.compile(r"<style\b", re.IGNORECASE)
_IMPORT_RE = re.compile(r"@import\b", re.IGNORECASE)
# The legacy `background=` HTML attribute is forbidden. The value may be quoted OR unquoted --
# `<td background=x.png>` is valid HTML and classic Outlook's Word engine honors it, so the check
# must catch both forms (an earlier `[\"']`-only pattern let the unquoted form slip the gate).
# ANCHORED to a real tag attribute (`<...\sbackground=`): a bare `background=` substring living
# inside an attribute VALUE -- e.g. an href query string like `?background=1` -- is NOT the
# forbidden attribute and must not trip the gate (that unanchored false positive hard-rejected
# otherwise-valid bundles).
_BACKGROUND_ATTR_RE = re.compile(r"<[^>]*\sbackground\s*=\s*(?:[\"']|[^\s>\"'])", re.IGNORECASE)
_VML_TAG_RE = re.compile(r"<v:\w+", re.IGNORECASE)
# CSS `background-image` is OUTSIDE Grammar G: probe-verified 2026-07-09 on a live classic-Outlook
# box that the Word engine paints neither CSS background-image nor the legacy background= attribute
# (both render blank while every Chromium-class engine paints them) -- a bundle carrying it would
# preview one way and ship another. Flat fills belong in bgcolor + background-color.
_BACKGROUND_IMAGE_RE = re.compile(r"background-image\s*:", re.IGNORECASE)


def _is_data_svg(ref: str) -> bool:
    """True iff `ref` is a `data:` URI whose media type is SVG (e.g. `data:image/svg+xml;...`).
    Inline SVG markup shipped via a data URI is banned by the same zero-`.svg` contract that bans a
    `.svg` file reference -- but a data URI would otherwise short-circuit past the extension check as
    an `_EXTERNAL_PREFIXES` reference. Inspect only the media-type header (before the first `,`) so a
    base64 payload that merely happens to contain the letters `svg` is not a false positive."""
    low = str(ref or "").strip().lower()
    if not low.startswith("data:"):
        return False
    header = low[len("data:"):].split(",", 1)[0]
    return "svg" in header


class ConformanceError(RuntimeError):
    """FAIL LOUD: raised by `assert_bundle()` when `validate_bundle()` reports `pass: False`. The
    message joins every violation's own message so a single caught exception still names every
    offending file/line/reference."""


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _violation(vtype: str, message: str, *, file: Optional[str] = None, ref: Optional[str] = None, line: Optional[int] = None) -> dict:
    entry = {"type": vtype, "message": message}
    if file is not None:
        entry["file"] = file
    if ref is not None:
        entry["ref"] = ref
    if line is not None:
        entry["line"] = line
    return entry


def _is_safe_relative_path(path_str: str) -> bool:
    """True iff `path_str` is a bare, non-traversing relative path: no leading `/`, no drive
    letter/URI-scheme colon in its first segment, no `..` or empty segment anywhere. Mirrors the
    downstream `bundle_intake.is_safe_relative_path` rule (reimplemented locally -- see module
    docstring)."""
    if not path_str:
        return False
    s = str(path_str).strip().replace("\\", "/")
    if not s or s.startswith("/"):
        return False
    segments = s.split("/")
    if ":" in segments[0]:
        return False
    return all(seg not in ("", ".", "..") for seg in segments)


def _strip_fragment_query(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0].strip()


# Table for `_scan_forbidden_grammar`: (regex, vtype, fixed_ref_or_None, message_body).
# When `fixed_ref` is None, the violation's `ref` is the matched text itself (`m.group(0)`) and
# `message_body` is a template consuming `{found!r}` (also the matched text); otherwise `ref` is
# the fixed string as-is and `message_body` is used verbatim (no `.format()` call, so a message
# containing an unrelated literal `{`/`}` -- none do today -- would still be safe).
_FORBIDDEN_GRAMMAR_RULES = (
    (_SRCSET_RE, "srcset_forbidden", "srcset",
     "`srcset` is forbidden -- rasters must be referenced only via <img src> or inline style "
     "url(...)"),
    (_LINK_TAG_RE, "link_tag_forbidden", None,
     "a <link> tag is forbidden (found {found!r}) -- no <link rel=stylesheet> or any other "
     "<link> may ship"),
    (_STYLE_TAG_RE, "style_block_forbidden", "<style>",
     "a <style> block is forbidden -- every style must be an inline `style=\"...\"` attribute"),
    (_IMPORT_RE, "import_forbidden", "@import",
     "`@import` is forbidden"),
    (_BACKGROUND_ATTR_RE, "background_attribute_forbidden", "background=",
     "the legacy `background=` HTML attribute is forbidden (blank in classic Outlook, "
     "probe-verified) -- use a bgcolor flat fill or an <img> row"),
    (_BACKGROUND_IMAGE_RE, "background_image_forbidden", "background-image",
     "CSS `background-image` is outside Grammar G -- classic Outlook's Word engine never paints "
     "it (probe-verified), so the band would render blank for recipients; use a bgcolor flat "
     "fill or an <img> row"),
    (_VML_TAG_RE, "vml_forbidden", None,
     "VML markup is forbidden (found {found!r})"),
)


def _scan_forbidden_grammar(html_text: str, entry_name: str) -> list:
    """Every occurrence of a forbidden HTML/CSS construct in `html_text`, each its own precise,
    line-numbered violation."""
    violations = []
    for regex, vtype, fixed_ref, message_body in _FORBIDDEN_GRAMMAR_RULES:
        for m in regex.finditer(html_text):
            line = _line_no(html_text, m.start())
            if fixed_ref is None:
                ref = m.group(0)
                message = message_body.format(found=m.group(0))
            else:
                ref = fixed_ref
                message = message_body
            violations.append(
                _violation(
                    vtype, f"{entry_name}:{line}: {message}",
                    file=entry_name, ref=ref, line=line,
                )
            )
    return violations


def _resolve_asset_ref(
    url: str, *, root: Path, entry_name: str, match_start: int, html_text: str, kind: str
) -> tuple:
    """Validate one candidate raster reference (an `<img src=...>` value or a style `url(...)`
    value). Returns `(violations, resolved_relpath_or_None)`."""
    violations = []
    clean = _strip_fragment_query(url)
    line = _line_no(html_text, match_start)
    if _is_data_svg(clean):
        violations.append(
            _violation(
                "svg_reference",
                f"{entry_name}:{line}: {kind} embeds inline SVG via a data: URI (banned by the "
                f"zero-svg contract): {url!r}",
                file=entry_name, ref=url, line=line,
            )
        )
        return violations, None
    if not clean or clean.lower().startswith(_EXTERNAL_PREFIXES):
        return violations, None
    if clean.lower().endswith(".svg"):
        violations.append(
            _violation(
                "svg_reference",
                f"{entry_name}:{line}: {kind} references a forbidden .svg asset: {url!r}",
                file=entry_name, ref=url, line=line,
            )
        )
        return violations, None
    if not _is_safe_relative_path(clean):
        violations.append(
            _violation(
                "unsafe_asset_path",
                f"{entry_name}:{line}: {kind} reference {url!r} is not a safe relative path "
                "(absolute path, drive letter, or '..' traversal)",
                file=entry_name, ref=url, line=line,
            )
        )
        return violations, None
    candidate = (root / clean).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        violations.append(
            _violation(
                "asset_out_of_root",
                f"{entry_name}:{line}: {kind} reference {url!r} resolves outside the bundle root",
                file=entry_name, ref=url, line=line,
            )
        )
        return violations, None
    if not candidate.is_file():
        violations.append(
            _violation(
                "asset_missing",
                f"{entry_name}:{line}: {kind} reference {url!r} does not resolve to a real file "
                "under the bundle root",
                file=entry_name, ref=url, line=line,
            )
        )
        return violations, None
    if candidate.suffix.lower() not in ALLOWED_ASSET_EXTS:
        violations.append(
            _violation(
                "disallowed_asset_extension",
                f"{entry_name}:{line}: {kind} reference {url!r} has extension "
                f"{candidate.suffix!r}, not one of {ALLOWED_ASSET_EXTS}",
                file=entry_name, ref=url, line=line,
            )
        )
    relpath = candidate.relative_to(root.resolve()).as_posix()
    return violations, relpath


def _scan_asset_references(html_text: str, root: Path, entry_name: str) -> tuple:
    """Walk every `<img src=...>` and inline-style `url(...)` reference plus (for the `.svg`
    check only) every `href=...`. Returns `(violations, referenced_relpaths_set)`."""
    violations: list = []
    referenced: set = set()

    for img_match in _IMG_TAG_RE.finditer(html_text):
        tag = img_match.group(0)
        src_match = _SRC_ATTR_RE.search(tag)
        if src_match is None:
            violations.append(
                _violation(
                    "img_missing_src",
                    f"{entry_name}:{_line_no(html_text, img_match.start())}: an <img> tag has no "
                    f"src attribute (found {tag!r})",
                    file=entry_name, ref=tag, line=_line_no(html_text, img_match.start()),
                )
            )
            continue
        abs_start = img_match.start() + src_match.start("url")
        v, relpath = _resolve_asset_ref(
            src_match.group("url"), root=root, entry_name=entry_name, match_start=abs_start,
            html_text=html_text, kind="<img src>",
        )
        violations.extend(v)
        if relpath:
            referenced.add(relpath)

    for url_match in _URL_FUNC_RE.finditer(html_text):
        v, relpath = _resolve_asset_ref(
            url_match.group("url"), root=root, entry_name=entry_name, match_start=url_match.start("url"),
            html_text=html_text, kind="inline style url()",
        )
        violations.extend(v)
        if relpath:
            referenced.add(relpath)

    # href is not part of the raster-reference grammar at all (it is how a live text/CTA region
    # becomes clickable) -- the ONLY thing worth checking on it is the absolute .svg ban, since an
    # href pointing at a raster asset would be a grammar violation of a different (rarer) shape
    # that the corpus never produces; an external/mailto/anchor href is completely legitimate.
    for href_match in _HREF_ATTR_RE.finditer(html_text):
        url = href_match.group("url")
        clean = _strip_fragment_query(url)
        line = _line_no(html_text, href_match.start("url"))
        if _is_data_svg(clean):
            violations.append(
                _violation(
                    "svg_reference",
                    f"{entry_name}:{line}: <a href> embeds inline SVG via a data: URI (banned by "
                    f"the zero-svg contract): {url!r}",
                    file=entry_name, ref=url, line=line,
                )
            )
            continue
        if not clean or clean.lower().startswith(_EXTERNAL_PREFIXES):
            continue
        if clean.lower().endswith(".svg"):
            violations.append(
                _violation(
                    "svg_reference",
                    f"{entry_name}:{line}: <a href> references a forbidden .svg asset: {url!r}",
                    file=entry_name, ref=url, line=line,
                )
            )

    return violations, referenced


def _scan_filesystem_assets(root: Path) -> list:
    """Every file under `root` (excluding the bundle-contract files) must have a raster extension
    -- present or not referenced, an off-grammar file sitting in the bundle is still a violation."""
    violations = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relpath = path.relative_to(root).as_posix()
        if relpath in _NON_ASSET_ROOT_FILES:
            continue
        suffix = path.suffix.lower()
        if suffix == ".svg":
            violations.append(
                _violation(
                    "svg_reference",
                    f"{relpath}: a .svg file is present in the bundle -- zero .svg may ship, "
                    "even unreferenced",
                    file=relpath,
                )
            )
        elif suffix not in ALLOWED_ASSET_EXTS:
            violations.append(
                _violation(
                    "disallowed_asset_extension",
                    f"{relpath}: file extension {suffix!r} is not one of {ALLOWED_ASSET_EXTS} "
                    "-- rasters must be png/jpg/gif only",
                    file=relpath,
                )
            )
    return violations


def _dedupe(violations: list) -> list:
    seen = set()
    out = []
    for v in violations:
        key = (v.get("type"), v.get("file"), v.get("ref"), v.get("line"))
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def validate_bundle(bundle_dir) -> dict:
    """Lint the emitted bundle folder at `bundle_dir` against the OFT-safe grammar. Never raises
    -- any unexpected internal failure is folded into a `violations` entry instead (so a caller
    that only wants a report, not an exception, always gets one). Returns
    `{"pass": bool, "violations": [...], "warnings": [...]}`."""
    violations: list = []
    warnings: list = []
    try:
        root = Path(bundle_dir)
        if not root.is_dir():
            violations.append(
                _violation("missing_bundle_dir", f"bundle directory does not exist: {root}", file=str(root))
            )
            return {"pass": False, "violations": violations, "warnings": warnings}

        entry_path = root / "index.html"
        html_text: Optional[str] = None
        if not entry_path.is_file():
            violations.append(
                _violation("missing_entry", "no index.html found at the bundle root", file="index.html")
            )
        else:
            raw = entry_path.read_bytes()
            try:
                html_text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                violations.append(
                    _violation(
                        "entry_not_utf8",
                        f"index.html is not valid UTF-8: {exc}",
                        file="index.html",
                    )
                )

        if html_text is not None:
            if DPI_LOCK_BLOCK not in html_text:
                violations.append(
                    _violation(
                        "missing_dpi_lock_block",
                        "index.html is missing the classic-Outlook DPI-lock <xml> block "
                        "(html_emitter.DPI_LOCK_BLOCK) -- Word will silently rescale px measurements",
                        file="index.html",
                    )
                )
            violations.extend(_scan_forbidden_grammar(html_text, "index.html"))
            ref_violations, _referenced = _scan_asset_references(html_text, root, "index.html")
            violations.extend(ref_violations)

        violations.extend(_scan_filesystem_assets(root))

        manifest_path = root / "_bundle_manifest.json"
        if not manifest_path.is_file():
            warnings.append({"type": "manifest_missing", "message": "_bundle_manifest.json not found at bundle root"})
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                violations.append(
                    _violation("invalid_manifest_json", f"_bundle_manifest.json is not valid JSON: {exc}", file="_bundle_manifest.json")
                )
            else:
                if manifest.get("schema_version") != 1:
                    violations.append(
                        _violation(
                            "invalid_manifest",
                            f"_bundle_manifest.json schema_version is {manifest.get('schema_version')!r}, expected 1",
                            file="_bundle_manifest.json",
                        )
                    )
                for key in ("entry_html", "assets", "bundle_hash"):
                    if key not in manifest:
                        violations.append(
                            _violation(
                                "invalid_manifest",
                                f"_bundle_manifest.json is missing required key {key!r}",
                                file="_bundle_manifest.json",
                            )
                        )

        if not (root / "regions.json").is_file():
            warnings.append({"type": "regions_missing", "message": "regions.json not found at bundle root"})

        violations = _dedupe(violations)
    except Exception as exc:  # pragma: no cover -- defensive: validate_bundle() must never raise
        violations.append(_violation("internal_error", f"conformance_validator internal error: {exc!r}"))

    return {"pass": len(violations) == 0, "violations": violations, "warnings": warnings}


def assert_bundle(bundle_dir) -> dict:
    """The fail-loud gate: run `validate_bundle(bundle_dir)` and raise `ConformanceError` (naming
    every violation) if it did not pass. Returns the same result dict on success."""
    result = validate_bundle(bundle_dir)
    if not result["pass"]:
        joined = "; ".join(v["message"] for v in result["violations"])
        raise ConformanceError(f"bundle at {bundle_dir!s} failed conformance ({len(result['violations'])} violation(s)): {joined}")
    return result


def _discover_entry_html(root: Path) -> Path:
    preferred = root / "index.html"
    if preferred.is_file():
        return preferred
    candidates = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".html", ".htm") and p.name != "_bundle_manifest.json"
    )
    if not candidates:
        raise ConformanceError(f"bundle at {root!s} contains no HTML file to build a manifest from")
    if len(candidates) > 1:
        names = ", ".join(c.relative_to(root).as_posix() for c in candidates)
        raise ConformanceError(
            f"bundle at {root!s} contains multiple HTML files with no 'index.html' to disambiguate: {names}"
        )
    return candidates[0]


def _referenced_assets(html_text: str, root: Path) -> list:
    """Every LOCAL raster asset `html_text` references via `<img src>` or an inline style
    `url(...)` -- the same grammar `validate_bundle` enforces, used here purely for manifest
    content discovery (not re-validated; call `validate_bundle`/`assert_bundle` first if you need
    the safety guarantee)."""
    referenced: set = set()

    def _add_local(raw_url: str) -> None:
        url = _strip_fragment_query(raw_url)
        if url and not url.lower().startswith(_EXTERNAL_PREFIXES) and _is_safe_relative_path(url):
            candidate = (root / url).resolve()
            if candidate.is_file():
                # A path outside root (relative_to raises) simply isn't a local bundle asset.
                with contextlib.suppress(ValueError):
                    referenced.add(candidate.relative_to(root.resolve()).as_posix())

    for img_match in _IMG_TAG_RE.finditer(html_text):
        src_match = _SRC_ATTR_RE.search(img_match.group(0))
        if src_match is None:
            continue
        _add_local(src_match.group("url"))
    for url_match in _URL_FUNC_RE.finditer(html_text):
        _add_local(url_match.group("url"))
    return sorted(referenced)


def write_manifest(bundle_dir) -> dict:
    """Compute and write `_bundle_manifest.json` at `bundle_dir`'s root:
    `{"schema_version": 1, "entry_html": <relpath>, "assets": [...sorted relpaths...],
    "bundle_hash": <sha256 hex>}` -- same field names as the downstream
    `bundle_intake.unpack_and_verify_bundle` consumer, and the same hash algorithm
    `html_emitter._write_bundle_manifest` already uses (sha256 over each sorted relpath's own
    sha256 file-content digest, so re-running this against an unchanged bundle reproduces the
    identical hash). Raises `ConformanceError` if no HTML entry point can be found."""
    import hashlib

    root = Path(bundle_dir)
    entry_path = _discover_entry_html(root)
    entry_relpath = entry_path.relative_to(root).as_posix()
    html_text = entry_path.read_text(encoding="utf-8", errors="replace")
    asset_relpaths = _referenced_assets(html_text, root)

    relpaths = sorted({entry_relpath, *asset_relpaths})
    digest = hashlib.sha256()
    for relpath in relpaths:
        file_hash = hashlib.sha256((root / relpath).read_bytes()).hexdigest()
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("utf-8"))
        digest.update(b"\n")

    manifest = {
        "schema_version": 1,
        "entry_html": entry_relpath,
        "assets": asset_relpaths,
        "bundle_hash": digest.hexdigest(),
    }
    (root / "_bundle_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest
