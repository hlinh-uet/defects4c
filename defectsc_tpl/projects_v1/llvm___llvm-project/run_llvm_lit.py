#!/usr/bin/env python3
"""Run one LLVM lit selection and emit Debugging-Framework test IDs."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


RESULT_RE = re.compile(
    r"^(?P<status>PASS|FAIL|XPASS|XFAIL|UNSUPPORTED):\s+"
    r"(?P<suite>[^:]+?)\s+::\s+(?P<test>.+)\s*$"
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--build-dir", required=True)
    result.add_argument("selection")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    build_dir = Path(args.build_dir)
    lit = build_dir / "bin" / "llvm-lit"
    if not lit.is_file():
        print(f"llvm-lit not found: {lit}", file=sys.stderr)
        return 2

    try:
        process = subprocess.Popen(
            [str(lit), "-v", args.selection],
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
        if status in {"FAIL", "XPASS"}:
            print(f"FAILED {test_id}")
        elif status == "PASS":
            print(f"PASSED {test_id}")

    if is_single_test(args.selection):
        requested = args.selection.replace("\\", "/")
        status = observed.get(requested)
        if status is None:
            # Lit prints paths relative to the suite. Match conservatively by
            # suffix so LLVM :: Analysis/Foo.ll maps to llvm/test/Analysis/Foo.ll.
            matches = [
                value
                for test_id, value in observed.items()
                if requested.endswith(test_id) or test_id.endswith(requested)
            ]
            status = matches[0] if len(matches) == 1 else None
        if status in {None, "UNSUPPORTED", "XFAIL"}:
            print(f"requested test outcome is not observable as PASS/FAIL: {requested}")
            return 2
    return returncode


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


def is_single_test(selection: str) -> bool:
    return Path(selection).suffix.lower() in {
        ".ll", ".mir", ".s", ".c", ".cpp", ".test", ".yaml"
    }


if __name__ == "__main__":
    raise SystemExit(main())
