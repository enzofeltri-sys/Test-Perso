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
