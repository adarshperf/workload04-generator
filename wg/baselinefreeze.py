"""Golden-baseline discovery, freezing, and loading.

`init-baseline` is the one-time command that reads the ORIGINAL Workload04
resource list + physical corpus, computes full statistics, verifies them
against the known expected facts (10,000 entries / 3 EICAR entries / 0.03%),
and -- only after explicit acceptance if reality disagrees -- freezes an
immutable manifest that every later `--avg-size` generation reads from.

The original Workload04 resource list and corpus are never written to by
this module or anything downstream of it.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import common as wg_common
from .common import wmime_baseline, GENERATOR_VERSION

EXPECTED_TOTAL_ENTRIES = 10000
EXPECTED_EICAR_COUNT = 3
EXPECTED_EICAR_PCT = EXPECTED_EICAR_COUNT / EXPECTED_TOTAL_ENTRIES  # 0.03%


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_raw_ordered_uris(resource_list: Path) -> List[str]:
    """Full original line sequence (URIs only, in file order, WITH repeats),
    skipping blank/comment lines -- this is what lets generation reproduce
    the exact original interleaving/ordering, which the deduplicated
    per-URI structure in wmime.baseline.parse_resource_list intentionally
    discards (it keeps only first-occurrence order + a weight count)."""
    uris = []
    with resource_list.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            uri = line
            if line[0] == '"':
                close = line.find('"', 1)
                if close != -1:
                    uri = line[1:close]
            elif "\t" in line:
                uri = line.split("\t", 1)[0]
            uris.append(uri)
    return uris


class DiscrepancyError(Exception):
    def __init__(self, message: str, discrepancies: list):
        super().__init__(message)
        self.discrepancies = discrepancies


def analyze(resource_list: Path, source_root: Path):
    """Parse + compute full statistics. Never writes anything."""
    stats = wmime_baseline.compute_baseline(resource_list, source_root)
    raw_order = read_raw_ordered_uris(resource_list)
    return stats, raw_order


def count_physical_files_on_disk(source_root: Path, *, exclude=None) -> int:
    """Recursive `find . -type f | wc -l`-equivalent count of the real
    physical payload directory -- the hard fact the spec requires be
    verified from disk, independent of whether the resource list's own
    entries happen to resolve against it.

    `exclude`: absolute paths to skip -- specifically, a resource-list file
    that happens to live directly inside `source_root` (the bundled-workload
    layout convention: root/<label>-resources.txt next to root/<label>/...)
    is METADATA, not a payload file, and must never inflate this count.
    """
    exclude_resolved = {Path(e).resolve() for e in (exclude or ())}
    count = 0
    for p in source_root.rglob("*"):
        if p.is_file() and p.resolve() not in exclude_resolved:
            count += 1
    return count


def generic_eicar_count(stats) -> int:
    """Content-based EICAR count -- SourceRecord.eicar_detected scans
    actual file bytes (including inside zip members), so this is valid for
    ANY corpus, not just the known Workload04 AV_TEST_URIS list. Used as
    the authoritative EICAR count for every workload; the fixed-URI
    cross-check in check_discrepancies() is an ADDITIONAL, stricter check
    applied only when enforce_known_invariants=True (i.e. label=="workload04")."""
    return sum(r.weight for r in stats.records if r.eicar_detected)


def compute_corpus_fingerprint(raw_order: List[str], physical_file_count_on_disk: int,
                                total_corpus_bytes: int) -> str:
    """Deterministic fingerprint of a golden corpus: depends on the exact
    resource-list order/content plus on-disk physical file count and total
    bytes. Used to detect a stale/mismatched /opt/<workload> without
    depending on any single file's checksum."""
    h = hashlib.sha256()
    h.update(f"{physical_file_count_on_disk}:{total_corpus_bytes}:{len(raw_order)}\n".encode("utf-8"))
    for uri in raw_order:
        h.update(uri.encode("utf-8", errors="replace"))
        h.update(b"\n")
    return h.hexdigest()


def check_discrepancies(stats, physical_file_count_on_disk: int = None, *,
                         enforce_known_invariants: bool = True) -> list:
    """Compare computed facts against the known/expected baseline facts.
    Returns a list of human-readable discrepancy strings (empty = matches).

    `enforce_known_invariants` gates the Workload04-SPECIFIC hard numeric
    checks (10,000 entries, 3 EICAR @ 0.03%, the fixed AV_TEST_URIS list).
    Only the default/bundled "workload04" label enforces these exactly, per
    spec: future/user-supplied workloads (Workload18, Workload24, ...) are
    measured and preserved as-is, never forced to match Workload04's
    specific numbers. The generic missing-resource-list-entry check always
    applies (any corpus benefits from knowing its resource list resolves)."""
    discrepancies = []
    if enforce_known_invariants:
        if stats.total_entries != EXPECTED_TOTAL_ENTRIES:
            discrepancies.append(
                f"total logical request entries: expected {EXPECTED_TOTAL_ENTRIES}, "
                f"actual {stats.total_entries}"
            )
        if physical_file_count_on_disk is not None and physical_file_count_on_disk != EXPECTED_TOTAL_ENTRIES:
            discrepancies.append(
                f"physical payload files on disk (recursive count under source_root): "
                f"expected {EXPECTED_TOTAL_ENTRIES}, actual {physical_file_count_on_disk}"
            )
    if stats.missing_files:
        discrepancies.append(
            f"{stats.missing_files} of {stats.total_entries} resource-list entries do NOT resolve "
            f"to an existing file under source_root (stale/broken references in the resource list "
            f"itself -- distinct from the physical-file-count-on-disk check above; a corpus can have "
            f"exactly 10,000 physical files on disk while some resource-list lines still point at "
            f"the WRONG one of them)."
        )
    if enforce_known_invariants:
        eicar_count = sum(r.weight for r in stats.records if r.uri in wg_common.AV_TEST_URIS)
        eicar_pct = eicar_count / stats.total_entries if stats.total_entries else 0.0
        if eicar_count != EXPECTED_EICAR_COUNT:
            discrepancies.append(
                f"EICAR/AV-test entries: expected {EXPECTED_EICAR_COUNT}, actual {eicar_count}"
            )
        if abs(eicar_pct - EXPECTED_EICAR_PCT) > 1e-6:
            discrepancies.append(
                f"EICAR/AV-test frequency: expected {EXPECTED_EICAR_PCT * 100:.4f}%, "
                f"actual {eicar_pct * 100:.4f}%"
            )
        missing_av = [uri for uri in wg_common.AV_TEST_URIS
                      if not any(r.uri == uri and r.exists and r.valid for r in stats.records)]
        if missing_av:
            discrepancies.append(
                "mandatory EICAR/AV-test URIs not resolvable on disk: " + ", ".join(missing_av)
            )
    return discrepancies


def freeze(resource_list: Path, source_root: Path, out_dir: Path, *,
           label: str = None, accept_discrepancies: bool = False) -> dict:
    """Compute, verify, and (if accepted) write the frozen baseline manifest.

    `label` identifies the source workload ("workload04" by default, or a
    sanitized name derived from --workload-root for any other corpus --
    see wg_common.get_workload_label()/sanitize_workload_label()). Only
    "workload04" enforces the known exact invariants (10,000/3/0.03%); any
    other label's actual measured facts are recorded as-is.

    Raises DiscrepancyError if computed facts disagree with the expected
    facts and `accept_discrepancies` is False -- caller decides how to
    surface this (interactive prompt, CLI flag, etc.) before retrying with
    accept_discrepancies=True. For non-"workload04" labels, callers should
    normally pass accept_discrepancies=True outright (see
    workload04_generator.py's auto-freeze path for --workload-root),
    since Workload04-specific invariants are expected to not apply.
    """
    label = label or wg_common.DEFAULT_WORKLOAD_LABEL
    enforce_known_invariants = (label == wg_common.DEFAULT_WORKLOAD_LABEL)

    stats, raw_order = analyze(resource_list, source_root)
    physical_file_count_on_disk = count_physical_files_on_disk(source_root, exclude={resource_list})
    discrepancies = check_discrepancies(
        stats, physical_file_count_on_disk, enforce_known_invariants=enforce_known_invariants)
    if discrepancies and not accept_discrepancies:
        raise DiscrepancyError(
            "Computed baseline facts differ from the expected Workload04 facts."
            if enforce_known_invariants else
            "Computed baseline facts have discrepancies (informational for a non-Workload04 workload).",
            discrepancies,
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    # Full per-URI record table (first-occurrence order, one row per unique URI).
    records_path = out_dir / "baseline_records.csv"
    with records_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["uri", "extension", "mime_type", "family", "size_bytes",
                    "weight", "exists", "valid", "eicar_detected"])
        for r in stats.records:
            w.writerow([r.uri, r.ext, r.mime, r.family, r.size_bytes, r.weight,
                        r.exists, r.valid, r.eicar_detected])

    # Full original line-by-line order (WITH repeats), needed to reproduce
    # the exact original interleaving during generation.
    order_path = out_dir / "baseline_resource_order.txt"
    order_path.write_text("\n".join(raw_order) + "\n", encoding="utf-8")

    eicar_count = generic_eicar_count(stats)
    fingerprint = compute_corpus_fingerprint(raw_order, physical_file_count_on_disk, stats.total_corpus_bytes)

    header = {
        "workload_label": label,
        "generator_tool_version": GENERATOR_VERSION,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "resource_list_path": str(resource_list.resolve()),
        "resource_list_sha256": _sha256_file(resource_list),
        "source_root_path": str(source_root.resolve()),
        "corpus_fingerprint": fingerprint,
        "total_entries": stats.total_entries,
        "unique_uris": stats.unique_uris,
        "missing_files": stats.missing_files,
        "invalid_files": stats.invalid_files,
        "physical_file_count_on_disk": physical_file_count_on_disk,
        "eicar_count": eicar_count,
        "eicar_pct": eicar_count / stats.total_entries if stats.total_entries else 0.0,
        "eicar_uris": list(wg_common.AV_TEST_URIS) if enforce_known_invariants else [],
        "total_corpus_bytes": stats.total_corpus_bytes,
        "request_weighted_average_bytes": stats.request_weighted_average_bytes,
        "median_bytes": stats.median_bytes,
        "p90": stats.p90, "p95": stats.p95, "p99": stats.p99,
        "min_bytes": stats.min_bytes, "max_bytes": stats.max_bytes,
        "extension_distribution": stats.extension_distribution,
        "mime_distribution": stats.mime_distribution,
        "discrepancies_at_freeze_time": discrepancies,
        "known_invariants_enforced": enforce_known_invariants,
        "records_file": "baseline_records.csv",
        "order_file": "baseline_resource_order.txt",
    }
    manifest_path = out_dir / "baseline_manifest.json"
    manifest_path.write_text(json.dumps(header, indent=2), encoding="utf-8")
    return header


class FrozenBaseline:
    """Loaded view of a previously-frozen baseline manifest, used by `generate`."""

    def __init__(self, out_dir: Path):
        manifest_path = out_dir / "baseline_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"No frozen baseline found at {out_dir}. Run "
                f"'workload04_generator.py init-baseline' first."
            )
        self.dir = out_dir
        self.header = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.source_root = Path(self.header["source_root_path"])
        self.records = self._load_records(out_dir / self.header["records_file"])
        self.raw_order = (out_dir / self.header["order_file"]).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()

    @staticmethod
    def _load_records(path: Path) -> dict:
        """uri -> record dict, keyed for O(1) lookup during generation."""
        out = {}
        with path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row["size_bytes"] = int(row["size_bytes"])
                row["weight"] = int(row["weight"])
                row["exists"] = row["exists"] == "True"
                row["valid"] = row["valid"] == "True"
                row["eicar_detected"] = row["eicar_detected"] == "True"
                out[row["uri"]] = row
        return out

    @property
    def total_entries(self) -> int:
        return self.header["total_entries"]

    @property
    def label(self) -> str:
        """Source workload label ("workload04", "workload18", ...).
        Falls back to the default for baselines frozen before this field
        existed."""
        return self.header.get("workload_label", wg_common.DEFAULT_WORKLOAD_LABEL)

    @property
    def known_invariants_enforced(self) -> bool:
        """Whether this baseline's Workload04-specific invariants
        (10,000 entries, 3 EICAR, 0.03%) were enforced at freeze time.
        Baselines frozen before this field existed default to True --
        that was the only behavior the tool had at the time."""
        return self.header.get("known_invariants_enforced", True)

    def source_root_reachable(self) -> bool:
        """Whether the ORIGINAL corpus (`source_root_path` recorded at
        freeze time) is still reachable from THIS machine. False right
        after copying a frozen baseline to a new machine/checkout without
        re-running init-baseline there yet -- generating in that state
        would silently degrade every record to non-representative content
        (see workload04_generator.py cmd_generate's reachability check)."""
        try:
            return self.source_root.is_dir()
        except OSError:
            return False

    @property
    def eicar_count(self) -> int:
        return self.header["eicar_count"]
