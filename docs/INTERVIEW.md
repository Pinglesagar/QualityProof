# Playwright topics, mapped to this repository

A crib sheet for discussing this project. Every row points at real code you can
open, in both languages. The final section lists what the project deliberately
does *not* do — being able to state that clearly is worth more in an interview
than claiming completeness.

## Python ↔ TypeScript equivalence

| Concept | pytest-playwright (Python) | @playwright/test (TypeScript) |
|---|---|---|
| Test declaration | `def test_x(page: Page)` | `test("x", async ({ page }) => {})` |
| Fixtures | `@pytest.fixture` in `conftest.py` | `test.extend<Fixtures>({...})` |
| Context options | `browser_context_args` fixture | `use: {}` in `playwright.config.ts` |
| Base URL | `base_url` fixture | `use.baseURL` |
| Web-first assertion | `expect(locator).to_be_visible()` | `await expect(locator).toBeVisible()` |
| Soft assertion | `expect.soft(...)` | `expect.soft(...)` |
| Role locator | `page.get_by_role("button", name="Pay")` | `page.getByRole("button", { name: "Pay" })` |
| Test id | `page.get_by_test_id("total")` | `page.getByTestId("total")` |
| ARIA snapshot | `to_match_aria_snapshot(...)` | `toMatchAriaSnapshot(...)` |
| API testing | `api_request_context` fixture | `request` fixture / `APIRequestContext` |
| Auth reuse | `storage_state` in context args | `storageState` + a setup project |
| Parallelism | `pytest -n auto` (xdist) | `workers` + `fullyParallel` |
| Sharding | `--shard i/n` | `--shard=i/n` |
| Retries | `--reruns n` | `retries: n` |
| Tracing | `--tracing=retain-on-failure` | `use.trace: "retain-on-failure"` |
| Reporters | pytest plugins / JUnit XML | `reporter: [...]`, `Reporter` interface |

Both columns exist here, generated from one reviewed scenario, so you can show
the same behaviour expressed twice: [`generation.py`](../src/qualityproof/generation.py)
(`python_locator` / `typescript_locator`) and the equivalence tests in
[`test_dual_language_emission.py`](../tests/test_dual_language_emission.py).

## Topics and where to find them

| Topic | Where | What to say |
|---|---|---|
| **Locator strategy** | `models.py::Locator`, `LOCATOR_PREFERENCE` | Preference order role → test id → label → placeholder → text → CSS. The CSS is *kept* as a recorded contract, so locator drift is detected as evidence rather than suffered as a test failure. |
| **Strict mode** | `scenarios.py::_unique_role_locators` | Only role/name pairs unique on the page become assertions. Ambiguity is excluded at mining time, not hidden behind `.first`. |
| **Fixtures & isolation** | `fixtures.py` | Locale, timezone and viewport pinned so a failure means the app changed, not the host. Per-worker artifact directories keyed on `PYTEST_XDIST_WORKER`. |
| **Auth reuse** | `discovery.py` `--save-storage-state`, `fixtures.py::qualityproof_storage_state` | One login during discovery; every test reuses the session. No test performs a login, so no test can fail because of another's session. |
| **Auto-waiting vs hard waits** | `discovery.py` (`wait_for_load_state`) | A fixed sleep is too short on a slow host and wasted time on a fast one. There is one bounded `networkidle` wait, and the TypeScript lint config makes `waitForTimeout` an error. |
| **Parallelism & sharding** | `execution.py::_worker_argument`, `execute_tests(shard=...)` | xdist with `--dist loadscope`; shards partition by stable position so a file always lands in the same shard. |
| **Retries vs flake** | `execution.py::_execution_verdicts`, `fixtures.py::pytest_runtest_logreport` | pytest's JUnit writer records a retried test as a single clean `<testcase>`, so anything reading only the XML reports it as a straight pass. Retries are captured from `report.outcome == "rerun"` and merged when verdicts are derived. Playwright reports `flaky` natively, so both runners meet on one verdict. **Say it as plumbing hygiene, not differentiation** — Maven Surefire has emitted `<flakyFailure>` since 2015. |
| **Trace debugging** | `security.py::ArtifactPolicy` | Traces are on by default only when nothing sensitive is present. Authenticated capture needs an explicit flag and is quarantined, because a trace zip cannot be redacted after the fact. |
| **Network interception** | `discovery.py::enforce_route` | `context.route` used as a firewall — cross-origin and mutating requests are aborted *before* network I/O, not merely observed. |
| **API testing** | `models.py::ApiAssertion`, `fixtures.py::api_request_context` | Origin-relative, GET/HEAD only, bound to `base_url`, so a path-only assertion cannot reach another host or mutate state. |
| **Visual vs ARIA** | `models.py::AriaSnapshotAssertion` | ARIA snapshots are text: diffable, reviewable, redactable. Preferred over pixel comparison, which is none of those. |
| **Reporter API** | `packages/qualityproof-playwright/src/reporter.ts` | Implements `Reporter`; maps `expected/unexpected/flaky/skipped` onto the engine's verdicts. Being a reporter means Playwright keeps owning execution. |
| **Static analysis** | `audit.py::_assertions`, `analyze.ts::analyzeSource` | Parsed, never imported — in both languages. Importing a spec would execute module-level code from the system under test's repo. |
| **npm packaging** | `packages/qualityproof-playwright/package.json` | `exports` map with subpaths, `peerDependencies` on `@playwright/test`, `files: ["dist"]`. The example imports the package by its published name, so running it verifies the export map. |
| **Cross-language contracts** | `interop/fixtures/`, `test_interop_contract.py`, `test/interop.test.ts` | Shared golden fixtures validated by pydantic *and* ajv; TS types generated from the exported schema. Drift is a build failure, not a surprise. |
| **CI fan-out** | `.github/workflows/ci.yml` | Node job builds, lints, tests, runs the example against the Python demo app, then asserts TypeScript evidence reached the ledger. Two green jobs would only prove two runners ran. |

## Questions worth rehearsing

**"Why not just use `@playwright/test`?"** You should — this runs on top of it. The
gap it fills is that a passing suite tells you nothing about *which requirements
are covered by assertions you can defend*. That is a provenance problem, not a
runner problem.

**"How do you handle flaky tests?"** Retry, then record the retry as its own
verdict. The interesting part is *why it was hard*: pytest emits no rerun marker
in JUnit XML, so an earlier version of this claim was false — my test asserted on
a `<rerun>` element pytest never produces, which validated my assumption instead
of the runner. It now captures `report.outcome` directly, and a test drives a real
flapping test end to end. Surefire has done the same since 2015, so this is table
stakes done correctly, not a differentiator.

**"Why role locators?"** They survive restyling and assert what a user perceives.
The demo's flagship seeded defect renames a CSS hook while leaving role and name
intact — a CSS-based suite breaks, a role-based one correctly keeps passing, and
the change is reported by the evidence diff instead.

**"How do you know your tests are any good?"** Mutation testing on the modules
that decide what evidence proves: 67.5% combined, `audit.py` 84.5%. More
usefully, it was used as a work list — it named `_resolve_locator`, `classify` and
`validate_http_origin` as least defended, which produced 46 boundary tests and
moved `audit.py` up 20 points. The wrapper also refuses to publish a score when
nothing is killed, because that means the mutants were never imported, not that
the tests are worthless.

**"Your benchmark shows 0.818 — why not higher?"** Because it used to show
1.000 and that was an artifact. Only two hard-coded test names became signals and
the other changed verdicts were dropped; since precision is matched-over-emitted,
a dropped observation cannot cost anything, so dropping was indistinguishable from
accuracy. The mapping is now derived and a test enforces that every changed
verdict is emitted. Volunteer the recall caveat too: one of nine seeds is the
crawler detecting its own refusal, and two are found by purpose-written tests.

**"What's the weakest part?"** No git history, in a project about provenance.
After that: the facet-category rule that makes matching strict lives in the
benchmark script rather than the product, and rejects nothing on the shipped
fixture.

## Lead with these

Three things here are true, checkable, and rare. Everything else should be
described modestly.

1. **"Playwright owns execution; I only decide what a run is entitled to claim."**
   Then `audit.py`: parsed with `ast`, never imported, because importing a spec
   executes module-level code from the system under test's own repository. Same
   property via the TypeScript compiler API in `analyze.ts`.
2. **The interop contract.** pydantic is the source of truth, JSON Schema is
   exported from it, TypeScript types are generated from that schema, and
   `interop/fixtures/` is validated by pydantic *and* ajv — so neither language is
   trusted to describe the contract and drift is a compile error.
3. **Refuse rather than fall back.** When a model proposal does not match
   persisted discovery, it is rejected, not silently replaced with a live LLM call.
   Most agentic tools treat a cache miss as a cost optimisation and fall through.

## What this project does not do

- It does not replace Playwright's runner, and should not claim to. Playwright
  1.56+ ships agents that verify selectors live against the running application,
  which is stronger evidence than validating against a persisted crawl.
- `VERIFIED` means traceable and attributable, not correct.
- The benchmark covers one controlled nine-defect fixture the author wrote. It is
  not a general accuracy claim and no third-party tool was run for comparison.
- Every individual mechanism has prior art — Doorstop, OWASP ZAP, Surefire,
  Applitools, browser-use, cucumber/messages. The composition is the contribution.
  See the prior-art table in the README and claim that, not novelty.
- Docker image builds and Trivy scans are configured but unexercised locally.
- Azure deployment is manual and remains skipped without repository variables.
