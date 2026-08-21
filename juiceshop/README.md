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

Two of the 21 tests act as an authenticated customer and administrator, so the
suite has a documented entry criterion (test plan **E-2**): role credentials must be
present in the environment. Juice Shop ships documented demo accounts; export them
and save the sessions once:

```bash
export JS_CUSTOMER_USER=... JS_CUSTOMER_PASS=...
export JS_ADMIN_USER=... JS_ADMIN_PASS=...
python -m scripts.juiceshop_auth
```

Credentials are read from the environment only and never written into this
repository. The saved sessions live outside the repo tree, under
`~/.qualityproof-auth/juiceshop/`.

A saved session goes stale when the application is restarted: Juice Shop still
answers `200` for the old token but resolves it to an empty user, so the browser
behaves as an anonymous visitor. The suite refuses to run in that state and tells
you to refresh, rather than failing with `['Your Basket (anonymous)']` as though the
application were broken. Blaming the system under test for a harness precondition
is the worst failure mode a suite has.

Then, with Juice Shop on `http://localhost:3000`:

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
| Traceable to a verified test | 21/21 |
| Demonstrated by a passing test | 20/21 — P1 13/13, P2 6/6, P3 1/2 |
| Execution against the live app | 20 passed, 1 strict xfail |
| Open findings | 1 |

`JS-CAT-2` is the difference between those two coverage rows: perfectly traceable, and
contradicted by evidence. A coverage report that collapses the two says 21/21 and is
wrong about the thing that matters.

Every one of the 21 tests carries a `REQUIREMENT` provenance record naming a requirement
in `requirements.yaml`, and the auditor re-reads that file and checks the recorded digest
before crediting the claim — so a requirement cannot be edited out from under a test that
claims to prove it.

## The one open finding

`JS-CAT-2` requires the catalogue to expose exactly one level-one heading. It exposes
**none**: the highest-ranked heading in the document is an `h2` carrying the site brand in
the toolbar, so no heading names the page. Recorded in
[`docs/project/findings/JS-CAT-2-no-h1.json`](docs/project/findings/JS-CAT-2-no-h1.json).

The first revision of that finding said the page rendered *three* level-one headings, and
that was wrong — which is worth keeping visible, because the tool caused the error. The
accessibility facet checked for a heading with `document.querySelector('h1')`, and on a
first visit Juice Shop opens a welcome banner: a modal dialog containing two `h1` elements.
So the check found a heading, reported no defect, and the headings it recorded as the
page's belonged to a dialog rendered above it. A structural rule defeated by an unrelated
overlay produces a false negative on exactly the pages most likely to be wrong. The
detector now scopes document-outline checks to page content and records `modal_dialog_open`
as context. Fixing it turned a wrong finding into a correct and more serious one.

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
