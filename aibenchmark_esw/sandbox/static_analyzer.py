import os
import shutil
import subprocess
import re
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from aibenchmark_esw.models import StaticSafetyMetrics
from aibenchmark_esw.sandbox.c_source import code_tokens, mask_noncode


DEFAULT_CPPCHECK_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class _CppcheckOutcome:
    status: Literal["completed", "failed", "timeout"]
    metrics: Optional[StaticSafetyMetrics] = None
    diagnostic: Optional[str] = None


class StaticAnalyzer:
    def configuration(self) -> Dict[str, Any]:
        return {"builtin_rules": ["allocation-reference", "misra.15.1", "floating-types", "misra.4.6",
                                  "host-process", "host-network", "host-filesystem"],
                "basic_type_exceptions": ["plain int (status/main APIs)", "plain char (character data)"],
                "cppcheck_enabled": ["warning", "style", "performance", "portability"],
                "inconclusive": True, "standard": "task effective standard",
                "error_severities": ["error"], "warning_severities": ["warning", "style", "performance", "portability"],
                "cppcheck_timeout_seconds": self.cppcheck_timeout_seconds}

    def __init__(self, cppcheck_cmd: Optional[str] = None,
                 cppcheck_timeout_seconds: float = DEFAULT_CPPCHECK_TIMEOUT_SECONDS) -> None:
        if (isinstance(cppcheck_timeout_seconds, bool)
                or not isinstance(cppcheck_timeout_seconds, (int, float))
                or not math.isfinite(cppcheck_timeout_seconds)
                or cppcheck_timeout_seconds <= 0):
            raise ValueError("cppcheck_timeout_seconds must be finite and positive")
        configured = cppcheck_cmd if cppcheck_cmd is not None else os.environ.get("AIBENCHMARK_ESW_CPPCHECK")
        self.cppcheck_cmd = None if configured == "off" else configured or shutil.which("cppcheck")
        self.cppcheck_timeout_seconds = float(cppcheck_timeout_seconds)

    def analyze(self, source_path: Path, include_dirs: Optional[List[Path]] = None,
                standard: str = "c99") -> StaticSafetyMetrics:
        """
        Always check embedded rules; add cppcheck diagnostics when available.
        """
        metrics = self._heuristic_check(source_path)
        metrics.cppcheck_status = "disabled" if not self.cppcheck_cmd else "not_run"
        if self.cppcheck_cmd and source_path.exists():
            outcome = self._run_cppcheck(source_path, include_dirs, standard)
            metrics.cppcheck_status = outcome.status
            metrics.cppcheck_diagnostic = outcome.diagnostic
            if outcome.metrics is not None:
                metrics.error_count += outcome.metrics.error_count
                metrics.warning_count += outcome.metrics.warning_count
                metrics.violations.extend(outcome.metrics.violations)
                metrics.findings.extend(outcome.metrics.findings)
        return metrics

    def _run_cppcheck(self, source_path: Path, include_dirs: Optional[List[Path]] = None,
                      standard: str = "c99") -> _CppcheckOutcome:
        try:
            assert self.cppcheck_cmd is not None
            cmd = [
                self.cppcheck_cmd,
                "--enable=warning,style,performance,portability",
                "--inconclusive",
                f"--std={standard}",
                "--template={severity}:{id}:{file}:{line}:{message}",
                "--quiet",
            ]
            for directory in include_dirs or []:
                cmd += ["-I", str(directory)]
            cmd.append(str(source_path))
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="backslashreplace",
                                  timeout=self.cppcheck_timeout_seconds)
            if proc.returncode != 0:
                diagnostic = f"cppcheck exited with {proc.returncode}: {(proc.stdout + proc.stderr).strip()}"
                return _CppcheckOutcome(status="failed", diagnostic=diagnostic)
            errors = 0
            warnings = 0
            violations = []
            findings = []

            for line in proc.stderr.splitlines():
                if line.startswith("error:"):
                    errors += 1
                    violations.append(line.strip())
                elif line.startswith(("warning:", "style:", "performance:", "portability:")):
                    warnings += 1
                    violations.append(line.strip())
                else:
                    continue
                parts = line.split(":", 2)
                location = re.match(r"(.*):(\d+):(.*)", parts[2]) if len(parts) == 3 else None
                findings.append({"rule_id": parts[1], "engine": "cppcheck", "severity": parts[0],
                    "message": location.group(3) if location else parts[2],
                    "file": Path(location.group(1)).name if location else source_path.name,
                    "line": int(location.group(2)) if location else None})

            return _CppcheckOutcome(
                status="completed",
                metrics=StaticSafetyMetrics(
                    error_count=errors,
                    warning_count=warnings,
                    violations=violations,
                    findings=findings,
                ),
            )
        except subprocess.TimeoutExpired as error:
            return _CppcheckOutcome(status="timeout", diagnostic=str(error))
        except (OSError, UnicodeError) as error:
            return _CppcheckOutcome(status="failed", diagnostic=str(error))

    def _heuristic_check(self, source_path: Path) -> StaticSafetyMetrics:
        if not source_path.exists():
            return StaticSafetyMetrics()

        with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()

        errors = 0
        warnings = 0
        violations = []

        tokens = code_tokens(code)

        # Reject named allocation API references as well as direct calls.
        # Object macros and function-pointer bindings need no '(' after the
        # allocator name; scanning identifier tokens also catches those aliases.
        identifiers = set(tokens)
        if identifiers.intersection({"malloc", "calloc", "realloc", "free"}):
            errors += 1
            violations.append("MISRA-C Violation: Dynamic memory allocation API reference (malloc/calloc/realloc/free) detected.")

        # 2. Uncontrolled Goto (MISRA Rule 15.1)
        if "goto" in identifiers:
            warnings += 1
            violations.append("MISRA-C Advisory: Use of 'goto' statement.")

        # 3. Floating point in core routines (if integer-only target)
        if identifiers.intersection({"double", "float"}):
            warnings += 1
            violations.append("Notice: Floating-point types used in resource-constrained task.")

        # 4. Standard variable types instead of fixed-width (MISRA Rule 4.6)
        pairs = set(zip(tokens, tokens[1:]))
        if (identifiers.intersection({"long", "short"}) or
                pairs.intersection({(sign, kind) for sign in ("unsigned", "signed") for kind in ("int", "char", "short", "long")})):
            warnings += 1
            violations.append("MISRA-C Rule 4.6: Basic numerical types should use fixed-width typedefs.")

        # Host-capability rules are conservative named-reference checks, just
        # like allocation aliases. Object member access is not a host API call.
        references = {token for index, token in enumerate(tokens)
                      if index == 0 or tokens[index - 1] not in (".", "->")}
        categories = {
            "process execution": {"system", "popen", "_popen", "fork", "vfork", "posix_spawn",
                                  "posix_spawnp", "execl", "execlp", "execle", "execv", "execvp",
                                  "execvpe", "execve", "CreateProcessA", "CreateProcessW", "ShellExecuteA",
                                  "ShellExecuteW", "WinExec"},
            "network access": {"socket", "socketpair", "connect", "bind", "listen", "accept",
                               "send", "sendto", "recv", "recvfrom", "getaddrinfo", "WSASocketA",
                               "WSASocketW"},
            "filesystem access": {"fopen", "freopen", "fread", "fwrite", "open", "creat", "read",
                                  "write", "unlink", "remove", "rename", "CreateFileA", "CreateFileW"},
        }
        for category, names in categories.items():
            found = sorted(references.intersection(names))
            if found:
                errors += 1
                violations.append(f"Embedded safety: Host {category} API reference ({', '.join(found)}) detected.")

        masked = mask_noncode(code)
        findings = []
        for message in violations:
            if "allocation" in message:
                rule, severity, names = "builtin.allocation-reference", "error", {"malloc", "calloc", "realloc", "free"}
            elif "goto" in message:
                rule, severity, names = "builtin.misra.15.1", "warning", {"goto"}
            elif "Floating" in message:
                rule, severity, names = "builtin.floating-types", "warning", {"float", "double"}
            elif "4.6" in message:
                rule, severity, names = "builtin.misra.4.6", "warning", {"unsigned", "signed", "short", "long"}
            else:
                category = next(name for name in categories if name in message)
                rule, severity, names = "builtin.host-" + category.split()[0], "error", categories[category]
            match = re.search(r"\b(?:" + "|".join(re.escape(name) for name in sorted(names)) + r")\b", masked)
            findings.append({"rule_id": rule, "engine": "builtin", "severity": severity, "message": message,
                             "file": source_path.name, "line": masked.count("\n", 0, match.start()) + 1 if match else None})
        return StaticSafetyMetrics(
            error_count=errors,
            warning_count=warnings,
            violations=violations,
            findings=findings,
        )
