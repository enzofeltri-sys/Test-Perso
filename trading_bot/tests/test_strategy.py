import yaml

from strategy import regime_strategy_from_config


def _load_strategy():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    return regime_strategy_from_config(cfg["strategy"])


def test_should_enter_does_not_commit_active_regime_before_confirmation():
    """Régression : should_enter() proposait un régime candidat en mutant
    directement _active, avant même que l'entrée soit vraiment exécutée. Un
    appelant qui évalue plusieurs candidats avant d'allouer les slots
    disponibles (portfolio_backtester.py / paper_trader.py) pouvait alors
    laisser active_regime pointer sur un régime alors qu'aucune position
    n'était réellement ouverte."""
    strat = _load_strategy()
    row = {"regime": "trending", "score": 5, "rsi": 10}

    assert strat.should_enter(row) is True
    assert strat.active_regime is None  # pas encore confirmé


def test_compute_stop_and_target_confirms_active_regime():
    strat = _load_strategy()
    row = {
        "regime": "trending", "score": 5, "rsi": 10,
        "atr": 2.0, "high": 105, "low": 95, "close": 100,
    }
    assert strat.should_enter(row) is True
    strat.compute_stop_and_target(entry_price=100.0, row=row)
    assert strat.active_regime == "trend"


def test_on_position_closed_resets_active_regime():
    strat = _load_strategy()
    row = {
        "regime": "trending", "score": 5, "rsi": 10,
        "atr": 2.0, "high": 105, "low": 95, "close": 100,
    }
    strat.should_enter(row)
    strat.compute_stop_and_target(100.0, row)
    assert strat.active_regime == "trend"

    strat.on_position_closed()
    assert strat.active_regime is None


def test_market_filter_is_off_by_default(ohlcv):
    strat = _load_strategy()
    assert strat.market_filter_ema is None
    prepared = strat.prepare(ohlcv)
    assert "market_ok" not in prepared.columns


def test_market_filter_blocks_every_entry_below_long_ema():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    cfg["strategy"]["market_filter"] = {"ema_period": 200}
    strat = regime_strategy_from_config(cfg["strategy"])
    assert strat.market_filter_ema == 200

    strong_trend = {"regime": "trending", "score": 5, "rsi": 10}
    strong_range = {"regime": "ranging", "close": 90, "bb_lower": 95, "rsi_mr": 10}
    assert strat.should_enter({**strong_trend, "market_ok": False}) is False
    assert strat.should_enter({**strong_range, "market_ok": False}) is False
    assert strat.should_enter({**strong_trend, "market_ok": True}) is True
    assert strat.should_enter({**strong_range, "market_ok": True}) is True


def test_market_filter_never_removes_rows(ohlcv):
    """Une EMA longue avec min_periods supprimerait (NaN -> dropna) les
    `span` premières bougies de chaque fenêtre de test walk-forward et de
    chaque cycle live — le filtre doit être défini dès la première bougie."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    without = regime_strategy_from_config(cfg["strategy"]).prepare(ohlcv).dropna()
    cfg["strategy"]["market_filter"] = {"ema_period": 10_000}
    with_filter = regime_strategy_from_config(cfg["strategy"]).prepare(ohlcv).dropna()

    assert len(with_filter) == len(without)
    assert with_filter["market_ok"].dtype == bool
