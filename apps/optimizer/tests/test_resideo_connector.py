"""Tests for the Resideo (Honeywell Home) connector — PR7.

Covers:
1. Mock client returns coherent data + satisfies the Protocol.
2. Factory picks Real vs Mock based on env (all three vars must be set).
3. OAuth refresh exchanges refresh→access via the Honeywell token endpoint,
   sends HTTP Basic auth, and surfaces auth/unavailable errors cleanly.
4. The Real client caches access tokens between calls within their TTL.
5. Discovery picks the first location + first thermostat under it.
6. ``get_status`` parses the live thermostat detail body.
7. ``set_setpoint`` sends mode=Heat + heatSetpoint + TemporaryHold.
"""

from __future__ import annotations

import base64
import json
import os
import time
from unittest.mock import patch

import httpx
import pytest
from src.connectors.resideo import (
    API_BASE_URL,
    OAUTH2_TOKEN_URL,
    MockResideoClient,
    ResideoAuthError,
    ResideoClient,
    ResideoMalformed,
    ResideoStatus,
    ResideoUnavailable,
    _basic_auth_header,
    _RealResideoClient,
    _refresh_access_token,
    is_using_mock_resideo,
    resideo_client,
)

# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------


async def test_mock_client_returns_coherent_status() -> None:
    client = MockResideoClient()
    status = await client.get_status()

    assert isinstance(status, ResideoStatus)
    # Indoor temp lands in a plausible residential range.
    assert 18.0 <= status.indoor_temp_c <= 23.5
    # Setpoint is a sensible default.
    assert 19.0 <= status.setpoint_c <= 22.0
    # Humidity is reported (mocks always have it).
    assert status.humidity_pct is not None
    assert 30.0 <= status.humidity_pct <= 65.0
    await client.aclose()


async def test_mock_client_set_setpoint_updates_state() -> None:
    client = MockResideoClient()
    await client.set_setpoint(21.5)
    status = await client.get_status()
    assert status.setpoint_c == pytest.approx(21.5)


async def test_mock_client_satisfies_protocol() -> None:
    client: ResideoClient = MockResideoClient()
    status = await client.get_status()
    assert isinstance(status, ResideoStatus)
    await client.aclose()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_mock_when_any_var_missing() -> None:
    for missing in ("RESIDEO_CLIENT_ID", "RESIDEO_CLIENT_SECRET", "RESIDEO_REFRESH_TOKEN"):
        env = {
            "RESIDEO_CLIENT_ID": "cid",
            "RESIDEO_CLIENT_SECRET": "sec",
            "RESIDEO_REFRESH_TOKEN": "rt",
            missing: "",
        }
        with patch.dict(os.environ, env, clear=False):
            client = resideo_client()
            assert isinstance(client, MockResideoClient), f"{missing} blank ⇒ mock"
            assert is_using_mock_resideo() is True


def test_factory_returns_real_when_all_three_set() -> None:
    env = {
        "RESIDEO_CLIENT_ID": "cid",
        "RESIDEO_CLIENT_SECRET": "sec",
        "RESIDEO_REFRESH_TOKEN": "rt",
    }
    with patch.dict(os.environ, env, clear=False):
        client = resideo_client()
        assert isinstance(client, _RealResideoClient)
        assert is_using_mock_resideo() is False


# ---------------------------------------------------------------------------
# OAuth refresh
# ---------------------------------------------------------------------------


def _token_body(access: str = "AT-1", refresh: str = "RT-NEW", expires_in: int = 1800) -> bytes:
    return json.dumps(
        {
            "access_token": access,
            "expires_in": expires_in,
            "refresh_token": refresh,
            "token_type": "Bearer",
        }
    ).encode("utf-8")


def test_basic_auth_header_format() -> None:
    header = _basic_auth_header("user", "pass")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header[len("Basic ") :]).decode("ascii")
    assert decoded == "user:pass"


async def test_refresh_happy_path_uses_basic_auth_and_returns_new_refresh() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("authorization")
        captured["body"] = req.content.decode("utf-8")
        return httpx.Response(
            200,
            content=_token_body(access="AT-1", refresh="RT-NEW"),
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    token, new_refresh = await _refresh_access_token(
        "RT-OLD",
        client_id="cid",
        client_secret="sec",
        transport=transport,
    )

    assert token.value == "AT-1"
    assert new_refresh == "RT-NEW"
    assert captured["url"] == OAUTH2_TOKEN_URL
    assert captured["auth"] == _basic_auth_header("cid", "sec")
    body_s = str(captured["body"])
    assert "grant_type=refresh_token" in body_s
    assert "refresh_token=RT-OLD" in body_s
    # Early-refresh shaves a buffer off expiry but keeps it well in the future.
    assert token.expires_at > time.time() + 1000


async def test_refresh_invalid_grant_raises_auth_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, content=b'{"error":"invalid_grant","error_description":"revoked"}'
        )

    with pytest.raises(ResideoAuthError):
        await _refresh_access_token(
            "bad", "cid", "sec", transport=httpx.MockTransport(handler)
        )


async def test_refresh_5xx_is_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    with pytest.raises(ResideoUnavailable):
        await _refresh_access_token(
            "rt", "cid", "sec", transport=httpx.MockTransport(handler)
        )


async def test_refresh_network_error_is_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed")

    with pytest.raises(ResideoUnavailable):
        await _refresh_access_token(
            "rt", "cid", "sec", transport=httpx.MockTransport(handler)
        )


# ---------------------------------------------------------------------------
# _RealResideoClient — token caching + discovery + read + write
# ---------------------------------------------------------------------------


_LOCATION_BODY = json.dumps(
    [
        {
            "locationID": 12345,
            "name": "Sittard",
            "devices": [
                {
                    "deviceID": "LCC-AB12CD34",
                    "deviceClass": "Thermostat",
                    "deviceType": "Thermostat",
                    "name": "Woonkamer",
                }
            ],
        }
    ]
).encode("utf-8")

_THERMOSTAT_BODY = json.dumps(
    {
        "deviceID": "LCC-AB12CD34",
        "indoorTemperature": 20.7,
        "indoorHumidity": 47,
        "changeableValues": {
            "mode": "Heat",
            "heatSetpoint": 20.5,
            "coolSetpoint": 25.0,
        },
        "operationStatus": {"mode": "Heat"},
    }
).encode("utf-8")


def _api_handler(
    calls: list[tuple[str, str, dict[str, str], bytes]],
) -> httpx.MockTransport:
    """Routes /locations and /devices/thermostats/* to canned bodies; records writes."""

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        params = dict(req.url.params)
        body = req.content
        calls.append((req.method, path, params, body))
        if req.method == "GET" and path.endswith("/locations"):
            return httpx.Response(200, content=_LOCATION_BODY)
        if req.method == "GET" and path.endswith("/devices/thermostats/LCC-AB12CD34"):
            return httpx.Response(200, content=_THERMOSTAT_BODY)
        if req.method == "POST" and path.endswith("/devices/thermostats/LCC-AB12CD34"):
            return httpx.Response(200, content=b"{}")
        return httpx.Response(404, content=b"unknown route")

    return httpx.MockTransport(handler)


def _token_handler(calls: dict[str, int]) -> httpx.MockTransport:
    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] = calls.get("n", 0) + 1
        return httpx.Response(200, content=_token_body(access="AT-CACHED", expires_in=1800))

    return httpx.MockTransport(handler)


async def test_real_client_caches_access_token_within_ttl() -> None:
    token_calls: dict[str, int] = {}
    api_calls: list[tuple[str, str, dict[str, str], bytes]] = []

    client = _RealResideoClient(
        client_id="cid",
        client_secret="sec",
        refresh_token="rt",
        token_transport=_token_handler(token_calls),
        api_transport=_api_handler(api_calls),
    )

    t1 = await client._ensure_token()
    t2 = await client._ensure_token()
    assert t1 == t2 == "AT-CACHED"
    assert token_calls["n"] == 1, "second call should hit cache"


async def test_real_client_refreshes_after_expiry() -> None:
    token_calls: dict[str, int] = {}
    api_calls: list[tuple[str, str, dict[str, str], bytes]] = []

    client = _RealResideoClient(
        client_id="cid",
        client_secret="sec",
        refresh_token="rt",
        token_transport=_token_handler(token_calls),
        api_transport=_api_handler(api_calls),
    )
    await client._ensure_token()
    assert client._access is not None
    client._access.expires_at = time.time() - 1
    await client._ensure_token()
    assert token_calls["n"] == 2


async def test_real_client_get_status_parses_live_body() -> None:
    api_calls: list[tuple[str, str, dict[str, str], bytes]] = []
    token_calls: dict[str, int] = {}

    client = _RealResideoClient(
        client_id="cid",
        client_secret="sec",
        refresh_token="rt",
        api_base_url=API_BASE_URL,
        token_transport=_token_handler(token_calls),
        api_transport=_api_handler(api_calls),
    )
    status = await client.get_status()

    assert status.indoor_temp_c == pytest.approx(20.7)
    assert status.setpoint_c == pytest.approx(20.5)
    assert status.humidity_pct == pytest.approx(47.0)
    assert status.is_heating is True

    methods = [(m, p) for (m, p, _q, _b) in api_calls]
    # First call discovers locations, second reads the thermostat.
    assert methods[0] == ("GET", "/v2/locations")
    assert methods[1] == ("GET", "/v2/devices/thermostats/LCC-AB12CD34")
    # Both API calls carry apikey=cid in the query string.
    for _m, _p, params, _b in api_calls[:2]:
        assert params.get("apikey") == "cid"

    await client.aclose()


async def test_real_client_set_setpoint_sends_heat_and_temporary_hold() -> None:
    api_calls: list[tuple[str, str, dict[str, str], bytes]] = []
    token_calls: dict[str, int] = {}

    client = _RealResideoClient(
        client_id="cid",
        client_secret="sec",
        refresh_token="rt",
        token_transport=_token_handler(token_calls),
        api_transport=_api_handler(api_calls),
    )
    await client.set_setpoint(21.8)

    post = next(
        (m, p, q, b)
        for (m, p, q, b) in api_calls
        if m == "POST" and p.endswith("/devices/thermostats/LCC-AB12CD34")
    )
    _m, _p, q, b = post
    assert q.get("locationId") == "12345"
    assert q.get("apikey") == "cid"
    body = json.loads(b.decode("utf-8"))
    assert body["mode"] == "Heat"
    assert body["heatSetpoint"] == pytest.approx(21.8)
    assert body["thermostatSetpointStatus"] == "TemporaryHold"


async def test_real_client_get_status_5xx_is_unavailable() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/locations"):
            return httpx.Response(503, content=b"down")
        return httpx.Response(404)

    token_calls: dict[str, int] = {}
    client = _RealResideoClient(
        client_id="cid",
        client_secret="sec",
        refresh_token="rt",
        token_transport=_token_handler(token_calls),
        api_transport=httpx.MockTransport(api),
    )
    with pytest.raises(ResideoUnavailable):
        await client.get_status()


async def test_real_client_get_status_no_thermostat_raises_malformed() -> None:
    def api(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/locations"):
            empty = json.dumps([{"locationID": 1, "devices": []}]).encode("utf-8")
            return httpx.Response(200, content=empty)
        return httpx.Response(404)

    token_calls: dict[str, int] = {}
    client = _RealResideoClient(
        client_id="cid",
        client_secret="sec",
        refresh_token="rt",
        token_transport=_token_handler(token_calls),
        api_transport=httpx.MockTransport(api),
    )
    with pytest.raises(ResideoMalformed):
        await client.get_status()
