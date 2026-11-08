"""Offline contracts for action identity, evidence gates, and dataset authoring."""

import copy
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_consumer_action import runner

from aibenchmark_esw import cli
from aibenchmark_esw.baseline_check import current_fingerprints, verify_baseline
from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.evaluation import failed_evaluation
from aibenchmark_esw.metrics.github_summary import github_summary
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.models import CATEGORIES, TaskConfig
from aibenchmark_esw.mutations import generate_mutations
from aibenchmark_esw.sandbox.executor import ExecutionSandbox
from aibenchmark_esw.task_variants import prepare_task_variants


class TestCIIssueFixes(unittest.TestCase):
    def test_action_reserves_identity_flags_and_assignment_spellings(self):
        module = runner()
        for option in ('--model', '--tasks', '--task', '--tier', '--category', '--tasks-root', '--solution',
                       '--reference', '--output', '--junit-output', '--resume', '--save-solutions', '--compiler',
                       '--jobs', '--dry-run', '--help'):
            for spelling in ([option, 'value'], [option + '=value']):
                with self.subTest(spelling=spelling), self.assertRaises(ValueError):
                    module.build_command({'INPUT_EXTRA_ARGUMENTS': json.dumps(spelling)})
        command, _ = module.build_command({'INPUT_EXTRA_ARGUMENTS': '["--max-tokens", "100", "--repair-turns", "1"]'})
        self.assertIn('--repair-turns', command)

    def test_negative_signal_status_and_failed_run_summary_are_preserved(self):
        module = runner()
        report = json.loads(Path('results/baseline.json').read_text(encoding='utf-8'))
        report['metadata']['run_status'] = 'aborted'
        report['metadata']['pending_tasks'] = [report['tasks'][0]['task_id']]
        results = BenchmarkReporter.from_json_dict(json.loads(Path('results/baseline.json').read_text(encoding='utf-8')))
        results[0] = failed_evaluation(DatasetLoader().get_task(results[0].task_id), report['model_name'], 'Not evaluated')
        report = BenchmarkReporter.to_json_dict(results, report['model_name'], report['metadata'])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'benchmark.junit.xml').write_text('Previous invocation', encoding='utf-8')
            outputs, summary = root / 'outputs.txt', root / 'summary.md'

            def interrupted_run(command):
                Path(command[command.index('--output') + 1]).write_text(json.dumps(report), encoding='utf-8')
                return subprocess.CompletedProcess(command, -9)

            with patch.dict(os.environ, {'INPUT_OUTPUT_DIRECTORY': str(root), 'GITHUB_OUTPUT': str(outputs),
                                         'GITHUB_STEP_SUMMARY': str(summary)}), patch.object(sys, 'argv', ['runner', 'benchmark']), \
                    patch.object(module.subprocess, 'run', side_effect=interrupted_run):
                self.assertEqual(module.main(), 137)
            self.assertIn('exit-code=137', outputs.read_text(encoding='utf-8'))
            self.assertIn('raw-exit-code=-9', outputs.read_text(encoding='utf-8'))
            self.assertIn('Run status: aborted', summary.read_text(encoding='utf-8'))
            paths = dict(line.split('=', 1) for line in outputs.read_text(encoding='utf-8').splitlines())
            self.assertTrue(Path(paths['json']).is_file())
            self.assertFalse(Path(paths['junit']).exists())
            self.assertEqual((root / 'benchmark.junit.xml').read_text(encoding='utf-8'), 'Previous invocation')

    def test_github_summary_cli_handles_collections_and_escape_labels(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'samples.json'
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_run(cli.build_parser().parse_args(['run', '--tasks', 'tier1_crc16',
                    '--samples', '2', '--output', str(path)])), 0)
                output = Path(directory) / 'summary.md'
                self.assertEqual(cli.cmd_report(cli.build_parser().parse_args(['report', '--results', str(path),
                    '--format', 'github-summary', '--output', str(output)])), 0)
            self.assertIn('tier1_crc16', output.read_text(encoding='utf-8'))
        report = json.loads(Path('results/baseline.json').read_text(encoding='utf-8'))
        report['model_name'] = '<img src=x>|`model`'
        for task in report['tasks']:
            task['model_name'] = report['model_name']
        summary = github_summary(report)
        self.assertIn('&lt;img', summary)
        self.assertNotIn('<img', summary)
        self.assertIn('\\|', summary)

    def test_baseline_checks_score_completion_dirty_state_and_subset(self):
        report = json.loads(Path('results/baseline.json').read_text(encoding='utf-8'))
        report['metadata'].update(current_fingerprints(), source_dirty=False)
        verify_baseline(report)
        dirty = copy.deepcopy(report)
        dirty['metadata']['source_dirty'] = True
        with self.assertRaisesRegex(ValueError, 'dirty'):
            verify_baseline(dirty)
        verify_baseline(dirty, allow_dirty=True)
        bad = copy.deepcopy(report)
        bad['tasks'][0]['error_log'] = 'measurement failed'
        with self.assertRaisesRegex(ValueError, 'score regression'):
            verify_baseline(bad)
        bad = copy.deepcopy(report)
        bad['tasks'][0]['scores'].update(safety=97, total=99.4)
        bad['tasks'][0]['safety_metrics']['warning_count'] = 1
        for field in ('tier_weighted_score', 'tier_weight_policy', 'by_tier', 'by_category'):
            bad.pop(field, None)
        with self.assertRaisesRegex(ValueError, 'regression'):
            verify_baseline(bad)
        subset = copy.deepcopy(report)
        subset['tasks'] = subset['tasks'][:1]
        selected = [subset['tasks'][0]['task_id']]
        subset['metadata']['selected_tasks'] = selected
        subset['metadata']['scoring_policy']['tasks'] = {task_id: subset['metadata']['scoring_policy']['tasks'][task_id] for task_id in selected}
        subset.pop('generation_summary', None)
        for field in ('tier_weighted_score', 'tier_weight_policy', 'by_tier', 'by_category'):
            subset.pop(field, None)
        subset['metadata'].update(current_fingerprints(task_ids=selected))
        verify_baseline(subset, reference=report)

    def test_numeric_mutations_preserve_octal_and_negative_syntax(self):
        source = 'int f(int a){return a-0 + 010U;}'
        mutants = generate_mutations(source, max_mutants=50)
        replacements = {m.replacements[0] for m in mutants}
        self.assertIn(('0', '(-1)'), replacements)
        self.assertIn(('010U', '07U'), replacements)
        self.assertIn(('010U', '011U'), replacements)
        self.assertFalse(any('a--1' in m.apply(source) for m in mutants))
        with TemporaryDirectory() as directory:
            loader = DatasetLoader()
            task = loader.get_task('tier1_crc16')
            # Compile numeric probes as actual candidate objects, not token-only assertions.
            for index, mutation in enumerate(m for m in mutants if m.name.endswith(('increment', 'decrement'))):
                result = ExecutionSandbox().compile_object(task, mutation.apply(source), Path(directory) / str(index), 'candidate')
                self.assertTrue(result.success, result.output)

    def test_crc_heldout_variant_conflict_rejected_before_mutation(self):
        loader = DatasetLoader()
        task = loader.get_task('tier1_crc16')
        original = loader.get_reference_solution(task.id)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            suite = root / task.id / 'tests'
            suite.mkdir(parents=True)
            (suite / 'test_crc.c').write_text('int main(void){return 0;}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'polynomial'), prepare_task_variants([task], 1, root):
                pass
        self.assertEqual(loader.get_reference_solution(task.id), original)

    def test_category_vocabulary_and_descriptions_are_consistent(self):
        loader = DatasetLoader()
        for task in loader.list_tasks():
            self.assertIn(task.category, CATEGORIES)
            self.assertNotEqual(task.description, task.name)
        data = json.loads((loader.get_task('tier1_crc16').task_dir / 'task.json').read_text(encoding='utf-8'))
        data['category'] = 'typo-category'
        with self.assertRaisesRegex(ValueError, 'category must be'):
            TaskConfig.from_dict(data, Path('.'))
        self.assertEqual(loader.get_task('tier3_spi_flash').category, 'device_drivers')
        self.assertEqual(loader.get_task('tier4_uart_frame_fix').category, 'bug_fix_and_safety')


if __name__ == '__main__':
    unittest.main()
