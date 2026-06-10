#!/usr/bin/env python3
"""Extract failed-test input/context records for php-src metadata.

This script is read-only with respect to builds: it consumes metadata produced
by build_meta_php.py and, when present, checked-out php-src trees under
out_tmp_dirs/php___php-src or /out/php___php-src. It does not run tests or
collect coverage again.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
BUGS_JSON = PROJECT_DIR / "bugs_list_new.json"


def _detect_path(host_subpath: str, container_path: str) -> Path:
    host_path = DEFECTS4C_ROOT / host_subpath
    if host_path.exists():
        return host_path
    in_container = Path(container_path)
    if in_container.exists() or in_container.parent.exists():
        return in_container
    return host_path


DEFAULT_METADATA_DIR = _detect_path(
    "out_tmp_dirs/unified_debugging/php/metadata",
    "/out/unified_debugging/php/metadata",
)
DEFAULT_OUT_FILE = DEFAULT_METADATA_DIR / "php_test_info.json"
DEFAULT_REPO_ROOT = _detect_path(f"out_tmp_dirs/{PROJECT_NAME}", f"/out/{PROJECT_NAME}")

FAIL_OUTCOMES = {"FAIL", "FAILED"}
PASS_OUTCOMES = {"PASS", "PASSED"}
PHP_CODE_SECTIONS = ("FILE", "FILEEOF")
EXPECTED_SECTIONS = ("EXPECT", "EXPECTF", "EXPECTREGEX")
INPUT_SECTIONS = ("INI", "ARGS", "ENV", "GET", "POST", "COOKIE", "HEADERS", "STDIN")
CONTEXT_SECTIONS = ("SKIPIF", "CLEAN")
PHP_CALL_EXCLUDES = {
    "array",
    "catch",
    "clone",
    "declare",
    "die",
    "echo",
    "empty",
    "eval",
    "exit",
    "for",
    "foreach",
    "function",
    "if",
    "include",
    "include_once",
    "isset",
    "list",
    "print",
    "require",
    "require_once",
    "return",
    "switch",
    "throw",
    "try",
    "while",
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract php-src failed-test input information.")
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--bugs-json", type=Path, default=BUGS_JSON)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT_FILE)
    parser.add_argument(
        "--include-fixed-fail",
        action="store_true",
        help="Include tests that fail on both buggy and fixed versions.",
    )
    parser.add_argument(
        "--update-metadata",
        action="store_true",
        help="Also write failed_test_info into each *_meta.json file.",
    )
    parser.add_argument(
        "--metadata-field",
        default="failed_test_info",
        help="Field name used with --update-metadata.",
    )
    parser.add_argument(
        "--max-asset-bytes",
        "--max-asset-chars",
        dest="max_asset_bytes",
        type=int,
        default=12000,
        help="Maximum bytes kept for each metadata-listed auxiliary test asset.",
    )
    args = parser.parse_args(argv)

    bug_index = load_bug_index(args.bugs_json)
    records = collect_test_info(
        metadata_dir=args.metadata_dir,
        repo_root=args.repo_root,
        bug_index=bug_index,
        include_fixed_fail=args.include_fixed_fail,
        max_asset_bytes=args.max_asset_bytes,
    )
    payload = {
        "project": PROJECT_NAME,
        "metadata_dir": str(args.metadata_dir),
        "repo_root": str(args.repo_root),
        "bugs_json": str(args.bugs_json),
        "include_fixed_fail": args.include_fixed_fail,
        "failed_test_count": len(records),
        "tests": records,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[php-test-info] wrote {len(records)} failed-test records to {args.output}")

    if args.update_metadata:
        updated = update_metadata_files(records, args.metadata_field)
        print(f"[php-test-info] updated {updated} metadata file(s) with {args.metadata_field}")

    return 0


def load_bug_index(path: Path) -> Dict[str, dict]:
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: Dict[str, dict] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        type_info = item.get("type") or {}
        keys = [
            item.get("output_bug_id"),
            type_info.get("id"),
            type_info.get("name"),
            item.get("commit_after"),
        ]
        for key in keys:
            if key:
                out[str(key)] = item
    return out


def collect_test_info(
    *,
    metadata_dir: Path,
    repo_root: Path,
    bug_index: Dict[str, dict],
    include_fixed_fail: bool,
    max_asset_bytes: int,
) -> List[dict]:
    records: List[dict] = []
    for meta_path in sorted(metadata_dir.glob("*_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            records.append(_error_record(meta_path, f"metadata_read_error: {exc}"))
            continue

        metadata_id = meta_path.name.removesuffix("_meta.json")
        bug = bug_index.get(str(meta.get("bug_id") or "")) or bug_index.get(metadata_id) or {}
        for test in meta.get("tests", []) or []:
            if not isinstance(test, dict) or not _is_fail(test.get("outcome")):
                continue
            if not include_fixed_fail and _is_fail(test.get("outcome_fixed")):
                continue
            records.append(_build_record(meta, metadata_id, meta_path, test, repo_root, bug, max_asset_bytes))
    return records


def update_metadata_files(records: List[dict], field_name: str) -> int:
    by_file: Dict[str, List[dict]] = {}
    for record in records:
        meta_file = record.get("metadata_file")
        if meta_file:
            by_file.setdefault(str(meta_file), []).append(record)

    updated = 0
    for meta_file, grouped in sorted(by_file.items()):
        path = Path(meta_file)
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta[field_name] = grouped
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        updated += 1
    return updated


def _build_record(
    meta: dict,
    metadata_id: str,
    meta_path: Path,
    test: dict,
    repo_root: Path,
    bug: dict,
    max_asset_bytes: int,
) -> dict:
    test_id = str(test.get("test_id") or "")
    test_relpath = _test_relpath(test_id)
    repo_dir = _repo_dir_for(meta, repo_root)
    test_file, failure_line = _test_file_for(test, repo_dir, test_relpath)
    parse_errors: List[str] = []

    phpt_source = ""
    sections: Dict[str, List[dict]] = {}
    if test_file and test_file.is_file():
        phpt_source = test_file.read_text(encoding="utf-8", errors="replace")
        sections = parse_phpt_sections(phpt_source)
    else:
        parse_errors.append(f"test_file_missing:{test_file or ''}")

    php_section = _first_section(sections, PHP_CODE_SECTIONS)
    expected_section_name, expected_section = _first_named_section(sections, EXPECTED_SECTIONS)
    title = _section_text(sections, "TEST").strip()
    skipif = _section_text(sections, "SKIPIF")
    clean = _section_text(sections, "CLEAN")
    php_code = php_section.get("content", "") if php_section else ""
    expected_output = expected_section.get("content", "") if expected_section else str(test.get("expected_output") or "")
    actual_output = str(test.get("actual_output") or "")
    failure_info = _extract_failure_info(actual_output)
    metadata_test_files = _metadata_test_files(bug)
    auxiliary_assets = _load_auxiliary_assets(repo_dir, test_relpath, metadata_test_files, max_asset_bytes)

    source_relpath = _safe_relpath(test_file, repo_dir) if test_file else test_relpath
    input_values = _input_values(sections)
    php_calls = _extract_php_calls(php_code)
    php_classes = _extract_php_new_classes(php_code)
    string_literals = _extract_php_string_literals(php_code)
    numeric_literals = _extract_numeric_literals(php_code)
    external_inputs = _external_input_summaries(auxiliary_assets, php_code, expected_output)
    failure_actual = _php_failure_actual(
        failure_info,
        actual_output,
        test_relpath=test_relpath,
    )
    assertions = _php_contract_assertions(
        expected_section_name=expected_section_name,
        expected_output=expected_output,
        php_calls=php_calls,
        external_inputs=external_inputs,
    )

    return {
        "project": PROJECT_NAME,
        "metadata_id": metadata_id,
        "metadata_file": str(meta_path),
        "bug_id": meta.get("bug_id", metadata_id),
        "type_name": meta.get("cve", meta.get("type_name", "")) or "",
        "commit_before": meta.get("commit_before", ""),
        "commit_after": meta.get("commit_after", ""),
        "source_file": meta.get("source_file", ""),
        "source_basename": meta.get("source_basename", ""),
        "ground_truth": meta.get("ground_truth", []) or [],
        "ground_truth_functions": meta.get("ground_truth_functions", []) or [],
        "test_id": test_id,
        "test_relpath": test_relpath,
        "test_file": source_relpath,
        "test_file_abs": str(test_file) if test_file else "",
        "failure_line": failure_line,
        "test_case_name": title or test_id,
        "test_case_line_start": php_section.get("line_start") if php_section else None,
        "test_case_line_end": php_section.get("line_end") if php_section else None,
        "test_case_source": php_code,
        "test_case_source_found": bool(php_code),
        "phpt_sections": _section_index(sections),
        "test_title": title,
        "php_code": php_code,
        "skipif_source": skipif,
        "clean_source": clean,
        "expected_section": expected_section_name,
        "expected_output": expected_output,
        "input_sections": input_values,
        "metadata_test_files": metadata_test_files,
        "auxiliary_test_assets": auxiliary_assets,
        "input_summary": {
            "test_relpath": test_relpath,
            "title": title,
            "section_names": list(sections.keys()),
            "ini_settings": _parse_ini_settings(input_values.get("INI", "")),
            "args": input_values.get("ARGS", ""),
            "env": input_values.get("ENV", ""),
            "get": input_values.get("GET", ""),
            "post": input_values.get("POST", ""),
            "cookie": input_values.get("COOKIE", ""),
            "headers": input_values.get("HEADERS", ""),
            "stdin": input_values.get("STDIN", ""),
            "php_functions_called": php_calls,
            "php_classes_instantiated": php_classes,
            "string_literals": string_literals,
            "numeric_literals": numeric_literals,
            "assertions": assertions,
            "external_inputs": external_inputs,
            "expected_output": expected_output,
            "failure_actual": failure_actual,
            "failure_expected": expected_output,
            "failure_summary": failure_info,
        },
        "outcome": test.get("outcome", ""),
        "outcome_fixed": test.get("outcome_fixed", ""),
        "is_actionable_regression": _is_fail(test.get("outcome")) and _is_pass(test.get("outcome_fixed")),
        "fail_reason": test.get("fail_reason", ""),
        "actual_output": actual_output,
        "covered_functions": test.get("covered_functions", []) or [],
        "covered_function_count": len(test.get("covered_functions", []) or []),
        "coverage_error": test.get("coverage_error", ""),
        "parse_errors": parse_errors,
    }


def _error_record(meta_path: Path, error: str) -> dict:
    return {
        "project": PROJECT_NAME,
        "metadata_id": meta_path.name.removesuffix("_meta.json"),
        "metadata_file": str(meta_path),
        "parse_errors": [error],
    }


def _is_fail(value: Any) -> bool:
    return str(value or "").strip().upper() in FAIL_OUTCOMES


def _is_pass(value: Any) -> bool:
    return str(value or "").strip().upper() in PASS_OUTCOMES


def _test_relpath(test_id: str) -> str:
    return test_id if test_id.endswith(".phpt") else f"{test_id}.phpt"


def _repo_dir_for(meta: dict, repo_root: Path) -> Path:
    joined = "\n".join(
        str(meta.get(key) or "")
        for key in ("source_file", "test_cmd_template", "compile_cmd")
    )
    match = re.search(r"/out/php___php-src/(git_repo_dir_[^/\s'\"]+)", joined)
    if match:
        return repo_root / match.group(1)
    bug_id = str(meta.get("bug_id") or "")
    if bug_id:
        return repo_root / f"git_repo_dir_{bug_id}"
    commit_after = str(meta.get("commit_after") or "")
    return repo_root / f"git_repo_dir_{commit_after}"


def _test_file_for(test: dict, repo_dir: Path, test_relpath: str) -> Tuple[Optional[Path], Optional[int]]:
    text = "\n".join(str(test.get(key) or "") for key in ("fail_reason", "actual_output"))
    match = re.search(r"(/out/php___php-src/git_repo_dir_[^]\s]+/[^]\n]+\.phpt)(?::(?P<line>\d+))?", text)
    if match:
        line = int(match.group("line")) if match.group("line") else None
        return _map_container_path(match.group(1)), line

    failed = _extract_failed_test_path(text)
    if failed:
        return repo_dir / failed, None
    return repo_dir / test_relpath, None


def _map_container_path(path: str) -> Path:
    prefix = "/out/php___php-src/"
    if path.startswith(prefix):
        return DEFAULT_REPO_ROOT / path[len(prefix):]
    return Path(path)


def _extract_failed_test_path(text: str) -> str:
    matches = re.findall(r"\[([^\]\n]+\.phpt)\]", text)
    if matches:
        return matches[-1]
    match = re.search(r"\b([A-Za-z0-9_./+-]+\.phpt)\b", text)
    return match.group(1) if match else ""


def _safe_relpath(path: Optional[Path], root: Path) -> str:
    if not path:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def parse_phpt_sections(source: str) -> Dict[str, List[dict]]:
    markers = list(re.finditer(r"(?m)^--([A-Z0-9_-]+)--[ \t]*\r?$", source))
    sections: Dict[str, List[dict]] = {}
    for idx, marker in enumerate(markers):
        name = marker.group(1).upper()
        content_start = marker.end()
        if content_start < len(source) and source[content_start] == "\n":
            content_start += 1
        content_end = markers[idx + 1].start() if idx + 1 < len(markers) else len(source)
        content = source[content_start:content_end]
        if content.endswith("\n"):
            content = content[:-1]
        item = {
            "name": name,
            "line_start": source.count("\n", 0, content_start) + 1,
            "line_end": source.count("\n", 0, content_end) + 1,
            "content": content,
        }
        sections.setdefault(name, []).append(item)
    return sections


def _first_section(sections: Dict[str, List[dict]], names: Tuple[str, ...]) -> Optional[dict]:
    for name in names:
        values = sections.get(name) or []
        if values:
            return values[0]
    return None


def _first_named_section(sections: Dict[str, List[dict]], names: Tuple[str, ...]) -> Tuple[str, Optional[dict]]:
    for name in names:
        values = sections.get(name) or []
        if values:
            return name, values[0]
    return "", None


def _section_text(sections: Dict[str, List[dict]], name: str) -> str:
    values = sections.get(name.upper()) or []
    return values[0].get("content", "") if values else ""


def _section_index(sections: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for name, values in sections.items():
        out[name] = [
            {
                "line_start": item.get("line_start"),
                "line_end": item.get("line_end"),
                "content_length": len(item.get("content", "")),
            }
            for item in values
        ]
    return out


def _input_values(sections: Dict[str, List[dict]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name in INPUT_SECTIONS + CONTEXT_SECTIONS:
        text = "\n".join(item.get("content", "") for item in sections.get(name, [])).strip()
        if text:
            out[name] = text
    return out


def _parse_ini_settings(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")) or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _metadata_test_files(bug: dict) -> List[str]:
    files = bug.get("files") if isinstance(bug, dict) else {}
    tests = files.get("test") if isinstance(files, dict) else []
    return [str(item) for item in tests if item]


def _load_auxiliary_assets(
    repo_dir: Path,
    test_relpath: str,
    metadata_test_files: List[str],
    max_bytes: int,
) -> List[dict]:
    assets: List[dict] = []
    seen = {test_relpath}
    for relpath in metadata_test_files:
        if relpath in seen or relpath.endswith(".phpt"):
            continue
        seen.add(relpath)
        path = repo_dir / relpath
        item = {
            "path": relpath,
            "path_abs": str(path),
            "exists": path.is_file(),
            "encoding": "",
            "content": "",
            "content_length": 0,
            "sha256": "",
        }
        if path.is_file():
            data = path.read_bytes()
            item["content_length"] = len(data)
            item["sha256"] = hashlib.sha256(data).hexdigest()
            keep = data if max_bytes <= 0 else data[:max_bytes]
            if _looks_text(keep):
                item["encoding"] = "text"
                item["content"] = keep.decode("utf-8", errors="replace")
            else:
                item["encoding"] = "base64"
                item["content"] = base64.b64encode(keep).decode("ascii")
            if max_bytes > 0 and len(data) > max_bytes:
                item["truncated"] = True
            else:
                item["truncated"] = False
        assets.append(item)
    return assets


def _looks_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _asset_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    kinds = {
        ".gd": "gd image",
        ".gif": "GIF image",
        ".jpg": "JPEG image",
        ".jpeg": "JPEG image",
        ".phar": "PHAR archive",
        ".png": "PNG image",
        ".tif": "TIFF image",
        ".tiff": "TIFF image",
    }
    return kinds.get(suffix, f"{suffix[1:]} file" if suffix else "file")


def _short_sha(value: Any) -> str:
    text = str(value or "")
    return text[:12] if len(text) > 12 else text


def _external_input_summaries(assets: List[dict], php_code: str, expected_output: str = "") -> List[str]:
    out = []
    for asset in assets:
        path = str(asset.get("path") or "")
        basename = Path(path).name
        referenced = bool(basename and basename in php_code)
        exists = bool(asset.get("exists"))
        encoding = str(asset.get("encoding") or "")
        size = asset.get("content_length", 0)
        kind = _asset_kind(path)
        truncated = bool(asset.get("truncated"))
        sha = _short_sha(asset.get("sha256"))
        parts = [path, kind]
        if exists:
            parts.append(f"{size} bytes")
        else:
            parts.append("missing")
        if sha:
            parts.append(f"sha256={sha}...")
        if encoding:
            parts.append(f"stored as {encoding}")
        semantic = _asset_semantic_summary(asset, expected_output)
        if semantic:
            parts.append(semantic)
        preview = _asset_content_preview(asset)
        if preview:
            parts.append(preview)
        if truncated:
            parts.append("content truncated in metadata")
        if referenced:
            parts.append("referenced by PHP test code")
        out.append("; ".join(parts))
    return out


def _asset_bytes(asset: dict, *, max_decode_bytes: int = 256) -> bytes:
    encoding = str(asset.get("encoding") or "")
    content = asset.get("content")
    if not content:
        return b""
    if encoding == "base64":
        try:
            return base64.b64decode(str(content), validate=False)[:max_decode_bytes]
        except (ValueError, TypeError):
            return b""
    if encoding == "text":
        return str(content).encode("utf-8", errors="replace")[:max_decode_bytes]
    return b""


def _asset_semantic_summary(asset: dict, expected_output: str = "") -> str:
    path = str(asset.get("path") or "")
    suffix = Path(path).suffix.lower()
    data = _asset_bytes(asset)
    size = int(asset.get("content_length") or 0)
    expected = str(expected_output or "")

    if suffix in {".tif", ".tiff"}:
        return _tiff_summary(data, size, expected)
    if suffix == ".gd":
        return _gd_summary(data, size, expected)
    if suffix == ".phar":
        return _phar_summary(data, size, expected)
    return ""


def _tiff_summary(data: bytes, size: int, expected_output: str) -> str:
    details = []
    endian = ""
    if len(data) >= 8 and data[:2] in {b"II", b"MM"}:
        endian = "little-endian" if data[:2] == b"II" else "big-endian"
        byteorder = "little" if data[:2] == b"II" else "big"
        magic = int.from_bytes(data[2:4], byteorder)
        first_ifd = int.from_bytes(data[4:8], byteorder)
        details.append(f"{endian} TIFF header, magic={magic}, first IFD offset=0x{first_ifd:x}")
        if first_ifd >= size:
            details.append(f"first IFD offset is beyond file size 0x{size:x}")
    elif data:
        details.append("does not start with a standard TIFF byte-order marker")

    match = re.search(
        r"filesize\(x(?P<size>[0-9a-fA-F]+)\)\s+less than start of IFD dir\(x(?P<ifd>[0-9a-fA-F]+)\)",
        expected_output,
    )
    if match:
        file_size = int(match.group("size"), 16)
        ifd = int(match.group("ifd"), 16)
        details.append(
            f"expected warning describes malformed TIFF: referenced IFD offset 0x{ifd:x} is beyond file size 0x{file_size:x}"
        )

    if not details and size:
        details.append(f"TIFF image input, {size} bytes")
    return "; ".join(details)


def _gd_summary(data: bytes, size: int, expected_output: str) -> str:
    details = []
    if data.startswith(b"gd2"):
        details.append("GD2 image header detected")
    elif data.startswith(b"gd"):
        details.append("GD image header detected")
    if size:
        details.append(f"GD image input size={size} bytes")
    if "INT_MAX" in expected_output or "not a valid GD2 file" in expected_output:
        details.append("expected warning indicates invalid GD2 input and/or allocation-size overflow handling")
    return "; ".join(details)


def _phar_summary(data: bytes, size: int, expected_output: str) -> str:
    details = []
    if size:
        details.append(f"PHAR archive input size={size} bytes")
    if b"__HALT_COMPILER" in data:
        details.append("PHAR stub marker __HALT_COMPILER is present in preview")
    if "crash" in expected_output.lower() or "corruption" in expected_output.lower():
        details.append("expected output references crash/corruption behavior for hostile archive input")
    return "; ".join(details)


def _asset_content_preview(asset: dict, *, max_bytes: int = 64) -> str:
    encoding = str(asset.get("encoding") or "")
    content = asset.get("content")
    if not content:
        return ""

    if encoding == "base64":
        try:
            data = base64.b64decode(str(content), validate=False)
        except (ValueError, TypeError):
            return ""
        if not data:
            return ""
        preview = data[:max_bytes].hex()
        shown = min(len(data), max_bytes)
        return f"first {shown} byte(s) hex={preview}"

    if encoding == "text":
        text = " ".join(str(content).split())
        if not text:
            return ""
        if len(text) > 160:
            text = text[:160].rstrip() + "..."
        return f"text preview={text!r}"

    return ""


def _expected_excerpt(text: str, max_chars: int = 500) -> str:
    compact = " ".join(str(text or "").strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "..."


def _php_contract_assertions(
    *,
    expected_section_name: str,
    expected_output: str,
    php_calls: List[str],
    external_inputs: List[str],
) -> List[str]:
    assertions = []
    if php_calls:
        assertions.append("PHPT --FILE-- executes PHP calls: " + ", ".join(php_calls[:12]))
    for item in external_inputs[:8]:
        assertions.append("PHPT external input: " + item)
    if expected_output:
        label = expected_section_name or "EXPECT"
        assertions.append(f"PHPT --{label}-- expected output: {_expected_excerpt(expected_output)}")
    return assertions


def _extract_php_calls(source: str) -> List[str]:
    cleaned = _strip_php_strings_and_comments(source)
    names = []
    for match in re.finditer(r"(?<!->)(?<!::)\b([A-Za-z_]\w*)\s*\(", cleaned):
        name = match.group(1)
        if name.lower() not in PHP_CALL_EXCLUDES:
            names.append(name)
    return sorted(dict.fromkeys(names))


def _extract_php_new_classes(source: str) -> List[str]:
    cleaned = _strip_php_strings_and_comments(source)
    names = re.findall(r"\bnew\s+([A-Za-z_][A-Za-z0-9_\\\\]*)\b", cleaned)
    return sorted(dict.fromkeys(names))


def _extract_php_string_literals(source: str) -> List[str]:
    strings = []
    pattern = re.compile(r"""(?sx)
        (?P<sq>'(?:\\.|[^'\\])*')
        |
        (?P<dq>"(?:\\.|[^"\\])*")
    """)
    for match in pattern.finditer(source):
        strings.append(match.group(0))
    return sorted(dict.fromkeys(strings))


def _extract_numeric_literals(source: str) -> List[str]:
    cleaned = _strip_php_strings_and_comments(source)
    nums = re.findall(r"(?<![A-Za-z_])(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)(?:[eE][+-]?\d+)?", cleaned)
    return sorted(dict.fromkeys(nums))


def _strip_php_strings_and_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"(?m)//.*$", " ", text)
    text = re.sub(r"(?m)#.*$", " ", text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', " ", text)
    return text


def _extract_failure_info(text: str) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    title = ""
    test_path = ""
    for match in re.finditer(r"^(Tests (?:failed|warned|borked|leaked|skipped|passed)|Expected fail)\s*:\s*(\d+)", text, re.MULTILINE | re.IGNORECASE):
        counts[match.group(1).lower()] = int(match.group(2))
    summary = re.search(r"FAILED TEST SUMMARY\s*-+\s*(?P<body>.*?)=+", text, re.DOTALL)
    if summary:
        line = next((ln.strip() for ln in summary.group("body").splitlines() if ln.strip()), "")
        title = re.sub(r"\s*\[[^\]]+\.phpt\]\s*$", "", line).strip()
        path_match = re.search(r"\[([^\]]+\.phpt)\]", line)
        if path_match:
            test_path = path_match.group(1)
    return {
        "counts": counts,
        "failed_test_title": title,
        "failed_test_path": test_path,
    }


def _php_failure_actual(
    failure_info: Dict[str, Any],
    actual_output: str,
    *,
    test_relpath: str,
) -> str:
    title = str(failure_info.get("failed_test_title") or "").strip()
    path = str(failure_info.get("failed_test_path") or "").strip()

    for line in actual_output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAIL ", "BORK ", "WARN ", "LEAK ")):
            return stripped
    if title and path:
        return f"FAIL {title} [{path}]"
    if title:
        return f"FAIL {title} [{test_relpath}]"
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
