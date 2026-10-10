import copy
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as N
from unittest.mock import Mock, patch

from aibenchmark_esw import cli
from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.sampling import pass_at_k


class TestCLIBacklogWorkflows(unittest.TestCase):
    def invoke(self, arguments):
        args = cli.build_parser().parse_args(arguments)
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            if args.command == "run":
                status = cli.cmd_run(args)
            else:
                status = getattr(cli, "cmd_" + args.command.replace("-", "_"))(args)
        return status, out.getvalue(), err.getvalue()

    def test_samples_have_distinct_identity_correct_pass_k_and_offline_exports(self):
        self.assertAlmostEqual(pass_at_k(5, 2, 2), 0.7)
        with TemporaryDirectory() as directory:
            output = Path(directory) / "sampled.json"
            self.assertEqual(self.invoke(["run", "--tasks", "tier1_crc16", "--samples", "2", "--pass-k", "2", "--output", str(output)])[0], 0)
            report = json.loads(output.read_text())
            self.assertEqual(len({child["metadata"]["run_id"] for child in report["samples"]}), 2)
            self.assertEqual(report["sampling"]["statistics"]["tier1_crc16"]["pass_at_k"], {"2": 1.0})
            BenchmarkReporter.from_json_dict(report)
            from aibenchmark_esw.report_schema import validate_report_structure
            validate_report_structure(report)
            # A completed collection resumes without repeating compilation.
            with patch.object(cli, "evaluate_task", side_effect=AssertionError("repeat")):
                self.assertEqual(self.invoke(["run", "--resume", str(output)])[0], 0)
            self.assertEqual(self.invoke(["aggregate", "--results", str(output), "--format", "json"])[0], 0)
            for format_name in ("html", "sarif"):
                export = Path(directory) / ("report." + format_name)
                self.assertEqual(self.invoke(["report", "--results", str(output), "--format", format_name, "--output", str(export)])[0], 0)
                self.assertGreater(export.stat().st_size, 100)

    def test_outage_after_success_restores_all_failed_slots_and_reordered_checkpoint(self):
        loader = DatasetLoader()
        tasks = loader.list_tasks()[:4]
        def response(task):
            return N(model="mock-resolved", usage=N(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                choices=[N(finish_reason="stop", message=N(content=loader.get_reference_solution(task.id)))])
        provider = Mock()
        provider.completion.side_effect = [response(tasks[0]), TimeoutError(), TimeoutError(), TimeoutError()]
        with TemporaryDirectory() as directory, patch.dict("sys.modules", {"litellm": provider}):
            output = Path(directory) / "outage.json"
            command = ["run", "--model", "mock", "--tasks", ",".join(task.id for task in tasks), "--output", str(output)]
            self.assertEqual(self.invoke(command)[0], 1)
            report = json.loads(output.read_text())
            self.assertEqual(set(report["metadata"]["pending_tasks"]), {task.id for task in tasks[1:]})
            report["tasks"].reverse()
            output.write_text(json.dumps(report), encoding="utf-8")
            provider.completion.side_effect = [response(task) for task in tasks[1:]]
            self.assertEqual(self.invoke(["run", "--resume", str(output)])[0], 0)
            report = json.loads(output.read_text())
            self.assertEqual([task["task_id"] for task in report["tasks"]], [task.id for task in tasks])
            self.assertEqual(provider.completion.call_count, 7)
            self.assertTrue(all(task["test_result"]["all_passed"] for task in report["tasks"]))

    def test_baseline_cli_doctor_mutations_replay_comparison_and_trend(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            solutions = root / "solutions"
            self.assertEqual(self.invoke(["doctor", "--tasks", "tier1_crc16", "--check-references"])[0], 0)
            self.assertEqual(self.invoke(["mutations", "--tasks", "tier1_crc16", "--output", str(root / "mutations.json")])[0], 0)
            self.assertEqual(self.invoke(["run", "--tasks", "tier1_crc16", "--output", str(first), "--save-solutions", str(solutions)])[0], 0)
            self.assertEqual(self.invoke(["replay", "--results", str(first), "--solutions", str(solutions), "--output", str(root / "replay.json")])[0], 0)
            report = json.loads(first.read_text())
            report = copy.deepcopy(report)
            report["model_name"] = "alternative"
            report["metadata"]["run_id"] = "alternative-run"
            for task in report["tasks"]:
                task["model_name"] = "alternative"
            second.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(self.invoke(["compare", "--results", str(first), str(second), "--format", "junit", "--baseline-model", "baseline"])[0], 0)
            self.assertEqual(self.invoke(["trend", "--results", str(first), str(second), "--format", "html"])[0], 0)
            with self.assertRaises(ValueError):
                self.invoke(["aggregate", "--results", str(first), "--output", str(first)])

    def test_baseline_check_entrypoint_and_cli_validation_errors(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "wrong.json"
            output.write_text("{}", encoding="utf-8")
            process = subprocess.run([sys.executable, "-m", "aibenchmark_esw.baseline_check", str(output)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 1)
            self.assertIn("stale", process.stderr)
        for command in ("doctor", "mutations"):
            status, _, error = self.invoke([command, "--tasks", "missing"])
            self.assertEqual(status, 1)
            self.assertIn("Unknown task", error)

    def test_budget_aborts_before_request_and_can_resume_with_increased_limit(self):
        provider = Mock()
        loader = DatasetLoader()
        provider.completion.return_value = N(model="mock-resolved", usage=N(
            prompt_tokens=2, completion_tokens=3, total_tokens=5,
            prompt_tokens_details=N(cached_tokens=1), completion_tokens_details=N(reasoning_tokens=2)),
            choices=[N(finish_reason="stop", message=N(content=loader.get_reference_solution("tier1_crc16")))])
        with TemporaryDirectory() as directory, patch.dict("sys.modules", {"litellm": provider}):
            output = Path(directory) / "budget.json"
            command = ["run", "--model", "mock", "--tasks", "tier1_crc16", "--max-tokens", "100",
                "--input-cost-per-million", "1", "--output-cost-per-million", "2", "--max-cost-usd", "0", "--output", str(output)]
            self.assertEqual(self.invoke(command)[0], 1)
            provider.completion.assert_not_called()
            self.assertEqual(self.invoke(["run", "--resume", str(output), "--max-cost-usd", "1"])[0], 0)
            report = json.loads(output.read_text())
            generation = report["tasks"][0]["generation"]
            # The unattempted aborted slot does not invent measured usage/cost.
            self.assertAlmostEqual(generation["known_cost_usd"], 8e-6)
            self.assertEqual(generation["turns"][0]["usage"]["prompt_tokens_details"]["cached_tokens"], 1)
            self.assertEqual(generation["turns"][0]["usage"]["completion_tokens_details"]["reasoning_tokens"], 2)
            self.assertAlmostEqual(report["metadata"]["cost_budget"]["charged_usd"], 8e-6)

    def test_partial_sampling_collection_resumes_only_pending_sample(self):
        provider = Mock()
        loader = DatasetLoader()
        response = N(model="mock-resolved", usage=N(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            choices=[N(finish_reason="stop", message=N(content=loader.get_reference_solution("tier1_crc16")))])
        failure = RuntimeError("authentication")
        failure.status_code = 401
        provider.completion.side_effect = [response, failure]
        with TemporaryDirectory() as directory, patch.dict("sys.modules", {"litellm": provider}):
            output = Path(directory) / "sampled.json"
            self.assertEqual(self.invoke(["run", "--model", "mock", "--tasks", "tier1_crc16", "--samples", "2", "--output", str(output)])[0], 1)
            report = json.loads(output.read_text())
            first_id = report["samples"][0]["metadata"]["run_id"]
            self.assertEqual(report["sampling"]["pending"], [1])
            provider.completion.side_effect = [response]
            self.assertEqual(self.invoke(["run", "--resume", str(output)])[0], 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["samples"][0]["metadata"]["run_id"], first_id)
            self.assertEqual(provider.completion.call_count, 3)

    def test_sampling_resume_preserves_solution_root_and_checks_completed_compatibility(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "samples.json"
            sources = Path(directory) / "sources"
            self.assertEqual(self.invoke(["run", "--tasks", "tier1_crc16", "--samples", "2", "--output", str(output),
                                          "--save-solutions", str(sources)])[0], 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["metadata"]["run_options"]["save_solutions"], str(sources.resolve()))
            report["metadata"]["evaluator_sha256"] = "changed"
            output.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evaluator_sha256"):
                self.invoke(["run", "--resume", str(output)])

    def test_sampling_reader_rejects_tampered_pass_k_statistics(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "samples.json"
            self.assertEqual(self.invoke(["run", "--tasks", "tier1_crc16", "--samples", "2", "--output", str(output)])[0], 0)
            report = json.loads(output.read_text())
            report["sampling"]["statistics"]["tier1_crc16"]["pass_at_k"]["1"] = 0
            with self.assertRaisesRegex(ValueError, "statistics"):
                BenchmarkReporter.from_json_dict(report)
