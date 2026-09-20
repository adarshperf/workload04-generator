#!/usr/bin/env python3
"""workload04_generator.py -- permanent Workload04 variant-generation tool.

One-time setup:
    python workload04_generator.py init-baseline \\
        --resource-list /opt/load/workload04-resources.txt \\
        --source-root /opt/workload04

Normal use (only parameter required):
    python workload04_generator.py --avg-size 100KiB
    python workload04_generator.py --avg-size 1MiB
    python workload04_generator.py --avg-size 250MiB

Dry run (no files written, just the pre-flight/feasibility report):
    python workload04_generator.py --avg-size 250MiB --dry-run

Ask "what is the biggest average size I can safely generate on THIS
machine right now?" without generating anything:
    python workload04_generator.py max-avg-size

Validate an already-generated tier:
    python workload04_generator.py validate 100KiB

Independent, read-only request-weighted size audit:
    python workload04_generator.py audit-size 100KiB

Self-test:
    python workload04_generator.py self-test

Model: every baseline physical resource gets its own generated physical
resource (strict 1:1 mapping) -- see wg/pool.py module docstring. Synthetic
(non-representative) fallback content is never used silently -- generation
refuses to start if the frozen baseline has any record with no resolvable
source seed, unless --allow-synthetic-fallback is passed explicitly.

Output location: ALL generated tiers are written under a single output
root, in priority order: --output-root CLI flag > WORKLOAD04_OUTPUT_ROOT
environment variable > /opt/generated_workload04 (production default on
Linux/POSIX) > a dev/test fallback next to this checkout (non-POSIX only,
e.g. local Windows development). See README.md "Output directory" section.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wg import common as wg_common
from wg import baselinefreeze
from wg import bundled as wg_bundled
from wg import pool as wg_pool
from wg import generate as wg_generate
from wg import validate as wg_validate
from wg import sizeaudit
from wg import reportio
from wg import storage as wg_storage
from wg import safety as wg_safety


def _add_output_root_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--output-root",
        help="Override the output root for generated tiers (default: "
             "$WORKLOAD04_OUTPUT_ROOT, else /opt/generated_<workload-label> on Linux, "
             "else a dev/test fallback next to this checkout). Intended for "
             "testing/development; production use should rely on the default.",
    )


def _apply_output_root(args) -> None:
    if getattr(args, "output_root", None):
        wg_common.set_generated_root(args.output_root)


def _add_workload_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--workload-root",
        help="Path to an explicit golden workload corpus (e.g. Workload18, Workload24). "
             "Bypasses the bundled default Workload04 entirely -- no auto-extraction, no "
             "/opt/workload04 involved. When omitted, the default bundled Workload04 corpus "
             "is used (auto-extracted to /opt/workload04 the first time it's needed).",
    )
    p.add_argument(
        "--resource-list",
        help="Resource list for --workload-root, if it can't be discovered automatically "
             "inside the workload root (a single *-resources.txt file).",
    )
    p.add_argument(
        "--workload-label",
        help="Explicit label identifying an --workload-root corpus (used for baseline "
             "directory naming and generated output naming). Default resolution order: "
             "1) this flag, 2) derived from the resource list's '<label>-resources.txt' "
             "filename, 3) the --workload-root directory's own basename.",
    )


def _resolve_workload_label(workload_root: Path, resource_list: Path, explicit_label: str = None) -> str:
    """Deterministic label resolution for an explicit --workload-root,
    NEVER derived from an incidental parent directory name (e.g. a pytest
    tmp_path): explicit --workload-label > name conventionally encoded in
    the resource-list filename > the workload root's own basename."""
    if explicit_label:
        return wg_common.sanitize_workload_label(explicit_label)
    derived = wg_common.derive_label_from_resource_list_name(resource_list)
    return wg_common.sanitize_workload_label(derived or workload_root.name)


def _resolve_workload(args, *, baseline_dir_override: str = None):
    """Resolve which workload (Workload04 default, or an explicit
    --workload-root) this invocation targets, freezing/loading its baseline
    as needed, and set the active workload label (wg_common.set_workload_label())
    so output_dir_for_tier()/uri_root_prefix()/get_generated_root() all
    reflect it automatically. Returns (FrozenBaseline, label)."""
    workload_root = getattr(args, "workload_root", None)
    if workload_root:
        source_root = Path(workload_root).resolve()

        resource_list_arg = getattr(args, "resource_list", None)
        if resource_list_arg:
            resource_list = Path(resource_list_arg).resolve()
        else:
            candidates = sorted(source_root.glob("*-resources.txt")) or sorted(source_root.glob("*resources*.txt"))
            if len(candidates) != 1:
                print(
                    f"[FATAL] Could not automatically determine the resource list inside "
                    f"{source_root} (found {len(candidates)} candidate file(s) matching "
                    f"*-resources.txt). Pass --resource-list explicitly.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            resource_list = candidates[0]

        label = _resolve_workload_label(source_root, resource_list, getattr(args, "workload_label", None))
        baseline_dir = Path(baseline_dir_override) if baseline_dir_override else wg_common.default_baseline_dir(label)

        baseline = None
        if (baseline_dir / "baseline_manifest.json").is_file():
            existing = baselinefreeze.FrozenBaseline(baseline_dir)
            if Path(existing.header["source_root_path"]).resolve() == source_root:
                baseline = existing

        if baseline is None:
            print(f"Analyzing workload '{label}' at {source_root} (resource list: {resource_list})...")
            header = baselinefreeze.freeze(resource_list, source_root, baseline_dir,
                                            label=label, accept_discrepancies=True)
            if header["discrepancies_at_freeze_time"]:
                print(f"[NOTE] '{label}' baseline recorded with these observations (expected/normal "
                      f"for a non-Workload04 corpus -- actual measured facts are used as-is, not "
                      f"forced to match Workload04's specific invariants):")
                for d in header["discrepancies_at_freeze_time"]:
                    print(f"  - {d}")
            print(f"  physical_file_count_on_disk: {header['physical_file_count_on_disk']}")
            print(f"  total_entries              : {header['total_entries']}")
            print(f"  eicar_count (content-based) : {header['eicar_count']}")
            baseline = baselinefreeze.FrozenBaseline(baseline_dir)

        wg_common.set_workload_label(label)
        return baseline, label

    # Default: bundled Workload04.
    label = wg_common.DEFAULT_WORKLOAD_LABEL
    baseline_dir = Path(baseline_dir_override) if baseline_dir_override else wg_common.default_baseline_dir(label)
    try:
        baseline = wg_bundled.load_or_bootstrap_default_baseline(baseline_dir, label)
    except wg_bundled.CorpusVerificationError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        raise SystemExit(2)
    wg_common.set_workload_label(label)
    return baseline, label


def cmd_init_baseline(argv) -> int:
    p = argparse.ArgumentParser(prog="workload04_generator.py init-baseline")
    p.add_argument("--resource-list", help="Path to the original workload04-resources.txt")
    p.add_argument("--source-root", help="Path to the original workload04/ corpus directory")
    p.add_argument("--baseline-dir", default=str(wg_common.DEFAULT_BASELINE_DIR),
                   help="Where to write the frozen baseline manifest (default: tool_dir/baseline)")
    p.add_argument("--yes", "--force", dest="force", action="store_true",
                   help="Accept and freeze even if computed facts differ from the expected "
                        "10,000 entries / 3 EICAR entries / 0.03%% frequency")
    args = p.parse_args(argv)

    try:
        resource_list = wg_common.wmime_baseline.discover_resource_list(args.resource_list)
        source_root = wg_common.wmime_baseline.discover_source_root(args.source_root, resource_list)
    except FileNotFoundError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2

    print(f"Resource list : {resource_list}")
    print(f"Source root   : {source_root}")
    print("Analyzing baseline (this reads every resolvable source file once, and walks the "
          "physical directory tree to verify the real on-disk payload file count)...")

    baseline_dir = Path(args.baseline_dir)
    try:
        header = baselinefreeze.freeze(resource_list, source_root, baseline_dir,
                                        accept_discrepancies=args.force)
    except baselinefreeze.DiscrepancyError as e:
        print("\n[DISCREPANCY] Computed baseline facts differ from the expected Workload04 facts:")
        for d in e.discrepancies:
            print(f"  - {d}")
        print(
            "\nThe baseline was NOT frozen. Re-run with --yes/--force to accept these "
            "values and freeze them as the new source of truth, or fix the source data first."
        )
        return 3

    print("\nBaseline frozen successfully.")
    print(f"  total_entries              : {header['total_entries']}")
    print(f"  physical_file_count_on_disk: {header['physical_file_count_on_disk']}")
    print(f"  unique_uris                : {header['unique_uris']}")
    print(f"  eicar_count                : {header['eicar_count']} ({header['eicar_pct'] * 100:.4f}%)")
    print(f"  missing_files (resource-list entries not resolving): {header['missing_files']}")
    print(f"  request-weighted avg       : {wg_common.human_size(header['request_weighted_average_bytes'])}")
    print(f"  frozen manifest            : {baseline_dir / 'baseline_manifest.json'}")
    if header["discrepancies_at_freeze_time"]:
        print("\n[NOTE] Frozen WITH accepted discrepancies (see baseline_manifest.json "
              "'discrepancies_at_freeze_time'). Records with no resolvable source file will be "
              "generated with synthetic, non-representative content -- flagged explicitly in every "
              "generated tier's manifest.csv/validation-report.")
    return 0


def cmd_generate(argv) -> int:
    p = argparse.ArgumentParser(prog="workload04_generator.py")
    p.add_argument("--avg-size", required=True,
                   help="Target request-weighted average object size, e.g. 100KiB, 1MiB, 250MiB")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the pre-flight/feasibility report only; write nothing")
    p.add_argument("--baseline-dir", default=None,
                   help="Frozen baseline location (default: baselines/<workload-label>/, "
                        "derived automatically from --workload-root or the bundled Workload04)")
    p.add_argument("--seed", type=int, default=42, help="Deterministic seed (default: 42)")
    p.add_argument("--force", action="store_true", help="Overwrite an existing generated tier directory")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--allow-synthetic-fallback", action="store_true",
                   help="Diagnostic escape hatch only: explicitly accept generating synthetic "
                        "(non-representative) content for baseline records with no resolvable source "
                        "seed. Without this flag, generation refuses to start if the frozen baseline "
                        "has ANY such records -- fix the baseline (see analyze_missing_baseline_entries.py) "
                        "instead of silently masking it.")
    # Advanced/debug-only override -- never required for normal use.
    p.add_argument("--max-single-file-bytes", type=int, default=wg_common.DEFAULT_MAX_SINGLE_FILE_BYTES)
    _add_output_root_arg(p)
    _add_workload_args(p)
    args = p.parse_args(argv)
    _apply_output_root(args)

    try:
        target_bytes = wg_common.parse_size(args.avg_size)
    except ValueError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2
    tier = wg_common.tier_name(args.avg_size)

    try:
        baseline, label = _resolve_workload(args, baseline_dir_override=args.baseline_dir)
    except (baselinefreeze.DiscrepancyError, FileNotFoundError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2

    # Baseline staleness/fingerprint check (spec section 22): a frozen
    # baseline copied onto a new machine still records the ORIGINAL
    # machine's source_root_path. If that path isn't reachable HERE and
    # init-baseline hasn't been re-run with a local --source-root yet,
    # every seed read would silently fail and generation would degrade to
    # non-representative content for all records -- refuse instead, unless
    # explicitly overridden.
    if not args.dry_run and not baseline.source_root_reachable() and not args.allow_synthetic_fallback:
        print(
            f"[FATAL] The frozen baseline's original corpus location "
            f"({baseline.source_root}) is not reachable from this machine. This usually "
            f"means the baseline metadata was copied here but 'init-baseline' has not yet "
            f"been re-run with --source-root/--resource-list pointing at THIS machine's "
            f"copy of the golden Workload04 corpus. Generating now would silently degrade "
            f"every record to non-representative content. Re-run init-baseline locally, or "
            f"pass --allow-synthetic-fallback to explicitly accept degraded content.",
            file=sys.stderr,
        )
        return 8

    plan = wg_pool.build_plan(
        baseline, target_bytes, args.seed,
        max_single_file_bytes=args.max_single_file_bytes,
    )

    output_root = wg_common.get_generated_root()
    out_dir = wg_common.output_dir_for_tier(tier)

    # Permission pre-flight (spec section 9): never start ANY work -- not
    # even a dry-run's directory probe -- against an output root we can't
    # actually write to. --dry-run must create NOTHING, so it uses the
    # non-mutating check (no mkdir, no probe file).
    perm_error = wg_storage.check_output_root_writable(output_root, create=not args.dry_run)
    if perm_error:
        print(f"[FATAL] {perm_error}", file=sys.stderr)
        return 6

    preflight = wg_storage.run_preflight(
        plan, tier=tier, requested_avg_text=args.avg_size, out_dir=out_dir, label=label,
    )
    print(wg_storage.render_preflight_report(preflight))

    if args.dry_run:
        if not baseline.source_root_reachable():
            print(
                "[WARN] The frozen baseline's original corpus location "
                f"({baseline.source_root}) is not reachable from this machine -- a REAL "
                "generation would refuse to start (or degrade content, with "
                "--allow-synthetic-fallback) until init-baseline is re-run locally. This "
                "dry-run's storage feasibility numbers above are still accurate."
            )
        return 0 if preflight.feasible else 1

    if not preflight.feasible:
        print("[FATAL] Storage pre-flight failed -- aborting before any payload file is created.",
              file=sys.stderr)
        return 4

    if out_dir.exists() and not args.force:
        print(f"[FATAL] {out_dir} already exists. Pass --force to regenerate it.", file=sys.stderr)
        return 2

    if plan.missing_seed_count and not args.allow_synthetic_fallback:
        missing_uris = [r.uri for r in plan.records if not r.has_source_seed]
        print(
            f"[FATAL] {plan.missing_seed_count} baseline record(s) have no resolvable source seed "
            f"(e.g. {missing_uris[0]!r}). Refusing to generate synthetic (non-representative) content "
            f"silently. Fix the baseline (run analyze_missing_baseline_entries.py, then re-run "
            f"init-baseline) or pass --allow-synthetic-fallback to explicitly accept this.",
            file=sys.stderr,
        )
        return 5

    print(f"Generating {label}-{tier} (target average {args.avg_size}, "
          f"{plan.total_physical_files} physical payload files)...")

    try:
        with wg_safety.generation_lock(output_root, tier=tier):
            with wg_safety.staged_generation(out_dir) as (staging, commit):
                result = None
                size_tolerance = wg_common.DEFAULT_SIZE_TOLERANCE_PCT
                max_iterations = 6
                for iteration in range(1, max_iterations + 1):
                    result = wg_generate.generate_workload(
                        baseline, plan, tier=tier, seed=args.seed, verbose=args.verbose,
                        allow_synthetic_fallback=args.allow_synthetic_fallback,
                        output_dir=staging,
                    )
                    total = len(result.files) or 1
                    actual_avg = sum(gf.generated_size_bytes for gf in result.files) / total
                    error = abs(actual_avg - target_bytes) / target_bytes if target_bytes else 0.0
                    if args.verbose:
                        print(f"  [iteration {iteration}] actual={wg_common.human_size(actual_avg)} "
                              f"target={wg_common.human_size(target_bytes)} error={error * 100:+.2f}%")
                    if error <= size_tolerance or actual_avg <= 0:
                        break
                    factor = target_bytes / actual_avg
                    wg_pool.rescale_plan_sizes(plan, factor)

                manifest = wg_generate.write_reproducibility_manifest(
                    baseline, plan, result, target_text=args.avg_size)
                report = wg_validate.validate(baseline, plan, result, manifest)
                reportio.write_target_outputs(result, manifest, report, final_out_dir=out_dir)

                print(wg_validate.render_text_report(report))

                if report["overall"] != "PASS":
                    print(
                        "[FATAL] Post-generation validation FAILED -- the staged tier will NOT be "
                        "moved into the final output location, and any previously-valid tier there "
                        "is left untouched.",
                        file=sys.stderr,
                    )
                    return 1

                commit()
    except wg_safety.LockHeldError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 7

    print(f"Output directory: {out_dir}")
    return 0


def cmd_max_avg_size(argv) -> int:
    p = argparse.ArgumentParser(prog="workload04_generator.py max-avg-size")
    p.add_argument("--baseline-dir", default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-single-file-bytes", type=int, default=wg_common.DEFAULT_MAX_SINGLE_FILE_BYTES)
    _add_output_root_arg(p)
    _add_workload_args(p)
    args = p.parse_args(argv)
    _apply_output_root(args)

    try:
        baseline, label = _resolve_workload(args, baseline_dir_override=args.baseline_dir)
    except (baselinefreeze.DiscrepancyError, FileNotFoundError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2

    output_root = wg_common.get_generated_root()
    perm_error = wg_storage.check_output_root_writable(output_root)
    if perm_error:
        print(f"[FATAL] {perm_error}", file=sys.stderr)
        return 6

    print(f"Source workload : {label}")
    result = wg_storage.compute_max_avg_size(
        baseline, output_root, seed=args.seed, max_single_file_bytes=args.max_single_file_bytes,
    )
    print(wg_storage.render_max_avg_size_report(result, output_root))
    return 0


def cmd_validate(argv) -> int:
    p = argparse.ArgumentParser(prog="workload04_generator.py validate")
    p.add_argument("tier_dir", help="Tier name (e.g. 100KiB) or path to an already-generated "
                                     "<workload-label>-<TIER> directory")
    p.add_argument("--baseline-dir", default=None,
                   help="Override the baseline dir (default: derived automatically from the "
                        "tier's own recorded workload_label)")
    _add_output_root_arg(p)
    args = p.parse_args(argv)
    _apply_output_root(args)

    tier_arg = args.tier_dir
    if Path(tier_arg).exists():
        out_dir = Path(tier_arg).resolve()
    else:
        tier = wg_common.tier_name(tier_arg)
        out_dir = wg_common.output_dir_for_tier(tier)

    manifest_path = out_dir / "manifest.json"
    resources_path = out_dir / "workload-resources.txt"
    if not manifest_path.is_file() or not resources_path.is_file():
        print(f"[FATAL] {out_dir} does not look like a generated tier directory "
              f"(missing manifest.json/workload-resources.txt).", file=sys.stderr)
        return 2

    import csv
    import json as _json
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    tier = manifest["tier"]
    label = manifest.get("workload_label", wg_common.DEFAULT_WORKLOAD_LABEL)
    wg_common.set_workload_label(label)

    baseline_dir = Path(args.baseline_dir) if args.baseline_dir else wg_common.default_baseline_dir(label)
    try:
        baseline = baselinefreeze.FrozenBaseline(baseline_dir)
    except FileNotFoundError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2

    target_bytes = manifest["target_average_requested_bytes"]
    seed = manifest["seed"]

    plan = wg_pool.build_plan(baseline, target_bytes, seed)
    # Re-derive result from what's actually on disk + manifest.csv, rather
    # than re-generating, so `validate` is a pure read-only check.
    result = wg_generate.GenerationResult(tier=tier, target_bytes=target_bytes, seed=seed, output_dir=out_dir)
    with (out_dir / "manifest.csv").open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result.files.append(wg_generate.GeneratedFile(
                uri=row["uri"], rel_path=row["rel_path"], rewritten_uri=row["rewritten_uri"],
                extension=row["extension"], mime_type=row["mime_type"], family_used=row["family_used"],
                effective_family=row["effective_family"], source_size_bytes=int(row["source_size_bytes"]),
                generated_size_bytes=int(row["generated_size_bytes"]),
                has_source_seed=row["has_source_seed"] == "True",
                baseline_decoded_rel=row.get("baseline_decoded_rel", ""),
                normalized_rel=row.get("normalized_rel", ""),
                collision_suffix=row.get("collision_suffix") or None,
                is_av_test=row["is_av_test"] == "True", validation_ok=row["validation_ok"] == "True",
                validation_msg=row["validation_msg"],
            ))
    result.resource_lines = resources_path.read_text(encoding="utf-8").splitlines()

    report = wg_validate.validate(baseline, plan, result, manifest)
    print(wg_validate.render_text_report(report))
    return 0 if report["overall"] == "PASS" else 1


def cmd_self_test(argv) -> int:
    tests_dir = Path(__file__).resolve().parent / "tests"
    if not tests_dir.is_dir():
        print(f"[FATAL] no tests/ directory found at {tests_dir}", file=sys.stderr)
        return 2
    try:
        import pytest
    except ImportError:
        print("[FATAL] pytest is not installed; run `pip install pytest` first.", file=sys.stderr)
        return 2
    return pytest.main([str(tests_dir), "-v"])


def cmd_audit_size(argv) -> int:
    p = argparse.ArgumentParser(prog="workload04_generator.py audit-size")
    p.add_argument("tier_dir", help="Tier name (e.g. 100KiB) or path to an already-generated "
                                     "<workload-label>-<TIER> directory")
    _add_output_root_arg(p)
    args = p.parse_args(argv)
    _apply_output_root(args)

    tier_arg = args.tier_dir
    if Path(tier_arg).exists():
        out_dir = Path(tier_arg).resolve()
    else:
        tier = wg_common.tier_name(tier_arg)
        out_dir = wg_common.output_dir_for_tier(tier)

    resources_path = out_dir / "workload-resources.txt"
    if not resources_path.is_file():
        print(f"[FATAL] {out_dir} does not look like a generated tier directory "
              f"(missing workload-resources.txt).", file=sys.stderr)
        return 2

    manifest_path = out_dir / "manifest.json"
    target_bytes = 0
    tier = out_dir.name
    if manifest_path.is_file():
        import json as _json
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        target_bytes = manifest["target_average_requested_bytes"]
        tier = manifest.get("tier", tier)

    result = sizeaudit.audit(tier, out_dir, target_bytes)
    print(sizeaudit.render_text_report(result))
    return 0 if result.passed else 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "init-baseline":
        return cmd_init_baseline(argv[1:])
    if argv and argv[0] == "validate":
        return cmd_validate(argv[1:])
    if argv and argv[0] == "audit-size":
        return cmd_audit_size(argv[1:])
    if argv and argv[0] == "max-avg-size":
        return cmd_max_avg_size(argv[1:])
    if argv and argv[0] == "self-test":
        return cmd_self_test(argv[1:])
    return cmd_generate(argv)


if __name__ == "__main__":
    raise SystemExit(main())

