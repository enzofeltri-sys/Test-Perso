"""
paper_trader.py
---------------
Trading "papier" (100% virtuel) sur un PORTEFEUILLE de plusieurs paires,
avec la même logique que portfolio_backtester.py : le bot suit le marché
en direct via l'API PUBLIQUE de l'exchange (aucune clé API requise) et
simule ses propres achats/ventes, stop-loss et take-profit inclus, dans
un portefeuille fictif à capital partagé. Aucun ordre réel n'est jamais
envoyé par ce module.
"""

import csv
import os
import time
from datetime import datetime, timezone

import pandas as pd

import data
from risk import position_size, DailyLossCircuitBreaker, TotalDrawdownCircuitBreaker


class PortfolioPaperTrader:
    def __init__(self, exchange_id: str, symbols: list, timeframe: str, strategy_factory,
                 initial_balance: float, poll_interval_seconds: int, log_file: str,
                 fee_pct: float, risk_per_trade_pct: float, max_daily_loss_pct: float = None,
                 max_concurrent_positions: int = None, lookback: int = 200,
                 max_correlation_for_new_position: float = None, correlation_lookback: int = 30,
                 momentum_lookback: int = 20, max_total_drawdown_pct: float = None,
                 slippage_pct: float = 0.0, max_position_pct_of_equity: float = None,
                 max_position_notional_usd: float = None, max_positions_per_symbol: int = 1,
                 reentry_cooldown_hours: float = None):
        self.exchange = data.get_exchange(exchange_id)
        self.symbols = symbols
        self.timeframe = timeframe
        self.strategies = {s: strategy_factory() for s in symbols}
        self.poll_interval_seconds = poll_interval_seconds
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        # quantité/valeur minimum par ordre sur cet exchange (voir
        # data.get_min_order_limits) — chargé une fois, les limites de
        # marché ne changent pas assez souvent pour justifier un appel
        # à chaque cycle. Best-effort : {} si l'exchange ne répond pas.
        try:
            self.min_order_limits = data.get_min_order_limits(self.exchange, symbols)
        except Exception:
            self.min_order_limits = {}
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct_of_equity = max_position_pct_of_equity
        self.max_position_notional_usd = max_position_notional_usd
        self.max_concurrent_positions = max_concurrent_positions
        self.max_correlation_for_new_position = max_correlation_for_new_position
        self.correlation_lookback = correlation_lookback
        self.momentum_lookback = momentum_lookback
        self.max_positions_per_symbol = max_positions_per_symbol or 1
        self.reentry_cooldown_hours = reentry_cooldown_hours
        self.lookback = lookback
        self.log_file = log_file

        self.cash = initial_balance
        # {symbol: [liste de positions ouvertes sur cette paire]} — voir
        # portfolio_backtester.py pour pourquoi une liste (rachat possible)
        # et pourquoi le "regime" qui a ouvert chaque position est stocké
        # SUR la position, jamais lu depuis l'état interne partagé de la
        # stratégie du symbole.
        self.positions = {s: [] for s in symbols}
        self.last_activity = {s: None for s in symbols}  # dernier achat OU vente sur cette paire (cooldown)

        self.daily_breaker = DailyLossCircuitBreaker(max_daily_loss_pct) if max_daily_loss_pct else None
        self.total_dd_breaker = TotalDrawdownCircuitBreaker(max_total_drawdown_pct) if max_total_drawdown_pct else None

        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        if not os.path.exists(log_file):
            with open(log_file, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "symbol", "side", "price", "qty", "reason",
                                  "cash_after", "equity_after"])

    def _log_trade(self, symbol: str, side: str, price: float, qty: float, reason: str, equity: float):
        with open(self.log_file, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(), symbol, side, price, qty, reason,
                round(self.cash, 2), round(equity, 2)
            ])

    def _step(self):
        dfs = {}
        rows = {}
        for s in self.symbols:
            try:
                df = data.fetch_latest_candles(self.exchange, s, self.timeframe, limit=self.lookback)
                df = self.strategies[s].prepare(df).dropna()
                if not df.empty:
                    dfs[s] = df
                    rows[s] = df.iloc[-1]
            except Exception as e:
                print(f"Erreur en récupérant {s} (on continue avec les autres) : {e}")

        if not rows:
            print("Pas encore assez de données pour aucun symbole, on attend.")
            return

        prices = {s: rows[s]["close"] for s in rows}

        def _position_value():
            return sum(p["qty"] * prices[s] for s in rows for p in self.positions.get(s, []))

        equity = self.cash + _position_value()

        if self.daily_breaker:
            self.daily_breaker.update(datetime.now(timezone.utc), equity)
        if self.total_dd_breaker:
            self.total_dd_breaker.update(equity)

        # 1) sorties
        for s in list(rows.keys()):
            open_positions = self.positions.get(s) or []
            if not open_positions:
                continue
            row = rows[s]
            for pos in list(open_positions):
                regime = pos.get("regime")
                sub_strategy = self.strategies[s].range_strategy if regime == "range" else self.strategies[s].trend_strategy
                exit_price = exit_reason = None

                if row["low"] <= pos["stop_price"]:
                    exit_price, exit_reason = pos["stop_price"], "stop_loss"
                elif row["high"] >= pos["target_price"]:
                    exit_price, exit_reason = pos["target_price"], "take_profit"
                elif sub_strategy.should_exit_on_signal(row):
                    exit_price, exit_reason = row["close"], "signal"

                if exit_price is None:
                    continue

                # Retirer la position AVANT de calculer equity_now : sinon
                # elle est comptée deux fois (le cash de la vente déjà
                # crédité juste en dessous, ET sa valeur de marché encore
                # présente dans self.positions[s] tant que la liste n'a pas
                # été réassignée) — même bug que web_app.py/run_tick,
                # signalé sur le bot #2.
                self.positions[s] = [p for p in self.positions[s] if p is not pos]

                exit_price *= (1 - self.slippage_pct)  # on suppose une exécution légèrement défavorable
                proceeds = pos["qty"] * exit_price
                fee = proceeds * self.fee_pct
                self.cash += proceeds - fee
                self.last_activity[s] = row.name
                equity_now = self.cash + _position_value()
                self._log_trade(s, "sell", exit_price, pos["qty"], exit_reason, equity_now)
                print(f"[{datetime.now()}] VENTE ({exit_reason})  {s}  {pos['qty']:.6f} @ {exit_price:.2f}")

        # 2) entrées : coupe-circuits, puis priorisation par momentum, puis filtre de corrélation
        breaker_ok = self.daily_breaker.can_open_new_position() if self.daily_breaker else True
        dd_ok = self.total_dd_breaker.can_open_new_position() if self.total_dd_breaker else True
        open_count = sum(len(self.positions.get(s) or []) for s in self.symbols)

        if breaker_ok and dd_ok:
            candidates = []
            for s in rows:
                if len(self.positions.get(s) or []) >= self.max_positions_per_symbol:
                    continue
                if self.reentry_cooldown_hours and self.last_activity.get(s) is not None:
                    elapsed_h = (rows[s].name - self.last_activity[s]).total_seconds() / 3600
                    if elapsed_h < self.reentry_cooldown_hours:
                        continue
                if self.strategies[s].should_enter(rows[s]):
                    mom_series = dfs[s]["close"].pct_change(self.momentum_lookback)
                    mom = mom_series.iloc[-1] if len(mom_series) else 0.0
                    candidates.append((s, 0.0 if pd.isna(mom) else mom))
            candidates.sort(key=lambda item: item[1], reverse=True)

            for s, _ in candidates:
                if self.max_concurrent_positions and open_count >= self.max_concurrent_positions:
                    break

                held_symbols = [s2 for s2 in self.symbols if self.positions.get(s2)]
                if self.max_correlation_for_new_position is not None and held_symbols and s in dfs:
                    returns_s = dfs[s]["close"].pct_change().tail(self.correlation_lookback).reset_index(drop=True)
                    too_correlated = False
                    for held in held_symbols:
                        if held not in dfs:
                            continue
                        returns_held = dfs[held]["close"].pct_change().tail(self.correlation_lookback).reset_index(drop=True)
                        n = min(len(returns_s), len(returns_held))
                        if n < self.correlation_lookback:
                            continue
                        corr = returns_s.tail(n).corr(returns_held.tail(n))
                        if not pd.isna(corr) and abs(corr) > self.max_correlation_for_new_position:
                            too_correlated = True
                            break
                    if too_correlated:
                        continue

                row = rows[s]
                equity_now = self.cash + _position_value()
                fill_price = row["close"] * (1 + self.slippage_pct)  # exécution légèrement défavorable
                stop_p, target_p, stop_distance = self.strategies[s].compute_stop_and_target(fill_price, row)
                regime = self.strategies[s].active_regime
                available_cash = self.cash / (1 + self.fee_pct)
                limits = self.min_order_limits.get(s, {})
                qty = position_size(equity_now, self.risk_per_trade_pct, fill_price, stop_distance, available_cash,
                                     max_position_pct_of_equity=self.max_position_pct_of_equity,
                                     max_position_notional_usd=self.max_position_notional_usd,
                                     min_amount=limits.get("min_amount"), min_cost=limits.get("min_cost"))

                if qty > 0:
                    cost = qty * fill_price
                    fee = cost * self.fee_pct
                    self.cash -= (cost + fee)
                    self.positions.setdefault(s, []).append({
                        "qty": qty, "entry_price": fill_price, "stop_price": stop_p,
                        "target_price": target_p, "regime": regime,
                    })
                    self.last_activity[s] = row.name
                    open_count += 1
                    equity_after = self.cash + _position_value()
                    self._log_trade(s, "buy", fill_price, qty, "signal", equity_after)
                    print(f"[{datetime.now()}] ACHAT  {s}  {qty:.6f} @ {fill_price:.2f} "
                          f"(stop={stop_p:.2f}, target={target_p:.2f})")
        elif not dd_ok:
            print(f"[{datetime.now()}] coupe-circuit de DRAWDOWN TOTAL actif — le bot n'ouvrira plus "
                  f"aucune position tant qu'il n'est pas révisé manuellement")
        else:
            print(f"[{datetime.now()}] coupe-circuit de perte journalière actif — pas de nouvelle entrée")

        total_equity = self.cash + _position_value()
        # dénominateur = max_concurrent_positions s'il est défini, sinon le
        # nombre de paires — avec le rachat activé, open_count peut dépasser
        # len(symbols) (plusieurs positions sur une même paire), donc la
        # seule borne encore valide comme dénominateur est max_concurrent_positions.
        denom = self.max_concurrent_positions or len(self.symbols)
        print(f"[{datetime.now()}] équity totale = {total_equity:.2f}  "
              f"positions ouvertes = {open_count}/{denom}")

    def run_forever(self):
        print(f"Paper trading démarré sur {', '.join(self.symbols)} ({self.timeframe}), "
              f"capital virtuel initial = {self.cash:.2f}")
        print("Aucun ordre réel ne sera envoyé. Ctrl+C pour arrêter.\n")
        while True:
            try:
                self._step()
            except Exception as e:
                print(f"Erreur pendant le cycle (on continue) : {e}")
            time.sleep(self.poll_interval_seconds)

    def run_once(self):
        """Utile pour tester une itération sans boucler indéfiniment."""
        self._step()


# ---------------------------------------------------------------------------
# NOTE POUR PLUS TARD — passage au trading réel
# ---------------------------------------------------------------------------
# Ce module ne place jamais d'ordre réel. Pour passer en argent réel un jour,
# il faudra notamment :
#   1. Tester d'abord sur le "testnet" de l'exchange (ex: testnet Binance)
#      avec de fausses clés API avant toute clé réelle.
#   2. Remplacer les mises à jour de self.cash / self.positions par de vrais
#      appels ccxt authentifiés : exchange.create_market_buy_order(...) /
#      create_market_sell_order(...), avec gestion des erreurs réseau,
#      des rejets d'ordre, des limites de taux (rate limit) et des ordres
#      partiellement exécutés — pour CHAQUE paire du portefeuille.
#   3. Passer les stop-loss/take-profit en vrais ordres côté exchange (ex:
#      OCO orders si disponibles) plutôt qu'en simple vérification côté bot,
#      pour être protégé même si le bot plante ou perd la connexion.
#   4. Stocker les clés API uniquement via variables d'environnement ou un
#      gestionnaire de secrets — jamais en clair dans un fichier de config.
#   5. Surveiller le bot activement au début (alertes, logs détaillés), et
#      commencer avec un capital réel minime pendant plusieurs semaines
#      avant d'augmenter progressivement.
# Ce n'est pas quelque chose à activer sans avoir bien testé et compris
# chaque étape ci-dessus.
# ---------------------------------------------------------------------------
