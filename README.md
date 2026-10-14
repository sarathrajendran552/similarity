# matchbiotmp

A self-contained command-line tool that compares two biometric fingerprint
**templates** (not images) and prints the matching score, using ZKTeco's
ZKFinger SDK (algorithm version 2.54).

The tool has **no external dependencies beyond a normal Linux C/C++ runtime**
(glibc, libstdc++, and libudev): the ZKFinger SDK shared libraries are embedded
directly inside the executable and extracted to a private temporary directory
at startup. It does **not** need a fingerprint sensor, a USB dongle, or any
manually installed license file.

```
$ ./matchbiotmp --csv template_report.csv --emp 36006 --emp 36006
36006: 2 template(s)
36006: 2 template(s)
Best score: 94
Pairs compared: 2
Threshold: 35
Result: MATCH
```

---

## Why this exists

A ZK Fingerprint (ZK10 / SilkID) template is a proprietary blob produced by the
ZKFinger algorithm. Only the SDK's matching engine can score two templates
against each other — there is no public format specification.

Out of the box the matching engine is unusable without hardware:

1. `ZKFPM_Init()` only initializes the *capture* library and returns `-2` when no
   sensor is attached.
2. The matching algorithm (`libzkfinger10.so` → Innovatrics IDKit inside
   `libidkit.so.2`) is licensed. `IEngine_InitModule()` fails with code `1129`
   ("license") unless a valid license is installed or a dongle answers a
   challenge-response check (`CZKFPHelper::CheckValue` → `sensorCheckLic`).

The fix used here: ZKTeco ships a **ZKSOFTWARE license blob embedded inside
`libzkfinger10.so`** (196 bytes at file offset `0x8240`). Applying it directly
with `IEngine_InitWithLicense(blob, 196)` raises the engine's user limit to
`50000` and lets it initialize with no hardware at all. The tool then calls the
SDK's internal `CZKFPHelper::InitFP(width, height)` entry point to bring up the
algorithm, and uses the public `ZKFPM_DBMatch` API to score templates.

---

## Repository layout

```
.
├── README.md
├── build.sh                 # builds the standalone ELF
├── src/
│   └── matchbiotmp.c        # the whole tool
├── matchbiotmp              # built, self-contained x86-64 ELF (~7.7 MB)
├── matchall.py              # batch all-vs-all driver (resumable)
├── requirements.txt         # Python deps for matchall.py (none)
├── build/                   # objcopy objects + copied .so files (build artifacts)
├── template_report.csv      # example template database
└── ZKTecoSDK/               # original ZKTeco SDK distribution
    ├── SDK/
    │   ├── lib-x64/         # libraries embedded by the build
    │   └── lib-x86/
    └── Demo&Doc/
        ├── c/demo/          # reference C demo + headers
        └── c/doc/           # SDK PDF documentation
```

## Requirements

**Runtime** (target machine): Linux x86-64 with glibc, `libstdc++.so.6`,
`libgcc_s.so.1`, and `libudev.so.1` (the latter is pulled in by the bundled
`libusb-1.0`; USB is never actually exercised for offline matching). No SDK,
sensor, or license is required.

**Build** (only when rebuilding): `gcc`, GNU `objcopy`, and the
`ZKTecoSDK/SDK/lib-x64/` directory.

## Build

```sh
./build.sh
# -> built: /path/to/matchbiotmp
```

`build.sh` copies each required shared library, converts it to an object file
with `objcopy -I binary`, and links everything into one executable. The result
depends only on the system C/C++ runtime:

```sh
$ ldd matchbiotmp
    linux-vdso.so.1
    libc.so.6 => /usr/lib/libc.so.6
    /lib64/ld-linux-x86-64.so.2
```

---

## Usage

```
matchbiotmp [options] <T1> <T2>                 match two templates
matchbiotmp [options] --csv F --emp A --emp B   match two employees
matchbiotmp [options] --csv F --all             pairwise score matrix
```

Run `./matchbiotmp --help` for the full option list.

### Options

| Option | Description |
| --- | --- |
| `-i, --input-format raw\|b64\|hex\|auto` | How to interpret *file* contents (default `auto`) |
| `-t, --threshold N` | Match threshold, `0..100` (default `35`) |
| `--width N` | Algorithm image width (default `256`) |
| `--height N` | Algorithm image height (default `360`) |
| `--csv FILE` | Template CSV (`emp_code,actual_template,...`) |
| `--emp CODE` | Employee code; give it **twice** to compare two employees |
| `--row N` | 1-based data row; give it twice |
| `--all` | Score every pair among CSV rows |
| `--limit N` | Max rows for `--all` (default `10`) |
| `-q, --quiet` | Print only the score |
| `-v, --verbose` | Extra diagnostics (per-pair scores) on stderr |
| `-h, --help` | Help |

### Input formats

Each template can be supplied as:

* **A file path** — the content is auto-detected as raw binary, Base64 text, or
  hex text. Force the interpretation with `-i raw|b64|hex`.
* **A literal string** — interpreted as Base64, or hex if it looks like hex.
* **An explicit prefix** — `b64:<base64>`, `hex:<hex>`, or `raw:<bytes>`.

```sh
./matchbiotmp a.tpl b.tpl                       # raw template files
./matchbiotmp a.b64 b.b64                       # Base64 text files
./matchbiotmp "b64:4d77..." "hex:4d775353..."   # inline literals
./matchbiotmp -i b64 a.tpl b.tpl                # force Base64 interpretation
```

### CSV mode

The CSV is expected to have a header containing `emp_code` and
`actual_template` columns (extra columns are ignored):

```csv
emp_code,actual_template,template_hash,occurrences
36006,4d77SS21...,a1b2...,1
```

Match two specific employees:

```sh
./matchbiotmp --csv template_report.csv --emp 36006 --emp 135817
```

### Multiple templates per employee

An employee may have several enrolled templates (e.g. multiple fingers, or
several captures). When `--emp` (or `--row`) is given twice, **every** template
of the first selection is compared against **every** template of the second,
and the reported score is the **best (highest)** of those comparisons — the
usual "any enrolled finger matches" verification rule. A template is never
compared with itself.

```sh
./matchbiotmp --csv template_report.csv --emp 58973 --emp 58973 -v
58973: 3 template(s)
58973: 3 template(s)
  58973 template #1979 vs 58973 template #2427: 0
  58973 template #1979 vs 58973 template #5258: 12
  ...
Best score: 12
Pairs compared: 6
Threshold: 35
Result: NO MATCH
```

### Pairwise matrix

```sh
./matchbiotmp --csv template_report.csv --all --limit 5
# pairwise match scores (threshold 35), 5 templates
emp_code,144472,33825,143476,33133,141182
144472,100,0,0,0,0
33825,0,100,0,0,0
...
```

### Score semantics

* Scores are integers in the range **0–100** (100 = identical template).
* ZKFinger's `ZKFPM_DBMatch` normally clamps its result to `0` whenever the
  similarity is below the engine's internal threshold. This tool lowers that
  internal threshold so it can always report the **raw similarity**, then makes
  the match decision itself.
* `Result: MATCH` when the score is `>= --threshold` (default `35`).
* `-q` prints just the number, which is handy in scripts.

### Exit codes

* `0` — comparison performed successfully (regardless of match/no-match).
* `1` — error (bad arguments, unreadable/malformed template, employee not found).

---

## Batch comparison — `matchall.py`

`matchall.py` is a resumable driver that compares **every employee against
every other employee** in a CSV by invoking `matchbiotmp` once per template
pair. It only uses the Python standard library (see `requirements.txt`).

`--min-score` is **compulsory**: only employee pairs whose best score is
**greater than** this value are written to the results file. All other
comparisons are still performed and counted, they are just not recorded
individually — so the results file stays small and contains only the pairs you
care about.

```sh
python3 matchall.py --csv template_report.csv --min-score 50   # only scores >50
python3 matchall.py --csv template_report.csv --min-score 50 --limit-emps 100
python3 matchall.py --csv template_report.csv --min-score 0 --include-self
```

For each unordered pair of distinct `emp_code`s it compares **all** template
combinations of the two employees and records the best score. With
`--include-self` it additionally compares an employee's own templates against
each other (useful for capture-quality checks).

### Status display

Progress is shown as a checklist (redrawn in place on a terminal, printed as a
log when redirected):

```
[1/4] ✓ Loading template database
[2/4] ✓ Building comparison plan
[3/4] ⠋ Matching employees 1234/22653 (5.4%)
[4/4] ○ Writing summary
```

### Cancel-safe and resumable

The run is interrupted with Ctrl-C / `SIGINT` / `SIGTERM`. On interruption (or
any time) the following files are kept in the current working directory:

| File | Purpose |
| --- | --- |
| `matchall_resume.json` | signature of the exact command + progress |
| `matchall_results.csv` | matches only: one row per pair scoring above `--min-score` (`idx,emp_a,emp_b,n_templates_a,n_templates_b,best_score`) |
| `matchall_summary.json` | final statistics (compared, matches, filtered out) and top pairs |
| `matchall_errors.log` | individual comparison errors |

Run the **same command again** and it detects the matching signature and
resumes from the first unfinished pair. Completed work is tracked in
`matchall_resume.json` (written after every pair by default); the results file
records each match with its work-unit `idx`, so on restart any partially written
line or not-yet-committed row is discarded — the run is safe even against a hard
`SIGKILL`. If you run a **different** command, the old state/results are backed
up (`*.bak.<timestamp>`) and a fresh run starts; use `--fresh` to force this.

All filenames are configurable (`--state-file`, `--results-file`, …). Run
`python3 matchall.py --help` for every option (`--min-score`, `--threshold`,
`--delay`, `--timeout`, `--state-every`, `--top`, `--dry-run`, …).

---

## How it works

1. **Startup / re-exec.** The binary writes its nine embedded shared libraries
   (`libzkfp.so`, `libzkfinger10.so`, `libsilkidcap.so`, `libidkit.so.2`,
   `libsqlite3.so.0`, `libcrypto.so.0.9.8`, `libiomp5.so`, `libusb-0.1.so.4`,
   `libusb-1.0.so.0`) to a fresh `mkdtemp` directory, then re-executes itself
   via `/proc/self/exe` with `LD_LIBRARY_PATH` pointing there. The re-exec is
   required because glibc computes its library search path at process start and
   ignores `LD_LIBRARY_PATH` changes made at runtime. The directory is removed
   on exit.
2. **License.** `libidkit.so.2` is loaded and the embedded ZKSOFTWARE license
   blob is applied with `IEngine_InitWithLicense()`.
3. **Engine bring-up.** `libzkfp.so` is loaded; `ZKFPM_DBInit()` obtains the
   engine context and the internal symbol `_ZN11CZKFPHelper6InitFPEii`
   (`CZKFPHelper::InitFP`) initializes the algorithm at the requested image
   size — no sensor needed.
4. **Matching.** `ZKFPM_DBMatch()` scores each template pair. The tool handles
   Base64/hex/raw decoding, CSV parsing, the cross-product logic, thresholding,
   and formatting.
5. **Noise suppression.** The SDK prints diagnostics (`Algorithm Version:...`)
   to stdout; the tool redirects them to `/dev/null` during initialization
   unless `-v` is given.

---

## Limitations and data notes

* **x86-64 Linux only.** The build embeds the SDK's `lib-x64` libraries. An
  `lib-x86` set exists in the SDK but is not used.
* **Template format.** The tool matches the `SS21` template container used by
  the ZKFinger 10 / SilkID algorithm (the format in `template_report.csv`).
* Some rows in the sample CSV store multiple finger templates as a JSON object
  (`{"0": "...", "11": "..."}`) using a different `ZKFiX` container that this
  SDK's `ZKFPM_DBMatch` does not accept; those rows are skipped with a clear
  error if selected.
* Many duplicate `emp_code` rows in `template_report.csv` are **different
  fingers** of the same person rather than repeat captures, so they legitimately
  score low against each other. Use the multi-template comparison to surface the
  best pair.

## Legal / licensing

This project bundles and uses ZKTeco's ZKFinger SDK and the license blob
contained in it. The tool is intended for use with
templates produced by a compatible ZKTeco device.
