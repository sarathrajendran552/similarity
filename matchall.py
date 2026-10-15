#!/usr/bin/env python3
"""
matchall.py - compare employees in a template CSV by driving the `matchbiotmp`
binary. It either compares every employee against every other employee, or
searches a small "query" CSV against a larger database CSV. The binary runs as
a single long-lived ``--serve`` process so the matching engine is initialised
once and reused for every comparison.

A compulsory --min-score is required: every template pair whose score is
*greater than or equal to* that value is written to the results file. Pairs
below it are still compared but not recorded individually. All matching
template pairs are recorded (not just the single best per employee pair or per
query).

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
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# bump this whenever the results-file schema or comparison semantics change, so
# stale resume state from an older version is rejected instead of silently
# resumed (which would skip work and truncate the results file)
_FORMAT_VERSION = 2
# one row per matching template pair (tpl_a/tpl_b are 1-based within employee)
_RESULT_HEADER = ["idx", "emp_a", "tpl_a", "emp_b", "tpl_b", "score"]
# query/search mode: one row per matching (query template, database template)
_QUERY_RESULT_HEADER = ["idx", "query_emp", "query_tpl", "db_emp", "db_tpl",
                        "score"]

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
def _server_error(err: str) -> bool:
    """True when ``err`` means the persistent server must be restarted."""
    return err == "timeout" or err.startswith("server")


class Matcher:
    """A single long-lived ``matchbiotmp --serve`` process.

    The engine (and the embedded SDK libraries) is initialised exactly once
    when the process starts; every subsequent comparison is a line written to
    stdin and a single reply line read from stdout. This removes the
    per-pair process spawn / library extraction that dominated the old
    one-shot invocation, while keeping all orchestration in Python.
    """

    def __init__(self, binary, threshold, width, height, timeout):
        self._cmd = [binary, "--serve", "-t", str(threshold),
                     "--width", str(width), "--height", str(height)]
        self.timeout = timeout
        self.proc = None
        self._spawn()

    def _spawn(self):
        self.proc = subprocess.Popen(
            self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)

    def restart(self, log_error):
        self.close()
        self._spawn()
        log_error("restarted matcher process")

    def score(self, t1, t2):
        """Return (score, error). score is None on failure."""
        try:
            self.proc.stdin.write(f"b64:{t1} b64:{t2}\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            return None, f"server write failed: {exc}"
        ready, _, _ = select.select([self.proc.stdout], [], [], self.timeout)
        if not ready:
            return None, "timeout"
        line = self.proc.stdout.readline()
        if not line:
            return None, "server closed unexpectedly"
        line = line.strip()
        if line.startswith("S "):
            try:
                return int(line[2:]), None
            except ValueError:
                return None, f"bad score line: {line[:80]}"
        if line.startswith("E "):
            return None, line[2:][:200]
        return None, f"unexpected reply: {line[:80]}"

    def close(self):
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.write("Q\n")
                self.proc.stdin.flush()
                self.proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        finally:
            self.proc = None


def compare_group(matcher, ta, tb, same_emp, min_score, log_error):
    """Compare every template of ``ta`` against every template of ``tb``.

    Returns (best_score, rows, combos, errors, aborted), where ``rows`` holds
    ``(tpl_a, tpl_b, score)`` (1-based template indices) for every combination
    with ``score >= min_score``.
    """
    best = -1
    rows = []
    combos = 0
    errors = 0
    for ia, x in enumerate(ta):
        for ib, y in enumerate(tb):
            if same_emp and ia == ib:
                continue
            if stop_requested():
                return best, rows, combos, errors, True
            score, err = matcher.score(x, y)
            combos += 1
            if err is not None:
                errors += 1
                log_error(f"comparison error: {err}")
                if _server_error(err):
                    matcher.restart(log_error)
                continue
            if score > best:
                best = score
            if score >= min_score:
                rows.append((ia + 1, ib + 1, score))
    if best < 0:
        best = 0
    return best, rows, combos, errors, False


def search_group(matcher, query_templates, db_entries, min_score, log_error):
    """Score every query template against every database template.

    ``db_entries`` is a list of ``(emp_code, db_tpl, template)`` where
    ``db_tpl`` is the 1-based template index within that employee. Returns
    (best_score, best_emp, rows, combos, errors, aborted), where ``rows`` holds
    ``(query_tpl, db_emp, db_tpl, score)`` for every combination with
    ``score >= min_score``.
    """
    best = -1
    best_emp = ""
    rows = []
    combos = 0
    errors = 0
    for qi, x in enumerate(query_templates):
        for emp, db_tpl, y in db_entries:
            if stop_requested():
                return best, best_emp, rows, combos, errors, True
            score, err = matcher.score(x, y)
            combos += 1
            if err is not None:
                errors += 1
                log_error(f"comparison error: {err}")
                if _server_error(err):
                    matcher.restart(log_error)
                continue
            if score > best:
                best = score
                best_emp = emp
            if score >= min_score:
                rows.append((qi + 1, emp, db_tpl, score))
    if best < 0:
        best = 0
    return best, best_emp, rows, combos, errors, False


# --------------------------------------------------------------------------- #
# persistence                                                                 #
# --------------------------------------------------------------------------- #
def file_fingerprint(path: Path):
    st = path.stat()
    return {"path": str(path.resolve()), "size": st.st_size,
            "mtime_ns": st.st_mtime_ns}


def make_signature(args, binary, n_emps, total, query_path=None):
    data = {
        "format_version": _FORMAT_VERSION,
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
        "mode": "query" if query_path is not None else "all",
    }
    if query_path is not None:
        data["query_csv"] = file_fingerprint(query_path)
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


def sanitize_results(results_path: Path, completed: int,
                     header=None):
    """Tidy the results file and create it with a header if missing.

    Only rows above the minimum score are recorded, so the number of data
    lines no longer equals the number of completed work units. ``completed``
    (kept in the state file) is the source of truth for resume; here we drop
    any partial trailing line and any row belonging to an uncommitted unit
    (``idx >= completed``) so a hard kill can never duplicate or lose a match.
    """
    header = header or _RESULT_HEADER
    if not results_path.exists():
        with open(results_path, "w", newline="") as f:
            csv.writer(f, lineterminator="\n").writerow(header)
        return
    with open(results_path, "r", newline="") as f:
        rows = list(csv.reader(f))
    clean = [header]
    for row in rows[1:]:
        if len(row) != len(header):
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
def collect_summary(results_path: Path, units_compared, min_score, top_n,
                    best_pair):
    """Build the all-vs-all summary from the results file.

    Every data line is a matching template pair (score >= ``min_score``); the
    number of work units (employee pairs) with at least one match is counted
    from the distinct ``idx`` values.
    """
    matches = 0
    matched_units = set()
    top: list = []
    with open(results_path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 6:
                continue
            matches += 1
            matched_units.add(row[0])
            score = int(row[5])
            if top_n > 0:
                heapq.heappush(top, (score, row[1], row[2], row[3], row[4]))
                if len(top) > top_n:
                    heapq.heappop(top)

    top_sorted = sorted(top, reverse=True)
    return {
        "units_compared": units_compared,
        "min_score": min_score,
        "template_pairs_matched": matches,
        "units_matched": len(matched_units),
        "best": {"score": best_pair[0], "emp_a": best_pair[1],
                 "emp_b": best_pair[2]}
        if units_compared and best_pair[0] >= 0 else None,
        "top": [{"score": s, "emp_a": a, "tpl_a": ta, "emp_b": b, "tpl_b": tb}
                for s, a, ta, b, tb in top_sorted],
    }


def print_summary(summary, summary_path: Path):
    total = summary["units_compared"]
    print()
    print(f"Units compared : {total}")
    print(f"Template pairs (>= {summary['min_score']}) : "
          f"{summary['template_pairs_matched']}")
    print(f"Units matched  : {summary['units_matched']}")
    if summary["best"]:
        b = summary["best"]
        print(f"Best score     : {b['score']}  ({b['emp_a']} vs {b['emp_b']})")
    if summary["top"]:
        print(f"Top {len(summary['top'])} template pairs:")
        for item in summary["top"]:
            print(f"  {item['score']:>3}  "
                  f"{item['emp_a']}[{item['tpl_a']}] vs "
                  f"{item['emp_b']}[{item['tpl_b']}]")
    print(f"Summary written to {summary_path.name}")


def collect_query_summary(results_path: Path, queries_compared, min_score,
                          top_n, best_pair):
    """Build the summary for a query/search run.

    Every data line is a matching (query template, database template) pair
    scoring >= ``min_score``.
    """
    matches = 0
    matched_queries = set()
    top: list = []
    with open(results_path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 6:
                continue
            matches += 1
            matched_queries.add(row[0])
            score = int(row[5])
            if top_n > 0:
                heapq.heappush(top, (score, row[1], row[2], row[3], row[4]))
                if len(top) > top_n:
                    heapq.heappop(top)

    top_sorted = sorted(top, reverse=True)
    return {
        "mode": "query",
        "queries": queries_compared,
        "min_score": min_score,
        "template_pairs_matched": matches,
        "queries_matched": len(matched_queries),
        "unmatched": queries_compared - len(matched_queries),
        "best": {"score": best_pair[0], "emp_a": best_pair[1],
                 "emp_b": best_pair[2]}
        if queries_compared and best_pair[0] >= 0 else None,
        "top": [{"score": s, "emp_a": a, "tpl_a": ta, "emp_b": b, "tpl_b": tb}
                for s, a, ta, b, tb in top_sorted],
    }


def print_query_summary(summary, summary_path: Path):
    print()
    print(f"Queries        : {summary['queries']}")
    print(f"Template pairs (>= {summary['min_score']}) : "
          f"{summary['template_pairs_matched']}")
    print(f"Queries matched: {summary['queries_matched']}")
    print(f"No match       : {summary['unmatched']}")
    if summary["best"]:
        b = summary["best"]
        print(f"Best score     : {b['score']}  ({b['emp_a']} -> {b['emp_b']})")
    if summary["top"]:
        print(f"Top {len(summary['top'])} template pairs:")
        for item in summary["top"]:
            print(f"  {item['score']:>3}  "
                  f"{item['emp_a']}[{item['tpl_a']}] -> "
                  f"{item['emp_b']}[{item['tpl_b']}]")
    print(f"Summary written to {summary_path.name}")


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="matchall.py",
        description="Compare employees in a template CSV with matchbiotmp: "
                    "all-vs-all, or a query CSV against a database CSV "
                    "(resumable).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--csv", required=True,
                   help="input template CSV (the database)")
    p.add_argument("--query-csv", default=None,
                   help="search mode: score every employee in this (small) CSV "
                        "against --csv and record every matching template pair")
    p.add_argument("--matchbiotmp", default=None,
                   help="path to the matchbiotmp binary")
    p.add_argument("--min-score", type=int, required=True,
                   help="compulsory: record every template pair whose score "
                        "is >= this value (all comparisons are still run)")
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
                   help="number of top scoring template pairs to report")
    p.add_argument("--state-file", default="matchall_resume.json",
                   help="resume/state file (in the working directory)")
    p.add_argument("--results-file", default=None,
                   help="per-unit results file "
                        "(default: matchall_query_results.csv in --query-csv "
                        "mode, else matchall_results.csv)")
    p.add_argument("--errors-file", default="matchall_errors.log",
                   help="log for individual comparison errors")
    p.add_argument("--summary-file", default=None,
                   help="JSON summary written at the end "
                        "(default: matchall_query_summary.json in --query-csv "
                        "mode, else matchall_summary.json)")
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
    query_path = Path(args.query_csv) if args.query_csv else None
    if query_path is not None and not query_path.is_file():
        sys.exit(f"error: query CSV not found: {query_path}")
    query_mode = query_path is not None
    binary = resolve_binary(args.matchbiotmp)

    if args.results_file is None:
        args.results_file = ("matchall_query_results.csv" if query_mode
                             else "matchall_results.csv")
    if args.summary_file is None:
        args.summary_file = ("matchall_query_summary.json" if query_mode
                             else "matchall_summary.json")

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
    db_emps = sorted(groups.keys())
    if args.limit_emps > 0 and not query_mode:
        db_emps = db_emps[:args.limit_emps]
    db_templates = [groups[e] for e in db_emps]
    n = len(db_emps)

    q_emps: list[str] = []
    q_templates: list[list[str]] = []
    if query_mode:
        qgroups = load_records(query_path, args.include_json, warn)
        q_emps = sorted(qgroups.keys())
        if args.limit_emps > 0:
            q_emps = q_emps[:args.limit_emps]
        q_templates = [qgroups[e] for e in q_emps]
    status.step(1)

    # ---- plan ------------------------------------------------------------- #
    # each db entry is (emp_code, 1-based template index within emp, template)
    db_entries = [(db_emps[i], ti + 1, t)
                  for i in range(n)
                  for ti, t in enumerate(db_templates[i])]
    self_units = []
    if query_mode:
        total = len(q_emps)
        header = _QUERY_RESULT_HEADER
    else:
        self_units = [i for i in range(n) if len(db_templates[i]) > 1] \
            if args.include_self else []
        total = n * (n - 1) // 2 + len(self_units)
        header = _RESULT_HEADER

    signature, sig_data = make_signature(args, binary, n, total, query_path)

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
            sanitize_results(results_path, completed, header)
            resumed = completed > 0
            print(f"Resuming: {completed}/{total} already done "
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

    if completed == 0:
        sanitize_results(results_path, 0, header)    # create header / drop stale

    if not resumed:
        if query_mode:
            print(f"Plan: {total} query employee(s) searched against "
                  f"{n} database employee(s) / {len(db_entries)} template(s) "
                  f"(min score {args.min_score}, "
                  f"threshold {args.threshold}).")
        else:
            print(f"Plan: {n} employee(s), {total} pair(s) to compare "
                  f"(min score {args.min_score}, "
                  f"threshold {args.threshold}).")

    if args.dry_run:
        status.finish()
        if query_mode:
            print(f"Dry run complete. Would search {total} query employee(s) "
                  f"against {len(db_entries)} database template(s) "
                  f"(recording template pairs with score >= {args.min_score}); "
                  f"{completed} already done.")
        else:
            print(f"Dry run complete. Would compare {total} units "
                  f"(recording template pairs with score >= {args.min_score}); "
                  f"{completed} already done.")
        return 0

    # ---- step 3: match ---------------------------------------------------- #
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

    matcher = Matcher(binary, args.threshold, args.width, args.height,
                      args.timeout)
    try:
        with open(results_path, "a", newline="") as wf:
            writer = csv.writer(wf, lineterminator="\n")
            if query_mode:
                for qi in range(completed, total):
                    if stop_requested():
                        break
                    best, best_emp, rows, _combos, _errs, aborted = \
                        search_group(matcher, q_templates[qi], db_entries,
                                     args.min_score, log_error)
                    if aborted:
                        break
                    for qt, db_emp, db_tpl, score in rows:
                        writer.writerow([qi, q_emps[qi], qt, db_emp, db_tpl,
                                         score])
                    if rows:
                        wf.flush()
                    done = qi + 1
                    status.set_progress(done, total)
                    if best > best_pair[0]:
                        best_pair = (best, q_emps[qi], best_emp)
                    if args.delay:
                        time.sleep(args.delay)
                    if done % max(1, args.state_every) == 0:
                        write_state(state_path, signature, sig_data, total,
                                    done, results_path.name, best_pair)
            else:
                for idx, i, j, is_self in iter_work(n, args.include_self,
                                                    self_units, completed):
                    if stop_requested():
                        break
                    ta, tb = db_templates[i], db_templates[j]
                    best, rows, _combos, _errs, aborted = compare_group(
                        matcher, ta, tb, is_self, args.min_score, log_error)
                    if aborted:
                        break
                    for tpl_a, tpl_b, score in rows:
                        writer.writerow([idx, db_emps[i], tpl_a, db_emps[j],
                                         tpl_b, score])
                    if rows:
                        wf.flush()
                    done = idx + 1
                    status.set_progress(done, total)
                    if best > best_pair[0]:
                        best_pair = (best, db_emps[i], db_emps[j])
                    if args.delay:
                        time.sleep(args.delay)
                    if done % max(1, args.state_every) == 0:
                        write_state(state_path, signature, sig_data, total,
                                    done, results_path.name, best_pair)
    finally:
        matcher.close()

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
    if query_mode:
        summary = collect_query_summary(results_path, total, args.min_score,
                                        args.top, best_pair)
    else:
        summary = collect_summary(results_path, total, args.min_score,
                                  args.top, best_pair)
    summary_path.write_text(json.dumps(summary, indent=2))
    status.finish()
    if query_mode:
        print_query_summary(summary, summary_path)
    else:
        print_summary(summary, summary_path)
    print(f"Elapsed: {elapsed:.1f}s "
          f"({elapsed / max(1, done):.2f}s per unit)")
    if errors_written:
        print(f"{errors_written} error(s) logged to {errors_path.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
