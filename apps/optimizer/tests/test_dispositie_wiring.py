"""Tests voor regime-switch, providers, Firestore-persistentie + cum YTD-teller."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from src.optimizer.dispositie import (
    SITE_CONFIG_DEFAULT,
    Disposition,
    DispositionAllocation,
    DispositionDecision,
    regime_for,
)
from src.optimizer.dispositie_providers import (
    build_loads_for_interval,
    quarter_forecast_kwh,
)
from src.state.firestore import (
    get_cum_ytd_teruglevering,
    get_recent_disposition_decisions,
    save_disposition_decision,
)

from tests.fake_firestore import FakeFirestore

# ---------------------------------------------------------------------------
# Regime-switch (saldering → no_saldering op 01-01-2027)
# ---------------------------------------------------------------------------


def test_regime_is_saldering_before_2027() -> None:
    assert regime_for(date(2026, 12, 31)) == "saldering"
    assert regime_for(datetime(2026, 6, 15, 12, 0)) == "saldering"


def test_regime_flips_to_no_saldering_on_1_jan_2027() -> None:
    assert regime_for(date(2027, 1, 1)) == "no_saldering"
    assert regime_for(datetime(2027, 1, 1, 0, 0, 0)) == "no_saldering"
    assert regime_for(date(2028, 6, 1)) == "no_saldering"


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def test_quarter_forecast_kwh_clips_at_zero() -> None:
    """Geen surplus als basislast PV overschrijdt."""
    assert quarter_forecast_kwh(pv_power_w=400.0, house_load_w=600.0) == 0.0


def test_quarter_forecast_kwh_converts_kw_to_kwh_per_quarter() -> None:
    """4000 W surplus × 0.25 h = 1.0 kWh."""
    assert quarter_forecast_kwh(pv_power_w=4600.0, house_load_w=600.0) == pytest.approx(1.0, abs=1e-3)


def test_build_loads_outside_solar_window_returns_empty() -> None:
    """Vóór 08:00 / na 17:00: geen verschuifbare lasten."""
    early = datetime(2026, 6, 15, 6, 0)
    loads = build_loads_for_interval(SITE_CONFIG_DEFAULT, early)
    assert loads == []


def test_build_loads_in_solar_window_includes_dhw_and_buffer() -> None:
    midday = datetime(2026, 6, 15, 12, 0)
    loads = build_loads_for_interval(SITE_CONFIG_DEFAULT, midday, is_sunny=True)
    ids = [load.id for load in loads]
    assert "weheat_dhw" in ids
    assert "buffer_overheat" in ids
    # WeHeat is read-only → engine schrijft alleen advies tot write-adapter bestaat.
    assert all(not load.controllable for load in loads)


def test_build_loads_skips_buffer_when_not_sunny() -> None:
    midday = datetime(2026, 6, 15, 12, 0)
    loads = build_loads_for_interval(SITE_CONFIG_DEFAULT, midday, is_sunny=False)
    ids = [load.id for load in loads]
    assert "buffer_overheat" not in ids


# ---------------------------------------------------------------------------
# Firestore-persistentie
# ---------------------------------------------------------------------------


def _sample_decision() -> DispositionDecision:
    # Use een interval kort vóór nu zodat de `hours=24`-query 'm consistent
    # binnen het venster ziet, onafhankelijk van wanneer de test draait.
    recent = (datetime.now() - timedelta(minutes=15)).replace(second=0, microsecond=0)
    return DispositionDecision(
        interval_start=recent.isoformat(),
        regime="saldering",
        spot_price_eur_per_kwh=0.085,
        forecast_surplus_kwh=1.5,
        cum_ytd_teruglevering_kwh=4200.0,
        allocations=[
            DispositionAllocation(
                disposition=Disposition.SELF_CONSUME,
                load_id="weheat_dhw",
                kwh=1.0,
                marginal_gain_eur_per_kwh=0.130,
            ),
            DispositionAllocation(
                disposition=Disposition.EXPORT,
                kwh=0.5,
                marginal_gain_eur_per_kwh=0.0,
            ),
        ],
        expected_saving_eur=0.13,
        rationale="[saldering] self_consume:weheat_dhw 1.000 kWh @ +€0.130/kWh → export 0.500 kWh @ +€0.000/kWh",
    )


def test_save_and_read_disposition_decision_roundtrips(fake_db: FakeFirestore) -> None:
    """Persistentie + ophalen levert dezelfde allocaties op."""
    save_disposition_decision(_sample_decision())
    recent = get_recent_disposition_decisions(hours=24)

    assert len(recent) == 1
    got = recent[0]
    assert got.regime == "saldering"
    assert got.expected_saving_eur == pytest.approx(0.13)
    dispositions = [a.disposition for a in got.allocations]
    assert dispositions == [Disposition.SELF_CONSUME, Disposition.EXPORT]
    assert got.allocations[0].load_id == "weheat_dhw"


# ---------------------------------------------------------------------------
# Cum YTD-teruglevering — bron is P1 total_export_kwh, niet netto.
# ---------------------------------------------------------------------------


def test_cum_ytd_first_call_sets_baseline_and_returns_zero(fake_db: FakeFirestore) -> None:
    """Eerste keer dit jaar: huidige stand wordt baseline; YTD = 0."""
    moment = datetime(2026, 6, 15, 12, 0)
    cum = get_cum_ytd_teruglevering(register_kwh=12345.6, now=moment)
    assert cum == 0.0


def test_cum_ytd_returns_delta_when_year_unchanged(fake_db: FakeFirestore) -> None:
    """Binnen hetzelfde jaar: YTD = huidige stand − jaarbaseline."""
    seed = datetime(2026, 1, 1, 0, 15)
    get_cum_ytd_teruglevering(register_kwh=10_000.0, now=seed)

    later = datetime(2026, 6, 15, 12, 0)
    cum = get_cum_ytd_teruglevering(register_kwh=14_200.0, now=later)
    assert cum == pytest.approx(4200.0)


def test_cum_ytd_resets_on_year_boundary(fake_db: FakeFirestore) -> None:
    """Jaarwissel: baseline opnieuw zetten op huidige stand → YTD weer 0."""
    get_cum_ytd_teruglevering(register_kwh=10_000.0, now=datetime(2026, 1, 1))
    get_cum_ytd_teruglevering(register_kwh=18_000.0, now=datetime(2026, 12, 31))

    # 01-01-2027: nieuwe jaarteller.
    cum = get_cum_ytd_teruglevering(register_kwh=18_100.0, now=datetime(2027, 1, 1))
    assert cum == 0.0

    # Later in 2027 telt de delta sinds 18_100.
    cum_later = get_cum_ytd_teruglevering(register_kwh=18_300.0, now=datetime(2027, 3, 1))
    assert cum_later == pytest.approx(200.0)
