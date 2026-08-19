#!/usr/bin/env python3
"""Prepare mongo-c-driver Defects4C cases for the public Debugging-Framework CLI."""

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
    / "mongo-c-driver"
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
DEFAULT_IMAGE = "mongo-c-driver/defect4c:latest"
PROJECT_NAME = "mongodb___mongo-c-driver"
MONGO_C_DRIVER_BUILD_COMMAND = "defects4c-mongo-c-driver-build"
MONGO_C_DRIVER_TEST_COMMAND = "defects4c-mongo-c-driver-test"
REGRESSION_TEST_LIMIT = 70
TARGET_BY_ASSET = {"test59.bson": "/bson/validate"}
TEST_EVIDENCE_PATTERN = r"^(?:PASSED|FAILED|SKIPPED)\s+\S+"
TEST_FAILURE_PATTERN = r"^FAILED\s+\S+"

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
    print("case_id\tbug_id\tstatus\tcommit_after\ttest_assets\tsource_files")
    for bug in bugs:
        print(
            "\t".join(
                (
                    case_id_for(bug),
                    bug_id_for(bug),
                    str((bug.get("unittest") or {}).get("status") or ""),
                    required_sha(bug, "commit_after"),
                    ",".join(test_assets_for(bug)),
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
    force: bool,
) -> tuple[Path, Path, Path]:
    case_id = case_id_for(bug)
    final_project, final_config, final_failure = input_paths(inputs_root, case_id)
    if input_ready(final_project, final_config, final_failure):
        if not force:
            prepared_image = config_environment_image(final_config)
            if prepared_image != image:
                raise RuntimeError(
                    f"Input {case_id} uses image {prepared_image}, current image is "
                    f"{image}; use --force to verify and recreate the contract"
                )
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
        all_tests = discover_mongo_c_driver_tests(project_root)
        declared_tests = target_tests_for_bug(project_root, bug)
        validate_test_ids(declared_tests, all_tests)
        if not declared_tests:
            raise ValueError(f"{case_id}: no mongo_c_driver target test maps to files.test")
        regression_tests = select_regression_tests(
            all_tests,
            excluded_tests=declared_tests,
            seed=case_id,
        )
        if not regression_tests:
            raise RuntimeError(f"{case_id}: no supplemental mongo_c_driver regression tests found")

        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=project_root,
            commands=validation_build_commands(jobs),
            log_path=work_dir / "buggy-build.log",
            timeout=command_timeout,
        )
        failed_tests, failure_sections = observe_target_tests(
            runtime=runtime,
            image=image,
            project_root=project_root,
            tests=declared_tests,
            log_path=work_dir / "buggy-target-tests.log",
            timeout=command_timeout,
        )
        if not failed_tests:
            raise RuntimeError(
                f"{case_id}: none of the declared mongo_c_driver tests failed: "
                + ", ".join(declared_tests)
            )
        buggy_regression_outcomes = observe_regression_tests(
            runtime=runtime,
            image=image,
            project_root=project_root,
            tests=regression_tests,
            log_path=work_dir / "buggy-regression-tests.log",
            timeout=command_timeout,
        )

        shutil.rmtree(project_root)
        fixed_results, fixed_regression_outcomes = verify_fixed_oracle(
            source_repo=source_repo,
            staging=staging,
            bug=bug,
            runtime=runtime,
            image=image,
            jobs=jobs,
            target_tests=failed_tests,
            regression_tests=regression_tests,
            timeout=command_timeout,
            log_path=work_dir / "fixed-verification.log",
        )
        eligible_tests = [
            test for test in failed_tests if fixed_results.get(test) == "passed"
        ]
        excluded_targets = [
            test for test in failed_tests if test not in eligible_tests
        ]
        if excluded_targets:
            print(
                f"[filter] {case_id}: loại mongo_c_driver target không pass trên fixed: "
                + ", ".join(excluded_targets),
                flush=True,
            )
        if not eligible_tests:
            raise RuntimeError(
                f"{case_id}: no mongo_c_driver test has the required buggy-fail/fixed-pass outcome"
            )

        excluded_regression_tests = sorted(
            test
            for test in regression_tests
            if buggy_regression_outcomes.get(test) != "passed"
            or fixed_regression_outcomes.get(test) != "passed"
        )
        if excluded_regression_tests:
            print(
                f"[filter] {case_id}: loại mongo_c_driver regression không pass trên cả "
                "buggy và fixed: " + ", ".join(excluded_regression_tests),
                flush=True,
            )
        if len(excluded_regression_tests) == len(regression_tests):
            raise RuntimeError(
                f"{case_id}: no supplemental mongo_c_driver regression test passes on both "
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
            regression_tests=regression_tests,
            excluded_regression_tests=excluded_regression_tests,
            image=image,
            runtime=runtime,
            jobs=jobs,
        )

        materialize_snapshot(
            source_repo=source_repo,
            target=project_root,
            commit_after=commit_after,
            commit_before=commit_before,
            source_files=source_files,
        )
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


def materialize_snapshot(
    *,
    source_repo: Path,
    target: Path,
    commit_after: str,
    commit_before: str,
    source_files: list[str],
) -> None:
    """Export one fixed tree and overlay only the declared buggy source files."""
    if target.exists():
        raise FileExistsError(f"Target already exists: {target}")
    if not source_files:
        raise ValueError("Bug entry has no files.src; cannot construct MONGO_C_DRIVER snapshot")
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
                validate_archive_member(member)
                if sys.version_info >= (3, 12):
                    archive.extract(member, path=target, filter="fully_trusted")
                else:
                    archive.extract(member, path=target)
    except BaseException:
        process.kill()
        process.wait()
        shutil.rmtree(target, ignore_errors=True)
        raise
    error = process.stderr.read().decode("utf-8", errors="replace").strip()
    if process.wait() != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"Cannot export MONGO_C_DRIVER commit {commit_after}: {error}")

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
            raise RuntimeError(
                f"Cannot read buggy source {commit_before}:{relpath}: {detail}"
            )
        destination = target / relpath
        if not destination.is_file():
            raise FileNotFoundError(f"Buggy overlay target does not exist: {destination}")
        mode = destination.stat().st_mode
        destination.write_bytes(completed.stdout)
        destination.chmod(mode)


def validate_archive_member(member: tarfile.TarInfo) -> None:
    path = Path(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe path in MONGO_C_DRIVER Git archive: {member.name}")
    if member.issym() or member.islnk():
        link = Path(member.linkname)
        if link.is_absolute() or ".." in link.parts:
            raise RuntimeError(
                f"Unsafe link in MONGO_C_DRIVER Git archive: {member.name} -> {member.linkname}"
            )


def ensure_source_cache(
    *, cache_root: Path, remote: str, commits: tuple[str, str]
) -> Path:
    """Return a reusable repo without cloning full history for every case."""
    project_cache = cache_root / PROJECT_NAME
    shared = project_cache / "source-cache"
    candidates = [shared]
    candidates.extend(
        path for path in sorted(project_cache.glob("git_repo_dir*")) if path != shared
    )
    for candidate in candidates:
        if (candidate / ".git").is_dir() and all(
            git_has_commit(candidate, sha) for sha in commits
        ):
            return candidate.resolve()

    if not remote:
        raise ValueError("mongo-c-driver project.json has no main_repo")
    if shared.exists() and not (shared / ".git").is_dir():
        raise RuntimeError(f"MONGO_C_DRIVER cache path is not a Git repository: {shared}")
    if not (shared / ".git").is_dir():
        shared.parent.mkdir(parents=True, exist_ok=True)
        run_checked(
            [
                "git", "clone", "--filter=blob:none", "--depth=1",
                "--no-checkout", remote, str(shared),
            ],
            timeout=60 * 30,
        )
    for sha in commits:
        if git_has_commit(shared, sha):
            continue
        run_checked(
            [
                "git", "-C", str(shared), "fetch", "--filter=blob:none",
                "--depth=1", "origin", sha,
            ],
            timeout=60 * 20,
        )
        if not git_has_commit(shared, sha):
            raise RuntimeError(f"Fetched MONGO_C_DRIVER cache does not contain commit: {sha}")
    return shared.resolve()


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
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {shlex.join(command)}"
        )


def validation_build_commands(jobs: int) -> list[list[str]]:
    configure = [MONGO_C_DRIVER_BUILD_COMMAND, "configure", "--jobs", str(jobs)]
    return [configure, [MONGO_C_DRIVER_BUILD_COMMAND, "build", "--jobs", str(jobs)]]


def mongo_c_driver_test_command(
    *, tests: Iterable[str], excluded_tests: Iterable[str] = ()
) -> list[str]:
    selected = list(tests)
    if not selected:
        raise ValueError("MONGO_C_DRIVER test command requires at least one selected test")
    command = [MONGO_C_DRIVER_TEST_COMMAND]
    for excluded in excluded_tests:
        command.extend(["--exclude-test", excluded])
    for test in selected:
        command.extend(["--test", test])
    return command


def write_framework_config(
    path: Path,
    *,
    failing_tests: Iterable[str],
    regression_tests: Iterable[str],
    excluded_regression_tests: Iterable[str],
    image: str,
    runtime: str,
    jobs: int,
) -> None:
    failing = list(dict.fromkeys(failing_tests))
    selected = list(dict.fromkeys(regression_tests))
    excluded = sorted(set(excluded_regression_tests))
    if not failing:
        raise ValueError("mongo_c_driver config requires at least one failing test")
    if not 1 <= len(selected) <= REGRESSION_TEST_LIMIT:
        raise ValueError(
            f"mongo_c_driver regression set must contain between 1 and {REGRESSION_TEST_LIMIT} tests"
        )
    if not set(excluded).issubset(selected):
        raise ValueError("Excluded mongo_c_driver regression tests must be in the selected set")
    if len(excluded) == len(selected):
        raise ValueError("At least one mongo_c_driver regression test must remain after exclusions")
    build_commands = validation_build_commands(jobs)
    config = {
        "schema_version": 6,
        "system": "cmake",
        "setup": [build_commands[0]],
        "build": [build_commands[1]],
        "target_test": [
            {
                "command": mongo_c_driver_test_command(tests=["{test_id}"]),
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "regression_test": [
            {
                "command": mongo_c_driver_test_command(
                    tests=selected, excluded_tests=excluded
                ),
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "repair": {
            "failing_tests": failing,
            "source_extensions": [".c", ".h"],
        },
        "workspace": {
            "disposable": True,
            "initialize_git_if_missing": True,
        },
        "environment": {"mode": "image", "runtime": runtime, "image": image},
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


TEST_REGISTRATION_RE = re.compile(
    r'\bTestSuite_Add[A-Za-z0-9_]*\s*\(\s*'
    r'[A-Za-z_][A-Za-z0-9_]*\s*,\s*"(/[^"]+)"',
    re.MULTILINE,
)


def discover_mongo_c_driver_tests(project_root: Path) -> list[str]:
    tests_dir = project_root / "src" / "libbson" / "tests"
    if not tests_dir.is_dir():
        raise FileNotFoundError(
            f"mongo-c-driver libbson tests are missing: {tests_dir}"
        )
    tests: set[str] = set()
    for path in sorted(tests_dir.rglob("*.c")):
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"//[^\n]*", "", text)
        tests.update(TEST_REGISTRATION_RE.findall(text))
    discovered = sorted(tests)
    if not discovered:
        raise RuntimeError(
            f"mongo-c-driver contains zero registered libbson tests: {tests_dir}"
        )
    return discovered


def test_assets_for(bug: dict) -> list[str]:
    values = (bug.get("files") or {}).get("test") or []
    values = [values] if isinstance(values, str) else values
    normalized = (
        str(value).strip().replace("\\", "/")
        for value in values
        if str(value).strip()
    )
    return list(dict.fromkeys(normalized))


def target_tests_for_bug(project_root: Path, bug: dict) -> list[str]:
    available = set(discover_mongo_c_driver_tests(project_root))
    targets = {
        TARGET_BY_ASSET[Path(value).name]
        for value in test_assets_for(bug)
        if Path(value).name in TARGET_BY_ASSET
    }
    return sorted(targets.intersection(available))


def select_regression_tests(
    discovered_tests: Iterable[str],
    *,
    excluded_tests: Iterable[str],
    seed: str,
    limit: int = REGRESSION_TEST_LIMIT,
) -> list[str]:
    if limit < 1:
        raise ValueError("MONGO_C_DRIVER regression test limit must be >= 1")
    excluded = set(excluded_tests)
    candidates = sorted(
        {
            test.strip()
            for test in discovered_tests
            if test.strip() and test.strip() not in excluded
        },
        key=lambda test: (hashlib.sha256(f"{seed}\0{test}".encode()).digest(), test),
    )
    return candidates[:limit]


def observe_target_tests(
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
            command = mongo_c_driver_test_command(tests=[test])
            result = run_in_image(runtime, image, project_root, command, timeout=timeout)
            log.write(command_section(command, result.returncode, result.stdout))
            if result.returncode not in {0, 1}:
                raise RuntimeError(
                    f"mongo_c_driver target {test} exited {result.returncode}; see {log_path}"
                )
            outcome = mongo_c_driver_adapter_outcomes(result.stdout).get(test)
            if outcome is None:
                raise RuntimeError(f"mongo_c_driver target outcome was not observed: {test}")
            if outcome != "failed":
                continue
            failed.append(test)
            sections[test] = (
                f"===== failing_test: {test} =====\n{result.stdout.rstrip()}\n"
            )
    return failed, sections


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
    command = mongo_c_driver_test_command(tests=tests)
    result = run_in_image(runtime, image, project_root, command, timeout=timeout)
    with log_path.open("a" if append else "w", encoding="utf-8") as log:
        log.write(command_section(command, result.returncode, result.stdout))
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            f"mongo_c_driver regression selection exited {result.returncode}; see {log_path}"
        )
    outcomes = mongo_c_driver_adapter_outcomes(result.stdout)
    missing = [test for test in tests if test not in outcomes]
    if missing:
        raise RuntimeError("mongo_c_driver regression outcomes are missing for: " + ", ".join(missing))
    return {test: outcomes[test] for test in tests}


def mongo_c_driver_adapter_outcomes(output: str) -> dict[str, str]:
    labels = {"PASSED": "passed", "FAILED": "failed", "SKIPPED": "skipped"}
    return {
        match.group(2): labels[match.group(1)]
        for match in re.finditer(
            r"^(PASSED|FAILED|SKIPPED)\s+(\S+)\s*$", output, re.MULTILINE
        )
    }


def verify_fixed_oracle(
    *,
    source_repo: Path,
    staging: Path,
    bug: dict,
    runtime: str,
    image: str,
    jobs: int,
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
    try:
        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            commands=validation_build_commands(jobs),
            log_path=log_path,
            timeout=timeout,
        )
        fixed_results: dict[str, str] = {}
        with log_path.open("a", encoding="utf-8") as log:
            for test in target_tests:
                command = mongo_c_driver_test_command(tests=[test])
                result = run_in_image(runtime, image, fixed_root, command, timeout=timeout)
                log.write(command_section(command, result.returncode, result.stdout))
                if result.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"Fixed mongo_c_driver target {test} exited {result.returncode}"
                    )
                outcome = mongo_c_driver_adapter_outcomes(result.stdout).get(test)
                if outcome is None:
                    raise RuntimeError(f"Fixed mongo_c_driver target was not observed: {test}")
                fixed_results[test] = outcome
        regression_outcomes = observe_regression_tests(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            tests=regression_tests,
            log_path=log_path,
            timeout=timeout,
            append=True,
        )
        return fixed_results, regression_outcomes
    finally:
        shutil.rmtree(fixed_root, ignore_errors=True)


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


def validate_test_ids(tests: Iterable[str], available: Iterable[str]) -> None:
    available_set = set(available)
    for test in tests:
        if not re.fullmatch(r"/[A-Za-z0-9_./:+-]+", test):
            raise ValueError(f"Unsafe mongo-c-driver test id: {test!r}")
        if test not in available_set:
            raise FileNotFoundError(f"Declared mongo_c_driver test is missing: {test}")


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
    target = value.get("target_test")
    regression = value.get("regression_test")
    repair = value.get("repair")
    environment = value.get("environment")
    workspace = value.get("workspace")
    if not (
        value.get("system") == "cmake"
        and isinstance(value.get("setup"), list) and len(value["setup"]) == 1
        and isinstance(value.get("build"), list) and len(value["build"]) == 1
        and isinstance(target, list) and len(target) == 1
        and isinstance(regression, list) and len(regression) == 1
        and isinstance(repair, dict)
        and repair.get("failing_tests")
        and isinstance(environment, dict)
        and environment.get("mode") == "image"
        and environment.get("runtime")
        and environment.get("image")
        and isinstance(workspace, dict)
        and workspace.get("disposable") is True
        and workspace.get("initialize_git_if_missing") is True
        and {".c", ".h"}.issubset(set(repair.get("source_extensions", [])))
    ):
        return False
    target_args = command_arguments(target[0])
    regression_args = command_arguments(regression[0])
    if target_args is None or regression_args is None:
        return False
    selected = option_values(regression_args, "--test")
    excluded = option_values(regression_args, "--exclude-test")
    return bool(
        target_args == [MONGO_C_DRIVER_TEST_COMMAND, "--test", "{test_id}"]
        and regression_args
        and regression_args[0] == MONGO_C_DRIVER_TEST_COMMAND
        and 1 <= len(selected) <= REGRESSION_TEST_LIMIT
        and len(selected) == len(set(selected))
        and set(excluded).issubset(selected)
        and len(set(excluded)) < len(selected)
        and not any("{test_id}" in argument for argument in regression_args)
    )


def command_arguments(entry: object) -> list[str] | None:
    command = entry.get("command") if isinstance(entry, dict) else entry
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return None
    if isinstance(command, list) and all(isinstance(value, str) for value in command):
        return list(command)
    return None


def option_values(arguments: list[str], option: str) -> list[str]:
    return [
        arguments[index + 1]
        for index, value in enumerate(arguments[:-1])
        if value == option
    ]


def config_environment_image(config_path: Path) -> str:
    value = read_json(config_path)
    environment = value.get("environment") if isinstance(value, dict) else None
    image = environment.get("image") if isinstance(environment, dict) else None
    return image if isinstance(image, str) else ""


def remove_project_input(project_root: Path, inputs_root: Path, case_id: str) -> None:
    if not project_root.exists():
        return
    resolved_root = inputs_root.resolve()
    resolved_project = project_root.resolve()
    if resolved_project.parent != resolved_root or resolved_project.name != case_id:
        raise RuntimeError(f"Từ chối xóa input path không an toàn: {resolved_project}")
    shutil.rmtree(resolved_project)


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
        description="Create mongo-c-driver inputs, then call the public Debugging-Framework CLI."
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
    selection.add_argument(
        "--all", action="store_true", help="Process the mongo-c-driver bug."
    )
    selection.add_argument("--list", action="store_true", help="List bugs and exit.")
    parser.add_argument("--inputs-root", type=Path, default=DEFAULT_INPUTS_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--jobs", type=int, help="Prepare uses 4 when omitted.")
    parser.add_argument("--attempts", type=int)
    parser.add_argument(
        "--command-timeout", type=int,
        help="Prepare uses 7200 seconds per mongo-c-driver command when omitted.",
    )
    parser.add_argument("--codex-timeout", type=int)
    parser.add_argument("--model", default="")
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
                    force=args.force,
                )
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
    regression_command = ((config.get("regression_test") or [{}])[0].get("command") or [])
    print("[inputs ready]")
    print(f"  project:       {project_root}")
    print(f"  config:        {config_path}")
    print(f"  failure log:   {failure_path}")
    print(f"  failing tests: {', '.join(failing)}")
    print(f"  regression:    {regression_command.count('--test')} selected")


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
