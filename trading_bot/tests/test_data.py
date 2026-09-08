from data import get_min_order_limits


class FakeExchange:
    def __init__(self, markets):
        self.markets = markets
        self.load_markets_called = False

    def load_markets(self):
        self.load_markets_called = True


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
