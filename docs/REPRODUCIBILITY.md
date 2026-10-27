# Reproducing and comparing benchmark results

Install `pip install -e ".[dev,llm]"` and make a C compiler available. Run the
dataset asset check and reference baseline before spending provider credits:

```bash
aibenchmark-esw validate
aibenchmark-esw run --model baseline --output results/baseline.json
python -m unittest discover -s tests -v
```

Use exact, preferably snapshot, provider model IDs available to your account.
The README shows OpenAI/Anthropic runs with matching token limits and timeouts.
Credentials belong in environment variables, never in committed reports.

On PowerShell, set `$env:OPENAI_API_KEY` or `$env:ANTHROPIC_API_KEY`, then pass
the model directly, for example `--model "openai/<available-model-id>"`.
Temperature is omitted unless explicitly supplied. Provider defaults and model
sampling can still vary; recording settings does not guarantee identical outputs.

## What a report records

Schema version 2 includes UTC time, package/Python/host versions, compiler
identity/version and optimization, source revision when available, task
selection, and generation settings. Task fingerprints cover effective task
configuration, task text/sources/tests, and the Unity harness; candidate and
prompt hashes identify the generated implementation and submitted messages.
An evaluator hash identifies the Python implementation used for grading.
Checkout paths and CRLF/LF differences in recognized text assets do not alter
these hashes. Opaque/binary fixtures preserve all bytes, including newlines.

`validate` checks required files without compilation or API access. During a
run, missing, empty, or unreadable task assets fail that task before generation;
the failure stays in the report and denominator while other valid tasks continue.
Passing this file check does not establish reference correctness or budget fit.

Reports also record the selected static-analysis backend and cppcheck version.
Per-task safety results distinguish disabled, completed, failed, and unrun
cppcheck checks. A failed invocation retains its diagnostic and uses the built-in
rules; reports and comparisons explicitly warn about that reduced coverage.
Comparisons reject differing recorded analyzer configurations/versions and warn
when older reports lack analyzer information. CSV includes cppcheck completed
and failed task counts.

For repeatable analyzer selection, set `AIBENCHMARK_ESW_CPPCHECK=off` to use only
the built-in rules, or set it to a cppcheck executable path. When unset, PATH
discovery remains the default. A programmatic `StaticAnalyzer` executable
argument takes precedence. Compiler-matrix CI jobs explicitly use built-in
analysis; a separate Linux job installs cppcheck and requires real analysis of
a reference implementation and a deliberate out-of-bounds defect. To run those
integration checks locally, set `AIBENCHMARK_ESW_TEST_CPPCHECK` to your working
cppcheck executable and run `python -m unittest discover -s tests -p test_cppcheck_integration.py -v`.

Per-task generation metadata includes the requested and resolved model,
provider-reported token usage, elapsed generation time, and finish reason.
Missing usage stays unknown. API failures and token-limit truncations stay in
the report and denominator, receiving zero points. A failed or oversized
reference prevents memory normalization and produces an explicit error.

Use `--save-solutions results/model_sources` to save extracted implementation
files, then replay one with:

```bash
aibenchmark-esw eval --task tier1_crc16 --solution results/model_sources/tier1_crc16.c
```

To retain a local evaluation as JSON, add `--output results/local_crc16.json`.
It includes the same toolchain, analyzer, task, and candidate fingerprints as
`run`; local evaluations have no provider prompt or usage. Compilation and test
failures are still saved. `--reference` and `--solution` select alternative inputs
and cannot be combined.

## Checkpoints and interruption

`run --output` writes a checkpoint before any provider requests and after each
task, replacing the destination only after a complete JSON file is flushed.
An output-path failure therefore stops the initial run before generation. A
later write failure preserves the previous valid checkpoint.

Ctrl+C during generation/evaluation saves available completed results, usage,
and an `interrupted` run status, then exits with code 130. Unfinished tasks stay
in the report and denominator with zero scores and explicit diagnostics.
`metadata.pending_tasks` identifies them. An abrupt process termination leaves
the last checkpoint marked `running`. Report commands display unfinished state;
comparisons require finished runs. Use `run --resume checkpoint.json` to retry only pending tasks. Resume restores
saved options, retains completed slots, usage history and run_id, and rejects
changed fingerprints, toolchains, analyzers, platforms, targets or prompt settings
before replacing the checkpoint. Ordinary new invocations start fresh; retain
independent runs in distinct files.

## Fair comparisons

```bash
aibenchmark-esw compare --results results/openai.json results/claude.json --format cli
aibenchmark-esw compare --results results/openai.json results/claude.json \
  --format markdown --output results/comparison.md
aibenchmark-esw compare --results results/openai.json results/claude.json \
  --format csv --output results/comparison.csv
```

Runs must contain distinct model names and identical task sets, including
failures. Available weights, C standards, task/dataset and evaluator fingerprints, compilers,
host architecture, temperatures, token limits, and request timeouts must agree. Legacy reports
without provenance carry explicit warnings, rather than a claim of comparability.
CSV includes usage/duration task counts so partial totals can be identified.

Report readers validate booleans, counts, finite nonnegative measurements,
supported schema versions, and score consistency before rendering or comparing.
Uncompiled or incomplete tasks cannot carry positive scores. Valid legacy files
remain readable; metadata and weights they never recorded remain unknown.

Pass@1 here means one generated candidate passes all tests for a task. It is not
a multi-sample Pass@k estimate. Memory is the host implementation object's
allocated-section footprint, excluding stack/heap and the harness; it is not
the final MCU image. The safety scanner is heuristic. Host tests do not prove
portable ISR/thread synchronization or certification for safety-critical use.

GitHub Actions publishes baseline JSON and Markdown for each compiler job.
Those artifacts validate reference implementations and packaging, not paid
provider model performance. Real provider evaluations are opt-in and require
your own authorized API access.


## Current evidence, schema and policy

Verify the committed evidence without compiler/provider access:
`python -m aibenchmark_esw.baseline_check results/baseline.json`.
CI rejects evaluator or dataset drift on every push/PR. The refreshed published
baseline sets `source_revision` and `source_dirty` to null with
`source_revision_status=published-artifact`: a report cannot contain the hash of
the commit that adds itself. Use the publishing commit from `git log -- results/baseline.json`
and the verified code/dataset hashes. Runtime checkout reports still record HEAD
and dirty state; installed distributions report null rather than an unrelated
working-directory repository revision.

New schema-2 reports declare a scoring policy (`functional-gated-v1`, 15/error,
3/warning, per-task limits/weights). Deleting a per-task policy field or disagreeing
with the recorded policy is rejected. Matching local bundled-task hashes permit
configuration corroboration. Older reports without the declaration are explicitly
legacy/unverifiable; their averages remain descriptive, with warnings and unknown
comparison provenance. This is consistency checking, not report authentication.

The portable draft 2020-12 JSON Schema is available from
`aibenchmark_esw.report_schema.report_schema()` or `validate-report --schema`.
It can be used with any compatible validator without installing this evaluator.
The optional `schema` extra enables `validate-report file.json`, which also calls
the Python consistency validator. Schema covers shapes, ranges, enums and required
fields. Python additionally checks count/completion/return-code consistency,
functional ratios, safety penalties, limits-based memory rewards, composite
weights/policy, pending-task state and selection identity. JSON Schema does not
prove arithmetic consistency or correspondence to a particular source dataset.

`compare` adds a per-task total matrix and deterministic largest-gap summary,
for example `Largest gap: tier1_crc16 (model-a 100.00 vs model-b 40.00)`.
Unmeasured memory is labelled unavailable; aggregate memory preserves the previous
zero-filled average with a named measured-count warning. Existing aggregate CSV
columns are unchanged. `--format csv-long` exports fixed columns
`model,task_id,total,functional,memory,safety,measured,pass_at_1`, with empty memory
for unmeasured entries. `aggregate` accepts independent complete run IDs and
reports mean/sample standard deviation, coverage and per-task pass frequency;
Independent sampling collections also carry unbiased Pass@k estimates. A single
run has no standard-deviation or confidence-interval estimate.

`doctor --check-references` validates local tools with a trusted fixture before
requests. An absent optional analyzer is valid builtin mode; a broken explicitly
configured analyzer fails preflight. Mutations cover 47 hand-reviewed faulty
candidates across 15 tasks; compilation errors/timeouts are invalid probes,
not successful kills. CI requires reference success and no surviving/invalid
mutants. The suite contains 15 tasks and 150 C test cases, including bounded
COBS, SPI NOR, DMA ownership, fixed-point control, flash updates, framed UART,
and strengthened CRC/I2C/HOLD contracts.

Candidate tests and reference validation have separate durations. Caches use
assets, reference text, compiler identity/settings and target identity, retain no
compiled artifacts, and apply only within a process/run. Provider retries and
plan/review turns retain public transcripts and known usage; missing token usage
stays unknown. Resumed paid attempts remain in history with coverage rather than
being overwritten. System prompt files and per-task prompt overrides are hashed.

`--target arm:cortex-m0 --cross-compiler clang` and `--target avr:atmega328p`
compile target objects while tests run on the host. Default limits may be overridden
by `limits.targets["arm:cortex-m0"]` or CPU key `cortex-m0`; effective budgets,
compiler/flags and target are recorded and rendered. Mixed targets cannot be
ranked together. ELF allocated non-NOBITS sections count as Flash; writable and
NOBITS sections count as RAM, including architecture-specific allocated sections.
Measurements exclude startup/linker/stack/heap and do not establish final MCU fit.
Native host behaviour remains the default. ELF, COFF and Mach-O relocatable
footprints are supported; universal Mach-O conservatively uses the largest
component measurements. RISC-V targets include `riscv:rv32imac` and
`riscv:rv64gc`, with recorded ISA/ABI/compiler flags.

Structured safety findings retain rule_id, engine, severity, message, basename and
line alongside legacy prose. Provenance declares rule configuration, effective
standard policy and severity mapping; comparisons reject differences. File/line
are diagnostic lexer/cppcheck locations; macro expansion can limit precision.

## Sampling, usage, costs and offline exports

```bash
aibenchmark-esw run --model '<provider/model>' --samples 5 --pass-k 1,3,5 --temperature 0.7 --max-tokens 4096 --output results/samples.json --save-solutions results/sample_sources
aibenchmark-esw run --model '<provider/model>' --tasks tier1_crc16 --dry-run --max-tokens 4096 --input-cost-per-million 1 --output-cost-per-million 2
aibenchmark-esw run --model '<provider/model>' --max-tokens 4096 --max-cost-usd 2 --input-cost-per-million 1 --output-cost-per-million 2 --output results/budget.json
aibenchmark-esw replay --results results/model.json --solutions results/model_sources --output results/replayed.json
aibenchmark-esw report --results results/model.json --format html --output results/model.html
aibenchmark-esw report --results results/model.json --format sarif --output results/model.sarif
aibenchmark-esw compare --results results/model_a.json results/model_b.json --format junit --baseline-model model-a --regression-threshold 1 --output results/regressions.xml
aibenchmark-esw trend --results results/day1.json results/day2.json --format html --output results/trend.html
```

Custom prices above are examples, not provider price quotations. Cost is USD;
the client records provider response cost when supplied, otherwise known token
counts with explicit prices or LiteLLM's installed price table. Unknown pricing
is `null`, never zero. Each provider attempt and turn retains measurements;
failed/missing measurements keep full totals unknown and publish known subtotals
with coverage. Cached/reasoning token details are retained when reported.

The shared budget reserves a conservative UTF-8 prompt bound plus the configured
maximum output before each request, across retries, turns, jobs and samples.
Unknown prices or missing `--max-tokens` fail before making a request. An
attempt without measured cost keeps its reservation charged. It is an estimate
guard, not a provider billing guarantee; unusual provider surcharges or price
changes can exceed an estimate. A resumed budget can be increased explicitly
with `--max-cost-usd`; prior charged/reserved history stays recorded. Dry-run
validates task assets and builds prompts without compilation/provider requests;
unavailable prices/output bounds are shown as unknown. Dry-run cannot overwrite
a resume checkpoint.

`samples` is an additive schema-2 array of identified independent full reports.
The wrapper's ordinary task table represents its first completed sample;
`sampling.statistics` contains means, sample standard deviations, approximate
95% mean intervals, and `1 - C(n-c,k)/C(n,k)`. Normal approximation intervals
are descriptive and unreliable for small samples; they are not ranking proof.
Temperature zero warns about possibly redundant samples. Per-sample checkpoints
live beside the collection in `<output>.samples/`; save that folder when moving
an interrupted collection. Completed samples are retained; resume reruns only
pending samples/tasks. Each sample's saved C files have a separate directory.
Aggregation rejects unfinished collections and expands complete collections.

Replay checks every saved source's hash before grading, creates fresh evaluator/
task/toolchain provenance and a new run ID, and records the origin run ID.
Missing or truncated candidates are explicit zero slots; historical generation
data is labelled origin evidence rather than new provider spending. Replay runs
cannot be counted as independent model samples. HTML exports contain their own
scripts/styles and no external services. SARIF locations include only known
positive line numbers. Comparison JUnit creates threshold-based regression
failures; trend history groups incompatible fingerprints instead of ranking
them together. Trend compares matching per-task model pipeline proportions
across different sample counts, while preserving generation turn order and
distinguishing changed model mixtures or task assignments. Each history row
records sample and task-attempt counts; raw per-task model sequences retain
every contributing sample. Every file export protects source inputs and output ancestors.

## Optional execution and dataset checks

`--warnings` adds recorded compiler flags and scored warning findings.
`--extended-safety-rules` opts into bounded embedded heuristics (stdio/control
flow/VLA/recursion/math); these are diagnostic checks, not formal MISRA
certification. `--static-analysis-timeout` bounds cppcheck and records timeout
coverage explicitly. Differing rule/flag configurations cannot be compared.

`--isolation container --container-engine docker --container-image gcc:14`
compiles and tests inside a compiler-equipped image with an immutable recorded
image ID, disabled network, read-only root, dropped capabilities, non-root user
and a workspace-only mount. Podman and Linux bubblewrap are also supported.
These require working local runtimes; native/process remain the default. Read
[SECURITY.md](../SECURITY.md) for the exact trust boundary and prerequisites.

`mutations --auto --max-mutants 20 --jobs 2 --min-score 0.8` adds deterministic
operator mutants. The reviewed gate remains mandatory; automatically generated
invalid mutants are separated from killed/surviving compiled mutations. A score
is a coverage aid and does not prove correctness. Canary audit markers, private
test overlays, reproducible CRC variants and reference-similarity warnings are
described in [TASK_VARIANTS.md](TASK_VARIANTS.md). Variant fingerprints include
effective assets; held-out tests enter evaluation and never enter LLM prompts.
CI covers GCC/Clang/TCC, macOS, actual MSVC, sanitizer runtimes and Docker;
scheduled runs remain offline. The reusable consumer action is documented in
[GITHUB_ACTION.md](GITHUB_ACTION.md).
