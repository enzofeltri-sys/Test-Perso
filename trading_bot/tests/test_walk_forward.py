import yaml

from walk_forward import walk_forward_analysis, aggregate_walk_forward, _split_train_test


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
