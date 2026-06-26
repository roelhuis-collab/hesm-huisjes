"""
Resideo (Honeywell Lyric T6) connector — read indoor temp + setpoint, write setpoint.

Auth uses the Resideo / Honeywell Home developer portal (developer.honeywellhome.com):
OAuth2 ``authorization_code`` flow with HTTP Basic at the token endpoint. The
interactive dance happens once via ``scripts/resideo_bootstrap.py``; the
resulting refresh token lives in Secret Manager as ``resideo-refresh-token`` and
Cloud Run swaps it for short-lived access tokens (~30 min) on demand.

Real-cloud + mock implementations live side by side. ``resideo_client()``
returns a real client when all three of ``RESIDEO_CLIENT_ID`` +
``RESIDEO_CLIENT_SECRET`` + ``RESIDEO_REFRESH_TOKEN`` are present, mock
otherwise — the optimizer cycle never branches on which one it gets, both
satisfy the ``ResideoClient`` protocol.

Roel's hardware: Honeywell Lyric T6 wired (Y6H810WF1005). The Total Connect
Comfort API surfaces it as a thermostat device under the user's location.
"""

from __future__ import annotations

import base64
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
# Constants — Honeywell Home / Resideo developer-portal endpoints.
# Source: developer.honeywellhome.com → My Apps → API documentation.
# ---------------------------------------------------------------------------

OAUTH2_AUTHORIZE_URL = "https://api.honeywell.com/oauth2/authorize"
OAUTH2_TOKEN_URL = "https://api.honeywell.com/oauth2/token"
API_BASE_URL = "https://api.honeywell.com/v2"

# Refresh access tokens slightly ahead of expiry to absorb clock skew.
ACCESS_TOKEN_EARLY_REFRESH_S = 60.0

# Honeywell Home returns Celsius when the location is in metric mode; we send
# Celsius back in writes. Roel's account is metric.
DEFAULT_THERMOSTAT_MODE = "Heat"
DEFAULT_HOLD_TYPE = "TemporaryHold"  # next scheduled period clears the override


# ---------------------------------------------------------------------------
# Subclassed exceptions — same shape as WeHeat / Growatt / ENTSO-E
# ---------------------------------------------------------------------------


class ResideoError(ConnectorError):
    """Base for Resideo-specific failures."""


class ResideoAuthError(ResideoError, ConnectorAuthError):
    """Missing OAuth credentials or expired refresh token."""


class ResideoUnavailable(ResideoError, ConnectorUnavailable):
    """Total Connect Comfort API down or 5xx."""


class ResideoMalformed(ResideoError, ConnectorMalformed):
    """200 OK with an unexpected body shape."""


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Mock — coherent synthetic data
# ---------------------------------------------------------------------------


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
        is_heating = indoor < self._setpoint - 0.3
        return ResideoStatus(
            captured_at=now,
            indoor_temp_c=indoor,
            setpoint_c=self._setpoint,
            humidity_pct=45.0
            + 8 * math.sin(2 * math.pi * hour / 24)
            + self._rng.uniform(-2, 2),
            is_heating=is_heating,
        )

    async def set_setpoint(self, target_c: float) -> None:
        self._setpoint = float(target_c)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# OAuth refresh helper — used by the real client + bootstrap script
# ---------------------------------------------------------------------------


@dataclass
class _AccessToken:
    value: str
    expires_at: float  # epoch seconds


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


async def _refresh_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[_AccessToken, str]:
    """Exchange a refresh token for a fresh access token via Honeywell.

    Returns ``(access_token, new_refresh_token)``. Honeywell *rotates*
    refresh tokens on each refresh — the caller should persist the new
    one back to Secret Manager so future cycles don't fall over. For the
    Cloud Run optimizer that means writing a new version of
    ``resideo-refresh-token`` after each refresh; we keep that out of
    this helper and surface the new value to the caller instead.

    ``transport`` is injectable so the unit tests can plug a
    ``MockTransport`` without monkey-patching.
    """
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Authorization": _basic_auth_header(client_id, client_secret),
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(transport=transport, timeout=15.0) as client:
        try:
            resp = await client.post(OAUTH2_TOKEN_URL, data=payload, headers=headers)
        except httpx.RequestError as exc:
            raise ResideoUnavailable(f"token endpoint unreachable: {exc}") from exc

    if resp.status_code in (400, 401, 403):
        raise ResideoAuthError(
            f"refresh failed ({resp.status_code}): {resp.text[:200]}"
        )
    if resp.status_code >= 500:
        raise ResideoUnavailable(f"token endpoint {resp.status_code}")
    if resp.status_code != 200:
        raise ResideoMalformed(f"unexpected status {resp.status_code}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise ResideoMalformed(f"token response not JSON: {exc}") from exc

    access = body.get("access_token")
    expires_in = body.get("expires_in")
    new_refresh = body.get("refresh_token", refresh_token)
    if not isinstance(access, str) or not isinstance(expires_in, (int, float)):
        raise ResideoMalformed("token response missing access_token / expires_in")
    if not isinstance(new_refresh, str):
        raise ResideoMalformed("token response missing refresh_token")
    return (
        _AccessToken(
            value=access,
            expires_at=time.time() + float(expires_in) - ACCESS_TOKEN_EARLY_REFRESH_S,
        ),
        new_refresh,
    )


# ---------------------------------------------------------------------------
# Real client — Honeywell Home v2 API
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Honeywell mixes ints/floats/None; safe conversion."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _RealResideoClient:
    """Thermostat client backed by the Honeywell Home v2 cloud.

    Discovers ``locationId`` and the first thermostat ``deviceId`` on
    first use; both stay stable across the process lifetime. Subsequent
    cycles reuse the cached ids.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        api_base_url: str = API_BASE_URL,
        token_transport: httpx.AsyncBaseTransport | None = None,
        api_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not client_id or not client_secret or not refresh_token:
            raise ResideoAuthError("Resideo client requires id + secret + refresh token")
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._api_base_url = api_base_url.rstrip("/")
        self._token_transport = token_transport
        self._api_transport = api_transport

        self._access: _AccessToken | None = None
        self._http: httpx.AsyncClient | None = None
        self._location_id: str | None = None
        self._device_id: str | None = None

    # --- async-context plumbing -----------------------------------------

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- HTTP helpers ----------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                transport=self._api_transport,
                timeout=15.0,
                base_url=self._api_base_url,
            )
        return self._http

    async def _ensure_token(self) -> str:
        if self._access is None or time.time() >= self._access.expires_at:
            self._access, new_refresh = await _refresh_access_token(
                self._refresh_token,
                self._client_id,
                self._client_secret,
                transport=self._token_transport,
            )
            # Persist the rotated refresh token in-process. The
            # Secret-Manager write-back happens in main.py via a hook
            # registered at startup; we don't reach into GCP from inside
            # the connector to keep it testable in isolation.
            self._refresh_token = new_refresh
        return self._access.value

    async def _api_get(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        token = await self._ensure_token()
        merged = {**params, "apikey": self._client_id}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = await self._client().get(path, params=merged, headers=headers)
        except httpx.RequestError as exc:
            raise ResideoUnavailable(f"GET {path} unreachable: {exc}") from exc
        return self._parse_json(resp, where=f"GET {path}")

    async def _api_post(
        self, path: str, *, params: dict[str, str], body: dict[str, Any]
    ) -> None:
        token = await self._ensure_token()
        merged = {**params, "apikey": self._client_id}
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._client().post(
                path, params=merged, headers=headers, json=body
            )
        except httpx.RequestError as exc:
            raise ResideoUnavailable(f"POST {path} unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise ResideoUnavailable(f"POST {path} {resp.status_code}")
        if resp.status_code in (401, 403):
            raise ResideoAuthError(f"POST {path} {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ResideoMalformed(
                f"POST {path} {resp.status_code}: {resp.text[:200]}"
            )

    def _parse_json(self, resp: httpx.Response, *, where: str) -> dict[str, Any]:
        if resp.status_code >= 500:
            raise ResideoUnavailable(f"{where} {resp.status_code}")
        if resp.status_code in (401, 403):
            raise ResideoAuthError(f"{where} {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ResideoMalformed(f"{where} {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ResideoMalformed(f"{where}: non-JSON body: {exc}") from exc
        if not isinstance(data, (dict, list)):
            raise ResideoMalformed(f"{where}: unexpected JSON shape")
        return data  # type: ignore[return-value]

    # --- bootstrap ------------------------------------------------------

    async def _discover(self) -> None:
        """Find the first location and the first thermostat under it."""
        data = await self._api_get("/locations", params={})
        if not isinstance(data, list) or not data:
            raise ResideoMalformed("no locations on account")
        first = data[0]
        if not isinstance(first, dict):
            raise ResideoMalformed("location record is not an object")
        loc_id = first.get("locationID") or first.get("locationId")
        if loc_id is None:
            raise ResideoMalformed("location record missing locationID")
        self._location_id = str(loc_id)

        devices = first.get("devices") or []
        thermos = [
            d
            for d in devices
            if isinstance(d, dict)
            and (d.get("deviceClass") == "Thermostat" or "thermostat" in str(d.get("deviceType", "")).lower())
        ]
        if not thermos:
            raise ResideoMalformed("no thermostat in first location")
        dev = thermos[0]
        dev_id = dev.get("deviceID") or dev.get("deviceId")
        if dev_id is None:
            raise ResideoMalformed("thermostat record missing deviceID")
        self._device_id = str(dev_id)

    async def _ensure_ids(self) -> tuple[str, str]:
        if self._location_id is None or self._device_id is None:
            await self._discover()
        assert self._location_id is not None
        assert self._device_id is not None
        return self._location_id, self._device_id

    # --- public API -----------------------------------------------------

    async def get_status(self) -> ResideoStatus:
        loc, dev = await self._ensure_ids()
        data = await self._api_get(
            f"/devices/thermostats/{dev}",
            params={"locationId": loc},
        )
        if not isinstance(data, dict):
            raise ResideoMalformed("thermostat detail not an object")

        indoor = _as_float(data.get("indoorTemperature"))
        humidity = _as_float(data.get("indoorHumidity"))

        changeable = data.get("changeableValues") or {}
        if not isinstance(changeable, dict):
            changeable = {}
        setpoint = _as_float(changeable.get("heatSetpoint"))
        operation = data.get("operationStatus") or {}
        if not isinstance(operation, dict):
            operation = {}
        mode_raw = str(operation.get("mode") or "").lower()
        # Honeywell reports "EquipmentOff", "Heat", "Cool", "Idle" depending on
        # equipment; we count anything that's actively calling for heat.
        is_heating = "heat" in mode_raw and "off" not in mode_raw

        if indoor is None:
            raise ResideoMalformed("thermostat missing indoorTemperature")
        if setpoint is None:
            raise ResideoMalformed("thermostat missing heatSetpoint")

        return ResideoStatus(
            captured_at=datetime.now(),
            indoor_temp_c=indoor,
            setpoint_c=setpoint,
            humidity_pct=humidity,
            is_heating=is_heating,
        )

    async def set_setpoint(self, target_c: float) -> None:
        loc, dev = await self._ensure_ids()
        target = round(float(target_c), 1)
        body = {
            "mode": DEFAULT_THERMOSTAT_MODE,
            "heatSetpoint": target,
            "thermostatSetpointStatus": DEFAULT_HOLD_TYPE,
        }
        await self._api_post(
            f"/devices/thermostats/{dev}",
            params={"locationId": loc},
            body=body,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resideo_client() -> ResideoClient:
    """Return a real client when all three env vars are set, mock otherwise."""
    cid = os.environ.get("RESIDEO_CLIENT_ID", "").strip()
    sec = os.environ.get("RESIDEO_CLIENT_SECRET", "").strip()
    refresh = os.environ.get("RESIDEO_REFRESH_TOKEN", "").strip()
    if cid and sec and refresh:
        return _RealResideoClient(cid, sec, refresh)
    return MockResideoClient()


def is_using_mock_resideo() -> bool:
    return not (
        os.environ.get("RESIDEO_CLIENT_ID", "").strip()
        and os.environ.get("RESIDEO_CLIENT_SECRET", "").strip()
        and os.environ.get("RESIDEO_REFRESH_TOKEN", "").strip()
    )
