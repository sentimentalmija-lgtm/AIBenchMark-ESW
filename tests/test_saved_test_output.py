import unittest

from test_aggregation import fixture_report

from aibenchmark_esw.metrics.exports import report_html
from aibenchmark_esw.metrics.reporter import BenchmarkReporter


class TestSavedTestOutput(unittest.TestCase):
    def test_test_output_survives_saved_report_and_offline_html(self):
        report = fixture_report()
        results = BenchmarkReporter.from_json_dict(report)
        results[0].test_result.output = 'All tests passed: audit diagnostic'
        saved = BenchmarkReporter.to_json_dict(results, report['model_name'], report['metadata'])
        restored = BenchmarkReporter.from_json_dict(saved)
        self.assertEqual(restored[0].test_result.output, results[0].test_result.output)
        self.assertIn('audit diagnostic', report_html(restored, report['model_name']))

    def test_output_is_bounded_without_splitting_utf8_and_truncation_survives_resave(self):
        report = fixture_report()
        results = BenchmarkReporter.from_json_dict(report)
        results[0].test_result.output = '가' * 30000
        saved = BenchmarkReporter.to_json_dict(results, report['model_name'], report['metadata'])
        tests = saved['tasks'][0]['test_result']
        self.assertLessEqual(len(tests['output'].encode('utf-8')), 65536)
        self.assertTrue(tests['output_truncated'])
        restored = BenchmarkReporter.from_json_dict(saved)
        again = BenchmarkReporter.to_json_dict(restored, report['model_name'], report['metadata'])
        self.assertEqual(again['tasks'][0]['test_result'], tests)

    def test_legacy_reports_without_output_fields_still_load(self):
        report = fixture_report()
        tests = report['tasks'][0]['test_result']
        tests.pop('output', None)
        tests.pop('output_truncated', None)
        self.assertEqual(BenchmarkReporter.from_json_dict(report)[0].test_result.output, '')

    def test_saved_output_shape_and_byte_limit_are_validated(self):
        for values in ({'output': 1}, {'output': '가' * 30000}, {'output_truncated': 'yes'}):
            with self.subTest(values=values):
                report = fixture_report()
                report['tasks'][0]['test_result'].update(values)
                with self.assertRaises(ValueError):
                    BenchmarkReporter.from_json_dict(report)
