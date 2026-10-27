import io
import json
import os
import shutil
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aibenchmark_esw import cli
from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.provenance import task_sha256
from aibenchmark_esw.resources import data_root


@contextmanager
def working_directory(path):
    original = Path.cwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(original)


class TestExternalDataset(unittest.TestCase):
    def test_external_entry_names_cannot_collide_with_generated_harness(self):
        base = DatasetLoader().get_task("tier1_crc16")
        names = ("crc16.c", "aibenchmark_tests_0.c", "AIBENCHMARK_TESTS_0.c",
                 "unity.c", "candidate.c")
        for name in names:
            with self.subTest(entry_name=name), TemporaryDirectory() as directory:
                root = Path(directory) / "tasks"
                task_dir = root / base.id
                shutil.copytree(base.task_dir, task_dir)
                metadata = task_dir / "task.json"
                config = json.loads(metadata.read_text(encoding="utf-8"))
                original = task_dir / config["entry_file"]
                starter = original.read_bytes()
                entry = original.with_name(name)
                if entry != original:
                    original.rename(entry)
                config["entry_file"] = entry.relative_to(task_dir).as_posix()
                metadata.write_text(json.dumps(config), encoding="utf-8")
                report = Path(directory) / "report.json"
                args = cli.build_parser().parse_args([
                    "eval", "--tasks-root", str(root), "--task", base.id,
                    "--reference", "--output", str(report),
                ])
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(output):
                    status = cli.cmd_eval(args)
                self.assertEqual(status, 0, output.getvalue())
                results = BenchmarkReporter.from_json_dict(json.loads(report.read_text(encoding="utf-8")))
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].compiled, results[0].error_log)
                self.assertTrue(results[0].test_result.passed, results[0].test_result.output)
                self.assertEqual(results[0].test_result.passed_tests, 7)
                self.assertEqual(results[0].scores.total_score, 100)
                self.assertIsNone(results[0].error_log)
                self.assertEqual(entry.read_bytes(), starter)

    def test_external_tasks_support_list_eval_run_and_content_provenance(self):
        # Windows runners can place the checkout and temp files on different
        # drives. Run from the temp parent to exercise a valid relative root.
        with TemporaryDirectory() as directory, working_directory(directory):
            root = Path(directory) / "my_tasks"
            task_dir = root / "my_crc"
            shutil.copytree(DatasetLoader().get_task("tier1_crc16").task_dir, task_dir)
            metadata = task_dir / "task.json"
            config = json.loads(metadata.read_text(encoding="utf-8"))
            config["id"] = "custom_crc"
            metadata.write_text(json.dumps(config), encoding="utf-8")
            # Relative roots must work even though the compiler uses a temporary cwd.
            common = ["--tasks-root", os.path.relpath(root)]
            args = cli.build_parser().parse_args(["list", *common])
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.cmd_list(args), 0)
            self.assertIn("custom_crc", output.getvalue())
            self.assertNotIn("tier1_ring_buffer", output.getvalue())
            task = DatasetLoader(root).get_task("custom_crc")
            fingerprint = task_sha256(task, data_root() / "third_party/unity")
            for command, flags in (("eval", ["--task", "custom_crc", "--reference"]),
                                    ("run", ["--tasks", "custom_crc", "--tier", "1"])):
                report = Path(directory) / f"{command}.json"
                args = cli.build_parser().parse_args([command, *common, *flags, "--output", str(report)])
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(getattr(cli, f"cmd_{command}")(args), 0)
                data = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(data["metadata"]["selected_tasks"], ["custom_crc"])
                self.assertEqual(data["metadata"]["task_fingerprints"], {"custom_crc": fingerprint})
                self.assertEqual(data["overall_score"], 100)
                self.assertEqual(len(BenchmarkReporter.from_json_dict(data)), 1)
            # Output protection applies to external roots, too.
            args = cli.build_parser().parse_args(["run", *common, "--model", "mock",
                "--output", str(metadata)])
            with patch.object(cli.LLMClient, "generate_solution") as generate:
                with self.assertRaisesRegex(ValueError, "benchmark inputs"):
                    cli.cmd_run(args)
                generate.assert_not_called()
            self.assertEqual(json.loads(metadata.read_text())["id"], "custom_crc")

    def test_explicit_selection_errors_are_rejected_before_api_calls_or_outputs(self):
        cases = [
            (["--tasks", "tier1_crc16,tier2_debounce_fsm", "--tier", "1"], "outside --tier"),
            (["--tasks", "tier1_crc16,tier1_crc16"], "distinct"),
            (["--tasks", ""], "nonempty"),
            (["--tasks", "tier1_crc16,"], "nonempty"),
            (["--tasks", "tier1_crc16,typo"], "Unknown"),
        ]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            for flags, error in cases:
                args = cli.build_parser().parse_args(["run", "--model", "mock", *flags, "--output", str(output)])
                with self.subTest(flags=flags), patch.object(cli.LLMClient, "generate_solution") as generate:
                    with redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(cli.cmd_run(args), 1)
                    self.assertIn(error, errors.getvalue())
                    generate.assert_not_called()
                    self.assertFalse(output.exists())

    def test_external_ids_are_portable_saved_source_names(self):
        with TemporaryDirectory() as directory:
            task_dir = Path(directory) / "task"
            task_dir.mkdir()
            for task_id in ("C:escape", "a/b", "a\\b", "..", "with space", "CON", "nul", "COM1", "LPT9"):
                (task_dir / "task.json").write_text(json.dumps({"id": task_id}), encoding="utf-8")
                with self.subTest(task_id=task_id), self.assertLogs("aibenchmark_esw.dataset", level="ERROR"):
                    with self.assertRaisesRegex(ValueError, "Task id"):
                        DatasetLoader(Path(directory))
