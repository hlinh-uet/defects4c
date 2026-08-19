from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
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


adapter = load_module("_test_arrow_adapter", "run_arrow_tests.py")
runner = load_module("_test_arrow_runner", "run_debugging_case.py")


class AdapterTests(unittest.TestCase):
    def test_parse_gtest_list_supports_parameterized_cases(self) -> None:
        output = """Take.
  RemoveNop
Prefix/ParameterizedSuite.
  HandlesValue/0  # GetParam() = 1
DISABLED_Suite.
  Hidden
"""
        self.assertEqual(
            adapter.parse_gtest_list(output),
            ["Take.RemoveNop", "Prefix/ParameterizedSuite.HandlesValue/0"],
        )

    def test_parse_failed_gtests_ignores_summary_lines(self) -> None:
        output = """1: [  FAILED  ] Take.RemoveNop
1: [  FAILED  ] Prefix/Suite.Case/0
1: [  FAILED  ] 2 tests, listed below:
"""
        self.assertEqual(
            adapter.parse_failed_gtests(output),
            ["Take.RemoveNop", "Prefix/Suite.Case/0"],
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
        entry = adapter.CTestEntry("arrow-compute-vector-test", ("test_opt",), "")
        with mock.patch.object(
            adapter,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="unexpected empty run\n"),
        ):
            outcomes, output = adapter.run_one(
                Path("build"),
                {entry.name: entry},
                "arrow-compute-vector-test::Take.Case",
                30,
            )
        self.assertEqual(
            outcomes, {"arrow-compute-vector-test::Take.Case": "failed"}
        )
        self.assertIn("Missing test execution evidence", output)

    def test_failed_ctest_target_expands_to_gtest_case_ids(self) -> None:
        entry = adapter.CTestEntry("arrow-compute-vector-test", ("test_opt",), "")
        output = (
            "1: [  FAILED  ] Take.First\n"
            "1: [  FAILED  ] Prefix/Take.Second/0\n"
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
                "arrow-compute-vector-test::Take.First": "failed",
                "arrow-compute-vector-test::Prefix/Take.Second/0": "failed",
            },
        )

    def test_ctest_not_run_is_skipped_even_with_nonzero_exit(self) -> None:
        entry = adapter.CTestEntry("arrow-compute-vector-test", ("test_opt",), "")
        with mock.patch.object(
            adapter,
            "run",
            return_value=SimpleNamespace(
                returncode=8,
                stdout="1/1 Test #1: arrow-compute-vector-test ...***Not Run\n",
            ),
        ):
            outcomes, _ = adapter.run_one(
                Path("build"), {entry.name: entry}, entry.name, 30
            )
        self.assertEqual(outcomes, {entry.name: "skipped"})

    def test_ctest_environment_keeps_arrow_test_data_offline(self) -> None:
        entry = adapter.CTestEntry(
            "arrow-array-test",
            ("array-test",),
            "",
            ("CUSTOM_ARROW_SETTING=enabled",),
        )
        environment = adapter.test_environment(
            entry, Path("/workspace/.debugging-framework/build")
        )

        self.assertEqual(environment["CUSTOM_ARROW_SETTING"], "enabled")
        self.assertEqual(environment["ARROW_TEST_DATA"], "/workspace/testing/data")
        self.assertEqual(
            environment["PARQUET_TEST_DATA"],
            "/workspace/cpp/submodules/parquet-testing/data",
        )


class BuildAndMetadataTests(unittest.TestCase):
    def test_declared_target_comes_from_unittest_name(self) -> None:
        bug = {"unittest": {"name": ["arrow-concatenate-test"]}}
        self.assertEqual(runner.candidate_targets(bug), ["arrow-concatenate-test"])

    def test_build_is_limited_to_declared_target(self) -> None:
        commands = runner.validation_build_commands(
            2, ["arrow-compute-vector-test"]
        )

        self.assertEqual(commands[0][4], "cpp")
        self.assertIn("-DARROW_DEPENDENCY_SOURCE=SYSTEM", commands[0])
        self.assertIn("-DARROW_DATASET=OFF", commands[0])
        self.assertEqual(
            commands[1],
            [
                "cmake",
                "--build",
                runner.BUILD_DIR,
                "--target",
                "arrow-compute-vector-test",
                "--parallel",
                "2",
            ],
        )

    def test_orc_and_dataset_features_are_target_specific(self) -> None:
        orc = runner.arrow_feature_flags(["arrow-orc-adapter-test"])
        dataset = runner.arrow_feature_flags(["arrow-dataset-partition-test"])

        self.assertIn("-DARROW_ORC=ON", orc)
        self.assertIn("-DORC_SOURCE=BUNDLED", orc)
        self.assertIn("-DARROW_DATASET=ON", dataset)
        self.assertIn("-DARROW_FILESYSTEM=ON", dataset)

    def test_submodule_sha_requires_a_gitlink(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="160000 commit " + "a" * 40 + "\ttesting\n",
            stderr="",
        )
        with mock.patch.object(runner.subprocess, "run", return_value=completed):
            self.assertEqual(
                runner.submodule_sha(Path("repo"), "b" * 40, "testing"),
                "a" * 40,
            )

    def test_dependency_archive_rejects_parent_links(self) -> None:
        member = tarfile.TarInfo("safe-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"

        with self.assertRaisesRegex(RuntimeError, "Unsafe link"):
            runner.validate_archive_member(member, "Arrow dependency")


class SelectionAndOracleTests(unittest.TestCase):
    def test_regression_selection_prioritizes_failing_suite_and_caps_at_70(self) -> None:
        failure = "arrow-compute-vector-test::Take.Regression"
        discovered = [failure]
        discovered.extend(
            f"arrow-compute-vector-test::Other.Case{index}" for index in range(100)
        )
        discovered.extend(
            f"arrow-compute-vector-test::Take.Pass{index}" for index in range(5)
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
                f"arrow-compute-vector-test::Take.Pass{index}" for index in range(5)
            },
        )

    def test_direct_fixed_target_outcome_is_authoritative(self) -> None:
        test_id = "arrow-compute-vector-test::Take.Regression"
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
                return_value={"arrow-compute-vector-test::Other.Pass": "passed"},
            ):
                direct, regression = runner.verify_fixed_oracle(
                    source_repo=staging / "source",
                    dependency_source=staging / "external",
                    staging=staging,
                    bug={"commit_after": "a" * 40, "files": {"src": ["source/a.cpp"]}},
                    runtime="docker",
                    image="sha256:test",
                    jobs=1,
                    targets=["arrow-compute-vector-test"],
                    target_tests=[test_id],
                    regression_tests=["arrow-compute-vector-test::Other.Pass"],
                    timeout=30,
                    log_path=staging / "fixed.log",
                )

        self.assertEqual(direct, {test_id: "failed"})
        self.assertEqual(
            regression, {"arrow-compute-vector-test::Other.Pass": "passed"}
        )


class FrameworkContractTests(unittest.TestCase):
    def test_generated_config_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            selected = [
                "arrow-compute-vector-test::Take.Pass",
                "arrow-compute-vector-test::Other.Skip",
            ]
            runner.write_framework_config(
                path,
                failing_tests=["arrow-compute-vector-test::Take.Regression"],
                image="sha256:test",
                runtime="docker",
                jobs=2,
                targets=["arrow-compute-vector-test"],
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
                ["arrow-compute-vector-test::Take.Regression"],
            )
            self.assertIn(".cc", config["repair"]["source_extensions"])

    def test_config_rejects_all_regression_tests_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "must remain"):
                runner.write_framework_config(
                    path,
                    failing_tests=["arrow-compute-vector-test::Take.Regression"],
                    image="sha256:test",
                    runtime="docker",
                    jobs=2,
                    targets=["arrow-compute-vector-test"],
                    regression_tests=["arrow-compute-vector-test::Other.Skip"],
                    excluded_regression_tests=["arrow-compute-vector-test::Other.Skip"],
                )


if __name__ == "__main__":
    unittest.main()
