"""
Marchés de paris déduits du modèle de buts (Poisson) : 1X2, double chance,
over/under (0.5/1.5/2.5 buts), et le marché composé "résultat + total de
buts". Ce que chacun a comme cote :

  - 1X2            : VRAIE cote (The Odds API, marché h2h)
  - Over/Under     : VRAIE cote tentée (The Odds API, marché "totals"),
                     repli sur une cote ESTIMÉE si la ligne n'est pas
                     disponible pour ce match
  - Double chance  : toujours ESTIMÉE — affichage/compréhension
                     uniquement, jamais pariable
  - Résultat+buts  : toujours ESTIMÉE — idem (pas un marché standard chez
                     The Odds API)

Une cote "estimée" est la cote équitable du modèle (1/proba) réduite d'une
marge bookmaker typique (config.ESTIMATED_ODDS_MARGIN). C'est volontaire :
ça la rend structurellement impossible à "battre" avec ce même modèle
(EV = proba × cote_estimée − 1 = 1/marge − 1 < 0 toujours), donc le bot ne
peut jamais parier contre sa propre opinion sans un vrai prix de marché
indépendant — seulement l'afficher pour comprendre gains/pertes.
"""

import numpy as np
from scipy.stats import poisson

from src import config


def joint_grid(lambda_home: float, lambda_away: float, goals_max: int = config.GOALS_GRID_MAX) -> np.ndarray:
    """Grille jointe P(buts_domicile=i, buts_extérieur=j) pour i,j dans
    [0, goals_max], en supposant les deux processus de buts indépendants
    (approximation Poisson standard, sans correction de corrélation type
    Dixon-Coles pour les scores faibles — gardée simple ici)."""
    home_pmf = poisson.pmf(np.arange(goals_max + 1), lambda_home)
    away_pmf = poisson.pmf(np.arange(goals_max + 1), lambda_away)
    return np.outer(home_pmf, away_pmf)


def market_probabilities(lambda_home: float, lambda_away: float) -> dict:
    """Calcule toutes les probabilités de marché supportées à partir de la
    grille jointe de Poisson. Dict imbriqué par marché."""
    grid = joint_grid(lambda_home, lambda_away)
    home_goals, away_goals = np.indices(grid.shape)
    total_goals = home_goals + away_goals

    outcome_masks = {
        "H": home_goals > away_goals,
        "D": home_goals == away_goals,
        "A": home_goals < away_goals,
    }
    result = {k: grid[mask].sum() for k, mask in outcome_masks.items()}
    double_chance = {
        "1X": result["H"] + result["D"],
        "X2": result["D"] + result["A"],
        "12": result["H"] + result["A"],
    }

    totals = {}
    for line in config.TOTAL_GOALS_LINES:
        p_over = grid[total_goals > line].sum()
        totals[line] = {"Over": p_over, "Under": 1.0 - p_over}

    rg_line = config.RESULT_AND_GOALS_LINE
    over_mask = total_goals > rg_line
    result_and_goals = {}
    for outcome, outcome_mask in outcome_masks.items():
        result_and_goals[f"{outcome} & Over {rg_line}"] = grid[outcome_mask & over_mask].sum()
        result_and_goals[f"{outcome} & Under {rg_line}"] = grid[outcome_mask & ~over_mask].sum()

    return {
        "1x2": result,
        "double_chance": double_chance,
        "totals": totals,
        "result_and_goals": result_and_goals,
    }


def estimated_odds(prob: float, margin: float = config.ESTIMATED_ODDS_MARGIN) -> float:
    """Cote estimée = cote équitable (1/proba) réduite d'une marge
    bookmaker typique. Voir docstring du module."""
    if prob <= 0:
        return float("inf")
    return 1.0 / (prob * margin)


def build_candidates(probs: dict, real_odds: dict) -> list[dict]:
    """Liste à plat de tous les candidats de pari pour UN match :
    {market, selection, prob, odds, is_estimated}.

    real_odds : cotes réellement récupérées (The Odds API), déjà
    normalisées par l'appelant :
      {"h2h": {"H": o, "D": o, "A": o} | None,
       "totals": {0.5: {"Over": o, "Under": o} | None, 1.5: ..., 2.5: ...}}
    Une entrée manquante ou None retombe sur la cote estimée (is_estimated
    = True) — voir docstring du module pour ce qui reste pariable ou non."""
    candidates = []

    h2h_odds = real_odds.get("h2h") or {}
    for selection, prob in probs["1x2"].items():
        odds = h2h_odds.get(selection)
        candidates.append({
            "market": "1x2", "selection": selection, "prob": prob,
            "odds": odds if odds else estimated_odds(prob),
            "is_estimated": not bool(odds),
        })

    for selection, prob in probs["double_chance"].items():
        candidates.append({
            "market": "double_chance", "selection": selection, "prob": prob,
            "odds": estimated_odds(prob), "is_estimated": True,
        })

    totals_odds = real_odds.get("totals") or {}
    for line, line_probs in probs["totals"].items():
        line_real = totals_odds.get(line) or {}
        for selection, prob in line_probs.items():
            odds = line_real.get(selection)
            candidates.append({
                "market": "totals", "selection": f"{selection} {line}", "prob": prob,
                "odds": odds if odds else estimated_odds(prob),
                "is_estimated": not bool(odds),
            })

    for selection, prob in probs["result_and_goals"].items():
        candidates.append({
            "market": "result_and_goals", "selection": selection, "prob": prob,
            "odds": estimated_odds(prob), "is_estimated": True,
        })

    return candidates
