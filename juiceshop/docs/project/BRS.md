# Business Requirements Specification

**Project:** Quality evidence programme for the OWASP Juice Shop storefront
**Document ID:** BRS-JS-001 · **Version:** 1.0 · **Status:** Baselined

> **Simulation notice.** This is a training artefact. The "business" is fictional;
> the system under test is [OWASP Juice Shop](https://owasp.org/www-project-juice-shop/),
> an intentionally insecure application published expressly so that people may
> test against it. No third-party production system is in scope, and none is
> tested. See [`docs/project/AUTHORISATION.md`](AUTHORISATION.md).

## 1. Purpose

Define *why* quality work is funded on this product, in business terms, so that
technical requirements in [SRS-JS-001](SRS.md) can be traced to a business need
rather than to a tester's preference.

## 2. Background

The Juice Shop storefront sells consumer goods online. It supports anonymous
browsing, registered customer accounts, and an administrative back office. The
organisation has grown by acquisition; the storefront was assembled quickly and
has no documented requirement baseline. Consequently:

- Nobody can state which customer-facing behaviours are covered by automated tests.
- Regressions are found by customers rather than by the pipeline.
- A prior release exposed an administrative page to ordinary customers. It was
  detected six days later, by a customer.

## 3. Business objectives

| ID | Objective | Success measure |
|---|---|---|
| BR-1 | Know what our tests actually prove | Every requirement in the SRS is reported as verified, partially covered, or unproven, on every pipeline run |
| BR-2 | Never again ship a privilege regression undetected | Any change in what a role can reach is surfaced before release |
| BR-3 | Protect the checkout revenue path | Basket and checkout behaviours are covered by traceable, defensible assertions |
| BR-4 | Meet accessibility obligations | Form controls carry programmatic labels; regressions are detected automatically |
| BR-5 | Reduce release-decision time | A release reviewer can answer "what is not proven?" from one artefact, without asking an engineer |
| BR-6 | Make quality evidence auditable | Every "verified" claim traces to a written requirement, and the linkage is machine-checked |

## 4. Stakeholders

| Role | Interest | Accepts |
|---|---|---|
| Head of Engineering | Release confidence, cycle time | BR-1, BR-5 |
| Security Lead | Access-control correctness | BR-2 |
| Head of Commerce | Revenue path availability | BR-3 |
| Accessibility & Compliance | Legal obligation | BR-4 |
| QA Lead (owner of this programme) | Evidence quality | BR-1, BR-6 |

## 5. Scope

**In scope.** Anonymous browsing, customer registration and login, product
catalogue, product detail, basket, checkout initiation, customer profile,
administrative surface reachability, and the privilege boundary between customer
and administrator.

**Out of scope.** Payment provider integration (third party), email delivery,
native mobile applications, load and performance characteristics, and penetration
testing of the deliberately-planted vulnerabilities. This programme measures
*functional and access-control regression*, not exploitability.

## 6. Constraints

| ID | Constraint |
|---|---|
| C-1 | Testing runs against a locally hosted instance only. No shared or third-party environment is targeted |
| C-2 | Automated exploration must not activate state-changing controls. Discovery is read-only by policy |
| C-3 | Credentials are supplied by environment variable and must never appear in committed evidence |
| C-4 | Evidence must be reproducible offline; no external service may be required to produce a release report |

## 7. Assumptions

- The application is available at a known local origin with seeded demo data.
- Two identities exist with distinct privilege: a registered customer and an administrator.
- The requirement baseline in the SRS is authoritative; disagreement is resolved by amending the SRS, not by adjusting tests.

## 8. Acceptance

This BRS is satisfied when each business objective traces to at least one SRS
requirement, and the pipeline reports coverage of those requirements on every run.
Traceability is generated, not hand-maintained — see
[`RTM.md`](RTM.md).
