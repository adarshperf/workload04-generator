"""Strict 1:1 physical-resource mapping (2026-09-19 rework).

GOLDEN PRINCIPLE (per spec): every baseline physical resource gets its own
generated physical resource. No bounded pool, no per-class collapsing, no
"representative file per MIME class" shortcut. This module replaces the
earlier bounded-diversity-pool design, which the spec explicitly rejects
(the 21-file and 258-file shortcuts).

The real Workload04 baseline (verified via init-baseline / FrozenBaseline)
has exactly one weight-1 record per unique URI -- no duplicate lines -- so
"1 baseline physical file -> 1 generated physical file, in the same relative
position, with the same logical request-list line count" falls out directly:
every baseline record becomes exactly one PlannedRecord, generation writes
exactly one physical file per PlannedRecord, and the output resource list is
simply the original resource list with its root path segment rewritten.

Per-record target size is PROPORTIONAL to that record's own original size
(scaled by a single global factor, refined by the retry loop in
generate.generate_workload), so the relative small/medium/large/long-tail
shape of the baseline is preserved automatically -- not just the mean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from . import common as wg_common


@dataclass
class PlannedRecord:
    uri: str
    extension: str
    family: str
    mime: str
    is_av_test: bool
    has_source_seed: bool          # False if this baseline URI never resolved to a real file
    source_size_bytes: int          # real seed size if has_source_seed, else a synthetic base
    target_size_bytes: int


@dataclass
class WorkloadPlan:
    target_bytes: int
    seed: int
    records: List[PlannedRecord] = field(default_factory=list)
    scale_factor: float = 1.0
    max_single_file_bytes: int = wg_common.DEFAULT_MAX_SINGLE_FILE_BYTES
    missing_seed_count: int = 0

    @property
    def total_physical_files(self) -> int:
        return len(self.records)

    @property
    def estimated_disk_bytes(self) -> int:
        return sum(r.target_size_bytes for r in self.records)


def _clamped_target(base_size: int, scale_factor: float, is_av_test: bool,
                     max_single_file_bytes: int) -> int:
    target = max(1, round(base_size * scale_factor))
    if is_av_test:
        # AV-test resources only ever GROW -- never shrink below their real
        # seed size, so the EICAR/malware signature can never be truncated
        # away by a downscaling target (e.g. a 100KiB-average run must not
        # shrink crypt-ssleay-infected.zip below its own real byte size).
        target = max(target, base_size)
    return min(target, max_single_file_bytes)


def build_plan(baseline, target_bytes: int, seed: int, *,
               max_single_file_bytes: int = wg_common.DEFAULT_MAX_SINGLE_FILE_BYTES) -> WorkloadPlan:
    """Build the strict 1:1 plan: exactly one PlannedRecord per baseline
    record, in baseline.raw_order's original sequence (deduplicated by first
    occurrence -- the real baseline has no duplicate URIs, but this stays
    correct even if a future baseline does)."""
    plan = WorkloadPlan(target_bytes=target_bytes, seed=seed, max_single_file_bytes=max_single_file_bytes)

    global_avg = baseline.header["request_weighted_average_bytes"] or 1.0
    plan.scale_factor = (target_bytes / global_avg) if global_avg else 1.0

    seen = set()
    for uri in baseline.raw_order:
        if uri in seen:
            continue
        seen.add(uri)
        rec = baseline.records.get(uri)
        if rec is None:
            continue  # should not happen -- every raw_order URI has a record row
        is_av_test = uri in wg_common.AV_TEST_URIS
        has_seed = bool(rec["exists"]) and bool(rec["valid"]) and rec["size_bytes"] > 0
        # Records with no resolvable seed on disk (see baseline discrepancy
        # report) have no real byte content to scale from -- use the global
        # baseline average as a neutral synthetic base instead of 0, which
        # would otherwise floor their generated size at ~1 byte forever.
        base_size = rec["size_bytes"] if has_seed else max(256, int(global_avg))
        target = _clamped_target(base_size, plan.scale_factor, is_av_test, max_single_file_bytes)
        plan.records.append(PlannedRecord(
            uri=uri, extension=rec["extension"], family=wg_common.classify_family(rec["extension"]),
            mime=wg_common.guess_mime(rec["extension"]), is_av_test=is_av_test,
            has_source_seed=has_seed, source_size_bytes=base_size, target_size_bytes=target,
        ))
        if not has_seed:
            plan.missing_seed_count += 1

    return plan


def rescale_plan_sizes(plan: WorkloadPlan, factor: float) -> None:
    """Adjust every record's per-file target size by a uniform factor
    in-place (retry/convergence loop) -- never touches which records exist,
    only bytes-per-file, so the 1:1 physical-file/logical-entry counts are
    completely unaffected by retries."""
    plan.scale_factor *= factor
    for r in plan.records:
        r.target_size_bytes = _clamped_target(
            r.source_size_bytes, plan.scale_factor, r.is_av_test, plan.max_single_file_bytes,
        )


# Families whose format generators (see wmime/formats.py) never shrink a
# real seed below its own byte size -- doing so would either destroy the
# format's required structure (HTML/CSS/JS/XML: mid-tag truncation; PDF/PE:
# broken headers/xrefs/checksums) or (ZIP) requires dropping real archive
# members, which the generator refuses to do silently. Used to compute a
# PROVABLE LOWER BOUND on the achievable request-weighted average for a
# plan -- NOT the exact achievable minimum: other families (jpeg/gif/png/
# wav) have their own content-dependent re-encode/resize floors that are
# not captured here, so the true minimum may be higher than this bound.
HARD_NO_SHRINK_FAMILIES = frozenset({"html", "css", "js", "xml", "pdf", "pe", "zip"})


def compute_hard_floor(plan: WorkloadPlan) -> dict:
    """Lower bound on the achievable request-weighted average for `plan`.

    Records with no resolvable source seed are excluded: an empty seed
    pads normally regardless of family (see wmime/formats.py), so they
    impose no such floor -- only real, already-oversized seeds do.

    Returns a dict: floor_bytes, floor_avg_bytes (the value to compare a
    requested average against), record_count, total_records,
    per_family_bytes, per_family_count.
    """
    per_family_bytes = {}
    per_family_count = {}
    floor_bytes = 0
    floor_count = 0
    for r in plan.records:
        if r.has_source_seed and r.family in HARD_NO_SHRINK_FAMILIES:
            per_family_bytes[r.family] = per_family_bytes.get(r.family, 0) + r.source_size_bytes
            per_family_count[r.family] = per_family_count.get(r.family, 0) + 1
            floor_bytes += r.source_size_bytes
            floor_count += 1
    total_records = len(plan.records) or 1
    return {
        "floor_bytes": floor_bytes,
        "floor_avg_bytes": floor_bytes / total_records,
        "record_count": floor_count,
        "total_records": total_records,
        "per_family_bytes": per_family_bytes,
        "per_family_count": per_family_count,
    }

