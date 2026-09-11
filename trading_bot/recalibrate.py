"""
recalibrate.py
---------------
Recalibrage périodique (mensuel) des paramètres de STRATÉGIE, à partir
d'une vraie validation walk-forward sur l'historique réel — jamais sur
la confiance d'un seul backtest.

Ce script :
  1. télécharge l'historique réel de chaque paire du portefeuille (comme
     `python main.py validate`),
  2. relance le walk-forward (`walk_forward_analysis`) avec le
     `param_grid` de `config.yaml`, sur le portefeuille complet (mêmes
     garde-fous que le backtest et le paper trading réel),
  3. si — et SEULEMENT si — les fenêtres hors-échantillon RÉCENTES sont
     robustes (rendement composé positif sur PLUSIEURS fenêtres
     consécutives, pas une seule fenêtre isolée qui aurait pu gagner par
     hasard), écrit les paramètres de la fenêtre la plus récente dans
     Supabase (`tradingbot_config.strategy_overrides`) — voir
     `_apply_overrides` dans walk_forward.py pour le format (clés
     pointées, ex: "trend.min_score_to_enter").

Ce que ce script NE fait JAMAIS :
  - toucher aux paramètres de RISQUE (risk_per_trade_pct, coupe-circuits,
    active_symbols...) — ceux-là restent sous contrôle humain exclusif,
    ajustables uniquement à la main via tradingbot_config (voir
    DEPLOIEMENT.md).
  - placer un ordre, réel ou simulé.
  - écrire quoi que ce soit si les critères de robustesse ne sont pas
    remplis : le bot continue simplement avec les derniers paramètres
    déjà en place (ceux de config.yaml, ou un recalibrage précédent).

Prévu pour tourner comme Cron Job Render, une fois par mois (voir
render.yaml et DEPLOIEMENT.md) — mais se lance aussi à la main :

    python recalibrate.py --config config.yaml
    python recalibrate.py --dry-run     # calcule et affiche, n'écrit jamais
"""

import argparse
import sys
from datetime import datetime, timezone

import yaml

import alerts
import data
import supabase_state as db
from main import _fetch_portfolio_history
from walk_forward import (walk_forward_analysis, aggregate_walk_forward, _apply_overrides,
                          summarize_windows, print_windows)
from strategy import regime_strategy_from_config

# Nombre de fenêtres hors-échantillon les plus RÉCENTES examinées pour la
# décision de robustesse — délibérément > 1 pour ne jamais agir sur une
# seule fenêtre qui aurait pu être gagnante par hasard, et délibérément
# petit : un edge validé sur des données d'il y a des années ne dit rien
# sur si les conditions de marché actuelles lui sont encore favorables.
RECENT_WINDOWS = 3
MIN_POSITIVE_WINDOW_FRACTION = 0.5  # majorité des fenêtres récentes doit être individuellement positive


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def evaluate_robustness(windows: list, recent_n: int = RECENT_WINDOWS) -> dict:
    """Ne regarde QUE les `recent_n` dernières fenêtres hors-échantillon
    (les plus proches du présent), pas l'agrégat sur tout l'historique —
    voir docstring du module."""
    if len(windows) < recent_n:
        return {
            "robust": False,
            "reason": f"seulement {len(windows)} fenêtre(s) hors-échantillon disponible(s), {recent_n} requises",
        }

    recent = windows[-recent_n:]
    agg = aggregate_walk_forward(recent)
    positive_fraction = agg["pct_windows_positive"] / 100.0
    robust = agg["compounded_oos_return_pct"] > 0 and positive_fraction >= MIN_POSITIVE_WINDOW_FRACTION

    if robust:
        reason = None
    elif agg["compounded_oos_return_pct"] <= 0:
        reason = f"rendement OOS composé récent = {agg['compounded_oos_return_pct']:.2f}% (pas positif)"
    else:
        reason = (f"seulement {agg['pct_windows_positive']:.0f}% des {recent_n} fenêtres récentes "
                   f"individuellement positives (minimum {MIN_POSITIVE_WINDOW_FRACTION * 100:.0f}%)")

    return {
        "robust": robust,
        "reason": reason,
        "recent_windows": recent,
        "compounded_oos_return_pct": agg["compounded_oos_return_pct"],
        "pct_windows_positive": agg["pct_windows_positive"],
    }


def run(cfg: dict, dry_run: bool = False) -> dict:
    pf_cfg = cfg["portfolio"]
    bt_cfg = cfg["backtest"]
    risk_cfg = cfg["risk"]
    wf_cfg = cfg["walk_forward"]

    print(f"[{datetime.now(timezone.utc).isoformat()}] Recalibrage — téléchargement de l'historique réel...")
    market_data = _fetch_portfolio_history(cfg)

    try:
        exchange = data.get_exchange(cfg["exchange"]["id"])
        min_order_limits = data.get_min_order_limits(exchange, pf_cfg["symbols"])
    except Exception:
        min_order_limits = {}

    # Mêmes garde-fous portefeuille que le backtest / le paper trading réel
    # (voir walk_forward.py) — sinon on validerait une stratégie différente
    # de celle qui tourne réellement.
    portfolio_kwargs = dict(
        initial_balance=bt_cfg["initial_balance"],
        fee_pct=bt_cfg["fee_pct"],
        risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
        max_daily_loss_pct=risk_cfg.get("max_daily_loss_pct"),
        slippage_pct=bt_cfg.get("slippage_pct", 0.0),
        max_concurrent_positions=pf_cfg.get("max_concurrent_positions"),
        max_correlation_for_new_position=risk_cfg.get("max_correlation_for_new_position"),
        correlation_lookback=risk_cfg.get("correlation_lookback", 30),
        momentum_lookback=pf_cfg.get("momentum_lookback", 20),
        max_total_drawdown_pct=risk_cfg.get("max_total_drawdown_pct"),
        max_position_pct_of_equity=risk_cfg.get("max_position_pct_of_equity"),
        max_position_notional_usd=risk_cfg.get("max_position_notional_usd"),
        min_order_limits=min_order_limits,
    )

    print("Walk-forward en cours (peut prendre plusieurs minutes selon le param_grid)...")
    windows = walk_forward_analysis(
        market_data, cfg["strategy"], wf_cfg["param_grid"], portfolio_kwargs,
        train_days=wf_cfg["train_days"], test_days=wf_cfg["test_days"], step_days=wf_cfg["step_days"],
    )

    if not windows:
        reason = "pas assez d'historique pour au moins une fenêtre train/test complète"
        print(reason)
        if not dry_run:
            db.log_journal_entry(
                "bot",
                f"Recalibrage : {reason} — rien à évaluer, aucun changement.",
                data={"applied": False, "num_windows": 0},
            )
        return {"applied": False, "reason": reason}

    print(f"{len(windows)} fenêtre(s) hors-échantillon au total :")
    # détail fenêtre par fenêtre — c'est le diagnostic qui explique un
    # verdict, pas seulement le verdict (voir print_windows)
    print_windows(windows)
    agg_all = aggregate_walk_forward(windows)
    print(f"  Sur toutes les fenêtres : rendement composé {agg_all['compounded_oos_return_pct']:+.2f}% "
          f"(buy&hold {agg_all['compounded_buy_and_hold_pct']:+.2f}%), "
          f"taux de gain global {agg_all['overall_win_rate_pct']:.1f}%, "
          f"sorties {agg_all['exit_reasons']}")
    verdict = evaluate_robustness(windows)
    if verdict.get("compounded_oos_return_pct") is not None:
        print(f"  {len(verdict['recent_windows'])} fenêtres récentes examinées : "
              f"rendement composé = {verdict['compounded_oos_return_pct']:.2f}%  "
              f"| % positives = {verdict['pct_windows_positive']:.0f}%")

    # "recent_windows" contient des Timestamps pandas, pas sérialisables tels
    # quels en JSON — jamais inclus dans ce qui part vers le journal/Supabase.
    verdict_summary = {k: v for k, v in verdict.items() if k != "recent_windows"}
    # ...mais leur version compacte (dates ISO, nombres JSON-valides) part
    # dans le journal : le manager doit pouvoir lire POURQUOI le bot a
    # conclu ce qu'il a conclu, pas juste le verdict
    windows_summary = summarize_windows(windows)
    diagnostic = {
        "num_windows": len(windows),
        "windows": windows_summary,
        "all_windows_compounded_return_pct": round(agg_all["compounded_oos_return_pct"], 2),
        "all_windows_buy_and_hold_pct": round(agg_all["compounded_buy_and_hold_pct"], 2),
        "overall_win_rate_pct": round(agg_all["overall_win_rate_pct"], 1),
        "exit_reasons": agg_all["exit_reasons"],
    }

    if not verdict["robust"]:
        print(f"Pas assez robuste pour recalibrer : {verdict['reason']} — aucun changement écrit.")
        if not dry_run:
            db.log_journal_entry(
                "bot",
                f"Recalibrage : pas assez robuste pour agir ({verdict['reason']}). "
                f"{len(windows)} fenêtres walk-forward au total, "
                f"{len(verdict['recent_windows'])} récentes examinées. Aucun changement — "
                "le bot continue avec les derniers paramètres en place.",
                data={"applied": False, **diagnostic, **verdict_summary},
            )
        return {"applied": False, "reason": verdict["reason"], "windows": windows_summary}

    candidate_params = windows[-1]["chosen_params"]
    print(f"Robuste — nouveaux paramètres candidats (fenêtre la plus récente) : {candidate_params}")

    # sanity check : les paramètres candidats doivent au moins réussir à
    # construire une stratégie valide avant d'être écrits en base — sinon
    # on préfère planter ici (rien n'est écrit) plutôt que d'écrire des
    # réglages qui feraient planter run_tick() le mois suivant
    strat_cfg = _apply_overrides(cfg["strategy"], candidate_params)
    regime_strategy_from_config(strat_cfg)

    if dry_run:
        print("--dry-run : rien n'est écrit dans Supabase.")
        return {**verdict, "applied": False, "reason": "dry-run", "candidate_params": candidate_params,
                "windows": windows_summary}

    note = (f"recalibrage auto — rendement OOS composé récent "
            f"{verdict['compounded_oos_return_pct']:.2f}% sur {len(verdict['recent_windows'])} fenêtres")
    db.save_strategy_overrides(candidate_params, note=note)
    print("Écrit dans tradingbot_config.strategy_overrides.")
    db.log_journal_entry(
        "bot",
        f"Recalibrage appliqué : nouveaux paramètres de stratégie {candidate_params}, validés sur "
        f"{len(verdict['recent_windows'])} fenêtres récentes robustes (rendement composé "
        f"{verdict['compounded_oos_return_pct']:.2f}%, {verdict['pct_windows_positive']:.0f}% positives). "
        "Écrit dans tradingbot_config.strategy_overrides.",
        data={"applied": True, "candidate_params": candidate_params, **diagnostic, **verdict_summary},
    )
    return {**verdict, "applied": True, "candidate_params": candidate_params, "windows": windows_summary}


def main():
    parser = argparse.ArgumentParser(
        description="Recalibrage mensuel des paramètres de stratégie (walk-forward sur historique réel)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="calcule et affiche, n'écrit jamais dans Supabase")
    args = parser.parse_args()

    cfg = load_config(args.config)

    try:
        run(cfg, dry_run=args.dry_run)
    except Exception as e:
        try:
            db.log_error(f"Erreur non gérée dans recalibrate.py : {e}")
        except Exception:
            pass
        if not args.dry_run:
            db.log_journal_entry(
                "bot",
                f"Recalibrage : échec inattendu ({e}) — voir tradingbot_errors pour la trace complète. "
                "Aucun changement appliqué.",
            )
        # le recalibrage ne tourne qu'une fois par mois : un échec silencieux
        # se découvre le mois suivant, voire jamais
        alerts.send(
            f"Le recalibrage mensuel a échoué : {e}. Aucun paramètre n'a été modifié, "
            f"le bot continue avec les réglages en place (trace dans tradingbot_errors).",
            alerts.WARNING,
        )
        print(f"ERREUR : {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
