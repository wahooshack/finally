# Market Data Backend Review

Review date: 2026-11-19. Reviewed HEAD: `c53ab8383b4c54960c94d252b5416859fcda32c5`.

## Conclusion

**The implemented core has a sound structure and all 74 existing tests pass, but it is not ready for trading integration.** There are seven actionable implementation findings: one P1, five P2s, and one P3. Passing tests miss subscription races, stream isolation, invalid quotes, and provider capability differences. The current PLAN also requires substantial market integration work that is explicitly documented as unimplemented.

The source/cache separation, immutable quote objects, batched provider calls, and separate synchronous simulation engine are good foundations. The GBM equation and Cholesky correlation implementation behaved correctly in additional numerical checks. These strengths do not establish quote validity or lifecycle correctness.

This review makes no application changes. The pre-existing modification to `planning/REVIEW.md` was preserved.

## Scope and documentation

Read every existing file recursively under the repository-level `planning/` directory: `PLAN.md`, `MARKET_DATA_SUMMARY.md`, `MARKET_DATA_DESIGN.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`, `REVIEW.md`, and all five documents in `planning/archive/`. Also read both READMEs, the root/backend developer guides, and `backend/planning/REVIEW.md`.

Reviewed all nine Python modules in `backend/app/market/` (including `__init__.py`), all six market test modules and test configuration, `market_data_demo.py`, `pyproject.toml`, and relevant installed Massive 2.2.0 client code. Checked repository test inventory and CI workflows; the 74 backend tests are the entire existing application test suite. Frontend, full application, Docker, and E2E suites do not exist yet.

PLAN section 6 explicitly calls freshness, baselines, feed status, and expanded streaming **required integration work**, while the current detailed design is marked **Design**. Those omissions are listed separately below rather than described as newly introduced regressions. Historical reviews were checked against current source; their conclusions were not assumed to remain valid.

## Implementation findings

Priorities: P1 = fix before consuming prices for fills/valuations; P2 = correctness issue to fix; P3 = lower-impact contract issue. Locations are relative to the repository root.

### 1. [P1] Reject invalid quotes before replacing the cached price

Location: `backend/app/market/cache.py:23-42`; consumer: `backend/app/market/stream.py:80-83`.

`PriceCache.update()` accepts zero, negative, non-finite, and arbitrarily large prices, plus unchecked timestamps. `round()` does not validate floats. A malformed provider quote can therefore replace a good quote, increment the version, and reach every consumer. The stream uses permissive `json.dumps`, which emits `NaN`/`Infinity`; browser `JSON.parse` rejects those payloads. Zero/negative prices would also be unsuitable for downstream fills and valuations, though trading itself is not implemented yet.

Reproduced: prices `0.0`, `-1.0`, `nan`, `inf`, and `2e9` were all stored. Serializing the NaN/Infinity updates with `allow_nan=False` raised `ValueError`. A positive price of `0.001` became `0.0` through rounding. An update at source time 100 overwrote a newer quote at source time 200. An explicit timestamp of `0.0` was silently replaced with wall time by `timestamp or time.time()`.

Recommendation: implement the planned common validation boundary and injectable clock. Reject invalid inputs without changing the prior quote or version; retain raw prices; distinguish missing timestamps from supplied values; enforce the agreed future-time limit and source ordering. Use strict JSON serialization as a final guard. Add boundary and rejection tests, including preservation of an existing valid quote. Unrounded storage and full freshness are already planned requirements, but invalid quotes affect the current stream today.

### 2. [P2] Discard results for tickers removed during an in-flight Massive poll

Location: `backend/app/market/massive_client.py:99-111`; related: `:74-77`, `:126-130`.

The poll awaits a worker-thread fetch and then writes every returned snapshot without checking current subscriptions. While it awaits, `remove_ticker()` can remove a ticker and clear its cache entry. When the response arrives, the poll puts that ticker back in the cache even though `get_tickers()` excludes it. Streams continue including the resurrected quote, and subsequent polls do not refresh it.

Reproduced with a real worker thread and a paused fake fetch: remove AAPL after the fetch starts, release a valid AAPL snapshot, and await completion. Result: tracked tickers `[]`, cached tickers `['AAPL']`.

Recommendation: capture an immutable request set before dispatch, then check current membership before applying each result. Use subscription generations if removal/re-addition must invalidate the old request too. Pass the captured set into the worker instead of reading mutable `self._tickers` there. Test removal of the final ticker, partial removal, and removal/re-addition during a fetch. This also avoids an empty subscription set reaching the provider after the initial guard.

### 3. [P2] Build a distinct router for each cache

Location: `backend/app/market/stream.py:17-26`, `:48`.

`create_stream_router()` decorates a module-level router. Two calls return the same object and accumulate duplicate `/api/stream/prices` routes, each closing over a different cache. Rebuilding an app in tests or embedding two app instances can serve the earlier cache through the later app's router.

Reproduced: construct cache A with AAPL and cache B with MSFT, then call the factory for each. Both returned the same router with two routes. Calling the first route on the second returned router produced AAPL data from cache A.

Recommendation: create `APIRouter` inside the factory. Add an isolation test proving different objects, one route per router, and data from the intended cache. This known issue remains present despite the earlier summary's broad completion statement.

### 4. [P2] Publish cache removals, including transition to an empty quote map

Location: `backend/app/market/cache.py:59-62`; `backend/app/market/stream.py:75-83`.

Removal does not increment the version that drives SSE change detection. With no subsequent update, a connected client receives no indication that the cache changed. Even after adding a version bump, `if prices:` suppresses the frame when the final ticker disappears. Initial empty caches are also never represented by a data frame.

Reproduced: consume the retry directive and first AAPL event, remove AAPL, then continue the generator through additional connection checks. Version stayed `(1, 1)` and no additional frame was emitted.

Recommendation: bump the version only on an actual removal and emit `data: {}` when the resulting snapshot is empty. Test partial and last-ticker removal without relying on another price update. Keep frontend removal behavior explicit; the new application-level state events are a separate requirement.

### 5. [P2] Handle snapshots without last trades and snapshot entitlement failures

Location: `backend/app/market/massive_client.py:103-106`, `:121-124`.

The current parser only accepts `last_trade`. Valid minute/day snapshot prices are skipped when the trade field is absent. The supplied current API reference documents those optional fields and plans that omit last trades; it also documents free Basic keys rejecting snapshots. A `NOT_AUTHORIZED` response currently just causes another snapshot attempt on the next cycle, with no EOD fallback. A configured API key therefore does not guarantee a usable data path for the documented plans.

Reproduced using a real `TickerSnapshot.from_dict` containing minute/day values but no last trade: no quote was cached. Two fake `BadResponse('{"status":"NOT_AUTHORIZED"}')` cycles made two snapshot attempts and left the cache empty. No live subscription or entitlement was tested.

Recommendation: implement the documented trade/minute/day extraction chain with the correct timestamp units, plus explicit entitlement handling and the planned grouped-daily fallback. Do not substitute yesterday's close as a live snapshot price. Add real-model tests for each fallback and fake-client tests for entitlement failures. This is known planned work, but it limits the current core's advertised real-data capability.

### 6. [P2] Ensure stopping during the initial poll actually stops the source

Location: `backend/app/market/massive_client.py:43-50`, `:57-65`.

`start()` awaits the first poll before assigning `_task`. If shutdown calls `stop()` during that wait, there is no task to cancel. The initial poll can subsequently publish a quote and `start()` creates a new polling task after `stop()` has returned. This violates the existing interface's promise that stopping ends cache writes and can leave a task running during teardown.

Reproduced with a blocked first fetch: call `start()` in a task, wait for the fetch to enter, await `stop()`, then release the response. AAPL appeared in the cache and a new polling task was running after stop. Ordinary start-then-stop tests do not exercise this interleaving.

Recommendation: put the immediate poll inside an owned background task before yielding control, and use explicit lifecycle state or a generation to prevent late startup from reviving a stopped source. Test stop during the first fetch as well as a later poll. The planned non-blocking startup addresses both this race and provider-dependent startup delay.

### 7. [P3] Make duplicate simulator subscriptions a true no-op

Location: `backend/app/market/simulator.py:242-248`; contract: `backend/app/market/interface.py`, `add_ticker()`.

`GBMSimulator.add_ticker()` ignores duplicates, but its source adapter still republishes the existing price. This bumps the cache version and replaces previous-tick change/direction even though no tick or subscription change occurred.

Reproduced without manually changing the cache: force one positive normal draw with shocks disabled, let the source perform one tick, then add the already tracked AAPL. The direction changed from `up` to `flat` and the version advanced by one.

Recommendation: return before publishing when the ticker is already tracked. Test the adapter's cache/version behavior, not only the math engine's ticker count.

## Required integration work still absent

These items are explicitly pending in PLAN section 6 and the current detailed design. They prevent treating this core as the complete current market contract:

| Requirement | Current implementation |
|---|---|
| Raw quote precision | Cache and simulator round to cents; model changes are also rounded. Two tests explicitly assert the older rounding behavior. |
| Source time versus receipt time | Only `timestamp` exists. No `quoted_at`, `fetched_at`, shared clock, or freshness predicate. |
| Fresh/stale/unavailable state | No quote-age classification or status transitions; provider failures only log errors. Cached values remain readable indefinitely. |
| Session identity and baseline | No session ID, retained baseline, or session performance fields. |
| Feed condition and restart/state notification | No source `feed_status`/`market_mode` contract or named SSE `feed_status` event. |
| Idle stream continuity | No 15-second heartbeat; unchanged or empty caches can leave the stream silent indefinitely. |
| Watchlist union positions and retry reconciliation | No market service, committed-state reader, or periodic reconciler exists yet. |
| Shared REST/SSE serializer and consistent snapshot | Only the legacy `PriceUpdate.to_dict()` and `get_all()` are present. |
| Deterministic test source | No scripted source or injected RNG/clock. Simulation uses separate global Python and NumPy RNGs. |

Database, trade service, full app lifecycle, frontend, and deployment work are outside the implemented market core; their absence is not counted as a market-code regression. Rejecting stale quotes inside a future trade transaction cannot yet be exercised end to end.

## Design and documentation cautions

1. **The proposed EOD polling code still exceeds the stated free-tier request budget.** `planning/MARKET_DATA_DESIGN.md:939-951` walks back over dates on every cycle; `:883` and section 16 call the 30-second interval two calls per minute. Two cycles on Sunday with data only on Friday actually issue six calls, before the snapshot probe or retries. Executed the actual proposed `latest_eod` function with a fake client: dates were Sunday, Saturday, Friday, then the same three again. UTC was substituted only for that request-count check because local IANA timezone data is absent; timezone correctness was not tested. Enforce a shared request budget and cache session discovery. A longer nominal interval alone is not a complete fix. The older API reference says 15 seconds and the interface says 60 seconds, so consolidate the schedule. The installed client defaults to three retries; the runtime source also still constructs it with that default.
2. **Resolve the chat-change notification contract before implementing it.** PLAN and the proposed stream use the highest inserted message sequence to detect chat changes, but finalizing/journaling an existing assistant row need not change that sequence. A persisted revision that advances on updates is needed for other tabs to notice completion. This is a future integration issue already discussed in `backend/planning/REVIEW.md` and the design's open questions; the root `planning/REVIEW.md` no longer contains the finding that the design references.
3. **Refresh completion and test claims.** `MARKET_DATA_SUMMARY.md` says all issues are resolved and reports 73 tests/84% coverage. The current checkout has 74 tests and measured 91% statement coverage, with the issues above still present. The archived statement that SSE testing requires a running server is incorrect: the real async generator and route callbacks can be tested directly, as this review did. The earlier timestamp-field bug is fixed in current source (`sip_timestamp / 1e9`), and real Massive model tests now protect that mapping. Build package discovery, top-level imports, public simulator ticker access, and the generator return annotation are also already fixed.

## Validation results

Used the existing `backend/.venv` directly; did not install or upgrade dependencies, load `.env`, or call external market services. Environment: Windows, Python **3.14.10**, pytest **9.0.2**, pytest-asyncio **1.3.0**, pytest-cov **7.0.0**, Massive **2.2.0**, FastAPI **0.128.7**, NumPy **2.4.2**, Ruff **0.15.0**. The planned Python 3.13 runtime was not separately tested.

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -v --cov=app --cov-report=term-missing
```

Result: **74 passed, 0 failed, 74 warnings**, in 1.66 seconds of pytest-reported test time. All warnings concern the test fixture's deprecated `asyncio.DefaultEventLoopPolicy`, slated for removal in Python 3.16. No tests were skipped.

| Test module | Passed |
|---|---:|
| `test_cache.py` | 13 |
| `test_factory.py` | 7 |
| `test_massive.py` | 14 |
| `test_models.py` | 11 |
| `test_simulator.py` | 19 |
| `test_simulator_source.py` | 10 |
| **Total** | **74** |

Measured statement coverage:

| Module | Coverage | Missing executable lines |
|---|---:|---|
| `cache.py`, `factory.py`, `interface.py`, `models.py`, `seed_prices.py`, package initializers | 100% | None |
| `massive_client.py` | 94% | 87-89, 128 |
| `simulator.py` | 98% | 149, 268-269 |
| `stream.py` | 33% | 26-48, 62-87 |
| **Overall** | **91%** | **31 of 350 statements** |

Coverage above is from the existing suite only. Subsequent diagnostic checks were separate and are not included in that percentage. Branch coverage was not measured. In particular, the actual `_fetch_snapshots()` request and recurring poll-loop body are not exercised by the suite, despite high provider-module statement coverage.

From the repository root:

```powershell
.\backend\.venv\Scripts\python.exe -m ruff check backend/app backend/tests backend/market_data_demo.py
```

Result: **All checks passed**. Lint was run without automatic fixes.

Additional checks against actual implementation code, using in-process scripts and fake provider responses:

- Reproduced all seven implementation findings above; no provider network calls or credentials were required.
- Four worker threads performed 1,000 updates each to one cache: final version 4,000 with one entry. Mutating the dictionary returned by `get_all()` did not mutate the cache.
- Seeded both global RNGs and ran 20,000 simulation steps with shocks disabled. Correlations of internal unrounded log returns were AAPL/MSFT **0.6054**, JPM/V **0.5070**, and AAPL/TSLA **0.3130**, each within 0.05 of the configured value.
- Constructed 50 tickers, including all ten defaults, verified the 50-by-50 Cholesky factor, and ran 10,000 steps with positive emitted prices. This is a bounded numerical check, not a guarantee against floating-point underflow over arbitrary runtimes.
- Injected a simulator step exception, then synchronized on the next successful step: the task survived and published the next price. The existing `test_exception_resilience` does not itself inject an exception; `test_custom_event_probability` also only starts/stops rather than checking shock magnitudes.
- Ran the terminal demo with `DURATION` temporarily set to 0.6 seconds in memory. It populated all ten prices, rendered the summary, and shut down cleanly; the source file was unchanged. Visual behavior in an interactive terminal was not assessed.
- Reproduced the proposed EOD request-count problem separately from runtime code, as described above. Loading `America/New_York` in the current environment fails without `tzdata`; the design already explicitly requires adding that dependency when implementing EOD support.

The focused scripts were review diagnostics, not committed regression tests. No live API, ASGI socket/browser, Docker image, clean dependency installation, or non-Windows runtime was exercised. No additional concrete credential-disclosure issue was found in the reviewed market source; credentials are not logged directly by these modules.

## Recommended next steps

1. Fix quote validation/precision, subscription and shutdown races, router isolation, and removal publication; add regression tests at each affected boundary.
2. Implement the already documented freshness/session/feed/reconciliation contract, correcting EOD budgeting and chat revision semantics before copying proposed code.
3. Add direct generator and route isolation tests, exercise the provider fetch and recurring loop with a fake client, and test failure recovery and missing tickers. Use synchronized events and injected clocks/RNGs instead of short timing assumptions.
4. Refresh the summary to distinguish the implemented core from integrated functionality, then validate the eventual Python 3.13/container runtime and full application flows when they exist.

The current tests and lint are green. Approval for trading integration should wait for the correctness fixes and required quote lifecycle behavior above.

## Resolution (2026-11-19)

All seven implementation findings were confirmed independently and fixed, in the order 1, 5, 2, 3, 4, 6, 7, each with regression tests. The suite is now **99 passed**, ruff is clean, and `app.market` coverage is 97% (`stream.py` 89%). Against the pre-fix code, all 18 new cache, simulator and stream tests fail. The new Massive tests cannot run against it because they import helpers that did not exist yet, so the in-flight removal test was checked by removing its guard instead; it fails without it.

Documentation cautions:
1. **EOD budget:** resolved in the code and docs. The session date is discovered once per day and cached, so later polls cost one call, and the client uses `retries=0`. The schedule is now 30 s in every doc. The code still polls every 60 s and uses the bar's `t` as `quoted_at`; both are listed as gaps in `MARKET_DATA_DESIGN.md` §10.2.
2. **Chat revision:** still open in PLAN (`MARKET_DATA_DESIGN.md` §20, question 1).
3. **Stale summary:** `MARKET_DATA_SUMMARY.md` has been rewritten to separate what is built from what is planned.

The required integration work in the table above is unchanged and still pending.
