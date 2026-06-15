"""Tests voor de EnergyZero-kwartierfeed.

Geen echte netwerkcalls: ``httpx.MockTransport`` levert JSON-payloads die
exact lijken op de echte ``https://public.api.energyzero.nl/public/v1/prices``-
response. We dekken het happy path, DST-overgang (25 uur in herfst), missende
intervallen en 404 (dag-vooruit nog niet gepubliceerd).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from src.connectors.energyzero import (
    EnergyZeroClient,
    EnergyZeroMalformed,
    EnergyZeroUnavailable,
    _floor_to_quarter,
)

# ---------------------------------------------------------------------------
# Helpers — bouw een synthetische EnergyZero-response
# ---------------------------------------------------------------------------


def _quarter_iso(start_utc: datetime) -> str:
    return start_utc.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _build_response(
    *,
    start_local: datetime,
    quarters: int,
    base_eur_kwh: float = 0.05,
    cents_step_per_quarter: float = 0.001,
) -> dict[str, Any]:
    """Genereer een vollere ``base``-stream, vergelijkbaar met de echte API.

    Start_local is interpretatief lokaal Europe/Amsterdam-tijd; we converteren
    handmatig naar UTC door 2 uur (zomer) of 1 uur (winter) eraf te halen via
    de ``offset_hours`` argument om DST scherp te testen.
    """
    items: list[dict[str, Any]] = []
    current = start_local.replace(tzinfo=UTC)
    for i in range(quarters):
        start = current + timedelta(minutes=15 * i)
        end = start + timedelta(minutes=15)
        items.append(
            {
                "start": _quarter_iso(start),
                "end": _quarter_iso(end),
                "price": {"value": base_eur_kwh + cents_step_per_quarter * i},
            }
        )
    return {"base": items}


def _make_client(handler: httpx.MockTransport) -> EnergyZeroClient:
    http = httpx.AsyncClient(transport=handler)
    return EnergyZeroClient(http=http)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_quarter_prices_happy_path() -> None:
    """96 kwartieren in normale dag, oplopende prijzen, base-stream als kale spot."""
    target = date(2026, 6, 15)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        body = _build_response(
            start_local=datetime(2026, 6, 15, 0, 0),
            quarters=96,
        )
        return httpx.Response(200, json=body)

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        prices = await ez.get_quarter_prices(target)

    assert len(prices) == 96
    assert prices[0].spot_eur_kwh == pytest.approx(0.05, abs=1e-6)
    assert prices[1].spot_eur_kwh == pytest.approx(0.051, abs=1e-6)
    # Pad correct opgebouwd?
    assert "energyType=ENERGY_TYPE_ELECTRICITY" in captured["url"]
    assert "date=15-06-2026" in captured["url"]
    assert "interval=INTERVAL_QUARTER" in captured["url"]


async def test_quarter_prices_dst_autumn_returns_100_intervals() -> None:
    """Herfst-DST-wissel (25-10-2026): 25 uur in de dag = 100 kwartieren."""
    target = date(2026, 10, 25)

    def handler(request: httpx.Request) -> httpx.Response:
        # Pseudo-DST: EnergyZero levert echte UTC-tijdstempels; voor de test
        # genereren we gewoon 100 opeenvolgende kwartieren.
        return httpx.Response(200, json=_build_response(
            start_local=datetime(2026, 10, 24, 22, 0),  # 00:00 NL local in DST-wisseldag
            quarters=100,
        ))

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        prices = await ez.get_quarter_prices(target)

    assert len(prices) == 100, "herfst-DST hoort 100 kwartieren te geven (25 uur)"


async def test_quarter_prices_caches_per_day() -> None:
    """Tweede call voor dezelfde dag mag de mock-transport NIET opnieuw raken."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=_build_response(
            start_local=datetime(2026, 6, 15, 0, 0),
            quarters=4,
        ))

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        await ez.get_quarter_prices(date(2026, 6, 15))
        await ez.get_quarter_prices(date(2026, 6, 15))
        await ez.get_quarter_prices(date(2026, 6, 16))

    assert calls["count"] == 2


async def test_quarter_price_for_returns_matching_slot() -> None:
    """quarter_price_for() vindt het juiste kwartier in de cache."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_build_response(
            start_local=datetime(2026, 6, 15, 0, 0),
            quarters=96,
        ))

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        # 12:07 UTC valt in het 12:00-12:15-kwartier.
        when = datetime(2026, 6, 15, 12, 7, tzinfo=UTC)
        price = await ez.quarter_price_for(when)

    assert price is not None
    assert price.start_utc == datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


async def test_quarter_price_for_returns_none_when_slot_missing() -> None:
    """Een interval dat geen prijs-record heeft → None (caller schakelt safe mode)."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Slechts één kwartier in de stream: het kwartier 00:00–00:15.
        return httpx.Response(200, json=_build_response(
            start_local=datetime(2026, 6, 15, 0, 0),
            quarters=1,
        ))

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        when = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        price = await ez.quarter_price_for(when)

    assert price is None


async def test_404_means_day_ahead_not_published_yet() -> None:
    """EnergyZero antwoordt 404 voordat ~14:00 de prijzen voor morgen klaar zijn."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_yet_available"})

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        prices = await ez.get_quarter_prices(date(2099, 1, 1))

    assert prices == []


async def test_5xx_raises_unavailable() -> None:
    """5xx → ConnectorUnavailable zodat de cycle veilig terugvalt op safe mode."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        with pytest.raises(EnergyZeroUnavailable):
            await ez.get_quarter_prices(date(2026, 6, 15))


async def test_malformed_body_raises_malformed() -> None:
    """200 OK zonder ``base``-array → ConnectorMalformed (vendor wijziging?)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        with pytest.raises(EnergyZeroMalformed):
            await ez.get_quarter_prices(date(2026, 6, 15))


async def test_malformed_skips_invalid_items_but_keeps_others() -> None:
    """Een corrupt item in de stream wordt overgeslagen; rest blijft staan."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "base": [
                {"start": "2026-06-15T00:00:00Z", "price": {"value": 0.05}},
                {"start": "not-a-date", "price": {"value": 0.06}},   # invalid
                {"start": "2026-06-15T00:30:00Z", "price": {"value": "not-a-number"}},  # invalid
                {"start": "2026-06-15T00:45:00Z", "price": {"value": 0.07}},
            ],
        }
        return httpx.Response(200, content=json.dumps(body).encode())

    client = _make_client(httpx.MockTransport(handler))
    async with client as ez:
        prices = await ez.get_quarter_prices(date(2026, 6, 15))

    assert len(prices) == 2
    assert prices[0].spot_eur_kwh == pytest.approx(0.05)
    assert prices[1].spot_eur_kwh == pytest.approx(0.07)


def test_floor_to_quarter_handles_all_minute_buckets() -> None:
    base = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    assert _floor_to_quarter(base.replace(minute=0)) == base
    assert _floor_to_quarter(base.replace(minute=7)) == base
    assert _floor_to_quarter(base.replace(minute=14)) == base
    assert _floor_to_quarter(base.replace(minute=15)) == base.replace(minute=15)
    assert _floor_to_quarter(base.replace(minute=59)) == base.replace(minute=45)
