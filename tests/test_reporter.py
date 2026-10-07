import unittest

from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.models import (
    TaskEvaluationResult, TaskWeights, TestResult, SizeMetrics,
    StaticSafetyMetrics, DimensionScores,
)


class TestReporter(unittest.TestCase):
    def result(self, task_id, weights, effective_standard="c99"):
        return TaskEvaluationResult(
            task_id, 1, "test", True,
            TestResult(total_tests=1, passed_tests=1, passed=True, completed=True),
            SizeMetrics(100, 0, 100, 0), StaticSafetyMetrics(),
            DimensionScores(100, 100, 100, 100), 0.1,
            weights=weights, target_standard="c99", effective_standard=effective_standard,
        )

    def test_nondefault_uniform_weights_are_displayed(self):
        results = [self.result("one", TaskWeights(0.7, 0.1, 0.2))]
        markdown = BenchmarkReporter.generate_markdown(results, "test")
        self.assertIn("| **Functional Correctness** | 100.00 / 100 | 70% |", markdown)
        self.assertIn("| **Memory Efficiency** | 100.00 / 100 | 10% |", markdown)
        self.assertIn("70%/10%/20%", BenchmarkReporter.generate_cli_table(results, "test"))

    def test_malformed_metadata_is_rejected_and_absent_metadata_can_render(self):
        malformed = [
            lambda data: data.update(metadata=[]),
            lambda data: data.update(metadata={"compiler": []}),
            lambda data: data.update(metadata={"platform": "Windows"}),
            lambda data: data.update(metadata={"generation_settings": []}),
            lambda data: data["tasks"].__setitem__(0, None),
            lambda data: data["tasks"][0].update(generation=[]),
            lambda data: data["tasks"][0].update(provenance="hash"),
            lambda data: data["tasks"][0].update(generation={"usage": []}),
        ]
        for mutate in malformed:
            with self.subTest(mutation=mutate):
                data = BenchmarkReporter.to_json_dict([self.result("one", TaskWeights())], "test")
                mutate(data)
                with self.assertRaises(ValueError):
                    BenchmarkReporter.from_json_dict(data)
        results = [self.result("one", TaskWeights())]
        markdown = BenchmarkReporter.generate_markdown(results, "test", {"compiler": None})
        self.assertIn("**Compiler**: Unknown", markdown)

    def test_provenance_round_trip_and_markdown(self):
        result = self.result("one", TaskWeights())
        result.provenance = {"task_sha256": "task-digest", "candidate_sha256": "candidate-digest"}
        metadata = {"compiler": {"name": "tcc", "version": "tcc 0.9.27", "optimization": "native"},
                    "dataset_sha256": "dataset-digest", "source_revision": "source-commit"}
        data = BenchmarkReporter.to_json_dict([result], "test", metadata)
        self.assertEqual(data["metadata"], metadata)
        self.assertEqual(BenchmarkReporter.from_json_dict(data)[0].provenance, result.provenance)
        markdown = BenchmarkReporter.generate_markdown([result], "test", metadata)
        self.assertIn("dataset-digest", markdown)
        self.assertIn("tcc 0.9.27", markdown)

    def test_static_analyzer_fallback_survives_round_trip_and_is_visible(self):
        result = self.result("one", TaskWeights())
        result.safety_metrics.cppcheck_status = "failed"
        result.safety_metrics.cppcheck_diagnostic = "tool unavailable"
        data = BenchmarkReporter.to_json_dict([result], "test")
        restored = BenchmarkReporter.from_json_dict(data)
        self.assertEqual(restored[0].safety_metrics.cppcheck_diagnostic, "tool unavailable")
        self.assertIn("cppcheck failed", BenchmarkReporter.generate_markdown(restored, "test"))
        self.assertIn("cppcheck failed", BenchmarkReporter.generate_cli_table(restored, "test"))

    def test_cppcheck_timeout_is_preserved_and_warns_about_reduced_coverage(self):
        result = self.result("one", TaskWeights())
        result.safety_metrics.cppcheck_status = "timeout"
        result.safety_metrics.cppcheck_diagnostic = "Command 'cppcheck' timed out after 30 seconds"
        restored = BenchmarkReporter.from_json_dict(
            BenchmarkReporter.to_json_dict([result], "test"))
        self.assertEqual(restored[0].safety_metrics.cppcheck_status, "timeout")
        for output in (BenchmarkReporter.generate_markdown(restored, "test"),
                       BenchmarkReporter.generate_cli_table(restored, "test")):
            self.assertIn("cppcheck timed out", output)
            self.assertIn("safety score uses built-in rules only", output)

    def test_mixed_weights_round_trip_and_display_per_task(self):
        results = [self.result("one", TaskWeights()),
                   self.result("two", TaskWeights(0.8, 0.1, 0.1))]
        data = BenchmarkReporter.to_json_dict(results, "test")
        self.assertEqual(data["tasks"][1]["weights"]["functional"], 0.8)
        restored = BenchmarkReporter.from_json_dict(data)
        markdown = BenchmarkReporter.generate_markdown(restored, "test")
        self.assertIn("Varies by task", markdown)
        self.assertIn("60%/20%/20%", markdown)
        self.assertIn("80%/10%/10%", markdown)

    def test_legacy_weights_are_unknown_instead_of_assumed(self):
        data = BenchmarkReporter.to_json_dict([self.result("one", TaskWeights())], "test")
        del data["tasks"][0]["weights"]
        results = BenchmarkReporter.from_json_dict(data)
        markdown = BenchmarkReporter.generate_markdown(results, "test")
        self.assertIn("Unknown (legacy report)", markdown)
        self.assertNotIn("60%", markdown)

    def test_actual_standard_is_preserved_and_visible(self):
        results = [self.result("one", TaskWeights(), effective_standard="c11")]
        restored = BenchmarkReporter.from_json_dict(BenchmarkReporter.to_json_dict(results, "test"))
        self.assertEqual(restored[0].target_standard, "c99")
        self.assertEqual(restored[0].effective_standard, "c11")
        self.assertIn("c99->c11", BenchmarkReporter.generate_markdown(restored, "test"))
        self.assertIn("c99->c11", BenchmarkReporter.generate_cli_table(restored, "test"))

    def test_incomplete_or_uncompiled_results_do_not_count_toward_pass_at_1(self):
        for compiled, completed in ((True, False), (False, True)):
            with self.subTest(compiled=compiled, completed=completed):
                result = self.result("one", TaskWeights())
                result.compiled = compiled
                result.test_result.completed = completed
                self.assertEqual(BenchmarkReporter.to_json_dict([result], "test")["pass_at_1_pct"], 0)
                self.assertIn("Pass@1: 0.0%", BenchmarkReporter.generate_cli_table([result], "test"))
                self.assertIn("**Pass@1 (All Tests Passed)**: 0/1 (0.0%)",
                              BenchmarkReporter.generate_markdown([result], "test"))


if __name__ == "__main__":
    unittest.main()
