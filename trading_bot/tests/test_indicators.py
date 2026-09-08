import numpy as np
import pandas as pd
import pytest

import indicators as ind


def test_sma_matches_manual_average():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = ind.sma(s, window=3)
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == 2.0
    assert result.iloc[4] == 4.0


def test_ema_converges_to_constant_series():
    s = pd.Series([50.0] * 60)
    result = ind.ema(s, span=12)
    assert result.iloc[-1] == pytest.approx(50.0)


def test_rsi_is_bounded_and_high_on_pure_uptrend():
    s = pd.Series(np.arange(1, 60, dtype=float))  # strictement croissant
    result = ind.rsi(s, period=14).dropna()
    assert (result >= 0).all() and (result <= 100).all()
    assert result.iloc[-1] > 90  # que des gains -> RSI proche de 100


def test_macd_line_equals_fast_minus_slow_ema():
    s = pd.Series(np.linspace(100, 150, 80))
    macd_line, signal_line, hist = ind.macd(s, fast=12, slow=26, signal=9)
    expected = ind.ema(s, 12) - ind.ema(s, 26)
    pd.testing.assert_series_equal(macd_line, expected, check_names=False)
    pd.testing.assert_series_equal(hist, macd_line - signal_line, check_names=False)


def test_atr_is_non_negative(ohlcv):
    result = ind.atr(ohlcv, period=14).dropna()
    assert (result >= 0).all()
