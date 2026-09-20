"""analyze_missing_baseline_entries.py -- READ-ONLY diagnostic.

Investigates the 219 baseline resource-list entries that do not resolve to
a physical file. Writes nothing to the baseline, the resource list, or any
generated workload -- only reads and prints/writes a report file.

Usage:
    python analyze_missing_baseline_entries.py [--out missing_entries_report.md]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from wg import common as wg_common
from wg import baselinefreeze


def _rel_from_uri(uri: str) -> str:
    parts = uri.lstrip("/").split("/", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(TOOL_ROOT / "missing_entries_report.md"))
    ap.add_argument("--baseline-dir", default=str(wg_common.DEFAULT_BASELINE_DIR))
    args = ap.parse_args()

    baseline = baselinefreeze.FrozenBaseline(Path(args.baseline_dir))
    source_root = baseline.source_root  # external golden corpus root, e.g. /opt/workload04

    # --- 1. Load every baseline record, in ORIGINAL resource-list order ---
    position_of = {uri: i for i, uri in enumerate(baseline.raw_order)}
    missing = []
    for uri, rec in baseline.records.items():
        if not rec["exists"]:
            missing.append(rec)
    missing.sort(key=lambda r: position_of.get(r["uri"], -1))
    if not missing:
        print("No missing (exists=False) baseline records found -- nothing to analyze.")
        print(f"Baseline is clean: {len(baseline.records)} records, all resolve.")
        return 0

    # --- Enumerate ALL physical files under the real payload root ----------
    # real files live one level deeper at source_root/workload04/... (the
    # doubled-directory convention -- resolved_path = source_root /
    # uri.lstrip('/'), and every uri starts with '/workload04/...').
    payload_root = source_root / "workload04"
    all_physical_rel = set()
    physical_by_lower = defaultdict(list)      # lowercased relpath -> [relpath,...]
    physical_by_basename_lower = defaultdict(list)  # lowercased basename -> [relpath,...]
    for p in payload_root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(payload_root).as_posix()
            all_physical_rel.add(rel)
            physical_by_lower[rel.lower()].append(rel)
            physical_by_basename_lower[p.name.lower()].append(rel)

    # --- Referenced (successfully resolving) relpaths -----------------------
    referenced_rel = {
        _rel_from_uri(rec["uri"]) for rec in baseline.records.values() if rec["exists"]
    }
    orphans = sorted(all_physical_rel - referenced_rel)

    rows = []
    for rec in missing:
        uri = rec["uri"]
        rel = _rel_from_uri(uri)
        directory = str(Path(rel).parent).replace("\\", "/")
        if directory == ".":
            directory = "(root)"
        basename = Path(rel).name
        position = position_of.get(uri, -1)

        decoded_rel = urllib.parse.unquote(rel)
        decoded_exists = (payload_root / decoded_rel).is_file()
        has_percent = "%" in rel

        # Case-insensitive exact-path match, and decoded case-insensitive match.
        ci_match = physical_by_lower.get(rel.lower(), [])
        ci_decoded_match = physical_by_lower.get(decoded_rel.lower(), [])

        # Basename-only match anywhere in the tree (encoded and decoded forms).
        basename_matches = set(physical_by_basename_lower.get(basename.lower(), []))
        decoded_basename = Path(decoded_rel).name
        basename_matches |= set(physical_by_basename_lower.get(decoded_basename.lower(), []))
        basename_matches.discard(rel)

        rows.append({
            "position": position,
            "uri": uri,
            "rel": rel,
            "extension": rec["extension"],
            "mime": rec.get("mime_type", ""),
            "directory": directory,
            "has_percent_encoding": has_percent,
            "decoded_rel": decoded_rel if decoded_rel != rel else "",
            "decoded_form_exists_on_disk": decoded_exists,
            "case_insensitive_exact_match": ci_match,
            "case_insensitive_decoded_match": ci_decoded_match,
            "basename_matches_elsewhere": sorted(basename_matches),
        })

    # --- Cross-check against the already-generated 100KiB tier -------------
    tier_dir = wg_common.output_dir_for_tier("100KiB")
    gen_synthetic_uris = set()
    gen_check_available = False
    if (tier_dir / "manifest.csv").is_file():
        gen_check_available = True
        with (tier_dir / "manifest.csv").open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["has_source_seed"] == "False":
                    gen_synthetic_uris.add(row["uri"])

    missing_uris = {r["uri"] for r in missing}
    synthetic_matches_missing = gen_synthetic_uris == missing_uris

    # --- Classification ------------------------------------------------------
    decoded_would_fix = sum(1 for r in rows if r["decoded_form_exists_on_disk"])
    ci_would_fix = sum(1 for r in rows if r["case_insensitive_exact_match"] or r["case_insensitive_decoded_match"])
    basename_elsewhere = sum(1 for r in rows if r["basename_matches_elsewhere"])
    orphan_count = len(orphans)

    L = []
    L.append("# Missing Baseline Entries -- Read-Only Analysis")
    L.append("")
    L.append(f"Baseline: `{baseline.header['resource_list_path']}`")
    L.append(f"Source root (payload files): `{payload_root}`")
    L.append(f"Total missing (exists=False) entries: **{len(missing)}**")
    L.append(f"Total physical files on disk (payload_root, recursive): **{len(all_physical_rel)}**")
    L.append(f"Total orphan physical files (on disk, not referenced by any resolving entry): **{orphan_count}**")
    L.append("")
    L.append("## Summary counts")
    L.append("")
    L.append(f"- Entries whose percent-DECODED path exists on disk: **{decoded_would_fix}** / 219")
    L.append(f"- Entries with a case-insensitive path match (encoded or decoded): **{ci_would_fix}** / 219")
    L.append(f"- Entries whose basename (encoded or decoded) exists ELSEWHERE in the tree: **{basename_elsewhere}** / 219")
    L.append(f"- Orphan physical files on disk (unreferenced by the 9,781 resolving entries): **{orphan_count}**")
    L.append(f"- 219 missing == {orphan_count} orphans? **{len(missing) == orphan_count}**")
    if gen_check_available:
        L.append(f"- Generated 100KiB tier's synthetic (has_source_seed=False) URIs == these 219 missing URIs exactly? "
                 f"**{synthetic_matches_missing}** ({len(gen_synthetic_uris)} synthetic rows found)")
    else:
        L.append("- Generated 100KiB tier manifest.csv not found -- skip cross-check.")
    L.append("")

    ext_counts = defaultdict(int)
    dir_counts = defaultdict(int)
    for r in rows:
        ext_counts[r["extension"] or "(none)"] += 1
        dir_counts[r["directory"]] += 1

    L.append("## By extension")
    L.append("")
    L.append("| Extension | Count |")
    L.append("|---|---|")
    for ext, c in sorted(ext_counts.items(), key=lambda kv: -kv[1]):
        L.append(f"| .{ext} | {c} |")
    L.append("")

    L.append("## By directory")
    L.append("")
    L.append("| Directory | Count |")
    L.append("|---|---|")
    for d, c in sorted(dir_counts.items(), key=lambda kv: -kv[1]):
        L.append(f"| {d} | {c} |")
    L.append("")

    L.append("## All 219 missing entries (original resource-list order)")
    L.append("")
    L.append("| # | Position | URI | MIME | Ext | Directory | %-encoded | Decoded exists? | Case-insensitive match | Basename found elsewhere |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        ci = r["case_insensitive_exact_match"] or r["case_insensitive_decoded_match"]
        ci_str = ", ".join((r["case_insensitive_exact_match"] or []) + (r["case_insensitive_decoded_match"] or [])) or "-"
        basename_str = "; ".join(r["basename_matches_elsewhere"][:3]) or "-"
        if len(r["basename_matches_elsewhere"]) > 3:
            basename_str += f" (+{len(r['basename_matches_elsewhere']) - 3} more)"
        L.append(
            f"| {i} | {r['position']} | `{r['uri']}` | {r['mime']} | {r['extension']} | {r['directory']} | "
            f"{'YES' if r['has_percent_encoding'] else 'no'} | "
            f"{'YES -> ' + r['decoded_rel'] if r['decoded_form_exists_on_disk'] else 'no'} | "
            f"{ci_str} | {basename_str} |"
        )
    L.append("")

    L.append("## Orphan physical files (on disk, unreferenced by any resolving resource-list entry)")
    L.append("")
    L.append(f"Total: {orphan_count}")
    L.append("")
    if orphans:
        L.append("| # | Relative path |")
        L.append("|---|---|")
        for i, o in enumerate(orphans[:250], 1):
            L.append(f"| {i} | `{o}` |")
        if len(orphans) > 250:
            L.append(f"| ... | ({len(orphans) - 250} more, truncated) |")
    L.append("")

    L.append("## Possible URI <-> orphan-file pairings (same directory + extension, position-adjacent)")
    L.append("")
    orphans_by_dir_ext = defaultdict(list)
    for o in orphans:
        d = str(Path(o).parent).replace("\\", "/")
        e = Path(o).suffix.lstrip(".").lower()
        orphans_by_dir_ext[(d, e)].append(o)
    pairing_rows = []
    for r in rows:
        d = r["directory"] if r["directory"] != "(root)" else "."
        e = r["extension"].lower()
        candidates = orphans_by_dir_ext.get((d, e), [])
        pairing_rows.append((r["uri"], candidates))
    paired = sum(1 for _, c in pairing_rows if len(c) == 1)
    L.append(f"Missing entries with EXACTLY ONE same-directory+same-extension orphan candidate: {paired} / 219")
    L.append("")
    L.append("| Missing URI | Same-dir+ext orphan candidate(s) |")
    L.append("|---|---|")
    for uri, candidates in pairing_rows:
        cand_str = "; ".join(f"`{c}`" for c in candidates[:5]) or "(none)"
        if len(candidates) > 5:
            cand_str += f" (+{len(candidates) - 5} more)"
        L.append(f"| `{uri}` | {cand_str} |")
    L.append("")

    L.append("## Conclusion")
    L.append("")
    if orphan_count == len(missing) and decoded_would_fix == 0 and ci_would_fix == 0:
        conclusion = (
            "**(C) Stale resource-list entries with unrelated orphan files.** The count of "
            "unreferenced physical files on disk exactly equals the count of unresolvable "
            "resource-list entries, but percent-decoding and case-insensitive matching do not "
            "explain the mismatch -- these look like resource-list URIs that were renamed/"
            "reshuffled on disk at some point without updating the resource list, or vice versa."
        )
    elif decoded_would_fix > 0 or ci_would_fix > 0:
        conclusion = (
            "**(B) Path/encoding resolution errors in the generator/resolver, at least partially.** "
            f"{decoded_would_fix} of the 219 entries resolve successfully once percent-decoded, and "
            f"{ci_would_fix} resolve with a case-insensitive match -- the resolver "
            "(`wmime.baseline.parse_resource_list`) does a literal, case-sensitive, non-decoded "
            "path join (`source_root / uri.lstrip('/')`), so these are false 'missing' results, "
            "not genuinely absent content."
        )
    else:
        conclusion = (
            "**(A) Genuinely missing source resources** -- no decoded, case-insensitive, or "
            "basename-elsewhere match was found for these entries anywhere in the physical corpus."
        )
    L.append(conclusion)
    L.append("")
    if gen_check_available:
        L.append(f"Generated 100KiB tier's 219 synthetic-content rows match these 219 missing URIs exactly: "
                 f"**{synthetic_matches_missing}**.")

    report_text = "\n".join(L) + "\n"
    Path(args.out).write_text(report_text, encoding="utf-8")
    print(f"Report written to: {args.out}")
    print()
    print(f"missing={len(missing)} orphans={orphan_count} decoded_fix={decoded_would_fix} "
          f"case_insensitive_fix={ci_would_fix} basename_elsewhere={basename_elsewhere}")
    if gen_check_available:
        print(f"synthetic_matches_missing_exactly={synthetic_matches_missing}")
    print()
    print(conclusion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
