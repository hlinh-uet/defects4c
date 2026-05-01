#!/usr/bin/env python3
"""
build_meta_libyang.py — Pipeline sinh metadata Unified-Debugging cho CESNET/libyang.

Tương tự build_meta_tcpdump.py nhưng dùng CMake+Ninja+CTest thay vì autoconf+make+TESTonce.

Với mỗi bug trong bugs_list_new.json:
  1. Checkout fixed tree (commit_after), overlay src_files từ commit_before.
  2. CMake configure + Ninja build với coverage flags.
  3. Chạy CTest từng test case, thu gcov coverage riêng.
  4. Ghi full coverage vào raw/ và coverage đã bỏ test/harness vào metadata/.
"""
from __future__ import annotations
import argparse, fcntl, hashlib, json, os, re, shlex, shutil, subprocess, sys
import textwrap, time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Path constants ──
PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"
REMOTE_URL = "https://github.com/CESNET/libyang.git"
BUILD_DIR_NAME = "build_meta_libyang"
BUILD_META_CMOCKA_FILTER_ENV = "BUILD_META_CMOCKA_TEST_FILTER"
COVERAGE_PARSER_VERSION = 2

COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage -Wno-error"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage -lgcov"
ASAN_CFLAGS = "-fsanitize=address -fno-omit-frame-pointer"
ASAN_LDFLAGS = "-fsanitize=address"

# gcov <11 format: function NAME called N returned M%
_GCOV_FUNC_RE_OLD = re.compile(
    r"^function\s+(?P<name>\S+)\s+called\s+(?P<calls>\d+)\s+returned", re.MULTILINE,
)
# gcov 11+ (Ubuntu 22.04) format: Function 'NAME'\nLines executed:XX.XX% of N
_GCOV_FUNC_RE_NEW = re.compile(
    r"^Function\s+'(?P<name>[^']+)'\s*\n"
    r"Lines executed:(?P<pct>[\d.]+)%\s+of\s+(?P<lines>\d+)",
    re.MULTILINE,
)
def _parse_gcov_functions(text: str) -> List[str]:
    """Parse gcov output, hỗ trợ cả format cũ (gcov <11) và mới (gcov 11+)."""
    funcs = []
    for m in _GCOV_FUNC_RE_NEW.finditer(text):
        try:
            if float(m.group("pct")) > 0.0: funcs.append(_normalize_c_function(m.group("name")))
        except ValueError: pass
    if funcs: return funcs
    for m in _GCOV_FUNC_RE_OLD.finditer(text):
        try:
            if int(m.group("calls")) > 0: funcs.append(_normalize_c_function(m.group("name")))
        except ValueError: pass
    return [f for f in funcs if f]

def _normalize_c_function(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name)).strip()
    if not name:
        return ""
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    name = re.sub(r"^(static|extern|inline|const|volatile)\s+", "", name)
    if " " in name:
        name = name.rsplit(" ", 1)[-1]
    return name.strip("* ")

# ── Helpers ──
def _safe_exists(p: Path) -> bool:
    try: return p.exists()
    except OSError: return False

def _detect(name, host_sub, container_sub):
    h = DEFECTS4C_ROOT / host_sub
    if _safe_exists(h): return h
    c = Path(container_sub)
    if _safe_exists(c.parent): return c
    # Fallback: tạo mới tại host path
    return h

def log(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def which(bin_name: str) -> Optional[str]:
    return shutil.which(bin_name)

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
        raise RuntimeError(f"Đang có process khác chạy (lock: {lock_path}){owner_msg}.")
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp

def _lock_path_for_run(out_root: Path, metadata_dir: Path) -> Path:
    del metadata_dir
    key = hashlib.md5(str(out_root.resolve()).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_libyang.{key}.lock"

def run(cmd, *, cwd=None, env=None, check=False, timeout=None, capture=True):
    args = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    try:
        proc = subprocess.run(
            args, cwd=str(cwd) if cwd else None, env=env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            encoding="utf-8" if capture else None,
            errors="replace" if capture else None, timeout=timeout,
        )
    except subprocess.TimeoutExpired: return 124, "", "TimeoutExpired"
    except UnicodeDecodeError: return 1, "", "UnicodeDecodeError"
    rc = proc.returncode
    out = (proc.stdout or "") if capture else ""
    err = (proc.stderr or "") if capture else ""
    if check and rc != 0:
        raise RuntimeError(f"Command failed ({rc}): {' '.join(args)}\n{err[-2000:]}")
    return rc, out, err

# ── Dataclasses ──
@dataclass
class BugEntry:
    sha_after: str; sha_before: str; src_files: List[str]; test_files: List[str]
    type_name: Optional[str]; type_id: str; raw: dict
    test_flags: List[str] = field(default_factory=list)
    output_bug_id: str = ""
    @property
    def bug_id(self): return self.type_id or f"{PROJECT_NAME}@{self.sha_after}"
    @property
    def safe_bug_id(self):
        base = self.output_bug_id or self.bug_id
        return base.replace("@","__").replace("/","__")

@dataclass
class TestResult:
    test_id: str; outcome: str; outcome_fixed: str = ""; fail_reason: str = ""
    actual_output: str = ""; covered_functions: List[str] = field(default_factory=list)
    coverage_error: str = ""

@dataclass
class CTestEntry:
    name: str
    command: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)

@dataclass
class TestSpec:
    test_id: str
    ctest_name: str
    case_name: str = ""
    command: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)

# ── Load bugs ──
def load_bugs() -> List[BugEntry]:
    data = json.loads(BUGS_JSON.read_text())
    bugs = []
    for raw in data:
        sha_after = raw.get("commit_after")
        if not sha_after: continue
        sha_before = raw.get("commit_before", "")
        files = raw.get("files", {}) or {}
        src_files = files.get("src", []) or []
        test_files = files.get("test", []) or []
        t = raw.get("type") or {}
        cc = raw.get("c_compile") or {}
        tf = cc.get("test_flags") or []
        bugs.append(BugEntry(
            sha_after=sha_after, sha_before=sha_before or "",
            src_files=src_files, test_files=test_files,
            type_name=t.get("name"), type_id=t.get("id") or sha_after,
            raw=raw, test_flags=tf,
        ))
    counts = Counter(b.bug_id for b in bugs)
    for b in bugs:
        b.output_bug_id = f"{b.bug_id}__{b.sha_after[:12]}" if counts[b.bug_id] > 1 else b.bug_id
    return bugs

# ── Git operations ──
def ensure_repo(out_root, sha_after, *, clone_if_missing=False) -> Path:
    repo = out_root / f"git_repo_dir_{sha_after}"
    if repo.exists() and (repo / ".git").exists(): return repo
    if not clone_if_missing:
        raise FileNotFoundError(f"Repo chưa tồn tại: {repo}")
    out_root.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {REMOTE_URL} → {repo}")
    run(["git", "clone", REMOTE_URL, str(repo)], check=True)
    return repo

def _git_checkout(repo, sha, label):
    log(f"  [git] checkout {label}={sha[:10]}")
    run(["git", "reset", "--hard"], cwd=repo, check=True)
    run(["git", "clean", "-ffdx"], cwd=repo)
    rc, _, _ = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
    if rc != 0: run(["git", "fetch", "--all", "--tags"], cwd=repo)
    run(["git", "checkout", "--force", sha], cwd=repo, check=True)

def checkout_buggy(repo, bug):
    _git_checkout(repo, bug.sha_after, "fixed-base")
    if bug.sha_before and bug.src_files:
        log(f"  [git] overlay buggy src from {bug.sha_before[:10]}")
        run(["git", "checkout", "--force", bug.sha_before, "--", *bug.src_files],
            cwd=repo, check=True)

def checkout_fixed(repo, bug):
    _git_checkout(repo, bug.sha_after, "fixed")

def apply_cmocka_filter_patch(repo: Path) -> int:
    """Make libyang CMocka test binaries filterable per test case.

    libyang registers only one CTest entry per CMocka binary. CMocka supports
    filtering through cmocka_set_test_filter(), but these historical binaries do
    not expose it. This build-only patch wires an env var to that API.
    """
    patched = 0
    marker = "BUILD_META_CMOCKA_TEST_FILTER"
    for path in sorted((repo / "tests").rglob("*.c")):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if "cmocka_run_group_tests" not in text or marker in text:
            continue
        new_text = text
        if "#include <cmocka.h>" in new_text and "#include <stdlib.h>" not in new_text:
            new_text = new_text.replace("#include <cmocka.h>", "#include <cmocka.h>\n#include <stdlib.h>", 1)
        hook = (
            "{\n"
            f"        const char *build_meta_filter = getenv(\"{BUILD_META_CMOCKA_FILTER_ENV}\");\n"
            "        if (build_meta_filter && build_meta_filter[0]) {\n"
            "            cmocka_set_test_filter(build_meta_filter);\n"
            "        }\n"
            "    }\n"
            "    return cmocka_run_group_tests("
        )
        new_text = new_text.replace("return cmocka_run_group_tests(", hook, 1)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            patched += 1
    return patched

# ── CMake build ──
def compile_with_coverage(repo, *, jobs, timeout=1800, asan=False) -> bool:
    patched = apply_cmocka_filter_patch(repo)
    if patched:
        log(f"  [build-compat] cmocka filter patch applied to {patched} file(s)")

    env = os.environ.copy()
    cflags = COV_CFLAGS
    ldflags = COV_LDFLAGS
    if asan:
        cflags = f"{cflags} {ASAN_CFLAGS}"
        ldflags = f"{ldflags} {ASAN_LDFLAGS}"
        env["ASAN_OPTIONS"] = "detect_leaks=0"
    env["CC"] = "gcc"; env["CXX"] = "g++"

    build_dir = repo / BUILD_DIR_NAME
    if build_dir.exists(): shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(exist_ok=True)

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
        "-DENABLE_TESTS=ON",
        "-DENABLE_BUILD_TESTS=ON",
        "-DENABLE_VALGRIND_TESTS=OFF",
        "-DENABLE_COVERAGE=OFF",
        "-DENABLE_TOOLS=OFF",
    ]
    log("  [build] cmake configure")
    rc, out, err = run(cmake_args, cwd=repo, env=env, timeout=300)
    if rc != 0:
        log(f"  [build] cmake FAILED rc={rc}\n{(out + err)[-3000:]}")
        return False

    log(f"  [build] ninja -j{jobs}")
    rc, out, err = run(["ninja", "-C", str(build_dir), f"-j{jobs}"],
                     cwd=repo, env=env, timeout=timeout)
    if rc != 0:
        log(f"  [build] ninja FAILED rc={rc}\n{(out + err)[-3000:]}")
        return False

    gcno = list(build_dir.rglob("*.gcno"))
    if gcno: log(f"  [build] coverage OK ({len(gcno)} *.gcno)")
    else: log("  [build] WARNING: no *.gcno found")
    return True

# ── CTest operations ──
def _parse_ctest_environment(value) -> Dict[str, str]:
    if not value:
        return {}
    items = value if isinstance(value, list) else [value]
    env: Dict[str, str] = {}
    for item in items:
        for part in str(item).split(";"):
            if "=" in part:
                key, val = part.split("=", 1)
                if key:
                    env[key] = val
    return env

def list_ctest_entries(build_dir: Path) -> List[CTestEntry]:
    """Lấy danh sách CTest entries kèm command/env, ưu tiên JSON."""
    rc, out, err = run(["ctest", "--test-dir", str(build_dir), "--show-only=json-v1"],
                       cwd=build_dir, timeout=60)
    if rc == 0:
        try:
            payload = json.loads(out)
            entries: List[CTestEntry] = []
            for test in payload.get("tests", []):
                name = test.get("name")
                if not name:
                    continue
                env: Dict[str, str] = {}
                for prop in test.get("properties", []) or []:
                    if prop.get("name") == "ENVIRONMENT":
                        env.update(_parse_ctest_environment(prop.get("value")))
                entries.append(CTestEntry(
                    name=name,
                    command=[str(arg) for arg in (test.get("command") or [])],
                    environment=env,
                ))
            if entries:
                return entries
        except json.JSONDecodeError as exc:
            log(f"  [tests] ctest json parse failed: {exc}")

    rc, out, err = run(["ctest", "--test-dir", str(build_dir), "--show-only=human"],
                       cwd=build_dir, timeout=60)
    if rc != 0:
        log(f"  [tests] ctest discovery failed rc={rc}\n{(out + err)[-2000:]}")
        return []
    names = []
    for line in out.splitlines():
        line = line.strip()
        # format: "Test #N: test_name"
        m = re.match(r"Test\s+#?\d+:\s+(\S+)", line)
        if m: names.append(m.group(1))
    return [CTestEntry(name=name) for name in names]

def list_ctest_tests(build_dir: Path) -> List[str]:
    return [entry.name for entry in list_ctest_entries(build_dir)]

def _candidate_ctest_names_for_source(path: Path) -> List[str]:
    stem = path.stem
    names = []
    if stem.startswith("test_"):
        names.append(stem[len("test_"):])
    names.append(stem)
    out = []
    for name in names:
        out.extend([name, f"src_{name}", f"utest_{name}"])
    return out

def _parse_cmocka_cases(source_text: str) -> List[str]:
    cases = []
    seen = set()
    pattern = re.compile(
        r"\b(?:cmocka_unit_test(?:_setup(?:_teardown)?|_teardown)?|UTEST)\s*\(\s*(?P<name>[A-Za-z_]\w*)",
        re.MULTILINE,
    )
    for match in pattern.finditer(source_text):
        name = match.group("name")
        if name not in seen:
            seen.add(name)
            cases.append(name)
    return cases

def discover_cmocka_cases(repo: Path) -> Dict[str, List[str]]:
    by_ctest: Dict[str, List[str]] = {}
    tests_dir = repo / "tests"
    if not tests_dir.exists():
        return by_ctest
    for path in sorted(tests_dir.rglob("*.c")):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if "cmocka_run_group_tests" not in text:
            continue
        cases = _parse_cmocka_cases(text)
        if not cases:
            continue
        for name in _candidate_ctest_names_for_source(path):
            by_ctest.setdefault(name, [])
            by_ctest[name].extend(cases)
    for name in list(by_ctest.keys()):
        by_ctest[name] = sorted(dict.fromkeys(by_ctest[name]))
    return by_ctest

def discover_test_specs(repo: Path, build_dir: Path, ctest_names: List[str]) -> List[TestSpec]:
    entries = {entry.name: entry for entry in list_ctest_entries(build_dir)}
    cmocka_cases = discover_cmocka_cases(repo)
    specs: List[TestSpec] = []
    for ctest_name in ctest_names:
        entry = entries.get(ctest_name) or CTestEntry(name=ctest_name)
        cases = cmocka_cases.get(ctest_name, [])
        if not cases:
            specs.append(TestSpec(
                test_id=ctest_name,
                ctest_name=ctest_name,
                command=entry.command,
                environment=entry.environment,
            ))
            continue
        for case_name in cases:
            specs.append(TestSpec(
                test_id=f"{ctest_name}::{case_name}",
                ctest_name=ctest_name,
                case_name=case_name,
                command=entry.command,
                environment=entry.environment,
            ))
    return specs

def select_tests(all_tests: List[str], bug: BugEntry) -> List[str]:
    """Chọn test dựa trên test_flags từ bugs_list_new.json."""
    if not bug.test_flags: return all_tests
    selected = []
    for tf in bug.test_flags:
        # test_flags có thể chứa "|" (e.g. "utest_xpath|utest_xpath_valgrind")
        patterns = [p.strip() for p in tf.split("|") if p.strip()]
        for pat in patterns:
            for t in all_tests:
                if t == pat or re.match(pat.replace("*", ".*"), t):
                    if t not in selected: selected.append(t)
    # Luôn giữ tất cả test nếu test_flags không match được gì
    return selected if selected else all_tests

def select_test_specs(all_specs: List[TestSpec], bug: BugEntry) -> List[TestSpec]:
    if not bug.test_flags:
        return all_specs
    selected: List[TestSpec] = []
    seen = set()
    for tf in bug.test_flags:
        patterns = [p.strip() for p in tf.split("|") if p.strip()]
        for pat in patterns:
            regex = pat.replace("*", ".*")
            for spec in all_specs:
                candidates = [spec.test_id, spec.ctest_name]
                if spec.case_name:
                    candidates.append(spec.case_name)
                if any(candidate == pat or candidate.startswith(f"{pat}::") or re.match(regex, candidate) for candidate in candidates):
                    if spec.test_id not in seen:
                        selected.append(spec)
                        seen.add(spec.test_id)
    return selected if selected else all_specs

def clear_gcda(path: Path):
    for g in path.rglob("*.gcda"):
        try: g.unlink()
        except OSError: pass

def run_one_ctest(build_dir: Path, test_name: str, timeout=120) -> Tuple[bool, str]:
    """Chạy 1 ctest, trả (passed, output)."""
    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")
    rc, out, err = run(
        ["ctest", "--test-dir", str(build_dir), "-R", f"^{re.escape(test_name)}$",
         "-V", "--timeout", str(timeout)],
        cwd=build_dir, env=env, timeout=timeout + 30,
    )
    combined = (out or "") + ("\n" + err if err else "")
    passed = (rc == 0) and ("100% tests passed" in combined or "Test passed" in combined
                            or "1 test passed" in combined)
    if rc == 0 and "0 tests" not in combined: passed = True
    if "***Failed" in combined or "***Timeout" in combined: passed = False
    return passed, combined

def run_one_test_spec(build_dir: Path, spec: TestSpec, timeout=120) -> Tuple[bool, str]:
    if not spec.case_name:
        return run_one_ctest(build_dir, spec.ctest_name, timeout=timeout)

    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")
    env.update(spec.environment)
    env[BUILD_META_CMOCKA_FILTER_ENV] = spec.case_name
    command = spec.command or [str(build_dir / "tests" / spec.ctest_name)]
    rc, out, err = run(command, cwd=build_dir, env=env, timeout=timeout + 30)
    combined = (out or "") + ("\n" + err if err else "")
    passed = rc == 0
    if "[  FAILED  ]" in combined or "FAILED TEST(S)" in combined or "***Failed" in combined or "***Timeout" in combined:
        passed = False
    if re.search(r"Running\s+0\s+test|0 test\(s\) run", combined):
        passed = False
    return passed, combined

# ── Coverage collection ──
def collect_coverage(repo: Path, build_dir: Path, *, verbose: bool = False) -> Tuple[Dict[str, List[str]], str]:
    if not shutil.which("gcov"):
        return {}, "gcov_not_found"
    gcda_files = list(build_dir.rglob("*.gcda"))
    if not gcda_files:
        if verbose: log("  [cov] no *.gcda found (test may have crashed)")
        return {}, "no_gcda_files"
    # Clean old gcov files
    for g in build_dir.rglob("*.gcov"):
        try: g.unlink()
        except OSError: pass

    covered: Dict[str, List[str]] = {}
    for gcda in gcda_files:
        gcda_dir = gcda.parent
        gcda_name = gcda.name

        # Chạy gcov trực tiếp trên file .gcda (CMake naming).
        # Sau đó đọc các file .gcov vì test cũ có thể #include trực tiếp
        # src/*.c, làm nhiều Source xuất hiện từ một test_*.c.gcda.
        rc, out, err = run(
            ["gcov", "-f", "-b", "-c", gcda_name],
            cwd=gcda_dir, timeout=30,
        )
        if rc != 0 and verbose:
            log(f"  [cov] gcov returned {rc} for {gcda_name}: {err[-200:]}")

        parsed_any_file = False
        for gcov_file in gcda_dir.glob("*.gcov"):
            try:
                txt = gcov_file.read_text(errors="replace")
            except OSError:
                continue
            source = _source_from_gcov_text(txt)
            funcs = _parse_gcov_functions(txt)
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
            parsed_any_file = True

        if not parsed_any_file and verbose and out:
            log(f"  [cov] no parseable .gcov files for {gcda_name}")

    # Clean gcov files
    for g in build_dir.rglob("*.gcov"):
        try: g.unlink()
        except OSError: pass
    for rel in list(covered.keys()):
        covered[rel] = sorted(set(covered[rel]))
    if verbose:
        log(f"  [cov] found {sum(len(v) for v in covered.values())} functions in {len(covered)} files")
    if not covered:
        return {}, f"no_functions_parsed_from_{len(gcda_files)}_gcda_files"
    return covered, ""

def _source_from_gcov_text(text: str) -> str:
    for line in text.splitlines()[:20]:
        marker = "Source:"
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return ""

def filter_production_coverage(cov_map):
    """Giữ coverage của production source, bỏ test/harness khỏi metadata."""
    return {
        rel: funcs
        for rel, funcs in cov_map.items()
        if _is_production_source(rel)
    }

def _is_production_source(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/").lstrip("./")
    if not rel.startswith("src/"):
        return False
    return rel.endswith((".c", ".cc", ".cpp", ".cxx"))

def coverage_to_qualified(cov_map):
    return [f"{os.path.basename(f)}:{fn}" for f, funcs in cov_map.items() for fn in funcs]

# ── Ground truth ──
def extract_ground_truth(bug, repo):
    if not bug.sha_before: return []
    rc, diff, _ = run(["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files],
                      cwd=repo)
    if rc != 0 or not diff: return []
    funcs, kw = set(), {"if","for","while","switch","return","sizeof","case","do"}
    source_cache: Dict[str, str] = {}
    for line in diff.splitlines():
        if line.startswith("@@"):
            line_func = ""
            tail = line.split("@@", 2)[-1].strip()
            m = re.match(r".*?\b([A-Za-z_]\w*)\s*\(", tail)
            if m and m.group(1) not in kw:
                line_func = m.group(1)
            if line_func:
                funcs.add(line_func)
                continue

            hunk = re.match(r"@@\s+-\d+(?:,\d+)?\s+\+(?P<start>\d+)(?:,(?P<count>\d+))?", line)
            if not hunk:
                continue
            start_line = int(hunk.group("start"))
            line_count = int(hunk.group("count") or "1")
            for src_file in bug.src_files:
                if src_file not in source_cache:
                    rc_show, text, _ = run(["git", "show", f"{bug.sha_after}:{src_file}"], cwd=repo)
                    source_cache[src_file] = text if rc_show == 0 else ""
                fn = ""
                for candidate_line in range(start_line, start_line + max(line_count, 1)):
                    fn = _find_enclosing_c_function(source_cache[src_file], candidate_line)
                    if fn:
                        break
                if fn:
                    funcs.add(fn)
    return sorted(funcs)

def _find_enclosing_c_function(source: str, line_number: int) -> str:
    if not source or line_number <= 0:
        return ""
    line_offsets = [0]
    for m in re.finditer(r"\n", source):
        line_offsets.append(m.end())
    if line_number > len(line_offsets):
        target = len(source)
    else:
        target = line_offsets[line_number - 1]

    kw = {"if", "for", "while", "switch", "return", "sizeof", "case", "do"}
    func_re = re.compile(
        r"(?m)^\s*[A-Za-z_][\w\s\*\(\),]*?\b(?P<name>[A-Za-z_]\w*)\s*"
        r"\([^;{}]*\)\s*\{",
        re.MULTILINE,
    )
    best = ""
    for match in func_re.finditer(source):
        name = match.group("name")
        if name in kw:
            continue
        open_brace = source.find("{", match.start(), match.end())
        if open_brace < 0 or open_brace > target:
            continue
        close_brace = _matching_brace_offset(source, open_brace)
        if close_brace >= target:
            best = name
    return best

def _matching_brace_offset(source: str, open_offset: int) -> int:
    depth = 0
    i = open_offset
    n = len(source)
    state = "code"
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"; i += 2; continue
            if ch == "/" and nxt == "*":
                state = "block_comment"; i += 2; continue
            if ch == '"':
                state = "string"; i += 1; continue
            if ch == "'":
                state = "char"; i += 1; continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"; i += 2; continue
        elif state in {"string", "char"}:
            if ch == "\\":
                i += 2; continue
            if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
                state = "code"
        i += 1
    return n

# ── run_one_test.sh helper ──
RUN_ONE_TEST_SH = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # Auto-generated by build_meta_libyang.py
    set -uo pipefail
    HERE=$(cd "$(dirname "$0")" && pwd)
    TEST_ID="${1:?Usage: $0 <test_id>}"
    BUILD_DIR="$HERE/__BUILD_DIR_NAME__"
    if [[ "$TEST_ID" == *"::"* ]]; then
      CTEST_NAME="${TEST_ID%%::*}"
      CASE_NAME="${TEST_ID#*::}"
      OUTPUT=$(BUILD_META_CMOCKA_TEST_FILTER="$CASE_NAME" "$BUILD_DIR/tests/$CTEST_NAME" 2>&1)
    else
      OUTPUT=$(ctest --test-dir "$BUILD_DIR" -R "^${TEST_ID}$" -V --timeout 120 2>&1)
    fi
    STATUS=$?
    echo "$OUTPUT"
    if [[ $STATUS -ne 0 ]] || echo "$OUTPUT" | grep -Eq '\*\*\*Failed|\[  FAILED  \]|FAILED TEST\(S\)|Running 0 test|0 test\(s\) run'; then
      exit 1
    fi
    exit 0
""").lstrip()

def write_run_one_test(repo):
    p = repo / "run_one_test.sh"
    p.write_text(RUN_ONE_TEST_SH.replace("__BUILD_DIR_NAME__", BUILD_DIR_NAME)); p.chmod(0o755)
    return p

# ── Write metadata ──
def _write_meta(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))

def _test_to_dict(r):
    item = {"test_id": r.test_id, "outcome": r.outcome, "outcome_fixed": r.outcome_fixed,
            "fail_reason": r.fail_reason, "actual_output": r.actual_output,
            "covered_functions": r.covered_functions}
    if r.coverage_error:
        item["coverage_error"] = r.coverage_error
    return item

def _existing_metadata_is_current(path: Path, *, require_coverage: bool) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    phase_info = data.get("phase_info") or {}
    tests = data.get("tests") or []
    if not tests or data.get("build_error"):
        return False
    if phase_info.get("test_granularity") != "cmocka_case":
        return False
    if int(phase_info.get("coverage_parser_version") or 0) != COVERAGE_PARSER_VERSION:
        return False
    if require_coverage and phase_info.get("skip_coverage"):
        return False
    return True

# ── Main processing ──
def process_bug(bug, out_root, metadata_dir, raw_dir, *, jobs, test_timeout,
                skip_if_exists, clone_if_missing, skip_coverage, dual_run, run_all_tests=False):
    safe_name = f"{bug.safe_bug_id}_meta.json"
    out_path = metadata_dir / safe_name
    raw_out_path = raw_dir / safe_name

    if skip_if_exists and out_path.exists():
        if _existing_metadata_is_current(out_path, require_coverage=not skip_coverage):
            log(f"[skip] {bug.bug_id}"); return out_path
        log(f"[rerun] {bug.bug_id} existing metadata is suite-level/old")

    log(f"=== {bug.bug_id} | {bug.type_name or ''} ===")
    try: repo = ensure_repo(out_root, bug.sha_after, clone_if_missing=clone_if_missing)
    except Exception as e: log(f"  [error] repo: {e}"); return None

    try: checkout_buggy(repo, bug)
    except Exception as e: log(f"  [error] checkout: {e}"); return None

    build_dir = repo / BUILD_DIR_NAME
    compile_cmd = (
        f"cd {shlex.quote(str(repo))} && cmake -G Ninja -S . -B {BUILD_DIR_NAME} "
        f"-DCMAKE_BUILD_TYPE=Debug -DCMAKE_C_FLAGS='{COV_CFLAGS}' "
        f"-DENABLE_TESTS=ON -DENABLE_BUILD_TESTS=ON -DENABLE_VALGRIND_TESTS=OFF "
        f"-DENABLE_COVERAGE=OFF -DENABLE_TOOLS=OFF && "
        f"ninja -C {BUILD_DIR_NAME} -j{jobs}"
    )
    test_cmd_template = f"bash {shlex.quote(str(repo / 'run_one_test.sh'))} {{test_id}}"
    source_file = str(repo / bug.src_files[0]) if bug.src_files else ""

    if not compile_with_coverage(repo, jobs=jobs):
        rec = {"bug_id": bug.bug_id, "dataset_name": "defects4c", "language": "C",
               "project": PROJECT_NAME, "commit_after": bug.sha_after,
               "commit_before": bug.sha_before, "source_file": source_file,
               "compile_cmd": compile_cmd, "tests": [], "build_error": "compile_failed",
               "phase_info": {"errors": ["buggy_compile_failed"]}}
        _write_meta(raw_out_path, rec); _write_meta(out_path, rec)
        return out_path

    write_run_one_test(repo)
    all_ctest_tests = list_ctest_tests(build_dir)
    all_specs = discover_test_specs(repo, build_dir, all_ctest_tests)
    selected = all_specs if run_all_tests else select_test_specs(all_specs, bug)
    selected_ctest_tests = sorted({spec.ctest_name for spec in selected})
    log(f"  [tests] ctest={len(all_ctest_tests)}, cases={len(all_specs)}, selected_cases={len(selected)}")
    phase_info = {
        "run_all_tests": run_all_tests,
        "dual_run": dual_run,
        "skip_coverage": skip_coverage,
        "test_granularity": "cmocka_case",
        "coverage_parser_version": COVERAGE_PARSER_VERSION,
        "all_ctest_tests": len(all_ctest_tests),
        "selected_ctest_tests": len(selected_ctest_tests),
        "all_tests": len(all_specs),
        "selected_tests": len(selected),
        "errors": [],
    }
    if not selected:
        phase_info["errors"].append("no_tests_selected")

    if dual_run:
        # Phase A: buggy outcomes
        results_a = []
        for idx, spec in enumerate(selected, 1):
            passed, output = run_one_test_spec(build_dir, spec, timeout=test_timeout)
            results_a.append(TestResult(
                test_id=spec.test_id, outcome="PASS" if passed else "FAIL",
                fail_reason="" if passed else output[-3000:],
                actual_output="" if passed else output[-3000:],
            ))
            if idx % 25 == 0 or idx == len(selected):
                nf = sum(1 for r in results_a if r.outcome == "FAIL")
                log(f"  [phaseA-buggy] {idx}/{len(selected)} (fail={nf})")

        # Phase A-fixed: fixed outcomes
        fixed_map = {}
        try:
            checkout_fixed(repo, bug)
            if compile_with_coverage(repo, jobs=jobs):
                fixed_entries = {entry.name: entry for entry in list_ctest_entries(build_dir)}
                fixed_selected = [
                    TestSpec(
                        test_id=spec.test_id,
                        ctest_name=spec.ctest_name,
                        case_name=spec.case_name,
                        command=(fixed_entries.get(spec.ctest_name) or CTestEntry(spec.ctest_name)).command,
                        environment=(fixed_entries.get(spec.ctest_name) or CTestEntry(spec.ctest_name)).environment,
                    )
                    for spec in selected
                ]
                for idx, spec in enumerate(fixed_selected, 1):
                    passed, _ = run_one_test_spec(build_dir, spec, timeout=test_timeout)
                    fixed_map[spec.test_id] = "PASS" if passed else "FAIL"
                    if idx % 25 == 0 or idx == len(selected):
                        nf = sum(1 for v in fixed_map.values() if v == 'FAIL')
                        log(f"  [phaseA-fixed] {idx}/{len(selected)} (fail={nf})")
                log(f"  [phaseA-fixed] done (fail={sum(1 for v in fixed_map.values() if v=='FAIL')})")
            else:
                phase_info["errors"].append("fixed_compile_failed")
        except Exception as e:
            phase_info["errors"].append(f"fixed_phase_exception: {e}")
            log(f"  [error] fixed phase failed: {e}")

        # Phase B: coverage on buggy — chạy TẤT CẢ selected tests
        # (không chỉ fail tests, vì test crash/segfault sẽ không flush gcda)
        raw_cov_map = {}
        meta_cov_map = {}
        cov_error_map = {}
        if not skip_coverage:
            try:
                checkout_buggy(repo, bug)
                if compile_with_coverage(repo, jobs=jobs):
                    cov_entries = {entry.name: entry for entry in list_ctest_entries(build_dir)}
                    phase_b_tests = [
                        TestSpec(
                            test_id=spec.test_id,
                            ctest_name=spec.ctest_name,
                            case_name=spec.case_name,
                            command=(cov_entries.get(spec.ctest_name) or CTestEntry(spec.ctest_name)).command,
                            environment=(cov_entries.get(spec.ctest_name) or CTestEntry(spec.ctest_name)).environment,
                        )
                        for spec in selected
                    ]
                    for idx, spec in enumerate(phase_b_tests, 1):
                        clear_gcda(build_dir)
                        run_one_test_spec(build_dir, spec, timeout=test_timeout)
                        cov, cov_error = collect_coverage(repo, build_dir, verbose=(idx <= 2))
                        raw_cov_map[spec.test_id] = coverage_to_qualified(cov)
                        meta_cov_map[spec.test_id] = coverage_to_qualified(filter_production_coverage(cov))
                        cov_error_map[spec.test_id] = cov_error
                        if idx % 25 == 0 or idx == len(phase_b_tests):
                            n_raw = sum(1 for v in raw_cov_map.values() if v)
                            n_meta = sum(1 for v in meta_cov_map.values() if v)
                            log(f"  [phaseB] {idx}/{len(phase_b_tests)} (raw_cov={n_raw}, meta_cov={n_meta})")
                else:
                    phase_info["errors"].append("coverage_compile_failed")
                    cov_error_map.update({spec.test_id: "coverage_compile_failed" for spec in selected})
            except Exception as e:
                phase_info["errors"].append(f"coverage_phase_exception: {e}")
                cov_error_map.update({spec.test_id: f"coverage_phase_exception: {e}" for spec in selected})
                log(f"  [error] phaseB failed: {e}")

        raw_results = []
        metadata_results = []
        for r in results_a:
            common = {
                "test_id": r.test_id,
                "outcome": r.outcome,
                "outcome_fixed": fixed_map.get(r.test_id, "NOT_RUN"),
                "fail_reason": r.fail_reason,
                "actual_output": r.actual_output,
                "coverage_error": cov_error_map.get(r.test_id, ""),
            }
            raw_results.append(TestResult(
                **common,
                covered_functions=raw_cov_map.get(r.test_id, []),
            ))
            metadata_results.append(TestResult(
                **common,
                covered_functions=meta_cov_map.get(r.test_id, []),
            ))
        results = metadata_results
        raw_results_for_record = raw_results
    else:
        # Single-phase
        raw_results_for_record = []
        results = []
        for idx, spec in enumerate(selected, 1):
            clear_gcda(build_dir)
            passed, output = run_one_test_spec(build_dir, spec, timeout=test_timeout)
            cov, cov_error = ({}, "") if skip_coverage else collect_coverage(repo, build_dir)
            raw_results_for_record.append(TestResult(
                test_id=spec.test_id, outcome="PASS" if passed else "FAIL",
                outcome_fixed="NOT_RUN",
                fail_reason="" if passed else output[-3000:],
                actual_output="" if passed else output[-3000:],
                covered_functions=coverage_to_qualified(cov),
                coverage_error=cov_error,
            ))
            results.append(TestResult(
                test_id=spec.test_id, outcome="PASS" if passed else "FAIL",
                outcome_fixed="NOT_RUN",
                fail_reason="" if passed else output[-3000:],
                actual_output="" if passed else output[-3000:],
                covered_functions=coverage_to_qualified(filter_production_coverage(cov)),
                coverage_error=cov_error,
            ))
            if idx % 5 == 0 or idx == len(selected):
                nf = sum(1 for r in results if r.outcome == "FAIL")
                log(f"  [tests] {idx}/{len(selected)} (fail={nf})")

    gt_funcs = extract_ground_truth(bug, repo)
    raw_phase_info = {
        **phase_info,
        "coverage_scope": "full_gcov",
    }
    metadata_phase_info = {
        **phase_info,
        "coverage_scope": "production_source_only",
        "coverage_filter": "keep repo-relative C/C++ files under src/",
    }
    raw_record = {
        "bug_id": bug.bug_id, "dataset_name": "defects4c", "language": "C",
        "project": PROJECT_NAME, "commit_after": bug.sha_after,
        "commit_before": bug.sha_before, "source_file": source_file,
        "source_basename": os.path.basename(source_file) if source_file else "",
        "compile_cmd": compile_cmd, "test_cmd_template": test_cmd_template,
        "type_name": bug.type_name, "ground_truth_functions": gt_funcs,
        "ground_truth": [f"{source_file}::{fn}" for fn in gt_funcs] if source_file else [],
        "phase_info": raw_phase_info,
        "tests": [_test_to_dict(r) for r in raw_results_for_record],
    }
    metadata_record = {
        **raw_record,
        "phase_info": metadata_phase_info,
        "tests": [_test_to_dict(r) for r in results],
    }
    _write_meta(raw_out_path, raw_record); log(f"  [ok] wrote raw {raw_out_path}")
    _write_meta(out_path, metadata_record); log(f"  [ok] wrote {out_path}")
    return out_path

# ── CLI ──
def main(argv=None):
    DEFAULT_OUT = _detect("out", f"out_tmp_dirs/{PROJECT_NAME}", f"/out/{PROJECT_NAME}")
    # Output lưu tại out_tmp_dirs/unified_debugging/libyang/ (trong container: /out/unified_debugging/libyang/)
    DEFAULT_META = _detect("meta",
                           "out_tmp_dirs/unified_debugging/libyang/metadata",
                           "/out/unified_debugging/libyang/metadata")
    DEFAULT_RAW = _detect("raw",
                          "out_tmp_dirs/unified_debugging/libyang/raw",
                          "/out/unified_debugging/libyang/raw")
    ap = argparse.ArgumentParser(description="Sinh metadata Unified-Debugging cho libyang.")
    ap.add_argument("--sha", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--metadata-dir", type=Path, default=DEFAULT_META)
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    ap.add_argument("--jobs", type=int, default=max(os.cpu_count() or 2, 2) - 1)
    ap.add_argument("--test-timeout", type=int, default=120)
    ap.add_argument("--skip-coverage", action="store_true")
    ap.add_argument(
        "--dual-run", dest="dual_run", action="store_true", default=True,
        help="Run buggy+fixed outcomes and buggy coverage (default).",
    )
    ap.add_argument(
        "--single-run", dest="dual_run", action="store_false",
        help="Only run the buggy version; outcome_fixed will be NOT_RUN.",
    )
    ap.add_argument("--skip-if-exists", action="store_true")
    ap.add_argument("--clone", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument(
        "--run-all-tests", dest="run_all_tests", action="store_true", default=True,
        help="Run all CTest tests for each bug (default).",
    )
    ap.add_argument(
        "--trigger-tests-only", dest="run_all_tests", action="store_false",
        help="Run only tests matched by bugs_list_new.json c_compile.test_flags.",
    )
    args = ap.parse_args(argv)

    bugs = load_bugs()
    if args.sha:
        wanted = [s for s in args.sha if s]
        bugs = [b for b in bugs
                if b.sha_after in wanted or b.sha_after.startswith(tuple(wanted))]
    if args.limit: bugs = bugs[:args.limit]
    if args.list:
        for b in bugs: print(f"{b.bug_id}\t{b.type_name or '-'}\t{','.join(b.src_files)}")
        return 0
    if not bugs: log("Không có bug nào khớp."); return 1

    log(f"Sẽ xử lý {len(bugs)} bug.")
    args.metadata_dir.mkdir(parents=True, exist_ok=True)
    args.raw_dir.mkdir(parents=True, exist_ok=True)

    fail_count = 0
    lock_fp = None
    try:
        lock_fp = _acquire_single_run_lock(_lock_path_for_run(args.out_root, args.metadata_dir))
    except RuntimeError as exc:
        log(f"[error] {exc}")
        return 3

    try:
        for bug in bugs:
            try:
                out = process_bug(bug, args.out_root, args.metadata_dir, args.raw_dir,
                                  jobs=args.jobs, test_timeout=args.test_timeout,
                                  skip_if_exists=args.skip_if_exists,
                                  clone_if_missing=args.clone,
                                  skip_coverage=args.skip_coverage, dual_run=args.dual_run,
                                  run_all_tests=args.run_all_tests)
                if out is None: fail_count += 1
            except KeyboardInterrupt: log("Interrupted."); return 130
            except Exception as e: fail_count += 1; log(f"  [exception] {bug.bug_id}: {e}")
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
