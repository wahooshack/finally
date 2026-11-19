"""Thread-safe in-memory price cache."""

from __future__ import annotations

import math
import time
from threading import Lock

from .models import PriceUpdate

MAX_PRICE = 1e9
MAX_FUTURE_SKEW = 5.0  # seconds a source timestamp may run ahead of the clock


def is_valid_quote(price: float, timestamp: float, now: float) -> bool:
    """True if a price/timestamp pair may enter the cache."""
    return (
        math.isfinite(price)
        and 0 < price <= MAX_PRICE
        and math.isfinite(timestamp)
        and timestamp <= now + MAX_FUTURE_SKEW
    )


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate | None:
        """Record a new price for a ticker. Returns the created PriceUpdate, or None if rejected.

        Rejects non-finite, non-positive or oversized prices, non-finite or future
        timestamps, and quotes older than the cached one; a rejection leaves the cache
        and version unchanged. Prices are stored unrounded. On the first update for a
        ticker, previous_price == price (direction='flat').
        """
        now = time.time()
        ts = now if timestamp is None else timestamp
        if not is_valid_quote(price, ts, now):
            return None
        with self._lock:
            prev = self._prices.get(ticker)
            if prev and ts < prev.timestamp:
                return None
            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=prev.price if prev else price,
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache; bumps the version only if it was present."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
