from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = (
    ROOT
    / "defectsc_tpl"
    / "projects_v1"
    / "llvm___llvm-project"
    / "run_debugging_case.py"
)
LIT_ADAPTER_PATH = RUNNER_PATH.with_name("run_llvm_lit.py")


def load_runner():
    spec = importlib.util.spec_from_file_location("llvm_debugging_case", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_lit_adapter():
    spec = importlib.util.spec_from_file_location("llvm_lit_adapter", LIT_ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dataset_and_case_ids_are_available():
    runner = load_runner()
    bugs = runner.load_bugs()

    assert len(bugs) == 143
    latest = next(
        bug
        for bug in bugs
        if bug["commit_after"] == "ab3fdbdfbe7edc62049c602d87be91c3ad3f5e3b"
    )
    assert runner.case_id_for(latest) == "B__ab3fdbdfbe7e"
    assert runner.candidate_tests(latest) == [
        "llvm/test/Transforms/InstCombine/zext-or-icmp.ll",
        "llvm/test/Analysis/ValueTracking/select-known-non-zero.ll",
    ]


def test_schema_v6_contract_has_target_and_full_suite(tmp_path):
    runner = load_runner()
    path = tmp_path / "case.debugging-framework.json"

    runner.write_framework_config(
        path,
        failing_tests=["llvm/test/Analysis/example.ll"],
        image="sha256:llvm-image",
        runtime="docker",
        jobs=3,
        build_type="Release",
    )

    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["schema_version"] == 6
    assert config["repair"]["failing_tests"] == ["llvm/test/Analysis/example.ll"]
    assert config["environment"] == {
        "mode": "image",
        "runtime": "docker",
        "image": "sha256:llvm-image",
    }
    assert config["build"][0][-2:] == ["--parallel", "3"]
    assert config["build"][0][4] == "llvm-test-depends"
    assert config["target_test"][0]["command"][-1] == "{test_id}"
    assert config["regression_test"][0]["command"][-1] == "llvm/test"
    assert "{test_id}" not in " ".join(config["regression_test"][0]["command"])
    assert runner.framework_config_ready(path)


def test_lit_output_requires_observed_test_and_real_pass_or_failure():
    runner = load_runner()
    test = "llvm/test/Analysis/ValueTracking/select-known-non-zero.ll"
    failing = """-- Testing: 1 tests, 1 workers --
FAIL: LLVM :: Analysis/ValueTracking/select-known-non-zero.ll (1 of 1)
Testing Time: 0.10s
  Unexpected Failures: 1
"""
    passing = """-- Testing: 1 tests, 1 workers --
PASS: LLVM :: Analysis/ValueTracking/select-known-non-zero.ll (1 of 1)
Testing Time: 0.08s
  Expected Passes: 1
"""
    zero = "-- Testing: 0 tests, 1 workers --\nTesting Time: 0.01s\n"

    assert runner.lit_test_observed(failing, test)
    assert not runner.lit_test_passed(failing, test)
    assert runner.lit_test_passed(passing, test)
    assert not runner.lit_suite_observed(zero)


def test_lit_adapter_emits_framework_test_ids(tmp_path, capsys):
    adapter = load_lit_adapter()
    build_dir = tmp_path / "build"
    lit = build_dir / "bin" / "llvm-lit"
    lit.parent.mkdir(parents=True)
    lit.write_text(
        "#!/usr/bin/env python3\n"
        "print('-- Testing: 1 tests, 1 workers --')\n"
        "print('FAIL: LLVM :: Analysis/Example.ll (1 of 1)')\n"
        "print('Testing Time: 0.01s')\n"
        "print('Unexpected Failures: 1')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    lit.chmod(0o755)

    returncode = adapter.main(
        ["--build-dir", str(build_dir), "llvm/test/Analysis/Example.ll"]
    )

    assert returncode == 1
    assert "FAILED llvm/test/Analysis/Example.ll" in capsys.readouterr().out


def test_materialize_snapshot_exports_full_fixed_tree_and_buggy_overlay(tmp_path):
    runner = load_runner()
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test"], repo)

    source = repo / "llvm" / "lib" / "Example.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("buggy\n", encoding="utf-8")
    (repo / "fixed-only.txt").write_text("before\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "buggy"], repo)
    before = output(["git", "rev-parse", "HEAD"], repo)

    source.write_text("fixed\n", encoding="utf-8")
    (repo / "fixed-only.txt").write_text("after\n", encoding="utf-8")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "fixed"], repo)
    after = output(["git", "rev-parse", "HEAD"], repo)

    target = tmp_path / "materialized"
    runner.materialize_snapshot(
        source_repo=repo,
        target=target,
        commit_after=after,
        commit_before=before,
        source_files=["llvm/lib/Example.cpp"],
    )

    assert (target / "llvm/lib/Example.cpp").read_text(encoding="utf-8") == "buggy\n"
    assert (target / "fixed-only.txt").read_text(encoding="utf-8") == "after\n"
    assert not (target / ".git").exists()


def run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def output(command: list[str], cwd: Path) -> str:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
