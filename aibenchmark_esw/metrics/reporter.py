from dataclasses import asdict
from typing import Any, Optional

from aibenchmark_esw.metrics.text import markdown_text
from aibenchmark_esw.metrics.validation import (
    validate_composite_score,
    validate_run_state,
    validate_task_result,
)
from aibenchmark_esw.models import (
    DimensionScores,
    SizeMetrics,
    StaticSafetyMetrics,
    TaskEvaluationResult,
    TaskLimits,
    TaskWeights,
    TestResult,
)


class BenchmarkReporter:
    @staticmethod
    def generate_html(results, model_name, metadata=None):
        from aibenchmark_esw.metrics.exports import report_html
        return report_html(results, model_name, metadata)

    @staticmethod
    def _all_tests_passed(result: TaskEvaluationResult) -> bool:
        tests = result.test_result
        return (result.compiled is True and tests.completed is True and tests.passed is True
                and tests.total_tests > 0 and tests.passed_tests == tests.total_tests
                and tests.failed_tests == tests.ignored_tests == 0)

    @staticmethod
    def _weight_summary(results: list[TaskEvaluationResult], dimension: str) -> str:
        if any(result.weights is None for result in results):
            return "Unknown (legacy report)"
        values = {getattr(result.weights, dimension) for result in results}
        return f"{next(iter(values)) * 100:g}%" if len(values) == 1 else "Varies by task"

    @staticmethod
    def _task_weights(result: TaskEvaluationResult) -> str:
        if result.weights is None:
            return "Unknown"
        return "/".join(f"{getattr(result.weights, key) * 100:g}%"
                        for key in ("functional", "memory", "safety"))

    @staticmethod
    def _standard(result: TaskEvaluationResult) -> str:
        if result.target_standard is None:
            return "Unknown"
        if result.effective_standard is None:
            return f"{result.target_standard} (not run)"
        if result.target_standard != result.effective_standard:
            return f"{result.target_standard}->{result.effective_standard}"
        return result.effective_standard

    @staticmethod
    def _expected_flat_summaries(results: list[TaskEvaluationResult]) -> dict[str, float]:
        return {
            "overall_score": round(sum(r.scores.total_score for r in results) / max(1, len(results)), 2),
            "pass_at_1_pct": round(sum(1 for r in results if BenchmarkReporter._all_tests_passed(r)) / max(1, len(results)) * 100.0, 2),
        }

    @staticmethod
    def from_json_dict(data: dict[str, Any], *, strict_summaries: bool = False) -> list[TaskEvaluationResult]:
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("Results must be an object containing a tasks array")
        version = data.get("schema_version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
            raise ValueError(f"Unsupported report schema version: {version}")
        for field in ("overall_score", "pass_at_1_pct", "tier_weighted_score"):
            value = data.get(field)
            if field in data and (isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 100):
                raise ValueError(f"{field} must be a finite score from 0 to 100")
        if "model_name" in data and (not isinstance(data["model_name"], str) or not data["model_name"].strip()):
            raise ValueError("model_name must be a nonempty string")
        if data.get("metadata") is not None and not isinstance(data["metadata"], dict):
            raise ValueError("Run metadata must be an object")
        for key in ("compiler", "platform", "generation_settings", "static_analysis", "execution_settings", "task_categories", "cost_budget", "run_options"):
            value = (data.get("metadata") or {}).get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"Metadata {key} must be an object")
        categories = (data.get('metadata') or {}).get('task_categories') or {}
        if any(not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip() for key, value in categories.items()):
            raise ValueError('Task categories must map nonempty task names to category strings')
        if "generation_summary" in data:
            from aibenchmark_esw.metrics.measurements import measurement_summary
            summary = data["generation_summary"]
            if not isinstance(summary, dict) or set(measurement_summary([])) - set(summary):
                raise ValueError("generation_summary must contain the measurement summary fields")
            for key in measurement_summary([]):
                value = summary.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or value < 0 or value != value or value == float('inf')):
                    raise ValueError("Invalid generation_summary measurement: " + key)
        results = []
        samples = data.get("samples")
        if 'sampling' in data and 'samples' not in data:
            raise ValueError('sampling requires a samples array')
        if 'samples' in data:
            if not isinstance(samples, list):
                raise ValueError("samples must be an array of independent reports")
            for sample in samples:
                if not isinstance(sample, dict) or "samples" in sample:
                    raise ValueError("Each sample must be a full report without nested samples")
                BenchmarkReporter.from_json_dict(sample)
            sampling = data.get("sampling")
            if not isinstance(sampling, dict):
                raise ValueError("sampling must describe the independent samples")
            requested, completed, pending = (sampling.get(key) for key in ("requested", "completed", "pending"))
            if (isinstance(requested, bool) or not isinstance(requested, int) or requested < 1
                    or isinstance(completed, bool) or not isinstance(completed, int)
                    or completed != len(samples) or completed > requested
                    or not isinstance(pending, list)
                    or any(isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < requested for index in pending)
                    or len(set(pending)) != len(pending) or len(pending) != requested - completed):
                raise ValueError("sampling counts and pending indexes must match the independent samples")
            if (data.get("metadata") or {}).get("run_status") == "completed" and (pending or not samples):
                raise ValueError("Completed sampling collections cannot contain pending samples")
            active_sample = sampling.get("active_sample")
            if active_sample is not None and (isinstance(active_sample, bool) or not isinstance(active_sample, int)
                                              or active_sample not in pending):
                raise ValueError("Active sample must identify a pending sample")
            pass_k = sampling.get("pass_k", [1])
            if (not isinstance(pass_k, list) or any(isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= requested for k in pass_k)
                    or len(set(pass_k)) != len(pass_k)):
                raise ValueError("sampling pass_k must contain distinct valid sample counts")
            sample_ids = [(sample.get("metadata") or {}).get("run_id") for sample in samples]
            if (any(not isinstance(run_id, str) or not run_id.strip() for run_id in sample_ids)
                    or len(set(sample_ids)) != len(sample_ids)
                    or (data.get("metadata") or {}).get("run_id") in sample_ids):
                raise ValueError("Independent samples require distinct recorded run IDs")
            task_ids = {item["task_id"] for item in data["tasks"] if isinstance(item, dict) and isinstance(item.get("task_id"), str)}
            for sample in samples:
                if ((sample.get("metadata") or {}).get("run_status") != "completed"
                        or sample.get("model_name") != data.get("model_name")
                        or {item["task_id"] for item in sample["tasks"]} != task_ids):
                    raise ValueError("Independent samples must be completed reports with the same model and task set")
                from aibenchmark_esw.metrics.comparison import (
                    validate_collection_conditions,
                )
                validate_collection_conditions(data, sample)
            active = sampling.get('active_report')
            if active is not None:
                if not isinstance(active, dict) or 'samples' in active or active_sample is None:
                    raise ValueError('active_report must describe the active pending sample')
                BenchmarkReporter.from_json_dict(active)
                active_id = (active.get('metadata') or {}).get('run_id')
                if (not isinstance(active_id, str) or not active_id.strip() or active_id in sample_ids
                        or active_id == (data.get('metadata') or {}).get('run_id')):
                    raise ValueError('active_report run_id must be a nonempty string distinct from '
                                     'completed sample and collection run IDs')
                from aibenchmark_esw.metrics.comparison import (
                    validate_collection_conditions,
                )
                validate_collection_conditions(data, active)
            if "statistics" in sampling:
                from aibenchmark_esw.sampling import sampling_statistics
                expected_statistics = sampling_statistics(samples, pass_k)
                if sampling["statistics"] != expected_statistics:
                    raise ValueError("Sampling statistics disagree with the independent sample results")
        policy = (data.get("metadata") or {}).get("scoring_policy")
        local_loader = None
        if policy is not None:
            from aibenchmark_esw.metrics.scorer import (
                SAFETY_ERROR_PENALTY,
                SAFETY_WARNING_PENALTY,
                SCORING_FORMULA_VERSION,
            )
            if (not isinstance(policy, dict) or policy.get("formula_version") != SCORING_FORMULA_VERSION
                    or policy.get("safety_error_penalty") != SAFETY_ERROR_PENALTY
                    or policy.get("safety_warning_penalty") != SAFETY_WARNING_PENALTY
                    or not isinstance(policy.get("tasks"), dict)):
                raise ValueError("Unknown or invalid scoring_policy")
            from aibenchmark_esw.dataset import DatasetLoader
            try:
                local_loader = DatasetLoader()
            except (OSError, ValueError):
                pass  # Optional local corroboration must not prevent portable reads.
        for item in data["tasks"]:
            if not isinstance(item, dict):
                raise ValueError("Each task result must be an object")
            validate_task_result(item, data.get("model_name", "unknown"))
            if "model_name" in data and item.get("model_name", data["model_name"]) != data["model_name"]:
                raise ValueError("Task model names must match the run model")
            if policy is not None:
                expected = policy["tasks"].get(item["task_id"])
                if not isinstance(expected, dict) or any(item.get(key) != expected.get(key) or item.get(key) is None
                        for key in ("limits", "weights")):
                    raise ValueError("Task limits/weights disagree with recorded scoring_policy")
                # Corroborate a bundled task only when its fingerprint identifies
                # the local configuration; external datasets remain portable.
                from dataclasses import replace

                from aibenchmark_esw.dataset import DatasetLoader
                from aibenchmark_esw.provenance import task_sha256
                from aibenchmark_esw.resources import data_root
                local = local_loader.get_task(item["task_id"]) if local_loader is not None else None
                if local is not None and item.get("footprint_target", "host") != "host":
                    target = item["footprint_target"]
                    effective_limits = local.target_limits.get(target) or local.target_limits.get(target.split(":")[-1])
                    if effective_limits is not None:
                        local = replace(local, limits=effective_limits)
                fingerprint = (item.get("provenance") or {}).get("task_sha256")
                if local is not None and fingerprint == task_sha256(local, data_root() / "third_party" / "unity"):
                    if expected != {"limits": asdict(local.limits), "weights": asdict(local.weights)}:
                        raise ValueError("Scoring policy disagrees with the referenced task configuration")
            for key in ("generation", "provenance"):
                if item.get(key) is not None and not isinstance(item[key], dict):
                    raise ValueError(f"Task {key} must be an object")
            if item.get("limits") is not None and not isinstance(item["limits"], dict):
                raise ValueError("Task limits must be an object")
            usage = (item.get("generation") or {}).get("usage")
            if usage is not None and not isinstance(usage, dict):
                raise ValueError("Generation usage must be an object")
            tests = item["test_result"]
            size = item["size_metrics"]
            safety = item["safety_metrics"]
            scores = item["scores"]
            from aibenchmark_esw.sandbox.stack_usage import validate_stack_usage
            validate_stack_usage(item.get('stack_usage'))
            results.append(TaskEvaluationResult(
                task_id=item["task_id"], tier=item["tier"],
                model_name=item["model_name"] if "model_name" in item else data.get("model_name", "unknown"),
                compiled=item["compiled"],
                test_result=TestResult(
                    total_tests=tests["total"], passed_tests=tests["passed"],
                    failed_tests=tests["failed"], ignored_tests=tests.get("ignored", 0),
                    passed=tests["all_passed"], completed=tests.get("completed", tests["total"] > 0),
                    returncode=tests.get("returncode"),
                    output=tests.get("output", ""), output_truncated=tests.get("output_truncated", False),
                ),
                size_metrics=SizeMetrics(**size), safety_metrics=StaticSafetyMetrics(**safety),
                scores=DimensionScores(scores["functional"], scores["memory"],
                                       scores["safety"], scores["total"]),
                execution_time_sec=item["execution_time_sec"], error_log=item.get("error_log"),
                weights=TaskWeights(**item["weights"]) if item.get("weights") is not None else None,
                target_standard=item.get("target_standard"),
                effective_standard=item.get("effective_standard"),
                generation=item.get("generation"),
                provenance=item.get("provenance"),
                limits=TaskLimits(**item["limits"]) if item.get("limits") is not None else None,
                candidate_time_sec=item.get("candidate_time_sec"),
                reference_validation_time_sec=item.get("reference_validation_time_sec"),
                footprint_target=item.get("footprint_target", "host"),
                stack_usage=item.get('stack_usage'),
            ))
            validate_composite_score(results[-1])
        if len({result.task_id for result in results}) != len(results):
            raise ValueError("Duplicate task IDs in a report")
        validate_run_state(data.get("metadata") or {}, results)
        if 'samples' in data:
            from aibenchmark_esw.metrics.comparison import effective_standards
            for result in results:
                if len(effective_standards(data, result.task_id)) > 1:
                    raise ValueError('Collection child has incompatible task effective_standard: ' + result.task_id)
        from aibenchmark_esw.metrics.measurements import measurement_summary
        expected_summary = measurement_summary(results)
        if 'samples' in data:
            assert isinstance(samples, list)
            measured = [item for sample in samples for item in BenchmarkReporter.from_json_dict(sample)]
            active = data['sampling'].get('active_report')
            if active is not None:
                measured += BenchmarkReporter.from_json_dict(active)
            expected_summary = measurement_summary(measured)
        if 'generation_summary' in data:
            import math
            # Old interrupted collections may include spending from an active
            # child saved only beside the wrapper. Keep it explicitly unverified.
            unverifiable = ('samples' in data and data['sampling'].get('active_sample') is not None
                            and data['sampling'].get('active_report') is None)
            if not unverifiable:
                for key, expected in expected_summary.items():
                    recorded = data['generation_summary'].get(key)
                    if expected is None or recorded is None:
                        matches = expected is recorded
                    else:
                        matches = math.isclose(recorded, expected, rel_tol=1e-9, abs_tol=1e-6)
                        if isinstance(expected, int) and key not in ('total_cost_usd', 'known_cost_usd'):
                            matches = matches and isinstance(recorded, int) and not isinstance(recorded, bool)
                    if not matches:
                        raise ValueError('generation_summary disagrees with task/sample measurements: ' + key)
        from aibenchmark_esw.metrics.groups import grouped_scores
        expected_groups = grouped_scores(results, data.get('metadata'))
        for scope in ('by_tier', 'by_category'):
            if scope in data:
                if not isinstance(data[scope], dict):
                    raise ValueError(scope + ' must be a group object')
                for values in data[scope].values():
                    if (not isinstance(values, dict)
                            or any(type(values.get(key)) is not int for key in ('count', 'task_attempts'))
                            or any(isinstance(values.get(key), bool) or not isinstance(values.get(key), (int, float)) for key in ('mean_score', 'pass_at_1_pct'))):
                        raise ValueError(scope + ' contains invalid group measurements')
        for field in ('tier_weighted_score', 'tier_weight_policy', 'by_tier', 'by_category'):
            if field in data and data[field] != expected_groups[field]:
                raise ValueError(field + ' disagrees with task results')
        if strict_summaries and version == 2:
            expected_flat = BenchmarkReporter._expected_flat_summaries(results)
            for field, expected in expected_flat.items():
                if field in data and data[field] != expected:
                    raise ValueError(field + ' disagrees with task results')
        return results

    @staticmethod
    def generate_markdown(results: list[TaskEvaluationResult], model_name: str,
                          metadata: Optional[dict[str, Any]] = None) -> str:
        if not results:
            return "No benchmark results available."

        total_tasks = len(results)
        compiled_tasks = sum(1 for r in results if r.compiled)
        all_passed_tasks = sum(1 for r in results if BenchmarkReporter._all_tests_passed(r))

        avg_func = sum(r.scores.functional_score for r in results) / total_tasks
        from aibenchmark_esw.metrics.measurements import (
            dimension_summary,
            dimension_text,
        )
        dimensions = dimension_summary(results)
        avg_mem = dimension_text(dimensions['memory'])
        avg_safe = dimension_text(dimensions['safety'])
        avg_total = sum(r.scores.total_score for r in results) / total_tasks

        md = []
        md.append(f"# AIBenchMark-ESW Benchmark Report: {markdown_text(model_name)}\n")
        md += [markdown_text(line) for line in BenchmarkReporter.sampling_lines(metadata)]
        warning = BenchmarkReporter.run_warning(metadata)
        if warning:
            md.append(f"**{warning}**\n")
        if (metadata or {}).get("unreadable_assets"):
            md.append("**Dataset provenance is incomplete because some input assets were unreadable.**\n")
        md.append("### Summary Overview")
        md.append(f"- **Total Tasks**: {total_tasks}")
        md.append(f"- **Compilation Rate**: {compiled_tasks}/{total_tasks} ({compiled_tasks/total_tasks*100:.1f}%)")
        qualifier = " (first sample)" if (metadata or {}).get('sampling') else ""
        md.append(f"- **Pass@1 (All Tests Passed){qualifier}**: {all_passed_tasks}/{total_tasks} ({all_passed_tasks/total_tasks*100:.1f}%)")
        md.append(f"- **Overall AIBenchMark-ESW Score{qualifier}**: **{avg_total:.2f} / 100.0**\n")
        from aibenchmark_esw.metrics.groups import breakdown_markdown, grouped_scores
        groups = grouped_scores(results, metadata)
        md.append(f"- **Tier-weighted score{qualifier}**: **{groups['tier_weighted_score']:.2f} / 100.0** (weights proportional to tier)")
        md += breakdown_markdown([dict(model=model_name, **groups)])

        if metadata:
            compiler = metadata.get("compiler") or {}
            md.append("### Reproducibility")
            md.append(f"- **Run (UTC)**: {metadata.get('created_at_utc', 'Unknown')}")
            md.append(f"- **Run status**: {metadata.get('run_status', 'Unknown (legacy report)')}")
            if metadata.get('run_mode') == 'replay':
                md.append(f"- **Evaluation mode**: offline replay; origin run `{metadata.get('origin_run_id', 'Unknown')}`; fixed sources, no new generation or spend")
            md.append(f"- **Benchmark / Python**: {metadata.get('benchmark_version', 'Unknown')} / {metadata.get('python_version', 'Unknown')}")
            md.append(f"- **Compiler**: {compiler.get('name', 'Unknown')} / {compiler.get('version') or 'Unknown'} ({compiler.get('optimization', 'Unknown')})")
            analysis = metadata.get("static_analysis") or {}
            md.append(f"- **Static analysis**: {analysis.get('engine', 'Unknown')} / {analysis.get('cppcheck_version') or 'No cppcheck version recorded'}")
            md.append(f"- **Dataset SHA-256**: `{metadata.get('dataset_sha256', 'Unknown')}`")
            md.append(f"- **Evaluator SHA-256**: `{metadata.get('evaluator_sha256', 'Unknown')}`")
            md.append(f"- **Source revision**: `{metadata.get('source_revision') or 'Unavailable in installed distribution'}`")
            md.append(f"- **Source had local changes**: {metadata.get('source_dirty', 'Unknown')}\n")

        md.append("### Dimensional Scores")
        md.append("| Dimension | Average Score | Weight |")
        md.append("| :--- | :--- | :--- |")
        md.append(f"| **Functional Correctness** | {avg_func:.2f} / 100 | {BenchmarkReporter._weight_summary(results, 'functional')} |")
        md.append(f"| **Memory Efficiency** | {avg_mem} / 100 | {BenchmarkReporter._weight_summary(results, 'memory')} |")
        md.append(f"| **Safety & Code Rules** | {avg_safe} / 100 | {BenchmarkReporter._weight_summary(results, 'safety')} |")
        md.append(f"Measured dimensions: memory {dimensions['memory_measured_attempts']}/{total_tasks}, "
                  f"safety {dimensions['safety_measured_attempts']}/{total_tasks} task attempts.\n")
        md.append(f"| **Composite Score** | **{avg_total:.2f} / 100** | 100% |\n")

        md.append("### Detailed Task Breakdown")
        md.append("Weights below are functional/memory/safety. Memory and safety contributions are scaled by the functional pass fraction.\n")
        md.append("| Tier | Task ID | Standard | Weights | Compile | Tests Passed | Flash/RAM (B) | Safety | Score | Time |")
        md.append("| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

        for r in results:
            status_comp = "PASS" if r.compiled else "FAIL"
            test_str = f"{r.test_result.passed_tests}/{r.test_result.total_tests}"
            if not r.test_result.completed:
                test_str += " (incomplete)"
            mem_str = (f"{r.size_metrics.flash_bytes} / {r.size_metrics.ram_bytes}"
                       if r.size_metrics.measured else "Unavailable")
            safe_str = f"Err:{r.safety_metrics.error_count}, Warn:{r.safety_metrics.warning_count}"
            score_str = f"**{r.scores.total_score:.1f}**"
            time_str = f"{r.execution_time_sec:.2f}s"
            md.append(f"| {r.tier} | {markdown_text(r.task_id)} | {BenchmarkReporter._standard(r)} | {BenchmarkReporter._task_weights(r)} | {status_comp} | {test_str} | {mem_str} | {safe_str} | {score_str} | {time_str} |")

        for r in results:
            if r.footprint_target != "host" and r.limits is not None:
                md.append(f"\nFootprint target: {r.footprint_target}; {r.task_id} budget Flash/RAM="
                          f"{r.limits.max_flash_bytes}/{r.limits.max_ram_bytes}B\n")

        from aibenchmark_esw.sandbox.stack_usage import stack_usage_lines
        stack_lines = stack_usage_lines(results)
        if stack_lines:
            md += ['', '### Compiler stack evidence', '', *stack_lines]
        from aibenchmark_esw.metrics.measurements import render_measurements
        measurements = render_measurements(results, (metadata or {}).get('collection_generation_summary'))
        if measurements:
            md += ["", "### Generation measurements", "", *measurements]

        warnings = BenchmarkReporter.analysis_warnings(results) + BenchmarkReporter.scoring_warnings(results, metadata)
        if warnings:
            md += ["", "### Validation and analysis coverage", ""]
            md += [f"- {markdown_text(warning)}" for warning in warnings]
        findings = [(result.task_id, finding) for result in results for finding in result.safety_metrics.findings]
        if findings:
            md += ["", "### Static-analysis findings", ""]
            for task_id, finding in findings:
                message = markdown_text(finding['message'])
                md.append(f"- `{markdown_text(task_id)}` [{markdown_text(finding['engine'])} / {markdown_text(finding['rule_id'])} / {markdown_text(finding['severity'])}] "
                          f"{markdown_text(finding['file'])}:{finding.get('line') or '?'}: {message}")

        return "\n".join(md)

    @staticmethod
    def generate_cli_table(results: list[TaskEvaluationResult], model_name: str,
                           metadata: Optional[dict[str, Any]] = None) -> str:
        lines = []
        lines.append("=" * 110)
        lines.append(f" AIBenchMark-ESW Benchmark Results - Model: {model_name}")
        lines += BenchmarkReporter.sampling_lines(metadata)
        lines.append("=" * 110)
        header = f"{'Tier':<5} {'Task ID':<22} {'Standard':<14} {'Weights F/M/S':<16} {'Comp':<6} {'Tests':<8} {'Flash/RAM':<14} {'Score':<8}"
        lines.append(header)
        lines.append("-" * 110)

        for r in results:
            if r.footprint_target != "host" and r.limits is not None:
                lines.append(f"Target {r.footprint_target}: {r.task_id} Flash/RAM budget {r.limits.max_flash_bytes}/{r.limits.max_ram_bytes}B")
            comp = "PASS" if r.compiled else "FAIL"
            tests = (f"{r.test_result.passed_tests}/{r.test_result.total_tests}"
                     if r.test_result.completed else "INCOMP")
            mem = (f"{r.size_metrics.flash_bytes}/{r.size_metrics.ram_bytes}B"
                   if r.size_metrics.measured else "Unavailable")
            score = f"{r.scores.total_score:.1f}"
            standard = BenchmarkReporter._standard(r)
            weights = BenchmarkReporter._task_weights(r)
            lines.append(f"{r.tier:<5} {r.task_id:<22} {standard:<14} {weights:<16} {comp:<6} {tests:<8} {mem:<14} {score:<8}")

        lines.append("-" * 110)
        avg_total = sum(r.scores.total_score for r in results) / max(1, len(results))
        pass_at_1 = sum(1 for r in results if BenchmarkReporter._all_tests_passed(r)) / max(1, len(results)) * 100.0
        qualifier = " (first sample)" if (metadata or {}).get('sampling') else ""
        lines.append(f"Final Score{qualifier}: {avg_total:.2f}/100.0 | Pass@1{qualifier}: {pass_at_1:.1f}%")
        from aibenchmark_esw.metrics.groups import grouped_scores
        lines.append(f"Tier-weighted score{qualifier}: {grouped_scores(results, metadata)['tier_weighted_score']:.2f}/100.0")
        lines.append("=" * 110)
        lines += [f"Warning: {warning}" for warning in
                  BenchmarkReporter.analysis_warnings(results) + BenchmarkReporter.scoring_warnings(results, metadata)]
        warning = BenchmarkReporter.run_warning(metadata)
        if warning:
            lines.append(warning)
        if (metadata or {}).get('run_mode') == 'replay':
            lines.append(f"Offline replay; origin run {(metadata or {}).get('origin_run_id')}; fixed sources, no new generation or spend.")
        if (metadata or {}).get("unreadable_assets"):
            lines.append("Dataset provenance is incomplete because some input assets were unreadable.")
        for result in results:
            for finding in result.safety_metrics.findings:
                lines.append(f"[{result.task_id}] {finding['engine']} / {finding['rule_id']} / {finding['severity']}: "
                             f"{finding['file']}:{finding.get('line') or '?'}: {finding['message']}")
        from aibenchmark_esw.metrics.measurements import render_measurements
        lines += render_measurements(results, (metadata or {}).get('collection_generation_summary'))
        return "\n".join(lines)

    @staticmethod
    def sampling_lines(metadata):
        sampling = (metadata or {}).get('sampling')
        if not sampling:
            return []
        import json
        lines = ['', 'Independent sample estimates',
                 f"Samples: {sampling['completed']}/{sampling['requested']} completed; pending {len(sampling['pending'])}.",
                 'Task dimensions below describe the first sample. Collection estimates use completed independent samples.']
        for task_id, stats in sampling.get('statistics', {}).items():
            pass_k = {key: value * 100 if value is not None else None for key, value in stats['pass_at_k'].items()}
            lines.append(f"{task_id}: {stats['attempts']} samples; {stats['passed']} passes; mean score {stats['mean_score']}; "
                         f"sample SD {stats.get('sample_stddev')}; approximate 95% mean interval {stats.get('approximate_95pct_mean_interval')}; "
                         f"Pass@k (%) {json.dumps(pass_k, sort_keys=True)}")
        lines.append('Intervals use Student-t critical values and assume independent samples; one-sample intervals are unavailable.')
        return lines + ['']

    @staticmethod
    def run_warning(metadata):
        metadata = metadata or {}
        if metadata.get("run_status") in ("running", "interrupted", "aborted"):
            if metadata.get('sampling'):
                sampling = metadata['sampling']
                warning = f"Run status: {metadata['run_status']}; {len(sampling['pending'])} samples pending. This collection is unfinished."
                if sampling.get('active_sample') is not None and sampling.get('active_report') is None:
                    warning += ' Active sample measurements are unverified because its report is not embedded; spending is a recorded subtotal.'
                return warning
            return (f"Run status: {metadata['run_status']}; {len(metadata.get('pending_tasks', []))} "
                    "tasks pending (included as zero). This run is unfinished.")
        return None

    @staticmethod
    def analysis_warnings(results: list[TaskEvaluationResult]) -> list[str]:
        warnings = []
        for result in results:
            similarity = (result.generation or {}).get('reference_similarity_warning')
            if isinstance(similarity, str) and similarity.strip():
                warnings.append(result.task_id + ': ' + similarity)
            status = result.safety_metrics.cppcheck_status
            if status in ("failed", "timeout"):
                warnings.append(f"{result.task_id}: cppcheck {status}; safety score uses built-in rules only.")
            elif result.compiled and result.test_result.completed and status in (None, "not_run"):
                warnings.append(f"{result.task_id}: static-analysis coverage was not recorded or did not run.")
        return warnings

    @staticmethod
    def scoring_warnings(results: list[TaskEvaluationResult],
                         metadata: Optional[dict[str, Any]] = None) -> list[str]:
        missing = [result.task_id for result in results
                   if result.compiled and result.test_result.completed
                   and result.size_metrics.measured and result.limits is None]
        warnings = (["Memory scores cannot be independently verified without recorded task limits: "
                     + ", ".join(missing) + "."] if missing else [])
        policy_tasks = ((metadata or {}).get("scoring_policy") or {}).get("tasks", {})
        unverified = [result.task_id for result in results if result.limits is not None
                      and policy_tasks.get(result.task_id) != {
                          "limits": asdict(result.limits),
                          "weights": asdict(result.weights) if result.weights is not None else None}]
        if unverified or not (metadata or {}).get("scoring_policy"):
            warnings.append("Legacy scoring policy is unverifiable; limits and weights are report-supplied."
                            + (" Unverified tasks: " + ", ".join(unverified) + "." if unverified else ""))
        return warnings

    @staticmethod
    def to_json_dict(results: list[TaskEvaluationResult], model_name: str,
                     metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        from aibenchmark_esw.metrics.groups import grouped_scores
        from aibenchmark_esw.metrics.measurements import measurement_summary
        return {
            "schema_version": 2,
            "metadata": metadata,
            "model_name": model_name,
            "overall_score": round(sum(r.scores.total_score for r in results) / max(1, len(results)), 2),
            "pass_at_1_pct": round(sum(1 for r in results if BenchmarkReporter._all_tests_passed(r)) / max(1, len(results)) * 100.0, 2),
            "tasks": [r.to_dict() for r in results],
            "generation_summary": measurement_summary(results),
            **grouped_scores(results, metadata),
        }
