"""
recalibrate.py ne doit JAMAIS écrire de nouveaux paramètres de stratégie
sans preuve de robustesse récente (voir docstring du module) — ces tests
couvrent la logique de décision (evaluate_robustness) séparément de
l'orchestration (run), et vérifient que run() n'écrit dans Supabase que
quand evaluate_robustness dit oui.
"""

import sys

import pytest
import yaml

import main as main_module
import recalibrate


def _window(test_return_pct, chosen_params=None):
    return {
        "train_start": None, "train_end": None, "test_start": None, "test_end": None,
        "chosen_params": chosen_params or {"trend.min_score_to_enter": 3},
        "test_return_pct": test_return_pct,
        "test_sharpe": 1.0,
        "test_max_drawdown_pct": -2.0,
        "test_num_trades": 10,
        "test_win_rate_pct": 55.0,
    }


def test_evaluate_robustness_needs_enough_recent_windows():
    verdict = recalibrate.evaluate_robustness([_window(5.0), _window(3.0)], recent_n=3)
    assert verdict["robust"] is False
    assert "2 fenêtre" in verdict["reason"]


def test_evaluate_robustness_true_when_all_recent_windows_positive():
    windows = [_window(5.0), _window(3.0), _window(2.0)]
    verdict = recalibrate.evaluate_robustness(windows, recent_n=3)
    assert verdict["robust"] is True
    assert verdict["pct_windows_positive"] == 100.0


def test_evaluate_robustness_false_when_composed_return_negative():
    windows = [_window(-10.0), _window(3.0), _window(2.0)]
    verdict = recalibrate.evaluate_robustness(windows, recent_n=3)
    assert verdict["robust"] is False
    assert "composé" in verdict["reason"]


def test_evaluate_robustness_false_when_single_isolated_window_carries_it():
    # une fenêtre très gagnante ne doit pas suffire si la majorité des
    # fenêtres récentes est perdante -- exactement le cas que le critère
    # "pas juste une fenêtre isolée" doit intercepter
    windows = [_window(50.0), _window(-5.0), _window(-5.0)]
    verdict = recalibrate.evaluate_robustness(windows, recent_n=3)
    assert verdict["pct_windows_positive"] < 50.0
    assert verdict["robust"] is False


def test_evaluate_robustness_only_looks_at_recent_windows():
    # de vieilles fenêtres très négatives ne doivent pas empêcher un
    # recalibrage si les fenêtres RÉCENTES sont robustes
    old_bad = [_window(-50.0)] * 5
    recent_good = [_window(4.0), _window(3.0), _window(2.0)]
    verdict = recalibrate.evaluate_robustness(old_bad + recent_good, recent_n=3)
    assert verdict["robust"] is True


def _patch_history_fetch(monkeypatch, portfolio_data):
    monkeypatch.setattr(main_module.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(main_module.data, "fetch_ohlcv_history",
                         lambda exchange, symbol, timeframe, since_days: portfolio_data[symbol])
    monkeypatch.setattr(recalibrate.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(recalibrate.data, "get_min_order_limits", lambda exchange, symbols: {})


class FakeDB:
    def __init__(self):
        self.saved = None
        self.errors = []
        self.journal = []

    def save_strategy_overrides(self, strategy_overrides, note=None):
        self.saved = {"strategy_overrides": strategy_overrides, "note": note}

    def log_error(self, message):
        self.errors.append(message)

    def log_journal_entry(self, author, message, data=None):
        self.journal.append({"author": author, "message": message, "data": data})


def test_run_writes_strategy_overrides_when_robust(monkeypatch, portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    _patch_history_fetch(monkeypatch, portfolio_data)
    robust_windows = [_window(4.0, {"trend.min_score_to_enter": 3}) for _ in range(3)]
    monkeypatch.setattr(recalibrate, "walk_forward_analysis", lambda *a, **kw: robust_windows)
    fake_db = FakeDB()
    monkeypatch.setattr(recalibrate, "db", fake_db)

    result = recalibrate.run(cfg, dry_run=False)

    assert result["applied"] is True
    assert fake_db.saved is not None
    assert fake_db.saved["strategy_overrides"] == {"trend.min_score_to_enter": 3}
    assert "recalibrage auto" in fake_db.saved["note"]
    assert len(fake_db.journal) == 1
    assert fake_db.journal[0]["author"] == "bot"
    assert "appliqué" in fake_db.journal[0]["message"]
    assert fake_db.journal[0]["data"]["applied"] is True


def test_run_writes_nothing_when_not_robust(monkeypatch, portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    _patch_history_fetch(monkeypatch, portfolio_data)
    losing_windows = [_window(-3.0) for _ in range(3)]
    monkeypatch.setattr(recalibrate, "walk_forward_analysis", lambda *a, **kw: losing_windows)
    fake_db = FakeDB()
    monkeypatch.setattr(recalibrate, "db", fake_db)

    result = recalibrate.run(cfg, dry_run=False)

    assert result["applied"] is False
    assert fake_db.saved is None
    assert len(fake_db.journal) == 1
    assert fake_db.journal[0]["author"] == "bot"
    assert "pas assez robuste" in fake_db.journal[0]["message"].lower()


def test_run_dry_run_never_writes_even_when_robust(monkeypatch, portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    _patch_history_fetch(monkeypatch, portfolio_data)
    robust_windows = [_window(4.0) for _ in range(3)]
    monkeypatch.setattr(recalibrate, "walk_forward_analysis", lambda *a, **kw: robust_windows)
    fake_db = FakeDB()
    monkeypatch.setattr(recalibrate, "db", fake_db)

    result = recalibrate.run(cfg, dry_run=True)

    assert result["applied"] is False
    assert result["reason"] == "dry-run"
    assert fake_db.saved is None
    assert fake_db.journal == [], "--dry-run ne doit rien écrire, y compris dans le journal"


def test_run_writes_nothing_when_no_windows_at_all(monkeypatch, portfolio_data):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    _patch_history_fetch(monkeypatch, portfolio_data)
    monkeypatch.setattr(recalibrate, "walk_forward_analysis", lambda *a, **kw: [])
    fake_db = FakeDB()
    monkeypatch.setattr(recalibrate, "db", fake_db)

    result = recalibrate.run(cfg, dry_run=False)

    assert result["applied"] is False
    assert fake_db.saved is None
    assert len(fake_db.journal) == 1
    assert fake_db.journal[0]["author"] == "bot"


def test_main_journals_unexpected_failures(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("exchange:\n  id: kucoin\n")  # minimal, jamais lu jusqu'au bout

    def _boom(cfg, dry_run=False):
        raise RuntimeError("panne réseau simulée")

    fake_db = FakeDB()
    monkeypatch.setattr(recalibrate, "run", _boom)
    monkeypatch.setattr(recalibrate, "db", fake_db)
    monkeypatch.setattr(sys, "argv", ["recalibrate.py", "--config", str(config_path)])

    with pytest.raises(SystemExit):
        recalibrate.main()

    assert len(fake_db.errors) == 1
    assert len(fake_db.journal) == 1
    assert fake_db.journal[0]["author"] == "bot"
    assert "échec" in fake_db.journal[0]["message"].lower()
