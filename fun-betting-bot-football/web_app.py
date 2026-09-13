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
  /admin/retrain  déclenche retrain.py directement depuis ce service (protégé
               par RETRAIN_TOKEN) — alternative au workflow GitHub Actions
               pour le tout premier entraînement (ou un entraînement manuel),
               utile quand on n'a pas encore accès aux secrets du dépôt.
               Ce service a un accès réseau normal (contrairement à un
               environnement de dev restreint) donc le téléchargement
               football-data.co.uk y fonctionne.

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
import math
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request

import alerts
from src import config, data_loader, external_data, features, markets, model as model_module, strategy, supabase_state
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


def _quota_exhausted(state: dict) -> bool:
    remaining = state.get("odds_api_remaining")
    return remaining is not None and remaining < config.ODDS_API_MIN_REMAINING_CREDITS


def _leg_won(leg: dict, result: dict) -> bool:
    if leg["market"] == "1x2":
        return leg["selection"] == result["actual_result"]
    if leg["market"] == "totals":
        direction, line_str = leg["selection"].split()
        return (result["total_goals"] > float(line_str)) if direction == "Over" else (result["total_goals"] < float(line_str))
    return False


def _settle_pending_bets(state: dict, errors: list) -> int:
    """Règle les tickets (seuls ou combinés) dont TOUTES les jambes ont un
    résultat connu. Une jambe est réglée dès que son match est terminé ;
    un ticket combiné reste en attente tant qu'il lui manque au moins une
    jambe. Best-effort par conception : une erreur ici ne doit jamais
    empêcher le placement de nouveaux paris juste après."""
    pending_legs = supabase_state.get_pending_legs()
    if not pending_legs:
        return 0

    hours_since = _hours_since(state.get("last_scores_fetch_at"))
    if hours_since is not None and hours_since < config.SCORES_FETCH_INTERVAL_HOURS:
        return 0

    if _quota_exhausted(state):
        supabase_state.log_journal_entry(
            "bot",
            f"Quota The Odds API presque épuisé ({state['odds_api_remaining']} restants) — "
            "règlement des paris en attente reporté.",
            {"event": "quota_low"},
        )
        state["last_scores_fetch_at"] = _now().isoformat()
        return 0

    settled_tickets = 0
    try:
        known = supabase_state.get_known_team_names()
        events, api_remaining = data_loader.fetch_scores(leagues=list(config.LEAGUES.keys()))
        if api_remaining is not None:
            state["odds_api_remaining"] = api_remaining

        results_by_key = {}
        for event in events:
            if not event["completed"] or event["home_score"] is None or event["away_score"] is None:
                continue
            league_known = known.get(event["league"], [])
            home = normalize_team_name(event["home_team"], league_known)
            away = normalize_team_name(event["away_team"], league_known)
            if event["home_score"] > event["away_score"]:
                actual = "H"
            elif event["away_score"] > event["home_score"]:
                actual = "A"
            else:
                actual = "D"
            results_by_key[(event["league"], home, away)] = {
                "actual_result": actual, "total_goals": event["home_score"] + event["away_score"],
            }

        now = _now()
        touched_bet_ids = set()
        for leg in pending_legs:
            commence = _parse_iso(leg.get("commence_time"))
            if commence is None or now - commence < timedelta(hours=config.RESULT_SETTLE_BUFFER_HOURS):
                continue
            result = results_by_key.get((leg["league"], leg["home_team"], leg["away_team"]))
            if result is None:
                continue
            supabase_state.update_leg_result(leg["id"], "won" if _leg_won(leg, result) else "lost")
            touched_bet_ids.add(leg["bet_id"])

        for bet_id in touched_bet_ids:
            legs = supabase_state.get_ticket_legs(bet_id)
            if any(leg["result"] == "pending" for leg in legs):
                continue

            ticket = supabase_state.get_ticket(bet_id)
            won = all(leg["result"] == "won" for leg in legs)
            pnl = ticket["stake"] * (ticket["odds"] - 1) if won else -ticket["stake"]
            payout = ticket["stake"] * ticket["odds"] if won else 0.0
            state["bankroll"] += payout
            supabase_state.finalize_ticket(bet_id, "won" if won else "lost", round(pnl, 2), round(state["bankroll"], 2))

            legs_desc = ", ".join(f"{leg['home_team']}-{leg['away_team']}:{leg['selection']}" for leg in legs)
            kind = "combiné" if len(legs) > 1 else "pari"
            supabase_state.log_journal_entry(
                "bot",
                f"Ticket réglé ({kind}, {len(legs)} jambe(s)) : {legs_desc} — "
                f"{'gagné' if won else 'perdu'} ({pnl:+.2f}). Bankroll : {state['bankroll']:.2f}.",
                {"event": "settlement"},
            )
            settled_tickets += 1

        state["last_scores_fetch_at"] = _now().isoformat()
    except Exception as exc:
        errors.append(f"settlement: {exc}")
        supabase_state.log_error(f"settlement: {exc}")

    return settled_tickets


def _describe_match_markets(match: dict, probs: dict, candidates: list) -> str:
    """Résumé compact de tous les marchés d'un match, pour le journal —
    permet de comprendre gains/pertes même sur les marchés jamais pariés
    (double chance, résultat+buts : toujours estimés, voir src/markets)."""
    def fmt(c):
        tag = " (estimé)" if c["is_estimated"] else ""
        return f"{c['selection']}@{c['odds']:.2f}({c['prob']:.0%}){tag}"

    by_market = {}
    for c in candidates:
        by_market.setdefault(c["market"], []).append(c)

    parts = [f"{match['home_team']} vs {match['away_team']} ({match['league']})"]
    labels = {"1x2": "1X2", "double_chance": "Double chance", "totals": "Buts", "result_and_goals": "Résultat+buts"}
    for market, label in labels.items():
        if market in by_market:
            parts.append(f"{label}: " + " ".join(fmt(c) for c in by_market[market]))
    return " | ".join(parts)


def _describe_match_context(match: dict, european_matches: list) -> str:
    """Contexte optionnel — blessures (API-Football) et match européen
    récent (football-data.org) — pour lecture humaine uniquement (voir
    src/external_data.py : ni l'un ni l'autre n'entraîne le modèle).
    Chaîne vide si rien d'utile (pas de clé configurée, rien trouvé) :
    l'appelant n'ajoute alors rien au journal."""
    parts = []
    for label, team in (("dom.", match["home_team"]), ("ext.", match["away_team"])):
        bits = []
        injuries = external_data.fetch_injury_count(team)
        if injuries is not None:
            bits.append(f"{injuries} blessé(s)")
        # Deux avis indépendants sur la coupe d'Europe : API-Football (par
        # id d'équipe, plus fiable) en premier, football-data.org (par
        # rapprochement de nom) en repli si le premier ne trouve rien.
        euro_date = external_data.fetch_recent_uefa_fixture(team) or \
            external_data.played_in_europe_recently(team, european_matches)
        if euro_date:
            bits.append(f"a joué en coupe d'Europe le {euro_date[:10]}")
        if bits:
            parts.append(f"{label} {team} : " + ", ".join(bits))
    return " | ".join(parts)


def _place_new_bets(state: dict, errors: list) -> int:
    """Place de nouveaux tickets (seuls ou combinés jusqu'à 3 matchs) sur
    les matchs à venir si l'EV le justifie — voir src/strategy.build_tickets.
    Ne fait rien (et ne consomme aucun crédit API) tant que le throttle
    ODDS_FETCH_INTERVAL_HOURS n'est pas écoulé."""
    hours_since = _hours_since(state.get("last_odds_fetch_at"))
    if hours_since is not None and hours_since < config.ODDS_FETCH_INTERVAL_HOURS:
        return 0

    drawdown_floor = config.INITIAL_BANKROLL * config.DRAWDOWN_STOP_FRACTION
    if state["bankroll"] < drawdown_floor:
        supabase_state.log_journal_entry(
            "bot",
            f"Coupe-circuit : bankroll ({state['bankroll']:.2f}) sous le seuil de "
            f"{drawdown_floor:.2f} ({config.DRAWDOWN_STOP_FRACTION:.0%} du capital de départ) — "
            "plus aucun nouveau pari tant qu'elle n'est pas remontée au-dessus. Les paris déjà "
            "en cours continuent d'être réglés normalement.",
            {"event": "circuit_breaker"},
        )
        state["last_odds_fetch_at"] = _now().isoformat()
        return 0

    if _quota_exhausted(state):
        supabase_state.log_journal_entry(
            "bot",
            f"Quota The Odds API presque épuisé ({state['odds_api_remaining']} restants) — "
            "pas de nouveaux matchs récupérés ce cycle.",
            {"event": "quota_low"},
        )
        state["last_odds_fetch_at"] = _now().isoformat()
        return 0

    tickets_placed = 0
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
        upcoming, api_remaining = data_loader.fetch_upcoming_matches(
            leagues=list(config.LEAGUES.keys()), known_team_names=known,
        )
        if api_remaining is not None:
            state["odds_api_remaining"] = api_remaining

        try:
            external_data.reset_call_budget()
            european_matches = external_data.fetch_recent_european_matches()
        except Exception:
            european_matches = []

        round_matches = []
        for match in upcoming:
            if supabase_state.leg_exists(match["home_team"], match["away_team"], match["commence_time"]):
                continue

            X_match = features.build_features_for_match(
                history_df, match["home_team"], match["away_team"], match_date=match["commence_time"],
            )
            if X_match is None:
                supabase_state.log_journal_entry(
                    "bot",
                    f"Historique insuffisant pour {match['home_team']} vs {match['away_team']}, match ignoré.",
                    {"event": "unmatched_team"},
                )
                continue

            lambda_home, lambda_away = model_module.predict_goal_rates(trained_model, X_match)
            probs = markets.market_probabilities(lambda_home[0], lambda_away[0])
            candidates = markets.build_candidates(probs, match["odds"])

            message = _describe_match_markets(match, probs, candidates)
            try:
                context = _describe_match_context(match, european_matches)
            except Exception:
                context = ""
            if context:
                message += f" || Contexte : {context}"
            supabase_state.log_journal_entry("bot", message, {"event": "match_preview"})
            round_matches.append({"match": match, "candidates": candidates})

        for ticket in strategy.build_tickets(round_matches):
            stake = strategy.compute_stake(state["bankroll"], ticket["prob"], ticket["odds"])
            if stake <= 0:
                continue

            state["bankroll"] -= stake
            legs_payload = [{
                "league": leg["match"]["league"],
                "home_team": leg["match"]["home_team"],
                "away_team": leg["match"]["away_team"],
                "commence_time": leg["match"]["commence_time"],
                "market": leg["market"],
                "selection": leg["selection"],
                "prob": round(leg["prob"], 4),
                "odds": leg["odds"],
            } for leg in ticket["legs"]]
            supabase_state.insert_ticket(stake, round(ticket["prob"], 6), ticket["odds"], round(ticket["ev"], 4), legs_payload)

            kind = "combiné" if len(ticket["legs"]) > 1 else "pari"
            legs_desc = ", ".join(f"{leg['match']['home_team']}-{leg['match']['away_team']}:{leg['selection']}@{leg['odds']:.2f}" for leg in ticket["legs"])
            supabase_state.log_journal_entry(
                "bot",
                f"Nouveau {kind} ({len(ticket['legs'])} jambe(s)) : {legs_desc} — "
                f"prob {ticket['prob']:.1%}, cote {ticket['odds']:.2f}, EV {ticket['ev']:+.1%}, mise {stake:.2f}.",
                {"event": "new_bet"},
            )
            tickets_placed += 1

        state["last_odds_fetch_at"] = _now().isoformat()
    except Exception as exc:
        errors.append(f"placement: {exc}")
        supabase_state.log_error(f"placement: {exc}")

    return tickets_placed


def run_tick() -> dict:
    errors = []
    state = supabase_state.load_state(config.INITIAL_BANKROLL)

    tickets_settled = _settle_pending_bets(state, errors)
    tickets_placed = _place_new_bets(state, errors)

    supabase_state.save_state(state)

    return {
        "tickets_placed": tickets_placed,
        "tickets_settled": tickets_settled,
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

    recent_tickets = _safe_call(supabase_state.get_recent_tickets, 15, default=[])
    settled_tickets = _safe_call(supabase_state.get_settled_tickets_chronological, 300, default=[])
    journal = _safe_call(supabase_state.get_recent_journal, 40, default=[])
    errors = _safe_call(supabase_state.get_recent_errors, 5, default=[])
    model_row = _safe_call(supabase_state.load_model, default={})

    return _render_status_page(bankroll, recent_tickets, settled_tickets, journal, errors, model_row)


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


@app.route("/admin/retrain")
def admin_retrain():
    """Déclenche retrain.py directement depuis ce service — voir le
    docstring en tête de fichier. Protégé par RETRAIN_TOKEN (absent =
    endpoint désactivé, même logique que /alert-test). Synchrone : le
    ré-entraînement complet (téléchargement + entraînement + backtest)
    prend quelques secondes, largement sous le timeout gunicorn (60s)."""
    expected = os.environ.get("RETRAIN_TOKEN")
    if not expected:
        return jsonify({"error": "RETRAIN_TOKEN n'est pas défini — endpoint désactivé."}), 404
    if request.args.get("token") != expected:
        return jsonify({"error": "jeton invalide"}), 403

    import retrain as retrain_module

    try:
        result = retrain_module.run(dry_run=request.args.get("dry_run") == "1")
        return jsonify(result), 200
    except Exception as exc:
        supabase_state.log_error(f"admin/retrain: {exc}")
        return jsonify({"error": str(exc)}), 500


def _nice_step(raw_step: float) -> float:
    """Arrondit un pas d'axe au nombre rond juste au-dessus (1/2/5 × 10^n) —
    évite des graduations comme 733,4 sur l'axe Y."""
    if raw_step <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for mult in (1, 2, 5, 10):
        step = mult * magnitude
        if step >= raw_step:
            return step
    return 10 * magnitude


def _build_bankroll_chart_svg(points: list) -> str:
    """Graphique ligne SVG autonome (aucun JS/dépendance externe) de
    l'évolution de la bankroll, un point par pari réglé. points :
    [(datetime, float), ...] déjà triés croissants. Retourne "" si moins
    de 2 points (rien à tracer)."""
    if len(points) < 2:
        return ""

    W, H = 640, 220
    PAD_L, PAD_R, PAD_T, PAD_B = 46, 74, 16, 28

    t_min, t_max = points[0][0], points[-1][0]
    if t_max == t_min:
        t_max = t_min + timedelta(hours=1)

    values = [v for _, v in points] + [config.INITIAL_BANKROLL]
    v_min, v_max = min(values), max(values)
    v_range = max(v_max - v_min, 1.0)
    v_pad = v_range * 0.15
    v_min, v_max = v_min - v_pad, v_max + v_pad

    def x_of(t):
        frac = (t - t_min).total_seconds() / (t_max - t_min).total_seconds()
        return PAD_L + frac * (W - PAD_L - PAD_R)

    def y_of(v):
        frac = (v - v_min) / (v_max - v_min)
        return H - PAD_B - frac * (H - PAD_T - PAD_B)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
             f'aria-label="Évolution de la bankroll au fil des paris réglés">']

    ref_y = y_of(config.INITIAL_BANKROLL)
    parts.append(f'<line x1="{PAD_L}" y1="{ref_y:.1f}" x2="{W - PAD_R}" y2="{ref_y:.1f}" '
                 f'stroke="var(--rule)" stroke-width="1" stroke-dasharray="3,3"/>')

    tick_step = _nice_step((v_max - v_min) / 4)
    tick = tick_step * round(v_min / tick_step)
    while tick <= v_max:
        if tick > v_min:
            y = y_of(tick)
            parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                         f'stroke="var(--rule)" stroke-width="1" opacity="0.5"/>')
            parts.append(f'<text x="{PAD_L - 6:.1f}" y="{y + 3:.1f}" font-size="9" fill="var(--text-faint)" '
                         f'text-anchor="end" font-family="IBM Plex Mono, monospace">{tick:,.0f}</text>')
        tick += tick_step

    for frac, anchor in ((0.0, "start"), (0.5, "middle"), (1.0, "end")):
        t = t_min + (t_max - t_min) * frac
        parts.append(f'<text x="{x_of(t):.1f}" y="{H - 8}" font-size="9" fill="var(--text-faint)" '
                     f'text-anchor="{anchor}" font-family="IBM Plex Mono, monospace">{t.strftime("%d/%m")}</text>')

    poly = " ".join(f"{x_of(t):.1f},{y_of(v):.1f}" for t, v in points)
    parts.append(f'<polyline points="{poly}" fill="none" stroke="var(--series-4)" '
                 f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    last_t, last_v = points[-1]
    lx, ly = x_of(last_t), y_of(last_v)
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="7" fill="var(--card)"/>')
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="5" fill="var(--series-4)"/>')
    parts.append(f'<text x="{lx + 10:.1f}" y="{ly + 3:.1f}" font-size="11" fill="var(--text)" '
                 f'font-family="IBM Plex Sans, sans-serif">{last_v:,.0f}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _render_status_page(bankroll, recent_tickets, settled_tickets, journal, errors, model_row) -> str:
    label = os.environ.get("BOT_LABEL") or "bot de paris football virtuels"
    metrics = (model_row or {}).get("backtest_metrics") or {}
    trained_at = (model_row or {}).get("trained_at") or None

    def esc(value) -> str:
        return html.escape(str(value)) if value is not None else ""

    def fmt_ts(value) -> str:
        dt = _parse_iso(value) if value else None
        return dt.strftime("%d/%m %H:%M") if dt else "—"

    chart_points = []
    for t in settled_tickets:
        dt = _parse_iso(t.get("settled_at"))
        if dt is not None and t.get("bankroll_after") is not None:
            chart_points.append((dt, float(t["bankroll_after"])))
    chart_svg = _build_bankroll_chart_svg(chart_points)

    pnl_since_start = bankroll - config.INITIAL_BANKROLL
    roi_live_pct = pnl_since_start / config.INITIAL_BANKROLL * 100

    def ticket_card(t) -> str:
        legs = sorted(t.get("legs") or [], key=lambda leg: leg.get("commence_time") or "")
        kind = "Combiné" if len(legs) > 1 else "Simple"
        legs_html = "".join(
            f'<div class="leg"><span class="leg-match">{esc(leg.get("home_team"))} – {esc(leg.get("away_team"))} '
            f'<span class="bet-league">{esc(leg.get("league"))}</span></span>'
            f'<span class="pill pill-{esc(leg.get("result"))}">{esc(leg.get("selection"))} @ {esc(leg.get("odds"))}</span></div>'
            for leg in legs
        )
        return (
            f'<div class="bet-row"><div class="bet-match">{kind} · {len(legs)} match(s) '
            f'<span class="bet-league">cote {esc(t.get("odds"))} · prob {float(t.get("prob") or 0):.0%}</span></div>'
            f'{legs_html}'
            f'<div class="bet-meta"><span class="pill pill-{esc(t.get("status"))}">{esc(t.get("status"))}</span>'
            f'<span class="mono">mise {esc(t.get("stake"))}</span>'
            f'<span class="mono">{esc(t.get("pnl")) if t.get("pnl") is not None else ""}</span></div></div>'
        )

    bet_cards = "".join(ticket_card(t) for t in recent_tickets) or '<p class="empty">Aucun pari pour l\'instant.</p>'

    is_preview = lambda j: (j.get("data") or {}).get("event") == "match_preview"
    main_journal = [j for j in journal if not is_preview(j)][:12]
    preview_journal = [j for j in journal if is_preview(j)][:10]

    journal_items = "".join(
        f'<li><span class="mono">{fmt_ts(j.get("ts"))}</span> {esc(j.get("message"))}</li>'
        for j in main_journal
    ) or '<li class="empty">Rien pour l\'instant.</li>'

    preview_items = "".join(
        f'<li><span class="mono">{fmt_ts(j.get("ts"))}</span> {esc(j.get("message"))}</li>'
        for j in preview_journal
    )
    preview_block = (
        f'<div class="card"><h2>Marchés examinés récemment</h2>'
        f'<p class="sub">Toutes les probabilités/cotes vues par le bot, y compris les marchés jamais '
        f'pariés (double chance, résultat+buts : toujours estimés — voir README).</p>'
        f'<ul class="list">{preview_items}</ul></div>'
        if preview_items else ""
    )

    errors_block = ""
    if errors:
        errors_items = "".join(f'<li><span class="mono">{fmt_ts(e.get("ts"))}</span> {esc(e.get("message"))}</li>' for e in errors)
        errors_block = f'<div class="card"><h2>Erreurs récentes</h2><ul class="list">{errors_items}</ul></div>'

    chart_block = (
        f'<div class="chart-wrap">{chart_svg}</div>'
        if chart_svg else
        '<p class="empty">La courbe apparaîtra dès le premier pari réglé.</p>'
    )

    trained_line = (
        f'Entraîné le {esc(trained_at)[:16].replace("T", " ")} UTC — backtest : '
        f'ROI {esc(metrics.get("roi_pct"))}% · yield {esc(metrics.get("yield_pct"))}% · '
        f'{esc(metrics.get("num_bets"))} tickets · win rate {esc(metrics.get("win_rate"))}% · '
        f'drawdown max {esc(metrics.get("max_drawdown_pct"))}% '
        f'<span class="sub">(backtest 1X2 uniquement, pas de cotes over/under historiques — voir README)</span>'
        if trained_at else
        "Pas encore de modèle entraîné — voir DEPLOIEMENT.md."
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(label)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;1,9..144,500&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{{
  --bg:#F5F5F3; --text:#1C1C1A; --text-muted:#767671; --text-faint:#A5A59F;
  --rule:#DBDBD6; --card:#EBEBE7; --series-4:#8558d3;
  --pill-won-bg:#e4f1e8; --pill-won-text:#3E7A52;
  --pill-lost-bg:#f6e6e4; --pill-lost-text:#A3453A;
  --pill-pending-bg:#eeeae4; --pill-pending-text:#767671;
}}
@media (prefers-color-scheme: dark){{
  :root{{
    --bg:#17181A; --text:#E7E6E1; --text-muted:#8E8E88; --text-faint:#5C5D59;
    --rule:#333432; --card:#1F2022; --series-4:#9a72dd;
    --pill-won-bg:#1c2c22; --pill-won-text:#6FAE87;
    --pill-lost-bg:#2c1e1c; --pill-lost-text:#D08076;
    --pill-pending-bg:#232323; --pill-pending-text:#8E8E88;
  }}
}}
*{{ box-sizing:border-box; margin:0; }}
body{{ background:var(--bg); color:var(--text); font-family:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif; line-height:1.55; }}
.wrap{{ max-width:640px; margin:0 auto; padding:40px 20px 64px; }}
.mono{{ font-family:"IBM Plex Mono", ui-monospace, monospace; font-variant-numeric:tabular-nums; }}
.kicker{{ font-family:"IBM Plex Mono", monospace; font-size:0.7rem; letter-spacing:0.12em; text-transform:uppercase; color:var(--text-faint); margin-bottom:8px; }}
h1{{ font-family:"Fraunces", serif; font-weight:500; font-size:1.7rem; margin-bottom:4px; }}
.disclaimer{{ font-size:0.8rem; color:var(--text-muted); margin-bottom:28px; }}
.card{{ background:var(--card); border-radius:14px; padding:20px 22px; margin-bottom:18px; }}
.bankroll-value{{ font-family:"IBM Plex Mono", monospace; font-size:2.4rem; font-weight:500; margin:4px 0 2px; }}
.bankroll-delta{{ font-size:0.85rem; font-weight:500; }}
.bankroll-delta.pos{{ color:#3E7A52; }}
.bankroll-delta.neg{{ color:#A3453A; }}
.chart-wrap{{ margin-top:14px; }}
.empty{{ font-size:0.82rem; color:var(--text-faint); }}
h2{{ font-size:0.92rem; font-weight:500; margin-bottom:12px; }}
.sub{{ font-size:0.78rem; color:var(--text-muted); margin-bottom:14px; }}
.bet-row{{ padding:10px 0; border-bottom:1px solid var(--rule); }}
.bet-row:last-child{{ border-bottom:none; }}
.bet-match{{ font-size:0.92rem; font-weight:500; margin-bottom:4px; }}
.bet-league{{ font-size:0.7rem; color:var(--text-faint); font-weight:400; }}
.bet-meta{{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; font-size:0.78rem; color:var(--text-muted); }}
.leg{{ display:flex; justify-content:space-between; align-items:center; gap:10px; font-size:0.82rem; padding:4px 0; }}
.leg-match{{ color:var(--text-muted); }}
.pill{{ padding:2px 8px; border-radius:999px; font-size:0.72rem; font-weight:500; }}
.pill-won{{ background:var(--pill-won-bg); color:var(--pill-won-text); }}
.pill-lost{{ background:var(--pill-lost-bg); color:var(--pill-lost-text); }}
.pill-pending{{ background:var(--pill-pending-bg); color:var(--pill-pending-text); }}
.list{{ list-style:none; font-size:0.82rem; }}
.list li{{ padding:6px 0; border-bottom:1px solid var(--rule); display:flex; gap:10px; }}
.list li:last-child{{ border-bottom:none; }}
.list li .mono{{ color:var(--text-faint); flex-shrink:0; }}
footer{{ margin-top:24px; font-size:0.76rem; color:var(--text-faint); text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <p class="kicker">⚽ Paris virtuels</p>
  <h1>{esc(label)}</h1>
  <p class="disclaimer">100% éducatif — aucun argent réel n'est en jeu, aucun pari réel n'est placé.</p>

  <div class="card">
    <div class="kicker">Bankroll</div>
    <div class="bankroll-value">{bankroll:,.2f}</div>
    <div class="bankroll-delta {'pos' if pnl_since_start >= 0 else 'neg'}">
      {'+' if pnl_since_start >= 0 else ''}{pnl_since_start:,.2f} depuis le départ ({roi_live_pct:+.1f}%)
    </div>
    {chart_block}
  </div>

  <div class="card">
    <h2>Dernier modèle entraîné</h2>
    <p class="sub">{trained_line}</p>
  </div>

  <div class="card">
    <h2>Derniers tickets</h2>
    {bet_cards}
  </div>

  <div class="card">
    <h2>Journal</h2>
    <ul class="list">{journal_items}</ul>
  </div>

  {preview_block}

  {errors_block}

  <footer>Lecture seule — <span class="mono">/tick</span> déclenche un cycle (pensé pour UptimeRobot).</footer>
</div>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
