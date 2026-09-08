"""
export_history.py
-----------------
Exporte l'historique OHLCV réel de chaque paire du portefeuille en CSV
(un fichier par paire), avec exactement la même source et la même
profondeur que `main.py validate` / `recalibrate.py`
(`exchange.history_exchange_id`, `backtest.since_days`).

À quoi ça sert : pouvoir rejouer le walk-forward et tester des variantes
de stratégie LOCALEMENT, sur les vraies données, sans retélécharger à
chaque essai — et sans dépendre d'un accès réseau aux exchanges depuis
la machine d'expérimentation. Voir `load_history()` pour relire les
fichiers dans le format attendu par PortfolioBacktester.

    python export_history.py --out history/
    python -c "from export_history import load_history; print(load_history('history/').keys())"
"""

import argparse
import os

import pandas as pd
import yaml

from main import _fetch_portfolio_history


def _filename(symbol: str) -> str:
    return symbol.replace("/", "-") + ".csv"


def export_history(cfg: dict, out_dir: str) -> dict:
    market_data = _fetch_portfolio_history(cfg)
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for symbol, df in market_data.items():
        path = os.path.join(out_dir, _filename(symbol))
        df.to_csv(path, index_label="timestamp")
        paths[symbol] = path
    return paths


def load_history(in_dir: str, symbols: list = None) -> dict:
    """{symbol: DataFrame OHLCV indexé par timestamp} — même format que
    `_fetch_portfolio_history`, prêt pour PortfolioBacktester / walk-forward."""
    if symbols is None:
        symbols = [f[:-4].replace("-", "/") for f in sorted(os.listdir(in_dir)) if f.endswith(".csv")]
    data = {}
    for symbol in symbols:
        df = pd.read_csv(os.path.join(in_dir, _filename(symbol)), index_col="timestamp", parse_dates=True)
        data[symbol] = df
    return data


def main():
    parser = argparse.ArgumentParser(description="Exporte l'historique OHLCV du portefeuille en CSV")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--out", default="history")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    paths = export_history(cfg, args.out)
    for symbol, path in paths.items():
        print(f"{symbol} -> {path}")


if __name__ == "__main__":
    main()
