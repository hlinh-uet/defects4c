#!/usr/bin/env python3
"""
Build Unified-Debugging metadata for Defects4C project php___php-src.

The flow matches the custom tcpdump/cJSON builders:
  - bug_id comes from bugs_list_new.json -> type.id
  - fixed tree is commit_after
  - buggy tree is fixed tree with files.src overlaid from commit_before
  - phase A records buggy outcome and optional fixed outcome
  - phase B records gcov coverage from the buggy tree only
"""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"

REMOTE_URL = "https://github.com/php/php-src.git"
DEFAULT_TEST_TIMEOUT = 180
COVERAGE_PARSER_VERSION = 2

BASE_CFLAGS = "-Wno-error -g -O0"
COV_CFLAGS = "-fprofile-arcs -ftest-coverage"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage"
ASAN_CFLAGS = "-fsanitize=address -fno-omit-frame-pointer"
ASAN_LDFLAGS = "-fsanitize=address"

DEFAULT_CONFIGURE_FLAGS = [
    "--enable-phpdbg",
    "--enable-fpm",
    "--without-pear",
    "--enable-sysvsem",
    "--enable-sysvshm",
    "--enable-shmop",
    "--enable-pcntl",
    "--enable-mbstring",
    "--enable-shared=Yes",
    "--enable-static=No",
]

_GCOV_FUNC_RE = re.compile(
    r"^function\s+(?P<name>.+?)\s+called\s+(?P<calls>\d+)\s+returned",
    re.MULTILINE,
)
_GCOV_FUNC_LINES_RE = re.compile(
    r"Function '(?P<name>[^']+)'\nLines executed:(?P<pct>[0-9.]+)%",
    re.MULTILINE,
)
_PHP_TEST_COUNT_RE = re.compile(
    r"^(?P<label>Tests (?:failed|warned|borked|leaked))\s*:\s*(?P<count>\d+)\b",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass
class BugEntry:
    sha_after: str
    sha_before: str
    src_files: List[str]
    test_files: List[str]
    build_flags: List[str]
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
    test_relpath: str
    command: List[str] = field(default_factory=list)


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
    container_default = Path("/out") / PROJECT_NAME
    if _safe_exists(Path("/out")):
        return container_default
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / PROJECT_NAME
    if _safe_exists(host_default):
        return host_default
    return host_default


def _detect_default_metadata_dir() -> Path:
    host_default = (
        DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "php" / "metadata"
    )
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/php/metadata")
    return host_default


def _detect_default_raw_dir() -> Path:
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "php" / "raw"
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/php/raw")
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
    return Path("/tmp") / f".build_meta_php.{key}.lock"


def load_bugs() -> List[BugEntry]:
    if not BUGS_JSON.exists():
        raise FileNotFoundError(f"Không tìm thấy {BUGS_JSON}")
    data = json.loads(BUGS_JSON.read_text(encoding="utf-8"))
    bugs: List[BugEntry] = []
    for item in data:
        files = item.get("files") or {}
        c_compile = item.get("c_compile") or {}
        type_info = item.get("type") or {}
        bugs.append(BugEntry(
            sha_after=item.get("commit_after") or "",
            sha_before=item.get("commit_before") or "",
            src_files=list(files.get("src") or []),
            test_files=[t for t in list(files.get("test") or []) if str(t).endswith(".phpt")],
            build_flags=list(c_compile.get("build_flags") or []),
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
    checkout_commit(repo_dir, bug.sha_after)
    if bug.sha_before and bug.src_files:
        run(
            ["git", "checkout", "--force", bug.sha_before, "--", *bug.src_files],
            cwd=repo_dir,
            check=True,
        )


def checkout_fixed(repo_dir: Path, bug: BugEntry) -> None:
    checkout_commit(repo_dir, bug.sha_after)


def _build_env(*, asan: bool, coverage: bool) -> dict:
    env = os.environ.copy()
    cflags = [BASE_CFLAGS]
    ldflags: List[str] = []
    if coverage:
        cflags.append(COV_CFLAGS)
        ldflags.append(COV_LDFLAGS)
    if asan:
        cflags.append(ASAN_CFLAGS)
        ldflags.append(ASAN_LDFLAGS)
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")
    env["CFLAGS"] = " ".join(cflags)
    env["LDFLAGS"] = " ".join(ldflags)
    env["CC"] = env.get("CC", "gcc")
    env["CXX"] = env.get("CXX", "g++")
    env["NO_INTERACTION"] = "1"
    bison27_bin = Path("/opt/bison-2.7/bin")
    if (bison27_bin / "bison").exists():
        env["PATH"] = f"{bison27_bin}:{env.get('PATH', '')}"
    return env


def _configure_cmd(bug: BugEntry) -> List[str]:
    flags = list(DEFAULT_CONFIGURE_FLAGS)
    flags.extend(bug.build_flags)
    return ["./configure", "--quiet", *flags]


def _has_compatible_bison(env: dict) -> bool:
    rc, out, err = run(["bison", "--version"], env=env, timeout=10)
    if rc != 0:
        log(f"  [build] không chạy được bison --version\n{(out + err)[-1000:]}")
        return False
    first_line = (out or err).splitlines()[0] if (out or err).splitlines() else ""
    if " 2.7" in first_line or first_line.endswith("2.7"):
        return True
    log(
        "  [build] php-src commit cũ cần Bison 2.7; "
        f"đang thấy {first_line!r}. Hãy rebuild Dockerfile.php."
    )
    return False


def _patch_aarch64_inline_asm(repo_dir: Path) -> None:
    """Old php-src uses overly broad AArch64 asm constraints with modern GCC."""
    machine = platform.machine().lower()
    if machine not in {"aarch64", "arm64"}:
        return
    target = repo_dir / "Zend" / "zend_multiply.h"
    if not target.exists():
        return
    text = target.read_text(encoding="utf-8", errors="replace")
    patched = re.sub(
        r'(:\s*)"=X"\(__tmpvar\),\s*"=X"\(usedval\)([^\n]*\\\n\s*:\s*)'
        r'"X"\(a\),\s*"X"\(b\)',
        r'\1"=r"(__tmpvar), "=r"(usedval)\2"r"(a), "r"(b)',
        text,
        count=1,
    )
    if patched != text:
        target.write_text(patched, encoding="utf-8")


def compile_php(repo_dir: Path, bug: BugEntry, *, jobs: int, asan: bool, coverage: bool) -> bool:
    _patch_aarch64_inline_asm(repo_dir)
    env = _build_env(asan=asan, coverage=coverage)
    if not _has_compatible_bison(env):
        return False
    rc, out, err = run(["./buildconf", "--force"], cwd=repo_dir, env=env, timeout=180)
    if rc != 0:
        log(f"  [build] buildconf failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    rc, out, err = run(_configure_cmd(bug), cwd=repo_dir, env=env, timeout=600)
    if rc != 0:
        log(f"  [build] configure failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    rc, out, err = run(["make", "-j", str(jobs)], cwd=repo_dir, env=env, timeout=1200)
    if rc != 0:
        log(f"  [build] make failed rc={rc}\n{(out + err)[-3000:]}")
        return False
    return True


def discover_tests(repo_dir: Path, bug: BugEntry, *, test_scope: str, max_tests: int) -> List[TestEntry]:
    tests: List[str]
    if test_scope == "metadata":
        tests = list(bug.test_files)
    else:
        tests = sorted(
            p.relative_to(repo_dir).as_posix()
            for p in repo_dir.rglob("*.phpt")
            if ".git/" not in p.as_posix()
        )
        metadata_tests = [t for t in bug.test_files if t in tests]
        tests = metadata_tests + [t for t in tests if t not in set(metadata_tests)]
    if max_tests > 0:
        tests = tests[:max_tests]
    return [
        TestEntry(
            test_id=t[:-5] if t.endswith(".phpt") else t,
            test_relpath=t,
            command=[
                "sapi/cli/php",
                "run-tests.php",
                "-q",
                "-p",
                "sapi/cli/php",
                "-g",
                "FAIL,XFAIL,BORK,WARN,LEAK,SKIP",
                t,
            ],
        )
        for t in tests
    ]


def clear_gcda(repo_dir: Path) -> None:
    for gcda in repo_dir.rglob("*.gcda"):
        try:
            gcda.unlink()
        except OSError:
            pass


def run_one_test(repo_dir: Path, te: TestEntry, *, timeout: int) -> Tuple[bool, str, str]:
    env = os.environ.copy()
    env["NO_INTERACTION"] = "1"
    env["TEST_PHP_EXECUTABLE"] = str(repo_dir / "sapi/cli/php")
    rc, out, err = run(te.command, cwd=repo_dir, env=env, timeout=timeout)
    combined = (out or "") + (err or "")
    counts = {
        m.group("label").lower(): int(m.group("count"))
        for m in _PHP_TEST_COUNT_RE.finditer(combined)
    }
    if counts:
        bad = {
            label: count
            for label, count in counts.items()
            if count > 0
        }
        passed = not bad
        reason = ",".join(f"{k}={v}" for k, v in sorted(bad.items()))
        if passed:
            return True, combined, ""
        return False, combined, reason or f"exit_code={rc}"

    failed_markers = ["FAILED TEST SUMMARY", "BORKED"]
    passed = rc == 0 and not any(marker in combined for marker in failed_markers)
    if passed:
        return True, combined, ""
    reason = "timeout" if rc == 124 else f"exit_code={rc}"
    return False, combined, reason


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


def collect_coverage(repo_dir: Path) -> Dict[str, List[str]]:
    if not which("gcov"):
        return {}
    gcda_files = list(repo_dir.rglob("*.gcda"))
    covered: Dict[str, List[str]] = {}

    for gcov_file in repo_dir.rglob("*.gcov"):
        try:
            gcov_file.unlink()
        except OSError:
            pass

    for gcda in gcda_files:
        funcs_called: List[str] = []
        rc, out, _ = run(
            ["gcov", "-f", "-b", "-c", gcda.name],
            cwd=gcda.parent,
            capture=True,
            timeout=30,
        )
        if rc != 0:
            continue
        src_path = _source_from_gcov_output(repo_dir, gcda, out)
        if src_path is None:
            src_candidates = _source_candidates(repo_dir, gcda)
            src_path = next((p for p in src_candidates if p.exists()), None)
        if src_path is None:
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
        if rel_src.startswith(".git/"):
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
    case_lines = []
    for te in entries:
        cmd = " ".join(shlex.quote(x) for x in te.command)
        case_lines.append(
            f"{shlex.quote(te.test_id)}) cd \"$ROOT\"; exec {cmd} ;;"
        )
    script = repo_dir / "run_one_test.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "ROOT=$(cd \"$(dirname \"$0\")\" && pwd)\n"
        "export NO_INTERACTION=1\n"
        "export TEST_PHP_EXECUTABLE=\"$ROOT/sapi/cli/php\"\n"
        "test_id=${1:?usage: run_one_test.sh <test_id>}\n"
        "case \"$test_id\" in\n"
        + "\n".join("  " + line for line in case_lines)
        + "\n  *) echo \"unknown test_id: $test_id\" >&2; exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
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
    if data.get("build_error"):
        return False
    phase_info = data.get("phase_info") or {}
    try:
        version = int(phase_info.get("coverage_parser_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != COVERAGE_PARSER_VERSION or not isinstance(data.get("tests"), list):
        return False
    if phase_info.get("phase_b_status") == "compile_failed":
        return False
    if phase_info.get("phase_b_scope") != "skip_coverage":
        if phase_info.get("mode") == "dual" and "phase_b_with_coverage" not in phase_info:
            return False
        if phase_info.get("mode") == "single" and "with_coverage" not in phase_info:
            return False
    return True


def _php_symbol_from_hunk_signature(signature: str) -> str:
    for macro in ("PHP_FUNCTION", "ZEND_FUNCTION"):
        match = re.search(rf"\b{macro}\s*\(\s*([A-Za-z_]\w*)\s*\)", signature)
        if match:
            return f"zif_{match.group(1)}"

    for macro in ("PHP_METHOD", "ZEND_METHOD", "SPL_METHOD"):
        match = re.search(
            rf"\b{macro}\s*\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)",
            signature,
        )
        if match:
            return f"zim_{match.group(1)}_{match.group(2)}"

    c_keywords = {"if", "for", "while", "switch", "return", "sizeof", "case", "do"}
    match = re.match(r".*?\b([A-Za-z_]\w*)\s*\(", signature)
    if match and match.group(1) not in c_keywords:
        return match.group(1)
    return ""


def _php_symbol_at_line(repo_dir: Path, bug: BugEntry, src_file: str, line_number: int) -> str:
    if line_number <= 0:
        return ""
    rc, text, _ = run(
        ["git", "show", f"{bug.sha_after}:{src_file}"],
        cwd=repo_dir,
        capture=True,
    )
    if rc != 0 or not text:
        path = repo_dir / src_file
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")

    line_offsets = [0]
    for match in re.finditer("\n", text):
        line_offsets.append(match.end())
    target_offset = line_offsets[min(line_number - 1, len(line_offsets) - 1)]
    prefix = text[:target_offset]

    macro_matches = list(re.finditer(
        r"\b(?:PHP_FUNCTION|ZEND_FUNCTION|PHP_METHOD|ZEND_METHOD|SPL_METHOD)\s*\([^)]*\)",
        prefix,
    ))
    normal_matches = list(re.finditer(
        r"(?m)^[A-Za-z_][\w\s\*]*\s+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{",
        prefix,
    ))

    candidates: List[Tuple[int, str]] = []
    for match in macro_matches:
        symbol = _php_symbol_from_hunk_signature(match.group(0))
        if symbol:
            candidates.append((match.start(), symbol))
    for match in normal_matches:
        candidates.append((match.start(), match.group(1)))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1]


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
        symbol = _php_symbol_at_line(repo_dir, bug, src_file, line_number)
        if symbol:
            funcs.append(symbol)
    return sorted(set(funcs))


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
    for line in diff.splitlines():
        if line.startswith("@@"):
            tail = line.split("@@", 2)[-1].strip()
            name = _php_symbol_from_hunk_signature(tail)
            if name:
                funcs.add(name)
    if funcs:
        return sorted(funcs)
    return _ground_truth_from_locations(bug, repo_dir)


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


def _compile_cmd_for_meta(bug: BugEntry, *, jobs: int) -> str:
    cfg = " ".join(shlex.quote(x) for x in _configure_cmd(bug))
    return f"./buildconf --force && {cfg} && make -j {jobs}"


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
) -> Optional[Path]:
    out_path = metadata_dir / f"{bug.safe_bug_id}_meta.json"
    raw_out_path = raw_dir / f"{bug.safe_bug_id}_meta.json"

    try:
        repo_dir = ensure_repo(bug, out_root, clone=clone)
    except Exception as exc:
        log(f"  [error] không tìm/clone được repo: {exc}")
        return None

    compile_cmd = _compile_cmd_for_meta(bug, jobs=jobs)

    try:
        checkout_buggy(repo_dir, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy lỗi: {exc}")
        return None

    if dual_run:
        phase_info: Dict[str, object] = {
            "mode": "dual",
            "phase_b_scope": "all" if not skip_coverage else "skip_coverage",
            "test_policy": "fixed_tree_tests_for_buggy_and_fixed",
            "test_scope": test_scope,
        }
        log("  [phaseA-buggy] checkout+build ASAN")
        if not compile_php(repo_dir, bug, jobs=jobs, asan=True, coverage=False):
            record = _empty_record(bug, repo_dir, compile_cmd, error="phaseA_buggy_compile_failed")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        entries = discover_tests(repo_dir, bug, test_scope=test_scope, max_tests=max_tests)
        write_run_one_test(repo_dir, entries)
        results_a, _ = run_tests(
            repo_dir, entries, collect_cov=False,
            test_timeout=test_timeout, phase_label="phaseA-buggy",
        )

        fixed_outcome_by_test: Dict[str, str] = {}
        try:
            checkout_fixed(repo_dir, bug)
            log("  [phaseA-fixed] checkout+build ASAN (outcome_fixed)")
            if compile_php(repo_dir, bug, jobs=jobs, asan=True, coverage=False):
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
        if not skip_coverage:
            log("  [phaseB] checkout buggy + build GCOV")
            if compile_php(repo_dir, bug, jobs=jobs, asan=False, coverage=True):
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
        phase_info = {
            "mode": "single",
            "test_policy": "fixed_tree_tests",
            "test_scope": test_scope,
        }
        if not compile_php(repo_dir, bug, jobs=jobs, asan=asan, coverage=not skip_coverage):
            record = _empty_record(bug, repo_dir, compile_cmd, error="compile_failed")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        entries = discover_tests(repo_dir, bug, test_scope=test_scope, max_tests=max_tests)
        write_run_one_test(repo_dir, entries)
        results, n_cov = run_tests(
            repo_dir, entries, collect_cov=not skip_coverage,
            test_timeout=test_timeout, phase_label="single",
        )
        phase_info["with_coverage"] = n_cov

    write_run_one_test(repo_dir, discover_tests(repo_dir, bug, test_scope=test_scope, max_tests=max_tests))
    source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
    ground_truth_functions = _extract_ground_truth_funcs(bug, repo_dir)
    test_cmd_template = f"bash {shlex.quote(str(repo_dir / 'run_one_test.sh'))} {{test_id}}"
    phase_info["coverage_parser_version"] = COVERAGE_PARSER_VERSION
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
        prog="build_meta_php.py",
        description="Sinh metadata Unified-Debugging cho php/php-src.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sha", action="append", default=[], help="Chỉ xử lý commit_after khớp prefix.")
    ap.add_argument("--only", dest="sha", action="append", help="Alias của --sha.")
    ap.add_argument("--limit", type=int, default=0, help="Giới hạn số bug xử lý (0 = tất cả).")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT, help=f"Thư mục chứa git_repo_dir_<bug_id> (mặc định: {DEFAULT_OUT_ROOT}).")
    ap.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR, help=f"Nơi ghi metadata (mặc định: {DEFAULT_METADATA_DIR}).")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Nơi ghi raw output (mặc định: {DEFAULT_RAW_DIR}).")
    ap.add_argument("--jobs", type=int, default=max(os.cpu_count() or 2, 2) - 1, help="Số job khi build.")
    ap.add_argument("--test-timeout", type=int, default=DEFAULT_TEST_TIMEOUT, help="Timeout mỗi .phpt test.")
    ap.add_argument("--max-tests", type=int, default=0, help="Giới hạn số test mỗi bug (0 = tất cả).")
    ap.add_argument("--test-scope", choices=["all", "metadata"], default="all", help="all = mọi .phpt trong fixed tree; metadata = chỉ files.test.")
    ap.add_argument("--skip-coverage", action="store_true", help="Không thu thập gcov.")
    ap.add_argument("--asan", action="store_true", help="Build + test với AddressSanitizer trong single mode.")
    ap.add_argument("--dual-run", action="store_true", help="Phase A buggy/fixed outcomes + phase B buggy coverage.")
    ap.add_argument(
        "--gcov-scope",
        choices=["all", "fail", "regression", "fail+regression"],
        default="all",
        help="Tương thích tcpdump/cJSON; php hiện thu coverage cho test đã chọn.",
    )
    ap.add_argument("--skip-if-exists", action="store_true", help="Bỏ qua bug đã có output metadata.")
    ap.add_argument("--clone", action="store_true", help="Tự clone php-src từ GitHub nếu chưa có repo.")
    ap.add_argument("--prepare-repos", action="store_true", help="Chỉ chuẩn bị repo theo bug_id rồi thoát.")
    ap.add_argument("--list", action="store_true", help="Chỉ liệt kê bug rồi thoát.")
    return ap


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    bugs = load_bugs()
    if args.sha:
        wanted = tuple(s for s in args.sha if s)
        bugs = [
            b for b in bugs
            if b.sha_after in wanted or b.sha_after.startswith(wanted)
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
                checkout_fixed(repo_dir, bug)
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
    log(f"  test_scope   = {args.test_scope}")

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
                if _existing_metadata_is_current(out_path):
                    log(f"[{idx}/{len(bugs)}] skip existing {bug.bug_id}")
                    ok += 1
                    continue
                log(f"[{idx}/{len(bugs)}] rerun existing {bug.bug_id} (old/invalid parser)")
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
                test_scope=args.test_scope,
                max_tests=args.max_tests,
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
