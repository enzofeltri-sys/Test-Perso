"""
Construction des features à partir de l'historique brut : forme récente
(points, buts marqués/encaissés sur les N derniers matchs) et repos
(jours depuis le match précédent), pour chaque équipe.

Les cibles ne sont plus une classe résultat (H/D/A) mais les buts marqués
par chaque équipe (FTHG/FTAG) — voir src/model.py, qui entraîne un modèle
de buts (Poisson) dont toutes les probabilités de marché sont déduites.
"""

import numpy as np
import pandas as pd

from src import config

FEATURE_COLUMNS = [
    "home_form_pts", "home_form_gf", "home_form_ga",
    "away_form_pts", "away_form_gf", "away_form_ga",
    "diff_form_pts", "diff_form_goal_diff",
    "home_rest_days", "away_rest_days",
]


def _long_format(df: pd.DataFrame) -> pd.DataFrame:
    """Transforme le DataFrame match (1 ligne = 1 match) en DataFrame équipe
    (1 ligne = 1 équipe pour 1 match), pour pouvoir calculer une forme glissante
    par équipe indépendamment de son statut domicile/extérieur."""
    home = pd.DataFrame({
        "match_id": df.index,
        "team": df["HomeTeam"],
        "date": df["Date"],
        "is_home": True,
        "points": np.select([df["FTR"] == "H", df["FTR"] == "D"], [3, 1], default=0),
        "goals_for": df["FTHG"],
        "goals_against": df["FTAG"],
    })
    away = pd.DataFrame({
        "match_id": df.index,
        "team": df["AwayTeam"],
        "date": df["Date"],
        "is_home": False,
        "points": np.select([df["FTR"] == "A", df["FTR"] == "D"], [3, 1], default=0),
        "goals_for": df["FTAG"],
        "goals_against": df["FTHG"],
    })
    return pd.concat([home, away], ignore_index=True).sort_values(["team", "date"])


def _rolling_form(long_df: pd.DataFrame, window: int = config.FORM_WINDOW) -> pd.DataFrame:
    """Moyenne glissante des N derniers matchs, décalée d'un cran (shift) pour ne
    jamais inclure le match en cours dans sa propre feature (pas de fuite de données).
    Ajoute aussi rest_days (jours depuis le match précédent de l'équipe, plafonné à
    MAX_REST_DAYS pour absorber les trêves estivales sans exploser l'échelle)."""
    stats = ["points", "goals_for", "goals_against"]
    grouped = long_df.groupby("team")[stats]
    rolled = grouped.apply(lambda g: g.shift(1).rolling(window, min_periods=1).mean())
    rolled.columns = ["form_pts", "form_gf", "form_ga"]

    prev_date = long_df.groupby("team")["date"].shift(1)
    rest_days = (long_df["date"] - prev_date).dt.days.clip(upper=config.MAX_REST_DAYS)

    result = pd.concat([long_df.reset_index(drop=True), rolled.reset_index(drop=True)], axis=1)
    result["rest_days"] = rest_days.reset_index(drop=True)
    return result


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Construit la matrice de features X et les cibles (buts marqués à domicile,
    buts marqués à l'extérieur) à partir de l'historique brut. Le résultat H/D/A
    reste disponible via df.loc[X.index, "FTR"] pour le backtest — il n'est plus
    la cible d'entraînement (voir src/model.py)."""
    df = df.reset_index(drop=True)

    long_df = _long_format(df)
    long_df = _rolling_form(long_df)

    cols = ["form_pts", "form_gf", "form_ga", "rest_days"]
    home_stats = long_df[long_df["is_home"]].set_index("match_id")[cols]
    away_stats = long_df[~long_df["is_home"]].set_index("match_id")[cols]

    features = pd.DataFrame(index=df.index)
    features["home_form_pts"] = home_stats["form_pts"]
    features["home_form_gf"] = home_stats["form_gf"]
    features["home_form_ga"] = home_stats["form_ga"]
    features["away_form_pts"] = away_stats["form_pts"]
    features["away_form_gf"] = away_stats["form_gf"]
    features["away_form_ga"] = away_stats["form_ga"]
    features["diff_form_pts"] = features["home_form_pts"] - features["away_form_pts"]
    features["diff_form_goal_diff"] = (
        (features["home_form_gf"] - features["home_form_ga"])
        - (features["away_form_gf"] - features["away_form_ga"])
    )
    features["home_rest_days"] = home_stats["rest_days"]
    features["away_rest_days"] = away_stats["rest_days"]

    valid = features.dropna().index
    X = features.loc[valid, FEATURE_COLUMNS]
    y_home_goals = df.loc[valid, "FTHG"].astype(float)
    y_away_goals = df.loc[valid, "FTAG"].astype(float)

    return X, y_home_goals, y_away_goals


def build_features_for_match(
    history_df: pd.DataFrame, home_team: str, away_team: str, match_date=None,
) -> pd.DataFrame | None:
    """Calcule les features (forme + repos) pour UN match à venir (pas encore
    joué), à partir de l'historique connu des deux équipes. `match_date` sert à
    calculer le repos (par défaut : maintenant). Retourne None si l'une des
    deux équipes n'a pas d'historique (impossible de calculer sa forme)."""
    match_date = pd.Timestamp(match_date) if match_date is not None else pd.Timestamp.now(tz="UTC")
    if match_date.tzinfo is not None:
        match_date = match_date.tz_convert("UTC").tz_localize(None)
    long_df = _long_format(history_df).sort_values("date")

    def _team_stats(team: str) -> tuple[float, float, float, float] | None:
        team_history = long_df[long_df["team"] == team]
        if team_history.empty:
            return None

        # Garde-fou (voir config.MIN_RECENT_MATCHES) : une équipe reléguée
        # depuis longtemps ou tout juste promue a "techniquement" ses 5
        # derniers matchs connus (ci-dessous), mais trop vieux/rares pour
        # représenter l'équipe actuelle — team_history.tail() ne regarde
        # jamais leur ancienneté. Sans ce filtre, le modèle reçoit des
        # features silencieusement fausses plutôt qu'un rejet explicite.
        window_start = match_date - pd.Timedelta(days=config.MIN_RECENT_MATCHES_WINDOW_DAYS)
        recent_count = (team_history["date"] >= window_start).sum()
        if recent_count < config.MIN_RECENT_MATCHES:
            return None

        recent = team_history.tail(config.FORM_WINDOW)
        last_date = team_history["date"].iloc[-1]
        rest_days = min((match_date - last_date).days, config.MAX_REST_DAYS)
        return (
            recent["points"].mean(),
            recent["goals_for"].mean(),
            recent["goals_against"].mean(),
            rest_days,
        )

    home_stats = _team_stats(home_team)
    away_stats = _team_stats(away_team)
    if home_stats is None or away_stats is None:
        return None

    home_pts, home_gf, home_ga, home_rest = home_stats
    away_pts, away_gf, away_ga, away_rest = away_stats

    return pd.DataFrame([{
        "home_form_pts": home_pts,
        "home_form_gf": home_gf,
        "home_form_ga": home_ga,
        "away_form_pts": away_pts,
        "away_form_gf": away_gf,
        "away_form_ga": away_ga,
        "diff_form_pts": home_pts - away_pts,
        "diff_form_goal_diff": (home_gf - home_ga) - (away_gf - away_ga),
        "home_rest_days": home_rest,
        "away_rest_days": away_rest,
    }])[FEATURE_COLUMNS]
