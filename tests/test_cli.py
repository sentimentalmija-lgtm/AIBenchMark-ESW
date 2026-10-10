import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from dataclasses import replace

from aibenchmark_esw import cli
from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.metrics.reporter import BenchmarkReporter


class TestCLI(unittest.TestCase):
    def test_generation_settings_usage_and_candidate_are_saved_for_replay(self):
        reference = DatasetLoader().get_reference_solution("tier1_crc16")
        provider = MagicMock()
        provider.completion.return_value = Namespace(model="resolved-snapshot", usage=Namespace(
            prompt_tokens=20, completion_tokens=30, total_tokens=50), choices=[Namespace(
                finish_reason="stop", message=Namespace(content=f"```c\n{reference}\n```"))])
        with TemporaryDirectory() as directory:
            report = Path(directory) / "run.json"
            solutions = Path(directory) / "solutions"
            args = cli.build_parser().parse_args(["run", "--model", "anthropic/mock", "--tasks", "tier1_crc16",
                "--max-tokens", "2048", "--request-timeout", "10", "--output", str(report),
                "--save-solutions", str(solutions)])
            with patch.dict("sys.modules", {"litellm": provider}), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_run(args), 0)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual((solutions / "tier1_crc16.c").read_text(encoding="utf-8"), reference.strip())
        restored = BenchmarkReporter.from_json_dict(data)
        self.assertEqual(restored[0].generation["usage"]["total_tokens"], 50)
        self.assertEqual(restored[0].generation["max_tokens"], 2048)
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["metadata"]["generation_settings"]["max_tokens"], 2048)
        self.assertEqual(len(restored[0].provenance["candidate_sha256"]), 64)
        self.assertEqual(len(restored[0].provenance["prompt_sha256"]), 64)

    def test_truncated_generation_is_saved_with_zero_score_and_usage(self):
        provider = MagicMock()
        provider.completion.return_value = Namespace(model="mock", usage=Namespace(
            prompt_tokens=20, completion_tokens=10, total_tokens=30), choices=[Namespace(
                finish_reason="length", message=Namespace(content="int x;"))])
        with TemporaryDirectory() as directory:
            report = Path(directory) / "run.json"
            args = cli.build_parser().parse_args(["run", "--model", "mock", "--tasks", "tier1_crc16",
                                                 "--output", str(report)])
            with patch.dict("sys.modules", {"litellm": provider}), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_run(args), 1)
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["overall_score"], 0)
        self.assertEqual(data["pass_at_1_pct"], 0)
        self.assertIn("truncated", data["tasks"][0]["error_log"])
        self.assertEqual(data["tasks"][0]["generation"]["usage"]["total_tokens"], 30)

    def test_invalid_generation_arguments_are_rejected(self):
        for flag, value in (("--max-tokens", "0"), ("--request-timeout", "nan"), ("--temperature", "inf")):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["run", flag, value])

    def test_pass_k_reports_cli_usage_for_invalid_values(self):
        for value in ("abc", "0", "1,1", "1,", ""):
            with self.subTest(value=value), redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as exit_status:
                cli.build_parser().parse_args(["run", "--samples", "2", "--pass-k", value])
            self.assertEqual(exit_status.exception.code, 2)
            self.assertIn("argument --pass-k", error.getvalue())
            self.assertIn("positive integers", error.getvalue())
        parsed = cli.build_parser().parse_args(["run", "--samples", "2", "--pass-k", "1,2"])
        self.assertEqual(parsed.pass_k, "1,2")

    def test_model_notes_before_c_implementation_still_score_full_points(self):
        loader = DatasetLoader()
        reference = loader.get_reference_solution("tier1_crc16")
        response = f"Analysis:\n```\nCCITT-FALSE 0x1021 implementation plan\n```\n```c\n{reference}\n```"
        litellm = MagicMock()
        litellm.completion.return_value = Namespace(choices=[Namespace(message=Namespace(content=response))])
        with TemporaryDirectory() as directory:
            report = Path(directory) / "results.json"
            args = Namespace(tier=None, tasks="tier1_crc16", model="mock", compiler=None, output=str(report))
            with patch.dict("sys.modules", {"litellm": litellm}), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_run(args), 0)
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["pass_at_1_pct"], 100)
        self.assertEqual(data["overall_score"], 100)

    def test_task_standard_is_used_for_model_prompt(self):
        for standard in ("c99", "c11", "c17"):
            with self.subTest(standard=standard):
                loader = DatasetLoader()
                task = replace(loader.get_task("tier1_crc16"), target_standard=standard,
                               prompt=f"Implement the API using {standard.upper()}.")
                loader._tasks = {task.id: task}
                captured = []
                def generate(messages):
                    captured.extend(messages)
                    return loader.get_reference_solution(task.id)
                args = Namespace(tier=None, tasks=None, model="mock", compiler=None, output=None)
                with patch.object(cli, "DatasetLoader", return_value=loader), \
                        patch.object(cli.LLMClient, "generate_solution", side_effect=generate), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.cmd_run(args), 0)
                self.assertIn(f"portable {standard.upper()} code", captured[0]["content"])

    def test_generation_failure_is_saved_and_included_in_denominator(self):
        loader = DatasetLoader()
        with TemporaryDirectory() as directory:
            report = Path(directory) / "results.json"
            args = Namespace(tier=None, tasks="tier1_crc16,tier1_ring_buffer", model="mock",
                             compiler=None, output=str(report))
            with patch.object(cli.LLMClient, "generate_solution", side_effect=[
                RuntimeError("API failed"), loader.get_reference_solution("tier1_ring_buffer")
            ]), redirect_stdout(io.StringIO()):
                exit_code = cli.cmd_run(args)
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(data["tasks"]), 2)
        self.assertEqual(data["pass_at_1_pct"], 50)
        self.assertEqual(data["tasks"][0]["scores"]["total"], 0)
        self.assertIn("API failed", data["tasks"][0]["error_log"])

    def test_all_generation_failures_are_saved(self):
        with TemporaryDirectory() as directory:
            report = Path(directory) / "results.json"
            args = Namespace(tier=1, tasks=None, model="mock", compiler=None, output=str(report))
            with patch.object(cli.LLMClient, "generate_solution", side_effect=RuntimeError("API failed")), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_run(args), 1)
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(len(data["tasks"]), len(DatasetLoader().list_tasks(tier=1)))
        self.assertEqual(data["pass_at_1_pct"], 0)
        self.assertEqual(data["overall_score"], 0)

    def test_missing_baseline_reference_is_saved(self):
        with TemporaryDirectory() as directory:
            report = Path(directory) / "results.json"
            args = Namespace(tier=None, tasks="tier1_crc16", model="baseline", compiler=None, output=str(report))
            with patch.object(cli.DatasetLoader, "get_reference_solution", return_value=None), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_run(args), 1)
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(len(data["tasks"]), 1)
        self.assertIn("Missing reference", data["tasks"][0]["error_log"])

    def test_unknown_task_is_rejected_before_running(self):
        args = Namespace(tier=None, tasks="tier1_crc16,typo", model="baseline", compiler=None, output=None)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_run(args), 1)

    def test_report_formats_and_legacy_json(self):
        data = json.loads(Path("results/baseline.json").read_text(encoding="utf-8"))
        # Exercise the pre-change schema even after refreshing the stored baseline.
        data["metadata"].pop("scoring_policy", None)
        for task in data["tasks"]:
            task["test_result"].pop("completed", None)
            task["test_result"].pop("returncode", None)
            task["size_metrics"].pop("measured", None)
            task["safety_metrics"].pop("violations", None)
            for key in ("weights", "target_standard", "effective_standard"):
                task.pop(key, None)
        with TemporaryDirectory() as directory:
            report = Path(directory) / "legacy.json"
            report.write_text(json.dumps(data), encoding="utf-8")
            for format_name, expected in [("markdown", "# AIBenchMark-ESW Benchmark Report"),
                                          ("cli", "AIBenchMark-ESW Benchmark Results")]:
                with self.subTest(format=format_name), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.cmd_report(Namespace(results=str(report), format=format_name)), 0)
                    self.assertIn(expected, output.getvalue())
                    output.getvalue().encode("cp949")
        restored = BenchmarkReporter.from_json_dict(data)
        self.assertEqual(len(restored), len(data["tasks"]))

    def test_invalid_dataset_exits_cleanly(self):
        with patch("sys.argv", ["aibenchmark-esw", "list"]), \
                patch.object(cli, "DatasetLoader", side_effect=ValueError("Invalid task")), \
                redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit:
                cli.main()
        self.assertEqual(exit.exception.code, 1)
        self.assertIn("Error: Invalid task", output.getvalue())

    def test_invalid_scores_are_reported_without_traceback(self):
        with TemporaryDirectory() as directory:
            report = Path(directory) / "invalid.json"
            data = json.loads(Path("results/baseline.json").read_text(encoding="utf-8"))
            for value in (None, "invalid", True, float("nan"), float("inf"), -1, 101):
                data["tasks"][0]["scores"]["total"] = value
                report.write_text(json.dumps(data), encoding="utf-8")
                for format_name in ("cli", "markdown"):
                    with self.subTest(value=value, format=format_name), redirect_stderr(io.StringIO()) as output:
                        self.assertEqual(cli.cmd_report(Namespace(results=str(report), format=format_name)), 1)
                        self.assertIn("Error: Invalid results file:", output.getvalue())
                        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
