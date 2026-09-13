"""
Métriques de performance calculées sur un journal de paris (bet log), et
graphiques associés. Utilisé par retrain.py après le backtest hebdomadaire —
matplotlib/seaborn ne sont PAS des dépendances de web_app.py (Render), donc
ce module ne doit jamais être importé depuis web_app.py.
"""

import pandas as pd

from src import config


def compute_metrics(bet_log: pd.DataFrame) -> dict:
    """Calcule ROI, yield, win rate, drawdown max sur un journal de paris.
    Retourne des zéros/None proprement si bet_log est vide (aucun pari
    sélectionné par la stratégie) plutôt que de planter."""
    if bet_log.empty:
        return {
            "num_bets": 0, "win_rate": None, "total_staked": 0.0,
            "total_pnl": 0.0, "roi_pct": 0.0, "yield_pct": None,
            "max_drawdown_pct": 0.0, "final_bankroll": config.INITIAL_BANKROLL,
        }

    total_staked = bet_log["stake"].sum()
    total_pnl = bet_log["pnl"].sum()
    num_bets = len(bet_log)
    win_rate = (bet_log["result"] == "won").mean() * 100

    roi_pct = total_pnl / config.INITIAL_BANKROLL * 100
    yield_pct = (total_pnl / total_staked * 100) if total_staked > 0 else None

    bankroll_series = bet_log["bankroll"]
    running_max = bankroll_series.cummax()
    drawdown = (bankroll_series - running_max) / running_max
    max_drawdown_pct = drawdown.min() * 100

    return {
        "num_bets": num_bets,
        "win_rate": round(win_rate, 2),
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(roi_pct, 2),
        "yield_pct": round(yield_pct, 2) if yield_pct is not None else None,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "final_bankroll": round(bankroll_series.iloc[-1], 2),
    }


def save_plots(bet_log: pd.DataFrame, output_dir) -> None:
    """Sauvegarde 3 graphiques (courbe de bankroll, distribution des P&L,
    ROI cumulé) dans output_dir. No-op silencieux si bet_log est vide."""
    if bet_log.empty:
        return

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns

    output_dir.mkdir(parents=True, exist_ok=True)
    sns.set_theme(style="darkgrid")

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(range(len(bet_log)), bet_log["bankroll"])
    ax.set_title("Évolution de la bankroll")
    ax.set_xlabel("Pari #")
    ax.set_ylabel("Bankroll")
    fig.tight_layout()
    fig.savefig(output_dir / "bankroll_curve.png")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5))
    sns.histplot(bet_log["pnl"], bins=30, ax=ax)
    ax.set_title("Distribution des P&L par pari")
    ax.set_xlabel("P&L")
    fig.tight_layout()
    fig.savefig(output_dir / "pnl_distribution.png")
    plt.close(fig)

    cumulative_roi = bet_log["pnl"].cumsum() / config.INITIAL_BANKROLL * 100
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(range(len(bet_log)), cumulative_roi)
    ax.set_title("ROI cumulé")
    ax.set_xlabel("Pari #")
    ax.set_ylabel("ROI cumulé (%)")
    fig.tight_layout()
    fig.savefig(output_dir / "cumulative_roi.png")
    plt.close(fig)
