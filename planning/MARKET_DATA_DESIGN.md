# Market Data Backend — Detailed Design

Implementation spec for everything in `backend/app/market/`: the unified API, the GBM simulator, the Massive (Polygon.io) client, the SSE stream, and the glue the rest of the backend uses. It turns PLAN.md section 6 (and the market parts of sections 7, 8 and 12) into code.

**Status:** Design, partly implemented. The core subsystem exists and its 101 tests pass (checked 2026-11-19 with `uv run --extra dev pytest`). The seven fixes from `MARKET_DATA_REVIEW.md` are in the code:
- quote validation and unrounded prices
- the §10.1 price fallback chain and EOD mode
- ignoring tickers removed during a poll
- non-blocking Massive `start()`
- a router per `create_stream_router()` call
- publishing removals to the stream
- duplicate simulator adds as a no-op

Everything else here is still to do, including the clock, freshness, `quoted_at`/`fetched_at`, the baseline, feed status, the service and the stream rewrite. EOD mode matches §10.2, including the 30 s interval and the 16:00 ET `quoted_at`. See `MARKET_DATA_SUMMARY.md` for the current state.

**Related docs**

| Doc | Role |
|---|---|
| `PLAN.md` §6 | The requirements. Where this doc and PLAN disagree, PLAN wins. |
| `MARKET_INTERFACE.md` | Short version of the interface. This doc has the full code. |
| `MARKET_SIMULATOR.md` | Simulator math and calibration. |
| `MASSIVE_API.md` | Massive REST reference: endpoints, fields, plans, errors. |
| `archive/MARKET_DATA_DESIGN.md` | The original design that the current code follows. Historical only. |

Where this doc chooses differently from `MARKET_INTERFACE.md`, the change and the reason are listed in §16.

---

## Contents

1. Requirements map
2. Architecture and module layout
3. Clock and time formatting (`clock.py`)
4. Quote model (`models.py`)
5. Validity and freshness (`freshness.py`)
6. Price cache (`cache.py`)
7. Quote serialization (`quotes.py`)
8. Source interface (`interface.py`)
9. GBM simulator (`simulator.py`)
10. Massive client (`massive_client.py`)
11. Scripted test source (`scripted.py`)
12. Factory (`factory.py`)
13. Market service: reconciliation, feed status, execution quotes (`service.py`)
14. SSE stream (`stream.py`)
15. Wiring into the app
16. Decisions and differences from earlier docs
17. Edge cases
18. Tests
19. Implementation order
20. Open questions

---

## 1. Requirements map

Each PLAN §6 rule, and where it is implemented.

| PLAN rule | Implemented in |
|---|---|
| Simulator by default; Massive when `MASSIVE_API_KEY` is non-empty | `factory.py` §12 |
| Common `start / stop / add_ticker / remove_ticker / get_tickers` | `interface.py` §8 |
| Do not round stored prices | `models.py`, `cache.py`, `simulator.py` |
| `quoted_at` (source) and `fetched_at` (receipt); `timestamp` as an alias | `models.py` §4 |
| Session baseline, set on the first valid price and kept through `remove()` | `cache.py` §6 |
| `session_id` per backend lifetime; `market_mode` | `cache.py`, `interface.py` |
| Sources report failures, giving `feed_status` = `starting`/`healthy`/`degraded` | sources §9–10, `service.py` §13 |
| One injectable clock | `clock.py` §3, held by `PriceCache` |
| Freshness: price finite and > 0, fetch age ≤ 60 s, source age ≤ 24 h, future time ≤ +5 s | `freshness.py` §5 |
| Failures, missing symbols, and invalid prices do not advance `fetched_at` | `cache.py` §6, `massive_client.py` §10 |
| Router built inside `create_stream_router()` | `stream.py` §14 |
| `feed_status` event first and on every change; status recomputed every 1 s; 15 s heartbeat | `stream.py` §14 |
| SSE entries carry the REST quote fields plus the numeric `timestamp` | `quotes.py` §7 |
| Tracked = watchlist ∪ positions; reconcile at startup, after mutations, every 5 s | `service.py` §13 |
| Never block a trade on the provider; capture one immutable quote | `service.py` `capture_quote` |
| Deterministic market source for E2E, absent from the production app | `scripted.py` §11, §15.4 |

---

## 2. Architecture and module layout

```
                       create_market_data_source(cache)     reads MASSIVE_API_KEY
                                    │
         ┌──────────────────────────┼───────────────────────────┐
         │                          │                           │
 SimulatorDataSource        MassiveDataSource           ScriptedDataSource
 GBM, 500 ms ticks          REST poll 15 s (EOD 30 s)   tests only, never chosen by the factory
         │                          │                           │
         └──────── cache.update(ticker, price, quoted_at) ──────┘   producers write
                                    │
                               PriceCache
             clock · session_id · latest quotes · baselines · version
                                    │
                               MarketData  (service.py)
       feed_status · reconcile loop · capture_quote · snapshot · quote_fields
                                    │
   ┌──────────────┬─────────────────┼──────────────────┬─────────────────┐
 SSE stream    /api/watchlist   /api/portfolio     trade execution   snapshot task
                                                                        consumers read
```

Rules:

1. **Sources push; consumers read the cache.** Nothing asks a source for a price. A source never touches the database.
2. **One clock.** `PriceCache` owns it. Sources stamp time through `cache.now()`, and every freshness check uses it, so a test controls quote age by injecting one function.
3. **One freshness predicate.** `quote_status()` is the only code that decides `fresh`/`stale`/`unavailable`. REST, SSE, snapshots and trades all call it.
4. **One serializer.** `quote_fields()` builds the PLAN §8 quote object for both REST and SSE.
5. **The database decides what is tracked.** `MarketData` reads the committed watchlist ∪ positions from a callable the DB layer provides. The market package contains no SQL.
6. **No provider I/O on a request path.** `add_ticker` and `remove_ticker` only change in-memory sets. Network calls happen only in background loops.

### Module layout

| File | Contents | Status |
|---|---|---|
| `clock.py` | `Clock`, `system_clock`, `ManualClock`, `iso_ms` | **New** |
| `models.py` | `PriceUpdate` | Changed: `quoted_at`/`fetched_at`, no rounding, `to_dict` removed |
| `freshness.py` | Limits, `is_valid_quote`, `quote_status` | **New** |
| `cache.py` | `PriceCache`, `CacheSnapshot` | Changed: clock, session, baselines, validation, snapshot |
| `quotes.py` | `quote_fields`, `sse_entry` | **New** |
| `interface.py` | `MarketDataSource`, `FeedStatus`, `MarketMode` | Changed: `market_mode`, `feed_status` |
| `seed_prices.py` | Simulator constants | Unchanged |
| `simulator.py` | `GBMSimulator`, `SimulatorDataSource` | Changed: RNG injection, no rounding, clock, status |
| `massive_client.py` | `MassiveDataSource`, `extract_quote`, `find_eod_session`, `eod_quotes` | Changed: timestamp bug fix, fallback chain, EOD mode, status |
| `scripted.py` | `ScriptedDataSource` | **New** (test support) |
| `factory.py` | `create_market_data_source` | Minor change |
| `service.py` | `MarketData`, `ExecutionQuote`, `QuoteRejected`, `normalize_ticker` | **New** |
| `stream.py` | `create_stream_router`, `price_events` | Rewritten |
| `__init__.py` | Public exports | Updated |

New runtime dependency: `tzdata` (for `zoneinfo` in the slim Docker image; used by Massive EOD mode). Add it with `uv add tzdata`.

---

## 3. Clock and time formatting (`clock.py`)

```python
"""Injectable wall clock and RFC 3339 formatting shared by the market subsystem."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], float]
"""Returns the current time as Unix seconds (float)."""


def system_clock() -> float:
    """Production clock."""
    return time.time()


class ManualClock:
    """Test clock that only moves when told to."""

    def __init__(self, start: float = 1_763_305_200.0) -> None:  # 2025-11-16T15:00:00Z
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def set(self, t: float) -> None:
        self.t = float(t)


def iso_ms(ts: float | None) -> str | None:
    """Unix seconds -> '2026-11-16T15:00:00.000Z' (UTC, milliseconds, Z suffix)."""
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, tz=UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
```

`iso_ms` lives here because every REST timestamp in the app uses the same format. Other packages (DB, API) import it from here or from a shared `app/timeutil.py` that re-exports it.

---

## 4. Quote model (`models.py`)

```python
"""Immutable quote snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Latest quote for one ticker. Prices are stored unrounded.

    Only PriceCache creates these, so the price is always finite and > 0 and
    previous_price is never zero.
    """

    ticker: str
    price: float
    previous_price: float
    quoted_at: float  # source quote time, Unix seconds
    fetched_at: float  # backend receipt time (cache clock), Unix seconds

    @property
    def timestamp(self) -> float:
        """Legacy alias for quoted_at (numeric `timestamp` in SSE)."""
        return self.quoted_at

    @property
    def change(self) -> float:
        """Previous-tick change in price."""
        return self.price - self.previous_price

    @property
    def change_percent(self) -> float:
        """Previous-tick change in percentage points (5 means 5%). Used for flashes."""
        if self.previous_price <= 0:
            return 0.0
        return (self.price / self.previous_price - 1.0) * 100.0

    @property
    def direction(self) -> Direction:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"
```

Changes from the current code:

- `timestamp` is no longer a field. It is a read-only alias of `quoted_at`.
- `change` and `change_percent` are no longer rounded. Rounding is a display concern (frontend, LLM context, chat summaries).
- `to_dict()` is removed. Serialization needs the baseline, the clock and the session, which the model does not have. It lives in `quotes.py`.

---

## 5. Validity and freshness (`freshness.py`)

*Validity* decides whether a raw quote may enter the cache. *Freshness* classifies a cached quote at a given time. A cached quote is always valid, so freshness only looks at age.

```python
"""Quote validity and freshness rules. The single predicate used by REST, SSE,
snapshots and trade execution."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .models import PriceUpdate

QuoteStatus = Literal["fresh", "stale", "unavailable"]

MAX_FETCH_AGE = 60.0  # seconds since backend receipt (fetched_at)
MAX_SOURCE_AGE = 24 * 3600.0  # seconds since source quote time (quoted_at)
MAX_FUTURE_SKEW = 5.0  # quoted_at may be at most this far ahead of the clock
MAX_PRICE = 1e9  # matches the trade price bound in PLAN §8


def _is_real(x: object) -> bool:
    """A finite int/float that is not a bool."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def is_valid_quote(price: object, quoted_at: object, now: float) -> bool:
    """True if a raw provider quote may enter the cache."""
    return (
        _is_real(price)
        and 0.0 < price <= MAX_PRICE  # type: ignore[operator]
        and _is_real(quoted_at)
        and 0.0 < quoted_at <= now + MAX_FUTURE_SKEW  # type: ignore[operator]
    )


def quote_status(update: PriceUpdate | None, now: float) -> QuoteStatus:
    """Classify a cached quote at time `now`. Boundaries are inclusive: exactly
    60 s of fetch age or exactly 24 h of source age is still fresh."""
    if update is None:
        return "unavailable"
    if now - update.fetched_at > MAX_FETCH_AGE:
        return "stale"
    if now - update.quoted_at > MAX_SOURCE_AGE:
        return "stale"
    return "fresh"
```

Examples (clock at `T`):

| Quote | Status |
|---|---|
| none | `unavailable` |
| `fetched_at = T-60`, `quoted_at = T-60` | `fresh` |
| `fetched_at = T-60.001` | `stale` (fetch age) |
| `fetched_at = T-1`, `quoted_at = T-86_401` (old EOD close fetched just now) | `stale` (source age) |
| `fetched_at = T-1`, `quoted_at = T-900` (15-min delayed plan) | `fresh` (source age exposed via `quoted_at`) |
| raw `quoted_at = T+6` | invalid, never cached |
| raw price `0`, `-1`, `nan`, `inf`, `True`, `"190"`, `2e9` | invalid, never cached |

---

## 6. Price cache (`cache.py`)

```python
"""Thread-safe latest-quote store for one backend session."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import Lock
from types import MappingProxyType

from .clock import Clock, system_clock
from .freshness import QuoteStatus, is_valid_quote, quote_status
from .models import PriceUpdate


@dataclass(frozen=True, slots=True)
class CacheSnapshot:
    """A consistent, immutable view of the cache at one instant.

    Use one snapshot per SSE frame, per REST response, and per portfolio
    snapshot so that every holding is valued against the same quote set.
    """

    now: float
    version: int
    session_id: str
    quotes: Mapping[str, PriceUpdate]
    baselines: Mapping[str, float]

    def quote(self, ticker: str) -> PriceUpdate | None:
        return self.quotes.get(ticker)

    def baseline(self, ticker: str) -> float | None:
        return self.baselines.get(ticker)

    def status(self, ticker: str) -> QuoteStatus:
        return quote_status(self.quotes.get(ticker), self.now)


class PriceCache:
    """Latest quote per ticker plus the session baseline.

    Writers: exactly one MarketDataSource (simulator, Massive or scripted).
    Readers: SSE, REST, portfolio snapshots, trade execution.
    All writes happen on the event loop thread. The lock also makes reads from
    worker threads safe.
    """

    def __init__(self, clock: Clock = system_clock, session_id: str | None = None) -> None:
        self._clock = clock
        self.session_id = session_id or str(uuid.uuid4())
        self._prices: dict[str, PriceUpdate] = {}
        self._baselines: dict[str, float] = {}  # never cleared during the process
        self._version = 0
        self._lock = Lock()

    # --- time -------------------------------------------------------------

    def now(self) -> float:
        """The single clock for stamping and freshness."""
        return self._clock()

    # --- writes -----------------------------------------------------------

    def update(self, ticker: str, price: float, quoted_at: float) -> PriceUpdate | None:
        """Record a provider quote and stamp fetched_at from the clock.

        Returns:
          * the new PriceUpdate when stored;
          * the existing PriceUpdate, unchanged, when the quote is older than the
            one already held (an out-of-order response; fetched_at does not move);
          * None when the quote is invalid (nothing changes).
        """
        now = self._clock()
        if not is_valid_quote(price, quoted_at, now):
            return None
        price, quoted_at = float(price), float(quoted_at)

        with self._lock:
            prev = self._prices.get(ticker)
            if prev is not None and quoted_at < prev.quoted_at:
                return prev
            if prev is not None and quoted_at == prev.quoted_at and price == prev.price:
                # Same source quote fetched again: refresh receipt time only, so the
                # previous-tick metrics (direction/flash) are not reset to flat.
                update = replace(prev, fetched_at=now)
            else:
                update = PriceUpdate(
                    ticker=ticker,
                    price=price,
                    previous_price=prev.price if prev is not None else price,
                    quoted_at=quoted_at,
                    fetched_at=now,
                )
            self._prices[ticker] = update
            self._baselines.setdefault(ticker, price)
            self._version += 1
            return update

    def remove(self, ticker: str) -> None:
        """Drop the latest quote. The session baseline is kept on purpose."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    # --- reads ------------------------------------------------------------

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def baseline(self, ticker: str) -> float | None:
        with self._lock:
            return self._baselines.get(ticker)

    def status(self, ticker: str) -> QuoteStatus:
        return quote_status(self.get(ticker), self._clock())

    def snapshot(self) -> CacheSnapshot:
        now = self._clock()
        with self._lock:
            return CacheSnapshot(
                now=now,
                version=self._version,
                session_id=self.session_id,
                quotes=MappingProxyType(dict(self._prices)),
                baselines=MappingProxyType(dict(self._baselines)),
            )

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: object) -> bool:
        with self._lock:
            return ticker in self._prices
```

Behavior the tests pin down:

| Case | Result |
|---|---|
| First valid quote | `previous_price == price`, `direction == "flat"`, baseline set to `price` |
| Second quote | `previous_price` = old price; baseline unchanged |
| Invalid quote (any row in the §5 table) | returns `None`; price, `fetched_at`, baseline and version all unchanged |
| Same `(price, quoted_at)` again, clock moved on | `fetched_at` advances; `previous_price` and `direction` kept; version bumps |
| Older `quoted_at` than held | returns the held update; nothing changes |
| `remove()` then a new quote | new `previous_price == price` (flat), **baseline still the original** |
| New `PriceCache()` (backend restart) | new `session_id`, no baselines |
| `update("AAPL", 190.123456, …)` | stored exactly as `190.123456` |

Version bumps on removal, so the SSE loop pushes a frame without the removed ticker.

---

## 7. Quote serialization (`quotes.py`)

One function builds the shared quote object (PLAN §8), so REST and SSE cannot drift.

```python
"""Shared REST/SSE quote fields (PLAN §8)."""

from __future__ import annotations

from typing import Any

from .cache import CacheSnapshot
from .clock import iso_ms


def quote_fields(snap: CacheSnapshot, ticker: str) -> dict[str, Any]:
    """The 11 shared quote fields. Values are null before the first quote; a stale
    quote keeps its last values and `status` says it is stale."""
    q = snap.quote(ticker)
    base = snap.baseline(ticker)
    return {
        "price": q.price if q else None,
        "quoted_at": iso_ms(q.quoted_at) if q else None,
        "fetched_at": iso_ms(q.fetched_at) if q else None,
        "status": snap.status(ticker),
        "session_id": snap.session_id,
        "session_baseline": base,
        "session_change_percent": (q.price / base - 1.0) * 100.0 if q and base else None,
        "previous_price": q.previous_price if q else None,
        "change": q.change if q else None,
        "change_percent": q.change_percent if q else None,
        "direction": q.direction if q else None,
    }


def sse_entry(snap: CacheSnapshot, ticker: str) -> dict[str, Any]:
    """SSE entry = ticker + shared fields + legacy numeric `timestamp` (= quoted_at)."""
    q = snap.quote(ticker)
    return {
        "ticker": ticker,
        **quote_fields(snap, ticker),
        "timestamp": q.quoted_at if q else None,
    }
```

Notes:

- `session_baseline` can be non-null while `price` is null: a ticker that was removed and re-added and has not been quoted again yet. That is correct: the baseline survives removal.
- Every value is a finite float or null (the cache only holds finite positive prices), so `json.dumps(..., allow_nan=False)` never fails. Keep `allow_nan=False` anyway as a guard.

SSE entry example:

```json
{
  "ticker": "AAPL",
  "price": 190.51837, "quoted_at": "2026-11-16T15:00:00.500Z", "fetched_at": "2026-11-16T15:00:00.500Z",
  "status": "fresh", "session_id": "a0d05845-7140-44dd-93de-c183fb726d81",
  "session_baseline": 190.0, "session_change_percent": 0.27282,
  "previous_price": 190.50411, "change": 0.01426, "change_percent": 0.007486, "direction": "up",
  "timestamp": 1763305200.5
}
```

---

## 8. Source interface (`interface.py`)

```python
"""Contract for market data producers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

FeedStatus = Literal["starting", "healthy", "degraded"]
MarketMode = Literal["simulator", "massive"]


class MarketDataSource(ABC):
    """Produces quotes into a PriceCache on its own schedule.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "MSFT"])   # once
        await source.add_ticker("TSLA")         # in-memory, never blocks on I/O
        await source.remove_ticker("MSFT")      # drops cached quote, keeps baseline
        await source.stop()                     # idempotent

    Tickers passed in are already normalized (uppercase, ^[A-Z.]{1,10}$).
    """

    market_mode: ClassVar[MarketMode]

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing quotes for `tickers`. Must not wait on the network.
        Calling start() twice raises RuntimeError."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background work. Safe to call more than once, or before start()."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Track a ticker. No-op if already tracked. Allowed before start()."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and call cache.remove(ticker). No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""

    @property
    @abstractmethod
    def feed_status(self) -> FeedStatus:
        """Provider condition, independent of any browser's SSE connection."""
```

Feed status per source:

| Source | `starting` | `healthy` | `degraded` |
|---|---|---|---|
| Simulator | before `start()` | after `start()` while steps succeed | the last `step()` raised |
| Massive | from `start()` until the first poll completes | the last poll call succeeded | the last poll call failed (network, timeout, 401, 403 after the EOD switch, 429, 5xx) |
| Scripted | before `start()` | default after `start()` | when a test sets it |

`MarketData` (§13) also reports `degraded` when the last reconciliation pass failed.

---

## 9. GBM simulator (`simulator.py`)

The math, parameters and calibration are explained in `MARKET_SIMULATOR.md`. Summary: correlated Geometric Brownian Motion with `dt` = 500 ms of trading time, sector correlations via a Cholesky factor, and a 0.1% per-tick chance of a 2–5% shock.

Changes from the current code:

1. `step()` returns unrounded prices.
2. All randomness comes from one injected `np.random.Generator`, so tests are reproducible.
3. Quotes are stamped with the cache clock: `quoted_at = fetched_at = tick time`.
4. `GBMSimulator` exists from construction, so `add_ticker` works before `start()`, and a batch add rebuilds the Cholesky factor once.
5. Reports `feed_status`.

```python
"""Correlated GBM market simulator (default market data source)."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Iterable

import numpy as np

from .cache import PriceCache
from .interface import FeedStatus, MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Pure, synchronous price engine. No asyncio, no cache, no clock.

    S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
    """

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: Iterable[str] = (),
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
        rng: np.random.Generator | None = None,
    ) -> None:
        self._dt = dt
        self._sqrt_dt = math.sqrt(dt)
        self._event_prob = event_probability
        self._rng = rng if rng is not None else np.random.default_rng()
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        self.add_tickers(tickers)

    # --- public -------------------------------------------------------------

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: unrounded price}."""
        n = len(self._tickers)
        if n == 0:
            return {}
        z = self._rng.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        out: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]
            price = self._prices[ticker] * math.exp(
                (mu - 0.5 * sigma * sigma) * self._dt + sigma * self._sqrt_dt * float(z[i])
            )
            if self._rng.random() < self._event_prob:
                magnitude = float(self._rng.uniform(0.02, 0.05))
                sign = 1.0 if self._rng.random() < 0.5 else -1.0
                price *= 1.0 + sign * magnitude
                logger.debug("Shock on %s: %+.1f%%", ticker, sign * magnitude * 100)
            self._prices[ticker] = price
            out[ticker] = price
        return out

    def add_tickers(self, tickers: Iterable[str]) -> None:
        """Add several tickers and rebuild the Cholesky factor once."""
        added = False
        for ticker in tickers:
            if ticker in self._prices:
                continue
            self._tickers.append(ticker)
            seed = SEED_PRICES.get(ticker)
            self._prices[ticker] = (
                float(seed) if seed is not None else float(self._rng.uniform(50.0, 300.0))
            )
            self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))
            added = True
        if added:
            self._rebuild_cholesky()

    def add_ticker(self, ticker: str) -> None:
        self.add_tickers([ticker])

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def current_prices(self) -> dict[str, float]:
        return dict(self._prices)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- internals ----------------------------------------------------------

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = corr[j, i] = rho
        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        tech, finance = CORRELATION_GROUPS["tech"], CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """Async adapter: step the simulator every `update_interval` and write the cache."""

    market_mode = "simulator"

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
        rng: np.random.Generator | None = None,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._sim = GBMSimulator(event_probability=event_probability, rng=rng)
        self._task: asyncio.Task[None] | None = None
        self._status: FeedStatus = "starting"

    @property
    def feed_status(self) -> FeedStatus:
        return self._status

    async def start(self, tickers: list[str]) -> None:
        if self._task is not None:
            raise RuntimeError("SimulatorDataSource already started")
        self._sim.add_tickers(tickers)
        self._publish(self._sim.current_prices())  # quotes available immediately
        self._status = "healthy"
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def add_ticker(self, ticker: str) -> None:
        if ticker in self._sim.get_tickers():
            return
        self._sim.add_ticker(ticker)
        if self._task is not None:  # started: quote it now, don't wait for the next tick
            price = self._sim.get_price(ticker)
            if price is not None:
                self._publish({ticker: price})
        logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)  # baseline kept by the cache
        logger.info("Simulator: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers()

    # --- internals ----------------------------------------------------------

    def _publish(self, prices: dict[str, float]) -> None:
        now = self._cache.now()  # tick time: quoted_at == fetched_at
        for ticker, price in prices.items():
            self._cache.update(ticker, price, quoted_at=now)

    async def _run_loop(self) -> None:
        while True:
            try:
                self._publish(self._sim.step())
                self._status = "healthy"
            except Exception:
                logger.exception("Simulator step failed")
                self._status = "degraded"
            await asyncio.sleep(self._interval)
```

Behavior notes:

- **Adding a ticker gives an immediate quote.** With the simulator, "add to the watchlist, then wait for a quote" takes milliseconds (one reconcile pass, §13).
- **Remove and re-add** restarts the ticker from its seed price (or a new random price for an unknown symbol). `session_change_percent` still measures against the original baseline. The simulator does not promise continuity across removal.
- **Restart:** a new process means a new `PriceCache` (new `session_id`) and a new simulator from seed prices. Cash, holdings, trades and history persist. The UI explains the reset (PLAN §6, Simulator Restarts).
- **Tiny moves can make a tick `flat`.** Not a concern: `direction` is for flashes only.

Example:

```python
import numpy as np
from app.market.simulator import GBMSimulator

sim = GBMSimulator(["AAPL", "JPM", "ZZZZ"], rng=np.random.default_rng(42))
sim.get_price("AAPL")   # 190.0  (seed)
sim.get_price("ZZZZ")   # e.g. 243.9 (rng-dependent, in [50, 300])
sim.step()              # {'AAPL': 190.0071..., 'JPM': 194.9962..., 'ZZZZ': 243.91...}
```

---

## 10. Massive client (`massive_client.py`)

API reference: `MASSIVE_API.md`. Facts checked against the installed `massive` 2.2.0 package on 2026-11-19:

- `RESTClient(api_key, connect_timeout, read_timeout, retries, ...)` is **synchronous** (urllib3).
- `get_snapshot_all(market_type, tickers=None, ...) -> list[TickerSnapshot]`. **With `tickers` empty or None, the call returns every US stock** (~10k), so never call it with an empty list.
- `LastTrade` has `price` and `sip_timestamp` (nanoseconds). There is **no** `timestamp` attribute. The code used to read `snap.last_trade.timestamp`, which failed on every snapshot so no price was ever written. **Fixed 2026-11-19** (reads `sip_timestamp / 1e9`). The §10.1 fallback chain (`extract_quote`) and the §10.2 EOD mode are implemented too.
- `MinuteSnapshot.close` / `.timestamp` (milliseconds); `Agg.close` for `day`; `TickerSnapshot.updated` (nanoseconds).
- `get_grouped_daily_aggs(date, adjusted=...) -> list[GroupedDailyAgg]` with `ticker`, `close`, `timestamp` (ms).
- Non-200 responses raise `massive.exceptions.BadResponse(body_text)`. The status code is not an attribute, so a plan restriction is detected by `"NOT_AUTHORIZED"` in `str(exc)`.

### 10.1 Price extraction

Use the freshest field the plan provides, in this order:

| Order | Field | Price | `quoted_at` | Plans |
|---|---|---|---|---|
| 1 | `lastTrade` | `last_trade.price` | `sip_timestamp / 1e9` | Developer and above |
| 2 | `min` (latest minute bar) | `min.close` | `min.timestamp / 1e3` | Starter and above |
| 3 | `day` | `day.close` | `updated / 1e9` | Starter and above |
| never | `prevDay` | — | — | yesterday's close is not a live price |

### 10.2 EOD mode (free Basic keys)

Basic keys get a 403 `NOT_AUTHORIZED` on snapshots. On the first such response, the source switches permanently (for that process) to **EOD mode**, polling `get_grouped_daily_aggs`.

- **Discover the session date once per day, not on every poll.** Walk back from today (US/Eastern) over weekends and holidays to the most recent date with bars, then cache that date. Later polls that day cost **one call** for the cached date. Walking back on every poll would cost up to 3 calls per poll over a weekend, which breaks the 5 calls/min budget (found in `MARKET_DATA_REVIEW.md`). Discovery reuses the bars it found, so it costs no extra call. If discovery finds nothing, the date is not cached and the next poll tries again.
- `quoted_at` for an EOD bar is that session's **16:00 America/New_York close**. The bar's own `t` is the start of the day window, which would overstate its age by 16 hours.
- The EOD poll interval is 30 s. That is 2 calls/min (within the 5/min limit) and keeps `fetched_at` under the 60 s limit even if one poll fails. On a discovery poll, the first-403 probe plus a walk-back can reach about 5 calls in one minute. A resulting 429 is just a failed poll, retried on the next cycle.

**Implemented 2026-11-19:**
- `EOD_POLL_INTERVAL = 30.0`.
- `quoted_at = session_close_ts(day)`, the 16:00 America/New_York close (21:00 UTC in winter, 20:00 UTC in summer).
- "Today" is the US/Eastern date.

`tzdata` was added as a dependency because Windows has no IANA timezone database (`ZoneInfo("America/New_York")` failed without it).
- The PLAN's 24 h source-age limit makes EOD closes stale from 16:00 ET the next day until the next close publishes. Over a weekend, Friday's close is stale. **This is intended** (PLAN §6: "reject old closed-market quotes after 24 hours"). Prices still display, marked stale, and trading is disabled.

### 10.3 Code

```python
"""Massive (Polygon.io) REST poller."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import FeedStatus, MarketDataSource

logger = logging.getLogger(__name__)

NS_PER_S = 1_000_000_000
MS_PER_S = 1_000
US_EASTERN = ZoneInfo("America/New_York")
SESSION_CLOSE = time(16, 0)

Quote = tuple[float, float]  # (price, quoted_at unix seconds)


def extract_quote(snap: TickerSnapshot) -> Quote | None:
    """(price, quoted_at) from a snapshot via the §10.1 fallback chain, or None."""
    lt = snap.last_trade
    if lt is not None and lt.price and lt.sip_timestamp:
        return float(lt.price), lt.sip_timestamp / NS_PER_S
    m = snap.min
    if m is not None and m.close and m.timestamp:
        return float(m.close), m.timestamp / MS_PER_S
    d = snap.day
    if d is not None and d.close and snap.updated:
        return float(d.close), snap.updated / NS_PER_S
    return None


def session_close_ts(day: date) -> float:
    """Unix seconds of the 16:00 ET close on `day`."""
    return datetime.combine(day, SESSION_CLOSE, tzinfo=US_EASTERN).timestamp()


def find_eod_session(client: RESTClient, today: date, max_back: int = 7) -> tuple[date, list] | None:
    """Most recent (date, bars) with grouped bars, walking back over non-trading days.

    Synchronous; call it in a worker thread. Today's bars are tried first, since
    they exist after the close. Call once per day and cache the date (§10.2).
    """
    day = today
    for _ in range(max_back):
        bars = client.get_grouped_daily_aggs(day.isoformat(), adjusted=True)
        if bars:
            return day, bars
        day -= timedelta(days=1)
    return None


def eod_quotes(bars: list, day: date, tickers: set[str]) -> dict[str, Quote]:
    """EOD close per requested ticker, quoted at the 16:00 ET session close."""
    close_ts = session_close_ts(day)
    return {b.ticker: (float(b.close), close_ts) for b in bars if b.ticker in tickers and b.close}


def _is_not_authorized(exc: Exception) -> bool:
    return isinstance(exc, BadResponse) and "NOT_AUTHORIZED" in str(exc)


class MassiveDataSource(MarketDataSource):
    """Polls one batched snapshot per cycle for every tracked ticker.

    Intervals: 15 s by default (safe for any plan); 2-5 s is fine on paid plans.
    EOD mode polls every `eod_poll_interval` seconds.
    """

    market_mode = "massive"

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        eod_poll_interval: float = 30.0,
        client: RESTClient | None = None,
    ) -> None:
        self._cache = price_cache
        self._interval = poll_interval
        self._eod_interval = eod_poll_interval
        # retries=0: the poll loop is the retry; urllib3 retries would burn free-tier
        # quota on 429s and hide failures from feed_status.
        self._client = client or RESTClient(
            api_key=api_key, connect_timeout=5.0, read_timeout=10.0, retries=0
        )
        self._tickers: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._status: FeedStatus = "starting"
        self._eod_mode = False
        self._eod_day: date | None = None  # cached session date (§10.2)
        self._eod_checked: date | None = None  # the day that date was discovered
        self._missing: frozenset[str] = frozenset()

    # --- interface ----------------------------------------------------------

    @property
    def feed_status(self) -> FeedStatus:
        return self._status

    @property
    def eod_mode(self) -> bool:
        return self._eod_mode

    def missing_tickers(self) -> frozenset[str]:
        """Tickers requested in the last successful poll that returned no valid quote."""
        return self._missing

    async def start(self, tickers: list[str]) -> None:
        if self._task is not None:
            raise RuntimeError("MassiveDataSource already started")
        self._tickers.update(tickers)
        # Do not await a poll here: startup must not block on the provider.
        # The loop polls immediately; feed_status stays "starting" until it finishes.
        self._task = asyncio.create_task(self._run_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval", len(tickers), self._interval)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # A fetch already running in a worker thread finishes in the background;
        # its result is discarded because the loop task is gone.

    async def add_ticker(self, ticker: str) -> None:
        self._tickers.add(ticker)  # quoted on the next poll

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return sorted(self._tickers)

    # --- polling ------------------------------------------------------------

    async def _run_loop(self) -> None:
        while True:
            await self._poll_once()
            await asyncio.sleep(self._eod_interval if self._eod_mode else self._interval)

    async def _poll_once(self) -> None:
        """One cycle: fetch, write valid quotes, update feed status. Never raises."""
        requested = frozenset(self._tickers)
        if not requested:
            self._status = "healthy"  # nothing to fetch is not a provider problem
            self._missing = frozenset()
            return

        try:
            quotes = await asyncio.to_thread(self._fetch, requested)
        except Exception as exc:
            if self._eod_mode or not _is_not_authorized(exc):
                self._fail(exc)
                return
            logger.warning("Massive snapshots are not in this plan; switching to end-of-day mode")
            self._eod_mode = True
            try:
                quotes = await asyncio.to_thread(self._fetch, requested)
            except Exception as exc2:
                self._fail(exc2)
                return

        accepted: set[str] = set()
        for ticker, (price, quoted_at) in quotes.items():
            # Skip tickers removed while the fetch was in flight.
            if ticker in requested and ticker in self._tickers:
                if self._cache.update(ticker, price, quoted_at) is not None:
                    accepted.add(ticker)

        self._missing = frozenset(requested & self._tickers) - accepted
        if self._missing:
            logger.info("Massive: no valid quote for %s", ", ".join(sorted(self._missing)))
        self._status = "healthy"

    def _fail(self, exc: Exception) -> None:
        # Cached quotes are left alone; they age into "stale" via the clock.
        logger.error("Massive poll failed: %s", exc)
        self._status = "degraded"

    def _fetch(self, tickers: frozenset[str]) -> dict[str, Quote]:
        """Synchronous provider call; runs in a worker thread."""
        if self._eod_mode:
            today = datetime.fromtimestamp(self._cache.now(), US_EASTERN).date()
            if self._eod_checked == today:  # one call per poll
                bars = self._client.get_grouped_daily_aggs(self._eod_day.isoformat(), adjusted=True)
            else:  # discovery, at most once per day; retried next poll if nothing found
                found = find_eod_session(self._client, today)
                if found is None:
                    return {}
                (self._eod_day, bars), self._eod_checked = found, today
            return eod_quotes(bars, self._eod_day, set(tickers))
        snaps = self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=sorted(tickers))
        out: dict[str, Quote] = {}
        for snap in snaps:
            q = extract_quote(snap) if snap.ticker else None
            if q is not None:
                out[snap.ticker] = q
        return out
```

### 10.4 What happens when

| Event | Cache | `fetched_at` | Feed status |
|---|---|---|---|
| Poll OK, ticker present with a valid quote | updated | advances | `healthy` |
| Poll OK, ticker missing from the response (bad symbol, untraded) | untouched | does not advance | `healthy`; ticker listed in `missing_tickers()`; per-ticker status `unavailable` or eventually `stale` |
| Poll OK, quote invalid (price 0, future time) | untouched | does not advance | `healthy` (as above) |
| Same source quote returned again | `fetched_at` refreshed | advances | `healthy` |
| Network error, timeout, 401, 429, 5xx | untouched | does not advance | `degraded` |
| First 403 `NOT_AUTHORIZED` | switch to EOD, retry once at once | per the EOD result | per the EOD result |
| Provider down for more than 60 s | quotes remain (indicative) | — | `degraded`; every quote turns `stale` |

### 10.5 Example: one poll by hand

```python
import asyncio, os
from app.market import PriceCache
from app.market.massive_client import MassiveDataSource

async def main():
    cache = PriceCache()
    src = MassiveDataSource(os.environ["MASSIVE_API_KEY"], cache)
    await src.add_ticker("AAPL")
    await src.add_ticker("MSFT")
    await src._poll_once()
    print(src.feed_status, src.eod_mode, cache.get("AAPL"))

asyncio.run(main())
# healthy False PriceUpdate(ticker='AAPL', price=190.5, previous_price=190.5,
#                           quoted_at=1763305159.123, fetched_at=1763305160.02)
```

---

## 11. Scripted test source (`scripted.py`)

PLAN §12 needs exact, test-controlled prices for E2E balance assertions. A seeded random simulator is not enough. `ScriptedDataSource` is a third implementation of the interface that only changes prices when a test tells it to. The factory never selects it. Only the test entrypoint (§15.4) constructs it, so the production app has no way to reach it.

```python
"""Deterministic MarketDataSource for tests. Never selected by the factory."""

from __future__ import annotations

from .cache import PriceCache
from .interface import FeedStatus, MarketDataSource, MarketMode


class ScriptedDataSource(MarketDataSource):
    """Prices change only through set_price(). Pair with ManualClock for exact ages.

    quote_on_add: if a ticker has a scripted price when it is added, publish it
    immediately (like the simulator). Set it to False to test "added, waiting for
    first quote".
    """

    market_mode: MarketMode = "simulator"

    def __init__(
        self,
        price_cache: PriceCache,
        prices: dict[str, float] | None = None,
        *,
        mode: MarketMode = "simulator",
        quote_on_add: bool = True,
    ) -> None:
        self._cache = price_cache
        self._script: dict[str, float] = dict(prices or {})
        self._tickers: set[str] = set()
        self._status: FeedStatus = "starting"
        self._started = False
        self.quote_on_add = quote_on_add
        self.market_mode = mode  # instance override, e.g. to test market-mode changes

    @property
    def feed_status(self) -> FeedStatus:
        return self._status

    async def start(self, tickers: list[str]) -> None:
        if self._started:
            raise RuntimeError("already started")
        self._started = True
        self._status = "healthy"
        for t in tickers:
            await self.add_ticker(t)

    async def stop(self) -> None:
        self._started = False

    async def add_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            return
        self._tickers.add(ticker)
        if self._started and self.quote_on_add and ticker in self._script:
            self._cache.update(ticker, self._script[ticker], self._cache.now())

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return sorted(self._tickers)

    # --- test controls ------------------------------------------------------

    def set_price(self, ticker: str, price: float, quoted_at: float | None = None) -> bool:
        """Script a price; publish it now if the ticker is tracked. Returns True if cached."""
        self._script[ticker] = price
        if ticker not in self._tickers:
            return False
        return self._cache.update(ticker, price, quoted_at if quoted_at is not None else self._cache.now()) is not None

    def refetch(self, ticker: str) -> None:
        """Simulate re-receiving the same source quote (advances fetched_at only)."""
        q = self._cache.get(ticker)
        if q is not None and ticker in self._tickers:
            self._cache.update(ticker, q.price, q.quoted_at)

    def set_feed_status(self, status: FeedStatus) -> None:
        """Simulate a provider outage (degraded) or recovery (healthy)."""
        self._status = status
```

Typical test script, used both in pytest and behind the E2E control routes:

```python
clock = ManualClock()
cache = PriceCache(clock=clock)
src = ScriptedDataSource(cache, {"AAPL": 190.5, "MSFT": 420.0})
await src.start(["AAPL"])                  # AAPL fresh at 190.5; MSFT not tracked
clock.advance(61); assert cache.status("AAPL") == "stale"
src.refetch("AAPL"); assert cache.status("AAPL") == "fresh"
src.set_feed_status("degraded")            # provider outage while SSE stays connected
```

---

## 12. Factory (`factory.py`)

```python
"""Select the market data source from the environment."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(
    price_cache: PriceCache, env: Mapping[str, str] | None = None
) -> MarketDataSource:
    """MassiveDataSource if MASSIVE_API_KEY is set and non-blank, else the simulator.
    Returns an unstarted source."""
    env = os.environ if env is None else env
    api_key = env.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        logger.info("Market data source: Massive API")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

The `env` parameter lets tests avoid patching `os.environ`. The E2E environment sets `MASSIVE_API_KEY=` explicitly so that an inherited key cannot switch the source (PLAN §12). The API key is never logged.

---

## 13. Market service (`service.py`)

`MarketData` is the one object the rest of the app holds. It wraps the cache and the source and adds three things neither has on its own: subscription reconciliation against the database, an aggregate feed status, and execution-quote capture for trades.

```python
"""Unified market data API used by routes, the trade service, SSE and snapshots."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from .cache import CacheSnapshot, PriceCache
from .freshness import quote_status
from .interface import FeedStatus, MarketDataSource, MarketMode
from .quotes import quote_fields, sse_entry

logger = logging.getLogger(__name__)

TrackedReader = Callable[[], Awaitable[set[str]]]
"""Reads committed watchlist ∪ open-position tickers from the database."""

TICKER_RE = re.compile(r"^[A-Z.]{1,10}$")


def normalize_ticker(raw: str) -> str:
    """Uppercase and validate (PLAN §8). Raises ValueError; the API maps it to 422."""
    t = raw.strip().upper()
    if not TICKER_RE.fullmatch(t):
        raise ValueError(f"invalid ticker: {raw!r}")
    return t


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    """The one immutable quote a trade fills at."""

    ticker: str
    price: float
    quoted_at: float
    fetched_at: float
    session_id: str


class QuoteRejected(Exception):
    def __init__(self, code: Literal["quote_unavailable", "quote_stale"], detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class MarketData:
    def __init__(
        self,
        cache: PriceCache,
        source: MarketDataSource,
        read_tracked: TrackedReader,
        reconcile_interval: float = 5.0,
    ) -> None:
        self.cache = cache
        self.source = source
        self._read_tracked = read_tracked
        self._interval = reconcile_interval
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._reconcile_error: str | None = None

    # --- identity -----------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self.cache.session_id

    @property
    def market_mode(self) -> MarketMode:
        return self.source.market_mode

    def now(self) -> float:
        return self.cache.now()

    @property
    def feed_status(self) -> FeedStatus:
        status = self.source.feed_status
        if status == "starting":
            return "starting"
        if status == "degraded" or self._reconcile_error is not None:
            return "degraded"
        return "healthy"

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Register every committed watchlist and position ticker, then start the
        reconcile loop. Call after DB init and chat recovery, before serving."""
        tracked = await self._read_tracked()
        await self.source.start(sorted(tracked))
        self._task = asyncio.create_task(self._reconcile_loop(), name="market-reconcile")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.source.stop()

    # --- reconciliation -----------------------------------------------------

    def nudge(self) -> None:
        """Ask for a reconcile pass soon. Call after committing any watchlist or
        position change. Non-blocking, so it is safe on a request path."""
        self._wake.set()

    async def reconcile_now(self) -> None:
        """Converge source tickers to the committed watchlist ∪ positions.
        Never raises. A failure is retried on the next pass and makes feed_status
        degraded until a pass succeeds."""
        async with self._lock:
            try:
                desired = await self._read_tracked()
                current = set(self.source.get_tickers())
                for t in sorted(desired - current):
                    await self.source.add_ticker(t)
                for t in sorted(current - desired):
                    await self.source.remove_ticker(t)
                self._reconcile_error = None
            except Exception as exc:
                logger.exception("Market reconcile failed")
                self._reconcile_error = repr(exc)

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            self._wake.clear()
            await self.reconcile_now()

    def tracked_tickers(self) -> list[str]:
        return self.source.get_tickers()

    # --- reads --------------------------------------------------------------

    def snapshot(self) -> CacheSnapshot:
        return self.cache.snapshot()

    def quote_fields(self, ticker: str, snap: CacheSnapshot | None = None) -> dict[str, Any]:
        return quote_fields(snap or self.cache.snapshot(), ticker)

    def sse_payload(self, snap: CacheSnapshot) -> dict[str, dict[str, Any]]:
        """{ticker: entry} for every tracked ticker, including ones with no quote yet."""
        tickers = set(self.source.get_tickers()) | set(snap.quotes)
        return {t: sse_entry(snap, t) for t in sorted(tickers)}

    def capture_quote(self, ticker: str) -> ExecutionQuote:
        """Capture one immutable quote for a fill. Synchronous with no I/O, so it is
        safe to call inside the BEGIN IMMEDIATE trade transaction (PLAN §6)."""
        q = self.cache.get(ticker)
        status = quote_status(q, self.cache.now())
        if q is None:
            raise QuoteRejected("quote_unavailable", f"no price available for {ticker} yet")
        if status != "fresh":
            raise QuoteRejected("quote_stale", f"price for {ticker} is stale")
        return ExecutionQuote(ticker, q.price, q.quoted_at, q.fetched_at, self.cache.session_id)
```

### 13.1 Why reconciliation reads from the database

The tracked set is always derived from committed state, never adjusted step by step. The routes do not need to know whether a ticker is watched, held or both; they commit and call `nudge()`. That gives:

- **Watchlist remove while held:** the ticker is still in positions, so it is still desired and keeps streaming.
- **Sell to zero while watched:** still on the watchlist, so it stays.
- **Sell to zero while not watched:** absent from both, so it is removed on the next pass. The baseline is kept.
- **Buy of a held-but-unwatched ticker:** the trade adds it to the watchlist in the same transaction. The tracked set does not change.
- **Restart:** `start()` rebuilds the set from the database. A persisted holding is registered before its first quote arrives, and shows `unavailable` until then.
- **Failed subscription work:** retried every 5 s. It never rolls back a committed change.

`read_tracked` is supplied by the DB layer, for example:

```python
async def read_tracked_tickers(self) -> set[str]:
    def q() -> set[str]:
        with self.connect() as con:  # read-only; WAL lets it run alongside writers
            rows = con.execute(
                "SELECT ticker FROM watchlist WHERE user_id = ? "
                "UNION SELECT ticker FROM positions WHERE user_id = ? AND quantity > 0",
                ("default", "default"),
            ).fetchall()
        return {r[0] for r in rows}
    return await asyncio.to_thread(q)
```

### 13.2 Trade flow, market side

```python
# inside the trade service, after request-identity checks
with db.immediate_transaction() as tx:             # BEGIN IMMEDIATE
    tracked_state = tx.ticker_state(ticker)         # watched / held / neither (from DB)
    ...                                             # not_tracked / insufficient_shares checks
    quote = market.capture_quote(ticker)            # raises QuoteRejected -> 400
    fill_price = quote.price                        # the only price used for this fill
    ...                                             # cash/position/trade/watchlist/state_version
    tx.insert_trade(..., price=fill_price,
                    quoted_at=iso_ms(quote.quoted_at), fetched_at=iso_ms(quote.fetched_at))
market.nudge()                                      # after commit, outside the transaction
```

`capture_quote` checks time and freshness once, inside the transaction, with no `await` between capture and commit. The trade does not re-read the cache and never waits on the provider.

### 13.3 Portfolio snapshot, market side

```python
snap = market.snapshot()                      # one quote set for the whole snapshot
for pos in positions:                         # read in the same DB read transaction as cash
    status = snap.status(pos.ticker)          # fresh / stale / unavailable
    ...
# aggregate status: unavailable > stale > fresh; total_value is null unless all fresh
db.insert_snapshot(session_id=market.session_id, market_mode=market.market_mode, ...)
```

The snapshot task takes its clock from `market.now()`, so tests can step it directly (PLAN §12: "invoke one snapshot iteration directly").

---

## 14. SSE stream (`stream.py`)

### 14.1 Wire format

```
retry: 1000

event: feed_status
data: {"feed_status":"healthy","market_mode":"simulator","session_id":"a0d0…","state_version":3,"chat_sequence":12}

data: {"AAPL":{"ticker":"AAPL","price":190.518,…,"timestamp":1763305200.5},"MSFT":{…}}

data: {"AAPL":{…},"MSFT":{…}}

: heartbeat

event: feed_status
data: {"feed_status":"degraded","market_mode":"simulator","session_id":"a0d0…","state_version":4,"chat_sequence":12}
```

- The `feed_status` event comes **first on every connection**, before any quote event, even when nothing is tracked. After that it is sent whenever any of its fields changes.
- Unnamed `data:` events carry the full quote map for every tracked ticker, keyed by ticker. A frame is sent when the cache version changes (a new price, a refetch or a removal), or when the 1 s recompute finds that any ticker's `status` changed, or that the tracked set changed.
- `: heartbeat` (an SSE comment; `EventSource` ignores it) is sent when nothing else has been written for 15 s.

### 14.2 Code

```python
"""SSE endpoint: live quotes plus the feed_status event."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .service import MarketData

logger = logging.getLogger(__name__)

StateReader = Callable[[], Awaitable[Mapping[str, int]]]
"""Returns {"state_version": int, "chat_sequence": int} from committed DB state."""

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _dumps(obj: Any) -> str:
    return json.dumps(obj, allow_nan=False, separators=(",", ":"))


def _event(name: str, data: Any) -> str:
    return f"event: {name}\ndata: {_dumps(data)}\n\n"


def _data(data: Any) -> str:
    return f"data: {_dumps(data)}\n\n"


def create_stream_router(
    market: MarketData,
    read_state: StateReader,
    *,
    tick: float = 0.5,
    status_every: float = 1.0,
    heartbeat_every: float = 15.0,
) -> APIRouter:
    """Build a new router on every call, so repeated calls never duplicate routes."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            price_events(
                market, read_state, request.is_disconnected,
                tick=tick, status_every=status_every, heartbeat_every=heartbeat_every,
            ),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return router


async def price_events(
    market: MarketData,
    read_state: StateReader,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    tick: float = 0.5,
    status_every: float = 1.0,
    heartbeat_every: float = 15.0,
) -> AsyncGenerator[str, None]:
    """Event generator. Timing uses the market clock so tests can drive it with
    ManualClock and tick=0."""
    state: dict[str, int] = {"state_version": 0, "chat_sequence": 0}

    async def refresh_state() -> None:
        try:
            state.update(await read_state())
        except Exception:  # DB busy etc.: keep the last values; retried next second
            logger.warning("SSE state read failed", exc_info=True)

    def feed_payload() -> dict[str, Any]:
        return {
            "feed_status": market.feed_status,
            "market_mode": market.market_mode,
            "session_id": market.session_id,
            **state,
        }

    yield "retry: 1000\n\n"
    await refresh_state()
    feed = feed_payload()
    yield _event("feed_status", feed)  # always first

    last_write = last_check = market.now()
    last_version = -1
    last_statuses: dict[str, str] | None = None

    try:
        while not await is_disconnected():
            snap = market.snapshot()
            now = snap.now
            send_quotes = snap.version != last_version

            if last_statuses is None or now - last_check >= status_every:
                last_check = now
                await refresh_state()
                new_feed = feed_payload()
                if new_feed != feed:
                    feed = new_feed
                    yield _event("feed_status", feed)
                    last_write = now
                tickers = set(market.tracked_tickers()) | set(snap.quotes)
                statuses = {t: snap.status(t) for t in tickers}
                if statuses != last_statuses:
                    send_quotes = True
                    last_statuses = statuses

            if send_quotes:
                last_version = snap.version
                yield _data(market.sse_payload(snap))
                last_write = now
            elif now - last_write >= heartbeat_every:
                yield ": heartbeat\n\n"
                last_write = now

            await asyncio.sleep(tick)
    except asyncio.CancelledError:
        logger.debug("SSE stream cancelled")
        raise
```

Notes:

- **Cost.** Every connection reads `state_version`/`chat_sequence` once a second, which is a cheap indexed read under WAL. A few tabs means a few reads per second. If that ever matters, move the state read to one shared 1 s task in the app and give the stream a getter. The event contract stays the same.
- **Frontend expectations** (PLAN §10): when `session_id` changes, clear caches and refetch everything; when `state_version` is higher, refetch portfolio and watchlist; when `chat_sequence` differs, refetch chat. Quote-age timers on the frontend run off `fetched_at`/`quoted_at`, so they go stale even while SSE is disconnected.
- **Disconnect:** `request.is_disconnected()` is polled every tick; client cancellation also ends the generator through `CancelledError`, which is re-raised.

---

## 15. Wiring into the app

### 15.1 Package exports (`__init__.py`)

```python
from .cache import CacheSnapshot, PriceCache
from .clock import Clock, ManualClock, iso_ms, system_clock
from .factory import create_market_data_source
from .freshness import QuoteStatus, quote_status
from .interface import FeedStatus, MarketDataSource, MarketMode
from .models import PriceUpdate
from .service import ExecutionQuote, MarketData, QuoteRejected, normalize_ticker
from .stream import create_stream_router

__all__ = [...]  # the names above
```

`ScriptedDataSource` is deliberately not exported. Import it from `app.market.scripted`.

### 15.2 App factory and lifespan (`app/main.py`)

Routers are registered when the app is built, not in the lifespan, so that `/api` routes come before the static mount. `MarketData` is built unstarted at construction and started in the lifespan.

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import Database
from app.market import MarketData, PriceCache, create_market_data_source, create_stream_router, system_clock
from app.market.interface import MarketDataSource


def create_app(
    *,
    db_path: Path | None = None,
    clock=system_clock,
    source_factory=create_market_data_source,   # tests inject a ScriptedDataSource
    extra_routers=(),                            # test-control routes, E2E app only
) -> FastAPI:
    db = Database(db_path or default_db_path())
    cache = PriceCache(clock=clock)              # new session_id per process
    source: MarketDataSource = source_factory(cache)
    market = MarketData(cache, source, read_tracked=db.read_tracked_tickers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.init()                          # schema, WAL, seed (idempotent)
        await db.recover_running_chats()        # PLAN §7 startup recovery
        await market.start()                     # register watchlist ∪ positions
        snapshots = start_snapshot_task(db, market)  # first snapshot right away, then every 30 s
        try:
            yield
        finally:
            await stop_task(snapshots)
            await market.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.db, app.state.market = db, market

    app.include_router(create_stream_router(market, db.read_stream_state))
    app.include_router(health_router(market))
    app.include_router(portfolio_router(db, market))
    app.include_router(watchlist_router(db, market))
    app.include_router(chat_router(db, market))
    for r in extra_routers:
        app.include_router(r)

    static = Path(__file__).parent.parent / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="static")
    return app


app = create_app()   # uvicorn app.main:app --workers 1
```

Health route:

```python
@router.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "market_mode": market.market_mode,
        "feed_status": market.feed_status,
        "session_id": market.session_id,
    }
```

### 15.3 Watchlist route, market side

```python
@router.post("/api/watchlist")
async def add(body: WatchlistAdd) -> dict:
    ticker = body.ticker                    # already normalized by the validator
    result = await watchlist_service.add(body.request_id, ticker)   # one transaction
    market.nudge()                          # subscribe; never awaited inside the transaction
    return result                           # quote shows "unavailable" until one arrives

@router.get("/api/watchlist")
async def list_watchlist() -> dict:
    rows, state_version = await db.read_watchlist()
    snap = market.snapshot()
    return {
        "state_version": state_version,
        "feed_status": market.feed_status,
        "items": [
            {"ticker": r.ticker, "added_at": r.added_at, **market.quote_fields(r.ticker, snap)}
            for r in rows
        ],
    }
```

### 15.4 E2E test entrypoint (never in the production app)

`backend/tests/e2e_app.py` builds the app with a `ManualClock`, a `ScriptedDataSource` and a `/api/__test__` router (set price, advance clock, refetch, set feed status, reset DB). The E2E container runs `uvicorn tests.e2e_app:app --workers 1` with `LLM_MOCK=true` and `MASSIVE_API_KEY=`. Production runs `app.main:app`, which never imports `tests/` or `scripted.py`, so the controls do not exist there.

```python
# backend/tests/e2e_app.py
clock = ManualClock(start=time.time())
holder: dict[str, ScriptedDataSource] = {}

def scripted_factory(cache):
    holder["src"] = ScriptedDataSource(cache, dict(SEED_PRICES))
    return holder["src"]

control = APIRouter(prefix="/api/__test__")

@control.post("/price")
async def set_price(ticker: str, price: float, age: float = 0.0):
    return {"cached": holder["src"].set_price(ticker, price, clock() - age)}

@control.post("/clock/advance")
async def advance(seconds: float):
    clock.advance(seconds)
    return {"now": clock()}

@control.post("/feed")
async def feed(status: str):
    holder["src"].set_feed_status(status)
    return {"feed_status": status}

app = create_app(clock=clock, source_factory=scripted_factory, extra_routers=[control])
```

Because the clock is manual, wall time does not age quotes in E2E. Tests advance it explicitly to trigger staleness and the snapshot cadence.

---

## 16. Decisions and differences from earlier docs

| Topic | Decision | Why |
|---|---|---|
| Massive `start()` | Does not await the first poll; feed stays `starting` until it completes | Startup and requests never block on the provider (PLAN §6). Also lets `stop()` cancel an in-flight first poll. Implemented 2026-11-19. |
| Missing symbol in a successful Massive response | Feed stays `healthy`; the ticker is `unavailable`/`stale` and listed in `missing_tickers()` | A single mistyped watchlist symbol should not show the whole feed as degraded. Per-ticker status already shows the problem. (`MARKET_INTERFACE.md` §5.1 said `degraded`.) |
| EOD `quoted_at` | 16:00 ET session close, not the bar's window start | The bar `t` is midnight ET, which overstates age by 16 h and would make every EOD close stale on arrival. |
| EOD poll interval | 30 s | Below the 60 s fetch-age limit with room for one failed poll, and 2 calls/min fits the 5/min limit. |
| EOD session date | Discovered once per day and cached | Walking back on every poll exceeds the free 5 calls/min budget over weekends. |
| Re-fetch of an unchanged quote | Advance `fetched_at` only; keep `previous_price` | Otherwise every Massive poll would reset `direction` to `flat` and kill the flash information. |
| Out-of-order quote | Ignored, `fetched_at` unchanged | Never move a quote backward in source time. |
| Reconcile owner | `MarketData`, reading the DB through a callable | Keeps SQL out of `market/` and makes the tracked set always match committed state. |
| `PriceUpdate.to_dict` | Removed; `quotes.py` serializes | Serialization needs the baseline, the clock and the session. |
| RNG | Injected `np.random.Generator` | Reproducible simulator tests. |
| Massive client | Constructed in `__init__`, injectable | Tests pass a fake client returning real `TickerSnapshot` objects instead of patching. |

---

## 17. Edge cases

| Situation | Behavior |
|---|---|
| Empty tracked set at startup | Sources start with no tickers. SSE sends `feed_status` and then `data: {}`. Massive makes no API call (an empty `tickers` list would fetch all stocks). |
| Persisted holdings, first quote not yet received (Massive) | Registered at start; `status: "unavailable"`, null prices; valuation `unavailable`; trades rejected with `quote_unavailable`. |
| Ticker added, buy issued immediately | Simulator: quoted within one reconcile pass (milliseconds). Massive: `quote_unavailable` until the next poll. No waiting and no silent retry. |
| Provider outage | Feed `degraded`; quotes go `stale` after 60 s; SSE stays connected and pushes the status transitions; trades rejected with `quote_stale`. |
| Clock at exactly 60 s fetch age | Still `fresh` (inclusive boundary). |
| Source timestamp 6 s in the future | Rejected at entry; `fetched_at` not advanced. |
| Remove then re-add within a process | Baseline kept; `session_change_percent` measured against the original baseline. |
| Backend restart | New `session_id`; baselines reset; simulator restarts from seeds; the frontend detects the change through `feed_status`. |
| Price above 1e9 or non-finite from the provider | Rejected at entry; logged as missing for Massive. |
| Stream DB read fails | Keeps the last `state_version`/`chat_sequence`; retried next second. |
| Simulator `step()` raises | Logged; feed `degraded`; loop continues; quotes age normally. |
| `create_stream_router()` called twice (tests) | Two independent routers; no duplicate routes. |
| 100+ tickers | Simulator Cholesky stays cheap (n³ with small n). One Massive call per cycle; the URL stays well under limits. |

---

## 18. Tests

All market tests live in `backend/tests/market/`. Run with `uv run --extra dev pytest`. No test sleeps for real intervals: use `ManualClock`, `tick=0`, and direct calls such as `_poll_once()` and `reconcile_now()`.

### 18.1 Existing tests that must change

| File | Change |
|---|---|
| `test_models.py` | Construct with `quoted_at=`/`fetched_at=` instead of `timestamp=`; assert `timestamp == quoted_at`; no rounding in `change`/`change_percent`; delete the `to_dict` tests (moved to `test_quotes.py`). |
| `test_cache.py` | `update(t, p, quoted_at)` signature; replace `test_custom_timestamp` and `test_price_rounding` with unrounded storage and `fetched_at == clock()`. |
| `test_simulator.py` | Replace `test_prices_rounded_to_two_decimals` with an unrounded check; pass `rng=np.random.default_rng(seed)`. |
| `test_simulator_source.py` | Use `ManualClock`; check `quoted_at == fetched_at == clock()`; `feed_status` transitions. |
| `test_massive.py` | Replace `MagicMock` snapshots (which hid the `timestamp` attribute bug) with `TickerSnapshot.from_dict` and a fake client. |
| `test_factory.py` | Use the `env=` parameter. |

`market_data_demo.py` reads `cache.get(...)` fields and keeps working. Update it if it uses `timestamp` as a constructor argument or calls `to_dict()`.

### 18.2 New and updated test examples

**Freshness boundaries (`test_freshness.py`)**

```python
import math
import pytest
from app.market.freshness import is_valid_quote, quote_status
from app.market.models import PriceUpdate

T = 1_763_305_200.0

def q(fetched_age: float, source_age: float) -> PriceUpdate:
    return PriceUpdate("AAPL", 190.0, 190.0, quoted_at=T - source_age, fetched_at=T - fetched_age)

@pytest.mark.parametrize("fa,sa,expected", [
    (0, 0, "fresh"), (60, 60, "fresh"), (60.001, 0, "stale"),
    (1, 86_400, "fresh"), (1, 86_400.001, "stale"),
])
def test_status_boundaries(fa, sa, expected):
    assert quote_status(q(fa, sa), T) == expected

def test_missing_is_unavailable():
    assert quote_status(None, T) == "unavailable"

@pytest.mark.parametrize("price", [0, -1, math.nan, math.inf, True, "190", 1e9 + 1])
def test_invalid_prices(price):
    assert not is_valid_quote(price, T, T)

def test_future_skew():
    assert is_valid_quote(1.0, T + 5, T)
    assert not is_valid_quote(1.0, T + 5.001, T)
```

**Cache baseline and refetch (`test_cache.py`)**

```python
def test_baseline_survives_remove_and_readd():
    clock = ManualClock()
    cache = PriceCache(clock=clock)
    cache.update("AAPL", 190.0, clock())
    cache.update("AAPL", 191.0, clock())
    cache.remove("AAPL")
    assert cache.get("AAPL") is None and cache.baseline("AAPL") == 190.0
    cache.update("AAPL", 200.0, clock())
    assert cache.get("AAPL").direction == "flat"
    assert quote_fields(cache.snapshot(), "AAPL")["session_change_percent"] == pytest.approx(200 / 190 * 100 - 100)

def test_invalid_update_has_no_side_effects():
    clock = ManualClock(); cache = PriceCache(clock=clock)
    first = cache.update("AAPL", 190.0, clock())
    v = cache.version
    clock.advance(10)
    assert cache.update("AAPL", float("nan"), clock()) is None
    assert cache.get("AAPL") is first and cache.version == v

def test_refetch_same_quote_advances_fetched_at_only():
    clock = ManualClock(); cache = PriceCache(clock=clock)
    cache.update("AAPL", 190.0, 100.0); cache.update("AAPL", 191.0, 101.0)
    clock.advance(30)
    u = cache.update("AAPL", 191.0, 101.0)
    assert u.fetched_at == clock() and u.direction == "up" and u.quoted_at == 101.0

def test_new_cache_new_session():
    assert PriceCache().session_id != PriceCache().session_id

def test_prices_not_rounded():
    cache = PriceCache()
    assert cache.update("X", 190.123456, cache.now()).price == 190.123456
```

**Simulator (`test_simulator.py`, `test_simulator_source.py`)**

```python
def test_deterministic_with_seed():
    a = GBMSimulator(["AAPL", "MSFT"], rng=np.random.default_rng(7))
    b = GBMSimulator(["AAPL", "MSFT"], rng=np.random.default_rng(7))
    assert [a.step() for _ in range(100)] == [b.step() for _ in range(100)]

def test_correlation_structure():
    sim = GBMSimulator(["AAPL", "MSFT", "JPM", "V", "TSLA"], event_probability=0.0,
                       rng=np.random.default_rng(1))
    paths = np.array([[p for _, p in sorted(sim.step().items())] for _ in range(20_000)])
    r = np.diff(np.log(paths), axis=0)
    c = np.corrcoef(r.T)   # sorted order: AAPL, JPM, MSFT, TSLA, V
    assert c[0, 2] == pytest.approx(0.6, abs=0.05)   # AAPL-MSFT
    assert c[1, 4] == pytest.approx(0.5, abs=0.05)   # JPM-V
    assert c[0, 3] == pytest.approx(0.3, abs=0.05)   # AAPL-TSLA

async def test_source_stamps_clock_and_quotes_on_add():
    clock = ManualClock(); cache = PriceCache(clock=clock)
    src = SimulatorDataSource(cache, update_interval=3600, rng=np.random.default_rng(0))
    assert src.feed_status == "starting"
    await src.start(["AAPL"])
    q = cache.get("AAPL")
    assert q.quoted_at == q.fetched_at == clock() and src.feed_status == "healthy"
    await src.add_ticker("ZZZZ")
    assert 50 <= cache.get_price("ZZZZ") <= 300
    await src.remove_ticker("ZZZZ")
    assert "ZZZZ" not in cache and cache.baseline("ZZZZ") is not None
    await src.stop(); await src.stop()
```

**Massive with real model objects (`test_massive.py`)**

```python
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, TickerSnapshot

T = 1_763_305_200.0

class FakeClient:
    def __init__(self, snaps=None, exc=None, grouped=None):
        self.snaps, self.exc, self.grouped, self.calls = snaps or [], exc, grouped or {}, []
    def get_snapshot_all(self, market_type, tickers=None, **kw):
        self.calls.append(("snapshot", tuple(tickers)))
        if self.exc: raise self.exc
        return self.snaps
    def get_grouped_daily_aggs(self, date, adjusted=None, **kw):
        self.calls.append(("grouped", date))
        return self.grouped.get(date, [])

def snap(d): return TickerSnapshot.from_dict(d)

async def make(client, *tickers):
    clock = ManualClock(T); cache = PriceCache(clock=clock)
    src = MassiveDataSource("k", cache, client=client)
    for t in tickers: await src.add_ticker(t)
    return src, cache, clock

async def test_last_trade_ns_timestamp():
    c = FakeClient([snap({"ticker": "AAPL", "lastTrade": {"p": 190.5, "t": int((T - 2) * 1e9)}})])
    src, cache, _ = await make(c, "AAPL")
    await src._poll_once()
    q = cache.get("AAPL")
    assert q.price == 190.5 and q.quoted_at == pytest.approx(T - 2) and q.fetched_at == T
    assert src.feed_status == "healthy"

async def test_fallback_to_minute_bar_then_day():
    c = FakeClient([
        snap({"ticker": "A", "min": {"c": 10.0, "t": int((T - 60) * 1e3)}}),
        snap({"ticker": "B", "day": {"c": 20.0}, "updated": int((T - 5) * 1e9)}),
        snap({"ticker": "C", "prevDay": {"c": 30.0}}),            # never used
    ])
    src, cache, _ = await make(c, "A", "B", "C")
    await src._poll_once()
    assert cache.get_price("A") == 10.0 and cache.get_price("B") == 20.0
    assert cache.get("C") is None and src.missing_tickers() == {"C"}

async def test_failure_keeps_cache_and_degrades():
    c = FakeClient([snap({"ticker": "AAPL", "lastTrade": {"p": 1.0, "t": int(T * 1e9)}})])
    src, cache, clock = await make(c, "AAPL")
    await src._poll_once()
    c.exc = BadResponse('{"status":"ERROR"}')
    clock.advance(61)
    await src._poll_once()
    assert src.feed_status == "degraded" and cache.get_price("AAPL") == 1.0
    assert cache.status("AAPL") == "stale"

async def test_not_authorized_switches_to_eod():
    day = "2025-11-16"   # T is 2025-11-16 10:00 ET
    bars = [GroupedDailyAgg.from_dict({"T": "AAPL", "c": 189.0, "t": 0}),
            GroupedDailyAgg.from_dict({"T": "ZZZ", "c": 1.0, "t": 0})]
    c = FakeClient(exc=BadResponse('{"status":"NOT_AUTHORIZED"}'), grouped={"2025-11-14": bars})
    src, cache, _ = await make(c, "AAPL")
    await src._poll_once()
    assert src.eod_mode and cache.get_price("AAPL") == 189.0 and "ZZZ" not in cache
    assert cache.get("AAPL").quoted_at == session_close_ts(date(2025, 11, 14))
    assert ("grouped", day) in c.calls          # walked back from today

async def test_no_call_with_empty_ticker_set():
    c = FakeClient()
    src, _, _ = await make(c)
    await src._poll_once()
    assert c.calls == [] and src.feed_status == "healthy"

async def test_removed_during_fetch_not_written():
    ...  # remove_ticker between fetch and write; assert ticker absent from cache
```

**Service and reconciliation (`test_service.py`)**

```python
async def test_reconcile_follows_db_and_degrades_on_failure():
    tracked = {"AAPL", "MSFT"}
    fail = False
    async def read():
        if fail: raise RuntimeError("db busy")
        return set(tracked)
    cache = PriceCache(clock=ManualClock())
    m = MarketData(cache, ScriptedDataSource(cache, {"AAPL": 1, "MSFT": 2, "NVDA": 3}), read)
    await m.start()
    assert m.tracked_tickers() == ["AAPL", "MSFT"] and m.feed_status == "healthy"
    tracked -= {"MSFT"}; tracked |= {"NVDA"}
    await m.reconcile_now()
    assert m.tracked_tickers() == ["AAPL", "NVDA"] and "MSFT" not in cache
    fail = True; await m.reconcile_now()
    assert m.feed_status == "degraded"
    fail = False; await m.reconcile_now()
    assert m.feed_status == "healthy"
    await m.stop()

async def test_capture_quote_rules():
    clock = ManualClock(); cache = PriceCache(clock=clock)
    src = ScriptedDataSource(cache, {"AAPL": 190.5})
    m = MarketData(cache, src, read_tracked=lambda: _aset({"AAPL", "MSFT"}))
    await m.start()
    assert m.capture_quote("AAPL").price == 190.5
    with pytest.raises(QuoteRejected) as e: m.capture_quote("MSFT")
    assert e.value.code == "quote_unavailable"
    clock.advance(60.5)
    with pytest.raises(QuoteRejected) as e: m.capture_quote("AAPL")
    assert e.value.code == "quote_stale"
```

**Stream (`test_stream.py`)**

```python
async def collect(gen, n):
    return [await anext(gen) for _ in range(n)]

def disconnected_after(k):
    calls = 0
    async def f():
        nonlocal calls; calls += 1
        return calls > k
    return f

async def test_feed_status_first_even_when_empty():
    cache = PriceCache(clock=ManualClock())
    m = MarketData(cache, ScriptedDataSource(cache), read_tracked=lambda: _aset(set()))
    await m.start()
    state = lambda: _aval({"state_version": 3, "chat_sequence": 12})
    out = await collect(price_events(m, state, disconnected_after(1), tick=0), 3)
    assert out[0] == "retry: 1000\n\n"
    assert out[1].startswith("event: feed_status\n")
    assert json.loads(out[1].split("data: ")[1])["state_version"] == 3
    assert out[2] == "data: {}\n\n"

async def test_status_transition_without_price_change():
    clock = ManualClock(); cache = PriceCache(clock=clock)
    src = ScriptedDataSource(cache, {"AAPL": 190.0})
    m = MarketData(cache, src, read_tracked=lambda: _aset({"AAPL"}))
    await m.start()
    gen = price_events(m, lambda: _aval({"state_version": 0, "chat_sequence": 0}),
                       disconnected_after(99), tick=0)
    first = await collect(gen, 3)                       # retry, feed_status, quotes(fresh)
    assert json.loads(first[2][6:])["AAPL"]["status"] == "fresh"
    clock.advance(61)
    frame = await anext(gen)
    assert json.loads(frame[6:])["AAPL"]["status"] == "stale"

async def test_feed_status_event_on_change_and_heartbeat():
    ...  # src.set_feed_status("degraded"); advance clock 1 s -> "event: feed_status" with degraded
    ...  # no changes; advance clock 15 s -> ": heartbeat\n\n"

def test_router_factory_no_duplicate_routes():
    m = ...  # any MarketData
    r1 = create_stream_router(m, read_state)
    r2 = create_stream_router(m, read_state)
    assert len(r1.routes) == 1 and len(r2.routes) == 1 and r1 is not r2
```

(`_aset` and `_aval` are small helpers that return an awaitable of the given value.)

### 18.3 Coverage targets

- `freshness.py`, `cache.py`, `quotes.py`, `service.py`, `stream.py`: 100% of branches.
- `massive_client.py`: every row of §10.4, through `FakeClient`; no network.
- One opt-in live test (`@pytest.mark.live`, skipped unless `MASSIVE_API_KEY` is set) that polls AAPL once and asserts a valid quote or EOD mode.

---

## 19. Implementation order

Each step leaves the test suite green.

1. `clock.py`, `freshness.py`, `models.py` and `cache.py` (plus `snapshot`). Update `test_models.py` and `test_cache.py`; add `test_freshness.py`.
2. `quotes.py` with tests.
3. `interface.py` (`market_mode`, `feed_status`), then the simulator changes. Update the simulator tests.
4. `massive_client.py` rewrite and `test_massive.py` with `FakeClient`. Add `tzdata`.
5. `scripted.py`, `factory.py` (`env=`), `service.py`, with `test_service.py`.
6. `stream.py` rewrite with `test_stream.py`.
7. `__init__.py` exports; `market_data_demo.py` smoke run (`uv run market_data_demo.py`).
8. Integration: `create_app`, lifespan, health route, DB `read_tracked_tickers` / `read_stream_state` (done with the DB and API work).
9. Run `uv run --extra dev pytest --cov=app.market` and `ruff check`, and report the actual results.

---

## 20. Open questions

1. **Chat revision in `feed_status`.** `planning/REVIEW.md` points out that `chat_sequence` does not change when a running assistant message is finalized, so other tabs miss completions. If PLAN adopts a `chat_revision`, it is a DB-layer change only: `read_stream_state` returns the extra key and `price_events` passes the state dict through unchanged.
2. **Trade check order for untracked tickers.** PLAN §6 says a stale or unavailable quote is rejected "whatever the ticker state", but its table also gives `not_tracked` for buying an untracked ticker (which never has a quote). Recommendation: the trade service checks DB ticker state first (`not_tracked` for a buy, `insufficient_shares` for a sell), then calls `capture_quote`. This matches the table and is the order shown in §13.2.
3. **Massive poll interval configuration.** It is a constructor argument (default 15 s). If paid-plan users want 2–5 s, add an optional `MASSIVE_POLL_INTERVAL` env var. Not in PLAN §5 today, so not added.
