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
from flask import Flask, jsonify, render_template_string

import data
import supabase_state as db
from strategy import regime_strategy_from_config
from risk import position_size, DailyLossCircuitBreaker, TotalDrawdownCircuitBreaker
from walk_forward import _apply_overrides as _apply_strategy_param_overrides

app = Flask(__name__)

STATUS_PAGE = """<!doctype html>
<title>Journal de bord — bot de trading</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,500&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
  :root{
    --bg:#F5F5F3; --text:#1C1C1A; --text-muted:#767671; --text-faint:#A5A59F;
    --rule:#DBDBD6; --accent:#96622A; --green:#3E7A52; --red:#A3453A;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --bg:#17181A; --text:#E7E6E1; --text-muted:#8E8E88; --text-faint:#5C5D59;
      --rule:#333432; --accent:#CB9855; --green:#6FAE87; --red:#D08076;
    }
  }
  *{ box-sizing:border-box; margin:0; }
  body{
    background:var(--bg); color:var(--text);
    font-family:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
    line-height:1.55;
  }
  .wrap{ max-width:600px; margin:0 auto; padding:72px 24px 96px; }
  .mono{ font-family:"IBM Plex Mono", ui-monospace, monospace; }
  header{ margin-bottom:56px; }
  .kicker{
    font-family:"IBM Plex Mono", monospace; font-size:0.72rem; letter-spacing:0.14em;
    text-transform:uppercase; color:var(--accent); margin:0 0 14px;
  }
  h1{
    font-family:"Fraunces", Georgia, serif; font-weight:500; font-size:2.1rem;
    letter-spacing:-0.01em; line-height:1.08; margin:0 0 16px; text-wrap:balance;
  }
  .status-line{ display:flex; align-items:baseline; gap:9px; font-size:0.88rem; color:var(--text-muted); }
  .status-line .dot{ width:6px; height:6px; border-radius:50%; display:inline-block; }
  .status-line .dot.ok{ background:var(--green); }
  .status-line .dot.bad{ background:var(--red); }
  .status-line strong{ color:var(--text); font-weight:500; }
  section{ padding:30px 0; border-top:1px solid var(--rule); }
  section > .label{
    font-family:"IBM Plex Mono", monospace; font-size:0.7rem; letter-spacing:0.1em;
    text-transform:uppercase; color:var(--text-faint); margin:0 0 20px;
  }
  .stats{ display:grid; grid-template-columns:1fr 1fr; row-gap:20px; }
  .stat .n{ font-size:0.78rem; color:var(--text-muted); margin-bottom:3px; }
  .stat .v{ font-family:"IBM Plex Mono", monospace; font-variant-numeric:tabular-nums; font-size:1.1rem; }
  .stat .v.zero{ color:var(--text-faint); }
  .stat .v.bad{ color:var(--red); }
  .row{ display:flex; justify-content:space-between; align-items:baseline; gap:16px; padding:11px 0; }
  .row + .row{ border-top:1px solid var(--rule); }
  .row .name{ font-weight:500; font-size:0.9rem; }
  .row .detail{ font-size:0.79rem; color:var(--text-muted); margin-top:2px; }
  .row .side{ font-family:"IBM Plex Mono", monospace; font-size:0.74rem; white-space:nowrap; flex-shrink:0; }
  .row .side.buy{ color:var(--green); }
  .row .side.sell{ color:var(--red); }
  .empty{ font-size:0.85rem; color:var(--text-faint); }
  footer{ padding-top:30px; border-top:1px solid var(--rule); font-size:0.82rem; color:var(--text-muted); }
  footer a{ color:var(--accent); }

  #pull-indicator{
    position:fixed; top:0; left:0; right:0; z-index:10;
    display:flex; align-items:center; justify-content:center;
    height:52px; margin-top:-52px;
    font-family:"IBM Plex Mono", monospace; font-size:0.72rem;
    letter-spacing:0.08em; text-transform:uppercase; color:var(--text-muted);
    transition:transform 0.15s ease-out;
    pointer-events:none;
  }
  #pull-indicator.armed{ color:var(--accent); }
  @media (prefers-reduced-motion: reduce){ #pull-indicator{ transition:none; } }
</style>

<div id="pull-indicator">tirer pour rafraîchir</div>

<div class="wrap">
  <header>
    <p class="kicker">Bot de trading crypto — paper trading</p>
    <h1>Journal de bord</h1>
    <p class="status-line">
      <span class="dot {{ 'ok' if healthy else 'bad' }}"></span>
      <strong>{{ 'En ligne' if healthy else 'Coupe-circuit déclenché' }}</strong>
      — {{ symbols|join(' · ') }}
    </p>
  </header>

  <section>
    <p class="label">État actuel</p>
    <div class="stats">
      <div class="stat"><div class="n">Cash</div><div class="v">${{ '%.2f'|format(cash) }}</div></div>
      <div class="stat"><div class="n">Positions ouvertes</div><div class="v {{ 'zero' if not positions else '' }}">{{ positions|length }}</div></div>
      <div class="stat"><div class="n">Coupe-circuit jour</div><div class="v {{ 'bad' if daily_tripped else '' }}">{{ 'déclenché' if daily_tripped else 'ok' }}</div></div>
      <div class="stat"><div class="n">Coupe-circuit total</div><div class="v {{ 'bad' if total_tripped else '' }}">{{ 'déclenché' if total_tripped else 'ok' }}</div></div>
      <div class="stat"><div class="n">Stratégie</div><div class="v {{ '' if strategy_overrides else 'zero' }}" style="font-size:0.95rem;">{{ 'recalibrée (' ~ strategy_overrides|length ~ ' réglage' ~ ('s' if strategy_overrides|length > 1 else '') ~ ')' if strategy_overrides else 'config.yaml' }}</div></div>
    </div>
  </section>

  <section>
    <p class="label">Positions ouvertes</p>
    {% if positions %}
      {% for p in positions %}
      <div class="row">
        <div>
          <div class="name">{{ p.symbol }}</div>
          <div class="detail">{{ '%.6f'|format(p.qty) }} @ ${{ '%.2f'|format(p.entry_price) }} · stop ${{ '%.2f'|format(p.stop_price) }} · target ${{ '%.2f'|format(p.target_price) }}</div>
        </div>
        <span class="side mono">{{ p.active_substrategy or '—' }}</span>
      </div>
      {% endfor %}
    {% else %}
      <p class="empty">Aucune position ouverte en ce moment.</p>
    {% endif %}
  </section>

  <section>
    <p class="label">Derniers trades</p>
    {% if trades %}
      {% for t in trades %}
      <div class="row">
        <div>
          <div class="name">{{ t.symbol }}</div>
          <div class="detail">{{ t.ts }} · {{ t.reason }} · {{ '%.6f'|format(t.qty) }} @ ${{ '%.2f'|format(t.price) }}</div>
        </div>
        <span class="side mono {{ t.side }}">{{ t.side }}</span>
      </div>
      {% endfor %}
    {% else %}
      <p class="empty">Aucun trade pour l'instant.</p>
    {% endif %}
  </section>

  <section>
    <p class="label">Journal</p>
    {% if journal %}
      {% for j in journal %}
      <div class="row">
        <div>
          <div class="detail">{{ j.ts }}</div>
          <div class="name" style="font-size:0.82rem;font-weight:400;">{{ j.message }}</div>
        </div>
        <span class="side mono {{ 'buy' if j.author == 'manager' else '' }}">{{ j.author }}</span>
      </div>
      {% endfor %}
    {% else %}
      <p class="empty">Aucune entrée pour l'instant.</p>
    {% endif %}
  </section>

  {% if errors %}
  <section>
    <p class="label">Dernières erreurs</p>
    {% for e in errors %}
    <div class="row">
      <div>
        <div class="detail">{{ e.ts }}</div>
        <div class="name" style="font-size:0.82rem;font-weight:400;">{{ e.message }}</div>
      </div>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  <footer>
    <p>Tire vers le bas en haut de la page pour rafraîchir · <a href="/tick">/tick</a> déclenche un cycle manuellement (UptimeRobot le fait déjà toutes les 5 min).</p>
  </footer>
</div>

<script>
(function () {
  // "Tirer pour rafraîchir" en JS : nécessaire dès que la page est ouverte
  // en PWA/écran d'accueil (pas de barre de navigateur pour tirer dessus),
  // et fonctionne aussi dans un onglet de navigateur classique.
  var indicator = document.getElementById("pull-indicator");
  var THRESHOLD = 70;
  var startY = null;
  var pulling = false;

  document.addEventListener("touchstart", function (e) {
    if (window.scrollY <= 0) {
      startY = e.touches[0].clientY;
      pulling = true;
    }
  }, { passive: true });

  document.addEventListener("touchmove", function (e) {
    if (!pulling || startY === null) return;
    var dy = e.touches[0].clientY - startY;
    if (dy <= 0) { indicator.style.transform = ""; indicator.classList.remove("armed"); return; }
    var pull = Math.min(dy, THRESHOLD * 1.6);
    indicator.style.transform = "translateY(" + pull + "px)";
    indicator.textContent = dy > THRESHOLD ? "relâcher pour rafraîchir" : "tirer pour rafraîchir";
    indicator.classList.toggle("armed", dy > THRESHOLD);
  }, { passive: true });

  document.addEventListener("touchend", function (e) {
    if (!pulling || startY === null) return;
    var dy = (e.changedTouches[0].clientY - startY);
    pulling = false;
    startY = null;
    if (dy > THRESHOLD) {
      indicator.textContent = "actualisation…";
      window.location.reload();
    } else {
      indicator.style.transform = "";
      indicator.classList.remove("armed");
    }
  });
})();
</script>
"""


def load_config() -> dict:
    path = os.environ.get("BOT_CONFIG", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _apply_config_overrides(risk_cfg: dict, pf_cfg: dict, overrides: dict) -> set:
    """Applique les réglages posés dans tradingbot_config par-dessus
    config.yaml (une valeur nulle/absente = on garde celle de
    config.yaml). Retourne l'ensemble des symboles autorisés à ouvrir de
    NOUVELLES positions — les positions déjà ouvertes sur une paire
    désactivée restent gérées normalement (voir run_tick)."""
    for key in ("risk_per_trade_pct", "max_daily_loss_pct", "max_total_drawdown_pct",
                "max_correlation_for_new_position"):
        if overrides.get(key) is not None:
            risk_cfg[key] = float(overrides[key])

    if overrides.get("correlation_lookback") is not None:
        risk_cfg["correlation_lookback"] = int(overrides["correlation_lookback"])

    if overrides.get("max_concurrent_positions") is not None:
        pf_cfg["max_concurrent_positions"] = int(overrides["max_concurrent_positions"])
    if overrides.get("momentum_lookback") is not None:
        pf_cfg["momentum_lookback"] = int(overrides["momentum_lookback"])

    active = overrides.get("active_symbols")
    return set(active) if active else set(pf_cfg["symbols"])


def _apply_strategy_overrides(base_strategy_cfg: dict, strategy_overrides: dict) -> dict:
    """Applique les paramètres de stratégie recalibrés (voir
    recalibrate.py) par-dessus config.yaml — mêmes clés pointées
    ("trend.min_score_to_enter") que le walk-forward, puisque c'est
    justement ce format que recalibrate.py écrit dans
    tradingbot_config.strategy_overrides. Contrairement aux réglages de
    risque (_apply_config_overrides), ceux-ci ne sont JAMAIS touchés à la
    main — uniquement par recalibrate.py, sous condition de robustesse."""
    return _apply_strategy_param_overrides(base_strategy_cfg, strategy_overrides or {})


def run_tick() -> dict:
    cfg = load_config()
    pf_cfg = cfg["portfolio"]
    risk_cfg = cfg["risk"]
    pt_cfg = cfg["paper_trading"]
    ex_cfg = cfg["exchange"]
    fee_pct = cfg["backtest"].get("fee_pct", 0.001)
    slippage_pct = cfg["backtest"].get("slippage_pct", 0.0)

    overrides = db.load_config_overrides()
    active_symbols = _apply_config_overrides(risk_cfg, pf_cfg, overrides)
    strategy_cfg = _apply_strategy_overrides(cfg["strategy"], overrides.get("strategy_overrides"))

    symbols = pf_cfg["symbols"]
    exchange = data.get_exchange(ex_cfg["id"])
    # quantité/valeur minimum par ordre sur cet exchange (voir
    # data.get_min_order_limits) — best-effort, {} si l'appel échoue
    # (comportement identique à avant ce contrôle, pas de plancher).
    try:
        min_order_limits = data.get_min_order_limits(exchange, symbols)
    except Exception:
        min_order_limits = {}
    # une instance de stratégie par paire, reconstruite à chaque appel
    # (aucun état interne des stratégies n'est réutilisé d'un tick à
    # l'autre : la sous-stratégie active pour une position ouverte est
    # stockée dans `positions[symbol]["active_substrategy"]`, pas dans
    # l'objet stratégie — voir plus bas).
    strategies = {s: regime_strategy_from_config(strategy_cfg) for s in symbols}

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
            exit_price *= (1 - slippage_pct)  # on suppose une exécution légèrement défavorable
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
            if s not in active_symbols:
                continue  # désactivée via tradingbot_config.active_symbols
            if strategies[s].should_enter(rows[s]):
                mom_series = dfs[s]["close"].pct_change(momentum_lookback)
                mom = mom_series.iloc[-1] if len(mom_series) else 0.0
                candidates.append((s, 0.0 if pd.isna(mom) else mom))
        candidates.sort(key=lambda item: item[1], reverse=True)

        for s, _ in candidates:
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
            fill_price = row["close"] * (1 + slippage_pct)  # exécution légèrement défavorable
            # compute_stop_and_target() sur le wrapper RegimeSwitchingStrategy
            # confirme le régime actif (should_enter() ne fait que le PROPOSER,
            # voir strategy.py) -> on le lit juste après pour le figer dans la
            # position, puisque l'état interne de la stratégie n'est pas
            # reconduit d'un tick à l'autre (processus stateless, voir plus haut).
            stop_p, target_p, stop_distance = strategies[s].compute_stop_and_target(fill_price, row)
            active = strategies[s].active_regime
            equity_now = cash + sum(positions[s2]["qty"] * prices[s2] for s2 in rows if positions.get(s2))
            available_cash = cash / (1 + fee_pct)
            limits = min_order_limits.get(s, {})
            qty = position_size(equity_now, risk_cfg["risk_per_trade_pct"], fill_price, stop_distance, available_cash,
                                 min_amount=limits.get("min_amount"), min_cost=limits.get("min_cost"))

            if qty > 0:
                cost = qty * fill_price
                fee = cost * fee_pct
                cash -= (cost + fee)
                positions[s] = {
                    "qty": qty, "entry_price": fill_price, "stop_price": stop_p,
                    "target_price": target_p, "active_substrategy": active,
                }
                open_count += 1
                equity_after = cash + sum(positions[s2]["qty"] * prices[s2] for s2 in rows if positions.get(s2))
                db.log_trade(s, "buy", fill_price, qty, "signal", cash, equity_after)
                trades_this_tick.append({"symbol": s, "side": "buy", "price": fill_price, "reason": "signal"})

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
        "active_symbols": sorted(active_symbols),
        "config_overrides_active": any(
            overrides.get(k) is not None for k in (
                "risk_per_trade_pct", "max_daily_loss_pct", "max_total_drawdown_pct",
                "max_correlation_for_new_position", "correlation_lookback",
                "momentum_lookback", "max_concurrent_positions", "active_symbols",
                "strategy_overrides",
            )
        ),
        "active_strategy_overrides": overrides.get("strategy_overrides") or {},
    }


def _fmt_ts(ts_str):
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%d/%m %H:%M UTC")
    except (ValueError, TypeError):
        return ts_str


@app.route("/")
def health():
    # Page de statut lisible pour Render (vérifie que le service répond) et
    # pour un humain qui ouvre l'URL — /tick reste l'endpoint que ping
    # UptimeRobot pour déclencher un cycle, pas celui-ci. Ne doit JAMAIS
    # faire échouer le health-check : un souci Supabase retombe sur le
    # texte brut d'origine plutôt que de planter la page.
    try:
        cfg = load_config()
        pt_cfg = cfg["paper_trading"]

        state = db.load_state(initial_balance=pt_cfg["initial_balance"])
        trades = db.get_recent_trades(limit=8)
        errors = db.get_recent_errors(limit=5)
        journal = db.get_recent_journal(limit=8)
        # load_config_overrides() est déjà best-effort ({} si Supabase
        # est injoignable ou si la table n'existe pas) — voir supabase_state.py.
        strategy_overrides = db.load_config_overrides().get("strategy_overrides") or {}

        positions = [{"symbol": s, **p} for s, p in state["positions"].items() if p]
        daily_tripped = bool(state.get("daily_tripped_today"))
        total_tripped = bool(state.get("total_dd_tripped"))

        trades_view = [{
            "ts": _fmt_ts(t.get("ts")), "symbol": t.get("symbol"), "side": t.get("side"),
            "price": t.get("price"), "qty": t.get("qty"), "reason": t.get("reason"),
        } for t in trades]
        errors_view = [{"ts": _fmt_ts(e.get("ts")), "message": e.get("message")} for e in errors]
        journal_view = [{
            "ts": _fmt_ts(j.get("ts")), "author": j.get("author"), "message": j.get("message"),
        } for j in journal]

        return render_template_string(
            STATUS_PAGE,
            healthy=not (daily_tripped or total_tripped),
            daily_tripped=daily_tripped, total_tripped=total_tripped,
            cash=state["cash"], positions=positions,
            symbols=cfg["portfolio"]["symbols"],
            trades=trades_view, errors=errors_view, journal=journal_view,
            strategy_overrides=strategy_overrides,
        ), 200
    except Exception:
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
