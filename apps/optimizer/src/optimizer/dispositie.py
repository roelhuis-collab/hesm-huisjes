"""
Dispositie-engine — kwartier-besparingsmodule (policy-laag).

Per 15-minuten-kwartier kiest deze module wat er met het verwachte PV-overschot
gebeurt. Vier bestemmingen, gerangschikt naar marginale waarde:

  1. self_consume — verschuifbare last activeren (WeHeat-tapwater, buffer,
     EV-laden, witgoed)
  2. store        — accu laden (indien aanwezig)
  3. export       — terugleveren naar het net (baseline = 0)
  4. curtail      — export-limiting op de omvormer (noodrem)

Twee tariefregimes:
  - 'saldering'    — heel 2026, alleen terugleverstaffel telt. Curtail is hier
    altijd verlies; de engine sluit het uit.
  - 'no_saldering' — vanaf 01-01-2027. Per-kWh terugleverkosten en feed-in
    tariff bepalen exportNetValue; bij negatieve waarde wint curtail van export
    maar verliest nog altijd van self_consume/store.

Constanten (ENERGIEDIRECT_STAFFEL, TARIFF, SITE_CONFIG_DEFAULT) zijn gespiegeld
uit /config/site.config.ts en /config/tariff.energiedirect.ts. Bij contract-
of leverancierwissel houd je beide in sync.

WeHeat is read-only (zie CLAUDE.md / PR6): de DhwLoad-adapter levert
controllable=False zolang er geen bevestigde write-adapter bestaat — engine
schrijft adviezen, schakelt nog niet fysiek.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True)
class StaffelBand:
    """Eén band uit de jaarcumulatieve terugleverstaffel."""

    min: int
    max: int
    cost_per_year: float


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
class TariffConfig:
    """Spiegelt /config/tariff.energiedirect.ts → TARIFF + TARIFF_CONFIG-velden."""

    import_price_eur_per_kwh: float
    feed_in_tariff_saldering_eur_per_kwh: float
    feed_in_cost_2027_eur_per_kwh: float
    feed_in_tariff_2027_eur_per_kwh: float


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
    forecast_surplus_kwh: float
    cum_ytd_teruglevering_kwh: float
    allocations: list[DispositionAllocation]
    expected_saving_eur: float
    rationale: str

    def to_firestore(self) -> dict[str, Any]:
        return {
            "interval_start": self.interval_start,
            "regime": self.regime,
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


# ---------------------------------------------------------------------------
# Energiedirect-staffel + tariefconstanten (gespiegeld uit TS)
# ---------------------------------------------------------------------------


ENERGIEDIRECT_STAFFEL: list[StaffelBand] = [
    StaffelBand(0, 250, 0.00),
    StaffelBand(251, 500, 48.84),
    StaffelBand(501, 750, 81.36),
    StaffelBand(751, 1000, 113.64),
    StaffelBand(1001, 1250, 146.28),
    StaffelBand(1251, 1500, 178.80),
    StaffelBand(1501, 1750, 211.32),
    StaffelBand(1751, 2000, 243.84),
    StaffelBand(2001, 2250, 276.36),
    StaffelBand(2251, 2500, 308.76),
    StaffelBand(2501, 2750, 341.28),
    StaffelBand(2751, 3000, 373.80),
    StaffelBand(3001, 3250, 406.32),
    StaffelBand(3251, 3500, 438.84),
    StaffelBand(3501, 3750, 471.36),
    StaffelBand(3751, 4000, 503.76),
    StaffelBand(4001, 4250, 536.28),
    StaffelBand(4251, 4500, 568.80),
    StaffelBand(4501, 4750, 601.32),
    StaffelBand(4751, 5000, 633.84),
    StaffelBand(5001, 5250, 666.24),
    StaffelBand(5251, 5500, 698.76),
    StaffelBand(5501, 5750, 731.28),
    StaffelBand(5751, 6000, 763.80),
    StaffelBand(6001, 6250, 796.32),
    StaffelBand(6251, 6500, 828.84),
    StaffelBand(6501, 6750, 861.24),
    StaffelBand(6751, 7000, 893.76),
    StaffelBand(7001, 7250, 926.28),
    StaffelBand(7251, 7500, 958.80),
    StaffelBand(7501, 7750, 991.32),
    StaffelBand(7751, 8000, 1023.84),
    StaffelBand(8001, 8250, 1056.24),
    StaffelBand(8251, 8500, 1088.76),
    StaffelBand(8501, 8750, 1121.28),
    StaffelBand(8751, 9000, 1153.80),
    StaffelBand(9001, 9250, 1186.32),
    StaffelBand(9251, 9500, 1218.84),
    StaffelBand(9501, 9750, 1251.24),
    StaffelBand(9751, 10000, 1283.76),
    StaffelBand(10001, 9_999_999, 1332.48),
]

TARIFF = TariffConfig(
    import_price_eur_per_kwh=0.23209,
    feed_in_tariff_saldering_eur_per_kwh=0.15 * 1.21,
    feed_in_cost_2027_eur_per_kwh=0.078,
    feed_in_tariff_2027_eur_per_kwh=0.06,
)

# Spiegelt SITE_CONFIG uit /config/site.config.ts (Kempenstraat 3, 6137 KL Sittard).
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
# Pure functies — staffel-rekenwerk
# ---------------------------------------------------------------------------


def staffel_cost_at(cum_kwh: float, staffel: list[StaffelBand]) -> float:
    """Totale jaar-staffelkost voor `cum_kwh` teruggeleverde kWh."""
    band = _find_band(cum_kwh, staffel)
    return band.cost_per_year


def marginal_staffel_cost(cum_kwh: float, staffel: list[StaffelBand]) -> float:
    """Marginale kost van één extra teruggeleverde kWh op `cum_kwh` cumulatief.

    Bij Energiedirect (vlakke €32,52 per 250-kWh-band) levert dit ≈ €0,13008/kWh.
    De berekening is generiek zodat ongelijke banden bij andere leveranciers ook
    correct verwerkt worden.
    """
    band = _find_band(cum_kwh, staffel)
    next_band = _next_band(band, staffel)
    if next_band is None:
        return 0.0
    width = band.max - band.min + 1
    step = next_band.cost_per_year - band.cost_per_year
    if step <= 0 or width <= 0:
        return 0.0
    return step / width


def _find_band(cum_kwh: float, staffel: list[StaffelBand]) -> StaffelBand:
    for band in staffel:
        if band.min <= cum_kwh <= band.max:
            return band
    return staffel[-1]


def _next_band(current: StaffelBand, staffel: list[StaffelBand]) -> StaffelBand | None:
    for band in staffel:
        if band.min > current.max:
            return band
    return None


# ---------------------------------------------------------------------------
# Hulp-helpers
# ---------------------------------------------------------------------------


def battery_charge_room_kwh(battery: BatteryConfig, soc_kwh: float, interval_minutes: int = 15) -> float:
    """Hoeveel kWh kan de accu dit kwartier nog opnemen?

    Beperkt door zowel headroom (usableKwh − soc) als laadvermogen × tijd.
    """
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
    staffel: list[StaffelBand] = field(default_factory=lambda: ENERGIEDIRECT_STAFFEL)
    interval_minutes: int = 15


def _gain_per_disposition(
    disposition: Disposition,
    cfg: EngineConfig,
    staffel_marg: float,
    export_net: float,
) -> float:
    import_p = cfg.tariff.import_price_eur_per_kwh
    rte = cfg.site.battery.round_trip_efficiency if cfg.site.battery else 0.9

    if cfg.regime == "saldering":
        if disposition is Disposition.SELF_CONSUME:
            return staffel_marg
        if disposition is Disposition.STORE:
            return staffel_marg * rte
        if disposition is Disposition.EXPORT:
            return 0.0
        if disposition is Disposition.CURTAIL:
            # Onder saldering: gooi je saldeerwaarde weg om alleen staffel te besparen → altijd negatief.
            return -(import_p - staffel_marg)
    else:
        if disposition is Disposition.SELF_CONSUME:
            return import_p - export_net
        if disposition is Disposition.STORE:
            return import_p * rte - export_net
        if disposition is Disposition.EXPORT:
            return 0.0
        if disposition is Disposition.CURTAIL:
            # Positief zodra export_net < 0 (negatieve terugleververgoeding).
            return -export_net
    raise ValueError(f"Unknown disposition: {disposition!r}")


def _build_rationale(
    allocations: list[DispositionAllocation],
    regime: TariffRegime,
    export_net: float,
) -> str:
    if not allocations:
        return "Geen surplus dit kwartier — niets te alloceren."
    parts: list[str] = []
    for a in allocations:
        label = a.disposition.value
        if a.load_id:
            label = f"{label}:{a.load_id}"
        parts.append(f"{label} {a.kwh:.3f} kWh @ +€{a.marginal_gain_eur_per_kwh:.3f}/kWh")
    regime_label = "saldering" if regime == "saldering" else f"no_saldering (exportNet={export_net:+.3f})"
    return f"[{regime_label}] " + " → ".join(parts)


def decide(
    interval_start: str,
    forecast_surplus_kwh: float,
    loads: list[DeferrableLoad],
    state: EngineState,
    cfg: EngineConfig,
) -> DispositionDecision:
    """Beslis voor één kwartier waar het PV-overschot heen moet.

    Greedy: sorteer kandidaten op marginale winst t.o.v. terugleveren en vul tot
    `forecast_surplus_kwh` op is.
    """
    staffel_marg = marginal_staffel_cost(state.cum_ytd_teruglevering_kwh, cfg.staffel)
    export_net = (
        0.0
        if cfg.regime == "saldering"
        else cfg.tariff.feed_in_tariff_2027_eur_per_kwh - cfg.tariff.feed_in_cost_2027_eur_per_kwh
    )

    candidates: list[_Candidate] = []

    for load in loads:
        if not load.controllable or load.available_kwh <= 0:
            continue
        candidates.append(
            _Candidate(
                disposition=Disposition.SELF_CONSUME,
                capacity_kwh=load.available_kwh,
                gain=_gain_per_disposition(Disposition.SELF_CONSUME, cfg, staffel_marg, export_net),
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
                    gain=_gain_per_disposition(Disposition.STORE, cfg, staffel_marg, export_net),
                )
            )

    candidates.append(
        _Candidate(
            disposition=Disposition.EXPORT,
            capacity_kwh=export_room_kwh(cfg.site, cfg.interval_minutes),
            gain=_gain_per_disposition(Disposition.EXPORT, cfg, staffel_marg, export_net),
        )
    )
    candidates.append(
        _Candidate(
            disposition=Disposition.CURTAIL,
            capacity_kwh=float("inf"),
            gain=_gain_per_disposition(Disposition.CURTAIL, cfg, staffel_marg, export_net),
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
        forecast_surplus_kwh=_round3(forecast_surplus_kwh),
        cum_ytd_teruglevering_kwh=_round3(state.cum_ytd_teruglevering_kwh),
        allocations=allocations,
        expected_saving_eur=_round2(expected_saving),
        rationale=_build_rationale(allocations, cfg.regime, export_net),
    )


__all__ = [
    "ENERGIEDIRECT_STAFFEL",
    "SITE_CONFIG_DEFAULT",
    "TARIFF",
    "BatteryConfig",
    "DeferrableLoad",
    "Disposition",
    "DispositionAllocation",
    "DispositionDecision",
    "EngineConfig",
    "EngineState",
    "EvChargerConfig",
    "HeatPumpConfig",
    "LoadProvider",
    "SiteConfig",
    "StaffelBand",
    "SurplusForecastProvider",
    "TariffConfig",
    "TariffRegime",
    "battery_charge_room_kwh",
    "decide",
    "export_room_kwh",
    "marginal_staffel_cost",
    "staffel_cost_at",
]


# Silence unused-import warnings for asdict (kept available for downstream callers).
_ = asdict
