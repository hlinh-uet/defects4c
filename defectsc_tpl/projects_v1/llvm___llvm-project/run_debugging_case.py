#!/usr/bin/env python3
"""Prepare LLVM Defects4C cases for the public Debugging-Framework CLI.

The runner keeps the complete llvm-project source tree.  It uses a partial Git
cache plus ``git archive`` so a materialized input does not carry LLVM's very
large history.  A prepared case contains only the buggy workspace; build
artifacts are deliberately removed before the input is published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTS4C_ROOT = PROJECT_DIR.parents[2]
DEFAULT_INPUTS_ROOT = (
    DEFECTS4C_ROOT
    / "out_tmp_dirs"
    / "debugging_framework"
    / "llvm"
    / "inputs"
)
DEFAULT_CACHE_ROOT = DEFECTS4C_ROOT / "out_tmp_dirs"
DEFAULT_FRAMEWORK_BIN = (
    DEFECTS4C_ROOT.parent
    / "Debugging-Framework"
    / ".venv"
    / "bin"
    / "debugging-framework"
)
BUGS_PATH = PROJECT_DIR / "bugs_list_new.json"
PROJECT_META_PATH = PROJECT_DIR / "project.json"
DEFAULT_IMAGE = "llvm/defect4c:latest"
PROJECT_NAME = "llvm___llvm-project"
BUILD_DIR = ".debugging-framework/build"
LIT_ADAPTER_COMMAND = "defects4c-llvm-lit"
REGRESSION_TEST_LIMIT = 70
TEST_EVIDENCE_PATTERN = (
    r"Testing Time:|Expected Passes\s*:\s*[1-9]|Unexpected Failures\s*:\s*[1-9]|"
    r"Unsupported Tests\s*:\s*[1-9]|^(?:PASS|FAIL|XPASS|XFAIL|UNSUPPORTED):|"
    r"^(?:PASSED|FAILED)\s+\S+"
)
TEST_FAILURE_PATTERN = (
    r"^(?:FAIL|XPASS):|Unexpected Failures\s*:\s*[1-9]|"
    r"Failed Tests \([1-9][0-9]*\)|^FAILED\s+\S+"
)

if str(DEFECTS4C_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFECTS4C_ROOT))

import prepare_project as materializer  # noqa: E402


def load_bugs() -> list[dict]:
    value = read_json(BUGS_PATH)
    if not isinstance(value, list):
        raise ValueError(f"Bug list must be an array: {BUGS_PATH}")
    return value


def select_bugs(
    bugs: list[dict], selectors: Iterable[str], select_all: bool
) -> list[dict]:
    if select_all:
        return list(bugs)
    return [materializer.select_bug(bugs, selector) for selector in selectors]


def print_bug_list(bugs: list[dict]) -> None:
    print("case_id\tbug_id\tcommit_after\ttests\tsource_files")
    for bug in bugs:
        print(
            "\t".join(
                (
                    case_id_for(bug),
                    bug_id_for(bug),
                    required_sha(bug, "commit_after"),
                    ",".join(candidate_tests(bug)),
                    ",".join(materializer.source_files(bug)),
                )
            )
        )


def prepare_case(
    *,
    bug: dict,
    project_meta: dict,
    inputs_root: Path,
    cache_root: Path,
    runtime: str,
    image: str,
    jobs: int,
    command_timeout: int,
    build_type: str,
    force: bool,
) -> tuple[Path, Path, Path]:
    case_id = case_id_for(bug)
    final_project, final_config, final_failure = input_paths(inputs_root, case_id)
    if input_ready(final_project, final_config, final_failure):
        if not force:
            return final_project, final_config, final_failure
    elif any(path.exists() for path in (final_project, final_config, final_failure)) and not force:
        raise RuntimeError(
            f"Input cũ hoặc chưa hoàn chỉnh: {final_project}; dùng --force để tạo lại"
        )

    inputs_root.mkdir(parents=True, exist_ok=True)
    if force:
        remove_project_input(final_project, inputs_root, case_id)
        remove_file_input(final_config, inputs_root, f"{case_id}.debugging-framework.json")
        remove_file_input(final_failure, inputs_root, f"{case_id}.failure.log")

    staging = Path(tempfile.mkdtemp(prefix=f".{case_id}.preparing-", dir=inputs_root))
    project_root = staging / case_id
    config_path = staging / f"{case_id}.debugging-framework.json"
    failure_path = staging / f"{case_id}.failure.log"
    work_dir = staging / ".prepare-work"
    work_dir.mkdir()

    commit_after = required_sha(bug, "commit_after")
    commit_before = required_sha(bug, "commit_before")
    source_files = materializer.source_files(bug)
    declared_tests = candidate_tests(bug)
    if not declared_tests:
        raise ValueError(f"{case_id}: no LLVM lit test declared")

    try:
        source_repo = ensure_source_cache(
            cache_root=cache_root,
            remote=str(project_meta.get("main_repo") or ""),
            commits=(commit_after, commit_before),
        )
        materialize_snapshot(
            source_repo=source_repo,
            target=project_root,
            commit_after=commit_after,
            commit_before=commit_before,
            source_files=source_files,
        )
        validate_test_paths(project_root, declared_tests)
        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=project_root,
            commands=validation_build_commands(jobs, build_type),
            log_path=work_dir / "buggy-build.log",
            timeout=command_timeout,
        )
        failed_tests, failure_sections = observe_buggy_tests(
            runtime=runtime,
            image=image,
            project_root=project_root,
            tests=declared_tests,
            log_path=work_dir / "buggy-tests.log",
            timeout=command_timeout,
        )
        if not failed_tests:
            raise RuntimeError(
                f"{case_id}: none of the declared LLVM tests failed: "
                + ", ".join(declared_tests)
            )
        discovered_tests = discover_llvm_tests(
            runtime=runtime,
            image=image,
            project_root=project_root,
            log_path=work_dir / "test-discovery.log",
            timeout=command_timeout,
        )
        regression_tests = select_regression_tests(
            discovered_tests,
            excluded_tests=declared_tests,
            seed=case_id,
        )
        if not regression_tests:
            raise RuntimeError(f"{case_id}: no supplemental LLVM regression tests found")
        buggy_regression_outcomes = observe_regression_tests(
            runtime=runtime,
            image=image,
            project_root=project_root,
            tests=regression_tests,
            log_path=work_dir / "buggy-regression-tests.log",
            timeout=command_timeout,
        )

        # Do not hold two LLVM build trees at once. The published project must
        # be clean anyway, so discard the buggy build before checking fixed.
        clear_build_artifacts(project_root)
        fixed_results, fixed_regression_outcomes = verify_fixed_oracle(
            source_repo=source_repo,
            staging=staging,
            bug=bug,
            runtime=runtime,
            image=image,
            jobs=jobs,
            build_type=build_type,
            target_tests=failed_tests,
            regression_tests=regression_tests,
            timeout=command_timeout,
            log_path=work_dir / "fixed-verification.log",
        )
        eligible_tests = [test for test in failed_tests if fixed_results.get(test) == "passed"]
        excluded = [test for test in failed_tests if test not in eligible_tests]
        if excluded:
            print(
                f"[filter] {case_id}: loại test không pass trên fixed: "
                + ", ".join(excluded),
                flush=True,
            )
        if not eligible_tests:
            raise RuntimeError(
                f"{case_id}: no LLVM test has the required buggy-fail/fixed-pass outcome"
            )

        excluded_regression_tests = sorted(
            test
            for test in regression_tests
            if buggy_regression_outcomes.get(test) != "passed"
            or fixed_regression_outcomes.get(test) != "passed"
        )
        if excluded_regression_tests:
            print(
                f"[filter] {case_id}: loại khỏi regression vì không pass trên "
                "cả buggy và fixed: " + ", ".join(excluded_regression_tests),
                flush=True,
            )
        if len(excluded_regression_tests) == len(regression_tests):
            raise RuntimeError(
                f"{case_id}: no supplemental LLVM regression test passes on both "
                "buggy and fixed"
            )

        failure_path.write_text(
            "\n".join(failure_sections[test].rstrip() for test in eligible_tests).rstrip()
            + "\n",
            encoding="utf-8",
        )
        write_framework_config(
            config_path,
            failing_tests=eligible_tests,
            image=image,
            runtime=runtime,
            jobs=jobs,
            build_type=build_type,
            regression_tests=regression_tests,
            excluded_regression_tests=excluded_regression_tests,
        )

        clear_build_artifacts(project_root)
        shutil.rmtree(work_dir)
        project_root.rename(final_project)
        config_path.rename(final_config)
        failure_path.rename(final_failure)
        staging.rmdir()
        return final_project, final_config, final_failure
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if not input_ready(final_project, final_config, final_failure):
            remove_project_input(final_project, inputs_root, case_id)
            remove_file_input(final_config, inputs_root, f"{case_id}.debugging-framework.json")
            remove_file_input(final_failure, inputs_root, f"{case_id}.failure.log")
        raise


def ensure_source_cache(
    *, cache_root: Path, remote: str, commits: Iterable[str]
) -> Path:
    """Return one shared partial clone and make selected revisions available."""
    if not remote:
        raise ValueError("llvm project.json has no main_repo")
    cache = cache_root / PROJECT_NAME / "source-cache"
    if not (cache / ".git").is_dir():
        if cache.exists():
            raise RuntimeError(f"LLVM cache path exists but is not a Git repository: {cache}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        run_checked(
            ["git", "clone", "--filter=blob:none", "--no-checkout", remote, str(cache)],
            timeout=60 * 60,
        )
    for sha in commits:
        if git_has_commit(cache, sha):
            continue
        run_checked(
            ["git", "-C", str(cache), "fetch", "--filter=blob:none", "origin", sha],
            timeout=60 * 30,
        )
        if not git_has_commit(cache, sha):
            raise RuntimeError(f"Fetched LLVM cache does not contain commit: {sha}")
    return cache.resolve()


def materialize_snapshot(
    *,
    source_repo: Path,
    target: Path,
    commit_after: str,
    commit_before: str,
    source_files: list[str],
) -> None:
    """Export the fixed tree, then overlay buggy source without Git history."""
    if target.exists():
        raise FileExistsError(f"Target already exists: {target}")
    target.mkdir(parents=True)
    process = subprocess.Popen(
        ["git", "-C", str(source_repo), "archive", "--format=tar", commit_after],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError(f"Unsafe path in LLVM Git archive: {member.name}")
                archive.extract(member, path=target)
    except BaseException:
        process.kill()
        process.wait()
        shutil.rmtree(target, ignore_errors=True)
        raise
    error = process.stderr.read().decode("utf-8", errors="replace").strip()
    if process.wait() != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"Cannot export LLVM commit {commit_after}: {error}")

    for relpath in source_files:
        completed = subprocess.run(
            ["git", "-C", str(source_repo), "show", f"{commit_before}:{relpath}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Cannot read buggy source {commit_before}:{relpath}: {detail}")
        destination = target / relpath
        if not destination.is_file():
            raise FileNotFoundError(f"Buggy overlay target does not exist: {destination}")
        mode = destination.stat().st_mode
        destination.write_bytes(completed.stdout)
        destination.chmod(mode)


def validation_build_commands(jobs: int, build_type: str) -> list[list[str]]:
    return [
        [
            "cmake", "-G", "Ninja", "-S", "llvm", "-B", BUILD_DIR,
            f"-DCMAKE_BUILD_TYPE={build_type}",
            "-DBUILD_SHARED_LIBS=ON",
            "-DLLVM_ENABLE_ASSERTIONS=ON",
            "-DLLVM_TARGETS_TO_BUILD=X86",
            "-DLLVM_INCLUDE_TESTS=ON",
            "-DLLVM_BUILD_TESTS=ON",
            "-DLLVM_INCLUDE_BENCHMARKS=OFF",
            "-DLLVM_INCLUDE_EXAMPLES=OFF",
            "-DLLVM_ENABLE_BINDINGS=OFF",
            "-DLLVM_OPTIMIZED_TABLEGEN=ON",
            "-DLLVM_CCACHE_BUILD=ON",
            "-DLLVM_PARALLEL_LINK_JOBS=1",
        ],
        [
            "cmake", "--build", BUILD_DIR,
            "--target", "llvm-test-depends",
            "--parallel", str(jobs),
        ],
    ]


def llvm_lit_command(
    test: str | None = None,
    *,
    tests: Iterable[str] = (),
    excluded_tests: Iterable[str] = (),
) -> list[str]:
    command = [
        LIT_ADAPTER_COMMAND,
        "--build-dir", BUILD_DIR,
    ]
    selected = list(tests)
    if test is not None and selected:
        raise ValueError("Cannot combine one LLVM lit test with a regression test set")
    for excluded in excluded_tests:
        command.extend(["--exclude-test", excluded])
    for selected_test in selected:
        command.extend(["--test", selected_test])
    if test is not None:
        command.append(test)
    if test is None and not selected:
        raise ValueError("LLVM lit command requires at least one selected test")
    return command


def llvm_lit_discovery_command() -> list[str]:
    return [
        LIT_ADAPTER_COMMAND,
        "--build-dir", BUILD_DIR,
        "--list-tests", "llvm/test",
    ]


def write_framework_config(
    path: Path,
    *,
    failing_tests: Iterable[str],
    image: str,
    runtime: str,
    jobs: int,
    build_type: str,
    regression_tests: Iterable[str],
    excluded_regression_tests: Iterable[str] = (),
) -> None:
    selected_regression_tests = list(dict.fromkeys(regression_tests))
    excluded = sorted(set(excluded_regression_tests))
    if not 1 <= len(selected_regression_tests) <= REGRESSION_TEST_LIMIT:
        raise ValueError(
            "LLVM regression set must contain between 1 and "
            f"{REGRESSION_TEST_LIMIT} tests"
        )
    if not set(excluded).issubset(selected_regression_tests):
        raise ValueError("Excluded LLVM regression tests must be in the selected set")
    if len(excluded) == len(selected_regression_tests):
        raise ValueError("At least one LLVM regression test must remain after exclusions")
    config = {
        "schema_version": 6,
        "system": "cmake",
        "setup": [validation_build_commands(jobs, build_type)[0]],
        "build": [validation_build_commands(jobs, build_type)[1]],
        "target_test": [
            {
                "command": llvm_lit_command("{test_id}"),
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "regression_test": [
            {
                "command": llvm_lit_command(
                    tests=selected_regression_tests,
                    excluded_tests=excluded,
                ),
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "repair": {"failing_tests": list(failing_tests)},
        "workspace": {
            "disposable": True,
            "initialize_git_if_missing": True,
        },
        "environment": {"mode": "image", "runtime": runtime, "image": image},
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def discover_llvm_tests(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    log_path: Path,
    timeout: int,
) -> list[str]:
    command = llvm_lit_discovery_command()
    result = run_in_image(runtime, image, project_root, command, timeout=timeout)
    log_path.write_text(
        command_section(command, result.returncode, result.stdout), encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(f"LLVM lit discovery failed; see {log_path}")
    discovered = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(r"^DISCOVERED\s+(\S+)\s*$", result.stdout, re.MULTILINE)
            if match.group(1).startswith("llvm/test/")
        }
    )
    if not discovered:
        raise RuntimeError(f"LLVM lit discovered zero tests; see {log_path}")
    return discovered


def select_regression_tests(
    discovered_tests: Iterable[str],
    *,
    excluded_tests: Iterable[str],
    seed: str,
    limit: int = REGRESSION_TEST_LIMIT,
) -> list[str]:
    if limit < 1:
        raise ValueError("LLVM regression test limit must be >= 1")
    excluded = set(excluded_tests)
    candidates = sorted(
        {
            test.strip()
            for test in discovered_tests
            if test.strip().startswith("llvm/test/") and test.strip() not in excluded
        },
        key=lambda test: (hashlib.sha256(f"{seed}\0{test}".encode()).digest(), test),
    )
    return candidates[:limit]


def observe_regression_tests(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    tests: list[str],
    log_path: Path,
    timeout: int,
    append: bool = False,
) -> dict[str, str]:
    if not tests:
        raise ValueError("Cannot observe an empty LLVM regression test set")
    command = llvm_lit_command(tests=tests)
    result = run_in_image(runtime, image, project_root, command, timeout=timeout)
    with log_path.open("a" if append else "w", encoding="utf-8") as log:
        log.write(command_section(command, result.returncode, result.stdout))
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            f"LLVM regression selection exited {result.returncode}; see {log_path}"
        )
    if not lit_suite_observed(result.stdout):
        raise RuntimeError(f"LLVM regression selection was not observed; see {log_path}")
    outcomes = lit_adapter_outcomes(result.stdout)
    missing = [test for test in tests if test not in outcomes]
    if missing:
        raise RuntimeError(
            "LLVM regression outcomes are missing for: " + ", ".join(missing)
        )
    return {test: outcomes[test] for test in tests}


def lit_adapter_outcomes(output: str) -> dict[str, str]:
    labels = {"PASSED": "passed", "FAILED": "failed", "SKIPPED": "skipped"}
    return {
        match.group(2): labels[match.group(1)]
        for match in re.finditer(
            r"^(PASSED|FAILED|SKIPPED)\s+(\S+)\s*$", output, re.MULTILINE
        )
    }


def observe_buggy_tests(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    tests: list[str],
    log_path: Path,
    timeout: int,
) -> tuple[list[str], dict[str, str]]:
    failed: list[str] = []
    sections: dict[str, str] = {}
    with log_path.open("w", encoding="utf-8") as log:
        for test in tests:
            command = llvm_lit_command(test)
            result = run_in_image(runtime, image, project_root, command, timeout=timeout)
            section = command_section(command, result.returncode, result.stdout)
            log.write(section)
            if not lit_test_observed(result.stdout, test):
                raise RuntimeError(f"LLVM lit test was not observed: {test}")
            if result.returncode == 0:
                continue
            if not re.search(TEST_FAILURE_PATTERN, result.stdout, re.MULTILINE):
                raise RuntimeError(
                    f"LLVM lit test {test} exited {result.returncode} without failure evidence"
                )
            failed.append(test)
            sections[test] = f"===== failing_test: {test} =====\n{result.stdout.rstrip()}\n"
    return failed, sections


def verify_fixed_oracle(
    *,
    source_repo: Path,
    staging: Path,
    bug: dict,
    runtime: str,
    image: str,
    jobs: int,
    build_type: str,
    target_tests: list[str],
    regression_tests: list[str],
    timeout: int,
    log_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    fixed_root = staging / ".fixed-verification-project"
    commit_after = required_sha(bug, "commit_after")
    materialize_snapshot(
        source_repo=source_repo,
        target=fixed_root,
        commit_after=commit_after,
        commit_before=commit_after,
        source_files=materializer.source_files(bug),
    )
    results: dict[str, str] = {}
    try:
        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            commands=validation_build_commands(jobs, build_type),
            log_path=log_path,
            timeout=timeout,
        )
        with log_path.open("a", encoding="utf-8") as log:
            for test in target_tests:
                command = llvm_lit_command(test)
                result = run_in_image(runtime, image, fixed_root, command, timeout=timeout)
                log.write(command_section(command, result.returncode, result.stdout))
                if not lit_test_observed(result.stdout, test):
                    raise RuntimeError(f"Fixed LLVM lit test was not observed: {test}")
                results[test] = "passed" if result.returncode == 0 and lit_test_passed(
                    result.stdout, test
                ) else "failed"

        regression_outcomes = observe_regression_tests(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            tests=regression_tests,
            log_path=log_path,
            timeout=timeout,
            append=True,
        )
        return results, regression_outcomes
    finally:
        shutil.rmtree(fixed_root, ignore_errors=True)


def lit_test_observed(output: str, test: str) -> bool:
    relative = test.removeprefix("llvm/test/")
    return (
        relative in output
        and (test in output or f"LLVM :: {relative}" in output)
        and lit_suite_observed(output)
    )


def lit_test_passed(output: str, test: str) -> bool:
    if not lit_test_observed(output, test):
        return False
    relative = re.escape(test.removeprefix("llvm/test/"))
    return bool(
        re.search(rf"^PASSED\s+{re.escape(test)}$", output, re.MULTILINE)
        or
        re.search(rf"^PASS:.*{relative}", output, re.MULTILINE)
        or re.search(r"Expected Passes\s*:\s*[1-9]", output)
    ) and not re.search(TEST_FAILURE_PATTERN, output, re.MULTILINE)


def lit_suite_observed(output: str) -> bool:
    if re.search(r"(?:Testing:|tests?, 0 workers|No tests?)", output, re.IGNORECASE):
        if re.search(r"Testing:\s*0 tests|No tests?", output, re.IGNORECASE):
            return False
    return bool(re.search(TEST_EVIDENCE_PATTERN, output, re.MULTILINE))


def validate_test_paths(project_root: Path, tests: Iterable[str]) -> None:
    for test in tests:
        relative = Path(test)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe LLVM test path: {test}")
        if relative.suffix not in {".ll", ".mir", ".s", ".c", ".cpp", ".test", ".yaml"}:
            raise ValueError(f"Unsupported LLVM lit test path: {test}")
        if not (project_root / relative).is_file():
            raise FileNotFoundError(f"Declared LLVM lit test is missing: {test}")


def candidate_tests(bug: dict) -> list[str]:
    raw = (bug.get("c_compile") or {}).get("test_flags") or []
    raw = [raw] if isinstance(raw, str) else raw
    tests: list[str] = []
    for value in raw:
        test = str(value).strip().replace("\\", "/")
        if test and test not in tests:
            tests.append(test)
    return tests


def run_commands_logged(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    commands: Iterable[list[str]],
    log_path: Path,
    timeout: int,
) -> None:
    with log_path.open("w", encoding="utf-8") as log:
        for command in commands:
            result = run_in_image(runtime, image, project_root, command, timeout=timeout)
            log.write(command_section(command, result.returncode, result.stdout))
            if result.returncode != 0:
                raise RuntimeError(
                    f"Command failed ({result.returncode}); see {log_path}: "
                    + shlex.join(command)
                )


def run_in_image(
    runtime: str,
    image: str,
    project_root: Path,
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess:
    if sys.platform == "darwin":
        wrapper = (
            'workspace="/tmp/debugging-framework-workspace"; '
            'mkdir -p "$workspace" || exit $?; '
            'cp -a /input/. "$workspace"/ || exit $?; '
            'cd "$workspace" || exit $?; '
            '"$@"; command_status=$?; '
            'cd /tmp || exit $?; '
            'cp -a "$workspace"/. /input/; copy_status=$?; '
            '[ "$copy_status" -eq 0 ] || exit "$copy_status"; '
            'exit "$command_status"'
        )
        argv = [
            runtime, "run", "--rm", "--network=none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp",
            "-v", f"{project_root.resolve()}:/input:rw",
            "-w", "/tmp", image,
            "sh", "-c", wrapper, "debugging-framework", *command,
        ]
    else:
        argv = [
            runtime, "run", "--rm", "--network=none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp",
            "-v", f"{project_root.resolve()}:/workspace:rw",
            "-w", "/workspace", image, *command,
        ]
    try:
        return subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = str(exc.stdout or "") + str(exc.stderr or "")
        raise RuntimeError(
            f"Command timed out after {timeout}s: {shlex.join(command)}\n{output}"
        ) from exc


def command_section(command: list[str], returncode: int, output: str) -> str:
    return (
        f"===== command: {shlex.join(command)} =====\n"
        f"returncode: {returncode}\n{output.rstrip()}\n\n"
    )


def clear_build_artifacts(project_root: Path) -> None:
    path = project_root / ".debugging-framework"
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        raise RuntimeError(f"Build artifact path is not a directory: {path}")


def input_ready(project_root: Path, config_path: Path, failure_path: Path) -> bool:
    return (
        project_root.is_dir()
        and config_path.is_file()
        and framework_config_ready(config_path)
        and not (project_root / ".git").exists()
        and not (project_root / ".debugging-framework.json").exists()
        and failure_path.is_file()
        and failure_path.stat().st_size > 0
    )


def framework_config_ready(config_path: Path) -> bool:
    try:
        value = read_json(config_path)
    except ValueError:
        return False
    if not isinstance(value, dict) or value.get("schema_version") != 6:
        return False
    regression = value.get("regression_test")
    repair = value.get("repair")
    environment = value.get("environment")
    workspace = value.get("workspace")
    if not (
        isinstance(regression, list)
        and len(regression) == 1
        and isinstance(repair, dict)
        and repair.get("failing_tests")
        and isinstance(environment, dict)
        and environment.get("mode") == "image"
        and environment.get("image")
        and isinstance(workspace, dict)
        and workspace.get("disposable") is True
        and workspace.get("initialize_git_if_missing") is True
    ):
        return False
    entry = regression[0]
    command = entry.get("command") if isinstance(entry, dict) else entry
    if isinstance(command, str):
        arguments = shlex.split(command)
    elif isinstance(command, list):
        arguments = [str(argument) for argument in command]
    else:
        return False
    selected = [
        arguments[index + 1]
        for index, argument in enumerate(arguments[:-1])
        if argument == "--test"
    ]
    excluded = [
        arguments[index + 1]
        for index, argument in enumerate(arguments[:-1])
        if argument == "--exclude-test"
    ]
    return bool(
        1 <= len(selected) <= REGRESSION_TEST_LIMIT
        and len(selected) == len(set(selected))
        and set(excluded).issubset(selected)
        and len(set(excluded)) < len(selected)
        and "llvm/test" not in arguments
        and not any("{test_id}" in argument for argument in arguments)
    )


def remove_project_input(project_root: Path, inputs_root: Path, case_id: str) -> None:
    if not project_root.exists():
        return
    resolved_root = inputs_root.resolve()
    resolved_project = project_root.resolve()
    if resolved_project.parent != resolved_root or resolved_project.name != case_id:
        raise RuntimeError(f"Từ chối xóa input path không an toàn: {resolved_project}")
    shutil.rmtree(resolved_project)


def upgrade_workspace_contract(config_path: Path) -> None:
    """Upgrade already-prepared inputs without rebuilding the benchmark case."""
    if not config_path.is_file():
        return
    value = read_json(config_path)
    if not isinstance(value, dict):
        return
    workspace = {
        "disposable": True,
        "initialize_git_if_missing": True,
    }
    if value.get("workspace") != workspace:
        value["workspace"] = workspace
        config_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def remove_file_input(path: Path, inputs_root: Path, expected_name: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.parent.resolve() != inputs_root.resolve() or path.name != expected_name:
        raise RuntimeError(f"Từ chối xóa input file không an toàn: {path}")
    if not path.is_file() and not path.is_symlink():
        raise RuntimeError(f"Input file path không phải file: {path}")
    path.unlink()


def case_id_for(bug: dict) -> str:
    return materializer.safe_name(
        f"{bug_id_for(bug)}__{required_sha(bug, 'commit_after')[:12]}"
    )


def bug_id_for(bug: dict) -> str:
    return str((bug.get("type") or {}).get("id") or required_sha(bug, "commit_after")[:12])


def required_sha(bug: dict, key: str) -> str:
    return materializer.required_sha(bug, key)


def git_has_commit(repo: Path, sha: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def run_checked(command: list[str], *, timeout: int) -> None:
    completed = subprocess.run(command, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {shlex.join(command)}")


def resolve_runtime(value: str) -> str:
    executable = shutil.which(value) if value else None
    if not executable:
        raise FileNotFoundError(f"OCI runtime not found: {value}")
    completed = subprocess.run(
        [executable, "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"OCI runtime is not ready: {value}")
    return value


def inspect_image(runtime: str, image: str) -> str:
    completed = subprocess.run(
        [runtime, "image", "inspect", "--format", "{{.Id}}", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        detail = value.splitlines()
        raise RuntimeError(
            "Prepared OCI image is unavailable: " + (detail[-1] if detail else image)
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create LLVM inputs, then call the public Debugging-Framework CLI."
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="trial",
        choices=("prepare", "show", "doctor", "repair", "trial"),
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--bug", "--sha", dest="selectors", action="append",
        help="Unique bug type.id or commit_after prefix; repeatable.",
    )
    selection.add_argument("--all", action="store_true", help="Process all 143 bugs.")
    selection.add_argument("--list", action="store_true", help="List bugs and exit.")
    parser.add_argument("--inputs-root", type=Path, default=DEFAULT_INPUTS_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--jobs", type=int, help="Prepare uses 4 when omitted.")
    parser.add_argument("--attempts", type=int)
    parser.add_argument(
        "--command-timeout", type=int,
        help="Prepare uses 7200 seconds per LLVM command when omitted.",
    )
    parser.add_argument("--codex-timeout", type=int)
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--build-type", choices=("Release", "Debug", "RelWithDebInfo"), default="Release",
        help="Release is the disk-conscious default; assertions remain enabled.",
    )
    parser.add_argument("--framework-bin", type=Path, default=DEFAULT_FRAMEWORK_BIN)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_args(args)
    bugs = load_bugs()
    if args.list:
        print_bug_list(bugs)
        return 0
    selected = select_bugs(bugs, args.selectors or (), args.all)
    framework_bin = None
    if args.action not in {"prepare", "show"}:
        framework_bin = resolve_framework_bin(args.framework_bin)
    inputs_root = args.inputs_root.expanduser().resolve()
    runtime = ""
    image_id = ""
    if args.action in {"prepare", "trial"}:
        runtime = resolve_runtime(args.runtime)
        image_id = inspect_image(runtime, args.image)

    overall_returncode = 0
    prepare_jobs = args.jobs if args.jobs is not None else 4
    prepare_timeout = args.command_timeout if args.command_timeout is not None else 7200
    for index, bug in enumerate(selected, start=1):
        case_id = case_id_for(bug)
        project_root, config_path, failure_path = input_paths(inputs_root, case_id)
        print(f"[{index}/{len(selected)}] case {case_id}", flush=True)
        try:
            upgrade_workspace_contract(config_path)
            if args.action in {"prepare", "trial"}:
                project_root, config_path, failure_path = prepare_case(
                    bug=bug,
                    project_meta=read_json(PROJECT_META_PATH),
                    inputs_root=inputs_root,
                    cache_root=args.cache_root.expanduser().resolve(),
                    runtime=runtime,
                    image=image_id,
                    jobs=prepare_jobs,
                    command_timeout=prepare_timeout,
                    build_type=args.build_type,
                    force=args.force,
                )
            upgrade_workspace_contract(config_path)
            require_inputs(project_root, config_path, failure_path)
            print_contract(project_root, config_path, failure_path)

            if args.action == "prepare":
                continue
            if args.action == "show":
                display_bin = resolve_framework_for_display(args.framework_bin)
                print_commands(args, display_bin, project_root, config_path, failure_path)
                continue

            assert framework_bin is not None
            if args.action in {"doctor", "trial"}:
                doctor = [
                    str(framework_bin), "doctor", str(project_root),
                    "--config", str(config_path),
                ]
                if args.jobs is not None:
                    doctor.extend(["--jobs", str(args.jobs)])
                print("[Debugging-Framework] doctor", flush=True)
                returncode = run_command(doctor)
                if returncode != 0:
                    overall_returncode = returncode
                    if not args.continue_on_error:
                        break
                    continue
                if args.action == "doctor":
                    continue

            print("[Debugging-Framework] repair", flush=True)
            returncode = run_command(
                build_repair_command(
                    args, framework_bin, project_root, config_path, failure_path
                )
            )
            if returncode != 0:
                overall_returncode = returncode
                if not args.continue_on_error:
                    break
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            overall_returncode = 2
            print(f"[ERROR] {case_id}: {exc}", file=sys.stderr)
            if not args.continue_on_error:
                break
    return overall_returncode


def validate_args(args: argparse.Namespace) -> None:
    for name in ("jobs", "attempts", "command_timeout", "codex_timeout"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise ValueError(f"--{name.replace('_', '-')} must be >= 1")
    if args.force and args.action not in {"prepare", "trial"}:
        raise ValueError("--force chỉ dùng với prepare hoặc trial")


def input_paths(inputs_root: Path, case_id: str) -> tuple[Path, Path, Path]:
    return (
        inputs_root / case_id,
        inputs_root / f"{case_id}.debugging-framework.json",
        inputs_root / f"{case_id}.failure.log",
    )


def require_inputs(project_root: Path, config_path: Path, failure_path: Path) -> None:
    if not input_ready(project_root, config_path, failure_path):
        raise FileNotFoundError(
            "Thiếu input. Chạy action prepare trước: "
            f"project={project_root}, config={config_path}, failure={failure_path}"
        )


def resolve_framework_bin(value: Path) -> Path:
    candidate = value.expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate.resolve()
    discovered = shutil.which(str(value)) or shutil.which("debugging-framework")
    if discovered:
        return Path(discovered).resolve()
    raise FileNotFoundError(f"debugging-framework executable not found: {value}")


def resolve_framework_for_display(value: Path) -> Path:
    try:
        return resolve_framework_bin(value)
    except FileNotFoundError:
        return value.expanduser().resolve()


def build_repair_command(
    args: argparse.Namespace,
    framework_bin: Path,
    project_root: Path,
    config_path: Path,
    failure_path: Path,
) -> list[str]:
    command = [
        str(framework_bin), "repair",
        "--project", str(project_root),
        "--config", str(config_path),
        "--failure-output", str(failure_path),
    ]
    for flag, value in (
        ("--attempts", args.attempts),
        ("--command-timeout", args.command_timeout),
        ("--codex-timeout", args.codex_timeout),
        ("--jobs", args.jobs),
    ):
        if value is not None:
            command.extend([flag, str(value)])
    if args.model:
        command.extend(["--model", args.model])
    return command


def print_contract(project_root: Path, config_path: Path, failure_path: Path) -> None:
    config = read_json(config_path)
    failing = ((config.get("repair") or {}).get("failing_tests") or [])
    print("[inputs ready]")
    print(f"  project:      {project_root}")
    print(f"  config:       {config_path}")
    print(f"  failure log:  {failure_path}")
    print(f"  failing test: {', '.join(failing)}")


def print_commands(
    args: argparse.Namespace,
    framework_bin: Path,
    project_root: Path,
    config_path: Path,
    failure_path: Path,
) -> None:
    doctor = [
        str(framework_bin), "doctor", str(project_root), "--config", str(config_path)
    ]
    if args.jobs is not None:
        doctor.extend(["--jobs", str(args.jobs)])
    repair = build_repair_command(
        args, framework_bin, project_root, config_path, failure_path
    )
    print("\nPublic Debugging-Framework commands:\n")
    print(shlex.join(doctor))
    print("\n" + shlex.join(repair))


def run_command(command: list[str]) -> int:
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        print(f"[ERROR] Không chạy được command: {exc}", file=sys.stderr)
        return 127


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2)
