"""Spot-gedreven engine-tests — Zonneplan dynamisch contract.

De staffel-tests uit de Energiedirect-PR zijn verwijderd; ``tariff.energiedirect.ts``
blijft als historische referentie maar de engine raakt 'm niet meer aan.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.optimizer.dispositie import (
    SITE_CONFIG_DEFAULT,
    TARIFF,
    DeferrableLoad,
    Disposition,
    EngineConfig,
    EngineState,
    SalderingConfig,
    decide,
    export_value,
    import_price,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def saldering_cfg() -> EngineConfig:
    return EngineConfig(regime="saldering", site=SITE_CONFIG_DEFAULT, tariff=TARIFF)


@pytest.fixture
def no_saldering_cfg() -> EngineConfig:
    tariff = replace(TARIFF, saldering=SalderingConfig(active=False, until_date="2027-01-01"))
    return EngineConfig(regime="no_saldering", site=SITE_CONFIG_DEFAULT, tariff=tariff)


def _controllable_load(load_id: str, kwh: float) -> DeferrableLoad:
    return DeferrableLoad(id=load_id, label=load_id, available_kwh=kwh, controllable=True)


# ---------------------------------------------------------------------------
# Sanity — prijsformules
# ---------------------------------------------------------------------------


def test_import_price_adds_inkoopvergoeding_and_energy_tax() -> None:
    """Spot 0.10 + inkoop 0.025 + energy_tax 0.1316 = 0.2566 incl. btw."""
    assert import_price(0.10, TARIFF) == pytest.approx(0.10 + 0.025 + 0.1316, abs=1e-6)


def test_export_value_under_saldering_adds_energy_tax_back() -> None:
    """Saldeerbereik: energiebelasting komt terug op export → spot + opslag + tax."""
    val = export_value(
        0.10,
        interval_hour=21,  # geen Zonnebonus om de formule schoon te testen
        cum_ytd_teruglevering_kwh=0.0,
        regime="saldering",
        tariff=TARIFF,
    )
    assert val == pytest.approx(0.10 + 0.0 + 0.1316, abs=1e-6)


def test_export_value_under_no_saldering_drops_energy_tax() -> None:
    val_saldering = export_value(
        0.10,
        interval_hour=21,
        cum_ytd_teruglevering_kwh=0.0,
        regime="saldering",
        tariff=TARIFF,
    )
    val_no_saldering = export_value(
        0.10,
        interval_hour=21,
        cum_ytd_teruglevering_kwh=0.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    assert val_saldering - val_no_saldering == pytest.approx(TARIFF.energy_tax_eur_per_kwh, abs=1e-6)


# ---------------------------------------------------------------------------
# Duur avonduur (spot hoog, geen Zonnebonus): self_consume wint van export
# ---------------------------------------------------------------------------


def test_high_evening_spot_self_consume_beats_export(no_saldering_cfg: EngineConfig) -> None:
    """Avond (geen Zonnebonus), no_saldering, spot 0.30: self_consume wint ruim.

    Onder no_saldering = (importPrice − exportValue) = inkoopvergoeding + energy_tax
    ≈ €0,16/kWh — onafhankelijk van de spot.
    """
    decision = decide(
        interval_start="2027-01-15T21:00:00",
        forecast_surplus_kwh=2.0,
        loads=[_controllable_load("weheat_dhw", 2.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=no_saldering_cfg,
        spot_price_eur_per_kwh=0.30,
    )

    assert len(decision.allocations) == 1
    alloc = decision.allocations[0]
    assert alloc.disposition is Disposition.SELF_CONSUME
    assert alloc.kwh == pytest.approx(2.0)
    expected_gain = TARIFF.inkoopvergoeding_eur_per_kwh + TARIFF.energy_tax_eur_per_kwh
    assert alloc.marginal_gain_eur_per_kwh == pytest.approx(expected_gain, abs=1e-3)
    # ~€0.16 × 2 kWh ≈ €0.31
    assert decision.expected_saving_eur == pytest.approx(2.0 * expected_gain, abs=0.02)


def test_high_evening_spot_under_saldering_self_consume_wins_only_by_inkoopvergoeding(
    saldering_cfg: EngineConfig,
) -> None:
    """Onder saldering: self_consume - export = inkoopvergoeding (~€0,025) — krap, maar wint."""
    decision = decide(
        interval_start="2026-11-15T21:00:00",
        forecast_surplus_kwh=2.0,
        loads=[_controllable_load("weheat_dhw", 2.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=saldering_cfg,
        spot_price_eur_per_kwh=0.30,
    )

    alloc = decision.allocations[0]
    assert alloc.disposition is Disposition.SELF_CONSUME
    assert alloc.marginal_gain_eur_per_kwh == pytest.approx(
        TARIFF.inkoopvergoeding_eur_per_kwh, abs=1e-3
    )


# ---------------------------------------------------------------------------
# Goedkoop / negatief middaguur (spot < 0): export NIET kiezen, curtail-gedrag correct
# ---------------------------------------------------------------------------


def test_deeply_negative_spot_export_is_skipped(no_saldering_cfg: EngineConfig) -> None:
    """Spot −0.20 no_saldering → exportValue −0.20 → curtail-gain +0.20.

    Export-gain = 0, dus negatieve-export-prijs (verlies) wordt vermeden:
    overschot voorbij de self_consume-capaciteit gaat naar curtail, NIET naar export.
    """
    decision = decide(
        interval_start="2027-06-15T13:00:00",  # daytime, maar bonus alleen bij spot+opslag>0 → niet hier
        forecast_surplus_kwh=3.0,
        loads=[_controllable_load("weheat_dhw", 1.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=no_saldering_cfg,
        spot_price_eur_per_kwh=-0.20,
    )

    dispositions = [a.disposition for a in decision.allocations]
    assert Disposition.EXPORT not in dispositions, "spot<0: terugleveren is verlies, engine vermijdt het."
    assert Disposition.CURTAIL in dispositions

    curtail = next(a for a in decision.allocations if a.disposition is Disposition.CURTAIL)
    assert curtail.marginal_gain_eur_per_kwh == pytest.approx(0.20, abs=1e-3)


def test_deeply_negative_spot_under_saldering_can_still_export(
    saldering_cfg: EngineConfig,
) -> None:
    """Onder saldering compenseert de energy_tax-restitutie tot ~−0,07 spot.

    Bij spot = −0,05 is exportValue = −0,05 + 0,1316 ≈ +0,08 → export blijft positief
    en wordt gewoon gekozen boven curtail (gain −0,08).
    """
    decision = decide(
        interval_start="2026-06-15T13:00:00",
        forecast_surplus_kwh=2.0,
        loads=[_controllable_load("weheat_dhw", 0.5)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=saldering_cfg,
        spot_price_eur_per_kwh=-0.05,
    )

    dispositions = [a.disposition for a in decision.allocations]
    assert Disposition.EXPORT in dispositions
    assert Disposition.CURTAIL not in dispositions


# ---------------------------------------------------------------------------
# Zonnebonus — daytime + cap + positief (spot+opslag) gating
# ---------------------------------------------------------------------------


def test_zonnebonus_active_daytime_increases_export_value() -> None:
    """11:00, spot 0.10, ytd < 7500 → exportValue krijgt +10% spot."""
    val_with = export_value(
        0.10,
        interval_hour=11,
        cum_ytd_teruglevering_kwh=0.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    val_without = export_value(
        0.10,
        interval_hour=21,  # buiten bonus-venster
        cum_ytd_teruglevering_kwh=0.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    assert val_with - val_without == pytest.approx(0.10 * TARIFF.zonnebonus_percentage, abs=1e-6)


def test_zonnebonus_not_applied_at_evening() -> None:
    """22:00 buiten venster → geen bonus."""
    val_evening = export_value(
        0.10,
        interval_hour=22,
        cum_ytd_teruglevering_kwh=0.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    assert val_evening == pytest.approx(0.10, abs=1e-6)


def test_zonnebonus_blocked_above_cap() -> None:
    """ytd >= 7500 → geen bonus, ook overdag."""
    val_under = export_value(
        0.10,
        interval_hour=11,
        cum_ytd_teruglevering_kwh=7_499.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    val_over = export_value(
        0.10,
        interval_hour=11,
        cum_ytd_teruglevering_kwh=7_500.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    assert val_under > val_over
    assert val_over == pytest.approx(0.10, abs=1e-6)


def test_zonnebonus_blocked_when_base_export_value_not_positive() -> None:
    """spot+opslag <= 0 → bonus geblokkeerd (geen bonus op negatieve marktprijs)."""
    val = export_value(
        0.0,  # spot+opslag = 0 → niet positief, geen bonus
        interval_hour=11,
        cum_ytd_teruglevering_kwh=0.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    assert val == pytest.approx(0.0, abs=1e-6)

    val_negative = export_value(
        -0.05,
        interval_hour=11,
        cum_ytd_teruglevering_kwh=0.0,
        regime="no_saldering",
        tariff=TARIFF,
    )
    assert val_negative == pytest.approx(-0.05, abs=1e-6)


# ---------------------------------------------------------------------------
# Regime-switch — energy_tax-term verdwijnt uit exportValue per 2027
# ---------------------------------------------------------------------------


def test_regime_switch_drops_energy_tax_from_export_value() -> None:
    """Verschil tussen saldering en no_saldering op exportValue = energy_tax (binnen bonus en cap)."""
    for hour in (11, 13, 21):
        for spot in (0.05, 0.10, 0.25):
            val_s = export_value(
                spot,
                interval_hour=hour,
                cum_ytd_teruglevering_kwh=0.0,
                regime="saldering",
                tariff=TARIFF,
            )
            val_ns = export_value(
                spot,
                interval_hour=hour,
                cum_ytd_teruglevering_kwh=0.0,
                regime="no_saldering",
                tariff=TARIFF,
            )
            assert val_s - val_ns == pytest.approx(TARIFF.energy_tax_eur_per_kwh, abs=1e-6)


# ---------------------------------------------------------------------------
# Bonus — uncontrollable last (WeHeat zonder write-adapter) wordt overgeslagen
# ---------------------------------------------------------------------------


def test_uncontrollable_load_is_ignored(saldering_cfg: EngineConfig) -> None:
    """WeHeat heeft geen bevestigde write-adapter; controllable=False ⇒ niet schakelen."""
    decision = decide(
        interval_start="2026-06-15T12:00:00",
        forecast_surplus_kwh=2.0,
        loads=[DeferrableLoad(id="weheat_dhw", label="WeHeat DHW", available_kwh=2.0, controllable=False)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=saldering_cfg,
        spot_price_eur_per_kwh=0.10,
    )

    dispositions = [a.disposition for a in decision.allocations]
    assert Disposition.SELF_CONSUME not in dispositions
    assert Disposition.EXPORT in dispositions


# ---------------------------------------------------------------------------
# Output-afronding (Firestore/UI-veilig)
# ---------------------------------------------------------------------------


def test_outputs_are_rounded(saldering_cfg: EngineConfig) -> None:
    """Allocaties op 3 decimalen, expected_saving_eur op 2 decimalen."""
    decision = decide(
        interval_start="2026-06-15T12:00:00",
        forecast_surplus_kwh=1.23456789,
        loads=[_controllable_load("weheat_dhw", 2.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=saldering_cfg,
        spot_price_eur_per_kwh=0.10,
    )

    for alloc in decision.allocations:
        assert alloc.kwh == round(alloc.kwh, 3)
        assert alloc.marginal_gain_eur_per_kwh == round(alloc.marginal_gain_eur_per_kwh, 3)
    assert decision.forecast_surplus_kwh == round(decision.forecast_surplus_kwh, 3)
    assert decision.cum_ytd_teruglevering_kwh == round(decision.cum_ytd_teruglevering_kwh, 3)
    assert decision.expected_saving_eur == round(decision.expected_saving_eur, 2)
    assert decision.spot_price_eur_per_kwh == round(decision.spot_price_eur_per_kwh, 3)
