# Market Simulator

How FinAlly simulates stock prices when no `MASSIVE_API_KEY` is set. It is the default market data source: no network, no key, prices for any ticker. It implements the same `MarketDataSource` interface as Massive (MARKET_INTERFACE.md), so the rest of the backend cannot tell the two apart.

Code: `backend/app/market/simulator.py` and `backend/app/market/seed_prices.py`.

## 1. Goals

- Prices look real: they drift, they have the right volatility per stock, sectors move together, and there are occasional jumps.
- Ticks visible in the UI every 500 ms, with sub-cent to few-cent moves.
- Any symbol works. A symbol it has never seen gets a plausible random price.
- Cheap: a 50-ticker step is one small matrix-vector product.
- Testable: randomness can be seeded and time comes from the cache clock.

It does not try to model market hours, order books, bid/ask spreads, volume, or cross-restart continuity.

## 2. The model

### 2.1 Geometric Brownian Motion

Each tick, every price moves by:

```
S(t+dt) = S(t) * exp((mu - sigma^2 / 2) * dt + sigma * sqrt(dt) * Z)
```

| Symbol | Meaning | Source |
|---|---|---|
| `S` | Price | Seed price, then the previous tick |
| `mu` | Annual drift | `TICKER_PARAMS` (default 0.05) |
| `sigma` | Annual volatility | `TICKER_PARAMS` (default 0.25) |
| `dt` | Tick as a fraction of a trading year | `0.5 / (252 * 6.5 * 3600)` = 8.48e-8 |
| `Z` | Correlated standard normal | Cholesky, section 2.2 |

The exponential keeps prices positive. The `-sigma^2/2` term makes `mu` the true expected growth rate.

Because `dt` is calibrated to trading time, every 500 ms tick behaves like 500 ms of a real trading session. One-sigma moves per tick:

| Ticker | Price | sigma | 1-sigma per tick | 1-sigma per 6.5 h of ticks |
|---|---|---|---|---|
| AAPL | $190 | 0.22 | $0.012 | 1.4% |
| NVDA | $800 | 0.40 | $0.093 | 2.5% |
| TSLA | $250 | 0.50 | $0.036 | 3.2% |

That gives a steady flicker of green and red flashes without wild swings.

### 2.2 Correlated moves (Cholesky)

Real stocks in a sector move together. The simulator builds an `n x n` correlation matrix `C` over the tracked tickers, factors it once as `C = L L^T`, and turns independent normals into correlated ones each tick:

```python
z = rng.standard_normal(n)
z_corr = L @ z
```

Pairwise correlation (`_pairwise_correlation`):

| Pair | rho |
|---|---|
| Either ticker is TSLA | 0.3 (it does its own thing) |
| Both in tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| Both in finance (JPM, V) | 0.5 |
| Anything else, including unknown tickers | 0.3 |

`L` is rebuilt only when tickers are added or removed. That is O(n^3) but trivial for n < 100 (checked: 50 tickers factor without trouble). The block structure with an off-diagonal floor of 0.3 is always positive-definite, so `np.linalg.cholesky` does not fail. With 0 or 1 ticker there is no matrix and `z` is used as is.

### 2.3 Shock events

After the GBM step, each ticker has a `event_probability` (default 0.001) chance per tick of a jump of ±2–5%:

```python
if rng.random() < self._event_prob:
    self._prices[t] *= 1 + rng.uniform(0.02, 0.05) * rng.choice((-1, 1))
```

With 10 tickers at 2 ticks/s, that is about one event every 50 s. The events give the heatmap and the AI assistant something to talk about.

### 2.4 Seed prices and unknown tickers

`seed_prices.py` holds realistic starting prices and `{sigma, mu}` for the 10 default tickers. Any other symbol starts at `uniform(50, 300)` with the default parameters. Seeds are not persisted, so an unknown symbol gets a new random price after a backend restart (PLAN section 6, Simulator Restarts).

## 3. Code structure

```
seed_prices.py   SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS,
                 CORRELATION_GROUPS, INTRA_TECH_CORR, INTRA_FINANCE_CORR,
                 CROSS_GROUP_CORR, TSLA_CORR

simulator.py
  GBMSimulator              pure math, no asyncio, no cache
    step() -> dict[str, float]      advance one tick
    add_ticker / remove_ticker      update state, rebuild Cholesky
    get_price / get_tickers
  SimulatorDataSource(MarketDataSource)
    start(tickers)          build GBMSimulator, seed cache, start loop
    stop()                  cancel loop
    add_ticker / remove_ticker
    get_tickers()
    feed_status / market_mode = "simulator"
    _run_loop()             step -> cache.update -> sleep(0.5)
```

The split is deliberate. `GBMSimulator` is synchronous and deterministic given its RNG, so tests can call `step()` directly with no event loop or sleeping. `SimulatorDataSource` is the thin async adapter to the interface.

## 4. Changes required by PLAN.md section 6

The current simulator works; these changes align it with the quote lifecycle.

### 4.1 No rounding

`step()` currently returns `round(price, 2)`. Return the raw float and let the cache store it unrounded. The internal state `self._prices` is already unrounded.

### 4.2 Timestamps from the shared clock

The simulator's quote time is its tick time, so `quoted_at == fetched_at`:

```python
async def _run_loop(self) -> None:
    """Step the simulation every interval and write quotes to the cache."""
    while True:
        try:
            now = self._cache.now()
            for ticker, price in self._sim.step().items():
                self._cache.update(ticker, price, quoted_at=now)
            self._status = "healthy"
        except Exception:
            logger.exception("Simulator step failed")
            self._status = "degraded"
        await asyncio.sleep(self._interval)
```

`cache.update` stamps `fetched_at` with the same clock, so the two match. `start()` and `add_ticker()` seed the cache the same way, so a new ticker has a fresh quote immediately. For the simulator, therefore, the answer to "add to the watchlist and wait for a quote" is effectively instant.

### 4.3 Injectable randomness

Replace module-level `np.random` and `random` calls with one generator:

```python
class GBMSimulator:
    def __init__(self, tickers, dt=DEFAULT_DT, event_probability=0.001,
                 rng: np.random.Generator | None = None) -> None:
        self._rng = rng or np.random.default_rng()
```

Use `self._rng.standard_normal`, `self._rng.random`, `self._rng.uniform`, and `self._rng.choice` everywhere, including the unknown-ticker seed price. `SimulatorDataSource` accepts and passes through `rng`. Tests then use `np.random.default_rng(42)` for reproducible paths.

### 4.4 Remove keeps the baseline

`remove_ticker` keeps calling `cache.remove(ticker)`; the cache now keeps the session baseline (MARKET_INTERFACE.md section 4). Re-adding a ticker in the same process restarts it from its seed price (or a new random one for unknown symbols), while `session_change_percent` still measures against the original baseline. This is acceptable, because the simulator makes no claim of continuity across removal.

### 4.5 Restarts

Each backend start creates a new `PriceCache` (new `session_id`) and a new `GBMSimulator` from seed prices. Persisted cash, holdings, and history survive the restart; simulated prices do not. The P&L chart breaks lines at session boundaries, and the UI explains the reset (PLAN section 6).

## 5. Tests

Existing tests: `tests/market/test_simulator.py` and `test_simulator_source.py`. Update and extend them:

- **Positivity and scale:** 10,000 steps with a seeded RNG keep every price > 0, and a median per-tick move near the section 2.1 table.
- **No rounding:** some stepped prices have more than 2 decimals.
- **Correlation:** with events off, the sample correlation of log returns over ~20k steps is close to 0.6 for AAPL/MSFT, 0.5 for JPM/V, and 0.3 for TSLA/AAPL (tolerance about ±0.05).
- **Events:** `event_probability=1.0` gives every tick a jump of 2–5%; `0.0` gives none.
- **Dynamic tickers:** add or remove rebuilds the Cholesky factor to the right shape; unknown tickers get a price in [50, 300]; removing the last ticker makes `step()` return `{}`.
- **Determinism:** two simulators with the same seed produce identical paths.
- **Source adapter:** after `start()`, every ticker is in the cache with `quoted_at == fetched_at == clock()`; `add_ticker` seeds immediately; `remove_ticker` drops the quote but keeps `cache.baseline()`; `feed_status` moves from `starting` to `healthy`; `stop()` twice is safe.

For E2E tests, PLAN section 12 requires a fixed, test-controlled market source rather than this random simulator. That fixture should be another small `MarketDataSource` that writes explicit prices through `cache.update`. It must not be a mode of this class.
