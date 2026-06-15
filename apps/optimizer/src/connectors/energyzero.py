"""
EnergyZero connector — publieke kwartier-day-ahead-prijsfeed.

Architectuur
------------
EnergyZero zit onder de motorkap van het Zonneplan-dynamisch-contract en
publiceert de Europese day-ahead-spotprijs per kwartier (sinds 01-10-2025
draait EPEX op kwartier-resolutie, daarvoor was het per uur).

We kiezen EnergyZero boven ENTSO-E omdat:

  * geen auth-token nodig (public endpoint),
  * native kwartier-resolutie (matcht onze engine 1-op-1),
  * meegeleverde JSON kent een schone "base" stream — kale spot, excl. btw,
    excl. inkoopvergoeding/energiebelasting. Onze engine doet die opbouw
    al via ``TARIFF_CONFIG`` (zie ``optimizer/dispositie.py``).

ENTSO-E blijft beschikbaar als fallback voor het uur-tarief en wordt al door
``connectors/entsoe.py`` afgevangen — die module raken we niet aan.

API
---
``GET https://public.api.energyzero.nl/public/v1/prices``

Query-parameters:
  * ``energyType=ENERGY_TYPE_ELECTRICITY``
  * ``date=DD-MM-YYYY``  — lokale Europe/Amsterdam-dag (DST-safe via inDST-keuze)
  * ``interval=INTERVAL_QUARTER``

Response is JSON met een ``base``-array; elk item heeft een ISO 8601 UTC
``start`` + ``end`` en ``price.value`` in EUR/kWh. Op DST-wissel zit er
respectievelijk 92 (lente, 23 uur) of 100 (herfst, 25 uur) kwartieren in
de dag.

Publicatie: dag-vooruit-prijzen verschijnen op werkdagen rond 13:00–15:00
CET/CEST. We cachen ze in-memory per dag-key zodat we maximaal één
HTTP-call per dag per Cloud Run-instance maken.

Referenties
-----------
* https://public.api.energyzero.nl/public/v1/prices  (publieke endpoint)
* https://github.com/klaasnicolaas/python-energyzero  (open-source client als ground-truth)
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
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
# Subclassed exceptions — vendor name in stack trace
# ---------------------------------------------------------------------------


class EnergyZeroError(ConnectorError):
    """Base voor EnergyZero-specifieke fouten."""


class EnergyZeroUnavailable(EnergyZeroError, ConnectorUnavailable):
    """Tijdelijk: timeout, 5xx, netwerkblip."""


class EnergyZeroMalformed(EnergyZeroError, ConnectorMalformed):
    """200 OK, maar JSON-body matcht niet."""


# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------


_DEFAULT_BASE_URL = "https://public.api.energyzero.nl/public/v1/prices"
_DEFAULT_TIMEOUT_S = 5.0
_NL_TZ = ZoneInfo("Europe/Amsterdam")


# ---------------------------------------------------------------------------
# Datamodel
# ---------------------------------------------------------------------------


class QuarterPrice(BaseModel):
    """Eén kwartier-day-ahead-prijs uit de ``base``-stream van EnergyZero.

    ``start_utc`` is het begin van het kwartier in UTC (zoals door EnergyZero
    geleverd). ``spot_eur_kwh`` is de KALE spotprijs (excl. btw, excl.
    inkoopvergoeding/energiebelasting) — vergelijkbaar met ENTSO-E
    ``spot_eur_mwh / 1000``. De engine telt zelf de Zonneplan-opslag en de
    energiebelasting erbij (zie ``optimizer/dispositie.py:import_price``).
    """

    model_config = ConfigDict(extra="ignore")

    start_utc: datetime
    spot_eur_kwh: float


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class EnergyZeroClient:
    """Async EnergyZero kwartier-prijsfeed.

    Usage::

        async with EnergyZeroClient.from_env() as ez:
            prices = await ez.get_quarter_prices(date.today())

    Caching is in-memory per dag-key. Een instantie is bedoeld voor één
    Cloud Run-call; voor een lang-levende worker is de cache klein (een
    paar dagen aan kwartieren).
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._http = http
        self._owns_http = http is None
        self._cache: dict[date, list[QuarterPrice]] = {}

    @classmethod
    def from_env(cls) -> EnergyZeroClient:
        """Construct vanuit env; ``ENERGYZERO_BASE_URL`` is optioneel (default = public endpoint)."""
        base_url = os.environ.get("ENERGYZERO_BASE_URL", "").strip() or _DEFAULT_BASE_URL
        return cls(base_url=base_url)

    # --- async-context plumbing ----------------------------------------------

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

    # --- public surface ------------------------------------------------------

    async def get_quarter_prices(self, local_date: date) -> list[QuarterPrice]:
        """Fetch alle kwartier-day-ahead-prijzen voor één lokale dag (Europe/Amsterdam).

        DST-safe: lente-wissel geeft 23 uur (92 kwartieren), herfst 25 uur (100).
        Wordt gecached per dag.
        """
        cached = self._cache.get(local_date)
        if cached is not None:
            return cached

        if self._http is None:
            raise EnergyZeroError("Client niet als async-context geopend (gebruik `async with`).")

        params = {
            "energyType": "ENERGY_TYPE_ELECTRICITY",
            "date": local_date.strftime("%d-%m-%Y"),
            "interval": "INTERVAL_QUARTER",
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": "hesm-huisjes/1.0 (https://github.com/roelhuis-collab/hesm-huisjes)",
        }

        try:
            resp = await self._http.get(self._base_url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise EnergyZeroUnavailable(f"EnergyZero unreachable: {exc}") from exc

        if resp.status_code == 404:
            # EnergyZero geeft 404 als de dag-vooruit-prijzen nog niet zijn gepubliceerd.
            self._cache[local_date] = []
            return []
        if resp.status_code >= 500:
            raise EnergyZeroUnavailable(f"EnergyZero 5xx ({resp.status_code})")
        if resp.status_code >= 400:
            raise EnergyZeroMalformed(f"EnergyZero {resp.status_code}: {resp.text[:200]}")

        try:
            payload: Any = resp.json()
        except ValueError as exc:
            raise EnergyZeroMalformed(f"EnergyZero non-JSON body: {exc}") from exc

        prices = _parse_base_stream(payload)
        self._cache[local_date] = prices
        return prices

    async def quarter_price_for(self, when: datetime) -> QuarterPrice | None:
        """Geef de prijs voor het kwartier waar ``when`` in valt, of None bij gat.

        ``when`` mag naïef of aware zijn. Naïef → behandeld als UTC (Cloud Run
        draait UTC). De lokale dag wordt afgeleid via Europe/Amsterdam zodat
        de EnergyZero-query naar de juiste publicatie wijst.
        """
        utc = when if when.tzinfo else when.replace(tzinfo=UTC)
        local_date = utc.astimezone(_NL_TZ).date()

        prices = await self.get_quarter_prices(local_date)
        if not prices:
            return None

        floor_utc = _floor_to_quarter(utc.astimezone(UTC))
        for p in prices:
            if p.start_utc == floor_utc:
                return p
        return None


# ---------------------------------------------------------------------------
# Parser + helpers
# ---------------------------------------------------------------------------


def _floor_to_quarter(when: datetime) -> datetime:
    """Round een UTC-datetime naar beneden naar 15-min grens."""
    minute = (when.minute // 15) * 15
    return when.replace(minute=minute, second=0, microsecond=0)


def _parse_base_stream(payload: Any) -> list[QuarterPrice]:
    """Lees de ``base``-array (kale spot, excl. btw) uit het EnergyZero-antwoord.

    Resilient: een misvormd item logt zichzelf weg maar laat de rest staan; pas
    bij een ontbrekende ``base``-key of een lege payload faalt het volledig.
    """
    if not isinstance(payload, dict):
        raise EnergyZeroMalformed("Payload is niet een JSON-object.")

    raw = payload.get("base")
    if not isinstance(raw, list):
        raise EnergyZeroMalformed("Payload mist een 'base'-array.")

    out: list[QuarterPrice] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start_raw = item.get("start")
        price_obj = item.get("price")
        if not isinstance(start_raw, str) or not isinstance(price_obj, dict):
            continue
        value = price_obj.get("value")
        if not isinstance(value, int | float):
            continue
        try:
            start = _parse_iso_utc(start_raw)
            qp = QuarterPrice(start_utc=start, spot_eur_kwh=float(value))
        except (ValueError, ValidationError):
            continue
        out.append(qp)

    out.sort(key=lambda q: q.start_utc)
    return out


def _parse_iso_utc(raw: str) -> datetime:
    """Parse ISO 8601 met trailing 'Z' → UTC-aware datetime."""
    cleaned = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = [
    "EnergyZeroClient",
    "EnergyZeroError",
    "EnergyZeroMalformed",
    "EnergyZeroUnavailable",
    "QuarterPrice",
]
