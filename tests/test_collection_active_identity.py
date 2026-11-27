import copy
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from test_aggregation import fixture_report
from test_collection_exports import collection

from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.resume import load_resume
from aibenchmark_esw.sampling import run_samples

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
    def setup_resume(self, directory, saved, sidecar):
        saved['metadata']['created_at_utc'] = '2026-10-15T00:00:00Z'
        output = Path(directory) / 'report.json'
        output.write_text(json.dumps(saved), encoding='utf-8')
        child_path = Path(str(output) + '.samples') / 'sample-0002.json'
        child_path.parent.mkdir()
        child_path.write_text(json.dumps(sidecar), encoding='utf-8')
        args = SimpleNamespace(samples=2, pass_k=None, _explicit_options=set(), output=output, resume=output,
                               save_solutions=None, _sampling_task_ids=['one'], junit_output=None,
                               system_prompt_file=None)
        return args, output, child_path

    def test_resume_starts_unstarted_sample_despite_stale_completed_sidecar(self):
        saved = collection([fixture_report(100, 'done')], status='interrupted')
        with TemporaryDirectory() as directory:
            args, output, _ = self.setup_resume(directory, saved, fixture_report(100, 'old-campaign'))

            def run_once(child_args):
                self.assertIsNone(child_args.resume)
                child = fixture_report(100, 'fresh-sample')
                child_args.output.write_text(json.dumps(child), encoding='utf-8')
                child_args._sample_checkpoint_callback(child)
                return 0

            with redirect_stdout(io.StringIO()):
                self.assertEqual(run_samples(args, saved, run_once, []), 0)
            resumed = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual([sample['metadata']['run_id'] for sample in resumed['samples']], ['done', 'fresh-sample'])

    def test_resume_rejects_mismatched_active_sidecar_before_replacing_files(self):
        saved = active_collection()
        with TemporaryDirectory() as directory:
            args, output, child_path = self.setup_resume(directory, saved, fixture_report(100, 'old-campaign'))
            before = output.read_bytes(), child_path.read_bytes()

            def run_once(child_args):
                load_resume(child_args)
                self.fail('A mismatched active child was loaded')

            with self.assertRaisesRegex(ValueError, 'Active sample checkpoint.*run_id'):
                run_samples(args, saved, run_once, [])
            self.assertEqual((output.read_bytes(), child_path.read_bytes()), before)

    def test_resume_rejects_legacy_active_sidecar_without_a_recorded_id(self):
        saved = active_collection()
        sidecar = saved['sampling'].pop('active_report')
        with TemporaryDirectory() as directory:
            args, output, child_path = self.setup_resume(directory, saved, sidecar)
            before = output.read_bytes(), child_path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'no recorded run_id'):
                run_samples(args, saved, lambda _: self.fail('Unbound child was started'), [])
            self.assertEqual((output.read_bytes(), child_path.read_bytes()), before)

    def test_resume_recovers_matching_child_that_finished_before_wrapper(self):
        saved = active_collection()
        with TemporaryDirectory() as directory:
            args, output, child_path = self.setup_resume(directory, saved, fixture_report(100, 'active'))

            def run_once(child_args):
                self.assertEqual(child_args.resume, child_path)
                child = load_resume(child_args)
                child_args._sample_checkpoint_callback(child)
                intermediate = json.loads(output.read_text(encoding='utf-8'))['sampling']
                self.assertEqual(intermediate['active_sample'], 1)
                self.assertEqual(intermediate['active_report']['metadata']['run_id'], 'active')
                return 0

            with redirect_stdout(io.StringIO()):
                self.assertEqual(run_samples(args, saved, run_once, []), 0)
            resumed = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual([sample['metadata']['run_id'] for sample in resumed['samples']], ['done', 'active'])
        self.assertIsNone(resumed['sampling']['active_sample'])
        self.assertNotIn('active_report', resumed['sampling'])

    def test_resume_does_not_adopt_stale_sidecar_when_fresh_child_writes_nothing(self):
        saved = collection([fixture_report(100, 'done')], status='interrupted')
        with TemporaryDirectory() as directory:
            args, output, _ = self.setup_resume(directory, saved, fixture_report(100, 'old-campaign'))
            before = output.read_bytes()
            with self.assertRaisesRegex(ValueError, 'Sample did not produce a checkpoint'):
                run_samples(args, saved, lambda child_args: 1, [])
            self.assertEqual(output.read_bytes(), before)

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
