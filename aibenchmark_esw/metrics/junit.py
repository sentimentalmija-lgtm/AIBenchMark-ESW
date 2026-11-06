"""JUnit XML with one CI testcase per benchmark task, without fabricated cases."""

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from aibenchmark_esw.metrics.reporter import BenchmarkReporter


def collection_results(report):
    """Return recorded sample attempts with each child's identity and pending state."""
    BenchmarkReporter.from_json_dict(report)
    children = list(report.get('samples', []))
    active = (report.get('sampling') or {}).get('active_report')
    if active is not None and active['metadata']['run_id'] not in {
            child['metadata']['run_id'] for child in children}:
        children.append(active)
    results = []
    for child in children:
        metadata = child.get('metadata') or {}
        pending = set(metadata.get('pending_tasks') or [])
        for result in BenchmarkReporter.from_json_dict(child):
            result.provenance = dict(result.provenance or {},
                sample_run_id=metadata['run_id'], sample_run_status=metadata.get('run_status'),
                sample_pending=result.task_id in pending)
            results.append(result)
    return results


def render_collection_junit(report):
    return render_junit(collection_results(report), report['model_name'], report.get('metadata'))


def _xml_text(value):
    return re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", str(value))


def _xml_identity(value):
    """Keep XML-invalid characters distinct from absent or literal escapes."""
    text = str(value).replace('%', '%25')
    return re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]",
                  lambda match: f'%{ord(match.group()):06X}', text)


def render_junit(results, model_name, metadata=None):
    """Render functional outcomes and evaluation errors; pending tasks are skipped.

    Low memory/safety scores are properties, not fabricated Unity failures.
    """
    metadata = metadata or {}
    pending = set(metadata.get("pending_tasks") or [])
    suite = ET.Element("testsuite", name=_xml_text(model_name), tests=str(len(results)))
    properties = ET.SubElement(suite, "properties")
    for key in ("run_id", "run_status", "dataset_sha256", "evaluator_sha256"):
        if metadata.get(key) is not None:
            ET.SubElement(properties, "property", name=key, value=_xml_text(metadata[key]))
    counts = {"failures": 0, "errors": 0, "skipped": 0}
    elapsed = 0.0
    for result in results:
        duration = result.candidate_time_sec if result.candidate_time_sec is not None else result.execution_time_sec
        elapsed += duration
        provenance = result.provenance or {}
        sample_id = provenance.get('sample_run_id')
        classname = _xml_identity(model_name)
        if sample_id is not None:
            classname += f' [sample={quote(sample_id, safe="")}]'
        case = ET.SubElement(suite, "testcase", name=_xml_identity(result.task_id),
                             classname=classname, time=f"{duration:.6f}")
        props = ET.SubElement(case, "properties")
        if sample_id is not None:
            for key, value in (('task_id', result.task_id), ('sample_run_id', sample_id),
                               ('sample_run_status', provenance.get('sample_run_status'))):
                if value is not None:
                    ET.SubElement(props, 'property', name=key, value=_xml_text(value))
        for dimension in ("functional", "memory", "safety", "total"):
            ET.SubElement(props, "property", name=dimension + "_score",
                          value=str(getattr(result.scores, dimension + "_score")))
        tests = result.test_result
        detail = result.error_log or tests.output or ""
        if provenance.get('sample_pending', result.task_id in pending):
            tag, kind, message = "skipped", "pending", "Task evaluation is pending"
        elif not result.compiled:
            tag, kind, message = "error", "generation_or_compilation", "Generation or compilation did not succeed"
        elif not tests.completed:
            tag, kind, message = "error", "incomplete", "Test execution did not complete"
        elif tests.failed_tests:
            tag, kind, message = "failure", "functional", f"{tests.failed_tests}/{tests.total_tests} tests failed"
        elif tests.ignored_tests:
            tag, kind, message = "skipped", "ignored", f"{tests.ignored_tests}/{tests.total_tests} tests were ignored"
        elif result.error_log:
            tag, kind, message = "error", "evaluation", "Evaluation reported an error"
        elif not BenchmarkReporter._all_tests_passed(result):
            tag, kind, message = "failure", "functional", "Task did not pass every test"
        else:
            continue
        counts[{"error": "errors", "failure": "failures", "skipped": "skipped"}[tag]] += 1
        element = ET.SubElement(case, tag, type=kind, message=message)
        element.text = _xml_text(detail)
    for key, count in counts.items():
        suite.set(key, str(count))
    suite.set("time", f"{elapsed:.6f}")
    ET.indent(suite)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(suite, encoding="unicode") + "\n"
