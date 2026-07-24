"""CLI: `dump`, `analyze`, `build`, `validate`, `route`, `linkgen`, `emit`, `bakeoff`, `preview`, `gate`, `compare` (see build_parser())."""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

from .psd_adapter import psd_to_layout_tree
from .run_report import (
    RunSummary,
    configure_logging,
    get_logger,
    new_run_id,
    operator_unexpected_message,
)

# The canonical definition is `layer_router.POLICIES` -- mirrored here as a plain literal only
# because argparse needs `choices` at parse time, before this module's lazy per-command imports
# run (the lazy-import pattern keeps `dump`/`analyze` usable even while a downstream builder's
# module is mid-development; `layer_router` itself is lightweight/import-safe, but staying
# consistent with that pattern costs nothing here).
_POLICY_CHOICES = ("live", "hybrid", "raster")


def _write_or_print(text: str, output: str | None, detail: str | None = None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {output}" + (f" ({detail})" if detail else ""))
    else:
        print(text)


def _cmd_dump(args: argparse.Namespace) -> int:
    tree = psd_to_layout_tree(args.psd)
    payload = tree.to_dict()
    text = json.dumps(payload, indent=2)
    _write_or_print(text, args.output)
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    # Imported lazily so `dump` works even before grid_analyzer exists / while it's under
    # active development by another builder.
    from .grid_analyzer import analyze_layout_tree

    tree = psd_to_layout_tree(args.psd)
    report = analyze_layout_tree(tree, model=args.model)
    text = json.dumps(report, indent=2)
    _write_or_print(text, args.output)
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    # Imported lazily, same rationale as `analyze`: `build` works even while the classifier/
    # solver are under active development by another builder.
    from .intake_validator import validate_tree
    from .table_solver import SafetyInvariantViolation, build_table_trees
    from .table_tree import trees_to_json

    tree = psd_to_layout_tree(args.psd)

    # S1.1 Defect 3: run the SAME fail-loud SOP gate `validate` uses, on the build path, so `build`
    # and `validate` can never disagree about one PSD. Runs on the already-loaded tree (not a
    # second `validate_psd(args.psd)` call) to avoid re-parsing the PSD file twice.
    report = validate_tree(tree)
    if not report["pass"]:
        print("REFUSED -- intake validator reports SOP violations (SOP #8); run `validate` for the full report:", file=sys.stderr)
        for v in report["violations"]:
            print(f"  - [{v['type']}] {v['artboard']!r}: {v['message']}", file=sys.stderr)
        return 1

    try:
        trees = build_table_trees(tree, email_override=args.email)
    except SafetyInvariantViolation as exc:
        # Defense in depth: the validator gate above should already have refused any PSD that
        # would trip this. FAIL LOUD anyway -- never write a TableTree with an editable field
        # trapped in a graphic/button cell. Run `validate` on this PSD for the full report naming
        # every offending layer.
        print(f"REFUSED -- safety invariant violated: {exc}", file=sys.stderr)
        for v in exc.violations:
            print(f"  - {v['email']!r}: layers={v['layer_ids']} rect={v['rect']}", file=sys.stderr)
        return 1
    text = trees_to_json(trees)
    _write_or_print(text, args.output, detail=f"{len(trees)} email(s)")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    # Imported lazily, same rationale as `build`.
    from .intake_validator import validate_psd

    report = validate_psd(args.psd)
    text = json.dumps(report, indent=2)
    _write_or_print(text, args.output, detail="PASS" if report["pass"] else "FAIL")
    return 0 if report["pass"] else 1


def _cmd_linkgen(args: argparse.Namespace) -> int:
    # Imported lazily, same rationale as `build`/`validate`.
    from .link_scaffold import write_starter_manifest

    try:
        out_path = write_starter_manifest(
            args.psd, args.output, force=args.force,
            email_override=args.email, email_index=args.email_index, policy=args.policy,
        )
    except FileExistsError as exc:
        print(f"REFUSED -- {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"REFUSED -- {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out_path}")
    return 0


def _cmd_route(args: argparse.Namespace) -> int:
    # Imported lazily, same rationale as `build`/`validate`.
    from .layer_router import iter_routed, render_role, route
    from .table_solver import build_table_trees

    layout = psd_to_layout_tree(args.psd)
    trees = build_table_trees(layout, email_override=args.email)
    if not trees:
        print(f"REFUSED -- {args.psd} solved to zero artboards/emails", file=sys.stderr)
        return 1
    tree = trees[0]
    routed = route(tree, args.policy)

    summary = {
        "psd": args.psd,
        "email": tree.email,
        "policy": args.policy,
        "width": tree.width,
        "num_emails_in_psd": len(trees),
        "regions": [
            {"key": list(key), "role": render_role(cell), "verb": verb}
            for cell, key, verb in iter_routed(routed)
        ],
    }
    text = json.dumps(summary, indent=2)
    _write_or_print(text, args.output, detail=f"{len(summary['regions'])} region(s)")
    return 0


def _resolve_density(args: argparse.Namespace, canvas_width: int) -> float:
    """Resolve the emit density from --config / --email-width / --density + the PSD canvas width,
    printing the resolved value + its source (and any author_width mismatch warning) to stderr so a
    run is never ambiguous about the scale it used. Raises config.ConfigError on a malformed config."""
    from .config import load_config, resolve_density

    cfg = load_config(getattr(args, "config", None))
    density, source, warning = resolve_density(
        cfg,
        canvas_width,
        cli_density=getattr(args, "density", None),
        cli_email_width=getattr(args, "email_width", None),
    )
    print(f"density {density:g} (source: {source}) for email width {int(canvas_width)}px", file=sys.stderr)
    if warning:
        print(f"  WARNING: {warning}", file=sys.stderr)
    return density


def _warn_texts(warnings) -> list[str]:
    """Flatten html_emitter warnings (dicts or strings) to short human lines for the run summary."""
    out = []
    for w in warnings or []:
        if isinstance(w, dict):
            reason = w.get("reason") or w.get("message") or ""
            out.append(f"{w.get('type', 'warning')}: {reason}".rstrip(": "))
        else:
            out.append(str(w))
    return out


def _cmd_emit(args: argparse.Namespace) -> int:
    # Imported lazily, same rationale as `build`/`validate`.
    from .config import ConfigError
    from .html_emitter import HtmlEmitterError, emit
    from .layer_router import EditabilityViolation, route
    from .rasterizer import composite_psd
    from .table_solver import SafetyInvariantViolation, build_table_trees

    # S1 observability: every run gets an id-tagged logger and a run_summary.json written on the way
    # out (success OR failure), so a run leaves a readable trace. Degrade warnings fired during the
    # run (missing font, unreadable layer, dropped band) are captured into the summary too, so a
    # quietly-lower-fidelity run is no longer silent. Diagnostics still print to stderr (the CLI's
    # human output, which the GUI streams); the summary is the machine-readable record.
    summary = RunSummary(new_run_id())
    configure_logging(run_id=summary.run_id)
    log = get_logger(__name__)
    log.info("emit start: psd=%r out=%r policy=%s", args.psd, args.output, args.policy)
    caught: list = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            layout = psd_to_layout_tree(args.psd)
            layer_names = {l.id: l.name for l in layout.layers}
            with summary.stage("solve"):
                trees = build_table_trees(layout, email_override=args.email)
            if not trees:
                summary.fail("solve", f"{args.psd} solved to zero artboards/emails")
                print(f"REFUSED -- {args.psd} solved to zero artboards/emails", file=sys.stderr)
                return 1
            idx = int(getattr(args, "email_index", 0) or 0)
            if idx < 0 or idx >= len(trees):
                summary.fail("solve", f"--email-index {idx} out of range ({len(trees)} email(s))")
                print(f"REFUSED -- --email-index {idx} out of range; this PSD solved to {len(trees)} email(s):",
                      file=sys.stderr)
                for i, t in enumerate(trees):
                    print(f"  [{i}] {t.email} ({t.width}px wide)", file=sys.stderr)
                return 1
            tree = trees[idx]
            if len(trees) > 1:
                print(f"multi-email PSD: emitting [{idx}] {tree.email!r} of {len(trees)} "
                      f"(others: {', '.join(t.email for i, t in enumerate(trees) if i != idx)})",
                      file=sys.stderr)
            try:
                # Density derives from the EMAIL's own width (the artboard), never the canvas: a
                # 3-email drip sheet is a 2138px canvas of 640px artboards -- canvas-derived density
                # (3.34) scaled one email to a third of its size (live-caught on the drip corpus).
                density = _resolve_density(args, tree.width)
            except ConfigError as exc:
                summary.fail("config", f"bad config: {exc}")
                print(f"REFUSED -- bad config: {exc}", file=sys.stderr)
                return 1
            with summary.stage("route"):
                routed = route(tree, args.policy)
            composite = composite_psd(args.psd)

            link_manifest = None
            if getattr(args, "links", None):
                try:
                    with open(args.links, encoding="utf-8") as fh:
                        link_manifest = json.load(fh)
                except (OSError, json.JSONDecodeError) as exc:
                    summary.fail("links", f"bad --links manifest {args.links!r}: {exc}")
                    print(f"REFUSED -- bad --links manifest {args.links!r}: {exc}", file=sys.stderr)
                    return 1

            with summary.stage("emit") as rec:
                result = emit(
                    routed, args.output, link_manifest=link_manifest,
                    composite=composite, layer_names=layer_names, psd_path=args.psd, density=density
                )
                rec.warnings.extend(_warn_texts(result.get("warnings")))
            links = result.get("links") or {}
            if links.get("bound") is not None:
                print(f"links: {len(links['bound'])} bound, {len(links.get('unbound') or [])} unbound",
                      file=sys.stderr)
            print(f"wrote bundle to {args.output} ({len(result['assets'])} asset(s), {len(result['warnings'])} warning(s))",
                  file=sys.stderr)
            print(
                json.dumps(
                    {
                        "index_path": result["index_path"],
                        "assets": result["assets"],
                        "warnings": result["warnings"],
                        "overflow_flags": result["overflow_flags"],
                        "bundle_manifest": result["bundle_manifest"],
                    },
                    indent=2,
                )
            )
            return 0
    except SafetyInvariantViolation as exc:
        # Fail-loud #1, reachable via build_table_trees on the emit path (not just `build`): editable
        # text is trapped in a graphic/button cell. Name the layers; point at `validate` for the rest.
        print("REFUSED -- editable text is trapped in a graphic/button cell (SOP #8); "
              "run `validate` on this PSD for the full layer list:", file=sys.stderr)
        for v in exc.violations:
            print(f"  - {v['email']!r}: layers={v['layer_ids']} rect={v['rect']}", file=sys.stderr)
        return 1
    except EditabilityViolation as exc:
        # Fail-loud #2: a protected (merge/CTA/body) region would be flattened to a picture.
        print(f"REFUSED -- a protected region (merge field / CTA / body copy) would be flattened "
              f"to a picture: {exc}. Give that layer its own space (SOP #3).", file=sys.stderr)
        return 1
    except HtmlEmitterError as exc:
        print(f"REFUSED -- emit failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Flush any degrade warning fired during the run into the summary + log, so a
        # structurally-fine-but-lower-fidelity run leaves a trace instead of a silent whisper.
        for w in caught:
            summary.warn(f"{w.category.__name__}: {w.message}", logger=log)
        try:
            summary.write(Path(args.output) / "run_summary.json")
        except OSError as exc:
            log.warning("could not write run_summary.json: %s", exc)
        log.info("emit %s (run=%s, %d stage(s))", summary.result, summary.run_id, len(summary.stages))


def _cmd_bakeoff(args: argparse.Namespace) -> int:
    # Imported lazily, same rationale as `build`/`validate`.
    from .bakeoff import run as run_bakeoff
    from .config import ConfigError
    from .conformance_validator import ConformanceError
    from .table_solver import build_table_trees

    layout = psd_to_layout_tree(args.psd)
    trees = build_table_trees(layout)
    if not trees:
        print(f"REFUSED -- {args.psd} solved to zero artboards/emails", file=sys.stderr)
        return 1
    try:
        # Density derives from the baked EMAIL's own width (trees[0], the artboard), never the
        # canvas: a multi-email drip sheet is a wide canvas of narrow artboards, so a canvas-derived
        # density would mis-scale the baked email -- same rule as the emit path.
        density = _resolve_density(args, trees[0].width)
    except ConfigError as exc:
        print(f"REFUSED -- bad config: {exc}", file=sys.stderr)
        return 1
    try:
        report = run_bakeoff(args.psd, args.output, density=density)
    except ConformanceError as exc:
        print(f"REFUSED -- a bake-off bundle failed conformance: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2))
    if not report["geometry_identical"]:
        print("FAIL -- policies produced non-identical geometry (AC-201 violated)", file=sys.stderr)
        return 1
    if not report["editability_clean"]:
        print("FAIL -- a protected (merge/cta/body) region was rasterized (AC-202/205 violated)", file=sys.stderr)
        return 1
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    # Lazy import, same rationale as the others.
    from .compare import build_comparison
    from .config import ConfigError
    from .conformance_validator import ConformanceError
    from .table_solver import build_table_trees

    layout = psd_to_layout_tree(args.psd)
    trees = build_table_trees(layout)
    if not trees:
        print(f"REFUSED -- {args.psd} solved to zero artboards/emails", file=sys.stderr)
        return 1
    try:
        # Same scale rule as emit/bakeoff: density derives from the baked email's width (trees[0],
        # the artboard), never the wide canvas, so a multi-email drip sheet is not mis-scaled.
        density = _resolve_density(args, trees[0].width)
    except ConfigError as exc:
        print(f"REFUSED -- bad config: {exc}", file=sys.stderr)
        return 1
    try:
        result = build_comparison(args.psd, args.output, density=density)
    except ConformanceError as exc:
        print(f"REFUSED -- a bake-off bundle failed conformance: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {result['compare_path']}")
    print("open compare.html in a browser: proof | preview | live | hybrid | raster, side by side")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    # Lazy import, same rationale as the others. The per-region fidelity gate: emit first (or point
    # at an existing bundle), then compare truth-vs-render per region and fail loud with a list.
    from .fidelity_gate import gate_bundle

    report = gate_bundle(args.psd, args.bundle)
    if not report.get("available"):
        print(f"GATE UNAVAILABLE -- {report.get('reason')}", file=sys.stderr)
        return 2
    if args.full:
        print(json.dumps(report, indent=2))
    else:
        print(f"gate: {'PASS' if report['pass'] else 'FAIL'} "
              f"({report['counts']['fail']} fail / {report['counts']['warn']} warn "
              f"across {report['counts']['regions']} regions) -> {args.bundle}\\gate_report.json")
    if not report["pass"] and not args.full:
        for f in report["findings"]:
            if f["severity"] == "fail":
                print(f"  - [{f['check']}] {f['note']}")
    return 0 if report["pass"] else 1


def _cmd_preview(args: argparse.Namespace) -> int:
    # Lazy import (same rationale as build/emit): the faithful absolute-positioned diagnostic view,
    # independent of the OFT-safe table emitter.
    from .preview import render_preview

    result = render_preview(args.psd, args.output or "preview_out")
    print(f"wrote {result['preview_path']} ({result['artboards']} artboard(s), {len(result['assets'])} asset(s))")
    if not result["composite_available"]:
        print("  NOTE: composite unavailable -- region boxes shown over a blank ground", file=sys.stderr)
    return 0


def _add_density_args(p: argparse.ArgumentParser) -> None:
    """Shared emit-size / author-size knobs (see config.py). Flags override the config file; the
    config's `email_width` is the normal path (density = canvas_width / email_width)."""
    p.add_argument("--config", help="Path to a config JSON (else ./psd_html.config.json if present, else built-in defaults).")
    p.add_argument("--email-width", type=int, dest="email_width", help="Target email CSS width; density = canvas_width / this. Overrides config.")
    p.add_argument("--density", type=float, help="Explicit density (retina scale). Overrides --email-width and config.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="psd_html", description="PSD -> HTML front-end spike tools.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dump = sub.add_parser("dump", help="Dump a PSD's LayoutTree IR as JSON.")
    p_dump.add_argument("psd", help="Path to the .psd file.")
    p_dump.add_argument("-o", "--output", help="Write JSON to this file instead of stdout.")
    p_dump.set_defaults(func=_cmd_dump)

    p_analyze = sub.add_parser("analyze", help="Run the guillotine grid analyzer on a PSD.")
    p_analyze.add_argument("psd", help="Path to the .psd file.")
    p_analyze.add_argument("-o", "--output", help="Write JSON to this file instead of stdout.")
    p_analyze.add_argument(
        "--model",
        choices=["v1", "v2", "both"],
        default="both",
        help="v1 = raw-rect guillotine only; v2 = band-peel (full-bleed fills peeled to "
        "background before guillotining) only; both = compute and report both (default).",
    )
    p_analyze.set_defaults(func=_cmd_analyze)

    p_build = sub.add_parser("build", help="Build the nested TableTree IR (one per artboard) from a PSD.")
    p_build.add_argument("psd", help="Path to the .psd file.")
    p_build.add_argument("-o", "--output", help="Write JSON to this file instead of stdout.")
    p_build.add_argument("--email", help="Override the email name (single-artboard PSD) or prefix (multi-artboard PSD).")
    p_build.set_defaults(func=_cmd_build)

    p_validate = sub.add_parser("validate", help="Run the fail-loud SOP intake validator on a PSD.")
    p_validate.add_argument("psd", help="Path to the .psd file.")
    p_validate.add_argument("-o", "--output", help="Write the JSON report to this file instead of stdout.")
    p_validate.set_defaults(func=_cmd_validate)

    p_route = sub.add_parser("route", help="Route a PSD's first solved email and print a routing summary (per-leaf verb).")
    p_route.add_argument("psd", help="Path to the .psd file.")
    p_route.add_argument("--policy", choices=_POLICY_CHOICES, default="hybrid", help="Routing policy (default: hybrid).")
    p_route.add_argument("--email", help="Override the email name.")
    p_route.add_argument("-o", "--output", help="Write the JSON summary to this file instead of stdout.")
    p_route.set_defaults(func=_cmd_route)

    p_linkgen = sub.add_parser(
        "linkgen",
        help="Generate a starter links.json for a PSD: discovers every real link_slot/region "
        "id the pipeline recognizes (no guessing the schema) and writes a manifest with those "
        "as an advisory checklist -- the real slots/regions/inline sections start empty.",
    )
    p_linkgen.add_argument("psd", help="Path to the .psd file.")
    p_linkgen.add_argument("-o", "--output", help="Write the manifest here (default: <psd-name>.links.json next to the PSD).")
    p_linkgen.add_argument("--force", action="store_true", help="Overwrite -o if it already exists.")
    p_linkgen.add_argument("--policy", choices=_POLICY_CHOICES, default="hybrid", help="Routing policy (default: hybrid).")
    p_linkgen.add_argument("--email", help="Override the email name.")
    p_linkgen.add_argument("--email-index", type=int, default=0, dest="email_index",
                           help="Which email to scan in a multi-artboard PSD (0-based).")
    p_linkgen.set_defaults(func=_cmd_linkgen)

    p_emit = sub.add_parser("emit", help="Route + emit an OFT-safe HTML bundle for one solved email of a PSD "
                                         "(select which with --email-index; default the first).")
    p_emit.add_argument("psd", help="Path to the .psd file.")
    p_emit.add_argument("--policy", choices=_POLICY_CHOICES, default="hybrid", help="Routing policy (default: hybrid).")
    p_emit.add_argument("--email", help="Override the email name.")
    p_emit.add_argument("-o", "--output", required=True, help="Directory to write the bundle into.")
    p_emit.add_argument("--links", help="Path to a links.json manifest ({slots, regions, inline}) -- the "
                                        "deterministic source of every hyperlink; see grammar/PIPELINE.md.")
    p_emit.add_argument("--email-index", type=int, default=0, dest="email_index",
                        help="Which email to emit from a multi-artboard PSD (0-based; the tool lists "
                             "the available emails when the index is out of range).")
    _add_density_args(p_emit)
    p_emit.set_defaults(func=_cmd_emit)

    p_bakeoff = sub.add_parser(
        "bakeoff", help="Run the raster/live/hybrid fidelity bake-off for a PSD's first solved email."
    )
    p_bakeoff.add_argument("psd", help="Path to the .psd file.")
    p_bakeoff.add_argument("-o", "--output", required=True, help="Directory to write the 3 policy bundles + report into.")
    _add_density_args(p_bakeoff)
    p_bakeoff.set_defaults(func=_cmd_bakeoff)

    p_preview = sub.add_parser(
        "preview",
        help="Render a FAITHFUL absolute-positioned diagnostic view (truth pixels + extracted "
        "region boxes). NOT the Outlook output -- a placement/scale/alignment check.",
    )
    p_preview.add_argument("psd", help="Path to the .psd file.")
    p_preview.add_argument("-o", "--output", help="Directory to write preview.html into (default: preview_out).")
    p_preview.set_defaults(func=_cmd_preview)

    p_gate = sub.add_parser(
        "gate",
        help="Run the per-region fidelity gate: compare an emitted bundle against its source PSD "
        "(truth) region by region; exit 1 with a defect list on any failure.",
    )
    p_gate.add_argument("psd", help="Path to the source .psd file (the truth).")
    p_gate.add_argument("bundle", help="Path to an emitted bundle directory (index.html + regions.json).")
    p_gate.add_argument("--full", action="store_true", help="Print the full JSON report.")
    p_gate.set_defaults(func=_cmd_gate)

    p_compare = sub.add_parser(
        "compare",
        help="Hands-on side-by-side: emit the 3 bake-off variants + preview + a compare.html "
        "(proof | preview | live | hybrid | raster) for eyeballing and the D6 read.",
    )
    p_compare.add_argument("psd", help="Path to the .psd file.")
    p_compare.add_argument("-o", "--output", required=True, help="Directory to write the comparison into.")
    _add_density_args(p_compare)
    p_compare.set_defaults(func=_cmd_compare)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 -- top-level operator guard (S1.1); detail preserved below
        # A command's own REFUSED paths handle EXPECTED failures (they return 1 with a plain,
        # layer/URL-naming message). Anything reaching here is UNEXPECTED -- a bug or an environment
        # fault -- and a non-technical operator should get a bounded, actionable line, not a raw
        # traceback. The full traceback is kept for the maintainer (DEBUG-level, and re-raised
        # verbatim under PSD_HTML_DEBUG=1); it is never swallowed.
        if os.environ.get("PSD_HTML_DEBUG"):
            raise
        run_id = new_run_id()
        get_logger(__name__).debug("unexpected error (run=%s)", run_id, exc_info=True)
        print(operator_unexpected_message(run_id, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
