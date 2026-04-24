#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project DaveGamble___cJSON.

The flow mirrors the tcpdump metadata builder:
  - bug_id comes from bugs_list_new.json -> type.id
  - raw and metadata contain the same record
  - phase A runs the buggy test executable and, with --dual-run, the fixed one
    to fill outcome_fixed
  - phase B collects gcov coverage from the buggy version only
"""

from __future__ import annotations

import argparse
from collections import Counter
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"

REMOTE_URL = "https://github.com/DaveGamble/cJSON.git"
BUILD_DIR_NAME = "build_meta_cjson"
DEFAULT_TEST_TIMEOUT = 120

COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage"
ASAN_CFLAGS = "-fsanitize=address -fno-omit-frame-pointer"
ASAN_LDFLAGS = "-fsanitize=address"

_GCOV_FUNC_RE = re.compile(
    r"^function\s+(?P<name>\S+)\s+called\s+(?P<calls>\d+)\s+returned",
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
    if _safe_exists(host_default):
        return host_default
    container_default = Path("/out") / PROJECT_NAME
    if _safe_exists(container_default):
        return container_default
    return host_default


def _detect_default_metadata_dir() -> Path:
    host_default = (
        DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "cjson" / "metadata"
    )
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/cjson/metadata")
    return host_default


def _detect_default_raw_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "cjson" / "raw"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/cjson/raw")
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
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.seek(0)
        owner = fp.read().strip()
        owner_msg = f" owner={owner}" if owner else ""
        fp.close()
        raise RuntimeError(
            f"Đang có process khác chạy (lock: {lock_path}){owner_msg}."
        )
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp


def _lock_path_for_metadata_dir(metadata_dir: Path) -> Path:
    key = hashlib.md5(str(metadata_dir).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_cjson.{key}.lock"


def load_bugs() -> List[BugEntry]:
    if not BUGS_JSON.exists():
        raise FileNotFoundError(f"Không tìm thấy {BUGS_JSON}")
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for item in data:
        sha_after = item.get("commit_after") or ""
        sha_before = item.get("commit_before") or ""
        files = item.get("files") or {}
        type_info = item.get("type") or {}
        bug = BugEntry(
            sha_after=sha_after,
            sha_before=sha_before,
            src_files=list(files.get("src") or []),
            test_files=list(files.get("test") or []),
            cve_name=type_info.get("name") or type_info.get("id"),
            type_id=type_info.get("id") or sha_after,
            raw=item,
        )
        bugs.append(bug)

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
            raise FileExistsError(
                f"Repo theo bug_id tồn tại nhưng không phải git repo: {repo_dir}"
            )
        log(f"  [repo] rename legacy repo {legacy_repo_dir.name} -> {repo_dir.name}")
        legacy_repo_dir.rename(repo_dir)
        return repo_dir
    if not clone:
        raise FileNotFoundError(
            f"Không thấy repo {repo_dir}. Chạy --prepare-repos --clone hoặc thêm --clone."
        )
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
    checkout_commit(repo_dir, bug.sha_before)


def checkout_fixed(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)


def _build_env(*, asan: bool, coverage: bool) -> dict:
    env = os.environ.copy()
    cflags: List[str] = []
    ldflags: List[str] = []
    if coverage:
        cflags.append(COV_CFLAGS)
        ldflags.append(COV_LDFLAGS)
    if asan:
        cflags.append(ASAN_CFLAGS)
        ldflags.append(ASAN_LDFLAGS)
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")
    if cflags:
        env["CFLAGS"] = " ".join(cflags)
    if ldflags:
        env["LDFLAGS"] = " ".join(ldflags)
    return env


def _cmake_configure_cmd(repo_dir: Path, build_dir: Path, *, asan: bool, coverage: bool) -> List[str]:
    cflags = []
    ldflags = []
    if coverage:
        cflags.append(COV_CFLAGS)
        ldflags.append(COV_LDFLAGS)
    if asan:
        cflags.append(ASAN_CFLAGS)
        ldflags.append(ASAN_LDFLAGS)

    cmd = ["cmake"]
    if which("ninja"):
        cmd.extend(["-G", "Ninja"])
    cmd.extend([
        "-S", str(repo_dir),
        "-B", str(build_dir),
        "-DENABLE_CJSON_UTILS=On",
        "-DENABLE_VALGRIND=OFF",
        "-DENABLE_SAFE_STACK=OFF",
        "-DENABLE_SANITIZERS=OFF",
        "-DENABLE_CJSON_TEST=On",
        f"-DCMAKE_C_FLAGS={' '.join(cflags)}",
        f"-DCMAKE_EXE_LINKER_FLAGS={' '.join(ldflags)}",
    ])
    return cmd


def compile_cjson(repo_dir: Path, *, jobs: int, asan: bool, coverage: bool) -> bool:
    if not which("cmake"):
        log("  [build] cmake không có trong PATH")
        return False
    build_dir = repo_dir / BUILD_DIR_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir)
    env = _build_env(asan=asan, coverage=coverage)
    rc, out, err = run(
        _cmake_configure_cmd(repo_dir, build_dir, asan=asan, coverage=coverage),
        cwd=repo_dir, env=env, capture=True, timeout=120,
    )
    if rc != 0:
        log(f"  [build] cmake configure failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    rc, out, err = run(
        ["cmake", "--build", str(build_dir), "--parallel", str(jobs)],
        cwd=repo_dir, env=env, capture=True, timeout=180,
    )
    if rc != 0:
        log(f"  [build] cmake build failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    return True


def _test_id_from_source(test_file: str) -> str:
    base = Path(test_file).name
    if base.endswith(".c"):
        base = base[:-2]
    return base


def discover_tests(repo_dir: Path, bug: BugEntry) -> List[TestEntry]:
    build_dir = repo_dir / BUILD_DIR_NAME
    tests: List[TestEntry] = []
    for test_file in bug.test_files:
        if test_file.endswith(".c"):
            test_id = _test_id_from_source(test_file)
            exe_rel = f"{BUILD_DIR_NAME}/tests/{test_id}"
            tests.append(TestEntry(
                test_id=test_id,
                executable_relpath=exe_rel,
                source_relpath=test_file,
            ))

    if not tests:
        misc = build_dir / "tests" / "misc_tests"
        if misc.exists():
            tests.append(TestEntry("misc_tests", f"{BUILD_DIR_NAME}/tests/misc_tests"))

    existing: List[TestEntry] = []
    for te in tests:
        if (repo_dir / te.executable_relpath).exists():
            existing.append(te)
        else:
            log(f"  [tests] bỏ qua {te.test_id}: không thấy {te.executable_relpath}")
    return existing


def clear_gcda(repo_dir: Path) -> None:
    for path in repo_dir.rglob("*.gcda"):
        try:
            path.unlink()
        except OSError:
            pass


def run_one_test(repo_dir: Path, te: TestEntry, *, timeout: int) -> Tuple[bool, str, str]:
    exe = repo_dir / te.executable_relpath
    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")
    rc, out, err = run([str(exe)], cwd=repo_dir, env=env, timeout=timeout, capture=True)
    combined = out + err
    passed = rc == 0
    reason = "" if passed else f"exit_code={rc}"
    return passed, combined, reason


def run_tests(
    repo_dir: Path,
    entries: List[TestEntry],
    *,
    collect_cov: bool,
    test_timeout: int,
    phase_label: str,
) -> Tuple[List[TestResult], int]:
    results: List[TestResult] = []
    n_with_cov = 0
    for idx, te in enumerate(entries, 1):
        clear_gcda(repo_dir)
        passed, combined_output, reason = run_one_test(repo_dir, te, timeout=test_timeout)
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
        out.extend(repo_dir.rglob(name))
    return list(dict.fromkeys(out))


def collect_coverage(repo_dir: Path) -> Dict[str, List[str]]:
    if not which("gcov"):
        return {}
    gcda_files = list((repo_dir / BUILD_DIR_NAME).rglob("*.gcda"))
    covered: Dict[str, List[str]] = {}

    for gcov_file in repo_dir.rglob("*.gcov"):
        try:
            gcov_file.unlink()
        except OSError:
            pass

    for gcda in gcda_files:
        funcs_called: List[str] = []
        src_path: Optional[Path] = None
        for candidate in _source_candidates(repo_dir, gcda):
            rc, out, _ = run(
                ["gcov", "-f", "-b", "-c", "-o", str(gcda.parent), str(candidate)],
                cwd=gcda.parent, capture=True, timeout=30,
            )
            if rc != 0:
                continue
            src_path = candidate
            for m in _GCOV_FUNC_RE.finditer(out):
                try:
                    if int(m.group("calls")) > 0:
                        funcs_called.append(m.group("name"))
                except ValueError:
                    pass
            if funcs_called:
                break

        if not funcs_called or src_path is None:
            continue
        try:
            rel_src = src_path.relative_to(repo_dir).as_posix()
        except ValueError:
            rel_src = src_path.name
        if rel_src.startswith(BUILD_DIR_NAME + "/"):
            continue
        covered[rel_src] = sorted(set(funcs_called))

    for gcov_file in repo_dir.rglob("*.gcov"):
        try:
            gcov_file.unlink()
        except OSError:
            pass
    return covered


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for fname, funcs in cov_map.items():
        base = os.path.basename(fname)
        for fn in funcs:
            out.append(f"{base}:{fn}")
    return sorted(set(out))


def write_run_one_test(repo_dir: Path, entries: List[TestEntry]) -> None:
    mapping = "\n".join(
        f"{shlex.quote(te.test_id)} {shlex.quote(te.executable_relpath)}"
        for te in entries
    )
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

        case "$TEST_ID" in
{_case_lines(entries)}
          *)
            echo "[run_one_test] unknown test_id '$TEST_ID'" >&2
            exit 2
            ;;
        esac

        if [[ ! -x "$ROOT/$EXE_REL" ]]; then
          BUILD_DIR="$ROOT/{BUILD_DIR_NAME}"
          cmake -S "$ROOT" -B "$BUILD_DIR" -DENABLE_CJSON_UTILS=On \\
            -DENABLE_VALGRIND=OFF -DENABLE_SAFE_STACK=OFF \\
            -DENABLE_SANITIZERS=OFF -DENABLE_CJSON_TEST=On
          cmake --build "$BUILD_DIR"
        fi

        exec "$ROOT/$EXE_REL"
    """), encoding="utf-8")
    script.chmod(0o755)
    (repo_dir / ".build_meta_cjson_tests").write_text(mapping + "\n", encoding="utf-8")


def _case_lines(entries: List[TestEntry]) -> str:
    lines: List[str] = []
    for te in entries:
        lines.append(f"          {shlex.quote(te.test_id)}) EXE_REL={shlex.quote(te.executable_relpath)} ;;")
    return "\n".join(lines)


def _extract_ground_truth_funcs(bug: BugEntry, repo_dir: Path) -> List[str]:
    if not bug.sha_before:
        return []
    rc, diff, _ = run(
        ["git", "diff", bug.sha_before, bug.sha_after, "--", *bug.src_files],
        cwd=repo_dir, capture=True,
    )
    if rc != 0 or not diff:
        return []
    funcs = set()
    c_keywords = {
        "if", "for", "while", "switch", "return", "sizeof", "case", "do",
    }
    for line in diff.splitlines():
        if line.startswith("@@"):
            tail = line.split("@@", 2)[-1].strip()
            m = re.match(r".*?\b([A-Za-z_]\w*)\s*\(", tail)
            if m:
                name = m.group(1)
                if name not in c_keywords:
                    funcs.add(name)
    return sorted(funcs)


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
        "tests": [],
        "build_error": error,
    }


def _compile_cmd_for_meta(repo_dir: Path, *, asan: bool, coverage: bool, jobs: int) -> str:
    build_dir = repo_dir / BUILD_DIR_NAME
    cfg = _cmake_configure_cmd(repo_dir, build_dir, asan=asan, coverage=coverage)
    return " ".join(shlex.quote(x) for x in cfg) + (
        f" && cmake --build {shlex.quote(str(build_dir))} --parallel {jobs}"
    )


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
) -> Optional[Path]:
    out_path = metadata_dir / f"{bug.safe_bug_id}_meta.json"
    raw_out_path = raw_dir / f"{bug.safe_bug_id}_meta.json"

    try:
        repo_dir = ensure_repo(bug, out_root, clone=clone)
    except Exception as exc:
        log(f"  [error] không tìm/clone được repo: {exc}")
        return None

    compile_cmd = _compile_cmd_for_meta(repo_dir, asan=asan or dual_run, coverage=True, jobs=jobs)

    try:
        checkout_buggy(repo_dir, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy lỗi: {exc}")
        return None

    if dual_run:
        phase_info: Dict[str, object] = {
            "mode": "dual",
            "phase_b_scope": gcov_scope,
            "test_policy": "buggy_tests_for_buggy_and_fixed",
        }
        log("  [phaseA-buggy] checkout+build ASAN")
        if not compile_cjson(repo_dir, jobs=jobs, asan=True, coverage=False):
            record = _empty_record(bug, repo_dir, compile_cmd, error="phaseA_buggy_compile_failed")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        entries = discover_tests(repo_dir, bug)
        write_run_one_test(repo_dir, entries)
        results_a, _ = run_tests(
            repo_dir, entries, collect_cov=False,
            test_timeout=test_timeout, phase_label="phaseA-buggy",
        )

        fixed_outcome_by_test: Dict[str, str] = {}
        try:
            checkout_fixed(repo_dir, bug)
            log("  [phaseA-fixed] checkout+build ASAN (outcome_fixed)")
            if compile_cjson(repo_dir, jobs=jobs, asan=True, coverage=False):
                fixed_results, _ = run_tests(
                    repo_dir, entries, collect_cov=False,
                    test_timeout=test_timeout, phase_label="phaseA-fixed",
                )
                fixed_outcome_by_test = {r.test_id: r.outcome for r in fixed_results}
                phase_info["phase_a_fixed_status"] = "ok"
                phase_info["phase_a_fixed_fail_count"] = sum(
                    1 for r in fixed_results if r.outcome == "FAIL"
                )
            else:
                phase_info["phase_a_fixed_status"] = "compile_failed"
                log("  [warn] phaseA-fixed build thất bại, outcome_fixed sẽ rỗng.")
        finally:
            checkout_buggy(repo_dir, bug)

        cov_by_test: Dict[str, List[str]] = {}
        if skip_coverage:
            phase_info["phase_b_scope"] = "skip_coverage"
        else:
            log("  [phaseB] checkout buggy + build GCOV")
            if compile_cjson(repo_dir, jobs=jobs, asan=False, coverage=True):
                results_b, n_cov = run_tests(
                    repo_dir, entries, collect_cov=True,
                    test_timeout=test_timeout, phase_label="phaseB",
                )
                cov_by_test = {r.test_id: r.covered_functions for r in results_b}
                phase_info["phase_b_with_coverage"] = n_cov
                phase_info["phase_b_test_count"] = len(entries)
            else:
                phase_info["phase_b_status"] = "compile_failed"

        results = [
            TestResult(
                test_id=r.test_id,
                outcome=r.outcome,
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
        phase_info = {"mode": "single", "test_policy": "buggy_tests"}
        if not compile_cjson(repo_dir, jobs=jobs, asan=asan, coverage=not skip_coverage):
            record = _empty_record(bug, repo_dir, compile_cmd, error="compile_failed")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        entries = discover_tests(repo_dir, bug)
        write_run_one_test(repo_dir, entries)
        results, n_cov = run_tests(
            repo_dir, entries, collect_cov=not skip_coverage,
            test_timeout=test_timeout, phase_label="single",
        )
        phase_info["with_coverage"] = n_cov

    write_run_one_test(repo_dir, discover_tests(repo_dir, bug))
    source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
    ground_truth_functions = _extract_ground_truth_funcs(bug, repo_dir)
    test_cmd_template = f"bash {shlex.quote(str(repo_dir / 'run_one_test.sh'))} {{test_id}}"
    base_record = {
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

    _write_meta(raw_out_path, base_record)
    log(f"  [ok] wrote raw {raw_out_path}")
    _write_meta(out_path, base_record)
    log(f"  [ok] wrote {out_path}")
    return out_path


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="build_meta_cjson.py",
        description="Sinh metadata Unified-Debugging cho DaveGamble/cJSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sha", action="append", default=[], help="Chỉ xử lý commit_after khớp prefix.")
    ap.add_argument("--only", dest="sha", action="append", help="Alias của --sha.")
    ap.add_argument("--limit", type=int, default=0, help="Giới hạn số bug xử lý (0 = tất cả).")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT, help=f"Thư mục chứa git_repo_dir_<bug_id> (mặc định: {DEFAULT_OUT_ROOT}).")
    ap.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR, help=f"Nơi ghi metadata (mặc định: {DEFAULT_METADATA_DIR}).")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Nơi ghi raw output (mặc định: {DEFAULT_RAW_DIR}).")
    ap.add_argument("--jobs", type=int, default=max(os.cpu_count() or 2, 2) - 1, help="Số job khi build.")
    ap.add_argument("--test-timeout", type=int, default=DEFAULT_TEST_TIMEOUT, help="Timeout mỗi test executable.")
    ap.add_argument("--skip-coverage", action="store_true", help="Không thu thập gcov.")
    ap.add_argument("--asan", action="store_true", help="Build + test với AddressSanitizer trong single mode.")
    ap.add_argument("--dual-run", action="store_true", help="Phase A buggy/fixed outcomes + phase B buggy coverage.")
    ap.add_argument(
        "--gcov-scope",
        choices=["all", "fail", "regression", "fail+regression"],
        default="all",
        help="Tương thích tcpdump; cJSON hiện chỉ có test executable nên luôn chạy all.",
    )
    ap.add_argument("--skip-if-exists", action="store_true", help="Bỏ qua bug đã có output metadata.")
    ap.add_argument("--clone", action="store_true", help="Tự clone cJSON từ GitHub nếu chưa có repo.")
    ap.add_argument("--prepare-repos", action="store_true", help="Chỉ chuẩn bị repo theo bug_id rồi thoát.")
    ap.add_argument("--list", action="store_true", help="Chỉ liệt kê bug rồi thoát.")
    return ap


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    bugs = load_bugs()
    if args.sha:
        wanted = [s for s in args.sha if s]
        bugs = [
            b for b in bugs
            if b.sha_after in wanted or b.sha_after.startswith(tuple(wanted))
        ]
    if args.limit:
        bugs = bugs[: args.limit]

    if args.list:
        for bug in bugs:
            print(f"{bug.sha_after}\t{bug.bug_id}\t{repo_dir_for_bug(args.out_root, bug).name}\t{','.join(bug.src_files)}")
        return 0

    if args.prepare_repos:
        ok = 0
        for idx, bug in enumerate(bugs, 1):
            try:
                repo_dir = ensure_repo(bug, args.out_root, clone=args.clone)
                checkout_buggy(repo_dir, bug)
                log(f"[{idx}/{len(bugs)}] prepared {bug.bug_id}: {repo_dir}")
                ok += 1
            except Exception as exc:
                log(f"[{idx}/{len(bugs)}] [error] prepare repo {bug.bug_id}: {exc}")
        log(f"Prepare repos xong: {ok}/{len(bugs)}")
        return 0 if ok == len(bugs) else 1

    log(f"Sẽ xử lý {len(bugs)} bug.")
    log(f"  metadata_dir = {args.metadata_dir}")
    log(f"  raw_dir      = {args.raw_dir}")
    log(f"  dual_run     = {args.dual_run}")

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
            if args.skip_if_exists and out_path.exists():
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
            )
            if result:
                ok += 1
    finally:
        if lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fp.close()

    log(f"Hoàn tất: {ok}/{len(bugs)} bug có output.")
    return 0 if ok == len(bugs) else 1


if __name__ == "__main__":
    sys.exit(main())
