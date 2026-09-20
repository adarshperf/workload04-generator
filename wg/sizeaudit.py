"""Independent, read-only request-weighted size audit (spec Part 4/5/7/8).

Computes the request-weighted average STRICTLY from:
  - the generated resource list on disk (one entry per LOGICAL request, in
    order -- if a URI occurs multiple times, its file size is counted once
    per occurrence, not once total),
  - the actual generated physical file's size ON DISK, re-read via `stat()`
    fresh every time this module runs -- never trusted from any in-memory
    generation result or from manifest.json/manifest.csv's own recorded
    `generated_size_bytes` column.

This is the AUTHORITATIVE metric for whether --avg-size was honored. It is
NOT an average over unique physical files (which is only equivalent to the
request-weighted average when every URI has weight 1, as the current golden
baseline does -- this module stays correct even if a future baseline has
duplicate resource-list lines).

The resource list's URI stays percent-ENCODED (e.g. '%20'); physical
filenames on disk are percent-DECODED (e.g. a literal space) -- see
wg/generate.py's _fs_rel_from_uri. This module decodes the URI's relative
part before resolving it against output_dir, exactly mirroring how the
generator itself names the physical file.

Do NOT use HTTP Storm/HttpBlaster runtime metrics to prove this number is
correct -- those may include protocol/header overhead depending on the exact
metric being read. This generator-side, filesystem-based calculation is the
source of truth; a later performance-test run is a secondary sanity check,
never the other way around.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from . import common as wg_common


@dataclass
class SizeAuditResult:
    tier: str
    output_dir: Path
    target_bytes: int
    logical_request_count: int = 0
    physical_payload_file_count: int = 0
    unique_payload_paths: int = 0
    missing_references: List[str] = field(default_factory=list)
    sizes: List[int] = field(default_factory=list)  # one entry per LOGICAL request, resource-list order

    @property
    def total_payload_bytes(self) -> int:
        return sum(self.sizes)

    @property
    def weighted_average_bytes(self) -> float:
        return (sum(self.sizes) / len(self.sizes)) if self.sizes else 0.0

    @property
    def min_bytes(self) -> int:
        return min(self.sizes) if self.sizes else 0

    @property
    def max_bytes(self) -> int:
        return max(self.sizes) if self.sizes else 0

    def percentile(self, pct: float) -> float:
        if not self.sizes:
            return 0.0
        s = sorted(self.sizes)
        k = (len(s) - 1) * (pct / 100.0)
        f = int(k)
        c = min(f + 1, len(s) - 1)
        if f == c:
            return float(s[f])
        return s[f] + (s[c] - s[f]) * (k - f)

    @property
    def median_bytes(self) -> float:
        return self.percentile(50)

    @property
    def difference_bytes(self) -> float:
        return self.weighted_average_bytes - self.target_bytes

    @property
    def error_pct(self) -> float:
        if not self.target_bytes:
            return 0.0
        return self.difference_bytes / self.target_bytes * 100.0

    @property
    def within_tolerance(self) -> bool:
        return self.target_bytes == 0 or abs(self.error_pct) <= wg_common.DEFAULT_SIZE_TOLERANCE_PCT * 100.0

    @property
    def passed(self) -> bool:
        return (
            self.within_tolerance
            and not self.missing_references
            and self.logical_request_count == self.physical_payload_file_count == self.unique_payload_paths
        )


def audit(tier: str, output_dir: Path, target_bytes: int) -> SizeAuditResult:
    """Pure read-only filesystem audit. Never writes anything."""
    result = SizeAuditResult(tier=tier, output_dir=output_dir, target_bytes=target_bytes)

    resources_path = output_dir / "workload-resources.txt"
    lines = [ln for ln in resources_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    result.logical_request_count = len(lines)

    prefix = wg_common.uri_root_prefix(tier)
    unique_paths = set()
    for line in lines:
        uri = line.strip()
        rel_encoded = uri[len(prefix):] if uri.startswith(prefix) else uri.lstrip("/")
        # The resource list keeps the URI percent-ENCODED (e.g. '%20'), but
        # physical filenames on disk are percent-DECODED (e.g. a literal
        # space) -- see wg/generate.py's _fs_rel_from_uri. Must decode here
        # too, or every encoded filename would wrongly show up as missing.
        try:
            rel = wg_common.decode_relpath_for_filesystem(rel_encoded)
        except ValueError:
            result.missing_references.append(uri)
            continue
        path = output_dir / rel
        unique_paths.add(rel)
        if not path.is_file():
            result.missing_references.append(uri)
            continue
        result.sizes.append(path.stat().st_size)

    result.unique_payload_paths = len(unique_paths)
    result.physical_payload_file_count = sum(
        1 for p in output_dir.rglob("*") if p.is_file() and p.name not in wg_common.METADATA_FILENAMES
    )
    return result


def render_text_report(result: SizeAuditResult) -> str:
    L = []
    L.append(f"Size Audit -- workload04-{result.tier}")
    L.append("=" * (17 + len(result.tier)))
    L.append("")
    L.append(f"Target request-weighted average : {wg_common.human_size(result.target_bytes)}")
    L.append(f"Actual request-weighted average  : {wg_common.human_size(result.weighted_average_bytes)}")
    sign = "+" if result.difference_bytes >= 0 else ""
    L.append(f"Difference                       : {sign}{wg_common.human_size(result.difference_bytes)}")
    L.append(f"Error                            : {result.error_pct:+.2f}%  "
             f"(tolerance +/-{wg_common.DEFAULT_SIZE_TOLERANCE_PCT * 100:.0f}%)")
    L.append(f"Logical requests                : {result.logical_request_count:,}")
    L.append(f"Physical payload files           : {result.physical_payload_file_count:,}")
    L.append(f"Unique payload paths             : {result.unique_payload_paths:,}")
    L.append(f"Total payload bytes              : {wg_common.human_size(result.total_payload_bytes)} "
             f"({result.total_payload_bytes:,} bytes)")
    L.append(f"Minimum object size              : {wg_common.human_size(result.min_bytes)}")
    L.append(f"Maximum object size              : {wg_common.human_size(result.max_bytes)}")
    L.append(f"Median                           : {wg_common.human_size(result.median_bytes)}")
    L.append(f"P90                              : {wg_common.human_size(result.percentile(90))}")
    L.append(f"P95                              : {wg_common.human_size(result.percentile(95))}")
    L.append(f"P99                              : {wg_common.human_size(result.percentile(99))}")
    L.append(f"Missing references               : {len(result.missing_references)}")
    if result.missing_references:
        for uri in result.missing_references[:20]:
            L.append(f"    - {uri}")
        if len(result.missing_references) > 20:
            L.append(f"    ... (+{len(result.missing_references) - 20} more)")
    L.append("")
    L.append(f"Result: {'PASS' if result.passed else 'FAIL'}")
    return "\n".join(L) + "\n"
