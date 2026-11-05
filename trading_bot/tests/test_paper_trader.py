"""
paper_trader.py — le moteur de paper trading "boucle infinie" (utilisé en
local via `python main.py paper`, indépendant de web_app.py/run_tick qui
sert le déploiement Render). Ces tests couvrent spécifiquement le rachat
(plusieurs positions simultanées sur une même paire, voir
max_positions_per_symbol) et le cooldown de rachat (reentry_cooldown_hours)
— le reste (sorties, coupe-circuits, anti-corrélation) est la même logique
que portfolio_backtester.py, déjà couverte là-bas.
"""

import pandas as pd
import pytest

import paper_trader
from conftest import make_ohlcv


class _AlwaysEnterStrategy:
    """Stub minimal : entre dès que possible, ne sort jamais d'elle-même —
    isole le comportement du portefeuille (rachat, cooldown) de la
    logique de décision d'une vraie stratégie."""
    def __init__(self):
        self._active = None

    def prepare(self, df):
        return df

    def should_enter(self, row):
        return True

    def should_exit_on_signal(self, row):
        return False

    def compute_stop_and_target(self, entry_price, row):
        self._active = "trend"
        return entry_price * 0.01, entry_price * 100, entry_price

    def on_position_closed(self):
        self._active = None

    @property
    def active_regime(self):
        return self._active

    @property
    def trend_strategy(self):
        return self

    @property
    def range_strategy(self):
        return self


class _FlickerStrategy(_AlwaysEnterStrategy):
    """Sort systématiquement à la bougie suivante (sortie sur signal) —
    génère des trades rapprochés dans le temps pour isoler le cooldown."""
    def should_exit_on_signal(self, row):
        return True


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


def _make_trader(monkeypatch, strategy_factory, symbols=("BTC/USDT",), **kwargs):
    monkeypatch.setattr(paper_trader.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(paper_trader.data, "get_min_order_limits", lambda exchange, symbols: {})
    trader = paper_trader.PortfolioPaperTrader(
        exchange_id="kucoin", symbols=list(symbols), timeframe="1h",
        strategy_factory=strategy_factory, initial_balance=1000.0,
        poll_interval_seconds=1, log_file=kwargs.pop("log_file"),
        fee_pct=0.001, risk_per_trade_pct=0.5, max_position_notional_usd=10.0,
        **kwargs,
    )
    return trader


def test_max_positions_per_symbol_caps_rebuys_on_the_same_pair(monkeypatch, tmp_path):
    trader = _make_trader(
        monkeypatch, _AlwaysEnterStrategy, max_positions_per_symbol=3,
        max_concurrent_positions=100, log_file=str(tmp_path / "trades.csv"),
    )
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    monkeypatch.setattr(paper_trader.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))

    for _ in range(6):
        clock.idx += 1
        trader.run_once()

    assert len(trader.positions["BTC/USDT"]) == 3


def test_default_max_positions_per_symbol_matches_historical_behaviour(monkeypatch, tmp_path):
    trader = _make_trader(
        monkeypatch, _AlwaysEnterStrategy, max_concurrent_positions=100,
        log_file=str(tmp_path / "trades.csv"),
    )  # max_positions_per_symbol non fourni -> défaut 1
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    monkeypatch.setattr(paper_trader.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))

    for _ in range(6):
        clock.idx += 1
        trader.run_once()

    assert len(trader.positions["BTC/USDT"]) == 1


def test_reentry_cooldown_blocks_immediate_rebuy_but_allows_after_the_window(monkeypatch, tmp_path):
    cooldown_hours = 5
    trader = _make_trader(
        monkeypatch, _FlickerStrategy, max_positions_per_symbol=1,
        reentry_cooldown_hours=cooldown_hours, log_file=str(tmp_path / "trades.csv"),
    )
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=100, start=100.0)}
    clock = Clock(idx=10)
    monkeypatch.setattr(paper_trader.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))

    buys = []
    for _ in range(40):
        clock.idx += 1
        before = len(trader.positions.get("BTC/USDT") or [])
        trader.run_once()
        after = len(trader.positions.get("BTC/USDT") or [])
        if after > before:
            buys.append(histories["BTC/USDT"].index[clock.idx])

    assert len(buys) >= 3, "pas assez de rachats pour que le test prouve quelque chose"
    for prev, nxt in zip(buys, buys[1:]):
        gap_hours = (nxt - prev).total_seconds() / 3600
        assert gap_hours >= cooldown_hours - 1e-9, (
            f"rachat {gap_hours:.1f}h après le précédent, en dessous du cooldown ({cooldown_hours}h)"
        )


def test_sell_equity_after_does_not_double_count_the_closed_position(monkeypatch, tmp_path):
    """Régression (même bug que web_app.py/run_tick, signalé sur le bot #2) :
    l'equity journalisée pour une vente comptait la position vendue une
    seconde fois, la position n'étant retirée de self.positions[symbole]
    qu'après avoir calculé l'equity. Sans autre position ouverte après
    cette vente, l'equity journalisée doit être EXACTEMENT égale au cash."""
    log_file = str(tmp_path / "trades.csv")
    trader = _make_trader(
        monkeypatch, _FlickerStrategy, max_positions_per_symbol=1, log_file=log_file,
    )
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    monkeypatch.setattr(paper_trader.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))

    clock.idx += 1
    trader.run_once()  # achète
    clock.idx += 1
    trader.run_once()  # vend (sortie sur signal, systématique avec _FlickerStrategy)

    import csv
    with open(log_file) as f:
        rows = list(csv.DictReader(f))
    sell_rows = [r for r in rows if r["side"] == "sell"]
    assert sell_rows, "aucune vente enregistrée : le scénario ne teste rien"
    sell = sell_rows[-1]
    assert float(sell["equity_after"]) == pytest.approx(float(sell["cash_after"])), (
        f"equity_after ({sell['equity_after']}) devrait être égal à cash_after "
        f"({sell['cash_after']}) puisqu'aucune position n'est plus ouverte — "
        "l'écart correspond à la valeur de la position comptée deux fois"
    )


def test_position_value_falls_back_instead_of_zeroing_a_symbol_that_fails_to_fetch(monkeypatch, tmp_path):
    """Même bug corrigé côté web_app.py/run_tick : quand fetch_latest_candles
    échoue pour UN symbole (429 KuCoin), ses positions ouvertes ne doivent
    pas être comptées comme valant 0$ — ça ferait chuter l'équity perçue et
    pourrait déclencher à tort TotalDrawdownCircuitBreaker (ne se réarme
    jamais seul)."""
    trader = _make_trader(
        monkeypatch, _AlwaysEnterStrategy, symbols=("BTC/USDT", "ETH/USDT"),
        max_positions_per_symbol=1, max_total_drawdown_pct=0.20,
        log_file=str(tmp_path / "trades.csv"),
    )
    # positions déjà ouvertes, directement posées (pas besoin de rejouer
    # l'ouverture) ; plus haut historique fixé à leur valeur d'entrée.
    trader.positions = {
        "BTC/USDT": [{"qty": 5.0, "entry_price": 100.0, "stop_price": 1.0,
                      "target_price": 1_000_000.0, "regime": "trend"}],
        "ETH/USDT": [{"qty": 4.0, "entry_price": 50.0, "stop_price": 1.0,
                      "target_price": 1_000_000.0, "regime": "trend"}],
    }
    trader.cash = 100.0
    trader.total_dd_breaker._peak_equity = 800.0  # 100 + 5*100 + 4*50

    histories = {
        "BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0),
        "ETH/USDT": make_ohlcv(seed=1, n=50, start=50.0),
    }
    clock = Clock(idx=10)
    base_fetch = _fake_fetch_latest_candles(histories, clock)

    def _fetch_eth_fails(exchange, symbol, timeframe, limit=200):
        if symbol == "ETH/USDT":
            raise RuntimeError('kucoin {"code":"429000","msg":"Too many requests."}')
        return base_fetch(exchange, symbol, timeframe, limit=limit)

    monkeypatch.setattr(paper_trader.data, "fetch_latest_candles", _fetch_eth_fails)

    clock.idx += 1
    trader.run_once()  # ETH échoue ce cycle ; BTC reste proche de son prix d'entrée

    assert not trader.total_dd_breaker.tripped, (
        "ETH compté comme valant 0$ (bug) aurait fait chuter l'équity de ~310$ (-39%), "
        "largement au-delà du seuil de -20%, sans aucune vraie perte sur les deux positions"
    )
