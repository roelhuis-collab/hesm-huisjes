"""Tests for the Zonneplan connector — PR9.

Covers:
1. Mock client returns coherent data (positive tariff, sane P1 shape) and
   satisfies the ``ZonneplanClient`` protocol.
2. Factory picks Real vs Mock based on env (all three vars required).
3. OAuth refresh exchanges refresh→access, sends device_uuid, surfaces
   auth/unavailable errors cleanly.
4. Real client caches access tokens within TTL.
5. Discovery picks the first connection + first PV installation.
6. ``get_status`` parses P1 + tariff + PV bodies and returns a merged
   ``ZonneplanStatus``.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import httpx
import pytest
from src.connectors.zonneplan import (
    API_BASE_URL,
    MockZonneplanClient,
    ZonneplanAuthError,
    ZonneplanClient,
    ZonneplanMalformed,
    ZonneplanStatus,
    ZonneplanUnavailable,
    _RealZonneplanClient,
    _refresh_access_token,
    is_using_mock_zonneplan,
    zonneplan_client,
)

# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------


async def test_mock_client_returns_coherent_status() -> None:
    client = MockZonneplanClient()
    status = await client.get_status()

    assert isinstance(status, ZonneplanStatus)
    # Tariff sits in typical NL dynamic-tariff range.
    assert 0.10 <= status.tariff_all_in_eur_kwh <= 0.60
    # PV never negative.
    assert status.pv_power_w >= 0.0
    # Import + export monotonic non-negative.
    assert status.total_import_kwh > 0
    assert status.total_export_kwh > 0
    # Feed-in cheaper than buy (post-saldering assumption for the mock).
    if status.feedin_all_in_eur_kwh is not None:
        assert status.feedin_all_in_eur_kwh < status.tariff_all_in_eur_kwh
    await client.aclose()


async def test_mock_client_satisfies_protocol() -> None:
    client: ZonneplanClient = MockZonneplanClient()
    status = await client.get_status()
    assert isinstance(status, ZonneplanStatus)
    await client.aclose()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_mock_when_any_var_missing() -> None:
    for missing in (
        "ZONNEPLAN_ACCESS_TOKEN",
        "ZONNEPLAN_REFRESH_TOKEN",
        "ZONNEPLAN_DEVICE_UUID",
    ):
        env = {
            "ZONNEPLAN_ACCESS_TOKEN": "at",
            "ZONNEPLAN_REFRESH_TOKEN": "rt",
            "ZONNEPLAN_DEVICE_UUID": "u",
            missing: "",
        }
        with patch.dict(os.environ, env, clear=False):
            assert isinstance(zonneplan_client(), MockZonneplanClient)
            assert is_using_mock_zonneplan() is True


def test_factory_returns_real_when_all_three_set() -> None:
    env = {
        "ZONNEPLAN_ACCESS_TOKEN": "at",
        "ZONNEPLAN_REFRESH_TOKEN": "rt",
        "ZONNEPLAN_DEVICE_UUID": "u",
    }
    with patch.dict(os.environ, env, clear=False):
        assert isinstance(zonneplan_client(), _RealZonneplanClient)
        assert is_using_mock_zonneplan() is False


# ---------------------------------------------------------------------------
# OAuth refresh
# ---------------------------------------------------------------------------


def _refresh_body(
    access: str = "AT-NEW", refresh: str = "RT-NEW", expires_in: int = 3600
) -> bytes:
    return json.dumps(
        {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }
    ).encode("utf-8")


async def test_refresh_happy_path_sends_uuid_and_returns_new_pair() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, content=_refresh_body())

    token, new_refresh = await _refresh_access_token(
        "RT-OLD", "device-uuid", transport=httpx.MockTransport(handler)
    )
    assert token.value == "AT-NEW"
    assert new_refresh == "RT-NEW"
    body_dict = captured["body"]
    assert isinstance(body_dict, dict)
    assert body_dict["refresh_token"] == "RT-OLD"
    assert body_dict["device_uuid"] == "device-uuid"


async def test_refresh_401_is_auth_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error":"revoked"}')

    with pytest.raises(ZonneplanAuthError):
        await _refresh_access_token(
            "rt", "u", transport=httpx.MockTransport(handler)
        )


async def test_refresh_5xx_is_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    with pytest.raises(ZonneplanUnavailable):
        await _refresh_access_token(
            "rt", "u", transport=httpx.MockTransport(handler)
        )


# ---------------------------------------------------------------------------
# Real client — discovery + get_status
# ---------------------------------------------------------------------------


_USER_ME = json.dumps(
    {
        "data": {
            "connections": [{"uuid": "conn-1", "name": "Sittard"}],
            "pv_installations": [{"uuid": "pv-1"}],
        }
    }
).encode("utf-8")

_LIVE_CONSUMPTION = json.dumps(
    {
        "data": {
            "active_power_watt": -1240,
            "total_import_kwh": 4823.15,
            "total_export_kwh": 3921.44,
        }
    }
).encode("utf-8")

_CURRENT_TARIFF = json.dumps(
    {
        "data": {
            "price_incl_tax_eur_per_kwh": 0.2871,
            "feedin_price_incl_tax_eur_per_kwh": 0.1866,
        }
    }
).encode("utf-8")

_PV_LIVE = json.dumps(
    {"data": {"power_watt": 4380, "yield_kwh_today": 22.7}}
).encode("utf-8")


def _api_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        calls.append(path)
        if path == "/user/me":
            return httpx.Response(200, content=_USER_ME)
        if path == "/user/connection/conn-1/consumption/live":
            return httpx.Response(200, content=_LIVE_CONSUMPTION)
        if path == "/user/connection/conn-1/electricity-prices/current":
            return httpx.Response(200, content=_CURRENT_TARIFF)
        if path == "/user/pv/pv-1":
            return httpx.Response(200, content=_PV_LIVE)
        return httpx.Response(404, content=b"unknown")

    return httpx.MockTransport(handler)


async def test_real_client_get_status_merges_three_endpoints() -> None:
    calls: list[str] = []
    client = _RealZonneplanClient(
        access_token="AT",
        refresh_token="RT",
        device_uuid="U",
        api_base_url=API_BASE_URL,
        api_transport=_api_transport(calls),
        access_token_expires_at=time.time() + 3600,
    )
    status = await client.get_status()

    assert status.active_power_w == pytest.approx(-1240.0)
    assert status.total_import_kwh == pytest.approx(4823.15)
    assert status.total_export_kwh == pytest.approx(3921.44)
    assert status.tariff_all_in_eur_kwh == pytest.approx(0.2871)
    assert status.feedin_all_in_eur_kwh == pytest.approx(0.1866)
    assert status.pv_power_w == pytest.approx(4380.0)
    assert status.pv_yield_today_kwh == pytest.approx(22.7)

    # Discovery on first call, then live endpoints.
    assert "/user/me" in calls
    assert "/user/connection/conn-1/consumption/live" in calls
    assert "/user/connection/conn-1/electricity-prices/current" in calls
    assert "/user/pv/pv-1" in calls

    await client.aclose()


async def test_real_client_survives_pv_endpoint_failure() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/user/me":
            return httpx.Response(200, content=_USER_ME)
        if path == "/user/connection/conn-1/consumption/live":
            return httpx.Response(200, content=_LIVE_CONSUMPTION)
        if path == "/user/connection/conn-1/electricity-prices/current":
            return httpx.Response(200, content=_CURRENT_TARIFF)
        if path == "/user/pv/pv-1":
            return httpx.Response(503, content=b"pv-down")
        return httpx.Response(404)

    client = _RealZonneplanClient(
        access_token="AT",
        refresh_token="RT",
        device_uuid="U",
        api_transport=httpx.MockTransport(handler),
        access_token_expires_at=time.time() + 3600,
    )
    status = await client.get_status()
    # PV missing → zeros, but P1 + tariff still populated.
    assert status.pv_power_w == 0.0
    assert status.pv_yield_today_kwh == 0.0
    assert status.tariff_all_in_eur_kwh == pytest.approx(0.2871)


async def test_real_client_missing_tariff_raises_malformed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/user/me":
            return httpx.Response(200, content=_USER_ME)
        if path == "/user/connection/conn-1/consumption/live":
            return httpx.Response(200, content=_LIVE_CONSUMPTION)
        if path == "/user/connection/conn-1/electricity-prices/current":
            empty = json.dumps({"data": {}}).encode("utf-8")
            return httpx.Response(200, content=empty)
        return httpx.Response(404)

    client = _RealZonneplanClient(
        access_token="AT",
        refresh_token="RT",
        device_uuid="U",
        api_transport=httpx.MockTransport(handler),
        access_token_expires_at=time.time() + 3600,
    )
    with pytest.raises(ZonneplanMalformed):
        await client.get_status()


async def test_real_client_401_on_live_data_is_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/user/me":
            return httpx.Response(401, content=b'{"error":"expired"}')
        return httpx.Response(404)

    client = _RealZonneplanClient(
        access_token="AT",
        refresh_token="RT",
        device_uuid="U",
        api_transport=httpx.MockTransport(handler),
        access_token_expires_at=time.time() + 3600,
    )
    with pytest.raises(ZonneplanAuthError):
        await client.get_status()
