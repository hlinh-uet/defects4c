#!/usr/bin/env python3
"""Run exact curl runtests.pl cases and emit framework outcomes."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from pathlib import Path


PASS_RE = re.compile(r"^TESTDONE:\s+1 tests out of 1 reported OK:\s+100%\s*$", re.MULTILINE)
SKIP_RE = re.compile(
    r"(?:^test\s+\d+\s+SKIPPED:|^TESTINFO:\s+1 tests were skipped\b)",
    re.MULTILINE,
)
FAIL_RE = re.compile(r"^TESTFAIL:", re.MULTILINE)
AUTOMAKE_RE = re.compile(r"^(PASS|FAIL):\s+(\d+)\b", re.MULTILINE)
CONSIDERED_RE = re.compile(
    r"^TESTDONE:\s+(\d+) tests were considered during\b", re.MULTILINE
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--test", action="append", default=[])
    result.add_argument("--exclude-test", action="append", default=[])
    result.add_argument("--test-timeout", type=int, default=180)
    result.add_argument("--list-tests", action="store_true")
    return result


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def discover_tests(root: Path) -> list[str]:
    data_dir = root / "tests" / "data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"curl test data directory is missing: {data_dir}")
    tests = sorted(
        {
            match.group(1)
            for path in data_dir.iterdir()
            if path.is_file() and (match := re.fullmatch(r"test(\d+)", path.name))
        },
        key=int,
    )
    if not tests:
        raise RuntimeError("curl test data contains zero runnable tests")
    return tests


def run_command(
    command: list[str], *, cwd: Path, timeout: int
) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        return 124, output + f"\ncurl test timed out after {timeout}s\n"


def classify_outcome(output: str, returncode: int) -> str | None:
    if returncode == 124:
        return "FAILED"
    if SKIP_RE.search(output) and returncode == 0:
        return "SKIPPED"
    if returncode != 0 or FAIL_RE.search(output):
        return "FAILED"
    if PASS_RE.search(output):
        return "PASSED"
    return None


def run_one(root: Path, test_id: str, timeout: int) -> tuple[str, str]:
    tests_dir = root / "tests"
    runner = tests_dir / "runtests.pl"
    binary = root / "src" / "curl"
    if not runner.is_file():
        raise FileNotFoundError(f"curl test runner is missing: {runner}")
    if not binary.is_file():
        raise FileNotFoundError(f"curl binary is missing: {binary}")
    returncode, output = run_command(
        ["perl", "./runtests.pl", "-n", test_id],
        cwd=tests_dir,
        timeout=timeout,
    )
    outcome = classify_outcome(output, returncode)
    if outcome is None:
        raise RuntimeError(f"curl test outcome was not observed: {test_id}")
    return outcome, output


def run_many(
    root: Path, test_ids: list[str], timeout_per_test: int
) -> tuple[dict[str, str], str]:
    tests_dir = root / "tests"
    runner = tests_dir / "runtests.pl"
    binary = root / "src" / "curl"
    if not runner.is_file():
        raise FileNotFoundError(f"curl test runner is missing: {runner}")
    if not binary.is_file():
        raise FileNotFoundError(f"curl binary is missing: {binary}")
    returncode, output = run_command(
        ["perl", "./runtests.pl", "-n", "-a", "-am", *test_ids],
        cwd=tests_dir,
        timeout=timeout_per_test * len(test_ids),
    )
    if returncode == 124:
        raise RuntimeError("curl regression batch timed out")
    if returncode not in {0, 1}:
        raise RuntimeError(f"curl regression batch exited {returncode}")
    outcomes: dict[str, str] = {}
    for label, test_id in AUTOMAKE_RE.findall(output):
        if test_id in outcomes:
            raise RuntimeError(f"duplicate curl test outcome: {test_id}")
        outcomes[test_id] = "PASSED" if label == "PASS" else "FAILED"
    considered = CONSIDERED_RE.search(output)
    if considered is None or int(considered.group(1)) != len(test_ids):
        raise RuntimeError("curl regression batch completion was not observed")
    for test_id in test_ids:
        outcomes.setdefault(test_id, "SKIPPED")
    return outcomes, output


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.test_timeout < 1:
        print("--test-timeout must be >= 1", file=sys.stderr)
        return 2
    root = Path.cwd()
    available = discover_tests(root)
    if args.list_tests:
        if args.test or args.exclude_test:
            print("--list-tests cannot be combined with test selection", file=sys.stderr)
            return 2
        for test_id in available:
            print(f"DISCOVERED {test_id}")
        return 0

    selected = unique(args.test)
    excluded = set(unique(args.exclude_test))
    if not selected:
        print("at least one --test is required", file=sys.stderr)
        return 2
    invalid_ids = sorted(test for test in selected if not re.fullmatch(r"\d+", test))
    if invalid_ids:
        print("invalid curl test ids: " + ", ".join(invalid_ids), file=sys.stderr)
        return 2
    unknown = sorted(set(selected).difference(available), key=int)
    if unknown:
        print("curl tests are missing: " + ", ".join(unknown), file=sys.stderr)
        return 2
    invalid_exclusions = sorted(excluded.difference(selected), key=int)
    if invalid_exclusions:
        print(
            "excluded tests are not selected: " + ", ".join(invalid_exclusions),
            file=sys.stderr,
        )
        return 2
    active = [test for test in selected if test not in excluded]
    if not active:
        print("all selected tests were excluded", file=sys.stderr)
        return 2
    for test in sorted(excluded, key=int):
        print(f"EXCLUDED {test}")

    outcomes: dict[str, str] = {}
    if len(active) == 1:
        test = active[0]
        outcome, output = run_one(root, test, args.test_timeout)
        outcomes[test] = outcome
        if outcome != "PASSED" and output.strip():
            print(f"===== curl test: {test} =====")
            print(output, end="" if output.endswith("\n") else "\n")
    else:
        outcomes, output = run_many(root, active, args.test_timeout)
        if any(outcome != "PASSED" for outcome in outcomes.values()):
            print("===== curl regression batch =====")
            print(output, end="" if output.endswith("\n") else "\n")
    for test, outcome in outcomes.items():
        print(f"{outcome} {test}")
    return 1 if any(outcome == "FAILED" for outcome in outcomes.values()) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
