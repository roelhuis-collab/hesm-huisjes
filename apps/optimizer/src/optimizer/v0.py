"""
Rule-based optimizer (v0).

The "v0" name is intentional: this is a hand-rolled decider that gets
the system running while we accumulate state for a real MILP /
learning-based optimizer later. Every decision must:

  1. Respect Layer-1 hard limits (parquet floor, legionella, etc.)
  2. Apply Layer-2 strategy weights (cost / comfort / self-cons / green)
  3. Optionally use Layer-3 learned hints (when active)
  4. Produce a one-line Dutch ``reason`` for the dashboard + AI chat

The five tags drive UI rendering on Simple.tsx and the decisions
timeline:

  * ``BOOST``     — actively charge boiler / heat pump on cheap or PV-rich
  * ``PV-DUMP``   — PV surplus → dompelaar + boiler
  * ``COAST``     — peak price, ride out on stored heat
  * ``NORMAL``    — nothing special; default day profile
  * ``NEG-PRICE`` — wholesale negative, dump to dompelaar (capped)
  * ``OVERRIDE``  — user override active, optimizer steps aside
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

DecisionTag = Literal["BOOST", "PV-DUMP", "COAST", "NORMAL", "NEG-PRICE", "OVERRIDE"]


@dataclass
class Plan:
    """The optimizer's output for one cycle."""

    tag: DecisionTag
    action: str               # short English label, used by AI / logs
    reason: str               # one-line Dutch sentence for the dashboard
    rationale: str            # longer English explanation incl. the numbers

    boiler_target_temp: float
    dompelaar_on: bool
    heat_pump_allowed: bool
    indoor_setpoint_offset: float = 0.0
    estimated_savings_eur: float | None = None


@dataclass
class StateInput:
    """The minimal slice of SystemState the optimizer reads."""

    timestamp: datetime
    pv_power: float           # W
    house_load: float         # W (excl HP / dompelaar)
    hp_power: float           # W
    dompelaar_on: bool
    boiler_temp: float        # °C
    indoor_temp: float        # °C
    outdoor_temp: float       # °C
    grid_import: float | None # W (positive = importing, negative = exporting)
    price_eur_kwh: float | None  # current spot all-in


@dataclass
class _LimitsView:
    """Layer-1 fields the optimizer cares about."""

    floor_max_flow_c: float
    boiler_legionella_floor_c: float
    boiler_max_c: float
    dompelaar_max_price_eur_kwh: float
    dompelaar_only_with_pv_above_w: float


def _safe_boiler_target(target: float, limits: _LimitsView) -> float:
    """Clamp into [legionella floor, max ceiling]."""
    return max(limits.boiler_legionella_floor_c, min(target, limits.boiler_max_c))


def plan_next_quarter(
    state: StateInput,
    *,
    limits: _LimitsView,
    current_price: float | None,        # €/kWh all-in
    avg_price_today: float | None,      # €/kWh all-in (for "cheap" comparison)
    pv_surplus: float,                  # W = pv_power − house_load (excl. HP)
    overrides: dict[str, Any] | None = None,
) -> Plan:
    """Decide what to do for the next 15-min cycle.

    The function is deterministic and side-effect free — easy to test.
    Caller is responsible for applying the plan and persisting state.
    """
    if overrides:
        # User override is in effect; optimizer steps aside but still
        # returns a Plan so the cycle persists a record of the override.
        kind = next(iter(overrides))
        return Plan(
            tag="OVERRIDE",
            action=f"override:{kind}",
            reason=f"Handmatige override actief ({kind}).",
            rationale=f"User override {kind!r} present in policy; optimizer skipped.",
            boiler_target_temp=_safe_boiler_target(55.0, limits),
            dompelaar_on=state.dompelaar_on,
            heat_pump_allowed=True,
        )

    # ---------------------------------------------------------------
    # 1. Negative wholesale price — dump heat into dompelaar.
    # ---------------------------------------------------------------
    if current_price is not None and current_price < 0:
        return Plan(
            tag="NEG-PRICE",
            action="dump_to_dompelaar",
            reason=(
                f"Negatieve spot ({current_price:.3f} €/kWh) — dompelaar verbrandt "
                f"geld voor je."
            ),
            rationale=(
                f"current_price={current_price:.4f} < 0; dompelaar on, boiler to max."
            ),
            boiler_target_temp=_safe_boiler_target(limits.boiler_max_c, limits),
            dompelaar_on=True,
            heat_pump_allowed=True,
            estimated_savings_eur=0.20,
        )

    # ---------------------------------------------------------------
    # 2. PV surplus large enough to drive dompelaar.
    # ---------------------------------------------------------------
    if pv_surplus >= limits.dompelaar_only_with_pv_above_w:
        return Plan(
            tag="PV-DUMP",
            action="pv_to_storage",
            reason=(
                f"Zonneoverschot {pv_surplus / 1000:.1f} kW — boiler en dompelaar "
                f"vangen het op."
            ),
            rationale=(
                f"pv_surplus={pv_surplus:.0f}W ≥ "
                f"{limits.dompelaar_only_with_pv_above_w:.0f}W threshold; "
                f"dompelaar on, boiler push to {limits.boiler_max_c}°C."
            ),
            boiler_target_temp=_safe_boiler_target(limits.boiler_max_c - 2, limits),
            dompelaar_on=True,
            heat_pump_allowed=True,
        )

    # ---------------------------------------------------------------
    # 3. Cheap hour — boost boiler with heat pump.
    # ---------------------------------------------------------------
    if (
        current_price is not None
        and avg_price_today is not None
        and current_price < avg_price_today * 0.7
    ):
        return Plan(
            tag="BOOST",
            action="boiler_charge",
            reason=(
                f"Goedkoop uur ({current_price:.3f} €/kWh, gemiddeld "
                f"{avg_price_today:.3f}) — boiler laadt op."
            ),
            rationale=(
                f"price {current_price:.4f} < 0.7×avg ({avg_price_today:.4f}); "
                f"boiler target raised."
            ),
            boiler_target_temp=_safe_boiler_target(limits.boiler_max_c - 5, limits),
            dompelaar_on=False,
            heat_pump_allowed=True,
            estimated_savings_eur=0.05,
        )

    # ---------------------------------------------------------------
    # 4. Expensive hour — coast on stored heat.
    # ---------------------------------------------------------------
    if (
        current_price is not None
        and avg_price_today is not None
        and current_price > avg_price_today * 1.3
        and state.boiler_temp >= limits.boiler_legionella_floor_c + 5
    ):
        return Plan(
            tag="COAST",
            action="coast_on_storage",
            reason=(
                f"Duur uur ({current_price:.3f} €/kWh) — coast op opgeslagen warmte."
            ),
            rationale=(
                f"price {current_price:.4f} > 1.3×avg ({avg_price_today:.4f}) and "
                f"boiler {state.boiler_temp:.0f}°C has slack; HP paused."
            ),
            boiler_target_temp=_safe_boiler_target(state.boiler_temp - 2, limits),
            dompelaar_on=False,
            heat_pump_allowed=False,
            estimated_savings_eur=0.05,
        )

    # ---------------------------------------------------------------
    # 5. Default — normal day profile.
    # ---------------------------------------------------------------
    return Plan(
        tag="NORMAL",
        action="default",
        reason="Alles draait op het normale dagprofiel.",
        rationale=(
            f"No special condition: price={current_price}, surplus={pv_surplus:.0f}W."
        ),
        boiler_target_temp=_safe_boiler_target(55.0, limits),
        dompelaar_on=False,
        heat_pump_allowed=True,
    )
