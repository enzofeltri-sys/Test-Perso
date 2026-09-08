"""
backtester.py
-------------
Moteur de simulation bar-par-bar avec état (position, stop-loss,
take-profit persistants entre les bougies) — nécessaire car, contrairement
à un simple croisement de moyennes, le stop-loss/take-profit dépendent du
prix d'entrée du trade en cours, pas seulement de la bougie du moment.

Hypothèses simplificatrices à garder en tête :
  - Le stop-loss et le take-profit sont vérifiés sur le high/low de
    chaque bougie (plus réaliste qu'une exécution au close uniquement),
    mais si les deux sont touchés dans la même bougie, on suppose que le
    stop-loss est déclenché en premier (hypothèse conservatrice — on ne
    sait pas dans quel ordre c'est arrivé dans la bougie).
  - Pas de slippage, pas de délai d'exécution réseau.
  - Un seul actif, long-only, pas de levier.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from risk import position_size, DailyLossCircuitBreaker


class Backtester:
    def __init__(self, strategy, initial_balance: float, fee_pct: float,
                 risk_per_trade_pct: float, max_daily_loss_pct: float = None,
                 slippage_pct: float = 0.0):
        self.strategy = strategy
        self.initial_balance = initial_balance
        self.fee_pct = fee_pct
        self.risk_per_trade_pct = risk_per_trade_pct
        self.slippage_pct = slippage_pct
        self.circuit_breaker = DailyLossCircuitBreaker(max_daily_loss_pct) if max_daily_loss_pct else None

    def run(self, df: pd.DataFrame) -> dict:
        df = self.strategy.prepare(df)
        # les premières lignes n'ont pas encore d'indicateurs valides (NaN, période de
        # "chauffe" des moyennes/ATR/ADX) : on ne trade pas dessus, quelle que soit la stratégie
        ready = df.dropna()

        cash = self.initial_balance
        coins = 0.0
        position = 0
        entry_price = stop_price = target_price = None

        equity_curve = []
        trades = []
        current_trade = None
        breaker_blocks = 0

        for ts, row in ready.iterrows():
            price = row["close"]
            equity_before = cash + coins * price

            if self.circuit_breaker:
                self.circuit_breaker.update(ts, equity_before)

            # 1) gérer une position ouverte : stop-loss / take-profit / sortie sur signal
            if position == 1:
                exit_price = None
                exit_reason = None

                if row["low"] <= stop_price:
                    exit_price, exit_reason = stop_price, "stop_loss"
                elif row["high"] >= target_price:
                    exit_price, exit_reason = target_price, "take_profit"
                elif self.strategy.should_exit_on_signal(row):
                    exit_price, exit_reason = price, "signal"

                if exit_price is not None:
                    exit_price *= (1 - self.slippage_pct)  # on suppose une exécution légèrement défavorable
                    proceeds = coins * exit_price
                    fee = proceeds * self.fee_pct
                    cash += proceeds - fee
                    pnl = proceeds - fee - current_trade["cost"]
                    current_trade.update({
                        "exit_time": ts, "exit_price": exit_price,
                        "exit_reason": exit_reason, "pnl": pnl,
                    })
                    trades.append(current_trade)
                    current_trade = None
                    coins = 0.0
                    position = 0
                    self.strategy.on_position_closed()

            # 2) chercher une nouvelle entrée si on est à plat
            if position == 0:
                breaker_ok = self.circuit_breaker.can_open_new_position() if self.circuit_breaker else True
                if not breaker_ok:
                    breaker_blocks += 1

                if breaker_ok and self.strategy.should_enter(row):
                    equity = cash + coins * price
                    fill_price = price * (1 + self.slippage_pct)  # exécution légèrement défavorable
                    stop_p, target_p, stop_distance = self.strategy.compute_stop_and_target(fill_price, row)

                    available_cash_for_sizing = cash / (1 + self.fee_pct)
                    qty = position_size(equity, self.risk_per_trade_pct, fill_price, stop_distance, available_cash_for_sizing)

                    if qty > 0:
                        cost = qty * fill_price
                        fee = cost * self.fee_pct
                        cash -= (cost + fee)
                        coins += qty
                        position = 1
                        entry_price, stop_price, target_price = fill_price, stop_p, target_p
                        current_trade = {
                            "entry_time": ts, "entry_price": fill_price, "qty": qty,
                            "stop_price": stop_p, "target_price": target_p,
                            "cost": cost + fee,
                        }

            equity_curve.append(cash + coins * price)

        ready = ready.copy()
        ready["equity"] = equity_curve

        return self._compute_metrics(ready, trades, breaker_blocks)

    def _compute_metrics(self, df: pd.DataFrame, trades: list, breaker_blocks: int) -> dict:
        equity = df["equity"]
        final_equity = equity.iloc[-1] if len(equity) else self.initial_balance
        total_return_pct = (final_equity / self.initial_balance - 1) * 100
        bh_return_pct = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100 if len(df) else 0

        running_max = equity.cummax()
        drawdown = (equity - running_max) / running_max
        max_drawdown_pct = drawdown.min() * 100 if len(drawdown) else 0

        returns = equity.pct_change().dropna()
        sharpe = (returns.mean() / returns.std() * np.sqrt(365)) if returns.std() > 0 else 0

        pnls = [t["pnl"] for t in trades if "pnl" in t]
        num_trades = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = (len(wins) / num_trades * 100) if num_trades else 0
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf") if wins else 0

        exit_reasons = {}
        for t in trades:
            r = t.get("exit_reason", "unknown")
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

        return {
            "final_equity": final_equity,
            "total_return_pct": total_return_pct,
            "buy_and_hold_return_pct": bh_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "sharpe_ratio_approx": sharpe,
            "num_trades": num_trades,
            "win_rate_pct": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "exit_reasons": exit_reasons,
            "circuit_breaker_blocks": breaker_blocks,
            "equity_curve": df[["equity"]],
            "trades": trades,
        }

    @staticmethod
    def plot_equity_curve(equity_curve: pd.DataFrame, initial_balance: float, out_path: str):
        fig, ax1 = plt.subplots(figsize=(11, 5))
        ax1.plot(equity_curve.index, equity_curve["equity"], color="#2563eb", label="Équity du bot")
        ax1.axhline(initial_balance, color="gray", linestyle="--", linewidth=1, label="Capital initial")
        ax1.set_ylabel("Équity (USDT)")
        ax1.set_xlabel("Date")
        ax1.legend(loc="upper left")
        ax1.set_title("Courbe d'équité — backtest (multi-signaux + gestion du risque)")
        fig.tight_layout()
        fig.savefig(out_path, dpi=140)
        plt.close(fig)
