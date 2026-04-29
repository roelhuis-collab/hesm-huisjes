"""
Growatt connector — PV inverter production + per-phase power.

Real ShinePhone-cloud poll requires ``GROWATT_USERNAME`` +
``GROWATT_PASSWORD``. Mock returns a sun-curve based on time of day.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)


class GrowattError(ConnectorError):
    """Base for Growatt-specific failures."""


class GrowattAuthError(GrowattError, ConnectorAuthError):
    """Missing or rejected ShinePhone credentials."""


class GrowattUnavailable(GrowattError, ConnectorUnavailable):
    """ShinePhone cloud down."""


class GrowattMalformed(GrowattError, ConnectorMalformed):
    """200 OK with an unexpected body shape."""


# Roel's array: 26 panels × ~400 W ≈ 9 kW peak DC; inverter caps at 9 kW AC.
PEAK_PV_W = 7500.0


@dataclass
class GrowattStatus:
    """One snapshot of PV inverter output."""

    captured_at: datetime
    pv_power_w: float
    daily_yield_kwh: float
    power_l1_w: float
    power_l2_w: float
    power_l3_w: float


class GrowattClient(Protocol):
    async def get_status(self) -> GrowattStatus: ...
    async def aclose(self) -> None: ...


def _solar_factor(hour: float) -> float:
    """Fake solar elevation: sine over 06:00–20:00, peak at 13:00."""
    if hour < 6 or hour > 20:
        return 0.0
    # sin curve over the daylight window
    return max(0.0, math.sin(math.pi * (hour - 6) / 14))


class MockGrowattClient:
    """Synthetic PV output following a sun curve."""

    def __init__(self) -> None:
        self._rng = random.Random(0x501A8)

    async def get_status(self) -> GrowattStatus:
        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        factor = _solar_factor(hour)
        # Random cloud bursts shave 0–40% off when sun is up.
        if factor > 0:
            factor *= 1.0 - 0.4 * self._rng.random()

        total = PEAK_PV_W * factor + self._rng.uniform(-30, 30) if factor > 0 else 0.0
        per_phase = total / 3.0
        # Crude daily-yield accumulator: integrates the sin curve so far.
        # Good-enough for the dashboard; replaced by real reading later.
        elapsed_factor = max(0.0, math.sin(math.pi * (hour - 6) / 14))
        daily_yield = PEAK_PV_W / 1000.0 * 7.0 * (1 - math.cos(math.pi * elapsed_factor))

        return GrowattStatus(
            captured_at=now,
            pv_power_w=max(0.0, total),
            daily_yield_kwh=daily_yield,
            power_l1_w=per_phase + self._rng.uniform(-15, 15),
            power_l2_w=per_phase + self._rng.uniform(-15, 15),
            power_l3_w=per_phase + self._rng.uniform(-15, 15),
        )

    async def aclose(self) -> None:
        return None


class _RealGrowattClient:
    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password

    async def get_status(self) -> GrowattStatus:
        raise NotImplementedError(
            "Real Growatt client not wired yet — ShinePhone poll pending."
        )

    async def aclose(self) -> None:
        return None


def growatt_client() -> GrowattClient:
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if user and pwd:
        return _RealGrowattClient(user, pwd)
    return MockGrowattClient()


def is_using_mock_growatt() -> bool:
    return not (
        os.environ.get("GROWATT_USERNAME", "").strip()
        and os.environ.get("GROWATT_PASSWORD", "").strip()
    )
