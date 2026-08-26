# --- START OF FILE notifier.py ---

import base64
import logging
import os
import subprocess
import sys
import threading
import time
import requests
from typing import Optional
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def _hidden_startup():
    """STARTUPINFO that hides the console for a child process on Windows.
    (No return annotation — subprocess.STARTUPINFO only exists on Windows.)"""
    if sys.platform != "win32":
        return None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return si
    except Exception:
        return None


class Notifier:

    SOUND_TRADE = "ms-winsoundevent:Notification.Mail"
    SOUND_WIN = "ms-winsoundevent:Notification.Mail"
    SOUND_LOSS = "ms-winsoundevent:Notification.Reminder"
    SOUND_URGENT = "ms-winsoundevent:Notification.Looping.Alarm"
    SOUND_SILENT = "silent"

    @staticmethod
    def _fmt_price(price) -> str:
        """Format a trade QUOTE with broker-grade precision (not 2 decimals, which
        makes entry/SL/TP on 5-digit pairs look identical, e.g. 1.91385 -> '1.91').
        Region-aware decimals: crypto/indices 2dp, JPY-style 3dp, majors 5dp.
        Dollar/P&L amounts should NOT use this — use :,.2f for those."""
        try:
            p = float(price)
        except (TypeError, ValueError):
            return "0"
        if p <= 0:
            return "0"
        if p >= 1000:      # BTC/ETH, indices
            return f"{p:,.2f}"
        if p >= 100:       # JPY crosses (GBPJPY ~190, AUDJPY ~112)
            return f"{p:,.3f}"
        if p >= 10:        # oil, silver (XAGUSD ~30.123)
            return f"{p:,.3f}"
        if p >= 1:         # EUR/GBP/AUD majors (5dp), metals
            return f"{p:,.5f}"
        return f"{p:,.5f}" # < 1.0 pairs

    @staticmethod
    def _html_escape(text) -> str:
        """Escape dynamic text before it enters a Telegram HTML message. A raw &
        (e.g. 'P&L') or < > breaks Telegram's strict HTML parser and the whole
        message is rejected (400 'can't parse entities') — which is why won/lost
        notifications were silently dropped while opened/limits (no '&') worked."""
        try:
            s = str(text)
        except Exception:
            return ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        try:
            return float(os.getenv(key, default))
        except (TypeError, ValueError):
            return default

    def _should_notify_placement(self, symbol: str, bias: str, entry_price: float) -> bool:
        """Suppress duplicate pending-limit placement notifications.

        A sniper limit that expires and is re-issued on the SAME pair+bias at a
        near-identical level within the cooldown is the same setup, not news —
        pinging Telegram each time looks like spam. We still notify when:
          - the cooldown elapsed, or
          - the new entry price moved beyond the tolerance (a genuinely new setup).
        Knobs: TG_NOTIFY_COOLDOWN_MIN (default 360 = 6h),
               TG_NOTIFY_PRICE_TOLERANCE_PCT (default 0.02 = 0.02% of price)."""
        cooldown_s = self._env_float("TG_NOTIFY_COOLDOWN_MIN", 360.0) * 60.0
        tol = self._env_float("TG_NOTIFY_PRICE_TOLERANCE_PCT", 0.02) / 100.0
        key = (str(symbol).upper(), str(bias).upper())
        now = time.time()
        last = self._notify_last.get(key)
        if last and (now - last["ts"]) < cooldown_s:
            lp = float(last.get("price") or 0.0)
            ep = float(entry_price or 0.0)
            if lp > 0 and ep > 0 and abs(ep - lp) / lp <= tol:
                return False  # same setup, same level, within cooldown -> suppress
        self._notify_last[key] = {"ts": now, "price": float(entry_price or 0.0)}
        return True

    def __init__(self, db=None, enabled: bool = True):
        self.db = db
        self.enabled = enabled
        self._ps: Optional[str] = None
        self._is_wsl = False
        self.toast_enabled = False
        self._running = True
        # FIX: init these in __init__ (previously only in the C2 listener thread),
        # so _is_admin() can't raise AttributeError if called before the thread runs.
        self._admin_cache = []
        self._last_admin_check = 0
        # NOTIFICATION DEDUP: suppresses duplicate "limit placed" pings when the
        # same pair+bias is re-issued at a near-identical price within a cooldown
        # (e.g. a snipe that expired and was re-placed at a slightly different
        # level would otherwise spam Telegram). Market fills are never suppressed.
        self._notify_last = {}

        if self.enabled:
            self._detect_environment()
            self._ps = self._find_powershell()
            if not self._ps:
                log.info("[Notifier] PowerShell not found. Desktop Toasts disabled.")
            else:
                self.toast_enabled = True
                log.info(
                    f"[Notifier] Desktop Toasts Active "
                    f"({'WSL' if self._is_wsl else 'Native'})"
                )

        # Start the Telegram C2 Listener if token exists
        if self.tg_enabled and self.db:
            threading.Thread(target=self._telegram_c2_listener, daemon=True).start()

    @property
    def tg_token(self):
        return os.getenv("TELEGRAM_BOT_TOKEN", "").strip("'\" ")

    @property
    def tg_chat_id(self):
        return os.getenv("TELEGRAM_CHAT_ID", "").strip("'\" ")

    @property
    def tg_enabled(self):
        return bool(self.tg_token and self.tg_chat_id)

    def trade_opened(
        self, symbol: str, bias: str, entry: float, sl: float,
        tp: float, rr: float, size: float, trade_id: str,
        sl_pct: float, order_type: str = "MARKET",
    ):
        # NOTIFICATION DEDUP: only pending-limit placements are deduped (a snipe
        # re-issued at the same level within the cooldown is the same setup).
        # Market executions are real fills — always notified.
        if order_type == "LIMIT" and not self._should_notify_placement(symbol, bias, entry):
            log.info(f"[Notifier] Suppressed duplicate {bias} LIMIT placement ping for {symbol} "
                     f"({self._fmt_price(entry)}) — same setup re-issued within cooldown.")
            return

        action = "BUY" if bias == "LONG" else "SELL"
        if order_type == "LIMIT":
            title = f"{action} LIMIT ORDER PLACED"
            msg_header = f"<b>{action} LIMIT ORDER PLACED:</b> <b>{symbol}</b>"
        elif order_type == "FILLED":
            title = f"LIMIT {action} FILLED"
            msg_header = f"<b>LIMIT {action} FILLED:</b> <b>{symbol}</b>"
        else:
            title = f"{action} MARKET ORDER EXECUTED"
            msg_header = f"<b>{action} MARKET ORDER EXECUTED:</b> <b>{symbol}</b>"

        self._dispatch(
            f"{title} | {symbol}",
            f"Entry: ${self._fmt_price(entry)}\nSL: ${self._fmt_price(sl)} ({sl_pct:.2f}%)\n"
            f"TP: ${self._fmt_price(tp)}\nSize: ${size:,.0f} | R:R {rr:.1f}:1",
            self.SOUND_TRADE,
            f"{msg_header}\nID: <code>{trade_id}</code>\n\n"
            f"Size: <code>${size:,.0f}</code> | R:R <code>{rr:.1f}:1</code>\n"
            f"Entry: <code>${self._fmt_price(entry)}</code>\n"
            f"SL: <code>${self._fmt_price(sl)}</code> ({sl_pct:.2f}%)\n"
            f"TP: <code>${self._fmt_price(tp)}</code>",
        )

    def clear_placement_dedup(self, symbol: str, bias: str):
        """Forget the 'LIMIT placed' dedup key for a pair+bias. Call this when a
        resting limit is CANCELLED/EXPIRED so the NEXT placement of that pair is
        treated as a brand-new order and always notified — otherwise a snipe that
        re-fires minutes later would be silently suppressed (the user only ever
        sees the FILLED ping)."""
        try:
            key = (str(symbol).upper(), str(bias).upper())
            self._notify_last.pop(key, None)
        except Exception:
            pass

    def pending_cancelled(self, symbol: str, bias: str, entry: float, reason: str = ""):
        """A resting limit order was cancelled without a fill (expired, momentum
        guard, conviction reversal, or it vanished at the broker). This is the
        counterpart to 'LIMIT ORDER PLACED' — the user must know the setup is
        gone even though nothing was filled on MT5."""
        action = "BUY" if bias == "LONG" else "SELL"
        title = f"{action} LIMIT CANCELLED"
        msg_header = f"<b>{action} LIMIT ORDER CANCELLED:</b> <b>{symbol}</b>"
        reason_line = f"\nReason: <i>{self._html_escape(reason)}</i>" if reason else ""
        self._dispatch(
            f"{title} | {symbol}",
            f"Limit at ${self._fmt_price(entry)} was cancelled — not filled.{reason_line}",
            self.SOUND_SILENT,
            f"{msg_header}\nEntry: <code>${self._fmt_price(entry)}</code>{reason_line}",
        )

    def trade_closed(
        self, symbol: str, bias: str, entry: float, exit_price: float,
        pnl_usd: float, pnl_pct: float, reason: str, trade_id: str,
    ):
        is_win = pnl_usd > 0
        word = "WIN" if is_win else "LOSS"
        sign = "+" if pnl_usd > 0 else ""
        self._dispatch(
            f"{word} {sign}${abs(pnl_usd):,.2f} ({sign}{pnl_pct:.2f}%)",
            f"{symbol} {bias}\n"
            f"Entry: ${self._fmt_price(entry)}\nExit: ${self._fmt_price(exit_price)}\nReason: {reason}",
            self.SOUND_WIN if is_win else self.SOUND_LOSS,
            f"<b>TRADE {word}:</b> <b>{symbol} {bias}</b>\n"
            f"P&amp;L: <b>{sign}${pnl_usd:,.2f}</b> ({sign}{pnl_pct:.2f}%)\n\n"
            f"Reason: {self._html_escape(reason)}\nEntry: <code>${self._fmt_price(entry)}</code>\n"
            f"Exit: <code>${self._fmt_price(exit_price)}</code>\nID: <code>{trade_id}</code>",
        )

    def _telegram_c2_listener(self):
        """Background thread that polls Telegram for commands securely."""
        log.info("[Telegram C2] Secure Mobile Command Interface Active.")
        offset = None

        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self.tg_token}/getUpdates?timeout=10"
                if offset: url += f"&offset={offset}"

                response = requests.get(url, timeout=15).json()
                if response.get("ok"):
                    for update in response.get("result", []):
                        offset = update["update_id"] + 1

                        # ARCHITECT FIX: Support both Groups ('message') and Channels ('channel_post')
                        message = update.get("message") or update.get("channel_post") or {}

                        # Strip ALL invisible characters, spaces, and newlines from both IDs
                        chat_id = str(message.get("chat", {}).get("id", "")).strip()
                        saved_id = str(self.tg_chat_id).strip()

                        chat_type = message.get("chat", {}).get("type", "")
                        user_id = message.get("from", {}).get("id")
                        first_name = message.get("from", {}).get("first_name", "Unknown")
                        text = message.get("text", "").strip()

                        # SECURITY LAYER 1: Ignore messages outside your registered Chat ID
                        if chat_id == saved_id and text.startswith("/"):

                            # SECURITY LAYER 2: Context-Aware Authorization
                            # If it's a Channel, only admins can post, so it's naturally secure.
                            # If it's a Group/DM, we run the strict RBAC admin check.
                            is_authorized = True if chat_type == "channel" else self._is_admin(user_id, chat_id)

                            if is_authorized:
                                self._process_mobile_command(text)
                            else:
                                log.warning(f"Unauthorized command by {first_name}: {text}")
                                self._dispatch("Security Alert", "Unauthorized Access", self.SOUND_URGENT,
                                    f"<b>SECURITY ALERT</b>\nUser <b>{self._html_escape(first_name)}</b> attempted unauthorized access.\nBlocked: <code>{self._html_escape(text)}</code>")

            except Exception as e:
                time.sleep(5)

    def _is_admin(self, user_id, chat_id) -> bool:
        if not user_id: return False
        if str(user_id) == str(chat_id): return True
        now = time.time()
        if not self._admin_cache or (now - self._last_admin_check > 3600):
            try:
                resp = requests.get(f"https://api.telegram.org/bot{self.tg_token}/getChatAdministrators?chat_id={chat_id}", timeout=10).json()
                if resp.get("ok"):
                    self._admin_cache = [a["user"]["id"] for a in resp.get("result", [])]
                    self._last_admin_check = now
            except: return False
        return user_id in self._admin_cache

    def _process_mobile_command(self, text: str):
        # This splits "/status@bih0lar_bot" and keeps only "/status"
        command = text.lower().strip().split('@')[0]

        if command == "/status":
            self.db.set_system_flag("manual_override", "SEND_STATUS")
            self._dispatch("Command Received", "Fetching live data...", self.SOUND_SILENT, "<i>Fetching live account status...</i>")
        elif command == "/close_all":
            self.db.set_system_flag("manual_override", "CLOSE_ALL")
            self._dispatch("Panic", "Closing all", self.SOUND_URGENT, "<b>COMMAND RECEIVED:</b> Closing all open positions.")
        elif command == "/close_winners":
            self.db.set_system_flag("manual_override", "CLOSE_WINNERS")
            self._dispatch("Profit Securing", "Closing winners", self.SOUND_WIN, "<i>Scanning for profitable positions...</i>")
        elif command.startswith("/close "):
            parts = command.split(" ")
            if len(parts) > 1:
                symbol = parts[1].upper()
                self.db.set_system_flag("manual_override", f"CLOSE_SYMBOL_{symbol}")
                self._dispatch("Surgical Close", f"Closing {symbol}", self.SOUND_URGENT, f"<i>Attempting to close {symbol}...</i>")
        elif command == "/pause":
            self.kill_switch(True)
            self.db.set_system_flag("kill_switch", "TRUE")
        elif command == "/resume":
            self.kill_switch(False)
            self.db.set_system_flag("kill_switch", "FALSE")

    def trailing_stop_moved(
        self, symbol: str, bias: str, trade_id: str,
        old_sl: float, new_sl: float, price: float,
    ):
        self._dispatch(
            f"Trailing Stop Updated | {symbol}",
            f"{bias}\nSL: ${self._fmt_price(old_sl)} -> ${self._fmt_price(new_sl)}\nPrice: ${self._fmt_price(price)}",
            self.SOUND_SILENT,
            f"<b>TRAILING STOP UPDATED</b> ({symbol} {bias})\n"
            f"ID: <code>{trade_id[:8]}</code>\n\n"
            f"Price: <code>${self._fmt_price(price)}</code>\n"
            f"SL moved: <code>${self._fmt_price(old_sl)}</code> -> <code>${self._fmt_price(new_sl)}</code>",
        )

    def profit_milestone(self, symbol: str, bias: str, milestone: int, profit: float, ticket: str):
        self._dispatch(
            f"Profit Milestone | {symbol} +${milestone}",
            f"{bias} trade is floating at +${profit:,.2f}",
            self.SOUND_WIN,
            f"<b>PROFIT MILESTONE REACHED</b>\n\n"
            f"Symbol: <b>{symbol} {bias}</b>\n"
            f"Floating Profit: <code>+${profit:,.2f}</code>\n\n"
            f"<i>Consider closing manually via the MT5 mobile app to lock in gains.</i>",
        )

    def scholar_review(
        self, trades_reviewed: int, win_rate: float,
        total_pnl: float, regime: str,
    ):
        sign = "+" if total_pnl >= 0 else ""
        self._dispatch(
            f"Scholar Meta-Review | {trades_reviewed} trades",
            f"Win Rate: {win_rate:.0%}\nP&amp;L: {sign}${abs(total_pnl):,.2f}\nRegime: {regime}",
            self.SOUND_TRADE,
            f"<b>SCHOLAR META-REVIEW</b>\nReviewed {trades_reviewed} trades.\n\n"
            f"Win Rate: {win_rate:.0%}\nP&amp;L: {sign}${total_pnl:,.2f}\n"
            f"Market Regime: {regime.capitalize()}\n"
            f"<i>Parameter adjustments may have been applied; review the dashboard for details.</i>",
        )

    # ARCHITECT UPGRADE: Daily Digest formatting
    def daily_digest(self, balance: float, pnl_usd: float, pnl_pct: float, win_rate: float, wins: int, losses: int):
        sign = "+" if pnl_usd >= 0 else ""
        date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

        t_title = f"Daily Digest | {sign}${abs(pnl_usd):,.2f}"
        t_body = f"Win Rate: {win_rate:.0%} ({wins}W / {losses}L)\nBalance: ${balance:,.2f}"

        tg_msg = (
            f"<b>ALADIN PRO: DAILY DIGEST</b>\n"
            f"Date: {date_str}\n\n"
            f"<b>Ending Balance:</b> <code>${balance:,.2f}</code>\n"
            f"<b>Daily P&amp;L:</b> <code>{sign}${abs(pnl_usd):,.2f} ({sign}{pnl_pct:.2f}%)</code>\n"
            f"<b>Win Rate Today:</b> <code>{win_rate:.0%} ({wins} Wins / {losses} Losses)</code>\n\n"
            f"<i>System idle. Ready for the next session.</i>"
        )

        self._dispatch(t_title, t_body, self.SOUND_WIN if pnl_usd >= 0 else self.SOUND_LOSS, tg_msg)

    def kill_switch(self, activated: bool):
        if activated:
            self._dispatch(
                "KILL SWITCH ACTIVATED", "All trading halted immediately.",
                self.SOUND_URGENT,
                "<b>KILL SWITCH ACTIVATED</b>\nAll trading halted.",
            )
        else:
            self._dispatch(
                "KILL SWITCH DEACTIVATED", "Trading operations resumed.",
                self.SOUND_TRADE,
                "<b>KILL SWITCH DEACTIVATED</b>\nTrading operations resumed.",
            )

    def alert(self, title: str, message: str, urgent: bool = False):
        self._dispatch(
            title, message,
            self.SOUND_URGENT if urgent else self.SOUND_LOSS,
            f"<b>{self._html_escape(title)}</b>\n{self._html_escape(message)}",
        )

    def prospective_setup(self, symbol: str, bias: str, confidence: float, reason: str):
        self._dispatch(
            f"Setup Watch: {symbol} {bias} ({confidence:.0%})",
            f"Status: {reason}\nKeep an eye on this chart.",
            self.SOUND_SILENT, # Keeps it a quiet notification so it doesn't wake you up
            f"<b>SETUP DETECTED (AWAITING ENTRY)</b>\n\n"
            f"<b>Asset:</b> {symbol}\n"
            f"<b>Intended Bias:</b> {bias}\n"
            f"<b>SMC Score:</b> {confidence:.0%}\n\n"
            f"<b>Status:</b> <i>{self._html_escape(reason)}</i>\n\n"
            f"<i>The system is awaiting its entry criteria; manual execution may be considered.</i>"
        )

    def _dispatch(self, t_title: str, t_body: str, sound: str, tg_msg: str):
        if self.enabled:
            threading.Thread(
                target=self._run_dispatch,
                args=(t_title, t_body, sound, tg_msg),
                daemon=True,
            ).start()

    def _run_dispatch(self, t_title: str, t_body: str, sound: str, tg_msg: str):
        if self.tg_enabled:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    json={
                        "chat_id": self.tg_chat_id,
                        "text": tg_msg,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10,
                )
                if r.status_code != 200:
                    log.error(f"[Notifier] Telegram rejected: {r.text}")
            except Exception as e:
                log.error(f"[Notifier] Telegram exception: {e}")

        if self.toast_enabled:
            try:
                self._send_toast(t_title, t_body, sound)
                if sound != self.SOUND_SILENT:
                    self._play_backup_sound(sound)
            except Exception as e:
                log.warning(f"[Notifier] Desktop Toast dispatch failed: {e}")

    def _send_toast(self, title: str, body: str, sound: str):
        title_b64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
        body_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")

        if sound == self.SOUND_SILENT:
            audio_tag = '<audio silent="true"/>'
        else:
            audio_tag = f'<audio src="{sound}" loop="{"true" if "Looping" in sound else "false"}"/>'

        ps_script = f"""
try {{
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

  $title = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{title_b64}'))
  $body = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{body_b64}'))

  $t = @"
  <toast duration="{'long' if 'Looping' in sound else 'short'}">
    <visual><binding template="ToastGeneric"><text>$([System.Security.SecurityElement]::Escape($title))</text><text>$([System.Security.SecurityElement]::Escape($body))</text></binding></visual>
    {audio_tag}
  </toast>
"@
  $x = New-Object [Windows.Data.Xml.Dom.XmlDocument]
  $x.LoadXml($t)
  $n = [Windows.UI.Notifications.ToastNotification]::new($x)
  $appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($n)
}} catch {{ }}
"""

        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        kwargs = {"capture_output": True, "text": True, "timeout": 15}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run([self._ps, "-NoProfile", "-EncodedCommand", encoded], **kwargs)

    def _play_backup_sound(self, sound: str):
        try:
            if sys.platform == "win32":
                import winsound
                if sound == self.SOUND_URGENT:
                    winsound.MessageBeep(winsound.MB_ICONHAND)
                elif sound == self.SOUND_LOSS:
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                elif sound in (self.SOUND_WIN, self.SOUND_TRADE):
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                else:
                    winsound.MessageBeep(winsound.MB_OK)
            elif self._is_wsl and self._ps:
                freq = {
                    self.SOUND_URGENT: 1000, self.SOUND_LOSS: 600,
                    self.SOUND_WIN: 800, self.SOUND_TRADE: 850,
                }.get(sound, 750)
                _si_hide = _hidden_startup()
                subprocess.run(
                    [self._ps, "-NoProfile", "-Command", f"[Console]::Beep({freq}, 300)"],
                    capture_output=True, timeout=5,
                    startupinfo=_si_hide,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except Exception:
            pass

    def _detect_environment(self):
        try:
            with open("/proc/version", "r", encoding="utf-8") as f:
                content = f.read().lower()
                self._is_wsl = "microsoft" in content or "wsl" in content
        except Exception:
            self._is_wsl = False

    def _find_powershell(self) -> Optional[str]:
        if self._is_wsl:
            candidates = [
                "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                "/mnt/c/Windows/SysWOW64/WindowsPowerShell/v1.0/powershell.exe",
                "powershell.exe",
            ]
        else:
            candidates = [
                os.path.join(
                    os.environ.get("SystemRoot", r"C:\Windows"),
                    "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
                ),
                "powershell.exe",
            ]
        for p in candidates:
            try:
                kwargs = {"capture_output": True, "text": True, "timeout": 10}
                if sys.platform == "win32":
                    # hide the console: powershell.exe from a windowed app would
                    # otherwise flash a window on every notifier start
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0
                    kwargs["startupinfo"] = si
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                result = subprocess.run(
                    [p, "-NoProfile", "-Command", "Write-Output ok"], **kwargs)
                if result.returncode == 0:
                    return p
            except Exception:
                continue
        return None
