# Test Plan

**Project:** Quality evidence programme for the OWASP Juice Shop storefront
**Document ID:** TP-JS-001 · **Version:** 1.0
**Traces to:** [BRS-JS-001](BRS.md) · [SRS-JS-001](SRS.md)

## 1. Objective

Establish, on every pipeline run, which SRS requirements are proven by defensible
evidence and which are not — and fail the build when anything critical is unproven.

The measure of success is not a pass rate. A suite of 500 green tests that
establishes nothing about `JS-ADMIN-2` is worse than three tests that establish it,
because the first creates false confidence.

## 2. Scope

| In scope | Out of scope |
|---|---|
| Functional behaviour reachable through the UI and HTTP surface | Exploiting the planted vulnerabilities |
| Access-control reachability per role | Payment provider integration |
| Accessibility of form controls and headings | Full WCAG 2.2 AA audit |
| Responsive layout overflow at three viewports | Pixel-level visual regression |
| Release-over-release change detection | Load, soak and performance testing |
| Evidence integrity of the programme itself | Native mobile applications |

## 3. Test strategy

### 3.1 Risk-based prioritisation

Requirements are banded P1–P3 in the SRS. The pipeline gate is
`--require-priority P1`: **every P1 requirement must be verified or the build
fails.** P2 and P3 are reported but do not block. This is deliberate — a
percentage target invites gaming by adding easy tests, whereas a per-band
requirement cannot be satisfied except by covering the thing that matters.

### 3.2 Levels

| Level | What it establishes | Mechanism |
|---|---|---|
| Static audit | What a test *claims*, and whether the claim resolves | AST analysis; never executes the test |
| Route reachability | Which routes exist for which identity | Bounded multi-role discovery |
| UI behaviour | Observable state after navigation | Generated pytest-Playwright, human-approved |
| Accessibility | Programmatic labelling, heading structure | Native checks during discovery |
| Layout | Horizontal overflow per viewport | Geometry measurement, not screenshots |
| Release comparison | What changed, and which facet changed | Immutable snapshot diff |

### 3.3 Approach to negative requirements

`JS-AUTH-4`, `JS-BASKET-2`, `JS-ADMIN-2` and `JS-ADMIN-3` are negative: they
require that something *not* happen. Automated exploration can observe a denial on
the paths it tried; it cannot prove no path exists. These are evidenced as
role-differential observations and reported as such. **They are never recorded as
proof of absence.** A reviewer reads them as "no reachable path was found by this
crawl", which is a bounded and honest claim.

### 3.4 What automation will not do

By policy, and enforced in code rather than convention:

- No control is activated during discovery. No order is placed, no account deleted.
- No cross-origin request leaves the browser.
- No CAPTCHA is solved; such pages are recorded as refused.
- No credential value is written to retained evidence.

## 4. Entry criteria

| # | Criterion |
|---|---|
| E-1 | The application is reachable at the configured local origin with seeded data |
| E-2 | Customer and administrator credentials are present in the environment |
| E-3 | `requirements.yaml` regenerates from the SRS with no diff (no drift) |
| E-4 | Lint, strict type check and the unit suite pass |

## 5. Exit criteria

| # | Criterion | Gate |
|---|---|---|
| X-1 | Every P1 requirement is verified | `coverage --require-priority P1` |
| X-2 | No test cites a requirement absent from the registry | `coverage --fail-on-orphans` |
| X-3 | No requirement is entirely unreferenced | `coverage --fail-on-uncovered` |
| X-4 | No secret appears in retained evidence | redaction tests + secret scan |
| X-5 | Every generated test parses, lints and collects | generation validation |
| X-6 | The release diff has been reviewed, and each reported change is either expected or raised | human sign-off |

X-6 is deliberately human. A tool can tell you what changed; only a person can say
whether the change was intended.

## 6. Deliverables

| Artefact | Produced by |
|---|---|
| `ledger.json` / `ledger.html` | `qualityproof report` |
| `coverage.json` / `coverage.md` | `qualityproof coverage` |
| Requirements traceability matrix | generated — see [RTM.md](RTM.md) |
| Immutable release snapshots and diff | `qualityproof snapshot` / `diff` |
| Discovery evidence and unknown frontier | `qualityproof discover` |

## 7. Environment

| Item | Value |
|---|---|
| System under test | OWASP Juice Shop v20.2.0, local, `http://127.0.0.1:3000` |
| Browser | Chromium via Playwright |
| Identities | anonymous · customer · administrator |
| Viewports | 375×812, 768×1024, 1280×800 |
| Credential source | environment variables only |

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Discovery mutates application state | High | Read-only request policy enforced pre-network; destructive labels refused |
| Credentials leak into committed evidence | High | Redaction before write; artefact capture disabled when secrets are present |
| Generated assertions are ambiguous under Playwright strict mode | Medium | Only role/name pairs unique on a page become assertions |
| The requirement baseline drifts from the document | Medium | `requirements.yaml` is generated; CI fails on drift (E-3) |
| Coverage is satisfied by shallow tests | Medium | Verification requires resolvable provenance, not merely a passing test |
| The tool's own evidence is wrong | High | Mutation testing of the trust rules; adversarial review of the gates |

## 9. Reporting cadence

Every pipeline run publishes the ledger, the coverage report and the release diff
as build artefacts. The single number a release reviewer reads first is
**unproven P1 count**. It should be zero, and when it is not, the report names
which ones.
