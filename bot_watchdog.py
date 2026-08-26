# --- START OF FILE bot_watchdog.py ---
"""
ALADIN Watchdog
---------------
Launches main.py and keeps it alive:
  * if the bot process dies (crash, exception, disconnect), it's restarted
  * writes a pidfile so the control panel can detect status
  * stops cleanly on SIGTERM (from the control panel's STOP button)

Run:  python bot_watchdog.py
"""
import os
import sys
import time
import signal
import subprocess
import logging
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass
PIDFILE = os.path.join(DATA_DIR, ".bot.pid")
WATCH_LOG = os.path.join(DATA_DIR, "watchdog.log")
BOT_STDERR_LOG = os.path.join(DATA_DIR, "bot_stderr.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | watchdog | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(WATCH_LOG, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")

_running = True
_bot: subprocess.Popen | None = None
_bot_err = None


def _handle_stop(signum, frame):
    global _running
    log.info("Stop signal received — shutting down bot.")
    _running = False
    if _bot is not None and _bot.poll() is None:
        try:
            _bot.terminate()
        except Exception:
            pass


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


def _write_pidfile(bot_pid=None):
    """pidfile format: line1 = watchdog pid, line2 = bot pid (0 if none)."""
    try:
        with open(PIDFILE, "w") as f:
            f.write(f"{os.getpid()}\n{bot_pid or 0}\n")
    except Exception:
        pass


def main():
    global _bot, _bot_err
    log.info("=" * 56)
    log.info(" ALADIN watchdog starting")
    log.info("=" * 56)
    _write_pidfile()
    log.info(f"watchdog pid {os.getpid()}")

    consecutive_fails = 0
    bot_started_at = 0.0
    while _running:
        if _bot is not None and _bot.poll() is None:
            time.sleep(5)          # bot alive: just poll
            continue

        # ── bot is dead (or not started yet) ──
        if _bot is not None:
            rc = _bot.poll()
            alive_for = time.time() - bot_started_at
            # only count as a "crash" if it died quickly; a bot that ran 30s+
            # was healthy — reset the counter so one bad day doesn't trip us
            if alive_for >= 30:
                consecutive_fails = 0
            else:
                consecutive_fails += 1
            log.info(
                f"bot exited with code {rc} after {alive_for:.0f}s "
                f"({'restarting' if _running else 'shutting down'})"
            )
        if not _running:
            break

        # ── exponential backoff on rapid crashes (no 5s hammering) ──
        delay = int(min(5 * (2 ** max(consecutive_fails - 1, 0)), 120))
        if consecutive_fails >= 3:
            log.critical(
                f"{consecutive_fails} rapid crashes — backing off {delay}s "
                f"(see bot_stderr.log for the reason)"
            )
        if delay > 0:
            time.sleep(delay)

        # ── spawn the bot (console fully hidden on Windows) ──
        try:
            if _bot_err is not None:
                try:
                    _bot_err.close()
                except Exception:
                    pass
            _bot_err = open(BOT_STDERR_LOG, "ab")
            _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            _si = None
            if sys.platform == "win32":
                try:
                    _si = subprocess.STARTUPINFO()
                    _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    _si.wShowWindow = 0  # SW_HIDE: no console flash
                except Exception:
                    _si = None
            _bot = subprocess.Popen(
                [sys.executable, "-u", os.path.join(BASE_DIR, "main.py")],
                cwd=BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=_bot_err,
                creationflags=_flags,
                startupinfo=_si,
            )
            bot_started_at = time.time()
            _write_pidfile(_bot.pid)
            log.info(f"bot started (pid {_bot.pid})")
        except Exception as e:
            log.error(f"failed to start bot: {e}")
            consecutive_fails += 1
            if consecutive_fails >= 8:
                log.critical("8 consecutive start failures — giving up to avoid a crash loop.")
                break
            time.sleep(10)
            continue

    # cleanup
    _write_pidfile()
    if _bot_err is not None:
        try:
            _bot_err.close()
        except Exception:
            pass
    try:
        os.remove(PIDFILE)
    except Exception:
        pass
    log.info("watchdog stopped.")


if __name__ == "__main__":
    main()
