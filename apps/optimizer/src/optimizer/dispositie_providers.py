"""
v1-implementaties van SurplusForecastProvider, LoadProvider en SpotPriceProvider.

* Spot: EnergyZero publieke kwartierfeed (zit onder Zonneplan dynamisch).
  Geeft kale spot (excl. btw). Engine telt zelf Zonneplan-opslag +
  energiebelasting erbij.
* Surplus: HomeWizard P1 ``active_power_w`` als live bron (negatief = export).
  Bij staleness > 30 s of ontbrekende P1 valt-ie terug op een Growatt-
  afgeleide schatting (PV − basislast). Cycle markeert de beslissing dan
  als ``safe_mode=True`` zodat een toekomstige actuatie-laag weet niet te
  schakelen.
* Lasten: opgebouwd uit ``SiteConfig`` en respecteert de ``controllable``-vlag —
  de WeHeat heeft geen bevestigde write-adapter, dus DHW en buffer-overheat
  blijven ``controllable=False``.

Architectuur-noot: HomeWizard publiceert geen cloud-API (zie
``infra/SETUP.md`` voor de tunnel-/push-agent-keuze). Deze PR levert de
**provider**; de tunnel zelf is buiten scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from src.connectors.base import ConnectorError
from src.connectors.energyzero import EnergyZeroClient
from src.optimizer.dispositie import (
    DeferrableLoad,
    SiteConfig,
)

log = logging.getLogger(__name__)

DEFAULT_P1_MAX_AGE_SECONDS: float = 30.0
SurplusSource = Literal["p1", "growatt_fallback", "no_data"]


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


@dataclass(frozen=True)
class SurplusSnapshot:
    """Wat de surplus-keuze voor dit kwartier wordt + waarom.

    ``stale`` betekent: P1 was er, maar te oud. We vallen dan terug op een
    Growatt-afgeleide schatting EN markeren de beslissing als safe_mode in de
    cycle-laag.
    """

    surplus_kwh: float
    source: SurplusSource
    p1_age_seconds: float | None
    stale: bool


def live_surplus_kwh(active_power_w: float, *, interval_minutes: int = 15) -> float:
    """P1 sign-conventie: ``active_power_w`` negatief = export, positief = import.

    Live overschot = max(0, -active_power_w) × interval / 1000.
    """
    surplus_w = max(0.0, -active_power_w)
    return round(surplus_w * (interval_minutes / 60.0) / 1000.0, 4)


def build_surplus_snapshot(
    *,
    p1_active_power_w: float | None,
    p1_captured_at: datetime | None,
    pv_power_w: float,
    house_load_w: float,
    now: datetime,
    max_age_seconds: float = DEFAULT_P1_MAX_AGE_SECONDS,
    interval_minutes: int = 15,
) -> SurplusSnapshot:
    """Kies P1 (live) of Growatt (fallback) als surplus-bron voor dit kwartier.

    Beslisboom:

      1. P1 aanwezig en ≤ ``max_age_seconds`` oud → live surplus, ``source='p1'``.
      2. P1 aanwezig maar te oud → Growatt-fallback, ``stale=True``.
      3. P1 ontbreekt → Growatt-fallback. ``source='no_data'`` als ook PV ≤ 0.

    De ``stale=True`` op pad 2 is het signaal voor de cycle om safe_mode te zetten.
    """
    fallback = quarter_forecast_kwh(pv_power_w, house_load_w, interval_minutes=interval_minutes)

    if p1_active_power_w is not None and p1_captured_at is not None:
        captured = p1_captured_at if p1_captured_at.tzinfo else p1_captured_at.replace(tzinfo=UTC)
        moment = now if now.tzinfo else now.replace(tzinfo=UTC)
        age = max(0.0, (moment - captured).total_seconds())
        if age <= max_age_seconds:
            return SurplusSnapshot(
                surplus_kwh=live_surplus_kwh(p1_active_power_w, interval_minutes=interval_minutes),
                source="p1",
                p1_age_seconds=age,
                stale=False,
            )
        return SurplusSnapshot(
            surplus_kwh=fallback,
            source="growatt_fallback",
            p1_age_seconds=age,
            stale=True,
        )

    if pv_power_w <= 0:
        return SurplusSnapshot(
            surplus_kwh=0.0,
            source="no_data",
            p1_age_seconds=None,
            stale=True,
        )
    return SurplusSnapshot(
        surplus_kwh=fallback,
        source="growatt_fallback",
        p1_age_seconds=None,
        stale=True,
    )


class EnergyZeroSpotPriceProvider:
    """SpotPriceProvider die de publieke EnergyZero-kwartierfeed gebruikt.

    Levert de KALE day-ahead-spotprijs (€/kWh, excl. btw en opslagen) voor
    het kwartier waar het interval in valt. Geeft ``None`` zodra de prijs
    niet (meer) te halen is — de cycle-laag schakelt dan naar safe mode
    en de engine schrijft alleen advies.

    Caching gebeurt door de onderliggende ``EnergyZeroClient`` (per
    Europe/Amsterdam-dag, in-memory). Een instantie deelt zijn cache
    over alle aanroepen binnen één Cloud Run-process.
    """

    def __init__(self, client: EnergyZeroClient | None = None) -> None:
        self._client = client

    async def forecast(self, interval_start: str) -> float | None:
        when = datetime.fromisoformat(interval_start)
        owns_client = self._client is None
        client = self._client or EnergyZeroClient.from_env()
        try:
            async with client if owns_client else _NoopAsyncContext(client):
                price = await client.quarter_price_for(when)
        except ConnectorError as exc:
            log.warning("energyzero: spot lookup faalde: %s", exc)
            return None
        return None if price is None else price.spot_eur_kwh


class _NoopAsyncContext:
    """Wrappert een al-geopende client zonder hem te sluiten bij __aexit__."""

    def __init__(self, client: EnergyZeroClient) -> None:
        self._client = client

    async def __aenter__(self) -> EnergyZeroClient:
        return self._client

    async def __aexit__(self, *exc_info: object) -> None:
        return None


__all__ = [
    "DEFAULT_P1_MAX_AGE_SECONDS",
    "EnergyZeroSpotPriceProvider",
    "SurplusSnapshot",
    "SurplusSource",
    "build_loads_for_interval",
    "build_surplus_snapshot",
    "live_surplus_kwh",
    "quarter_forecast_kwh",
]
