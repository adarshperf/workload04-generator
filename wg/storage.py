"""Storage pre-flight checks (spec sections 9-13/15/16).

Real, filesystem-backed capacity/inode checks -- never a rough
``10,000 * average_size`` guess. Every generation (and every ``--dry-run``
and ``max-avg-size`` invocation) goes through this module before any
payload file is written.

Byte-capacity uses ``shutil.disk_usage`` (stdlib, cross-platform: Windows
and POSIX). Inode/free-file-count checks use ``os.statvfs`` where the
platform provides it (Linux/POSIX only -- silently skipped, never fatal, on
platforms without it, e.g. Windows dev/test).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import common as wg_common

# Number of fixed metadata files written alongside payload files in every
# generated tier (see wg_common.METADATA_FILENAMES) -- included in the
# required-inode estimate so a filesystem with barely enough inodes for
# 10,000 payload files doesn't fail with a confusing mid-generation error.
_METADATA_FILE_COUNT = len(wg_common.METADATA_FILENAMES)

# Extra safety headroom (files + bytes) beyond the exact estimate, on top of
# DEFAULT_DISK_SAFETY_MARGIN, to absorb directory-entry overhead and the
# staging directory used during atomic generation (spec section 14) briefly
# existing alongside its future final location on the same filesystem.
_INODE_SAFETY_COUNT = 64


@dataclass
class FilesystemInfo:
    path: Path                  # the (possibly-not-yet-existing) target path this describes
    checked_path: Path          # nearest existing ancestor actually stat'd
    total_bytes: int
    free_bytes: int
    available_bytes: int        # free bytes usable by a non-privileged user (POSIX: f_bavail)
    block_size: Optional[int] = None      # filesystem allocation unit, if known (POSIX f_frsize)
    total_inodes: Optional[int] = None
    free_inodes: Optional[int] = None
    available_inodes: Optional[int] = None  # inodes usable by a non-privileged user


def _nearest_existing_ancestor(path: Path) -> Path:
    p = path
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return p


def check_output_root_writable(output_root: Path, *, create: bool = True) -> Optional[str]:
    """Verify the output root can actually be created/written to, BEFORE
    any pre-flight or generation work starts (spec section 9). Returns
    None if writable, otherwise a human-readable error message including
    remediation commands.

    `create=False` (used for `--dry-run`, which must write NOTHING) skips
    both `mkdir` and the write-probe file, instead checking whether the
    nearest EXISTING ancestor directory looks writable -- non-mutating,
    never creates the output root merely to answer a feasibility question.
    """
    if not create:
        ancestor = _nearest_existing_ancestor(output_root)
        if not os.access(str(ancestor), os.W_OK):
            return (
                f"Output root {output_root} does not appear writable (nearest existing "
                f"ancestor {ancestor} is not writable by the current user).\n"
                f"If this is a permissions issue, ask an administrator to run:\n"
                f"    sudo mkdir -p {output_root}\n"
                f"    sudo chown -R $(id -un):$(id -gn) {output_root}\n"
                f"then re-run this command."
            )
        return None
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return (
            f"Cannot create output root {output_root}: {e}\n"
            f"If this is a permissions issue, ask an administrator to run:\n"
            f"    sudo mkdir -p {output_root}\n"
            f"    sudo chown -R $(id -un):$(id -gn) {output_root}\n"
            f"then re-run this command."
        )
    probe = output_root / f".workload04_generator_write_test_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return (
            f"Output root {output_root} exists but is not writable by the current user: {e}\n"
            f"Fix ownership/permissions, e.g.:\n"
            f"    sudo chown -R $(id -un):$(id -gn) {output_root}\n"
            f"then re-run this command."
        )
    return None


def get_filesystem_info(path: Path) -> FilesystemInfo:
    """Real filesystem capacity for the device that will hold `path`, using
    the nearest existing ancestor directory if `path` itself doesn't exist
    yet (the common case at dry-run/pre-flight time)."""
    checked = _nearest_existing_ancestor(Path(path))
    usage = shutil.disk_usage(str(checked))

    block_size = None
    total_inodes = free_inodes = available_inodes = None
    statvfs = getattr(os, "statvfs", None)
    if statvfs is not None:
        try:
            vfs = statvfs(str(checked))
            block_size = vfs.f_frsize or vfs.f_bsize
            total_inodes = vfs.f_files
            free_inodes = vfs.f_ffree
            available_inodes = vfs.f_favail
        except OSError:
            pass

    return FilesystemInfo(
        path=Path(path), checked_path=checked,
        total_bytes=usage.total, free_bytes=usage.free, available_bytes=usage.free,
        block_size=block_size,
        total_inodes=total_inodes, free_inodes=free_inodes, available_inodes=available_inodes,
    )


def _round_up_to_block(n: int, block_size: Optional[int]) -> int:
    if not block_size:
        return n
    if n <= 0:
        return block_size
    return ((n + block_size - 1) // block_size) * block_size


def estimate_allocated_bytes(plan, block_size: Optional[int]) -> int:
    """Sum of each planned file's size rounded UP to the filesystem block
    size (allocation unit), not just the raw byte sum -- real filesystems
    allocate whole blocks per file, and with 10,000 small files the
    rounding overhead is not negligible (e.g. a 271-byte file on a 4096-byte
    block filesystem still consumes a full 4096-byte block)."""
    if not block_size:
        return plan.estimated_disk_bytes
    return sum(_round_up_to_block(r.target_size_bytes, block_size) for r in plan.records)


@dataclass
class PreflightResult:
    tier: str
    out_dir: Path
    requested_avg_text: str
    requested_avg_bytes: int
    physical_files: int
    logical_requests: int
    estimated_payload_bytes: int
    estimated_allocated_bytes: int
    safety_margin_pct: float
    required_bytes: int
    coexisting_old_tier_bytes: int
    filesystem: FilesystemInfo
    required_inodes: int
    inode_check_available: bool
    feasible_bytes: bool
    feasible_inodes: bool
    feasible_content: bool
    content_floor_avg_bytes: float
    content_floor_record_count: int
    content_floor_families: dict
    max_safe_avg_bytes: Optional[int]
    max_theoretical_avg_bytes: Optional[int]
    label: str = None
    notes: list = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        # Disk-space, inode, and content-size feasibility are independent
        # checks -- a request can pass any two of these and still fail the
        # third (e.g. plenty of disk space but a content-size floor that
        # the requested average cannot meet).
        return self.feasible_bytes and self.feasible_inodes and self.feasible_content


def _existing_tier_bytes(out_dir: Path) -> int:
    """Size of an already-generated tier directory, if present -- needed so
    a --force regeneration's pre-flight correctly accounts for the OLD tier
    and the NEW (staged) tier briefly coexisting on disk (spec section 14),
    instead of assuming the old one will already be gone."""
    if not out_dir.is_dir():
        return 0
    total = 0
    for p in out_dir.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def run_preflight(plan, *, tier: str, requested_avg_text: str, out_dir: Path,
                   safety_margin: float = None, force_coexistence: bool = True,
                   label: str = None) -> PreflightResult:
    """The authoritative storage pre-flight, shared by --dry-run, real
    generation, and the pre-flight summary printed before generation
    starts. Uses the SAME plan/estimate the actual generation will use --
    never a separate, potentially-diverging approximation."""
    if safety_margin is None:
        safety_margin = wg_common.DEFAULT_DISK_SAFETY_MARGIN
    if label is None:
        label = wg_common.get_workload_label()

    fs = get_filesystem_info(out_dir)
    allocated = estimate_allocated_bytes(plan, fs.block_size)

    coexisting = _existing_tier_bytes(out_dir) if force_coexistence else 0
    required = int(allocated * (1 + safety_margin)) + coexisting

    feasible_bytes = fs.available_bytes >= required

    required_inodes = plan.total_physical_files + _METADATA_FILE_COUNT + _INODE_SAFETY_COUNT
    inode_check_available = fs.available_inodes is not None
    feasible_inodes = True
    if inode_check_available:
        feasible_inodes = fs.available_inodes >= required_inodes

    # Content-size feasibility is INDEPENDENT of disk-space/inode
    # feasibility: a request can have all the disk space in the world and
    # still be impossible to honor, because some formats refuse to shrink
    # below their real source size (see wg_pool.HARD_NO_SHRINK_FAMILIES).
    from . import pool as wg_pool  # local import: avoid a cycle at module load time
    floor = wg_pool.compute_hard_floor(plan)
    feasible_content = plan.target_bytes >= floor["floor_avg_bytes"]

    notes = []
    if coexisting:
        notes.append(
            f"An existing tier directory ({wg_common.human_size(coexisting)}) will temporarily "
            f"coexist with the newly-staged tier during --force regeneration; accounted for above."
        )
    if not inode_check_available:
        notes.append(
            "Free-inode count is not available on this platform/filesystem; inode pre-flight was "
            "skipped (byte-capacity pre-flight above is still authoritative)."
        )
    if not feasible_content:
        families = ", ".join(sorted(floor["per_family_count"])) or "none"
        notes.append(
            f"Requested average ({wg_common.human_size(plan.target_bytes)}) is below a provable "
            f"content-size LOWER BOUND of {wg_common.human_size(floor['floor_avg_bytes'])} for this "
            f"corpus: {floor['record_count']:,} of {floor['total_records']:,} records use formats "
            f"({families}) that never shrink below their real source size, by design, to avoid "
            f"producing structurally invalid files (see wmime/formats.py). This is a LOWER BOUND, "
            f"not necessarily the exact achievable minimum -- other formats' own shrink limits "
            f"(e.g. jpeg/gif/png re-encoding floors) may raise the true minimum further. Increase "
            f"--avg-size to at least this lower bound."
        )

    max_safe = max_theoretical = None
    if plan.total_physical_files:
        max_theoretical = fs.available_bytes // plan.total_physical_files
        max_safe = int(fs.available_bytes / (1 + safety_margin)) // plan.total_physical_files

    return PreflightResult(
        tier=tier, out_dir=out_dir,
        requested_avg_text=requested_avg_text, requested_avg_bytes=plan.target_bytes,
        physical_files=plan.total_physical_files, logical_requests=plan.total_physical_files,
        estimated_payload_bytes=plan.estimated_disk_bytes, estimated_allocated_bytes=allocated,
        safety_margin_pct=safety_margin, required_bytes=required,
        coexisting_old_tier_bytes=coexisting, filesystem=fs,
        required_inodes=required_inodes, inode_check_available=inode_check_available,
        feasible_bytes=feasible_bytes, feasible_inodes=feasible_inodes,
        feasible_content=feasible_content,
        content_floor_avg_bytes=floor["floor_avg_bytes"],
        content_floor_record_count=floor["record_count"],
        content_floor_families=dict(floor["per_family_count"]),
        max_safe_avg_bytes=max_safe, max_theoretical_avg_bytes=max_theoretical,
        label=label, notes=notes,
    )


def render_preflight_report(r: PreflightResult) -> str:
    L = []
    L.append("=" * 50)
    L.append(f"{(r.label or wg_common.get_workload_label()).capitalize()} Generation Pre-Flight")
    L.append("=" * 50)
    L.append(f"Requested average object size : {r.requested_avg_text}")
    L.append(f"Physical payload files         : {r.physical_files:,}")
    L.append(f"Logical resource entries       : {r.logical_requests:,}")
    L.append(f"Estimated payload bytes        : {wg_common.human_size(r.estimated_payload_bytes)}")
    L.append(f"Estimated filesystem usage     : {wg_common.human_size(r.estimated_allocated_bytes)}"
              + (f" (block size {r.filesystem.block_size}B)" if r.filesystem.block_size else ""))
    if r.coexisting_old_tier_bytes:
        L.append(f"Existing tier to be replaced   : {wg_common.human_size(r.coexisting_old_tier_bytes)} "
                  f"(temporarily coexists during --force)")
    L.append(f"Safety margin                  : {r.safety_margin_pct * 100:.0f}%")
    L.append(f"Required (with safety margin)  : {wg_common.human_size(r.required_bytes)}")
    L.append(f"Available free space           : {wg_common.human_size(r.filesystem.available_bytes)}")
    if r.max_safe_avg_bytes is not None:
        L.append(f"Maximum safe avg object size   : {wg_common.human_size(r.max_safe_avg_bytes)}")
        L.append(f"Maximum theoretical avg size   : {wg_common.human_size(r.max_theoretical_avg_bytes)}")
    if r.content_floor_record_count:
        L.append(f"Content-size lower bound (fmt) : {wg_common.human_size(r.content_floor_avg_bytes)} "
                  f"(from {r.content_floor_record_count:,} records: "
                  f"{', '.join(sorted(r.content_floor_families))})")
    if r.inode_check_available:
        L.append(f"Required inodes (est.)         : {r.required_inodes:,}")
        L.append(f"Available inodes               : {r.filesystem.available_inodes:,}")
    L.append(f"Output root                    : {r.out_dir.parent}")
    L.append(f"Tier directory                 : {r.out_dir}")
    for n in r.notes:
        L.append(f"Note: {n}")
    L.append("=" * 50)
    L.append(f"Disk-space feasibility         : {'PASS' if r.feasible_bytes else 'FAIL'}")
    L.append(f"Inode feasibility              : {'PASS' if r.feasible_inodes else 'FAIL'}")
    L.append(f"Content-size feasibility       : {'PASS' if r.feasible_content else 'FAIL'}")
    if r.feasible:
        L.append("PASS: sufficient storage and content-size feasibility")
    else:
        reasons = []
        if not r.feasible_bytes:
            reasons.append("insufficient free disk space")
        if not r.feasible_inodes:
            reasons.append("insufficient free inodes")
        if not r.feasible_content:
            reasons.append(
                f"requested average below format-preserving content-size lower bound "
                f"({wg_common.human_size(r.content_floor_avg_bytes)})"
            )
        L.append(f"FAIL: {', '.join(reasons)}")
        L.append("Generation aborted before payload creation.")
    return "\n".join(L) + "\n"


def compute_max_avg_size(baseline, out_dir: Path, *, seed: int = 42,
                          safety_margin: float = None,
                          max_single_file_bytes: int = None) -> dict:
    """Deterministically compute the maximum safe/theoretical average
    object size for THIS machine's actual free capacity, using the SAME
    scaling model (`wg.pool.build_plan` / `estimate_allocated_bytes`) real
    generation uses -- never an unrelated approximation. Because per-record
    target size is a linear function of the requested average (subject
    only to the fixed max-single-file-bytes cap and the AV-test
    never-shrink floor, both independent of scale for realistic targets),
    a single build_plan() at a probe size plus linear back-solving is exact
    for the byte budget; a short bisection refines around any nonlinearity
    introduced by per-file caps/floors."""
    from . import pool as wg_pool  # local import: avoid a cycle at module load time

    if safety_margin is None:
        safety_margin = wg_common.DEFAULT_DISK_SAFETY_MARGIN
    if max_single_file_bytes is None:
        max_single_file_bytes = wg_common.DEFAULT_MAX_SINGLE_FILE_BYTES

    fs = get_filesystem_info(out_dir)
    coexisting = _existing_tier_bytes(out_dir)
    usable_bytes = max(0, fs.available_bytes - coexisting)
    safe_budget = int(usable_bytes / (1 + safety_margin))

    n = max(1, baseline.total_entries)

    def allocated_bytes_for(target_bytes: int) -> int:
        probe_plan = wg_pool.build_plan(baseline, max(1, target_bytes), seed,
                                         max_single_file_bytes=max_single_file_bytes)
        return estimate_allocated_bytes(probe_plan, fs.block_size)

    # Bisection over target_bytes (monotonic non-decreasing in allocated
    # bytes) so the fixed per-file cap/AV-floor nonlinearities are handled
    # exactly, without assuming pure linearity.
    lo, hi = 1, max(1, safe_budget)
    # Grow hi until it overshoots the budget or hits the hard per-file cap
    # scaled by file count (an obvious, cheap upper bound).
    hi = max(hi, 1)
    while allocated_bytes_for(hi) < safe_budget and hi < max_single_file_bytes * n:
        hi *= 2
    best = 1
    for _ in range(40):
        if lo > hi:
            break
        mid = (lo + hi) // 2
        if mid <= 0:
            break
        if allocated_bytes_for(mid) <= safe_budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    max_theoretical = usable_bytes // n if n else 0

    return {
        "filesystem": fs,
        "physical_files": n,
        "available_bytes": fs.available_bytes,
        "coexisting_old_tier_bytes": coexisting,
        "usable_bytes": usable_bytes,
        "safety_margin_pct": safety_margin,
        "safe_budget_bytes": safe_budget,
        "max_safe_avg_bytes": best,
        "max_theoretical_avg_bytes": max_theoretical,
        "max_single_file_bytes": max_single_file_bytes,
    }


def render_max_avg_size_report(r: dict, out_dir: Path) -> str:
    fs = r["filesystem"]
    L = []
    L.append("Maximum Safe Average Object Size")
    L.append("=================================")
    L.append("")
    L.append(f"Output root                   : {out_dir}")
    L.append(f"Filesystem checked            : {fs.checked_path}")
    L.append(f"Filesystem total capacity     : {wg_common.human_size(fs.total_bytes)}")
    L.append(f"Filesystem free bytes         : {wg_common.human_size(fs.free_bytes)}")
    if r["coexisting_old_tier_bytes"]:
        L.append(f"Existing content under output root: {wg_common.human_size(r['coexisting_old_tier_bytes'])}")
    L.append(f"Usable bytes                  : {wg_common.human_size(r['usable_bytes'])}")
    L.append(f"Safety margin                 : {r['safety_margin_pct'] * 100:.0f}%")
    L.append(f"Assumed physical file count   : {r['physical_files']:,}")
    L.append(f"Maximum single object size    : {wg_common.human_size(r['max_single_file_bytes'])}")
    if fs.available_inodes is not None:
        L.append(f"Available inodes              : {fs.available_inodes:,}")
    L.append("")
    L.append(f"Maximum SAFE average object size       : {wg_common.human_size(r['max_safe_avg_bytes'])}")
    L.append(f"Maximum THEORETICAL average object size: {wg_common.human_size(r['max_theoretical_avg_bytes'])}")
    L.append("")
    L.append("Normal generation enforces the SAFE maximum (includes the safety margin), not the")
    L.append("theoretical maximum. Use `--avg-size <SIZE> --dry-run` to check any specific target.")
    return "\n".join(L) + "\n"
