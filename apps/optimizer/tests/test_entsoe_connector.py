"""
Tests for the ENTSO-E day-ahead prices connector.

Coverage:
  * 24-hour happy path with correct UTC ordering and conversion math
  * 23-hour DST short-day tolerated
  * 401 / 403 → EntsoeAuthError
  * 503 → EntsoeUnavailable
  * timeout → EntsoeUnavailable
  * network error → EntsoeUnavailable
  * malformed XML → EntsoeMalformed
  * security token sent as query param, not header
  * shared-base inheritance test
  * from_env raises if token missing
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from itertools import pairwise

import httpx
import pytest
from src.connectors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMalformed,
    ConnectorUnavailable,
)
from src.connectors.entsoe import (
    ENERGY_TAX_EUR_KWH,
    SUPPLIER_MARKUP_EUR_KWH,
    VAT_RATE,
    EntsoeAuthError,
    EntsoeClient,
    EntsoeMalformed,
    EntsoeUnavailable,
)

TOKEN = "test-token-abc"
BASE = "https://entsoe.test/api"


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> EntsoeClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5.0)
    return EntsoeClient(api_token=TOKEN, base_url=BASE, http=http)


def _xml_24h(start_iso: str = "2026-04-26T22:00Z") -> str:
    """A minimal well-formed Publication_MarketDocument with 24 points."""
    points = "\n".join(
        f"      <Point><position>{i}</position>"
        f"<price.amount>{50.0 + i}</price.amount></Point>"
        for i in range(1, 25)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>{start_iso}</start>
        <end>2026-04-27T22:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
{points}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""


def _xml_n_hours(n: int, start_iso: str = "2026-03-28T23:00Z") -> str:
    points = "\n".join(
        f"      <Point><position>{i}</position>"
        f"<price.amount>{10.0 + i}</price.amount></Point>"
        for i in range(1, n + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>{start_iso}</start>
        <end>2026-03-29T22:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
{points}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""


# --- happy path ------------------------------------------------------------


async def test_get_day_ahead_prices_happy_path() -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured.update(dict(req.headers))
        return httpx.Response(
            200,
            content=_xml_24h().encode("utf-8"),
            headers={"content-type": "application/xml"},
        )

    async with _client(handler) as ent:
        prices = await ent.get_day_ahead_prices(date(2026, 4, 27))

    assert len(prices) == 24

    # Strictly increasing UTC ordering, 1-hour steps.
    for earlier, later in pairwise(prices):
        assert (later.timestamp_utc - earlier.timestamp_utc).total_seconds() == 3600

    # First slot starts at 22:00 UTC on the 26th (the timeInterval start).
    assert prices[0].timestamp_utc == datetime(2026, 4, 26, 22, 0, tzinfo=UTC)

    # Conversion math: position 1 → 51.0 €/MWh; position 5 → 55.0 €/MWh.
    # all_in = (spot/1000 + tax + markup) * (1 + VAT)
    expected_first = ((51.0 / 1000.0) + ENERGY_TAX_EUR_KWH + SUPPLIER_MARKUP_EUR_KWH) * (1 + VAT_RATE)
    assert prices[0].spot_eur_mwh == pytest.approx(51.0)
    assert prices[0].all_in_eur_kwh == pytest.approx(expected_first)

    expected_fifth = ((55.0 / 1000.0) + ENERGY_TAX_EUR_KWH + SUPPLIER_MARKUP_EUR_KWH) * (1 + VAT_RATE)
    assert prices[4].spot_eur_mwh == pytest.approx(55.0)
    assert prices[4].all_in_eur_kwh == pytest.approx(expected_fifth)


async def test_security_token_is_query_param_not_header() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["params"] = dict(req.url.params)
        captured["auth_header"] = req.headers.get("authorization")
        return httpx.Response(200, content=_xml_24h().encode("utf-8"))

    async with _client(handler) as ent:
        await ent.get_day_ahead_prices(date(2026, 4, 27))

    params = captured["params"]
    assert isinstance(params, dict)
    assert params["securityToken"] == TOKEN
    assert params["documentType"] == "A44"
    assert params["processType"] == "A01"
    assert params["in_Domain"] == "10YNL----------L"
    assert params["out_Domain"] == "10YNL----------L"
    # Format YYYYMMDDhhmm
    assert len(params["periodStart"]) == 12
    assert len(params["periodEnd"]) == 12
    # And critically: the token is NOT also leaking via Authorization.
    assert captured["auth_header"] is None


async def test_dst_short_day_23_hours_tolerated() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml_n_hours(23).encode("utf-8"))

    async with _client(handler) as ent:
        prices = await ent.get_day_ahead_prices(date(2026, 3, 29))

    assert len(prices) == 23
    # All slots distinct, ordered.
    timestamps = [p.timestamp_utc for p in prices]
    assert timestamps == sorted(timestamps)


async def test_dst_long_day_25_hours_tolerated() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_xml_n_hours(25).encode("utf-8"))

    async with _client(handler) as ent:
        prices = await ent.get_day_ahead_prices(date(2026, 10, 25))

    assert len(prices) == 25


# --- error mapping ---------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_status(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"<error/>")

    async with _client(handler) as ent:
        with pytest.raises(EntsoeAuthError):
            await ent.get_day_ahead_prices(date(2026, 4, 27))


async def test_5xx_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"<error/>")

    async with _client(handler) as ent:
        with pytest.raises(EntsoeUnavailable):
            await ent.get_day_ahead_prices(date(2026, 4, 27))


async def test_timeout_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    async with _client(handler) as ent:
        with pytest.raises(EntsoeUnavailable):
            await ent.get_day_ahead_prices(date(2026, 4, 27))


async def test_network_error_maps_to_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated dns failure")

    async with _client(handler) as ent:
        with pytest.raises(EntsoeUnavailable):
            await ent.get_day_ahead_prices(date(2026, 4, 27))


async def test_malformed_xml_maps_to_malformed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<<<not-xml>>>")

    async with _client(handler) as ent:
        with pytest.raises(EntsoeMalformed):
            await ent.get_day_ahead_prices(date(2026, 4, 27))


async def test_xml_without_timeseries_maps_to_malformed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'<?xml version="1.0"?><Acknowledgement_MarketDocument/>',
        )

    async with _client(handler) as ent:
        with pytest.raises(EntsoeMalformed):
            await ent.get_day_ahead_prices(date(2026, 4, 27))


# --- shared base hierarchy -------------------------------------------------


async def test_subclasses_can_be_caught_via_shared_base() -> None:
    """Orchestrator catches ConnectorUnavailable; verify the inheritance."""
    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client(unavailable) as ent:
        with pytest.raises(ConnectorUnavailable):
            await ent.get_day_ahead_prices(date(2026, 4, 27))

    def auth(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with _client(auth) as ent:
        with pytest.raises(ConnectorAuthError):
            await ent.get_day_ahead_prices(date(2026, 4, 27))

    def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-xml")

    async with _client(malformed) as ent:
        with pytest.raises(ConnectorMalformed):
            await ent.get_day_ahead_prices(date(2026, 4, 27))

    # Final base — anything we throw should be a ConnectorError.
    async with _client(malformed) as ent:
        with pytest.raises(ConnectorError):
            await ent.get_day_ahead_prices(date(2026, 4, 27))


# --- from_env --------------------------------------------------------------


def test_from_env_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
    with pytest.raises(EntsoeAuthError):
        EntsoeClient.from_env()


def test_from_env_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENTSOE_API_TOKEN", "  token-xyz  ")
    client = EntsoeClient.from_env()
    assert client._api_token == "token-xyz"


def test_init_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        EntsoeClient(api_token="")


# --- using outside async-context fails fast --------------------------------


async def test_using_without_context_raises() -> None:
    client = EntsoeClient(api_token=TOKEN, http=None)
    with pytest.raises(RuntimeError):
        await client.get_day_ahead_prices(date(2026, 4, 27))


# --- conversion sanity -----------------------------------------------------


def test_conversion_constants_match_briefing() -> None:
    """Sanity-check the formula constants — guards against accidental edits."""
    assert ENERGY_TAX_EUR_KWH == 0.1108
    assert SUPPLIER_MARKUP_EUR_KWH == 0.025
    assert VAT_RATE == 0.21


def test_realistic_dutch_retail_price_lands_in_expected_range() -> None:
    """
    Sanity: at a typical NL spot price (€80/MWh), the all-in EUR/kWh should
    land near €0.29, i.e. the same ballpark Tibber/Frank/EnergyZero quote.
    Catches accidental drops or doublings of the VAT factor.
    """
    spot_eur_mwh = 80.0
    subtotal = (spot_eur_mwh / 1000.0) + ENERGY_TAX_EUR_KWH + SUPPLIER_MARKUP_EUR_KWH
    all_in = subtotal * (1 + VAT_RATE)
    assert 0.25 < all_in < 0.32, f"all_in price {all_in:.4f} outside expected NL retail band"


