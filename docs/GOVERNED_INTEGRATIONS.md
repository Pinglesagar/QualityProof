# Governed integrations, healing, and release comparison

Findings synchronize to **Jira** or **Azure Boards**. Both are dry-run by default,
both derive the same fingerprint from the same finding, and both share one
implementation of identity and idempotency in `trackers.py`. Only two things are
tracker-specific, because only two things genuinely differ: how the payload is
spelled, and how it is transmitted. A finding is evidence about the system under
test, so which tracker records it is a deployment detail.

The renderer and the transport are parameterised on the payload type, so handing a
Jira field object to the Azure Boards transport is a type error rather than a
rejected write against somebody's real board.

## Jira

`qualityproof jira sync finding.json --project-key DEMO` is a dry-run by default. The default
adapter is a local JSON mock under `.qualityproof/jira/issues.json`; add `--apply` to write it.
Findings use a stable fingerprint and SQLite issue mapping keyed by adapter, validated account/base,
project key, and fingerprint, so repeated applied syncs update the same issue without ever reusing
a mock, tenant, account, or project mapping in another scope. Descriptions are Atlassian Document
Format (ADF), and evidence is recursively redacted before it reaches any adapter.

`--issue-type` selects the type to create, defaulting to `Bug`. Issue types are per-project, so
confirm yours exists under *Project settings -> Issue types*; a type the project does not define is
rejected by Jira as an opaque validation error at write time.

For Jira Cloud REST v3 there are two supported credentials. An Atlassian API token, which is the
short path for a single user:

```console
export QUALITYPROOF_JIRA_EMAIL="you@example.com"
export QUALITYPROOF_JIRA_API_TOKEN="<token from id.atlassian.com/manage-profile/security/api-tokens>"
qualityproof jira sync finding.json --project-key QP --adapter cloud \
  --base-url https://your-site.atlassian.net
# Review the dry-run, then repeat with --apply.
```

Or a short-lived OAuth access token, which is what a shared or automated deployment should use:

```console
export QUALITYPROOF_JIRA_BEARER_TOKEN="<short-lived OAuth access token>"
qualityproof jira sync finding.json --project-key QP --adapter cloud \
  --base-url https://api.atlassian.com/ex/jira/YOUR-CLOUD-ID
```

The API token is sent as HTTP Basic `email:token`, which is what Atlassian's REST API expects; the
OAuth token is sent as `Bearer`. When both are present the API token wins. A half-configured Basic
credential -- a token with no email, or an email with no token -- is refused rather than silently
falling back, because the fallback would send a request that fails for a reason the error does not
name.

QualityProof does not request, collect, or store credentials. They are read only from those
environment variables and retained only in process memory. Never put tokens, OAuth codes, PKCE
verifiers, or client secrets in `qualityproof.toml`, findings, shell history, or source control. A
token that has been pasted anywhere shared should be revoked and reissued rather than reused.

For Atlassian OAuth 2.0 (3LO), register a public/native OAuth client and exact redirect URI, then
run `qualityproof jira auth-url --client-id ... --redirect-uri ...`. It emits a fresh random state,
PKCE verifier, and authorization URL. Keep the verifier ephemeral, verify the callback state
exactly, and exchange the returned code using the library helper
`qualityproof.jira.exchange_authorization_code`. The helper uses PKCE and returns the token response
in memory; the application is responsible for using a platform secret store outside QualityProof.
Do not log or persist that response. `qualityproof jira config` documents the safe configuration.

## Azure Boards

`qualityproof boards sync finding.json --ado-project "Quality Proof"` is a dry-run by
default. The default adapter is a local JSON mock under
`.qualityproof/boards/work-items.json`; add `--apply` to write it.

```console
export QUALITYPROOF_AZURE_DEVOPS_PAT="<personal access token, Work Items (Read & write)>"
qualityproof boards sync finding.json --ado-project "Quality Proof" --adapter azure \
  --organization-url https://dev.azure.com/YOUR-ORG --work-item-type Bug
# Review the dry-run, then repeat with --apply.
```

Three differences from Jira are worth knowing before the first write, because each
one fails with an error that does not explain itself:

- Work items are written as a **JSON Patch array** with content type
  `application/json-patch+json`. A field object is rejected. Creating uses
  `POST /_apis/wit/workitems/$<Type>`, so the type is part of the path.
- A personal access token authenticates over HTTP Basic with an **empty username**,
  so the credential is `:<token>`. Sending it as a bearer, or as the username,
  fails authentication without saying why. A token containing a colon is refused
  outright, because `user:token` pasted whole authenticates as neither half.
- `--work-item-type` comes from the project's **process template**. `Bug` exists
  under Agile and Scrum; Basic offers `Issue` instead. Check Project settings →
  Process.

Severity is written into the description rather than into
`Microsoft.VSTS.Common.Severity`. That field exists on Bug under Agile and CMMI and
does not exist on Issue under Basic, so setting it unconditionally would turn a
correct configuration into a rejected write depending on the process template.

Descriptions are HTML, so evidence is escaped before it is embedded. Without that,
a finding's own text could close the tag and inject markup into a work item, and
evidence that can rewrite its own container is not evidence.

The finding fingerprint is written to `System.Tags` as `qp-<first 12 hex>`, which is
queryable in Azure Boards. A repeated sync updates the same work item instead of
filing a duplicate, and a human can get from the evidence to the record and back.

The tracker is part of the stored mapping identity. Without it, syncing one finding
through the mock for Jira and again for Azure Boards would compute the same mapping
key for two genuinely different records.

QualityProof does not request, collect or store credentials. The token is read only
from `QUALITYPROOF_AZURE_DEVOPS_PAT` and retained only in process memory, and it is
only ever sent to a validated `dev.azure.com/<organization>` or
`<organization>.visualstudio.com` host. Never place it in `qualityproof.toml`, a
finding, shell history or source control.

**Azure Boards requires no payment method** for small teams, unlike deploying Azure
cloud resources. `qualityproof boards config` prints the safe configuration.

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
