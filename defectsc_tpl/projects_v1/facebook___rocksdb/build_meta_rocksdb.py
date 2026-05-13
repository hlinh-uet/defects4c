#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project facebook___rocksdb.

Design:
  - fixed tree is commit_after
  - buggy tree is commit_after with files.src overlaid from commit_before
  - Phase A runs GoogleTest cases and stores buggy/fixed outcomes
  - Phase B rebuilds buggy with gcov and stores real per-test coverage
  - raw/ keeps full gcov coverage; metadata/ keeps production-only coverage
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
REMOTE_URL = "https://github.com/facebook/rocksdb.git"
BUILD_DIR_NAME = "build_meta_rocksdb"

BASE_CFLAGS = "-g -O0 -Wno-error"
COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage -Wno-error"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage -lgcov"

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
    build_flags: List[str]
    test_flags: List[str]
    type_name: Optional[str]
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
class TestSpec:
    test_id: str
    binary_name: str
    gtest_filter: str


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
    if _safe_exists(Path("/out")):
        return Path("/out") / PROJECT_NAME
    return host_default


def _detect_default_metadata_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "rocksdb" / "metadata"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/rocksdb/metadata")
    return host_default


def _detect_default_raw_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "rocksdb" / "raw"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/rocksdb/raw")
    return host_default


def _acquire_single_run_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.seek(0)
        owner = fp.read().strip()
        fp.close()
        suffix = f" owner={owner}" if owner else ""
        raise RuntimeError(f"Another build_meta_rocksdb process is running (lock: {lock_path}){suffix}.")
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp


def _lock_path_for_run(out_root: Path) -> Path:
    key = hashlib.md5(str(out_root.resolve()).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_rocksdb.{key}.lock"


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

    out = (proc.stdout or "") if capture else ""
    err = (proc.stderr or "") if capture else ""
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(shlex.quote(a) for a in args)}\n"
            f"stdout: {out[-2000:]}\nstderr: {err[-2000:]}"
        )
    return proc.returncode, out, err


def load_bugs() -> List[BugEntry]:
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for raw in data:
        sha_after = raw.get("commit_after") or ""
        if not sha_after:
            continue
        files = raw.get("files") or {}
        c_compile = raw.get("c_compile") or {}
        bug_type = raw.get("type") or {}
        bugs.append(BugEntry(
            sha_after=sha_after,
            sha_before=raw.get("commit_before") or "",
            src_files=list(files.get("src") or []),
            test_files=list(files.get("test") or []),
            build_flags=list(c_compile.get("build_flags") or []),
            test_flags=list(c_compile.get("test_flags") or []),
            type_name=bug_type.get("name"),
            type_id=bug_type.get("id") or sha_after,
            raw=raw,
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
        raise FileNotFoundError(f"Repo not found: {repo}. Run clone step or pass --clone.")
    out_root.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {REMOTE_URL} -> {repo}")
    run(["git", "clone", REMOTE_URL, str(repo)], check=True, timeout=3600)
    return repo


def _git_checkout(repo: Path, sha: str, label: str) -> None:
    log(f"  [git] checkout {label}={sha[:10]}")
    run(["git", "reset", "--hard"], cwd=repo, check=True)
    run(["git", "clean", "-ffdx"], cwd=repo, check=True)
    rc, _, _ = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
    if rc != 0:
        run(["git", "fetch", "--all", "--tags"], cwd=repo, check=True, timeout=1800)
    run(["git", "checkout", "--force", sha], cwd=repo, check=True)


def checkout_buggy(repo: Path, bug: BugEntry) -> None:
    _git_checkout(repo, bug.sha_after, "fixed-base")
    if bug.sha_before and bug.src_files:
        log(f"  [git] overlay buggy src from {bug.sha_before[:10]}")
        run(["git", "checkout", "--force", bug.sha_before, "--", *bug.src_files], cwd=repo, check=True)


def checkout_fixed(repo: Path, bug: BugEntry) -> None:
    _git_checkout(repo, bug.sha_after, "fixed")


def _cmake_args(repo: Path, build_dir: Path, *, coverage: bool) -> List[str]:
    cflags = COV_CFLAGS if coverage else BASE_CFLAGS
    ldflags = COV_LDFLAGS if coverage else ""
    return [
        "cmake",
        "-G", "Ninja",
        "-S", str(repo),
        "-B", str(build_dir),
        "-DCMAKE_BUILD_TYPE=Debug",
        "-DCMAKE_C_FLAGS=" + cflags,
        "-DCMAKE_CXX_FLAGS=" + cflags,
        "-DCMAKE_EXE_LINKER_FLAGS=" + ldflags,
        "-DCMAKE_SHARED_LINKER_FLAGS=" + ldflags,
        "-DWITH_TESTS=ON",
        "-DFAIL_ON_WARNINGS=OFF",
        "-DWITH_BENCHMARK_TOOLS=OFF",
        "-DWITH_TOOLS=OFF",
    ]


def _build_env() -> dict:
    env = os.environ.copy()
    env.setdefault("CC", "gcc")
    env.setdefault("CXX", "g++")
    env.setdefault("GTEST_THROW_ON_FAILURE", "0")
    env.setdefault("SKIP_FORMAT_BUCK_CHECKS", "1")
    env.setdefault("CTEST_OUTPUT_ON_FAILURE", "1")
    env.setdefault("CTEST_TEST_TIMEOUT", "300")
    return env


def _target_candidates(binary_name: str) -> List[str]:
    return list(dict.fromkeys([binary_name, f"test/{binary_name}", f"db/{binary_name}", f"options/{binary_name}"]))


def compile_project(repo: Path, *, binary_names: List[str], jobs: int, coverage: bool, timeout: int = 3600) -> bool:
    build_dir = repo / BUILD_DIR_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    env = _build_env()
    phase = "GCOV" if coverage else "plain"
    log(f"  [build-{phase}] cmake configure")
    rc, out, err = run(_cmake_args(repo, build_dir, coverage=coverage), cwd=repo, env=env, timeout=900)
    if rc != 0:
        log(f"  [build-{phase}] cmake FAILED rc={rc}\n{(out + err)[-4000:]}")
        return False

    targets = list(dict.fromkeys(binary_names))
    if not targets:
        log("  [build] no test targets selected")
        return False
    for target in targets:
        built = False
        errors = []
        for candidate in _target_candidates(target):
            rc, out, err = run(["ninja", "-C", str(build_dir), f"-j{jobs}", candidate], cwd=repo, env=env, timeout=timeout)
            if rc == 0:
                built = True
                break
            errors.append((out + err)[-500:])
        if not built:
            log(f"  [build-{phase}] target {target} FAILED\n{errors[-1] if errors else ''}")
            return False
    if coverage:
        gcno = list(build_dir.rglob("*.gcno"))
        log(f"  [build-GCOV] gcno={len(gcno)}")
    return True


def _test_binary_from_file(test_file: str) -> str:
    return Path(test_file).stem


def initial_trigger_specs(bug: BugEntry) -> List[TestSpec]:
    binaries = [_test_binary_from_file(path) for path in bug.test_files]
    if not binaries:
        binaries = [Path(src).stem + "_test" for src in bug.src_files]
    out: List[TestSpec] = []
    for binary in binaries:
        for flag in bug.test_flags:
            case = str(flag).strip()
            if case:
                out.append(TestSpec(test_id=f"{binary}::{case}", binary_name=binary, gtest_filter=case))
    if not out:
        for binary in binaries:
            out.append(TestSpec(test_id=binary, binary_name=binary, gtest_filter="*"))
    return _unique_specs(out)


def _find_test_binary(build_dir: Path, binary_name: str) -> Optional[Path]:
    candidates = [
        build_dir / binary_name,
        build_dir / "test" / binary_name,
        build_dir / "db" / binary_name,
        build_dir / "options" / binary_name,
    ]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    for cand in build_dir.rglob(binary_name):
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def discover_gtest_cases(build_dir: Path, binary_name: str, *, timeout: int = 120) -> List[TestSpec]:
    binary = _find_test_binary(build_dir, binary_name)
    if not binary:
        return []
    rc, out, err = run([str(binary), "--gtest_list_tests"], cwd=binary.parent, timeout=timeout)
    if rc != 0:
        log(f"  [tests] --gtest_list_tests failed for {binary_name}: {(out + err)[-1000:]}")
        return []
    specs: List[TestSpec] = []
    suite = ""
    for raw in out.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("Running main"):
            continue
        if not line.startswith("  ") and line.endswith("."):
            suite = line.strip()
            continue
        if line.startswith("  ") and suite:
            case = line.strip().split("#", 1)[0].strip()
            if case:
                filt = f"{suite}{case}"
                specs.append(TestSpec(test_id=f"{binary_name}::{filt}", binary_name=binary_name, gtest_filter=filt))
    return _unique_specs(specs)


def _unique_specs(specs: List[TestSpec]) -> List[TestSpec]:
    seen = set()
    out = []
    for spec in specs:
        if spec.test_id in seen:
            continue
        seen.add(spec.test_id)
        out.append(spec)
    return out


def _gtest_suite_name(gtest_filter: str) -> str:
    return str(gtest_filter).split(".", 1)[0]


def select_specs(build_dir: Path, bug: BugEntry, *, run_all_tests: bool, max_tests: int) -> List[TestSpec]:
    triggers = initial_trigger_specs(bug)
    if not run_all_tests:
        selected = triggers
    else:
        all_specs: List[TestSpec] = []
        for binary in sorted({spec.binary_name for spec in triggers}):
            all_specs.extend(discover_gtest_cases(build_dir, binary))
        trigger_set = {spec.test_id for spec in triggers}
        trigger_suites = {
            (spec.binary_name, _gtest_suite_name(spec.gtest_filter))
            for spec in triggers
            if spec.gtest_filter and spec.gtest_filter != "*"
        }
        same_suite = [
            spec for spec in all_specs
            if spec.test_id not in trigger_set
            and (spec.binary_name, _gtest_suite_name(spec.gtest_filter)) in trigger_suites
        ]
        remaining = [
            spec for spec in all_specs
            if spec.test_id not in trigger_set
            and (spec.binary_name, _gtest_suite_name(spec.gtest_filter)) not in trigger_suites
        ]
        selected = triggers + same_suite + remaining
    if max_tests and max_tests > 0:
        selected = selected[:max_tests]
    return _unique_specs(selected)


def clear_gcda(path: Path) -> None:
    for gcda in path.rglob("*.gcda"):
        try:
            gcda.unlink()
        except OSError:
            pass


def run_one_test_spec(build_dir: Path, spec: TestSpec, *, timeout: int = 300) -> Tuple[bool, str]:
    binary = _find_test_binary(build_dir, spec.binary_name)
    if not binary:
        return False, f"test_binary_not_found:{spec.binary_name}"
    cmd = [str(binary), f"--gtest_filter={spec.gtest_filter}", "--gtest_color=no"]
    rc, out, err = run(cmd, cwd=binary.parent, env=_build_env(), timeout=timeout + 30)
    combined = (out or "") + ("\n" + err if err else "")
    passed = rc == 0
    if re.search(r"\[\s+FAILED\s+\]|FAILED TEST|Running 0 tests", combined):
        passed = False
    return passed, combined


def _parse_gcov_functions(text: str) -> List[str]:
    raw_funcs: List[str] = []
    for match in _GCOV_FUNC_RE_NEW.finditer(text):
        try:
            if float(match.group("pct")) > 0.0:
                raw_funcs.append(match.group("name"))
        except ValueError:
            pass
    if not raw_funcs:
        for match in _GCOV_FUNC_RE_OLD.finditer(text):
            try:
                if int(match.group("calls")) > 0:
                    raw_funcs.append(match.group("name"))
            except ValueError:
                pass
    funcs = [_normalize_cpp_function(name) for name in _demangle_cpp_names(raw_funcs)]
    return sorted(set(f for f in funcs if f))


def _demangle_cpp_names(names: List[str]) -> List[str]:
    if not names or not shutil.which("c++filt"):
        return names
    try:
        proc = subprocess.run(
            ["c++filt"],
            input="\n".join(names) + "\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return names
    if proc.returncode != 0:
        return names
    out = proc.stdout.splitlines()
    return out if len(out) == len(names) else names


def _source_from_gcov_text(text: str) -> str:
    for line in text.splitlines()[:20]:
        marker = "Source:"
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return ""


def _parse_gcov_file_sections(text: str) -> List[Tuple[str, List[str]]]:
    sections: List[Tuple[str, List[str]]] = []
    current_source = ""
    current_lines: List[str] = []

    def flush() -> None:
        nonlocal current_source, current_lines
        if current_source:
            funcs = _parse_gcov_functions("\n".join(current_lines))
            if funcs:
                sections.append((current_source, funcs))
        current_source = ""
        current_lines = []

    for line in text.splitlines():
        if line.startswith("File '") and "'" in line[len("File '"):]:
            flush()
            current_source = line.split("'", 2)[1]
            current_lines = []
            continue
        if current_source:
            current_lines.append(line)
    flush()
    return sections


def _source_from_gcda_path(repo: Path, build_dir: Path, gcda: Path) -> str:
    try:
        rel = gcda.relative_to(build_dir).as_posix()
    except ValueError:
        return ""
    marker = ".dir/"
    if marker not in rel:
        return ""
    source_rel = rel.split(marker, 1)[1]
    if source_rel.endswith(".gcda"):
        source_rel = source_rel[:-len(".gcda")]
    candidate = repo / source_rel
    if candidate.is_file():
        return source_rel
    return ""


def _source_to_repo_rel(repo: Path, gcda: Path, source: str) -> str:
    src = Path(source)
    candidates = [src] if src.is_absolute() else [
        (gcda.parent / src).resolve(),
        (repo / src).resolve(),
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return candidate.relative_to(repo).as_posix()
            except ValueError:
                return str(candidate).replace("\\", "/")
    return str(source).replace("\\", "/").lstrip("./")


def _source_path_from_rel(repo: Path, rel: str) -> Path:
    path = Path(rel)
    return path if path.is_absolute() else repo / path


def _function_likely_defined_in_source(repo: Path, rel: str, fn: str, cache: Dict[str, str]) -> bool:
    path = _source_path_from_rel(repo, rel)
    key = str(path)
    if key not in cache:
        try:
            cache[key] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            cache[key] = ""
    text = cache[key]
    if not text:
        return True

    name = re.sub(r"^(rocksdb|ROCKSDB_NAMESPACE)::", "", fn)
    if "::" in name:
        return re.search(re.escape(name) + r"\s*\(", text) is not None

    leaf = name.rsplit("::", 1)[-1]
    if leaf.startswith("operator"):
        return leaf in text
    return re.search(r"\b" + re.escape(leaf) + r"\s*\(", text) is not None


def collect_coverage(repo: Path, build_dir: Path, *, verbose: bool = False) -> Tuple[Dict[str, List[str]], str]:
    if not shutil.which("gcov"):
        return {}, "gcov_not_found"
    gcda_files = list(build_dir.rglob("*.gcda"))
    if not gcda_files:
        return {}, "no_gcda_files"
    for old in build_dir.rglob("*.gcov"):
        try:
            old.unlink()
        except OSError:
            pass
    covered: Dict[str, List[str]] = {}
    source_cache: Dict[str, str] = {}
    for gcda in gcda_files:
        rc, out, err = run(["gcov", "-f", gcda.name], cwd=gcda.parent, timeout=60)
        if rc != 0 and verbose:
            log(f"  [cov] gcov rc={rc} for {gcda.name}: {err[-300:]}")

        sections = _parse_gcov_file_sections(out)
        if not sections:
            funcs = _parse_gcov_functions(out)
            source = _source_from_gcov_text(out) or _source_from_gcda_path(repo, build_dir, gcda)
            sections = [(source, funcs)] if source and funcs else []

        for source, funcs in sections:
            rel = _source_to_repo_rel(repo, gcda, source)
            if rel.startswith(BUILD_DIR_NAME + "/"):
                continue
            funcs = [fn for fn in funcs if _function_likely_defined_in_source(repo, rel, fn, source_cache)]
            if funcs:
                covered.setdefault(rel, []).extend(funcs)
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
    if not rel.endswith((".h", ".hpp", ".hh", ".cc", ".cpp", ".cxx", ".c")):
        return False
    if rel.startswith((BUILD_DIR_NAME + "/", "test/", "third-party/", "plugin/")):
        return False
    return rel.startswith(("db/", "options/", "util/", "table/", "file/", "cache/", "memory/", "monitoring/", "env/", "include/", "utilities/"))


def filter_production_coverage(cov_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    return {rel: funcs for rel, funcs in cov_map.items() if _is_production_source(rel)}


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    return sorted(set(f"{os.path.basename(rel)}:{fn}" for rel, funcs in cov_map.items() for fn in funcs if fn))


def _normalize_cpp_function(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name)).strip()
    if not name:
        return ""
    name = _strip_cpp_parameter_list(name)
    name = re.sub(r"^(virtual|static|constexpr|const|inline|typename|class|struct)\s+", "", name)
    name = _drop_cpp_return_type(name)
    name = _strip_cpp_template_args(name)
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
    last_space = -1
    for idx, ch in enumerate(name):
        if ch == "<":
            angle_depth += 1
        elif ch == ">" and angle_depth:
            angle_depth -= 1
        elif ch.isspace() and angle_depth == 0:
            last_space = idx
    if last_space >= 0:
        candidate = name[last_space + 1:].strip()
        if candidate:
            return candidate
    return name


def _strip_cpp_template_args(name: str) -> str:
    out: List[str] = []
    depth = 0
    for ch in name:
        if ch == "<":
            depth += 1
            continue
        if ch == ">" and depth:
            depth -= 1
            continue
        if depth == 0:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


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


def _qualify_rocksdb_function(repo: Path, rel_source: str, fn: str) -> str:
    if not fn or "::" not in fn or fn.startswith("rocksdb::"):
        return fn
    try:
        text = (repo / rel_source).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fn
    if re.search(r"\bnamespace\s+rocksdb\b|\bROCKSDB_NAMESPACE\b", text):
        return f"rocksdb::{fn}"
    return fn


def extract_ground_truth(bug: BugEntry, repo: Path) -> List[str]:
    if not bug.sha_before or not bug.src_files:
        return []
    funcs = set()
    rc, diff, _ = run(["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files], cwd=repo)
    if rc == 0 and diff:
        for line in diff.splitlines():
            if line.startswith("@@"):
                fn = _function_from_diff_tail(line.split("@@", 2)[-1].strip())
                if fn:
                    funcs.add(_qualify_rocksdb_function(repo, bug.src_files[0], fn))
    return sorted(funcs)


def ground_truth_entries(bug: BugEntry, gt_funcs: List[str]) -> List[str]:
    basename = os.path.basename(bug.src_files[0]) if bug.src_files else ""
    return [f"{basename}:{fn}" for fn in gt_funcs] if basename else []


RUN_ONE_TEST_SH = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # Auto-generated by build_meta_rocksdb.py
    set -uo pipefail
    HERE=$(cd "$(dirname "$0")" && pwd)
    TEST_ID="${1:?Usage: $0 <test_id>}"
    BUILD_DIR="$HERE/__BUILD_DIR_NAME__"
    if [[ "$TEST_ID" == *"::"* ]]; then
      BIN_NAME="${TEST_ID%%::*}"
      GTEST_FILTER="${TEST_ID#*::}"
    else
      BIN_NAME="$TEST_ID"
      GTEST_FILTER="*"
    fi
    BIN=""
    for cand in "$BUILD_DIR/$BIN_NAME" "$BUILD_DIR/test/$BIN_NAME" "$BUILD_DIR/db/$BIN_NAME" "$BUILD_DIR/options/$BIN_NAME"; do
      if [[ -x "$cand" ]]; then BIN="$cand"; break; fi
    done
    if [[ -z "$BIN" ]]; then
      BIN=$(find "$BUILD_DIR" -type f -perm -111 -name "$BIN_NAME" | head -n 1)
    fi
    if [[ -z "$BIN" ]]; then
      echo "[run_one_test] binary not found for $TEST_ID" >&2
      exit 2
    fi
    OUTPUT=$("$BIN" --gtest_filter="$GTEST_FILTER" --gtest_color=no 2>&1)
    STATUS=$?
    echo "$OUTPUT"
    if [[ $STATUS -ne 0 ]] || echo "$OUTPUT" | grep -Eq '\[[[:space:]]*FAILED[[:space:]]*\]|FAILED TEST|Running 0 tests'; then
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
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _existing_metadata_is_current(path: Path, *, require_coverage: bool, expected_max_tests: int, expected_run_all: bool) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if data.get("build_error"):
        return False
    tests = data.get("tests")
    phase_info = data.get("phase_info") or {}
    if not isinstance(tests, list) or not tests:
        return False
    if int(phase_info.get("max_tests") or 0) != int(expected_max_tests or 0):
        return False
    if bool(phase_info.get("run_all_tests")) != bool(expected_run_all):
        return False
    if require_coverage and phase_info.get("skip_coverage"):
        return False
    if require_coverage and not any(t.get("covered_functions") for t in tests if isinstance(t, dict)):
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


def metadata_compile_cmd(repo: Path, *, binary_names: List[str], jobs: int) -> str:
    build_dir = BUILD_DIR_NAME
    cmake_args = _cmake_args(Path("."), Path(build_dir), coverage=False)
    targets = list(dict.fromkeys(binary_names))
    ninja_parts = [
        " || ".join(
            " ".join(shlex.quote(x) for x in ["ninja", "-C", build_dir, f"-j{jobs}", candidate])
            for candidate in _target_candidates(target)
        )
        for target in targets
    ]
    return (
        f"cd {shlex.quote(str(repo))} && "
        f"{' '.join(shlex.quote(x) for x in cmake_args)} && "
        + " && ".join(f"({part})" for part in ninja_parts)
    )


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
    max_tests: int,
) -> Optional[Path]:
    safe_name = f"{bug.safe_bug_id}_meta.json"
    out_path = metadata_dir / safe_name
    raw_out_path = raw_dir / safe_name
    if skip_if_exists and out_path.exists():
        if _existing_metadata_is_current(
            out_path,
            require_coverage=not skip_coverage,
            expected_max_tests=max_tests,
            expected_run_all=run_all_tests,
        ):
            log(f"[skip] {bug.bug_id}")
            return out_path
        log(f"[rerun] {bug.bug_id} existing metadata is old/invalid")

    log(f"=== {bug.bug_id} | {bug.type_name or ''} ===")
    try:
        repo = ensure_repo(out_root, bug.sha_after, clone_if_missing=clone_if_missing)
    except Exception as exc:
        log(f"  [error] repo: {exc}")
        return None

    trigger_specs = initial_trigger_specs(bug)
    binary_names = sorted({spec.binary_name for spec in trigger_specs})
    compile_cmd = metadata_compile_cmd(repo, binary_names=binary_names, jobs=jobs)
    source_file = str(repo / bug.src_files[0]) if bug.src_files else ""
    test_cmd_template = f"bash {shlex.quote(str(repo / 'run_one_test.sh'))} {{test_id}}"
    phase_info = {
        "dual_run": dual_run,
        "run_all_tests": run_all_tests,
        "max_tests": max_tests,
        "skip_coverage": skip_coverage,
        "test_granularity": "gtest_case",
        "selection_policy": "test_flags_then_same_suite_then_binary_cases" if run_all_tests else "test_flags_only",
        "errors": [],
    }

    try:
        checkout_buggy(repo, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy: {exc}")
        return None

    if not compile_project(repo, binary_names=binary_names, jobs=jobs, coverage=False):
        record = _empty_record(bug, repo, compile_cmd, error="buggy_phase_a_compile_failed")
        _write_meta(raw_out_path, record)
        _write_meta(out_path, record)
        return out_path
    write_run_one_test(repo)
    build_dir = repo / BUILD_DIR_NAME
    selected = select_specs(build_dir, bug, run_all_tests=run_all_tests, max_tests=max_tests)
    phase_info["trigger_tests"] = len(trigger_specs)
    phase_info["selected_tests"] = len(selected)
    phase_info["selected_binaries"] = binary_names
    log(f"  [tests] selected={len(selected)}, binaries={','.join(binary_names)}")
    if not selected:
        phase_info["errors"].append("no_tests_selected")

    results_a: List[TestResult] = []
    for idx, spec in enumerate(selected, 1):
        passed, output = run_one_test_spec(build_dir, spec, timeout=test_timeout)
        results_a.append(TestResult(
            test_id=spec.test_id,
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
            if compile_project(repo, binary_names=binary_names, jobs=jobs, coverage=False):
                fixed_build_dir = repo / BUILD_DIR_NAME
                for idx, spec in enumerate(selected, 1):
                    passed, _ = run_one_test_spec(fixed_build_dir, spec, timeout=test_timeout)
                    fixed_map[spec.test_id] = "PASS" if passed else "FAIL"
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
            if compile_project(repo, binary_names=binary_names, jobs=jobs, coverage=True):
                cov_build_dir = repo / BUILD_DIR_NAME
                for idx, spec in enumerate(selected, 1):
                    clear_gcda(cov_build_dir)
                    run_one_test_spec(cov_build_dir, spec, timeout=test_timeout)
                    cov, cov_error = collect_coverage(repo, cov_build_dir, verbose=(idx <= 2))
                    raw_cov_map[spec.test_id] = coverage_to_qualified(cov)
                    meta_cov_map[spec.test_id] = coverage_to_qualified(filter_production_coverage(cov))
                    if raw_cov_map[spec.test_id] and not meta_cov_map[spec.test_id] and not cov_error:
                        cov_error = "filtered_no_production_coverage"
                    cov_error_map[spec.test_id] = cov_error
                    if idx % 5 == 0 or idx == len(selected):
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
            "outcome_fixed": fixed_map.get(result.test_id, "NOT_RUN"),
            "fail_reason": result.fail_reason,
            "actual_output": result.actual_output,
        }
        raw_results.append(TestResult(
            **common,
            covered_functions=raw_cov_map.get(result.test_id, []),
            coverage_error=cov_error_map.get(result.test_id, ""),
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
        "ground_truth": ground_truth_entries(bug, gt_funcs),
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
            "coverage_filter": "keep RocksDB production dirs; drop tests/build/third-party",
        },
        "tests": [_test_to_dict(r) for r in metadata_results],
    }
    _write_meta(raw_out_path, raw_record)
    log(f"  [ok] wrote raw {raw_out_path}")
    _write_meta(out_path, metadata_record)
    log(f"  [ok] wrote {out_path}")
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build Unified-Debugging metadata for facebook/rocksdb.")
    parser.add_argument("--sha", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out-root", type=Path, default=_detect_default_out_root())
    parser.add_argument("--metadata-dir", type=Path, default=_detect_default_metadata_dir())
    parser.add_argument("--raw-dir", type=Path, default=_detect_default_raw_dir())
    parser.add_argument("--jobs", type=int, default=max((os.cpu_count() or 2) - 1, 1))
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--max-tests", type=int, default=0, help="Maximum tests after selection. 0 means no cap.")
    parser.add_argument("--skip-coverage", action="store_true")
    parser.add_argument("--dual-run", dest="dual_run", action="store_true", default=True)
    parser.add_argument("--single-run", dest="dual_run", action="store_false")
    parser.add_argument("--run-all-tests", dest="run_all_tests", action="store_true", default=True, help="Run all gtest cases in selected test binaries (default).")
    parser.add_argument("--trigger-tests-only", dest="run_all_tests", action="store_false", help="Run only c_compile.test_flags trigger cases.")
    parser.add_argument("--skip-if-exists", action="store_true")
    parser.add_argument("--clone", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    bugs = load_bugs()
    if args.sha:
        wanted = tuple(s for s in args.sha if s)
        bugs = [b for b in bugs if b.sha_after in wanted or b.sha_after.startswith(wanted)]
    if args.limit:
        bugs = bugs[:args.limit]
    if args.list:
        for bug in bugs:
            print(f"{bug.bug_id}\t{bug.type_name or '-'}\t{bug.sha_after}\t{','.join(bug.src_files)}\t{','.join(bug.test_flags)}")
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
