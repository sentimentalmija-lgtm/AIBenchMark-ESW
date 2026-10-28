"""Dependency-free, escaped offline HTML, SARIF and comparison JUnit exports."""

import html
import json
import math
from xml.etree import ElementTree as ET

from aibenchmark_esw.metrics.junit import _xml_text


def _escape(value):
    return html.escape(str(value), quote=True)


def _document(title, content, task_filters=True):
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>''' + _escape(title) + '''</title><style>
:root{color-scheme:light dark;font:15px system-ui}body{max-width:1400px;margin:auto;padding:24px}
table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #8886;text-align:left}
th{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:8px 0}meter{width:100%}
input,select{padding:8px;margin:8px}tr[hidden]{display:none} .score{min-width:100px}
@media(max-width:700px){table{display:block;overflow-x:auto}}
</style></head><body><h1>''' + _escape(title) + '''</h1>
<label>Filter <input id="filter" type="search" aria-label="Filter rows"></label>
''' + ('''
<label>Status <select id="status"><option value="">All</option><option>PASS</option><option>FAIL</option><option>INCOMPLETE</option><option>ERROR</option></select></label>
<label>Tier <select id="tier"><option value="">All</option><option>1</option><option>2</option><option>3</option><option>4</option></select></label>
<label>Category <select id="category"><option value="">All</option></select></label>
''' if task_filters else '') + '''
<label>Minimum score <input id="minscore" type="number" min="0" max="100" value="0"></label>
''' + content + '''<script>
const categories=[...new Set([...document.querySelectorAll('tbody tr')].map(r=>r.dataset.category).filter(Boolean))].sort();
categories.forEach(c=>document.querySelector('#category')?.add(new Option(c,c)));
function filterRows(){const q=document.querySelector('#filter').value.toLowerCase(),s=document.querySelector('#status')?.value||'',t=document.querySelector('#tier')?.value||'',c=document.querySelector('#category')?.value||'',n=Number(document.querySelector('#minscore').value);
document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q)||(s&&r.dataset.status!==s)||(t&&r.dataset.tier!==t)||(c&&r.dataset.category!==c)||Number(r.dataset.score||0)<n);}
['filter','minscore'].forEach(id=>document.getElementById(id).addEventListener('input',filterRows));
['status','tier','category'].forEach(id=>document.getElementById(id)?.addEventListener('change',filterRows));
document.querySelectorAll('th[data-sort]').forEach(h=>h.addEventListener('click',()=>{const b=h.closest('table').querySelector('tbody'),i=h.cellIndex,d=h.dataset.direction==='asc'?-1:1;
const rows=[...b.rows];rows.sort((a,c)=>{const x=a.cells[i].dataset.value||a.cells[i].textContent,y=c.cells[i].dataset.value||c.cells[i].textContent;
return d*(x.trim()!==''&&y.trim()!==''&&Number.isFinite(Number(x))&&Number.isFinite(Number(y))?Number(x)-Number(y):x.localeCompare(y));});
rows.forEach(r=>b.appendChild(r));h.dataset.direction=d===1?'asc':'desc';}));
</script></body></html>'''


def _table(headers, rows):
    return '<table><thead><tr>' + ''.join('<th data-sort="true">' + _escape(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'


def report_html(results, model_name, metadata=None):
    from aibenchmark_esw.metrics.reporter import BenchmarkReporter
    rows = []
    for result in results:
        status = 'INCOMPLETE' if not result.test_result.completed else 'PASS' if BenchmarkReporter._all_tests_passed(result) else 'FAIL'
        if status == 'PASS' and result.error_log:
            status = 'ERROR'
        category = ((metadata or {}).get('task_categories') or {}).get(result.task_id, 'Unknown (legacy report)')
        values = [result.task_id, result.tier, category, status, BenchmarkReporter._standard(result), BenchmarkReporter._task_weights(result)]
        cells = ['<td>' + _escape(value) + '</td>' for value in values]
        from aibenchmark_esw.metrics.measurements import dimension_summary
        dimensions = dimension_summary([result])
        for value in (result.scores.total_score, result.scores.functional_score, dimensions['memory'], dimensions['safety']):
            cells.append(f'<td class="score" data-value="{value}">{value:.2f}<meter min="0" max="100" value="{value}"></meter></td>'
                         if value is not None else '<td class="score">Unavailable</td>')
        for value, budget in ((result.size_metrics.flash_bytes, result.limits.max_flash_bytes if result.limits else None),
                              (result.size_metrics.ram_bytes, result.limits.max_ram_bytes if result.limits else None)):
            text = f'{value} / {budget if budget is not None else "unknown"} B' if result.size_metrics.measured else 'Unavailable'
            cells.append('<td>' + _escape(text) + (f'<meter min="0" max="{max(budget, value, 1)}" value="{value}"></meter>' if budget is not None and result.size_metrics.measured else '') + '</td>')
        cells.append(f'<td data-value="{result.execution_time_sec}">{result.execution_time_sec:.3f}s</td>')
        details = {'compiler/test diagnostics': result.error_log, 'test output': result.test_result.output,
                   'stack usage (per-function compiler evidence)': result.stack_usage,
                   'static analysis': result.safety_metrics.__dict__, 'generation': result.generation, 'provenance': result.provenance}
        cells.append('<td><details><summary>Diagnostics and provenance</summary><pre>' + _escape(json.dumps(details, indent=2, default=str)) + '</pre></details></td>')
        rows.append(f'<tr data-status="{status}" data-tier="{result.tier}" data-category="{_escape(category)}" data-score="{result.scores.total_score}">' + ''.join(cells) + '</tr>')
    warnings = BenchmarkReporter.analysis_warnings(results) + BenchmarkReporter.scoring_warnings(results, metadata)
    from aibenchmark_esw.metrics.measurements import render_measurements
    measurements = render_measurements(results, (metadata or {}).get('collection_generation_summary'))
    from aibenchmark_esw.sandbox.stack_usage import stack_usage_lines
    measurements += stack_usage_lines(results)
    sampling = BenchmarkReporter.sampling_lines(metadata)
    warning = BenchmarkReporter.run_warning(metadata)
    if warning:
        warnings.append(warning)
    from aibenchmark_esw.metrics.groups import breakdown_rows, grouped_scores
    groups = grouped_scores(results, metadata)
    group_rows = breakdown_rows([dict(model=model_name, **groups)])
    group_fields = ['scope', 'group', 'count', 'task_attempts', 'mean_score', 'pass_at_1_pct']
    group_table = '<h2>Tier and category breakdown</h2>' + _table(group_fields, ['<tr>' + ''.join('<td>' + _escape(row[field]) + '</td>' for field in group_fields) + '</tr>' for row in group_rows])
    return _document('AIBenchMark-ESW: ' + model_name, '<p>' + _escape('; '.join(warnings)) + '</p>' +
                     '<p>Tier-weighted score' + (' (first sample)' if (metadata or {}).get('sampling') else '') + ': ' + _escape(groups['tier_weighted_score']) + '; flat score: ' + _escape(BenchmarkReporter.to_json_dict(results, model_name)['overall_score']) + '</p>' + group_table +
                     '<pre>' + _escape('\n'.join(sampling + measurements)) + '</pre>' +
                     _table(['Task', 'Tier', 'Category', 'Status', 'Standard', 'Weights F/M/S', 'Score', 'Functional', 'Memory', 'Safety', 'Flash', 'RAM', 'Time', 'Details'], rows) +
                     '<details open><summary>Reproducibility metadata</summary><pre>' + _escape(json.dumps(metadata or {}, indent=2)) + '</pre></details>')


def summary_html(title, records, summary):
    fields = list(records[0]) if records else []
    rows = []
    for record in records:
        cells = ''.join('<td>' + _escape(json.dumps(record[field], ensure_ascii=False) if isinstance(record[field], (dict, list)) else record[field]) + '</td>' for field in fields)
        score = record.get('score', (record.get('statistics') or {}).get('score', {}).get('mean', 0))
        rows.append('<tr data-score="' + _escape(score) + '">' + cells + '</tr>')
    extra = ''
    from aibenchmark_esw.metrics.groups import breakdown_rows
    groups = breakdown_rows(records)
    if groups:
        keys = list(groups[0])
        extra += '<h2>Tier and category breakdown</h2>' + _table(keys, ['<tr>' + ''.join('<td>' + _escape(row[key]) + '</td>' for key in keys) + '</tr>' for row in groups])
    paired = summary.get('paired')
    if paired:
        extra += '<h2>Paired task differences</h2><p>' + _escape(paired['interpretation']) + '</p>'
        paired_keys = ['model', 'baseline', 'matched_tasks', 'unmatched_tasks', 'median_delta', 'median_delta_ci95', 'p_value', 'p_value_bonferroni', 'cohens_dz', 'task_deltas']
        extra += _table(paired_keys, ['<tr>' + ''.join('<td>' + _escape(row[key]) + '</td>' for key in paired_keys) + '</tr>' for row in paired['comparisons']])
    return _document(title, _table(fields, rows) + extra + '<details><summary>Complete data and warnings</summary><pre>' + _escape(json.dumps(summary, indent=2, ensure_ascii=False)) + '</pre></details>', task_filters=False)


def generate_sarif(results, model_name='unknown', metadata=None, *, source_root=None, source_mapping=None):
    from aibenchmark_esw.metrics.sarif_sources import generate_sarif as render
    return render(results, model_name, metadata, source_root=source_root, source_mapping=source_mapping)


def comparison_junit(comparison, baseline_model=None, regression_threshold=0.0):
    if isinstance(regression_threshold, bool) or not isinstance(regression_threshold, (int, float)) or not math.isfinite(regression_threshold) or regression_threshold < 0:
        raise ValueError('Regression threshold must be a finite nonnegative score delta')
    baseline_model = baseline_model or comparison['models'][0]['model']
    baseline = {row['task_id']: row for row in comparison['task_rows'] if row['model'] == baseline_model}
    if not baseline:
        raise ValueError('Baseline model is absent from comparison')
    suite = ET.Element('testsuite', name='AIBenchMark-ESW comparison', tests=str(len(comparison['task_rows'])))
    failures = 0
    for row in comparison['task_rows']:
        case = ET.SubElement(suite, 'testcase', classname=_xml_text(row['model']), name=_xml_text(row['task_id']))
        delta = row['total'] - baseline[row['task_id']]['total']
        text = f'Score {row["total"]:g}; baseline {baseline_model} {baseline[row["task_id"]]["total"]:g}; delta {delta:g}; threshold {regression_threshold:g}'
        if delta < -regression_threshold:
            failures += 1
            ET.SubElement(case, 'failure', message='Task score regression', type='score-regression').text = _xml_text(text)
        ET.SubElement(case, 'system-out').text = _xml_text(text)
    suite.set('failures', str(failures))
    from aibenchmark_esw.metrics.paired import paired_comparisons
    paired = paired_comparisons(comparison, baseline_model)
    for row in paired['comparisons']:
        case = ET.SubElement(suite, 'testcase', classname=_xml_text(row['model']), name='paired-task-statistics')
        ET.SubElement(case, 'system-out').text = _xml_text(json.dumps(row, ensure_ascii=False))
    suite.set('tests', str(len(suite.findall('testcase'))))
    return ET.tostring(suite, encoding='unicode', xml_declaration=True) + '\n'
