#!/usr/bin/env python3
"""Configure or build the historical curl benchmark revision."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


CONFIGURE_FLAGS = (
    "--enable-debug",
    "--without-zlib",
    "--without-winssl",
    "--without-darwinssl",
    "--without-gnutls",
    "--without-polarssl",
    "--without-mbedtls",
    "--without-cyassl",
    "--without-nss",
    "--without-axtls",
    "--without-ca-bundle",
    "--without-ca-path",
    "--without-ca-fallback",
    "--without-libpsl",
    "--without-libmetalink",
    "--without-libssh2",
    "--without-librtmp",
    "--without-winidn",
    "--without-libidn2",
    "--without-nghttp2",
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
        print(f"cannot execute curl build command: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2
    root = Path.cwd()
    environment = build_environment()
    if args.action == "configure":
        if not (root / "configure").is_file():
            buildconf = root / "buildconf"
            if not buildconf.is_file():
                print("curl configure and buildconf are missing", file=sys.stderr)
                return 2
            returncode = run(["./buildconf"], environment)
            if returncode != 0:
                return returncode
        prefix = root / ".debugging-framework" / "install"
        return run(
            ["./configure", f"--prefix={prefix}", *CONFIGURE_FLAGS],
            environment,
        )

    returncode = run(["make", "-j", str(args.jobs)], environment)
    if returncode != 0:
        return returncode
    # The top-level `all` target does not descend into tests/.  runtests.pl
    # probes tests/server/sws and sockfilt even for many otherwise simple
    # cases, so validation must materialize the native test helpers as well.
    returncode = run(
        ["make", "-C", "tests", "-j", str(args.jobs), "all"], environment
    )
    if returncode != 0:
        return returncode
    required = (
        root / "src" / "curl",
        root / "tests" / "runtests.pl",
        root / "tests" / "data",
        root / "tests" / "server" / "sws",
        root / "tests" / "server" / "sockfilt",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    if missing:
        print("curl build/test artifacts are missing: " + ", ".join(missing), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
