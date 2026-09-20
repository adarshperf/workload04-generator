"""Bundled default Workload04 corpus: safe extraction + verification.

The application ships `bundled_workloads/workload04.zip` (+ a companion
`.sha256` checksum file) as the portable source for the DEFAULT golden
Workload04 corpus. On first use (when the default runtime location
`/opt/workload04` -- or its non-POSIX dev fallback -- doesn't exist yet, or
fails verification), the bundled zip is extracted there automatically; the
user never has to unzip it by hand. Every subsequent run reuses the
already-extracted, already-verified corpus without re-extracting.

This module is ONLY used for the DEFAULT bundled Workload04 flow. An
explicit `--workload-root` bypasses all of this entirely (spec section 5).
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path
from typing import Optional, Tuple

from . import common as wg_common
from . import baselinefreeze


class CorpusVerificationError(Exception):
    pass


def bundled_zip_path() -> Path:
    return wg_common.TOOL_ROOT / "bundled_workloads" / "workload04.zip"


def bundled_zip_checksum_path() -> Path:
    return bundled_zip_path().with_suffix(".zip.sha256")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_bundled_zip_checksum() -> Tuple[bool, str]:
    """Verify the shipped workload04.zip against its companion .sha256 file
    (spec section 17) -- catches a corrupted or silently-modified bundle
    before it is ever extracted."""
    zip_path = bundled_zip_path()
    sha_path = bundled_zip_checksum_path()
    if not zip_path.is_file():
        return False, f"bundled zip not found at {zip_path}"
    if not sha_path.is_file():
        return False, f"checksum file not found at {sha_path}"
    expected = sha_path.read_text(encoding="utf-8").strip().split()[0].lower()
    actual = _sha256_file(zip_path).lower()
    if expected != actual:
        return False, f"checksum mismatch: expected {expected}, actual {actual}"
    return True, "OK"


def default_workload_root(label: str = None) -> Path:
    """Default runtime location for the extracted default workload corpus:
    /opt/<label> on POSIX (e.g. /opt/workload04), with the same
    WORKLOAD04_SOURCE_ROOT-env-var-override / non-POSIX-dev-fallback
    pattern used by get_generated_root()."""
    label = label or wg_common.DEFAULT_WORKLOAD_LABEL
    env = os.environ.get("WORKLOAD04_SOURCE_ROOT")
    if env:
        return Path(env).resolve()
    if os.name == "posix":
        return Path(f"/opt/{label}")
    return wg_common.TOOL_ROOT / label


def _safe_zip_members(zf: zipfile.ZipFile, dest_dir: Path):
    """Validate every member resolves strictly inside dest_dir before
    extracting anything (spec section 18: no path traversal, no absolute
    paths, all-or-nothing). Returns the validated member list."""
    dest_dir = dest_dir.resolve()
    members = zf.infolist()
    for m in members:
        name = m.filename
        if name.startswith("/") or name.startswith("\\") or ":" in name.split("/")[0]:
            raise CorpusVerificationError(f"unsafe absolute-looking zip entry: {name!r}")
        resolved = (dest_dir / name).resolve()
        try:
            resolved.relative_to(dest_dir)
        except ValueError:
            raise CorpusVerificationError(f"zip entry escapes destination directory: {name!r}")
    return members


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> int:
    """Extract `zip_path` into `dest_dir`, rejecting any entry that would
    write outside `dest_dir` (path traversal, absolute paths). Validates
    ALL entries before extracting ANY of them. Returns the file count
    extracted."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        members = _safe_zip_members(zf, dest_dir)
        count = 0
        for m in members:
            zf.extract(m, path=dest_dir)
            if not m.is_dir():
                count += 1
    return count


def verify_extracted_workload04(source_root: Path, resource_list: Path) -> Tuple[bool, str]:
    """Lightweight-but-real structural verification of an extracted (or
    pre-existing) default Workload04 corpus, reusing the SAME analysis
    baselinefreeze.freeze() would use -- never a separate, potentially-
    diverging check. Enforces the known Workload04 invariants exactly
    (10,000 files, 3 EICAR, 0.03%) since this is specifically the DEFAULT
    bundled corpus verification path."""
    if not source_root.is_dir():
        return False, f"source root {source_root} does not exist or is not a directory"
    if not resource_list.is_file():
        return False, f"resource list {resource_list} does not exist"
    try:
        stats, _raw_order = baselinefreeze.analyze(resource_list, source_root)
        physical_count = baselinefreeze.count_physical_files_on_disk(source_root, exclude={resource_list})
        discrepancies = baselinefreeze.check_discrepancies(
            stats, physical_count, enforce_known_invariants=True)
    except Exception as e:  # pragma: no cover -- defensive: any parse error means "invalid"
        return False, f"could not analyze corpus: {e}"
    if discrepancies:
        return False, "; ".join(discrepancies)
    return True, "OK"


def ensure_default_workload_available(label: str = None) -> Tuple[Path, Path]:
    """Guarantee the default bundled Workload04 corpus is present, valid,
    and ready to use at its default runtime location, auto-extracting the
    bundled zip only if needed (spec sections 3-4, 19):

    1. If the default location already contains a corpus that verifies OK,
       reuse it as-is -- NEVER re-extract, never overwrite.
    2. Otherwise (missing, or present but invalid/incomplete/wrong-corpus),
       extract the bundled zip fresh and verify the result.
    3. If verification still fails after extraction, abort loudly -- never
       silently generate from an unverified/wrong corpus.

    Returns (source_root, resource_list) ready to pass to baselinefreeze.freeze().
    """
    label = label or wg_common.DEFAULT_WORKLOAD_LABEL
    root = default_workload_root(label)
    resource_list = root / f"{label}-resources.txt"
    source_root = root  # payload lives at root/<label>/... (doubled-root convention)

    ok, _detail = verify_extracted_workload04(source_root, resource_list)
    if ok:
        return source_root, resource_list

    # Not present or not valid yet -- extract the bundled zip.
    zip_path = bundled_zip_path()
    if not zip_path.is_file():
        raise CorpusVerificationError(
            f"No usable {label} corpus at {root}, and no bundled zip found at {zip_path}. "
            f"Provide --workload-root explicitly, or restore bundled_workloads/{label}.zip."
        )
    checksum_ok, checksum_detail = verify_bundled_zip_checksum()
    if not checksum_ok:
        raise CorpusVerificationError(
            f"Bundled {zip_path.name} failed checksum verification ({checksum_detail}) -- "
            f"refusing to extract a possibly-corrupted/modified archive."
        )

    if root.exists():
        # Something is there but didn't verify -- do NOT silently overwrite
        # or merge into it (spec section 4).
        raise CorpusVerificationError(
            f"{root} already exists but failed corpus verification ({_detail}). "
            f"Refusing to overwrite it automatically. Remove/rename {root} and retry, "
            f"or pass --workload-root to use a different corpus location explicitly."
        )

    root.mkdir(parents=True, exist_ok=True)
    safe_extract_zip(zip_path, root)

    ok, detail = verify_extracted_workload04(source_root, resource_list)
    if not ok:
        raise CorpusVerificationError(
            f"Extracted {label} corpus at {root} failed verification after extraction: {detail}. "
            f"This indicates a corrupted bundle or an incomplete extraction -- remove {root} and retry."
        )
    return source_root, resource_list


def load_or_bootstrap_default_baseline(baseline_dir: Path, label: str = None):
    """Return a ready-to-use FrozenBaseline for the default bundled
    Workload04 workload, per spec section 19's first-run/second-run
    behavior:

    - If a baseline is already frozen at `baseline_dir` AND its recorded
      source_root is still reachable, reuse it as-is -- the bundled zip is
      never touched, matching "second run must not unzip again."
    - Otherwise, ensure the default corpus is available (extracting the
      bundled zip only if actually needed) and (re-)freeze the baseline
      from it.
    """
    label = label or wg_common.DEFAULT_WORKLOAD_LABEL
    try:
        existing = baselinefreeze.FrozenBaseline(baseline_dir)
        if existing.source_root_reachable():
            return existing
    except FileNotFoundError:
        pass

    source_root, resource_list = ensure_default_workload_available(label)
    baselinefreeze.freeze(resource_list, source_root, baseline_dir,
                           label=label, accept_discrepancies=False)
    return baselinefreeze.FrozenBaseline(baseline_dir)
