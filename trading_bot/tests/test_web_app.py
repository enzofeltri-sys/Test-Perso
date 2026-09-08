"""
Régression pour web_app.py — la version déployée sur Render (voir
DEPLOIEMENT.md). Ce module ne garde AUCUN état en mémoire entre deux
appels à /tick : chaque position ouverte doit mémoriser explicitement
quelle sous-stratégie (trend/range) l'a ouverte, dans
`positions[symbol]["active_substrategy"]`, pour gérer sa sortie
correctement au tick suivant (voir web_app.py).

Ces tests simulent plusieurs ticks successifs avec de fausses données de
marché et un faux backend Supabase (en mémoire, pas de réseau), et
vérifient qu'aucune position ouverte ne se retrouve avec
`active_substrategy` à None — ce qui ferait silencieusement retomber sur
la mauvaise sous-stratégie (voir la correction de strategy.py :
RegimeSwitchingStrategy.compute_stop_and_target() doit être appelé sur le
wrapper, pas sur une sous-stratégie choisie à la main AVANT confirmation).
"""

import yaml

import web_app
from conftest import make_ohlcv


class FakeDB:
    def __init__(self):
        self.state = None
        self.trades = []
        self.errors = []

    def load_state(self, initial_balance):
        if self.state is None:
            return {
                "cash": initial_balance, "positions": {},
                "daily_current_day": None, "daily_equity_at_day_start": None,
                "daily_tripped_today": False, "total_dd_peak_equity": None,
                "total_dd_tripped": False,
            }
        return self.state

    def save_state(self, state):
        self.state = dict(state)

    def log_trade(self, symbol, side, price, qty, reason, cash_after, equity_after):
        self.trades.append({
            "symbol": symbol, "side": side, "price": price, "qty": qty,
            "reason": reason, "cash_after": cash_after, "equity_after": equity_after,
        })

    def log_error(self, message):
        self.errors.append(message)


class Clock:
    def __init__(self, idx):
        self.idx = idx


def _fake_fetch_latest_candles(histories, clock):
    def _fetch(exchange, symbol, timeframe, limit=200):
        df = histories[symbol]
        end = clock.idx + 1
        start = max(0, end - limit)
        return df.iloc[start:end].copy()
    return _fetch


def test_run_tick_never_leaves_a_position_with_unresolved_substrategy(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)

    any_position_opened = False
    for _ in range(60):
        clock.idx += 5
        result = web_app.run_tick()
        assert result["ok"] is True

        positions = fake_db.state["positions"] if fake_db.state else {}
        for symbol, pos in positions.items():
            if pos is None:
                continue
            any_position_opened = True
            assert pos["active_substrategy"] in ("trend", "range"), (
                f"{symbol}: active_substrategy={pos['active_substrategy']!r} — "
                "une position ouverte doit toujours savoir quelle sous-stratégie gère sa sortie"
            )

    assert any_position_opened, "aucune position ouverte sur 60 ticks : le test ne couvre rien, ajuster les seeds/n"
    assert not fake_db.errors
