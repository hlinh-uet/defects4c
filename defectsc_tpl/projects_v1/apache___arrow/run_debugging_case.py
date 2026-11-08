#!/usr/bin/env python3
"""Prepare Apache Arrow defects for the public Debugging-Framework CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTS4C_ROOT = PROJECT_DIR.parents[2]
DEFAULT_INPUTS_ROOT = (
    DEFECTS4C_ROOT
    / "out_tmp_dirs"
    / "debugging_framework"
    / "arrow"
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
DEFAULT_IMAGE = "arrow/defect4c:latest"
PROJECT_NAME = "apache___arrow"
BUILD_DIR = ".debugging-framework/build"
TEST_ADAPTER_COMMAND = "defects4c-arrow-test"
GTEST_CMAKE_DIR = "/opt/arrow-googletest/lib/cmake/GTest"
TEST_ARTIFACT_DIRECTORY = "defects4c-test-artifacts"
REGRESSION_TEST_LIMIT = 70
ARROW_SUBMODULES = (
    ("testing", "https://github.com/apache/arrow-testing.git"),
    (
        "cpp/submodules/parquet-testing",
        "https://github.com/apache/parquet-testing.git",
    ),
)
REQUIRED_DEPENDENCIES = tuple(path for path, _ in ARROW_SUBMODULES)
TEST_EVIDENCE_PATTERN = r"^(?:PASSED|FAILED|SKIPPED)\s+\S+"
TEST_FAILURE_PATTERN = r"^FAILED\s+\S+"

if str(DEFECTS4C_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFECTS4C_ROOT))

import prepare_project as materializer  # noqa: E402


class PrepareReport:
    """Keep diagnostics outside the disposable source/staging directories."""

    def __init__(self, inputs_root: Path, case_id: str, **metadata: object) -> None:
        root = inputs_root.parent / "prepare-logs" / case_id
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
        self.directory = Path(tempfile.mkdtemp(prefix=stamp, dir=root))
        self.manifest = {
            "schema_version": 1,
            "case_id": case_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "stage": "metadata",
            "outcomes": {},
            **metadata,
        }
        self.update()

    def update(self, **values: object) -> None:
        self.manifest.update(values)
        temporary = self.directory / "manifest.json.tmp"
        temporary.write_text(
            json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.directory / "manifest.json")

    def record_outcomes(self, phase: str, outcomes: dict[str, str]) -> None:
        self.manifest["outcomes"].setdefault(phase, {}).update(outcomes)
        self.update()


OutcomeRecorder = Callable[[str, dict[str, str]], None]


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
    print("case_id\tbug_id\tstatus_manual\tcommit_after\ttargets\tsource_files")
    for bug in bugs:
        print(
            "\t".join(
                (
                    case_id_for(bug),
                    bug_id_for(bug),
                    str(bug.get("status_manual") or ""),
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
    report = PrepareReport(
        inputs_root, case_id,
        commit_after=bug.get("commit_after"),
        commit_before=bug.get("commit_before"),
        environment={"runtime": runtime, "image": image},
        jobs=jobs,
        command_timeout=command_timeout,
        inputs={"project": str(final_project), "config": str(final_config),
                "failure_log": str(final_failure)},
    )
    work_dir = report.directory
    print(f"[prepare logs] {work_dir}", flush=True)

    try:
        commit_after = required_sha(bug, "commit_after")
        commit_before = required_sha(bug, "commit_before")
        source_files = materializer.source_files(bug)
        declared_targets = candidate_targets(bug)
        if not declared_targets:
            raise ValueError(f"{case_id}: no Arrow CTest target declared")
        report.update(
            stage="source-cache", source_files=source_files,
            declared_targets=declared_targets,
        )
        source_repo = ensure_source_cache(
            cache_root=cache_root,
            remote=str(project_meta.get("main_repo") or ""),
            commits=(commit_after, commit_before),
            log_path=work_dir / "source-cache.log",
        )
        report.update(stage="buggy-materialization")
        materialize_snapshot(
            source_repo=source_repo,
            target=project_root,
            commit_after=commit_after,
            commit_before=commit_before,
            source_files=source_files,
        )
        report.update(stage="dependencies")
        dependencies = provision_dependencies(
            source_repo=source_repo,
            cache_root=cache_root,
            commit=commit_after,
            project_root=project_root,
            log_path=work_dir / "dependency-sync.log",
        )
        report.update(dependencies=dependencies, stage="buggy-build")
        remove_git_metadata(project_root)
        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=project_root,
            commands=validation_build_commands(jobs, declared_targets),
            log_path=work_dir / "buggy-build.log",
            timeout=command_timeout,
        )
        report.update(stage="buggy-target-tests")
        failed_tests, failure_sections = observe_buggy_targets(
            runtime=runtime,
            image=image,
            project_root=project_root,
            targets=declared_targets,
            log_path=work_dir / "buggy-target-tests.log",
            timeout=command_timeout,
            record_outcomes=report.record_outcomes,
        )
        report.update(
            reproduced_failing_tests=failed_tests,
            excluded_unreproduced_tests={
                test: {"reason": "not_failed_in_isolation", "buggy": outcome}
                for test, outcome in report.manifest["outcomes"].get("buggy-isolated", {}).items()
                if outcome != "failed"
            },
        )
        if not failed_tests:
            raise RuntimeError(
                f"{case_id}: no declared Arrow test reproduced failure in isolation: "
                + ", ".join(declared_targets)
            )
        report.update(stage="test-discovery")
        discovered_tests = discover_arrow_tests(
            runtime=runtime,
            image=image,
            project_root=project_root,
            targets=declared_targets,
            log_path=work_dir / "test-discovery.log",
            timeout=command_timeout,
        )
        regression_tests = select_regression_tests(
            discovered_tests,
            excluded_tests=failed_tests,
            seed=case_id,
        )
        report.update(
            discovered_tests=discovered_tests, selected_regression_tests=regression_tests,
        )
        if not regression_tests:
            raise RuntimeError(f"{case_id}: no supplemental Arrow regression tests found")
        report.update(stage="buggy-regression-tests")
        buggy_regression_outcomes = observe_tests(
            runtime=runtime,
            image=image,
            project_root=project_root,
            tests=regression_tests,
            log_path=work_dir / "buggy-regression-tests.log",
            timeout=command_timeout,
            record_outcomes=report.record_outcomes,
        )

        clear_build_artifacts(project_root)
        report.update(stage="fixed-verification")
        fixed_results, fixed_regression_outcomes = verify_fixed_oracle(
            source_repo=source_repo,
            dependency_source=project_root,
            staging=staging,
            bug=bug,
            runtime=runtime,
            image=image,
            jobs=jobs,
            targets=declared_targets,
            target_tests=failed_tests,
            regression_tests=regression_tests,
            timeout=command_timeout,
            log_path=work_dir / "fixed-verification.log",
            record_outcomes=report.record_outcomes,
        )
        eligible_tests = [
            test for test in failed_tests if fixed_results.get(test) == "passed"
        ]
        excluded_targets = [test for test in failed_tests if test not in eligible_tests]
        report.update(
            failing_tests=eligible_tests,
            excluded_failing_tests={test: "not_passed_on_fixed" for test in excluded_targets},
        )
        if excluded_targets:
            print(
                f"[filter] {case_id}: loại target không pass trên fixed: "
                + ", ".join(excluded_targets),
                flush=True,
            )
        if not eligible_tests:
            raise RuntimeError(
                f"{case_id}: no Arrow test has the required buggy-fail/fixed-pass outcome"
            )

        excluded_regression_tests = sorted(
            test
            for test in regression_tests
            if buggy_regression_outcomes.get(test) != "passed"
            or fixed_regression_outcomes.get(test) != "passed"
        )
        report.update(excluded_regression_tests={
            test: {
                "reason": "not_passed_on_both_baselines",
                "buggy": buggy_regression_outcomes.get(test),
                "fixed": fixed_regression_outcomes.get(test),
            }
            for test in excluded_regression_tests
        })
        if excluded_regression_tests:
            print(
                f"[filter] {case_id}: loại regression không pass trên cả buggy và fixed: "
                + ", ".join(excluded_regression_tests),
                flush=True,
            )
        if len(excluded_regression_tests) == len(regression_tests):
            raise RuntimeError(
                f"{case_id}: no supplemental Arrow regression test passes on both versions"
            )

        report.update(stage="write-config")
        failure_path.write_text(
            "\n".join(
                failure_sections[test].rstrip() for test in eligible_tests
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        write_framework_config(
            config_path,
            failing_tests=eligible_tests,
            image=image,
            runtime=runtime,
            jobs=jobs,
            targets=declared_targets,
            regression_tests=regression_tests,
            excluded_regression_tests=excluded_regression_tests,
        )
        shutil.copy2(config_path, work_dir / config_path.name)
        shutil.copy2(failure_path, work_dir / failure_path.name)

        # Recreate the input from Git as PHP does: builds and test runs can leave
        # files outside the build directory, and the copy back never deletes.
        report.update(stage="clean-input")
        shutil.rmtree(project_root)
        materialize_snapshot(
            source_repo=source_repo,
            target=project_root,
            commit_after=commit_after,
            commit_before=commit_before,
            source_files=source_files,
        )
        provision_dependencies(
            source_repo=source_repo,
            cache_root=cache_root,
            commit=commit_after,
            project_root=project_root,
            log_path=work_dir / "final-dependency-sync.log",
        )
        remove_git_metadata(project_root)
        report.update(stage="publish")
        project_root.rename(final_project)
        config_path.rename(final_config)
        failure_path.rename(final_failure)
        staging.rmdir()
        report.update(
            status="success", stage="complete",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        return final_project, final_config, final_failure
    except BaseException as exc:
        (work_dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        report.update(
            status="failed", error={"type": type(exc).__name__, "message": str(exc)},
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        shutil.rmtree(staging, ignore_errors=True)
        if not input_ready(final_project, final_config, final_failure):
            remove_project_input(final_project, inputs_root, case_id)
            remove_file_input(final_config, inputs_root, f"{case_id}.debugging-framework.json")
            remove_file_input(final_failure, inputs_root, f"{case_id}.failure.log")
        raise


def ensure_source_cache(
    *, cache_root: Path, remote: str, commits: Iterable[str], log_path: Path | None = None
) -> Path:
    if not remote:
        raise ValueError("Arrow project.json has no main_repo")
    cache = cache_root / PROJECT_NAME / "source-cache"
    if not (cache / ".git").is_dir():
        if cache.exists():
            raise RuntimeError(f"Arrow cache is not a Git repository: {cache}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        run_checked(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth=1",
                "--no-checkout",
                remote,
                str(cache),
            ],
            timeout=60 * 30,
            log_path=log_path,
        )
    for sha in commits:
        if git_has_commit(cache, sha):
            continue
        run_checked(
            [
                "git",
                "-C",
                str(cache),
                "fetch",
                "--filter=blob:none",
                "--depth=1",
                "origin",
                sha,
            ],
            timeout=60 * 20,
            log_path=log_path,
        )
        if not git_has_commit(cache, sha):
            raise RuntimeError(f"Fetched Arrow cache does not contain commit: {sha}")
    return cache.resolve()


def materialize_snapshot(
    *,
    source_repo: Path,
    target: Path,
    commit_after: str,
    commit_before: str,
    source_files: list[str],
) -> None:
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
                validate_archive_member(member, "Arrow Git")
                archive.extract(member, path=target)
    except BaseException:
        process.kill()
        process.wait()
        shutil.rmtree(target, ignore_errors=True)
        raise
    error = process.stderr.read().decode("utf-8", errors="replace").strip()
    if process.wait() != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"Cannot export Arrow commit {commit_after}: {error}")
    if not source_files:
        raise ValueError("Bug entry has no files.src")
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


def provision_dependencies(
    *,
    source_repo: Path,
    cache_root: Path,
    commit: str,
    project_root: Path,
    log_path: Path,
) -> list[dict[str, str]]:
    dependencies: list[dict[str, str]] = []
    for relpath, remote in ARROW_SUBMODULES:
        sha = submodule_sha(source_repo, commit, relpath)
        cache = ensure_dependency_cache(
            cache_root=cache_root,
            name=Path(relpath).name,
            remote=remote,
            commit=sha,
            log_path=log_path,
        )
        destination = project_root / relpath
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        materialize_git_tree(cache, destination, sha)
        dependencies.append({"path": relpath, "commit": sha, "remote": remote})
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{relpath}\t{sha}\t{remote}\n")
    return dependencies


def submodule_sha(source_repo: Path, commit: str, relpath: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_repo), "ls-tree", commit, "--", relpath],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    match = re.fullmatch(
        rf"160000\s+commit\s+([0-9a-f]{{40}})\t{re.escape(relpath)}\n?",
        completed.stdout,
    )
    if completed.returncode != 0 or match is None:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"Cannot resolve Arrow submodule {relpath} at {commit}: {detail}"
        )
    return match.group(1)


def ensure_dependency_cache(
    *, cache_root: Path, name: str, remote: str, commit: str,
    log_path: Path | None = None,
) -> Path:
    cache = cache_root / PROJECT_NAME / "dependencies" / name
    if not (cache / ".git").is_dir():
        if cache.exists():
            raise RuntimeError(f"Arrow dependency cache is not a Git repo: {cache}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        run_checked(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth=1",
                "--no-checkout",
                remote,
                str(cache),
            ],
            timeout=60 * 30,
            log_path=log_path,
        )
    if not git_has_commit(cache, commit):
        run_checked(
            [
                "git",
                "-C",
                str(cache),
                "fetch",
                "--filter=blob:none",
                "--depth=1",
                "origin",
                commit,
            ],
            timeout=60 * 20,
            log_path=log_path,
        )
    if not git_has_commit(cache, commit):
        raise RuntimeError(f"Arrow dependency cache lacks commit: {commit}")
    return cache.resolve()


def materialize_git_tree(repo: Path, target: Path, commit: str) -> None:
    if target.exists():
        raise FileExistsError(f"Dependency target already exists: {target}")
    target.mkdir(parents=True)
    process = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                validate_archive_member(member, "Arrow dependency")
                archive.extract(member, path=target)
    except BaseException:
        process.kill()
        process.wait()
        shutil.rmtree(target, ignore_errors=True)
        raise
    error = process.stderr.read().decode("utf-8", errors="replace").strip()
    if process.wait() != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"Cannot export Arrow dependency {commit}: {error}")


def validate_archive_member(member: tarfile.TarInfo, label: str) -> None:
    path = Path(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe path in {label} archive: {member.name}")
    if member.issym() or member.islnk():
        link = PurePosixPath(member.linkname)
        # Symlink targets resolve from the link's directory; Arrow uses
        # in-tree ones such as python/cmake_modules -> ../cpp/cmake_modules.
        # Hard link targets are archive-root paths.
        base = PurePosixPath(member.name).parent if member.issym() else PurePosixPath()
        resolved = posixpath.normpath((base / link).as_posix())
        if link.is_absolute() or resolved == ".." or resolved.startswith("../"):
            raise RuntimeError(
                f"Unsafe link in {label} archive: {member.name} -> {member.linkname}"
            )


def copy_dependencies(source: Path, project_root: Path) -> None:
    for relpath in REQUIRED_DEPENDENCIES:
        current = source / relpath
        if not current.is_dir():
            raise FileNotFoundError(
                f"Prepared Arrow dependency is missing: {current}"
            )
        destination = project_root / relpath
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(current, destination, symlinks=True)


def remove_git_metadata(root: Path) -> None:
    for current, directories, files in os.walk(root):
        if ".git" in directories:
            git_dir = Path(current) / ".git"
            shutil.rmtree(git_dir)
            directories.remove(".git")
        if ".git" in files:
            (Path(current) / ".git").unlink()


def candidate_targets(bug: dict) -> list[str]:
    raw = (bug.get("unittest") or {}).get("name") or []
    raw = [raw] if isinstance(raw, str) else raw
    targets: list[str] = []
    for value in raw:
        target = str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", target):
            raise ValueError(f"Invalid Arrow CTest target: {target!r}")
        if target not in targets:
            targets.append(target)
    return targets


def cmake_targets(ctest_targets: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(ctest_targets))


def arrow_feature_flags(targets: Iterable[str]) -> list[str]:
    selected = set(targets)
    dataset = any("-dataset-" in target for target in selected)
    orc = any("-orc-" in target for target in selected)
    return [
        "-DARROW_COMPUTE=ON",
        f"-DARROW_DATASET={'ON' if dataset else 'OFF'}",
        f"-DARROW_FILESYSTEM={'ON' if dataset else 'OFF'}",
        f"-DARROW_CSV={'ON' if dataset else 'OFF'}",
        f"-DARROW_JSON={'ON' if dataset else 'OFF'}",
        f"-DARROW_ORC={'ON' if orc else 'OFF'}",
        "-DARROW_PARQUET=OFF",
        "-DARROW_DEPENDENCY_SOURCE=SYSTEM",
        "-DGTest_SOURCE=SYSTEM",
        f"-DGTest_DIR={GTEST_CMAKE_DIR}",
        f"-DORC_SOURCE={'BUNDLED' if orc else 'SYSTEM'}",
    ]


def validation_build_commands(jobs: int, targets: Iterable[str]) -> list[list[str]]:
    selected_targets = list(targets)
    build_targets = cmake_targets(selected_targets)
    if not build_targets:
        raise ValueError("Arrow build requires at least one test target")
    # The ORC test links liborc.a explicitly. Linking Arrow shared as well
    # registers ORC's protobuf descriptors twice before GoogleTest can start.
    static_orc = any("-orc-" in target for target in selected_targets)
    return [
        [
            "cmake", "-G", "Ninja", "-S", "cpp", "-B", BUILD_DIR,
            "-DCMAKE_BUILD_TYPE=Debug",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DARROW_BUILD_TESTS=ON",
            f"-DARROW_BUILD_SHARED={'OFF' if static_orc else 'ON'}",
            f"-DARROW_BUILD_STATIC={'ON' if static_orc else 'OFF'}",
            "-DARROW_BUILD_BENCHMARKS=OFF",
            "-DARROW_BUILD_EXAMPLES=OFF",
            "-DARROW_BUILD_INTEGRATION=OFF",
            "-DARROW_BUILD_UTILITIES=OFF",
            "-DARROW_USE_ASAN=OFF",
            "-DARROW_USE_UBSAN=OFF",
            "-DARROW_USE_WERROR=OFF",
            # Older Arrow releases ignore ARROW_USE_WERROR and append -Werror
            # in Debug/CHECKIN builds, overriding our earlier -Wno-error.
            "-DBUILD_WARNING_LEVEL=PRODUCTION",
            "-DARROW_JEMALLOC=OFF",
            "-DARROW_MIMALLOC=OFF",
            "-DARROW_USE_CCACHE=OFF",
            "-DARROW_SIMD_LEVEL=NONE",
            "-DARROW_RUNTIME_SIMD_LEVEL=NONE",
            "-DCMAKE_C_FLAGS=-g -O0 -Wno-error",
            "-DCMAKE_CXX_FLAGS=-g -O0 -Wno-error",
            *arrow_feature_flags(selected_targets),
        ],
        [
            "cmake", "--build", BUILD_DIR, "--target", *build_targets,
            "--parallel", str(jobs),
        ],
    ]


def arrow_test_command(test: str) -> list[str]:
    return [TEST_ADAPTER_COMMAND, "--build-dir", BUILD_DIR, test]


def arrow_target_command(target: str) -> list[str]:
    return [
        TEST_ADAPTER_COMMAND, "--build-dir", BUILD_DIR,
        "--expand-target-failures", target,
    ]


def arrow_batch_command(
    tests: Iterable[str], excluded_tests: Iterable[str] = ()
) -> list[str]:
    selected = list(tests)
    if not selected:
        raise ValueError("Arrow batch command requires at least one test")
    command = [TEST_ADAPTER_COMMAND, "--build-dir", BUILD_DIR]
    for excluded in excluded_tests:
        command.extend(["--exclude-test", excluded])
    for test in selected:
        command.extend(["--test", test])
    return command


def discovery_command(targets: Iterable[str]) -> list[str]:
    command = [TEST_ADAPTER_COMMAND, "--build-dir", BUILD_DIR]
    for target in targets:
        command.extend(["--list-tests", target])
    return command


def write_framework_config(
    path: Path,
    *,
    failing_tests: Iterable[str],
    image: str,
    runtime: str,
    jobs: int,
    targets: Iterable[str],
    regression_tests: Iterable[str],
    excluded_regression_tests: Iterable[str] = (),
) -> None:
    failing = list(dict.fromkeys(failing_tests))
    selected = list(dict.fromkeys(regression_tests))
    excluded = sorted(set(excluded_regression_tests))
    if not failing:
        raise ValueError("Arrow config requires at least one failing test")
    if not 1 <= len(selected) <= REGRESSION_TEST_LIMIT:
        raise ValueError(
            f"Arrow regression set must contain 1..{REGRESSION_TEST_LIMIT} tests"
        )
    if not set(excluded).issubset(selected):
        raise ValueError("Excluded Arrow tests must belong to the selected set")
    if len(excluded) == len(selected):
        raise ValueError("At least one Arrow regression test must remain")
    commands = validation_build_commands(jobs, targets)
    config = {
        "system": "cmake",
        "setup": [commands[0]],
        "build": [commands[1]],
        "target_test": [
            {
                "command": arrow_test_command("{test_id}"),
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "regression_test": [
            {
                "command": arrow_batch_command(selected, excluded),
                "evidence_pattern": TEST_EVIDENCE_PATTERN,
                "failure_pattern": TEST_FAILURE_PATTERN,
            }
        ],
        "repair": {
            "failing_tests": failing,
            "source_extensions": [".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"],
        },
        "workspace": {
            "disposable": True,
            "initialize_git_if_missing": True,
        },
        "environment": {"mode": "image", "runtime": runtime, "image": image},
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def discover_arrow_tests(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    targets: list[str],
    log_path: Path,
    timeout: int,
) -> list[str]:
    command = discovery_command(targets)
    result = run_test_with_artifacts(
        runtime, image, project_root, command, log_path=log_path, timeout=timeout
    )
    log_path.write_text(
        command_section(command, result.returncode, result.stdout), encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(f"Arrow test discovery failed; see {log_path}")
    discovered = sorted(
        {
            match.group(1)
            for match in re.finditer(r"^DISCOVERED\s+(\S+)\s*$", result.stdout, re.MULTILINE)
        }
    )
    if not discovered:
        raise RuntimeError(f"Arrow test discovery returned zero tests; see {log_path}")
    return discovered


def test_suite(test_id: str) -> str:
    _, _, case = test_id.partition("::")
    suite = case.split(".", 1)[0] if case else ""
    parts = [part for part in suite.split("/") if part and not part.isdigit()]
    return parts[-1] if parts else suite


def select_regression_tests(
    discovered_tests: Iterable[str],
    *,
    excluded_tests: Iterable[str],
    seed: str,
    limit: int = REGRESSION_TEST_LIMIT,
) -> list[str]:
    if limit < 1:
        raise ValueError("Arrow regression test limit must be >= 1")
    excluded = set(excluded_tests)
    priority_suites = {test_suite(test) for test in excluded if test_suite(test)}
    candidates = {
        test.strip()
        for test in discovered_tests
        if test.strip() and test.strip() not in excluded
    }
    return sorted(
        candidates,
        key=lambda test: (
            0 if test_suite(test) in priority_suites else 1,
            hashlib.sha256(f"{seed}\0{test}".encode()).digest(),
            test,
        ),
    )[:limit]


def adapter_outcomes(output: str) -> dict[str, str]:
    labels = {"PASSED": "passed", "FAILED": "failed", "SKIPPED": "skipped"}
    return {
        match.group(2): labels[match.group(1)]
        for match in re.finditer(
            r"^(PASSED|FAILED|SKIPPED)\s+(\S+)\s*$", output, re.MULTILINE
        )
    }


def observe_buggy_targets(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    targets: list[str],
    log_path: Path,
    timeout: int,
    record_outcomes: OutcomeRecorder | None = None,
) -> tuple[list[str], dict[str, str]]:
    failed: list[str] = []
    sections: dict[str, str] = {}
    seen: set[str] = set()
    with log_path.open("w", encoding="utf-8") as log:
        for target in targets:
            command = arrow_target_command(target)
            result = run_test_with_artifacts(
                runtime, image, project_root, command, log_path=log_path, timeout=timeout
            )
            log.write(command_section(command, result.returncode, result.stdout))
            if result.returncode not in {0, 1}:
                raise RuntimeError(
                    f"Arrow target {target} exited {result.returncode}; see {log_path}"
                )
            outcomes = adapter_outcomes(result.stdout)
            if record_outcomes:
                record_outcomes("buggy-target", outcomes)
            if not outcomes:
                raise RuntimeError(f"Arrow target was not observed: {target}")
            current = [test for test, outcome in outcomes.items() if outcome == "failed"]
            for test in current:
                if test in seen:
                    continue
                seen.add(test)
                # The target run interleaves every case's output; rerun the
                # failing case alone so the log leads with its assertion.
                case_command = arrow_test_command(test)
                case_result = run_test_with_artifacts(
                    runtime, image, project_root, case_command, log_path=log_path, timeout=timeout
                )
                log.write(
                    command_section(case_command, case_result.returncode, case_result.stdout)
                )
                if case_result.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"Isolated Arrow test {test} exited {case_result.returncode}; see {log_path}"
                    )
                outcome = adapter_outcomes(case_result.stdout).get(test)
                if outcome is None:
                    raise RuntimeError(f"Isolated Arrow test was not observed: {test}")
                if record_outcomes:
                    record_outcomes("buggy-isolated", {test: outcome})
                if outcome != "failed":
                    log.write(f"Excluded {test}: not failed in isolation ({outcome})\n")
                    continue
                failed.append(test)
                sections[test] = (
                    f"===== failing_test: {test} =====\n{case_result.stdout.rstrip()}\n"
                )
    return failed, sections


def observe_tests(
    *,
    runtime: str,
    image: str,
    project_root: Path,
    tests: list[str],
    log_path: Path,
    timeout: int,
    append: bool = False,
    record_outcomes: OutcomeRecorder | None = None,
    phase: str = "buggy-regression",
) -> dict[str, str]:
    command = arrow_batch_command(tests)
    result = run_test_with_artifacts(
        runtime, image, project_root, command, log_path=log_path, timeout=timeout
    )
    with log_path.open("a" if append else "w", encoding="utf-8") as log:
        log.write(command_section(command, result.returncode, result.stdout))
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"Arrow tests exited {result.returncode}; see {log_path}")
    outcomes = adapter_outcomes(result.stdout)
    if record_outcomes:
        record_outcomes(phase, outcomes)
    missing = [test for test in tests if test not in outcomes]
    if missing:
        raise RuntimeError("Arrow outcomes are missing for: " + ", ".join(missing))
    return {test: outcomes[test] for test in tests}


def verify_fixed_oracle(
    *,
    source_repo: Path,
    dependency_source: Path,
    staging: Path,
    bug: dict,
    runtime: str,
    image: str,
    jobs: int,
    targets: list[str],
    target_tests: list[str],
    regression_tests: list[str],
    timeout: int,
    log_path: Path,
    record_outcomes: OutcomeRecorder | None = None,
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
        copy_dependencies(dependency_source, fixed_root)
        remove_git_metadata(fixed_root)
        run_commands_logged(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            commands=validation_build_commands(jobs, targets),
            log_path=log_path,
            timeout=timeout,
        )
        results: dict[str, str] = {}
        with log_path.open("a", encoding="utf-8") as log:
            for test in target_tests:
                command = arrow_test_command(test)
                result = run_test_with_artifacts(
                    runtime, image, fixed_root, command, log_path=log_path, timeout=timeout
                )
                log.write(command_section(command, result.returncode, result.stdout))
                if result.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"Fixed Arrow test {test} exited {result.returncode}; see {log_path}"
                    )
                outcomes = adapter_outcomes(result.stdout)
                if record_outcomes:
                    record_outcomes("fixed-target", outcomes)
                if test not in outcomes:
                    raise RuntimeError(f"Fixed Arrow test was not observed: {test}")
                results[test] = outcomes[test]
        regression_outcomes = observe_tests(
            runtime=runtime,
            image=image,
            project_root=fixed_root,
            tests=regression_tests,
            log_path=log_path,
            timeout=timeout,
            append=True,
            record_outcomes=record_outcomes,
            phase="fixed-regression",
        )
        return results, regression_outcomes
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
    network: bool = False,
) -> subprocess.CompletedProcess:
    network_args = [] if network else ["--network=none"]
    container_name = "defects4c-arrow-" + uuid.uuid4().hex
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
            runtime, "run", "--rm", "--name", container_name, *network_args,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp",
            "-v", f"{project_root.resolve()}:/input:rw",
            "-w", "/tmp", image,
            "sh", "-c", wrapper, "debugging-framework", *command,
        ]
    else:
        argv = [
            runtime, "run", "--rm", "--name", container_name, *network_args,
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
        def decode(value: str | bytes | None) -> str:
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""

        output = decode(exc.stdout) + decode(exc.stderr)
        output += recover_image_test_artifacts(runtime, container_name, project_root, command)
        return subprocess.CompletedProcess(
            argv, 124, output + f"\nTIMEOUT after {timeout}s: {shlex.join(command)}\n"
        )


def recover_image_test_artifacts(
    runtime: str, container: str, project_root: Path, command: list[str],
) -> str:
    """Rescue logs from the macOS temporary workspace before removing a timed-out container."""
    notes: list[str] = []

    def execute(arguments: list[str]) -> bool:
        try:
            result = subprocess.run(
                [runtime, *arguments], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            if result.returncode == 0:
                return True
            notes.append(f"Artifact recovery {arguments[0]} failed: {result.stdout.strip()}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            notes.append(f"Artifact recovery {arguments[0]} failed: {exc}")
        return False

    blocks: list[str] = []
    try:
        if command and command[0] == TEST_ADAPTER_COMMAND:
            paused = execute(["pause", container])
            try:
                workspace = "/tmp/debugging-framework-workspace" if sys.platform == "darwin" else "/workspace"
                with tempfile.TemporaryDirectory(prefix="arrow-recovery-") as directory:
                    rescued = Path(directory)
                    for source, name in (("build/test-logs", "live"), (TEST_ARTIFACT_DIRECTORY, "snapshots")):
                        destination = rescued / name
                        destination.mkdir()
                        execute(["cp", f"{container}:{workspace}/{BUILD_DIR}/{source}/.", str(destination)])
                    files = [path for path in rescued.rglob("*") if path.is_file()]
                    if files:
                        root = project_root / BUILD_DIR / TEST_ARTIFACT_DIRECTORY
                        root.mkdir(parents=True, exist_ok=True)
                        snapshot = root / ("interrupted-" + uuid.uuid4().hex)
                        shutil.copytree(rescued, snapshot)
                        (snapshot / "recovery.json").write_text(
                            json.dumps({"command": command, "reason": "container_command_timeout"}, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        for path in sorted(files):
                            if path.name.endswith(".txt.raw"):
                                content = path.read_text(encoding="utf-8", errors="replace")
                                blocks.append(f"===== recovered artifact: {path.relative_to(rescued)} =====\n{content.rstrip()}\n")
            finally:
                if paused:
                    execute(["unpause", container])
    finally:
        # Killing just the Docker CLI leaves the container/test running.
        execute(["rm", "--force", container])
    return "\n" + "\n".join([*blocks, *notes])


def run_test_with_artifacts(
    runtime: str, image: str, project_root: Path, command: list[str], *,
    log_path: Path, timeout: int,
) -> subprocess.CompletedProcess:
    try:
        return run_in_image(runtime, image, project_root, command, timeout=timeout)
    finally:
        source = project_root / BUILD_DIR / TEST_ARTIFACT_DIRECTORY
        if source.is_dir():
            destination = log_path.parent / "test-artifacts" / log_path.stem
            destination.mkdir(parents=True, exist_ok=True)
            for snapshot in source.iterdir():
                shutil.move(str(snapshot), str(destination / snapshot.name))
            source.rmdir()


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
        and all((project_root / value).is_dir() for value in REQUIRED_DEPENDENCIES)
        and config_path.is_file()
        and framework_config_ready(config_path)
        and not any(path.name == ".git" for path in project_root.rglob(".git"))
        and not (project_root / ".debugging-framework.json").exists()
        and failure_path.is_file()
        and failure_path.stat().st_size > 0
    )


def framework_config_ready(config_path: Path) -> bool:
    try:
        value = read_json(config_path)
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    regression = value.get("regression_test")
    target = value.get("target_test")
    repair = value.get("repair")
    environment = value.get("environment")
    workspace = value.get("workspace")
    if not (
        value.get("system") == "cmake"
        and isinstance(value.get("setup"), list) and len(value["setup"]) == 1
        and isinstance(value.get("build"), list) and len(value["build"]) == 1
        and isinstance(target, list) and len(target) == 1
        and isinstance(regression, list) and len(regression) == 1
        and isinstance(repair, dict) and repair.get("failing_tests")
        and {".cc", ".h"}.issubset(set(repair.get("source_extensions", [])))
        and isinstance(environment, dict)
        and environment.get("mode") == "image"
        and environment.get("runtime")
        and environment.get("image")
        and isinstance(workspace, dict)
        and workspace.get("disposable") is True
        and workspace.get("initialize_git_if_missing") is True
    ):
        return False
    target_args = command_arguments(target[0])
    regression_args = command_arguments(regression[0])
    if target_args is None or regression_args is None:
        return False
    selected = option_values(regression_args, "--test")
    excluded = option_values(regression_args, "--exclude-test")
    return bool(
        target_args[:3] == [TEST_ADAPTER_COMMAND, "--build-dir", BUILD_DIR]
        and target_args[-1] == "{test_id}"
        and regression_args[:3] == [TEST_ADAPTER_COMMAND, "--build-dir", BUILD_DIR]
        and 1 <= len(selected) <= REGRESSION_TEST_LIMIT
        and len(selected) == len(set(selected))
        and set(excluded).issubset(selected)
        and len(set(excluded)) < len(selected)
        and not any("{test_id}" in value for value in regression_args)
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
    if project_root.resolve().parent != inputs_root.resolve() or project_root.name != case_id:
        raise RuntimeError(f"Từ chối xóa input path không an toàn: {project_root}")
    shutil.rmtree(project_root)


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


def run_checked(command: list[str], *, timeout: int, log_path: Path | None = None) -> None:
    if log_path is None:
        completed = subprocess.run(command, timeout=timeout, check=False)
    else:
        # Stream Git progress straight to disk, including partial output on timeout.
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"===== command: {shlex.join(command)} =====\n")
            log.flush()
            try:
                completed = subprocess.run(
                    command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False
                )
            except subprocess.TimeoutExpired as exc:
                log.write(f"\nTIMEOUT after {timeout}s\n")
                raise RuntimeError(f"Command timed out; see {log_path}") from exc
            log.write(f"\nreturncode: {completed.returncode}\n")
    if completed.returncode != 0:
        detail = f"; see {log_path}" if log_path else ""
        raise RuntimeError(f"Command failed ({completed.returncode}){detail}: {shlex.join(command)}")


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
        description="Create Apache Arrow inputs, then call Debugging-Framework."
    )
    parser.add_argument(
        "action", nargs="?", default="trial",
        choices=("prepare", "show", "doctor", "repair", "trial"),
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--bug", "--sha", dest="selectors", action="append",
        help="Unique bug type.id or commit_after prefix; repeatable.",
    )
    selection.add_argument("--all", action="store_true", help="Process all bugs.")
    selection.add_argument("--list", action="store_true", help="List bugs and exit.")
    parser.add_argument("--inputs-root", type=Path, default=DEFAULT_INPUTS_ROOT)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--jobs", type=int, help="Prepare uses 4 when omitted.")
    parser.add_argument("--attempts", type=int)
    parser.add_argument(
        "--command-timeout", type=int,
        help="Prepare uses 7200 seconds per command when omitted.",
    )
    parser.add_argument("--agent-timeout", type=int)
    parser.add_argument("--harness", default="")
    parser.add_argument("--retrieval-model", default="")
    parser.add_argument("--repair-model", default="")
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
                print_commands(
                    args,
                    resolve_framework_for_display(args.framework_bin),
                    project_root,
                    config_path,
                    failure_path,
                )
                continue
            assert framework_bin is not None
            if args.action in {"doctor", "trial"}:
                doctor = [
                    str(framework_bin), "doctor", str(project_root),
                    "--config", str(config_path),
                ]
                if args.jobs is not None:
                    doctor.extend(["--jobs", str(args.jobs)])
                append_agent_options(doctor, args)
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
    for name in ("jobs", "attempts", "command_timeout", "agent_timeout"):
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


def append_agent_options(command: list[str], args: argparse.Namespace) -> None:
    for flag, value in (
        ("--harness", args.harness),
        ("--retrieval-model", args.retrieval_model),
        ("--repair-model", args.repair_model),
    ):
        if value:
            command.extend([flag, value])


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
        ("--agent-timeout", args.agent_timeout),
        ("--jobs", args.jobs),
    ):
        if value is not None:
            command.extend([flag, str(value)])
    append_agent_options(command, args)
    return command


def print_contract(project_root: Path, config_path: Path, failure_path: Path) -> None:
    config = read_json(config_path)
    failing = ((config.get("repair") or {}).get("failing_tests") or [])
    regression = ((config.get("regression_test") or [{}])[0].get("command") or [])
    print("[inputs ready]")
    print(f"  project:       {project_root}")
    print(f"  config:        {config_path}")
    print(f"  failure log:   {failure_path}")
    print(f"  failing tests: {', '.join(failing)}")
    print(f"  regression:    {regression.count('--test')} selected")


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
    append_agent_options(doctor, args)
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
