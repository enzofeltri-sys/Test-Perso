"""
Modèle prédictif 1X2 : régression logistique multinomiale sur les features
de forme (src/features.py). Entraîné par retrain.py (GitHub Actions), le
modèle est sérialisé en base64 et stocké dans Supabase (footballbot_model)
— jamais sur le disque de Render, éphémère.
"""

import base64
import pickle

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config


def train_model(X: pd.DataFrame, y: pd.Series) -> Pipeline:
    """Entraîne une régression logistique multinomiale (classes 0=H, 1=D, 2=A)
    sur des features standardisées."""
    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("classifier", LogisticRegression(
            # lbfgs gère nativement le multinomial pour >2 classes depuis
            # scikit-learn >= 1.5 (le paramètre multi_class est supprimé,
            # plus besoin de le forcer).
            solver="lbfgs",
            max_iter=1000,
            random_state=config.RANDOM_STATE,
        )),
    ])
    pipeline.fit(X, y)
    return pipeline


def predict_proba(model: Pipeline, X: pd.DataFrame) -> pd.DataFrame:
    """Retourne les probabilités [H, D, A] pour chaque ligne de X, dans cet
    ordre de colonnes quel que soit l'ordre interne des classes du modèle."""
    raw = model.predict_proba(X)
    class_order = list(model.named_steps["classifier"].classes_)
    probs = pd.DataFrame(raw, columns=class_order, index=X.index)
    return probs[[0, 1, 2]].rename(columns={0: "H", 1: "D", 2: "A"})


def serialize_model(model: Pipeline) -> str:
    """Sérialise le pipeline entraîné en base64 (texte), pour stockage dans
    une colonne Supabase — pas de fichier local persistant sur Render."""
    return base64.b64encode(pickle.dumps(model)).decode("ascii")


def deserialize_model(model_b64: str) -> Pipeline:
    return pickle.loads(base64.b64decode(model_b64))
