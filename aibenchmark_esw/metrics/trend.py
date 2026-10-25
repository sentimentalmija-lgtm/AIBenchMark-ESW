"""Chronological histories grouped by compatible grading conditions."""

import csv
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime

from aibenchmark_esw.metrics.comparison import compare_runs, generation_conditions
from aibenchmark_esw.metrics.measurements import generation_sequence, portable_settings
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.metrics.text import csv_safe_row, markdown_text


def trend_runs(reports, regression_threshold=0.0):
    if isinstance(regression_threshold, bool) or not isinstance(regression_threshold, (int, float)) or not math.isfinite(regression_threshold) or regression_threshold < 0:
        raise ValueError('Regression threshold must be a finite nonnegative score delta')
    groups = defaultdict(list)
    ids = set()
    for report in reports:
        results = BenchmarkReporter.from_json_dict(report)
        metadata = report.get('metadata') or {}
        run_id = metadata.get('run_id')
        if not isinstance(run_id, str) or not run_id.strip() or run_id in ids:
            raise ValueError('Trend requires distinct recorded run IDs')
        ids.add(run_id)
        timestamp = metadata.get('created_at_utc')
        if not isinstance(timestamp, str):
            raise ValueError('Trend requires an ISO timestamp with timezone')
        try:
            parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Timezone required')
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError('Trend requires an ISO timestamp with timezone') from error
        checked = compare_runs([report], _allow_repeated_models=True)
        if checked['models'][0]['provenance'] != 'Recorded':
            raise ValueError('Trend requires recorded dataset/evaluator/toolchain provenance')
        identity = {key: metadata.get(key) for key in ('dataset_sha256','evaluator_sha256','benchmark_version','compiler','static_analysis','execution_settings','scoring_policy','platform')}
        identity['tasks'] = {result.task_id: {'fingerprint': (result.provenance or {}).get('task_sha256'),
                                            'standard': result.target_standard, 'effective_standard': result.effective_standard,
                                            'target': result.footprint_target} for result in results}
        # Recorded platform versions that affect grading belong in the group;
        # Python/install paths, timestamps and source revisions remain history.
        platform = metadata.get('platform') or {}
        identity['platform'] = {key: platform.get(key) for key in ('system','machine','release')}
        signature = json.dumps(portable_settings(identity), sort_keys=True)
        groups[signature].append((parsed, report, checked))
    output = []
    for signature, members in sorted(groups.items()):
        members.sort(key=lambda item: (item[0], item[1]['metadata']['run_id']))
        previous: dict = {}
        rows = []
        for _, report, checked in members:
            metadata = report['metadata']
            # Keep every contributing sample in the exported evidence.
            samples = report.get('samples') or [report]
            task_sequences: dict[str, list] = defaultdict(list)
            for sample in samples:
                for result in sample['tasks']:
                    task_sequences[result['task_id']].append(generation_sequence(result.get('generation') or {}))
            task_sequences = {task_id: sorted(values, key=json.dumps)
                              for task_id, values in sorted(task_sequences.items())}
            sequences = sorted({sequence for values in task_sequences.values() for sequence in values}, key=json.dumps)
            # Compare per-task pipeline proportions independently of sample count.
            # Retain turn order and distinguish changed mixtures, including unknowns.
            task_profiles = {}
            for task_id, values in task_sequences.items():
                counts = Counter(values)
                divisor = math.gcd(*counts.values())
                task_profiles[task_id] = [(sequence, count // divisor) for sequence, count in
                                         sorted(counts.items(), key=lambda item: json.dumps(item[0]))]
            model_key = (report['model_name'], json.dumps(generation_conditions(metadata), sort_keys=True),
                         json.dumps(task_profiles, sort_keys=True))
            scores = {row['task_id']: row['total'] for row in checked['task_rows']}
            last = previous.get(model_key)
            regressions = []
            if last:
                for task_id, score in sorted(scores.items()):
                    delta = score - last['task_scores'][task_id]
                    if delta < -regression_threshold:
                        regressions.append({'task_id': task_id, 'delta': delta, 'previous_run_id':last['run_id']})
            row = {'run_id':metadata['run_id'], 'created_at_utc':metadata['created_at_utc'], 'model':report['model_name'],
                   'source_revision':metadata.get('source_revision'), 'score':checked['models'][0]['score'],
                   'sample_count': len(samples), 'task_attempts': sum(map(len, task_sequences.values())),
                   'task_scores':scores, 'regressions':regressions, 'generation_settings':generation_conditions(metadata),
                   'resolved_model_sequences': [list(sequence) for sequence in sequences],
                   'resolved_model_sequences_by_task': {task_id: [list(sequence) for sequence in values]
                                                       for task_id, values in task_sequences.items()}}
            previous[model_key] = row
            rows.append(row)
        output.append({'compatibility_sha256':hashlib.sha256(signature.encode()).hexdigest(),
                       'conditions':json.loads(signature), 'runs':rows})
    return {'schema_version':1, 'regression_threshold':regression_threshold, 'groups':output}


def render_trend(summary, format_name='markdown'):
    rows = [dict(compatibility_sha256=group['compatibility_sha256'], **run) for group in summary['groups'] for run in group['runs']]
    if format_name == 'json':
        return json.dumps(summary, indent=2, allow_nan=False) + '\n'
    if format_name == 'html':
        from aibenchmark_esw.metrics.exports import summary_html
        return summary_html('Benchmark history', rows, summary)
    if format_name == 'csv':
        stream=io.StringIO(newline='')
        fields=['compatibility_sha256','run_id','created_at_utc','model','source_revision','score','sample_count','task_attempts','task_scores','regressions','generation_settings','resolved_model_sequences','resolved_model_sequences_by_task']
        writer=csv.DictWriter(stream,fieldnames=fields,lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow(csv_safe_row({key:json.dumps(value,sort_keys=True) if isinstance(value,(dict,list)) else value for key,value in row.items()}))
        return stream.getvalue()
    if format_name not in ('markdown','cli'):
        raise ValueError('Unknown trend format: '+format_name)
    lines=['# Benchmark history' if format_name=='markdown' else 'Benchmark history', '',
           'Runs are compared only within matching dataset, evaluator and per-task model pipeline proportions.']
    for group in summary['groups']:
        lines += ['', 'Compatibility group: '+group['compatibility_sha256'], '| UTC | Model | Run | Score | Samples | Task attempts | Regression |','| :--- | :--- | :--- | ---: | ---: | ---: | :--- |']
        for row in group['runs']:
            cells=[row['created_at_utc'],row['model'],row['run_id'],f"{row['score']:.2f}",row['sample_count'],row['task_attempts'],json.dumps(row['regressions'])]
            lines.append('| '+' | '.join(markdown_text(cell) for cell in cells)+' |')
    return '\n'.join(lines)
