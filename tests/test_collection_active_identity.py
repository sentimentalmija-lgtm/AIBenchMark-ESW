import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET

from test_aggregation import fixture_report
from test_collection_exports import collection

from aibenchmark_esw.metrics.reporter import BenchmarkReporter

MISSING = object()
INVALID_IDS = (('completed sample', 'done'), ('collection', 'collection'), ('missing', MISSING),
               ('empty', ''), ('whitespace', ' \t '), ('null', None), ('non-string', 42))


def active_collection(pending=False):
    active = fixture_report(0, 'active')
    active['metadata']['run_status'] = 'interrupted'
    if pending:
        active['metadata']['pending_tasks'] = ['one']
        task = active['tasks'][0]
        task.update(compiled=False, effective_standard=None)
        task['test_result'].update(total=0, passed=0, failed=0, completed=False, returncode=None)
        task['scores'] = dict.fromkeys(('functional', 'memory', 'safety', 'total'), 0)
    return collection([fixture_report(100, 'done')], active, 'interrupted')


def with_active_id(report, run_id):
    report = copy.deepcopy(report)
    metadata = report['sampling']['active_report']['metadata']
    if run_id is MISSING:
        metadata.pop('run_id')
    else:
        metadata['run_id'] = run_id
    return report


class TestCollectionActiveIdentity(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'aibenchmark_esw.cli', *map(str, arguments)],
                              capture_output=True, text=True, timeout=30)

    def test_reader_rejects_missing_invalid_and_conflicting_active_ids(self):
        report = active_collection()
        for name, run_id in INVALID_IDS:
            with self.subTest(identity=name), self.assertRaisesRegex(ValueError, 'active_report.*run_id'):
                BenchmarkReporter.from_json_dict(with_active_id(report, run_id))

    def test_cli_rejects_invalid_active_ids_before_replacing_outputs(self):
        report = active_collection()
        with TemporaryDirectory() as directory:
            source, output = Path(directory) / 'report.json', Path(directory) / 'export.txt'
            for name, run_id in INVALID_IDS:
                source.write_text(json.dumps(with_active_id(report, run_id)), encoding='utf-8')
                for format_name in ('validate-report', 'junit', 'github-summary'):
                    with self.subTest(identity=name, format=format_name):
                        output.write_bytes(b'previous export')
                        arguments = (['validate-report', source] if format_name == 'validate-report' else
                                     ['report', '--results', source, '--format', format_name, '--output', output])
                        process = self.run_cli(*arguments)
                        self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
                        self.assertIn('active_report', process.stderr)
                        self.assertIn('run_id', process.stderr)
                        self.assertNotIn('Traceback', process.stderr)
                        self.assertEqual(output.read_bytes(), b'previous export')

    def test_cli_preserves_valid_attempts_pending_slots_and_sample_identity(self):
        with TemporaryDirectory() as directory:
            source, output = Path(directory) / 'report.json', Path(directory) / 'export.txt'
            for pending in (False, True):
                with self.subTest(pending=pending):
                    source.write_text(json.dumps(active_collection(pending)), encoding='utf-8')
                    checked = self.run_cli('validate-report', source)
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    exported = self.run_cli('report', '--results', source, '--format', 'junit', '--output', output)
                    self.assertEqual(exported.returncode, 0, exported.stderr)
                    suite = ET.parse(output).getroot()
                    self.assertEqual(suite.get('tests'), '2')
                    self.assertEqual(suite.get('failures'), '0' if pending else '1')
                    self.assertEqual(suite.get('skipped'), '1' if pending else '0')
                    cases = suite.findall('testcase')
                    self.assertEqual(len({(case.get('classname'), case.get('name')) for case in cases}), 2)
                    self.assertEqual({case.find("properties/property[@name='sample_run_id']").get('value')
                                      for case in cases}, {'done', 'active'})
                    summary = self.run_cli('report', '--results', source, '--format', 'github-summary', '--output', output)
                    self.assertEqual(summary.returncode, 0, summary.stderr)
                    expected = '| one | 1/1 | 1 | 0 | 100.00 |' if pending else '| one | 1/2 | 0 | 0 | 50.00 |'
                    self.assertIn(expected, output.read_text(encoding='utf-8'))
