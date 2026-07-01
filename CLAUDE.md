# HESM by Huisjes — Claude Code Briefing

> **This document is the persistent project context. Read it fully at the start of every session. Update it when material decisions change.**

## TL;DR

Build a Home Energy System Management (HEMS) platform for Roel Huisjes' house in Sittard, Netherlands. It optimizes a heat pump, thermal storage, immersion heater, PV system, and (later) battery against EPEX day-ahead prices, weather forecasts, and learned household patterns. Cloud-only architecture (Google Cloud Run + Firebase + Netlify). Vendor-neutral. AI-driven (Claude). Vendor-lockin-free. Eventually publishable as PWA installable from Safari.

The repo already has a scaffold (see "What's already built"). Your job is to extend it through a defined PR sequence (see "Roadmap"), one reviewable chunk at a time, until the system runs on Roel's actual hardware.

## The person you're building for

**Roel Huisjes** — CEO of Kriya Materials (specialty nanotechnology, Nuth NL). Native Dutch, fluent English/German. Technical background, comfortable reading code, not a daily coder. Already builds Draftly (cycling-club PWA on Firebase + Netlify + Vite + React) using Claude Code, so he's familiar with the workflow and stack. Lives in Sittard area, active cyclist (R+D TEAM Watersley Offroad), CEO mindset.

**How he wants to be communicated with:**
- Direct, no fluff, no over-apologizing
- Push back when his asks conflict with principles — explain why
- Don't ask permission for low-stakes decisions; make them and note them
- Ask for confirmation on architectural pivots, regulatory implications, or anything that would cause rework
- Dutch in user-facing UI strings, English in code/comments
- Honest assessments over enthusiasm

## Goal & scope

**Primary:** running, robust HESM for Roel's house. Manageable from iPad/iPhone PWA. Saves him €700-1,500/year post-saldering (1 Jan 2027).

**Secondary, eventual:** if it works well, considered for App Store distribution. Build with that optionality in mind (clean code, good docs, MIT-licensed, no hardcoded credentials), but don't over-engineer for hypothetical commercial use. **Personal use is the priority.**

**Out of scope:**
- BRP/leveringsvergunning routes (regulatory complexity, no value-add for residential)
- Direct EPEX/wholesale market participation (use a dynamic-tariff supplier as the meta-layer; Roel's switching to **Zonneplan** from 7 Jul 2026 — supersedes the earlier Tibber/Frank/EnergyZero plan)
- Edge hardware (Roel explicitly rejected a mini-PC; cloud-only)

## Hardware setup (being installed Q2 2026)

| Component | Model / spec | Integration path |
|---|---|---|
| Heat pump | WeHeat Blackbird P80 (8 kW thermal, R290) | WeHeat cloud API (OAuth, **read-only telemetry**) |
| Indoor unit | WeHeat Compact All-Electric | via WeHeat (read-only) |
| DHW boiler | Inventum MAXtank 500L RVS | passive — temperature read via WeHeat |
| Buffer tank | WeHeat 100L Duplex RVS 2205 | passive |
| Immersion heater | 3 kW (in boiler tank) | Shelly Pro 2PM contactor (**only DHW lever we control**) |
| Thermostat | Honeywell Lyric T6 wired (Y6H810WF1005) | Resideo Total Connect Comfort API |
| PV inverter | Growatt MOD 9000TL3-X (9 kW 3-phase) | Cloud poll + future local Modbus |
| Solar | 26 panels, ~11.000 kWh/year | via Growatt |
| Smart meter | ZIV ESMR5 (Enexis 2022) | HomeWizard Wi-Fi P1 Meter (~€80, ordered separately) |
| Battery | **Future** — 10 kWh AC-coupled (e.g. Sigenergy / Marstek) | Add as 5th lever post-launch |

**Heating circuit constraints (CRITICAL — never violate):**
- Floor: max 50 °C (parquet)
- Bathroom: max 55 °C (no parquet there)
- Jaga LTV radiators: 45–50 °C is comfortable
- Boiler 500L: legionella floor 45 °C, hard ceiling 65 °C

## Principles (non-negotiable)

1. **Layer 1 is sacred.** The hard limits in `policy.py:SystemLimits` are never violated. Not by the optimizer, not by the AI, not by the user via UI (validation rejects). Parquet doesn't care about your clever heuristic.
2. **Failsafe by default.** If the optimizer service is down, devices fall back to their factory defaults. Roel's house must never become uncomfortable because of our software. Aggressive watchdog with FCM alert if anything stops responding.
3. **Transparent decisions, always.** Every optimizer action has a `rationale` string. The dashboard shows it. The AI chat can explain it on request. No black-box "trust me".
4. **User overrules everything.** Manual override is one tap away on the iPad. AI suggestions must always be skippable. Layer 1 limits are the *only* thing the user can't override (those are physical safety).
5. **Don't fight the device.** Honeywell, Shelly, Growatt have their own internal logic. Our role is to send setpoints and on/off commands within their normal API surface. We never spoof state, never bypass their safety. If they reject a command, we accept it and log. **WeHeat is read-only** — its public third_party API offers no write endpoints, so the heat pump runs on its own schedule and we observe.
6. **Cloud-only, no edge.** Roel rejected the edge mini-PC option. All control happens via device clouds. We accept the latency (200-500ms is fine for 15-min cycles) and the dependency on third-party APIs.
7. **AI is explainable and incremental.** Layer 3 (learning) is dormant for the first 42 days. After that, the user opts in via push notification. Even after activation, suggestions are soft inputs, never overrides.
8. **Default = simplicity, depth on demand.** The Simple page is what Roel sees daily on the iPad. The Advanced dashboard is for when he wants to dig in. Don't blur this.
9. **Public repo, MIT license.** Code quality reflects on Roel. No hardcoded secrets. No "TODO: clean up" left in main. Tests for anything that touches Layer 1 limits or money.
10. **Ship in reviewable PRs.** 50–250 lines of changes per PR. Each PR has a clear scope, passes its own tests, and doesn't break what came before.

## Architecture

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

**Runtime topology:**
- Cloud Run service (Python 3.12, FastAPI) — `apps/optimizer`
  - `/optimize` — Cloud Scheduler triggers every 15 min
  - `/jobs/learning-check` — Cloud Scheduler triggers daily at 19:00
  - `/chat`, `/policy`, `/override`, `/learning/respond` — frontend-triggered
- Firestore — single source of truth for state, history, policy, decisions, learned profile
- Firebase Auth — Roel + partner login
- Firebase Cloud Messaging — push to iPad/iPhone PWA
- Netlify — Vite PWA hosted on `hesm-huisjes.netlify.app` (subdomain for now, custom domain later)

**Auth & secrets:**
- **Workload Identity Federation** for everything Cloud Run calls. No JSON service account keys. No static tokens. WIF binding from Cloud Run service account → Firebase, Cloud Scheduler, FCM, Secret Manager.
- Third-party API keys (WeHeat, Resideo, Shelly, ENTSO-E, Anthropic) live in Secret Manager, accessed via WIF.
- Cloud Scheduler → Cloud Run uses OIDC token auth (no `CLOUD_SCHEDULER_TOKEN` shared secret; that placeholder in `main.py` should be replaced with `verify_oidc_token()`).
- Firebase Auth tokens for user-initiated requests (chat, policy updates, override).

## The three-layer policy model

| Layer | What | Who controls | When activated |
|---|---|---|---|
| **1. Limits** | Hard physical/safety limits (max flow temp, comfort bands, legionella floor) | User via UI, validated server-side | Day 0 |
| **2. Strategy** | Objective function weights (cost / comfort / self-consumption / renewable share) | User picks preset OR custom slider | Day 0 |
| **3. Learning** | Patterns extracted from history (wake/leave/return times, thermal mass, forecast bias) | AI extracts, user accepts via push | Day 42+ after explicit opt-in |

See `apps/optimizer/src/optimizer/policy.py` for Layer 1+2 (already implemented).
See `apps/optimizer/src/optimizer/learning.py` for Layer 3 (scaffolded, dormant until activated).

**Key insight:** Layer 3 is fully built but its `train()` and `suggest()` return empty results when `is_active = False`. The optimizer treats empty results as "no learned signal" and falls back to pure rule-based behavior. This means we can ship the system on day 1 without waiting for the learning layer to be useful.

## Week-6 activation pattern

The single most important behavioral design choice. Don't blur this:

```
Day 0–41: data collection only
  → optimizer runs against rules + Layer 1+2 only
  → Layer 3 module exists in code but `is_active = False`
  → Firestore accumulates state snapshots and decisions

Day 42 (or first day where 42 days + 85% data quality): learning_check.py
  → detects readiness
  → sends FCM push to user: "Klaar om patronen te leren?"
  → notification deep-links to /settings/learning

User taps "Activate":
  → POST /learning/respond with accepted=true
  → policy.learning_enabled = True
  → activation_status.is_active = True
  → next nightly job trains the LearnedProfile from history
  → optimizer starts using suggestions from suggest()

User taps "Snooze" or dismisses:
  → push_dismissed_count += 1
  → re-prompt in 7 days
  → after 3 dismisses: cool-off 30 days

User never responds:
  → Layer 3 stays dormant indefinitely
  → system works fine without it (just less personalized)
```

Never auto-activate Layer 3. Never. The user opting in is part of the trust contract.

## Tech stack (decided, do not revisit)

| Concern | Choice | Why |
|---|---|---|
| Backend language | Python 3.12 | Optimizer libraries (Pyomo, OR-Tools, NumPy), Roel comfortable with it |
| Backend framework | FastAPI | Async, types, auto-OpenAPI |
| Backend runtime | Google Cloud Run | Scale-to-zero, fits Roel's tiny scale, cheap |
| Backend deps | `uv` for package management | Faster than pip/poetry |
| State store | Firestore | Same stack Roel uses for Draftly; realtime sync to PWA |
| Auth | Firebase Auth | Same |
| Push | Firebase Cloud Messaging | Same |
| Scheduling | Cloud Scheduler | Native GCP, free tier, reliable cron |
| Secrets | Secret Manager + WIF | No static keys, no JSON service accounts |
| Frontend lang | TypeScript | Type safety across the stack |
| Frontend framework | React 18 + Vite | Same as Draftly |
| Styling | Tailwind CSS (full, not core utility subset) | More creative range than artifact preview |
| Charts | Recharts | Light, composable |
| Icons | Lucide React | Same as Draftly |
| Hosting | Netlify | Same as Draftly; PWA-friendly |
| Domain | `hesm-huisjes.netlify.app` initially | No domain costs; migrate to `hesm.huisjes.[tld]` later if commercialized |
| AI | Anthropic Claude API (Sonnet 4.7 latest) | Roel's preferred model; in-app chat + decision rationales |
| Repo | Public on GitHub: `roelhuisjes/hesm-huisjes` | MIT license |
| Code style Python | `ruff` + `mypy --strict` | Clean, typed |
| Code style TS | Default Vite ESLint + Prettier | Don't bikeshed |
| Tests | `pytest` (Python), `vitest` (TS) | Standard |

## What's already built

In `/apps/optimizer/src/`:
- `optimizer/policy.py` — Layer 1 (SystemLimits, TempBand) + Layer 2 (Strategy, StrategyWeights) + Policy with Firestore (de)serialization
- `optimizer/learning.py` — Layer 3 with `LearningLayer` class, `ActivationStatus`, `is_ready_for_activation()`, `LearnedProfile` and stub extractors marked TODO
- `jobs/learning_check.py` — daily cron handler that detects readiness and triggers push
- `notifications/push.py` — FCM helper using Firebase Admin SDK (web push + APNS configured)
- `state/models.py` — Pydantic DTOs for `SystemState`, `Decision`, `FCMToken`, plus persistence mirrors of `ActivationStatus` / `LearnedProfile` (PR1)
- `state/firestore.py` — Firestore data layer with all collection helpers used by `main.py`, `learning_check.py`, `push.py` (PR1)
- `connectors/base.py` — shared exception hierarchy (`ConnectorError`, `ConnectorAuthError`, `ConnectorUnavailable`, `ConnectorMalformed`) for all third-party clients (PR2)
- `connectors/homewizard.py` — async HomeWizard P1 client against the **local v1 API** (`/api`, `/api/v1/data`). Reads `HOMEWIZARD_BASE_URL` + optional `HOMEWIZARD_HEADER_*` env vars. Tunnel choice deferred to PR5 — see `infra/SETUP.md` (PR2)
- `connectors/entsoe.py` — async ENTSO-E Transparency Platform client. `get_day_ahead_prices(date)` returns 24 (or 23/25 on DST) `HourlyPrice` rows with raw spot €/MWh and **VAT-inclusive** all-in EUR/kWh: `((spot/1000) + 0.1108 + 0.025) * 1.21`. Matches Tibber/Frank/EnergyZero retail quoting. Uses `defusedxml` for safe XML parsing. Reads `ENTSOE_API_TOKEN` from env / Secret Manager (PR3)
- `connectors/openmeteo.py` — async Open-Meteo client (no auth) returning hourly temp + cloud cover for Sittard, with a crude PV estimate (sine elevation × cloud factor). Defaults to 50.99°N/5.87°E; overridable via `OPENMETEO_LATITUDE`/`OPENMETEO_LONGITUDE`/`OPENMETEO_BASE_URL`. Solcast replaces the PV model post-launch (PR4)
- `connectors/weheat.py` — **read-only** WeHeat third_party API client. OAuth2 authorization_code + PKCE bootstrap (one-time `scripts/weheat_bootstrap.py`) yields a refresh token in Secret Manager (`weheat-refresh-token`); `_RealWeHeatClient` exchanges it for an access token on demand and queries the `weheat==2026.4.8` SDK (`HeatPumpDiscovery`, `HeatPump.async_get_logs`). `MockWeHeatClient` returns coherent synthetic data when the refresh token is absent. No write paths exist on the public API (PR6).
- `connectors/resideo.py` / `shelly.py` / `growatt.py` — each exposes a real-cloud client class **and** a `MockXxxClient` returning coherent synthetic data, picked by an `xxx_client()` factory based on whether vendor creds are set in env. Real clients are sealed with `NotImplementedError` pending vendor access; mocks let the optimizer cycle run end-to-end on staging. ENTSO-E gained the same fallback (`entsoe_client()`) — no token → mock 24h prices via a typical NL daily curve (PR12)
- `main.py` — production FastAPI app, **deployed on Cloud Run**. `/health` (public), `/policy` (CRUD), `/learning/respond`, `/override`, `/jobs/learning-check`, `/chat` (streaming SSE — Claude Sonnet 4.6), `/optimize` (runs the full cycle via `optimizer.cycle.run_cycle`). OIDC-token verification for scheduler endpoints via `SCHEDULER_ALLOWED_EMAILS`. Sentry SDK initialised at startup if `SENTRY_DSN` is set (PR5, extended PR10, PR13)
- `optimizer/v0.py` — rule-based decider. Pure function `plan_next_quarter(state, limits, current_price, avg_price_today, pv_surplus, overrides)` → `Plan` with one of {BOOST, PV-DUMP, COAST, NORMAL, NEG-PRICE, OVERRIDE}. Respects Layer-1 hard limits via clamping helper (PR13)
- `optimizer/cycle.py` — orchestrates one 15-min cycle: gather state in parallel, compose `StateInput`, plan, apply (clamped), persist `SystemState` + `Decision` to Firestore. Resilient to per-connector failures via `asyncio.gather`-with-fallbacks. Sole entrypoint from `/optimize` (PR13)
- `ai/claude.py` — Anthropic-backed conversational layer. `answer_with_context(messages)` streams Server-Sent Events. System prompt composed from live Firestore state (persona + house spec + Layer 1/2 policy + most-recent SystemState + last 24 h of decisions) with one `cache_control: {"type": "ephemeral"}` breakpoint — within a 15-min cycle the prompt is byte-stable and follow-up questions read the cache at ~10% cost. Default model `claude-sonnet-4-6`, overridable via `HESM_CHAT_MODEL` (PR10)

In `/apps/optimizer/`:
- `pyproject.toml` — uv-managed deps + ruff + mypy strict + pytest config (PR1)
- `tests/` — pytest suite with in-memory `FakeFirestore` fake, 15 tests covering policy / activation / state snapshots / decisions / FCM tokens / learned profile (PR1)

In `/apps/dashboard/`:
- `src/pages/Simple.tsx` — iPad-default page with current-action sentence, today/month savings, big override button. Imports `useLiveState` and `OverrideSheet` (not yet built — see PR8/9)
- `public/manifest.json` — PWA manifest with shortcuts

In root:
- `README.md` — public-facing project overview

**Not yet built (needs you):**
- `connectors/` — real `resideo.py` / `shelly.py` / `growatt.py` (mocks live; real OAuth flows pending)
- `ai/claude.py` — chat backend with system-context injection
- `safety/failsafe.py`, `safety/watchdog.py` — failsafe checks
- (Frontend complete — PR11a/b/c/d shipped)
- Infra: `Dockerfile`, `pyproject.toml`, `cloudbuild.yaml`, `firestore.rules`, WIF bindings, Cloud Scheduler jobs

## Roadmap (PR sequence)

Work these top-to-bottom unless you discover a blocker. Each PR is its own branch, opened against `main`, ~50-250 lines, with tests where applicable.

1. **PR1 — Firestore state layer** ✅ shipped
   - `state/models.py`, `state/firestore.py`, `tests/` with in-memory fake — all helpers from `main.py` / `learning_check.py` / `push.py` resolve. ruff + mypy --strict + 15 tests pass.
   - Discrepancy uncovered: `main.py` imports `src.optimizer.v0`, `src.connectors`, `src.ai.claude` — none exist. Wiring deferred to PR5.
2. **PR2 — HomeWizard P1 connector** ✅ shipped
   - Built against the **local v1 API** — HomeWizard has no public cloud API.
   - **Validated against real hardware 2026-04-30**: P1 op `192.168.1.132` (`hw-p1meter-42b5ea.home`), serial `5c2faf42b5ea`, firmware 5.19, DSMR 5.0. Connector parst alle velden zonder errors.
   - **Tunnel parked.** Roel switcht per 1 juli 2026 naar Tibber/Frank/EnergyZero — die geven dezelfde P1-data via cloud-API en maken een lokale tunnel obsoleet. Tot dan: optimizer draait op mocks (PR12). Heroverweeg alleen als WeHeat live komt vóór 1 juli én de supplier-switch nog niet is gemaakt.
   - Established the connector pattern: shared `ConnectorError` hierarchy in `connectors/base.py`, async httpx client, env-driven config, MockTransport tests. PR3+ copy this shape.
3. **PR3 — ENTSO-E prices connector** ✅ shipped
   - Async client for `web-api.tp.entsoe.eu/api`, document type A44 / process A01, NL domain `10YNL----------L`. Returns hourly `HourlyPrice(timestamp_utc, spot_eur_mwh, all_in_eur_kwh)` for a given local day; tolerates DST 23/25-hour days.
   - Conversion: `((spot/1000) + 0.1108 + 0.025) * 1.21` — VAT-inclusive, matches how Tibber/Frank/EnergyZero quote tariffs. Constants live in `entsoe.py`.
   - Token via `ENTSOE_API_TOKEN` query param. `defusedxml` added for safe XML parsing. ruff + mypy --strict + 18 new tests (53 total) pass.
4. **PR4 — Open-Meteo weather connector** ✅ shipped
   - `connectors/openmeteo.py` + 21 MockTransport tests. Hourly temp + cloud cover for Sittard, parsed into UTC `HourlyForecast` rows with a crude PV estimate. Solcast replaces the PV model post-launch.
   - Sittard coordinates corrected: **50.99°N**, 5.87°E (the earlier 51.99 was a typo — that latitude lies near Eindhoven).
5. **PR5 — Cloud Run skeleton deploy** ✅ shipped
   - **Live**: https://hesm-optimizer-4rsk5dywaa-ez.a.run.app/health
   - Multi-stage Dockerfile + `cloudbuild.yaml` (manual `gcloud builds submit` for now; GitHub trigger documented in `infra/SETUP.md`).
   - Two service accounts: `hesm-optimizer` runtime SA (Firestore, secrets, FCM, logs/metrics/trace) and `hesm-scheduler` invoker SA. Project-level minimum-privilege IAM bindings.
   - Cloud Scheduler jobs: `*/15 * * * *` POST `/optimize`, `0 19 * * *` POST `/jobs/learning-check`, both with OIDC tokens whose email is verified against `SCHEDULER_ALLOWED_EMAILS` in `main.py`.
   - Secret Manager: `anthropic-api-key` and `sentry-dsn` mounted via `--set-secrets`.
   - Firestore in `europe-west4` Native mode + `firestore.rules` deployed via Firebase CLI.
   - Sentry SDK init at startup; `SENTRY_DSN` blank in dev silently skips it.
   - `/health` public; `/policy` + `/learning/respond` + `/override` work end-to-end; `/optimize` returns 503 with explicit message until PR6-9 wire device connectors. End-to-end smoke verified: scheduler-triggered `/jobs/learning-check` initialised `data_start` in Firestore.
   - Full runbook in `infra/SETUP.md` (deploy, rollback, log access, secret rotation, missing tunnel/ENTSO-E token notes).
6. **PR6 — WeHeat connector** ✅ shipped
   - **Important correction during this PR:** the WeHeat public `third_party` API is **read-only** (only `GET` endpoints — confirmed against the OpenAPI shape of `weheat==2026.4.8`). No DHW setpoint writes, no on/off, no manual mode. The Home Assistant integration is `cloud_polling` for the same reason.
   - Auth: OAuth2 **authorization_code + PKCE** against the WeHeat Keycloak realm (`auth.weheat.nl`), not client_credentials. Public OAuth client `HomeAssistantAPI` (shipped publicly with HA) — anyone with a WeHeat account can use it. Scopes `openid offline_access`. Initial dance happens once via `scripts/weheat_bootstrap.py` (local browser → captures `refresh_token`); Cloud Run reads the refresh token from Secret Manager (`weheat-refresh-token`) and refreshes access tokens headless.
   - Implementation wraps the official `weheat` SDK (`HeatPumpDiscovery`, `HeatPump`); thin adapter so a future swap is one-file.
   - Connector returns `WeHeatStatus` (read-only): boiler/buffer/flow/return temps, HP power in/out, COP, compressor %, room thermostat readback, `heat_pump_state` enum. No `set_*` methods anywhere.
   - `optimizer/cycle.py:_apply_plan` accordingly sends only Shelly relay commands (the immersion heater is the only DHW lever). Boiler-target stays in the Plan as an informational target that drives dompelaar logic.
7. **PR7 — Resideo Lyric connector** ✅ shipped
   - Honeywell Home developer-portal OAuth2 (`authorization_code` flow with HTTP Basic at the token endpoint). One-time `scripts/resideo_bootstrap.py` captures a refresh token → Secret Manager (`resideo-refresh-token` + `resideo-client-id` + `resideo-client-secret`). Cloud Run swaps refresh→access on demand; tokens cached for their TTL.
   - Endpoints used: `GET /v2/locations` (discovery) → first location → first thermostat → `GET /v2/devices/thermostats/{id}?locationId=…` (read) / `POST` same path (write `heatSetpoint` + `mode=Heat` + `TemporaryHold`).
   - **This is the first connector with a real *write* path.** WeHeat is read-only by API design; PV/HP have no write surface; Shelly write-path arrives only when the dompelaar is physically installed. So Resideo is currently the only lever the optimizer can actuate end-to-end.
   - Falls back to `MockResideoClient` when any of the three env vars is unset. 16 new tests (175 total) — mypy strict + ruff clean.
8. **PR8 — Shelly Cloud connector** — **parked**, hardware-blocked. The Shelly Pro 2PM relay sits in front of the 3 kW immersion heater inside the boiler tank; the dompelaar is not yet installed (Roel has confirmed 2026-06-22). Wire this PR up once installation lands.
9. **PR9 — Zonneplan connector** ✅ shipped (supersedes the original Growatt-cloud plan). One cloud call to `app-api.zonneplan.nl` yields P1 net power, VAT-inclusive retail tariff, and PV production — replacing the ENTSO-E retail-markup formula and the parked HomeWizard tunnel in one PR. Auth uses email → magic-link → bearer + refresh token; bootstrap via `scripts/zonneplan_bootstrap.py`. Cycle now prefers Zonneplan for PV / grid net / current tariff, falls back to Growatt / HomeWizard / ENTSO-E if Zonneplan is unavailable. 11 new tests (192 total). The existing `connectors/growatt.py` stays as a PV-detail fallback but is not the primary source.
10. **PR10 — Claude AI chat backend** ✅ shipped
    - `src/ai/claude.py` with async `answer_with_context(messages)` streaming Server-Sent Events. System prompt rebuilt per request from Firestore (persona + house spec + Layer 1/2 + last SystemState + 24 h of decisions); one `cache_control` breakpoint at the system block.
    - Default `claude-sonnet-4-6` (Sonnet 4.7 doesn't exist — corrected from CLAUDE.md). Override via `HESM_CHAT_MODEL` env.
    - `/chat` endpoint live at the Cloud Run URL; verified with real Anthropic API call.
    - 13 new tests (87 total) using a fake AsyncAnthropic client; mypy strict + ruff clean.
11. **PR11 — Frontend essentials** (split into 11a/b/c)
    - **PR11a — foundation** ✅ shipped. Vite + React 18 + TS + Tailwind + React Router + Firebase web SDK. Auth context with Google popup sign-in. `useLiveState` hook subscribing to Firestore in realtime. `OverrideSheet` bottom-sheet posting to Cloud Run `/override`. Sign-in page + `RequireAuth` guard. `netlify.toml` with SPA rewrites + security headers.
    - **PR11b — Settings pages** ✅ shipped. Four settings pages (Limits / Strategy / Learning / Connectors) editing Layer 1+2 policy, showing Layer-3 activation progress, and rendering the `/health` wiring map. Typed API client (`src/lib/api.ts`) for `/policy`, `/learning/respond`, `/health`. Placeholder `Advanced.tsx` so the "details" link from Simple isn't broken — full Advanced view comes in 11c.
    - **PR11c — Advanced page** ✅ shipped. Live state panel, Open-Meteo 48h temp+cloud chart (Recharts), 24h decisions timeline (Firestore subscription), SSE streaming AI chat panel. EPEX price chart is a placeholder until ENTSO-E API token lands. Recharts + date-fns added; bundle now ~1 MB JS — code-splitting in 11d.
    - **PR11d — PWA shell + service worker + code-split** ✅ shipped. `vite-plugin-pwa` precaches the shell; Cloud Run / Firestore / Open-Meteo always hit network (no stale data). Lazy-loaded `/advanced` route puts Recharts in its own chunk. Manual chunks split Firebase + Recharts so Simple's first paint stays small. iOS-friendly install hint (Safari has no `beforeinstallprompt`, the banner shows the Share-menu route). Manifest references a single SVG icon.

After all three, the system is deployable end-to-end. Then we replace mocks with real connectors when hardware lands (~late Q2 2026).

## External APIs & credentials

Roel will obtain these. Document the steps in `infra/SETUP.md` so he can do it without you holding his hand:

| Service | What | How |
|---|---|---|
| Anthropic | Claude API key | console.anthropic.com → API Keys |
| ENTSO-E | EPEX prices | Email request to transparency@entsoe.eu |
| WeHeat | Refresh token | One-time `uv run scripts/weheat_bootstrap.py` (uses public HA OAuth client `HomeAssistantAPI`); store result in Secret Manager as `weheat-refresh-token` |
| Resideo | Client ID + Secret + Refresh Token | developer.honeywellhome.com → My Apps → use redirect `http://localhost:8765/callback`. Then one-time `uv run scripts/resideo_bootstrap.py --client-id … --client-secret …`. See `infra/SETUP.md` for the full PR7 runbook. |
| Shelly Cloud | Auth key | Shelly account → settings → cloud auth |
| HomeWizard | Token | HomeWizard Energy app → settings → token |
| Growatt | Username + Password | Existing ShinePhone account |
| Solcast (later) | API key | solcast.com hobbyist tier |

All stored in Secret Manager. Loaded via WIF at runtime. Never committed.

## Code standards

**Python:**
- Type hints everywhere (`mypy --strict` passes)
- Dataclasses or Pydantic models for data structures (Pydantic for API I/O, dataclasses for internal state)
- Async by default for I/O (httpx, not requests)
- No bare `except`; catch specific exceptions, log with context
- Tests for any function that does math on money or temperatures
- One concern per module (don't merge unrelated helpers)

**TypeScript:**
- Strict mode on
- Functional components only, no classes
- Hooks for state, no Redux
- Tailwind utility classes; minimal custom CSS
- No `any` types; use `unknown` if you must escape

**Errors & logging:**
- Structured logs (JSON in production via Cloud Logging)
- Every error path includes context (which device, which user action, which state)
- Failures that affect Roel's house comfort trigger an FCM alert via `send_alert()`

**Comments:**
- English in code
- Dutch in user-facing strings (UI labels, push notification text, AI chat responses)
- Don't comment what; comment why
- Module docstrings explain the role within the architecture

**Commits:**
- Conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`)
- One logical change per commit
- PR descriptions reference the PR number from the roadmap

## Anti-patterns (don't do these)

- ❌ **Don't add an edge device.** Roel rejected it. All control is cloud.
- ❌ **Don't lock in a vendor.** Each connector lives behind an interface. Replacing WeHeat with another HP brand should be a one-file change.
- ❌ **Don't auto-activate Layer 3.** Ever. Push prompt → user taps → activated. No exceptions.
- ❌ **Don't violate Layer 1 limits.** Even with a "good reason". The user can update the limit in settings if they want; the optimizer never silently exceeds.
- ❌ **Don't fail silently.** If a connector errors, log it AND raise an FCM alert if it persists more than 30 minutes.
- ❌ **Don't generate generic AI app aesthetics.** No purple gradients on white. No Inter font. No emoji-as-decoration. The artifact preview shows the target aesthetic — refined, dark, mono-numeric, single warm accent. Maintain that.
- ❌ **Don't write 500-line PRs.** Split. If you can't, it's because you're combining unrelated concerns.
- ❌ **Don't hardcode test data in production code paths.** Mocks live in `tests/`. Connectors return real data or raise.
- ❌ **Don't ship without tests for Layer 1.** The validator and the limit-enforcement code paths must be tested.
- ❌ **Don't reinvent.** Use the libraries already in `pyproject.toml`. If you need a new one, justify in the PR description.

## How to proceed

**First action this session:** read `optimizer_v0.py`, `policy.py`, `learning.py`, `learning_check.py`, `push.py`, `main.py` end-to-end. Understand the existing shape. Then start PR1 (Firestore state layer).

**Use specialized agents (Task tool) when it actually parallelizes work:**
- One agent on the connector for PR2/3/4 (these are independent and similar in shape — could fan out)
- One agent on Tailwind + routing setup while another does the Firestore layer
- Don't fan out for things that share state or have ordering dependencies

**If you hit a real blocker:** ask Roel directly. Don't guess at architectural choices. Examples that warrant asking:
- A connector requires a hardware setting Roel needs to flip
- An API behaves differently from documentation and requires a workaround that has user-visible implications
- You discover the existing scaffold has a flaw that requires reshaping

**Examples that don't warrant asking:**
- Choosing between two equivalent implementations
- Naming a variable
- Adding a small dep that's clearly justified
- Refactoring something within a single file for clarity

**When you finish a PR:** update this CLAUDE.md's "What's already built" and "Roadmap" sections. Open the next branch automatically. Ship steadily.

## Definition of done (per PR)

- [ ] Code passes ruff + mypy --strict (Python) or ESLint (TS)
- [ ] Tests pass for new code; existing tests still pass
- [ ] PR description states scope, decisions made, anything non-obvious
- [ ] CLAUDE.md updated if architecture or roadmap shifted
- [ ] No new TODOs in main code paths (move them to GitHub issues if real)
- [ ] Branch pushed, PR opened, ready for Roel's review

## Definition of done (whole project, V1)

- [ ] All 11 PRs merged
- [ ] Cloud Run service deployed and stable for 7 consecutive days
- [ ] Roel can install the PWA on his iPad and his iPhone
- [ ] Optimizer runs every 15 min against real device data
- [ ] AI chat works and gives sensible answers about live state
- [ ] Override flow works end-to-end
- [ ] Failsafe verified by killing the service in staging — house behaves normally
- [ ] Layer 3 activation push tested in staging (fast-forward the data clock)

After V1: V2 adds the battery integration when Roel buys one. V3 considers the App Store path (only if Roel decides to commercialize after living with it for 6+ months).

---

**Last updated:** 2026-04-26 (initial briefing handover from conversation with Roel)
**Maintained by:** whoever's coding. Update when material decisions change.
