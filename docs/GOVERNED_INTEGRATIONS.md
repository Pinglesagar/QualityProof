# Governed integrations, healing, and release comparison

## Jira

`qualityproof jira sync finding.json --project-key DEMO` is a dry-run by default. The default
adapter is a local JSON mock under `.qualityproof/jira/issues.json`; add `--apply` to write it.
Findings use a stable fingerprint and SQLite issue mapping keyed by adapter, validated account/base,
project key, and fingerprint, so repeated applied syncs update the same issue without ever reusing
a mock, tenant, account, or project mapping in another scope. Descriptions are Atlassian Document
Format (ADF), and evidence is recursively redacted before it reaches any adapter.

For Jira Cloud REST v3, use:

```console
export QUALITYPROOF_JIRA_BEARER_TOKEN="<short-lived OAuth access token>"
qualityproof jira sync finding.json --project-key QP --adapter cloud \
  --base-url https://api.atlassian.com/ex/jira/YOUR-CLOUD-ID
# Review the dry-run, then repeat with --apply.
```

QualityProof does not request, collect, or store personal API tokens. Cloud credentials are read
only from `QUALITYPROOF_JIRA_BEARER_TOKEN` and retained only in process memory. Never put tokens,
OAuth codes, PKCE verifiers, or client secrets in `qualityproof.toml`, findings, shell history, or
source control.

For Atlassian OAuth 2.0 (3LO), register a public/native OAuth client and exact redirect URI, then
run `qualityproof jira auth-url --client-id ... --redirect-uri ...`. It emits a fresh random state,
PKCE verifier, and authorization URL. Keep the verifier ephemeral, verify the callback state
exactly, and exchange the returned code using the library helper
`qualityproof.jira.exchange_authorization_code`. The helper uses PKCE and returns the token response
in memory; the application is responsible for using a platform secret store outside QualityProof.
Do not log or persist that response. `qualityproof jira config` documents the safe configuration.

## Locator healing

`qualityproof heal propose evidence.json` consumes a failed locator plus semantic candidates. It
rejects every candidate whose precondition, user intent, or postcondition differs. Remaining
candidates are deterministically scored by accessible role, name, test id, and context, with a hard
maximum of ten inputs and three outputs by default. Paths must be safe project-relative POSIX paths;
locators must be parseable, single-line Python expressions. Each proposal contains confidence,
component scores, redacted input evidence, and a locator diff.

`qualityproof heal review PROPOSAL --decision approve --reason "..."` records a human event.
Approval parses the source, requires the recorded locator and full assertion line to match exactly,
and writes a bounded, valid unified `.patch` artifact under `.qualityproof/healing/patches`; it
never edits custom tests. Traversal, control characters, newline injection, stale context, and
syntax-breaking replacements are rejected. Rejection records no patch. QualityProof never
auto-applies changes, deletes or skips tests, removes assertions, or weakens an assertion.

## Evidence snapshots and release comparison

Capture named immutable snapshots:

```console
qualityproof snapshot create release-1
qualityproof snapshot create release-2 --application application.json
qualityproof diff release-1 release-2 --format markdown
```

Snapshots normalize requirements, routes, collision-safe page-state keys (`route#state-id`),
reviewed scenarios, current execution verdicts, unresolved unknowns, coverage counts, and optional
application metadata. Application metadata is compared explicitly. Existing names cannot be
overwritten. Diff output is deterministic JSON or Markdown under `.qualityproof/reports` unless
`--output` is provided.
