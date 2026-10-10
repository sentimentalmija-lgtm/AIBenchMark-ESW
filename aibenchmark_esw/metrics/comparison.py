"""Compare saved runs without making model API requests."""

import csv
import io
import math
from dataclasses import asdict

from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.metrics.measurements import measurement_summary, portable_settings, coverage_text
from aibenchmark_esw.metrics.statistics import score_statistics, interval_text


GENERATION_CONDITIONS = ("temperature", "max_tokens", "request_timeout_seconds",
                         "prompt_strategy", "review_turn", "system_prompt_sha256", "max_retries", "retry_backoff_seconds",
                         "input_cost_per_million", "output_cost_per_million")


def generation_conditions(metadata):
    settings = metadata.get("generation_settings")
    if settings is None:
        return None
    # Defaults preserve comparison with the original one-turn report format.
    defaults = {"prompt_strategy": "single", "review_turn": False, "max_retries": 0, "retry_backoff_seconds": 1}
    return {key: settings.get(key, defaults.get(key)) for key in GENERATION_CONDITIONS}


def compare_runs(reports, *, _allow_repeated_models=False, _check_generation=True):
    if len(reports) < (1 if _allow_repeated_models else 2):
        raise ValueError("Comparison requires at least two reports")
    runs, models, warnings = [], set(), []
    for report in reports:
        results = BenchmarkReporter.from_json_dict(report)
        model = report.get("model_name")
        if "samples" in report and not report["samples"]:
            raise ValueError(f"Cannot compare collection without completed samples: {model}")
        if (not isinstance(model, str) or not model.strip()
                or (model in models and not _allow_repeated_models)):
            raise ValueError("Reports must have distinct, nonempty model names")
        models.add(model)
        if not results:
            raise ValueError("Cannot compare an empty run")
        tasks = {result.task_id: result for result in results}
        if len(tasks) != len(results):
            raise ValueError("Duplicate task IDs in a run")
        if any(result.model_name != model for result in results):
            raise ValueError("Task model names must match the run model")
        metadata = report.get("metadata") or {}
        if metadata.get("unreadable_assets"):
            raise ValueError(f"Cannot compare {model}: benchmark inputs were unreadable when fingerprinted")
        if metadata.get("run_status") not in (None, "completed"):
            raise ValueError(f"Cannot compare unfinished run: {model} ({metadata['run_status']})")
        compiler = metadata.get("compiler") or {}
        host = metadata.get("platform") or {}
        analysis = metadata.get("static_analysis") or {}
        known_analysis = (analysis.get("engine") == "builtin" or
                          (analysis.get("engine") == "builtin+cppcheck" and bool(analysis.get("cppcheck_version"))))
        complete_provenance = bool(metadata.get("dataset_sha256") and metadata.get("evaluator_sha256")
                                   and compiler.get("name") and compiler.get("version")
                                   and host.get("system") and host.get("machine") and known_analysis)
        if not metadata.get("scoring_policy"):
            complete_provenance = False
            warnings.append(f"{model}: legacy scoring policy is unverifiable; limits and weights are report-supplied.")
        if not complete_provenance:
            warnings.append(f"{model}: missing evaluator/dataset/toolchain provenance; compatibility cannot be fully checked.")
        if not known_analysis:
            warnings.append(f"{model}: static-analysis configuration/version is unknown.")
        warnings.extend(f"{model}: {warning}" for warning in BenchmarkReporter.analysis_warnings(results))
        warnings.extend(f"{model}: {warning}" for warning in BenchmarkReporter.scoring_warnings(results))
        if not host.get("release"):
            warnings.append(f"{model}: host OS release is unknown; compatibility cannot be fully checked.")
        runs.append((report, results, tasks, metadata, complete_provenance))

    expected = set(runs[0][2])
    for _, _, tasks, _, _ in runs[1:]:
        if set(tasks) != expected:
            raise ValueError("Reports must contain identical task sets, including failures")
    for task_id in expected:
        entries = [run[2][task_id] for run in runs]
        if len({entry.footprint_target for entry in entries}) > 1:
            raise ValueError(f"Incompatible footprint targets for {task_id}")
        weights = {tuple(asdict(entry.weights).values()) for entry in entries if entry.weights is not None}
        standards = {entry.target_standard for entry in entries if entry.target_standard is not None}
        effective = {entry.effective_standard for entry in entries if entry.effective_standard is not None}
        if len(weights) > 1 or len(standards) > 1 or len(effective) > 1:
            raise ValueError(f"Incompatible weights or C standards for {task_id}")
        if any(entry.weights is None or entry.target_standard is None for entry in entries):
            warnings.append(f"{task_id}: legacy weights or C standard are unknown.")
        fingerprints = {(entry.provenance or {}).get("task_sha256") for entry in entries}
        fingerprints.discard(None)
        if len(fingerprints) > 1:
            raise ValueError(f"Incompatible task fingerprints for {task_id}")
        limits = {tuple(asdict(entry.limits).values()) for entry in entries if entry.limits is not None}
        if len(limits) > 1:
            raise ValueError(f"Incompatible task limits for {task_id}")

    for key in ("dataset_sha256", "evaluator_sha256", "benchmark_version", "compiler", "static_analysis",
                "execution_settings", "scoring_policy"):
        available = [portable_settings(metadata[key]) if key in ("compiler", "execution_settings") else metadata[key]
                     for _, _, _, metadata, _ in runs if metadata.get(key)]
        if any(value != available[0] for value in available[1:]):
            raise ValueError(f"Incompatible {key} across reports")
    for key in ("system", "machine", "release"):
        values = [(metadata.get("platform") or {}).get(key) for _, _, _, metadata, _ in runs]
        known = [value for value in values if value is not None]
        if any(value != known[0] for value in known[1:]):
            raise ValueError(f"Incompatible host platform/architecture ({key}) across reports")
    settings = [generation_conditions(metadata) for _, _, _, metadata, _ in runs]
    known_settings = [value for value in settings if value is not None]
    if _check_generation and any(value != known_settings[0] for value in known_settings[1:]):
        raise ValueError("Incompatible temperature, token limits, request timeouts or prompt strategy across reports")
    if any(value is None for value in settings):
        for (report, _, _, _, _), settings_value in zip(runs, settings):
            if settings_value is None:
                warnings.append(f"{report['model_name']}: generation settings are missing or not applicable "
                                "to this local/baseline run; generation conditions cannot be fully compared.")
    execution = [metadata.get("execution_settings") for _, _, _, metadata, _ in runs]
    if any(value is not None for value in execution) and any(value is None for value in execution):
        warnings.append("Some reports lack execution settings; runtime compatibility cannot be fully checked.")

    rows = []
    task_rows = []
    for report, results, _, _, provenance in runs:
        sample_results = [BenchmarkReporter.from_json_dict(sample) for sample in report['samples']] if 'samples' in report else [results]
        if 'samples' in report:
            compare_runs(report['samples'], _allow_repeated_models=True)
        all_results = [result for sample in sample_results for result in sample]
        tokens, durations = [], []
        for result in results:
            generation = result.generation or {}
            usage = generation.get("usage") or {}
            total = usage.get("total_tokens")
            duration = generation.get("latency_seconds")
            if total is not None:
                if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise ValueError("Invalid token usage in report")
                tokens.append(total)
            if duration is not None:
                if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                    raise ValueError("Invalid generation duration in report")
                durations.append(duration)
        summary = BenchmarkReporter.to_json_dict(all_results, report["model_name"])
        measured_count = sum(r.size_metrics.measured for r in all_results)
        if measured_count != len(all_results):
            warnings.append(f"{report['model_name']}: memory average includes unmeasured zeros ({measured_count}/{len(all_results)} measured).")
        from aibenchmark_esw.sampling import pass_at_k
        count = len(sample_results)
        ks = (report.get('sampling') or {}).get('pass_k', [1])
        model_pass_k: dict = {str(k): [] for k in ks}
        for result in sorted(results, key=lambda item: item.task_id):
            attempts = [attempt for attempt in all_results if attempt.task_id == result.task_id]
            passed = sum(BenchmarkReporter._all_tests_passed(attempt) for attempt in attempts)
            task_pass_k = {str(k): round(pass_at_k(count, passed, k) * 100, 6) for k in ks}
            for k, value in task_pass_k.items():
                model_pass_k[k].append(value)
            task_rows.append({"model": report["model_name"], "task_id": result.task_id,
                "total": round(sum(attempt.scores.total_score for attempt in attempts) / count, 6),
                "functional": round(sum(attempt.scores.functional_score for attempt in attempts) / count, 6),
                "memory": round(sum(attempt.scores.memory_score for attempt in attempts) / count, 6) if all(attempt.size_metrics.measured for attempt in attempts) else None,
                "safety": round(sum(attempt.scores.safety_score for attempt in attempts) / count, 6),
                "measured": all(attempt.size_metrics.measured for attempt in attempts),
                "pass_at_1": passed == count, "samples": count, "passed_samples": passed,
                "pass_at_k": task_pass_k})
        statistics = score_statistics([BenchmarkReporter.to_json_dict(sample, report['model_name'])['overall_score'] for sample in sample_results])
        rows.append({
            "model": report["model_name"], "tasks": len(results),
            "score": summary["overall_score"], "pass_at_1_pct": summary["pass_at_1_pct"],
            "samples": count, "task_attempts": len(all_results),
            "score_sample_stddev": statistics['sample_stddev'], "score_mean_ci95_approx": statistics['mean_ci95_approx'],
            "pass_at_k": {key: round(sum(values) / len(values), 6) for key, values in model_pass_k.items()},
            "functional": round(sum(r.scores.functional_score for r in all_results) / len(all_results), 2),
            "memory": round(sum(r.scores.memory_score for r in all_results) / len(all_results), 2),
            "safety": round(sum(r.scores.safety_score for r in all_results) / len(all_results), 2),
            "total_tokens": sum(tokens) if tokens else None, "usage_tasks": len(tokens),
            "generation_seconds": round(sum(durations), 3) if durations else None,
            "duration_tasks": len(durations), "provenance": "Recorded" if provenance else "Unknown",
            "cppcheck_completed_tasks": sum(r.safety_metrics.cppcheck_status == "completed" for r in results),
            "cppcheck_failed_tasks": sum(r.safety_metrics.cppcheck_status == "failed" for r in results),
            "generation_settings": generation_conditions(report.get("metadata") or {}),
            "run_mode": (report.get("metadata") or {}).get("run_mode", "generation" if (report.get("metadata") or {}).get("generation_settings") else "local"),
            "origin_run_id": (report.get("metadata") or {}).get("origin_run_id"),
        })
        rows[-1].update(measurement_summary(all_results))
        if rows[-1]["run_mode"] == "replay":
            warnings.append(f"{report['model_name']}: offline replay of {rows[-1]['origin_run_id']}; fixed candidate grading, no new generation or spend.")
        if rows[-1]["usage_tasks"] < len(all_results):
            warnings.append(f"{report['model_name']}: token usage is incomplete; known subtotal "
                            f"{rows[-1]['known_total_tokens']}, complete {rows[-1]['usage_tasks']}/{len(all_results)} task attempts.")
    gaps = []
    for task_id in sorted(expected):
        scores = sorted((row for row in task_rows if row["task_id"] == task_id), key=lambda row: (row["total"], row["model"]))
        gaps.append({"task_id": task_id, "gap": scores[-1]["total"] - scores[0]["total"],
                     "high": scores[-1], "low": scores[0]})
    largest = sorted(gaps, key=lambda gap: (-gap["gap"], gap["task_id"]))[0]
    return {"task_ids": sorted(expected), "warnings": warnings, "task_rows": task_rows, "largest_gap": largest,
            "models": sorted(rows, key=lambda row: (-row["score"], row["model"]))}


def render_comparison(comparison, format_name="markdown", *, baseline_model=None, regression_threshold=0.0):
    if format_name == "html":
        from aibenchmark_esw.metrics.exports import summary_html
        return summary_html("Model comparison", comparison["models"], comparison)
    if format_name == "junit":
        from aibenchmark_esw.metrics.exports import comparison_junit
        return comparison_junit(comparison, baseline_model, regression_threshold)
    if format_name == "csv-long":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=["model", "task_id", "total", "functional", "memory",
                                                   "safety", "measured", "pass_at_1"],
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(comparison["task_rows"])
        return stream.getvalue()
    if format_name == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(comparison["models"][0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(comparison["models"])
        return stream.getvalue()
    if format_name == "cli":
        lines = ["AIBenchMark-ESW Model Comparison", "=" * 94,
                 f"{'Model':<32} {'Score':>8} {'Pass@1':>8} {'F/M/S':>20} {'Provenance':>12}"]
        for row in comparison["models"]:
            model = row["model"].replace("\r", " ").replace("\n", " ")
            dimensions = f"{row['functional']:.1f}/{row['memory']:.1f}/{row['safety']:.1f}"
            lines.append(f"{model:<32} {row['score']:>8.2f} {row['pass_at_1_pct']:>7.2f}% {dimensions:>20} {row['provenance']:>12}")
        lines += ["", "Failed tasks remain in the denominator."]
        lines += [f"{row['model']} generation settings: {row['generation_settings']}; tokens: {row['total_tokens']} "
                  f"(known subtotal {row['known_total_tokens']}; complete {row['usage_tasks']}/{row['tasks']}); "
                  f"cost USD: {row['total_cost_usd']} (known subtotal {row['known_cost_usd']}; complete {row['cost_tasks']}/{row['tasks']})"
                  for row in comparison["models"]]
        lines += [row['model'] + ': ' + coverage_text(row) for row in comparison['models']]
        lines += [f"{row['model']}: {row['samples']} samples; sample SD {row['score_sample_stddev']}; " + interval_text(row['score_mean_ci95_approx']) + f"; Pass@k (%) {row['pass_at_k']}" for row in comparison['models']]
        lines += [f"Warning: {warning}" for warning in comparison["warnings"]]
        lines += _task_matrix(comparison, markdown=False)
        return "\n".join(lines)
    lines = ["# AIBenchMark-ESW Model Comparison", "",
             "Identical task sets; scores include failed tasks. Pass@1 measures fully passing suites.", ""]
    for warning in comparison["warnings"]:
        lines.append(f"- Warning: {warning}")
    if comparison["warnings"]:
        lines.append("")
    lines += ["| Model | Score | Pass@1 (%) | Functional | Memory | Safety | Tokens | Generation (s) | Provenance |",
              "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |"]
    for row in comparison["models"]:
        model = row["model"].replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        tokens = (f"Unknown (known subtotal {row['known_total_tokens']} ({row['usage_tasks']}/{row['tasks']} tasks); complete coverage)"
                  if row["total_tokens"] is None else str(row["total_tokens"]))
        duration = "Unknown" if row["generation_seconds"] is None else str(row["generation_seconds"])
        if row["total_tokens"] is not None and row["usage_tasks"] < row["tasks"]:
            tokens += f" ({row['usage_tasks']}/{row['tasks']} tasks)"
        if row["generation_seconds"] is not None and row["duration_tasks"] < row["tasks"]:
            duration += f" ({row['duration_tasks']}/{row['tasks']} tasks)"
        lines.append(f"| {model} | {row['score']:.2f} | {row['pass_at_1_pct']:.2f} | {row['functional']:.2f} | {row['memory']:.2f} | {row['safety']:.2f} | {tokens} | {duration} | {row['provenance']} |")
    lines += _task_matrix(comparison, markdown=True)
    lines += ["", "### Generation conditions"] + [f"- {row['model']}: {row['generation_settings']}" for row in comparison["models"]]
    lines += ["", "### Generation costs"] + [f"- {row['model']}: USD {row['total_cost_usd']}; known subtotal {row['known_cost_usd']}; "
                                              f"complete {row['cost_tasks']}/{row['tasks']} tasks; cost per passing task {row['cost_per_passed_task']}"
                                              for row in comparison['models']]
    lines += ["", "### Request and turn coverage"] + [row['model'] + ': ' + coverage_text(row) for row in comparison['models']]
    lines += ['', '### Independent sample estimates'] + [f"- {row['model']}: {row['samples']} samples; sample SD {row['score_sample_stddev']}; " + interval_text(row['score_mean_ci95_approx']) + f"; Pass@k (%) {row['pass_at_k']}" for row in comparison['models']]
    return "\n".join(lines)


def _task_matrix(comparison, markdown):
    def clean(value):
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    ids = comparison["task_ids"]
    lines = ["", "### Per-task totals" if markdown else "Per-task totals", ""]
    if markdown:
        lines += ["| Model | " + " | ".join(clean(item) for item in ids) + " |",
                  "| :--- | " + " | ".join("---:" for _ in ids) + " |"]
    else:
        lines.append("Model | " + " | ".join(ids))
    coverage_notes = []
    for model in (row["model"] for row in comparison["models"]):
        tasks = {row["task_id"]: row for row in comparison["task_rows"] if row["model"] == model}
        values = [f"{tasks[item]['total']:.2f}" for item in ids]
        lines.append(("| " if markdown else "") + clean(model) + " | " + " | ".join(values) + (" |" if markdown else ""))
        unavailable = [item for item in ids if not tasks[item]["measured"]]
        if unavailable:
            coverage_notes.append(f"Memory unavailable ({clean(model)}): " + ", ".join(clean(item) for item in unavailable))
    if coverage_notes:
        lines += [""] + coverage_notes
    gap = comparison["largest_gap"]
    lines += ["", f"Largest gap: {clean(gap['task_id'])} ({clean(gap['high']['model'])} {gap['high']['total']:.2f} vs "
                      f"{clean(gap['low']['model'])} {gap['low']['total']:.2f})."]
    return lines
