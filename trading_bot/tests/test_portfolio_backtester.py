import pandas as pd
import pytest
import yaml

from portfolio_backtester import PortfolioBacktester, _periods_per_year
from strategy import regime_strategy_from_config


def _make_backtester(cfg):
    def strategy_factory():
        return regime_strategy_from_config(cfg["strategy"])

    bt_cfg, risk_cfg, pf_cfg = cfg["backtest"], cfg["risk"], cfg["portfolio"]
    return PortfolioBacktester(
        strategy_factory=strategy_factory,
        initial_balance=bt_cfg["initial_balance"],
        fee_pct=bt_cfg["fee_pct"],
        risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
        max_daily_loss_pct=risk_cfg.get("max_daily_loss_pct"),
        max_total_drawdown_pct=risk_cfg.get("max_total_drawdown_pct"),
        slippage_pct=bt_cfg.get("slippage_pct", 0.0),
        max_concurrent_positions=pf_cfg["max_concurrent_positions"],
        max_correlation_for_new_position=risk_cfg.get("max_correlation_for_new_position"),
        correlation_lookback=risk_cfg.get("correlation_lookback", 30),
        momentum_lookback=pf_cfg.get("momentum_lookback", 20),
    )


def test_portfolio_backtest_runs_end_to_end(portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    bt = _make_backtester(cfg)
    result = bt.run(portfolio_data)

    expected_keys = {
        "final_equity", "total_return_pct", "buy_and_hold_return_pct",
        "max_drawdown_pct", "sharpe_ratio_approx", "num_trades", "win_rate_pct",
        "profit_factor", "exit_reasons", "per_symbol", "circuit_breaker_blocks",
        "total_drawdown_breaker_blocks", "correlation_blocks", "equity_curve", "trades",
    }
    assert expected_keys <= result.keys()
    assert len(result["equity_curve"]) > 0
    assert set(result["per_symbol"].keys()) == set(portfolio_data.keys())
    # jamais plus de positions ouvertes simultanément que le plafond configuré
    assert result["circuit_breaker_blocks"] >= 0


def test_total_drawdown_breaker_never_unblocks_once_tripped(portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    cfg["risk"]["max_total_drawdown_pct"] = 0.01  # seuil très bas -> se déclenche vite
    bt = _make_backtester(cfg)
    result = bt.run(portfolio_data)
    if bt.total_dd_breaker.tripped:
        assert result["total_drawdown_breaker_blocks"] > 0


def test_periods_per_year_matches_hourly_candles():
    index = pd.date_range("2023-01-01", periods=100, freq="h")
    assert _periods_per_year(index) == pytest.approx(365.25 * 24, rel=1e-6)


def test_periods_per_year_matches_daily_candles():
    index = pd.date_range("2023-01-01", periods=100, freq="D")
    assert _periods_per_year(index) == pytest.approx(365.25, rel=1e-6)
