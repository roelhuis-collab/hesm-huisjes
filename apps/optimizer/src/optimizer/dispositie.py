"""
Dispositie-engine — kwartier-besparingsmodule (policy-laag), spot-gedreven.

Per 15-minuten-kwartier kiest deze module wat er met het verwachte PV-overschot
gebeurt. Vier bestemmingen, gerangschikt naar marginale waarde:

  1. self_consume — verschuifbare last activeren (WeHeat-tapwater, buffer,
     EV-laden, witgoed)
  2. store        — accu laden (indien aanwezig)
  3. export       — terugleveren naar het net (baseline = 0)
  4. curtail      — export-limiting op de omvormer (noodrem)

Economisch model — Zonneplan dynamisch (zie config/site.config.ts TARIFF_CONFIG):

    import_price(t) = spot(t) + inkoopvergoeding + energy_tax
    export_value(t) = spot(t) + terugleveropslag
                      + (overdag & (spot+opslag)>0 & cum_ytd_export<cap
                         ? zonnebonus_pct × spot : 0)
                      + (saldering.active ? energy_tax : 0)

Onder saldering (t/m 2026) krijg je de energiebelasting terug op je export
binnen het saldeerbereik — de self_consume-winst is dan slechts de
Zonneplan-inkoopvergoeding (~€0,025/kWh). Per 01-01-2027 vervalt die
energy_tax-term en stijgt self_consume naar ~€0,16/kWh (energy_tax +
inkoopvergoeding). De Zonnebonus is een Zonneplan-bonus van +10% over de
spot, alleen overdag, alleen bij positieve (spot+opslag), capped op 7.500 kWh
teruglevering per kalenderjaar.

config/tariff.energiedirect.ts blijft als historische referentie — niet meer
gebruikt door de engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

# ---------------------------------------------------------------------------
# Datamodel
# ---------------------------------------------------------------------------


class Disposition(StrEnum):
    SELF_CONSUME = "self_consume"
    STORE = "store"
    EXPORT = "export"
    CURTAIL = "curtail"


TariffRegime = Literal["saldering", "no_saldering"]
ContractType = Literal["dynamic", "fixed"]


@dataclass(frozen=True)
class BatteryConfig:
    usable_kwh: float
    max_charge_kw: float
    round_trip_efficiency: float


@dataclass(frozen=True)
class EvChargerConfig:
    max_kw: float
    home_daytime_probability: float


@dataclass(frozen=True)
class HeatPumpConfig:
    model: str
    annual_electric_kwh: float
    dhw_shiftable_kwh_per_day: float
    controllable: bool


@dataclass(frozen=True)
class SiteConfig:
    """Spiegelt /config/site.config.ts → SITE_CONFIG."""

    pv_kwp: float
    annual_pv_yield_kwh: float
    inverter: str
    meter: str
    heat_pump: HeatPumpConfig
    buffer_overheat_kwh_per_day: float
    battery: BatteryConfig | None
    ev_charger: EvChargerConfig | None
    export_limit_kw: float | None


@dataclass(frozen=True)
class SalderingConfig:
    """Saldering-statusvlag. De datum-switch zelf zit in ``regime_for()``."""

    active: bool = True
    until_date: str = "2027-01-01"


@dataclass(frozen=True)
class TariffConfig:
    """Spiegelt /config/site.config.ts → TARIFF_CONFIG (Zonneplan dynamisch).

    Alle bedragen incl. 21% btw. ``spot(t)`` komt per kwartier van een
    ``SpotPriceProvider`` en wordt nooit hier opgeslagen.
    """

    inkoopvergoeding_eur_per_kwh: float
    energy_tax_eur_per_kwh: float
    terugleveropslag_eur_per_kwh: float
    zonnebonus_cap_kwh: float
    zonnebonus_percentage: float
    zonnebonus_start_hour: int
    zonnebonus_end_hour: int
    saldering: SalderingConfig = field(default_factory=SalderingConfig)
    contract_type: ContractType = "dynamic"
    supplier: str = "zonneplan"


@dataclass
class DeferrableLoad:
    """Verschuifbare last dit kwartier (witgoed, tapwater, EV, ...)."""

    id: str
    label: str
    available_kwh: float
    controllable: bool


@dataclass
class DispositionAllocation:
    disposition: Disposition
    kwh: float
    marginal_gain_eur_per_kwh: float
    load_id: str | None = None


@dataclass
class DispositionDecision:
    interval_start: str
    regime: TariffRegime
    spot_price_eur_per_kwh: float
    forecast_surplus_kwh: float
    cum_ytd_teruglevering_kwh: float
    allocations: list[DispositionAllocation]
    expected_saving_eur: float
    rationale: str

    def to_firestore(self) -> dict[str, Any]:
        return {
            "interval_start": self.interval_start,
            "regime": self.regime,
            "spot_price_eur_per_kwh": self.spot_price_eur_per_kwh,
            "forecast_surplus_kwh": self.forecast_surplus_kwh,
            "cum_ytd_teruglevering_kwh": self.cum_ytd_teruglevering_kwh,
            "allocations": [
                {
                    "disposition": a.disposition.value,
                    "load_id": a.load_id,
                    "kwh": a.kwh,
                    "marginal_gain_eur_per_kwh": a.marginal_gain_eur_per_kwh,
                }
                for a in self.allocations
            ],
            "expected_saving_eur": self.expected_saving_eur,
            "rationale": self.rationale,
        }


@dataclass
class EngineState:
    """Live state read door de engine bij elke kwartier-tick."""

    cum_ytd_teruglevering_kwh: float
    battery_soc_kwh: float | None = None


# ---------------------------------------------------------------------------
# Provider protocols (modulair; v1-implementaties in connectors/)
# ---------------------------------------------------------------------------


class SurplusForecastProvider(Protocol):
    """Verwacht PV-overschot voor het komende kwartier (kWh)."""

    async def forecast(self, interval_start: str) -> float: ...


class LoadProvider(Protocol):
    """Beschikbare verschuifbare lasten voor het komende kwartier."""

    async def available_loads(self, interval_start: str) -> list[DeferrableLoad]: ...


class SpotPriceProvider(Protocol):
    """Kale EPEX day-ahead spot-prijs voor het kwartier (€/kWh, incl. niets)."""

    async def forecast(self, interval_start: str) -> float: ...


class FlatDayNightSpotPriceProvider:
    """Stub-implementatie: vlak dag/nacht-profiel.

    Geeft een dag-tarief tijdens 08:00–22:00 en een nacht-tarief daarbuiten.
    Bedoeld voor ontwikkelen + smoke-tests tot de echte ENTSO-E-koppeling
    landt — dan kan deze in een aparte PR vervangen worden door een
    ``EntsoeSpotPriceProvider`` die de bestaande ``entsoe_client`` aanroept
    en de all-in-conversie OVERSLAAT (we willen hier de kale spot, niet
    het VAT-inclusieve retail-tarief).

    TODO PR15: vervang door echte EPEX-koppeling (ENTSO-E day-ahead, A44/A01,
    NL-domein 10YNL----------L). De entsoe-connector geeft nu all-in;
    we hebben hier ``spot/1000`` nodig (kale €/kWh, vóór belasting en btw).
    """

    def __init__(
        self,
        day_eur_per_kwh: float = 0.10,
        night_eur_per_kwh: float = 0.05,
        day_start_hour: int = 8,
        day_end_hour: int = 22,
    ) -> None:
        self._day = day_eur_per_kwh
        self._night = night_eur_per_kwh
        self._day_start = day_start_hour
        self._day_end = day_end_hour

    async def forecast(self, interval_start: str) -> float:
        hour = datetime.fromisoformat(interval_start).hour
        if self._day_start <= hour < self._day_end:
            return self._day
        return self._night


# ---------------------------------------------------------------------------
# Tarief- + sitespiegels (waarden uit /config/site.config.ts)
# ---------------------------------------------------------------------------


REGIME_SWITCH_DATE = date(2027, 1, 1)


def regime_for(when: date | datetime) -> TariffRegime:
    """Saldering vervalt per 01-01-2027 (ACM-besluit). Datum-gestuurde switch."""
    if isinstance(when, datetime):
        when = when.date()
    return "saldering" if when < REGIME_SWITCH_DATE else "no_saldering"


# Zonneplan dynamisch — incl. btw, bron: config/site.config.ts → TARIFF_CONFIG.
TARIFF = TariffConfig(
    inkoopvergoeding_eur_per_kwh=0.025,
    energy_tax_eur_per_kwh=0.1316,
    terugleveropslag_eur_per_kwh=0.0,
    zonnebonus_cap_kwh=7500.0,
    zonnebonus_percentage=0.10,
    zonnebonus_start_hour=10,
    zonnebonus_end_hour=15,
    saldering=SalderingConfig(active=True, until_date="2027-01-01"),
    contract_type="dynamic",
    supplier="zonneplan",
)

SITE_CONFIG_DEFAULT = SiteConfig(
    pv_kwp=10.53,
    annual_pv_yield_kwh=10_500,
    inverter="growatt",
    meter="ziv-esmr5",
    heat_pump=HeatPumpConfig(
        model="weheat-blackbird-p80",
        annual_electric_kwh=3_800,
        dhw_shiftable_kwh_per_day=4.0,
        controllable=False,  # WeHeat write-adapter nog niet bevestigd; engine schrijft adviezen.
    ),
    buffer_overheat_kwh_per_day=3.0,
    battery=None,
    ev_charger=None,
    export_limit_kw=None,
)


# ---------------------------------------------------------------------------
# Prijsformules
# ---------------------------------------------------------------------------


def import_price(spot_eur_per_kwh: float, tariff: TariffConfig) -> float:
    """All-in invoerprijs: kale spot + Zonneplan-opslag + energiebelasting (incl. btw)."""
    return spot_eur_per_kwh + tariff.inkoopvergoeding_eur_per_kwh + tariff.energy_tax_eur_per_kwh


def export_value(
    spot_eur_per_kwh: float,
    *,
    interval_hour: int,
    cum_ytd_teruglevering_kwh: float,
    regime: TariffRegime,
    tariff: TariffConfig,
) -> float:
    """Waarde van één teruggeleverde kWh, incl. Zonnebonus en saldering-restitutie.

    Componenten:
      - basis             = spot + terugleveropslag
      - + Zonnebonus      = zonnebonus_pct × spot, alleen overdag, alleen wanneer
                            basis > 0 én cum YTD < cap
      - + energy_tax      = alleen wanneer regime=='saldering' (saldeerbereik t/m 2026)
    """
    base = spot_eur_per_kwh + tariff.terugleveropslag_eur_per_kwh

    daytime = tariff.zonnebonus_start_hour <= interval_hour < tariff.zonnebonus_end_hour
    under_cap = cum_ytd_teruglevering_kwh < tariff.zonnebonus_cap_kwh
    bonus = (
        tariff.zonnebonus_percentage * spot_eur_per_kwh
        if daytime and under_cap and base > 0
        else 0.0
    )

    saldering_tax = tariff.energy_tax_eur_per_kwh if regime == "saldering" else 0.0

    return base + bonus + saldering_tax


# ---------------------------------------------------------------------------
# Hulp-helpers
# ---------------------------------------------------------------------------


def battery_charge_room_kwh(battery: BatteryConfig, soc_kwh: float, interval_minutes: int = 15) -> float:
    """Hoeveel kWh kan de accu dit kwartier nog opnemen?"""
    headroom = max(0.0, battery.usable_kwh - soc_kwh)
    max_charge_kwh = battery.max_charge_kw * (interval_minutes / 60.0)
    return min(headroom, max_charge_kwh)


def export_room_kwh(site: SiteConfig, interval_minutes: int = 15) -> float:
    """Hoeveel kWh kan dit kwartier worden teruggeleverd? Inf als geen limiet."""
    if site.export_limit_kw is None:
        return float("inf")
    return site.export_limit_kw * (interval_minutes / 60.0)


def _round3(value: float) -> float:
    return round(value, 3)


def _round2(value: float) -> float:
    return round(value, 2)


# ---------------------------------------------------------------------------
# Engine — greedy marginale-waarde-allocatie
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    disposition: Disposition
    capacity_kwh: float
    gain: float
    load_id: str | None = None


@dataclass
class EngineConfig:
    regime: TariffRegime
    site: SiteConfig
    tariff: TariffConfig
    interval_minutes: int = 15


def _gain_per_disposition(
    disposition: Disposition,
    *,
    import_p: float,
    export_v: float,
    rte: float,
) -> float:
    """Marginale winst t.o.v. terugleveren (= baseline = 0)."""
    if disposition is Disposition.SELF_CONSUME:
        return import_p - export_v
    if disposition is Disposition.STORE:
        return import_p * rte - export_v
    if disposition is Disposition.EXPORT:
        return 0.0
    if disposition is Disposition.CURTAIL:
        # Positief zodra export_v < 0 (negatieve marktprijs onder saldering of no_saldering).
        return -export_v
    raise ValueError(f"Unknown disposition: {disposition!r}")


def _build_rationale(
    allocations: list[DispositionAllocation],
    regime: TariffRegime,
    spot: float,
    export_v: float,
) -> str:
    regime_label = "saldering" if regime == "saldering" else "no_saldering"
    head = f"[{regime_label}] spot={spot:+.3f} exportValue={export_v:+.3f}"
    if not allocations:
        return f"{head} — geen surplus."
    parts: list[str] = []
    for a in allocations:
        label = a.disposition.value
        if a.load_id:
            label = f"{label}:{a.load_id}"
        parts.append(f"{label} {a.kwh:.3f} kWh @ +€{a.marginal_gain_eur_per_kwh:.3f}/kWh")
    return f"{head} — " + " → ".join(parts)


def decide(
    interval_start: str,
    forecast_surplus_kwh: float,
    loads: list[DeferrableLoad],
    state: EngineState,
    cfg: EngineConfig,
    *,
    spot_price_eur_per_kwh: float,
) -> DispositionDecision:
    """Beslis voor één kwartier waar het PV-overschot heen moet.

    Greedy: sorteer kandidaten op marginale winst t.o.v. terugleveren en vul tot
    ``forecast_surplus_kwh`` op is.
    """
    interval_hour = datetime.fromisoformat(interval_start).hour
    rte = cfg.site.battery.round_trip_efficiency if cfg.site.battery else 0.9

    import_p = import_price(spot_price_eur_per_kwh, cfg.tariff)
    export_v = export_value(
        spot_price_eur_per_kwh,
        interval_hour=interval_hour,
        cum_ytd_teruglevering_kwh=state.cum_ytd_teruglevering_kwh,
        regime=cfg.regime,
        tariff=cfg.tariff,
    )

    candidates: list[_Candidate] = []

    for load in loads:
        if not load.controllable or load.available_kwh <= 0:
            continue
        candidates.append(
            _Candidate(
                disposition=Disposition.SELF_CONSUME,
                capacity_kwh=load.available_kwh,
                gain=_gain_per_disposition(
                    Disposition.SELF_CONSUME,
                    import_p=import_p,
                    export_v=export_v,
                    rte=rte,
                ),
                load_id=load.id,
            )
        )

    if cfg.site.battery is not None:
        soc = state.battery_soc_kwh if state.battery_soc_kwh is not None else 0.0
        room = battery_charge_room_kwh(cfg.site.battery, soc, cfg.interval_minutes)
        if room > 0:
            candidates.append(
                _Candidate(
                    disposition=Disposition.STORE,
                    capacity_kwh=room,
                    gain=_gain_per_disposition(
                        Disposition.STORE,
                        import_p=import_p,
                        export_v=export_v,
                        rte=rte,
                    ),
                )
            )

    candidates.append(
        _Candidate(
            disposition=Disposition.EXPORT,
            capacity_kwh=export_room_kwh(cfg.site, cfg.interval_minutes),
            gain=_gain_per_disposition(
                Disposition.EXPORT, import_p=import_p, export_v=export_v, rte=rte
            ),
        )
    )
    candidates.append(
        _Candidate(
            disposition=Disposition.CURTAIL,
            capacity_kwh=float("inf"),
            gain=_gain_per_disposition(
                Disposition.CURTAIL, import_p=import_p, export_v=export_v, rte=rte
            ),
        )
    )

    # Greedy: hoogste marginale winst eerst. Negatieve-gain-kandidaten doen alleen
    # mee als al het andere op is — vandaar dat we niet vroegtijdig filteren.
    candidates.sort(key=lambda c: c.gain, reverse=True)

    allocations: list[DispositionAllocation] = []
    remaining = forecast_surplus_kwh
    for cand in candidates:
        if remaining <= 1e-6:
            break
        if cand.capacity_kwh <= 0:
            continue
        take = min(remaining, cand.capacity_kwh)
        if take <= 0:
            continue
        allocations.append(
            DispositionAllocation(
                disposition=cand.disposition,
                load_id=cand.load_id,
                kwh=_round3(take),
                marginal_gain_eur_per_kwh=_round3(cand.gain),
            )
        )
        remaining -= take

    expected_saving = sum(a.kwh * a.marginal_gain_eur_per_kwh for a in allocations)

    return DispositionDecision(
        interval_start=interval_start,
        regime=cfg.regime,
        spot_price_eur_per_kwh=_round3(spot_price_eur_per_kwh),
        forecast_surplus_kwh=_round3(forecast_surplus_kwh),
        cum_ytd_teruglevering_kwh=_round3(state.cum_ytd_teruglevering_kwh),
        allocations=allocations,
        expected_saving_eur=_round2(expected_saving),
        rationale=_build_rationale(allocations, cfg.regime, spot_price_eur_per_kwh, export_v),
    )


__all__ = [
    "REGIME_SWITCH_DATE",
    "SITE_CONFIG_DEFAULT",
    "TARIFF",
    "BatteryConfig",
    "ContractType",
    "DeferrableLoad",
    "Disposition",
    "DispositionAllocation",
    "DispositionDecision",
    "EngineConfig",
    "EngineState",
    "EvChargerConfig",
    "FlatDayNightSpotPriceProvider",
    "HeatPumpConfig",
    "LoadProvider",
    "SalderingConfig",
    "SiteConfig",
    "SpotPriceProvider",
    "SurplusForecastProvider",
    "TariffConfig",
    "TariffRegime",
    "battery_charge_room_kwh",
    "decide",
    "export_room_kwh",
    "export_value",
    "import_price",
    "regime_for",
]


# Silence unused-import warnings for asdict (kept available for downstream callers).
_ = asdict
