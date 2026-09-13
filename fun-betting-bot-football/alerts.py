"""
alerts.py
---------
Notifications sortantes quand quelque chose mérite un ŒIL HUMAIN.

Pourquoi c'est nécessaire : UptimeRobot ne sait qu'une chose, "le service
répond". Or `/tick` répond volontairement 200 même en cas d'erreur (une
erreur de trading ne doit pas faire croire à une panne de service et
déclencher de fausses alertes d'hébergement). Résultat, sans ce module :
un coupe-circuit qui se déclenche à 3h du matin, ou l'exchange qui refuse
les requêtes pendant six heures, personne ne l'apprend avant la prochaine
visite de la page de statut.

Deux règles de conception, non négociables :

1. **Ne JAMAIS faire échouer un cycle.** Tout est best-effort, avec
   timeout court : une alerte qui n'arrive pas est ennuyeuse, un cycle de
   trading qui plante parce que Discord est lent est inacceptable. Toute
   exception est avalée, comme dans supabase_state.log_error().

2. **Ne JAMAIS spammer.** Une alerte qu'on reçoit toutes les 5 minutes
   devient un bruit qu'on apprend à ignorer — et le jour où elle compte
   vraiment, on ne la lit plus. La déduplication est de la responsabilité
   de l'APPELANT (voir web_app.run_tick : les coupe-circuits n'alertent
   qu'à la transition, les erreurs qu'à la première d'une fenêtre de
   temps), parce que lui seul sait ce qui constitue un événement nouveau.

Configuration — sur Render :

    ALERT_WEBHOOK_URL   URL du webhook (Discord, Slack, ou Telegram)
    ALERT_CHAT_ID       uniquement pour Telegram (identifiant du destinataire)
    BOT_LABEL           optionnel, défaut "bot de trading". Préfixe chaque
                         alerte (ex: "bot altcoins") — utile quand PLUSIEURS
                         bots partagent le même webhook/canal, pour savoir
                         lequel a parlé sans deviner.

Non renseignée = module inerte, le bot tourne exactement comme avant.
C'est le comportement par défaut, y compris en test.
"""

import os

import requests

TIMEOUT_SECONDS = 10

# Sévérités — juste un préfixe lisible d'un coup d'œil sur mobile.
CRITICAL = "🛑"
WARNING = "⚠️"
INFO = "ℹ️"


def is_configured() -> bool:
    return bool(os.environ.get("ALERT_WEBHOOK_URL"))


def _payload(url: str, text: str) -> dict:
    """Forme du corps attendue par le service destinataire.

    Discord attend {"content": ...}, Slack {"text": ...}. Les deux
    IGNORENT poliment la clé de l'autre, donc envoyer les deux permet de
    servir les deux sans configuration supplémentaire ni détection
    fragile. Telegram, lui, exige un chat_id explicite."""
    if "api.telegram.org" in url:
        return {"chat_id": os.environ.get("ALERT_CHAT_ID", ""), "text": text}
    return {"content": text, "text": text}


def send(message: str, severity: str = WARNING) -> bool:
    """Envoie une alerte. Retourne True si elle est partie, False sinon —
    jamais d'exception, quelle que soit la panne côté réseau ou service."""
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return False
    try:
        label = os.environ.get("BOT_LABEL") or "bot de trading"
        text = f"{severity} [{label}] {message}"[:1900]
        resp = requests.post(url, json=_payload(url, text), timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        return True
    except Exception:
        return False
