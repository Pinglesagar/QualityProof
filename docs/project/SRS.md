# Software Requirements Specification

**Project:** Quality evidence programme for the OWASP Juice Shop storefront
**Document ID:** SRS-JS-001 · **Version:** 1.0 · **Traces to:** [BRS-JS-001](BRS.md)

> Requirements are numbered, atomic and testable. Each states an observable
> behaviour, not an implementation. The machine-readable form in
> [`requirements.yaml`](requirements.yaml) is generated from this document and is
> what the tooling registers; if the two disagree, this document wins and the YAML
> is regenerated.

## Conventions

- **Shall** denotes a mandatory requirement.
- Each requirement has a stable ID (`JS-<area>-<n>`) that never changes or is reused.
- **Priority:** P1 revenue or security critical · P2 core journey · P3 supporting.
- **Verification:** how the requirement is intended to be evidenced.

---

## 1. Anonymous browsing

### JS-CAT-1 — Catalogue is reachable without authentication
The storefront **shall** present the product catalogue to an unauthenticated
visitor. *Priority: P1. Verification: automated UI assertion on the catalogue landing state.*

### JS-CAT-2 — Catalogue presents a primary heading
The catalogue page **shall** expose exactly one level-one heading naming the page,
so assistive technology can announce it. *Priority: P3. Verification: automated accessibility assertion.*

### JS-CAT-3 — Products are individually addressable
Each catalogue entry **shall** be reachable at its own stable route.
*Priority: P2. Verification: discovery route inventory.*

### JS-CAT-4 — Catalogue is usable at a narrow viewport
The catalogue **shall** render without horizontal overflow at a viewport width of
375 CSS pixels. *Priority: P3. Verification: automated layout measurement at three viewports.*

---

## 2. Authentication

### JS-AUTH-1 — A registered customer can authenticate
A visitor holding valid credentials **shall** obtain an authenticated session.
*Priority: P1. Verification: authenticated discovery establishes a session.*

### JS-AUTH-2 — The login form controls are programmatically labelled
Email and password inputs **shall** each have an associated accessible name.
*Priority: P2. Verification: automated accessibility assertion.*

### JS-AUTH-3 — An authenticated session exposes a sign-out control
An authenticated user **shall** be offered a means of ending the session.
*Priority: P2. Verification: automated UI assertion. The control is never activated by automation.*

### JS-AUTH-4 — Invalid credentials do not grant a session
Authentication with incorrect credentials **shall not** produce an authenticated
session. *Priority: P1. Verification: negative authentication check.*

---

## 3. Basket and checkout

### JS-BASKET-1 — An authenticated customer has a basket
An authenticated customer **shall** be able to reach their basket.
*Priority: P1. Verification: authenticated route reachability.*

### JS-BASKET-2 — The basket is not reachable anonymously
An unauthenticated visitor **shall not** reach a populated basket.
*Priority: P1. Verification: differential role reachability.*

### JS-CHECKOUT-1 — Checkout is reachable from the basket
An authenticated customer with a basket **shall** be able to initiate checkout.
*Priority: P1. Verification: authenticated route reachability. Order placement is never automated.*

---

## 4. Customer profile

### JS-PROFILE-1 — A customer can reach their own profile
An authenticated customer **shall** be able to reach their profile page.
*Priority: P2. Verification: authenticated route reachability.*

### JS-PROFILE-2 — Profile form controls are programmatically labelled
Every editable profile input **shall** have an associated accessible name.
*Priority: P2. Verification: automated accessibility assertion.*

---

## 5. Administrative boundary

### JS-ADMIN-1 — The administrative surface exists for administrators
An authenticated administrator **shall** be able to reach the administration page.
*Priority: P2. Verification: role-scoped discovery as the administrator identity.*

### JS-ADMIN-2 — Ordinary customers are denied the administrative surface
An authenticated non-administrative customer **shall not** receive administrative
content. *Priority: P1. Verification: differential role reachability; the HTTP status observed for the route must differ between roles.*

### JS-ADMIN-3 — Anonymous visitors are denied the administrative surface
An unauthenticated visitor **shall not** receive administrative content.
*Priority: P1. Verification: differential role reachability.*

### JS-ADMIN-4 — A change in role reachability is detectable between releases
Where a route's reachability for a given role changes between two releases, the
comparison **shall** report it. *Priority: P1. Verification: snapshot diff on the status facet.*

---

## 6. Evidence and process requirements

These constrain how the programme itself operates. They exist because BR-6 asks
for auditable evidence, and evidence about evidence is otherwise unfalsifiable.

### JS-EV-1 — Automated exploration shall not change application state
Discovery **shall** issue only read-only requests, save for one explicitly
configured authentication endpoint, and **shall not** activate controls.
*Priority: P1. Verification: crawl firewall unit and browser tests.*

### JS-EV-2 — Credentials shall not appear in retained evidence
No credential value supplied by environment variable **shall** appear in any
retained artefact. *Priority: P1. Verification: redaction tests; secret-scanning check.*

### JS-EV-3 — A requirement is verified only against a registered source
A test **shall** be credited against a requirement only where the requirement is
registered and the citation resolves in that requirement's own recorded source.
*Priority: P1. Verification: provenance-gate tests.*

### JS-EV-4 — Unproven requirements shall be reported on every run
Each pipeline run **shall** report requirements that no test references, and
requirements referenced only by tests that establish nothing.
*Priority: P1. Verification: coverage report and CI gate.*

---

## Traceability

| Business objective | Satisfied by |
|---|---|
| BR-1 Know what tests prove | JS-EV-3, JS-EV-4 |
| BR-2 No undetected privilege regression | JS-ADMIN-2, JS-ADMIN-3, JS-ADMIN-4, JS-BASKET-2 |
| BR-3 Protect checkout | JS-BASKET-1, JS-CHECKOUT-1 |
| BR-4 Accessibility | JS-CAT-2, JS-AUTH-2, JS-PROFILE-2 |
| BR-5 Faster release decisions | JS-EV-4 |
| BR-6 Auditable evidence | JS-EV-1, JS-EV-2, JS-EV-3 |

Generated per-requirement coverage lives in [`RTM.md`](RTM.md) and is produced by
`qualityproof coverage`. It is not maintained by hand: a hand-maintained matrix
records what someone believed, which is precisely the problem this programme exists
to remove.

## Known limitations of this baseline

- Requirements describe behaviour observable through the UI and HTTP surface. They
  do not constrain internal design.
- `JS-AUTH-4` and `JS-BASKET-2` are negative requirements. Automated discovery can
  observe a denial but cannot prove that *no* path exists; these are evidenced as
  observations at a stated confidence, never as proof.
- The deliberately planted vulnerabilities in the system under test are out of
  scope by [BRS §5](BRS.md).
