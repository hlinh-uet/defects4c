#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project yhirose___cpp-peglib.

The project is header-only C++ and its Defects4C test template runs the CMake
test binary at build_dir/test/test-main. For each bug this script:
  - uses bugs_list_new.json -> type.id as bug_id
  - checks out commit_after as the fixed tree
  - overlays files.src from commit_before for the buggy tree
  - Phase A: ASAN build for buggy/fixed PASS/FAIL labels
  - Phase B: non-ASAN GCOV build on buggy for covered_functions
  - writes identical raw and metadata records
"""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
SHORT_PROJECT = "peglib"
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"

REMOTE_URL = "https://github.com/yhirose/cpp-peglib.git"
BUILD_DIR_NAME = "build_meta_peglib"
DEFAULT_TEST_TIMEOUT = 120
DEFAULT_LABEL_RETRIES = 5

# Old bundled Catch headers fail to compile with modern glibc because SIGSTKSZ
# is no longer accepted as a constant expression for its POSIX signal handler.
BASE_CXXFLAGS = "-g -O0 -std=c++17 -DCATCH_CONFIG_NO_POSIX_SIGNALS"
BASE_CFLAGS = "-g -O0"
COV_FLAGS = "--coverage"
ASAN_FLAGS = "-fsanitize=address -fno-omit-frame-pointer"
ASAN_LDFLAGS = "-fsanitize=address"

ASAN_ERROR_MARKERS = (
    "==ERROR",
    "AddressSanitizer:",
    "runtime error:",
    "Segmentation fault",
    "SIGSEGV",
)

_GCOV_FILE_RE = re.compile(r"^File '(.+)'$")
_GCOV_FUNC_LINES_RE = re.compile(r"^Function '(.+)'$")
_GCOV_LINES_EXEC_RE = re.compile(r"^Lines executed:([0-9.]+)%")
_GCOV_FUNC_CALLED_RE = re.compile(
    r"^function\s+(?P<name>.+?)\s+called\s+(?P<calls>\d+)\s+returned",
)
_ASAN_FRAME_RE = re.compile(
    r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>.*?)\s+"
    r"(?P<file>/[^:\s]+):\d+",
    re.MULTILINE,
)


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
    executable_relpath: str
    source_relpath: str = ""
    test_name: str = ""
    command: List[str] = field(default_factory=list)
    working_dir_relpath: str = "."


@dataclass
class TestResult:
    test_id: str
    outcome: str
    test_name: str = ""
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
        return Path("/out") / "unified_debugging" / SHORT_PROJECT / "metadata"
    return (
        DEFECTS4C_ROOT
        / "out_tmp_dirs"
        / "unified_debugging"
        / SHORT_PROJECT
        / "metadata"
    )


def _detect_default_raw_dir() -> Path:
    if _safe_exists(Path("/out")):
        return Path("/out") / "unified_debugging" / SHORT_PROJECT / "raw"
    return (
        DEFECTS4C_ROOT
        / "out_tmp_dirs"
        / "unified_debugging"
        / SHORT_PROJECT
        / "raw"
    )


def _detect_default_debug_dir() -> Path:
    if _safe_exists(Path("/out")):
        return Path("/out") / "unified_debugging" / SHORT_PROJECT / "debug"
    return (
        DEFECTS4C_ROOT
        / "out_tmp_dirs"
        / "unified_debugging"
        / SHORT_PROJECT
        / "debug"
    )


DEFAULT_OUT_ROOT = _detect_default_out_root()
DEFAULT_METADATA_DIR = _detect_default_metadata_dir()
DEFAULT_RAW_DIR = _detect_default_raw_dir()
DEFAULT_DEBUG_DIR = _detect_default_debug_dir()


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "item"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def _write_command_debug(
    path: Optional[Path],
    *,
    args: List[str],
    cwd,
    env: Optional[dict],
    env_keys: Optional[List[str]],
    rc: int,
    out: str,
    err: str,
    elapsed: float,
) -> None:
    if path is None:
        return
    selected_env = {}
    for key in env_keys or []:
        if env and key in env:
            selected_env[key] = env[key]
        elif key in os.environ:
            selected_env[key] = os.environ[key]
    header = {
        "command": args,
        "cwd": str(cwd) if cwd else "",
        "returncode": rc,
        "elapsed_sec": round(elapsed, 3),
        "env": selected_env,
    }
    _write_text(
        path,
        json.dumps(header, indent=2, ensure_ascii=False)
        + "\n\n--- stdout ---\n"
        + (out or "")
        + "\n\n--- stderr ---\n"
        + (err or ""),
    )


def run(
    cmd,
    *,
    cwd=None,
    env=None,
    check=False,
    timeout=None,
    capture=True,
    debug_path: Optional[Path] = None,
    debug_env_keys: Optional[List[str]] = None,
):
    args = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    started = time.time()
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
        err = f"TimeoutExpired: {exp}"
        _write_command_debug(
            debug_path,
            args=args,
            cwd=cwd,
            env=env,
            env_keys=debug_env_keys,
            rc=124,
            out="",
            err=err,
            elapsed=time.time() - started,
        )
        return 124, "", err
    except UnicodeDecodeError as exp:
        err = f"UnicodeDecodeError: {exp}"
        _write_command_debug(
            debug_path,
            args=args,
            cwd=cwd,
            env=env,
            env_keys=debug_env_keys,
            rc=1,
            out="",
            err=err,
            elapsed=time.time() - started,
        )
        return 1, "", err

    rc = proc.returncode
    out = (proc.stdout or "") if capture else ""
    err = (proc.stderr or "") if capture else ""
    _write_command_debug(
        debug_path,
        args=args,
        cwd=cwd,
        env=env,
        env_keys=debug_env_keys,
        rc=rc,
        out=out,
        err=err,
        elapsed=time.time() - started,
    )
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


def _lock_path_for_metadata_dir(metadata_dir: Path) -> Path:
    key = hashlib.md5(str(metadata_dir).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_peglib.{key}.lock"


def load_bugs() -> List[BugEntry]:
    if not BUGS_JSON.exists():
        raise FileNotFoundError(f"Missing {BUGS_JSON}")
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for item in data:
        files = item.get("files") or {}
        type_info = item.get("type") or {}
        bugs.append(
            BugEntry(
                sha_after=item.get("commit_after") or "",
                sha_before=item.get("commit_before") or "",
                src_files=list(files.get("src") or []),
                test_files=list(files.get("test") or []),
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
    run(["git", "clean", "-fdx"], cwd=repo_dir, check=True)
    rc, _, _ = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir)
    if rc != 0:
        run(["git", "fetch", "origin", sha], cwd=repo_dir, check=True)
    run(["git", "checkout", "--force", sha], cwd=repo_dir, check=True)


def _assert_head(repo_dir: Path, expected_sha: str, label: str) -> None:
    rc, out, err = run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture=True)
    if rc != 0:
        raise RuntimeError(f"Cannot read git HEAD for {label}: {(out + err)[-1000:]}")
    actual = out.strip()
    if actual != expected_sha:
        raise RuntimeError(
            f"{label} checkout invariant failed: HEAD={actual}, expected={expected_sha}"
        )


def _assert_paths_match_commit(
    repo_dir: Path,
    commit_sha: str,
    paths: List[str],
    label: str,
) -> None:
    if not paths:
        return
    rc, out, err = run(
        ["git", "diff", "--quiet", commit_sha, "--", *paths],
        cwd=repo_dir,
        capture=True,
    )
    if rc != 0:
        rc2, diff_out, diff_err = run(
            ["git", "diff", "--", commit_sha, "--", *paths],
            cwd=repo_dir,
            capture=True,
        )
        raise RuntimeError(
            f"{label} checkout invariant failed: paths do not match {commit_sha}\n"
            f"{(diff_out + diff_err or out + err)[-2000:]}"
        )


def assert_fixed_tree(repo_dir: Path, bug: BugEntry) -> None:
    _assert_head(repo_dir, bug.sha_after, "fixed")
    _assert_paths_match_commit(repo_dir, bug.sha_after, bug.src_files, "fixed src")
    _assert_paths_match_commit(repo_dir, bug.sha_after, bug.test_files, "fixed tests")


def assert_buggy_tree(repo_dir: Path, bug: BugEntry) -> None:
    _assert_head(repo_dir, bug.sha_after, "buggy fixed-base")
    _assert_paths_match_commit(repo_dir, bug.sha_before, bug.src_files, "buggy src overlay")
    _assert_paths_match_commit(repo_dir, bug.sha_after, bug.test_files, "buggy fixed-tree tests")


def checkout_buggy(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)
    if bug.sha_before and bug.src_files:
        run(
            ["git", "checkout", "--force", bug.sha_before, "--", *bug.src_files],
            cwd=repo_dir,
            check=True,
        )
    assert_buggy_tree(repo_dir, bug)


def checkout_fixed(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)
    assert_fixed_tree(repo_dir, bug)


def _build_flags(*, asan: bool, coverage: bool) -> Tuple[str, str, str]:
    cxxflags = [BASE_CXXFLAGS]
    cflags = [BASE_CFLAGS]
    ldflags: List[str] = []
    if coverage:
        cxxflags.append(COV_FLAGS)
        cflags.append(COV_FLAGS)
        ldflags.append(COV_FLAGS)
    if asan:
        cxxflags.append(ASAN_FLAGS)
        cflags.append(ASAN_FLAGS)
        ldflags.append(ASAN_LDFLAGS)
    return " ".join(cxxflags), " ".join(cflags), " ".join(ldflags)


def _cmake_configure_cmd(
    repo_dir: Path,
    build_dir: Path,
    *,
    asan: bool,
    coverage: bool,
) -> List[str]:
    cxxflags, cflags, ldflags = _build_flags(asan=asan, coverage=coverage)
    cmd = ["cmake"]
    if which("ninja"):
        cmd.extend(["-G", "Ninja"])
    cmd.extend(
        [
            "-S",
            str(repo_dir),
            "-B",
            str(build_dir),
            "-DCMAKE_BUILD_TYPE=Debug",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DCMAKE_CXX_STANDARD=17",
            f"-DCMAKE_CXX_FLAGS={cxxflags}",
            f"-DCMAKE_C_FLAGS={cflags}",
            f"-DCMAKE_EXE_LINKER_FLAGS={ldflags}",
        ]
    )
    return cmd


def _build_env(*, asan: bool) -> dict:
    env = os.environ.copy()
    if asan:
        env.setdefault(
            "ASAN_OPTIONS",
            "detect_leaks=0:halt_on_error=1:abort_on_error=1:symbolize=1",
        )
    return env


def compile_peglib(
    repo_dir: Path,
    *,
    jobs: int,
    asan: bool,
    coverage: bool,
    debug_dir: Optional[Path] = None,
    phase_label: str = "build",
) -> bool:
    if not which("cmake"):
        log("  [build] cmake is not available")
        return False
    build_dir = repo_dir / BUILD_DIR_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir)
    env = _build_env(asan=asan)
    rc, out, err = run(
        _cmake_configure_cmd(repo_dir, build_dir, asan=asan, coverage=coverage),
        cwd=repo_dir,
        env=env,
        capture=True,
        timeout=180,
        debug_path=(debug_dir / phase_label / "01_cmake_configure.log") if debug_dir else None,
        debug_env_keys=["ASAN_OPTIONS", "CFLAGS", "CXXFLAGS", "LDFLAGS"],
    )
    if rc != 0:
        log(f"  [build] cmake configure failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    rc, out, err = run(
        ["cmake", "--build", str(build_dir), "--parallel", str(jobs)],
        cwd=repo_dir,
        env=env,
        capture=True,
        timeout=300,
        debug_path=(debug_dir / phase_label / "02_cmake_build.log") if debug_dir else None,
        debug_env_keys=["ASAN_OPTIONS", "CFLAGS", "CXXFLAGS", "LDFLAGS"],
    )
    if rc != 0:
        log(f"  [build] cmake build failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    if not (build_dir / "test" / "test-main").exists():
        log("  [build] warning: build/test/test-main was not found")
    return True


def _relpath_if_possible(path_str: str, repo_dir: Path) -> str:
    p = Path(path_str)
    try:
        return p.relative_to(repo_dir).as_posix()
    except ValueError:
        return path_str


def _test_id_from_name(index: int, name: str) -> str:
    return f"catch_{index:03d}_{_safe_label(name)}"


def _discover_tests_from_ctest(repo_dir: Path) -> List[TestEntry]:
    build_dir = repo_dir / BUILD_DIR_NAME
    if not which("ctest") or not build_dir.exists():
        return []
    rc, out, err = run(
        ["ctest", "--show-only=json-v1", "--test-dir", str(build_dir)],
        cwd=repo_dir,
        capture=True,
        timeout=60,
    )
    if rc != 0:
        log(f"  [tests] ctest discovery failed rc={rc}\n{(out + err)[-1500:]}")
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        log(f"  [tests] cannot parse ctest json: {exc}")
        return []

    entries: List[TestEntry] = []
    for item in payload.get("tests", []):
        name = item.get("name")
        command = [str(x) for x in (item.get("command") or [])]
        if not name or not command:
            continue
        workdir = build_dir
        for prop in item.get("properties") or []:
            if prop.get("name") == "WORKING_DIRECTORY" and prop.get("value"):
                workdir = Path(prop["value"])
                break
        executable_rel = _relpath_if_possible(command[0], repo_dir)
        entries.append(
            TestEntry(
                test_id=name,
                executable_relpath=executable_rel,
                source_relpath="test/test1.cc",
                test_name=name,
                command=command,
                working_dir_relpath=_relpath_if_possible(str(workdir), repo_dir),
            )
        )
    return entries


def _discover_catch_tests_from_binary(repo_dir: Path, base_entry: TestEntry) -> List[TestEntry]:
    exe = repo_dir / base_entry.executable_relpath
    if not exe.exists():
        return []
    workdir_rel = base_entry.working_dir_relpath or "."
    workdir = repo_dir / workdir_rel
    if not workdir.exists():
        workdir = repo_dir

    rc, out, err = run(
        [str(exe), "--list-test-names-only"],
        cwd=workdir,
        capture=True,
        timeout=60,
    )
    names = []
    seen = set()
    for line in out.splitlines():
        name = line.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)

    # Catch v2.2.2 returns the number of listed tests for this command in this
    # project. Treat non-empty stdout as successful discovery.
    if rc != 0 and not names:
        log(f"  [tests] Catch test listing failed rc={rc}\n{(out + err)[-1500:]}")
        return []

    entries: List[TestEntry] = []
    for idx, name in enumerate(names, 1):
        entries.append(
            TestEntry(
                test_id=_test_id_from_name(idx, name),
                executable_relpath=base_entry.executable_relpath,
                source_relpath=base_entry.source_relpath,
                test_name=name,
                command=[str(exe), name],
                working_dir_relpath=workdir_rel,
            )
        )
    return entries


def discover_tests(repo_dir: Path, bug: BugEntry) -> List[TestEntry]:
    build_dir = repo_dir / BUILD_DIR_NAME
    ctest_entries = _discover_tests_from_ctest(repo_dir)
    tests = _discover_catch_tests_from_binary(repo_dir, ctest_entries[0]) if ctest_entries else []
    if tests:
        log(f"  [tests] discovered {len(tests)} Catch test case(s)")
    else:
        exe = build_dir / "test" / "test-main"
        if exe.exists():
            base_entry = TestEntry(
                test_id="TestMain",
                executable_relpath=f"{BUILD_DIR_NAME}/test/test-main",
                source_relpath=(bug.test_files[0] if bug.test_files else "test/test1.cc"),
                test_name="TestMain",
                command=[str(exe)],
                working_dir_relpath=f"{BUILD_DIR_NAME}/test",
            )
            tests = _discover_catch_tests_from_binary(repo_dir, base_entry)
    if not tests:
        raise RuntimeError(
            "Cannot discover individual Catch test cases from test/test-main. "
            "Refusing to generate aggregate TestMain-only metadata."
        )

    existing: List[TestEntry] = []
    for te in tests:
        exe = repo_dir / te.executable_relpath
        if exe.exists():
            existing.append(te)
        else:
            log(f"  [tests] skip {te.test_id}: missing {te.executable_relpath}")
    return existing


def _has_failure_marker(output: str) -> bool:
    return any(marker in output for marker in ASAN_ERROR_MARKERS)


def clear_gcda(repo_dir: Path) -> None:
    for path in repo_dir.rglob("*.gcda"):
        try:
            path.unlink()
        except OSError:
            pass


def run_one_test(
    repo_dir: Path,
    te: TestEntry,
    *,
    timeout: int,
    asan_env: bool,
    debug_path: Optional[Path] = None,
) -> Tuple[bool, str, str]:
    env = _build_env(asan=asan_env)
    cmd = te.command or [str(repo_dir / te.executable_relpath)]
    workdir = repo_dir / te.working_dir_relpath
    if not workdir.exists():
        workdir = repo_dir
    rc, out, err = run(
        cmd,
        cwd=workdir,
        env=env,
        timeout=timeout,
        capture=True,
        debug_path=debug_path,
        debug_env_keys=["ASAN_OPTIONS"],
    )
    combined = (out or "") + (err or "")
    failed = rc != 0 or _has_failure_marker(combined)
    if not failed:
        return True, combined, ""
    reason = "asan_error" if _has_failure_marker(combined) else f"exit_code={rc}"
    if rc == 124:
        reason = "timeout"
    return False, combined, reason


def run_tests(
    repo_dir: Path,
    entries: List[TestEntry],
    *,
    collect_cov: bool,
    test_timeout: int,
    phase_label: str,
    asan_env: bool,
    debug_dir: Optional[Path] = None,
    label_retries: int = 1,
) -> Tuple[List[TestResult], int]:
    results: List[TestResult] = []
    n_with_cov = 0
    for idx, te in enumerate(entries, 1):
        clear_gcda(repo_dir)
        test_label = f"{idx:03d}_{_safe_label(te.test_id)}"
        attempts = max(label_retries if asan_env and not collect_cov else 1, 1)
        passed = True
        combined_output = ""
        reason = ""
        attempt_used = 0
        for attempt in range(1, attempts + 1):
            attempt_used = attempt
            suffix = (
                f"{test_label}.attempt{attempt:02d}.test.log"
                if attempts > 1
                else f"{test_label}.test.log"
            )
            passed, combined_output, reason = run_one_test(
                repo_dir,
                te,
                timeout=test_timeout,
                asan_env=asan_env,
                debug_path=(debug_dir / phase_label / suffix) if debug_dir else None,
            )
            if not passed:
                break
        covered_funcs: List[str] = []
        cov_map: Dict[str, List[str]] = {}
        if collect_cov:
            cov_map = collect_coverage(
                repo_dir,
                debug_dir=(debug_dir / phase_label) if debug_dir else None,
                debug_label=test_label,
            )
            covered_funcs = coverage_to_qualified(cov_map)
            if not covered_funcs and not passed:
                covered_funcs = fallback_coverage_from_output(combined_output, repo_dir)
            if covered_funcs:
                n_with_cov += 1
            if debug_dir:
                _write_text(
                    debug_dir / phase_label / f"{test_label}.coverage.json",
                    json.dumps(
                        {
                            "coverage_map": cov_map,
                            "covered_functions": covered_funcs,
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                )
        if debug_dir:
            _write_text(
                debug_dir / phase_label / f"{test_label}.summary.json",
                json.dumps(
                    {
                        "test_id": te.test_id,
                        "test_name": te.test_name,
                        "command": te.command or [str(repo_dir / te.executable_relpath)],
                        "working_dir": str(repo_dir / te.working_dir_relpath),
                        "outcome": "PASS" if passed else "FAIL",
                        "fail_reason": "" if passed else reason,
                        "attempts": attempt_used,
                        "max_attempts": attempts,
                        "output_tail": combined_output[-2000:],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        results.append(
            TestResult(
                test_id=te.test_id,
                outcome="PASS" if passed else "FAIL",
                test_name=te.test_name,
                fail_reason="" if passed else reason,
                actual_output="" if passed else combined_output[-4000:],
                expected_output="",
                covered_functions=covered_funcs,
            )
        )
        n_fail = sum(1 for r in results if r.outcome == "FAIL")
        cov_info = f", with_coverage={n_with_cov}" if collect_cov else " (no-cov run)"
        log(f"  [{phase_label}] {idx}/{len(entries)} done (fail={n_fail}{cov_info})")
    return results, n_with_cov


def _repo_relative_from_gcov(file_name: str, repo_dir: Path, gcda: Path) -> Optional[str]:
    raw = file_name.strip()
    if not raw or raw.startswith("<"):
        return None
    if _is_coverage_excluded_file(raw):
        return None
    candidates: List[Path] = []
    p = Path(raw)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(gcda.parent / p)
        candidates.append(repo_dir / p)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            rel = resolved.relative_to(repo_dir.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        if _is_coverage_excluded_file(rel):
            return None
        return rel
    return None


def _is_coverage_excluded_file(path: str) -> bool:
    rel = str(path).replace("\\", "/").lstrip("./")
    base = os.path.basename(rel)
    return (
        rel.startswith(".git/")
        or rel.startswith(f"{BUILD_DIR_NAME}/")
        or rel.startswith("test/")
        or "/test/" in rel
        or base == "catch.hh"
    )


def _parse_gcov_output(output: str, repo_dir: Path, gcda: Path) -> Dict[str, List[str]]:
    current_file: Optional[str] = None
    pending_func: Optional[Tuple[str, str]] = None
    covered: Dict[str, List[str]] = {}

    for line in output.splitlines():
        m_file = _GCOV_FILE_RE.match(line)
        if m_file:
            current_file = _repo_relative_from_gcov(m_file.group(1), repo_dir, gcda)
            pending_func = None
            continue

        m_func_lines = _GCOV_FUNC_LINES_RE.match(line)
        if m_func_lines and current_file:
            pending_func = (current_file, m_func_lines.group(1))
            continue

        m_lines = _GCOV_LINES_EXEC_RE.match(line)
        if m_lines and pending_func:
            try:
                pct = float(m_lines.group(1))
            except ValueError:
                pct = 0.0
            if pct > 0.0:
                rel_file, func_name = pending_func
                covered.setdefault(rel_file, []).append(_normalize_cpp_function(func_name))
            pending_func = None
            continue

        m_called = _GCOV_FUNC_CALLED_RE.match(line)
        if m_called and current_file:
            try:
                calls = int(m_called.group("calls"))
            except ValueError:
                calls = 0
            if calls > 0:
                covered.setdefault(current_file, []).append(_normalize_cpp_function(m_called.group("name")))

    return {fname: sorted(set(funcs)) for fname, funcs in covered.items()}


def _parse_gcov_json(path: Path, repo_dir: Path, gcda: Path) -> Dict[str, List[str]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fp:
            payload = json.load(fp)
    except (OSError, gzip.BadGzipFile, json.JSONDecodeError):
        return {}

    covered: Dict[str, List[str]] = {}
    for file_info in payload.get("files") or []:
        rel_file = _repo_relative_from_gcov(str(file_info.get("file") or ""), repo_dir, gcda)
        if not rel_file:
            continue
        funcs: List[str] = []
        for fn in file_info.get("functions") or []:
            try:
                count = int(fn.get("execution_count") or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                continue
            name = fn.get("demangled_name") or fn.get("name")
            if name:
                funcs.append(_normalize_cpp_function(str(name)))
        if funcs:
            covered.setdefault(rel_file, []).extend(funcs)
    return {fname: sorted(set(funcs)) for fname, funcs in covered.items()}


def collect_coverage(
    repo_dir: Path,
    *,
    debug_dir: Optional[Path] = None,
    debug_label: str = "coverage",
) -> Dict[str, List[str]]:
    if not which("gcov"):
        return {}
    build_dir = repo_dir / BUILD_DIR_NAME
    gcda_files = list(build_dir.rglob("*.gcda"))
    covered: Dict[str, List[str]] = {}

    for gcov_file in repo_dir.rglob("*.gcov"):
        try:
            gcov_file.unlink()
        except OSError:
            pass

    for gcda in gcda_files:
        for old_json in gcda.parent.glob("*.gcov.json.gz"):
            try:
                old_json.unlink()
            except OSError:
                pass

        json_cmd = ["gcov", "--json-format", "-b", "-c", "-m", gcda.name]
        gcov_label = f"{debug_label}_{_safe_label(gcda.name)}"
        rc_json, out_json, err_json = run(
            json_cmd,
            cwd=gcda.parent,
            capture=True,
            timeout=60,
            debug_path=(debug_dir / f"{gcov_label}.gcov_json.log") if debug_dir else None,
        )
        if rc_json == 0:
            json_covered_current = False
            for json_path in gcda.parent.glob("*.gcov.json.gz"):
                parsed_json = _parse_gcov_json(json_path, repo_dir, gcda)
                for fname, funcs in parsed_json.items():
                    covered.setdefault(fname, []).extend(funcs)
                    json_covered_current = True
                try:
                    json_path.unlink()
                except OSError:
                    pass
            if json_covered_current:
                continue

        cmd = ["gcov", "-f", "-b", "-c", "-m", gcda.name]
        rc, out, err = run(
            cmd,
            cwd=gcda.parent,
            capture=True,
            timeout=60,
            debug_path=(debug_dir / f"{gcov_label}.gcov.log") if debug_dir else None,
        )
        if rc != 0:
            rc, out, err = run(
                ["gcov", "-f", "-b", "-c", gcda.name],
                cwd=gcda.parent,
                capture=True,
                timeout=60,
                debug_path=(debug_dir / f"{gcov_label}.gcov_fallback.log") if debug_dir else None,
            )
        if rc != 0:
            log(f"  [cov] gcov failed for {gcda.name}: {(out + err)[-800:]}")
            continue
        for fname, funcs in _parse_gcov_output(out + err, repo_dir, gcda).items():
            covered.setdefault(fname, []).extend(funcs)

    for gcov_file in repo_dir.rglob("*.gcov"):
        try:
            gcov_file.unlink()
        except OSError:
            pass
    return {fname: sorted(set(funcs)) for fname, funcs in covered.items()}


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for fname, funcs in cov_map.items():
        base = os.path.basename(fname)
        for fn in funcs:
            out.append(f"{base}:{fn}")
    return sorted(set(out))


def fallback_coverage_from_output(output: str, repo_dir: Path) -> List[str]:
    covered: List[str] = []
    repo_resolved = repo_dir.resolve()
    for m in _ASAN_FRAME_RE.finditer(output):
        func = m.group("func").strip()
        file_path = Path(m.group("file"))
        try:
            rel = file_path.resolve().relative_to(repo_resolved).as_posix()
        except (OSError, ValueError):
            continue
        if _is_coverage_excluded_file(rel):
            continue
        if func.startswith("__interceptor_"):
            continue
        covered.append(f"{os.path.basename(rel)}:{func}")
    return sorted(set(covered))


def _script_arg(arg: str, repo_dir: Path) -> str:
    try:
        rel = Path(arg).relative_to(repo_dir).as_posix()
        return f'"$ROOT/{rel}"'
    except ValueError:
        return shlex.quote(arg)


def write_run_one_test(repo_dir: Path, entries: List[TestEntry]) -> None:
    case_lines = []
    for te in entries:
        cmd = te.command or [str(repo_dir / te.executable_relpath)]
        cmd_items = " ".join(_script_arg(x, repo_dir) for x in cmd)
        cwd = te.working_dir_relpath or "."
        case_lines.append(
            f"{shlex.quote(te.test_id)}) TEST_CWD={shlex.quote(cwd)}; "
            f"TEST_CMD=({cmd_items}) ;;"
        )

    script = repo_dir / "run_one_test.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            ROOT="$(cd "$(dirname "$0")" && pwd)"
            TEST_ID="${{1:-}}"
            if [[ -z "$TEST_ID" ]]; then
              echo "usage: $0 <test_id>" >&2
              exit 2
            fi

            case "$TEST_ID" in
            """
        )
        + "\n".join("  " + line for line in case_lines)
        + textwrap.dedent(
            """
              *)
                echo "[run_one_test] unknown test_id '$TEST_ID'" >&2
                exit 2
                ;;
            esac

            export ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0:halt_on_error=1:abort_on_error=1:symbolize=1}"
            cd "$ROOT/$TEST_CWD"
            exec "${TEST_CMD[@]}"
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)


def _normalize_cpp_function(name: str) -> str:
    """Strip C++ noise from a function name to produce a short canonical form.

    Removes: parameter list, return type, template arguments, leading
    keywords, peglib-specific namespace prefixes, and common internal
    namespace prefixes.
    """
    name = re.sub(r"\s+", " ", str(name)).strip()
    if not name:
        return ""
    name = _strip_cpp_parameter_list(name)
    name = re.sub(r"^(virtual|static|constexpr|const|inline|typename)\s+", "", name)
    # Strip peglib library namespace prefix
    if name.startswith("peg::"):
        name = name[len("peg::"):]
    name = _drop_cpp_return_type(name)
    name = _strip_cpp_template_args(name)
    # Strip common internal / anonymous namespace prefixes
    for internal_prefix in ("detail::", "internal::", "Catch::", "(anonymous namespace)::"):
        if name.startswith(internal_prefix):
            name = name[len(internal_prefix):]
            break
    return name.strip()


def _strip_cpp_parameter_list(name: str) -> str:
    angle_depth = 0
    for idx, ch in enumerate(name):
        if ch == "<":
            angle_depth += 1
        elif ch == ">" and angle_depth:
            angle_depth -= 1
        elif ch == "(" and angle_depth == 0:
            if name[max(0, idx - 8):idx] == "operator":
                continue
            return name[:idx].strip()
    return name


def _drop_cpp_return_type(name: str) -> str:
    if "operator " in name:
        return name
    angle_depth = 0
    last_top_level_space = -1
    for idx, ch in enumerate(name):
        if ch == "<":
            angle_depth += 1
        elif ch == ">" and angle_depth:
            angle_depth -= 1
        elif ch.isspace() and angle_depth == 0:
            last_top_level_space = idx
    if last_top_level_space >= 0:
        candidate = name[last_top_level_space + 1:].strip()
        if candidate:
            return candidate
    return name


def _strip_cpp_template_args(name: str) -> str:
    out: List[str] = []
    angle_depth = 0
    for ch in name:
        if ch == "<":
            angle_depth += 1
            continue
        if ch == ">" and angle_depth:
            angle_depth -= 1
            continue
        if angle_depth == 0:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _extract_ground_truth_funcs(bug: BugEntry, repo_dir: Path) -> List[str]:
    if not bug.sha_before or not bug.src_files:
        return []
    funcs = set()
    source_cache: Dict[str, str] = {}
    rc, diff, _ = run(["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files], cwd=repo_dir)
    if rc == 0 and diff:
        changed_lines = _changed_new_lines_from_diff(diff)
        for src_file, line_numbers in changed_lines.items():
            if src_file not in bug.src_files:
                continue
            if src_file not in source_cache:
                rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo_dir)
                source_cache[src_file] = text if rc_show == 0 else ""
            for line_no in line_numbers:
                fn = _find_enclosing_cpp_function(source_cache[src_file], line_no)
                if fn:
                    funcs.add(fn)
                    break

        for line in diff.splitlines():
            if not line.startswith("@@"):
                continue
            if funcs:
                break
            tail = line.split("@@", 2)[-1].strip()
            header_func = _function_from_diff_tail(tail)
            hunk = re.match(r"@@\s+-\d+(?:,\d+)?\s+\+(?P<start>\d+)(?:,(?P<count>\d+))?", line)
            if not hunk:
                if header_func:
                    funcs.add(header_func)
                continue
            start = int(hunk.group("start"))
            count = int(hunk.group("count") or "1")
            found_scoped = False
            for src_file in bug.src_files:
                if src_file not in source_cache:
                    rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo_dir)
                    source_cache[src_file] = text if rc_show == 0 else ""
                for candidate_line in range(start, start + max(count, 1)):
                    fn = _find_enclosing_cpp_function(source_cache[src_file], candidate_line)
                    if fn:
                        funcs.add(fn)
                        found_scoped = True
                        break
            if header_func and not found_scoped:
                funcs.add(header_func)

    if not funcs:
        loc = (bug.raw.get("files") or {}).get("src0_location") or {}
        line_no = loc.get("line_number") or loc.get("hunk_start") or loc.get("func_start")
        if line_no:
            for src_file in bug.src_files:
                rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo_dir)
                if rc_show == 0:
                    fn = _find_enclosing_cpp_function(text, int(line_no))
                    if fn:
                        funcs.add(fn)

    return sorted(funcs)


def _changed_new_lines_from_diff(diff: str) -> Dict[str, List[int]]:
    changed: Dict[str, List[int]] = {}
    current_file = ""
    new_line = 0

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
            changed.setdefault(current_file, [])
            continue
        hunk = re.match(r"@@\s+-\d+(?:,\d+)?\s+\+(?P<start>\d+)(?:,\d+)?\s+@@", line)
        if hunk:
            new_line = int(hunk.group("start"))
            continue
        if not current_file or not new_line:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed.setdefault(current_file, []).append(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            changed.setdefault(current_file, []).append(new_line)
        elif line.startswith(" "):
            new_line += 1
    return {path: sorted(set(lines)) for path, lines in changed.items() if lines}


def _function_from_diff_tail(tail: str) -> str:
    if not tail:
        return ""
    keywords = {"if", "for", "while", "switch", "return", "sizeof", "case", "do"}
    match = re.search(r"([A-Za-z_~][\w:~<>]*)\s*\(", tail)
    if not match:
        return ""
    name = _normalize_cpp_function(match.group(1))
    leaf = name.rsplit("::", 1)[-1]
    return "" if leaf in keywords else name


def _find_enclosing_cpp_function(source: str, line_number: int) -> str:
    if not source or line_number <= 0:
        return ""
    line_functions = _build_cpp_function_line_map(source)
    return line_functions.get(line_number, "")


def _build_cpp_function_line_map(source: str) -> Dict[int, str]:
    if not source:
        return {}
    lines = source.splitlines()
    line_functions: Dict[int, str] = {}
    scope_stack: List[Tuple[int, str]] = []
    function_stack: List[Tuple[int, str]] = []
    brace_depth = 0
    pending = ""
    namespace_re = re.compile(r"\bnamespace\s+([A-Za-z_]\w*)\b")
    type_re = re.compile(r"\b(?:class|struct)\s+([A-Za-z_]\w*)\b")

    for line_no, raw_line in enumerate(lines, 1):
        line = re.sub(r"//.*", "", raw_line)
        line = re.sub(r"/\*.*?\*/", " ", line)
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            pending = ""
            if function_stack:
                line_functions[line_no] = function_stack[-1][1]
            continue

        before_open = ""
        if "{" in stripped:
            before_open = f"{pending} {stripped.split('{', 1)[0]}".strip()

        namespace_match = namespace_re.search(before_open)
        type_match = type_re.search(before_open)
        opens = line.count("{")
        closes = line.count("}")

        if opens:
            if namespace_match:
                scope_stack.append((brace_depth + 1, namespace_match.group(1)))
            elif type_match:
                scope_stack.append((brace_depth + 1, type_match.group(1)))
            else:
                fn = _function_from_signature(before_open)
                if fn:
                    if "::" not in fn and scope_stack:
                        fn = _normalize_cpp_function(
                            f"{'::'.join(name for _, name in scope_stack)}::{fn}"
                        )
                    function_stack.append((brace_depth + 1, fn))

        brace_depth += opens - closes
        while function_stack and brace_depth < function_stack[-1][0]:
            function_stack.pop()
        while scope_stack and brace_depth < scope_stack[-1][0]:
            scope_stack.pop()

        if "{" in stripped or "}" in stripped or stripped.endswith(";"):
            pending = ""
        else:
            pending = f"{pending} {stripped}".strip()

        if function_stack:
            line_functions[line_no] = function_stack[-1][1]

    return line_functions


def _function_from_signature(signature: str) -> str:
    signature = re.sub(r"//.*", "", signature)
    signature = re.sub(r"/\*.*?\*/", " ", signature)
    signature = re.sub(r"\s+", " ", signature).strip()
    if not signature or signature.startswith(("if ", "for ", "while ", "switch ", "return ")):
        return ""
    before_args = _strip_cpp_parameter_list(signature)
    if "[" in before_args or "]" in before_args or before_args.endswith("="):
        return ""
    name = _normalize_cpp_function(before_args)
    if not name:
        return ""
    leaf = name.rsplit("::", 1)[-1]
    if leaf in {"if", "for", "while", "switch", "return", "sizeof"}:
        return ""
    return name


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
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def _existing_output_is_complete(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("build_error"):
        return False
    tests = payload.get("tests") or []
    if len(tests) <= 1 and (tests[0].get("test_id") if tests else "") in {"TestMain", "test-main"}:
        return False
    return True


def _empty_record(bug: BugEntry, repo_dir: Path, compile_cmd: str, *, error: str) -> dict:
    source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
    return {
        "bug_id": bug.bug_id,
        "dataset_name": "defects4c",
        "language": "C++",
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
        "tests": [],
        "build_error": error,
    }


def _compile_cmd_for_meta(repo_dir: Path, *, jobs: int) -> str:
    build_dir = repo_dir / BUILD_DIR_NAME
    cfg = _cmake_configure_cmd(repo_dir, build_dir, asan=True, coverage=False)
    return " ".join(shlex.quote(x) for x in cfg) + (
        f" && cmake --build {shlex.quote(str(build_dir))} --parallel {jobs}"
    )


def _select_phase_b_entries(
    entries: List[TestEntry],
    results_a: List[TestResult],
    bug: BugEntry,
    gcov_scope: str,
) -> List[TestEntry]:
    fail_ids = {r.test_id for r in results_a if r.outcome == "FAIL"}
    regression_sources = set(bug.test_files)
    if gcov_scope == "all":
        return list(entries)
    if gcov_scope == "fail":
        return [t for t in entries if t.test_id in fail_ids]
    if gcov_scope == "regression":
        return [t for t in entries if t.source_relpath in regression_sources]
    if gcov_scope == "fail+regression":
        return [
            t
            for t in entries
            if t.test_id in fail_ids or t.source_relpath in regression_sources
        ]
    return list(entries)


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
    gcov_scope: str,
    test_timeout: int,
    debug_dir: Optional[Path],
    label_retries: int,
) -> Optional[Path]:
    out_path = metadata_dir / f"{bug.safe_bug_id}_meta.json"
    raw_out_path = raw_dir / f"{bug.safe_bug_id}_meta.json"
    debug_bug_dir = debug_dir / bug.safe_bug_id if debug_dir else None

    try:
        repo_dir = ensure_repo(bug, out_root, clone=clone)
    except Exception as exc:
        log(f"  [error] cannot find/clone repo: {exc}")
        return None

    compile_cmd = _compile_cmd_for_meta(repo_dir, jobs=jobs)
    if debug_bug_dir:
        if debug_bug_dir.exists():
            shutil.rmtree(debug_bug_dir)
        _write_text(
            debug_bug_dir / "00_bug.json",
            json.dumps(
                {
                    "bug_id": bug.bug_id,
                    "repo_dir": str(repo_dir),
                    "commit_after": bug.sha_after,
                    "commit_before": bug.sha_before,
                    "src_files": bug.src_files,
                    "test_files": bug.test_files,
                    "compile_cmd": compile_cmd,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )

    try:
        checkout_buggy(repo_dir, bug)
        if debug_bug_dir:
            run(
                ["git", "status", "--short"],
                cwd=repo_dir,
                capture=True,
                timeout=30,
                debug_path=debug_bug_dir / "phaseA-buggy" / "00_git_status.log",
            )
            run(
                ["git", "diff", "--", *bug.src_files],
                cwd=repo_dir,
                capture=True,
                timeout=30,
                debug_path=debug_bug_dir / "phaseA-buggy" / "00_src_overlay.diff.log",
            )
    except Exception as exc:
        log(f"  [error] checkout buggy failed: {exc}")
        return None

    if dual_run:
        phase_info: Dict[str, object] = {
            "mode": "dual",
            "phase_b_scope": gcov_scope if not skip_coverage else "skip_coverage",
            "version_policy": "fixed=commit_after; buggy=commit_after_with_files.src_from_commit_before",
            "checkout_invariants": "asserted_before_each_build",
            "test_policy": "fixed_tree_catch_tests_for_buggy_and_fixed",
            "coverage_build": "non_asan_gcov",
            "final_build": "asan_buggy",
        }

        log("  [phaseA-buggy] checkout+build ASAN")
        if not compile_peglib(
            repo_dir,
            jobs=jobs,
            asan=True,
            coverage=False,
            debug_dir=debug_bug_dir,
            phase_label="phaseA-buggy",
        ):
            record = _empty_record(bug, repo_dir, compile_cmd, error="phaseA_buggy_compile_failed")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        entries = discover_tests(repo_dir, bug)
        phase_info["test_discovery"] = {
            "runner": "catch",
            "scope": "individual_test_cases",
            "count": len(entries),
        }
        write_run_one_test(repo_dir, entries)
        results_a, _ = run_tests(
            repo_dir,
            entries,
            collect_cov=False,
            test_timeout=test_timeout,
            phase_label="phaseA-buggy",
            asan_env=True,
            debug_dir=debug_bug_dir,
            label_retries=label_retries,
        )

        fixed_outcome_by_test: Dict[str, str] = {}
        try:
            checkout_fixed(repo_dir, bug)
            if debug_bug_dir:
                run(
                    ["git", "status", "--short"],
                    cwd=repo_dir,
                    capture=True,
                    timeout=30,
                    debug_path=debug_bug_dir / "phaseA-fixed" / "00_git_status.log",
                )
            log("  [phaseA-fixed] checkout+build ASAN (outcome_fixed)")
            if compile_peglib(
                repo_dir,
                jobs=jobs,
                asan=True,
                coverage=False,
                debug_dir=debug_bug_dir,
                phase_label="phaseA-fixed",
            ):
                fixed_results, _ = run_tests(
                    repo_dir,
                    entries,
                    collect_cov=False,
                    test_timeout=test_timeout,
                    phase_label="phaseA-fixed",
                    asan_env=True,
                    debug_dir=debug_bug_dir,
                    label_retries=label_retries,
                )
                fixed_outcome_by_test = {r.test_id: r.outcome for r in fixed_results}
                phase_info["phase_a_fixed_status"] = "ok"
                phase_info["phase_a_fixed_fail_count"] = sum(
                    1 for r in fixed_results if r.outcome == "FAIL"
                )
            else:
                phase_info["phase_a_fixed_status"] = "compile_failed"
        finally:
            checkout_buggy(repo_dir, bug)

        cov_by_test: Dict[str, List[str]] = {}
        selected_phase_b = _select_phase_b_entries(entries, results_a, bug, gcov_scope)
        if skip_coverage:
            selected_phase_b = []
        elif selected_phase_b:
            log(
                "  [phaseB] checkout buggy + build GCOV "
                f"(scope={gcov_scope}, tests={len(selected_phase_b)})"
            )
            if compile_peglib(
                repo_dir,
                jobs=jobs,
                asan=False,
                coverage=True,
                debug_dir=debug_bug_dir,
                phase_label="phaseB",
            ):
                results_b, n_cov = run_tests(
                    repo_dir,
                    selected_phase_b,
                    collect_cov=True,
                    test_timeout=test_timeout,
                    phase_label="phaseB",
                    asan_env=False,
                    debug_dir=debug_bug_dir,
                )
                cov_by_test = {r.test_id: r.covered_functions for r in results_b}
                phase_info["phase_b_with_coverage"] = n_cov
                phase_info["phase_b_test_count"] = len(selected_phase_b)
            else:
                phase_info["phase_b_status"] = "compile_failed"
        else:
            phase_info["phase_b_test_count"] = 0

        log("  [finalize] rebuild buggy ASAN for test_cmd_template")
        try:
            checkout_buggy(repo_dir, bug)
            if compile_peglib(
                repo_dir,
                jobs=jobs,
                asan=True,
                coverage=False,
                debug_dir=debug_bug_dir,
                phase_label="finalize-buggy",
            ):
                write_run_one_test(repo_dir, entries)
                phase_info["final_build_status"] = "ok"
            else:
                phase_info["final_build_status"] = "compile_failed"
        except Exception as exc:
            phase_info["final_build_status"] = f"failed: {exc}"

        results = [
            TestResult(
                test_id=r.test_id,
                outcome=r.outcome,
                test_name=r.test_name,
                outcome_fixed=fixed_outcome_by_test.get(r.test_id, ""),
                fail_reason=r.fail_reason,
                actual_output=r.actual_output,
                expected_output=r.expected_output,
                covered_functions=cov_by_test.get(r.test_id, []),
            )
            for r in results_a
        ]
        phase_info["phase_a_fail_count"] = sum(1 for r in results if r.outcome == "FAIL")
    else:
        phase_info = {
            "mode": "single",
            "version_policy": "fixed=commit_after; buggy=commit_after_with_files.src_from_commit_before",
            "checkout_invariants": "asserted_before_build",
            "test_policy": "fixed_tree_catch_tests",
        }
        if not compile_peglib(
            repo_dir,
            jobs=jobs,
            asan=asan,
            coverage=not skip_coverage,
            debug_dir=debug_bug_dir,
            phase_label="single",
        ):
            record = _empty_record(bug, repo_dir, compile_cmd, error="compile_failed")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        entries = discover_tests(repo_dir, bug)
        write_run_one_test(repo_dir, entries)
        results, n_cov = run_tests(
            repo_dir,
            entries,
            collect_cov=not skip_coverage,
            test_timeout=test_timeout,
            phase_label="single",
            asan_env=asan,
            debug_dir=debug_bug_dir,
        )
        phase_info["with_coverage"] = n_cov

    source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
    ground_truth_functions = _extract_ground_truth_funcs(bug, repo_dir)
    test_cmd_template = f"bash {shlex.quote(str(repo_dir / 'run_one_test.sh'))} {{test_id}}"
    record = {
        "bug_id": bug.bug_id,
        "dataset_name": "defects4c",
        "language": "C++",
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
        prog="build_meta_peglib.py",
        description="Build Unified-Debugging metadata for yhirose/cpp-peglib.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sha", action="append", default=[], help="Only process commit_after prefix.")
    ap.add_argument("--only", dest="sha", action="append", help="Alias of --sha.")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of bugs (0 = all).")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT, help=f"Repo output root (default: {DEFAULT_OUT_ROOT}).")
    ap.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR, help=f"Metadata output dir (default: {DEFAULT_METADATA_DIR}).")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Raw output dir (default: {DEFAULT_RAW_DIR}).")
    ap.add_argument("--debug-artifacts", action="store_true", help="Write build/test/gcov logs for each phase.")
    ap.add_argument("--debug-dir", type=Path, default=DEFAULT_DEBUG_DIR, help=f"Debug artifact dir (default: {DEFAULT_DEBUG_DIR}).")
    ap.add_argument("--jobs", type=int, default=max(os.cpu_count() or 2, 2) - 1, help="Parallel build jobs.")
    ap.add_argument("--test-timeout", type=int, default=DEFAULT_TEST_TIMEOUT, help="Timeout for each test executable.")
    ap.add_argument("--label-retries", type=int, default=DEFAULT_LABEL_RETRIES, help="ASAN label attempts per test; any failing attempt marks FAIL.")
    ap.add_argument("--skip-coverage", action="store_true", help="Do not collect gcov coverage.")
    ap.add_argument("--asan", action="store_true", help="Use ASAN in single-run mode.")
    ap.add_argument("--dual-run", action="store_true", help="Phase A buggy/fixed outcomes + Phase B buggy coverage.")
    ap.add_argument(
        "--gcov-scope",
        choices=["all", "fail", "regression", "fail+regression"],
        default="all",
        help="When --dual-run is used, select tests for Phase B.",
    )
    ap.add_argument("--skip-if-exists", action="store_true", help="Skip bugs with existing metadata.")
    ap.add_argument("--clone", action="store_true", help="Clone cpp-peglib from GitHub if missing.")
    ap.add_argument("--prepare-repos", action="store_true", help="Prepare repos by bug_id, then exit.")
    ap.add_argument("--list", action="store_true", help="List matching bugs, then exit.")
    return ap


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    bugs = load_bugs()
    if args.sha:
        wanted = tuple(s for s in args.sha if s)
        bugs = [
            b
            for b in bugs
            if b.sha_after in wanted or b.sha_after.startswith(wanted)
        ]
    if args.limit:
        bugs = bugs[: args.limit]

    if args.list:
        for bug in bugs:
            print(
                f"{bug.sha_after}\t{bug.bug_id}\t"
                f"{repo_dir_for_bug(args.out_root, bug).name}\t"
                f"{','.join(bug.src_files)}"
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

    if not bugs:
        log("No matching bug.")
        return 1

    args.metadata_dir.mkdir(parents=True, exist_ok=True)
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    if args.debug_artifacts:
        args.debug_dir.mkdir(parents=True, exist_ok=True)

    log(f"Will process {len(bugs)} bug(s).")
    log(f"  metadata_dir = {args.metadata_dir}")
    log(f"  raw_dir      = {args.raw_dir}")
    log(f"  dual_run     = {args.dual_run}")
    if args.debug_artifacts:
        log(f"  debug_dir    = {args.debug_dir}")
    if args.dual_run:
        log(f"  gcov_scope   = {args.gcov_scope}")

    lock_fp = None
    try:
        lock_fp = _acquire_single_run_lock(_lock_path_for_metadata_dir(args.metadata_dir))
    except RuntimeError as exc:
        log(f"[error] {exc}")
        return 2

    ok = 0
    try:
        for idx, bug in enumerate(bugs, 1):
            out_path = args.metadata_dir / f"{bug.safe_bug_id}_meta.json"
            if args.skip_if_exists and _existing_output_is_complete(out_path):
                log(f"[{idx}/{len(bugs)}] skip existing {bug.bug_id}")
                ok += 1
                continue
            log(f"[{idx}/{len(bugs)}] {bug.bug_id} after={bug.sha_after[:12]}")
            result = process_bug(
                bug,
                out_root=args.out_root,
                metadata_dir=args.metadata_dir,
                raw_dir=args.raw_dir,
                jobs=args.jobs,
                clone=args.clone,
                skip_coverage=args.skip_coverage,
                asan=args.asan,
                dual_run=args.dual_run,
                gcov_scope=args.gcov_scope,
                test_timeout=args.test_timeout,
                debug_dir=args.debug_dir if args.debug_artifacts else None,
                label_retries=args.label_retries,
            )
            if result:
                ok += 1
    finally:
        if lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fp.close()

    log(f"Done: {ok}/{len(bugs)} bug(s) have output.")
    return 0 if ok == len(bugs) else 1


if __name__ == "__main__":
    sys.exit(main())
