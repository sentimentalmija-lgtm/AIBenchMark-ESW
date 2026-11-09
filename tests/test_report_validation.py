import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from aibenchmark_esw import cli
from aibenchmark_esw.metrics.reporter import BenchmarkReporter


class TestReportValidation(unittest.TestCase):
    def report(self):
        data = json.loads(Path("results/baseline.json").read_text(encoding="utf-8"))
        # These fixtures deliberately exercise the pre-policy legacy path.
        data["metadata"].pop("scoring_policy", None)
        for field in ('tier_weighted_score', 'tier_weight_policy', 'by_tier', 'by_category'):
            data.pop(field, None)
        return data

    def test_invalid_flags_counts_measurements_and_scores_are_rejected(self):
        mutations = [
            lambda task: task.update(compiled="false"),
            lambda task: task["test_result"].update(completed="false"),
            lambda task: task["test_result"].update(all_passed=1),
            lambda task: task["test_result"].update(total=True),
            lambda task: task["test_result"].update(passed=-1),
            lambda task: task["test_result"].update(passed=999),
            lambda task: task["test_result"].update(all_passed=False),
            lambda task: task["test_result"].update(returncode=7),
            lambda task: task["size_metrics"].update(flash_bytes=-1),
            lambda task: task["size_metrics"].update(measured="false"),
            lambda task: task["size_metrics"].update(measured=False),
            lambda task: task["safety_metrics"].update(error_count=-1),
            lambda task: task["safety_metrics"].update(violations="diagnostic"),
            lambda task: task.update(execution_time_sec=float("nan")),
            lambda task: task.update(execution_time_sec=-1),
            lambda task: task.update(tier=True),
            lambda task: task.update(task_id=[]),
            lambda task: task["scores"].update(functional=50),
            lambda task: task["scores"].update(total=50),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data = self.report()
                mutate(data["tasks"][0])
                with self.assertRaises(ValueError):
                    BenchmarkReporter.from_json_dict(data)

    def test_incomplete_diagnostics_are_kept_but_cannot_earn_points(self):
        data = self.report()
        task = data["tasks"][0]
        task["test_result"].update(completed=False, all_passed=False, total=1, passed=0,
                                    failed=2, ignored=3, returncode=7)
        task["scores"] = dict.fromkeys(("functional", "memory", "safety", "total"), 0)
        result = BenchmarkReporter.from_json_dict(data)[0]
        self.assertEqual(result.test_result.failed_tests, 2)
        self.assertEqual(BenchmarkReporter.to_json_dict([result], "baseline")["pass_at_1_pct"], 0)
        task["scores"]["total"] = 10
        with self.assertRaisesRegex(ValueError, "zero scores"):
            BenchmarkReporter.from_json_dict(data)

    def test_present_limits_and_weights_require_every_field(self):
        from aibenchmark_esw.report_schema import validate_report_structure

        for name in ("limits", "weights"):
            original = self.report()["tasks"][0][name]
            for missing in (tuple(original), *((key,) for key in original)):
                data = self.report()
                for key in missing:
                    del data["tasks"][0][name][key]
                with self.subTest(name=name, missing=missing):
                    with self.assertRaisesRegex(ValueError, "Missing " + name + " fields"):
                        BenchmarkReporter.from_json_dict(data)
                    with self.assertRaisesRegex(ValueError, "Report schema violation"):
                        validate_report_structure(data)

    def test_legacy_limits_warn_even_when_present(self):
        data = self.report()
        results = BenchmarkReporter.from_json_dict(data)
        self.assertTrue(BenchmarkReporter.scoring_warnings(results))
        for render in (BenchmarkReporter.generate_cli_table,
                       BenchmarkReporter.generate_markdown, BenchmarkReporter.generate_html):
            with self.subTest(render=render.__name__):
                self.assertIn("unverifiable", render(results, data["model_name"], data["metadata"]))

        data = json.loads(Path("results/baseline.json").read_text(encoding="utf-8"))
        results = BenchmarkReporter.from_json_dict(data)
        self.assertEqual(BenchmarkReporter.scoring_warnings(results, data["metadata"]), [])
        del data["metadata"]["scoring_policy"]["tasks"][results[0].task_id]
        warnings = BenchmarkReporter.scoring_warnings(results, data["metadata"])
        self.assertIn(results[0].task_id, " ".join(warnings))

    def test_absent_and_null_legacy_limits_weights_remain_unknown(self):
        for omit in (True, False):
            data = self.report()
            for task in data["tasks"]:
                for name in ("limits", "weights"):
                    if omit:
                        task.pop(name, None)
                    else:
                        task[name] = None
            results = BenchmarkReporter.from_json_dict(data)
            self.assertTrue(all(result.limits is None and result.weights is None for result in results))

    def test_rounded_partial_scores_and_valid_legacy_reports_still_load(self):
        data = self.report()
        task = data["tasks"][0]
        task["test_result"].update(total=3, passed=1, failed=2, ignored=0, all_passed=False, returncode=2)
        task["safety_metrics"].update(error_count=0, warning_count=2)
        task["limits"] = {"max_flash_bytes": 10000, "max_ram_bytes": 0, "timeout_seconds": 10}
        task["size_metrics"].update(flash_bytes=8451, ram_bytes=0, ref_flash_bytes=1000, ref_ram_bytes=0)
        task["scores"].update(functional=33.33, memory=17.21, safety=94, total=27.41)
        self.assertEqual(BenchmarkReporter.from_json_dict(data)[0].scores.total_score, 27.41)
        legacy = self.report()
        legacy.pop("schema_version")
        for task in legacy["tasks"]:
            task.pop("weights", None)
            task["test_result"].pop("completed", None)
            task["test_result"].pop("returncode", None)
        self.assertEqual(len(BenchmarkReporter.from_json_dict(legacy)), len(legacy["tasks"]))

    def test_unsupported_schema_and_duplicate_tasks_are_rejected(self):
        for version in (True, "2", 0, 3, None):
            data = self.report()
            data["schema_version"] = version
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "schema version"):
                BenchmarkReporter.from_json_dict(data)
        data = self.report()
        data["tasks"].append(copy.deepcopy(data["tasks"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate task"):
            BenchmarkReporter.from_json_dict(data)

    def test_pass_at_1_does_not_trust_in_memory_success_flags_alone(self):
        result = BenchmarkReporter.from_json_dict(self.report())[0]
        result.compiled = "false"
        self.assertEqual(BenchmarkReporter.to_json_dict([result], "test")["pass_at_1_pct"], 0)
        result.compiled = True
        result.test_result.passed_tests = 0
        self.assertEqual(BenchmarkReporter.to_json_dict([result], "test")["pass_at_1_pct"], 0)

    def test_report_and_compare_return_clean_errors_for_invalid_boolean(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            data = self.report()
            data["tasks"][0]["compiled"] = "false"
            path.write_text(json.dumps(data), encoding="utf-8")
            for argv in (["report", "--results", str(path)],
                         ["compare", "--results", str(path), "results/baseline.json"]):
                args = cli.build_parser().parse_args(argv)
                command = cli.cmd_report if args.command == "report" else cli.cmd_compare
                with self.subTest(command=args.command), redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                    self.assertEqual(command(args), 1)
                    self.assertIn("compiled must be a boolean", errors.getvalue())
                    self.assertNotIn("Traceback", errors.getvalue())

    def test_impossible_safety_and_legacy_composites_are_rejected(self):
        data = self.report()
        data["tasks"][0]["safety_metrics"]["error_count"] = 7
        with self.assertRaisesRegex(ValueError, "Safety score"):
            BenchmarkReporter.from_json_dict(data)
        data = self.report()
        task = data["tasks"][0]
        total = task["test_result"]["total"]
        task["test_result"].update(passed=0, failed=total, all_passed=False, returncode=total)
        task["scores"].update(functional=0, total=100)
        task.pop("weights", None)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            BenchmarkReporter.from_json_dict(data)

    def test_recorded_limits_validate_memory_and_legacy_limits_remain_visibly_unknown(self):
        data = self.report()
        task = data["tasks"][0]
        task["limits"] = {"max_flash_bytes": 1, "max_ram_bytes": 0, "timeout_seconds": 10}
        with self.assertRaisesRegex(ValueError, "Memory score"):
            BenchmarkReporter.from_json_dict(data)
        task.pop("limits")
        restored = BenchmarkReporter.from_json_dict(data)
        self.assertIn("cannot be independently verified", BenchmarkReporter.generate_markdown(restored, "baseline"))

    def test_aborted_state_and_optional_timings_round_trip(self):
        data = self.report()
        data["metadata"]["run_status"] = "aborted"
        data["metadata"]["pending_tasks"] = []
        data["tasks"][0].update(candidate_time_sec=0.25, reference_validation_time_sec=0.5)
        restored = BenchmarkReporter.from_json_dict(data)
        self.assertEqual(restored[0].candidate_time_sec, 0.25)
        self.assertEqual(restored[0].reference_validation_time_sec, 0.5)
        self.assertIn("aborted", BenchmarkReporter.run_warning(data["metadata"]))
        data["tasks"][0]["candidate_time_sec"] = -1
        with self.assertRaisesRegex(ValueError, "candidate_time_sec"):
            BenchmarkReporter.from_json_dict(data)

    def test_cli_report_and_compare_reject_contradictory_safety(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "contradiction.json"
            data = self.report()
            data["tasks"][0]["safety_metrics"]["warning_count"] = 2
            path.write_text(json.dumps(data), encoding="utf-8")
            for argv in (["report", "--results", str(path)],
                         ["compare", "--results", str(path), "results/baseline.json"]):
                args = cli.build_parser().parse_args(argv)
                command = cli.cmd_report if args.command == "report" else cli.cmd_compare
                with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                    self.assertEqual(command(args), 1)
                self.assertIn("Safety score", errors.getvalue())

    def _current_report(self):
        data = json.loads(Path("results/baseline.json").read_text(encoding="utf-8"))
        return BenchmarkReporter.to_json_dict(
            BenchmarkReporter.from_json_dict(data), data["model_name"], data["metadata"])

    def test_validate_report_rejects_contradictory_flat_overall_score(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flat.json"
            report = self._current_report()
            report["overall_score"] = 0
            path.write_text(json.dumps(report), encoding="utf-8")
            args = cli.build_parser().parse_args(["validate-report", str(path)])
            with self.assertRaisesRegex(ValueError, "overall_score disagrees"):
                cli.cmd_validate_report(args)

    def test_validate_report_rejects_contradictory_pass_at_1_pct(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "flat.json"
            report = self._current_report()
            report["pass_at_1_pct"] = 0
            path.write_text(json.dumps(report), encoding="utf-8")
            args = cli.build_parser().parse_args(["validate-report", str(path)])
            with self.assertRaisesRegex(ValueError, "pass_at_1_pct disagrees"):
                cli.cmd_validate_report(args)

    def test_validate_report_accepts_valid_current_report(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "valid.json"
            path.write_text(json.dumps(self._current_report()), encoding="utf-8")
            args = cli.build_parser().parse_args(["validate-report", str(path)])
            with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.cmd_validate_report(args), 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertIn("PASS", output.getvalue())


if __name__ == "__main__":
    unittest.main()
