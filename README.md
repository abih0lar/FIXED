# ALADIN — Institutional Multi-Asset Trading Bot (MT5 / Exness)

ALADIN is a Windows desktop trading system for MetaTrader 5 (Exness) that hunts
**Smart Money Concepts (SMC)** setups — HTF bias → liquidity sweep → FVG
pullback entry — with an institutional-style risk manager, a Qt control
panel, Telegram remote control, economic-calendar filtering, and a Gemini-powered
"Scholar" review agent.

The panel never trades. The bot engine is a separate process, supervised by a
watchdog, and every entry passes a layered risk gate before it reaches the broker.

---

## ⚠️ Disclaimer

Trading leveraged FX / crypto / metals carries substantial risk of loss.
Backtests ≠ future results. Run on a **demo account** until you have weeks of
forward-test evidence, and never risk money you cannot afford to lose.

---

## Architecture

```
┌────────────┐   spawn    ┌────────────┐   spawn    ┌────────────────────┐
│  app.py    │──────────▶│ bot_watch- │──────────▶│ main.py            │
│ Qt panel   │           │ dog.py     │           │  └─ Orchestrator   │
│ (no trades)│           │ (restart+  │           │     ├─ Scout       │  scan & signal
└────────────┘           │  backoff)  │           │     ├─ RiskManager │  approve & size
                         └────────────┘           │     ├─ DataEngine  │  MT5 gateway
                                                  │     ├─ NewsFilter  │  calendar blocks
                                                  │     ├─ Notifier    │  toast/Telegram C2
                                                  │     └─ Scholar     │  review/meta-tune
                                                  └─────────┬──────────┘
                                            SQLite (db/)  Telegram   Gemini API
```

| File | Role |
|---|---|
| `app.py` | PySide6 control panel: license gate, broker/strategy/Telegram tabs, live log, signals dashboard. Launches/stops the bot; never trades itself. |
| `bot_watchdog.py` | Keeps `main.py` alive: restarts on crash with exponential backoff, gives up after 8 rapid failures. |
| `main.py` | Headless bot entry point (signal handlers, rotating logs). |
| `orchestrator.py` | The loop: session windows, scans, conviction reversals, pending-limit lifecycle, trailing stops, broker↔DB reconciliation. |
| `scout.py` | SMC analysis: fractal market structure (BOS/CHoCH), liquidity sweeps, FVG + order blocks, premium/discount zones, confluence confidence. |
| `risk_manager.py` | The veto layer: daily-loss limit (incl. floating PnL), drawdown vs peak equity, correlated-group cap, margin clamp, min-lot guard, lot sizing. |
| `data_engine.py` | All MT5 I/O behind one lock: candles, ticks, orders, positions, indicators (via `ta`). |
| `news_filter.py` | ForexFactory high-impact calendar; blocks new entries in the blast window. Fails open. |
| `notifier.py` | Windows toasts + Telegram notifications **and remote control** (`/pause`, `/close_all`, …). |
| `scholar.py` | Post-trade reviews (Gemini) + opt-in Q-learning meta-tuner (**off by default**). |
| `database.py` | SQLite (WAL, thread-local connections): trades, monologue, scholar reviews, Q-table, flags. |
| `config.py` | Every knob, loaded from `data/.env` (root `.env` as legacy fallback). |
| `keygen.py` | License generator (Fernet, signed with `ALADIN_DRM_KEY`). |
| `inject_env.py` / `build.bat` | Build pipeline: sanitized `.env` + DRM secret stamped into the exe. |
| `smoke_test.py` | Import-level test suite for every module (stubs unavailable deps — runs anywhere). |

---

## Requirements

- **Windows** (the `MetaTrader5` package is Windows-only) with an installed
  **MT5 terminal** (Exness or other broker)
- **Python 3.10+**
- Dependencies: `pip install -r requirements.txt`

## Quickstart

```bat
git clone https://github.com/abih0lar/FIXED.git
cd FIXED
pip install -r requirements.txt

:: create data\.env (see Configuration below) with your MT5 credentials,
:: then either run the panel:
python app.py

:: or run the bot headless:
python main.py            :: single process
python bot_watchdog.py    :: auto-restart supervision (recommended)
```

On first launch the panel writes a default `.env` into `data/`, creates
`data/` and `db/`, and materializes its UI assets. Enter your MT5 login in the
**Broker** tab and a license key in the **License** tab, then press **START**.

## Configuration (`data/.env`)

Only the essentials are listed — `config.py` documents every key.

### Broker

| Key | Default | Notes |
|---|---|---|
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | — | Your Exness MT5 account. |
| `MT5_PATH` | auto | Path to `terminal64.exe` if not auto-detected. |
| `EXCHANGE_MARKET_TYPE` | `forex` | `forex` \| `crypto` \| `synthetic` (changes sessions & spreads). |
| `DAY_SYMBOLS` / `NIGHT_SYMBOLS` | `EURUSDm, GBPUSDm` / `AUDJPYm, CHFJPYm` | WAT-session symbol rotation; `m` = Exness mini suffix. |

### Risk (the part that keeps you alive)

| Key | Default | Notes |
|---|---|---|
| `MAX_RISK_PER_TRADE_PCT` | `2.0` | % of equity risked per trade (SL-based lot sizing). |
| `MAX_DAILY_LOSS_PCT` | `10.0` | Day stop as % of equity (prefers this) or `MAX_DAILY_LIMIT` in `$`. Includes floating PnL. |
| `MAX_DRAWDOWN_PCT` | `50.0` | Halt vs. stored peak equity. |
| `MAX_OPEN_POSITIONS` | `4` | Concurrent positions (DB **and** broker-truth checked). |
| `MAX_GROUP_RISK_PCT` | `8.0` | Cap on correlated currency-group exposure. |

### Strategy (SMC tuning)

| Key | Default | Notes |
|---|---|---|
| `MIN_CONFIDENCE` | `0.60` | Forex confidence gate (crypto/metals have their own). |
| `MIN_RR` | `1.5` | Minimum realized R:R after liquidity-cap. |
| `TP_TARGET_RR` | `1.5` | Raw TP distance in R before capping. |
| `MIN_ADX` | `0` (off) | 4h ADX chop filter, e.g. `25` to require real trends. |
| `LIMIT_EXPIRY_HOURS` | `2` | Auto-cancel unfilled snipe limits. |
| `COOLDOWN_HOURS` | `2` | Per-symbol new-entry block after a losing close. |
| `NEWS_FILTER_ENABLED` | `true` | Block entries around high-impact events (±15/30 min). |

Crypto (`CRYPTO_*`) and metals (`METAL_*`) have parallel knob sets
(`CRYPTO_MIN_CONFIDENCE`, `CRYPTO_TP_TARGET_RR`, `METAL_MIN_RR`, …) so each
asset class keeps its own backtested tuning.

### Integrations

| Key | Notes |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Enables toasts→Telegram + remote commands. |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Powers Scholar reviews (`gemini-1.5-flash`). |
| `SCHOLAR_AUTO_TUNE` | `false` — RL parameter mutation ships OFF (matches validated backtests). |
| `ALADIN_DRM_KEY` | License signing secret. **Never commit.** |
| `LICENSE_KEY` | Your Fernet license (validated on panel start). |

---

## Telegram remote control

With a bot token configured, admins in the chat can send:

| Command | Action |
|---|---|
| `/status` | Live account + position snapshot |
| `/close_all` | Panic-close everything |
| `/close_winners` | Secure profitable positions |
| `/close <SYMBOL>` | Close one symbol's position (e.g. `/close eurusdm`) |
| `/pause` / `/resume` | Kill switch — blocks **new** entries (open positions keep managing) |

## Data & logs

| Path | Contents |
|---|---|
| `data/.env` | Live config (root `.env` migrated automatically) |
| `db/trading_bot.db` | SQLite: trades, monologue, reviews, Q-table, flags (WAL mode) |
| `data/bot.log` | Bot log (rotating, 5 MB × 5) |
| `data/watchdog.log` / `data/bot_stderr.log` | Supervision + crash output |
| `signals_log.csv` | Every signal emitted (feeds the dashboard stats) |

---

## Building the distributable exe

`build.bat` produces `dist/ALADIN.exe` (PyInstaller onefile):

1. `inject_env.py` embeds a **sanitized** `.env` — all strategy tuning kept,
   every credential blanked (MT5, Telegram, Gemini, DRM key, license).
2. Your real `ALADIN_DRM_KEY` is stamped into the exe's license validator so
   buyers' keys validate offline. The build **aborts** if the key is missing or
   is the old leaked secret — rotate before building.
3. `dist/` and `build/` are git-ignored; never commit binaries.

> The exe bundles the engine as extractable bytecode. It deters casual copying,
> it does not stop a determined reverser.

### Issuing licenses

```bat
python keygen.py
:: → CLIENT_LICENSE_KEY.txt (valid N days, signed with ALADIN_DRM_KEY)
```

Rotating the secret invalidates all old keys: set a new `ALADIN_DRM_KEY`,
re-run `keygen.py`, rebuild.

## Testing

```bash
python smoke_test.py .   # imports every module (deps stubbed), runs small checks
```

Runs on any OS — MetaTrader5/PySide6/numpy/etc. are stubbed, so it catches
import-time and wiring regressions, not broker behaviour. Live behaviour must
be forward-tested on demo.

## Security notes

- No secrets are committed: `.env`, `data/`, `db/`, `dist/`, `build/` are all
  git-ignored. The DRM secret enters the exe only at build time.
- If a secret ever lands in git history, **rotate it** — history is forever.
- License clock is fetched over HTTPS with a cached offline fallback.
