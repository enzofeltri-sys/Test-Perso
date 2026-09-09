"""
discord_bot.py
---------------
Contrairement à `alerts.py` (un webhook qui ne sait qu'ÉCRIRE dans un
salon), ce module LIT la structure d'un serveur Discord via l'API REST,
avec un vrai bot (token bot + accès au serveur), pour lister ses salons.

Utilisé uniquement à la demande (endpoint /discord-channels, protégé par
ALERT_TEST_TOKEN comme /alert-test) — jamais depuis run_tick : contrairement
aux alertes, un cycle de trading n'a aucune raison de lister des salons, donc
aucune des règles "jamais échouer / jamais spammer" d'alerts.py ne s'applique
ici. Une erreur (token invalide, bot absent du serveur) doit remonter
normalement pour être diagnostiquée.

Configuration — deux variables d'environnement, sur Render :

    DISCORD_BOT_TOKEN   token du bot (Discord Developer Portal → Bot →
                         Reset Token), avec le bot invité sur le serveur
    DISCORD_GUILD_ID    identifiant du serveur (clic droit sur son icône
                         en mode développeur → Copier l'ID du serveur)
"""

import os

import requests

API_BASE = "https://discord.com/api/v10"
TIMEOUT_SECONDS = 10

# https://discord.com/developers/docs/resources/channel#channel-object-channel-types
CHANNEL_TYPES = {
    0: "texte",
    2: "vocal",
    4: "catégorie",
    5: "annonces",
    13: "scène",
    15: "forum",
    16: "média",
}


def is_configured() -> bool:
    return bool(os.environ.get("DISCORD_BOT_TOKEN")) and bool(os.environ.get("DISCORD_GUILD_ID"))


def list_channels() -> list:
    """Liste les salons du serveur configuré, triés dans l'ordre où
    Discord les affiche (le champ `position` de l'API, propre à chaque
    catégorie). Laisse remonter toute erreur (réseau, token invalide,
    bot absent du serveur) — à l'appelant de la présenter."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild_id:
        raise RuntimeError("DISCORD_BOT_TOKEN et DISCORD_GUILD_ID doivent être définis")

    resp = requests.get(
        f"{API_BASE}/guilds/{guild_id}/channels",
        headers={"Authorization": f"Bot {token}"},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    return sorted(
        (
            {
                "id": c["id"],
                "name": c["name"],
                "type": CHANNEL_TYPES.get(c["type"], f"type {c['type']}"),
                "position": c.get("position", 0),
            }
            for c in resp.json()
        ),
        key=lambda c: c["position"],
    )
