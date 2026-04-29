"""
Resideo (Honeywell Lyric T6) connector — read indoor temp + setpoint, write setpoint.

Same pattern as WeHeat: ``resideo_client()`` returns a real OAuth2
client when ``RESIDEO_CLIENT_ID`` + ``RESIDEO_CLIENT_SECRET`` are set,
mock otherwise. Real client sealed until we have access to
developer.honeywellhome.com.
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


class ResideoError(ConnectorError):
    """Base for Resideo-specific failures."""


class ResideoAuthError(ResideoError, ConnectorAuthError):
    """Missing OAuth credentials or expired refresh token."""


class ResideoUnavailable(ResideoError, ConnectorUnavailable):
    """Total Connect Comfort API down or 5xx."""


class ResideoMalformed(ResideoError, ConnectorMalformed):
    """200 OK with an unexpected body shape."""


@dataclass
class ResideoStatus:
    captured_at: datetime
    indoor_temp_c: float
    setpoint_c: float
    humidity_pct: float | None
    is_heating: bool


class ResideoClient(Protocol):
    async def get_status(self) -> ResideoStatus: ...
    async def set_setpoint(self, target_c: float) -> None: ...
    async def aclose(self) -> None: ...


class MockResideoClient:
    """Synthetic indoor temp following a sinusoidal wake/leave pattern."""

    def __init__(self) -> None:
        self._setpoint = 20.5
        self._rng = random.Random(0xC0DE01)

    async def get_status(self) -> ResideoStatus:
        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        # Indoor follows setpoint with small lag; a touch lower at night.
        night_dip = -0.4 if (hour < 6 or hour > 23) else 0
        indoor = self._setpoint + night_dip + self._rng.uniform(-0.25, 0.25)
        # Heating active when indoor < setpoint - 0.3
        is_heating = indoor < self._setpoint - 0.3
        return ResideoStatus(
            captured_at=now,
            indoor_temp_c=indoor,
            setpoint_c=self._setpoint,
            humidity_pct=45.0 + 8 * math.sin(2 * math.pi * hour / 24)
            + self._rng.uniform(-2, 2),
            is_heating=is_heating,
        )

    async def set_setpoint(self, target_c: float) -> None:
        self._setpoint = float(target_c)

    async def aclose(self) -> None:
        return None


class _RealResideoClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    async def get_status(self) -> ResideoStatus:
        raise NotImplementedError(
            "Real Resideo client not wired yet — Total Connect Comfort OAuth pending."
        )

    async def set_setpoint(self, target_c: float) -> None:
        raise NotImplementedError(
            "Real Resideo client not wired yet — Total Connect Comfort OAuth pending."
        )

    async def aclose(self) -> None:
        return None


def resideo_client() -> ResideoClient:
    cid = os.environ.get("RESIDEO_CLIENT_ID", "").strip()
    sec = os.environ.get("RESIDEO_CLIENT_SECRET", "").strip()
    if cid and sec:
        return _RealResideoClient(cid, sec)
    return MockResideoClient()


def is_using_mock_resideo() -> bool:
    return not (
        os.environ.get("RESIDEO_CLIENT_ID", "").strip()
        and os.environ.get("RESIDEO_CLIENT_SECRET", "").strip()
    )
