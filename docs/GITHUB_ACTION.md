# Run the benchmark in a consumer repository

The root `action.yml` installs the benchmark, evaluates candidates, and uploads JSON and JUnit evidence even when the benchmark fails. The action preserves the benchmark exit status, so failed evaluations fail the job after artifact upload. A host C compiler must already be installed on the runner; Ubuntu hosted runners provide GCC.

The default hidden artifact directory is uploaded with `include-hidden-files: true`.
The smoke workflow downloads the uploaded artifact and verifies both evidence files.
`exit-code` normalizes signal termination to 128 plus the signal; `raw-exit-code`
retains the subprocess value. Extra arguments cannot replace dedicated model,
task, dataset, solution, compiler, concurrency, or artifact inputs, nor resume or
dry-run the action. This protects provider-extra installation and run identity.

Every saved run also produces a compact Actions step summary, including failed
or interrupted evidence. Offline export uses
`aibenchmark-esw report --results run.json --format github-summary --output summary.md`.
It escapes labels and omits provider responses and diagnostic bodies.

```yaml
permissions:
  contents: read
jobs:
  benchmark:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: sentimentalmija-lgtm/AIBenchMark-ESW@main # Pin a reviewed commit SHA in production.
        with:
          model: baseline
          tasks: tier1_crc16,tier1_ring_buffer
          compiler: gcc
          jobs: '2'
          artifact-name: embedded-reference-evidence
```

`model: baseline` runs completely offline. To evaluate a local candidate, select `command: eval`, set `tasks` to one task ID, and set `solution` to its source pathname. Omitting `solution` evaluates that task's golden reference.

```yaml
      - uses: sentimentalmija-lgtm/AIBenchMark-ESW@main
        with:
          command: eval
          tasks: tier1_crc16
          solution: firmware/crc16.c
          extra-arguments: '["--tasks-root", "benchmark/tasks", "--compile-timeout", "30"]'
```

For generation, set a provider-qualified `model` and supply the provider's credentials through step `env`, for example `OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}`. Credentials are inherited by the provider client, not passed as command arguments. This opt-in mode makes provider requests and can incur spend. `extra-arguments` accepts a JSON array of individual CLI arguments, including generation settings and spend limits. Arguments are executed directly without shell interpolation; `--output`, `--junit-output`, and `--resume` are reserved by the action.

By default, the action installs its own source revision. `package-version` selects an exact published PyPI version instead; choose a version that supports the requested CLI flags. `python-version` defaults to `3.11`. `output-directory` defaults to `.aibenchmark-artifacts`, and the action exposes absolute `json`, `junit`, and `exit-code` outputs for subsequent steps.

Each invocation writes into a new subdirectory of `output-directory`. Use the
`json` and `junit` outputs to find its files. Reusing the parent directory preserves
earlier evidence without uploading or summarizing it as the current run. Failures
that produce a new checkpoint still upload it; failures before checkpoint creation
have no report to upload or summarize.

The repository's `Consumer action offline smoke` workflow exercises this composite action with two real references, validates its JSON and JUnit artifacts, and never requests a provider.
