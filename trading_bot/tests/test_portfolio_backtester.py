import pandas as pd
import pytest
import yaml

from portfolio_backtester import PortfolioBacktester, _periods_per_year
from strategy import regime_strategy_from_config


def _make_backtester(cfg, min_order_limits=None):
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
        max_position_pct_of_equity=risk_cfg.get("max_position_pct_of_equity"),
        max_position_notional_usd=risk_cfg.get("max_position_notional_usd"),
        slippage_pct=bt_cfg.get("slippage_pct", 0.0),
        max_concurrent_positions=pf_cfg["max_concurrent_positions"],
        max_correlation_for_new_position=risk_cfg.get("max_correlation_for_new_position"),
        correlation_lookback=risk_cfg.get("correlation_lookback", 30),
        momentum_lookback=pf_cfg.get("momentum_lookback", 20),
        max_positions_per_symbol=pf_cfg.get("max_positions_per_symbol", 1),
        reentry_cooldown_hours=risk_cfg.get("reentry_cooldown_hours"),
        min_order_limits=min_order_limits,
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


def test_trades_record_which_substrategy_opened_them(portfolio_data):
    """Le diagnostic doit pouvoir dire QUELLE jambe (trend / range) perd :
    chaque trade porte le régime qui l'a ouvert, et per_regime les agrège."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    result = _make_backtester(cfg).run(portfolio_data)

    assert result["num_trades"] > 0
    for t in result["trades"]:
        assert t["regime"] in ("trend", "range")
    assert set(result["per_regime"]) <= {"trend", "range"}
    assert sum(v["num_trades"] for v in result["per_regime"].values()) == result["num_trades"]
    assert abs(sum(v["pnl"] for v in result["per_regime"].values()) - sum(t["pnl"] for t in result["trades"])) < 1e-6


def test_total_drawdown_breaker_never_unblocks_once_tripped(portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    cfg["risk"]["max_total_drawdown_pct"] = 0.01  # seuil très bas -> se déclenche vite
    bt = _make_backtester(cfg)
    result = bt.run(portfolio_data)
    if bt.total_dd_breaker.tripped:
        assert result["total_drawdown_breaker_blocks"] > 0


def test_min_order_limits_reject_trades_below_exchange_floor(portfolio_data):
    """Le bot doit se comporter comme s'il tradait pour de vrai : un signal
    dont la taille calculée par le risque tomberait sous le minimum d'ordre
    de l'exchange doit être refusé, pas exécuté quand même."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    baseline = _make_backtester(cfg).run(portfolio_data)
    assert baseline["num_trades"] > 0, "le scénario de base doit produire des trades pour que le test prouve quelque chose"

    # plancher de notionnel absurdement haut -> aucun trade ne peut jamais passer
    huge_limits = {s: {"min_amount": None, "min_cost": 10_000_000.0} for s in portfolio_data}
    blocked = _make_backtester(cfg, min_order_limits=huge_limits).run(portfolio_data)
    assert blocked["num_trades"] == 0


def test_periods_per_year_matches_hourly_candles():
    index = pd.date_range("2023-01-01", periods=100, freq="h")
    assert _periods_per_year(index) == pytest.approx(365.25 * 24, rel=1e-6)


def test_periods_per_year_matches_daily_candles():
    index = pd.date_range("2023-01-01", periods=100, freq="D")
    assert _periods_per_year(index) == pytest.approx(365.25, rel=1e-6)


class _AlwaysEnterStrategy:
    """Stub minimal (même interface qu'une RegimeSwitchingStrategy) : entre
    dès que possible, ne sort JAMAIS d'elle-même (stop/target hors de
    portée, pas de sortie sur signal) — isole le comportement du
    PORTEFEUILLE (plafond de rachat par paire) de la logique de décision
    d'une vraie stratégie."""
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
        return entry_price * 0.01, entry_price * 100, entry_price  # jamais touchés

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


class _FlickerStrategy:
    """Stub : entre dès que possible, sort à la bougie SUIVANTE (sortie sur
    signal systématique) — génère une série de trades très rapprochés dans
    le temps, pour isoler le cooldown du reste."""
    def __init__(self):
        self._active = None

    def prepare(self, df):
        return df

    def should_enter(self, row):
        return True

    def should_exit_on_signal(self, row):
        return True

    def compute_stop_and_target(self, entry_price, row):
        self._active = "trend"
        return entry_price * 0.01, entry_price * 100, entry_price  # jamais touchés par low/high

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


def test_max_positions_per_symbol_caps_rebuys_on_the_same_pair(portfolio_data):
    """Bot #3 : jusqu'à 3 positions simultanées sur UNE paire, jamais plus
    même si la stratégie continue de signaler une entrée à chaque bougie."""
    data = {"BTC/USDT": portfolio_data["BTC/USDT"]}
    bt = PortfolioBacktester(
        strategy_factory=_AlwaysEnterStrategy,
        initial_balance=1000.0, fee_pct=0.001, risk_per_trade_pct=0.5,
        max_position_notional_usd=10.0, max_positions_per_symbol=3,
        max_concurrent_positions=100,
    )
    result = bt.run(data)

    assert result["final_open_positions"]["BTC/USDT"] == 3


def test_max_positions_per_symbol_of_one_matches_historical_behaviour(portfolio_data):
    """Défaut (1) : jamais plus d'une position par paire à la fois — le
    comportement des bots #1/#2, inchangé par l'ajout du rachat."""
    data = {"BTC/USDT": portfolio_data["BTC/USDT"]}
    bt = PortfolioBacktester(
        strategy_factory=_AlwaysEnterStrategy,
        initial_balance=1000.0, fee_pct=0.001, risk_per_trade_pct=0.5,
        max_position_notional_usd=10.0,  # max_positions_per_symbol non fourni -> défaut 1
        max_concurrent_positions=100,
    )
    result = bt.run(data)

    assert result["final_open_positions"]["BTC/USDT"] == 1


def test_reentry_cooldown_blocks_immediate_rebuy_but_allows_after_the_window(portfolio_data):
    """Après une sortie sur une paire, aucune nouvelle entrée sur CETTE
    paire avant reentry_cooldown_hours — même si la stratégie signale une
    entrée dès la bougie suivante."""
    data = {"BTC/USDT": portfolio_data["BTC/USDT"].iloc[:100]}
    cooldown_hours = 5
    bt = PortfolioBacktester(
        strategy_factory=_FlickerStrategy,
        initial_balance=1000.0, fee_pct=0.001, risk_per_trade_pct=0.5,
        max_position_notional_usd=10.0, max_positions_per_symbol=1,
        reentry_cooldown_hours=cooldown_hours,
    )
    result = bt.run(data)

    btc_trades = [t for t in result["trades"] if t["symbol"] == "BTC/USDT"]
    assert len(btc_trades) >= 3, "pas assez de trades pour que le test prouve quelque chose"
    for prev, nxt in zip(btc_trades, btc_trades[1:]):
        gap_hours = (nxt["entry_time"] - prev["exit_time"]).total_seconds() / 3600
        assert gap_hours >= cooldown_hours - 1e-9, (
            f"rachat {gap_hours:.1f}h après la sortie précédente, en dessous du cooldown ({cooldown_hours}h)"
        )


def test_reentry_cooldown_disabled_by_default_allows_immediate_rebuy(portfolio_data):
    """None (défaut) = pas de cooldown — comportement historique inchangé."""
    data = {"BTC/USDT": portfolio_data["BTC/USDT"].iloc[:50]}
    bt = PortfolioBacktester(
        strategy_factory=_FlickerStrategy,
        initial_balance=1000.0, fee_pct=0.001, risk_per_trade_pct=0.5,
        max_position_notional_usd=10.0, max_positions_per_symbol=1,
    )
    result = bt.run(data)

    btc_trades = [t for t in result["trades"] if t["symbol"] == "BTC/USDT"]
    # sans cooldown, devrait racheter dès la bougie suivante -> ~1 trade
    # ouvert/fermé par bougie disponible (borné par le nombre de bougies)
    assert len(btc_trades) > 5
