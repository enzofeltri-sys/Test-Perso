"""
data.py
-------
Récupération des données de marché via ccxt (API publique des exchanges).

Aucune clé API n'est nécessaire pour ces fonctions : elles ne font que lire
des données de marché publiques (bougies OHLCV), jamais passer d'ordres.
"""

import time
import pandas as pd
import ccxt


def get_exchange(exchange_id: str):
    """Instancie un exchange ccxt en mode lecture seule (données publiques)."""
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})
    return exchange


def fetch_ohlcv_history(exchange, symbol: str, timeframe: str, since_days: int) -> pd.DataFrame:
    """
    Télécharge l'historique OHLCV en paginant (ccxt limite généralement
    à ~300-1000 bougies par appel selon l'exchange).

    Retourne un DataFrame indexé par timestamp avec les colonnes:
    open, high, low, close, volume
    """
    ms_per_day = 24 * 60 * 60 * 1000
    since = exchange.milliseconds() - since_days * ms_per_day

    all_candles = []
    limit = 1000

    while True:
        now = exchange.milliseconds()
        if since >= now:
            break

        candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not candles:
            # aucune donnée à partir de `since` : soit l'historique de
            # l'exchange s'arrête réellement là, soit on a rattrapé le
            # présent — dans les deux cas il n'y a plus rien à paginer
            break
        all_candles += candles
        last_ts = candles[-1][0]
        # évite les boucles infinies si l'exchange renvoie toujours la même page
        next_since = last_ts + 1
        if next_since <= since:
            break
        since = next_since
        # NE PAS s'arrêter juste parce qu'un appel a rendu moins que `limit` —
        # certains exchanges (ex: OKX) plafonnent nativement bien en dessous de
        # 1000 bougies par appel sans que ça signifie "fin de l'historique". Le
        # seul signal fiable est : plus de bougies du tout, ou `since` a
        # rattrapé le présent (vérifié en haut de boucle).
        time.sleep(exchange.rateLimit / 1000)

    if not all_candles:
        raise ValueError(f"Aucune donnée retournée pour {symbol} sur {exchange.id}")

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop_duplicates(subset="timestamp").set_index("timestamp").sort_index()
    return df


def fetch_latest_candles(exchange, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    """Récupère les `limit` dernières bougies (utilisé en paper trading pour le polling)."""
    candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp").sort_index()
    return df


def get_min_order_limits(exchange, symbols: list) -> dict:
    """Contraintes minimales de l'exchange par paire (quantité et/ou
    valeur notionnelle minimum par ordre) — sans ça, le bot peut calculer
    une position plus petite que ce qu'un exchange réel accepterait, un
    ordre qu'aucune exécution réelle ne pourrait jamais passer.

    Retourne {symbol: {"min_amount": float|None, "min_cost": float|None}}.
    Best-effort : certains exchanges ne publient pas ces limites via ccxt
    pour certaines paires, auquel cas la paire concernée n'a simplement
    pas de plancher connu (comportement identique à avant ce contrôle)."""
    if not exchange.markets:
        exchange.load_markets()

    limits = {}
    for s in symbols:
        market = exchange.markets.get(s)
        m_limits = (market or {}).get("limits") or {}
        limits[s] = {
            "min_amount": (m_limits.get("amount") or {}).get("min"),
            "min_cost": (m_limits.get("cost") or {}).get("min"),
        }
    return limits
