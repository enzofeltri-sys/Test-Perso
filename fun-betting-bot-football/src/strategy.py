"""
Stratégie de paris : calcul d'EV, sélection des paris, regroupement en
combinés (jusqu'à 3 matchs), dimensionnement de la mise (Kelly réduit).

Règle centrale : on ne parie JAMAIS sur un candidat à cote estimée (voir
src/markets.py) — uniquement sur des cotes réelles de marché (1X2 et,
quand disponible, over/under). Les marchés à cote estimée (double chance,
résultat+buts) restent visibles dans le journal/la page de statut pour
comprendre gains et pertes, mais ne sont jamais des paris réels.
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


def compute_stake(
    bankroll: float,
    prob: float,
    odds: float,
    kelly_multiplier: float = config.KELLY_MULTIPLIER,
    max_stake_fraction: float = config.MAX_STAKE_FRACTION,
) -> float:
    """Mise = bankroll * kelly_fraction * kelly_multiplier, plafonnée à
    max_stake_fraction de la bankroll, jamais négative. S'applique aussi
    bien à un pari seul qu'à un ticket combiné (prob/odds = celles du
    combiné dans ce cas, voir build_tickets)."""
    fraction = kelly_fraction(prob, odds) * kelly_multiplier
    fraction = min(fraction, max_stake_fraction)
    return max(0.0, round(bankroll * fraction, 2))


def best_candidate_for_match(candidates: list[dict]) -> dict | None:
    """Parmi les candidats d'UN match (issus de markets.build_candidates),
    retourne celui à l'EV la plus haute PARMI LES COTES RÉELLES uniquement
    — jamais une cote estimée : le bot ne parie jamais contre sa propre
    estimation faute de prix de marché indépendant (voir src/markets.py).
    Ignore aussi les candidats dont l'EV dépasse config.MAX_SANE_EV (voir
    ce commentaire) : une EV énorme trahit presque toujours une probabilité
    de modèle aberrante plutôt qu'une vraie occasion, un autre candidat du
    même match reste éligible s'il en a une plus raisonnable. None si aucun
    candidat n'a de cote réelle exploitable ET une EV crédible."""
    real = [c for c in candidates if not c["is_estimated"] and _is_valid_odds(c.get("odds"))]
    if not real:
        return None
    scored = [{**c, "ev": calculate_ev(c["prob"], c["odds"])} for c in real]
    sane = [c for c in scored if c["ev"] <= config.MAX_SANE_EV]
    if not sane:
        return None
    return max(sane, key=lambda c: c["ev"])


def _match_key(match: dict) -> tuple:
    return (match.get("home_team"), match.get("away_team"), match.get("commence_time"))


def _make_ticket(legs: list[dict]) -> dict:
    combo_prob, combo_odds = 1.0, 1.0
    for leg in legs:
        combo_prob *= leg["prob"]
        combo_odds *= leg["odds"]
    return {
        "legs": legs,
        "prob": combo_prob,
        "odds": combo_odds,
        "ev": calculate_ev(combo_prob, combo_odds),
    }


def build_tickets(matches: list[dict]) -> list[dict]:
    """matches : [{"match": {league, home_team, away_team, commence_time, ...},
    "candidates": [...]}] pour un lot de matchs à venir (un cycle /tick).

    Retourne les tickets à parier :
    - Les jambes dont l'EV dépasse COMBO_LEG_EV_THRESHOLD (seuil renforcé)
      sont regroupées, triées par EV décroissante, par paquets de
      MAX_COMBO_LEGS (3) maximum — un ticket combiné par paquet (jamais
      deux sélections du MÊME match dans un combiné, chaque jambe vient
      d'un match différent).
    - Les matchs restants (pas dans un combiné) dont la meilleure EV
      dépasse le seuil plus souple SINGLE_EV_THRESHOLD deviennent des
      paris seuls.
    - Probabilité/cote d'un combiné = produit des jambes (indépendance
      entre matchs différents, hypothèse raisonnable)."""
    best_by_match = []
    for entry in matches:
        best = best_candidate_for_match(entry["candidates"])
        if best is not None:
            best_by_match.append({"match": entry["match"], **best})

    combo_pool = sorted(
        (b for b in best_by_match if b["ev"] > config.COMBO_LEG_EV_THRESHOLD),
        key=lambda b: b["ev"], reverse=True,
    )
    used_keys = {_match_key(b["match"]) for b in combo_pool}

    tickets = [
        _make_ticket(combo_pool[i:i + config.MAX_COMBO_LEGS])
        for i in range(0, len(combo_pool), config.MAX_COMBO_LEGS)
    ]

    for b in best_by_match:
        if _match_key(b["match"]) in used_keys:
            continue
        if b["ev"] > config.SINGLE_EV_THRESHOLD:
            tickets.append(_make_ticket([b]))

    return tickets
