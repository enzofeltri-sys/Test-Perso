import json

import numpy as np
import pandas as pd
import yaml

from walk_forward import (walk_forward_analysis, aggregate_walk_forward, _split_train_test,
                          summarize_windows, print_windows)


def _portfolio_kwargs(cfg):
    bt_cfg, risk_cfg, pf_cfg = cfg["backtest"], cfg["risk"], cfg["portfolio"]
    return dict(
        initial_balance=bt_cfg["initial_balance"],
        fee_pct=bt_cfg["fee_pct"],
        risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
        max_daily_loss_pct=risk_cfg.get("max_daily_loss_pct"),
        slippage_pct=bt_cfg.get("slippage_pct", 0.0),
        max_concurrent_positions=pf_cfg.get("max_concurrent_positions"),
        max_correlation_for_new_position=risk_cfg.get("max_correlation_for_new_position"),
        correlation_lookback=risk_cfg.get("correlation_lookback", 30),
        momentum_lookback=pf_cfg.get("momentum_lookback", 20),
        max_total_drawdown_pct=risk_cfg.get("max_total_drawdown_pct"),
        max_position_pct_of_equity=risk_cfg.get("max_position_pct_of_equity"),
        max_position_notional_usd=risk_cfg.get("max_position_notional_usd"),
    )


def test_split_train_test_has_no_overlap(portfolio_data):
    df = portfolio_data["BTC/USDT"]
    train_start, train_end = df.index[0], df.index[300]
    test_start, test_end = train_end, df.index[400]

    train_data, test_data = _split_train_test(portfolio_data, train_start, train_end, test_start, test_end)

    for symbol in portfolio_data:
        overlap = set(train_data[symbol].index) & set(test_data[symbol].index)
        assert overlap == set(), f"{symbol}: {len(overlap)} bougies partagées entre train et test"
        assert len(train_data[symbol]) > 0
        assert len(test_data[symbol]) > 0


def test_walk_forward_analysis_produces_non_overlapping_oos_windows(portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    windows = walk_forward_analysis(
        portfolio_data, cfg["strategy"], cfg["walk_forward"]["param_grid"],
        _portfolio_kwargs(cfg), train_days=30, test_days=10, step_days=10,
    )

    assert len(windows) > 0
    for w in windows:
        assert w["test_start"] == w["train_end"]
        assert w["test_end"] > w["test_start"]
        assert w["test_num_trades"] >= 0

    agg = aggregate_walk_forward(windows)
    assert agg["num_windows"] == len(windows)
    assert agg["total_oos_trades"] == sum(w["test_num_trades"] for w in windows)


def test_walk_forward_windows_carry_diagnostics(portfolio_data):
    """Sans le POURQUOI (buy&hold, raisons de sortie, résultat par paire),
    une fenêtre perdante ne dit pas si c'est le marché ou la stratégie."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    windows = walk_forward_analysis(
        portfolio_data, cfg["strategy"], {}, _portfolio_kwargs(cfg),
        train_days=30, test_days=10, step_days=10,
    )
    assert windows
    for w in windows:
        assert "test_buy_and_hold_return_pct" in w
        assert "test_profit_factor" in w
        assert isinstance(w["test_exit_reasons"], dict)
        assert set(w["test_per_symbol"]) == set(portfolio_data)
        assert sum(w["test_exit_reasons"].values()) == w["test_num_trades"]

    agg = aggregate_walk_forward(windows)
    assert "compounded_buy_and_hold_pct" in agg
    assert 0.0 <= agg["overall_win_rate_pct"] <= 100.0
    assert sum(agg["exit_reasons"].values()) == agg["total_oos_trades"]


def test_summarize_windows_is_json_serialisable(capsys):
    """Le résumé part dans le journal (jsonb) : Timestamps -> dates ISO,
    profit factor infini (aucune perte) -> null, jamais une exception."""
    windows = [{
        "train_start": pd.Timestamp("2026-01-01"), "train_end": pd.Timestamp("2026-04-01"),
        "test_start": pd.Timestamp("2026-04-01"), "test_end": pd.Timestamp("2026-05-01"),
        "chosen_params": {"trend.min_score_to_enter": 3},
        "test_return_pct": np.float64(1.234567),
        "test_sharpe": 0.5, "test_max_drawdown_pct": -3.0,
        "test_num_trades": np.int64(4), "test_win_rate_pct": 100.0,
        "test_buy_and_hold_return_pct": -2.0,
        "test_profit_factor": np.inf,
        "test_exit_reasons": {"target": 4},
        "test_per_symbol": {"BTC/USDT": {"pnl": np.float64(12.345), "trades": 4}},
    }]

    summary = summarize_windows(windows)
    text = json.dumps(summary)  # ne doit pas lever
    assert "Infinity" not in text and "NaN" not in text
    assert summary[0]["test_start"] == "2026-04-01"
    assert summary[0]["profit_factor"] is None
    assert summary[0]["return_pct"] == 1.23
    assert summary[0]["per_symbol_pnl"] == {"BTC/USDT": 12.35}

    print_windows(windows)
    out = capsys.readouterr().out
    assert "2026-04-01 -> 2026-05-01" in out
    assert "target=4" in out


def test_aggregate_tolerates_windows_without_diagnostics():
    """Compatibilité : d'anciennes fenêtres (ou des tests) sans les clés de
    diagnostic ne doivent pas casser l'agrégation."""
    windows = [{
        "test_return_pct": 2.0, "test_sharpe": 1.0, "test_max_drawdown_pct": -1.0,
        "test_num_trades": 0, "test_win_rate_pct": 0.0,
    }]
    agg = aggregate_walk_forward(windows)
    assert agg["compounded_buy_and_hold_pct"] == 0.0
    assert agg["overall_win_rate_pct"] == 0.0
    assert agg["exit_reasons"] == {}


def test_walk_forward_analysis_selects_from_param_grid(portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    param_grid = {"trend.min_score_to_enter": [2, 3]}
    windows = walk_forward_analysis(
        portfolio_data, cfg["strategy"], param_grid, _portfolio_kwargs(cfg),
        train_days=30, test_days=10, step_days=10,
    )
    for w in windows:
        assert w["chosen_params"]["trend.min_score_to_enter"] in (2, 3)
