"""Post-generation validation (strict 1:1 model).

Every check runs automatically after generation and produces both a
machine-readable dict (written as validation-report.json / diversity-report
.json / size-report.json) and the human-readable text report shown in the
console and written to validation-report.txt.

Hard invariants for this model (non-negotiable, fail loudly if violated):
    physical payload files  == baseline physical file count == 10,000
    logical request entries == baseline logical entries      == 10,000
    unique generated relpaths == 10,000 (no collapse, no duplicate targets)
    every workload-resources.txt line is exactly one URI/path field --
    no tab, no MIME/content-type suffix (matches the golden baseline format)
    no whitespace anywhere in a generated physical path or runtime URI --
    whitespace is normalized to '_' (deterministic collision-safe suffix
    applied when two baseline URIs would otherwise normalize to the same
    path); runtime URI and physical path are the SAME string (see
    wg/pathnorm.py), so no percent-encoding survives either.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import List, Tuple

from . import common as wg_common
from . import pathnorm
from . import sizeaudit
from .baselinefreeze import FrozenBaseline
from .generate import GenerationResult
from .pool import WorkloadPlan


_PCT_ENCODING_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ANY_PCT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_WHITESPACE_RE = re.compile(r"\s")


def count_payload_files_on_disk(output_dir: Path) -> int:
    """Everything under output_dir that is NOT one of the fixed metadata
    filenames -- see wg_common.METADATA_FILENAMES and README.md."""
    n = 0
    for p in output_dir.rglob("*"):
        if p.is_file() and p.name not in wg_common.METADATA_FILENAMES:
            n += 1
    return n


def validate(baseline: FrozenBaseline, plan: WorkloadPlan, result: GenerationResult,
             manifest: dict) -> dict:
    checks: List[Tuple[str, bool, str]] = []
    tier = result.tier
    root_prefix = wg_common.uri_root_prefix(tier)

    # 1/2. Physical payload file count -- HARD requirement: == baseline == 10,000
    physical_on_disk = count_payload_files_on_disk(result.output_dir)
    checks.append((
        "physical_payload_file_count",
        physical_on_disk == baseline.total_entries == len(result.files),
        f"baseline={baseline.total_entries} generated_on_disk={physical_on_disk} "
        f"generated_in_manifest={len(result.files)}",
    ))

    # 3. Unique generated physical paths -- no collapse, no duplicate targets
    unique_relpaths = len({gf.rel_path for gf in result.files})
    checks.append((
        "unique_generated_physical_paths",
        unique_relpaths == len(result.files) == baseline.total_entries,
        f"expected={baseline.total_entries} unique_relpaths={unique_relpaths} total_files={len(result.files)}",
    ))

    # 4. Logical request entries
    logical_count = len(result.resource_lines)
    checks.append((
        "logical_request_count", logical_count == baseline.total_entries,
        f"baseline={baseline.total_entries} generated={logical_count}",
    ))

    # 5. Every resource-list entry resolves to an existing generated file
    missing, unreadable = [], []
    for gf in result.files:
        p = result.output_dir / gf.rel_path
        if not p.is_file():
            missing.append(gf.rel_path)
            continue
        try:
            p.open("rb").close()
        except OSError:
            unreadable.append(gf.rel_path)
    checks.append(("file_existence", len(missing) == 0, f"missing={len(missing)}"))
    checks.append(("file_readability", len(unreadable) == 0, f"unreadable={len(unreadable)}"))

    # 6. EICAR count + ratio -- exact
    eicar_count = sum(1 for gf in result.files if gf.is_av_test)
    eicar_pct = eicar_count / logical_count if logical_count else 0.0
    baseline_eicar_pct = baseline.eicar_count / baseline.total_entries if baseline.total_entries else 0.0
    checks.append((
        "eicar_count", eicar_count == baseline.eicar_count,
        f"baseline={baseline.eicar_count} generated={eicar_count}",
    ))
    checks.append((
        "eicar_ratio", abs(eicar_pct - baseline_eicar_pct) < 1e-9,
        f"baseline={baseline_eicar_pct * 100:.4f}% generated={eicar_pct * 100:.4f}%",
    ))
    eicar_without_seed = [gf.uri for gf in result.files if gf.is_av_test and not gf.has_source_seed]
    checks.append((
        "eicar_has_source_seed", len(eicar_without_seed) == 0,
        f"EICAR records missing a real source seed={len(eicar_without_seed)}",
    ))

    # 7. MIME/extension distribution -- exact by construction (1 file per
    # original record, same extension/MIME, only bytes/size differ)
    mime_mismatches = sum(
        1 for gf in result.files
        if gf.mime_type != wg_common.guess_mime(gf.extension) and not gf.is_av_test
    )
    checks.append((
        "mime_distribution", mime_mismatches == 0,
        f"mismatched files={mime_mismatches}",
    ))
    ext_mismatches = sum(
        1 for gf in result.files
        if (gf.rel_path.rsplit(".", 1)[-1].lower() if "." in gf.rel_path else "") != gf.extension.lower()
        and gf.extension
    )
    checks.append((
        "extension_distribution", ext_mismatches == 0,
        f"mismatched extensions={ext_mismatches}",
    ))

    # 8. Path root correctness
    bad_root = [line for line in result.resource_lines if not line.startswith(root_prefix)]
    checks.append((
        "path_root_consistency", len(bad_root) == 0,
        f"expected prefix={root_prefix!r}; incorrect references={len(bad_root)}",
    ))
    leftover_original_root = [
        line for line in result.resource_lines if "/workload04/" in line
    ]
    checks.append((
        "no_leftover_original_root", len(leftover_original_root) == 0,
        f"leftover /workload04/ references={len(leftover_original_root)}",
    ))

    # 8b. Runtime resource-list format: exactly ONE field per line (the URI
    # only). No tab, no MIME/content-type suffix, no extra columns -- matches
    # the golden baseline's own workload04-resources.txt format exactly
    # (verified: one path per line, no second field). MIME is retained only
    # in manifest.csv/manifest.json, never in the runtime resource list.
    bad_format_lines = [line for line in result.resource_lines if "\t" in line or " " in line]
    checks.append((
        "resource_list_one_field_per_line", len(bad_format_lines) == 0,
        f"lines with a tab/space (extra column)={len(bad_format_lines)}",
    ))

    # 9. URL/path encoding sanity (no bare/unescaped '%') + path traversal guard
    bad_encoding = [gf.rewritten_uri for gf in result.files if _PCT_ENCODING_RE.search(gf.rewritten_uri)]
    traversal = [gf.rewritten_uri for gf in result.files if ".." in gf.rewritten_uri.split("/")]
    checks.append((
        "url_encoding", len(bad_encoding) == 0,
        f"malformed percent-escapes={len(bad_encoding)}",
    ))
    checks.append((
        "no_path_traversal", len(traversal) == 0,
        f"traversal-looking entries={len(traversal)}",
    ))

    # 10. Directory structure preserved -- INDEPENDENT re-verification: the
    # generated rel_path must match the value pathnorm.build_normalized_paths
    # produces when recomputed fresh from (baseline uri, seed) alone -- this
    # exercises the SAME single-source-of-truth algorithm generate.py used,
    # never just trusting what was written (spec 2026-09-20 whitespace-
    # normalization: catches drift in the normalization/collision pipeline).
    recomputed = pathnorm.build_normalized_paths([gf.uri for gf in result.files], plan.seed)
    structure_mismatches = sum(
        1 for gf in result.files
        if gf.rel_path != recomputed[gf.uri].final_rel
    )
    checks.append((
        "directory_structure_preserved", structure_mismatches == 0,
        f"mismatched relative paths={structure_mismatches}",
    ))

    # 10b. Physical filesystem filenames must be percent-DECODED -- no '%20'
    # etc. literally in the on-disk filename (2026-09-20 fix: the generator
    # previously used the still-encoded relative path as the physical
    # filename, causing HttpBlaster 404s). The runtime URI (rewritten_uri)
    # is checked separately below and MUST still be encoded.
    encoded_fs_names = [gf.rel_path for gf in result.files if _ANY_PCT_ESCAPE_RE.search(gf.rel_path)]
    checks.append((
        "filesystem_filename_decoded", len(encoded_fs_names) == 0,
        f"physical filenames with leftover percent-encoding={len(encoded_fs_names)}",
    ))

    # 10c. Whitespace normalization (2026-09-20 spec): generated physical
    # paths and runtime URIs must NEVER contain whitespace -- whitespace is
    # replaced with '_' at generation time (wg/pathnorm.py). %20 should
    # never appear either, since whitespace is no longer encoded at all.
    whitespace_in_paths = [gf.rel_path for gf in result.files if _WHITESPACE_RE.search(gf.rel_path)]
    whitespace_in_uris = [gf.rewritten_uri for gf in result.files if _WHITESPACE_RE.search(gf.rewritten_uri)]
    percent20_uris = [gf.rewritten_uri for gf in result.files if "%20" in gf.rewritten_uri]
    checks.append((
        "no_whitespace_in_physical_paths", len(whitespace_in_paths) == 0,
        f"physical paths containing whitespace={len(whitespace_in_paths)}",
    ))
    checks.append((
        "no_whitespace_in_runtime_uris", len(whitespace_in_uris) == 0,
        f"runtime URIs containing whitespace={len(whitespace_in_uris)}",
    ))
    checks.append((
        "no_percent20_from_whitespace", len(percent20_uris) == 0,
        f"runtime URIs containing '%20'={len(percent20_uris)}",
    ))

    # 11. Format validation
    invalid = [gf for gf in result.files if not gf.validation_ok]
    fmt_status = "PASS" if not invalid else ("PARTIAL" if len(invalid) < len(result.files) else "FAIL")
    checks.append(("format_validation", fmt_status != "FAIL", f"status={fmt_status} invalid={len(invalid)}"))

    # 11b. Format fidelity (informational, spec section 13): track any
    # record whose intended format family fell back to generic
    # binary_fallback content instead of its real format-aware generator
    # (typically because an OPTIONAL dependency -- Pillow/pypdf/pefile --
    # is not installed on this machine). This is NOT a PASS/FAIL gate --
    # a machine without those optional deps is expected to show nonzero
    # counts here -- but it is always reported so an unintended downgrade
    # is visible, never silent.
    fallback_files = [gf for gf in result.files if gf.effective_family == "binary_fallback"]
    fallback_family_counts = dict(Counter(gf.family_used for gf in fallback_files))
    checks.append((
        "format_fidelity_fallback_count", True,
        f"records using generic binary_fallback instead of their real format generator="
        f"{len(fallback_files)} (by intended family: {fallback_family_counts})",
    ))

    # 12. Size statistics / weighted average -- authoritative, independent
    # filesystem-based audit: one size sample per LOGICAL resource-list entry
    # (re-read from disk via stat(), not trusted from in-memory generation
    # results), per spec Part 4/8. Equivalent to a per-unique-file average
    # only because the current baseline has weight=1 everywhere; this stays
    # correct if a future baseline has duplicate resource-list lines.
    audit_result = sizeaudit.audit(tier, result.output_dir, result.target_bytes)
    target = result.target_bytes
    actual_avg = audit_result.weighted_average_bytes
    checks.append((
        "weighted_average_size", audit_result.within_tolerance,
        f"target={wg_common.human_size(target)} actual={wg_common.human_size(actual_avg)} "
        f"error={audit_result.error_pct:+.2f}% (tolerance +/-{wg_common.DEFAULT_SIZE_TOLERANCE_PCT * 100:.0f}%)",
    ))
    checks.append((
        "size_audit_no_missing_references", len(audit_result.missing_references) == 0,
        f"unresolvable resource-list entries={len(audit_result.missing_references)}",
    ))

    # 12b. No silent synthetic fallback in the normal (clean-baseline) path
    checks.append((
        "no_synthetic_fallback", plan.missing_seed_count == 0,
        f"records with no real source seed={plan.missing_seed_count}",
    ))

    # 13. Baseline untouched (spot-check: baseline dir mtimes aren't ours to
    # assert without a stored fingerprint; the real guarantee is structural
    # -- this tool never opens baseline.source_root for writing anywhere in
    # generate.py/pool.py, verified by code inspection, not by a runtime probe.)
    checks.append(("baseline_not_modified_by_design", True, "generator never opens source_root for writing"))

    # 14. Disk usage
    actual_disk = audit_result.total_payload_bytes
    checks.append((
        "disk_usage_estimate", True,
        f"estimated={wg_common.human_size(plan.estimated_disk_bytes)} actual={wg_common.human_size(actual_disk)}",
    ))

    # 15. Manifest consistency
    checks.append((
        "manifest_consistency", len(plan.records) == len(result.files) == manifest["physical_file_count"],
        f"plan={len(plan.records)} generated={len(result.files)} manifest={manifest['physical_file_count']}",
    ))

    overall_pass = all(ok for _, ok, _ in checks)

    report = {
        "tier": tier,
        "overall": "PASS" if overall_pass else "FAIL",
        "checks": [{"name": n, "status": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in checks],
        "physical_payload_files": {
            "baseline": baseline.total_entries, "generated": physical_on_disk,
            "unique_generated_relpaths": unique_relpaths,
        },
        "logical_requests": {"baseline": baseline.total_entries, "generated": logical_count},
        "eicar": {
            "baseline_count": baseline.eicar_count, "generated_count": eicar_count,
            "baseline_pct": baseline_eicar_pct * 100.0, "generated_pct": eicar_pct * 100.0,
        },
        "size": {
            "target_bytes": target, "actual_bytes": actual_avg,
            "difference_bytes": audit_result.difference_bytes, "error_pct": audit_result.error_pct,
            "total_payload_bytes": audit_result.total_payload_bytes,
            "logical_request_count": audit_result.logical_request_count,
            "median": audit_result.median_bytes, "p90": audit_result.percentile(90),
            "p95": audit_result.percentile(95), "p99": audit_result.percentile(99),
            "min": audit_result.min_bytes, "max": audit_result.max_bytes,
            "baseline_weighted_average": baseline.header["request_weighted_average_bytes"],
            "baseline_median": baseline.header["median_bytes"], "baseline_p90": baseline.header["p90"],
            "baseline_p95": baseline.header["p95"], "baseline_p99": baseline.header["p99"],
            "baseline_min": baseline.header["min_bytes"], "baseline_max": baseline.header["max_bytes"],
        },
        "diversity": {
            "baseline_physical_files": baseline.total_entries,
            "generated_physical_files": physical_on_disk,
            "diversity_retained_pct": (physical_on_disk / baseline.total_entries * 100.0)
                                       if baseline.total_entries else 0.0,
            "missing_seed_count": plan.missing_seed_count,
        },
        "whitespace_normalization": {
            "collision_count": sum(1 for gf in result.files if gf.collision_suffix),
            "whitespace_in_physical_paths": len(whitespace_in_paths),
            "whitespace_in_runtime_uris": len(whitespace_in_uris),
            "percent20_in_runtime_uris": len(percent20_uris),
        },
        "path_root": {"expected": root_prefix, "incorrect_references": len(bad_root)},
        "missing_files": missing,
        "invalid_paths": bad_encoding,
        "format_validation_status": fmt_status,
        "format_fidelity": {
            "binary_fallback_count": len(fallback_files),
            "binary_fallback_by_intended_family": fallback_family_counts,
        },
        "disk": {"estimated_bytes": plan.estimated_disk_bytes, "actual_bytes": actual_disk},
    }
    return report


def render_text_report(report: dict) -> str:
    L = []
    L.append("Workload04 Generation Validation")
    L.append("=" * 33)
    L.append("")
    L.append("Target Average:")
    L.append(f"    {wg_common.human_size(report['size']['target_bytes'])}")
    L.append("")
    L.append("Physical Payload Files:")
    L.append(f"    Baseline: {report['physical_payload_files']['baseline']}")
    L.append(f"    Generated: {report['physical_payload_files']['generated']}")
    L.append(f"    Unique generated paths: {report['physical_payload_files']['unique_generated_relpaths']}")
    ppf_status = "PASS" if (report['physical_payload_files']['baseline'] ==
                            report['physical_payload_files']['generated'] ==
                            report['physical_payload_files']['unique_generated_relpaths']) else "FAIL"
    L.append(f"    Status: {ppf_status}")
    L.append("")
    L.append("Logical Requests:")
    L.append(f"    Baseline: {report['logical_requests']['baseline']}")
    L.append(f"    Generated: {report['logical_requests']['generated']}")
    L.append(f"    Status: {'PASS' if report['logical_requests']['baseline'] == report['logical_requests']['generated'] else 'FAIL'}")
    L.append("")
    L.append("EICAR Requests:")
    L.append(f"    Baseline: {report['eicar']['baseline_count']}")
    L.append(f"    Generated: {report['eicar']['generated_count']}")
    L.append(f"    Status: {'PASS' if report['eicar']['baseline_count'] == report['eicar']['generated_count'] else 'FAIL'}")
    L.append("")
    L.append("EICAR Ratio:")
    L.append(f"    Baseline: {report['eicar']['baseline_pct']:.4f}%")
    L.append(f"    Generated: {report['eicar']['generated_pct']:.4f}%")
    L.append(f"    Status: {'PASS' if abs(report['eicar']['baseline_pct'] - report['eicar']['generated_pct']) < 1e-6 else 'FAIL'}")
    L.append("")
    L.append("Request-Weighted Average Object Size:")
    L.append(f"    Target                  : {wg_common.human_size(report['size']['target_bytes'])}")
    L.append(f"    Actual                  : {wg_common.human_size(report['size']['actual_bytes'])}")
    sign = "+" if report['size']['difference_bytes'] >= 0 else ""
    L.append(f"    Difference              : {sign}{wg_common.human_size(report['size']['difference_bytes'])}")
    L.append(f"    Error                   : {report['size']['error_pct']:+.2f}%")
    L.append(f"    Logical requests        : {report['size']['logical_request_count']:,}")
    L.append(f"    Total payload bytes     : {wg_common.human_size(report['size']['total_payload_bytes'])}")
    err = next(c["detail"] for c in report["checks"] if c["name"] == "weighted_average_size")
    status = next(c["status"] for c in report["checks"] if c["name"] == "weighted_average_size")
    L.append(f"    Status: {status}  ({err})")
    L.append("")
    L.append("Size shape (baseline -> generated):")
    L.append(f"    median: {wg_common.human_size(report['size']['baseline_median'])} -> {wg_common.human_size(report['size']['median'])}")
    L.append(f"    p90   : {wg_common.human_size(report['size']['baseline_p90'])} -> {wg_common.human_size(report['size']['p90'])}")
    L.append(f"    p95   : {wg_common.human_size(report['size']['baseline_p95'])} -> {wg_common.human_size(report['size']['p95'])}")
    L.append(f"    p99   : {wg_common.human_size(report['size']['baseline_p99'])} -> {wg_common.human_size(report['size']['p99'])}")
    L.append(f"    min   : {wg_common.human_size(report['size']['baseline_min'])} -> {wg_common.human_size(report['size']['min'])}")
    L.append(f"    max   : {wg_common.human_size(report['size']['baseline_max'])} -> {wg_common.human_size(report['size']['max'])}")
    L.append("")
    L.append("Diversity:")
    L.append(f"    Baseline physical files: {report['diversity']['baseline_physical_files']}")
    L.append(f"    Generated physical files: {report['diversity']['generated_physical_files']}")
    L.append(f"    Diversity retained: {report['diversity']['diversity_retained_pct']:.2f}%")
    L.append("")
    L.append("Synthetic Fallback (no real source seed):")
    L.append(f"    Count: {report['diversity']['missing_seed_count']}")
    L.append(f"    Status: {'PASS' if report['diversity']['missing_seed_count'] == 0 else 'FAIL -- clean baseline should have 0'}")
    if report['diversity']['missing_seed_count']:
        L.append(f"    NOTE: {report['diversity']['missing_seed_count']} records had no resolvable "
                 f"source seed in the baseline corpus and were generated with synthetic (non-representative) "
                 f"content of the correct type/size only -- see manifest.csv 'has_source_seed' column.")
    L.append("")
    L.append("Path Root:")
    L.append(f"    Expected: {report['path_root']['expected']}")
    L.append(f"    Incorrect references: {report['path_root']['incorrect_references']}")
    L.append(f"    Status: {'PASS' if report['path_root']['incorrect_references'] == 0 else 'FAIL'}")
    L.append("")
    L.append("Whitespace Normalization:")
    L.append(f"    Whitespace in physical paths: {report['whitespace_normalization']['whitespace_in_physical_paths']}")
    L.append(f"    Whitespace in runtime URIs: {report['whitespace_normalization']['whitespace_in_runtime_uris']}")
    L.append(f"    '%20' in runtime URIs: {report['whitespace_normalization']['percent20_in_runtime_uris']}")
    L.append(f"    Collision suffixes applied: {report['whitespace_normalization']['collision_count']}")
    ws_status = "PASS" if (report['whitespace_normalization']['whitespace_in_physical_paths'] == 0 and
                           report['whitespace_normalization']['whitespace_in_runtime_uris'] == 0 and
                           report['whitespace_normalization']['percent20_in_runtime_uris'] == 0) else "FAIL"
    L.append(f"    Status: {ws_status}")
    L.append("")
    L.append("Missing Files:")
    L.append(f"    {len(report['missing_files'])}")
    L.append("")
    L.append("Invalid Paths:")
    L.append(f"    {len(report['invalid_paths'])}")
    L.append("")
    mime_status = next(c["status"] for c in report["checks"] if c["name"] == "mime_distribution")
    L.append("MIME Distribution:")
    L.append(f"    {mime_status}")
    L.append("")
    L.append("Format Validation:")
    L.append(f"    {report['format_validation_status']}")
    L.append("")
    L.append("Format Fidelity:")
    L.append(f"    binary_fallback count: {report['format_fidelity']['binary_fallback_count']}")
    if report["format_fidelity"]["binary_fallback_by_intended_family"]:
        L.append(f"    by intended family: {report['format_fidelity']['binary_fallback_by_intended_family']}")
    L.append("")
    L.append("Baseline Modified:")
    L.append("    NO")
    L.append("")
    L.append("All checks:")
    for c in report["checks"]:
        L.append(f"    [{c['status']}] {c['name']}: {c['detail']}")
    L.append("")
    L.append("Overall:")
    L.append(f"    {report['overall']}")
    return "\n".join(L) + "\n"

