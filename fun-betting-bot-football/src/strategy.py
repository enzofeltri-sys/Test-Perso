"""
Stratégie de paris : calcul d'EV, critère de sélection, dimensionnement de
la mise (fraction de Kelly réduite). Un seul pari maximum par match (celui
avec l'EV le plus élevé au-dessus du seuil) — évite de parier H et A sur le
même match, plus simple à suivre et à auditer dans le journal.
"""

import math

from src import config


def _is_valid_odds(value) -> bool:
    return value is not None and not (isinstance(value, float) and math.isnan(value)) and value > 1


def calculate_ev(prob: float, odds: float) -> float:
    """Espérance de valeur d'un pari unitaire : EV = p * cote - 1."""
    return prob * odds - 1


def kelly_fraction(prob: float, odds: float) -> float:
    """Fraction de Kelly pleine : f* = (p*(o-1) - (1-p)) / (o-1).
    Bornée à [0, 1] — jamais de mise négative (pas d'edge) ni > bankroll."""
    if odds <= 1:
        return 0.0
    edge = prob * (odds - 1) - (1 - prob)
    fraction = edge / (odds - 1)
    return max(0.0, min(fraction, 1.0))


def select_bet(probs: dict, odds: dict, ev_threshold: float = config.EV_THRESHOLD) -> dict | None:
    """Parmi les issues H/D/A, retourne celle avec l'EV le plus élevé si elle
    dépasse le seuil, sinon None (aucun pari sur ce match).

    probs, odds : dicts {"H": ..., "D": ..., "A": ...}
    """
    candidates = []
    for outcome in ("H", "D", "A"):
        p, o = probs.get(outcome), odds.get(outcome)
        if p is None or not _is_valid_odds(o):
            continue
        ev = calculate_ev(p, o)
        if ev > ev_threshold:
            candidates.append({"selection": outcome, "prob": p, "odds": o, "ev": ev})

    if not candidates:
        return None
    return max(candidates, key=lambda c: c["ev"])


def compute_stake(
    bankroll: float,
    prob: float,
    odds: float,
    kelly_multiplier: float = config.KELLY_MULTIPLIER,
    max_stake_fraction: float = config.MAX_STAKE_FRACTION,
) -> float:
    """Mise = bankroll * kelly_fraction * kelly_multiplier, plafonnée à
    max_stake_fraction de la bankroll (garde-fou contre un edge mal estimé
    par le modèle) et jamais négative."""
    fraction = kelly_fraction(prob, odds) * kelly_multiplier
    fraction = min(fraction, max_stake_fraction)
    return max(0.0, round(bankroll * fraction, 2))
