"""Reproducible task authoring overlays; public assets are never called hidden."""

import hashlib
import json
import re
import shutil
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

VARIANT_VERSION = "task-parameters-v2"
TASK_VARIANT_VERSIONS = {"tier1_crc16": "crc-polynomial-v1",
                         "tier3_i2c_sensor": "i2c-registers-v1"}


def _crc(data, polynomial):
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ (polynomial if value & 0x8000 else 0)) & 0xFFFF
    return value


def _crc_variant(task, seed):
    digest = hashlib.sha256(f"{TASK_VARIANT_VERSIONS[task.id]}:{seed}:{task.id}".encode()).digest()
    polynomial = (int.from_bytes(digest[:2], "big") | 0x8001)
    old = {0x29B1: _crc(b"123456789", polynomial),
           0x84C0: _crc(bytes(4), polynomial), 0x1D0F: _crc(bytes([255] * 4), polynomial)}
    for folder in ("src", "reference", "tests", "include"):
        for path in sorted((task.task_dir / folder).glob("*")):
            if path.suffix not in (".c", ".h"):
                continue
            source = path.read_text(encoding="utf-8")
            source = re.sub(r"0x1021(?=[uUlL]*\b)", f"0x{polynomial:04X}", source, flags=re.IGNORECASE)
            source = re.sub(r"0x(?:29B1|84C0|1D0F)\b",
                            lambda match: f"0x{old[int(match.group(), 16)]:04X}", source, flags=re.IGNORECASE)
            path.write_text(source, encoding="utf-8")
    prompt = task.prompt + f"\nEvaluation variant: use CRC polynomial 0x{polynomial:04X} instead of 0x1021; init remains 0xFFFF, no reflection, no final XOR.\n"
    (task.task_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    return replace(task, prompt=prompt), {"polynomial": polynomial}


def _i2c_variant(task, seed):
    """Render named register hooks; reference, starter and oracle use the API macros."""
    version = TASK_VARIANT_VERSIONS[task.id]
    digest = hashlib.sha256(f"{version}:{seed}:{task.id}".encode()).digest()
    # Separate ranges keep all hooks distinct; adjacent temperature bytes do not wrap.
    parameters = {
        "SENSOR_DEFAULT_I2C_ADDR": 0x20 + digest[0] % 32,
        "SENSOR_REG_TEMP_MSB": 0x10 + 2 * (digest[1] % 16),
        "SENSOR_REG_CONFIG": 0x40 + digest[2] % 32,
        "SENSOR_REG_WHO_AM_I": 0x60 + digest[3] % 32,
        "SENSOR_EXPECTED_CHIP_ID": 0x80 + digest[4] % 128,
    }
    parameters["SENSOR_REG_TEMP_LSB"] = parameters["SENSOR_REG_TEMP_MSB"] + 1
    header_path = task.task_dir / "include" / "i2c_sensor.h"
    header = header_path.read_text(encoding="utf-8")
    prompt = task.prompt
    for name, value in parameters.items():
        header, count = re.subn(rf"(?m)^(#define {name}\s+)0x[0-9A-Fa-f]+(?=\s|$)",
                               rf"\g<1>0x{value:02X}", header)
        if count != 1:
            raise ValueError(f"I2C variant requires exactly one header hook: {name}")
        if name == "SENSOR_EXPECTED_CHIP_ID":
            continue
        prompt, count = re.subn(rf"(`{name}` \(`)0x[0-9A-Fa-f]+(`\))",
                               rf"\g<1>0x{value:02X}\g<2>", prompt)
        # CHIP_ID has a separately named prompt hook rather than a register definition.
        if count != 1:
            raise ValueError(f"I2C variant requires exactly one prompt hook: {name}")
    chip_hook = "`SENSOR_EXPECTED_CHIP_ID` (`0xA5`)"
    if prompt.count(chip_hook) != 2:
        raise ValueError("I2C variant requires chip ID hooks in hardware and errors")
    prompt = prompt.replace(chip_hook, f"`SENSOR_EXPECTED_CHIP_ID` (`0x{parameters['SENSOR_EXPECTED_CHIP_ID']:02X}`)")
    header_path.write_bytes(header.encode("utf-8"))
    (task.task_dir / "prompt.md").write_bytes(prompt.encode("utf-8"))
    return replace(task, prompt=prompt), parameters


@contextmanager
def prepare_task_variants(tasks, variant_seed=None, heldout_tests=None):
    """Yield (tasks, evidence); temporary asset paths live for the whole context.

    Held-out root layout is TASK_ID/tests/test_*.c. A present task overlay
    replaces its whole tests directory, preserving public prompt/API assets.
    A seed changes only registered task parameters; evidence records the scope.
    """
    tasks = list(tasks)
    if variant_seed is not None and (isinstance(variant_seed, bool) or not isinstance(variant_seed, int)):
        raise ValueError("variant_seed must be an integer")
    overlay = Path(heldout_tests).resolve() if heldout_tests is not None else None
    supplied = {}
    heldout_digest = hashlib.sha256()
    if overlay is not None:
        if not overlay.is_dir():
            raise ValueError("heldout_tests must be a directory")
        ids = {task.id for task in tasks}
        for entry in sorted(overlay.iterdir()):
            if not entry.is_dir() or entry.is_symlink() or entry.name not in ids:
                raise ValueError(f"Unknown or unsafe held-out task overlay: {entry.name}")
            tests = entry / "tests"
            if not tests.is_dir() or tests.is_symlink() or not list(tests.glob("test_*.c")):
                raise ValueError(f"Held-out task {entry.name} requires tests/test_*.c")
            for path in sorted(tests.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Held-out assets cannot be symlinks")
                if path.is_file():
                    relative = path.relative_to(overlay).as_posix().encode()
                    content = path.read_bytes()
                    heldout_digest.update(len(relative).to_bytes(8, "big") + relative)
                    heldout_digest.update(len(content).to_bytes(8, "big") + content)
            supplied[entry.name] = tests
        if not supplied:
            raise ValueError("heldout_tests contains no task suites")
    if variant_seed is not None:
        for task_id in sorted(supplied.keys() & TASK_VARIANT_VERSIONS.keys()):
            parameter = "polynomial" if task_id == "tier1_crc16" else "registers or chip ID"
            raise ValueError(f"Cannot combine --variant-seed with a {task_id} held-out suite: expectations may use a different {parameter}")
    evidence: dict[str, Any] = {"variant_version": VARIANT_VERSION, "variant_seed": variant_seed,
                "heldout_tests_used": bool(supplied),
                "heldout_sha256": heldout_digest.hexdigest() if supplied else None,
                "variant_parameters": {}, "variant_task_versions": {},
                "unvaried_tasks": [task.id for task in tasks
                                  if variant_seed is None or task.id not in TASK_VARIANT_VERSIONS]}
    if variant_seed is None and not supplied:
        yield tasks, evidence
        return
    with TemporaryDirectory(prefix="aibenchmark_variants_") as directory:
        prepared = []
        for task in tasks:
            destination = Path(directory) / task.id
            shutil.copytree(task.task_dir, destination)
            current = replace(task, task_dir=destination)
            if variant_seed is not None:
                if task.id == "tier1_crc16":
                    current, parameters = _crc_variant(current, variant_seed)
                    evidence["variant_parameters"][task.id] = parameters
                elif task.id == "tier3_i2c_sensor":
                    current, parameters = _i2c_variant(current, variant_seed)
                    evidence["variant_parameters"][task.id] = parameters
                if task.id in TASK_VARIANT_VERSIONS:
                    evidence["variant_task_versions"][task.id] = TASK_VARIANT_VERSIONS[task.id]
                # Even unchanged tasks record seed identity in their task asset
                # fingerprint; provenance can distinguish authoring conditions.
                metadata_path = destination / "task.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["variant"] = {"version": VARIANT_VERSION, "seed": variant_seed,
                                       "task_version": TASK_VARIANT_VERSIONS.get(task.id),
                                       "parameters": evidence["variant_parameters"].get(task.id),
                                       "varied": task.id in TASK_VARIANT_VERSIONS}
                metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            if task.id in supplied:
                shutil.rmtree(destination / "tests")
                shutil.copytree(supplied[task.id], destination / "tests")
            prepared.append(current)
        yield prepared, evidence


@contextmanager
def prepare_replay_tasks(tasks, recorded, *, heldout_tests=None, regrade=False):
    """Reconstruct the recorded task contract from the current dataset."""
    if regrade or recorded is None:
        if heldout_tests is not None:
            raise ValueError('Replay --heldout-tests requires an origin report with a held-out suite and no --regrade')
        yield tasks
        return
    if not isinstance(recorded, dict) or recorded.get('variant_version') != VARIANT_VERSION:
        raise ValueError('Unsupported replay variant version; use a compatible evaluator or explicitly --regrade')
    heldout_used = recorded.get('heldout_tests_used')
    if not isinstance(heldout_used, bool):
        raise ValueError('Invalid replay variant evidence: heldout_tests_used must be a boolean')
    if heldout_used and heldout_tests is None:
        raise ValueError('Replay requires the original private suite via --heldout-tests, or explicitly --regrade')
    if not heldout_used and heldout_tests is not None:
        raise ValueError('Replay --heldout-tests requires an origin report with a held-out suite')
    with prepare_task_variants(tasks, recorded.get('variant_seed'), heldout_tests) as (prepared, evidence):
        if json.dumps(evidence, sort_keys=True) != json.dumps(recorded, sort_keys=True):
            raise ValueError('Recorded variant evidence does not match regenerated parameters or the held-out suite')
        yield prepared


def reference_similarity(candidate, reference):
    """Exact normalized token match is a warning, never evidence of memorization.

    Preserve string/character literals, unlike mask_noncode, and remove only
    comments and spacing. Different algorithms are not scored for similarity.
    """
    pattern = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\r\n]*|[A-Za-z_]\w*|\d+(?:\.\d+)?|>>=|<<=|==|!=|<=|>=|->|\+\+|--|&&|\|\||[^\s]'
    def tokens(source):
        return tuple(token for token in re.findall(pattern, source, re.DOTALL)
                     if not token.startswith(("/*", "//")))
    return bool(candidate and reference and tokens(candidate) == tokens(reference))
