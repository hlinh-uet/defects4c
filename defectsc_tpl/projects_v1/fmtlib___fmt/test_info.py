#!/usr/bin/env python3
"""Extract failed-test input/context records for fmt metadata.

This script is intentionally read-only with respect to builds: it consumes the
metadata produced by build_meta_fmt.py and the existing checked-out test source
trees under out_tmp_dirs/fmtlib___fmt. It does not run tests or collect
coverage again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parent
DEFECTSC_TPL_DIR = PROJECT_DIR.parent.parent
DEFECTS4C_ROOT = DEFECTSC_TPL_DIR.parent
PROJECT_NAME = PROJECT_DIR.name
DEFAULT_METADATA_DIR = DEFECTS4C_ROOT / "out_tmp_dirs" / "unified_debugging" / "fmt" / "metadata"
DEFAULT_OUT_FILE = DEFAULT_METADATA_DIR / "fmt_test_info.json"
DEFAULT_REPO_ROOT = DEFECTS4C_ROOT / "out_tmp_dirs" / PROJECT_NAME

FAIL_OUTCOMES = {"FAIL", "FAILED"}
PASS_OUTCOMES = {"PASS", "PASSED"}
GTEST_MACROS = ("TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P")

CPP_KEYWORDS = {
    "alignas", "alignof", "and", "auto", "bool", "break", "case", "catch",
    "char", "class", "const", "constexpr", "continue", "decltype", "default",
    "delete", "do", "double", "else", "enum", "explicit", "false", "float",
    "for", "if", "inline", "int", "long", "namespace", "new", "noexcept",
    "nullptr", "operator", "or", "private", "protected", "public", "return",
    "short", "signed", "sizeof", "static", "struct", "switch", "template",
    "this", "throw", "true", "try", "typename", "unsigned", "using", "virtual",
    "void", "volatile", "while",
}

COMMON_IDENTIFIERS = {
    "EXPECT_EQ", "EXPECT_STREQ", "EXPECT_THROW", "EXPECT_THROW_MSG",
    "EXPECT_TRUE", "EXPECT_FALSE", "ASSERT_EQ", "ASSERT_TRUE", "ASSERT_FALSE",
    "fmt", "std", "testing",
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract fmt failed-test input information.")
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT_FILE)
    parser.add_argument(
        "--include-fixed-fail",
        action="store_true",
        help="Include tests that fail on both buggy and fixed versions.",
    )
    args = parser.parse_args(argv)

    records = collect_test_info(
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
    print(f"[fmt-test-info] wrote {len(records)} failed-test records to {args.output}")
    return 0


def collect_test_info(
    *,
    metadata_dir: Path,
    repo_root: Path,
    include_fixed_fail: bool,
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
            records.append(_build_record(meta, metadata_id, meta_path, test, repo_root))
    return records


def _build_record(meta: dict, metadata_id: str, meta_path: Path, test: dict, repo_root: Path) -> dict:
    test_id = str(test.get("test_id") or "")
    ctest_name, gtest_filter = _split_test_id(test_id)
    repo_dir = _repo_dir_for(meta, repo_root)
    test_file, failure_line = _test_file_for(meta, test, repo_dir, ctest_name)
    parse_errors: List[str] = []

    source_text = ""
    test_case = None
    helpers: List[dict] = []
    if test_file and test_file.is_file():
        source_text = test_file.read_text(encoding="utf-8", errors="replace")
        test_case = find_gtest_case(source_text, gtest_filter)
        if test_case:
            helpers = extract_referenced_helpers(source_text, test_case)
        else:
            parse_errors.append(f"test_case_not_found:{gtest_filter}")
    else:
        parse_errors.append(f"test_file_missing:{test_file or ''}")

    test_case_source = test_case["source"] if test_case else ""
    assertions = _extract_assertions(test_case_source)
    string_literals = _extract_string_literals(test_case_source)
    numeric_literals = _extract_numeric_literals(test_case_source)
    failure_values = _extract_failure_values(str(test.get("fail_reason") or test.get("actual_output") or ""))

    source_relpath = _safe_relpath(test_file, repo_dir) if test_file else ""
    helper_names = [h["name"] for h in helpers]

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
        "gtest_filter": gtest_filter,
        "test_file": source_relpath,
        "test_file_abs": str(test_file) if test_file else "",
        "failure_line": failure_line,
        "test_case_name": gtest_filter,
        "test_case_line_start": test_case["line_start"] if test_case else None,
        "test_case_line_end": test_case["line_end"] if test_case else None,
        "test_case_source": test_case_source,
        "test_case_source_found": bool(test_case),
        "test_helpers_source": helpers,
        "input_summary": {
            "test_binary": ctest_name,
            "gtest_filter": gtest_filter,
            "assertions": assertions,
            "string_literals": string_literals,
            "numeric_literals": numeric_literals,
            "referenced_helpers": helper_names,
            "failure_actual": failure_values.get("actual", ""),
            "failure_expected": failure_values.get("expected", ""),
            "failure_which_is": failure_values.get("which_is", []),
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
    match = re.search(r"/out/fmtlib___fmt/(git_repo_dir_[^/]+)", source_file)
    if match:
        return repo_root / match.group(1)
    commit_after = str(meta.get("commit_after") or "")
    return repo_root / f"git_repo_dir_{commit_after}"


def _test_file_for(meta: dict, test: dict, repo_dir: Path, ctest_name: str) -> Tuple[Optional[Path], Optional[int]]:
    text = "\n".join(str(test.get(key) or "") for key in ("fail_reason", "actual_output"))
    match = re.search(r"(/out/fmtlib___fmt/git_repo_dir_[^:\n]+/test/[^:\n]+):(?P<line>\d+):", text)
    if match:
        mapped = _map_container_path(match.group(1))
        line = int(match.group("line"))
        return mapped, line

    inferred = repo_dir / "test" / f"{ctest_name}.cc"
    if inferred.is_file():
        return inferred, None
    return inferred, None


def _map_container_path(path: str) -> Path:
    prefix = "/out/fmtlib___fmt/"
    if path.startswith(prefix):
        return DEFAULT_REPO_ROOT / path[len(prefix):]
    return Path(path)


def _safe_relpath(path: Optional[Path], root: Path) -> str:
    if not path:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def find_gtest_case(source: str, gtest_filter: str) -> Optional[dict]:
    if not gtest_filter:
        return None
    suite, case = _split_gtest_filter(gtest_filter)
    suite_base = suite.split("/", 1)[0]
    case_base = case.split("/", 1)[0]

    macro_re = re.compile(r"\b(" + "|".join(GTEST_MACROS) + r")\s*\(")
    for match in macro_re.finditer(source):
        open_paren = source.find("(", match.start())
        close_paren = _find_matching(source, open_paren, "(", ")")
        if close_paren < 0:
            continue
        args = _split_macro_args(source[open_paren + 1:close_paren])
        if len(args) < 2:
            continue
        source_suite = args[0].strip()
        source_case = args[1].strip()
        if source_suite != suite_base or source_case != case_base:
            continue
        open_brace = source.find("{", close_paren)
        if open_brace < 0:
            continue
        close_brace = _find_matching(source, open_brace, "{", "}")
        if close_brace < 0:
            continue
        start = match.start()
        end = close_brace + 1
        return {
            "macro": match.group(1),
            "source": source[start:end],
            "start": start,
            "end": end,
            "line_start": source.count("\n", 0, start) + 1,
            "line_end": source.count("\n", 0, end) + 1,
        }
    return None


def _split_gtest_filter(gtest_filter: str) -> Tuple[str, str]:
    if "." not in gtest_filter:
        return gtest_filter, ""
    return gtest_filter.rsplit(".", 1)


def _split_macro_args(text: str) -> List[str]:
    args: List[str] = []
    start = 0
    depth = 0
    for idx, ch in enumerate(text):
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>" and depth:
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[start:idx].strip())
            start = idx + 1
    args.append(text[start:].strip())
    return args


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


def extract_referenced_helpers(source: str, test_case: dict) -> List[dict]:
    body = test_case.get("source", "")
    used = _identifiers(body)
    prelude = source[: int(test_case["start"])]
    helpers = []

    for block in _top_level_blocks(prelude):
        name = block.get("name", "")
        if not name or name not in used:
            continue
        helpers.append(block)

    for alias in _using_aliases(prelude):
        if alias["name"] in used:
            helpers.append(alias)

    seen = set()
    out = []
    for helper in helpers:
        key = (helper.get("kind"), helper.get("name"), helper.get("line_start"))
        if key in seen:
            continue
        seen.add(key)
        out.append(helper)
    return out[:12]


def _identifiers(text: str) -> set:
    out = set(re.findall(r"\b[A-Za-z_]\w*\b", text))
    return {x for x in out if x not in CPP_KEYWORDS and x not in COMMON_IDENTIFIERS}


def _top_level_blocks(text: str) -> List[dict]:
    blocks: List[dict] = []
    for open_brace in _top_level_braces(text):
        close_brace = _find_matching(text, open_brace, "{", "}")
        if close_brace < 0:
            continue
        header_start = _header_start(text, open_brace)
        block_end = close_brace + 1
        while block_end < len(text) and text[block_end] in " \t\r\n;":
            block_end += 1
            if text[block_end - 1] == ";":
                break
        header = text[header_start:open_brace].strip()
        if "TEST" in header:
            continue
        kind, name = _definition_name(header)
        if not name:
            continue
        blocks.append({
            "kind": kind,
            "name": name,
            "line_start": text.count("\n", 0, header_start) + 1,
            "line_end": text.count("\n", 0, block_end) + 1,
            "source": text[header_start:block_end].strip(),
        })
    return blocks


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


def _definition_name(header: str) -> Tuple[str, str]:
    compact = re.sub(r"\s+", " ", header).strip()
    match = re.search(r"\b(struct|class|enum)\s+([A-Za-z_]\w*)\b", compact)
    if match:
        return match.group(1), match.group(2)
    before_paren = compact.rsplit("(", 1)[0].strip()
    if not before_paren:
        return "", ""
    match = re.search(r"([A-Za-z_]\w*)\s*$", before_paren)
    if match:
        name = match.group(1)
        if name not in CPP_KEYWORDS:
            return "function", name
    return "", ""


def _using_aliases(text: str) -> List[dict]:
    out: List[dict] = []
    for match in re.finditer(r"^\s*using\s+([^;\n]+);", text, re.MULTILINE):
        expr = match.group(1).strip()
        name = expr.rsplit("::", 1)[-1].split("=", 1)[0].strip()
        if not name:
            continue
        out.append({
            "kind": "using",
            "name": name,
            "line_start": text.count("\n", 0, match.start()) + 1,
            "line_end": text.count("\n", 0, match.end()) + 1,
            "source": match.group(0).strip(),
        })
    return out


def _extract_assertions(test_source: str) -> List[str]:
    assertions = []
    for line in test_source.splitlines():
        stripped = line.strip()
        if re.search(r"\b(EXPECT|ASSERT)_[A-Z_]+\s*\(", stripped):
            assertions.append(stripped)
    return assertions


def _extract_string_literals(test_source: str) -> List[str]:
    strings = []
    for match in re.finditer(r'(?:L|u8|u|U)?R?"(?:\\.|[^"\\])*"', test_source):
        strings.append(match.group(0))
    return sorted(dict.fromkeys(strings))


def _extract_numeric_literals(test_source: str) -> List[str]:
    cleaned = _strip_strings_and_comments(test_source)
    nums = re.findall(r"(?<![A-Za-z_])(?:0x[0-9A-Fa-f]+|\d+(?:\.\d+)?)(?:[uUlLfF]*)", cleaned)
    return sorted(dict.fromkeys(nums))


def _strip_strings_and_comments(text: str) -> str:
    text = re.sub(r"//.*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r'(?:L|u8|u|U)?R?"(?:\\.|[^"\\])*"', " ", text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", " ", text)
    return text


def _extract_failure_values(text: str) -> Dict[str, Any]:
    actual = ""
    expected = ""
    which_is = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Actual:"):
            actual = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Expected:"):
            expected = stripped.split(":", 1)[1].strip()
        elif "Which is:" in stripped:
            which_is.append(stripped.split("Which is:", 1)[1].strip())
    return {"actual": actual, "expected": expected, "which_is": which_is}


if __name__ == "__main__":
    raise SystemExit(main())
