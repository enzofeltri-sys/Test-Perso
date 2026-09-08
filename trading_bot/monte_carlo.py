"""
monte_carlo.py
---------------
Un backtest ne donne qu'UNE seule courbe d'équité : celle correspondant
à l'ordre exact dans lequel les trades sont arrivés historiquement. Si on
avait eu la série de gains/pertes dans un ordre différent (les mêmes
trades, juste mélangés), le chemin — et surtout le pire drawdown en
cours de route — aurait pu être très différent, même si le résultat final
moyen reste le même.

Le bootstrap ici ré-échantillonne (avec remise) les gains/pertes de
chaque trade du backtest pour générer des centaines de chemins
alternatifs, et rapporte une FOURCHETTE de résultats plausibles
(percentiles) plutôt qu'un seul chiffre. Un backtest qui a l'air
excellent mais dont la fourchette Monte Carlo est très large (ou dont le
5e percentile est franchement mauvais) est moins fiable qu'il n'y paraît.

Limite importante : ce ré-échantillonnage suppose que les trades sont
indépendants les uns des autres. En pratique, des trades consécutifs
dans le même régime de marché peuvent être corrélés (une série de pertes
groupées pendant un "mauvais" mois, par exemple) — le bootstrap simple
tend donc à SOUS-ESTIMER légèrement le risque de drawdown réel.
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def bootstrap_trade_returns(trade_pnls: list, initial_balance: float,
                             num_simulations: int = 1000, seed: int = 42) -> dict:
    if not trade_pnls:
        return {"num_simulations": 0}

    rng = np.random.default_rng(seed)
    pnls = np.array(trade_pnls, dtype=float)
    n = len(pnls)

    final_returns = np.empty(num_simulations)
    max_drawdowns = np.empty(num_simulations)

    for i in range(num_simulations):
        sample = rng.choice(pnls, size=n, replace=True)
        equity = np.concatenate([[initial_balance], initial_balance + np.cumsum(sample)])
        running_max = np.maximum.accumulate(equity)
        drawdown = (equity - running_max) / running_max
        max_drawdowns[i] = drawdown.min() * 100
        final_returns[i] = (equity[-1] / initial_balance - 1) * 100

    def pct(arr, p):
        return float(np.percentile(arr, p))

    return {
        "num_simulations": num_simulations,
        "num_trades_per_sim": n,
        "return_pct_p5": pct(final_returns, 5),
        "return_pct_p25": pct(final_returns, 25),
        "return_pct_p50": pct(final_returns, 50),
        "return_pct_p75": pct(final_returns, 75),
        "return_pct_p95": pct(final_returns, 95),
        "prob_of_loss_pct": float((final_returns < 0).mean() * 100),
        "max_drawdown_p5_worst_case": pct(max_drawdowns, 5),
        "max_drawdown_p50_median": pct(max_drawdowns, 50),
        "max_drawdown_p95_best_case": pct(max_drawdowns, 95),
        "_final_returns_array": final_returns,
    }


def plot_distribution(final_returns_array, out_path: str):
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.hist(final_returns_array, bins=50, color="#2563eb", alpha=0.8)
    ax.axvline(0, color="black", linestyle="--", linewidth=1)
    median = np.median(final_returns_array)
    ax.axvline(median, color="#dc2626", linestyle="-", linewidth=1.5, label=f"Médiane = {median:.1f}%")
    ax.set_xlabel("Rendement final simulé (%)")
    ax.set_ylabel("Nombre de simulations")
    ax.set_title("Distribution Monte Carlo des rendements possibles\n(mêmes trades, ordres différents)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
