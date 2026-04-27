"""
Tests for the Firestore data layer.

These tests run against the in-memory ``FakeFirestore`` from
``conftest.py``, so they require no external services and are safe to run
in CI. They cover:

  * Policy round-trips (Layer 1 + Layer 2 settings preserved)
  * Activation status lifecycle (defaults, dismissals, activation)
  * State snapshot persistence + counting + earliest-date lookup
  * Decision persistence
  * FCM token registration, listing, and soft-delete
  * Learned profile round-trips (including time-of-day fields)
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from src.optimizer.learning import ActivationStatus, DailyPattern, LearnedProfile, ThermalSignature
from src.optimizer.policy import (
    Strategy,
    StrategyWeights,
    SystemLimits,
    TempBand,
    default_policy,
)
from src.state import firestore as fs
from src.state.models import Decision, FCMToken, SystemState

# pytest plugin: keep the fake_db fixture from conftest in scope
pytestmark = pytest.mark.usefixtures("fake_db")


# --- Policy ----------------------------------------------------------------


def test_get_policy_returns_default_when_empty() -> None:
    p = fs.get_policy()
    assert p.strategy is Strategy.MAX_SAVING
    assert p.learning_enabled is False
    assert p.limits.boiler_legionella_floor_c == 45.0


def test_save_and_reload_policy_preserves_layer1_and_layer2() -> None:
    original = default_policy()
    original.strategy = Strategy.CUSTOM
    original.custom_weights = StrategyWeights(cost=0.4, comfort=0.4,
                                              self_consumption=0.15, renewable_share=0.05)
    original.limits.living_room = TempBand(20.0, 21.5)
    original.limits.floor_max_flow_c = 48.0
    original.learning_enabled = True

    fs.save_policy(original)
    reloaded = fs.get_policy()

    assert reloaded.strategy is Strategy.CUSTOM
    assert reloaded.custom_weights is not None
    assert reloaded.custom_weights.cost == pytest.approx(0.4)
    assert reloaded.limits.living_room.min_c == 20.0
    assert reloaded.limits.living_room.max_c == 21.5
    assert reloaded.limits.floor_max_flow_c == 48.0
    assert reloaded.learning_enabled is True


def test_save_policy_bumps_updated_at() -> None:
    p = default_policy()
    p.updated_at = datetime(2020, 1, 1)
    fs.save_policy(p)
    reloaded = fs.get_policy()
    assert reloaded.updated_at > datetime(2020, 1, 1)


# --- Activation status ------------------------------------------------------


def test_activation_status_defaults_to_dormant() -> None:
    status = fs.get_activation_status()
    assert status.is_active is False
    assert status.activated_at is None
    assert status.push_dismissed_count == 0
    assert status.data_start is None


def test_activation_status_dismissal_persists() -> None:
    status = fs.get_activation_status()
    status.push_dismissed_count = 2
    status.push_sent_at = datetime(2026, 5, 1, 19, 0)
    fs.update_activation_status(status)

    reloaded = fs.get_activation_status()
    assert reloaded.push_dismissed_count == 2
    assert reloaded.push_sent_at == datetime(2026, 5, 1, 19, 0)
    assert reloaded.is_active is False


def test_activation_status_activation_persists() -> None:
    status = ActivationStatus(
        is_active=True,
        activated_at=datetime(2026, 6, 7, 8, 30),
        data_start=datetime(2026, 4, 26),
    )
    fs.update_activation_status(status)

    reloaded = fs.get_activation_status()
    assert reloaded.is_active is True
    assert reloaded.activated_at == datetime(2026, 6, 7, 8, 30)
    assert reloaded.data_start == datetime(2026, 4, 26)


# --- State snapshots --------------------------------------------------------


def _snapshot(ts: datetime, indoor: float = 20.5) -> SystemState:
    return SystemState(
        timestamp=ts,
        pv_power=4500, house_load=800, hp_power=900,
        dompelaar_on=False,
        boiler_temp=52, buffer_temp=38,
        indoor_temp=indoor, outdoor_temp=12,
    )


def test_save_and_count_state_samples() -> None:
    base = datetime(2026, 4, 27, 12, 0)
    for i in range(5):
        fs.save_state_snapshot(_snapshot(base + timedelta(minutes=15 * i)))

    # Snapshots at base + {0, 15, 30, 45, 60} minutes
    assert fs.count_state_samples(since=base) == 5
    assert fs.count_state_samples(since=base + timedelta(minutes=15)) == 4  # boundary inclusive
    assert fs.count_state_samples(since=base + timedelta(minutes=20)) == 3
    assert fs.count_state_samples(since=base + timedelta(hours=2)) == 0


def test_get_data_start_date_returns_earliest() -> None:
    base = datetime(2026, 4, 27, 12, 0)
    fs.save_state_snapshot(_snapshot(base + timedelta(minutes=30)))
    fs.save_state_snapshot(_snapshot(base))
    fs.save_state_snapshot(_snapshot(base + timedelta(minutes=15)))

    earliest = fs.get_data_start_date()
    assert earliest == base


def test_get_data_start_date_returns_none_when_empty() -> None:
    assert fs.get_data_start_date() is None


# --- Decisions --------------------------------------------------------------


def test_save_decision_round_trip() -> None:
    d = Decision(
        timestamp=datetime(2026, 4, 27, 13, 0),
        tag="BOOST",
        action="boiler_charge",
        reason="Goedkoop uur, boiler nog niet vol",
        rationale="Spot price 0.08 EUR/kWh < threshold; boiler at 52°C target 60°C",
        boiler_target_temp=60.0,
        dompelaar_on=False,
        heat_pump_allowed=True,
        indoor_setpoint_offset=0.0,
        estimated_savings_eur=0.12,
        strategy_used="max_saving",
        learning_active=False,
    )
    fs.save_decision(d)

    raw = list(fs._db().collection(fs.DECISIONS_COLLECTION).stream())
    assert len(raw) == 1
    payload = raw[0].to_dict()
    assert payload is not None
    assert payload["tag"] == "BOOST"
    assert payload["boiler_target_temp"] == 60.0


# --- FCM tokens -------------------------------------------------------------


def test_fcm_tokens_register_list_and_soft_delete() -> None:
    fs.save_fcm_token(FCMToken(token="ipad-roel", platform="web", label="iPad Roel"))
    fs.save_fcm_token(FCMToken(token="iphone-roel", platform="web", label="iPhone Roel"))
    fs.save_fcm_token(FCMToken(token="old-laptop", platform="web", valid=False))

    tokens = fs.get_user_fcm_tokens()
    assert sorted(tokens) == ["ipad-roel", "iphone-roel"]

    fs.mark_fcm_token_invalid("ipad-roel")
    tokens_after = fs.get_user_fcm_tokens()
    assert tokens_after == ["iphone-roel"]


def test_fcm_token_upsert_replaces_metadata() -> None:
    fs.save_fcm_token(FCMToken(token="x", platform="web", label="old"))
    fs.save_fcm_token(FCMToken(token="x", platform="ios", label="new"))

    tokens = fs.get_user_fcm_tokens()
    assert tokens == ["x"]


# --- Learned profile --------------------------------------------------------


def test_learned_profile_defaults_to_empty() -> None:
    profile = fs.get_learned_profile()
    assert profile.last_trained is None
    assert profile.samples_used == 0
    assert profile.thermal.heat_loss_w_per_k == 0.0


def test_learned_profile_round_trip_preserves_times_and_thermal() -> None:
    profile = LearnedProfile(
        daily=DailyPattern(
            typical_wake_time=time(7, 15),
            typical_leave_time=time(8, 30),
            typical_return_time=time(17, 45),
            typical_sleep_time=time(23, 30),
            confidence=0.82,
        ),
        thermal=ThermalSignature(
            heat_loss_w_per_k=180.0,
            pv_to_indoor_lag_min=22.0,
            boiler_decay_c_per_hour=0.4,
            floor_thermal_mass_kwh_per_c=2.1,
        ),
        last_trained=datetime(2026, 6, 8, 4, 0),
        samples_used=4032,
    )
    fs.save_learned_profile(profile)

    reloaded = fs.get_learned_profile()
    assert reloaded.daily.typical_wake_time == time(7, 15)
    assert reloaded.daily.typical_return_time == time(17, 45)
    assert reloaded.daily.confidence == pytest.approx(0.82)
    assert reloaded.thermal.heat_loss_w_per_k == 180.0
    assert reloaded.thermal.pv_to_indoor_lag_min == 22.0
    assert reloaded.last_trained == datetime(2026, 6, 8, 4, 0)
    assert reloaded.samples_used == 4032


# --- Sanity: invalid limits don't get saved by mistake ----------------------


def test_policy_validate_catches_legionella_violation() -> None:
    p = default_policy()
    p.limits = SystemLimits(boiler_legionella_floor_c=40.0)
    errs = p.limits.validate()
    assert any("legionella" in e for e in errs)
