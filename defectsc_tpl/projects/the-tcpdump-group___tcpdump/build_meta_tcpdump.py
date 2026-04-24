#!/usr/bin/env python3
"""
build_meta_tcpdump.py
=====================

Standalone pipeline sinh metadata theo chuẩn **Unified-Debugging** cho mọi bug
thuộc project ``the-tcpdump-group___tcpdump`` trong framework Defects4C.

Với mỗi bug trong ``bugs_list_new.json``, script làm:

    1. Xác định cây mã nguồn buggy (checkout ``commit_before``).
    2. Dùng trực tiếp bộ test hiện có trong cây buggy
       (``tests/TESTLIST`` và các asset ở ``commit_before``).
    3. Cấu hình + biên dịch tcpdump với cờ coverage (gcov/gcno).
    4. Parse ``tests/TESTLIST`` của buggy version và chạy ``./TESTonce`` cho **từng** test case
       (mặc định chạy toàn bộ TESTLIST). Trước mỗi test xoá sạch ``*.gcda``
       để lấy coverage riêng cho test đó.
    5. Phân tích output ``gcov`` → ``covered_functions`` dạng
       ``<file.c>:<func>`` mà FL của Unified-Debugging kỳ vọng.
    6. Ghi ``{safe_bug_id}_meta.json`` vào 2 chỗ:
         * ``raw/`` — kết quả thô, giữ nguyên outcome chạy trên buggy.
         * ``metadata/`` — cùng nội dung với ``raw/`` để Unified-Debugging dùng.
    7. Sinh ``run_one_test.sh`` trong build tree để ``test_cmd_template``
       có thể chạy lại đúng 1 test khi APR validate patch.

Ghi chú:
    * Cần infra Defects4C sẵn (repo đã được clone bởi ``bulk_git_clone_v2.sh``
      vào ``out_tmp_dirs/``). Nếu thiếu, dùng ``--clone`` để tự clone.
    * Nên chạy trong Docker container slim (xem README) để có gcc/gcov +
      libpcap-dev.
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


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent                # .../projects/the-tcpdump-group___tcpdump
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent                 # .../defectsc_tpl
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent                     # .../defects4c
PROJECT_NAME = PROJECT_DIR.name                              # the-tcpdump-group___tcpdump
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"


def _safe_exists(path: Path) -> bool:
    """`Path.exists()` nhưng không crash khi thiếu quyền truy cập."""
    try:
        return path.exists()
    except OSError:
        return False


def _acquire_single_run_lock(lock_path: Path):
    """Chỉ cho phép 1 process build_meta_tcpdump chạy trên cùng output dir."""
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
    """Đặt lock ở ``/tmp`` để tránh lỗi permission trên mount ``/out``."""
    key = hashlib.md5(str(metadata_dir).encode("utf-8")).hexdigest()
    return Path("/tmp") / f".build_meta_tcpdump.{key}.lock"


def _detect_default_out_root() -> Path:
    """Default cho ``--out-root``.

    - Host: ``<defects4c>/out_tmp_dirs/<project>``.
    - Container (mount ``out_tmp_dirs:/out``): ``/out/<project>``.
    """
    host_default = DEFECTS4C_ROOT / "out_tmp_dirs" / PROJECT_NAME
    if _safe_exists(host_default):
        return host_default
    container_default = Path("/out") / PROJECT_NAME
    if _safe_exists(container_default):
        return container_default
    return host_default


def _detect_default_metadata_dir() -> Path:
    """Default cho ``--metadata-dir``."""
    host_default = DEFECTS4C_ROOT / "unified_debugging" / "tcpdump" / "metadata"
    if _safe_exists(host_default.parent.parent):
        return host_default
    container_mount = Path("/unified_debugging/tcpdump/metadata")
    if _safe_exists(container_mount.parent.parent):
        return container_mount
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/tcpdump/metadata")
    return host_default


def _detect_default_raw_dir() -> Path:
    """Default cho ``--raw-dir``."""
    host_default = DEFECTS4C_ROOT / "unified_debugging" / "tcpdump" / "raw"
    if _safe_exists(host_default.parent.parent):
        return host_default
    container_mount = Path("/unified_debugging/tcpdump/raw")
    if _safe_exists(container_mount.parent.parent):
        return container_mount
    if _safe_exists(Path("/out")):
        return Path("/out/unified_debugging/tcpdump/raw")
    return host_default


DEFAULT_OUT_ROOT = _detect_default_out_root()
DEFAULT_METADATA_DIR = _detect_default_metadata_dir()
DEFAULT_RAW_DIR = _detect_default_raw_dir()

REMOTE_URL = "https://github.com/the-tcpdump-group/tcpdump.git"

COV_CFLAGS = "-g -O0 -fprofile-arcs -ftest-coverage"
COV_LDFLAGS = "-fprofile-arcs -ftest-coverage"
ASAN_CFLAGS = "-fsanitize=address -fno-omit-frame-pointer"
ASAN_LDFLAGS = "-fsanitize=address"

# "<name> <input.pcap> <output.out> [options...]"
_TESTLIST_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$")

# "function NAME called N returned M ..." trong gcov output (cần -f)
_GCOV_FUNC_RE = re.compile(
    r"^function\s+(?P<name>\S+)\s+called\s+(?P<calls>\d+)\s+returned",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class BugEntry:
    """Một bản ghi bug từ ``bugs_list_new.json``."""
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
    """Một dòng trong ``tests/TESTLIST``."""
    name: str
    input_file: str
    expected_file: str
    options: str

    @property
    def test_id(self) -> str:
        return self.name


@dataclass
class TestResult:
    test_id: str
    outcome: str                      # "PASS" | "FAIL"
    outcome_fixed: str = ""
    fail_reason: str = ""
    actual_output: str = ""
    expected_output: str = ""
    covered_functions: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------
def run(cmd, *, cwd=None, env=None, check=False, timeout=None, capture=True):
    """Chạy shell command, trả ``(rc, stdout, stderr)`` dạng str.

    Dùng ``errors="replace"`` để không crash khi output có byte không phải
    UTF-8 (gcov binary markers, pcap bleed-over, v.v.).
    """
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
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Step 1: load bug list
# ---------------------------------------------------------------------------
def load_bugs() -> List[BugEntry]:
    if not BUGS_JSON.exists():
        raise FileNotFoundError(f"Không tìm thấy {BUGS_JSON}")
    data = json.loads(BUGS_JSON.read_text())
    bugs: List[BugEntry] = []
    for raw in data:
        sha_after = raw.get("commit_after")
        sha_before = raw.get("commit_before")
        files = raw.get("files", {}) or {}
        src_files = files.get("src", []) or []
        test_files = files.get("test", []) or []
        type_info = raw.get("type") or {}
        cve = type_info.get("name")
        type_id = type_info.get("id") or sha_after
        if not sha_after:
            continue
        bugs.append(BugEntry(
            sha_after=sha_after,
            sha_before=sha_before or "",
            src_files=src_files,
            test_files=test_files,
            cve_name=cve,
            type_id=type_id,
            raw=raw,
        ))
    counts = Counter(b.bug_id for b in bugs)
    for bug in bugs:
        if counts[bug.bug_id] > 1:
            bug.output_bug_id = f"{bug.bug_id}__{bug.sha_after[:12]}"
        else:
            bug.output_bug_id = bug.bug_id
    return bugs


# ---------------------------------------------------------------------------
# Step 2: locate / prepare buggy source tree
# ---------------------------------------------------------------------------
def ensure_repo(out_root: Path, sha_after: str, *, clone_if_missing: bool = False) -> Path:
    """Trả đường dẫn repo cho bug này. Clone nếu cần."""
    repo_dir = out_root / f"git_repo_dir_{sha_after}"
    if repo_dir.exists() and (repo_dir / ".git").exists():
        return repo_dir
    if not clone_if_missing:
        raise FileNotFoundError(
            f"Repo chưa tồn tại: {repo_dir}\n"
            f"Hãy chạy 'bash bulk_git_clone_v2.sh' của defects4c hoặc dùng --clone."
        )
    out_root.mkdir(parents=True, exist_ok=True)
    log(f"Cloning {REMOTE_URL} → {repo_dir}")
    run(["git", "clone", REMOTE_URL, str(repo_dir)], check=True, capture=True)
    return repo_dir


def _git_checkout(repo_dir: Path, target_sha: str, label: str) -> None:
    log(f"  [git] reset --hard + checkout {label}={target_sha[:10]}")
    run(["git", "reset", "--hard"], cwd=repo_dir, check=True)
    # Một số run để lại nhiều artifact trong tests/NEW, đôi khi `git clean -fdx`
    # báo "Directory not empty" và trả rc=1. Retry với cleanup cưỡng bức.
    rc, out, err = run(["git", "clean", "-ffdx"], cwd=repo_dir, capture=True)
    if rc != 0:
        run(["rm", "-rf", "tests/NEW", "tests/DIFF"], cwd=repo_dir, capture=True)
        rc2, out2, err2 = run(["git", "clean", "-ffdx"], cwd=repo_dir, capture=True)
        if rc2 != 0:
            raise RuntimeError(
                "git clean failed after retry\n"
                f"first_clean_stdout={out[-1200:]}\n"
                f"first_clean_stderr={err[-1200:]}\n"
                f"retry_clean_stdout={out2[-1200:]}\n"
                f"retry_clean_stderr={err2[-1200:]}\n"
            )
    rc, _, _ = run(["git", "cat-file", "-e", f"{target_sha}^{{commit}}"], cwd=repo_dir)
    if rc != 0:
        run(["git", "fetch", "--all", "--tags"], cwd=repo_dir, capture=True)
    run(["git", "checkout", "--force", target_sha], cwd=repo_dir, check=True)


def checkout_buggy(repo_dir: Path, bug: BugEntry) -> None:
    """Reset về ``commit_before`` để reproduce bug."""
    target_sha = bug.sha_before or bug.sha_after
    _git_checkout(repo_dir, target_sha, "buggy")


def checkout_fixed(repo_dir: Path, bug: BugEntry) -> None:
    """Checkout về ``commit_after`` để chạy test trên bản fixed."""
    _git_checkout(repo_dir, bug.sha_after, "fixed")


def existing_buggy_test_asset_basenames(repo_dir: Path, bug: BugEntry) -> List[str]:
    """Các asset trong ``files.test`` nhưng thực sự tồn tại ở checkout buggy."""
    bases: List[str] = []
    for rel in bug.test_files:
        base = os.path.basename(rel)
        if not base or base == "TESTLIST":
            continue
        if (repo_dir / rel).exists():
            bases.append(base)
    return sorted(set(bases))


# ---------------------------------------------------------------------------
# Step 3: compile with coverage
# ---------------------------------------------------------------------------
def compile_with_coverage(
    repo_dir: Path,
    *,
    jobs: int,
    timeout: int = 1800,
    asan: bool = False,
) -> bool:
    """Configure + make với coverage (và tùy chọn ASAN)."""
    env = os.environ.copy()
    cflags = COV_CFLAGS
    ldflags = COV_LDFLAGS
    if asan:
        cflags = f"{cflags} {ASAN_CFLAGS}"
        ldflags = f"{ldflags} {ASAN_LDFLAGS}"
        # LeakSanitizer thường gây noise/timeout cho regression tests rất dài.
        env["ASAN_OPTIONS"] = env.get("ASAN_OPTIONS", "detect_leaks=0")
    env["CFLAGS"] = f"{env.get('CFLAGS', '')} {cflags}".strip()
    env["LDFLAGS"] = f"{env.get('LDFLAGS', '')} {ldflags}".strip()
    env["CC"] = env.get("CC", "gcc")
    env["CXX"] = env.get("CXX", "g++")

    log("  [build] configure (coverage)")
    if not (repo_dir / "configure").exists() and (repo_dir / "configure.ac").exists():
        run(["autoreconf", "-fi"], cwd=repo_dir, env=env, capture=True, timeout=300)
    rc, _, err = run(
        ["./configure", f"--prefix={repo_dir}"],
        cwd=repo_dir, env=env, capture=True, timeout=600,
    )
    if rc != 0:
        log(f"  [build] configure FAILED (rc={rc}). stderr[-400]={err[-400:]}")
        return False

    log(f"  [build] make -j{jobs}")
    rc, _, err = run(
        ["make", "-j", str(jobs)],
        cwd=repo_dir, env=env, capture=True, timeout=timeout,
    )
    if rc != 0:
        rc, _, err = run(
            ["make", "all", "-j", str(jobs)],
            cwd=repo_dir, env=env, capture=True, timeout=timeout,
        )
    if rc != 0:
        log(f"  [build] make FAILED. stderr[-400]={err[-400:]}")
        return False

    tcpdump_bin = repo_dir / "tcpdump"
    if not tcpdump_bin.exists():
        log("  [build] không thấy binary ./tcpdump sau khi make.")
        return False

    gcno_files = list(repo_dir.rglob("*.gcno"))
    if not gcno_files:
        log("  [build] CẢNH BÁO: không tìm thấy *.gcno → coverage flag bị "
            "build system override, covered_functions sẽ rỗng. "
            "Kiểm tra lại CFLAGS/LDFLAGS trong Makefile.in.")
    else:
        log(f"  [build] coverage OK ({len(gcno_files)} *.gcno)")
    return True


# ---------------------------------------------------------------------------
# Step 4: parse & run tests
# ---------------------------------------------------------------------------
def parse_testlist(tests_dir: Path) -> List[TestEntry]:
    testlist = tests_dir / "TESTLIST"
    if not testlist.exists():
        return []
    entries: List[TestEntry] = []
    for line in testlist.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _TESTLIST_RE.match(line)
        if not m:
            continue
        name, inp, outp, opts = m.group(1), m.group(2), m.group(3), (m.group(4) or "")
        entries.append(TestEntry(
            name=name, input_file=inp, expected_file=outp, options=opts.strip(),
        ))
    return entries


def select_tests(
    all_tests: List[TestEntry],
    bug: BugEntry,
    max_pass: int,
    *,
    buggy_pcap_basenames: Optional[set] = None,
) -> List[TestEntry]:
    """Giữ regression test của buggy tree + bổ sung ``max_pass`` entries khác.

    Nếu không xác định được regression pcap trong checkout buggy, chạy toàn bộ
    ``TESTLIST`` hiện có để không phụ thuộc vào asset từ ``commit_after``.
    """
    pcap_names = set(buggy_pcap_basenames or [])
    if not pcap_names:
        return list(all_tests)
    regression, others = [], []
    for t in all_tests:
        if t.input_file in pcap_names or os.path.basename(t.input_file) in pcap_names:
            regression.append(t)
        else:
            others.append(t)
    if max_pass is None or max_pass < 0 or max_pass >= len(others):
        return regression + others
    return regression + others[:max_pass]


def clear_gcda(repo_dir: Path) -> None:
    for gcda in repo_dir.rglob("*.gcda"):
        try:
            gcda.unlink()
        except OSError:
            pass


def run_one_test(tests_dir: Path, entry: TestEntry, timeout: int = 60) -> Tuple[bool, str, str]:
    """Chạy ``./TESTonce`` cho 1 entry. Trả ``(passed, combined_output, reason)``."""
    cmd = ["./TESTonce", entry.name, entry.input_file, entry.expected_file, entry.options]
    rc, out, err = run(cmd, cwd=tests_dir, capture=True, timeout=timeout)
    combined = (out or "") + (("\n" + err) if err else "")
    # Không dùng heuristic "failed" quá rộng để tránh false FAIL từ log text.
    # TESTonce đã chuẩn hóa trạng thái bằng exit code + chuỗi "TEST FAILED".
    failed = (rc != 0) or ("TEST FAILED" in combined)

    reason = ""
    if failed:
        diff_path = tests_dir / f"{entry.name}.diff"
        if diff_path.exists():
            try:
                reason = diff_path.read_text(errors="replace")[-4000:]
            except OSError:
                reason = ""
        if not reason:
            reason = combined[-4000:] or f"TESTonce exit code {rc}"
    return (not failed), combined, reason


def _run_tests_with_optional_coverage(
    repo_dir: Path,
    tests_dir: Path,
    entries: List[TestEntry],
    *,
    test_timeout: int,
    collect_cov: bool,
    cov_verbose_first_n: int = 0,
    phase_label: str = "tests",
) -> Tuple[List[TestResult], int]:
    """
    Chạy danh sách test entries và (tuỳ chọn) thu coverage.

    Args:
      phase_label: nhãn hiển thị trong log (ví dụ: "phaseA", "phaseB").

    Returns:
      - danh sách TestResult theo đúng thứ tự entries
      - số test có covered_functions khác rỗng
    """
    results: List[TestResult] = []
    n_with_cov = 0
    for idx, te in enumerate(entries, 1):
        clear_gcda(repo_dir)
        passed, combined_output, reason = run_one_test(
            tests_dir, te, timeout=test_timeout,
        )
        covered_map = {}
        if collect_cov:
            covered_map = collect_coverage(repo_dir, verbose=(idx <= cov_verbose_first_n))
        covered_funcs = coverage_to_qualified(covered_map)
        if covered_funcs:
            n_with_cov += 1

        expected = ""
        exp_path = tests_dir / te.expected_file
        if exp_path.exists():
            try:
                expected = exp_path.read_text(errors="replace")
            except OSError:
                expected = ""

        results.append(TestResult(
            test_id=te.test_id,
            outcome="PASS" if passed else "FAIL",
            fail_reason="" if passed else (reason or "TESTonce reported failure"),
            actual_output="" if passed else combined_output[-4000:],
            expected_output="" if passed else expected,
            covered_functions=covered_funcs,
        ))

        if idx % 25 == 0 or idx == len(entries):
            n_fail = sum(1 for r in results if r.outcome == "FAIL")
            cov_info = f", with_coverage={n_with_cov}" if collect_cov else " (no-cov run)"
            log(f"  [{phase_label}] {idx}/{len(entries)} done "
                f"(fail={n_fail}{cov_info})")
    return results, n_with_cov


# ---------------------------------------------------------------------------
# Step 4b: coverage collection
# ---------------------------------------------------------------------------
def collect_coverage(repo_dir: Path, *, verbose: bool = False) -> Dict[str, List[str]]:
    """
    Quét ``*.gcda`` (rglob, bao gồm subdir như ``netdissect/``), chạy
    ``gcov -f -b -c`` từng file → parse hàm đã gọi.

    Returns: ``{"<rel_path>/file.c": ["func1", ...]}``.

    Nếu test làm tcpdump crash (SEGV) trước exit, gcc runtime có thể KHÔNG
    flush ``.gcda`` → map sẽ rỗng cho test đó (đây là hành vi đúng).
    """
    if not which("gcov"):
        if verbose:
            log("  [cov] gcov không có trong PATH")
        return {}

    gcda_files = list(repo_dir.rglob("*.gcda"))
    if not gcda_files:
        if verbose:
            log("  [cov] không thấy *.gcda sau khi test "
                "(binary có thể crash trước exit)")
        return {}

    for g in repo_dir.rglob("*.gcov"):
        try:
            g.unlink()
        except OSError:
            pass

    covered: Dict[str, List[str]] = {}
    for gcda in gcda_files:
        gcda_dir = gcda.parent
        src_name = gcda.stem + ".c"
        src_path = gcda_dir / src_name
        if not src_path.exists():
            continue

        rc, out, _ = run(
            ["gcov", "-f", "-b", "-c", "-o", str(gcda_dir), src_name],
            cwd=gcda_dir, capture=True, timeout=30,
        )
        if rc != 0:
            continue

        funcs_called: List[str] = []
        for m in _GCOV_FUNC_RE.finditer(out):
            try:
                if int(m.group("calls")) > 0:
                    funcs_called.append(m.group("name"))
            except ValueError:
                pass

        if not funcs_called:
            gcov_file = gcda_dir / f"{src_name}.gcov"
            if gcov_file.exists():
                try:
                    txt = gcov_file.read_text(errors="replace")
                except OSError:
                    txt = ""
                for m in _GCOV_FUNC_RE.finditer(txt):
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
            rel_src = src_name
        covered[rel_src] = sorted(set(funcs_called))

    for g in repo_dir.rglob("*.gcov"):
        try:
            g.unlink()
        except OSError:
            pass

    if verbose and not covered:
        log(f"  [cov] gcov chạy nhưng không parse được function "
            f"(#gcda={len(gcda_files)})")

    return covered


def coverage_to_qualified(cov_map: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for fname, funcs in cov_map.items():
        for fn in funcs:
            out.append(f"{fname}:{fn}")
    return out


# ---------------------------------------------------------------------------
# Step 5: emit helper run_one_test.sh inside repo_dir
# ---------------------------------------------------------------------------
RUN_ONE_TEST_SH = textwrap.dedent(r"""
    #!/usr/bin/env bash
    # Auto-generated by build_meta_tcpdump.py
    # Chạy đúng 1 test case từ tests/TESTLIST để APR validate bản vá.
    # Usage: bash run_one_test.sh <test_id>

    set -uo pipefail
    HERE=$(cd "$(dirname "$0")" && pwd)
    TEST_ID="${1:?Usage: $0 <test_id>}"

    cd "$HERE/tests" || exit 2
    LINE=$(grep -E "^[[:space:]]*${TEST_ID}[[:space:]]" TESTLIST | head -n 1)
    if [[ -z "$LINE" ]]; then
      echo "[run_one_test] test_id '$TEST_ID' not in TESTLIST" >&2
      exit 3
    fi

    # shellcheck disable=SC2206
    FIELDS=($LINE)
    NAME=${FIELDS[0]}
    INP=${FIELDS[1]}
    OUTP=${FIELDS[2]}
    OPTS="${FIELDS[@]:3}"

    OUTPUT=$(./TESTonce "$NAME" "$INP" "$OUTP" "$OPTS" 2>&1)
    STATUS=$?
    echo "$OUTPUT"
    if [[ $STATUS -ne 0 ]] || grep -q "TEST FAILED" <<<"$OUTPUT"; then
      exit 1
    fi
    exit 0
""").lstrip()


def write_run_one_test(repo_dir: Path) -> Path:
    path = repo_dir / "run_one_test.sh"
    path.write_text(RUN_ONE_TEST_SH)
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Step 6: process one bug end-to-end
# ---------------------------------------------------------------------------
def process_bug(
    bug: BugEntry,
    out_root: Path,
    metadata_dir: Path,
    raw_dir: Path,
    *,
    jobs: int,
    max_pass: int,
    test_timeout: int,
    skip_if_exists: bool,
    clone_if_missing: bool,
    skip_coverage: bool,
    asan: bool = False,
    dual_run: bool = False,
    gcov_scope: str = "fail+regression",
) -> Optional[Path]:
    safe_name = f"{bug.safe_bug_id}_meta.json"
    out_path = metadata_dir / safe_name
    raw_out_path = raw_dir / safe_name

    if skip_if_exists and out_path.exists():
        log(f"[skip] {bug.bug_id} (đã có {out_path.name})")
        return out_path

    log(f"=== {bug.bug_id} | {bug.cve_name or ''} ===")
    try:
        repo_dir = ensure_repo(out_root, bug.sha_after, clone_if_missing=clone_if_missing)
    except Exception as exc:
        log(f"  [error] không tìm/clone được repo: {exc}")
        return None

    # Chuẩn bị repo buggy (dùng chung cho cả 1-phase và 2-phase).
    try:
        checkout_buggy(repo_dir, bug)
    except Exception as exc:
        log(f"  [error] checkout buggy lỗi: {exc}")
        return None

    # Cần build 1 lần để có TESTLIST đầy đủ và helper script.
    if not compile_with_coverage(repo_dir, jobs=jobs, asan=False):
        compile_cmd_tmp = (
            f"cd {shlex.quote(str(repo_dir))} && "
            f"CFLAGS='{COV_CFLAGS}' LDFLAGS='{COV_LDFLAGS}' "
            f"./configure --prefix={shlex.quote(str(repo_dir))} && make -j{jobs}"
        )
        record = _empty_record(bug, repo_dir, compile_cmd_tmp, error="compile_failed")
        _write_meta(raw_out_path, record)
        _write_meta(out_path, record)
        return out_path

    write_run_one_test(repo_dir)
    test_cmd_template = f"bash {shlex.quote(str(repo_dir / 'run_one_test.sh'))} {{test_id}}"
    tests_dir = repo_dir / "tests"
    all_entries = parse_testlist(tests_dir)
    buggy_test_assets = existing_buggy_test_asset_basenames(repo_dir, bug)
    buggy_pcap_basenames = {name for name in buggy_test_assets if name.endswith(".pcap")}
    if buggy_test_assets:
        log(f"  [tests] asset từ metadata có sẵn trong buggy tree: {len(buggy_test_assets)}")
    else:
        log("  [tests] không thấy asset nào từ files.test trong buggy tree; dùng toàn bộ TESTLIST buggy.")
    selected = select_tests(
        all_entries,
        bug,
        max_pass=max_pass,
        buggy_pcap_basenames=buggy_pcap_basenames,
    )
    regression_ids = {
        t.test_id for t in selected
        if t.input_file in buggy_pcap_basenames or os.path.basename(t.input_file) in buggy_pcap_basenames
    }
    log(f"  [tests] TESTLIST={len(all_entries)}, sẽ chạy={len(selected)} "
        f"(regression={len(regression_ids)})")

    # Build command ghi trong metadata: nếu dual-run thì ghi phase A (ASAN) + phase B trong phase_info.
    cflags_for_cmd = COV_CFLAGS
    ldflags_for_cmd = COV_LDFLAGS
    if asan or dual_run:
        cflags_for_cmd = f"{cflags_for_cmd} {ASAN_CFLAGS}"
        ldflags_for_cmd = f"{ldflags_for_cmd} {ASAN_LDFLAGS}"
    compile_cmd = (
        f"cd {shlex.quote(str(repo_dir))} && "
        f"CFLAGS='{cflags_for_cmd}' LDFLAGS='{ldflags_for_cmd}' "
        f"./configure --prefix={shlex.quote(str(repo_dir))} && make -j{jobs}"
    )

    phase_info: Dict[str, object] = {
        "mode": "dual" if dual_run else "single",
        "test_policy": (
            "buggy_tests_for_buggy_and_fixed" if dual_run else "buggy_tests"
        ),
    }

    if dual_run:
        # Phase A: chạy buggy trước, sau đó chạy fixed để lấy outcome_fixed.
        log("  [phaseA-buggy] checkout+build ASAN (labels)")
        try:
            checkout_buggy(repo_dir, bug)
        except Exception as exc:
            log(f"  [error] phaseA-buggy checkout lỗi: {exc}")
            return None
        if not compile_with_coverage(repo_dir, jobs=jobs, asan=True):
            record = _empty_record(bug, repo_dir, compile_cmd, error="compile_failed_phaseA")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        results_phase_a, _ = _run_tests_with_optional_coverage(
            repo_dir, tests_dir, selected,
            test_timeout=test_timeout,
            collect_cov=False,
            phase_label="phaseA-buggy",
        )

        fixed_outcome_by_test: Dict[str, str] = {}
        log("  [phaseA-fixed] checkout+build ASAN (outcome_fixed)")
        try:
            checkout_fixed(repo_dir, bug)
        except Exception as exc:
            log(f"  [warn] phaseA-fixed checkout lỗi: {exc}")
            phase_info["phase_a_fixed_status"] = "checkout_failed"
        else:
            fixed_tests_dir = repo_dir / "tests"
            if compile_with_coverage(repo_dir, jobs=jobs, asan=True):
                results_phase_fixed, _ = _run_tests_with_optional_coverage(
                    repo_dir, fixed_tests_dir, selected,
                    test_timeout=test_timeout,
                    collect_cov=False,
                    phase_label="phaseA-fixed",
                )
                fixed_outcome_by_test = {
                    r.test_id: r.outcome for r in results_phase_fixed
                }
                phase_info["phase_a_fixed_status"] = "ok"
                phase_info["phase_a_fixed_fail_count"] = sum(
                    1 for r in results_phase_fixed if r.outcome == "FAIL"
                )
            else:
                log("  [warn] phaseA-fixed build thất bại, outcome_fixed sẽ rỗng.")
                phase_info["phase_a_fixed_status"] = "compile_failed"

        # Chọn tập test cho phase B (gcov).
        fail_ids = {r.test_id for r in results_phase_a if r.outcome == "FAIL"}
        if gcov_scope == "all":
            selected_phase_b = selected
        elif gcov_scope == "fail":
            selected_phase_b = [t for t in selected if t.test_id in fail_ids]
        elif gcov_scope == "regression":
            selected_phase_b = [t for t in selected if t.test_id in regression_ids]
        elif gcov_scope == "fail+regression":
            selected_phase_b = [t for t in selected if t.test_id in fail_ids or t.test_id in regression_ids]
        else:
            selected_phase_b = selected

        # Phase B: non-ASAN + gcov để lấy covered_functions ổn định.
        cov_by_test: Dict[str, List[str]] = {}
        if skip_coverage:
            log("  [phaseB] skip vì --skip-coverage")
        elif not selected_phase_b:
            log(f"  [phaseB] không có test nào theo gcov_scope={gcov_scope}")
        else:
            log(f"  [phaseB] checkout+build GCOV (scope={gcov_scope}, tests={len(selected_phase_b)})")
            try:
                checkout_buggy(repo_dir, bug)
            except Exception as exc:
                log(f"  [warn] phaseB checkout buggy lỗi: {exc}")
                selected_phase_b = []
            if selected_phase_b and compile_with_coverage(repo_dir, jobs=jobs, asan=False):
                results_phase_b, n_cov_b = _run_tests_with_optional_coverage(
                    repo_dir, tests_dir, selected_phase_b,
                    test_timeout=test_timeout,
                    collect_cov=True,
                    cov_verbose_first_n=2,
                    phase_label="phaseB",
                )
                cov_by_test = {r.test_id: r.covered_functions for r in results_phase_b}
                phase_info["phase_b_with_coverage"] = n_cov_b
            elif selected_phase_b:
                log("  [warn] phaseB build thất bại, giữ covered_functions rỗng.")

        # Merge: outcome từ A, coverage từ B.
        results = []
        for r in results_phase_a:
            results.append(TestResult(
                test_id=r.test_id,
                outcome=r.outcome,
                outcome_fixed=fixed_outcome_by_test.get(r.test_id, ""),
                fail_reason=r.fail_reason,
                actual_output=r.actual_output,
                expected_output=r.expected_output,
                covered_functions=cov_by_test.get(r.test_id, []),
            ))
        phase_info["phase_a_fail_count"] = sum(1 for r in results_phase_a if r.outcome == "FAIL")
        phase_info["phase_b_scope"] = gcov_scope
        phase_info["phase_b_test_count"] = len(selected_phase_b)
    else:
        # Single-phase như cũ.
        if not compile_with_coverage(repo_dir, jobs=jobs, asan=asan):
            record = _empty_record(bug, repo_dir, compile_cmd, error="compile_failed")
            _write_meta(raw_out_path, record)
            _write_meta(out_path, record)
            return out_path
        results, n_with_cov = _run_tests_with_optional_coverage(
            repo_dir, tests_dir, selected,
            test_timeout=test_timeout,
            collect_cov=not skip_coverage,
            cov_verbose_first_n=2,
            phase_label="tests",
        )
        phase_info["single_with_coverage"] = n_with_cov

    # --- Ghi meta ---
    source_file = str(repo_dir / bug.src_files[0]) if bug.src_files else ""
    ground_truth_functions = _extract_ground_truth_funcs(bug, repo_dir)

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
    }

    raw_record = {
        **base_record,
        "tests": [_test_to_dict(r) for r in results],
    }
    _write_meta(raw_out_path, raw_record)
    log(f"  [ok] wrote raw {raw_out_path}")

    _write_meta(out_path, raw_record)
    log(f"  [ok] wrote {out_path}")
    return out_path


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


def _write_meta(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Ground-truth extraction (best-effort)
# ---------------------------------------------------------------------------
def _extract_ground_truth_funcs(bug: BugEntry, repo_dir: Path) -> List[str]:
    """Cố gắng lấy tên hàm được sửa từ diff ``commit_before..commit_after``."""
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
            # "@@ -a,b +c,d @@ int my_func(args)"
            tail = line.split("@@", 2)[-1].strip()
            m = re.match(r".*?\b([A-Za-z_]\w*)\s*\(", tail)
            if m:
                name = m.group(1)
                if name not in c_keywords:
                    funcs.add(name)
    return sorted(funcs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="build_meta_tcpdump.py",
        description="Sinh metadata Unified-Debugging cho tcpdump / Defects4C.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--sha", action="append", default=[],
        help="Chỉ xử lý bug với commit_after khớp (có thể lặp, hỗ trợ prefix).",
    )
    ap.add_argument(
        "--only", dest="sha", action="append",
        help="Alias của --sha.",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Giới hạn số bug xử lý (0 = tất cả).",
    )
    ap.add_argument(
        "--out-root", type=Path, default=DEFAULT_OUT_ROOT,
        help=f"Thư mục chứa git_repo_dir_<sha> (mặc định: {DEFAULT_OUT_ROOT}).",
    )
    ap.add_argument(
        "--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR,
        help=f"Nơi ghi metadata cho Unified-Debugging (mặc định: {DEFAULT_METADATA_DIR}).",
    )
    ap.add_argument(
        "--raw-dir", type=Path, default=DEFAULT_RAW_DIR,
        help=f"Nơi ghi raw output (cùng nội dung với metadata, mặc định: {DEFAULT_RAW_DIR}).",
    )
    ap.add_argument(
        "--jobs", type=int, default=max(os.cpu_count() or 2, 2) - 1,
        help="Số job khi make (mặc định: ncpu-1).",
    )
    ap.add_argument(
        "--max-pass", type=int, default=-1,
        help="Tối đa số test ngoài regression chạy (-1 = tất cả, mặc định).",
    )
    ap.add_argument(
        "--test-timeout", type=int, default=60,
        help="Timeout mỗi TESTonce (giây).",
    )
    ap.add_argument(
        "--skip-coverage", action="store_true",
        help="Không thu thập gcov (nhanh hơn, mất covered_functions).",
    )
    ap.add_argument(
        "--asan", action="store_true",
        help="Build + test với AddressSanitizer trên toàn bộ test run.",
    )
    ap.add_argument(
        "--dual-run", action="store_true",
        help="Chạy 2-phase: ASAN (labels) + GCOV non-ASAN (coverage).",
    )
    ap.add_argument(
        "--gcov-scope",
        choices=["all", "fail", "regression", "fail+regression"],
        default="fail+regression",
        help="Khi --dual-run, chọn tập test cho phase GCOV (mặc định: fail+regression).",
    )
    ap.add_argument(
        "--skip-if-exists", action="store_true",
        help="Bỏ qua bug đã có {bug_id}_meta.json trong metadata-dir.",
    )
    ap.add_argument(
        "--clone", action="store_true",
        help="Tự clone tcpdump từ GitHub nếu chưa có repo.",
    )
    ap.add_argument(
        "--list", action="store_true",
        help="Chỉ liệt kê bug rồi thoát.",
    )
    return ap


def main(argv=None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)

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
        for b in bugs:
            print(f"{b.bug_id}\t{b.cve_name or '-'}\t{','.join(b.src_files)}")
        return 0

    if not bugs:
        log("Không có bug nào khớp điều kiện.")
        return 1

    log(f"Sẽ xử lý {len(bugs)} bug.")
    log(f"  metadata_dir = {args.metadata_dir}")
    log(f"  raw_dir      = {args.raw_dir}")
    log(f"  asan         = {args.asan}")
    log(f"  dual_run     = {args.dual_run}")
    if args.dual_run:
        log(f"  gcov_scope   = {args.gcov_scope}")
    args.metadata_dir.mkdir(parents=True, exist_ok=True)
    args.raw_dir.mkdir(parents=True, exist_ok=True)

    lock_file = _lock_path_for_metadata_dir(args.metadata_dir)
    lock_fp = None
    try:
        lock_fp = _acquire_single_run_lock(lock_file)
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
                    max_pass=args.max_pass,
                    test_timeout=args.test_timeout,
                    skip_if_exists=args.skip_if_exists,
                    clone_if_missing=args.clone,
                    skip_coverage=args.skip_coverage,
                    asan=args.asan,
                    dual_run=args.dual_run,
                    gcov_scope=args.gcov_scope,
                )
                if out is None:
                    fail_count += 1
            except KeyboardInterrupt:
                log("Bị ngắt bởi người dùng.")
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

    log(f"HOÀN TẤT. fail={fail_count}/{len(bugs)}")
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
