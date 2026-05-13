#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project redis___hiredis.

The project uses one custom C test binary that prints numbered test outcomes
such as "#27 Multi-bulk ... PASSED". This builder parses those numbered
outcomes into individual metadata test entries.
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
SHORT_PROJECT = "hiredis"
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"

REMOTE_URL = "https://github.com/redis/hiredis.git"
BUILD_DIR_NAME = "build_meta_hiredis"
DEFAULT_TEST_TIMEOUT = 240

BASE_CFLAGS = "-g -O0 -Wno-error"
COV_FLAGS = "--coverage"
ASAN_FLAGS = "-fsanitize=address -fsanitize-recover=address -fno-omit-frame-pointer"
ASAN_LDFLAGS = "-fsanitize=address"

ASAN_ERROR_MARKERS = (
    "==ERROR",
    "AddressSanitizer:",
    "runtime error:",
    "Segmentation fault",
    "SIGSEGV",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TEST_BLOCK_RE = re.compile(r"(?ms)^#(?P<num>\d+)\s+(?P<body>.*?)(?=^#\d+\s+|\Z)")
_STATUS_RE = re.compile(r"\b(PASSED|FAILED|SKIPPED)\b")
_GCOV_FUNC_RE = re.compile(
    r"^function\s+(?P<name>.+?)\s+called\s+(?P<calls>\d+)\s+returned",
    re.MULTILINE,
)
_GCOV_FUNC_LINES_RE = re.compile(
    r"Function '(?P<name>[^']+)'\nLines executed:(?P<pct>[0-9.]+)%",
    re.MULTILINE,
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
    display_name: str
    ordinal: int
    source_relpath: str = "test.c"
    command: List[str] = field(default_factory=list)
    working_dir_relpath: str = BUILD_DIR_NAME


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


DEFAULT_OUT_ROOT = _detect_default_out_root()
DEFAULT_METADATA_DIR = _detect_default_metadata_dir()
DEFAULT_RAW_DIR = _detect_default_raw_dir()


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "item"


def _timeout_stream(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run(
    cmd,
    *,
    cwd=None,
    env=None,
    check=False,
    timeout=None,
    capture=True,
):
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
        err = f"TimeoutExpired: {exp}"
        out = _timeout_stream(exp.stdout)
        err_text = _timeout_stream(exp.stderr)
        if err_text:
            err = err + "\n" + err_text
        return 124, out, err
    except UnicodeDecodeError as exp:
        err = f"UnicodeDecodeError: {exp}"
        return 1, "", err

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
        raise RuntimeError(f"Another process is running (lock: {lock_path}){owner_msg}.")
    fp.seek(0)
    fp.truncate(0)
    fp.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fp.flush()
    return fp


def _lock_path_for_metadata_dir(metadata_dir: Path) -> Path:
    key = hashlib.md5(str(metadata_dir).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_hiredis.{key}.lock"


def load_bugs() -> List[BugEntry]:
    if not BUGS_JSON.exists():
        raise FileNotFoundError(f"Cannot find {BUGS_JSON}")
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for item in data:
        typ = item.get("type") or {}
        files = item.get("files") or {}
        sha_after = item.get("commit_after") or ""
        sha_before = item.get("commit_before") or ""
        bug = BugEntry(
            sha_after=sha_after,
            sha_before=sha_before,
            src_files=list(files.get("src") or []),
            test_files=list(files.get("test") or []),
            cve_name=typ.get("name") or typ.get("id"),
            type_id=typ.get("id") or sha_after,
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
        run(["git", "fetch", "origin", bug.sha_before, bug.sha_after], cwd=repo_dir)
        return repo_dir
    if legacy_repo_dir.exists() and not repo_dir.exists():
        if not (legacy_repo_dir / ".git").exists():
            raise FileExistsError(f"Legacy repo is not a git repo: {legacy_repo_dir}")
        log(f"  [repo] rename legacy repo {legacy_repo_dir.name} -> {repo_dir.name}")
        legacy_repo_dir.rename(repo_dir)
        return repo_dir
    if not clone:
        raise FileNotFoundError(
            f"Cannot find repo {repo_dir}. Run --prepare-repos --clone or pass --clone."
        )
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if not repo_dir.exists():
        run(["git", "clone", "--no-checkout", REMOTE_URL, str(repo_dir)], check=True)
    run(["git", "fetch", "origin", bug.sha_before, bug.sha_after], cwd=repo_dir, check=True)
    return repo_dir


def checkout_commit(repo_dir: Path, sha: str) -> None:
    run(["git", "reset", "--hard"], cwd=repo_dir, check=True)
    run(["git", "clean", "-fdx"], cwd=repo_dir, check=True)
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


def _build_env(*, asan: bool, coverage: bool) -> dict:
    env = os.environ.copy()
    cflags = [BASE_CFLAGS]
    ldflags: List[str] = []
    if coverage:
        cflags.append(COV_FLAGS)
        ldflags.append(COV_FLAGS)
    if asan:
        cflags.append(ASAN_FLAGS)
        ldflags.append(ASAN_LDFLAGS)
        env["ASAN_OPTIONS"] = (
            "detect_leaks=0:abort_on_error=0:halt_on_error=0:"
            "exitcode=1:symbolize=1"
        )
    env["CFLAGS"] = " ".join(cflags)
    if ldflags:
        env["LDFLAGS"] = " ".join(ldflags)
    return env


def _cmake_configure_cmd(
    repo_dir: Path,
    build_dir: Path,
    *,
    asan: bool,
    coverage: bool,
) -> List[str]:
    cflags = [BASE_CFLAGS]
    ldflags: List[str] = []
    if coverage:
        cflags.append(COV_FLAGS)
        ldflags.append(COV_FLAGS)
    if asan:
        cflags.append(ASAN_FLAGS)
        ldflags.append(ASAN_LDFLAGS)

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
            "-DENABLE_SSL=ON",
            "-DENABLE_SSL_TESTS=OFF",
            "-DENABLE_EXAMPLES=OFF",
            "-DDISABLE_TESTS=OFF",
            f"-DCMAKE_C_FLAGS={' '.join(cflags)}",
            f"-DCMAKE_EXE_LINKER_FLAGS={' '.join(ldflags)}",
            f"-DCMAKE_SHARED_LINKER_FLAGS={' '.join(ldflags)}",
            f"-DCMAKE_MODULE_LINKER_FLAGS={' '.join(ldflags)}",
        ]
    )
    return cmd


def compile_hiredis(
    repo_dir: Path,
    *,
    jobs: int,
    asan: bool,
    coverage: bool,
) -> bool:
    if not which("cmake"):
        log("  [build] cmake is not in PATH")
        return False
    build_dir = repo_dir / BUILD_DIR_NAME
    if build_dir.exists():
        shutil.rmtree(build_dir)
    env = _build_env(asan=asan, coverage=coverage)
    rc, out, err = run(
        _cmake_configure_cmd(repo_dir, build_dir, asan=asan, coverage=coverage),
        cwd=repo_dir,
        env=env,
        capture=True,
        timeout=180,
    )
    if rc != 0:
        log(f"  [build] cmake configure failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    rc, out, err = run(
        ["cmake", "--build", str(build_dir), "--parallel", str(jobs)],
        cwd=repo_dir,
        env=env,
        capture=True,
        timeout=240,
    )
    if rc != 0:
        log(f"  [build] cmake build failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    return True


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _test_id(num: int, name: str) -> str:
    return f"hiredis_{num:03d}_{_safe_label(name)}"


def parse_hiredis_output(output: str) -> List[TestResult]:
    clean = _strip_ansi(output)
    results: List[TestResult] = []
    for match in _TEST_BLOCK_RE.finditer(clean):
        num = int(match.group("num"))
        body = match.group("body").strip()
        status_match = _STATUS_RE.search(body)
        status = status_match.group(1) if status_match else ""
        if status_match:
            name = body[: status_match.start()].strip()
        else:
            name = body.splitlines()[0].strip() if body else f"test_{num:03d}"
        name = re.sub(r"\s+", " ", name).strip()
        if name.endswith(":"):
            name = name[:-1].strip()
        outcome = "FAIL" if status == "FAILED" else "PASS"
        reason = "reported_failed" if status == "FAILED" else ""
        results.append(
            TestResult(
                test_id=_test_id(num, name),
                outcome=outcome,
                fail_reason=reason,
                actual_output="" if outcome == "PASS" else body[-4000:],
            )
        )
    return results


def _run_hiredis_suite(
    repo_dir: Path,
    *,
    test_timeout: int,
    phase_label: str,
    asan_env: bool,
    allow_allocator_may_return_null: bool = False,
) -> Tuple[int, str, str, List[TestResult]]:
    build_dir = repo_dir / BUILD_DIR_NAME
    env = os.environ.copy()
    if asan_env:
        asan_options = (
            "detect_leaks=0:abort_on_error=0:halt_on_error=0:"
            "exitcode=1:symbolize=1"
        )
        if allow_allocator_may_return_null:
            asan_options = asan_options.replace(
                "exitcode=1", "allocator_may_return_null=1:exitcode=1"
            )
        env["ASAN_OPTIONS"] = asan_options
    env.setdefault("REDIS_SERVER", "redis-server")
    redis_port, redis_ssl_port = _ports_for_phase(phase_label)
    env["REDIS_PORT"] = str(redis_port)
    env["REDIS_SSL_PORT"] = str(redis_ssl_port)
    rc, out, err = run(
        [str(repo_dir / "test.sh")],
        cwd=build_dir,
        env=env,
        capture=True,
        timeout=test_timeout,
    )
    combined = out + err
    parsed = parse_hiredis_output(combined)
    if parsed and _has_failure_marker(combined):
        target = next((r for r in parsed if "maxelements" in r.test_id), None)
        if target is None:
            target = next((r for r in parsed if r.outcome == "FAIL"), parsed[-1])
        target.outcome = "FAIL"
        target.fail_reason = "asan_error"
        target.actual_output = combined[-4000:]
    elif rc != 0 and parsed and all(r.outcome == "PASS" for r in parsed):
        parsed[-1].outcome = "FAIL"
        parsed[-1].fail_reason = f"suite_exit_code={rc}"
        parsed[-1].actual_output = combined[-4000:]
    return rc, out, err, parsed


def overlay_asan_failure(
    base_results: List[TestResult],
    strict_results: List[TestResult],
    strict_output: str,
) -> List[TestResult]:
    if not _has_failure_marker(strict_output):
        return base_results
    target = next((r for r in base_results if "maxelements" in r.test_id), None)
    if target is None:
        target = next((r for r in strict_results if "maxelements" in r.test_id), None)
    if target is None and base_results:
        target = base_results[-1]
    if target is not None:
        target.outcome = "FAIL"
        target.fail_reason = "asan_error"
        target.actual_output = strict_output[-4000:]
    return base_results


def _has_failure_marker(output: str) -> bool:
    return any(marker in output for marker in ASAN_ERROR_MARKERS)


def _ports_for_phase(phase_label: str) -> Tuple[int, int]:
    digest = int(hashlib.md5(phase_label.encode("utf-8")).hexdigest()[:6], 16)
    redis_port = 30000 + ((os.getpid() + digest) % 20000)
    return redis_port, redis_port + 10000


def merge_phase_results(
    *,
    fixed_results: List[TestResult],
    buggy_results: List[TestResult],
    fixed_required: bool,
) -> List[TestResult]:
    fixed_by_id = {r.test_id: r for r in fixed_results}
    buggy_by_id = {r.test_id: r for r in buggy_results}
    ordered_ids = [r.test_id for r in fixed_results] if fixed_required else [r.test_id for r in buggy_results]
    for r in buggy_results:
        if r.test_id not in ordered_ids:
            ordered_ids.append(r.test_id)

    merged: List[TestResult] = []
    for test_id in ordered_ids:
        buggy = buggy_by_id.get(test_id)
        fixed = fixed_by_id.get(test_id)
        base = buggy or fixed
        if base is None:
            continue
        result = TestResult(
            test_id=test_id,
            outcome=buggy.outcome if buggy else "FAIL",
            outcome_fixed=fixed.outcome if fixed else "",
            fail_reason=buggy.fail_reason if buggy else "not_observed_in_buggy_suite",
            actual_output=buggy.actual_output if buggy else "",
            expected_output="",
            covered_functions=[],
        )
        merged.append(result)
    return merged


def log_test_progress(
    phase_label: str,
    results: List[TestResult],
    *,
    collect_cov: bool,
) -> None:
    n_fail = 0
    n_with_cov = 0
    total = len(results)
    for idx, result in enumerate(results, 1):
        if result.outcome == "FAIL":
            n_fail += 1
        if result.covered_functions:
            n_with_cov += 1

        if collect_cov:
            cov_info = f", with_coverage={n_with_cov}"
        else:
            cov_info = " (no-cov run)"
        log(f"  [{phase_label}] {idx}/{total} done (fail={n_fail}{cov_info})")


def clear_gcda(repo_dir: Path) -> None:
    for path in repo_dir.rglob("*.gcda"):
        try:
            path.unlink()
        except OSError:
            pass


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

    for idx, gcda in enumerate(gcda_files, 1):
        funcs_called: List[str] = []
        src_candidates = _source_candidates(repo_dir, gcda)
        src_path = next((p for p in src_candidates if p.exists()), None)
        rc, out, err = run(
            ["gcov", "-f", "-b", "-c", gcda.name],
            cwd=gcda.parent,
            capture=True,
            timeout=60,
        )
        if rc != 0 or src_path is None:
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
        if not funcs_called:
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


def fallback_coverage_from_output(output: str, repo_dir: Path) -> List[str]:
    repo_prefix = str(repo_dir)
    covered: List[str] = []
    for m in _ASAN_FRAME_RE.finditer(output):
        func = m.group("func")
        src = m.group("file")
        if not src.startswith(repo_prefix):
            continue
        if func.startswith("__interceptor_"):
            continue
        covered.append(f"{os.path.basename(src)}:{func}")
    return sorted(set(covered))


def write_run_one_test(repo_dir: Path, results: List[TestResult]) -> None:
    mapping_lines = []
    for result in results:
        m = re.match(r"^hiredis_(\d{3})_", result.test_id)
        ordinal = int(m.group(1)) if m else 0
        mapping_lines.append(f"{result.test_id}\t{ordinal}")

    script = repo_dir / "run_one_test.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            ROOT="$(cd "$(dirname "$0")" && pwd)"
            BUILD_DIR="$ROOT/{BUILD_DIR_NAME}"
            TEST_ID="${{1:-}}"
            MAP="$ROOT/.build_meta_hiredis_tests"
            if [[ -z "$TEST_ID" ]]; then
              echo "usage: $0 <test_id>" >&2
              exit 2
            fi
            if [[ ! -x "$BUILD_DIR/hiredis-test" ]]; then
              cmake -S "$ROOT" -B "$BUILD_DIR" -DENABLE_SSL=ON \\
                -DENABLE_SSL_TESTS=OFF -DENABLE_EXAMPLES=OFF -DDISABLE_TESTS=OFF
              cmake --build "$BUILD_DIR"
            fi
            LOG="$(mktemp)"
            trap 'rm -f "$LOG"' EXIT
            (cd "$BUILD_DIR" && bash "$ROOT/test.sh") >"$LOG" 2>&1 || true
            python3 - "$TEST_ID" "$MAP" "$LOG" <<'PY'
            import re, sys
            test_id, map_path, log_path = sys.argv[1:4]
            wanted = None
            with open(map_path, encoding="utf-8") as fh:
                for line in fh:
                    tid, ordinal = line.rstrip("\\n").split("\\t", 1)
                    if tid == test_id:
                        wanted = int(ordinal)
                        break
            if wanted is None:
                print(f"[run_one_test] unknown test_id {{test_id}}", file=sys.stderr)
                sys.exit(2)
            text = open(log_path, encoding="utf-8", errors="replace").read()
            text = re.sub(r"\\x1b\\[[0-9;]*m", "", text)
            pattern = re.compile(r"(?ms)^#(\\d+)\\s+(.*?)(?=^#\\d+\\s+|\\Z)")
            for m in pattern.finditer(text):
                if int(m.group(1)) != wanted:
                    continue
                block = m.group(0).rstrip()
                print(block)
                if re.search(r"\\bPASSED\\b|\\bSKIPPED\\b", block):
                    sys.exit(0)
                sys.exit(1)
            print(text[-4000:])
            sys.exit(1)
            PY
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    (repo_dir / ".build_meta_hiredis_tests").write_text(
        "\n".join(mapping_lines) + "\n",
        encoding="utf-8",
    )


def _extract_ground_truth_funcs(bug: BugEntry, repo_dir: Path) -> List[str]:
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
    c_keywords = {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "case",
        "do",
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


def _compile_cmd_for_meta(
    repo_dir: Path,
    *,
    asan: bool,
    coverage: bool,
    jobs: int,
) -> str:
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
    dual_run: bool,
    gcov_scope: str,
    test_timeout: int,
) -> Optional[Path]:
    out_path = metadata_dir / f"{bug.safe_bug_id}_meta.json"
    raw_out_path = raw_dir / f"{bug.safe_bug_id}_meta.json"

    try:
        repo_dir = ensure_repo(bug, out_root, clone=clone)
    except Exception as exc:
        log(f"  [error] cannot find/clone repo: {exc}")
        return None

    compile_cmd = _compile_cmd_for_meta(
        repo_dir,
        asan=True,
        coverage=True,
        jobs=jobs,
    )

    try:
        checkout_buggy(repo_dir, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy failed: {exc}")
        return None

    phase_info: Dict[str, object] = {
        "mode": "dual",
        "phase_b_scope": gcov_scope,
        "test_policy": "fixed_tree_tests_for_buggy_and_fixed",
    }

    log("  [phaseA-buggy] checkout+build ASAN")
    if not compile_hiredis(
        repo_dir,
        jobs=jobs,
        asan=True,
        coverage=False,
    ):
        record = _empty_record(bug, repo_dir, compile_cmd, error="phaseA_buggy_compile_failed")
        _write_meta(raw_out_path, record)
        _write_meta(out_path, record)
        return out_path
    rc_buggy, out_buggy, err_buggy, buggy_strict_results = _run_hiredis_suite(
        repo_dir,
        test_timeout=test_timeout,
        phase_label="phaseA-buggy-strict",
        asan_env=True,
        allow_allocator_may_return_null=False,
    )
    strict_output = out_buggy + err_buggy
    buggy_results = buggy_strict_results
    if _has_failure_marker(strict_output):
        log("  [phaseA-buggy] strict ASAN found sanitizer failure; rerun tolerant ASAN for full test list")
        rc_tol, out_tol, err_tol, buggy_tolerant_results = _run_hiredis_suite(
            repo_dir,
            test_timeout=test_timeout,
            phase_label="phaseA-buggy-tolerant",
            asan_env=True,
            allow_allocator_may_return_null=True,
        )
        if buggy_tolerant_results:
            buggy_results = overlay_asan_failure(
                buggy_tolerant_results,
                buggy_strict_results,
                strict_output,
            )
        else:
            buggy_results = overlay_asan_failure(
                buggy_strict_results,
                buggy_strict_results,
                strict_output,
            )
    log(f"  [phaseA-buggy] parsed {len(buggy_results)} custom test outcome(s)")
    log_test_progress("phaseA-buggy", buggy_results, collect_cov=False)

    fixed_results: List[TestResult] = []
    try:
        checkout_fixed(repo_dir, bug)
        log("  [phaseA-fixed] checkout+build ASAN (outcome_fixed)")
        if compile_hiredis(
            repo_dir,
            jobs=jobs,
            asan=True,
            coverage=False,
        ):
            rc_fixed, out_fixed, err_fixed, fixed_results = _run_hiredis_suite(
                repo_dir,
                test_timeout=test_timeout,
                phase_label="phaseA-fixed",
                asan_env=True,
                allow_allocator_may_return_null=False,
            )
            phase_info["phase_a_fixed_status"] = "ok"
            phase_info["phase_a_fixed_fail_count"] = sum(
                1 for r in fixed_results if r.outcome == "FAIL"
            )
            log(f"  [phaseA-fixed] parsed {len(fixed_results)} custom test outcome(s)")
            log_test_progress("phaseA-fixed", fixed_results, collect_cov=False)
        else:
            phase_info["phase_a_fixed_status"] = "compile_failed"
            log("  [warn] phaseA-fixed build failed, outcome_fixed will be empty.")
    finally:
        checkout_buggy(repo_dir, bug)

    results = merge_phase_results(
        fixed_results=fixed_results,
        buggy_results=buggy_results,
        fixed_required=bool(fixed_results),
    )
    phase_info["phase_a_fail_count"] = sum(1 for r in results if r.outcome == "FAIL")

    cov_functions: List[str] = []
    if skip_coverage:
        phase_info["phase_b_scope"] = "skip_coverage"
    else:
        log("  [phaseB] checkout buggy + build GCOV")
        checkout_buggy(repo_dir, bug)
        if compile_hiredis(
            repo_dir,
            jobs=jobs,
            asan=False,
            coverage=True,
        ):
            clear_gcda(repo_dir)
            rc_cov, out_cov, err_cov, cov_results = _run_hiredis_suite(
                repo_dir,
                test_timeout=test_timeout,
                phase_label="phaseB",
                asan_env=False,
                allow_allocator_may_return_null=False,
            )
            cov_map = collect_coverage(repo_dir)
            cov_functions = coverage_to_qualified(cov_map)
            if not cov_functions and (out_cov or err_cov):
                cov_functions = fallback_coverage_from_output(out_cov + err_cov, repo_dir)
            phase_info["phase_b_test_count"] = len(results)
            phase_info["phase_b_with_coverage"] = len(results) if cov_functions else 0
            for result in results:
                result.covered_functions = cov_functions
            log_test_progress("phaseB", results, collect_cov=True)
        else:
            phase_info["phase_b_status"] = "compile_failed"

    if skip_coverage:
        for result in results:
            result.covered_functions = cov_functions

    write_run_one_test(repo_dir, results)
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
        "ground_truth": [f"{source_file}::{fn}" for fn in ground_truth_functions]
        if source_file
        else [],
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
        prog="build_meta_hiredis.py",
        description="Build Unified-Debugging metadata for redis/hiredis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sha", action="append", default=[], help="Only process matching commit_after prefix.")
    ap.add_argument("--only", dest="sha", action="append", help="Alias of --sha.")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of bugs (0 = all).")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT, help=f"Repo root (default: {DEFAULT_OUT_ROOT}).")
    ap.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR, help=f"Metadata output (default: {DEFAULT_METADATA_DIR}).")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Raw output (default: {DEFAULT_RAW_DIR}).")
    ap.add_argument("--jobs", type=int, default=max(os.cpu_count() or 2, 2) - 1, help="Parallel build jobs.")
    ap.add_argument("--test-timeout", type=int, default=DEFAULT_TEST_TIMEOUT, help="Timeout for the hiredis suite.")
    ap.add_argument("--skip-coverage", action="store_true", help="Do not collect gcov coverage.")
    ap.add_argument("--dual-run", action="store_true", help="Accepted for consistency; hiredis always uses dual mode.")
    ap.add_argument(
        "--gcov-scope",
        choices=["all", "fail", "regression", "fail+regression"],
        default="all",
        help="Kept for project consistency; hiredis coverage is suite-level.",
    )
    ap.add_argument("--skip-if-exists", action="store_true", help="Skip existing metadata.")
    ap.add_argument("--clone", action="store_true", help="Clone redis/hiredis if the repo is missing.")
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

    log(f"Will process {len(bugs)} bug(s).")
    log(f"  metadata_dir = {args.metadata_dir}")
    log(f"  raw_dir      = {args.raw_dir}")
    log("  dual_run     = True")

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

    log(f"Done: {ok}/{len(bugs)} bug(s) have output.")
    return 0 if ok == len(bugs) else 1


if __name__ == "__main__":
    sys.exit(main())
