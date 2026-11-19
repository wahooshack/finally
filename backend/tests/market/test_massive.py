"""Tests for MassiveDataSource (mocked)."""

import asyncio
import threading
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import (
    EOD_POLL_INTERVAL,
    US_EASTERN,
    MassiveDataSource,
    extract_quote,
    session_close_ts,
)


def _make_snapshot(ticker: str, price: float, timestamp_ns: int) -> TickerSnapshot:
    """Build a real Massive snapshot from API-shaped JSON (lastTrade.t is in nanoseconds).

    Using the package's model instead of a MagicMock ensures tests read the same
    attribute names as the real client.
    """
    return TickerSnapshot.from_dict(
        {"ticker": ticker, "lastTrade": {"p": price, "t": timestamp_ns}}
    )


@pytest.mark.asyncio
class TestMassiveDataSource:
    """Unit tests for MassiveDataSource with mocked API."""

    async def test_poll_updates_cache(self):
        """Test that polling updates the cache."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,  # Long interval so the loop doesn't auto-poll
        )
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50, 1707580800123456789),
            _make_snapshot("GOOGL", 175.25, 1707580800123456789),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        """Test that malformed snapshots are skipped gracefully."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        good_snap = _make_snapshot("AAPL", 190.50, 1707580800123456789)
        bad_snap = TickerSnapshot.from_dict({"ticker": "BAD"})  # no lastTrade

        with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
            await source._poll_once()

        # Good ticker processed, bad one skipped
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_missing_trade_timestamp_skipped(self):
        """A lastTrade without a timestamp is skipped, not written with a bogus time."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        snap = TickerSnapshot.from_dict({"ticker": "AAPL", "lastTrade": {"p": 190.5}})
        with patch.object(source, "_fetch_snapshots", return_value=[snap]):
            await source._poll_once()

        assert cache.get("AAPL") is None

    async def test_api_error_does_not_crash(self):
        """Test that API errors don't crash the poller."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None  # No update happened

    async def test_timestamp_conversion(self):
        """Test that lastTrade sip_timestamp (nanoseconds) is converted to seconds."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800123456789)]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update is not None
        assert update.timestamp == pytest.approx(1707580800.123456789)  # ns -> s

    async def test_add_ticker(self):
        """Test adding a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("AAPL")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_uppercase_normalization(self):
        """Test that tickers are normalized to uppercase."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("aapl")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_strips_whitespace(self):
        """Test that ticker whitespace is stripped."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("  AAPL  ")
        assert "AAPL" in source.get_tickers()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]
        cache.update("AAPL", 190.00)

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]

        tickers = source.get_tickers()
        assert tickers == ["AAPL", "GOOGL"]

    async def test_empty_tickers_skips_poll(self):
        """Test that polling is skipped when there are no tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = []

        # Should not call _fetch_snapshots
        with patch.object(source, "_fetch_snapshots") as mock_fetch:
            await source._poll_once()
            mock_fetch.assert_not_called()

    async def test_stop_is_idempotent(self):
        """Test that stop() can be called multiple times."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.stop()
        await source.stop()  # Should not raise

    async def test_stop_cancels_task(self):
        """Test that stop() cancels the polling task."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=10.0)

        # Mock the client and start
        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])

        # Verify task is running
        assert source._task is not None
        assert not source._task.done()

        # Stop and verify task is cancelled
        await source.stop()
        assert source._task is None

    async def test_start_polls_immediately(self):
        """The polling task polls right away, without waiting a full interval."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800123456789)]

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
                await source.start(["AAPL"])
                for _ in range(100):
                    if "AAPL" in cache:
                        break
                    await asyncio.sleep(0.01)

        assert cache.get_price("AAPL") == 190.50
        await source.stop()

    async def test_stop_during_first_poll(self):
        """stop() during the first fetch cancels it: no cache write, no task left running."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        entered, release = threading.Event(), threading.Event()

        def paused_fetch(tickers):
            entered.set()
            release.wait(5)
            return [_make_snapshot("AAPL", 190.0, 1707580800_000_000_000)]

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", side_effect=paused_fetch):
                await source.start(["AAPL"])
                await asyncio.to_thread(entered.wait, 5)
                task = source._task
                await source.stop()
                release.set()
                await asyncio.sleep(0.05)

        assert task.done()
        assert source._task is None
        assert "AAPL" not in cache


class TestExtractQuote:
    """Price fallback chain for plans that omit lastTrade."""

    def test_last_trade_preferred(self):
        snap = TickerSnapshot.from_dict({
            "ticker": "AAPL",
            "lastTrade": {"p": 190.5, "t": 1707580800_000_000_000},
            "min": {"c": 189.0, "t": 1707580700_000},
        })
        assert extract_quote(snap) == (190.5, pytest.approx(1707580800.0))

    def test_minute_bar_when_no_last_trade(self):
        """Starter plans have no lastTrade; the minute bar (ms timestamp) is used."""
        snap = TickerSnapshot.from_dict({"ticker": "AAPL", "min": {"c": 189.0, "t": 1707580700_000}})
        assert extract_quote(snap) == (189.0, pytest.approx(1707580700.0))

    def test_day_bar_with_updated(self):
        snap = TickerSnapshot.from_dict(
            {"ticker": "AAPL", "day": {"c": 188.0}, "updated": 1707580600_000_000_000}
        )
        assert extract_quote(snap) == (188.0, pytest.approx(1707580600.0))

    def test_prev_day_close_not_used(self):
        snap = TickerSnapshot.from_dict({"ticker": "AAPL", "prevDay": {"c": 187.0}})
        assert extract_quote(snap) is None


class FakeEodClient:
    """Fake RESTClient: snapshots are not authorized; grouped bars exist for one date."""

    def __init__(self, bar_date: str):
        self.bar_date = bar_date
        self.grouped_calls: list[str] = []

    def get_snapshot_all(self, **kwargs):
        raise BadResponse('{"status":"NOT_AUTHORIZED","message":"not entitled"}')

    def get_grouped_daily_aggs(self, day, adjusted=True):
        self.grouped_calls.append(day)
        if day != self.bar_date:
            return []
        return [
            GroupedDailyAgg.from_dict({"T": "AAPL", "c": 188.0, "t": 1707580800_000}),
            GroupedDailyAgg.from_dict({"T": "MSFT", "c": 410.0, "t": 1707580800_000}),
        ]


@pytest.mark.asyncio
class TestMassiveFallbacks:
    """Minute-bar polling and the free-plan end-of-day fallback."""

    async def test_poll_uses_minute_bar_without_last_trade(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL"]
        source._client = MagicMock()
        snap = TickerSnapshot.from_dict({"ticker": "AAPL", "min": {"c": 189.0, "t": 1707580700_000}})

        with patch.object(source, "_fetch_snapshots", return_value=[snap]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 189.0

    async def test_not_authorized_switches_to_eod(self):
        """A 403 on snapshots switches to grouped-daily EOD prices every 30 s."""
        today = datetime.now(US_EASTERN).date()
        session = today - timedelta(days=2)
        bar_date = session.isoformat()
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL"]
        source._client = FakeEodClient(bar_date)

        await source._poll_once()  # snapshot rejected -> EOD mode
        assert source._eod_mode
        assert source._interval == EOD_POLL_INTERVAL == 30.0
        assert cache.get_price("AAPL") is None

        await source._poll_once()  # discovery walks back to the bar date
        assert cache.get_price("AAPL") == 188.0
        assert cache.get("AAPL").timestamp == session_close_ts(session)  # 16:00 ET, not bar t
        assert "MSFT" not in cache  # untracked tickers are filtered out
        assert len(source._client.grouped_calls) == 3

        await source._poll_once()  # date cached: one call per poll
        assert source._client.grouped_calls[3:] == [bar_date]

    async def test_ticker_removed_during_poll_not_recached(self):
        """A ticker removed while the fetch is in flight is not written back to the cache."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "MSFT"]
        source._client = MagicMock()
        entered, release = threading.Event(), threading.Event()
        requested = []

        def paused_fetch(tickers):
            requested.append(list(tickers))
            entered.set()
            release.wait(5)
            return [
                _make_snapshot("AAPL", 190.0, 1707580800_000_000_000),
                _make_snapshot("MSFT", 410.0, 1707580800_000_000_000),
            ]

        with patch.object(source, "_fetch_snapshots", side_effect=paused_fetch):
            poll = asyncio.create_task(source._poll_once())
            await asyncio.to_thread(entered.wait, 5)
            await source.remove_ticker("AAPL")
            release.set()
            await poll

        assert requested == [["AAPL", "MSFT"]]
        assert source.get_tickers() == ["MSFT"]
        assert "AAPL" not in cache
        assert cache.get_price("MSFT") == 410.0


class TestSessionClose:
    """EOD quote time is the 16:00 America/New_York close, across DST."""

    def test_winter_close_is_21_utc(self):
        assert session_close_ts(date(2026, 11, 16)) == datetime(2026, 11, 16, 21, 0, tzinfo=UTC).timestamp()

    def test_summer_close_is_20_utc(self):
        assert session_close_ts(date(2026, 7, 16)) == datetime(2026, 7, 16, 20, 0, tzinfo=UTC).timestamp()
