"""
regime.py
---------
Détection du régime de marché via l'ADX (Average Directional Index), un
indicateur classique qui mesure la FORCE d'une tendance (pas sa direction).

Pourquoi c'est utile : un croisement de moyennes mobiles fonctionne bien
quand le marché est en tendance claire, et mal quand il oscille sans
direction (beaucoup de faux signaux). Une stratégie de retournement
(mean-reversion) fait l'inverse : elle est efficace en marché sans
tendance et se fait détruire par une vraie tendance forte. L'ADX permet
de savoir dans quel type de marché on est, pour utiliser la bonne
stratégie au bon moment plutôt qu'une seule stratégie partout.

ADX > adx_trend_threshold  -> marché en tendance ("trending")
ADX <= adx_trend_threshold -> marché sans direction claire ("ranging")
"""

import numpy as np
import pandas as pd


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Calcule +DI, -DI et ADX (méthode de Wilder).
    Retourne un DataFrame avec les colonnes: plus_di, minus_di, adx
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    plus_dm_smooth = pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    plus_di = 100 * (plus_dm_smooth / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smooth / atr.replace(0, np.nan))

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    return pd.DataFrame({
        "plus_di": plus_di.fillna(0),
        "minus_di": minus_di.fillna(0),
        "adx": adx.fillna(0),
    }, index=df.index)


def classify_regime(adx: pd.Series, adx_trend_threshold: float = 25.0) -> pd.Series:
    """Retourne une série de labels 'trending' / 'ranging'."""
    return np.where(adx > adx_trend_threshold, "trending", "ranging")


def debounce_regime(regime_labels, confirm_bars: int = 3) -> pd.Series:
    """
    Applique une hystérésis à la classification de régime : le régime ne
    bascule officiellement que lorsqu'il est resté identique pendant au
    moins `confirm_bars` bougies consécutives. Tant que ce n'est pas le
    cas, on continue d'utiliser le dernier régime confirmé.

    Sans ça, l'ADX oscille souvent juste autour du seuil (25 par exemple)
    pendant plusieurs bougies d'affilée, ce qui ferait basculer la
    stratégie active plusieurs fois par jour pour rien ("whipsaw" de
    régime) — chaque bascule inutile coûte potentiellement en faux
    signaux d'entrée.
    """
    s = pd.Series(regime_labels)
    same_as_prev = s == s.shift(1)
    run_id = (~same_as_prev).cumsum()
    run_length = s.groupby(run_id).cumcount() + 1

    confirmed_raw = s.where(run_length >= confirm_bars)
    confirmed = confirmed_raw.ffill()
    return confirmed
