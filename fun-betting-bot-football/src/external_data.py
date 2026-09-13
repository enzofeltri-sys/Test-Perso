"""
Sources de données optionnelles, AFFICHAGE UNIQUEMENT :

- API-Football (blessures + calendrier Ligue des Champions/Europa) —
  nécessite API_FOOTBALL_KEY. Base URL, auth (x-apisports-key), structure
  de réponse ({get, parameters, errors, results, paging, response}) et
  en-têtes de quota (x-ratelimit-requests-remaining par jour,
  X-Ratelimit-Remaining par minute) conformes à la doc officielle.
- football-data.org (calendrier européen, second avis) — nécessite
  FOOTBALL_DATA_ORG_KEY. Rapprochement par sous-chaîne (pas d'id), moins
  fiable qu'API-Football mais indépendant.

Aucune des deux ne peut ENTRAÎNER le modèle : il n'existe pas d'historique
de blessures ni de calendrier européen passé exploitable ici, donc pas de
coefficient appris possible (voir src/model.py, entraîné uniquement sur
football-data.co.uk). Ces fonctions servent uniquement à enrichir le
journal (voir web_app._describe_match_context) pour une lecture humaine —
c'est TOI qui juges, jamais un ajustement automatique des paris.

⚠️ Les IDs de ligue UEFA (config.API_FOOTBALL_UEFA_LEAGUE_IDS) et le
rapprochement de noms football-data.org sont non vérifiés en direct
(réseau restreint au moment de l'écriture) — à confirmer une fois déployé.
"""

from datetime import datetime, timedelta, timezone

import requests

from src import config, supabase_state


def _api_football_headers() -> dict:
    return {"x-apisports-key": config.API_FOOTBALL_KEY}


def _api_football_quota_ok() -> bool:
    remaining = supabase_state.get_api_football_remaining()
    return remaining is None or remaining >= config.API_FOOTBALL_MIN_REMAINING


# Plafond d'appels API-Football pour le cycle EN COURS (voir
# reset_call_budget) — protège la limite de 10 requêtes/MINUTE du free
# tier, distincte du quota journalier suivi via Supabase. Réinitialisé une
# fois par cycle par web_app._place_new_bets, jamais persisté (la fenêtre
# d'une minute d'API-Football n'a pas besoin d'être suivie entre deux
# cycles, largement espacés par le throttle ODDS_FETCH_INTERVAL_HOURS).
_call_budget = {"remaining": None}

# Cache mémoire de footballbot_team_refs pour la durée du cycle EN COURS.
# resolve_api_football_team_id, fetch_injury_count et fetch_recent_uefa_fixture
# lisent chacune la même ligne pour une même équipe — jusqu'à 3 lectures
# Supabase redondantes par équipe sans ce cache, qui s'ajoutaient au budget
# de temps global du cycle (voir web_app._place_new_bets) au point
# d'empêcher tout appel API-Football d'aboutir sur les toutes premières
# équipes traitées. Réinitialisé avec _call_budget par reset_call_budget().
_team_ref_cache: dict = {}


def reset_call_budget(max_calls: int = None) -> None:
    """À appeler une fois par cycle, avant toute autre fonction de ce
    module (voir web_app._place_new_bets)."""
    _call_budget["remaining"] = max_calls if max_calls is not None else config.API_FOOTBALL_MAX_CALLS_PER_CYCLE
    _team_ref_cache.clear()


def _get_team_ref(team_name: str) -> dict:
    if team_name not in _team_ref_cache:
        _team_ref_cache[team_name] = supabase_state.get_team_ref(team_name)
    return _team_ref_cache[team_name]


def _save_team_ref(team_name: str, **fields) -> None:
    supabase_state.save_team_ref(team_name, **fields)
    _team_ref_cache[team_name] = {**_team_ref_cache.get(team_name, {}), **fields}


def _api_football_get(path: str, params: dict) -> list | None:
    """GET générique vers API-Football : vérifie le quota journalier ET le
    budget d'appels du cycle AVANT l'appel, suit x-ratelimit-requests-remaining
    APRÈS, et vérifie le champ `errors` de la réponse (non vide = erreur
    métier même en HTTP 200, d'après la doc officielle) avant de faire
    confiance à `response`. None sur tout problème (quota, budget, réseau,
    erreur API) plutôt que de planter l'appelant."""
    if not config.API_FOOTBALL_KEY or not _api_football_quota_ok():
        return None
    if _call_budget["remaining"] is not None and _call_budget["remaining"] <= 0:
        return None

    try:
        _call_budget["remaining"] = (
            _call_budget["remaining"] - 1 if _call_budget["remaining"] is not None else None
        )
        resp = requests.get(
            f"{config.API_FOOTBALL_BASE_URL}/{path}",
            headers=_api_football_headers(), params=params, timeout=6,
        )
        resp.raise_for_status()

        remaining_header = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining_header is not None:
            try:
                supabase_state.save_api_football_remaining(int(remaining_header))
            except ValueError:
                pass

        data = resp.json()
        errors = data.get("errors")
        if errors:
            return None
        return data.get("response") or []
    except Exception:
        return None


def resolve_api_football_team_id(team_name: str) -> int | None:
    """Id API-Football d'une équipe, par recherche sur son nom — mis en
    cache (footballbot_team_refs) puisqu'un id ne change jamais (donnée de
    référence, voir doc officielle : "à appeler une fois, puis cacher")."""
    if not config.API_FOOTBALL_KEY:
        return None

    cached = _get_team_ref(team_name)
    if cached.get("api_football_id") is not None:
        return cached["api_football_id"]

    results = _api_football_get("teams", {"name": team_name})
    if not results:
        return None

    team_id = results[0]["team"]["id"]
    _save_team_ref(team_name, api_football_id=team_id)
    return team_id


def fetch_injury_count(team_name: str) -> int | None:
    """Nombre de joueurs actuellement listés comme blessés pour cette
    équipe (saison en cours). None si indisponible (pas de clé, quota
    épuisé, équipe non trouvée, erreur...) — distinct de 0, qui laisserait
    croire "aucun blessé confirmé". Mis en cache (config.INJURY_CACHE_HOURS)."""
    if not config.API_FOOTBALL_KEY:
        return None

    cached = _get_team_ref(team_name)
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

    results = _api_football_get("injuries", {"team": team_id, "season": datetime.now().year})
    if results is None:
        return None

    count = len(results)
    _save_team_ref(
        team_name, injury_count=count, injury_checked_at=datetime.now(timezone.utc).isoformat(),
    )
    return count


def fetch_recent_uefa_fixture(team_name: str, days_back: int = None) -> str | None:
    """Date ISO du dernier match européen (Ligue des Champions/Europa/
    Conference) de cette équipe dans les `days_back` derniers jours, via
    API-Football (`/fixtures?team=<id>&last=5`, filtré sur `league.id` —
    voir config.API_FOOTBALL_UEFA_LEAGUE_IDS), ou None. Plus fiable que le
    rapprochement par nom (football-data.org) puisque basé sur l'id
    d'équipe déjà résolu pour les blessures — pas de requête en plus pour
    l'id, seulement pour les fixtures."""
    if not config.API_FOOTBALL_KEY:
        return None

    team_id = resolve_api_football_team_id(team_name)
    if team_id is None:
        return None

    results = _api_football_get("fixtures", {"team": team_id, "last": 5})
    if not results:
        return None

    days_back = days_back or config.EURO_LOOKBACK_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    for fixture in results:
        league_id = (fixture.get("league") or {}).get("id")
        if league_id not in config.API_FOOTBALL_UEFA_LEAGUE_IDS:
            continue
        date_str = (fixture.get("fixture") or {}).get("date")
        if not date_str:
            continue
        try:
            match_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if match_date >= cutoff:
            return date_str

    return None


def fetch_recent_european_matches(days_back: int = None) -> list[dict]:
    """Second avis, indépendant d'API-Football : matchs de Ligue des
    Champions/Europa des derniers jours via football-data.org — [] si pas
    de clé ou erreur. Un seul appel par compétition suivie
    (config.EURO_COMPETITION_CODES), pas un par équipe.

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
