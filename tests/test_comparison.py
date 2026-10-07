import copy
import csv
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from aibenchmark_esw import cli
from aibenchmark_esw.metrics.comparison import compare_runs, render_comparison


class TestComparison(unittest.TestCase):
    def report(self, name):
        data = json.loads(Path("results/baseline.json").read_text(encoding="utf-8"))
        data["model_name"] = name
        for task in data["tasks"]:
            task["model_name"] = name
            task["safety_metrics"]["cppcheck_status"] = "disabled"
        data["metadata"] = {
            "dataset_sha256": "same-dataset", "compiler": {"name": "tcc", "version": "0.9.27"},
            "evaluator_sha256": "same-evaluator",
            "static_analysis": {"engine": "builtin", "cppcheck_version": None},
            "platform": {"system": "Windows", "machine": "AMD64"},
            "generation_settings": {"temperature": None, "max_tokens": 4096, "request_timeout_seconds": 60},
        }
        return data

    def test_comparison_keeps_failed_tasks_in_denominator_and_recomputes_summaries(self):
        first, second = self.report("openai/mock"), self.report("anthropic/mock")
        second["tasks"][0]["compiled"] = False
        second["tasks"][0]["test_result"].update(all_passed=False, completed=False)
        second["tasks"][0]["scores"] = dict.fromkeys(("functional", "memory", "safety", "total"), 0)
        second["overall_score"] = 100  # Ignore stale or misleading stored summaries.
        comparison = compare_runs([first, second])
        self.assertEqual(comparison["models"][0]["model"], "openai/mock")
        count = len(first["tasks"])
        expected = round((count - 1) / count * 100, 2)
        self.assertEqual(comparison["models"][1]["score"], expected)
        self.assertEqual(comparison["models"][1]["pass_at_1_pct"], expected)
        self.assertEqual(len(comparison["task_ids"]), count)

    def test_incompatible_inputs_are_rejected(self):
        mutations = [
            lambda data: data["tasks"].pop(),
            lambda data: data["tasks"].append(copy.deepcopy(data["tasks"][0])),
            lambda data: data["metadata"].update(dataset_sha256="other-dataset"),
            lambda data: data["metadata"].update(evaluator_sha256="other-evaluator"),
            lambda data: data["metadata"]["compiler"].update(version="other-version"),
            lambda data: data["metadata"]["platform"].update(machine="other-architecture"),
            lambda data: data["metadata"]["generation_settings"].update(max_tokens=1),
            lambda data: data["metadata"]["generation_settings"].update(request_timeout_seconds=1),
            lambda data: data["tasks"][0].update(weights={"functional": 0.8, "memory": 0.1, "safety": 0.1}),
            lambda data: data["tasks"][0].update(effective_standard="c11"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                first, second = self.report("one"), self.report("two")
                mutate(second)
                with self.assertRaises(ValueError):
                    compare_runs([first, second])
        with self.assertRaises(ValueError):
            compare_runs([self.report("same"), self.report("same")])

    def test_cross_compiler_installation_paths_and_stamps_do_not_block_comparison(self):
        first, second = self.report("one"), self.report("two")
        first_target = {"target": "arm:cortex-m0", "compiler": "/opt/arm/releases/13/bin/arm-none-eabi-gcc",
                        "compiler_name": "arm-none-eabi-gcc",
                        "version": "arm-none-eabi-gcc 13.2.1", "flags": ["-mcpu=cortex-m0", "-mthumb"],
                        "stamp": [1234, 100], "measurement": "target-object"}
        second_target = {**first_target, "compiler": r"C:\Toolchain\versions\13\arm-none-eabi-gcc.exe",
                         "stamp": [1234, 200]}
        first["metadata"]["compiler"]["target"] = first_target
        second["metadata"]["compiler"]["target"] = second_target
        first["metadata"]["execution_settings"] = {"isolation": "process", "footprint": first_target}
        second["metadata"]["execution_settings"] = {"isolation": "process", "footprint": second_target}

        compare_runs([first, second])
        self.assertEqual(second["metadata"]["compiler"]["target"]["compiler"], second_target["compiler"])
        self.assertEqual(second["metadata"]["compiler"]["target"]["stamp"], [1234, 200])

        legacy_first, legacy_second = copy.deepcopy(first), copy.deepcopy(second)
        for report in (legacy_first, legacy_second):
            report["metadata"]["compiler"]["target"].pop("compiler_name")
        compare_runs([legacy_first, legacy_second])

        for field, value in (("target", "arm:cortex-m3"),
                             ("compiler_name", "arm-none-eabi-clang"),
                             ("version", "arm-none-eabi-gcc 14.1.0"),
                             ("flags", ["-mcpu=cortex-m3", "-mthumb"])):
            incompatible = copy.deepcopy(second)
            incompatible["metadata"]["compiler"]["target"][field] = value
            incompatible["metadata"]["execution_settings"]["footprint"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "compiler"):
                compare_runs([first, incompatible])

        unknown_version = copy.deepcopy(second)
        unknown_version["metadata"]["compiler"]["target"]["version"] = None
        unknown_version["metadata"]["execution_settings"]["footprint"]["version"] = None
        with self.assertRaisesRegex(ValueError, "compiler"):
            compare_runs([first, unknown_version])

    def test_task_fingerprint_mismatch_is_rejected_even_without_run_provenance(self):
        first, second = self.report("one"), self.report("two")
        first["tasks"][0]["provenance"] = {"task_sha256": "one"}
        second["tasks"][0]["provenance"] = {"task_sha256": "two"}
        first.pop("metadata")
        second.pop("metadata")
        with self.assertRaisesRegex(ValueError, "fingerprints"):
            compare_runs([first, second])

    def test_legacy_missing_provenance_and_partial_usage_are_visible(self):
        first, second = self.report("one"), self.report("two")
        first.pop("metadata")
        first["tasks"][0]["generation"] = {"usage": {"total_tokens": 50}, "latency_seconds": 1.25}
        comparison = compare_runs([first, second])
        markdown = render_comparison(comparison)
        self.assertIn("compatibility cannot be fully checked", markdown)
        self.assertIn(f"50 (1/{len(first['tasks'])} tasks)", markdown)
        self.assertIn(f"1.25 (1/{len(first['tasks'])} tasks)", markdown)
        self.assertIn("Unknown", markdown)

    def test_csv_round_trip_and_cli_output(self):
        first, second = self.report("one"), self.report("two")
        comparison = compare_runs([first, second])
        rows = list(csv.DictReader(io.StringIO(render_comparison(comparison, "csv"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["score"], "100.0")
        self.assertIn("Pass@1", render_comparison(comparison, "cli"))
        with TemporaryDirectory() as directory:
            files = [Path(directory) / f"{index}.json" for index in range(2)]
            for path, report in zip(files, (first, second)):
                path.write_text(json.dumps(report), encoding="utf-8")
            output = Path(directory) / "comparison.csv"
            args = cli.build_parser().parse_args(["compare", "--results", *map(str, files),
                                                 "--format", "csv", "--output", str(output)])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_compare(args), 0)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(output.read_text(encoding="utf-8"))))), 2)
            files[1].write_text("{}", encoding="utf-8")
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.cmd_compare(args), 1)
            self.assertIn("Cannot compare reports", errors.getvalue())

    def test_static_analysis_configuration_must_match_and_fallback_is_visible(self):
        first, second = self.report("one"), self.report("two")
        second["metadata"]["static_analysis"] = {"engine": "builtin+cppcheck", "cppcheck_version": "2.fixture"}
        with self.assertRaisesRegex(ValueError, "static_analysis"):
            compare_runs([first, second])
        first["metadata"]["static_analysis"] = dict(second["metadata"]["static_analysis"])
        second["metadata"]["static_analysis"]["cppcheck_version"] = "other-version"
        with self.assertRaisesRegex(ValueError, "static_analysis"):
            compare_runs([first, second])
        second["metadata"]["static_analysis"] = dict(first["metadata"]["static_analysis"])
        for report in (first, second):
            for task in report["tasks"]:
                task["safety_metrics"]["cppcheck_status"] = "completed"
        second["tasks"][0]["safety_metrics"].update(cppcheck_status="failed", cppcheck_diagnostic="tool timed out")
        comparison = compare_runs([first, second])
        self.assertIn("cppcheck failed", render_comparison(comparison))
        row = next(row for row in comparison["models"] if row["model"] == "two")
        self.assertEqual(row["cppcheck_failed_tasks"], 1)
        second["metadata"].pop("static_analysis")
        self.assertIn("static-analysis configuration/version is unknown", render_comparison(compare_runs([first, second])))

    def test_baseline_or_missing_generation_settings_have_named_warning(self):
        first, second = self.report("baseline"), self.report("generated")
        first["metadata"]["generation_settings"] = None
        comparison = compare_runs([first, second])
        for format_name in ("cli", "markdown"):
            rendered = render_comparison(comparison, format_name)
            self.assertIn("baseline: generation settings", rendered)
            self.assertIn("local/baseline", rendered)
        second["metadata"].pop("generation_settings")
        self.assertTrue(any("generated: generation settings" in warning
                            for warning in compare_runs([first, second])["warnings"]))

    def test_os_release_and_execution_settings_are_compared_when_recorded(self):
        first, second = self.report("one"), self.report("two")
        first["metadata"]["platform"]["release"] = "old"
        second["metadata"]["platform"]["release"] = "new"
        with self.assertRaisesRegex(ValueError, "release"):
            compare_runs([first, second])
        second["metadata"]["platform"].pop("release")
        self.assertTrue(any("release is unknown" in warning for warning in compare_runs([first, second])["warnings"]))
        first["metadata"]["execution_settings"] = {"isolation": "process"}
        second["metadata"]["execution_settings"] = {"isolation": "native"}
        with self.assertRaisesRegex(ValueError, "execution_settings"):
            compare_runs([first, second])

    def test_generation_strategy_defaults_and_system_prompt_identity(self):
        first, second = self.report("one"), self.report("two")
        second["metadata"]["generation_settings"].update(prompt_strategy="single", review_turn=False)
        compare_runs([first, second])
        second["metadata"]["generation_settings"]["prompt_strategy"] = "plan"
        with self.assertRaisesRegex(ValueError, "prompt strategy"):
            compare_runs([first, second])
        second["metadata"]["generation_settings"]["prompt_strategy"] = "single"
        second["metadata"]["generation_settings"]["system_prompt_sha256"] = "custom"
        with self.assertRaisesRegex(ValueError, "prompt strategy"):
            compare_runs([first, second])

    def test_known_unreadable_input_provenance_is_rejected(self):
        first, second = self.report("one"), self.report("two")
        first["metadata"]["unreadable_assets"] = {"one": [{"asset": "task/prompt.md", "error": "PermissionError"}]}
        with self.assertRaisesRegex(ValueError, "unreadable"):
            compare_runs([first, second])
