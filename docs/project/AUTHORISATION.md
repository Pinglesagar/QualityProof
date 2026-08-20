# Authorisation and scope of testing

## What is tested

A locally hosted instance of **OWASP Juice Shop**, run from source on the
engineer's own machine at `http://127.0.0.1:3000`.

## Why testing it is permitted

Juice Shop is published by the OWASP Foundation under the MIT licence
*specifically* to be attacked and tested. Its own documentation describes it as
intended for security training, awareness demos, CTFs and tool benchmarking. No
permission beyond its licence is required, and no third-party service is involved.

## What is explicitly not tested

- No live commercial site. Not Amazon, not Flipkart, not any hosted storefront.
- No shared or third-party-hosted Juice Shop instance, including the public demo.
- No production system belonging to anyone.

Pointing an automated crawler at a third party's site without written permission
is a terms-of-service violation and, depending on jurisdiction, may be unlawful.
The tool enforces this structurally rather than trusting the operator: `discover`
refuses any origin outside `--allowed-domain`, refuses cross-origin requests
before they leave the browser, and never activates a control.

## Standing safety constraints

| Constraint | Enforced by |
|---|---|
| Same-origin only, host allow-list required | `discovery.py::is_allowed_request` |
| Read-only methods, plus exactly one configured login endpoint | `discovery.py::is_allowed_request` |
| Destructive controls never activated | `discovery.py::is_destructive`, `scenarios.py::is_destructive_semantic` |
| CAPTCHA pages refused rather than solved | `discovery.py`, `captcha_refused` unknown |
| Credentials from environment only, redacted from evidence | `security.py::EvidenceRedactor` |
| Bounded pages, depth, actions and wall-clock | `discovery.py::DiscoveryOptions` |

## A note on the planted vulnerabilities

Juice Shop deliberately contains injection flaws, broken access control and
similar defects. This programme does **not** exploit them. It measures functional
and access-control *regression* between releases. Where the tool's differential
role crawl observes that a route is reachable by a lower-privileged identity, that
is reported as an observation for a human to judge — not as an exploit, and not as
a verdict.
