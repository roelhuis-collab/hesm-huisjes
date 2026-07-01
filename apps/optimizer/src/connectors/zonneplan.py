"""
Zonneplan connector — P1 net power + dynamic tariff + PV production.

Zonneplan is Roel's electricity supplier from 7 jul 2026, replacing the
originally-planned Tibber/Frank/EnergyZero switch. A Zonneplan Connect
dongle sits on the P1 port next to the existing HomeWizard dongle (via
splitter). Zonneplan's cloud is publicly reachable from Cloud Run — no
LAN tunnel, no Raspberry Pi — which retires the parked HomeWizard
tunnel plan.

The connector collapses three data sources into one API call:
  1. **P1** — active grid power (W), total import/export registers (kWh).
  2. **Dynamic tariff** — Zonneplan's actual retail all-in €/kWh for the
     current 15-min interval. Replaces the ENTSO-E retail-markup
     formula ``((spot/1000) + 0.1108 + 0.025) * 1.21`` — no more
     guessing markup.
  3. **PV** — total PV output (W) + today's yield (kWh). Under the hood
     Zonneplan reads Roel's Growatt inverter; per-phase power is not
     exposed but the optimizer only uses totals anyway.

Auth flow — magic link + rotating bearer:
  * The Zonneplan mobile-app / web-app auth API accepts email → sends a
    magic link to that address → the link contains a one-time token →
    the token is exchanged (with a device UUID) for an access + refresh
    token. See ``scripts/zonneplan_bootstrap.py`` for the one-shot
    dance. Cloud Run stores the pair in Secret Manager
    (``zonneplan-access-token`` + ``zonneplan-refresh-token``) and
    refreshes headless when the access token expires.

Real-cloud + mock implementations live side by side. ``zonneplan_client()``
returns a real client when a bearer token is set, mock otherwise — the
optimizer cycle never branches on which one it gets.
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
# Constants — Zonneplan app API.
# Endpoints are the ones the community Home-Assistant integration
# (kansspel/ha-zonneplan) uses successfully; final validation happens
# in situ once Roel's account is live.
# ---------------------------------------------------------------------------

API_BASE_URL = "https://app-api.zonneplan.nl"
AUTH_REQUEST_PATH = "/auth/request"
AUTH_TOKEN_PATH = "/auth/login-with-token"
AUTH_REFRESH_PATH = "/auth/refresh"
USER_ME_PATH = "/user/me"
LIVE_CONSUMPTION_PATH = "/user/connection/{uuid}/consumption/live"
CURRENT_TARIFF_PATH = "/user/connection/{uuid}/electricity-prices/current"
PV_LIVE_PATH = "/user/pv/{uuid}"

ACCESS_TOKEN_EARLY_REFRESH_S = 60.0

# Rough Sittard peak — 26 panels × ~350 W usable = ~9 kW DC; inverter
# caps at 9 kW AC. Used only by the mock's synthetic sun curve.
PEAK_PV_W = 7500.0
DEFAULT_HOUSE_LOAD_W = 400.0


# ---------------------------------------------------------------------------
# Subclassed exceptions — same shape as WeHeat / Resideo / ENTSO-E
# ---------------------------------------------------------------------------


class ZonneplanError(ConnectorError):
    """Base for Zonneplan-specific failures."""


class ZonneplanAuthError(ZonneplanError, ConnectorAuthError):
    """Missing or rejected token."""


class ZonneplanUnavailable(ZonneplanError, ConnectorUnavailable):
    """Zonneplan cloud unreachable or 5xx."""


class ZonneplanMalformed(ZonneplanError, ConnectorMalformed):
    """200 OK but body did not match expected shape."""


# ---------------------------------------------------------------------------
# Response shape — one snapshot the optimizer cares about
# ---------------------------------------------------------------------------


@dataclass
class ZonneplanStatus:
    """One snapshot of P1 + tariff + PV.

    All power values in W, energy in kWh, tariff in EUR/kWh (all-in,
    VAT-inclusive — the price Roel actually pays / receives).
    """

    captured_at: datetime

    # P1 — smart meter
    active_power_w: float          # signed: positive = importing from grid
    total_import_kwh: float
    total_export_kwh: float

    # Tariff — dynamic day-ahead retail
    tariff_all_in_eur_kwh: float   # buy price
    feedin_all_in_eur_kwh: float | None  # sell price (post-saldering only)

    # PV
    pv_power_w: float
    pv_yield_today_kwh: float


class ZonneplanClient(Protocol):
    async def get_status(self) -> ZonneplanStatus: ...
    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Mock — coherent synthetic data
# ---------------------------------------------------------------------------


def _solar_factor(hour: float) -> float:
    """Sinusoidal solar elevation: peak ~13:00, zero outside 06-20."""
    if hour < 6 or hour > 20:
        return 0.0
    return max(0.0, math.sin(math.pi * (hour - 6) / 14))


def _tariff_curve(hour: float) -> float:
    """Typical NL dynamic-tariff shape: mid-day dip, evening peak."""
    base = 0.28
    morning_peak = 0.10 * math.exp(-(((hour - 7.5) / 1.5) ** 2))
    evening_peak = 0.14 * math.exp(-(((hour - 18.0) / 1.5) ** 2))
    midday_dip = -0.09 * math.exp(-(((hour - 13.0) / 2.5) ** 2))
    return base + morning_peak + evening_peak + midday_dip


class MockZonneplanClient:
    """Synthetic P1 + tariff + PV following a realistic Dutch daily curve.

    Import/export registers drift monotonically over the process
    lifetime so integration tests that sample twice see plausible
    positive deltas.
    """

    def __init__(self) -> None:
        self._rng = random.Random(0x2071AE)
        self._import_total = 4823.0
        self._export_total = 3921.0

    async def get_status(self) -> ZonneplanStatus:
        now = datetime.now()
        hour = now.hour + now.minute / 60.0

        pv = PEAK_PV_W * _solar_factor(hour)
        pv += self._rng.uniform(-40, 40) if pv > 0 else 0.0
        pv = max(0.0, pv)

        # House draws ~400W baseload with wake/leave bumps.
        house = DEFAULT_HOUSE_LOAD_W + 250 * math.exp(-(((hour - 7.5) / 1.2) ** 2))
        house += 200 * math.exp(-(((hour - 19.0) / 1.5) ** 2))
        house += self._rng.uniform(-60, 60)

        net = house - pv  # positive = importing
        # Nudge counters based on ~1 quarter of running at this power.
        quarter_kwh = abs(net) * 0.25 / 1000.0
        if net > 0:
            self._import_total += quarter_kwh
        else:
            self._export_total += quarter_kwh

        # Rough NL summer PV yield today: integrate solar_factor since 06:00.
        elapsed = max(0.0, hour - 6.0)
        yield_today = (PEAK_PV_W / 1000.0) * 7.0 * (1 - math.cos(math.pi * min(1.0, elapsed / 14.0)))

        tariff = _tariff_curve(hour)
        # Post-saldering feed-in is a fraction of buy price; mock as 65%.
        feedin: float | None = round(tariff * 0.65, 4)

        return ZonneplanStatus(
            captured_at=now,
            active_power_w=round(net, 1),
            total_import_kwh=round(self._import_total, 3),
            total_export_kwh=round(self._export_total, 3),
            tariff_all_in_eur_kwh=round(tariff, 4),
            feedin_all_in_eur_kwh=feedin,
            pv_power_w=round(pv, 1),
            pv_yield_today_kwh=round(yield_today, 3),
        )

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# OAuth refresh helper
# ---------------------------------------------------------------------------


@dataclass
class _AccessToken:
    value: str
    expires_at: float  # epoch seconds


async def _refresh_access_token(
    refresh_token: str,
    device_uuid: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[_AccessToken, str]:
    """Exchange a refresh token for a fresh access token.

    Returns ``(access_token, new_refresh_token)``. Zonneplan rotates
    the refresh token on every use; the caller should keep the new one
    for the next refresh cycle.
    """
    payload = {"refresh_token": refresh_token, "device_uuid": device_uuid}
    async with httpx.AsyncClient(transport=transport, timeout=15.0) as client:
        try:
            resp = await client.post(API_BASE_URL + AUTH_REFRESH_PATH, json=payload)
        except httpx.RequestError as exc:
            raise ZonneplanUnavailable(f"refresh endpoint unreachable: {exc}") from exc

    if resp.status_code in (400, 401, 403):
        raise ZonneplanAuthError(
            f"refresh failed ({resp.status_code}): {resp.text[:200]}"
        )
    if resp.status_code >= 500:
        raise ZonneplanUnavailable(f"refresh endpoint {resp.status_code}")
    if resp.status_code != 200:
        raise ZonneplanMalformed(f"unexpected status {resp.status_code}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise ZonneplanMalformed(f"refresh response not JSON: {exc}") from exc

    access = body.get("access_token")
    expires_in = body.get("expires_in")
    new_refresh = body.get("refresh_token", refresh_token)
    if not isinstance(access, str) or not isinstance(expires_in, (int, float)):
        raise ZonneplanMalformed("refresh missing access_token / expires_in")
    if not isinstance(new_refresh, str):
        raise ZonneplanMalformed("refresh missing new refresh_token")
    return (
        _AccessToken(
            value=access,
            expires_at=time.time() + float(expires_in) - ACCESS_TOKEN_EARLY_REFRESH_S,
        ),
        new_refresh,
    )


# ---------------------------------------------------------------------------
# Real client
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _RealZonneplanClient:
    """Zonneplan cloud client — single ``get_status`` runs three GETs.

    On first use the client fetches ``/user/me`` to discover the
    ``connection_uuid`` (P1 + tariff) and, if present, the ``pv_uuid``.
    Both are cached for the process lifetime.
    """

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        device_uuid: str,
        *,
        api_base_url: str = API_BASE_URL,
        token_transport: httpx.AsyncBaseTransport | None = None,
        api_transport: httpx.AsyncBaseTransport | None = None,
        access_token_expires_at: float | None = None,
    ) -> None:
        if not access_token or not refresh_token or not device_uuid:
            raise ZonneplanAuthError(
                "Zonneplan client requires access + refresh token + device_uuid"
            )
        self._refresh_token = refresh_token
        self._device_uuid = device_uuid
        self._api_base_url = api_base_url.rstrip("/")
        self._token_transport = token_transport
        self._api_transport = api_transport

        # If the caller knows when the initial access token expires,
        # respect it. Otherwise assume it's live for its full TTL.
        self._access = _AccessToken(
            value=access_token,
            expires_at=access_token_expires_at
            if access_token_expires_at is not None
            else time.time() + 3600 - ACCESS_TOKEN_EARLY_REFRESH_S,
        )
        self._http: httpx.AsyncClient | None = None

        self._connection_uuid: str | None = None
        self._pv_uuid: str | None = None

    # --- lifecycle ------------------------------------------------------

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                transport=self._api_transport,
                timeout=15.0,
                base_url=self._api_base_url,
            )
        return self._http

    async def _ensure_token(self) -> str:
        if time.time() >= self._access.expires_at:
            self._access, self._refresh_token = await _refresh_access_token(
                self._refresh_token,
                self._device_uuid,
                transport=self._token_transport,
            )
        return self._access.value

    async def _api_get(self, path: str) -> dict[str, Any]:
        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        try:
            resp = await self._client().get(path, headers=headers)
        except httpx.RequestError as exc:
            raise ZonneplanUnavailable(f"GET {path} unreachable: {exc}") from exc

        if resp.status_code >= 500:
            raise ZonneplanUnavailable(f"{path} {resp.status_code}")
        if resp.status_code in (401, 403):
            raise ZonneplanAuthError(f"{path} {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ZonneplanMalformed(f"{path} {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ZonneplanMalformed(f"{path}: non-JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ZonneplanMalformed(f"{path}: unexpected JSON shape")
        return data

    # --- discovery ------------------------------------------------------

    async def _discover(self) -> None:
        data = await self._api_get(USER_ME_PATH)
        # The Zonneplan API nests the data under a ``data`` key.
        payload = data.get("data", data)
        if not isinstance(payload, dict):
            raise ZonneplanMalformed("/user/me payload not an object")
        connections = payload.get("connections") or []
        if not isinstance(connections, list) or not connections:
            raise ZonneplanMalformed("no connections on account")
        first = connections[0]
        if not isinstance(first, dict):
            raise ZonneplanMalformed("connection record not an object")
        cid = first.get("uuid") or first.get("id")
        if cid is None:
            raise ZonneplanMalformed("connection record missing uuid")
        self._connection_uuid = str(cid)

        pv_installs = payload.get("pv_installations") or []
        if isinstance(pv_installs, list) and pv_installs:
            first_pv = pv_installs[0]
            if isinstance(first_pv, dict):
                pvid = first_pv.get("uuid") or first_pv.get("id")
                if pvid is not None:
                    self._pv_uuid = str(pvid)

    async def _ensure_ids(self) -> str:
        if self._connection_uuid is None:
            await self._discover()
        assert self._connection_uuid is not None
        return self._connection_uuid

    # --- public ---------------------------------------------------------

    async def get_status(self) -> ZonneplanStatus:
        conn = await self._ensure_ids()

        consumption = await self._api_get(
            LIVE_CONSUMPTION_PATH.format(uuid=conn)
        )
        tariff = await self._api_get(CURRENT_TARIFF_PATH.format(uuid=conn))

        pv_payload: dict[str, Any] | None = None
        if self._pv_uuid is not None:
            try:
                pv_payload = await self._api_get(PV_LIVE_PATH.format(uuid=self._pv_uuid))
            except ZonneplanError:
                pv_payload = None  # PV endpoint hiccups are non-fatal

        c = consumption.get("data", consumption) if isinstance(consumption, dict) else {}
        t = tariff.get("data", tariff) if isinstance(tariff, dict) else {}
        pv_data = (pv_payload or {}).get("data", pv_payload or {})

        active_power = _as_float(
            (c or {}).get("active_power_watt") if isinstance(c, dict) else None
        )
        if active_power is None and isinstance(c, dict):
            active_power = _as_float(c.get("active_power_w"))
        if active_power is None:
            raise ZonneplanMalformed("live consumption missing active_power")

        total_import = _as_float(
            (c or {}).get("total_import_kwh") if isinstance(c, dict) else None,
            default=0.0,
        )
        total_export = _as_float(
            (c or {}).get("total_export_kwh") if isinstance(c, dict) else None,
            default=0.0,
        )

        tariff_buy = _as_float(
            (t or {}).get("price_incl_tax_eur_per_kwh") if isinstance(t, dict) else None
        )
        if tariff_buy is None and isinstance(t, dict):
            tariff_buy = _as_float(t.get("total_price_including_tax"))
        if tariff_buy is None:
            raise ZonneplanMalformed("tariff response missing buy price")

        tariff_feedin = _as_float(
            (t or {}).get("feedin_price_incl_tax_eur_per_kwh") if isinstance(t, dict) else None
        )

        pv_power = _as_float(
            (pv_data or {}).get("power_watt") if isinstance(pv_data, dict) else None,
            default=0.0,
        )
        pv_yield_today = _as_float(
            (pv_data or {}).get("yield_kwh_today") if isinstance(pv_data, dict) else None,
            default=0.0,
        )

        return ZonneplanStatus(
            captured_at=datetime.now(),
            active_power_w=active_power,
            total_import_kwh=total_import if total_import is not None else 0.0,
            total_export_kwh=total_export if total_export is not None else 0.0,
            tariff_all_in_eur_kwh=tariff_buy,
            feedin_all_in_eur_kwh=tariff_feedin,
            pv_power_w=pv_power if pv_power is not None else 0.0,
            pv_yield_today_kwh=pv_yield_today if pv_yield_today is not None else 0.0,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def zonneplan_client() -> ZonneplanClient:
    """Return a real client when creds are set, mock otherwise."""
    access = os.environ.get("ZONNEPLAN_ACCESS_TOKEN", "").strip()
    refresh = os.environ.get("ZONNEPLAN_REFRESH_TOKEN", "").strip()
    uuid = os.environ.get("ZONNEPLAN_DEVICE_UUID", "").strip()
    if access and refresh and uuid:
        return _RealZonneplanClient(access, refresh, uuid)
    return MockZonneplanClient()


def is_using_mock_zonneplan() -> bool:
    return not (
        os.environ.get("ZONNEPLAN_ACCESS_TOKEN", "").strip()
        and os.environ.get("ZONNEPLAN_REFRESH_TOKEN", "").strip()
        and os.environ.get("ZONNEPLAN_DEVICE_UUID", "").strip()
    )
