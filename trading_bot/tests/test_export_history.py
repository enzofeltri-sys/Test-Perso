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
