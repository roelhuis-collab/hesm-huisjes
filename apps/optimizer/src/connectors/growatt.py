"""
Growatt connector — PV inverter productie + per-fase vermogen.

Implementatie tegen de reverse-engineered ShinePhone-cloud-API
(``openapi.growatt.com``). De officiële Growatt-API vereist een vendor-
account dat consumenten niet hebben — de ShinePhone-API gebruikt dezelfde
credentials als de mobile app en is open-source goed gedocumenteerd
(``indykoning/PyPi_GrowattServer``).

Flow per cycle (cached na de eerste call):
  1. POST ``/newTwoLoginAPI.do`` met username + MD5-gehashed wachtwoord
     (Growatt-specifieke 0→c-substitutie op even posities) → sessie-cookie.
  2. GET ``/PlantListAPI.do`` → plantId van de eerste installatie.
  3. GET ``/newTwoPlantAPI.do?op=getAllDeviceListTwo`` → inverter-serial.
  4. GET ``/newInverterAPI.do?op=getInverterDetailData`` → live waarden.

Wij cachen plant- en inverter-IDs voor de levensduur van de client
(verandert niet binnen één Cloud Run-instance). Re-login gebeurt bij een
401-style response: cookies vervallen na een paar uur stille tijd.

Mock-fallback blijft staan zodat staging zonder creds werkt; ``growatt_client()``
kiest op basis van env vars.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)


class GrowattError(ConnectorError):
    """Base voor Growatt-specifieke fouten."""


class GrowattAuthError(GrowattError, ConnectorAuthError):
    """Missing or rejected ShinePhone credentials."""


class GrowattUnavailable(GrowattError, ConnectorUnavailable):
    """ShinePhone cloud down."""


class GrowattMalformed(GrowattError, ConnectorMalformed):
    """200 OK with an unexpected body shape."""


# Roel's array: 26 panels × ~400 W ≈ 9 kW peak DC; inverter caps at 9 kW AC.
PEAK_PV_W = 7500.0

_DEFAULT_BASE_URL = "https://openapi.growatt.com"
_DEFAULT_TIMEOUT_S = 10.0


@dataclass
class GrowattStatus:
    """One snapshot of PV inverter output."""

    captured_at: datetime
    pv_power_w: float
    daily_yield_kwh: float
    power_l1_w: float
    power_l2_w: float
    power_l3_w: float


class GrowattClient(Protocol):
    async def get_status(self) -> GrowattStatus: ...
    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Mock — synthetische zon-curve voor staging
# ---------------------------------------------------------------------------


def _solar_factor(hour: float) -> float:
    """Fake solar elevation: sine over 06:00–20:00, peak at 13:00."""
    if hour < 6 or hour > 20:
        return 0.0
    return max(0.0, math.sin(math.pi * (hour - 6) / 14))


class MockGrowattClient:
    """Synthetic PV output following a sun curve."""

    def __init__(self) -> None:
        self._rng = random.Random(0x501A8)

    async def get_status(self) -> GrowattStatus:
        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        factor = _solar_factor(hour)
        if factor > 0:
            factor *= 1.0 - 0.4 * self._rng.random()

        total = PEAK_PV_W * factor + self._rng.uniform(-30, 30) if factor > 0 else 0.0
        per_phase = total / 3.0
        elapsed_factor = max(0.0, math.sin(math.pi * (hour - 6) / 14))
        daily_yield = PEAK_PV_W / 1000.0 * 7.0 * (1 - math.cos(math.pi * elapsed_factor))

        return GrowattStatus(
            captured_at=now,
            pv_power_w=max(0.0, total),
            daily_yield_kwh=daily_yield,
            power_l1_w=per_phase + self._rng.uniform(-15, 15),
            power_l2_w=per_phase + self._rng.uniform(-15, 15),
            power_l3_w=per_phase + self._rng.uniform(-15, 15),
        )

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Real client — ShinePhone-cloud poll
# ---------------------------------------------------------------------------


def _hash_password(password: str) -> str:
    """Growatt-specifieke pre-login hash: MD5-hex, dan elke '0' op even positie naar 'c'."""
    digest = hashlib.md5(password.encode("utf-8")).hexdigest()
    chars = list(digest)
    for i in range(0, len(chars), 2):
        if chars[i] == "0":
            chars[i] = "c"
    return "".join(chars)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Growatt mengt strings en getallen door elkaar; veilige conversie."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _RealGrowattClient:
    """Async client tegen de Growatt ShinePhone-cloud."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if not username or not password:
            raise GrowattAuthError("Growatt-username of -wachtwoord ontbreekt.")
        self._username = username
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._http = http
        self._owns_http = http is None

        self._user_id: str | None = None
        self._plant_id: str | None = None
        self._inverter_sn: str | None = None

    # --- async-context plumbing -----------------------------------------

    async def __aenter__(self) -> Self:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=httpx.AsyncHTTPTransport(retries=2),
                follow_redirects=False,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- HTTP helpers ----------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None and self._owns_http:
            # Lazy-open zodat ``growatt_client().get_status()`` werkt zonder
            # expliciete ``async with`` — matcht het pattern van de mock-client
            # en de manier waarop ``cycle.py`` 'm gebruikt.
            self._http = httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=httpx.AsyncHTTPTransport(retries=2),
                follow_redirects=False,
            )
        if self._http is None:
            raise GrowattError("Geen httpx-client beschikbaar.")
        return self._http

    async def _post_form(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            resp = await self._client().post(f"{self._base_url}{path}", data=data)
        except httpx.HTTPError as exc:
            raise GrowattUnavailable(f"Growatt unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise GrowattUnavailable(f"Growatt 5xx ({resp.status_code})")
        if resp.status_code >= 400:
            raise GrowattMalformed(f"Growatt {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json() or {}
        except ValueError as exc:
            raise GrowattMalformed(f"Growatt non-JSON body: {exc}") from exc

    async def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            resp = await self._client().get(f"{self._base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            raise GrowattUnavailable(f"Growatt unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise GrowattUnavailable(f"Growatt 5xx ({resp.status_code})")
        if resp.status_code >= 400:
            raise GrowattMalformed(f"Growatt {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json() or {}
        except ValueError as exc:
            raise GrowattMalformed(f"Growatt non-JSON body: {exc}") from exc

    # --- bootstrap ------------------------------------------------------

    async def _login(self) -> None:
        body = await self._post_form(
            "/newTwoLoginAPI.do",
            {"userName": self._username, "password": _hash_password(self._password)},
        )
        back = body.get("back") if isinstance(body.get("back"), dict) else body
        if not isinstance(back, dict) or not back.get("success"):
            raise GrowattAuthError(f"Growatt-login geweigerd: {body!r}"[:200])
        user = back.get("user") if isinstance(back.get("user"), dict) else {}
        user_id = user.get("id") if isinstance(user, dict) else None
        if user_id is None:
            raise GrowattMalformed("Growatt-login zonder user.id in response.")
        self._user_id = str(user_id)

    async def _discover_plant(self) -> None:
        if self._user_id is None:
            await self._login()
        # Aangenomen: één plant per account. Voor multi-plant breidt de keuze
        # later uit naar een config-veld (GROWATT_PLANT_ID).
        body = await self._get_json("/PlantListAPI.do", {"userId": str(self._user_id)})
        plants = body.get("back") if isinstance(body.get("back"), list) else body.get("data", [])
        if not isinstance(plants, list) or not plants:
            raise GrowattMalformed("Geen plants gevonden in PlantListAPI-response.")
        first = plants[0]
        if not isinstance(first, dict):
            raise GrowattMalformed("Plant-record is geen JSON-object.")
        pid = first.get("plantId") or first.get("id") or first.get("plant_id")
        if pid is None:
            raise GrowattMalformed("Plant-record zonder plantId-veld.")
        self._plant_id = str(pid)

    async def _discover_inverter(self) -> None:
        if self._plant_id is None:
            await self._discover_plant()
        body = await self._get_json(
            "/newTwoPlantAPI.do",
            {
                "op": "getAllDeviceListTwo",
                "plantId": str(self._plant_id),
                "pageNum": "1",
                "pageSize": "1",
            },
        )
        devices = body.get("deviceList")
        if not isinstance(devices, list) or not devices:
            raise GrowattMalformed("Geen devices in getAllDeviceListTwo-response.")
        first = devices[0]
        if not isinstance(first, dict):
            raise GrowattMalformed("Device-record is geen JSON-object.")
        sn = first.get("deviceSn") or first.get("sn") or first.get("inverterSn")
        if sn is None:
            raise GrowattMalformed("Device-record zonder serial-nummer.")
        self._inverter_sn = str(sn)

    # --- public API -----------------------------------------------------

    async def get_status(self) -> GrowattStatus:
        if self._inverter_sn is None:
            await self._discover_inverter()
        body = await self._get_json(
            "/newInverterAPI.do",
            {"op": "getInverterDetailData", "inverterId": str(self._inverter_sn)},
        )
        # Response is meestal {"obj": {...}} of {"back": {...}} — beide
        # ondersteunen zonder over te schrijven welke nesting de vendor kiest.
        payload = body.get("obj") or body.get("back") or body
        if not isinstance(payload, dict):
            raise GrowattMalformed("inverter-detail payload geen JSON-object.")

        pac_w = _as_float(payload.get("pac"))
        # Sommige builds geven pac in W, andere in 0.1 W (vendor inconsistency).
        # Roel's MOD 9000TL3-X = 9 kW peak — als de waarde > 50000 ligt is het 0.1 W.
        if pac_w > 50000:
            pac_w = pac_w / 10.0

        return GrowattStatus(
            captured_at=datetime.now(),
            pv_power_w=max(0.0, pac_w),
            daily_yield_kwh=_as_float(payload.get("eToday")),
            power_l1_w=_as_float(payload.get("pacR")),
            power_l2_w=_as_float(payload.get("pacS")),
            power_l3_w=_as_float(payload.get("pacT")),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def growatt_client() -> GrowattClient:
    user = os.environ.get("GROWATT_USERNAME", "").strip()
    pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
    if user and pwd:
        return _RealGrowattClient(user, pwd)
    return MockGrowattClient()


def is_using_mock_growatt() -> bool:
    return not (
        os.environ.get("GROWATT_USERNAME", "").strip()
        and os.environ.get("GROWATT_PASSWORD", "").strip()
    )
