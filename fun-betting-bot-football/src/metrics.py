"""
Métriques de performance calculées sur un journal de paris (bet log), et
graphiques associés. Utilisé par retrain.py après le backtest hebdomadaire —
matplotlib/seaborn ne sont PAS des dépendances de web_app.py (Render), donc
ce module ne doit jamais être importé depuis web_app.py.
"""

import math

import pandas as pd

from src import config


def _finite_or(value, fallback=0.0):
    """None passe tel quel (distinct de "0", voir yield_pct) ; tout float
    non fini (NaN/Infinity, ex: bankroll contaminée par un pari mal filtré
    en amont) est remplacé par `fallback` — sans ça, requests refuse
    d'encoder le JSON envoyé à Supabase (voir supabase_state.save_model)."""
    if value is None:
        return None
    return value if math.isfinite(value) else fallback


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

    # running_max ne peut normalement pas être <= 0 (bankroll part de
    # config.INITIAL_BANKROLL, toujours positif), mais un pari en NaN
    # (ex: cote/probabilité aberrante non filtrée en amont) contamine
    # bankroll par propagation (NaN + x = NaN) — le garde-fou div-par-zéro
    # ci-dessous évite que ça remonte jusqu'au JSON envoyé à Supabase
    # (requests refuse d'encoder NaN/Infinity, voir supabase_state.save_model).
    bankroll_series = bet_log["bankroll"]
    running_max = bankroll_series.cummax()
    safe_running_max = running_max.mask(running_max <= 0)
    drawdown = (bankroll_series - safe_running_max) / safe_running_max
    max_drawdown_pct = drawdown.min(skipna=True) * 100
    if pd.isna(max_drawdown_pct):
        max_drawdown_pct = 0.0

    final_bankroll = bankroll_series.iloc[-1]

    return {
        "num_bets": num_bets,
        "win_rate": _finite_or(round(win_rate, 2)),
        "total_staked": _finite_or(round(total_staked, 2)),
        "total_pnl": _finite_or(round(total_pnl, 2)),
        "roi_pct": _finite_or(round(roi_pct, 2)),
        "yield_pct": _finite_or(round(yield_pct, 2)) if yield_pct is not None else None,
        "max_drawdown_pct": _finite_or(round(max_drawdown_pct, 2)),
        "final_bankroll": _finite_or(round(final_bankroll, 2), fallback=config.INITIAL_BANKROLL),
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
