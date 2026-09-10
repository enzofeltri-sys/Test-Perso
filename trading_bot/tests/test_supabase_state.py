"""
Le préfixe de table (TABLE_PREFIX) permet de faire tourner plusieurs bots
indépendants sur le même projet Supabase, chacun avec ses propres tables
(voir DEPLOIEMENT.md, "Un deuxième bot en parallèle"). Ces tests vérifient
que chaque fonction publique interroge la bonne table selon le préfixe,
et que l'absence de préfixe retombe sur "tradingbot" (comportement
identique à avant l'introduction de cette variable — le bot #1 ne doit
RIEN changer).
"""

import pytest

import supabase_state as db


class FakeResponse:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else []
        self.status_code = status

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def _capture(monkeypatch):
    calls = []

    def _get(url, headers=None, params=None, timeout=None):
        calls.append({"method": "GET", "url": url})
        return FakeResponse([])

    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"method": "POST", "url": url})
        return FakeResponse({})

    monkeypatch.setattr(db.requests, "get", _get)
    monkeypatch.setattr(db.requests, "post", _post)
    monkeypatch.setenv("SUPABASE_URL", "https://exemple.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "cle-factice")
    return calls


def _table_of(calls, index=0):
    return calls[index]["url"].rsplit("/", 1)[-1]


@pytest.mark.parametrize("env_value,expected_prefix", [
    (None, "tradingbot"),   # absent -> comportement d'avant TABLE_PREFIX
    ("", "tradingbot"),     # vide -> idem, jamais une table "_state"
    ("altbot", "altbot"),
])
def test_load_state_respects_table_prefix(monkeypatch, env_value, expected_prefix):
    calls = _capture(monkeypatch)
    if env_value is None:
        monkeypatch.delenv("TABLE_PREFIX", raising=False)
    else:
        monkeypatch.setenv("TABLE_PREFIX", env_value)

    db.load_state(initial_balance=1000.0)

    assert _table_of(calls) == f"{expected_prefix}_state"


def test_two_prefixes_never_collide(monkeypatch):
    """Le cas d'usage réel : bot #1 (tradingbot) et bot #2 (altbot) tournant
    en parallèle ne doivent jamais lire/écrire la même table."""
    calls = _capture(monkeypatch)

    monkeypatch.setenv("TABLE_PREFIX", "tradingbot")
    db.load_state(1000.0)
    monkeypatch.setenv("TABLE_PREFIX", "altbot")
    db.load_state(1000.0)

    tables = [_table_of(calls, i) for i in range(2)]
    assert tables == ["tradingbot_state", "altbot_state"]
    assert tables[0] != tables[1]


@pytest.mark.parametrize("fn,args,expected_suffix", [
    (lambda: db.save_state({"cash": 1, "positions": {}}), (), "state"),
    (lambda: db.log_trade("BTC/USDT", "buy", 1.0, 1.0, "signal", 1.0, 1.0), (), "trades"),
    (lambda: db.get_recent_trades(), (), "trades"),
    (lambda: db.get_recent_errors(), (), "errors"),
    (lambda: db.log_journal_entry("bot", "message"), (), "journal"),
    (lambda: db.get_recent_journal(), (), "journal"),
    (lambda: db.get_last_journal_event("trade"), (), "journal"),
    (lambda: db.load_config_overrides(), (), "config"),
    (lambda: db.save_strategy_overrides({"a": 1}), (), "config"),
    (lambda: db.log_error("boum"), (), "errors"),
])
def test_every_function_uses_its_own_table_under_a_custom_prefix(monkeypatch, fn, args, expected_suffix):
    calls = _capture(monkeypatch)
    monkeypatch.setenv("TABLE_PREFIX", "altbot")

    fn()

    assert _table_of(calls) == f"altbot_{expected_suffix}"
