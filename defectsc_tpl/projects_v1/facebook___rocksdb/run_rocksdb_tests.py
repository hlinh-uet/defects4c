#!/usr/bin/env python3
"""Discover and run RocksDB tests at GoogleTest-case granularity."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def run(
    command: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: int,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return subprocess.CompletedProcess(command, 124, stdout + stderr + "\nTIMEOUT\n")


def test_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GTEST_THROW_ON_FAILURE", "0")
    env.setdefault("GTEST_COLOR", "no")
    env.setdefault("SKIP_FORMAT_BUCK_CHECKS", "1")
    return env


def find_test_binary(build_dir: Path, binary_name: str) -> Path:
    if not binary_name or Path(binary_name).name != binary_name:
        raise ValueError(f"Invalid RocksDB test binary: {binary_name}")
    candidates = [
        build_dir / binary_name,
        build_dir / "test" / binary_name,
        build_dir / "db" / binary_name,
        build_dir / "options" / binary_name,
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    for candidate in build_dir.rglob(binary_name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(f"RocksDB test binary not found: {binary_name}")


def parse_gtest_list(output: str) -> list[str]:
    cases: list[str] = []
    seen: set[str] = set()
    suite = ""
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            value = line.split("#", 1)[0].strip()
            suite = value if value.endswith(".") else ""
            continue
        if not suite:
            continue
        case = line.strip().split("#", 1)[0].strip()
        if not case:
            continue
        test = f"{suite}{case}"
        disabled_suite = any(
            part.startswith("DISABLED_") for part in suite.rstrip(".").split("/")
        )
        disabled_case = any(
            part.startswith("DISABLED_") for part in case.split("/")
        )
        if disabled_suite or disabled_case:
            continue
        if test not in seen:
            seen.add(test)
            cases.append(test)
    return cases


def list_tests(build_dir: Path, binaries: Iterable[str], timeout: int) -> list[str]:
    selected: list[str] = []
    for binary_name in binaries:
        binary = find_test_binary(build_dir, binary_name)
        result = run(
            [str(binary), "--gtest_list_tests"],
            cwd=binary.parent,
            env=test_environment(),
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"GoogleTest discovery failed for {binary_name} ({result.returncode}):\n{result.stdout}"
            )
        cases = parse_gtest_list(result.stdout)
        if not cases:
            raise RuntimeError(f"GoogleTest discovery returned zero tests: {binary_name}")
        for case in cases:
            test_id = f"{binary_name}::{case}"
            if test_id not in selected:
                selected.append(test_id)
    return selected


def split_test_id(test_id: str) -> tuple[str, str]:
    binary_name, separator, case = test_id.partition("::")
    if not separator or not binary_name or not case:
        raise ValueError(f"Invalid RocksDB test id: {test_id}")
    return binary_name, case


def run_one(build_dir: Path, test_id: str, timeout: int) -> tuple[str, str]:
    binary_name, case = split_test_id(test_id)
    binary = find_test_binary(build_dir, binary_name)
    result = run(
        [str(binary), f"--gtest_filter={case}", "--gtest_color=no"],
        cwd=binary.parent,
        env=test_environment(),
        timeout=timeout,
    )
    output = result.stdout
    zero_tests = bool(re.search(r"Running\s+0\s+tests?|0 tests? from 0 test", output))
    skipped = bool(re.search(r"\[\s*SKIPPED\s*\]", output))
    failure_evidence = bool(
        re.search(
            r"\[\s*FAILED\s*\]|FAILED TEST|AddressSanitizer|"
            r"UndefinedBehaviorSanitizer|runtime error:|TIMEOUT",
            output,
            re.IGNORECASE,
        )
    )
    run_evidence = bool(re.search(r"\[\s*RUN\s*\]\s+\S+", output))
    pass_evidence = bool(
        re.search(r"\[\s*OK\s*\]\s+\S+|\[\s*PASSED\s*\]\s+[1-9][0-9]*\s+tests?", output)
    )
    if zero_tests:
        return "skipped", output
    if skipped and not failure_evidence:
        return "skipped", output
    if result.returncode != 0 or failure_evidence:
        return "failed", output
    if not run_evidence or not pass_evidence:
        return "failed", output + "\nMissing GoogleTest execution evidence\n"
    return "passed", output


def emit_outcomes(outcomes: dict[str, str], outputs: list[str]) -> int:
    if outputs:
        print("\n".join(value.rstrip() for value in outputs if value.strip()))
    labels = {"passed": "PASSED", "failed": "FAILED", "skipped": "SKIPPED"}
    for test_id, outcome in outcomes.items():
        print(f"{labels[outcome]} {test_id}")
    return 0 if outcomes and all(value == "passed" for value in outcomes.values()) else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("test_id", nargs="?")
    result.add_argument("--build-dir", type=Path, required=True)
    result.add_argument("--list-tests", action="append", default=[])
    result.add_argument("--test", action="append", default=[])
    result.add_argument("--exclude-test", action="append", default=[])
    result.add_argument("--timeout", type=int, default=300)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.timeout < 1:
        raise ValueError("--timeout must be >= 1")
    modes = int(bool(args.list_tests)) + int(bool(args.test)) + int(bool(args.test_id))
    if modes != 1:
        raise ValueError("choose exactly one of --list-tests, --test, or test_id")
    build_dir = args.build_dir.resolve()
    if not build_dir.is_dir():
        raise FileNotFoundError(f"RocksDB build directory not found: {build_dir}")
    if args.list_tests:
        for test_id in list_tests(build_dir, args.list_tests, args.timeout):
            print(f"DISCOVERED {test_id}")
        return 0

    selected = args.test or [args.test_id]
    excluded = set(args.exclude_test)
    outcomes: dict[str, str] = {}
    outputs: list[str] = []
    for test_id in selected:
        if test_id in excluded:
            continue
        outcome, output = run_one(build_dir, test_id, args.timeout)
        outcomes[test_id] = outcome
        if outcome != "passed":
            outputs.append(output)
    return emit_outcomes(outcomes, outputs)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
