"""
Open-Meteo weather connector — hourly forecast for Sittard.

Open-Meteo is a free, key-less public API. We use it as the weather feed
into the optimizer: ambient temperature drives the heat-pump load model,
and cloud cover drives a *crude* PV-production estimate.

Crude PV caveat
---------------
The PV figure here is a placeholder: a sine bump centred on 13:00 local
over a fixed 06:00–20:00 daylight window, scaled by ``PV_PEAK_W`` (26
panels, ~11.000 kWh/yr → ~7-8 kW peak), with linear cloud attenuation
(0% clouds keeps full output, 100% drops to 30% of clear-sky). It ignores
azimuth, tilt, season and solar elevation tables. **Solcast replaces this
post-launch.**

Endpoint: ``GET https://api.open-meteo.com/v1/forecast`` with
``hourly=temperature_2m,cloud_cover`` and ``timezone=Europe/Amsterdam``.
We convert each timestamp to UTC at parse time so downstream consumers
get naive UTC ``datetime`` objects (matching the rest of the codebase).

Docs: https://open-meteo.com/en/docs
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from src.connectors.base import (
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)

# ---------------------------------------------------------------------------
# Subclassed exceptions — keep the Open-Meteo-specific name in stack traces
# while the orchestrator catches the shared base. There is no AuthError
# variant because Open-Meteo requires no credentials.
# ---------------------------------------------------------------------------


class OpenMeteoError(ConnectorError):
    """Base for Open-Meteo-specific failures."""


class OpenMeteoUnavailable(OpenMeteoError, ConnectorUnavailable):
    """API down, timeout, network blip, 5xx."""


class OpenMeteoMalformed(OpenMeteoError, ConnectorMalformed):
    """Endpoint returned 200 but body didn't match the expected schema."""


# ---------------------------------------------------------------------------
# Crude PV model — see module docstring. Replaced by Solcast post-launch.
# ---------------------------------------------------------------------------

PV_PEAK_W = 7000.0
"""Clear-sky peak output, watts. Calibrated against Roel's 26 panels /
~11.000 kWh/year array; refine when real production data lands."""

_DAYLIGHT_START_H = 6.0
_DAYLIGHT_END_H = 20.0
_DAYLIGHT_SPAN_H = _DAYLIGHT_END_H - _DAYLIGHT_START_H  # peak at 13:00 (midpoint)
_CLOUD_ATTENUATION = 0.7  # 100% clouds → output * (1 - 0.7) = 30% of clear-sky


def pv_estimate_w(local_dt: datetime, cloud_cover_pct: float) -> float:
    """Crude PV estimate in watts.

    ``local_dt`` is local Europe/Amsterdam time. ``cloud_cover_pct`` is the
    Open-Meteo cloud-cover percentage [0..100].

    Sine elevation curve over a fixed 06:00–20:00 window peaking at 13:00,
    times a linear cloud-attenuation factor. Returns 0 outside the window.
    """
    hour = local_dt.hour + local_dt.minute / 60.0
    if hour < _DAYLIGHT_START_H or hour > _DAYLIGHT_END_H:
        return 0.0
    fraction = math.sin(math.pi * (hour - _DAYLIGHT_START_H) / _DAYLIGHT_SPAN_H)
    elevation_factor = max(0.0, fraction)
    cloud_factor = 1.0 - _CLOUD_ATTENUATION * (cloud_cover_pct / 100.0)
    return PV_PEAK_W * elevation_factor * cloud_factor


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class HourlyForecast(BaseModel):
    """One hour of forecast, with the crude PV estimate already applied."""

    model_config = ConfigDict(extra="ignore")

    timestamp_utc: datetime
    temperature_c: float
    cloud_cover_pct: float
    pv_estimate_w: float


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_DEFAULT_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_DEFAULT_TIMEOUT_S = 5.0
_LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# Sittard, Netherlands. CLAUDE.md briefly listed 51.99°N (a typo — that's
# near Eindhoven). Correct value is 50.99°N, 5.87°E.
_SITTARD_LAT = 50.99
_SITTARD_LON = 5.87


class OpenMeteoClient:
    """Async Open-Meteo client.

    Usage::

        async with OpenMeteoClient.from_env() as om:
            forecast = await om.get_forecast(hours=48)

    The client owns its own ``httpx.AsyncClient`` unless one is injected,
    which makes test mocking via :class:`httpx.MockTransport` straightforward.
    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._latitude = latitude
        self._longitude = longitude
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._http = http
        self._owns_http = http is None

    @classmethod
    def from_env(cls) -> OpenMeteoClient:
        """Construct from ``OPENMETEO_LATITUDE`` / ``OPENMETEO_LONGITUDE``.

        Defaults to Sittard (50.99°N, 5.87°E) when the env vars are unset,
        which is the only deployment that exists today. ``OPENMETEO_BASE_URL``
        is also honoured for staging or local fixtures.
        """
        lat = _env_float("OPENMETEO_LATITUDE", _SITTARD_LAT)
        lon = _env_float("OPENMETEO_LONGITUDE", _SITTARD_LON)
        base_url = os.environ.get("OPENMETEO_BASE_URL", _DEFAULT_BASE_URL).strip()
        return cls(latitude=lat, longitude=lon, base_url=base_url or _DEFAULT_BASE_URL)

    # --- async-context plumbing ------------------------------------------

    async def __aenter__(self) -> Self:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=httpx.AsyncHTTPTransport(retries=2),
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- public surface --------------------------------------------------

    async def get_forecast(self, hours: int = 48) -> list[HourlyForecast]:
        """Fetch hourly forecast and parse it into ``HourlyForecast`` rows.

        ``hours`` clips the result to the next *N* hours from the start of
        the response. Open-Meteo always returns whole-day arrays, so the
        default of 48 maps to the ``forecast_days=2`` request.
        """
        params = {
            "latitude": f"{self._latitude}",
            "longitude": f"{self._longitude}",
            "hourly": "temperature_2m,cloud_cover",
            "timezone": "Europe/Amsterdam",
            "forecast_days": "2",
        }
        raw = await self._get_json(params)
        return _parse_forecast(raw, hours=hours)

    # --- internals -------------------------------------------------------

    async def _get_json(self, params: dict[str, str]) -> Any:
        if self._http is None:
            raise RuntimeError("OpenMeteoClient must be used as an async context manager")

        try:
            response = await self._http.get(self._base_url, params=params)
        except httpx.TimeoutException as exc:
            raise OpenMeteoUnavailable("timeout calling Open-Meteo") from exc
        except httpx.RequestError as exc:
            raise OpenMeteoUnavailable(f"network error calling Open-Meteo: {exc}") from exc

        if 500 <= response.status_code < 600:
            raise OpenMeteoUnavailable(f"{response.status_code} from Open-Meteo")
        if response.status_code != 200:
            raise OpenMeteoError(f"unexpected status {response.status_code} from Open-Meteo")

        try:
            return response.json()
        except ValueError as exc:
            raise OpenMeteoMalformed("non-JSON response from Open-Meteo") from exc


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _parse_forecast(raw: Any, *, hours: int) -> list[HourlyForecast]:
    if not isinstance(raw, dict):
        raise OpenMeteoMalformed(f"expected JSON object, got {type(raw).__name__}")

    hourly = raw.get("hourly")
    if not isinstance(hourly, dict):
        raise OpenMeteoMalformed("missing or non-object 'hourly' block")

    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    clouds = hourly.get("cloud_cover")
    if not isinstance(times, list) or not isinstance(temps, list) or not isinstance(clouds, list):
        raise OpenMeteoMalformed("'hourly' must contain time/temperature_2m/cloud_cover lists")
    if not (len(times) == len(temps) == len(clouds)):
        raise OpenMeteoMalformed("hourly arrays have mismatched lengths")

    out: list[HourlyForecast] = []
    for raw_time, raw_temp, raw_cloud in zip(times[:hours], temps[:hours], clouds[:hours], strict=False):
        try:
            local_dt = datetime.fromisoformat(str(raw_time)).replace(tzinfo=_LOCAL_TZ)
            utc_dt = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            cloud_pct = float(raw_cloud)
            forecast = HourlyForecast(
                timestamp_utc=utc_dt,
                temperature_c=float(raw_temp),
                cloud_cover_pct=cloud_pct,
                pv_estimate_w=pv_estimate_w(local_dt.replace(tzinfo=None), cloud_pct),
            )
        except (ValueError, TypeError, ValidationError) as exc:
            raise OpenMeteoMalformed(f"bad row {raw_time!r}: {exc}") from exc
        out.append(forecast)
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise OpenMeteoError(f"{name} must be a float, got {value!r}") from exc


__all__ = [
    "PV_PEAK_W",
    "HourlyForecast",
    "OpenMeteoClient",
    "OpenMeteoError",
    "OpenMeteoMalformed",
    "OpenMeteoUnavailable",
    "pv_estimate_w",
]
