# HESM infrastructure runbook

This document is the **operational manual** for the cloud side of the
project. It captures every one-shot setup step that lives outside the
code, plus the steady-state runbook (deploy, rollback, look at logs).

PR5 ships the first deployable system; this file is its companion.

---

## Project facts

| Thing | Value |
|---|---|
| GCP project ID | `hesm-huisjes` |
| GCP project number | `943607238094` |
| Region | `europe-west4` (Eemshaven, NL) |
| Cloud Run service | `hesm-optimizer` |
| Cloud Run URL | https://hesm-optimizer-4rsk5dywaa-ez.a.run.app |
| Runtime SA | `hesm-optimizer@hesm-huisjes.iam.gserviceaccount.com` |
| Scheduler SA | `hesm-scheduler@hesm-huisjes.iam.gserviceaccount.com` |
| Artifact Registry repo | `europe-west4-docker.pkg.dev/hesm-huisjes/hesm/` |
| Firestore | Native mode, `europe-west4` |
| Secret Manager secrets | `anthropic-api-key`, `sentry-dsn` |

---

## One-shot setup (already done in PR5)

The following GCP resources exist. Listed here so we can recreate the
environment in a different project (staging, App Store fork) without
losing context.

### APIs enabled

```bash
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  firestore.googleapis.com cloudscheduler.googleapis.com \
  fcm.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project=hesm-huisjes
```

### Firestore database

```bash
gcloud firestore databases create \
  --location=europe-west4 --type=firestore-native \
  --project=hesm-huisjes
```

### Artifact Registry repo

```bash
gcloud artifacts repositories create hesm \
  --repository-format=docker --location=europe-west4 \
  --description="HESM container images" \
  --project=hesm-huisjes
```

### Service accounts + roles

The runtime SA (`hesm-optimizer`) holds least-privilege project-level
roles. Read/write to Firestore, log + metric writers, Firebase admin
agent (for FCM), and read access to the two secrets.

```bash
gcloud iam service-accounts create hesm-optimizer \
  --display-name="HESM Optimizer Cloud Run service" \
  --project=hesm-huisjes

SA="hesm-optimizer@hesm-huisjes.iam.gserviceaccount.com"
for role in \
  roles/datastore.user \
  roles/logging.logWriter \
  roles/monitoring.metricWriter \
  roles/cloudtrace.agent \
  roles/firebase.sdkAdminServiceAgent ; do
  gcloud projects add-iam-policy-binding hesm-huisjes \
    --member="serviceAccount:$SA" --role="$role" --condition=None
done

for secret in anthropic-api-key sentry-dsn ; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor" \
    --project=hesm-huisjes
done
```

The Cloud Build SA needs three roles to deploy on our behalf:

```bash
PN=943607238094
CB_SA="${PN}@cloudbuild.gserviceaccount.com"
for role in roles/run.admin roles/iam.serviceAccountUser roles/artifactregistry.writer ; do
  gcloud projects add-iam-policy-binding hesm-huisjes \
    --member="serviceAccount:$CB_SA" --role="$role" --condition=None
done
```

The scheduler SA (`hesm-scheduler`) only needs `roles/run.invoker` on
the Cloud Run service itself, plus the email is allow-listed by the
service via `SCHEDULER_ALLOWED_EMAILS`:

```bash
gcloud iam service-accounts create hesm-scheduler \
  --display-name="HESM Cloud Scheduler invoker" \
  --project=hesm-huisjes

gcloud run services add-iam-policy-binding hesm-optimizer \
  --region=europe-west4 \
  --member="serviceAccount:hesm-scheduler@hesm-huisjes.iam.gserviceaccount.com" \
  --role="roles/run.invoker" --project=hesm-huisjes
```

### Cloud Scheduler jobs

```bash
URL="https://hesm-optimizer-4rsk5dywaa-ez.a.run.app"

gcloud scheduler jobs create http hesm-optimize \
  --location=europe-west4 \
  --schedule="*/15 * * * *" \
  --uri="$URL/optimize" \
  --http-method=POST \
  --oidc-service-account-email=hesm-scheduler@hesm-huisjes.iam.gserviceaccount.com \
  --oidc-token-audience="$URL" \
  --time-zone="Europe/Amsterdam" \
  --project=hesm-huisjes

gcloud scheduler jobs create http hesm-learning-check \
  --location=europe-west4 \
  --schedule="0 19 * * *" \
  --uri="$URL/jobs/learning-check" \
  --http-method=POST \
  --oidc-service-account-email=hesm-scheduler@hesm-huisjes.iam.gserviceaccount.com \
  --oidc-token-audience="$URL" \
  --time-zone="Europe/Amsterdam" \
  --project=hesm-huisjes
```

### Secrets (already populated)

* `anthropic-api-key` — Anthropic Claude API key (PR10 wires the actual chat).
* `sentry-dsn` — Sentry project DSN.

To rotate either secret:

```bash
printf '%s' '<NEW VALUE>' | gcloud secrets versions add SECRET_NAME --data-file=- --project=hesm-huisjes
gcloud run services update hesm-optimizer --region=europe-west4 --project=hesm-huisjes
```

### Firestore security rules

Stored in `/firestore.rules`. Deploy via Firebase CLI:

```bash
firebase deploy --only firestore:rules --project hesm-huisjes
```

---

## Deploy — steady-state

From the repo root:

```bash
cd apps/optimizer
gcloud builds submit --config=cloudbuild.yaml . --project=hesm-huisjes
```

This:
1. Builds the container (multi-stage Dockerfile).
2. Pushes to Artifact Registry with `${BUILD_ID}` and `latest` tags.
3. Deploys to Cloud Run with the runtime SA, secrets, and env vars.

A successful deploy logs the new revision URL. Curl `/health` to
confirm wiring:

```bash
URL=$(gcloud run services describe hesm-optimizer --region=europe-west4 \
  --format='value(status.url)' --project=hesm-huisjes)
curl -s "$URL/health" | jq
```

---

## Rollback

Cloud Run keeps every revision. Roll back without rebuilding:

```bash
# List revisions
gcloud run revisions list --service=hesm-optimizer \
  --region=europe-west4 --project=hesm-huisjes

# Route 100% traffic to a known-good revision
gcloud run services update-traffic hesm-optimizer \
  --to-revisions=hesm-optimizer-00003-abc=100 \
  --region=europe-west4 --project=hesm-huisjes
```

---

## Logs & Sentry

* **Cloud Run logs:** https://console.cloud.google.com/run/detail/europe-west4/hesm-optimizer/logs?project=hesm-huisjes
* **Cloud Build history:** https://console.cloud.google.com/cloud-build/builds?project=hesm-huisjes
* **Sentry issues:** https://sentry.io (the project tied to the DSN we stored)
* **Cloud Scheduler runs:** https://console.cloud.google.com/cloudscheduler?project=hesm-huisjes

Tail recent service logs from the CLI:

```bash
gcloud logging read 'resource.type=cloud_run_revision \
  AND resource.labels.service_name=hesm-optimizer' \
  --project=hesm-huisjes --limit=20 --freshness=10m
```

---

## Open decisions deferred to later PRs

### HomeWizard P1 — local-API exposure

HomeWizard has no public cloud API; the connector targets the local v1
API. To keep Cloud Run reachable to the meter, expose the local IP via
a tunnel from a small always-on device.

**Recommended:** Cloudflare Tunnel + Cloudflare Access service-token.
After setup, store the URL and headers as Secret Manager secrets and
mount them in `cloudbuild.yaml`:

```yaml
- --set-env-vars=...,HOMEWIZARD_BASE_URL=https://hwz.huisjes.dev
- --set-secrets=...,HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_ID=hwz-cf-id:latest,HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_SECRET=hwz-cf-secret:latest
```

Alternatives: Tailscale Funnel (`*.ts.net`), or swap to Tibber Pulse if
Roel switches suppliers — that gives a real cloud API and the tunnel
device disappears.

### ENTSO-E API token

ENTSO-E requires emailing transparency@entsoe.eu for an API key. After
arrival, store it:

```bash
printf '%s' 'YOUR_TOKEN' | gcloud secrets create entsoe-api-token \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

gcloud secrets add-iam-policy-binding entsoe-api-token \
  --member="serviceAccount:hesm-optimizer@hesm-huisjes.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=hesm-huisjes
```

Then add to `cloudbuild.yaml` under `--set-secrets`:
`ENTSOE_API_TOKEN=entsoe-api-token:latest`.

### CI/CD trigger from GitHub

Right now we run `gcloud builds submit` manually. PR-trigger is a small
follow-up:

```bash
gcloud builds triggers create github \
  --repo-name=hesm-huisjes --repo-owner=roelhuis-collab \
  --branch-pattern=^main$ \
  --build-config=apps/optimizer/cloudbuild.yaml \
  --included-files='apps/optimizer/**' \
  --project=hesm-huisjes
```

(Requires connecting the GitHub repo to Cloud Build first via the
console — one click.)
