"""Test fixtures for the ALADIN engine.

The `MetaTrader5` package is Windows-only and needs a live broker
terminal, so conftest installs a deterministic stub into sys.modules
BEFORE any project module is imported. Everything else (config,
database, scout, risk_manager) runs for real against tmp SQLite files
and synthetic OHLCV frames.
"""
import os
import sys
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

# ── MetaTrader5 stub ──────────────────────────────────────────────────────
# Must happen before `import data_engine` / `import risk_manager`.
_MT5 = MagicMock(name="MetaTrader5_stub")
# constants referenced at data_engine import time (TF_MAP)
for _i, _name in enumerate(
    ("TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30",
     "TIMEFRAME_H1", "TIMEFRAME_H4", "TIMEFRAME_D1"), start=1
):
    setattr(_MT5, _name, _i)
_MT5.ORDER_TYPE_BUY = 0
_MT5.ORDER_TYPE_SELL = 1
sys.modules["MetaTrader5"] = _MT5

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Broker defaults ───────────────────────────────────────────────────────
DEFAULT_SYMBOL_INFO = SimpleNamespace(
    volume_min=0.01, volume_step=0.01, volume_max=100.0,
    trade_tick_size=0.00001, trade_tick_value=1.0, point=0.00001,
    name="EURUSDm", currency_profit="USD",
)
DEFAULT_TICK = SimpleNamespace(ask=1.10005, bid=1.10000, time=0)


def configure_broker(positions=(), orders=(), symbol_info=None, tick=None,
                     margin=100.0, leverage=100):
    """Point the stubbed MT5 API at deterministic per-test state."""
    _MT5.positions_get = lambda symbol=None, **kw: list(positions)
    _MT5.orders_get = lambda symbol=None, **kw: list(orders)
    _MT5.symbol_info = lambda symbol=None, **kw: symbol_info or DEFAULT_SYMBOL_INFO
    _MT5.symbol_info_tick = lambda symbol=None, **kw: tick or DEFAULT_TICK
    _MT5.order_calc_margin = lambda *a, **kw: margin
    _MT5.account_info = lambda **kw: SimpleNamespace(
        leverage=leverage, balance=10000.0, equity=10000.0)


@pytest.fixture(autouse=True)
def _broker_defaults(monkeypatch):
    """Every test starts from a clean, empty broker and a known market type,
    regardless of what a developer's local data/.env contains."""
    from config import BOT_CONFIG
    monkeypatch.setattr(BOT_CONFIG.exchange, "market_type", "forex")
    configure_broker()
    yield


@pytest.fixture
def dyn():
    """The live DynamicConfig — tests monkeypatch fields on it for determinism."""
    from config import BOT_CONFIG
    return BOT_CONFIG.dynamic


@pytest.fixture
def db(tmp_path):
    """Real SQLite database, isolated per test."""
    from database import Database
    return Database(str(tmp_path / "test_trading_bot.db"))


# ── Synthetic OHLCV helpers ───────────────────────────────────────────────

def _df(opens, highs, lows, closes):
    idx = pd.date_range("2026-08-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": [1000.0] * len(closes)},
        index=idx,
    )


def candles_from_closes(closes, wick=0.0002):
    """Simple series: open = previous close, fixed wick padding."""
    opens = [closes[0]] + list(closes[:-1])
    highs = [c + wick for c in closes]
    lows = [c - wick for c in closes]
    return _df(opens, highs, lows, closes)


def zigzag_df(n_legs=10, leg_len=12, up_leg=0.0040, down_leg=0.0030,
              start=1.0000, start_dir=1, wick=0.0002):
    """Deterministic trending series: alternating legs creating verified
    fractal pivots. start_dir=+1 with up_leg>down_leg -> bullish (HH+HL);
    start_dir=-1 with down_leg>up_leg -> bearish (LH+LL).
    Bars per leg must be >= 2*fractal_window+1 so pivots confirm."""
    closes, price, direction = [], start, start_dir
    for _ in range(n_legs):
        step = up_leg if direction > 0 else -down_leg
        for _b in range(leg_len):
            price += step / leg_len
            closes.append(round(price, 6))
        direction *= -1
    return candles_from_closes(closes, wick=wick)


def rising_df(n=120, start=1.0500, end=1.0962, wick=0.0002):
    """Strictly increasing series: no fractal pivots (=> no liquidity
    targets, EMA-fallback bullish structure)."""
    closes = list(np.linspace(start, end, n))
    return candles_from_closes(closes, wick=wick)


def fvg_tail_df(base=1.0900, rise_to=1.0944, wick=0.0002):
    """Rising series (118 bars) whose final four bars contain exactly one
    dominant clean bullish FVG:
        c[-4].high = 1.0950 (gap bottom, c1)
        c[-3]      = displacement candle (c2)
        c[-2].low  = 1.0960 (gap top, c3)
        c[-1]      = last bar, stays above the 50% mitigation line (1.0955)
    find_fvg returns (1.0960, 1.0950).
    """
    closes = list(np.linspace(base, rise_to, 118))
    closes += [1.0948, 1.0962, 1.0962, 1.0962]          # c1, c2, c3, last
    df = candles_from_closes(closes, wick=wick)
    h, l = df.columns.get_loc("high"), df.columns.get_loc("low")
    df.iloc[-4, h] = 1.0950                              # c1.high = gap bottom
    df.iloc[-3, l] = 1.0958                              # c2 wick inside gap
    df.iloc[-2, l] = 1.0960                              # c3.low  = gap top
    df.iloc[-1, l] = 1.0959                              # above mitigation line
    return df


def flat_df(n=120, price=1.1000, jitter=0.0004, seed=7, wick=0.0004):
    """Mean-reverting noise around `price` (no clean structure, no FVGs)."""
    rng = np.random.default_rng(seed)
    closes = list(price + np.cumsum(rng.uniform(-jitter, jitter, n)) * 0.1)
    return candles_from_closes(closes, wick=wick)


# ── Fake DataEngine ───────────────────────────────────────────────────────

class FakeDataEngine:
    """Stands in for DataEngine in Scout/Risk pipelines (no MT5 at all)."""

    def __init__(self, frames=None, snapshot=None):
        self.lock = threading.RLock()
        self.frames = frames or {}
        self.snapshot = snapshot
        self._market_info = {}
        self.floating_pnl = 0.0

    def fetch_ohlcv(self, symbol, timeframe, limit=200):
        if timeframe not in self.frames:
            raise ValueError(f"No data for {symbol} {timeframe}")
        return self.frames[timeframe].iloc[-limit:].copy()

    def build_snapshot(self, symbol):
        return self.snapshot

    def get_total_floating_pnl(self):
        return self.floating_pnl

    def get_account_balance(self):
        return 10000.0


def make_snapshot(price=1.0962, atr=0.0008):
    from models import IndicatorSnapshot
    return IndicatorSnapshot(
        timestamp=datetime.now(timezone.utc), price=price,
        rsi=55.0, macd_line=0.0010, macd_signal=0.0005,
        macd_histogram=0.0005, bb_upper=price * 1.002, bb_middle=price,
        bb_lower=price * 0.998, bb_pct_b=0.5, ma20=price * 0.999,
        ma50=price * 0.998, atr=atr, volume_current=1000.0,
        volume_avg=1000.0, volume_ratio=1.0,
    )


@pytest.fixture
def fake_engine():
    def _make(frames=None, snapshot=None):
        return FakeDataEngine(frames=frames, snapshot=snapshot or make_snapshot())
    return _make
