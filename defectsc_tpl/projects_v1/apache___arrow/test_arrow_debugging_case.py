from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
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
    def test_passing_test_output_is_retained(self) -> None:
        entry = adapter.CTestEntry("target", ("test",))
        output = io.StringIO()
        with mock.patch.object(adapter, "ctest_entries", return_value={"target": entry}), mock.patch.object(
            adapter, "run_one", return_value=({"target::Suite.Pass": "passed"}, "full passing output")
        ), redirect_stdout(output):
            status = adapter.main(["--build-dir", "build", "--test", "target::Suite.Pass"])
        self.assertEqual(status, 0)
        self.assertIn("full passing output", output.getvalue())
        self.assertIn("PASSED target::Suite.Pass", output.getvalue())

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

    def test_archive_allows_in_tree_relative_symlinks(self) -> None:
        for name, linkname in (
            ("python/cmake_modules", "../cpp/cmake_modules"),
            (
                "cpp/examples/parquet/parquet-arrow/cmake_modules/FindArrow.cmake",
                "../../../../cmake_modules/FindArrow.cmake",
            ),
        ):
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = linkname
            runner.validate_archive_member(member, "Arrow Git")

        escaping = tarfile.TarInfo("python/cmake_modules")
        escaping.type = tarfile.SYMTYPE
        escaping.linkname = "../../outside"
        with self.assertRaisesRegex(RuntimeError, "Unsafe link"):
            runner.validate_archive_member(escaping, "Arrow Git")


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

    def test_failure_log_only_keeps_failures_reproduced_in_isolation(self) -> None:
        target = "arrow-compute-vector-test"
        alone = f"{target}::Take.First"
        order_dependent = f"{target}::Take.Second"
        target_output = (
            "[ RUN      ] Other.Pass\n[       OK ] Other.Pass\n"
            f"FAILED {alone}\nFAILED {order_dependent}\n"
        )
        case_output = {
            alone: f"take.cc:42: Failure\nExpected: 1\n  Actual: 2\nFAILED {alone}\n",
            order_dependent: f"PASSED {order_dependent}\n",
        }

        def run_in_image(runtime, image, project_root, command, *, timeout):
            test = command[-1]
            stdout = target_output if test == target else case_output[test]
            return SimpleNamespace(returncode=1, stdout=stdout)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner, "run_in_image", side_effect=run_in_image
        ):
            failed, sections = runner.observe_buggy_targets(
                runtime="docker",
                image="sha256:test",
                project_root=Path(directory),
                targets=[target],
                log_path=Path(directory) / "targets.log",
                timeout=30,
            )

        self.assertEqual(failed, [alone])
        self.assertIn("take.cc:42: Failure", sections[alone])
        self.assertNotIn("Other.Pass", sections[alone])
        self.assertNotIn(order_dependent, sections)

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


class PrepareLifecycleTests(unittest.TestCase):
    """Exercise the real prepare/filter/publish flow with a simulated OCI runtime."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bug = {
            "type": {"id": "audit"},
            "commit_after": "a" * 40,
            "commit_before": "b" * 40,
            "files": {"src": ["source.cc"]},
            "unittest": {"name": ["arrow-audit-test"]},
        }
        self.target = "arrow-audit-test"
        self.failing = self.target + "::Suite.Fail"
        self.unfixed = self.target + "::Suite.Unfixed"
        self.order_dependent = self.target + "::Suite.OrderDependent"
        self.passing = self.target + "::Suite.Pass"
        self.skipped = self.target + "::Suite.Skip"
        self.build_error = False
        self.fixed_error = False
        self.reproduce = True
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(runner, "ensure_source_cache", side_effect=self.source_cache))
        stack.enter_context(mock.patch.object(runner, "materialize_snapshot", side_effect=self.materialize))
        stack.enter_context(mock.patch.object(runner, "provision_dependencies", side_effect=self.dependencies))
        stack.enter_context(mock.patch.object(runner, "run_in_image", side_effect=self.run_in_image))
        stack.enter_context(redirect_stdout(io.StringIO()))

    def source_cache(self, **kwargs):
        kwargs["log_path"].write_text("cached source\n")
        return self.root / "cache"

    def materialize(self, **kwargs):
        project = kwargs["target"]
        project.mkdir()
        fixed = kwargs["commit_before"] == kwargs["commit_after"]
        (project / "source.cc").write_text("fixed" if fixed else "buggy")

    def dependencies(self, **kwargs):
        for name in runner.REQUIRED_DEPENDENCIES:
            (kwargs["project_root"] / name).mkdir(parents=True, exist_ok=True)
        kwargs["log_path"].write_text("dependency sync output\n")
        return [{"path": name, "commit": "c" * 40} for name in runner.REQUIRED_DEPENDENCIES]

    def run_in_image(self, runtime, image, project, command, **kwargs):
        def result(status, output):
            if command[0] == runner.TEST_ADAPTER_COMMAND:
                output += "\n===== artifact: target.txt.raw =====\nraw assertion details\n"
                output += "===== artifact: target.xml =====\n<testsuite/>\n"
            return subprocess.CompletedProcess(command, status, output)

        fixed = (project / "source.cc").read_text() == "fixed"
        if command[0] == "cmake":
            (project / "generated-artifact").write_text("must not be published")
            (project / runner.BUILD_DIR).mkdir(parents=True, exist_ok=True)
            if self.build_error:
                return result(2, "compiler error details\n")
            return result(0, "full compiler output\n")
        snapshots = project / runner.BUILD_DIR / runner.TEST_ARTIFACT_DIRECTORY
        snapshots.mkdir(parents=True, exist_ok=True)
        snapshot = Path(tempfile.mkdtemp(prefix="test-", dir=snapshots))
        for suffix in (".txt.raw", ".txt", ".xml"):
            (snapshot / ("target" + suffix)).write_text("artifact contents\n")
        if "--expand-target-failures" in command:
            return result(1, "".join(f"FAILED {test}\n" for test in (
                self.failing, self.unfixed, self.order_dependent
            )))
        if "--list-tests" in command:
            return result(0, "".join(f"DISCOVERED {test}\n" for test in (
                self.failing, self.unfixed, self.order_dependent, self.passing, self.skipped
            )))
        if "--test" in command:
            tests = runner.option_values(command, "--test")
            outcomes = {test: "SKIPPED" if test == self.skipped and not fixed else "PASSED" for test in tests}
            return result(int("SKIPPED" in outcomes.values()), "regression output\n" + "".join(
                f"{label} {test}\n" for test, label in outcomes.items()
            ))
        test = command[-1]
        if fixed and self.fixed_error:
            return result(2, f"PASSED {test}\ninfrastructure error\n")
        failed = self.reproduce and test in {self.failing, self.unfixed} and (not fixed or test == self.unfixed)
        return result(int(failed), "isolated assertion output\n" + f"{'FAILED' if failed else 'PASSED'} {test}\n")

    def prepare(self, *, force=False):
        return runner.prepare_case(
            bug=self.bug, project_meta={}, inputs_root=self.root / "inputs",
            cache_root=self.root / "cache", runtime="docker", image="sha256:test",
            jobs=1, command_timeout=30, force=force,
        )

    def reports(self):
        return sorted((self.root / "prepare-logs").glob("*/*/manifest.json"))

    def test_success_keeps_logs_manifest_config_and_clean_source(self) -> None:
        project, config_path, failure = self.prepare()
        report_path, = self.reports()
        manifest = json.loads(report_path.read_text())
        config = json.loads(config_path.read_text())
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["stage"], "complete")
        self.assertEqual(manifest["failing_tests"], [self.failing])
        self.assertEqual(manifest["outcomes"]["buggy-isolated"][self.order_dependent], "passed")
        self.assertIn(self.order_dependent, manifest["excluded_unreproduced_tests"])
        self.assertIn(self.unfixed, manifest["excluded_failing_tests"])
        self.assertEqual(manifest["excluded_regression_tests"][self.skipped]["buggy"], "skipped")
        self.assertEqual(manifest["outcomes"]["fixed-regression"][self.skipped], "passed")
        self.assertEqual(config["repair"]["failing_tests"], [self.failing])
        self.assertIn("raw assertion details", failure.read_text())
        self.assertIn("<testsuite/>", failure.read_text())
        regression = config["regression_test"][0]["command"]
        self.assertIn(self.skipped, runner.option_values(regression, "--exclude-test"))
        self.assertEqual(set(runner.option_values(regression, "--test")), set(manifest["selected_regression_tests"]))
        self.assertEqual((project / "source.cc").read_text(), "buggy")
        self.assertFalse((project / "generated-artifact").exists())
        self.assertFalse((project / ".debugging-framework").exists())
        self.assertTrue(runner.input_ready(project, config_path, failure))
        self.assertEqual((report_path.parent / config_path.name).read_bytes(), config_path.read_bytes())
        self.assertEqual((report_path.parent / failure.name).read_bytes(), failure.read_bytes())
        for name in ("source-cache.log", "dependency-sync.log", "final-dependency-sync.log", "buggy-build.log",
                     "buggy-target-tests.log", "test-discovery.log", "buggy-regression-tests.log", "fixed-verification.log"):
            self.assertTrue((report_path.parent / name).is_file(), name)
        self.assertIn("full compiler output", (report_path.parent / "fixed-verification.log").read_text())
        for phase in ("buggy-target-tests", "buggy-regression-tests", "fixed-verification"):
            for suffix in (".txt.raw", ".txt", ".xml"):
                self.assertTrue(list((report_path.parent / "test-artifacts" / phase).glob("*/target" + suffix)))
        self.assertEqual(list((self.root / "inputs").glob(".*.preparing-*")), [])

    def test_build_failure_retains_diagnostics_and_does_not_publish(self) -> None:
        self.build_error = True
        with self.assertRaisesRegex(RuntimeError, "Command failed") as raised:
            self.prepare()
        report_path, = self.reports()
        manifest = json.loads(report_path.read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["stage"], "buggy-build")
        log = report_path.parent / "buggy-build.log"
        self.assertIn(str(log), str(raised.exception))
        self.assertIn("compiler error details", log.read_text())
        self.assertIn("RuntimeError", (report_path.parent / "error.log").read_text())
        self.assertEqual(list((self.root / "inputs").iterdir()), [])

    def test_retries_keep_previous_attempt_logs(self) -> None:
        self.build_error = True
        with self.assertRaises(RuntimeError):
            self.prepare()
        failed_report, = self.reports()
        previous = failed_report.read_bytes()
        self.build_error = False
        self.prepare()
        self.assertEqual(len(self.reports()), 2)
        self.assertEqual(failed_report.read_bytes(), previous)
        self.prepare(force=True)
        self.assertEqual(len(self.reports()), 3)
        self.prepare()  # Reuse does not pretend to be a new verification run.
        self.assertEqual(len(self.reports()), 3)

    def test_no_isolated_failure_aborts_with_exclusion_evidence(self) -> None:
        self.reproduce = False
        with self.assertRaisesRegex(RuntimeError, "reproduced failure in isolation"):
            self.prepare()
        report_path, = self.reports()
        manifest = json.loads(report_path.read_text())
        self.assertEqual(manifest["reproduced_failing_tests"], [])
        self.assertEqual(len(manifest["excluded_unreproduced_tests"]), 3)
        self.assertEqual(list((self.root / "inputs").iterdir()), [])

    def test_fixed_infrastructure_error_cannot_be_accepted_as_pass(self) -> None:
        self.fixed_error = True
        with self.assertRaisesRegex(RuntimeError, "exited 2"):
            self.prepare()
        report_path, = self.reports()
        manifest = json.loads(report_path.read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["stage"], "fixed-verification")
        self.assertIn("infrastructure error", (report_path.parent / "fixed-verification.log").read_text())
        self.assertTrue(list((report_path.parent / "test-artifacts/fixed-verification").glob("*/target.xml")))
        self.assertEqual(list((self.root / "inputs").iterdir()), [])


class CommandLoggingTests(unittest.TestCase):
    def test_adapter_timeout_keeps_readable_partial_output(self) -> None:
        process = mock.MagicMock()
        process.pid = 12345
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["test"], 30, output=b"partial test output\n"),
            ("partial test output\n", None),
        ]
        process.__enter__.return_value = process
        with mock.patch.object(
            adapter.subprocess, "Popen", return_value=process,
        ), mock.patch.object(adapter.os, "killpg") as killpg:
            result = adapter.run(["test"], timeout=30)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "partial test output\n\nTIMEOUT\n")
        killpg.assert_called_once_with(process.pid, adapter.signal.SIGKILL)

    def test_cache_command_retains_stdout_stderr_and_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "source-cache.log"
            command = [sys.executable, "-c", "import sys; print('clone progress'); print('fetch error', file=sys.stderr); sys.exit(2)"]
            with self.assertRaisesRegex(RuntimeError, "see .*source-cache.log"):
                runner.run_checked(command, timeout=30, log_path=log)
            output = log.read_text()
            self.assertIn("clone progress\n", output)
            self.assertIn("fetch error\n", output)
            self.assertIn("returncode: 2", output)

    def test_timeout_retains_partial_output_in_command_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(["docker"], 30, output=b"partial compiler output\n"),
        ):
            log = Path(directory) / "build.log"
            with self.assertRaisesRegex(RuntimeError, "Command failed \\(124\\)"):
                runner.run_commands_logged(
                    runtime="docker", image="sha256:test", project_root=Path(directory),
                    commands=[["cmake", "--build", "build"]], log_path=log, timeout=30,
                )
            output = log.read_text()
            self.assertIn("partial compiler output\n", output)
            self.assertNotIn("b'partial", output)
            self.assertIn("TIMEOUT after 30s", output)
            self.assertIn("returncode: 124", output)


class TestArtifactTests(unittest.TestCase):
    def test_failed_container_recovery_keeps_timeout_and_reports_copy_errors(self) -> None:
        def execute(argv, **kwargs):
            if argv[1] == "run":
                raise subprocess.TimeoutExpired(argv, 30, output=b"partial stdout\n")
            return subprocess.CompletedProcess(argv, 1, "container unavailable")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            runner.subprocess, "run", side_effect=execute
        ) as run:
            result = runner.run_in_image(
                "docker", "sha256:test", Path(directory), [runner.TEST_ADAPTER_COMMAND, "target"], timeout=30
            )
        self.assertEqual(result.returncode, 124)
        self.assertIn("partial stdout", result.stdout)
        self.assertIn("Artifact recovery cp failed: container unavailable", result.stdout)
        self.assertEqual(run.call_args.args[0][1:3], ["rm", "--force"])

    def test_snapshots_keep_raw_text_xml_and_reject_stale_or_unrelated_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build"
            entry = adapter.CTestEntry("target", (
                "/source/run-test.sh", str(build), "test", str(build / "bin" / "actual-target.bin")
            ))
            paths = adapter.test_artifact_paths(build, entry)
            paths[0].parent.mkdir(parents=True)
            unrelated = paths[0].parent / "another-target.txt"
            unrelated.write_text("unrelated old failure")
            for path in paths:
                path.write_text("stale test output")

            def execute(command, **kwargs):
                self.assertTrue(all(not path.exists() for path in paths))
                case = next(value for value in command if value.startswith("--gtest_filter="))
                for path in paths:
                    path.write_text(f"{case}\nExpected: 1\nActual: 2\n")
                return subprocess.CompletedProcess(command, 124, "redirecting output\nTIMEOUT\n")

            with mock.patch.object(adapter, "run", side_effect=execute):
                for case in ("Suite.First", "Suite.Second"):
                    outcomes, output = adapter.run_one(build, {"target": entry}, f"target::{case}", 30)
                    self.assertEqual(outcomes, {f"target::{case}": "failed"})
                    self.assertIn("Expected: 1\nActual: 2", output)
                    self.assertNotIn("stale test output", output)
                    self.assertNotIn("unrelated old failure", output)
                    self.assertIn(f"===== artifact: target::{case}: actual-target.txt.raw =====", output)
                    for name in ("actual-target.txt", "actual-target.xml"):
                        self.assertNotIn(f"===== artifact: target::{case}: {name} =====", output)
            snapshots = list((build / adapter.ARTIFACT_DIRECTORY).iterdir())
            self.assertEqual(len(snapshots), 2)
            saved = {json.loads((path / "test.json").read_text())["test_id"]: path for path in snapshots}
            self.assertIn("Suite.First", (saved["target::Suite.First"] / "actual-target.txt.raw").read_text())
            self.assertNotIn("Suite.Second", (saved["target::Suite.First"] / "actual-target.txt.raw").read_text())
            self.assertEqual(unrelated.read_text(), "unrelated old failure")

    def test_real_wrapper_timeout_recovers_raw_log_and_stops_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "build"
            build.mkdir()
            sentinel = root / "orphan-wrote-output"
            script = root / "run-test.sh"
            script.write_text(
                "import pathlib, subprocess, sys, time\n"
                "logs = pathlib.Path(sys.argv[1]) / 'build/test-logs'\n"
                "logs.mkdir(parents=True, exist_ok=True)\n"
                "(logs / 'target.txt.raw').write_text('partial assertion: expected 1, actual 2\\n')\n"
                "(logs / 'target.xml').write_text('<testsuite/>')\n"
                "subprocess.Popen([sys.executable, '-c', "
                + repr(f"import time, pathlib; time.sleep(0.5); pathlib.Path({str(sentinel)!r}).write_text('orphan')")
                + "])\n"
                "print('redirecting output', flush=True)\n"
                "time.sleep(10)\n"
            )
            entry = adapter.CTestEntry("target", (sys.executable, str(script), str(build), "test", "target"))
            real_run = adapter.run

            def short_run(command, **kwargs):
                return real_run(command, **{**kwargs, "timeout": 0.2})

            with mock.patch.object(adapter, "run", side_effect=short_run):
                outcomes, output = adapter.run_one(build, {"target": entry}, "target::Suite.Timeout", 30)
            self.assertEqual(outcomes, {"target::Suite.Timeout": "failed"})
            self.assertIn("partial assertion: expected 1, actual 2", output)
            self.assertNotIn("<testsuite/>", output)
            snapshot = next((build / adapter.ARTIFACT_DIRECTORY).iterdir())
            self.assertEqual((snapshot / "target.xml").read_text(), "<testsuite/>")
            self.assertIn("TIMEOUT", output)
            time.sleep(0.6)
            self.assertFalse(sentinel.exists(), "wrapper children must stop before artifact snapshot")

    def test_artifact_content_does_not_change_passing_execution_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            entry = adapter.CTestEntry("target", ("run-test.sh", str(build), "test", "target"))

            def execute(command, **kwargs):
                path = adapter.test_artifact_paths(build, entry)[2]
                path.parent.mkdir(parents=True)
                path.write_text("[  FAILED  ] text inside XML diagnostic\n")
                return subprocess.CompletedProcess(command, 0, "[  PASSED  ] 1 test.\n")

            with mock.patch.object(adapter, "run", side_effect=execute):
                outcomes, output = adapter.run_one(build, {"target": entry}, "target::Suite.Pass", 30)
            self.assertEqual(outcomes, {"target::Suite.Pass": "passed"})
            self.assertNotIn("text inside XML diagnostic", output)
            snapshot = next((build / adapter.ARTIFACT_DIRECTORY).iterdir())
            self.assertIn("text inside XML diagnostic", (snapshot / "target.xml").read_text())

    def test_container_timeout_recovers_temporary_workspace_before_cleanup(self) -> None:
        for platform, workspace in (("darwin", "/tmp/debugging-framework-workspace"), ("linux", "/workspace")):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                command = [runner.TEST_ADAPTER_COMMAND, "--test", "target::Suite.Timeout"]
                actions = []

                def execute(argv, **kwargs):
                    actions.append(argv)
                    if argv[1] == "run":
                        raise subprocess.TimeoutExpired(argv, 30, output=b"redirecting output\n")
                    if argv[1] == "cp":
                        self.assertIn(workspace + "/" + runner.BUILD_DIR, argv[2])
                        destination = Path(argv[-1])
                        if "test-logs" in argv[2]:
                            (destination / "target.txt.raw").write_text("timeout assertion details\n")
                        else:
                            (destination / "previous.xml").write_text("<testsuite/>\n")
                    return subprocess.CompletedProcess(argv, 0, "")

                with mock.patch.object(runner.sys, "platform", platform), mock.patch.object(
                    runner.subprocess, "run", side_effect=execute
                ):
                    result = runner.run_test_with_artifacts(
                        "docker", "sha256:test", root, command, log_path=root / "target.log", timeout=30
                    )
                self.assertEqual(result.returncode, 124)
                self.assertIn("timeout assertion details", result.stdout)
                self.assertNotIn("<testsuite/>", result.stdout)
                self.assertEqual([argv[1] for argv in actions], ["run", "pause", "cp", "cp", "unpause", "rm"])
                saved = list((root / "test-artifacts/target").glob("*/live/target.txt.raw"))
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0].read_text(), "timeout assertion details\n")
                self.assertFalse((root / runner.BUILD_DIR / runner.TEST_ARTIFACT_DIRECTORY).exists())


if __name__ == "__main__":
    unittest.main()
