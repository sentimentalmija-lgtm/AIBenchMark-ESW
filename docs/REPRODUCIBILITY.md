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
Checkout paths and CRLF/LF differences do not alter these hashes.

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
it is not a Pass@k estimator. A single run has no standard-deviation estimate.

`doctor --check-references` validates local tools with a trusted fixture before
requests. An absent optional analyzer is valid builtin mode; a broken explicitly
configured analyzer fails preflight. Mutations cover 22 hand-reviewed faulty
candidates across eight tasks; compilation errors/timeouts are invalid probes,
not successful kills. CI requires reference success and no surviving/invalid
mutants. The suite contains eight tasks and 77 C test cases, including bounded
COBS and strengthened CRC/I2C/HOLD contracts.

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
ranked together. Reports retain the resolved compiler path and file stamp for
provenance, while compare, aggregate and resume match cross-compilers by target,
executable name, version and flags when the version is available. Missing versions
retain strict comparison. Resume resolves saved absolute compiler paths from the
current explicit setting or local compiler discovery, then validates stable toolchain
identity. ELF allocated non-NOBITS sections count as Flash; writable and
NOBITS sections count as RAM, including architecture-specific allocated sections.
Measurements exclude startup/linker/stack/heap and do not establish final MCU fit.
Native host behaviour remains the default; Mach-O footprint is unsupported.

Structured safety findings retain rule_id, engine, severity, message, basename and
line alongside legacy prose. Provenance declares rule configuration, effective
standard policy and severity mapping; comparisons reject differences. File/line
are diagnostic lexer/cppcheck locations; macro expansion can limit precision.
