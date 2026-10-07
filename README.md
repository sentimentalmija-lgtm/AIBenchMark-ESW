# AIBenchMark-ESW: An Open-Source Embedded AI Coding Benchmark

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![C Standard](https://img.shields.io/badge/standard-C99%2FC11-orange)](https://en.wikipedia.org/wiki/C99)
[![Test Harness](https://img.shields.io/badge/harness-Unity%20TDD-brightgreen)](https://github.com/ThrowTheSwitch/Unity)
[![Benchmark validation](https://github.com/sentimentalmija-lgtm/AIBenchMark-ESW/actions/workflows/ci.yml/badge.svg)](https://github.com/sentimentalmija-lgtm/AIBenchMark-ESW/actions/workflows/ci.yml)

> A host-based benchmark for evaluating AI-generated embedded C using functional tests, resource budgets, and selected static safety rules.

The project is in early development: eight tasks and 77 C test cases provide a reproducible reference baseline. The published [100-point baseline](results/baseline.md) measures the bundled golden implementations; it is not an OpenAI or Claude model score. See [reproducibility](docs/REPRODUCIBILITY.md), the [roadmap](docs/ROADMAP.md), and [contributor guidance](CONTRIBUTING.md).

---

## 📌 Why AIBenchMark-ESW?

Many general coding benchmarks emphasize test pass rates for general-purpose software. Firmware evaluation also needs to account for resource budgets and hardware behavior.

However, **embedded systems software (firmware)** operates under fundamentally different constraints:
1. **Strict Resource Budgets**: Firmware runs on microcontrollers with tens of kilobytes of Flash and RAM. Code size and memory layout directly dictate whether code can run.
2. **Hardware & Peripheral Abstraction**: Embedded code directly controls registers, timers, interrupts, and communication buses (I2C, SPI, UART).
3. **Safety & Reliability**: Many firmware projects restrict dynamic memory allocation and require predictable behavior. This benchmark penalizes named allocation API references and selected code hazards; its heuristic checks are not a safety certification.

**AIBenchMark-ESW fills this gap** by providing an automated, host-based testbed that evaluates AI-generated embedded C code across **functional correctness**, **memory footprint**, and **static code safety**.

---

## 🚀 Key Features

* **Deterministic Host-Based Testing**: Validates code using mocked hardware abstraction layers (Mock HAL) and [Unity TDD](https://github.com/ThrowTheSwitch/Unity). No physical development boards required; runs seamlessly in any CI/CD pipeline or Docker container.
* **Multi-Dimensional Scoring**:
  $$\text{Total Score} = 0.6 \times S_{\text{func}} + \frac{S_{\text{func}}}{100} \times (0.2 \times S_{\text{mem}} + 0.2 \times S_{\text{safety}})$$
  Combines test pass rate, Flash/RAM footprint consumption, and static safety warnings. For partially passing suites, the memory and safety contributions are multiplied by the functional pass fraction.
* **4-Tier Problem Progression**: Tasks range from core embedded data structures to asynchronous protocol state machines, register-level device drivers, and real-world race condition bug fixes.
* **Provider Integration**: Uses [LiteLLM](https://docs.litellm.ai/docs/) for OpenAI, Anthropic, and other supported providers. Choose an exact model ID available to your account. Temperature is omitted by default for compatibility; token limits and request timeouts are configurable. Results retain usage, latency, resolved model identity, and generation failures.
* **Auditable Results**: JSON reports record compiler/host information and dataset, candidate, and prompt fingerprints. Save candidate sources for replay and compare existing model reports offline, with checks for mismatched evaluation conditions.
* **Automated Validation**: GitHub Actions checks Linux GCC/Clang and Windows TCC, including installation from built distributions, and publishes reference JSON/Markdown artifacts without model API credentials.

---

## 📊 Benchmark Tiers & Tasks

| Tier | Category | Task ID | Description | Key Embedded Focus |
| :---: | :--- | :--- | :--- | :--- |
| **1** | Core Fundamentals | `tier1_ring_buffer` | SPSC-style Ring Buffer | Boundary wrap-around, zero allocation, pointer safety |
| **1** | Core Fundamentals | `tier1_crc16` | Standard CRC-16/CCITT-FALSE Checksum Engine | Fixed polynomial (0x1021), bitwise manipulation, lookup logic |
| **1** | Core Fundamentals | `tier1_q15_math` | Saturating Q1.15 Arithmetic | Overflow-safe add/subtract/multiply, negative-product truncation, 16-bit-int portability |
| **2** | FSM & Protocols | `tier2_debounce_fsm` | Noise-Immune Button Input Debounce FSM | Glitch rejection, multi-event emission (Click, Hold, Release) |
| **2** | FSM & Protocols | `tier2_cobs_codec` | Bounded COBS packet codec | Zero-free framing, exact capacities, malformed blocks, 254-byte boundaries |
| **2** | FSM & Protocols | `tier2_tick_timer` | Rollover-Safe Tick Timer | One-shot/periodic deadlines, missed expirations, phase retention, full-width intervals |
| **3** | Device Drivers | `tier3_i2c_sensor` | I2C Temperature Sensor Driver with Mock HAL | Register verification, error/timeout propagation, fixed-point math |
| **4** | Bug Fix & Safety | `tier4_bitmask_fix` | W1C Interrupt Status Register & Priority Bitmask Fix | Write-1-to-Clear race condition, bitfield isolation |

---

## 🛠️ Quickstart Guide

### 1. Installation

Clone the repository and install the Python package in development mode:

```bash
git clone https://github.com/sentimentalmija-lgtm/AIBenchMark-ESW.git
cd AIBenchMark-ESW

# Install minimal core
pip install -e .

# Or install with LLM provider dependencies
pip install -e ".[full]"

# Development and distribution validation
pip install -e ".[dev]"
python -m unittest discover -s tests -v
```

*Prerequisites*: A C compiler in your PATH (`gcc`, `clang`, `tcc`, or `cl`). Candidate and reference code use the same compiler and settings. GCC/Clang use `-Os`; TCC uses its native code generation; MSVC uses `/O1`. MSVC cannot select strict C99, so C99 tasks fail by default with that compiler. Add `--allow-standard-fallback` to `eval` or `run` to explicitly permit C11 instead. Results and reports record both the requested and effective standard.

Regular wheel installations also include all tasks, headers, reference implementations, and the Unity harness. Direct wheels and wheels built from source distributions exclude local build products and caches. Editable installations use the checkout's `tasks/` and `third_party/` directories. Optional Clang integration tests check ELF common symbols and sensor arithmetic on an AVR target.

---

### 2. Basic CLI Usage

#### List Available Tasks
```bash
aibenchmark-esw list
```

Check required task files without a compiler or API access:

```bash
aibenchmark-esw validate
aibenchmark-esw validate --tasks-root ./my_tasks --tier 1
```

`run` checks these assets before each task's generation. Missing, empty, or unreadable inputs produce a failed result without a provider request; valid tasks continue. Use the reference baseline to check compilation, functional correctness, and resource budgets.

#### Evaluate Local Solution or Reference
Test a specific task against the built-in golden reference:
```bash
aibenchmark-esw eval --task tier1_ring_buffer --reference
```
Or test your own local C solution:
```bash
aibenchmark-esw eval --task tier1_ring_buffer --solution ./my_ring_buffer.c

# Export a local evaluation with the same provenance as benchmark runs.
aibenchmark-esw eval --task tier1_ring_buffer --solution ./my_ring_buffer.c \
  --output results/local_ring_buffer.json
```

#### Run Benchmark with LLM Models
Run the benchmark across all tasks using any LLM:
```bash
# Use exact model IDs supported by your provider account and LiteLLM version.
export OPENAI_API_KEY="your-api-key"
export OPENAI_MODEL="<available-openai-model-id>"
aibenchmark-esw run --model "openai/$OPENAI_MODEL" --max-tokens 4096 \
  --request-timeout 60 --save-solutions results/openai_sources --output results/openai.json

# Evaluate an available Claude model with the same evaluation settings.
export ANTHROPIC_API_KEY="your-api-key"
export ANTHROPIC_MODEL="<available-claude-model-id>"
aibenchmark-esw run --model "anthropic/$ANTHROPIC_MODEL" --max-tokens 4096 \
  --request-timeout 60 --save-solutions results/claude_sources --output results/claude.json

# Evaluate a local Ollama model (no API key needed)
export LOCAL_MODEL="<installed-ollama-model-id>"
aibenchmark-esw run --model "ollama/$LOCAL_MODEL" --max-tokens 4096 --output results/local.json

# Run reference baseline
aibenchmark-esw run --model baseline --output results/baseline.json
```

#### View Results
```bash
aibenchmark-esw report --results results/baseline.json

# Render a Markdown report
aibenchmark-esw report --results results/baseline.json --format markdown

# Compare existing runs without API calls; export CSV for further analysis.
aibenchmark-esw compare --results results/openai.json results/claude.json \
  --format markdown --output results/comparison.md
aibenchmark-esw compare --results results/openai.json results/claude.json \
  --format csv --output results/comparison.csv

# Replay the exact source extracted from a provider response.
aibenchmark-esw eval --task tier1_crc16 --solution results/openai_sources/tier1_crc16.c
```

Comparison requires identical task sets, weights, standards, and compatible recorded evaluator, dataset, and toolchain settings. Failed tasks remain in the denominator. Legacy files can be compared with explicit missing-provenance warnings. Token usage and generation duration are reported only where available; no API price or dollar cost is inferred.

For cross-toolchain validation, set `AIBENCHMARK_ESW_COMPILER` to a compiler executable (for example, `clang`); an explicit `--compiler` argument takes precedence. Provider settings must be supported by the selected model. A token-limit-truncated response is recorded as a generation failure.

With `--output`, runs save a complete JSON checkpoint before generation and after each task. Ctrl+C preserves completed work and usage, marks pending tasks explicitly, and exits with code 130. Pending tasks remain in the denominator at zero points; reports show the unfinished status and `compare` rejects unfinished runs. Use `run --resume results/checkpoint.json` to continue pending tasks under the same verified inputs and run identity. Without `--resume`, each invocation starts a new run; choose a new output filename to retain earlier runs. Local `eval --output` also exports provenance and failed evaluations. `--reference` and `--solution` are mutually exclusive.

Output paths are checked before evaluation or provider calls. Reports and saved candidate files must not overwrite source inputs, task data, the evaluator, or the Unity harness. Comparison and aggregation exports also protect task, evaluator, and harness sources. Existing output reports can still be replaced intentionally.

Reports record the static-analysis backend and cppcheck version. When cppcheck fails, its diagnostic is saved and reports warn that safety scoring used only the built-in rules. Comparisons reject known mismatches in analyzer configuration and flag missing legacy analyzer information.

Set `AIBENCHMARK_ESW_CPPCHECK=off` for reproducible built-in-only analysis, or set it to a working cppcheck executable. Leaving it unset enables PATH discovery. Compiler CI jobs select built-in analysis explicitly; a separate CI job validates an installed cppcheck against real C fixtures.

---

## 📐 Scoring Methodology

Each task is evaluated across three weighted dimensions:

### 1. Functional Correctness ($S_{\text{func}}$, 60%)
Evaluates whether the candidate implementation compiles without errors and passes all test cases in the Unity suite.
$$S_{\text{func}} = \frac{\text{Passed Test Cases}}{\text{Total Test Cases}} \times 100$$
*(Note: Compilation failure, timeout, abnormal process termination, or an incomplete test suite gives an overall task score of 0. Completed suites with assertion failures retain partial credit. Ignored tests do not count toward Pass@1.)*

Every selected task is included in the results, including model API failures and missing references. `eval` and `run` return a nonzero exit status if any task fails or an evaluation error occurs; `run --output` still saves the results.

Completion requires the trusted test runner to return, emit its per-run completion marker, and produce consistent per-test records and a single Unity summary. Printing a summary and exiting early receives zero points. Candidate C executes in the same native process as the test harness; this completion check is not an operating-system security sandbox. Model prompts use each task's requested C standard.

The percentages above are the default weights. Each task can specify its own nonnegative weights summing to 1; results preserve these weights and reports display them. Reports with mixed weights identify them as varying by task. Older result files without weights display them as unknown.

Dataset loading rejects missing roots, malformed task metadata, invalid limits or weights, and duplicate task IDs instead of silently excluding tasks. An optional `reference_file` in `task.json` selects a reference path relative to the task directory; older tasks retain the `reference/<entry_file basename>` convention. Use `list`, `eval`, or `run` with `--tasks-root ./my_tasks` to evaluate external tasks with the installed package; see the [contributor workflow](CONTRIBUTING.md). Explicit `--tasks` selections reject empty, duplicate, unknown, or conflicting `--tier` entries.

### 2. Memory Footprint Efficiency ($S_{\text{mem}}$, 20%)
Measures a separately compiled implementation object, excluding Unity, test code, and the host executable runtime. Candidate and reference objects are compiled with the same settings. Allocated code and read-only sections count toward Flash; initialized writable data counts toward both Flash and RAM; zero-initialized data and COFF/ELF common symbols count toward RAM. Debug and symbol table bytes are excluded. This is a host object footprint, not an MCU-linked image or a stack usage measurement.

The combined size ($\text{Flash} + \text{RAM}$) is compared to the measured reference ($M_{\text{ref}}$) and combined maximum budget ($M_{\text{max}}$). Each individual Flash and RAM limit must also be respected:
* If $M_{\text{actual}} \le M_{\text{ref}}$: $S_{\text{mem}} = 100$
* If $M_{\text{ref}} < M_{\text{actual}} \le M_{\text{max}}$: Linear decay toward 0.
* If $M_{\text{actual}} > M_{\text{max}}$: $S_{\text{mem}} = 0$
* If either individual resource limit is exceeded: $S_{\text{mem}} = 0$
* Missing or unreadable measurements are reported as unavailable and receive no memory points.
* References that fail to compile, do not complete and pass every test, or exceed either resource budget produce an explicit reference-validation error and receive no memory points.

Reference implementations must pass all functional tests and fit the budgets with the compiler used for comparisons. The refreshed GCC 15.2 baseline scores 100/100 and passes all 77 tests. The ring buffer's Flash budget is 2048 bytes, accommodating its measured 567-byte GCC object footprint. Its RAM footprint is legitimately zero because it uses caller-owned storage. The Q1.15 task has 16 cases, including a sweep of all 65,536 raw values for selected arithmetic identities and scaling checks; it does not exhaust all input pairs. The tick timer has 16 cases for wrap, deadline boundaries, phase retention, and missed-expiration counts; real elapsed time from its stored origin must remain below 2^32 ticks. Other compilers may produce different footprints; resource limits remain enforced for both candidates and references.

### 3. Static Code Safety ($S_{\text{safety}}$, 20%)
Checks selected embedded safety rules using built-in heuristics and, when installed, `cppcheck`. These checks are not full MISRA-C certification. Penalizes:
* Dynamic memory allocation (`malloc`, `free`) — reported as an error by the built-in checks
* Unrestricted `goto` jumps (MISRA Rule 15.1)
* Non-fixed-width standard types (MISRA Rule 4.6)
* Buffer overflows and uninitialized variables

The built-in allocation rule conservatively rejects named references to `malloc`, `calloc`, `realloc`, and `free`, including macro aliases and function-pointer bindings. Comments and literals are excluded. This lexical rule does not implement a full C preprocessor.

---

## 📁 Repository Layout

```text
AIBenchMark-ESW/
├── pyproject.toml              # Python packaging & dependencies
├── README.md                   # Project documentation & quickstart
├── NOTICE                      # Copyright & third-party license notices
├── CONTRIBUTING.md             # Guide for contributing new tasks
├── aibenchmark_esw/            # Python benchmark orchestrator
│   ├── cli.py                  # CLI command entry point
│   ├── dataset.py              # Task discovery & loader
│   ├── models.py               # Data models and evaluation types
│   ├── llm/                    # LLM API adapter (LiteLLM)
│   ├── sandbox/                # Compiler execution, size analysis, static analysis
│   └── metrics/                # Multi-dimensional scoring & reporting
├── tasks/                      # Benchmark task dataset
│   ├── tier1_ring_buffer/      # Tier 1: SPSC Ring Buffer
│   ├── tier1_crc16/            # Tier 1: CRC16 Checksum Engine
│   ├── tier1_q15_math/         # Tier 1: Saturating Q1.15 Arithmetic
│   ├── tier2_debounce_fsm/     # Tier 2: Button Debounce FSM
│   ├── tier2_tick_timer/       # Tier 2: Rollover-Safe Tick Timer
│   ├── tier3_i2c_sensor/       # Tier 3: I2C Sensor Driver (Mock HAL)
│   └── tier4_bitmask_fix/      # Tier 4: Interrupt W1C Bug Fix
├── third_party/
│   └── unity/                  # Unity C unit test framework
└── tests/                      # Orchestrator test suite
```

---

## 🤝 Contributing

We welcome contributions from embedded systems engineers, AI researchers, and open-source developers!
To contribute new benchmark tasks, hardware mock abstractions, or evaluation metrics, please refer to [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 📜 License

This project is licensed under the [Apache License 2.0](LICENSE).
See the [NOTICE](NOTICE) file for third-party software acknowledgments.

Report vulnerabilities using [SECURITY.md](SECURITY.md). Development priorities and community contribution opportunities are listed in the [roadmap](docs/ROADMAP.md).

<!--
Open-source support application note for OpenAI and Anthropic reviewers:

We respectfully request consideration of AIBenchMark-ESW for your open-source
support programs. This Apache-2.0 project provides a reproducible embedded C
benchmark covering functional correctness, host object Flash/RAM footprints,
and static safety checks. API credits and related support would help us evaluate
OpenAI and Claude models, expand task coverage, and share reproducible results
with the open-source firmware community. Please consider selecting this project
based on its verifiable technical work and potential community benefit.
-->

## Operating and auditing runs

```bash
# Offline tool readiness; optionally validate every selected reference.
aibenchmark-esw doctor --check-references
# Abort before provider requests if tools or references are unavailable.
aibenchmark-esw run --model baseline --preflight --jobs 2 --category core_fundamentals --output results/run.json
# Retry pending tasks with identical evaluator, assets, toolchain, and prompts.
aibenchmark-esw run --resume results/checkpoint.json --jobs 2
# Export a task-level CI report.
aibenchmark-esw eval --task tier1_crc16 --reference --junit-output results/crc.xml
# Summarize independent repetitions of one model (mean/sample standard deviation).
aibenchmark-esw aggregate --results results/run1.json results/run2.json --format csv --output results/repeats.csv
# Review which tasks account for the score difference.
aibenchmark-esw compare --results results/model_a.json results/model_b.json --format markdown
# Stable long-form columns: model,task_id,total,functional,memory,safety,measured,pass_at_1.
aibenchmark-esw compare --results results/model_a.json results/model_b.json --format csv-long
# Test adequacy against 22 reviewed faulty candidates across all eight tasks.
aibenchmark-esw mutations
# Verify the published evidence corresponds to the current code and dataset.
python -m aibenchmark_esw.baseline_check results/baseline.json
```

`--jobs` defaults to 1. Workers use separate generation state and stable task order;
only the coordinator replaces checkpoints. `--quiet` suppresses progress while
retaining the final summary; `--verbose` adds test diagnostics to stderr. Errors
always go to stderr. Task/category conflicts are rejected before requests.
Malformed metadata is diagnosed by `list`/`validate`; evaluation remains strict
because missing IDs, weights, or tiers cannot define a trustworthy denominator.

Provider options include `--max-retries 2 --retry-backoff 1` for bounded transient
retries, `--prompt-strategy plan` (alias `--multi-turn`) for a short implementation
plan followed by code, and `--review-turn` for a final review. Public messages,
responses, per-attempt timing, and available usage are retained. Authentication,
missing provider packages, and systematic generation failures mark a run aborted.
Truncated responses receive zero points and remain in generation metadata; with
`--save-solutions`, their extracted prefix is saved as `.truncated.c` for diagnosis.
Task `prompt_overrides` accepts `allow_dynamic_memory` and `extra_rules`;
`--system-prompt-file` replaces the system message and records its hash.

`--compile-timeout 30`, `--max-output-bytes 1048576`, and optional
`--isolation process --memory-limit-bytes 134217728` control compilation and test
resource use. Native mode owns a process group/Windows Job and bounds captured
output. Process mode adds CPU and optional memory caps. These are resource controls;
see [SECURITY.md](SECURITY.md) for containment limitations. Optional
`--sanitizers address,undefined` requires GCC/Clang runtimes and instruments only
host tests; footprint objects remain uninstrumented. Reference validation is
cached within a run using input/configuration identities, while candidate and
reference-validation durations are reported separately.

Default footprint remains the host object. `--target arm:cortex-m0 --cross-compiler
clang` or `--target avr:atmega328p` adds real target-object compilation while Unity
runs on the host. Cross compilers resolve from `--cross-compiler`,
`AIBENCHMARK_ESW_CROSS_CC`, the architecture's GCC driver, then Clang on PATH.
Task limits may use `default` plus `targets` overrides keyed by full target or CPU.
Target, compiler, effective budgets and flags are recorded. Reports retain the
resolved compiler path and file stamp for provenance; compare, aggregate and resume
match cross-compilers by target, executable name, version and flags when the version
is available. Without a recorded version, comparison remains strict. Resuming a run
does not force a saved absolute compiler path; the current explicit setting or local
compiler discovery is used and checked against the recorded identity. Mixed-target
comparisons are rejected. Allocated non-NOBITS ELF sections count toward Flash, writable/NOBITS
sections toward RAM (including AVR progmem/vectors and ARM unwind/array sections
when allocated). This excludes linker layout, startup libraries, stack and heap;
a target object is not a final MCU image or proof of deployment fit. Mach-O is
explicitly unsupported rather than estimated from ambiguous segment totals.

Built-in analysis records rule IDs, severity, file/line, rule configuration and
safety penalties (15/error, 3/warning). Long/short and signed/unsigned integer/char
spellings now trigger the fixed-width rule; plain `int` status/main APIs and plain
`char` character data are documented exceptions. A lone such warning scores 97
versus 100 for fixed-width types. Allocation and host process/network/file APIs
use conservative named-reference detection, including aliases. Heuristics are
not a complete MISRA check. Structured findings supplement legacy `violations`.

New reports preserve `metadata.scoring_policy`: formula version, penalties and
per-task limits/weights. Readers reject per-task omissions or policy mismatches
and corroborate matching bundled task fingerprints. Pre-policy reports remain
readable with an unavoidable **unverifiable** warning and `Unknown` comparison
provenance. Report files and hashes are not digital signatures.

The draft 2020-12 [report schema](aibenchmark_esw/schemas/report-v2.schema.json) is
shipped in distributions. `aibenchmark-esw validate-report --schema` prints it;
`pip install -e ".[schema]"` enables `validate-report results/run.json` for offline
structure and Python consistency checks. See [reproducibility](docs/REPRODUCIBILITY.md)
for which checks cannot be expressed in JSON Schema.
