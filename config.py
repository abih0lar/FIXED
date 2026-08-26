# --- START OF FILE config.py ---

import os
import logging
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

# FIX: load .env deterministically — prefer data/.env (canonical) with the
# legacy root .env only as fallback, and NEVER override env vars the parent
# process already set (e.g. app.py exports the correct absolute BOT_DB_PATH).
# The old `load_dotenv(override=True)` reloaded the CWD .env and clobbered
# that value, so the DB was created outside db/ on machines with a stale root
# .env.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_ENV = os.path.join(_BASE_DIR, "data", ".env")
_LEGACY_ENV = os.path.join(_BASE_DIR, ".env")
if os.path.exists(_DATA_ENV):
    load_dotenv(_DATA_ENV, override=False)
elif os.path.exists(_LEGACY_ENV):
    load_dotenv(_LEGACY_ENV, override=False)


def _env(key: str, default: str = "") -> str:
    val = os.environ.get(key, "")
    if val:
        return val.replace('"', '').replace("'", "").strip()
    return default


def _env_int(key: str, default: int = 0) -> int:
    try:
        val = os.environ.get(key, "")
        if val:
            return int(val.replace('"', '').replace("'", "").strip())
        return default
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        val = os.environ.get(key, "")
        if val:
            return float(val.replace('"', '').replace("'", "").strip())
        return default
    except (ValueError, TypeError):
        return default


def _env_db_path() -> str:
    """BOT_DB_PATH with a guaranteed db/ prefix. A bare filename (e.g. a stale
    'trading_bot.db' from an old .env) must NEVER land in the project root —
    normalize it into db/<name>."""
    val = _env("BOT_DB_PATH", "").strip()
    if not val:
        return "db/trading_bot.db"
    name = os.path.basename(val)
    d = os.path.dirname(val)
    if not d or d == ".":
        return os.path.join("db", name)
    return val


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "")
    if val:
        val = val.replace('"', '').replace("'", "").strip().lower()
        if val in ("true", "1", "yes"):
            return True
        if val in ("false", "0", "no"):
            return False
    return default


def _env_list(key: str, default: List[str] = None) -> List[str]:
    val = os.environ.get(key, "")
    if val:
        val = val.replace('"', '').replace("'", "").strip()
        return [s.strip() for s in val.split(",") if s.strip()]
    return default or []


def _env_pct_or_usd(key_pct: str, key_usd: str, default: str) -> str:
    """Daily-loss style value. Prefers a percentage key, normalizes it to 'X%'
    so RiskManager._parse_dynamic_limit treats it as a % of portfolio;
    falls back to a flat $ key."""
    val = _env(key_pct, "")
    if val:
        if "%" not in val:
            val = val + "%"
        return val
    return _env(key_usd, default)


@dataclass
class ExchangeConfig:
    exchange_id: str = field(default_factory=lambda: _env("EXCHANGE_ID", "exness").lower())

    # MT5 credentials
    mt5_login: int = field(default_factory=lambda: _env_int("MT5_LOGIN", 0))
    mt5_password: str = field(default_factory=lambda: _env("MT5_PASSWORD", ""))
    mt5_server: str = field(default_factory=lambda: _env("MT5_SERVER", ""))
    mt5_path: str = field(default_factory=lambda: _env("MT5_PATH", ""))

    # Trading Lists
    day_symbols: List[str] = field(default_factory=lambda: _env_list("DAY_SYMBOLS", ["EURUSDm", "GBPUSDm"]))
    night_symbols: List[str] = field(default_factory=lambda: _env_list("NIGHT_SYMBOLS", ["AUDJPYm", "CHFJPYm"]))

    @property
    def symbols(self) -> List[str]:
        # Dynamically merges both lists so DataEngine loads them into the broker Market Watch
        return list(set(self.day_symbols + self.night_symbols))

    api_key: str = ""
    api_secret: str = ""
    sandbox: bool = False

    market_type: str = field(default_factory=lambda: _env("EXCHANGE_MARKET_TYPE", "forex").lower())
    margin_mode: str = field(default_factory=lambda: _env("MARGIN_MODE", "hedging").lower())


@dataclass
class TimeframeConfig:
    primary: str = "1h"
    structural: str = "4h"


@dataclass
class DynamicConfig:
    ma_fast: int = 20
    ma_slow: int = 50
    rsi_length: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_length: int = 20
    bb_std: float = 2.0
    bb_touch_threshold: float = 0.05
    bb_upper_threshold: float = 0.95
    sl_atr_multiplier: float = 1.5
    sl_wick_buffer_pct: float = 0.15
    min_wick_ratio: float = 1.5
    min_wick_pct_of_range: float = 0.40
    wick_proximity_pct: float = 0.5
    pivot_lookback: int = 10
    pivot_cluster_pct: float = 0.5

    max_risk_per_trade_pct: float = field(default_factory=lambda: _env_float("MAX_RISK_PER_TRADE_PCT", 2.0))

    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 4))

    # Polymorphic Limits (Accepts either "20$" or "2%")
    # FIX: prefers MAX_DAILY_LOSS_PCT (e.g. '10.0' -> '10.0%') over MAX_DAILY_LIMIT,
    # so a percentage configured in .env is actually enforced instead of ignored.
    max_daily_loss: str = field(default_factory=lambda: _env_pct_or_usd("MAX_DAILY_LOSS_PCT", "MAX_DAILY_LIMIT", "20$"))
    daily_profit_target: str = field(default_factory=lambda: _env("DAILY_PROFIT_TARGET", "30$"))
    max_drawdown_pct: float = field(default_factory=lambda: _env_float("MAX_DRAWDOWN_PCT", 50.0))

    crypto_lot_multiplier: float = field(default_factory=lambda: _env_float("CRYPTO_LOT_MULTIPLIER", 1.0))
    forex_lot_multiplier: float = field(default_factory=lambda: _env_float("FOREX_LOT_MULTIPLIER", 1.0))

    max_position_size_usd: float = field(default_factory=lambda: _env_float("MAX_POSITION_SIZE_USD", 10000.0))
    min_confidence: float = field(default_factory=lambda: _env_float("MIN_CONFIDENCE", 0.60))
    max_slippage_pct: float = field(default_factory=lambda: _env_float("MAX_SLIPPAGE_PCT", 0.15))
    execution_window_start: int = field(default_factory=lambda: _env_int("EXECUTION_WINDOW_START", 55))
    execution_window_end: int = field(default_factory=lambda: _env_int("EXECUTION_WINDOW_END", 59))

    slippage_pct: float = 0.05
    taker_fee_pct: float = 0.055

    sl_max_pct: float = 2.0
    default_tp_rr: float = 2.5

    tsl_breakeven_r: float = 0.65
    # F3-FIX: half-risk SL trigger (profit in R at which SL moves to -0.5R).
    # TUNED: 0.4R is the backtest optimum (earlier = more scratch exits).
    tsl_half_risk_r: float = field(default_factory=lambda: _env_float("TSL_HALF_RISK_R", 0.4))
    atr_chandelier_multiplier: float = 3.0
    # F3-FIX: R-based chandelier trail distance (in R). The old formula trailed at
    # 3xATR while SL was 1.5xATR -> 2R of give-back; the chandelier never tightened
    # until +2R. R-based trailing at tsl_chandelier_r_mult x R (default 1.5R) actually
    # locks runners (a +2R extreme trails at +0.5R).
    tsl_chandelier_r_mult: float = field(default_factory=lambda: _env_float("TSL_CHANDELIER_R_MULT", 1.5))
    kelly_fraction: float = 0.50
    use_structural_filter: bool = True
    allow_short: bool = True

    # F2-FIX: minimum realized R:R a setup must offer before scout emits it AND
    # risk_manager approves it (replaces the hardcoded 2.0 gate). Liquidity-capped
    # TPs frequently fell below 2.0 -> the live pipeline executed ZERO trades in
    # 40 days across 4 instruments. Tunable; keep conservative (1.5) and forward-test.
    min_rr: float = field(default_factory=lambda: _env_float("MIN_RR", 1.5))
    # WIN-RATE lever: raw TP distance in R before liquidity capping.
    # TUNED by 90d/60d backtests: 1.5R (closer TP -> higher hit rate) beats 3.0R.
    tp_target_rr: float = field(default_factory=lambda: _env_float("TP_TARGET_RR", 1.5))
    # WIN-RATE lever: how far (in FVG-gap multiples) price may be from the FVG and
    # still count as a valid entry. TUNED to 2.0 (limit-order style near-gap entry).
    fvg_entry_tolerance: float = field(default_factory=lambda: _env_float("FVG_ENTRY_TOLERANCE", 2.0))
    # WIN-RATE lever: minimum FVG gap size as a fraction of price (forex base;
    # crypto uses 5x). Lower = more (smaller) FVGs accepted -> more trades.
    min_fvg_gap_pct: float = field(default_factory=lambda: _env_float("MIN_FVG_GAP_PCT", 0.0002))
    # WIN-RATE lever: minimum 4h ADX to take a trade (0 = off). Filters out chop —
    # SMC entries against weak/no HTF trend are the biggest loser in backtests.
    min_adx: float = field(default_factory=lambda: _env_float("MIN_ADX", 0.0))
    # F6-FIX: max combined risk (%) of all positions in the same currency group.
    # 5 correlated positions x 2% each = 10% = the daily loss limit -> one shock
    # halts the day. Default 8% allows ~4x2% correlated exposure.
    max_group_risk_pct: float = field(default_factory=lambda: _env_float("MAX_GROUP_RISK_PCT", 8.0))
    # F4-EXPOSE: SL buffer as ATR multiple (was hardcoded 0.75).
    sl_buffer_atr_mult: float = field(default_factory=lambda: _env_float("SL_BUFFER_ATR_MULT", 0.75))
    # F7-EXPOSE: toggle the premium/discount HTF zone filter for A/B testing
    # (True = current behaviour).
    htf_zone_filter: bool = field(default_factory=lambda: _env_bool("HTF_ZONE_FILTER", True))

    # ── CRYPTO-SPECIFIC TUNING (BTC/ETH/SOL/…). Coexists with the forex tuning
    #    above: crypto symbols resolve these values, forex symbols use the top ones.
    #    180d backtest (Feb-Aug 2026, 1h entries, 4h structure): pooled 62.5% WR,
    #    +0.18R, test PF 2.03. Momentum/breakout alternatives tested NEGATIVE —
    #    tuned SMC is the crypto method.
    crypto_min_confidence: float = field(default_factory=lambda: _env_float("CRYPTO_MIN_CONFIDENCE", 0.45))
    crypto_tp_target_rr: float = field(default_factory=lambda: _env_float("CRYPTO_TP_TARGET_RR", 1.5))
    crypto_fvg_entry_tolerance: float = field(default_factory=lambda: _env_float("CRYPTO_FVG_ENTRY_TOLERANCE", 2.0))
    crypto_min_fvg_gap_pct: float = field(default_factory=lambda: _env_float("CRYPTO_MIN_FVG_GAP_PCT", 0.0001))
    crypto_sl_buffer_atr_mult: float = field(default_factory=lambda: _env_float("CRYPTO_SL_BUFFER_ATR_MULT", 0.75))
    crypto_min_rr: float = field(default_factory=lambda: _env_float("CRYPTO_MIN_RR", 1.0))
    crypto_htf_zone_filter: bool = field(default_factory=lambda: _env_bool("CRYPTO_HTF_ZONE_FILTER", False))
    crypto_entry_tf: str = field(default_factory=lambda: _env("CRYPTO_ENTRY_TF", "1h"))
    # SNIPER: hours a pending limit order may rest before it's auto-cancelled
    # (prevents stale snipe setups clogging max_open_positions). 0 = never expire.
    limit_expiry_hours: float = field(default_factory=lambda: _env_float("LIMIT_EXPIRY_HOURS", 2.0))
    # MOMENTUM GUARD (pre-fill): cancel a resting snipe if price moves this far
    # (fraction of price) away from the limit — i.e. momentum left, the pullback
    # isn't coming. 0.0 disables. Covers "order not yet filled" scenario.
    pending_cancel_distance_pct: float = field(default_factory=lambda: _env_float("PENDING_CANCEL_DISTANCE_PCT", 0.3))
    # MOMENTUM GUARD (post-fill): tighten SL to breakeven if the fast EMA flips
    # against the trade once profit >= this (in R). Covers "order filled, momentum
    # reversed" scenario before the price-based trail catches up.
    momentum_be_r: float = field(default_factory=lambda: _env_float("MOMENTUM_BE_R", 0.5))
    # POST-LOSS COOLDOWN: hours a symbol is barred from NEW entries after a losing
    # close (prevents revenge-trading / churn after a loss). 180d experiment: 2h
    # and 4h are statistically identical in EV, but 2h blocks ~40% fewer setups.
    cooldown_hours: float = field(default_factory=lambda: _env_float("COOLDOWN_HOURS", 2.0))

    # ECONOMIC-CALENDAR FILTER: when enabled, the bot refuses NEW entries on a
    # symbol whose currencies have a High-Impact event within the block window
    # (ForexFactory weekly feed, cached 4h, fail-open if the feed is unreachable).
    news_filter_enabled: bool = field(default_factory=lambda: _env_bool("NEWS_FILTER_ENABLED", True))
    news_block_before_mins: int = field(default_factory=lambda: _env_int("NEWS_BLOCK_BEFORE_MINS", 15))
    news_block_after_mins: int = field(default_factory=lambda: _env_int("NEWS_BLOCK_AFTER_MINS", 30))

    # CRYPTO MARKET-ENTRY FALLBACK: when crypto confidence >= this, skip the
    # pending limit (which often never fills) and enter at market. 180d backtest:
    # conf>=0.65 -> +19% fills, WR 62->67%, avgR +0.20 (unchanged), PF 1.72.
    # Below the threshold, keep the SMC limit order. 0.0 disables the fallback.
    crypto_market_entry_conf: float = field(default_factory=lambda: _env_float("CRYPTO_MARKET_ENTRY_CONF", 0.65))

    # ── METALS-SPECIFIC TUNING (XAU/XAG/XPT/XPD — gold, silver, platinum…).
    #    60d/15m backtest (Jun-Aug 2026): 17 trades, 71% WR, +0.31R, PF 2.74
    #    in-sample; +0.06R out-of-sample; survives ~$0.35 spread (+0.22R/PF 2.09).
    #    NOTE: confidence is NOT predictive for metals — no confidence gate.
    #    EXPERIMENTAL — forward-test on demo before sizing.
    metal_min_confidence: float = field(default_factory=lambda: _env_float("METAL_MIN_CONFIDENCE", 0.35))
    metal_tp_target_rr: float = field(default_factory=lambda: _env_float("METAL_TP_TARGET_RR", 2.0))
    metal_fvg_entry_tolerance: float = field(default_factory=lambda: _env_float("METAL_FVG_ENTRY_TOLERANCE", 3.0))
    metal_min_fvg_gap_pct: float = field(default_factory=lambda: _env_float("METAL_MIN_FVG_GAP_PCT", 0.0001))
    metal_sl_buffer_atr_mult: float = field(default_factory=lambda: _env_float("METAL_SL_BUFFER_ATR_MULT", 0.75))
    metal_min_rr: float = field(default_factory=lambda: _env_float("METAL_MIN_RR", 1.0))
    metal_htf_zone_filter: bool = field(default_factory=lambda: _env_bool("METAL_HTF_ZONE_FILTER", False))

    learning_rate: float = 0.1
    discount_factor: float = 0.9
    exploration_rate: float = 0.2
    scholar_trigger_trades: int = 5
    # SCHOLAR AUTO-TUNE: the RL meta-optimizer adjusts live parameters after a
    # few closed trades. The profitable backtests ran with this OFF (the worker
    # was never exercised), so it ships OFF to match the validated strategy;
    # friends can opt in. When off, Scholar still records insights but never
    # mutates live parameters.
    scholar_auto_tune: bool = field(default_factory=lambda: _env_bool("SCHOLAR_AUTO_TUNE", False))


@dataclass
class AIConfig:
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-1.5-flash"))
    gemini_temperature: float = field(default_factory=lambda: _env_float("GEMINI_TEMPERATURE", 0.7))
    gemini_max_tokens: int = field(default_factory=lambda: _env_int("GEMINI_MAX_TOKENS", 2048))


@dataclass
class NotificationConfig:
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))
    notify_debug: bool = field(default_factory=lambda: _env_bool("NOTIFY_DEBUG", False))


@dataclass
class BotConfig:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    timeframes: TimeframeConfig = field(default_factory=TimeframeConfig)
    dynamic: DynamicConfig = field(default_factory=DynamicConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    db_path: str = field(default_factory=_env_db_path)


BOT_CONFIG = BotConfig()
cfg = BOT_CONFIG.exchange
if not cfg.mt5_login or not cfg.mt5_password:
    logging.critical("MT5 credentials not found. Check the .env file.")