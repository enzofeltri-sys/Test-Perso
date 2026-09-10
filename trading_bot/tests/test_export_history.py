import pandas as pd

import export_history


def test_export_then_load_round_trips(monkeypatch, portfolio_data, tmp_path):
    monkeypatch.setattr(export_history, "_fetch_portfolio_history", lambda cfg: portfolio_data)

    paths = export_history.export_history({}, str(tmp_path))
    assert set(paths) == set(portfolio_data)
    assert all(p.endswith(".csv") and "/" not in p.split("/")[-1].replace(".csv", "") for p in paths.values())

    loaded = export_history.load_history(str(tmp_path))
    assert set(loaded) == set(portfolio_data)
    for symbol, df in portfolio_data.items():
        got = loaded[symbol]
        assert isinstance(got.index, pd.DatetimeIndex)
        assert list(got.columns) == list(df.columns)
        pd.testing.assert_frame_equal(got, df, check_freq=False, check_names=False)


def test_symbols_override_never_touches_config(monkeypatch, tmp_path):
    """Le portefeuille candidat écrase cfg['portfolio']['symbols'] pour CET
    export uniquement — jamais sur le cfg d'origine, encore moins sur disque."""
    seen_cfgs = []

    def _fake_fetch(cfg):
        seen_cfgs.append(cfg)
        return {s: pd.DataFrame({"close": [1.0, 2.0]}) for s in cfg["portfolio"]["symbols"]}

    monkeypatch.setattr(export_history, "_fetch_portfolio_history", _fake_fetch)
    original_cfg = {"portfolio": {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]}}

    paths = export_history.export_history(original_cfg, str(tmp_path), symbols=["BNB/USDT", "XRP/USDT"])

    assert set(paths) == {"BNB/USDT", "XRP/USDT"}
    assert seen_cfgs[0]["portfolio"]["symbols"] == ["BNB/USDT", "XRP/USDT"]
    # le cfg appelant, lui, n'a jamais bougé
    assert original_cfg["portfolio"]["symbols"] == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


def test_symbols_override_absent_falls_back_to_config(monkeypatch, tmp_path):
    seen_cfgs = []

    def _fake_fetch(cfg):
        seen_cfgs.append(cfg)
        return {s: pd.DataFrame({"close": [1.0, 2.0]}) for s in cfg["portfolio"]["symbols"]}

    monkeypatch.setattr(export_history, "_fetch_portfolio_history", _fake_fetch)
    cfg = {"portfolio": {"symbols": ["BTC/USDT"]}}

    export_history.export_history(cfg, str(tmp_path))

    assert seen_cfgs[0]["portfolio"]["symbols"] == ["BTC/USDT"]
