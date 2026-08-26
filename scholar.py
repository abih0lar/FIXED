# --- START OF FILE scholar.py ---

import json
import logging
import random
import re
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import numpy as np
import threading

from config import BOT_CONFIG
from database import Database
from models import MonologueEntry, ScholarInsight

log = logging.getLogger(__name__)


class DynamicConfigProxy:

    def __init__(self, config):
        self._config = config
        self._lock = threading.Lock()

    def __getattr__(self, name):
        if name.startswith('_'):
            return object.__getattribute__(self, name)
        with self._lock:
            return getattr(self._config, name)

    def safe_update(self, updates: Dict):
        with self._lock:
            for key, value in updates.items():
                setattr(self._config, key, value)

    def snapshot(self) -> Dict:
        with self._lock:
            return vars(self._config).copy()


class GeminiClient:
    def __init__(self):
        self.api_key = BOT_CONFIG.ai.gemini_api_key
        self.model = BOT_CONFIG.ai.gemini_model
        self.available = bool(self.api_key)
        if self.available:
            log.info("[Scholar] Gemini REST API configured successfully.")
        else:
            log.warning("[Scholar] No Gemini API key found.")

    def generate(self, prompt: str, retries: int = 3) -> Optional[str]:
        if not self.available:
            return None
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )

        # ARCHITECT FIX: Combine system instructions into the main prompt.
        # Google's API strictly rejects "systemInstruction" and "responseMimeType"
        # on certain endpoints/regions, causing a 400 Bad Request.
        # This simplified payload is universally accepted and bulletproof.
        full_prompt = (
            "You are the Scholar agent of an Institutional Multi-Asset Trading bot. "
            "Always respond in strictly valid JSON format.\n\n"
            f"{prompt}"
        )

        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {
                "temperature": BOT_CONFIG.ai.gemini_temperature,
                "maxOutputTokens": BOT_CONFIG.ai.gemini_max_tokens,
            },
        }

        for attempt in range(retries):
            try:
                r = requests.post(url, json=payload, timeout=30)
                r.raise_for_status()
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as e:
                if "429" in str(e):
                    wait_time = (attempt + 1) * 15
                    log.warning(f"[Scholar] Rate limited, waiting {wait_time}s")
                    time.sleep(wait_time)
                else:
                    log.error("[Scholar] Gemini API error: %s", e)
                    return None
        return None

    def parse_json(self, text: str) -> Optional[Dict]:
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        for pat in [r"```json\s*([\s\S]*?)\s*```", r"(\*\*\{[\s\S]*?\}\*\*)"]:
            m = re.search(pat, text)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
        return None


class QLearningEngine:
    ACTIONS = [
        "widen_bb", "tighten_bb", "widen_sl", "tighten_sl",
        "raise_confidence", "lower_confidence", "hold_params",
    ]

    def __init__(self, db: Database, config_proxy: DynamicConfigProxy):
        self.db = db
        self.proxy = config_proxy
        self.lr = self.proxy.learning_rate
        self.gamma = self.proxy.discount_factor
        self.epsilon = self.proxy.exploration_rate

    def discretize_state(self, volatility: float, trend_strength: float, win_rate: float) -> str:
        v = "high" if volatility > 0.015 else "medium" if volatility > 0.008 else "low"
        t = "trending" if trend_strength > 0.6 else "ranging"
        w = "high" if win_rate > 0.6 else "medium" if win_rate > 0.4 else "low"
        return f"{v}_{t}_{w}"

    def get_q(self, state: str) -> Dict[str, float]:
        q = self.db.get_q_values(state)
        if not q:
            q = {a: 0.0 for a in self.ACTIONS}
            self.db.upsert_q_values(state, q)
        return q

    def choose(self, state: str) -> str:
        if random.random() < self.epsilon:
            return random.choice(self.ACTIONS)
        q = self.get_q(state)
        return max(q, key=q.get)

    def update(self, state: str, action: str, reward: float, next_state: str):
        q = self.get_q(state)
        q_next = self.get_q(next_state)
        q[action] = round(
            q[action] + self.lr * (reward + self.gamma * max(q_next.values()) - q[action]), 6
        )
        self.db.upsert_q_values(state, q)

    def apply(self, action: str) -> Dict:
        proxy = self.proxy
        changes: Dict = {}

        if action == "widen_bb":
            old_val = proxy.bb_std
            new_val = min(3.0, round(old_val + 0.2, 1))
            if old_val != new_val:
                changes["bb_std"] = {"old": old_val, "new": new_val}
        elif action == "tighten_bb":
            old_val = proxy.bb_std
            new_val = max(1.5, round(old_val - 0.2, 1))
            if old_val != new_val:
                changes["bb_std"] = {"old": old_val, "new": new_val}
        elif action == "widen_sl":
            old_val = proxy.sl_max_pct
            new_val = min(5.0, round(old_val + 0.25, 2))
            if old_val != new_val:
                changes["sl_max_pct"] = {"old": old_val, "new": new_val}
        elif action == "tighten_sl":
            old_val = proxy.sl_max_pct
            new_val = max(0.5, round(old_val - 0.25, 2))
            if old_val != new_val:
                changes["sl_max_pct"] = {"old": old_val, "new": new_val}
        elif action == "raise_confidence":
            old_val = proxy.min_confidence
            new_val = min(0.80, round(old_val + 0.05, 2))
            if old_val != new_val:
                changes["min_confidence"] = {"old": old_val, "new": new_val}
        elif action == "lower_confidence":
            old_val = proxy.min_confidence
            new_val = max(0.35, round(old_val - 0.05, 2))
            if old_val != new_val:
                changes["min_confidence"] = {"old": old_val, "new": new_val}

        if changes:
            updates = {k: v["new"] for k, v in changes.items()}
            proxy.safe_update(updates)

        return changes


class Scholar:
    def __init__(self, db: Database):
        self.db = db
        self._dynamic_proxy = DynamicConfigProxy(BOT_CONFIG.dynamic)
        # FIX: restore ONLY the parameters Scholar is allowed to tune. The old
        # code re-applied the WHOLE dynamic-config snapshot from the DB, which
        # silently clobbered user-controlled fields (max_open_positions, MIN_RR,
        # TP_TARGET_RR, ...) back to stale values — e.g. editing "Max Open
        # Positions" 5->7 in the GUI had no effect after a restart because the
        # snapshot forced 5 again. The .env / GUI is the source of truth for
        # everything else.
        _scholar_tunable = {"bb_std", "sl_max_pct", "min_confidence"}
        latest = self.db.get_latest_config_snapshot()
        if latest:
            restore = {k: v for k, v in latest.items() if k in _scholar_tunable}
            if restore:
                self._dynamic_proxy.safe_update(restore)
                log.info(f"[Scholar] Restored tunable params from snapshot: {sorted(restore)}")
        self.rl = QLearningEngine(db, self._dynamic_proxy)
        self.llm = GeminiClient()

    def should_review(self) -> bool:
        closed = [t for t in self.db.get_trades(days_back=30) if t["status"] == "CLOSED"]
        reviews = self.db.get_scholar_reviews(limit=1)
        if not reviews:
            return len(closed) >= BOT_CONFIG.dynamic.scholar_trigger_trades

        last_time_str = reviews[0].get("timestamp", "")
        if not last_time_str:
            return len(closed) >= BOT_CONFIG.dynamic.scholar_trigger_trades
        try:
            last_time = datetime.fromisoformat(last_time_str).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return len(closed) >= BOT_CONFIG.dynamic.scholar_trigger_trades

        new_closed = 0
        for t in closed:
            tc_str = t.get("timestamp_close")
            if not tc_str:
                continue
            try:
                tc = datetime.fromisoformat(tc_str).replace(tzinfo=timezone.utc)
                if tc > last_time:
                    new_closed += 1
            except (ValueError, TypeError):
                continue
        return new_closed >= BOT_CONFIG.dynamic.scholar_trigger_trades

    def meta_strategy_update(self, volatility: float, trend_strength: float) -> ScholarInsight:
        now = datetime.now(timezone.utc)
        closed = [
            t for t in self.db.get_trades(days_back=30) if t["status"] == "CLOSED"
        ][: BOT_CONFIG.dynamic.scholar_trigger_trades]
        if not closed:
            return self._empty_insight(now)

        wins = [t for t in closed if (t.get("pnl_usd") or 0) > 0]
        total_pnl = sum(t.get("pnl_usd") or 0 for t in closed)
        win_rate = len(wins) / max(len(closed), 1)

        analysis = "Gemini unavailable"
        lessons = self._rule_based_review(closed, volatility, win_rate)
        if self.llm.available:
            llm_result = self._llm_meta_review(closed, volatility, trend_strength)
            if llm_result:
                analysis, lessons = llm_result

        state = self.rl.discretize_state(volatility, trend_strength, win_rate)
        reward = (win_rate - 0.5) * 2 + float(
            np.clip((total_pnl / max(len(closed), 1)) / 100, -0.5, 0.5)
        )
        action = self.rl.choose(state)
        changes = self.rl.apply(action)
        self.rl.update(state, action, reward, state)

        if changes:
            self.db.save_config_snapshot(self._dynamic_proxy.snapshot(), changed_by="scholar")

        regime = "volatile" if volatility > 0.015 else "trending" if trend_strength > 0.6 else "ranging"

        insight = ScholarInsight(
            timestamp=now, period_start=now - timedelta(days=1), period_end=now,
            total_trades=len(closed), win_rate=win_rate, total_pnl=total_pnl,
            avg_risk_reward=0, lessons=lessons, parameter_changes=changes,
            market_regime=regime, reasoning=analysis,
        )
        self.db.add_scholar_review(insight)
        self.db.add_monologue(
            MonologueEntry(now, "Scholar",
                f"META-UPDATE: {len(closed)} trades | ${total_pnl:,.2f}", "action")
        )
        return insight

    def _llm_meta_review(self, trades: list, volatility: float, trend_strength: float) -> Optional[tuple]:
        proxy = self._dynamic_proxy
        prompt = (
            f"You are the Risk and Strategy Scholar for an Institutional Multi-Asset Crypto Bot.\n"
            f"Analyze {len(trades)} trades. Volatility: {volatility}, Trend: {trend_strength}.\n"
            f"Trades: {json.dumps([{'asset': t['symbol'], 'bias': t['bias'], 'pnl': t.get('pnl_usd')} for t in trades])}\n"
            f"Return strictly valid JSON: "
            f'{{"analysis": "your assessment", "lessons":["insight 1"], '
            f'"recommended_min_confidence": {proxy.min_confidence}}}'
        )
        raw = self.llm.generate(prompt)
        if not raw:
            return None
        parsed = self.llm.parse_json(raw)
        if not parsed:
            return (raw[:200], [])
        lessons = parsed.get("lessons", [])
        analysis = parsed.get("analysis", "")
        new_conf = parsed.get("recommended_min_confidence")
        if isinstance(new_conf, (int, float)):
            new_conf = float(np.clip(new_conf, 0.35, 0.80))
            old_conf = proxy.min_confidence
            if abs(new_conf - old_conf) > 0.01:
                proxy.safe_update({"min_confidence": round(new_conf, 2)})
                lessons.append(f"Adjusted min_confidence to {new_conf:.2f}")
        return (analysis, lessons)

    def _rule_based_review(self, trades: list, volatility: float, win_rate: float) -> List[str]:
        lessons: List[str] = []
        if win_rate < 0.4:
            lessons.append("Low win rate. Raising confidence threshold.")
        if volatility > 0.015:
            lessons.append("High vol. Widening SL to avoid noise stops.")
        if not lessons:
            lessons.append("Performance within acceptable range. Holding parameters.")
        return lessons

    def _empty_insight(self, now: datetime) -> ScholarInsight:
        return ScholarInsight(
            timestamp=now, period_start=now, period_end=now,
            total_trades=0, win_rate=0, total_pnl=0, avg_risk_reward=0,
            lessons=["No trades"], parameter_changes={},
            market_regime="unknown", reasoning="No trades",
        )


# --- END OF FILE scholar.py ---
