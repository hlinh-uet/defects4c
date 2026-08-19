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


adapter = load_module("_test_rocksdb_adapter", "run_rocksdb_tests.py")
runner = load_module("_test_rocksdb_runner", "run_debugging_case.py")


class AdapterTests(unittest.TestCase):
    def test_parse_gtest_list_supports_parameterized_and_ignores_disabled(self) -> None:
        output = """DBTest.
  OpensDatabase
Prefix/ParameterizedSuite.
  HandlesValue/0  # GetParam() = 1
DISABLED_Suite.
  Hidden
Prefix/DISABLED_Parameterized.
  Hidden/0
Prefix/DISABLED_Parameterized/0.
  AlsoHidden
Prefix/ParameterizedSuite.
  DISABLED_Hidden/1
"""
        self.assertEqual(
            adapter.parse_gtest_list(output),
            ["DBTest.OpensDatabase", "Prefix/ParameterizedSuite.HandlesValue/0"],
        )

    def test_zero_exit_without_execution_evidence_is_not_pass(self) -> None:
        with mock.patch.object(
            adapter, "find_test_binary", return_value=Path("/tmp/options_test")
        ), mock.patch.object(
            adapter,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="unexpected output\n"),
        ):
            outcome, output = adapter.run_one(
                Path("build"), "options_test::OptionsParserTest.DumpAndParse", 30
            )
        self.assertEqual(outcome, "failed")
        self.assertIn("Missing GoogleTest execution evidence", output)

    def test_running_zero_tests_is_skipped(self) -> None:
        with mock.patch.object(
            adapter, "find_test_binary", return_value=Path("/tmp/options_test")
        ), mock.patch.object(
            adapter,
            "run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="[==========] Running 0 tests from 0 test suites.\n",
            ),
        ):
            outcome, _ = adapter.run_one(
                Path("build"), "options_test::OptionsParserTest.Missing", 30
            )
        self.assertEqual(outcome, "skipped")

    def test_real_gtest_pass_requires_run_and_ok(self) -> None:
        output = """[==========] Running 1 test from 1 test suite.
[ RUN      ] OptionsParserTest.DumpAndParse
[       OK ] OptionsParserTest.DumpAndParse (1 ms)
[  PASSED  ] 1 test.
"""
        with mock.patch.object(
            adapter, "find_test_binary", return_value=Path("/tmp/options_test")
        ), mock.patch.object(
            adapter, "run", return_value=SimpleNamespace(returncode=0, stdout=output)
        ):
            outcome, _ = adapter.run_one(
                Path("build"), "options_test::OptionsParserTest.DumpAndParse", 30
            )
        self.assertEqual(outcome, "passed")

    def test_adapter_outcomes_preserve_skipped(self) -> None:
        self.assertEqual(
            runner.adapter_outcomes(
                "PASSED options_test::Suite.Pass\n"
                "FAILED options_test::Suite.Fail\n"
                "SKIPPED options_test::Suite.Skip\n"
            ),
            {
                "options_test::Suite.Pass": "passed",
                "options_test::Suite.Fail": "failed",
                "options_test::Suite.Skip": "skipped",
            },
        )


class SelectionAndOracleTests(unittest.TestCase):
    def test_dataset_maps_test_file_to_binary_and_declared_case(self) -> None:
        bug = runner.load_bugs()[0]
        self.assertEqual(runner.candidate_targets(bug), ["db_blob_compaction_test"])
        self.assertEqual(
            runner.candidate_tests(bug),
            [
                "db_blob_compaction_test::"
                "DBBlobCompactionTest.CompactionDoNotFillCache"
            ],
        )

    def test_regression_selection_prioritizes_target_suite_and_caps_at_70(self) -> None:
        failure = "options_test::OptionsParserTest.DumpAndParse"
        discovered = [failure]
        discovered.extend(f"options_test::Other.Case{index}" for index in range(100))
        discovered.extend(
            f"options_test::OptionsParserTest.Pass{index}" for index in range(5)
        )
        selected = runner.select_regression_tests(
            discovered, excluded_tests=[failure], seed="case"
        )
        self.assertEqual(len(selected), 70)
        self.assertNotIn(failure, selected)
        self.assertEqual(
            set(selected[:5]),
            {f"options_test::OptionsParserTest.Pass{index}" for index in range(5)},
        )

    def test_direct_fixed_target_outcome_is_authoritative(self) -> None:
        test_id = "options_test::OptionsParserTest.DumpAndParse"
        regression_id = "options_test::OptionsParserTest.Pass"
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)

            def materialize_snapshot(**kwargs: object) -> None:
                Path(kwargs["target"]).mkdir()

            with mock.patch.object(
                runner, "materialize_snapshot", side_effect=materialize_snapshot
            ), mock.patch.object(
                runner, "remove_git_metadata"
            ), mock.patch.object(
                runner, "run_commands_logged"
            ), mock.patch.object(
                runner,
                "run_in_image",
                return_value=SimpleNamespace(returncode=1, stdout=f"FAILED {test_id}\n"),
            ), mock.patch.object(
                runner, "observe_tests", return_value={regression_id: "passed"}
            ):
                direct, regression = runner.verify_fixed_oracle(
                    source_repo=staging / "source",
                    staging=staging,
                    bug={"commit_after": "a" * 40, "files": {"src": ["options/a.cc"]}},
                    runtime="docker",
                    image="sha256:test",
                    jobs=1,
                    targets=["options_test"],
                    target_tests=[test_id],
                    regression_tests=[regression_id],
                    timeout=30,
                    log_path=staging / "fixed.log",
                )
        self.assertEqual(direct, {test_id: "failed"})
        self.assertEqual(regression, {regression_id: "passed"})


class FrameworkContractTests(unittest.TestCase):
    def test_generated_config_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            selected = [
                "options_test::OptionsParserTest.Pass",
                "options_test::Other.Skip",
            ]
            runner.write_framework_config(
                path,
                failing_tests=["options_test::OptionsParserTest.DumpAndParse"],
                image="sha256:test",
                runtime="docker",
                jobs=2,
                targets=["options_test"],
                regression_tests=selected,
                excluded_regression_tests=[selected[1]],
            )
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(runner.framework_config_ready(path))
            self.assertEqual(config["schema_version"], 6)
            self.assertEqual(
                config["workspace"],
                {"disposable": True, "initialize_git_if_missing": True},
            )
            self.assertEqual(
                config["repair"]["failing_tests"],
                ["options_test::OptionsParserTest.DumpAndParse"],
            )

    def test_config_rejects_all_regression_tests_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            skipped = "options_test::Other.Skip"
            with self.assertRaisesRegex(ValueError, "must remain"):
                runner.write_framework_config(
                    path,
                    failing_tests=["options_test::OptionsParserTest.DumpAndParse"],
                    image="sha256:test",
                    runtime="docker",
                    jobs=2,
                    targets=["options_test"],
                    regression_tests=[skipped],
                    excluded_regression_tests=[skipped],
                )


if __name__ == "__main__":
    unittest.main()
