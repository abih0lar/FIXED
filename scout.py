# --- START OF FILE scout.py ---

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple, List
import numpy as np
import pandas as pd

from config import BOT_CONFIG
from data_engine import DataEngine
from database import Database
from models import Bias, IndicatorSnapshot, MonologueEntry, ScoutSignal, SignalStrength

log = logging.getLogger(__name__)


class Scout:

    def __init__(self, data_engine: DataEngine, db: Database):
        self.data = data_engine
        self.db = db

    def get_dealing_range(self, df: pd.DataFrame, lookback: int = 120):
        """Calculates the absolute Premium/Discount Matrix over the dealing range."""
        if len(df) < lookback: return None, None, None
        window = df.tail(lookback)
        swing_high = float(window["high"].max())
        swing_low = float(window["low"].min())
        equilibrium = (swing_high + swing_low) / 2.0
        return swing_high, swing_low, equilibrium

    @staticmethod
    def _dedupe_fractals(pivots: List[dict]) -> List[dict]:
        """F1-FIX: consecutive candles on a plateau all equal the rolling max/min,
        producing duplicate fractal entries (e.g. sweep high 105.6 twice). Keep only
        the FIRST occurrence of each repeated price so 'second-to-last pivot' logic
        (liquidity targets / sweeps) doesn't pick the duplicate of the current level."""
        out = []
        last_price = None
        for p in pivots:
            price = float(p["price"])
            if last_price is None or abs(price - last_price) > 1e-12:
                out.append(p)
            last_price = price
        return out

    def _get_fractal_swings(self, df: pd.DataFrame, window: int = 3) -> Tuple[List[dict], List[dict]]:
        """THE ARCHITECT FIX: True Fractal Pivot Detection for exact Market Structure."""
        df_copy = df.copy()
        # Find local peaks and troughs mathematically
        df_copy['is_high'] = df_copy['high'] == df_copy['high'].rolling(window=2*window+1, center=True).max()
        df_copy['is_low'] = df_copy['low'] == df_copy['low'].rolling(window=2*window+1, center=True).min()

        highs = [{"idx": i, "price": row['high']} for i, row in df_copy[df_copy['is_high']].iterrows()]
        lows = [{"idx": i, "price": row['low']} for i, row in df_copy[df_copy['is_low']].iterrows()]

        return self._dedupe_fractals(highs), self._dedupe_fractals(lows)

    def detect_structure(self, df: pd.DataFrame, lookback: int = 100):
        """Evaluates HH/HL or LH/LL sequence using Fractal Swings to pinpoint BOS/CHoCH."""
        if len(df) < lookback + 10: return "Neutral", "None", None

        highs, lows = self._get_fractal_swings(df.tail(lookback))
        if len(highs) < 2 or len(lows) < 2:
            # Fallback to EMA momentum if structure is perfectly consolidating
            ema = df["close"].ewm(span=20, adjust=False).mean()
            return "Bullish" if df["close"].iloc[-1] > ema.iloc[-1] else "Bearish", "None", None

        last_h, prev_h = highs[-1]['price'], highs[-2]['price']
        last_l, prev_l = lows[-1]['price'], lows[-2]['price']
        current_close = float(df["close"].iloc[-1])

        is_uptrend = last_h > prev_h and last_l > prev_l
        is_downtrend = last_h < prev_h and last_l < prev_l

        if is_uptrend:
            if current_close > last_h: return "Bullish", "BOS", last_h
            if current_close < last_l: return "Bearish", "CHoCH", last_l
            return "Bullish", "None", None

        elif is_downtrend:
            if current_close < last_l: return "Bearish", "BOS", last_l
            if current_close > last_h: return "Bullish", "CHoCH", last_h
            return "Bearish", "None", None

        ema = df["close"].ewm(span=20, adjust=False).mean()
        return "Bullish" if current_close > ema.iloc[-1] else "Bearish", "None", None

    def detect_choch_override(self, df: pd.DataFrame, expected_bias: str, lookback: int = 15):
        """Fast-twitch reversal detection for violent V-bottoms/tops."""
        if len(df) < lookback + 5: return False, None
        past = df.iloc[-(lookback):-3]
        if past.empty: return False, None

        sh = float(past["high"].max())
        sl = float(past["low"].min())

        prior_10 = df.iloc[-13:-3]
        if len(prior_10) < 5: return False, None
        avg_body = float((prior_10["close"] - prior_10["open"]).abs().mean())

        last3 = df.tail(3)
        for i in range(len(last3)):
            candle = last3.iloc[i]
            body_size = abs(float(candle["close"]) - float(candle["open"]))
            is_bullish = float(candle["close"]) > float(candle["open"])
            is_bearish = float(candle["close"]) < float(candle["open"])

            if expected_bias == "Bullish":
                body_close = float(candle["close"]) > sh
                disp = body_size >= (avg_body * 1.5) and is_bullish
                if body_close and disp: return True, "Bullish"

            elif expected_bias == "Bearish":
                body_close = float(candle["close"]) < sl
                disp = body_size >= (avg_body * 1.5) and is_bearish
                if body_close and disp: return True, "Bearish"

        return False, None

    def detect_liquidity_sweep(self, df: pd.DataFrame, direction: str, lookback: int = 50, recent_window: int = 5):
        """F1-FIX: verifies if price swept a specific Fractal Pivot before reversing.

        Old behaviour missed real sweeps because:
          (a) plateau candles created duplicate fractals, so highs[-2] was often the
              sweep level itself and `recent.max > target` failed;
          (b) `if not highs or not lows` gated BOTH branches although a bearish sweep
              only needs fractal highs (and vice versa);
          (c) the target was not restricted to pivots BEFORE the recent window.
        Now: target = the extreme pivot strictly before the recent window."""
        df_sub = df.tail(lookback + recent_window)
        highs, lows = self._get_fractal_swings(df_sub)
        recent_df = df.tail(recent_window)
        if len(recent_df) == 0:
            return False, None

        cutoff = df_sub.index[-recent_window]  # first timestamp of the recent window

        if direction == "Bearish":
            prior = [h for h in highs if h["idx"] < cutoff]
            if not prior:
                return False, None
            target_high = max(float(h["price"]) for h in prior)
            if float(recent_df["high"].max()) > target_high and \
               float(recent_df["close"].iloc[-1]) < target_high:
                return True, target_high
            return False, None

        elif direction == "Bullish":
            prior = [l for l in lows if l["idx"] < cutoff]
            if not prior:
                return False, None
            target_low = min(float(l["price"]) for l in prior)
            if float(recent_df["low"].min()) < target_low and \
               float(recent_df["close"].iloc[-1]) > target_low:
                return True, target_low
            return False, None

        return False, None

    def find_fvg(self, df: pd.DataFrame, direction: str, lookback: int = 40):
        """Vectorized FVG detection supporting Consequent Encroachment (50% Mitigation Tolerance)."""
        df_sub = df.iloc[-lookback:].copy()
        df_sub['c1_high'] = df_sub['high'].shift(2)
        df_sub['c1_low'] = df_sub['low'].shift(2)

        # Track the absolute extremes that hit the gap in the future
        df_sub['fut_low_min'] = df_sub['low'].iloc[::-1].cummin().iloc[::-1].shift(-1)
        df_sub['fut_high_max'] = df_sub['high'].iloc[::-1].cummax().iloc[::-1].shift(-1)

        candidates = []
        for i in range(2, len(df_sub) - 1):
            c3_l, c3_h = df_sub['low'].iloc[i], df_sub['high'].iloc[i]
            c1_h, c1_l = df_sub['c1_high'].iloc[i], df_sub['c1_low'].iloc[i]

            fvg_top, fvg_bot = None, None

            if direction == "Bullish" and c3_l > c1_h:
                fvg_top, fvg_bot = c3_l, c1_h
                gap_size = fvg_top - fvg_bot
                # Allowed to mitigate up to 50% of the gap
                allowed_mitigation = fvg_top - (gap_size * 0.5)
                if df_sub['fut_low_min'].iloc[i] > allowed_mitigation:
                    candidates.append((abs(fvg_top - fvg_bot), fvg_top, fvg_bot))

            elif direction == "Bearish" and c3_h < c1_l:
                fvg_top, fvg_bot = c1_l, c3_h
                gap_size = fvg_bot - fvg_top
                allowed_mitigation = fvg_bot + (gap_size * 0.5)
                if df_sub['fut_high_max'].iloc[i] < allowed_mitigation:
                    candidates.append((abs(fvg_top - fvg_bot), fvg_top, fvg_bot))

        if candidates:
            if direction == "Bullish": candidates.sort(key=lambda x: x[1], reverse=True)
            else: candidates.sort(key=lambda x: x[2], reverse=False)
            return candidates[0][1], candidates[0][2]
        return None

    def find_order_block(self, df: pd.DataFrame, fvg_top: float, fvg_bottom: float, direction: str, lookback: int = 40) -> Optional[Tuple[float, float]]:
        """Finds the true structural origin (Wick Extreme) of the move, not just a random Doji."""
        df_sub = df.iloc[-lookback:].copy()

        for i in range(len(df_sub) - 2, 0, -1):
            candle = df_sub.iloc[i]
            # Identify the cluster of candles immediately prior to the FVG
            if direction == "Bullish" and float(candle['low']) <= fvg_bottom:
                # Return the absolute extreme low of this 3-candle cluster to place the SL safely
                cluster = df_sub.iloc[max(0, i-2):i+1]
                return float(cluster['high'].max()), float(cluster['low'].min())

            elif direction == "Bearish" and float(candle['high']) >= fvg_top:
                cluster = df_sub.iloc[max(0, i-2):i+1]
                return float(cluster['high'].max()), float(cluster['low'].min())
        return None

    def find_liquidity_targets(self, df: pd.DataFrame, bias: str, price: float, lookback: int = 120):
        """Targets External Liquidity (Unswept Fractal Swings) instead of ghost Order Blocks."""
        highs, lows = self._get_fractal_swings(df.tail(lookback))
        targets = []

        if bias == "Bullish":
            for h in highs:
                if h['price'] > price: targets.append(h['price'])
        elif bias == "Bearish":
            for l in lows:
                if l['price'] < price: targets.append(l['price'])

        targets.sort(key=lambda x: abs(x - price))
        return targets

    # ═══════════════════════════════════════════════════════════
    #  MAIN ALADIN SCANNER FUSION
    # ═══════════════════════════════════════════════════════════

    def scan(self, symbol: str) -> ScoutSignal:
        log_msgs: list[str] = []
        try:
            df_4h = self.data.fetch_ohlcv(symbol, "4h", 300)
            df_1h = self.data.fetch_ohlcv(symbol, "1h", 300)
            df_15m = self.data.fetch_ohlcv(symbol, "15m", 300)
            df_5m = self.data.fetch_ohlcv(symbol, "5m", 300)
        except Exception as e:
            return self._neutral_signal(symbol, f"Data fetch failed: {e}", 0.0)

        if len(df_4h) < 50 or len(df_15m) < 50:
            return self._neutral_signal(symbol, "Insufficient bars for SMC mapping.", 0.0)

        snap = self.data.build_snapshot(symbol)
        price = snap.price
        market_type = BOT_CONFIG.exchange.market_type.lower()
        is_crypto = "crypto" in market_type or any(c in symbol.upper() for c in ["BTC", "ETH", "SOL"])
        # METALS-COEXISTENCE: XAU/XAG/XPT/XPD (gold, silver, platinum, palladium)
        # get their own tuning; they backtest best on 15m entries (like forex) with
        # no confidence gate, tp 2.0, tolerance 3.0.
        is_metal = any(c in symbol.upper() for c in ("XAU", "XAG", "XPT", "XPD"))

        # CRYPTO-COEXISTENCE: crypto symbols resolve their own tuning (1h entries,
        # wider tolerance, no zone filter, etc.); forex symbols use the forex
        # values above, untouched. Backtested on 180d BTC+ETH: 62.5% WR / +0.18R.
        dyn = BOT_CONFIG.dynamic
        if is_metal:
            # METALS: 15m entries (like forex), no zone filter, tp 2.0, tol 3.0.
            # 60d/15m backtest: 71% WR / +0.31R / PF 2.74 (experimental — forward-test).
            zone_filter = dyn.metal_htf_zone_filter
            tp_target = dyn.metal_tp_target_rr
            tol = dyn.metal_fvg_entry_tolerance
            base_gap = dyn.metal_min_fvg_gap_pct
            buffer_mult = dyn.metal_sl_buffer_atr_mult
            min_rr = dyn.metal_min_rr
            crypto_entry = False
        elif is_crypto:
            zone_filter = dyn.crypto_htf_zone_filter
            tp_target = dyn.crypto_tp_target_rr
            tol = dyn.crypto_fvg_entry_tolerance
            base_gap = dyn.crypto_min_fvg_gap_pct
            buffer_mult = dyn.crypto_sl_buffer_atr_mult
            min_rr = dyn.crypto_min_rr
            crypto_entry = True
        else:
            zone_filter = dyn.htf_zone_filter
            tp_target = dyn.tp_target_rr
            tol = dyn.fvg_entry_tolerance
            base_gap = dyn.min_fvg_gap_pct
            buffer_mult = dyn.sl_buffer_atr_mult
            min_rr = dyn.min_rr
            crypto_entry = False

        # 1. PREMIUM / DISCOUNT MATRIX
        sh_1h, sl_1h, eq_1h = self.get_dealing_range(df_1h, lookback=48)
        in_premium = price > eq_1h if eq_1h else False
        in_discount = price < eq_1h if eq_1h else False
        zone_label = "Premium" if in_premium else "Discount" if in_discount else "Equilibrium"

        # 2. MARKET STRUCTURE ALIGNMENT (Fractal Driven)
        bias_4h, struct_4h, _ = self.detect_structure(df_4h)
        bias_1h, struct_1h, _ = self.detect_structure(df_1h)
        bias_15m, struct_15m, _ = self.detect_structure(df_15m)

        base_bias = bias_4h if bias_4h != "Neutral" else "NEUTRAL"
        is_pullback = (bias_4h != bias_1h)

        b4 = "LONG" if bias_4h == "Bullish" else "SHORT" if bias_4h == "Bearish" else "NEUTRAL"
        b1 = "LONG" if bias_1h == "Bullish" else "SHORT" if bias_1h == "Bearish" else "NEUTRAL"
        pullback_str = " (Pullback)" if is_pullback else ""
        ui_trend_status = f"4H: {b4} | 1H: {b1}{pullback_str} | Zone: {zone_label}"

        # 2b. TREND-STRENGTH FILTER (optional, MIN_ADX > 0): avoid chop. SMC setups
        # taken when the 4h trend is weak lose disproportionately; require a
        # minimum 4h ADX so we only trade real, tradeable trends.
        min_adx = BOT_CONFIG.dynamic.min_adx
        if min_adx > 0:
            try:
                adx_val = float(df_4h.iloc[-1].get("adx", np.nan))
            except (TypeError, ValueError):
                adx_val = float("nan")
            if not np.isnan(adx_val) and adx_val < min_adx:
                return self._neutral_signal(
                    symbol, f"DENIED: HTF trend too weak (4H ADX {adx_val:.0f} < {min_adx:.0f})",
                    0.0, snap, ui_trend_status)

        # 3. STRICT PREMIUM/DISCOUNT REJECTION (toggle; crypto uses its own value)
        if zone_filter:
            if base_bias == "Bullish" and in_premium:
                return self._neutral_signal(symbol, f"DENIED: Bullish structure, but price is in HTF Premium. Waiting for pullback.", 0.0, snap, ui_trend_status, "LONG")
            if base_bias == "Bearish" and in_discount:
                return self._neutral_signal(symbol, f"DENIED: Bearish structure, but price is in HTF Discount. Waiting for pullback.", 0.0, snap, ui_trend_status, "SHORT")

        # 4. FALLING KNIFE PROTECTION
        if is_pullback:
            if base_bias == "Bullish" and bias_15m == "Bearish":
                return self._neutral_signal(symbol, f"Waiting: 15M still Bearish. Awaiting LTF CHoCH to confirm pullback is over.", 0.0, snap, ui_trend_status, "LONG")
            if base_bias == "Bearish" and bias_15m == "Bullish":
                return self._neutral_signal(symbol, f"Waiting: 15M still Bullish. Awaiting LTF CHoCH to confirm pullback is over.", 0.0, snap, ui_trend_status, "SHORT")

        if base_bias == "NEUTRAL":
            return self._neutral_signal(symbol, f"No HTF Bias", 0.0, snap, ui_trend_status)

        bias = Bias.LONG if base_bias == "Bullish" else Bias.SHORT
        intended_str = "LONG" if bias == Bias.LONG else "SHORT"

        # 5. ENTRY ARRAYS (FVG & Structural OB)
        if crypto_entry:
            # CRYPTO-COEXISTENCE: entries on 1h (4h structure) — 180d backtest
            # showed 1h entries are the robust TF for BTC/ETH (15m whipsaws).
            df_entry = df_1h
            entry_tf = "1H"
            fvg = self.find_fvg(df_1h, base_bias)
        else:
            df_entry = df_15m
            entry_tf = "15M"
            fvg = self.find_fvg(df_15m, base_bias)

            if not fvg:
                fvg = self.find_fvg(df_5m, base_bias)
                entry_tf = "5M"
                df_entry = df_5m

        if not fvg:
            return self._neutral_signal(symbol, f"DENIED: No unmitigated FVG found", 0.0, snap, ui_trend_status, intended_str)

        fvg_top, fvg_bottom = fvg
        order_block = self.find_order_block(df_entry, fvg_top, fvg_bottom, base_bias)

        # 6. LIQUIDITY SWEEPS (Fractal Targeting)
        swp_4h, _ = self.detect_liquidity_sweep(df_4h, base_bias)
        swp_1h, _ = self.detect_liquidity_sweep(df_1h, base_bias)
        swp_ent, _ = self.detect_liquidity_sweep(df_entry, base_bias, lookback=40)

        # 7. CONFIDENCE SCORING
        score = 20
        if not is_pullback: score += 15
        if bias == Bias.LONG and in_discount: score += 15
        if bias == Bias.SHORT and in_premium: score += 15

        if swp_4h: score += 20
        elif swp_1h: score += 15
        elif swp_ent: score += 10

        if struct_4h == "CHoCH": score += 15
        elif struct_4h == "BOS": score += 10

        macd_line = df_1h["close"].ewm(span=12).mean() - df_1h["close"].ewm(span=26).mean()
        macd_sig = macd_line.ewm(span=9).mean()
        macd_hist = macd_line - macd_sig

        delta = df_1h["close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = delta.clip(upper=0).abs().rolling(14).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi_series = 100 - (100 / (1 + rs))

        rsi_val = rsi_series.iloc[-1]
        hist_val = macd_hist.iloc[-1]

        if bias == Bias.LONG and rsi_val > 50 and hist_val > 0: score += 15
        elif bias == Bias.SHORT and rsi_val < 50 and hist_val < 0: score += 15

        confidence = min(max(score / 100.0, 0.0), 1.0)

        # 8. FVG SIZE FILTER (configurable: MIN_FVG_GAP_PCT, crypto uses 5x)
        gap_size = abs(fvg_top - fvg_bottom)
        size_factor = 0.5 if entry_tf == "5M" else 1.0
        # base_gap resolved above (crypto uses its own value; crypto keeps x5 gap multiplier)
        min_gap_pct = (base_gap if not is_crypto else base_gap * 5.0) * size_factor

        if (gap_size / price) < min_gap_pct - 1e-12:
            return self._neutral_signal(symbol, f"DENIED: {entry_tf} FVG size too small", 0.0, snap, ui_trend_status, intended_str)

        # 9. ASSET-AWARE STOP LOSS BUFFER (Concrete Wall Protection)
        safe_atr = snap.atr if snap.atr > 0 else (price * 0.001)
        if is_crypto:
            min_buffer = price * 0.0015
        else:
            market_data = self.data._market_info.get(symbol, {})
            point_value = market_data.get("point", 0.00001)
            min_buffer = point_value * 20

        # F4-EXPOSE: buffer as ATR multiple (crypto uses its own multiplier)
        buffer = max(safe_atr * buffer_mult, min_buffer)

        if order_block:
            ob_high, ob_low = order_block
            sl = (ob_low - buffer) if bias == Bias.LONG else (ob_high + buffer)
            entry_edge = ob_high if bias == Bias.LONG else ob_low
        else:
            sl = (fvg_bottom - buffer) if bias == Bias.LONG else (fvg_top + buffer)
            entry_edge = fvg_top if bias == Bias.LONG else fvg_bottom

        risk = abs(entry_edge - sl)
        # WIN-RATE lever: TP distance is configurable (was hardcoded 3.0 R).
        # tp_target_rr is a raw target before the external-liquidity cap below.
        raw_tp3 = entry_edge + (risk * tp_target) if bias == Bias.LONG else entry_edge - (risk * tp_target)

        # SMC limit-entry price: the FVG gap edge where a pending limit order rests.
        # Longs pull back DOWN into a bullish gap -> order at the gap TOP (fvg_top);
        # shorts rally UP into a bearish gap -> order at the gap BOTTOM (fvg_bottom).
        limit_entry = fvg_top if bias == Bias.LONG else fvg_bottom

        # 10. MACRO TARGET & ENTRY PULLBACK DISCIPLINE
        # WIN-RATE lever: FVG_ENTRY_TOLERANCE lets price be within tol x gap of the
        # FVG and still valid (limit-order style near-gap entry) instead of requiring
        # price to be strictly inside the gap at scan time.
        # SNIPER FIX: when price is beyond the tolerance (above the gap for longs /
        # below for shorts), this is STILL a valid SMC setup — price simply hasn't
        # pulled back yet. Instead of dropping it as NEUTRAL (which made the bot
        # "leave entries for granted"), flag it awaiting_entry so the orchestrator
        # places a GTC pending limit at the FVG edge and waits for the snipe.
        awaiting = False
        if bias == Bias.LONG:
            if price >= raw_tp3:
                return self._neutral_signal(symbol, f"INVALIDATED: Price already hit Macro Target ({raw_tp3:.5f})", 0.0, snap, ui_trend_status, intended_str)
            if price > fvg_top + tol * gap_size:
                awaiting = True
            if price < sl:
                return self._neutral_signal(symbol, f"DENIED: Price ({price:.5f}) broke below Stop Loss. Setup Invalid.", 0.0, snap, ui_trend_status, intended_str)

        elif bias == Bias.SHORT:
            if price <= raw_tp3:
                return self._neutral_signal(symbol, f"INVALIDATED: Price already hit Macro Target ({raw_tp3:.5f})", 0.0, snap, ui_trend_status, intended_str)
            if price < fvg_bottom - tol * gap_size:
                awaiting = True
            if price > sl:
                return self._neutral_signal(symbol, f"DENIED: Price ({price:.5f}) broke above Stop Loss. Setup Invalid.", 0.0, snap, ui_trend_status, intended_str)

        # 11. DYNAMIC EXTERNAL LIQUIDITY CAPPING (Fractal Liquidity Targeting)
        final_tp = raw_tp3
        tp_capped = False

        ext_liquidity = self.find_liquidity_targets(df_1h, base_bias, price, lookback=48)
        for liq_target in ext_liquidity:
            if bias == Bias.LONG and price < liq_target < raw_tp3:
                final_tp = liq_target; tp_capped = True; break
            elif bias == Bias.SHORT and price > liq_target > raw_tp3:
                final_tp = liq_target; tp_capped = True; break

        # F2-FIX: minimum realized R:R gate, computed at the LIMIT entry price
        # (the intended SMC fill), not the current market price — a signal waiting
        # for a pullback has a much better R:R at the FVG edge than at market.
        entry_ref = limit_entry if limit_entry > 0 else price
        sl_dist = abs(entry_ref - sl)
        if sl_dist > 0:
            realized_rr = abs(final_tp - entry_ref) / sl_dist
        else:
            realized_rr = 0.0
        if realized_rr < min_rr - 1e-9:
            return self._neutral_signal(
                symbol,
                f"DENIED: Realized R:R {realized_rr:.2f}:1 below min {min_rr:.1f}:1 "
                f"(TP {final_tp:.5f} vs SL {sl:.5f})",
                0.0, snap, ui_trend_status, intended_str)

        log_msgs.append(f"[{symbol}] SMC SETUP {'SNIPER (pending limit)' if awaiting else 'TAPPED'}: {base_bias}")
        log_msgs.append(f"   Matrix: {zone_label} | 4H {struct_4h}, 1H {struct_1h}")
        log_msgs.append(f"   Entry: {entry_tf} FVG {'+ Extreme Origin OB ' if order_block else ''}"
                        f"{'@ LIMIT ' + str(round(limit_entry, 5)) + ' (awaiting pullback)' if awaiting else 'Tapped @ ' + str(round(price, 5))}")
        log_msgs.append(f"   SL: {sl:.5f} | Final TP: {final_tp:.5f} {'(Capped at Liquidity)' if tp_capped else '(3.0 RR)'}")

        for msg in log_msgs:
            self.db.add_monologue(MonologueEntry(datetime.now(timezone.utc), "Scout", msg, "info"))

        return ScoutSignal(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol, bias=bias,
            strength=SignalStrength.STRONG if confidence >= 0.8 else SignalStrength.MODERATE,
            confidence=confidence,
            entry_price=price, sniper_entry_price=limit_entry, indicators=snap,
            nearest_support=sl_1h or 0.0, nearest_resistance=sh_1h or 0.0,
            wick_rejection=None, patterns_detected=["SMC FVG", "SMC OB"] if order_block else ["SMC FVG"],
            reasoning=f"{ui_trend_status}\n" + "\n".join(log_msgs),
            confidence_breakdown={"smc_score": score, "vol_ratio": snap.volume_ratio, "intended_bias": intended_str},
            suggested_sl=round(sl, 5), suggested_tp=round(final_tp, 5),
            limit_entry_price=limit_entry,
            awaiting_entry=awaiting,
        )

    def _neutral_signal(self, symbol: str, reason: str, partial_conf: float, snap: Optional[IndicatorSnapshot] = None, trend_status: str = "4H: NEUTRAL", intended_bias: str = "NEUTRAL") -> ScoutSignal:
        now = datetime.now(timezone.utc)
        log.info(f"[{symbol}] SMC Filter: {reason} | {trend_status}")
        self.db.add_monologue(MonologueEntry(now, "Scout", f"[{symbol}] {reason}", "warning"))

        if not snap:
            snap = IndicatorSnapshot(
                timestamp=now, price=0.0, rsi=50.0, macd_line=0.0, macd_signal=0.0,
                macd_histogram=0.0, bb_upper=0.0, bb_middle=0.0, bb_lower=0.0, bb_pct_b=0.5,
                ma20=0.0, ma50=0.0, atr=0.0, volume_current=0.0, volume_avg=0.0, volume_ratio=0.0,
            )

        return ScoutSignal(
            timestamp=now, symbol=symbol, bias=Bias.NEUTRAL, strength=SignalStrength.NONE,
            confidence=partial_conf, entry_price=snap.price, sniper_entry_price=0.0, indicators=snap,
            nearest_support=0.0, nearest_resistance=0.0, wick_rejection=None, patterns_detected=[],
            reasoning=f"{reason} | {trend_status}",
            confidence_breakdown={"smc_score": partial_conf * 100, "vol_ratio": snap.volume_ratio, "intended_bias": intended_bias},
            suggested_sl=0.0, suggested_tp=0.0,
        )


# --- END OF FILE scout.py ---
