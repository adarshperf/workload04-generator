"""Physical file generation + resource-list construction (strict 1:1 model).

Every `PlannedRecord` from `pool.WorkloadPlan` becomes exactly one physical
file on disk, at the SAME relative path as the original baseline resource
(only the leading root segment is rewritten, e.g. /workload04/... ->
/workload04-100KiB/...). Nothing is pooled, shared, or collapsed:

  * physical file count == logical request-list line count == baseline
    logical entry count, always, by construction,
  * every original line's URI, extension, MIME class and relative directory
    position is unchanged (only WHICH bytes live at that path changes), and
  * the original line ORDER is preserved exactly (resource list is a
    line-for-line rewrite, not a rebuild).

RUNTIME URI and PHYSICAL FILESYSTEM PATH are now the SAME normalized string
(only the '/workload04-<TIER>/' root prefix differs), per the 2026-09-20
whitespace-normalization spec: every generated path is percent-decoded,
then every whitespace character is replaced with '_' (see wg/pathnorm.py,
the single source of truth for this pipeline -- used by both this module
and validate.py's independent re-check). E.g. baseline
'/workload04/gfx/gfx2/Kopie%20(3)%20von%20b1.jpg' becomes physical file
'gfx/gfx2/Kopie_(3)_von_b1.jpg' AND runtime URI
'/workload04-100KiB/gfx/gfx2/Kopie_(3)_von_b1.jpg'. Deterministic collision
handling (pathnorm._apply_collision_suffix) guarantees the 10,000 final
paths stay unique even when two different baseline URIs normalize to the
same string.

Directory layout (see README.md "Directory layout" for the HttpBlaster
path-resolution proof this is based on):

    generated_workload04/workload04-<TIER>/<relpath...>   <- payload files
    generated_workload04/workload04-<TIER>/<metadata>      <- fixed filenames
                                                               (wg_common.METADATA_FILENAMES)

httpblaster.path_root must be set to `generated_workload04/` (the PARENT of
the tier directory) so that its verbatim `path_root + URI` concatenation
resolves correctly -- this mirrors the original corpus's own
`source_root=.../workload04` + `/workload04/...` URI convention exactly.
"""
from __future__ import annotations

import csv
import io
import json
import random
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import common as wg_common
from . import pathnorm
from .common import wmime_formats
from .pool import WorkloadPlan, PlannedRecord
from .baselinefreeze import FrozenBaseline


def _zip_contains_eicar(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                try:
                    if wg_common.EICAR_MARKER in zf.read(name):
                        return True
                except (zipfile.BadZipFile, RuntimeError, OSError):
                    continue
    except (zipfile.BadZipFile, OSError):
        pass
    return False


@dataclass
class GeneratedFile:
    uri: str                       # original baseline URI, e.g. /workload04/gfx/gfx1/f14.gif
    rel_path: str                  # PHYSICAL + RUNTIME final path (same string, no root prefix),
                                    # whitespace-free, e.g. 'gfx/gfx2/Kopie_(3)_von_b1.jpg'
    rewritten_uri: str              # RUNTIME URI = uri_root_prefix(tier) + rel_path
    extension: str
    mime_type: str
    family_used: str
    effective_family: str
    source_size_bytes: int
    generated_size_bytes: int
    has_source_seed: bool
    baseline_decoded_rel: str = ""    # baseline physical relative path, percent-decoded, PRE-normalization
    normalized_rel: str = ""           # whitespace-free path BEFORE any collision suffix
    collision_suffix: Optional[str] = None  # full suffixed rel_path, if a collision occurred
    is_av_test: bool = False
    validation_ok: bool = True
    validation_msg: str = ""
    deviation: Optional[str] = None


@dataclass
class GenerationResult:
    tier: str
    target_bytes: int
    seed: int
    output_dir: Path
    files: List[GeneratedFile] = field(default_factory=list)
    resource_lines: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def generate_workload(baseline: FrozenBaseline, plan: WorkloadPlan, *,
                       tier: str, seed: int, verbose: bool = False,
                       allow_synthetic_fallback: bool = False,
                       output_dir: Optional[Path] = None) -> GenerationResult:
    """Synthetic fallback (content generated with no real source seed) is a
    diagnostic capability only -- it is never used silently. Callers in the
    normal generation path must refuse to call this at all when
    `plan.missing_seed_count > 0` unless the user explicitly opted in via
    `allow_synthetic_fallback=True` (see workload04_generator.py cmd_generate's
    `--allow-synthetic-fallback` flag); this function still enforces it here
    as a defense-in-depth check for any other caller (e.g. tests).

    `output_dir` defaults to `wg_common.output_dir_for_tier(tier)` but can be
    overridden to write into a staging directory (spec section 14, atomic
    generation) -- the runtime URI prefix is always derived from `tier`
    regardless of where the files are physically staged, so the resource
    list stays correct once the staging directory is atomically moved into
    its final location."""
    if plan.missing_seed_count and not allow_synthetic_fallback:
        missing_uris = [r.uri for r in plan.records if not r.has_source_seed]
        raise RuntimeError(
            f"{plan.missing_seed_count} baseline record(s) have no resolvable source seed "
            f"(e.g. {missing_uris[0]!r}) and allow_synthetic_fallback=False. Fix the source "
            f"corpus/resource list (see analyze_missing_baseline_entries.py) or re-run with "
            f"--allow-synthetic-fallback to explicitly accept synthetic, non-representative "
            f"content for these records."
        )
    if output_dir is None:
        output_dir = wg_common.output_dir_for_tier(tier)
    result = GenerationResult(tier=tier, target_bytes=plan.target_bytes, seed=seed, output_dir=output_dir)

    normalized_paths = pathnorm.build_normalized_paths([rec.uri for rec in plan.records], seed)
    prefix = wg_common.uri_root_prefix(tier)

    total = len(plan.records)
    resource_lines = []
    for i, rec in enumerate(plan.records):
        if verbose and total > 200 and i % 500 == 0:
            print(f"  ... {i}/{total} physical files generated")

        norm = normalized_paths[rec.uri]
        seed_bytes = b""
        if rec.has_source_seed:
            resolved = wg_common.resolve_uri_to_path(baseline.source_root, rec.uri)
            try:
                seed_bytes = resolved.read_bytes()
            except OSError as e:
                result.warnings.append(f"could not read seed for {rec.uri}: {e}; using empty seed")

        rng = random.Random(f"{seed}:{rec.uri}")
        data, deviation = wmime_formats.generate(rec.family, seed_bytes, rec.target_size_bytes, rng)
        effective_family = "binary_fallback" if (deviation or "").startswith("binary_fallback") else rec.family
        ok, msg = wmime_formats.validate(effective_family, data)

        if rec.is_av_test:
            has_marker = wg_common.EICAR_MARKER in data or _zip_contains_eicar(data)
            if not has_marker:
                ok = False
                msg = (msg + "; " if msg else "") + "EICAR marker missing after generation -- REJECTED"
                result.warnings.append(f"CRITICAL: {rec.uri} lost its EICAR signature during generation!")

        if not rec.has_source_seed:
            result.warnings.append(
                f"{rec.uri}: no resolvable source file in the baseline corpus -- generated with "
                f"synthetic (non-representative) content of the correct type/size only."
            )

        out_path = output_dir / norm.final_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)

        rewritten = f"{prefix}{norm.final_rel}"
        gf = GeneratedFile(
            uri=rec.uri, rel_path=norm.final_rel, rewritten_uri=rewritten, extension=rec.extension,
            mime_type=rec.mime, family_used=rec.family, effective_family=effective_family,
            source_size_bytes=rec.source_size_bytes, generated_size_bytes=len(data),
            has_source_seed=rec.has_source_seed,
            baseline_decoded_rel=norm.decoded_rel, normalized_rel=norm.normalized_rel,
            collision_suffix=norm.collision_suffix, is_av_test=rec.is_av_test,
            validation_ok=ok, validation_msg=msg, deviation=deviation,
        )
        result.files.append(gf)
        resource_lines.append(rewritten)

    result.resource_lines = resource_lines
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "workload-resources.txt").write_text("\n".join(resource_lines) + "\n", encoding="utf-8")
    _write_manifest_csv(output_dir, result.files)
    return result


def _write_manifest_csv(output_dir: Path, files: List[GeneratedFile]) -> None:
    fieldnames = ["rel_path", "rewritten_uri", "uri", "baseline_decoded_rel", "normalized_rel",
                  "collision_suffix", "extension", "mime_type",
                  "family_used", "effective_family", "source_size_bytes", "generated_size_bytes",
                  "has_source_seed", "validation_ok", "validation_msg", "is_av_test"]
    with (output_dir / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for gf in files:
            w.writerow({
                "rel_path": gf.rel_path, "rewritten_uri": gf.rewritten_uri, "uri": gf.uri,
                "baseline_decoded_rel": gf.baseline_decoded_rel, "normalized_rel": gf.normalized_rel,
                "collision_suffix": gf.collision_suffix or "",
                "extension": gf.extension, "mime_type": gf.mime_type, "family_used": gf.family_used,
                "effective_family": gf.effective_family, "source_size_bytes": gf.source_size_bytes,
                "generated_size_bytes": gf.generated_size_bytes, "has_source_seed": gf.has_source_seed,
                "validation_ok": gf.validation_ok, "validation_msg": gf.validation_msg,
                "is_av_test": gf.is_av_test,
            })


def write_reproducibility_manifest(baseline: FrozenBaseline, plan: WorkloadPlan,
                                    result: GenerationResult, *, target_text: str) -> dict:
    total = len(result.files) or 1
    weighted_bytes = sum(gf.generated_size_bytes for gf in result.files)  # weight is always 1/record
    actual_avg = weighted_bytes / total

    manifest = {
        "generator_tool_version": wg_common.GENERATOR_VERSION,
        "algorithm_version": "1to1-mapping-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workload_label": baseline.label,
        "baseline_resource_list_sha256": baseline.header["resource_list_sha256"],
        "baseline_frozen_at": baseline.header["frozen_at"],
        "seed": result.seed,
        "tier": result.tier,
        "target_average_requested_text": target_text,
        "target_average_requested_bytes": result.target_bytes,
        "target_average_achieved_bytes": actual_avg,
        "target_average_error_pct": ((actual_avg - result.target_bytes) / result.target_bytes * 100.0)
                                     if result.target_bytes else 0.0,
        "scale_factor": plan.scale_factor,
        "logical_request_count": len(result.resource_lines),
        "baseline_logical_request_count": baseline.total_entries,
        "physical_file_count": len(result.files),
        "baseline_physical_file_count": baseline.total_entries,
        "unique_generated_relpaths": len({gf.rel_path for gf in result.files}),
        "eicar_count": sum(1 for gf in result.files if gf.is_av_test),
        "baseline_eicar_count": baseline.eicar_count,
        "missing_seed_count": plan.missing_seed_count,
        "max_single_file_bytes": plan.max_single_file_bytes,
        "estimated_disk_bytes": plan.estimated_disk_bytes,
        "actual_disk_bytes": sum(gf.generated_size_bytes for gf in result.files),
        "collision_count": sum(1 for gf in result.files if gf.collision_suffix),
        "warnings": result.warnings,
    }
    (result.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest

