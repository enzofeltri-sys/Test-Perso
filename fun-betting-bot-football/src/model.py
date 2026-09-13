"""
Modèle prédictif 1X2 : régression logistique multinomiale sur les features
de forme (src/features.py). Entraîné par retrain.py (GitHub Actions), le
modèle est sérialisé en base64 et stocké dans Supabase (footballbot_model)
— jamais sur le disque de Render, éphémère.
"""

import base64
import pickle

import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src import config


def train_model(X: pd.DataFrame, y: pd.Series) -> Pipeline:
    """Entraîne une régression logistique multinomiale (classes 0=H, 1=D, 2=A)
    sur des features standardisées, PUIS calibre ses probabilités
    (CalibratedClassifierCV, isotonic, 5-fold).

    La calibration n'est pas cosmétique ici : avec seulement 8 features de
    forme, une régression logistique brute est structurellement
    surconfiante (elle sort des probabilités proches de 0/1 sans avoir
    vraiment l'information pour ça). Or la stratégie EV mise justement sur
    les écarts entre proba du modèle et cote du marché — un modèle
    surconfiant "trouve" alors de la fausse valeur presque partout et perd
    massivement contre des cotes de marché qui, elles, sont efficientes.
    Un premier backtest sans calibration a perdu ~99% de la bankroll
    (EV positive détectée sur 87% des matchs, win rate réel de 25% sur ces
    paris) — signature typique d'une surconfiance non corrigée, pas d'un
    "vrai" edge. La calibration ne garantit pas un edge positif (bien
    prédire le foot avec 8 features est difficile, point) mais évite que
    le bot confonde bruit de modèle et opportunité de marché."""
    base_classifier = LogisticRegression(
        # lbfgs gère nativement le multinomial pour >2 classes depuis
        # scikit-learn >= 1.5 (le paramètre multi_class est supprimé,
        # plus besoin de le forcer).
        solver="lbfgs",
        max_iter=1000,
        random_state=config.RANDOM_STATE,
    )
    calibrated_classifier = CalibratedClassifierCV(base_classifier, method="isotonic", cv=5)
    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("classifier", calibrated_classifier),
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
