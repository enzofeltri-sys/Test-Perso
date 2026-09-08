"""
walk_forward.py
----------------
Validation "walk-forward" : la manière standard de vérifier qu'une
stratégie n'est pas juste sur-ajustée ("overfit") à une période
historique précise.

Principe : on découpe l'historique en fenêtres glissantes. Sur chaque
fenêtre, on choisit les meilleurs paramètres UNIQUEMENT à partir de la
période d'entraînement (train), puis on mesure la performance de ces
paramètres sur la période de test qui suit immédiatement (test),
JAMAIS vue pendant le choix des paramètres. On avance ensuite la
fenêtre et on recommence.

Seuls les résultats de test (hors-échantillon, "out-of-sample" ou OOS)
sont ensuite agrégés pour juger la stratégie — jamais les résultats
d'entraînement, qui seraient artificiellement optimistes. C'est une
simulation raisonnable de "si j'avais réoptimisé mes paramètres tous les
X jours avec seulement les données disponibles à ce moment-là".

Ça ne prouve toujours pas qu'une stratégie sera rentable en vrai (le futur
peut différer du passé de façon que même le walk-forward ne capture pas),
mais un résultat qui s'effondre en walk-forward alors qu'il avait l'air
excellent en backtest simple est un signal d'alerte très fiable de
surapprentissage.
"""

import copy
import itertools

import numpy as np
import pandas as pd

from backtester import Backtester
from strategy import regime_strategy_from_config


def _apply_overrides(base_strategy_cfg: dict, overrides: dict) -> dict:
    cfg = copy.deepcopy(base_strategy_cfg)
    for dotted_key, value in overrides.items():
        section, key = dotted_key.split(".")
        cfg[section][key] = value
    return cfg


def _param_grid_combinations(param_grid: dict) -> list:
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    value_lists = [param_grid[k] for k in keys]
    return [dict(zip(keys, values)) for values in itertools.product(*value_lists)]


def walk_forward_analysis(df: pd.DataFrame, base_strategy_cfg: dict, param_grid: dict,
                           backtest_kwargs: dict, train_days: int, test_days: int,
                           step_days: int, selection_metric: str = "sharpe_ratio_approx",
                           min_trades_for_selection: int = 3) -> list:
    combos = _param_grid_combinations(param_grid)
    windows = []

    start = df.index.min()
    end = df.index.max()
    train_delta = pd.Timedelta(days=train_days)
    test_delta = pd.Timedelta(days=test_days)
    step_delta = pd.Timedelta(days=step_days)

    window_start = start
    while True:
        train_start = window_start
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_end > end:
            break

        train_df = df.loc[train_start:train_end]
        test_df = df.loc[test_start:test_end]

        best_combo, best_score = None, -np.inf
        for combo in combos:
            strat_cfg = _apply_overrides(base_strategy_cfg, combo)
            strat = regime_strategy_from_config(strat_cfg)
            bt = Backtester(strat, **backtest_kwargs)
            try:
                res = bt.run(train_df)
            except Exception:
                continue
            if res["num_trades"] < min_trades_for_selection:
                continue
            score = res.get(selection_metric, -np.inf)
            if score > best_score:
                best_score, best_combo = score, combo

        if best_combo is None:
            best_combo = combos[0]

        strat_cfg = _apply_overrides(base_strategy_cfg, best_combo)
        strat = regime_strategy_from_config(strat_cfg)
        bt = Backtester(strat, **backtest_kwargs)
        test_res = bt.run(test_df)

        windows.append({
            "train_start": train_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
            "chosen_params": best_combo,
            "test_return_pct": test_res["total_return_pct"],
            "test_sharpe": test_res["sharpe_ratio_approx"],
            "test_max_drawdown_pct": test_res["max_drawdown_pct"],
            "test_num_trades": test_res["num_trades"],
            "test_win_rate_pct": test_res["win_rate_pct"],
        })

        window_start += step_delta

    return windows


def aggregate_walk_forward(windows: list) -> dict:
    if not windows:
        return {"num_windows": 0}

    compounded = 1.0
    for w in windows:
        compounded *= (1 + w["test_return_pct"] / 100)

    return {
        "num_windows": len(windows),
        "compounded_oos_return_pct": (compounded - 1) * 100,
        "avg_oos_sharpe": float(np.mean([w["test_sharpe"] for w in windows])),
        "avg_oos_max_drawdown_pct": float(np.mean([w["test_max_drawdown_pct"] for w in windows])),
        "worst_oos_max_drawdown_pct": float(np.min([w["test_max_drawdown_pct"] for w in windows])),
        "pct_windows_positive": sum(1 for w in windows if w["test_return_pct"] > 0) / len(windows) * 100,
        "total_oos_trades": sum(w["test_num_trades"] for w in windows),
    }
