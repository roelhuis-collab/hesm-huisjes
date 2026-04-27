"""
Cloud Run service entrypoint.

Endpoints:
  POST /optimize              — run a single 15-min optimization cycle (Cloud Scheduler)
  POST /jobs/learning-check   — daily learning-readiness check (Cloud Scheduler)
  POST /chat                  — AI chat (Claude) with full system context
  GET  /policy                — read current policy
  PUT  /policy                — update policy (Layer 1+2)
  POST /learning/respond      — user accepted/dismissed activation prompt
  POST /override              — temporary manual override
  GET  /health                — liveness probe
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import firebase_admin
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

from src.optimizer.policy import Policy, default_policy, Strategy, StrategyWeights
from src.optimizer.learning import LearningLayer
from src.jobs.learning_check import run_daily_check, handle_activation_response
from src.state.firestore import (
    get_policy, save_policy,
    get_activation_status, get_learned_profile,
    save_state_snapshot, save_decision,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("hesm")

CLOUD_SCHEDULER_TOKEN = os.environ.get("CLOUD_SCHEDULER_TOKEN", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    log.info("hesm-optimizer started")
    yield
    log.info("hesm-optimizer shutting down")


app = FastAPI(title="HESM by Huisjes — optimizer", lifespan=lifespan)


def verify_scheduler(authorization: str | None) -> None:
    """Lightweight auth for Cloud Scheduler endpoints."""
    if not CLOUD_SCHEDULER_TOKEN:
        return  # disabled in dev
    if authorization != f"Bearer {CLOUD_SCHEDULER_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


# --- Optimization loop ---------------------------------------------------

@app.post("/optimize")
async def optimize(authorization: str | None = Header(default=None)):
    """Run one 15-minute optimization cycle. Triggered by Cloud Scheduler."""
    verify_scheduler(authorization)

    policy = get_policy()
    activation = get_activation_status()
    learned = get_learned_profile() if activation.is_active else None
    learning = LearningLayer(activation, learned)

    # 1. Gather state from all device clouds (parallel; mocked for now)
    # TODO: replace with real connectors as hardware comes online
    from src.connectors import weheat, growatt, homewizard, resideo, shelly, entsoe, openmeteo

    state = await _gather_state()
    prices = await entsoe.get_prices_24h()
    weather = await openmeteo.get_forecast_24h()

    # 2. Get learning suggestions (empty if dormant)
    suggestions = learning.suggest(
        current_time=state.timestamp,
        outdoor_temp=state.outdoor_temp,
        pv_forecast=weather.pv_production_kw,
    )

    # 3. Run the optimizer (rule-based v0 for now, MILP later)
    from src.optimizer.v0 import plan_next_quarter
    plan = plan_next_quarter(state, prices, weather)

    # 4. Apply the plan to devices (parallel; respects Layer 1 limits)
    await _apply_plan(plan, policy)

    # 5. Persist for history + dashboard
    save_state_snapshot(state)
    save_decision(plan, policy, suggestions)

    log.info("optimize: %s", plan.rationale)
    return {"status": "ok", "plan": plan.rationale}


# --- Daily learning check -----------------------------------------------

@app.post("/jobs/learning-check")
async def learning_check(authorization: str | None = Header(default=None)):
    verify_scheduler(authorization)
    return run_daily_check()


# --- AI chat ------------------------------------------------------------

class ChatRequest(BaseModel):
    messages: list[dict]   # [{role: "user"|"assistant", content: "..."}, ...]


@app.post("/chat")
async def chat(req: ChatRequest):
    from src.ai.claude import answer_with_context
    return await answer_with_context(req.messages)


# --- Policy CRUD --------------------------------------------------------

@app.get("/policy")
async def policy_get():
    return get_policy().to_firestore()


class PolicyUpdate(BaseModel):
    strategy: Literal["max_saving", "comfort_first", "max_self_consumption", "eco_green_hours", "custom"] | None = None
    custom_weights: dict | None = None
    limits: dict | None = None


@app.put("/policy")
async def policy_put(update: PolicyUpdate):
    policy = get_policy()

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


# --- Learning activation response --------------------------------------

class ActivationResponse(BaseModel):
    accepted: bool


@app.post("/learning/respond")
async def learning_respond(resp: ActivationResponse):
    return handle_activation_response(resp.accepted)


# --- Manual override ----------------------------------------------------

class Override(BaseModel):
    kind: Literal["holiday", "guest_mode", "boost_dhw", "manual_off", "boost_heating"]
    duration_hours: float = 0
    payload: dict = {}


@app.post("/override")
async def override(o: Override):
    policy = get_policy()
    policy.overrides[o.kind] = {
        "duration_hours": o.duration_hours,
        "payload": o.payload,
        "set_at": __import__("datetime").datetime.now().isoformat(),
    }
    save_policy(policy)
    log.info("override applied: %s for %sh", o.kind, o.duration_hours)
    return {"status": "ok"}


# --- Health -------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Internal helpers (stubs for now) ----------------------------------

async def _gather_state():
    """Aggregate state from all device clouds. Mocked until connectors are live."""
    from src.optimizer.v0 import SystemState
    from datetime import datetime
    return SystemState(
        timestamp=datetime.now(),
        pv_power=4500, house_load=800, hp_power=900,
        dompelaar_on=False,
        boiler_temp=52, buffer_temp=38, indoor_temp=20.5, outdoor_temp=12,
    )


async def _apply_plan(plan, policy):
    """Send commands to devices, respecting Layer 1 limits."""
    boiler_target = min(plan.boiler_target_temp, policy.limits.boiler_max_c)
    boiler_target = max(boiler_target, policy.limits.boiler_legionella_floor_c)
    log.info("apply: boiler→%.0f°C dompelaar=%s hp=%s offset=%+.1f°C",
             boiler_target, plan.dompelaar_on, plan.heat_pump_allowed,
             plan.indoor_setpoint_offset)
    # TODO: real API calls when connectors land
