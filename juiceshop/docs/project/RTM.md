# Requirements Traceability Matrix

**Generated.** Produced by `qualityproof coverage` against the registered
requirement baseline. Do not edit by hand: a hand-maintained matrix records what
someone believed, which is the problem this programme exists to remove.

**Traces to:** [SRS-JS-001 v1.1](SRS.md) · [TP-JS-001](TEST-PLAN.md)
**System under test:** OWASP Juice Shop v20.2.0, locally hosted
([authorisation](AUTHORISATION.md))

Regenerate with:

```console
qualityproof requirements import docs/project/requirements.yaml --project <p>
qualityproof audit juiceshop/tests --project <p>
qualityproof coverage --project <p> --require-priority P1 --fail-on-orphans
```

## Notes on the evidence

- `JS-ADMIN-2` and `JS-ADMIN-3` are **bounded observations, not proof of absence.**
  Every route answers HTTP 200 to every identity — the guard is entirely
  client-side — so the evidence is that administrative content is not rendered, on
  that route, for that identity. Automated exploration cannot prove no path exists.
- `JS-CAT-2` is an **open finding against the application**, recorded as a strict
  expected failure: the catalogue renders several level-one headings, so assistive
  technology cannot identify the page. Strict means fixing the application fails
  the marker rather than letting it rot into a false statement.
- `JS-BASKET-2` was **amended after the tool disproved it** — see the SRS revision
  history. The original text forbade reaching a basket anonymously; the application
  permits a guest basket. The requirement now states the property that matters.
- `JS-ADMIN-4` is evidenced on constructed snapshots, deliberately. The requirement
  constrains the *comparison*, not the application. A real two-release comparison
  was also run (default configuration against `quiet`): it reported ten routes
  changed across the `headings`, `controls`, `accessibility` and `layout` facets,
  with zero spurious additions or removals.
- `JS-EV-1` to `JS-EV-4` constrain the programme itself, and are evidenced by
  exercising those guarantees rather than by assertion in prose.

- Registered requirements: 21
- Verified by at least one test: 21
- Referenced but not established: 0
- **Uncovered (no test references them): 0**
- Orphan links (test cites an unregistered id): 0
- Untraced tests (assert something, name no requirement): 0

| Requirement | Priority | Area | Status | Verified | Partial | Unknown |
|---|---|---|---|---:|---:|---:|
| `JS-ADMIN-1` | P2 | Administrative boundary | VERIFIED | 1 | 0 | 0 |
| `JS-ADMIN-2` | P1 | Administrative boundary | VERIFIED | 1 | 0 | 0 |
| `JS-ADMIN-3` | P1 | Administrative boundary | VERIFIED | 1 | 0 | 0 |
| `JS-ADMIN-4` | P1 | Administrative boundary | VERIFIED | 1 | 0 | 0 |
| `JS-AUTH-1` | P1 | Authentication | VERIFIED | 1 | 0 | 0 |
| `JS-AUTH-2` | P2 | Authentication | VERIFIED | 1 | 0 | 0 |
| `JS-AUTH-3` | P2 | Authentication | VERIFIED | 1 | 0 | 0 |
| `JS-AUTH-4` | P1 | Authentication | VERIFIED | 1 | 0 | 0 |
| `JS-BASKET-1` | P1 | Basket and checkout | VERIFIED | 1 | 0 | 0 |
| `JS-BASKET-2` | P1 | Basket and checkout | VERIFIED | 1 | 0 | 0 |
| `JS-CAT-1` | P1 | Anonymous browsing | VERIFIED | 1 | 0 | 0 |
| `JS-CAT-2` | P3 | Anonymous browsing | VERIFIED | 1 | 0 | 0 |
| `JS-CAT-3` | P2 | Anonymous browsing | VERIFIED | 1 | 0 | 0 |
| `JS-CAT-4` | P3 | Anonymous browsing | VERIFIED | 1 | 0 | 0 |
| `JS-CHECKOUT-1` | P1 | Basket and checkout | VERIFIED | 1 | 0 | 0 |
| `JS-EV-1` | P1 | Evidence and process requirements | VERIFIED | 1 | 0 | 0 |
| `JS-EV-2` | P1 | Evidence and process requirements | VERIFIED | 1 | 0 | 0 |
| `JS-EV-3` | P1 | Evidence and process requirements | VERIFIED | 1 | 0 | 0 |
| `JS-EV-4` | P1 | Evidence and process requirements | VERIFIED | 1 | 0 | 0 |
| `JS-PROFILE-1` | P2 | Customer profile | VERIFIED | 1 | 0 | 0 |
| `JS-PROFILE-2` | P2 | Customer profile | VERIFIED | 1 | 0 | 0 |

## By priority

Percentages hide risk. A release gate should ask whether anything critical is unproven, not what the average looks like.

| Priority | Verified | Total |
|---|---:|---:|
| P1 | 13 | 13 |
| P2 | 6 | 6 |
| P3 | 2 | 2 |

A requirement is VERIFIED only when a test reaches VERIFIED against it. That means traceable and attributable, never correct.
