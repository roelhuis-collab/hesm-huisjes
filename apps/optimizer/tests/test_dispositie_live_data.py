"""Live-data integratie: P1-surplus, Growatt-fallback, staleness-guard, safe_mode.

Geen netwerk: alle externe IO via stubs of MockTransport. We testen de pure
helpers ``live_surplus_kwh`` en ``build_surplus_snapshot`` plus de cycle-laag
``_run_dispositie`` met een geïnjecteerde fake EnergyZero-provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from src.connectors.homewizard import P1MeterReading
from src.optimizer import cycle
from src.optimizer.dispositie import Disposition
from src.optimizer.dispositie_providers import (
    DEFAULT_P1_MAX_AGE_SECONDS,
    EnergyZeroSpotPriceProvider,
    build_surplus_snapshot,
    live_surplus_kwh,
)
from src.optimizer.v0 import StateInput
from src.state.firestore import DISPOSITION_DECISIONS_COLLECTION, _db

from tests.fake_firestore import FakeFirestore

# ---------------------------------------------------------------------------
# Pure: P1 sign-conventie + kWh-conversie
# ---------------------------------------------------------------------------


def test_live_surplus_kwh_negative_power_means_export() -> None:
    """active_power_w = −4000 W ⇒ 4 kW export × 0,25 h = 1,0 kWh."""
    assert live_surplus_kwh(-4000.0) == pytest.approx(1.0, abs=1e-3)


def test_live_surplus_kwh_positive_power_means_import_so_zero_surplus() -> None:
    """active_power_w > 0 (import vanaf net): surplus klippt op 0."""
    assert live_surplus_kwh(800.0) == 0.0


def test_live_surplus_kwh_zero() -> None:
    assert live_surplus_kwh(0.0) == 0.0


# ---------------------------------------------------------------------------
# build_surplus_snapshot — beslisboom
# ---------------------------------------------------------------------------


def test_snapshot_prefers_fresh_p1_over_growatt() -> None:
    """P1 vers (< 30 s oud) ⇒ source=p1, niet stale."""
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    captured = now - timedelta(seconds=5)

    snap = build_surplus_snapshot(
        p1_active_power_w=-4000.0,
        p1_captured_at=captured,
        pv_power_w=4500.0,
        house_load_w=500.0,
        now=now,
    )

    assert snap.source == "p1"
    assert snap.stale is False
    assert snap.p1_age_seconds == pytest.approx(5.0, abs=0.1)
    assert snap.surplus_kwh == pytest.approx(1.0, abs=1e-3)


def test_snapshot_falls_back_to_growatt_when_p1_stale() -> None:
    """P1 > 30 s oud ⇒ Growatt-fallback, stale=True (cycle markeert safe_mode)."""
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    captured = now - timedelta(seconds=120)

    snap = build_surplus_snapshot(
        p1_active_power_w=-4000.0,
        p1_captured_at=captured,
        pv_power_w=4500.0,
        house_load_w=500.0,
        now=now,
    )

    assert snap.source == "growatt_fallback"
    assert snap.stale is True
    assert snap.p1_age_seconds == pytest.approx(120.0, abs=0.1)
    # Fallback = (4500-500) × 0,25 / 1000 = 1.0 kWh
    assert snap.surplus_kwh == pytest.approx(1.0, abs=1e-3)


def test_snapshot_falls_back_to_growatt_when_p1_missing() -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    snap = build_surplus_snapshot(
        p1_active_power_w=None,
        p1_captured_at=None,
        pv_power_w=3000.0,
        house_load_w=500.0,
        now=now,
    )

    assert snap.source == "growatt_fallback"
    assert snap.stale is True
    assert snap.surplus_kwh > 0


def test_snapshot_no_data_when_both_p1_and_pv_missing() -> None:
    """Geen P1 én geen Growatt-PV ⇒ source=no_data, surplus=0, stale=True."""
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    snap = build_surplus_snapshot(
        p1_active_power_w=None,
        p1_captured_at=None,
        pv_power_w=0.0,
        house_load_w=500.0,
        now=now,
    )

    assert snap.source == "no_data"
    assert snap.stale is True
    assert snap.surplus_kwh == 0.0


def test_snapshot_uses_default_max_age_30s() -> None:
    """De default-grens komt uit DEFAULT_P1_MAX_AGE_SECONDS = 30."""
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    # Net binnen de marge.
    fresh = build_surplus_snapshot(
        p1_active_power_w=-1000.0,
        p1_captured_at=now - timedelta(seconds=DEFAULT_P1_MAX_AGE_SECONDS),
        pv_power_w=2000.0,
        house_load_w=200.0,
        now=now,
    )
    assert fresh.source == "p1"

    # Net buiten de marge.
    stale = build_surplus_snapshot(
        p1_active_power_w=-1000.0,
        p1_captured_at=now - timedelta(seconds=DEFAULT_P1_MAX_AGE_SECONDS + 1),
        pv_power_w=2000.0,
        house_load_w=200.0,
        now=now,
    )
    assert stale.source == "growatt_fallback"
    assert stale.stale is True


# ---------------------------------------------------------------------------
# Cycle-laag: safe_mode-paden via geïnjecteerde fake provider
# ---------------------------------------------------------------------------


class _FakeSpotProvider:
    def __init__(self, return_value: float | None) -> None:
        self._return = return_value
        self.calls: list[str] = []

    async def forecast(self, interval_start: str) -> float | None:
        self.calls.append(interval_start)
        return self._return


def _make_state_input(
    *,
    now: datetime,
    pv_power: float = 4500.0,
    house_load: float = 500.0,
) -> StateInput:
    return StateInput(
        timestamp=now,
        pv_power=pv_power,
        house_load=house_load,
        hp_power=0.0,
        dompelaar_on=False,
        boiler_temp=55.0,
        indoor_temp=21.0,
        outdoor_temp=18.0,
        grid_import=None,
        price_eur_kwh=None,
    )


def _hw_reading(*, age_seconds: float, active_power_w: float, now: datetime, export_kwh: float) -> P1MeterReading:
    return P1MeterReading(
        captured_at=now - timedelta(seconds=age_seconds),
        active_power_w=active_power_w,
        total_export_kwh=export_kwh,
    )


async def test_run_dispositie_marks_safe_mode_when_spot_missing(
    fake_db: FakeFirestore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spot ontbreekt ⇒ safe_mode, lege allocaties, beslissing wordt wel gepersist."""
    monkeypatch.setattr(cycle, "_SPOT_PROVIDER", _FakeSpotProvider(return_value=None))

    now = datetime(2026, 6, 15, 12, 0)
    gathered: dict[str, Any] = {
        "homewizard": _hw_reading(age_seconds=5, active_power_w=-4000.0, now=now, export_kwh=1000.0),
        "weather": None,
        "prices": None,
        "weheat": None,
        "resideo": None,
        "shelly": None,
        "growatt": None,
    }

    decision = await cycle._run_dispositie(_make_state_input(now=now), gathered)

    assert decision is not None
    assert decision.safe_mode is True
    assert decision.allocations == []
    assert "geen day-ahead-prijs" in decision.rationale.lower()

    # Direct uit de raw-collection lezen (de query-helper filtert op laatste 24 uur
    # en de testtijd ligt voorbij dat venster t.o.v. wall-clock).
    raw = [d.to_dict() or {} for d in _db().collection(DISPOSITION_DECISIONS_COLLECTION).stream()]
    assert len(raw) == 1
    assert raw[0]["safe_mode"] is True


async def test_run_dispositie_marks_safe_mode_when_p1_stale(
    fake_db: FakeFirestore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 ouder dan 30 s ⇒ Growatt-fallback, allocaties wel berekend, safe_mode=True."""
    monkeypatch.setattr(cycle, "_SPOT_PROVIDER", _FakeSpotProvider(return_value=0.08))

    now = datetime(2027, 6, 15, 12, 0)  # no_saldering om gain robuust te maken
    gathered: dict[str, Any] = {
        "homewizard": _hw_reading(age_seconds=120, active_power_w=-4000.0, now=now, export_kwh=1000.0),
        "weather": None,
        "prices": None,
        "weheat": None,
        "resideo": None,
        "shelly": None,
        "growatt": None,
    }

    decision = await cycle._run_dispositie(_make_state_input(now=now), gathered)

    assert decision is not None
    assert decision.safe_mode is True
    assert decision.spot_price_eur_per_kwh == pytest.approx(0.08)
    assert "stale" in decision.rationale.lower()
    # Allocaties wel berekend (advies-modus): export blijft kiesbaar bij positieve spot.
    dispositions = [a.disposition for a in decision.allocations]
    assert Disposition.EXPORT in dispositions or Disposition.SELF_CONSUME in dispositions


async def test_run_dispositie_no_safe_mode_when_fresh_p1_and_spot(
    fake_db: FakeFirestore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verse P1 + spot beschikbaar ⇒ normale beslissing, geen safe_mode."""
    monkeypatch.setattr(cycle, "_SPOT_PROVIDER", _FakeSpotProvider(return_value=0.10))

    now = datetime(2027, 6, 15, 12, 0)
    gathered: dict[str, Any] = {
        "homewizard": _hw_reading(age_seconds=5, active_power_w=-4000.0, now=now, export_kwh=1000.0),
        "weather": None,
        "prices": None,
        "weheat": None,
        "resideo": None,
        "shelly": None,
        "growatt": None,
    }

    decision = await cycle._run_dispositie(_make_state_input(now=now), gathered)

    assert decision is not None
    assert decision.safe_mode is False
    assert "safe_mode" not in decision.rationale.lower()
    assert decision.forecast_surplus_kwh == pytest.approx(1.0, abs=1e-3)


# ---------------------------------------------------------------------------
# EnergyZeroSpotPriceProvider — fallback bij ontbrekende prijs
# ---------------------------------------------------------------------------


class _StubEnergyZeroClient:
    def __init__(self, return_value: Any) -> None:
        self._return = return_value

    async def __aenter__(self) -> _StubEnergyZeroClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def quarter_price_for(self, when: datetime) -> Any:
        return self._return


async def test_provider_returns_none_when_client_yields_none() -> None:
    """Provider geeft None door als de onderliggende client geen prijs heeft."""
    provider = EnergyZeroSpotPriceProvider(client=_StubEnergyZeroClient(return_value=None))  # type: ignore[arg-type]
    result = await provider.forecast("2026-06-15T12:00:00")
    assert result is None
