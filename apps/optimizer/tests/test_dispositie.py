"""Golden-case tests voor de dispositie-engine (§8 van de spec)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.optimizer.dispositie import (
    ENERGIEDIRECT_STAFFEL,
    SITE_CONFIG_DEFAULT,
    TARIFF,
    DeferrableLoad,
    Disposition,
    EngineConfig,
    EngineState,
    decide,
    marginal_staffel_cost,
    staffel_cost_at,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def saldering_cfg() -> EngineConfig:
    return EngineConfig(
        regime="saldering",
        site=SITE_CONFIG_DEFAULT,
        tariff=TARIFF,
        staffel=ENERGIEDIRECT_STAFFEL,
    )


@pytest.fixture
def no_saldering_cfg_negative_net() -> EngineConfig:
    """exportNet = 0.06 − 0.078 = −0.018 (curtail wint van export)."""
    return EngineConfig(
        regime="no_saldering",
        site=SITE_CONFIG_DEFAULT,
        tariff=TARIFF,
        staffel=ENERGIEDIRECT_STAFFEL,
    )


@pytest.fixture
def no_saldering_cfg_positive_net() -> EngineConfig:
    """exportNet > 0 → export wint van curtail."""
    tariff = replace(TARIFF, feed_in_tariff_2027_eur_per_kwh=0.15)
    return EngineConfig(
        regime="no_saldering",
        site=SITE_CONFIG_DEFAULT,
        tariff=tariff,
        staffel=ENERGIEDIRECT_STAFFEL,
    )


def _controllable_load(load_id: str, kwh: float) -> DeferrableLoad:
    return DeferrableLoad(id=load_id, label=load_id, available_kwh=kwh, controllable=True)


# ---------------------------------------------------------------------------
# §8 test 1 + 2 — staffel-rekenwerk
# ---------------------------------------------------------------------------


def test_marginal_staffel_cost_at_8100_kwh() -> None:
    """Vlakke Energiedirect-staffel: €32,52 per 250 kWh = €0,13008/kWh."""
    assert marginal_staffel_cost(8100, ENERGIEDIRECT_STAFFEL) == pytest.approx(0.13008, abs=1e-5)


def test_staffel_cost_lookup_at_8100_kwh() -> None:
    """8100 kWh teruglevering valt in band 8001–8250 → €1056,24/jaar."""
    assert staffel_cost_at(8100, ENERGIEDIRECT_STAFFEL) == pytest.approx(1056.24)


def test_staffel_cost_lookup_at_6600_kwh() -> None:
    """6600 kWh valt in band 6501–6750 → €861,24/jaar."""
    assert staffel_cost_at(6600, ENERGIEDIRECT_STAFFEL) == pytest.approx(861.24)


# ---------------------------------------------------------------------------
# §8 test 3 — golden besparing 8100 → 6600 = €195/jaar
# ---------------------------------------------------------------------------


def test_golden_saving_1500_kwh_shifted_under_saldering(saldering_cfg: EngineConfig) -> None:
    """Verschuif 1500 kWh naar self_consume → ~€195/jaar besparing (rekentool)."""
    decision = decide(
        interval_start="2026-06-15T12:00:00Z",
        forecast_surplus_kwh=1500.0,
        loads=[_controllable_load("weheat_dhw", 1500.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=8100.0),
        cfg=saldering_cfg,
    )

    # Alles naar self_consume; gain ≈ marginale staffelkost @ 8100 kWh.
    assert len(decision.allocations) == 1
    alloc = decision.allocations[0]
    assert alloc.disposition is Disposition.SELF_CONSUME
    assert alloc.kwh == pytest.approx(1500.0)
    assert alloc.marginal_gain_eur_per_kwh == pytest.approx(0.130, abs=1e-3)

    # Golden case: besparing ~€195 (rekentool: precies €195.00 verschil tussen bands).
    assert decision.expected_saving_eur == pytest.approx(195.0, abs=1.0)

    # Lookup-pad: direct staffel-verschil moet exact €195 zijn.
    saved_lookup = staffel_cost_at(8100, ENERGIEDIRECT_STAFFEL) - staffel_cost_at(6600, ENERGIEDIRECT_STAFFEL)
    assert saved_lookup == pytest.approx(195.0)


# ---------------------------------------------------------------------------
# §8 test 4 — saldering kiest NOOIT curtail
# ---------------------------------------------------------------------------


def test_saldering_never_curtails_when_export_room_available(saldering_cfg: EngineConfig) -> None:
    """Onder saldering is curtail-gain altijd negatief; export (gain=0) wint altijd."""
    decision = decide(
        interval_start="2026-06-15T12:00:00Z",
        forecast_surplus_kwh=20.0,  # ruim boven verschuifbare lasten
        loads=[_controllable_load("weheat_dhw", 1.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=8100.0),
        cfg=saldering_cfg,
    )

    dispositions = [a.disposition for a in decision.allocations]
    assert Disposition.CURTAIL not in dispositions
    # Rest moet teruggeleverd worden (gain=0, baseline).
    assert Disposition.EXPORT in dispositions


# ---------------------------------------------------------------------------
# §8 test 5 — no_saldering, exportNet < 0
# ---------------------------------------------------------------------------


def test_no_saldering_negative_export_net_prefers_curtail_over_export(
    no_saldering_cfg_negative_net: EngineConfig,
) -> None:
    """feedInTariff 0.06, feedInCost 0.078 → exportNet = −0.018.

    Self_consume wint (importPrice − exportNet = ~0.250). Daarna curtail (+0.018)
    boven export (0). Engine moet NOOIT export kiezen bij negatieve exportNet.
    """
    decision = decide(
        interval_start="2027-06-15T12:00:00Z",
        forecast_surplus_kwh=5.0,
        loads=[_controllable_load("weheat_dhw", 1.0)],  # beperkte self_consume-capaciteit
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=no_saldering_cfg_negative_net,
    )

    dispositions = [a.disposition for a in decision.allocations]
    assert decision.allocations[0].disposition is Disposition.SELF_CONSUME
    assert Disposition.EXPORT not in dispositions, "Bij exportNet<0 mag de engine niet terugleveren."
    assert Disposition.CURTAIL in dispositions

    # Gains in lijn met de spec.
    self_alloc = next(a for a in decision.allocations if a.disposition is Disposition.SELF_CONSUME)
    curtail_alloc = next(a for a in decision.allocations if a.disposition is Disposition.CURTAIL)
    assert self_alloc.marginal_gain_eur_per_kwh == pytest.approx(0.250, abs=1e-3)
    assert curtail_alloc.marginal_gain_eur_per_kwh == pytest.approx(0.018, abs=1e-3)


# ---------------------------------------------------------------------------
# §8 test 6 — no_saldering, exportNet > 0
# ---------------------------------------------------------------------------


def test_no_saldering_positive_export_net_prefers_export_over_curtail(
    no_saldering_cfg_positive_net: EngineConfig,
) -> None:
    """feedInTariff 0.15 → exportNet > 0 → curtail-gain negatief → export verkozen."""
    decision = decide(
        interval_start="2027-06-15T12:00:00Z",
        forecast_surplus_kwh=5.0,
        loads=[_controllable_load("weheat_dhw", 1.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=no_saldering_cfg_positive_net,
    )

    dispositions = [a.disposition for a in decision.allocations]
    assert decision.allocations[0].disposition is Disposition.SELF_CONSUME
    assert Disposition.EXPORT in dispositions
    assert Disposition.CURTAIL not in dispositions


# ---------------------------------------------------------------------------
# §8 test 7 — capaciteitsbegrenzing → overschot naar export (B met net>0) / curtail (B met net<0)
# ---------------------------------------------------------------------------


def test_capacity_overflow_goes_to_export_in_saldering(saldering_cfg: EngineConfig) -> None:
    """Surplus > self_consume-capaciteit → rest naar export (saldering)."""
    decision = decide(
        interval_start="2026-06-15T12:00:00Z",
        forecast_surplus_kwh=3.0,
        loads=[_controllable_load("weheat_dhw", 1.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=saldering_cfg,
    )

    self_alloc = next(a for a in decision.allocations if a.disposition is Disposition.SELF_CONSUME)
    export_alloc = next(a for a in decision.allocations if a.disposition is Disposition.EXPORT)
    assert self_alloc.kwh == pytest.approx(1.0)
    assert export_alloc.kwh == pytest.approx(2.0)


def test_capacity_overflow_goes_to_curtail_in_no_saldering_negative_net(
    no_saldering_cfg_negative_net: EngineConfig,
) -> None:
    """Surplus > self_consume-capaciteit, geen accu → rest naar curtail bij exportNet<0."""
    decision = decide(
        interval_start="2027-06-15T12:00:00Z",
        forecast_surplus_kwh=3.0,
        loads=[_controllable_load("weheat_dhw", 1.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=no_saldering_cfg_negative_net,
    )

    self_alloc = next(a for a in decision.allocations if a.disposition is Disposition.SELF_CONSUME)
    curtail_alloc = next(a for a in decision.allocations if a.disposition is Disposition.CURTAIL)
    assert self_alloc.kwh == pytest.approx(1.0)
    assert curtail_alloc.kwh == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# §8 test 8 — alle getallen naar buiten zijn afgerond (geen float-artefacten)
# ---------------------------------------------------------------------------


def test_outputs_are_rounded(saldering_cfg: EngineConfig) -> None:
    """Allocaties op 3 decimalen, expected_saving_eur op 2 decimalen — Firestore-/UI-veilig."""
    decision = decide(
        interval_start="2026-06-15T12:00:00Z",
        forecast_surplus_kwh=1.23456789,
        loads=[_controllable_load("weheat_dhw", 2.0)],
        state=EngineState(cum_ytd_teruglevering_kwh=8100.0),
        cfg=saldering_cfg,
    )

    for alloc in decision.allocations:
        # Geen meer dan 3 decimalen, geen float-staart.
        assert alloc.kwh == round(alloc.kwh, 3)
        assert alloc.marginal_gain_eur_per_kwh == round(alloc.marginal_gain_eur_per_kwh, 3)
    assert decision.forecast_surplus_kwh == round(decision.forecast_surplus_kwh, 3)
    assert decision.cum_ytd_teruglevering_kwh == round(decision.cum_ytd_teruglevering_kwh, 3)
    assert decision.expected_saving_eur == round(decision.expected_saving_eur, 2)


# ---------------------------------------------------------------------------
# Bonus: load met controllable=False wordt overgeslagen (WeHeat zonder write-adapter)
# ---------------------------------------------------------------------------


def test_uncontrollable_load_is_ignored(saldering_cfg: EngineConfig) -> None:
    """De WeHeat heeft geen bevestigde write-adapter; controllable=False ⇒ niet schakelen."""
    decision = decide(
        interval_start="2026-06-15T12:00:00Z",
        forecast_surplus_kwh=2.0,
        loads=[DeferrableLoad(id="weheat_dhw", label="WeHeat DHW", available_kwh=2.0, controllable=False)],
        state=EngineState(cum_ytd_teruglevering_kwh=0.0),
        cfg=saldering_cfg,
    )

    dispositions = [a.disposition for a in decision.allocations]
    assert Disposition.SELF_CONSUME not in dispositions
    assert Disposition.EXPORT in dispositions
