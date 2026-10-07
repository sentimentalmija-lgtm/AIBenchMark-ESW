import sys
import json
import argparse
import math
import copy
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import replace
from aibenchmark_esw.models import TaskConfig

from aibenchmark_esw.dataset import DatasetLoader, validate_task_assets
from aibenchmark_esw.sandbox.executor import ExecutionSandbox
from aibenchmark_esw.sandbox.static_analyzer import DEFAULT_CPPCHECK_TIMEOUT_SECONDS, StaticAnalyzer
from aibenchmark_esw.metrics.reporter import BenchmarkReporter
from aibenchmark_esw.llm.client import LLMClient, is_fatal_provider_error
from aibenchmark_esw.evaluation import evaluate_task, failed_evaluation
from aibenchmark_esw.provenance import collect_run_metadata, text_sha256
from aibenchmark_esw.metrics.comparison import compare_runs, render_comparison
from aibenchmark_esw.report_io import atomic_write_json, atomic_write_text
from aibenchmark_esw.output_paths import validate_output_paths
from aibenchmark_esw.resources import data_root
from aibenchmark_esw.reference_cache import ReferenceCache
from aibenchmark_esw.metrics.aggregation import aggregate_runs, render_aggregation
from aibenchmark_esw.metrics.junit import render_junit


class _Parser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        tokens = sys.argv[1:] if args is None else args
        parsed = super().parse_args(tokens, namespace)
        parsed._explicit_options = {str(token).split("=", 1)[0] for token in tokens if str(token).startswith("--")}
        return parsed


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _retries(value):
    number = int(value)
    if not 0 <= number <= 10:
        raise argparse.ArgumentTypeError("must be between 0 and 10")
    return number


def _backoff(value):
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 60:
        raise argparse.ArgumentTypeError("must be between 0 and 60")
    return number


def _temperature(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 2:
        raise argparse.ArgumentTypeError("must be a finite number between 0 and 2")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="aibenchmark-esw",
        description="AIBenchMark-ESW: Open-Source Embedded AI Coding Benchmark Framework",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: list
    list_p = subparsers.add_parser("list", help="List available benchmark tasks")
    list_p.add_argument("--tier", type=int, choices=[1, 2, 3, 4], help="Filter tasks by tier")

    validate_p = subparsers.add_parser("validate", help="Check required task files without compilation or API calls")
    validate_p.add_argument("--tier", type=int, choices=[1, 2, 3, 4], help="Validate tasks in this tier")
    validate_p.add_argument("--tasks", type=str, help="Comma-separated task IDs to validate")

    # Command: eval (local solution evaluation)
    eval_p = subparsers.add_parser("eval", help="Evaluate a local solution or reference implementation")
    eval_p.add_argument("--task", type=str, required=True, help="Task ID (e.g. tier1_ring_buffer)")
    input_group = eval_p.add_mutually_exclusive_group()
    input_group.add_argument("--solution", type=str, help="Path to C solution file")
    input_group.add_argument("--reference", action="store_true", help="Evaluate the built-in reference solution")
    eval_p.add_argument("--output", type=Path, help="Save evaluation JSON with reproducibility metadata")
    eval_p.add_argument("--compiler", type=str, help="Custom C compiler executable path")
    eval_p.add_argument("--allow-standard-fallback", action="store_true",
                        help="Allow MSVC to evaluate C99 tasks as C11; recorded in results")

    # Command: run (LLM or baseline benchmark)
    run_p = subparsers.add_parser("run", help="Run benchmark across tasks using an LLM or reference baseline")
    run_p.add_argument("--model", type=str, default="baseline", help="Provider/model ID available to your account, or 'baseline'")
    run_p.add_argument("--tier", type=int, choices=[1, 2, 3, 4], help="Run only tasks in this tier")
    run_p.add_argument("--tasks", type=str, help="Comma-separated task IDs to evaluate")
    run_p.add_argument("--output", type=str, help="Save JSON progress after every task and on interruption")
    run_p.add_argument("--compiler", type=str, help="Custom C compiler executable path")
    run_p.add_argument("--temperature", type=_temperature,
                       help="Optional provider-supported temperature; omitted by default")
    run_p.add_argument("--max-tokens", type=_positive_int, help="Maximum generation tokens per task")
    run_p.add_argument("--request-timeout", type=_positive_float, default=60.0,
                       help="Provider request timeout in seconds (default: 60)")
    run_p.add_argument("--save-solutions", type=Path,
                       help="Directory for extracted candidate C files, for local replay")
    run_p.add_argument("--allow-standard-fallback", action="store_true",
                       help="Allow MSVC to evaluate C99 tasks as C11; recorded in results")
    run_p.add_argument("--resume", type=Path, help="Resume pending tasks from a compatible checkpoint")
    run_p.add_argument("--jobs", type=_positive_int, default=1, help="Maximum concurrent task evaluations")
    run_p.add_argument("--preflight", action="store_true", help="Check tools and references before generation")
    run_p.add_argument("--max-retries", type=_retries, default=0)
    run_p.add_argument("--retry-backoff", type=_backoff, default=1.0)
    run_p.add_argument("--prompt-strategy", choices=["single", "plan"], default="single")
    run_p.add_argument("--multi-turn", dest="prompt_strategy", action="store_const", const="plan")
    run_p.add_argument("--review-turn", action="store_true")
    run_p.add_argument("--system-prompt-file", type=Path, help="UTF-8 system prompt override")

    # Command: report
    report_p = subparsers.add_parser("report", help="Generate report from evaluation JSON")
    report_p.add_argument("--results", type=str, required=True, help="Path to results JSON file")
    report_p.add_argument("--format", choices=["cli", "markdown", "junit"], default="cli", help="Output format")
    schema_p = subparsers.add_parser("validate-report", help="Validate report structure and cross-field consistency offline")
    schema_p.add_argument("results", type=Path, nargs="?")
    schema_p.add_argument("--schema", action="store_true", help="Print the portable JSON Schema")

    compare_p = subparsers.add_parser("compare", help="Compare saved model runs without API calls")
    compare_p.add_argument("--results", nargs="+", type=Path, required=True, help="Two or more saved JSON reports")
    compare_p.add_argument("--format", choices=["cli", "markdown", "csv", "csv-long"], default="markdown")
    compare_p.add_argument("--output", type=Path, help="Save the comparison to a file")

    aggregate_p = subparsers.add_parser("aggregate", help="Summarize independent repeated runs offline")
    aggregate_p.add_argument("--results", nargs="+", type=Path, required=True)
    aggregate_p.add_argument("--format", choices=["cli", "markdown", "json", "csv"], default="markdown")
    aggregate_p.add_argument("--output", type=Path)
    doctor_p = subparsers.add_parser("doctor", help="Check local tools without provider requests")
    doctor_p.add_argument("--check-references", action="store_true")
    doctor_p.add_argument("--tier", type=int, choices=[1, 2, 3, 4])
    doctor_p.add_argument("--tasks", type=str)
    mutation_p = subparsers.add_parser("mutations", help="Verify reviewed faulty candidates are rejected")
    mutation_p.add_argument("--tier", type=int, choices=[1, 2, 3, 4])
    mutation_p.add_argument("--tasks", type=str)
    mutation_p.add_argument("--output", type=Path)
    for command in (doctor_p, mutation_p):
        command.add_argument("--compiler", type=str)
        command.add_argument("--allow-standard-fallback", action="store_true")
    for command in (run_p, eval_p, doctor_p, mutation_p):
        command.add_argument("--target", help="Additional avr:atmega328p or arm:cortex-m0 target object")
        command.add_argument("--cross-compiler", help="Cross compiler executable (host tests retain --compiler)")
        command.add_argument("--compile-timeout", type=_positive_float, default=30.0)
        command.add_argument("--max-output-bytes", type=_positive_int, default=1048576)
        command.add_argument("--isolation", choices=["native", "process"], default="native")
        command.add_argument("--memory-limit-bytes", type=_positive_int)
        command.add_argument("--sanitizers", default="", help="Comma-separated address,undefined; requires GCC/Clang")
    for command in (run_p, eval_p):
        command.add_argument("--static-analysis-timeout", type=_positive_float,
                             default=DEFAULT_CPPCHECK_TIMEOUT_SECONDS,
                             help="Maximum cppcheck runtime in seconds (default: 30)")
        verbosity = command.add_mutually_exclusive_group()
        verbosity.add_argument("--quiet", action="store_true", help="Show final summary only")
        verbosity.add_argument("--verbose", action="store_true", help="Show full task diagnostics on stderr")
        command.add_argument("--junit-output", type=Path)
    for command in (list_p, eval_p, run_p, validate_p, doctor_p, mutation_p):
        command.add_argument("--category", type=str)

    for command in (list_p, eval_p, run_p, validate_p, doctor_p, mutation_p, compare_p, aggregate_p):
        command.add_argument("--tasks-root", type=Path,
                             help="Directory containing task folders (defaults to bundled tasks)")

    return parser


def _new_executor(args):
    sanitizers = getattr(args, "sanitizers", "")
    if isinstance(sanitizers, str):
        sanitizers = tuple(item.strip() for item in sanitizers.split(",") if item.strip())
    return ExecutionSandbox(compiler_path=getattr(args, "compiler", None),
        allow_standard_fallback=getattr(args, "allow_standard_fallback", False),
        compile_timeout_seconds=getattr(args, "compile_timeout", 30),
        max_output_bytes=getattr(args, "max_output_bytes", 1048576),
        isolation=getattr(args, "isolation", "native"),
        memory_limit_bytes=getattr(args, "memory_limit_bytes", None), sanitizers=sanitizers,
        target=getattr(args, "target", None), cross_compiler=getattr(args, "cross_compiler", None))


def _new_analyzer(args):
    return StaticAnalyzer(cppcheck_timeout_seconds=getattr(
        args, "static_analysis_timeout", DEFAULT_CPPCHECK_TIMEOUT_SECONDS))


def _target_task(task: TaskConfig, args) -> TaskConfig:
    target = getattr(args, "target", None)
    if target:
        limits = task.target_limits.get(target) or task.target_limits.get(target.split(":")[-1])
        if limits is not None:
            return replace(task, limits=limits)
    return task


def _new_client(args):
    return LLMClient(model_name=args.model, temperature=getattr(args, "temperature", None),
        max_tokens=getattr(args, "max_tokens", None), request_timeout=getattr(args, "request_timeout", 60),
        max_retries=getattr(args, "max_retries", 0), retry_backoff_seconds=getattr(args, "retry_backoff", 1),
        prompt_strategy=getattr(args, "prompt_strategy", "single"), review_turn=getattr(args, "review_turn", False))


def _benchmark_roots(args=None):
    roots = [data_root() / "tasks", data_root() / "third_party" / "unity", Path(__file__).resolve().parent]
    if args is not None and getattr(args, "tasks_root", None) is not None:
        roots.append(Path(args.tasks_root))
    return roots + [child for root in roots[:1] + roots[3:] if root.is_dir()
                    for child in root.iterdir() if child.is_dir()]


def _show_load_errors(loader):
    for path, error in loader.load_errors.items():
        print(f"Error: Invalid task at {path}: {error}", file=sys.stderr)


def _write_junit(args, results, model, metadata):
    if getattr(args, "junit_output", None):
        atomic_write_text(args.junit_output, render_junit(results, model, metadata))


def _task_diagnostics(args, result):
    if result.error_log:
        print(f"[{result.task_id}] {result.error_log}", file=sys.stderr)
    if getattr(args, "verbose", False) and result.test_result.output and result.test_result.output != result.error_log:
        print(f"[{result.task_id}] Test output:\n{result.test_result.output}", file=sys.stderr)


def cmd_list(args: argparse.Namespace) -> int:
    loader = DatasetLoader(getattr(args, "tasks_root", None), strict=False)
    _show_load_errors(loader)
    tasks = loader.list_tasks(tier=args.tier, category=getattr(args, "category", None))
    print("=" * 80)
    print(f" AIBenchMark-ESW Tasks (Total: {len(tasks)})")
    print("=" * 80)
    print(f"{'Tier':<6} {'Task ID':<24} {'Category':<20} {'Standard':<10} {'Name'}")
    print("-" * 80)
    for t in tasks:
        print(f"Tier {t.tier:<2} {t.id:<24} {t.category:<20} {t.target_standard:<10} {t.name}")
    print("=" * 80)
    return 1 if loader.load_errors else 0


def cmd_eval(args: argparse.Namespace) -> int:
    loader = DatasetLoader(getattr(args, "tasks_root", None))
    task = loader.get_task(args.task)
    if not task:
        print(f"Error: Task '{args.task}' not found.", file=sys.stderr)
        return 1
    task = _target_task(task, args)
    if getattr(args, "category", None) is not None and args.category != task.category:
        print(f"Error: Task '{args.task}' is outside category {args.category}", file=sys.stderr)
        return 1

    output = getattr(args, "output", None)
    validate_output_paths([output, getattr(args, "junit_output", None)], protected_files=[args.solution] if args.solution else [],
                          protected_roots=_input_roots(loader))
    asset_errors = validate_task_assets(task)
    if asset_errors:
        model = "golden_reference" if args.reference else "local_solution" if args.solution else "starter_stub"
        result = failed_evaluation(task, model, "Task asset validation failed: " + "; ".join(asset_errors))
        asset_metadata = {"run_status": "completed", "selected_tasks": [task.id], "pending_tasks": []}
        _save_checkpoint(output, [result], model, asset_metadata)
        _write_junit(args, [result], model, asset_metadata)
        print(BenchmarkReporter.generate_cli_table([result], model, asset_metadata))
        _task_diagnostics(args, result)
        return 1
    solution_code = None
    model_name = "local_solution"
    if args.reference:
        solution_code = loader.get_reference_solution(args.task)
        model_name = "golden_reference"
        if not solution_code:
            print(f"Error: Reference solution for '{args.task}' not found.", file=sys.stderr)
            return 1
    elif args.solution:
        sol_path = Path(args.solution)
        if not sol_path.is_file():
            print(f"Error: Solution file '{args.solution}' not found.", file=sys.stderr)
            return 1
        with open(sol_path, "r", encoding="utf-8") as f:
            solution_code = f.read()
    else:
        # Default to starter code
        solution_code = loader.get_starter_code(args.task)
        model_name = "starter_stub"

    if not solution_code:
        print(f"Error: No solution available for '{args.task}'.", file=sys.stderr)
        return 1

    executor = _new_executor(args)
    analyzer = _new_analyzer(args)
    metadata = collect_run_metadata([task], executor, static_analyzer=analyzer) if output or getattr(args, "junit_output", None) else None
    if metadata is not None:
        metadata.update(run_status="running", pending_tasks=[task.id])
        pending = failed_evaluation(task, model_name, "Not evaluated: evaluation has not finished")
        _attach_provenance(pending, metadata, task.id, solution_code)
        _save_checkpoint(output, [pending], model_name, metadata)
    interrupted = False
    try:
        errors = validate_task_assets(task)
        if errors:
            raise ValueError("Task asset validation failed: " + "; ".join(errors))
        result = evaluate_task(task, solution_code, loader.get_reference_solution(task.id),
                               model_name, executor, analyzer)
    except KeyboardInterrupt:
        result = failed_evaluation(task, model_name, "Evaluation interrupted by user")
        interrupted = True
    except Exception as error:
        result = failed_evaluation(task, model_name, str(error))
    if metadata is not None:
        metadata.update(run_status="interrupted" if interrupted else "completed",
                        pending_tasks=[task.id] if interrupted else [])
        _attach_provenance(result, metadata, task.id, solution_code)
        _save_checkpoint(output, [result], model_name, metadata)
    print(BenchmarkReporter.generate_cli_table([result], model_name=model_name, metadata=metadata))
    _task_diagnostics(args, result)
    _write_junit(args, [result], model_name, metadata)
    if output:
        print(f"\nResults successfully saved to: {output}")
    return 130 if interrupted else 0 if result.test_result.passed and not result.error_log else 1


def _attach_provenance(result, metadata, task_id, solution_code=None, messages=None):
    result.footprint_target = ((metadata.get("compiler") or {}).get("target") or {}).get("target", "host")
    result.provenance = {**(result.provenance or {}),
        "task_sha256": metadata["task_fingerprints"][task_id],
        "candidate_sha256": text_sha256(solution_code) if solution_code else None,
        "prompt_sha256": text_sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False)) if messages else None,
    }


def _save_checkpoint(output, results, model_name, metadata):
    if output:
        atomic_write_json(output, BenchmarkReporter.to_json_dict(results, model_name, metadata))


def _input_roots(loader):
    # A discovered task folder may itself link outside the dataset root.
    return [loader.tasks_root, *(task.task_dir for task in loader.list_tasks()),
            data_root() / "third_party" / "unity", Path(__file__).resolve().parent]


def _select_tasks(loader, args):
    tasks = loader.list_tasks(tier=args.tier, category=getattr(args, "category", None))
    if args.tasks is not None:
        selected_ids = [item.strip() for item in args.tasks.split(",")]
        if any(not item for item in selected_ids) or len(set(selected_ids)) != len(selected_ids):
            print("Error: --tasks must contain distinct, nonempty task IDs.", file=sys.stderr)
            return None
        unknown = [item for item in selected_ids if loader.get_task(item) is None]
        if unknown:
            print(f"Error: Unknown task IDs: {', '.join(unknown)}", file=sys.stderr)
            return None
        conflicts = [item for item in selected_ids
                     if args.tier is not None and loader.get_task(item).tier != args.tier]
        if conflicts:
            print(f"Error: Requested tasks outside --tier {args.tier}: {', '.join(conflicts)}", file=sys.stderr)
            return None
        tasks = [task for task in tasks if task.id in selected_ids]
        category_conflicts = [item for item in selected_ids if getattr(args, "category", None) is not None
                              and loader.get_task(item).category != args.category]
        if category_conflicts:
            print(f"Error: Requested tasks outside --category {args.category}: {', '.join(category_conflicts)}", file=sys.stderr)
            return None
    if not tasks:
        print("No matching tasks found.", file=sys.stderr)
        return None
    return tasks


def cmd_validate(args: argparse.Namespace) -> int:
    loader = DatasetLoader(getattr(args, "tasks_root", None), strict=False)
    _show_load_errors(loader)
    tasks = _select_tasks(loader, args)
    if not tasks:
        return 1
    failures = 0
    for task in tasks:
        errors = validate_task_assets(task)
        failures += bool(errors)
        print(f"[{'FAIL' if errors else 'PASS'}] {task.id}")
        for error in errors:
            print(f"  - {error}")
    print(f"Dataset structure: {len(tasks) - failures} passed, {failures} failed.")
    print("Use run --model baseline to check reference compilation, tests, and resource budgets.")
    return 1 if failures or loader.load_errors else 0


def cmd_run(args: argparse.Namespace) -> int:
    from aibenchmark_esw.resume import load_resume, validate_resume, merge_generation_history
    saved = load_resume(args) if getattr(args, "resume", None) else None
    loader = DatasetLoader(getattr(args, "tasks_root", None))
    tasks = _select_tasks(loader, args)
    if not tasks:
        return 1
    if saved:
        tasks = [loader.get_task(item) for item in saved["metadata"]["selected_tasks"]]
    tasks = [_target_task(task, args) for task in tasks]
    solution_directory = getattr(args, "save_solutions", None)
    solution_paths = [] if solution_directory is None else [
        Path(solution_directory) / f"{task.id}{suffix}" for task in tasks for suffix in (".c", ".truncated.c")]
    system_file = getattr(args, "system_prompt_file", None)
    validate_output_paths([args.output, getattr(args, "junit_output", None), *solution_paths],
                          protected_files=[system_file] if system_file else [], protected_roots=_input_roots(loader))
    system_prompt = Path(system_file).read_text(encoding="utf-8") if system_file else None
    if system_prompt is not None and not system_prompt.strip():
        raise ValueError("System prompt file must not be empty")
    executor, analyzer = _new_executor(args), _new_analyzer(args)
    cancelled = threading.Event()
    executor.cancel_event = cancelled
    client = _new_client(args) if args.model != "baseline" else None
    settings = None if client is None else {**client.settings(),
        "system_prompt_sha256": text_sha256(system_prompt) if system_prompt is not None else None}
    metadata = collect_run_metadata(tasks, executor, settings, analyzer)
    option_names = ("compiler", "allow_standard_fallback", "compile_timeout", "static_analysis_timeout", "max_output_bytes",
                    "isolation", "memory_limit_bytes", "sanitizers", "save_solutions", "tasks_root", "system_prompt_file",
                    "target", "cross_compiler")
    metadata["run_options"] = {name: getattr(args, name, None) for name in option_names}
    metadata["run_options"]["compiler"] = executor.compiler_path
    metadata["run_options"].update(allow_standard_fallback=executor.allow_standard_fallback,
        compile_timeout=executor.compile_timeout_seconds, max_output_bytes=executor.max_output_bytes,
        isolation=executor.isolation, memory_limit_bytes=executor.memory_limit_bytes, sanitizers=list(executor.sanitizers),
        static_analysis_timeout=analyzer.cppcheck_timeout_seconds)
    for name in ("save_solutions", "tasks_root", "system_prompt_file"):
        if metadata["run_options"][name] is not None:
            metadata["run_options"][name] = str(Path(metadata["run_options"][name]).resolve())
    if saved:
        validate_resume(saved, metadata, tasks)
        results = BenchmarkReporter.from_json_dict(saved)
        pending = {i for i, task in enumerate(tasks) if task.id in saved["metadata"]["pending_tasks"]}
        metadata = copy.deepcopy(saved["metadata"])
        metadata.setdefault("resumed_at_utc", []).append(datetime.now(timezone.utc).isoformat())
    else:
        results = [failed_evaluation(task, args.model, "Not evaluated: run has not reached this task") for task in tasks]
        pending = set(range(len(tasks)))
        for task, result in zip(tasks, results):
            _attach_provenance(result, metadata, task.id)
    cache = ReferenceCache()
    if getattr(args, "preflight", False):
        from aibenchmark_esw.doctor import run_doctor
        readiness = run_doctor(tasks, loader, executor, True, cache)
        if not readiness["passed"]:
            for check in readiness["checks"]:
                if not check["passed"]:
                    print(f"Preflight: {check['name']}: {check['detail']}", file=sys.stderr)
            return 1
    if solution_directory is not None:
        Path(solution_directory).mkdir(parents=True, exist_ok=True)
    quiet = getattr(args, "quiet", False)
    if not quiet:
        print(f"Starting AIBenchMark-ESW run on {len(tasks)} tasks using model '{args.model}'...")
    def checkpoint(status):
        metadata.update(run_status=status, pending_tasks=[task.id for i, task in enumerate(tasks) if i in pending])
        _save_checkpoint(args.output, results, args.model, metadata)
    checkpoint("running" if pending else "completed")

    def work(index):
        task = tasks[index]
        llm = _new_client(args) if client is not None else None
        if llm is not None:
            llm.cancel_event = cancelled
        solution_code = messages = None
        state, generating = "done", False
        try:
            if cancelled.is_set():
                raise InterruptedError("Run cancelled")
            errors = validate_task_assets(task)
            if errors:
                raise ValueError("Task asset validation failed: " + "; ".join(errors))
            reference = loader.get_reference_solution(task.id)
            if not reference:
                raise ValueError("Missing reference implementation")
            if llm is None:
                solution_code = reference
            else:
                headers = "\n".join(f"// --- {header.name} ---\n{header.read_text(encoding='utf-8')}"
                                    for header in sorted((task.task_dir / "include").glob("*.h")))
                messages = llm.build_prompt(task.prompt, headers, loader.get_starter_code(task.id) or "",
                    target_standard=task.target_standard, prompt_overrides=task.prompt_overrides,
                    system_prompt=system_prompt)
                generating = True
                solution_code = llm.generate_solution(messages)
                generating = False
                if not solution_code:
                    raise ValueError("Model returned an empty implementation")
            if solution_directory is not None:
                atomic_write_text(Path(solution_directory) / f"{task.id}.c", solution_code)
            result = evaluate_task(task, solution_code, reference, args.model, executor,
                                   analyzer, reference_cache=cache)
        except (KeyboardInterrupt, InterruptedError):
            result = failed_evaluation(task, args.model, "Evaluation interrupted by user")
            state = "interrupted"
        except Exception as error:
            result = failed_evaluation(task, args.model, str(error))
            if generating:
                state = "fatal" if is_fatal_provider_error(error) else "generation_failure"
            if llm and llm.last_generation and llm.last_generation.get("partial_response") and solution_directory is not None:
                atomic_write_text(Path(solution_directory) / f"{task.id}.truncated.c",
                                  llm.extract_c_code(llm.last_generation["partial_response"]))
        if llm is not None:
            result.generation = merge_generation_history(results[index].generation, llm.last_generation)
            if llm.last_generation:
                messages = llm.last_generation.get("request_messages", messages)
        _attach_provenance(result, metadata, task.id, solution_code, messages)
        return index, result, state

    interrupted = aborted = False
    failure_streak = 0
    generation_failures = attempts = 0
    def accept(outcome):
        nonlocal interrupted, aborted, failure_streak, generation_failures, attempts
        index, result, state = outcome
        if cancelled.is_set() and state == "done" and not result.test_result.completed:
            state = "interrupted"
        results[index] = result
        attempts += 1
        generation_failures += state in ("fatal", "generation_failure")
        failure_streak = failure_streak + 1 if state == "generation_failure" else 0
        interrupted |= state == "interrupted" and not aborted
        aborted |= state == "fatal" or failure_streak >= 3
        if state not in ("interrupted", "fatal") and not (state == "generation_failure" and aborted):
            pending.discard(index)
        if interrupted or aborted:
            cancelled.set()
        checkpoint("interrupted" if interrupted else "aborted" if aborted else "running" if pending else "completed")
        _task_diagnostics(args, result)
        if not quiet:
            status = "PASS" if result.test_result.passed and not result.error_log else "FAIL"
            print(f"[{result.task_id}] {status} - Score: {result.scores.total_score:.1f}")

    indices = sorted(pending)
    jobs = getattr(args, "jobs", 1)
    if jobs == 1:
        for index in indices:
            accept(work(index))
            if interrupted or aborted:
                break
    else:
        pool = ThreadPoolExecutor(max_workers=jobs)
        active: dict = {}
        remaining = iter(indices)
        def fill():
            while not cancelled.is_set() and len(active) < jobs:
                index = next(remaining, None)
                if index is None:
                    break
                active[pool.submit(work, index)] = index
        try:
            fill()
            while active:
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in finished:
                    active.pop(future)
                    accept(future.result())
                fill()
        except KeyboardInterrupt:
            interrupted = True
            cancelled.set()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            for future in active:
                if not future.cancelled():
                    accept(future.result())
    if attempts and attempts == generation_failures and not interrupted:
        aborted = True
        # Failed provider requests are retryable pending slots on resume.
        pending.update(i for i in indices if not results[i].test_result.completed)
    checkpoint("interrupted" if interrupted else "aborted" if aborted else "completed")
    print("\n" + BenchmarkReporter.generate_cli_table(results, model_name=args.model, metadata=metadata))
    _write_junit(args, results, args.model, metadata)
    if args.output and not quiet:
        print(f"\nResults successfully saved to: {args.output}")
    return 130 if interrupted else 1 if aborted or pending else 0 if all(
        result.test_result.passed and not result.error_log for result in results) else 1


def cmd_report(args: argparse.Namespace) -> int:
    report_file = Path(args.results)
    if not report_file.is_file():
        print(f"Error: File not found: {args.results}", file=sys.stderr)
        return 1

    try:
        data = json.loads(report_file.read_text(encoding="utf-8"))
        results = BenchmarkReporter.from_json_dict(data)
        model_name = data.get("model_name", "unknown")
        rendered = (render_junit(results, model_name, data.get("metadata")) if args.format == "junit" else
                    BenchmarkReporter.generate_markdown(results, model_name, data.get("metadata")) if args.format == "markdown"
                    else BenchmarkReporter.generate_cli_table(results, model_name, data.get("metadata")))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Error: Invalid results file: {error}", file=sys.stderr)
        return 1
    print(rendered)
    return 0


def cmd_validate_report(args):
    from aibenchmark_esw.report_schema import report_schema, validate_report_structure
    if args.schema:
        print(json.dumps(report_schema(), indent=2))
        return 0
    if args.results is None:
        raise ValueError("Specify a report path or --schema")
    report = json.loads(args.results.read_text(encoding="utf-8"))
    validate_report_structure(report)
    BenchmarkReporter.from_json_dict(report)
    print("Report structure and consistency: PASS")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        validate_output_paths([args.output], protected_files=args.results, protected_roots=_benchmark_roots(args))
        reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.results]
        comparison = compare_runs(reports)
        rendered = render_comparison(comparison, args.format)
        if args.output:
            atomic_write_text(args.output, rendered)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"Error: Cannot compare reports: {error}", file=sys.stderr)
        return 1
    # CSV exports keep a regular schema; provenance warnings still go to stderr.
    if args.format in ("csv", "csv-long"):
        for warning in comparison["warnings"]:
            print(f"Warning: {warning}", file=sys.stderr)
    sys.stdout.write(rendered + ("" if rendered.endswith("\n") else "\n"))
    return 0


def cmd_aggregate(args):
    validate_output_paths([args.output], protected_files=args.results, protected_roots=_benchmark_roots(args))
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.results]
    summary = aggregate_runs(reports)
    rendered = render_aggregation(summary, args.format)
    if args.output:
        atomic_write_text(args.output, rendered)
    sys.stdout.write(rendered + ("" if rendered.endswith("\n") else "\n"))
    return 0


def cmd_doctor(args):
    from aibenchmark_esw.doctor import run_doctor
    loader = DatasetLoader(getattr(args, "tasks_root", None), strict=False)
    _show_load_errors(loader)
    tasks = _select_tasks(loader, args)
    if not tasks:
        return 1
    report = run_doctor([_target_task(task, args) for task in tasks], loader, _new_executor(args), args.check_references)
    for check in report["checks"]:
        print(f"[{'PASS' if check['passed'] else 'FAIL'}] {check['name']}: {check['detail']}")
    return 0 if report["passed"] and not loader.load_errors else 1


def cmd_mutations(args):
    from aibenchmark_esw.mutations import run_mutations
    loader = DatasetLoader(getattr(args, "tasks_root", None))
    tasks = _select_tasks(loader, args)
    if not tasks:
        return 1
    validate_output_paths([args.output], protected_roots=_input_roots(loader))
    report = run_mutations([_target_task(task, args) for task in tasks], loader, _new_executor(args))
    if args.output:
        atomic_write_json(args.output, report)
    for task in report["tasks"]:
        print(f"{task['task_id']}: killed={task['killed']} survived={task['survived']} invalid={task['invalid']}")
    return 0 if report["passed"] else 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "list": cmd_list,
        "validate": cmd_validate,
        "eval": cmd_eval,
        "run": cmd_run,
        "report": cmd_report,
        "validate-report": cmd_validate_report,
        "compare": cmd_compare,
        "aggregate": cmd_aggregate,
        "doctor": cmd_doctor,
        "mutations": cmd_mutations,
    }

    try:
        exit_code = dispatch[args.command](args)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("Interrupted. Any completed JSON checkpoint remains available.", file=sys.stderr)
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
