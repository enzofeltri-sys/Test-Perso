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

import hmac
import json
import math
import os
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yaml
from flask import Flask, jsonify, render_template_string, request

import data
import supabase_state as db
import alerts
from strategy import regime_strategy_from_config
from risk import position_size, DailyLossCircuitBreaker, TotalDrawdownCircuitBreaker
from walk_forward import _apply_overrides as _apply_strategy_param_overrides

app = Flask(__name__)

STATUS_PAGE = """<!doctype html>
<title>Journal de bord — {{ bot_label }}</title>
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
    <p class="kicker">{{ bot_label|capitalize }} — paper trading</p>
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
                "max_correlation_for_new_position", "max_position_pct_of_equity",
                "max_position_notional_usd"):
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


# Fenêtre pendant laquelle une nouvelle erreur ne redéclenche PAS d'alerte.
# Sans elle, un exchange injoignable pendant six heures enverrait 72
# notifications (une par tick) — et on apprendrait à les ignorer.
ERROR_ALERT_COOLDOWN_MINUTES = 60


def _is_new_error_episode() -> bool:
    """True si aucune erreur n'a été enregistrée dans la dernière heure.

    Déduplication SANS état en mémoire : ce module est relancé à chaque
    /tick (rien ne survit d'un cycle à l'autre), donc on lit l'horodatage
    de la dernière erreur déjà en base plutôt que de tenir un compteur
    qui serait perdu à chaque fois.

    En cas de doute — table illisible, horodatage inexploitable — on
    retourne False : mieux vaut manquer une alerte que d'en envoyer une
    toutes les cinq minutes, parce qu'une alerte qu'on apprend à ignorer
    ne protège plus de rien.
    """
    try:
        recent = db.get_recent_errors(limit=1)
    except Exception:
        return False
    if not recent:
        return True
    raw_ts = recent[0].get("ts")
    if not raw_ts:
        return False
    try:
        last = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return age_minutes >= ERROR_ALERT_COOLDOWN_MINUTES


CONFIG_OVERRIDE_KEYS = (
    "risk_per_trade_pct", "max_daily_loss_pct", "max_total_drawdown_pct",
    "max_position_pct_of_equity", "max_position_notional_usd",
    "max_correlation_for_new_position", "correlation_lookback",
    "momentum_lookback", "max_concurrent_positions", "active_symbols",
    "strategy_overrides",
)


def _config_fingerprint(overrides: dict) -> str:
    """Empreinte stable des réglages effectivement posés dans
    tradingbot_config (clés non nulles uniquement) — basée sur le CONTENU,
    pas sur updated_at, pour qu'un manager qui oublie de mettre updated_at
    à jour soit quand même acquitté."""
    active = {k: overrides.get(k) for k in CONFIG_OVERRIDE_KEYS if overrides.get(k) is not None}
    return json.dumps(active, sort_keys=True, default=str)


def _acknowledge_config_changes(overrides: dict) -> None:
    """Ferme la boucle manager -> bot : quand tradingbot_config change (par
    le manager, ou par recalibrate.py), le bot le dit UNE fois dans le
    journal au lieu d'appliquer en silence. L'entrée de journal est
    elle-même l'acquittement (data.event='config_change' + empreinte) :
    aucun état en mémoire, aucune colonne supplémentaire."""
    fingerprint = _config_fingerprint(overrides)
    last = db.get_last_journal_event("config_change")
    last_fingerprint = (last.get("data") or {}).get("fingerprint") if last else None
    if fingerprint == last_fingerprint:
        return
    if fingerprint == "{}" and last_fingerprint is None:
        return  # jamais eu de réglage manager : rien à acquitter

    who = overrides.get("updated_by") or "inconnu"
    note = overrides.get("note")
    active = json.loads(fingerprint)
    if active:
        details = ", ".join(f"{k}={v}" for k, v in active.items())
        message = (f"Réglages tradingbot_config modifiés (par {who}"
                   + (f", note : « {note} »" if note else "")
                   + f") : {details}. Appliqués à partir de ce cycle.")
    else:
        message = (f"Réglages tradingbot_config remis à zéro (par {who}) : retour aux "
                   "valeurs de config.yaml à partir de ce cycle.")
    db.log_journal_entry("bot", message, data={
        "event": "config_change", "fingerprint": fingerprint, "updated_by": who,
    })


def run_tick() -> dict:
    cfg = load_config()
    pf_cfg = cfg["portfolio"]
    risk_cfg = cfg["risk"]
    pt_cfg = cfg["paper_trading"]
    ex_cfg = cfg["exchange"]
    fee_pct = cfg["backtest"].get("fee_pct", 0.001)
    slippage_pct = cfg["backtest"].get("slippage_pct", 0.0)

    overrides = db.load_config_overrides()
    _acknowledge_config_changes(overrides)
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

    new_error_episode = False
    if fetch_errors:
        # vérifié AVANT d'écrire celles de ce tick, sinon elles compteraient
        # elles-mêmes comme "une erreur récente" et rien n'alerterait jamais
        new_error_episode = _is_new_error_episode()
    for msg in fetch_errors:
        db.log_error(f"Erreur récupération des données — {msg}")
    if new_error_episode:
        alerts.send(
            f"{len(fetch_errors)} paire(s) impossibles à récupérer ce cycle : "
            f"{'; '.join(fetch_errors)[:600]}. Le bot continue avec les paires "
            f"disponibles. Prochaine alerte au plus tôt dans "
            f"{ERROR_ALERT_COOLDOWN_MINUTES} min.",
            alerts.WARNING,
        )

    if not rows:
        return {"ok": True, "note": "pas assez de données ce cycle", "errors": fetch_errors}

    prices = {s: rows[s]["close"] for s in rows}
    equity = cash + sum(positions[s]["qty"] * prices[s] for s in rows if positions.get(s))

    now = datetime.now(timezone.utc)
    daily_was_tripped = daily_breaker._tripped_today
    total_was_tripped = total_dd_breaker._tripped
    daily_breaker.update(now, equity)
    total_dd_breaker.update(equity)

    # un coupe-circuit qui se déclenche est journalisé UNE fois (à la
    # transition), pas à chaque tick tant qu'il reste déclenché
    if daily_breaker._tripped_today and not daily_was_tripped:
        db.log_journal_entry(
            "bot",
            f"Coupe-circuit journalier déclenché : équity {equity:.2f} contre "
            f"{daily_breaker._equity_at_day_start:.2f} en début de journée "
            f"(seuil -{risk_cfg['max_daily_loss_pct'] * 100:.0f}%). Plus aucune nouvelle "
            "entrée aujourd'hui, les positions ouvertes restent gérées normalement.",
            data={"event": "circuit_breaker", "kind": "daily", "equity": equity},
        )
        alerts.send(
            f"Coupe-circuit JOURNALIER déclenché : capital {equity:.2f} USDT contre "
            f"{daily_breaker._equity_at_day_start:.2f} en début de journée "
            f"(seuil -{risk_cfg['max_daily_loss_pct'] * 100:.0f}%). Aucune nouvelle entrée "
            f"aujourd'hui ; les positions ouvertes restent gérées normalement. "
            f"Remise à zéro automatique demain.",
            alerts.WARNING,
        )
    if total_dd_breaker._tripped and not total_was_tripped:
        db.log_journal_entry(
            "bot",
            f"Coupe-circuit de drawdown TOTAL déclenché : équity {equity:.2f}, plus haut "
            f"historique {total_dd_breaker._peak_equity:.2f} "
            f"(seuil -{risk_cfg['max_total_drawdown_pct'] * 100:.0f}%). Plus aucune nouvelle "
            "entrée tant qu'un humain n'a pas révisé la situation — ce coupe-circuit ne se "
            "réarme jamais tout seul.",
            data={"event": "circuit_breaker", "kind": "total_drawdown", "equity": equity},
        )
        # celui-ci ne se réarme JAMAIS tout seul (voir risk.py) : il demande
        # une décision humaine, c'est donc la seule alerte critique du bot
        alerts.send(
            f"Coupe-circuit de DRAWDOWN TOTAL déclenché : capital {equity:.2f} USDT, plus haut "
            f"historique {total_dd_breaker._peak_equity:.2f} "
            f"(seuil -{risk_cfg['max_total_drawdown_pct'] * 100:.0f}%). Le bot n'ouvrira PLUS "
            f"aucune position tant qu'un humain n'a pas revu la stratégie — ce coupe-circuit "
            f"ne se réarme pas tout seul.",
            alerts.CRITICAL,
        )

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
            pnl_pct = (exit_price / pos["entry_price"] - 1) * 100
            reason_label = {"stop_loss": "stop-loss touché", "take_profit": "objectif atteint",
                            "signal": "signal de sortie"}.get(exit_reason, exit_reason)
            # le % sur le prix ne dit pas ce que ça pèse : une position à 5%
            # du capital et une à 25% qui bougent de -1% n'ont pas le même effet
            pnl_usdt = (proceeds - fee) - pos["qty"] * pos["entry_price"] * (1 + fee_pct)
            equity_impact_pct = pnl_usdt / (equity_now - pnl_usdt) * 100 if equity_now != pnl_usdt else 0.0
            db.log_journal_entry(
                "bot",
                f"Vente {s} : {pos['qty']:.6f} @ {exit_price:.2f} ({reason_label}), entrée à "
                f"{pos['entry_price']:.2f} → {pnl_pct:+.2f}% sur le prix, soit {pnl_usdt:+.2f} USDT "
                f"nets de frais ({equity_impact_pct:+.2f}% du capital). Capital après : {equity_now:.2f} USDT.",
                data={"event": "trade", "side": "sell", "symbol": s, "reason": exit_reason,
                      "price": exit_price, "qty": pos["qty"], "pnl_pct": pnl_pct,
                      "pnl_usdt": pnl_usdt, "equity_impact_pct": equity_impact_pct,
                      "equity_after": equity_now},
            )

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
                                 max_position_pct_of_equity=risk_cfg.get("max_position_pct_of_equity"),
                                 max_position_notional_usd=risk_cfg.get("max_position_notional_usd"),
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
                regime_label = "tendance (EMA/MACD/volume)" if active == "trend" else "retournement (Bollinger/RSI)"
                # Une quantité brute ("0,01262 BTC") ne dit pas à un lecteur
                # humain ce qu'il engage. Ces trois nombres-là, si : combien
                # de capital part, ce que coûte le stop s'il est touché, et
                # l'exposition totale après ce trade. C'est exactement ce qui
                # manquait au rapport du 09/09/2026, où une position à 99,9%
                # du capital est passée inaperçue.
                notional = qty * fill_price
                pct_of_equity = notional / equity_now * 100 if equity_now else 0.0
                risk_if_stopped_pct = qty * (fill_price - stop_p) / equity_now * 100 if equity_now else 0.0
                exposure_pct = sum(
                    positions[s2]["qty"] * prices[s2] for s2 in rows if positions.get(s2)
                ) / equity_after * 100 if equity_after else 0.0
                db.log_journal_entry(
                    "bot",
                    f"Achat {s} : {qty:.6f} @ {fill_price:.2f} = {notional:.2f} USDT, soit "
                    f"{pct_of_equity:.1f}% du capital — signal de {regime_label}, "
                    f"stop {stop_p:.2f}, objectif {target_p:.2f}. Si le stop est touché je perds "
                    f"{risk_if_stopped_pct:.2f}% du capital ; exposition totale après ce trade : "
                    f"{exposure_pct:.1f}%.",
                    data={"event": "trade", "side": "buy", "symbol": s, "regime": active,
                          "price": fill_price, "qty": qty, "stop": stop_p, "target": target_p,
                          "notional": notional, "pct_of_equity": pct_of_equity,
                          "risk_if_stopped_pct": risk_if_stopped_pct, "exposure_pct": exposure_pct},
                )

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
        # part du capital réellement engagée en marché, tous symboles
        # confondus — visible même quand tradingbot_journal n'existe pas
        "exposure_pct": round(
            (total_equity - cash) / total_equity * 100 if total_equity else 0.0, 1),
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
                "strategy_overrides", "max_position_pct_of_equity",
                "max_position_notional_usd",
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


# Les 3 bots connus, tous sur le même projet Supabase (préfixes de table
# différents) mais des services Render distincts (voir DEPLOIEMENT.md,
# "Un deuxième bot en parallèle" / "Un troisième bot"). Codé en dur
# plutôt que découvert dynamiquement : c'est une vue en LECTURE SEULE, qui
# ne pilote rien — l'ajouter ici ne fait jamais apparaître un bot qui
# n'existe pas ailleurs, et l'inverse n'est pas dangereux non plus (un
# bot pas encore migré affiche juste "aucune donnée pour l'instant").
BOT_REGISTRY = [
    {"prefix": "tradingbot", "label": "Bot #1 — BTC/ETH/SOL", "url": "https://test-perso.onrender.com"},
    {"prefix": "altbot", "label": "Bot #2 — BNB/XRP/LINK", "url": "https://trading-bot-altcoins.onrender.com"},
    {"prefix": "microbot", "label": "Bot #3 — 10 paires, 10$/position", "url": "https://trading-bot-microbets.onrender.com"},
]


def _fetch_bot_summary(prefix: str) -> dict:
    """Lit l'essentiel d'UN bot par son préfixe de table, en lecture seule,
    directement en REST — jamais via supabase_state (dont TABLE_PREFIX ne
    vaut que pour le bot de CE processus, pas pour lire les tables d'un
    AUTRE bot). Best-effort partout : une table pas encore migrée ne doit
    jamais faire échouer la page pour les bots qui, eux, existent déjà.

    Un seul appel `trades` (croissant, jusqu'à 500) sert trois besoins à la
    fois : les derniers trades affichés (on en reprend la queue), le compte
    des positions clôturées (les ventes), et la courbe d'équité du
    graphique — sur les volumes réels de ce projet (dizaines de trades par
    mois), 500 couvre largement plusieurs mois d'historique en une requête."""
    def _get(table, params):
        try:
            resp = requests.get(f"{db._base_url()}/{prefix}_{table}", headers=db._headers(),
                                 params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    state_rows = _get("state", {"id": "eq.default", "select": "*"})
    state = (state_rows or [{}])[0] if state_rows else {}
    trades_asc = _get("trades", {"select": "*", "order": "ts.asc", "limit": "500"}) or []
    journal = _get("journal", {"select": "*", "order": "ts.desc", "limit": "5"}) or []

    positions = (state or {}).get("positions") or {}
    healthy = state is not None and not (state.get("daily_tripped_today") or state.get("total_dd_tripped"))
    recent_trades = list(reversed(trades_asc))[:5]

    return {
        "found": state_rows is not None,
        "healthy": healthy,
        "total_dd_tripped": bool((state or {}).get("total_dd_tripped")),
        "cash": (state or {}).get("cash"),
        "open_positions": len(positions),
        "closed_positions": sum(1 for t in trades_asc if t.get("side") == "sell"),
        "trades": [{
            "symbol": t.get("symbol"), "side": t.get("side"), "reason": t.get("reason"),
            "price": t.get("price"), "qty": t.get("qty"), "ts": _fmt_ts(t.get("ts")),
        } for t in recent_trades],
        "journal": [{
            "ts": _fmt_ts(j.get("ts")), "author": j.get("author"), "message": j.get("message"),
        } for j in journal],
        # (ts brut, equity_after) — matière première du graphique, jamais
        # affiché directement ; on garde le ts ISO ici, l'analyse (parsing,
        # échelle) est isolée dans _build_equity_chart_svg pour rester testable.
        "equity_points": [(t["ts"], t["equity_after"]) for t in trades_asc
                           if t.get("ts") and t.get("equity_after") is not None],
    }


def _parse_ts(ts_str):
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def _nice_step(raw_step: float) -> float:
    """Arrondit un pas d'axe au "nombre rond" juste au-dessus (1/2/5 * 10^n)
    — évite des graduations comme 733,4 $ sur l'axe Y. Marche aussi pour de
    tout petits écarts (le bot #3 plafonne chaque position à 10$ : son
    équity peut à peine bouger d'une fenêtre à l'autre)."""
    if raw_step <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for mult in (1, 2, 5, 10):
        step = mult * magnitude
        if step >= raw_step:
            return step
    return 10 * magnitude


def _build_equity_chart_svg(series: list) -> str:
    """series: [{"label": str, "color_var": "--series-1", "points": [(datetime, float), ...]}],
    points DÉJÀ triés croissants par date. Rend un graphique ligne SVG
    autonome (aucun JS, aucune dépendance externe) — un point par trade
    réellement exécuté (pas une courbe continue : personne ne connaît la
    valeur du portefeuille ENTRE deux trades, ce serait inventer des
    données). Retourne "" si aucune série n'a au moins un point.

    Charte couleurs/formes : palette catégorielle validée (3 premiers
    slots, voir dataviz), lignes 2px, points de fin >=8px avec anneau de
    séparation, étiquettes directes en fin de ligne, jamais la couleur de
    la série sur le texte (charte "marks-and-anatomy")."""
    active = [s for s in series if s["points"]]
    if not active:
        return ""

    W, H = 640, 240
    # marge gauche : graduations $ ; marge droite : réservée aux étiquettes
    # de fin de ligne — les deux ne doivent JAMAIS partager le même côté,
    # sinon un point qui finit près d'une graduation ronde les superpose.
    PAD_L, PAD_R, PAD_T, PAD_B = 46, 128, 16, 30

    all_points = [p for s in active for p in s["points"]]
    t_min = min(p[0] for p in all_points)
    t_max = max(p[0] for p in all_points)
    if t_max == t_min:
        t_max = t_min + timedelta(hours=1)

    values = [p[1] for p in all_points] + [1000.0]  # 1000 = capital de départ, toujours dans le cadre
    v_min, v_max = min(values), max(values)
    v_range = max(v_max - v_min, 1.0)
    v_pad = v_range * 0.12
    v_min, v_max = v_min - v_pad, v_max + v_pad

    def x_of(t):
        frac = (t - t_min).total_seconds() / (t_max - t_min).total_seconds()
        return PAD_L + frac * (W - PAD_L - PAD_R)

    def y_of(v):
        frac = (v - v_min) / (v_max - v_min)
        return H - PAD_B - frac * (H - PAD_T - PAD_B)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
             f'aria-label="Évolution du capital des {len(active)} bot(s) au fil des trades">']

    # ligne de référence : le capital de départ commun aux 3 bots (1000$).
    # Pas d'étiquette texte inline ici (superposition avec les courbes qui
    # démarrent toutes près de cette ligne) — le sens de la ligne est
    # expliqué dans la légende de la carte (chart-sub).
    ref_y = y_of(1000.0)
    parts.append(f'<line x1="{PAD_L}" y1="{ref_y:.1f}" x2="{W - PAD_R}" y2="{ref_y:.1f}" '
                 f'stroke="var(--rule)" stroke-width="1"/>')

    # graduations Y (nombres ronds) — à gauche, jamais du même côté que les
    # étiquettes directes de fin de ligne (à droite), sinon collision quand
    # une série finit près d'une valeur ronde.
    tick_step = _nice_step((v_max - v_min) / 4)
    tick = tick_step * round(v_min / tick_step)
    while tick <= v_max:
        if tick > v_min:
            y = y_of(tick)
            parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                         f'stroke="var(--rule)" stroke-width="1" opacity="0.5"/>')
            parts.append(f'<text x="{PAD_L - 6:.1f}" y="{y + 3:.1f}" font-size="9" fill="var(--text-faint)" '
                         f'text-anchor="end" font-family="IBM Plex Mono, monospace">${tick:,.0f}</text>')
        tick += tick_step

    # repères de date (début / milieu / fin)
    for frac, anchor in ((0.0, "start"), (0.5, "middle"), (1.0, "end")):
        t = t_min + (t_max - t_min) * frac
        x = x_of(t)
        parts.append(f'<text x="{x:.1f}" y="{H - 8}" font-size="9" fill="var(--text-faint)" '
                     f'text-anchor="{anchor}" font-family="IBM Plex Mono, monospace">{t.strftime("%d/%m")}</text>')

    # une ligne par bot + point de fin étiqueté — labels positionnés avec un
    # écart minimum pour ne jamais se chevaucher quand deux bots finissent
    # à une valeur proche (voir dataviz: "quand les fins convergent...")
    end_labels = []
    for s in active:
        pts = s["points"]
        poly = " ".join(f"{x_of(t):.1f},{y_of(v):.1f}" for t, v in pts)
        parts.append(f'<polyline points="{poly}" fill="none" stroke="var({s["color_var"]})" '
                     f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        last_t, last_v = pts[-1]
        lx, ly = x_of(last_t), y_of(last_v)
        # anneau de séparation (surface) puis point plein (couleur de série)
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="7" fill="var(--card)"/>')
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="5" fill="var({s["color_var"]})">'
                     f'<title>{s["label"]} : ${last_v:,.2f} au {last_t.strftime("%d/%m %H:%M")} UTC</title></circle>')
        end_labels.append({"y": ly, "text": f'{s["label"]} · ${last_v:,.0f}', "color_var": s["color_var"]})

    end_labels.sort(key=lambda e: e["y"])
    MIN_GAP = 14
    for i in range(1, len(end_labels)):
        if end_labels[i]["y"] - end_labels[i - 1]["y"] < MIN_GAP:
            end_labels[i]["y"] = end_labels[i - 1]["y"] + MIN_GAP
    for e in end_labels:
        parts.append(f'<circle cx="{W - PAD_R + 10}" cy="{e["y"] - 3:.1f}" r="3.5" fill="var({e["color_var"]})"/>')
        parts.append(f'<text x="{W - PAD_R + 18}" y="{e["y"]:.1f}" font-size="10.5" fill="var(--text)" '
                     f'font-family="IBM Plex Sans, sans-serif">{e["text"]}</text>')

    parts.append("</svg>")
    return "".join(parts)


ALL_PAGE = """<!doctype html>
<title>Vue d'ensemble — 3 bots</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,500&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
  :root{
    --bg:#F5F5F3; --text:#1C1C1A; --text-muted:#767671; --text-faint:#A5A59F;
    --rule:#DBDBD6; --accent:#96622A; --green:#3E7A52; --red:#A3453A; --card:#EBEBE7;
    /* palette catégorielle validée (dataviz skill) — 3 premiers slots,
       les seuls qui passent le contrôle CVD/contraste toutes paires
       confondues en clair ET en sombre : bleu / orange / aqua */
    --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --bg:#17181A; --text:#E7E6E1; --text-muted:#8E8E88; --text-faint:#5C5D59;
      --rule:#333432; --accent:#CB9855; --green:#6FAE87; --red:#D08076; --card:#1F2022;
      --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
    }
  }
  :root[data-theme="dark"]{
    --bg:#17181A; --text:#E7E6E1; --text-muted:#8E8E88; --text-faint:#5C5D59;
    --rule:#333432; --accent:#CB9855; --green:#6FAE87; --red:#D08076; --card:#1F2022;
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
  }
  *{ box-sizing:border-box; margin:0; }
  body{ background:var(--bg); color:var(--text); font-family:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif; line-height:1.55; }
  .wrap{ max-width:640px; margin:0 auto; padding:56px 20px 96px; }
  .mono{ font-family:"IBM Plex Mono", ui-monospace, monospace; }
  .kicker{ font-family:"IBM Plex Mono", monospace; font-size:0.72rem; letter-spacing:0.14em; text-transform:uppercase; color:var(--text-faint); margin-bottom:10px; }
  h1{ font-family:"Fraunces", serif; font-weight:500; font-size:1.9rem; margin-bottom:44px; }
  .bot{ background:var(--card); border-radius:14px; padding:22px 22px 20px; margin-bottom:20px; }
  .bot-head{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:14px; }
  .bot-name{ font-weight:500; font-size:1.02rem; }
  .status-line{ display:flex; align-items:baseline; gap:8px; font-size:0.82rem; color:var(--text-muted); margin-bottom:16px; }
  .dot{ width:6px; height:6px; border-radius:50%; display:inline-block; }
  .dot.ok{ background:var(--green); } .dot.bad{ background:var(--red); } .dot.unknown{ background:var(--text-faint); }
  .stats-row{ display:flex; gap:24px; margin-bottom:16px; flex-wrap:wrap; }
  .stat .n{ font-size:0.72rem; color:var(--text-muted); margin-bottom:2px; }
  .stat .v{ font-family:"IBM Plex Mono", monospace; font-variant-numeric:tabular-nums; font-size:0.98rem; }
  .label{ font-size:0.72rem; color:var(--text-faint); text-transform:uppercase; letter-spacing:0.08em; margin:14px 0 6px; }
  .row{ padding:8px 0; font-size:0.85rem; }
  .row + .row{ border-top:1px solid var(--rule); }
  .row .name{ font-weight:500; }
  .row .detail{ font-size:0.76rem; color:var(--text-muted); margin-top:1px; }
  .side{ font-family:"IBM Plex Mono", monospace; font-size:0.72rem; }
  .side.buy{ color:var(--green); } .side.sell{ color:var(--red); }
  .empty{ font-size:0.82rem; color:var(--text-faint); }
  .bot-link{
    display:inline-flex; align-items:center; gap:6px; margin-top:16px;
    padding:9px 14px; border-radius:9px; background:var(--bg);
    font-size:0.82rem; font-weight:500; color:var(--accent); text-decoration:none;
  }
  .chart-card{ background:var(--card); border-radius:14px; padding:22px; margin-bottom:24px; }
  .chart-card h2{ font-size:0.95rem; font-weight:500; margin-bottom:4px; }
  .chart-sub{ font-size:0.78rem; color:var(--text-muted); margin-bottom:16px; }
  .legend{ display:flex; gap:18px; flex-wrap:wrap; margin-bottom:14px; }
  .legend-item{ display:flex; align-items:center; gap:6px; font-size:0.8rem; color:var(--text-muted); }
  .legend-dot{ width:9px; height:9px; border-radius:50%; display:inline-block; flex-shrink:0; }
  footer{ margin-top:40px; font-size:0.78rem; color:var(--text-faint); text-align:center; }
  footer a{ color:inherit; }
</style>

<div class="wrap">
  <p class="kicker">Vue d'ensemble</p>
  <h1>Les 3 bots</h1>

  <div class="chart-card">
    <h2>Évolution du capital</h2>
    <p class="chart-sub">Un point par trade réellement exécuté — pas une estimation entre deux trades. La ligne fine horizontale marque les 1000$ de départ commun aux 3 bots.</p>
    {% if chart_svg %}
      <div class="legend">
        {% for b in bots %}
        <span class="legend-item"><span class="legend-dot" style="background:var({{ b.color_var }})"></span>{{ b.label.split(' — ')[0] }}</span>
        {% endfor %}
      </div>
      {{ chart_svg|safe }}
    {% else %}
      <p class="empty">Aucun trade sur aucun bot pour l'instant — le graphique apparaîtra dès le premier.</p>
    {% endif %}
  </div>

  {% for b in bots %}
  <div class="bot">
    <div class="bot-head">
      <span class="bot-name"><span class="legend-dot" style="background:var({{ b.color_var }})"></span> {{ b.label }}</span>
    </div>
    {% if not b.summary.found %}
      <p class="empty">Aucune donnée pour l'instant — tables pas encore migrées ou service pas encore déployé.</p>
    {% else %}
      <div class="status-line">
        <span class="dot {{ 'bad' if b.summary.total_dd_tripped else ('ok' if b.summary.healthy else 'bad') }}"></span>
        {% if b.summary.total_dd_tripped %}
          <strong>Coupe-circuit total déclenché</strong>
        {% elif b.summary.healthy %}
          En ligne
        {% else %}
          Coupe-circuit journalier déclenché
        {% endif %}
      </div>
      <div class="stats-row">
        <div class="stat"><div class="n">Cash</div><div class="v">{{ '$%.2f'|format(b.summary.cash) if b.summary.cash is not none else '—' }}</div></div>
        <div class="stat"><div class="n">Positions ouvertes</div><div class="v">{{ b.summary.open_positions }}</div></div>
        <div class="stat"><div class="n">Positions clôturées</div><div class="v">{{ b.summary.closed_positions }}</div></div>
      </div>
      <p class="label">Derniers trades</p>
      {% if b.summary.trades %}
        {% for t in b.summary.trades %}
        <div class="row">
          <div class="name">{{ t.symbol }} <span class="side mono {{ t.side }}">{{ t.side }}</span></div>
          <div class="detail">{{ t.ts }} · {{ t.reason }} · {{ '%.6f'|format(t.qty) }} @ ${{ '%.2f'|format(t.price) }}</div>
        </div>
        {% endfor %}
      {% else %}
        <p class="empty">Aucun trade pour l'instant.</p>
      {% endif %}
      <p class="label">Journal</p>
      {% if b.summary.journal %}
        {% for j in b.summary.journal %}
        <div class="row">
          <div class="detail">{{ j.ts }} · {{ j.author }}</div>
          <div class="name" style="font-weight:400;">{{ j.message }}</div>
        </div>
        {% endfor %}
      {% else %}
        <p class="empty">Aucune entrée pour l'instant.</p>
      {% endif %}
    {% endif %}
    <a class="bot-link" href="{{ b.url }}">Voir la page complète de {{ b.label.split(' — ')[0] }} →</a>
  </div>
  {% endfor %}

  <footer>
    <p>Lecture seule — ne déclenche aucun cycle. Chaque bot garde son propre <span class="mono">/tick</span> et son propre déploiement, indépendamment de cette page.</p>
  </footer>
</div>
"""


@app.route("/all")
def all_bots():
    try:
        color_vars = ["--series-1", "--series-2", "--series-3"]
        bots = []
        chart_series = []
        for b, color_var in zip(BOT_REGISTRY, color_vars):
            summary = _fetch_bot_summary(b["prefix"])
            bots.append({"label": b["label"], "url": b["url"], "summary": summary, "color_var": color_var})
            points = []
            for ts_raw, equity in summary["equity_points"]:
                try:
                    points.append((_parse_ts(ts_raw), float(equity)))
                except (ValueError, TypeError):
                    continue  # une ligne mal formée ne doit pas casser tout le graphique
            if points:
                chart_series.append({"label": b["label"].split(" — ")[0], "color_var": color_var, "points": points})

        chart_svg = _build_equity_chart_svg(chart_series)
        return render_template_string(ALL_PAGE, bots=bots, chart_svg=chart_svg), 200
    except Exception:
        return "OK - vue d'ensemble indisponible pour le moment.", 200


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
            bot_label=os.environ.get("BOT_LABEL") or "bot de trading crypto",
        ), 200
    except Exception:
        return "OK - bot de paper trading en ligne. Utilise /tick pour déclencher un cycle.", 200


@app.route("/tick")
def tick():
    try:
        result = run_tick()
        return jsonify(result), 200
    except Exception as e:
        # ordre volontaire : la trace part en base d'abord (source de vérité),
        # l'alerte ensuite — et la dédup se lit AVANT d'écrire, sinon
        # l'erreur qu'on vient d'écrire masquerait son propre épisode
        new_episode = _is_new_error_episode()
        db.log_error(f"Erreur non gérée dans /tick : {e}\n{traceback.format_exc()}")
        if new_episode:
            alerts.send(
                f"Le cycle de trading a planté : {e}. Le bot ne tradera plus tant que "
                f"l'erreur persiste (trace complète dans tradingbot_errors). Prochaine "
                f"alerte au plus tôt dans {ERROR_ALERT_COOLDOWN_MINUTES} min.",
                alerts.CRITICAL,
            )
        # HTTP 200 volontaire : une erreur ici ne doit pas faire croire à
        # UptimeRobot que le service est "down" et générer des alertes
        # inutiles — l'erreur est déjà tracée pour le point de
        # supervision quotidien.
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/alert-test")
def alert_test():
    """Envoie une alerte de test et dit précisément ce qui s'est passé.

    Pourquoi cet endpoint existe : un système d'alerte qu'on n'a jamais vu
    se déclencher n'est pas un système d'alerte, c'est une croyance d'être
    couvert. Le jour où le coupe-circuit tombe n'est pas le bon moment
    pour découvrir que l'URL du webhook était mal collée. Il sert aussi
    après coup, à chaque rotation de webhook ou changement de service.

    Protection : un jeton dans l'URL (`ALERT_TEST_TOKEN`), parce que le
    service est ouvert à tous. Sans ce jeton configuré, l'endpoint
    n'existe pas — un jeton par défaut serait pire que pas de jeton du
    tout. Le jeton apparaît dans les logs d'accès Render, ce qui est
    acceptable ici : il ne donne le droit que d'envoyer un message de
    test vers TON propre webhook, rien d'autre. Il ne touche ni à l'état
    du bot, ni aux positions, ni à la config.
    """
    expected = os.environ.get("ALERT_TEST_TOKEN")
    if not expected:
        return jsonify({
            "ok": False,
            "error": "endpoint désactivé : ALERT_TEST_TOKEN n'est pas défini sur ce service",
        }), 404

    # comparaison à temps constant : ne fuite pas le jeton caractère par
    # caractère via le temps de réponse
    if not hmac.compare_digest(request.args.get("token", ""), expected):
        return jsonify({"ok": False, "error": "jeton invalide"}), 403

    # distinguer les deux échecs possibles est TOUT l'intérêt du test :
    # "webhook pas configuré" et "configuré mais l'envoi a échoué" se
    # corrigent de façon complètement différente
    if not alerts.is_configured():
        return jsonify({
            "ok": False,
            "alert_configured": False,
            "error": "ALERT_WEBHOOK_URL n'est pas définie sur ce service — "
                     "aucune alerte ne partira, y compris les vraies",
        }), 200

    sent = alerts.send(
        f"Test manuel des alertes ({datetime.now(timezone.utc).strftime('%d/%m %H:%M')} UTC). "
        "Si tu lis ceci, la chaîne d'alerte fonctionne de bout en bout : "
        "les vraies alertes (coupe-circuit, cycle planté, données inaccessibles) "
        "arriveront par ce même canal. Aucun impact sur le bot.",
        alerts.INFO,
    )
    return jsonify({
        "ok": sent,
        "alert_configured": True,
        "message": "alerte de test envoyée — vérifie ton canal" if sent else
                   "ALERT_WEBHOOK_URL est définie mais l'envoi a échoué : "
                   "URL invalide, webhook supprimé, ou service injoignable",
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
