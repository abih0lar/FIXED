"""Unit + integration tests for the Scout SMC signal engine.

All market data is synthetic and hand-built so every fractal pivot,
FVG and structure call is deterministic. See conftest.py for builders.
"""
import numpy as np
import pandas as pd
import pytest

from conftest import (
    candles_from_closes, fvg_tail_df, flat_df, make_snapshot, rising_df,
    zigzag_df,
)
from models import Bias, ScoutSignal

SYMBOL = "EURUSDm"


# ── dealing range (Premium / Discount matrix) ────────────────────────────

def test_dealing_range_brackets_window(fake_engine):
    from scout import Scout
    df = rising_df(120, 1.05, 1.0962)
    sc = Scout(fake_engine(), db=None)
    sh, sl, eq = sc.get_dealing_range(df, lookback=48)
    window = df.tail(48)
    assert sh == pytest.approx(float(window["high"].max()))
    assert sl == pytest.approx(float(window["low"].min()))
    assert eq == pytest.approx((sh + sl) / 2)
    assert sl < eq < sh


def test_dealing_range_insufficient_bars(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    assert sc.get_dealing_range(rising_df(30), lookback=120) == (None, None, None)


# ── fractal swings & dedupe ──────────────────────────────────────────────

def test_fractal_swings_find_exact_pivots(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    df = zigzag_df(n_legs=10, leg_len=12)          # peaks ~1.004..1.008
    highs, lows = sc._get_fractal_swings(df, window=3)
    assert len(highs) >= 2 and len(lows) >= 2
    # every detected pivot high must be a local maximum of its +-3 window
    for h in highs:
        i = df.index.get_loc(h["idx"])
        lo, hi = max(0, i - 3), min(len(df), i + 4)
        assert h["price"] == df["high"].iloc[lo:hi].max()


def test_dedupe_fractals_keeps_first_of_plateau(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    pivots = [{"idx": 1, "price": 1.1050}, {"idx": 2, "price": 1.1050},
              {"idx": 3, "price": 1.1060}]
    out = sc._dedupe_fractals(pivots)
    assert [p["idx"] for p in out] == [1, 3]


# ── market structure ─────────────────────────────────────────────────────

def test_structure_bullish_bos(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    df = zigzag_df(n_legs=7, leg_len=8)            # HH + HL, closes above last high
    bias, struct, level = sc.detect_structure(df, lookback=40)
    assert bias == "Bullish"
    assert struct == "BOS"
    assert level is not None


def test_structure_bearish_bos(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    # start with a down leg: LH + LL sequence, closes below the last low
    df = zigzag_df(n_legs=7, leg_len=8, start_dir=-1, up_leg=0.0030, down_leg=0.0040)
    bias, struct, level = sc.detect_structure(df, lookback=40)
    assert bias == "Bearish"
    assert struct == "BOS"


def test_structure_falls_back_to_ema_when_no_pivots(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    df = rising_df(120)                            # strictly rising -> no fractals
    bias, struct, level = sc.detect_structure(df, lookback=100)
    assert bias == "Bullish" and struct == "None" and level is None


# ── liquidity sweeps ─────────────────────────────────────────────────────

def _sweep_df(direction="Bearish"):
    """Pivot at bar 10 (1.0100 high / 1.0000 low), recent bars pierce it and
    close back inside — the classic stop-hunt."""
    if direction == "Bearish":
        closes = list(np.linspace(1.0000, 1.0098, 11))       # bar 10 high = 1.0100
        closes += list(np.linspace(1.0090, 1.0048, 14))      # fall away
        closes += [1.0048] * 5 + [1.0050] * 4                # drift
        closes += [1.0060, 1.0080, 1.0100, 1.0102]           # sweep high 1.0104
        closes += [1.0090]                                   # close back under
    else:
        closes = list(np.linspace(1.0100, 1.0002, 11))       # bar 10 low = 1.0000
        closes += list(np.linspace(1.0010, 1.0052, 14))
        closes += [1.0052] * 5 + [1.0050] * 4
        closes += [1.0040, 1.0020, 1.0000, 0.9998]           # sweep low 0.9996
        closes += [1.0010]                                   # close back above
    return candles_from_closes(closes, wick=0.0002)


def test_bearish_liquidity_sweep_detected(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    swept, level = sc.detect_liquidity_sweep(_sweep_df("Bearish"), "Bearish")
    assert swept is True
    assert level == pytest.approx(1.0100)


def test_bullish_liquidity_sweep_detected(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    swept, level = sc.detect_liquidity_sweep(_sweep_df("Bullish"), "Bullish")
    assert swept is True
    assert level == pytest.approx(1.0000)


def test_no_sweep_when_price_stays_inside_range(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    swept, _ = sc.detect_liquidity_sweep(rising_df(120), "Bearish")
    assert swept is False


# ── FVG / order blocks / liquidity targets ───────────────────────────────

def test_bullish_fvg_found_and_unmitigated(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    fvg = sc.find_fvg(fvg_tail_df(), "Bullish", lookback=40)
    assert fvg is not None
    top, bot = fvg
    assert top == pytest.approx(1.0960)
    assert bot == pytest.approx(1.0950)
    assert top > bot


def test_bearish_fvg_found(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    # c1 = idx2 (low 1.1000), c3 = idx4 (high 1.0962): bearish gap [1.0962, 1.1000].
    # last bar's high (1.0942) stays below the mitigation line (1.0943).
    o = [1.1000, 1.1000, 1.1000, 1.0960, 1.0950, 1.0940]
    h = [1.1002, 1.1002, 1.1002, 1.0972, 1.0962, 1.0942]
    l = [1.0998, 1.0998, 1.1000, 1.0968, 1.0962, 1.0938]
    c = [1.1000, 1.1000, 1.1000, 1.0960, 1.0955, 1.0940]
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                       "volume": [100.0] * 6})
    fvg = sc.find_fvg(df, "Bearish", lookback=40)
    assert fvg is not None
    top, bot = fvg
    assert top == pytest.approx(1.1000)   # upper edge = c1.low
    assert bot == pytest.approx(1.0962)   # lower edge = c3.high


def test_no_fvg_in_smooth_series(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    assert sc.find_fvg(rising_df(60, 1.0, 1.01), "Bullish", lookback=40) is None


def test_order_block_anchors_to_origin_extreme(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    df = fvg_tail_df()
    ob = sc.find_order_block(df, 1.0960, 1.0950, "Bullish")
    assert ob is not None
    ob_high, ob_low = ob
    assert ob_high <= 1.0950 + 1e-9          # cluster preceding the gap
    assert ob_low < ob_high


def test_liquidity_targets_sorted_by_proximity(fake_engine):
    from scout import Scout
    sc = Scout(fake_engine(), db=None)
    df = zigzag_df(n_legs=10, leg_len=12)
    targets = sc.find_liquidity_targets(df, "Bullish", price=1.0060, lookback=120)
    assert targets, "zigzag must leave unswept fractal highs above price"
    assert all(t > 1.0060 for t in targets)
    assert targets == sorted(targets, key=lambda t: abs(t - 1.0060))


# ── full scan pipeline ───────────────────────────────────────────────────

def _frames(DF4, DF1, DF15, DF5):
    return {"4h": DF4, "1h": DF1, "15m": DF15, "5m": DF5}


def test_scan_emits_long_sniper_setup(fake_engine, db, dyn, monkeypatch):
    from scout import Scout
    monkeypatch.setattr(dyn, "htf_zone_filter", False)
    monkeypatch.setattr(dyn, "min_adx", 0.0)
    monkeypatch.setattr(dyn, "min_rr", 1.5)
    monkeypatch.setattr(dyn, "tp_target_rr", 2.0)
    monkeypatch.setattr(dyn, "min_fvg_gap_pct", 0.0002)

    snap = make_snapshot(price=1.0962, atr=0.01)   # wide ATR -> wide SL -> RR at limit clears the gate
    eng = fake_engine(_frames(zigzag_df(), rising_df(), fvg_tail_df(), flat_df()), snap)
    signal = Scout(eng, db).scan(SYMBOL)

    assert signal.bias == Bias.LONG
    assert 0.0 < signal.confidence <= 1.0
    assert signal.awaiting_entry is False                  # price is at the gap
    assert signal.limit_entry_price == pytest.approx(1.0960)
    assert signal.suggested_sl < 1.0950                    # below the FVG
    assert signal.suggested_tp > snap.price
    assert "SMC FVG" in signal.patterns_detected
    assert signal.indicators is snap


def test_scan_emits_short_setup_on_bearish_structure(fake_engine, db, dyn, monkeypatch):
    from scout import Scout
    monkeypatch.setattr(dyn, "htf_zone_filter", False)
    monkeypatch.setattr(dyn, "min_adx", 0.0)
    monkeypatch.setattr(dyn, "min_rr", 1.5)
    monkeypatch.setattr(dyn, "tp_target_rr", 2.0)
    monkeypatch.setattr(dyn, "min_fvg_gap_pct", 0.0002)

    bear = zigzag_df(n_legs=10, leg_len=12, start_dir=-1, up_leg=0.0030, down_leg=0.0040)
    fall = rising_df(120, 1.0962, 1.0500)                  # inverted -> falling
    snap = make_snapshot(price=1.0938, atr=0.01)
    eng = fake_engine(_frames(bear, fall, _bear_fvg_tail(), flat_df(seed=3)), snap)
    signal = Scout(eng, db).scan(SYMBOL)

    assert signal.bias == Bias.SHORT
    assert signal.suggested_sl > snap.price                # stop above entry
    assert signal.suggested_tp < snap.price


def test_scan_denied_when_bullish_but_price_in_premium(fake_engine, db, dyn, monkeypatch):
    from scout import Scout
    monkeypatch.setattr(dyn, "htf_zone_filter", True)      # strict P/D matrix
    monkeypatch.setattr(dyn, "min_adx", 0.0)

    snap = make_snapshot(price=1.0962)                     # above 1h equilibrium
    eng = fake_engine(_frames(zigzag_df(), rising_df(), fvg_tail_df(), flat_df()), snap)
    signal = Scout(eng, db).scan(SYMBOL)

    assert signal.bias == Bias.NEUTRAL
    assert signal.confidence == 0.0
    assert "Premium" in signal.reasoning


def test_scan_denied_when_htf_trend_too_weak(fake_engine, db, dyn, monkeypatch):
    from scout import Scout
    monkeypatch.setattr(dyn, "min_adx", 25.0)
    monkeypatch.setattr(dyn, "htf_zone_filter", False)

    df4 = zigzag_df()
    df4["adx"] = 10.0                                      # weak 4h trend
    snap = make_snapshot(price=1.0962)
    eng = fake_engine(_frames(df4, rising_df(), fvg_tail_df(), flat_df()), snap)
    signal = Scout(eng, db).scan(SYMBOL)

    assert signal.bias == Bias.NEUTRAL
    assert "ADX" in signal.reasoning


def test_scan_denied_without_fvg(fake_engine, db, dyn, monkeypatch):
    from scout import Scout
    monkeypatch.setattr(dyn, "htf_zone_filter", False)
    monkeypatch.setattr(dyn, "min_adx", 0.0)

    snap = make_snapshot(price=1.1000)
    eng = fake_engine(_frames(zigzag_df(), rising_df(), flat_df(seed=1),
                              flat_df(seed=2)), snap)
    signal = Scout(eng, db).scan(SYMBOL)

    assert signal.bias == Bias.NEUTRAL
    assert "No unmitigated FVG" in signal.reasoning


def test_scan_flags_awaiting_entry_when_price_above_tolerance(fake_engine, db, dyn, monkeypatch):
    from scout import Scout
    monkeypatch.setattr(dyn, "htf_zone_filter", False)
    monkeypatch.setattr(dyn, "min_adx", 0.0)
    monkeypatch.setattr(dyn, "min_rr", 0.9)
    monkeypatch.setattr(dyn, "tp_target_rr", 2.0)
    monkeypatch.setattr(dyn, "fvg_entry_tolerance", 0.5)   # narrow the window
    monkeypatch.setattr(dyn, "min_fvg_gap_pct", 0.0002)

    snap = make_snapshot(price=1.0968, atr=0.01)           # above gap, below TP
    eng = fake_engine(_frames(zigzag_df(), rising_df(), fvg_tail_df(), flat_df()), snap)
    signal = Scout(eng, db).scan(SYMBOL)

    assert signal.bias == Bias.LONG
    assert signal.awaiting_entry is True
    assert signal.limit_entry_price == pytest.approx(1.0960)


def test_scan_survives_data_outage(fake_engine, db):
    from scout import Scout
    eng = fake_engine({})                                  # no frames at all
    signal = Scout(eng, db).scan(SYMBOL)
    assert signal.bias == Bias.NEUTRAL
    assert "Data fetch failed" in signal.reasoning


def _bear_fvg_tail():
    """Mirror of fvg_tail_df: falling series (118 bars) ending in a clean
    bearish FVG. find_fvg returns (1.0950, 1.0940)."""
    closes = list(np.linspace(1.1000, 1.0956, 118))
    closes += [1.0952, 1.0942, 1.0938, 1.0932]          # c1, c2, c3, last
    df = candles_from_closes(closes, wick=0.0002)
    h, l = df.columns.get_loc("high"), df.columns.get_loc("low")
    df.iloc[-4, l] = 1.0950                              # c1.low  = gap upper edge
    df.iloc[-3, h] = 1.0944                              # c2 stays inside zone
    df.iloc[-2, h] = 1.0940                              # c3.high = gap lower edge
    df.iloc[-1, h] = 1.0934                              # below mitigation line
    return df
