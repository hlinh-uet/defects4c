#!/usr/bin/env python3
"""Configure or build old php-src revisions in the benchmark image."""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


DEFAULT_CONFIGURE_FLAGS = [
    "--enable-phpdbg",
    "--enable-fpm",
    "--without-pear",
    "--enable-sysvsem",
    "--enable-sysvshm",
    "--enable-shmop",
    "--enable-pcntl",
    "--enable-mbstring",
    "--enable-shared=Yes",
    "--enable-static=No",
]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("action", choices=("configure", "build"))
    result.add_argument("--jobs", type=int, default=4)
    result.add_argument("--build-flag", action="append", default=[])
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2
    env = build_environment()
    if args.action == "configure":
        if not compatible_bison(env):
            return 2
        patch_aarch64_inline_asm(Path.cwd())
        commands = [
            ["./buildconf", "--force"],
            ["./configure", "--quiet", *DEFAULT_CONFIGURE_FLAGS, *args.build_flag],
        ]
    else:
        commands = [["make", "-j", str(args.jobs)]]
    for command in commands:
        try:
            completed = subprocess.run(command, env=env, check=False)
        except OSError as exc:
            print(f"cannot execute PHP build command: {exc}", file=sys.stderr)
            return 2
        if completed.returncode != 0:
            return completed.returncode
    return 0


def build_environment() -> dict[str, str]:
    env = os.environ.copy()
    bison_bin = Path("/opt/bison-2.7/bin")
    if (bison_bin / "bison").is_file():
        env["PATH"] = f"{bison_bin}:{env.get('PATH', '')}"
    env.update(
        {
            "CC": "gcc",
            "CXX": "g++",
            "CFLAGS": "-Wno-error -g -O0",
            "CXXFLAGS": "-Wno-error -g -O0",
            "NO_INTERACTION": "1",
        }
    )
    return env


def compatible_bison(env: dict[str, str]) -> bool:
    try:
        completed = subprocess.run(
            ["bison", "--version"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"cannot inspect Bison 2.7: {exc}", file=sys.stderr)
        return False
    first_line = completed.stdout.splitlines()[0] if completed.stdout.splitlines() else ""
    if completed.returncode == 0 and re.search(r"\b2\.7(?:\D|$)", first_line):
        return True
    print(f"php-src benchmark requires Bison 2.7; found: {first_line!r}", file=sys.stderr)
    return False


def patch_aarch64_inline_asm(project_root: Path) -> None:
    if platform.machine().lower() not in {"aarch64", "arm64"}:
        return
    target = project_root / "Zend" / "zend_multiply.h"
    if not target.is_file():
        return
    text = target.read_text(encoding="utf-8", errors="replace")
    patched = re.sub(
        r'(:\s*)"=X"\(__tmpvar\),\s*"=X"\(usedval\)([^\n]*\\\n\s*:\s*)'
        r'"X"\(a\),\s*"X"\(b\)',
        r'\1"=r"(__tmpvar), "=r"(usedval)\2"r"(a), "r"(b)',
        text,
        count=1,
    )
    if patched != text:
        target.write_text(patched, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
