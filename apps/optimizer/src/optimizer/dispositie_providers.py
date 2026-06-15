"""
v1-implementaties van SurplusForecastProvider en LoadProvider.

Pragmatisch — geen weersbias of irradiantie-model. Schalen op de actuele
PV-output (Growatt) en een vast basislastprofiel. Voor verfijning (Solcast,
weers-bias, geleerde basislast) komen aparte PR's later.

Lasten worden opgebouwd uit ``SiteConfig`` en gerespecteren de
``controllable``-vlag — de WeHeat heeft geen bevestigde write-adapter, dus
DHW en buffer-overheat blijven ``controllable=False`` totdat dat omslaat
in site.config.ts.
"""

from __future__ import annotations

from datetime import datetime

from src.optimizer.dispositie import (
    DeferrableLoad,
    SiteConfig,
)


def quarter_forecast_kwh(
    pv_power_w: float,
    house_load_w: float,
    *,
    interval_minutes: int = 15,
) -> float:
    """Pragmatische v1: surplus = max(0, PV − basislast) over één kwartier."""
    surplus_w = max(0.0, pv_power_w - house_load_w)
    return round(surplus_w * (interval_minutes / 60.0) / 1000.0, 4)


def build_loads_for_interval(
    site: SiteConfig,
    interval_start: datetime,
    *,
    is_sunny: bool = True,
    interval_minutes: int = 15,
) -> list[DeferrableLoad]:
    """Bouw verschuifbare lasten voor één kwartier vanuit SiteConfig.

    De dag-budgetten (``dhwShiftableKwhPerDay``, ``bufferOverheatKwhPerDay``)
    worden ruwweg verdeeld over de daluren (08:00–17:00, 9 uur = 36 kwartieren).
    Buffer-overheat alleen op zonnige dagen. EV en accu alleen als de
    bijbehorende config niet null is.
    """
    quarters_per_day_window = max(1, (17 - 8) * 60 // interval_minutes)
    loads: list[DeferrableLoad] = []

    hour = interval_start.hour
    in_solar_window = 8 <= hour < 17

    if in_solar_window:
        dhw_kwh = site.heat_pump.dhw_shiftable_kwh_per_day / quarters_per_day_window
        loads.append(
            DeferrableLoad(
                id="weheat_dhw",
                label="WeHeat tapwater",
                available_kwh=round(dhw_kwh, 4),
                controllable=site.heat_pump.controllable,
            )
        )

        if is_sunny and site.buffer_overheat_kwh_per_day > 0:
            buffer_kwh = site.buffer_overheat_kwh_per_day / quarters_per_day_window
            loads.append(
                DeferrableLoad(
                    id="buffer_overheat",
                    label="Buffer overheat",
                    available_kwh=round(buffer_kwh, 4),
                    # Buffer-overheat hangt aan WeHeat-aansturing → zelfde gating.
                    controllable=site.heat_pump.controllable,
                )
            )

    if site.ev_charger is not None:
        ev_kwh = site.ev_charger.max_kw * (interval_minutes / 60.0)
        loads.append(
            DeferrableLoad(
                id="ev_charger",
                label="EV-lader",
                available_kwh=round(ev_kwh, 4),
                controllable=True,  # eigen EVSE met directe schakeling
            )
        )

    return loads


__all__ = ["build_loads_for_interval", "quarter_forecast_kwh"]
