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
| Secret Manager secrets | `anthropic-api-key`, `sentry-dsn`, `weheat-refresh-token` (PR6), `resideo-client-id` + `resideo-client-secret` + `resideo-refresh-token` (PR7, when wired) |

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

### HomeWizard P1 — local-API exposure (LAN-only)

HomeWizard publiceert **geen** publieke cloud-API; de officiële docs
([api-documentation.homewizard.com](https://api-documentation.homewizard.com/docs/introduction/))
beperken access tot "the same Wi-Fi network as the device". Voor Cloud Run
(HESM is cloud-only) betekent dit dat we het apparaat moeten exposen via
een tunnel vanaf een klein altijd-aan kastje thuis.

Dit is technisch een doorbreking van het "cloud-only"-principe (zie CLAUDE.md
anti-pattern "Don't add an edge device"). De afspraak: het kastje runt
ALLEEN een dom tunnel-proces (Cloudflare Tunnel of Tailscale), géén
optimizer-logica. Bij uitval valt de cycle terug naar **safe mode** (zie
``optimizer/dispositie_providers.py`` → ``build_surplus_snapshot`` met
30 s staleness-grens) en schakelt niets.

**Aanbevolen:** Cloudflare Tunnel + Cloudflare Access service-token. Na setup
sla je de URL + headers op als Secret Manager-secrets en mount je ze in
`cloudbuild.yaml`:

```yaml
- --set-env-vars=...,HOMEWIZARD_BASE_URL=https://hwz.huisjes.dev
- --set-secrets=...,HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_ID=hwz-cf-id:latest,HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_SECRET=hwz-cf-secret:latest
```

Alternatieven: Tailscale Funnel (`*.ts.net`), of een dedicated push-agent
die elke 15 s een measurement naar Firestore schrijft (zou een aparte PR
worden, niet onderdeel van de dispositie-PR-reeks).

**P1-splitter:** sinds Zonneplan dynamisch (08-07-2026) zit er een
P1-splitter tussen ZIV ESMR5 en de Zonneplan-meter; de HomeWizard hangt
parallel op die splitter. Praktisch geen impact op het uitlezen — beide
devices halen hun eigen telegram.

### EnergyZero — kale day-ahead-spot (publieke API, geen auth)

Sinds 08-07-2026 draait het contract op Zonneplan dynamisch, dat onder
de motorkap de EnergyZero-prijsfeed gebruikt. De dispositie-engine
spreekt EnergyZero direct aan voor de kale spot-prijs:

```
GET https://public.api.energyzero.nl/public/v1/prices
    ?energyType=ENERGY_TYPE_ELECTRICITY
    &date=DD-MM-YYYY
    &interval=INTERVAL_QUARTER
```

Geen auth-token, geen Secret Manager-entry. Optioneel kan `ENERGYZERO_BASE_URL`
geset worden in `cloudbuild.yaml` voor staging-mirroring; default blijft
de publieke endpoint. ENTSO-E (PR3) blijft beschikbaar als uur-fallback,
maar wordt niet meer door de dispositie-engine gebruikt — daar zit nu
EnergyZero met native kwartier-resolutie.

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

### WeHeat refresh token (PR6)

WeHeat uses OAuth2 `authorization_code + PKCE` against the Keycloak realm
`auth.weheat.nl`. We use the public Home Assistant OAuth client so no
business onboarding is required — anyone with a WeHeat account can run
the bootstrap script.

**Step 1 — capture the refresh token (one-time, on Roel's laptop):**

```bash
cd apps/optimizer
uv run scripts/weheat_bootstrap.py
```

A browser opens, you sign in to your WeHeat account, the script prints
the refresh token to stdout. The token does not expire under normal
use; rotate by re-running the script if it gets revoked.

**Step 2 — store it in Secret Manager + grant access:**

```bash
printf '%s' '<paste-refresh-token>' | gcloud secrets create weheat-refresh-token \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

gcloud secrets add-iam-policy-binding weheat-refresh-token \
  --member="serviceAccount:hesm-optimizer@hesm-huisjes.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=hesm-huisjes
```

**Step 3 — mount in `cloudbuild.yaml` under `--set-secrets`:**

```
WEHEAT_REFRESH_TOKEN=weheat-refresh-token:latest
```

The connector falls back to `MockWeHeatClient` when `WEHEAT_REFRESH_TOKEN`
is unset, so the cycle keeps running on staging without it.

### Resideo Lyric thermostat (PR7)

Resideo uses OAuth2 `authorization_code` against the Honeywell Home
developer portal (`developer.honeywellhome.com`). Unlike WeHeat there's
no public OAuth client we can re-use — Roel needs to register his own
app at the portal and obtain a Consumer Key + Consumer Secret.

**Step 1 — register an app at developer.honeywellhome.com:**

- Sign in with your Honeywell-account (same login as the Lyric app).
- *My Apps* → *Create a new app*.
- App Name: anything (e.g. `HESM Sittard`).
- Callback URL: **`http://localhost:8765/callback`** — required exact match.
- Save. The portal shows your `Consumer Key` (= client_id) and
  `Consumer Secret` (= client_secret).

**Step 2 — capture the refresh token (one-time, on Roel's laptop):**

```bash
cd apps/optimizer
uv run scripts/resideo_bootstrap.py \
  --client-id   '<CONSUMER_KEY>' \
  --client-secret '<CONSUMER_SECRET>'
```

A browser opens, you sign in to Honeywell and approve the app; the
script prints the refresh token to stdout. Honeywell rotates refresh
tokens on each access-token refresh, so the secret stays valid as long
as the cycle keeps running successfully (no manual rotation needed).

**Step 3 — store creds + grant access:**

```bash
printf '%s' '<CONSUMER_KEY>' | gcloud secrets create resideo-client-id \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

printf '%s' '<CONSUMER_SECRET>' | gcloud secrets create resideo-client-secret \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

printf '%s' '<REFRESH_TOKEN>' | gcloud secrets create resideo-refresh-token \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

for s in resideo-client-id resideo-client-secret resideo-refresh-token; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:hesm-optimizer@hesm-huisjes.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=hesm-huisjes
done
```

**Step 4 — extend `cloudbuild.yaml` `--set-secrets`:**

```
RESIDEO_CLIENT_ID=resideo-client-id:latest,RESIDEO_CLIENT_SECRET=resideo-client-secret:latest,RESIDEO_REFRESH_TOKEN=resideo-refresh-token:latest
```

The connector falls back to `MockResideoClient` whenever any of the
three vars is unset, so the cycle keeps running on staging without it.

**Caveat — refresh-token rotation:** Honeywell rotates refresh tokens on
every access-token refresh. The current connector keeps the rotated
token in-process but does **not** write it back to Secret Manager.
That's fine for hours of uptime; for a long-lived Cloud Run instance
the next refresh after a cold start uses the original (stable) token.
If you ever see `ResideoAuthError` in logs, just re-run the bootstrap.

### Zonneplan tokens (PR9-new)

Zonneplan is Roel's supplier from 7 jul 2026 (replaces the earlier
Tibber/Frank/EnergyZero plan). One cloud call gives P1 net power +
retail tariff + PV production — replaces the ENTSO-E retail-markup
formula for pricing and moots the HomeWizard tunnel for grid data.

**Step 1 — bootstrap (one-time, on Roel's laptop):**

```bash
cd apps/optimizer
uv run scripts/zonneplan_bootstrap.py --email 'roelhuis@gmail.com'
```

You get a magic-link email from Zonneplan; paste the one-time code
back into the terminal. The script prints an `access_token`,
`refresh_token`, and `device_uuid` (a fresh UUID it generated for you).

**Step 2 — store all three in Secret Manager + grant access:**

```bash
printf '%s' '<ACCESS_TOKEN>' | gcloud secrets create zonneplan-access-token \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

printf '%s' '<REFRESH_TOKEN>' | gcloud secrets create zonneplan-refresh-token \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

printf '%s' '<DEVICE_UUID>' | gcloud secrets create zonneplan-device-uuid \
  --data-file=- --replication-policy=automatic --project=hesm-huisjes

for s in zonneplan-access-token zonneplan-refresh-token zonneplan-device-uuid; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:hesm-optimizer@hesm-huisjes.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=hesm-huisjes
done
```

**Step 3 — extend `cloudbuild.yaml` `--set-secrets`:**

```
ZONNEPLAN_ACCESS_TOKEN=zonneplan-access-token:latest,ZONNEPLAN_REFRESH_TOKEN=zonneplan-refresh-token:latest,ZONNEPLAN_DEVICE_UUID=zonneplan-device-uuid:latest
```

Falls back to `MockZonneplanClient` when any of the three is unset;
the cycle keeps running on staging without it.

**Endpoint caveat:** the Zonneplan API is not officially public. The
paths mirror what the community Home-Assistant integration uses
successfully. If Zonneplan drifts, symptoms will surface as
`ZonneplanMalformed` in Cloud Logs on the first live run — the
connector proper never crashes the cycle thanks to `_safe_call`.

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
