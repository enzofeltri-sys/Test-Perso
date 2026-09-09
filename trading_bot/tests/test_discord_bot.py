"""
discord_bot.py lit la structure d'un serveur (contrairement à alerts.py qui
ne fait qu'y écrire) : ces tests couvrent la configuration, le tri des
salons dans l'ordre d'affichage Discord, et la propagation des erreurs —
qu'un cycle de trading ne peut jamais rencontrer (ce module n'est appelé
que depuis /discord-channels, jamais depuis run_tick).
"""

import pytest
import requests

import discord_bot
import web_app


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"HTTP {self.status}")

    def json(self):
        return self._payload


def _capture(monkeypatch, payload=None, status=200):
    calls = []

    def _get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(payload or [], status)

    monkeypatch.setattr(discord_bot.requests, "get", _get)
    return calls


def test_is_configured_requires_both_token_and_guild(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    assert discord_bot.is_configured() is False

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "un-token")
    assert discord_bot.is_configured() is False

    monkeypatch.setenv("DISCORD_GUILD_ID", "12345")
    assert discord_bot.is_configured() is True


def test_list_channels_requires_configuration(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    with pytest.raises(RuntimeError):
        discord_bot.list_channels()


def test_list_channels_authenticates_as_the_bot_on_the_right_guild(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "mon-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")
    calls = _capture(monkeypatch, payload=[])
    discord_bot.list_channels()
    assert calls[0]["url"] == f"{discord_bot.API_BASE}/guilds/999/channels"
    assert calls[0]["headers"]["Authorization"] == "Bot mon-token"
    assert calls[0]["timeout"] == discord_bot.TIMEOUT_SECONDS


def test_list_channels_sorts_by_display_position_and_labels_types(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "mon-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")
    _capture(monkeypatch, payload=[
        {"id": "2", "name": "général", "type": 0, "position": 1},
        {"id": "1", "name": "annonces", "type": 5, "position": 0},
        {"id": "3", "name": "vocal", "type": 2, "position": 2},
        {"id": "4", "name": "mystère", "type": 99, "position": 3},
    ])
    channels = discord_bot.list_channels()
    assert [c["name"] for c in channels] == ["annonces", "général", "vocal", "mystère"]
    assert channels[0]["type"] == "annonces"
    assert channels[1]["type"] == "texte"
    assert channels[2]["type"] == "vocal"
    assert channels[3]["type"] == "type 99"


def test_list_channels_propagates_http_errors(monkeypatch):
    """Contrairement à alerts.send(), ce module ne doit RIEN avaler : une
    erreur ici sert à diagnostiquer, pas à protéger un cycle de trading."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "mauvais-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")
    _capture(monkeypatch, status=401)
    with pytest.raises(requests.HTTPError):
        discord_bot.list_channels()


# --------------------------------------------------------------------
# /discord-channels — même protection par jeton que /alert-test
# --------------------------------------------------------------------

def _client():
    return web_app.app.test_client()


def test_endpoint_does_not_exist_without_a_token(monkeypatch):
    monkeypatch.delenv("ALERT_TEST_TOKEN", raising=False)
    resp = _client().get("/discord-channels")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_endpoint_rejects_a_wrong_token(monkeypatch):
    monkeypatch.setenv("ALERT_TEST_TOKEN", "le-bon-jeton")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "mon-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")
    calls = _capture(monkeypatch, payload=[])

    assert _client().get("/discord-channels").status_code == 403
    assert _client().get("/discord-channels?token=mauvais").status_code == 403
    assert calls == []


def test_endpoint_reports_missing_discord_configuration(monkeypatch):
    monkeypatch.setenv("ALERT_TEST_TOKEN", "le-bon-jeton")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)

    body = _client().get("/discord-channels?token=le-bon-jeton").get_json()
    assert body["ok"] is False
    assert "DISCORD_BOT_TOKEN" in body["error"]


def test_endpoint_lists_channels_with_the_right_token(monkeypatch):
    monkeypatch.setenv("ALERT_TEST_TOKEN", "le-bon-jeton")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "mon-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")
    _capture(monkeypatch, payload=[
        {"id": "1", "name": "général", "type": 0, "position": 0},
    ])

    resp = _client().get("/discord-channels?token=le-bon-jeton")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["channels"] == [{"id": "1", "name": "général", "type": "texte", "position": 0}]


def test_endpoint_reports_discord_api_failure_without_crashing(monkeypatch):
    monkeypatch.setenv("ALERT_TEST_TOKEN", "le-bon-jeton")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "mauvais-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "999")
    _capture(monkeypatch, status=401)

    resp = _client().get("/discord-channels?token=le-bon-jeton")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is False
    assert "impossible de lister les salons" in body["error"]
