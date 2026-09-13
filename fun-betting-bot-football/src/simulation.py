"""
Backtest : rejoue la stratégie (modèle + EV + Kelly) sur un ensemble de
matchs déjà joués, pour produire un rapport de performance. Utilisé par
retrain.py (GitHub Actions) après chaque ré-entraînement hebdomadaire — la
logique de sélection/mise est EXACTEMENT celle utilisée en direct par
web_app.py (src/strategy.py), pour que le backtest soit représentatif.
"""

import pandas as pd

from src import config, model as model_module, strategy

BET_LOG_COLUMNS = [
    "date", "league", "home_team", "away_team", "selection",
    "prob", "odds", "stake", "result", "pnl", "bankroll",
]


def run_backtest(df: pd.DataFrame, X: pd.DataFrame, y: pd.Series, trained_model) -> pd.DataFrame:
    """Simule les paris sur (X, y) dans l'ordre chronologique de df. `df` doit
    contenir les colonnes Date, League, HomeTeam, AwayTeam, FTR, OddsH/D/A,
    et son index doit correspondre à celui de X/y (voir features.build_features,
    qui aligne les deux en filtrant les lignes sans historique suffisant)."""
    probs_df = model_module.predict_proba(trained_model, X)
    meta = df.loc[X.index].sort_values("Date")

    bankroll = config.INITIAL_BANKROLL
    rows = []

    for idx in meta.index:
        row = meta.loc[idx]
        probs = probs_df.loc[idx].to_dict()
        odds = {"H": row["OddsH"], "D": row["OddsD"], "A": row["OddsA"]}

        bet = strategy.select_bet(probs, odds, config.EV_THRESHOLD)
        if bet is None:
            continue

        stake = strategy.compute_stake(bankroll, bet["prob"], bet["odds"])
        if stake <= 0:
            continue

        actual_result = config.CLASS_TO_RESULT[y.loc[idx]]
        won = bet["selection"] == actual_result
        pnl = stake * (bet["odds"] - 1) if won else -stake
        bankroll += pnl

        rows.append({
            "date": row["Date"],
            "league": row["League"],
            "home_team": row["HomeTeam"],
            "away_team": row["AwayTeam"],
            "selection": bet["selection"],
            "prob": bet["prob"],
            "odds": bet["odds"],
            "stake": stake,
            "result": "won" if won else "lost",
            "pnl": pnl,
            "bankroll": bankroll,
        })

    return pd.DataFrame(rows, columns=BET_LOG_COLUMNS)
