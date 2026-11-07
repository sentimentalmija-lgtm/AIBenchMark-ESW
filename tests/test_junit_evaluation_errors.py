import io
import json
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from test_aggregation import fixture_report
from test_collection_exports import collection

from aibenchmark_esw import cli
from aibenchmark_esw.metrics.junit import render_collection_junit, render_junit
from aibenchmark_esw.metrics.reporter import BenchmarkReporter

UNITY_OUTPUT = 'test.c:10:test_pass:PASS\ntest.c:20:test_ignored:IGNORE\n2 Tests 0 Failures 1 Ignored\n'
PIPELINE_ERRORS = ('Candidate object compilation failed', 'measurement unavailable', 'Reference validation failed')


def ignored_report(run_id='ignored', error=None, output=UNITY_OUTPUT):
    report = fixture_report(50, run_id=run_id)
    task = report['tasks'][0]
    task['test_result'].update(failed=0, ignored=1, returncode=0, output=output)
    task['error_log'] = output or 'Tests did not complete successfully'
    if error:
        task['error_log'] += '\nMemory analysis failed: ' + error
        task['size_metrics']['measured'] = False
        task['scores'].update(memory=0, total=40)
        report['overall_score'] = 40
    return report


def single_junit(report):
    return render_junit(BenchmarkReporter.from_json_dict(report), report['model_name'], report['metadata'])


class TestJUnitEvaluationErrors(unittest.TestCase):
    def assert_outcomes(self, document, errors, skipped):
        suite = ET.fromstring(document)
        self.assertEqual(suite.get('errors'), str(errors))
        self.assertEqual(suite.get('skipped'), str(skipped))
        self.assertEqual(suite.get('failures'), '0')
        for error in suite.findall('testcase/error'):
            self.assertEqual(error.get('type'), 'evaluation')
            self.assertIn('Memory analysis failed:', error.text)
        for skip in suite.findall('testcase/skipped'):
            self.assertEqual(skip.get('type'), 'ignored')
        return suite

    def test_ignored_unity_output_alone_remains_skipped(self):
        for output in ('', UNITY_OUTPUT, 'diagnostic: Memory analysis failed\n' + UNITY_OUTPUT):
            with self.subTest(output=output):
                self.assert_outcomes(single_junit(ignored_report(output=output)), errors=0, skipped=1)

    def test_ignored_cases_do_not_hide_object_size_or_reference_errors(self):
        for error in PIPELINE_ERRORS:
            with self.subTest(error=error):
                suite = self.assert_outcomes(single_junit(ignored_report(error=error)), errors=1, skipped=0)
                self.assertIn(error, suite.findtext('testcase/error'))

    def test_collection_preserves_each_samples_error_or_skip(self):
        samples = [ignored_report()] + [ignored_report(str(index), error)
                                       for index, error in enumerate(PIPELINE_ERRORS)]
        report = collection(samples, requested=len(samples))
        suite = self.assert_outcomes(render_collection_junit(report), errors=3, skipped=1)
        self.assertEqual(suite.get('tests'), '4')
        self.assertEqual({case.find("properties/property[@name='sample_run_id']").get('value')
                          for case in suite.findall('testcase')}, {'ignored', '0', '1', '2'})

    def test_offline_single_and_collection_regeneration_preserves_outcomes(self):
        samples = [ignored_report()] + [ignored_report(str(index), error)
                                       for index, error in enumerate(PIPELINE_ERRORS)]
        cases = [(sample, int(index > 0), int(index == 0), single_junit)
                 for index, sample in enumerate(samples)]
        cases.append((collection(samples, requested=len(samples)), 3, 1, render_collection_junit))
        with TemporaryDirectory() as directory:
            source, output = Path(directory) / 'report.json', Path(directory) / 'report.xml'
            for report, errors, skipped, renderer in cases:
                with self.subTest(run_id=report['metadata']['run_id']):
                    source.write_text(json.dumps(report), encoding='utf-8')
                    args = cli.build_parser().parse_args(['report', '--results', str(source),
                        '--format', 'junit', '--output', str(output)])
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.cmd_report(args), 0)
                    document = output.read_text(encoding='utf-8')
                    self.assert_outcomes(document, errors, skipped)
                    self.assertEqual(document, renderer(report))

    def test_saved_output_truncation_does_not_change_error_or_skip(self):
        for error in (None, PIPELINE_ERRORS[0]):
            with self.subTest(error=error):
                report = ignored_report(error=error)
                results = BenchmarkReporter.from_json_dict(report)
                results[0].test_result.output = 'diagnostic\n' * 7000 + UNITY_OUTPUT
                results[0].error_log = results[0].test_result.output
                if error:
                    results[0].error_log += '\nMemory analysis failed: ' + error
                direct = render_junit(results, report['model_name'], report['metadata'])
                saved = BenchmarkReporter.to_json_dict(results, report['model_name'], report['metadata'])
                self.assertTrue(saved['tasks'][0]['test_result']['output_truncated'])
                self.assert_outcomes(single_junit(saved), errors=int(error is not None), skipped=int(error is None))
                self.assertEqual(single_junit(saved), direct)
