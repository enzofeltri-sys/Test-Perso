"""
web_app.py
----------
Point d'entrée alternatif à paper_trader.py, pensé pour un hébergement
GRATUIT qui met le service en veille par inactivité (ex: Render free
tier), réveillé par un ping HTTP périodique externe (ex: UptimeRobot,
toutes les 5 minutes) plutôt que par une boucle `while True` qui doit
tourner sans interruption.

Différence fondamentale avec paper_trader.py : ici, RIEN n'est gardé en
mémoire du process entre deux appels — le process peut être redémarré à
tout moment par l'hébergeur. Chaque appel à /tick :
  1. charge l'état du portefeuille (cash, positions, coupe-circuits)
     depuis Supabase,
  2. récupère les dernières bougies de chaque paire du portefeuille,
  3. applique EXACTEMENT la même logique que paper_trader.py (sorties,
     coupe-circuits journalier + drawdown total, anti-corrélation,
     priorisation des entrées par momentum),
  4. sauvegarde le nouvel état + journalise les trades dans Supabase,
  5. répond avec un résumé JSON (toujours HTTP 200, même en cas
     d'erreur interne, pour ne pas fausser le monitoring UptimeRobot —
     l'erreur est loguée séparément pour le point de supervision
     quotidien).

Aucun ordre réel n'est jamais envoyé par ce module. Variables
d'environnement requises : voir supabase_state.py et render.yaml.
"""

import os
import traceback
from datetime import datetime, timezone

import pandas as pd
import yaml
from flask import Flask, jsonify

import data
import supabase_state as db
from strategy import regime_strategy_from_config
from risk import position_size, DailyLossCircuitBreaker, TotalDrawdownCircuitBreaker

app = Flask(__name__)


def load_config() -> dict:
    path = os.environ.get("BOT_CONFIG", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def run_tick() -> dict:
    cfg = load_config()
    pf_cfg = cfg["portfolio"]
    risk_cfg = cfg["risk"]
    pt_cfg = cfg["paper_trading"]
    ex_cfg = cfg["exchange"]
    fee_pct = cfg["backtest"].get("fee_pct", 0.001)

    symbols = pf_cfg["symbols"]
    exchange = data.get_exchange(ex_cfg["id"])
    # une instance de stratégie par paire, reconstruite à chaque appel
    # (aucun état interne des stratégies n'est réutilisé d'un tick à
    # l'autre : la sous-stratégie active pour une position ouverte est
    # stockée dans `positions[symbol]["active_substrategy"]`, pas dans
    # l'objet stratégie — voir plus bas).
    strategies = {s: regime_strategy_from_config(cfg["strategy"]) for s in symbols}

    state = db.load_state(initial_balance=pt_cfg["initial_balance"])
    cash = state["cash"]
    positions = state["positions"]

    daily_breaker = DailyLossCircuitBreaker(risk_cfg["max_daily_loss_pct"])
    daily_breaker._current_day = state["daily_current_day"]
    daily_breaker._equity_at_day_start = state["daily_equity_at_day_start"]
    daily_breaker._tripped_today = state["daily_tripped_today"]

    total_dd_breaker = TotalDrawdownCircuitBreaker(risk_cfg["max_total_drawdown_pct"])
    total_dd_breaker._peak_equity = state["total_dd_peak_equity"]
    total_dd_breaker._tripped = state["total_dd_tripped"]

    dfs, rows, fetch_errors = {}, {}, []
    for s in symbols:
        try:
            df = data.fetch_latest_candles(exchange, s, ex_cfg["timeframe"],
                                            limit=pt_cfg.get("lookback_candles", 200))
            df = strategies[s].prepare(df).dropna()
            if not df.empty:
                dfs[s] = df
                rows[s] = df.iloc[-1]
        except Exception as e:
            fetch_errors.append(f"{s}: {e}")

    for msg in fetch_errors:
        db.log_error(f"Erreur récupération des données — {msg}")

    if not rows:
        return {"ok": True, "note": "pas assez de données ce cycle", "errors": fetch_errors}

    prices = {s: rows[s]["close"] for s in rows}
    equity = cash + sum(positions[s]["qty"] * prices[s] for s in rows if positions.get(s))

    now = datetime.now(timezone.utc)
    daily_breaker.update(now, equity)
    total_dd_breaker.update(equity)

    trades_this_tick = []

    # 1) sorties — on utilise la sous-stratégie qui avait ouvert la
    #    position (mémorisée dans `positions[s]`), pas l'état interne
    #    (volatile) de l'objet RegimeSwitchingStrategy.
    for s in list(rows.keys()):
        pos = positions.get(s)
        if not pos:
            continue
        row = rows[s]
        active = pos.get("active_substrategy", "trend")
        sub_strategy = strategies[s].trend_strategy if active == "trend" else strategies[s].range_strategy

        exit_price = exit_reason = None
        if row["low"] <= pos["stop_price"]:
            exit_price, exit_reason = pos["stop_price"], "stop_loss"
        elif row["high"] >= pos["target_price"]:
            exit_price, exit_reason = pos["target_price"], "take_profit"
        elif sub_strategy.should_exit_on_signal(row):
            exit_price, exit_reason = row["close"], "signal"

        if exit_price is not None:
            proceeds = pos["qty"] * exit_price
            fee = proceeds * fee_pct
            cash += proceeds - fee
            positions[s] = None
            equity_now = cash + sum(positions[s2]["qty"] * prices[s2] for s2 in rows if positions.get(s2))
            db.log_trade(s, "sell", exit_price, pos["qty"], exit_reason, cash, equity_now)
            trades_this_tick.append({"symbol": s, "side": "sell", "price": exit_price, "reason": exit_reason})

    # 2) entrées — coupe-circuits, puis priorisation par momentum, puis
    #    filtre anti-corrélation (identique à paper_trader.py).
    breaker_ok = daily_breaker.can_open_new_position()
    dd_ok = total_dd_breaker.can_open_new_position()
    max_concurrent = pf_cfg.get("max_concurrent_positions")
    open_count = sum(1 for s in symbols if positions.get(s))

    if breaker_ok and dd_ok:
        momentum_lookback = pf_cfg.get("momentum_lookback", 20)
        correlation_lookback = risk_cfg.get("correlation_lookback", 30)
        max_corr = risk_cfg.get("max_correlation_for_new_position")

        candidates = []
        for s in rows:
            if positions.get(s):
                continue
            if strategies[s].should_enter(rows[s]):
                mom_series = dfs[s]["close"].pct_change(momentum_lookback)
                mom = mom_series.iloc[-1] if len(mom_series) else 0.0
                # should_enter() vient de mettre à jour l'état interne de
                # la stratégie -> on le lit tout de suite et on le fige
                # dans le tuple candidat (jamais relu plus tard).
                active = strategies[s].active_regime
                candidates.append((s, 0.0 if pd.isna(mom) else mom, active))
        candidates.sort(key=lambda item: item[1], reverse=True)

        for s, _, active in candidates:
            if max_concurrent and open_count >= max_concurrent:
                break

            held_symbols = [s2 for s2 in symbols if positions.get(s2)]
            if max_corr is not None and held_symbols and s in dfs:
                returns_s = dfs[s]["close"].pct_change().tail(correlation_lookback).reset_index(drop=True)
                too_correlated = False
                for held in held_symbols:
                    if held not in dfs:
                        continue
                    returns_held = dfs[held]["close"].pct_change().tail(correlation_lookback).reset_index(drop=True)
                    n = min(len(returns_s), len(returns_held))
                    if n < correlation_lookback:
                        continue
                    corr = returns_s.tail(n).corr(returns_held.tail(n))
                    if not pd.isna(corr) and abs(corr) > max_corr:
                        too_correlated = True
                        break
                if too_correlated:
                    continue

            row = rows[s]
            price = row["close"]
            sub_strategy = strategies[s].trend_strategy if active == "trend" else strategies[s].range_strategy
            stop_p, target_p, stop_distance = sub_strategy.compute_stop_and_target(price, row)
            equity_now = cash + sum(positions[s2]["qty"] * prices[s2] for s2 in rows if positions.get(s2))
            available_cash = cash / (1 + fee_pct)
            qty = position_size(equity_now, risk_cfg["risk_per_trade_pct"], price, stop_distance, available_cash)

            if qty > 0:
                cost = qty * price
                fee = cost * fee_pct
                cash -= (cost + fee)
                positions[s] = {
                    "qty": qty, "entry_price": price, "stop_price": stop_p,
                    "target_price": target_p, "active_substrategy": active,
                }
                open_count += 1
                equity_after = cash + sum(positions[s2]["qty"] * prices[s2] for s2 in rows if positions.get(s2))
                db.log_trade(s, "buy", price, qty, "signal", cash, equity_after)
                trades_this_tick.append({"symbol": s, "side": "buy", "price": price, "reason": "signal"})

    total_equity = cash + sum(positions[s]["qty"] * prices[s] for s in rows if positions.get(s))

    db.save_state({
        "cash": cash,
        "positions": positions,
        "daily_current_day": daily_breaker._current_day,
        "daily_equity_at_day_start": daily_breaker._equity_at_day_start,
        "daily_tripped_today": daily_breaker._tripped_today,
        "total_dd_peak_equity": total_dd_breaker._peak_equity,
        "total_dd_tripped": total_dd_breaker._tripped,
    })

    return {
        "ok": True,
        "timestamp": now.isoformat(),
        "equity": round(total_equity, 2),
        "cash": round(cash, 2),
        "open_positions": open_count,
        "trades_this_tick": trades_this_tick,
        "daily_breaker_tripped": daily_breaker._tripped_today,
        "total_drawdown_breaker_tripped": total_dd_breaker._tripped,
        "errors": fetch_errors,
    }


@app.route("/")
def health():
    # endpoint minimal pour Render (vérifie que le service répond) et
    # pour un ping UptimeRobot "simple" qui ne doit PAS déclencher de
    # cycle de trading (voir /tick pour ça).
    return "OK - bot de paper trading en ligne. Utilise /tick pour déclencher un cycle.", 200


@app.route("/tick")
def tick():
    try:
        result = run_tick()
        return jsonify(result), 200
    except Exception as e:
        db.log_error(f"Erreur non gérée dans /tick : {e}\n{traceback.format_exc()}")
        # HTTP 200 volontaire : une erreur ici ne doit pas faire croire à
        # UptimeRobot que le service est "down" et générer des alertes
        # inutiles — l'erreur est déjà tracée pour le point de
        # supervision quotidien.
        return jsonify({"ok": False, "error": str(e)}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
