"""Shared constants, MIME/extension classification and small utilities."""
from __future__ import annotations

import random
import string
import urllib.parse
from pathlib import Path
from typing import Optional

# --- Repository layout -------------------------------------------------

# This file is vendored inside the standalone workload04_generator/wmime/
# package -- both TOOL_ROOT and REPO_ROOT resolve to the standalone
# project's own root, so path fallbacks below never reach outside it.
TOOL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_ROOT


def decode_relpath_for_filesystem(rel: str) -> str:
    """Percent-decode a URL-relative path (no leading slash) for FILESYSTEM
    use only -- never for anything that ends up back in a runtime resource
    list/config, which must keep the URI's own percent-encoding verbatim
    (e.g. '/workload04/gfx/gfx2/Kopie%20(3)%20von%20b1.jpg').

    The corpus stores (and generated tiers must store) files under their
    real, unencoded names (e.g. 'Kopie (3) von b1.jpg' -- a literal space,
    not '%20'); this is the single shared decode used both for reading
    baseline seed files (via resolve_uri_to_path below) and for constructing
    the generated physical filename (2026-09-20: the generator was
    previously using the still-encoded relative path as the physical
    filename too, producing literal '%20' on disk and 404s in HttpBlaster).

    Raises ValueError if the decoded path contains a '..' segment (path
    traversal guard) -- callers should treat that the same as "not found"/
    "refuse to write".
    """
    decoded = urllib.parse.unquote(rel).replace("\\", "/")
    if any(part == ".." for part in decoded.split("/")):
        raise ValueError(f"path traversal detected in relative path: {rel!r}")
    return decoded


def resolve_uri_to_path(source_root: Path, uri: str) -> Path:
    """Resolve a resource-list URI to its FILESYSTEM path for content
    lookup only -- never for anything that ends up back in a runtime
    resource list or config (those must keep the URI's own percent-encoding
    verbatim, e.g. '/workload04/gfx/gfx2/Kopie%20(3)%20von%20b1.jpg').

    The corpus stores files under their real, unencoded names (e.g.
    'Kopie (3) von b1.jpg' -- a literal space, not '%20'), so the URI must
    be percent-DECODED before it is joined to source_root; a literal,
    non-decoded join can never find these files even though they genuinely
    exist (2026-09-19 missing-baseline-entries investigation: confirmed for
    219/219 previously-"missing" entries).

    Raises ValueError if the decoded path would escape source_root (path
    traversal guard) -- callers should treat that the same as "not found".
    """
    rel = uri.lstrip("/")
    decoded_rel = decode_relpath_for_filesystem(rel)
    root_resolved = source_root.resolve()
    candidate = (source_root / decoded_rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"resolved path escapes source_root: {uri!r} -> {candidate}")
    return candidate

# --- Default target sizes (master task section 2) -----------------------

DEFAULT_TARGETS = {
    "100KiB": 100 * 1024,
    "1MiB": 1 * 1024 * 1024,
    "4MiB": 4 * 1024 * 1024,
    "16MiB": 16 * 1024 * 1024,
    "64MiB": 64 * 1024 * 1024,
    "128MiB": 128 * 1024 * 1024,
    "250MiB": 250 * 1024 * 1024,
}

# --- MIME table -----------------------------------------------------------
# Sourced directly from mwg-perf/workload04_MIME/*.cdb file names, which encode
# "{type}_{subtype}.{ext}.cdb" for this exact corpus (see docs/DESIGN.md 1.3),
# plus a small number of extensions observed on disk but not present in that
# reference set (wav, olb, com, uce, pg, numeric/blank -> unknown).
EXTENSION_MIME = {
    "js": "application/javascript",
    "mjs": "application/javascript",
    "doc": "application/msword",
    "uce": "application/octet-stream",
    "pdf": "application/pdf",
    "xls": "application/vnd.ms-excel",
    "xml": "application/xml",
    "com": "application/x-msdos-program",
    "dll": "application/x-msdos-program",
    "exe": "application/x-msdos-program",
    "dat": "application/x-ns-proxy-autoconfig",
    "inf": "application/x-setupscript",
    "swf": "application/x-shockwave-flash",
    "zip": "application/zip",
    "au": "audio/basic",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "gif": "image/gif",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "ico": "image/x-icon",
    "css": "text/css",
    "323": "text/h323",
    "htm": "text/html",
    "html": "text/html",
    "ini": "text/plain",
    "log": "text/plain",
    "txt": "text/plain",
    "csv": "text/plain",
    "md": "text/plain",
    # Legacy / opaque types with no safe resize strategy implemented here.
    "olb": "application/octet-stream",
    "pg": "application/octet-stream",
}
DEFAULT_MIME = "application/octet-stream"  # matches "unkn_unkn" in the reference corpus

# The workload04 corpus intentionally includes real EICAR/AV-test archives.
# Per the SWG AV/GAM coverage requirement, these are RETAINED and grown (not
# excluded) -- see AV_TEST_URIS below. This marker is used both to *find* the
# AV-test payload (so it can be verified as still present after generation)
# and to make sure it never leaks into a file that isn't supposed to carry it.
EICAR_MARKER = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR"

# Baseline resource-list URIs that intentionally carry an EICAR/AV-test
# signature. These are mandatory, always-present, never-excluded entries:
# growing them must preserve the original payload byte-for-byte (only benign
# padding/members may be added) and their baseline request frequency.
AV_TEST_URIS = (
    "/workload04/arc/eicar.zip",
    "/workload04/arc/eicarcom2.zip",
    "/workload04/arc/crypt-ssleay-infected.zip",
)


def is_av_test_uri(uri: str) -> bool:
    return uri in AV_TEST_URIS


def guess_mime(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    if ext.isdigit() or ext == "":
        return DEFAULT_MIME
    return EXTENSION_MIME.get(ext, DEFAULT_MIME)


# --- Format family classification ---------------------------------------
# Determines which byte-exact, format-aware padding strategy applies.

_FAMILY_BY_EXT = {
    "html": "html", "htm": "html",
    "css": "css",
    "js": "js", "mjs": "js",
    "xml": "xml",
    "txt": "text", "log": "text", "ini": "text", "csv": "text", "md": "text",
    "jpg": "jpeg", "jpeg": "jpeg",
    "png": "png",
    "gif": "gif",
    "pdf": "pdf",
    "zip": "zip",
    "exe": "pe", "dll": "pe", "com": "pe",
    "wav": "wav",
}


def classify_family(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return _FAMILY_BY_EXT.get(ext, "binary_fallback")


# Coarse buckets used only for the human-readable console summary
# (master task section 5 example: HTML / JPEG / GIF / CSS-JS / PDF / ZIP / EXE-DLL / Other).
_CONSOLE_BUCKETS = [
    ("HTML", {"html", "htm"}),
    ("JPEG", {"jpg", "jpeg"}),
    ("GIF", {"gif"}),
    ("CSS/JS", {"css", "js", "mjs"}),
    ("PDF", {"pdf"}),
    ("ZIP", {"zip"}),
    ("EXE/DLL", {"exe", "dll", "com"}),
]


def console_bucket(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    for label, members in _CONSOLE_BUCKETS:
        if ext in members:
            return label
    return "Other"


# --- Major MIME families (mandatory-representation set) -----------------
# Every generated workload must retain at least one structurally-valid file
# for each of these labels *if the source corpus contains that extension at
# all*. EXE and DLL are tracked as separate labels (both real, distinct
# populations in Workload04) even though they share one MIME string and one
# padding "family" (pe). Order defines report/table column order.
MAJOR_FAMILIES = [
    ("HTML", {"html", "htm"}, "html", "text/html"),
    ("CSS", {"css"}, "css", "text/css"),
    ("JS", {"js", "mjs"}, "js", "application/javascript"),
    ("JPEG", {"jpg", "jpeg"}, "jpeg", "image/jpeg"),
    ("GIF", {"gif"}, "gif", "image/gif"),
    ("PNG", {"png"}, "png", "image/png"),
    ("PDF", {"pdf"}, "pdf", "application/pdf"),
    ("ZIP", {"zip"}, "zip", "application/zip"),
    ("EXE", {"exe"}, "pe", "application/x-msdos-program"),
    ("DLL", {"dll"}, "pe", "application/x-msdos-program"),
]
MAJOR_FAMILY_LABELS = [m[0] for m in MAJOR_FAMILIES]
MAJOR_FAMILY_EXTS = {e for m in MAJOR_FAMILIES for e in m[1]}


def major_family_label(ext: str) -> Optional[str]:
    ext = ext.lower().lstrip(".")
    for label, exts, _fam, _mime in MAJOR_FAMILIES:
        if ext in exts:
            return label
    return None


# --- Reproducible filler text --------------------------------------------
# Restricted to [a-z, space, newline] so it can never contain a comment
# terminator substring ("--", "*/", "-->") regardless of content length.
_FILLER_ALPHABET = string.ascii_lowercase + "     \n"
_FILLER_ALPHABET_BYTES = _FILLER_ALPHABET.encode("ascii")
# 256-entry translation table so raw random bytes (from the C-accelerated
# randbytes()) can be remapped into our safe alphabet via bytes.translate(),
# instead of random.Random.choices() -- choices() is pure-Python per-element
# and becomes the dominant cost once a single class's padding reaches the
# hundreds of MB (as HTML/CSS/JS now can, post the major-family-diversity
# fix, since a class's per-file target can be up to class_size_ratio_max
# times the overall target).
_FILLER_TRANSLATE_TABLE = bytes(_FILLER_ALPHABET_BYTES[b % len(_FILLER_ALPHABET_BYTES)] for b in range(256))


def make_filler_text(rng: random.Random, length: int, line_width: int = 96) -> bytes:
    if length <= 0:
        return b""
    body = rng.randbytes(length).translate(_FILLER_TRANSLATE_TABLE)
    if line_width > 0 and length > line_width:
        body = b"\n".join(body[i:i + line_width] for i in range(0, len(body), line_width + 1))
    if len(body) > length:
        body = body[:length]
    elif len(body) < length:
        body += b"a" * (length - len(body))
    return body


def make_filler_bytes(rng: random.Random, length: int) -> bytes:
    """Arbitrary binary filler (safe inside length-prefixed containers)."""
    if length <= 0:
        return b""
    # random.Random.randbytes() is implemented in C and is orders of magnitude
    # faster than a per-byte Python loop for multi-MB payloads.
    return rng.randbytes(length)


# --- Tolerances / defaults (master task sections 7, 10, 15, 16) ---------

DEFAULT_SIZE_TOLERANCE = 0.05
DEFAULT_MIME_TOLERANCE = 0.03
DEFAULT_RETRY_LIMIT = 10
DEFAULT_SEED = 42
DEFAULT_SOFT_CORPUS_BUDGET_BYTES = 300 * 1024 * 1024
DEFAULT_MIN_UNIQUE_TOTAL = 6
DEFAULT_MAX_UNIQUE_FILES = 600
DEFAULT_MAX_SINGLE_FILE_BYTES = 512 * 1024 * 1024

# --- Diversity-preservation / anti-distortion knobs ----------------------
# 2026-09-19 master-task rework: request-weighted MIME/extension/family shares
# must track the GOLDEN BASELINE (10K Workload04) share as closely as
# practical, not be pushed up to an arbitrary floor. The previous 1% floor
# (kept a present-but-tiny class from being statistical noise) is exactly the
# kind of distortion the new tolerance model forbids: it made PDF/CSS/PNG/ZIP/
# EXE generate at ~1% against a true source share as low as 0.03%. The floor
# is now 0.0 (a no-op) by default; minimum representation (>=1 resource-list
# line) falls out naturally from total_line_budget instead -- see
# DEFAULT_TOTAL_LINE_BUDGET below.
DEFAULT_MIN_MAJOR_SHARE = 0.0
# Bounds on how far a class's per-file target size may deviate from the
# overall target average, relative to that class's true size ratio in the
# source corpus. Prevents both a) classes collapsing to near-zero size and
# b) a single class (e.g. ZIP/PDF, naturally ~20-30x the corpus average)
# from requesting an absurdly large per-file size that forces heavy capping.
DEFAULT_CLASS_SIZE_RATIO_MIN = 0.3
DEFAULT_CLASS_SIZE_RATIO_MAX = 3.0
# How many extra (non-major, long-tail) extensions to keep as a representative
# "Other" bucket, beyond the mandatory major families.
DEFAULT_OTHER_BUCKET_FILES = 8
# DEPRECATED 2026-09-19: request counts are no longer scaled to a "line
# budget" at all -- every category's resource-list line count is now the
# golden baseline's own EXACT integer request weight (e.g. all ~10000
# entries of the real Workload04 baseline, not a reduced ~3000). This
# constant is kept only so old call sites/CLI flags that still pass
# `total_line_budget=...` don't break; the value is accepted but ignored.
DEFAULT_TOTAL_LINE_BUDGET = 3000

# --- Validator tolerance model (legacy; superseded by exact-count matching)
# 2026-09-19: request distribution PASS/FAIL is now decided by EXACT integer
# count equality against the golden baseline (generated_count ==
# baseline_count), per the master task's explicit rejection of "approximately
# preserved". These pp-based constants are kept for any remaining callers
# that want a soft tolerance for REPORTING purposes only (distribution.
# tolerance_for_share) -- they are no longer the pass/fail gate in
# validator.py or distribution.ratio_audit_rows().
DEFAULT_MINOR_CLASS_SHARE_THRESHOLD = 0.01   # baseline share boundary: <1% is "minor"
DEFAULT_MAJOR_CLASS_ABS_TOLERANCE_PP = 0.005  # ±0.5 percentage points for baseline share >= 1%
DEFAULT_MINOR_CLASS_ABS_TOLERANCE_PP = 0.001  # ±0.1 percentage points for baseline share < 1% (incl. AV-test)
# Validator: any binary_fallback weight attributed to a major family above
# this fraction of that family's own weight fails validation.
DEFAULT_MAX_MAJOR_FALLBACK_FRACTION = 0.0


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:.2f}{unit}"
        n /= 1024.0
    return f"{n:.2f}PiB"
