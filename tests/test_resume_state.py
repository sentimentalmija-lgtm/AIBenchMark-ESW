import argparse
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from aibenchmark_esw.resume import load_resume, validate_resume, merge_generation_history


def checkpoint():
    return {"schema_version": 2, "model_name": "provider/model", "metadata": {
        "run_id": "same-run", "run_status": "interrupted", "pending_tasks": ["one"],
        "selected_tasks": ["one"], "dataset_sha256": "dataset", "evaluator_sha256": "evaluator",
        "task_fingerprints": {"one": "task"}, "compiler": {"name": "tcc", "version": "1"},
        "static_analysis": {"engine": "builtin"}, "platform": {"system": "Windows", "release": "10", "machine": "x64"},
        "generation_settings": {"temperature": 0.7, "max_tokens": 512, "request_timeout_seconds": 10},
        "execution_settings": {"isolation": "process", "sanitizers": [], "max_output_bytes": 4096,
                               "static_analysis_timeout_seconds": 42.0},
        "run_options": {"compiler": "tcc", "allow_standard_fallback": False,
                        "static_analysis_timeout": 42.0}},
        "tasks": [{"task_id": "one", "tier": 1, "model_name": "provider/model", "compiled": False,
                   "test_result": {"total": 0, "passed": 0, "failed": 0, "completed": False, "all_passed": False},
                   "size_metrics": {"measured": False}, "safety_metrics": {},
                   "scores": {"functional": 0, "memory": 0, "safety": 0, "total": 0}, "execution_time_sec": 0}]}


class TestResumeState(unittest.TestCase):
    def test_restore_model_options_and_default_in_place_output_without_mutating_saved_identity(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "resume.json"
            path.write_text(json.dumps(checkpoint()), encoding="utf-8")
            args = argparse.Namespace(resume=path, model="baseline", output=None, jobs=4, _explicit_options=set())
            report = load_resume(args)
            self.assertEqual((args.model, args.temperature, args.max_tokens, args.request_timeout),
                             ("provider/model", 0.7, 512, 10))
            self.assertEqual((args.tasks, args.isolation, args.max_output_bytes), ("one", "process", 4096))
            self.assertEqual(args.static_analysis_timeout, 42.0)
            self.assertEqual((args.output, args.jobs), (path, 4))
            self.assertEqual(report["metadata"]["run_id"], "same-run")

    def test_explicit_conflicts_fail_but_output_and_jobs_are_adjustable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "resume.json"
            path.write_text(json.dumps(checkpoint()), encoding="utf-8")
            for option, value in (("model", "different"), ("temperature", 1), ("tasks", "other"), ("tier", 2)):
                args = argparse.Namespace(resume=path, _explicit_options={"--" + option})
                setattr(args, option, value)
                with self.subTest(option=option), self.assertRaises(ValueError):
                    load_resume(args)
            args = argparse.Namespace(resume=path, output="another.json", jobs=8,
                                      _explicit_options={"--output", "--jobs"})
            load_resume(args)
            self.assertEqual((args.output, args.jobs), ("another.json", 8))
            args = argparse.Namespace(resume=path, static_analysis_timeout=30.0,
                                      _explicit_options={"--static-analysis-timeout"})
            with self.assertRaisesRegex(ValueError, "static-analysis-timeout"):
                load_resume(args)

    def test_fingerprint_toolchain_runtime_and_settings_must_match_before_resume(self):
        report = checkpoint()
        fresh = copy.deepcopy(report["metadata"])
        tasks = [SimpleNamespace(id="one")]
        validate_resume(report, fresh, tasks)
        for key, value in (("dataset_sha256", "changed"), ("evaluator_sha256", "changed"),
                           ("compiler", {"name": "gcc"}), ("static_analysis", {"engine": "other"}),
                           ("execution_settings", {"isolation": "native"}),
                           ("generation_settings", {"temperature": 1})):
            altered = copy.deepcopy(fresh)
            altered[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_resume(report, altered, tasks)
        with self.assertRaisesRegex(ValueError, "every saved task"):
            validate_resume(report, fresh, [])

    def test_history_accumulates_usage_once_and_exposes_partial_coverage(self):
        def generation(tokens, latency):
            return {"usage": {"total_tokens": tokens}, "latency_seconds": latency, "resolved_model": "snapshot"}
        first, second, third = generation(10, 1), generation(20, 2), generation(30, 3)
        merged = merge_generation_history(merge_generation_history(first, second), third)
        self.assertEqual(merged["usage"]["total_tokens"], 60)
        self.assertEqual(merged["latency_seconds"], 6)
        self.assertEqual([attempt["usage"]["total_tokens"] for attempt in merged["history"]], [10, 20])
        self.assertEqual(first["usage"]["total_tokens"], 10)
        incomplete = merge_generation_history(first, generation(None, 2))
        self.assertIsNone(incomplete["usage"]["total_tokens"])
        self.assertEqual(incomplete["known_usage"]["total_tokens"], 10)
        self.assertEqual(incomplete["usage_coverage"]["total_tokens"], {"known_attempts": 1, "total_attempts": 2})
        self.assertEqual(merge_generation_history(None, third), third)
        self.assertEqual(merge_generation_history(first, None), first)
