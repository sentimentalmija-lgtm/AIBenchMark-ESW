import copy
import csv
import io
import json
import unittest

from dataclasses import asdict
from aibenchmark_esw.metrics.scorer import SCORING_FORMULA_VERSION, SAFETY_ERROR_PENALTY, SAFETY_WARNING_PENALTY
from aibenchmark_esw.metrics.aggregation import aggregate_runs, render_aggregation
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.models import (
    DimensionScores, SizeMetrics, StaticSafetyMetrics, TaskEvaluationResult,
    TaskLimits, TaskWeights, TestResult,
)


def fixture_report(score=100, run_id="run-1", model="provider/alias"):
    passed = int(score / 50)
    result = TaskEvaluationResult(
        "one", 1, model, True,
        TestResult(total_tests=2, passed_tests=passed, failed_tests=2-passed,
                   passed=passed == 2, completed=True, returncode=2-passed),
        SizeMetrics(100, 0, 100, 0), StaticSafetyMetrics(cppcheck_status="disabled"),
        DimensionScores(score, 100, 100, score), 0.1,
        weights=TaskWeights(), limits=TaskLimits(), target_standard="c99", effective_standard="c99",
        generation={"resolved_model": "snapshot-1", "usage": None, "latency_seconds": None},
        provenance={"task_sha256": "task-one"})
    metadata = {"run_id": run_id, "run_status": "completed", "pending_tasks": [],
                "selected_tasks": ["one"], "dataset_sha256": "dataset", "evaluator_sha256": "evaluator",
                "compiler": {"name": "fixture", "version": "1"},
                "platform": {"system": "fixture", "machine": "x64", "release": "1"},
                "static_analysis": {"engine": "builtin", "cppcheck_version": None},
                "generation_settings": {"temperature": 0.5, "max_tokens": 100, "request_timeout_seconds": 60}}
    metadata["scoring_policy"] = {"formula_version": SCORING_FORMULA_VERSION, "safety_error_penalty": SAFETY_ERROR_PENALTY, "safety_warning_penalty": SAFETY_WARNING_PENALTY, "tasks": {"one": {"weights": asdict(result.weights), "limits": asdict(result.limits)}}}
    return BenchmarkReporter.to_json_dict([result], model, metadata)


class TestAggregation(unittest.TestCase):
    def test_repeat_samples_include_failures_and_use_sample_standard_deviation(self):
        summary = aggregate_runs([fixture_report(score, str(i)) for i, score in enumerate((100, 0, 50))])
        group = summary["groups"][0]
        self.assertEqual(group["runs"], 3)
        self.assertEqual(group["statistics"]["score"], {"mean": 50, "sample_stddev": 50})
        self.assertEqual(group["task_success"][0]["passed_runs"], 1)
        self.assertAlmostEqual(group["task_success"][0]["pass_rate_pct"], 100/3, places=5)
        self.assertIsNone(group["total_tokens"])
        json.loads(render_aggregation(summary, "json"))
        rows = list(csv.DictReader(io.StringIO(render_aggregation(summary, "csv"))))
        self.assertEqual(rows[0]["score_sample_stddev"], "50.0")

    def test_single_sample_usage_and_duration_coverage_are_explicit(self):
        report = fixture_report()
        report["tasks"][0]["generation"].update(usage={"total_tokens": 37}, latency_seconds=0.25)
        summary = aggregate_runs([report])
        group = summary["groups"][0]
        self.assertIsNone(group["statistics"]["score"]["sample_stddev"])
        self.assertEqual((group["total_tokens"], group["usage_tasks"], group["duration_tasks"]), (37, 1, 1))
        self.assertIn("Unavailable (one run)", render_aggregation(summary))
        self.assertIn("not Pass@k", render_aggregation(summary, "cli"))

    def test_duplicates_legacy_and_unfinished_runs_are_rejected(self):
        report = fixture_report()
        with self.assertRaisesRegex(ValueError, "Duplicate run_id"):
            aggregate_runs([report, copy.deepcopy(report)])
        for mutate in (lambda data: data["metadata"].pop("run_id"),
                       lambda data: data["metadata"].update(run_status="interrupted"),
                       lambda data: data["metadata"].pop("dataset_sha256")):
            invalid = copy.deepcopy(report)
            mutate(invalid)
            with self.assertRaises(ValueError):
                aggregate_runs([invalid])

    def test_incompatible_evaluation_inputs_rejected_and_resolved_identities_separate(self):
        first, second = fixture_report(), fixture_report(run_id="run-2")
        second["metadata"]["dataset_sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "dataset_sha256"):
            aggregate_runs([first, second])
        second["metadata"]["dataset_sha256"] = "dataset"
        second["tasks"][0]["generation"]["resolved_model"] = "snapshot-2"
        groups = aggregate_runs([first, second])["groups"]
        self.assertEqual([group["resolved_model"] for group in groups], ["snapshot-1", "snapshot-2"])
        self.assertEqual([group["runs"] for group in groups], [1, 1])

    def test_cross_compiler_paths_do_not_split_equivalent_aggregate_samples(self):
        first, second = fixture_report(), fixture_report(run_id="run-2")
        for report, compiler, stamp in (
                (first, "/opt/arm/releases/13/bin/cc1", [1234, 100]),
                (second, r"C:\Toolchain\versions\13\gcc.exe", [1234, 200])):
            footprint = {"target": "arm:cortex-m0", "compiler": compiler,
                         "compiler_name": "arm-none-eabi-gcc",
                         "version": "arm-none-eabi-gcc 13.2.1",
                         "flags": ["-mcpu=cortex-m0", "-mthumb"], "stamp": stamp,
                         "measurement": "target-object"}
            report["metadata"]["compiler"]["target"] = footprint
            report["metadata"]["execution_settings"] = {"isolation": "process", "footprint": footprint}

        summary = aggregate_runs([first, second])
        self.assertEqual(summary["groups"][0]["runs"], 2)
        self.assertEqual(second["metadata"]["compiler"]["target"]["stamp"], [1234, 200])

    def test_generation_configurations_are_named_separate_groups(self):
        first, second = fixture_report(), fixture_report(run_id="run-2")
        second["metadata"]["generation_settings"]["temperature"] = 1
        groups = aggregate_runs([first, second])["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual({group["generation_settings"]["temperature"] for group in groups}, {0.5, 1})

    def test_failed_generation_remains_in_denominator_with_visible_identity_inference(self):
        success, failed = fixture_report(), fixture_report(0, "run-2")
        task = failed["tasks"][0]
        task.update(compiled=False, generation={"resolved_model": None}, error_log="API request failed")
        task["test_result"].update(total=0, passed=0, failed=0, completed=False, returncode=None)
        task["scores"] = dict.fromkeys(("functional", "memory", "safety", "total"), 0)
        summary = aggregate_runs([success, failed])
        self.assertEqual(summary["groups"][0]["runs"], 2)
        self.assertEqual(summary["groups"][0]["statistics"]["score"]["mean"], 50)
        self.assertTrue(any("identity is inferred" in warning for warning in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
