import numpy as np
import pandas as pd
import pytest


def make_ohlcv(seed: int = 0, n: int = 500, start: float = 100.0, freq: str = "h") -> pd.DataFrame:
    """Bougies OHLCV synthétiques (bruit + léger cycle) — pas de vraies données
    de marché, juste de quoi exercer le code de bout en bout de façon déterministe."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.01, n) + 0.0005 * np.sin(np.arange(n) / 50)
    close = start * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    openp = np.roll(close, 1)
    openp[0] = start
    volume = rng.uniform(100, 1000, n)
    index = pd.date_range("2023-01-01", periods=n, freq=freq)
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


@pytest.fixture
def ohlcv():
    return make_ohlcv()


@pytest.fixture
def portfolio_data():
    return {
        "BTC/USDT": make_ohlcv(seed=0, n=1500),
        "ETH/USDT": make_ohlcv(seed=1, n=1500),
        "SOL/USDT": make_ohlcv(seed=2, n=1500),
    }
