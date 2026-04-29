"""
Shelly Cloud connector — Pro 2PM relay controlling the dompelaar.

Read state + power, write on/off. Real cloud client sealed until
``SHELLY_AUTH_KEY`` is supplied; mock otherwise.
"""

from __future__ import annotations

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


class ShellyError(ConnectorError):
    """Base for Shelly-specific failures."""


class ShellyAuthError(ShellyError, ConnectorAuthError):
    """Missing or rejected Shelly Cloud auth key."""


class ShellyUnavailable(ShellyError, ConnectorUnavailable):
    """Shelly cloud down."""


class ShellyMalformed(ShellyError, ConnectorMalformed):
    """200 OK with an unexpected body shape."""


@dataclass
class ShellyStatus:
    captured_at: datetime
    is_on: bool
    power_w: float


class ShellyClient(Protocol):
    async def get_status(self) -> ShellyStatus: ...
    async def set_relay(self, on: bool) -> None: ...
    async def aclose(self) -> None: ...


class MockShellyClient:
    """Mock dompelaar — off by default, draws ~3 kW when on."""

    def __init__(self) -> None:
        self._on = False
        self._rng = random.Random(0xDEADBEEF)

    async def get_status(self) -> ShellyStatus:
        return ShellyStatus(
            captured_at=datetime.now(),
            is_on=self._on,
            power_w=3000 + self._rng.uniform(-30, 30) if self._on else 0.0,
        )

    async def set_relay(self, on: bool) -> None:
        self._on = bool(on)

    async def aclose(self) -> None:
        return None


class _RealShellyClient:
    def __init__(self, auth_key: str) -> None:
        self._auth_key = auth_key

    async def get_status(self) -> ShellyStatus:
        raise NotImplementedError(
            "Real Shelly Cloud client not wired yet — auth key pending."
        )

    async def set_relay(self, on: bool) -> None:
        raise NotImplementedError(
            "Real Shelly Cloud client not wired yet — auth key pending."
        )

    async def aclose(self) -> None:
        return None


def shelly_client() -> ShellyClient:
    auth = os.environ.get("SHELLY_AUTH_KEY", "").strip()
    if auth:
        return _RealShellyClient(auth)
    return MockShellyClient()


def is_using_mock_shelly() -> bool:
    return not os.environ.get("SHELLY_AUTH_KEY", "").strip()
