#!/usr/bin/env python3
"""Discover and run SPIRV-Tools tests at GoogleTest-case granularity."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CTestEntry:
    name: str
    command: tuple[str, ...]
    working_directory: str = ""


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
        output = str(exc.stdout or "") + str(exc.stderr or "")
        return subprocess.CompletedProcess(command, 124, output + "\nTIMEOUT\n")


def ctest_entries(build_dir: Path, timeout: int) -> dict[str, CTestEntry]:
    result = run(
        ["ctest", "--test-dir", str(build_dir), "--show-only=json-v1"],
        cwd=build_dir.parent,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CTest discovery failed ({result.returncode}):\n{result.stdout}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CTest discovery returned invalid JSON: {exc}") from exc
    entries: dict[str, CTestEntry] = {}
    for test in payload.get("tests", []):
        name = str(test.get("name") or "").strip()
        command = tuple(str(value) for value in test.get("command", []))
        if not name or not command:
            continue
        working_directory = ""
        for prop in test.get("properties", []):
            if prop.get("name") == "WORKING_DIRECTORY":
                working_directory = str(prop.get("value") or "")
                break
        entries[name] = CTestEntry(name, command, working_directory)
    if not entries:
        raise RuntimeError("CTest discovered zero runnable tests")
    return entries


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
            suite = value[:-1] if value.endswith(".") else ""
            continue
        if not suite:
            continue
        case = line.strip().split("#", 1)[0].strip()
        if not case or suite.startswith("DISABLED_") or case.startswith("DISABLED_"):
            continue
        test = f"{suite}.{case}"
        if test not in seen:
            cases.append(test)
            seen.add(test)
    return cases


def list_gtest_cases(entry: CTestEntry, timeout: int) -> list[str]:
    env = os.environ.copy()
    env.setdefault("GTEST_COLOR", "no")
    result = run(
        [*entry.command, "--gtest_list_tests"],
        cwd=entry.working_directory or None,
        env=env,
        timeout=timeout,
    )
    return parse_gtest_list(result.stdout) if result.returncode == 0 else []


def test_ids(
    entries: dict[str, CTestEntry], targets: Iterable[str], timeout: int
) -> list[str]:
    selected: list[str] = []
    for target in targets:
        entry = entries.get(target)
        if entry is None:
            raise RuntimeError(f"CTest target does not exist: {target}")
        cases = list_gtest_cases(entry, timeout)
        values = [f"{target}::{case}" for case in cases] or [target]
        for value in values:
            if value not in selected:
                selected.append(value)
    return selected


def split_test_id(test_id: str) -> tuple[str, str]:
    target, separator, case = test_id.partition("::")
    if not target or (separator and not case):
        raise ValueError(f"Invalid SPIRV test id: {test_id}")
    return target, case


def parse_failed_gtests(output: str) -> list[str]:
    failed: list[str] = []
    for line in output.splitlines():
        match = re.search(r"(?:^|\s)\[\s*FAILED\s*\]\s+(\S+)", line)
        if not match:
            continue
        case = match.group(1).rstrip(".,:")
        if "." not in case or case[0].isdigit():
            continue
        if case not in failed:
            failed.append(case)
    return failed


def run_one(
    build_dir: Path,
    entries: dict[str, CTestEntry],
    test_id: str,
    timeout: int,
    *,
    expand_target_failures: bool = False,
) -> tuple[dict[str, str], str]:
    target, case = split_test_id(test_id)
    entry = entries.get(target)
    if entry is None:
        raise RuntimeError(f"CTest target does not exist: {target}")
    env = os.environ.copy()
    env.setdefault("GTEST_COLOR", "no")
    if case:
        command = [*entry.command, f"--gtest_filter={case}", "--gtest_color=no"]
        cwd: Path | str | None = entry.working_directory or build_dir
    else:
        command = [
            "ctest", "--test-dir", str(build_dir), "-R", f"^{re.escape(target)}$",
            "-V", "--output-on-failure", "--timeout", str(timeout),
        ]
        cwd = build_dir.parent
    result = run(command, cwd=cwd, env=env, timeout=timeout + 30)
    output = result.stdout
    zero_tests = bool(
        re.search(r"Running\s+0\s+tests|0 tests? from 0 test|No tests were found", output)
    )
    skipped = bool(
        re.search(r"\[\s*SKIPPED\s*\]|\*\*\*Skipped|\*\*\*Not Run", output)
    )
    failure_evidence = bool(
        re.search(r"\[\s*FAILED\s*\]|\*\*\*Failed|\*\*\*Timeout|TIMEOUT", output)
    )
    failed = result.returncode != 0 or failure_evidence
    observed = bool(
        re.search(
            r"Running\s+[1-9][0-9]*\s+tests?|"
            r"\[\s*PASSED\s*\]\s+[1-9][0-9]*\s+tests?|"
            r"[1-9][0-9]*% tests passed|"
            r"[1-9][0-9]*/[1-9][0-9]* Test\s+#",
            output,
        )
    )
    if not case and expand_target_failures and failed:
        failed_cases = parse_failed_gtests(output)
        if failed_cases:
            return ({f"{target}::{value}": "failed" for value in failed_cases}, output)
    if zero_tests:
        return {test_id: "skipped"}, output
    if skipped and not failure_evidence:
        return {test_id: "skipped"}, output
    if failed:
        return {test_id: "failed"}, output
    if not observed:
        return {test_id: "failed"}, output + "\nMissing test execution evidence\n"
    return {test_id: "passed"}, output


def emit_outcomes(outcomes: dict[str, str], output: str) -> int:
    if any(outcome != "passed" for outcome in outcomes.values()) and output.strip():
        print(output.rstrip())
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
    result.add_argument("--expand-target-failures", action="store_true")
    result.add_argument("--timeout", type=int, default=180)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.timeout < 1:
        raise ValueError("--timeout must be >= 1")
    modes = int(bool(args.list_tests)) + int(bool(args.test)) + int(bool(args.test_id))
    if modes != 1:
        raise ValueError("choose exactly one of --list-tests, --test, or test_id")
    build_dir = args.build_dir.resolve()
    entries = ctest_entries(build_dir, args.timeout)
    if args.list_tests:
        for test_id in test_ids(entries, args.list_tests, args.timeout):
            print(f"DISCOVERED {test_id}")
        return 0

    selected = args.test or [args.test_id]
    excluded = set(args.exclude_test)
    outcomes: dict[str, str] = {}
    outputs: list[str] = []
    for test_id in selected:
        if test_id in excluded:
            continue
        current, output = run_one(
            build_dir,
            entries,
            test_id,
            args.timeout,
            expand_target_failures=args.expand_target_failures,
        )
        outcomes.update(current)
        if any(value != "passed" for value in current.values()):
            outputs.append(output)
    return emit_outcomes(outcomes, "\n".join(outputs))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
