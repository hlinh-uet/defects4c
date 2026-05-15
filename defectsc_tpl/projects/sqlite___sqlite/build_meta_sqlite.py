#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project sqlite___sqlite.

SQLite uses its own Tcl-based test runner (`testfixture`), so this script keeps
the cJSON metadata schema but replaces CTest discovery with deterministic Tcl
test selection:
  - always run the bug's `files.test` entries
  - add a bounded deterministic random sample of extra tests
  - collect gcov coverage from the buggy tree only
"""

from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - script normally runs inside Linux Docker.
    fcntl = None


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"

REMOTE_URL = "https://github.com/sqlite/sqlite.git"
BUILD_DIR_NAME = "build_meta_sqlite"
DEFAULT_TEST_TIMEOUT = 120
DEFAULT_RANDOM_SEED = 20260428
DEFAULT_MAX_TESTS = 50
COVERAGE_PARSER_VERSION = 2

BUILD_FLAGS = ["--enable-shared=yes", "--enable-static=no"]
COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage -Wl,--export-dynamic -Wl,-u,__gcov_dump"

GCOV_SIGNAL_FLUSH_C = r"""
#define _GNU_SOURCE
#include <signal.h>
#include <string.h>

extern void __gcov_dump(void) __attribute__((weak));

#define GCOV_ALT_STACK_SIZE (1024 * 1024)

static struct sigaction old_segv;
static struct sigaction old_abrt;
static struct sigaction old_bus;
static struct sigaction old_ill;
static struct sigaction old_fpe;
static struct sigaction old_term;
static volatile sig_atomic_t dumping;
static unsigned char altstack_mem[GCOV_ALT_STACK_SIZE] __attribute__((aligned(16)));

static struct sigaction *old_action_for(int sig) {
    switch (sig) {
        case SIGSEGV: return &old_segv;
        case SIGABRT: return &old_abrt;
        case SIGBUS:  return &old_bus;
        case SIGILL:  return &old_ill;
        case SIGFPE:  return &old_fpe;
        case SIGTERM: return &old_term;
        default:      return &old_segv;
    }
}

static void flush_and_chain(int sig, siginfo_t *info, void *uctx) {
    struct sigaction *old = old_action_for(sig);

    if (!dumping) {
        dumping = 1;
        if (__gcov_dump) {
            __gcov_dump();
        }
    }

    if (old->sa_flags & SA_SIGINFO) {
        if (old->sa_sigaction) {
            old->sa_sigaction(sig, info, uctx);
            return;
        }
    } else if (old->sa_handler == SIG_IGN) {
        return;
    } else if (old->sa_handler && old->sa_handler != SIG_DFL) {
        old->sa_handler(sig);
        return;
    }

    signal(sig, SIG_DFL);
    raise(sig);
}

static void install_one(int sig, struct sigaction *old) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sigemptyset(&sa.sa_mask);
    sa.sa_sigaction = flush_and_chain;
    sa.sa_flags = SA_SIGINFO | SA_RESETHAND | SA_ONSTACK;
    sigaction(sig, &sa, old);
}

static void install_altstack(void) {
    stack_t ss;
    memset(&ss, 0, sizeof(ss));
    ss.ss_sp = altstack_mem;
    ss.ss_size = sizeof(altstack_mem);
    ss.ss_flags = 0;
    sigaltstack(&ss, NULL);
}

__attribute__((constructor))
static void install_handlers(void) {
    install_altstack();
    install_one(SIGSEGV, &old_segv);
    install_one(SIGABRT, &old_abrt);
    install_one(SIGBUS, &old_bus);
    install_one(SIGILL, &old_ill);
    install_one(SIGFPE, &old_fpe);
    install_one(SIGTERM, &old_term);
}
"""

_GCOV_SOURCE_RE = re.compile(r"Source:(?P<source>.+)$")
_GCOV_FUNC_RE = re.compile(
    r"^function\s+(?P<name>.+?)\s+called\s+(?P<calls>\d+)\s+returned",
    re.MULTILINE,
)
_GCOV_FUNC_LINES_RE = re.compile(
    r"Function '(?P<name>[^']+)'\nLines executed:(?P<pct>[0-9.]+)%",
    re.MULTILINE,
)

EXCLUDED_TEST_PATTERNS = [
    "all.test",
    "full.test",
    "quick.test",
    "veryquick.test",
    "extraquick.test",
    "soak.test",
    "permutations.test",
    "malloc*.test",
    "*malloc*.test",
    "*ioerr*.test",
    "*fault*.test",
    "*_err.test",
    "crash*.test",
    "speed*.test",
    "thread*.test",
    "*thread*.test",
    "fuzz*.test",
    "*fuzz*.test",
    "bigfile*.test",
    "walslow.test",
]


@dataclass
class BugEntry:
    sha_after: str
    sha_before: str
    src_files: List[str]
    test_files: List[str]
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
    source_relpath: str = ""
    origin: str = "random"


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
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / PROJECT_NAME
    container_default = Path("/out") / PROJECT_NAME
    if _safe_exists(Path("/out")):
        return container_default
    if _safe_exists(host_default):
        return host_default
    return host_default


def _detect_default_metadata_dir() -> Path:
    host_default = (
        DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "sqlite" / "metadata"
    )
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/sqlite/metadata")
    return host_default


def _detect_default_raw_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "sqlite" / "raw"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/sqlite/raw")
    return host_default


DEFAULT_OUT_ROOT = _detect_default_out_root()
DEFAULT_METADATA_DIR = _detect_default_metadata_dir()
DEFAULT_RAW_DIR = _detect_default_raw_dir()


def run(cmd, *, cwd=None, env=None, check=False, timeout=None, capture=True):
    args = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            encoding="utf-8" if capture else None,
            errors="replace" if capture else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exp:
        return 124, "", f"TimeoutExpired: {exp}"
    except UnicodeDecodeError as exp:
        return 1, "", f"UnicodeDecodeError: {exp}"

    rc = proc.returncode
    out = (proc.stdout or "") if capture else ""
    err = (proc.stderr or "") if capture else ""
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
    if fcntl is None:
        fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fp.flush()
        return fp
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.seek(0)
        owner = fp.read().strip()
        owner_msg = f" owner={owner}" if owner else ""
        fp.close()
        raise RuntimeError(f"Another process is running (lock: {lock_path}){owner_msg}.")
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp


def _release_single_run_lock(lock_fp) -> None:
    if fcntl is not None:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
    lock_fp.close()


def _lock_path_for_metadata_dir(metadata_dir: Path) -> Path:
    key = hashlib.md5(str(metadata_dir).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_sqlite.{key}.lock"


def load_bugs() -> List[BugEntry]:
    if not BUGS_JSON.exists():
        raise FileNotFoundError(f"Missing {BUGS_JSON}")
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for item in data:
        files = item.get("files") or {}
        type_info = item.get("type") or {}
        bugs.append(BugEntry(
            sha_after=item.get("commit_after") or "",
            sha_before=item.get("commit_before") or "",
            src_files=list(files.get("src") or []),
            test_files=list(files.get("test") or []),
            cve_name=type_info.get("name") or type_info.get("id"),
            type_id=type_info.get("id") or item.get("commit_after") or "",
            raw=item,
        ))

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
            raise FileExistsError(f"Target repo path exists but is not a git repo: {repo_dir}")
        log(f"  [repo] rename legacy repo {legacy_repo_dir.name} -> {repo_dir.name}")
        legacy_repo_dir.rename(repo_dir)
        return repo_dir
    if not clone:
        raise FileNotFoundError(
            f"Missing repo {repo_dir}. Run --prepare-repos --clone or add --clone."
        )
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if not repo_dir.exists():
        run(["git", "clone", "--no-checkout", REMOTE_URL, str(repo_dir)], check=True)
    run(["git", "fetch", "origin", bug.sha_before, bug.sha_after], cwd=repo_dir, check=True)
    return repo_dir


def checkout_commit(repo_dir: Path, sha: str) -> None:
    run(["git", "reset", "--hard"], cwd=repo_dir, check=True)
    run(["git", "clean", "-ffdx"], cwd=repo_dir, check=True)
    rc, _, _ = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir)
    if rc != 0:
        run(["git", "fetch", "--all", "--tags"], cwd=repo_dir, check=True)
    run(["git", "checkout", "--force", sha], cwd=repo_dir, check=True)


def checkout_buggy(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)
    if bug.sha_before and bug.src_files:
        run(
            ["git", "checkout", "--force", bug.sha_before, "--", *bug.src_files],
            cwd=repo_dir,
            check=True,
        )


def checkout_fixed(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)


def _build_env(*, coverage: bool) -> dict:
    env = os.environ.copy()
    cflags = ["-Wno-error"]
    ldflags: List[str] = []
    if coverage:
        cflags.insert(0, COV_CFLAGS)
        ldflags.append(COV_LDFLAGS)
    env["CFLAGS"] = " ".join(cflags)
    env["CPPFLAGS"] = "-Wno-error"
    if ldflags:
        env["LDFLAGS"] = " ".join(ldflags)
    env.setdefault("CC", "gcc")
    env.setdefault("CXX", "g++")
    return env


def _configure_cmd(repo_dir: Path) -> List[str]:
    return [
        "./configure",
        *BUILD_FLAGS,
        f"--prefix={repo_dir / BUILD_DIR_NAME}",
    ]


def compile_sqlite(repo_dir: Path, *, jobs: int, coverage: bool, timeout: int = 1800) -> bool:
    if not which("make"):
        log("  [build] make is not in PATH")
        return False
    env = _build_env(coverage=coverage)
    rc, out, err = run(
        _configure_cmd(repo_dir),
        cwd=repo_dir,
        env=env,
        capture=True,
        timeout=600,
    )
    if rc != 0:
        log(f"  [build] configure failed rc={rc}\n{(out + err)[-3000:]}")
        return False

    rc, out, err = run(
        ["make", "-j", str(jobs)],
        cwd=repo_dir,
        env=env,
        capture=True,
        timeout=timeout,
    )
    if rc != 0:
        log(f"  [build] make failed rc={rc}\n{(out + err)[-3000:]}")
        return False

    rc, out, err = run(
        ["make", "-j", str(jobs), "testfixture"],
        cwd=repo_dir,
        env=env,
        capture=True,
        timeout=timeout,
    )
    if rc != 0:
        log(f"  [build] make testfixture failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    if not (repo_dir / "testfixture").exists():
        log("  [build] missing ./testfixture after build")
        return False
    return True


def ensure_gcov_signal_flush_so(out_root: Path) -> Optional[Path]:
    gcc = which("gcc")
    if not gcc:
        log("  [gcov-flush] gcc not found; crash-time gcov flush disabled.")
        return None

    build_dir = out_root / "_gcov_signal_flush"
    src_path = build_dir / "gcov_flush_on_signal.c"
    so_path = build_dir / "libgcov_flush_on_signal.so"
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
        current = src_path.read_text(encoding="utf-8") if src_path.exists() else ""
        if current != GCOV_SIGNAL_FLUSH_C:
            src_path.write_text(GCOV_SIGNAL_FLUSH_C, encoding="utf-8")
        if so_path.exists() and so_path.stat().st_mtime >= src_path.stat().st_mtime:
            return so_path
    except OSError as exc:
        log(f"  [gcov-flush] cannot prepare source: {exc}")
        return None

    rc, out, err = run(
        [gcc, "-shared", "-fPIC", "-o", str(so_path), str(src_path)],
        timeout=60,
    )
    if rc != 0:
        log(f"  [gcov-flush] build failed rc={rc}\n{(out + err)[-2000:]}")
        return None
    return so_path


def _compile_cmd_for_meta(repo_dir: Path, *, coverage: bool, jobs: int) -> str:
    env_parts = ["CFLAGS=-Wno-error", "CPPFLAGS=-Wno-error"]
    if coverage:
        env_parts = [
            f"CFLAGS={shlex.quote(COV_CFLAGS + ' -Wno-error')}",
            "CPPFLAGS=-Wno-error",
            f"LDFLAGS={shlex.quote(COV_LDFLAGS)}",
        ]
    cfg = " ".join(shlex.quote(x) for x in _configure_cmd(repo_dir))
    return " ".join(env_parts) + f" {cfg} && make -j{jobs} && make -j{jobs} testfixture"


def _is_excluded_test(path: str) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_TEST_PATTERNS)


def _normalize_test_path(repo_dir: Path, rel: str) -> Optional[str]:
    rel = rel.replace("\\", "/")
    path = repo_dir / rel
    if path.exists():
        return rel
    if not rel.startswith("test/") and (repo_dir / "test" / rel).exists():
        return f"test/{rel}"
    return None


def mandatory_tests(repo_dir: Path, bug: BugEntry) -> List[TestEntry]:
    entries: List[TestEntry] = []
    seen = set()
    for test_file in bug.test_files:
        rel = _normalize_test_path(repo_dir, test_file)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        entries.append(TestEntry(test_id=rel, source_relpath=rel, origin="bug"))
    return entries


def additional_test_candidates(
    repo_dir: Path,
    *,
    exclude: Iterable[str],
    max_tests: int,
    seed: int,
    bug: BugEntry,
) -> List[TestEntry]:
    excluded = set(exclude)
    test_files = []
    for path in (repo_dir / "test").glob("*.test"):
        rel = path.relative_to(repo_dir).as_posix()
        if rel in excluded or _is_excluded_test(rel):
            continue
        test_files.append(rel)
    test_files = sorted(set(test_files))
    stable_seed = seed + int(hashlib.md5(bug.sha_after.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(stable_seed)
    rng.shuffle(test_files)
    if max_tests > 0:
        test_files = test_files[:max_tests]
    return [
        TestEntry(test_id=rel, source_relpath=rel, origin="random")
        for rel in test_files
    ]


def cleanup_sqlite_temp(repo_dir: Path) -> None:
    patterns = [
        "testdir_*",
        "test.db*",
        "temp*.db*",
        "etilqs_*",
        "test-out.txt",
    ]
    for pattern in patterns:
        for path in repo_dir.glob(pattern):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError:
                pass
    for sub in ["test"]:
        d = repo_dir / sub
        if not d.exists():
            continue
        for pattern in patterns:
            for path in d.glob(pattern):
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except OSError:
                    pass


def clear_gcda(repo_dir: Path) -> None:
    for path in repo_dir.rglob("*.gcda"):
        try:
            path.unlink()
        except OSError:
            pass


def clear_gcov(repo_dir: Path) -> None:
    for path in repo_dir.rglob("*.gcov"):
        try:
            path.unlink()
        except OSError:
            pass


def _sqlite_test_passed(rc: int, output: str) -> bool:
    if re.search(r"^0 errors out of", output, flags=re.MULTILINE):
        return rc == 0
    if re.search(r"^[1-9][0-9]* errors out of", output, flags=re.MULTILINE):
        return False
    return rc == 0


def _run_testfixture(
    cmd: List[str],
    *,
    cwd: Path,
    env: dict,
    timeout: int,
    graceful_timeout: bool,
) -> Tuple[int, str, str]:
    if not graceful_timeout:
        return run(cmd, cwd=cwd, env=env, timeout=timeout, capture=True)

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired as exp:
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        timeout_msg = f"TimeoutExpired: {exp}"
        return 124, out or "", ((err or "") + "\n" + timeout_msg).strip()


def run_one_test(
    repo_dir: Path,
    te: TestEntry,
    *,
    timeout: int,
    gcov_flush_so: Optional[Path] = None,
) -> Tuple[bool, str, str]:
    cleanup_sqlite_temp(repo_dir)
    cmd = ["./testfixture", te.test_id]
    env = os.environ.copy()
    if gcov_flush_so is not None:
        existing = env.get("LD_PRELOAD", "")
        env["LD_PRELOAD"] = f"{gcov_flush_so} {existing}".strip()
    rc, out, err = _run_testfixture(
        cmd,
        cwd=repo_dir,
        env=env,
        timeout=timeout,
        graceful_timeout=gcov_flush_so is not None,
    )
    combined = out + err
    passed = _sqlite_test_passed(rc, combined)
    if passed:
        return True, combined, ""
    if rc == 124:
        return False, combined, f"timeout={timeout}"
    return False, combined, f"exit_code={rc}"


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
    n_with_cov = 0
    for idx, te in enumerate(entries, 1):
        clear_gcda(repo_dir)
        passed, combined_output, reason = run_one_test(
            repo_dir,
            te,
            timeout=test_timeout,
            gcov_flush_so=gcov_flush_so if collect_cov else None,
        )
        covered_funcs: List[str] = []
        if collect_cov:
            covered_funcs = coverage_to_qualified(collect_coverage(repo_dir))
            if covered_funcs:
                n_with_cov += 1
        results.append(TestResult(
            test_id=te.test_id,
            outcome="PASS" if passed else "FAIL",
            fail_reason="" if passed else reason,
            actual_output="" if passed else combined_output[-4000:],
            expected_output="",
            covered_functions=covered_funcs,
        ))
        n_fail = sum(1 for r in results if r.outcome == "FAIL")
        cov_info = f", with_coverage={n_with_cov}" if collect_cov else " (no-cov run)"
        log(f"  [{phase_label}] {idx}/{len(entries)} done (fail={n_fail}{cov_info})")
    return results, n_with_cov


def _rel_source_from_gcov(source: str, repo_dir: Path) -> str:
    source = source.strip()
    source = source.replace("\\", "/")
    if source.startswith("/"):
        p = Path(source)
        try:
            return p.relative_to(repo_dir).as_posix()
        except ValueError:
            return p.name
    while source.startswith("../"):
        source = source[3:]
    return source


def _parse_gcov_file(gcov_file: Path, repo_dir: Path) -> Tuple[Optional[str], List[str]]:
    text = gcov_file.read_text(encoding="utf-8", errors="replace")
    source = None
    for line in text.splitlines()[:20]:
        m = _GCOV_SOURCE_RE.search(line)
        if m:
            source = _rel_source_from_gcov(m.group("source"), repo_dir)
            break
    funcs: List[str] = []
    for m in _GCOV_FUNC_RE.finditer(text):
        try:
            if int(m.group("calls")) > 0:
                funcs.append(m.group("name"))
        except ValueError:
            pass
    return source, sorted(set(funcs))


def _source_candidates(repo_dir: Path, gcda: Path) -> List[Path]:
    names = []
    stem = gcda.stem
    if stem.endswith(".c"):
        names.append(stem)
    else:
        names.append(f"{stem}.c")
    names.append(gcda.name.replace(".gcda", ""))

    out: List[Path] = []
    for name in dict.fromkeys(names):
        local = gcda.parent / name
        if local.exists():
            out.append(local)
    return list(dict.fromkeys(out))


def _source_from_gcov_output(repo_dir: Path, gcda: Path, output: str) -> Optional[Path]:
    for match in re.finditer(r"^File '([^']+)'", output, re.MULTILINE):
        raw = match.group(1)
        if raw.startswith("/"):
            candidate = Path(raw)
        else:
            candidate = (gcda.parent / raw).resolve()
        try:
            rel = candidate.relative_to(repo_dir)
        except ValueError:
            continue
        if ".git" in rel.parts:
            continue
        if candidate.suffix in {".c", ".h"}:
            return candidate
    return None


def _discover_function_sources(repo_dir: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    roots = [
        repo_dir / "src",
        repo_dir / "ext" / "misc",
        repo_dir / "ext" / "fts3",
        repo_dir / "ext" / "fts5",
        repo_dir / "ext" / "rtree",
        repo_dir / "ext" / "session",
    ]
    func_def = re.compile(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{")
    keywords = {"if", "for", "while", "switch", "return", "sizeof"}
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.c"):
            try:
                rel = path.relative_to(repo_dir).as_posix()
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            window = ""
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("//") or stripped.startswith("*"):
                    continue
                window = f"{window} {stripped}".strip()
                if len(window) > 500:
                    window = stripped
                if "{" not in window:
                    continue
                m = func_def.search(window)
                if m and m.group(1) not in keywords:
                    mapping.setdefault(m.group(1), rel)
                window = ""
    return mapping


def _add_covered_func(
    covered: Dict[str, List[str]],
    function_sources: Dict[str, str],
    src: str,
    func: str,
) -> None:
    if src.startswith(BUILD_DIR_NAME + "/") or src.startswith(".git/"):
        return
    mapped_src = src
    if os.path.basename(src) == "sqlite3.c":
        mapped_src = function_sources.get(func, src)
    covered.setdefault(mapped_src, []).append(func)


def collect_coverage(repo_dir: Path) -> Dict[str, List[str]]:
    if not which("gcov"):
        return {}
    clear_gcov(repo_dir)
    covered: Dict[str, List[str]] = {}
    function_sources = _discover_function_sources(repo_dir)
    gcda_files = list(repo_dir.rglob("*.gcda"))
    for gcda in gcda_files:
        funcs_called: List[str] = []
        rc, out, _ = run(
            ["gcov", "-f", gcda.name],
            cwd=gcda.parent,
            capture=True,
            timeout=60,
        )
        if rc != 0:
            continue
        for m in _GCOV_FUNC_LINES_RE.finditer(out):
            try:
                if float(m.group("pct")) > 0.0:
                    funcs_called.append(m.group("name"))
            except ValueError:
                pass
        if not funcs_called:
            for m in _GCOV_FUNC_RE.finditer(out):
                try:
                    if int(m.group("calls")) > 0:
                        funcs_called.append(m.group("name"))
                except ValueError:
                    pass
        if funcs_called:
            src_path = _source_from_gcov_output(repo_dir, gcda, out)
            if src_path is None:
                src_candidates = _source_candidates(repo_dir, gcda)
                src_path = next((p for p in src_candidates if p.exists()), None)
            if src_path is not None:
                try:
                    rel_src = src_path.relative_to(repo_dir).as_posix()
                except ValueError:
                    rel_src = src_path.name
                for func in funcs_called:
                    _add_covered_func(covered, function_sources, rel_src, func)

        for gcov_file in gcda.parent.glob("*.gcov"):
            src, funcs = _parse_gcov_file(gcov_file, repo_dir)
            if not src or not funcs:
                continue
            for func in funcs:
                _add_covered_func(covered, function_sources, src, func)

    for src, funcs in list(covered.items()):
        covered[src] = sorted(set(funcs))
    clear_gcov(repo_dir)
    return covered


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for fname, funcs in cov_map.items():
        base = os.path.basename(fname)
        for fn in funcs:
            out.append(f"{base}:{fn}")
    return sorted(set(out))


def write_run_one_test(repo_dir: Path, entries: List[TestEntry]) -> None:
    script = repo_dir / "run_one_test.sh"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        ROOT="$(cd "$(dirname "$0")" && pwd)"
        TEST_ID="${{1:-}}"
        if [[ -z "$TEST_ID" ]]; then
          echo "usage: $0 <test_id>" >&2
          exit 2
        fi

        TEST_FILE="$TEST_ID"
        if [[ ! -f "$ROOT/$TEST_FILE" && "$TEST_FILE" != test/* && -f "$ROOT/test/$TEST_FILE" ]]; then
          TEST_FILE="test/$TEST_FILE"
        fi
        if [[ ! -f "$ROOT/$TEST_FILE" && "$TEST_FILE" != *.test && -f "$ROOT/$TEST_FILE.test" ]]; then
          TEST_FILE="$TEST_FILE.test"
        fi
        if [[ ! -f "$ROOT/$TEST_FILE" && "$TEST_FILE" != test/* && "$TEST_FILE" != *.test && -f "$ROOT/test/$TEST_FILE.test" ]]; then
          TEST_FILE="test/$TEST_FILE.test"
        fi
        if [[ ! -f "$ROOT/$TEST_FILE" ]]; then
          echo "[run_one_test] unknown test_id '$TEST_ID'" >&2
          exit 2
        fi

        cd "$ROOT"
        if [[ ! -x ./testfixture ]]; then
          ./configure {' '.join(BUILD_FLAGS)} --prefix="$ROOT/{BUILD_DIR_NAME}" \\
            CFLAGS="-Wno-error" CPPFLAGS="-Wno-error"
          make -j"$(nproc)" testfixture
        fi
        exec ./testfixture "$TEST_FILE"
    """), encoding="utf-8")
    script.chmod(0o755)
    mapping = "\n".join(f"{te.test_id}\t{te.origin}" for te in entries)
    (repo_dir / ".build_meta_sqlite_tests").write_text(mapping + "\n", encoding="utf-8")


def _extract_ground_truth_funcs_from_diff(bug: BugEntry, repo_dir: Path) -> List[str]:
    if not bug.sha_before or not bug.src_files:
        return []
    rc, diff, _ = run(
        ["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files],
        cwd=repo_dir,
        capture=True,
    )
    if rc != 0 or not diff:
        return []
    funcs = set()
    c_keywords = {"if", "for", "while", "switch", "return", "sizeof", "case", "do"}
    for line in diff.splitlines():
        if not line.startswith("@@"):
            continue
        tail = line.split("@@", 2)[-1].strip()
        m = re.match(r".*?\b([A-Za-z_]\w*)\s*\(", tail)
        if m and m.group(1) not in c_keywords:
            funcs.add(m.group(1))
    return sorted(funcs)


def _function_name_near_line(repo_dir: Path, rel_file: str, line_no: int) -> Optional[str]:
    path = repo_dir / rel_file
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    idx = max(min(line_no - 1, len(lines) - 1), 0)
    signature = ""
    for i in range(idx, max(-1, idx - 100), -1):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("//"):
            continue
        signature = f"{stripped} {signature}".strip()
        m = re.search(r"\b([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?", signature)
        if m and not re.match(r"^(if|for|while|switch)\b", signature):
            return m.group(1)
        if stripped.endswith(";"):
            signature = ""
    return None


def _extract_ground_truth_funcs(bug: BugEntry, repo_dir: Path) -> List[str]:
    funcs = _extract_ground_truth_funcs_from_diff(bug, repo_dir)
    if funcs:
        return funcs
    loc = ((bug.raw.get("files") or {}).get("src0_location") or {})
    line_no = loc.get("func_start") or loc.get("hunk_start") or loc.get("line_number")
    if bug.src_files and isinstance(line_no, int):
        name = _function_name_near_line(repo_dir, bug.src_files[0], line_no)
        if name:
            return [name]
    return []


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
    if data.get("build_error"):
        return False
    phase_info = data.get("phase_info") or {}
    try:
        version = int(phase_info.get("coverage_parser_version") or 0)
    except (TypeError, ValueError):
        version = 0
    tests = data.get("tests")
    if version != COVERAGE_PARSER_VERSION or not isinstance(tests, list) or not tests:
        return False
    if phase_info.get("phase_b_status") == "compile_failed":
        return False
    if phase_info.get("phase_b_scope") != "skip_coverage":
        if "phase_b_with_coverage" not in phase_info:
            return False
        if not any(t.get("covered_functions") for t in tests if isinstance(t, dict)):
            return False
    return True


def _coverage_hits_ground_truth(covered: List[str], ground_truth_functions: List[str]) -> bool:
    if not covered or not ground_truth_functions:
        return False
    covered_funcs = {item.rsplit(":", 1)[-1] for item in covered if ":" in item}
    return any(fn in covered_funcs for fn in ground_truth_functions)


def _annotate_phase_validation(
    phase_info: Dict[str, object],
    results: List[TestResult],
    ground_truth_functions: List[str],
) -> None:
    related = [
        r for r in results
        if r.outcome == "FAIL" and r.outcome_fixed == "PASS"
    ]
    tests_with_coverage = sum(1 for r in results if r.covered_functions)
    related_gt_hits = sum(
        1 for r in related
        if _coverage_hits_ground_truth(r.covered_functions, ground_truth_functions)
    )
    phase_info["related_fail_count"] = len(related)
    phase_info["tests_with_coverage"] = tests_with_coverage
    phase_info["related_gt_coverage_count"] = related_gt_hits
    if not results:
        phase_info["validation_status"] = "no_tests"
    elif tests_with_coverage == 0 and phase_info.get("phase_b_scope") != "skip_coverage":
        phase_info["validation_status"] = "no_coverage"
    elif not related:
        phase_info["validation_status"] = "no_related_fail"
    elif ground_truth_functions and related_gt_hits == 0:
        phase_info["validation_status"] = "no_related_gt_coverage"
    else:
        phase_info["validation_status"] = "ok"


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
        "phase_info": {
            "mode": "error",
            "error": error,
            "coverage_parser_version": COVERAGE_PARSER_VERSION,
        },
        "tests": [],
        "build_error": error,
    }


def _attach_fixed_results(
    buggy_results: List[TestResult],
    fixed_results: List[TestResult],
) -> None:
    fixed_by_id = {r.test_id: r for r in fixed_results}
    for b in buggy_results:
        f = fixed_by_id.get(b.test_id)
        if f:
            b.outcome_fixed = f.outcome


def process_bug(
    bug: BugEntry,
    *,
    out_root: Path,
    metadata_dir: Path,
    raw_dir: Path,
    jobs: int,
    clone: bool,
    dual_run: bool,
    skip_coverage: bool,
    max_tests: int,
    random_seed: int,
    test_timeout: int,
) -> Optional[Path]:
    out_path = metadata_dir / f"{bug.safe_bug_id}_meta.json"
    raw_out_path = raw_dir / f"{bug.safe_bug_id}_meta.json"

    try:
        repo_dir = ensure_repo(bug, out_root, clone=clone)
    except Exception as exc:
        log(f"  [error] cannot locate/clone repo: {exc}")
        return None

    compile_cmd = _compile_cmd_for_meta(repo_dir, coverage=True, jobs=jobs)

    try:
        checkout_buggy(repo_dir, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy failed: {exc}")
        return None

    log("  [phaseA-buggy] build normal")
    if not compile_sqlite(repo_dir, jobs=jobs, coverage=False):
        record = _empty_record(bug, repo_dir, compile_cmd, error="phaseA_buggy_compile_failed")
        _write_meta(raw_out_path, record)
        _write_meta(out_path, record)
        return out_path

    bug_entries = mandatory_tests(repo_dir, bug)
    random_entries = additional_test_candidates(
        repo_dir,
        exclude=[te.test_id for te in bug_entries],
        max_tests=max_tests,
        seed=random_seed,
        bug=bug,
    )
    candidate_entries = bug_entries + random_entries
    log(f"  [tests] candidates={len(candidate_entries)} bug={len(bug_entries)} random={len(random_entries)}")

    buggy_results, _ = run_tests(
        repo_dir,
        candidate_entries,
        collect_cov=False,
        test_timeout=test_timeout,
        phase_label="phaseA-buggy",
    )

    fixed_results: List[TestResult] = []
    phase_info: Dict[str, object] = {
        "mode": "dual" if dual_run else "single",
        "test_policy": "bug_tests_plus_deterministic_random_extra_tests",
        "max_tests": max_tests,
        "random_seed": random_seed,
        "selected_bug_tests": len(bug_entries),
        "selected_extra_tests": len(random_entries),
        "selected_tests": len(candidate_entries),
    }
    if dual_run:
        try:
            checkout_fixed(repo_dir, bug)
            log("  [phaseA-fixed] build normal")
            if compile_sqlite(repo_dir, jobs=jobs, coverage=False):
                fixed_results, _ = run_tests(
                    repo_dir,
                    candidate_entries,
                    collect_cov=False,
                    test_timeout=test_timeout,
                    phase_label="phaseA-fixed",
                )
                phase_info["phase_a_fixed_status"] = "ok"
                phase_info["phase_a_fixed_fail_count"] = sum(
                    1 for r in fixed_results if r.outcome == "FAIL"
                )
            else:
                phase_info["phase_a_fixed_status"] = "compile_failed"
        finally:
            checkout_buggy(repo_dir, bug)

    _attach_fixed_results(buggy_results, fixed_results)
    results = buggy_results
    phase_info["phase_a_fail_count"] = sum(1 for r in results if r.outcome == "FAIL")

    cov_by_test: Dict[str, List[str]] = {}
    if skip_coverage:
        phase_info["phase_b_scope"] = "skip_coverage"
    else:
        phase_info["phase_b_scope"] = "all"
        log("  [phaseB] checkout buggy + build gcov")
        checkout_buggy(repo_dir, bug)
        if compile_sqlite(repo_dir, jobs=jobs, coverage=True):
            gcov_flush_so = ensure_gcov_signal_flush_so(out_root)
            phase_info["phase_b_gcov_signal_flush"] = bool(gcov_flush_so)
            results_b, n_cov = run_tests(
                repo_dir,
                candidate_entries,
                collect_cov=True,
                test_timeout=test_timeout,
                phase_label="phaseB",
                gcov_flush_so=gcov_flush_so,
            )
            cov_by_test = {r.test_id: r.covered_functions for r in results_b}
            phase_info["phase_b_with_coverage"] = n_cov
            phase_info["phase_b_test_count"] = len(candidate_entries)
        else:
            phase_info["phase_b_status"] = "compile_failed"

    for r in results:
        r.covered_functions = cov_by_test.get(r.test_id, [])

    write_run_one_test(repo_dir, candidate_entries)
    source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
    ground_truth_functions = _extract_ground_truth_funcs(bug, repo_dir)
    test_cmd_template = f"bash {shlex.quote(str(repo_dir / 'run_one_test.sh'))} {{test_id}}"
    phase_info["coverage_parser_version"] = COVERAGE_PARSER_VERSION
    _annotate_phase_validation(phase_info, results, ground_truth_functions)
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
        "ground_truth": [
            f"{source_file}::{fn}" for fn in ground_truth_functions
        ] if source_file else [],
        "phase_info": phase_info,
        "tests": [_test_to_dict(r) for r in results],
    }

    _write_meta(raw_out_path, record)
    log(f"  [ok] wrote raw {raw_out_path}")
    _write_meta(out_path, record)
    log(f"  [ok] wrote {out_path}")
    return out_path


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="build_meta_sqlite.py",
        description="Build Unified-Debugging metadata for sqlite/sqlite Defects4C bugs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sha", action="append", default=[], help="Only process commit_after matching this prefix.")
    ap.add_argument("--only", dest="sha", action="append", help="Alias of --sha.")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of bugs processed (0 = all).")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT, help=f"Root containing git_repo_dir_<bug_id> (default: {DEFAULT_OUT_ROOT}).")
    ap.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR, help=f"Metadata output dir (default: {DEFAULT_METADATA_DIR}).")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Raw output dir (default: {DEFAULT_RAW_DIR}).")
    ap.add_argument("--jobs", type=int, default=max((os.cpu_count() or 2) - 1, 1), help="Parallel build jobs.")
    ap.add_argument("--test-timeout", type=int, default=DEFAULT_TEST_TIMEOUT, help="Timeout per Tcl test file.")
    ap.add_argument("--skip-coverage", action="store_true", help="Skip gcov coverage collection.")
    ap.add_argument("--dual-run", action="store_true", help="Run buggy and fixed outcomes, then buggy coverage.")
    ap.add_argument("--max-tests", type=int, default=DEFAULT_MAX_TESTS, help="Number of deterministic random extra tests to add per bug (0 = all selectable tests).")
    ap.add_argument("--random-seed", type=int, default=DEFAULT_RANDOM_SEED, help="Seed for deterministic random extra test selection.")
    ap.add_argument("--skip-if-exists", action="store_true", help="Skip bugs with existing metadata output.")
    ap.add_argument("--clone", action="store_true", help="Clone sqlite/sqlite from GitHub if the repo is missing.")
    ap.add_argument("--prepare-repos", action="store_true", help="Only prepare repos, then exit.")
    ap.add_argument("--list", action="store_true", help="List bugs, then exit.")
    return ap


def _filter_bugs(bugs: List[BugEntry], sha_filters: List[str], limit: int) -> List[BugEntry]:
    if sha_filters:
        wanted = [s for s in sha_filters if s]
        bugs = [
            b for b in bugs
            if b.sha_after in wanted or any(b.sha_after.startswith(w) for w in wanted)
        ]
    if limit:
        bugs = bugs[:limit]
    return bugs


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.max_tests < 0:
        parser.error("--max-tests must be >= 0")
    bugs = _filter_bugs(load_bugs(), args.sha, args.limit)

    if args.list:
        for bug in bugs:
            print(
                f"{bug.sha_after}\t{bug.safe_bug_id}\t"
                f"{repo_dir_for_bug(args.out_root, bug).name}\t{','.join(bug.test_files)}"
            )
        return 0

    if args.prepare_repos:
        ok = 0
        for idx, bug in enumerate(bugs, 1):
            try:
                repo_dir = ensure_repo(bug, args.out_root, clone=args.clone)
                checkout_fixed(repo_dir, bug)
                log(f"[{idx}/{len(bugs)}] prepared {bug.safe_bug_id}: {repo_dir}")
                ok += 1
            except Exception as exc:
                log(f"[{idx}/{len(bugs)}] [error] prepare repo {bug.safe_bug_id}: {exc}")
        log(f"Prepare repos done: {ok}/{len(bugs)}")
        return 0 if ok == len(bugs) else 1

    log(f"Will process {len(bugs)} bug(s).")
    log(f"  metadata_dir = {args.metadata_dir}")
    log(f"  raw_dir      = {args.raw_dir}")
    log(f"  dual_run     = {args.dual_run}")
    log(f"  max_tests    = {args.max_tests}")

    try:
        lock_fp = _acquire_single_run_lock(_lock_path_for_metadata_dir(args.metadata_dir))
    except RuntimeError as exc:
        log(f"[error] {exc}")
        return 2

    ok = 0
    try:
        for idx, bug in enumerate(bugs, 1):
            out_path = args.metadata_dir / f"{bug.safe_bug_id}_meta.json"
            if args.skip_if_exists and out_path.exists():
                if _existing_metadata_is_current(out_path):
                    log(f"[{idx}/{len(bugs)}] skip existing {bug.safe_bug_id}")
                    ok += 1
                    continue
                log(f"[{idx}/{len(bugs)}] rerun existing {bug.safe_bug_id} (old/invalid parser)")
            log(f"[{idx}/{len(bugs)}] {bug.safe_bug_id} after={bug.sha_after[:12]}")
            result = process_bug(
                bug,
                out_root=args.out_root,
                metadata_dir=args.metadata_dir,
                raw_dir=args.raw_dir,
                jobs=args.jobs,
                clone=args.clone,
                dual_run=args.dual_run,
                skip_coverage=args.skip_coverage,
                max_tests=args.max_tests,
                random_seed=args.random_seed,
                test_timeout=args.test_timeout,
            )
            if result:
                ok += 1
    finally:
        _release_single_run_lock(lock_fp)

    log(f"Done: {ok}/{len(bugs)} bug(s) produced output.")
    return 0 if ok == len(bugs) else 1


if __name__ == "__main__":
    sys.exit(main())
