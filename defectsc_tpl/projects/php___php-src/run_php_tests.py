#!/usr/bin/env python3
"""Run exact php-src PHPT selections and emit framework test outcomes."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


SUMMARY_RE = re.compile(
    r"^(?P<label>Number of tests|Tests (?:passed|skipped|warned|failed|borked|leaked)|"
    r"Expected fail)\s*:\s*(?P<count>\d+)\b",
    re.MULTILINE | re.IGNORECASE,
)
BAD_LABELS = {"tests warned", "tests failed", "tests borked", "tests leaked"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--php", default="sapi/cli/php")
    result.add_argument("--runner", default="run-tests.php")
    result.add_argument("--test-timeout", type=int, default=180)
    result.add_argument("--test", action="append", default=[])
    result.add_argument("--exclude-test", action="append", default=[])
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    if args.test_timeout < 1:
        print("--test-timeout must be >= 1", file=sys.stderr)
        return 2
    selected = unique(args.test)
    excluded = set(unique(args.exclude_test))
    if not selected:
        print("at least one --test is required", file=sys.stderr)
        return 2
    unknown = sorted(excluded.difference(selected))
    if unknown:
        print("excluded tests are not selected: " + ", ".join(unknown), file=sys.stderr)
        return 2
    active = [test for test in selected if test not in excluded]
    if not active:
        print("all selected tests were excluded", file=sys.stderr)
        return 2

    php = Path(args.php)
    runner = Path(args.runner)
    if not php.is_file():
        print(f"php executable not found: {php}", file=sys.stderr)
        return 2
    if not runner.is_file():
        print(f"PHP test runner not found: {runner}", file=sys.stderr)
        return 2
    for test in active:
        path = Path(test)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".phpt":
            print(f"unsafe or unsupported PHPT test path: {test}", file=sys.stderr)
            return 2
        if not path.is_file():
            print(f"PHPT test not found: {test}", file=sys.stderr)
            return 2

    for test in sorted(excluded):
        print(f"EXCLUDED {test}")

    outcomes: dict[str, str] = {}
    for test in active:
        command = [
            str(php.resolve()), str(runner.resolve()), "-q", "-p", str(php.resolve()),
            "-g", "FAIL,XFAIL,BORK,WARN,LEAK,SKIP", test,
        ]
        env = os.environ.copy()
        env["NO_INTERACTION"] = "1"
        env["TEST_PHP_EXECUTABLE"] = str(php.resolve())
        print(f"===== PHPT {test} =====", flush=True)
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.test_timeout,
                check=False,
            )
            output = completed.stdout
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            output = decode_timeout_output(exc)
            returncode = 124
            output += f"\nPHPT timed out after {args.test_timeout}s\n"
        except OSError as exc:
            print(f"cannot execute PHPT runner: {exc}", file=sys.stderr)
            return 2
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
        outcome = classify_outcome(output, returncode)
        if outcome is None:
            print(f"PHPT outcome was not observed: {test}", file=sys.stderr)
            return 2
        outcomes[test] = outcome

    for test, outcome in outcomes.items():
        print(f"{outcome} {test}")
    return 1 if any(outcome == "FAILED" for outcome in outcomes.values()) else 0


def classify_outcome(output: str, returncode: int) -> str | None:
    counts = {
        match.group("label").strip().lower(): int(match.group("count"))
        for match in SUMMARY_RE.finditer(output)
    }
    if returncode == 124:
        return "FAILED"
    if any(counts.get(label, 0) > 0 for label in BAD_LABELS):
        return "FAILED"
    if counts.get("tests passed", 0) > 0 and returncode == 0:
        return "PASSED"
    if (
        counts.get("tests skipped", 0) > 0
        or counts.get("expected fail", 0) > 0
    ) and returncode == 0:
        return "SKIPPED"
    if returncode != 0:
        return "FAILED"
    return None


def decode_timeout_output(exc: subprocess.TimeoutExpired) -> str:
    values = []
    for value in (exc.stdout, exc.stderr):
        if isinstance(value, bytes):
            values.append(value.decode("utf-8", errors="replace"))
        elif value:
            values.append(str(value))
    return "".join(values)


def unique(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        normalized = str(value).strip().replace("\\", "/")
        if normalized and normalized not in result:
            result.append(normalized)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
