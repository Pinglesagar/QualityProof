# Worked engagement: OWASP Juice Shop

A complete QualityProof engagement against a real third-party application, run the way
a delivery team would run it: business requirements, a specification, a machine-readable
requirements registry, an authored Playwright suite traced to that registry, and a
coverage report that names what is *not* proven.

The system under test is [OWASP Juice Shop](https://owasp.org/www-project-juice-shop/)
v20.2.0, hosted locally. It is published expressly to be tested, which is why it was
chosen: no third-party site is crawled, and the authorisation basis is recorded in
[`docs/project/AUTHORISATION.md`](docs/project/AUTHORISATION.md).

## Why this directory is self-contained

QualityProof resolves every provenance source *inside the project directory*, so an
audit gives the same answer regardless of the working directory it was invoked from.
That rule is what forces this layout: the specification, the requirements registry and
the tests all live under `juiceshop/`, and a test citing `docs/project/SRS.md` resolves
against this project rather than against whatever happens to sit beside the caller.

```
juiceshop/
  qualityproof.toml            project configuration (no secrets)
  docs/project/
    BRS.md                     business requirements
    SRS.md                     specification, 21 numbered requirements
    TEST-PLAN.md               scope, environment, entry and exit criteria
    AUTHORISATION.md           why testing this target is permitted
    requirements.yaml          machine-readable registry, the audit's authority
    RTM.md                     generated traceability matrix
    findings/                  open findings, one JSON file each
  scenarios/custom/            the Playwright suite (21 tests)
```

## Reproduce it

Start Juice Shop on `http://localhost:3000`, then:

```bash
qualityproof requirements import juiceshop/docs/project/requirements.yaml --project juiceshop
qualityproof discover http://localhost:3000/ --project juiceshop --max-pages 8 \
  --seed-route "/#/search" --seed-route "/#/login" --seed-route "/#/contact"
qualityproof audit juiceshop/scenarios/custom --project juiceshop
qualityproof test --project juiceshop
qualityproof coverage --project juiceshop
```

Juice Shop is an Angular single-page application, so its routes are URL fragments.
Discovery treats a fragment as a distinct route and forces `about:blank` between
fragment navigations, because a same-document hash change fires no navigation event
and would otherwise be recorded as the previous page. Without that, discovery finds
one route; with it, eight.

## Measured result

| | |
|---|---|
| Requirements registered | 21 |
| Audit | 21 verified, 0 partial, 0 unknown |
| Coverage | 21/21 verified — P1 13/13, P2 6/6, P3 2/2 |
| Execution against the live app | 20 passed, 1 strict xfail |
| Open findings | 1 |

Every one of the 21 tests carries a `REQUIREMENT` provenance record naming a requirement
in `requirements.yaml`, and the auditor re-reads that file and checks the recorded digest
before crediting the claim — so a requirement cannot be edited out from under a test that
claims to prove it.

## The one open finding

`JS-CAT-2` requires the catalogue to expose exactly one level-one heading. It exposes
three, so assistive technology cannot identify the page. The finding was produced by the
accessibility facet during role-scoped discovery, not by reading the application's code,
and is recorded in [`docs/project/findings/JS-CAT-2-multiple-h1.json`](docs/project/findings/JS-CAT-2-multiple-h1.json).

The verifying test is marked `xfail(strict=True)`, not skipped. That distinction is the
point: a skip is silence, whereas a strict expected failure asserts the defect is still
present and *fails the suite the moment the application is fixed*, forcing the finding to
be closed rather than left to rot into a false statement.

## What this engagement does not prove

- Eight routes of a large application, bounded by `--max-pages 8`. This is a
  demonstration of the pipeline, not an exhaustive test of Juice Shop.
- Checkout, payment and order history are specified in the SRS and deliberately
  untested: the destructive-action guard refuses to place orders, and no purchase
  is performed against any application.
- The requirements were written by the author of the tool, from the application's
  observable behaviour. They are a faithful specification exercise, not a customer's
  requirements document.
