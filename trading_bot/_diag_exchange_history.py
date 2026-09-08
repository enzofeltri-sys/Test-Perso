"""Diagnostic temporaire : combien de bougies 1h BTC/USDT chaque exchange
rend-il réellement accessible (depuis CE réseau), peu importe le since_days
demandé ? Sert à choisir une source de données pour le walk-forward,
indépendamment de l'exchange utilisé pour le paper trading en direct
(KuCoin, limité à ~83 jours en 1h — voir la conversation). Fichier à
supprimer une fois la décision prise."""

import ccxt

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
SINCE_DAYS = 365

for ex_id in ["kraken", "bybit", "okx", "binance", "kucoin", "mexc", "gateio", "bitget"]:
    try:
        exchange_class = getattr(ccxt, ex_id)
        ex = exchange_class({"enableRateLimit": True, "timeout": 15000})
        since = ex.milliseconds() - SINCE_DAYS * 24 * 60 * 60 * 1000
        candles = ex.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, since=since, limit=1000)
        if not candles:
            print(f"{ex_id}: 0 bougie")
            continue
        import datetime
        first = datetime.datetime.utcfromtimestamp(candles[0][0] / 1000)
        last = datetime.datetime.utcfromtimestamp(candles[-1][0] / 1000)
        span_days = (last - first).days
        print(f"{ex_id}: {len(candles)} bougies (1er appel), {first} -> {last} (~{span_days}j sur ce seul appel)")
    except Exception as e:
        print(f"{ex_id}: ECHEC - {type(e).__name__}: {str(e)[:150]}")
