"""
Les alertes ont deux façons de rater : ne pas partir quand il faut, et
partir trop souvent (auquel cas on apprend à les ignorer, et elles ne
protègent plus de rien). Ces tests couvrent les deux, plus la règle
absolue : une alerte ne doit JAMAIS faire échouer un cycle de trading.
"""

from datetime import datetime, timedelta, timezone

import pytest
import yaml

import alerts
import web_app
from test_web_app import Clock, FakeDB, _patch_market


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


def _capture(monkeypatch, status=200, boom=False):
    sent = []

    def _post(url, json=None, timeout=None):
        sent.append({"url": url, "json": json, "timeout": timeout})
        if boom:
            raise ConnectionError("réseau injoignable")
        return FakeResponse(status)

    monkeypatch.setattr(alerts.requests, "post", _post)
    return sent


def test_send_is_inert_without_configuration(monkeypatch):
    """Par défaut — et notamment en test — le module ne doit rien envoyer."""
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    sent = _capture(monkeypatch)
    assert alerts.is_configured() is False
    assert alerts.send("quelque chose") is False
    assert sent == []


def test_send_payload_serves_discord_and_slack(monkeypatch):
    """Discord lit "content", Slack lit "text" ; envoyer les deux évite une
    détection fragile et sert les deux sans configuration en plus."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    sent = _capture(monkeypatch)
    assert alerts.send("capital à 900", alerts.CRITICAL) is True
    assert sent[0]["json"]["content"] == sent[0]["json"]["text"]
    assert "capital à 900" in sent[0]["json"]["content"]
    assert alerts.CRITICAL in sent[0]["json"]["content"]
    assert sent[0]["timeout"] == alerts.TIMEOUT_SECONDS


def test_send_payload_for_telegram_carries_chat_id(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://api.telegram.org/bot123/sendMessage")
    monkeypatch.setenv("ALERT_CHAT_ID", "4242")
    sent = _capture(monkeypatch)
    alerts.send("coucou")
    assert sent[0]["json"]["chat_id"] == "4242"
    assert "coucou" in sent[0]["json"]["text"]
    assert "content" not in sent[0]["json"]


def test_send_never_raises_whatever_happens(monkeypatch):
    """Une alerte qui n'arrive pas est ennuyeuse ; un cycle de trading qui
    plante parce que le webhook est lent est inacceptable."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    _capture(monkeypatch, boom=True)
    assert alerts.send("message") is False           # exception réseau avalée
    _capture(monkeypatch, status=500)
    assert alerts.send("message") is False           # statut d'erreur avalé


def test_message_is_truncated_for_chat_services(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    sent = _capture(monkeypatch)
    alerts.send("x" * 5000)
    assert len(sent[0]["json"]["content"]) <= 1900


# --------------------------------------------------------------------
# Déduplication : la partie qui décide si on reste écoutable
# --------------------------------------------------------------------

def _db_with_last_error(minutes_ago):
    db = FakeDB()
    if minutes_ago is not None:
        ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        db.errors_rows = [{"ts": ts.isoformat(), "message": "boum"}]
    return db


def test_new_error_episode_true_when_no_recent_error(monkeypatch):
    monkeypatch.setattr(web_app, "db", _db_with_last_error(None))
    assert web_app._is_new_error_episode() is True


def test_new_error_episode_false_during_cooldown(monkeypatch):
    """Un exchange injoignable ne doit pas générer une alerte par tick."""
    monkeypatch.setattr(web_app, "db", _db_with_last_error(minutes_ago=5))
    assert web_app._is_new_error_episode() is False


def test_new_error_episode_true_again_after_cooldown(monkeypatch):
    db = _db_with_last_error(minutes_ago=web_app.ERROR_ALERT_COOLDOWN_MINUTES + 1)
    monkeypatch.setattr(web_app, "db", db)
    assert web_app._is_new_error_episode() is True


@pytest.mark.parametrize("rows", [
    [{"ts": None}],                    # horodatage absent
    [{"ts": "pas une date"}],          # horodatage inexploitable
])
def test_new_error_episode_prefers_silence_when_it_cannot_deduplicate(monkeypatch, rows):
    """En cas de doute on se tait : mieux vaut manquer une alerte que d'en
    envoyer une toutes les cinq minutes."""
    db = FakeDB()
    db.errors_rows = rows
    monkeypatch.setattr(web_app, "db", db)
    assert web_app._is_new_error_episode() is False


def test_new_error_episode_survives_an_unreadable_errors_table(monkeypatch):
    db = FakeDB()

    def _boom(limit=5):
        raise RuntimeError("table absente")

    db.get_recent_errors = _boom
    monkeypatch.setattr(web_app, "db", db)
    assert web_app._is_new_error_episode() is False


# --------------------------------------------------------------------
# Bout en bout, dans un vrai cycle
# --------------------------------------------------------------------

def test_fetch_errors_alert_once_then_stay_quiet(monkeypatch):
    """Trois ticks d'affilée avec l'exchange injoignable : une seule alerte."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    sent = _capture(monkeypatch)

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    db = FakeDB()
    monkeypatch.setattr(web_app, "db", db)
    monkeypatch.setattr(web_app.data, "get_exchange", lambda exchange_id: object())

    def _always_fails(exchange, symbol, timeframe, limit=200):
        raise RuntimeError("exchange injoignable")

    monkeypatch.setattr(web_app.data, "fetch_latest_candles", _always_fails)

    for _ in range(3):
        result = web_app.run_tick()
        assert result["ok"] is True, "une panne de données ne doit pas faire échouer le cycle"

    assert len(sent) == 1, f"{len(sent)} alertes envoyées pour un seul épisode"
    assert "impossibles à récupérer" in sent[0]["json"]["content"]
    assert len(db.errors) == 3 * len(cfg["portfolio"]["symbols"]), "toutes les erreurs restent tracées"


def _tripped_state():
    # capital très en dessous du plus haut connu -> le drawdown total se déclenche
    return {
        "cash": 500.0, "positions": {},
        "daily_current_day": None, "daily_equity_at_day_start": None,
        "daily_tripped_today": False,
        "total_dd_peak_equity": 1000.0, "total_dd_tripped": False,
    }


def test_total_drawdown_breaker_sends_a_critical_alert_once(monkeypatch):
    """Le coupe-circuit total ne se réarme jamais seul : il exige un humain,
    donc il alerte — mais une seule fois, pas à chaque tick."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    sent = _capture(monkeypatch)

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    clock = Clock(idx=300)
    db = FakeDB()
    db.state = _tripped_state()
    _patch_market(monkeypatch, cfg["portfolio"]["symbols"], clock)
    monkeypatch.setattr(web_app, "db", db)

    for _ in range(3):
        clock.idx += 5
        web_app.run_tick()

    criticals = [s for s in sent if alerts.CRITICAL in s["json"]["content"]]
    assert len(criticals) == 1, f"{len(criticals)} alertes critiques au lieu d'une"
    assert "DRAWDOWN TOTAL" in criticals[0]["json"]["content"]
    assert "ne se réarme pas tout seul" in criticals[0]["json"]["content"]


def test_a_failing_webhook_never_breaks_a_cycle(monkeypatch):
    """Même règle qu'ailleurs : le trading passe avant la notification."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
    _capture(monkeypatch, boom=True)

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    clock = Clock(idx=300)
    db = FakeDB()
    db.state = _tripped_state()
    _patch_market(monkeypatch, cfg["portfolio"]["symbols"], clock)
    monkeypatch.setattr(web_app, "db", db)

    clock.idx += 5
    assert web_app.run_tick()["ok"] is True
