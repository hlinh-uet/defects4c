#!/usr/bin/env python3
"""Run exact tcpdump TESTLIST entries and emit framework outcomes."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


TESTLIST_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$")


@dataclass(frozen=True)
class TestEntry:
    name: str
    input_file: str
    expected_file: str
    options: str = ""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--test", action="append", default=[])
    result.add_argument("--exclude-test", action="append", default=[])
    result.add_argument("--list-tests", action="store_true")
    result.add_argument("--test-timeout", type=int, default=120)
    return result


def parse_testlist(path: Path) -> dict[str, TestEntry]:
    if not path.is_file():
        raise FileNotFoundError(f"tcpdump TESTLIST is missing: {path}")
    entries: dict[str, TestEntry] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = TESTLIST_RE.match(raw_line)
        if not match:
            continue
        entry = TestEntry(
            name=match.group(1),
            input_file=match.group(2),
            expected_file=match.group(3),
            options=(match.group(4) or "").strip(),
        )
        if entry.name in entries:
            raise ValueError(f"duplicate tcpdump TESTLIST id: {entry.name}")
        entries[entry.name] = entry
    if not entries:
        raise RuntimeError("tcpdump TESTLIST contains zero runnable tests")
    return entries


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def validate_entry(tests_dir: Path, entry: TestEntry) -> None:
    if entry.name.startswith("-") or any(char.isspace() for char in entry.name):
        raise ValueError(f"unsafe tcpdump TESTLIST id: {entry.name!r}")
    for value in (entry.input_file, entry.expected_file):
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe tcpdump test asset: {value}")
        if not (tests_dir / relative).is_file():
            raise FileNotFoundError(f"tcpdump test asset is missing: {value}")


def decode_timeout_output(exc: subprocess.TimeoutExpired) -> str:
    values: list[str] = []
    for value in (exc.stdout, exc.stderr):
        if isinstance(value, bytes):
            values.append(value.decode("utf-8", errors="replace"))
        elif value:
            values.append(str(value))
    return "".join(values)


def run_one(root: Path, entry: TestEntry, timeout: int) -> tuple[str, str]:
    tests_dir = root / "tests"
    validate_entry(tests_dir, entry)
    runner = tests_dir / "TESTonce"
    binary = root / "tcpdump"
    if not runner.is_file():
        raise FileNotFoundError(f"tcpdump TESTonce is missing: {runner}")
    if not binary.is_file():
        raise FileNotFoundError(f"tcpdump binary is missing: {binary}")
    command = [
        "./TESTonce", entry.name, entry.input_file, entry.expected_file, entry.options
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=tests_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = completed.stdout
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = decode_timeout_output(exc) + f"\ntcpdump test timed out after {timeout}s\n"
        returncode = 124
    explicit_failure = "TEST FAILED" in output
    skipped = bool(re.search(r"\b(?:TEST\s+)?SKIPPED\b", output, re.IGNORECASE))
    failure = returncode != 0 or explicit_failure
    diff_path = tests_dir / f"{entry.name}.diff"
    if failure and diff_path.is_file():
        diff = diff_path.read_text(encoding="utf-8", errors="replace")
        if diff.strip():
            output += f"\n===== {diff_path.name} =====\n{diff}"
    if skipped and not explicit_failure:
        return "SKIPPED", output
    if failure:
        return "FAILED", output
    return "PASSED", output


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.test_timeout < 1:
        print("--test-timeout must be >= 1", file=sys.stderr)
        return 2
    root = Path.cwd()
    entries = parse_testlist(root / "tests" / "TESTLIST")
    if args.list_tests:
        if args.test or args.exclude_test:
            print("--list-tests cannot be combined with test selection", file=sys.stderr)
            return 2
        for test_id in sorted(entries):
            print(f"DISCOVERED {test_id}")
        return 0

    selected = unique(args.test)
    excluded = set(unique(args.exclude_test))
    if not selected:
        print("at least one --test is required", file=sys.stderr)
        return 2
    unknown = sorted(set(selected).difference(entries))
    if unknown:
        print("tcpdump tests are not in TESTLIST: " + ", ".join(unknown), file=sys.stderr)
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
    outputs: list[str] = []
    for test in active:
        outcome, output = run_one(root, entries[test], args.test_timeout)
        outcomes[test] = outcome
        if outcome != "PASSED" and output.strip():
            outputs.append(f"===== tcpdump test: {test} =====\n{output.rstrip()}")
    if outputs:
        print("\n".join(outputs))
    for test, outcome in outcomes.items():
        print(f"{outcome} {test}")
    return 1 if any(outcome == "FAILED" for outcome in outcomes.values()) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
