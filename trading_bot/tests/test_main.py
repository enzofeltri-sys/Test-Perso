"""
_fetch_portfolio_history() doit utiliser exchange.history_exchange_id
pour l'historique profond (backtest/validate/recalibrate.py) quand il
est présent, et se rabattre sur exchange.id sinon — voir config.yaml :
kucoin (l'exchange live) plafonne son historique 1h public à ~83 jours
quel que soit since_days demandé, insuffisant pour walk_forward.
train_days + test_days (vérifié empiriquement, voir la conversation).
"""

import pandas as pd

import main as main_module


def _fake_df():
    idx = pd.date_range("2023-01-01", periods=5, freq="h")
    return pd.DataFrame({"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}, index=idx)


def test_fetch_portfolio_history_prefers_history_exchange_id(monkeypatch):
    requested_ids = []
    monkeypatch.setattr(main_module.data, "get_exchange", lambda exchange_id: requested_ids.append(exchange_id) or object())
    monkeypatch.setattr(main_module.data, "fetch_ohlcv_history", lambda exchange, symbol, timeframe, since_days: _fake_df())

    cfg = {
        "exchange": {"id": "kucoin", "history_exchange_id": "binance", "timeframe": "1h"},
        "backtest": {"since_days": 365},
        "portfolio": {"symbols": ["BTC/USDT"]},
    }
    main_module._fetch_portfolio_history(cfg)

    assert requested_ids == ["binance"]


def test_fetch_portfolio_history_falls_back_to_live_exchange_id(monkeypatch):
    requested_ids = []
    monkeypatch.setattr(main_module.data, "get_exchange", lambda exchange_id: requested_ids.append(exchange_id) or object())
    monkeypatch.setattr(main_module.data, "fetch_ohlcv_history", lambda exchange, symbol, timeframe, since_days: _fake_df())

    cfg = {
        "exchange": {"id": "kucoin", "timeframe": "1h"},  # pas de history_exchange_id
        "backtest": {"since_days": 365},
        "portfolio": {"symbols": ["BTC/USDT"]},
    }
    main_module._fetch_portfolio_history(cfg)

    assert requested_ids == ["kucoin"]
