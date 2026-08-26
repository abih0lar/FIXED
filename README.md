# ALADIN — Smart-Money-Concepts trading bot for MetaTrader 5

ALADIN is an algorithmic trading system for **MetaTrader 5 (Exness)** with a
licensed **PySide6 desktop control panel** that distributes as a single
Windows executable. The engine trades an SMC (Smart Money Concepts)
playbook — multi-timeframe structure, Fair Value Gaps, order blocks,
liquidity sweeps and a premium/discount matrix — behind a hard risk layer.

> ⚠️ **Disclaimer:** trading leveraged products carries substantial risk.
> This software is provided as-is, with no guarantee of profitability.
> Always forward-test on a **demo account** before enabling live sizing.

---

## Architecture

```
┌──────────────────────────  control plane  ──────────────────────────┐
│  app.py (PySide6 panel) ── manages → bot_watchdog.py ── supervises  │
│  license gate · broker tab · telegram tab · dashboard      │        │
└────────────────────────────────────────────────────────────┼────────┘
                                                             ▼
┌───────────────────────────  trading engine  ────────────────────────┐
│ main.py → Orchestrator (scan loop · position mgmt · reconciliation) │
│                                                     │               │
│   Scout ──signals──▶ RiskManager ──verdicts──▶ DataEngine (MT5 API) │
│     │                    │                  │                       │
│  Database (SQLite)   NewsFilter         Notifier (Telegram C2)      │
│  Scholar (optional RL/LLM auto-tune — OFF by default)               │
└─────────────────────────────────────────────────────────────────────┘
```

| Module | Role |
|---|---|
| `main.py` | Headless engine entry point (signal handling, log rotation) |
| `bot_watchdog.py` | Supervises the engine, restarts on crash, pidfile for the panel |
| `orchestrator.py` | The trading loop: scan → risk → execute → manage → reconcile. Writes `data/signals_log.csv` |
| `scout.py` | SMC signal engine: 4h/1h/15m/5m alignment, fractal structure, CHoCH, sweeps, FVGs, order blocks, liquidity targets |
| `risk_manager.py` | Daily-loss hard stop (incl. floating PnL), drawdown cap, confidence gates, spread guard, margin/risk caps, correlated-group cap |
| `data_engine.py` | MT5 wrapper (global lock, retries, indicators, order plumbing) |
| `news_filter.py` | ForexFactory weekly calendar — blocks entries around High-Impact events (fail-open) |
| `notifier.py` | Telegram alerts **and** remote commands (see below) |
| `scholar.py` | Q-learning + optional Gemini meta-review. **Ships OFF** (`SCHOLAR_AUTO_TUNE=false`) to match validated backtests |
| `database.py` | SQLite (WAL) — trades, scholar reviews, q-table, system flags |
| `app.py` | Desktop panel; runs the engine as a subprocess; embeds engine + env for single-exe builds |
| `keygen.py` | Offline Fernet license generator (operator tool) |
| `inject_env.py` / `embed_engine.py` | Build-time embedders for the distributable exe |

Runtime state lives in `data/` (logs, `.env`, signals CSV) and `db/`
(trading_bot.db). Both are **gitignored — never commit them**.

## Requirements

* Windows (the `MetaTrader5` Python API is Windows-only) + the **MT5 terminal
  installed and logged in** (Exness or any broker with MT5)
* Python **3.11+**
* `pip install -r requirements.txt`

## Setup

```bat
:: 1. create your local config (never committed)
mkdir data
copy .env.example data\.env
notepad data\.env        :: fill MT5_LOGIN / MT5_PASSWORD / MT5_SERVER

:: 2. pick a strong DRM secret (licenses are Fernet tokens of this key)
::    e.g.  python -c "import secrets; print(secrets.token_urlsafe(32))"
::    put it in data\.env as ALADIN_DRM_KEY=...

:: 3. run it
python main.py           :: headless engine
python app.py            :: or the desktop control panel
```

Key risk settings to review before going live (all in `data/.env`):
`MAX_DAILY_LOSS_PCT` / `MAX_DAILY_LIMIT`, `MAX_DRAWDOWN_PCT`,
`MAX_RISK_PER_TRADE_PCT`, `MAX_OPEN_POSITIONS`, `MIN_CONFIDENCE`,
`MIN_RR`, `MAX_GROUP_RISK_PCT`.

## Telegram remote control

Create a bot with @BotFather, put the token + your chat id in
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (only your chat id is authorized).

| Command | Action |
|---|---|
| `/status` | Live account snapshot on demand |
| `/close_all` | Panic-close every position |
| `/close_winners` | Close profitable positions only |
| `/close EURUSDm` | Close one symbol |
| `/pause` / `/resume` | Kill switch on/off |

## Building the distributable exe

```bat
build.bat
:: = python inject_env.py --embed-drm-key   (embeds settings + license key,
::   blanks YOUR mt5/telegram/gemini credentials)
:: = pyinstaller --onefile dist\ALADIN.exe
```

The buyer's exe validates licenses offline against the embedded key. After a
build, `app.py` temporarily contains the DRM key — **do not commit it**;
restore with `git checkout -- app.py`. See `SECURITY.md` for the full
secret-handling runbook.

## Tests

Two complementary layers:

**1. `pytest` — behavioural suite (runs anywhere, no broker needed).**
`tests/conftest.py` stubs the `MetaTrader5` SDK and feeds Scout/Risk fully
synthetic, deterministic OHLCV data:

```bash
pip install -r requirements-dev.txt
pytest                   # 45 tests: SMC detectors, scan pipeline, risk gates, DB
```

Covered: fractal pivots, structure (BOS/CHoCH/EMA-fallback), liquidity
sweeps, FVG detection & mitigation, order blocks, liquidity targets, the full
`scan()` pipeline (LONG/SHORT/awaiting/denials), every major risk gate
(daily loss, drawdown, kill switch, confidence, duplicates, spread, R:R,
margin black hole, group cap), volatility-scaled sizing, and the SQLite layer.

**2. `smoke_test.py` — zero-dependency import smoke test.**
Stubs every third-party dep in-process and imports all 13 modules, catching
import-time breakage (missing imports, NameErrors at class definition) even
on a bare Python:

```bash
python smoke_test.py .   # from the repo root
```

The `import app` check needs a GUI-capable environment (Windows with
`requirements.txt` installed, or Linux with PySide6 + libGL); all engine
checks run anywhere.

## Security notes

Secrets (`.env`, DRM key, license files) must never reach git. For the
rotation/purge runbook and build-time key handling, read **[SECURITY.md](SECURITY.md)**.
