# Market Data Backend — Summary

**Status (2026-11-19):** The core subsystem is built and tested (101 tests pass, 97% statement coverage of `app.market`). All seven implementation findings from `MARKET_DATA_REVIEW.md` are fixed. **The PLAN §6 quote lifecycle is not built yet**, so the core is not ready to feed trade execution. See "Still to do".

## What exists

A market data subsystem in `backend/app/market/` with one interface over two price sources.

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Massive (Polygon.io) REST poller (when MASSIVE_API_KEY is set)
        │
        ▼
   PriceCache (thread-safe, in-memory, validates every quote)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation   (not built yet)
        └──→ Trade execution       (not built yet)
```

| File | Purpose |
|------|---------|
| `models.py` | `PriceUpdate`: immutable quote (ticker, price, previous_price, timestamp, change, direction) |
| `interface.py` | `MarketDataSource`: `start/stop/add_ticker/remove_ticker/get_tickers` |
| `cache.py` | `PriceCache`: thread-safe store with a version counter. Rejects invalid prices (≤ 0, NaN/inf, > 1e9), non-finite or > 5 s future timestamps, and quotes older than the cached one. Stores prices unrounded. |
| `seed_prices.py` | Seed prices, per-ticker drift/volatility, correlation groups |
| `simulator.py` | `GBMSimulator` (correlated GBM with random shocks) and `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource`: one batched snapshot per poll. Price falls back from last trade to minute bar to today's bar. Switches to end-of-day grouped bars on a free key's `NOT_AUTHORIZED`: polled every 30 s, each close quoted at 16:00 ET (needs `tzdata`). |
| `factory.py` | `create_market_data_source()`: Massive if `MASSIVE_API_KEY` is non-empty, else the simulator |
| `stream.py` | `create_stream_router()`: a new FastAPI SSE router per call; sends the full price map (including `{}`) whenever the cache version changes |

Behavior worth knowing:
- `PriceCache.update()` returns `None` and changes nothing when it rejects a quote.
- `MassiveDataSource.start()` returns immediately. The first poll runs inside the polling task, so prices appear shortly after startup.
- `remove()` bumps the cache version, and the stream sends the remaining map, including `{}`.

## Review fixes (2026-11-19)

| # | Finding (`MARKET_DATA_REVIEW.md`) | Fix |
|---|---|---|
| 1 | Invalid quotes accepted | Validation in `PriceCache.update()`; unrounded storage; `json.dumps(allow_nan=False)` in the stream |
| 5 | No price without `lastTrade`; no free-plan path | `extract_quote()` fallback chain; EOD mode with the session date discovered once per day; `retries=0` |
| 2 | Removed ticker re-cached by an in-flight poll | Request set captured before the fetch; results applied only for still-tracked tickers |
| 3 | Shared module-level router | Router built inside `create_stream_router()` |
| 4 | Removals not streamed | `remove()` bumps the version; the stream sends empty maps |
| 6 | `stop()` during the first poll did not stop it | First poll moved into the polling task |
| 7 | Duplicate simulator add republished the price | Early return for already-tracked tickers |

## Still to do

From PLAN §6 and `MARKET_DATA_DESIGN.md` (full spec with code):
- `quoted_at` / `fetched_at`, an injectable clock, and the `fresh` / `stale` / `unavailable` freshness predicate
- session ID and session baseline (`session_change_percent`)
- `feed_status` / `market_mode` on sources, the named SSE `feed_status` event, a 15 s heartbeat, and status recompute every second
- the shared REST/SSE quote serializer, and the market service with reconciliation of watchlist ∪ positions every 5 s
- an injected RNG for the simulator, and a scripted test source for E2E
- pin Python 3.13 locally (`.python-version`); the venv currently runs 3.14

## Tests

**101 tests, all passing** (`uv run --extra dev pytest`); ruff clean.

| Module | Tests | Coverage |
|--------|------:|---------|
| test_cache.py | 25 | cache.py: 100% |
| test_factory.py | 7 | factory.py: 100% |
| test_massive.py | 24 | massive_client.py: 97% |
| test_models.py | 11 | models.py: 100% |
| test_simulator.py | 19 | simulator.py: 98% |
| test_simulator_source.py | 11 | (integration) |
| test_stream.py | 4 | stream.py: 89% |

The Massive tests build real `TickerSnapshot` / `GroupedDailyAgg` objects from API-shaped JSON and use fake clients; they make no network calls. EOD mode has not been run against a real free key.

## Demo

```bash
cd backend
uv run market_data_demo.py
```

A live terminal dashboard of the 10 default tickers with sparklines and an event log. Runs 60 seconds or until Ctrl+C.

## Usage for downstream code

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT"])  # returns immediately

update = cache.get("AAPL")          # PriceUpdate or None
price = cache.get_price("AAPL")     # float or None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")
await source.stop()
```
