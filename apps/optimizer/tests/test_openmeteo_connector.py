"""
Tests for the Open-Meteo weather connector.

We exercise the client against an :class:`httpx.MockTransport` so the
network stack is real (URL routing, status-code handling, JSON parsing)
but no actual HTTP traffic happens.

Coverage:
  * 48-hour happy path → 48 HourlyForecast objects with correct fields
  * timezone conversion (Europe/Amsterdam → UTC), winter and summer DST
  * crude PV estimate matches expected magnitudes at noon / midnight /
    overcast
  * 503 → OpenMeteoUnavailable
  * timeout → OpenMeteoUnavailable
  * non-JSON body → OpenMeteoMalformed
  * missing 'hourly' block → OpenMeteoMalformed
  * shared-base inheritance (orchestrator catches ConnectorUnavailable)
  * from_env defaults to Sittard when env vars are unset
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
import pytest
from src.connectors import (
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)
from src.connectors.openmeteo import (
    PV_PEAK_W,
    HourlyForecast,
    OpenMeteoClient,
    OpenMeteoMalformed,
    OpenMeteoUnavailable,
    pv_estimate_w,
)

BASE = "https://api.test.open-meteo.test/v1/forecast"


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: object,
) -> OpenMeteoClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5.0)
    return OpenMeteoClient(
        latitude=50.99,
        longitude=5.87,
        base_url=BASE,
        http=http,
        **kwargs,  # type: ignore[arg-type]
    )


def _payload(hours: int, *, start_local: str = "2026-04-27T00:00") -> dict[str, object]:
    """Build a deterministic 'hourly' payload with `hours` rows."""
    start = datetime.fromisoformat(start_local)
    times: list[str] = []
    temps: list[float] = []
    clouds: list[float] = []
    for h in range(hours):
        t = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=h)
        times.append(t.strftime("%Y-%m-%dT%H:%M"))
        temps.append(round(8.0 + (h % 12) * 0.5, 2))
        clouds.append(float((h * 7) % 100))
    return {
        "latitude": 50.99,
        "longitude": 5.87,
        "timezone": "Europe/Amsterdam",
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "cloud_cover": clouds,
        },
    }


# --- happy path ------------------------------------------------------------


async def test_get_forecast_returns_48_rows() -> None:
    payload = _payload(48)

    def handler(req: httpx.Request) -> httpx.Response:
        # sanity-check the query string we built
        assert req.url.params["latitude"] == "50.99"
        assert req.url.params["longitude"] == "5.87"
        assert req.url.params["hourly"] == "temperature_2m,cloud_cover"
        assert req.url.params["timezone"] == "Europe/Amsterdam"
        return httpx.Response(200, json=payload)

    async with _client(handler) as om:
        rows = await om.get_forecast(hours=48)

    assert len(rows) == 48
    assert all(isinstance(r, HourlyForecast) for r in rows)
    assert rows[0].temperature_c == pytest.approx(8.0)
    assert rows[0].cloud_cover_pct == 0.0


async def test_get_forecast_clips_to_requested_hours() -> None:
    payload = _payload(48)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _client(handler) as om:
        rows = await om.get_forecast(hours=12)

    assert len(rows) == 12


# --- timezone conversion ---------------------------------------------------


async def test_winter_local_time_converts_to_utc_plus_one() -> None:
    """In January NL is UTC+1 (CET, no DST). 12:00 local → 11:00 UTC."""
    payload = {
        "hourly": {
            "time": ["2026-01-15T12:00"],
            "temperature_2m": [3.5],
            "cloud_cover": [50.0],
        },
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _client(handler) as om:
        rows = await om.get_forecast(hours=1)

    assert rows[0].timestamp_utc == datetime(2026, 1, 15, 11, 0)


async def test_summer_local_time_converts_to_utc_plus_two() -> None:
    """In July NL is UTC+2 (CEST). 12:00 local → 10:00 UTC."""
    payload = {
        "hourly": {
            "time": ["2026-07-15T12:00"],
            "temperature_2m": [22.0],
            "cloud_cover": [10.0],
        },
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _client(handler) as om:
        rows = await om.get_forecast(hours=1)

    assert rows[0].timestamp_utc == datetime(2026, 7, 15, 10, 0)


# --- crude PV estimate -----------------------------------------------------


def test_pv_estimate_at_solar_noon_clear_sky() -> None:
    """13:00 local, 0% cloud → near peak watts (within 5%)."""
    noon = datetime(2026, 6, 21, 13, 0)
    pv = pv_estimate_w(noon, cloud_cover_pct=0.0)
    assert pv == pytest.approx(PV_PEAK_W, rel=0.05)


def test_pv_estimate_at_midnight_is_zero() -> None:
    midnight = datetime(2026, 6, 21, 0, 0)
    assert pv_estimate_w(midnight, cloud_cover_pct=0.0) == 0.0


def test_pv_estimate_before_dawn_is_zero() -> None:
    pre_dawn = datetime(2026, 6, 21, 5, 30)
    assert pv_estimate_w(pre_dawn, cloud_cover_pct=0.0) == 0.0


def test_pv_estimate_after_dusk_is_zero() -> None:
    after_dusk = datetime(2026, 6, 21, 21, 0)
    assert pv_estimate_w(after_dusk, cloud_cover_pct=0.0) == 0.0


def test_pv_estimate_overcast_drops_to_30_percent() -> None:
    """13:00 local, 100% cloud → ~30% of clear-sky peak."""
    noon = datetime(2026, 6, 21, 13, 0)
    pv = pv_estimate_w(noon, cloud_cover_pct=100.0)
    expected = PV_PEAK_W * 0.30  # 1 - 0.7 * 1.0
    assert pv == pytest.approx(expected, rel=0.05)


def test_pv_estimate_partial_cloud_scales_linearly() -> None:
    """50% cloud cover → cloud_factor = 1 - 0.7 * 0.5 = 0.65 of clear-sky."""
    noon = datetime(2026, 6, 21, 13, 0)
    clear = pv_estimate_w(noon, cloud_cover_pct=0.0)
    half = pv_estimate_w(noon, cloud_cover_pct=50.0)
    assert half == pytest.approx(clear * 0.65, rel=1e-6)


# --- error mapping ---------------------------------------------------------


async def test_5xx_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    async with _client(handler) as om:
        with pytest.raises(OpenMeteoUnavailable):
            await om.get_forecast()


async def test_timeout_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    async with _client(handler) as om:
        with pytest.raises(OpenMeteoUnavailable):
            await om.get_forecast()


async def test_network_error_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated dns failure")

    async with _client(handler) as om:
        with pytest.raises(OpenMeteoUnavailable):
            await om.get_forecast()


async def test_non_json_maps_to_malformed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>oops</html>",
            headers={"content-type": "text/html"},
        )

    async with _client(handler) as om:
        with pytest.raises(OpenMeteoMalformed):
            await om.get_forecast()


async def test_missing_hourly_key_maps_to_malformed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"latitude": 50.99, "longitude": 5.87})

    async with _client(handler) as om:
        with pytest.raises(OpenMeteoMalformed):
            await om.get_forecast()


async def test_mismatched_array_lengths_maps_to_malformed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": ["2026-04-27T00:00", "2026-04-27T01:00"],
                    "temperature_2m": [8.0],  # short
                    "cloud_cover": [10.0, 20.0],
                },
            },
        )

    async with _client(handler) as om:
        with pytest.raises(OpenMeteoMalformed):
            await om.get_forecast()


# --- shared base hierarchy --------------------------------------------------


async def test_subclasses_can_be_caught_via_shared_base() -> None:
    """Orchestrator catches ConnectorUnavailable; we verify the inheritance."""
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client(handler) as om:
        with pytest.raises(ConnectorUnavailable):
            await om.get_forecast()
        with pytest.raises(ConnectorError):  # ultimate base
            try:
                await om.get_forecast()
            except ConnectorUnavailable:
                raise


async def test_malformed_via_shared_base() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    async with _client(handler) as om:
        with pytest.raises(ConnectorMalformed):
            await om.get_forecast()


# --- env construction ------------------------------------------------------


def test_from_env_defaults_to_sittard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENMETEO_LATITUDE", raising=False)
    monkeypatch.delenv("OPENMETEO_LONGITUDE", raising=False)
    monkeypatch.delenv("OPENMETEO_BASE_URL", raising=False)

    client = OpenMeteoClient.from_env()
    assert client._latitude == pytest.approx(50.99)
    assert client._longitude == pytest.approx(5.87)


def test_from_env_honours_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENMETEO_LATITUDE", "52.37")
    monkeypatch.setenv("OPENMETEO_LONGITUDE", "4.89")
    monkeypatch.setenv("OPENMETEO_BASE_URL", "https://example.test/forecast")

    client = OpenMeteoClient.from_env()
    assert client._latitude == pytest.approx(52.37)
    assert client._longitude == pytest.approx(4.89)
    assert client._base_url == "https://example.test/forecast"


# --- using outside async-context fails fast --------------------------------


async def test_using_without_context_raises() -> None:
    client = OpenMeteoClient(latitude=50.99, longitude=5.87, http=None)
    with pytest.raises(RuntimeError):
        await client.get_forecast()
