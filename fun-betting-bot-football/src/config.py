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
MAX_REST_DAYS = 30  # plafond du nombre de jours de repos pris en compte (trêve estivale, etc.)

# --------------------------------------------------------------------------
# Modèle — buts (Poisson)
# --------------------------------------------------------------------------
RANDOM_STATE = 42
# Mapping résultat -> classe, encore utilisé pour lire FTR et pour les
# libellés de sélection 1X2 (H/D/A) dans src/markets.py.
RESULT_TO_CLASS = {"H": 0, "D": 1, "A": 2}
CLASS_TO_RESULT = {v: k for k, v in RESULT_TO_CLASS.items()}

GOALS_GRID_MAX = 10  # troncature de la grille de Poisson jointe (au-delà, probabilité négligeable)
TOTAL_GOALS_LINES = [0.5, 1.5, 2.5]  # lignes over/under supportées
# Ligne utilisée pour le marché composé "résultat + total de buts" (le plus
# courant chez les bookmakers pour ce type de marché) — voir src/markets.py.
RESULT_AND_GOALS_LINE = 2.5

# --------------------------------------------------------------------------
# Marchés / cotes
# --------------------------------------------------------------------------
# Le 1X2 vient toujours d'une vraie cote (The Odds API). Double chance et
# over/under sont tentés en vrai (marchés "double_chance"/"totals" de The
# Odds API, non vérifiés en direct faute d'accès réseau au moment de
# l'écriture) avec repli automatique par ligne/événement sur une cote
# ESTIMÉE si le marché n'est pas renvoyé. "Résultat + buts" n'existe pas
# comme marché standard chez The Odds API : toujours estimé.
# Cote estimée = cote équitable (1/proba du modèle) à laquelle on applique
# une marge bookmaker typique — ce qui l'empêche STRUCTURELLEMENT d'avoir
# une EV positive (le bot ne peut pas "battre" sa propre estimation). Ces
# cotes sont donc affichées pour comprendre les gains/pertes, jamais pour
# parier dessus si aucune vraie cote n'est disponible.
ESTIMATED_ODDS_MARGIN = 1.05

# --------------------------------------------------------------------------
# Stratégie de paris
# --------------------------------------------------------------------------
INITIAL_BANKROLL = 1000.0
# Seuils volontairement conservateurs : un premier backtest avec un modèle
# non calibré et un seuil à 5% a perdu ~99% de la bankroll en pariant sur
# 87% des matchs. La calibration corrige la cause première, ces seuils
# sont une deuxième ligne de défense — un modèle à quelques features de
# forme ne mérite pas une confiance illimitée.
SINGLE_EV_THRESHOLD = 0.10       # pari seul : EV > 10%
COMBO_LEG_EV_THRESHOLD = 0.15    # chaque jambe d'un combiné doit être plus solide qu'un pari seul
MAX_COMBO_LEGS = 3               # jamais plus de 3 matchs dans un même pari combiné
KELLY_MULTIPLIER = 0.15          # fraction de Kelly (0.15, plus prudent que le "quart de Kelly" classique)
MAX_STAKE_FRACTION = 0.03        # garde-fou : jamais plus de 3% de la bankroll sur un seul pari/combiné

# Coupe-circuit : si la bankroll tombe sous cette fraction du capital de
# départ, le bot arrête de PARIER (il continue à régler les paris déjà en
# cours) — dernier filet si le modèle s'avère quand même mauvais malgré la
# calibration. Se lève automatiquement si la bankroll remonte au-dessus.
DRAWDOWN_STOP_FRACTION = 0.20

# --------------------------------------------------------------------------
# Throttle des appels à The Odds API (free tier = 500 crédits/mois)
# --------------------------------------------------------------------------
ODDS_FETCH_INTERVAL_HOURS = 20   # nouveaux matchs à venir : au plus 1x/jour/ligue
SCORES_FETCH_INTERVAL_HOURS = 12  # règlement des paris en attente : 2x/jour max
RESULT_SETTLE_BUFFER_HOURS = 3   # on ne tente de régler un match que 3h après son coup d'envoi
# Filet de sécurité indépendant de toute estimation de coût par marché :
# The Odds API renvoie le quota restant dans l'en-tête de chaque réponse
# (x-requests-remaining). Sous ce seuil, on arrête tout nouvel appel
# jusqu'au mois suivant plutôt que de risquer un dépassement.
ODDS_API_MIN_REMAINING_CREDITS = 20
