"""
supabase_state.py
------------------
Persistance de l'état du bot de paris (bankroll, horodatages de throttle),
de l'historique des matchs, du modèle entraîné, du journal des paris et du
journal texte — via l'API REST de Supabase (PostgREST), avec juste
`requests`. Même approche que trading_bot/supabase_state.py.

Pourquoi c'est nécessaire : web_app.py tourne sur un hébergement gratuit
(Render free tier) qui redémarre le processus n'importe quand (mise en
veille par inactivité, redéploiement...). Rien ne peut vivre uniquement en
mémoire Python d'un appel à l'autre — tout est rechargé/sauvegardé à
CHAQUE tick.

Variables d'environnement requises :
  SUPABASE_URL   ex: https://xxxx.supabase.co
  SUPABASE_KEY   clé "anon" du projet (Project Settings -> API -> Legacy anon key)

Variable optionnelle :
  TABLE_PREFIX   préfixe des tables (défaut "footballbot"). Permet de faire
                 tourner plusieurs bots sur le même projet Supabase — voir
                 DEPLOIEMENT.md.
"""

import os
from datetime import datetime, timezone

import pandas as pd
import requests

STATE_ID = "default"
MODEL_ID = "default"
PAGE_SIZE = 1000


def _prefix() -> str:
    return os.environ.get("TABLE_PREFIX") or "footballbot"


def _table(suffix: str) -> str:
    return f"{_prefix()}_{suffix}"


def _headers() -> dict:
    key = os.environ["SUPABASE_KEY"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"


# --------------------------------------------------------------------------
# État (bankroll, throttle)
# --------------------------------------------------------------------------

def load_state(initial_bankroll: float) -> dict:
    """Récupère l'état sauvegardé, ou un état initial si c'est le tout
    premier cycle (aucune ligne encore en base)."""
    url = f"{_base_url()}/{_table('state')}"
    resp = requests.get(
        url, headers=_headers(),
        params={"id": f"eq.{STATE_ID}", "select": "*"}, timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()

    if not rows:
        return {"bankroll": initial_bankroll, "last_odds_fetch_at": None, "last_scores_fetch_at": None}

    row = rows[0]
    return {
        "bankroll": row["bankroll"],
        "last_odds_fetch_at": row.get("last_odds_fetch_at"),
        "last_scores_fetch_at": row.get("last_scores_fetch_at"),
    }


def save_state(state: dict) -> None:
    """Sauvegarde (upsert) l'état courant — une seule ligne, id='default'."""
    url = f"{_base_url()}/{_table('state')}"
    payload = {
        "id": STATE_ID,
        "bankroll": state["bankroll"],
        "last_odds_fetch_at": state.get("last_odds_fetch_at"),
        "last_scores_fetch_at": state.get("last_scores_fetch_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()


# --------------------------------------------------------------------------
# Historique des matchs (alimenté par retrain.py, lu par web_app.py pour
# calculer les features des matchs à venir)
# --------------------------------------------------------------------------

def upsert_matches(df: pd.DataFrame) -> int:
    """Upsert en masse dans {prefix}_matches, par lots de PAGE_SIZE lignes.
    Déduplication sur (league, season, home_team, away_team, date). Retourne
    le nombre de lignes envoyées."""
    if df.empty:
        return 0

    records = df.copy()
    records["date"] = pd.to_datetime(records["Date"]).dt.strftime("%Y-%m-%d")
    records = records.rename(columns={
        "Season": "season", "League": "league",
        "HomeTeam": "home_team", "AwayTeam": "away_team",
        "FTHG": "fthg", "FTAG": "ftag", "FTR": "ftr",
        "OddsH": "odds_h", "OddsD": "odds_d", "OddsA": "odds_a",
    })
    columns = ["league", "season", "home_team", "away_team", "date", "fthg", "ftag", "ftr", "odds_h", "odds_d", "odds_a"]
    payload_rows = records[columns].where(pd.notnull(records[columns]), None).to_dict(orient="records")

    url = f"{_base_url()}/{_table('matches')}"
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    params = {"on_conflict": "league,season,home_team,away_team,date"}

    sent = 0
    for i in range(0, len(payload_rows), PAGE_SIZE):
        batch = payload_rows[i:i + PAGE_SIZE]
        resp = requests.post(url, headers=headers, params=params, json=batch, timeout=30)
        resp.raise_for_status()
        sent += len(batch)

    return sent


def get_all_matches() -> pd.DataFrame:
    """Récupère tout l'historique des matchs stocké en base (paginé), pour
    calculer les features de forme des matchs à venir. Retourne un
    DataFrame avec les colonnes d'origine (Date, League, HomeTeam, ...)
    pour rester compatible avec src.features."""
    url = f"{_base_url()}/{_table('matches')}"
    all_rows = []
    offset = 0
    while True:
        resp = requests.get(
            url, headers=_headers(),
            params={"select": "*", "order": "date.asc", "limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json()
        all_rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if not all_rows:
        return pd.DataFrame(columns=["Date", "Season", "League", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "OddsH", "OddsD", "OddsA"])

    df = pd.DataFrame(all_rows).rename(columns={
        "date": "Date", "season": "Season", "league": "League",
        "home_team": "HomeTeam", "away_team": "AwayTeam",
        "fthg": "FTHG", "ftag": "FTAG", "ftr": "FTR",
        "odds_h": "OddsH", "odds_d": "OddsD", "odds_a": "OddsA",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def get_known_team_names() -> dict:
    """{league: [noms d'équipes connus]} — sert au rapprochement flou des
    noms d'équipes (voir src/team_names.normalize_team_name)."""
    df = get_all_matches()
    if df.empty:
        return {}
    known = {}
    for league, group in df.groupby("League"):
        known[league] = sorted(set(group["HomeTeam"]) | set(group["AwayTeam"]))
    return known


# --------------------------------------------------------------------------
# Modèle entraîné
# --------------------------------------------------------------------------

def save_model(model_b64: str, feature_columns: list, train_matches: int, test_matches: int, backtest_metrics: dict) -> None:
    url = f"{_base_url()}/{_table('model')}"
    payload = {
        "id": MODEL_ID,
        "model_b64": model_b64,
        "feature_columns": feature_columns,
        "train_matches": train_matches,
        "test_matches": test_matches,
        "backtest_metrics": backtest_metrics,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()


def load_model() -> dict:
    """Retourne la ligne du modèle courant ({} si aucun modèle entraîné
    pour l'instant — web_app.py doit gérer ce cas sans planter)."""
    url = f"{_base_url()}/{_table('model')}"
    resp = requests.get(
        url, headers=_headers(),
        params={"id": f"eq.{MODEL_ID}", "select": "*"}, timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else {}


# --------------------------------------------------------------------------
# Paris (footballbot_bets)
# --------------------------------------------------------------------------

def bet_exists(home_team: str, away_team: str, commence_time: str) -> bool:
    url = f"{_base_url()}/{_table('bets')}"
    resp = requests.get(
        url, headers=_headers(),
        params={
            "home_team": f"eq.{home_team}", "away_team": f"eq.{away_team}",
            "commence_time": f"eq.{commence_time}", "select": "id", "limit": "1",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return len(resp.json()) > 0


def insert_bet(bet: dict) -> None:
    url = f"{_base_url()}/{_table('bets')}"
    resp = requests.post(url, headers=_headers(), json=bet, timeout=15)
    resp.raise_for_status()


def get_pending_bets() -> list:
    url = f"{_base_url()}/{_table('bets')}"
    resp = requests.get(
        url, headers=_headers(),
        params={"status": "eq.pending", "select": "*"}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def settle_bet(bet_id: int, status: str, pnl: float, bankroll_after: float) -> None:
    url = f"{_base_url()}/{_table('bets')}"
    payload = {
        "status": status, "pnl": pnl, "bankroll_after": bankroll_after,
        "settled_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = requests.patch(url, headers=_headers(), params={"id": f"eq.{bet_id}"}, json=payload, timeout=15)
    resp.raise_for_status()


def get_recent_bets(limit: int = 10) -> list:
    url = f"{_base_url()}/{_table('bets')}"
    resp = requests.get(
        url, headers=_headers(),
        params={"select": "*", "order": "ts.desc", "limit": str(limit)}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_settled_bets_chronological(limit: int = 300) -> list:
    """Paris réglés (won/lost), du plus ancien au plus récent — matière
    première de la courbe de bankroll de la page de statut. Best-effort :
    [] si Supabase est injoignable plutôt que de casser la page."""
    try:
        url = f"{_base_url()}/{_table('bets')}"
        resp = requests.get(
            url, headers=_headers(),
            params={"status": "in.(won,lost)", "select": "*", "order": "settled_at.asc", "limit": str(limit)},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


# --------------------------------------------------------------------------
# Journal / erreurs (best-effort, calqué sur trading_bot)
# --------------------------------------------------------------------------

def log_journal_entry(author: str, message: str, data: dict = None) -> None:
    try:
        url = f"{_base_url()}/{_table('journal')}"
        payload = {"author": author, "message": message}
        if data is not None:
            payload["data"] = data
        resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
        resp.raise_for_status()
    except Exception:
        pass


def get_recent_journal(limit: int = 10) -> list:
    try:
        url = f"{_base_url()}/{_table('journal')}"
        resp = requests.get(
            url, headers=_headers(),
            params={"select": "*", "order": "ts.desc", "limit": str(limit)}, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def log_error(message: str) -> None:
    try:
        url = f"{_base_url()}/{_table('errors')}"
        resp = requests.post(url, headers=_headers(), json={"message": message[:4000]}, timeout=15)
        resp.raise_for_status()
    except Exception:
        pass


def get_recent_errors(limit: int = 5) -> list:
    try:
        url = f"{_base_url()}/{_table('errors')}"
        resp = requests.get(
            url, headers=_headers(),
            params={"select": "*", "order": "ts.desc", "limit": str(limit)}, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []
