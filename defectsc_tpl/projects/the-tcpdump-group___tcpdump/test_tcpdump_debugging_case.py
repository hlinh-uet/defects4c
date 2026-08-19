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


adapter = load_module("_test_tcpdump_adapter", "run_tcpdump_tests.py")
build_adapter = load_module("_test_tcpdump_build_adapter", "run_tcpdump_build.py")
runner = load_module("_test_tcpdump_runner", "run_debugging_case.py")


def write_testlist(root: Path, lines: str) -> None:
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "TESTLIST").write_text(lines, encoding="utf-8")


class TestlistTests(unittest.TestCase):
    def test_parser_preserves_options_and_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_testlist(
                root,
                "# comment\n"
                "target target.pcap target.out -vv -n\n"
                "passing pass.pcap pass.out\n",
            )
            parsed = runner.parse_testlist(root)

        self.assertEqual(parsed["target"], ("target.pcap", "target.out", "-vv -n"))
        self.assertEqual(parsed["passing"], ("pass.pcap", "pass.out", ""))

    def test_target_mapping_accepts_pcap_or_expected_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_testlist(
                root,
                "first target.pcap generated.out -n\n"
                "second other.pcap target.out -vv\n"
                "unrelated pass.pcap pass.out\n",
            )
            bug = {
                "files": {
                    "test": ["tests/TESTLIST", "tests/target.pcap", "tests/target.out"]
                }
            }
            mapped = runner.target_tests_for_bug(root, bug)

        self.assertEqual(mapped, ["first", "second"])

    def test_duplicate_test_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_testlist(root, "same a.pcap a.out\nsame b.pcap b.out\n")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                runner.parse_testlist(root)


class AdapterTests(unittest.TestCase):
    def test_build_environment_enables_asan(self) -> None:
        environment = build_adapter.build_environment()

        self.assertIn("-fsanitize=address", environment["CFLAGS"])
        self.assertIn("-fsanitize=address", environment["LDFLAGS"])
        self.assertEqual(environment["ASAN_OPTIONS"], "detect_leaks=0")

    def test_adapter_outcomes_preserve_skipped(self) -> None:
        self.assertEqual(
            runner.tcpdump_adapter_outcomes(
                "PASSED pass\nFAILED fail\nSKIPPED skip\n"
            ),
            {"pass": "passed", "fail": "failed", "skip": "skipped"},
        )

    def test_nonzero_skip_is_not_mislabeled_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            for name in ("input.pcap", "expected.out", "TESTonce"):
                (tests / name).touch()
            (root / "tcpdump").touch()
            entry = adapter.TestEntry("skip", "input.pcap", "expected.out")
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout="TEST SKIPPED\n"),
            ):
                outcome, _ = adapter.run_one(root, entry, 30)

        self.assertEqual(outcome, "SKIPPED")

    def test_explicit_test_failure_wins_over_skip_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            for name in ("input.pcap", "expected.out", "TESTonce"):
                (tests / name).touch()
            (root / "tcpdump").touch()
            entry = adapter.TestEntry("fail", "input.pcap", "expected.out")
            with mock.patch.object(
                adapter.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=1, stdout="helper SKIPPED\nTEST FAILED\n"
                ),
            ):
                outcome, _ = adapter.run_one(root, entry, 30)

        self.assertEqual(outcome, "FAILED")


class SelectionAndOracleTests(unittest.TestCase):
    def test_regression_selection_caps_at_70_and_excludes_targets(self) -> None:
        discovered = ["target"] + [f"passing-{index}" for index in range(100)]
        selected = runner.select_regression_tests(
            discovered, excluded_tests=["target"], seed="case"
        )

        self.assertEqual(len(selected), 70)
        self.assertNotIn("target", selected)
        self.assertEqual(
            selected,
            runner.select_regression_tests(
                reversed(discovered), excluded_tests=["target"], seed="case"
            ),
        )

    def test_direct_fixed_target_outcome_is_authoritative(self) -> None:
        target = "target"
        regression_test = "passing"
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
                    bug={"commit_after": "a" * 40, "files": {"src": ["print.c"]}},
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
            selected = ["pass", "skip"]
            runner.write_framework_config(
                path,
                failing_tests=["target"],
                regression_tests=selected,
                excluded_regression_tests=["skip"],
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
            self.assertEqual(config["repair"]["failing_tests"], ["target"])
            self.assertEqual(config["repair"]["source_extensions"], [".c", ".h"])
            self.assertIn("--exclude-test", config["regression_test"][0]["command"])

    def test_config_rejects_all_regression_tests_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "must remain"):
                runner.write_framework_config(
                    path,
                    failing_tests=["target"],
                    regression_tests=["skip"],
                    excluded_regression_tests=["skip"],
                    image="sha256:test",
                    runtime="docker",
                    jobs=2,
                )


if __name__ == "__main__":
    unittest.main()
