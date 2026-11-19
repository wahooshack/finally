# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single start script. A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, P&L %
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet
- **Acceptance baseline**: functional at 1024 × 768 (tablet landscape), optimized for 1440 × 900 and larger. All controls support keyboard operation with visible focus and accessible labels. Gains, losses, connection states, and action outcomes include text or symbols alongside color.
- **Local access**: this unauthenticated, single-user app is published on the host loopback interface only. Network-accessible deployment is a separate scope requiring access controls.

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python 3.13 / uv)                     │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving        │
│                      (Next.js export)           │
│                                                 │
│  SQLite database (named Docker volume)          │
│  Background tasks: market data, snapshots       │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python 3.13), managed as a `uv` project
- **Database**: SQLite, single file `finally.db` in the `db/` directory
- **Real-time data**: Server-Sent Events (SSE) — one-way server→client push
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided
- **Process model**: exactly one backend worker owns the price cache, feed, subscription reconciler, and snapshot task. Multiple tabs are supported; multiple backend workers are not.

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── db/                   # Schema definitions, seed data, init logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests
├── db/                       # SQLite location for local (non-Docker) runs
│   └── .gitkeep              # finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration.
- **`backend/db/`** contains schema SQL definitions and seed logic.
- **`db/`** at the top level holds `finally.db` for local runs. In Docker the database lives in a named volume mounted at `/app/db`.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests. Unit tests live within `frontend/` and `backend/` respectively.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required (unless LLM_MOCK=true): OpenRouter API key for LLM chat
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses; the app starts without `OPENROUTER_API_KEY`
- **In Docker**: `.env` is passed with `docker run --env-file .env`
- **Local dev**: run from `backend/` with `uv run --env-file ../.env ...` (e.g. `uv run --env-file ../.env uvicorn app.main:app --reload`)

---

## 6. Market Data (Existing Core and Required Integration)

The core market data subsystem exists in `backend/app/market/`; `planning/MARKET_DATA_SUMMARY.md` documents that implementation. The lifecycle, freshness, baseline, and stream extensions below are required integration work, not features already provided by the core.

- `create_market_data_source()` returns the simulator or Massive source based on `MASSIVE_API_KEY`. Both implement `start / stop / add_ticker / remove_ticker / get_tickers`.
- The source writes into a shared in-memory `PriceCache` (latest price, previous price, timestamp, change, direction per ticker). Portfolio valuation and trade execution read prices from this cache.
- `GET /api/stream/prices` (from `create_stream_router(price_cache)`) pushes an SSE event ~every 500ms when prices change. Each event's `data` is a JSON object keyed by ticker: `{"AAPL": {"ticker": "AAPL", "price": 190.50, "previous_price": ..., "timestamp": ..., "change": ..., "direction": ...}, ...}`.
- The simulator prices any ticker. Unknown tickers get a random seed price ($50–$300) and default volatility.

### Required Changes to `backend/app/market/`

The existing core does not yet support the rules below. Make these changes and update the existing market tests to match:

- **`PriceUpdate` / `PriceCache.update()`**: stop rounding prices (currently `round(price, 2)`); round only for display. Replace the single `timestamp` with `quoted_at` (source time) and `fetched_at` (backend receipt time); keep `timestamp` as a derived alias of `quoted_at` for the SSE payload.
- **Session baseline**: add a baseline store in `PriceCache`, keyed by ticker, set on the first valid price and not cleared by `remove()`.
- **Data sources**: the simulator sets `quoted_at = fetched_at =` its tick time. The Massive client passes the provider's quote time as `quoted_at`. Both report failures so the backend can derive `feed_status` (`starting`, `healthy`, `degraded`).
- **Clock**: freshness checks read time through one injectable clock function so tests can control quote age.
- **`stream.py`**: build the `APIRouter` inside `create_stream_router()` (it is currently module-level, so repeated calls register duplicate routes). Add the named `feed_status` event, the 15-second heartbeat, and the once-a-second status recompute described below.

### Integration Rules

- **Tracked tickers = watchlist ∪ held positions.** At startup the backend registers all watchlist tickers and position tickers with the data source. Removing a ticker from the watchlist only calls `remove_ticker` if no position is held; selling a position to zero only calls `remove_ticker` if the ticker is not on the watchlist.
- **Change %** uses a backend-process session baseline, retained in memory independently of the latest update: the first valid price per ticker since backend startup. Retain the baseline through removal/re-addition within that process; reset on backend restart. REST and SSE expose `session_id`, `session_baseline`, and `session_change_percent = (price / session_baseline - 1) * 100`. Late-connecting browsers get the same baseline. The existing `change_percent` and `direction` remain previous-tick metrics for flashes, not session performance.
- The frontend ignores SSE entries for tickers it isn't displaying.

### Quote Lifecycle and Freshness

- Trade outcomes by ticker state. "Tracked" means watched or held; only tracked tickers receive quotes. Any trade whose quote is stale or unavailable is rejected with 400 (`quote_stale` / `quote_unavailable`), whatever the ticker state.

  | Ticker state | Buy (fresh quote) | Sell (fresh quote) |
  |---|---|---|
  | Watched | Executes | Executes if enough shares are held, else 400 `insufficient_shares` |
  | Held, not watched | Executes and adds the ticker to the watchlist in the same transaction | Executes if enough shares are held, else 400 `insufficient_shares` |
  | Neither | 400 `not_tracked`: add it to the watchlist and wait for a quote | 400 `insufficient_shares` |

  A first-time purchase therefore means adding the ticker to the watchlist and waiting for a quote first. A rejected trade never adds a watchlist entry or creates a subscription, so a trade request never blocks on the provider.
- Adding to the watchlist commits immediately even if no quote is available; show `unavailable` until one arrives. Failed or delayed subscription work does not undo the watchlist change.
- Each quote records source `quoted_at` separately from backend `fetched_at`, the last successful receipt of a valid quote for that symbol. Provider failures, missing symbols, and invalid prices do not advance `fetched_at`. Repeated successful fetches of the same source quote may advance it.
- A quote is `fresh` only when its price is finite and positive, fetch age is at most 60 seconds, and source age is at most 24 hours; otherwise an existing valid quote is `stale`, or `unavailable` when none exists. Reject future source timestamps more than 5 seconds ahead as invalid. These are simulated-fill freshness limits, not a promise of exchange-real-time prices: expose source age even when fresh, and reject old closed-market quotes after 24 hours. Use the same clock and predicate for REST, SSE, snapshots, and execution.
- Trades reject unavailable or stale quotes with 400. Inside the trade transaction, capture one immutable quote, recheck freshness, and use its price throughout the fill; never read a second execution price or await provider work inside that transaction.
- Valuations retain every holding. For stale quotes, expose the last price as indicative with status `stale`; for missing quotes, use null price and null derived values. Aggregate valuation status is `unavailable` if any holding lacks a quote, otherwise `stale` if any is stale, otherwise `fresh` (cash-only portfolios are fresh). `total_value` and total profit are null unless all holdings are fresh; never substitute zero or silently omit a holding.
- Extend SSE quote entries with the REST quote fields from Section 8 while preserving the existing numeric Unix-seconds `timestamp` as a legacy alias for source time. Recompute status at least once a second and emit status transitions even without a price/cache-version change. Send a heartbeat every 15 seconds. Feed status (`starting`, `healthy`, `degraded`) describes provider/subscription condition independently of browser SSE connectivity.
- **`feed_status` event**: a named SSE event, sent first on every connection (before any quotes, even with no tracked tickers) and again whenever any of its fields changes. Payload: `{"feed_status": "healthy", "market_mode": "simulator", "session_id": "<uuid>", "state_version": 3, "chat_sequence": 12}`. `state_version` is the committed `users_profile.state_version`; `chat_sequence` is the highest committed `chat_messages.sequence` (0 when empty), raised by chat journal writes as well as finalization. Read both during the once-a-second recompute. This event lets every open tab detect backend restarts and changes committed by other tabs.
- Reconcile subscriptions from committed watchlist ∪ positions at startup, after mutations, and every 5 seconds. Retry failed additions/removals on that loop, expose degraded feed status, and rebuild from database state after restart. Never roll back a committed trade because a subscription update failed.

### Simulator Restarts

Keep the existing simulator reset behavior, but assign each backend lifetime a new `session_id` and record market mode (`simulator` or `massive`). Persist snapshots by session. History responses mark session boundaries; the chart never connects lines across simulator resets or market-mode changes. The UI explains that simulator prices reset on restart while cash, holdings, trades, and earlier history persist. Unknown symbols may receive a new random seed on restart; no cross-session market gain is inferred.

---

## 7. Database

### SQLite, Initialized at Startup

A FastAPI lifespan hook runs `CREATE TABLE IF NOT EXISTS` for all tables and seeds default data if the user profile is missing. No separate migration step; a fresh Docker volume starts with a clean, seeded database automatically.

The database path is `db/finally.db` relative to the project root locally, and `/app/db/finally.db` in the container.

### Schema

Every table has a `user_id` column (TEXT, default `"default"`). It is hardcoded for now (single-user); this column alone does not implement multi-user access control. Timestamps are UTC RFC 3339 strings with millisecond precision and a `Z` suffix.

**users_profile** — User state (cash balance)
- `user_id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `state_version` INTEGER (default: `0`; incremented on every committed portfolio/watchlist mutation)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- PRIMARY KEY `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- PRIMARY KEY `(user_id, ticker)`

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL
- `price` REAL
- `quoted_at`, `fetched_at` TEXT (captured execution quote timestamps)
- `request_id` TEXT (owning operation)
- `action_id` TEXT (unique within that operation; `"trade"` for a manual trade)
- `executed_at` TEXT (ISO timestamp)
- UNIQUE `(user_id, request_id, action_id)`

**portfolio_snapshots** — Portfolio value over time. Record immediately after startup registration, then every 30 seconds, including null-valued gaps when quotes are not fresh. Record an additional snapshot on the first transition to a fully priced portfolio so startup need not wait for the timer. Read cash/positions consistently and capture one quote set per snapshot.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `session_id` TEXT
- `market_mode` TEXT (`"simulator"` or `"massive"`)
- `state_version` INTEGER
- `total_value` REAL (nullable under the valuation rules in Section 6)
- `valuation_status` TEXT (`"fresh"`, `"stale"`, or `"unavailable"`)
- `recorded_at` TEXT (ISO timestamp)

Retain 7 days of snapshots; the API serves the last 24 hours. Sort by `(recorded_at, id)` ascending, and represent null values and session boundaries as chart gaps. The chart displays total portfolio value in USD, with a $10,000 reference line; `profit = total_value - 10000` is a separate label, nullable by the same rule.

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `sequence` INTEGER (unique per user, allocated monotonically on insert; user message before its assistant reply)
- `content` TEXT
- `request_id` TEXT (owning chat operation)
- `status` TEXT (`"running"`, `"completed"`, `"partial"`, or `"failed"`; null for user messages; `running` only until the chat request is finalized)
- `actions` TEXT (JSON — ordered action results from Section 9, updated as each action commits; null for user messages)
- `created_at` TEXT (ISO timestamp)
- UNIQUE `(user_id, request_id, role)`

**operation_requests** — Stored responses for mutating REST/chat requests, so a retry never runs twice
- `user_id` TEXT
- `request_id` TEXT (client-generated UUID)
- `operation` TEXT (method and endpoint)
- `payload_hash` TEXT (canonical validated payload, including target ticker)
- `response_status` INTEGER (null while a chat request is running)
- `response_body` TEXT (JSON; exact final response; null while a chat request is running)
- `created_at` TEXT
- PRIMARY KEY `(user_id, request_id)`

Keep these rows for the lifetime of the database so an old retry cannot become a new trade.

### Transactions and Request Identity

- Enable SQLite WAL, foreign keys, and a 5-second busy timeout. Use `BEGIN IMMEDIATE` for each mutation transaction so cash/share validation occurs after obtaining the write lock. Read authoritative balances/positions inside that transaction; never validate against a stale frontend or pre-transaction portfolio. Return 503 on lock timeout with no mutation; retry only with the same request ID.
- A trade transaction includes balance/share checks, captured-price validation, cash and position changes, trade log insert, automatic watchlist insert, and state-version increment. All commit together or all roll back. Independent REST mutations use the same service as chat actions. No network, LLM, or subscription calls occur inside a write transaction.
- Every mutation carries a client-generated `request_id`: in the JSON body for POST and a required `request_id` query parameter for DELETE. Normalize tickers before hashing; hash operation plus validated business fields, excluding the ID. Check identity before evaluating current business state. Same ID and same payload returns the stored HTTP status/body without running it again; different payload or endpoint returns 409.
- For manual trades/watchlist changes, insert the `operation_requests` row, perform the mutation, and store its response in one transaction. Business rejections are stored as final responses without portfolio changes; malformed requests (422) are rejected before any row is inserted. An unexpected database failure rolls back the whole transaction, so a same-ID retry is safe. The trade log's UNIQUE `(user_id, request_id, action_id)` is an extra duplicate guard.
- Chat journals its progress so an interruption never loses an outcome:
  1. A short transaction before the provider call inserts the `operation_requests` row (null response), the user message, and an assistant message with status `running` and `actions: []`.
  2. After plan validation, one transaction writes the full planned action list into that assistant message, each action with status `pending`.
  3. Each action's transaction performs its mutation and updates that action's entry (`executed`/`rejected` with result/error) in the assistant message, so a committed mutation and its recorded outcome commit together.
  4. A final transaction sets the assistant content and status and stores the operation response.
- A same-ID chat retry while the response is still null returns 409 (`chat request still running`).
- **Startup recovery**: on startup, before serving requests, finalize every assistant message still `running`. Mark `pending` actions `skipped`, compute the final status from the recorded outcomes, set content to the backend summary of those outcomes followed by `"Interrupted by a server restart; remaining actions were skipped."`, and store the matching operation response. Never resume or re-run actions. A same-ID retry then returns this finalized response, so the user sees what actually executed before deciding to send a new message.

### Default Seed Data

- One user profile: `user_id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. API Endpoints

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Cash balance, total value, positions (ticker, quantity, avg cost, current price, unrealized P&L, P&L %) |
| POST | `/api/portfolio/trade` | Execute a trade: `{request_id, ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots from the last 24 hours (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{request_id, ticker}` |
| DELETE | `/api/watchlist/{ticker}?request_id={uuid}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/chat` | Recent chat messages (last 50), so the panel survives a page refresh |
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |

### Rules

- **Errors** have shape `{"detail": "..."}`. Use a custom validation mapping so malformed JSON, missing/unknown fields, invalid UUID/ticker/side, non-finite or out-of-range numbers, and wrong types produce 422 with a string `detail`. Use 400 for business rejections (insufficient cash/shares, unavailable/stale quote), 404 for removing an absent watchlist entry, 409 for request-ID conflicts, and 503 for database contention. Chat action business failures are recorded in its 200 response instead.
- **Ticker validation**: tickers are uppercased and must match `^[A-Z.]{1,10}$`. No external lookup.
- **Trading rules**:
  - A trade fills at one fresh cached price. Outcomes per ticker state, including the automatic watchlist add, follow the table in Section 6.
  - Buys update `avg_cost` as a weighted average. Sells leave `avg_cost` unchanged.
  - Delete a position only on an exact full liquidation (`requested_quantity == held_quantity`), explicitly setting the remaining quantity to zero. Preserve every positive residual; never discard a holding below an epsilon.
  - Quantities must be finite numbers in `(0, 1e12]`; reject booleans and numeric strings. Execution prices must be finite and in `(0, 1e9]`; every calculated product, balance, and average must also be finite. Store REAL values without display rounding. Check `cost <= cash` and `sell_quantity <= held_quantity` strictly, with no tolerance that grants buying power/shares. Reject an operation if floating-point precision would leave a changed cash/quantity value numerically unchanged.
  - Balance and quantity invariants are nonnegative; open positions have positive quantity and average cost. Use stable summation for valuation. Comparison tolerances are for tests/display only: absolute `1e-8` USD for cash/value and `1e-12` shares for quantity in bounded test fixtures, never for authorizing or deleting positions. Format USD to 2 decimals and quantities to up to 8 decimals, with an explicit `<0.00000001` cue for smaller holdings and a Sell all control using the full returned quantity.
- **Watchlist**: removing a ticker that has an open position is allowed; its price keeps streaming (see Section 6).
- **Percentages**: a position's `pnl_percent = (current_price − avg_cost) / avg_cost * 100`. A quote's `change_percent` is the previous-tick change (for flashes) and `session_change_percent` is the change since the session baseline. All percentage fields use percentage points (`5` means 5%), never fractional ratios.

### Response Contract and Examples

All REST timestamps are UTC RFC 3339 milliseconds (`2026-11-16T15:00:00.000Z`); absent values are JSON null, never NaN/Infinity. IDs below are illustrative UUIDs. GETs return 200. Successful manual trade/watchlist mutations return 200 (including duplicate watchlist additions); DELETE returns a JSON result rather than 204. A replay returns the originally stored status/body, even if state has since changed. Fields in the following examples are required unless explicitly nullable.

Quote fields shared by REST and SSE are `price`, `quoted_at`, `fetched_at`, `status`, `session_id`, `session_baseline`, `session_change_percent`, `previous_price`, `change`, `change_percent`, and `direction`. Price/timestamps/baseline/changes/direction are nullable before the first quote. Stale quotes retain their last known values; `status` governs usability. The legacy SSE `timestamp` remains numeric, while the new timestamp fields use the REST format. `direction` is `up`, `down`, or `flat` when known.

`GET /api/watchlist` returns entries ordered by `(added_at, ticker)` ascending, including new symbols without quotes:

```json
{
  "state_version": 0,
  "feed_status": "starting",
  "items": [{
    "ticker": "AAPL", "added_at": "2026-11-16T15:00:00.000Z",
    "price": null, "quoted_at": null, "fetched_at": null,
    "status": "unavailable", "session_id": "a0d05845-7140-44dd-93de-c183fb726d81",
    "session_baseline": null, "session_change_percent": null,
    "previous_price": null, "change": null, "change_percent": null, "direction": null
  }]
}
```

`GET /api/portfolio` returns positions sorted by ticker. Each position's `quote` uses the shared quote fields:

```json
{
  "state_version": 1, "as_of": "2026-11-16T15:00:01.000Z",
  "cash_balance": 9809.5, "total_value": 10000.0, "profit": 0.0,
  "valuation_status": "fresh",
  "positions": [{
    "ticker": "AAPL", "quantity": 1.0, "avg_cost": 190.5,
    "current_price": 190.5, "market_value": 190.5,
    "unrealized_pnl": 0.0, "pnl_percent": 0.0,
    "quote": {
      "price": 190.5, "quoted_at": "2026-11-16T15:00:00.000Z", "fetched_at": "2026-11-16T15:00:00.000Z",
      "status": "fresh", "session_id": "a0d05845-7140-44dd-93de-c183fb726d81",
      "session_baseline": 190.5, "session_change_percent": 0.0,
      "previous_price": 190.5, "change": 0.0, "change_percent": 0.0, "direction": "flat"
    }
  }]
}
```

For an unavailable quote, that position's `current_price`, `market_value`, `unrealized_pnl`, and `pnl_percent` are null; the position remains present. With a stale quote those fields are indicative and the quote status is shown. Aggregate `total_value` and `profit` are null whenever valuation is not fresh.

`POST /api/portfolio/trade` request:

```json
{"request_id":"b8d1166c-9163-45ae-ae3d-3676840f6950","ticker":"AAPL","side":"buy","quantity":1}
```

Response (chat actions reference this committed trade via `trade_id` and expose its fill price):

```json
{
  "request_id": "b8d1166c-9163-45ae-ae3d-3676840f6950", "status": "executed", "state_version": 1,
  "trade": {
    "id": "9c026acc-2632-48ec-b559-abde9b566e38", "ticker": "AAPL", "side": "buy",
    "quantity": 1.0, "price": 190.5, "total": 190.5, "executed_at": "2026-11-16T15:00:01.000Z",
    "quoted_at": "2026-11-16T15:00:00.000Z", "fetched_at": "2026-11-16T15:00:00.000Z"
  },
  "cash_balance": 9809.5, "position": {"ticker":"AAPL","quantity":1.0,"avg_cost":190.5},
  "watchlist_added": false
}
```

After a full sell, `position` is null. Watchlist mutations return `{request_id, ticker, status, state_version}` where status is `added`, `unchanged` (already present), or `removed`. An unchanged add does not change `added_at` or increment the version.

`GET /api/portfolio/history` uses server time for the 24-hour cutoff, returns points in `(recorded_at, id)` order, and includes null-valued gaps:

```json
{
  "metric": "total_value", "currency": "USD", "initial_balance": 10000.0,
  "points": [{
    "id": "6b03bb8c-3379-41e1-833a-33b696b5ce60", "recorded_at": "2026-11-16T15:00:00.000Z",
    "session_id": "a0d05845-7140-44dd-93de-c183fb726d81", "market_mode": "simulator",
    "state_version": 0, "total_value": 10000.0, "valuation_status": "fresh"
  }]
}
```

`GET /api/chat` returns `{messages: [...]}` containing the latest 50 messages by `sequence`, displayed in ascending sequence order. Each entry has `id`, `sequence`, `request_id`, `role`, `content`, `status`, `actions`, and `created_at`. User entries have null status/actions; assistant entries contain the status/action results from Section 9, including failed requests. For example, a persisted provider failure is:

```json
{
  "id": "2b5fd434-e5a9-46b8-b081-07f6834bacab", "sequence": 2, "request_id": "21f9d570-8d4a-4922-bd53-aea79f463b18",
  "role": "assistant", "content": "I couldn't process that request. No actions were executed.",
  "status": "failed", "actions": [], "created_at": "2026-11-16T15:01:00.000Z"
}
```

`GET /api/health` returns `{status: "ok", market_mode, feed_status, session_id}` after database initialization; feed degradation is reported independently of process health.

---

## 9. LLM Integration

When writing code to make calls to LLMs, use the cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Accepts `{request_id, message}`; message must contain 1–4,000 characters after trimming. Applies the request identity rules in Section 7 before any provider call.
2. Loads current portfolio/watchlist context with quote statuses and at most 20 prior messages, including recorded action outcomes. Cap prior-history text/serialized outcomes at 32,000 characters by dropping oldest complete request pairs; never drop outcome records while retaining a claim that an action succeeded. Persistent history is not deleted by this prompt bound.
3. Calls the LLM via LiteLLM → OpenRouter with structured output, a 30-second provider deadline, and automatic provider retries disabled. No database transaction remains open while awaiting the provider.
4. Validates the complete response and at most 10 ordered actions before executing anything; assigns stable action IDs derived from `request_id` plus ordinal (`a0`, `a1`, etc., scoped to that request). Malformed or oversized plans fail without actions. Journals the validated plan with every action `pending` (Section 7).
5. Executes actions sequentially in list order, using the same transaction/validation code as REST. Each action has its own atomic transaction, which also records that action's outcome. On a business rejection, record a rejected result and continue; earlier successes remain committed. Later actions read current committed state, enabling sell-then-buy. On an unexpected error, record that action as rejected with `internal_error`, stop, retain committed results, and mark remaining actions skipped.
6. Builds completion text from actual action outcomes, finalizes the assistant message and response (Section 7), and returns HTTP 200 JSON. No token streaming is required.

### Structured Output Schema

Both fields are required; `actions` may be empty. Use a strict discriminated union for action types and reject extra fields. The array is the execution order, including interleaved watchlist and trade actions.

```json
{
  "message": "I propose selling AAPL before buying MSFT.",
  "actions": [
    {"type": "trade", "ticker": "AAPL", "side": "sell", "quantity": 1},
    {"type": "watchlist_add", "ticker": "MSFT"},
    {"type": "trade", "ticker": "MSFT", "side": "buy", "quantity": 1},
    {"type": "watchlist_remove", "ticker": "NFLX"}
  ]
}
```

- `message`: analysis or proposed intent, at most 4,000 characters; never authoritative evidence of execution.
- `actions`: `trade` requires ticker/side/quantity; `watchlist_add` and `watchlist_remove` require ticker. IDs and outcomes are assigned by the backend, not the model.
- Adding a ticker does not guarantee its quote is ready for the next action. A following buy rejects if still unavailable; do not wait on provider I/O or silently retry it. Show that outcome and allow a later explicit request after a quote arrives.

### Response to Frontend

`POST /api/chat` returns:

```json
{
  "request_id": "21f9d570-8d4a-4922-bd53-aea79f463b18",
  "status": "partial",
  "message": "Sold 1 AAPL at $190.50. Buy MSFT rejected: no price available yet.",
  "actions": [
    {"action_id":"a0","type":"trade","ticker":"AAPL","side":"sell","quantity":1,
     "status":"executed","result":{"trade_id":"9c026acc-2632-48ec-b559-abde9b566e38","price":190.5,"state_version":2},"error":null},
    {"action_id":"a1","type":"trade","ticker":"MSFT","side":"buy","quantity":1,
     "status":"rejected","result":null,"error":{"code":"quote_unavailable","detail":"no price available for MSFT yet"}}
  ]
}
```

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is deliberate:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

Action results retain their validated input and list order. Each has `action_id`, `status` (`executed`, `rejected`, `skipped`; `pending` only while the request is `running`), `result` (null unless executed), and `error` (null on success, otherwise `{code, detail}`). Trade results include `trade_id`, `price`, and `state_version`; watchlist results include mutation status (`added`, `unchanged`, `removed`) and `state_version`. Error codes include `insufficient_cash`, `insufficient_shares`, `not_tracked`, `quote_unavailable`, `quote_stale`, `watchlist_not_found`, and `internal_error`.

Final request status is `completed` when all actions succeed (or none were requested), `partial` when successes coexist with rejections/skips, and `failed` when no action succeeded.

When actions were proposed, discard the model's pre-execution message for user-facing completion text and render a backend-generated summary of actual results. For analysis-only responses, display the model message. Persist that displayed text and the same result list so reload and future context agree with what happened. No second provider call is needed to summarize execution.

### Failure Handling

If the LLM call fails (API error, timeout, unparseable JSON, invalid/oversized plan), log the error and finalize HTTP 200 with `status: "failed"`, `message: "I couldn't process that request. No actions were executed."`, and `actions: []`. Finalize the journaled messages and the operation response, so the failure survives reload. A backend crash mid-request is finalized by the startup recovery in Section 7. Same-ID retry returns this failure; a deliberate retry of the intent uses a new ID. A client timeout does not cancel the server operation; a same-ID retry returns the stored result once it is final. Serialize active chat requests for the default user; reject a different new request with 409 while one is active, without reserving its ID, to avoid overlapping conversation turns. Manual trades remain supported concurrently through database serialization.

### System Prompt Guidance

The LLM is prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON
- Treat execution as proposed until backend results confirm it; use quote status and actual prior outcomes when reasoning

### LLM Mock Mode

When `LLM_MOCK=true`, the backend skips OpenRouter and applies fixed rules to the user message (case-insensitive):
- Contains `"buy"` → proposal `"Mock: buying 1 AAPL."` with action `{"type":"trade","ticker":"AAPL","side":"buy","quantity":1}` (buy takes precedence if both words occur)
- Contains `"sell"` and an AAPL position is held → proposal `"Mock: selling 1 AAPL."` with action `{"type":"trade","ticker":"AAPL","side":"sell","quantity":1}`; normal validation may reject a holding below one share
- Contains `"sell"` and no AAPL position is held → message `"Mock: no AAPL position to sell."` with `actions: []`
- Otherwise → message `"Mock response."` with `actions: []`

Mock responses go through the same execution, storage, and response code as real ones. The mock buy follows the Section 6 table: if AAPL is neither watched nor held, it is rejected with `not_tracked` (AAPL is watched in the default seed).

---

## 10. Frontend Design

### Layout

A single-page application with a dense, terminal-inspired layout. Component architecture is up to the Frontend Engineer, but the UI includes:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), session change % (`session_change_percent`), and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger price chart for the selected ticker. Clicking a ticker in the watchlist selects it. Like sparklines, it shows only data accumulated since page load; there is no price history endpoint.
- **Portfolio heatmap** — treemap where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss). Cash is not included. With no positions, show a placeholder message.
- **P&L chart** — total portfolio value in USD from `/api/portfolio/history`, with a $10,000 reference line, explicit gaps for invalid valuations, and labeled session/reset boundaries (Section 7)
- **Positions table** — ticker, quantity, avg cost, current price, unrealized P&L, P&L % (`pnl_percent`)
- **Trade bar** — ticker field, quantity field, buy button, sell button. Market orders, instant fill. Errors shown inline.
- **AI chat panel** — docked/collapsible sidebar. Loads history from `GET /api/chat` on page load. Message input, scrolling conversation, loading indicator while waiting. Trade executions, watchlist changes and errors shown inline.
- **Header** — portfolio total value (updating live), SSE connection indicator, separate feed condition/source-age cue, cash balance

### Data Flow

- Fetch `/api/portfolio` and `/api/watchlist` on load, and re-fetch after every trade, watchlist change, or chat response with actions.
- Keep tabs in sync through the `feed_status` event (Section 6), which arrives on every (re)connection. If its `session_id` differs from the one applied, clear quote/chart caches and refetch portfolio, watchlist, and history. If its `state_version` is higher than the applied one, refetch portfolio and watchlist. If its `chat_sequence` differs from the latest loaded message, refetch `/api/chat`.
- Live total value is computed on the frontend: `cash + Σ(quantity × latest SSE price)`, only when every holding has a fresh quote. Apply Section 6 null/stale rules to totals, P&L, tables, and chart gaps. If any holding cannot be freshly valued, replace treemap sizing with a stale/unavailable placeholder and keep all holdings visible in the table; do not present a partial allocation as a complete one.
- Poll `/api/portfolio/history` every 30 seconds to match the snapshot cadence.
- Ignore a portfolio/watchlist response whose `state_version` is lower than the one already applied for that resource. Keep SSE quotes in a separate cache and value holdings against the latest quote. Quote-age timers must transition to stale even when SSE is disconnected.
- Generate each mutation's UUID once per user action. Disable the initiating control while pending. On a network error, retry once with the same ID; do not persist pending IDs across reload. Render returned chat action results, then refresh portfolio/watchlist.
- If a selected ticker leaves the watchlist but is still held, retain its chart; otherwise select the first watchlist ticker, then first holding, or show an empty chart. Missing quotes show a waiting state and disable trading for that symbol; stale quotes show their age and disable trading. Server validation remains authoritative.
- Bound each frontend price-series buffer to 1,000 points per displayed ticker, discard buffers for symbols neither watched nor held, and cap retained symbol buffers at 100 with least-recently-used eviction. Keep at most the last 50 chat messages rendered and enforce the 4,000-character input bound with a visible counter/error. Session percentage always comes from the backend baseline, not the bounded chart buffer.

### Technical Notes

- Use `EventSource` for the SSE connection to `/api/stream/prices`; its built-in retry handles reconnection
- **Charts**: Lightweight Charts (canvas) for the main price chart, sparklines, and P&L line chart. The heatmap is a simple hand-rolled treemap of absolutely positioned divs — no second charting library.
- Price flash effect: on a new price, briefly apply a CSS class with a background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 24 LTS slim
  - Copy frontend/
  - npm ci && npm run build (produces static export in out/)

Stage 2: Python 3.13 slim
  - Install uv
  - Copy backend/
  - uv sync --frozen (install Python dependencies from lockfile)
  - Copy frontend build output into backend static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app, bound to 0.0.0.0:8000 inside the container, exactly one worker
```

FastAPI registers all `/api` routes first, then mounts `StaticFiles(directory="static", html=True)` at `/` so the frontend never shadows API paths.

### Docker Volume

The SQLite database persists in a named Docker volume:

```bash
docker run -d --name finally -v finally-data:/app/db -p 127.0.0.1:8000:8000 --env-file .env finally
```

The host port binds only to loopback; start scripts must use the same mapping. Keep `.env` and `.env.*` (except the committed placeholder `.env.example`) out of the image build context with `.dockerignore`. Pass secrets at runtime via `--env-file`, never copy them into an image layer. Network-accessible hosting requires a separate access-control design.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts are idempotent — safe to run multiple times.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)** — market data tests already exist; update them for the market changes in Section 6. Add:
- Portfolio: trade execution, weighted avg cost, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss, selling to zero, no cached price)
- LLM: structured output parsing, malformed responses, mock mode rules, action execution and error reporting
- API routes: status codes, response shapes, error handling
- Database: init and seed on an empty database are idempotent
- Atomicity: inject a write failure between trade updates and verify cash, positions, trades, watchlist, state version, and the stored response all roll back. Use separate database connections and synchronized competing requests to prove overlapping buys cannot overspend and sells cannot oversell, including manual/chat overlap.
- Request identity: retry a committed manual/chat request with the same ID and assert one trade and the stored response. Cover conflicting payload IDs (409), a same-ID chat retry while running (409), duplicate watchlist adds, and rejection replay.
- Ticker states: every row of the Section 6 trade table, including buying a held-but-unwatched ticker (adds it to the watchlist) and an untracked ticker (`not_tracked`).
- Quotes: first purchase requires registration, delayed first poll, stale source versus stale fetch times, provider outage without clearing cached prices, invalid/future timestamps, startup holdings awaiting prices, and held-but-unwatched subscriptions. Cover reconciliation recovery after failed subscription updates and baseline consistency for late clients, removal/re-addition, and process restart.
- Chat: ordered sell-then-buy, interleaved add/buy, a rejected middle action with later execution, provider deadline, invalid/oversized output, persisted failure, partial success reload/context, an unexpected error mid-plan (remaining actions skipped), and an interruption between an action commit (trade and watchlist) and finalization, where startup recovery finalizes the request with the committed outcomes and a same-ID retry returns it. Assert completion text reflects actual outcomes even if the model claims a rejected trade succeeded.
- History: initial/first-priced snapshots, null-valued valuation gaps, 24-hour filtering, 7-day pruning, deterministic ordering, simulator-session boundaries, and persisted portfolio/history across restart. Inject a clock and invoke one snapshot iteration directly; do not sleep 30 seconds per test.

**Frontend** — unit-test pure logic only:
- Portfolio math (total value, P&L, P&L %)
- Price flash direction
- Treemap layout calculation
- Null/stale valuation propagation, session-baseline versus tick percentage, REST/SSE ordering, and bounded buffer eviction

Component behavior is covered by E2E tests.

### E2E Tests (in `test/`)

**Infrastructure**: Playwright runs from the host (`npx playwright test`) against the running app container at `http://localhost:8000`.

**Environment**: Run with `LLM_MOCK=true`, `MASSIVE_API_KEY` explicitly empty (override inherited keys), and no provider credentials. Inject a deterministic market source with fixed, test-controlled quotes/clock through a test-only server fixture; a seeded random simulator alone is insufficient for exact balance assertions. Test controls must not be exposed in the normal production app. Neither LLM nor market tests make external calls.

**Isolation**: Stateful E2E scenarios run with one Playwright worker and parallelism disabled. Reset an exclusive test database/volume to seed state before each scenario using fixtures that also clear market caches and clock state. Restart-persistence scenarios retain that scenario's volume only until complete. Parallel CI jobs need separate containers, ports, and volumes; a fresh shared volume does not isolate tests.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send "buy", receive a response, trade execution appears inline
- Chat history persists across a page reload
- SSE resilience: disconnect and verify reconnection
- Two tabs: a trade, watchlist change, or chat message in one tab appears in the other via the `feed_status` event
- Restart detection: after a backend restart, reconnect with an empty tracked set and with persisted holdings awaiting a first quote; the frontend detects the new `session_id` and resets its caches
- Same-ID retry of a manual/chat request yields one execution; partial chat failures and provider failures survive reload
- First-time ticker registration, delayed/unavailable/stale quotes, provider outage while SSE stays connected, and startup with persisted holdings
- Held-but-unwatched price updates, refresh/SSE races, selected-ticker removal, keyboard-only trading/chat/watchlist operation, and text cues for status/P&L at the minimum layout
- Restart preserves cash/holdings/history and marks simulator reset gaps; use the controlled clock to advance snapshot timing
- Clean Docker build/start smoke check with an isolated volume and loopback port: `/api/health`, `/`, and referenced static JS/CSS assets load successfully; verify one worker, seeded database, and API routes are not shadowed by static serving

### Completion Criteria

Implementation is complete when the shared REST/SSE contracts above are exercised by backend/frontend integration checks, transaction and replay tests prove one execution per intended action, degraded quotes never masquerade as complete valuations, and reload/restart preserves actual chat outcomes. Run unit tests plus the isolated E2E scenarios and clean-container smoke check; report what was run rather than assuming the existing market tests still pass.
