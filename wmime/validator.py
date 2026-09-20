"""Standalone validator for a generated workload directory.

2026-09-19 master-task rework: EICAR/AV-test content is a MANDATORY, retained
baseline component (see common.AV_TEST_URIS), not something to reject on
sight. Validation now checks that the mandatory AV-test payload is still
genuinely present (no accidental removal) and that no *other* file
accidentally acquired an EICAR marker (no accidental contamination), and that
AV-test request frequency tracks its baseline share. Distribution PASS/FAIL
for every MAJOR family is decided by absolute percentage-point deviation from
the true baseline share (bucketed major >=1% / minor <1%), not a ratio -- see
docs/QUALIFICATION_REPORT.md for the full rationale.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import List, Tuple

from . import common, formats, distribution, baseline

EICAR_MARKER = common.EICAR_MARKER


def _load_manifest(target_dir: Path) -> list:
    manifest_path = target_dir / "manifest.csv"
    if not manifest_path.is_file():
        return []
    with manifest_path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _family_by_uri(manifest_rows: list) -> dict:
    """Map the resource-list URI (files/... path re-based to /ext/name) ->
    effective family, from manifest.csv's target_class column."""
    out = {}
    for row in manifest_rows:
        gen_file = row.get("generated_file", "").replace("\\", "/")
        if gen_file.startswith("files/"):
            uri = "/" + gen_file[len("files/"):]
        else:
            uri = "/" + gen_file
        family = row.get("target_class")
        if family:
            out[uri] = family
    return out


def _av_test_flag_by_uri(manifest_rows: list) -> dict:
    """Map generated resource-list URI -> whether it's a mandatory AV-test
    file (manifest.csv's is_av_test column, written by generator.py)."""
    out = {}
    for row in manifest_rows:
        gen_file = row.get("generated_file", "").replace("\\", "/")
        uri = "/" + gen_file[len("files/"):] if gen_file.startswith("files/") else "/" + gen_file
        out[uri] = str(row.get("is_av_test", "")).strip().lower() in ("true", "1")
    return out


def _parse_resource_list(path: Path) -> List[Tuple[str, str]]:
    entries = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            uri, mime = line.split("\t", 1)
        elif line.startswith('"'):
            close = line.find('"', 1)
            uri = line[1:close] if close != -1 else line
            mime = ""
        else:
            uri, mime = line, ""
        entries.append((uri.strip(), mime.strip()))
    return entries


def validate_workload_dir(target_dir: Path, *,
                           size_tolerance: float = common.DEFAULT_SIZE_TOLERANCE,
                           mime_tolerance: float = common.DEFAULT_MIME_TOLERANCE,
                           major_class_abs_tolerance_pp: float = common.DEFAULT_MAJOR_CLASS_ABS_TOLERANCE_PP,
                           minor_class_abs_tolerance_pp: float = common.DEFAULT_MINOR_CLASS_ABS_TOLERANCE_PP,
                           minor_class_share_threshold: float = common.DEFAULT_MINOR_CLASS_SHARE_THRESHOLD,
                           max_major_fallback_fraction: float = common.DEFAULT_MAX_MAJOR_FALLBACK_FRACTION,
                           verbose: bool = False) -> Tuple[bool, List[str], dict]:
    """Returns (ok, problems, report). `report` always contains whatever could
    be computed, even on failure, so callers can print diagnostics."""
    problems: List[str] = []
    report: dict = {}
    resource_list = target_dir / "workload-resources.txt"
    files_dir = target_dir / "files"
    summary_path = target_dir / "summary.json"

    if not resource_list.is_file():
        return False, [f"missing workload-resources.txt in {target_dir}"], report

    entries = _parse_resource_list(resource_list)
    if not entries:
        return False, ["workload-resources.txt has no usable entries"], report

    manifest_rows = _load_manifest(target_dir)
    if not manifest_rows:
        problems.append("missing/empty manifest.csv -- cannot verify format validity or MIME distribution")

    weight = Counter(uri for uri, _ in entries)
    family_by_uri = _family_by_uri(manifest_rows)
    av_test_by_uri = _av_test_flag_by_uri(manifest_rows)

    # --- 1/8/9: file existence, structural format validity ------------------
    # --- P/Q (section 16): no accidental EICAR removal / no accidental EICAR
    #     insertion -- checked per-file against the *expected* AV-test set,
    #     not a blanket "any EICAR anywhere = FAIL" (EICAR is mandatory here).
    missing = []
    format_failures = []
    eicar_removed = []
    eicar_contaminated = []
    total_bytes_weighted = 0
    total_weight = 0
    unique_bytes_sum = 0

    for uri, count in weight.items():
        rel = uri.lstrip("/")
        path = files_dir / rel
        if not path.is_file():
            missing.append(uri)
            continue
        data = path.read_bytes()
        ext = Path(uri).suffix.lstrip(".").lower()
        has_eicar = baseline._contains_eicar(data, ext)
        expected_av_test = av_test_by_uri.get(uri, False)
        if expected_av_test and not has_eicar:
            eicar_removed.append(uri)
        elif not expected_av_test and has_eicar:
            eicar_contaminated.append(uri)
        family = family_by_uri.get(uri, common.classify_family(ext))
        ok, msg = formats.validate(family, data)
        if not ok:
            format_failures.append(f"{uri}: {msg}")
        total_bytes_weighted += len(data) * count
        total_weight += count
        unique_bytes_sum += len(data)

    if missing:
        problems.append(f"{len(missing)} referenced file(s) missing on disk: {missing[:10]}"
                         + (" ..." if len(missing) > 10 else ""))
    if format_failures:
        problems.append(f"{len(format_failures)} file(s) failed structural validation: "
                         + "; ".join(format_failures[:5]) + (" ..." if len(format_failures) > 5 else ""))
    if eicar_removed:
        problems.append(f"AV-test file(s) LOST their EICAR/AV signature (accidental removal): {eicar_removed}")
    if eicar_contaminated:
        problems.append(f"EICAR/AV signature found in NON-AV-test file(s) (accidental contamination): "
                         f"{eicar_contaminated}")

    actual_avg = total_bytes_weighted / total_weight if total_weight else 0.0

    # --- 2: target-size tolerance -------------------------------------------
    target_bytes = None
    summary = {}
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        target_bytes = summary.get("target_average_bytes")
        if target_bytes:
            error = abs(actual_avg - target_bytes) / target_bytes
            if error > size_tolerance:
                problems.append(
                    f"request-weighted average {actual_avg:.0f}B deviates {error * 100:.2f}% "
                    f"from target {target_bytes}B (tolerance {size_tolerance * 100:.1f}%)"
                )
    else:
        problems.append("summary.json missing; cannot verify target-size tolerance")

    # --- request distribution: total entry count must be EXACT --------------
    # 2026-09-19 master task: the generated resource list must reproduce the
    # golden baseline's total request count exactly (e.g. 10000), not some
    # smaller "line budget" -- reduced total request-list size is rejected
    # even if every per-category share still looks reasonable.
    baseline_total_weight = summary.get("plan_report", {}).get("baseline_total_weight")
    if baseline_total_weight is not None and total_weight != baseline_total_weight:
        problems.append(
            f"total resource-list entries do NOT exactly match baseline: "
            f"generated={total_weight} vs baseline={baseline_total_weight} -- EXACT match required"
        )

    # --- 3/4/10/11: MIME family presence, fallback, distribution deviation -
    # Generated-side numbers are recomputed independently from manifest.csv
    # (never trusted from summary.json) so validation can't be spoofed/stale.
    gen_report = distribution.generated_family_report(manifest_rows) if manifest_rows else {
        "shares": {}, "weights": {}, "fallback_weight": {}, "fallback_fraction": {}, "total_weight": 0,
    }
    # Source-side reference counts/shares come from summary.json's recorded
    # major_family_comparison (written by the generator from the same
    # baseline the target was planned against).
    source_shares = {}
    source_counts = {}
    for row in summary.get("major_family_comparison", []):
        source_shares[row["label"]] = row.get("source_share", 0.0)
        source_counts[row["label"]] = row.get("source_count", 0)

    major_present_in_source = [lbl for lbl in common.MAJOR_FAMILY_LABELS if source_shares.get(lbl, 0) > 0]

    missing_major = [lbl for lbl in major_present_in_source if gen_report["shares"].get(lbl, 0) <= 0]
    if missing_major:
        problems.append(f"major MIME families present in source but MISSING from generated output: {missing_major}")

    fallback_major = []
    for lbl in major_present_in_source:
        frac = gen_report["fallback_fraction"].get(lbl, 0.0)
        if frac > max_major_fallback_fraction:
            fallback_major.append(f"{lbl} ({frac * 100:.1f}% binary_fallback)")
    if fallback_major:
        problems.append(f"major MIME families using binary_fallback (must be genuine content): {fallback_major}")

    # 2026-09-19: request distribution must be EXACT, not "within tolerance" --
    # generated_count must equal baseline_count for every major family (and
    # 'Other', checked separately by the ratio-audit report). A pp-tolerance
    # pass is no longer acceptable; only genuine integer equality passes.
    distorted = []
    for lbl in major_present_in_source:
        gcount = gen_report["weights"].get(lbl, 0)
        scount = source_counts.get(lbl, 0)
        if gcount <= 0:
            continue  # already reported via missing_major
        if gcount != scount:
            gshare = gen_report["shares"].get(lbl, 0.0)
            sshare = source_shares.get(lbl, 0.0)
            distorted.append(
                f"{lbl}: generated count={gcount} ({gshare*100:.4f}%) vs baseline count={scount} "
                f"({sshare*100:.4f}%) -- EXACT match required, not tolerance"
            )
    if distorted:
        problems.append(f"major MIME classes do NOT exactly match baseline request count: {distorted}")

    # --- 17: AV-test/EICAR request-frequency preservation (EXACT) -----------
    av_test_summary = summary.get("av_test_summary", {})
    baseline_av_weight = av_test_summary.get("baseline_request_count", 0)
    generated_av_weight = av_test_summary.get("generated_request_count", 0)
    baseline_av_share = av_test_summary.get("baseline_request_share", 0.0)
    generated_av_share = av_test_summary.get("generated_request_share", 0.0)
    if baseline_av_weight > 0 and generated_av_weight == 0:
        problems.append("AV-test/EICAR resources present in baseline but ZERO in generated output")
    elif baseline_av_weight > 0 and generated_av_weight != baseline_av_weight:
        problems.append(
            f"AV-test/EICAR request count does NOT exactly match baseline: "
            f"generated={generated_av_weight} ({generated_av_share*100:.4f}%) vs "
            f"baseline={baseline_av_weight} ({baseline_av_share*100:.4f}%) -- EXACT match required"
        )

    # --- report --------------------------------------------------------------
    family_comparison = []
    for lbl in common.MAJOR_FAMILY_LABELS + ["Other"]:
        family_comparison.append({
            "label": lbl,
            "source_count": source_counts.get(lbl, 0),
            "source_share": source_shares.get(lbl, 0.0),
            "generated_count": gen_report["weights"].get(lbl, 0),
            "generated_share": gen_report["shares"].get(lbl, 0.0),
            "fallback_weight": gen_report["fallback_weight"].get(lbl, 0),
            "fallback_fraction": gen_report["fallback_fraction"].get(lbl, 0.0),
        })
    report = {
        "target_average_bytes": target_bytes,
        "actual_weighted_average_bytes": actual_avg,
        "size_error_percent": ((actual_avg - target_bytes) / target_bytes * 100.0) if target_bytes else None,
        "unique_file_count": len(weight),
        "total_resource_entries": total_weight,
        "baseline_total_entries": baseline_total_weight,
        "total_corpus_bytes": unique_bytes_sum,
        "estimated_httpblaster_memory_bytes": unique_bytes_sum,
        "family_comparison": family_comparison,
        "av_test_baseline_count": baseline_av_weight,
        "av_test_baseline_share": baseline_av_share,
        "av_test_generated_count": generated_av_weight,
        "av_test_generated_share": generated_av_share,
        "total_fallback_weight": sum(gen_report["fallback_weight"].values()),
        "total_fallback_weight_fraction": (
            sum(gen_report["fallback_weight"].values()) / gen_report["total_weight"]
            if gen_report["total_weight"] else 0.0
        ),
    }

    if verbose:
        print(f"[{target_dir.name}] entries={len(entries)} unique={len(weight)} "
              f"actual_avg={common.human_size(actual_avg)}")

    return (len(problems) == 0), problems, report


def print_report(target_dir: Path, ok: bool, problems: List[str], report: dict = None) -> None:
    print(f"=== {target_dir} ===")
    print("PASS" if ok else "FAIL")
    for p in problems:
        print(f"  - {p}")
    if report:
        tb = report.get("target_average_bytes")
        aa = report.get("actual_weighted_average_bytes")
        if tb:
            print(f"  size: target={common.human_size(tb)} actual={common.human_size(aa)} "
                  f"error={report.get('size_error_percent', 0):.2f}%")
        bte = report.get("baseline_total_entries")
        gte = report.get("total_resource_entries")
        entry_match = "EXACT MATCH" if bte == gte else "MISMATCH"
        print(f"  unique_files={report.get('unique_file_count')} "
              f"resource_entries={gte} (baseline={bte}, {entry_match}) "
              f"corpus_bytes={common.human_size(report.get('total_corpus_bytes', 0))} "
              f"est_httpblaster_memory={common.human_size(report.get('estimated_httpblaster_memory_bytes', 0))}")
        print(f"  total fallback weight: {report.get('total_fallback_weight')} "
              f"({report.get('total_fallback_weight_fraction', 0) * 100:.1f}% of all requests)")
        print("  MIME family baseline_count/generated_count  baseline%/generated% (fallback%):")
        for row in report.get("family_comparison", []):
            if row["source_share"] == 0 and row["generated_share"] == 0:
                continue
            match = "=" if row["source_count"] == row["generated_count"] else "!="
            print(f"    {row['label']:6s} {row['source_count']:>6d} {match} {row['generated_count']:<6d}  "
                  f"baseline={row['source_share']*100:7.4f}%  "
                  f"generated={row['generated_share']*100:7.4f}%  "
                  f"fallback={row['fallback_fraction']*100:5.1f}%")
        bc = report.get("av_test_baseline_count")
        if bc is not None:
            gc = report.get("av_test_generated_count")
            match = "=" if bc == gc else "!="
            print(f"  AV-test/EICAR: baseline={bc} {match} generated={gc}  "
                  f"baseline={report.get('av_test_baseline_share', 0)*100:.4f}%  "
                  f"generated={report.get('av_test_generated_share', 0)*100:.4f}%")


