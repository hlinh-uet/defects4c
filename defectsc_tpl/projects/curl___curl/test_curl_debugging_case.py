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


adapter = load_module("_test_curl_adapter", "run_curl_tests.py")
build_adapter = load_module("_test_curl_build_adapter", "run_curl_build.py")
runner = load_module("_test_curl_runner", "run_debugging_case.py")


def write_test_data(root: Path, names: list[str]) -> None:
    data_dir = root / "tests" / "data"
    data_dir.mkdir(parents=True)
    for name in names:
        (data_dir / name).touch()


class DiscoveryTests(unittest.TestCase):
    def test_discovery_accepts_only_numbered_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_test_data(root, ["test20", "test3", "Makefile.inc", "test3.txt"])

            self.assertEqual(runner.discover_curl_tests(root), ["3", "20"])
            self.assertEqual(adapter.discover_tests(root), ["3", "20"])

    def test_target_mapping_uses_declared_test_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_test_data(root, ["test1", "test1440", "test1441"])
            bug = {
                "files": {
                    "test": [
                        "tests/data/Makefile.inc",
                        "tests/data/test1440",
                        "tests/data/test1441",
                    ]
                }
            }

            self.assertEqual(
                runner.target_tests_for_bug(root, bug), ["1440", "1441"]
            )


class AdapterTests(unittest.TestCase):
    def test_native_summary_classification(self) -> None:
        self.assertEqual(
            adapter.classify_outcome(
                "TESTDONE: 1 tests out of 1 reported OK: 100%\n", 0
            ),
            "PASSED",
        )
        self.assertEqual(
            adapter.classify_outcome(
                "TESTDONE: 0 tests out of 1 reported OK: 0%\n"
                "TESTFAIL: These test cases failed: 1440\n",
                1,
            ),
            "FAILED",
        )
        self.assertEqual(
            adapter.classify_outcome(
                "test 1440 SKIPPED: curl lacks SSL support\n"
                "TESTFAIL: No tests were performed\n"
                "TESTINFO: 1 tests were skipped due to these restraints:\n",
                0,
            ),
            "SKIPPED",
        )
        self.assertIsNone(adapter.classify_outcome("unexpected output\n", 0))

    def test_run_one_invokes_exact_test_without_valgrind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_test_data(root, ["test1440"])
            (root / "tests" / "runtests.pl").touch()
            (root / "src").mkdir()
            (root / "src" / "curl").touch()
            with mock.patch.object(
                adapter,
                "run_command",
                return_value=(
                    0,
                    "TESTDONE: 1 tests out of 1 reported OK: 100%\n",
                ),
            ) as run_command:
                outcome, _ = adapter.run_one(root, "1440", 30)

        self.assertEqual(outcome, "PASSED")
        self.assertEqual(
            run_command.call_args.args[0],
            ["perl", "./runtests.pl", "-n", "1440"],
        )

    def test_regression_batch_preserves_pass_fail_and_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_test_data(root, ["test1", "test2", "test3"])
            (root / "tests" / "runtests.pl").touch()
            (root / "src").mkdir()
            (root / "src" / "curl").touch()
            output = (
                "PASS: 1 - passing\n"
                "FAIL: 2 - failing - stdout\n"
                "TESTDONE: 2 tests out of 2 reported OK: 50%\n"
                "TESTFAIL: These test cases failed: 2\n"
                "TESTDONE: 3 tests were considered during 1 seconds.\n"
            )
            with mock.patch.object(
                adapter, "run_command", return_value=(1, output)
            ) as run_command:
                outcomes, _ = adapter.run_many(root, ["1", "2", "3"], 30)

        self.assertEqual(
            outcomes, {"1": "PASSED", "2": "FAILED", "3": "SKIPPED"}
        )
        self.assertEqual(
            run_command.call_args.args[0],
            ["perl", "./runtests.pl", "-n", "-a", "-am", "1", "2", "3"],
        )

    def test_build_adapter_matches_dependency_minimal_recipe(self) -> None:
        environment = build_adapter.build_environment()

        self.assertEqual(environment["CC"], "gcc")
        self.assertIn("--without-zlib", build_adapter.CONFIGURE_FLAGS)
        self.assertIn("--without-libpsl", build_adapter.CONFIGURE_FLAGS)

    def test_build_action_materializes_native_test_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            required = (
                root / "src" / "curl",
                root / "tests" / "runtests.pl",
                root / "tests" / "data",
                root / "tests" / "server" / "sws",
                root / "tests" / "server" / "sockfilt",
            )
            for path in required:
                if path.suffix or path.name in {"curl", "sws", "sockfilt"}:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                else:
                    path.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(build_adapter.Path, "cwd", return_value=root), mock.patch.object(
                build_adapter, "run", return_value=0
            ) as run:
                returncode = build_adapter.main(["build", "--jobs", "2"])

        self.assertEqual(returncode, 0)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["make", "-C", "tests", "-j", "2", "all"],
        )


class SelectionAndOracleTests(unittest.TestCase):
    def test_regression_selection_caps_at_70_and_excludes_targets(self) -> None:
        discovered = ["1440", "1441"] + [str(index) for index in range(1, 101)]
        selected = runner.select_regression_tests(
            discovered,
            excluded_tests=["1440", "1441"],
            seed="CVE-2017-7407",
        )

        self.assertEqual(len(selected), 70)
        self.assertNotIn("1440", selected)
        self.assertNotIn("1441", selected)
        self.assertEqual(
            selected,
            runner.select_regression_tests(
                reversed(discovered),
                excluded_tests=["1440", "1441"],
                seed="CVE-2017-7407",
            ),
        )

    def test_direct_fixed_target_outcome_is_authoritative(self) -> None:
        target = "1440"
        regression_test = "1"
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)

            def materialize_snapshot(**kwargs: object) -> None:
                Path(kwargs["target"]).mkdir()

            responses = [
                SimpleNamespace(returncode=1, stdout=f"FAILED {target}\n"),
                SimpleNamespace(returncode=0, stdout=f"PASSED {regression_test}\n"),
            ]
            with mock.patch.object(
                runner, "materialize_snapshot", side_effect=materialize_snapshot
            ), mock.patch.object(
                runner, "run_commands_logged"
            ), mock.patch.object(
                runner, "run_in_image", side_effect=responses
            ):
                direct, regression = runner.verify_fixed_oracle(
                    source_repo=staging / "source",
                    staging=staging,
                    bug={
                        "commit_after": "a" * 40,
                        "files": {"src": ["src/tool_writeout.c"]},
                    },
                    runtime="docker",
                    image="sha256:test",
                    jobs=1,
                    target_tests=[target],
                    regression_tests=[regression_test],
                    timeout=30,
                    log_path=staging / "fixed.log",
                )

        self.assertEqual(direct, {target: "failed"})
        self.assertEqual(regression, {regression_test: "passed"})


class FrameworkContractTests(unittest.TestCase):
    def test_generated_config_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            runner.write_framework_config(
                path,
                failing_tests=["1440", "1441"],
                regression_tests=["1", "2"],
                excluded_regression_tests=["2"],
                image="sha256:test",
                runtime="docker",
                jobs=2,
            )
            config = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(runner.framework_config_ready(path))
            self.assertEqual(
                config["workspace"],
                {"disposable": True, "initialize_git_if_missing": True},
            )
            self.assertEqual(
                config["repair"]["failing_tests"], ["1440", "1441"]
            )
            self.assertIn("--exclude-test", config["regression_test"][0]["command"])

    def test_config_rejects_all_regression_tests_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "must remain"):
                runner.write_framework_config(
                    path,
                    failing_tests=["1440"],
                    regression_tests=["1"],
                    excluded_regression_tests=["1"],
                    image="sha256:test",
                    runtime="docker",
                    jobs=2,
                )


if __name__ == "__main__":
    unittest.main()
