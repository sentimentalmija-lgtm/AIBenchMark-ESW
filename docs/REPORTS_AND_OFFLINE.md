# Saved reports and offline workflows

All commands below operate on saved reports or local C files. They make no provider requests.

Saved task results include `test_result.output`, capped at 64 KiB of UTF-8 text,
and `test_result.output_truncated`. Offline HTML shows both in diagnostics.
The execution-time `--max-output-bytes` limit still applies; the saved-output
cap is an additional report-size bound. Older reports without these fields
load with empty output and no truncation marker. The cap does not shorten
in-memory test output or replace the separate evaluation `error_log`.

```sh
aibenchmark-esw report --results results/run.json --format html > report.html
aibenchmark-esw report --results results/run.json --format sarif > findings.sarif
aibenchmark-esw compare --results results/a.json results/b.json --format html --output comparison.html
aibenchmark-esw aggregate --results results/repeat-1.json results/repeat-2.json --format html --output repeats.html
```

HTML is one standalone file with embedded CSS and JavaScript. It supports text, status, tier, category, and minimum-score filtering; sortable columns; dimensional and memory budget meters; and expandable diagnostics, generation evidence, findings, and provenance. Category filtering uses recorded `metadata.task_categories`; legacy reports show unknown categories. Report text is escaped, and the file needs no external assets or network requests.

SARIF exports structured built-in, compiler, and cppcheck findings as SARIF 2.1.0 rules and results. Positive source lines become location regions; findings without a line retain the file location. Comparison JUnit uses one testcase per model/task pair and fails a testcase only when its score falls below the selected baseline by more than the threshold:

```sh
aibenchmark-esw compare --results results/baseline.json results/candidate.json \
  --format junit --baseline-model baseline --regression-threshold 2 --output regressions.xml
```

The default JUnit baseline is the highest-scoring comparison model. Choose `--baseline-model` explicitly for CI policy. The threshold is an absolute score-point delta, not a relative percentage.

JUnit testcase identities preserve XML-invalid model/task characters as `%`
followed by six uppercase hexadecimal digits, and literal `%` as `%25`.
Ordinary names remain unchanged. This prevents distinct labels from collapsing
when XML display diagnostics remove unsupported characters. Sample IDs retain
their existing URL-encoded identity suffixes.

## Regrade a saved candidate set

```sh
aibenchmark-esw replay --results results/original.json --solutions-dir saved_sources \
  --output results/regraded.json --junit-output results/regraded.xml --jobs 2
```

Sources normally use `<task_id>.c`, matching `run --save-solutions`. A `<task_id>/<entry_filename>` mapping is also accepted; supplying both is ambiguous and rejected. Every available complete source must match the original recorded candidate hash before grading or checkpoint writes begin. Hash mismatches abort preflight. Missing files, truncated files, and origins without a candidate hash remain explicit unsuccessful task slots, so the task denominator is preserved. Report/source/dataset/output collisions are rejected.

Replay uses the shared reference-validation and grading pipeline. Compiler, target, analyzer, execution limits, and external `--tasks-root` options produce fresh grading provenance and a fresh run ID. `metadata.run_mode` is `replay`; `origin_run_id` and each task's `origin_generation` and `origin_provenance` retain the source generation evidence. Replay has no new generation usage or spend. Its JSON is checkpointed atomically after each completed task, including an interrupted state if stopped.

Replay evaluates fixed candidates; it is not another independent generation sample. `aggregate` rejects replay reports. Use `compare` to examine grading changes when conditions are compatible, or `trend` to retain separate compatibility groups when the evaluator, dataset, or toolchain changes.

## Chronological compatible histories

```sh
aibenchmark-esw trend --results results/history --regression-threshold 2 \
  --format html --output history.html
```

`trend` accepts saved report files or directories through `--results`. It requires distinct run IDs, timestamps with a timezone, completed runs, and recorded grading provenance. Runs are ordered chronologically within groups defined by task fingerprints, evaluator/dataset identity, compiler/target options, host platform, analyzer configuration, and scoring policy. Compiler install paths and timestamps are informational. For each model and generation condition/identity sequence, a task drop greater than the threshold is flagged against its preceding compatible run. Changes to grading conditions form a separate group rather than a misleading regression comparison.

Generation identity preserves the contributing model sequence for each task across every sample. Task and sample ordering do not affect identity, but changing task-to-model assignments or the number of samples contributed by a sequence prevents regression comparison with that earlier run. JSON and CSV histories expose this mapping in `resolved_model_sequences_by_task`.

## Coverage and independent samples

`generation_summary` is an optional derived report field. Exact `total_tokens` and `total_cost_usd` are available only when every relevant task attempt has complete measurements. `known_total_tokens` and `known_cost_usd` retain measured subtotals. Complete/partial task counts and known/total request and turn counts distinguish missing coverage from zero use or zero cost. Cost per passing task is unavailable when complete spend or a passing task is unavailable. Original generation in replay provenance is never charged again.

Legacy reports with explicitly recorded usage and no coverage metadata retain their task-level measurement; request/turn coverage is marked unknown. Recorded retry defaults are zero retries and one-second backoff when omitted. Different retry or explicit token pricing policies are incompatible for comparison and form separate aggregation groups. Every successful contributing generation turn belongs to the effective model sequence, including an unknown earlier identity, so mixed pipelines are auditable and separated from pure model samples.

Sampling collections contain independent full child reports under `samples` and requested/completed/pending counts under `sampling`. Each child is independently validated, completed, identified, and has the same requested model and task set. Aggregation expands completed collections; unfinished collections cannot silently omit pending samples. Comparisons use all completed child samples to calculate task and overall means, sample standard deviation, and unbiased Pass@k. Report dimensions explicitly label their first sample, with collection estimates shown separately.

Mean confidence intervals use a labeled approximate 95% normal interval, clipped to the score range. They are descriptive and especially unreliable with small sample counts; one sample has no variability or interval estimate. Pass@k uses sampling without replacement from independent attempts, and the displayed percentage is the average over tasks. Aggregate success rates remain single-attempt rates.

See [the consumer GitHub Action](GITHUB_ACTION.md) for reproducible JSON/JUnit collection in another repository.
