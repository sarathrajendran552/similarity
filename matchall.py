#!/usr/bin/env python3
"""
matchall.py - compare every employee in a template CSV against every other
employee by repeatedly invoking the `matchbiotmp` binary:

    matchbiotmp [options] <T1> <T2>

A compulsory --min-score is required: only pairs whose best score is *greater
than* that value are written to the results file. Every other comparison is
still performed and counted, but is not recorded individually.

The job is fully deterministic and *resumable*. Progress, results and a
"signature" of the exact command are written to the current working directory.
If the script is interrupted (Ctrl-C / SIGTERM / SIGKILL) simply run it again
with the same arguments: it detects the matching signature and continues where
it left off. Running a *different* command backs up the previous state and
starts fresh.

Only the Python standard library is used (see requirements.txt).
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import heapq
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_RESULT_HEADER = ["idx", "emp_a", "emp_b", "n_templates_a", "n_templates_b",
                  "best_score"]

_STOP = False


def _on_signal(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True


def stop_requested() -> bool:
    return _STOP


# --------------------------------------------------------------------------- #
# status / progress display                                                   #
# --------------------------------------------------------------------------- #
class Status:
    """Prints a checklist with a spinner, e.g.

        [1/4] ✓ Loading template database
        [2/4] ✓ Building comparison plan
        [3/4] ⠋ Matching employees 123/465 (26.5%)
        [4/4] ○ Writing summary

    On a TTY the whole block is redrawn in place. When output is redirected
    only completed steps and periodic progress lines are written, so the log
    stays clean.
    """

    def __init__(self, steps, enabled=True):
        self.steps = list(steps)
        self.state = ["pending"] * len(self.steps)
        self.progress = None
        self.enabled = enabled
        self.is_tty = sys.stdout.isatty()
        self._frame = 0
        self._drawn = 0
        self._last_draw = 0.0
        self._last_progress = 0.0
        self._printed_done = [False] * len(self.steps)

    # -- rendering ---------------------------------------------------------- #
    def _symbol(self, i):
        s = self.state[i]
        if s == "done":
            return "✓"
        if s == "active":
            return SPINNER[self._frame % len(SPINNER)]
        return "○"

    def _line(self, i):
        n = len(self.steps)
        extra = ""
        if self.state[i] == "active" and self.progress:
            cur, total = self.progress
            if total > 0:
                extra = f" {cur}/{total} ({100.0 * cur / total:.1f}%)"
        return f"[{i + 1}/{n}] {self._symbol(i)} {self.steps[i]}{extra}"

    def _block(self):
        return [self._line(i) for i in range(len(self.steps))]

    def _print(self, text):
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def _draw_block(self, force=False):
        if not self.enabled:
            return
        now = time.time()
        if not force and now - self._last_draw < 0.1:
            return
        self._last_draw = now
        self._frame += 1
        lines = self._block()
        if self._drawn:
            sys.stdout.write(f"\x1b[{self._drawn}A")
        for ln in lines:
            sys.stdout.write("\x1b[2K" + ln + "\n")
        sys.stdout.flush()
        self._drawn = len(lines)

    def _print_done(self, upto):
        for k in range(upto):
            if not self._printed_done[k]:
                self._print(f"[{k + 1}/{len(self.steps)}] ✓ {self.steps[k]}")
                self._printed_done[k] = True

    # -- public API --------------------------------------------------------- #
    def step(self, i, label=None):
        if label is not None:
            self.steps[i] = label
        self.state = ["done" if k < i else ("active" if k == i else "pending")
                      for k in range(len(self.steps))]
        self.progress = None
        if not self.enabled:
            return
        if self.is_tty:
            self._draw_block(force=True)
        else:
            self._print_done(i)

    def set_progress(self, cur, total):
        self.progress = (cur, total)
        if not self.enabled:
            return
        if self.is_tty:
            self._draw_block()
        else:
            now = time.time()
            if now - self._last_progress >= 2.0 or cur >= total:
                self._last_progress = now
                active = next((k for k, s in enumerate(self.state)
                               if s == "active"), None)
                if active is not None:
                    self._print(self._line(active))

    def finish(self):
        self.progress = None
        self.state = ["done"] * len(self.steps)
        if not self.enabled:
            return
        if self.is_tty:
            self._draw_block(force=True)
            if self._drawn:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._drawn = 0
        else:
            self._print_done(len(self.steps))


# --------------------------------------------------------------------------- #
# input handling                                                              #
# --------------------------------------------------------------------------- #
def clean_template(s: str) -> str:
    return "".join(s.split())


def is_base64(s: str) -> bool:
    if len(s) < 16:
        return False
    try:
        base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
        return True
    except Exception:
        return False


def extract_templates(field: str, include_json: bool):
    """Return the base64 template(s) contained in a CSV field.

    Most rows hold a single base64 template. Some rows wrap several templates
    in a JSON object like {"0": "...", "1": "..."} using a different container
    that matchbiotmp does not understand; those are skipped unless
    ``include_json`` is set.
    """
    field = field.strip()
    if field.startswith("{"):
        if not include_json:
            return []
        try:
            obj = json.loads(field)
        except Exception:
            return []
        if isinstance(obj, dict):
            return [clean_template(str(v)) for v in obj.values()]
        return []
    return [clean_template(field)]


def load_records(csv_path: Path, include_json: bool, warn):
    """Parse the CSV into an ordered {emp_code: [templates]} mapping."""
    groups: dict[str, list[str]] = {}
    skipped_json = 0
    invalid = 0
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        emp_col = next((c for c in fields
                        if c.strip().lower() == "emp_code"), None)
        tpl_col = next((c for c in fields
                        if c.strip().lower() == "actual_template"), None)
        if not emp_col or not tpl_col:
            sys.exit("error: CSV must contain 'emp_code' and "
                     "'actual_template' columns")
        for row in reader:
            emp = (row.get(emp_col) or "").strip()
            raw = row.get(tpl_col) or ""
            if not emp or not raw.strip():
                continue
            if raw.lstrip().startswith("{") and not include_json:
                skipped_json += 1
                continue
            for tpl in extract_templates(raw, include_json):
                if is_base64(tpl):
                    bucket = groups.setdefault(emp, [])
                    if tpl not in bucket:          # de-duplicate within emp
                        bucket.append(tpl)
                else:
                    invalid += 1
    if skipped_json:
        warn(f"skipped {skipped_json} row(s) with a JSON multi-template "
             f"container (use --include-json to force)")
    if invalid:
        warn(f"skipped {invalid} template(s) that were not valid base64")
    return groups


# --------------------------------------------------------------------------- #
# work plan                                                                   #
# --------------------------------------------------------------------------- #
def unrank_pair(k: int, n: int):
    """Map a linear index to an unordered pair (i, j), i < j, lexicographic."""
    i = 0
    while True:
        block = n - 1 - i
        if k < block:
            return i, i + 1 + k
        k -= block
        i += 1


def iter_work(n, include_self, self_units, start):
    """Yield (index, i, j, is_self) work units starting at ``start``."""
    idx = start
    cross = n * (n - 1) // 2
    if idx < cross:
        i, j = unrank_pair(idx, n)
        while idx < cross:
            yield idx, i, j, False
            idx += 1
            j += 1
            if j >= n:
                i += 1
                j = i + 1
    if include_self:
        for s, i in enumerate(self_units):
            gi = cross + s
            if gi < start:
                continue
            yield gi, i, i, True


# --------------------------------------------------------------------------- #
# matching                                                                    #
# --------------------------------------------------------------------------- #
def run_match(binary, t1, t2, threshold, width, height, timeout):
    """Return (score, error). score is None on failure."""
    cmd = [binary, "-t", str(threshold), "--width", str(width),
           "--height", str(height), "b64:" + t1, "b64:" + t2]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, f"spawn failed: {exc}"

    score = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("Score:"):
            try:
                score = int(line.split(":", 1)[1])
            except ValueError:
                pass
    if score is None:
        err = (proc.stderr.strip() or proc.stdout.strip() or
               f"exit {proc.returncode}").splitlines()
        return None, (err[-1][:200] if err else "no score produced")
    return score, None


def compare_group(binary, ta, tb, same_emp, threshold, width, height,
                  timeout, log_error):
    """Compare every template of ``ta`` against every template of ``tb``.

    Returns (best_score, combos, errors, aborted).
    """
    best = -1
    combos = 0
    errors = 0
    for ia, x in enumerate(ta):
        for ib, y in enumerate(tb):
            if same_emp and ia == ib:
                continue
            if stop_requested():
                return best, combos, errors, True
            score, err = run_match(binary, x, y, threshold, width, height,
                                   timeout)
            combos += 1
            if err is not None:
                errors += 1
                log_error(f"comparison error: {err}")
                continue
            if score > best:
                best = score
    if best < 0:
        best = 0
    return best, combos, errors, False


# --------------------------------------------------------------------------- #
# persistence                                                                 #
# --------------------------------------------------------------------------- #
def file_fingerprint(path: Path):
    st = path.stat()
    return {"path": str(path.resolve()), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns}


def make_signature(args, binary, n_emps, total):
    data = {
        "csv": file_fingerprint(Path(args.csv)),
        "binary": file_fingerprint(Path(binary)),
        "threshold": args.threshold,
        "min_score": args.min_score,
        "width": args.width,
        "height": args.height,
        "include_self": args.include_self,
        "include_json": args.include_json,
        "limit_emps": args.limit_emps,
        "n_emps": n_emps,
        "total": total,
    }
    blob = json.dumps(data, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest(), data


def write_state(path: Path, signature, sig_data, total, completed,
                results_file, best_pair=(-1, "", "")):
    payload = {
        "signature": signature,
        "details": sig_data,
        "total": total,
        "completed": completed,
        "results_file": results_file,
        "best": {"score": best_pair[0], "emp_a": best_pair[1],
                 "emp_b": best_pair[2]},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def sanitize_results(results_path: Path, completed: int):
    """Tidy the results file and create it with a header if missing.

    Only pairs above the minimum score are recorded, so the number of data
    lines no longer equals the number of completed work units. ``completed``
    (kept in the state file) is the source of truth for resume; here we drop
    any partial trailing line and any row belonging to an uncommitted unit
    (``idx >= completed``) so a hard kill can never duplicate or lose a match.
    """
    if not results_path.exists():
        with open(results_path, "w", newline="") as f:
            csv.writer(f, lineterminator="\n").writerow(_RESULT_HEADER)
        return
    with open(results_path, "r", newline="") as f:
        rows = list(csv.reader(f))
    clean = [_RESULT_HEADER]
    for row in rows[1:]:
        if len(row) != len(_RESULT_HEADER):
            break                                   # partial trailing line
        try:
            idx = int(row[0])
        except ValueError:
            break                                   # not our format; restart
        if idx >= completed:
            break                                   # uncommitted; reprocess
        clean.append(row)
    if len(clean) != len(rows):
        with open(results_path, "w", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows(clean)


def backup(path: Path):
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path.rename(path.with_name(path.name + f".bak.{stamp}"))


# --------------------------------------------------------------------------- #
# summary                                                                     #
# --------------------------------------------------------------------------- #
def collect_summary(results_path: Path, pairs_compared, min_score, top_n,
                    best_pair):
    """Build the summary from the (already filtered) results file.

    Every data line in ``results_path`` is a recorded match (score above
    ``min_score``); everything else is only counted via ``pairs_compared``.
    """
    matches = 0
    top: list = []
    with open(results_path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 6:
                continue
            matches += 1
            score = int(row[5])
            if top_n > 0:
                heapq.heappush(top, (score, row[1], row[2]))
                if len(top) > top_n:
                    heapq.heappop(top)

    top_sorted = sorted(top, reverse=True)
    non_matches = pairs_compared - matches
    return {
        "pairs_compared": pairs_compared,
        "min_score": min_score,
        "matches": matches,
        "non_matches": non_matches,
        "match_rate_pct": round(100.0 * matches / pairs_compared, 4)
        if pairs_compared else 0.0,
        "best": {"score": best_pair[0], "emp_a": best_pair[1],
                 "emp_b": best_pair[2]}
        if pairs_compared and best_pair[0] >= 0 else None,
        "top": [{"score": s, "emp_a": a, "emp_b": b}
                for s, a, b in top_sorted],
    }


def print_summary(summary, summary_path: Path):
    total = summary["pairs_compared"]
    print()
    print(f"Pairs compared : {total}")
    print(f"Matches (> {summary['min_score']}) : {summary['matches']}"
          + (f" ({summary['match_rate_pct']}%)" if total else ""))
    print(f"Filtered out   : {summary['non_matches']}")
    if summary["best"]:
        b = summary["best"]
        print(f"Best score     : {b['score']}  ({b['emp_a']} vs {b['emp_b']})")
    if summary["top"]:
        print(f"Top {len(summary['top'])}:")
        for item in summary["top"]:
            print(f"  {item['score']:>3}  {item['emp_a']} vs {item['emp_b']}")
    print(f"Summary written to {summary_path.name}")


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="matchall.py",
        description="Run matchbiotmp for every pair of employees in a "
                    "template CSV (resumable).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--csv", required=True, help="input template CSV")
    p.add_argument("--matchbiotmp", default=None,
                   help="path to the matchbiotmp binary")
    p.add_argument("--min-score", type=int, required=True,
                   help="compulsory: only record pairs whose best score is "
                        "GREATER than this value (all other comparisons are "
                        "still run and counted)")
    p.add_argument("-t", "--threshold", type=int, default=35,
                   help="match threshold passed to matchbiotmp")
    p.add_argument("--width", type=int, default=256)
    p.add_argument("--height", type=int, default=360)
    p.add_argument("--include-self", action="store_true",
                   help="also compare an employee's own templates against "
                        "each other")
    p.add_argument("--include-json", action="store_true",
                   help="parse JSON multi-template rows instead of skipping")
    p.add_argument("--limit-emps", type=int, default=0,
                   help="only use the first N employees (0 = all)")
    p.add_argument("--delay", type=float, default=0.0,
                   help="seconds to sleep between work units")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="per-comparison timeout in seconds")
    p.add_argument("--top", type=int, default=10,
                   help="number of top scoring pairs to report")
    p.add_argument("--state-file", default="matchall_resume.json",
                   help="resume/state file (in the working directory)")
    p.add_argument("--results-file", default="matchall_results.csv",
                   help="per-pair results file")
    p.add_argument("--errors-file", default="matchall_errors.log",
                   help="log for individual comparison errors")
    p.add_argument("--summary-file", default="matchall_summary.json",
                   help="JSON summary written at the end")
    p.add_argument("--state-every", type=int, default=1,
                   help="rewrite the state file every N work units")
    p.add_argument("--fresh", action="store_true",
                   help="ignore and back up any existing state")
    p.add_argument("--no-status", action="store_true",
                   help="disable the animated status display")
    p.add_argument("--dry-run", action="store_true",
                   help="build the plan and print it without matching")
    return p.parse_args(argv)


def resolve_binary(arg):
    candidates = []
    if arg:
        candidates.append(Path(arg))
    candidates.append(Path(__file__).resolve().with_name("matchbiotmp"))
    candidates.append(Path.cwd() / "matchbiotmp")
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c.resolve())
    sys.exit("error: cannot find an executable matchbiotmp "
             "(use --matchbiotmp PATH)")


def main(argv=None):
    global _STOP
    args = parse_args(argv)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        sys.exit(f"error: CSV not found: {csv_path}")
    binary = resolve_binary(args.matchbiotmp)

    status = Status(
        ["Loading template database",
         "Building comparison plan",
         "Matching employees",
         "Writing summary"],
        enabled=not args.no_status)

    warnings: list[str] = []

    def warn(msg):
        warnings.append(msg)
        print(f"warning: {msg}", file=sys.stderr)
        # a stray stderr line would desync the in-place status block
        if status.is_tty:
            status._drawn = 0

    # ---- step 1: load ----------------------------------------------------- #
    status.step(0)
    groups = load_records(csv_path, args.include_json, warn)
    emps = sorted(groups.keys())
    if args.limit_emps > 0:
        emps = emps[:args.limit_emps]
    templates = [groups[e] for e in emps]
    n = len(emps)
    status.step(1)

    self_units = [i for i in range(n) if len(templates[i]) > 1] \
        if args.include_self else []
    total = n * (n - 1) // 2 + len(self_units)

    signature, sig_data = make_signature(args, binary, n, total)

    state_path = Path(args.state_file)
    results_path = Path(args.results_file)
    errors_path = Path(args.errors_file)
    summary_path = Path(args.summary_file)

    if total == 0:
        status.finish()
        print("Nothing to do: not enough employees/templates to compare.")
        return 0

    # ---- resume ----------------------------------------------------------- #
    completed = 0
    resumed = False
    best_pair = (-1, "", "")
    if state_path.exists() and not args.fresh:
        try:
            old = json.loads(state_path.read_text())
        except Exception:
            old = None
        if old and old.get("signature") == signature:
            completed = min(int(old.get("completed", 0)), total)
            b = old.get("best") or {}
            best_pair = (int(b.get("score", -1)), b.get("emp_a", ""),
                         b.get("emp_b", ""))
            sanitize_results(results_path, completed)
            resumed = completed > 0
            print(f"Resuming: {completed}/{total} pairs already done "
                  f"({100.0 * completed / total:.1f}%).")
        else:
            print("Existing state does not match this command; "
                  "backing it up and starting fresh.")
            for p in (state_path, results_path):
                backup(p)
            completed = 0
    elif args.fresh and state_path.exists():
        for p in (state_path, results_path):
            backup(p)

    if completed == 0 and not results_path.exists():
        sanitize_results(results_path, 0)    # create with header

    if not resumed:
        print(f"Plan: {n} employee(s), {total} pair(s) to compare "
              f"(min score {args.min_score}, threshold {args.threshold}).")

    if args.dry_run:
        status.finish()
        print(f"Dry run complete. Would compare {total} pairs "
              f"(recording scores > {args.min_score}); "
              f"{completed} already done.")
        return 0

    # ---- step 3: compare -------------------------------------------------- #
    start_time = time.time()
    errors_written = 0
    _STOP = False
    write_state(state_path, signature, sig_data, total, completed,
                results_path.name, best_pair)

    def log_error(msg):
        nonlocal errors_written
        errors_written += 1
        with open(errors_path, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")

    status.step(2)
    status.set_progress(completed, total)
    done = completed

    with open(results_path, "a", newline="") as wf:
        writer = csv.writer(wf, lineterminator="\n")
        for idx, i, j, is_self in iter_work(n, args.include_self, self_units,
                                            completed):
            if stop_requested():
                break
            ta, tb = templates[i], templates[j]
            best, combos, errs, aborted = compare_group(
                binary, ta, tb, is_self, args.threshold, args.width,
                args.height, args.timeout, log_error)
            if aborted:
                break
            if best > args.min_score:
                writer.writerow([idx, emps[i], emps[j], len(ta), len(tb),
                                 best])
                wf.flush()
            done = idx + 1
            status.set_progress(done, total)
            if best > best_pair[0]:
                best_pair = (best, emps[i], emps[j])
            if args.delay:
                time.sleep(args.delay)
            if done % max(1, args.state_every) == 0:
                write_state(state_path, signature, sig_data, total, done,
                            results_path.name, best_pair)

    # ---- interruption? ---------------------------------------------------- #
    if stop_requested() or done < total:
        write_state(state_path, signature, sig_data, total, done,
                    results_path.name, best_pair)
        print()
        print(f"Interrupted at {done}/{total} "
              f"({100.0 * done / total:.1f}%). State saved to "
              f"{state_path.name}.")
        print("Run the same command again to resume.")
        return 130

    # ---- step 4: summary -------------------------------------------------- #
    write_state(state_path, signature, sig_data, total, total,
                results_path.name, best_pair)
    status.step(3)
    elapsed = time.time() - start_time
    summary = collect_summary(results_path, total, args.min_score, args.top,
                              best_pair)
    summary_path.write_text(json.dumps(summary, indent=2))
    status.finish()
    print_summary(summary, summary_path)
    print(f"Elapsed: {elapsed:.1f}s "
          f"({elapsed / max(1, done):.2f}s per pair)")
    if errors_written:
        print(f"{errors_written} error(s) logged to {errors_path.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
