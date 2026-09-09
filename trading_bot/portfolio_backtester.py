"""
portfolio_backtester.py
------------------------
Simulation multi-actifs avec un capital PARTAGÉ entre plusieurs paires,
plutôt qu'un backtest isolé par paire. L'idée : une seule paire donne un
résultat qui dépend beaucoup de la chance (le chemin précis pris par son
prix) ; répartir le capital sur plusieurs paires non parfaitement
corrélées lisse un peu ce bruit, à condition de garder un contrôle du
risque au niveau du portefeuille entier (pas seulement par trade).

Chaque symbole a sa PROPRE instance de stratégie (une stratégie à
bascule de régime garde un état interne — quelle sous-stratégie a ouvert
la position en cours — donc on ne peut pas partager une seule instance
entre plusieurs paires).

Contrôles de risque au niveau portefeuille :
  - `max_concurrent_positions` : nombre maximum de paires en position en
    même temps, pour éviter de tout miser d'un coup sur des signaux qui
    peuvent être corrélés entre cryptos (elles montent/descendent souvent
    ensemble).
  - `max_correlation_for_new_position` : garde-fou plus fin que le
    précédent — refuse une nouvelle position si elle est trop corrélée
    (corrélation glissante des rendements) avec une position déjà
    ouverte, même s'il reste des "slots" disponibles. Deux paires très
    corrélées comptent sinon comme deux positions "diversifiées" alors
    qu'elles bougent souvent ensemble.
  - priorisation par momentum : quand plusieurs paires signalent une
    entrée sur la même bougie mais qu'il n'y a pas assez de slots pour
    toutes, on privilégie celle avec le meilleur momentum récent plutôt
    que l'ordre arbitraire de la liste des symboles.
  - le coupe-circuit de perte journalière (risk.DailyLossCircuitBreaker)
    s'applique au capital TOTAL du portefeuille, pas paire par paire.
  - le coupe-circuit de drawdown TOTAL (risk.TotalDrawdownCircuitBreaker)
    arrête définitivement les nouvelles entrées si la baisse depuis le
    plus haut historique dépasse un seuil — signal qu'il faut arrêter et
    réévaluer, pas insister.
  - le dimensionnement par le risque (risk.position_size) utilise
    l'équity totale du portefeuille à l'instant t, pas juste le capital
    "alloué" à cette paire.
"""

import itertools

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from risk import position_size, DailyLossCircuitBreaker, TotalDrawdownCircuitBreaker


def _periods_per_year(index: pd.DatetimeIndex) -> float:
    """Facteur d'annualisation dérivé de l'espacement réel des bougies de la
    courbe d'équité, plutôt qu'un `sqrt(365)` supposant à tort des bougies
    journalières (le timeframe configuré peut être 1m, 1h, 4h, 1d, ...) —
    sinon le Sharpe annualisé est faussé d'un facteur potentiellement énorme."""
    if len(index) < 2:
        return 365.0
    median_seconds = pd.Series(index).diff().dt.total_seconds().median()
    if not median_seconds or median_seconds <= 0:
        return 365.0
    seconds_per_year = 365.25 * 24 * 3600
    return seconds_per_year / median_seconds


class PortfolioBacktester:
    def __init__(self, strategy_factory, initial_balance: float, fee_pct: float,
                 risk_per_trade_pct: float, max_daily_loss_pct: float = None,
                 slippage_pct: float = 0.0, max_concurrent_positions: int = None,
                 max_correlation_for_new_position: float = None, correlation_lookback: int = 30,
                 momentum_lookback: int = 20, max_total_drawdown_pct: float = None,
                 min_order_limits: dict = None, max_position_pct_of_equity: float = None):
        """
        strategy_factory: fonction sans argument qui retourne une NOUVELLE
        instance de stratégie (une par symbole, cf. docstring du module).

        min_order_limits: {symbol: {"min_amount": ..., "min_cost": ...}}
        (voir data.get_min_order_limits) — un signal qui suggère une
        position plus petite que le minimum d'ordre de l'exchange pour ce
        symbole est refusé, comme le ferait un exchange réel.
        """
        self.strategy_factory = strategy_factory
        self.initial_balance = initial_balance
        self.fee_pct = fee_pct
        self.risk_per_trade_pct = risk_per_trade_pct
        self.slippage_pct = slippage_pct
        self.max_concurrent_positions = max_concurrent_positions
        self.max_correlation_for_new_position = max_correlation_for_new_position
        self.correlation_lookback = correlation_lookback
        self.momentum_lookback = momentum_lookback
        self.min_order_limits = min_order_limits or {}
        self.max_position_pct_of_equity = max_position_pct_of_equity
        self.daily_breaker = DailyLossCircuitBreaker(max_daily_loss_pct) if max_daily_loss_pct else None
        self.total_dd_breaker = TotalDrawdownCircuitBreaker(max_total_drawdown_pct) if max_total_drawdown_pct else None

    def run(self, data: dict) -> dict:
        """data: {symbol: DataFrame OHLCV}. Les DataFrames doivent avoir un
        index temporel ; seules les dates communes à TOUS les symboles sont
        utilisées (simplification raisonnable quand les paires viennent du
        même exchange / même timeframe)."""
        symbols = list(data.keys())
        strategies = {s: self.strategy_factory() for s in symbols}

        prepared = {}
        for s in symbols:
            df = strategies[s].prepare(data[s].copy()).dropna()
            prepared[s] = df

        common_index = prepared[symbols[0]].index
        for s in symbols[1:]:
            common_index = common_index.intersection(prepared[s].index)
        common_index = common_index.sort_values()

        if len(common_index) == 0:
            raise ValueError("Aucune date commune entre les symboles du portefeuille — vérifie les données.")

        for s in symbols:
            prepared[s] = prepared[s].loc[common_index]

        # --- pré-calculs vectorisés pour la corrélation et le momentum ---
        returns_by_symbol = {s: prepared[s]["close"].pct_change() for s in symbols}
        momentum_by_symbol = {
            s: prepared[s]["close"].pct_change(self.momentum_lookback) for s in symbols
        }
        corr_lookup = {}
        if self.max_correlation_for_new_position is not None:
            for s1, s2 in itertools.combinations(symbols, 2):
                corr_lookup[frozenset((s1, s2))] = returns_by_symbol[s1].rolling(
                    self.correlation_lookback, min_periods=self.correlation_lookback
                ).corr(returns_by_symbol[s2])

        cash = self.initial_balance
        positions = {s: None for s in symbols}
        equity_curve = []
        trades = []
        breaker_blocks = 0
        total_dd_blocks = 0
        correlation_blocks = 0

        row_iterators = zip(*[prepared[s].iterrows() for s in symbols])

        for aligned in row_iterators:
            ts = aligned[0][0]
            rows = {s: aligned[i][1] for i, s in enumerate(symbols)}
            prices = {s: rows[s]["close"] for s in symbols}

            equity_now = cash + sum(
                positions[s]["qty"] * prices[s] for s in symbols if positions[s] is not None
            )
            if self.daily_breaker:
                self.daily_breaker.update(ts, equity_now)
            if self.total_dd_breaker:
                self.total_dd_breaker.update(equity_now)

            # 1) sorties (stop-loss / take-profit / signal) pour toutes les paires en position
            for s in symbols:
                pos = positions[s]
                if pos is None:
                    continue
                row = rows[s]
                exit_price = exit_reason = None

                if row["low"] <= pos["stop_price"]:
                    exit_price, exit_reason = pos["stop_price"], "stop_loss"
                elif row["high"] >= pos["target_price"]:
                    exit_price, exit_reason = pos["target_price"], "take_profit"
                elif strategies[s].should_exit_on_signal(row):
                    exit_price, exit_reason = row["close"], "signal"

                if exit_price is not None:
                    exit_price *= (1 - self.slippage_pct)
                    proceeds = pos["qty"] * exit_price
                    fee = proceeds * self.fee_pct
                    cash += proceeds - fee
                    pnl = proceeds - fee - pos["cost"]
                    trades.append({
                        "symbol": s, "entry_time": pos["entry_time"], "entry_price": pos["entry_price"],
                        "qty": pos["qty"], "exit_time": ts, "exit_price": exit_price,
                        "exit_reason": exit_reason, "pnl": pnl,
                        # quelle sous-stratégie a ouvert ce trade (trend / range) —
                        # à lire AVANT on_position_closed(), qui remet l'état à neutre
                        "regime": getattr(strategies[s], "active_regime", None),
                    })
                    positions[s] = None
                    strategies[s].on_position_closed()

            # 2) entrées : coupe-circuits d'abord, puis priorisation par momentum,
            #    puis filtre de corrélation, puis plafond de positions concurrentes
            breaker_ok = self.daily_breaker.can_open_new_position() if self.daily_breaker else True
            if not breaker_ok:
                breaker_blocks += 1
            dd_ok = self.total_dd_breaker.can_open_new_position() if self.total_dd_breaker else True
            if not dd_ok:
                total_dd_blocks += 1

            open_count = sum(1 for s in symbols if positions[s] is not None)

            if breaker_ok and dd_ok:
                # on ne consomme should_enter() qu'une fois par symbole flat, et on
                # trie les candidats par momentum décroissant avant d'allouer les slots
                candidates = []
                for s in symbols:
                    if positions[s] is not None:
                        continue
                    if strategies[s].should_enter(rows[s]):
                        mom = momentum_by_symbol[s].loc[ts]
                        candidates.append((s, 0.0 if pd.isna(mom) else mom))
                candidates.sort(key=lambda item: item[1], reverse=True)

                for s, _ in candidates:
                    if self.max_concurrent_positions and open_count >= self.max_concurrent_positions:
                        break

                    held_symbols = [s2 for s2 in symbols if positions[s2] is not None]
                    if self.max_correlation_for_new_position is not None and held_symbols:
                        corrs = []
                        for held in held_symbols:
                            c = corr_lookup.get(frozenset((s, held)))
                            val = c.loc[ts] if c is not None else np.nan
                            corrs.append(0.0 if pd.isna(val) else abs(val))
                        if max(corrs) > self.max_correlation_for_new_position:
                            correlation_blocks += 1
                            continue

                    row = rows[s]
                    equity_now = cash + sum(
                        positions[s2]["qty"] * prices[s2] for s2 in symbols if positions[s2] is not None
                    )
                    fill_price = row["close"] * (1 + self.slippage_pct)
                    stop_p, target_p, stop_distance = strategies[s].compute_stop_and_target(fill_price, row)

                    available_cash = cash / (1 + self.fee_pct)
                    limits = self.min_order_limits.get(s, {})
                    qty = position_size(equity_now, self.risk_per_trade_pct, fill_price, stop_distance, available_cash,
                                         min_amount=limits.get("min_amount"), min_cost=limits.get("min_cost"),
                                         max_position_pct_of_equity=self.max_position_pct_of_equity)

                    if qty > 0:
                        cost = qty * fill_price
                        fee = cost * self.fee_pct
                        cash -= (cost + fee)
                        positions[s] = {
                            "qty": qty, "entry_price": fill_price, "entry_time": ts,
                            "stop_price": stop_p, "target_price": target_p, "cost": cost + fee,
                        }
                        open_count += 1

            total_equity = cash + sum(
                positions[s]["qty"] * prices[s] for s in symbols if positions[s] is not None
            )
            equity_curve.append({"timestamp": ts, "equity": total_equity})

        equity_df = pd.DataFrame(equity_curve).set_index("timestamp")

        bh_return_pct = self._buy_and_hold_return(prepared, symbols)

        return self._compute_metrics(equity_df, trades, breaker_blocks, total_dd_blocks,
                                      correlation_blocks, bh_return_pct, symbols)

    @staticmethod
    def _buy_and_hold_return(prepared: dict, symbols: list) -> float:
        """Rendement d'un portefeuille équipondéré acheté au début et gardé jusqu'à la fin."""
        rets = []
        for s in symbols:
            close = prepared[s]["close"]
            rets.append(close.iloc[-1] / close.iloc[0] - 1)
        return float(np.mean(rets) * 100)

    def _compute_metrics(self, equity_df: pd.DataFrame, trades: list, breaker_blocks: int,
                          total_dd_blocks: int, correlation_blocks: int,
                          bh_return_pct: float, symbols: list) -> dict:
        equity = equity_df["equity"]
        final_equity = equity.iloc[-1] if len(equity) else self.initial_balance
        total_return_pct = (final_equity / self.initial_balance - 1) * 100

        running_max = equity.cummax()
        drawdown = (equity - running_max) / running_max
        max_drawdown_pct = drawdown.min() * 100 if len(drawdown) else 0

        returns = equity.pct_change().dropna()
        sharpe = (returns.mean() / returns.std() * np.sqrt(_periods_per_year(equity.index))) if returns.std() > 0 else 0

        pnls = [t["pnl"] for t in trades]
        num_trades = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = (len(wins) / num_trades * 100) if num_trades else 0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf") if wins else 0

        exit_reasons = {}
        per_symbol = {s: {"num_trades": 0, "pnl": 0.0} for s in symbols}
        per_regime = {}
        for t in trades:
            exit_reasons[t["exit_reason"]] = exit_reasons.get(t["exit_reason"], 0) + 1
            per_symbol[t["symbol"]]["num_trades"] += 1
            per_symbol[t["symbol"]]["pnl"] += t["pnl"]
            regime = t.get("regime") or "unknown"
            bucket = per_regime.setdefault(regime, {"num_trades": 0, "pnl": 0.0})
            bucket["num_trades"] += 1
            bucket["pnl"] += t["pnl"]

        return {
            "final_equity": final_equity,
            "total_return_pct": total_return_pct,
            "buy_and_hold_return_pct": bh_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "sharpe_ratio_approx": sharpe,
            "num_trades": num_trades,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "exit_reasons": exit_reasons,
            "per_symbol": per_symbol,
            "per_regime": per_regime,   # P&L par sous-stratégie (trend / range) — laquelle perd ?
            "circuit_breaker_blocks": breaker_blocks,
            "total_drawdown_breaker_blocks": total_dd_blocks,
            "correlation_blocks": correlation_blocks,
            "equity_curve": equity_df,
            "trades": trades,
        }

    @staticmethod
    def plot_equity_curve(equity_curve: pd.DataFrame, initial_balance: float, out_path: str):
        fig, ax1 = plt.subplots(figsize=(11, 5))
        ax1.plot(equity_curve.index, equity_curve["equity"], color="#2563eb", label="Équity du portefeuille")
        ax1.axhline(initial_balance, color="gray", linestyle="--", linewidth=1, label="Capital initial")
        ax1.set_ylabel("Équity (USDT)")
        ax1.set_xlabel("Date")
        ax1.legend(loc="upper left")
        ax1.set_title("Courbe d'équité — backtest portefeuille multi-actifs")
        fig.tight_layout()
        fig.savefig(out_path, dpi=140)
        plt.close(fig)
