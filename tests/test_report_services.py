import copy
import json
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from test_aggregation import fixture_report
from aibenchmark_esw.metrics.aggregation import aggregate_runs, render_aggregation
from aibenchmark_esw.metrics.comparison import compare_runs, render_comparison
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.metrics.statistics import score_statistics


class ReportIntegrityTests(unittest.TestCase):
    def test_empty_collection_can_resume_but_cannot_be_compared(self):
        checkpoint = fixture_report(run_id='wrapper')
        checkpoint['samples'] = []
        checkpoint['sampling'] = {'requested': 1, 'completed': 0, 'pending': [0], 'pass_k': [1], 'statistics': {}}
        checkpoint['metadata']['run_status'] = 'interrupted'
        self.assertEqual(len(BenchmarkReporter.from_json_dict(checkpoint)), 1)
        with self.assertRaisesRegex(ValueError, 'without completed samples'):
            compare_runs([checkpoint, fixture_report(model='other')])
        self.assertEqual(score_statistics([]),
                         {'mean': None, 'sample_stddev': None, 'mean_ci95_approx': None})

    def test_provenance_shape_checked_before_policy_use(self):
        for value in ('bad', [1], 1):
            report = fixture_report()
            report['tasks'][0]['provenance'] = value
            with self.assertRaisesRegex(ValueError, 'Task provenance must be an object'):
                BenchmarkReporter.from_json_dict(report)

    def test_new_generation_metadata_is_shape_checked(self):
        mutations=[{'known_usage':'bad'},{'usage_coverage':['bad']},{'turns':[{'usage':'bad'}]},
                   {'usage_complete':'yes'},{'cost_complete':'yes'}]
        for mutation in mutations:
            report=fixture_report()
            report['tasks'][0]['generation'].update(mutation)
            with self.assertRaises(ValueError):
                BenchmarkReporter.from_json_dict(report)

    def test_unknown_subobject_fields_raise_value_error(self):
        for field in ('size_metrics', 'safety_metrics', 'weights', 'limits'):
            report = fixture_report()
            report['tasks'][0][field]['bogus'] = 1
            with self.assertRaisesRegex(ValueError, 'Unknown'):
                BenchmarkReporter.from_json_dict(report)

    def test_finding_counts_must_cover_each_scored_severity(self):
        for severity in ('error', 'warning', 'style', 'performance', 'portability'):
            report = fixture_report()
            report['tasks'][0]['safety_metrics']['findings'] = [dict(rule_id='probe', engine='builtin', severity=severity, message='probe', file='candidate.c')]
            with self.assertRaisesRegex(ValueError, 'findings'):
                BenchmarkReporter.from_json_dict(report)

    def test_missing_finding_line_renders_unknown(self):
        report = fixture_report()
        task = report['tasks'][0]
        task['safety_metrics'].update(warning_count=1, findings=[dict(rule_id='probe', engine='builtin', severity='warning', message='probe', file='candidate.c')])
        task['scores'].update(safety=97, total=99.4)
        results = BenchmarkReporter.from_json_dict(report)
        for renderer in (BenchmarkReporter.generate_cli_table, BenchmarkReporter.generate_markdown):
            self.assertIn('candidate.c:?', renderer(results, report['model_name']))

    def test_posix_wrapped_failure_counts_portable_to_windows_reader(self):
        report = fixture_report(0)
        task = report['tasks'][0]
        task['test_result'].update(total=256, failed=256, returncode=0)
        BenchmarkReporter.from_json_dict(report)

    def test_unavailable_local_dataset_does_not_break_portable_reader(self):
        with patch('aibenchmark_esw.dataset.DatasetLoader', side_effect=ValueError('broken local task')):
            self.assertEqual(len(BenchmarkReporter.from_json_dict(fixture_report())), 1)

    def test_retry_settings_compare_and_group_with_legacy_defaults(self):
        first = fixture_report()
        for key, value in [('max_retries', 2), ('retry_backoff_seconds', 7), ('input_cost_per_million', 2), ('output_cost_per_million', 7)]:
            second = fixture_report(run_id='second', model='other')
            second['metadata']['generation_settings'][key] = value
            with self.assertRaisesRegex(ValueError, 'Incompatible'):
                compare_runs([first, second])
            second['model_name'] = second['tasks'][0]['model_name'] = first['model_name']
            groups = aggregate_runs([first, second])['groups']
            self.assertEqual(len(groups), 2)
            self.assertIn(key, groups[0]['generation_settings'])

    def test_partial_usage_is_subtotal_with_complete_coverage_zero(self):
        first, second = fixture_report(), fixture_report(run_id='other', model='other')
        first['tasks'][0]['generation'].update(usage={'total_tokens':37}, usage_complete=False)
        row = next(row for row in compare_runs([first, second])['models'] if row['model'] == first['model_name'])
        self.assertIsNone(row['total_tokens'])
        self.assertEqual((row['known_total_tokens'], row['usage_tasks'], row['usage_partial_tasks']), (37, 0, 1))
        group = aggregate_runs([first])['groups'][0]
        self.assertIsNone(group['total_tokens'])
        self.assertEqual(group['known_total_tokens'], 37)

    def test_mixed_turn_identity_separate_from_pure_final_model(self):
        first, second = fixture_report(), fixture_report(run_id='second')
        first['tasks'][0]['generation'].update(resolved_model='B', turns=[{'resolved_model':'A'}, {'resolved_model':'B'}])
        second['tasks'][0]['generation'].update(resolved_model='B', turns=[{'resolved_model':'B'}, {'resolved_model':'B'}])
        groups = aggregate_runs([first, second])['groups']
        self.assertEqual(len(groups), 2)
        self.assertEqual({tuple(group['resolved_model_sequence']) for group in groups}, {('A','B'), ('B','B')})

    def test_cross_compiler_path_and_stamp_are_informational(self):
        first, second = fixture_report(), fixture_report(model='other')
        for report, path, stamp in [(first,'C:/gcc/bin/arm-none-eabi-gcc.exe',[1,2]),(second,'/usr/bin/arm-none-eabi-gcc',[1,3])]:
            target = dict(compiler=path, stamp=stamp, version='gcc 13', target='arm', flags=['-Os'])
            report['metadata']['compiler']['target'] = target
            report['metadata']['execution_settings'] = {'footprint':target}
        self.assertEqual(len(compare_runs([first,second])['models']), 2)

    def test_samples_cannot_bypass_task_validation(self):
        report = fixture_report()
        report['samples'] = [copy.deepcopy(report)]
        report['samples'][0]['tasks'][0]['compiled'] = 'bad'
        with self.assertRaises(ValueError):
            BenchmarkReporter.from_json_dict(report)

    def test_schema_samples_recursive_and_compiler_findings(self):
        from aibenchmark_esw.report_schema import validate_report_structure
        report=fixture_report()
        report['samples']=[fixture_report(run_id='sample')]
        report['sampling']={'requested':1,'completed':1,'pending':[]}
        validate_report_structure(report)
        report['samples'][0]['tasks'][0]['size_metrics']['bogus']=1
        with self.assertRaises(ValueError):
            validate_report_structure(report)
        report=fixture_report()
        task=report['tasks'][0]
        task['safety_metrics'].update(warning_count=1,cppcheck_status='timeout',findings=[dict(rule_id='compiler.warning',engine='compiler',severity='warning',message='warning',file='candidate.c')])
        task['scores'].update(safety=97,total=99.4)
        validate_report_structure(report)
        BenchmarkReporter.from_json_dict(report)

    def test_cost_subtotals_do_not_appear_as_complete_spend(self):
        report=fixture_report()
        report['tasks'][0]['generation'].update(cost_usd=None,known_cost_usd=0.25,cost_complete=False)
        group=aggregate_runs([report])['groups'][0]
        self.assertIsNone(group['total_cost_usd'])
        self.assertIsNone(group['cost_per_passed_task'])
        self.assertEqual((group['known_cost_usd'],group['cost_partial_tasks']),(0.25,1))

    def test_all_text_exports_show_partial_coverage_and_spend(self):
        report=fixture_report()
        report['tasks'][0]['generation'].update(usage={'total_tokens':37},usage_complete=False,cost_usd=None,known_cost_usd=0.25,cost_complete=False)
        results=BenchmarkReporter.from_json_dict(report)
        for render in (BenchmarkReporter.generate_cli_table,BenchmarkReporter.generate_markdown):
            text=render(results,report['model_name'],report['metadata'])
            self.assertIn('known subtotal 37',text)
            self.assertIn('0.25',text)
        other=fixture_report(model='other')
        comparison=compare_runs([report,other])
        for format_name in ('cli','markdown'):
            self.assertIn('0.25',render_comparison(comparison,format_name))

    def test_sampling_consistency_and_expanded_aggregate(self):
        wrapper=fixture_report(run_id='wrapper')
        wrapper['samples']=[fixture_report(run_id='sample-0'),fixture_report(50,run_id='sample-1')]
        from aibenchmark_esw.sampling import sampling_statistics
        wrapper['sampling']={'requested':2,'completed':2,'pending':[],'pass_k':[1,2],
                             'statistics':sampling_statistics(wrapper['samples'], [1,2])}
        summary=aggregate_runs([wrapper])
        self.assertEqual(summary['groups'][0]['runs'],2)
        self.assertEqual(summary['groups'][0]['statistics']['score']['mean'],75)
        interval=summary['groups'][0]['statistics']['score']['mean_ci95_approx']
        self.assertEqual(interval['method'],'normal approximation; descriptive, small-sample uncertainty')
        self.assertLess(interval['lower'],75)
        self.assertGreater(interval['upper'],75)
        comparison=compare_runs([wrapper,fixture_report(model='other')])
        row=next(row for row in comparison['models'] if row['model']==wrapper['model_name'])
        self.assertEqual(row['score'],75)
        self.assertEqual(row['samples'],2)
        self.assertEqual(row['pass_at_k']['2'],100)
        self.assertAlmostEqual(row['score_sample_stddev'],35.355339,places=5)
        task=next(task for task in comparison['task_rows'] if task['model']==wrapper['model_name'])
        self.assertEqual(task['total'],75)
        self.assertIn('approximate 95%',render_comparison(comparison))
        invalid=copy.deepcopy(wrapper)
        invalid['sampling']['completed']=1
        with self.assertRaisesRegex(ValueError,'sampling'):
            BenchmarkReporter.from_json_dict(invalid)
        invalid=copy.deepcopy(wrapper)
        invalid['samples'][1]['metadata']['run_id']='sample-0'
        with self.assertRaisesRegex(ValueError,'sample'):
            BenchmarkReporter.from_json_dict(invalid)
        interrupted=copy.deepcopy(wrapper)
        interrupted['metadata']['run_status']='interrupted'
        interrupted['samples'].pop()
        interrupted['sampling'].update(completed=1,pending=[1])
        interrupted['sampling']['statistics'] = sampling_statistics(interrupted['samples'], [1,2])
        BenchmarkReporter.from_json_dict(interrupted)
        with self.assertRaisesRegex(ValueError,'completed'):
            aggregate_runs([interrupted])

    def test_unknown_turn_identity_not_inferred_as_pure_model(self):
        first,second=fixture_report(),fixture_report(run_id='second')
        first['tasks'][0]['generation'].update(resolved_model='B',turns=[{'resolved_model':None},{'resolved_model':'B'}])
        second['tasks'][0]['generation'].update(resolved_model='B',turns=[{'resolved_model':'B'},{'resolved_model':'B'}])
        groups=aggregate_runs([first,second])['groups']
        self.assertEqual(len(groups),2)
        self.assertIn([None,'B'],[group['resolved_model_sequence'] for group in groups])

    def test_known_usage_used_when_exact_fields_are_unknown(self):
        report=fixture_report()
        report['tasks'][0]['generation'].update(usage={'total_tokens':None},known_usage={'total_tokens':37},usage_complete=False)
        group=aggregate_runs([report])['groups'][0]
        self.assertEqual(group['known_total_tokens'],37)
        self.assertIsNone(group['total_tokens'])

    def test_usage_attempt_and_turn_coverage_retained(self):
        report=fixture_report()
        report['tasks'][0]['generation'].update(usage={'total_tokens':None},known_usage={'total_tokens':37},usage_complete=False,
            usage_coverage={'total_tokens':{'known_attempts':1,'total_attempts':3}},
            turns=[{'resolved_model':'A','usage':{'total_tokens':37}},{'resolved_model':'A','usage':None}])
        group=aggregate_runs([report])['groups'][0]
        self.assertEqual((group['usage_known_attempts'],group['usage_total_attempts']),(1,3))
        self.assertEqual((group['usage_known_turns'],group['usage_total_turns']),(1,2))
        self.assertIn('1/3 measured requests',render_aggregation(aggregate_runs([report])))
        self.assertIn('1/2 measured turns',render_aggregation(aggregate_runs([report]),'cli'))

    def test_serialization_exposes_cost_totals_and_reference_similarity_warning(self):
        report=fixture_report()
        report['tasks'][0]['generation'].update(cost_usd=.25,known_cost_usd=.25,cost_complete=True,reference_similarity_warning='Matches normalized public reference; this is not proof of contamination.')
        results=BenchmarkReporter.from_json_dict(report)
        saved=BenchmarkReporter.to_json_dict(results,report['model_name'],report['metadata'])
        self.assertEqual(saved['generation_summary']['total_cost_usd'],.25)
        for renderer in (BenchmarkReporter.generate_cli_table,BenchmarkReporter.generate_markdown,BenchmarkReporter.generate_html):
            self.assertIn('Matches normalized public reference',renderer(results,report['model_name'],report['metadata']))

    def test_collection_report_renders_sampling_and_full_spend(self):
        from aibenchmark_esw.metrics.measurements import measurement_summary
        report=fixture_report()
        results=BenchmarkReporter.from_json_dict(report)
        results[0].generation.update(cost_usd=.25,cost_complete=True)
        metadata=copy.deepcopy(report['metadata'])
        metadata['sampling']={'requested':2,'completed':2,'pending':[],'statistics':{'one':{'attempts':2,'passed':1,'mean_score':75,'sample_stddev':35.355,'approximate_95pct_mean_interval':[26,100],'pass_at_k':{'1':.5,'2':1}}}}
        metadata['collection_generation_summary']=measurement_summary([results[0],results[0]])
        for render in (BenchmarkReporter.generate_cli_table,BenchmarkReporter.generate_markdown,BenchmarkReporter.generate_html):
            text=render(results,report['model_name'],metadata)
            self.assertIn('first sample',text)
            self.assertIn('75',text)
            self.assertIn('0.5',text)
            self.assertIn('Pass@k',text)


class ExportServiceTests(unittest.TestCase):
    def test_html_escaped_interactive_and_complete(self):
        report = fixture_report(model='<script>alert(1)</script>')
        report['tasks'][0]['error_log'] = '</pre><script>bad()</script>'
        rendered = BenchmarkReporter.generate_html(BenchmarkReporter.from_json_dict(report), report['model_name'], report['metadata'])
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>alert', rendered)
        for marker in ('<!doctype html>', '<style>', '<script>', 'data-sort', 'data-status', 'id="tier"', 'id="category"', 'id="minscore"', '<details', 'dataset_sha256', 'Functional', 'Flash'):
            self.assertIn(marker, rendered)
        self.assertNotIn('src="http', rendered)
        comparison = compare_runs([fixture_report(),fixture_report(model='other')])
        self.assertIn('<table', render_comparison(comparison,'html'))
        self.assertIn('<table', render_aggregation(aggregate_runs([fixture_report()]),'html'))

    def test_sarif_rules_locations_and_unknown_line(self):
        from aibenchmark_esw.metrics.exports import generate_sarif
        report = fixture_report()
        report['tasks'][0]['safety_metrics'].update(warning_count=2, findings=[dict(rule_id='probe', engine='builtin', severity='warning', message='probe', file='dir/a b.c', line=3),dict(rule_id='probe', engine='builtin', severity='warning', message='unknown line', file='candidate.c')])
        report['tasks'][0]['scores'].update(safety=94,total=98.8)
        sarif = json.loads(generate_sarif(BenchmarkReporter.from_json_dict(report)))
        self.assertEqual(sarif['version'],'2.1.0')
        run=sarif['runs'][0]
        self.assertEqual(len(run['tool']['driver']['rules']),1)
        self.assertEqual(run['results'][0]['locations'][0]['physicalLocation']['region']['startLine'],3)
        self.assertNotIn('region',run['results'][1]['locations'][0]['physicalLocation'])

    def test_junit_comparison_regression_threshold_and_baseline(self):
        comparison=compare_runs([fixture_report(),fixture_report(50,model='other')])
        root=ElementTree.fromstring(render_comparison(comparison,'junit',baseline_model='provider/alias',regression_threshold=10))
        self.assertEqual(len(root.findall('.//testcase')),2)
        self.assertEqual(len(root.findall('.//failure')),1)
        self.assertIn('-50',root.find('.//failure').text)

    def test_trend_orders_timestamps_groups_fingerprints_and_flags_regressions(self):
        from aibenchmark_esw.metrics.trend import trend_runs, render_trend
        old,new,changed=fixture_report(run_id='old'),fixture_report(50,run_id='new'),fixture_report(run_id='changed')
        for report,time in [(old,'2026-01-01T00:00:00Z'),(new,'2026-01-02T00:00:00Z'),(changed,'2026-01-03T00:00:00Z')]:
            report['metadata']['created_at_utc']=time
        changed['metadata']['dataset_sha256']='changed'
        summary=trend_runs([new,changed,old],regression_threshold=10)
        self.assertEqual(len(summary['groups']),2)
        group=next(group for group in summary['groups'] if len(group['runs'])==2)
        self.assertEqual([run['run_id'] for run in group['runs']],['old','new'])
        self.assertEqual(group['runs'][1]['regressions'][0]['delta'],-50)
        self.assertIn('Regression',render_trend(summary,'markdown'))

    def test_trend_preserves_task_model_mapping_and_ignores_task_order(self):
        from aibenchmark_esw.metrics.trend import trend_runs
        old = fixture_report(run_id='old')
        second = copy.deepcopy(old['tasks'][0])
        second['task_id'] = 'two'
        second['provenance']['task_sha256'] = 'task-two'
        old['tasks'].append(second)
        old['metadata']['selected_tasks'] = ['one', 'two']
        old['metadata']['scoring_policy']['tasks']['two'] = copy.deepcopy(
            old['metadata']['scoring_policy']['tasks']['one'])
        old['tasks'][0]['generation']['resolved_model'] = 'A'
        old['tasks'][1]['generation']['resolved_model'] = 'B'
        new = copy.deepcopy(old)
        new['metadata']['run_id'] = 'new'
        failed = fixture_report(0)['tasks'][0]
        for key in ('test_result', 'scores'):
            new['tasks'][0][key] = failed[key]
        for report, day in ((old, 1), (new, 2)):
            report['metadata']['created_at_utc'] = f'2026-10-0{day}T00:00:00Z'
        new['tasks'].reverse()
        rows = trend_runs([old, new])['groups'][0]['runs']
        self.assertEqual(rows[1]['regressions'], [
            {'task_id': 'one', 'delta': -100.0, 'previous_run_id': 'old'}])
        for task in new['tasks']:
            task['generation']['resolved_model'] = 'B' if task['task_id'] == 'one' else 'A'
        rows = trend_runs([old, new])['groups'][0]['runs']
        self.assertEqual(rows[1]['regressions'], [])

    def test_trend_includes_later_sample_identities_and_preserves_multiplicity(self):
        from aibenchmark_esw.metrics.trend import trend_runs
        from aibenchmark_esw.sampling import sampling_statistics

        def collection(run_id, scores, models, day):
            samples = [fixture_report(score, run_id=f'{run_id}-{index}')
                       for index, score in enumerate(scores)]
            for sample, model in zip(samples, models):
                sample['tasks'][0]['generation']['resolved_model'] = model
            report = copy.deepcopy(samples[0])
            report['metadata'].update(run_id=run_id, created_at_utc=f'2026-10-0{day}T00:00:00Z')
            report['samples'] = samples
            report['sampling'] = {'requested': len(samples), 'completed': len(samples),
                                  'pending': [], 'pass_k': [1],
                                  'statistics': sampling_statistics(samples, [1])}
            return report

        old = collection('old', [100, 100, 100], ['A', 'A', 'B'], 1)
        reordered = collection('reordered', [100, 0, 0], ['B', 'A', 'A'], 2)
        changed = collection('changed', [100, 0, 0], ['A', 'B', 'B'], 3)
        rows = trend_runs([changed, reordered, old])['groups'][0]['runs']
        self.assertEqual(rows[1]['regressions'][0]['previous_run_id'], 'old')
        self.assertEqual(rows[2]['regressions'], [])
        self.assertEqual(rows[0]['resolved_model_sequences_by_task'],
                         {'one': [['A'], ['A'], ['B']]})
