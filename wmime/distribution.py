"""Source-vs-generated MIME/family distribution reporting.

Shared by generator.py (embeds a report in summary.json) and validator.py
(uses the same numbers to decide PASS/FAIL) so both sides always agree on
what "the distribution" means -- computed strictly from the *realized*
resource list + generated files, never from the theoretical plan.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from . import common


def source_family_shares(stats) -> dict:
    """Request-weighted share of each MAJOR family label (+ 'Other') in the
    source corpus, computed from the golden baseline's FULL request-list
    weight (master task: exact preservation) -- a record whose backing file
    happens to be missing on this checkout still counts toward its
    category's true baseline weight; only content SELECTION requires an
    existing file. AV-test (EICAR) URIs are excluded from every family
    bucket's numerator -- they are tracked as their own dedicated category,
    never folded into ZIP -- but still count toward the TOTAL denominator so
    shares stay comparable to the generator's own accounting."""
    ext_weight = Counter()
    total = 0
    for r in stats.records:
        total += r.weight
        if r.uri in common.AV_TEST_URIS:
            continue
        ext_weight[r.ext] += r.weight
    total = total or 1
    shares = defaultdict(float)
    for ext, w in ext_weight.items():
        label = common.major_family_label(ext) or "Other"
        shares[label] += w / total
    return dict(shares)


def source_family_counts(stats) -> dict:
    """Request-weighted count (not share) of each MAJOR family label + 'Other'
    in the source corpus -- needed for the ratio-audit CSVs, which report
    absolute counts alongside shares (master task section 14). See
    source_family_shares for the full-baseline-weight and AV-test-exclusion
    rationale."""
    ext_weight = Counter()
    for r in stats.records:
        if r.uri in common.AV_TEST_URIS:
            continue
        ext_weight[r.ext] += r.weight
    counts = defaultdict(int)
    for ext, w in ext_weight.items():
        label = common.major_family_label(ext) or "Other"
        counts[label] += w
    return dict(counts)


def generated_family_report(manifest_rows) -> dict:
    """From manifest rows (dicts with extension/target_class/request_weight),
    compute per-major-family (+ 'Other') generated share and fallback weight.

    manifest_rows: iterable of dicts (or csv.DictReader rows) with at least
    'extension', 'target_class', 'request_weight' keys (string or int ok).
    AV-test (EICAR) rows are excluded from every family bucket here too --
    they're tracked as their own dedicated category (see _av_test_summary /
    ratio_audit_rows), never folded into ZIP, matching source_family_shares/
    source_family_counts's exclusion so both sides of the comparison agree.
    """
    family_weight = Counter()
    family_fallback_weight = Counter()
    ext_weight = Counter()
    total_weight = 0
    for row in manifest_rows:
        w = int(row["request_weight"])
        total_weight += w  # AV-test rows still count toward the total, just not any family bucket
        if str(row.get("is_av_test", "")).strip().lower() in ("true", "1"):
            continue
        ext = (row["extension"] or "").lower()
        label = common.major_family_label(ext) or "Other"
        family_weight[label] += w
        ext_weight[ext] += w
        if row["target_class"] == "binary_fallback":
            family_fallback_weight[label] += w

    shares = {label: w / total_weight for label, w in family_weight.items()} if total_weight else {}
    fallback_fraction = {
        label: family_fallback_weight.get(label, 0) / family_weight[label]
        for label in family_weight if family_weight[label]
    }
    return {
        "shares": shares,
        "weights": dict(family_weight),
        "ext_weights": dict(ext_weight),
        "fallback_weight": dict(family_fallback_weight),
        "fallback_fraction": fallback_fraction,
        "total_weight": total_weight,
    }


def build_comparison(stats, manifest_rows) -> dict:
    """Combine source + generated numbers into one comparison table, keyed by
    MAJOR_FAMILY_LABELS + 'Other'."""
    src = source_family_shares(stats)
    src_counts = source_family_counts(stats)
    gen = generated_family_report(manifest_rows)
    labels = list(common.MAJOR_FAMILY_LABELS) + ["Other"]
    rows = []
    for label in labels:
        rows.append({
            "label": label,
            "source_share": src.get(label, 0.0),
            "source_count": src_counts.get(label, 0),
            "generated_share": gen["shares"].get(label, 0.0),
            "generated_weight": gen["weights"].get(label, 0),
            "generated_count": gen["weights"].get(label, 0),
            "fallback_weight": gen["fallback_weight"].get(label, 0),
            "fallback_fraction": gen["fallback_fraction"].get(label, 0.0),
            "present_in_source": src.get(label, 0.0) > 0,
            "present_in_generated": gen["weights"].get(label, 0) > 0,
        })
    return {"rows": rows, "total_weight": gen["total_weight"]}


def tolerance_for_share(share: float) -> float:
    """Absolute percentage-point tolerance bucketed by baseline share size
    (master task sections 4, 18, 19, 20): major (>=1%) classes get a wider
    absolute tolerance than minor (<1%) classes, because a ratio-based check
    is meaningless near zero (e.g. 0.01% baseline "failing" at 3x is really
    just 0.02pp, invisible in practice)."""
    if share >= common.DEFAULT_MINOR_CLASS_SHARE_THRESHOLD:
        return common.DEFAULT_MAJOR_CLASS_ABS_TOLERANCE_PP
    return common.DEFAULT_MINOR_CLASS_ABS_TOLERANCE_PP


def ratio_audit_rows(stats, manifest_rows, av_test_summary: dict = None) -> list:
    """Build the master-task section 14 ratio_audit.csv rows: one per MAJOR
    family label + 'Other', plus a dedicated 'EICAR/AV-test' row (which is a
    subset of ZIP, reported separately for visibility per section 23).

    2026-09-19: PASS requires an EXACT integer count match against the
    golden baseline (generated_count == baseline_count), not "within
    tolerance" -- the master task explicitly rejects an approximate-but-
    close request distribution. absolute_pp_difference/relative_difference_
    percent are still computed and reported for transparency, but they are
    NOT the pass/fail gate anymore.
    """
    comparison = build_comparison(stats, manifest_rows)
    rows = []
    for row in comparison["rows"]:
        baseline_count = row["source_count"]
        baseline_share = row["source_share"]
        generated_count = row["generated_weight"]
        generated_share = row["generated_share"]
        abs_pp = generated_share - baseline_share
        rel_pct = (abs_pp / baseline_share * 100.0) if baseline_share else (
            0.0 if generated_share == 0 else float("inf")
        )
        status = "PASS" if generated_count == baseline_count else "FAIL"
        rows.append({
            "category": row["label"],
            "baseline_count": baseline_count,
            "baseline_share": baseline_share,
            "generated_count": generated_count,
            "generated_share": generated_share,
            "absolute_pp_difference": abs_pp,
            "relative_difference_percent": rel_pct,
            "pass_fail": status,
        })
    if av_test_summary:
        baseline_count = av_test_summary.get("baseline_request_count", 0)
        baseline_share = av_test_summary.get("baseline_request_share", 0.0)
        generated_count = av_test_summary.get("generated_request_count", 0)
        generated_share = av_test_summary.get("generated_request_share", 0.0)
        abs_pp = generated_share - baseline_share
        rel_pct = (abs_pp / baseline_share * 100.0) if baseline_share else (
            0.0 if generated_share == 0 else float("inf")
        )
        status = "PASS" if generated_count == baseline_count else "FAIL"
        rows.append({
            "category": "EICAR/AV-test",
            "baseline_count": baseline_count,
            "baseline_share": baseline_share,
            "generated_count": generated_count,
            "generated_share": generated_share,
            "absolute_pp_difference": abs_pp,
            "relative_difference_percent": rel_pct,
            "pass_fail": status,
        })
    return rows


def write_ratio_audit_csv(rows: list, out_path) -> None:
    import csv
    fieldnames = ["category", "baseline_count", "baseline_share", "generated_count", "generated_share",
                  "absolute_pp_difference", "relative_difference_percent", "pass_fail"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fieldnames)
        for r in rows:
            w.writerow([
                r["category"],
                r["baseline_count"],
                f"{r['baseline_share'] * 100:.4f}%",
                r["generated_count"],
                f"{r['generated_share'] * 100:.4f}%",
                f"{r['absolute_pp_difference'] * 100:+.4f}pp",
                ("inf" if r["relative_difference_percent"] == float("inf")
                 else f"{r['relative_difference_percent']:+.2f}%"),
                r["pass_fail"],
            ])
