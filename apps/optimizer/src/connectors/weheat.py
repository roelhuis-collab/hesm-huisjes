"""
WeHeat connector — heat pump status + boiler control.

Real-cloud + mock implementations live side-by-side. ``weheat_client()``
returns a real client when ``WEHEAT_CLIENT_ID`` + ``WEHEAT_CLIENT_SECRET``
are both present in the env, and a mock otherwise. This lets the
optimizer cycle run end-to-end on staging/dev while we wait for vendor
API access — every dashboard graph still animates with coherent data.

The real OAuth2 flow is sketched but not wired against an actual
``WeHeat`` endpoint yet — sealed by ``NotImplementedError`` until we
have credentials and can test against the live cloud. Mock returns
synthetic data shaped like a typical residential WeHeat Blackbird P80.
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

# ---------------------------------------------------------------------------
# Subclassed exceptions — same shape as HomeWizard / ENTSO-E / Open-Meteo
# ---------------------------------------------------------------------------


class WeHeatError(ConnectorError):
    """Base for WeHeat-specific failures."""


class WeHeatAuthError(WeHeatError, ConnectorAuthError):
    """Missing / rejected OAuth client credentials."""


class WeHeatUnavailable(WeHeatError, ConnectorUnavailable):
    """WeHeat cloud down, network blip, 5xx."""


class WeHeatMalformed(WeHeatError, ConnectorMalformed):
    """200 OK but body did not match expected schema."""


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


@dataclass
class WeHeatStatus:
    """One snapshot of the heat pump + DHW boiler.

    All fields are SI: power in W, temperature in °C, COP unitless.
    """

    captured_at: datetime
    is_running: bool
    hp_power_w: float
    cop: float | None
    boiler_temp_c: float
    buffer_temp_c: float
    flow_temp_c: float
    return_temp_c: float
    setpoint_c: float
    dhw_setpoint_c: float


# ---------------------------------------------------------------------------
# Client protocol — both real + mock implement this
# ---------------------------------------------------------------------------


class WeHeatClient(Protocol):
    async def get_status(self) -> WeHeatStatus: ...
    async def set_dhw_setpoint(self, target_c: float) -> None: ...
    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Mock — coherent synthetic data
# ---------------------------------------------------------------------------


class MockWeHeatClient:
    """Synthetic WeHeat status that follows a realistic daily pattern.

    Heat pump runs more in the early morning (preheat) and evening
    (occupancy). COP varies sinusoidally with outdoor temp proxy.
    Setpoints sit in plausible ranges so the dashboard UI looks alive.
    """

    def __init__(self) -> None:
        self._dhw_setpoint = 55.0
        self._rng = random.Random(0xC0FFEE)  # deterministic enough for fake data

    async def get_status(self) -> WeHeatStatus:
        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        # Heat-pump duty cycle: peak 06:00 + 17:00, low at 13:00.
        duty = 0.5 + 0.5 * math.sin(2 * math.pi * (hour - 6) / 24)
        is_running = duty > 0.55

        # Power scales with duty, jittered.
        hp_power = 0.0
        if is_running:
            hp_power = 1500 + 1500 * duty + self._rng.uniform(-150, 150)
        cop = 4.5 - 0.6 * (1 - duty) + self._rng.uniform(-0.15, 0.15)

        return WeHeatStatus(
            captured_at=now,
            is_running=is_running,
            hp_power_w=hp_power,
            cop=cop if is_running else None,
            boiler_temp_c=53.0 + 4 * duty + self._rng.uniform(-0.6, 0.6),
            buffer_temp_c=36.0 + 6 * duty + self._rng.uniform(-0.4, 0.4),
            flow_temp_c=33.0 + 8 * duty + self._rng.uniform(-0.3, 0.3),
            return_temp_c=29.0 + 6 * duty + self._rng.uniform(-0.3, 0.3),
            setpoint_c=20.5,
            dhw_setpoint_c=self._dhw_setpoint,
        )

    async def set_dhw_setpoint(self, target_c: float) -> None:
        # Server-side Layer-1 already clamps; just record it.
        self._dhw_setpoint = float(target_c)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Real client — sealed off until WeHeat credentials arrive
# ---------------------------------------------------------------------------


class _RealWeHeatClient:
    """OAuth2 client_credentials flow against the WeHeat cloud.

    Sealed with ``NotImplementedError`` pending vendor API access. Wire
    this up once `WEHEAT_CLIENT_ID` + `WEHEAT_CLIENT_SECRET` are real.
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    async def get_status(self) -> WeHeatStatus:
        raise NotImplementedError(
            "Real WeHeat client not wired yet — vendor credentials pending."
        )

    async def set_dhw_setpoint(self, target_c: float) -> None:
        raise NotImplementedError(
            "Real WeHeat client not wired yet — vendor credentials pending."
        )

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def weheat_client() -> WeHeatClient:
    """Return a real client when creds are set, mock otherwise.

    The optimizer cycle never branches on which one it gets — both
    satisfy the WeHeatClient protocol.
    """
    cid = os.environ.get("WEHEAT_CLIENT_ID", "").strip()
    sec = os.environ.get("WEHEAT_CLIENT_SECRET", "").strip()
    if cid and sec:
        return _RealWeHeatClient(cid, sec)
    return MockWeHeatClient()


def is_using_mock_weheat() -> bool:
    """Convenience for /health to surface whether the connector is real."""
    return not (
        os.environ.get("WEHEAT_CLIENT_ID", "").strip()
        and os.environ.get("WEHEAT_CLIENT_SECRET", "").strip()
    )
