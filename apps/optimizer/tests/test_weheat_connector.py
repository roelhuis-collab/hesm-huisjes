"""Tests for the WeHeat connector — read-only after PR6.

Focus areas:
1. Mock client returns coherent data and satisfies the Protocol.
2. Factory picks Real vs Mock based on ``WEHEAT_REFRESH_TOKEN`` env.
3. OAuth refresh exchanges a refresh token for an access token via
   the Keycloak endpoint, and surfaces auth/unavailable errors cleanly.
4. The Real client only refreshes when its access token has expired.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import httpx
import pytest
from src.connectors.weheat import (
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    OAUTH2_TOKEN_URL,
    MockWeHeatClient,
    WeHeatAuthError,
    WeHeatClient,
    WeHeatStatus,
    WeHeatUnavailable,
    _RealWeHeatClient,
    _refresh_access_token,
    is_using_mock_weheat,
    weheat_client,
)

# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------


async def test_mock_client_returns_coherent_status() -> None:
    client = MockWeHeatClient()
    status = await client.get_status()

    assert isinstance(status, WeHeatStatus)
    # Boiler in plausible residential range, never below legionella floor.
    assert 45.0 <= status.boiler_temp_c <= 65.0
    # Buffer always cooler than boiler in the synthetic shape.
    assert status.buffer_temp_c < status.boiler_temp_c
    # Flow > return when running.
    if status.is_running:
        assert status.flow_temp_c >= status.return_temp_c
        assert status.hp_power_w > 0
        assert status.cop is not None and status.cop > 1.0
    else:
        assert status.cop is None
        assert status.hp_power_w == 0.0

    await client.aclose()


async def test_mock_client_satisfies_protocol() -> None:
    # Structural typing: assigning to the Protocol var must type-check.
    client: WeHeatClient = MockWeHeatClient()
    status = await client.get_status()
    assert isinstance(status, WeHeatStatus)
    await client.aclose()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_mock_when_no_refresh_token() -> None:
    with patch.dict(os.environ, {"WEHEAT_REFRESH_TOKEN": ""}, clear=False):
        client = weheat_client()
        assert isinstance(client, MockWeHeatClient)
        assert is_using_mock_weheat() is True


def test_factory_returns_real_when_refresh_token_set() -> None:
    with patch.dict(os.environ, {"WEHEAT_REFRESH_TOKEN": "fake-rt"}, clear=False):
        client = weheat_client()
        assert isinstance(client, _RealWeHeatClient)
        assert is_using_mock_weheat() is False


def test_factory_uses_default_client_id_when_unset() -> None:
    env = {
        "WEHEAT_REFRESH_TOKEN": "rt",
        "WEHEAT_CLIENT_ID": "",
        "WEHEAT_CLIENT_SECRET": "",
    }
    with patch.dict(os.environ, env, clear=False):
        client = weheat_client()
        assert isinstance(client, _RealWeHeatClient)
        assert client._client_id == DEFAULT_CLIENT_ID
        assert client._client_secret == DEFAULT_CLIENT_SECRET


# ---------------------------------------------------------------------------
# OAuth refresh
# ---------------------------------------------------------------------------


def _token_response(access: str = "ACCESS-XYZ", expires_in: int = 300) -> bytes:
    return json.dumps(
        {
            "access_token": access,
            "expires_in": expires_in,
            "refresh_token": "new-rt",
            "token_type": "Bearer",
        }
    ).encode("utf-8")


async def test_refresh_access_token_happy_path() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.content.decode("utf-8")
        return httpx.Response(
            200, content=_token_response(), headers={"content-type": "application/json"}
        )

    transport = httpx.MockTransport(handler)
    token = await _refresh_access_token(
        "old-rt",
        client_id="HomeAssistantAPI",
        client_secret="secret",
        transport=transport,
    )

    assert token.value == "ACCESS-XYZ"
    # Early-refresh window subtracted, but still in the future.
    assert token.expires_at > time.time() + 100
    assert captured["url"] == OAUTH2_TOKEN_URL
    body = str(captured["body"])
    assert "grant_type=refresh_token" in body
    assert "refresh_token=old-rt" in body
    assert "client_id=HomeAssistantAPI" in body


async def test_refresh_access_token_invalid_grant_raises_auth_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, content=b'{"error":"invalid_grant","error_description":"revoked"}'
        )

    with pytest.raises(WeHeatAuthError):
        await _refresh_access_token(
            "bad-rt", "id", "sec", transport=httpx.MockTransport(handler)
        )


async def test_refresh_access_token_5xx_is_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"service unavailable")

    with pytest.raises(WeHeatUnavailable):
        await _refresh_access_token(
            "rt", "id", "sec", transport=httpx.MockTransport(handler)
        )


async def test_refresh_access_token_network_error_is_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed")

    with pytest.raises(WeHeatUnavailable):
        await _refresh_access_token(
            "rt", "id", "sec", transport=httpx.MockTransport(handler)
        )


# ---------------------------------------------------------------------------
# _RealWeHeatClient — token caching
# ---------------------------------------------------------------------------


async def test_real_client_caches_access_token_until_expiry() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=_token_response(expires_in=600))

    client = _RealWeHeatClient(
        "rt",
        client_id="id",
        client_secret="sec",
        token_transport=httpx.MockTransport(handler),
    )

    # Two consecutive refreshes should reuse the cached access token.
    t1 = await client._ensure_token()
    t2 = await client._ensure_token()
    assert t1 == t2 == "ACCESS-XYZ"
    assert calls["n"] == 1, "second call should hit the cache, not the network"


async def test_real_client_refreshes_after_expiry() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=_token_response(access=f"A-{calls['n']}"))

    client = _RealWeHeatClient(
        "rt",
        client_id="id",
        client_secret="sec",
        token_transport=httpx.MockTransport(handler),
    )
    await client._ensure_token()
    # Force expiry.
    assert client._access is not None
    client._access.expires_at = time.time() - 1
    await client._ensure_token()
    assert calls["n"] == 2
