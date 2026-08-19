#!/usr/bin/env python3
"""Configure or build historical tcpdump revisions in the benchmark image."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("action", choices=("configure", "build"))
    result.add_argument("--jobs", type=int, default=4)
    return result


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "CC": "gcc",
            "CXX": "g++",
            "CFLAGS": (
                "-g -O0 -Wno-error -fsanitize=address "
                "-fno-omit-frame-pointer"
            ),
            "CXXFLAGS": (
                "-g -O0 -Wno-error -fsanitize=address "
                "-fno-omit-frame-pointer"
            ),
            "LDFLAGS": "-fsanitize=address",
            "ASAN_OPTIONS": "detect_leaks=0",
        }
    )
    return env


def run(command: list[str], env: dict[str, str]) -> int:
    try:
        return subprocess.run(command, env=env, check=False).returncode
    except OSError as exc:
        print(f"cannot execute tcpdump build command: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2
    root = Path.cwd()
    env = build_environment()
    if args.action == "configure":
        if not (root / "configure").is_file():
            if not (root / "configure.ac").is_file():
                print("tcpdump configure and configure.ac are missing", file=sys.stderr)
                return 2
            returncode = run(["autoreconf", "-fi"], env)
            if returncode != 0:
                return returncode
        prefix = root / ".debugging-framework" / "install"
        return run(["./configure", f"--prefix={prefix}"], env)

    returncode = run(["make", "-j", str(args.jobs)], env)
    if returncode != 0:
        returncode = run(["make", "all", "-j", str(args.jobs)], env)
    if returncode != 0:
        return returncode
    required = (
        root / "tcpdump",
        root / "tests" / "TESTLIST",
        root / "tests" / "TESTonce",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        print("tcpdump build artifacts are missing: " + ", ".join(missing), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
