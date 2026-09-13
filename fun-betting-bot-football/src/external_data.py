"""
Sources de données optionnelles, AFFICHAGE UNIQUEMENT :

- API-Football (blessures) — nécessite API_FOOTBALL_KEY.
- football-data.org (calendrier Ligue des Champions/Europa, pour repérer
  un match européen récent) — nécessite FOOTBALL_DATA_ORG_KEY.

Aucune des deux ne peut ENTRAÎNER le modèle : il n'existe pas d'historique
de blessures ni de calendrier européen passé exploitable ici, donc pas de
coefficient appris possible (voir src/model.py, entraîné uniquement sur
football-data.co.uk). Ces fonctions servent uniquement à enrichir le
journal (voir web_app._describe_match_context) pour une lecture humaine —
c'est TOI qui juges, jamais un ajustement automatique des paris.

⚠️ Schémas de réponse non vérifiés en direct (réseau restreint au moment
de l'écriture) — tout est défensif : une erreur ou un format inattendu
renvoie None/[] plutôt que de faire planter un cycle. À confirmer une
fois déployé (voir DEPLOIEMENT.md).
"""

from datetime import datetime, timedelta, timezone

import requests

from src import config, supabase_state


def _api_football_headers() -> dict:
    return {"x-apisports-key": config.API_FOOTBALL_KEY}


def resolve_api_football_team_id(team_name: str) -> int | None:
    """Id API-Football d'une équipe, par recherche sur son nom — mis en
    cache (footballbot_team_refs) puisqu'un id ne change jamais."""
    if not config.API_FOOTBALL_KEY:
        return None

    cached = supabase_state.get_team_ref(team_name)
    if cached.get("api_football_id") is not None:
        return cached["api_football_id"]

    try:
        resp = requests.get(
            f"{config.API_FOOTBALL_BASE_URL}/teams",
            headers=_api_football_headers(), params={"name": team_name}, timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("response") or []
        if not results:
            return None
        team_id = results[0]["team"]["id"]
        supabase_state.save_team_ref(team_name, api_football_id=team_id)
        return team_id
    except Exception:
        return None


def fetch_injury_count(team_name: str) -> int | None:
    """Nombre de joueurs actuellement listés comme blessés pour cette
    équipe (API-Football, saison en cours). None si indisponible (pas de
    clé, équipe non trouvée, erreur réseau...) — distinct de 0, qui
    laisserait croire "aucun blessé confirmé". Résultat mis en cache
    (config.INJURY_CACHE_HOURS) pour économiser le quota gratuit
    (~100 requêtes/jour)."""
    if not config.API_FOOTBALL_KEY:
        return None

    cached = supabase_state.get_team_ref(team_name)
    checked_at = cached.get("injury_checked_at")
    if checked_at:
        try:
            checked_dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - checked_dt).total_seconds() < config.INJURY_CACHE_HOURS * 3600:
                return cached.get("injury_count")
        except ValueError:
            pass

    team_id = resolve_api_football_team_id(team_name)
    if team_id is None:
        return None

    try:
        resp = requests.get(
            f"{config.API_FOOTBALL_BASE_URL}/injuries",
            headers=_api_football_headers(),
            params={"team": team_id, "season": datetime.now().year}, timeout=10,
        )
        resp.raise_for_status()
        count = len(resp.json().get("response") or [])
        supabase_state.save_team_ref(
            team_name, injury_count=count, injury_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        return count
    except Exception:
        return None


def fetch_recent_european_matches(days_back: int = None) -> list[dict]:
    """Matchs de Ligue des Champions/Europa des derniers jours (toutes
    équipes confondues) — [] si pas de clé ou erreur. Un seul appel par
    compétition suivie (config.EURO_COMPETITION_CODES), pas un par équipe.

    Retourne [{"home": nom_football_data_org, "away": nom_football_data_org,
    "date": iso_date}, ...]. Les noms sont ceux de football-data.org (ex:
    "Arsenal FC"), PAS ceux de football-data.co.uk — voir
    `played_in_europe_recently` pour le rapprochement tolérant."""
    if not config.FOOTBALL_DATA_ORG_KEY:
        return []

    days_back = days_back or config.EURO_LOOKBACK_DAYS
    date_to = datetime.now(timezone.utc).date()
    date_from = date_to - timedelta(days=days_back)
    headers = {"X-Auth-Token": config.FOOTBALL_DATA_ORG_KEY}

    matches = []
    for code in config.EURO_COMPETITION_CODES:
        try:
            resp = requests.get(
                f"{config.FOOTBALL_DATA_ORG_BASE_URL}/competitions/{code}/matches",
                headers=headers,
                params={"dateFrom": date_from.isoformat(), "dateTo": date_to.isoformat(), "status": "FINISHED"},
                timeout=10,
            )
            resp.raise_for_status()
            for m in resp.json().get("matches") or []:
                home = (m.get("homeTeam") or {}).get("name")
                away = (m.get("awayTeam") or {}).get("name")
                if home and away:
                    matches.append({"home": home, "away": away, "date": m.get("utcDate")})
        except Exception:
            continue

    return matches


def played_in_europe_recently(team_name: str, european_matches: list[dict]) -> str | None:
    """Date (ISO) du dernier match européen trouvé pour `team_name` parmi
    `european_matches` (voir fetch_recent_european_matches), ou None.
    Rapprochement tolérant par sous-chaîne (football-data.co.uk dit
    "Arsenal", football-data.org dit "Arsenal FC") plutôt qu'un mapping
    exact — non vérifié en direct, à confirmer une fois déployé."""
    for m in european_matches:
        if team_name in m["home"] or team_name in m["away"] or m["home"] in team_name or m["away"] in team_name:
            return m["date"]
    return None
