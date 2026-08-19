#!/usr/bin/env python3
"""Run exact libbson TestSuite selections and emit framework outcomes."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from pathlib import Path


BUILD_DIR = Path(".debugging-framework/build")
TEST_BINARY = BUILD_DIR / "src" / "libmongoc" / "test-libmongoc"
TEST_REGISTRATION_RE = re.compile(
    r'\bTestSuite_Add[A-Za-z0-9_]*\s*\(\s*'
    r'[A-Za-z_][A-Za-z0-9_]*\s*,\s*"(/[^"]+)"',
    re.MULTILINE,
)
STATUS_RE = re.compile(r'"status"\s*:\s*"([A-Za-z_]+)"', re.IGNORECASE)


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
    tests_dir = root / "src" / "libbson" / "tests"
    if not tests_dir.is_dir():
        raise FileNotFoundError(f"libbson tests are missing: {tests_dir}")
    tests: set[str] = set()
    for path in sorted(tests_dir.rglob("*.c")):
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"//[^\n]*", "", text)
        tests.update(TEST_REGISTRATION_RE.findall(text))
    discovered = sorted(tests)
    if not discovered:
        raise RuntimeError("mongo-c-driver contains zero registered libbson tests")
    return discovered


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
        return 124, output + f"\nlibbson test timed out after {timeout}s\n"


def classify_outcome(output: str, returncode: int) -> str | None:
    statuses = [status.upper() for status in STATUS_RE.findall(output)]
    if returncode == 124:
        return "FAILED"
    if returncode != 0 or any(status in {"FAIL", "FAILED", "ERROR"} for status in statuses):
        return "FAILED"
    if any(status in {"PASS", "PASSED", "SUCCESS"} for status in statuses):
        return "PASSED"
    if statuses and all(status in {"SKIP", "SKIPPED"} for status in statuses):
        return "SKIPPED"
    return None


def run_one(root: Path, test_id: str, timeout: int) -> tuple[str, str]:
    binary = root / TEST_BINARY
    if not binary.is_file():
        raise FileNotFoundError(
            f"combined mongo-c-driver test executable is missing: {binary}"
        )
    returncode, output = run_command(
        [str(binary), "-l", test_id], cwd=root, timeout=timeout
    )
    outcome = classify_outcome(output, returncode)
    if outcome is None:
        raise RuntimeError(f"libbson test outcome was not observed: {test_id}")
    return outcome, output


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
    invalid = sorted(
        test for test in selected if not re.fullmatch(r"/[A-Za-z0-9_./:+-]+", test)
    )
    if invalid:
        print("invalid libbson test ids: " + ", ".join(invalid), file=sys.stderr)
        return 2
    unknown = sorted(set(selected).difference(available))
    if unknown:
        print("libbson tests are missing: " + ", ".join(unknown), file=sys.stderr)
        return 2
    invalid_exclusions = sorted(excluded.difference(selected))
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
    for test in sorted(excluded):
        print(f"EXCLUDED {test}")

    outcomes: dict[str, str] = {}
    for test in active:
        outcome, output = run_one(root, test, args.test_timeout)
        outcomes[test] = outcome
        if outcome != "PASSED" and output.strip():
            print(f"===== libbson test: {test} =====")
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
