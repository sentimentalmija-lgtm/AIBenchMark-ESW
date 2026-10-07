import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.provenance import collect_run_metadata, task_sha256, text_sha256, _checkout_provenance
from aibenchmark_esw.sandbox.executor import ExecutionSandbox
from aibenchmark_esw.sandbox.static_analyzer import StaticAnalyzer


class TestProvenance(unittest.TestCase):
    def setUp(self):
        self.task = DatasetLoader().get_task("tier1_crc16")
        self.executor = ExecutionSandbox()

    def test_fingerprint_ignores_checkout_location_and_line_endings(self):
        expected = task_sha256(self.task, self.executor.unity_dir)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copied = root / "task"
            unity = root / "unity"
            shutil.copytree(self.task.task_dir, copied)
            shutil.copytree(self.executor.unity_dir, unity)
            for path in list(copied.rglob("*")) + list(unity.glob("*")):
                if path.is_file():
                    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(task_sha256(replace(self.task, task_dir=copied), unity), expected)
            (copied / "tests/build").mkdir()
            (copied / "tests/build/generated.c").write_text("generated", encoding="utf-8")
            self.assertEqual(task_sha256(replace(self.task, task_dir=copied), unity), expected)

    def test_configuration_source_and_harness_changes_change_fingerprints(self):
        expected = task_sha256(self.task, self.executor.unity_dir)
        self.assertNotEqual(expected, task_sha256(replace(self.task, target_standard="c11"), self.executor.unity_dir))
        self.assertNotEqual(expected, task_sha256(replace(self.task, limits=replace(
            self.task.limits, max_ram_bytes=129)), self.executor.unity_dir))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copied = root / "task"
            unity = root / "unity"
            shutil.copytree(self.task.task_dir, copied)
            shutil.copytree(self.executor.unity_dir, unity)
            config = replace(self.task, task_dir=copied)
            (copied / "tests/test_crc16.c").write_text("changed tests", encoding="utf-8")
            self.assertNotEqual(expected, task_sha256(config, unity))
            (unity / "unity.c").write_text("changed harness", encoding="utf-8")
            self.assertNotEqual(task_sha256(self.task, self.executor.unity_dir), task_sha256(self.task, unity))

    def test_metadata_does_not_collect_environment_credentials(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "private-openai-key", "ANTHROPIC_API_KEY": "private-claude-key"}):
            metadata = collect_run_metadata([self.task], self.executor, {"temperature": None})
        encoded = json.dumps(metadata)
        self.assertNotIn("private-openai-key", encoded)
        self.assertNotIn("private-claude-key", encoded)
        self.assertEqual(metadata["compiler"]["name"], Path(self.executor.compiler_path).stem.lower())
        self.assertEqual(len(metadata["dataset_sha256"]), 64)
        self.assertEqual(len(metadata["evaluator_sha256"]), 64)
        self.assertEqual(metadata["selected_tasks"], ["tier1_crc16"])

    def test_candidate_hash_normalizes_newlines(self):
        self.assertEqual(text_sha256("int x;\r\n"), text_sha256("int x;\n"))

    def test_metadata_records_selected_static_analyzer_and_version(self):
        analyzer = StaticAnalyzer("fixture-cppcheck")
        def output(command):
            return "Cppcheck 2.fixture" if command[0] == "fixture-cppcheck" else None
        with patch("aibenchmark_esw.provenance._command_output", side_effect=output):
            metadata = collect_run_metadata([self.task], self.executor, static_analyzer=analyzer)
        self.assertEqual(metadata["static_analysis"],
                         {"engine": "builtin+cppcheck", "cppcheck_version": "Cppcheck 2.fixture", "configuration": analyzer.configuration()})
        self.assertEqual(metadata["execution_settings"]["static_analysis_timeout_seconds"],
                         analyzer.cppcheck_timeout_seconds)
        custom = StaticAnalyzer("fixture-cppcheck", cppcheck_timeout_seconds=12.5)
        with patch("aibenchmark_esw.provenance._command_output", side_effect=output):
            metadata = collect_run_metadata([self.task], self.executor, static_analyzer=custom)
        self.assertEqual(metadata["execution_settings"]["static_analysis_timeout_seconds"], 12.5)
        self.assertEqual(metadata["static_analysis"]["configuration"]["cppcheck_timeout_seconds"], 12.5)
        with patch("aibenchmark_esw.sandbox.static_analyzer.shutil.which", return_value=None):
            metadata = collect_run_metadata([self.task], self.executor, static_analyzer=StaticAnalyzer())
        self.assertEqual(metadata["static_analysis"], {"engine": "builtin", "cppcheck_version": None, "configuration": analyzer.configuration()})

    def test_programmatic_prompt_overrides_change_effective_task_identity(self):
        altered = replace(self.task, prompt_overrides={"allow_dynamic_memory": True})
        self.assertNotEqual(task_sha256(self.task, self.executor.unity_dir),
                            task_sha256(altered, self.executor.unity_dir))

    def test_foreign_checkout_cannot_claim_the_benchmark_source_revision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "aibenchmark_esw"
            package.mkdir()
            (root / ".git").mkdir()
            (root / "pyproject.toml").write_text('[project]\nname = "other-project"\n', encoding="utf-8")
            with patch("aibenchmark_esw.provenance._command_output") as command:
                self.assertEqual(_checkout_provenance(package), (None, None))
                command.assert_not_called()
            (root / "pyproject.toml").write_text('[project]\nname = "aibenchmark-esw"\n', encoding="utf-8")
            with patch("aibenchmark_esw.provenance._command_output", side_effect=[str(root), "commit", ""]):
                self.assertEqual(_checkout_provenance(package), ("commit", False))
            # A nested directory must not inherit an ancestor repository's HEAD.
            with patch("aibenchmark_esw.provenance._command_output", return_value=str(root.parent)):
                self.assertEqual(_checkout_provenance(package), (None, None))

    def test_nonstandard_include_changes_real_test_outcome_and_comparison_fingerprint(self):
        from aibenchmark_esw.evaluation import evaluate_task
        from aibenchmark_esw.metrics.comparison import compare_runs
        from aibenchmark_esw.metrics.reporter import BenchmarkReporter
        from aibenchmark_esw.models import TaskConfig
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("src", "reference", "include", "tests"):
                (root / folder).mkdir()
            reference = "int add_one(int x) { return x+1; }\n"
            candidate = "int add_one(int x) { return 42; }\n"
            (root / "include/api.h").write_text("int add_one(int x);\n", encoding="utf-8")
            (root / "src/solution.c").write_text(candidate, encoding="utf-8")
            (root / "reference/solution.c").write_text(reference, encoding="utf-8")
            (root / "tests/test_answer.c").write_text(
                '#include "unity.h"\n#include "api.h"\n#include "cases.inc"\n'
                'void setUp(void) {}\nvoid tearDown(void) {}\n'
                'void test_add(void) { TEST_ASSERT_EQUAL_INT(TEST_INPUT+1, add_one(TEST_INPUT)); }\n'
                'int main(void) { UNITY_BEGIN(); RUN_TEST(test_add); return UNITY_END(); }\n', encoding="utf-8")
            task = TaskConfig.from_dict({"id": "asset", "entry_file": "src/solution.c"}, root)
            analyzer = StaticAnalyzer("off")
            reports = []
            for value in (41, 42):
                (root / "tests/cases.inc").write_text(f"#define TEST_INPUT {value}\n", encoding="utf-8")
                metadata = collect_run_metadata([task], self.executor, static_analyzer=analyzer)
                result = evaluate_task(task, candidate, reference, str(value), self.executor, analyzer)
                self.assertTrue(result.compiled, result.error_log)
                result.provenance = {"task_sha256": metadata["task_fingerprints"][task.id]}
                reports.append(BenchmarkReporter.to_json_dict([result], str(value), metadata))
            self.assertEqual([report["pass_at_1_pct"] for report in reports], [100, 0])
            self.assertNotEqual(reports[0]["metadata"]["dataset_sha256"], reports[1]["metadata"]["dataset_sha256"])
            with self.assertRaisesRegex(ValueError, "fingerprints|dataset_sha256"):
                compare_runs(reports)

    def test_binary_fixture_bytes_are_hashed_without_text_newline_normalization(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task = replace(self.task, task_dir=root)
            fixture = root / "fixture.bin"
            fixture.write_bytes(b"\x00one\r\ntwo")
            original = task_sha256(task, self.executor.unity_dir)
            fixture.write_bytes(b"\x00one\ntwo")
            self.assertNotEqual(task_sha256(task, self.executor.unity_dir), original)

    def test_unreadable_asset_marks_provenance_incomplete_without_aborting_collection(self):
        original_read = Path.read_bytes
        def read(path):
            if path == self.task.task_dir / "prompt.md":
                raise PermissionError("unreadable prompt")
            return original_read(path)
        with patch.object(Path, "read_bytes", read):
            metadata = collect_run_metadata([self.task], self.executor, static_analyzer=StaticAnalyzer("off"))
        self.assertIsNone(metadata["dataset_sha256"])
        self.assertEqual(metadata["unreadable_assets"][self.task.id],
                         [{"asset": "task/prompt.md", "error": "PermissionError"}])
