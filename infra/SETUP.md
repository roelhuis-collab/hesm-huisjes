# HESM infrastructure setup

This document is the runbook for the manual one-time steps that the code
itself can't do — provisioning GCP resources, exchanging tokens, exposing
the local HomeWizard P1 meter to the internet, etc.

> **Status:** stub. Fully fleshed out in PR5 (Cloud Run + WIF deploy).
> Items added before PR5 are decisions deferred from earlier PRs that
> need to land somewhere durable.

## Open decisions deferred to PR5

### HomeWizard P1 — local-API exposure

**Context.** HomeWizard does not expose a public, documented cloud API.
The official API is local-network only. To keep Cloud Run cloud-only the
way CLAUDE.md intends, the local API is exposed to the internet via a
thin tunnel running on something always-on at home.

**Decision (PR2).** Connector built against the **local v1 API**
(`GET /api`, `GET /api/v1/data`). It reads `HOMEWIZARD_BASE_URL` and
optional auth headers via `HOMEWIZARD_HEADER_*` env vars. Whatever URL
is set there is whatever Cloud Run will hit — local IP, tunnel, or
something else later.

**Open: which tunnel?** Pick during PR5, when WIF + Secret Manager
wiring lands.

| Option | Hardware | Effort | Pros | Cons |
|---|---|---|---|---|
| **Cloudflare Tunnel** | something always-on (Pi Zero 2 W ~€20, NAS, router) | low | free; HTTPS terminated by Cloudflare; can layer Access service-tokens | needs Cloudflare account + free domain, or use `<id>.cfargotunnel.com` |
| **Tailscale + funnel** | same | very low | dead-simple install; SSO if desired | Funnel domain is `*.ts.net`; tied to Tailscale |
| **No tunnel — keep local-only** | none extra | none | cheapest | requires moving the optimizer to a LAN host, conflicts with cloud-only principle |

Recommended: Cloudflare Tunnel + Cloudflare Access service-token
authentication. Set `HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_ID` and
`HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_SECRET` in Secret Manager so only
Cloud Run can hit the tunnel.

**Future swap.** If/when Roel switches to Tibber and buys a Tibber Pulse,
the connector becomes a thin Tibber-GraphQL client and the tunnel device
goes away. Single-file PR.
