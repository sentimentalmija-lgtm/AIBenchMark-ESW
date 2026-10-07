from typing import List, Dict, Any
from typing import Optional
from aibenchmark_esw.metrics.validation import validate_task_result, validate_composite_score, validate_run_state
from aibenchmark_esw.models import (
    TaskEvaluationResult, TestResult, SizeMetrics, StaticSafetyMetrics, DimensionScores, TaskWeights, TaskLimits,
)


class BenchmarkReporter:
    @staticmethod
    def _all_tests_passed(result: TaskEvaluationResult) -> bool:
        tests = result.test_result
        return (result.compiled is True and tests.completed is True and tests.passed is True
                and tests.total_tests > 0 and tests.passed_tests == tests.total_tests
                and tests.failed_tests == tests.ignored_tests == 0)

    @staticmethod
    def _weight_summary(results: List[TaskEvaluationResult], dimension: str) -> str:
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
    def from_json_dict(data: Dict[str, Any]) -> List[TaskEvaluationResult]:
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("Results must be an object containing a tasks array")
        version = data.get("schema_version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2):
            raise ValueError(f"Unsupported report schema version: {version}")
        for field in ("overall_score", "pass_at_1_pct"):
            value = data.get(field)
            if field in data and (isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 100):
                raise ValueError(f"{field} must be a finite score from 0 to 100")
        if "model_name" in data and (not isinstance(data["model_name"], str) or not data["model_name"].strip()):
            raise ValueError("model_name must be a nonempty string")
        if data.get("metadata") is not None and not isinstance(data["metadata"], dict):
            raise ValueError("Run metadata must be an object")
        for key in ("compiler", "platform", "generation_settings", "static_analysis", "execution_settings"):
            value = (data.get("metadata") or {}).get(key)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"Metadata {key} must be an object")
        results = []
        policy = (data.get("metadata") or {}).get("scoring_policy")
        if policy is not None:
            from aibenchmark_esw.metrics.scorer import SCORING_FORMULA_VERSION, SAFETY_ERROR_PENALTY, SAFETY_WARNING_PENALTY
            if (not isinstance(policy, dict) or policy.get("formula_version") != SCORING_FORMULA_VERSION
                    or policy.get("safety_error_penalty") != SAFETY_ERROR_PENALTY
                    or policy.get("safety_warning_penalty") != SAFETY_WARNING_PENALTY
                    or not isinstance(policy.get("tasks"), dict)):
                raise ValueError("Unknown or invalid scoring_policy")
        for item in data["tasks"]:
            if not isinstance(item, dict):
                raise ValueError("Each task result must be an object")
            validate_task_result(item, data.get("model_name", "unknown"))
            if policy is not None:
                expected = policy["tasks"].get(item["task_id"])
                if not isinstance(expected, dict) or any(item.get(key) != expected.get(key) or item.get(key) is None
                        for key in ("limits", "weights")):
                    raise ValueError("Task limits/weights disagree with recorded scoring_policy")
                # Corroborate a bundled task only when its fingerprint identifies
                # the local configuration; external datasets remain portable.
                from dataclasses import asdict, replace
                from aibenchmark_esw.dataset import DatasetLoader
                from aibenchmark_esw.provenance import task_sha256
                from aibenchmark_esw.resources import data_root
                local = DatasetLoader().get_task(item["task_id"])
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
            results.append(TaskEvaluationResult(
                task_id=item["task_id"], tier=item["tier"],
                model_name=item["model_name"] if "model_name" in item else data.get("model_name", "unknown"),
                compiled=item["compiled"],
                test_result=TestResult(
                    total_tests=tests["total"], passed_tests=tests["passed"],
                    failed_tests=tests["failed"], ignored_tests=tests.get("ignored", 0),
                    passed=tests["all_passed"], completed=tests.get("completed", tests["total"] > 0),
                    returncode=tests.get("returncode"),
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
            ))
            validate_composite_score(results[-1])
        if len({result.task_id for result in results}) != len(results):
            raise ValueError("Duplicate task IDs in a report")
        validate_run_state(data.get("metadata") or {}, results)
        return results

    @staticmethod
    def generate_markdown(results: List[TaskEvaluationResult], model_name: str,
                          metadata: Optional[Dict[str, Any]] = None) -> str:
        if not results:
            return "No benchmark results available."

        total_tasks = len(results)
        compiled_tasks = sum(1 for r in results if r.compiled)
        all_passed_tasks = sum(1 for r in results if BenchmarkReporter._all_tests_passed(r))

        avg_func = sum(r.scores.functional_score for r in results) / total_tasks
        avg_mem = sum(r.scores.memory_score for r in results) / total_tasks
        avg_safe = sum(r.scores.safety_score for r in results) / total_tasks
        avg_total = sum(r.scores.total_score for r in results) / total_tasks

        md = []
        md.append(f"# AIBenchMark-ESW Benchmark Report: `{model_name}`\n")
        warning = BenchmarkReporter.run_warning(metadata)
        if warning:
            md.append(f"**{warning}**\n")
        if (metadata or {}).get("unreadable_assets"):
            md.append("**Dataset provenance is incomplete because some input assets were unreadable.**\n")
        md.append("### Summary Overview")
        md.append(f"- **Total Tasks**: {total_tasks}")
        md.append(f"- **Compilation Rate**: {compiled_tasks}/{total_tasks} ({compiled_tasks/total_tasks*100:.1f}%)")
        md.append(f"- **Pass@1 (All Tests Passed)**: {all_passed_tasks}/{total_tasks} ({all_passed_tasks/total_tasks*100:.1f}%)")
        md.append(f"- **Overall AIBenchMark-ESW Score**: **{avg_total:.2f} / 100.0**\n")

        if metadata:
            compiler = metadata.get("compiler") or {}
            md.append("### Reproducibility")
            md.append(f"- **Run (UTC)**: {metadata.get('created_at_utc', 'Unknown')}")
            md.append(f"- **Run status**: {metadata.get('run_status', 'Unknown (legacy report)')}")
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
        md.append(f"| **Memory Efficiency** | {avg_mem:.2f} / 100 | {BenchmarkReporter._weight_summary(results, 'memory')} |")
        md.append(f"| **Safety & Code Rules** | {avg_safe:.2f} / 100 | {BenchmarkReporter._weight_summary(results, 'safety')} |")
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
            md.append(f"| {r.tier} | `{r.task_id}` | {BenchmarkReporter._standard(r)} | {BenchmarkReporter._task_weights(r)} | {status_comp} | {test_str} | {mem_str} | {safe_str} | {score_str} | {time_str} |")

        for r in results:
            if r.footprint_target != "host" and r.limits is not None:
                md.append(f"\nFootprint target: {r.footprint_target}; {r.task_id} budget Flash/RAM="
                          f"{r.limits.max_flash_bytes}/{r.limits.max_ram_bytes}B\n")

        warnings = BenchmarkReporter.analysis_warnings(results) + BenchmarkReporter.scoring_warnings(results)
        if not (metadata or {}).get("scoring_policy"):
            warnings.append("Legacy scoring policy is unverifiable; limits and weights are report-supplied.")
        if warnings:
            md += ["", "### Validation and analysis coverage", ""]
            md += [f"- {warning}" for warning in warnings]
        findings = [(result.task_id, finding) for result in results for finding in result.safety_metrics.findings]
        if findings:
            md += ["", "### Static-analysis findings", ""]
            for task_id, finding in findings:
                message = finding["message"].replace("|", "\\|").replace("\n", " ")
                md.append(f"- `{task_id}` [{finding['engine']} / {finding['rule_id']} / {finding['severity']}] "
                          f"{finding['file']}:{finding['line'] or '?'}: {message}")

        return "\n".join(md)

    @staticmethod
    def generate_cli_table(results: List[TaskEvaluationResult], model_name: str,
                           metadata: Optional[Dict[str, Any]] = None) -> str:
        lines = []
        lines.append("=" * 110)
        lines.append(f" AIBenchMark-ESW Benchmark Results - Model: {model_name}")
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
        lines.append(f"Final Score: {avg_total:.2f}/100.0 | Pass@1: {pass_at_1:.1f}%")
        lines.append("=" * 110)
        lines += [f"Warning: {warning}" for warning in
                  BenchmarkReporter.analysis_warnings(results) + BenchmarkReporter.scoring_warnings(results)]
        warning = BenchmarkReporter.run_warning(metadata)
        if warning:
            lines.append(warning)
        if (metadata or {}).get("unreadable_assets"):
            lines.append("Dataset provenance is incomplete because some input assets were unreadable.")
        if not (metadata or {}).get("scoring_policy"):
            lines.append("Warning: Legacy scoring policy is unverifiable; limits and weights are report-supplied.")
        for result in results:
            for finding in result.safety_metrics.findings:
                lines.append(f"[{result.task_id}] {finding['engine']} / {finding['rule_id']} / {finding['severity']}: "
                             f"{finding['file']}:{finding['line'] or '?'}: {finding['message']}")
        return "\n".join(lines)

    @staticmethod
    def run_warning(metadata):
        metadata = metadata or {}
        if metadata.get("run_status") in ("running", "interrupted", "aborted"):
            return (f"Run status: {metadata['run_status']}; {len(metadata.get('pending_tasks', []))} "
                    "tasks pending (included as zero). This run is unfinished.")
        return None

    @staticmethod
    def analysis_warnings(results: List[TaskEvaluationResult]) -> List[str]:
        warnings = []
        for result in results:
            status = result.safety_metrics.cppcheck_status
            if status == "timeout":
                warnings.append(f"{result.task_id}: cppcheck timed out; safety score uses built-in rules only.")
            elif status == "failed":
                warnings.append(f"{result.task_id}: cppcheck failed; safety score uses built-in rules only.")
            elif result.compiled and result.test_result.completed and status in (None, "not_run"):
                warnings.append(f"{result.task_id}: static-analysis coverage was not recorded or did not run.")
        return warnings

    @staticmethod
    def scoring_warnings(results: List[TaskEvaluationResult]) -> List[str]:
        missing = [result.task_id for result in results
                   if result.compiled and result.test_result.completed
                   and result.size_metrics.measured and result.limits is None]
        return (["Memory scores cannot be independently verified without recorded task limits: "
                 + ", ".join(missing) + "."] if missing else [])

    @staticmethod
    def to_json_dict(results: List[TaskEvaluationResult], model_name: str,
                     metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "metadata": metadata,
            "model_name": model_name,
            "overall_score": round(sum(r.scores.total_score for r in results) / max(1, len(results)), 2),
            "pass_at_1_pct": round(sum(1 for r in results if BenchmarkReporter._all_tests_passed(r)) / max(1, len(results)) * 100.0, 2),
            "tasks": [r.to_dict() for r in results],
        }
