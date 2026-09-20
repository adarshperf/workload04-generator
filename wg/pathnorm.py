"""Whitespace-free path normalization + deterministic collision handling.

Single source of truth for the URI/physical-path pipeline, so `generate.py`
(writes files) and `validate.py` (independently re-verifies from the
manifest) never duplicate/diverge on the algorithm.

Pipeline for every baseline URI, e.g.
'/workload04/gfx/gfx2/Kopie%20(3)%20von%20b1.jpg':

  1. rel_from_uri       -> 'gfx/gfx2/Kopie%20(3)%20von%20b1.jpg'   (encoded, root stripped)
  2. decoded_rel_from_uri -> 'gfx/gfx2/Kopie (3) von b1.jpg'         (percent-decoded)
  3. normalized_rel_from_decoded -> 'gfx/gfx2/Kopie_(3)_von_b1.jpg'  (whitespace -> '_')
  4. final_rel          -> same as (3), unless a collision with another
                            baseline URI's normalized_rel requires a
                            deterministic hash suffix before the extension.

The runtime URI and the physical filesystem path are now the SAME string
(final_rel), just with/without the '/workload04-<TIER>/' root prefix --
there is no percent-encoding left to preserve, because the only characters
ever percent-encoded in this corpus are whitespace, and whitespace is now
represented as '_' instead of being encoded at all.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import common as wg_common


def rel_from_uri(uri: str) -> str:
    """'/workload04/gfx/gfx1/f14.gif' -> 'gfx/gfx1/f14.gif' (strip the first
    path segment, whatever it's called). Stays percent-ENCODED."""
    parts = uri.lstrip("/").split("/", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def decoded_rel_from_uri(uri: str) -> str:
    return wg_common.decode_relpath_for_filesystem(rel_from_uri(uri))


def normalized_rel_from_decoded(decoded_rel: str) -> str:
    return wg_common.normalize_relpath_no_whitespace(decoded_rel)


def _apply_collision_suffix(rel: str, seed: int, uri: str) -> str:
    """Deterministic, reproducible, collision-safe suffix: a short hash of
    (seed, original baseline URI) inserted before the extension, e.g.
    'gfx/gfx2/a_b.jpg' -> 'gfx/gfx2/a_b__1a2b3c4d.jpg'. Stable for the same
    baseline + target + seed (target is implicit: seed is per-tier)."""
    if "/" in rel:
        dirpart, base = rel.rsplit("/", 1)
        dirpart += "/"
    else:
        dirpart, base = "", rel
    if "." in base:
        stem, ext = base.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = base, ""
    digest = hashlib.sha256(f"{seed}:{uri}".encode("utf-8")).hexdigest()[:8]
    return f"{dirpart}{stem}__{digest}{ext}"


@dataclass
class NormalizedPath:
    uri: str
    decoded_rel: str
    normalized_rel: str                # whitespace-free, BEFORE collision suffix
    final_rel: str                     # actually used on disk / in the runtime URI
    collision_suffix: Optional[str] = None   # the full suffixed final_rel, if a collision occurred


def build_normalized_paths(uris: List[str], seed: int) -> Dict[str, NormalizedPath]:
    """Deterministic, reproducible whitespace-normalization + collision
    resolution over an ORDERED list of baseline URIs.

    Collision rule: if two or more URIs normalize to the same relative
    path, the one whose decoded (pre-normalization) path is ALREADY
    identical to the normalized path keeps the clean, un-suffixed name;
    every other member of that collision group gets a deterministic hash
    suffix. If none of the colliding members is already clean (all needed
    normalization), the first one (by input order) keeps the clean name and
    the rest are suffixed. Order only affects which member is "clean" --
    never correctness (every member always gets a unique final_rel).
    """
    prepared = []
    for uri in uris:
        decoded_rel = decoded_rel_from_uri(uri)
        normalized_rel = normalized_rel_from_decoded(decoded_rel)
        prepared.append((uri, decoded_rel, normalized_rel))

    groups: Dict[str, List[int]] = {}
    for idx, (_, _, normalized_rel) in enumerate(prepared):
        groups.setdefault(normalized_rel, []).append(idx)

    final_rel_by_idx: List[Optional[str]] = [None] * len(prepared)
    suffix_by_idx: List[Optional[str]] = [None] * len(prepared)

    for normalized_rel, idxs in groups.items():
        if len(idxs) == 1:
            final_rel_by_idx[idxs[0]] = normalized_rel
            continue
        unchanged = [i for i in idxs if prepared[i][1] == normalized_rel]
        clean_idx = unchanged[0] if unchanged else idxs[0]
        for i in idxs:
            if i == clean_idx:
                final_rel_by_idx[i] = normalized_rel
            else:
                uri = prepared[i][0]
                suffixed = _apply_collision_suffix(normalized_rel, seed, uri)
                final_rel_by_idx[i] = suffixed
                suffix_by_idx[i] = suffixed

    results: Dict[str, NormalizedPath] = {}
    for i, (uri, decoded_rel, normalized_rel) in enumerate(prepared):
        results[uri] = NormalizedPath(
            uri=uri, decoded_rel=decoded_rel, normalized_rel=normalized_rel,
            final_rel=final_rel_by_idx[i], collision_suffix=suffix_by_idx[i],
        )
    return results
