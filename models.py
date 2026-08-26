# --- START OF FILE models.py ---

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class Bias(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class SignalStrength(Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    NONE = "NONE"


class TradeStatus(Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


@dataclass
class IndicatorSnapshot:
    timestamp: datetime
    price: float
    rsi: float
    macd_line: float
    macd_signal: float
    macd_histogram: float
    bb_upper: float
    bb_middle: float
    bb_lower: float
    bb_pct_b: float
    ma20: float
    ma50: float
    atr: float
    volume_current: float
    volume_avg: float
    volume_ratio: float


@dataclass
class WickRejection:
    timestamp: datetime
    direction: str
    price: float
    wick_length: float
    body_length: float
    wick_ratio: float
    wick_pct_of_range: float
    rejected_level: float
    level_type: str
    candle_low: float = 0.0
    candle_high: float = 0.0


@dataclass
class SupportResistance:
    level: float
    strength: int
    level_type: str
    last_tested: datetime


@dataclass
class ScoutSignal:
    timestamp: datetime
    symbol: str
    bias: Bias
    strength: SignalStrength
    confidence: float
    entry_price: float
    sniper_entry_price: float
    indicators: IndicatorSnapshot
    nearest_support: float
    nearest_resistance: float
    wick_rejection: Optional[WickRejection]
    patterns_detected: List[str]
    reasoning: str
    confidence_breakdown: Dict[str, float]
    suggested_sl: float
    suggested_tp: float = 0.0
    # SMC limit-entry price: the FVG gap edge where a pending limit order should
    # rest (fvg_top for longs, fvg_bottom for shorts). 0.0 = market entry only.
    limit_entry_price: float = 0.0
    # True when the setup is valid but price hasn't reached the FVG zone yet —
    # emit a pending limit order and wait for the pullback (the SMC sniper).
    awaiting_entry: bool = False


@dataclass
class RiskVerdict:
    approved: bool
    original_signal: ScoutSignal
    lot_size: float              # NEW: Exact MT5 volume to execute
    position_size_usd: float     # Estimated notional exposure
    position_size_pct: float
    stop_loss: float
    sl_distance_pct: float
    reasoning: str
    discipline_rules: Dict[str, str]
    warnings: List[str]
    leverage: int = 0            # Kept for DB compatibility, ignored for sizing
    margin_required: float = 0.0 # Estimated margin
    take_profit: float = 0.0
    risk_reward: float = 0.0


@dataclass
class Trade:
    id: str
    timestamp_open: datetime
    timestamp_close: Optional[datetime]
    symbol: str
    bias: Bias
    entry_price: float
    exit_price: Optional[float]
    position_size_usd: float
    initial_stop_loss: float
    stop_loss: float
    status: TradeStatus
    pnl_usd: Optional[float]
    pnl_pct: Optional[float]
    scout_reasoning: str
    risk_reasoning: str
    leverage: int = 0            # Kept for DB compatibility
    margin_used: float = 0.0
    filled_qty: float = 0.0
    trailing_extreme_price: float = 0.0
    take_profit: float = 0.0
    sl_order_id: str = ""
    tp_order_id: str = ""
    ticket: int = 0


@dataclass
class ScholarInsight:
    timestamp: datetime
    period_start: datetime
    period_end: datetime
    total_trades: int
    win_rate: float
    total_pnl: float
    avg_risk_reward: float
    lessons: List[str]
    parameter_changes: Dict[str, Dict]
    market_regime: str
    reasoning: str


@dataclass
class PivotEvent:
    timestamp: datetime
    symbol: str
    old_bias: Bias
    new_bias: Bias
    trigger_price: float
    broken_level: float
    volume_ratio: float
    reasoning: str


@dataclass
class MonologueEntry:
    timestamp: datetime
    agent: str
    message: str
    severity: str
