# --- START OF FILE app.py ---
"""
ALADIN — Institutional Control Panel (PySide6 / Qt)
====================================================
A proper Qt desktop control panel for the ALADIN bot. Manages the bot as a
separate process (watchdog -> main.py). The panel never trades.

Design:
  * nature photo background with a dimmed, glass card (single dark theme)
  * Qt Style Sheets (DARK_QSS) — real theming, not canvas hacks
  * icon-less text buttons (colored, hover, disabled) for START/STOP/SAVE/SIGNALS
  * tabs: License / Broker / Strategy / Telegram / Dashboard (lazy-built)
  * live log console (bounded) + signals dashboard (win rate, P&L, equity curve)
  * license gate: the bot cannot start without a valid, unexpired key
  * robust: stale-lock recovery, friendly error dialog, graceful asset fallback

Deps: pip install PySide6 python-dotenv cryptography pillow
Run:  python app.py
"""
import os
import sys
import csv
import time
import signal
import base64
import atexit
import shutil
import urllib.request
import subprocess
import threading
import queue
import json
from datetime import datetime, timezone, date

from PySide6.QtCore import Qt, QTimer, QSize, Signal
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QLinearGradient, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QFrame, QVBoxLayout, QHBoxLayout,
    QGridLayout, QTabWidget, QPushButton, QLineEdit, QPlainTextEdit, QScrollArea,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog, QSizePolicy,
)
from dotenv import load_dotenv

def _ensure_data_dir():
    """Create the data folder (all writable runtime files live here).
    Migrates an existing BASE_DIR/.env into data/ once, so upgrades keep the
    user's settings without any manual move. (Paths computed inline so this
    can run before the constants block below.)"""
    _data = os.path.join(BASE_DIR, "data")
    try:
        os.makedirs(_data, exist_ok=True)
    except Exception:
        pass
    try:
        old_env = os.path.join(BASE_DIR, ".env")
        if os.path.exists(old_env) and not os.path.exists(os.path.join(_data, ".env")):
            import shutil as _sh
            _sh.copy2(old_env, os.path.join(_data, ".env"))
    except Exception:
        pass
    # database folder
    _db = os.path.join(BASE_DIR, "db")
    try:
        os.makedirs(_db, exist_ok=True)
    except Exception:
        pass
    # DB housekeeping: ensure trading_bot.db (+ sidecars) live ONLY in db/.
    #   * if db/ has no DB yet, migrate one from data/ or the project root
    #     (keeps trade history) and remove the originals + their -wal/-shm.
    #   * if db/ already has the canonical DB, any root/data strays are
    #     orphans from before the fix — remove them so they never confuse a
    #     user or a future scan.
    try:
        import shutil as _sh
        _target = os.path.join(_db, "trading_bot.db")
        _candidates = (os.path.join(_data, "trading_bot.db"),
                       os.path.join(BASE_DIR, "trading_bot.db"))
        if not os.path.exists(_target):
            for _old in _candidates:
                try:
                    if os.path.exists(_old) and os.path.getsize(_old) > 0:
                        _sh.copy2(_old, _target)
                        for _suffix in ("-wal", "-shm"):
                            if os.path.exists(_old + _suffix):
                                try:
                                    _sh.copy2(_old + _suffix, _target + _suffix)
                                except Exception:
                                    pass
                        break
                except Exception:
                    continue
        # remove stray originals wherever they are (canonical now in db/)
        for _old in _candidates:
            try:
                if os.path.exists(_old):
                    os.remove(_old)
            except Exception:
                pass
            for _suffix in ("-wal", "-shm"):
                try:
                    if os.path.exists(_old + _suffix):
                        os.remove(_old + _suffix)
                except Exception:
                    pass
    except Exception:
        pass
    return _data


# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDED CONFIG — a SANITIZED .env (all strategy settings, credentials
# blanked) is stamped into the exe at build time by inject_env.py, so
# ALADIN.exe works out of the box yet NEVER ships the builder's MT5 password
# or DRM secret. Each user enters their OWN MT5 account in the Broker tab.
# On first launch the embedded copy is materialized as .env next to the exe;
# an existing .env is never overwritten (live edits win).
# ─────────────────────────────────────────────────────────────────────────────
_EMBEDDED_ENV_B64 = "CiMg4pSA4pSAIEV4bmVzcyBNVDUg4pSA4pSACkVYQ0hBTkdFX0lEPSdFWE5FU1MnCk1UNV9MT0dJTj0nJwpNVDVfUEFTU1dPUkQ9JycKTVQ1X1NFUlZFUj0nJwpNVDVfUEFUSD0nJwpFWENIQU5HRV9NQVJLRVRfVFlQRT0nZm9yZXgnCk1BUkdJTl9NT0RFPWhlZGdpbmcKCiMg4pSA4pSAIFJpc2sgTWFuYWdlbWVudCDilIDilIAKTUFYX09QRU5fUE9TSVRJT05TPSc1JwojIFJpc2sgcGVyIHRyYWRlIChvZiBlcXVpdHkpLiBPbiBhIHRpbnkgYWNjb3VudCB0aGlzIGNhbiBkZW55IHN5bWJvbHMgd2hvc2UKIyBtaW5pbXVtIGxvdCByaXNrcyB0b28gbXVjaCDigJQgdGhhdCBpcyBpbnRlbnRpb25hbCBwcm90ZWN0aW9uLiBSYWlzZSB3aXRoIGNhcmUuCk1BWF9SSVNLX1BFUl9UUkFERV9QQ1Q9Mi4wCk1BWF9EUkFXRE9XTl9QQ1Q9JzUwLjAnCkNSWVBUT19MT1RfTVVMVElQTElFUj0nMy4wJwpGT1JFWF9MT1RfTVVMVElQTElFUj0nNS4wJwpNSU5fQ09ORklERU5DRT0nMC4zNScKCiMg4pSA4pSAIFdpbi1SYXRlIFR1bmluZyAoOTBkLzYwZCBiYWNrdGVzdCwgQXVnIDIwMjYpIOKUgOKUgApUUF9UQVJHRVRfUlI9MS41Ck1JTl9SUj0nMS4wJwpGVkdfRU5UUllfVE9MRVJBTkNFPTIuMApUU0xfSEFMRl9SSVNLX1I9MC40Ck1JTl9BRFg9MC4wCkhURl9aT05FX0ZJTFRFUj1mYWxzZSAgICMgPC0tIFRIRSBiaWcgb25lOiBtYXRjaGVzIHRoZSBiYWNrdGVzdGVkIGNvbmZpZwpNSU5fRlZHX0dBUF9QQ1Q9MC4wMDAxICAgIyBBL0ItdmVyaWZpZWQ6IHNhbWUgZWRnZSwgfjIwJSBtb3JlIHRyYWRlcyB0aGFuIDAuMDAwMgoKIyDilIDilIAgQ3J5cHRvIENvZXhpc3RlbmNlIChCVEMvRVRIIGJyYW5jaDsgZm9yZXggdmFsdWVzIGFib3ZlIHVudG91Y2hlZCkg4pSA4pSACkNSWVBUT19NSU5fQ09ORklERU5DRT0wLjQ1CkNSWVBUT19UUF9UQVJHRVRfUlI9MS41CkNSWVBUT19GVkdfRU5UUllfVE9MRVJBTkNFPTIuMApDUllQVE9fTUlOX0ZWR19HQVBfUENUPTAuMDAwMQpDUllQVE9fU0xfQlVGRkVSX0FUUl9NVUxUPTAuNzUKQ1JZUFRPX01JTl9SUj0xLjAKQ1JZUFRPX0hURl9aT05FX0ZJTFRFUj1mYWxzZQpDUllQVE9fRU5UUllfVEY9MWgKQ1JZUFRPX01BUktFVF9FTlRSWV9DT05GPTAuNjUgICAjIGNvbmY+PTAuNjUgY3J5cHRvIC0+IG1hcmtldCBlbnRyeSAoYmFja3Rlc3QtdmVyaWZpZWQpCgojIOKUgOKUgCBTbmlwZXIgZW50cnkgKHBlbmRpbmcgbGltaXQgYXQgRlZHIGVkZ2UpIOKUgOKUgApMSU1JVF9FWFBJUllfSE9VUlM9MiAgICMgc25pcGVyIHBlbmRpbmcgbGltaXRzIGF1dG8tY2FuY2VsIGFmdGVyIDJoIChmYXN0IGJvb2sgdHVybm92ZXIpCgojIOKUgOKUgCBOb3RpZmljYXRpb24gZGVkdXAgKHByZXZlbnRzIHNuaXBlIHJlLXBsYWNlbWVudCBzcGFtIG9uIFRlbGVncmFtKSDilIDilIAKVEdfTk9USUZZX0NPT0xET1dOX01JTj0zNjAgICAgICAgICMgc3VwcHJlc3MgcmUtcGluZyBvZiBzYW1lIHBhaXIrYmlhcyB3aXRoaW4gNmgKCiMg4pSA4pSAIFBvc3QtbG9zcyBjb29sZG93biAocGVyLXN5bWJvbCBicmFrZSBhZnRlciBhIGxvc2luZyBjbG9zZSkg4pSA4pSACkNPT0xET1dOX0hPVVJTPScyJwpUR19OT1RJRllfUFJJQ0VfVE9MRVJBTkNFX1BDVD0wLjAyICAjIHVubGVzcyBlbnRyeSBtb3ZlZCA+MC4wMiUgKG5ldyBzZXR1cCkKCiMg4pSA4pSAIE1vbWVudHVtIGd1YXJkcyAoY291bnRlciBtb21lbnR1bSBzaGlmdHMpIOKUgOKUgApQRU5ESU5HX0NBTkNFTF9ESVNUQU5DRV9QQ1Q9MC4zICAgIyBjYW5jZWwgdW5maWxsZWQgc25pcGUgaWYgcHJpY2UgcnVucyAwLjMlIGF3YXkKTU9NRU5UVU1fQkVfUj0wLjUgICAgICAgICAgICAgICAgICMgZmlsbGVkIHRyYWRlOiBFTUEgZmxpcCAtPiBicmVha2V2ZW4gb25jZSArMC41UgoKIyDilIDilIAgTWV0YWxzIChnb2xkL3NpbHZlciDigJQgRVhQRVJJTUVOVEFMLCBmb3J3YXJkLXRlc3Qgb24gZGVtbykg4pSA4pSACiMgQWRkIFhBVVVTRG0gKGdvbGQpIHRvIERBWS9OSUdIVF9TWU1CT0xTIHRvIGVuYWJsZS4gNjBkLzE1bSBidDogNzElIFdSLCArMC4zMVIuCk1FVEFMX01JTl9DT05GSURFTkNFPTAuMzUKTUVUQUxfVFBfVEFSR0VUX1JSPTIuMApNRVRBTF9GVkdfRU5UUllfVE9MRVJBTkNFPTMuMApNRVRBTF9NSU5fRlZHX0dBUF9QQ1Q9MC4wMDAxCk1FVEFMX1NMX0JVRkZFUl9BVFJfTVVMVD0wLjc1Ck1FVEFMX01JTl9SUj0xLjAKTUVUQUxfSFRGX1pPTkVfRklMVEVSPWZhbHNlCgojIOKUgOKUgCBHZW1pbmkgQUkg4pSA4pSACgpHRU1JTklfQVBJX0tFWT0nJwpHRU1JTklfTU9ERUw9Z2VtaW5pLTEuNS1mbGFzaApHRU1JTklfVEVNUEVSQVRVUkU9MC43CkdFTUlOSV9NQVhfVE9LRU5TPTIwNDgKCkFMQURJTl9EUk1fS0VZPScnCgojIOKUgOKUgCBUZWxlZ3JhbSDilIDilIAKVEVMRUdSQU1fQk9UX1RPS0VOPScnClRFTEVHUkFNX0NIQVRfSUQ9JycKTk9USUZZX0RFQlVHPSdmYWxzZScKCiMg4pSA4pSAIEJvdCDilIDilIAKQk9UX0RCX1BBVEg9dHJhZGluZ19ib3QuZGIKTUFYX1BPU0lUSU9OX1NJWkVfVVNEPTEwMApFWEVDVVRJT05fV0lORE9XX1NUQVJUPTAKRVhFQ1VUSU9OX1dJTkRPV19FTkQ9NTkKREFZX1NZTUJPTFM9J0VVUlVTRG0sIEdCUFVTRG0sIEVVUkpQWW0sIEdCUEpQWW0sIENBRENIRm0sIEJUQ1VTRG0sIEVUSFVTRG0sIFVTRENBRG0sIFVTRENIRm0sIEdCUEpQWW0sIEdCUENBRG0nCk5JR0hUX1NZTUJPTFM9J0FVREpQWW0sIEFVRE5aRG0sIEFVRENBRG0sIEVVUlVTRG0sIE5aRFVTRG0sIE5aREpQWW0sIEFVRENBRG0sIEFVRE5aRG0sIEVVUkFVRG0sIEdCUEFVRG0sIEdCUFVTRG0sIEFVRENIRm0sIEFVRFVTRG0sIFVTREpQWW0sIENBREpQWW0sIENIRkpQWW0sIEJUQ1VTRG0sIEVUSFVTRG0sIEdCUEpQWW0sIEVVUkpQWW0nCkRBSUxZX1BST0ZJVF9UQVJHRVQ9JzM1JCcKTUFYX0RBSUxZX0xJTUlUPScxMCQnCgpMSUNFTlNFX0tFWT0nJwoKIyDilIDilIAgRWNvbm9taWMgQ2FsZW5kYXIgKG5ld3MpIGZpbHRlciDilIDilIAKTkVXU19GSUxURVJfRU5BQkxFRD10cnVlCk5FV1NfQkxPQ0tfQkVGT1JFX01JTlM9MTUKTkVXU19CTE9DS19BRlRFUl9NSU5TPTMwCgojIOKUgOKUgCBTY2hvbGFyIG1ldGEtb3B0aW1pemVyIOKUgOKUgApTQ0hPTEFSX0FVVE9fVFVORT1mYWxzZQo="


def _ensure_env():
    """Write the embedded .env to disk when none exists (frozen exe case)."""
    if _EMBEDDED_ENV_B64 and not os.path.exists(ENV_FILE):
        try:
            import base64
            data = base64.b64decode(_EMBEDDED_ENV_B64).decode("utf-8")
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# LICENSE SECRET — injected at BUILD TIME by inject_env.py from the builder's
# local data/.env (ALADIN_DRM_KEY). The repo/source only ever carries the
# placeholder below; a real secret must never be committed. If the placeholder
# survives into a build, license validation reports "DRM secret missing"
# instead of silently validating against a public string.
# ─────────────────────────────────────────────────────────────────────────────
_EMBEDDED_SECRET = "__ALADIN_DRM_KEY_PLACEHOLDER__"
_EMBEDDED_LICENSE = None  # optional: a pre-validated license baked into the exe

_secret_ok = False


def _setup_engine_logging():
    """Replicate main.py's logging setup (file -> bot.log) for the frozen
    in-process engine, which never runs main.py."""
    import logging as _logging
    from logging.handlers import RotatingFileHandler as _RFH
    root = _logging.getLogger()
    if any(isinstance(h, _RFH) for h in root.handlers):
        return
    root.setLevel(_logging.INFO)
    _fmt = _logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        "%Y-%m-%d %H:%M:%S")
    try:
        fh = _RFH(os.path.join(DATA_DIR, "bot.log"),
                  maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(_fmt)
        root.addHandler(fh)
    except Exception:
        pass


def _ensure_secret():
    """Prefer the .env secret (live editing); otherwise use the embedded one."""
    global _secret_ok
    s = (os.getenv("ALADIN_DRM_KEY", "").replace("'", "").replace('"', "").strip()
         or _EMBEDDED_SECRET.strip())
    if s and s != "__ALADIN_DRM_KEY_PLACEHOLDER__":
        _secret_ok = True
    return s


def _inject_embedded_license():
    """If the .env has no LICENSE_KEY but an embedded one exists, seed it."""
    if _EMBEDDED_LICENSE and not os.getenv("LICENSE_KEY", "").strip():
        os.environ["LICENSE_KEY"] = _EMBEDDED_LICENSE


def _env_report():
    """Summarise what license-related values the app sees (for diagnostics)."""
    lines = [
        "ALADIN license env report",
        f"  BASE_DIR:      {BASE_DIR}",
        f"  .env exists:   {os.path.exists(ENV_FILE)}",
        f"  .env path:     {ENV_FILE}",
        f"  ALADIN_DRM_KEY from env: {'yes' if os.getenv('ALADIN_DRM_KEY') else 'no'}",
        f"  embedded secret active:  {_secret_ok and not os.getenv('ALADIN_DRM_KEY')}",
        f"  LICENSE_KEY from env:    {'yes' if os.getenv('LICENSE_KEY') else 'no'}",
        f"  embedded license active: {bool(_EMBEDDED_LICENSE and not os.getenv('LICENSE_KEY'))}",
        f"  MT5 credentials present: {'yes' if (os.getenv('MT5_LOGIN','').strip() and os.getenv('MT5_PASSWORD','').strip() and os.getenv('MT5_SERVER','').strip()) else 'no'}",
    ]
    try:
        with open(PANEL_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return lines

# BASE_DIR: when frozen into ALADIN.exe, live next to the exe (config, logs,
# assets, pidfile all stay in the exe's folder). From source: this file's dir.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "4.2"
DATA_DIR = os.path.join(BASE_DIR, "data")          # config + logs + lock files
DB_DIR = os.path.join(BASE_DIR, "db")            # database files (trading_bot.db)
ENV_FILE = os.path.join(DATA_DIR, ".env")
PANEL_LOG = os.path.join(DATA_DIR, "panel_assets.log")
PANEL_LAUNCH_LOG = os.path.join(DATA_DIR, "panel_launch.log")
BOT_LOG = os.path.join(DATA_DIR, "bot.log")
SIGNALS_LOG = os.path.join(DATA_DIR, "signals_log.csv")
MAIN_PY = os.path.join(BASE_DIR, "main.py")
WATCHDOG_PY = os.path.join(BASE_DIR, "bot_watchdog.py")
PIDFILE = os.path.join(DATA_DIR, ".bot.pid")
LOCKFILE = os.path.join(DATA_DIR, ".panel.lock")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")      # static images (read-only)
BG_MAIN = os.path.join(ASSETS_DIR, "bg_main.png")
LOGO_HEADER = os.path.join(ASSETS_DIR, "logo_header.png")   # lockup: emblem + wordmark
LOGO_EMBLEM = os.path.join(ASSETS_DIR, "logo_emblem.png")   # square emblem (taskbar)
LOGO_PATH = os.path.join(ASSETS_DIR, "logo.ico")

# data folder must exist before .env/logs/lock are touched
_ensure_data_dir()
os.environ.setdefault("BOT_DB_PATH", os.path.join(DB_DIR, "trading_bot.db"))
os.environ.setdefault("ALADIN_DATA_DIR", DATA_DIR)

_ensure_env()                     # materialize embedded .env first (frozen exe)
load_dotenv(ENV_FILE, override=True)
# BOT_DB_PATH may be relative in .env ("db/trading_bot.db") — make it absolute
# against BASE_DIR so the DB resolves correctly regardless of the launch cwd
# (shortcuts, Explorer double-click, different working directories). A bare
# filename (stale "trading_bot.db") is also normalized into db/ so it can
# never land in the project root.
_bp = os.environ.get("BOT_DB_PATH", "").strip()
if _bp:
    _bp_name = os.path.basename(_bp.replace("\\", "/"))
    _bp_dir = os.path.dirname(_bp.replace("\\", "/"))
    if not _bp_dir:
        _bp = os.path.join("db", _bp_name)
    if not os.path.isabs(_bp):
        _bp = os.path.join(BASE_DIR, _bp)
    os.environ["BOT_DB_PATH"] = os.path.normpath(_bp)
_DRM_SECRET = _ensure_secret()

# ─────────────────────────────────────────────────────────────────────────────
# THEME TOKENS (Qt style sheets)
# ─────────────────────────────────────────────────────────────────────────────
DARK_QSS = """
QMainWindow, QWidget#root { background: #0a0d12; color: #e9eef4; }
QTabWidget::pane { border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; background: rgba(13,17,23,0.70); }
QTabBar::tab { background: #0f141b; color: #93a1b0; padding: 9px 20px;
               border: 1px solid transparent; border-top-left-radius: 6px;
               border-top-right-radius: 6px; font-weight: 600; }
QTabBar::tab:selected { background: #1d2633; color: #35c7a6; border-color: #26313f; }
QFrame#card { background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.16);
              border-top: 1px solid rgba(255,255,255,0.30); border-radius: 12px; }
QFrame#panel { background: rgba(13,17,23,0.80); border: 1px solid rgba(255,255,255,0.10); border-radius: 10px; }
QFrame#tile { background: rgba(13,17,23,0.80); border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; }
QLabel { color: #e9eef4; }
QLabel#muted { color: #93a1b0; }
QLabel#accent { color: #35c7a6; }
QLineEdit { background: #0d1218; color: #e9eef4; border: 1px solid #26313f;
            border-radius: 6px; padding: 6px 8px; font-family: Consolas; }
QLineEdit:focus { border-color: #35c7a6; }
QPlainTextEdit#log { background: #0a0d12; color: #35c7a6; border: 1px solid #26313f;
                     border-radius: 8px; font-family: Consolas; font-size: 9pt; }
QPushButton { background: #1d2633; color: #e9eef4; border: 1px solid #26313f;
              border-radius: 8px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #26313f; }
QPushButton:pressed { background: #0f141b; }
QPushButton:disabled { color: #5a6673; background: #151c26; }
QPushButton#primary { background: #35c7a6; color: #03251d; border: none; }
QPushButton#primary:hover { background: #2aa387; }
QPushButton#start { background: #1f9d5c; color: #ffffff; border: none; }
QPushButton#start:hover { background: #2fd07a; }
QPushButton#stop { background: #cc3f3f; color: #ffffff; border: none; }
QPushButton#stop:hover { background: #ff5c5c; }
QPushButton#save { background: #2f7fd9; color: #ffffff; border: none; }
QPushButton#save:hover { background: #4aa3ff; }
QPushButton#file { background: #d98f2f; color: #ffffff; border: none; }
QPushButton#file:hover { background: #ffb454; }
QTableWidget { background: #151c26; color: #e9eef4; gridline-color: #26313f;
               border: 1px solid #26313f; border-radius: 8px; }
QHeaderView::section { background: #1d2633; color: #35c7a6; border: none;
                       padding: 6px; font-weight: 600; }
QScrollBar:vertical { background: #0a0d12; width: 10px; }
QScrollBar::handle:vertical { background: #26313f; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QDialog, QMessageBox { background: #0f141b; }
QMessageBox QLabel, QDialog QLabel { color: #e9eef4; background: transparent; }
QMessageBox QPushButton, QDialog QPushButton { background: #1d2633; color: #e9eef4;
    border: 1px solid #26313f; border-radius: 6px; padding: 6px 18px; min-width: 72px; }
QMessageBox QPushButton:hover, QDialog QPushButton:hover { background: #26313f; }
QMessageBox QPushButton:pressed, QDialog QPushButton:pressed { background: #0f141b; }
"""


# ─────────────────────────────────────────────────────────────────────────────
# ROBUSTNESS — single instance + stale lock recovery
# ─────────────────────────────────────────────────────────────────────────────
def _win_hide():
    """Return (creationflags, startupinfo) that hide the console for a child
    process on Windows. Prevents console 'flash' when a windowed app spawns a
    console program (python, tasklist, etc.)."""
    if sys.platform != "win32":
        return 0, None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    si = None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
    except Exception:
        si = None
    return flags, si


def _bot_python():
    """Python launcher for the bot engine subprocess.
    From source: the current interpreter. Frozen (exe): a python found on PATH,
    because the engine stays as .py files next to ALADIN.exe."""
    if not getattr(sys, "frozen", False):
        return [sys.executable]
    for cand in ("python", "python3"):
        p = shutil.which(cand)
        if p:
            return [p]
    p = shutil.which("py")
    if p:
        return [p, "-3"]
    return [sys.executable]  # last resort (needs python on PATH to run the bot)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        # NO subprocess here: spawning tasklist.exe from a windowed app would
        # flash a console window every status refresh. Check the process
        # directly via the Windows API instead (no console, no pipe).
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True  # can't verify -> assume alive (safe side)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_single_instance(path):
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                owner = open(path).read().strip()
            except Exception:
                owner = ""
            if owner and not _pid_alive(owner):
                try:
                    os.remove(path)
                    continue
                except OSError:
                    return False
            return False
    return False


def _release_single_instance(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDED ASSETS — the panel self-materializes its images on first launch,
# so it works out of the box with just app.py + assets_b64.py (no assets/ copy).
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_assets():
    """Write embedded images to disk if missing (never overwrites existing)."""
    written = []
    try:
        import base64
        import assets_b64
    except Exception:
        return written
    try:
        os.makedirs(ASSETS_DIR, exist_ok=True)
    except OSError:
        pass
    for name, data in (("bg_main.png", assets_b64.BG_MAIN_PNG),
                       ("logo_header.png", assets_b64.LOGO_HEADER_PNG),
                       ("logo_emblem.png", assets_b64.LOGO_EMBLEM_PNG),
                       ("logo.ico", assets_b64.LOGO_ICO)):
        target = os.path.join(ASSETS_DIR, name)   # all static assets incl. the ico
        try:
            if not os.path.exists(target) or os.path.getsize(target) == 0:
                with open(target, "wb") as f:
                    f.write(base64.b64decode(data))
                written.append(target)
        except Exception:
            pass
    return written


# ─────────────────────────────────────────────────────────────────────────────
# LICENSE — Fernet-validated (pure, reusable)
# ─────────────────────────────────────────────────────────────────────────────
def _crypto_key(secret):
    return base64.urlsafe_b64encode(secret.encode("utf-8").ljust(32)[:32])


_net_time_cache = {"t": 0.0, "val": None}

def _network_utc_now():
    """UTC now with a short TTL cache so repeated license checks never block
    the UI on a slow/dead network (cached for 5 minutes; falls back to local
    UTC if the network is unavailable)."""
    import time as _t
    now = _t.time()
    if _net_time_cache["val"] is not None and now - _net_time_cache["t"] < 300:
        return _net_time_cache["val"]
    try:
        # FIX: HTTPS, not HTTP. Over plain HTTP a man-in-the-middle can serve a
        # fake clock (e.g. year 2000) and make any expired license validate.
        with urllib.request.urlopen("https://worldtimeapi.org/api/timezone/Etc/UTC", timeout=3) as r:
            data = json.loads(r.read().decode())
            val = datetime.fromisoformat(data["datetime"].replace("Z", "+00:00")).replace(tzinfo=None)
            _net_time_cache["t"] = now
            _net_time_cache["val"] = val
            return val
    except Exception:
        # FIX: cache the LOCAL-UTC fallback too. Previously a failed fetch was
        # never cached, so every license check on a dead/blocked network blocked
        # the UI on a fresh 3s attempt each time. Cache the fallback for 5 min
        # so offline validation is instant after the first miss.
        _net_time_cache["t"] = now
        _net_time_cache["val"] = datetime.now(timezone.utc).replace(tzinfo=None)
        return _net_time_cache["val"]


def validate_license_key(key, secret, now=None):
    try:
        from cryptography.fernet import Fernet
        f = Fernet(_crypto_key(secret))
        decrypted = f.decrypt(key.strip().encode("utf-8")).decode("utf-8")
    except Exception:
        return False, "Invalid license key.", None
    parts = decrypted.split("|")
    if len(parts) < 2 or parts[0] != "ALADIN_PRO":
        return False, "Invalid license format.", None
    try:
        expiry = datetime.strptime(parts[1], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False, "Corrupted license payload.", None
    now = now or _network_utc_now()
    if now > expiry:
        return False, f"License expired {expiry.strftime('%Y-%m-%d %H:%M')} UTC.", expiry
    return True, f"valid until {expiry.strftime('%Y-%m-%d %H:%M')} UTC.", expiry


# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS STATS (reused parser)
# ─────────────────────────────────────────────────────────────────────────────
class SignalStats:
    def __init__(self, path=SIGNALS_LOG):
        self.path = path

    def load(self):
        out = {"total": 0, "closed": 0, "pending": 0, "wins": 0, "losses": 0,
               "win_rate": 0.0, "avg_r": 0.0, "profit_factor": 0.0, "today_pnl": 0.0,
               "today_closed": 0, "per_symbol": {}, "last": [], "equity": [], "ok": False}
        if not os.path.exists(self.path):
            return out
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            return out
        today = date.today().isoformat()
        closed, pending, all_r, equity, cum = [], 0, [], [], 0.0
        for r in rows:
            status = (r.get("status") or "").strip().upper()
            if status == "CLOSED":
                closed.append(r)
                try:
                    R = float(r.get("R") or 0.0)
                    all_r.append(R)
                    cum += float(r.get("pnl_usd") or 0.0)
                    equity.append(cum)
                except (TypeError, ValueError):
                    pass
            elif status in ("PENDING", "OPEN"):
                pending += 1
        out["total"], out["closed"], out["pending"] = len(rows), len(closed), pending
        if all_r:
            wins = [x for x in all_r if x > 0]
            losses = [x for x in all_r if x <= 0]
            out["wins"], out["losses"] = len(wins), len(losses)
            out["win_rate"] = len(wins) / len(all_r)
            out["avg_r"] = sum(all_r) / len(all_r)
            out["profit_factor"] = (sum(wins) / abs(sum(losses))) if sum(losses) else (999.0 if wins else 0.0)
        out["equity"] = equity
        for r in closed:
            if (r.get("close_ts_utc") or "")[:10] == today:
                try:
                    out["today_pnl"] += float(r.get("pnl_usd") or 0.0)
                    out["today_closed"] += 1
                except (TypeError, ValueError):
                    pass
        per = {}
        for r in closed:
            sym = (r.get("symbol") or "?").upper()
            d = per.setdefault(sym, {"n": 0, "wins": 0, "r_sum": 0.0, "win_sum": 0.0, "loss_sum": 0.0})
            d["n"] += 1
            try:
                R = float(r.get("R") or 0.0)
                d["r_sum"] += R
                if R > 0:
                    d["wins"] += 1
                    d["win_sum"] += R
                else:
                    d["loss_sum"] += R
            except (TypeError, ValueError):
                pass
        out["per_symbol"] = per
        out["last"] = closed[-5:][::-1]
        out["ok"] = True
        return out


# ─────────────────────────────────────────────────────────────────────────────
# QUIET HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _set_window_icon(app, win):
    # Windows taskbar: without a unique AppUserModelID the taskbar shows the
    # Python icon instead of ours — set it BEFORE the window is shown.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ALADIN.ControlPanel.1")
        except Exception:
            pass
    # prefer the square emblem (no text) for the taskbar; lockup/legacy as backup
    for p in (LOGO_EMBLEM, LOGO_HEADER, LOGO_PATH):
        if os.path.exists(p):
            try:
                icon = QIcon(p)
                app.setWindowIcon(icon)
                win.setWindowIcon(icon)
                break
            except Exception:
                continue


def _load_env(updates):
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    written, out = set(), []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            out.append(line)
            continue
        if "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}='{updates[k]}'\n")
                written.add(k)
            else:
                out.append(line)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in written:
            out.append(f"{k}='{v}'\n")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(out)
    load_dotenv(ENV_FILE, override=True)


# ─────────────────────────────────────────────────────────────────────────────
# EQUITY CURVE WIDGET (custom paint — real chart, not canvas hacks)
# ─────────────────────────────────────────────────────────────────────────────
class EquityCurve(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setMinimumHeight(120)

    def set_data(self, equity):
        self.points = list(equity)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if not self.points or len(self.points) < 2:
            p.setPen(QColor("#93a1b0"))
            p.drawText(self.rect(), Qt.AlignCenter, "No closed trades yet")
            return
        mn, mx = min(self.points), max(self.points)
        span = (mx - mn) or 1.0
        n = len(self.points)
        pts = []
        for i, v in enumerate(self.points):
            x = 6 + i * (w - 12) / max(n - 1, 1)
            y = h - 8 - (v - mn) / span * (h - 20)
            pts.append((x, y))
        # gradient fill under curve
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QColor(53, 199, 166, 90))
        grad.setColorAt(1, QColor(53, 199, 166, 10))
        poly = QPainterPath = __import__("PySide6.QtGui", fromlist=["QPainterPath"]).QPainterPath()
        poly.moveTo(6, h - 4)
        for x, y in pts:
            poly.lineTo(x, y)
        poly.lineTo(w - 6, h - 4)
        poly.closeSubpath()
        p.fillPath(poly, QBrush(grad))
        # line
        pen = p.pen()
        pen.setColor(QColor("#35c7a6"))
        pen.setWidth(2)
        p.setPen(pen)
        for i in range(1, len(pts)):
            p.drawLine(int(pts[i - 1][0]), int(pts[i - 1][1]), int(pts[i][0]), int(pts[i][1]))
        # labels
        p.setPen(QColor("#93a1b0"))
        p.drawText(10, 16, f"peak ${mx:,.2f}")
        p.drawText(10, h - 8, f"low ${mn:,.2f}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────
class AladinWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        _ensure_assets()  # materialize embedded images if the assets/ copy is missing
        self.setWindowTitle(f"ALADIN · Quantitative Execution Node  v{APP_VERSION}")
        self.resize(1180, 860)
        self.setMinimumSize(980, 660)

        # state
        self.license_valid = False
        self.license_msg = "License not validated"
        self._stderr_sz = -1   # last observed bot_stderr.log size (crash-loop hint)
        # In the frozen exe the engine runs INSIDE this process (no .py files
        # are ever written to disk — the source stays bytecode in the exe).
        # Source mode keeps the watchdog subprocess. ALADIN_INPROCESS env is
        # for testing.
        self._inprocess = bool(getattr(sys, "frozen", False)
                               or os.environ.get("ALADIN_INPROCESS"))
        self._engine_obj = None
        self._engine_running = False
        self.vars = {}
        self.bot_proc = None
        self._pending_start = False
        self._stopping = False
        self._launch_check_timer = None
        self._launch_check_ticks = 0
        self._log_queue = queue.Queue()
        self._stop_tail = False
        self._stats_built = False
        self._config_edits = []     # all QLineEdit widgets on config tabs
        self._config_btns = []      # action buttons on config tabs

        self._build()
        self._apply_theme()
        _inject_embedded_license()
        self._load_env_into_vars()
        self._try_auto_validate_license()
        # startup diagnostics -> panel_assets.log (asset report + license report)
        try:
            rep = self._asset_report()
            _env_report()
            missing = [l.split()[0] for l in rep if " MISSING" in l]
            self.status.setText(
                f"panel v{APP_VERSION} — assets ok" if not missing
                else f"panel v{APP_VERSION} — missing: {', '.join(missing)} (see panel_assets.log)")
        except Exception:
            pass

        # timers
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._drain_log)
        self._log_timer.start(1200)
        self._tail_thread = threading.Thread(target=self._tail_log, daemon=True)
        self._tail_thread.start()
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(5000)
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(10000)
        self._refresh_status()   # paint correct button/chip state on launch

    # ── build UI ────────────────────────────────────────────────
    def _build(self):
        # root: photo background label behind a central scroll area
        self.bg_label = QLabel(self)
        self.bg_label.setGeometry(0, 0, self.width(), self.height())
        self.bg_label.setAlignment(Qt.AlignCenter)
        self.bg_label.lower()

        # dim overlay keeps text readable while letting the nature photo glow through
        self.dim = QLabel(self)
        self.dim.setStyleSheet("background: rgba(0,0,0,0.15);")
        self.dim.setGeometry(0, 0, self.width(), self.height())

        # central card frame (glass)
        self.card = QFrame(self)
        self.card.setObjectName("card")
        self.card.setGeometry(18, 18, self.width() - 36, self.height() - 36)

        outer = QVBoxLayout(self.card)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        # header
        hdr = QHBoxLayout()
        self.logo = QLabel()
        self.logo.setFixedSize(66, 66)   # square emblem (no wordmark)
        hdr.addWidget(self.logo, 0, Qt.AlignVCenter)
        title_box = QVBoxLayout()
        s = QLabel("Quantitative Execution Node  ·  Control Panel")
        s.setObjectName("muted")
        title_box.addWidget(s, 0, Qt.AlignVCenter)
        hdr.addLayout(title_box)
        hdr.addStretch()
        self.lic_chip = QLabel("LICENSE REQUIRED")
        self.lic_chip.setStyleSheet("border:1px solid #ffb454; border-radius:6px; padding:4px 10px; font-weight:700; background: rgba(10,13,18,0.55); color:#ffb454;")
        self.st_chip = QLabel("BOT STOPPED")
        self.st_chip.setStyleSheet("border:1px solid #ff5c5c; border-radius:6px; padding:4px 10px; font-weight:700; background: rgba(10,13,18,0.55); color:#ff5c5c;")
        hdr.addWidget(self.lic_chip, 0, Qt.AlignVCenter)
        hdr.addSpacing(8)
        hdr.addWidget(self.st_chip, 0, Qt.AlignVCenter)
        outer.addLayout(hdr)

        # tabs
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._tab_license()
        self._tab_broker()
        self._tab_strategy()
        self._tab_telegram()
        self._stats_placeholder = QWidget()
        self.tabs.addTab(self._stats_placeholder, "Dashboard")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # log
        log_lbl = QLabel("LIVE SYSTEM LOGS")
        log_lbl.setObjectName("muted")
        outer.addWidget(log_lbl)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        outer.addWidget(self.log, 2)

        # footer buttons
        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.start_btn = QPushButton("START BOT")
        self.start_btn.setObjectName("start")
        self.start_btn.clicked.connect(self.start_bot)
        self.stop_btn = QPushButton("STOPPED")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_bot)
        self.save_btn = QPushButton("SAVE CONFIG")
        self.save_btn.setObjectName("save")
        self.save_btn.clicked.connect(self.save_env)
        self.signals_btn = QPushButton("SIGNAL LOG")
        self.signals_btn.setObjectName("file")
        self.signals_btn.clicked.connect(self.open_signals)
        foot.addWidget(self.start_btn)
        foot.addWidget(self.stop_btn)
        foot.addWidget(self.save_btn)
        foot.addWidget(self.signals_btn)
        foot.addStretch()
        outer.addLayout(foot)

        # status bar
        self.status = QLabel("ALADIN Control Panel · bot runs as a separate process")
        self.status.setObjectName("muted")
        self.status.setStyleSheet("background: rgba(10,13,18,0.45); border-radius: 4px; padding: 2px 8px;")
        outer.addWidget(self.status)

        # registry of every editable field + config action button (populated
        # AFTER the tabs are built) so we can lock them while the bot runs
        self._config_edits = list(self.vars.values())
        if getattr(self, "license_edit", None) is not None:
            self._config_edits.append(self.license_edit)
        self._config_btns = [b for b in (getattr(self, "save_btn", None),)
                             if b is not None]

        self._load_logo()
        self._apply_background()

    def _paint_emblem(self, size=116):
        """Fallback emblem when PNGs are missing: globe + candles, silver on navy."""
        from PySide6.QtGui import QPixmap, QPainter, QPainterPath, QColor, QPen, QBrush
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        s = size
        path = QPainterPath()
        path.addRoundedRect(0, 0, s, s, s * 0.16, s * 0.16)
        p.fillPath(path, QBrush(QColor(11, 17, 26)))
        p.setPen(QPen(QColor(70, 92, 120), max(1, s * 0.025)))
        p.drawPath(path)
        # globe (circle + meridian)
        silver = QColor(226, 231, 238)
        cx, cy, r = s * 0.5, s * 0.58, s * 0.26
        p.setPen(QPen(silver, max(2, s * 0.02)))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(int(cx - r), int(cy - r), int(2 * r), int(2 * r))
        p.drawEllipse(int(cx - r * 0.45), int(cy - r), int(r * 0.9), int(2 * r))  # meridian
        p.drawLine(int(cx), int(cy - r), int(cx), int(cy + r))
        # 3 candles rising from globe
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(silver))
        for bx, bh in ((0.40, 0.14), (0.52, 0.20), (0.64, 0.10)):
            x = bx * s
            p.drawRoundedRect(int(x - s * 0.035), int(cy - r - bh * s), int(s * 0.07), int(bh * s),
                              int(s * 0.012), int(s * 0.012))
            p.drawRect(int(x - s * 0.008), int(cy - r - bh * s - s * 0.03), int(s * 0.016), int(s * 0.03))
        p.end()
        return pm

    def _load_logo(self):
        try:
            from PySide6.QtGui import QPixmap
            # square emblem (globe + candles); painted fallback if missing
            pm = QPixmap(LOGO_HEADER) if os.path.exists(LOGO_HEADER) else QPixmap()
            if pm.isNull():
                pm = self._paint_emblem(124)
            # hi-res internal, then display size — crisp on hi-dpi
            big = pm.scaled(124, 124, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._logo_pm = big.scaled(62, 62, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.logo.setPixmap(self._logo_pm)
        except Exception:
            try:
                self._logo_pm = self._paint_emblem(124).scaled(
                    62, 62, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.logo.setPixmap(self._logo_pm)
            except Exception:
                self.logo.setText("A")

    def _apply_background(self):
        # professional trading backdrop; solid navy last resort
        p = BG_MAIN if os.path.exists(BG_MAIN) else None
        if not p:
            self.bg_label.setStyleSheet("background:#0a0d12;")
            return
        try:
            from PySide6.QtGui import QPixmap
            pm = QPixmap(p)
            if pm.isNull():
                return
            # scaled to window, dimmed via a dark overlay label
            scaled = pm.scaled(self.width(), self.height(),
                               Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = (scaled.width() - self.width()) // 2
            y = (scaled.height() - self.height()) // 2
            scaled = scaled.copy(x, y, self.width(), self.height())
            self.bg_label.setPixmap(scaled)
            self.bg_label.lower()
        except Exception:
            pass

    def _asset_report(self):
        """Write a startup report of what assets were found/loaded to panel_assets.log.
        Lets us see exactly what the app resolves on a given machine."""
        lines = []
        lines.append(f"ALADIN panel asset report  (app v{APP_VERSION})")
        lines.append(f"platform: {sys.platform} | python: {sys.version.split()[0]} | "
                     f"PySide6: {getattr(__import__('PySide6', fromlist=['__version__']), '__version__', '?')}")
        lines.append(f"BASE_DIR: {BASE_DIR}")
        lines.append(f"cwd:      {os.getcwd()}")
        lines.append("")
        try:
            from PySide6.QtGui import QPixmap
        except Exception:
            QPixmap = None
        lines.append(f"materialized on this launch: {len(_ensure_assets())} file(s)")
        lines.append("")
        for label, path in (("bg_main     ", BG_MAIN),
                            ("logo_header ", LOGO_HEADER),
                            ("logo_emblem ", LOGO_EMBLEM),
                            ("logo_ico    ", LOGO_PATH)):
            if not os.path.exists(path):
                lines.append(f"  {label} MISSING   {path}")
                continue
            sz = os.path.getsize(path)
            if QPixmap is not None:
                pm = QPixmap(path)
                ok = "OK" if not pm.isNull() else "LOAD-FAILED"
                lines.append(f"  {label} {ok}  {sz:>9,}B  {pm.width()}x{pm.height()}  {path}")
            else:
                lines.append(f"  {label} present {sz:>9,}B  (QPixmap unavailable)  {path}")
        lines.append("")
        bg_used = BG_MAIN if os.path.exists(BG_MAIN) else "SOLID-FALLBACK"
        lines.append(f"background selected: {os.path.basename(bg_used) if os.path.exists(bg_used) else bg_used}")
        lines.append(f"header icon:         {os.path.basename(LOGO_HEADER) if os.path.exists(LOGO_HEADER) else 'PAINTED-FALLBACK'}")
        try:
            with open(PANEL_LOG, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass
        return lines

    # ── tabs ───────────────────────────────────────────────────
    def _entry_row(self, parent, row, label, key, show=None):
        lay = QGridLayout()
        lab = QLabel(label)
        lab.setObjectName("muted")
        ed = QLineEdit()
        ed.setEchoMode(QLineEdit.Password if show == "*" else QLineEdit.Normal)
        self.vars[key] = ed
        lay.addWidget(lab, row, 0)
        lay.addWidget(ed, row, 1)
        parent.addLayout(lay)

    def _tab_license(self):
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        lab = QLabel("License Key")
        lab.setObjectName("muted")
        self.license_edit = QLineEdit()
        self.license_edit.setEchoMode(QLineEdit.Password)
        btn = QPushButton("Validate License")
        btn.clicked.connect(self.validate_license)
        h.addWidget(lab)
        h.addWidget(self.license_edit, 1)
        h.addWidget(btn)
        v.addLayout(h)
        self.license_status = QLabel(self.license_msg)
        self.license_status.setStyleSheet("color:#ffb454; font-weight:600;")
        v.addWidget(self.license_status)
        v.addStretch()
        self.tabs.addTab(w, "License")

    def _tab_broker(self):
        w = QWidget()
        v = QVBoxLayout(w)
        for i, (label, key, show) in enumerate([
                ("License Key", "LICENSE_KEY", "*"), ("MT5 Login", "MT5_LOGIN", None),
                ("MT5 Password", "MT5_PASSWORD", "*"), ("Server Name", "MT5_SERVER", None),
                ("MT5 Terminal Path", "MT5_PATH", None), ("Exchange ID", "EXCHANGE_ID", None)]):
            self._entry_row(v, i, label, key, show=show)
        v.addStretch()
        self.tabs.addTab(w, "Broker")

    def _tab_strategy(self):
        w = QWidget()
        v = QVBoxLayout(w)
        for i, (label, key) in enumerate([
                ("Market Type", "EXCHANGE_MARKET_TYPE"), ("Day Symbols", "DAY_SYMBOLS"),
                ("Night Symbols", "NIGHT_SYMBOLS"), ("Forex Lot Multiplier", "FOREX_LOT_MULTIPLIER"),
                ("Crypto Lot Multiplier", "CRYPTO_LOT_MULTIPLIER"), ("Min Confidence", "MIN_CONFIDENCE"),
                ("Max Open Positions", "MAX_OPEN_POSITIONS"), ("Daily Profit Target", "DAILY_PROFIT_TARGET"),
                ("Daily Loss Limit", "MAX_DAILY_LIMIT"), ("Max Drawdown %", "MAX_DRAWDOWN_PCT"),
                ("Cooldown Hours", "COOLDOWN_HOURS"), ("Min RR", "MIN_RR")]):
            self._entry_row(v, i, label, key)
        v.addStretch()
        self.tabs.addTab(w, "Strategy / Assets")

    def _tab_telegram(self):
        w = QWidget()
        v = QVBoxLayout(w)
        for i, (label, key) in enumerate([("Telegram Bot Token", "TELEGRAM_BOT_TOKEN"),
                                          ("Telegram Chat ID", "TELEGRAM_CHAT_ID"),
                                          ("Notify Debug", "NOTIFY_DEBUG")]):
            self._entry_row(v, i, label, key)
        v.addStretch()
        self.tabs.addTab(w, "Telegram")

    def _on_tab_changed(self, idx):
        if self._stats_built:
            return
        if self.tabs.tabText(idx) == "Dashboard":
            self._build_stats_tab()
            self._stats_built = True
            self._refresh_stats()
            # The lazy rebuild REMOVES the placeholder tab and appends the real
            # Dashboard at the end, which shifts every tab index. Qt then snaps
            # the selection to whatever now sits at the OLD index — if the
            # placeholder was NOT last, that is a different tab (e.g. Telegram).
            # Reselect the Dashboard tab by name (signals blocked so we don't
            # re-enter this handler) so the click always lands on Dashboard,
            # regardless of where the placeholder originally sat.
            try:
                self.tabs.blockSignals(True)
                for i in range(self.tabs.count()):
                    if self.tabs.tabText(i) == "Dashboard":
                        self.tabs.setCurrentIndex(i)
                        break
            finally:
                self.tabs.blockSignals(False)

    def _build_stats_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        # stat tiles row
        tiles = QHBoxLayout()
        self.tiles = {}
        _tips = {
            "win_rate": "Winning closed trades ÷ total closed trades.",
            "avg_r": "Average R-multiple per closed trade (profit ÷ initial risk).",
            "profit_factor": "Gross profit ÷ gross loss. ∞ = no losing trades yet.",
            "today_pnl": "Realised profit/loss on trades closed today (UTC).",
            "closed": "Total closed trades on record.",
            "pending": "Live pending limit orders currently resting at the broker.",
        }
        for key, label in [("win_rate", "Win Rate"), ("avg_r", "Average R-Multiple"),
                           ("profit_factor", "Profit Factor"), ("today_pnl", "Today P&L"),
                           ("closed", "Closed"), ("pending", "Pending Orders")]:
            tile = QFrame()
            tile.setObjectName("tile")
            tl = QVBoxLayout(tile)
            lab = QLabel(label.upper())
            lab.setObjectName("muted")
            val = QLabel("--")
            val.setStyleSheet("font-size:18px; font-weight:800; color:#35c7a6;")
            val.setAlignment(Qt.AlignCenter)
            val.setToolTip(_tips.get(key, ""))
            tl.addWidget(lab)
            tl.addWidget(val)
            tiles.addWidget(tile, 1)
            self.tiles[key] = val
        v.addLayout(tiles)
        # equity + per-symbol
        mid = QHBoxLayout()
        eq_frame = QFrame()
        eq_frame.setObjectName("panel")
        ev = QVBoxLayout(eq_frame)
        ev.addWidget(QLabel("EQUITY CURVE"))
        self.equity = EquityCurve()
        ev.addWidget(self.equity, 1)
        mid.addWidget(eq_frame, 3)
        per_frame = QFrame()
        per_frame.setObjectName("panel")
        pv = QVBoxLayout(per_frame)
        pv.addWidget(QLabel("PERFORMANCE BY SYMBOL"))
        self.sym_table = QTableWidget(0, 5)
        self.sym_table.setHorizontalHeaderLabels(["Symbol", "Trades", "Win Rate", "Avg R-Multiple", "Profit Factor"])
        self.sym_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sym_table.verticalHeader().setVisible(False)
        # read-only report: cells can't be edited, dragged, or focused
        self.sym_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.sym_table.setSelectionMode(QTableWidget.NoSelection)
        self.sym_table.setFocusPolicy(Qt.NoFocus)
        pv.addWidget(self.sym_table, 1)
        mid.addWidget(per_frame, 2)
        v.addLayout(mid, 1)
        self.tabs.removeTab(self.tabs.indexOf(self._stats_placeholder))
        self.tabs.addTab(w, "Dashboard")

    # ── theme ──────────────────────────────────────────────────
    def _apply_theme(self):
        # single dark theme: dark photo + dim overlay + glass card
        self.setStyleSheet(DARK_QSS)
        # also apply at the app level so dialogs (QMessageBox etc.) are
        # dark and readable — otherwise they render native-white with
        # light text (invisible).
        try:
            qapp = QApplication.instance()
            if qapp is not None:
                qapp.setStyleSheet(DARK_QSS)
        except Exception:
            pass
        try:
            st = self.style()
            st.unpolish(self)
            st.polish(self)
            for w in self.findChildren(QWidget):
                st.unpolish(w)
                st.polish(w)
            self.update()
        except Exception:
            pass
        try:
            self.dim.setStyleSheet("background: rgba(0,0,0,0.15);")
            self.dim.lower()
            self.card.raise_()
        except Exception:
            pass
        lic_color = "#2fd07a" if self.license_valid else "#ffb454"
        st_color = "#2fd07a" if self._is_bot_running() else "#ff5c5c"
        self.lic_chip.setStyleSheet(f"border:1px solid {lic_color}; border-radius:6px; padding:4px 10px; font-weight:700; background: rgba(10,13,18,0.55); color:{lic_color};")
        self.st_chip.setStyleSheet(f"border:1px solid {st_color}; border-radius:6px; padding:4px 10px; font-weight:700; background: rgba(10,13,18,0.55); color:{st_color};")
        self.license_status.setStyleSheet(f"color:{lic_color}; font-weight:600;")
        self._apply_background()

    # ── env ────────────────────────────────────────────────────
    def _load_env_into_vars(self):
        for key, ed in self.vars.items():
            ed.setText(os.getenv(key, ""))
        self.license_edit.setText(os.getenv("LICENSE_KEY", ""))

    def save_env(self):
        updates = {k: ed.text().strip() for k, ed in self.vars.items()}
        updates["LICENSE_KEY"] = self.license_edit.text().strip()
        try:
            _load_env(updates)
        except Exception as e:
            QMessageBox.critical(
                self, "Could not save config",
                f"The configuration could not be written to .env.\n\n"
                f"error: {e}\n\n"
                f"check that the app folder is writable: {ENV_FILE}")
            return
        self.status.setText("Configuration saved — restart the bot to apply changes.")

    # ── license ────────────────────────────────────────────────
    def _secret(self):
        # the DRM secret: embedded at build time; .env overrides when present
        return _DRM_SECRET

    def _try_auto_validate_license(self):
        key = os.getenv("LICENSE_KEY", "").strip()
        if key:
            self.license_edit.setText(key)
            self._apply_license(*validate_license_key(key, self._secret()))

    def validate_license(self):
        key = self.license_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "License", "Enter your license key first.")
            return
        ok, msg, expiry = validate_license_key(key, self._secret())
        if not ok and not _secret_ok:
            msg = (f"{msg}\n\n"
                   f"note: the DRM secret is missing — ALADIN_DRM_KEY was not found "
                   f"in .env and is not embedded in this build. see {PANEL_LOG}")
        self._apply_license(ok, msg, expiry)

    def _apply_license(self, ok, msg, expiry):
        self.license_valid = ok
        self.license_msg = msg
        self.license_status.setText(msg)
        color = "#2fd07a" if ok else "#ffb454"
        self.license_status.setStyleSheet(f"color:{color}; font-weight:600;")
        self.lic_chip.setText("LICENSE VALID" if ok else "LICENSE REQUIRED")
        self.lic_chip.setStyleSheet(f"border:1px solid {color}; border-radius:6px; padding:4px 10px; font-weight:700; background: rgba(10,13,18,0.55); color:{color};")
        if ok:
            try:
                updates = {k: ed.text().strip() for k, ed in self.vars.items()}
                updates["LICENSE_KEY"] = self.license_edit.text().strip()
                _load_env(updates)
            except Exception:
                pass
            self.status.setText("License valid. You can start the bot.")
        self._refresh_status()

    # ── bot lifecycle ──────────────────────────────────────────
    def _is_bot_running(self):
        if self._inprocess:
            return bool(getattr(self, "_engine_running", False))
        try:
            if not os.path.exists(PIDFILE):
                return False
            with open(PIDFILE) as f:
                lines = [l.strip() for l in f if l.strip()]
            if not lines:
                return False
            return _pid_alive(lines[-1])
        except Exception:
            return False

    def start_bot(self):
        # immediate visible feedback — the button always reacts
        self._pending_start = True
        self.start_btn.setText("STARTING...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setText("STOP BOT")
        self.stop_btn.setEnabled(False)
        self.status.setText("starting bot...")
        try:
            QApplication.processEvents()
        except Exception:
            pass
        key = self.license_edit.text().strip() or os.getenv("LICENSE_KEY", "").strip()
        if not self.license_valid:
            ok, msg, exp = validate_license_key(key, self._secret())
            if not ok:
                QMessageBox.critical(self, "License Required", msg)
                self._apply_license(False, msg, exp)
                try:
                    self.tabs.setCurrentIndex(0)  # License tab
                except Exception:
                    pass
                self.status.setText("license required — enter your key on the License tab")
                self._pending_start = False
                self._refresh_status()
                return
            self._apply_license(True, msg, exp)
        # Persist EVERY editable field (Broker + Strategy + Telegram tabs) to
        # .env automatically so whatever the user typed is what the bot runs
        # with — no separate SAVE CONFIG step required, and no stale values.
        m_updates = {k: ed.text().strip() for k, ed in self.vars.items()}
        m_updates["LICENSE_KEY"] = self.license_edit.text().strip()
        try:
            _load_env(m_updates)
        except Exception as e:
            QMessageBox.critical(
                self, "Could not save config",
                f"Your MT5 details could not be saved to .env.\n\n"
                f"error: {e}\n\n"
                f"check that the app folder is writable: {ENV_FILE}")
            return
        if not (m_updates.get("MT5_LOGIN") and m_updates.get("MT5_PASSWORD")
                and m_updates.get("MT5_SERVER")):
            QMessageBox.information(
                self, "MT5 account required",
                "Enter your own Exness/MT5 account details in the Broker tab "
                "(MT5 Login, MT5 Password, Server Name) before starting the bot.")
            try:
                self.tabs.setCurrentIndex(1)  # Broker tab
            except Exception:
                pass
            self.status.setText("mt5 account required — fill the Broker tab first")
            self._pending_start = False
            self._refresh_status()
            return
        if not self._inprocess and (not os.path.exists(MAIN_PY)
                                     or not os.path.exists(WATCHDOG_PY)):
            QMessageBox.critical(
                self, "Missing",
                "Bot engine files not found next to this app.\n\n"
                "The engine (main.py, bot_watchdog.py, ...) should be present "
                "next to this app. If it wasn't, copy the ALADIN project files "
                "into the same folder as this app and restart.")
            return
        if self._is_bot_running():
            self.status.setText("bot is already running.")
            return
        err = self._launch_bot()
        if err:
            QMessageBox.critical(self, "Launch Failed", err)
            self.status.setText("launch failed — see panel_launch.log")
        self._refresh_status()

    def _launch_bot(self):
        """Start the bot engine. Returns an error string or None.
        Frozen exe: engine runs in-process (no source files on disk).
        Source: watchdog subprocess, several python launchers, panel_launch.log."""
        if self._inprocess:
            return self._start_engine_inprocess()
        candidates = []
        for pre in (_bot_python(), ["py", "-3"], ["python"], ["python3"]):
            if pre not in candidates:
                candidates.append(pre)
        last_err = None
        for pre in candidates:
            cmd = pre + ["-u", WATCHDOG_PY]
            try:
                flags, si = _win_hide()
                self.bot_proc = subprocess.Popen(
                    cmd, cwd=BASE_DIR,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags, startupinfo=si)
                self._pending_start = True
                self.status.setText("starting bot...")
                self._write_launch_report("ok", cmd, None)
                self._schedule_launch_check()
                return None
            except Exception as e:
                last_err = e
        msg = (f"could not launch the bot process.\n\n"
               f"last command tried: {' '.join(cmd)}\n\n"
               f"error: {last_err}\n\n"
               f"full details: {PANEL_LAUNCH_LOG}")
        self._write_launch_report("failed", cmd, str(last_err))
        self._pending_start = False
        return msg

    def _start_engine_inprocess(self):
        """Frozen mode: import the bundled engine and run it in-process.
        No .py files are written to disk — the source stays bytecode inside
        the exe. Logging goes to bot.log next to the exe."""
        try:
            os.chdir(BASE_DIR)   # engine reads .env / writes bot.log here
            _setup_engine_logging()
            import orchestrator  # bundled by PyInstaller
            self._engine_obj = orchestrator.Orchestrator()
            self._engine_obj.start()   # spawns daemon threads, returns
            self._engine_running = True
            self._pending_start = False
            self.status.setText("bot started")
            return None
        except Exception as e:
            self._engine_running = False
            self._pending_start = False
            self._engine_obj = None   # drop the failed instance (no stale threads)
            return (f"the bot engine failed to start.\n\n"
                    f"error: {e}\n\n"
                    f"see {os.path.join(BASE_DIR, 'bot.log')} for details")

    def _write_launch_report(self, status, cmd, err):
        try:
            from datetime import datetime as _dt
            with open(PANEL_LAUNCH_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{_dt.now().isoformat(timespec='seconds')}] status={status}\n")
                f.write(f"  python(s): {sys.executable}\n")
                f.write(f"  command:  {' '.join(cmd) if cmd else 'n/a'}\n")
                f.write(f"  watchdog exists: {os.path.exists(WATCHDOG_PY)}\n")
                f.write(f"  main.py exists:  {os.path.exists(MAIN_PY)}\n")
                f.write(f"  error:    {err or 'none'}\n\n")
        except Exception:
            pass

    def _schedule_launch_check(self):
        """Confirm the watchdog actually comes up (pidfile appears) within ~9s.
        If not, surface the reason from the logs instead of waiting forever."""
        try:
            self._launch_check_ticks = 0
            t = QTimer(self)
            t.timeout.connect(lambda: self._launch_check_tick(t))
            t.start(1500)
            self._launch_check_timer = t
        except Exception:
            pass

    def _launch_check_tick(self, timer):
        try:
            if self._is_bot_running():
                timer.stop()
                self._launch_check_timer = None
                self._refresh_status()
                return
            self._launch_check_ticks += 1
            if self._launch_check_ticks >= 6:
                timer.stop()
                self._launch_check_timer = None
                self._pending_start = False
                self.status.setText("bot failed to start")
                QMessageBox.warning(self, "Bot did not start", self._failure_reason())
                self._refresh_status()   # restore the buttons
        except Exception:
            try:
                timer.stop()
            except Exception:
                pass

    def _failure_reason(self):
        parts = []
        for name in ("watchdog.log", "bot_stderr.log", "bot.log"):
            p = os.path.join(BASE_DIR, name)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        tail = "\n".join(f.read().splitlines()[-8:])
                    parts.append(f"--- {name} (last lines) ---\n{tail}")
                except Exception:
                    pass
        if not parts:
            return ("the bot process did not start and no log was written.\n\n"
                    "possible causes:\n"
                    "  - python is not available on PATH\n"
                    "  - an antivirus blocked the process\n"
                    "  - bot_watchdog.py or main.py is missing")
        return "the bot process did not start.\n\n" + "\n\n".join(parts)

    def stop_bot(self):
        self._pending_start = False
        self._stopping = True
        # immediate visible feedback, same as the start side
        self.status.setText("stopping bot...")
        self.start_btn.setText("STOPPING...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setText("STOPPING...")
        self.stop_btn.setEnabled(False)
        try:
            QApplication.processEvents()
        except Exception:
            pass
        if self._launch_check_timer is not None:
            try:
                self._launch_check_timer.stop()
            except Exception:
                pass
            self._launch_check_timer = None
        if self._inprocess:
            if self._engine_obj is not None:
                try:
                    self._engine_obj.stop()
                except Exception:
                    pass
            self._engine_obj = None
            self._engine_running = False
            self._stopping = False
            self.status.setText("bot stopped")
            self._refresh_status()
            return
        try:
            if os.path.exists(PIDFILE):
                with open(PIDFILE) as f:
                    pids = [int(l.strip()) for l in f if l.strip()]
                for pid in reversed(pids):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        pass
                time.sleep(2.0)
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
        except Exception:
            pass
        try:
            os.remove(PIDFILE)
        except OSError:
            pass
        self._stopping = False
        self.status.setText("bot stopped")
        self._refresh_status()

    def _refresh_status(self):
        running = self._is_bot_running()
        pending = bool(getattr(self, "_pending_start", False))
        stopping = bool(getattr(self, "_stopping", False))
        # LOCK the config fields while the bot is running: changing License /
        # Broker / Strategy / Telegram settings mid-run would apply only after a
        # restart anyway, and could confuse the running engine. Unlock on stop.
        try:
            lock = running or pending or stopping
            for ed in getattr(self, "_config_edits", []):
                ed.setReadOnly(lock)
                ed.setEnabled(not lock)
            for b in getattr(self, "_config_btns", []):
                b.setEnabled(not lock)
        except Exception:
            pass
        color = "#2fd07a" if running else "#ff5c5c"
        self.st_chip.setText("BOT RUNNING" if running else "BOT STOPPED")
        self.st_chip.setStyleSheet(f"border:1px solid {color}; border-radius:6px; padding:4px 10px; font-weight:700; background: rgba(10,13,18,0.55); color:{color};")
        # START/STOP buttons always show the live state
        if stopping:
            # a stop is in flight — keep both buttons disabled with STOPPING...
            self.start_btn.setText("STOPPING...")
            self.start_btn.setEnabled(False)
            self.stop_btn.setText("STOPPING...")
            self.stop_btn.setEnabled(False)
            return
        if running:
            self.start_btn.setText("BOT RUNNING")
            self.stop_btn.setText("STOP BOT")
        elif pending:
            self.start_btn.setText("STARTING...")
            self.stop_btn.setText("STOP BOT")
        else:
            self.start_btn.setText("START BOT")
            self.stop_btn.setText("STOPPED")
        self.start_btn.setEnabled(not running and not pending)
        self.stop_btn.setEnabled(running)
        # status line follows the real state:
        #   starting bot... -> (watchdog writes pidfile) -> bot started
        #   bot started -> (process dies) -> bot stopped
        cur = self.status.text()
        if running:
            if self._pending_start or cur == "starting bot...":
                self._pending_start = False
                self.status.setText("bot started")
            # crash-loop hint: only when stderr actually GROWS since the last
            # refresh (a healthy bot that logged one line long ago stays put)
            try:
                sp = os.path.join(DATA_DIR, "bot_stderr.log")
                if os.path.exists(sp):
                    sz = os.path.getsize(sp)
                    if sz > self._stderr_sz >= 0 and time.time() - os.path.getmtime(sp) < 60:
                        self.status.setText("bot restarting — check bot_stderr.log")
                    self._stderr_sz = sz
            except Exception:
                pass
        else:
            if self._pending_start:
                return  # still waiting for the bot to come up
            if cur == "bot started":
                self.status.setText("bot stopped")

    # ── log tail ───────────────────────────────────────────────
    def _tail_log(self):
        try:
            pos = os.path.getsize(BOT_LOG) if os.path.exists(BOT_LOG) else 0
            while not self._stop_tail:
                try:
                    sz = os.path.getsize(BOT_LOG)
                    if sz < pos:
                        pos = 0
                    if sz > pos:
                        with open(BOT_LOG, "r", encoding="utf-8", errors="ignore") as f:
                            f.seek(pos)
                            for line in f:
                                self._log_queue.put(line.rstrip("\n"))
                            pos = f.tell()
                except Exception:
                    pass
                time.sleep(1.0)
        except Exception:
            pass

    def _drain_log(self):
        try:
            lines = []
            while True:
                lines.append(self._log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log.appendPlainText("\n".join(lines))

    # ── stats ──────────────────────────────────────────────────
    def _live_db_counts(self):
        """Live OPEN / PENDING counts read straight from the bot's trades DB
        (read-only). The dashboard's PENDING must reflect live broker state —
        not the accumulated historical signal log, which grows forever and
        never reflects cancelled/filled/expired limits."""
        try:
            import sqlite3
            db = os.environ.get("BOT_DB_PATH", "trading_bot.db")
            if not os.path.isabs(db):
                db = os.path.join(BASE_DIR, db)
            if not os.path.exists(db):
                return None
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(
                    "SELECT status, COUNT(*) FROM trades "
                    "WHERE status IN ('OPEN','PENDING') GROUP BY status").fetchall()
            finally:
                con.close()
            d = dict(rows)
            return {"open": int(d.get("OPEN", 0) or 0),
                    "pending": int(d.get("PENDING", 0) or 0)}
        except Exception:
            return None

    def _refresh_stats(self):
        if not getattr(self, "_stats_built", False):
            return
        stats = SignalStats().load()
        self.equity.set_data(stats["equity"])
        self.tiles["win_rate"].setText(f"{stats['win_rate']:.0%}" if stats["closed"] else "--")
        self.tiles["avg_r"].setText(f"{stats['avg_r']:+.2f}")
        pf = stats["profit_factor"]
        if stats["closed"] == 0:
            self.tiles["profit_factor"].setText("--")
        elif stats["losses"] == 0:
            self.tiles["profit_factor"].setText("∞ · No Losses Yet")
        else:
            self.tiles["profit_factor"].setText(f"{pf:.2f}")
        tpnl = stats["today_pnl"]
        self.tiles["today_pnl"].setText(f"{tpnl:+.2f}")
        self.tiles["today_pnl"].setStyleSheet(
            "font-size:18px; font-weight:800; color:%s;" % ("#2fd07a" if tpnl >= 0 else "#ff5c5c"))
        self.tiles["closed"].setText(str(stats["closed"]))
        # PENDING = live orders resting at the broker (from the bot's DB);
        # fall back to the signal log only if the DB is unreachable
        live = self._live_db_counts()
        self.tiles["pending"].setText(
            str(live["pending"]) if live is not None else str(stats["pending"]))
        # per-symbol table
        self.sym_table.setRowCount(len(stats["per_symbol"]))
        for i, (sym, d) in enumerate(sorted(stats["per_symbol"].items(), key=lambda x: -x[1]["n"])):
            wr = d["wins"] / d["n"] if d["n"] else 0
            pfs = d["win_sum"] / abs(d["loss_sum"]) if d["loss_sum"] else (999 if d["win_sum"] else 0)
            self.sym_table.setItem(i, 0, QTableWidgetItem(sym))
            self.sym_table.setItem(i, 1, QTableWidgetItem(str(d["n"])))
            self.sym_table.setItem(i, 2, QTableWidgetItem(f"{wr:.0%}"))
            self.sym_table.setItem(i, 3, QTableWidgetItem(f"{d['r_sum'] / d['n']:+.2f}"))
            if d["loss_sum"] == 0 and d["win_sum"] > 0:
                self.sym_table.setItem(i, 4, QTableWidgetItem("∞ · No Losses Yet"))
            else:
                self.sym_table.setItem(i, 4, QTableWidgetItem(f"{pfs:.2f}"))

    # ── misc ───────────────────────────────────────────────────
    def open_signals(self):
        if not os.path.exists(SIGNALS_LOG):
            QMessageBox.information(self, "No data", "signals_log.csv doesn't exist yet.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(SIGNALS_LOG)
            else:
                subprocess.Popen(["xdg-open", SIGNALS_LOG])
        except Exception as e:
            QMessageBox.critical(self, "Open failed", str(e))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self.card.setGeometry(18, 18, self.width() - 36, self.height() - 36)
            self.bg_label.setGeometry(0, 0, self.width(), self.height())
            self.dim.setGeometry(0, 0, self.width(), self.height())
            self._apply_background()
        except Exception:
            pass

    def closeEvent(self, event):
        self._stop_tail = True
        # stop every timer so nothing keeps firing after close
        for t in (getattr(self, "_log_timer", None),
                  getattr(self, "_status_timer", None),
                  getattr(self, "_stats_timer", None),
                  getattr(self, "_launch_check_timer", None)):
            try:
                if t is not None:
                    t.stop()
            except Exception:
                pass
        # stop the in-process engine if running
        if self._inprocess and self._engine_obj is not None:
            try:
                self._engine_obj.stop()
            except Exception:
                pass
        # let the tail thread exit cleanly
        try:
            if getattr(self, "_tail_thread", None) is not None:
                self._tail_thread.join(timeout=2)
        except Exception:
            pass
        _release_single_instance(LOCKFILE)
        super().closeEvent(event)


def main():
    _ensure_assets()  # images embedded in assets_b64.py — no separate copy needed
    if not _acquire_single_instance(LOCKFILE):
        try:
            pid = open(LOCKFILE).read().strip()
        except Exception:
            pid = "?"
        app = QApplication(sys.argv)
        QMessageBox.critical(None, "Already running",
                             f"ALADIN Control Panel is already open (pid {pid}).")
        return
    atexit.register(_release_single_instance, LOCKFILE)

    app = QApplication(sys.argv)
    app.setApplicationName("ALADIN")
    win = AladinWindow()
    _set_window_icon(app, win)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
