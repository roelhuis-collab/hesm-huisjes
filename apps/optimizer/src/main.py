"""
Cloud Run service entrypoint.

Endpoints
---------
  POST /optimize              — run a single 15-min optimization cycle (Cloud Scheduler)
  POST /jobs/learning-check   — daily learning-readiness check (Cloud Scheduler)
  POST /chat                  — AI chat (Claude) with full system context
  GET  /policy                — read current policy
  PUT  /policy                — update policy (Layer 1+2)
  POST /learning/respond      — user accepted/dismissed activation prompt
  POST /override              — temporary manual override
  GET  /health                — liveness probe (no auth)

Auth model
----------
* ``/health`` is unauthenticated and returns 200 unconditionally.
* Cloud Scheduler endpoints (``/optimize``, ``/jobs/learning-check``) require
  a Google OIDC ID token in the ``Authorization`` header. Cloud Scheduler
  attaches one automatically when configured with an OIDC token; we verify
  it with ``google-auth`` and reject anything else.
* User endpoints (``/policy``, ``/chat``, ``/override``, ``/learning/respond``)
  expect a Firebase ID token. PR11 adds the Firebase Auth wiring on the
  frontend; until then those endpoints return 401.

Wiring status
-------------
The optimizer cycle and AI chat depend on connectors and modules that land
in later PRs (``optimizer.v0``, ``connectors.weheat`` etc., ``ai.claude``).
Until those exist, this module returns HTTP 503 with a clear message
pointing at the missing PR. ``/policy``, ``/health``, ``/learning-check``
and ``/override`` work today.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import firebase_admin
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from src.jobs.learning_check import handle_activation_response, run_daily_check
from src.optimizer.policy import Policy, Strategy, StrategyWeights
from src.state.firestore import get_policy, save_policy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("hesm")


# ---------------------------------------------------------------------------
# Sentry — initialized at import time so it captures startup errors too.
# ---------------------------------------------------------------------------

_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=0.1,
        environment=os.environ.get("HESM_ENV", "production"),
        send_default_pii=False,
    )
    log.info("sentry: initialized")
else:
    log.info("sentry: SENTRY_DSN not set, error reporting disabled")


# ---------------------------------------------------------------------------
# FastAPI app + Firebase Admin lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    log.info("hesm-optimizer started")
    yield
    log.info("hesm-optimizer shutting down")


app = FastAPI(title="HESM by Huisjes — optimizer", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

# Comma-separated list of email addresses (or unique IDs) of service
# accounts allowed to invoke Cloud-Scheduler-only endpoints. Cloud
# Scheduler attaches an OIDC token whose ``email`` claim equals the
# scheduler's runtime SA. We bind that to one specific SA in PR5's
# infra setup.
_SCHEDULER_ALLOWED = {
    s.strip().lower()
    for s in os.environ.get("SCHEDULER_ALLOWED_EMAILS", "").split(",")
    if s.strip()
}


def verify_scheduler_token(authorization: str | None) -> None:
    """Accept only Google OIDC tokens issued to the scheduler SA.

    In production ``SCHEDULER_ALLOWED_EMAILS`` is set to the email of the
    Cloud Scheduler invoker SA. Locally (env unset) the check is skipped
    so developers can curl the endpoint while testing.
    """
    if not _SCHEDULER_ALLOWED:
        return  # dev mode

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.split(None, 1)[1].strip()

    try:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        claims: dict[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token, ga_requests.Request()
        )
    except Exception as exc:
        log.warning("scheduler auth: token rejected — %s", exc)
        raise HTTPException(status_code=401, detail="invalid token") from exc

    email = str(claims.get("email", "")).lower()
    if email not in _SCHEDULER_ALLOWED:
        log.warning("scheduler auth: email %s not in allow-list", email)
        raise HTTPException(status_code=403, detail="email not allowed")


# ---------------------------------------------------------------------------
# Health + version
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe. Never authenticated. Cheap."""
    return {
        "status": "ok",
        "service": "hesm-optimizer",
        "wiring": {
            # Honest about what's actually wired to real things.
            "firestore": True,
            "homewizard_connector": True,
            "entsoe_connector": True,
            "openmeteo_connector": True,
            "weheat_connector": False,       # PR6
            "resideo_connector": False,      # PR7
            "shelly_connector": False,       # PR8
            "growatt_connector": False,      # PR9
            "ai_chat": False,                # PR10
            "optimizer_v0": False,           # arrives with PR5 wiring or later
        },
    }


# ---------------------------------------------------------------------------
# Optimization loop — wiring deferred
# ---------------------------------------------------------------------------


@app.post("/optimize")
async def optimize(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Run one 15-min optimization cycle. Triggered by Cloud Scheduler."""
    verify_scheduler_token(authorization)
    raise HTTPException(
        status_code=503,
        detail=(
            "/optimize is not yet wired: optimizer.v0 + WeHeat/Resideo/Shelly/Growatt "
            "connectors land in PR6-9. Cloud Scheduler will continue retrying — that's fine."
        ),
    )


# ---------------------------------------------------------------------------
# Daily learning check — fully wired
# ---------------------------------------------------------------------------


@app.post("/jobs/learning-check")
async def learning_check(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    verify_scheduler_token(authorization)
    return run_daily_check()


# ---------------------------------------------------------------------------
# AI chat — wiring deferred
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    messages: list[dict[str, str]]


@app.post("/chat")
async def chat(req: ChatRequest, request: Request) -> dict[str, Any]:
    raise HTTPException(
        status_code=503,
        detail="/chat lands in PR10 — Anthropic key is already in Secret Manager.",
    )


# ---------------------------------------------------------------------------
# Policy CRUD — fully wired
# ---------------------------------------------------------------------------


@app.get("/policy")
async def policy_get() -> dict[str, Any]:
    return get_policy().to_firestore()


class PolicyUpdate(BaseModel):
    strategy: Literal[
        "max_saving", "comfort_first", "max_self_consumption", "eco_green_hours", "custom"
    ] | None = None
    custom_weights: dict[str, float] | None = None
    limits: dict[str, Any] | None = None


@app.put("/policy")
async def policy_put(update: PolicyUpdate) -> dict[str, Any]:
    policy: Policy = get_policy()

    if update.strategy:
        policy.strategy = Strategy(update.strategy)

    if update.custom_weights:
        policy.custom_weights = StrategyWeights(**update.custom_weights)

    if update.limits:
        for key, value in update.limits.items():
            if hasattr(policy.limits, key):
                setattr(policy.limits, key, value)

    errors = policy.limits.validate()
    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    save_policy(policy)
    return {"status": "ok", "policy": policy.to_firestore()}


# ---------------------------------------------------------------------------
# Learning activation response — fully wired
# ---------------------------------------------------------------------------


class ActivationResponse(BaseModel):
    accepted: bool


@app.post("/learning/respond")
async def learning_respond(resp: ActivationResponse) -> dict[str, Any]:
    return handle_activation_response(resp.accepted)


# ---------------------------------------------------------------------------
# Manual override — fully wired (just records intent, /optimize honours it later)
# ---------------------------------------------------------------------------


class Override(BaseModel):
    kind: Literal["holiday", "guest_mode", "boost_dhw", "manual_off", "boost_heating"]
    duration_hours: float = 0
    payload: dict[str, Any] = {}


@app.post("/override")
async def override(o: Override) -> dict[str, Any]:
    from datetime import datetime

    policy = get_policy()
    policy.overrides[o.kind] = {
        "duration_hours": o.duration_hours,
        "payload": o.payload,
        "set_at": datetime.now().isoformat(),
    }
    save_policy(policy)
    log.info("override applied: %s for %sh", o.kind, o.duration_hours)
    return {"status": "ok"}
