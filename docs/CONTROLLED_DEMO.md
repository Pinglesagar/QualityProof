# Controlled two-version demo and benchmark

The local seed shop provides reproducible input for QualityProof without an API key, hosted
service, production credential, third-party CAPTCHA, or Azure integration. `v1` is the baseline;
`v2` contains only the changes declared in `demo/seeded-defects.json`.

Run the full workflow:

```console
uv run playwright install chromium
uv run python -m scripts.run_demo_workflow
```

The script resets `.qualityproof/demo-workflow` and `benchmark-results`, then runs:

1. `init`
2. authenticated, bounded `discover` against v1
3. deterministic `plan`
4. non-interactive human-fixture `review`
5. deterministic `generate`
6. baseline contract `test` against v1
7. static `audit`
8. JSON/HTML `report`
9. immutable v1 `snapshot`
10. v2 `discover` and candidate contract `test` (expected regressions persist real failed verdicts)
11. immutable v2 `snapshot`
12. Markdown `diff`
13. independent-ground-truth artifact scoring plus separate fixture-integrity checks

Outputs are:

- `benchmark-results/workflow.log`
- `benchmark-results/workflow-summary.json`
- `benchmark-results/benchmark.json`
- `benchmark-results/benchmark.csv`
- `benchmark-results/benchmark.md`
- `.qualityproof/demo-workflow/.qualityproof/reports/`
- `.qualityproof/demo-workflow/.qualityproof/snapshots/`

Direct v1/v2 seed checks are labelled fixture integrity only; they confirm that benchmark inputs
were constructed correctly and are never reported as product detection. Separately, the benchmark
extracts candidate signals from persisted QualityProof snapshots, diffs, verdicts, and unknowns,
normalizes route parameters, then matches them against `demo/benchmark-ground-truth.json` (or an
independently supplied `--ground-truth` file). Precision is matched produced signals divided by all
produced signals; recall is matched expected finding IDs divided by all expected IDs. The report
includes matched IDs, misses, and unmatched signals. Findings with no distinct QualityProof output
remain misses.

No third-party tool is executed or named as a comparator. Runtime is a local observation and will
vary by machine. This benchmark is a controlled regression fixture, not evidence of general tool
accuracy or third-party superiority.

Run only the benchmark over an existing workflow project:

```console
uv run python -m scripts.benchmark_demo
# Optional independently supplied/hidden truth:
uv run python -m scripts.benchmark_demo --ground-truth /secure/ground-truth.json
```

The demo reset endpoint accepts localhost requests only. Its plaintext credentials, unsigned demo
identity cookie, and static CAPTCHA test field are deliberately unsuitable for production.
