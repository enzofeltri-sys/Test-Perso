"""
mean_reversion.py
------------------
Stratégie de retournement vers la moyenne (mean-reversion), pensée pour
les marchés SANS tendance claire (régime "ranging" détecté par l'ADX,
voir regime.py). Là où le croisement de moyennes se fait piéger par les
oscillations sans direction, cette stratégie en profite : elle achète
quand le prix s'écarte anormalement de sa moyenne récente (en supposant
qu'il va y revenir), et revend quand il y revient.

Logique (long-only, comme le reste du projet) :
  - ACHAT  quand le prix touche/casse la bande de Bollinger basse ET que
           le RSI confirme une zone de survente (pas juste "un peu bas").
  - VENTE  quand le prix revient à la moyenne mobile centrale, ou que le
           RSI repasse en zone neutre/haute, ou stop-loss / take-profit
           (basés sur l'ATR, comme pour la stratégie de tendance).

Comme toute stratégie de retournement, son point faible est une vraie
tendance forte et durable : le prix peut "casser" la bande basse et
continuer à baisser longtemps sans revenir à la moyenne. C'est
exactement pour ça qu'elle n'est utilisée qu'en régime "ranging" dans
la stratégie hybride (RegimeSwitchingStrategy).
"""

from dataclasses import dataclass

import pandas as pd

import indicators as ind


@dataclass
class MeanReversionParams:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_exit: float = 55.0
    atr_period: int = 14
    atr_multiplier_stop: float = 1.5
    reward_risk_ratio: float = 1.5


class MeanReversionStrategy:
    def __init__(self, params: MeanReversionParams):
        self.p = params

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p = self.p

        bb_mid = ind.sma(df["close"], p.bb_period)
        bb_std = df["close"].rolling(window=p.bb_period, min_periods=p.bb_period).std()

        df["bb_mid"] = bb_mid
        df["bb_upper"] = bb_mid + p.bb_std * bb_std
        df["bb_lower"] = bb_mid - p.bb_std * bb_std
        df["rsi_mr"] = ind.rsi(df["close"], p.rsi_period)
        df["atr_mr"] = ind.atr(df, p.atr_period)

        return df

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.compute_indicators(df)

    def should_enter(self, row) -> bool:
        return (row["close"] <= row["bb_lower"]) and (row["rsi_mr"] < self.p.rsi_oversold)

    def should_exit_on_signal(self, row) -> bool:
        return (row["close"] >= row["bb_mid"]) or (row["rsi_mr"] > self.p.rsi_exit)

    def on_position_closed(self):
        pass

    def compute_stop_and_target(self, entry_price: float, row):
        p = self.p
        stop_distance = p.atr_multiplier_stop * row["atr_mr"]
        stop_price = entry_price - stop_distance
        target_price = entry_price + p.reward_risk_ratio * stop_distance
        return stop_price, target_price, stop_distance


def mean_reversion_from_config(cfg: dict) -> MeanReversionStrategy:
    params = MeanReversionParams(
        bb_period=cfg["bb_period"],
        bb_std=cfg["bb_std"],
        rsi_period=cfg["rsi_period"],
        rsi_oversold=cfg["rsi_oversold"],
        rsi_exit=cfg["rsi_exit"],
        atr_period=cfg["atr_period"],
        atr_multiplier_stop=cfg["atr_multiplier_stop"],
        reward_risk_ratio=cfg["reward_risk_ratio"],
    )
    return MeanReversionStrategy(params)
