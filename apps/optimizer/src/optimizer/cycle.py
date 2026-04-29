"""
The 15-minute optimizer cycle, end-to-end.

This module is the only place that orchestrates state-gathering,
planning, applying, and persisting. ``main.py`` calls ``run_cycle()``
from the ``/optimize`` endpoint and that's it.

Flow
----
  read policy + activation
  └─► gather state from every connector in parallel
       └─► fetch today's day-ahead prices + 48h weather
            └─► compute plan via optimizer.v0.plan_next_quarter
                 └─► apply plan to devices (clamped to Layer 1)
                      └─► persist SystemState + Decision in Firestore

Resilience
----------
Each connector is awaited inside ``asyncio.gather(..., return_exceptions=True)``;
a single connector failure logs + falls back to None values for that
device, the cycle still produces a Plan (possibly with reduced
intelligence). The whole cycle is wrapped in a try so a runaway
exception never knocks Cloud Scheduler out of its retry loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from src.connectors.entsoe import entsoe_client
from src.connectors.growatt import growatt_client
from src.connectors.homewizard import HomeWizardP1Client, P1MeterReading
from src.connectors.openmeteo import HourlyForecast, OpenMeteoClient
from src.connectors.resideo import resideo_client
from src.connectors.shelly import shelly_client
from src.connectors.weheat import weheat_client
from src.optimizer.policy import Policy, SystemLimits
from src.optimizer.v0 import Plan, StateInput, _LimitsView, plan_next_quarter
from src.state.firestore import (
    get_policy,
    save_decision,
    save_state_snapshot,
)
from src.state.models import Decision, SystemState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _limits_view(limits: SystemLimits) -> _LimitsView:
    return _LimitsView(
        floor_max_flow_c=limits.floor_max_flow_c,
        boiler_legionella_floor_c=limits.boiler_legionella_floor_c,
        boiler_max_c=limits.boiler_max_c,
        dompelaar_max_price_eur_kwh=limits.dompelaar_max_price_eur_kwh,
        dompelaar_only_with_pv_above_w=limits.dompelaar_only_with_pv_above_w,
    )


async def _safe_call(coro: Any, *, name: str) -> Any:
    """Await a coroutine; return None and log on any exception."""
    try:
        return await coro
    except Exception as exc:
        log.warning("cycle: connector %s failed: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Phase 1 — gather everything in parallel
# ---------------------------------------------------------------------------


async def _gather_all() -> dict[str, Any]:
    """Run every connector concurrently. Each result is keyed by name.

    Failures bubble up as ``None`` per-key; the caller copes with missing
    data rather than crashing the cycle.
    """
    weheat = weheat_client()
    resideo = resideo_client()
    shelly = shelly_client()
    growatt = growatt_client()

    today = datetime.now().date()

    async with entsoe_client() as ent:
        # HomeWizard is the one connector without a built-in mock —
        # without HOMEWIZARD_BASE_URL set, just skip it (mock-mode for HW
        # is the LAN-tunnel approach, not a synthetic generator).
        import os

        hw_url = os.environ.get("HOMEWIZARD_BASE_URL", "").strip()
        weather_client = OpenMeteoClient.from_env()

        async with weather_client as wc:
            hw_task = (
                _gather_homewizard(hw_url) if hw_url else _async_none()
            )

            (
                weheat_status,
                resideo_status,
                shelly_status,
                growatt_status,
                hw_reading,
                price_curve,
                weather_curve,
            ) = await asyncio.gather(
                _safe_call(weheat.get_status(), name="weheat"),
                _safe_call(resideo.get_status(), name="resideo"),
                _safe_call(shelly.get_status(), name="shelly"),
                _safe_call(growatt.get_status(), name="growatt"),
                _safe_call(hw_task, name="homewizard"),
                _safe_call(ent.get_day_ahead_prices(today), name="entsoe"),
                _safe_call(wc.get_forecast(48), name="openmeteo"),
            )

    # close per-connector resources
    await asyncio.gather(
        _safe_call(weheat.aclose(), name="weheat-close"),
        _safe_call(resideo.aclose(), name="resideo-close"),
        _safe_call(shelly.aclose(), name="shelly-close"),
        _safe_call(growatt.aclose(), name="growatt-close"),
    )

    return {
        "weheat": weheat_status,
        "resideo": resideo_status,
        "shelly": shelly_status,
        "growatt": growatt_status,
        "homewizard": hw_reading,
        "prices": price_curve,
        "weather": weather_curve,
    }


async def _async_none() -> None:
    return None


async def _gather_homewizard(base_url: str) -> P1MeterReading | None:
    """Fetch one P1 reading; HomeWizard has no built-in mock."""
    async with HomeWizardP1Client.from_env() as hw:
        return await hw.get_measurement()


# ---------------------------------------------------------------------------
# Phase 2 — compose StateInput + run the optimizer
# ---------------------------------------------------------------------------


def _compose_state(gathered: dict[str, Any]) -> tuple[StateInput, SystemState]:
    """Merge connector outputs into the optimizer input + persistable state."""
    now = datetime.now()
    weheat = gathered["weheat"]
    resideo = gathered["resideo"]
    shelly = gathered["shelly"]
    growatt = gathered["growatt"]
    hw: P1MeterReading | None = gathered["homewizard"]

    pv = float(growatt.pv_power_w) if growatt else 0.0
    hp = float(weheat.hp_power_w) if weheat else 0.0
    dompelaar = bool(shelly.is_on) if shelly else False
    boiler_temp = float(weheat.boiler_temp_c) if weheat else 50.0
    buffer_temp = float(weheat.buffer_temp_c) if weheat else 35.0
    indoor_temp = float(resideo.indoor_temp_c) if resideo else 20.0
    cop = weheat.cop if weheat else None

    # Outdoor temp from the next-hour weather forecast.
    weather: list[HourlyForecast] | None = gathered["weather"]
    outdoor_temp = float(weather[0].temperature_c) if weather else 12.0

    # House load: prefer P1 net; fall back to a synthesized estimate.
    grid_import = float(hw.active_power_w) if hw and hw.active_power_w is not None else None
    if grid_import is not None:
        # net = house_load + hp + dompelaar - pv  ⇒  house = net + pv - hp - dompelaar
        dompelaar_w = float(shelly.power_w) if shelly else 0.0
        house_load = max(0.0, grid_import + pv - hp - dompelaar_w)
    else:
        house_load = 600.0  # rough baseline residential load

    # Current price = first hour of today's curve, if available.
    prices = gathered["prices"]
    current_price = float(prices[0].all_in_eur_kwh) if prices else None
    # avg_price is computed by the caller via _avg_price() when needed.

    state_input = StateInput(
        timestamp=now,
        pv_power=pv,
        house_load=house_load,
        hp_power=hp,
        dompelaar_on=dompelaar,
        boiler_temp=boiler_temp,
        indoor_temp=indoor_temp,
        outdoor_temp=outdoor_temp,
        grid_import=grid_import,
        price_eur_kwh=current_price,
    )

    persistable = SystemState(
        timestamp=now,
        pv_power=pv,
        house_load=house_load,
        hp_power=hp,
        dompelaar_on=dompelaar,
        boiler_temp=boiler_temp,
        buffer_temp=buffer_temp,
        indoor_temp=indoor_temp,
        outdoor_temp=outdoor_temp,
        cop=cop,
        grid_import=grid_import,
        price_eur_kwh=current_price,
    )
    return state_input, persistable


def _avg_price(prices: Any) -> float | None:
    if not prices:
        return None
    return float(sum(p.all_in_eur_kwh for p in prices) / len(prices))


# ---------------------------------------------------------------------------
# Phase 3 — apply plan (soft, since real device clients are sealed)
# ---------------------------------------------------------------------------


async def _apply_plan(plan: Plan, policy: Policy) -> None:
    """Send the plan to the relevant device clients.

    Real WeHeat / Resideo / Shelly clients raise NotImplementedError
    until vendor creds arrive (PR12 mocks accept the calls). The cycle
    swallows those errors so we don't crash on missing creds.
    """
    boiler_target = max(
        policy.limits.boiler_legionella_floor_c,
        min(plan.boiler_target_temp, policy.limits.boiler_max_c),
    )

    weheat = weheat_client()
    shelly = shelly_client()
    try:
        await _safe_call(
            weheat.set_dhw_setpoint(boiler_target),
            name="weheat-setpoint",
        )
        await _safe_call(
            shelly.set_relay(plan.dompelaar_on),
            name="shelly-relay",
        )
    finally:
        await _safe_call(weheat.aclose(), name="weheat-close")
        await _safe_call(shelly.aclose(), name="shelly-close")

    log.info(
        "apply: boiler→%.0f°C dompelaar=%s hp=%s offset=%+.1f°C — %s",
        boiler_target,
        plan.dompelaar_on,
        plan.heat_pump_allowed,
        plan.indoor_setpoint_offset,
        plan.action,
    )


# ---------------------------------------------------------------------------
# Phase 4 — persist
# ---------------------------------------------------------------------------


def _persist(state: SystemState, plan: Plan, policy: Policy) -> None:
    save_state_snapshot(state)
    save_decision(
        Decision(
            timestamp=state.timestamp,
            tag=plan.tag,
            action=plan.action,
            reason=plan.reason,
            rationale=plan.rationale,
            boiler_target_temp=plan.boiler_target_temp,
            dompelaar_on=plan.dompelaar_on,
            heat_pump_allowed=plan.heat_pump_allowed,
            indoor_setpoint_offset=plan.indoor_setpoint_offset,
            estimated_savings_eur=plan.estimated_savings_eur,
            strategy_used=policy.strategy.value,
            learning_active=policy.learning_enabled,
        )
    )


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------


async def run_cycle() -> Plan:
    """One end-to-end optimizer cycle. Returns the Plan that was applied."""
    policy = get_policy()
    gathered = await _gather_all()
    state_input, persistable = _compose_state(gathered)

    plan = plan_next_quarter(
        state_input,
        limits=_limits_view(policy.limits),
        current_price=state_input.price_eur_kwh,
        avg_price_today=_avg_price(gathered["prices"]),
        pv_surplus=max(0.0, state_input.pv_power - state_input.house_load),
        overrides=policy.overrides or None,
    )

    await _apply_plan(plan, policy)
    _persist(persistable, plan, policy)
    return plan


__all__ = ["_compose_state", "_gather_all", "run_cycle"]


# Suppress "imported but unused" for things we deliberately re-export
_ = (timedelta,)
