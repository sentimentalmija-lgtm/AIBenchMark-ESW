"""Collect explicit, credential-free inputs needed to reproduce a benchmark."""

import hashlib
import json
import platform
import re
import subprocess
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from aibenchmark_esw import __version__
from aibenchmark_esw.sandbox.static_analyzer import StaticAnalyzer
from aibenchmark_esw.metrics.scorer import SCORING_FORMULA_VERSION, SAFETY_ERROR_PENALTY, SAFETY_WARNING_PENALTY


def text_sha256(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _digest_records(records):
    digest = hashlib.sha256()
    for name, content in sorted(records):
        # Length-prefix names and data so distinct records cannot concatenate
        # into the same byte stream. Ignore checkout-specific line endings.
        name = name.encode("utf-8")
        # Text inputs are checkout-independent; binary fixtures retain every byte.
        try:
            content.decode("utf-8")
            if b"\x00" not in content:
                content = content.replace(b"\r\n", b"\n")
        except UnicodeDecodeError:
            pass
        for part in (name, content):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def task_sha256(task, unity_dir, *, unreadable=None):
    config = {key: getattr(task, key) for key in
              ("id", "tier", "target_standard", "entry_file", "reference_file", "prompt")}
    config.update(limits=asdict(task.limits), weights=asdict(task.weights))
    config["prompt_overrides"] = getattr(task, "prompt_overrides", {})
    config["target_limits"] = {key: asdict(value) for key, value in getattr(task, "target_limits", {}).items()}
    records = [("effective_config", json.dumps(config, sort_keys=True).encode("utf-8"))]

    def add_asset(name, path):
        try:
            records.append((name, path.read_bytes()))
        except OSError as error:
            if unreadable is None:
                raise
            unreadable.append({"asset": name, "error": type(error).__name__})
            records.append(("unreadable/" + name, type(error).__name__.encode("ascii")))

    for path in sorted(task.task_dir.rglob("*")):
        relative = path.relative_to(task.task_dir)
        if (path.is_file()
                and path.suffix.lower() not in (".o", ".obj", ".exe", ".dll", ".so", ".a", ".lib", ".pyc")
                and not any(part in ("build", "__pycache__", "CMakeFiles", ".git", ".pytest_cache")
                            for part in relative.parts)):
            add_asset("task/" + relative.as_posix(), path)
    for path in sorted(Path(unity_dir).glob("*")):
        if path.is_file() and path.suffix in (".c", ".h"):
            add_asset("harness/" + path.name, path)
    return _digest_records(records)


def _command_output(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                errors="backslashreplace", timeout=5)
        if result.returncode == 0:
            return (result.stdout + result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _checkout_provenance(package_dir):
    """Only attribute a revision to a checkout declaring this distribution."""
    checkout = package_dir.parent
    if not (checkout / ".git").exists():
        return None, None
    try:
        project = (checkout / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    section = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", project)
    if (section is None or not re.search(
            r'''(?m)^name\s*=\s*["']aibenchmark-esw["']\s*$''', section.group(1))):
        return None, None
    top = _command_output(["git", "-C", str(checkout), "rev-parse", "--show-toplevel"])
    if not top or Path(top).resolve() != checkout.resolve():
        return None, None
    revision = _command_output(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    status = _command_output(["git", "-C", str(checkout), "status", "--porcelain"])
    return revision, bool(status) if status is not None else None


def collect_run_metadata(tasks, executor, generation_settings=None, static_analyzer=None):
    compiler = Path(executor.compiler_path).stem.lower()
    version = _command_output([executor.compiler_path, "/?" if compiler == "cl" else
                               "-v" if compiler == "tcc" else "--version"])
    fingerprints, unreadable_assets = {}, {}
    for task in tasks:
        errors: list = []
        fingerprints[task.id] = task_sha256(task, executor.unity_dir, unreadable=errors)
        if errors:
            unreadable_assets[task.id] = errors
    analyzer = static_analyzer or StaticAnalyzer()
    cppcheck_version = _command_output([analyzer.cppcheck_cmd, "--version"]) if analyzer.cppcheck_cmd else None
    package_dir = Path(__file__).resolve().parent
    revision, dirty = _checkout_provenance(package_dir)
    execution_settings = getattr(executor, "execution_settings", None)
    execution_settings = execution_settings() if callable(execution_settings) else None
    if isinstance(execution_settings, dict):
        execution_settings = {**execution_settings,
                              "static_analysis_timeout_seconds": analyzer.cppcheck_timeout_seconds}
    return {
        "run_id": uuid.uuid4().hex,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark_version": __version__,
        "evaluator_sha256": _digest_records([
            (path.relative_to(package_dir).as_posix(), path.read_bytes())
            for path in package_dir.rglob("*.py") if "_data" not in path.relative_to(package_dir).parts
        ]),
        "python_version": platform.python_version(),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "compiler": {"name": compiler, "version": version.splitlines()[0] if version else None,
                     "target": executor.cross.settings() if getattr(executor, "cross", None) else {"target": "host"},
                     "optimization": "/O1" if compiler == "cl" else "native" if compiler == "tcc" else "-Os",
                     "allow_standard_fallback": executor.allow_standard_fallback},
        "static_analysis": {"engine": "builtin+cppcheck" if analyzer.cppcheck_cmd else "builtin",
                            "cppcheck_version": cppcheck_version, "configuration": analyzer.configuration()},
        "source_revision": revision, "source_dirty": dirty,
        "selected_tasks": [task.id for task in tasks],
        "task_fingerprints": fingerprints,
        "dataset_sha256": (None if unreadable_assets else
                           _digest_records([(name, value.encode("ascii")) for name, value in fingerprints.items()])),
        "unreadable_assets": unreadable_assets,
        "generation_settings": generation_settings,
        "scoring_policy": {"formula_version": SCORING_FORMULA_VERSION,
                           "safety_error_penalty": SAFETY_ERROR_PENALTY,
                           "safety_warning_penalty": SAFETY_WARNING_PENALTY,
                           "tasks": {task.id: {"weights": asdict(task.weights), "limits": asdict(task.limits)} for task in tasks}},
        "execution_settings": execution_settings,
    }
