# --- START OF FILE database.py ---

import json
import os
import sys
import logging
import sqlite3
import base64
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Dict, List, Optional
from models import Trade, MonologueEntry, ScholarInsight

log = logging.getLogger(__name__)

TRADE_COLUMNS = {
    "timestamp_close", "exit_price", "position_size_usd",
    "stop_loss", "status", "pnl_usd", "pnl_pct",
    "scout_reasoning", "risk_reasoning", "trailing_extreme_price",
    "take_profit", "leverage", "margin_used", "filled_qty",
    "sl_order_id", "tp_order_id", "ticket"
}


class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        # datetime subclasses (incl. pandas Timestamp / patched sim clocks)
        # all expose isoformat(); also catches tz-aware variants reliably
        iso = getattr(obj, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                pass
        if str(type(obj).__name__) == "datetime64":   # numpy datetime64
            return str(obj)
        if is_dataclass(obj):
            return asdict(obj)
        return super().default(obj)


class Database:
    def __init__(self, db_path: str = "trading_bot.db"):
        # FIX: a relative db_path (".env says 'db/trading_bot.db'") must resolve
        # against the APP base (exe folder when frozen, else this file's dir) —
        # NOT the process cwd. Otherwise the DB lands wherever the exe was
        # launched from (dist\, a shortcut's cwd, ...) and the db\ folder stays
        # empty. Also create the parent dir so it always works.
        try:
            _base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                else os.path.dirname(os.path.abspath(__file__))
            _base = os.path.dirname(_base) if _base.endswith(("db", "data")) else _base
        except Exception:
            _base = None
        if _base and db_path and not os.path.isabs(db_path):
            db_path = os.path.normpath(os.path.join(_base, db_path))
        try:
            _d = os.path.dirname(os.path.abspath(db_path)) if db_path else ""
            if _d:
                os.makedirs(_d, exist_ok=True)
        except Exception:
            pass
        self.db_path = db_path
        self._local = threading.local()  # Thread-safe persistent connection pooling
        self._init_tables()

    @property
    def connection(self):
        # Create a connection ONLY once per thread
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;") # Faster than FULL, safe in WAL
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def _conn(self):
        conn = self.connection
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _add_column_if_not_exists(self, conn, table: str, column: str, definition: str):
        cursor = conn.execute(f"PRAGMA table_info({table})")
        existing_cols = [row["name"] for row in cursor.fetchall()]
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            log.info(f"[DB] Migration: Added {column} to {table}")

    def _init_tables(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    timestamp_open TEXT NOT NULL,
                    timestamp_close TEXT,
                    symbol TEXT NOT NULL,
                    bias TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    position_size_usd REAL NOT NULL,
                    initial_stop_loss REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    status TEXT NOT NULL,
                    pnl_usd REAL,
                    pnl_pct REAL,
                    scout_reasoning TEXT,
                    risk_reasoning TEXT,
                    leverage INTEGER NOT NULL DEFAULT 10,
                    margin_used REAL NOT NULL DEFAULT 0.0,
                    filled_qty REAL NOT NULL DEFAULT 0.0,
                    trailing_extreme_price REAL NOT NULL DEFAULT 0.0,
                    take_profit REAL NOT NULL DEFAULT 0.0,
                    ticket INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS monologue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    message TEXT NOT NULL,
                    severity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scholar_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    total_trades INTEGER,
                    win_rate REAL,
                    total_pnl REAL,
                    lessons TEXT,
                    parameter_changes TEXT,
                    market_regime TEXT,
                    reasoning TEXT
                );
                CREATE TABLE IF NOT EXISTS pivot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    old_bias TEXT NOT NULL,
                    new_bias TEXT NOT NULL,
                    trigger_price REAL,
                    broken_level REAL,
                    volume_ratio REAL,
                    reasoning TEXT
                );
                CREATE TABLE IF NOT EXISTS dynamic_config_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    changed_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS q_table (
                    state_key TEXT PRIMARY KEY,
                    action_values TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_flags (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Deterministic schema migrations
            self._add_column_if_not_exists(conn, "trades", "leverage", "INTEGER NOT NULL DEFAULT 10")
            self._add_column_if_not_exists(conn, "trades", "margin_used", "REAL NOT NULL DEFAULT 0.0")
            self._add_column_if_not_exists(conn, "trades", "filled_qty", "REAL NOT NULL DEFAULT 0.0")
            self._add_column_if_not_exists(conn, "trades", "sl_order_id", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_not_exists(conn, "trades", "tp_order_id", "TEXT NOT NULL DEFAULT ''")
            self._add_column_if_not_exists(conn, "trades", "ticket", "INTEGER NOT NULL DEFAULT 0")

    def set_memory_state(self, key: str, obj):
        if obj is None:
            return
        try:
            json_str = json.dumps(obj, cls=CustomEncoder)
            b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_flags (key, value) VALUES (?, ?)",
                    (key, b64),
                )
        except Exception as e:
            log.warning(f"[DB] set_memory_state failed for {key}: {e}")

    def get_memory_state(self, key: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM system_flags WHERE key = ?", (key,)
            ).fetchone()
            if row:
                try:
                    return json.loads(base64.b64decode(row["value"]).decode("utf-8"))
                except Exception:
                    return None
            return None

    def set_system_flag(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO system_flags (key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_system_flag(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM system_flags WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def insert_trade(self, trade: Trade):
        ext = (
            trade.trailing_extreme_price
            if trade.trailing_extreme_price != 0.0
            else trade.entry_price
        )
        with self._conn() as conn:
            # EXPLICIT COLUMN MAPPING TO PREVENT SCHEMA MISALIGNMENT
            conn.execute(
                """INSERT INTO trades (
                    id, timestamp_open, timestamp_close, symbol, bias,
                    entry_price, exit_price, position_size_usd, initial_stop_loss,
                    stop_loss, status, pnl_usd, pnl_pct, scout_reasoning, risk_reasoning,
                    leverage, margin_used, filled_qty, trailing_extreme_price,
                    take_profit, sl_order_id, tp_order_id, ticket
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.id,
                    trade.timestamp_open.isoformat(),
                    trade.timestamp_close.isoformat() if trade.timestamp_close else None,
                    trade.symbol,
                    trade.bias.value,
                    trade.entry_price,
                    trade.exit_price,
                    trade.position_size_usd,
                    trade.initial_stop_loss,
                    trade.stop_loss,
                    trade.status.value,
                    trade.pnl_usd,
                    trade.pnl_pct,
                    trade.scout_reasoning,
                    trade.risk_reasoning,
                    trade.leverage,
                    trade.margin_used,
                    trade.filled_qty,
                    ext,
                    trade.take_profit,
                    trade.sl_order_id,
                    trade.tp_order_id,
                    trade.ticket,
                ),
            )

    def update_trade(self, trade_id: str, **kwargs):
        if not kwargs:
            return
        safe = {k: v for k, v in kwargs.items() if k in TRADE_COLUMNS}
        if not safe:
            log.warning(f"[DB] update_trade: no valid columns in {list(kwargs.keys())}")
            return
        set_parts = [f"{k} = ?" for k in safe.keys()]
        values = list(safe.values()) + [trade_id]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE trades SET {', '.join(set_parts)} WHERE id = ?", values
            )

    def get_trades(self, days_back: int = 30) -> List[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM trades WHERE timestamp_open > ? ORDER BY timestamp_open DESC",
                    (cutoff,),
                ).fetchall()
            ]

    def get_active_trades(self) -> List[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM trades WHERE status IN ('OPEN', 'PENDING') ORDER BY timestamp_open ASC"
                ).fetchall()
            ]

    def get_pending_trades(self) -> List[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM trades WHERE status = 'PENDING' ORDER BY timestamp_open ASC"
                ).fetchall()
            ]

    def get_open_trades(self) -> List[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY timestamp_open ASC"
                ).fetchall()
            ]

    def get_trade_stats(self, days_back: int = 7) -> Dict:
        try:
            closed = [t for t in self.get_trades(days_back) if t["status"] == "CLOSED"]
            wins = [t for t in closed if (t.get("pnl_usd") or 0) > 0]
            losses = [t for t in closed if (t.get("pnl_usd") or 0) <= 0]
            total_pnl = sum(t.get("pnl_usd") or 0 for t in closed)
            win_rate = len(wins) / max(len(closed), 1)

            win_pnls = [abs(t.get("pnl_usd", 0)) for t in wins]
            loss_pnls = [abs(t.get("pnl_usd", 0)) for t in losses]
            avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
            avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 1
            avg_rr = avg_win / avg_loss if avg_loss > 0 else 0

            return {
                "total": len(closed),
                "wins": len(wins),
                "losses": len(closed) - len(wins),
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "avg_rr": avg_rr,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
            }
        except Exception as e:
            log.warning(f"[DB] get_trade_stats error: {e}")
            return {
                "total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_rr": 0, "avg_win": 0, "avg_loss": 1,
            }

    def add_monologue(self, entry: MonologueEntry):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO monologue (timestamp, agent, message, severity) VALUES (?,?,?,?)",
                (entry.timestamp.isoformat(), entry.agent, entry.message, entry.severity),
            )

    def get_monologue(self, limit: int = 50) -> List[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in reversed(
                    conn.execute(
                        "SELECT * FROM monologue ORDER BY id DESC LIMIT ?", (limit,)
                    ).fetchall()
                )
            ]

    def add_scholar_review(self, insight: ScholarInsight):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO scholar_reviews
                   (timestamp, period_start, period_end, total_trades,
                    win_rate, total_pnl, lessons, parameter_changes,
                    market_regime, reasoning)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    insight.timestamp.isoformat(),
                    insight.period_start.isoformat(),
                    insight.period_end.isoformat(),
                    insight.total_trades,
                    insight.win_rate,
                    insight.total_pnl,
                    json.dumps(insight.lessons),
                    json.dumps(insight.parameter_changes),
                    insight.market_regime,
                    insight.reasoning,
                ),
            )

    def get_scholar_reviews(self, limit: int = 10) -> List[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM scholar_reviews ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def get_q_values(self, state_key: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT action_values FROM q_table WHERE state_key = ?", (state_key,)
            ).fetchone()
            return json.loads(row["action_values"]) if row else None

    def upsert_q_values(self, state_key: str, action_values: Dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO q_table (state_key, action_values) VALUES (?, ?)
                   ON CONFLICT(state_key) DO UPDATE SET action_values = excluded.action_values""",
                (state_key, json.dumps(action_values)),
            )

    def save_config_snapshot(self, config_dict: Dict, changed_by: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO dynamic_config_history (timestamp, config_json, changed_by) VALUES (?,?,?)",
                (datetime.now(timezone.utc).isoformat(), json.dumps(config_dict), changed_by),
            )

    def get_latest_config_snapshot(self) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT config_json FROM dynamic_config_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row["config_json"]) if row else None

    def reset_trades_only(self):
        with self._conn() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM pivot_events")

    def reset_all_data(self):
        with self._conn() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM monologue")
            conn.execute("DELETE FROM scholar_reviews")
            conn.execute("DELETE FROM pivot_events")
            conn.execute("DELETE FROM q_table")
            conn.execute("DELETE FROM dynamic_config_history")
