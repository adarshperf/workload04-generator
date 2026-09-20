"""Workload04 discovery and baseline (source) analysis (master task section 3-6)."""
from __future__ import annotations

import json
import csv
import io
import statistics
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import common


def _contains_eicar(content: bytes, ext: str) -> bool:
    """Check for the EICAR test string, including inside zip members.

    Used both for classification (baseline manifest: which records are
    AV-test content) and for post-generation verification that the mandatory
    AV-test payload (see common.AV_TEST_URIS) is still genuinely present, and
    that no *other* file accidentally acquired it.

    A raw byte search misses an EICAR string that only exists inside a
    *compressed* zip member -- DEFLATE alters the byte representation, so the
    literal marker isn't present in the container's raw bytes. generate_zip()
    decompresses and re-stores members uncompressed (ZIP_STORED), so zip (and
    zip-like) seeds must be checked member-by-member too.
    """
    if common.EICAR_MARKER in content:
        return True
    if ext == "zip" or content[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for name in zf.namelist():
                    try:
                        if common.EICAR_MARKER in zf.read(name):
                            return True
                    except (zipfile.BadZipFile, RuntimeError, OSError):
                        continue
        except (zipfile.BadZipFile, OSError):
            pass
    return False


@dataclass
class SourceRecord:
    uri: str            # e.g. /workload04/gfx/gfx1/f14.gif
    resolved_path: Optional[Path]
    ext: str
    mime: str
    family: str
    size_bytes: int      # 0 if missing
    weight: int          # number of times this uri appears in the resource list
    exists: bool
    valid: bool = True   # False if file could not be opened at all
    eicar_detected: bool = False  # True if an EICAR test-string was found in the file


@dataclass
class BaselineStats:
    resource_list: Path
    source_root: Path
    total_entries: int
    unique_uris: int
    missing_files: int
    invalid_files: int
    total_corpus_bytes: int
    unique_file_average_bytes: float
    request_weighted_average_bytes: float
    median_bytes: float
    p50: float
    p90: float
    p95: float
    p99: float
    min_bytes: int
    max_bytes: int
    extension_distribution: dict = field(default_factory=dict)   # ext -> weighted request share
    mime_distribution: dict = field(default_factory=dict)        # mime -> weighted request share
    console_bucket_distribution: dict = field(default_factory=dict)
    family_stats: dict = field(default_factory=dict)  # family -> {count, weight, total_bytes, avg_bytes}
    duplicate_path_frequency: dict = field(default_factory=dict)  # uri -> count (only if >1)
    records: list = field(default_factory=list)  # List[SourceRecord]


def discover_resource_list(explicit: Optional[str]) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path("/opt/load/workload04-resources.txt"))
    candidates.append(common.REPO_ROOT / "configs" / "workload04-resources.txt")
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "Could not locate workload04-resources.txt. Tried: "
        + ", ".join(str(c) for c in candidates)
        + ". Pass --source-resource-list explicitly."
    )


def discover_source_root(explicit: Optional[str], resource_list: Path) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path("/opt/workload04"))
    candidates.append(common.REPO_ROOT / "workload04")

    # Sample a handful of lines to score each candidate root by resolution rate.
    sample_uris = []
    with resource_list.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if line and not line.startswith("#"):
                sample_uris.append(line)
            if len(sample_uris) >= 200:
                break

    best_root = None
    best_score = -1
    for root in candidates:
        if not root.exists():
            continue
        hits = 0
        for uri in sample_uris:
            try:
                resolved = common.resolve_uri_to_path(root, uri)
            except ValueError:
                continue
            if resolved.is_file():
                hits += 1
        score = hits / max(1, len(sample_uris))
        if score > best_score:
            best_score = score
            best_root = root

    if best_root is None or best_score < 0.5:
        tried = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(
            f"Could not auto-detect a usable --source-root (best match resolved "
            f"{best_score * 100:.0f}% of sampled entries). Tried: {tried}. "
            "Pass --source-root explicitly."
        )
    return best_root


def parse_resource_list(resource_list: Path, source_root: Path) -> list:
    """Parse the resource list preserving duplicate-line weighting."""
    counts = Counter()
    order = []
    with resource_list.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Reuse same three formats as the real parsers, path only.
            uri = line
            if line[0] == '"':
                close = line.find('"', 1)
                if close != -1:
                    uri = line[1:close]
            elif "\t" in line:
                uri = line.split("\t", 1)[0]
            if uri not in counts:
                order.append(uri)
            counts[uri] += 1

    records = []
    for uri in order:
        # Percent-decode for FILESYSTEM lookup only -- the corpus stores files
        # under their real, unencoded names (e.g. literal spaces/parens), so a
        # literal join can never find them even though they genuinely exist
        # (see common.resolve_uri_to_path docstring). `uri` itself is kept
        # untouched everywhere else (resource list, runtime paths, etc.).
        try:
            resolved = common.resolve_uri_to_path(source_root, uri)
        except ValueError:
            resolved = source_root / uri.lstrip("/")  # traversal attempt -> report as not-found, don't crash
        # Lowercased for classification/grouping only -- the on-disk path
        # (rel/resolved) keeps its original case. Without this, "DLL"/"dll"/
        # "dLL" etc. were counted as separate extension classes even though
        # MIME/family lookup already lowercases, fragmenting budget slots.
        ext = Path(uri).suffix.lstrip(".").lower()
        mime = common.guess_mime(ext)
        family = common.classify_family(ext)
        exists = resolved.is_file()
        size = resolved.stat().st_size if exists else 0
        valid = True
        eicar_detected = False
        if exists:
            try:
                content = resolved.read_bytes()
                eicar_detected = _contains_eicar(content, ext)
            except OSError:
                valid = False
        records.append(
            SourceRecord(
                uri=uri,
                resolved_path=resolved if exists else None,
                ext=ext,
                mime=mime,
                family=family,
                size_bytes=size,
                weight=counts[uri],
                exists=exists,
                valid=valid,
                eicar_detected=eicar_detected,
            )
        )
    return records


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def compute_baseline(resource_list: Path, source_root: Path) -> BaselineStats:
    records = parse_resource_list(resource_list, source_root)
    total_entries = sum(r.weight for r in records)
    unique_uris = len(records)
    missing = [r for r in records if not r.exists]
    invalid = [r for r in records if r.exists and not r.valid]
    usable = [r for r in records if r.exists and r.valid]

    total_corpus_bytes = sum(r.size_bytes for r in usable)
    unique_avg = total_corpus_bytes / len(usable) if usable else 0.0

    weighted_bytes = sum(r.size_bytes * r.weight for r in usable)
    total_weight = sum(r.weight for r in usable)
    weighted_avg = weighted_bytes / total_weight if total_weight else 0.0

    # Weighted distribution of sizes (each occurrence contributes its file size once).
    expanded_sizes = sorted(
        s for r in usable for s in [r.size_bytes] * r.weight
    )
    median = _percentile(expanded_sizes, 50)

    ext_weight = Counter()
    mime_weight = Counter()
    bucket_weight = Counter()
    family_agg = defaultdict(lambda: {"count": 0, "weight": 0, "total_bytes": 0})
    for r in usable:
        ext_weight[r.ext or "(none)"] += r.weight
        mime_weight[r.mime] += r.weight
        bucket_weight[common.console_bucket(r.ext)] += r.weight
        fam = family_agg[r.family]
        fam["count"] += 1
        fam["weight"] += r.weight
        fam["total_bytes"] += r.size_bytes * r.weight

    for fam, agg in family_agg.items():
        agg["avg_bytes"] = agg["total_bytes"] / agg["weight"] if agg["weight"] else 0.0

    def _share(counter):
        total = sum(counter.values()) or 1
        return {k: v / total for k, v in sorted(counter.items(), key=lambda kv: -kv[1])}

    duplicate_paths = {r.uri: r.weight for r in records if r.weight > 1}

    return BaselineStats(
        resource_list=resource_list,
        source_root=source_root,
        total_entries=total_entries,
        unique_uris=unique_uris,
        missing_files=len(missing),
        invalid_files=len(invalid),
        total_corpus_bytes=total_corpus_bytes,
        unique_file_average_bytes=unique_avg,
        request_weighted_average_bytes=weighted_avg,
        median_bytes=median,
        p50=median,
        p90=_percentile(expanded_sizes, 90),
        p95=_percentile(expanded_sizes, 95),
        p99=_percentile(expanded_sizes, 99),
        min_bytes=expanded_sizes[0] if expanded_sizes else 0,
        max_bytes=expanded_sizes[-1] if expanded_sizes else 0,
        extension_distribution=_share(ext_weight),
        mime_distribution=_share(mime_weight),
        console_bucket_distribution=_share(bucket_weight),
        family_stats=dict(family_agg),
        duplicate_path_frequency=duplicate_paths,
        records=records,
    )


def write_baseline_outputs(stats: BaselineStats, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "baseline_manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["uri", "resolved_path", "extension", "mime_type", "family",
                    "size_bytes", "weight", "exists", "valid"])
        for r in stats.records:
            w.writerow([r.uri, str(r.resolved_path) if r.resolved_path else "",
                        r.ext, r.mime, r.family, r.size_bytes, r.weight,
                        r.exists, r.valid])

    summary = {
        "resource_list": str(stats.resource_list),
        "source_root": str(stats.source_root),
        "total_resource_entries": stats.total_entries,
        "unique_resource_count": stats.unique_uris,
        "missing_files": stats.missing_files,
        "invalid_files": stats.invalid_files,
        "total_corpus_bytes": stats.total_corpus_bytes,
        "unique_file_average_bytes": stats.unique_file_average_bytes,
        "request_weighted_average_bytes": stats.request_weighted_average_bytes,
        "median_bytes": stats.median_bytes,
        "p50": stats.p50,
        "p90": stats.p90,
        "p95": stats.p95,
        "p99": stats.p99,
        "min_bytes": stats.min_bytes,
        "max_bytes": stats.max_bytes,
        "extension_distribution": stats.extension_distribution,
        "mime_distribution": stats.mime_distribution,
        "console_bucket_distribution": stats.console_bucket_distribution,
        "family_stats": stats.family_stats,
        "duplicate_path_frequency_count": len(stats.duplicate_path_frequency),
    }
    (out_dir / "baseline_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def print_console_summary(stats: BaselineStats) -> None:
    print(f"Source resource list : {stats.resource_list}")
    print(f"Source root          : {stats.source_root}")
    print(f"Total resource entries: {stats.total_entries}  (unique paths: {stats.unique_uris})")
    print(f"Missing files         : {stats.missing_files}")
    print(f"Invalid files         : {stats.invalid_files}")
    print(f"Total corpus bytes    : {common.human_size(stats.total_corpus_bytes)}")
    print(f"Unique-file average   : {common.human_size(stats.unique_file_average_bytes)}")
    print(f"Request-weighted avg  : {common.human_size(stats.request_weighted_average_bytes)}  <-- primary target metric")
    print(f"Median / P90 / P95 / P99: "
          f"{common.human_size(stats.p50)} / {common.human_size(stats.p90)} / "
          f"{common.human_size(stats.p95)} / {common.human_size(stats.p99)}")
    print(f"Min / Max             : {common.human_size(stats.min_bytes)} / {common.human_size(stats.max_bytes)}")
    print()
    print("Console bucket distribution (request-weighted):")
    for label, share in stats.console_bucket_distribution.items():
        print(f"    {label:<10} {share * 100:5.1f}%")
