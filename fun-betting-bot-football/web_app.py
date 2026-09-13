"""
web_app.py
----------
Service web (pensé pour Render, plan gratuit) qui fait tourner le bot de
paris virtuels dans le cloud, consultable depuis un navigateur (téléphone
compris) :

  /       page de statut (bankroll, derniers paris, dernier backtest, journal)
  /tick   déclenche un cycle : règle les paris en attente dont le match est
          fini, puis place de nouveaux paris sur les matchs à venir si l'EV
          le justifie. Fait pour être appelé régulièrement par UptimeRobot.
  /alert-test  vérifie la chaîne d'alerte (voir alerts.py), protégé par
               ALERT_TEST_TOKEN.

Aucun argent réel n'est jamais en jeu : "bankroll" est une unité virtuelle,
"paris" ne sont que des lignes dans Supabase. Voir README.md.

Contrairement à trading_bot/web_app.py, ce bot ne réagit pas seconde par
seconde : les matchs sont connus des jours à l'avance et ne se règlent
qu'après coup, donc /tick throttle lui-même ses appels à The Odds API
(voir config.ODDS_FETCH_INTERVAL_HOURS / SCORES_FETCH_INTERVAL_HOURS) pour
rester dans le quota gratuit (500 crédits/mois) même appelé toutes les 5
minutes par UptimeRobot.
"""

import os
import html
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

import alerts
from src import config, data_loader, features, model as model_module, strategy, supabase_state
from src.team_names import normalize_team_name

app = Flask(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_since(iso_value: str | None) -> float | None:
    dt = _parse_iso(iso_value) if iso_value else None
    if dt is None:
        return None
    return (_now() - dt).total_seconds() / 3600


def _settle_pending_bets(state: dict, errors: list) -> int:
    """Règle les paris en attente dont le match est terminé. Best-effort par
    conception : une erreur ici ne doit jamais empêcher le placement de
    nouveaux paris juste après."""
    pending = supabase_state.get_pending_bets()
    if not pending:
        return 0

    hours_since = _hours_since(state.get("last_scores_fetch_at"))
    if hours_since is not None and hours_since < config.SCORES_FETCH_INTERVAL_HOURS:
        return 0

    settled = 0
    try:
        known = supabase_state.get_known_team_names()
        events = data_loader.fetch_scores(leagues=list(config.LEAGUES.keys()))
        results_by_key = {}
        for event in events:
            if not event["completed"] or event["home_score"] is None or event["away_score"] is None:
                continue
            league_known = known.get(event["league"], [])
            home = normalize_team_name(event["home_team"], league_known)
            away = normalize_team_name(event["away_team"], league_known)
            results_by_key[(event["league"], home, away)] = event

        now = _now()
        for bet in pending:
            commence = _parse_iso(bet.get("commence_time"))
            if commence is None or now - commence < timedelta(hours=config.RESULT_SETTLE_BUFFER_HOURS):
                continue

            event = results_by_key.get((bet["league"], bet["home_team"], bet["away_team"]))
            if event is None:
                continue

            if event["home_score"] > event["away_score"]:
                actual = "H"
            elif event["away_score"] > event["home_score"]:
                actual = "A"
            else:
                actual = "D"

            won = bet["selection"] == actual
            pnl = bet["stake"] * (bet["odds"] - 1) if won else -bet["stake"]
            payout = bet["stake"] * bet["odds"] if won else 0.0
            state["bankroll"] += payout

            supabase_state.settle_bet(bet["id"], "won" if won else "lost", round(pnl, 2), round(state["bankroll"], 2))
            supabase_state.log_journal_entry(
                "bot",
                f"Pari réglé : {bet['home_team']} vs {bet['away_team']} — {bet['selection']} @ {bet['odds']} "
                f"— {'gagné' if won else 'perdu'} ({pnl:+.2f}). Bankroll : {state['bankroll']:.2f}.",
                {"event": "settlement"},
            )
            settled += 1

        state["last_scores_fetch_at"] = _now().isoformat()
    except Exception as exc:
        errors.append(f"settlement: {exc}")
        supabase_state.log_error(f"settlement: {exc}")

    return settled


def _place_new_bets(state: dict, errors: list) -> int:
    """Place de nouveaux paris sur les matchs à venir si l'EV le justifie.
    Ne fait rien (et ne consomme aucun crédit API) tant que le throttle
    ODDS_FETCH_INTERVAL_HOURS n'est pas écoulé."""
    hours_since = _hours_since(state.get("last_odds_fetch_at"))
    if hours_since is not None and hours_since < config.ODDS_FETCH_INTERVAL_HOURS:
        return 0

    placed = 0
    try:
        model_row = supabase_state.load_model()
        if not model_row:
            supabase_state.log_journal_entry(
                "bot",
                "Pas de modèle entraîné pour l'instant — retrain.py doit tourner au moins une fois "
                "(déclenchement manuel du workflow GitHub Actions, ou attendre le prochain run hebdomadaire).",
            )
            state["last_odds_fetch_at"] = _now().isoformat()
            return 0

        trained_model = model_module.deserialize_model(model_row["model_b64"])
        history_df = supabase_state.get_all_matches()
        known = supabase_state.get_known_team_names()
        upcoming = data_loader.fetch_upcoming_matches(
            leagues=list(config.LEAGUES.keys()), known_team_names=known,
        )

        for match in upcoming:
            if supabase_state.bet_exists(match["home_team"], match["away_team"], match["commence_time"]):
                continue

            X_match = features.build_features_for_match(history_df, match["home_team"], match["away_team"])
            if X_match is None:
                supabase_state.log_journal_entry(
                    "bot",
                    f"Historique insuffisant pour {match['home_team']} vs {match['away_team']}, match ignoré.",
                    {"event": "unmatched_team"},
                )
                continue

            probs = model_module.predict_proba(trained_model, X_match).iloc[0].to_dict()
            odds = {"H": match["odds_h"], "D": match["odds_d"], "A": match["odds_a"]}
            bet = strategy.select_bet(probs, odds, config.EV_THRESHOLD)
            if bet is None:
                continue

            stake = strategy.compute_stake(state["bankroll"], bet["prob"], bet["odds"])
            if stake <= 0:
                continue

            state["bankroll"] -= stake
            supabase_state.insert_bet({
                "commence_time": match["commence_time"],
                "league": match["league"],
                "home_team": match["home_team"],
                "away_team": match["away_team"],
                "market": "h2h",
                "selection": bet["selection"],
                "prob": round(bet["prob"], 4),
                "odds": bet["odds"],
                "stake": stake,
                "status": "pending",
            })
            supabase_state.log_journal_entry(
                "bot",
                f"Nouveau pari : {match['home_team']} vs {match['away_team']} — {bet['selection']} @ {bet['odds']} "
                f"(prob {bet['prob']:.0%}, EV {bet['ev']:+.1%}), mise {stake:.2f}.",
                {"event": "new_bet"},
            )
            placed += 1

        state["last_odds_fetch_at"] = _now().isoformat()
    except Exception as exc:
        errors.append(f"placement: {exc}")
        supabase_state.log_error(f"placement: {exc}")

    return placed


def run_tick() -> dict:
    errors = []
    state = supabase_state.load_state(config.INITIAL_BANKROLL)

    bets_settled = _settle_pending_bets(state, errors)
    bets_placed = _place_new_bets(state, errors)

    supabase_state.save_state(state)

    return {
        "bets_placed": bets_placed,
        "bets_settled": bets_settled,
        "bankroll": round(state["bankroll"], 2),
        "errors": errors,
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/tick")
def tick():
    try:
        result = run_tick()
    except Exception as exc:
        supabase_state.log_error(f"tick: {exc}")
        alerts.send(f"Cycle planté : {exc}", severity=alerts.CRITICAL)
        return jsonify({"errors": [str(exc)]}), 200

    if result["errors"]:
        alerts.send(f"Erreurs pendant le cycle : {'; '.join(result['errors'])}", severity=alerts.WARNING)

    return jsonify(result), 200


@app.route("/")
def status():
    bankroll = config.INITIAL_BANKROLL
    try:
        state = supabase_state.load_state(config.INITIAL_BANKROLL)
        bankroll = state["bankroll"]
    except Exception:
        pass

    recent_bets = _safe_call(supabase_state.get_recent_bets, 15, default=[])
    journal = _safe_call(supabase_state.get_recent_journal, 15, default=[])
    errors = _safe_call(supabase_state.get_recent_errors, 5, default=[])
    model_row = _safe_call(supabase_state.load_model, default={})

    return _render_status_page(bankroll, recent_bets, journal, errors, model_row)


def _safe_call(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:
        return default


@app.route("/alert-test")
def alert_test():
    expected = os.environ.get("ALERT_TEST_TOKEN")
    if not expected:
        return jsonify({"error": "ALERT_TEST_TOKEN n'est pas défini — endpoint désactivé."}), 404
    if request.args.get("token") != expected:
        return jsonify({"error": "jeton invalide"}), 403

    if not alerts.is_configured():
        return jsonify({
            "ok": False, "alert_configured": False,
            "error": "ALERT_WEBHOOK_URL n'est pas définie sur ce service — aucune alerte ne partira, y compris les vraies.",
        }), 200

    sent = alerts.send("Test de la chaîne d'alerte (bot de paris football).", severity=alerts.INFO)
    if sent:
        return jsonify({"ok": True, "alert_configured": True}), 200
    return jsonify({
        "ok": False, "alert_configured": True,
        "error": "ALERT_WEBHOOK_URL est définie mais l'envoi a échoué : URL invalide, webhook supprimé, ou service injoignable.",
    }), 200


def _render_status_page(bankroll, recent_bets, journal, errors, model_row) -> str:
    label = os.environ.get("BOT_LABEL") or "bot de paris football virtuels"
    metrics = (model_row or {}).get("backtest_metrics") or {}
    trained_at = (model_row or {}).get("trained_at") or "jamais"

    def esc(value) -> str:
        return html.escape(str(value)) if value is not None else ""

    bets_rows = "".join(
        f"<tr><td>{esc(b.get('commence_time', ''))[:16]}</td><td>{esc(b.get('league'))}</td>"
        f"<td>{esc(b.get('home_team'))} - {esc(b.get('away_team'))}</td>"
        f"<td>{esc(b.get('selection'))} @ {esc(b.get('odds'))}</td>"
        f"<td>{esc(b.get('stake'))}</td>"
        f"<td>{esc(b.get('status'))}</td>"
        f"<td>{esc(b.get('pnl') if b.get('pnl') is not None else '')}</td></tr>"
        for b in recent_bets
    ) or "<tr><td colspan='7'>Aucun pari pour l'instant.</td></tr>"

    journal_items = "".join(
        f"<li><b>{esc(j.get('ts', ''))[:16]}</b> [{esc(j.get('author'))}] {esc(j.get('message'))}</li>"
        for j in journal
    ) or "<li>Rien pour l'instant.</li>"

    errors_items = "".join(f"<li>{esc(e.get('ts', ''))[:16]} — {esc(e.get('message'))}</li>" for e in errors)
    errors_block = f"<h2>Erreurs récentes</h2><ul>{errors_items}</ul>" if errors else ""

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(label)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 16px; background:#0f1115; color:#e6e6e6; }}
h1 {{ font-size: 1.3rem; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
td, th {{ border-bottom: 1px solid #333; padding: 6px 8px; text-align: left; }}
.bankroll {{ font-size: 2rem; font-weight: bold; }}
.disclaimer {{ color: #f0ad4e; margin: 12px 0; }}
ul {{ padding-left: 18px; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>⚽ {esc(label)}</h1>
<p class="disclaimer">Paris 100% virtuels — aucun argent réel n'est en jeu.</p>
<p>Bankroll actuelle</p>
<div class="bankroll">{bankroll:.2f}</div>
<h2>Dernier modèle entraîné</h2>
<p>Entraîné le : {esc(trained_at)[:19]}</p>
<p>Backtest — ROI: {esc(metrics.get('roi_pct'))}% · Yield: {esc(metrics.get('yield_pct'))}% ·
Paris: {esc(metrics.get('num_bets'))} · Win rate: {esc(metrics.get('win_rate'))}% ·
Drawdown max: {esc(metrics.get('max_drawdown_pct'))}%</p>
<h2>Derniers paris</h2>
<table>
<tr><th>Coup d'envoi</th><th>Ligue</th><th>Match</th><th>Sélection</th><th>Mise</th><th>Statut</th><th>P&L</th></tr>
{bets_rows}
</table>
<h2>Journal</h2>
<ul>{journal_items}</ul>
{errors_block}
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
