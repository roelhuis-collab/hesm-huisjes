"""
ENTSO-E Transparency Platform connector — day-ahead spot prices.

Architecture note
-----------------
ENTSO-E is the wholesale data feed: it publishes the EPEX day-ahead
auction result (€/MWh per hour, per bidding zone) for the next day around
13:00 CET. The optimizer needs those prices in EUR/kWh, all-in (i.e.
including the residential meta-layer of energy tax + supplier markup),
because the cost objective compares against the household's import meter.

We deliberately do **not** subscribe via SFTP or message broker — a once-
or-twice-daily pull is plenty for a 15-minute optimizer cycle. We hit the
public REST API with a security token and parse the XML response.

API surface
-----------
``GET https://web-api.tp.entsoe.eu/api``

Required query parameters for day-ahead prices:
  * ``securityToken``  — issued by ENTSO-E (request via transparency@entsoe.eu)
  * ``documentType=A44`` — Price Document
  * ``processType=A01``  — Day-ahead
  * ``in_Domain``        — bidding zone EIC (NL = ``10YNL----------L``)
  * ``out_Domain``       — same as ``in_Domain`` for prices
  * ``periodStart``      — ``YYYYMMDDhhmm`` UTC inclusive
  * ``periodEnd``        — ``YYYYMMDDhhmm`` UTC exclusive

Response: XML ``Publication_MarketDocument`` containing one ``TimeSeries``
with a ``Period`` of 24 (or 23/25 on DST boundaries) ``Point`` elements,
each carrying a 1-based ``position`` and ``price.amount`` in €/MWh.

Conversion to all-in EUR/kWh
----------------------------
``all_in = ((spot_eur_mwh / 1000) + ENERGY_TAX + SUPPLIER_MARKUP) * (1 + VAT_RATE)``

This matches how Tibber / Frank / EnergyZero present their tariffs: spot
clearing price plus excise tax plus supplier markup, with 21% BTW applied
to the whole subtotal. The dashboard's savings figures and the
optimizer's cost objective both work in these VAT-inclusive €/kWh.

References
----------
* https://transparency.entsoe.eu/content/static_content/Static%20content/web%20api/Guide.html
* https://documenter.getpostman.com/view/7009892/2s93JtP3F6
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Self

import httpx
from defusedxml import ElementTree as ET  # noqa: N817 — stdlib's standard alias
from pydantic import BaseModel, ConfigDict

from src.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)

# ---------------------------------------------------------------------------
# Subclassed exceptions — keep the ENTSO-E-specific name in stack traces
# while the orchestrator catches the shared base.
# ---------------------------------------------------------------------------


class EntsoeError(ConnectorError):
    """Base for ENTSO-E-specific failures."""


class EntsoeAuthError(EntsoeError, ConnectorAuthError):
    """Missing or rejected security token (401/403, or env var unset)."""


class EntsoeUnavailable(EntsoeError, ConnectorUnavailable):
    """Transparency platform down, timeout, network blip, 5xx."""


class EntsoeMalformed(EntsoeError, ConnectorMalformed):
    """200 OK but the XML body did not match the expected shape."""


# ---------------------------------------------------------------------------
# Conversion constants — see module docstring.
# ---------------------------------------------------------------------------

# Belasting op leveringen (energy tax + ODE), residential, 2026 estimate.
ENERGY_TAX_EUR_KWH: float = 0.1108
# Average dynamic-tariff supplier fee (Tibber / Frank / EnergyZero range).
SUPPLIER_MARKUP_EUR_KWH: float = 0.025
# Dutch VAT applied to the full subtotal (spot + tax + markup).
VAT_RATE: float = 0.21

# Bidding-zone EIC code for the Netherlands.
_NL_DOMAIN = "10YNL----------L"
_DOC_TYPE_DAY_AHEAD = "A44"
_PROCESS_TYPE_DAY_AHEAD = "A01"

_DEFAULT_BASE_URL = "https://web-api.tp.entsoe.eu/api"
_DEFAULT_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HourlyPrice(BaseModel):
    """One hourly day-ahead price slot, post-conversion.

    ``timestamp_utc`` is the start of the hourly slot in UTC.
    ``spot_eur_mwh`` is the raw EPEX clearing price.
    ``all_in_eur_kwh`` is VAT-inclusive: spot + energy tax + supplier
    markup, all multiplied by ``1 + VAT_RATE``.
    """

    model_config = ConfigDict(extra="ignore")

    timestamp_utc: datetime
    spot_eur_mwh: float
    all_in_eur_kwh: float


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class EntsoeClient:
    """Async ENTSO-E Transparency Platform client.

    Usage::

        async with EntsoeClient.from_env() as ent:
            prices = await ent.get_day_ahead_prices(date.today())

    The client owns its own ``httpx.AsyncClient`` unless one is injected,
    which makes test mocking via :class:`httpx.MockTransport` straightforward.
    """

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_token:
            raise ValueError("api_token is required")
        self._api_token = api_token
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._http = http
        self._owns_http = http is None

    @classmethod
    def from_env(cls) -> EntsoeClient:
        """Construct from ``ENTSOE_API_TOKEN``. Raises if unset."""
        token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
        if not token:
            raise EntsoeAuthError(
                "ENTSOE_API_TOKEN is not set — request a token via "
                "transparency@entsoe.eu and store it in Secret Manager."
            )
        return cls(api_token=token)

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

    async def get_day_ahead_prices(self, date_local: date) -> list[HourlyPrice]:
        """Fetch hourly day-ahead prices for a given local (Europe/Amsterdam) day.

        ``date_local`` is interpreted as a calendar day in the NL bidding
        zone. We request a UTC window that covers the full day; ENTSO-E
        returns 24 points on regular days, 23 on the spring-forward DST
        Sunday, and 25 on the autumn fall-back Sunday. We tolerate all
        three.
        """
        # NL is UTC+1 (winter) / UTC+2 (summer). To safely cover the local
        # day without dragging in a tz library, we ask for a 26-hour UTC
        # window starting at the previous midnight UTC and ending past the
        # local day's last hour. ENTSO-E returns only the points that
        # actually exist for the requested window, deduplicated by domain.
        period_start = datetime(date_local.year, date_local.month, date_local.day, tzinfo=UTC) \
            - timedelta(hours=2)
        period_end = period_start + timedelta(hours=28)

        params = {
            "securityToken": self._api_token,
            "documentType": _DOC_TYPE_DAY_AHEAD,
            "processType": _PROCESS_TYPE_DAY_AHEAD,
            "in_Domain": _NL_DOMAIN,
            "out_Domain": _NL_DOMAIN,
            "periodStart": _fmt_period(period_start),
            "periodEnd": _fmt_period(period_end),
        }
        body = await self._get_xml(params)
        return _parse_day_ahead_prices(body)

    # --- internals -------------------------------------------------------

    async def _get_xml(self, params: dict[str, str]) -> bytes:
        if self._http is None:
            raise RuntimeError("EntsoeClient must be used as an async context manager")

        try:
            response = await self._http.get(self._base_url, params=params)
        except httpx.TimeoutException as exc:
            raise EntsoeUnavailable("timeout calling ENTSO-E") from exc
        except httpx.RequestError as exc:
            raise EntsoeUnavailable(f"network error calling ENTSO-E: {exc}") from exc

        if response.status_code in (401, 403):
            raise EntsoeAuthError(
                f"{response.status_code} from ENTSO-E — security token rejected"
            )
        if 500 <= response.status_code < 600:
            raise EntsoeUnavailable(f"{response.status_code} from ENTSO-E")
        if 400 <= response.status_code < 500:
            raise EntsoeError(f"{response.status_code} from ENTSO-E")
        if response.status_code != 200:
            raise EntsoeError(f"unexpected status {response.status_code} from ENTSO-E")

        return response.content


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fmt_period(moment: datetime) -> str:
    """Format a UTC datetime as ENTSO-E ``YYYYMMDDhhmm``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def _parse_period_start(text: str) -> datetime:
    """Parse an ENTSO-E ``timeInterval/start`` value (``YYYY-MM-DDThh:mmZ``)."""
    # ENTSO-E omits seconds and uses a literal "Z". fromisoformat handles
    # offsets but not the bare "Z" suffix on Python <3.11; we're on 3.12 so
    # this works, but normalize defensively.
    normalized = text.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(UTC)


def _strip_ns(tag: str) -> str:
    """``{urn:...}TimeSeries`` -> ``TimeSeries``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findall_local(node: ET.Element, name: str) -> list[ET.Element]:
    """Find direct-or-deep descendants by local name, ignoring XML namespace."""
    return [el for el in node.iter() if _strip_ns(el.tag) == name]


def _find_local(node: ET.Element, name: str) -> ET.Element | None:
    for el in node.iter():
        if _strip_ns(el.tag) == name:
            return el
    return None


def _parse_day_ahead_prices(body: bytes) -> list[HourlyPrice]:
    """Walk the Publication_MarketDocument and return one HourlyPrice per Point."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise EntsoeMalformed(f"invalid XML from ENTSO-E: {exc}") from exc

    out: list[HourlyPrice] = []
    timeseries = _findall_local(root, "TimeSeries")
    if not timeseries:
        raise EntsoeMalformed("no TimeSeries elements in ENTSO-E response")

    for ts in timeseries:
        for period in _findall_local(ts, "Period"):
            interval = _find_local(period, "timeInterval")
            if interval is None:
                raise EntsoeMalformed("Period missing timeInterval")
            start_el = _find_local(interval, "start")
            if start_el is None or start_el.text is None:
                raise EntsoeMalformed("timeInterval missing start")
            try:
                period_start = _parse_period_start(start_el.text.strip())
            except ValueError as exc:
                raise EntsoeMalformed(f"unparseable timeInterval/start: {exc}") from exc

            for point in _findall_local(period, "Point"):
                pos_el = _find_local(point, "position")
                amt_el = _find_local(point, "price.amount")
                if pos_el is None or pos_el.text is None:
                    raise EntsoeMalformed("Point missing position")
                if amt_el is None or amt_el.text is None:
                    raise EntsoeMalformed("Point missing price.amount")
                try:
                    position = int(pos_el.text.strip())
                    spot_eur_mwh = float(amt_el.text.strip())
                except ValueError as exc:
                    raise EntsoeMalformed(f"non-numeric Point payload: {exc}") from exc

                # position is 1-based; each step is one hour.
                ts_utc = period_start + timedelta(hours=position - 1)
                subtotal = (spot_eur_mwh / 1000.0) + ENERGY_TAX_EUR_KWH + SUPPLIER_MARKUP_EUR_KWH
                all_in = subtotal * (1.0 + VAT_RATE)
                out.append(
                    HourlyPrice(
                        timestamp_utc=ts_utc,
                        spot_eur_mwh=spot_eur_mwh,
                        all_in_eur_kwh=all_in,
                    )
                )

    if not out:
        raise EntsoeMalformed("ENTSO-E response contained no Point elements")

    out.sort(key=lambda p: p.timestamp_utc)
    return out


# ---------------------------------------------------------------------------
# Mock + factory — used when no ENTSOE_API_TOKEN is set.
# ---------------------------------------------------------------------------

# Realistic NL day-ahead pattern: low at night, peak 18-20h. Wholesale €/MWh.
_DAILY_SHAPE_EUR_MWH = [
    35, 30, 28, 27, 28, 32, 45, 65, 85, 75, 60, 50,   # 00..11
    45, 42, 40, 45, 60, 90, 120, 110, 80, 65, 55, 45,  # 12..23
]


class MockEntsoeClient:
    """Synthetic day-ahead prices following a typical NL daily curve.

    Returns 24 ``HourlyPrice`` rows for the requested local day. Used when
    no ENTSO-E API token is in the env so the optimizer cycle and the
    dashboard chart still have data to work with.
    """

    async def get_day_ahead_prices(self, date_local: date) -> list[HourlyPrice]:
        # Day in question runs from local midnight to local midnight; treat
        # local==UTC for the mock (Roel is UTC+1/+2; close enough for fakes).
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        start = _dt(date_local.year, date_local.month, date_local.day, tzinfo=UTC)
        out: list[HourlyPrice] = []
        for hour, spot_eur_mwh in enumerate(_DAILY_SHAPE_EUR_MWH):
            ts = start + _td(hours=hour)
            subtotal = (
                (spot_eur_mwh / 1000.0) + ENERGY_TAX_EUR_KWH + SUPPLIER_MARKUP_EUR_KWH
            )
            all_in = subtotal * (1 + VAT_RATE)
            out.append(
                HourlyPrice(
                    timestamp_utc=ts,
                    spot_eur_mwh=float(spot_eur_mwh),
                    all_in_eur_kwh=all_in,
                )
            )
        return out

    async def __aenter__(self) -> MockEntsoeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def entsoe_client() -> EntsoeClient | MockEntsoeClient:
    """Return a real client when the token is set, mock otherwise.

    Both implementations share ``get_day_ahead_prices(date)``; callers
    don't need to branch.
    """
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    if token:
        return EntsoeClient(api_token=token)
    return MockEntsoeClient()


def is_using_mock_entsoe() -> bool:
    return not os.environ.get("ENTSOE_API_TOKEN", "").strip()
