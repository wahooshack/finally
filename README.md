# FinAlly — AI Trading Workstation

An AI-powered trading workstation: live streaming prices, a simulated $10k portfolio, and an LLM chat assistant that can analyze positions and execute trades. Built by coding agents as the capstone of an agentic AI coding course.

## Status

The core market data subsystem (`backend/app/market/`) is built and tested; its quote-freshness work and the rest of the platform are in development. See [planning/MARKET_DATA_SUMMARY.md](planning/MARKET_DATA_SUMMARY.md) and [planning/PLAN.md](planning/PLAN.md).

## Features

- Live prices over SSE with flash animations and sparklines
- Market orders with instant fills against a simulated portfolio
- Portfolio heatmap, P&L chart, and positions table
- AI chat that trades and manages the watchlist via natural language
- Built-in market simulator, or real data via the Massive (Polygon.io) API

## Architecture

One Docker container on port 8000: FastAPI (Python 3.13, uv) serves the REST/SSE API and a static Next.js export. SQLite persists state in a named volume. LLM calls go through LiteLLM to OpenRouter (Cerebras).

## Quick Start

```bash
cp .env.example .env            # add OPENROUTER_API_KEY
./scripts/start_mac.sh          # Windows: scripts/start_windows.ps1
# open http://localhost:8000
```

Backend only (local dev):

```bash
cd backend
uv sync --extra dev
uv run --env-file ../.env uvicorn app.main:app --reload
uv run pytest
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Unless `LLM_MOCK=true` | OpenRouter key for AI chat |
| `MASSIVE_API_KEY` | No | Real market data; empty uses the simulator |
| `LLM_MOCK` | No | `true` for deterministic mock LLM responses |

## Layout

```
backend/    FastAPI uv project
frontend/   Next.js static export
planning/   Specs and agent contracts
scripts/    Start/stop scripts
test/       Playwright E2E tests
```

The app is single-user and unauthenticated; it binds to localhost only.

## License

See [LICENSE](LICENSE).
