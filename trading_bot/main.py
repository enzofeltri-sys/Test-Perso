"""
main.py
-------
Point d'entrée en ligne de commande.

Usage :
    python main.py backtest [--config config.yaml]
    python main.py validate [--config config.yaml]
    python main.py paper    [--config config.yaml] [--once]
"""

import argparse
import sys

import yaml

import data
from strategy import regime_strategy_from_config
from portfolio_backtester import PortfolioBacktester
from paper_trader import PortfolioPaperTrader
from walk_forward import walk_forward_analysis, aggregate_walk_forward, print_windows
from monte_carlo import bootstrap_trade_returns, plot_distribution


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _fetch_portfolio_history(cfg: dict) -> dict:
    ex_cfg = cfg["exchange"]
    bt_cfg = cfg["backtest"]
    # exchange.history_exchange_id (si présent) sert UNIQUEMENT à l'historique
    # profond (backtest/validate/recalibrate.py) — l'exchange live (exchange.id,
    # web_app.py/paper_trader.py) n'est pas concerné. Voir config.yaml.
    exchange_id = ex_cfg.get("history_exchange_id") or ex_cfg["id"]
    exchange = data.get_exchange(exchange_id)

    result = {}
    for symbol in cfg["portfolio"]["symbols"]:
        print(f"Téléchargement de l'historique {symbol} ({ex_cfg['timeframe']}) "
              f"sur {bt_cfg['since_days']} jours depuis {exchange_id}...")
        df = data.fetch_ohlcv_history(exchange, symbol, ex_cfg["timeframe"], bt_cfg["since_days"])
        print(f"  -> {len(df)} bougies ({df.index[0]} -> {df.index[-1]})")
        result[symbol] = df
    return result


def cmd_backtest(cfg: dict):
    bt_cfg = cfg["backtest"]
    risk_cfg = cfg["risk"]
    pf_cfg = cfg["portfolio"]

    market_data = _fetch_portfolio_history(cfg)

    # quantité/valeur minimum par ordre sur l'exchange configuré (voir
    # data.get_min_order_limits) — un backtest qui ignorerait ces
    # planchers surestimerait ce qu'un exchange réel aurait accepté.
    try:
        exchange = data.get_exchange(cfg["exchange"]["id"])
        min_order_limits = data.get_min_order_limits(exchange, pf_cfg["symbols"])
    except Exception:
        min_order_limits = {}

    def strategy_factory():
        return regime_strategy_from_config(cfg["strategy"])

    bt = PortfolioBacktester(
        strategy_factory=strategy_factory,
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
        min_order_limits=min_order_limits,
    )
    results = bt.run(market_data)

    print("\n===== Résultats du backtest (portefeuille) =====")
    print(f"Paires                      : {', '.join(pf_cfg['symbols'])}")
    print(f"Capital initial             : {bt_cfg['initial_balance']:.2f}")
    print(f"Capital final               : {results['final_equity']:.2f}")
    print(f"Rendement stratégie         : {results['total_return_pct']:.2f} %")
    print(f"Rendement buy & hold (moy.) : {results['buy_and_hold_return_pct']:.2f} %")
    print(f"Drawdown maximum            : {results['max_drawdown_pct']:.2f} %")
    print(f"Sharpe (approx., annualisé) : {results['sharpe_ratio_approx']:.2f}")
    print(f"Nombre de trades            : {results['num_trades']}")
    print(f"Taux de trades gagnants     : {results['win_rate_pct']:.2f} %")
    print(f"Profit factor               : {results['profit_factor']:.2f}")
    print(f"Sorties par raison          : {results['exit_reasons']}")
    print(f"Résultat par paire          : {results['per_symbol']}")
    print(f"Entrées bloquées (coupe-circuit journalier)   : {results['circuit_breaker_blocks']}")
    print(f"Entrées bloquées (coupe-circuit drawdown total) : {results['total_drawdown_breaker_blocks']}")
    print(f"Entrées bloquées (trop corrélées)             : {results['correlation_blocks']}")

    out_path = "backtest_equity_curve.png"
    PortfolioBacktester.plot_equity_curve(results["equity_curve"], bt_cfg["initial_balance"], out_path)
    print(f"\nGraphique sauvegardé : {out_path}")

    return results


def cmd_validate(cfg: dict):
    bt_cfg = cfg["backtest"]
    risk_cfg = cfg["risk"]
    pf_cfg = cfg["portfolio"]
    wf_cfg = cfg["walk_forward"]
    mc_cfg = cfg["monte_carlo"]

    try:
        exchange = data.get_exchange(cfg["exchange"]["id"])
        min_order_limits = data.get_min_order_limits(exchange, pf_cfg["symbols"])
    except Exception:
        min_order_limits = {}

    # Mêmes garde-fous portefeuille (anti-corrélation, priorisation par momentum,
    # coupe-circuits partagés) que cmd_backtest / le paper trading réel : le
    # walk-forward doit valider CE portefeuille-là, pas une paire isolée.
    portfolio_kwargs = dict(
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
        min_order_limits=min_order_limits,
    )

    market_data = _fetch_portfolio_history(cfg)

    print("\n===== Validation walk-forward (portefeuille complet) =====")
    print("Seuls les résultats HORS-ÉCHANTILLON (test) sont agrégés ci-dessous — voir README.\n")
    windows = walk_forward_analysis(
        market_data, cfg["strategy"], wf_cfg["param_grid"], portfolio_kwargs,
        train_days=wf_cfg["train_days"], test_days=wf_cfg["test_days"], step_days=wf_cfg["step_days"],
    )
    agg = aggregate_walk_forward(windows)
    if agg.get("num_windows", 0) == 0:
        print("  Pas assez d'historique pour au moins une fenêtre train/test complète.")
    else:
        print("  Détail par fenêtre (test hors-échantillon) :")
        print_windows(windows)
        print()
        print(f"  Fenêtres testées            : {agg['num_windows']}")
        print(f"  Rendement OOS composé       : {agg['compounded_oos_return_pct']:.2f} %")
        print(f"  Buy & hold sur ces fenêtres : {agg['compounded_buy_and_hold_pct']:.2f} %")
        print(f"  Sharpe OOS moyen            : {agg['avg_oos_sharpe']:.2f}")
        print(f"  Drawdown OOS moyen          : {agg['avg_oos_max_drawdown_pct']:.2f} %")
        print(f"  Pire drawdown OOS observé   : {agg['worst_oos_max_drawdown_pct']:.2f} %")
        print(f"  % de fenêtres positives     : {agg['pct_windows_positive']:.1f} %")
        print(f"  Trades hors-échantillon     : {agg['total_oos_trades']}")
        print(f"  Taux de gain global OOS     : {agg['overall_win_rate_pct']:.1f} %")
        print(f"  Sorties par raison (OOS)    : {agg['exit_reasons']}")
    print()

    print("===== Backtest portefeuille + analyse Monte Carlo =====")
    results = cmd_backtest(cfg)
    pnls = [t["pnl"] for t in results["trades"]]

    mc = bootstrap_trade_returns(pnls, initial_balance=bt_cfg["initial_balance"],
                                  num_simulations=mc_cfg["num_simulations"])
    if mc.get("num_simulations", 0) == 0:
        print("Pas assez de trades pour une analyse Monte Carlo.")
        return

    print(f"\n--- Distribution Monte Carlo sur {mc['num_simulations']} simulations "
          f"({mc['num_trades_per_sim']} trades ré-échantillonnés) ---")
    print(f"Rendement final : 5e percentile = {mc['return_pct_p5']:.1f} %  |  "
          f"médiane = {mc['return_pct_p50']:.1f} %  |  95e percentile = {mc['return_pct_p95']:.1f} %")
    print(f"Probabilité de finir en perte sur ce jeu de trades : {mc['prob_of_loss_pct']:.1f} %")
    print(f"Drawdown : pire cas (5e pct) = {mc['max_drawdown_p5_worst_case']:.1f} %  |  "
          f"médian = {mc['max_drawdown_p50_median']:.1f} %")

    plot_distribution(mc["_final_returns_array"], "monte_carlo_distribution.png")
    print("\nGraphique sauvegardé : monte_carlo_distribution.png")


def cmd_paper(cfg: dict, run_once: bool):
    ex_cfg = cfg["exchange"]
    pf_cfg = cfg["portfolio"]
    pt_cfg = cfg["paper_trading"]
    risk_cfg = cfg["risk"]

    if cfg.get("mode") == "live" or cfg.get("live_trading", {}).get("enabled"):
        print("Ce script ne place jamais d'ordres réels, quelle que soit la config.")
        print("Le mode 'live' n'est pas implémenté ici par sécurité — voir README.md.")
        sys.exit(1)

    def strategy_factory():
        return regime_strategy_from_config(cfg["strategy"])

    trader = PortfolioPaperTrader(
        exchange_id=ex_cfg["id"],
        symbols=pf_cfg["symbols"],
        timeframe=ex_cfg["timeframe"],
        strategy_factory=strategy_factory,
        initial_balance=pt_cfg["initial_balance"],
        poll_interval_seconds=pt_cfg["poll_interval_seconds"],
        log_file=pt_cfg["log_file"],
        fee_pct=cfg["backtest"].get("fee_pct", 0.001),
        slippage_pct=cfg["backtest"].get("slippage_pct", 0.0),
        risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
        max_daily_loss_pct=risk_cfg.get("max_daily_loss_pct"),
        max_concurrent_positions=pf_cfg.get("max_concurrent_positions"),
        lookback=pt_cfg.get("lookback_candles", 200),
        max_correlation_for_new_position=risk_cfg.get("max_correlation_for_new_position"),
        correlation_lookback=risk_cfg.get("correlation_lookback", 30),
        momentum_lookback=pf_cfg.get("momentum_lookback", 20),
        max_total_drawdown_pct=risk_cfg.get("max_total_drawdown_pct"),
        max_position_pct_of_equity=risk_cfg.get("max_position_pct_of_equity"),
        max_position_notional_usd=risk_cfg.get("max_position_notional_usd"),
    )

    if run_once:
        trader.run_once()
    else:
        trader.run_forever()


def main():
    parser = argparse.ArgumentParser(description="Bot de trading (backtest / validation / paper trading)")
    parser.add_argument("command", choices=["backtest", "validate", "paper"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="paper trading: une seule itération (pour tester)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.command == "backtest":
        cmd_backtest(cfg)
    elif args.command == "validate":
        cmd_validate(cfg)
    elif args.command == "paper":
        cmd_paper(cfg, run_once=args.once)


if __name__ == "__main__":
    main()
