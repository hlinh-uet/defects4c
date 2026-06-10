#!/usr/bin/env python3
"""Extract failed-test input/context records for libyang metadata.

This script is read-only with respect to builds: it consumes metadata produced
by build_meta_libyang.py and checked-out source trees under out_tmp_dirs. It
does not run tests or collect coverage again.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name


def _detect_path(host_subpath: str, container_path: str) -> Path:
    host_path = DEFECTS4C_ROOT / host_subpath
    if host_path.exists():
        return host_path
    in_container = Path(container_path)
    if in_container.exists() or in_container.parent.exists():
        return in_container
    return host_path


DEFAULT_METADATA_DIR = _detect_path(
    "out_tmp_dirs/unified_debugging/libyang/metadata",
    "/out/unified_debugging/libyang/metadata",
)
DEFAULT_OUT_FILE = DEFAULT_METADATA_DIR / "libyang_test_info.json"
DEFAULT_REPO_ROOT = _detect_path(f"out_tmp_dirs/{PROJECT_NAME}", f"/out/{PROJECT_NAME}")

FAIL_OUTCOMES = {"FAIL", "FAILED"}
PASS_OUTCOMES = {"PASS", "PASSED"}

C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default",
    "do", "double", "else", "enum", "extern", "float", "for", "goto",
    "if", "inline", "int", "long", "register", "restrict", "return",
    "short", "signed", "sizeof", "static", "struct", "switch", "typedef",
    "union", "unsigned", "void", "volatile", "while",
}

COMMON_IDENTIFIERS = {
    "NULL", "true", "false", "state", "cmocka_run_group_tests",
    "cmocka_unit_test", "cmocka_unit_test_setup",
    "cmocka_unit_test_teardown", "cmocka_unit_test_setup_teardown", "UTEST",
    "UTEST_SETUP", "UTEST_TEARDOWN", "assert_int_equal",
    "assert_int_not_equal", "assert_string_equal", "assert_string_not_equal",
    "assert_ptr_equal", "assert_non_null", "assert_null", "fail",
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract libyang failed-test input information.")
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
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
        "--max-helper-chars",
        type=int,
        default=12000,
        help="Maximum source characters kept for each referenced helper/global.",
    )
    args = parser.parse_args(argv)

    records = collect_test_info(
        metadata_dir=args.metadata_dir,
        repo_root=args.repo_root,
        include_fixed_fail=args.include_fixed_fail,
        max_helper_chars=args.max_helper_chars,
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
    print(f"[libyang-test-info] wrote {len(records)} failed-test records to {args.output}")

    if args.update_metadata:
        updated = update_metadata_files(records, args.metadata_field)
        print(f"[libyang-test-info] updated {updated} metadata file(s) with {args.metadata_field}")

    return 0


def collect_test_info(
    *,
    metadata_dir: Path,
    repo_root: Path,
    include_fixed_fail: bool,
    max_helper_chars: int,
) -> List[dict]:
    records: List[dict] = []
    for meta_path in sorted(metadata_dir.glob("*_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            records.append(_error_record(meta_path, f"metadata_read_error: {exc}"))
            continue

        metadata_id = meta_path.name.removesuffix("_meta.json")
        for test in meta.get("tests", []) or []:
            if not isinstance(test, dict) or not _is_fail(test.get("outcome")):
                continue
            if not include_fixed_fail and _is_fail(test.get("outcome_fixed")):
                continue
            records.append(_build_record(meta, metadata_id, meta_path, test, repo_root, max_helper_chars))
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
    max_helper_chars: int,
) -> dict:
    test_id = str(test.get("test_id") or "")
    ctest_name, case_name = _split_test_id(test_id)
    repo_dir = _repo_dir_for(meta, repo_root)
    fail_text = str(test.get("fail_reason") or test.get("actual_output") or "")
    test_file, failure_line = _test_file_for(test, repo_dir, ctest_name, case_name)
    parse_errors: List[str] = []

    source_text = ""
    test_case = None
    helpers: List[dict] = []
    registrations: List[dict] = []
    if test_file and test_file.is_file():
        source_text = test_file.read_text(encoding="utf-8", errors="replace")
        test_case = find_c_test_function(source_text, case_name or ctest_name)
        registrations = extract_test_registrations(source_text, case_name or ctest_name)
        if test_case:
            registration_source = "\n".join(item.get("source", "") for item in registrations)
            helpers = extract_referenced_context(
                source_text,
                test_case,
                extra_source=registration_source,
                max_helper_chars=max_helper_chars,
            )
        else:
            parse_errors.append(f"test_case_not_found:{case_name or ctest_name}")
    else:
        parse_errors.append(f"test_file_missing:{test_file or ''}")

    failure_info = _extract_failure_info(fail_text)
    if failure_line is None:
        failure_line = failure_info.get("line")

    test_case_source = test_case["source"] if test_case else ""
    assertions = _extract_assertions(test_case_source)
    string_literals = _extract_string_literals(test_case_source)
    numeric_literals = _extract_numeric_literals(test_case_source)
    helper_names = [h["name"] for h in helpers]
    source_relpath = _safe_relpath(test_file, repo_dir) if test_file else ""

    return {
        "project": PROJECT_NAME,
        "metadata_id": metadata_id,
        "metadata_file": str(meta_path),
        "bug_id": meta.get("bug_id", metadata_id),
        "type_name": meta.get("type_name", ""),
        "commit_before": meta.get("commit_before", ""),
        "commit_after": meta.get("commit_after", ""),
        "source_file": meta.get("source_file", ""),
        "source_basename": meta.get("source_basename", ""),
        "ground_truth": meta.get("ground_truth", []) or [],
        "ground_truth_functions": meta.get("ground_truth_functions", []) or [],
        "test_id": test_id,
        "test_binary": ctest_name,
        "case_name": case_name,
        "test_file": source_relpath,
        "test_file_abs": str(test_file) if test_file else "",
        "failure_line": failure_line,
        "test_case_name": case_name or ctest_name,
        "test_case_line_start": test_case["line_start"] if test_case else None,
        "test_case_line_end": test_case["line_end"] if test_case else None,
        "test_case_source": test_case_source,
        "test_case_source_found": bool(test_case),
        "test_registration_source": registrations,
        "test_helpers_source": helpers,
        "input_summary": {
            "test_binary": ctest_name,
            "case_name": case_name,
            "assertions": assertions,
            "string_literals": string_literals,
            "numeric_literals": numeric_literals,
            "referenced_helpers": helper_names,
            "failure_error": failure_info.get("error", ""),
            "failure_actual": failure_info.get("actual", ""),
            "failure_expected": failure_info.get("expected", ""),
            "failure_log": failure_info.get("log", []),
        },
        "outcome": test.get("outcome", ""),
        "outcome_fixed": test.get("outcome_fixed", ""),
        "is_actionable_regression": _is_fail(test.get("outcome")) and _is_pass(test.get("outcome_fixed")),
        "fail_reason": test.get("fail_reason", ""),
        "actual_output": test.get("actual_output", ""),
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


def _split_test_id(test_id: str) -> Tuple[str, str]:
    if "::" not in test_id:
        return test_id, ""
    left, right = test_id.split("::", 1)
    return left, right


def _repo_dir_for(meta: dict, repo_root: Path) -> Path:
    source_file = str(meta.get("source_file") or "")
    match = re.search(r"/out/CESNET___libyang/(git_repo_dir_[^/]+)", source_file)
    if match:
        return repo_root / match.group(1)
    commit_after = str(meta.get("commit_after") or "")
    return repo_root / f"git_repo_dir_{commit_after}"


def _test_file_for(test: dict, repo_dir: Path, ctest_name: str, case_name: str) -> Tuple[Optional[Path], Optional[int]]:
    text = "\n".join(str(test.get(key) or "") for key in ("fail_reason", "actual_output"))
    match = re.search(r"(/out/CESNET___libyang/git_repo_dir_[^:\n]+/tests/[^:\n]+\.c):(?P<line>\d+):", text)
    if match:
        mapped = _map_container_path(match.group(1))
        return mapped, int(match.group("line"))

    inferred = _infer_test_file(repo_dir, ctest_name, case_name)
    return inferred, None


def _map_container_path(path: str) -> Path:
    prefix = "/out/CESNET___libyang/"
    if path.startswith(prefix):
        return DEFAULT_REPO_ROOT / path[len(prefix):]
    return Path(path)


def _infer_test_file(repo_dir: Path, ctest_name: str, case_name: str) -> Optional[Path]:
    tests_dir = repo_dir / "tests"
    if not tests_dir.is_dir():
        return tests_dir / _ctest_to_filename(ctest_name)

    candidates = list(tests_dir.rglob("*.c"))
    best_path: Optional[Path] = None
    best_score = -1
    expected_names = _expected_source_names(ctest_name)
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(repo_dir).as_posix()
        score = 0
        if path.name in expected_names:
            score += 40
        if f"/{_layout_hint(ctest_name)}/" in f"/{rel}/":
            score += 8
        if case_name and _file_mentions_case(text, case_name):
            score += 80
        if "cmocka_run_group_tests" in text:
            score += 5
        if score > best_score:
            best_score = score
            best_path = path

    if best_path:
        return best_path
    return tests_dir / _ctest_to_filename(ctest_name)


def _ctest_to_filename(ctest_name: str) -> str:
    base = ctest_name
    for prefix in ("src_", "utest_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return f"test_{base}.c"


def _expected_source_names(ctest_name: str) -> set:
    base = ctest_name
    names = {f"{base}.c", f"test_{base}.c"}
    for prefix in ("src_", "utest_", "test_"):
        if base.startswith(prefix):
            stripped = base[len(prefix):]
            names.update({f"{stripped}.c", f"test_{stripped}.c"})
    return names


def _layout_hint(ctest_name: str) -> str:
    if ctest_name.startswith("src_"):
        return "src"
    if ctest_name.startswith("utest_"):
        return "utests"
    return "tests"


def _file_mentions_case(source: str, case_name: str) -> bool:
    if not case_name:
        return False
    escaped = re.escape(case_name)
    registration = re.search(
        rf"\b(?:cmocka_unit_test(?:_setup(?:_teardown)?|_teardown)?|UTEST)\s*\(\s*{escaped}\b",
        source,
    )
    function = re.search(rf"(?m)^\s*(?:static\s+)?(?:void|int)\s*\n?\s*{escaped}\s*\(", source)
    return bool(registration or function)


def _safe_relpath(path: Optional[Path], root: Path) -> str:
    if not path:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def find_c_test_function(source: str, case_name: str) -> Optional[dict]:
    if not case_name:
        return None

    pattern = re.compile(
        rf"(?m)(?:^|\n)(?P<header>"
        rf"(?:[A-Za-z_][\w\s\*]*\n\s*){{0,3}}"
        rf"(?:static\s+)?[A-Za-z_][\w\s\*]*\s+"
        rf"{re.escape(case_name)}\s*\([^;{{}}]*\)\s*)\{{"
    )
    for match in pattern.finditer(source):
        open_brace = source.find("{", match.end("header"))
        if open_brace < 0:
            continue
        close_brace = _find_matching(source, open_brace, "{", "}")
        if close_brace < 0:
            continue
        start = match.start("header")
        while start > 0 and source[start - 1] in "\n\r":
            start -= 1
        end = close_brace + 1
        return {
            "kind": "function",
            "name": case_name,
            "source": source[start:end].strip(),
            "start": start,
            "end": end,
            "line_start": source.count("\n", 0, start) + 1,
            "line_end": source.count("\n", 0, end) + 1,
        }
    return None


def extract_test_registrations(source: str, case_name: str) -> List[dict]:
    if not case_name:
        return []
    out = []
    pattern = re.compile(
        rf"\b(?:cmocka_unit_test(?:_setup(?:_teardown)?|_teardown)?|UTEST)\s*\([^;\n]*\b{re.escape(case_name)}\b[^;\n]*\)",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        out.append({
            "line_start": source.count("\n", 0, match.start()) + 1,
            "line_end": source.count("\n", 0, match.end()) + 1,
            "source": match.group(0).strip(),
        })
    return out[:8]


def extract_referenced_context(
    source: str,
    test_case: dict,
    *,
    extra_source: str = "",
    max_helper_chars: int,
) -> List[dict]:
    body = test_case.get("source", "")
    used = _identifiers(body + "\n" + extra_source)
    prelude = source[: int(test_case["start"])]
    helpers: List[dict] = []

    for item in _prelude_definitions(prelude):
        name = item.get("name", "")
        if name and name in used:
            helpers.append(_truncate_source(item, max_helper_chars))

    seen = set()
    out = []
    for helper in helpers:
        key = (helper.get("kind"), helper.get("name"), helper.get("line_start"))
        if key in seen:
            continue
        seen.add(key)
        out.append(helper)
    return out[:20]


def _identifiers(text: str) -> set:
    out = set(re.findall(r"\b[A-Za-z_]\w*\b", _strip_strings_and_comments(text)))
    return {x for x in out if x not in C_KEYWORDS and x not in COMMON_IDENTIFIERS}


def _prelude_definitions(text: str) -> List[dict]:
    items: List[dict] = []
    items.extend(_macro_definitions(text))
    items.extend(_top_level_function_blocks(text))
    items.extend(_global_variable_definitions(text))
    return items


def _macro_definitions(text: str) -> List[dict]:
    out = []
    for match in re.finditer(r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)(?:\([^)]*\))?.*(?:\\\n.*)*", text):
        out.append({
            "kind": "macro",
            "name": match.group(1),
            "line_start": text.count("\n", 0, match.start()) + 1,
            "line_end": text.count("\n", 0, match.end()) + 1,
            "source": match.group(0).strip(),
        })
    return out


def _top_level_function_blocks(text: str) -> List[dict]:
    blocks: List[dict] = []
    for open_brace in _top_level_braces(text):
        close_brace = _find_matching(text, open_brace, "{", "}")
        if close_brace < 0:
            continue
        header_start = _header_start(text, open_brace)
        header = text[header_start:open_brace].strip()
        if "(" not in header or header.startswith(("if", "for", "while", "switch")):
            continue
        name = _function_name(header)
        if not name:
            continue
        end = close_brace + 1
        blocks.append({
            "kind": "function",
            "name": name,
            "line_start": text.count("\n", 0, header_start) + 1,
            "line_end": text.count("\n", 0, end) + 1,
            "source": text[header_start:end].strip(),
        })
    return blocks


def _global_variable_definitions(text: str) -> List[dict]:
    out = []
    pattern = re.compile(
        r"(?ms)^(?P<src>\s*(?:static\s+)?(?:const\s+)?(?:char|int|uint\d+_t|struct\s+\w+)"
        r"[\w\s\*\[\]]*\s+(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*=\s*.*?;)"
    )
    for match in pattern.finditer(text):
        src = match.group("src")
        if "\n\n" in src[: src.find("=")]:
            continue
        out.append({
            "kind": "global",
            "name": match.group("name"),
            "line_start": text.count("\n", 0, match.start("src")) + 1,
            "line_end": text.count("\n", 0, match.end("src")) + 1,
            "source": src.strip(),
        })
    return out


def _truncate_source(item: dict, max_chars: int) -> dict:
    source = item.get("source", "")
    if max_chars > 0 and len(source) > max_chars:
        item = dict(item)
        item["source"] = source[:max_chars] + "\n/* ... truncated by test_info.py ... */"
        item["truncated"] = True
    return item


def _top_level_braces(text: str) -> Iterable[int]:
    depth = 0
    in_string = ""
    escaped = False
    in_line_comment = False
    in_block_comment = False
    for idx, ch in enumerate(text):
        nxt = text[idx + 1] if idx + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = ""
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in {'"', "'"}:
            in_string = ch
            continue
        if ch == "{":
            if depth == 0:
                yield idx
            depth += 1
        elif ch == "}" and depth:
            depth -= 1


def _header_start(text: str, open_brace: int) -> int:
    start = max(text.rfind("\n\n", 0, open_brace), text.rfind(";", 0, open_brace), text.rfind("}", 0, open_brace))
    if start < 0:
        return 0
    return start + (2 if text[start:start + 2] == "\n\n" else 1)


def _function_name(header: str) -> str:
    compact = re.sub(r"\s+", " ", header).strip()
    before_paren = compact.rsplit("(", 1)[0].strip()
    match = re.search(r"([A-Za-z_]\w*)\s*$", before_paren)
    if not match:
        return ""
    name = match.group(1)
    return "" if name in C_KEYWORDS else name


def _find_matching(text: str, start: int, open_ch: str, close_ch: str) -> int:
    if start < 0 or start >= len(text) or text[start] != open_ch:
        return -1
    depth = 0
    in_string = ""
    escaped = False
    in_line_comment = False
    in_block_comment = False
    for idx in range(start, len(text)):
        ch = text[idx]
        nxt = text[idx + 1] if idx + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = ""
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in {'"', "'"}:
            in_string = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _extract_assertions(test_source: str) -> List[str]:
    assertions = []
    for line in test_source.splitlines():
        stripped = line.strip()
        if re.search(r"\b(assert_[A-Za-z_]+|fail|CHECK_[A-Za-z_]+|logbuf_assert)\s*\(", stripped):
            assertions.append(stripped)
    return assertions


def _extract_string_literals(test_source: str) -> List[str]:
    strings = []
    for match in re.finditer(r'(?:L|u8|u|U)?"(?:\\.|[^"\\])*"', test_source):
        strings.append(match.group(0))
    return sorted(dict.fromkeys(strings))


def _extract_numeric_literals(test_source: str) -> List[str]:
    cleaned = _strip_strings_and_comments(test_source)
    nums = re.findall(r"(?<![A-Za-z_])(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)(?:[uUlLfF]*)", cleaned)
    return sorted(dict.fromkeys(nums))


def _strip_strings_and_comments(text: str) -> str:
    text = re.sub(r"//.*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r'(?:L|u8|u|U)?"(?:\\.|[^"\\])*"', " ", text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", " ", text)
    return text


def _extract_failure_info(text: str) -> Dict[str, Any]:
    error = ""
    actual = ""
    expected = ""
    log_lines: List[str] = []
    failure_path = ""
    failure_line = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[  ERROR   ] ---"):
            error = stripped.split("---", 1)[1].strip()
            left, right = _split_cmocka_comparison(error)
            if left or right:
                actual = left
                expected = right
        elif stripped.startswith("[   LINE   ] ---"):
            loc = stripped.split("---", 1)[1].strip()
            match = re.search(r"(?P<path>[^:\n]+\.c):(?P<line>\d+):", loc)
            if match:
                failure_path = match.group("path")
                failure_line = int(match.group("line"))
        elif stripped and not stripped.startswith("[") and not stripped.endswith("FAILED TEST(S)"):
            log_lines.append(stripped)

    return {
        "error": error,
        "actual": actual,
        "expected": expected,
        "path": failure_path,
        "line": failure_line,
        "log": log_lines[:20],
    }


def _split_cmocka_comparison(expr: str) -> Tuple[str, str]:
    if " != " not in expr:
        return "", ""
    left, right = expr.split(" != ", 1)
    return left.strip(), right.strip()


if __name__ == "__main__":
    raise SystemExit(main())
