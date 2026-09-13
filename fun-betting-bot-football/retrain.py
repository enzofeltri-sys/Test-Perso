"""
retrain.py
----------
Ré-entraîne le modèle de buts (Poisson) sur l'historique football-data.co.uk
le plus récent, backteste la stratégie EV/Kelly/combos sur la saison de
test, et publie le modèle + le rapport de backtest dans Supabase
(footballbot_model) pour que web_app.py (Render) puisse s'en servir au
prochain /tick.

Fait pour tourner comme workflow GitHub Actions programmé (voir
.github/workflows/retrain.yml) — pas sur Render, dont le Cron Job exige un
plan payant (même raisonnement que trading_bot/recalibrate.py).

Usage :
    python retrain.py             # entraîne et publie dans Supabase
    python retrain.py --dry-run   # entraîne et affiche le rapport, n'écrit rien
"""

import argparse
import sys
from pathlib import Path

from src import config, data_loader, features, metrics, model as model_module, simulation, supabase_state


def run(dry_run: bool = False) -> dict:
    print("Téléchargement de l'historique football-data.co.uk...")
    df = data_loader.download_historical_data()
    print(f"{len(df)} matchs téléchargés ({df['Season'].min()}–{df['Season'].max()}).")

    if not dry_run:
        sent = supabase_state.upsert_matches(df)
        print(f"{sent} lignes envoyées à Supabase (table des matchs).")

    X, y_home, y_away = features.build_features(df)
    meta = df.loc[X.index]
    train_mask = meta["Season"] < config.TEST_SEASON_START
    X_train, yh_train, ya_train = X[train_mask], y_home[train_mask], y_away[train_mask]
    X_test, yh_test, ya_test = X[~train_mask], y_home[~train_mask], y_away[~train_mask]

    if X_train.empty or X_test.empty:
        raise RuntimeError(
            f"Split train/test vide (train={len(X_train)}, test={len(X_test)}). "
            f"Vérifie config.TEST_SEASON_START ({config.TEST_SEASON_START}) et l'historique téléchargé."
        )

    print(f"Entraînement sur {len(X_train)} matchs, test sur {len(X_test)} matchs.")
    trained_model = model_module.train_model(X_train, yh_train, ya_train)

    bet_log = simulation.run_backtest(df, X_test, yh_test, ya_test, trained_model)
    backtest_metrics = metrics.compute_metrics(bet_log)
    print("Backtest :", backtest_metrics)

    plots_dir = Path(__file__).resolve().parent / "notebooks" / "plots"
    metrics.save_plots(bet_log, plots_dir)
    if not bet_log.empty:
        print(f"Graphiques sauvegardés dans {plots_dir}")

    if dry_run:
        print("--dry-run : rien n'est écrit dans Supabase.")
        return {"applied": False, "backtest_metrics": backtest_metrics}

    model_b64 = model_module.serialize_model(trained_model)
    supabase_state.save_model(
        model_b64=model_b64,
        feature_columns=features.FEATURE_COLUMNS,
        train_matches=len(X_train),
        test_matches=len(X_test),
        backtest_metrics=backtest_metrics,
    )
    supabase_state.log_journal_entry(
        "bot",
        f"Ré-entraînement : {len(X_train)} matchs (train), {len(X_test)} matchs (test, saison "
        f"{config.TEST_SEASON_START}+). Backtest — ROI {backtest_metrics['roi_pct']}%, "
        f"yield {backtest_metrics['yield_pct']}%, {backtest_metrics['num_bets']} tickets (seuls+combinés), "
        f"win rate {backtest_metrics['win_rate']}%, drawdown max {backtest_metrics['max_drawdown_pct']}%.",
    )
    print("Modèle publié dans Supabase.")

    return {"applied": True, "backtest_metrics": backtest_metrics}


def main():
    parser = argparse.ArgumentParser(description="Ré-entraînement hebdomadaire du modèle de buts (Poisson)")
    parser.add_argument("--dry-run", action="store_true", help="calcule et affiche, n'écrit rien dans Supabase")
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run)
    except Exception as exc:
        if not args.dry_run:
            try:
                supabase_state.log_error(f"retrain.py : {exc}")
                supabase_state.log_journal_entry(
                    "bot", f"Ré-entraînement en échec ({exc}) — le modèle précédent reste en place.",
                )
            except Exception:
                pass
        print(f"Erreur : {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
