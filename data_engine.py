# --- START OF FILE data_engine.py ---

import logging
import time
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional
from functools import wraps
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from config import BOT_CONFIG
from models import IndicatorSnapshot, SupportResistance, WickRejection
import ta as ta_lib

log = logging.getLogger(__name__)

MIN_BARS_FOR_INDICATORS = 55
MAGIC = 420420

TF_MAP = {
    "1m": mt5.TIMEFRAME_M1,
    "5m": mt5.TIMEFRAME_M5,
    "15m": mt5.TIMEFRAME_M15,
    "30m": mt5.TIMEFRAME_M30,
    "1h": mt5.TIMEFRAME_H1,
    "4h": mt5.TIMEFRAME_H4,
    "1d": mt5.TIMEFRAME_D1,
}


def retry(retries=3, delay=0.5):
    """Decorator to retry functions prone to transient network/broker failures."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries:
                        raise e
                    log.warning(f"[DataEngine] {func.__name__} failed (attempt {attempt}/{retries}): {e}. Retrying in {delay}s...")
                    time.sleep(delay)
        return wrapper
    return decorator


class DataEngine:
    def __init__(self):
        self.lock = threading.RLock() # THE CORE ARCHITECTURAL LOCK

        cfg = BOT_CONFIG.exchange
        init_kw: dict = {}
        if cfg.mt5_login: init_kw["login"] = cfg.mt5_login
        if cfg.mt5_password: init_kw["password"] = cfg.mt5_password
        if cfg.mt5_server: init_kw["server"] = cfg.mt5_server
        if cfg.mt5_path: init_kw["path"] = cfg.mt5_path

        with self.lock:
            if not mt5.initialize(**init_kw):
                raise RuntimeError(f"[DataEngine] MT5 init failed: {mt5.last_error()}")
            acct = mt5.account_info()

        if acct is None:
            raise RuntimeError("[DataEngine] Cannot read MT5 account info")

        mode = "LIVE" if acct.trade_mode == 2 else "DEMO"
        log.info(
            f"[DataEngine] MT5 connected: {acct.server} ({mode}) "
            f"| Login: {acct.login} | Leverage 1:{acct.leverage} "
            f"| Balance: ${acct.balance:,.2f}"
        )

        with self.lock:
            for symbol in cfg.symbols:
                if not mt5.symbol_select(symbol, True):
                    log.warning(f"[DataEngine] Could not select {symbol}")
                else:
                    log.info(f"[DataEngine] {symbol} added to Market Watch")
                time.sleep(0.1)

        self._market_info: Dict[str, dict] = {}
        self._load_market_limits()
        self._cache: Dict[str, pd.DataFrame] = {}
        self._last_fetch: Dict[str, datetime] = {}
        self._last_prices: Dict[str, float] = {}

    def _check(self) -> bool:
        with self.lock:
            if mt5.terminal_info() is not None:
                return True
            log.warning("[DataEngine] MT5 lost — reconnecting…")
            cfg = BOT_CONFIG.exchange
            kw: dict = {}
            if cfg.mt5_login: kw["login"] = cfg.mt5_login
            if cfg.mt5_password: kw["password"] = cfg.mt5_password
            if cfg.mt5_server: kw["server"] = cfg.mt5_server
            if cfg.mt5_path: kw["path"] = cfg.mt5_path
            ok = mt5.initialize(**kw)
            if ok: log.info("[DataEngine] MT5 reconnected")
            return ok

    def shutdown(self):
        with self.lock:
            mt5.shutdown()
        log.info("[DataEngine] MT5 connection closed")

    def _load_market_limits(self):
        with self.lock:
            for symbol in BOT_CONFIG.exchange.symbols:
                info = mt5.symbol_info(symbol)
                if info is None:
                    self._market_info[symbol] = self._default_market_info()
                    continue

                min_not = info.volume_min * info.trade_contract_size * (info.ask or info.bid or 1)
                self._market_info[symbol] = {
                    "volume_min": info.volume_min,
                    "volume_max": info.volume_max,
                    "volume_step": info.volume_step,
                    "contract_size": info.trade_contract_size,
                    "point": info.point,
                    "digits": info.digits,
                    "min_notional": max(1.0, min_not),
                    "filling_mode": info.filling_mode,
                    "tick_value": info.trade_tick_value,
                    "tick_size": info.trade_tick_size,
                }

    @staticmethod
    def _default_market_info() -> dict:
        return {
            "volume_min": 0.01, "volume_max": 100.0,
            "volume_step": 0.01, "contract_size": 100000,
            "point": 0.00001, "digits": 5,
            "min_notional": 1.0, "filling_mode": 2,
            "tick_value": 1.0, "tick_size": 0.00001,
        }

    def _filling(self, symbol: str) -> int:
        with self.lock:
            info = mt5.symbol_info(symbol)

        if info is None:
            return mt5.ORDER_FILLING_FOK

        fill_mode = info.filling_mode
        if (fill_mode & 1): return mt5.ORDER_FILLING_FOK
        elif (fill_mode & 2): return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _calc_deviation(self, info, price: float) -> int:
        """Deviation (in points) bounded to sane limits regardless of symbol.

        FIX: the old close-position code did `30 * (point / 0.00001)`, which
        exploded to 3,000 points on JPY pairs and 300,000 on indices (US30),
        allowing fills far from the requested price. Here we scale from the
        configured max slippage % but clamp to [20, 500] points."""
        max_slip_pct = BOT_CONFIG.dynamic.max_slippage_pct / 100.0
        if info is not None and info.point > 0 and price > 0:
            dev = int((price * max_slip_pct) / info.point)
            return max(20, min(dev, 500))
        return 30

    def get_account_info(self) -> dict:
        self._check()
        with self.lock:
            a = mt5.account_info()

        if a is None: return {}
        return {
            "buying_power": a.margin_free,
            "total_equity": a.equity,
            "margin_used": a.margin,
            "portfolio_value": a.equity,
            "balance": a.balance,
            "leverage": a.leverage,
            "profit": a.profit,
        }

    def get_account_balance(self) -> float:
        return float(self.get_account_info().get("total_equity", 0))

    def is_market_open(self, symbol: str) -> bool:
        if BOT_CONFIG.exchange.market_type.lower() == "synthetic":
            return True

        crypto_keywords = ["BTC", "ETH", "SOL", "LTC", "XRP", "DOGE", "ADA", "DOT"]
        if any(crypto in symbol.upper() for crypto in crypto_keywords):
            return True

        # BROKER-AUTHORITATIVE: if MT5 says the market is closed (no live tick),
        # believe the broker over our weekday heuristic. This prevents rc=10018
        # (Market closed) when Exness closes FX for the weekend / holidays before
        # our heuristic would.
        try:
            with self.lock:
                tick = mt5.symbol_info_tick(symbol)
            if tick is None or (tick.bid == 0 and tick.ask == 0):
                return False
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        wd = now.weekday()
        hr = now.hour

        if wd == 4 and hr >= 22: return False
        if wd == 5: return False
        if wd == 6 and hr < 21: return False

        return True

    def get_live_price(self, symbol: str) -> float:
        with self.lock:
            tick = mt5.symbol_info_tick(symbol)

        if tick is not None:
            mid = (tick.bid + tick.ask) / 2
            if mid > 0:
                self._last_prices[symbol] = mid
                return mid
        return self._last_prices.get(symbol, 0.0)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        ck = f"{symbol}_{timeframe}"
        now = datetime.now(timezone.utc)
        if (
            ck in self._last_fetch
            and (now - self._last_fetch[ck]).total_seconds() < 10
            and len(self._cache.get(ck, [])) >= limit
        ):
            return self._cache[ck].iloc[-limit:].copy()

        mt5_tf = TF_MAP.get(timeframe)
        if mt5_tf is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        with self.lock:
            rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, min(limit + 50, 5000))

        if rates is None or len(rates) == 0:
            raise ValueError(f"No data for {symbol} {timeframe}")

        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        for drop_col in ("time", "spread", "real_volume"):
            if drop_col in df.columns:
                df.drop(columns=[drop_col], inplace=True)
        df.set_index("timestamp", inplace=True)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.dropna(subset=["open", "high", "low", "close"], inplace=True)

        self._cache[ck] = df
        self._last_fetch[ck] = now
        return df.copy()

    def get_total_floating_pnl(self) -> float:
        """Calculates true unrealized risk across the entire portfolio securely."""
        with self.lock:
            positions = mt5.positions_get()
            if not positions:
                return 0.0
            return sum(float(getattr(p, 'profit', 0.0)) for p in positions)

    def notional_to_qty(self, symbol: str, notional_usd: float, price: float) -> float:
        if price <= 0:
            return 0.0

        with self.lock:
            acct = mt5.account_info()
            margin_1_lot = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol, 1.0, price)

        lev = acct.leverage if acct else 100

        if margin_1_lot is not None and margin_1_lot > 0:
            notional_1_lot = margin_1_lot * lev
            return notional_usd / notional_1_lot

        log.critical(f"[DataEngine] order_calc_margin failed for {symbol}.")
        return 0.0

    def _clamp_order(self, symbol: str, qty: float) -> Optional[float]:
        info = self._market_info.get(symbol, {})
        v_min = info.get("volume_min", 0.01)
        v_max = info.get("volume_max", 100.0)
        v_step = info.get("volume_step", 0.01)

        if qty > v_max: qty = v_max
        if qty < v_min: return None

        qty = round(round(qty / v_step) * v_step, 8)
        return qty if qty > 0 else None

    # ── market orders ──────────────────────────────────────────

    @retry(retries=3, delay=0.5)
    def submit_market_order(self, symbol: str, side: str, qty: float, sl: float = 0.0, tp: float = 0.0) -> dict:
        qty = self._clamp_order(symbol, qty)
        if qty is None:
            raise ValueError(f"Order qty invalid for {symbol}")

        self._check()
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL

        with self.lock:
            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)

        if tick is None:
            raise ValueError(f"No tick for {symbol}")

        price = tick.ask if side == "buy" else tick.bid

        dynamic_deviation = self._calc_deviation(info, price)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": qty,
            "type": order_type,
            "price": price,
            "deviation": dynamic_deviation,
            "magic": MAGIC,
            "comment": "ALADIN",
            "type_filling": self._filling(symbol),
        }

        if sl > 0: request["sl"] = sl
        if tp > 0: request["tp"] = tp

        log.info(f"[DataEngine] Market {side} {qty} {symbol} @ {price} | SL: {sl} | TP: {tp}")

        with self.lock:
            result = mt5.order_send(request)

        if result is None:
            raise RuntimeError(f"order_send None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # transient rejections (off quotes / requote / market closed): skip
            # gracefully and retry next cycle, not fatal
            if result.retcode in (10006, 10017, 10018):
                log.warning(f"[DataEngine] rc={result.retcode} for {symbol} — market order skipped (retry next cycle).")
                return None
            raise RuntimeError(f"order_send rejected: rc={result.retcode} {result.comment}")

        fill = result.price if result.price and result.price > 0 else price
        return {
            "id": str(result.order),
            "status": "closed",
            "symbol": symbol,
            "side": side,
            "type": "market",
            "qty": str(qty),
            "filled_qty": str(result.volume or qty),
            "filled_avg_price": str(fill),
            "stop_price": str(sl),
            "leverage": 0,
        }

    def submit_market_order_and_wait(
        self, symbol: str, side: str, qty: float, leverage: int = 10,
        timeout: float = 15.0, sl: float = 0.0, tp: float = 0.0
    ) -> Optional[dict]:
        try:
            return self.submit_market_order(symbol, side, qty, sl, tp)
        except Exception as e:
            log.error(f"[DataEngine] Market order failed {symbol}: {e}")
            return None

    # ── pending limit orders (SMC limit entries) ─────────────────

    @retry(retries=3, delay=0.5)
    def submit_pending_limit(self, symbol: str, side: str, qty: float,
                             limit_price: float, sl: float = 0.0, tp: float = 0.0) -> dict:
        """Place a GTC pending limit order at the FVG gap edge (SMC execution).
        Returns the order ticket; fill is detected by reconciliation."""
        qty = self._clamp_order(symbol, qty)
        if qty is None:
            raise ValueError(f"Order qty invalid for {symbol}")

        self._check()
        with self.lock:
            info = mt5.symbol_info(symbol)
            tick = mt5.symbol_info_tick(symbol)
        digits = info.digits if info else 5

        # PRE-FLIGHT SIDE CHECK: a BUY limit must rest BELOW the market, a SELL
        # limit ABOVE it. If the FVG level was already passed by the time we
        # place (stale setup), the broker would reject with rc=10015 — skip
        # now with a clear log instead of hitting the broker (no retry-hammer).
        if tick is not None and (tick.bid > 0 or tick.ask > 0):
            if side == "buy" and limit_price >= (tick.ask or tick.bid):
                log.warning(f"[DataEngine] {symbol} BUY limit {limit_price} at/above market "
                            f"({tick.ask:.5f}) — FVG level already passed, skipping (retry next cycle).")
                return None
            if side == "sell" and limit_price <= (tick.bid or tick.ask):
                log.warning(f"[DataEngine] {symbol} SELL limit {limit_price} at/below market "
                            f"({tick.bid:.5f}) — FVG level already passed, skipping (retry next cycle).")
                return None

        order_type = mt5.ORDER_TYPE_BUY_LIMIT if side == "buy" else mt5.ORDER_TYPE_SELL_LIMIT
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": qty,
            "type": order_type,
            "price": round(limit_price, digits),
            "magic": MAGIC,
            "comment": "ALADIN_LIMIT",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        if sl > 0: request["sl"] = round(sl, digits)
        if tp > 0: request["tp"] = round(tp, digits)

        log.info(f"[DataEngine] Pending limit {side} {qty} {symbol} @ {round(limit_price, digits)} | SL: {sl} | TP: {tp}")

        with self.lock:
            result = mt5.order_send(request)

        if result is None:
            raise RuntimeError(f"order_send None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # Transient broker rejections are normal states, not errors — skip this
            # placement and let the next cycle retry:
            #   10018 market closed (weekend/holiday)
            #   10006 off quotes / 10017 requote — no live quotes for the symbol at
            #        this instant (thin-liquidity pairs at session boundaries, gaps)
            #   10015 invalid price / 10030 invalid stops — the setup level or
            #        SL/TP was rejected by the broker; won't self-heal in 1.5s,
            #        re-evaluate next cycle instead of hammering retries
            if result.retcode in (10006, 10015, 10017, 10018, 10030):
                reason = {10006: "off quotes", 10015: "invalid price",
                          10017: "requote", 10018: "market closed",
                          10030: "invalid stops"}.get(result.retcode, f"rc={result.retcode}")
                log.warning(f"[DataEngine] {reason} for {symbol} — pending limit skipped (retry next cycle).")
                return None
            raise RuntimeError(f"pending limit rejected: rc={result.retcode} {result.comment}")

        return {
            "id": str(result.order),
            "status": "pending",
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "qty": str(qty),
            "filled_qty": "0",
            "filled_avg_price": str(round(limit_price, digits)),
            "stop_price": str(limit_price),
            "leverage": 0,
        }

    # ── position queries ───────────────────────────────────────

    def _get_position_by_ticket(self, ticket: int):
        with self.lock:
            positions = mt5.positions_get()

        if positions:
            for p in positions:
                if p.ticket == ticket:
                    return p
        return None

    def _get_position_obj(self, symbol: str, side: str = None):
        with self.lock:
            positions = mt5.positions_get(symbol=symbol)

        if not positions: return None
        for p in positions:
            if side:
                ps = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
                if ps == side: return p
            else:
                return p
        return None

    # ── stop loss / tp modification (trailing logic) ───────────

    @retry(retries=3, delay=0.5)
    def submit_stop_order(self, symbol: str, side: str, qty: float, stop_price: float, ticket: int = 0) -> dict:
        pos = self._get_position_by_ticket(ticket) if ticket > 0 else self._get_position_obj(symbol)
        if pos is None:
            # position already closed (SL/TP filled) — nothing to update; skip
            # gracefully instead of raising (which the @retry would hammer 3x)
            log.info(f"[DataEngine] No position for SL update ticket {ticket} — already closed, skipping.")
            return None

        digits = self._market_info.get(symbol, {}).get("digits", 5)
        sl = round(stop_price, digits)

        current_sl = round(pos.sl, digits) if pos.sl else 0.0
        if abs(current_sl - sl) < self._market_info.get(symbol, {}).get("point", 0.00001):
            return {"id": str(pos.ticket), "stop_price": str(current_sl)}

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": pos.ticket,
            "sl": sl,
            "tp": pos.tp,
        }

        with self.lock:
            result = mt5.order_send(request)

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"SL mod failed: {result.comment if result else mt5.last_error()}")

        return {"id": str(pos.ticket), "stop_price": str(sl)}

    @retry(retries=3, delay=0.5)
    def submit_tp_order(self, symbol: str, side: str, qty: float, tp_price: float, ticket: int = 0) -> dict:
        pos = self._get_position_by_ticket(ticket) if ticket > 0 else self._get_position_obj(symbol)
        if pos is None:
            log.info(f"[DataEngine] No position for TP update ticket {ticket} — already closed, skipping.")
            return None

        digits = self._market_info.get(symbol, {}).get("digits", 5)
        tp = round(tp_price, digits)

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": pos.ticket,
            "sl": pos.sl,
            "tp": tp,
        }

        with self.lock:
            result = mt5.order_send(request)

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"TP mod failed: {result.comment if result else mt5.last_error()}")

        return {"id": str(pos.ticket), "stop_price": str(tp)}

    @retry(retries=3, delay=0.5)
    def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            ticket = int(order_id)
        except (ValueError, TypeError):
            return True

        with self.lock:
            orders = mt5.orders_get(ticket=ticket)
            if orders and len(orders) > 0:
                result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
                if result and result.retcode == mt5.TRADE_RETCODE_DONE: return True
                raise RuntimeError(f"Cancel order failed: {result.comment if result else mt5.last_error()}")
        return True

    def cancel_conditional_orders(self, symbol: str):
        with self.lock:
            orders = mt5.orders_get(symbol=symbol)
            if not orders: return
            for o in orders:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})

    @retry(retries=3, delay=0.5)
    def close_position(self, symbol: str, side: str = None, ticket: int = 0) -> Optional[dict]:
        self._check()
        pos = self._get_position_by_ticket(ticket) if ticket > 0 else self._get_position_obj(symbol, side)

        if pos is None:
            log.warning(f"[DataEngine] No position to close for ticket {ticket}")
            return None

        with self.lock:
            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)

        if tick is None:
            raise ValueError(f"No tick data available for {symbol}")

        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            # FIX: bounded deviation (see _calc_deviation) instead of the old
            # `30 * (point / 0.00001)` which blew up to 3,000+ points on JPY/indices.
            "deviation": self._calc_deviation(info, price),
            "magic": MAGIC,
            "comment": "ALADIN_CLOSE",
            "type_filling": self._filling(symbol),
        }

        with self.lock:
            result = mt5.order_send(request)

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"Close failed: {result.comment if result else mt5.last_error()}")

        fill = result.price if result.price and result.price > 0 else price
        return self._close_result(pos, fill, result.volume or pos.volume)

    def _close_result(self, pos, fill_price, fill_vol) -> dict:
        side = "sell" if pos.type == mt5.POSITION_TYPE_BUY else "buy"
        return {
            "id": str(pos.ticket),
            "status": "closed",
            "symbol": pos.symbol,
            "side": side,
            "type": "market",
            "qty": str(pos.volume),
            "filled_qty": str(fill_vol),
            "filled_avg_price": str(fill_price),
            "stop_price": "",
            "reduce_only": True,
        }

    # ═══════════════════════════════════════════════════════════
    #  INDICATORS (PURE PANDAS MATH - NO MT5 LOCK REQUIRED HERE)
    # ═══════════════════════════════════════════════════════════

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        dyn = BOT_CONFIG.dynamic
        df = df.copy()

        try:
            df["ma20"] = ta_lib.trend.EMAIndicator(close=df["close"], window=dyn.ma_fast).ema_indicator()
            df["ma50"] = ta_lib.trend.EMAIndicator(close=df["close"], window=dyn.ma_slow).ema_indicator()
            df["rsi"] = ta_lib.momentum.RSIIndicator(close=df["close"], window=dyn.rsi_length).rsi()

            macd = ta_lib.trend.MACD(
                close=df["close"],
                window_fast=dyn.macd_fast,
                window_slow=dyn.macd_slow,
                window_sign=dyn.macd_signal,
            )
            df["MACD"] = macd.macd()
            df["MACDs"] = macd.macd_signal()
            df["MACDh"] = macd.macd_diff()

            bb = ta_lib.volatility.BollingerBands(close=df["close"], window=dyn.bb_length, window_dev=dyn.bb_std)
            df["BBU"] = bb.bollinger_hband()
            df["BBM"] = bb.bollinger_mavg()
            df["BBL"] = bb.bollinger_lband()
            df["bb_pct_b"] = bb.bollinger_pband()

            df["atr"] = ta_lib.volatility.AverageTrueRange(
                high=df["high"], low=df["low"], close=df["close"], window=14
            ).average_true_range()

            adx_ind = ta_lib.trend.ADXIndicator(
                high=df["high"], low=df["low"], close=df["close"], window=14
            )
            df["adx"] = adx_ind.adx()
            df["di_plus"] = adx_ind.adx_pos()
            df["di_minus"] = adx_ind.adx_neg()

            df["vol_avg"] = df["volume"].rolling(20).mean()
            df["vol_ratio"] = df["volume"] / (df["vol_avg"] + 1e-10)
        except Exception as e:
            log.warning(f"Indicator calculation error: {e}")

        return df

    def detect_wick_rejections(self, df, sr_levels, lookback=3):
        dyn = BOT_CONFIG.dynamic
        rejections = []
        levels = [{"price": sr.level, "type": sr.level_type} for sr in sr_levels]
        if "BBU" in df.columns:
            bbu_val = float(df.iloc[-2].get("BBU", 0))
            bbl_val = float(df.iloc[-2].get("BBL", 0))
            if not np.isnan(bbu_val): levels.append({"price": bbu_val, "type": "bb_upper"})
            if not np.isnan(bbl_val): levels.append({"price": bbl_val, "type": "bb_lower"})

        if len(df) < lookback: return rejections
        for i in range(max(0, len(df) - lookback), len(df) - 1):
            row = df.iloc[i]
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]
            if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c): continue
            body, fr = abs(c - o), h - l
            if fr <= 0 or body <= 0: continue
            uw, lw = h - max(c, o), min(c, o) - l

            if lw / body >= dyn.min_wick_ratio and lw / fr >= dyn.min_wick_pct_of_range:
                for lvl in levels:
                    if lvl["type"] in ("support", "bb_lower") and lvl["price"] > 0:
                        if abs(l - lvl["price"]) / lvl["price"] * 100 <= dyn.wick_proximity_pct:
                            rejections.append(WickRejection(
                                df.index[i].to_pydatetime(), "bullish",
                                float(c), float(lw), float(body), round(lw / body, 2), round(lw / fr, 2),
                                float(lvl["price"]), lvl["type"], float(l), float(h),
                            ))
                            break
            if uw / body >= dyn.min_wick_ratio and uw / fr >= dyn.min_wick_pct_of_range:
                for lvl in levels:
                    if lvl["type"] in ("resistance", "bb_upper") and lvl["price"] > 0:
                        if abs(h - lvl["price"]) / lvl["price"] * 100 <= dyn.wick_proximity_pct:
                            rejections.append(WickRejection(
                                df.index[i].to_pydatetime(), "bearish",
                                float(c), float(uw), float(body), round(uw / body, 2), round(uw / fr, 2),
                                float(lvl["price"]), lvl["type"], float(l), float(h),
                            ))
                            break
        return rejections

    def detect_bb_touch(self, df):
        if "bb_pct_b" not in df.columns or len(df) < 2: return None
        p = df["bb_pct_b"].iloc[-2]
        if pd.isna(p): return None
        if p <= BOT_CONFIG.dynamic.bb_touch_threshold: return "lower"
        if p >= BOT_CONFIG.dynamic.bb_upper_threshold: return "upper"
        return None

    def find_support_resistance(self, df, window=10, num_levels=5):
        if len(df) < (2 * window + 1): return []
        levels = []
        for i in range(window, len(df) - window):
            if pd.isna(df["high"].iloc[i]) or pd.isna(df["low"].iloc[i]): continue
            if df["high"].iloc[i] == df["high"].iloc[i - window : i + window + 1].max():
                levels.append({"level": float(df["high"].iloc[i]), "type": "resistance", "time": df.index[i].to_pydatetime()})
            if df["low"].iloc[i] == df["low"].iloc[i - window : i + window + 1].min():
                levels.append({"level": float(df["low"].iloc[i]), "type": "support", "time": df.index[i].to_pydatetime()})
        if not levels: return []
        clustered = self._cluster_levels(levels, BOT_CONFIG.dynamic.pivot_cluster_pct)
        clustered.sort(key=lambda x: x.strength, reverse=True)
        return clustered[:num_levels]

    def _cluster_levels(self, raw, tol_pct):
        if not raw: return []
        ordered = sorted(raw, key=lambda x: x["level"])
        clusters, cur = [], [ordered[0]]
        for item in ordered[1:]:
            avg = np.mean([c["level"] for c in cur])
            if abs(item["level"] - avg) / avg * 100 < tol_pct:
                cur.append(item)
            else:
                clusters.append(cur)
                cur = [item]
        clusters.append(cur)
        return [
            SupportResistance(
                round(float(np.mean([c["level"] for c in cl])), 2),
                len(cl),
                max(set(c["type"] for c in cl), key=lambda t: sum(1 for c in cl if c["type"] == t)),
                max(c["time"] for c in cl),
            )
            for cl in clusters
        ]

    def build_snapshot(self, symbol: str) -> IndicatorSnapshot:
        df = self.fetch_ohlcv(symbol, BOT_CONFIG.timeframes.primary, 200)
        df = self.compute_indicators(df)
        r_closed = df.iloc[-2] if len(df) > 1 else df.iloc[-1]
        p = self.get_live_price(symbol)
        p = p if p > 0 else float(r_closed["close"])

        def _safe(col, fallback=0.0):
            val = r_closed.get(col, fallback)
            if isinstance(val, float) and np.isnan(val): return fallback
            return float(val)

        return IndicatorSnapshot(
            timestamp=datetime.now(timezone.utc), price=p,
            rsi=_safe("rsi", 50.0),
            macd_line=_safe("MACD", 0.0), macd_signal=_safe("MACDs", 0.0),
            macd_histogram=_safe("MACDh", 0.0),
            bb_upper=_safe("BBU", p * 1.02), bb_middle=_safe("BBM", p),
            bb_lower=_safe("BBL", p * 0.98), bb_pct_b=_safe("bb_pct_b", 0.5),
            ma20=_safe("ma20", p), ma50=_safe("ma50", p),
            atr=_safe("atr", p * 0.01),
            volume_current=float(r_closed["volume"] if not pd.isna(r_closed.get("volume", np.nan)) else 0.0),
            volume_avg=_safe("vol_avg", 0.0), volume_ratio=_safe("vol_ratio", 1.0),
        )
