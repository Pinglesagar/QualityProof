## What and why

<!-- The why matters more than the what; the diff shows the what. -->

## Gate

- [ ] `uv run ruff check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
- [ ] `npm --prefix packages/qualityproof-playwright run lint && npm --prefix packages/qualityproof-playwright test` (if TS changed)
- [ ] `uv run python -m scripts.run_demo_workflow` (if the pipeline changed)

## Honesty checklist

- [ ] No trust rule was widened to make a test pass
- [ ] `demo/benchmark-ground-truth.json` is unchanged, or the change is justified
      and is not a score improvement
- [ ] Any reported metric was actually measured; nothing is asserted that was
      skipped or unverified
