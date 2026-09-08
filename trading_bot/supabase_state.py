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
"""

import os
from datetime import date, datetime, timezone

import requests

STATE_ID = "default"


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
    url = f"{_base_url()}/tradingbot_state"
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
    url = f"{_base_url()}/tradingbot_state"
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
    url = f"{_base_url()}/tradingbot_trades"
    payload = {
        "symbol": symbol, "side": side, "price": price, "qty": qty, "reason": reason,
        "cash_after": cash_after, "equity_after": equity_after,
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
    resp.raise_for_status()


def get_recent_trades(limit: int = 10) -> list:
    """Les derniers trades journalisés, du plus récent au plus ancien —
    utilisé par la page de statut de web_app.py."""
    url = f"{_base_url()}/tradingbot_trades"
    resp = requests.get(
        url, headers=_headers(),
        params={"select": "*", "order": "ts.desc", "limit": str(limit)}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_recent_errors(limit: int = 5) -> list:
    """Les dernières erreurs journalisées, du plus récent au plus ancien."""
    url = f"{_base_url()}/tradingbot_errors"
    resp = requests.get(
        url, headers=_headers(),
        params={"select": "*", "order": "ts.desc", "limit": str(limit)}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def log_error(message: str) -> None:
    """Best-effort : un souci de logging ne doit jamais faire planter un
    cycle de trading."""
    url = f"{_base_url()}/tradingbot_errors"
    try:
        resp = requests.post(url, headers=_headers(), json={"message": message[:4000]}, timeout=15)
        resp.raise_for_status()
    except Exception:
        pass
