import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aibenchmark_esw import cli
from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.output_paths import validate_output_paths


class TestOutputPaths(unittest.TestCase):
    def test_existing_output_scan_fails_closed_when_protected_directory_cannot_be_scanned(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "tasks"
            root.mkdir()
            output = Path(directory) / "report.json"
            output.write_text("keep", encoding="utf-8")
            with patch("aibenchmark_esw.output_paths.os.scandir", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ValueError, "Unable to scan protected input directory"):
                    validate_output_paths([output], protected_roots=[root])
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_existing_output_scan_does_not_follow_directory_symlink_cycles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "tasks"
            root.mkdir()
            output = Path(directory) / "report.json"
            output.write_text("keep", encoding="utf-8")
            try:
                (root / "cycle").symlink_to(root, target_is_directory=True)
            except OSError:
                self.skipTest("Symbolic link creation requires OS support or privileges")
            script = (
                "import sys; from pathlib import Path; "
                "from aibenchmark_esw.output_paths import validate_output_paths; "
                "validate_output_paths([Path(sys.argv[1])], protected_roots=[Path(sys.argv[2])])"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(output), str(root)],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_existing_output_scan_protects_file_symlink_aliases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "tasks"
            root.mkdir()
            source = Path(directory) / "reference.c"
            source.write_text("keep", encoding="utf-8")
            try:
                (root / "alias.c").symlink_to(source)
            except OSError:
                self.skipTest("Symbolic link creation requires OS support or privileges")
            with self.assertRaisesRegex(ValueError, "overwrite an input"):
                validate_output_paths([source], protected_roots=[root])

    def test_eval_cannot_replace_its_source_or_hard_link(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "candidate.c"
            source.write_text(DatasetLoader().get_reference_solution("tier1_crc16"), encoding="utf-8")
            alias = Path(directory) / "alias.json"
            os.link(source, alias)
            original = source.read_bytes()
            for output in (source, source.parent / "." / source.name, alias):
                args = cli.build_parser().parse_args(["eval", "--task", "tier1_crc16",
                    "--solution", str(source), "--output", str(output)])
                with self.subTest(output=output), patch.object(cli, "evaluate_task") as evaluate:
                    with self.assertRaisesRegex(ValueError, "overwrite an input"):
                        cli.cmd_eval(args)
                    evaluate.assert_not_called()
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(alias.read_bytes(), original)

    def test_benchmark_inputs_and_new_files_inside_roots_are_protected(self):
        loader = DatasetLoader()
        for root in cli._input_roots(loader):
            output = root / "must-not-be-created.json"
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, "benchmark inputs"):
                validate_output_paths([output], protected_roots=cli._input_roots(loader))
            self.assertFalse(output.exists())
        with TemporaryDirectory() as directory:
            root = Path(directory) / "tasks"
            root.mkdir()
            source = root / "reference.c"
            source.write_text("original", encoding="utf-8")
            alias = Path(directory) / "alias.c"
            os.link(source, alias)
            with self.assertRaisesRegex(ValueError, "overwrite an input"):
                validate_output_paths([alias], protected_roots=[root])
            self.assertEqual(source.read_text(), "original")

    def test_run_rejects_collisions_and_invalid_save_directory_before_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "file"
            existing.write_text("keep", encoding="utf-8")
            cases = [
                ["--save-solutions", directory, "--output", str(root / "tier1_crc16.c")],
                ["--save-solutions", str(existing)],
                ["--save-solutions", str(DatasetLoader().tasks_root)],
            ]
            for flags in cases:
                args = cli.build_parser().parse_args(["run", "--model", "mock",
                    "--tasks", "tier1_crc16", *flags])
                with self.subTest(flags=flags), patch.object(cli.LLMClient, "generate_solution") as generate:
                    with self.assertRaises((ValueError, OSError)), redirect_stdout(io.StringIO()):
                        cli.cmd_run(args)
                    generate.assert_not_called()
            self.assertEqual(existing.read_text(), "keep")
            self.assertFalse((root / "tier1_crc16.c").exists())

    def test_compare_cannot_overwrite_an_input_or_hard_link(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "one.json", root / "two.json"]
            data = json.loads(Path("results/baseline.json").read_text(encoding="utf-8"))
            for index, path in enumerate(paths):
                data["model_name"] = f"model-{index}"
                for task in data["tasks"]:
                    task["model_name"] = data["model_name"]
                path.write_text(json.dumps(data), encoding="utf-8")
            originals = [path.read_bytes() for path in paths]
            alias = root / "alias.md"
            os.link(paths[0], alias)
            for output in (*paths, alias):
                args = cli.build_parser().parse_args(["compare", "--results", *map(str, paths),
                                                     "--output", str(output)])
                with self.subTest(output=output), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(cli.cmd_compare(args), 1)
                self.assertIn("overwrite an input", errors.getvalue())
                self.assertEqual([path.read_bytes() for path in paths], originals)

    def test_existing_output_reports_can_still_be_replaced(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text("old output", encoding="utf-8")
            args = cli.build_parser().parse_args(["run", "--tasks", "tier1_crc16", "--output", str(output)])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_run(args), 0)
            self.assertEqual(json.loads(output.read_text())["overall_score"], 100)

    def test_symlinked_task_folders_and_dangling_input_aliases_are_protected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "tasks"
            root.mkdir()
            external = Path(directory) / "external"
            external.mkdir()
            (external / "task.json").write_text('{"id":"linked"}', encoding="utf-8")
            source = external / "reference.c"
            source.write_text("keep source", encoding="utf-8")
            dangling_target = Path(directory) / "missing.c"
            try:
                (root / "linked").symlink_to(external, target_is_directory=True)
                (external / "alias.c").symlink_to(dangling_target)
            except OSError:
                self.skipTest("Symbolic link creation requires OS support or privileges")
            for output in (source, root / "linked/reference.c", root / "linked/alias.c"):
                args = cli.build_parser().parse_args(["run", "--tasks-root", str(root),
                    "--model", "mock", "--output", str(output)])
                with self.subTest(output=output), patch.object(cli.LLMClient, "generate_solution") as generate:
                    with self.assertRaisesRegex(ValueError, "benchmark inputs"):
                        cli.cmd_run(args)
                    generate.assert_not_called()
            self.assertEqual(source.read_text(), "keep source")
            self.assertFalse(dangling_target.exists())
