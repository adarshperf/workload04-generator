"""Per-target planning, generation and the tolerance/retry loop.

Redesigned 2026-09-18 after the qualification audit found that the previous
"uniform scale factor + repeat-count boost on cap" design let a handful of
capped classes (DLL/INF) balloon to a wildly disproportionate request-weight
share at large targets, while unrelated major MIME families (CSS/PNG/PDF/ZIP/
EXE) were silently dropped once the unique-file budget got tight. See
docs/DESIGN.md and docs/QUALIFICATION_REPORT.md for the full rationale.

Key invariants of the new design:
  * Every MAJOR family present in the source (common.MAJOR_FAMILIES) always
    gets at least one *structurally valid* representative file -- selection
    never silently accepts a mislabeled seed (formats.quick_signature_ok).
  * A class's request-weight *share* is fixed at planning time from its true
    source share (with a floor for required-but-tiny classes) and is never
    touched again to "compensate" for a file-size cap. If a class's ideal
    per-file size would exceed --max-single-file-bytes, it gets *more unique
    files* (each <= the cap) instead of one file repeated disproportionately
    more often.
  * The retry/tolerance loop only ever rescales *sizes* (uniformly, across
    all classes at once); it never touches repeat counts/weights.
  * All reported/validated distributions are computed from the *realized*
    manifest + resource list, never from the plan.
"""
from __future__ import annotations

import csv
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from . import common, formats, distribution
from .baseline import BaselineStats


@dataclass
class PlannedFile:
    label: str            # report/group label, e.g. "JPEG", "Other:inf"
    is_major: bool
    class_family: str      # padding family used by formats.generate()
    class_ext: str
    seed_uri: str
    seed_path: Path
    seed_size: int
    target_size: int
    repeat_count: int
    out_name: str
    out_uri: str
    source_share: float    # this class's true request-weight share in the source
    is_av_test: bool = False  # mandatory AV-test group (common.AV_TEST_URIS); never split, never floored


@dataclass
class TargetResult:
    target_name: str
    target_bytes: int
    output_dir: Path
    actual_weighted_average: float
    average_error_percent: float
    total_resource_entries: int
    unique_file_count: int
    total_corpus_bytes: int
    estimated_httpblaster_memory_bytes: int
    mime_distribution: dict
    extension_distribution: dict
    warnings: list
    seed: int
    iterations: int
    within_tolerance: bool
    manifest_rows: list = field(default_factory=list)
    plan_report: dict = field(default_factory=dict)


def _pick_valid_representative(candidates, family: str, max_attempts: int = 25):
    """Pick one structurally-valid seed record, starting near the median size
    and fanning outward, so we don't systematically pick only the smallest or
    largest file. Returns None if nothing in `candidates` passes the
    magic-byte check for `family` (e.g. every candidate is mislabeled)."""
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda r: r.size_bytes)
    n = len(ordered)
    mid = n // 2
    order = sorted(range(n), key=lambda i: abs(i - mid))
    checked = 0
    for idx in order:
        if checked >= max_attempts:
            break
        rec = ordered[idx]
        try:
            data = rec.resolved_path.read_bytes()
        except OSError:
            checked += 1
            continue
        if formats.quick_signature_ok(family, data):
            return rec
        checked += 1
    return None


def plan_target(stats: BaselineStats, target_bytes: int, *, seed: int,
                 max_unique_files: Optional[int] = None,
                 soft_corpus_budget_bytes: int = common.DEFAULT_SOFT_CORPUS_BUDGET_BYTES,
                 min_unique_total: int = common.DEFAULT_MIN_UNIQUE_TOTAL,
                 total_line_budget: int = common.DEFAULT_TOTAL_LINE_BUDGET,
                 max_single_file_bytes: int = common.DEFAULT_MAX_SINGLE_FILE_BYTES,
                 min_major_share: float = common.DEFAULT_MIN_MAJOR_SHARE,
                 class_size_ratio_min: float = common.DEFAULT_CLASS_SIZE_RATIO_MIN,
                 class_size_ratio_max: float = common.DEFAULT_CLASS_SIZE_RATIO_MAX,
                 other_bucket_files: int = common.DEFAULT_OTHER_BUCKET_FILES) -> tuple:
    """Return (list[PlannedFile], plan_report dict).

    2026-09-19: request COUNTS are now EXACT, not approximate. Every group's
    resource-list line count is the baseline's own integer request weight for
    that category (computed from ALL baseline records, including ones whose
    backing file happens to be missing on this particular checkout -- request
    weight is a property of the resource LIST, not of local file
    availability). `total_line_budget` is accepted for backward-compatible
    signatures but no longer scales anything: the golden 10K Workload04
    resource list itself is the line budget (`total_entries`).

    AV-test (EICAR) entries (common.AV_TEST_URIS) are NEVER excluded from
    `usable` -- they are mandatory, baseline-required content, not a defect
    to route around. They get their own dedicated groups (by exact URI, not
    by extension) so their weight is never double-counted under the generic
    ZIP family and their request frequency is preserved independently of it.
    """
    usable = [r for r in stats.records if r.exists and r.valid and r.size_bytes > 0]
    if not usable:
        raise RuntimeError("no usable source records to plan from")
    global_avg = stats.request_weighted_average_bytes or 1.0
    rng = random.Random(seed)

    # TRUE baseline weight per extension, from ALL records (not just the ones
    # resolvable on this checkout) -- this is the golden request-count source
    # of truth; a missing backing file only affects which file can serve as
    # content, never the category's request-list count.
    full_ext_weight = defaultdict(int)
    for r in stats.records:
        full_ext_weight[r.ext] += r.weight
    baseline_total_weight = sum(r.weight for r in stats.records) or 1

    non_av_usable = [r for r in usable if r.uri not in common.AV_TEST_URIS]
    ext_bytes = defaultdict(int)
    ext_weight_usable = defaultdict(int)
    for r in non_av_usable:
        ext_bytes[r.ext] += r.size_bytes * r.weight
        ext_weight_usable[r.ext] += r.weight

    av_test_usable = {r.uri: r for r in usable if r.uri in common.AV_TEST_URIS}
    av_test_baseline_weight_by_uri = {
        r.uri: r.weight for r in stats.records if r.uri in common.AV_TEST_URIS
    }
    av_test_weight_total = sum(av_test_baseline_weight_by_uri.values())

    groups = []
    dropped_major = []
    included_exts = set()

    for label, exts, family, mime in common.MAJOR_FAMILIES:
        present = [e for e in exts if full_ext_weight.get(e, 0) > 0]
        if not present:
            continue  # not present in the source corpus at all -- nothing to preserve
        source_weight = sum(full_ext_weight[e] for e in present)
        if label == "ZIP":
            source_weight -= av_test_weight_total  # AV-test entries tracked as their own group
        candidates = [r for r in non_av_usable if r.ext in present]
        rep = _pick_valid_representative(candidates, family)
        if rep is None:
            dropped_major.append({"label": label, "reason": "no structurally-valid seed file found in source"})
            continue
        groups.append({
            "label": label, "family": family, "mime": mime, "ext": rep.ext,
            "seed": rep, "baseline_weight": source_weight, "is_major": True, "is_av_test": False,
        })
        included_exts.update(present)

    # Dedicated, mandatory AV-test groups -- one per baseline AV-test URI,
    # sized off their OWN seed size (not the generic zip class average, which
    # crypt-ssleay-infected.zip's 234KB would otherwise skew heavily).
    for uri in common.AV_TEST_URIS:
        rec = av_test_usable.get(uri)
        if rec is None:
            continue  # not resolvable from this source root; nothing to grow
        groups.append({
            "label": f"EICAR:{Path(uri).name}", "family": "zip", "mime": "application/zip",
            "ext": "zip", "seed": rec, "baseline_weight": av_test_baseline_weight_by_uri.get(uri, 1),
            "is_major": False, "is_av_test": True,
        })

    # Long-tail "Other" bucket: a handful of the highest-weight remaining
    # extensions get a unique representative file, but the exact AGGREGATE
    # "Other" request count (every non-major, non-AV-test extension, whether
    # or not it got its own representative) is redistributed onto those
    # representatives so the total is preserved exactly -- an integer
    # reallocation, never a rounded share, so "Other count" always matches
    # the baseline exactly.
    remaining = sorted(
        ((e, w) for e, w in full_ext_weight.items() if e not in included_exts),
        key=lambda kv: -kv[1],
    )
    other_included = []
    other_dropped = []
    for ext, w in remaining:
        if len(other_included) < other_bucket_files and any(r.ext == ext for r in non_av_usable):
            other_included.append((ext, w))
        else:
            other_dropped.append((ext, w))
    dropped_longtail_weight = sum(w for _, w in other_dropped)

    if other_included:
        own_total = sum(w for _, w in other_included) or 1
        allocated = []
        running = 0
        for i, (ext, w) in enumerate(other_included):
            if i < len(other_included) - 1:
                extra = round(dropped_longtail_weight * w / own_total)
            else:
                extra = dropped_longtail_weight - running
            running += extra
            allocated.append((ext, w + extra))
        for ext, final_weight in allocated:
            candidates = [r for r in non_av_usable if r.ext == ext]
            family = common.classify_family(ext)
            rep = _pick_valid_representative(candidates, family) or candidates[0]
            groups.append({
                "label": f"Other:{ext or '(none)'}", "family": family,
                "mime": common.guess_mime(ext), "ext": ext, "seed": rep,
                "baseline_weight": final_weight, "is_major": False, "is_av_test": False,
            })
    else:
        dropped_longtail_weight += dropped_longtail_weight  # no-op guard; nothing to attach it to

    if not groups:
        raise RuntimeError("planning produced zero groups -- source corpus appears empty/unusable")

    for g in groups:
        g["source_share"] = g["baseline_weight"] / baseline_total_weight

    # Bounded, per-class target size -- relative to the class's true size
    # ratio in the source, but clamped so no class can demand an extreme
    # per-file size. If that (still bounded) size exceeds the cap, split into
    # multiple files instead of inflating this class's request weight -- but
    # the split count itself must never exceed what that class's TRUE
    # request weight (baseline_weight, an exact integer) can support (each
    # split file needs >=1 resource-list line just to be reachable). Without
    # this second bound, a tiny-share class needing many size-driven splits
    # would have its total weight forced up past its baseline count, silently
    # reproducing the exact DLL-explosion defect this design otherwise
    # prevents. When capped this way, the class's realized total size falls
    # short of its ideal target; the retry/rescale loop (which only ever
    # touches sizes, never weights) makes up the difference elsewhere.
    for g in groups:
        ext = g["ext"]
        if g["is_av_test"]:
            # Own size, not the generic zip class average -- crypt-ssleay-
            # infected.zip (234KB) and eicar.zip (184B) must scale off
            # themselves, not get lumped with unrelated generic zips.
            class_avg_original = g["seed"].size_bytes
        else:
            class_avg_original = (
                ext_bytes[ext] / ext_weight_usable[ext] if ext_weight_usable.get(ext) else g["seed"].size_bytes
            )
        ratio = (class_avg_original / global_avg) if global_avg else 1.0
        ratio = min(max(ratio, class_size_ratio_min), class_size_ratio_max)
        class_target_avg = max(1, round(target_bytes * ratio))
        if g["is_av_test"]:
            # Exactly one canonical seed per baseline AV-test URI -- never
            # split it into multiple unique files, just cap its size.
            k, per_file = 1, min(class_target_avg, max_single_file_bytes)
        elif class_target_avg > max_single_file_bytes:
            k = min(math.ceil(class_target_avg / max_single_file_bytes), g["baseline_weight"])
            k = max(1, k)
            per_file = min(max_single_file_bytes, class_target_avg)
        else:
            k, per_file = 1, class_target_avg
        g["num_files"] = k
        g["per_file_size"] = per_file

    planned: list = []
    for g in groups:
        total_lines = max(g["num_files"], g["baseline_weight"])
        base_count, extra = divmod(total_lines, g["num_files"])
        for i in range(g["num_files"]):
            seed_rec = g["seed"]
            safe_name = seed_rec.uri.strip("/").replace("/", "_")
            suffix = f"_{i + 1}" if g["num_files"] > 1 else ""
            out_name = f"{g['ext'] or 'noext'}_{safe_name}{suffix}"
            repeat_count = base_count + (1 if i < extra else 0)
            planned.append(PlannedFile(
                label=g["label"], is_major=g["is_major"], class_family=g["family"],
                class_ext=g["ext"], seed_uri=seed_rec.uri, seed_path=seed_rec.resolved_path,
                seed_size=seed_rec.size_bytes, target_size=g["per_file_size"],
                repeat_count=repeat_count, out_name=out_name,
                out_uri=f"/{g['ext'] or 'bin'}/{out_name}", source_share=g["source_share"],
                is_av_test=g["is_av_test"],
            ))

    plan_report = {
        "dropped_major_families": dropped_major,
        "other_bucket_extensions": [e for e, _ in other_included],
        "dropped_longtail_extensions": [e for e, _ in other_dropped],
        "dropped_longtail_count": len(other_dropped),
        "dropped_longtail_source_share": dropped_longtail_weight / baseline_total_weight,
        "av_test_baseline_weight": av_test_weight_total,
        "av_test_baseline_share": av_test_weight_total / baseline_total_weight if baseline_total_weight else 0.0,
        "av_test_uris_present": sorted(av_test_usable.keys()),
        "av_test_uris_missing": sorted(set(common.AV_TEST_URIS) - set(av_test_usable.keys())),
        "baseline_total_weight": baseline_total_weight,
    }
    return planned, plan_report


def rescale_plan_sizes(planned: list, factor: float, max_single_file_bytes: int) -> list:
    """Correct the overall size to hit the target average. Only ever touches
    `target_size` -- repeat_count (request weight) is never modified here,
    which is the whole point: no class's weight share can drift because of
    a size-tolerance retry."""
    out = []
    for p in planned:
        new_target = max(1, round(p.target_size * factor))
        new_target = min(new_target, max_single_file_bytes)
        out.append(replace(p, target_size=new_target))
    return out


def execute_plan(planned: list, target_bytes: int, output_dir: Path, *, seed: int,
                  verbose: bool = False) -> TargetResult:
    files_dir = output_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    manifest_rows = []
    resource_lines = []
    warnings = []
    total_weight = 0
    weighted_bytes = 0
    unique_bytes = 0
    ext_weight = defaultdict(int)
    mime_weight = defaultdict(int)

    for i, p in enumerate(planned):
        if verbose and len(planned) > 20 and i % 50 == 0:
            print(f"  ... {i}/{len(planned)} files generated")
        try:
            seed_bytes = p.seed_path.read_bytes()
        except OSError as e:
            warnings.append(f"could not read seed {p.seed_path}: {e}")
            continue
        mime = common.guess_mime(p.class_ext)
        data, deviation = formats.generate(p.class_family, seed_bytes, p.target_size, rng)
        # A generator may internally downgrade to binary_fallback (e.g. a seed
        # file whose extension doesn't match its actual content). Validate
        # against the family that was actually used, not the nominal one.
        effective_family = "binary_fallback" if (deviation or "").startswith("binary_fallback") else p.class_family
        ok, msg = formats.validate(effective_family, data)
        if effective_family == "binary_fallback" and p.is_major:
            warnings.append(
                f"MAJOR FAMILY FALLBACK: {p.label} seed {p.seed_uri} downgraded to binary_fallback "
                f"({deviation}); this should not happen post-fix -- investigate the seed file."
            )
        if effective_family == "binary_fallback" and p.is_av_test:
            warnings.append(
                f"AV-TEST FALLBACK: {p.label} seed {p.seed_uri} downgraded to binary_fallback "
                f"({deviation}); AV-test files must stay genuine ZIPs -- investigate the seed file."
            )
        out_path = files_dir / p.out_uri.lstrip("/")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        status = "ok"
        if deviation:
            warnings.append(f"{p.out_uri}: {deviation}")
            status = "deviation"
        if not ok:
            warnings.append(f"{p.out_uri}: validation failed: {msg}")
            status = "validation_failed"

        manifest_rows.append({
            "generated_file": str(out_path.relative_to(output_dir)),
            "source_file": p.seed_uri,
            "extension": p.class_ext,
            "mime_type": mime,
            "source_size_bytes": p.seed_size,
            "generated_size_bytes": len(data),
            "request_weight": p.repeat_count,
            "magic_validation": ok,
            "structural_validation": ok,
            "target_class": effective_family,
            "validation_status": status,
            "label": p.label,
            "is_major": p.is_major,
            "is_av_test": p.is_av_test,
            "source_share": p.source_share,
        })

        resource_lines.append(f"{p.out_uri}\t{mime}")
        for _ in range(p.repeat_count - 1):
            resource_lines.append(f"{p.out_uri}\t{mime}")

        total_weight += p.repeat_count
        weighted_bytes += len(data) * p.repeat_count
        unique_bytes += len(data)
        ext_weight[p.class_ext] += p.repeat_count
        mime_weight[mime] += p.repeat_count

    actual_avg = weighted_bytes / total_weight if total_weight else 0.0
    error_pct = ((actual_avg - target_bytes) / target_bytes * 100.0) if target_bytes else 0.0

    (output_dir / "workload-resources.txt").write_text(
        "\n".join(resource_lines) + "\n", encoding="utf-8"
    )

    fieldnames = ["generated_file", "source_file", "extension", "mime_type", "source_size_bytes",
                  "generated_size_bytes", "request_weight", "magic_validation", "structural_validation",
                  "target_class", "validation_status", "label", "is_major", "is_av_test", "source_share"]
    with (output_dir / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in manifest_rows:
            w.writerow(row)

    total_share = sum(ext_weight.values()) or 1
    ext_dist = {k: v / total_share for k, v in sorted(ext_weight.items(), key=lambda kv: -kv[1])}
    mime_dist = {k: v / total_share for k, v in sorted(mime_weight.items(), key=lambda kv: -kv[1])}

    return TargetResult(
        target_name=output_dir.name,
        target_bytes=target_bytes,
        output_dir=output_dir,
        actual_weighted_average=actual_avg,
        average_error_percent=error_pct,
        total_resource_entries=total_weight,
        unique_file_count=len(manifest_rows),
        total_corpus_bytes=unique_bytes,
        estimated_httpblaster_memory_bytes=unique_bytes,
        mime_distribution=mime_dist,
        extension_distribution=ext_dist,
        warnings=warnings,
        seed=seed,
        iterations=1,
        within_tolerance=True,
        manifest_rows=manifest_rows,
    )


def write_validation_report(result: TargetResult, output_dir: Path) -> None:
    lines = [f"Validation report for {result.target_name}",
             f"Target average bytes    : {result.target_bytes}",
             f"Actual weighted average : {result.actual_weighted_average:.1f}",
             f"Average error percent   : {result.average_error_percent:.2f}%",
             f"Unique files            : {result.unique_file_count}",
             f"Total resource entries  : {result.total_resource_entries}",
             f"Within tolerance        : {result.within_tolerance}",
             ""]
    if result.plan_report.get("dropped_major_families"):
        lines.append("MAJOR FAMILIES DROPPED (should be empty):")
        for d in result.plan_report["dropped_major_families"]:
            lines.append(f"  - {d['label']}: {d['reason']}")
        lines.append("")
    dl = result.plan_report.get("dropped_longtail_extensions", [])
    if dl:
        lines.append(f"Long-tail extensions dropped ({len(dl)}, "
                      f"{result.plan_report['dropped_longtail_source_share']*100:.2f}% of source weight): "
                      f"{dl[:20]}" + (" ..." if len(dl) > 20 else ""))
        lines.append("")
    av_missing = result.plan_report.get("av_test_uris_missing", [])
    if av_missing:
        lines.append(f"AV-TEST URIS NOT RESOLVABLE FROM SOURCE ROOT (should be empty): {av_missing}")
        lines.append("")
    if result.warnings:
        lines.append(f"Warnings/deviations ({len(result.warnings)}):")
        lines.extend(f"  - {w}" for w in result.warnings)
    else:
        lines.append("No warnings.")
    (output_dir / "validation_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _av_test_summary(result: TargetResult, baseline: BaselineStats) -> dict:
    """AV-test (EICAR) baseline vs generated request-frequency comparison
    (master task section 17). Baseline numbers come from plan_report
    (computed once, at planning time, straight from the source baseline);
    generated numbers are recomputed from the realized manifest so this can
    never silently go stale."""
    av_rows = [r for r in result.manifest_rows if r.get("is_av_test")]
    generated_weight = sum(r["request_weight"] for r in av_rows)
    total_weight = result.total_resource_entries or 1
    baseline_weight = result.plan_report.get("av_test_baseline_weight", 0)
    baseline_share = result.plan_report.get("av_test_baseline_share", 0.0)
    generated_share = generated_weight / total_weight if total_weight else 0.0
    return {
        "baseline_uris": list(common.AV_TEST_URIS),
        "baseline_request_count": baseline_weight,
        "baseline_request_share": baseline_share,
        "generated_request_count": generated_weight,
        "generated_request_share": generated_share,
        "difference_pp": generated_share - baseline_share,
        "generated_files": [r["generated_file"] for r in av_rows],
    }


def write_summary(result: TargetResult, output_dir: Path, *, mime_tolerance: float,
                   baseline: BaselineStats, size_tolerance: float) -> None:
    sizes = sorted(row["generated_size_bytes"] for row in result.manifest_rows for _ in range(row["request_weight"]))

    def pct(p):
        if not sizes:
            return 0
        k = (len(sizes) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(sizes) - 1)
        return sizes[f] if f == c else sizes[f] + (sizes[c] - sizes[f]) * (k - f)

    comparison = distribution.build_comparison(baseline, result.manifest_rows)

    summary = {
        "target_average_bytes": result.target_bytes,
        "actual_request_weighted_average_bytes": result.actual_weighted_average,
        "average_error_percent": result.average_error_percent,
        "median_bytes": pct(50),
        "p90_bytes": pct(90),
        "p95_bytes": pct(95),
        "p99_bytes": pct(99),
        "min_bytes": sizes[0] if sizes else 0,
        "max_bytes": sizes[-1] if sizes else 0,
        "total_resource_entries": result.total_resource_entries,
        "unique_file_count": result.unique_file_count,
        "total_corpus_bytes": result.total_corpus_bytes,
        "estimated_httpblaster_memory_bytes": result.estimated_httpblaster_memory_bytes,
        "mime_distribution": result.mime_distribution,
        "extension_distribution": result.extension_distribution,
        "major_family_comparison": comparison["rows"],
        "av_test_summary": _av_test_summary(result, baseline),
        "plan_report": result.plan_report,
        "validation_summary": {
            "ok": sum(1 for r in result.manifest_rows if r["validation_status"] == "ok"),
            "deviation": sum(1 for r in result.manifest_rows if r["validation_status"] == "deviation"),
            "validation_failed": sum(1 for r in result.manifest_rows if r["validation_status"] == "validation_failed"),
        },
        "warnings": result.warnings,
        "generation_seed": result.seed,
        "size_tolerance": size_tolerance,
        "mime_tolerance": mime_tolerance,
        "within_size_tolerance": result.within_tolerance,
        "iterations": result.iterations,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def generate_target(stats: BaselineStats, target_name: str, target_bytes: int, output_root: Path, *,
                     seed: int = common.DEFAULT_SEED,
                     size_tolerance: float = common.DEFAULT_SIZE_TOLERANCE,
                     mime_tolerance: float = common.DEFAULT_MIME_TOLERANCE,
                     retry_limit: int = common.DEFAULT_RETRY_LIMIT,
                     max_unique_files: Optional[int] = None,
                     soft_corpus_budget_bytes: int = common.DEFAULT_SOFT_CORPUS_BUDGET_BYTES,
                     min_unique_total: int = common.DEFAULT_MIN_UNIQUE_TOTAL,
                     total_line_budget: int = common.DEFAULT_TOTAL_LINE_BUDGET,
                     max_single_file_bytes: int = common.DEFAULT_MAX_SINGLE_FILE_BYTES,
                     min_major_share: float = common.DEFAULT_MIN_MAJOR_SHARE,
                     class_size_ratio_min: float = common.DEFAULT_CLASS_SIZE_RATIO_MIN,
                     class_size_ratio_max: float = common.DEFAULT_CLASS_SIZE_RATIO_MAX,
                     other_bucket_files: int = common.DEFAULT_OTHER_BUCKET_FILES,
                     dry_run: bool = False,
                     verbose: bool = False) -> TargetResult:
    output_dir = output_root / target_name
    planned, plan_report = plan_target(
        stats, target_bytes, seed=seed, max_unique_files=max_unique_files,
        soft_corpus_budget_bytes=soft_corpus_budget_bytes,
        min_unique_total=min_unique_total, total_line_budget=total_line_budget,
        max_single_file_bytes=max_single_file_bytes, min_major_share=min_major_share,
        class_size_ratio_min=class_size_ratio_min, class_size_ratio_max=class_size_ratio_max,
        other_bucket_files=other_bucket_files,
    )
    plan_warnings = []
    if plan_report["dropped_major_families"]:
        plan_warnings.append(
            f"MAJOR FAMILIES DROPPED: {[d['label'] for d in plan_report['dropped_major_families']]}"
        )
    if plan_report["dropped_longtail_extensions"]:
        dl = plan_report["dropped_longtail_extensions"]
        plan_warnings.append(
            f"long-tail budget dropped {len(dl)} extension(s) "
            f"({plan_report['dropped_longtail_source_share']*100:.2f}% of source weight): "
            f"{dl[:15]}" + (" ..." if len(dl) > 15 else "")
        )

    if dry_run:
        total_lines = sum(p.repeat_count for p in planned)
        est_planned_avg = (
            sum(p.target_size * p.repeat_count for p in planned) / total_lines if total_lines else 0
        )
        result = TargetResult(
            target_name=target_name, target_bytes=target_bytes, output_dir=output_dir,
            actual_weighted_average=est_planned_avg,
            average_error_percent=((est_planned_avg - target_bytes) / target_bytes * 100) if target_bytes else 0,
            total_resource_entries=total_lines, unique_file_count=len(planned),
            total_corpus_bytes=sum(p.target_size for p in planned),
            estimated_httpblaster_memory_bytes=sum(p.target_size for p in planned),
            mime_distribution={}, extension_distribution={}, warnings=["dry-run: no files written"] + plan_warnings,
            seed=seed, iterations=0, within_tolerance=True, plan_report=plan_report,
        )
        return result

    factor = 1.0
    result = None
    for iteration in range(1, retry_limit + 1):
        this_plan = rescale_plan_sizes(planned, factor, max_single_file_bytes) if factor != 1.0 else planned
        result = execute_plan(this_plan, target_bytes, output_dir, seed=seed, verbose=verbose)
        result.plan_report = plan_report
        result.warnings = plan_warnings + result.warnings
        result.iterations = iteration
        error = abs(result.actual_weighted_average - target_bytes) / target_bytes if target_bytes else 0
        if error <= size_tolerance:
            result.within_tolerance = True
            break
        result.within_tolerance = False
        if result.actual_weighted_average > 0:
            factor *= target_bytes / result.actual_weighted_average
        if verbose:
            print(f"  [retry {iteration}] actual={result.actual_weighted_average:.0f}B "
                  f"target={target_bytes}B error={error * 100:.2f}% -> resizing by {factor:.4f} "
                  f"(weights unchanged)")

    write_validation_report(result, output_dir)
    write_summary(result, output_dir, mime_tolerance=mime_tolerance, baseline=stats, size_tolerance=size_tolerance)
    return result

