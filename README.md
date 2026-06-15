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

## Dispositie-engine (kwartier-besparingsmodule)

Naast `optimizer/v0.py` draait per kwartier een **dispositie-engine** (`optimizer/dispositie.py`) die het verwachte PV-overschot toewijst aan de meest waardevolle bestemming. Vier bestemmingen, gerangschikt op marginale winst t.o.v. terugleveren (= baseline):

1. **Zelf verbruiken** — een verschuifbare last activeren (WeHeat-tapwater, buffer-overheat, EV-laden, witgoed).
2. **Opslaan** — accu laden (zodra die er staat).
3. **Terugleveren** — naar het net. Baseline, gain = €0.
4. **Curtailen** — export-limiting op de omvormer. Noodrem.

Sinds 08-07-2026 draait het huis op **Zonneplan dynamisch** (`config/site.config.ts` → `TARIFF_CONFIG`). De engine rekent per kwartier met de kale EPEX-spot (uit een `SpotPriceProvider`) plus de Zonneplan-componenten:

```
importPrice(t) = spot(t) + inkoopvergoeding + energiebelasting
exportValue(t) = spot(t) + terugleveropslag
                 + (overdag & (spot+opslag)>0 & cumYtd<7500 ? 10% × spot : 0)   // Zonnebonus
                 + (saldering.active ? energiebelasting : 0)                    // restitutie binnen saldeerbereik
```

Marginale winst t.o.v. terugleveren: `self_consume = importPrice − exportValue`, `store = importPrice·rte − exportValue`, `export = 0` (baseline), `curtail = −exportValue` (positief bij negatieve marktprijs).

Twee tariefregimes, datum-gestuurd via `regime_for()`:

* **Saldering** (t/m 2026): energiebelasting komt terug op je export binnen het saldeerbereik → `exportValue` is hoog, `self_consume`-winst is alleen de Zonneplan-inkoopvergoeding (~€0,025/kWh).
* **No saldering** (vanaf 01-01-2027): `exportValue` zonder energiebelasting-restitutie → `self_consume`-winst stijgt naar ~€0,16/kWh (energiebelasting + inkoopvergoeding) onafhankelijk van de spot. Curtail wint pas van export bij negatieve marktprijs; self_consume verslaat dat economisch zelden, behalve bij extreem negatieve spot.

**Zonnebonus** is een Zonneplan-bonus: +10% over de spot bovenop de gewone terugleververgoeding, alleen tussen 10:00 en 15:00, alleen wanneer `(spot + terugleveropslag) > 0`, en capped op 7.500 kWh teruglevering per kalenderjaar. De cum YTD-teruglevering komt uit het ZIV ESMR5 **teruglever-register** (P1 `total_export_kwh`), niet uit de netto-stand — bijgehouden in `dispositie/cum_teruglevering` met jaarwissel-reset.

Beslissingen worden per kwartier naar Firestore (`disposition_decisions/`) geschreven. Het PWA-scherm **`/dispositie`** toont vandaag's besparing, de actuele spot, de Zonnebonus-ruimte en de allocatie-tijdlijn live. Zolang `heat_pump.controllable=false` blijft (geen bevestigde WeHeat write-adapter) schrijft de engine adviezen en schakelt niet fysiek. `FlatDayNightSpotPriceProvider` is voorlopig de stub-spot-bron; echte EPEX-koppeling (per-kwartier) staat op de roadmap. `config/tariff.energiedirect.ts` blijft als historische referentie van de Energiedirect-staffel (contract afgelopen 07-07-2026) — niet meer in gebruik door de engine.

## Repo-layout

```
hesm-huisjes/
├── apps/
│   ├── optimizer/             # Python service voor Cloud Run
│   │   ├── src/
│   │   │   ├── main.py        # FastAPI entrypoint
│   │   │   ├── optimizer/
│   │   │   │   ├── v0.py        # rule-based decision engine
│   │   │   │   ├── dispositie.py# kwartier-dispositie-engine (saldering/no-saldering)
│   │   │   │   ├── dispositie_providers.py # forecast + load providers
│   │   │   │   ├── policy.py    # Layer 1 (limits) + Layer 2 (strategy)
│   │   │   │   └── learning.py  # Layer 3 (dormant tot week 6)
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
├── config/
│   ├── site.config.ts             # gezaghebbende site-waarden (Kempenstraat 3)
│   └── tariff.energiedirect.ts    # terugleverstaffel + tariefconstanten
├── types/
│   └── dispositie.ts              # TS-types voor de dispositie-engine
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
