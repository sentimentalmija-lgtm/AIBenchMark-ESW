"""Additional target-object evidence; Unity suites continue to run on the host."""

import locale
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from aibenchmark_esw.models import CompilationResult
from aibenchmark_esw.sandbox.process_runner import (
    ExecutionCancelled,
    ExecutionCleanupError,
    OutputLimitExceeded,
    run_bounded,
)
from aibenchmark_esw.targets import supported_target


class CrossCompiler:
    def __init__(self, target, compiler=None, timeout=30, max_output_bytes=1048576,
                 isolation="native", memory_limit_bytes=None, runner=None, warnings=False, finding_parser=None):
        if not supported_target(target):
            raise ValueError("Target must be AVR ATmega, ARM Cortex-M0/M0plus/M3/M4/M7/M23/M33/M55/M85, or RISC-V rv32i/imc/imac/ec/rv64gc/imac")
        self.max_output_bytes, self.isolation, self.memory_limit_bytes = max_output_bytes, isolation, memory_limit_bytes
        self.cancel_event, self.runner = None, runner
        self.target, self.timeout = target, timeout
        architecture, self.cpu = target.split(":")
        commands = (["avr-gcc"] if architecture == "avr" else ["arm-none-eabi-gcc"] if architecture == "arm"
                    else ["riscv64-unknown-elf-gcc", "riscv-none-elf-gcc"] if self.cpu.startswith("rv64")
                    else ["riscv32-unknown-elf-gcc", "riscv-none-elf-gcc", "riscv64-unknown-elf-gcc"])
        command = commands[0]
        selected = compiler or os.environ.get("AIBENCHMARK_ESW_CROSS_CC") or next(
            (path for path in (shutil.which(name) for name in commands) if path), None) or shutil.which("clang")
        if not selected:
            raise ValueError(f"Cross compiler unavailable: install {command}/Clang or set AIBENCHMARK_ESW_CROSS_CC")
        self.compiler = str(Path(selected).resolve()) if Path(selected).is_file() else selected
        compiler_name = Path(self.compiler).stem.lower()
        if self.cpu.startswith("rv64") and "riscv32" in compiler_name:
            raise ValueError("RV64 target requires an RV64-capable toolchain; selected riscv32 compiler")
        self.warning_flags = ["-Wall", "-Wextra", "-Wconversion", "-Wshadow"] if warnings else []
        self.finding_parser = finding_parser
        self.abi = None
        if architecture == "riscv":
            self.abi = ("ilp32e" if self.cpu == "rv32ec" else "ilp32" if self.cpu.startswith("rv32")
                        else "lp64d" if self.cpu == "rv64gc" else "lp64")
            self.flags = (["--target=riscv" + self.cpu[2:4] + "-unknown-elf"] if "clang" in compiler_name else [])
            self.flags += [f"-march={self.cpu}", f"-mabi={self.abi}"]
        else:
            self.flags = (["--target=avr", f"-mmcu={self.cpu}"] if architecture == "avr" else
                      ["--target=arm-none-eabi", f"-mcpu={self.cpu}", "-mthumb"]) if "clang" in compiler_name else (
                      [f"-mmcu={self.cpu}"] if architecture == "avr" else [f"-mcpu={self.cpu}", "-mthumb"])
        self.flags += ["-Os", "-ffreestanding"]
        try:
            with tempfile.TemporaryDirectory(prefix="aibenchmark_cross_probe_") as directory:
                version = (self.runner or run_bounded)([self.compiler, "--version"], cwd=directory,
                                                      timeout=10, max_output_bytes=65536)
            lines = version.stdout.decode(locale.getpreferredencoding(False), errors="backslashreplace").splitlines()
            self.version = lines[0] if version.returncode == 0 and lines else None
        except (OSError, ValueError, subprocess.SubprocessError):
            self.version = None
        path = Path(self.compiler)
        self.stamp = [path.stat().st_size, path.stat().st_mtime_ns] if path.is_file() else None

    def settings(self):
        return {"target": self.target, "compiler": self.compiler, "version": self.version,
                "name": Path(self.compiler).name, "isa": self.cpu, "abi": self.abi,
                "flags": self.flags, "warning_flags": self.warning_flags,
                "stamp": self.stamp, "measurement": "target-object"}

    def compile(self, task, source, output, extra_flags=()):
        command = [self.compiler, *self.flags, *self.warning_flags, *extra_flags, f"-std={task.target_standard}", "-I",
                   str(task.task_dir.resolve() / "include"), "-c", str(source), "-o", str(output)]
        try:
            runner = self.runner or run_bounded
            result = runner(command, cwd=output.parent, timeout=self.timeout,
                            max_output_bytes=self.max_output_bytes, isolation=self.isolation,
                            memory_limit_bytes=self.memory_limit_bytes, cancel_event=self.cancel_event)
            log = result.stdout.decode(locale.getpreferredencoding(False), errors="backslashreplace")
            success = result.returncode == 0 and output.is_file()
            findings = self.finding_parser(log, [source]) if self.finding_parser else []
            return CompilationResult(success, log,
                binary_path=output if success else None, error_message=None if success else "Target compilation failed",
                effective_standard=task.target_standard, findings=findings, warning_flags=self.warning_flags)
        except (OutputLimitExceeded, ExecutionCancelled, ExecutionCleanupError, subprocess.TimeoutExpired) as error:
            captured = error.output or b""
            log = captured.decode(locale.getpreferredencoding(False), errors="backslashreplace") if isinstance(captured, bytes) else captured
            return CompilationResult(False, log + "\n" + str(error), error_message="Target compilation bounded or cancelled",
                                     effective_standard=task.target_standard)
        except OSError as error:
            return CompilationResult(False, str(error), error_message="Target compiler unavailable")
