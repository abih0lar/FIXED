# --- START OF FILE orchestrator.py ---

import logging
import threading
import time
import uuid
import os
import csv
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

import numpy as np
import MetaTrader5 as mt5

from config import BOT_CONFIG
from data_engine import DataEngine, MIN_BARS_FOR_INDICATORS, MAGIC
from database import Database
from models import Bias, MonologueEntry, SignalStrength, Trade, TradeStatus
from news_filter import NewsFilter
from notifier import Notifier
from risk_manager import RiskManager
from scholar import Scholar
from scout import Scout

log = logging.getLogger(__name__)


class TrailingStopManager:

    def update(
        self, bias: str, entry: float, initial_sl: float,
        current_price: float, current_sl: float, current_extreme: float,
        be_r: float, atr: float, chandelier_mult: float,
        ma_fast: float = 0.0
    ) -> Tuple[Optional[float], float]:
        if atr <= 0: return (None, current_extreme)

        MIN_STEP_PCT = 0.0005
        fee_round_trip_pct = BOT_CONFIG.dynamic.taker_fee_pct * 2 / 100
        breakeven_buffer = max(entry * 0.0005, entry * fee_round_trip_pct * 1.5)
        r_dist = abs(entry - initial_sl)

        # F3-FIX: R-based chandelier. The old `extreme - atr*3` trailed 2R behind a
        # 1.5xATR SL, so it could never tighten beyond breakeven until +2R — runners
        # gave back everything between BE and 2R. Now the trail distance is in R units
        # (tsl_chandelier_r_mult, default 1.5R), so a 2R extreme locks +0.5R.
        dyn = BOT_CONFIG.dynamic
        chand_r_mult = dyn.tsl_chandelier_r_mult
        half_r = dyn.tsl_half_risk_r

        if bias == "LONG":
            updated_extreme = max(current_extreme, current_price)
            profit = current_price - entry
            new_sl = current_sl

            # MOMENTUM GUARD (post-fill): if price is in profit >= momentum_be_r but
            # the fast EMA has flipped below entry, momentum has turned — pull SL to
            # breakeven now, before the price-based chandelier trail catches up.
            if ma_fast > 0 and profit >= (r_dist * dyn.momentum_be_r) - 1e-12 and ma_fast < entry:
                be_sl = entry + breakeven_buffer
                if be_sl > new_sl:
                    new_sl = be_sl

            half_risk_sl = initial_sl + (r_dist * 0.5)
            if profit >= (r_dist * half_r) - 1e-12 and new_sl < half_risk_sl and half_risk_sl < entry:
                new_sl = half_risk_sl

            if profit >= (r_dist * be_r) - 1e-12 and new_sl < entry:
                new_sl = entry + breakeven_buffer

            chandelier_sl = updated_extreme - (r_dist * chand_r_mult)
            if chandelier_sl > new_sl:
                new_sl = chandelier_sl

            if ma_fast > 0 and profit >= (r_dist * 2.0) - 1e-12:
                ma_sl = ma_fast - (atr * 0.5)
                if ma_sl > new_sl and ma_sl < current_price:
                    new_sl = ma_sl

            if new_sl > current_sl:
                if (new_sl - current_sl) / max(current_sl, 1e-8) >= MIN_STEP_PCT:
                    return (round(new_sl, 5), updated_extreme)
            return (None, updated_extreme)

        elif bias == "SHORT":
            updated_extreme = min(current_extreme, current_price)
            profit = entry - current_price
            new_sl = current_sl
            momentum_fired = False

            # MOMENTUM GUARD (post-fill): symmetric for shorts — fast EMA above
            # entry while in profit >= momentum_be_r means momentum turned up; pull
            # SL to breakeven now (be_sl is ABOVE the current SL — protective move,
            # so it must bypass the normal tighten-only gate below).
            if ma_fast > 0 and profit >= (r_dist * dyn.momentum_be_r) - 1e-12 and ma_fast > entry:
                be_sl = entry - breakeven_buffer
                if be_sl > new_sl:
                    new_sl = be_sl
                    momentum_fired = True

            half_risk_sl = initial_sl - (r_dist * 0.5)
            if profit >= (r_dist * half_r) - 1e-12 and new_sl > half_risk_sl and half_risk_sl > entry:
                new_sl = half_risk_sl

            if profit >= (r_dist * be_r) - 1e-12 and new_sl > entry:
                new_sl = entry - breakeven_buffer

            chandelier_sl = updated_extreme + (r_dist * chand_r_mult)
            if chandelier_sl < new_sl:
                new_sl = chandelier_sl

            if ma_fast > 0 and profit >= (r_dist * 2.0) - 1e-12:
                ma_sl = ma_fast + (atr * 0.5)
                if ma_sl < new_sl and ma_sl > current_price:
                    new_sl = ma_sl

            # Momentum guard may raise SL above current_sl (protective breakeven);
            # bypass the tighten-only gate when it fired.
            if momentum_fired:
                if new_sl > current_sl:
                    return (round(new_sl, 5), updated_extreme)
                return (None, updated_extreme)

            if new_sl < current_sl:
                if (current_sl - new_sl) / max(current_sl, 1e-8) >= MIN_STEP_PCT:
                    return (round(new_sl, 5), updated_extreme)
            return (None, updated_extreme)

        return (None, current_extreme)


class Orchestrator:

    SCAN_INTERVAL_SECONDS = 60

    def __init__(self):
        self.db = Database(BOT_CONFIG.db_path)
        self.data_engine = DataEngine()
        self.scout = Scout(self.data_engine, self.db)
        self.risk_mgr = RiskManager(self.db, self.data_engine)
        self.scholar = Scholar(self.db)
        self.trailing = TrailingStopManager()
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._scholar_thread: Optional[threading.Thread] = None
        self.cycle_count = 0
        self.last_signals: Dict = {}
        self.last_verdicts: Dict = {}
        self._milestones: Dict = {}
        self._cooldowns: Dict[str, datetime] = {}
        self._last_digest_date = None
        self._scan_lock = threading.Lock()
        self._initial_scan_done = threading.Event()
        # F2-DIAG: pipeline starvation counters (denied setups vs approved trades)
        self._denial_stats = {"rr": 0, "conf": 0, "other": 0, "approved": 0}
        # ticket -> reason of the LAST pending-cancel notification, so the same
        # dead order can never re-ping Telegram every cycle
        self._cancel_notified: Dict[str, str] = {}
        self._last_starvation_warn = None
        # SIGNALS LOG: every executed trade (open/fill/close) appended to a CSV so
        # you can track win rate / R / outcomes without parsing log spam.
        _data_dir = os.environ.get("ALADIN_DATA_DIR") or \
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        try:
            os.makedirs(_data_dir, exist_ok=True)
        except Exception:
            pass
        self._signals_log = os.path.join(_data_dir, "signals_log.csv")
        self._signals_lock = threading.Lock()
        self._ensure_signals_header()
        # FIX: single Notifier instance, constructed WITH db so the Telegram C2
        # listener can persist flags (the old first `Notifier()` was wasted work).
        self.notifier = Notifier(db=self.db)
        # ECONOMIC CALENDAR: blocks new entries around High-Impact news (the
        # weekly ForexFactory feed, cached 4h, fail-open when unreachable).
        dyn_n = BOT_CONFIG.dynamic
        self.news_filter = NewsFilter(
            block_before_mins=dyn_n.news_block_before_mins,
            block_after_mins=dyn_n.news_block_after_mins,
        )
        self.news_filter_enabled = bool(dyn_n.news_filter_enabled)
        threading.Thread(target=self._safe_initial_scan, daemon=True).start()

    # ── signals CSV log ─────────────────────────────────────────

    _SIGNAL_HEADER = [
        "trade_id", "symbol", "bias", "order_type", "ticket",
        "signal_ts_utc", "confidence", "signal_rr", "limit_entry", "market_entry",
        "fill_ts_utc", "fill_price", "qty", "initial_sl", "initial_tp",
        "close_ts_utc", "exit_price", "pnl_usd", "pnl_pct", "R",
        "exit_reason", "status",
    ]

    def _signals_path(self):
        return getattr(self, "_signals_log", None) or "signals_log.csv"

    def _ensure_signals_header(self):
        try:
            if not os.path.exists(self._signals_path()):
                with open(self._signals_path(), "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._SIGNAL_HEADER)
        except Exception as e:
            log.error(f"[SignalsLog] header init failed: {e}")

    def _append_signal_row(self, row: dict):
        """Append one signal row (thread-safe). Missing keys -> blank."""
        try:
            with self._signals_lock:
                path = self._signals_path()
                exists = os.path.exists(path)
                with open(path, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=self._SIGNAL_HEADER, extrasaction="ignore")
                    if not exists:
                        w.writeheader()
                    w.writerow(row)
        except Exception as e:
            log.error(f"[SignalsLog] append failed: {e}")

    def _fmt_dt(self, dt) -> str:
        if dt is None:
            return ""
        if isinstance(dt, str):
            return dt
        try:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(dt)

    def _safe_initial_scan(self):
        try:
            self._reconcile_positions()
            self._execute_scan()
        except Exception as e:
            log.error(f"Initial scan error: {e}")
        finally:
            self._initial_scan_done.set()

    def start(self):
        if not self.running:
            self.running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            # Scholar auto-tune is OFF by default (matches the validated backtests).
            # When off, Scholar still records insights but never mutates live
            # parameters — the bot behaves exactly as validated.
            if BOT_CONFIG.dynamic.scholar_auto_tune:
                self._scholar_thread = threading.Thread(target=self._scholar_worker, daemon=True)
                self._scholar_thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)
        if self._scholar_thread:
            self._scholar_thread.join(timeout=5)
        self.data_engine.shutdown()

    def _run_loop(self):
        if not self._initial_scan_done.is_set():
            self._initial_scan_done.wait(timeout=15)
        while self.running:
            start_time = time.monotonic()
            try:
                self.cycle_count += 1
                log.info(f"--- Cycle {self.cycle_count} ---")

                self._check_daily_digest()
                self._reconcile_positions()
                self._manage_positions()
                self._execute_scan()
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)
            elapsed = time.monotonic() - start_time
            time.sleep(max(0, self.SCAN_INTERVAL_SECONDS - elapsed))

    def _check_daily_digest(self):
        now = datetime.now(timezone.utc)
        if now.hour == 23 and now.minute >= 55:
            current_date_str = now.strftime("%Y-%m-%d")
            if self._last_digest_date != current_date_str:
                self._last_digest_date = current_date_str
                try:
                    bal = self.data_engine.get_account_balance()
                    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                    trades = self.db.get_trades(days_back=2)
                    today_trades = [t for t in trades if t["status"] == "CLOSED" and t.get("timestamp_close") and t["timestamp_close"] >= today_start]

                    wins = len([t for t in today_trades if (t.get("pnl_usd") or 0) > 0])
                    losses = len(today_trades) - wins
                    total_pnl = sum(t.get("pnl_usd") or 0 for t in today_trades)
                    win_rate = (wins / max(len(today_trades), 1))
                    start_bal = bal - total_pnl
                    pnl_pct = (total_pnl / start_bal * 100) if start_bal > 0 else 0.0

                    self.notifier.daily_digest(bal, total_pnl, pnl_pct, win_rate, wins, losses)
                    log.info(f"[Orchestrator] Daily Digest successfully broadcasted to Telegram.")
                except Exception as e:
                    log.error(f"Failed to compile Daily Digest: {e}")

    def _scholar_worker(self):
        log.info("[ScholarWorker] Background meta-strategy review thread started.")
        while self.running:
            try:
                if self.scholar.should_review():
                    log.info("[ScholarWorker] Triggering background meta-strategy review...")
                    symbol = BOT_CONFIG.exchange.symbols[0] if BOT_CONFIG.exchange.symbols else "EURUSD"
                    try:
                        df = self.data_engine.fetch_ohlcv(symbol, BOT_CONFIG.timeframes.primary, 200)
                        df = self.data_engine.compute_indicators(df)
                        r = df.iloc[-2]
                        vol = float(r.get("atr", 0.0)) / float(r["close"]) if r.get("close", 0) else 0.01
                        trend = float(r.get("adx", 25.0)) / 100.0
                    except Exception as e:
                        log.warning(f"[ScholarWorker] Fetching proxies failed: {e}. Using defaults.")
                        vol, trend = 0.015, 0.5

                    insight = self.scholar.meta_strategy_update(vol, trend)
                    if insight and insight.total_trades > 0:
                        self.notifier.scholar_review(
                            trades_reviewed=insight.total_trades,
                            win_rate=insight.win_rate,
                            total_pnl=insight.total_pnl,
                            regime=insight.market_regime
                        )
            except Exception as e:
                log.error(f"[ScholarWorker] Error: {e}", exc_info=True)

            for _ in range(15 * 60):
                if not self.running: break
                time.sleep(1)

    def _update_trailing_stop(self, symbol: str, side: str, qty: float, new_sl: float, ticket: int) -> Optional[dict]:
        try:
            return self.data_engine.submit_stop_order(symbol, side, qty, new_sl, ticket)
        except Exception as e:
            log.error(f"[{symbol}] Failed to place new SL for ticket {ticket}: {e}")
            return None

    def _execute_scan(self):
        if not self._scan_lock.acquire(blocking=False): return
        try:
            # FIX: honor the Telegram /pause kill switch. Previously the flag was
            # written to the DB but never read by RiskManager, so "pause" was cosmetic.
            self.risk_mgr.kill_switch_active = (self.db.get_system_flag("kill_switch") == "TRUE")

            bal = self.data_engine.get_account_balance()
            if bal > 0:
                log.info(f"[Orchestrator] Portfolio: ${bal:,.2f}")
                self.risk_mgr.set_portfolio_value(bal)
                peak_str = self.db.get_system_flag("peak_equity")
                if peak_str:
                    if bal > float(peak_str): self.db.set_system_flag("peak_equity", str(bal))
                else:
                    self.db.set_system_flag("peak_equity", str(bal))

            override = self.db.get_system_flag("manual_override")
            current_minute = datetime.now(timezone.utc).minute
            win_start = BOT_CONFIG.dynamic.execution_window_start
            win_end = BOT_CONFIG.dynamic.execution_window_end

            if BOT_CONFIG.exchange.market_type.lower() == "synthetic":
                in_window = True
            else:
                in_window = (win_start <= current_minute <= win_end)

            cycle_summary = []
            trade_candidates = []
            active_symbols = []
            hour_wat = 0

            if BOT_CONFIG.exchange.market_type.lower() == "synthetic":
                active_symbols = BOT_CONFIG.exchange.symbols
            else:
                now_utc = datetime.now(timezone.utc)
                hour_wat = (now_utc.hour + 1) % 24

                if 8 <= hour_wat < 18:
                    active_symbols.extend(BOT_CONFIG.exchange.day_symbols)

                if hour_wat >= 22 or hour_wat < 6:
                    active_symbols.extend(BOT_CONFIG.exchange.night_symbols)

                crypto_keywords = ["BTC", "ETH", "SOL", "LTC", "XRP", "DOGE", "ADA", "DOT"]
                for sym in BOT_CONFIG.exchange.symbols:
                    if any(c in sym.upper() for c in crypto_keywords):
                        active_symbols.append(sym)

            active_symbols = list(set(active_symbols))

            if not active_symbols:
                if self.cycle_count % 15 == 0:
                    log.info(f"[Orchestrator] System resting. No active trading sessions for {hour_wat}:00 WAT.")
                return

            for symbol in active_symbols:
                try:
                    if not self.data_engine.is_market_open(symbol):
                        cycle_summary.append(f"{symbol: <12} - MARKET CLOSED")
                        continue

                    # ECONOMIC CALENDAR: if a High-Impact event for this symbol's
                    # currencies is inside the block window, hold NEW entries —
                    # no signal, no limit, no market order (existing positions
                    # are unaffected; their SL/TP management still runs).
                    if self.news_filter_enabled:
                        try:
                            news_ok, news_reason = self.news_filter.is_safe_to_trade(symbol)
                        except Exception:
                            news_ok, news_reason = True, ""   # fail-open
                        if not news_ok:
                            cycle_summary.append(f"{symbol: <12} - NEWS BLOCKED")
                            log.info(f"[Orchestrator] [{symbol}] {news_reason}")
                            continue

                    active_trades = self.db.get_active_trades()
                    current_trade = next((t for t in active_trades if t["symbol"] == symbol), None)
                    broker_pos = self.data_engine._get_position_obj(symbol)

                    signal = self.scout.scan(symbol)
                    now_utc = datetime.now(timezone.utc)

                    if override == "FORCE_BUY":
                        signal.bias, signal.confidence, signal.strength = Bias.LONG, 0.85, SignalStrength.STRONG
                        if symbol in self._cooldowns: del self._cooldowns[symbol]
                        self.db.set_system_flag("manual_override", "NONE")
                        override = "NONE"
                    elif override == "FORCE_SELL":
                        signal.bias, signal.confidence, signal.strength = Bias.SHORT, 0.85, SignalStrength.STRONG
                        if symbol in self._cooldowns: del self._cooldowns[symbol]
                        self.db.set_system_flag("manual_override", "NONE")
                        override = "NONE"

                    self.last_signals[symbol] = signal
                    self.db.set_memory_state("last_signals", self.last_signals)

                    current_bias = None
                    if current_trade or broker_pos:
                        current_bias = current_trade["bias"] if current_trade else ("LONG" if broker_pos.type == mt5.POSITION_TYPE_BUY else "SHORT")

                    pos_flag = ""
                    if current_bias:
                        pdir = "BUY" if current_bias == "LONG" else "SELL"
                        pos_flag = f"[ACTIVE - {pdir}]"
                    elif symbol in self._cooldowns and now_utc < self._cooldowns[symbol]:
                        pos_flag = "[COOLDOWN]"

                    four_h = "NEUTRAL"
                    if "4H: LONG" in signal.reasoning: four_h = "LONG"
                    elif "4H: SHORT" in signal.reasoning: four_h = "SHORT"
                    elif "4H: OFF" in signal.reasoning: four_h = "OFF"

                    vol = signal.confidence_breakdown.get("vol_ratio", 0.0)
                    display_bias = signal.confidence_breakdown.get("intended_bias", signal.bias.value)
                    conf_str = f"({signal.confidence:.0%})"

                    summary_line = f"{symbol: <12} - {display_bias: <7} {conf_str:<6} {pos_flag:<16} 4H: {four_h:<7} VOL: {vol:.2f}x"
                    cycle_summary.append(summary_line)

                    if current_bias:
                        if signal.bias != Bias.NEUTRAL and signal.bias.value != current_bias:
                            conviction_gap = signal.confidence - BOT_CONFIG.dynamic.min_confidence
                            if conviction_gap >= 0.15:
                                log.warning(f"[{symbol}] CONVICTION GAP TRIGGERED! Reversing {current_bias} -> {signal.bias.value} (Conf: {signal.confidence:.0%})")
                                self.db.add_monologue(MonologueEntry(datetime.now(timezone.utc), "Orchestrator", f"[{symbol}] Conviction Reversal Triggered: {current_bias} -> {signal.bias.value}", "warning"))
                                self.close_trade_by_symbol(symbol, "Dynamic Conviction Reversal")
                                time.sleep(1.0)
                            else: continue
                        else: continue

                    # DIAGNOSTICS FIX: the scout returns NEUTRAL for "no valid setup"
                    # (waiting for pullback, FVG too small, no FVG, low RR...). Those
                    # are NOT risk denials — evaluating them polluted the starvation
                    # counter with 5,000+ fake "Confidence 0%" denials and spammed the
                    # log. Skip risk evaluation entirely for NEUTRAL scout output
                    # (the FORCE_* override above already converts NEUTRAL to a real
                    # bias, so real forced trades still pass through here).
                    if signal.bias == Bias.NEUTRAL:
                        continue

                    verdict = self.risk_mgr.evaluate(signal)
                    self.last_verdicts[symbol] = verdict
                    self.db.set_memory_state("last_verdicts", self.last_verdicts)

                    status_str = "APPROVED" if verdict.approved else "DENIED"
                    if symbol in self._cooldowns:
                        if now_utc < self._cooldowns[symbol]:
                            rem = self._cooldowns[symbol] - now_utc
                            hrs, remainder = divmod(int(rem.total_seconds()), 3600)
                            mins, _ = divmod(remainder, 60)
                            verdict.approved = False
                            verdict.lot_size = 0.0
                            status_str = f"DENIED (Cool-down Active: {hrs}h {mins}m remaining)"
                        else:
                            del self._cooldowns[symbol]

                    log.info(f"[{symbol}] Scout: {signal.bias.value} ({signal.confidence:.0%}) | Risk: {status_str} | Lot: {verdict.lot_size}")

                    if not verdict.approved:
                        # F2-DIAG: classify denials so starvation is visible
                        _r = verdict.reasoning
                        if ("R:R" in _r) or ("Target Capped" in _r) or ("below min" in _r):
                            self._denial_stats["rr"] += 1
                        elif "Confidence" in _r:
                            self._denial_stats["conf"] += 1
                        else:
                            self._denial_stats["other"] += 1
                        continue
                    else:
                        self._denial_stats["approved"] += 1

                    if not in_window and override not in ("FORCE_BUY", "FORCE_SELL"):
                        log.info(f"[{symbol}] Outside execution window (minute {current_minute}, window {win_start}-{win_end}) — skipping")
                        continue

                    trade_candidates.append((symbol, signal, verdict))

                except Exception as e:
                    log.error(f"Error scanning {symbol}: {e}", exc_info=True)

            # F2-DIAG: surface pipeline starvation (setups denied, none approved)
            if self._denial_stats["approved"] == 0 and sum(self._denial_stats.values()) > 0:
                now_utc = datetime.now(timezone.utc)
                if self._last_starvation_warn is None or \
                   (now_utc - self._last_starvation_warn).total_seconds() > 3600:
                    self._last_starvation_warn = now_utc
                    log.warning(
                        f"[Orchestrator] PIPELINE STARVATION: "
                        f"{sum(self._denial_stats.values())} setups denied, 0 approved since start "
                        f"(RR<min: {self._denial_stats['rr']}, confidence: {self._denial_stats['conf']}, "
                        f"other: {self._denial_stats['other']}). "
                        f"Tune MIN_RR / MIN_CONFIDENCE / HTF_ZONE_FILTER or inspect scout reasons.")

            if cycle_summary:
                log.info("=== CYCLE SUMMARY ===")
                for line in cycle_summary:
                    log.info(line)
                log.info("=====================")

            if trade_candidates:
                trade_candidates.sort(key=lambda x: x[1].confidence, reverse=True)

                for symbol, signal, verdict in trade_candidates:
                    side = "buy" if signal.bias == Bias.LONG else "sell"

                    if not BOT_CONFIG.dynamic.allow_short and side == "sell":
                        continue

                    active_count = len(self.db.get_active_trades())
                    if active_count >= BOT_CONFIG.dynamic.max_open_positions:
                        log.warning(f"[{symbol}] Max open positions ({BOT_CONFIG.dynamic.max_open_positions}) reached. Execution stopping.")
                        break

                    qty = verdict.lot_size
                    notional = verdict.position_size_usd
                    entry_price = signal.sniper_entry_price

                    if qty <= 0: continue

                    # SMC execution: if the signal carries a limit_entry_price (FVG gap
                    # edge), place a PENDING LIMIT and wait for the pullback instead of
                    # chasing with a market order. EXCEPTION (CRYPTO MARKET-ENTRY
                    # FALLBACK): for crypto signals with confidence >= the configured
                    # threshold, skip the limit and enter at market — high-confidence
                    # crypto setups frequently never return to the FVG edge, so the
                    # limit sits unfilled while price moves (180d: +19% fills, WR
                    # 62->67%, avgR unchanged, PF 1.72).
                    limit_price = getattr(signal, "limit_entry_price", 0.0) or 0.0
                    _crypto_keys = ("BTC", "ETH", "SOL", "LTC", "XRP", "DOGE", "ADA", "DOT")
                    _is_crypto_sym = any(k in symbol.upper() for k in _crypto_keys) or \
                        BOT_CONFIG.exchange.market_type.lower() == "crypto"
                    _mkt_conf = BOT_CONFIG.dynamic.crypto_market_entry_conf
                    _awaiting = bool(getattr(signal, "awaiting_entry", False))
                    # NEVER market-chase a sniper setup: awaiting_entry means price is
                    # away from the FVG, so a market order would be a chase. Always
                    # place the pending limit for these (crypto fallback excluded).
                    _use_market = (_is_crypto_sym and _mkt_conf > 0
                                   and signal.confidence >= _mkt_conf and not _awaiting)

                    if limit_price > 0 and not _use_market:
                        try:
                            filled_order = self.data_engine.submit_pending_limit(
                                symbol, side, qty, limit_price,
                                sl=verdict.stop_loss, tp=verdict.take_profit)
                        except Exception as e:
                            # one symbol's broker rejection must never abort the
                            # whole cycle — log and move on to the next candidate
                            log.warning(f"[{symbol}] Pending-limit placement failed "
                                        f"({type(e).__name__}: {e}) — skipping this cycle.")
                            continue
                        if not filled_order:
                            continue
                        trade_status = TradeStatus.PENDING
                        actual_fill = limit_price
                        order_type_label = "LIMIT"
                        # fresh placement -> allow a future cancel to notify again
                        try:
                            _nt = str(int(filled_order.get("id", 0)))
                            self._cancel_notified.pop(_nt, None)
                        except Exception:
                            pass
                        self._cancel_notified.pop(f"{symbol}:{side}", None)
                    else:
                        # ATTACK FIX 3: Slippage Check Before Firing (market orders only)
                        live_price = self.data_engine.get_live_price(symbol)
                        slippage_pct = abs(live_price - entry_price) / max(entry_price, 1e-8) * 100
                        if slippage_pct > 0.1:
                            log.warning(f"[{symbol}] Execution Aborted. Price slipped {slippage_pct:.2f}% during sorting queue. R:R invalidated.")
                            continue

                        try:
                            filled_order = self.data_engine.submit_market_order_and_wait(
                                symbol, side, qty, leverage=0, timeout=15,
                                sl=verdict.stop_loss, tp=verdict.take_profit)
                        except Exception as e:
                            log.warning(f"[{symbol}] Market placement failed "
                                        f"({type(e).__name__}: {e}) — skipping this cycle.")
                            continue
                        if not filled_order:
                            continue
                        trade_status = TradeStatus.OPEN
                        try:
                            actual_fill = float(filled_order.get("filled_avg_price", 0.0)) or \
                                self.data_engine.get_live_price(symbol)
                        except Exception:
                            actual_fill = self.data_engine.get_live_price(symbol)
                        order_type_label = "MARKET"

                    try:
                        trade_ticket = int(filled_order.get("id", 0))
                    except (ValueError, TypeError):
                        trade_ticket = 0

                    filled_qty = float(filled_order.get("filled_qty", qty))

                    trade = Trade(
                        id=str(uuid.uuid4())[:8],
                        timestamp_open=datetime.now(timezone.utc),
                        timestamp_close=None,
                        symbol=symbol,
                        bias=signal.bias,
                        entry_price=actual_fill,
                        exit_price=None,
                        position_size_usd=notional,
                        initial_stop_loss=verdict.stop_loss,
                        stop_loss=verdict.stop_loss,
                        status=trade_status,
                        pnl_usd=None,
                        pnl_pct=None,
                        scout_reasoning=signal.reasoning,
                        risk_reasoning=verdict.reasoning,
                        leverage=0,
                        margin_used=verdict.margin_required,
                        filled_qty=filled_qty,
                        trailing_extreme_price=actual_fill,
                        take_profit=verdict.take_profit,
                        ticket=trade_ticket,
                    )
                    # POST-PLACEMENT BOOKKEEPING: DB insert + signals CSV + notifier.
                    # Any failure here must NOT abort the remaining candidates —
                    # the order is already live at the broker, so log and continue.
                    try:
                        self.db.insert_trade(trade)

                        self._append_signal_row({
                            "trade_id": trade.id,
                            "symbol": symbol,
                            "bias": signal.bias.value,
                            "order_type": order_type_label,
                            "ticket": trade_ticket,
                            "signal_ts_utc": self._fmt_dt(signal.timestamp),
                            "confidence": round(signal.confidence, 3),
                            "signal_rr": round(verdict.risk_reward, 2),
                            "limit_entry": round(limit_price, 5) if limit_price > 0 else "",
                            "market_entry": round(signal.entry_price, 5),
                            "fill_ts_utc": self._fmt_dt(datetime.now(timezone.utc)),
                            "fill_price": round(actual_fill, 5),
                            "qty": qty,
                            "initial_sl": round(verdict.stop_loss, 5),
                            "initial_tp": round(verdict.take_profit, 5),
                            "status": trade_status.value,
                        })

                        self.notifier.trade_opened(
                            symbol=symbol, bias=signal.bias.value, entry=actual_fill,
                            sl=verdict.stop_loss, tp=verdict.take_profit,
                            rr=verdict.risk_reward, size=notional, trade_id=trade.id,
                            sl_pct=verdict.sl_distance_pct, order_type=order_type_label,
                        )
                    except Exception as book_err:
                        log.warning(f"[{symbol}] Trade {trade.id} placed but bookkeeping "
                                    f"failed ({type(book_err).__name__}: {book_err}) — "
                                    f"reconciliation will adopt it.")

        finally:
            self._scan_lock.release()

    def _manage_positions(self):
        override_flag = self.db.get_system_flag("manual_override") or "NONE"
        manual_close_all = (override_flag == "CLOSE_ALL")
        manual_close_winners = (override_flag == "CLOSE_WINNERS")
        target_close_symbol = override_flag.replace("CLOSE_SYMBOL_", "") if override_flag.startswith("CLOSE_SYMBOL_") else None
        send_status = (override_flag == "SEND_STATUS")

        # FIX: _check() already verifies terminal_info() under the engine lock
        # (and reconnects if needed); the old direct mt5.terminal_info() call
        # was both redundant and an unlocked MT5 API call.
        if not self.data_engine._check():
            log.warning("[Orchestrator] Terminal disconnected. Suspending position management until connection restores.")
            return

        if send_status:
            try:
                active_trades = self.db.get_active_trades()
                if not active_trades:
                    self.notifier._dispatch("Status", "No active trades", self.notifier.SOUND_SILENT, "<b>ALADIN PRO STATUS</b>\n\n<i>No open positions currently.</i>")
                else:
                    lines = ["<b>ALADIN PRO: LIVE POSITIONS</b>\n"]
                    total_float = 0.0
                    for t in active_trades:
                        sym = str(t.get("symbol", "UNKNOWN"))
                        bias = str(t.get("bias", "UNKNOWN"))
                        entry = float(t.get("entry_price", 0.0))
                        sl = float(t.get("stop_loss", 0.0))
                        try: ticket = int(t.get("ticket", 0) or 0)
                        except: ticket = 0

                        pos = self.data_engine._get_position_by_ticket(ticket) if ticket > 0 else None
                        if not pos:
                            pos_side = "long" if bias == "LONG" else "short"
                            pos = self.data_engine._get_position_obj(sym, pos_side)

                        if pos:
                            live_pnl = float(getattr(pos, 'profit', 0.0))
                            live_price = float(getattr(pos, 'price_current', entry))
                            vol = float(getattr(pos, 'volume', 0.0))
                        else:
                            live_pnl = 0.0
                            try: live_price = float(self.data_engine.get_live_price(sym))
                            except: live_price = entry
                            raw_vol = t.get("filled_qty", 0.0)
                            vol = float(raw_vol) if raw_vol else 0.0

                        total_float += live_pnl
                        sign = "+" if live_pnl > 0 else ""

                        lines.append(f"• <b>{sym} ({bias})</b> | {vol:.2f} Lots")
                        lines.append(f"Entry: <code>{entry:.5f}</code>")
                        lines.append(f"Price: <code>{live_price:.5f}</code>")
                        lines.append(f"Stop Loss: <code>{sl:.5f}</code>")
                        lines.append(f"Floating: <b>{sign}${live_pnl:.2f}</b>\n")

                    try: bal = float(self.data_engine.get_account_balance())
                    except: bal = 0.0

                    sign_tot = "+" if total_float > 0 else ""
                    lines.append(f"<b>Total Floating P&amp;L:</b> <code>{sign_tot}${abs(total_float):.2f}</code>")
                    lines.append(f"<b>Account Balance:</b> <code>${bal:.2f}</code>")

                    self.notifier._dispatch("Live Status", f"Floating: {sign_tot}${abs(total_float):.2f}", self.notifier.SOUND_SILENT, "\n".join(lines))

            except Exception as e:
                log.error(f"[Orchestrator] Status generation failed: {e}")
                self.notifier._dispatch("Status Error", "Failed to compile", self.notifier.SOUND_SILENT, f"<b>STATUS ERROR</b>\nFailed to compile live data. Check system logs.\nError: {e}")
            finally:
                self.db.set_system_flag("manual_override", "NONE")
            return

        if manual_close_all:
            log.warning("EXECUTING EMERGENCY CLOSE OF ALL OPEN POSITIONS...")
            with self.data_engine.lock:
                positions = mt5.positions_get()
            nuked_count = 0
            if positions:
                for pos in positions:
                    if pos.symbol in BOT_CONFIG.exchange.symbols:
                        try:
                            self.data_engine.close_position(pos.symbol, ticket=pos.ticket)
                            nuked_count += 1
                        except Exception as e:
                            log.error(f"Failed to nuke {pos.symbol}: {e}")
            for t in self.db.get_active_trades():
                self.db.update_trade(t["id"], status="CLOSED")
            self.notifier._dispatch("Panic Success", "All positions destroyed.", self.notifier.SOUND_WIN, f"<b>EMERGENCY CLOSE COMPLETE:</b> {nuked_count} positions closed.")
            self.db.set_system_flag("manual_override", "NONE")
            return

        closed_count = 0
        for t in self.db.get_active_trades():
            tid, symbol, bias = t["id"], t["symbol"], t["bias"]
            try: ticket = int(t.get("ticket", 0) or 0)
            except: ticket = 0

            # PENDING limit trades: manage the ORDER lifecycle, not a position.
            if str(t.get("status", "")) == "PENDING":
                # SNIPER EXPIRY: cancel stale resting limits so they don't occupy a
                # max_open_positions slot forever if price never returns to the FVG.
                try:
                    _age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(str(t.get("timestamp_open", "")).replace("Z", "+00:00"))).total_seconds() / 3600
                except Exception:
                    _age_h = 0.0
                _exp_h = BOT_CONFIG.dynamic.limit_expiry_hours
                _cancel_pct = BOT_CONFIG.dynamic.pending_cancel_distance_pct
                with self.data_engine.lock:
                    orders = mt5.orders_get(ticket=ticket) if ticket > 0 else None
                if orders:
                    # MOMENTUM GUARD (pre-fill): if price has run AWAY from the limit
                    # beyond pending_cancel_distance_pct, momentum left and the snipe
                    # pullback isn't coming — cancel now instead of waiting for expiry.
                    _cancel = _exp_h > 0 and _age_h > _exp_h
                    if not _cancel and _cancel_pct > 0:
                        try:
                            _lim = float(orders[0].price_open or 0.0)
                            _live = self.data_engine.get_live_price(symbol)
                            if _lim > 0 and _live > 0:
                                _away = abs(_live - _lim) / _lim * 100
                                if _away > _cancel_pct:
                                    _cancel = True
                                    log.info(f"[{symbol}] Momentum guard: price {_live:.5f} is {_away:.2f}% "
                                             f"away from limit {_lim:.5f} — cancelling snipe (pullback not coming).")
                        except Exception as e:
                            log.warning(f"[{symbol}] Momentum guard check failed: {e}")
                    if _cancel:
                        _canc_reason = ('expired after %.0fh' % _age_h) if (_exp_h > 0 and _age_h > _exp_h) else 'momentum guard — price ran away from the limit'
                        log.info(f"[{symbol}] Pending limit {ticket} cancelled ({_canc_reason}).")
                        try:
                            self.data_engine.cancel_order(str(ticket), symbol)
                        except Exception as e:
                            log.error(f"[{symbol}] Cancel expired limit failed: {e}")
                        self.db.update_trade(tid, status="CANCELLED")
                        try:
                            if self._cancel_notified.get(str(ticket)) != _canc_reason:
                                self.notifier.pending_cancelled(
                                    symbol, bias, float(t.get("entry_price", 0.0) or 0.0), _canc_reason)
                                self._cancel_notified[str(ticket)] = _canc_reason
                            # the old setup is gone — the next placement of this
                            # pair+bias must notify as a NEW order, not be deduped
                            self.notifier.clear_placement_dedup(symbol, bias)
                        except Exception as e:
                            log.warning(f"[{symbol}] Pending-cancel notification failed: {e}")
                    # order still resting & within expiry: nothing to do this cycle
                    continue
                # order is gone -> either filled (position appeared) or cancelled
                pos_side = "long" if bias == "LONG" else "short"
                filled_pos = self.data_engine._get_position_obj(symbol, pos_side) if ticket > 0 else None
                if filled_pos is not None:
                    self.db.update_trade(tid, status="OPEN", ticket=filled_pos.ticket,
                                         entry_price=float(filled_pos.price_open),
                                         filled_qty=float(filled_pos.volume),
                                         trailing_extreme_price=float(filled_pos.price_open))
                    log.info(f"[{symbol}] Pending limit {ticket} FILLED at {filled_pos.price_open}. Trade OPEN.")
                    try:
                        _f_entry = float(filled_pos.price_open)
                        _f_sl = float(t.get("stop_loss", 0.0) or 0.0)
                        _f_tp = float(t.get("take_profit", 0.0) or 0.0)
                        _f_rr = (abs(_f_tp - _f_entry) / abs(_f_entry - _f_sl)
                                 if (_f_tp > 0 and _f_sl > 0 and _f_entry != _f_sl) else 0.0)
                        _f_slpct = (abs(_f_entry - _f_sl) / _f_entry * 100) if _f_sl > 0 else 0.0
                        self.notifier.trade_opened(
                            symbol=symbol, bias=bias, entry=_f_entry,
                            sl=_f_sl, tp=_f_tp,
                            rr=_f_rr, size=float(t.get("position_size_usd", 0.0) or 0.0),
                            trade_id=tid, sl_pct=_f_slpct, order_type="FILLED")
                    except Exception as e:
                        log.warning(f"[{symbol}] Limit-fill notification failed: {e}")
                    continue
                # order vanished with no position -> cancelled (e.g. manual / SL-less)
                self.db.update_trade(tid, status="CANCELLED")
                log.info(f"[{symbol}] Pending limit {ticket} cancelled without fill.")
                try:
                    if self._cancel_notified.get(str(ticket)) != "signal passed":
                        self.notifier.pending_cancelled(
                            symbol, bias, float(t.get("entry_price", 0.0) or 0.0),
                            "cancelled without fill — signal passed")
                        self._cancel_notified[str(ticket)] = "signal passed"
                    self.notifier.clear_placement_dedup(symbol, bias)
                except Exception as e:
                    log.warning(f"[{symbol}] Pending-cancel notification failed: {e}")
                continue

            exchange_pos = self.data_engine._get_position_by_ticket(ticket) if ticket > 0 else None

            if exchange_pos is None:
                pos_side = "long" if bias == "LONG" else "short"
                exchange_pos = self.data_engine._get_position_obj(symbol, pos_side)
                if exchange_pos is not None:
                    ticket = exchange_pos.ticket
                    self.db.update_trade(tid, ticket=ticket)
                    log.info(f"[{symbol}] Healed orphaned DB ticket. New Ticket: {ticket}")

            if exchange_pos is None or float(getattr(exchange_pos, "volume", 0)) <= 0:
                try: price = self.data_engine.get_live_price(symbol)
                except Exception: price = float(t.get("entry_price", 0.0))
                try:
                    self._close_trade(t, price, "Exchange Stop Fill / Sync", api_execute=False)
                except Exception as e:
                    log.warning(f"[{symbol}] Close bookkeeping failed (non-fatal): {e}")
                continue

            floating_profit = getattr(exchange_pos, "profit", 0.0)
            should_close = manual_close_all or (manual_close_winners and floating_profit > 0) or (target_close_symbol and symbol == target_close_symbol)

            if floating_profit > 0:
                milestone_levels = [5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 200] if BOT_CONFIG.exchange.market_type.lower() == "synthetic" else [2, 4, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50]
                achieved = max([m for m in milestone_levels if floating_profit >= m] + [0])
                if achieved > self._milestones.get(ticket, 0):
                    self._milestones[ticket] = achieved
                    self.notifier.profit_milestone(symbol, bias, achieved, floating_profit, str(ticket))

            entry, initial_sl, sl = t["entry_price"], t["initial_stop_loss"], t["stop_loss"]
            try: price = self.data_engine.get_live_price(symbol)
            except Exception: continue

            current_atr, current_ma20 = entry * 0.01, 0.0
            try:
                df = self.data_engine.fetch_ohlcv(symbol, BOT_CONFIG.timeframes.primary, 200)
                if len(df) >= MIN_BARS_FOR_INDICATORS:
                    df = self.data_engine.compute_indicators(df)
                    atr_val = df.iloc[-2].get("atr", np.nan)
                    if not np.isnan(float(atr_val)): current_atr = float(atr_val)
                    ma20_val = df.iloc[-2].get("ma20", np.nan)
                    if not np.isnan(float(ma20_val)): current_ma20 = float(ma20_val)
            except Exception: pass

            current_extreme = t.get("trailing_extreme_price") or entry
            new_sl, updated_extreme = self.trailing.update(
                bias=bias, entry=entry, initial_sl=initial_sl,
                current_price=price, current_sl=sl, current_extreme=current_extreme,
                be_r=BOT_CONFIG.dynamic.tsl_breakeven_r, atr=current_atr,
                chandelier_mult=BOT_CONFIG.dynamic.atr_chandelier_multiplier, ma_fast=current_ma20
            )

            if new_sl is not None:
                self.db.update_trade(tid, stop_loss=new_sl, trailing_extreme_price=updated_extreme)
                qty = float(t.get("filled_qty") or 0)
                if qty > 0 and ticket > 0:
                    trade_side = "buy" if bias == "LONG" else "sell"
                    self._update_trailing_stop(symbol, trade_side, qty, new_sl, ticket=ticket)
                    self.notifier.trailing_stop_moved(symbol, bias, tid, sl, new_sl, price)
            elif updated_extreme != current_extreme:
                self.db.update_trade(tid, trailing_extreme_price=updated_extreme)

            if should_close:
                self._close_trade(t, price, "Telegram C2 Command", api_execute=True)
                closed_count += 1

        if manual_close_winners and closed_count == 0:
            self.notifier._dispatch("No Winners", "No profitable trades found.", self.notifier.SOUND_SILENT, "<b>COMMAND FAILED:</b> No profitable positions found to close.")
        elif target_close_symbol and closed_count == 0:
            self.notifier._dispatch("Not Found", f"{target_close_symbol} not found.", self.notifier.SOUND_SILENT, f"<b>COMMAND FAILED:</b> No active position found for {target_close_symbol}.")

        if override_flag in ("CLOSE_ALL", "CLOSE_WINNERS") or override_flag.startswith("CLOSE_SYMBOL_"):
            self.db.set_system_flag("manual_override", "NONE")

    def _close_trade(self, t: dict, exit_price: float, reason: str = "", api_execute: bool = True):
        entry, bias, tid, symbol, qty = t["entry_price"], t["bias"], t["id"], t["symbol"], float(t.get("filled_qty", 0.01))
        try: ticket = int(t.get("ticket", 0) or 0)
        except: ticket = 0

        close_result = None
        if api_execute:
            if str(t.get("status", "")) == "PENDING" and ticket > 0:
                # cancelling a resting limit order, not closing a position
                try:
                    self.data_engine.cancel_order(str(ticket), symbol)
                    log.info(f"[{symbol}] Pending limit {ticket} cancelled.")
                except Exception as e:
                    log.warning(f"Cancel pending order error {symbol}: {e}")
            else:
                try:
                    pos_side = "long" if bias == "LONG" else "short"
                    close_result = self.data_engine.close_position(symbol, side=pos_side, ticket=ticket)
                except Exception as e: log.warning(f"Close position API error {symbol}: {e}")
        else:
            log.info(f"[{symbol}] Local DB closure only (api_execute=False). Ticket: {ticket}")

        actual_exit = exit_price
        if close_result and isinstance(close_result, dict):
            raw_price = close_result.get("filled_avg_price", exit_price)
            try: actual_exit = float(raw_price) if float(raw_price) > 0 else exit_price
            except Exception: pass

        real_pnl, broker_exit_price = None, None
        if ticket > 0:
            time.sleep(3.0)
            try:
                with self.data_engine.lock:
                    deals = mt5.history_deals_get(position=ticket)
                if deals and len(deals) > 0:
                    real_pnl = sum((getattr(d, 'profit', 0.0) + getattr(d, 'commission', 0.0) + getattr(d, 'swap', 0.0) + getattr(d, 'fee', 0.0)) for d in deals)
                    for d in deals:
                        if d.entry == mt5.DEAL_ENTRY_OUT:
                            broker_exit_price = d.price
                            break
            except Exception as e:
                log.warning(f"[{symbol}] Could not fetch history deals for P&L: {e}")

        if real_pnl is not None:
            pnl_usd = real_pnl
            if broker_exit_price is not None and broker_exit_price > 0: actual_exit = broker_exit_price
            pnl_pct = ((actual_exit - entry) / entry) * 100 if bias == "LONG" else ((entry - actual_exit) / entry) * 100
        else:
            pnl_pct = ((actual_exit - entry) / entry) * 100 if bias == "LONG" else ((entry - actual_exit) / entry) * 100
            # FIX: MT5 API is not thread-safe — take the engine lock for symbol_info
            with self.data_engine.lock:
                info = mt5.symbol_info(symbol)
            if info is not None and info.trade_tick_size > 0:
                points = abs(actual_exit - entry)
                calc_pnl = (points / info.trade_tick_size) * info.trade_tick_value * qty
                pnl_usd = calc_pnl if pnl_pct >= 0 else -calc_pnl
            else:
                pnl_usd = float(t.get("position_size_usd", 10.0)) * (pnl_pct / 100)

        # Mark the trade CLOSED in the DB FIRST. Once we've decided to close,
        # nothing below may prevent the close from being recorded — otherwise
        # reconciliation would retry this same close every cycle forever.
        self.db.update_trade(tid, status="CLOSED", exit_price=round(actual_exit, 5), pnl_usd=round(pnl_usd, 2), pnl_pct=round(pnl_pct, 4), timestamp_close=datetime.now(timezone.utc).isoformat())

        if pnl_usd < 0:
            try:
                cooldown_h = getattr(BOT_CONFIG.dynamic, "cooldown_hours", 2.0) or 2.0
                self._cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(hours=float(cooldown_h))
                log.warning(f"[{symbol}] Trade closed in loss. Cooldown Protocol activated for {cooldown_h} hours.")
            except Exception as e:
                log.warning(f"[{symbol}] Cooldown bookkeeping failed (non-fatal): {e}")

        # SIGNALS LOG: record the outcome so win rate / R / exits are trackable.
        entry_ = float(entry)
        risk_r = abs(entry_ - float(t.get("initial_stop_loss", entry_))) or 1e-9
        r_mult = (actual_exit - entry_) / risk_r if bias == "LONG" else (entry_ - actual_exit) / risk_r
        self._append_signal_row({
            "trade_id": tid,
            "symbol": symbol,
            "bias": bias,
            "order_type": t.get("status", "") == "PENDING" and "LIMIT" or "",
            "ticket": ticket,
            "signal_ts_utc": t.get("timestamp_open", ""),
            "fill_price": round(entry_, 5),
            "qty": qty,
            "initial_sl": round(float(t.get("initial_stop_loss", 0.0)), 5),
            "initial_tp": round(float(t.get("take_profit", 0.0)), 5),
            "close_ts_utc": self._fmt_dt(datetime.now(timezone.utc)),
            "exit_price": round(actual_exit, 5),
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct, 4),
            "R": round(r_mult, 3),
            "exit_reason": reason,
            "status": "CLOSED",
        })

        try:
            if str(t.get("status", "")) == "PENDING":
                # a cancelled resting limit — a 'LOSS $0.00' close message would be
                # wrong/confusing. Notify it as a cancelled setup instead.
                self.notifier.pending_cancelled(
                    symbol, bias, float(entry), reason or "cancelled (conviction / manual)")
                self.notifier.clear_placement_dedup(symbol, bias)
            else:
                self.notifier.trade_closed(symbol=symbol, bias=bias, entry=entry, exit_price=round(actual_exit, 5), pnl_usd=round(pnl_usd, 2), pnl_pct=round(pnl_pct, 2), reason=f"{reason}", trade_id=tid)
        except Exception as e: log.error(f"[{symbol}] Failed to send Telegram notification: {e}")

    def _reconcile_positions(self):
        log.info("[Orchestrator] Starting position reconciliation with broker...")
        if not self.data_engine._check(): return
        with self.data_engine.lock:
            broker_positions = mt5.positions_get()
        if broker_positions is None: return

        broker_dict = {p.ticket: p for p in broker_positions if p.symbol in BOT_CONFIG.exchange.symbols}
        db_trades = self.db.get_active_trades()
        db_tickets = {int(t.get("ticket", 0)): t for t in db_trades if t.get("ticket")}

        for t in db_trades:
            try: ticket = int(t.get("ticket", 0) or 0)
            except: ticket = 0

            # PENDING limit trades store an ORDER ticket, not a position ticket;
            # skip them here (their lifecycle is handled in _manage_positions).
            if str(t.get("status", "")) == "PENDING":
                continue

            if ticket > 0 and ticket not in broker_dict:
                log.info(f"[Orchestrator] Reconciling: DB trade {t['id']} (Ticket {ticket}) missing from broker. Closing internally.")
                try: price = self.data_engine.get_live_price(t["symbol"])
                except Exception: price = float(t.get("entry_price", 0.0))
                try:
                    self._close_trade(t, price, "Exchange Stop Fill (Reconciliation)", api_execute=False)
                except Exception as e:
                    # even if the close bookkeeping fails, keep reconciling the
                    # rest — a single stale trade must not abort the cycle
                    log.warning(f"[Orchestrator] Reconciliation close failed for {t['id']}: {e}")

        for ticket, pos in broker_dict.items():
            if ticket not in db_tickets:
                # FIX: if a DB trade for this symbol is already active (e.g. its
                # ticket was 0 because order-ID capture failed), heal its ticket
                # instead of ingesting a duplicate record for the same position.
                existing = next((t for t in db_trades if t["symbol"] == pos.symbol), None)
                if existing is not None:
                    self.db.update_trade(existing["id"], ticket=ticket)
                    log.info(f"[Orchestrator] Reconciling: healed ticket {ticket} for existing DB trade {existing['id']} ({pos.symbol}).")
                    continue

                log.info(f"[Orchestrator] Reconciling: Found orphaned position {ticket} for {pos.symbol}. Ingesting.")
                bias = Bias.LONG if pos.type == mt5.POSITION_TYPE_BUY else Bias.SHORT

                # FIX: keep MT5 calls under the engine lock
                with self.data_engine.lock:
                    info = mt5.symbol_info(pos.symbol)
                calc_margin = 0.0
                if info and info.trade_tick_size > 0:
                    with self.data_engine.lock:
                        acct = mt5.account_info()
                        calc_margin = mt5.order_calc_margin(pos.type, pos.symbol, pos.volume, pos.price_open)
                    if calc_margin is None: calc_margin = 0.0
                    notional = calc_margin * (acct.leverage if acct else 100)
                else:
                    notional = 1000.0

                initial_sl = pos.sl
                if initial_sl <= 0:
                    try:
                        df_ohlcv = self.data_engine.fetch_ohlcv(pos.symbol, BOT_CONFIG.timeframes.primary, 20)
                        df_ohlcv = self.data_engine.compute_indicators(df_ohlcv)
                        atr_val = float(df_ohlcv.iloc[-2].get("atr", pos.price_open * 0.01))
                    except Exception:
                        atr_val = pos.price_open * 0.01
                    sl_dist = atr_val * BOT_CONFIG.dynamic.sl_atr_multiplier
                    initial_sl = pos.price_open - sl_dist if bias == Bias.LONG else pos.price_open + sl_dist

                trade = Trade(
                    id=str(uuid.uuid4())[:8], timestamp_open=datetime.now(timezone.utc),
                    timestamp_close=None, symbol=pos.symbol, bias=bias,
                    entry_price=pos.price_open, exit_price=None, position_size_usd=notional,
                    initial_stop_loss=initial_sl, stop_loss=initial_sl, status=TradeStatus.OPEN,
                    pnl_usd=None, pnl_pct=None, scout_reasoning="Ingested orphaned position",
                    risk_reasoning="Reconciliation sync", leverage=0, margin_used=calc_margin,
                    filled_qty=pos.volume, trailing_extreme_price=pos.price_open,
                    take_profit=pos.tp, ticket=ticket,
                )
                self.db.insert_trade(trade)

        # FIX: also adopt orphaned PENDING LIMIT orders (our magic) that exist at
        # the broker but have no DB record (e.g. the DB was deleted/recreated).
        # Without this, a wiped DB made the bot see the symbol as "free" and place
        # a SECOND pending limit on it — double-fill risk when both eventually
        # trigger. Adoption runs before any scan, so the symbol is never re-opened.
        try:
            with self.data_engine.lock:
                broker_orders = mt5.orders_get()
        except Exception:
            broker_orders = None
        if broker_orders:
            db_pending = {int(t.get("ticket", 0)): t for t in db_trades
                          if str(t.get("status", "")) == "PENDING" and t.get("ticket")}
            db_syms = {t["symbol"] for t in db_trades}
            # recently-CANCELLED tickets: if a cancel didn't fully clear at the
            # broker, re-adopting it would resurrect the same dead order and
            # re-notify every cycle (the exact bug in the field log).
            _dead_tickets = {
                int(x.get("ticket", 0)) for x in self.db.get_trades(days_back=1)
                if str(x.get("status", "")).upper() == "CANCELLED" and x.get("ticket")
            }
            for od in broker_orders:
                try:
                    if od.symbol not in BOT_CONFIG.exchange.symbols:
                        continue
                    if getattr(od, "magic", 0) != MAGIC:
                        continue
                    if od.ticket in db_pending:
                        continue
                    if od.ticket in _dead_tickets:
                        log.info(f"[Orchestrator] Skipping re-adoption of cancelled ticket {od.ticket} ({od.symbol}).")
                        continue
                    if od.symbol in db_syms:
                        continue   # symbol already tracked — don't double-manage
                    o_type = od.type
                    is_buy = o_type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP)
                    bias = Bias.LONG if is_buy else Bias.SHORT
                    limit_price = float(getattr(od, "price_open", 0) or 0)
                    o_sl = float(getattr(od, "sl", 0) or 0)
                    o_tp = float(getattr(od, "tp", 0) or 0)
                    vol = float(getattr(od, "volume", 0) or 0)
                    notional = limit_price * vol * 100000 if limit_price else 0.0
                    trade = Trade(
                        id=str(uuid.uuid4())[:8], timestamp_open=datetime.now(timezone.utc),
                        timestamp_close=None, symbol=od.symbol, bias=bias,
                        entry_price=limit_price, exit_price=None, position_size_usd=notional,
                        initial_stop_loss=o_sl, stop_loss=o_sl, status=TradeStatus.PENDING,
                        pnl_usd=None, pnl_pct=None, scout_reasoning="Ingested orphaned pending order",
                        risk_reasoning="Reconciliation sync", leverage=0, margin_used=0.0,
                        filled_qty=vol, trailing_extreme_price=limit_price,
                        take_profit=o_tp, ticket=od.ticket,
                    )
                    self.db.insert_trade(trade)
                    log.info(f"[Orchestrator] Adopted orphaned pending order {od.ticket} "
                             f"for {od.symbol} ({'BUY' if is_buy else 'SELL'} @ {limit_price}).")
                except Exception as e:
                    log.warning(f"[Orchestrator] Pending-order adoption failed for "
                                f"{getattr(od, 'ticket', '?')}: {e}")

    def close_trade_by_symbol(self, symbol: str, reason: str = "Manual Close") -> bool:
        active_trades = self.db.get_active_trades()
        trade = next((t for t in active_trades if t["symbol"] == symbol), None)
        if not trade: return False
        price = self.data_engine.get_live_price(symbol)
        if price <= 0: return False
        self._close_trade(trade, price, reason, api_execute=True)
        return True


# --- END OF FILE orchestrator.py ---
