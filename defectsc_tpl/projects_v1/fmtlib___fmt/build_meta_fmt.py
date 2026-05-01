#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project fmtlib___fmt.

For each bug:
  1. Use commit_after as the fixed tree.
  2. Use commit_after plus src_files checked out from commit_before as buggy tree.
  3. Phase A: build without sanitizer by default, expand CTest binaries to
     GoogleTest test cases, then run each case on buggy + fixed to collect
     outcomes. ASAN/UBSAN is available with --phase-a-asan.
  4. Phase B: build buggy without ASAN but with gcov flags, run each selected
     test case one by one, and collect per-test coverage.
  5. Write full gcov coverage to raw/ and production-only coverage to metadata/.
"""

from __future__ import annotations

import argparse
import fcntl
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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"
REMOTE_URL = "https://github.com/fmtlib/fmt.git"
BUILD_DIR_NAME = "build_meta_fmt"
GCOV_SIGNAL_HEADER = "__build_meta_gcov_signal_dump.h"

COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage -Wno-error"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage -lgcov"
ASAN_CFLAGS = "-g -O0 -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer -Wno-error"
ASAN_LDFLAGS = "-fsanitize=address,undefined"
ASAN_GTEST_FILTERS = {
    # This fixed-tree subtest intentionally requests an enormous allocation.
    # GCC ASAN aborts before fmt can turn it into the expected bad_alloc path,
    # so Phase A would label fixed format-test as FAIL for an infrastructure
    # reason. Non-ASAN Phase B still runs the complete test binary.
    "format-test": "-util_test.format_system_error",
}

_GCOV_FUNC_RE_NEW = re.compile(
    r"^Function\s+'(?P<name>[^']+)'\s*\n"
    r"Lines executed:(?P<pct>[\d.]+)%\s+of\s+(?P<lines>\d+)",
    re.MULTILINE,
)
_GCOV_FUNC_RE_OLD = re.compile(
    r"^function\s+(?P<name>.+?)\s+called\s+(?P<calls>\d+)\s+returned",
    re.MULTILINE,
)


@dataclass
class BugEntry:
    sha_after: str
    sha_before: str
    src_files: List[str]
    test_files: List[str]
    type_name: Optional[str]
    type_id: str
    raw: dict
    test_flags: List[str] = field(default_factory=list)
    output_bug_id: str = ""

    @property
    def bug_id(self) -> str:
        return self.type_id or f"{PROJECT_NAME}@{self.sha_after}"

    @property
    def safe_bug_id(self) -> str:
        base = self.output_bug_id or self.bug_id
        return base.replace("@", "__").replace("/", "__")


@dataclass
class TestResult:
    test_id: str
    outcome: str
    outcome_fixed: str = ""
    fail_reason: str = ""
    actual_output: str = ""
    covered_functions: List[str] = field(default_factory=list)
    coverage_error: str = ""


@dataclass
class CTestEntry:
    name: str
    command: List[str] = field(default_factory=list)
    working_directory: str = ""


@dataclass
class TestSpec:
    test_id: str
    ctest_name: str
    command: List[str] = field(default_factory=list)
    working_directory: str = ""
    gtest_filter: str = ""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _detect_default_out_root() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / PROJECT_NAME
    if _safe_exists(host_default):
        return host_default
    container_default = Path("/out") / PROJECT_NAME
    if _safe_exists(container_default.parent):
        return container_default
    return host_default


def _detect_default_metadata_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "fmt" / "metadata"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/fmt/metadata")
    return host_default


def _detect_default_raw_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "fmt" / "raw"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/fmt/raw")
    return host_default


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
        raise RuntimeError(f"Another build_meta_fmt process is running (lock: {lock_path}){owner_msg}.")
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp


def _lock_path_for_run(out_root: Path) -> Path:
    key = hashlib.md5(str(out_root.resolve()).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_fmt.{key}.lock"


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
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"TimeoutExpired: {exc}"
    except UnicodeDecodeError as exc:
        return 1, "", f"UnicodeDecodeError: {exc}"

    rc = proc.returncode
    out = (proc.stdout or "") if capture else ""
    err = (proc.stderr or "") if capture else ""
    if check and rc != 0:
        raise RuntimeError(
            f"Command failed ({rc}): {' '.join(shlex.quote(a) for a in args)}\n"
            f"stdout: {out[-2000:]}\nstderr: {err[-2000:]}"
        )
    return rc, out, err


def load_bugs() -> List[BugEntry]:
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for raw in data:
        sha_after = raw.get("commit_after") or ""
        if not sha_after:
            continue
        files = raw.get("files") or {}
        bug_type = raw.get("type") or {}
        c_compile = raw.get("c_compile") or {}
        bugs.append(BugEntry(
            sha_after=sha_after,
            sha_before=raw.get("commit_before") or "",
            src_files=list(files.get("src") or []),
            test_files=list(files.get("test") or []),
            type_name=bug_type.get("name"),
            type_id=bug_type.get("id") or sha_after,
            raw=raw,
            test_flags=list(c_compile.get("test_flags") or []),
        ))
    counts = Counter(b.bug_id for b in bugs)
    for bug in bugs:
        bug.output_bug_id = f"{bug.bug_id}__{bug.sha_after[:12]}" if counts[bug.bug_id] > 1 else bug.bug_id
    return bugs


def ensure_repo(out_root: Path, sha_after: str, *, clone_if_missing: bool = False) -> Path:
    repo = out_root / f"git_repo_dir_{sha_after}"
    if repo.exists() and (repo / ".git").exists():
        return repo
    if not clone_if_missing:
        raise FileNotFoundError(f"Repo not found: {repo}. Run the clone step or pass --clone.")
    out_root.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {REMOTE_URL} -> {repo}")
    run(["git", "clone", REMOTE_URL, str(repo)], check=True, timeout=1800)
    return repo


def _git_checkout(repo: Path, sha: str, label: str) -> None:
    log(f"  [git] checkout {label}={sha[:10]}")
    run(["git", "reset", "--hard"], cwd=repo, check=True)
    run(["git", "clean", "-ffdx"], cwd=repo, check=True)
    rc, _, _ = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
    if rc != 0:
        run(["git", "fetch", "--all", "--tags"], cwd=repo, check=True, timeout=1200)
    run(["git", "checkout", "--force", sha], cwd=repo, check=True)


def checkout_buggy(repo: Path, bug: BugEntry) -> None:
    _git_checkout(repo, bug.sha_after, "fixed-base")
    if bug.sha_before and bug.src_files:
        log(f"  [git] overlay buggy src from {bug.sha_before[:10]}")
        run(["git", "checkout", "--force", bug.sha_before, "--", *bug.src_files], cwd=repo, check=True)


def checkout_fixed(repo: Path, bug: BugEntry) -> None:
    _git_checkout(repo, bug.sha_after, "fixed")


def write_gcov_signal_header(repo: Path) -> Path:
    header = repo / GCOV_SIGNAL_HEADER
    header.write_text(
        textwrap.dedent(
            r"""
            #pragma once
            #include <signal.h>
            #include <stdlib.h>

            #ifdef __cplusplus
            extern "C" void __gcov_dump(void);
            #else
            void __gcov_dump(void);
            #endif

            static void build_meta_gcov_dump_and_exit(int sig) {
              __gcov_dump();
              _Exit(128 + sig);
            }

            __attribute__((constructor))
            static void build_meta_gcov_install_signal_handlers(void) {
              signal(SIGABRT, build_meta_gcov_dump_and_exit);
              signal(SIGSEGV, build_meta_gcov_dump_and_exit);
              signal(SIGILL, build_meta_gcov_dump_and_exit);
              signal(SIGFPE, build_meta_gcov_dump_and_exit);
              signal(SIGTERM, build_meta_gcov_dump_and_exit);
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return header


def compile_project(repo: Path, *, jobs: int, asan: bool, coverage: bool, timeout: int = 1800) -> bool:
    env = os.environ.copy()
    env["CC"] = env.get("CC", "gcc")
    env["CXX"] = env.get("CXX", "g++")
    cflags = "-g -O0 -Wno-error"
    ldflags = ""
    if coverage:
        signal_header = write_gcov_signal_header(repo)
        cflags = COV_CFLAGS
        cflags += f" -include {shlex.quote(str(signal_header))}"
        ldflags = COV_LDFLAGS
    if asan:
        cflags = ASAN_CFLAGS
        ldflags = ASAN_LDFLAGS
        env["ASAN_OPTIONS"] = env.get("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=1")
        env["UBSAN_OPTIONS"] = env.get("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=1")

    build_dir = repo / BUILD_DIR_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    cmake_args = [
        "cmake",
        "-G", "Ninja",
        "-S", str(repo),
        "-B", str(build_dir),
        "-DCMAKE_BUILD_TYPE=Debug",
        "-DCMAKE_C_FLAGS=" + cflags,
        "-DCMAKE_CXX_FLAGS=" + cflags,
        "-DCMAKE_EXE_LINKER_FLAGS=" + ldflags,
        "-DCMAKE_SHARED_LINKER_FLAGS=" + ldflags,
        "-DFMT_TEST=ON",
        "-DFMT_DOC=OFF",
        "-DFMT_INSTALL=OFF",
    ]
    phase = "ASAN" if asan else ("GCOV" if coverage else "plain")
    log(f"  [build-{phase}] cmake configure")
    rc, out, err = run(cmake_args, cwd=repo, env=env, timeout=300)
    if rc != 0:
        log(f"  [build-{phase}] cmake FAILED rc={rc}\n{(out + err)[-3000:]}")
        return False

    log(f"  [build-{phase}] ninja -j{jobs}")
    rc, out, err = run(["ninja", "-C", str(build_dir), f"-j{jobs}"], cwd=repo, env=env, timeout=timeout)
    if rc != 0:
        log(f"  [build-{phase}] ninja FAILED rc={rc}\n{(out + err)[-3000:]}")
        return False

    if not build_ctest_targets(build_dir, jobs=jobs, timeout=timeout):
        return False

    if coverage:
        gcno = list(build_dir.rglob("*.gcno"))
        if gcno:
            log(f"  [build-GCOV] coverage OK ({len(gcno)} *.gcno)")
        else:
            log("  [build-GCOV] WARNING: no *.gcno found")
    return True


def build_ctest_targets(build_dir: Path, *, jobs: int, timeout: int = 1800) -> bool:
    """Build CTest executables explicitly.

    Some fmt revisions register tests in CTest but do not build all test
    executables as part of Ninja's default target. Without this step CTest
    reports every test as "Not Run" because build_meta_fmt/bin/<test> is
    missing.
    """
    test_names = list_ctest_tests(build_dir)
    if not test_names:
        return True

    log(f"  [build] ninja test targets ({len(test_names)})")
    rc, out, err = run(
        ["ninja", "-C", str(build_dir), f"-j{jobs}", *test_names],
        cwd=build_dir.parent,
        timeout=timeout,
    )
    if rc == 0:
        return True

    # Older/newer CMake generators can expose the binary path as the target.
    bin_targets = [f"bin/{name}" for name in test_names]
    rc2, out2, err2 = run(
        ["ninja", "-C", str(build_dir), f"-j{jobs}", *bin_targets],
        cwd=build_dir.parent,
        timeout=timeout,
    )
    if rc2 == 0:
        return True

    log(
        "  [build] ninja test targets FAILED\n"
        f"targets stderr[-1500]={err[-1500:]}\n"
        f"bin targets stderr[-1500]={err2[-1500:]}"
    )
    return False


def list_ctest_tests(build_dir: Path) -> List[str]:
    entries = list_ctest_entries(build_dir)
    if entries:
        return [entry.name for entry in entries]

    rc, out, err = run(["ctest", "--test-dir", str(build_dir), "--show-only=human"], cwd=build_dir, timeout=60)
    if rc != 0:
        log(f"  [tests] ctest discovery failed rc={rc}\n{(out + err)[-2000:]}")
        return []
    names: List[str] = []
    for line in out.splitlines():
        m = re.match(r"\s*Test\s+#?\d+:\s+(\S+)", line)
        if m:
            names.append(m.group(1))
    return names


def list_ctest_entries(build_dir: Path) -> List[CTestEntry]:
    rc, out, err = run(["ctest", "--test-dir", str(build_dir), "--show-only=json-v1"], cwd=build_dir, timeout=60)
    if rc == 0:
        try:
            payload = json.loads(out)
            entries: List[CTestEntry] = []
            for test in payload.get("tests", []):
                name = test.get("name") or ""
                if not name:
                    continue
                working_directory = ""
                for prop in test.get("properties", []):
                    if prop.get("name") == "WORKING_DIRECTORY":
                        working_directory = str(prop.get("value") or "")
                        break
                entries.append(CTestEntry(
                    name=name,
                    command=[str(arg) for arg in test.get("command", [])],
                    working_directory=working_directory,
                ))
            if entries:
                return entries
        except json.JSONDecodeError as exc:
            log(f"  [tests] ctest json parse failed: {exc}")
    return []


def select_tests(all_tests: List[str], bug: BugEntry) -> List[str]:
    if not bug.test_flags:
        return all_tests
    selected: List[str] = []
    for item in bug.test_flags:
        patterns = [p.strip() for p in str(item).split("|") if p.strip()]
        for pat in patterns:
            for test_name in all_tests:
                if test_name == pat or re.match(pat.replace("*", ".*"), test_name):
                    if test_name not in selected:
                        selected.append(test_name)
    return selected if selected else all_tests


def discover_test_specs(build_dir: Path, ctest_names: List[str]) -> List[TestSpec]:
    entries_by_name = {entry.name: entry for entry in list_ctest_entries(build_dir)}
    specs: List[TestSpec] = []
    for ctest_name in ctest_names:
        entry = entries_by_name.get(ctest_name) or CTestEntry(name=ctest_name)
        case_names = list_gtest_cases(entry, timeout=60)
        if not case_names:
            specs.append(TestSpec(
                test_id=ctest_name,
                ctest_name=ctest_name,
                command=entry.command,
                working_directory=entry.working_directory,
            ))
            continue
        for case_name in case_names:
            specs.append(TestSpec(
                test_id=f"{ctest_name}::{case_name}",
                ctest_name=ctest_name,
                command=entry.command,
                working_directory=entry.working_directory,
                gtest_filter=case_name,
            ))
    return specs


def list_gtest_cases(entry: CTestEntry, timeout: int = 60) -> List[str]:
    if not entry.command:
        return []
    env = os.environ.copy()
    env.setdefault("GTEST_COLOR", "no")
    cwd = entry.working_directory or None
    rc, out, err = run([*entry.command, "--gtest_list_tests"], cwd=cwd, env=env, timeout=timeout)
    if rc != 0:
        return []
    cases = _parse_gtest_list(out)
    if not cases and "This program contains tests written using Google Test" in (out + err):
        return []
    return cases


def _parse_gtest_list(text: str) -> List[str]:
    cases: List[str] = []
    suite = ""
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith("."):
            suite = line.strip()[:-1]
            continue
        if not suite or not line.startswith(" "):
            continue
        case_name = line.strip().split("#", 1)[0].strip()
        if not case_name:
            continue
        if suite.startswith("DISABLED_") or case_name.startswith("DISABLED_"):
            continue
        cases.append(f"{suite}.{case_name}")
    return cases


def select_test_specs(all_specs: List[TestSpec], bug: BugEntry) -> List[TestSpec]:
    if not bug.test_flags:
        return all_specs
    selected: List[TestSpec] = []
    seen: set[str] = set()
    for item in bug.test_flags:
        patterns = [p.strip() for p in str(item).split("|") if p.strip()]
        for pat in patterns:
            regex = pat.replace("*", ".*")
            for spec in all_specs:
                candidates = [spec.ctest_name, spec.test_id]
                if spec.gtest_filter:
                    candidates.append(spec.gtest_filter)
                matched = any(candidate == pat or re.match(regex, candidate) for candidate in candidates)
                if matched and spec.test_id not in seen:
                    selected.append(spec)
                    seen.add(spec.test_id)
    return selected if selected else all_specs


def clear_gcda(path: Path) -> None:
    for gcda in path.rglob("*.gcda"):
        try:
            gcda.unlink()
        except OSError:
            pass


def run_one_ctest(build_dir: Path, test_name: str, timeout: int = 120, *, asan: bool = False) -> Tuple[bool, str]:
    env = os.environ.copy()
    if asan:
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=1")
        env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=1")
        if test_name in ASAN_GTEST_FILTERS:
            env["GTEST_FILTER"] = ASAN_GTEST_FILTERS[test_name]
    rc, out, err = run(
        ["ctest", "--test-dir", str(build_dir), "-R", f"^{re.escape(test_name)}$", "-V", "--timeout", str(timeout)],
        cwd=build_dir,
        env=env,
        timeout=timeout + 30,
    )
    combined = (out or "") + ("\n" + err if err else "")
    passed = rc == 0
    if re.search(r"No tests were found|0 tests passed,\s+0 tests failed out of 0", combined):
        passed = False
    if "***Failed" in combined or "***Timeout" in combined:
        passed = False
    return passed, combined


def run_one_test_spec(build_dir: Path, spec: TestSpec, timeout: int = 120, *, asan: bool = False) -> Tuple[bool, str]:
    if not spec.gtest_filter:
        return run_one_ctest(build_dir, spec.ctest_name, timeout=timeout, asan=asan)
    if not spec.command:
        return False, f"missing gtest command for {spec.test_id}"

    env = os.environ.copy()
    env.setdefault("GTEST_COLOR", "no")
    if asan:
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=1")
        env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=1")
    rc, out, err = run(
        [*spec.command, f"--gtest_filter={spec.gtest_filter}", "--gtest_color=no"],
        cwd=spec.working_directory or build_dir,
        env=env,
        timeout=timeout + 30,
    )
    combined = (out or "") + ("\n" + err if err else "")
    passed = rc == 0
    if re.search(r"Running\s+0\s+tests|0 tests? from 0 test", combined):
        passed = False
    if "[  FAILED  ]" in combined or "***Failed" in combined or "***Timeout" in combined:
        passed = False
    return passed, combined


def _parse_gcov_functions(text: str) -> List[str]:
    funcs: List[str] = []
    for match in _GCOV_FUNC_RE_NEW.finditer(text):
        try:
            if float(match.group("pct")) > 0.0:
                funcs.append(_normalize_cpp_function(match.group("name")))
        except ValueError:
            pass
    if funcs:
        return funcs
    for match in _GCOV_FUNC_RE_OLD.finditer(text):
        try:
            if int(match.group("calls")) > 0:
                funcs.append(_normalize_cpp_function(match.group("name")))
        except ValueError:
            pass
    return funcs


def _source_from_gcov_text(text: str) -> str:
    for line in text.splitlines()[:20]:
        marker = "Source:"
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return ""


def _parse_gcov_line_functions(gcov_text: str, source_text: str, line_function_map: Optional[Dict[int, str]] = None) -> List[str]:
    funcs: List[str] = []
    if not source_text:
        return funcs
    if line_function_map is None:
        line_function_map = _build_cpp_function_line_map(source_text)
    for line in gcov_text.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        count_text = parts[0].strip()
        line_no_text = parts[1].strip()
        if count_text in {"-", "#####", "====="} or not count_text:
            continue
        try:
            line_no = int(line_no_text)
        except ValueError:
            continue
        if line_no <= 0:
            continue
        fn = line_function_map.get(line_no, "")
        if fn:
            funcs.append(fn)
    return funcs


def collect_coverage(repo: Path, build_dir: Path, *, verbose: bool = False) -> Tuple[Dict[str, List[str]], str]:
    if not shutil.which("gcov"):
        return {}, "gcov_not_found"
    gcda_files = list(build_dir.rglob("*.gcda"))
    if not gcda_files:
        if verbose:
            log("  [cov] no *.gcda found")
        return {}, "no_gcda_files"

    for old in build_dir.rglob("*.gcov"):
        try:
            old.unlink()
        except OSError:
            pass

    covered: Dict[str, List[str]] = {}
    source_text_cache: Dict[Path, str] = {}
    line_function_cache: Dict[Path, Dict[int, str]] = {}
    for gcda in gcda_files:
        gcda_dir = gcda.parent
        rc, out, err = run(["gcov", "-m", "-f", "-b", "-c", gcda.name], cwd=gcda_dir, timeout=60)
        if rc != 0 and verbose:
            log(f"  [cov] gcov returned {rc} for {gcda.name}: {err[-300:]}")

        parsed_any = False
        for gcov_file in gcda_dir.glob("*.gcov"):
            try:
                text = gcov_file.read_text(errors="replace")
            except OSError:
                continue
            source = _source_from_gcov_text(text)
            if not source:
                continue
            src_path = Path(source)
            if not src_path.is_absolute():
                src_path = (gcda_dir / src_path).resolve()
            try:
                rel = src_path.relative_to(repo).as_posix()
            except ValueError:
                continue
            if rel.startswith(BUILD_DIR_NAME + "/"):
                continue
            funcs = _parse_gcov_functions(text)
            if src_path not in source_text_cache:
                try:
                    source_text_cache[src_path] = src_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    source_text_cache[src_path] = ""
            source_text = source_text_cache[src_path]
            if src_path not in line_function_cache:
                line_function_cache[src_path] = _build_cpp_function_line_map(source_text)
            funcs.extend(_parse_gcov_line_functions(text, source_text, line_function_cache[src_path]))
            funcs = [fn for fn in funcs if fn]
            if not funcs:
                continue
            covered.setdefault(rel, [])
            covered[rel].extend(funcs)
            parsed_any = True

        if not parsed_any and verbose and out:
            log(f"  [cov] no parseable .gcov files for {gcda.name}")

    for old in build_dir.rglob("*.gcov"):
        try:
            old.unlink()
        except OSError:
            pass

    for rel in list(covered.keys()):
        covered[rel] = sorted(set(covered[rel]))
    if verbose:
        log(f"  [cov] found {sum(len(v) for v in covered.values())} functions in {len(covered)} files")
    if not covered:
        return {}, f"no_functions_parsed_from_{len(gcda_files)}_gcda_files"
    return covered, ""


def _is_production_source(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/").lstrip("./")
    if not rel.endswith((".h", ".hpp", ".hh", ".cc", ".cpp", ".cxx")):
        return False
    return rel.startswith("include/fmt/") or rel.startswith("src/")


def filter_production_coverage(cov_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    return {rel: funcs for rel, funcs in cov_map.items() if _is_production_source(rel)}


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for rel, funcs in cov_map.items():
        base = os.path.basename(rel)
        for func in funcs:
            out.append(f"{base}:{func}")
    return sorted(set(out))


def _normalize_cpp_function(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name)).strip()
    if not name:
        return ""
    name = _strip_cpp_parameter_list(name)
    name = re.sub(r"^(virtual|static|constexpr|const|inline|typename)\s+", "", name)
    if name.startswith("fmt::"):
        name = name[len("fmt::"):]
    name = name.replace("fmt::v5::", "").replace("fmt::v6::", "").replace("fmt::v7::", "")
    name = name.replace("fmt::v8::", "").replace("fmt::v9::", "").replace("fmt::v10::", "")
    name = name.replace("v5::", "").replace("v6::", "").replace("v7::", "")
    name = name.replace("v8::", "").replace("v9::", "").replace("v10::", "")
    name = _drop_cpp_return_type(name)
    name = _strip_cpp_template_args(name)
    for internal_prefix in ("detail::", "internal::"):
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


def extract_ground_truth(bug: BugEntry, repo: Path) -> List[str]:
    if not bug.sha_before or not bug.src_files:
        return []
    funcs = set()
    source_cache: Dict[str, str] = {}
    rc, diff, _ = run(["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files], cwd=repo)
    if rc == 0 and diff:
        changed_lines = _changed_new_lines_from_diff(diff)
        for src_file, line_numbers in changed_lines.items():
            if src_file not in bug.src_files:
                continue
            if src_file not in source_cache:
                rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo)
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
                    rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo)
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
                rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo)
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
    before_args = before_args.replace("FMT_CONSTEXPR", " ").replace("FMT_INLINE", " ")
    if "[" in before_args or "]" in before_args or before_args.endswith("="):
        return ""
    name = _normalize_cpp_function(before_args)
    if not name:
        return ""
    leaf = name.rsplit("::", 1)[-1]
    if leaf in {"if", "for", "while", "switch", "return", "sizeof"}:
        return ""
    return name


RUN_ONE_TEST_SH = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # Auto-generated by build_meta_fmt.py
    set -uo pipefail
    HERE=$(cd "$(dirname "$0")" && pwd)
    TEST_ID="${1:?Usage: $0 <test_id>}"
    BUILD_DIR="$HERE/__BUILD_DIR_NAME__"
    if [[ "$TEST_ID" == *"::"* ]]; then
      CTEST_NAME="${TEST_ID%%::*}"
      GTEST_FILTER="${TEST_ID#*::}"
      OUTPUT=$("$BUILD_DIR/bin/$CTEST_NAME" --gtest_filter="$GTEST_FILTER" --gtest_color=no 2>&1)
    else
      OUTPUT=$(ctest --test-dir "$BUILD_DIR" -R "^${TEST_ID}$" -V --timeout 120 2>&1)
    fi
    STATUS=$?
    echo "$OUTPUT"
    if [[ $STATUS -ne 0 ]] || echo "$OUTPUT" | grep -Eq '\*\*\*Failed|\[  FAILED  \]|Running 0 tests'; then
      exit 1
    fi
    exit 0
""").lstrip()


def write_run_one_test(repo: Path) -> Path:
    path = repo / "run_one_test.sh"
    path.write_text(RUN_ONE_TEST_SH.replace("__BUILD_DIR_NAME__", BUILD_DIR_NAME), encoding="utf-8")
    path.chmod(0o755)
    return path


def _test_to_dict(result: TestResult) -> dict:
    item = {
        "test_id": result.test_id,
        "outcome": result.outcome,
        "outcome_fixed": result.outcome_fixed,
        "fail_reason": result.fail_reason,
        "actual_output": result.actual_output,
        "covered_functions": result.covered_functions,
    }
    if result.coverage_error:
        item["coverage_error"] = result.coverage_error
    return item


def _write_meta(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def _empty_record(bug: BugEntry, repo: Path, compile_cmd: str, *, error: str) -> dict:
    source_file = str(repo / bug.src_files[0]) if bug.src_files else ""
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
        "type_name": bug.type_name,
        "ground_truth_functions": [],
        "ground_truth": [],
        "phase_info": {"errors": [error]},
        "tests": [],
        "build_error": error,
    }


def process_bug(
    bug: BugEntry,
    out_root: Path,
    metadata_dir: Path,
    raw_dir: Path,
    *,
    jobs: int,
    test_timeout: int,
    skip_if_exists: bool,
    clone_if_missing: bool,
    skip_coverage: bool,
    dual_run: bool,
    run_all_tests: bool,
    phase_a_asan: bool,
) -> Optional[Path]:
    safe_name = f"{bug.safe_bug_id}_meta.json"
    out_path = metadata_dir / safe_name
    raw_out_path = raw_dir / safe_name
    if skip_if_exists and out_path.exists():
        log(f"[skip] {bug.bug_id}")
        return out_path

    log(f"=== {bug.bug_id} | {bug.type_name or ''} ===")
    try:
        repo = ensure_repo(out_root, bug.sha_after, clone_if_missing=clone_if_missing)
    except Exception as exc:
        log(f"  [error] repo: {exc}")
        return None

    source_file = str(repo / bug.src_files[0]) if bug.src_files else ""
    compile_cmd = (
        f"cd {shlex.quote(str(repo))} && cmake -G Ninja -S . -B {BUILD_DIR_NAME} "
        f"-DFMT_TEST=ON -DFMT_DOC=OFF -DCMAKE_CXX_FLAGS='{COV_CFLAGS}' && "
        f"ninja -C {BUILD_DIR_NAME} -j{jobs}"
    )
    test_cmd_template = f"bash {shlex.quote(str(repo / 'run_one_test.sh'))} {{test_id}}"
    phase_info = {
        "dual_run": dual_run,
        "run_all_tests": run_all_tests,
        "skip_coverage": skip_coverage,
        "phase_a_build": "asan" if phase_a_asan else "plain",
        "errors": [],
    }

    try:
        checkout_buggy(repo, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy: {exc}")
        return None

    # Discover tests from the fixed-base buggy tree. Phase A/Phase B reuse
    # these names so outcome and coverage rows stay aligned.
    if not compile_project(repo, jobs=jobs, asan=phase_a_asan, coverage=False):
        record = _empty_record(bug, repo, compile_cmd, error="buggy_phase_a_compile_failed")
        _write_meta(raw_out_path, record)
        _write_meta(out_path, record)
        return out_path
    write_run_one_test(repo)
    build_dir = repo / BUILD_DIR_NAME
    all_ctest_tests = list_ctest_tests(build_dir)
    all_specs = discover_test_specs(build_dir, all_ctest_tests)
    selected = all_specs if run_all_tests else select_test_specs(all_specs, bug)
    selected_ctest_tests = sorted({spec.ctest_name for spec in selected})
    phase_info["test_granularity"] = "gtest_case"
    phase_info["all_ctest_tests"] = len(all_ctest_tests)
    phase_info["selected_ctest_tests"] = len(selected_ctest_tests)
    phase_info["all_tests"] = len(all_specs)
    phase_info["selected_tests"] = len(selected)
    log(
        f"  [tests] ctest={len(all_ctest_tests)}, cases={len(all_specs)}, "
        f"selected_cases={len(selected)}"
    )
    if not selected:
        phase_info["errors"].append("no_tests_selected")

    results_a: List[TestResult] = []
    for idx, spec in enumerate(selected, 1):
        passed, output = run_one_test_spec(build_dir, spec, timeout=test_timeout, asan=phase_a_asan)
        results_a.append(TestResult(
            test_id=spec.test_id,
            outcome="PASS" if passed else "FAIL",
            fail_reason="" if passed else output[-3000:],
            actual_output="" if passed else output[-3000:],
        ))
        if idx % 25 == 0 or idx == len(selected):
            n_fail = sum(1 for r in results_a if r.outcome == "FAIL")
            log(f"  [phaseA-buggy] {idx}/{len(selected)} (fail={n_fail})")

    fixed_map: Dict[str, str] = {}
    if dual_run:
        try:
            checkout_fixed(repo, bug)
            if compile_project(repo, jobs=jobs, asan=phase_a_asan, coverage=False):
                fixed_build_dir = repo / BUILD_DIR_NAME
                fixed_entries = {entry.name: entry for entry in list_ctest_entries(fixed_build_dir)}
                fixed_selected = [
                    TestSpec(
                        test_id=spec.test_id,
                        ctest_name=spec.ctest_name,
                        command=fixed_entries.get(spec.ctest_name, CTestEntry(spec.ctest_name)).command,
                        working_directory=fixed_entries.get(spec.ctest_name, CTestEntry(spec.ctest_name)).working_directory,
                        gtest_filter=spec.gtest_filter,
                    )
                    for spec in selected
                ]
                for idx, spec in enumerate(fixed_selected, 1):
                    passed, _ = run_one_test_spec(fixed_build_dir, spec, timeout=test_timeout, asan=phase_a_asan)
                    fixed_map[spec.test_id] = "PASS" if passed else "FAIL"
                    if idx % 25 == 0 or idx == len(selected):
                        n_fail = sum(1 for v in fixed_map.values() if v == "FAIL")
                        log(f"  [phaseA-fixed] {idx}/{len(selected)} (fail={n_fail})")
            else:
                phase_info["errors"].append("fixed_phase_a_compile_failed")
        except Exception as exc:
            phase_info["errors"].append(f"fixed_phase_exception: {exc}")
            log(f"  [error] fixed phase failed: {exc}")

    raw_cov_map: Dict[str, List[str]] = {}
    meta_cov_map: Dict[str, List[str]] = {}
    cov_error_map: Dict[str, str] = {}
    if not skip_coverage:
        try:
            checkout_buggy(repo, bug)
            if compile_project(repo, jobs=jobs, asan=False, coverage=True):
                cov_build_dir = repo / BUILD_DIR_NAME
                cov_entries = {entry.name: entry for entry in list_ctest_entries(cov_build_dir)}
                cov_selected = [
                    TestSpec(
                        test_id=spec.test_id,
                        ctest_name=spec.ctest_name,
                        command=cov_entries.get(spec.ctest_name, CTestEntry(spec.ctest_name)).command,
                        working_directory=cov_entries.get(spec.ctest_name, CTestEntry(spec.ctest_name)).working_directory,
                        gtest_filter=spec.gtest_filter,
                    )
                    for spec in selected
                ]
                for idx, spec in enumerate(cov_selected, 1):
                    clear_gcda(cov_build_dir)
                    run_one_test_spec(cov_build_dir, spec, timeout=test_timeout, asan=False)
                    cov, cov_error = collect_coverage(repo, cov_build_dir, verbose=(idx <= 2))
                    raw_cov_map[spec.test_id] = coverage_to_qualified(cov)
                    meta_cov_map[spec.test_id] = coverage_to_qualified(filter_production_coverage(cov))
                    cov_error_map[spec.test_id] = cov_error
                    if idx % 25 == 0 or idx == len(selected):
                        n_raw = sum(1 for v in raw_cov_map.values() if v)
                        n_meta = sum(1 for v in meta_cov_map.values() if v)
                        log(f"  [phaseB] {idx}/{len(selected)} (raw_cov={n_raw}, meta_cov={n_meta})")
            else:
                phase_info["errors"].append("coverage_compile_failed")
                cov_error_map.update({spec.test_id: "coverage_compile_failed" for spec in selected})
        except Exception as exc:
            phase_info["errors"].append(f"coverage_phase_exception: {exc}")
            cov_error_map.update({spec.test_id: f"coverage_phase_exception: {exc}" for spec in selected})
            log(f"  [error] coverage phase failed: {exc}")

    raw_results: List[TestResult] = []
    metadata_results: List[TestResult] = []
    for result in results_a:
        common = {
            "test_id": result.test_id,
            "outcome": result.outcome,
            "outcome_fixed": fixed_map.get(result.test_id, "NOT_RUN" if dual_run else "NOT_RUN"),
            "fail_reason": result.fail_reason,
            "actual_output": result.actual_output,
            "coverage_error": cov_error_map.get(result.test_id, ""),
        }
        raw_results.append(TestResult(**common, covered_functions=raw_cov_map.get(result.test_id, [])))
        metadata_results.append(TestResult(**common, covered_functions=meta_cov_map.get(result.test_id, [])))

    write_run_one_test(repo)
    gt_funcs = extract_ground_truth(bug, repo)
    base_record = {
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
        "type_name": bug.type_name,
        "ground_truth_functions": gt_funcs,
        "ground_truth": [f"{source_file}::{fn}" for fn in gt_funcs] if source_file else [],
    }
    raw_record = {
        **base_record,
        "phase_info": {**phase_info, "coverage_scope": "full_gcov"},
        "tests": [_test_to_dict(r) for r in raw_results],
    }
    metadata_record = {
        **base_record,
        "phase_info": {
            **phase_info,
            "coverage_scope": "production_source_only",
            "coverage_filter": "keep include/fmt/* and src/* C++ files",
        },
        "tests": [_test_to_dict(r) for r in metadata_results],
    }
    _write_meta(raw_out_path, raw_record)
    log(f"  [ok] wrote raw {raw_out_path}")
    _write_meta(out_path, metadata_record)
    log(f"  [ok] wrote {out_path}")
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build Unified-Debugging metadata for fmtlib/fmt.")
    parser.add_argument("--sha", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out-root", type=Path, default=_detect_default_out_root())
    parser.add_argument("--metadata-dir", type=Path, default=_detect_default_metadata_dir())
    parser.add_argument("--raw-dir", type=Path, default=_detect_default_raw_dir())
    parser.add_argument("--jobs", type=int, default=max((os.cpu_count() or 2) - 1, 1))
    parser.add_argument("--test-timeout", type=int, default=180)
    parser.add_argument("--skip-coverage", action="store_true")
    parser.add_argument("--dual-run", dest="dual_run", action="store_true", default=True)
    parser.add_argument("--single-run", dest="dual_run", action="store_false")
    parser.add_argument("--run-all-tests", dest="run_all_tests", action="store_true", default=True)
    parser.add_argument("--trigger-tests-only", dest="run_all_tests", action="store_false")
    parser.add_argument(
        "--phase-a-asan",
        dest="phase_a_asan",
        action="store_true",
        default=False,
        help="Use ASAN/UBSAN in Phase A outcomes. Default is plain non-sanitized build.",
    )
    parser.add_argument(
        "--phase-a-plain",
        dest="phase_a_asan",
        action="store_false",
        help="Use plain non-sanitized Phase A outcomes (default).",
    )
    parser.add_argument("--skip-if-exists", action="store_true")
    parser.add_argument("--clone", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    bugs = load_bugs()
    if args.sha:
        wanted = [s for s in args.sha if s]
        bugs = [b for b in bugs if b.sha_after in wanted or b.sha_after.startswith(tuple(wanted))]
    if args.limit:
        bugs = bugs[:args.limit]
    if args.list:
        for bug in bugs:
            print(f"{bug.bug_id}\t{bug.type_name or '-'}\t{','.join(bug.src_files)}")
        return 0
    if not bugs:
        log("No matching bugs.")
        return 1

    args.metadata_dir.mkdir(parents=True, exist_ok=True)
    args.raw_dir.mkdir(parents=True, exist_ok=True)

    log(f"Will process {len(bugs)} bug(s).")
    lock_fp = None
    try:
        lock_fp = _acquire_single_run_lock(_lock_path_for_run(args.out_root))
    except RuntimeError as exc:
        log(f"[error] {exc}")
        return 3

    fail_count = 0
    try:
        for bug in bugs:
            try:
                out = process_bug(
                    bug,
                    args.out_root,
                    args.metadata_dir,
                    args.raw_dir,
                    jobs=args.jobs,
                    test_timeout=args.test_timeout,
                    skip_if_exists=args.skip_if_exists,
                    clone_if_missing=args.clone,
                    skip_coverage=args.skip_coverage,
                    dual_run=args.dual_run,
                    run_all_tests=args.run_all_tests,
                    phase_a_asan=args.phase_a_asan,
                )
                if out is None:
                    fail_count += 1
            except KeyboardInterrupt:
                log("Interrupted.")
                return 130
            except Exception as exc:
                fail_count += 1
                log(f"  [exception] {bug.bug_id}: {exc}")
    finally:
        if lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fp.close()

    log(f"DONE. fail={fail_count}/{len(bugs)}")
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
