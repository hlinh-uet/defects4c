#!/usr/bin/env python3
"""Run bounded LLVM lit selections and emit Debugging-Framework test IDs."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


RESULT_RE = re.compile(
    r"^(?P<status>PASS|FLAKYPASS|FAIL|XPASS|XFAIL|UNSUPPORTED|UNRESOLVED|TIMEOUT):\s+"
    r"(?P<suite>[^:]+?)\s+::\s+(?P<test>.+)\s*$"
)
DISCOVERED_RE = re.compile(
    r"^\s*(?P<suite>[^:]+?)\s+::\s+(?P<test>\S.*)\s*$"
)
FAILED_STATUSES = {"FAIL", "XPASS", "UNRESOLVED", "TIMEOUT"}
SKIPPED_STATUSES = {"XFAIL", "UNSUPPORTED"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--build-dir", required=True)
    result.add_argument(
        "--list-tests",
        action="store_true",
        help="Discover tests under selection without executing them.",
    )
    result.add_argument(
        "--test",
        action="append",
        default=[],
        help="Exact test selection; repeat for a bounded regression set.",
    )
    result.add_argument(
        "--exclude-test",
        action="append",
        default=[],
        help="Exact test ID to remove from the selected regression set.",
    )
    result.add_argument("selection", nargs="?")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    build_dir = Path(args.build_dir)
    lit = build_dir / "bin" / "llvm-lit"
    if not lit.is_file():
        print(f"llvm-lit not found: {lit}", file=sys.stderr)
        return 2

    if args.list_tests:
        if not args.selection or args.test or args.exclude_test:
            print(
                "--list-tests requires one selection and cannot be combined "
                "with --test/--exclude-test",
                file=sys.stderr,
            )
            return 2
        return list_tests(lit, args.selection)

    if args.selection and args.test:
        print("use either positional selection or --test, not both", file=sys.stderr)
        return 2
    exact_selections = bool(args.test)
    requested = unique(args.test or ([args.selection] if args.selection else []))
    if not requested:
        print("at least one test selection is required", file=sys.stderr)
        return 2
    excluded = set(unique(args.exclude_test))
    unknown_exclusions = sorted(excluded.difference(requested))
    if unknown_exclusions:
        print(
            "excluded tests are not in the selected set: "
            + ", ".join(unknown_exclusions),
            file=sys.stderr,
        )
        return 2
    selections = [test for test in requested if test not in excluded]
    if not selections:
        print("all selected tests were excluded", file=sys.stderr)
        return 2
    for test_id in sorted(excluded):
        print(f"EXCLUDED {test_id}")

    try:
        process = subprocess.Popen(
            [str(lit), "-v", *selections],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        print(f"cannot execute llvm-lit: {exc}", file=sys.stderr)
        return 2

    assert process.stdout is not None
    observed: dict[str, str] = {}
    for line in process.stdout:
        print(line, end="", flush=True)
        match = RESULT_RE.match(line.rstrip())
        if not match:
            continue
        test_id = canonical_test_id(match.group("suite"), match.group("test"))
        observed[test_id] = match.group("status")
    returncode = process.wait()

    for test_id, status in sorted(observed.items()):
        if status in FAILED_STATUSES:
            print(f"FAILED {test_id}")
        elif status in {"PASS", "FLAKYPASS"}:
            print(f"PASSED {test_id}")
        elif status in SKIPPED_STATUSES:
            print(f"SKIPPED {test_id}")

    missing: list[str] = []
    for selection in selections:
        if not exact_selections and not is_single_test(selection):
            continue
        normalized = selection.replace("\\", "/")
        status = observed.get(normalized)
        if status is None:
            # Lit prints paths relative to the suite. Match conservatively by
            # suffix so LLVM :: Analysis/Foo.ll maps to llvm/test/Analysis/Foo.ll.
            matches = [
                value
                for test_id, value in observed.items()
                if normalized.endswith(test_id) or test_id.endswith(normalized)
            ]
            status = matches[0] if len(matches) == 1 else None
        if status is None:
            missing.append(normalized)
    if missing:
        print(
            "requested test outcome was not observed: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    return returncode


def list_tests(lit: Path, selection: str) -> int:
    try:
        completed = subprocess.run(
            [str(lit), "--show-tests", selection],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        print(f"cannot execute llvm-lit: {exc}", file=sys.stderr)
        return 2
    print(completed.stdout, end="")
    discovered: set[str] = set()
    for line in completed.stdout.splitlines():
        match = DISCOVERED_RE.match(line)
        if not match:
            continue
        discovered.add(canonical_test_id(match.group("suite"), match.group("test")))
    for test_id in sorted(discovered):
        print(f"DISCOVERED {test_id}")
    if completed.returncode != 0:
        return completed.returncode
    if not discovered:
        print(f"no tests discovered under selection: {selection}", file=sys.stderr)
        return 2
    return 0


def canonical_test_id(suite: str, test: str) -> str:
    relative = re.sub(
        r"\s+\([0-9]+ of [0-9]+\)\s*$", "", test
    ).strip().replace("\\", "/")
    suite_name = suite.strip().upper()
    if suite_name == "LLVM":
        return "llvm/test/" + remove_prefix(relative, "llvm/test/")
    if suite_name == "CLANG":
        return "clang/test/" + remove_prefix(relative, "clang/test/")
    return f"{suite.strip()}::{relative}"


def remove_prefix(value: str, prefix: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def unique(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        normalized = str(value).strip().replace("\\", "/")
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def is_single_test(selection: str) -> bool:
    return Path(selection).suffix.lower() in {
        ".ll", ".mir", ".s", ".c", ".cpp", ".test", ".yaml"
    }


if __name__ == "__main__":
    raise SystemExit(main())
