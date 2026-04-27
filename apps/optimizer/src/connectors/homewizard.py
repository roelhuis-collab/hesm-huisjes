"""
HomeWizard P1 Meter connector — v1 local API.

Architecture note
-----------------
HomeWizard does not expose a public, documented cloud API. The official API
is local-network only (HTTPS or HTTP on the device's LAN IP). To keep
Cloud Run cloud-only the way CLAUDE.md intends, the LAN exposure is
delegated to a thin tunnel (Cloudflare Tunnel or Tailscale) running on
something always-on at home — a router, a NAS, or a Raspberry Pi Zero.
That tunnel device is *not* edge compute: it ships ~50 MB of memory and
zero optimizer logic. If it dies, the next cycle gets stale data, the
optimizer falls back to its rule-based baseline, and Layer 1 limits keep
the house safe.

The setup of that tunnel lives in ``infra/SETUP.md`` (added in PR5).
This connector only knows about a base URL — wherever the local API has
been published.

Endpoints
---------
``GET <base>/api``         — device info (product type, firmware, API version)
``GET <base>/api/v1/data`` — current measurement, all fields optional

Updates arrive every second on DSMR 5.0 meters, every 10 s on older ones.
We poll every 15 minutes from Cloud Scheduler, so freshness is never an
issue.

References
----------
* https://api-documentation.homewizard.com/docs/v1/measurement
* https://api-documentation.homewizard.com/docs/v1/api
"""

from __future__ import annotations

import os
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)

# ---------------------------------------------------------------------------
# Subclassed exceptions — keep the HomeWizard-specific name in stack traces
# while the orchestrator catches the shared base.
# ---------------------------------------------------------------------------


class HomeWizardError(ConnectorError):
    """Base for HomeWizard-specific failures."""


class HomeWizardAuthError(HomeWizardError, ConnectorAuthError):
    """Tunnel-level auth (Cloudflare Access etc.) rejected the request."""


class HomeWizardUnavailable(HomeWizardError, ConnectorUnavailable):
    """Tunnel down, P1 meter offline, timeout, 5xx."""


class HomeWizardMalformed(HomeWizardError, ConnectorMalformed):
    """Endpoint returned 200 but body didn't match the expected schema."""


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HomeWizardDevice(BaseModel):
    """Result of ``GET /api`` — capability/version handshake."""

    model_config = ConfigDict(extra="ignore")

    product_type: str
    product_name: str
    serial: str
    firmware_version: str
    api_version: str


class P1MeterReading(BaseModel):
    """A single snapshot of the P1 meter, post-parse.

    Field convention: ``active_power_w`` is the **net** instantaneous power
    seen at the smart meter — positive means importing from the grid,
    negative means exporting. All numeric fields are optional because
    HomeWizard documents them as optional and older meters or non-DSMR-5
    deployments simply don't report some of them.
    """

    model_config = ConfigDict(extra="ignore")

    captured_at: datetime
    active_power_w: float | None = None
    active_power_l1_w: float | None = None
    active_power_l2_w: float | None = None
    active_power_l3_w: float | None = None
    total_import_kwh: float | None = None
    total_export_kwh: float | None = None
    total_gas_m3: float | None = None
    smr_version: int | None = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


_DEFAULT_TIMEOUT_S = 5.0


class HomeWizardP1Client:
    """Async HomeWizard P1 client.

    Usage::

        async with HomeWizardP1Client.from_env() as hw:
            reading = await hw.get_measurement()

    The client owns its own ``httpx.AsyncClient`` unless one is injected,
    which makes test mocking via :class:`httpx.MockTransport` straightforward.
    """

    def __init__(
        self,
        base_url: str,
        *,
        extra_headers: dict[str, str] | None = None,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._base_url = base_url.rstrip("/")
        self._extra_headers = dict(extra_headers or {})
        self._timeout_s = timeout_s
        self._http = http
        self._owns_http = http is None

    @classmethod
    def from_env(cls) -> HomeWizardP1Client:
        """Construct from ``HOMEWIZARD_BASE_URL`` (and optional headers).

        Headers may be injected via ``HOMEWIZARD_HEADER_<NAME>`` env vars,
        e.g. ``HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_ID`` becomes the
        ``CF-Access-Client-Id`` header on every request. This lets PR5
        wire the secrets via Secret Manager without touching this module.
        """
        base_url = os.environ.get("HOMEWIZARD_BASE_URL", "").strip()
        if not base_url:
            raise HomeWizardAuthError(
                "HOMEWIZARD_BASE_URL is not set — point this at the tunneled "
                "P1 meter URL, e.g. https://hwz.huisjes.dev"
            )
        headers = {
            _env_to_header(k): v
            for k, v in os.environ.items()
            if k.startswith("HOMEWIZARD_HEADER_")
        }
        return cls(base_url=base_url, extra_headers=headers)

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

    async def get_device_info(self) -> HomeWizardDevice:
        """Capability handshake — verify product_type and API version.

        Call once at startup. Avoids surprises if the tunnel ever points
        at a non-P1 device.
        """
        raw = await self._get_json("/api")
        try:
            return HomeWizardDevice.model_validate(raw)
        except ValidationError as exc:
            raise HomeWizardMalformed(f"unexpected device info shape: {exc}") from exc

    async def get_measurement(self) -> P1MeterReading:
        """Current P1 measurement. The fields HomeWizard didn't report stay None."""
        raw = await self._get_json("/api/v1/data")
        if not isinstance(raw, dict):
            raise HomeWizardMalformed(f"expected JSON object, got {type(raw).__name__}")

        try:
            return P1MeterReading(
                captured_at=datetime.now(),
                active_power_w=_as_float(raw.get("active_power_w")),
                active_power_l1_w=_as_float(raw.get("active_power_l1_w")),
                active_power_l2_w=_as_float(raw.get("active_power_l2_w")),
                active_power_l3_w=_as_float(raw.get("active_power_l3_w")),
                total_import_kwh=_as_float(raw.get("total_power_import_kwh")),
                total_export_kwh=_as_float(raw.get("total_power_export_kwh")),
                total_gas_m3=_as_float(raw.get("total_gas_m3")),
                smr_version=_as_int(raw.get("smr_version")),
            )
        except ValidationError as exc:
            raise HomeWizardMalformed(f"unexpected measurement shape: {exc}") from exc

    # --- internals -------------------------------------------------------

    async def _get_json(self, path: str) -> Any:
        if self._http is None:
            raise RuntimeError("HomeWizardP1Client must be used as an async context manager")

        url = f"{self._base_url}{path}"
        try:
            response = await self._http.get(url, headers=self._extra_headers)
        except httpx.TimeoutException as exc:
            raise HomeWizardUnavailable(f"timeout calling {path}") from exc
        except httpx.RequestError as exc:
            raise HomeWizardUnavailable(f"network error calling {path}: {exc}") from exc

        if response.status_code in (401, 403):
            raise HomeWizardAuthError(
                f"{response.status_code} on {path} — check tunnel auth headers"
            )
        if 500 <= response.status_code < 600:
            raise HomeWizardUnavailable(f"{response.status_code} on {path}")
        if response.status_code != 200:
            raise HomeWizardError(f"unexpected status {response.status_code} on {path}")

        try:
            return response.json()
        except ValueError as exc:
            raise HomeWizardMalformed(f"non-JSON response on {path}") from exc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _env_to_header(env_name: str) -> str:
    """Convert ``HOMEWIZARD_HEADER_CF_ACCESS_CLIENT_ID`` → ``CF-Access-Client-Id``."""
    body = env_name[len("HOMEWIZARD_HEADER_") :]
    parts = body.split("_")
    return "-".join(p.capitalize() for p in parts)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
