"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

NS_PER_SECOND = 1_000_000_000
MS_PER_SECOND = 1_000
# Grouped-daily mode: 2 calls/min fits the free 5 calls/min, and keeps fetch age
# under the 60 s freshness limit even if one poll fails.
EOD_POLL_INTERVAL = 30.0
EOD_MAX_LOOKBACK_DAYS = 5  # weekends plus a holiday
US_EASTERN = ZoneInfo("America/New_York")  # needs the tzdata package on Windows
SESSION_CLOSE = time(16, 0)


def session_close_ts(day: date) -> float:
    """Unix seconds of the 16:00 ET close on `day`, the source time of its EOD bar.

    A daily bar's own `t` is the start of the day window, which would overstate
    the close's age by 16 hours.
    """
    return datetime.combine(day, SESSION_CLOSE, tzinfo=US_EASTERN).timestamp()


def extract_quote(snap: TickerSnapshot) -> tuple[float, float] | None:
    """Return (price, timestamp_seconds) from the freshest field the plan provides.

    Order: last trade (ns), latest minute bar (ms), today's bar with `updated` (ns).
    The previous day's close is never used as a live price.
    """
    lt = snap.last_trade
    if lt and lt.price and lt.sip_timestamp:
        return lt.price, lt.sip_timestamp / NS_PER_SECOND
    m = snap.min
    if m and m.close and m.timestamp:
        return m.close, m.timestamp / MS_PER_SECOND
    d = snap.day
    if d and d.close and snap.updated:
        return d.close, snap.updated / NS_PER_SECOND
    return None


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    If the plan lacks snapshots (403 NOT_AUTHORIZED, the free Basic plan), switches
    once to end-of-day mode: grouped daily bars, polled every EOD_POLL_INTERVAL.

    Rate limits:
      - Free tier: 5 req/min → EOD mode, 2 calls/min after daily date discovery
      - Paid tiers: snapshot every 15s (default) or faster
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None
        self._eod_mode = False
        self._eod_date: date | None = None  # latest trading date with bars
        self._eod_checked: date | None = None  # US/Eastern day the date was discovered

    async def start(self, tickers: list[str]) -> None:
        # retries=0: the poll loop is the retry; client retries waste free-tier quota
        self._client = RESTClient(api_key=self._api_key, retries=0)
        self._tickers = list(tickers)
        # The first poll runs inside the task, so start() never blocks on the
        # provider and stop() also cancels an in-flight first poll.
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll immediately, then on every interval."""
        while True:
            await self._poll_once()
            await asyncio.sleep(self._interval)

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        tickers = list(self._tickers)  # immutable request set for this poll
        if not tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            if self._eod_mode:
                quotes = await asyncio.to_thread(self._fetch_eod, tickers)
            else:
                quotes = await asyncio.to_thread(self._fetch_snapshot_quotes, tickers)
        except BadResponse as e:
            if "NOT_AUTHORIZED" in str(e) and not self._eod_mode:
                self._enter_eod_mode()
            else:
                logger.error("Massive poll failed: %s", e)
            return
        except Exception as e:
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.
            logger.error("Massive poll failed: %s", e)
            return

        # Tickers removed while the request was in flight must not be re-cached
        current = set(self._tickers)
        processed = 0
        for ticker, (price, timestamp) in quotes.items():
            if ticker in current and self._cache.update(ticker=ticker, price=price, timestamp=timestamp):
                processed += 1
        logger.debug("Massive poll: updated %d/%d tickers", processed, len(tickers))

    def _enter_eod_mode(self) -> None:
        """Switch to grouped-daily EOD prices after the plan rejected snapshots."""
        self._eod_mode = True
        self._interval = max(self._interval, EOD_POLL_INTERVAL)
        logger.warning(
            "Massive plan lacks snapshots; using end-of-day prices every %.0fs", self._interval
        )

    def _fetch_snapshot_quotes(self, tickers: list[str]) -> dict[str, tuple[float, float]]:
        """Fetch one batched snapshot and extract a quote per ticker. Runs in a thread."""
        quotes = {}
        for snap in self._fetch_snapshots(tickers):
            quote = extract_quote(snap)
            if quote:
                quotes[snap.ticker] = quote
            else:
                logger.warning("Skipping snapshot for %s: no usable price", snap.ticker)
        return quotes

    def _fetch_snapshots(self, tickers: list[str]) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )

    def _fetch_eod(self, tickers: list[str]) -> dict[str, tuple[float, float]]:
        """Latest end-of-day close per tracked ticker. Runs in a thread.

        Discovers the latest trading date (walking back over weekends/holidays)
        at most once per US/Eastern day; other polls cost a single call. Each
        close is quoted at its session's 16:00 ET close.
        """
        today = datetime.now(US_EASTERN).date()
        if self._eod_checked == today:
            bars = self._client.get_grouped_daily_aggs(self._eod_date.isoformat(), adjusted=True)
        else:
            found = self._discover_eod(today)
            if found is None:
                return {}
            (self._eod_date, bars), self._eod_checked = found, today
        wanted = set(tickers)
        close_ts = session_close_ts(self._eod_date)
        return {b.ticker: (b.close, close_ts) for b in bars if b.ticker in wanted and b.close}

    def _discover_eod(self, today: date) -> tuple[date, list] | None:
        """Most recent (date, bars), up to EOD_MAX_LOOKBACK_DAYS back, that has grouped bars."""
        for days_back in range(EOD_MAX_LOOKBACK_DAYS):
            day = today - timedelta(days=days_back)
            bars = self._client.get_grouped_daily_aggs(day.isoformat(), adjusted=True)
            if bars:
                return day, bars
        logger.error("Massive: no grouped daily bars in the last %d days", EOD_MAX_LOOKBACK_DAYS)
        return None
