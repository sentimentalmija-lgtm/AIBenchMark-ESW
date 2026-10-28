import unittest

from test_aggregation import fixture_report

from aibenchmark_esw.metrics.exports import report_html
from aibenchmark_esw.metrics.reporter import BenchmarkReporter


class TestHtmlEvaluationStatus(unittest.TestCase):
    def test_functional_pass_with_evaluation_error_has_error_status(self):
        report = fixture_report()
        report['tasks'][0]['error_log'] = 'Memory analysis failed: Object compiler failed'
        report['tasks'][0]['size_metrics']['measured'] = False
        report['tasks'][0]['scores'].update(memory=0, total=80)
        report['overall_score'] = 80
        html = report_html(BenchmarkReporter.from_json_dict(report), report['model_name'])
        self.assertIn('data-status="ERROR"', html)
        self.assertNotIn('data-status="PASS"', html)
        self.assertIn('<option>ERROR</option>', html)

    def test_clean_pass_failure_and_incomplete_statuses_are_preserved(self):
        for score, incomplete, expected in ((100, False, 'PASS'), (0, False, 'FAIL'), (0, True, 'INCOMPLETE')):
            with self.subTest(expected=expected):
                report = fixture_report(score)
                if incomplete:
                    report['tasks'][0]['test_result']['completed'] = False
                    report['tasks'][0]['scores'] = dict.fromkeys(('functional', 'memory', 'safety', 'total'), 0)
                html = report_html(BenchmarkReporter.from_json_dict(report), report['model_name'])
                self.assertIn(f'data-status="{expected}"', html)
