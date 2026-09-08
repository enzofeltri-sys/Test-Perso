import pandas as pd
import pytest

from risk import position_size, DailyLossCircuitBreaker, TotalDrawdownCircuitBreaker


def test_position_size_respects_risk_budget():
    # risque 1% de 10_000 = 100, stop à 5 de distance -> 20 unités, largement
    # dans les moyens (cash disponible) -> le sizing par le risque doit gagner
    qty = position_size(equity=10_000, risk_per_trade_pct=0.01, entry_price=50,
                         stop_distance=5, available_cash=100_000)
    assert qty == pytest.approx(20.0)


def test_position_size_capped_by_available_cash():
    qty = position_size(equity=10_000, risk_per_trade_pct=0.5, entry_price=50,
                         stop_distance=1, available_cash=100)
    # sizing par le risque voudrait 5000/1 = 5000 unités, impossible avec 100 de cash
    assert qty == pytest.approx(100 / 50)


def test_position_size_zero_on_invalid_stop_distance():
    assert position_size(10_000, 0.01, 50, stop_distance=0, available_cash=1000) == 0.0
    assert position_size(10_000, 0.01, 50, stop_distance=-1, available_cash=1000) == 0.0


def test_daily_loss_breaker_trips_and_resets_next_day():
    breaker = DailyLossCircuitBreaker(max_daily_loss_pct=0.03)
    day1 = pd.Timestamp("2023-01-01 00:00:00")

    breaker.update(day1, equity=1000)
    assert breaker.can_open_new_position()

    breaker.update(day1 + pd.Timedelta(hours=1), equity=960)  # -4% sur la journée
    assert not breaker.can_open_new_position()

    day2 = pd.Timestamp("2023-01-02 00:00:00")
    breaker.update(day2, equity=960)
    assert breaker.can_open_new_position()  # nouvelle journée -> réinitialisé


def test_total_drawdown_breaker_trips_and_stays_tripped():
    breaker = TotalDrawdownCircuitBreaker(max_total_drawdown_pct=0.20)

    breaker.update(1000)
    breaker.update(1200)  # nouveau plus haut
    assert breaker.can_open_new_position()

    breaker.update(900)  # -25% depuis le plus haut (1200) -> déclenché
    assert not breaker.can_open_new_position()

    breaker.update(1300)  # même si l'équity remonte au-dessus de l'ancien plus haut...
    assert not breaker.can_open_new_position()  # ...reste déclenché, par design
