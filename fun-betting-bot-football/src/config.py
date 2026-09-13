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

# --------------------------------------------------------------------------
# Sources optionnelles, AFFICHAGE UNIQUEMENT (voir src/external_data.py) —
# blessures et calendrier européen. Aucune des deux n'entraîne le modèle :
# pas d'historique exploitable pour ces signaux, donc pas de coefficient
# appris possible. Elles enrichissent le journal pour une lecture humaine,
# jamais un ajustement automatique des paris.
# --------------------------------------------------------------------------
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY") or None
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
INJURY_CACHE_HOURS = 24  # évite de re-consommer le quota gratuit (~100/jour) à chaque cycle
# Filet de sécurité indépendant de tout calcul de coût par endpoint :
# API-Football renvoie le quota JOURNALIER restant dans l'en-tête
# x-ratelimit-requests-remaining à CHAQUE réponse. Sous ce seuil, on
# arrête tout nouvel appel jusqu'au lendemain (voir save_api_football_remaining).
API_FOOTBALL_MIN_REMAINING = 5
# Le free tier limite aussi à 10 requêtes/MINUTE (en plus du quota
# journalier) — la dépasser de façon répétée peut bloquer la clé
# temporairement ou définitivement. Un cycle s'exécute en quelques
# secondes (largement dans la même fenêtre d'une minute), donc on
# plafonne le nombre d'appels API-Football par cycle bien en dessous de
# cette limite plutôt que de suivre un compteur glissant complexe.
API_FOOTBALL_MAX_CALLS_PER_CYCLE = 6
# IDs de compétition API-Football pour la coupe d'Europe — usuels mais non
# vérifiés en direct (réseau restreint au moment de l'écriture) : à
# confirmer une fois déployé (le journal loggue les ids rencontrés sur
# repli en cas de doute — voir src/external_data.py).
API_FOOTBALL_UEFA_LEAGUE_IDS = {2: "Champions League", 3: "Europa League", 848: "Conference League"}

FOOTBALL_DATA_ORG_KEY = os.getenv("FOOTBALL_DATA_ORG_KEY") or None
FOOTBALL_DATA_ORG_BASE_URL = "https://api.football-data.org/v4"
# Codes compétition football-data.org — non vérifiés en direct (réseau
# restreint au moment de l'écriture) : à confirmer une fois déployé.
EURO_COMPETITION_CODES = ["CL", "EL"]
EURO_LOOKBACK_DAYS = 5  # fenêtre pour détecter un match européen récent

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

# Garde-fou : une équipe reléguée depuis 2+ saisons (ex: Troyes, plus en
# Ligue 1 depuis 2022-23) ou tout juste promue (ex: Sunderland, ~20 matchs
# élite au total) a "techniquement" les 5 matchs requis par FORM_WINDOW —
# via features.build_features_for_match, qui prend les 5 DERNIERS matchs
# connus quelle que soit leur ancienneté — mais ces matchs sont trop vieux
# ou trop peu nombreux pour représenter l'équipe actuelle. Ça produit des
# features silencieusement fausses plutôt qu'un rejet ("Historique
# insuffisant"), et le modèle en tire des probabilités aberrantes (EV >
# +200% déjà observé en prod). Exige au moins MIN_RECENT_MATCHES matchs
# dans l'élite (config.LEAGUES) sur les MIN_RECENT_MATCHES_WINDOW_DAYS
# derniers jours ; sinon le match est ignoré comme un historique
# insuffisant classique.
MIN_RECENT_MATCHES = 10
MIN_RECENT_MATCHES_WINDOW_DAYS = 365

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
# Fenêtre de matchs évalués
# --------------------------------------------------------------------------
# The Odds API peut renvoyer des matchs à venir bien au-delà de quelques
# jours ; sans limite, CHAQUE match (donc chaque équipe) déclenche
# l'enrichissement blessures/coupe d'Europe (src/external_data.py, quota
# API-Football limité + appels Supabase), ce qui gonfle inutilement la
# consommation de quota et le temps du cycle pour des matchs encore
# lointains (voir web_app._place_new_bets, qui a déjà planté une fois par
# accumulation de ces appels). Se limiter aux matchs à J+3 maximum : la
# fenêtre "glisse" à chaque fetch (throttlé, voir ODDS_FETCH_INTERVAL_HOURS)
# donc un match plus lointain finit par y entrer de lui-même, plus près de
# son coup d'envoi — où les infos blessures sont de toute façon plus fiables
# (compos pas encore connues plusieurs jours à l'avance).
UPCOMING_MATCH_WINDOW_DAYS = 3

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
