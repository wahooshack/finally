# Market Data Interface

The unified Python API FinAlly uses for stock prices. It uses Massive when `MASSIVE_API_KEY` is set and the built-in simulator otherwise. The rest of the backend never knows which one is running.

The design keeps the existing structure in `backend/app/market/` and adds the changes required by PLAN.md section 6: unrounded prices, `quoted_at` and `fetched_at`, the session baseline, freshness, feed status, an injectable clock, and a per-call stream router. API details are in MASSIVE_API.md; simulator internals are in MARKET_SIMULATOR.md. MARKET_DATA_DESIGN.md is the full spec with complete code; where the two differ, that doc wins.

**Status (2026-11-19):** the items marked **Done** below are in the code (99 tests pass). Everything else is still design.

## 1. Shape

```
             create_market_data_source(cache)          <- reads MASSIVE_API_KEY
                         |
          +--------------+--------------+
          |                             |
  SimulatorDataSource           MassiveDataSource
  (GBM, 500 ms ticks)          (REST poll, 2-15 s)
          |                             |
          +-------- cache.update() -----+     producers write
                         |
                    PriceCache  (clock, session_id, baselines, version)
                         |
     +-------------------+--------------------+
     |                   |                    |
 SSE stream      portfolio valuation    trade execution      consumers read
```

Rules:

1. **Sources push, consumers read the cache.** Nothing calls a data source for a price. A trade reads `cache.get(ticker)` once and checks it with `quote_status`.
2. **One clock.** `PriceCache` owns the clock function. Sources stamp `fetched_at` and consumers judge freshness through `cache.now()`, so a test can control quote age by injecting one function.
3. **One freshness predicate** (`quote_status`) is used by REST, SSE, snapshots, and execution.

## 2. Module layout

| File | Contents | Change |
|---|---|---|
| `models.py` | `PriceUpdate` | Add `quoted_at`, `fetched_at`; drop rounding of `change` |
| `freshness.py` | Limits, `is_valid_quote`, `quote_status` | **New**. `is_valid_quote` is **Done** but currently lives in `cache.py`; move it here with `quote_status`. |
| `cache.py` | `PriceCache` | Clock, session, baselines. **Done:** validation, unrounded prices, out-of-order rejection, removal bumps `version`. |
| `interface.py` | `MarketDataSource` ABC | Add `market_mode`, `feed_status`, `reconcile` |
| `simulator.py` | `GBMSimulator`, `SimulatorDataSource` | Stamps tick time. **Done:** no rounding, duplicate `add_ticker` is a no-op. |
| `massive_client.py` | `MassiveDataSource` | Feed status. **Done:** timestamp bug, price fallback, EOD mode, `retries=0`, non-blocking `start()`, removed-during-poll guard. |
| `factory.py` | `create_market_data_source` | Pass-through only |
| `stream.py` | `create_stream_router` | `feed_status` event; heartbeat. **Done:** router built per call, empty map sent, strict JSON. |
| `seed_prices.py` | Simulator constants | Unchanged |

## 3. Models and freshness

### 3.1 `PriceUpdate` (`models.py`)

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable quote for one ticker. Prices are stored unrounded."""

    ticker: str
    price: float
    previous_price: float
    quoted_at: float    # source time, Unix seconds
    fetched_at: float   # backend receipt time, Unix seconds

    @property
    def timestamp(self) -> float:
        """Legacy alias for quoted_at (numeric SSE field)."""
        return self.quoted_at

    @property
    def change(self) -> float:
        return self.price - self.previous_price

    @property
    def change_percent(self) -> float:
        """Previous-tick change in percentage points (for flashes)."""
        return (self.price / self.previous_price - 1) * 100

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"
```

Rounding happens only for display (frontend, LLM prompt, chat summaries). Because the cache rejects non-positive prices, `previous_price` is never zero.

### 3.2 Freshness (`freshness.py`, new)

```python
"""Quote validity and freshness rules shared by REST, SSE, snapshots and trades."""

import math
from typing import Literal

QuoteStatus = Literal["fresh", "stale", "unavailable"]

MAX_FETCH_AGE = 60.0          # seconds since backend receipt
MAX_SOURCE_AGE = 24 * 3600.0  # seconds since source quote time
MAX_FUTURE_SKEW = 5.0         # source time may run at most 5 s ahead
MAX_PRICE = 1e9


def is_valid_quote(price: float, quoted_at: float, now: float) -> bool:
    """True if a raw quote may enter the cache."""
    return (
        math.isfinite(price)
        and 0 < price <= MAX_PRICE
        and math.isfinite(quoted_at)
        and quoted_at <= now + MAX_FUTURE_SKEW
    )


def quote_status(update: "PriceUpdate | None", now: float) -> QuoteStatus:
    """Classify a cached quote at time `now`."""
    if update is None:
        return "unavailable"
    if now - update.fetched_at > MAX_FETCH_AGE or now - update.quoted_at > MAX_SOURCE_AGE:
        return "stale"
    return "fresh"
```

Invalid quotes never reach the cache, so a cached quote is always valid; freshness is purely about age.

## 4. `PriceCache` (`cache.py`)

```python
Clock = Callable[[], float]


class PriceCache:
    """Thread-safe latest-quote store for one backend session."""

    def __init__(self, clock: Clock = time.time, session_id: str | None = None) -> None:
        self.now = clock
        self.session_id = session_id or str(uuid.uuid4())
        self._prices: dict[str, PriceUpdate] = {}
        self._baselines: dict[str, float] = {}
        self._lock = Lock()
        self._version = 0

    def update(self, ticker: str, price: float, quoted_at: float) -> PriceUpdate | None:
        """Store a quote if valid; stamps fetched_at from the clock. Returns None if rejected."""
        now = self.now()
        if not is_valid_quote(price, quoted_at, now):
            return None
        with self._lock:
            prev = self._prices.get(ticker)
            update = PriceUpdate(ticker, price, prev.price if prev else price, quoted_at, now)
            self._prices[ticker] = update
            self._baselines.setdefault(ticker, price)
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...
    def baseline(self, ticker: str) -> float | None: ...

    def remove(self, ticker: str) -> None:
        """Drop the latest quote. The session baseline is kept."""

    def status(self, ticker: str) -> QuoteStatus:
        return quote_status(self.get(ticker), self.now())

    @property
    def version(self) -> int: ...
```

Behavior that tests must pin down:

| Case | Result |
|---|---|
| First valid quote | `previous_price == price`, `direction == "flat"`, baseline set |
| Invalid price, NaN, or future time | Returns `None`; nothing changes (`fetched_at` not advanced) |
| Same source quote fetched again | New `fetched_at`; `quoted_at` unchanged; version bumped |
| `remove()` then re-add | Baseline unchanged |
| New `PriceCache` (backend restart) | New `session_id`, empty baselines |

### 4.1 Quote fields (REST and SSE)

One serializer builds the PLAN section 8 quote object so REST and SSE cannot drift:

```python
def quote_fields(cache: PriceCache, ticker: str) -> dict:
    """Shared quote fields; nulls before the first quote."""
    q, base = cache.get(ticker), cache.baseline(ticker)
    return {
        "price": q.price if q else None,
        "quoted_at": iso_ms(q.quoted_at) if q else None,
        "fetched_at": iso_ms(q.fetched_at) if q else None,
        "status": quote_status(q, cache.now()),
        "session_id": cache.session_id,
        "session_baseline": base,
        "session_change_percent": (q.price / base - 1) * 100 if q and base else None,
        "previous_price": q.previous_price if q else None,
        "change": q.change if q else None,
        "change_percent": q.change_percent if q else None,
        "direction": q.direction if q else None,
    }
```

`iso_ms` formats UTC RFC 3339 with milliseconds and a `Z` suffix. SSE entries add `ticker` and the legacy numeric `timestamp` (`q.quoted_at`).

## 5. `MarketDataSource` (`interface.py`)

```python
FeedStatus = Literal["starting", "healthy", "degraded"]


class MarketDataSource(ABC):
    """Produces quotes into a PriceCache. Consumers never call it for prices."""

    market_mode: str  # "simulator" or "massive"

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing quotes. Call once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call twice."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Track a ticker. No-op if present. Must not block on the provider."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and drop its cached quote (baseline is kept)."""

    @abstractmethod
    def get_tickers(self) -> list[str]: ...

    @property
    @abstractmethod
    def feed_status(self) -> FeedStatus: ...

    async def reconcile(self, desired: set[str]) -> None:
        """Converge the tracked set to `desired` (watchlist union positions)."""
        current = set(self.get_tickers())
        for t in sorted(desired - current):
            await self.add_ticker(t)
        for t in sorted(current - desired):
            await self.remove_ticker(t)
```

Both sources keep `add_ticker` and `remove_ticker` in memory only. The new symbol is picked up on the next tick or poll, so they cannot fail on the provider and never block a request.

The app calls `reconcile()` at startup, after each committed mutation, and every 5 s, always with the set read from committed DB state. Because the set comes from the database, the app never needs to know whether a ticker is watched or held.

### 5.1 Feed status

| Source | `starting` | `healthy` | `degraded` |
|---|---|---|---|
| Simulator | Before `start()` | After `start()` | Never in normal operation; set if `step()` raises |
| Massive | Before the first poll succeeds | Last poll succeeded, even if some tickers were missing from it | Last poll failed (network, 401/403/429/5xx) |

A failed poll leaves cached prices alone. They age into `stale` on their own through the clock. A ticker missing from a successful response (for example a mistyped symbol) shows as `unavailable` or `stale` per ticker and does not degrade the whole feed (MARKET_DATA_DESIGN.md §16).

## 6. `MassiveDataSource`

**Done (2026-11-19):**
- The timestamp bug is fixed (`sip_timestamp / 1e9`; MASSIVE_API.md section 3.3).
- `extract_quote()` falls back from the last trade to the minute bar to today's bar, and never uses yesterday's close (MASSIVE_API.md 3.4).
- Each poll captures the ticker list before the request and writes results only for tickers still tracked when it returns.
- On a 403 `NOT_AUTHORIZED` (free Basic key), the source switches once to EOD mode with grouped daily bars, discovering the session date once per day (MASSIVE_API.md 4.1). The switch is logged once.
- `RESTClient(api_key, retries=0)`: the poll loop is the retry.
- `start()` does not await the first poll. It runs inside the polling task, so `stop()` also cancels an in-flight first poll.
- The default interval stays 15 s and can be overridden by the constructor argument.

**Still to do:**
- feed status, with `self._status` set as in §5.1
- `market_mode = "massive"`
- EOD polling every 30 s instead of 60 s, and EOD `quoted_at` at the 16:00 ET close (MARKET_DATA_DESIGN.md §10.2)

Target shape of the poll once feed status is added:

```python
async def _poll_once(self) -> None:
    """Fetch one batched snapshot and write valid quotes to the cache."""
    tickers = list(self._tickers)  # immutable request set
    if not tickers:
        self._status = "healthy"
        return
    try:
        quotes = await asyncio.to_thread(self._fetch, tickers)
    except Exception as e:
        logger.error("Massive poll failed: %s", e)
        self._status = "degraded"
        return
    current = set(self._tickers)  # skip tickers removed while the fetch was in flight
    for ticker, quote in quotes.items():
        if ticker in current:
            self._cache.update(ticker, *quote)
    self._status = "healthy"
```

## 7. Factory (`factory.py`)

The signature is unchanged; the clock travels inside the cache:

```python
def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    """Massive if MASSIVE_API_KEY is non-empty, else the simulator. Returns it unstarted."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=cache)
    return SimulatorDataSource(price_cache=cache)
```

## 8. SSE stream (`stream.py`)

```python
StateReader = Callable[[], Awaitable[tuple[int, int]]]   # -> (state_version, chat_sequence)


def create_stream_router(cache: PriceCache, source: MarketDataSource,
                         read_state: StateReader) -> APIRouter:
    """Build a fresh router per call (fixes duplicate route registration)."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(_events(cache, source, read_state, request),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
```

Event loop (every 0.5 s):

1. Send `retry: 1000`, then a named `feed_status` event **first**, before any quotes:
   `event: feed_status\ndata: {"feed_status", "market_mode", "session_id", "state_version", "chat_sequence"}`
2. Once a second, recompute `feed_status`, `read_state()`, and every tracked ticker's `quote_status`. Emit `feed_status` if any of its fields changed.
3. Send a `data:` quote event when `cache.version` changed **or** any ticker's status changed (for example `fresh` to `stale` with no new price). The payload is `{ticker: quote_fields + ticker + timestamp}`.
4. Send the comment line `: heartbeat` if nothing was sent for 15 s.

`read_state` is provided by the database layer, which keeps `stream.py` free of SQL.

## 9. Using it from the app

```python
# lifespan
cache = PriceCache()                                  # tests: PriceCache(clock=fake_clock)
source = create_market_data_source(cache)
await source.start(sorted(await db.tracked_tickers()))  # watchlist union positions
app.include_router(create_stream_router(cache, source, db.read_stream_state))
reconciler = asyncio.create_task(reconcile_loop(source, db, every=5.0))
...
await source.stop()

# trade execution: capture one immutable quote and use it throughout the fill
quote = cache.get(ticker)
match quote_status(quote, cache.now()):
    case "unavailable":
        raise TradeRejected("quote_unavailable")
    case "stale":
        raise TradeRejected("quote_stale")
fill_price = quote.price
```

## 10. Tests to update or add

- `test_models.py`: no rounding; `timestamp == quoted_at`; `change_percent` in percentage points.
- `test_freshness.py` (new): boundary cases at 60 s fetch age, 24 h source age, and a +5 s future time; NaN, zero, negative, and > 1e9 prices.
- `test_cache.py`: invalid quotes rejected without side effects; baseline set once and kept through `remove()`; `fetched_at` comes from the injected clock.
- `test_massive.py`: build snapshots with `TickerSnapshot.from_dict` using the real JSON keys (`lastTrade.t` in ns), not hand-built mocks that hide attribute names. Cover the price fallback chain, a missing ticker (degraded), a failed poll (degraded, cache untouched), and a 403 switch to EOD mode.
- `test_stream.py` (new): `feed_status` first even with an empty cache; a status transition is emitted with no price change; heartbeat after 15 s (inject the clock and interval); two `create_stream_router()` calls do not duplicate routes.
