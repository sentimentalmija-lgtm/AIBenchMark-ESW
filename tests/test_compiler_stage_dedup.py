import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from compiler_tools import find_clang

from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.evaluation import _compiler_findings, evaluate_task
from aibenchmark_esw.models import (
    CompilationResult,
    SizeMetrics,
    StaticSafetyMetrics,
    TestResult,
)
from aibenchmark_esw.sandbox.executor import ExecutionSandbox
from aibenchmark_esw.sandbox.static_analyzer import StaticAnalyzer


class TestCompilerStageDedup(unittest.TestCase):
    def test_real_clang_warning_is_retained_once_across_both_compilations(self):
        compiler = find_clang()
        if not compiler:
            self.skipTest('Clang unavailable')
        loader = DatasetLoader()
        task = loader.get_task('tier1_crc16')
        reference = loader.get_reference_solution(task.id)
        source = 'static int audit_unused(void){return 0;}\n' + reference
        result = evaluate_task(task, source, reference, 'mock', ExecutionSandbox(compiler_path=compiler, warnings=True),
                               StaticAnalyzer(cppcheck_cmd='off'))
        self.assertTrue(result.test_result.passed, result.error_log)
        findings = [finding for finding in result.safety_metrics.findings
                    if finding['rule_id'] == 'compiler.-Wunused-function']
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0]['compilation_occurrences']), 2)

    def evaluate(self, object_message='unused variable', object_rule='compiler.-Wunused-variable'):
        warning = dict(rule_id='compiler.-Wunused-variable', engine='compiler', severity='warning',
                       message='unused variable', line=2, file='/tests/candidate.c', source_origin='candidate')
        other = dict(warning, file='/objects/candidate.c', message=object_message, rule_id=object_rule)
        executor = Mock(warnings=True, stack_usage=False, cross=None)
        executor.compile_and_test.return_value = (
            CompilationResult(True, '', effective_standard='c99', findings=[warning]),
            TestResult(total_tests=1, passed_tests=1, passed=True, completed=True, returncode=0))
        executor.compile_object.return_value = CompilationResult(True, '', binary_path=Path('object.o'), findings=[other])
        analyzer = Mock()
        analyzer.analyze.return_value = StaticSafetyMetrics(cppcheck_status='disabled')
        cache = Mock()
        cache.validate.return_value = (SimpleNamespace(error=None, flash_bytes=100, ram_bytes=0), False)
        with patch('aibenchmark_esw.evaluation.SizeAnalyzer.analyze', return_value=SizeMetrics(flash_bytes=100)):
            result = evaluate_task(DatasetLoader().get_task('tier1_crc16'), 'source', 'reference', 'mock', executor, analyzer, cache)
        return result, warning, other

    def test_repeated_candidate_warning_is_penalized_once_and_keeps_both_paths(self):
        result, warning, other = self.evaluate()
        self.assertEqual(result.safety_metrics.warning_count, 1)
        self.assertEqual(result.scores.safety_score, 97)
        self.assertEqual(result.scores.total_score, 99.4)
        occurrences = result.safety_metrics.findings[0]['compilation_occurrences']
        self.assertEqual({row['file'] for row in occurrences}, {warning['file'], other['file']})
        self.assertNotIn('compilation_occurrences', warning)
        from aibenchmark_esw.metrics.reporter import BenchmarkReporter
        saved = BenchmarkReporter.to_json_dict([result], 'mock')
        self.assertEqual(BenchmarkReporter.from_json_dict(saved)[0].safety_metrics.findings,
                         result.safety_metrics.findings)

    def test_different_messages_and_rules_at_the_same_line_are_not_merged(self):
        for message, rule in (('different warning', 'compiler.-Wunused-variable'),
                              ('unused variable', 'compiler.-Wconversion')):
            with self.subTest(message=message, rule=rule):
                result, _, _ = self.evaluate(message, rule)
                self.assertEqual(result.safety_metrics.warning_count, 2)
                self.assertEqual(result.scores.safety_score, 94)

    def test_ambiguous_origins_and_different_severities_remain_distinct(self):
        _, warning, other = self.evaluate()
        for change in ({'source_origin': 'unknown'}, {'severity': 'error'}):
            with self.subTest(change=change):
                findings = _compiler_findings([warning], [dict(other, **change)])
                self.assertEqual(len(findings), 2)
