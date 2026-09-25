#!/usr/bin/env bash
# install_linux.sh -- one-shot Linux installer for workload04_generator.
#
#  1. Verifies the OS is Linux.
#  2. Detects the Linux distribution's package manager (apt or dnf).
#  3. Detects a suitable Python interpreter (>= MIN_PY_MAJOR.MIN_PY_MINOR).
#  4. If Python/venv/pip support is missing, installs ONLY the minimal
#     required OS packages automatically (root directly, or via sudo for a
#     non-root user), then re-detects Python. Never installs unrelated
#     system packages, and never silently continues after a failed OS
#     package installation. Before creating .venv, also runs a REAL
#     functional probe (not just a static import check) to catch
#     Debian/Ubuntu's split-package ensurepip failure mode, and -- if
#     needed -- automatically installs the EXACT interpreter-version
#     package (e.g. python3.8-venv, python3.10-venv, python3.12-venv;
#     never hard-coded to one version), falling back to the generic
#     python3-venv only if that doesn't exist for this distro.
#  5. Creates an isolated .venv -- recovering (recreating ONLY .venv, never
#     touching project data) if an existing one is broken/incomplete.
#  6. Upgrades pip, installs requirements.txt (runtime + optional
#     format-fidelity deps) and requirements-dev.txt (pytest) into it, with
#     clear network/DNS/version diagnostics on failure.
#  7. Verifies the vendored `wmime` package and `wg` package both import.
#  8. Runs a lightweight installation validation (`--help`).
#  9. Runs `self-test` (baseline-dependent tests self-skip automatically if
#     no frozen baseline is present yet -- see README.md "Quick Start" for
#     the machine-specific `init-baseline` step; this installer never
#     invents or regenerates baseline/golden-corpus data).
# 10. Prints a clear PASS/FAIL summary and exits non-zero on ANY failure.
#
# Running this script multiple times on the same machine is safe
# (idempotent): already-installed OS packages, an already-healthy .venv,
# and already-installed pip requirements are all detected and reused as-is.
# This script never deletes baselines/, reference_workloads/, the external
# golden corpus, generated workload output, or user configuration -- the
# only thing it ever removes is a broken .venv/, and only .venv/.
set -u

MIN_PY_MAJOR=3
MIN_PY_MINOR=8

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

FAIL=0
step() { printf '\n== %s ==\n' "$1"; }
ok()   { printf '  [OK]   %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
err()  { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

# ===========================================================================
# Pure detection/decision functions.
#
# These are deliberately side-effect-free (aside from install_os_packages(),
# which is the one function that changes system state) so
# tests/test_install_linux.py can source this file with
# WG_INSTALL_SOURCE_ONLY=1 and unit-test the logic directly, without
# requiring root or actually installing anything.
# ===========================================================================

detect_package_manager() {
    # Prints exactly one of: apt dnf none
    if command -v apt-get >/dev/null 2>&1; then
        echo apt
    elif command -v dnf >/dev/null 2>&1; then
        echo dnf
    else
        echo none
    fi
}

have_root() {
    [ "$(id -u 2>/dev/null || echo 1)" = "0" ]
}

have_sudo() {
    command -v sudo >/dev/null 2>&1
}

# Minimal OS package providing `concept` (python|venv|pip) for package
# manager `pm` (apt|dnf). Empty output means "nothing to install" (e.g.
# venv support ships inside the `python3` package itself on RHEL-family).
#
# For apt:venv, pass the selected python binary as $3 to get the EXACT
# interpreter-version package (e.g. "python3.8-venv", "python3.10-venv",
# "python3.12-venv") -- Debian/Ubuntu split ensurepip's bootstrap wheels
# into a per-version package, and installing the wrong version's package
# does not fix another version's interpreter. Omitting $3 (or if the
# version can't be determined) falls back to the generic "python3-venv".
os_package_for_concept() {
    pm="$1"; concept="$2"; py="${3:-}"
    case "${pm}:${concept}" in
        apt:python) echo "python3" ;;
        apt:venv)
            if [ -n "$py" ]; then
                exact="$(apt_venv_package_name "$py")"
                if [ -n "$exact" ]; then
                    echo "$exact"
                else
                    echo "python3-venv"
                fi
            else
                echo "python3-venv"
            fi
            ;;
        apt:pip)    echo "python3-pip" ;;
        dnf:python) echo "python3" ;;
        dnf:venv)   echo "" ;;
        dnf:pip)    echo "python3-pip" ;;
        *)          echo "" ;;
    esac
}

python_version_ok() {
    # $1 = candidate python binary
    "$1" - <<PYEOF >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)
PYEOF
}

find_python() {
    # Prints the first adequate python interpreter name found on PATH, or
    # nothing (and returns 1) if none qualifies.
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1 && python_version_ok "$cand"; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

python_missing_concepts() {
    # $1 = python binary (already known to exist and version-check OK).
    # Prints a space-separated subset of: venv pip
    py="$1"
    missing=""
    if ! "$py" -c "import venv" >/dev/null 2>&1; then
        missing="$missing venv"
    fi
    if ! "$py" -m pip --version >/dev/null 2>&1 && ! "$py" -c "import ensurepip" >/dev/null 2>&1; then
        missing="$missing pip"
    fi
    printf '%s' "$missing" | sed -e 's/^ *//'
}

venv_is_healthy() {
    # $1 = venv directory. Healthy = has a working python with a working pip.
    [ -x "$1/bin/python" ] && "$1/bin/python" -m pip --version >/dev/null 2>&1
}

python_major_minor() {
    # $1 = python binary. Prints "3.8", "3.10", "3.12", etc.
    "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

apt_venv_package_name() {
    # $1 = python binary. Prints the EXACT-version Debian/Ubuntu venv
    # package for this interpreter (e.g. "python3.8-venv"), never a
    # hard-coded version. Prints nothing if the version can't be determined.
    ver="$(python_major_minor "$1")"
    if [ -n "$ver" ]; then
        echo "python${ver}-venv"
    fi
}

venv_probe_works() {
    # $1 = python binary. Attempts a REAL, disposable venv creation to
    # detect Debian/Ubuntu's split-package ensurepip failure mode: `import
    # venv` and `import ensurepip` can both succeed while actual venv
    # creation still fails ("ensurepip is not available") unless the exact
    # pythonX.Y-venv package is installed -- a static import check cannot
    # see this; only actually trying to create a venv can. The probe venv
    # is created under a throwaway temp directory and always removed.
    py="$1"
    probe_dir="$(mktemp -d 2>/dev/null)" || probe_dir="/tmp/wg_venv_probe.$$"
    mkdir -p "$probe_dir" 2>/dev/null
    result=1
    if "$py" -m venv "$probe_dir/probe" >/dev/null 2>&1 && [ -x "$probe_dir/probe/bin/python" ]; then
        result=0
    fi
    rm -rf -- "$probe_dir"
    return $result
}

# Ensures `py` can create a working venv, automatically installing the
# exact interpreter-version OS package if it currently cannot (preferring
# pythonX.Y-venv over the generic python3-venv on Debian/Ubuntu; falling
# back to the generic package only if the exact-version one doesn't fix
# it). Returns: 0 = working (already, or after installing a package), 1 =
# automatic installation was attempted and venv still doesn't work, 2 = no
# automatic installation was possible (unknown package manager, or neither
# root nor sudo available).
ensure_venv_capability() {
    py="$1"; pm="$2"
    if venv_probe_works "$py"; then
        return 0
    fi
    if [ "$pm" = "none" ]; then
        return 2
    fi
    primary_pkg="$(os_package_for_concept "$pm" venv "$py")"
    fallback_pkg="$(os_package_for_concept "$pm" venv)"
    [ -z "$fallback_pkg" ] && fallback_pkg="python3-venv"
    candidates="$primary_pkg"
    if [ -n "$fallback_pkg" ] && [ "$fallback_pkg" != "$primary_pkg" ]; then
        candidates="$candidates $fallback_pkg"
    fi
    for pkg in $candidates; do
        install_os_packages "$pm" "$pkg"
        rc=$?
        if [ "$rc" = "2" ]; then
            return 2
        fi
        if venv_probe_works "$py"; then
            return 0
        fi
    done
    return 1
}

diagnose_pip_failure() {
    # $1 = path to a pip log file. Prints a targeted hint (network/DNS,
    # version mismatch, or generic) without ever printing credentials.
    logfile="$1"
    if [ ! -f "$logfile" ]; then
        return 0
    fi
    if grep -qiE 'temporary failure in name resolution|could not resolve host|name or service not known' "$logfile"; then
        warn "Diagnosis: DNS resolution failure -- this machine cannot resolve the package index hostname."
    elif grep -qiE 'network is unreachable|connection refused|max retries exceeded|failed to establish a new connection|connection timed out' "$logfile"; then
        warn "Diagnosis: network connectivity failure reaching the Python package index (check proxy/firewall/offline status)."
    elif grep -qiE 'no matching distribution found|could not find a version that satisfies the requirement' "$logfile"; then
        warn "Diagnosis: no compatible package version found -- check this Python's version/platform against the requirement, and that the package index is reachable."
    elif grep -qiE 'read timed out' "$logfile"; then
        warn "Diagnosis: the package index connection timed out -- likely a slow/unreliable network."
    fi
    warn "Full pip output: $logfile"
}

# Installs the given OS packages (idempotent -- apt/dnf skip already-current
# packages on their own). Returns:
#   0 = installed (or nothing to do)
#   1 = install command itself failed
#   2 = neither root nor sudo available -- cannot install automatically
install_os_packages() {
    pm="$1"; shift
    # shellcheck disable=SC2124
    pkgs="$*"
    pkgs="$(printf '%s' "$pkgs" | sed -e 's/^ *//' -e 's/ *$//')"
    if [ -z "$pkgs" ]; then
        return 0
    fi
    logfile="/tmp/wg_os_packages.log"
    if have_root; then
        case "$pm" in
            apt) apt-get update -y >"$logfile" 2>&1 && apt-get install -y $pkgs >>"$logfile" 2>&1 ;;
            dnf) dnf install -y $pkgs >"$logfile" 2>&1 ;;
            *) return 1 ;;
        esac
        return $?
    elif have_sudo; then
        case "$pm" in
            apt) sudo apt-get update -y >"$logfile" 2>&1 && sudo apt-get install -y $pkgs >>"$logfile" 2>&1 ;;
            dnf) sudo dnf install -y $pkgs >"$logfile" 2>&1 ;;
            *) return 1 ;;
        esac
        return $?
    else
        return 2
    fi
}

manual_install_hint() {
    # $1 = package manager (apt|dnf|none), $2 = space-separated packages.
    pm="$1"; pkgs="$2"
    case "$pm" in
        apt) printf '    sudo apt-get update && sudo apt-get install -y %s\n' "$pkgs" ;;
        dnf) printf '    sudo dnf install -y %s\n' "$pkgs" ;;
        *)
            printf '    # Unrecognized package manager -- install via your distribution:\n'
            printf '    #   Debian/Ubuntu : sudo apt-get install -y python3 python3-venv python3-pip\n'
            printf '    #   RHEL/Fedora   : sudo dnf install -y python3 python3-pip\n'
            ;;
    esac
}

# Idempotent venv recovery: if `venv_dir` exists but is unhealthy (broken or
# incomplete), remove ONLY that directory so the caller can recreate it
# fresh -- never touches baselines/, reference_workloads/, generated output,
# or anything else. Only ever removes a path whose basename is exactly
# `.venv`, as a defensive guard against ever deleting the wrong directory.
# Prints nothing; returns 0 if `venv_dir` is now safe to (re)create or
# already healthy, 1 if it refused to remove an unexpected path.
recover_broken_venv() {
    venv_dir="$1"
    if [ -d "$venv_dir" ] && ! venv_is_healthy "$venv_dir"; then
        case "$(basename -- "$venv_dir")" in
            .venv) rm -rf -- "$venv_dir" ;;
            *) return 1 ;;
        esac
    fi
    return 0
}

# If sourced for unit testing (tests/test_install_linux.py), stop here --
# every function above is now defined and testable without running the
# installer or touching the system.
if [ "${WG_INSTALL_SOURCE_ONLY:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

# ===========================================================================
# Main installer flow
# ===========================================================================

# --- 1. OS check -----------------------------------------------------------
step "Operating system"
UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
if [ "$UNAME_S" != "Linux" ]; then
    err "This installer targets Linux only (detected: $UNAME_S)."
    printf '\nRESULT: FAIL\n'
    exit 1
fi
ok "Linux detected ($(uname -r 2>/dev/null || echo unknown))"

# --- 2. Package manager detection ------------------------------------------
step "Package manager"
PKG_MGR="$(detect_package_manager)"
case "$PKG_MGR" in
    apt) ok "Detected apt (Debian/Ubuntu family)" ;;
    dnf) ok "Detected dnf (RHEL/Rocky/Alma/Fedora family)" ;;
    *)   warn "No known package manager (apt/dnf) detected -- automatic OS package installation will not be available." ;;
esac

# --- 3. Python interpreter (+ automatic OS-package bootstrap if needed) ----
step "Python interpreter"
PYTHON_BIN="$(find_python || true)"
MISSING_CONCEPTS=""
if [ -z "$PYTHON_BIN" ]; then
    MISSING_CONCEPTS="python"
else
    ok "Found candidate: $PYTHON_BIN (Python $("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
    MISSING_CONCEPTS="$(python_missing_concepts "$PYTHON_BIN")"
fi

if [ -n "$MISSING_CONCEPTS" ]; then
    NEEDED_PKGS=""
    for concept in $MISSING_CONCEPTS; do
        if [ "$concept" = "venv" ]; then
            pkg="$(os_package_for_concept "$PKG_MGR" "$concept" "$PYTHON_BIN")"
        else
            pkg="$(os_package_for_concept "$PKG_MGR" "$concept")"
        fi
        [ -n "$pkg" ] && NEEDED_PKGS="$NEEDED_PKGS $pkg"
    done
    NEEDED_PKGS="$(printf '%s' "$NEEDED_PKGS" | sed -e 's/^ *//')"
    if [ -z "$NEEDED_PKGS" ]; then
        ok "No additional OS packages needed for: $MISSING_CONCEPTS"
    else
        warn "Missing prerequisite(s): $MISSING_CONCEPTS -- need OS package(s): $NEEDED_PKGS"
        if [ "$PKG_MGR" = "none" ]; then
            err "Cannot install automatically: no supported package manager (apt/dnf) detected."
            printf '\nInstall the following manually, then re-run this script:\n'
            manual_install_hint "$PKG_MGR" "$NEEDED_PKGS"
            printf '\nRESULT: FAIL\n'
            exit 1
        fi
        step "Installing OS prerequisites ($NEEDED_PKGS)"
        install_os_packages "$PKG_MGR" $NEEDED_PKGS
        rc=$?
        case "$rc" in
            0) ok "OS package installation succeeded ($NEEDED_PKGS)." ;;
            2)
                err "Neither running as root nor sudo is available -- cannot install: $NEEDED_PKGS"
                printf '\nInstall the following manually, then re-run this script:\n'
                manual_install_hint "$PKG_MGR" "$NEEDED_PKGS"
                printf '\nRESULT: FAIL\n'
                exit 1
                ;;
            *)
                err "OS package installation failed (see /tmp/wg_os_packages.log)."
                printf '\nInstall the following manually, then re-run this script:\n'
                manual_install_hint "$PKG_MGR" "$NEEDED_PKGS"
                printf '\nRESULT: FAIL\n'
                exit 1
                ;;
        esac
        # Re-detect after installation -- never assume it worked.
        PYTHON_BIN="$(find_python || true)"
        if [ -z "$PYTHON_BIN" ]; then
            err "Still no Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} found on PATH after installing OS packages."
            printf '\nRESULT: FAIL\n'
            exit 1
        fi
        STILL_MISSING="$(python_missing_concepts "$PYTHON_BIN")"
        if [ -n "$STILL_MISSING" ]; then
            err "Still missing after OS package installation: $STILL_MISSING"
            printf '\nRESULT: FAIL\n'
            exit 1
        fi
        ok "Re-detected working interpreter: $PYTHON_BIN"
    fi
fi

if [ -z "$PYTHON_BIN" ]; then
    err "No Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} found on PATH."
    printf '\nRESULT: FAIL\n'
    exit 1
fi
PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Using $PYTHON_BIN (Python $PY_VERSION)"

# --- 4. Virtual environment (create, or recover if broken) ----------------
step "Virtual environment"
VENV_DIR="$SCRIPT_DIR/.venv"
if [ -d "$VENV_DIR" ] && ! venv_is_healthy "$VENV_DIR"; then
    warn "Existing .venv at $VENV_DIR looks broken/incomplete (missing/unusable python or pip)."
    warn "Recreating ONLY .venv (no baseline/reference_workloads/generated output/config is touched)."
    if ! recover_broken_venv "$VENV_DIR"; then
        err "Refusing to remove unexpected path: $VENV_DIR"
        printf '\nRESULT: FAIL\n'
        exit 1
    fi
fi
if [ ! -d "$VENV_DIR" ]; then
    if ! venv_probe_works "$PYTHON_BIN"; then
        warn "$PYTHON_BIN cannot create a working virtual environment yet (ensurepip unavailable)."
        candidate_pkg="$(os_package_for_concept "$PKG_MGR" venv "$PYTHON_BIN")"
        [ -z "$candidate_pkg" ] && candidate_pkg="python3-venv"
        warn "Attempting automatic installation of: $candidate_pkg"
        ensure_venv_capability "$PYTHON_BIN" "$PKG_MGR"
        rc=$?
        case "$rc" in
            0) ok "Virtual environment capability confirmed after installing the required OS package." ;;
            2)
                err "Neither running as root nor sudo is available -- cannot install venv support automatically."
                printf '\nInstall the following manually, then re-run this script:\n'
                manual_install_hint "$PKG_MGR" "$candidate_pkg"
                printf '\nRESULT: FAIL\n'
                exit 1
                ;;
            *)
                err "Automatic installation of venv support failed (see /tmp/wg_os_packages.log)."
                printf '\nInstall the following manually, then re-run this script:\n'
                manual_install_hint "$PKG_MGR" "$candidate_pkg"
                printf '\nRESULT: FAIL\n'
                exit 1
                ;;
        esac
    fi
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
        err "Failed to create virtual environment at $VENV_DIR."
        printf '\nRESULT: FAIL\n'
        exit 1
    fi
    ok "Created $VENV_DIR"
else
    ok "Reusing existing, healthy $VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
    err "Virtual environment python not found at $VENV_PY."
    printf '\nRESULT: FAIL\n'
    exit 1
fi

# --- 5. Install dependencies ------------------------------------------------
step "Installing dependencies"
if ! "$VENV_PY" -m pip install --upgrade pip >/tmp/wg_pip_upgrade.log 2>&1; then
    err "pip self-upgrade failed."
    diagnose_pip_failure /tmp/wg_pip_upgrade.log
    printf '\nRESULT: FAIL\n'
    exit 1
fi
ok "pip upgraded"

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    if ! "$VENV_PY" -m pip install -r "$SCRIPT_DIR/requirements.txt" >/tmp/wg_pip_reqs.log 2>&1; then
        err "Failed to install requirements.txt (runtime + optional format-fidelity dependencies)."
        diagnose_pip_failure /tmp/wg_pip_reqs.log
        printf '\nRESULT: FAIL\n'
        exit 1
    fi
    ok "requirements.txt installed"
fi

if [ -f "$SCRIPT_DIR/requirements-dev.txt" ]; then
    if ! "$VENV_PY" -m pip install -r "$SCRIPT_DIR/requirements-dev.txt" >/tmp/wg_pip_dev.log 2>&1; then
        err "Failed to install requirements-dev.txt (pytest, needed only for self-test)."
        diagnose_pip_failure /tmp/wg_pip_dev.log
        printf '\nRESULT: FAIL\n'
        exit 1
    fi
    ok "requirements-dev.txt installed"
fi

# --- 6. Verify imports ---------------------------------------------------------
step "Verifying imports"
if ! "$VENV_PY" - <<PYEOF
import sys
sys.path.insert(0, "$SCRIPT_DIR")
from wg import common as wg_common  # noqa: F401
from wmime import common as wmime_common  # noqa: F401
print("import OK")
PYEOF
then
    err "Could not import wg/wmime packages -- installation is broken."
    printf '\nRESULT: FAIL\n'
    exit 1
fi
ok "wg and wmime packages import cleanly"

# --- 7. Lightweight installation validation -------------------------------------
step "CLI smoke check"
if ! "$VENV_PY" "$SCRIPT_DIR/workload04_generator.py" --help >/tmp/wg_help.log 2>&1; then
    err "workload04_generator.py --help failed. See /tmp/wg_help.log"
    printf '\nRESULT: FAIL\n'
    exit 1
fi
ok "CLI responds to --help"

# --- 8. Self-test -----------------------------------------------------------
step "Self-test"
BASELINE_MANIFEST="$SCRIPT_DIR/baselines/workload04/baseline_manifest.json"
if [ -f "$BASELINE_MANIFEST" ]; then
    ok "Frozen Workload04 baseline metadata found -- running the full regression self-test."
    warn "First run may take several minutes (verifies/extracts the bundled golden corpus and generates a real reference tier)."
else
    warn "No frozen baseline metadata found at $BASELINE_MANIFEST."
    warn "The golden corpus is always an external input -- this installer never invents or regenerates it."
    warn "Baseline-dependent tests below will skip automatically; after registering YOUR machine's corpus with:"
    warn "    source $VENV_DIR/bin/activate"
    warn "    python workload04_generator.py init-baseline --source-root <corpus> --resource-list <resource-list>"
    warn "re-run 'python workload04_generator.py self-test' for the full suite."
fi
if "$VENV_PY" "$SCRIPT_DIR/workload04_generator.py" self-test >/tmp/wg_selftest.log 2>&1; then
    ok "self-test PASSED. Log: /tmp/wg_selftest.log"
else
    err "self-test FAILED. See /tmp/wg_selftest.log"
fi

# --- 9. Summary ------------------------------------------------------------------
step "Summary"
if [ "$FAIL" -eq 0 ]; then
    printf '\nRESULT: PASS\n'
    printf '\nActivate the environment with:\n    source %s/bin/activate\n' "$VENV_DIR"
    exit 0
else
    printf '\nRESULT: FAIL\n'
    printf '\nSee README.md "Manual installation / recovery if install_linux.sh fails" for a\n'
    printf 'step-by-step manual fallback.\n'
    exit 1
fi
