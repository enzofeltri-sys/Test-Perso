"""
Construction des features à partir de l'historique brut :
forme récente (points, buts marqués/encaissés sur les N derniers matchs),
domicile/extérieur, ligue.
"""

import numpy as np
import pandas as pd

from src import config

FEATURE_COLUMNS = [
    "home_form_pts", "home_form_gf", "home_form_ga",
    "away_form_pts", "away_form_gf", "away_form_ga",
    "diff_form_pts", "diff_form_goal_diff",
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
    jamais inclure le match en cours dans sa propre feature (pas de fuite de données)."""
    stats = ["points", "goals_for", "goals_against"]
    grouped = long_df.groupby("team")[stats]
    rolled = grouped.apply(lambda g: g.shift(1).rolling(window, min_periods=1).mean())
    rolled.columns = ["form_pts", "form_gf", "form_ga"]
    return pd.concat([long_df.reset_index(drop=True), rolled.reset_index(drop=True)], axis=1)


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Construit la matrice de features X et la cible y (0=H, 1=D, 2=A) à partir
    de l'historique brut. Retourne aussi les colonnes méta (Date, League, équipes,
    cotes) alignées sur X pour la simulation, via df.loc[X.index]."""
    df = df.reset_index(drop=True)

    long_df = _long_format(df)
    long_df = _rolling_form(long_df)

    home_form = long_df[long_df["is_home"]].set_index("match_id")[["form_pts", "form_gf", "form_ga"]]
    away_form = long_df[~long_df["is_home"]].set_index("match_id")[["form_pts", "form_gf", "form_ga"]]

    features = pd.DataFrame(index=df.index)
    features["home_form_pts"] = home_form["form_pts"]
    features["home_form_gf"] = home_form["form_gf"]
    features["home_form_ga"] = home_form["form_ga"]
    features["away_form_pts"] = away_form["form_pts"]
    features["away_form_gf"] = away_form["form_gf"]
    features["away_form_ga"] = away_form["form_ga"]
    features["diff_form_pts"] = features["home_form_pts"] - features["away_form_pts"]
    features["diff_form_goal_diff"] = (
        (features["home_form_gf"] - features["home_form_ga"])
        - (features["away_form_gf"] - features["away_form_ga"])
    )

    valid = features.dropna().index
    X = features.loc[valid, FEATURE_COLUMNS]
    y = df.loc[valid, "FTR"].map(config.RESULT_TO_CLASS)

    return X, y


def build_features_for_match(history_df: pd.DataFrame, home_team: str, away_team: str) -> pd.DataFrame | None:
    """Calcule les features de forme pour UN match à venir (pas encore joué), à
    partir de l'historique connu des deux équipes. Retourne None si l'une des deux
    équipes n'a pas d'historique (impossible de calculer sa forme)."""
    long_df = _long_format(history_df).sort_values("date")

    def _team_form(team: str) -> tuple[float, float, float] | None:
        team_history = long_df[long_df["team"] == team].tail(config.FORM_WINDOW)
        if team_history.empty:
            return None
        return (
            team_history["points"].mean(),
            team_history["goals_for"].mean(),
            team_history["goals_against"].mean(),
        )

    home_stats = _team_form(home_team)
    away_stats = _team_form(away_team)
    if home_stats is None or away_stats is None:
        return None

    home_pts, home_gf, home_ga = home_stats
    away_pts, away_gf, away_ga = away_stats

    return pd.DataFrame([{
        "home_form_pts": home_pts,
        "home_form_gf": home_gf,
        "home_form_ga": home_ga,
        "away_form_pts": away_pts,
        "away_form_gf": away_gf,
        "away_form_ga": away_ga,
        "diff_form_pts": home_pts - away_pts,
        "diff_form_goal_diff": (home_gf - home_ga) - (away_gf - away_ga),
    }])[FEATURE_COLUMNS]
