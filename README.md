# workload04_generator

Standalone, portable, production-ready Linux tool that generates
target-average-object-size variants of the golden Workload04 dataset for
Secure Web Gateway Full Coverage / SSL Tap A/B performance testing.

This directory is self-contained: copy the whole `workload04_generator/`
folder (including its vendored `wmime/` subfolder) to any Linux machine,
run `./install_linux.sh`, point it at your golden Workload04 corpus, and
generate a tier -- no sibling repository, no GitHub Copilot, and no
developer-specific paths are required.

## Table of contents

1. [Overview](#overview)
2. [Golden Workload04 definition and invariants](#golden-workload04-definition-and-invariants)
3. [1:1 physical/logical model](#11-physicallogical-model)
4. [EICAR rule](#eicar-rule)
5. [MIME/type distribution preservation](#mimetype-distribution-preservation)
6. [Path normalization rules](#path-normalization-rules)
7. [Collision handling](#collision-handling)
8. [Architecture](#architecture)
9. [Package layout](#package-layout)
10. [External golden corpus setup](#external-golden-corpus-setup)
11. [Baseline fingerprint / staleness](#baseline-fingerprint--staleness)
12. [Reference workloads](#reference-workloads)
13. [Supported environments / minimum Python version](#supported-environments--minimum-python-version)
14. [Runtime and test dependencies](#runtime-and-test-dependencies)
15. [One-shot installation](#one-shot-installation)
16. [Manual installation / recovery if install_linux.sh fails](#manual-installation--recovery-if-install_linuxsh-fails)
17. [Output directory (/opt)](#output-directory-opt)
18. [Permissions](#permissions)
19. [Quick Start](#quick-start)
20. [Commands reference](#commands-reference)
21. [Output directory structure](#output-directory-structure)
22. [Manifest files](#manifest-files)
23. [Resource-list format](#resource-list-format)
24. [Storage pre-flight logic](#storage-pre-flight-logic)
25. [Safe maximum average size calculation](#safe-maximum-average-size-calculation)
26. [Inode checks](#inode-checks)
27. [Atomic generation behavior](#atomic-generation-behavior)
28. [Failure / cleanup behavior](#failure--cleanup-behavior)
29. [Force / replace semantics](#force--replace-semantics)
30. [Large targets / no universal maximum size](#large-targets--no-universal-maximum-size)
31. [Format/content fidelity](#formatcontent-fidelity)
32. [Troubleshooting](#troubleshooting)
33. [Using generated workloads with performance test tools](#using-generated-workloads-with-performance-test-tools)
34. [Known validated tiers](#known-validated-tiers)
35. [Test status](#test-status)
36. [Fresh Linux acceptance procedure](#fresh-linux-acceptance-procedure)
37. [Final acceptance / release checklist](#final-acceptance--release-checklist)
38. [Known limitations](#known-limitations)

## Overview

`workload04_generator.py` reads a frozen copy of the golden Workload04
baseline's statistics and, for any requested average object size, produces
a fully independent, self-validating "tier" directory: 10,000 physical
payload files, a runtime resource list, and a set of reports. The output is
a plain directory of files plus a one-URI-per-line resource list -- it is
**not tied to any specific performance/load-testing tool**. Copy a
generated tier to any machine and point whatever HTTP load generator you
use (HttpBlaster/HTTP Storm, Sledgehammer, IxLoad, a custom framework, or
anything else that can read a file + path list) at it. See
[Using generated workloads with performance test tools](#using-generated-workloads-with-performance-test-tools)
for tool-specific, entirely optional integration notes.

## Golden Workload04 definition and invariants

The original Golden Workload04 corpus contains exactly:

- 10,000 physical payload files
- 10,000 logical resource-list entries
- a strict 1:1 logical-to-physical mapping
- exactly 3 EICAR/AV-test resources (0.03% frequency)
- a fixed MIME/type/extension distribution
- a fixed directory hierarchy and resource ordering

**These invariants never change.** Every generated tier preserves them
exactly; only each file's byte size is scaled toward the requested average.
The original corpus itself is never modified by this tool (`init-baseline`
is read-only against it; see [External golden corpus setup](#external-golden-corpus-setup)).

## 1:1 physical/logical model

Every one of the baseline's 10,000 physical resources gets its own
generated physical resource, at the same relative path, with the same
extension/MIME identity, in the same request-list position. **Nothing is
pooled, shared, or collapsed** -- no "one file per MIME class" shortcut, no
bounded diversity pool, no duplicate collapsing, no synthetic resource
replacement, no resource-count reduction. The only thing that changes per
target size is each file's byte content/size, scaled proportionally from
its own original size. Request count, MIME distribution, extension
distribution, EICAR frequency, directory structure, and resource-list
order are all identical to the baseline by construction, not by post-hoc
rebalancing.

## EICAR rule

The 3 baseline EICAR/AV-test resources are preserved 1:1 in every tier
(`eicar_count == 3`, `eicar_pct == 0.03%`), and are never allowed to shrink
below their own real seed size (their EICAR/malware signature can never be
truncated away by a downscaling target) -- see `wg/pool.py::_clamped_target()`.
Every generated tier is checked for this (`eicar_count`, `eicar_ratio`,
`eicar_has_source_seed`).

## MIME/type distribution preservation

Extension, MIME type, and format "family" (image/text/binary/archive/etc.)
are copied unchanged from each baseline record to its generated
counterpart. `validate.py` checks `mime_distribution` and
`extension_distribution` against the baseline with **exact** counts, not a
statistical tolerance.

## Path normalization rules

**Baseline semantics are preserved exactly as recorded** (percent-encoding
and all) in `baseline_records.csv`/`baseline_resource_order.txt` -- nothing
about the frozen baseline itself changes. What changes is the *generated*
physical filesystem path and runtime URI, which intentionally do **not**
reproduce the baseline's raw percent-encoding:

- Whitespace in a generated path is replaced with `_`.
- The generated physical filesystem path and the generated runtime URI are
  the SAME normalized string (no percent-encoding at all in generated
  output, since whitespace was the only character ever percent-encoded in
  this corpus).
- Generated runtime URIs and generated physical paths never contain
  whitespace, and never contain `%20` (or any other percent-escape)
  resulting from whitespace normalization.
- This is deliberate and Linux/filesystem-safe by design -- do NOT read
  this as "generated encoding equals baseline encoding"; it explicitly is
  not, for physical/runtime paths. See [Collision handling](#collision-handling)
  and the detailed design notes further below for exactly why and how.

## Collision handling

Because generated paths are normalized (whitespace stripped), two
*different* baseline paths can, in rare cases, collapse to the same
normalized string. `wg/pathnorm.py` is the single source of truth for
detecting and resolving this deterministically:

- SHA-256 of `f"{seed}:{uri}"`, truncated to 8 hex characters, inserted as
  `<stem>__<hash8><ext>` immediately before the extension.
- When a collision occurs, the group member whose decoded baseline path
  was *already* whitespace-free (if any) keeps the plain normalized name;
  every other member gets the deterministic suffix.
- Deterministic and reproducible for the same baseline + target + seed;
  collision-safe (~0.0000116 probability of the 8-hex-char suffix itself
  colliding, across 10,000 records).
- After normalization, unique generated physical paths always remain
  exactly 10,000 -- checked by `unique_generated_physical_paths` and
  `exactly_10000_unique_paths_after_normalization`-style tests.
- `manifest.csv` records `baseline_decoded_rel`, `normalized_rel`, and
  `collision_suffix` for every file, and `manifest.json` records the total
  `collision_count`, for full auditability.

## Architecture

```
workload04_generator.py       <- CLI entry point (init-baseline, generate,
                                  validate, audit-size, max-avg-size, self-test)
compare_baseline_vs_generated.py   <- read-only baseline-vs-tier report
analyze_missing_baseline_entries.py <- read-only baseline diagnostic

wg/
    common.py       <- shared paths/size parsing, output-root resolution
    baselinefreeze.py <- init-baseline / FrozenBaseline
    pool.py          <- strict 1:1 planning (WorkloadPlan/PlannedRecord)
    generate.py      <- physical file + resource-list generation
    pathnorm.py      <- whitespace normalization + collision handling
    storage.py       <- filesystem capacity/inode pre-flight, max-avg-size
    safety.py        <- generation lock + atomic staged-generation
    validate.py      <- post-generation hard validation
    sizeaudit.py     <- independent, read-only size audit
    reportio.py       <- per-tier output-file writers

wmime/              <- vendored format-aware content generators (byte-exact
                        JPEG/PNG/GIF/PDF/ZIP/PE handling), MIME tables,
                        EICAR detection, baseline resource-list parsing.
                        Vendored (not a sibling-package import) so this
                        directory is fully standalone/copyable.

tests/               <- tests/test_regression.py (self-test suite)
baseline/            <- frozen baseline METADATA only (never the corpus itself)
```

## Package layout

```
workload04_generator/
    workload04_generator.py
    compare_baseline_vs_generated.py
    analyze_missing_baseline_entries.py
    wg/
    wmime/
    tests/
    baseline/
        README.md                  <- explains this dir holds metadata only
        baseline_manifest.json     <- present only after init-baseline
        baseline_records.csv
        baseline_resource_order.txt
    reference_workloads/
        workload04-100KiB/         <- pre-generated, validated reference tier
        workload04-1MiB/           <- pre-generated, validated reference tier
    requirements.txt
    requirements-dev.txt
    install_linux.sh
    README.md
    VERSION
```

The actual 10,000-file golden corpus is **never** bundled inside this
package by default -- it is treated as an external input (see next
section). `reference_workloads/` holds exactly two small, pre-validated
example tiers for quick sanity-checking (see
[Reference workloads](#reference-workloads)) -- these are NOT where new
runtime generations go. New generated tiers are written to
`/opt/generated_workload04` (see [Output directory](#output-directory-opt)),
never inside this package directory.

## External golden corpus setup

The tool does not depend on any hard-coded path to the golden corpus. Point
it at wherever the corpus lives on the target machine:

```bash
python workload04_generator.py init-baseline \
  --source-root /opt/workload04 \
  --resource-list /opt/workload04-resources.txt
```

If `--source-root`/`--resource-list` are omitted, `init-baseline` also
automatically checks `/opt/workload04` / `/opt/load/workload04-resources.txt`
(the conventional Linux install locations) before giving up with a clear
error. `init-baseline` is strictly **read-only** against both -- it never
writes into either.

## Baseline fingerprint / staleness

`baseline/baseline_manifest.json` records the `source_root_path` it was
frozen from. If this project is copied to a new machine and a real
generation is attempted (`--avg-size ...` without `--dry-run`) BEFORE
re-running `init-baseline` with `--source-root`/`--resource-list` pointing
at that machine's own copy of the corpus, `wg/baselinefreeze.py::FrozenBaseline.source_root_reachable()`
detects that the recorded path no longer resolves and the CLI refuses to
start (rather than silently generating non-representative content for
every record) -- see `workload04_generator.py cmd_generate`. Pass
`--allow-synthetic-fallback` to explicitly override this and accept
degraded content. `--dry-run` only prints a warning (never blocks), since
it writes nothing.

**Always re-run `init-baseline` with your own `--source-root`/
`--resource-list` on every new machine before generating a new tier.** The
bundled `reference_workloads/` tiers do not need this -- they were
pre-generated and are used read-only.

## Reference workloads

`reference_workloads/workload04-100KiB/` and `reference_workloads/workload04-1MiB/`
are two small, pre-generated, already-validated example tiers for quick
sanity-checking on a machine that already has them (e.g. `validate`/
`audit-size` right after cloning, before doing any real generation of your
own). They are **local, gitignored, developer-convenience artifacts, not
tracked in git** (see `.gitignore`) -- a fresh clone will not have this
directory at all, since the two tiers together are several GiB and are
never meant to travel with the repository. `tests/test_regression.py::
test_reference_workloads_integrity` validates them strictly when present,
and otherwise proves the same deterministic generation mechanism using an
already-generated real tier, instead of requiring the directory to exist.
They are ordinary read-only example output -- not templates the generator
reads from, and not affected by `--output-root`/`/opt/generated_workload04`.
Do not regenerate them casually; if you do, use `--force` and
re-validate/re-audit afterward. No other tier sizes are bundled in this
project on purpose (see
[Large targets](#large-targets--no-universal-maximum-size)) -- generate
whatever additional sizes you need under `/opt/generated_workload04`.

## Supported environments / minimum Python version

- **Primary target**: Linux (any distribution with Python >= 3.8).
- Also runs on Windows/macOS for local development (the tool uses
  `pathlib`/`shutil` throughout and has no Linux-only runtime
  dependency); inode-based storage checks and `/opt` as a default output
  root are POSIX/Linux-specific and are gracefully skipped or substituted
  on other platforms (see [Output directory](#output-directory-opt) and
  [Inode checks](#inode-checks)).
- **Minimum Python version: 3.8** -- determined from the codebase's use of
  `from __future__ import annotations` (3.7+) plus the pinned optional
  dependencies' own minimum supported Python (Pillow>=10.0 and pypdf>=4.0
  both require 3.8+). No walrus operator, no `match`/`case`, no
  `str.removeprefix`/`removesuffix`, and no `X | Y` union-type syntax are
  used anywhere in `wg/`, `wmime/`, or the CLI, so nothing in the tool's
  own code requires newer than 3.8.

## Runtime and test dependencies

- **Mandatory for generation/validation/audit**: none beyond the Python
  standard library. The entire strict 1:1 model, path normalization,
  collision handling, storage pre-flight, atomic staging, and validation
  logic use only stdlib (`pathlib`, `shutil`, `os`, `hashlib`, `json`,
  `csv`, `random`, `zipfile`, ...).
- **Optional, for higher-fidelity content generation** (`requirements.txt`):
  `Pillow`, `pypdf`, `pefile`. Each is imported behind a try/except in
  `wmime/formats.py`; if missing, that format family's generator falls
  back to a generic, still size-correct, still-validating
  "binary_fallback" content path rather than failing. Install these for
  byte-exact real JPEG/PNG/PDF/PE content.
- **Required only for `self-test`** (`requirements-dev.txt`): `pytest`.

## One-shot installation

```bash
cd workload04_generator
./install_linux.sh
```

The script is a true one-shot bootstrap: on a fresh Debian/Ubuntu or
RHEL/Rocky/Alma/Fedora machine with nothing preinstalled, it detects and
installs the minimal missing OS packages itself (no manual `apt`/`dnf`
step required first), then finishes setting up a working environment.

1. verifies the OS is Linux (exits non-zero otherwise);
2. detects the package manager (`apt` for Debian/Ubuntu, `dnf` for
   RHEL/Rocky/Alma/Fedora -- an unrecognized distro is reported clearly,
   never guessed);
3. detects a Python >= 3.8 interpreter and its `venv`/pip capability;
4. if anything from step 3 is missing, installs ONLY the minimal required
   OS package(s) (`python3`, `python3-venv`, `python3-pip` as applicable)
   -- directly if running as root, via `sudo` otherwise; if neither root
   nor `sudo` is available, or the install command itself fails, it
   **fails clearly with the exact packages/commands needed and never
   silently continues**; then re-detects Python and re-verifies before
   moving on;
5. creates an isolated `.venv` -- reusing it if already healthy, or
   **recovering by recreating ONLY `.venv`** (never touching
   `baselines/`, `reference_workloads/`, generated output, or any other
   project data) if an existing one is broken/incomplete;
6. upgrades pip and installs `requirements.txt` (runtime + optional
   format-fidelity packages) and `requirements-dev.txt` (`pytest`) into
   it, with a targeted diagnosis (DNS/network/version-mismatch) instead of
   a bare pip traceback on failure;
7. verifies `wg`/`wmime` import cleanly;
8. runs a lightweight `--help` smoke check;
9. runs `self-test` (baseline-dependent tests self-skip automatically if
   no frozen baseline is present yet -- this installer never invents or
   regenerates baseline/golden-corpus data; see
   [Golden corpus / baseline setup](#quick-start) below for that step);
10. prints a clear `RESULT: PASS`/`RESULT: FAIL` summary;
11. **exits non-zero on any failure and never silently continues.**

**Idempotent:** running `./install_linux.sh` again on the same machine is
safe -- already-installed OS packages, an already-healthy `.venv`, and
already-installed pip requirements are all detected and reused as-is.

## Manual installation / recovery if install_linux.sh fails

`install_linux.sh` already installs missing OS prerequisites (`python3`,
`python3-venv`, `python3-pip`) automatically on Debian/Ubuntu (`apt`) and
RHEL/Rocky/Alma/Fedora (`dnf`) machines, using `sudo` if not already root.
Manual installation is only needed if: the OS package manager isn't
`apt`/`dnf`, neither root nor `sudo` is available, or you simply prefer to
install by hand. Only the packages actually required by this tool are
listed below.

**Debian/Ubuntu:**

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
cd workload04_generator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python workload04_generator.py self-test
```

**RHEL/Rocky/Alma/CentOS:**

```bash
sudo dnf install -y python3 python3-pip
cd workload04_generator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python workload04_generator.py self-test
```

If `self-test` reports "baseline not frozen", run `init-baseline` first
(see [Quick Start](#quick-start)) then re-run `self-test`.

## Output directory (/opt)

**All generated tool output goes under `/opt/generated_workload04` by
default** on Linux:

```
/opt/generated_workload04/
    workload04-100KiB/
    workload04-1MiB/
    workload04-4MiB/
    ...
    workload04-<TARGET>/
```

Every payload file, resource list, manifest, validation report, size
report, and metadata file for a tier lives under
`/opt/generated_workload04/workload04-<TIER>/`. The tool never silently
writes production output into the current working directory.

Resolution priority (see `wg/common.py::get_generated_root()`):

1. `--output-root <path>` CLI flag (accepted by `generate`, `validate`,
   `audit-size`, `max-avg-size`) -- intended for testing/development;
   document any use of this in your own run notes, since it diverges from
   the production default.
2. `WORKLOAD04_OUTPUT_ROOT` environment variable.
3. `/opt/generated_workload04` (production default, POSIX/Linux only).
4. A dev/test fallback next to this checkout (non-POSIX platforms only,
   e.g. local Windows development -- never used on a real Linux
   deployment unless explicitly requested).

## Permissions

Before any pre-flight or generation work starts, the tool verifies it can
create and write to the output root (`wg/storage.py::check_output_root_writable()`).
If it cannot:

- **no** payload generation starts;
- a clear error is printed, including the exact remediation commands;
- the process exits non-zero.

Example remediation (exact commands depend on your environment; adjust
user/group as appropriate):

```bash
sudo mkdir -p /opt/generated_workload04
sudo chown -R "$(id -un)":"$(id -gn)" /opt/generated_workload04
```

## Quick Start

**A. Fresh Linux machine -- install:**

```bash
git clone <repo-url>
cd workload04_generator
./install_linux.sh
```

**B. Verify the installation:**

```bash
source .venv/bin/activate
python workload04_generator.py self-test
```

**C. Register this machine's golden corpus** (external input -- never
modified by this tool; skip if you're only using the bundled default
Workload04 corpus):

```bash
python workload04_generator.py init-baseline \
  --source-root /opt/workload04 \
  --resource-list /opt/workload04-resources.txt
```

**D. Generate a practical first tier.** `10KiB` is a good first target: it
is comfortably above this corpus's format-preserving content-size lower
bound (formats like HTML/CSS/JS/PDF/PE/ZIP never shrink below their real
source size, which puts a hard floor around ~4KiB for the golden
Workload04 corpus). A target like `512B` is correctly **rejected during
pre-flight, before any staging/generation**, because it is mathematically
below that floor for this corpus -- this is expected, documented behavior,
not a bug (see [Storage pre-flight logic](#storage-pre-flight-logic)).

```bash
python workload04_generator.py --avg-size 10KiB
# or, with an explicit workload/output root:
python workload04_generator.py --avg-size 10KiB \
  --workload-root /opt/workload04 \
  --resource-list /opt/workload04-resources.txt \
  --output-root /opt/generated_workload04
```

**E. Validate:**

```bash
python workload04_generator.py validate 10KiB
python workload04_generator.py audit-size 10KiB
```

Generated output (under `/opt/generated_workload04/` by default) is
**never** the golden baseline -- it is disposable, regeneratable content;
the golden corpus itself is only ever read, never written to.

```bash
python workload04_generator.py --avg-size 100KiB
python workload04_generator.py validate 100KiB
python workload04_generator.py audit-size 100KiB

python workload04_generator.py --avg-size 1MiB
python workload04_generator.py validate 1MiB
python workload04_generator.py audit-size 1MiB

# "What's the biggest average size this machine can safely generate right now?"
python workload04_generator.py max-avg-size

# Check a large target without generating anything:
python workload04_generator.py --avg-size 100GiB --dry-run
```

## Commands reference

```
# One-time setup (verifies + freezes the golden baseline; never modifies it)
python workload04_generator.py init-baseline \
    --resource-list <path> --source-root <path>

# If computed facts (e.g. resource-list entries that don't resolve to a real
# file) differ from the expected 10,000/3/0.03% facts, the tool reports the
# discrepancy and refuses to freeze until you pass --yes to accept it.
python workload04_generator.py init-baseline --yes

# Self-test (regression suite)
python workload04_generator.py self-test

# Pre-flight/feasibility check only, writes nothing
python workload04_generator.py --avg-size 100KiB --dry-run

# Normal generation -- only --avg-size is required
python workload04_generator.py --avg-size 100KiB
python workload04_generator.py --avg-size 1MiB
python workload04_generator.py --avg-size 4MiB

# What's the biggest average size THIS machine can safely generate?
python workload04_generator.py max-avg-size

# Read-only validation of an already-generated tier
python workload04_generator.py validate 100KiB

# Independent, read-only request-weighted size audit (re-stats every
# physical file fresh from disk -- never trusts in-memory generation results)
python workload04_generator.py audit-size 100KiB

# Read-only baseline-vs-generated comparison report
python compare_baseline_vs_generated.py 100KiB 1MiB

# Read-only diagnostic: investigate baseline resource-list entries that
# don't resolve to a physical file
python analyze_missing_baseline_entries.py
```

Every command above also accepts `--output-root <path>` (see
[Output directory](#output-directory-opt)).

Synthetic (non-representative) fallback content is never used silently:
normal generation refuses to start if the frozen baseline has ANY record
with no resolvable source seed. Pass `--allow-synthetic-fallback` to
explicitly opt into it as a diagnostic escape hatch.

Every generation is deterministic (fixed default seed 42, override with
`--seed`); re-running with the same baseline/target/seed reproduces the
same output. Regenerating an existing tier directory requires `--force`.

## Output directory structure

```
/opt/generated_workload04/
    workload04-100KiB/
        gfx/...                     <- payload files, mirroring the baseline's
        htm/...                        own relative directory structure
        js/...
        ...
        workload-resources.txt      <- one URI per line (see wg/common.py
        manifest.csv                   METADATA_FILENAMES -- never counted
        manifest.json                  as a payload file)
        validation-report.txt
        validation-report.json
        diversity-report.json
        size-report.json
        httpstorm-config.ini
        README.md
    workload04-1MiB/
        ...
```

**Why payload files sit directly under `workload04-<TIER>/` and not a
nested `files/` subfolder:** HttpBlaster resolves every request path as the
literal concatenation `path_root + URI` (verified in
`httpblaster/config.cpp::load_content_to_ram()` -- no path rewriting, no
stripping). The generated resource list's URIs already start with
`/workload04-<TIER>/...`. Setting `path_root` to the tier directory's
**parent** (`/opt/generated_workload04/`) makes `path_root + URI` resolve
to exactly `/opt/generated_workload04/workload04-<TIER>/<relpath>`. A
nested `files/` subfolder would introduce a path segment HttpBlaster has no
mechanism to skip, breaking every request with a 404.
`httpstorm-config.ini` in every generated tier sets `path_root` correctly
for you.

"Payload file" vs "metadata file" is decided purely by filename: everything
under a tier directory except the fixed metadata filenames
(`wg/common.py::METADATA_FILENAMES`) counts as a payload file.

## Manifest files

- `manifest.csv` -- one row per generated physical file: `rel_path`,
  `rewritten_uri`, `uri` (original baseline URI), `baseline_decoded_rel`,
  `normalized_rel`, `collision_suffix`, extension/MIME/family, source and
  generated sizes, `has_source_seed`, EICAR/validation flags.
- `manifest.json` -- full reproducibility record: seed, scale factor,
  target size, `collision_count`, checksums.

## Resource-list format

`workload-resources.txt` is **one URI per line, no second column** -- do
NOT add MIME/content-type as a second runtime field (that information
remains available in `manifest.csv`/the report JSON files, never inline in
the runtime resource list). Checked by the hard validation check
`resource_list_one_field_per_line`.

## Storage pre-flight logic

Before ANY payload file is written (for `--dry-run`, real generation, and
`max-avg-size`), `wg/storage.py::run_preflight()` computes a REAL,
filesystem-backed estimate -- never a rough `10,000 x average_size` guess:

- `shutil.disk_usage()` for total/free/available bytes on the actual target
  filesystem (walks up to the nearest existing ancestor if the tier
  directory doesn't exist yet).
- Each planned file's size is rounded UP to the filesystem's own
  allocation unit (`os.statvfs().f_frsize` on POSIX) before summing, since
  10,000 small files' block-rounding overhead is not negligible.
- A safety margin (`wg_common.DEFAULT_DISK_SAFETY_MARGIN = 0.15`, i.e.
  15% headroom) is required beyond the block-rounded estimate.
- If `--force` would replace an existing tier, that tier's actual on-disk
  size is added to the required total (the old and newly-staged tiers
  briefly coexist during atomic generation -- see
  [Atomic generation behavior](#atomic-generation-behavior)) -- the
  pre-flight never assumes the old tier will already be gone.
- Reports BOTH the absolute theoretical maximum and the recommended safe
  maximum; normal generation enforces the SAFE maximum.

Printed before every generation attempt:

```
==================================================
Workload04 Generation Pre-Flight
==================================================
Requested average object size : 100KiB
Physical payload files         : 10,000
Logical resource entries       : 10,000
Estimated payload bytes        : ...
Estimated filesystem usage     : ...
Safety margin                  : 15%
Required (with safety margin)  : ...
Available free space           : ...
Maximum safe avg object size   : ...
Maximum theoretical avg size   : ...
Output root                    : /opt/generated_workload04
Tier directory                 : /opt/generated_workload04/workload04-100KiB
==================================================
PASS: sufficient storage
```

or, on failure:

```
FAIL: insufficient free disk space
Generation aborted before payload creation.
```

**Nothing is generated when the pre-flight fails** -- this is enforced
before the output directory lock is even acquired.

## Safe maximum average size calculation

```
python workload04_generator.py max-avg-size
```

reports (via `wg/storage.py::compute_max_avg_size()`):

- filesystem/device checked, total capacity, free bytes;
- usable bytes after the safety margin;
- the 10,000-physical-file assumption;
- **maximum SAFE average object size** (includes the safety margin --
  this is what normal generation enforces);
- **maximum THEORETICAL average object size** (no safety margin);
- the maximum single-object-size cap (`DEFAULT_MAX_SINGLE_FILE_BYTES`);
  available inodes, if the platform reports them.

The calculation uses the exact SAME scaling model real generation uses
(`wg/pool.py::build_plan()`), via a bisection search over candidate target
sizes rather than an unrelated approximation -- so it stays correct even
though per-file target size is not perfectly linear in the requested
average (the AV-test never-shrink floor and the hard per-file cap both
introduce small nonlinearities near the extremes).

There is **no single universal maximum average object size** for this
tool -- it always depends on the target machine's actual free capacity,
filesystem, and any existing generated tier. Never assume a target "is
supported" merely because the CLI parser accepts the size string; always
check with `--dry-run` or `max-avg-size` first.

## Inode checks

Because the strict model always creates exactly 10,000 physical files (+ a
handful of metadata files), the pre-flight also checks available inodes
where the platform supports it (`os.statvfs().f_favail`, POSIX/Linux only).
If insufficient inodes are available, generation aborts before writing
anything, exactly like the byte-capacity check. On platforms without
`statvfs` (e.g. Windows), the inode check is skipped with a note in the
pre-flight report -- the byte-capacity check remains authoritative.

## Atomic generation behavior

Generation never writes directly into the final tier directory:

1. pre-flight (storage + permissions);
2. acquire a directory-based generation lock on the output root
   (`wg/safety.py::generation_lock()` -- refuses to start a second,
   concurrent generation against the same output root; a lock abandoned by
   a killed/crashed process is automatically reclaimed once it is
   confirmed dead or old enough);
3. generate all payloads + metadata into a hidden staging directory on the
   **same filesystem** as the final tier (`wg/safety.py::staged_generation()`);
4. run full validation against the staged directory;
5. only after a complete PASS, atomically rename the staging directory
   into the final tier location (displacing any previous tier only at
   this final step);
6. clean up the staging directory on any failure.

If `--force` is used against an existing tier, the existing tier is **not**
deleted until the replacement is fully generated and validated -- the
pre-flight's required-space calculation accounts for the old and new tiers
temporarily coexisting on disk.

## Failure / cleanup behavior

- If generation fails at any point (missing seed refused, validation
  FAIL, exception), the staging directory is removed and any previously
  valid tier at the final location is left completely untouched.
- The process always exits non-zero on failure.
- A concurrent generation attempt against the same output root is refused
  outright (`LockHeldError`) rather than racing disk-space accounting.

## Force / replace semantics

`--force` is required to regenerate an existing tier directory. The
pre-flight accounts for the old tier's real on-disk size coexisting with
the newly-staged tier; the old tier is only removed after the new one is
confirmed fully valid (see [Atomic generation behavior](#atomic-generation-behavior)).

## Large targets / no universal maximum size

Because every one of the 10,000 baseline resources becomes its own
physical file, storage scales roughly with `10,000 x average object size`
(plus block-rounding, metadata, and safety-margin overhead -- see
[Storage pre-flight logic](#storage-pre-flight-logic)). Do NOT assume any
specific target (4MiB/16MiB/64MiB/128MiB/250MiB/...) is universally
feasible -- it depends entirely on the target machine's free capacity.
`--dry-run` and real generation are always the authoritative feasibility
check; the tool never attempts a large generation merely because the size
string parsed successfully, and it never silently reduces the object size,
reduces the file count, changes the workload distribution, or falls back
to synthetic content to make a target "fit."

## Format/content fidelity

`wmime/formats.py` provides byte-exact, format-aware generators for JPEG,
GIF, PNG, PDF, ZIP, EXE/DLL (PE), HTML, CSS, JavaScript, XML, and EICAR.
The optional dependencies (`Pillow`, `pypdf`, `pefile`) improve fidelity
for JPEG/PNG (`Pillow`), PDF (`pypdf`), and EXE/DLL (`pefile`); without
them, those specific families fall back to a generic, still
size-correct, still-validating `binary_fallback` content path -- this is
expected and documented, never a silent, unexplained downgrade.

Every `validate`/generation report includes a **Format Fidelity** section
(`format_fidelity_fallback_count` check, always informational -- never a
PASS/FAIL gate on its own) reporting exactly how many records used
`binary_fallback` and, if any, which intended families they belong to.
Install the optional dependencies (see
[Runtime and test dependencies](#runtime-and-test-dependencies)) for full
fidelity, or treat a nonzero, expected count (matching missing optional
deps on this machine) as normal.

A `binary_fallback` count can also be nonzero even with all optional
dependencies installed: each format generator additionally falls back
per-record when that specific record's SOURCE SEED bytes (from the
external golden corpus) don't actually look like the claimed format (e.g.
a `.jpg`-extension baseline file whose bytes don't start with the JPEG SOI
marker) -- this reflects pre-existing, intentional edge cases in the
golden corpus itself, not a generator defect, and is unrelated to which
optional dependencies are installed.

## Troubleshooting

**Common installation failures**

- `install_linux.sh` reports "No Python >= 3.8 found": install Python 3.8+
  (see [Manual installation](#manual-installation--recovery-if-install_linuxsh-fails)).
- "Python venv module is not available": install `python3-venv` (Debian/Ubuntu)
  or ensure your `python3` package includes `venv` (RHEL family).
- `pip install` failures inside the venv: check `/tmp/wg_pip_*.log` (paths
  are printed by the installer) for the underlying pip error.

**Common storage failures**

- Pre-flight `FAIL: insufficient free disk space`: free up space, choose a
  smaller `--avg-size`, or point `--output-root`/`WORKLOAD04_OUTPUT_ROOT`
  at a filesystem with more free capacity. Use `max-avg-size` to find the
  largest size that currently fits.
- Pre-flight `FAIL: insufficient free inodes`: the target filesystem is
  out of inodes independent of byte capacity (common on filesystems
  formatted with very few inodes for their size) -- reformat with more
  inodes, or choose a different filesystem/output root.

**Common permission failures**

- "Cannot create output root": the current user cannot create
  `/opt/generated_workload04` (or your `--output-root`) -- see
  [Permissions](#permissions) for the exact remediation commands.
- "exists but is not writable": ownership is wrong -- `chown` it to the
  user that will run generation.

## Using generated workloads with performance test tools

A generated tier is a plain directory of payload files plus a
one-URI-per-line resource list (`workload-resources.txt`) and metadata
(`manifest.csv`/`.json`, validation/size reports). **It is tool-agnostic**:
copy `workload04-<TIER>/` to any machine and point whatever HTTP load
generator you use at it. This project does not need to remain installed on
that machine, and none of the tools below are prerequisites for using this
generator.

```
Generated tier root  : /opt/generated_workload04/workload04-100KiB/
Runtime resource list: /opt/generated_workload04/workload04-100KiB/workload-resources.txt
```

### HTTP Storm / HttpBlaster (optional)

```ini
path_root = /opt/generated_workload04/
path_file = /opt/generated_workload04/workload04-100KiB/workload-resources.txt
```

Use `path_file` (HttpBlaster/HTTP Storm accepts it as an alias for
`path_files`; every generated `httpstorm-config.ini` uses `path_file`).
`path_root` must be the tier directory's **parent**, since resource-list
URIs already include the `/workload04-<TIER>/` segment (see
[Output directory structure](#output-directory-structure)). A 404 storm
almost always means `path_root`/`path_file` are misconfigured relative to
each other.

### Sledgehammer (optional)

Every generated tier includes an `httpstorm-config.ini` with a
`[sledgehammer]` section (`path_file = .../workload-resources.txt`) as a
ready-to-use config snippet, purely as a convenience -- Sledgehammer is not
required to use this generator, and this generator is not required to use
Sledgehammer.

### IxLoad / custom or generic HTTP load tools (optional)

Any tool that accepts a list of request paths/URIs and a document root can
consume `workload-resources.txt` + the payload tree directly: point the
tool's document root at the tier directory, and its path/URL list at
`workload-resources.txt` (one path per line, no header, no second column).
MIME/content-type per file is derivable from the file extension, or read
from `manifest.csv` if a tool needs it explicitly.

**How to copy a generated workload to another machine:** `rsync`/`scp` the
entire `workload04-<TIER>/` directory (it is fully self-contained --
payload files + resource list + manifests + reports) to the target
machine, e.g. `/opt/generated_workload04/workload04-<TIER>/`, then point
your chosen tool's config at it as shown above. No files from this
generator project need to travel with it.

**Smoke-test workflow:** generate -> `validate` -> `audit-size` on the
generation machine, copy the tier to the target machine, start your load
tool against it, and confirm HTTP 200s (not 404s).

## Known validated tiers

| Tier | Target avg | Measured request-weighted avg | Error | Result |
|---|---|---|---|---|
| 100KiB | 100.00 KiB | 100.00 KiB | -0.00% | PASS |
| 1MiB | 1.00 MiB | 1024.00 KiB | -0.00% | PASS |

Both tiers: 10,000 physical files, 10,000 logical entries, EICAR 3/3
(0.03%), zero whitespace in physical paths/runtime URIs, zero synthetic
fallback, zero missing references, `self-test`/`validate`/`audit-size`/
`compare_baseline_vs_generated.py` all PASS. Regenerated via the generator
itself (`--force`, never manual renaming) after every generator change.

Larger targets (4MiB/16MiB/64MiB/128MiB/250MiB/...) are NOT universally
declared feasible -- always check with `--dry-run`/`max-avg-size` against
the machine you intend to generate on (see
[Large targets](#large-targets--no-universal-maximum-size)).

## Test status

`self-test` runs `tests/test_regression.py` via pytest -- covering baseline
counts, 1:1 physical/logical preservation, EICAR exactness, MIME/extension
distribution, path root correctness, determinism, percent-decoded
filesystem resolution, path-traversal rejection, zero-missing-baseline,
zero-synthetic-fallback, the independent size audit's 3% tolerance, the
one-field-per-line resource-list format, whitespace-free physical paths
and runtime URIs, deterministic collision handling, exactly-10,000 unique
generated paths after normalization, storage pre-flight (insufficient- and
sufficient-space cases), permission-failure handling, `--dry-run` writing
zero files, `max-avg-size` sanity, atomic staged-generation commit/rollback,
generation-lock concurrency and stale-lock recovery, and a hard-coded
Windows-path regression scan of the tool's own source. Run it yourself for
the current pass count and timing on your machine:

```bash
python workload04_generator.py self-test
```

## Fresh Linux acceptance procedure

The authoritative acceptance test for a newly-copied checkout is a real
Linux machine, in this order:

```bash
# 1. Copy this standalone workload04_generator/ directory to the machine.
# 2. Copy (or otherwise make reachable) the external golden Workload04
#    corpus, e.g. to /opt/workload04 + /opt/workload04-resources.txt.

cd workload04_generator
./install_linux.sh
source .venv/bin/activate

python workload04_generator.py init-baseline \
  --source-root /opt/workload04 \
  --resource-list /opt/workload04-resources.txt

python workload04_generator.py self-test
python workload04_generator.py max-avg-size

python workload04_generator.py --avg-size 256KiB --dry-run
python workload04_generator.py --avg-size 256KiB
python workload04_generator.py validate 256KiB
python workload04_generator.py audit-size 256KiB
```

`256KiB` is used as the first new-machine generation target deliberately --
it is neither of the two bundled `reference_workloads/` tiers, so a PASS
here proves the standalone checkout can independently plan, pre-flight,
generate, stage, validate, and audit a brand-new tier end to end, with no
help from anything pre-generated.

## Final acceptance / release checklist

Before relying on a freshly-copied checkout on a new machine, confirm:

- [ ] `./install_linux.sh` reports `RESULT: PASS`
- [ ] `python workload04_generator.py init-baseline ...` completes with
      `missing_files: 0`
- [ ] `self-test` -- all tests PASS
- [ ] `validate <tier>` -- PASS for every tier you plan to use
- [ ] `audit-size <tier>` -- PASS, error within +/-3%
- [ ] `compare_baseline_vs_generated.py <tiers...>` -- counts/EICAR match,
      scale ratios uniform across the size spectrum
- [ ] `max-avg-size` runs and reports a sane (nonzero, safe <= theoretical)
      result for the target machine
- [ ] `--avg-size <large size> --dry-run` correctly reports NOT FEASIBLE
      when it should, and FEASIBLE when it should
- [ ] Generated output confirmed under `/opt/generated_workload04` (or the
      explicitly configured `--output-root`/`WORKLOAD04_OUTPUT_ROOT`)
- [ ] No hard-coded Windows/developer-specific paths in the tool's own
      source (`test_no_hardcoded_windows_paths_in_source`)

---

## Appendix: design history and verified source-code facts

The sections below record *why* certain design decisions were made, kept
for future maintainers; they do not change any of the invariants above,
and none of them make any specific tool a prerequisite for this generator.

### Rationale behind the optional Sledgehammer/HttpBlaster integration format

These historical facts (from the tools' own source, observed during
original development of this generator inside a workspace that also
contained them) explain why the resource-list/`path_root`+URI format was
chosen -- they describe the OPTIONAL consumers documented in
[Using generated workloads with performance test tools](#using-generated-workloads-with-performance-test-tools),
not a dependency of this generator:

- **Sledgehammer**'s path selection is `fast_rand(seed) % path_count` --
  uniform random over the array index, on every request. Duplicate lines
  are its only weighting mechanism; physical line ORDER has no effect on
  selection probability. Since the golden baseline has no duplicate lines
  (weight 1 everywhere), this generator's resource list is a pure 1:1,
  order-preserving rewrite of the original -- compatible with that model,
  but not dependent on it.
- **HttpBlaster/HTTP Storm** loads every resource-list entry into RAM
  once, keyed by the exact runtime URI, then resolves `path_root + URI`
  verbatim (no path rewriting). This is why generated payload files sit
  directly under `workload04-<TIER>/` rather than a nested `files/`
  subfolder (see [Output directory structure](#output-directory-structure)) --
  a format choice that also works for any other tool using the same
  root+URI convention, not something specific to those two tools.

### Request-weighted average: authoritative calculation

The request-weighted average object size is calculated in `wg/sizeaudit.py`,
independently of in-memory generation results: it reads the generated
`workload-resources.txt` (one sample per LOGICAL request), resolves each
line to its physical file, and re-`stat()`s it fresh from disk. This is the
authoritative metric -- not an average over unique files (only equivalent
today because the baseline has weight=1 everywhere) and never a
specific load-tool's own runtime metric. PASS tolerance is +/-3%
(`wg_common.DEFAULT_SIZE_TOLERANCE_PCT`), shared by the generation retry
loop, `validate`, and `audit-size` so all three always agree.

### Baseline discrepancy handling history

**Resolved (2026-09-19):** 219 of the 10,000 resource-list entries have
percent-encoded filenames (e.g. `Kopie%20(3)%20von%20b1.jpg`) referencing
real on-disk files stored under their literal, unencoded names (e.g.
`Kopie (3) von b1.jpg`). The resolver previously joined the URI to
`source_root` literally, without percent-decoding, so it could never find
these 219 real files. Fixed in `wmime/common.py::resolve_uri_to_path()`
(decodes ONLY for filesystem lookup; a path-traversal guard rejects any
decoded `..` segment). `init-baseline` now completes with
`missing_files = 0` without needing `--yes`.

### Path-handling design history (2026-09-20 phases)

Two earlier designs were superseded, in order, by the current
[Path normalization rules](#path-normalization-rules):

1. *Original*: physical filename reused the still-percent-encoded relative
   path directly -- produced literal `%20` in physical filenames, which
   HttpBlaster's decoded-filename-on-disk lookup could never find (HTTP
   404s in smoke tests).
2. *First fix*: physical filename was percent-DECODED
   (`gfx/gfx2/Kopie (3) von b1.jpg`) while the runtime URI stayed
   percent-ENCODED (`Kopie%20(3)%20von%20b1.jpg`) -- two different
   strings, kept in sync manually.
3. *Current*: full whitespace normalization (`wg/pathnorm.py`) -- physical
   path and runtime URI are now the SAME whitespace-free string, with
   deterministic collision handling, eliminating percent-encoding
   entirely from generated output (see
   [Path normalization rules](#path-normalization-rules) /
   [Collision handling](#collision-handling) above for the current,
   authoritative behavior).

## Known limitations

- `install_linux.sh` and the fresh-Linux `256KiB` acceptance procedure
  have not been executed on a real Linux machine as part of every change
  to this project -- run the [Fresh Linux acceptance procedure](#fresh-linux-acceptance-procedure)
  yourself on your target machine before relying on a new checkout.
- Inode pre-flight (`os.statvfs`) is POSIX/Linux-only; on other platforms
  it is skipped with a note in the pre-flight report, and byte-capacity
  pre-flight remains authoritative.
- The bundled `reference_workloads/` tiers were generated on a prior
  machine and are provided read-only for sanity-checking; they are not
  regenerated automatically by anything in this project.
- `format_fidelity_fallback_count` is informational only -- it does not
  fail validation on its own, since a legitimate cause (missing optional
  `Pillow`/`pypdf`/`pefile`) is common and expected on a minimal install.


