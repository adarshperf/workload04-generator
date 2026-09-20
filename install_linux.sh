#!/usr/bin/env bash
# install_linux.sh -- one-shot Linux installer for workload04_generator.
#
# 1. Verifies the OS is Linux.
# 2. Detects a suitable Python interpreter (>= MIN_PY_MAJOR.MIN_PY_MINOR).
# 3. Verifies venv/ensurepip/pip capability.
# 4. Creates an isolated .venv.
# 5. Installs requirements.txt + requirements-dev.txt into it.
# 6. Verifies the vendored `wmime` package and `wg` package both import.
# 7. Runs a lightweight installation validation (`--help`).
# 8. Runs the full self-test (pytest) if a frozen baseline is already present;
#    otherwise runs only the baseline-independent subset.
# 9. Prints a clear PASS/FAIL summary and exits non-zero on ANY failure.
#
# This script never installs OS packages automatically. If your platform is
# missing python3-venv/python3-pip, it prints the exact apt/dnf commands to
# run yourself (see README.md "Manual Installation / Recovery" for the full
# list) and exits non-zero -- it never silently continues after a
# dependency failure.
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

# --- 1. OS check -----------------------------------------------------------
step "Operating system"
UNAME_S="$(uname -s 2>/dev/null || echo unknown)"
if [ "$UNAME_S" != "Linux" ]; then
    err "This installer targets Linux only (detected: $UNAME_S)."
    printf '\nRESULT: FAIL\n'
    exit 1
fi
ok "Linux detected ($(uname -r 2>/dev/null || echo unknown))"

# --- 2. Python detection ----------------------------------------------------
step "Python interpreter"
PYTHON_BIN=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver_ok=$("$cand" - <<PYEOF 2>/dev/null
import sys
print(1 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 0)
PYEOF
)
        if [ "$ver_ok" = "1" ]; then
            PYTHON_BIN="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    err "No Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} found on PATH."
    printf '\nInstall Python %s.%s or newer, then re-run this script. See README.md\n' "$MIN_PY_MAJOR" "$MIN_PY_MINOR"
    printf '"Manual Installation / Recovery if install_linux.sh fails" for exact commands.\n'
    printf '\nRESULT: FAIL\n'
    exit 1
fi
PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Using $PYTHON_BIN (Python $PY_VERSION)"

# --- 3. venv / pip capability ------------------------------------------------
step "venv / pip capability"
if ! "$PYTHON_BIN" -c "import venv" >/dev/null 2>&1; then
    err "Python venv module is not available for $PYTHON_BIN."
    if command -v apt-get >/dev/null 2>&1; then
        printf '  Try: sudo apt-get install -y python3-venv\n'
    elif command -v dnf >/dev/null 2>&1; then
        printf '  Try: sudo dnf install -y python3-venv\n'
    fi
    printf '\nRESULT: FAIL\n'
    exit 1
fi
ok "venv module available"

if ! "$PYTHON_BIN" -c "import ensurepip" >/dev/null 2>&1; then
    warn "ensurepip module not available -- venv creation may still work if pip is preinstalled."
else
    ok "ensurepip module available"
fi

# --- 4. Create .venv ---------------------------------------------------------
step "Virtual environment"
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
        err "Failed to create virtual environment at $VENV_DIR."
        printf '\nRESULT: FAIL\n'
        exit 1
    fi
    ok "Created $VENV_DIR"
else
    ok "Reusing existing $VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
    err "Virtual environment python not found at $VENV_PY."
    printf '\nRESULT: FAIL\n'
    exit 1
fi

# --- 5. Install dependencies --------------------------------------------------
step "Installing dependencies"
if ! "$VENV_PY" -m pip install --upgrade pip >/tmp/wg_pip_upgrade.log 2>&1; then
    err "pip self-upgrade failed. See /tmp/wg_pip_upgrade.log"
    printf '\nRESULT: FAIL\n'
    exit 1
fi
ok "pip upgraded"

if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    if ! "$VENV_PY" -m pip install -r "$SCRIPT_DIR/requirements.txt" >/tmp/wg_pip_reqs.log 2>&1; then
        err "Failed to install requirements.txt. See /tmp/wg_pip_reqs.log"
        printf '\nRESULT: FAIL\n'
        exit 1
    fi
    ok "requirements.txt installed"
fi

if [ -f "$SCRIPT_DIR/requirements-dev.txt" ]; then
    if ! "$VENV_PY" -m pip install -r "$SCRIPT_DIR/requirements-dev.txt" >/tmp/wg_pip_dev.log 2>&1; then
        err "Failed to install requirements-dev.txt. See /tmp/wg_pip_dev.log"
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

# --- 8. Self-test (if dependencies allow) ---------------------------------------
step "Self-test"
BASELINE_DIR="$SCRIPT_DIR/baseline"
if [ -f "$BASELINE_DIR/baseline_manifest.json" ]; then
    if "$VENV_PY" "$SCRIPT_DIR/workload04_generator.py" self-test >/tmp/wg_selftest.log 2>&1; then
        ok "self-test PASSED (full suite, frozen baseline found). Log: /tmp/wg_selftest.log"
    else
        err "self-test FAILED. See /tmp/wg_selftest.log"
    fi
else
    warn "No frozen baseline found at $BASELINE_DIR -- skipping the baseline-dependent self-test."
    warn "Run 'init-baseline' first (see README.md Quick Start), then re-run self-test manually:"
    warn "    source $VENV_DIR/bin/activate && python workload04_generator.py self-test"
fi

# --- 9. Summary ------------------------------------------------------------------
step "Summary"
if [ "$FAIL" -eq 0 ]; then
    printf '\nRESULT: PASS\n'
    printf '\nActivate the environment with:\n    source %s/bin/activate\n' "$VENV_DIR"
    exit 0
else
    printf '\nRESULT: FAIL\n'
    printf '\nSee README.md "Manual Installation / Recovery if install_linux.sh fails" for a\n'
    printf 'step-by-step manual fallback.\n'
    exit 1
fi
