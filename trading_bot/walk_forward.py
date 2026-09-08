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

Important : chaque fenêtre entraîne ET teste le PORTEFEUILLE complet
(toutes les paires ensemble, via PortfolioBacktester), avec les mêmes
garde-fous que le backtest et le paper trading réels — anti-corrélation,
priorisation par momentum, coupe-circuits partagés. Valider une paire
isolée à la place ne dirait rien sur ces garde-fous-là, qui changent
concrètement quels trades sont pris.

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

from portfolio_backtester import PortfolioBacktester
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


def _split_train_test(market_data: dict, train_start, train_end, test_start, test_end) -> tuple:
    """Découpe chaque paire du portefeuille en train/test pour cette fenêtre.

    `.loc[a:b]` est inclusif des DEUX bornes, et `test_start == train_end`
    par construction (la fenêtre de test commence juste après celle
    d'entraînement) : sans précaution, la bougie `train_end` se
    retrouverait à la fois dans train et dans test. On l'exclut donc de
    test si elle est déjà la dernière bougie de train.
    """
    train_data, test_data = {}, {}
    for symbol, df in market_data.items():
        train_df = df.loc[train_start:train_end]
        test_df = df.loc[test_start:test_end]
        if len(train_df):
            test_df = test_df[test_df.index > train_df.index[-1]]
        train_data[symbol] = train_df
        test_data[symbol] = test_df
    return train_data, test_data


def _run_portfolio(market_data: dict, strategy_cfg: dict, portfolio_kwargs: dict) -> dict:
    def strategy_factory():
        return regime_strategy_from_config(strategy_cfg)

    bt = PortfolioBacktester(strategy_factory=strategy_factory, **portfolio_kwargs)
    return bt.run(market_data)


def walk_forward_analysis(market_data: dict, base_strategy_cfg: dict, param_grid: dict,
                           portfolio_kwargs: dict, train_days: int, test_days: int,
                           step_days: int, selection_metric: str = "sharpe_ratio_approx",
                           min_trades_for_selection: int = 3) -> list:
    """
    market_data: {symbol: DataFrame OHLCV} — même format que
    PortfolioBacktester.run(), pour TOUTES les paires du portefeuille.
    """
    combos = _param_grid_combinations(param_grid)
    windows = []

    # intersection des plages de dates disponibles sur toutes les paires,
    # pour que chaque fenêtre train/test soit valide partout à la fois
    start = max(df.index.min() for df in market_data.values())
    end = min(df.index.max() for df in market_data.values())
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

        train_data, test_data = _split_train_test(market_data, train_start, train_end, test_start, test_end)

        best_combo, best_score = None, -np.inf
        for combo in combos:
            strat_cfg = _apply_overrides(base_strategy_cfg, combo)
            try:
                res = _run_portfolio(train_data, strat_cfg, portfolio_kwargs)
            except (ValueError, KeyError):
                continue
            if res["num_trades"] < min_trades_for_selection:
                continue
            score = res.get(selection_metric, -np.inf)
            if score > best_score:
                best_score, best_combo = score, combo

        if best_combo is None:
            best_combo = combos[0]

        strat_cfg = _apply_overrides(base_strategy_cfg, best_combo)
        try:
            test_res = _run_portfolio(test_data, strat_cfg, portfolio_kwargs)
        except (ValueError, KeyError):
            # pas assez de données communes sur cette fenêtre de test (bord de
            # l'historique) — on saute la fenêtre plutôt que de planter la validation
            window_start += step_delta
            continue

        windows.append({
            "train_start": train_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
            "chosen_params": best_combo,
            "test_return_pct": test_res["total_return_pct"],
            "test_sharpe": test_res["sharpe_ratio_approx"],
            "test_max_drawdown_pct": test_res["max_drawdown_pct"],
            "test_num_trades": test_res["num_trades"],
            "test_win_rate_pct": test_res["win_rate_pct"],
            # diagnostic : POURQUOI une fenêtre gagne ou perd — sans ça on ne
            # peut pas distinguer "mauvais marché" de "stratégie structurellement
            # mauvaise", ni voir quelles sorties (stop/objectif/signal) coûtent
            "test_buy_and_hold_return_pct": test_res["buy_and_hold_return_pct"],
            "test_profit_factor": test_res["profit_factor"],
            "test_exit_reasons": test_res["exit_reasons"],
            "test_per_symbol": test_res["per_symbol"],
            "test_per_regime": test_res["per_regime"],
        })

        window_start += step_delta

    return windows


def aggregate_walk_forward(windows: list) -> dict:
    if not windows:
        return {"num_windows": 0}

    compounded = 1.0
    compounded_bh = 1.0
    exit_reasons = {}
    for w in windows:
        compounded *= (1 + w["test_return_pct"] / 100)
        compounded_bh *= (1 + w.get("test_buy_and_hold_return_pct", 0.0) / 100)
        for reason, count in (w.get("test_exit_reasons") or {}).items():
            exit_reasons[reason] = exit_reasons.get(reason, 0) + count

    total_trades = sum(w["test_num_trades"] for w in windows)
    # taux de gain global pondéré par le nombre de trades, pas la moyenne des
    # taux par fenêtre (une fenêtre à 1 trade gagnant pèserait autant qu'une à 30)
    weighted_wins = sum(w["test_win_rate_pct"] / 100 * w["test_num_trades"] for w in windows)

    return {
        "num_windows": len(windows),
        "compounded_oos_return_pct": (compounded - 1) * 100,
        "compounded_buy_and_hold_pct": (compounded_bh - 1) * 100,
        "avg_oos_sharpe": float(np.mean([w["test_sharpe"] for w in windows])),
        "avg_oos_max_drawdown_pct": float(np.mean([w["test_max_drawdown_pct"] for w in windows])),
        "worst_oos_max_drawdown_pct": float(np.min([w["test_max_drawdown_pct"] for w in windows])),
        "pct_windows_positive": sum(1 for w in windows if w["test_return_pct"] > 0) / len(windows) * 100,
        "total_oos_trades": total_trades,
        "overall_win_rate_pct": (weighted_wins / total_trades * 100) if total_trades else 0.0,
        "exit_reasons": exit_reasons,
    }


def _json_number(value):
    """float/np.float -> float JSON-valide (inf/nan -> None) — un profit
    factor infini (aucune perte) casserait sinon l'écriture jsonb."""
    if value is None:
        return None
    value = float(value)
    return value if np.isfinite(value) else None


def summarize_windows(windows: list) -> list:
    """Version compacte et sérialisable en JSON des fenêtres, pour le
    journal partagé et la sortie console — pas pour recalculer quoi que
    ce soit (les Timestamps deviennent des dates ISO)."""
    out = []
    for w in windows:
        out.append({
            "test_start": str(w["test_start"])[:10],
            "test_end": str(w["test_end"])[:10],
            "chosen_params": w["chosen_params"],
            "return_pct": round(float(w["test_return_pct"]), 2),
            "buy_and_hold_pct": round(float(w.get("test_buy_and_hold_return_pct", 0.0)), 2),
            "num_trades": int(w["test_num_trades"]),
            "win_rate_pct": round(float(w["test_win_rate_pct"]), 1),
            "profit_factor": _json_number(w.get("test_profit_factor")),
            "max_drawdown_pct": round(float(w["test_max_drawdown_pct"]), 2),
            "exit_reasons": dict(w.get("test_exit_reasons") or {}),
            "per_symbol_pnl": {
                s: round(float(v["pnl"]), 2) for s, v in (w.get("test_per_symbol") or {}).items()
            },
            "per_regime": {
                r: {"num_trades": int(v["num_trades"]), "pnl": round(float(v["pnl"]), 2)}
                for r, v in (w.get("test_per_regime") or {}).items()
            },
        })
    return out


def print_windows(windows: list) -> None:
    """Détail lisible fenêtre par fenêtre — c'est ce qui permet de voir si
    une stratégie perd partout (structurel) ou seulement sur une période
    (marché), et si ce sont les stops, les objectifs ou les sorties sur
    signal qui font le résultat."""
    for i, w in enumerate(summarize_windows(windows), start=1):
        pf = "inf" if w["profit_factor"] is None else f"{w['profit_factor']:.2f}"
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(w["exit_reasons"].items())) or "aucun trade"
        per_sym = ", ".join(f"{s}={v:+.2f}" for s, v in w["per_symbol_pnl"].items())
        per_reg = ", ".join(f"{r}={v['pnl']:+.2f} ({v['num_trades']} trades)" for r, v in w["per_regime"].items())
        print(f"  [{i:>2}] {w['test_start']} -> {w['test_end']}  "
              f"rendement {w['return_pct']:+7.2f}%  (buy&hold {w['buy_and_hold_pct']:+7.2f}%)  "
              f"trades={w['num_trades']:>3}  gain={w['win_rate_pct']:5.1f}%  PF={pf}  "
              f"DD={w['max_drawdown_pct']:.2f}%")
        print(f"        params={w['chosen_params']}  sorties: {reasons}")
        if per_sym:
            print(f"        par paire: {per_sym}")
        if per_reg:
            print(f"        par sous-stratégie: {per_reg}")
