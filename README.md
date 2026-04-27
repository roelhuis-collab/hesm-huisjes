# HESM by Huisjes

Home Energy System Management voor Roel's huis in Sittard.
Stuurt warmtepomp, boiler, dompelaar en (later) batterij op basis van EPEX-prijzen, weersvoorspelling en PV-productie. Volledig cloud-gebaseerd, AI-ondersteund, vendor-neutraal.

## Architectuur in één plaatje

```
                          ┌─────────────────────────┐
                          │  ENTSO-E (EPEX prijzen) │
                          │  Open-Meteo / Solcast   │
                          └──────────┬──────────────┘
                                     │
┌──────────────────┐          ┌──────▼─────────────────┐         ┌──────────────────┐
│ Device clouds    │ ──poll── │  Cloud Run optimizer   │ ──────► │ Firebase /       │
│  · WeHeat        │          │  (Python, FastAPI)     │         │ Firestore        │
│  · Resideo Lyric │ ◄── act ─│  every 15 min          │ ◄────── │ (state, history) │
│  · Shelly Cloud  │          │  + on-demand chat      │         └────────┬─────────┘
│  · Growatt       │          │  + week-6 watcher      │                  │
│  · HomeWizard P1 │          └──────┬─────────────────┘         realtime │
└──────────────────┘                 │                                    │
                                     │ FCM push                  ┌────────▼─────────┐
                                     ▼                           │ Vite + React PWA │
                              ┌─────────────┐    state read      │ (Netlify)        │
                              │ User iPad   │ ◄──────────────────│ Simple + Advanced│
                              │ / iPhone    │                    │ Settings + AI    │
                              └─────────────┘                    └──────────────────┘
```

## Drie lagen van controle

| Laag | Wat | Wie controleert | Activatie |
|---|---|---|---|
| 1. Grenzen | Harde limieten (max temp, comfortband, legionella-floor) | Gebruiker via UI | Direct |
| 2. Strategie | Welke doelfunctie (besparing/comfort/zelfverbruik mix) | Gebruiker kiest profiel | Direct |
| 3. Leren | Patroon-extractie uit historie (vertrektijden, douche-routine, …) | AI, gebruiker accepteert | Na 6 weken data, via push |

Layer 1 en 2 leven vanaf dag 1. Layer 3 zit ingebouwd maar slaapt — `learning_check.py` draait dagelijks, ziet wanneer er 42 dagen aan data is, stuurt een push: *"Ik heb genoeg gezien om patronen te herkennen — wil je de leerlaag activeren?"*. Tot die tijd valt-ie nooit in de optimizer-loop.

## Repo-layout

```
hesm-huisjes/
├── apps/
│   ├── optimizer/             # Python service voor Cloud Run
│   │   ├── src/
│   │   │   ├── main.py        # FastAPI entrypoint
│   │   │   ├── optimizer/
│   │   │   │   ├── v0.py      # rule-based decision engine
│   │   │   │   ├── policy.py  # Layer 1 (limits) + Layer 2 (strategy)
│   │   │   │   └── learning.py# Layer 3 (dormant tot week 6)
│   │   │   ├── connectors/    # device/data adapters
│   │   │   ├── ai/            # Claude rationale + chat backend
│   │   │   ├── safety/        # failsafe + watchdog
│   │   │   ├── state/         # Firestore models
│   │   │   ├── notifications/ # FCM push
│   │   │   └── jobs/          # cron-triggered jobs
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   └── dashboard/             # Vite + React + Tailwind PWA
│       ├── src/
│       │   ├── pages/
│       │   │   ├── Simple.tsx     # default iPad-vriendelijk
│       │   │   └── Advanced.tsx   # de showpiece-dashboard
│       │   ├── components/
│       │   ├── lib/firebase.ts
│       │   └── hooks/
│       ├── public/manifest.json   # PWA-config
│       └── vite.config.ts
└── infra/
    ├── firebase.json
    ├── firestore.rules
    └── cloudbuild.yaml
```

## Status

| Component | Status |
|---|---|
| `optimizer/v0.py` | ✓ werkend (rule-based, getest) |
| `optimizer/policy.py` | ✓ Layer 1+2 |
| `optimizer/learning.py` | ✓ scaffold, dormant tot push-activatie |
| `jobs/learning_check.py` | ✓ daily cron, detecteert 6w-mark |
| `notifications/push.py` | ✓ FCM trigger |
| `main.py` | ✓ FastAPI Cloud Run entrypoint |
| `dashboard/Simple.tsx` | ✓ iPad-default view |
| `dashboard/Advanced.tsx` | – uit preview converteren naar productie |
| Connectors | – mock-data nu; vullen zodra hardware staat |

## Setup (development)

```bash
# clone
git clone https://github.com/roelhuisjes/hesm-huisjes.git
cd hesm-huisjes

# optimizer (Python)
cd apps/optimizer
uv sync
uv run uvicorn src.main:app --reload

# dashboard (Node)
cd ../dashboard
pnpm install
pnpm dev
```

Vereist Firebase project (auth + Firestore + FCM aangezet) en environment variables in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
FIREBASE_PROJECT_ID=hesm-huisjes
ENTSOE_API_KEY=...           # gratis aan te vragen via mail
WEHEAT_CLIENT_ID=...
WEHEAT_CLIENT_SECRET=...
RESIDEO_CLIENT_ID=...
RESIDEO_CLIENT_SECRET=...
SHELLY_AUTH_KEY=...
HOMEWIZARD_API_TOKEN=...
GROWATT_USERNAME=...
GROWATT_PASSWORD=...
```

## Deploy

```bash
# Optimizer naar Cloud Run
gcloud run deploy hesm-optimizer \
  --source apps/optimizer \
  --region europe-west4 \
  --memory 512Mi

# Cloud Scheduler trigger (15 min)
gcloud scheduler jobs create http hesm-optimize-quarter \
  --schedule="*/15 * * * *" \
  --uri=https://hesm-optimizer-xxx.run.app/optimize \
  --http-method=POST

# Dagelijkse leerlaag-check (week-6 detector)
gcloud scheduler jobs create http hesm-learning-check \
  --schedule="0 19 * * *" \
  --uri=https://hesm-optimizer-xxx.run.app/jobs/learning-check \
  --http-method=POST

# Dashboard naar Netlify
cd apps/dashboard && netlify deploy --prod
```

## Filosofie

- **Transparantie**: elke optimizer-beslissing heeft een rationale die in de UI staat. Geen black box.
- **Overrulebaar**: gebruiker kan elke beslissing terugdraaien, AI accepteert en leert van het signaal.
- **Veilig**: bij elke API-uitval valt het systeem terug op fabrieksdefaults van de devices. Het huis wordt nooit te koud of te warm door onze software.
- **Vendor-neutraal**: je kan elke device vervangen zonder de code te slopen.

## Licentie

MIT (publiek). Bijdragen welkom als je dit ook voor je eigen huis wilt gebruiken.
