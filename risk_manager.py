# --- START OF FILE risk_manager.py ---

import logging
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5
from data_engine import DataEngine
from config import BOT_CONFIG
from database import Database
from models import Bias, MonologueEntry, RiskVerdict, ScoutSignal

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, db: Database, data_engine: DataEngine, portfolio_value: float = 10_000):
        self.db = db
        self.data_engine = data_engine
        self.portfolio_value = portfolio_value
        self.kill_switch_active = False
        self.manual_override: Optional[str] = None

    def _parse_dynamic_limit(self, limit_str: str) -> float:
        limit_str = str(limit_str).strip().lower()
        try:
            if '%' in limit_str:
                pct = float(limit_str.replace('%', ''))
                return self.portfolio_value * (pct / 100.0)
            else:
                return float(limit_str.replace('$', '').replace('usd', ''))
        except ValueError:
            # FAIL-SAFE: an unparseable limit must NOT collapse to 0.0 (which
            # would hard-stop the bot on the very first $-0.01 loss). Fall back
            # to a sane 10% cap and say so loudly.
            log.warning(
                f"[Risk] Unparseable daily-loss limit '{limit_str}' — falling back "
                f"to 10% of equity (${self.portfolio_value * 0.10:.2f}).")
            return self.portfolio_value * 0.10

    def evaluate(self, signal: ScoutSignal) -> RiskVerdict:
        dyn = BOT_CONFIG.dynamic
        warnings_list: list[str] = []
        deny: list[str] = []
        log_msgs: list[str] = []

        crypto_keywords = ["BTC", "ETH", "SOL", "LTC", "XRP", "DOGE", "ADA", "DOT"]
        is_crypto = any(crypto in signal.symbol.upper() for crypto in crypto_keywords)

        active_multiplier = dyn.crypto_lot_multiplier if is_crypto else dyn.forex_lot_multiplier
        asset_class_name = "Crypto" if is_crypto else "Forex/Indices/Synth"

        discipline = {
            "sizing_mode": f"Volatility Scaled [{asset_class_name}] (Max: {active_multiplier}x)",
            "min_confidence": f"{dyn.min_confidence:.0%}",
        }

        if self.kill_switch_active:
            return self._deny(signal, "Kill switch active", discipline)

        closed_today_pnl = sum(
            t.get("pnl_usd", 0) or 0
            for t in self.db.get_trades(days_back=1)
            if t["status"] == "CLOSED"
        )
        open_floating_pnl = self.data_engine.get_total_floating_pnl()
        real_daily_pnl = closed_today_pnl + open_floating_pnl

        raw_loss_limit = getattr(dyn, "max_daily_loss", getattr(dyn, "max_daily_loss_pct", "20$"))
        daily_loss_limit = -abs(self._parse_dynamic_limit(raw_loss_limit))

        if real_daily_pnl <= daily_loss_limit:
            return self._deny(signal, f"Hard Stop: True Daily PnL (${real_daily_pnl:,.2f}) breached limit (${daily_loss_limit:.2f})", discipline)

        peak_equity = self.db.get_system_flag("peak_equity")
        if peak_equity:
            dd = (self.portfolio_value - float(peak_equity)) / max(float(peak_equity), 1.0) * 100
            if dd <= -dyn.max_drawdown_pct:
                return self._deny(signal, f"Max drawdown reached: {dd:.1f}%", discipline)

        if signal.bias == Bias.NEUTRAL and not self.manual_override:
            deny.append(f"[{signal.symbol}] No directional bias")

        # CRYPTO/METAL-COEXISTENCE: crypto and metals use their own confidence
        # gates so the forex threshold (0.6) doesn't starve them.
        _crypto_keys = ("BTC", "ETH", "SOL", "LTC", "XRP", "DOGE", "ADA", "DOT")
        _metal_keys = ("XAU", "XAG", "XPT", "XPD")
        _u = signal.symbol.upper()
        _is_crypto_sym = any(k in _u for k in _crypto_keys) or \
            BOT_CONFIG.exchange.market_type.lower() == "crypto"
        _is_metal_sym = any(k in _u for k in _metal_keys)
        if _is_metal_sym:
            _min_conf = dyn.metal_min_confidence
        elif _is_crypto_sym:
            _min_conf = dyn.crypto_min_confidence
        else:
            _min_conf = dyn.min_confidence
        if signal.confidence < _min_conf:
            deny.append(f"[{signal.symbol}] Confidence {signal.confidence:.0%} below {_min_conf:.0%}")

        active_t = self.db.get_active_trades()
        if len(active_t) >= dyn.max_open_positions:
            deny.append(f"[{signal.symbol}] Max positions reached")
        if any(t["symbol"] == signal.symbol for t in active_t):
            deny.append(f"[{signal.symbol}] Already have active trade")

        # HARD BROKER-TRUTH GUARD: the broker is the source of truth for what is
        # actually open. Even if the DB was deleted/recreated (wiped), never open
        # a second trade on a symbol the broker already occupies — an open
        # position OR a resting order on this symbol blocks new entries. This is
        # what prevents "bot re-suggests the same signal that's already open".
        try:
            with self.data_engine.lock:
                broker_pos = mt5.positions_get(symbol=signal.symbol) or []
                broker_ords = mt5.orders_get(symbol=signal.symbol) or []
            if any(float(getattr(p, "volume", 0) or 0) > 0 for p in broker_pos):
                deny.append(f"[{signal.symbol}] Already have open position at broker")
            elif broker_ords:
                deny.append(f"[{signal.symbol}] Resting order already at broker")
        except Exception:
            pass   # broker query failed — fall back to DB-only gating

        if deny:
            return self._deny(signal, "; ".join(deny), discipline)

        with self.data_engine.lock:
            tick = mt5.symbol_info_tick(signal.symbol)
            info = mt5.symbol_info(signal.symbol)

        if not info: return self._deny(signal, f"MT5 symbol info not found", discipline)

        if tick is not None and tick.ask > 0 and tick.bid > 0:
            spread_pct = ((tick.ask - tick.bid) / tick.bid) * 100
            max_spread_pct = 0.50 if BOT_CONFIG.exchange.market_type.lower() == "synthetic" else (0.15 if is_crypto else 0.03)
            if spread_pct > max_spread_pct:
                return self._deny(signal, f"Broker Spread too high ({spread_pct:.3f}%). Protection active.", discipline)

        stop_loss = signal.suggested_sl
        ideal_entry = signal.sniper_entry_price
        sl_dist_pct = abs(ideal_entry - stop_loss) / max(ideal_entry, 1e-8) * 100

        take_profit = signal.suggested_tp
        risk_reward = (abs(take_profit - ideal_entry) / max(abs(ideal_entry - stop_loss), 1e-8)) if take_profit > 0 else 0.0

        # F2-FIX: gate is configurable (was hardcoded 2.0). Scout already filters
        # below min_rr; this is the final safety net using the same constant.
        # 1e-9 tolerance: a setup computing 1.4999999999 for min 1.5 is float
        # error, not a real miss (it used to deny every exact-boundary setup).
        if risk_reward < dyn.min_rr - 1e-9:
            return self._deny(signal, f"SMC Target Capped: Realistic R:R is only {risk_reward:.1f}:1 (Needs {dyn.min_rr:.1f}:1)", discipline)

        v_min = info.volume_min
        v_step = info.volume_step
        v_max = info.volume_max
        tick_size = info.trade_tick_size
        tick_value = info.trade_tick_value

        sl_points = abs(ideal_entry - stop_loss)
        risk_per_one_lot_usd = (sl_points / tick_size) * tick_value if (tick_size > 0 and tick_value > 0) else 0.0
        max_allowed_risk_usd = max(2.0, self.portfolio_value * (dyn.max_risk_per_trade_pct / 100.0))

        # FIX: the old code did `min(v_min * multiplier, safe_lot)`, which could
        # never exceed v_min*multiplier (0.05 lots forex / 0.03 crypto at 5x/3x),
        # making the multiplier a cap instead of a scale. Now the multiplier
        # scales the volatility-derived lot, still floored at v_min and capped at v_max.
        if risk_per_one_lot_usd > 0:
            base_risk_lot = max_allowed_risk_usd / risk_per_one_lot_usd
            target_lot = base_risk_lot * active_multiplier
        else:
            target_lot = v_min * active_multiplier

        lot_size = max(v_min, min(target_lot, v_max))
        lot_size = round(round(lot_size / v_step) * v_step, 8)

        with self.data_engine.lock:
            order_type = mt5.ORDER_TYPE_BUY if signal.bias == Bias.LONG else mt5.ORDER_TYPE_SELL
            margin_required = mt5.order_calc_margin(order_type, signal.symbol, lot_size, ideal_entry)
            acct = mt5.account_info()

        # ATTACK FIX 2: The Margin Black Hole
        if margin_required is None or margin_required <= 0:
            return self._deny(signal, f"Broker refused to calculate margin risk. Safety abort.", discipline)

        acct_leverage = acct.leverage if acct else 100
        notional_size = margin_required * acct_leverage

        max_margin_allowed = self.portfolio_value * 0.15
        if margin_required > max_margin_allowed:
            penalized_lot = lot_size * (max_margin_allowed / margin_required)
            penalized_lot = round(round(penalized_lot / v_step) * v_step, 8)

            # ATTACK FIX 1: The Minimum Lot Size Death Trap
            if penalized_lot < v_min:
                return self._deny(signal, f"Account too small. Minimum lot ({v_min}) requires ${margin_required:.2f} margin (Cap is ${max_margin_allowed:.2f}).", discipline)

            lot_size = penalized_lot
            margin_required = max_margin_allowed
            notional_size = margin_required * acct_leverage
            warnings_list.append(f"Sizing explosion detected. Margin clamped to 15% (${max_margin_allowed:.2f}).")

        # F5-FIX: enforce max risk AFTER all clamps. The old code clamped lot_size up to
        # v_min and never re-checked risk, so small accounts silently risked 5-11% per
        # trade (e.g. $100 account, 1% SL -> 0.01 lot = $10.8 risk = 10.8% of equity).
        if risk_per_one_lot_usd > 0:
            final_risk_usd = lot_size * risk_per_one_lot_usd
            if final_risk_usd > max_allowed_risk_usd * 1.25:
                fit_lot = max_allowed_risk_usd / risk_per_one_lot_usd
                fit_lot = round(round(fit_lot / v_step) * v_step, 8)
                if fit_lot >= v_min:
                    lot_size = fit_lot
                    with self.data_engine.lock:
                        margin_required = mt5.order_calc_margin(order_type, signal.symbol, lot_size, ideal_entry) or margin_required
                    notional_size = margin_required * acct_leverage
                    warnings_list.append(
                        f"Risk cap enforced: lot shrunk to {lot_size} "
                        f"(risk ${final_risk_usd:.2f} -> ${lot_size * risk_per_one_lot_usd:.2f}).")
                else:
                    req_eq = final_risk_usd / max(dyn.max_risk_per_trade_pct / 100.0, 1e-9)
                    return self._deny(
                        signal,
                        f"Minimum lot ({v_min}) risks ${final_risk_usd:.2f} = "
                        f"{final_risk_usd / max(self.portfolio_value, 1) * 100:.1f}% of equity, "
                        f"above the {dyn.max_risk_per_trade_pct:.0f}% risk cap "
                        f"(needs ≥ ${req_eq:,.0f} equity at this risk %). "
                        f"Account too small for this SL width.",
                        discipline)

        # F6-FIX: correlated group exposure cap. Five 2%-risk EUR pairs = 10% of equity
        # = the daily loss limit in one correlated shock; cap per-currency-group risk.
        group = self._currency_group(signal.symbol)
        group_risk_now = 0.0
        for t in active_t:
            if self._currency_group(t.get("symbol", "")) == group:
                e = float(t.get("entry_price", 0) or 0)
                s = float(t.get("stop_loss", 0) or 0)
                sz = float(t.get("position_size_usd", 0) or 0)
                if e > 0 and s > 0 and e != s and sz > 0:
                    group_risk_now += abs(e - s) / e * sz
        candidate_risk = lot_size * risk_per_one_lot_usd if risk_per_one_lot_usd > 0 else 0.0
        group_limit = self.portfolio_value * (dyn.max_group_risk_pct / 100.0)
        if group_risk_now + candidate_risk > group_limit:
            return self._deny(
                signal,
                f"Correlated group ({group}) exposure "
                f"${group_risk_now + candidate_risk:,.2f} exceeds "
                f"${group_limit:,.2f} cap ({dyn.max_group_risk_pct:.0f}%).",
                discipline)

        log_msgs.append(f"[{signal.symbol}] APPROVED {signal.bias.value} | Lot Size: {lot_size} | Margin Req: ${margin_required:,.2f} | Notional: ${notional_size:,.2f} | SL: {sl_dist_pct:.2f}% | Mode: Volatility Scaled | R:R {risk_reward:.1f}:1")
        self._log(log_msgs)

        return RiskVerdict(
            approved=True, original_signal=signal, lot_size=lot_size,
            position_size_usd=round(notional_size, 2), position_size_pct=round((notional_size / max(1, self.portfolio_value)) * 100, 2),
            stop_loss=round(stop_loss, 5), sl_distance_pct=round(sl_dist_pct, 4),
            reasoning="\n".join(log_msgs), discipline_rules=discipline, warnings=warnings_list,
            leverage=0, margin_required=round(margin_required, 2), take_profit=round(take_profit, 5), risk_reward=round(risk_reward, 2)
        )

    @staticmethod
    def _currency_group(symbol: str) -> str:
        """F6: bucket a symbol into a correlated currency group."""
        s = symbol.upper()
        if any(k in s for k in ("US30", "US500", "USTEC", "NAS100")):
            return "USD"
        if "DE30" in s:
            return "EUR"
        if "UK100" in s:
            return "GBP"
        if "JP225" in s:
            return "JPY"
        if "AUS200" in s:
            return "AUD"
        if any(k in s for k in ("BTC", "ETH", "SOL", "LTC", "XRP", "DOGE", "ADA", "DOT")):
            return "CRYPTO"
        clean = s.replace("M", "").replace("-", "")
        if len(clean) == 6 and clean.isalpha():
            return clean[:3]
        return s

    def _deny(self, signal, reason, rules=None):
        rules = rules or {}
        self._log([f"[{signal.symbol}] DENIED: {reason}"])
        return RiskVerdict(False, signal, 0.0, 0.0, 0.0, 0.0, 0.0, reason, rules, [reason], 0, 0.0, 0.0, 0.0)

    def _log(self, msgs):
        for m in msgs:
            self.db.add_monologue(MonologueEntry(datetime.now(timezone.utc), "Risk", m, "info"))
            log.info(m)

    def set_portfolio_value(self, v):
        self.portfolio_value = v
        # VISIBILITY: log the resolved daily-loss limit + its source whenever it
        # changes, so a "locked out" day is never a mystery (it used to be silent).
        try:
            raw = BOT_CONFIG.dynamic.max_daily_loss
            limit = -abs(self._parse_dynamic_limit(raw))
            prev = getattr(self, "_last_logged_limit", None)
            if prev is None or abs(limit - prev) > 0.01:
                self._last_logged_limit = limit
                log.info(f"[Risk] Daily loss limit = ${limit:.2f}  (resolved from '{raw}'; "
                         f"equity ${self.portfolio_value:,.2f})")
        except Exception:
            pass


# --- END OF FILE risk_manager.py ---
