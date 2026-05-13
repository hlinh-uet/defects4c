#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project nginx/njs.

This script mirrors the php metadata pipeline at a project-specific level:
  - phase A builds with ASAN and records buggy/fixed outcomes
  - phase B builds the buggy overlay with GCOV and records covered functions
  - metadata/raw outputs share the Unified-Debugging schema used by php/cJSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import fcntl  # type: ignore
except ImportError:  # Windows host debugging; Docker/Linux uses fcntl.
    fcntl = None


SCRIPT_DIR = Path(__file__).resolve().parent
DEFECTS4C_ROOT = SCRIPT_DIR.parents[2]
PROJECT_NAME = "nginx___njs"
OUTPUT_SHORT_NAME = "nginx_njs"
REMOTE_URL = "https://github.com/nginx/njs"
BUGS_JSON = SCRIPT_DIR / "bugs_list_new.json"
COVERAGE_PARSER_VERSION = 4
DEFAULT_TEST_TIMEOUT = 120

ASAN_CFLAGS = "-fsanitize=address -g -Wno-error -fno-omit-frame-pointer"
ASAN_LDFLAGS = "-fsanitize=address"
COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage -Wno-error"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage"

CRASH_MARKERS = (
    "AddressSanitizer",
    "==ERROR",
    "SIGSEGV",
    "DEADLYSIGNAL",
    "Segmentation fault",
)

GCOV_SIGNAL_FLUSH_C = r"""
#define _GNU_SOURCE
#include <signal.h>
#include <stdlib.h>
#include <unistd.h>

extern void __gcov_dump(void) __attribute__((weak));

#define GCOV_ALT_STACK_SIZE (1024 * 1024)
static unsigned char altstack_mem[GCOV_ALT_STACK_SIZE] __attribute__((aligned(16)));

static void gcov_signal_handler(int sig, siginfo_t *info, void *ucontext)
{
    (void) info;
    (void) ucontext;

    if (__gcov_dump) {
        __gcov_dump();
    }

    signal(sig, SIG_DFL);
    raise(sig);
}

__attribute__((constructor))
static void install_gcov_signal_handlers(void)
{
    stack_t ss;
    struct sigaction sa;

    ss.ss_sp = altstack_mem;
    ss.ss_size = sizeof(altstack_mem);
    ss.ss_flags = 0;
    sigaltstack(&ss, NULL);

    sa.sa_sigaction = gcov_signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;

    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    sigaction(SIGILL, &sa, NULL);
    sigaction(SIGFPE, &sa, NULL);
}
"""

GCOV_SIGNAL_FLUSH_H = r"""
#ifndef DEFECTS4C_GCOV_FLUSH_ON_SIGNAL_H
#define DEFECTS4C_GCOV_FLUSH_ON_SIGNAL_H

#define _GNU_SOURCE
#include <signal.h>
#include <stdlib.h>
#include <unistd.h>

extern void __gcov_dump(void);

#define DEFECTS4C_GCOV_ALT_STACK_SIZE (64 * 1024)

static void *defects4c_gcov_altstack_mem;

static void defects4c_gcov_signal_handler(int sig, siginfo_t *info, void *ucontext)
{
    (void) info;
    (void) ucontext;

    __gcov_dump();

    signal(sig, SIG_DFL);
    raise(sig);
}

__attribute__((constructor))
static void defects4c_install_gcov_signal_handlers(void)
{
    stack_t ss;
    struct sigaction sa;

    defects4c_gcov_altstack_mem = malloc(DEFECTS4C_GCOV_ALT_STACK_SIZE);
    if (defects4c_gcov_altstack_mem != NULL) {
        ss.ss_sp = defects4c_gcov_altstack_mem;
        ss.ss_size = DEFECTS4C_GCOV_ALT_STACK_SIZE;
        ss.ss_flags = 0;
        sigaltstack(&ss, NULL);
    }

    sa.sa_sigaction = defects4c_gcov_signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;

    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    sigaction(SIGILL, &sa, NULL);
    sigaction(SIGFPE, &sa, NULL);
}

#endif
"""

_GCOV_FUNC_CALLED_RE = re.compile(
    r"^function\s+(?P<name>.+?)\s+called\s+(?P<calls>\d+)\s+returned",
    re.MULTILINE,
)
_GCOV_FUNC_LINES_RE = re.compile(
    r"Function '(?P<name>[^']+)'\nLines executed:(?P<pct>[0-9.]+)%",
    re.MULTILINE,
)


@dataclass
class BugEntry:
    sha_after: str
    sha_before: str
    src_files: List[str]
    test_files: List[str]
    build_flags: List[str]
    test_template: str
    cve_name: Optional[str]
    type_id: str
    raw: dict
    output_bug_id: str = ""

    @property
    def bug_id(self) -> str:
        return self.type_id or f"{PROJECT_NAME}@{self.sha_after}"

    @property
    def safe_bug_id(self) -> str:
        base = self.output_bug_id or self.bug_id
        return base.replace("@", "__").replace("/", "__")


@dataclass
class TestEntry:
    test_id: str
    kind: str
    relpath: str = ""
    poc_name: str = ""
    source: str = "metadata"


@dataclass
class TestResult:
    test_id: str
    outcome: str
    outcome_fixed: str = ""
    fail_reason: str = ""
    actual_output: str = ""
    expected_output: str = ""
    covered_functions: List[str] = field(default_factory=list)


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _detect_default_out_root() -> Path:
    if _safe_exists(Path("/out")):
        return Path("/out") / PROJECT_NAME
    return DEFECTS4C_ROOT / "out_tmp_dirs" / PROJECT_NAME


def _detect_default_metadata_dir() -> Path:
    if _safe_exists(Path("/out")):
        return Path("/out") / "unified_debugging" / OUTPUT_SHORT_NAME / "metadata"
    return DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / OUTPUT_SHORT_NAME / "metadata"


def _detect_default_raw_dir() -> Path:
    if _safe_exists(Path("/out")):
        return Path("/out") / "unified_debugging" / OUTPUT_SHORT_NAME / "raw"
    return DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / OUTPUT_SHORT_NAME / "raw"


DEFAULT_OUT_ROOT = _detect_default_out_root()
DEFAULT_METADATA_DIR = _detect_default_metadata_dir()
DEFAULT_RAW_DIR = _detect_default_raw_dir()


def run(cmd, *, cwd=None, env=None, check=False, timeout=None, capture=True):
    args = [str(x) for x in cmd]
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exp:
        out = exp.stdout or ""
        err = exp.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        return 124, out, f"{err}\nTimeoutExpired: {exp}".strip()
    except UnicodeDecodeError as exp:
        return 1, "", f"UnicodeDecodeError: {exp}"

    rc = proc.returncode
    out = proc.stdout or "" if capture else ""
    err = proc.stderr or "" if capture else ""
    if check and rc != 0:
        raise RuntimeError(
            f"Command failed ({rc}): {' '.join(shlex.quote(a) for a in args)}\n"
            f"stdout: {out[-2000:]}\nstderr: {err[-2000:]}"
        )
    return rc, out, err


def which(bin_name: str) -> Optional[str]:
    return shutil.which(bin_name)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _acquire_single_run_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = lock_path.open("a+", encoding="utf-8")
    if fcntl is not None:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fp.seek(0)
            owner = fp.read().strip()
            fp.close()
            raise RuntimeError(f"Dang co process khac chay (lock: {lock_path}) {owner}")
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp


def _lock_path_for_metadata_dir(metadata_dir: Path) -> Path:
    key = hashlib.md5(str(metadata_dir).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_nginx_njs.{key}.lock"


def load_bugs() -> List[BugEntry]:
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for item in data:
        files = item.get("files") or {}
        c_compile = item.get("c_compile") or {}
        type_info = item.get("type") or {}
        bugs.append(
            BugEntry(
                sha_after=item.get("commit_after") or "",
                sha_before=item.get("commit_before") or "",
                src_files=list(files.get("src") or []),
                test_files=list(files.get("test") or []),
                build_flags=list(c_compile.get("build_flags") or []),
                test_template=c_compile.get("test") or "",
                cve_name=type_info.get("name") or type_info.get("id"),
                type_id=type_info.get("id") or item.get("commit_after") or "",
                raw=item,
            )
        )

    counts = Counter(b.bug_id for b in bugs)
    for bug in bugs:
        if counts[bug.bug_id] > 1:
            bug.output_bug_id = f"{bug.bug_id}__{bug.sha_after[:12]}"
        else:
            bug.output_bug_id = bug.bug_id
    return bugs


def repo_dir_for_bug(out_root: Path, bug: BugEntry) -> Path:
    return out_root / f"git_repo_dir_{bug.safe_bug_id}"


def legacy_repo_dir_for_bug(out_root: Path, bug: BugEntry) -> Path:
    return out_root / f"git_repo_dir_{bug.sha_after}"


def ensure_repo(bug: BugEntry, out_root: Path, *, clone: bool) -> Path:
    repo_dir = repo_dir_for_bug(out_root, bug)
    legacy_repo_dir = legacy_repo_dir_for_bug(out_root, bug)
    if (repo_dir / ".git").exists():
        return repo_dir
    if (legacy_repo_dir / ".git").exists():
        if repo_dir.exists():
            raise FileExistsError(f"Repo path exists but is not a git repo: {repo_dir}")
        log(f"  [repo] rename legacy repo {legacy_repo_dir.name} -> {repo_dir.name}")
        legacy_repo_dir.rename(repo_dir)
        return repo_dir
    if not clone:
        raise FileNotFoundError(f"Missing repo {repo_dir}. Run --prepare-repos --clone or add --clone.")

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if not repo_dir.exists():
        run(["git", "clone", "--no-checkout", REMOTE_URL, str(repo_dir)], check=True)
    run(["git", "fetch", "origin", bug.sha_before, bug.sha_after], cwd=repo_dir, check=True)
    return repo_dir


def checkout_commit(repo_dir: Path, sha: str) -> None:
    run(["git", "reset", "--hard"], cwd=repo_dir, check=True)
    run(["git", "clean", "-fdx"], cwd=repo_dir, check=True)
    run(["git", "checkout", sha], cwd=repo_dir, check=True)


def checkout_buggy(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)
    if bug.sha_before and bug.src_files:
        run(["git", "checkout", "--force", bug.sha_before, "--", *bug.src_files], cwd=repo_dir, check=True)


def checkout_fixed(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)


def _shell_quote_join(items: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in items)


def _configure(
    repo_dir: Path,
    bug: BugEntry,
    *,
    asan: bool,
    coverage: bool,
    gcov_flush_header: Optional[Path] = None,
) -> bool:
    flags = list(bug.build_flags)
    if asan and "--address-sanitizer=YES" not in flags:
        flags.insert(0, "--address-sanitizer=YES")
    if coverage:
        cov_cflags = COV_CFLAGS
        if gcov_flush_header is not None:
            cov_cflags = f"{cov_cflags} -include {gcov_flush_header}"
        flags.extend([f"--cc-opt={cov_cflags}", f"--ld-opt={COV_LDFLAGS}"])

    cmd = ["./configure", *flags, "--build-dir=build"]
    rc, out, err = run(cmd, cwd=repo_dir, timeout=120)
    if rc == 0:
        return True

    fallback = ["./configure", *flags]
    rc2, out2, err2 = run(fallback, cwd=repo_dir, timeout=120)
    if rc2 != 0:
        log(f"  [configure] failed rc={rc2}\n{(out + err + out2 + err2)[-2000:]}")
        return False
    return True


def compile_njs(
    repo_dir: Path,
    bug: BugEntry,
    *,
    jobs: int,
    asan: bool,
    coverage: bool,
    gcov_flush_header: Optional[Path] = None,
) -> bool:
    if not _configure(repo_dir, bug, asan=asan, coverage=coverage, gcov_flush_header=gcov_flush_header):
        return False

    cflags = COV_CFLAGS if coverage else (ASAN_CFLAGS if asan else "-g -O0 -Wno-error")
    env = os.environ.copy()
    if asan:
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")

    for cmd in (
        ["make", "-j", str(jobs), f"CFLAGS={cflags}"],
        ["make", "-j", str(jobs), "build/njs_unit_test", f"CFLAGS={cflags}"],
    ):
        rc, out, err = run(cmd, cwd=repo_dir, env=env, timeout=900)
        if rc != 0:
            log(f"  [build] failed rc={rc}: {_shell_quote_join(cmd)}\n{(out + err)[-3000:]}")
            return False

    if not (repo_dir / "build" / "njs").exists():
        log("  [build] missing build/njs after successful make")
        return False
    if not (repo_dir / "build" / "njs_unit_test").exists():
        log("  [build] missing build/njs_unit_test after successful make")
        return False
    return True


def _poc_from_test_template(template_name: str) -> str:
    if not template_name:
        return ""
    path = SCRIPT_DIR / template_name
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(cve_\d{4}_\d+_poc\.js)", text)
    return match.group(1) if match else ""


def _add_entry(entries: List[TestEntry], seen: set, entry: TestEntry) -> None:
    if entry.test_id in seen:
        return
    seen.add(entry.test_id)
    entries.append(entry)


def discover_tests(repo_dir: Path, bug: BugEntry, *, test_scope: str, max_tests: int) -> List[TestEntry]:
    entries: List[TestEntry] = []
    seen = set()

    for rel in bug.test_files:
        rel = str(rel).replace("\\", "/")
        if rel.endswith(".c"):
            _add_entry(entries, seen, TestEntry(test_id=rel, kind="unit_full", relpath=rel))
        elif rel.endswith((".js", ".t.js")):
            _add_entry(entries, seen, TestEntry(test_id=rel, kind="js", relpath=rel))

    poc_name = _poc_from_test_template(bug.test_template)
    if poc_name:
        _add_entry(
            entries,
            seen,
            TestEntry(test_id=f"poc:{bug.bug_id}", kind="poc", poc_name=poc_name, source="override"),
        )

    if test_scope == "all":
        extra = [
            p.relative_to(repo_dir).as_posix()
            for p in sorted((repo_dir / "test").rglob("*.t.js"))
        ]
        if max_tests > 0:
            extra = extra[:max_tests]
        for rel in extra:
            _add_entry(entries, seen, TestEntry(test_id=rel, kind="js", relpath=rel, source="all"))

    return entries


def _command_for_entry(repo_dir: Path, entry: TestEntry) -> List[str]:
    if entry.kind == "unit_full":
        return [str(repo_dir / "build" / "njs_unit_test")]
    if entry.kind == "js":
        if entry.relpath.endswith(".t.js"):
            return ["./test/test262", "--binary=build/njs", entry.relpath]
        return [str(repo_dir / "build" / "njs"), entry.relpath]
    if entry.kind == "poc":
        return [str(repo_dir / "build" / "njs"), entry.poc_name]
    raise ValueError(f"Unknown test kind: {entry.kind}")


def _ensure_poc(repo_dir: Path, entry: TestEntry) -> None:
    if entry.kind != "poc":
        return
    src = SCRIPT_DIR / entry.poc_name
    dst = repo_dir / entry.poc_name
    if src.exists() and not dst.exists():
        shutil.copyfile(src, dst)


def _classify_outcome(rc: int, output: str) -> Tuple[str, str]:
    if rc == 124:
        return "FAIL", "timeout"
    for marker in CRASH_MARKERS:
        if marker in output:
            return "FAIL", marker
    if re.search(r"TOTAL:\s+FAILED\b", output):
        return "FAIL", "test262_failed"
    if rc != 0:
        return "FAIL", f"exit_code={rc}"
    if re.search(r"TOTAL:\s+PASSED\b", output):
        return "PASS", ""
    return "PASS", ""


def run_one_test(
    repo_dir: Path,
    entry: TestEntry,
    *,
    timeout: int,
    collect_cov: bool = False,
    gcov_flush_so: Optional[Path] = None,
) -> TestResult:
    if collect_cov:
        clear_gcda(repo_dir)
    _ensure_poc(repo_dir, entry)

    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")
    if gcov_flush_so is not None:
        preload = str(gcov_flush_so)
        old = env.get("LD_PRELOAD")
        env["LD_PRELOAD"] = f"{preload}:{old}" if old else preload

    cmd = _command_for_entry(repo_dir, entry)
    rc, out, err = run(cmd, cwd=repo_dir, env=env, timeout=timeout)
    output = (out or "") + (err or "")
    outcome, reason = _classify_outcome(rc, output)
    covered = coverage_to_qualified(collect_coverage(repo_dir)) if collect_cov else []

    return TestResult(
        test_id=entry.test_id,
        outcome=outcome,
        fail_reason=reason,
        actual_output=output[-12000:],
        expected_output="",
        covered_functions=covered,
    )


def run_tests(
    repo_dir: Path,
    entries: List[TestEntry],
    *,
    collect_cov: bool,
    test_timeout: int,
    phase_label: str,
    gcov_flush_so: Optional[Path] = None,
) -> Tuple[List[TestResult], int]:
    results: List[TestResult] = []
    with_cov = 0
    for idx, entry in enumerate(entries, 1):
        log(f"  [{phase_label}] {idx}/{len(entries)} running {entry.test_id}")
        result = run_one_test(
            repo_dir,
            entry,
            timeout=test_timeout,
            collect_cov=collect_cov,
            gcov_flush_so=gcov_flush_so if collect_cov else None,
        )
        if result.covered_functions:
            with_cov += 1
        results.append(result)
        n_fail = sum(1 for r in results if r.outcome == "FAIL")
        cov_info = f", with_coverage={with_cov}" if collect_cov else " (no-cov run)"
        log(f"  [{phase_label}] {idx}/{len(entries)} done (fail={n_fail}{cov_info})")
    return results, with_cov


def clear_gcda(repo_dir: Path) -> None:
    for path in repo_dir.rglob("*.gcda"):
        try:
            path.unlink()
        except OSError:
            pass


def ensure_gcov_signal_flush_so(out_root: Path) -> Optional[Path]:
    if not which("gcc"):
        log("  [gcov-flush] gcc not found; disabled.")
        return None
    build_dir = out_root / "_gcov_signal_flush"
    src_path = build_dir / "gcov_flush_on_signal.c"
    so_path = build_dir / "libgcov_flush_on_signal.so"
    build_dir.mkdir(parents=True, exist_ok=True)
    if not src_path.exists() or src_path.read_text(encoding="utf-8", errors="replace") != GCOV_SIGNAL_FLUSH_C:
        src_path.write_text(GCOV_SIGNAL_FLUSH_C, encoding="utf-8")
    if so_path.exists() and so_path.stat().st_mtime >= src_path.stat().st_mtime:
        return so_path
    rc, out, err = run(["gcc", "-shared", "-fPIC", "-O2", str(src_path), "-o", str(so_path)])
    if rc != 0:
        log(f"  [gcov-flush] build failed rc={rc}\n{(out + err)[-2000:]}")
        return None
    return so_path


def ensure_gcov_signal_flush_header(out_root: Path) -> Optional[Path]:
    build_dir = out_root / "_gcov_signal_flush"
    header_path = build_dir / "gcov_flush_on_signal.h"
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
        if not header_path.exists() or header_path.read_text(encoding="utf-8", errors="replace") != GCOV_SIGNAL_FLUSH_H:
            header_path.write_text(GCOV_SIGNAL_FLUSH_H, encoding="utf-8")
    except OSError as exc:
        log(f"  [gcov-flush] header disabled: {exc}")
        return None
    return header_path


def _source_from_gcov_output(repo_dir: Path, output: str) -> Optional[str]:
    matches = re.findall(r"^File '([^']+)'", output, flags=re.MULTILINE)
    for item in matches:
        if item.endswith((".c", ".h")):
            path = Path(item)
            if path.is_absolute():
                try:
                    return path.relative_to(repo_dir).as_posix()
                except ValueError:
                    return path.as_posix()
            return path.as_posix()
    return None


def collect_coverage(repo_dir: Path) -> Dict[str, List[str]]:
    if not which("gcov"):
        return {}

    coverage: Dict[str, set] = {}
    for gcov_file in repo_dir.rglob("*.gcov"):
        try:
            gcov_file.unlink()
        except OSError:
            pass

    for gcda in sorted(repo_dir.rglob("*.gcda")):
        rc, out, err = run(["gcov", "-f", gcda.name], cwd=gcda.parent)
        if rc != 0:
            continue
        output = (out or "") + (err or "")
        src = _source_from_gcov_output(repo_dir, output)
        if not src:
            continue
        funcs = coverage.setdefault(src, set())
        for match in _GCOV_FUNC_CALLED_RE.finditer(output):
            try:
                calls = int(match.group("calls"))
            except ValueError:
                calls = 0
            if calls > 0:
                funcs.add(match.group("name").strip())
        for match in _GCOV_FUNC_LINES_RE.finditer(output):
            try:
                pct = float(match.group("pct"))
            except ValueError:
                pct = 0.0
            if pct > 0:
                funcs.add(match.group("name").strip())

    for gcov_file in repo_dir.rglob("*.gcov"):
        try:
            gcov_file.unlink()
        except OSError:
            pass
    return {src: sorted(funcs) for src, funcs in coverage.items() if funcs}


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    qualified = []
    for src, funcs in cov_map.items():
        src_name = Path(src).name
        for func in funcs:
            qualified.append(f"{src_name}:{func}")
    return sorted(set(qualified))


def write_run_one_test(repo_dir: Path, entries: List[TestEntry]) -> None:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'ROOT="$(cd "$(dirname "$0")" && pwd)"',
        'BUILD_DIR="$ROOT/build"',
        f'TEMPLATE_PROJECT_DIR="{SCRIPT_DIR}"',
        'TEST_ID="${1:-}"',
        'if [[ -z "$TEST_ID" ]]; then echo "usage: $0 <test_id>" >&2; exit 2; fi',
        "",
        'case "$TEST_ID" in',
    ]
    for entry in entries:
        lines.append(
            f"{shlex.quote(entry.test_id)}) "
            f"TEST_KIND={shlex.quote(entry.kind)}; "
            f"TEST_RELPATH={shlex.quote(entry.relpath)}; "
            f"TEST_POC={shlex.quote(entry.poc_name)} ;;"
        )
    lines.extend(
        [
            '*) echo "[run_one_test] unknown test_id: $TEST_ID" >&2; exit 2 ;;',
            "esac",
            "",
            'cd "$ROOT"',
            'case "$TEST_KIND" in',
            'unit_full) exec "$BUILD_DIR/njs_unit_test" ;;',
            'js)',
            '  case "$TEST_RELPATH" in',
            '    *.t.js) exec "$ROOT/test/test262" --binary="$BUILD_DIR/njs" "$TEST_RELPATH" ;;',
            '    *) exec "$BUILD_DIR/njs" "$TEST_RELPATH" ;;',
            '  esac',
            '  ;;',
            'poc)',
            '  if [[ ! -f "$TEST_POC" ]]; then cp "$TEMPLATE_PROJECT_DIR/$TEST_POC" "$ROOT/$TEST_POC"; fi',
            '  exec "$BUILD_DIR/njs" "$TEST_POC"',
            "  ;;",
            '*) echo "[run_one_test] unsupported kind: $TEST_KIND" >&2; exit 2 ;;',
            "esac",
            "",
        ]
    )
    script = repo_dir / "run_one_test.sh"
    script.write_text("\n".join(lines), encoding="utf-8")
    script.chmod(0o755)


def _test_to_dict(r: TestResult) -> dict:
    return {
        "test_id": r.test_id,
        "outcome": r.outcome,
        "outcome_fixed": r.outcome_fixed,
        "fail_reason": r.fail_reason,
        "actual_output": r.actual_output,
        "expected_output": r.expected_output,
        "covered_functions": r.covered_functions,
    }


def _write_meta(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _existing_metadata_is_current(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    phase_info = data.get("phase_info") or {}
    tests = data.get("tests")
    return (
        data.get("project") == PROJECT_NAME
        and int(phase_info.get("coverage_parser_version") or 0) == COVERAGE_PARSER_VERSION
        and isinstance(tests, list)
        and bool(tests)
    )


def _c_symbol_from_hunk_signature(signature: str) -> str:
    c_keywords = {"if", "for", "while", "switch", "return", "sizeof", "case", "do"}
    match = re.match(r".*?\b([A-Za-z_]\w*)\s*\(", signature)
    if match and match.group(1) not in c_keywords:
        return match.group(1)
    return ""


def _c_symbol_at_line(repo_dir: Path, bug: BugEntry, src_file: str, line_number: int) -> str:
    if line_number <= 0:
        return ""
    rc, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo_dir)
    if rc != 0 or not text:
        path = repo_dir / src_file
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")

    lines = text.splitlines()
    prefix = "\n".join(lines[: max(0, min(line_number, len(lines)))])
    normal_matches = list(
        re.finditer(
            r"(?m)^[A-Za-z_][\w\s\*]*?[\s\*]+([A-Za-z_]\w*)\s*\([^;{}]*\)"
            r"\s*(?:/\*.*?\*/\s*)?\{",
            prefix,
            re.DOTALL,
        )
    )
    if not normal_matches:
        return ""
    return normal_matches[-1].group(1)


def _ground_truth_from_locations(bug: BugEntry, repo_dir: Path) -> List[str]:
    files = bug.raw.get("files") or {}
    funcs: List[str] = []
    for idx, src_file in enumerate(bug.src_files):
        location = files.get(f"src{idx}_location") or {}
        if not isinstance(location, dict):
            continue
        line_number = location.get("func_start") or location.get("hunk_start") or location.get("line_number")
        try:
            line_number = int(line_number)
        except (TypeError, ValueError):
            continue
        symbol = _c_symbol_at_line(repo_dir, bug, src_file, line_number)
        if symbol:
            funcs.append(symbol)
    return sorted(set(funcs))


def _extract_ground_truth_funcs(bug: BugEntry, repo_dir: Path) -> List[str]:
    if not bug.sha_before or not bug.src_files:
        return []
    rc, diff, _ = run(["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files], cwd=repo_dir)
    if rc != 0 or not diff:
        return _ground_truth_from_locations(bug, repo_dir)
    funcs = set()
    for line in diff.splitlines():
        if line.startswith("@@"):
            tail = line.split("@@", 2)[-1].strip()
            name = _c_symbol_from_hunk_signature(tail)
            if name:
                funcs.add(name)
    if funcs:
        return sorted(funcs)
    return _ground_truth_from_locations(bug, repo_dir)


def _compile_cmd_for_meta(bug: BugEntry, *, jobs: int, asan: bool, coverage: bool = False) -> str:
    flags = list(bug.build_flags)
    if asan and "--address-sanitizer=YES" not in flags:
        flags.insert(0, "--address-sanitizer=YES")
    cflags = COV_CFLAGS if coverage else (ASAN_CFLAGS if asan else "-g -O0 -Wno-error")
    return (
        f"./configure {' '.join(flags)} --build-dir=build && "
        f"make -j {jobs} CFLAGS={shlex.quote(cflags)} && "
        f"make -j {jobs} build/njs_unit_test CFLAGS={shlex.quote(cflags)}"
    )


def _empty_record(bug: BugEntry, repo_dir: Path, compile_cmd: str, *, error: str) -> dict:
    source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
    return {
        "bug_id": bug.bug_id,
        "dataset_name": "defects4c",
        "language": "C",
        "project": PROJECT_NAME,
        "commit_after": bug.sha_after,
        "commit_before": bug.sha_before,
        "source_file": source_file,
        "source_basename": os.path.basename(source_file) if source_file else "",
        "compile_cmd": compile_cmd,
        "test_cmd_template": "",
        "cve": bug.cve_name,
        "ground_truth_functions": [],
        "ground_truth": [],
        "phase_info": {"error": error, "coverage_parser_version": COVERAGE_PARSER_VERSION},
        "tests": [],
        "build_error": error,
    }


def process_bug(
    bug: BugEntry,
    *,
    out_root: Path,
    metadata_dir: Path,
    raw_dir: Path,
    jobs: int,
    clone: bool,
    skip_coverage: bool,
    asan: bool,
    dual_run: bool,
    test_scope: str,
    max_tests: int,
    test_timeout: int,
    gcov_scope: str,
) -> Path:
    repo_dir = ensure_repo(bug, out_root, clone=clone)
    out_path = metadata_dir / f"{bug.safe_bug_id}_meta.json"
    raw_out_path = raw_dir / f"{bug.safe_bug_id}_meta.json"
    compile_cmd = _compile_cmd_for_meta(bug, jobs=jobs, asan=True)
    phase_info: Dict[str, object] = {
        "mode": "dual" if dual_run else "single",
        "phase_b_scope": "all" if not skip_coverage else "skip_coverage",
        "test_policy": "fixed_tree_tests_for_buggy_and_fixed" if dual_run else "fixed_tree_tests",
        "test_scope": test_scope,
        "max_tests": max_tests,
        "gcov_scope": gcov_scope,
        "coverage_parser_version": COVERAGE_PARSER_VERSION,
    }

    try:
        checkout_fixed(repo_dir, bug)
        entries = discover_tests(repo_dir, bug, test_scope=test_scope, max_tests=max_tests)
        write_run_one_test(repo_dir, entries)
        phase_info["test_count"] = len(entries)

        if dual_run:
            log("  [phaseA-buggy] checkout+build ASAN")
            checkout_buggy(repo_dir, bug)
            if not compile_njs(repo_dir, bug, jobs=jobs, asan=True, coverage=False):
                record = _empty_record(bug, repo_dir, compile_cmd, error="phaseA_buggy_compile_failed")
                _write_meta(raw_out_path, record)
                _write_meta(out_path, record)
                return out_path
            results_a, _ = run_tests(repo_dir, entries, collect_cov=False, test_timeout=test_timeout, phase_label="phaseA-buggy")

            fixed_outcomes: Dict[str, str] = {}
            log("  [phaseA-fixed] checkout+build ASAN (outcome_fixed)")
            checkout_fixed(repo_dir, bug)
            if compile_njs(repo_dir, bug, jobs=jobs, asan=True, coverage=False):
                results_fixed, _ = run_tests(repo_dir, entries, collect_cov=False, test_timeout=test_timeout, phase_label="phaseA-fixed")
                fixed_outcomes = {r.test_id: r.outcome for r in results_fixed}
                phase_info["phase_a_fixed_status"] = "ok"
                phase_info["phase_a_fixed_test_count"] = len(results_fixed)
                phase_info["phase_a_fixed_fail_count"] = sum(1 for r in results_fixed if r.outcome == "FAIL")
            else:
                phase_info["phase_a_fixed_status"] = "compile_failed"
                phase_info["phase_a_fixed_fail_count"] = 0

            cov_by_test: Dict[str, List[str]] = {}
            if not skip_coverage:
                log("  [phaseB] checkout buggy + build GCOV")
                checkout_buggy(repo_dir, bug)
                gcov_flush_header = ensure_gcov_signal_flush_header(out_root)
                if compile_njs(
                    repo_dir,
                    bug,
                    jobs=jobs,
                    asan=False,
                    coverage=True,
                    gcov_flush_header=gcov_flush_header,
                ):
                    phase_info["phase_b_gcov_signal_flush"] = bool(gcov_flush_header)
                    results_b, n_cov = run_tests(
                        repo_dir,
                        entries,
                        collect_cov=True,
                        test_timeout=test_timeout,
                        phase_label="phaseB",
                        gcov_flush_so=None,
                    )
                    cov_by_test = {r.test_id: r.covered_functions for r in results_b}
                    phase_info["phase_b_with_coverage"] = n_cov
                    phase_info["phase_b_test_count"] = len(results_b)
                else:
                    phase_info["phase_b_status"] = "compile_failed"
            else:
                phase_info["phase_b_gcov_signal_flush"] = False

            results = [
                TestResult(
                    test_id=r.test_id,
                    outcome=r.outcome,
                    outcome_fixed=fixed_outcomes.get(r.test_id, ""),
                    fail_reason=r.fail_reason,
                    actual_output=r.actual_output,
                    expected_output=r.expected_output,
                    covered_functions=cov_by_test.get(r.test_id, []),
                )
                for r in results_a
            ]
            phase_info["phase_a_fail_count"] = sum(1 for r in results if r.outcome == "FAIL")

        else:
            checkout_buggy(repo_dir, bug)
            gcov_flush_header = ensure_gcov_signal_flush_header(out_root) if not skip_coverage else None
            if not compile_njs(
                repo_dir,
                bug,
                jobs=jobs,
                asan=asan,
                coverage=not skip_coverage,
                gcov_flush_header=gcov_flush_header,
            ):
                record = _empty_record(bug, repo_dir, compile_cmd, error="compile_failed")
                _write_meta(raw_out_path, record)
                _write_meta(out_path, record)
                return out_path
            results, n_cov = run_tests(
                repo_dir,
                entries,
                collect_cov=not skip_coverage,
                test_timeout=test_timeout,
                phase_label="single",
                gcov_flush_so=None,
            )
            phase_info["phase_b_gcov_signal_flush"] = bool(gcov_flush_header)
            phase_info["with_coverage"] = n_cov

        write_run_one_test(repo_dir, entries)
        source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
        ground_truth_functions = _extract_ground_truth_funcs(bug, repo_dir)
        test_cmd_template = f"bash {shlex.quote(str(repo_dir / 'run_one_test.sh'))} {{test_id}}"
        record = {
            "bug_id": bug.bug_id,
            "dataset_name": "defects4c",
            "language": "C",
            "project": PROJECT_NAME,
            "commit_after": bug.sha_after,
            "commit_before": bug.sha_before,
            "source_file": source_file,
            "source_basename": os.path.basename(source_file) if source_file else "",
            "compile_cmd": compile_cmd,
            "test_cmd_template": test_cmd_template,
            "cve": bug.cve_name,
            "ground_truth_functions": ground_truth_functions,
            "ground_truth": [f"{source_file}::{fn}" for fn in ground_truth_functions] if source_file else [],
            "phase_info": phase_info,
            "tests": [_test_to_dict(r) for r in results],
        }
        _write_meta(raw_out_path, record)
        log(f"  [ok] wrote raw {raw_out_path}")
        _write_meta(out_path, record)
        log(f"  [ok] wrote {out_path}")
        return out_path

    finally:
        try:
            final_entries = discover_tests(repo_dir, bug, test_scope=test_scope, max_tests=max_tests)
            write_run_one_test(repo_dir, final_entries)
        except Exception as exc:
            log(f"  [warn] final write_run_one_test failed: {exc}")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="build_meta_nginx_njs.py",
        description="Sinh metadata Unified-Debugging cho nginx/njs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sha", action="append", default=[], help="Chi xu ly commit_after khop prefix.")
    ap.add_argument("--only", dest="sha", action="append", help="Alias cua --sha.")
    ap.add_argument("--limit", type=int, default=0, help="Gioi han so bug xu ly (0 = tat ca).")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT, help=f"Thu muc chua git_repo_dir_<bug_id> (default: {DEFAULT_OUT_ROOT}).")
    ap.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR, help=f"Noi ghi metadata (default: {DEFAULT_METADATA_DIR}).")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Noi ghi raw output (default: {DEFAULT_RAW_DIR}).")
    ap.add_argument("--jobs", type=int, default=max(os.cpu_count() or 2, 2) - 1, help="So job khi build.")
    ap.add_argument("--test-timeout", type=int, default=DEFAULT_TEST_TIMEOUT, help="Timeout moi test command.")
    ap.add_argument("--max-tests", type=int, default=0, help="Gioi han extra test/**/*.t.js trong --test-scope all (0 = tat ca).")
    ap.add_argument("--test-scope", choices=["metadata", "all"], default="metadata", help="metadata = files.test + override PoC; all = metadata + test/**/*.t.js.")
    ap.add_argument("--skip-coverage", action="store_true", help="Khong thu thap gcov.")
    ap.add_argument("--asan", action="store_true", help="Build + test voi AddressSanitizer trong single mode.")
    ap.add_argument("--dual-run", action="store_true", help="Phase A buggy/fixed outcomes + Phase B buggy coverage.")
    ap.add_argument(
        "--gcov-scope",
        choices=["all", "fail", "regression", "fail+regression"],
        default="all",
        help="Tuong thich CLI tcpdump/cJSON/php; njs hien thu coverage cho test da chon.",
    )
    ap.add_argument("--skip-if-exists", action="store_true", help="Bo qua bug da co output metadata hop le.")
    ap.add_argument("--clone", action="store_true", help="Tu clone nginx/njs tu GitHub neu chua co repo.")
    ap.add_argument("--prepare-repos", action="store_true", help="Chi chuan bi repo theo bug_id roi thoat.")
    ap.add_argument("--list", action="store_true", help="Chi liet ke bug roi thoat.")
    return ap


def _filter_bugs(bugs: List[BugEntry], prefixes: List[str]) -> List[BugEntry]:
    wanted = tuple(x for x in prefixes if x)
    if not wanted:
        return bugs
    return [b for b in bugs if b.sha_after in wanted or b.sha_after.startswith(wanted)]


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    bugs = _filter_bugs(load_bugs(), args.sha or [])
    if args.limit:
        bugs = bugs[: args.limit]

    if args.list:
        for bug in bugs:
            print(
                f"{bug.sha_after}\t{bug.bug_id}\t{repo_dir_for_bug(args.out_root, bug).name}\t"
                f"{','.join(bug.src_files)}\t{','.join(bug.test_files)}"
            )
        return 0

    if args.prepare_repos:
        ok = 0
        for idx, bug in enumerate(bugs, 1):
            try:
                repo_dir = ensure_repo(bug, args.out_root, clone=args.clone)
                checkout_fixed(repo_dir, bug)
                log(f"[{idx}/{len(bugs)}] prepared {bug.bug_id}: {repo_dir}")
                ok += 1
            except Exception as exc:
                log(f"[{idx}/{len(bugs)}] [error] prepare repo {bug.bug_id}: {exc}")
        log(f"Prepare repos done: {ok}/{len(bugs)}")
        return 0 if ok == len(bugs) else 1

    log(f"Se xu ly {len(bugs)} bug.")
    log(f"  metadata_dir = {args.metadata_dir}")
    log(f"  raw_dir      = {args.raw_dir}")
    log(f"  out_root     = {args.out_root}")
    log(f"  dual_run     = {args.dual_run}")
    log(f"  test_scope   = {args.test_scope}")
    log(f"  max_tests    = {args.max_tests}")
    log(f"  test_timeout = {args.test_timeout}")
    log(f"  skip_coverage= {args.skip_coverage}")
    log(f"  gcov_scope   = {args.gcov_scope}")

    try:
        lock_fp = _acquire_single_run_lock(_lock_path_for_metadata_dir(args.metadata_dir))
    except RuntimeError as exc:
        log(f"[error] {exc}")
        return 2

    ok = 0
    try:
        for idx, bug in enumerate(bugs, 1):
            out_path = args.metadata_dir / f"{bug.safe_bug_id}_meta.json"
            if args.skip_if_exists and out_path.exists() and _existing_metadata_is_current(out_path):
                log(f"[{idx}/{len(bugs)}] skip existing {bug.bug_id}")
                ok += 1
                continue
            log(f"[{idx}/{len(bugs)}] {bug.bug_id} after={bug.sha_after[:12]}")
            try:
                process_bug(
                    bug,
                    out_root=args.out_root,
                    metadata_dir=args.metadata_dir,
                    raw_dir=args.raw_dir,
                    jobs=args.jobs,
                    clone=args.clone,
                    skip_coverage=args.skip_coverage,
                    asan=args.asan,
                    dual_run=args.dual_run,
                    test_scope=args.test_scope,
                    max_tests=args.max_tests,
                    test_timeout=args.test_timeout,
                    gcov_scope=args.gcov_scope,
                )
                ok += 1
            except Exception as exc:
                log(f"[{idx}/{len(bugs)}] [error] {bug.bug_id}: {exc}")
        log(f"Done: {ok}/{len(bugs)}")
        return 0 if ok == len(bugs) else 1
    finally:
        lock_fp.close()


if __name__ == "__main__":
    sys.exit(main())
