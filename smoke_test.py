#!/usr/bin/env python3
"""Import smoke test for the ALADIN codebase.

Stubs out non-installed deps (MetaTrader5, tkinter, PIL, ta, cryptography,
dotenv, requests, numpy, pandas) with minimal stand-ins, then imports every
module — catching the class of bug we found (NameError at class-definition /
import time due to a missing import).

Usage: python3 smoke_test.py <dir>   (e.g. review/ or fixed/)
"""
import os
import sys
import types
import traceback

TARGET = sys.argv[1] if len(sys.argv) > 1 else "fixed"
sys.path.insert(0, os.path.abspath(TARGET))
os.environ.setdefault("ALADIN_DRM_KEY", "x" * 40)
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("MAX_DAILY_LOSS_PCT", "10.0")  # mirrors the .env value


def make_mod(name, attrs=None):
    m = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# --- stub deps ---------------------------------------------------------
class _Tk:  # noqa
    def __init__(self, *a, **k): pass
    def mainloop(self): pass
    def configure(self, **k): pass
    def geometry(self, *a): pass
    def minsize(self, *a): pass
    def title(self, *a): pass
    def after(self, ms, fn=None): return 0
    def place(self, **k): pass
    def config(self, **k): pass

tk = make_mod("tkinter", {
    "Tk": _Tk, "END": "end", "BOTH": "both", "X": "x", "LEFT": "left",
    "DISABLED": "disabled", "NORMAL": "normal", "StringVar": lambda *a, **k: types.SimpleNamespace(set=lambda v: None, get=lambda: ""),
    "Entry": lambda *a, **k: types.SimpleNamespace(delete=lambda *x: None, insert=lambda *x: None, get=lambda: "", config=lambda **k: None),
    "Label": lambda *a, **k: types.SimpleNamespace(grid=lambda **k: None, pack=lambda **k: None, place=lambda **k: None),
    "Button": lambda *a, **k: types.SimpleNamespace(grid=lambda **k: None, pack=lambda **k: None, config=lambda **k: None),
    "Frame": lambda *a, **k: types.SimpleNamespace(grid=lambda **k: None, pack=lambda **k: None, grid_columnconfigure=lambda *x, **k: None),
    "LabelFrame": lambda *a, **k: types.SimpleNamespace(pack=lambda **k: None, grid_columnconfigure=lambda *x, **k: None),
})
ttk = make_mod("tkinter.ttk", {"Style": lambda *a, **k: types.SimpleNamespace(theme_use=lambda *x: None, configure=lambda *x, **k: None),
                               "Combobox": lambda *a, **k: types.SimpleNamespace(grid=lambda **k: None)})
tk.ttk = ttk
tk.filedialog = make_mod("tkinter.filedialog", {"askopenfilename": lambda **k: ""})
tk.scrolledtext = make_mod("tkinter.scrolledtext", {"ScrolledText": lambda *a, **k: types.SimpleNamespace(pack=lambda **k: None, configure=lambda **k: None, insert=lambda *x: None, yview=lambda *x: None)})
tk.messagebox = make_mod("tkinter.messagebox", {"showerror": lambda *a, **k: None})

pil = make_mod("PIL")
pil.Image = make_mod("PIL.Image", {"open": lambda *a, **k: types.SimpleNamespace(resize=lambda *a, **k: types.SimpleNamespace()), "LANCZOS": 1})
pil.ImageTk = make_mod("PIL.ImageTk", {"PhotoImage": lambda *a, **k: None})

mt5 = make_mod("MetaTrader5", {
    "TIMEFRAME_M1": 1, "TIMEFRAME_M5": 5, "TIMEFRAME_M15": 15, "TIMEFRAME_M30": 30,
    "TIMEFRAME_H1": 16385, "TIMEFRAME_H4": 16388, "TIMEFRAME_D1": 16408,
    "TRADE_ACTION_DEAL": 1, "TRADE_ACTION_SLTP": 6, "TRADE_ACTION_REMOVE": 5,
    "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1,
    "POSITION_TYPE_BUY": 0, "POSITION_TYPE_SELL": 1,
    "ORDER_FILLING_FOK": 0, "ORDER_FILLING_IOC": 1, "ORDER_FILLING_RETURN": 2,
    "TRADE_RETCODE_DONE": 10009,
    "DEAL_ENTRY_OUT": 1,
    "initialize": lambda **k: True, "shutdown": lambda: None,
    "last_error": lambda: (0, "stub"), "account_info": lambda: None,
    "terminal_info": lambda: types.SimpleNamespace(), "symbol_select": lambda *a: True,
    "symbol_info": lambda *a: None, "symbol_info_tick": lambda *a: None,
    "copy_rates_from_pos": lambda *a: None, "positions_get": lambda *a: None,
    "orders_get": lambda *a: None, "order_send": lambda *a: None,
    "order_calc_margin": lambda *a: 10.0, "history_deals_get": lambda *a: None,
})
make_mod("dotenv", {"load_dotenv": lambda *a, **k: True, "set_key": lambda *a, **k: None})
make_mod("requests", {"get": lambda *a, **k: types.SimpleNamespace(json=lambda: {}, raise_for_status=lambda: None),
                      "post": lambda *a, **k: types.SimpleNamespace(status_code=200, text="")})
make_mod("cryptography")
make_mod("cryptography.fernet", {"Fernet": lambda *a, **k: types.SimpleNamespace(encrypt=lambda x: x, decrypt=lambda x: x)})
make_mod("ta")
make_mod("ta.trend", {"EMAIndicator": lambda **k: types.SimpleNamespace(ema_indicator=lambda: None),
                      "MACD": lambda **k: types.SimpleNamespace(macd=lambda: None, macd_signal=lambda: None, macd_diff=lambda: None),
                      "ADXIndicator": lambda **k: types.SimpleNamespace(adx=lambda: None, adx_pos=lambda: None, adx_neg=lambda: None)})
make_mod("ta.momentum", {"RSIIndicator": lambda **k: types.SimpleNamespace(rsi=lambda: None)})
make_mod("ta.volatility", {"BollingerBands": lambda **k: types.SimpleNamespace(bollinger_hband=lambda: None, bollinger_mavg=lambda: None,
                                                                               bollinger_lband=lambda: None, bollinger_pband=lambda: None),
                           "AverageTrueRange": lambda **k: types.SimpleNamespace(average_true_range=lambda: None)})
np = make_mod("numpy", {"mean": lambda *a, **k: 0.0, "isnan": lambda x: False, "clip": lambda *a, **k: 0.0, "nan": float("nan")})
make_mod("pandas", {"DataFrame": object, "to_datetime": lambda *a, **k: None, "to_numeric": lambda *a, **k: None})

MODULES = [
    "models", "config", "database", "data_engine", "news_filter",
    "notifier", "risk_manager", "scout", "scholar", "orchestrator",
    "keygen", "main", "app",
]

failures = []
for mod in MODULES:
    try:
        __import__(mod)
        print(f"  OK   import {mod}")
    except Exception as e:
        failures.append((mod, e))
        print(f"  FAIL import {mod}: {type(e).__name__}: {e}")

# Runtime check that reproduces the notifier `time` NameError
try:
    from notifier import Notifier
    n = Notifier(enabled=False)  # no threading started (tg disabled, db None)
    try:
        n._is_admin(1, 2)
        print("  OK   notifier._is_admin runs")
    except Exception as e:
        failures.append(("notifier._is_admin", e))
        print(f"  FAIL notifier._is_admin: {type(e).__name__}: {e}")
except Exception as e:
    failures.append(("notifier", e))
    print(f"  FAIL notifier import: {type(e).__name__}: {e}")

# Config semantics check: MAX_DAILY_LOSS_PCT honored
try:
    import config as cfg
    got = cfg.BOT_CONFIG.dynamic.max_daily_loss
    print(f"  INFO max_daily_loss = {got!r}  (expect '10.0%' when MAX_DAILY_LOSS_PCT=10.0)")
except Exception as e:
    failures.append(("config.max_daily_loss", e))
    print(f"  FAIL config max_daily_loss: {e}")

print()
if failures:
    print(f"== {len(failures)} FAILURE(S) ==")
    sys.exit(1)
print("== ALL CHECKS PASSED ==")
