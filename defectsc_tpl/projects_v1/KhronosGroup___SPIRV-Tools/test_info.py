#!/usr/bin/env python3
"""Extract failed GoogleTest context from SPIRV-Tools metadata.

The optimizer CTest executable aggregates many ``test/opt/*.cpp`` files, so a
CTest-name-to-filename convention is not sufficient.  This extractor first
uses a file:line path from the failure output, then the bug's declared test
files, and finally a bounded search below ``test/``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
DATA_FOLDER = "spirv-tools"


def _detect(host: Path, container: Path) -> Path:
    if host.exists():
        return host
    if container.exists() or container.parent.exists():
        return container
    return host


DEFAULT_METADATA_DIR = _detect(
    DEFECTS4C_ROOT / "out_tmp_dirs/unified_debugging" / DATA_FOLDER / "metadata",
    Path("/out/unified_debugging") / DATA_FOLDER / "metadata",
)
DEFAULT_REPO_ROOT = _detect(
    DEFECTS4C_ROOT / "out_tmp_dirs" / PROJECT_NAME,
    Path("/out") / PROJECT_NAME,
)
DEFAULT_OUT_FILE = DEFAULT_METADATA_DIR / f"{DATA_FOLDER}_test_info.json"


def _load_fmt_test_info():
    path = PROJECT_DIR.parent / "fmtlib___fmt" / "test_info.py"
    spec = importlib.util.spec_from_file_location("_spirv_fmt_test_info_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load shared test-info extractor: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = _load_fmt_test_info()
base.PROJECT_NAME = PROJECT_NAME
base.DEFAULT_METADATA_DIR = DEFAULT_METADATA_DIR
base.DEFAULT_REPO_ROOT = DEFAULT_REPO_ROOT
_original_find_gtest_case = base.find_gtest_case


def find_gtest_case(source: str, gtest_filter: str):
    """Handle both ordinary and instantiated GoogleTest identifiers."""
    if not gtest_filter or "." not in gtest_filter:
        return _original_find_gtest_case(source, gtest_filter)
    suite, case = gtest_filter.rsplit(".", 1)
    normalized_case = case.split("/", 1)[0]
    parts = [part for part in suite.split("/") if part]
    candidates = [suite]
    if parts:
        # Prefix/Suite.Case/0 (value-parameterized tests).
        candidates.append(parts[-1])
        # Suite/0.Case (typed tests).
        if parts[-1].isdigit() and len(parts) > 1:
            candidates.append("/".join(parts[:-1]))
        candidates.append(parts[0])
    for candidate in dict.fromkeys(candidates):
        found = _original_find_gtest_case(source, f"{candidate}.{normalized_case}")
        if found:
            return found
    return None


base.find_gtest_case = find_gtest_case


def map_container_path(value: str) -> Path:
    prefix = f"/out/{PROJECT_NAME}/"
    if value.startswith(prefix):
        return DEFAULT_REPO_ROOT / value[len(prefix):]
    return Path(value)


def repo_dir_for(meta: dict, repo_root: Path) -> Path:
    source_file = str(meta.get("source_file") or "")
    pattern = rf"/out/{re.escape(PROJECT_NAME)}/(git_repo_dir_[^/]+)"
    match = re.search(pattern, source_file)
    if match:
        return repo_root / match.group(1)
    commit_after = str(meta.get("commit_after") or "")
    return repo_root / f"git_repo_dir_{commit_after}"


def _failure_path(test: dict) -> Tuple[Optional[Path], Optional[int]]:
    text = "\n".join(str(test.get(key) or "") for key in ("fail_reason", "actual_output"))
    patterns = (
        rf"(?P<path>/out/{re.escape(PROJECT_NAME)}/[^:\n]+/test/[^:\n]+\.(?:c|cc|cpp|cxx|h|hpp)):(?P<line>\d+)",
        r"(?P<path>(?:^|\s)test/[^:\n]+\.(?:c|cc|cpp|cxx|h|hpp)):(?P<line>\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            continue
        raw_path = match.group("path").strip()
        return map_container_path(raw_path), int(match.group("line"))
    return None, None


def _contains_case(path: Path, gtest_filter: str) -> bool:
    if not path.is_file() or not gtest_filter:
        return False
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return find_gtest_case(source, gtest_filter) is not None


def test_file_for(
    meta: dict,
    test: dict,
    repo_dir: Path,
    ctest_name: str,
) -> Tuple[Optional[Path], Optional[int]]:
    del ctest_name
    failure_path, failure_line = _failure_path(test)
    if failure_path:
        if not failure_path.is_absolute():
            failure_path = repo_dir / failure_path
        if failure_path.is_file():
            return failure_path, failure_line

    _, gtest_filter = base._split_test_id(str(test.get("test_id") or ""))
    declared = [str(item) for item in (meta.get("test_files") or []) if str(item)]
    for relpath in declared:
        candidate = repo_dir / relpath
        if _contains_case(candidate, gtest_filter):
            return candidate, failure_line

    test_root = repo_dir / "test"
    if test_root.is_dir() and gtest_filter:
        for suffix in ("*.cpp", "*.cc", "*.cxx"):
            for candidate in sorted(test_root.rglob(suffix)):
                if _contains_case(candidate, gtest_filter):
                    return candidate, failure_line

    if declared:
        return repo_dir / declared[0], failure_line
    return failure_path, failure_line


base._map_container_path = map_container_path
base._repo_dir_for = repo_dir_for
base._test_file_for = test_file_for


def update_metadata_files(records: List[dict], field_name: str) -> int:
    grouped: Dict[str, List[dict]] = {}
    for record in records:
        path = str(record.get("metadata_file") or "")
        if path:
            grouped.setdefault(path, []).append(record)
    updated = 0
    for filename, rows in sorted(grouped.items()):
        path = Path(filename)
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata[field_name] = rows
        path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        updated += 1
    return updated


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract SPIRV-Tools failed-test information.")
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT_FILE)
    parser.add_argument("--include-fixed-fail", action="store_true")
    parser.add_argument("--update-metadata", action="store_true")
    parser.add_argument("--metadata-field", default="failed_test_info")
    args = parser.parse_args(argv)

    records = base.collect_test_info(
        metadata_dir=args.metadata_dir,
        repo_root=args.repo_root,
        include_fixed_fail=args.include_fixed_fail,
    )
    payload = {
        "project": PROJECT_NAME,
        "metadata_dir": str(args.metadata_dir),
        "repo_root": str(args.repo_root),
        "include_fixed_fail": args.include_fixed_fail,
        "failed_test_count": len(records),
        "tests": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[spirv-test-info] wrote {len(records)} failed-test records to {args.output}")
    if args.update_metadata:
        count = update_metadata_files(records, args.metadata_field)
        print(f"[spirv-test-info] updated {count} metadata file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
