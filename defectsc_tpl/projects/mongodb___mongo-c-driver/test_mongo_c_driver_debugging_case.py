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


adapter = load_module("_test_mongo_c_driver_adapter", "run_mongo_c_driver_tests.py")
build_adapter = load_module(
    "_test_mongo_c_driver_build_adapter", "run_mongo_c_driver_build.py"
)
runner = load_module("_test_mongo_c_driver_runner", "run_debugging_case.py")


def write_registered_tests(root: Path) -> None:
    tests_dir = root / "src" / "libbson" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test-bson.c").write_text(
        """
        void install_bson (TestSuite *suite) {
          TestSuite_Add (suite, "/bson/validate", test_bson_validate);
          TestSuite_AddFull (suite, "/bson/copy", test_bson_copy, NULL, NULL);
          /* TestSuite_Add (suite, "/ignored/block", ignored); */
          // TestSuite_Add (suite, "/ignored/line", ignored);
        }
        """,
        encoding="utf-8",
    )


class DiscoveryTests(unittest.TestCase):
    def test_discovery_reads_registered_libbson_ids_and_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_registered_tests(root)

            expected = ["/bson/copy", "/bson/validate"]
            self.assertEqual(runner.discover_mongo_c_driver_tests(root), expected)
            self.assertEqual(adapter.discover_tests(root), expected)

    def test_target_mapping_uses_test59_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_registered_tests(root)
            bug = {
                "files": {
                    "test": [
                        "src/libbson/tests/test-bson.c",
                        "src/libbson/tests/binary/test59.bson",
                    ]
                }
            }

            self.assertEqual(
                runner.target_tests_for_bug(root, bug), ["/bson/validate"]
            )


class AdapterTests(unittest.TestCase):
    def test_native_status_classification(self) -> None:
        self.assertEqual(
            adapter.classify_outcome('{"status": "PASS"}\n', 0), "PASSED"
        )
        self.assertEqual(
            adapter.classify_outcome('{"status": "FAIL"}\n', 1), "FAILED"
        )
        self.assertEqual(
            adapter.classify_outcome('{"status": "SKIP"}\n', 0), "SKIPPED"
        )
        self.assertIsNone(adapter.classify_outcome("unexpected output\n", 0))
        self.assertEqual(adapter.classify_outcome("timeout\n", 124), "FAILED")

    def test_run_one_invokes_exact_libbson_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / adapter.TEST_BINARY
            binary.parent.mkdir(parents=True)
            binary.touch()
            with mock.patch.object(
                adapter,
                "run_command",
                return_value=(0, '{"status": "PASS"}\n'),
            ) as run_command:
                outcome, _ = adapter.run_one(root, "/bson/validate", 30)

        self.assertEqual(outcome, "PASSED")
        self.assertEqual(
            run_command.call_args.args[0],
            [str(binary), "-l", "/bson/validate"],
        )
        self.assertEqual(run_command.call_args.kwargs["cwd"], root)

    def test_build_adapter_uses_debug_cmake_and_ninja(self) -> None:
        environment = build_adapter.build_environment()

        self.assertEqual(environment["CC"], "gcc")
        self.assertEqual(environment["CXX"], "g++")
        self.assertIn("-DCMAKE_BUILD_TYPE=Debug", build_adapter.CONFIGURE_FLAGS)
        self.assertIn("-DENABLE_TESTS=ON", build_adapter.CONFIGURE_FLAGS)
        self.assertIn("-DENABLE_MAINTAINER_FLAGS=OFF", build_adapter.CONFIGURE_FLAGS)

    def test_build_action_checks_combined_native_test_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = (
                root
                / build_adapter.BUILD_DIR
                / "src"
                / "libmongoc"
                / "test-libmongoc"
            )
            binary.parent.mkdir(parents=True)
            binary.touch()
            data = root / "src" / "libbson" / "tests" / "binary"
            data.mkdir(parents=True)
            with mock.patch.object(
                build_adapter.Path, "cwd", return_value=root
            ), mock.patch.object(build_adapter, "run", return_value=0) as run:
                returncode = build_adapter.main(["build", "--jobs", "2"])

        self.assertEqual(returncode, 0)
        self.assertEqual(
            run.call_args.args[0],
            [
                "cmake",
                "--build",
                build_adapter.BUILD_DIR.as_posix(),
                "--parallel",
                "2",
            ],
        )


class SelectionAndOracleTests(unittest.TestCase):
    def test_regression_selection_caps_at_70_and_excludes_target(self) -> None:
        discovered = ["/bson/validate"] + [f"/test/{index}" for index in range(100)]
        selected = runner.select_regression_tests(
            discovered,
            excluded_tests=["/bson/validate"],
            seed="CVE-2018-16790",
        )

        self.assertEqual(len(selected), 70)
        self.assertNotIn("/bson/validate", selected)
        self.assertEqual(
            selected,
            runner.select_regression_tests(
                reversed(discovered),
                excluded_tests=["/bson/validate"],
                seed="CVE-2018-16790",
            ),
        )

    def test_direct_fixed_target_outcome_is_authoritative(self) -> None:
        target = "/bson/validate"
        regression_test = "/bson/copy"
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)

            def materialize_snapshot(**kwargs: object) -> None:
                Path(kwargs["target"]).mkdir()

            responses = [
                SimpleNamespace(returncode=1, stdout=f"FAILED {target}\n"),
                SimpleNamespace(
                    returncode=0, stdout=f"PASSED {regression_test}\n"
                ),
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
                        "files": {"src": ["src/libbson/src/bson/bson-iter.c"]},
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
                failing_tests=["/bson/validate"],
                regression_tests=["/bson/copy", "/bson/new"],
                excluded_regression_tests=["/bson/new"],
                image="sha256:test",
                runtime="docker",
                jobs=2,
            )
            config = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(runner.framework_config_ready(path))
            self.assertEqual(config["system"], "cmake")
            self.assertEqual(
                config["workspace"],
                {"disposable": True, "initialize_git_if_missing": True},
            )
            self.assertEqual(
                config["repair"]["failing_tests"], ["/bson/validate"]
            )
            regression_command = config["regression_test"][0]["command"]
            self.assertIn("--exclude-test", regression_command)
            self.assertEqual(regression_command.count("--test"), 2)

    def test_config_rejects_all_regression_tests_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "must remain"):
                runner.write_framework_config(
                    path,
                    failing_tests=["/bson/validate"],
                    regression_tests=["/bson/copy"],
                    excluded_regression_tests=["/bson/copy"],
                    image="sha256:test",
                    runtime="docker",
                    jobs=2,
                )


if __name__ == "__main__":
    unittest.main()
