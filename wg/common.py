"""Shared paths, size parsing and small helpers for workload04_generator.

Reuses the well-tested, purely functional leaf utilities from the vendored
``wmime`` package (content generators, MIME tables, EICAR detection,
baseline resource-list parsing) instead of re-implementing byte-exact
JPEG/PNG/GIF/PDF/ZIP/PE handling from scratch. Nothing from that package's
*directory/manifest conventions* (generator.py, distribution.py,
validator.py) is imported -- this tool defines its own, different, output
layout and validation rules per its own spec.

``wmime`` is vendored directly under this package (``workload04_generator/
wmime/``) so the whole ``workload04_generator/`` directory is a single,
standalone, copyable unit with no dependency on any sibling checkout.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]          # workload04_generator/ (project root)
REPO_ROOT = TOOL_ROOT                                     # standalone: no wrapping repo above this

if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

try:
    from wmime import common as wmime_common          # noqa: E402
    from wmime import formats as wmime_formats        # noqa: E402
    from wmime import baseline as wmime_baseline       # noqa: E402
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "workload04_generator requires its vendored 'wmime' package "
        f"(workload04_generator/wmime/) but it could not be imported from "
        f"{TOOL_ROOT}: {e}. If you moved/copied only some files, re-copy the "
        f"whole workload04_generator/ directory, including its wmime/ subfolder."
    ) from e

human_size = wmime_common.human_size
EICAR_MARKER = wmime_common.EICAR_MARKER
AV_TEST_URIS = wmime_common.AV_TEST_URIS
guess_mime = wmime_common.guess_mime
classify_family = wmime_common.classify_family
resolve_uri_to_path = wmime_common.resolve_uri_to_path
decode_relpath_for_filesystem = wmime_common.decode_relpath_for_filesystem

_WHITESPACE_RE = re.compile(r"\s")


def normalize_relpath_no_whitespace(rel: str) -> str:
    """Replace every whitespace character (space, tab, ...) in a relative
    path with '_' -- applies to BOTH directory and filename components in
    one pass, since it operates on the whole path string. Generated
    workload physical paths and runtime URIs must never contain whitespace
    (2026-09-20 spec: whitespace was previously preserved as a literal
    space in the physical filename and as '%20' in the runtime URI; both
    forms are now eliminated in favor of '_')."""
    return _WHITESPACE_RE.sub("_", rel)
DEFAULT_CLASS_SIZE_RATIO_MIN = wmime_common.DEFAULT_CLASS_SIZE_RATIO_MIN
DEFAULT_CLASS_SIZE_RATIO_MAX = wmime_common.DEFAULT_CLASS_SIZE_RATIO_MAX

GENERATOR_VERSION = "2.0.0"

# --- Default budgets (advanced/debug overrides only; never required) -----
# 2026-09-19 rework: the diversity model is now strict 1:1 (every baseline
# physical resource gets its own generated physical resource -- no bounded
# pool, no per-class collapsing). DEFAULT_MAX_SINGLE_FILE_BYTES remains as a
# hard per-file safety cap (protects against a single pathological record
# blowing past a sane file size); the pool-sizing knobs from the previous
# design (soft corpus budget, max total unique files, min pool per class) no
# longer apply and have been removed.
DEFAULT_MAX_SINGLE_FILE_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB hard per-file cap
DEFAULT_DISK_SAFETY_MARGIN = 0.15  # require 15% headroom beyond the estimate
# Authoritative request-weighted average object size tolerance (spec Part 4/5):
# PASS requires the per-LOGICAL-REQUEST weighted average to be within this
# fraction of --avg-size. Shared by the generation retry/convergence loop
# AND the validation/audit-size PASS gate, so both always agree.
DEFAULT_SIZE_TOLERANCE_PCT = 0.03

# --- Workload label (spec: support Workload04 by default, plus arbitrary
# future workloads e.g. "workload18"/"workload24" via --workload-root) -----
# "workload04" remains the default/bundled golden workload; its known
# invariants (10,000 files, 3 EICAR, 0.03%) are still validated exactly.
# Any other label is treated generically: actual corpus facts are measured
# and preserved, never forced to match Workload04's specific numbers.
DEFAULT_WORKLOAD_LABEL = "workload04"

_workload_label_override: str = None


def set_workload_label(label) -> None:
    """Explicitly set the active workload label for this process (derived
    from --workload-root's basename, or left at the default "workload04").
    Affects default_baseline_dir(), get_generated_root(), and
    uri_root_prefix()/output_dir_for_tier() when called without an
    explicit label/tier override."""
    global _workload_label_override
    _workload_label_override = str(label) if label else None


def get_workload_label() -> str:
    return _workload_label_override or DEFAULT_WORKLOAD_LABEL


def sanitize_workload_label(raw: str) -> str:
    """Turn an arbitrary --workload-root basename into a safe label usable
    in filesystem paths and URI segments (lowercase, alnum/-/_ only)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.strip()).strip("-_")
    return cleaned.lower() or "workload"


_RESOURCE_LIST_SUFFIX_RE = re.compile(r"[-_]?resources?$", re.IGNORECASE)


def derive_label_from_resource_list_name(resource_list: Path):
    """Derive a workload label from a conventional ``<label>-resources.txt``
    filename (e.g. ``workload18-resources.txt`` -> ``workload18``). Returns
    None if the filename doesn't follow that convention, so callers can
    fall back to another source (e.g. the workload root's basename)."""
    stem = Path(resource_list).stem  # strips .txt
    label = _RESOURCE_LIST_SUFFIX_RE.sub("", stem)
    return label or None


# Frozen baselines live alongside this tool, one subdirectory per workload
# label, so different workloads (Workload04, Workload18, Workload24, ...)
# never overwrite each other's metadata.
BASELINES_ROOT = TOOL_ROOT / "baselines"


def default_baseline_dir(label: str = None) -> Path:
    return BASELINES_ROOT / (label or get_workload_label())


# Back-compat constant: the default (Workload04) baseline directory, used as
# the default CLI --baseline-dir value and by tests that don't care about
# other workloads.
DEFAULT_BASELINE_DIR = default_baseline_dir(DEFAULT_WORKLOAD_LABEL)

# --- Output root (spec: "ALL GENERATED TOOL OUTPUT MUST GO UNDER /opt/") ---
# Production default output root is workload-specific:
# /opt/generated_<workload-label>/ (never directly under REPO_ROOT/the tool
# checkout, so it can never collide with, or be mistaken for, the original
# golden-baseline corpus directory, and so the tool never silently writes a
# multi-GiB workload into whatever directory happened to be the CWD).
def _default_production_output_root(label: str) -> Path:
    return Path(f"/opt/generated_{label}")


# Fallback used ONLY when the production default isn't appropriate for this
# platform/session (e.g. local Windows development or CI, where /opt does
# not exist as a concept) -- never used in a real Linux deployment unless
# explicitly requested via --output-root/WORKLOAD04_OUTPUT_ROOT. Kept next
# to the tool checkout, exactly like the previous (pre-/opt) default.
def _dev_fallback_output_root(label: str) -> Path:
    return TOOL_ROOT / f"generated_{label}"


_generated_root_override: Path = None


def set_generated_root(path) -> None:
    """Explicitly override the output root for this process (--output-root).
    Takes priority over the WORKLOAD04_OUTPUT_ROOT environment variable and
    the platform default. Must be called before any output_dir_for_tier()/
    get_generated_root() call that should observe it."""
    global _generated_root_override
    _generated_root_override = Path(path).resolve() if path else None


def get_generated_root() -> Path:
    """Resolve the effective output root, in priority order:

    1. explicit ``--output-root`` (``set_generated_root()``)
    2. ``WORKLOAD04_OUTPUT_ROOT`` environment variable
    3. production default ``/opt/generated_<workload-label>`` (POSIX)
    4. dev/test fallback next to the tool checkout (non-POSIX, e.g. Windows)

    Resolved dynamically (never cached at import time) so a CLI override
    applied after import still takes effect everywhere."""
    if _generated_root_override is not None:
        return _generated_root_override
    env = os.environ.get("WORKLOAD04_OUTPUT_ROOT")
    if env:
        return Path(env).resolve()
    label = get_workload_label()
    if os.name == "posix":
        return _default_production_output_root(label)
    return _dev_fallback_output_root(label)

# Fixed set of metadata filenames written at the top of every generated tier
# directory, alongside (never inside) the payload files -- used to count
# "payload files" as everything else, without needing a nested files/
# subfolder (which would break HttpBlaster's verbatim root+URI path
# resolution -- see README.md 'Directory layout' for the verified reason).
METADATA_FILENAMES = {
    "workload-resources.txt", "manifest.json", "manifest.csv",
    "validation-report.txt", "validation-report.json",
    "diversity-report.json", "size-report.json",
    "httpstorm-config.ini", "README.md",
}

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$")
_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
    "t": 1024 ** 4, "tb": 1024 ** 4, "tib": 1024 ** 4,
}


def parse_size(text: str) -> int:
    """Parse '100KiB' / '1MiB' / '250MiB' / '512' (bytes) / '2GiB' -> int bytes."""
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"Invalid size expression: {text!r} (expected e.g. '100KiB', '1MiB', '250MiB')")
    value, unit = m.groups()
    unit_key = unit.lower()
    if unit_key not in _UNITS:
        raise ValueError(f"Unknown size unit {unit!r} in {text!r} (expected B/KiB/MiB/GiB/TiB)")
    return int(round(float(value) * _UNITS[unit_key]))


def tier_name(text: str) -> str:
    """Normalize a user-supplied size string into a directory-safe tier name,
    e.g. '100kib' -> '100KiB', '1mb' -> '1MiB', '250MIB' -> '250MiB'.
    Falls back to a byte-count-derived name for odd/arbitrary sizes so the
    output directory name always stays deterministic and collision-free."""
    m = _SIZE_RE.match(text)
    if m:
        value, unit = m.groups()
        unit_key = unit.lower()
        if unit_key in _UNITS and "." not in value:
            canonical = {
                "": "B", "b": "B",
                "k": "KiB", "kb": "KiB", "kib": "KiB",
                "m": "MiB", "mb": "MiB", "mib": "MiB",
                "g": "GiB", "gb": "GiB", "gib": "GiB",
                "t": "TiB", "tb": "TiB", "tib": "TiB",
            }[unit_key]
            return f"{value}{canonical}"
    # Arbitrary/fractional size: derive a clean name from the byte count itself.
    n = parse_size(text)
    return human_size(n).replace(".00", "").replace(".", "_")


def output_dir_for_tier(tier: str, label: str = None) -> Path:
    return get_generated_root() / f"{label or get_workload_label()}-{tier}"


def uri_root_prefix(tier: str, label: str = None) -> str:
    return f"/{label or get_workload_label()}-{tier}/"


def disk_free_bytes(path: Path) -> int:
    """Free space on the filesystem that will hold `path` (walks up to the
    nearest existing ancestor -- the target tier directory itself usually
    doesn't exist yet at dry-run/feasibility-check time)."""
    import shutil
    p = path
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return shutil.disk_usage(str(p)).free
