from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter = load_module("_test_spirv_adapter", "run_spirv_tests.py")
runner = load_module("_test_spirv_runner", "run_debugging_case.py")


class AdapterTests(unittest.TestCase):
    def test_parse_gtest_list_supports_parameterized_cases(self) -> None:
        output = """Optimizer.
  RemoveNop
Prefix/ParameterizedSuite.
  HandlesValue/0  # GetParam() = 1
DISABLED_Suite.
  Hidden
"""
        self.assertEqual(
            adapter.parse_gtest_list(output),
            ["Optimizer.RemoveNop", "Prefix/ParameterizedSuite.HandlesValue/0"],
        )

    def test_parse_failed_gtests_ignores_summary_lines(self) -> None:
        output = """1: [  FAILED  ] Optimizer.RemoveNop
1: [  FAILED  ] Prefix/Suite.Case/0
1: [  FAILED  ] 2 tests, listed below:
"""
        self.assertEqual(
            adapter.parse_failed_gtests(output),
            ["Optimizer.RemoveNop", "Prefix/Suite.Case/0"],
        )

    def test_adapter_outcomes_preserve_skipped(self) -> None:
        self.assertEqual(
            runner.adapter_outcomes(
                "PASSED target::Suite.Pass\n"
                "FAILED target::Suite.Fail\n"
                "SKIPPED target::Suite.Skip\n"
            ),
            {
                "target::Suite.Pass": "passed",
                "target::Suite.Fail": "failed",
                "target::Suite.Skip": "skipped",
            },
        )

    def test_zero_exit_without_test_evidence_is_not_a_pass(self) -> None:
        entry = adapter.CTestEntry("spirv-tools-test_opt", ("test_opt",), "")
        with mock.patch.object(
            adapter,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="unexpected empty run\n"),
        ):
            outcomes, output = adapter.run_one(
                Path("build"),
                {entry.name: entry},
                "spirv-tools-test_opt::Optimizer.Case",
                30,
            )
        self.assertEqual(
            outcomes, {"spirv-tools-test_opt::Optimizer.Case": "failed"}
        )
        self.assertIn("Missing test execution evidence", output)

    def test_failed_ctest_target_expands_to_gtest_case_ids(self) -> None:
        entry = adapter.CTestEntry("spirv-tools-test_opt", ("test_opt",), "")
        output = (
            "1: [  FAILED  ] Optimizer.First\n"
            "1: [  FAILED  ] Prefix/Optimizer.Second/0\n"
            "0% tests passed, 1 tests failed out of 1\n"
        )
        with mock.patch.object(
            adapter,
            "run",
            return_value=SimpleNamespace(returncode=8, stdout=output),
        ):
            outcomes, _ = adapter.run_one(
                Path("build"),
                {entry.name: entry},
                entry.name,
                30,
                expand_target_failures=True,
            )
        self.assertEqual(
            outcomes,
            {
                "spirv-tools-test_opt::Optimizer.First": "failed",
                "spirv-tools-test_opt::Prefix/Optimizer.Second/0": "failed",
            },
        )

    def test_ctest_not_run_is_skipped_even_with_nonzero_exit(self) -> None:
        entry = adapter.CTestEntry("spirv-tools-test_opt", ("test_opt",), "")
        with mock.patch.object(
            adapter,
            "run",
            return_value=SimpleNamespace(
                returncode=8,
                stdout="1/1 Test #1: spirv-tools-test_opt ...***Not Run\n",
            ),
        ):
            outcomes, _ = adapter.run_one(
                Path("build"), {entry.name: entry}, entry.name, 30
            )
        self.assertEqual(outcomes, {entry.name: "skipped"})


class SelectionAndOracleTests(unittest.TestCase):
    def test_regression_selection_prioritizes_failing_suite_and_caps_at_70(self) -> None:
        failure = "spirv-tools-test_opt::Optimizer.Regression"
        discovered = [failure]
        discovered.extend(
            f"spirv-tools-test_opt::Other.Case{index}" for index in range(100)
        )
        discovered.extend(
            f"spirv-tools-test_opt::Optimizer.Pass{index}" for index in range(5)
        )
        selected = runner.select_regression_tests(
            discovered,
            excluded_tests=[failure],
            seed="case",
        )
        self.assertEqual(len(selected), 70)
        self.assertNotIn(failure, selected)
        self.assertEqual(
            set(selected[:5]),
            {
                f"spirv-tools-test_opt::Optimizer.Pass{index}" for index in range(5)
            },
        )

    def test_direct_fixed_target_outcome_is_authoritative(self) -> None:
        test_id = "spirv-tools-test_opt::Optimizer.Regression"
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)

            def materialize_snapshot(**kwargs: object) -> None:
                Path(kwargs["target"]).mkdir()

            with mock.patch.object(
                runner, "materialize_snapshot", side_effect=materialize_snapshot
            ), mock.patch.object(
                runner, "copy_dependencies"
            ), mock.patch.object(
                runner, "remove_git_metadata"
            ), mock.patch.object(
                runner, "run_commands_logged"
            ), mock.patch.object(
                runner,
                "run_in_image",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout=f"FAILED {test_id}\n",
                ),
            ), mock.patch.object(
                runner,
                "observe_tests",
                return_value={"spirv-tools-test_opt::Other.Pass": "passed"},
            ):
                direct, regression = runner.verify_fixed_oracle(
                    source_repo=staging / "source",
                    dependency_source=staging / "external",
                    staging=staging,
                    bug={"commit_after": "a" * 40, "files": {"src": ["source/a.cpp"]}},
                    runtime="docker",
                    image="sha256:test",
                    jobs=1,
                    targets=["spirv-tools-test_opt"],
                    target_tests=[test_id],
                    regression_tests=["spirv-tools-test_opt::Other.Pass"],
                    timeout=30,
                    log_path=staging / "fixed.log",
                )

        self.assertEqual(direct, {test_id: "failed"})
        self.assertEqual(
            regression, {"spirv-tools-test_opt::Other.Pass": "passed"}
        )


class FrameworkContractTests(unittest.TestCase):
    def test_generated_config_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            selected = [
                "spirv-tools-test_opt::Optimizer.Pass",
                "spirv-tools-test_opt::Other.Skip",
            ]
            runner.write_framework_config(
                path,
                failing_tests=["spirv-tools-test_opt::Optimizer.Regression"],
                image="sha256:test",
                runtime="docker",
                jobs=2,
                targets=["spirv-tools-test_opt"],
                regression_tests=selected,
                excluded_regression_tests=[selected[1]],
            )
            config = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(runner.framework_config_ready(path))
            self.assertEqual(
                config["workspace"],
                {"disposable": True, "initialize_git_if_missing": True},
            )
            self.assertEqual(
                config["repair"]["failing_tests"],
                ["spirv-tools-test_opt::Optimizer.Regression"],
            )

    def test_config_rejects_all_regression_tests_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "must remain"):
                runner.write_framework_config(
                    path,
                    failing_tests=["spirv-tools-test_opt::Optimizer.Regression"],
                    image="sha256:test",
                    runtime="docker",
                    jobs=2,
                    targets=["spirv-tools-test_opt"],
                    regression_tests=["spirv-tools-test_opt::Other.Skip"],
                    excluded_regression_tests=["spirv-tools-test_opt::Other.Skip"],
                )


if __name__ == "__main__":
    unittest.main()
