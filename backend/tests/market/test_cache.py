"""Tests for PriceCache."""

import math
import time

import pytest

from app.market.cache import PriceCache


class TestPriceCache:
    """Unit tests for the PriceCache."""

    def test_update_and_get(self):
        """Test updating and getting a price."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

    def test_first_update_is_flat(self):
        """Test that the first update has flat direction."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up(self):
        """Test price update with upward direction."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_direction_down(self):
        """Test price update with downward direction."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"
        assert update.change == -1.00

    def test_remove(self):
        """Test removing a ticker from cache."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_remove_nonexistent(self):
        """Test removing a ticker that doesn't exist."""
        cache = PriceCache()
        cache.remove("AAPL")  # Should not raise
        assert cache.version == 0

    def test_remove_bumps_version(self):
        """Removal is a change that SSE clients must see."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        version = cache.version
        cache.remove("AAPL")
        assert cache.version == version + 1

    def test_get_all(self):
        """Test getting all prices."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        all_prices = cache.get_all()
        assert set(all_prices.keys()) == {"AAPL", "GOOGL"}

    def test_version_increments(self):
        """Test that version counter increments."""
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2

    def test_get_price_convenience(self):
        """Test the convenience get_price method."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("NOPE") is None

    def test_len(self):
        """Test __len__ method."""
        cache = PriceCache()
        assert len(cache) == 0
        cache.update("AAPL", 190.00)
        assert len(cache) == 1
        cache.update("GOOGL", 175.00)
        assert len(cache) == 2

    def test_contains(self):
        """Test __contains__ method."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert "AAPL" in cache
        assert "GOOGL" not in cache

    def test_custom_timestamp(self):
        """Test updating with a custom timestamp."""
        cache = PriceCache()
        custom_ts = 1234567890.0
        update = cache.update("AAPL", 190.50, timestamp=custom_ts)
        assert update.timestamp == custom_ts

    def test_price_not_rounded(self):
        """Prices are stored unrounded; sub-cent prices survive."""
        cache = PriceCache()
        assert cache.update("AAPL", 190.12345).price == 190.12345
        assert cache.update("PENNY", 0.001).price == 0.001

    @pytest.mark.parametrize("price", [0.0, -1.0, math.nan, math.inf, 2e9])
    def test_invalid_price_rejected(self, price):
        """Invalid prices are rejected without replacing the prior quote or bumping version."""
        cache = PriceCache()
        cache.update("AAPL", 190.0, timestamp=100.0)
        version = cache.version
        assert cache.update("AAPL", price, timestamp=101.0) is None
        assert cache.get_price("AAPL") == 190.0
        assert cache.version == version

    @pytest.mark.parametrize("timestamp", [math.nan, math.inf, time.time() + 60])
    def test_invalid_timestamp_rejected(self, timestamp):
        """Non-finite and far-future timestamps are rejected."""
        cache = PriceCache()
        assert cache.update("AAPL", 190.0, timestamp=timestamp) is None
        assert "AAPL" not in cache

    def test_older_quote_does_not_overwrite_newer(self):
        """A quote older than the cached one is rejected."""
        cache = PriceCache()
        cache.update("AAPL", 190.0, timestamp=200.0)
        assert cache.update("AAPL", 180.0, timestamp=100.0) is None
        assert cache.get_price("AAPL") == 190.0

    def test_same_timestamp_refetch_accepted(self):
        """Re-fetching the same source quote is accepted."""
        cache = PriceCache()
        cache.update("AAPL", 190.0, timestamp=200.0)
        assert cache.update("AAPL", 190.0, timestamp=200.0) is not None

    def test_zero_timestamp_kept(self):
        """An explicit timestamp of 0.0 is not replaced with wall time."""
        cache = PriceCache()
        assert cache.update("AAPL", 190.0, timestamp=0.0).timestamp == 0.0
