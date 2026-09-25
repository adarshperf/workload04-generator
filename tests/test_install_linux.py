"""Regression tests for install_linux.sh's pure detection/decision logic.

These tests never install OS packages and never require root/sudo -- they
source install_linux.sh with WG_INSTALL_SOURCE_ONLY=1 (which makes the
script define its functions and return immediately, without running the
installer flow) and then call individual functions directly, exactly like
tests/test_regression.py unit-tests wg/ Python functions in isolation.

Requires a real `bash` interpreter to be reachable (Linux: always; Windows
dev: via Git for Windows' bundled bash.exe). If no bash is found at all,
these tests are skipped with a clear reason -- a genuine environment
capability gap, not a hidden failure (mirrors the existing `baseline`
fixture's skip-when-not-frozen pattern elsewhere in this suite).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = TOOL_ROOT / "install_linux.sh"

_CANDIDATE_BASH_PATHS = [
    "bash",
    r"C:\ProgramData\Git\bin\bash.exe",
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
]


def _find_bash():
    found = shutil.which("bash")
    if found:
        return found
    for cand in _CANDIDATE_BASH_PATHS:
        if Path(cand).is_file():
            return cand
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash interpreter found on this machine")


def _run_bash(script_body: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run `script_body` (a small bash snippet) after sourcing
    install_linux.sh with WG_INSTALL_SOURCE_ONLY=1, from TOOL_ROOT."""
    full = f'export WG_INSTALL_SOURCE_ONLY=1\nsource "{INSTALL_SCRIPT.as_posix()}"\n{script_body}\n'
    return subprocess.run(
        [BASH, "-c", full],
        cwd=str(TOOL_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --- Shell syntax -----------------------------------------------------------

def test_install_script_syntax_is_valid():
    result = subprocess.run([BASH, "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_script_sourceable_for_testing():
    result = _run_bash("echo SOURCED_OK")
    assert result.returncode == 0, result.stderr
    assert "SOURCED_OK" in result.stdout


# --- Portability: no hard-coded Windows/developer paths ---------------------

def test_no_hardcoded_windows_or_developer_paths_in_installer():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8", errors="replace")
    forbidden = ["C:\\\\", "C:/Users", "AdarshChouksey", "c:\\\\", "c:/Users"]
    offenders = [bad for bad in forbidden if bad in text]
    assert not offenders, f"Hard-coded Windows/user-specific paths found: {offenders}"


# --- Package manager detection ----------------------------------------------

def test_detect_package_manager_returns_known_value():
    result = _run_bash("detect_package_manager")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in {"apt", "dnf", "none"}


def test_os_package_mapping_apt_and_dnf():
    result = _run_bash(
        "os_package_for_concept apt python; "
        "os_package_for_concept apt venv; "
        "os_package_for_concept apt pip; "
        "os_package_for_concept dnf python; "
        'echo "[$(os_package_for_concept dnf venv)]"; '
        "os_package_for_concept dnf pip"
    )
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln != ""]
    assert lines == ["python3", "python3-venv", "python3-pip", "python3", "[]", "python3-pip"]


def test_manual_install_hint_mentions_exact_packages():
    result = _run_bash('manual_install_hint apt "python3 python3-venv python3-pip"')
    assert result.returncode == 0, result.stderr
    assert "python3-venv" in result.stdout
    assert "apt-get install" in result.stdout


def test_manual_install_hint_unknown_package_manager_is_generic_not_silent():
    result = _run_bash('manual_install_hint none "python3"')
    assert result.returncode == 0, result.stderr
    assert "python3" in result.stdout


# --- Python detection --------------------------------------------------------

def test_find_python_finds_an_interpreter_on_this_machine():
    """This machine (running pytest right now) obviously HAS an adequate
    Python -- find_python() must locate it via PATH lookup."""
    result = _run_bash("find_python")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ""


def test_python_version_ok_accepts_current_interpreter():
    py = sys.executable
    result = _run_bash(f'python_version_ok "{Path(py).as_posix()}" && echo OK || echo NOTOK')
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert "NOTOK" not in result.stdout


def test_python_missing_concepts_empty_for_healthy_interpreter():
    """The interpreter currently running pytest already has working venv +
    pip (pytest itself is installed via pip) -- nothing should be missing."""
    py = sys.executable
    result = _run_bash(f'python_missing_concepts "{Path(py).as_posix()}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# --- venv health / broken-venv recovery -------------------------------------

def test_venv_is_healthy_false_for_missing_directory(tmp_path):
    venv_dir = (tmp_path / "does_not_exist").as_posix()
    result = _run_bash(f'venv_is_healthy "{venv_dir}" && echo YES || echo NO')
    assert result.returncode == 0, result.stderr
    assert "NO" in result.stdout


def test_venv_is_healthy_false_for_broken_venv(tmp_path):
    """A directory that exists but has no bin/python is exactly the
    "broken/incomplete venv" case the installer must recover from."""
    broken = tmp_path / ".venv"
    (broken / "bin").mkdir(parents=True)
    result = _run_bash(f'venv_is_healthy "{broken.as_posix()}" && echo YES || echo NO')
    assert result.returncode == 0, result.stderr
    assert "NO" in result.stdout


def test_venv_is_healthy_true_for_real_venv(tmp_path):
    """Build a minimal POSIX-layout venv (bin/python backed by a real,
    working interpreter) using bash itself -- this is exactly the layout a
    real `python3 -m venv` produces on Linux, and exactly what
    venv_is_healthy() checks for; using bash (rather than this test
    process's own `venv` module, whose directory layout is OS-native and
    would be Scripts/python.exe on Windows) keeps the test meaningful
    cross-platform."""
    venv_dir = tmp_path / ".venv"
    result = _run_bash(
        f'mkdir -p "{venv_dir.as_posix()}/bin"; '
        f'ln -sf "$(command -v python || command -v python3)" "{venv_dir.as_posix()}/bin/python"; '
        f'venv_is_healthy "{venv_dir.as_posix()}" && echo YES || echo NO'
    )
    assert result.returncode == 0, result.stderr
    assert "YES" in result.stdout


def test_recover_broken_venv_removes_only_dot_venv(tmp_path):
    """Broken-venv recovery must remove ONLY a directory literally named
    .venv, and must refuse to touch anything else (defense in depth against
    ever deleting baseline/reference_workloads/project data)."""
    broken_venv = tmp_path / ".venv"
    (broken_venv / "bin").mkdir(parents=True)
    sibling_baseline = tmp_path / "baselines"
    sibling_baseline.mkdir()
    (sibling_baseline / "sentinel.txt").write_text("do not delete", encoding="utf-8")

    result = _run_bash(
        f'recover_broken_venv "{broken_venv.as_posix()}"; echo "RC=$?"'
    )
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    assert not broken_venv.exists(), "broken .venv should have been removed"
    assert sibling_baseline.is_dir(), "sibling project data must never be touched"
    assert (sibling_baseline / "sentinel.txt").read_text(encoding="utf-8") == "do not delete"


def test_recover_broken_venv_refuses_unexpected_path(tmp_path):
    """A directory NOT named .venv must never be removed, even if it looks
    unhealthy by the same criteria."""
    not_a_venv = tmp_path / "some_other_dir"
    (not_a_venv / "bin").mkdir(parents=True)

    result = _run_bash(f'recover_broken_venv "{not_a_venv.as_posix()}"; echo "RC=$?"')
    assert result.returncode == 0, result.stderr
    assert "RC=1" in result.stdout
    assert not_a_venv.is_dir(), "a non-.venv path must never be deleted"


def test_recover_broken_venv_is_idempotent_noop_when_healthy(tmp_path):
    """Running recovery against an already-healthy venv must be a no-op
    (idempotent second run). Uses the same bash-built POSIX-layout venv as
    test_venv_is_healthy_true_for_real_venv, for the same cross-platform
    reason."""
    venv_dir = tmp_path / ".venv"
    result = _run_bash(
        f'mkdir -p "{venv_dir.as_posix()}/bin"; '
        f'ln -sf "$(command -v python || command -v python3)" "{venv_dir.as_posix()}/bin/python"; '
        f'recover_broken_venv "{venv_dir.as_posix()}"; echo "RC=$?"'
    )
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    assert venv_dir.is_dir(), "a healthy venv must never be removed"
    assert (venv_dir / "bin" / "python").exists()


# --- root/sudo detection -----------------------------------------------------

def test_have_root_and_have_sudo_are_boolean_shell_predicates():
    """Both predicates must be well-defined boolean shell functions (exit
    0/1), regardless of this machine's actual privilege level."""
    result = _run_bash("have_root && echo R=yes || echo R=no; have_sudo && echo S=yes || echo S=no")
    assert result.returncode == 0, result.stderr
    assert "R=yes" in result.stdout or "R=no" in result.stdout
    assert "S=yes" in result.stdout or "S=no" in result.stdout


# --- pip failure diagnostics -------------------------------------------------

def test_diagnose_pip_failure_detects_dns_network_error(tmp_path):
    logfile = tmp_path / "pip.log"
    logfile.write_text(
        "Collecting Pillow\n"
        "  Could not fetch URL https://pypi.org/simple/pillow/: "
        "Temporary failure in name resolution\n",
        encoding="utf-8",
    )
    result = _run_bash(f'diagnose_pip_failure "{logfile.as_posix()}"')
    assert result.returncode == 0, result.stderr
    assert "DNS" in result.stdout


def test_diagnose_pip_failure_detects_version_mismatch(tmp_path):
    logfile = tmp_path / "pip.log"
    logfile.write_text(
        "ERROR: Could not find a version that satisfies the requirement Pillow>=10.0\n",
        encoding="utf-8",
    )
    result = _run_bash(f'diagnose_pip_failure "{logfile.as_posix()}"')
    assert result.returncode == 0, result.stderr
    assert "version" in result.stdout.lower()


def test_diagnose_pip_failure_never_leaks_credentials(tmp_path):
    """The diagnostic output must never echo the raw log content (which
    could contain an internal index URL with embedded credentials) --
    only a short diagnosis line plus the log FILE PATH."""
    logfile = tmp_path / "pip.log"
    logfile.write_text(
        "Could not fetch URL https://user:supersecretpassword@internal.example/simple/: "
        "Temporary failure in name resolution\n",
        encoding="utf-8",
    )
    result = _run_bash(f'diagnose_pip_failure "{logfile.as_posix()}"')
    assert result.returncode == 0, result.stderr
    assert "supersecretpassword" not in result.stdout


# --- idempotent execution: does not fail when run in a state that already
# looks fully installed ------------------------------------------------------

def test_install_os_packages_noop_for_empty_package_list():
    """When nothing is missing, install_os_packages must be a safe no-op
    (idempotent second run) and never attempt privileged calls."""
    result = _run_bash('install_os_packages apt; echo "RC=$?"')
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout


# --- version-specific venv package detection (regression: a real fresh
# Debian/Ubuntu machine with Python 3.8.10 but no python3.8-venv failed
# with "ensurepip is not available" because the old logic only checked
# `import venv`/`import ensurepip`, which both succeed on Debian even when
# actual venv creation is broken) -------------------------------------------

def test_python_major_minor_matches_actual_interpreter():
    py = Path(sys.executable).as_posix()
    expected = f"{sys.version_info.major}.{sys.version_info.minor}"
    result = _run_bash(f'python_major_minor "{py}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_apt_venv_package_name_is_version_specific_not_hardcoded():
    """Must derive the package name FROM the interpreter's actual version
    -- never hard-code e.g. python3.10-venv for a 3.8 or 3.12 interpreter."""
    py = Path(sys.executable).as_posix()
    major, minor = sys.version_info.major, sys.version_info.minor
    result = _run_bash(f'apt_venv_package_name "{py}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"python{major}.{minor}-venv"
    # A fabricated 3.8 interpreter reference must NOT collide with this
    # machine's own (different) version -- proves the mapping is dynamic.
    if (major, minor) != (3, 8):
        assert result.stdout.strip() != "python3.8-venv"


def test_os_package_for_concept_apt_venv_prefers_exact_version_over_generic():
    py = Path(sys.executable).as_posix()
    major, minor = sys.version_info.major, sys.version_info.minor
    with_py = _run_bash(f'os_package_for_concept apt venv "{py}"')
    without_py = _run_bash("os_package_for_concept apt venv")
    assert with_py.returncode == 0, with_py.stderr
    assert without_py.returncode == 0, without_py.stderr
    assert with_py.stdout.strip() == f"python{major}.{minor}-venv"
    assert without_py.stdout.strip() == "python3-venv"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="python -m venv lays out Scripts/ on Windows, not the POSIX bin/ "
           "layout this Linux-only installer's venv_probe_works() checks for",
)
def test_venv_probe_works_true_for_real_interpreter():
    """This machine's own interpreter (running pytest right now) must be
    able to create a real, working venv -- proving the probe isn't a
    false negative on a healthy system."""
    py = Path(sys.executable).as_posix()
    result = _run_bash(f'venv_probe_works "{py}" && echo YES || echo NO')
    assert result.returncode == 0, result.stderr
    assert "YES" in result.stdout


def test_venv_probe_works_false_for_nonexistent_interpreter():
    result = _run_bash('venv_probe_works "/no/such/python/binary" && echo YES || echo NO')
    assert result.returncode == 0, result.stderr
    assert "NO" in result.stdout


def _stubbed_env(probe_sequence, install_calls_file):
    """Bash snippet that overrides venv_probe_works to return the given
    sequence of exit codes (one per call, last value repeats after
    exhausted) and overrides install_os_packages to record each requested
    package to `install_calls_file` and succeed -- used to test
    ensure_venv_capability's orchestration without touching real OS
    packages or requiring root/sudo."""
    seq = " ".join(str(c) for c in probe_sequence)
    return f'''
_probe_seq=({seq})
_probe_i=0
venv_probe_works() {{
    idx=$_probe_i
    if [ "$idx" -ge "${{#_probe_seq[@]}}" ]; then
        idx=$(( ${{#_probe_seq[@]}} - 1 ))
    fi
    _probe_i=$(( _probe_i + 1 ))
    return "${{_probe_seq[$idx]}}"
}}
install_os_packages() {{
    pm="$1"; shift
    echo "$*" >> "{install_calls_file}"
    return 0
}}
'''


def test_ensure_venv_capability_noop_when_already_working(tmp_path):
    """Idempotent / healthy-case: if venv already works, install_os_packages
    must never be called at all."""
    calls_file = (tmp_path / "calls.txt").as_posix()
    body = _stubbed_env([0], calls_file) + f'\nensure_venv_capability "fakepython" apt; echo "RC=$?"'
    result = _run_bash(body)
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    assert not Path(calls_file).exists(), "a working interpreter must never trigger a package install"


def test_ensure_venv_capability_installs_exact_version_package_and_recovers(tmp_path):
    """Missing ensurepip/venv recovery path: probe fails once, the EXACT
    interpreter-version package gets installed, then the probe succeeds --
    exactly the fresh-Debian-machine scenario from the bug report."""
    calls_file = (tmp_path / "calls.txt").as_posix()
    py = Path(sys.executable).as_posix()
    body = _stubbed_env([1, 0], calls_file) + f'\nensure_venv_capability "{py}" apt; echo "RC=$?"'
    result = _run_bash(body)
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    major, minor = sys.version_info.major, sys.version_info.minor
    calls = Path(calls_file).read_text(encoding="utf-8").strip()
    assert calls == f"python{major}.{minor}-venv", (
        "must install the EXACT interpreter-version package first, not the generic one"
    )


def test_ensure_venv_capability_falls_back_to_generic_package(tmp_path):
    """If the exact-version package install doesn't fix it, the generic
    python3-venv package must be tried as a second, automatic attempt."""
    calls_file = (tmp_path / "calls.txt").as_posix()
    py = Path(sys.executable).as_posix()
    body = _stubbed_env([1, 1, 0], calls_file) + f'\nensure_venv_capability "{py}" apt; echo "RC=$?"'
    result = _run_bash(body)
    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout
    major, minor = sys.version_info.major, sys.version_info.minor
    calls = Path(calls_file).read_text(encoding="utf-8").splitlines()
    assert calls == [f"python{major}.{minor}-venv", "python3-venv"]


def test_ensure_venv_capability_fails_clearly_when_still_broken(tmp_path):
    """If venv still doesn't work after trying both the exact-version and
    generic packages, ensure_venv_capability must report failure (1), not
    silently claim success."""
    calls_file = (tmp_path / "calls.txt").as_posix()
    py = Path(sys.executable).as_posix()
    body = _stubbed_env([1, 1, 1], calls_file) + f'\nensure_venv_capability "{py}" apt; echo "RC=$?"'
    result = _run_bash(body)
    assert result.returncode == 0, result.stderr
    assert "RC=1" in result.stdout


def test_ensure_venv_capability_returns_2_when_no_root_or_sudo(tmp_path):
    """When automatic package installation is impossible (no root/sudo),
    ensure_venv_capability must report that distinctly (2) so the caller
    can fail with the exact manual command, never attempting a fallback
    that would also be impossible."""
    py = Path(sys.executable).as_posix()
    body = f'''
venv_probe_works() {{ return 1; }}
install_os_packages() {{ return 2; }}
ensure_venv_capability "{py}" apt; echo "RC=$?"
'''
    result = _run_bash(body)
    assert result.returncode == 0, result.stderr
    assert "RC=2" in result.stdout


def test_ensure_venv_capability_returns_2_for_unknown_package_manager():
    py = Path(sys.executable).as_posix()
    result = _run_bash(f'venv_probe_works() {{ return 1; }}\nensure_venv_capability "{py}" none; echo "RC=$?"')
    assert result.returncode == 0, result.stderr
    assert "RC=2" in result.stdout


def test_existing_installer_behavior_intact_for_apt_pip_and_python():
    """Non-venv concept mappings must be completely unaffected by the
    version-specific venv change."""
    result = _run_bash(
        "os_package_for_concept apt python; "
        "os_package_for_concept apt pip; "
        "os_package_for_concept dnf python; "
        "os_package_for_concept dnf pip"
    )
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln != ""]
    assert lines == ["python3", "python3-pip", "python3", "python3-pip"]
