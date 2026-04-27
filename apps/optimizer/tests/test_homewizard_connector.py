"""
Tests for the HomeWizard P1 connector.

We exercise the client against an :class:`httpx.MockTransport` so the
network stack is real (URL routing, status-code handling, JSON parsing)
but no actual HTTP traffic happens.

Coverage:
  * device-info happy path
  * measurement happy path with full DSMR-5 payload
  * measurement happy path with sparse payload (older meter)
  * 401 / 403 → HomeWizardAuthError
  * 503 → HomeWizardUnavailable
  * timeout → HomeWizardUnavailable
  * non-JSON body → HomeWizardMalformed
  * malformed device-info shape → HomeWizardMalformed
  * extra headers from env are forwarded
  * from_env raises if base URL missing
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from src.connectors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)
from src.connectors.homewizard import (
    HomeWizardAuthError,
    HomeWizardMalformed,
    HomeWizardP1Client,
    HomeWizardUnavailable,
    _env_to_header,
)

BASE = "https://hwz.test"


def _client(handler: Callable[[httpx.Request], httpx.Response],
            **kwargs: object) -> HomeWizardP1Client:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5.0)
    return HomeWizardP1Client(base_url=BASE, http=http, **kwargs)  # type: ignore[arg-type]


# --- /api device info ------------------------------------------------------


async def test_get_device_info_happy_path() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url == f"{BASE}/api"
        return httpx.Response(200, json={
            "product_type": "HWE-P1",
            "product_name": "P1 Meter",
            "serial": "3c39e7aabbcc",
            "firmware_version": "5.18",
            "api_version": "v1",
        })

    async with _client(handler) as hw:
        info = await hw.get_device_info()
    assert info.product_type == "HWE-P1"
    assert info.api_version == "v1"


async def test_get_device_info_malformed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"product_type": "HWE-P1"})  # missing other fields

    async with _client(handler) as hw:
        with pytest.raises(HomeWizardMalformed):
            await hw.get_device_info()


# --- /api/v1/data measurement ----------------------------------------------


async def test_measurement_full_dsmr5() -> None:
    full = {
        "smr_version": 50,
        "meter_model": "ISKRA  2M550T-101",
        "wifi_ssid": "kerkstraat",
        "active_power_w": -1234.5,        # exporting
        "active_power_l1_w": -400.0,
        "active_power_l2_w": -400.0,
        "active_power_l3_w": -434.5,
        "total_power_import_kwh": 12345.123,
        "total_power_export_kwh": 5678.456,
        "total_gas_m3": 1234.567,
        "gas_timestamp": 240427120000,
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url == f"{BASE}/api/v1/data"
        return httpx.Response(200, json=full)

    async with _client(handler) as hw:
        m = await hw.get_measurement()

    assert m.active_power_w == pytest.approx(-1234.5)
    assert m.active_power_l2_w == pytest.approx(-400.0)
    assert m.total_import_kwh == pytest.approx(12345.123)
    assert m.total_export_kwh == pytest.approx(5678.456)
    assert m.total_gas_m3 == pytest.approx(1234.567)
    assert m.smr_version == 50


async def test_measurement_sparse_old_meter() -> None:
    sparse = {
        "smr_version": 42,
        "active_power_w": 750.0,
        "total_power_import_kwh": 4242.0,
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sparse)

    async with _client(handler) as hw:
        m = await hw.get_measurement()

    assert m.active_power_w == 750.0
    assert m.total_import_kwh == 4242.0
    assert m.active_power_l1_w is None
    assert m.total_export_kwh is None
    assert m.total_gas_m3 is None


async def test_measurement_non_dict_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    async with _client(handler) as hw:
        with pytest.raises(HomeWizardMalformed):
            await hw.get_measurement()


async def test_measurement_non_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>",
                              headers={"content-type": "text/html"})

    async with _client(handler) as hw:
        with pytest.raises(HomeWizardMalformed):
            await hw.get_measurement()


# --- error mapping ---------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_status(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "no"})

    async with _client(handler) as hw:
        with pytest.raises(HomeWizardAuthError):
            await hw.get_measurement()


async def test_5xx_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    async with _client(handler) as hw:
        with pytest.raises(HomeWizardUnavailable):
            await hw.get_measurement()


async def test_timeout_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    async with _client(handler) as hw:
        with pytest.raises(HomeWizardUnavailable):
            await hw.get_measurement()


async def test_network_error_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated dns failure")

    async with _client(handler) as hw:
        with pytest.raises(HomeWizardUnavailable):
            await hw.get_measurement()


# --- shared base hierarchy --------------------------------------------------


async def test_subclasses_can_be_caught_via_shared_base() -> None:
    """Orchestrator catches ConnectorUnavailable; we verify the inheritance."""
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client(handler) as hw:
        with pytest.raises(ConnectorUnavailable):
            await hw.get_measurement()
        with pytest.raises(ConnectorError):  # ultimate base
            try:
                await hw.get_measurement()
            except ConnectorUnavailable:
                raise


async def test_auth_error_via_shared_base() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with _client(handler) as hw:
        with pytest.raises(ConnectorAuthError):
            await hw.get_measurement()


async def test_malformed_via_shared_base() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    async with _client(handler) as hw:
        with pytest.raises(ConnectorMalformed):
            await hw.get_measurement()


# --- extra headers + env ---------------------------------------------------


async def test_extra_headers_are_forwarded() -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.headers))
        return httpx.Response(200, json={
            "product_type": "HWE-P1", "product_name": "P1 Meter",
            "serial": "x", "firmware_version": "5.18", "api_version": "v1",
        })

    async with _client(
        handler,
        extra_headers={"CF-Access-Client-Id": "abc", "X-Foo": "bar"},
    ) as hw:
        await hw.get_device_info()

    assert seen.get("cf-access-client-id") == "abc"
    assert seen.get("x-foo") == "bar"


def test_from_env_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMEWIZARD_BASE_URL", raising=False)
    with pytest.raises(HomeWizardAuthError):
        HomeWizardP1Client.from_env()


def test_from_env_picks_up_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEWIZARD_BASE_URL", "https://hwz.example.com")
    monkeypatch.setenv("HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_ID", "client-abc")
    monkeypatch.setenv("HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_SECRET", "secret-xyz")

    client = HomeWizardP1Client.from_env()
    assert client._base_url == "https://hwz.example.com"
    assert client._extra_headers["Cf-Access-Client-Id"] == "client-abc"
    assert client._extra_headers["Cf-Access-Client-Secret"] == "secret-xyz"


def test_env_to_header_conversion() -> None:
    assert _env_to_header("HOMEWIZARD_HEADER_X_FOO") == "X-Foo"
    assert _env_to_header("HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_ID") == "Cf-Access-Client-Id"


# --- using outside async-context fails fast --------------------------------


async def test_using_without_context_raises() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    # http=None forces lazy creation, then we don't enter the context
    client = HomeWizardP1Client(base_url=BASE, http=None)
    # also avoid the transport-injection path:
    del transport
    with pytest.raises(RuntimeError):
        await client.get_measurement()


# --- end-to-end JSON parity check -----------------------------------------


async def test_real_world_json_round_trip() -> None:
    """Sanity: a verbatim payload from the HomeWizard docs round-trips."""
    payload = json.loads("""{
      "smr_version": 50,
      "meter_model": "ISKRA  2M550T-101",
      "unique_id": "00112233445566778899AABB",
      "active_power_w": 543.21,
      "active_power_l1_w": 200.0,
      "active_power_l2_w": 150.0,
      "active_power_l3_w": 193.21,
      "total_power_import_kwh": 9876.543,
      "total_power_import_t1_kwh": 4321.0,
      "total_power_import_t2_kwh": 5555.543,
      "total_power_export_kwh": 0.0,
      "total_gas_m3": 1111.222,
      "active_voltage_l1_v": 232.0
    }""")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _client(handler) as hw:
        m = await hw.get_measurement()
    assert m.active_power_w == pytest.approx(543.21)
    assert m.total_export_kwh == 0.0
    assert m.total_gas_m3 == pytest.approx(1111.222)
