"""
Configuration centrale du bot de paris virtuels.

Toutes les constantes "métier" (ligues, saisons, seuils, bankroll...) vivent
ici pour que le reste du code n'ait jamais de valeur en dur.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Chemins
# --------------------------------------------------------------------------
# Le bot tourne sur Render (disque éphémère) : aucun fichier local n'est
# persistant d'un cycle à l'autre. Seul le sample de secours reste sur
# disque (versionné avec le code) ; tout le reste (historique, modèle,
# paris) vit dans Supabase — voir src/supabase_state.py.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPCOMING_SAMPLE_PATH = DATA_DIR / "upcoming_matches_sample.json"

# --------------------------------------------------------------------------
# Football-Data.co.uk (historique + cotes)
# --------------------------------------------------------------------------
FOOTBALL_DATA_URL_TEMPLATE = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"

# Code ligue (football-data.co.uk) -> nom lisible
LEAGUES = {
    "E0": "Premier League",
    "F1": "Ligue 1",
}

# Saisons au format attendu par football-data.co.uk : "1819" = saison 2018-19
SEASON_START_YEAR = 2018
SEASON_END_YEAR = 2025  # génère jusqu'à la saison 2025-26 incluse


def _season_code(start_year: int) -> str:
    end_year = (start_year + 1) % 100
    return f"{start_year % 100:02d}{end_year:02d}"


SEASONS = [_season_code(y) for y in range(SEASON_START_YEAR, SEASON_END_YEAR + 1)]

# Saison à partir de laquelle on bascule en test (les saisons antérieures servent
# à l'entraînement). "2425" = saison 2024-25.
TEST_SEASON_START = "2425"

# Colonnes de cotes 1X2 par ordre de priorité (on prend la première dispo par match)
ODDS_COLUMN_PRIORITY = [
    ("B365H", "B365D", "B365A"),  # Bet365
    ("PSH", "PSD", "PSA"),        # Pinnacle
    ("WHH", "WHD", "WHA"),        # William Hill
    ("VCH", "VCD", "VCA"),        # BetVictor
    ("IWH", "IWD", "IWA"),        # Interwetten
    ("AvgH", "AvgD", "AvgA"),     # Moyenne du marché (saisons récentes)
]

# --------------------------------------------------------------------------
# The Odds API (cotes à venir, optionnel)
# --------------------------------------------------------------------------
ODDS_API_KEY = os.getenv("ODDS_API_KEY") or None
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# Clés de sport The Odds API pour nos deux ligues (voir /v4/sports pour la liste complète)
ODDS_API_SPORT_KEYS = {
    "E0": "soccer_epl",
    "F1": "soccer_france_ligue_one",
}

# --------------------------------------------------------------------------
# Feature engineering
# --------------------------------------------------------------------------
FORM_WINDOW = 5  # nombre de matchs récents pris en compte pour la forme

# --------------------------------------------------------------------------
# Modèle
# --------------------------------------------------------------------------
RANDOM_STATE = 42
# Mapping résultat -> classe utilisé PARTOUT (features, modèle, stratégie, simulation)
RESULT_TO_CLASS = {"H": 0, "D": 1, "A": 2}
CLASS_TO_RESULT = {v: k for k, v in RESULT_TO_CLASS.items()}

# --------------------------------------------------------------------------
# Stratégie de paris
# --------------------------------------------------------------------------
INITIAL_BANKROLL = 1000.0
EV_THRESHOLD = 0.05          # on ne parie que si EV > 5%
KELLY_MULTIPLIER = 0.25      # fraction de Kelly (0.25 = "quart de Kelly", plus prudent)
MAX_STAKE_FRACTION = 0.05    # garde-fou : jamais plus de 5% de la bankroll sur un seul pari

# --------------------------------------------------------------------------
# Throttle des appels à The Odds API (free tier = 500 crédits/mois ; avec 2
# ligues et ces intervalles, ~4 appels/jour max = ~120/mois, large marge)
# --------------------------------------------------------------------------
ODDS_FETCH_INTERVAL_HOURS = 20   # nouveaux matchs à venir : au plus 1x/jour/ligue
SCORES_FETCH_INTERVAL_HOURS = 12  # règlement des paris en attente : 2x/jour max
RESULT_SETTLE_BUFFER_HOURS = 3   # on ne tente de régler un match que 3h après son coup d'envoi
