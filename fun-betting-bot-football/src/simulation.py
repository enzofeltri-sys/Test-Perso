"""
Backtest : rejoue la stratégie (modèle de buts Poisson + marchés + combos,
voir src/strategy.build_tickets) sur un ensemble de matchs déjà joués,
groupés par date (un "cycle" = tous les matchs d'une même date, comme un
lot de matchs à venir en live), pour produire un rapport de performance.

⚠️ Limite connue : football-data.co.uk (l'historique) ne fournit que des
cotes 1X2 — aucune cote over/under historique. Le backtest ne peut donc
valider que le marché 1X2, jamais les totals (toujours "estimés" ici
faute de données, donc jamais sélectionnés — voir src/strategy), même si
le bot EN LIVE peut aussi parier sur les totals quand The Odds API fournit
une vraie cote pour le match à venir.
"""

import pandas as pd

from src import config, markets, model as model_module, strategy

TICKET_LOG_COLUMNS = [
    "date", "num_legs", "legs_desc", "prob", "odds", "stake", "result", "pnl", "bankroll",
]


def run_backtest(df: pd.DataFrame, X: pd.DataFrame, y_home: pd.Series, y_away: pd.Series, trained_model: dict) -> pd.DataFrame:
    """Simule les tickets (seuls + combinés) sur (X, y_home, y_away) dans
    l'ordre chronologique de df, groupés par date de match."""
    lambda_home, lambda_away = model_module.predict_goal_rates(trained_model, X)
    meta = df.loc[X.index].copy()
    meta["lambda_home"] = lambda_home
    meta["lambda_away"] = lambda_away

    bankroll = config.INITIAL_BANKROLL
    rows = []

    for date, group in meta.groupby(meta["Date"].dt.date):
        round_matches = []
        for idx, row in group.iterrows():
            probs = markets.market_probabilities(row["lambda_home"], row["lambda_away"])
            # Pas de cotes totals historiques (voir docstring du module) :
            # ce marché reste toujours estimé, donc jamais sélectionné.
            real_odds = {"h2h": {"H": row["OddsH"], "D": row["OddsD"], "A": row["OddsA"]}, "totals": {}}
            candidates = markets.build_candidates(probs, real_odds)
            round_matches.append({
                "match": {
                    "league": row["League"], "home_team": row["HomeTeam"], "away_team": row["AwayTeam"],
                    "commence_time": row["Date"].isoformat(), "_idx": idx,
                },
                "candidates": candidates,
            })

        for ticket in strategy.build_tickets(round_matches):
            stake = strategy.compute_stake(bankroll, ticket["prob"], ticket["odds"])
            if stake <= 0:
                continue

            won = all(
                leg["selection"] == meta.loc[leg["match"]["_idx"], "FTR"]
                for leg in ticket["legs"] if leg["market"] == "1x2"
            )
            pnl = stake * (ticket["odds"] - 1) if won else -stake
            bankroll += pnl

            legs_desc = "; ".join(
                f"{leg['match']['home_team']}-{leg['match']['away_team']}:{leg['selection']}@{leg['odds']:.2f}"
                for leg in ticket["legs"]
            )
            rows.append({
                "date": date, "num_legs": len(ticket["legs"]), "legs_desc": legs_desc,
                "prob": ticket["prob"], "odds": ticket["odds"], "stake": stake,
                "result": "won" if won else "lost", "pnl": pnl, "bankroll": bankroll,
            })

    return pd.DataFrame(rows, columns=TICKET_LOG_COLUMNS)
