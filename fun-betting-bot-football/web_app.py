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
               football-data.co.uk y fonctionne. Asynchrone (thread
               d'arrière-plan) : répond tout de suite, le résultat se
               consulte sur / une fois terminé (voir admin_retrain).

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
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

import alerts
from src import config, data_loader, external_data, features, markets, model as model_module, strategy, supabase_state
from src.team_names import normalize_team_name

app = Flask(__name__)

# Toutes les dates/heures internes (Supabase, calculs de throttle) restent en
# UTC — seul l'AFFICHAGE sur la page de statut est converti en heure
# française. Europe/Paris plutôt qu'un décalage fixe : bascule automatique
# CET (UTC+1)/CEST (UTC+2) selon l'heure d'été, toujours juste.
_LOCAL_TZ = ZoneInfo("Europe/Paris")


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

        # Aussi TOUS les tickets encore "pending" en base, pas seulement ceux
        # dont une jambe vient d'être résolue CE cycle-ci : un combiné dont
        # une jambe est déjà "lost" (résolue lors d'un cycle précédent) doit
        # être finalisé dès qu'on le détecte, pas seulement s'il se trouve
        # qu'une AUTRE de ses jambes est retouchée ce cycle-là — sinon il
        # reste "pending" indéfiniment tant que son match le plus tardif
        # n'est pas joué, alors que l'issue est déjà mathématiquement
        # certaine (bug vécu en prod : bankroll affichée fausse pendant
        # plusieurs jours). Coût négligeable à cette échelle (quelques
        # dizaines de tickets tout au plus).
        touched_bet_ids |= set(supabase_state.get_pending_ticket_ids())

        for bet_id in touched_bet_ids:
            ticket = supabase_state.get_ticket(bet_id)
            if ticket["status"] != "pending":
                continue  # déjà réglé (ex: lors d'un cycle précédent)

            legs = supabase_state.get_ticket_legs(bet_id)
            any_leg_lost = any(leg["result"] == "lost" for leg in legs)
            if not any_leg_lost and any(leg["result"] == "pending" for leg in legs):
                continue  # pas encore décidé : aucune jambe perdante, mais il en reste en attente

            # PostgREST sérialise les colonnes numeric en chaînes (précision
            # arbitraire) — jamais castées jusqu'ici, ce qui aurait planté
            # au tout premier ticket réglé (aucun ne l'avait encore été).
            stake = float(ticket["stake"])
            odds = float(ticket["odds"])
            won = not any_leg_lost
            pnl = stake * (odds - 1) if won else -stake
            payout = stake * odds if won else 0.0
            state["bankroll"] += payout
            supabase_state.finalize_ticket(bet_id, "won" if won else "lost", round(pnl, 2), round(state["bankroll"], 2))

            kind = "Pari simple" if len(legs) == 1 else f"Combiné ({len(legs)} matchs)"
            legs_desc = ", ".join(
                f"{leg['home_team']}-{leg['away_team']} : "
                f"{_describe_selection(leg['market'], leg['selection'], leg['home_team'], leg['away_team'])}"
                for leg in legs
            )
            supabase_state.log_journal_entry(
                "bot",
                f"Ticket réglé ({kind}) : {legs_desc} — "
                f"{'gagné' if won else 'perdu'} ({pnl:+.2f}€). Bankroll : {state['bankroll']:.2f}€.",
                {"event": "settlement"},
            )
            settled_tickets += 1

        state["last_scores_fetch_at"] = _now().isoformat()
    except Exception as exc:
        errors.append(f"settlement: {exc}")
        supabase_state.log_error(f"settlement: {exc}")

    return settled_tickets


def _league_name(code: str) -> str:
    """Nom lisible d'une ligue (ex: "E0" -> "Premier League") — voir
    config.LEAGUES. Retourne le code tel quel s'il est inconnu plutôt que
    de planter l'affichage."""
    return config.LEAGUES.get(code, code)


def _fmt_local(value: str | None) -> str:
    """Date/heure d'un match en heure française (voir _LOCAL_TZ), pour
    affichage seulement — les timestamps internes restent en UTC."""
    dt = _parse_iso(value) if value else None
    return dt.astimezone(_LOCAL_TZ).strftime("%d/%m %H:%M %Z") if dt else "—"


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _describe_selection(market: str, selection: str, home_team: str, away_team: str) -> str:
    """Traduit un code de sélection interne (ex: "H", "Over 2.5", "1X",
    "H & Over 2.5" — voir src/markets.py) en français lisible, pour que le
    pari soit compréhensible sans connaître les conventions internes du
    bot."""
    def outcome(code: str) -> str:
        return {"H": f"Victoire {home_team}", "D": "Match nul", "A": f"Victoire {away_team}"}.get(code, code)

    def goals(direction: str, line: str) -> str:
        word = "Plus" if direction == "Over" else "Moins"
        return f"{word} de {line.replace('.', ',')} buts"

    if market == "1x2":
        return outcome(selection)
    if market == "double_chance":
        return {
            "1X": f"{home_team} ou nul",
            "X2": f"{away_team} ou nul",
            "12": f"{home_team} ou {away_team} (pas de nul)",
        }.get(selection, selection)
    if market == "totals":
        direction, line = selection.split()
        return goals(direction, line)
    if market == "result_and_goals":
        outcome_code, _, goals_part = selection.partition(" & ")
        direction, line = goals_part.split()
        return f"{outcome(outcome_code)} & {goals(direction, line).lower()}"
    return selection


def _describe_match_markets(match: dict, probs: dict, candidates: list) -> str:
    """Résumé compact de tous les marchés d'un match, pour le journal —
    permet de comprendre gains/pertes même sur les marchés jamais pariés
    (double chance, résultat+buts : toujours estimés, voir src/markets)."""
    def fmt(c):
        tag = " (estimé)" if c["is_estimated"] else ""
        label = _describe_selection(c["market"], c["selection"], match["home_team"], match["away_team"])
        return f"{label}@{c['odds']:.2f}({c['prob']:.0%}){tag}"

    by_market = {}
    for c in candidates:
        by_market.setdefault(c["market"], []).append(c)

    parts = [
        f"{match['home_team']} vs {match['away_team']} "
        f"({_league_name(match['league'])}, {_fmt_local(match.get('commence_time'))})"
    ]
    labels = {"1x2": "1X2", "double_chance": "Double chance", "totals": "Buts", "result_and_goals": "Résultat+buts"}
    for market, label in labels.items():
        if market in by_market:
            parts.append(f"{label}: " + " ".join(fmt(c) for c in by_market[market]))
    return " | ".join(parts)


def _describe_match_context(match: dict, european_matches: list, deadline: float) -> str:
    """Contexte optionnel — blessures (API-Football) et match européen
    récent (football-data.org) — pour lecture humaine uniquement (voir
    src/external_data.py : ni l'un ni l'autre n'entraîne le modèle).
    Chaîne vide si rien d'utile (pas de clé configurée, rien trouvé) :
    l'appelant n'ajoute alors rien au journal.

    `deadline` (time.monotonic()) est revérifié à CHAQUE équipe, pas
    seulement une fois par match côté appelant : une seule équipe lente
    (plusieurs appels réseau, chacun avec son propre timeout) pouvait à
    elle seule épuiser tout le budget de temps du cycle avant même
    d'atteindre la deuxième équipe du premier match — vécu en prod (1 seul
    appel API-Football consommé, 0 ligne "Contexte" sur 20+ matchs)."""
    parts = []
    for label, team in (("dom.", match["home_team"]), ("ext.", match["away_team"])):
        if time.monotonic() >= deadline:
            break
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


def _team_logo_html(team_name: str) -> str:
    """<img> discret devant le nom d'une équipe (TheSportsDB, purement
    cosmétique — voir src/external_data.fetch_team_logo_url). Chaîne vide
    si pas de logo trouvé plutôt qu'une icône d'image cassée ; onerror
    retire aussi la balise côté client si l'URL cesse de répondre après
    coup (logo supprimé/renommé chez TheSportsDB)."""
    url = external_data.fetch_team_logo_url(team_name)
    if not url:
        return ""
    return f'<img class="team-logo" src="{_esc(url)}" alt="" onerror="this.remove()">'


def _ticket_card(t: dict) -> str:
    """Une carte HTML pour un ticket (pari seul ou combiné), utilisée à la
    fois sur la page de statut (paris en cours + 3 derniers réglés) et sur
    /historique (tout l'historique réglé) — voir _render_status_page et
    _render_history_page."""
    legs = sorted(t.get("legs") or [], key=lambda leg: leg.get("commence_time") or "")
    kind = "Pari simple" if len(legs) == 1 else "Combiné"
    stake = float(t.get("stake") or 0)
    odds = float(t.get("odds") or 0)
    potential_gain = stake * (odds - 1)
    pnl = t.get("pnl")
    pnl_value = float(pnl) if pnl is not None else None
    pnl_text = f'{"gagné" if pnl_value >= 0 else "perdu"} {pnl_value:+.2f}€' if pnl_value is not None else ""

    def leg_html(leg) -> str:
        label = _describe_selection(
            leg.get("market"), leg.get("selection"), leg.get("home_team"), leg.get("away_team"),
        )
        return (
            f'<div class="leg"><span class="leg-match">'
            f'{_team_logo_html(leg.get("home_team"))}{_esc(leg.get("home_team"))} – '
            f'{_team_logo_html(leg.get("away_team"))}{_esc(leg.get("away_team"))} '
            f'<span class="bet-league">{_esc(_league_name(leg.get("league")))} · '
            f'{_esc(_fmt_local(leg.get("commence_time")))}</span></span>'
            f'<span class="pill pill-{_esc(leg.get("result"))}">{_esc(label)} @ {float(leg.get("odds") or 0):.2f}</span></div>'
        )

    legs_html = "".join(leg_html(leg) for leg in legs)
    return (
        f'<div class="bet-row"><div class="bet-match">{kind} · {len(legs)} match(s) '
        f'<span class="bet-league">cote totale {odds:.2f} · prob {float(t.get("prob") or 0):.0%}</span></div>'
        f'{legs_html}'
        f'<div class="bet-meta"><span class="pill pill-{_esc(t.get("status"))}">{_esc(t.get("status"))}</span>'
        f'<span class="mono">mise {stake:.2f}€ → gain potentiel +{potential_gain:.2f}€</span>'
        f'<span class="mono">{pnl_text}</span></div></div>'
    )


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

        # Ne garde que les matchs à J+UPCOMING_MATCH_WINDOW_DAYS max — voir
        # config.py : évite de consommer du quota API-Football/Supabase sur
        # des matchs encore lointains à chaque cycle. Un match plus loin
        # entrera dans la fenêtre de lui-même lors d'un prochain fetch.
        horizon = _now() + timedelta(days=config.UPCOMING_MATCH_WINDOW_DAYS)
        upcoming = [
            m for m in upcoming
            if (dt := _parse_iso(m.get("commence_time"))) is not None and dt <= horizon
        ]

        # Budget de temps global (pas juste par appel) pour l'enrichissement
        # contexte (blessures/coupe d'Europe) : chaque appel réseau a déjà
        # son propre timeout (voir supabase_state.get_team_ref,
        # external_data._api_football_get), mais avec plusieurs matchs à
        # 2 équipes chacune, la SOMME de ces appels peut quand même dépasser
        # le timeout gunicorn (60s) et faire planter le worker en plein
        # cycle — vécu en prod (WORKER TIMEOUT / SIGKILL, /tick en 500,
        # aucun ticket placé) alors même que chaque fonction de
        # src/external_data.py est individuellement best-effort. Passé ce
        # budget, on continue à évaluer/parier normalement mais sans
        # contexte affiché pour les matchs restants — jamais l'inverse.
        # Doit être posé AVANT le fetch européen ci-dessous : sinon ce fetch
        # (jusqu'à 2 appels réseau) tournait hors budget, et le budget lui-même
        # n'était revérifié qu'une fois par MATCH — une seule équipe lente
        # suffisait à tout consommer avant la deuxième équipe du premier
        # match (vécu en prod : 1 appel API-Football, 0 ligne "Contexte" sur
        # 20+ matchs). Revérifié maintenant à chaque équipe (voir
        # _describe_match_context) et avant le fetch européen lui-même.
        context_deadline = time.monotonic() + 20

        try:
            external_data.reset_call_budget()
            european_matches = (
                external_data.fetch_recent_european_matches() if time.monotonic() < context_deadline else []
            )
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
            context = ""
            if time.monotonic() < context_deadline:
                try:
                    context = _describe_match_context(match, european_matches, context_deadline)
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

            kind = "Pari simple" if len(ticket["legs"]) == 1 else f"Combiné ({len(ticket['legs'])} matchs)"
            legs_desc = ", ".join(
                f"{leg['match']['home_team']}-{leg['match']['away_team']} "
                f"({_fmt_local(leg['match'].get('commence_time'))}) : "
                f"{_describe_selection(leg['market'], leg['selection'], leg['match']['home_team'], leg['match']['away_team'])} "
                f"@ {leg['odds']:.2f}"
                for leg in ticket["legs"]
            )
            potential_gain = stake * (ticket["odds"] - 1)
            supabase_state.log_journal_entry(
                "bot",
                f"Nouveau {kind} : {legs_desc} — "
                f"prob {ticket['prob']:.1%}, cote totale {ticket['odds']:.2f}, EV {ticket['ev']:+.1%}, "
                f"mise {stake:.2f}€ → gain potentiel +{potential_gain:.2f}€.",
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

    pending_tickets = _safe_call(supabase_state.get_pending_tickets, default=[])
    settled_tickets = _safe_call(supabase_state.get_settled_tickets_chronological, 300, default=[])
    journal = _safe_call(supabase_state.get_recent_journal, 40, default=[])
    errors = _safe_call(supabase_state.get_recent_errors, 5, default=[])
    model_row = _safe_call(supabase_state.load_model, default={})

    return _render_status_page(
        bankroll, pending_tickets, settled_tickets, journal, errors, model_row,
    )


@app.route("/historique")
def history():
    settled_tickets = _safe_call(supabase_state.get_settled_tickets_history, 500, default=[])
    return _render_history_page(settled_tickets)


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


_retrain_lock = threading.Lock()


def _run_retrain_background(dry_run: bool) -> None:
    """Exécuté hors requête HTTP (voir admin_retrain) — le résultat n'est
    donc visible que via le journal/`/` (backtest_metrics, trained_at), pas
    dans une réponse JSON directe."""
    import retrain as retrain_module

    try:
        result = retrain_module.run(dry_run=dry_run)
        if dry_run:
            # dry_run n'écrit rien dans Supabase (voir retrain.run) — sans ce
            # log, le résultat d'un essai --dry-run serait invisible puisque
            # cette fonction tourne hors requête HTTP (pas de réponse JSON
            # à laquelle l'accrocher).
            supabase_state.log_journal_entry(
                "bot", f"Ré-entraînement (dry-run) : {result['backtest_metrics']}",
            )
    except Exception as exc:
        supabase_state.log_error(f"admin/retrain: {exc}")
        supabase_state.log_journal_entry(
            "bot", f"Ré-entraînement en échec ({exc}) — le modèle précédent reste en place.",
        )
    finally:
        _retrain_lock.release()


@app.route("/admin/retrain")
def admin_retrain():
    """Déclenche retrain.py depuis ce service — voir le docstring en tête de
    fichier. Protégé par RETRAIN_TOKEN (absent = endpoint désactivé, même
    logique que /alert-test).

    Asynchrone (thread d'arrière-plan) : avec 5 championnats × ~8 saisons,
    le pipeline complet (téléchargement de ~40 CSV + upsert de l'historique
    + entraînement + backtest) peut dépasser le timeout gunicorn (60s,
    voir render.yaml) — gunicorn tuait alors le worker en plein calcul,
    renvoyant une erreur générique "Internal server error" sans passer par
    notre gestion d'erreur (observé en prod le 2026-09-14, après la
    correction des deux bugs précédents sur ce même endpoint). Le résultat
    se consulte sur `/` (section "Dernier modèle entraîné") une fois le
    ré-entraînement terminé, en général sous 2-3 minutes."""
    expected = os.environ.get("RETRAIN_TOKEN")
    if not expected:
        return jsonify({"error": "RETRAIN_TOKEN n'est pas défini — endpoint désactivé."}), 404
    if request.args.get("token") != expected:
        return jsonify({"error": "jeton invalide"}), 403

    if not _retrain_lock.acquire(blocking=False):
        return jsonify({"status": "already_running", "message": "Un ré-entraînement est déjà en cours."}), 409

    dry_run = request.args.get("dry_run") == "1"
    threading.Thread(target=_run_retrain_background, args=(dry_run,), daemon=True).start()
    return jsonify({
        "status": "started",
        "message": "Ré-entraînement lancé en arrière-plan — regarde la section "
                    "\"Dernier modèle entraîné\" sur / dans 2-3 minutes.",
    }), 202


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


# Styles partagés par la page de statut (/) et l'historique (/historique)
# — extrait une fois pour ne jamais dupliquer ce bloc entre les deux pages.
_PAGE_STYLE = """
:root{
  --bg:#F5F5F3; --text:#1C1C1A; --text-muted:#767671; --text-faint:#A5A59F;
  --rule:#DBDBD6; --card:#EBEBE7; --series-4:#8558d3;
  --pill-won-bg:#e4f1e8; --pill-won-text:#3E7A52;
  --pill-lost-bg:#f6e6e4; --pill-lost-text:#A3453A;
  --pill-pending-bg:#eeeae4; --pill-pending-text:#767671;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#17181A; --text:#E7E6E1; --text-muted:#8E8E88; --text-faint:#5C5D59;
    --rule:#333432; --card:#1F2022; --series-4:#9a72dd;
    --pill-won-bg:#1c2c22; --pill-won-text:#6FAE87;
    --pill-lost-bg:#2c1e1c; --pill-lost-text:#D08076;
    --pill-pending-bg:#232323; --pill-pending-text:#8E8E88;
  }
}
*{ box-sizing:border-box; margin:0; }
body{ background:var(--bg); color:var(--text); font-family:"IBM Plex Sans", ui-sans-serif, system-ui, sans-serif; line-height:1.55; }
.wrap{ max-width:640px; margin:0 auto; padding:40px 20px 64px; }
.mono{ font-family:"IBM Plex Mono", ui-monospace, monospace; font-variant-numeric:tabular-nums; }
.kicker{ font-family:"IBM Plex Mono", monospace; font-size:0.7rem; letter-spacing:0.12em; text-transform:uppercase; color:var(--text-faint); margin-bottom:8px; }
h1{ font-family:"Fraunces", serif; font-weight:500; font-size:1.7rem; margin-bottom:4px; }
.disclaimer{ font-size:0.8rem; color:var(--text-muted); margin-bottom:28px; }
.card{ background:var(--card); border-radius:14px; padding:20px 22px; margin-bottom:18px; }
.bankroll-value{ font-family:"IBM Plex Mono", monospace; font-size:2.4rem; font-weight:500; margin:4px 0 2px; }
.bankroll-delta{ font-size:0.85rem; font-weight:500; }
.bankroll-delta.pos{ color:#3E7A52; }
.bankroll-delta.neg{ color:#A3453A; }
.chart-wrap{ margin-top:14px; }
.empty{ font-size:0.82rem; color:var(--text-faint); }
h2{ font-size:0.92rem; font-weight:500; margin-bottom:12px; }
.sub{ font-size:0.78rem; color:var(--text-muted); margin-bottom:14px; }
.card-head{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:12px; }
.card-head h2{ margin-bottom:0; }
a.link{ font-size:0.78rem; color:var(--text-muted); }
.bet-row{ padding:10px 0; border-bottom:1px solid var(--rule); }
.bet-row:last-child{ border-bottom:none; }
.bet-match{ font-size:0.92rem; font-weight:500; margin-bottom:4px; }
.bet-league{ font-size:0.7rem; color:var(--text-faint); font-weight:400; }
.bet-meta{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; font-size:0.78rem; color:var(--text-muted); }
.leg{ display:flex; justify-content:space-between; align-items:center; gap:10px; font-size:0.82rem; padding:4px 0; }
.leg-match{ color:var(--text-muted); }
.team-logo{ width:14px; height:14px; vertical-align:-2px; margin-right:3px; object-fit:contain; }
.pill{ padding:2px 8px; border-radius:999px; font-size:0.72rem; font-weight:500; }
.pill-won{ background:var(--pill-won-bg); color:var(--pill-won-text); }
.pill-lost{ background:var(--pill-lost-bg); color:var(--pill-lost-text); }
.pill-pending{ background:var(--pill-pending-bg); color:var(--pill-pending-text); }
.list{ list-style:none; font-size:0.82rem; }
.list li{ padding:6px 0; border-bottom:1px solid var(--rule); display:flex; gap:10px; }
.list li:last-child{ border-bottom:none; }
.list li .mono{ color:var(--text-faint); flex-shrink:0; }
footer{ margin-top:24px; font-size:0.76rem; color:var(--text-faint); text-align:center; }
"""

_PAGE_HEAD = """<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='40' fill='%238558d3'/></svg>">
<link rel="apple-touch-icon" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAABuElEQVR4nO3SMQHAIADAMEDlDKEQQzOw3uxIFPTo3M8Z8GXdDuC/zEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB8kcJHOQzEEyB+kFDScDGHOGd5AAAAAASUVORK5CYII=">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;1,9..144,500&family=IBM+Plex+Sans:wght@400;500&family=IBM+Plex+Mono:wght@400;500&display=swap">"""


def _ticket_end_date(t: dict) -> str:
    """Date de fin d'un ticket = coup d'envoi de sa DERNIÈRE jambe (le
    ticket ne se règle qu'une fois tous les matchs joués) — chaîne
    ISO8601, donc triable directement sans parsing (ordre alphabétique
    = ordre chronologique)."""
    times = [leg.get("commence_time") for leg in (t.get("legs") or []) if leg.get("commence_time")]
    return max(times) if times else ""


def _render_status_page(
    bankroll, pending_tickets, settled_tickets, journal, errors, model_row,
) -> str:
    label = os.environ.get("BOT_LABEL") or "bot de paris football virtuels"
    metrics = (model_row or {}).get("backtest_metrics") or {}
    trained_at = (model_row or {}).get("trained_at") or None

    chart_points = []
    for t in settled_tickets:
        dt = _parse_iso(t.get("settled_at"))
        if dt is not None and t.get("bankroll_after") is not None:
            chart_points.append((dt.astimezone(_LOCAL_TZ), float(t["bankroll_after"])))
    chart_svg = _build_bankroll_chart_svg(chart_points)

    pnl_since_start = bankroll - config.INITIAL_BANKROLL
    roi_live_pct = pnl_since_start / config.INITIAL_BANKROLL * 100

    total_stake_pending = sum(float(t.get("stake") or 0) for t in pending_tickets)
    total_potential_gain_pending = sum(
        float(t.get("stake") or 0) * (float(t.get("odds") or 0) - 1) for t in pending_tickets
    )

    # Paris en cours triés par date de fin croissante (le plus proche
    # d'abord) + seulement les 3 derniers réglés — l'historique complet est
    # sur /historique pour ne pas noyer les paris en cours sous des
    # dizaines de tickets déjà réglés.
    # Uniquement les paris en cours ici, triés par date de fin croissante —
    # tout pari réglé (gagné ou perdu) va sur /historique, jamais mélangé
    # avec les paris encore actifs.
    pending_for_display = sorted(pending_tickets, key=_ticket_end_date)

    bet_cards = "".join(_ticket_card(t) for t in pending_for_display) or '<p class="empty">Aucun pari en cours pour l\'instant.</p>'

    is_preview = lambda j: (j.get("data") or {}).get("event") == "match_preview"
    main_journal = [j for j in journal if not is_preview(j)][:12]
    preview_journal = [j for j in journal if is_preview(j)][:10]

    journal_items = "".join(
        f'<li><span class="mono">{_fmt_local(j.get("ts"))}</span> {_esc(j.get("message"))}</li>'
        for j in main_journal
    ) or '<li class="empty">Rien pour l\'instant.</li>'

    preview_items = "".join(
        f'<li><span class="mono">{_fmt_local(j.get("ts"))}</span> {_esc(j.get("message"))}</li>'
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
        errors_items = "".join(f'<li><span class="mono">{_fmt_local(e.get("ts"))}</span> {_esc(e.get("message"))}</li>' for e in errors)
        errors_block = f'<div class="card"><h2>Erreurs récentes</h2><ul class="list">{errors_items}</ul></div>'

    chart_block = (
        f'<div class="chart-wrap">{chart_svg}</div>'
        if chart_svg else
        '<p class="empty">La courbe apparaîtra dès le premier pari réglé.</p>'
    )

    trained_dt = _parse_iso(trained_at) if trained_at else None
    trained_line = (
        f'Entraîné le {_esc(trained_dt.astimezone(_LOCAL_TZ).strftime("%d/%m %H:%M %Z"))} — backtest : '
        f'ROI {_esc(metrics.get("roi_pct"))}% · yield {_esc(metrics.get("yield_pct"))}% · '
        f'{_esc(metrics.get("num_bets"))} tickets · win rate {_esc(metrics.get("win_rate"))}% · '
        f'drawdown max {_esc(metrics.get("max_drawdown_pct"))}% '
        f'<span class="sub">(backtest 1X2 uniquement, pas de cotes over/under historiques — voir README)</span>'
        if trained_dt else
        "Pas encore de modèle entraîné — voir DEPLOIEMENT.md."
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(label)}</title>
{_PAGE_HEAD}
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="wrap">
  <p class="kicker">Paris virtuels</p>
  <h1>{_esc(label)}</h1>
  <p class="disclaimer">100% éducatif — aucun argent réel n'est en jeu, aucun pari réel n'est placé.</p>

  <div class="card">
    <div class="kicker">Bankroll</div>
    <div class="bankroll-value">{bankroll:,.2f}</div>
    <div class="bankroll-delta {'pos' if pnl_since_start >= 0 else 'neg'}">
      {'+' if pnl_since_start >= 0 else ''}{pnl_since_start:,.2f} depuis le départ ({roi_live_pct:+.1f}%)
    </div>
    <p class="sub">Mise totale en cours : {total_stake_pending:,.2f}€ ·
      gain potentiel : +{total_potential_gain_pending:,.2f}€ ({len(pending_tickets)} pari(s))</p>
    {chart_block}
  </div>

  <div class="card">
    <h2>Dernier modèle entraîné</h2>
    <p class="sub">{trained_line}</p>
  </div>

  <div class="card">
    <div class="card-head"><h2>Paris en cours</h2><a class="link" href="/historique">Historique complet →</a></div>
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


def _render_history_page(settled_tickets: list) -> str:
    """Historique complet des tickets réglés (won/lost) — la page de statut
    n'en garde que les 3 derniers pour ne pas noyer les paris en cours."""
    label = os.environ.get("BOT_LABEL") or "bot de paris football virtuels"
    cards = "".join(_ticket_card(t) for t in settled_tickets) or '<p class="empty">Aucun pari réglé pour l\'instant.</p>'

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Historique — {_esc(label)}</title>
{_PAGE_HEAD}
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="wrap">
  <p class="kicker">Paris virtuels</p>
  <div class="card-head"><h1>Historique</h1><a class="link" href="/">← Retour</a></div>
  <p class="disclaimer">Tous les tickets réglés (gagnés et perdus), les plus récents d'abord.</p>

  <div class="card">
    {cards}
  </div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
