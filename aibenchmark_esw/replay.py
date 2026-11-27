"""Replay verified fixed sources through the shared evaluator, without generation."""

import copy
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from aibenchmark_esw.evaluation import evaluate_task, failed_evaluation
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.output_paths import validate_output_paths
from aibenchmark_esw.provenance import collect_run_metadata, text_sha256
from aibenchmark_esw.report_io import atomic_write_json


def replay_candidates(origin_report, solutions_dir, tasks, executor, analyzer=None, *,
                      output=None, protected_files=(), extra_outputs=(), jobs=1, regrade=False):
    """Evaluate an explicit source set, checkpointing every completed task.

    ``tasks`` supplies the selected configurations, including external datasets.
    Their order is normalized to the origin's selected task IDs. Hash mismatches
    and ambiguous complete files fail preflight; missing/truncated sources keep
    zero-score slots. ``protected_files`` includes the origin report pathname.
    Generation evidence lives exclusively under each task's origin provenance.
    Prepare the origin's transformed tasks first when replaying a variant.
    Use ``regrade`` to evaluate the same sources against a different contract.
    """
    original = BenchmarkReporter.from_json_dict(origin_report)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError('Replay jobs must be a positive integer')
    if executor.cancel_event is None:
        executor.cancel_event = threading.Event()
    if origin_report.get('samples'):
        raise ValueError('Replay requires one explicit sample report, not a multi-sample collection')
    task_map = {task.id: task for task in tasks}
    original_map = {result.task_id: result for result in original}
    if len(task_map) != len(tasks) or set(task_map) != set(original_map):
        raise ValueError('Replay task configurations must match the complete origin task set')
    origin_metadata = origin_report.get('metadata') or {}
    selected = origin_metadata.get('selected_tasks') or [result.task_id for result in original]
    tasks = [task_map[task_id] for task_id in selected]
    directory = Path(solutions_dir)
    if not directory.is_dir():
        raise ValueError('Replay solutions directory must exist')
    validate_output_paths([output, *extra_outputs], protected_files=protected_files,
                          protected_roots=[directory, executor.unity_dir, *[task.task_dir for task in tasks]])
    sources = {}
    reasons = {}
    for task in tasks:
        # IDs are serialized identifiers; never let an imported one escape the
        # source directory or become an ambiguous filesystem mapping.
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', task.id) or task.id in ('.', '..'):
            raise ValueError('Replay task ID is unsafe for source mapping')
        paths = [directory / (task.id + '.c'), directory / task.id / Path(task.entry_file).name]
        existing = [path for path in paths if path.is_file()]
        if len(existing) > 1:
            raise ValueError(f'Ambiguous replay source mapping for {task.id}')
        generation = original_map[task.id].generation or {}
        if generation.get('truncated') or generation.get('finish_reason') == 'length':
            reasons[task.id] = 'Truncated origin candidate cannot be replayed as a complete source'
            continue
        if not existing:
            reasons[task.id] = ('Truncated candidate source is incomplete' if (directory/(task.id+'.truncated.c')).exists()
                                else 'Missing candidate source')
            continue
        path = existing[0]
        try:
            source = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as error:
            raise ValueError(f'Cannot read replay source {task.id}: {error}') from error
        expected_hash = (original_map[task.id].provenance or {}).get('candidate_sha256')
        if expected_hash is None:
            reasons[task.id] = 'Origin candidate hash unavailable; source cannot be verified'
        elif not isinstance(expected_hash, str) or text_sha256(source) != expected_hash:
            raise ValueError(f'Candidate source hash mismatch for {task.id}')
        else:
            sources[task.id] = source

    metadata = collect_run_metadata(tasks, executor, static_analyzer=analyzer)
    recorded_contract = origin_metadata.get('contamination_controls')
    recorded_fingerprints = origin_metadata.get('task_fingerprints')
    if regrade:
        metadata['replay_regrade'] = True
    elif recorded_contract is not None or recorded_fingerprints is not None:
        if not isinstance(recorded_fingerprints, dict) or recorded_fingerprints != metadata['task_fingerprints']:
            raise ValueError('Replay task fingerprints do not match the origin; restore the original task inputs '
                             'or explicitly --regrade')
        if recorded_contract is not None:
            metadata['contamination_controls'] = copy.deepcopy(recorded_contract)
    metadata.update(run_mode='replay', origin_run_id=origin_metadata.get('run_id'),
                    origin_model_name=origin_report.get('model_name'), run_status='running',
                    pending_tasks=[task.id for task in tasks])
    model = origin_report.get('model_name', 'unknown')

    def attach(result, task):
        old = original_map[task.id]
        result.generation = None
        result.footprint_target = executor.cross.target if executor.cross else 'host'
        if result.stack_usage is None and getattr(executor, 'stack_usage', False):
            from aibenchmark_esw.sandbox.stack_usage import unavailable_stack_usage
            result.stack_usage = {role: unavailable_stack_usage('Not evaluated', {'target': result.footprint_target})
                                  for role in ('candidate', 'reference')}
        result.provenance = dict(result.provenance or {}, task_sha256=metadata['task_fingerprints'][task.id],
                                 candidate_sha256=(old.provenance or {}).get('candidate_sha256'),
                                 origin_run_id=metadata['origin_run_id'], origin_generation=copy.deepcopy(old.generation),
                                 origin_provenance=copy.deepcopy(old.provenance))
        return result

    results = {task.id: attach(failed_evaluation(task, model, 'Replay pending'), task) for task in tasks}

    def checkpoint():
        report = BenchmarkReporter.to_json_dict([results[task.id] for task in tasks], model, metadata)
        BenchmarkReporter.from_json_dict(report)
        if output is not None:
            atomic_write_json(output, report)
        return report

    def grade(task):
        if task.id in reasons:
            result = failed_evaluation(task, model, reasons[task.id])
        else:
            result = evaluate_task(task, sources[task.id], task.reference_path.read_text(encoding='utf-8'), model, executor, analyzer)
        return attach(result, task)

    def complete(task_id, result):
        results[task_id] = result
        metadata['pending_tasks'].remove(task_id)
        checkpoint()

    checkpoint()
    pool = None
    try:
        if jobs == 1:
            for task in tasks:
                complete(task.id, grade(task))
        else:
            pool = ThreadPoolExecutor(max_workers=jobs)
            futures = {pool.submit(grade, task): task.id for task in tasks}
            for future in as_completed(futures):
                complete(futures[future], future.result())
    except KeyboardInterrupt:
        executor.cancel_event.set()
        metadata['run_status'] = 'interrupted'
        checkpoint()
        if pool is not None:
            for future in futures:
                future.cancel()
        raise
    except Exception:
        executor.cancel_event.set()
        metadata['run_status'] = 'aborted'
        checkpoint()
        raise
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        if metadata['run_status'] in ('interrupted', 'aborted'):
            checkpoint()
    metadata['run_status'] = 'completed'
    return checkpoint()
