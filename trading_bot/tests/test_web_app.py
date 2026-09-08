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

import pytest
import yaml

import web_app
from conftest import make_ohlcv


class FakeDB:
    def __init__(self, overrides=None):
        self.state = None
        self.trades = []
        self.errors = []
        self.overrides = overrides or {}

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

    def load_config_overrides(self):
        return self.overrides

    def get_recent_trades(self, limit=10):
        return list(reversed(self.trades))[:limit]

    def get_recent_errors(self, limit=5):
        return []


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


def test_run_tick_applies_slippage_to_fills(monkeypatch):
    """Le paper trading doit se comporter comme si c'était de l'argent réel :
    portfolio_backtester.py simule un glissement de prix à chaque exécution
    (slippage_pct, voir config.yaml) — run_tick() doit faire pareil, sinon
    le bot déployé obtient des remplissages parfaits qu'aucune exécution
    réelle n'obtiendrait, et les résultats affichés sont trop optimistes."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    slippage_pct = cfg["backtest"]["slippage_pct"]
    assert slippage_pct > 0, "slippage_pct doit être > 0 dans config.yaml pour que ce test prouve quelque chose"

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)

    checked_at_least_one_fill = False
    seen = 0
    for _ in range(60):
        clock.idx += 5
        web_app.run_tick()
        for t in fake_db.trades[seen:]:
            raw_close = float(histories[t["symbol"]]["close"].iloc[clock.idx])
            if t["side"] == "buy":
                expected = raw_close * (1 + slippage_pct)
            elif t["reason"] == "signal":
                expected = raw_close * (1 - slippage_pct)
            else:
                continue  # stop_loss/take_profit : le prix de référence n'est pas le close brut
            assert t["price"] == pytest.approx(expected, rel=1e-9), (
                f"{t['symbol']} {t['side']} ({t['reason']}) : {t['price']} != {expected} attendu "
                "avec le glissement appliqué"
            )
            checked_at_least_one_fill = True
        seen = len(fake_db.trades)

    assert checked_at_least_one_fill, "aucun trade au close exact à vérifier sur 60 ticks — ajuster seeds/n"


def test_run_tick_survives_min_order_limits_lookup_failure(monkeypatch):
    """data.get_min_order_limits() fait un appel réseau (load_markets) — s'il
    échoue, /tick doit continuer à fonctionner sans plancher connu plutôt que
    de planter le cycle entier pour une info secondaire."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    def _boom(exchange, symbols):
        raise RuntimeError("exchange indisponible")

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app.data, "get_min_order_limits", _boom)
    monkeypatch.setattr(web_app, "db", fake_db)

    clock.idx += 5
    result = web_app.run_tick()
    assert result["ok"] is True


def test_run_tick_rejects_entries_below_exchange_min_order(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    huge_limits = {s: {"min_amount": None, "min_cost": 10_000_000.0} for s in symbols}

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app.data, "get_min_order_limits", lambda exchange, syms: huge_limits)
    monkeypatch.setattr(web_app, "db", fake_db)

    for _ in range(60):
        clock.idx += 5
        web_app.run_tick()

    assert not fake_db.trades, "aucun ordre ne devrait jamais passer sous le minimum de l'exchange"


def test_run_tick_applies_strategy_overrides_before_constructing_strategies(monkeypatch):
    """recalibrate.py écrit les paramètres gagnants dans
    tradingbot_config.strategy_overrides — run_tick() doit les appliquer
    par-dessus config.yaml AVANT de construire les RegimeSwitchingStrategy,
    pas après (sinon la recalibration n'a aucun effet)."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    original_min_score = cfg["strategy"]["trend"]["min_score_to_enter"]
    original_adx = cfg["strategy"]["regime"]["adx_trend_threshold"]

    strategy_overrides = {"trend.min_score_to_enter": original_min_score + 1,
                           "regime.adx_trend_threshold": original_adx + 15}

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB(overrides={"strategy_overrides": strategy_overrides})

    seen_cfgs = []
    real_factory = web_app.regime_strategy_from_config

    def _spy_factory(strategy_cfg):
        seen_cfgs.append(strategy_cfg)
        return real_factory(strategy_cfg)

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)
    monkeypatch.setattr(web_app, "regime_strategy_from_config", _spy_factory)

    clock.idx += 5
    result = web_app.run_tick()

    assert seen_cfgs, "regime_strategy_from_config n'a jamais été appelé"
    for strat_cfg in seen_cfgs:
        assert strat_cfg["trend"]["min_score_to_enter"] == original_min_score + 1
        assert strat_cfg["regime"]["adx_trend_threshold"] == original_adx + 15
    assert result["active_strategy_overrides"] == strategy_overrides
    assert result["config_overrides_active"] is True


def test_run_tick_uses_config_yaml_strategy_when_no_strategy_overrides(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()  # pas d'overrides

    seen_cfgs = []
    real_factory = web_app.regime_strategy_from_config

    def _spy_factory(strategy_cfg):
        seen_cfgs.append(strategy_cfg)
        return real_factory(strategy_cfg)

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)
    monkeypatch.setattr(web_app, "regime_strategy_from_config", _spy_factory)

    clock.idx += 5
    result = web_app.run_tick()

    for strat_cfg in seen_cfgs:
        assert strat_cfg["trend"]["min_score_to_enter"] == cfg["strategy"]["trend"]["min_score_to_enter"]
    assert result["active_strategy_overrides"] == {}


def test_apply_config_overrides_defaults_to_config_yaml_when_no_override():
    risk_cfg = {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03}
    pf_cfg = {"symbols": ["BTC/USDT", "ETH/USDT"], "max_concurrent_positions": 2}

    active = web_app._apply_config_overrides(risk_cfg, pf_cfg, overrides={})

    assert risk_cfg["risk_per_trade_pct"] == 0.01  # inchangé
    assert active == {"BTC/USDT", "ETH/USDT"}  # toutes les paires par défaut


def test_apply_config_overrides_applies_overrides():
    risk_cfg = {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03,
                "correlation_lookback": 30}
    pf_cfg = {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"], "max_concurrent_positions": 2}

    active = web_app._apply_config_overrides(risk_cfg, pf_cfg, overrides={
        "risk_per_trade_pct": 0.005,
        "max_concurrent_positions": 1,
        "active_symbols": ["BTC/USDT"],
    })

    assert risk_cfg["risk_per_trade_pct"] == 0.005
    assert risk_cfg["max_daily_loss_pct"] == 0.03  # non surchargé, inchangé
    assert pf_cfg["max_concurrent_positions"] == 1
    assert active == {"BTC/USDT"}


def test_run_tick_never_opens_new_positions_on_symbols_deactivated_by_override(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    active_symbol = symbols[0]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB(overrides={"active_symbols": [active_symbol]})

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)

    for _ in range(60):
        clock.idx += 5
        result = web_app.run_tick()
        assert result["ok"] is True
        assert result["active_symbols"] == [active_symbol]
        assert result["config_overrides_active"] is True

        positions = fake_db.state["positions"] if fake_db.state else {}
        for symbol, pos in positions.items():
            if pos is not None:
                assert symbol == active_symbol, (
                    f"{symbol} a une position ouverte alors qu'il est désactivé par active_symbols"
                )
