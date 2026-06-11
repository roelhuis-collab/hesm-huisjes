"""
WeHeat connector — read-only heat pump telemetry.

The WeHeat public ``third_party`` API exposes only ``GET`` endpoints — no
setpoint writes, no on/off, no manual mode. The HA integration is for
the same reason ``iot_class: cloud_polling``. Our optimizer therefore
treats WeHeat purely as a sensor; the only DHW lever we control is the
3 kW immersion heater on the Shelly contactor.

Auth uses the WeHeat Keycloak realm (``auth.weheat.nl``) with the
public OAuth client ``HomeAssistantAPI`` that ships with Home Assistant.
The interactive ``authorization_code + PKCE`` dance happens once via
``scripts/weheat_bootstrap.py``; the resulting refresh token lives in
Secret Manager and Cloud Run swaps it for short-lived access tokens.

Real-cloud + mock implementations live side-by-side. ``weheat_client()``
returns a real client when ``WEHEAT_REFRESH_TOKEN`` is present and a
mock otherwise — the optimizer cycle never branches on which one it
gets, both satisfy the ``WeHeatClient`` protocol.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)

# ---------------------------------------------------------------------------
# Constants — match the HA WeHeat integration so we use the same OAuth
# client as every Home Assistant install on the planet. These IDs are
# public; they live in the HA application_credentials docs.
# ---------------------------------------------------------------------------

OAUTH2_TOKEN_URL = (
    "https://auth.weheat.nl/auth/realms/Weheat/protocol/openid-connect/token/"
)
OAUTH2_AUTHORIZE_URL = (
    "https://auth.weheat.nl/auth/realms/Weheat/protocol/openid-connect/auth/"
)
API_BASE_URL = "https://api.weheat.nl/third_party"
DEFAULT_CLIENT_ID = "HomeAssistantAPI"
# Public HA OAuth client secret — shipped with every HA install. Not a real secret.
DEFAULT_CLIENT_SECRET = "TqpNpiJDKbGXF8jaL9D1Y8yzl1pI1Fly"
SCOPES = "openid offline_access"

# Refresh access tokens slightly ahead of expiry to absorb clock skew.
ACCESS_TOKEN_EARLY_REFRESH_S = 60.0


# ---------------------------------------------------------------------------
# Subclassed exceptions — same shape as HomeWizard / ENTSO-E / Open-Meteo
# ---------------------------------------------------------------------------


class WeHeatError(ConnectorError):
    """Base for WeHeat-specific failures."""


class WeHeatAuthError(WeHeatError, ConnectorAuthError):
    """Missing / rejected OAuth credentials or refresh token."""


class WeHeatUnavailable(WeHeatError, ConnectorUnavailable):
    """WeHeat cloud down, network blip, 5xx."""


class WeHeatMalformed(WeHeatError, ConnectorMalformed):
    """200 OK but body did not match expected schema."""


# ---------------------------------------------------------------------------
# Response shape — read-only
# ---------------------------------------------------------------------------


@dataclass
class WeHeatStatus:
    """One snapshot of heat pump + DHW state.

    All fields are SI units: power in W, temperature in °C, COP unitless.
    Many fields are ``None`` when the pump is in standby or the WeHeat
    cloud has not received a recent log frame — callers must tolerate.
    """

    captured_at: datetime
    is_running: bool
    hp_power_w: float
    hp_thermal_w: float | None
    cop: float | None
    boiler_temp_c: float
    buffer_temp_c: float
    flow_temp_c: float
    return_temp_c: float
    indoor_temp_c: float | None
    indoor_setpoint_c: float | None
    compressor_pct: int | None
    state: str | None  # raw heat_pump_state name (HEATING / DHW / STANDBY / …)


# ---------------------------------------------------------------------------
# Client protocol — both real + mock implement this
# ---------------------------------------------------------------------------


class WeHeatClient(Protocol):
    async def get_status(self) -> WeHeatStatus: ...
    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Mock — coherent synthetic data
# ---------------------------------------------------------------------------


class MockWeHeatClient:
    """Synthetic WeHeat status that follows a realistic daily pattern.

    Heat pump runs more in the early morning (preheat) and evening
    (occupancy). COP varies sinusoidally. Field shapes match what the
    real client returns so the optimizer + dashboard cope identically.
    """

    def __init__(self) -> None:
        self._rng = random.Random(0xC0FFEE)  # deterministic enough for fake data

    async def get_status(self) -> WeHeatStatus:
        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        # Heat-pump duty cycle: peak 06:00 + 17:00, low at 13:00.
        duty = 0.5 + 0.5 * math.sin(2 * math.pi * (hour - 6) / 24)
        is_running = duty > 0.55

        hp_power = 0.0
        hp_thermal: float | None = None
        cop: float | None = None
        compressor_pct: int | None = None
        state = "STANDBY"
        if is_running:
            hp_power = 1500 + 1500 * duty + self._rng.uniform(-150, 150)
            cop = 4.5 - 0.6 * (1 - duty) + self._rng.uniform(-0.15, 0.15)
            hp_thermal = hp_power * cop
            compressor_pct = int(50 + 40 * duty)
            state = "HEATING" if hour < 23 else "DHW"

        return WeHeatStatus(
            captured_at=now,
            is_running=is_running,
            hp_power_w=hp_power,
            hp_thermal_w=hp_thermal,
            cop=cop,
            boiler_temp_c=53.0 + 4 * duty + self._rng.uniform(-0.6, 0.6),
            buffer_temp_c=36.0 + 6 * duty + self._rng.uniform(-0.4, 0.4),
            flow_temp_c=33.0 + 8 * duty + self._rng.uniform(-0.3, 0.3),
            return_temp_c=29.0 + 6 * duty + self._rng.uniform(-0.3, 0.3),
            indoor_temp_c=20.5 + self._rng.uniform(-0.3, 0.3),
            indoor_setpoint_c=20.5,
            compressor_pct=compressor_pct,
            state=state,
        )

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# OAuth refresh helper — used only by the real client
# ---------------------------------------------------------------------------


@dataclass
class _AccessToken:
    value: str
    expires_at: float  # epoch seconds


async def _refresh_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> _AccessToken:
    """Exchange a refresh token for a fresh access token via Keycloak.

    ``transport`` is injectable so the unit tests can plug a
    ``MockTransport`` without monkey-patching.
    """
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    async with httpx.AsyncClient(transport=transport, timeout=15.0) as client:
        try:
            resp = await client.post(OAUTH2_TOKEN_URL, data=payload)
        except httpx.RequestError as exc:
            raise WeHeatUnavailable(f"token endpoint unreachable: {exc}") from exc

    if resp.status_code in (400, 401, 403):
        # Keycloak returns ``{"error": "invalid_grant", ...}`` when the
        # refresh token has been revoked or expired.
        raise WeHeatAuthError(
            f"refresh failed ({resp.status_code}): {resp.text[:200]}"
        )
    if resp.status_code >= 500:
        raise WeHeatUnavailable(f"token endpoint {resp.status_code}")
    if resp.status_code != 200:
        raise WeHeatMalformed(f"unexpected status {resp.status_code}")

    body = resp.json()
    access = body.get("access_token")
    expires_in = body.get("expires_in")
    if not isinstance(access, str) or not isinstance(expires_in, int):
        raise WeHeatMalformed("token response missing access_token / expires_in")
    return _AccessToken(
        value=access,
        expires_at=time.time() + float(expires_in) - ACCESS_TOKEN_EARLY_REFRESH_S,
    )


# ---------------------------------------------------------------------------
# Real client — uses the official `weheat` SDK against the third_party API
# ---------------------------------------------------------------------------


class _RealWeHeatClient:
    """Read-only WeHeat client.

    The first ``get_status()`` call discovers heat pumps registered to
    the user, picks the first online one, and caches its UUID for
    subsequent calls. Logs are fetched fresh every cycle.
    """

    def __init__(
        self,
        refresh_token: str,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
        api_base_url: str = API_BASE_URL,
        token_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_base_url = api_base_url
        self._token_transport = token_transport
        self._access: _AccessToken | None = None
        self._aiohttp_session: Any | None = None
        self._heat_pump: Any | None = None
        self._heat_pump_uuid: str | None = None

    async def _ensure_token(self) -> str:
        if self._access is None or time.time() >= self._access.expires_at:
            self._access = await _refresh_access_token(
                self._refresh_token,
                self._client_id,
                self._client_secret,
                transport=self._token_transport,
            )
        return self._access.value

    async def _ensure_session(self) -> Any:
        if self._aiohttp_session is None:
            # Imported lazily so the optimizer process doesn't pay aiohttp
            # init cost when it only needs the mock client.
            import aiohttp

            self._aiohttp_session = aiohttp.ClientSession()
        return self._aiohttp_session

    async def _ensure_pump(self, token: str) -> Any:
        if self._heat_pump is not None:
            return self._heat_pump
        from weheat.abstractions.discovery import HeatPumpDiscovery
        from weheat.abstractions.heat_pump import HeatPump

        session = await self._ensure_session()
        try:
            pumps = await HeatPumpDiscovery.async_discover_active(
                self._api_base_url, token, session
            )
        except Exception as exc:
            raise WeHeatUnavailable(f"discovery failed: {exc}") from exc
        if not pumps:
            raise WeHeatUnavailable("no active heat pumps for this account")
        first = pumps[0]
        self._heat_pump_uuid = first.uuid
        self._heat_pump = HeatPump(self._api_base_url, first.uuid, session)
        return self._heat_pump

    async def get_status(self) -> WeHeatStatus:
        token = await self._ensure_token()
        pump = await self._ensure_pump(token)
        try:
            await pump.async_get_logs(token)
        except Exception as exc:
            raise WeHeatUnavailable(f"log fetch failed: {exc}") from exc

        hp_power = float(pump.power_input or 0.0)
        thermal = pump.power_output
        cop = pump.cop
        state_enum = pump.heat_pump_state
        state_name = state_enum.name if state_enum is not None else None
        is_running = hp_power > 50.0

        return WeHeatStatus(
            captured_at=datetime.now(),
            is_running=is_running,
            hp_power_w=hp_power,
            hp_thermal_w=float(thermal) if thermal is not None else None,
            cop=float(cop) if cop is not None else None,
            boiler_temp_c=float(pump.dhw_top_temperature or 0.0),
            buffer_temp_c=float(pump.dhw_bottom_temperature or 0.0),
            flow_temp_c=float(pump.water_outlet_temperature or 0.0),
            return_temp_c=float(pump.water_inlet_temperature or 0.0),
            indoor_temp_c=(
                float(pump.thermostat_room_temperature)
                if pump.thermostat_room_temperature is not None
                else None
            ),
            indoor_setpoint_c=(
                float(pump.thermostat_room_temperature_setpoint)
                if pump.thermostat_room_temperature_setpoint is not None
                else None
            ),
            compressor_pct=(
                int(pump.compressor_percentage)
                if pump.compressor_percentage is not None
                else None
            ),
            state=state_name,
        )

    async def aclose(self) -> None:
        if self._aiohttp_session is not None:
            await self._aiohttp_session.close()
            self._aiohttp_session = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def weheat_client() -> WeHeatClient:
    """Return a real client when a refresh token is set, mock otherwise.

    The optimizer cycle never branches on which one it gets — both
    satisfy the ``WeHeatClient`` protocol.
    """
    refresh = os.environ.get("WEHEAT_REFRESH_TOKEN", "").strip()
    if not refresh:
        return MockWeHeatClient()
    cid = os.environ.get("WEHEAT_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID
    sec = os.environ.get("WEHEAT_CLIENT_SECRET", "").strip() or DEFAULT_CLIENT_SECRET
    return _RealWeHeatClient(refresh, client_id=cid, client_secret=sec)


def is_using_mock_weheat() -> bool:
    """Convenience for /health to surface whether the connector is real."""
    return not os.environ.get("WEHEAT_REFRESH_TOKEN", "").strip()
