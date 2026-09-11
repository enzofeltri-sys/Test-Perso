"""
supabase_state.py
------------------
Stocke l'état du bot (cash, positions ouvertes, coupe-circuits) et
journalise les trades / erreurs dans Supabase, via son API REST
(PostgREST), avec juste `requests` — pas de dépendance lourde.

Pourquoi c'est nécessaire : web_app.py est fait pour tourner sur un
hébergement gratuit (ex: Render free tier) qui redémarre le processus
n'importe quand (mise en veille par inactivité, redéploiement...).
L'état ne peut donc PAS vivre uniquement en mémoire Python comme dans
paper_trader.py (pensé pour un `run_forever()` qui ne s'arrête jamais) —
il doit être rechargé et sauvegardé à CHAQUE appel.

Variables d'environnement requises :
  SUPABASE_URL   ex: https://xxxx.supabase.co
  SUPABASE_KEY   clé "anon" du projet (Project Settings -> API -> Legacy anon key)

Variable optionnelle :
  TABLE_PREFIX   préfixe des tables (défaut "tradingbot" -> tradingbot_state,
                 tradingbot_trades, ...). Sert à faire tourner PLUSIEURS bots
                 indépendants sur le MÊME projet Supabase, chacun avec ses
                 propres tables (ex: TABLE_PREFIX=altbot -> altbot_state,
                 altbot_trades, ...) — voir DEPLOIEMENT.md, "Un deuxième bot
                 en parallèle". Absent ou vide -> comportement identique à
                 avant cette variable (préfixe "tradingbot").
"""

import os
from datetime import date, datetime, timezone

import requests

STATE_ID = "default"


def _prefix() -> str:
    return os.environ.get("TABLE_PREFIX") or "tradingbot"


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


def load_state(initial_balance: float) -> dict:
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
        return {
            "cash": initial_balance,
            "positions": {},
            "daily_current_day": None,
            "daily_equity_at_day_start": None,
            "daily_tripped_today": False,
            "total_dd_peak_equity": None,
            "total_dd_tripped": False,
        }

    row = rows[0]
    day_str = row.get("daily_current_day")
    return {
        "cash": row["cash"],
        "positions": row.get("positions") or {},
        # stocké en base comme une date ISO ("2026-09-08") -> objet date,
        # sinon la comparaison dans DailyLossCircuitBreaker.update() ne
        # correspondrait jamais et le coupe-circuit journalier se
        # "réinitialiserait" à tort à chaque cycle.
        "daily_current_day": date.fromisoformat(day_str) if day_str else None,
        "daily_equity_at_day_start": row.get("daily_equity_at_day_start"),
        "daily_tripped_today": bool(row.get("daily_tripped_today", False)),
        "total_dd_peak_equity": row.get("total_dd_peak_equity"),
        "total_dd_tripped": bool(row.get("total_dd_tripped", False)),
    }


def save_state(state: dict) -> None:
    """Sauvegarde (upsert) l'état courant — une seule ligne, id='default'."""
    url = f"{_base_url()}/{_table('state')}"
    day = state.get("daily_current_day")
    payload = {
        "id": STATE_ID,
        "cash": state["cash"],
        "positions": state["positions"],
        "daily_current_day": day.isoformat() if hasattr(day, "isoformat") else day,
        "daily_equity_at_day_start": state.get("daily_equity_at_day_start"),
        "daily_tripped_today": bool(state.get("daily_tripped_today", False)),
        "total_dd_peak_equity": state.get("total_dd_peak_equity"),
        "total_dd_tripped": bool(state.get("total_dd_tripped", False)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()


def log_trade(symbol: str, side: str, price: float, qty: float, reason: str,
              cash_after: float, equity_after: float) -> None:
    url = f"{_base_url()}/{_table('trades')}"
    payload = {
        "symbol": symbol, "side": side, "price": price, "qty": qty, "reason": reason,
        "cash_after": cash_after, "equity_after": equity_after,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
    resp.raise_for_status()


def get_recent_trades(limit: int = 10) -> list:
    """Les derniers trades journalisés, du plus récent au plus ancien —
    utilisé par la page de statut de web_app.py."""
    url = f"{_base_url()}/{_table('trades')}"
    resp = requests.get(
        url, headers=_headers(),
        params={"select": "*", "order": "ts.desc", "limit": str(limit)}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_last_trade_ts_by_symbol(limit: int = 500) -> dict:
    """{symbol: horodatage ISO du DERNIER trade (achat OU vente) sur cette
    paire}, parmi les `limit` trades les plus récents tous symboles
    confondus. Sert au cooldown de rachat (voir run_tick) : combien de
    temps s'est écoulé depuis la dernière activité sur une paire, sans
    ajouter de colonne d'état ni de requête par symbole — les lignes
    arrivent déjà triées par date décroissante, donc la première
    occurrence de chaque symbole EST son trade le plus récent. `limit`
    doit largement couvrir la fenêtre de cooldown la plus longue utilisée :
    au volume de trades observé sur ce projet (quelques dizaines par mois),
    500 couvre plusieurs mois d'historique en un seul appel. Best-effort :
    {} si la table est injoignable, pour ne jamais faire échouer un cycle."""
    try:
        url = f"{_base_url()}/{_table('trades')}"
        resp = requests.get(
            url, headers=_headers(),
            params={"select": "symbol,ts", "order": "ts.desc", "limit": str(limit)}, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return {}
    last = {}
    for row in rows:
        symbol = row.get("symbol")
        if symbol and symbol not in last:
            last[symbol] = row.get("ts")
    return last


def get_recent_errors(limit: int = 5) -> list:
    """Les dernières erreurs journalisées, du plus récent au plus ancien."""
    url = f"{_base_url()}/{_table('errors')}"
    resp = requests.get(
        url, headers=_headers(),
        params={"select": "*", "order": "ts.desc", "limit": str(limit)}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def log_journal_entry(author: str, message: str, data: dict = None) -> None:
    """Ajoute une entrée au journal partagé (tradingbot_journal) — c'est
    là que le bot EXPLIQUE ses décisions (recalibrate.py notamment : quoi,
    pourquoi, avec quelles preuves), et là où le "manager" (toi, ou Claude
    Cowork) peut répondre en écrivant ses propres entrées
    (author='manager'). Ce canal ne pilote JAMAIS directement le bot —
    le pilotage réel reste tradingbot_config (structuré, pas du texte
    libre) ; ceci est uniquement la couche de visibilité/discussion.
    Best-effort : un souci ici ne doit jamais faire planter un cycle."""
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
    """Les dernières entrées du journal, du plus récent au plus ancien.
    Best-effort ([] si la table n'existe pas encore — voir DEPLOIEMENT.md
    pour la migration) : optionnelle, ne doit jamais faire échouer la page
    de statut ni un cycle."""
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


def get_last_journal_event(event: str) -> dict:
    """Dernière entrée du journal dont data.event vaut `event` (ex :
    'config_change'), ou {} s'il n'y en a pas. Sert au bot — qui ne garde
    aucun état en mémoire d'un tick à l'autre — à savoir ce qu'il a déjà
    acquitté, sans colonne supplémentaire dans tradingbot_state : l'entrée
    de journal EST l'acquittement. Best-effort ({} si la table n'existe
    pas encore)."""
    try:
        url = f"{_base_url()}/{_table('journal')}"
        resp = requests.get(
            url, headers=_headers(),
            params={"select": "*", "data->>event": f"eq.{event}", "order": "ts.desc", "limit": "1"},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else {}
    except Exception:
        return {}


def load_config_overrides() -> dict:
    """Réglages ajustables à la volée (sans redéployer), posés dans la
    table tradingbot_config — voir DEPLOIEMENT.md, section 'Ajuster le
    bot à la volée'. Une clé absente ou nulle veut dire "garde la valeur
    de config.yaml". Best-effort : si Supabase est injoignable ou que la
    table n'existe pas encore, on se rabat silencieusement sur
    config.yaml plutôt que de faire planter le cycle."""
    try:
        url = f"{_base_url()}/{_table('config')}"
        resp = requests.get(
            url, headers=_headers(),
            params={"id": "eq.default", "select": "*"}, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else {}
    except Exception:
        return {}


def save_strategy_overrides(strategy_overrides: dict, note: str = None) -> None:
    """Écrit les paramètres de stratégie recalibrés (voir recalibrate.py)
    dans tradingbot_config.strategy_overrides — UNIQUEMENT cette colonne,
    jamais les colonnes de risque/active_symbols existantes, qu'un humain
    garde le contrôle total dessus. Upsert comme save_state(), donc les
    autres colonnes déjà posées à la main ne sont pas touchées."""
    url = f"{_base_url()}/{_table('config')}"
    payload = {
        "id": STATE_ID,
        "strategy_overrides": strategy_overrides,
        "updated_by": "recalibrate.py",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if note:
        payload["note"] = note
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()


def log_error(message: str) -> None:
    """Best-effort : un souci de logging ne doit jamais faire planter un
    cycle de trading, ni empêcher /tick de répondre correctement — donc
    TOUT est dans le try, y compris la construction de l'URL/des headers
    (qui échoue elle-même si SUPABASE_URL/SUPABASE_KEY manquent)."""
    try:
        url = f"{_base_url()}/{_table('errors')}"
        resp = requests.post(url, headers=_headers(), json={"message": message[:4000]}, timeout=15)
        resp.raise_for_status()
    except Exception:
        pass
