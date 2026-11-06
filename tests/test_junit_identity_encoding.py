import io
import json
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from test_aggregation import fixture_report

from aibenchmark_esw.cli import build_parser, cmd_compare
from aibenchmark_esw.metrics.comparison import compare_runs
from aibenchmark_esw.metrics.exports import comparison_junit
from aibenchmark_esw.metrics.junit import render_junit
from aibenchmark_esw.metrics.reporter import BenchmarkReporter


class TestJUnitIdentityEncoding(unittest.TestCase):
    def test_offline_comparison_export_preserves_real_identities(self):
        with TemporaryDirectory() as directory:
            paths = [Path(directory) / f'{index}.json' for index in range(2)]
            for path, model, score in zip(paths, ('a\x00b', 'ab'), (100, 0)):
                path.write_text(json.dumps(fixture_report(score, model=model)), encoding='utf-8')
            output = Path(directory) / 'comparison.xml'
            args = build_parser().parse_args(['compare', '--results', *map(str, paths), '--format', 'junit',
                                              '--baseline-model', 'a\x00b', '--output', str(output)])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_compare(args), 0)
            suite = ET.parse(output).getroot()
            ids = [(case.get('classname'), case.get('name')) for case in suite.findall('testcase')]
            self.assertEqual(len(ids), len(set(ids)))

    def test_real_comparison_model_identities_do_not_collapse(self):
        reports = [fixture_report(100, 'a', 'a\x00b'), fixture_report(0, 'b', 'ab'),
                   fixture_report(100, 'c', 'a%000000b')]
        suite = ET.fromstring(comparison_junit(compare_runs(reports), 'a\x00b'))
        identities = [(case.get('classname'), case.get('name')) for case in suite.findall('testcase')]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(suite.get('failures'), '1')

    def test_real_task_identities_do_not_collapse_in_single_run_export(self):
        results = []
        for task_id in ('a\x00b', 'ab', 'a%000000b'):
            result = BenchmarkReporter.from_json_dict(fixture_report())[0]
            result.task_id = task_id
            results.append(result)
        suite = ET.fromstring(render_junit(results, 'normal/model'))
        identities = [(case.get('classname'), case.get('name')) for case in suite.findall('testcase')]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual({classname for classname, _ in identities}, {'normal/model'})
