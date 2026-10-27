import json
import locale
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from aibenchmark_esw.models import CompilationResult, TaskConfig, TestResult
from aibenchmark_esw.resources import data_root
from aibenchmark_esw.sandbox.c_source import mask_noncode
from aibenchmark_esw.sandbox.process_runner import (
    ExecutionCancelled,
    ExecutionCleanupError,
    OutputLimitExceeded,
    run_bounded,
)


def _timeout_output(error: subprocess.TimeoutExpired) -> str:
    """TimeoutExpired can contain bytes even when subprocess uses text mode."""
    streams = []
    for stream in (error.stdout, error.stderr):
        decoded: Optional[str]
        if isinstance(stream, bytes):
            decoded = stream.decode(locale.getpreferredencoding(False), errors="backslashreplace")
        else:
            decoded = stream
        if decoded:
            streams.append(decoded)
    partial = "".join(streams)
    if not partial:
        return str(error)
    return partial + ("" if partial.endswith("\n") else "\n") + str(error)


class ExecutionSandbox:
    def __init__(self, compiler_path: Optional[str] = None, allow_standard_fallback: bool = False,
                 compile_timeout_seconds: float = 30, max_output_bytes: int = 1048576,
                 isolation: str = "native", memory_limit_bytes: Optional[int] = None,
                 sanitizers=(), target=None, cross_compiler=None, warnings=False,
                 container_engine=None, container_image="gcc:14", container_runtime=None, hardening="auto", stack_usage=False):
        if hardening not in ("auto", "off", "required"):
            raise ValueError("hardening must be auto, off or required")
        self.hardening = hardening
        self.stack_usage = bool(stack_usage)
        self.compiler_path = compiler_path or os.environ.get("AIBENCHMARK_ESW_COMPILER") or (
            "gcc" if isolation == "container" else self._find_c_compiler())
        if Path(self.compiler_path).is_file():
            self.compiler_path = str(Path(self.compiler_path).resolve())
        self.unity_dir = data_root() / "third_party" / "unity"
        from aibenchmark_esw.resources import unity_integrity_errors
        integrity = unity_integrity_errors(self.unity_dir)
        if integrity:
            raise ValueError('; '.join(integrity))
        self.allow_standard_fallback = allow_standard_fallback
        if (isinstance(compile_timeout_seconds, bool) or not isinstance(compile_timeout_seconds, (float, int))
                or not math.isfinite(compile_timeout_seconds) or compile_timeout_seconds <= 0):
            raise ValueError("compile_timeout_seconds must be a finite positive number")
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        if isolation not in ("native", "process", "container", "bwrap"):
            raise ValueError("isolation must be native, process, container or bwrap")
        if memory_limit_bytes is not None:
            if isinstance(memory_limit_bytes, bool) or not isinstance(memory_limit_bytes, int) or memory_limit_bytes <= 0:
                raise ValueError("memory_limit_bytes must be a positive integer")
            if isolation == "native":
                raise ValueError("memory_limit_bytes requires process or external isolation")
        if isinstance(sanitizers, str):
            sanitizers = sanitizers.split(",") if sanitizers else ()
        self.sanitizers = tuple(sorted(set(sanitizers)))
        if set(self.sanitizers) - {"address", "undefined"}:
            raise ValueError("sanitizers must contain only address and/or undefined")
        self.compile_timeout_seconds = compile_timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.isolation = isolation
        self.memory_limit_bytes = memory_limit_bytes
        self.cancel_event = None
        self.warnings = bool(warnings)
        self.containment = None
        if isolation in ("container", "bwrap"):
            from aibenchmark_esw.sandbox.containment import ContainmentBackend
            self.containment = ContainmentBackend(isolation, container_engine, container_image, container_runtime)
        self.cross = None
        if target is not None:
            from aibenchmark_esw.sandbox.cross_compiler import CrossCompiler
            self.cross = CrossCompiler(target, cross_compiler, compile_timeout_seconds,
                                       max_output_bytes, isolation, memory_limit_bytes,
                                       self._run_contained if self.containment else None,
                                       warnings=self.warnings, finding_parser=lambda log, sources:
                                       self._warning_findings(log, sources, workspace=Path(sources[0]).parent))

    def hardening_flags(self, object_only=False):
        if self.hardening == "off":
            return []
        name = Path(self.compiler_path).stem.lower()
        if name == "tcc":
            if self.hardening == "required":
                raise ValueError("TCC does not support required hardening; use GCC/Clang/MSVC or --hardening off")
            return []
        if name in ("cl", "clang-cl"):
            flags = ["/sdl", "/guard:cf", "/D_CRT_SECURE_NO_WARNINGS"]
            return flags if object_only else flags + ["/link", "/DYNAMICBASE", "/NXCOMPAT", "/guard:cf"]
        flags = ["-fstack-protector-strong", "-D_FORTIFY_SOURCE=2"]
        linux = self.containment is not None or os.name != "nt" and sys.platform.startswith('linux')
        if linux:
            flags += ["-fPIE"]
            if not object_only:
                flags += ["-pie", "-Wl,-z,relro,-z,now", "-Wl,-z,noexecstack"]
        return flags

    def warning_flags(self):
        if not self.warnings:
            return []
        name = Path(self.compiler_path).stem.lower()
        return ["/W4"] if name in ("cl", "clang-cl") else ["-Wall", "-Wextra", "-Wconversion", "-Wshadow"] if name != "tcc" else ["-Wall"]

    def stack_usage_flags(self):
        compiler = self.cross.compiler if self.cross else self.compiler_path
        name = Path(compiler).stem.lower()
        return ['-fstack-usage'] if self.stack_usage and ('gcc' in name or 'clang' in name and name != 'clang-cl' or name == 'cc') else []

    def collect_stack_usage(self, compilation, output, flags):
        from aibenchmark_esw.sandbox.stack_usage import (
            read_stack_usage,
            unavailable_stack_usage,
        )
        if self.cancel_event is not None and self.cancel_event.is_set():
            return unavailable_stack_usage('Stack collection cancelled')
        if not compilation.success:
            return unavailable_stack_usage('Object compilation did not succeed')
        compiler = self.cross.settings() if self.cross else self.compiler_identity()
        provenance = {'target': self.cross.target if self.cross else 'host', 'compiler': compiler,
                      'flags': list(flags), 'measurement': 'compiler-per-function'}
        if not self.stack_usage_flags():
            return unavailable_stack_usage('Unsupported stack-usage compiler', provenance)
        return read_stack_usage(Path(output).with_suffix('.su'), self.max_output_bytes, provenance)

    def _run_contained(self, command, **options):
        assert self.containment is not None
        return self.containment.run(command, **options)

    def compiler_identity(self):
        """Return identity for the compiler actually executed, including images."""
        command = [self.compiler_path, "/?" if Path(self.compiler_path).stem.lower() == "cl" else
                   "-v" if Path(self.compiler_path).stem.lower() == "tcc" else "--version"]
        try:
            with tempfile.TemporaryDirectory(prefix="aibenchmark_compiler_probe_") as directory:
                runner = self._run_contained if self.containment else run_bounded
                result = runner(command, cwd=directory, timeout=10, max_output_bytes=65536,
                                cancel_event=self.cancel_event)
                output = result.stdout.decode(locale.getpreferredencoding(False), errors="backslashreplace")
                lines = output.strip().splitlines()
                version = lines[0] if result.returncode == 0 and lines else None
        except (OSError, ValueError, subprocess.SubprocessError):
            version = None
        return {"name": Path(self.compiler_path).stem.lower(), "version": version,
                "path": self.compiler_path, "allow_standard_fallback": self.allow_standard_fallback,
                "target": self.cross.settings() if self.cross else None}

    def execution_settings(self):
        return {"compile_timeout_seconds": self.compile_timeout_seconds,
                "max_output_bytes": self.max_output_bytes, "isolation": self.isolation,
                "memory_limit_bytes": self.memory_limit_bytes or (536870912 if self.isolation == "container" else None), "sanitizers": list(self.sanitizers),
                "compiler_warnings": self.warnings, "warning_flags": self.warning_flags(),
                "stack_usage": self.stack_usage,
                "containment": self.containment.settings() if self.containment else None,
                "candidate_environment": "minimal-v1",
                "oracle_policy": "private-unity-state-v1",
                "hardening": {"policy": self.hardening, "compile_flags": self.hardening_flags(True),
                              "binary_flags": self.hardening_flags(), "available": bool(self.hardening_flags())},
                "resource_limits": {"process_count": 64 if os.name == "nt" or self.isolation == "container" or self.isolation in ("process", "bwrap") and getattr(os, "getuid", lambda: 0)() != 0 else None,
                                    "process_count_scope": "job" if os.name == "nt" else "container" if self.isolation == "container" else "uid-current-population-plus-budget" if sys.platform == "darwin" else "uid-total",
                                    "file_size_bytes": 16777216 if os.name != "nt" or self.containment else None,
                                    "core_bytes": 0 if os.name != "nt" or self.containment else None,
                                    "open_files": 256 if os.name != "nt" or self.containment else None},
                "process_backend": "windows-job" if os.name == "nt" else "posix-session",
                "cpu_limit": "task-timeout" if self.isolation in ("process", "bwrap") else None,
                "footprint": self.cross.settings() if self.cross else {"target": "host", "measurement": "host-object"}}

    def _candidate_environment(self, scratch: Path):
        """Pass only runtime essentials; keep provider and host configuration private."""
        allowed = ("PATH", "SystemRoot", "WINDIR", "ComSpec") if os.name == "nt" else ("PATH",)
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        environment.update(HOME=str(scratch), TMPDIR=str(scratch), TEMP=str(scratch), TMP=str(scratch))
        if self.sanitizers:
            environment["ASAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1:detect_leaks=0"
            environment["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
        return environment

    def _find_c_compiler(self) -> str:
        for cmd in ["gcc", "clang", "tcc", "cl"]:
            path = shutil.which(cmd)
            if path:
                return path
        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile:
            for relative in ["tcc/current/tcc/tcc.exe", "gcc/current/bin/gcc.exe"]:
                bundled_path = Path(user_profile) / "scoop" / "apps" / relative
                if bundled_path.is_file():
                    return str(bundled_path)
        return "gcc"

    def _compile(self, task: TaskConfig, sources, output: Path, object_only=False,
                  extra_includes=(), stack_flags=None) -> CompilationResult:
        from aibenchmark_esw.resources import (
            task_integrity_errors,
            unity_integrity_errors,
        )
        errors = unity_integrity_errors(self.unity_dir) + task_integrity_errors(task.task_dir)
        if errors:
            return CompilationResult(False, "\n".join(errors), error_message="Trusted asset integrity check failed")
        output = output.resolve()
        compiler_name = Path(self.compiler_path).stem.lower()
        includes = [task.task_dir.resolve() / "include", self.unity_dir.resolve()]
        includes += [Path(directory).resolve() for directory in extra_includes]
        includes = [directory for directory in includes if directory.is_dir()]
        sources = [str(Path(source).resolve()) for source in sources]
        standard = task.target_standard
        if self.sanitizers and not object_only and (compiler_name in ("tcc", "cl", "clang-cl")):
            message = "Sanitizers require a GCC/Clang driver with the requested sanitizer runtime installed"
            return CompilationResult(False, message, error_message="Unsupported sanitizer toolchain")
        if compiler_name == "cl":
            if standard == "c99":
                if not self.allow_standard_fallback:
                    message = ("MSVC does not support strict C99. Use GCC/Clang/TCC or "
                               "--allow-standard-fallback to explicitly evaluate as C11.")
                    return CompilationResult(False, message, error_message="Unsupported C standard")
                standard = "c11"
            cmd = [self.compiler_path, "/nologo", "/TC", f"/std:{standard}", "/O1"]
            cmd += [f"/I{directory}" for directory in includes]
            if object_only:
                cmd += ["/c", f"/Fo{output}"]
            else:
                cmd += [f"/Fe{output}", f"/Fo{output.parent}{os.sep}"]
            cmd += sources
        else:
            cmd = [self.compiler_path, f"-std={task.target_standard}"]
            if compiler_name != "tcc":
                cmd += ["-Os", "-fvisibility=hidden", "-fno-common"]
            for directory in includes:
                cmd += ["-I", str(directory)]
            if object_only:
                cmd.append("-c")
            cmd += sources + ["-o", str(output)]
            if self.sanitizers and not object_only:
                cmd += ["-fsanitize=" + ",".join(self.sanitizers), "-fno-sanitize-recover=all",
                        "-fno-omit-frame-pointer", "-g"]
        try:
            cmd += self.warning_flags()
            cmd += self.hardening_flags(object_only)
            if object_only:
                cmd += self.stack_usage_flags() if stack_flags is None else list(stack_flags)
            runner = self._run_contained if self.containment else run_bounded
            proc = runner(cmd, timeout=self.compile_timeout_seconds, max_output_bytes=self.max_output_bytes,
                          cwd=str(output.parent), isolation=self.isolation,
                          memory_limit_bytes=self.memory_limit_bytes, cancel_event=self.cancel_event)
            log = proc.stdout.decode(locale.getpreferredencoding(False), errors="backslashreplace")
            findings = self._warning_findings(log, sources, workspace=output.parent)
            if proc.returncode != 0 or not output.is_file():
                return CompilationResult(False, log, error_message="Compilation failed",
                                         effective_standard=standard, findings=findings, warning_flags=self.warning_flags())
            result = CompilationResult(True, log, binary_path=output, effective_standard=standard,
                                       findings=findings, warning_flags=self.warning_flags())
            if object_only and self.stack_usage:
                result.stack_usage = self.collect_stack_usage(result, output, cmd[1:])
            return result
        except subprocess.TimeoutExpired as error:
            return CompilationResult(False, _timeout_output(error), error_message="Compilation timed out",
                                     effective_standard=standard)
        except (OutputLimitExceeded, ExecutionCancelled, ExecutionCleanupError) as error:
            log = error.output.decode(locale.getpreferredencoding(False), errors="backslashreplace")
            return CompilationResult(False, log + "\n" + str(error), error_message=str(error),
                                     effective_standard=standard)
        except (OSError, ValueError) as error:
            return CompilationResult(False, str(error), error_message="Compiler unavailable or timed out",
                                     effective_standard=standard)

    def _warning_findings(self, log, sources, workspace=None):
        if not self.warnings:
            return []
        owned = {Path(source).resolve() for source in sources
                 if not Path(source).name.startswith("aibenchmark_tests_") and Path(source).name != "unity.c"}
        contained = set()
        if self.containment and workspace is not None:
            root = Path(workspace).resolve()
            contained = {'/workspace/' + source.relative_to(root).as_posix()
                         for source in owned if source.is_relative_to(root)}
        findings = []
        for diagnostic in log.splitlines():
            match = re.match(r"^(.*?):(\d+)(?::\d+)?:\s*warning:\s*(.*)", diagnostic)
            msvc = re.match(r"^(.*?)\((\d+)(?:,\d+)?\):\s*warning\s+(C\d+):\s*(.*)", diagnostic)
            if match:
                file, line, message = match.groups()
                flag = re.search(r"\[(-W[^\]]+)\]\s*$", message)
                rule = flag.group(1) if flag else "warning"
            elif msvc:
                file, line, rule, message = msvc.groups()
            else:
                continue
            path = Path(file)
            if file in contained:
                origin = 'candidate'
            elif path.is_absolute():
                if path.resolve() not in owned:
                    continue
                origin = 'candidate'
            else:
                if path.parent != Path('.') or path.name not in {source.name for source in owned}:
                    continue
                origin = 'unknown'
            findings.append({"rule_id": "compiler." + rule, "engine": "compiler", "severity": "warning",
                             "message": message, "file": file, "line": int(line), "source_origin": origin})
        return findings

    def compile_object(self, task: TaskConfig, solution_code: str,
                       workspace: Path, name: str = "candidate") -> CompilationResult:
        """Compile only the implementation, excluding the test harness and runtime."""
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        source = workspace / f"{name}.c"
        source.write_text(solution_code, encoding="utf-8")
        output = workspace / f'{name}.o'
        artifact = output.with_suffix('.su')
        if self.stack_usage and (artifact.exists() or artifact.is_symlink()):
            artifact.unlink()
        if self.cross:
            self.cross.cancel_event = self.cancel_event
            stack_flags = self.stack_usage_flags()
            result = self.cross.compile(task, source, output, stack_flags) if stack_flags else self.cross.compile(task, source, output)
            if self.stack_usage:
                result.stack_usage = self.collect_stack_usage(result, workspace / f"{name}.o",
                    [*self.cross.flags, *self.cross.warning_flags, f'-std={task.target_standard}', *self.stack_usage_flags()])
        else:
            result = self._compile(task, [source], workspace / f"{name}.o", object_only=True)
            if self.stack_usage and result.stack_usage is None:
                result.stack_usage = self.collect_stack_usage(result, workspace / f"{name}.o", [])
        # A rejected evidence flag must not alter object scoring. Retry only an
        # explicit unsupported-option diagnostic, keeping all other flags intact.
        if (not result.success and self.stack_usage_flags()
                and re.search(r'(?:unknown|unrecognized|unsupported|not supported)[^\n]*-fstack-usage|'
                              r'-fstack-usage[^\n]*(?:unknown|unrecognized|unsupported|not supported)', result.output, re.I)):
            from aibenchmark_esw.sandbox.stack_usage import unavailable_stack_usage
            provenance = (result.stack_usage or {}).get('provenance', {})
            result = self.cross.compile(task, source, output) if self.cross else self._compile(
                task, [source], output, object_only=True, stack_flags=())
            result.stack_usage = unavailable_stack_usage('Compiler rejected -fstack-usage', provenance)
        return result

    def compile_and_test(self, task: TaskConfig, solution_code: str,
                         custom_workspace: Optional[Path] = None) -> tuple[CompilationResult, TestResult]:
        """The result owns automatic artifacts until cleanup() or garbage collection."""
        workspace = None
        retained = False
        if custom_workspace is None:
            workspace = tempfile.TemporaryDirectory(prefix=f"aibenchmark_esw_{task.id}_")
            work_dir = Path(workspace.name)
        else:
            work_dir = Path(custom_workspace).resolve()
            work_dir.mkdir(parents=True, exist_ok=True)
        try:
            if re.search(r"\bmain\s*\([^;{}]*\)\s*\{", mask_noncode(solution_code)):
                message = "Candidate source defines a main() function, conflicting with the separate test harness runner. Return only the implementation."
                return CompilationResult(False, message, error_message=message), TestResult(output=message, completed=False)
            tests = sorted((task.task_dir / "tests").glob("test_*.c"))
            if not tests:
                message = f"No test_*.c found in {task.task_dir / 'tests'}"
                return CompilationResult(False, message), TestResult(output=message, completed=False)
            # Dataset filenames must never alias generated harness sources.
            sol_file = work_dir / "candidate.c"
            sol_file.write_text(solution_code, encoding="utf-8")
            completion_token = uuid.uuid4().hex
            wrapped_tests = []
            runners = 0
            for index, test in enumerate(tests):
                source = test.read_text(encoding="utf-8")
                main = re.search(r"\bmain\s*\(([^)]*)\)\s*\{", mask_noncode(source))
                if main is None:
                    wrapped_tests.append(test)
                    continue
                runners += 1
                parameters = main.group(1).strip()
                if parameters in ("", "void"):
                    arguments = ""
                elif parameters.count(",") == 1:
                    arguments = "argc, argv"
                else:
                    message = "Test runner main must take void or argc/argv parameters"
                    return CompilationResult(False, message), TestResult(output=message)
                wrapped = work_dir / f"aibenchmark_tests_{index}.c"
                wrapped.write_text(
                    '#include <stdio.h>\n#define main aibenchmark_suite_main\n'
                    + f"#line 1 {json.dumps(test.resolve().as_posix())}\n" + source
                    + '\n#undef main\n#line 1 "aibenchmark_harness.c"\n'
                    + "int main(int argc, char **argv) {\n"
                    + '    FILE *aibenchmark_channel = argc > 1 ? fopen(argv[1], "wb") : NULL;\n'
                    + '    if (!aibenchmark_channel) return 125;\n'
                    + f"    int result = aibenchmark_suite_main({arguments});\n"
                    + f'    fputs("AIBenchMark-ESW:{completion_token}:END\\n", aibenchmark_channel);\n'
                    + '    if (fclose(aibenchmark_channel) != 0) return 125;\n'
                    + "    return result;\n}\n", encoding="utf-8",
                )
                wrapped_tests.append(wrapped)
            if runners != 1:
                message = "Exactly one test runner main is required"
                return CompilationResult(False, message), TestResult(output=message)
            suffix = ".exe" if os.name == "nt" else ""
            binary_path = work_dir / f"test_{task.id}{suffix}"
            comp_result = self._compile(task, wrapped_tests + [sol_file, self.unity_dir / "unity.c"], binary_path,
                                        extra_includes=[test.parent for test in tests])
            if not comp_result.success:
                return comp_result, TestResult(output="Compilation failed", completed=False)

            comp_result._workspace = workspace
            retained = True
            try:
                scratch = work_dir / "scratch"
                scratch.mkdir(exist_ok=True)
                environment = self._candidate_environment(scratch)
                receipt = work_dir / "harness.receipt"
                receipt.write_bytes(b"")
                runner = self._run_contained if self.containment else run_bounded
                proc = runner([str(binary_path), str(receipt)], timeout=task.limits.timeout_seconds,
                                   max_output_bytes=self.max_output_bytes, cwd=str(scratch), env=environment,
                                   isolation=self.isolation, memory_limit_bytes=self.memory_limit_bytes,
                                   cancel_event=self.cancel_event)
                output = proc.stdout.decode(locale.getpreferredencoding(False), errors="backslashreplace")
                # Receipt is produced only after the trusted suite returns.
                # Native code still shares the address space and filesystem;
                # this separate channel prevents the cwd/stdout exploit, not
                # arbitrary hostile native code from forging the verdict.
                expected = f"AIBenchMark-ESW:{completion_token}:END\n".encode()
                completed_channel = False
                if receipt.is_file():
                    with receipt.open("rb") as channel:
                        completed_channel = channel.read(len(expected) + 1) == expected
                if completed_channel:
                    output += "\n" + expected.decode()
                test_result = self._parse_unity_output(output, proc.returncode,
                                                       completion_token if completed_channel else None)
                if self.sanitizers and re.search(r"AddressSanitizer|UndefinedBehaviorSanitizer|runtime error:", output):
                    test_result.completed = test_result.passed = False
            except subprocess.TimeoutExpired as error:
                # Partial records are diagnostics only; a timeout never completes
                # a suite, even if its buffered output resembles a valid summary.
                test_result = TestResult(output=_timeout_output(error), completed=False)
            except (OutputLimitExceeded, ExecutionCancelled, ExecutionCleanupError) as error:
                output = error.output.decode(locale.getpreferredencoding(False), errors="backslashreplace")
                test_result = TestResult(output=output + "\n" + str(error), completed=False)
            except OSError as error:
                test_result = TestResult(output=str(error), completed=False)
            return comp_result, test_result
        finally:
            if workspace is not None and not retained:
                workspace.cleanup()

    def _parse_unity_output(self, output: str, returncode: int,
                            completion_token: Optional[str] = None) -> TestResult:
        summaries = list(re.finditer(
            r"(?m)^\s*(\d+)\s+Tests\s+(\d+)\s+Failures\s+(\d+)\s+Ignored\s*$", output))
        statuses = re.findall(r"(?m)^.*:\d+:[^:\r\n]+:(PASS|FAIL|IGNORE)\s*$", output)
        observed = tuple(statuses.count(status) for status in ("PASS", "FAIL", "IGNORE"))
        match = summaries[0] if len(summaries) == 1 else None
        if match is not None:
            total, failures, ignored = map(int, match.groups())
            passed = total - failures - ignored
        else:
            passed, failures, ignored = observed
            total = passed + failures + ignored

        marker = f"AIBenchMark-ESW:{completion_token}:END" if completion_token else None
        markers = list(re.finditer(rf"(?m)^{re.escape(marker)}\s*$", output)) if marker else []
        # A summary alone is candidate-controlled output. Require the harness
        # completion marker, consistent per-test records, and Unity's exit code.
        completed = (match is not None and total > 0 and passed >= 0
                     and observed == (passed, failures, ignored)
                     and returncode == (failures if os.name == "nt" else failures % 256)
                     and len(markers) == 1 and markers[0].start() > match.end())
        all_passed = completed and failures == 0 and ignored == 0
        return TestResult(total_tests=total, passed_tests=max(0, passed), failed_tests=failures,
                          ignored_tests=ignored, output=output, passed=all_passed,
                          completed=completed, returncode=returncode)
