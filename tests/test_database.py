"""Smoke tests for the SQLite persistence layer."""
from datetime import datetime, timezone

import pytest

from models import Bias, Trade, TradeStatus


def make_trade(i=0, status=TradeStatus.OPEN, symbol="EURUSDm"):
    now = datetime.now(timezone.utc)
    return Trade(
        id=f"trade-{i}", timestamp_open=now, timestamp_close=None,
        symbol=symbol, bias=Bias.LONG, entry_price=1.1000, exit_price=None,
        position_size_usd=10_000.0, initial_stop_loss=1.0950,
        stop_loss=1.0950, status=status, pnl_usd=None, pnl_pct=None,
        scout_reasoning="smc", risk_reasoning="ok",
    )


def test_insert_and_query_open_trade(db):
    db.insert_trade(make_trade())
    active = db.get_active_trades()
    assert len(active) == 1
    assert active[0]["symbol"] == "EURUSDm"
    assert active[0]["status"] == "OPEN"


def test_update_trade_status(db):
    db.insert_trade(make_trade())
    db.update_trade("trade-0", status="CLOSED", pnl_usd=25.5, pnl_pct=0.25)
    assert db.get_active_trades() == []
    closed = db.get_trades(days_back=1)
    assert closed[0]["pnl_usd"] == pytest.approx(25.5)


def test_pending_trades_are_active(db):
    db.insert_trade(make_trade(status=TradeStatus.PENDING))
    assert len(db.get_active_trades()) == 1
    assert len(db.get_pending_trades()) == 1
    assert db.get_open_trades() == []


def test_system_flags_roundtrip(db):
    db.set_system_flag("peak_equity", "12345.6")
    assert db.get_system_flag("peak_equity") == "12345.6"
    assert db.get_system_flag("missing") is None


def test_reset_trades_only_preserves_flags(db):
    db.insert_trade(make_trade())
    db.set_system_flag("peak_equity", "100")
    db.reset_trades_only()
    assert db.get_trades(days_back=30) == []
    assert db.get_system_flag("peak_equity") == "100"
