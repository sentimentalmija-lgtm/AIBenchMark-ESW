"""Validate and restore a saved run before any provider calls or output writes."""

import copy
import json
import os
import shutil
from pathlib import Path

from aibenchmark_esw.metrics.comparison import generation_conditions
from aibenchmark_esw.metrics.reporter import BenchmarkReporter

_GENERATION_OPTIONS = {
    "temperature": "temperature", "max_tokens": "max_tokens",
    "request_timeout_seconds": "request_timeout", "max_retries": "max_retries",
    "retry_backoff_seconds": "retry_backoff", "prompt_strategy": "prompt_strategy",
    "review_turn": "review_turn",
    'repair_turns': 'repair_turns',
    "input_cost_per_million": "input_cost_per_million", "output_cost_per_million": "output_cost_per_million",
}
_EXECUTION_OPTIONS = {
    "compile_timeout_seconds": "compile_timeout", "max_output_bytes": "max_output_bytes",
    "isolation": "isolation", "memory_limit_bytes": "memory_limit_bytes", "sanitizers": "sanitizers",
}
_RUN_OPTIONS: tuple[str, ...] = ("compiler", "allow_standard_fallback", "compile_timeout", "max_output_bytes",
                "isolation", "memory_limit_bytes", "sanitizers", "save_solutions", "tasks_root",
                "system_prompt_file", "target", "cross_compiler")
_RUN_OPTIONS += ("warnings", "hardening", "stack_usage", "container_engine", "container_image", "static_analysis_timeout", "extended_safety_rules",
                 "variant_seed", "heldout_tests", 'api_key_env')
_PATH_OPTIONS = {"save_solutions", "tasks_root", "system_prompt_file", "heldout_tests"}


def _equivalent(option, first, second):
    if option in ("compiler", "cross_compiler"):
        if first is None or second is None:
            return first is second
        def resolved(value):
            return Path(shutil.which(str(value)) or str(value)).resolve()
        left, right = resolved(first), resolved(second)
        return os.path.normcase(str(left)) == os.path.normcase(str(right)) or (
            left.is_file() and right.is_file() and left.samefile(right))
    if option in _PATH_OPTIONS:
        return (first is None and second is None) or (
            first is not None and second is not None and Path(first).resolve() == Path(second).resolve())
    if option == "sanitizers":
        def items(value):
            return sorted(part.strip() for part in value.split(",") if part.strip()) if isinstance(value, str) else sorted(value or [])
        return items(first) == items(second)
    return first == second


def _restore(args, name, value):
    explicit = set(getattr(args, "_explicit_options", ()))
    flags = {"--" + name.replace("_", "-")}
    if name == "prompt_strategy":
        flags.add("--multi-turn")
    if flags & explicit and not _equivalent(name, getattr(args, name, None), value):
        raise ValueError(f"Resume option --{name.replace('_', '-')} conflicts with the saved run")
    if name in _PATH_OPTIONS and value is not None:
        value = Path(value)
    setattr(args, name, value)


def load_resume(args):
    """Read args.resume, restore unspecified saved options, and return its report.

    --output and --jobs intentionally remain adjustable. A missing --output
    resumes in place. _explicit_options contains original command-line flags.
    """
    path = Path(args.resume)
    report = json.loads(path.read_text(encoding="utf-8"))
    results = BenchmarkReporter.from_json_dict(report)
    metadata = report.get("metadata") or {}
    if (not isinstance(metadata.get("run_id"), str) or not metadata["run_id"].strip()
            or metadata.get("run_status") not in ("running", "interrupted", "aborted", "completed")):
        raise ValueError("Resume requires an identified checkpoint with explicit run status")
    if (not isinstance(report.get("model_name"), str) or not report["model_name"].strip()
            or any(result.model_name != report["model_name"] for result in results)):
        raise ValueError("Resume requires matching run and task model names")
    selected = metadata.get("selected_tasks")
    if not isinstance(selected, list) or not selected:
        raise ValueError("Resume requires recorded selected_tasks")
    _restore(args, "model", report["model_name"])
    settings = metadata.get("generation_settings")
    if settings is not None:
        defaults = {"temperature": None, "max_tokens": None, "request_timeout_seconds": 60,
                    "max_retries": 0, "retry_backoff_seconds": 1, "prompt_strategy": "single", "review_turn": False,
                    'repair_turns': 0}
        for key, option in _GENERATION_OPTIONS.items():
            _restore(args, option, settings.get(key, defaults.get(key)))
    for key, option in _EXECUTION_OPTIONS.items():
        if key in (metadata.get("execution_settings") or {}):
            _restore(args, option, metadata["execution_settings"][key])
    options = metadata.get("run_options") or {}
    if not isinstance(options, dict):
        raise ValueError("Saved run_options must be an object")
    for option in _RUN_OPTIONS:
        if option in options:
            _restore(args, option, options[option])
    if "--max-cost-usd" not in set(getattr(args, "_explicit_options", ())):
        args.max_cost_usd = (metadata.get("cost_budget") or {}).get("limit_usd", options.get("max_cost_usd"))
    explicit = set(getattr(args, "_explicit_options", ()))
    if "--tasks" in explicit:
        requested = [item.strip() for item in (getattr(args, "tasks", "") or "").split(",")]
        if len(requested) != len(set(requested)) or set(requested) != set(selected):
            raise ValueError("Resume --tasks must match the saved task set")
    if "--tier" in explicit and any(result.tier != args.tier for result in results):
        raise ValueError("Resume --tier excludes saved tasks")
    args.tasks = ",".join(selected)
    if "--output" not in explicit:
        args.output = path
    return report


def validate_resume(report, fresh_metadata, tasks):
    """Reject changed evaluation/generation inputs before replacing a checkpoint."""
    BenchmarkReporter.from_json_dict(report)
    saved = report.get("metadata") or {}
    from aibenchmark_esw.metrics.measurements import portable_settings
    task_ids = [task.id for task in tasks]
    if set(task_ids) != set(saved.get("selected_tasks") or []):
        raise ValueError("Resume selection must preserve every saved task (including pending tasks)")
    required = ("dataset_sha256", "evaluator_sha256", "task_fingerprints", "compiler", "static_analysis", "platform")
    for key in required:
        if saved.get(key) is None or fresh_metadata.get(key) is None:
            raise ValueError(f"Resume requires recorded {key} to verify compatibility")
        first, second = saved[key], fresh_metadata[key]
        if key == "compiler":
            first, second = portable_settings(first), portable_settings(second)
        if first != second:
            raise ValueError(f"Resume has incompatible {key}; start a separate run")
    if portable_settings(saved.get("execution_settings")) != portable_settings(fresh_metadata.get("execution_settings")):
        raise ValueError("Resume has incompatible execution_settings; start a separate run")
    if generation_conditions(saved) != generation_conditions(fresh_metadata):
        raise ValueError("Resume has incompatible generation settings; start a separate run")
    if saved.get("benchmark_version") != fresh_metadata.get("benchmark_version"):
        raise ValueError("Resume has incompatible benchmark version")


def merge_generation_history(previous, current):
    """Keep every paid attempt and aggregate only fully known usage/latency.

    Partial known totals are retained separately with explicit coverage, while
    public usage remains unknown when any attempt omitted that measurement.
    """
    if not previous:
        return copy.deepcopy(current)
    if not current:
        return copy.deepcopy(previous)
    previous = copy.deepcopy(previous)
    result = copy.deepcopy(current)
    history = previous.pop("history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ValueError("Generation history must be an array of objects")
    previous["usage"] = previous.pop("attempt_usage", previous.get("usage"))
    previous["known_usage"] = previous.pop("attempt_known_usage", previous.get("known_usage"))
    previous["cost_usd"] = previous.pop("attempt_cost_usd", previous.get("cost_usd"))
    previous["known_cost_usd"] = previous.pop("attempt_known_cost_usd", previous.get("known_cost_usd"))
    previous["latency_seconds"] = previous.pop("attempt_latency_seconds", previous.get("latency_seconds"))
    previous["usage_coverage"] = previous.pop("attempt_usage_coverage", previous.get("usage_coverage"))
    for key in ("known_latency_seconds", "latency_coverage"):
        previous.pop(key, None)
    history.append(previous)
    result["history"] = history
    result["attempt_usage"] = copy.deepcopy(current.get("usage"))
    result["attempt_known_usage"] = copy.deepcopy(current.get("known_usage"))
    result["attempt_cost_usd"] = current.get("cost_usd")
    result["attempt_known_cost_usd"] = current.get("known_cost_usd")
    result["attempt_latency_seconds"] = current.get("latency_seconds")
    result["attempt_usage_coverage"] = copy.deepcopy(current.get("usage_coverage"))
    attempts = [attempt for attempt in history + [current]
                if "attempts" not in attempt or attempt["attempts"]]
    if not attempts:
        return result
    usage, known_usage, coverage = {}, {}, {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [(attempt.get("usage") or {}).get(key) for attempt in attempts]
        partial = [(attempt.get("known_usage") or {}).get(key) for attempt in attempts]
        known = [exact if exact is not None else subtotal for exact, subtotal in zip(values, partial)
                 if exact is not None or subtotal is not None]
        known_usage[key] = sum(value for value in known if value is not None) if known else None
        complete = all(value is not None for value in values)
        usage[key] = sum(value for value in values if value is not None) if complete else None
        request_counts = []
        for session in attempts:
            requests = session.get("attempts")
            if requests is not None:
                # Raw provider attempts also recover coverage from older merged
                # checkpoints that discarded the per-session coverage fields.
                request_counts.append({
                    "known_attempts": sum((request.get("usage") or {}).get(key) is not None
                                          for request in requests),
                    "total_attempts": len(requests)})
            else:
                recorded = (session.get("usage_coverage") or {}).get(key)
                # Legacy sessions without request evidence retain the original
                # one-attempt interpretation; never apply it to modern retries.
                request_counts.append(recorded if recorded is not None else {
                    "known_attempts": int((session.get("usage") or {}).get(key) is not None),
                    "total_attempts": 1})
        coverage[key] = {name: sum(count[name] for count in request_counts)
                         for name in ("known_attempts", "total_attempts")}
    result.update(usage=usage, known_usage=known_usage, usage_coverage=coverage)
    costs = [attempt.get("cost_usd") for attempt in attempts]
    partial_costs = [attempt.get("known_cost_usd") for attempt in attempts]
    known_costs = [exact if exact is not None else partial for exact, partial in zip(costs, partial_costs)
                   if exact is not None or partial is not None]
    result.update(cost_usd=sum(value for value in costs if value is not None) if all(value is not None for value in costs) else None,
                  known_cost_usd=sum(value for value in known_costs if value is not None) if known_costs else None,
                  cost_complete=all(value is not None for value in costs),
                  usage_complete=all(value is not None for value in usage.values()))
    durations = [attempt.get("latency_seconds") for attempt in attempts]
    known_durations = [value for value in durations if value is not None]
    result["known_latency_seconds"] = round(sum(known_durations), 6) if known_durations else None
    result["latency_seconds"] = round(sum(known_durations), 6) if len(known_durations) == len(attempts) else None
    result["latency_coverage"] = {"known_attempts": len(known_durations), "total_attempts": len(attempts)}
    return result
