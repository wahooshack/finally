# Review of changes since the last commit

Base: `14550e1` (`HEAD`). Reviewed tracked changes and untracked additions across the repository: both READMEs, `planning/PLAN.md`, the three new market-design documents, `.claude/settings.json`, and the review agent/command definitions. Existing review reports were treated as output artifacts and their claims independently checked.

No application source or tests changed. Findings concern the changed instructions, contracts, and proposed code examples. Paths in findings are relative to the repository root.

## Findings

### [P2] Use a separate revision for updates to existing chat messages

Location: `planning/PLAN.md:189`; related rules at lines 259, 286–289, and 553.

The feed defines `chat_sequence` as the highest committed message sequence and requires journal writes and finalization to raise it. However, sequences are allocated only on insertion. The user message and assistant placeholder are inserted before the provider call; action journaling and finalization update the existing assistant row.

If another tab loads the running placeholder, an analysis-only completion or provider failure changes neither the highest message sequence nor portfolio `state_version`. The tab gets no refresh signal and can display the running placeholder indefinitely. Action-bearing responses can similarly leave intermediate outcomes visible after finalization. `MARKET_INTERFACE.md` section 8 repeats this contract.

Add a persisted chat revision incremented atomically on insertion, action journaling, and finalization. Return it in both feed status and chat history, and use it for refresh and response ordering. Keep message sequence for display order. Verify second-tab updates after analysis-only completion, provider failure, and action finalization.

### [P2] Make the Quick Start reference runnable entry points

Location: `README.md:24–34`.

The revised Quick Start invokes `scripts/start_mac.sh` or `scripts/start_windows.ps1`, but neither exists. Its backend alternative starts `app.main:app`, but `backend/app/main.py` is absent too. Both paths also depend on the missing `.env.example`. A fresh checkout therefore cannot start using either documented path. The general development-status note does not identify these commands as future instructions.

Document the existing runnable market demo (`cd backend`, `uv sync --extra dev`, `uv run market_data_demo.py`) and test workflow. Label full-platform startup as planned until its prerequisites exist, or supply those prerequisites.

### [P2] Budget every EOD lookback request against the free-tier limit

Location: `planning/MASSIVE_API.md:180–190` and `planning/MASSIVE_API.md:250`.

The polling guidance says a 15-second Basic/EOD interval fits five calls per minute. However, `latest_eod()` makes a separate request for each date until it finds bars, repeating that lookback on every poll. On Monday, it requests Sunday, Saturday, and Friday each cycle: four cycles make 12 requests, before any snapshot entitlement probe or other traffic. Holidays can increase this further. Disabling client retries does not bound these explicit calls.

Executed the documented function with a fake client returning two empty dates followed by bars: four polling cycles issued 12 requests. `MARKET_INTERFACE.md` section 6 separately specifies a 60-second EOD interval, leaving implementers with conflicting instructions.

Specify one EOD schedule across both documents and enforce a shared request budget across lookback calls, entitlement detection, and retries. Cache the last successful session/date so every poll does not rediscover the same closed days. Verify weekend/holiday fallback against the rolling request limit.

### [P2] Discard poll results for subscriptions removed during the fetch

Location: `planning/MARKET_INTERFACE.md:263–280`.

The proposed `_poll_once()` captures the tracked ticker list, awaits the provider in a thread, then writes every returned snapshot without checking current subscriptions. While it awaits, `remove_ticker()` can remove a symbol and clear its quote. When the fetch completes, the loop restores that removed quote and can report a healthy feed using the old subscription set.

Reproduced this with the documented function and a paused fake fetch: removing AAPL during the await left AAPL back in the cache after completion. The periodic reconciler will not clear it because the source's subscription list already excludes AAPL. This violates the removal contract and retains obsolete cache entries; a stream built from all cached quotes can continue publishing them.

Before applying each response, check that its subscription is still current; use a subscription generation if removal followed by re-addition must also invalidate old responses. Derive coverage/status from the current desired set. Test removal and removal/re-addition while a fetch is in flight.

## Validation and limits

- Inspected the changed documents/configuration and relevant existing market source, cache, simulator constants, demo, project configuration, and installed Massive response models.
- Ran isolated reproductions of the EOD request count and in-flight removal race using functions extracted from the new documents, fake providers, and no network calls. Both reproduced the failures above.
- Confirmed the missing startup files and the presence of `backend/market_data_demo.py`.
- Confirmed the backend README's `--extra dev` change matches the optional dependency group in `pyproject.toml`.
- `git diff --check` passed, with line-ending conversion warnings only.
- No additional concrete security findings were identified in this diff. The EOD finding also concerns avoidable network traffic; the removal race concerns cache lifecycle practices. Unimplemented deployment controls cannot yet be runtime-verified.
- Application tests were not run because no application implementation changed. Existing market-source defects described by the new documents were not counted as regressions introduced by this diff.
- The earlier local report's review-hook finding no longer applies: the current `.claude/settings.json` contains plugin settings only.

Only this report was changed. Saved at `D:/projects/finally/backend/planning/REVIEW.md`, relative to the supplied working directory. The separate repository-level `planning/REVIEW.md` was left unchanged.
