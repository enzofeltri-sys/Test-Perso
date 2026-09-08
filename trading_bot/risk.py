"""
risk.py
-------
Gestion du risque, séparée de la logique de signal. Deux idées :

1. Dimensionnement par le risque ("position sizing") : au lieu d'investir
   un pourcentage fixe du capital à chaque trade, on calcule la taille de
   position pour que, SI le stop-loss est touché, la perte corresponde à
   un pourcentage défini du capital (ex: 1%) — jamais plus, quelle que
   soit la volatilité du moment. Un stop plus large (marché volatil)
   donne donc mécaniquement une position plus petite.

2. Coupe-circuit de perte journalière : si le bot perd plus qu'un certain
   pourcentage du capital sur une journée, il arrête de prendre de
   nouvelles positions jusqu'au lendemain. Ça protège contre une série de
   trades perdants pendant une journée de marché inhabituelle (ce n'est
   pas une garantie absolue contre les pertes, juste un frein).

3. Coupe-circuit de drawdown TOTAL : contrairement au précédent qui se
   réinitialise chaque jour, celui-ci regarde la baisse depuis le plus
   haut historique du capital, sur toute la durée. S'il se déclenche, il
   reste déclenché — l'idée est qu'une perte de cette ampleur (ex: -15%
   depuis le sommet) est un signal qu'il faut arrêter et RÉÉVALUER la
   stratégie, pas continuer à espérer que ça reparte tout seul.
"""

from dataclasses import dataclass
from datetime import datetime


def position_size(equity: float, risk_per_trade_pct: float, entry_price: float,
                   stop_distance: float, available_cash: float,
                   min_amount: float = None, min_cost: float = None) -> float:
    """
    Retourne la quantité (en unités de l'actif, ex: BTC) à acheter.

    - equity: capital total actuel (cash + valeur de la position)
    - risk_per_trade_pct: fraction du capital qu'on accepte de perdre si
      le stop est touché (ex: 0.01 pour 1%)
    - stop_distance: écart en prix entre le prix d'entrée et le stop-loss
    - available_cash: cash réellement disponible (on ne peut pas dépenser
      plus que ça, même si le sizing par le risque le suggérait)
    - min_amount / min_cost: quantité et/ou valeur notionnelle minimum
      qu'un ordre doit atteindre sur cet exchange (voir
      data.get_min_order_limits). Si la position calculée par le risque
      tombe EN DESSOUS de ce plancher, aucun exchange réel n'accepterait
      l'ordre — on retourne 0 (le trade est refusé) plutôt que
      d'arrondir vers le haut, ce qui reviendrait à risquer plus que
      `risk_per_trade_pct` sans le décider explicitement.
    """
    if stop_distance <= 0 or entry_price <= 0:
        return 0.0

    risk_amount = equity * risk_per_trade_pct
    qty_by_risk = risk_amount / stop_distance

    max_qty_by_cash = available_cash / entry_price

    qty = max(0.0, min(qty_by_risk, max_qty_by_cash))
    if qty <= 0:
        return 0.0

    effective_min_cost = max(min_cost or 0.0, (min_amount or 0.0) * entry_price)
    if effective_min_cost > 0 and qty * entry_price < effective_min_cost:
        return 0.0

    return qty


@dataclass
class DailyLossCircuitBreaker:
    max_daily_loss_pct: float
    _current_day: object = None
    _equity_at_day_start: float = None
    _tripped_today: bool = False

    def update(self, timestamp: datetime, equity: float):
        day = timestamp.date() if hasattr(timestamp, "date") else timestamp
        if day != self._current_day:
            self._current_day = day
            self._equity_at_day_start = equity
            self._tripped_today = False

        if self._equity_at_day_start:
            loss_pct = (equity - self._equity_at_day_start) / self._equity_at_day_start
            if loss_pct <= -abs(self.max_daily_loss_pct):
                self._tripped_today = True

    def can_open_new_position(self) -> bool:
        return not self._tripped_today


@dataclass
class TotalDrawdownCircuitBreaker:
    max_total_drawdown_pct: float
    _peak_equity: float = None
    _tripped: bool = False

    def update(self, equity: float):
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity

        if self._tripped:
            return  # une fois déclenché, reste déclenché (voir docstring du module)

        if self._peak_equity:
            drawdown_pct = (equity - self._peak_equity) / self._peak_equity
            if drawdown_pct <= -abs(self.max_total_drawdown_pct):
                self._tripped = True

    def can_open_new_position(self) -> bool:
        return not self._tripped

    @property
    def tripped(self) -> bool:
        return self._tripped
