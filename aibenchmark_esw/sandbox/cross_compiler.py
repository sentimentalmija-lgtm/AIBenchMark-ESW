"""Additional target-object evidence; Unity suites continue to run on the host."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from aibenchmark_esw.models import CompilationResult


class CrossCompiler:
    def __init__(self, target, compiler=None, timeout=30):
        if not re.fullmatch(r"(?:avr:atmega[0-9]+[a-z]*|arm:cortex-m[0347](?:plus)?)", target):
            raise ValueError("Target must be avr:atmega328p or arm:cortex-m0/m3/m4/m7")
        self.target, self.timeout = target, timeout
        architecture, self.cpu = target.split(":")
        command = "avr-gcc" if architecture == "avr" else "arm-none-eabi-gcc"
        selected = compiler or os.environ.get("AIBENCHMARK_ESW_CROSS_CC") or shutil.which(command) or shutil.which("clang")
        if not selected:
            raise ValueError(f"Cross compiler unavailable: install {command}/Clang or set AIBENCHMARK_ESW_CROSS_CC")
        self.compiler_name = Path(selected).name or str(selected)
        self.compiler = str(Path(selected).resolve()) if Path(selected).is_file() else selected
        self.flags = (["--target=avr", f"-mmcu={self.cpu}"] if architecture == "avr" else
                      ["--target=arm-none-eabi", f"-mcpu={self.cpu}", "-mthumb"]) if "clang" in Path(selected).stem else (
                      [f"-mmcu={self.cpu}"] if architecture == "avr" else [f"-mcpu={self.cpu}", "-mthumb"])
        self.flags += ["-Os", "-ffreestanding"]
        try:
            version = subprocess.run([self.compiler, "--version"], capture_output=True, text=True, timeout=5)
            self.version = (version.stdout + version.stderr).splitlines()[0] if version.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            self.version = None
        path = Path(self.compiler)
        self.stamp = [path.stat().st_size, path.stat().st_mtime_ns] if path.is_file() else None

    def settings(self):
        return {"target": self.target, "compiler": self.compiler, "compiler_name": self.compiler_name,
                "version": self.version,
                "flags": self.flags, "stamp": self.stamp, "measurement": "target-object"}

    def compile(self, task, source, output):
        command = [self.compiler, *self.flags, f"-std={task.target_standard}", "-I",
                   str(task.task_dir.resolve() / "include"), "-c", str(source), "-o", str(output)]
        try:
            result = subprocess.run(command, cwd=output.parent, capture_output=True, text=True,
                                    errors="backslashreplace", timeout=self.timeout)
            success = result.returncode == 0 and output.is_file()
            return CompilationResult(success, result.stdout + result.stderr,
                binary_path=output if success else None, error_message=None if success else "Target compilation failed",
                effective_standard=task.target_standard)
        except (OSError, subprocess.SubprocessError) as error:
            return CompilationResult(False, str(error), error_message="Target compiler unavailable or timed out")


def comparable_cross_compiler_settings(settings):
    if (not isinstance(settings, dict) or settings.get("measurement") != "target-object"
            or not isinstance(settings.get("version"), str) or not settings["version"].strip()):
        return settings
    compiler = settings.get("compiler_name") or settings.get("compiler")
    if not isinstance(compiler, str) or not compiler.strip():
        return settings
    compiler_name = compiler.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
    if compiler_name.endswith(".exe"):
        compiler_name = compiler_name[:-4]
    if not compiler_name:
        return settings
    comparable = {key: value for key, value in settings.items()
                  if key not in ("compiler", "compiler_name", "stamp")}
    comparable["compiler"] = compiler_name
    return comparable


def comparable_compiler_metadata(compiler):
    if not isinstance(compiler, dict) or not isinstance(compiler.get("target"), dict):
        return compiler
    target = compiler["target"]
    comparable = comparable_cross_compiler_settings(target)
    return compiler if comparable is target else {**compiler, "target": comparable}


def comparable_execution_settings(settings):
    if not isinstance(settings, dict) or not isinstance(settings.get("footprint"), dict):
        return settings
    footprint = settings["footprint"]
    comparable = comparable_cross_compiler_settings(footprint)
    return settings if comparable is footprint else {**settings, "footprint": comparable}
