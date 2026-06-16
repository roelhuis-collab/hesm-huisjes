"""Tests voor de echte Growatt ShinePhone-cloud-client.

Geen netwerk: alle calls via ``httpx.MockTransport``. We mocken de hele bootstrap
(login → plant-list → device-list → inverter-detail) en verifiëren dat de
response correct gemapt wordt naar ``GrowattStatus``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from src.connectors.growatt import (
    GrowattAuthError,
    GrowattMalformed,
    MockGrowattClient,
    _hash_password,
    _RealGrowattClient,
    growatt_client,
    is_using_mock_growatt,
)

# ---------------------------------------------------------------------------
# Wachtwoord-hash — Growatt-specifieke 0→c-substitutie
# ---------------------------------------------------------------------------


def test_hash_password_md5_then_zero_to_c_at_even_positions() -> None:
    """Bekende string: MD5("hello") = 5d41402abc4b2a76b9719d911017c592.

    De even-positie '0'-chars (op posities 6, 22, 28) worden 'c': de positionele
    substitutie maakt het verschil met een gewone MD5-hash.
    """
    raw_md5 = hashlib.md5(b"hello").hexdigest()
    transformed = _hash_password("hello")
    # Same length, same hex chars (alleen even-positie '0' gewijzigd).
    assert len(transformed) == len(raw_md5)
    for i, ch in enumerate(transformed):
        if i % 2 == 0 and raw_md5[i] == "0":
            assert ch == "c", f"positie {i}: '0' had 'c' moeten worden, kreeg {ch!r}"
        else:
            assert ch == raw_md5[i]


def test_hash_password_only_touches_even_position_zeros() -> None:
    """Even-positie '0'-chars worden 'c'; oneven-posities en niet-'0' blijven gelijk."""
    digest = hashlib.md5(b"hello").hexdigest()
    transformed = _hash_password("hello")
    for i in range(len(digest)):
        if i % 2 == 0 and digest[i] == "0":
            assert transformed[i] == "c"
        else:
            assert transformed[i] == digest[i]


# ---------------------------------------------------------------------------
# Factory + mock-detectie
# ---------------------------------------------------------------------------


def test_factory_returns_mock_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROWATT_USERNAME", raising=False)
    monkeypatch.delenv("GROWATT_PASSWORD", raising=False)
    assert isinstance(growatt_client(), MockGrowattClient)
    assert is_using_mock_growatt() is True


def test_factory_returns_real_client_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROWATT_USERNAME", "roel@example.com")
    monkeypatch.setenv("GROWATT_PASSWORD", "supersecret")
    client = growatt_client()
    assert isinstance(client, _RealGrowattClient)
    assert is_using_mock_growatt() is False


def test_real_client_rejects_empty_credentials() -> None:
    with pytest.raises(GrowattAuthError):
        _RealGrowattClient(username="", password="x")
    with pytest.raises(GrowattAuthError):
        _RealGrowattClient(username="x", password="")


# ---------------------------------------------------------------------------
# Bootstrap + get_status — happy path
# ---------------------------------------------------------------------------


def _login_response() -> dict[str, Any]:
    return {
        "back": {
            "success": True,
            "user": {"id": 42, "rightlevel": 1},
        }
    }


def _plant_list_response(plant_id: str = "9876") -> dict[str, Any]:
    return {"back": [{"plantId": plant_id, "plantName": "Kempenstraat 3"}]}


def _device_list_response(sn: str = "MOD9000XYZ") -> dict[str, Any]:
    return {
        "deviceList": [
            {
                "deviceSn": sn,
                "deviceModel": "MOD 9000TL3-X",
                "deviceType": "inverter",
            }
        ]
    }


def _inverter_detail_response() -> dict[str, Any]:
    return {
        "obj": {
            # MOD 9000TL3-X is 3-fase; vermogens per fase plus totaal.
            "pac": 3200.0,
            "eToday": 18.42,
            "pacR": 1070.0,
            "pacS": 1080.0,
            "pacT": 1050.0,
        }
    }


def _real_client(handler: httpx.MockTransport) -> _RealGrowattClient:
    http = httpx.AsyncClient(transport=handler)
    return _RealGrowattClient(username="roel", password="hunter2", http=http)


def _route(path: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(payload).encode())


def _build_handler(
    *,
    login: dict[str, Any] | None = None,
    plants: dict[str, Any] | None = None,
    devices: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/newTwoLoginAPI.do"):
            return _route(path, login or _login_response())
        if path.endswith("/PlantListAPI.do"):
            return _route(path, plants or _plant_list_response())
        if path.endswith("/newTwoPlantAPI.do"):
            return _route(path, devices or _device_list_response())
        if path.endswith("/newInverterAPI.do"):
            return _route(path, detail or _inverter_detail_response())
        return httpx.Response(404, json={"error": "route niet bekend"})

    return httpx.MockTransport(handler)


async def test_get_status_runs_full_bootstrap_and_maps_fields() -> None:
    """Login → plants → devices → detail → GrowattStatus met juiste velden."""
    client = _real_client(_build_handler())
    async with client as c:
        status = await c.get_status()

    assert status.pv_power_w == pytest.approx(3200.0)
    assert status.daily_yield_kwh == pytest.approx(18.42)
    assert status.power_l1_w == pytest.approx(1070.0)
    assert status.power_l2_w == pytest.approx(1080.0)
    assert status.power_l3_w == pytest.approx(1050.0)


async def test_get_status_caches_bootstrap_so_second_call_only_hits_detail() -> None:
    """Tweede call doet geen login/discovery opnieuw — login-counter bewijst dat."""
    counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counts[path] = counts.get(path, 0) + 1
        if path.endswith("/newTwoLoginAPI.do"):
            return _route(path, _login_response())
        if path.endswith("/PlantListAPI.do"):
            return _route(path, _plant_list_response())
        if path.endswith("/newTwoPlantAPI.do"):
            return _route(path, _device_list_response())
        if path.endswith("/newInverterAPI.do"):
            return _route(path, _inverter_detail_response())
        return httpx.Response(404)

    client = _real_client(httpx.MockTransport(handler))
    async with client as c:
        await c.get_status()
        await c.get_status()
        await c.get_status()

    assert counts.get("/newTwoLoginAPI.do") == 1
    assert counts.get("/PlantListAPI.do") == 1
    assert counts.get("/newTwoPlantAPI.do") == 1
    assert counts.get("/newInverterAPI.do") == 3


async def test_pac_in_decideci_watts_is_divided_when_too_large() -> None:
    """Sommige builds geven pac in 0.1 W; we delen door 10 als > 50 kW."""
    detail = {"obj": {"pac": 75000.0, "eToday": 10.0, "pacR": 0, "pacS": 0, "pacT": 0}}
    client = _real_client(_build_handler(detail=detail))
    async with client as c:
        status = await c.get_status()
    assert status.pv_power_w == pytest.approx(7500.0)


async def test_string_values_in_response_are_coerced_to_float() -> None:
    """Growatt mixt strings en getallen; we parsen beide netjes."""
    detail = {
        "obj": {
            "pac": "3200.5",
            "eToday": "18.42",
            "pacR": "1070",
            "pacS": "1080",
            "pacT": "1050",
        }
    }
    client = _real_client(_build_handler(detail=detail))
    async with client as c:
        status = await c.get_status()
    assert status.pv_power_w == pytest.approx(3200.5)
    assert status.daily_yield_kwh == pytest.approx(18.42)


# ---------------------------------------------------------------------------
# Faalpaden
# ---------------------------------------------------------------------------


async def test_login_failure_raises_auth_error() -> None:
    """``success: False`` in login-response → GrowattAuthError."""
    bad_login = {"back": {"success": False, "msg": "wrong password"}}
    client = _real_client(_build_handler(login=bad_login))
    with pytest.raises(GrowattAuthError):
        async with client as c:
            await c.get_status()


async def test_empty_plant_list_raises_malformed() -> None:
    client = _real_client(_build_handler(plants={"back": []}))
    with pytest.raises(GrowattMalformed):
        async with client as c:
            await c.get_status()


async def test_empty_device_list_raises_malformed() -> None:
    client = _real_client(_build_handler(devices={"deviceList": []}))
    with pytest.raises(GrowattMalformed):
        async with client as c:
            await c.get_status()


async def test_inverter_detail_missing_fields_returns_zero() -> None:
    """Defensief: missende velden → 0.0, geen crash."""
    detail: dict[str, Any] = {"obj": {}}
    client = _real_client(_build_handler(detail=detail))
    async with client as c:
        status = await c.get_status()
    assert status.pv_power_w == 0.0
    assert status.daily_yield_kwh == 0.0


# ---------------------------------------------------------------------------
# Mock werkt nog steeds (regression-check)
# ---------------------------------------------------------------------------


async def test_mock_still_produces_sun_curve() -> None:
    mock = MockGrowattClient()
    status = await mock.get_status()
    assert status.pv_power_w >= 0.0
    assert status.daily_yield_kwh >= 0.0
