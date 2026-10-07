from dataclasses import asdict, dataclass, field
import math
import re
from typing import Optional, List, Dict, Any, Literal
from pathlib import Path, PureWindowsPath


@dataclass
class TaskLimits:
    max_flash_bytes: int = 2048
    max_ram_bytes: int = 256
    timeout_seconds: int = 10

    def __post_init__(self):
        for name in ("max_flash_bytes", "max_ram_bytes", "timeout_seconds"):
            value = getattr(self, name)
            minimum = 0 if name == "max_ram_bytes" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass
class TaskWeights:
    functional: float = 0.6
    memory: float = 0.2
    safety: float = 0.2

    def __post_init__(self):
        values = (self.functional, self.memory, self.safety)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not 0 <= value <= 1 or not math.isfinite(value) for value in values):
            raise ValueError("Task weights must be finite nonnegative numbers")
        if not math.isclose(sum(values), 1.0, abs_tol=1e-9):
            raise ValueError("Task weights must sum to 1")


@dataclass
class TaskConfig:
    id: str
    name: str
    tier: int
    category: str
    target_standard: str
    description: str
    limits: TaskLimits
    weights: TaskWeights
    entry_file: str
    task_dir: Path
    prompt: str = ""
    reference_file: Optional[str] = None
    prompt_overrides: Dict[str, Any] = field(default_factory=dict)
    target_limits: Dict[str, TaskLimits] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("name", "category", "description"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"Task {name} must be a string")
        if not isinstance(self.prompt_overrides, dict):
            raise ValueError("prompt_overrides must be an object")
        unknown = set(self.prompt_overrides) - {"allow_dynamic_memory", "extra_rules"}
        if unknown:
            raise ValueError(f"Unknown prompt_overrides fields: {', '.join(sorted(unknown))}")
        if "allow_dynamic_memory" in self.prompt_overrides and not isinstance(self.prompt_overrides["allow_dynamic_memory"], bool):
            raise ValueError("prompt_overrides.allow_dynamic_memory must be a boolean")
        rules = self.prompt_overrides.get("extra_rules", [])
        if not isinstance(rules, list) or any(not isinstance(rule, str) or not rule.strip() for rule in rules):
            raise ValueError("prompt_overrides.extra_rules must be an array of nonempty strings")
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", self.id):
            raise ValueError("Task id must use letters, digits, underscores or hyphens and cannot start with a hyphen")
        reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}
        if self.id.upper() in reserved:
            raise ValueError("Task id cannot be a reserved Windows file name")
        if isinstance(self.tier, bool) or not isinstance(self.tier, int) or self.tier not in (1, 2, 3, 4):
            raise ValueError("Task tier must be 1, 2, 3, or 4")
        if self.target_standard not in ("c99", "c11", "c17"):
            raise ValueError("target_standard must be c99, c11, or c17")
        for name in ("entry_file", "reference_file"):
            value = getattr(self, name)
            if value is None and name == "reference_file":
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty relative file path")
            path = Path(value.replace("\\", "/"))
            if path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts or path == Path("."):
                raise ValueError(f"{name} must stay within the task directory")
            setattr(self, name, path.as_posix())

    @property
    def reference_path(self) -> Path:
        """Resolve the explicit reference or the legacy entry-basename convention."""
        return self.task_dir / (self.reference_file or f"reference/{Path(self.entry_file).name}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any], task_dir: Path, prompt: str = "") -> "TaskConfig":
        if not isinstance(data, dict):
            raise ValueError("Task metadata must be a JSON object")
        limits_data = data.get("limits", {})
        if not isinstance(limits_data, dict):
            raise ValueError("Task limits must be an object")
        targets = limits_data.get("targets", {})
        if not isinstance(targets, dict):
            raise ValueError("limits.targets must be an object")
        target_limits = {}
        defaults = limits_data.get("default", limits_data)
        if not isinstance(defaults, dict):
            raise ValueError("limits.default must be an object")
        limits_data = defaults
        limits = TaskLimits(
            max_flash_bytes=limits_data.get("max_flash_bytes", 2048),
            max_ram_bytes=limits_data.get("max_ram_bytes", 256),
            timeout_seconds=limits_data.get("timeout_seconds", 10),
        )
        for target, override in targets.items():
            if not isinstance(target, str) or not isinstance(override, dict):
                raise ValueError("Target limits must map target names to limit objects")
            target_limits[target] = TaskLimits(**{**asdict(limits), **override})
        weights_data = data.get("weights", {})
        if not isinstance(weights_data, dict):
            raise ValueError("Task weights must be an object")
        weights = TaskWeights(
            functional=weights_data.get("functional", 0.6),
            memory=weights_data.get("memory", 0.2),
            safety=weights_data.get("safety", 0.2),
        )
        return cls(
            id=data["id"],
            name=data["name"] if "name" in data else data["id"],
            tier=data.get("tier", 1),
            category=data.get("category", "general"),
            target_standard=data.get("target_standard", "c99"),
            description=data.get("description", ""),
            limits=limits,
            weights=weights,
            entry_file=data.get("entry_file", "src/solution.c"),
            task_dir=task_dir,
            prompt=prompt,
            reference_file=data.get("reference_file"),
            prompt_overrides=data.get("prompt_overrides", {}),
            target_limits=target_limits,
        )


@dataclass
class CompilationResult:
    success: bool
    output: str
    binary_path: Optional[Path] = None
    error_message: Optional[str] = None
    effective_standard: Optional[str] = None
    _workspace: Optional[Any] = field(default=None, repr=False, compare=False)

    def cleanup(self) -> None:
        """Release an automatically created workspace after consuming its artifacts."""
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None
            self.binary_path = None


@dataclass
class TestResult:
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    ignored_tests: int = 0
    output: str = ""
    passed: bool = False
    completed: bool = False
    returncode: Optional[int] = None


@dataclass
class SizeMetrics:
    flash_bytes: int = 0
    ram_bytes: int = 0
    ref_flash_bytes: int = 0
    ref_ram_bytes: int = 0
    measured: bool = True


@dataclass
class StaticSafetyMetrics:
    error_count: int = 0
    warning_count: int = 0
    violations: List[str] = field(default_factory=list)
    cppcheck_status: Optional[Literal["disabled", "not_run", "completed", "failed", "timeout"]] = None
    cppcheck_diagnostic: Optional[str] = None
    findings: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DimensionScores:
    functional_score: float = 0.0  # 0.0 - 100.0
    memory_score: float = 0.0      # 0.0 - 100.0
    safety_score: float = 0.0      # 0.0 - 100.0
    total_score: float = 0.0       # 0.0 - 100.0

    def __post_init__(self):
        for name in ("functional_score", "memory_score", "safety_score", "total_score"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= 100 or not math.isfinite(value)):
                raise ValueError(f"{name} must be a finite number between 0 and 100")


@dataclass
class TaskEvaluationResult:
    task_id: str
    tier: int
    model_name: str
    compiled: bool
    test_result: TestResult
    size_metrics: SizeMetrics
    safety_metrics: StaticSafetyMetrics
    scores: DimensionScores
    execution_time_sec: float
    error_log: Optional[str] = None
    weights: Optional[TaskWeights] = None
    target_standard: Optional[str] = None
    effective_standard: Optional[str] = None
    generation: Optional[Dict[str, Any]] = None
    provenance: Optional[Dict[str, Any]] = None
    limits: Optional[TaskLimits] = None
    candidate_time_sec: Optional[float] = None
    reference_validation_time_sec: Optional[float] = None
    footprint_target: str = "host"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tier": self.tier,
            "model_name": self.model_name,
            "compiled": self.compiled,
            "test_result": {
                "total": self.test_result.total_tests,
                "passed": self.test_result.passed_tests,
                "failed": self.test_result.failed_tests,
                "ignored": self.test_result.ignored_tests,
                "all_passed": self.test_result.passed,
                "completed": self.test_result.completed,
                "returncode": self.test_result.returncode,
            },
            "size_metrics": {
                "flash_bytes": self.size_metrics.flash_bytes,
                "ram_bytes": self.size_metrics.ram_bytes,
                "ref_flash_bytes": self.size_metrics.ref_flash_bytes,
                "ref_ram_bytes": self.size_metrics.ref_ram_bytes,
                "measured": self.size_metrics.measured,
            },
            "safety_metrics": {
                "error_count": self.safety_metrics.error_count,
                "warning_count": self.safety_metrics.warning_count,
                "violations": self.safety_metrics.violations,
                "cppcheck_status": self.safety_metrics.cppcheck_status,
                "cppcheck_diagnostic": self.safety_metrics.cppcheck_diagnostic,
                "findings": self.safety_metrics.findings,
            },
            "scores": {
                "functional": round(self.scores.functional_score, 2),
                "memory": round(self.scores.memory_score, 2),
                "safety": round(self.scores.safety_score, 2),
                "total": round(self.scores.total_score, 2),
            },
            "execution_time_sec": round(self.execution_time_sec, 3),
            "error_log": self.error_log,
            "weights": asdict(self.weights) if self.weights is not None else None,
            "target_standard": self.target_standard,
            "effective_standard": self.effective_standard,
            "generation": self.generation,
            "provenance": self.provenance,
            "limits": asdict(self.limits) if self.limits is not None else None,
            "candidate_time_sec": self.candidate_time_sec,
            "reference_validation_time_sec": self.reference_validation_time_sec,
            "footprint_target": self.footprint_target,
        }
