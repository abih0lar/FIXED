"""Unit tests for the RiskManager gating and sizing pipeline.

Broker state (positions, ticks, margin) is deterministic via the MT5
stub in conftest.py; trades live in a real per-test SQLite database.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import configure_broker, make_snapshot
from models import Bias, ScoutSignal, SignalStrength, Trade, TradeStatus

SYMBOL = "EURUSDm"


def make_signal(**over):
    base = dict(
        timestamp=datetime.now(timezone.utc), symbol=SYMBOL, bias=Bias.LONG,
        strength=SignalStrength.STRONG, confidence=0.8,
        entry_price=1.10000, sniper_entry_price=1.10000,
        indicators=make_snapshot(price=1.10000),
        nearest_support=1.0950, nearest_resistance=1.1100,
        wick_rejection=None, patterns_detected=["SMC FVG"],
        reasoning="test", confidence_breakdown={},
        suggested_sl=1.09500, suggested_tp=1.11000, limit_entry_price=1.10000,
    )
    base.update(over)
    return ScoutSignal(**base)


def make_trade(status=TradeStatus.CLOSED, symbol=SYMBOL, pnl=-500.0,
               entry=1.1000, sl=1.0950, size_usd=200000.0):
    now = datetime.now(timezone.utc)
    return Trade(
        id=f"t-{now.timestamp()}-{symbol}-{pnl}", timestamp_open=now,
        timestamp_close=now if status == TradeStatus.CLOSED else None,
        symbol=symbol, bias=Bias.LONG, entry_price=entry, exit_price=entry,
        position_size_usd=size_usd, initial_stop_loss=sl, stop_loss=sl,
        status=status, pnl_usd=pnl, pnl_pct=-0.5,
        scout_reasoning="", risk_reasoning="",
    )


@pytest.fixture
def rm(db, fake_engine):
    from risk_manager import RiskManager
    return RiskManager(db, fake_engine(), portfolio_value=10_000)


# ── limit parsing ─────────────────────────────────────────────────────────

def test_parse_dynamic_limit_percent(rm):
    assert rm._parse_dynamic_limit("20%") == pytest.approx(2000.0)


def test_parse_dynamic_limit_dollars(rm):
    assert rm._parse_dynamic_limit("$500") == pytest.approx(500.0)
    assert rm._parse_dynamic_limit("20$") == pytest.approx(20.0)


def test_parse_dynamic_limit_fails_safe_on_garbage(rm):
    # unparseable input must fall back to 10% of equity, never 0.0
    assert rm._parse_dynamic_limit("nonsense!!") == pytest.approx(1000.0)


# ── hard gates ────────────────────────────────────────────────────────────

def test_kill_switch_denies_everything(rm):
    rm.kill_switch_active = True
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "Kill switch" in v.reasoning


def test_daily_loss_breach_hard_stops(rm, db, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "max_daily_loss", "2%")            # $200
    db.insert_trade(make_trade(pnl=-500.0))                     # closed today
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "Hard Stop" in v.reasoning


def test_max_drawdown_gate(rm, db, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "max_drawdown_pct", 50.0)
    db.set_system_flag("peak_equity", "20000")                  # 50% below peak
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "drawdown" in v.reasoning.lower()


def test_neutral_bias_denied(rm, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "min_confidence", 0.6)
    v = rm.evaluate(make_signal(bias=Bias.NEUTRAL, confidence=0.9))
    assert v.approved is False
    assert "No directional bias" in v.reasoning


def test_low_confidence_denied(rm, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "min_confidence", 0.6)
    v = rm.evaluate(make_signal(confidence=0.4))
    assert v.approved is False
    assert "Confidence" in v.reasoning


def test_max_open_positions_denied(rm, db, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "max_open_positions", 2)
    for i in range(2):
        db.insert_trade(make_trade(status=TradeStatus.OPEN, symbol=f"SYM{i}m",
                                   pnl=0.0, size_usd=1000.0))
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "Max positions" in v.reasoning


def test_duplicate_symbol_in_db_denied(rm, db):
    db.insert_trade(make_trade(status=TradeStatus.OPEN, pnl=0.0, size_usd=1000.0))
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "Already have active trade" in v.reasoning


def test_duplicate_symbol_at_broker_denied(rm):
    # broker truth beats DB: an open MT5 position blocks the entry
    configure_broker(positions=[SimpleNamespace(symbol=SYMBOL, volume=0.1)])
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "open position at broker" in v.reasoning


def test_resting_order_at_broker_denied(rm):
    configure_broker(orders=[SimpleNamespace(symbol=SYMBOL, volume_current=0.1)])
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "Resting order" in v.reasoning


def test_wide_spread_denied(rm):
    configure_broker(tick=SimpleNamespace(ask=1.1050, bid=1.1000, time=0))
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "Spread" in v.reasoning


def test_poor_risk_reward_denied(rm, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "min_rr", 1.5)
    v = rm.evaluate(make_signal(suggested_tp=1.10100))          # RR 0.2
    assert v.approved is False
    assert "R:R" in v.reasoning


def test_margin_black_hole_denied(rm):
    configure_broker(margin=1_000_000.0)                        # absurd margin
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "margin" in v.reasoning.lower()


# ── sizing math on the happy path ─────────────────────────────────────────

def test_approval_with_volatility_scaled_lot(rm, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "max_risk_per_trade_pct", 2.0)
    monkeypatch.setattr(dyn, "forex_lot_multiplier", 1.0)
    monkeypatch.setattr(dyn, "min_rr", 1.5)
    # sl distance 0.005 / tick 0.00001 * $1 = $500 risk per lot
    # max risk $200 -> 0.4 lots
    v = rm.evaluate(make_signal())
    assert v.approved is True
    assert v.lot_size == pytest.approx(0.4)
    assert v.stop_loss == pytest.approx(1.09500)
    assert v.take_profit == pytest.approx(1.11000)
    assert v.risk_reward == pytest.approx(2.0)


def test_crypto_symbols_use_crypto_gates(rm, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "crypto_min_confidence", 0.45)
    monkeypatch.setattr(dyn, "min_confidence", 0.6)
    monkeypatch.setattr(dyn, "forex_lot_multiplier", 1.0)
    monkeypatch.setattr(dyn, "crypto_lot_multiplier", 3.0)
    monkeypatch.setattr(dyn, "max_risk_per_trade_pct", 2.0)
    monkeypatch.setattr(dyn, "min_rr", 1.5)
    # BTC metadata: 1 lot = 1 BTC, $10 tick, SL $1000 away = $1000 risk/lot
    btc_info = SimpleNamespace(volume_min=0.01, volume_step=0.01,
                               volume_max=100.0, trade_tick_size=10.0,
                               trade_tick_value=1.0, point=0.01,
                               name="BTCUSD", currency_profit="USD")
    configure_broker(symbol_info=btc_info)
    # confidence 0.5: below forex gate (0.6) but above crypto gate (0.45)
    v = rm.evaluate(make_signal(symbol="BTCUSD", confidence=0.5,
                                entry_price=65000.0, sniper_entry_price=65000.0,
                                suggested_sl=64000.0, suggested_tp=67000.0,
                                limit_entry_price=65000.0,
                                indicators=make_snapshot(price=65000.0, atr=500.0)))
    assert v.approved is True


def test_correlated_group_risk_cap(rm, db, dyn, monkeypatch):
    monkeypatch.setattr(dyn, "max_group_risk_pct", 8.0)         # $800
    # EURGBP maps to the same "EUR" group as EURUSD: $909 risk already open
    db.insert_trade(make_trade(status=TradeStatus.OPEN, symbol="EURGBP",
                               pnl=0.0, entry=1.1000, sl=1.0950,
                               size_usd=200000.0))
    v = rm.evaluate(make_signal())
    assert v.approved is False
    assert "Correlated group" in v.reasoning
