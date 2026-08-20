# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project is pre-alpha and not yet released.

## [Unreleased]

### Added

- **`@qualityproof/playwright` npm adapter** — a native Playwright Test reporter,
  typed requirement/provenance annotations, static assertion analysis via the
  TypeScript compiler API, a redaction-aware evidence manifest, a
  `qualityproof-ts` CLI (`analyze`, `verify`, `merge`), and an example suite that
  runs against the Python demo application.
- **Cross-language evidence path** — `qualityproof ingest` merges an external run
  manifest into the ledger under the identical trust rules. TypeScript types are
  generated from the engine's exported JSON Schema; shared golden fixtures in
  `interop/fixtures/` are validated by pydantic *and* ajv.
- **`qualityproof generate --language python|typescript|both`**, with equivalence
  tests asserting both emitters describe the same behaviour.
- **Semantic locator strategy** — role, test id, label, placeholder, text and CSS,
  with the CSS retained as an auditable contract. Emission prefers role locators;
  only role/name pairs unique on a page become assertions, keeping generated
  assertions strict-mode-safe.
- **Fixture layer** (`qualityproof.fixtures`) with pinned locale, timezone and
  viewport, per-worker artifact directories, storage-state reuse and an
  origin-bound `api_request_context`. A `conftest.py` is generated alongside tests.
- **Parallel and sharded execution** with xdist, plus `VerdictStatus.FLAKY`: a test
  that only passes on a rerun is never recorded as passing.
- **`ArtifactPolicy`** — trace and screenshot capture resolved from the run's
  secret exposure; authenticated capture requires explicit acknowledgement and is
  quarantined and excluded from published output.
- **Three new detectors** — native accessibility checks, per-viewport layout
  overflow measurement, and multi-role differential crawling (`--role`) that makes
  authorization boundaries observable.
- **Facet-attributed release comparison** — seven independent page facets, so a
  page-state change can be credited only to a cause that explains it.
- **API and ARIA-snapshot assertions**, and soft assertions in both emitters.
- Branch coverage with a CI floor (76% measured), and mutation testing of the
  trust-critical modules (67.5% combined, `audit.py` 84.5%).
  `scripts/mutation_report.py` reads outcomes from the runner's own per-mutant
  output and refuses to publish a score when nothing is killed, because a run in
  which the mutated package was never imported is an inert oracle rather than a
  0% result.
- 46 boundary tests for `_resolve_locator`, `classify`, `_find_identifier`,
  `_merge_metadata`, `build_ledger` and `validate_http_origin`, written in
  response to the mutation report identifying them as the least defended trust
  logic. `audit.py` rose from 63.9% to 84.5%.
- `docs/ARCHITECTURE.md` and `docs/INTERVIEW.md`.

### Added — requirement coverage

- **`qualityproof coverage`** answers what is *not* proven: uncovered
  requirements, requirements referenced only by unproven tests, orphan links where
  a test cites an identifier the registry does not contain, and untraced tests
  that assert something without naming a requirement. JSON and Markdown output,
  with independent CI gates (`--fail-under`, `--fail-on-uncovered`,
  `--fail-on-orphans`).
- **`qualityproof requirements import` / `list`** register an authoritative
  requirement set, partitioned by scope so re-importing replaces only its own
  entries. Seed manifests are accepted alongside requirement collections, because
  a seeded-defect record is a specification of expected behaviour.
- The registry is now consulted when resolving provenance: a test can no longer
  mint coverage of a requirement by citing an identifier that appears only in a
  file it chose itself.

### Changed

- Snapshot schema `1.1` adds per-page link maps, facet digests and role tags.
  Schema `1.0` snapshots still load.
- Route retargets are reported once instead of as an unrelated removal plus
  addition. Application-metadata differences are reported as context, not
  findings. Benchmark precision rose from 0.750 to 1.000 with the ground-truth
  file unchanged.
- Recall rose from 0.667 to 1.000 by capturing richer evidence — label
  associations, response status, layout geometry — rather than by relaxing any
  matcher. The matcher became stricter.
- Discovery replaced a fixed sleep with a bounded `networkidle` wait.
- Redaction now covers username-shaped environment variables and word-bounds
  short secrets so they cannot corrupt unrelated evidence.

### Fixed — seven bypasses found by attacking the coverage gate

An adversarial pass over the new coverage feature confirmed seven ways to make the
tool report something false. All were reproduced against real code before fixing,
and each now has a regression test.

- **Adding a locator made the requirement check *weaker*.** The located branch
  checked only that the identifier appeared in some file and in the registry, so a
  test could cite a description it wrote itself and be credited against the real
  requirement. A located claim must now resolve in the requirement's own
  registered source and, where the registry holds a digest, match it.
- **`API_SPEC` without a locator was satisfiable by hashing any readable file** —
  a digest of a shopping list resolved as authoritative. A resolvable
  `operation:` locator is now required.
- **Absolute source paths skipped the project-containment check**, so evidence for
  a VERIFIED requirement could live entirely outside the repository, and therefore
  outside code review. Containment now applies unconditionally.
- **A seed manifest could take over a specification identifier.** Seed-derived
  requirements are namespaced under `SEED-`, and their provenance now records the
  real manifest path and a content digest rather than an unresolvable label.
- **One scope's import could silently delete another scope's requirements.**
  Records are keyed globally while ownership is per-scope, so a stale sweep removed
  ids another manifest still claimed — turning a failing coverage gate green.
  Cross-scope collisions now fail loudly and deletion is restricted to unclaimed
  ids.
- **`coverage` graded frozen ledger rows.** Editing a requirement after an audit
  left a stale VERIFIED in place. Coverage now re-classifies against the live
  registry and reports rows whose stored status has drifted.
- **Approval corroboration was a substring match**, so a provenance source of
  `"e"` matched any review event and one genuine approval could be borrowed for an
  unrelated test. Matching is now on identity.
- External manifests are checked for project containment on ingest, since foreign
  evidence reaches the same trust rules as local source.

### Fixed — three provenance-gate holes

- **A human approval was self-certifying.** When the repository held no review
  events the resolver fell back to trusting the record, so any hand-written
  `HUMAN_APPROVED` provenance was authoritative on sight. A repository holding no
  approvals is now treated as evidence that nothing was approved.
- **A whole-file hash satisfied a fragment locator.** A digest could validate
  against the entire source while the locator pointed elsewhere, proving only that
  the file existed. A located claim must now match the located fragment.
- **Provenance sources resolved through a working-directory fallback**, so the
  same audit passed or failed depending on where it was invoked from. Sources now
  resolve inside the project only.

Closing these dropped the demo's VERIFIED rows from 9 to 0, confirming they had
been satisfied by accident. The demo workflow now copies its requirement source
into the project and registers it, so those rows are earned.

### Fixed — issues found by an adversarial review of this project's own claims

- **FLAKY was unreachable from pytest.** `_execution_verdicts` looked for a
  `<rerun>` element and duplicate `<testcase>` entries; pytest's JUnit writer
  emits neither, so a genuinely flapping test was recorded as a straight pass —
  exactly the collapse the feature claimed to prevent. The previous test passed
  because it asserted on hand-written XML containing an element pytest never
  produces. Retries are now captured from `report.outcome` via a
  `pytest_runtest_logreport` hook and merged when verdicts are derived, and the
  test drives a real flapping test end to end.
- **Generated Python tests could never be VERIFIED.** The emitter wrote no
  `@qualityproof` metadata, so every generated test audited as `UNKNOWN` and the
  `generate → audit` edge of the pipeline carried nothing. Metadata is now emitted
  as literals the auditor reads without importing, long values wrap as implicit
  concatenation, and the demo workflow audits the generated suite.
- **The auditor was blind to `expect.soft(...)`.** `_is_expect_call` required the
  invocation callee to be the bare name `expect`, so every soft assertion was
  invisible — including the ones this project's own generator emits. Mirror of a
  bug already fixed in the TypeScript analyser.
- **Emitting an API assertion produced a `NameError`.** The body referenced
  `api_request_context` while the signature never requested the fixture; no demo
  scenario happened to trigger it.
- **`_facet_changes` erased facet regressions.** States sharing a `(route, role)`
  pair were merged with `dict.update()`, keeping only the last-sorted state's
  digests. `/products/:int` carries three states per role, so the bug was live.
- **The benchmark silently dropped observations.** A hard-coded two-entry name map
  converted two changed verdicts into signals and discarded the rest. Because
  precision is matched-over-emitted, a dropped observation cannot cost anything.
  Derivation is now general and a test enforces that every changed verdict is
  emitted; precision consequently reports 0.818 rather than 1.000.
- **The destructive-action guard missed most destructive phrasings.** Seven
  English substrings let "Buy now", "Transfer funds", "Place order" and even
  "Log out" through, while flagging "PayPal" and "Payment history". Matching is now
  on word boundaries over a wider term set, with ambiguous nouns deliberately
  excluded and multi-word phrases used instead.
- **The model-proposal gate trusted the artifact it was guarding.** Validation was
  keyed on `scenario.proposer`, a self-declared string, so relabelling an AI
  proposal as `deterministic` skipped every check. Validation now triggers on
  AI provenance or hypothesis assertions.
- **The pre-generation gate rejected valid input.** `generate_approved` re-mined
  candidates without requirements, so every requirement-linked model scenario
  failed as having substituted its requirement associations; the gate had never
  fired successfully.
- **Evidence capture evaporated silently.** Any environment variable with a
  credential-shaped *name* marks a run authenticated and disables artifacts; an
  unrelated third-party variable was doing so. `ArtifactPolicy` now reports which
  variables were responsible, by name and never by value.
- Added coverage for the previously untested crawl firewall — including a real
  Chromium test proving cross-origin requests are aborted while same-origin reads
  continue — plus the artifact quarantine path, role specs and cross-role
  authorization observations.

### Fixed

- `execute_tests` invoked a bare `pytest` from `PATH`, which could run a different
  environment's runner than the one it was installed into.
- `junit.xml` was deleted after parsing, destroying the run's primary
  machine-readable evidence. It is retained, redacted through the XML tree so the
  replacement token cannot break well-formedness.
- `extra_args` reached pytest unvalidated while every other input path was
  checked; there is now an allow-list.
- The TypeScript analyser counted `expect.soft(x)` as an assertion in its own
  right, inflating assertion totals.
- `Frontier` internals were reached from outside the class.
