import numpy as np
import pandas as pd

from regime import compute_adx, classify_regime, debounce_regime


def test_classify_regime_threshold():
    adx = pd.Series([10.0, 24.9, 25.0, 25.1, 40.0])
    labels = classify_regime(adx, adx_trend_threshold=25.0)
    assert list(labels) == ["ranging", "ranging", "ranging", "trending", "trending"]


def test_debounce_regime_filters_single_bar_flip():
    # un seul "trending" isolé au milieu d'un régime "ranging" stable ne doit
    # pas faire basculer le régime confirmé (hystérésis, confirm_bars=3).
    # Les tout premiers bars restent NaN le temps que l'hystérésis ait assez
    # de bougies pour confirmer un premier régime (comportement attendu).
    raw = ["ranging"] * 5 + ["trending"] + ["ranging"] * 5
    confirmed = debounce_regime(raw, confirm_bars=3).dropna()
    assert (confirmed == "ranging").all()


def test_debounce_regime_confirms_after_enough_bars():
    raw = ["ranging"] * 5 + ["trending"] * 4
    confirmed = debounce_regime(raw, confirm_bars=3)
    assert confirmed.iloc[2:5].eq("ranging").all()
    assert confirmed.iloc[-1] == "trending"


def test_compute_adx_returns_bounded_columns(ohlcv):
    result = compute_adx(ohlcv, period=14).dropna()
    assert {"plus_di", "minus_di", "adx"} <= set(result.columns)
    assert (result["adx"] >= 0).all()
    assert (result["adx"] <= 100).all()
