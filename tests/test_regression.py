"""Regression tests for workload04_generator (spec section 47/48).

Requires a frozen baseline (`workload04_generator.py init-baseline --yes`)
to already exist at the default baseline dir. Tests that need a real
generated tier reuse a single small (100KiB) generation across the module
(expensive: ~10,000 physical files) rather than regenerating per-test.
"""
from __future__ import annotations

import csv
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))

from wg import common as wg_common
from wg import baselinefreeze
from wg import bundled as wg_bundled
from wg import pool as wg_pool
from wg import generate as wg_generate
from wg import validate as wg_validate
from wg import sizeaudit
from wg import pathnorm
from wg import reportio
from wg import storage as wg_storage
from wg import safety as wg_safety
from wmime import common as wmime_common


@pytest.fixture(scope="session", autouse=True)
def _portable_workload_roots(tmp_path_factory):
    """Session-scoped, user-writable substitutes for the two /opt-rooted
    production defaults (default source corpus + generated-tier output
    root), so the WHOLE suite never depends on /opt/workload04 or
    /opt/generated_workload04 existing or being writable by an
    unprivileged user. Uses the SAME override mechanisms the CLI/wg_bundled
    already support (WORKLOAD04_SOURCE_ROOT / WORKLOAD04_OUTPUT_ROOT env
    vars) -- no production default is changed, and any test that passes
    --output-root/--workload-root explicitly still takes priority over
    this."""
    source_parent = tmp_path_factory.mktemp("workload_source_root")
    output_root = tmp_path_factory.mktemp("workload_output_root")
    # Must not already exist: wg_bundled.ensure_default_workload_available()
    # refuses to extract into a pre-existing-but-unverified directory.
    source_root = source_parent / wg_common.DEFAULT_WORKLOAD_LABEL

    old_source = os.environ.get("WORKLOAD04_SOURCE_ROOT")
    old_output = os.environ.get("WORKLOAD04_OUTPUT_ROOT")
    os.environ["WORKLOAD04_SOURCE_ROOT"] = str(source_root)
    os.environ["WORKLOAD04_OUTPUT_ROOT"] = str(output_root)
    try:
        yield
    finally:
        for var, old in (("WORKLOAD04_SOURCE_ROOT", old_source), ("WORKLOAD04_OUTPUT_ROOT", old_output)):
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old


@pytest.fixture(scope="module")
def baseline():
    try:
        return baselinefreeze.FrozenBaseline(wg_common.DEFAULT_BASELINE_DIR)
    except FileNotFoundError:
        pytest.skip("baseline not frozen -- run `init-baseline --yes` first")


@pytest.fixture(scope="module")
def small_tier(baseline):
    """Generate one small real tier once, reused by every test that needs it."""
    tier = "100KiB"
    target_bytes = wg_common.parse_size("100KiB")
    out_dir = wg_common.output_dir_for_tier(tier)
    # Always start from a clean output directory: a stale tier from a prior
    # generator version (e.g. pre-whitespace-normalization) can leave behind
    # old-format physical files that the new run does not overwrite (their
    # normalized names differ), inflating the on-disk file count and leaving
    # obsolete filenames (like a literal-space name) present.
    shutil.rmtree(out_dir, ignore_errors=True)
    plan = wg_pool.build_plan(baseline, target_bytes, seed=42)
    result = wg_generate.generate_workload(baseline, plan, tier=tier, seed=42, verbose=False)
    manifest = wg_generate.write_reproducibility_manifest(baseline, plan, result, target_text="100KiB")
    report = wg_validate.validate(baseline, plan, result, manifest)
    reportio.write_target_outputs(result, manifest, report)
    return baseline, plan, result, manifest, report


@pytest.fixture(scope="module")
def extracted_bundled_workload04(tmp_path_factory):
    """Extract the bundled workload04.zip once (expensive: 10,000 files),
    reused by every test that needs a real extracted-corpus fixture."""
    dest = tmp_path_factory.mktemp("bundled_extract")
    count = wg_bundled.safe_extract_zip(wg_bundled.bundled_zip_path(), dest)
    return dest, count


# --- 1. baseline payload count = 10,000 ----------------------------------
def test_baseline_physical_file_count(baseline):
    assert baseline.header["physical_file_count_on_disk"] == 10000


def test_baseline_logical_entry_count(baseline):
    assert baseline.total_entries == 10000


# --- 2/3. generated payload count / unique paths = 10,000 ---------------
def test_generated_payload_count(small_tier):
    baseline, plan, result, manifest, report = small_tier
    assert report["physical_payload_files"]["generated"] == baseline.total_entries
    assert report["physical_payload_files"]["unique_generated_relpaths"] == baseline.total_entries


# --- 4. logical request count preserved ----------------------------------
def test_logical_request_count_preserved(small_tier):
    baseline, plan, result, manifest, report = small_tier
    assert len(result.resource_lines) == baseline.total_entries


# --- 5. every resource entry resolves ------------------------------------
def test_every_resource_entry_resolves(small_tier):
    baseline, plan, result, manifest, report = small_tier
    for gf in result.files:
        assert (result.output_dir / gf.rel_path).is_file()


# --- 6. exactly 3 EICAR entries -------------------------------------------
def test_eicar_count_exact(small_tier):
    baseline, plan, result, manifest, report = small_tier
    assert report["eicar"]["generated_count"] == baseline.eicar_count == 3


# --- 7/8. MIME + extension distribution preserved -------------------------
def test_mime_and_extension_distribution(small_tier):
    baseline, plan, result, manifest, report = small_tier
    mime_status = next(c for c in report["checks"] if c["name"] == "mime_distribution")
    ext_status = next(c for c in report["checks"] if c["name"] == "extension_distribution")
    assert mime_status["status"] == "PASS"
    assert ext_status["status"] == "PASS"


# --- 9. target root is correct / no /workload04/ leftover ----------------
def test_target_root_correct(small_tier):
    baseline, plan, result, manifest, report = small_tier
    prefix = wg_common.uri_root_prefix("100KiB")
    assert all(line.startswith(prefix) for line in result.resource_lines)
    assert not any("/workload04/" in line for line in result.resource_lines)


# --- 11. URL encoding preserved -------------------------------------------
def test_url_encoding_preserved(small_tier):
    baseline, plan, result, manifest, report = small_tier
    status = next(c for c in report["checks"] if c["name"] == "url_encoding")
    assert status["status"] == "PASS"


# --- 12. baseline remains untouched (structural guarantee) ---------------
def test_baseline_not_modified(baseline):
    # generate_workload/pool never open baseline.source_root for writing --
    # verified by code inspection (see validate.py check
    # 'baseline_not_modified_by_design'); this test just re-asserts the
    # frozen manifest's own checksum still matches the file on disk, using
    # the RESOLVED (per-machine) resource-list path -- not the historical
    # header['resource_list_path'], which may be a stale/foreign-platform
    # path from whichever machine originally froze the baseline.
    actual = baselinefreeze._sha256_file(baseline.resource_list_path)
    assert actual == baseline.header["resource_list_sha256"]


# --- 13. no resource collapse / 14. no duplicate generated paths ---------
def test_no_collapse_no_duplicates(small_tier):
    baseline, plan, result, manifest, report = small_tier
    relpaths = [gf.rel_path for gf in result.files]
    assert len(relpaths) == len(set(relpaths)) == baseline.total_entries


# --- 15. dry-run catches insufficient disk --------------------------------
def test_dry_run_catches_insufficient_disk(baseline):
    plan = wg_pool.build_plan(baseline, wg_common.parse_size("1MiB"), seed=42)
    text = reportio.write_dry_run_report(
        plan, tier="1MiB", target_text="1MiB", baseline_total_entries=baseline.total_entries,
        disk_free_bytes=1024,  # 1 KiB free -- always insufficient
    )
    assert "NOT FEASIBLE" in text


# --- 16. deterministic generation works -----------------------------------
def test_deterministic_generation(baseline):
    plan_a = wg_pool.build_plan(baseline, wg_common.parse_size("100KiB"), seed=7)
    plan_b = wg_pool.build_plan(baseline, wg_common.parse_size("100KiB"), seed=7)
    sizes_a = [r.target_size_bytes for r in plan_a.records]
    sizes_b = [r.target_size_bytes for r in plan_b.records]
    assert sizes_a == sizes_b


# --- 18. 100KiB end-to-end sample passes ----------------------------------
def test_100kib_end_to_end_overall_pass(small_tier):
    baseline, plan, result, manifest, report = small_tier
    assert report["overall"] == "PASS", report


def test_manifest_json_written(small_tier):
    baseline, plan, result, manifest, report = small_tier
    assert (result.output_dir / "manifest.json").is_file()
    data = json.loads((result.output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert data["physical_file_count"] == baseline.total_entries


# --- percent-decoded filesystem resolution (2026-09-19 resolver fix) -----
def test_percent_decoded_resolution(baseline):
    resolved = wg_common.resolve_uri_to_path(
        baseline.source_root, "/workload04/gfx/gfx2/Kopie%20(3)%20von%20b1.jpg"
    )
    assert resolved.is_file()
    assert resolved.name == "Kopie (3) von b1.jpg"


def test_resolve_uri_to_path_blocks_traversal(baseline):
    with pytest.raises(ValueError):
        wg_common.resolve_uri_to_path(baseline.source_root, "/workload04/../../etc/passwd")


# --- generic (not hard-coded %20) percent-decoding for filesystem names ---
def test_decode_relpath_for_filesystem_handles_arbitrary_percent_encoding():
    assert wg_common.decode_relpath_for_filesystem("Kopie%20(3)%20von%20b1.jpg") == "Kopie (3) von b1.jpg"
    assert wg_common.decode_relpath_for_filesystem("a%2Bb%26c.txt") == "a+b&c.txt"
    assert wg_common.decode_relpath_for_filesystem("gfx/gfx1/plain.gif") == "gfx/gfx1/plain.gif"
    assert wg_common.decode_relpath_for_filesystem("UPPER/MixedCase.JPG") == "UPPER/MixedCase.JPG"


def test_decode_relpath_for_filesystem_blocks_traversal():
    with pytest.raises(ValueError):
        wg_common.decode_relpath_for_filesystem("../../etc/passwd")
    with pytest.raises(ValueError):
        wg_common.decode_relpath_for_filesystem("gfx/%2e%2e/passwd")


# --- zero missing baseline entries / zero synthetic fallback -------------
def test_baseline_has_zero_missing_entries(baseline):
    assert baseline.header["missing_files"] == 0


def test_no_synthetic_fallback_in_clean_baseline(small_tier):
    baseline, plan, result, manifest, report = small_tier
    assert plan.missing_seed_count == 0
    assert all(gf.has_source_seed for gf in result.files)
    status = next(c for c in report["checks"] if c["name"] == "no_synthetic_fallback")
    assert status["status"] == "PASS"


def test_generate_workload_refuses_synthetic_fallback_by_default(baseline):
    plan = wg_pool.build_plan(baseline, wg_common.parse_size("100KiB"), seed=1)
    plan.records[0].has_source_seed = False  # simulate a dirty baseline without needing one
    plan.missing_seed_count = 1
    with pytest.raises(RuntimeError):
        wg_generate.generate_workload(baseline, plan, tier="100KiB", seed=1)


# --- request-weighted average via the independent size audit, 3% tolerance
def test_size_audit_matches_target_within_tolerance(small_tier):
    baseline, plan, result, manifest, report = small_tier
    audit_result = sizeaudit.audit("100KiB", result.output_dir, result.target_bytes)
    assert audit_result.logical_request_count == baseline.total_entries
    assert not audit_result.missing_references
    assert abs(audit_result.error_pct) <= wg_common.DEFAULT_SIZE_TOLERANCE_PCT * 100.0


# --- 2026-09-20 whitespace-normalization spec: runtime URI and physical
# path are now the SAME whitespace-free string (no %20, no literal space)
def test_runtime_uri_has_no_whitespace_or_percent20(small_tier):
    baseline, plan, result, manifest, report = small_tier
    kopie_lines = [ln for ln in result.resource_lines if "Kopie" in ln]
    assert kopie_lines, "expected at least one 'Kopie ...'-derived entry in the resource list"
    assert not any(" " in ln for ln in kopie_lines), "runtime URI must never contain a literal space"
    assert not any("%20" in ln for ln in kopie_lines), "runtime URI must never contain '%20'"
    assert any("Kopie_(3)_von_b1.jpg" in ln for ln in kopie_lines)


# --- workload-resources.txt is one URI/path field per line, no MIME column
def test_resource_list_is_single_field_per_line(small_tier):
    baseline, plan, result, manifest, report = small_tier
    resources_path = result.output_dir / "workload-resources.txt"
    lines = [ln for ln in resources_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 10000
    assert not any("\t" in ln or " " in ln for ln in lines), (
        "workload-resources.txt must contain exactly one URI/path field per line, "
        "matching the golden baseline's own format -- no MIME/content-type column"
    )
    status = next(c for c in report["checks"] if c["name"] == "resource_list_one_field_per_line")
    assert status["status"] == "PASS"


# --- 2026-09-20 fix: physical filename and runtime URI are BOTH normalized
# (whitespace -> '_'), and refer to the SAME path once the root is stripped
def test_physical_filename_and_runtime_uri_normalized(small_tier):
    baseline, plan, result, manifest, report = small_tier
    gf = next(gf for gf in result.files if gf.uri == "/workload04/gfx/gfx2/Kopie%20(3)%20von%20b1.jpg")
    assert gf.rel_path == "gfx/gfx2/Kopie_(3)_von_b1.jpg"
    assert gf.rewritten_uri == "/workload04-100KiB/gfx/gfx2/Kopie_(3)_von_b1.jpg"
    assert gf.rewritten_uri == f"/workload04-100KiB/{gf.rel_path}"
    assert (result.output_dir / "gfx" / "gfx2" / "Kopie_(3)_von_b1.jpg").is_file()
    assert not (result.output_dir / "gfx" / "gfx2" / "Kopie (3) von b1.jpg").is_file()
    assert not (result.output_dir / "gfx" / "gfx2" / "Kopie%20(3)%20von%20b1.jpg").is_file()
    assert "/workload04-100KiB/gfx/gfx2/Kopie_(3)_von_b1.jpg" in result.resource_lines


def test_no_percent_encoding_in_any_physical_filename(small_tier):
    baseline, plan, result, manifest, report = small_tier
    import re
    pct_re = re.compile(r"%[0-9A-Fa-f]{2}")
    encoded_names = [gf.rel_path for gf in result.files if pct_re.search(gf.rel_path)]
    assert encoded_names == []
    status = next(c for c in report["checks"] if c["name"] == "filesystem_filename_decoded")
    assert status["status"] == "PASS"


# --- no whitespace anywhere in generated physical paths or runtime URIs ---
def test_no_whitespace_anywhere_in_generated_tier(small_tier):
    baseline, plan, result, manifest, report = small_tier
    import re
    ws_re = re.compile(r"\s")
    bad_paths = [gf.rel_path for gf in result.files if ws_re.search(gf.rel_path)]
    bad_uris = [gf.rewritten_uri for gf in result.files if ws_re.search(gf.rewritten_uri)]
    assert bad_paths == []
    assert bad_uris == []
    for name in ("no_whitespace_in_physical_paths", "no_whitespace_in_runtime_uris",
                 "no_percent20_from_whitespace", "directory_structure_preserved"):
        status = next(c for c in report["checks"] if c["name"] == name)
        assert status["status"] == "PASS", (name, status)


# --- exactly 10,000 unique generated payload paths after normalization ----
def test_exactly_10000_unique_paths_after_normalization(small_tier):
    baseline, plan, result, manifest, report = small_tier
    rel_paths = [gf.rel_path for gf in result.files]
    assert len(rel_paths) == len(set(rel_paths)) == 10000


# --- deterministic whitespace normalization + collision handling (unit) ---
def test_pathnorm_directory_and_filename_whitespace_normalized():
    uris = ["/workload04/my folder/a b.jpg"]
    result = pathnorm.build_normalized_paths(uris, seed=1)
    assert result[uris[0]].final_rel == "my_folder/a_b.jpg"


def test_pathnorm_deterministic_collision_handling():
    # "a b.jpg" normalizes to "a_b.jpg", which collides with an
    # already-clean "a_b.jpg" at the same relative position.
    uris = ["/workload04/gfx/a_b.jpg", "/workload04/gfx/a b.jpg"]
    result_a = pathnorm.build_normalized_paths(uris, seed=42)
    # The already-clean one keeps the clean name; the other gets a suffix.
    assert result_a["/workload04/gfx/a_b.jpg"].final_rel == "gfx/a_b.jpg"
    assert result_a["/workload04/gfx/a_b.jpg"].collision_suffix is None
    suffixed = result_a["/workload04/gfx/a b.jpg"].final_rel
    assert suffixed != "gfx/a_b.jpg"
    assert suffixed.startswith("gfx/a_b__")
    assert suffixed.endswith(".jpg")
    assert result_a["/workload04/gfx/a b.jpg"].collision_suffix == suffixed
    # Reproducible: same seed + same input -> identical output.
    result_b = pathnorm.build_normalized_paths(uris, seed=42)
    assert result_a["/workload04/gfx/a b.jpg"].final_rel == result_b["/workload04/gfx/a b.jpg"].final_rel
    # Different seed -> different (but still valid/unique) suffix.
    result_c = pathnorm.build_normalized_paths(uris, seed=99)
    assert result_c["/workload04/gfx/a b.jpg"].final_rel != suffixed
    # Collision-safety: final paths are always unique regardless of seed.
    assert len({v.final_rel for v in result_a.values()}) == 2
    assert len({v.final_rel for v in result_c.values()}) == 2


def test_pathnorm_collision_when_all_members_need_normalization():
    # "a  b.jpg" (2 spaces) and "a__b.jpg" would only collide if BOTH
    # already contain literal underscores in place of whitespace; use two
    # distinct whitespace variants that normalize identically instead.
    uris = ["/workload04/gfx/a\tb.jpg", "/workload04/gfx/a b.jpg"]
    result = pathnorm.build_normalized_paths(uris, seed=7)
    finals = {v.final_rel for v in result.values()}
    assert len(finals) == 2, "both records must end up with distinct final paths"


# --- path traversal protection still works after normalization -----------
def test_pathnorm_blocks_traversal():
    with pytest.raises(ValueError):
        pathnorm.decoded_rel_from_uri("/workload04/../../etc/passwd")


# --- Portability / Linux-safety / storage-preflight tests -----------------

def test_no_hardcoded_windows_paths_in_source():
    """Portability audit (spec section 27): the tool's own source (NOT the
    vendored wmime/ package, and NOT this test file, which legitimately
    contains these strings as literals) must never reference a Windows
    drive letter or a developer-specific path."""
    forbidden = ["C:\\\\", "C:/Users", "AdarshChouksey", "c:\\\\", "c:/Users"]
    src_files = list(TOOL_ROOT.glob("*.py")) + list((TOOL_ROOT / "wg").glob("*.py"))
    offenders = []
    for f in src_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for bad in forbidden:
            if bad in text:
                offenders.append((str(f), bad))
    assert not offenders, f"Hard-coded Windows/user-specific paths found: {offenders}"


def test_output_root_resolution_priority(tmp_path, monkeypatch):
    """--output-root (set_generated_root) > WORKLOAD04_OUTPUT_ROOT env var >
    platform default."""
    try:
        wg_common.set_generated_root(None)
        monkeypatch.delenv("WORKLOAD04_OUTPUT_ROOT", raising=False)
        default_root = wg_common.get_generated_root()
        assert isinstance(default_root, Path)

        env_root = tmp_path / "envroot"
        monkeypatch.setenv("WORKLOAD04_OUTPUT_ROOT", str(env_root))
        assert wg_common.get_generated_root() == env_root.resolve()

        cli_root = tmp_path / "cliroot"
        wg_common.set_generated_root(cli_root)
        assert wg_common.get_generated_root() == cli_root.resolve()
    finally:
        wg_common.set_generated_root(None)


def test_permission_check_blocks_on_unwritable_target(tmp_path):
    """A parent path component that is a regular file (not a directory)
    makes mkdir() fail deterministically on every platform -- used here to
    simulate a permission/ownership failure without needing root/chmod
    tricks that behave differently on Windows vs Linux."""
    blocker = tmp_path / "blocker_file"
    blocker.write_text("x", encoding="utf-8")
    bad_root = blocker / "generated_workload04"
    msg = wg_storage.check_output_root_writable(bad_root)
    assert msg is not None
    assert "Cannot create output root" in msg


def test_storage_preflight_detects_insufficient_space(baseline, tmp_path, monkeypatch):
    plan = wg_pool.build_plan(baseline, wg_common.parse_size("1GiB"), seed=42)
    out_dir = tmp_path / "workload04-1GiB"

    class TinyUsage:
        total = 10_000_000_000
        free = 1024  # nowhere near enough for a 10,000 x ~1GiB plan

    monkeypatch.setattr(wg_storage.shutil, "disk_usage", lambda p: TinyUsage())
    result = wg_storage.run_preflight(plan, tier="1GiB", requested_avg_text="1GiB", out_dir=out_dir)
    assert not result.feasible
    assert not result.feasible_bytes
    assert not out_dir.exists()


def test_storage_preflight_passes_with_ample_space(baseline, tmp_path, monkeypatch):
    # 10KiB is comfortably above the corpus's format-preserving content-size
    # floor (~4165B for the real workload04 baseline) so this test still
    # isolates pure disk-space feasibility, as originally intended.
    plan = wg_pool.build_plan(baseline, wg_common.parse_size("10KiB"), seed=42)
    out_dir = tmp_path / "workload04-10KiB"

    class HugeUsage:
        total = 10 ** 15
        free = 10 ** 14

    monkeypatch.setattr(wg_storage.shutil, "disk_usage", lambda p: HugeUsage())
    result = wg_storage.run_preflight(plan, tier="10KiB", requested_avg_text="10KiB", out_dir=out_dir)
    assert result.feasible


def test_content_size_floor_detected_for_tiny_target(baseline, tmp_path, monkeypatch):
    """A tiny --avg-size (below the corpus's format-preserving content
    floor) must be reported infeasible for CONTENT reasons even with
    abundant disk space -- disk-space and content-size feasibility are
    independent checks."""
    plan = wg_pool.build_plan(baseline, wg_common.parse_size("0.5KiB"), seed=42)
    out_dir = tmp_path / "workload04-512B"

    class HugeUsage:
        total = 10 ** 15
        free = 10 ** 14

    monkeypatch.setattr(wg_storage.shutil, "disk_usage", lambda p: HugeUsage())
    result = wg_storage.run_preflight(plan, tier="512B", requested_avg_text="0.5KiB", out_dir=out_dir)

    assert result.feasible_bytes
    assert not result.feasible_content
    assert not result.feasible
    assert result.content_floor_avg_bytes > plan.target_bytes
    assert result.content_floor_record_count > 0
    assert "html" in result.content_floor_families
    assert not out_dir.exists()


def test_content_size_floor_is_a_lower_bound(baseline):
    """The floor is documented/used as a LOWER bound, not an exact-minimum
    claim: it covers only a subset of records/families."""
    plan = wg_pool.build_plan(baseline, wg_common.parse_size("0.5KiB"), seed=42)
    floor = wg_pool.compute_hard_floor(plan)
    assert 0 < floor["record_count"] < floor["total_records"]
    assert set(floor["per_family_count"]) <= wg_pool.HARD_NO_SHRINK_FAMILIES
    assert floor["floor_avg_bytes"] > wg_common.parse_size("0.5KiB")


def test_avg_size_below_content_floor_rejected_by_cli(baseline, tmp_path):
    """--avg-size 0.5KiB must be rejected at preflight for Workload04,
    before any staging directory or payload file is created."""
    import workload04_generator as cli
    out_root = tmp_path / "tiny_content_root"
    try:
        rc = cli.main(["--avg-size", "0.5KiB", "--output-root", str(out_root)])
        assert rc != 0
        assert not (out_root / "workload04-512B").exists()
        assert not list(out_root.glob(".workload04-512B.staging.*"))
    finally:
        wg_common.set_generated_root(None)


def test_plateau_detection_helper():
    import workload04_generator as cli
    assert cli._size_convergence_plateaued(1000.0, None) is False
    assert cli._size_convergence_plateaued(1000.0, 1000.5) is True
    assert cli._size_convergence_plateaued(1000.0, 2000.0) is False


def test_dry_run_creates_no_files(baseline, tmp_path):
    """--dry-run must perform the full pre-flight but write ZERO files."""
    import workload04_generator as cli
    out_root = tmp_path / "dryrun_root"
    try:
        rc = cli.main(["--avg-size", "100KiB", "--dry-run", "--output-root", str(out_root)])
        assert rc == 0
        assert not (out_root / "workload04-100KiB").exists()
    finally:
        wg_common.set_generated_root(None)


def test_insufficient_storage_aborts_before_generation(baseline, tmp_path, monkeypatch):
    """A real (non-dry-run) generation must abort with ZERO payload files
    written when the pre-flight determines storage is insufficient --
    never partially generate and then fail."""
    import workload04_generator as cli
    out_root = tmp_path / "tinyroot"

    class TinyUsage:
        total = 10_000_000
        free = 1024

    monkeypatch.setattr(wg_storage.shutil, "disk_usage", lambda p: TinyUsage())
    try:
        rc = cli.main(["--avg-size", "1MiB", "--output-root", str(out_root)])
        assert rc != 0
        assert not (out_root / "workload04-1MiB").exists()
    finally:
        wg_common.set_generated_root(None)


def test_max_avg_size_computation_sane(baseline, tmp_path):
    result = wg_storage.compute_max_avg_size(baseline, tmp_path)
    assert result["max_safe_avg_bytes"] >= 1
    assert result["max_safe_avg_bytes"] <= result["max_theoretical_avg_bytes"]
    report = wg_storage.render_max_avg_size_report(result, tmp_path)
    assert "Maximum SAFE average object size" in report


def test_staged_generation_cleans_up_on_failure(tmp_path):
    out_dir = tmp_path / "workload04-TESTFAIL"
    staging_ref = {}
    with pytest.raises(RuntimeError):
        with wg_safety.staged_generation(out_dir) as (staging, commit):
            staging_ref["path"] = staging
            assert staging.exists()
            (staging / "partial.txt").write_text("x", encoding="utf-8")
            raise RuntimeError("simulated mid-generation failure")
    assert not out_dir.exists()
    assert not staging_ref["path"].exists()


def test_staged_generation_commits_atomically(tmp_path):
    out_dir = tmp_path / "workload04-TESTOK"
    with wg_safety.staged_generation(out_dir) as (staging, commit):
        (staging / "file.txt").write_text("hello", encoding="utf-8")
        commit()
    assert out_dir.is_dir()
    assert (out_dir / "file.txt").read_text(encoding="utf-8") == "hello"


def test_staged_generation_preserves_old_tier_until_commit(tmp_path):
    """--force semantics: the previous valid tier must still exist if the
    staged replacement is never committed (e.g. validation failed)."""
    out_dir = tmp_path / "workload04-TESTFORCE"
    out_dir.mkdir()
    (out_dir / "old.txt").write_text("old", encoding="utf-8")
    with wg_safety.staged_generation(out_dir) as (staging, commit):
        (staging / "new.txt").write_text("new", encoding="utf-8")
        # commit() intentionally not called -- simulates a failed validation
    assert (out_dir / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (out_dir / "new.txt").exists()


def test_generation_lock_blocks_concurrent(tmp_path):
    root = tmp_path / "lockroot"
    with wg_safety.generation_lock(root, tier="X"):
        with pytest.raises(wg_safety.LockHeldError):
            with wg_safety.generation_lock(root, tier="Y"):
                pass


def test_generation_lock_recovers_stale_lock(tmp_path):
    """A lock left behind by a killed/crashed process must eventually be
    reclaimed, not block generation forever. Uses a backdated mtime so the
    test is deterministic regardless of how reliably this platform can
    check PID liveness (os.kill(pid, 0) semantics differ on Windows)."""
    root = tmp_path / "lockroot2"
    root.mkdir()
    lock_dir = root / ".workload04_generator.lock"
    lock_dir.mkdir()
    (lock_dir / "info.json").write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
    old_time = time.time() - 7 * 3600
    os.utime(lock_dir, (old_time, old_time))
    with wg_safety.generation_lock(root, tier="Z"):
        pass  # must reclaim the stale lock without raising LockHeldError


# --- Standalone-application tests (no Sledgehammer/sibling-repo dependency) --

def test_no_sledgehammer_or_sibling_repo_python_dependency():
    """This project must be importable/runnable with zero dependency on a
    Sledgehammer checkout or a sibling 'mixed_mime_workload' package --
    mentioning Sledgehammer as an OPTIONAL integration in README prose or
    an emitted config-snippet string is fine and expected; a Python import
    or sys.path reference to either is not."""
    forbidden = ["mixed_mime_workload", "import sledgehammer", "from sledgehammer"]
    py_files = (
        list(TOOL_ROOT.glob("*.py"))
        + list((TOOL_ROOT / "wg").glob("*.py"))
        + list((TOOL_ROOT / "wmime").glob("*.py"))
    )
    offenders = []
    for f in py_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for bad in forbidden:
            if bad in text:
                offenders.append((str(f), bad))
    assert not offenders, f"Sledgehammer/sibling-repo Python dependency found: {offenders}"


def test_wmime_is_vendored_locally():
    """The wmime package must be vendored inside this project (not a
    sys.path hack into a sibling checkout) -- see wg/common.py."""
    assert (TOOL_ROOT / "wmime" / "__init__.py").is_file()
    assert (TOOL_ROOT / "wmime" / "formats.py").is_file()
    assert str(wg_common.TOOL_ROOT) == str(TOOL_ROOT)


def test_reference_workloads_integrity(small_tier):
    """The two named reference tiers (100KiB, 1MiB) are a LOCAL,
    gitignored developer-convenience cache (see .gitignore and git
    history: reference_workloads/ has never been tracked) -- not a
    repository asset every checkout is guaranteed to have.

    On a machine where it already exists (e.g. a Windows dev checkout that
    ran real generations), verify it strictly: exactly the two known
    tiers, each with exactly 10,000 payload files matching its own
    manifest -- no partial/extra tiers silently accepted.

    On a fresh checkout where it does not exist yet (e.g. CI/a new clone),
    missing data must NOT be silently accepted as "nothing to check" --
    instead this proves the same deterministic reference-tier generation
    mechanism is correct, by checking the `small_tier` fixture's already-
    real 100KiB tier (produced by the exact same generate_workload() code
    path any reference_workloads/workload04-100KiB would come from)
    against the identical invariants a cached reference tier must satisfy.
    """
    ref_root = TOOL_ROOT / "reference_workloads"
    if ref_root.is_dir():
        tiers = sorted(p.name for p in ref_root.iterdir() if p.is_dir())
        assert tiers == ["workload04-100KiB", "workload04-1MiB"], (
            f"reference_workloads/ must contain exactly the two known tiers, found: {tiers}"
        )
        for tier_dir_name in tiers:
            tier_dir = ref_root / tier_dir_name
            manifest = json.loads((tier_dir / "manifest.json").read_text(encoding="utf-8"))
            assert manifest["physical_file_count"] == 10000
            payload_files = [
                p for p in tier_dir.rglob("*")
                if p.is_file() and p.name not in wg_common.METADATA_FILENAMES
            ]
            assert len(payload_files) == 10000, (
                f"{tier_dir_name}: expected 10000 payload files, found {len(payload_files)}"
            )
        return

    # No local reference_workloads/ cache on this checkout (expected on a
    # fresh clone/CI machine, since it is deliberately gitignored) -- prove
    # the deterministic reference-tier generation mechanism itself instead.
    baseline, plan, result, manifest, report = small_tier
    assert manifest["physical_file_count"] == 10000
    payload_files = [
        p for p in result.output_dir.rglob("*")
        if p.is_file() and p.name not in wg_common.METADATA_FILENAMES
    ]
    assert len(payload_files) == 10000
    assert report["overall"] == "PASS"


def test_baseline_source_root_reachability_helper():
    """FrozenBaseline.source_root_reachable() must reflect actual disk
    state -- used by cmd_generate to refuse silently-degraded generation
    on a machine where init-baseline hasn't been re-run yet locally."""
    class _Fake:
        source_root_reachable = baselinefreeze.FrozenBaseline.source_root_reachable

        def __init__(self, path):
            self.source_root = path

    assert _Fake(Path("/this/path/almost/certainly/does/not/exist/xyz123")).source_root_reachable() is False


# --- Cross-platform baseline path portability (2026-09-24) ---------------

def test_is_foreign_platform_path_detects_windows_path_on_posix(monkeypatch):
    monkeypatch.setattr(wg_common.os, "name", "posix")
    assert wg_common.is_foreign_platform_path("C:\\Users\\someone\\workload04") is True
    assert wg_common.is_foreign_platform_path("C:/Users/someone/workload04") is True
    assert wg_common.is_foreign_platform_path("/home/user/workload04") is False
    assert wg_common.is_foreign_platform_path("relative/path") is False


def test_is_foreign_platform_path_not_triggered_on_windows(monkeypatch):
    monkeypatch.setattr(wg_common.os, "name", "nt")
    assert wg_common.is_foreign_platform_path("C:\\Users\\someone\\workload04") is False


def test_frozen_baseline_resolves_stale_foreign_source_root(tmp_path):
    """A baseline manifest carrying a Windows absolute source_root_path/
    resource_list_path (as if copied from a Windows dev checkout) must
    never be treated as reachable-but-wrong on this machine -- it must
    fall back to this machine's own local corpus for the same label
    (never a hard-coded path, never sudo/chmod), and the fallback must
    resolve to a REAL, valid, usable corpus (not merely "some directory")."""
    frozen_dir = tmp_path / "frozen_copy"
    shutil.copytree(wg_common.DEFAULT_BASELINE_DIR, frozen_dir)
    manifest_path = frozen_dir / "baseline_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bogus_source_root = "C:\\Users\\someone\\sledgehammer\\workload04"
    manifest["source_root_path"] = bogus_source_root
    manifest["resource_list_path"] = "C:\\Users\\someone\\sledgehammer\\configs\\workload04-resources.txt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    fb = baselinefreeze.FrozenBaseline(frozen_dir)
    assert fb.source_root_reachable()
    assert fb.source_root != Path(bogus_source_root)
    assert fb.source_root.is_dir()
    assert fb.resource_list_path.is_file()
    resolved = wg_common.resolve_uri_to_path(
        fb.source_root, "/workload04/gfx/gfx2/Kopie%20(3)%20von%20b1.jpg"
    )
    assert resolved.is_file()
    assert resolved.name == "Kopie (3) von b1.jpg"


def test_no_opt_dependency_in_test_session():
    """The whole test session must resolve the default source corpus and
    generated-output root away from /opt, without any /opt directory
    needing to exist or be writable."""
    wg_common.set_generated_root(None)
    assert "WORKLOAD04_SOURCE_ROOT" in os.environ
    assert "WORKLOAD04_OUTPUT_ROOT" in os.environ
    assert not str(wg_bundled.default_workload_root("workload04")).startswith("/opt")
    assert not str(wg_common.get_generated_root()).startswith("/opt")


# --- Python 3.8 compatibility: random.Random.randbytes() was added in 3.9;
# make_filler_text()/make_filler_bytes() must keep working (deterministically,
# byte-for-byte matching randbytes()'s documented semantics) on 3.8+ ---------

def test_randbytes_compat_matches_real_randbytes_for_same_seed():
    """_randbytes_compat() must be byte-for-byte identical to CPython's
    documented random.Random.randbytes() semantics (getrandbits(n*8) packed
    little-endian) for the same seed. The expected value is computed here
    independently rather than via the real randbytes() method, which does
    not exist on Python 3.8 -- this test must itself run on 3.8."""
    seed = 20260925
    n = 4096
    r_compat = random.Random(seed)
    r_expected = random.Random(seed)
    expected = r_expected.getrandbits(n * 8).to_bytes(n, "little")
    assert wmime_common._randbytes_compat(r_compat, n) == expected


def test_randbytes_dispatches_to_real_randbytes_when_available():
    """When rng exposes a randbytes attribute, _randbytes() must dispatch to
    it rather than falling back to _randbytes_compat(). Uses a fake stand-in
    (the real stdlib randbytes is unavailable on Python 3.8) that records
    calls and raises if the fallback path is touched."""
    calls = []

    class _FakeRandbytesRng:
        def randbytes(self, n):
            calls.append(n)
            return b"\xab" * n

        def getrandbits(self, k):
            raise AssertionError("fallback path must not be used when randbytes exists")

    fake_rng = _FakeRandbytesRng()
    result = wmime_common._randbytes(fake_rng, 256)
    assert result == b"\xab" * 256
    assert calls == [256]


def test_randbytes_fallback_used_when_randbytes_attribute_missing():
    """Simulates Python 3.8 (no random.Random.randbytes) via a getrandbits-
    only stand-in, and proves the fallback path is actually taken (not
    silently skipped) and produces output identical to the real
    randbytes() for the same seed."""
    seed = 4242

    class _NoRandbytes:
        def __init__(self, seed):
            self._inner = random.Random(seed)

        def getrandbits(self, k):
            return self._inner.getrandbits(k)

    fake_rng = _NoRandbytes(seed)
    assert not hasattr(fake_rng, "randbytes")
    n = 1024
    expected = random.Random(seed).getrandbits(n * 8).to_bytes(n, "little")
    assert wmime_common._randbytes(fake_rng, n) == expected


def test_make_filler_bytes_works_without_randbytes_on_py38_style_rng():
    """End-to-end: make_filler_bytes() itself (not just the private helper)
    must work and stay deterministic when randbytes() is unavailable,
    matching the real 3.9+ output for the same seed."""
    seed = 99

    class _NoRandbytes:
        def __init__(self, seed):
            self._inner = random.Random(seed)

        def getrandbits(self, k):
            return self._inner.getrandbits(k)

    fake_rng = _NoRandbytes(seed)
    result = wmime_common.make_filler_bytes(fake_rng, 2048)
    expected = wmime_common.make_filler_bytes(random.Random(seed), 2048)
    assert result == expected
    assert len(result) == 2048


def test_make_filler_bytes_deterministic_with_same_seed():
    a = wmime_common.make_filler_bytes(random.Random(1234), 3000)
    b = wmime_common.make_filler_bytes(random.Random(1234), 3000)
    assert a == b
    assert len(a) == 3000


def test_make_filler_text_works_without_randbytes_on_py38_style_rng():
    """make_filler_text() also calls the shared _randbytes() helper --
    must be equally Python-3.8-safe and deterministic."""
    seed = 555

    class _NoRandbytes:
        def __init__(self, seed):
            self._inner = random.Random(seed)

        def getrandbits(self, k):
            return self._inner.getrandbits(k)

    fake_rng = _NoRandbytes(seed)
    result = wmime_common.make_filler_text(fake_rng, 500)
    expected = wmime_common.make_filler_text(random.Random(seed), 500)
    assert result == expected
    assert len(result) == 500


# --- Bundled default Workload04 + multi-workload support tests ------------

def _make_tiny_workload_fixture(root: Path, label: str, n: int = 5) -> Path:
    """Build a minimal, self-consistent future-workload fixture (spec
    section 25): n small text files + a matching *-resources.txt, NOT
    claiming to be 10,000 files -- exercises the generic (non-Workload04)
    baseline path."""
    payload_dir = root / label
    payload_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(n):
        rel = f"a{i}.txt"
        (payload_dir / rel).write_text(f"fixture file {i} for {label}\n" * 10, encoding="utf-8")
        lines.append(f"/{label}/{rel}")
    resource_list = root / f"{label}-resources.txt"
    resource_list.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_default_workload_root_resolution():
    root = wg_bundled.default_workload_root("workload04")
    assert root.name == "workload04"
    assert isinstance(root, Path)


def test_bundled_zip_present_and_checksum_verified():
    zip_path = wg_bundled.bundled_zip_path()
    sha_path = wg_bundled.bundled_zip_checksum_path()
    assert zip_path.is_file(), f"bundled zip missing at {zip_path}"
    assert sha_path.is_file(), f"checksum file missing at {sha_path}"
    ok, detail = wg_bundled.verify_bundled_zip_checksum()
    assert ok, detail


def test_bundled_zip_extracts_into_fresh_target(extracted_bundled_workload04):
    """Extraction into an empty target must produce the expected
    workload04-resources.txt + workload04/ layout with exactly 10,000
    payload files."""
    dest, count = extracted_bundled_workload04
    assert count == 10001  # 10,000 payload files + 1 resource list
    assert (dest / "workload04-resources.txt").is_file()
    assert (dest / "workload04").is_dir()
    payload_files = [p for p in (dest / "workload04").rglob("*") if p.is_file()]
    assert len(payload_files) == 10000


def test_verify_extracted_workload04_detects_incomplete_corpus(tmp_path):
    """A directory that exists but doesn't contain a valid Workload04
    corpus must be detected as invalid, not silently accepted."""
    fake_root = tmp_path / "fake_workload04"
    fake_root.mkdir()
    (fake_root / "workload04").mkdir()
    (fake_root / "workload04" / "only_one_file.txt").write_text("x", encoding="utf-8")
    fake_resources = fake_root / "workload04-resources.txt"
    fake_resources.write_text("/workload04/only_one_file.txt\n", encoding="utf-8")
    ok, detail = wg_bundled.verify_extracted_workload04(fake_root, fake_resources)
    assert not ok
    assert detail


def test_ensure_default_workload_available_no_reextraction_when_valid(extracted_bundled_workload04, monkeypatch):
    """When a valid extracted corpus already exists, ensure_default_workload_available()
    must reuse it as-is and must NEVER call safe_extract_zip() again -- proven here by
    making a second extraction attempt fail loudly, rather than by corrupting the corpus
    (which would just make the strict physical-file-count check correctly reject it)."""
    fake_default_root, _count = extracted_bundled_workload04
    monkeypatch.setattr(wg_bundled, "default_workload_root", lambda label=None: fake_default_root)

    def _fail_if_extracted(*args, **kwargs):
        raise AssertionError("safe_extract_zip must not be called when a valid corpus already exists")
    monkeypatch.setattr(wg_bundled, "safe_extract_zip", _fail_if_extracted)

    source_root, resource_list = wg_bundled.ensure_default_workload_available("workload04")
    assert source_root == fake_default_root

    # A second call must also reuse the same corpus without re-extracting.
    source_root2, resource_list2 = wg_bundled.ensure_default_workload_available("workload04")
    assert source_root2 == fake_default_root
    assert resource_list2 == resource_list


def test_zip_path_traversal_is_rejected(tmp_path):
    """A crafted zip with a '../' entry must never be extracted anywhere
    outside the destination directory."""
    import zipfile as _zipfile

    evil_zip = tmp_path / "evil.zip"
    with _zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("safe.txt", "ok")
        zf.writestr("../escape.txt", "should never be written outside dest")

    dest = tmp_path / "dest"
    with pytest.raises(wg_bundled.CorpusVerificationError):
        wg_bundled.safe_extract_zip(evil_zip, dest)
    # All-or-nothing: since validation runs before any extraction, nothing
    # should have been written, including the "safe" entry.
    assert not (tmp_path / "escape.txt").exists()
    assert not (dest / "safe.txt").exists()


def test_explicit_workload_root_precedence_and_separate_baseline(tmp_path):
    """--workload-root must freeze its OWN baseline under a distinct label,
    never touching/overwriting the workload04 baseline, and must not
    invoke any bundled-Workload04 logic."""
    import workload04_generator as cli

    fixture_root = _make_tiny_workload_fixture(tmp_path, "workload18test", n=6)
    baseline_dir = wg_common.default_baseline_dir("workload18test")
    shutil.rmtree(baseline_dir, ignore_errors=True)
    try:
        class Args:
            workload_root = str(fixture_root)
            resource_list = None

        baseline, label = cli._resolve_workload(Args())
        assert label == "workload18test"
        assert baseline.total_entries == 6
        assert baseline.label == "workload18test"
        # The known Workload04 baseline must be completely untouched.
        wl04_baseline = baselinefreeze.FrozenBaseline(wg_common.default_baseline_dir("workload04"))
        assert wl04_baseline.total_entries == 10000
    finally:
        wg_common.set_workload_label(None)
        shutil.rmtree(baseline_dir, ignore_errors=True)


def test_explicit_workload_generic_invariants_not_forced(tmp_path):
    """A tiny 6-file fixture must NOT be rejected for failing to match
    Workload04's 10,000/3/0.03% invariants -- those are only enforced for
    the default "workload04" label. A clean, self-consistent tiny corpus
    legitimately has ZERO discrepancies once Workload04-specific checks are
    correctly not applied to it; this test must not require a fabricated
    discrepancy just because the fixture differs in size from Workload04."""
    fixture_root = _make_tiny_workload_fixture(tmp_path, "workload24test", n=6)
    baseline_dir = wg_common.default_baseline_dir("workload24test")
    shutil.rmtree(baseline_dir, ignore_errors=True)
    try:
        resource_list = fixture_root / "workload24test-resources.txt"
        header = baselinefreeze.freeze(
            resource_list, fixture_root, baseline_dir,
            label="workload24test", accept_discrepancies=True,
        )
        # 1. the 6-entry corpus is accepted and reported correctly.
        assert header["total_entries"] == 6
        assert header["physical_file_count_on_disk"] == 6
        assert header["missing_files"] == 0
        # 2. Workload04's 10,000/3/0.03% rules are NOT applied to it.
        assert header["known_invariants_enforced"] is False
        for d in header["discrepancies_at_freeze_time"]:
            assert "10000" not in d and "0.0300" not in d, (
                f"Workload04-specific invariant leaked into a generic workload's discrepancies: {d!r}"
            )
        # 3. no cross-contamination with the real Workload04 baseline.
        wl04_baseline = baselinefreeze.FrozenBaseline(wg_common.default_baseline_dir("workload04"))
        assert wl04_baseline.total_entries == 10000
        assert wl04_baseline.known_invariants_enforced is True
    finally:
        shutil.rmtree(baseline_dir, ignore_errors=True)


def test_output_naming_and_manifest_identify_source_workload(tmp_path, monkeypatch):
    """Generated output directory naming and manifest.json must reflect
    the active workload label, not a hardcoded 'workload04'."""
    try:
        wg_common.set_workload_label("workload18test")
        out_dir = wg_common.output_dir_for_tier("256KiB")
        assert out_dir.name == "workload18test-256KiB"
        assert wg_common.uri_root_prefix("256KiB") == "/workload18test-256KiB/"
    finally:
        wg_common.set_workload_label(None)
    # Default (no override) must remain exactly the original Workload04 naming.
    assert wg_common.output_dir_for_tier("256KiB").name == "workload04-256KiB"
    assert wg_common.uri_root_prefix("256KiB") == "/workload04-256KiB/"


def test_workload_aware_max_avg_size_uses_selected_baseline(tmp_path):
    """max-avg-size's underlying calculation must operate on whichever
    baseline (Workload04 or an explicit workload) was resolved -- not
    always Workload04."""
    fixture_root = _make_tiny_workload_fixture(tmp_path, "workload99test", n=4)
    baseline_dir = wg_common.default_baseline_dir("workload99test")
    shutil.rmtree(baseline_dir, ignore_errors=True)
    try:
        resource_list = fixture_root / "workload99test-resources.txt"
        baselinefreeze.freeze(resource_list, fixture_root, baseline_dir,
                               label="workload99test", accept_discrepancies=True)
        small_baseline = baselinefreeze.FrozenBaseline(baseline_dir)
        result = wg_storage_module_max_avg_size(small_baseline, tmp_path / "out_root")
        assert result["physical_files"] == 4
    finally:
        shutil.rmtree(baseline_dir, ignore_errors=True)


def wg_storage_module_max_avg_size(baseline, out_dir):
    from wg import storage as wg_storage
    return wg_storage.compute_max_avg_size(baseline, out_dir)


def test_dry_run_reports_selected_workload_label(tmp_path):
    """--workload-root + --dry-run must preflight the EXPLICIT workload,
    never silently falling back to Workload04."""
    import workload04_generator as cli

    fixture_root = _make_tiny_workload_fixture(tmp_path, "workload77test", n=4)
    baseline_dir = wg_common.default_baseline_dir("workload77test")
    out_root = tmp_path / "generated_workload77test"
    shutil.rmtree(baseline_dir, ignore_errors=True)
    try:
        rc = cli.main([
            "--avg-size", "1KiB", "--dry-run",
            "--workload-root", str(fixture_root),
            "--output-root", str(out_root),
        ])
        assert rc == 0
        assert not out_root.exists()
    finally:
        wg_common.set_generated_root(None)
        wg_common.set_workload_label(None)
        shutil.rmtree(baseline_dir, ignore_errors=True)



