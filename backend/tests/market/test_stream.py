"""Tests for the SSE stream router and event generator."""

import asyncio
import json

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeRequest:
    """Minimal stand-in for a connected Starlette request."""

    client = None

    async def is_disconnected(self) -> bool:
        return False


async def next_frame(gen, timeout: float = 1.0) -> str:
    return await asyncio.wait_for(anext(gen), timeout)


def parse(frame: str) -> dict:
    assert frame.startswith("data: ")
    return json.loads(frame.removeprefix("data: "))


def test_each_router_is_distinct():
    """Repeated factory calls build separate routers with one route each."""
    r1 = create_stream_router(PriceCache())
    r2 = create_stream_router(PriceCache())
    assert r1 is not r2
    assert [r.path for r in r1.routes] == ["/api/stream/prices"]
    assert [r.path for r in r2.routes] == ["/api/stream/prices"]


@pytest.mark.asyncio
class TestGenerateEvents:
    async def test_empty_cache_sends_empty_map(self):
        gen = _generate_events(PriceCache(), FakeRequest(), interval=0.01)
        assert await next_frame(gen) == "retry: 1000\n\n"
        assert parse(await next_frame(gen)) == {}
        await gen.aclose()

    async def test_removing_last_ticker_sends_empty_map(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        gen = _generate_events(cache, FakeRequest(), interval=0.01)
        await next_frame(gen)  # retry directive
        assert set(parse(await next_frame(gen))) == {"AAPL"}

        cache.remove("AAPL")

        assert parse(await next_frame(gen)) == {}
        await gen.aclose()

    async def test_partial_removal_sends_remaining(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.update("MSFT", 410.0)
        gen = _generate_events(cache, FakeRequest(), interval=0.01)
        await next_frame(gen)
        await next_frame(gen)

        cache.remove("AAPL")

        assert set(parse(await next_frame(gen))) == {"MSFT"}
        await gen.aclose()
