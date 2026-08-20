# Authenticated application discovery

QualityProof discovery is an observation mechanism, not a proof of correctness. It uses a bounded
same-origin FIFO frontier and deterministic URL, route, record, and output ordering. Dynamic
numeric, UUID, and long hexadecimal path segments are normalized for route comparison.

Safety policy:

- HTTP(S) only; final redirects and links must remain on the start URL's exact origin.
- `--allowed-domain` can further restrict the host.
- page, depth, queued-action, and wall-clock limits are mandatory.
- CAPTCHA pages are recorded as unknown and are never bypassed.
- external and destructive links are recorded as blocked unknown frontier items.
- discovery follows safe anchors only and does not click buttons or submit forms.
- browser actions are fixed code paths; no LLM output controls runtime actions.

Authentication uses either Playwright `storage_state` or an explicit login page and selectors.
Username and password values are loaded only from environment-variable names supplied on the
command line. They are never accepted as CLI values, configuration values, evidence, or logs.
Selector login also requires an explicit mutation method and absolute submission path. The
authentication preflight allows same-origin GET, HEAD, and OPTIONS subresources plus only that
exact method/path pair; all other mutation requests are aborted before network I/O.

Each observed page stores its title, headings, links, forms, structured semantic controls
(role/name/action/exact selector), normalized route, semantic fingerprint, depth, and evidence
references. Screenshots, trace references, console errors, and
failed or HTTP 4xx/5xx responses are evidence. SQLite record kinds are `page_state`,
`action_edge`, `evidence`, and `unknown_item`; their public schemas are exported by `init`.
