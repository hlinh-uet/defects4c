#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project danmar___cppcheck.

Pipeline:
  1. Fixed version: checkout commit_after.
  2. Buggy version: checkout commit_after, then overlay files.src from commit_before.
  3. Phase A: plain non-sanitized build by default, run CTest on buggy and
     fixed for outcomes. ASAN/UBSAN is available with --phase-a-asan.
  4. Phase B: non-ASAN gcov build, run CTest one by one for per-test coverage.
  5. raw/ keeps full gcov coverage; metadata/ keeps production coverage only.
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
REMOTE_URL = "https://github.com/danmar/cppcheck.git"
BUILD_DIR_NAME = "build_meta_cppcheck"
COVERAGE_PARSER_VERSION = 2

COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage -Wno-error"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage -lgcov"
ASAN_CFLAGS = "-g -O0 -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer -Wno-error"
ASAN_LDFLAGS = "-fsanitize=address,undefined"

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
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "cppcheck" / "metadata"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/cppcheck/metadata")
    return host_default


def _detect_default_raw_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "cppcheck" / "raw"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/cppcheck/raw")
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
        raise RuntimeError(f"Another build_meta_cppcheck process is running (lock: {lock_path}){owner_msg}.")
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp


def _lock_path_for_run(out_root: Path) -> Path:
    key = hashlib.md5(str(out_root.resolve()).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_cppcheck.{key}.lock"


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


def apply_build_compat_patches(repo: Path) -> List[str]:
    """Apply local build-only compatibility fixes for old cppcheck revisions."""
    applied: List[str] = []

    # Older cppcheck revisions use SIGSTKSZ in a static array bound. With newer
    # glibc/GCC this is not an integral constant expression under -std=c++0x.
    # Keep the functional code unchanged and normalize the build-time constant
    # in the temporary checkout for both buggy and fixed phases.
    target = repo / "cli" / "cppcheckexecutor.cpp"
    if target.exists():
        old = "static const size_t MYSTACKSIZE = 16*1024+SIGSTKSZ;"
        new = "static const size_t MYSTACKSIZE = 16*1024+8192;"
        text = target.read_text(errors="replace")
        if old in text:
            target.write_text(text.replace(old, new), encoding="utf-8")
            applied.append("cppcheckexecutor_mystacksize_sigstksz")

    target = repo / "lib" / "valueflow.cpp"
    if target.exists():
        text = target.read_text(errors="replace")
        if "std::numeric_limits<" in text and "#include <limits>" not in text:
            marker = "#include <stack>\n"
            if marker in text:
                target.write_text(text.replace(marker, marker + "#include <limits>\n", 1), encoding="utf-8")
                applied.append("valueflow_include_limits")

    return applied


def compile_project(repo: Path, *, jobs: int, asan: bool, coverage: bool, timeout: int = 2400) -> bool:
    compat_patches = apply_build_compat_patches(repo)
    if compat_patches:
        log(f"  [build-compat] applied {', '.join(compat_patches)}")

    env = os.environ.copy()
    env["CC"] = env.get("CC", "gcc")
    env["CXX"] = env.get("CXX", "g++")
    cflags = "-g -O0 -Wno-error"
    ldflags = ""
    if coverage:
        cflags = COV_CFLAGS
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
        "-DBUILD_TESTS=ON",
        "-DBUILD_GUI=OFF",
        "-DHAVE_RULES=OFF",
        "-DUSE_MATCHCOMPILER=OFF",
    ]
    phase = "ASAN" if asan else ("GCOV" if coverage else "plain")
    log(f"  [build-{phase}] cmake configure")
    rc, out, err = run(cmake_args, cwd=repo, env=env, timeout=600)
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


def list_ctest_details(build_dir: Path) -> Tuple[List[str], Dict[str, List[str]]]:
    rc, out, err = run(["ctest", "--test-dir", str(build_dir), "--show-only=json-v1"], cwd=build_dir, timeout=60)
    if rc == 0:
        try:
            payload = json.loads(out)
            names: List[str] = []
            commands: Dict[str, List[str]] = {}
            for test in payload.get("tests", []):
                name = test.get("name")
                if not name:
                    continue
                names.append(name)
                cmd = test.get("command") or []
                if isinstance(cmd, list):
                    commands[name] = [str(x) for x in cmd]
            if names:
                return names, commands
        except json.JSONDecodeError as exc:
            log(f"  [tests] ctest json parse failed: {exc}")

    rc, out, err = run(["ctest", "--test-dir", str(build_dir), "--show-only=human"], cwd=build_dir, timeout=60)
    if rc != 0:
        log(f"  [tests] ctest discovery failed rc={rc}\n{(out + err)[-2000:]}")
        return [], {}
    names: List[str] = []
    for line in out.splitlines():
        m = re.match(r"\s*Test\s+#?\d+:\s+(\S+)", line)
        if m:
            names.append(m.group(1))
    return names, {}


def list_ctest_tests(build_dir: Path) -> List[str]:
    names, _ = list_ctest_details(build_dir)
    return names


def discover_cppcheck_subtests(repo: Path) -> List[str]:
    tests_dir = repo / "test"
    if not tests_dir.exists():
        return []

    discovered: List[str] = []
    seen = set()
    class_re = re.compile(r"\bclass\s+([A-Za-z_]\w*)\s*:\s*(?:public|private|protected)\s+TestFixture")
    register_re = re.compile(r"\bREGISTER_TEST\s*\(\s*([A-Za-z_]\w*)\s*\)")
    case_re = re.compile(r"\bTEST_CASE\s*\(\s*([A-Za-z_]\w*)\s*\)")

    for path in sorted(tests_dir.glob("test*.cpp")):
        if path.name in {"testrunner.cpp", "testsuite.cpp"}:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        registered = set(register_re.findall(text))
        if not registered:
            continue
        classes = list(class_re.finditer(text))
        for idx, match in enumerate(classes):
            class_name = match.group(1)
            if class_name not in registered:
                continue
            end = classes[idx + 1].start() if idx + 1 < len(classes) else len(text)
            segment = text[match.start():end]
            for case_name in case_re.findall(segment):
                test_id = f"{class_name}::{case_name}"
                if test_id not in seen:
                    seen.add(test_id)
                    discovered.append(test_id)
    return discovered


def discover_cppcheck_test_classes(repo: Path) -> List[str]:
    tests_dir = repo / "test"
    if not tests_dir.exists():
        return []
    register_re = re.compile(r"\bREGISTER_TEST\s*\(\s*([A-Za-z_]\w*)\s*\)")
    classes: List[str] = []
    seen = set()
    for path in sorted(tests_dir.glob("test*.cpp")):
        if path.name in {"testrunner.cpp", "testsuite.cpp"}:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for class_name in register_re.findall(text):
            if class_name not in seen:
                seen.add(class_name)
                classes.append(class_name)
    return classes


def list_project_tests(repo: Path, build_dir: Path, granularity: str) -> List[str]:
    if granularity == "subtest":
        subtests = discover_cppcheck_subtests(repo)
        if subtests:
            return subtests
    if granularity == "class":
        classes = discover_cppcheck_test_classes(repo)
        if classes:
            return classes
    return list_ctest_tests(build_dir)


def _pascal_from_token(value: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", value) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _src_trigger_class_candidates(bug: BugEntry) -> List[str]:
    special = {
        "analyzerinfo": ["TestAnalyzerInformation"],
        "astutils": ["TestAstUtils"],
        "checkbufferoverrun": ["TestBufferOverrun"],
        "checkclass": ["TestClass"],
        "checkcondition": ["TestCondition"],
        "checkleakautovar": ["TestLeakAutoVar", "TestLeakAutoVarWindows"],
        "checkmemoryleak": ["TestMemleak"],
        "checkuninitvar": ["TestUninitVar"],
        "checkunusedvar": ["TestUnusedVar"],
        "preprocessor": ["TestPreprocessor"],
        "symboldatabase": ["TestSymbolDatabase"],
        "templatesimplifier": ["TestSimplifyTemplate"],
        "tokenize": ["TestTokenizer"],
        "valueflow": ["TestValueFlow"],
    }
    out: List[str] = []
    for src in bug.src_files:
        stem = Path(src).stem.lower()
        for candidate in special.get(stem, []):
            if candidate not in out:
                out.append(candidate)
        stripped = stem
        if stripped.startswith("check"):
            stripped = stripped[len("check"):]
        generated = "Test" + _pascal_from_token(stripped)
        if generated != "Test" and generated not in out:
            out.append(generated)
    return out


def build_ctest_targets(build_dir: Path, *, jobs: int, timeout: int = 2400) -> bool:
    names, commands = list_ctest_details(build_dir)
    if not names:
        return True

    rc, targets_out, _ = run(["ninja", "-C", str(build_dir), "-t", "targets", "all"], cwd=build_dir.parent, timeout=120)
    available = set()
    if rc == 0:
        for line in targets_out.splitlines():
            target = line.split(":", 1)[0].strip()
            if target:
                available.add(target)

    candidates = set(names)
    for cmd in commands.values():
        if cmd:
            exe = os.path.basename(cmd[0])
            if exe:
                candidates.add(exe)
                candidates.add(f"test/{exe}")
                candidates.add(f"bin/{exe}")
    candidates.update({"testrunner", "test/testrunner", "bin/testrunner"})

    to_build = sorted(c for c in candidates if not available or c in available)
    if not to_build:
        return True

    built_any = False
    log(f"  [build] ninja candidate test targets ({len(to_build)})")
    for target in to_build:
        rc, out, err = run(["ninja", "-C", str(build_dir), f"-j{jobs}", target], cwd=build_dir.parent, timeout=timeout)
        if rc == 0:
            built_any = True
        elif target in names:
            log(f"  [build] target {target} failed/nonexistent: {(out + err)[-300:]}")
    return True if built_any or to_build else True


def select_tests(all_tests: List[str], bug: BugEntry) -> List[str]:
    if not bug.test_flags:
        return all_tests
    selected: List[str] = []
    for item in bug.test_flags:
        patterns = [p.strip() for p in str(item).split("|") if p.strip()]
        for pat in patterns:
            if os.path.basename(pat) == "testrunner":
                candidates = _src_trigger_class_candidates(bug)
                inferred = []
                for test_name in all_tests:
                    class_name = test_name.split("::", 1)[0]
                    if any(class_name == c or class_name.startswith(c) for c in candidates):
                        inferred.append(test_name)
                return inferred if inferred else all_tests
            for test_name in all_tests:
                if (
                    test_name == pat
                    or test_name.startswith(f"{pat}::")
                    or re.match(pat.replace("*", ".*"), test_name)
                ):
                    if test_name not in selected:
                        selected.append(test_name)
    return selected if selected else all_tests


def order_tests_with_triggers_first(all_tests: List[str], bug: BugEntry, *, run_all_tests: bool) -> List[str]:
    trigger_tests = select_tests(all_tests, bug)
    if not run_all_tests:
        return trigger_tests

    trigger_set = set(trigger_tests)
    return trigger_tests + [test_name for test_name in all_tests if test_name not in trigger_set]


def clear_gcda(path: Path) -> None:
    for gcda in path.rglob("*.gcda"):
        try:
            gcda.unlink()
        except OSError:
            pass


def run_one_ctest(build_dir: Path, test_name: str, timeout: int = 180, *, asan: bool = False) -> Tuple[bool, str]:
    env = os.environ.copy()
    if asan:
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=1")
        env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=1")
    testrunner = build_dir / "bin" / "testrunner"
    if testrunner.exists() and ("::" in test_name or test_name.startswith("Test")):
        rc, out, err = run(
            [str(testrunner), test_name],
            cwd=testrunner.parent,
            env=env,
            timeout=timeout + 30,
        )
        combined = (out or "") + ("\n" + err if err else "")
        passed = rc == 0 and "Tests failed: 0" in combined
        if re.search(r"Number of tests:\s+0\b", combined):
            passed = False
        return passed, combined

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
    if "***Failed" in combined or "***Timeout" in combined or "Subprocess aborted" in combined:
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
    return [f for f in funcs if f]


def _source_from_gcov_text(text: str) -> str:
    for line in text.splitlines()[:20]:
        marker = "Source:"
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return ""


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
            funcs = _parse_gcov_functions(text)
            if not source or not funcs:
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
        covered[rel] = sorted(set(f for f in covered[rel] if f))
    if verbose:
        log(f"  [cov] found {sum(len(v) for v in covered.values())} functions in {len(covered)} files")
    if not covered:
        return {}, f"no_functions_parsed_from_{len(gcda_files)}_gcda_files"
    return covered, ""


def _is_production_source(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/").lstrip("./")
    if not rel.endswith((".h", ".hpp", ".hh", ".c", ".cc", ".cpp", ".cxx")):
        return False
    return rel.startswith(("lib/", "cli/", "externals/", "oss-fuzz/")) and not rel.startswith("test/")


def filter_production_coverage(cov_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    return {rel: funcs for rel, funcs in cov_map.items() if _is_production_source(rel)}


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for rel, funcs in cov_map.items():
        base = os.path.basename(rel)
        for func in funcs:
            if func:
                out.append(f"{base}:{func}")
    return sorted(set(out))


def _normalize_cpp_function(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name)).strip()
    if not name:
        return ""
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    prefixes = {"virtual", "static", "constexpr", "const", "inline", "typename", "class", "struct"}
    parts = name.split()
    while parts and parts[0] in prefixes:
        parts.pop(0)
    return " ".join(parts).strip()


def extract_ground_truth(bug: BugEntry, repo: Path) -> List[str]:
    if not bug.sha_before or not bug.src_files:
        return []
    funcs = set()
    source_cache: Dict[str, str] = {}
    rc, diff, _ = run(["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files], cwd=repo)
    if rc == 0 and diff:
        for line in diff.splitlines():
            if not line.startswith("@@"):
                continue
            tail = line.split("@@", 2)[-1].strip()
            header_func = _function_from_diff_tail(tail)
            if header_func:
                funcs.add(header_func)
                continue
            hunk = re.match(r"@@\s+-\d+(?:,\d+)?\s+\+(?P<start>\d+)(?:,(?P<count>\d+))?", line)
            if not hunk:
                continue
            start = int(hunk.group("start"))
            count = int(hunk.group("count") or "1")
            for src_file in bug.src_files:
                if src_file not in source_cache:
                    rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo)
                    source_cache[src_file] = text if rc_show == 0 else ""
                for candidate_line in range(start, start + max(count, 1)):
                    fn = _find_enclosing_cpp_function(source_cache[src_file], candidate_line)
                    if fn:
                        funcs.add(fn)
                        break

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
    lines = source.splitlines()
    idx = min(max(line_number - 1, 0), len(lines) - 1)
    lower = max(0, idx - 160)
    signature_parts: List[str] = []
    for line_idx in range(idx, lower - 1, -1):
        line = lines[line_idx].strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("#"):
            signature_parts = []
            continue
        signature_parts.insert(0, line)
        joined = " ".join(signature_parts)
        if ";" in joined and "{" not in joined:
            signature_parts = []
            continue
        if "(" not in joined:
            continue
        name = _function_from_signature(joined)
        if name:
            return name
    return ""


def _function_from_signature(signature: str) -> str:
    signature = re.sub(r"//.*", "", signature)
    signature = re.sub(r"/\*.*?\*/", " ", signature)
    signature = re.sub(r"\s+", " ", signature).strip()
    if not signature or signature.startswith(("if ", "for ", "while ", "switch ", "return ")):
        return ""
    before_args = signature.split("(", 1)[0].strip()
    tokens = re.findall(r"[A-Za-z_~][\w:~]*", before_args)
    if not tokens:
        return ""
    name = _normalize_cpp_function(tokens[-1])
    leaf = name.rsplit("::", 1)[-1]
    if leaf in {"if", "for", "while", "switch", "return", "sizeof"}:
        return ""
    return name


RUN_ONE_TEST_SH = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # Auto-generated by build_meta_cppcheck.py
    set -uo pipefail
    HERE=$(cd "$(dirname "$0")" && pwd)
    TEST_ID="${1:?Usage: $0 <test_id>}"
    BUILD_DIR="$HERE/__BUILD_DIR_NAME__"
    if [[ "$TEST_ID" == *"::"* && -x "$BUILD_DIR/bin/testrunner" ]]; then
      OUTPUT=$(cd "$BUILD_DIR/bin" && ./testrunner "$TEST_ID" 2>&1)
    else
      OUTPUT=$(ctest --test-dir "$BUILD_DIR" -R "^${TEST_ID}$" -V --timeout 180 2>&1)
    fi
    STATUS=$?
    echo "$OUTPUT"
    if [[ $STATUS -ne 0 ]] || echo "$OUTPUT" | grep -q '\*\*\*Failed' || ! echo "$OUTPUT" | grep -q 'Tests failed: 0'; then
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


def _existing_metadata_is_current(
    path: Path,
    *,
    require_coverage: bool,
    expected_granularity: str,
    expected_max_tests: int,
) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    phase_info = data.get("phase_info") or {}
    tests = data.get("tests") or []
    if not (
        bool(tests)
        and not data.get("build_error")
        and phase_info.get("test_granularity") == expected_granularity
    ):
        return False
    if int(phase_info.get("max_tests") or 0) != int(expected_max_tests or 0):
        return False
    if int(phase_info.get("coverage_parser_version") or 0) != COVERAGE_PARSER_VERSION:
        return False
    if require_coverage and phase_info.get("skip_coverage"):
        return False
    return True


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
    test_granularity: str,
    max_tests: int,
) -> Optional[Path]:
    safe_name = f"{bug.safe_bug_id}_meta.json"
    out_path = metadata_dir / safe_name
    raw_out_path = raw_dir / safe_name
    phase_granularity = (
        f"cppcheck_testrunner_{test_granularity}"
        if test_granularity in {"class", "subtest"}
        else "ctest"
    )
    if skip_if_exists and out_path.exists():
        if _existing_metadata_is_current(
            out_path,
            require_coverage=not skip_coverage,
            expected_granularity=phase_granularity,
            expected_max_tests=max_tests,
        ):
            log(f"[skip] {bug.bug_id}")
            return out_path
        log(f"[rerun] {bug.bug_id} existing metadata has old/different test selection")

    log(f"=== {bug.bug_id} | {bug.type_name or ''} ===")
    try:
        repo = ensure_repo(out_root, bug.sha_after, clone_if_missing=clone_if_missing)
    except Exception as exc:
        log(f"  [error] repo: {exc}")
        return None

    source_file = str(repo / bug.src_files[0]) if bug.src_files else ""
    compile_cmd = (
        f"cd {shlex.quote(str(repo))} && cmake -G Ninja -S . -B {BUILD_DIR_NAME} "
        f"-DBUILD_TESTS=ON -DBUILD_GUI=OFF -DCMAKE_CXX_FLAGS='{COV_CFLAGS}' && "
        f"ninja -C {BUILD_DIR_NAME} -j{jobs}"
    )
    test_cmd_template = f"bash {shlex.quote(str(repo / 'run_one_test.sh'))} {{test_id}}"
    phase_info = {
        "dual_run": dual_run,
        "run_all_tests": run_all_tests,
        "max_tests": max_tests,
        "selection_policy": "trigger_tests_first_then_fill_to_max_tests",
        "skip_coverage": skip_coverage,
        "coverage_parser_version": COVERAGE_PARSER_VERSION,
        "phase_a_build": "asan" if phase_a_asan else "plain",
        "test_granularity": phase_granularity,
        "build_compat_patches": [
            "cppcheckexecutor_mystacksize_sigstksz",
            "valueflow_include_limits",
        ],
        "errors": [],
    }

    try:
        checkout_buggy(repo, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy: {exc}")
        return None

    if not compile_project(repo, jobs=jobs, asan=phase_a_asan, coverage=False):
        record = _empty_record(bug, repo, compile_cmd, error="buggy_phase_a_compile_failed")
        _write_meta(raw_out_path, record)
        _write_meta(out_path, record)
        return out_path
    write_run_one_test(repo)
    build_dir = repo / BUILD_DIR_NAME
    all_tests = list_project_tests(repo, build_dir, test_granularity)
    trigger_tests = select_tests(all_tests, bug)
    selected = order_tests_with_triggers_first(all_tests, bug, run_all_tests=run_all_tests)
    if max_tests and max_tests > 0 and len(selected) > max_tests:
        selected = selected[:max_tests]
    phase_info["all_tests"] = len(all_tests)
    phase_info["trigger_tests"] = len(trigger_tests)
    phase_info["selected_tests"] = len(selected)
    log(f"  [tests] all={len(all_tests)}, selected={len(selected)}")
    if not selected:
        phase_info["errors"].append("no_tests_selected")

    results_a: List[TestResult] = []
    for idx, test_name in enumerate(selected, 1):
        passed, output = run_one_ctest(build_dir, test_name, timeout=test_timeout, asan=phase_a_asan)
        results_a.append(TestResult(
            test_id=test_name,
            outcome="PASS" if passed else "FAIL",
            fail_reason="" if passed else output[-3000:],
            actual_output="" if passed else output[-3000:],
        ))
        if idx % 5 == 0 or idx == len(selected):
            n_fail = sum(1 for r in results_a if r.outcome == "FAIL")
            log(f"  [phaseA-buggy] {idx}/{len(selected)} (fail={n_fail})")

    fixed_map: Dict[str, str] = {}
    if dual_run:
        try:
            checkout_fixed(repo, bug)
            if compile_project(repo, jobs=jobs, asan=phase_a_asan, coverage=False):
                fixed_build_dir = repo / BUILD_DIR_NAME
                for idx, test_name in enumerate(selected, 1):
                    passed, _ = run_one_ctest(fixed_build_dir, test_name, timeout=test_timeout, asan=phase_a_asan)
                    fixed_map[test_name] = "PASS" if passed else "FAIL"
                    if idx % 5 == 0 or idx == len(selected):
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
                for idx, test_name in enumerate(selected, 1):
                    clear_gcda(cov_build_dir)
                    run_one_ctest(cov_build_dir, test_name, timeout=test_timeout, asan=False)
                    cov, cov_error = collect_coverage(repo, cov_build_dir, verbose=(idx <= 2))
                    raw_cov_map[test_name] = coverage_to_qualified(cov)
                    meta_cov_map[test_name] = coverage_to_qualified(filter_production_coverage(cov))
                    if raw_cov_map[test_name] and not meta_cov_map[test_name] and not cov_error:
                        cov_error = "filtered_no_production_coverage"
                    cov_error_map[test_name] = cov_error
                    if idx % 5 == 0 or idx == len(selected):
                        n_raw = sum(1 for v in raw_cov_map.values() if v)
                        n_meta = sum(1 for v in meta_cov_map.values() if v)
                        log(f"  [phaseB] {idx}/{len(selected)} (raw_cov={n_raw}, meta_cov={n_meta})")
            else:
                phase_info["errors"].append("coverage_compile_failed")
                cov_error_map.update({name: "coverage_compile_failed" for name in selected})
        except Exception as exc:
            phase_info["errors"].append(f"coverage_phase_exception: {exc}")
            cov_error_map.update({name: f"coverage_phase_exception: {exc}" for name in selected})
            log(f"  [error] coverage phase failed: {exc}")

    raw_results: List[TestResult] = []
    metadata_results: List[TestResult] = []
    for result in results_a:
        common = {
            "test_id": result.test_id,
            "outcome": result.outcome,
            "outcome_fixed": fixed_map.get(result.test_id, "NOT_RUN"),
            "fail_reason": result.fail_reason,
            "actual_output": result.actual_output,
        }
        raw_results.append(TestResult(
            **common,
            covered_functions=raw_cov_map.get(result.test_id, []),
            coverage_error="",
        ))
        metadata_results.append(TestResult(
            **common,
            covered_functions=meta_cov_map.get(result.test_id, []),
            coverage_error=cov_error_map.get(result.test_id, ""),
        ))

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
            "coverage_filter": "keep lib/*, cli/* and selected production C/C++ files; drop test/*",
        },
        "tests": [_test_to_dict(r) for r in metadata_results],
    }
    _write_meta(raw_out_path, raw_record)
    log(f"  [ok] wrote raw {raw_out_path}")
    _write_meta(out_path, metadata_record)
    log(f"  [ok] wrote {out_path}")
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build Unified-Debugging metadata for danmar/cppcheck.")
    parser.add_argument("--sha", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out-root", type=Path, default=_detect_default_out_root())
    parser.add_argument("--metadata-dir", type=Path, default=_detect_default_metadata_dir())
    parser.add_argument("--raw-dir", type=Path, default=_detect_default_raw_dir())
    parser.add_argument("--jobs", type=int, default=max((os.cpu_count() or 2) - 1, 1))
    parser.add_argument("--test-timeout", type=int, default=180)
    parser.add_argument(
        "--max-tests",
        type=int,
        default=50,
        help="Maximum tests to run per bug after discovery/selection. Use 0 for unlimited.",
    )
    parser.add_argument("--skip-coverage", action="store_true")
    parser.add_argument("--dual-run", dest="dual_run", action="store_true", default=True)
    parser.add_argument("--single-run", dest="dual_run", action="store_false")
    parser.add_argument("--run-all-tests", dest="run_all_tests", action="store_true", default=True)
    parser.add_argument("--trigger-tests-only", dest="run_all_tests", action="store_false")
    parser.add_argument(
        "--test-granularity",
        choices=("class", "subtest", "ctest"),
        default="subtest",
        help=(
            "cppcheck test granularity. Default 'subtest' runs individual TestClass::testCase "
            "entries. Trigger tests are ordered first, then --max-tests is applied."
        ),
    )
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
                    test_granularity=args.test_granularity,
                    max_tests=args.max_tests,
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
