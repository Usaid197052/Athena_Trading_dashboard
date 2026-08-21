from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from actions.mt5_analysis import _atr, _ema, _norm_symbol, _rsi, _score, _swings


def _ref_ema(close: np.ndarray, period: int) -> np.ndarray:
    s = pd.Series(close, dtype="float64")
    return s.ewm(span=period, adjust=False).mean().to_numpy()


def _ref_rsi(close: np.ndarray, period: int = 14) -> float:
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_g = pd.Series(gain).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    avg_l = pd.Series(loss).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    rs = avg_g[-1] / (avg_l[-1] + 1e-12)
    return float(100 - (100 / (1 + rs)))


def test_ema_matches_pandas_ewm():
    close = np.linspace(1.0, 2.0, 80)
    got = _ema(close, 20)
    ref = _ref_ema(close, 20)
    assert np.allclose(got, ref, rtol=1e-10, atol=1e-10)


def test_rsi_known_uptrend_is_high():
    close = np.linspace(1.0, 2.0, 50)
    rsi = _rsi(close, 14)
    assert rsi > 70
    assert abs(rsi - _ref_rsi(close, 14)) < 1e-9


def test_rsi_oscillating_is_midrange():
    close = np.array([1.0, 1.02, 1.0, 1.02, 1.0, 1.02, 1.0, 1.02] * 8)
    rsi = _rsi(close, 14)
    assert 30 < rsi < 70


def test_atr_positive_on_ranged_bars():
    n = 40
    close = np.linspace(1.0, 1.1, n)
    high = close + 0.01
    low = close - 0.01
    atr = _atr(high, low, close, 14)
    assert atr > 0


def test_bollinger_mid_is_sma20():
    c = np.arange(1.0, 41.0)
    sma20 = float(c[-20:].mean())
    std20 = float(c[-20:].std())
    bb_u = sma20 + 2 * std20
    bb_l = sma20 - 2 * std20
    assert bb_u > sma20 > bb_l
    assert abs(sma20 - 30.5) < 1e-9


def test_macd_hist_sign_on_uptrend():
    c = np.linspace(1.0, 3.0, 80)
    ema12 = _ema(c, 12)
    ema26 = _ema(c, 26)
    macd_line = ema12 - ema26
    signal = _ema(macd_line, 9)
    hist = float((macd_line - signal)[-1])
    assert hist > 0


def test_score_bullish_stack():
    score, signal, bias, conf, reasons = _score(
        close=1.20, e20=1.15, e50=1.10, e200=1.00,
        rsi=55.0, macd_hist=0.001, atr=0.01,
        sup=1.00, res=1.50, candle="none",
    )
    assert bias == "BUY"
    assert signal == "BULLISH"
    assert score >= 2
    assert any("EMA" in r for r in reasons)
    assert 0 < conf <= 1


def test_score_bearish_stack():
    score, signal, bias, conf, reasons = _score(
        close=1.00, e20=1.05, e50=1.10, e200=1.20,
        rsi=45.0, macd_hist=-0.001, atr=0.01,
        sup=0.80, res=1.40, candle="none",
    )
    assert bias == "SELL"
    assert signal == "BEARISH"
    assert score <= -2


def test_score_mixed_wait():
    score, signal, bias, conf, reasons = _score(
        close=1.10, e20=1.12, e50=1.08, e200=1.00,
        rsi=50.0, macd_hist=0.0, atr=0.01,
        sup=1.00, res=1.20, candle="none",
    )
    assert bias == "WAIT"
    assert signal == "NEUTRAL"


def test_norm_symbol_aliases():
    assert _norm_symbol("gold") == "XAUUSD"
    assert _norm_symbol("EUR/USD") == "EURUSD"
    assert _norm_symbol("xau") == "XAUUSD"
    assert _norm_symbol("brent") == "UKOIL"
