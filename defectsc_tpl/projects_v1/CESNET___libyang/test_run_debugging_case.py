from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import run_debugging_case as runner


class OutcomeContractTests(unittest.TestCase):
    def test_nonpassing_fixed_outcomes_are_excluded(self) -> None:
        outcomes = {
            "passes": "passed",
            "fails": "failed",
            "is-skipped": "skipped",
            "has-error": "error",
        }

        self.assertEqual(
            runner.nonpassing_outcome_ids(outcomes),
            ["fails", "has-error", "is-skipped"],
        )

    def test_ctest_junit_preserves_pass_fail_and_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "ctest.xml"
            report.write_text(
                """<testsuite>
  <testcase name="passes" />
  <testcase name="fails"><failure /></testcase>
  <testcase name="is-skipped"><skipped /></testcase>
</testsuite>
""",
                encoding="utf-8",
            )

            self.assertEqual(
                runner.read_ctest_outcomes(report),
                {"passes": "passed", "fails": "failed", "is-skipped": "skipped"},
            )

    def test_direct_fixed_target_result_is_authoritative(self) -> None:
        target = "utest_target"
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            log_path = staging / "fixed-verification.log"

            def materialize_worktree(**kwargs: object) -> None:
                fixed_root = Path(kwargs["target"])
                (fixed_root / ".debugging-framework").mkdir(parents=True)

            def run_in_image(
                runtime: str,
                image: str,
                project_root: Path,
                command: list[str],
                *,
                timeout: int,
            ) -> SimpleNamespace:
                del runtime, image, timeout
                report = project_root / ".debugging-framework" / "fixed-target-0.xml"
                report.write_text(
                    f'<testsuite><testcase name="{target}"><failure /></testcase></testsuite>',
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=8,
                    stdout=(
                        f"1/1 Test #1: {target} ...***Failed\n"
                        "0% tests passed, 1 tests failed out of 1\n"
                        "The following tests FAILED:\n"
                    ),
                )

            with mock.patch.object(
                runner.materializer,
                "materialize_worktree",
                side_effect=materialize_worktree,
            ), mock.patch.object(
                runner, "run_commands_logged"
            ), mock.patch.object(
                runner, "run_in_image", side_effect=run_in_image
            ), mock.patch.object(
                runner, "observe_ctest_suite", return_value={target: "passed"}
            ):
                direct, suite = runner.verify_fixed_oracle(
                    source_repo=staging / "source",
                    staging=staging,
                    bug={"commit_after": "a" * 40},
                    runtime="docker",
                    image="sha256:test",
                    jobs=1,
                    targets=[target],
                    timeout=30,
                    log_path=log_path,
                )

        self.assertEqual(direct, {target: "failed"})
        self.assertEqual(suite, {target: "passed"})
        self.assertEqual(runner.select_repair_targets([target], direct), [])


class FrameworkContractTests(unittest.TestCase):
    def test_generated_config_is_ready_and_marks_workspace_disposable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            runner.write_framework_config(
                config_path,
                failing_tests=["utest_target"],
                image="sha256:test",
                runtime="docker",
                jobs=2,
                excluded_regression_tests=["known_fixed_skip"],
            )

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(runner.framework_config_ready(config_path))
            self.assertEqual(
                config["workspace"],
                {"disposable": True, "initialize_git_if_missing": True},
            )
            regression = config["regression_test"][0]["command"]
            self.assertIn("-E", regression)
            self.assertIn("known_fixed_skip", regression[regression.index("-E") + 1])

    def test_config_without_workspace_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            runner.write_framework_config(
                config_path,
                failing_tests=["utest_target"],
                image="sha256:test",
                runtime="docker",
                jobs=2,
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            del config["workspace"]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            self.assertFalse(runner.framework_config_ready(config_path))

    def test_prepare_rejects_reusing_a_different_image_digest(self) -> None:
        bug = {
            "type": {"id": "X"},
            "commit_before": "b" * 40,
            "commit_after": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as directory:
            inputs_root = Path(directory) / "inputs"
            inputs_root.mkdir()
            case_id = runner.case_id_for(bug)
            project, config, failure = runner.input_paths(inputs_root, case_id)
            project.mkdir()
            failure.write_text("failing test output\n", encoding="utf-8")
            runner.write_framework_config(
                config,
                failing_tests=["utest_target"],
                image="sha256:old",
                runtime="docker",
                jobs=2,
            )

            with self.assertRaisesRegex(RuntimeError, "--force"):
                runner.prepare_case(
                    bug=bug,
                    project_meta={},
                    inputs_root=inputs_root,
                    cache_root=Path(directory) / "cache",
                    runtime="docker",
                    image="sha256:new",
                    jobs=2,
                    command_timeout=30,
                    force=False,
                )


if __name__ == "__main__":
    unittest.main()
