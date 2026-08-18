#!/usr/bin/env python3
"""Unified Debugging-Framework workflow for one or all libyang defects.

This single entry point owns both the Defects4C-to-fixture conversion and the
product-facing prepare/show/doctor/repair/trial workflow. It also retains batch
benchmark features such as ``--all`` and resume.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTS4C_ROOT = PROJECT_DIR.parents[2]
DEFAULT_INPUTS_ROOT = (
    DEFECTS4C_ROOT
    / "out_tmp_dirs"
    / "debugging_framework"
    / "libyang"
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
DEFAULT_IMAGE = "libyang/defect4c:latest"
PROJECT_NAME = "CESNET___libyang"
TEST_EVIDENCE_PATTERN = r"[0-9]+% tests passed|The following tests FAILED:"
TEST_FAILURE_PATTERN = r"The following tests FAILED:|\*\*\*Failed|FAILED TEST\(S\)"

if str(DEFECTS4C_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFECTS4C_ROOT))

import prepare_project as materializer  # noqa: E402


# The Defects4C fixture-builder implementation is kept in this file so users
# have exactly one libyang Debugging-Framework entry point.
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
    print("case_id\tbug_id\tcommit_after\ttargets\tsource_files")
    for bug in bugs:
        print(
            "\t".join(
                (
                    case_id_for(bug),
                    bug_id_for(bug),
                    required_sha(bug, "commit_after"),
                    ",".join(candidate_targets(bug)),
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
                    f"Input {case_id} dùng image {prepared_image}, nhưng image hiện tại "
                    f"là {image}; dùng --force để xác minh và tạo lại contract"
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
    declared_targets = candidate_targets(bug)
    if not declared_targets:
        raise ValueError(f"{case_id}: no c_compile.test_flags target")

    try:
        source_repo = materializer.ensure_source_repo(
            cache_root=cache_root,
            project_name=PROJECT_NAME,
            remote=str(project_meta.get("main_repo") or ""),
            commits=(commit_after, commit_before),
        )
        materializer.materialize_worktree(
            source_repo=source_repo,
            target=project_root,
            commit_after=commit_after,
            commit_before=commit_before,
            source_files=source_files,
            force=False,
        )
        remove_git_history(project_root)
        write_framework_config(
            config_path,
            failing_tests=declared_targets,
            image=image,
            runtime=runtime,
            jobs=jobs,
        )

        build_commands = validation_build_commands(jobs)
        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=project_root,
            commands=build_commands,
            log_path=work_dir / "prepare.log",
            timeout=command_timeout,
            require_success=True,
        )
        available_tests = discover_ctest_targets(
            runtime=runtime,
            image=image,
            project_root=project_root,
            log_path=work_dir / "test-discovery.log",
            timeout=command_timeout,
        )
        active_targets = [
            target for target in declared_targets if target in available_tests
        ]
        missing_targets = [
            target for target in declared_targets if target not in available_tests
        ]
        unexpected_missing = [
            target for target in missing_targets if not target.endswith("_valgrind")
        ]
        if unexpected_missing:
            raise RuntimeError(
                f"{case_id}: declared CTest targets are missing: "
                + ", ".join(unexpected_missing)
            )
        if missing_targets:
            with (work_dir / "test-discovery.log").open("a", encoding="utf-8") as log:
                log.write(
                    "\nExcluded declared Valgrind targets because the validation "
                    "contract configures ENABLE_VALGRIND_TESTS=OFF: "
                    + ", ".join(missing_targets)
                    + "\n"
                )
        if not active_targets:
            raise RuntimeError(
                f"{case_id}: none of the declared targets exist in CTest: "
                + ", ".join(declared_targets)
            )
        failed_targets, failure_sections = observe_failing_targets(
            runtime=runtime,
            image=image,
            project_root=project_root,
            targets=active_targets,
            log_path=work_dir / "target-tests.log",
            timeout=command_timeout,
        )
        if not failed_targets:
            raise RuntimeError(
                f"{case_id}: none of the available CTest targets failed: "
                + ", ".join(active_targets)
            )
        _buggy_outcomes = observe_ctest_suite(
            runtime=runtime,
            image=image,
            project_root=project_root,
            report_name="buggy-suite.xml",
            log_path=work_dir / "buggy-suite.log",
            timeout=command_timeout,
        )

        fixed_target_outcomes, fixed_suite_outcomes = verify_fixed_oracle(
            source_repo=source_repo,
            staging=staging,
            bug=bug,
            runtime=runtime,
            image=image,
            jobs=jobs,
            targets=failed_targets,
            timeout=command_timeout,
            log_path=work_dir / "fixed-verification.log",
        )
        eligible_targets = select_repair_targets(failed_targets, fixed_target_outcomes)
        excluded_targets = [
            target for target in failed_targets if target not in eligible_targets
        ]
        if excluded_targets:
            print(
                f"[filter] {case_id}: loại target không pass trên fixed: "
                + ", ".join(excluded_targets),
                flush=True,
            )
        if not eligible_targets:
            raise RuntimeError(
                f"{case_id}: no target has the required buggy-fail/fixed-pass outcome"
            )
        failure_output = "\n".join(
            failure_sections[target].rstrip() for target in eligible_targets
        ).rstrip() + "\n"
        failure_path.write_text(failure_output, encoding="utf-8")

        nonpassing_fixed_tests = nonpassing_outcome_ids(fixed_suite_outcomes)
        if nonpassing_fixed_tests:
            print(
                f"[filter] {case_id}: loại khỏi regression vì không pass trên fixed: "
                + ", ".join(
                    f"{target} ({fixed_suite_outcomes[target]})"
                    for target in nonpassing_fixed_tests
                ),
                flush=True,
            )
        write_framework_config(
            config_path,
            failing_tests=eligible_targets,
            image=image,
            runtime=runtime,
            jobs=jobs,
            excluded_regression_tests=nonpassing_fixed_tests,
        )

        framework_work_dir = project_root / ".debugging-framework"
        build_dir = framework_work_dir / "build"
        if build_dir.is_dir():
            shutil.rmtree(build_dir)
        if framework_work_dir.is_dir():
            if any(framework_work_dir.iterdir()):
                raise RuntimeError(
                    f"{case_id}: generated framework files remain in project input"
                )
            framework_work_dir.rmdir()
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
            remove_file_input(
                final_config, inputs_root, f"{case_id}.debugging-framework.json"
            )
            remove_file_input(final_failure, inputs_root, f"{case_id}.failure.log")
        raise


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
    setup = value.get("setup")
    build = value.get("build")
    target = value.get("target_test")
    regression = value.get("regression_test")
    repair = value.get("repair")
    environment = value.get("environment")
    workspace = value.get("workspace")
    failing_tests = repair.get("failing_tests") if isinstance(repair, dict) else None
    if not (
        value.get("system") == "cmake"
        and isinstance(setup, list) and len(setup) == 1
        and isinstance(build, list) and len(build) == 1
        and isinstance(target, list) and len(target) == 1
        and isinstance(regression, list) and len(regression) == 1
        and isinstance(failing_tests, list) and bool(failing_tests)
        and all(isinstance(item, str) and item.strip() for item in failing_tests)
        and len(failing_tests) == len(set(failing_tests))
        and isinstance(environment, dict)
        and environment.get("mode") == "image"
        and isinstance(environment.get("runtime"), str)
        and bool(environment.get("runtime"))
        and isinstance(environment.get("image"), str)
        and bool(environment.get("image"))
        and isinstance(workspace, dict)
        and workspace.get("disposable") is True
        and workspace.get("initialize_git_if_missing") is True
    ):
        return False
    setup_args = command_arguments(setup[0])
    build_args = command_arguments(build[0])
    target_args = command_arguments(target[0])
    regression_args = command_arguments(regression[0])
    if None in (setup_args, build_args, target_args, regression_args):
        return False
    assert setup_args is not None
    assert build_args is not None
    assert target_args is not None
    assert regression_args is not None
    target_pattern = option_value(target_args, "-R")
    exclusion_pattern = option_value(regression_args, "-E")
    return bool(
        setup_args and setup_args[0] == "cmake"
        and "-S" in setup_args and "-B" in setup_args
        and build_args[:2] == ["cmake", "--build"]
        and target_args[:3] == ["ctest", "--test-dir", ".debugging-framework/build"]
        and target_pattern == "^{test_id}$"
        and "--output-junit" in target_args
        and regression_args[:3]
        == ["ctest", "--test-dir", ".debugging-framework/build"]
        and "-R" not in regression_args
        and not any("{test_id}" in argument for argument in regression_args)
        and "--output-junit" in regression_args
        and ("-E" not in regression_args or bool(exclusion_pattern))
    )


def command_arguments(entry: object) -> list[str] | None:
    command = entry.get("command") if isinstance(entry, dict) else entry
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return None
    if isinstance(command, list) and all(isinstance(argument, str) for argument in command):
        return list(command)
    return None


def option_value(arguments: list[str], option: str) -> str | None:
    positions = [index for index, argument in enumerate(arguments) if argument == option]
    if len(positions) != 1 or positions[0] + 1 >= len(arguments):
        return None
    return arguments[positions[0] + 1]


def config_environment_image(config_path: Path) -> str:
    value = read_json(config_path)
    if not isinstance(value, dict):
        return ""
    environment = value.get("environment")
    if not isinstance(environment, dict):
        return ""
    image = environment.get("image")
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
    resolved_root = inputs_root.resolve()
    if path.parent.resolve() != resolved_root or path.name != expected_name:
        raise RuntimeError(f"Từ chối xóa input file không an toàn: {path}")
    if not path.is_file() and not path.is_symlink():
        raise RuntimeError(f"Input file path không phải file: {path}")
    path.unlink()


def validation_build_commands(jobs: int) -> list[list[str]]:
    return [
        [
            "cmake", "-G", "Ninja", "-S", ".", "-B", ".debugging-framework/build",
            "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_C_FLAGS=-g -O0 -Wno-error",
            "-DCMAKE_CXX_FLAGS=-g -O0 -Wno-error", "-DENABLE_TESTS=ON",
            "-DENABLE_BUILD_TESTS=ON", "-DENABLE_VALGRIND_TESTS=OFF",
            "-DENABLE_COVERAGE=OFF", "-DENABLE_TOOLS=OFF",
        ],
        [
            "cmake", "--build", ".debugging-framework/build",
            "--parallel", str(jobs),
        ],
    ]


def write_framework_config(
    path: Path,
    *,
    failing_tests: Iterable[str],
    image: str,
    runtime: str,
    jobs: int,
    excluded_regression_tests: Iterable[str] = (),
) -> None:
    regression_command = [
        "ctest", "--test-dir", ".debugging-framework/build",
    ]
    excluded = sorted(
        {
            str(value).strip()
            for value in excluded_regression_tests
            if str(value).strip()
        }
    )
    if excluded:
        exclusion_pattern = "^(" + "|".join(re.escape(value) for value in excluded) + ")$"
        regression_command.extend(["-E", exclusion_pattern])
    regression_command.extend(
        ["--output-junit", "../regression.xml", "--output-on-failure"]
    )
    config = {
        "schema_version": 6,
        "system": "cmake",
        "setup": [validation_build_commands(jobs)[0]],
        "build": [validation_build_commands(jobs)[1]],
        "target_test": [
            {
                "command": [
                    "ctest", "--test-dir", ".debugging-framework/build",
                    "-R", "^{test_id}$",
                    "--output-junit", "../target-{test_id}.xml",
                    "--output-on-failure",
                ],
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "regression_test": [
            {
                "command": regression_command,
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "repair": {
            "failing_tests": list(failing_tests),
        },
        "workspace": {
            "disposable": True,
            "initialize_git_if_missing": True,
        },
        "environment": {"mode": "image", "runtime": runtime, "image": image},
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def observe_failing_targets(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    targets: list[str],
    log_path: Path,
    timeout: int,
) -> tuple[list[str], dict[str, str]]:
    failed: list[str] = []
    failure_sections: dict[str, str] = {}
    with log_path.open("w", encoding="utf-8") as log:
        for target in targets:
            command = [
                "ctest", "--test-dir", ".debugging-framework/build",
                "-R", f"^{re.escape(target)}$", "-V", "--output-on-failure",
            ]
            result = run_in_image(
                runtime, image, project_root, command, timeout=timeout
            )
            section = command_section(command, result.returncode, result.stdout)
            log.write(section)
            if not test_execution_observed(result.stdout, target):
                raise RuntimeError(f"CTest target was not observed: {target}")
            if result.returncode == 0:
                continue
            if not re.search(TEST_FAILURE_PATTERN, result.stdout, re.MULTILINE):
                raise RuntimeError(
                    f"CTest target {target} exited {result.returncode} without test-failure evidence"
                )
            failed.append(target)
            failure_sections[target] = (
                f"===== failing_test: {target} =====\n{result.stdout.rstrip()}\n"
            )
    return failed, failure_sections


def discover_ctest_targets(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    log_path: Path,
    timeout: int,
) -> set[str]:
    command = [
        "ctest", "--test-dir", ".debugging-framework/build", "--show-only=json-v1"
    ]
    result = run_in_image(runtime, image, project_root, command, timeout=timeout)
    log_path.write_text(
        command_section(command, result.returncode, result.stdout), encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(f"CTest discovery failed; see {log_path}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CTest discovery JSON is invalid; see {log_path}") from exc
    tests = payload.get("tests") if isinstance(payload, dict) else None
    if not isinstance(tests, list):
        raise RuntimeError(f"CTest discovery has no tests array; see {log_path}")
    names = {
        str(item.get("name"))
        for item in tests
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    if not names:
        raise RuntimeError(f"CTest discovered zero tests; see {log_path}")
    return names


def observe_ctest_suite(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    report_name: str,
    log_path: Path,
    timeout: int,
) -> dict[str, str]:
    report_path = project_root / ".debugging-framework" / "build" / report_name
    if report_path.exists():
        report_path.unlink()
    command = [
        "ctest", "--test-dir", ".debugging-framework/build",
        "--output-on-failure", "--output-junit", report_name,
    ]
    result = run_in_image(runtime, image, project_root, command, timeout=timeout)
    log_path.write_text(
        command_section(command, result.returncode, result.stdout), encoding="utf-8"
    )
    if not ctest_suite_observed(result.stdout):
        raise RuntimeError(f"Full CTest suite was not observed; see {log_path}")
    if not report_path.is_file():
        raise RuntimeError(f"CTest JUnit report is missing: {report_path}")
    return read_ctest_outcomes(report_path)


def read_ctest_outcomes(report_path: Path) -> dict[str, str]:
    try:
        root = ET.parse(report_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RuntimeError(f"CTest JUnit report is invalid: {report_path}") from exc
    outcomes: dict[str, str] = {}
    for testcase in root.iter("testcase"):
        name = str(testcase.attrib.get("name") or "").strip()
        if not name:
            continue
        child_tags = {child.tag.rsplit("}", 1)[-1] for child in testcase}
        if child_tags & {"failure", "error"}:
            outcomes[name] = "failed"
        elif "skipped" in child_tags:
            outcomes[name] = "skipped"
        else:
            outcomes[name] = "passed"
    if not outcomes:
        raise RuntimeError(f"CTest JUnit report has no test cases: {report_path}")
    return outcomes


def test_execution_observed(output: str, target: str) -> bool:
    return target in output and ctest_suite_observed(output)


def ctest_suite_observed(output: str) -> bool:
    if "No tests were found" in output or re.search(
        r"\b(?:Total Tests:\s*0|0 tests? (?:were )?run|out of 0)\b", output
    ):
        return False
    return bool(re.search(TEST_EVIDENCE_PATTERN, output, re.MULTILINE))


def verify_fixed_oracle(
    *,
    source_repo: Path,
    staging: Path,
    bug: dict,
    runtime: str,
    image: str,
    jobs: int,
    targets: list[str],
    timeout: int,
    log_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    fixed_root = staging / ".fixed-verification-project"
    commit_after = required_sha(bug, "commit_after")
    materializer.materialize_worktree(
        source_repo=source_repo,
        target=fixed_root,
        commit_after=commit_after,
        commit_before=commit_after,
        source_files=materializer.source_files(bug),
        force=False,
    )
    remove_git_history(fixed_root)
    target_outcomes: dict[str, str] = {}
    try:
        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            commands=validation_build_commands(jobs),
            log_path=log_path,
            timeout=timeout,
            require_success=True,
        )
        with log_path.open("a", encoding="utf-8") as log:
            for index, target in enumerate(targets):
                report_name = f"fixed-target-{index}.xml"
                report_path = fixed_root / ".debugging-framework" / report_name
                command = [
                    "ctest", "--test-dir", ".debugging-framework/build",
                    "-R", f"^{re.escape(target)}$", "-V",
                    "--output-junit", f"../{report_name}", "--output-on-failure",
                ]
                result = run_in_image(runtime, image, fixed_root, command, timeout=timeout)
                log.write(command_section(command, result.returncode, result.stdout))
                if not test_execution_observed(result.stdout, target):
                    raise RuntimeError(f"Fixed CTest target was not observed: {target}")
                if not report_path.is_file():
                    raise RuntimeError(
                        f"Fixed CTest target JUnit report is missing: {report_path}"
                    )
                reported_outcome = read_ctest_outcomes(report_path).get(target)
                if reported_outcome is None:
                    raise RuntimeError(
                        f"Fixed CTest target outcome is missing from JUnit: {target}"
                    )
                target_outcomes[target] = (
                    "passed"
                    if result.returncode == 0 and reported_outcome == "passed"
                    else reported_outcome
                    if reported_outcome != "passed"
                    else "failed"
                )
        suite_outcomes = observe_ctest_suite(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            report_name="fixed-suite.xml",
            log_path=log_path.with_name("fixed-suite.log"),
            timeout=timeout,
        )
        return target_outcomes, suite_outcomes
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
    require_success: bool,
) -> None:
    with log_path.open("w", encoding="utf-8") as log:
        for command in commands:
            result = run_in_image(runtime, image, project_root, command, timeout=timeout)
            log.write(command_section(command, result.returncode, result.stdout))
            if require_success and result.returncode != 0:
                raise RuntimeError(
                    f"Command failed ({result.returncode}); see {log_path}: "
                    + " ".join(command)
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
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(command)}\n{output}") from exc


def command_section(command: list[str], returncode: int, output: str) -> str:
    return (
        f"===== command: {' '.join(command)} =====\n"
        f"returncode: {returncode}\n{output.rstrip()}\n\n"
    )


def candidate_targets(bug: dict) -> list[str]:
    raw = (bug.get("c_compile") or {}).get("test_flags") or []
    raw = [raw] if isinstance(raw, str) else raw
    targets: list[str] = []
    for value in raw:
        for target in str(value).split("|"):
            target = target.strip()
            if target and target not in targets:
                targets.append(target)
    return targets


def select_repair_targets(
    buggy_failed_targets: Iterable[str], fixed_outcomes: dict[str, str]
) -> list[str]:
    """Keep only targets with the benchmark oracle FAIL(buggy) -> PASS(fixed)."""
    return [
        target
        for target in buggy_failed_targets
        if fixed_outcomes.get(target) == "passed"
    ]


def nonpassing_outcome_ids(outcomes: dict[str, str]) -> list[str]:
    """Return tests that cannot be required by a fixed-compatible regression suite."""
    return sorted(
        target for target, outcome in outcomes.items() if outcome != "passed"
    )


def case_id_for(bug: dict) -> str:
    return materializer.safe_name(
        f"{bug_id_for(bug)}__{required_sha(bug, 'commit_after')[:12]}"
    )


def bug_id_for(bug: dict) -> str:
    return str((bug.get("type") or {}).get("id") or required_sha(bug, "commit_after")[:12])


def required_sha(bug: dict, key: str) -> str:
    return materializer.required_sha(bug, key)


def remove_git_history(project_root: Path) -> None:
    git_path = project_root / ".git"
    if git_path.is_dir():
        shutil.rmtree(git_path)
    elif git_path.exists():
        git_path.unlink()


def resolve_runtime(value: str) -> str:
    executable = shutil.which(value) if value else None
    if not executable:
        raise FileNotFoundError(f"OCI runtime not found: {value}")
    completed = subprocess.run(
        [executable, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=10, check=False,
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
        description="Create libyang inputs, then call the public Debugging-Framework CLI."
    )
    parser.add_argument(
        "action", nargs="?", default="trial",
        choices=("prepare", "show", "doctor", "repair", "trial"),
        help=(
            "create inputs; show the public CLI contract; run doctor; run repair; "
            "or run prepare+doctor+repair (default: trial)"
        ),
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--bug", "--sha", dest="selectors", action="append",
        help="Unique bug type.id or commit_after prefix; repeatable.",
    )
    selection.add_argument("--all", action="store_true", help="Process all 15 bugs.")
    selection.add_argument("--list", action="store_true", help="List bugs and exit.")
    parser.add_argument("--inputs-root", type=Path, default=DEFAULT_INPUTS_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--runtime", default="docker")
    parser.add_argument(
        "--jobs", type=int,
        help="Optional Framework override; prepare uses 4 when omitted.",
    )
    parser.add_argument("--attempts", type=int, help="Optional Framework override.")
    parser.add_argument(
        "--command-timeout", type=int,
        help="Optional Framework override; prepare uses 1800s when omitted.",
    )
    parser.add_argument("--codex-timeout", type=int, help="Optional Framework override.")
    parser.add_argument("--model", default="", help="Optional Codex model override.")
    parser.add_argument(
        "--framework-bin", type=Path, default=DEFAULT_FRAMEWORK_BIN,
        help="Installed debugging-framework executable.",
    )
    parser.add_argument("--force", action="store_true", help="Recreate existing inputs.")
    parser.add_argument(
        "--verify-fixed", action="store_true",
        help=(
            "Compatibility flag; prepare now always compares buggy/fixed outcomes "
            "and filters tests that fail on fixed."
        ),
    )
    parser.add_argument(
        "--continue-on-error", action="store_true",
        help="Continue with later cases after prepare or repair failure.",
    )
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
    prepare_command_timeout = (
        args.command_timeout if args.command_timeout is not None else 1800
    )

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
                    command_timeout=prepare_command_timeout,
                    force=args.force,
                )
            require_inputs(project_root, config_path, failure_path)
            print_contract(project_root, config_path, failure_path)

            if args.action == "prepare":
                continue
            if args.action == "show":
                display_bin = resolve_framework_for_display(args.framework_bin)
                print_commands(
                    args, display_bin, project_root, config_path, failure_path
                )
                continue

            assert framework_bin is not None
            if args.action in {"doctor", "trial"}:
                doctor_command = [
                    str(framework_bin), "doctor", str(project_root),
                    "--config", str(config_path),
                ]
                if args.jobs is not None:
                    doctor_command.extend(["--jobs", str(args.jobs)])
                print("[Debugging-Framework] doctor", flush=True)
                doctor_returncode = run_command(doctor_command)
                if doctor_returncode != 0:
                    overall_returncode = doctor_returncode
                    print(
                        f"[FAIL] {case_id}: doctor returned {doctor_returncode}",
                        file=sys.stderr,
                    )
                    if not args.continue_on_error:
                        break
                    continue
                if args.action == "doctor":
                    continue

            command = build_repair_command(
                args, framework_bin, project_root, config_path, failure_path
            )
            print("[Debugging-Framework] repair", flush=True)
            returncode = run_command(command)
            if returncode != 0:
                overall_returncode = returncode
                print(f"[FAIL] {case_id}: repair returned {returncode}", file=sys.stderr)
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
    if args.verify_fixed and args.action not in {"prepare", "trial"}:
        raise ValueError("--verify-fixed chỉ dùng với prepare hoặc trial")


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
        str(framework_bin),
        "repair",
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


def print_contract(
    project_root: Path,
    config_path: Path,
    failure_path: Path,
) -> None:
    config = read_json(config_path)
    failing_tests = ((config.get("repair") or {}).get("failing_tests") or [])
    print("[inputs ready]")
    print(f"  project:      {project_root}")
    print(f"  config:       {config_path}")
    print(f"  failure log:  {failure_path}")
    print(f"  failing test: {', '.join(failing_tests)}")


def print_commands(
    args: argparse.Namespace,
    framework_bin: Path,
    project_root: Path,
    config_path: Path,
    failure_path: Path,
) -> None:
    doctor = [
        str(framework_bin), "doctor", str(project_root),
        "--config", str(config_path),
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
