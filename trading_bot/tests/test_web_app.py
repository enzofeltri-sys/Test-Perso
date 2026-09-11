"""
Régression pour web_app.py — la version déployée sur Render (voir
DEPLOIEMENT.md). Ce module ne garde AUCUN état en mémoire entre deux
appels à /tick : chaque position ouverte doit mémoriser explicitement
quelle sous-stratégie (trend/range) l'a ouverte, dans
`positions[symbol]["active_substrategy"]`, pour gérer sa sortie
correctement au tick suivant (voir web_app.py).

Ces tests simulent plusieurs ticks successifs avec de fausses données de
marché et un faux backend Supabase (en mémoire, pas de réseau), et
vérifient qu'aucune position ouverte ne se retrouve avec
`active_substrategy` à None — ce qui ferait silencieusement retomber sur
la mauvaise sous-stratégie (voir la correction de strategy.py :
RegimeSwitchingStrategy.compute_stop_and_target() doit être appelé sur le
wrapper, pas sur une sous-stratégie choisie à la main AVANT confirmation).
"""

from datetime import datetime, timedelta, timezone

import pytest
import yaml

import web_app
from conftest import make_ohlcv


class FakeDB:
    def __init__(self, overrides=None):
        self.state = None
        self.trades = []
        self.errors = []
        self.journal = []
        self.overrides = overrides or {}
        # lignes renvoyées par get_recent_errors() — sert à simuler des
        # erreurs déjà horodatées en base (déduplication des alertes)
        self.errors_rows = []
        # quand fourni, court-circuite get_last_trade_ts_by_symbol() avec
        # des horodatages choisis par le test (cooldown de rachat) plutôt
        # que de les dériver de self.trades (horodatés en temps réel, donc
        # inutilisables pour tester "après le cooldown" de façon déterministe)
        self.last_trade_ts_override = None

    def load_state(self, initial_balance):
        if self.state is None:
            return {
                "cash": initial_balance, "positions": {},
                "daily_current_day": None, "daily_equity_at_day_start": None,
                "daily_tripped_today": False, "total_dd_peak_equity": None,
                "total_dd_tripped": False,
            }
        return self.state

    def save_state(self, state):
        self.state = dict(state)

    def log_trade(self, symbol, side, price, qty, reason, cash_after, equity_after):
        self.trades.append({
            "symbol": symbol, "side": side, "price": price, "qty": qty,
            "reason": reason, "cash_after": cash_after, "equity_after": equity_after,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def get_last_trade_ts_by_symbol(self, limit=500):
        if self.last_trade_ts_override is not None:
            return self.last_trade_ts_override
        last = {}
        for t in self.trades:  # ordre d'insertion chronologique -> la dernière écriture gagne
            last[t["symbol"]] = t["ts"]
        return last

    def log_error(self, message):
        self.errors.append(message)
        # en base, log_error() et get_recent_errors() touchent la MÊME table
        # (tradingbot_errors) : le faux doit refléter ce couplage, sinon la
        # déduplication des alertes semblerait cassée alors qu'elle marche
        # (get_recent_errors trie par ts décroissant -> insertion en tête)
        self.errors_rows.insert(0, {
            "ts": datetime.now(timezone.utc).isoformat(), "message": message,
        })

    def load_config_overrides(self):
        return self.overrides

    def get_recent_trades(self, limit=10):
        return list(reversed(self.trades))[:limit]

    def get_recent_errors(self, limit=5):
        return self.errors_rows[:limit]

    def get_recent_journal(self, limit=10):
        return list(reversed(self.journal))[:limit]

    def log_journal_entry(self, author, message, data=None):
        self.journal.append({"author": author, "message": message, "data": data})

    def get_last_journal_event(self, event):
        for entry in reversed(self.journal):
            if (entry.get("data") or {}).get("event") == event:
                return entry
        return {}


class Clock:
    def __init__(self, idx):
        self.idx = idx


def _fake_fetch_latest_candles(histories, clock):
    def _fetch(exchange, symbol, timeframe, limit=200):
        df = histories[symbol]
        end = clock.idx + 1
        start = max(0, end - limit)
        return df.iloc[start:end].copy()
    return _fetch


class _AlwaysEnterStrategy:
    """Stub (même interface qu'une RegimeSwitchingStrategy) : entre dès que
    possible, ne sort jamais d'elle-même (stop/target hors de portée) —
    isole le comportement du RACHAT (max_positions_per_symbol, cooldown)
    de la logique de décision d'une vraie stratégie."""
    def __init__(self):
        self._active = None

    def prepare(self, df):
        return df

    def should_enter(self, row):
        return True

    def should_exit_on_signal(self, row):
        return False

    def compute_stop_and_target(self, entry_price, row):
        self._active = "trend"
        return entry_price * 0.01, entry_price * 100, entry_price

    def on_position_closed(self):
        self._active = None

    @property
    def active_regime(self):
        return self._active

    @property
    def trend_strategy(self):
        return self

    @property
    def range_strategy(self):
        return self


def _minimal_cfg(max_positions_per_symbol=1, reentry_cooldown_hours=None, max_concurrent_positions=100):
    """Config minimale pour exercer run_tick() sans dépendre de config.yaml
    — un seul symbole, une stratégie stub (voir _AlwaysEnterStrategy)."""
    return {
        "portfolio": {
            "symbols": ["BTC/USDT"],
            "max_concurrent_positions": max_concurrent_positions,
            "max_positions_per_symbol": max_positions_per_symbol,
            "momentum_lookback": 20,
        },
        "risk": {
            "risk_per_trade_pct": 0.5,
            "max_position_notional_usd": 10.0,
            "reentry_cooldown_hours": reentry_cooldown_hours,
            "max_daily_loss_pct": 0.5,
            "max_total_drawdown_pct": 0.9,
        },
        "paper_trading": {"initial_balance": 1000.0, "lookback_candles": 200},
        "exchange": {"id": "kucoin", "timeframe": "1h"},
        "backtest": {"fee_pct": 0.001, "slippage_pct": 0.0},
        "strategy": {},
    }


def _patch_common(monkeypatch, cfg, histories, clock, fake_db):
    monkeypatch.setattr(web_app, "load_config", lambda: cfg)
    monkeypatch.setattr(web_app, "regime_strategy_from_config", lambda strategy_cfg: _AlwaysEnterStrategy())
    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)


def test_normalize_positions_migrates_old_single_position_format():
    old_format = {"BTC/USDT": {"qty": 0.01, "entry_price": 100.0}, "ETH/USDT": None}
    assert web_app._normalize_positions(old_format) == {
        "BTC/USDT": [{"qty": 0.01, "entry_price": 100.0}], "ETH/USDT": [],
    }


def test_normalize_positions_passes_through_new_list_format_unchanged():
    new_format = {"BTC/USDT": [{"qty": 0.01}, {"qty": 0.02}]}
    assert web_app._normalize_positions(new_format) == new_format


def test_normalize_positions_handles_missing_or_empty_input():
    assert web_app._normalize_positions(None) == {}
    assert web_app._normalize_positions({}) == {}


def test_run_tick_migrates_old_single_position_state_format(monkeypatch):
    """Une position déjà ouverte AVANT ce déploiement (ancien format : un
    dict par paire) ne doit jamais disparaître au premier cycle suivant —
    elle doit rester gérée normalement, juste enveloppée dans une liste."""
    cfg = _minimal_cfg(max_positions_per_symbol=3)
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    fake_db = FakeDB()
    fake_db.state = {
        "cash": 950.0,
        "positions": {"BTC/USDT": {
            "qty": 0.05, "entry_price": 90.0, "stop_price": 1.0, "target_price": 100000.0,
            "active_substrategy": "trend",
        }},
        "daily_current_day": None, "daily_equity_at_day_start": None,
        "daily_tripped_today": False, "total_dd_peak_equity": None, "total_dd_tripped": False,
    }
    _patch_common(monkeypatch, cfg, histories, clock, fake_db)

    clock.idx += 1
    result = web_app.run_tick()

    assert result["ok"] is True
    migrated = fake_db.state["positions"]["BTC/USDT"]
    assert isinstance(migrated, list)
    assert any(p["entry_price"] == 90.0 for p in migrated), "la position pré-existante a disparu à la migration"


def test_run_tick_caps_rebuys_at_max_positions_per_symbol(monkeypatch):
    """Bot #3 : jusqu'à 3 positions simultanées sur UNE paire, jamais plus,
    même si la stratégie continue de signaler une entrée à chaque cycle."""
    cfg = _minimal_cfg(max_positions_per_symbol=3, max_concurrent_positions=100)
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    fake_db = FakeDB()
    _patch_common(monkeypatch, cfg, histories, clock, fake_db)

    for _ in range(6):
        clock.idx += 1
        result = web_app.run_tick()
        assert result["ok"] is True

    assert len(fake_db.state["positions"]["BTC/USDT"]) == 3


def test_run_tick_default_max_positions_per_symbol_matches_historical_behaviour(monkeypatch):
    """Sans max_positions_per_symbol dans la config (bots #1/#2) : jamais
    plus d'une position par paire à la fois, comme avant le rachat."""
    cfg = _minimal_cfg()  # max_positions_per_symbol=1 par défaut
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    fake_db = FakeDB()
    _patch_common(monkeypatch, cfg, histories, clock, fake_db)

    for _ in range(6):
        clock.idx += 1
        result = web_app.run_tick()
        assert result["ok"] is True

    assert len(fake_db.state["positions"]["BTC/USDT"]) == 1


def test_run_tick_reentry_cooldown_blocks_immediate_rebuy(monkeypatch):
    cfg = _minimal_cfg(max_positions_per_symbol=1, reentry_cooldown_hours=5)
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    fake_db = FakeDB()
    # dernière activité sur cette paire il y a 1h -> encore dans le cooldown de 5h
    fake_db.last_trade_ts_override = {
        "BTC/USDT": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    }
    _patch_common(monkeypatch, cfg, histories, clock, fake_db)

    clock.idx += 1
    result = web_app.run_tick()

    assert result["ok"] is True
    assert result["open_positions"] == 0


def test_run_tick_reentry_cooldown_allows_rebuy_once_elapsed(monkeypatch):
    cfg = _minimal_cfg(max_positions_per_symbol=1, reentry_cooldown_hours=5)
    histories = {"BTC/USDT": make_ohlcv(seed=0, n=50, start=100.0)}
    clock = Clock(idx=10)
    fake_db = FakeDB()
    # dernière activité il y a 10h -> le cooldown de 5h est passé
    fake_db.last_trade_ts_override = {
        "BTC/USDT": (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    }
    _patch_common(monkeypatch, cfg, histories, clock, fake_db)

    clock.idx += 1
    result = web_app.run_tick()

    assert result["ok"] is True
    assert result["open_positions"] == 1


def test_run_tick_never_leaves_a_position_with_unresolved_substrategy(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)

    any_position_opened = False
    for _ in range(60):
        clock.idx += 5
        result = web_app.run_tick()
        assert result["ok"] is True

        positions = fake_db.state["positions"] if fake_db.state else {}
        for symbol, open_positions in positions.items():
            for pos in open_positions or []:
                any_position_opened = True
                assert pos["active_substrategy"] in ("trend", "range"), (
                    f"{symbol}: active_substrategy={pos['active_substrategy']!r} — "
                    "une position ouverte doit toujours savoir quelle sous-stratégie gère sa sortie"
                )

    assert any_position_opened, "aucune position ouverte sur 60 ticks : le test ne couvre rien, ajuster les seeds/n"
    assert not fake_db.errors


def test_run_tick_applies_slippage_to_fills(monkeypatch):
    """Le paper trading doit se comporter comme si c'était de l'argent réel :
    portfolio_backtester.py simule un glissement de prix à chaque exécution
    (slippage_pct, voir config.yaml) — run_tick() doit faire pareil, sinon
    le bot déployé obtient des remplissages parfaits qu'aucune exécution
    réelle n'obtiendrait, et les résultats affichés sont trop optimistes."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    slippage_pct = cfg["backtest"]["slippage_pct"]
    assert slippage_pct > 0, "slippage_pct doit être > 0 dans config.yaml pour que ce test prouve quelque chose"

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)

    checked_at_least_one_fill = False
    seen = 0
    for _ in range(60):
        clock.idx += 5
        web_app.run_tick()
        for t in fake_db.trades[seen:]:
            raw_close = float(histories[t["symbol"]]["close"].iloc[clock.idx])
            if t["side"] == "buy":
                expected = raw_close * (1 + slippage_pct)
            elif t["reason"] == "signal":
                expected = raw_close * (1 - slippage_pct)
            else:
                continue  # stop_loss/take_profit : le prix de référence n'est pas le close brut
            assert t["price"] == pytest.approx(expected, rel=1e-9), (
                f"{t['symbol']} {t['side']} ({t['reason']}) : {t['price']} != {expected} attendu "
                "avec le glissement appliqué"
            )
            checked_at_least_one_fill = True
        seen = len(fake_db.trades)

    assert checked_at_least_one_fill, "aucun trade au close exact à vérifier sur 60 ticks — ajuster seeds/n"


def test_run_tick_survives_min_order_limits_lookup_failure(monkeypatch):
    """data.get_min_order_limits() fait un appel réseau (load_markets) — s'il
    échoue, /tick doit continuer à fonctionner sans plancher connu plutôt que
    de planter le cycle entier pour une info secondaire."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    def _boom(exchange, symbols):
        raise RuntimeError("exchange indisponible")

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app.data, "get_min_order_limits", _boom)
    monkeypatch.setattr(web_app, "db", fake_db)

    clock.idx += 5
    result = web_app.run_tick()
    assert result["ok"] is True


def test_run_tick_rejects_entries_below_exchange_min_order(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()

    huge_limits = {s: {"min_amount": None, "min_cost": 10_000_000.0} for s in symbols}

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app.data, "get_min_order_limits", lambda exchange, syms: huge_limits)
    monkeypatch.setattr(web_app, "db", fake_db)

    for _ in range(60):
        clock.idx += 5
        web_app.run_tick()

    assert not fake_db.trades, "aucun ordre ne devrait jamais passer sous le minimum de l'exchange"


def test_run_tick_applies_strategy_overrides_before_constructing_strategies(monkeypatch):
    """recalibrate.py écrit les paramètres gagnants dans
    tradingbot_config.strategy_overrides — run_tick() doit les appliquer
    par-dessus config.yaml AVANT de construire les RegimeSwitchingStrategy,
    pas après (sinon la recalibration n'a aucun effet)."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    original_min_score = cfg["strategy"]["trend"]["min_score_to_enter"]
    original_adx = cfg["strategy"]["regime"]["adx_trend_threshold"]

    strategy_overrides = {"trend.min_score_to_enter": original_min_score + 1,
                           "regime.adx_trend_threshold": original_adx + 15}

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB(overrides={"strategy_overrides": strategy_overrides})

    seen_cfgs = []
    real_factory = web_app.regime_strategy_from_config

    def _spy_factory(strategy_cfg):
        seen_cfgs.append(strategy_cfg)
        return real_factory(strategy_cfg)

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)
    monkeypatch.setattr(web_app, "regime_strategy_from_config", _spy_factory)

    clock.idx += 5
    result = web_app.run_tick()

    assert seen_cfgs, "regime_strategy_from_config n'a jamais été appelé"
    for strat_cfg in seen_cfgs:
        assert strat_cfg["trend"]["min_score_to_enter"] == original_min_score + 1
        assert strat_cfg["regime"]["adx_trend_threshold"] == original_adx + 15
    assert result["active_strategy_overrides"] == strategy_overrides
    assert result["config_overrides_active"] is True


def test_run_tick_uses_config_yaml_strategy_when_no_strategy_overrides(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB()  # pas d'overrides

    seen_cfgs = []
    real_factory = web_app.regime_strategy_from_config

    def _spy_factory(strategy_cfg):
        seen_cfgs.append(strategy_cfg)
        return real_factory(strategy_cfg)

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)
    monkeypatch.setattr(web_app, "regime_strategy_from_config", _spy_factory)

    clock.idx += 5
    result = web_app.run_tick()

    for strat_cfg in seen_cfgs:
        assert strat_cfg["trend"]["min_score_to_enter"] == cfg["strategy"]["trend"]["min_score_to_enter"]
    assert result["active_strategy_overrides"] == {}


def test_apply_config_overrides_defaults_to_config_yaml_when_no_override():
    risk_cfg = {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03}
    pf_cfg = {"symbols": ["BTC/USDT", "ETH/USDT"], "max_concurrent_positions": 2}

    active = web_app._apply_config_overrides(risk_cfg, pf_cfg, overrides={})

    assert risk_cfg["risk_per_trade_pct"] == 0.01  # inchangé
    assert active == {"BTC/USDT", "ETH/USDT"}  # toutes les paires par défaut


def test_apply_config_overrides_applies_overrides():
    risk_cfg = {"risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03,
                "correlation_lookback": 30}
    pf_cfg = {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"], "max_concurrent_positions": 2}

    active = web_app._apply_config_overrides(risk_cfg, pf_cfg, overrides={
        "risk_per_trade_pct": 0.005,
        "max_concurrent_positions": 1,
        "active_symbols": ["BTC/USDT"],
    })

    assert risk_cfg["risk_per_trade_pct"] == 0.005
    assert risk_cfg["max_daily_loss_pct"] == 0.03  # non surchargé, inchangé
    assert pf_cfg["max_concurrent_positions"] == 1
    assert active == {"BTC/USDT"}


def test_run_tick_never_opens_new_positions_on_symbols_deactivated_by_override(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    active_symbol = symbols[0]

    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    clock = Clock(idx=250)
    fake_db = FakeDB(overrides={"active_symbols": [active_symbol]})

    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))
    monkeypatch.setattr(web_app, "db", fake_db)

    for _ in range(60):
        clock.idx += 5
        result = web_app.run_tick()
        assert result["ok"] is True
        assert result["active_symbols"] == [active_symbol]
        assert result["config_overrides_active"] is True

        positions = fake_db.state["positions"] if fake_db.state else {}
        for symbol, pos in positions.items():
            if pos is not None:
                assert symbol == active_symbol, (
                    f"{symbol} a une position ouverte alors qu'il est désactivé par active_symbols"
                )


def test_status_page_renders_journal_entries(monkeypatch):
    fake_db = FakeDB()
    fake_db.get_recent_journal = lambda limit=10: [
        {"ts": "2026-09-08T22:34:15+00:00", "author": "bot",
         "message": "Recalibrage : pas assez robuste pour agir."},
        {"ts": "2026-09-07T10:00:00+00:00", "author": "manager",
         "message": "Vu, on laisse tourner."},
    ]
    monkeypatch.setattr(web_app, "db", fake_db)

    client = web_app.app.test_client()
    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Recalibrage : pas assez robuste pour agir." in html
    assert "Vu, on laisse tourner." in html
    assert "manager" in html


def test_status_page_shows_empty_state_when_no_journal(monkeypatch):
    fake_db = FakeDB()  # get_recent_journal -> []
    monkeypatch.setattr(web_app, "db", fake_db)

    client = web_app.app.test_client()
    html = client.get("/").get_data(as_text=True)

    assert "Aucune entrée pour l'instant." in html


def test_status_page_survives_journal_lookup_failure(monkeypatch):
    fake_db = FakeDB()

    def _boom(limit=10):
        raise RuntimeError("table absente")

    fake_db.get_recent_journal = _boom
    monkeypatch.setattr(web_app, "db", fake_db)

    client = web_app.app.test_client()
    resp = client.get("/")

    # get_recent_journal() réel est best-effort (voir supabase_state.py) —
    # mais si jamais il lève, la page de statut entière ne doit pas planter
    # pour autant : repli sur le texte brut, toujours 200.
    assert resp.status_code == 200


def _patch_market(monkeypatch, symbols, clock):
    histories = {s: make_ohlcv(seed=i, n=700, start=100.0 + i * 20) for i, s in enumerate(symbols)}
    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())
    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _fake_fetch_latest_candles(histories, clock))


def _journal_events(fake_db, event):
    return [e for e in fake_db.journal if (e.get("data") or {}).get("event") == event]


def test_run_tick_acknowledges_config_changes_once(monkeypatch):
    """Quand le manager change tradingbot_config, le bot le dit dans le
    journal — une seule fois, pas à chaque tick tant que rien ne change."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    clock = Clock(idx=250)
    _patch_market(monkeypatch, symbols, clock)
    fake_db = FakeDB(overrides={"risk_per_trade_pct": 0.005, "updated_by": "enzo", "note": "resserré"})
    monkeypatch.setattr(web_app, "db", fake_db)

    for _ in range(3):
        clock.idx += 5
        web_app.run_tick()

    acks = _journal_events(fake_db, "config_change")
    assert len(acks) == 1
    assert acks[0]["author"] == "bot"
    assert "enzo" in acks[0]["message"]
    assert "risk_per_trade_pct=0.005" in acks[0]["message"]
    assert "resserré" in acks[0]["message"]

    # le manager change à nouveau la config -> nouvel acquittement, un seul
    fake_db.overrides = {"active_symbols": [symbols[0]], "updated_by": "cowork"}
    for _ in range(2):
        clock.idx += 5
        web_app.run_tick()

    acks = _journal_events(fake_db, "config_change")
    assert len(acks) == 2
    assert "cowork" in acks[1]["message"]
    assert symbols[0] in acks[1]["message"]


def test_run_tick_does_not_journal_config_when_no_override_was_ever_set(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    clock = Clock(idx=250)
    _patch_market(monkeypatch, symbols, clock)
    fake_db = FakeDB()  # aucun réglage manager
    monkeypatch.setattr(web_app, "db", fake_db)

    clock.idx += 5
    web_app.run_tick()

    assert _journal_events(fake_db, "config_change") == []


def test_run_tick_journals_every_trade_with_its_reason(monkeypatch):
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    clock = Clock(idx=250)
    _patch_market(monkeypatch, symbols, clock)
    fake_db = FakeDB()
    monkeypatch.setattr(web_app, "db", fake_db)

    for _ in range(60):
        clock.idx += 5
        web_app.run_tick()

    trade_entries = _journal_events(fake_db, "trade")
    assert fake_db.trades, "aucun trade sur 60 ticks : le test ne couvre rien"
    assert len(trade_entries) == len(fake_db.trades)
    for e in trade_entries:
        assert e["author"] == "bot"
        if e["data"]["side"] == "buy":
            assert e["data"]["regime"] in ("trend", "range")
        else:
            assert e["data"]["reason"] in ("stop_loss", "take_profit", "signal")


def test_run_tick_journals_daily_circuit_breaker_once(monkeypatch):
    from datetime import datetime, timezone

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    clock = Clock(idx=250)
    _patch_market(monkeypatch, symbols, clock)
    fake_db = FakeDB()
    # équity actuelle 1000, journée démarrée à 2000 -> -50%, bien au-delà du seuil
    fake_db.state = {
        "cash": 1000.0, "positions": {},
        "daily_current_day": datetime.now(timezone.utc).date(),
        "daily_equity_at_day_start": 2000.0, "daily_tripped_today": False,
        "total_dd_peak_equity": None, "total_dd_tripped": False,
    }
    monkeypatch.setattr(web_app, "db", fake_db)

    clock.idx += 5
    result = web_app.run_tick()
    assert result["daily_breaker_tripped"] is True
    assert len(_journal_events(fake_db, "circuit_breaker")) == 1

    clock.idx += 5
    web_app.run_tick()
    assert len(_journal_events(fake_db, "circuit_breaker")) == 1, "déclenché déjà acquitté : pas de doublon"


def test_buy_journal_entry_states_what_the_position_weighs(monkeypatch):
    """Régression du rapport du 09/09/2026 : le bot annonçait "achat de
    0,01262 BTC" sans jamais dire que c'était 99,9% du capital. Une
    quantité brute ne permet à personne de voir une surexposition — le
    journal doit donner le poids, le risque au stop et l'exposition."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]
    clock = Clock(idx=300)
    fake_db = FakeDB()
    _patch_market(monkeypatch, symbols, clock)
    monkeypatch.setattr(web_app, "db", fake_db)

    for _ in range(60):
        clock.idx += 5
        web_app.run_tick()

    buys = [e for e in _journal_events(fake_db, "trade") if e["data"]["side"] == "buy"]
    assert buys, "le scénario doit produire au moins un achat"
    for e in buys:
        d = e["data"]
        assert 0 < d["pct_of_equity"] <= 100
        assert d["notional"] == pytest.approx(d["qty"] * d["price"])
        # le risque annoncé doit être cohérent avec la distance au stop
        equity = d["notional"] / d["pct_of_equity"] * 100
        assert d["risk_if_stopped_pct"] == pytest.approx(
            d["qty"] * (d["price"] - d["stop"]) / equity * 100, rel=1e-6)
        # le risque au stop doit rester sous le budget de risque configuré
        assert d["risk_if_stopped_pct"] <= cfg["risk"]["risk_per_trade_pct"] * 100 + 1e-6
        assert 0 <= d["exposure_pct"] <= 100 + 1e-9
        # ...et le message lisible doit porter ces nombres, pas seulement le data
        assert "% du capital" in e["message"]
        assert "exposition totale" in e["message"]


def test_buy_journal_reports_concentration_faithfully_when_capped(monkeypatch):
    """Le poids annoncé doit refléter le plafond réellement appliqué : sans
    plafond la position peut monter à ~100% du capital, avec plafond elle
    doit être annoncée à sa taille plafonnée."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    symbols = cfg["portfolio"]["symbols"]

    def _run(cap):
        clock = Clock(idx=300)
        fake_db = FakeDB(overrides={"max_position_pct_of_equity": cap} if cap else None)
        _patch_market(monkeypatch, symbols, clock)
        monkeypatch.setattr(web_app, "db", fake_db)
        for _ in range(60):
            clock.idx += 5
            web_app.run_tick()
        return [e["data"]["pct_of_equity"]
                for e in _journal_events(fake_db, "trade") if e["data"]["side"] == "buy"]

    capped = _run(0.10)
    assert capped, "le scénario doit produire au moins un achat"
    assert max(capped) <= 10.0 + 1e-6, f"plafond 10% non respecté : {max(capped):.1f}%"


def test_tick_response_exposes_total_exposure(monkeypatch):
    """L'exposition doit être lisible dans /tick même si tradingbot_journal
    n'existe pas encore (log_journal_entry est best-effort et silencieux)."""
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    clock = Clock(idx=300)
    fake_db = FakeDB()
    _patch_market(monkeypatch, cfg["portfolio"]["symbols"], clock)
    monkeypatch.setattr(web_app, "db", fake_db)

    seen_invested = False
    for _ in range(60):
        clock.idx += 5
        result = web_app.run_tick()
        assert 0 <= result["exposure_pct"] <= 100 + 1e-9
        if result["open_positions"] > 0:
            assert result["exposure_pct"] > 0
            seen_invested = True
        else:
            assert result["exposure_pct"] == pytest.approx(0.0, abs=1e-9)
    assert seen_invested, "le scénario doit ouvrir au moins une position"


# --------------------------------------------------------------------
# /all — vue combinée en lecture seule des 3 bots
# --------------------------------------------------------------------

def _fake_requests_get(monkeypatch, tables):
    """tables: {table_name: rows_or_None (None = simule une table absente)}."""
    monkeypatch.setenv("SUPABASE_URL", "https://exemple.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "cle-factice")
    calls = []

    class _Resp:
        def __init__(self, rows):
            self._rows = rows

        def raise_for_status(self):
            if self._rows is None:
                raise RuntimeError("relation does not exist")

        def json(self):
            return self._rows

    def _get(url, headers=None, params=None, timeout=None):
        table = url.rsplit("/", 1)[-1]
        calls.append(table)
        return _Resp(tables.get(table))

    monkeypatch.setattr(web_app.requests, "get", _get)
    return calls


def test_fetch_bot_summary_reads_the_given_prefix_only(monkeypatch):
    """/all garde le résumé chiffré (cash, positions) mais pas le détail
    trade-par-trade ni le journal — ça reste sur la page propre à chaque
    bot, à un clic. Donc plus besoin de lire la table journal ici."""
    calls = _fake_requests_get(monkeypatch, {
        "altbot_state": [{"cash": 850.0, "positions": {"BNB/USDT": {}}, "daily_tripped_today": False, "total_dd_tripped": False}],
        "altbot_trades": [{"symbol": "BNB/USDT", "side": "buy", "reason": "signal", "price": 600.0, "qty": 0.01, "ts": "2026-09-11T07:00:00Z", "equity_after": 994.0}],
    })

    summary = web_app._fetch_bot_summary("altbot")

    assert calls == ["altbot_state", "altbot_trades"]
    assert summary["found"] is True
    assert summary["healthy"] is True
    assert summary["cash"] == 850.0
    assert summary["open_positions"] == 1
    assert summary["closed_positions"] == 0
    assert summary["equity_points"] == [("2026-09-11T07:00:00Z", 994.0)]


def test_fetch_bot_summary_counts_closed_positions_from_sell_trades(monkeypatch):
    """closed_positions = nombre de ventes réellement exécutées, pas le nombre
    de trades total (les achats ne clôturent rien)."""
    _fake_requests_get(monkeypatch, {
        "tradingbot_state": [{"cash": 900.0, "positions": {"ETH/USDT": {}}, "daily_tripped_today": False, "total_dd_tripped": False}],
        "tradingbot_trades": [
            {"symbol": "BTC/USDT", "side": "buy", "reason": "signal", "price": 60000.0, "qty": 0.01, "ts": "2026-09-01T00:00:00Z", "equity_after": 1000.0},
            {"symbol": "BTC/USDT", "side": "sell", "reason": "target", "price": 61000.0, "qty": 0.01, "ts": "2026-09-02T00:00:00Z", "equity_after": 1010.0},
            {"symbol": "ETH/USDT", "side": "buy", "reason": "signal", "price": 3000.0, "qty": 0.1, "ts": "2026-09-03T00:00:00Z", "equity_after": 1010.0},
        ],
    })

    summary = web_app._fetch_bot_summary("tradingbot")

    assert summary["open_positions"] == 1
    assert summary["closed_positions"] == 1


def test_fetch_bot_summary_degrades_gracefully_when_table_is_missing(monkeypatch):
    """Un bot pas encore migré ne doit jamais faire planter la vue combinée
    pour les bots qui, eux, existent déjà."""
    _fake_requests_get(monkeypatch, {"microbot_state": None, "microbot_trades": None})

    summary = web_app._fetch_bot_summary("microbot")

    assert summary["found"] is False
    assert summary["equity_points"] == []


def test_fetch_bot_summary_flags_total_drawdown_as_unhealthy(monkeypatch):
    _fake_requests_get(monkeypatch, {
        "tradingbot_state": [{"cash": 500.0, "positions": {}, "daily_tripped_today": False, "total_dd_tripped": True}],
        "tradingbot_trades": [],
    })

    summary = web_app._fetch_bot_summary("tradingbot")

    assert summary["healthy"] is False
    assert summary["total_dd_tripped"] is True


def test_fetch_bot_summary_builds_equity_points_in_chronological_order(monkeypatch):
    _fake_requests_get(monkeypatch, {
        "tradingbot_state": [{"cash": 900.0, "positions": {}, "daily_tripped_today": False, "total_dd_tripped": False}],
        "tradingbot_trades": [
            {"symbol": "BTC/USDT", "side": "buy", "reason": "signal", "price": 60000.0, "qty": 0.01, "ts": "2026-09-01T00:00:00Z", "equity_after": 1000.0},
            {"symbol": "BTC/USDT", "side": "sell", "reason": "target", "price": 61000.0, "qty": 0.01, "ts": "2026-09-02T00:00:00Z", "equity_after": 1010.0},
        ],
    })

    summary = web_app._fetch_bot_summary("tradingbot")

    assert summary["equity_points"] == [
        ("2026-09-01T00:00:00Z", 1000.0),
        ("2026-09-02T00:00:00Z", 1010.0),
    ]


def test_all_route_shows_every_bot_even_when_one_has_no_data(monkeypatch):
    _fake_requests_get(monkeypatch, {
        "tradingbot_state": [{"cash": 950.0, "positions": {}, "daily_tripped_today": False, "total_dd_tripped": False}],
        "tradingbot_trades": [],
        "altbot_state": [{"cash": 900.0, "positions": {}, "daily_tripped_today": False, "total_dd_tripped": False}],
        "altbot_trades": [],
        "microbot_state": None, "microbot_trades": None,
    })

    client = web_app.app.test_client()
    resp = client.get("/all")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Bot #1" in html and "Bot #2" in html and "Bot #3" in html
    assert "En ligne" in html
    assert "$950.00" in html and "$900.00" in html
    assert "Aucune donnée pour l'instant" in html  # bot #3, pas encore migré


def test_all_route_shows_the_summary_but_never_the_trade_by_trade_detail(monkeypatch):
    """/all garde cash + positions ouvertes/clôturées (utile en un coup
    d'oeil), mais pas les derniers trades ni le journal — déjà sur la page
    complète de chaque bot (le lien "Voir la page complète"), les dupliquer
    ici serait juste du bruit."""
    _fake_requests_get(monkeypatch, {
        "tradingbot_state": [{"cash": 950.0, "positions": {"BTC/USDT": {}}, "daily_tripped_today": False, "total_dd_tripped": False}],
        "tradingbot_trades": [
            {"symbol": "BTC/USDT", "side": "sell", "reason": "target", "price": 61000.0, "qty": 0.01, "ts": "2026-09-02T00:00:00Z", "equity_after": 1010.0},
        ],
        "altbot_state": None, "altbot_trades": None,
        "microbot_state": None, "microbot_trades": None,
    })

    html = web_app.app.test_client().get("/all").get_data(as_text=True)

    assert "Positions ouvertes" in html
    assert "Positions clôturées" in html
    assert "$950.00" in html
    assert "Derniers trades" not in html
    assert "Journal" not in html
    assert "BTC/USDT" not in html  # pas de ligne de trade individuelle


def test_all_route_never_crashes_even_if_supabase_is_unreachable(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    resp = web_app.app.test_client().get("/all")

    assert resp.status_code == 200


def test_all_route_has_pull_to_refresh_like_every_bot_s_own_page(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    html = web_app.app.test_client().get("/all").get_data(as_text=True)

    assert 'id="pull-indicator"' in html
    assert "touchstart" in html and "touchend" in html


# --------------------------------------------------------------------
# _nice_step / _build_equity_chart_svg — géométrie du graphique combiné
# --------------------------------------------------------------------

def test_nice_step_rounds_up_to_a_clean_number():
    assert web_app._nice_step(0) == 1.0
    assert web_app._nice_step(3) == 5.0
    assert web_app._nice_step(12) == 20.0
    assert web_app._nice_step(430) == 500.0
    assert web_app._nice_step(0.03) == pytest.approx(0.05)


def test_build_equity_chart_svg_returns_empty_string_when_no_points():
    assert web_app._build_equity_chart_svg([{"label": "Bot #1", "color_var": "--series-1", "points": []}]) == ""
    assert web_app._build_equity_chart_svg([]) == ""


def test_build_equity_chart_svg_skips_series_without_points_but_keeps_others():
    points = [(datetime(2026, 9, 1, tzinfo=timezone.utc), 1000.0),
              (datetime(2026, 9, 2, tzinfo=timezone.utc), 1050.0)]
    svg = web_app._build_equity_chart_svg([
        {"label": "Bot #1", "color_var": "--series-1", "points": points},
        {"label": "Bot #3", "color_var": "--series-3", "points": []},
    ])

    assert "Bot #1" in svg
    assert "Bot #3" not in svg
    assert svg.startswith("<svg")


def test_build_equity_chart_svg_never_puts_y_tick_labels_on_the_right(monkeypatch):
    """Régression : les graduations Y doivent rester à gauche pour ne jamais
    chevaucher les étiquettes directes de fin de ligne, placées à droite."""
    points = [(datetime(2026, 9, 1, tzinfo=timezone.utc), 1000.0),
              (datetime(2026, 9, 2, tzinfo=timezone.utc), 1230.0)]
    svg = web_app._build_equity_chart_svg([{"label": "Bot #1", "color_var": "--series-1", "points": points}])

    assert 'text-anchor="end"' in svg  # les ticks $ sont ancrés à droite de leur texte, côté gauche du graphique
    assert "départ" not in svg  # plus d'étiquette inline superposée aux courbes


def test_build_equity_chart_svg_separates_colliding_end_labels():
    """Deux séries qui finissent à des valeurs très proches ne doivent pas
    produire des étiquettes qui se chevauchent verticalement."""
    pts_a = [(datetime(2026, 9, 1, tzinfo=timezone.utc), 1000.0),
             (datetime(2026, 9, 2, tzinfo=timezone.utc), 1001.0)]
    pts_b = [(datetime(2026, 9, 1, tzinfo=timezone.utc), 1000.0),
             (datetime(2026, 9, 2, tzinfo=timezone.utc), 1000.5)]
    svg = web_app._build_equity_chart_svg([
        {"label": "Bot #1", "color_var": "--series-1", "points": pts_a},
        {"label": "Bot #3", "color_var": "--series-3", "points": pts_b},
    ])

    import re
    ys = [float(m) for m in re.findall(r'<text x="[\d.]+" y="([\d.]+)" font-size="10.5"', svg)]
    assert len(ys) == 2
    assert abs(ys[0] - ys[1]) >= 14
