# @qualityproof/playwright

Requirement traceability for Playwright Test, without replacing anything.

This package is a **reporter plus typed annotations**. Playwright keeps owning
execution — fixtures, projects, sharding, retries and tracing all behave exactly
as they already do. The reporter observes results, statically analyses your spec
files, and writes a language-neutral evidence manifest that the QualityProof
engine audits.

```
Playwright Test  →  reporter  →  evidence manifest  →  QualityProof engine  →  ledger
```

## Install

```bash
npm install --save-dev @qualityproof/playwright
```

Requires Node 20+ and `@playwright/test` 1.54 or newer as a peer dependency.

## Use

```ts
import { expect, provenance, requirement, test } from "@qualityproof/playwright";

test("checkout total", {
  annotation: [
    requirement("CHECKOUT-014"),
    provenance({
      kind: "REQUIREMENT",
      source: "docs/requirements.md",
      locator: "requirement:CHECKOUT-014",
    }),
  ],
}, async ({ page }) => {
  await page.goto("/checkout");
  await expect(page.getByTestId("total")).toHaveText("£99.99");
});
```

The literal `{ type, description }` form works identically; both are resolved
statically by the analyser.

Register the reporter:

```ts
// playwright.config.ts
export default defineConfig({
  reporter: [
    ["list"],
    ["@qualityproof/playwright/reporter", { strictAnnotations: false }],
  ],
});
```

Then audit the run:

```bash
qualityproof ingest .qualityproof/external/playwright-run.json
qualityproof report
```

## Reporter options

| Option | Default | Effect |
|---|---|---|
| `outputFile` | `.qualityproof/external/playwright-run.json` | Manifest destination. |
| `strictAnnotations` | `false` | When true, the run **fails** if any test declares no requirement. Turns traceability from a report into a CI gate. |
| `shard` | Playwright's own shard config | Shard label recorded in the manifest. |

Adopting the reporter cannot break an existing suite: an unannotated test still
runs and simply lands in the ledger as `UNKNOWN`.

## Sharded CI

Each shard writes its own manifest; merge them before ingest.

```bash
npx playwright test --shard=1/3
npx playwright test --shard=2/3
npx playwright test --shard=3/3
npx qualityproof-ts merge merged.json .qualityproof/external/playwright-run-*.json
qualityproof ingest merged.json
```

A test that passed in one shard and failed in another merges to `flaky`, never to
`pass` — the same rule the Python runner applies to reruns.

## CLI

```bash
npx qualityproof-ts analyze example/tests/*.spec.ts   # tests, assertions, annotations
npx qualityproof-ts verify manifest.json              # validate against the contract
npx qualityproof-ts merge out.json in1.json in2.json  # combine shard manifests
```

## What it does not do

- It does not run tests, discover applications, or crawl. Playwright and the
  Python engine do those.
- It does not decide what anything proves. A verdict of `VERIFIED` comes from the
  engine's provenance rules, applied identically to Python and TypeScript tests.
- It does not publish unredactable artifacts. Traces and screenshots from an
  authenticated run are recorded as quarantined rather than sanitised, because a
  trace zip cannot be redacted after capture.

## Design notes

**Specs are parsed, never imported.** Assertion analysis uses the TypeScript
compiler API. Importing a spec to count its assertions would execute module-level
code from the repository under test. It also means assertions are reported from
source, so a test that failed on its first `expect` still reports every assertion
it claims.

**Types are generated, not mirrored.** `npm run schema` regenerates
`src/generated/manifest.ts` from JSON Schema exported by the engine's pydantic
models, so a contract change is a compile error. Shared fixtures in
`interop/fixtures/` are validated by ajv here and by pydantic on the Python side.

Licensed under Apache-2.0.
