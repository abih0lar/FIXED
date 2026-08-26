"""
main.py — Headless Trading Bot Entry Point (MT5 / Exness)
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from orchestrator import Orchestrator

# ARCHITECT FIX: Log Rotation implemented. Capped at 5MB per file, keeping the last 5 files.
# Logs live in data/ next to the app, alongside the DB and config.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(os.path.join(_DATA_DIR, "bot.log"), maxBytes=5*1024*1024, backupCount=5, encoding="utf-8"),
    ],
)
log = logging.getLogger("__main__")

_orchestrator: Orchestrator | None = None


def _shutdown(signum, frame):
    log.info("Shutdown signal received — stopping orchestrator...")
    if _orchestrator is not None:
        _orchestrator.stop()
    log.info("Bot stopped cleanly. Goodbye.")
    sys.exit(0)


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def main():
    global _orchestrator

    log.info("=" * 60)
    log.info(" ALADIN Trading System (MT5 / Exness)")
    log.info(f" Started at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info("=" * 60)

    log.info("Initialising Orchestrator...")
    _orchestrator = Orchestrator()

    log.info("Starting background trading loop...")
    _orchestrator.start()

    log.info("Bot is live. Press Ctrl+C to stop.")
    log.info("Monitor via Exness MetaTrader 5 terminal.")
    log.info("-" * 60)

    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — shutting down...")
            _shutdown(signal.SIGINT, None)
        except Exception as e:
            log.error("Heartbeat error: %s", e)


if __name__ == "__main__":
    main()
