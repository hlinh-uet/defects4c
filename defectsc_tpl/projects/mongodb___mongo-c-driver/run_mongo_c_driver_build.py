#!/usr/bin/env python3
"""Configure or build the historical mongo-c-driver benchmark revision."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


BUILD_DIR = Path(".debugging-framework/build")
CONFIGURE_FLAGS = (
    "-DENABLE_TESTS=ON",
    "-DCMAKE_BUILD_TYPE=Debug",
    "-DENABLE_MAINTAINER_FLAGS=OFF",
    "-DENABLE_BSON=ON",
    "-DENABLE_MONGOC=ON",
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("action", choices=("configure", "build"))
    result.add_argument("--jobs", type=int, default=4)
    return result


def build_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CC": "gcc",
            "CXX": "g++",
            "CFLAGS": "-g -O0 -Wno-error",
            "CXXFLAGS": "-g -O0 -Wno-error",
        }
    )
    return environment


def run(command: list[str], environment: dict[str, str]) -> int:
    try:
        return subprocess.run(command, env=environment, check=False).returncode
    except OSError as exc:
        print(f"cannot execute mongo-c-driver build command: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2
    environment = build_environment()
    if args.action == "configure":
        return run(
            [
                "cmake",
                "-G",
                "Ninja",
                "-S",
                ".",
                "-B",
                BUILD_DIR.as_posix(),
                *CONFIGURE_FLAGS,
            ],
            environment,
        )

    returncode = run(
        [
            "cmake",
            "--build",
            BUILD_DIR.as_posix(),
            "--parallel",
            str(args.jobs),
        ],
        environment,
    )
    if returncode != 0:
        return returncode
    root = Path.cwd()
    required = (
        root / BUILD_DIR / "src" / "libbson" / "test-libbson",
        root / "src" / "libbson" / "tests" / "binary",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print(
            "mongo-c-driver build/test artifacts are missing: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
