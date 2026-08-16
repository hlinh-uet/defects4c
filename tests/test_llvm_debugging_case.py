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


def test_schema_v6_contract_has_target_and_bounded_regression_set(tmp_path):
    runner = load_runner()
    path = tmp_path / "case.debugging-framework.json"
    regression_tests = [
        "llvm/test/Analysis/one.ll",
        "llvm/test/Transforms/two.ll",
        "llvm/test/CodeGen/three.ll",
    ]

    runner.write_framework_config(
        path,
        failing_tests=["llvm/test/Analysis/example.ll"],
        image="sha256:llvm-image",
        runtime="docker",
        jobs=3,
        build_type="Release",
        regression_tests=regression_tests,
        excluded_regression_tests=["llvm/test/Transforms/two.ll"],
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
    regression_command = config["regression_test"][0]["command"]
    assert regression_command.count("--test") == 3
    assert regression_command.count("--exclude-test") == 1
    assert "llvm/test" not in regression_command
    assert "{test_id}" not in " ".join(regression_command)
    assert runner.framework_config_ready(path)

    regression_command[:] = [
        "defects4c-llvm-lit", "--build-dir", ".debugging-framework/build", "llvm/test"
    ]
    path.write_text(json.dumps(config), encoding="utf-8")
    assert not runner.framework_config_ready(path)


def test_regression_selection_is_deterministic_bounded_and_excludes_targets():
    runner = load_runner()
    discovered = [f"llvm/test/Analysis/test-{index}.ll" for index in range(100)]
    declared = [discovered[3], discovered[17]]

    first = runner.select_regression_tests(
        discovered, excluded_tests=declared, seed="B__fixed", limit=70
    )
    second = runner.select_regression_tests(
        reversed(discovered), excluded_tests=declared, seed="B__fixed", limit=70
    )

    assert first == second
    assert len(first) == 70
    assert not set(first).intersection(declared)
    assert len(set(first)) == len(first)


def test_lit_adapter_outcomes_include_pass_fail_and_skip():
    runner = load_runner()
    output = """PASSED llvm/test/Analysis/pass.ll
FAILED llvm/test/Analysis/fail.ll
SKIPPED llvm/test/Analysis/unsupported.ll
"""

    assert runner.lit_adapter_outcomes(output) == {
        "llvm/test/Analysis/pass.ll": "passed",
        "llvm/test/Analysis/fail.ll": "failed",
        "llvm/test/Analysis/unsupported.ll": "skipped",
    }


def test_regression_failures_are_outcomes_not_prepare_errors(tmp_path, monkeypatch):
    runner = load_runner()
    tests = [
        "llvm/test/Analysis/pass.ll",
        "llvm/test/Analysis/fail.ll",
    ]
    output_text = """-- Testing: 2 tests, 1 workers --
PASS: LLVM :: Analysis/pass.ll (1 of 2)
FAIL: LLVM :: Analysis/fail.ll (2 of 2)
Testing Time: 0.01s
Expected Passes: 1
Unexpected Failures: 1
PASSED llvm/test/Analysis/pass.ll
FAILED llvm/test/Analysis/fail.ll
"""

    monkeypatch.setattr(
        runner,
        "run_in_image",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, output_text),
    )

    outcomes = runner.observe_regression_tests(
        runtime="docker",
        image="llvm:test",
        project_root=tmp_path,
        tests=tests,
        log_path=tmp_path / "fixed-regression.log",
        timeout=10,
    )

    assert outcomes == {
        "llvm/test/Analysis/pass.ll": "passed",
        "llvm/test/Analysis/fail.ll": "failed",
    }


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


def test_lit_adapter_normalizes_flakypass_as_pass(tmp_path, capsys):
    adapter = load_lit_adapter()
    runner = load_runner()
    build_dir = tmp_path / "build"
    lit = build_dir / "bin" / "llvm-lit"
    lit.parent.mkdir(parents=True)
    lit.write_text(
        "#!/usr/bin/env python3\n"
        "print('-- Testing: 1 tests, 1 workers --')\n"
        "print('FLAKYPASS: LLVM :: Analysis/Flaky.ll (1 of 1)')\n"
        "print('Testing Time: 0.01s')\n"
        "print('Passed With Retry: 1')\n",
        encoding="utf-8",
    )
    lit.chmod(0o755)
    test_id = "llvm/test/Analysis/Flaky.ll"

    returncode = adapter.main(["--build-dir", str(build_dir), test_id])

    output_text = capsys.readouterr().out
    assert returncode == 0
    assert f"PASSED {test_id}" in output_text
    assert runner.lit_adapter_outcomes(output_text) == {test_id: "passed"}
    assert runner.lit_test_passed(output_text, test_id)


def test_lit_adapter_runs_selected_tests_and_applies_exact_exclusions(tmp_path, capsys):
    adapter = load_lit_adapter()
    build_dir = tmp_path / "build"
    lit = build_dir / "bin" / "llvm-lit"
    lit.parent.mkdir(parents=True)
    lit.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('ARGV ' + ' '.join(sys.argv[1:]))\n"
        "print('-- Testing: 1 tests, 1 workers --')\n"
        "print('PASS: LLVM :: Analysis/Keep.ll (1 of 1)')\n"
        "print('Testing Time: 0.01s')\n"
        "print('Expected Passes: 1')\n",
        encoding="utf-8",
    )
    lit.chmod(0o755)
    keep = "llvm/test/Analysis/Keep.ll"
    excluded = "llvm/test/Analysis/Exclude.ll"

    returncode = adapter.main(
        [
            "--build-dir", str(build_dir),
            "--test", keep,
            "--test", excluded,
            "--exclude-test", excluded,
        ]
    )

    output_text = capsys.readouterr().out
    assert returncode == 0
    assert f"EXCLUDED {excluded}" in output_text
    assert f"PASSED {keep}" in output_text
    argv_line = next(line for line in output_text.splitlines() if line.startswith("ARGV "))
    assert keep in argv_line
    assert excluded not in argv_line


def test_lit_adapter_discovers_without_executing_tests(tmp_path, capsys):
    adapter = load_lit_adapter()
    build_dir = tmp_path / "build"
    lit = build_dir / "bin" / "llvm-lit"
    lit.parent.mkdir(parents=True)
    lit.write_text(
        "#!/usr/bin/env python3\n"
        "print('-- Available Tests --')\n"
        "print('  LLVM :: Analysis/One.ll')\n"
        "print('  LLVM :: Transforms/Two.ll')\n",
        encoding="utf-8",
    )
    lit.chmod(0o755)

    returncode = adapter.main(
        ["--build-dir", str(build_dir), "--list-tests", "llvm/test"]
    )

    output_text = capsys.readouterr().out
    assert returncode == 0
    assert "DISCOVERED llvm/test/Analysis/One.ll" in output_text
    assert "DISCOVERED llvm/test/Transforms/Two.ll" in output_text


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
