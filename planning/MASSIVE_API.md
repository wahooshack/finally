# Massive API Reference (formerly Polygon.io)

Reference for the parts of the Massive REST API that FinAlly uses to fetch real-time and end-of-day (EOD) US stock prices for many tickers at once.

- Base URL: `https://api.massive.com` (the legacy `api.polygon.io` host still works)
- Python client: `massive` on PyPI (this project locks **2.2.0**), `from massive import RESTClient`
- Docs: https://massive.com/docs/rest/stocks/overview
- Verified on 2026-11-18 against Context7 docs (`/massive-com/client-python`, `/llmstxt/massive_llms_txt`) and the installed package source.

## 1. Authentication and client setup

You can pass the API key as the `apiKey` query parameter or as an `Authorization: Bearer <key>` header. The Python client handles this for you:

```python
from massive import RESTClient

client = RESTClient(
    api_key=os.environ["MASSIVE_API_KEY"],
    connect_timeout=5.0,
    read_timeout=10.0,
    retries=0,   # our poll loop is the retry; see section 6
)
```

`RESTClient.__init__` signature (2.2.0): `api_key=None, connect_timeout=10.0, read_timeout=10.0, num_pools=10, retries=3, base="https://api.massive.com", pagination=True, verbose=False, trace=False, custom_json=None`.

**The client is synchronous** (urllib3). In FastAPI, call it with `await asyncio.to_thread(...)` so it does not block the event loop.

## 2. Plans, data freshness and rate limits

Plans change; check https://massive.com/pricing before relying on these.

| Plan (Stocks) | Price | Calls | Recency | Snapshot | `lastTrade` in snapshot | `lastQuote` |
|---|---|---|---|---|---|---|
| Basic | Free | 5 / minute | End of day | Not included | No | No |
| Starter | Paid | Unlimited | 15-min delayed | Yes | No | No |
| Developer | Paid | Unlimited | 15-min delayed | Yes | Yes | No |
| Advanced | Paid | Unlimited | Real-time | Yes | Yes | Yes |
| Business | Paid | Unlimited | Real-time | Yes | Yes | Yes (+ `fmv`) |

What this means for FinAlly:

- **Every snapshot field is optional.** `lastTrade` is returned only on plans that include trades, so the price must come from a fallback chain (section 3.3).
- **Free keys cannot use snapshots.** On Basic, use grouped daily bars (section 4.1): one call returns the EOD close for every US stock.
- "Unlimited" plans still advise staying under ~100 requests/second. One snapshot call per 2–15 s is far below that.

## 3. Real-time prices: snapshot for many tickers

### 3.1 Endpoint

`GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,TSLA`

| Query param | Notes |
|---|---|
| `tickers` | Comma-separated, **case-sensitive**. If omitted, returns all ~10,000 tickers. |
| `include_otc` | Default `false`. |

Unknown or untraded symbols are left out of the response; no error is raised. Snapshot data is cleared daily around 3:30 AM ET and fills in again from about 4:00 AM ET, so `day` may be empty early in the morning.

### 3.2 Response

```json
{
  "status": "OK",
  "count": 1,
  "tickers": [{
    "ticker": "AAPL",
    "day":     {"o": 189.1, "h": 191.0, "l": 188.7, "c": 190.4, "v": 51234567, "vw": 190.02},
    "min":     {"o": 190.3, "h": 190.5, "l": 190.2, "c": 190.45, "v": 120000, "vw": 190.4,
                "av": 51234567, "n": 812, "t": 1763305140000},
    "prevDay": {"o": 187.9, "h": 189.5, "l": 187.2, "c": 189.0, "v": 48000000, "vw": 188.6},
    "lastTrade": {"p": 190.5, "s": 100, "x": 4, "c": [14, 41], "i": "71675577320245",
                  "t": 1763305159123456789},
    "lastQuote": {"p": 190.49, "s": 3, "P": 190.51, "S": 2, "t": 1763305159223456789},
    "todaysChange": 1.5,
    "todaysChangePerc": 0.794,
    "updated": 1763305159123456789
  }]
}
```

**Timestamp units differ by field:**

| Field | Unit |
|---|---|
| `lastTrade.t`, `lastQuote.t`, `updated` | **Nanoseconds** since the Unix epoch |
| `min.t` and aggregate bar `t` | **Milliseconds** since the Unix epoch |

### 3.3 Python client mapping

`client.get_snapshot_all("stocks", tickers=[...])` returns `list[TickerSnapshot]`. JSON keys map to snake_case attributes:

| JSON | Python attribute | Type |
|---|---|---|
| `ticker` | `snap.ticker` | `str` |
| `lastTrade.p` / `.t` | `snap.last_trade.price` / `snap.last_trade.sip_timestamp` | `float` / `int` ns |
| `lastQuote.p` / `.P` / `.t` | `snap.last_quote.bid_price` / `.ask_price` / `.sip_timestamp` | `float` / `int` ns |
| `min.c` / `.t` | `snap.min.close` / `snap.min.timestamp` | `float` / `int` ms |
| `day.c` | `snap.day.close` | `float` |
| `prevDay.c` | `snap.prev_day.close` | `float` |
| `todaysChangePerc` | `snap.todays_change_percent` | `float` |
| `updated` | `snap.updated` | `int` ns |

> **Gotcha (fixed 2026-11-19):** `LastTrade` has **no** `timestamp` attribute. The trade time is `sip_timestamp`, in nanoseconds. Reproduced with the installed 2.2.0 package:
>
> ```
> >>> s = TickerSnapshot.from_dict({"ticker":"X","lastTrade":{"p":20.5,"t":1605192894630916600}})
> >>> s.last_trade.sip_timestamp   # 1605192894630916600
> >>> s.last_trade.timestamp       # AttributeError
> ```
>
> `backend/app/market/massive_client.py` used to read `snap.last_trade.timestamp / 1000.0`, which raised `AttributeError` for every ticker, so the Massive source never wrote a price. It now reads `sip_timestamp / 1e9`, and `tests/market/test_massive.py` builds real `TickerSnapshot` objects so the attribute names are checked.

### 3.4 Extracting a price and source time

Use the freshest field the plan provides. This is implemented as `extract_quote()` in `backend/app/market/massive_client.py`:

```python
from massive.rest.models import TickerSnapshot

NS, MS = 1e9, 1e3

def extract_quote(snap: TickerSnapshot) -> tuple[float, float] | None:
    """Return (price, quoted_at_unix_seconds) from a snapshot, or None."""
    lt = snap.last_trade
    if lt and lt.price and lt.sip_timestamp:
        return lt.price, lt.sip_timestamp / NS
    m = snap.min
    if m and m.close and m.timestamp:
        return m.close, m.timestamp / MS
    if snap.day and snap.day.close and snap.updated:
        return snap.day.close, snap.updated / NS
    return None
```

`prevDay.c` is yesterday's close. Do not use it as a live price. It is useful as a reference value only.

### 3.5 Complete example

```python
import asyncio, os
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"], retries=0)

async def fetch_prices(tickers: list[str]) -> dict[str, tuple[float, float]]:
    snaps = await asyncio.to_thread(
        client.get_snapshot_all, SnapshotMarketType.STOCKS, tickers
    )
    out = {}
    for snap in snaps:
        q = extract_quote(snap)
        if q:
            out[snap.ticker] = q
    return out

print(asyncio.run(fetch_prices(["AAPL", "MSFT", "NVDA"])))
# {'AAPL': (190.5, 1763305159.123), 'MSFT': (...), 'NVDA': (...)}
```

### 3.6 Single ticker (not used for polling)

```python
snap = client.get_snapshot_ticker("stocks", "AAPL")   # GET /v2/snapshot/locale/us/markets/stocks/tickers/AAPL
trade = client.get_last_trade("AAPL")                 # GET /v2/last/trade/AAPL  (Developer+)
```

Each of these is one call per ticker, so they do not suit the 5 calls/min Basic limit. Always batch with `get_snapshot_all`.

## 4. End-of-day prices

### 4.1 Grouped daily: all tickers, one call (works on Basic)

`GET /v2/aggs/grouped/locale/us/market/stocks/{YYYY-MM-DD}?adjusted=true`

```python
from datetime import date, timedelta

def find_eod_session(client: RESTClient, today: date, max_back: int = 5) -> tuple[date, list] | None:
    """Most recent (date, bars) that has grouped bars, walking back over weekends/holidays.

    Call this once per day and cache the date; it can cost several calls.
    """
    for days_back in range(max_back):
        day = today - timedelta(days=days_back)
        bars = client.get_grouped_daily_aggs(day.isoformat(), adjusted=True)
        if bars:
            return day, bars
    return None

found = find_eod_session(client, date.today())        # discovery: up to max_back calls
if found:
    session_day, bars = found
    closes = {b.ticker: b.close for b in bars if b.ticker in {"AAPL", "MSFT"}}
    # Later polls the same day: one call for the cached date
    bars = client.get_grouped_daily_aggs(session_day.isoformat(), adjusted=True)
```

`GroupedDailyAgg` fields: `ticker` (`T`), `open`, `high`, `low`, `close`, `volume`, `vwap`, `timestamp` (ms), `transactions`, `otc`. The response covers about 10k tickers (several MB), so filter it client-side. If the date has no session (weekend or holiday), the result list is empty.

**Cache the session date.** Walking back on every poll costs up to 3 calls per poll over a weekend, which breaks the free 5 calls/min limit.

**Quote time for a close.** A daily bar's `t` marks the start of the day window, not the close. For the source time (`quoted_at`), use that session's 16:00 America/New_York close (needs `tzdata` on Windows). Otherwise the close looks 16 hours older than it is. See MARKET_DATA_DESIGN.md §10.2.

### 4.2 Previous close: one ticker

`GET /v2/aggs/ticker/{ticker}/prev`

```python
bars = client.get_previous_close_agg("AAPL")   # list[PreviousCloseAgg]
close, t_ms = bars[0].close, bars[0].timestamp
```

### 4.3 Daily open/close for a date: one ticker

`GET /v1/open-close/{ticker}/{date}`

```python
oc = client.get_daily_open_close_agg("AAPL", "2026-11-16")
oc.open, oc.close, oc.high, oc.low, oc.pre_market, oc.after_hours, oc.volume
```

### 4.4 Historical bars (charts or backfill)

`GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

```python
for bar in client.list_aggs("AAPL", 1, "day", "2026-10-01", "2026-11-16", limit=50000):
    print(bar.timestamp, bar.close)    # timestamp in ms; the iterator auto-paginates
```

FinAlly has no price-history endpoint, so this is reference only.

## 5. Errors

| Situation | HTTP | Client raises |
|---|---|---|
| Missing key | — | `massive.exceptions.AuthError` at construction |
| Invalid key | 401 | `massive.exceptions.BadResponse` (body JSON in `str(e)`) |
| Endpoint not in plan | 403 `NOT_AUTHORIZED` | `BadResponse` |
| Rate limited | 429 | Retried by urllib3 when `retries > 0`, then `urllib3.exceptions.MaxRetryError` |
| 5xx | 500/502/503/504 | Retried the same way |
| Network / timeout | — | `urllib3` exceptions |

```python
from massive.exceptions import BadResponse

try:
    snaps = client.get_snapshot_all("stocks", ["AAPL"])
except BadResponse as e:
    if "NOT_AUTHORIZED" in str(e):
        ...  # plan lacks snapshots, so switch to grouped-daily EOD (section 4.1)
    raise
```

## 6. Polling guidance for FinAlly

- **One batched snapshot call per cycle** for the whole tracked set (watchlist plus positions).
- **Interval:** snapshots every 15 s by default (2–5 s is fine on paid plans). In EOD mode, every 30 s with the session date cached (section 4.1). That is 2 calls/min, within the free 5 calls/min, and keeps fetch age under the 60 s freshness limit even if one poll fails. (The code currently uses 60 s; see MARKET_DATA_DESIGN.md §10.2.)
- **`retries=0`:** the poll loop is already the retry, and urllib3 retries on 429 waste free-tier quota and hide failures from `feed_status`.
- **Quote time:** use the source timestamp converted to seconds as `quoted_at`. The 15-minute delay on Starter/Developer is visible as source age; it does not make the quote invalid.
- **Missing tickers:** a requested symbol absent from the response is a per-ticker failure. Do not advance that ticker's `fetched_at`.
- **Plan detection:** a 403 on snapshots means the key is Basic. Fall back to grouped daily EOD and log the mode once (implemented in `MassiveDataSource`). EOD closes become stale for trading after 24 hours (PLAN section 6), which is the intended behavior.
