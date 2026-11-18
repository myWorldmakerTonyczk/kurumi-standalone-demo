# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Chinese-language, single-process AI paper-trading workspace: a persona-driven agent ("久留美") reads FX quotes, places virtual trades, writes journals/diaries, patrols a forum and group chats, and keeps long-term memory. Backend is Python (aiohttp + LangChain + SQLite); the frontend is plain HTML/CSS/JS with no build step. All UI copy, prompts, error messages, and code comments are Chinese — keep them that way.

## Commands

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# Run (offline demo mode). Without KURUMI_LIVE=0 it uses real quote sources.
KURUMI_LIVE=0 .venv/Scripts/python.exe app.py

# Tests (78 tests, ~45s). unittest, no pytest/lint config in repo.
.venv/Scripts/python.exe -m unittest discover -s tests -q

# Single test file / single test
.venv/Scripts/python.exe -m unittest discover -s tests -p "test_engine.py" -q
.venv/Scripts/python.exe -m unittest discover -s tests -p "test_engine.py" -k test_limit_orders_fill_at_favorable_quotes_only_once -q
```

Ports: admin `8790` (`KURUMI_ADMIN_PORT`), user `8791` (`KURUMI_USER_PORT`), both bound to `127.0.0.1`. Env vars: `KURUMI_LIVE` (`0` = offline, fixed EUR/USD quotes + deterministic replies when no model configured), `KURUMI_DATA` (data dir, default `./data`), `KURUMI_MODEL_URLS` (comma-separated exact-match allowlist for custom model endpoints), `KURUMI_PUBLIC_URL` (HTTPS public origin enables Secure cookies). Env is read at startup only; no `.env` loading.

## Architecture

### Process and servers

`app.py:main()` runs one asyncio process that starts **two distinct aiohttp applications** from `Demo.app(admin: bool)` — an admin app on 8790 and a user app on 8791. They share the same `Demo` instance and DB but have different auth boundaries (admin bearer token vs. `kurumi_session` cookie). `src/portal/server.py` holds the portal handlers/`Portal` class; in this standalone build the portal is always served by the 8791 app (portal settings are pinned to `127.0.0.1:8791`), and `Portal`'s own listener code is a leftover from the AstrBot plugin origin.

- `Demo` (`app.py`) is the composition root: it owns `Engine` (ledger), accounts/sessions, `AgentTimeline`, `TaskSchedule`, `Social` (patrol scheduler), `Community`, `AgentInbox`, `Memory`, `ImageLibrary`, `MarketBulletin`, and `ExternalAgent`.
- `dashboard_api.py:Dashboard` is the HTTP adapter for admin and legacy endpoints; it also constructs the `Portal` used by the user server.
- The user workspace (`/workspace/`) serves `views/dashboard/index.html` with `<body data-remote-workspace>` and talks to the same `/api/*` routes.

### Data layer

Everything is one SQLite file (`data/ledger.sqlite3`; admin credentials in `data/admin.json`). `src/core/engine.py:Engine` owns trading tables; game/account state is largely a JSON blob in the `games.data` column, and `demo_profiles.data` holds per-owner settings. Schema upgrades are done in place in constructors (e.g. `Demo.__init__`, `TaskSchedule.__init__`) via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` instead of a migration framework — follow that pattern for new columns. Timestamps are `time.time()` floats.

### Trading path

Agent tools and HTTP trading commands both funnel into `src/core/trading.py:execute()` → `Engine.action()`, which validates against current quotes and writes the ledger/journal. `src/agent/mcp/trading/execution.py:bind_trade` wraps trade tools per agent run: it rejects writes if `action_revision` changed mid-run, dedupes `observe`, and tracks `watch_actions`. Quotes refresh every 5 s in `Demo.prices()`; `Engine.tick()` settles limit orders, stops, and liquidations.

### Agent pipeline

`Demo.agent()` → `AgentTimeline.run()` → `Demo._agent()`:

1. `AgentTimeline` serializes all work per owner into task kinds (`private`, `watch`, `patrol`, `diary`, `trade_review`, …) and records traces/timeline entries. `_agent` additionally takes a per-game lock.
2. `_agent` builds the persona (profile + `config/default_persona.md`) and a system prompt by concatenating `workflow(name)` snippets from `config/workflow.json`, the account snapshot, recalled memory, and tool instructions.
3. It selects the model via `src/agent/model_registry.py`, wraps it with middleware (`TraceTools`, `cap_context`, optional retry/fallback), and calls LangChain `create_agent(...)`. LangChain/LangChain-OpenAI versions are pinned in `requirements.txt`; the agent API is consumed as-is, not abstracted.
4. Results are persisted to `demo_chat`, attachments, timeline references, and (for private chat) queued into async memory extraction.

### Tool system (on-demand groups)

Tools live in `src/agent/mcp/<group>/<tool>.py`; each module exposes a `build(...)` factory returning the tool callable (see `trading/open_trade.py`). The tool surface is assembled **per scenario** in `src/agent/mcp/registry.py` (`user_workspace_groups`, `user_group_groups`, `user_patrol_groups`) depending on writability, attachments, images, memory settings, and external-agent mode.

At runtime the model sees only its `INITIAL_TOOLS` (for on-demand task kinds: those plus `load_tool_group`, which reveals the rest):

- `src/agent/mcp/task_tools.py:INITIAL_TOOLS` defines first-call tools per task kind; `ON_DEMAND_TASKS` get the `ToolGroups` middleware.
- `ToolGroups` (`load_tool_group.py`) filters bound tools by loaded groups and rejects calls to unloaded tools.
- `CATALOG` in `load_tool_group.py` is the human-readable group description shown to the model.

Checklist for a new agent tool: create the module with `build()`, wire it into the right group in `registry.py`, add it to `INITIAL_TOOLS` only if it must be callable before loading a group, and extend `tests/test_tool_groups.py`.

### Background loops

Started in `main()`: `prices()` (5 s quote refresh + settlement), `jobs()` (2 s: task appointments via `TaskSchedule.tick`, auto-watch decisions, diary scheduling), `social.patrol()` (15 s community patrol scheduler), `market_bulletin.loop()`. Async memory extraction (`Memory.start`) runs in background tasks per chat stream; vision (`describe_image`) and voice (`voice_reply`) are awaited inline inside `_agent`. `TaskSchedule` also turns ledger events (stop-loss, liquidation, deposit…) into owner wake-ups.

### External agent mode

`src/external_agent/api.py`: when a profile's `agent_mode` is `external`, clients use `/api/external-tools` with a `kext_…` bearer token. This exposes a deliberately small, hard-coded tool surface (`quotes`, `history`, `chart`, optional `write_diary`) with per-tool JSON schemas. It is separate from the LangChain platform-agent tool groups — don't mix the two paths.

### Frontend

`views/dashboard/` is served without a build step. New JS/CSS must be added to the allowlists: `ASSETS`/route list in `app.py:app()` for the admin/user root server, `ASSETS`/`WORKSPACE_FILES` in `src/portal/server.py` for the portal. A strict CSP is applied to every response; keep client code in external modules (no inline scripts). Trading charts use the vendored TradingView lightweight-charts.

## Conventions

- Raise `ValueError("中文提示")` for user-facing validation failures — the middleware maps it to HTTP 400 `{"error": ...}`. Unexpected model/network errors become 502 with an incident log ID.
- Never echo saved model API keys to the browser; they are stored unencrypted in SQLite by design (documented in README).
- Model endpoint URLs must exactly match the built-in presets or `KURUMI_MODEL_URLS` entries (including trailing slash); HTTPS required except loopback.
- Tests construct a fresh `Demo(tempfile path)`, mock the LLM either by patching `app.model_client` (LangChain fake model) or by running a local OpenAI-compatible aiohttp server and adding its URL to `KURUMI_MODEL_URLS`; async tests use `unittest.IsolatedAsyncioTestCase`. `Demo.__init__` reads `KURUMI_LIVE`, so patch env before constructing it.
- Offline demo mode (`KURUMI_LIVE=0`) short-circuits agent behavior in `_agent` with deterministic replies/trades when no model is configured — keep that branch working when changing agent flow.
 

远端服务器ip：47.95.233.156
用户名root 密码Xab1212dadgfgd