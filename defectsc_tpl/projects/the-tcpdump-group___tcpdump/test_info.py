#!/usr/bin/env python3
"""Extract failed-test input/context records for tcpdump metadata.

This script is read-only with respect to builds: it consumes metadata produced
by build_meta_tcpdump.py and, when present, checked-out tcpdump trees under
out_tmp_dirs/the-tcpdump-group___tcpdump or /out/the-tcpdump-group___tcpdump.
It does not run tests or collect coverage again.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import struct
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
    "out_tmp_dirs/unified_debugging/tcpdump/metadata",
    "/out/unified_debugging/tcpdump/metadata",
)
DEFAULT_OUT_FILE = DEFAULT_METADATA_DIR / "tcpdump_test_info.json"
DEFAULT_REPO_ROOT = _detect_path(f"out_tmp_dirs/{PROJECT_NAME}", f"/out/{PROJECT_NAME}")

FAIL_OUTCOMES = {"FAIL", "FAILED"}
PASS_OUTCOMES = {"PASS", "PASSED"}
TESTLIST_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$")
PCAP_EXTENSIONS = {".pcap", ".cap"}
TEXT_EXTENSIONS = {".out", ".txt", ".log"}
LINKTYPE_NAMES = {
    0: "NULL",
    1: "EN10MB/Ethernet",
    6: "IEEE802_5/Token Ring",
    9: "PPP",
    12: "RAW",
    50: "PPP_HDLC",
    51: "PPP_ETHER",
    101: "RAW IPv4/IPv6",
    105: "IEEE802_11",
    113: "LINUX_SLL",
    127: "IEEE802_11_RADIO",
    228: "IPV4",
    229: "IPV6",
    276: "LINUX_SLL2",
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract tcpdump failed-test input information.")
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
        type=int,
        default=12000,
        help="Maximum bytes kept for each referenced test asset.",
    )
    parser.add_argument(
        "--max-expected-output-chars",
        type=int,
        default=4000,
        help="Maximum characters kept from large expected stdout fixtures.",
    )
    args = parser.parse_args(argv)

    bug_index = load_bug_index(args.bugs_json)
    records = collect_test_info(
        metadata_dir=args.metadata_dir,
        repo_root=args.repo_root,
        bug_index=bug_index,
        include_fixed_fail=args.include_fixed_fail,
        max_asset_bytes=args.max_asset_bytes,
        max_expected_output_chars=args.max_expected_output_chars,
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
    print(f"[tcpdump-test-info] wrote {len(records)} failed-test records to {args.output}")

    if args.update_metadata:
        updated = update_metadata_files(records, args.metadata_field)
        print(f"[tcpdump-test-info] updated {updated} metadata file(s) with {args.metadata_field}")

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
    max_expected_output_chars: int,
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
            records.append(
                _build_record(
                    meta,
                    metadata_id,
                    meta_path,
                    test,
                    repo_root,
                    bug,
                    max_asset_bytes,
                    max_expected_output_chars,
                )
            )
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
    max_expected_output_chars: int,
) -> dict:
    test_id = str(test.get("test_id") or "")
    repo_dir = _repo_dir_for(meta, repo_root)
    tests_dir = repo_dir / "tests"
    parse_errors: List[str] = []

    test_entry = _find_test_entry(tests_dir / "TESTLIST", test_id)
    if not test_entry:
        parse_errors.append(f"test_id_not_found_in_TESTLIST:{test_id}")
        test_entry = {
            "name": test_id,
            "input_file": "",
            "expected_file": "",
            "options": "",
            "line": "",
            "line_number": None,
        }

    input_relpath = _tests_relpath(test_entry.get("input_file", ""))
    expected_relpath = _tests_relpath(test_entry.get("expected_file", ""))
    input_path = repo_dir / input_relpath if input_relpath else None
    expected_path = repo_dir / expected_relpath if expected_relpath else None
    testlist_path = tests_dir / "TESTLIST"

    assets = _load_assets(repo_dir, [input_relpath, expected_relpath], max_asset_bytes)
    expected_output_raw = str(test.get("expected_output") or "")
    if not expected_output_raw and expected_path and expected_path.is_file():
        expected_output_raw = _read_text_file(expected_path)
    expected_output, expected_output_truncated = _clip_text_head_tail(
        expected_output_raw,
        max_expected_output_chars,
    )
    actual_output = str(test.get("actual_output") or "")
    fail_reason = str(test.get("fail_reason") or "")

    test_case_source = _test_case_source(test_entry, input_relpath, expected_relpath)
    input_summaries = _external_input_summaries(assets, expected_output)
    output_summary = _expected_output_summary(expected_output)
    failure_actual = _failure_actual(actual_output, fail_reason)
    assertions = _contract_assertions(test_entry, input_summaries, output_summary)
    string_literals = _string_literals(test_entry, input_relpath, expected_relpath)

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
        "test_binary": "tests/TESTonce",
        "test_file": _safe_relpath(testlist_path, repo_dir) if testlist_path.exists() else "",
        "test_file_abs": str(testlist_path) if testlist_path.exists() else "",
        "failure_line": test_entry.get("line_number"),
        "test_case_name": test_id,
        "test_case_line_start": test_entry.get("line_number"),
        "test_case_line_end": test_entry.get("line_number"),
        "test_case_source": test_case_source,
        "test_case_source_found": bool(test_entry.get("line")),
        "testlist_entry": test_entry,
        "input_file": input_relpath,
        "input_file_abs": str(input_path) if input_path else "",
        "expected_file": expected_relpath,
        "expected_file_abs": str(expected_path) if expected_path else "",
        "expected_output_length": len(expected_output_raw),
        "expected_output_truncated": expected_output_truncated,
        "tcpdump_options": test_entry.get("options", ""),
        "auxiliary_test_assets": assets,
        "input_summary": {
            "test_binary": "tests/TESTonce",
            "test_id": test_id,
            "testlist_entry": test_entry.get("line", ""),
            "input_file": input_relpath,
            "expected_file": expected_relpath,
            "options": test_entry.get("options", ""),
            "assertions": assertions,
            "string_literals": string_literals,
            "failure_actual": failure_actual,
            "failure_expected": expected_output,
            "expected_output": expected_output,
            "external_inputs": input_summaries,
        },
        "outcome": test.get("outcome", ""),
        "outcome_fixed": test.get("outcome_fixed", ""),
        "is_actionable_regression": _is_fail(test.get("outcome")) and _is_pass(test.get("outcome_fixed")),
        "fail_reason": test.get("fail_reason", ""),
        "actual_output": actual_output,
        "expected_output": expected_output,
        "covered_functions": test.get("covered_functions", []) or [],
        "covered_function_count": len(test.get("covered_functions", []) or []),
        "coverage_error": test.get("coverage_error", ""),
        "parse_errors": parse_errors,
    }


def _repo_dir_for(meta: dict, repo_root: Path) -> Path:
    source_file = str(meta.get("source_file") or "")
    marker = f"/{PROJECT_NAME}/"
    if marker in source_file:
        rel = source_file.split(marker, 1)[1].split("/", 1)[0]
        candidate = repo_root / rel
        if candidate.exists():
            return candidate
    commit_after = str(meta.get("commit_after") or "")
    if commit_after:
        candidate = repo_root / f"git_repo_dir_{commit_after}"
        if candidate.exists():
            return candidate
    return repo_root / f"git_repo_dir_{commit_after}"


def _find_test_entry(testlist: Path, test_id: str) -> Optional[dict]:
    if not testlist.is_file():
        return None
    try:
        lines = testlist.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = TESTLIST_RE.match(line)
        if not match:
            continue
        name, input_file, expected_file, options = (
            match.group(1),
            match.group(2),
            match.group(3),
            (match.group(4) or "").strip(),
        )
        if name == test_id:
            return {
                "name": name,
                "input_file": input_file,
                "expected_file": expected_file,
                "options": options,
                "line": line.strip(),
                "line_number": idx,
            }
    return None


def _test_case_source(entry: dict, input_relpath: str, expected_relpath: str) -> str:
    name = str(entry.get("name") or "")
    input_file = str(entry.get("input_file") or "")
    expected_file = str(entry.get("expected_file") or "")
    options = str(entry.get("options") or "")
    return "\n".join(
        [
            f"TESTLIST entry: {entry.get('line') or ''}",
            f"TESTonce command: ./TESTonce {name} {input_file} {expected_file} {options}".rstrip(),
            f"pcap input: {input_relpath}" if input_relpath else "pcap input: <not found>",
            f"expected output file: {expected_relpath}" if expected_relpath else "expected output file: <not found>",
        ]
    )


def _tests_relpath(name: str) -> str:
    if not name:
        return ""
    path = Path(name)
    if path.is_absolute():
        return name.lstrip("/")
    if name.startswith("tests/"):
        return name
    return f"tests/{name}"


def _load_assets(repo_dir: Path, relpaths: List[str], max_bytes: int) -> List[dict]:
    assets: List[dict] = []
    seen = set()
    for relpath in relpaths:
        if not relpath or relpath in seen:
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
            if _looks_text(data[: min(len(data), max(max_bytes, 1))]):
                item["encoding"] = "text"
                text = data.decode("utf-8", errors="replace")
                item["content"], item["truncated"] = _clip_text_head_tail(text, max_bytes)
            else:
                item["encoding"] = "base64"
                item["content"] = base64.b64encode(keep).decode("ascii")
                item["truncated"] = bool(max_bytes > 0 and len(data) > max_bytes)
        assets.append(item)
    return assets


def _external_input_summaries(assets: List[dict], expected_output: str) -> List[str]:
    out = []
    for asset in assets:
        path = str(asset.get("path") or "")
        suffix = Path(path).suffix.lower()
        exists = bool(asset.get("exists"))
        size = int(asset.get("content_length") or 0)
        sha = _short_sha(asset.get("sha256"))
        encoding = str(asset.get("encoding") or "")
        truncated = bool(asset.get("truncated"))
        kind = _asset_kind(path)
        parts = [path, kind]
        parts.append(f"{size} bytes" if exists else "missing")
        if sha:
            parts.append(f"sha256={sha}...")
        if encoding:
            parts.append(f"stored as {encoding}")
        if suffix in PCAP_EXTENSIONS:
            pcap = _pcap_summary(_asset_bytes(asset, max_decode_bytes=4096), size)
            if pcap:
                parts.append(pcap)
            preview = _asset_content_preview(asset, max_bytes=64)
            if preview:
                parts.append(preview)
        elif suffix in TEXT_EXTENSIONS:
            text_summary = _text_asset_summary(asset)
            if text_summary:
                parts.append(text_summary)
        if truncated:
            parts.append("content truncated in metadata")
        if suffix in PCAP_EXTENSIONS:
            parts.append("tcpdump packet input")
        elif suffix == ".out":
            parts.append("expected stdout fixture")
        out.append("; ".join(parts))
    if expected_output:
        out.append(_expected_output_summary(expected_output))
    return out


def _contract_assertions(entry: dict, input_summaries: List[str], output_summary: str) -> List[str]:
    assertions = [
        (
            "TCPDUMP TESTLIST runs TESTonce with "
            f"name={entry.get('name')}, input={entry.get('input_file')}, "
            f"expected={entry.get('expected_file')}, options={entry.get('options') or '<none>'}"
        )
    ]
    assertions.extend(f"TCPDUMP external input: {item}" for item in input_summaries)
    if output_summary:
        assertions.append(f"TCPDUMP expected output: {output_summary}")
    return assertions


def _string_literals(entry: dict, input_relpath: str, expected_relpath: str) -> List[str]:
    values = [
        str(entry.get("name") or ""),
        str(entry.get("input_file") or ""),
        str(entry.get("expected_file") or ""),
        str(entry.get("options") or ""),
        input_relpath,
        expected_relpath,
    ]
    return sorted(dict.fromkeys(f'"{value}"' for value in values if value))


def _failure_actual(actual_output: str, fail_reason: str) -> str:
    text = actual_output or fail_reason
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        if "TEST FAILED" in line or "AddressSanitizer" in line or "runtime error:" in line:
            return line
    for line in lines:
        if "FAILED" in line or "Segmentation fault" in line or "Aborted" in line:
            return line
    return lines[-1] if lines else ""


def _expected_output_summary(expected_output: str) -> str:
    text = str(expected_output or "").strip()
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    first = " ".join(lines[:3])
    if len(first) > 700:
        first = first[:700].rstrip() + "..."
    signals = []
    for keyword in (
        "truncated",
        "bad cksum",
        "WARNING",
        "length",
        "ethertype",
        "ICMP",
        "UDP",
        "TCP",
        "IPv6",
        "MPLS",
    ):
        if keyword.lower() in text.lower():
            signals.append(keyword)
    signal_text = f"; notable terms: {', '.join(dict.fromkeys(signals))}" if signals else ""
    return f"{len(lines)} non-empty line(s); first lines: {first}{signal_text}"


def _asset_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in PCAP_EXTENSIONS:
        return "pcap capture"
    if suffix == ".out":
        return "expected tcpdump output"
    return f"{suffix[1:]} file" if suffix else "file"


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


def _pcap_summary(data: bytes, size: int) -> str:
    if len(data) < 24:
        return "pcap header unavailable"
    magic = data[:4]
    formats = {
        b"\xd4\xc3\xb2\xa1": ("<", "microsecond-resolution"),
        b"\xa1\xb2\xc3\xd4": (">", "microsecond-resolution"),
        b"\x4d\x3c\xb2\xa1": ("<", "nanosecond-resolution"),
        b"\xa1\xb2\x3c\x4d": (">", "nanosecond-resolution"),
    }
    if magic not in formats:
        return f"unknown pcap magic=0x{magic.hex()}"
    endian, resolution = formats[magic]
    try:
        version_major, version_minor, _thiszone, _sigfigs, snaplen, network = struct.unpack(
            endian + "HHiiii", data[4:24]
        )
    except struct.error:
        return "pcap header parse failed"

    packets = []
    offset = 24
    complete_packets = 0
    while offset + 16 <= len(data) and len(packets) < 5:
        try:
            ts_sec, ts_frac, incl_len, orig_len = struct.unpack(endian + "IIII", data[offset:offset + 16])
        except struct.error:
            break
        offset += 16
        packet_data_available = max(0, min(incl_len, len(data) - offset))
        first_bytes = data[offset:offset + min(packet_data_available, 16)].hex()
        packets.append(
            f"pkt{len(packets) + 1}: ts={ts_sec}.{ts_frac}, incl_len={incl_len}, "
            f"orig_len={orig_len}, first_bytes={first_bytes}"
        )
        if packet_data_available < incl_len:
            break
        complete_packets += 1
        offset += incl_len

    packet_count_note = f"{complete_packets} complete packet(s) parsed from retained bytes"
    if offset < size:
        packet_count_note += "; file has more bytes beyond retained preview"
    linktype = LINKTYPE_NAMES.get(network, f"linktype {network}")
    details = [
        f"pcap {resolution}",
        f"version={version_major}.{version_minor}",
        f"snaplen={snaplen}",
        f"linktype={network} ({linktype})",
        packet_count_note,
    ]
    details.extend(packets)
    return "; ".join(details)


def _asset_content_preview(asset: dict, *, max_bytes: int = 64) -> str:
    data = _asset_bytes(asset, max_decode_bytes=max_bytes)
    if not data:
        return ""
    return f"first {len(data)} byte(s) hex={data.hex()}"


def _text_asset_summary(asset: dict) -> str:
    content = str(asset.get("content") or "")
    if not content:
        return ""
    lines = [ln for ln in content.splitlines() if ln.strip()]
    first = " ".join(lines[:3])
    if len(first) > 500:
        first = first[:500].rstrip() + "..."
    return f"{len(lines)} non-empty text line(s); first lines: {first}"


def _looks_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _clip_text_head_tail(text: str, max_chars: int) -> Tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    marker = "\n... [truncated middle] ...\n"
    budget = max(2, max_chars - len(marker))
    head_chars = max(1, budget // 2)
    tail_chars = max(1, budget - head_chars)
    omitted = len(text) - head_chars - tail_chars
    marker = f"\n... [truncated middle: {omitted} chars] ...\n"
    budget = max(2, max_chars - len(marker))
    head_chars = max(1, budget // 2)
    tail_chars = max(1, budget - head_chars)
    if head_chars + tail_chars >= len(text):
        return text, False
    omitted = len(text) - head_chars - tail_chars
    marker = f"\n... [truncated middle: {omitted} chars] ...\n"
    return text[:head_chars].rstrip() + marker + text[-tail_chars:].lstrip(), True


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _short_sha(value: Any) -> str:
    text = str(value or "")
    return text[:12] if len(text) > 12 else text


def _is_fail(value: Any) -> bool:
    return str(value or "").upper() in FAIL_OUTCOMES


def _is_pass(value: Any) -> bool:
    return str(value or "").upper() in PASS_OUTCOMES


def _error_record(meta_path: Path, error: str) -> dict:
    return {
        "project": PROJECT_NAME,
        "metadata_file": str(meta_path),
        "bug_id": meta_path.name.removesuffix("_meta.json"),
        "test_id": "",
        "test_case_source": "",
        "test_case_source_found": False,
        "input_summary": {
            "assertions": [],
            "string_literals": [],
            "failure_actual": error,
            "failure_expected": "",
        },
        "outcome": "ERROR",
        "outcome_fixed": "",
        "is_actionable_regression": False,
        "fail_reason": error,
        "actual_output": error,
        "expected_output": "",
        "covered_functions": [],
        "covered_function_count": 0,
        "coverage_error": "",
        "parse_errors": [error],
    }


if __name__ == "__main__":
    raise SystemExit(main())
