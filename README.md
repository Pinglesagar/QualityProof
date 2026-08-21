# QualityProof

**Playwright tells you a test passed. QualityProof tells you what that test is
entitled to prove.**

A local-first evidence engine that links requirements, test source, assertions
and runtime results, then applies one conservative rule-set to decide what each
test is entitled to claim. It runs on top of Playwright in **both Python and
TypeScript** and produces a single ledger from both.

The part with no equivalent I could find is the **composition**: one static
trust rule-set applied over pytest AST *and* the TypeScript compiler AST, with
pydantic → JSON Schema → TypeScript codegen as one-way authority and shared
fixtures validated by both languages' validators. The individual mechanisms all
have prior art, credited below.

> **Status:** pre-alpha. `VERIFIED` means *traceable and attributable*, not
> *correct*. Static analysis and passing tests are signals, never proof.

## Why

A green suite answers "did anything break". It does not answer the questions that
actually get asked in a release review:

- **Which requirements are not proven at all?** (`qualityproof coverage`)
- Which requirements are covered by assertions we can defend?
- Which of those assertions came from a human, a spec, or a language model?
- What changed between these two releases, and *what caused it*?
- Which tests only pass on the second attempt?

QualityProof answers those, and is deliberately conservative when it cannot.

## Quickstart

```bash
uv sync
uv run playwright install chromium
uv run python -m scripts.run_demo_workflow
```

That runs the whole pipeline against a local seeded demo application and writes a
benchmark under `benchmark-results/`. No API key, no network, no cloud.

For the TypeScript side:

```bash
npm --prefix packages/qualityproof-playwright ci
npm --prefix packages/qualityproof-playwright run build
uv run python -m scripts.run_interop_demo
```

## Measured results

Controlled nine-defect fixture, `demo/` v1 → v2. Expected findings live in
`demo/benchmark-ground-truth.json`, which the scorer loads and never writes. Note
the limit of that claim: it is a second file in the same repository by the same
author, expressed in the tool's own signal vocabulary, and with no git history
there is nothing to prove it was not edited. Judge the numbers on the disclosed
breakdown below, not on that assurance.

| Metric | Value |
|---|---|
| Precision | **0.818** (9 matched of 11 emitted signals) |
| Recall | 9 / 9 seeded defects — but read the caveat below |
| Unmatched signals | 2, both disclosed |
| Branch coverage (self) | 78% (`audit.py` 88%, `security.py` 95%) |
| Mutation score, trust modules | 67.5% (`audit.py` 84.5%) — measured before the latest 74 tests |
| Python tests | 290 |
| TypeScript tests | 31 |

**Read recall as 9/9 of a fixture the author wrote, not as a detection rate.**
Of the nine seeded defects: one (`SEED-SAFETY-001`) is identical in both releases
and is "detected" by the crawler reporting its own refusal, so it measures the
guard rather than a regression; two are detected because the workflow runs
hand-authored tests asserting those exact behaviours and observes them flip. Six
are both regressions and genuine discoveries.

Precision is 0.818 because two changed verdicts are emitted as unmatched signals.
An earlier version converted only two hard-coded test names into signals and
dropped the rest, which reported 1.000 — a dropped observation cannot cost
precision, so dropping is indistinguishable from accuracy. The mapping is now
derived and a test enforces that every changed verdict is emitted.

Page-state signals are **facet-attributed**: a signal is credited to a defect only
when the facet that changed could explain it. Caveat: the category rule lives in
the benchmark script rather than in `src/`, and on the shipped fixture it
currently rejects nothing.

Mutation testing was used as a source of work, not a badge. It identified
`_resolve_locator`, `classify` and `validate_http_origin` as the least defended
trust logic, which produced 46 boundary tests and took `audit.py` from 63.9% to
84.5%.

Scope: one controlled fixture. No third-party tool was run and no general
accuracy is claimed. See [the controlled demo](docs/CONTROLLED_DEMO.md).

### Against a real third-party application

The fixture above is the author's own. [`juiceshop/`](juiceshop/README.md) is the same
pipeline run end to end against OWASP Juice Shop v20.2.0, an application published
expressly to be tested, with a written BRS, SRS, test plan and authorisation basis:

| | |
|---|---|
| Requirements registered | 21 |
| Traceable to a verified test | 21/21 |
| **Demonstrated by a passing test** | **20/21** — P1 13/13, P2 6/6, P3 1/2 |
| Live execution | 20 passed, 1 strict xfail |
| Open findings | 1, accessibility, found by the tool |

Those two coverage rows are deliberately separate. Traceability asks whether a resolvable
test claims the requirement; demonstration asks whether the software actually does it. The
report said 21/21 verified while an open defect sat against one of them, because it never
consulted the execution verdicts it was already storing. `--require-priority P3` passes on
that requirement and `--require-demonstrated P3` fails on it, which is the distinction.

This is bounded at eight routes and does not test checkout or payment. What it does
demonstrate is that the traceability holds against software the author did not write.

## How it works

```
discover → plan → review → generate → test → audit → report → snapshot → diff
```

- **discover** — bounded, same-origin Playwright crawl. Cross-origin and mutating
  requests are aborted *before* network I/O. Controls are never activated. Crawls
  **once per role**, which makes a privilege boundary observable — an admin-only
  crawl sees an admin route return 200 before and after a guard is removed. There
  is no expected-policy model yet: cross-role differences are reported as
  questions, not verdicts.
- **plan / review** — journeys are mined deterministically from the persisted
  graph. A model may only *propose*: a proposal is checked against the persisted
  crawl graph and **refused rather than fallen back on** if it changes the route
  set, references an undiscovered control, changes a control's action semantics or
  relabels which requirement it covers. Validation is triggered by AI provenance,
  not by a self-declared `proposer` field inside the artifact being checked.
- **generate** — emits pytest-Playwright and/or `@playwright/test` from one
  approved scenario, preferring **role locators** and recording the CSS selector
  as an auditable contract rather than depending on it.
- **test** — parallel, shardable, with retries recorded as **FLAKY** rather than
  collapsed into PASS. pytest's JUnit output does not distinguish a retried test,
  so retries are captured from `report.outcome` and merged when verdicts are
  derived. Off by default (`retries = 0`).
- **audit** — parses test source with `ast` (Python) and the TypeScript compiler
  API (TS). Never imports it.
- **snapshot / diff** — compares releases across seven independent facets so a
  change can be attributed to a cause.
- **jira / boards** — a finding synchronizes to Jira or Azure Boards, dry-run by
  default, with the payload a write would send shown for review first. One
  fingerprint, derived from the finding, is tagged on the record so a repeated
  sync updates it rather than filing a duplicate. Identity and idempotency are
  shared; only the payload dialect and transport differ, and the two are
  type-bound so a Jira payload cannot reach the Azure transport.

Full detail: [architecture and threat model](docs/ARCHITECTURE.md) ·
[evidence model](docs/EVIDENCE_MODEL.md).

## What is *not* proven

The ledger reports what is covered. Nobody asks that in a release review — they
ask the complement, so that is a first-class command:

```console
qualityproof requirements import docs/requirements.yaml
qualityproof coverage --fail-on-orphans --fail-under 0.80
```

```
Requirements: 12; verified 9; partial 3; uncovered 0; orphan links 0; untraced tests 1.
```

It reports four things a green suite hides:

| Reported | Meaning |
|---|---|
| **Uncovered** | No test references the requirement at all. Silence is not success |
| **Partial** | Tests reference it, but none establish provenance |
| **Orphan links** | A test claims to cover an identifier the registry does not contain — it believes it covers something nobody specified |
| **Untraced tests** | Real assertions carrying no requirement link: work the ledger cannot credit |

The registry is the authority, and it is enforced rather than advisory: a cited
requirement must resolve in *its own registered source* and match the digest the
registry holds for it. An adversarial pass over this feature found seven ways to
forge a VERIFIED row — including that adding a locator made the check weaker, and
that one scope's import could silently delete another's requirements. All seven are
fixed with regression tests; the exercise is recorded in the changelog because
finding them is the point of building the gate.

## Ledger rules

| State | Requires |
|---|---|
| `VERIFIED` | A source assertion, a requirement link, **and** active authoritative provenance (`REQUIREMENT`, `HUMAN_APPROVED`, `API_SPEC`, or an approved AI hypothesis) |
| `PARTIAL` | Some traceability, but something is missing, stale or unresolvable. Baselines and observations alone can never reach `VERIFIED` |
| `UNKNOWN` | No usable metadata. The zero-configuration default |

These rules apply identically to Python and TypeScript. A TypeScript test earns
nothing for having run in a real browser. Generated Python tests emit the same
traceability metadata, so the `generate → audit` edge carries — though a mined
journey carries `OBSERVATION` provenance and therefore lands `PARTIAL`, which is
the honest ceiling on automatic generation.

## Two languages, one ledger

```
TypeScript specs → @qualityproof/playwright reporter ─┐
                                                      ├→ evidence engine → ledger
Python pytest-playwright → ast analysis ──────────────┘
```

The pydantic models are the contract; JSON Schema is exported from them; the
TypeScript types are **generated** from that schema, so drift is a compile error.
Shared fixtures in `interop/fixtures/` are validated by pydantic *and* ajv —
neither language is trusted to describe the contract alone.

See [`@qualityproof/playwright`](packages/qualityproof-playwright/README.md).

## Interviewing with this repo

[docs/INTERVIEW.md](docs/INTERVIEW.md) maps every common Playwright topic —
fixtures, isolation, auto-waiting, locator strategy, strict mode, sharding,
retries vs flake, trace debugging, network interception, API testing, ARIA
snapshots, the Reporter API, npm packaging — to real code here, with a
Python ↔ TypeScript equivalence table.

## Security posture

Credentials live in environment variables only; secret-shaped config keys are
rejected outright. Evidence is redacted before it is written. Traces and
screenshots cannot be redacted after capture, so they are disabled by default
when secrets are present and **quarantined** rather than sanitised when
explicitly enabled. See [`SECURITY.md`](SECURITY.md).

## Non-goals

- Replacing Playwright's runner, or claiming to beat it at being one
- Automatic test healing or mutation without human approval
- A general-purpose remote command runner
- Claiming static analysis or a passing run proves correctness

## Prior art

Every individual mechanism here has precedent, and pretending otherwise is the
fastest way to lose a technical conversation:

| Mechanism | Prior art |
|---|---|
| Hash-gated requirement traceability | [Doorstop](https://github.com/doorstop-dev/doorstop) (2013), OpenFastTrace |
| Role-differential access testing | OWASP ZAP Access Control Testing (~2014), Burp Autorize |
| Retry outcome as a distinct verdict | Maven Surefire `<flakyFailure>` (2015) |
| Cause attribution for visual/state diffs | Applitools Root Cause Analysis (2019), at finer granularity |
| Validating model output against a known element map | browser-use, Stagehand; Object Repository in UFT/Katalon |
| Schema-first cross-language contracts | cucumber/messages |

Playwright itself reports `flaky` natively and, from 1.56, ships agents that
verify selectors live against the running application — stronger evidence than
validating against a persisted crawl. This project complements Playwright; it does
not compete with it on execution.

## Known gaps

- Artifact capture is disabled whenever an environment variable that plausibly
  holds a credential is set — `GITHUB_TOKEN` alone is enough. That is the intended
  default, and the policy names the variables responsible instead of failing
  silently. It was **not** working as documented until running the Juice Shop
  engagement exposed it: the gate reused the redaction pattern, which matches `PWD`
  and `USER`, and those are set by every POSIX shell. Every run on every machine
  was therefore classed as authenticated and traces were disabled unconditionally.
  The credential signal is now separate and narrower than the redaction rule,
  because the two answer different questions: a username identifies an account but
  cannot authenticate as one.
- The facet-category rule that makes page-state matching strict lives in
  `scripts/benchmark_demo.py`, not in `src/`, and rejects nothing on the shipped
  fixture.
- Docker builds and Trivy image scans run in CI but cannot be exercised locally
  (no Docker on the development machine), so a break in them is only visible after
  a push.
- Mutation score covers four trust-critical modules only; the raw figure
  understates defended logic because mutants that only alter a human-readable
  message count as survivors. `generation.py` (55.0%) is weakest and is mostly
  string formatting.
- Azure deployment is manual and skipped without repository variables.

## License

Apache-2.0. See [LICENSE](LICENSE).
