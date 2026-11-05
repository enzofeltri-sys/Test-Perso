from data import get_min_order_limits, fetch_ohlcv_history, fetch_latest_candles

import ccxt
import pytest


class FakeExchange:
    def __init__(self, markets):
        self.markets = markets
        self.load_markets_called = False

    def load_markets(self):
        self.load_markets_called = True


MS_PER_HOUR = 3_600_000


class FakePaginatedExchange:
    """Simule fetch_ohlcv(since, limit) sur une liste de bougies fixe,
    avec une taille de page NATIVE potentiellement plus petite que la
    `limit` demandée (comme OKX, plafonné à 300 quel que soit limit=1000
    demandé) — voir la régression que ça a causée dans fetch_ohlcv_history."""

    id = "fake"
    rateLimit = 0

    def __init__(self, candles, native_page_size, now_ms):
        self.candles = candles  # liste de [ts, o, h, l, c, v], triée
        self.native_page_size = native_page_size
        self.now_ms = now_ms

    def milliseconds(self):
        return self.now_ms

    def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None):
        eff_limit = min(limit, self.native_page_size)
        return [c for c in self.candles if c[0] >= since][:eff_limit]


def test_fetch_ohlcv_history_paginates_past_a_small_native_page_size():
    since_days = 5
    now = 1_700_000_000_000
    start = now - since_days * 24 * MS_PER_HOUR
    n_hours = since_days * 24 + 10  # historique dispo au-delà de la fenêtre demandée
    candles = [[start + i * MS_PER_HOUR, 1, 1, 1, 1, 1] for i in range(n_hours)]

    # page native de 10 (bien en dessous de limit=1000 demandé en interne)
    exchange = FakePaginatedExchange(candles, native_page_size=10, now_ms=now)
    df = fetch_ohlcv_history(exchange, "BTC/USDT", "1h", since_days)

    assert len(df) >= since_days * 24, (
        f"seulement {len(df)} bougies récupérées malgré un historique disponible bien plus "
        "profond — la pagination s'est arrêtée trop tôt à cause d'une page native petite"
    )


def test_fetch_ohlcv_history_stops_when_exchange_genuinely_runs_out_of_data():
    since_days = 30
    now = 1_700_000_000_000
    start = now - since_days * 24 * MS_PER_HOUR
    n_hours = 5 * 24  # seulement 5 jours réellement disponibles, bien avant `now`
    candles = [[start + i * MS_PER_HOUR, 1, 1, 1, 1, 1] for i in range(n_hours)]

    exchange = FakePaginatedExchange(candles, native_page_size=1000, now_ms=now)
    df = fetch_ohlcv_history(exchange, "BTC/USDT", "1h", since_days)

    assert len(df) == n_hours


def test_get_min_order_limits_reads_amount_and_cost():
    exchange = FakeExchange({
        "BTC/USDT": {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 5.0}}},
    })
    limits = get_min_order_limits(exchange, ["BTC/USDT"])
    assert limits["BTC/USDT"] == {"min_amount": 0.0001, "min_cost": 5.0}


def test_get_min_order_limits_defaults_to_none_when_unpublished():
    exchange = FakeExchange({"BTC/USDT": {"limits": {}}})
    limits = get_min_order_limits(exchange, ["BTC/USDT"])
    assert limits["BTC/USDT"] == {"min_amount": None, "min_cost": None}


def test_get_min_order_limits_handles_unknown_symbol():
    exchange = FakeExchange({})
    limits = get_min_order_limits(exchange, ["DOES/NOTEXIST"])
    assert limits["DOES/NOTEXIST"] == {"min_amount": None, "min_cost": None}


def test_get_min_order_limits_loads_markets_only_when_not_already_cached():
    exchange = FakeExchange({})  # markets vide -> load_markets() doit être appelé
    get_min_order_limits(exchange, ["BTC/USDT"])
    assert exchange.load_markets_called is True

    exchange2 = FakeExchange({"BTC/USDT": {"limits": {}}})  # déjà chargé -> pas de rappel
    get_min_order_limits(exchange2, ["BTC/USDT"])
    assert exchange2.load_markets_called is False


class FakeFlakyExchange:
    """Simule un fetch_ohlcv qui échoue en RateLimitExceeded (429) les
    `fail_times` premiers appels, puis réussit — pour tester le bref
    retry de fetch_latest_candles sans dépendre d'un vrai 429 KuCoin."""

    def __init__(self, fail_times: int, candles):
        self.fail_times = fail_times
        self.candles = candles
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ccxt.RateLimitExceeded("kucoin 429000 Too many requests")
        return self.candles


def test_fetch_latest_candles_retries_once_on_rate_limit_then_succeeds(monkeypatch):
    """Bug réel : un 429 KuCoin isolé et transitoire faisait échouer toute
    la paire pour ce cycle (voir le bug de coupe-circuit qu'il a
    déclenché), alors qu'un bref retry suffit souvent."""
    monkeypatch.setattr("data.time.sleep", lambda s: None)  # pas de vraie attente dans les tests
    candles = [[0, 1, 1, 1, 1, 1]]
    exchange = FakeFlakyExchange(fail_times=1, candles=candles)

    df = fetch_latest_candles(exchange, "BTC/USDT", "1h", limit=200)

    assert exchange.calls == 2, "doit avoir retenté exactement une fois après le premier échec"
    assert len(df) == 1


def test_fetch_latest_candles_gives_up_after_max_retries(monkeypatch):
    """Un 429 qui persiste au-delà du nombre de retries doit toujours
    remonter (pour que run_tick le journalise et continue avec les autres
    paires) — ce n'est qu'un bref sursis, jamais une boucle infinie."""
    monkeypatch.setattr("data.time.sleep", lambda s: None)
    exchange = FakeFlakyExchange(fail_times=99, candles=[[0, 1, 1, 1, 1, 1]])

    with pytest.raises(ccxt.RateLimitExceeded):
        fetch_latest_candles(exchange, "BTC/USDT", "1h", limit=200, max_retries=1)

    assert exchange.calls == 2  # tentative initiale + 1 retry, puis abandon


def test_fetch_latest_candles_never_retries_on_a_non_rate_limit_error(monkeypatch):
    """Une erreur qui n'est PAS un 429 doit remonter immédiatement — pas de
    retry inutile qui ralentirait le cycle pour rien."""
    monkeypatch.setattr("data.time.sleep", lambda s: None)

    class _ExchangeBoom:
        def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
            raise ccxt.ExchangeError("symbole inconnu")

    with pytest.raises(ccxt.ExchangeError):
        fetch_latest_candles(_ExchangeBoom(), "BTC/USDT", "1h", limit=200)
