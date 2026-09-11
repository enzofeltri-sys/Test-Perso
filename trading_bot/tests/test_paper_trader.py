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
