"""
Chargement des données : historique (Football-Data.co.uk) et matchs à venir
(The Odds API si une clé est fournie, sinon fichier sample local).
"""

import io
import json
from typing import Iterable

import numpy as np
import pandas as pd
import requests

from src import config, team_names

CORE_COLUMNS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]
OUTPUT_COLUMNS = [
    "Date", "Season", "League", "HomeTeam", "AwayTeam",
    "FTHG", "FTAG", "FTR", "OddsH", "OddsD", "OddsA",
]


def generate_football_data_urls(
    leagues: Iterable[str] | None = None,
    seasons: Iterable[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Retourne la liste (league, season, url) des CSV à télécharger."""
    leagues = list(leagues) if leagues is not None else list(config.LEAGUES.keys())
    seasons = list(seasons) if seasons is not None else config.SEASONS
    return [
        (league, season, config.FOOTBALL_DATA_URL_TEMPLATE.format(season=season, league=league))
        for season in seasons
        for league in leagues
    ]


def _coalesce_odds(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute OddsH/OddsD/OddsA en prenant la première source de cotes disponible
    (par ordre de priorité défini dans config.ODDS_COLUMN_PRIORITY), colonne par colonne
    (un match peut avoir Bet365 manquant mais Pinnacle présent, etc.)."""
    odds_h = pd.Series(np.nan, index=df.index, dtype=float)
    odds_d = pd.Series(np.nan, index=df.index, dtype=float)
    odds_a = pd.Series(np.nan, index=df.index, dtype=float)

    for col_h, col_d, col_a in config.ODDS_COLUMN_PRIORITY:
        if col_h in df.columns:
            odds_h = odds_h.fillna(pd.to_numeric(df[col_h], errors="coerce"))
        if col_d in df.columns:
            odds_d = odds_d.fillna(pd.to_numeric(df[col_d], errors="coerce"))
        if col_a in df.columns:
            odds_a = odds_a.fillna(pd.to_numeric(df[col_a], errors="coerce"))

    df = df.copy()
    df["OddsH"], df["OddsD"], df["OddsA"] = odds_h, odds_d, odds_a
    return df


def _download_one_csv(league: str, season: str, url: str) -> pd.DataFrame | None:
    """Télécharge et nettoie un CSV football-data.co.uk. Retourne None si indisponible
    (saison pas encore jouée, 404, fichier vide/corrompu...) plutôt que de planter."""
    try:
        response = requests.get(url, timeout=15)
        if response.status_code != 200 or not response.content.strip():
            print(f"  - {league} {season} : indisponible (HTTP {response.status_code}), ignoré.")
            return None
        raw = pd.read_csv(io.BytesIO(response.content), encoding="latin-1", on_bad_lines="skip")
    except requests.RequestException as exc:
        print(f"  - {league} {season} : erreur réseau ({exc}), ignoré.")
        return None
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        print(f"  - {league} {season} : CSV illisible ({exc}), ignoré.")
        return None

    if not set(CORE_COLUMNS).issubset(raw.columns):
        print(f"  - {league} {season} : colonnes essentielles manquantes, ignoré.")
        return None

    raw = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTR"])
    raw["Date"] = pd.to_datetime(raw["Date"], dayfirst=True, errors="coerce")
    raw = raw.dropna(subset=["Date"])

    raw["League"] = league
    raw["Season"] = season
    raw = _coalesce_odds(raw)

    return raw.reindex(columns=OUTPUT_COLUMNS)


def download_historical_data(
    leagues: Iterable[str] | None = None,
    seasons: Iterable[str] | None = None,
    save: bool = True,
) -> pd.DataFrame:
    """Télécharge et concatène l'historique football-data.co.uk pour les ligues/saisons
    demandées, et sauvegarde le résultat dans data/historical_odds.csv."""
    urls = generate_football_data_urls(leagues, seasons)
    print(f"Téléchargement de {len(urls)} fichiers depuis football-data.co.uk...")

    frames = []
    for league, season, url in urls:
        frame = _download_one_csv(league, season, url)
        if frame is not None:
            frames.append(frame)

    if not frames:
        raise RuntimeError(
            "Aucune donnée historique n'a pu être téléchargée. "
            "Vérifie ta connexion internet et les codes ligue/saison dans config.py."
        )

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("Date").reset_index(drop=True)

    if save:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        combined.to_csv(config.HISTORICAL_DATA_PATH, index=False)
        print(f"{len(combined)} matchs sauvegardés dans {config.HISTORICAL_DATA_PATH}")

    return combined


def load_historical_data(force_refresh: bool = False) -> pd.DataFrame:
    """Charge data/historical_odds.csv s'il existe déjà, sinon télécharge tout."""
    if not force_refresh and config.HISTORICAL_DATA_PATH.exists():
        df = pd.read_csv(config.HISTORICAL_DATA_PATH, parse_dates=["Date"])
        print(f"Historique chargé depuis le cache local ({len(df)} matchs).")
        return df
    return download_historical_data()


def _load_sample_upcoming_matches() -> list[dict]:
    with open(config.UPCOMING_SAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_upcoming_matches(
    leagues: Iterable[str] | None = None,
    known_team_names: dict[str, list[str]] | None = None,
    regions: str = "eu",
    markets: str = "h2h",
) -> list[dict]:
    """Récupère les matchs à venir (toutes ligues suivies) avec leurs cotes 1X2.

    - Si ODDS_API_KEY est présente dans l'environnement, interroge The Odds API
      pour chaque ligue de `leagues` (codes football-data.co.uk, ex. "E0"/"F1").
      Les noms d'équipes sont normalisés vers la convention football-data.co.uk
      via team_names.normalize_team_name (known_team_names = {league: [noms
      connus dans l'historique]}, pour le rapprochement flou en repli).
    - Sinon (ou en cas d'erreur réseau/clé invalide/quota dépassé), retombe sur
      data/upcoming_matches_sample.json pour que le bot tourne quand même.

    Retourne une liste de dicts normalisés :
      {league, home_team, away_team, commence_time, odds_h, odds_d, odds_a}
    """
    leagues = list(leagues) if leagues is not None else list(config.LEAGUES.keys())
    known_team_names = known_team_names or {}

    if not config.ODDS_API_KEY:
        print("Pas de ODDS_API_KEY configurée : utilisation des matchs d'exemple (sample).")
        return _load_sample_upcoming_matches()

    matches = []
    for league in leagues:
        sport_key = config.ODDS_API_SPORT_KEYS.get(league, league)
        url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/odds"
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
            "apiKey": config.ODDS_API_KEY,
        }
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            events = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"Échec de l'appel à The Odds API pour {league} ({exc}), ligue ignorée.")
            continue

        known = known_team_names.get(league, [])
        for event in events:
            raw_home, raw_away = event.get("home_team"), event.get("away_team")
            odds_h, odds_d, odds_a = _extract_h2h_odds(event, raw_home, raw_away)
            if odds_h is None:
                continue
            matches.append({
                "league": league,
                "home_team": team_names.normalize_team_name(raw_home, known),
                "away_team": team_names.normalize_team_name(raw_away, known),
                "commence_time": event.get("commence_time"),
                "odds_h": odds_h,
                "odds_d": odds_d,
                "odds_a": odds_a,
            })

    if not matches:
        print("The Odds API n'a renvoyé aucun match exploitable, repli sur les matchs d'exemple.")
        return _load_sample_upcoming_matches()

    return matches


def fetch_scores(leagues: Iterable[str] | None = None, days_from: int = 3) -> list[dict]:
    """Récupère les scores des matchs récents/en cours (pour régler les paris en
    attente). Nécessite ODDS_API_KEY — retourne [] sans clé (les paris restent
    en attente jusqu'à ce qu'une clé soit configurée ; voir README).

    Retourne une liste de dicts : {league, home_team, away_team, commence_time,
    completed, home_score, away_score} (scores en int, ou None si pas encore
    joué/terminé). Les noms d'équipes NE sont PAS normalisés ici : c'est fait
    par l'appelant, qui connaît les noms déjà en base pour ce match (issus de
    fetch_upcoming_matches au moment de la mise)."""
    if not config.ODDS_API_KEY:
        return []

    leagues = list(leagues) if leagues is not None else list(config.LEAGUES.keys())
    results = []
    for league in leagues:
        sport_key = config.ODDS_API_SPORT_KEYS.get(league, league)
        url = f"{config.ODDS_API_BASE_URL}/sports/{sport_key}/scores"
        params = {"daysFrom": days_from, "apiKey": config.ODDS_API_KEY}
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            events = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"Échec de l'appel scores The Odds API pour {league} ({exc}), ligue ignorée.")
            continue

        for event in events:
            home_score, away_score = None, None
            for score in event.get("scores") or []:
                if score.get("name") == event.get("home_team"):
                    home_score = _safe_int(score.get("score"))
                elif score.get("name") == event.get("away_team"):
                    away_score = _safe_int(score.get("score"))
            results.append({
                "league": league,
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "commence_time": event.get("commence_time"),
                "completed": bool(event.get("completed")),
                "home_score": home_score,
                "away_score": away_score,
            })

    return results


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_h2h_odds(event: dict, home_team: str, away_team: str):
    """Moyenne les cotes 1X2 de tous les bookmakers renvoyés par The Odds API pour un événement."""
    home_prices, draw_prices, away_prices = [], [], []
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                if outcome["name"] == home_team:
                    home_prices.append(outcome["price"])
                elif outcome["name"] == away_team:
                    away_prices.append(outcome["price"])
                elif outcome["name"].lower() == "draw":
                    draw_prices.append(outcome["price"])

    if not home_prices or not away_prices:
        return None, None, None

    odds_d = float(np.mean(draw_prices)) if draw_prices else None
    return float(np.mean(home_prices)), odds_d, float(np.mean(away_prices))
