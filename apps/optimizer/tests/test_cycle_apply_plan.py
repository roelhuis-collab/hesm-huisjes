"""Tests for the Resideo write-path in ``optimizer/cycle.py:_apply_plan``.

The optimizer only *actually* touches Roel's house via three cloud
paths — Shelly, Resideo, and (nothing on WeHeat because it's read-only).
Until 2026-06 all three were mock-only; PR7 shipped a real Resideo
client, but ``_apply_plan`` didn't push the plan's
``indoor_setpoint_offset`` into it. This test module locks in that the
write-path fires with the right value, respects Layer-1 clamping, and
skips noise below the setpoint-epsilon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from src.connectors import resideo as resideo_mod
from src.optimizer.cycle import SETPOINT_EPSILON_C, _apply_plan
from src.optimizer.policy import Policy, SystemLimits, TempBand
from src.optimizer.v0 import Plan


@dataclass
class _RecordingResideoClient:
    """In-process stand-in that logs every set_setpoint call."""

    setpoint_c: float = 20.5
    writes: list[float] | None = None

    def __post_init__(self) -> None:
        self.writes = []

    async def get_status(self) -> Any:
        return None  # unused — _apply_resideo_setpoint reads from `gathered`

    async def set_setpoint(self, target_c: float) -> None:
        assert self.writes is not None
        self.writes.append(float(target_c))

    async def aclose(self) -> None:
        return None


@dataclass
class _RecordingShellyClient:
    relays: list[bool] | None = None

    def __post_init__(self) -> None:
        self.relays = []

    async def set_relay(self, on: bool) -> None:
        assert self.relays is not None
        self.relays.append(bool(on))

    async def aclose(self) -> None:
        return None


@dataclass
class _CurrentReading:
    """Mimics the ``ResideoStatus.setpoint_c`` field used by _apply_plan."""

    setpoint_c: float


def _policy(band_min: float = 19.5, band_max: float = 22.0) -> Policy:
    return Policy(
        limits=SystemLimits(
            living_room=TempBand(band_min, band_max),
        ),
    )


def _plan(offset: float = 0.0) -> Plan:
    return Plan(
        tag="NORMAL",
        action="test-apply",
        reason="test",
        rationale="test",
        boiler_target_temp=52.0,
        dompelaar_on=False,
        heat_pump_allowed=True,
        indoor_setpoint_offset=offset,
    )


# ---------------------------------------------------------------------------
# Setpoint write path
# ---------------------------------------------------------------------------


async def test_apply_plan_pushes_setpoint_when_offset_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.optimizer import cycle as cycle_mod

    resideo_stub = _RecordingResideoClient()
    shelly_stub = _RecordingShellyClient()
    monkeypatch.setattr(cycle_mod, "resideo_client", lambda: resideo_stub)
    monkeypatch.setattr(cycle_mod, "shelly_client", lambda: shelly_stub)

    plan = _plan(offset=+0.8)
    gathered = {"resideo": _CurrentReading(setpoint_c=20.5)}
    await _apply_plan(plan, _policy(), gathered)

    assert resideo_stub.writes == [pytest.approx(21.3)]
    assert shelly_stub.relays == [False]


async def test_apply_plan_clamps_above_layer1_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.optimizer import cycle as cycle_mod

    resideo_stub = _RecordingResideoClient()
    monkeypatch.setattr(cycle_mod, "resideo_client", lambda: resideo_stub)
    monkeypatch.setattr(cycle_mod, "shelly_client", lambda: _RecordingShellyClient())

    plan = _plan(offset=+3.0)  # 20.5 + 3 = 23.5, clamped to 22.0
    gathered = {"resideo": _CurrentReading(setpoint_c=20.5)}
    await _apply_plan(plan, _policy(band_min=19.5, band_max=22.0), gathered)

    assert resideo_stub.writes == [pytest.approx(22.0)]


async def test_apply_plan_clamps_below_layer1_min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.optimizer import cycle as cycle_mod

    resideo_stub = _RecordingResideoClient()
    monkeypatch.setattr(cycle_mod, "resideo_client", lambda: resideo_stub)
    monkeypatch.setattr(cycle_mod, "shelly_client", lambda: _RecordingShellyClient())

    plan = _plan(offset=-3.0)  # 20.5 - 3 = 17.5, clamped to 19.5
    gathered = {"resideo": _CurrentReading(setpoint_c=20.5)}
    await _apply_plan(plan, _policy(band_min=19.5, band_max=22.0), gathered)

    assert resideo_stub.writes == [pytest.approx(19.5)]


async def test_apply_plan_skips_write_below_epsilon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.optimizer import cycle as cycle_mod

    resideo_stub = _RecordingResideoClient()
    monkeypatch.setattr(cycle_mod, "resideo_client", lambda: resideo_stub)
    monkeypatch.setattr(cycle_mod, "shelly_client", lambda: _RecordingShellyClient())

    # 0.1° offset < SETPOINT_EPSILON_C — no write should fire.
    plan = _plan(offset=SETPOINT_EPSILON_C - 0.05)
    gathered = {"resideo": _CurrentReading(setpoint_c=20.5)}
    await _apply_plan(plan, _policy(), gathered)

    assert resideo_stub.writes == []


async def test_apply_plan_skips_when_no_resideo_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.optimizer import cycle as cycle_mod

    resideo_stub = _RecordingResideoClient()
    monkeypatch.setattr(cycle_mod, "resideo_client", lambda: resideo_stub)
    monkeypatch.setattr(cycle_mod, "shelly_client", lambda: _RecordingShellyClient())

    plan = _plan(offset=+1.0)
    await _apply_plan(plan, _policy(), gathered={"resideo": None})

    assert resideo_stub.writes == []


async def test_apply_plan_swallows_setpoint_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Honeywell 5xx must NOT crash the cycle."""
    from src.optimizer import cycle as cycle_mod

    class _BoomClient:
        async def set_setpoint(self, target_c: float) -> None:
            raise resideo_mod.ResideoUnavailable("500 server error")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(cycle_mod, "resideo_client", lambda: _BoomClient())
    monkeypatch.setattr(cycle_mod, "shelly_client", lambda: _RecordingShellyClient())

    plan = _plan(offset=+0.5)
    gathered = {"resideo": _CurrentReading(setpoint_c=20.5)}
    # Should not raise.
    await _apply_plan(plan, _policy(), gathered)
