# Contributing

## Setup

```bash
uv sync
uv run playwright install chromium
npm --prefix packages/qualityproof-playwright ci
npm --prefix packages/qualityproof-playwright run build
```

## The gate

Everything below must pass. CI runs the same commands.

```bash
uv run ruff check .
uv run mypy
uv run pytest
npm --prefix packages/qualityproof-playwright run lint
npm --prefix packages/qualityproof-playwright test
```

End-to-end checks:

```bash
uv run python -m scripts.run_demo_workflow    # pipeline + benchmark
uv run python -m scripts.run_interop_demo     # cross-language evidence
uv run python -m scripts.assert_interop_ledger
```

## Standards

- **Strict typing.** `mypy --strict` covers `src`, `tests`, `demo` and `scripts`.
  `any` is a lint error in the TypeScript package's `src`.
- **Comments explain *why*.** The what is readable from the code. Most comments
  here justify a constraint — read a few before writing one.
- **Tests state the risk they defend.** A docstring saying what breaks if the
  behaviour regresses is worth more than a restatement of the assertion.

## Rules that are not style preferences

1. **Never widen a trust rule to make a test pass.** If a rule fires, it is
   probably right. During development the AI-assertion invariant rejected a mined
   scenario; the fix was to demote the assertions to hypotheses, not to relax the
   invariant.
2. **Never edit `demo/benchmark-ground-truth.json` to improve a score.** It is
   loaded independently precisely so it cannot be tuned. Make detection better, or
   make the matcher stricter.
3. **Never report a metric you do not trust.** `scripts/mutation_report.py`
   refuses to emit a score when every mutant survives, because 0.0 would be a
   fabricated number rather than a measurement.
4. **Prefer role locators.** The TypeScript lint config makes raw CSS selectors
   and `waitForTimeout` errors in the example suite. Python emission follows the
   same preference order.

## Changing the interop contract

The pydantic models in `src/qualityproof/models.py` are authoritative.

```bash
npm --prefix packages/qualityproof-playwright run schema   # regenerate TS types
uv run pytest tests/test_interop_contract.py              # pydantic side
npm --prefix packages/qualityproof-playwright test        # ajv side
```

Add a fixture to `interop/fixtures/` for any new shape. Both suites read it.
