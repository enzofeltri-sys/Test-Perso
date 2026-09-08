"""
indicators.py
-------------
Fonctions d'indicateurs techniques, indépendantes de toute logique de
décision. Chacune prend une colonne de prix (ou un DataFrame OHLCV) et
retourne une série pandas.
"""

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, min_periods=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # avg_loss == 0 rend `rs` NaN dans les deux cas suivants, qu'il faut
    # distinguer : une vraie série de hausses pures (aucune perte sur toute
    # la période) doit donner un RSI de 100 (extrême), pas 50 (neutre) —
    # sans ce cas particulier, un marché en hausse ininterrompue ne
    # déclenchait jamais le veto de surachat (rsi_veto_overbought).
    pure_uptrend = (avg_gain > 0) & (avg_loss == 0)
    result = result.where(~pure_uptrend, 100.0)
    return result.fillna(50)  # neutre tant qu'on manque de données ou d'aucun mouvement


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Retourne (macd_line, signal_line, histogram)."""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, min_periods=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — mesure de volatilité basée sur high/low/close."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def volume_sma(series: pd.Series, window: int = 20) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()
