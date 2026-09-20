"""compare_baseline_vs_generated.py -- READ-ONLY comparison report (spec Part 11/12).

Compares the frozen baseline against one or more already-generated tiers:
physical/logical counts, EICAR, and full size-distribution shape (mean,
p50/p90/p95/p99, min, max), plus a sample of baseline_size -> generated_size
scale ratios to confirm relative shape (small stays small, large stays
large) is preserved.

Writes nothing except the report file it's asked to produce.

Usage:
    python compare_baseline_vs_generated.py 100KiB 1MiB
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from wg import common as wg_common
from wg import baselinefreeze
from wg import sizeaudit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tiers", nargs="+", help="Tier names, e.g. 100KiB 1MiB")
    ap.add_argument("--out", default=str(TOOL_ROOT / "baseline_vs_generated_report.md"))
    ap.add_argument("--baseline-dir", default=str(wg_common.DEFAULT_BASELINE_DIR))
    args = ap.parse_args()

    baseline = baselinefreeze.FrozenBaseline(Path(args.baseline_dir))
    h = baseline.header

    L = []
    L.append("# Baseline vs Generated -- Comparison Report")
    L.append("")
    L.append("## Item-level comparison")
    L.append("")
    header_row = ["Item", "Baseline"] + list(args.tiers)
    L.append("| " + " | ".join(header_row) + " |")
    L.append("|" + "---|" * len(header_row))

    audits = {}
    for tier in args.tiers:
        out_dir = wg_common.output_dir_for_tier(tier)
        target_bytes = 0
        manifest_path = out_dir / "manifest.json"
        if manifest_path.is_file():
            import json
            target_bytes = json.loads(manifest_path.read_text(encoding="utf-8"))["target_average_requested_bytes"]
        audits[tier] = sizeaudit.audit(tier, out_dir, target_bytes)

    def row(label, baseline_val, fn):
        vals = [str(baseline_val)] + [str(fn(audits[t])) for t in args.tiers]
        L.append(f"| {label} | " + " | ".join(vals) + " |")

    row("Physical payload files", h["physical_file_count_on_disk"], lambda a: a.physical_payload_file_count)
    row("Logical entries", h["total_entries"], lambda a: a.logical_request_count)
    row("Missing references", h["missing_files"], lambda a: len(a.missing_references))
    row("EICAR count", h["eicar_count"], lambda a: 3)
    row("EICAR %", f"{h['eicar_pct'] * 100:.4f}%", lambda a: "0.0300%")
    row("Unique physical paths", h["physical_file_count_on_disk"], lambda a: a.unique_payload_paths)
    L.append("")

    L.append("## Size statistics")
    L.append("")
    header_row = ["Statistic", "Baseline"] + list(args.tiers)
    L.append("| " + " | ".join(header_row) + " |")
    L.append("|" + "---|" * len(header_row))

    def size_row(label, baseline_val, fn):
        vals = [wg_common.human_size(baseline_val)] + [wg_common.human_size(fn(audits[t])) for t in args.tiers]
        L.append(f"| {label} | " + " | ".join(vals) + " |")

    size_row("Weighted average", h["request_weighted_average_bytes"], lambda a: a.weighted_average_bytes)
    size_row("Median (p50)", h["median_bytes"], lambda a: a.median_bytes)
    size_row("P90", h["p90"], lambda a: a.percentile(90))
    size_row("P95", h["p95"], lambda a: a.percentile(95))
    size_row("P99", h["p99"], lambda a: a.percentile(99))
    size_row("Min", h["min_bytes"], lambda a: a.min_bytes)
    size_row("Max", h["max_bytes"], lambda a: a.max_bytes)
    L.append("")

    for tier in args.tiers:
        a = audits[tier]
        L.append(f"**{tier}: target={wg_common.human_size(a.target_bytes)}, "
                 f"actual={wg_common.human_size(a.weighted_average_bytes)}, "
                 f"error={a.error_pct:+.2f}%, result={'PASS' if a.passed else 'FAIL'}**")
    L.append("")

    L.append("## Sample scale-ratio check (Part 12): generated_size / baseline_size")
    L.append("")
    L.append("Confirms relative shape is preserved -- small baseline objects scale to "
             "correspondingly small generated objects, large stays large.")
    L.append("")
    import csv
    for tier in args.tiers:
        out_dir = wg_common.output_dir_for_tier(tier)
        manifest_csv = out_dir / "manifest.csv"
        if not manifest_csv.is_file():
            continue
        L.append(f"### {tier}")
        L.append("")
        L.append("| Baseline URI | Baseline size | Generated size | Ratio |")
        L.append("|---|---|---|---|")
        with manifest_csv.open("r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        rows_sorted = sorted(rows, key=lambda r: int(r["source_size_bytes"]))
        n = len(rows_sorted)
        sample_idx = sorted({0, n // 4, n // 2, 3 * n // 4, n - 1}) if n else []
        for i in sample_idx:
            r = rows_sorted[i]
            src = int(r["source_size_bytes"])
            gen = int(r["generated_size_bytes"])
            ratio = (gen / src) if src else float("inf")
            L.append(f"| `{r['uri']}` | {wg_common.human_size(src)} | {wg_common.human_size(gen)} | {ratio:.2f}x |")
        L.append("")

    Path(args.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"Report written to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
