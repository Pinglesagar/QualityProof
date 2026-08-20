# Local containers and Azure deployment

The deployment is optional. QualityProof remains fully local: the control API reads local files,
and the complete workflow requires no cloud account. No workflow in this repository deploys on a
push; Azure deployment is manual and gated by repository variables.

## Local container demo

The production image uses the Playwright 1.62.0 Noble image required by `uv.lock`, installs the
locked Python environment, and runs as the image's non-root `pwuser`. The browser-job target also
installs locked tools required by generation (`pytest` and Ruff). CI launches Chromium inside the
image; `scripts.image_smoke` provides the same runtime contract check on hosts without Docker. See
`DEPENDENCY_PINNING.md` for digest verification and update policy.

```console
docker compose build
docker compose up -d
curl http://localhost:8000/healthz
curl http://localhost:8000/version
open http://localhost:8765/products
docker compose down
```

The API exposes only public `GET /healthz` and `GET /version` by default. Report/benchmark access
and run submission deliberately return 404 unless separately enabled and bearer protected. For a local,
operator-controlled queue, set all three values at runtime (never in an image or committed file):

```console
QUALITYPROOF_RUN_SUBMISSION_ENABLED=true
QUALITYPROOF_API_TOKEN=<long-random-bearer-token>
QUALITYPROOF_RUN_QUEUE_DIRECTORY=/data/queue
QUALITYPROOF_REPORT_ACCESS_ENABLED=true
QUALITYPROOF_REPORT_TOKEN=<separate-long-random-bearer-token>
```

Then `POST /runs` accepts only `requested_by` and `reason`; it cannot accept a shell command, target
URL, credentials, or browser instructions.

## Azure architecture

`infra/main.bicep` creates only consumption-oriented components:

- Basic Azure Container Registry with admin and anonymous access disabled.
- A Container Apps consumption environment and Log Analytics workspace capped at 1 GB/day.
- A scale-to-zero control Container App and a manual or queue-triggered Container Apps Job.
- Separate user-assigned managed identities for control and job workloads.
- Private Blob container for evidence and a private Storage Queue for optional run requests.
- Key Vault RBAC references for the API token and, in event mode, the KEDA queue scaler secret.

The control identity can pull its image, read only the evidence container, and—only when submission
is enabled—write the run queue and read the separate API and report secrets. The job identity can
pull its image and write only the evidence container; in event mode it can consume the run queue
and read only the scaler secret. Role assignments use child-resource scopes where Azure RBAC
supports them. The built-in Storage Queue Data Contributor role still permits more queue data
operations than each workload needs because Bicep cannot define a narrower built-in role; a custom
role is the remaining hardening option. ACR pull remains registry-scoped.

Storage defaults to Microsoft Entra authentication and shared-key access is disabled in manual
mode. Event mode explicitly enables it because this Container Apps Azure Queue scaler template uses
a connection secret; application code still uses managed identity. For stronger network isolation,
add private endpoints, private DNS, and a workload-profile environment after confirming the
associated fixed costs and GitHub runner network path.

The job always runs the repository's fixed controlled demo workflow. It does not execute request
content. Successful runs publish immutable, UUID-namespaced `runs/<run-id>/...` artifacts. Latest
report and benchmark aliases use ETag conditions (or create-if-absent), so concurrent writers
cannot silently overwrite one another.

## One-time Azure and GitHub setup

Install Azure CLI and sign in as an administrator, then choose unique values:

```console
az login
az account set --subscription "<subscription-id>"
az group create --name "<resource-group>" --location "uksouth"
az ad app create --display-name "qualityproof-github"
```

Record the app's `appId`, create a service principal, and grant deployment rights at the resource
group. `Contributor` creates resources; `User Access Administrator` is separately required because
the Bicep assigns narrowly scoped data-plane roles to managed identities.

```console
APP_ID="<app-id>"
RG_ID="$(az group show --name "<resource-group>" --query id -o tsv)"
az ad sp create --id "$APP_ID"
az role assignment create --assignee "$APP_ID" --role Contributor --scope "$RG_ID"
az role assignment create --assignee "$APP_ID" --role "User Access Administrator" --scope "$RG_ID"
```

Create a federated credential whose subject exactly matches the protected GitHub environment:

```console
cat > federated.json <<'JSON'
{
  "name": "qualityproof-azure-production",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:OWNER/REPOSITORY:environment:azure-production",
  "description": "QualityProof GitHub Actions OIDC",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
az ad app federated-credential create --id "$APP_ID" --parameters federated.json
rm federated.json
```

In GitHub, create and protect the `azure-production` environment, ideally with required reviewers.
Set these repository variables:

- `DEPLOY_AZURE=true` (the workflow job is skipped unless this explicit gate is present)
- `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_SUBSCRIPTION_ID`
- `AZURE_RESOURCE_GROUP` and a 3–12 character lowercase `AZURE_PREFIX`
- Optional `PUBLISH_PAGES=true` to publish the CI-generated static ledger on GitHub Pages

Add separate environment secrets `QUALITYPROOF_API_TOKEN` and `QUALITYPROOF_REPORT_TOKEN` only if
queued submissions and reports will be enabled. Generate at least 32 random bytes for each and
never reuse their values. GitHub receives no Azure client secret: `azure/login` exchanges
the short-lived OIDC token for Azure credentials. Run **Deploy Azure** manually. The workflow first
bootstraps infrastructure, pushes images tagged with the immutable Git commit SHA, and only then
creates or updates workloads. Leave `enable_run_submission=false` and `job_trigger=Manual` for the
lowest-risk default.

Start a manual job with:

```console
az containerapp job start --name "<prefix>-browser" --resource-group "<resource-group>"
```

## Jira OAuth and CAPTCHA policy

If Jira OAuth 2.0 (3LO) is configured, register the exact HTTPS callback owned by your deployment,
for example `https://qualityproof.example.com/integrations/jira/callback`. Do not use a wildcard,
the Container Apps default hostname for production, or a localhost callback outside development.
The current control service intentionally does not expose an OAuth callback; add and threat-model
that endpoint before registering it. Keep client secrets in Key Vault and validate OAuth state and
PKCE as described in `GOVERNED_INTEGRATIONS.md`.

The static `Type DEMO` CAPTCHA exists only in the first-party local fixture. Production discovery
must stop and report CAPTCHA presence. Never add test keys, bypass logic, solver services, or
session reuse to defeat a production CAPTCHA.

## Cost and cleanup

Typical low-use cost drivers are Basic ACR, Log Analytics ingestion/retention, Blob/Queue
transactions and storage, Container Apps executions, and network egress. Scale-to-zero minimizes
compute but does not make ACR or logs free. Prices vary by region; check the Azure Pricing
Calculator before deployment. Optional private endpoints, NAT, dedicated workload profiles,
Application Insights, and Front Door are intentionally omitted because they add fixed or material
cost.

To stop new runs, disable submission and set the job to manual. To remove everything, first retain
required evidence, then delete the resource group:

```console
az group delete --name "<resource-group>" --yes --no-wait
```

Key Vault purge protection intentionally prevents immediate permanent purge. Remove the GitHub
federated credential and Azure role assignments separately if the deployment identity is no longer
needed.
