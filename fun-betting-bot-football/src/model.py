"""
Modèle prédictif : buts marqués par chaque équipe (deux régressions de
Poisson indépendantes, domicile et extérieur), entraîné sur les features
de forme + repos (src/features.py).

Pourquoi un modèle de buts plutôt qu'un classifieur 1X2 : une fois qu'on
a le nombre de buts ATTENDU de chaque équipe, TOUTES les probabilités de
marché (1X2, double chance, over/under, résultat+buts...) se déduisent
d'une seule grille de Poisson jointe — voir src/markets.py. Un seul
modèle cohérent, plutôt qu'empiler un classifieur par marché.

Entraîné par retrain.py (GitHub Actions ou /admin/retrain), le modèle est
sérialisé en base64 et stocké dans Supabase (footballbot_model) — jamais
sur le disque de Render, éphémère.
"""

import base64
import pickle

import numpy as np
import pandas as pd
from sklearn.linear_model import PoissonRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config


def _build_pipeline() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("regressor", PoissonRegressor(alpha=1.0, max_iter=500)),
    ])


def train_model(X: pd.DataFrame, y_home_goals: pd.Series, y_away_goals: pd.Series) -> dict:
    """Entraîne deux régressions de Poisson indépendantes (buts domicile,
    buts extérieur) sur les mêmes features — chacune apprend elle-même
    comment pondérer forme et repos pour son propre camp (ex : le repos de
    l'équipe à domicile peut compter différemment pour ses propres buts que
    pour ceux de l'adversaire)."""
    home_model = _build_pipeline().fit(X, y_home_goals)
    away_model = _build_pipeline().fit(X, y_away_goals)
    return {"home": home_model, "away": away_model, "random_state": config.RANDOM_STATE}


def predict_goal_rates(model: dict, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Retourne (lambda_home, lambda_away) : le nombre de buts ATTENDU par
    chaque équipe pour chaque ligne de X (jamais négatif, Poisson oblige)."""
    lambda_home = np.clip(model["home"].predict(X), 1e-6, None)
    lambda_away = np.clip(model["away"].predict(X), 1e-6, None)
    return lambda_home, lambda_away


def serialize_model(model: dict) -> str:
    """Sérialise le modèle (dict de 2 pipelines sklearn) en base64, pour
    stockage dans une colonne Supabase — pas de fichier local persistant
    sur Render."""
    return base64.b64encode(pickle.dumps(model)).decode("ascii")


def deserialize_model(model_b64: str) -> dict:
    return pickle.loads(base64.b64decode(model_b64))
