import copy
import csv
import io
import json
import unittest

from test_aggregation import fixture_report
from test_collection_exports import collection

from aibenchmark_esw.metrics.trend import render_trend, trend_runs
from aibenchmark_esw.sampling import sampling_statistics


def trend_collection(run_id, score, models, day):
    samples = [fixture_report(score, f'{run_id}-{index}') for index in range(len(models))]
    for sample, model in zip(samples, models):
        sample['tasks'][0]['generation']['resolved_model'] = model
    report = collection(samples, requested=len(samples))
    report['sampling']['pass_k'] = [1]
    report['sampling']['statistics'] = sampling_statistics(samples, [1])
    report['metadata'].update(run_id=run_id, created_at_utc=f'2026-10-0{day}T00:00:00Z')
    return report


class TestTrendSamples(unittest.TestCase):
    def test_sample_count_changes_do_not_hide_score_regressions(self):
        old = trend_collection('old', 100, ['A', 'A'], 1)
        for count in (2, 3, 4):
            with self.subTest(samples=count):
                new = trend_collection('new', 0, ['A'] * count, 2)
                rows = trend_runs([new, old])['groups'][0]['runs']
                self.assertEqual([row['score'] for row in rows], [100, 0])
                self.assertEqual(rows[1]['regressions'], [
                    {'task_id': 'one', 'delta': -100.0, 'previous_run_id': 'old'}])

    def test_single_run_and_collection_share_matching_model_history(self):
        old = fixture_report(100, 'old')
        old['metadata']['created_at_utc'] = '2026-10-01T00:00:00Z'
        new = trend_collection('new', 0, ['snapshot-1'] * 3, 2)
        rows = trend_runs([old, new])['groups'][0]['runs']
        self.assertEqual(rows[1]['regressions'], [
            {'task_id': 'one', 'delta': -100.0, 'previous_run_id': 'old'}])

    def test_matching_model_proportions_compare_but_changed_mixes_do_not(self):
        old = trend_collection('old', 100, ['A', 'A', 'B'], 1)
        new = trend_collection('new', 0, ['B', 'A', 'A', 'B', 'A', 'A'], 2)
        changed = trend_collection('changed', 0, ['A', 'B', 'B', 'A', 'B', 'B'], 3)
        rows = trend_runs([changed, new, old])['groups'][0]['runs']
        self.assertEqual(rows[1]['regressions'], [
            {'task_id': 'one', 'delta': -100.0, 'previous_run_id': 'old'}])
        self.assertEqual(rows[2]['regressions'], [])
        self.assertEqual(rows[1]['resolved_model_sequences_by_task'],
                         {'one': [['A'], ['A'], ['A'], ['A'], ['B'], ['B']]})

    def test_model_assignment_to_tasks_still_matters_across_sample_counts(self):
        reports = [trend_collection('old', 100, ['A', 'A'], 1),
                   trend_collection('new', 0, ['A', 'A', 'A'], 2)]
        for report in reports:
            for source in [report, *report['samples']]:
                second = copy.deepcopy(source['tasks'][0])
                second['task_id'] = 'two'
                second['provenance']['task_sha256'] = 'task-two'
                second['generation']['resolved_model'] = 'B'
                source['tasks'].append(second)
                source['metadata']['selected_tasks'] = ['one', 'two']
                source['metadata']['scoring_policy']['tasks']['two'] = copy.deepcopy(
                    source['metadata']['scoring_policy']['tasks']['one'])
            report['sampling']['statistics'] = sampling_statistics(report['samples'], [1])
        rows = trend_runs(reports)['groups'][0]['runs']
        self.assertEqual({row['task_id'] for row in rows[1]['regressions']}, {'one', 'two'})
        self.assertEqual(rows[1]['task_attempts'], 6)
        for source in [reports[1], *reports[1]['samples']]:
            for task in source['tasks']:
                task['generation']['resolved_model'] = 'B' if task['task_id'] == 'one' else 'A'
        self.assertEqual(trend_runs(reports)['groups'][0]['runs'][1]['regressions'], [])

    def test_generation_turn_order_and_repetition_remain_model_identity(self):
        old = trend_collection('old', 100, ['B', 'B'], 1)
        new = trend_collection('new', 0, ['B', 'B', 'B'], 2)
        for report in (old, new):
            for source in [report, *report['samples']]:
                source['tasks'][0]['generation']['turns'] = [
                    {'resolved_model': 'A'}, {'resolved_model': 'B'}]
        self.assertTrue(trend_runs([old, new])['groups'][0]['runs'][1]['regressions'])
        for sequence in (['B', 'A'], ['A', 'B', 'B']):
            with self.subTest(sequence=sequence):
                for source in [new, *new['samples']]:
                    source['tasks'][0]['generation']['resolved_model'] = sequence[-1]
                    source['tasks'][0]['generation']['turns'] = [
                        {'resolved_model': model} for model in sequence]
                self.assertEqual(trend_runs([old, new])['groups'][0]['runs'][1]['regressions'], [])

    def test_exports_include_sample_coverage_and_keep_all_raw_sequences(self):
        report = trend_collection('run', 100, ['A', 'A', 'A'], 1)
        summary = trend_runs([report])
        row = json.loads(render_trend(summary, 'json'))['groups'][0]['runs'][0]
        self.assertEqual((row['sample_count'], row['task_attempts']), (3, 3))
        self.assertEqual(row['resolved_model_sequences_by_task'], {'one': [['A'], ['A'], ['A']]})
        exported = list(csv.DictReader(io.StringIO(render_trend(summary, 'csv'))))[0]
        self.assertEqual((exported['sample_count'], exported['task_attempts']), ('3', '3'))
        for format_name in ('markdown', 'cli'):
            self.assertIn('| Samples | Task attempts |', render_trend(summary, format_name))
        self.assertIn('sample_count', render_trend(summary, 'html'))
