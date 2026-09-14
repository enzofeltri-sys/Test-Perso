"""
Normalisation des noms d'équipes entre The Odds API et football-data.co.uk.

Les deux sources ne nomment pas les équipes pareil (ex: "Manchester United"
vs "Man United", "Paris Saint Germain" vs "Paris SG"). On a besoin d'un nom
commun pour relier un match à venir (via The Odds API) à l'historique de
l'équipe (via football-data.co.uk) et calculer ses features de forme.

⚠️ La table ci-dessous est construite à partir de ma connaissance générale
des conventions de nommage de ces deux sources, PAS vérifiée en direct
(mon environnement ne peut pas appeler ces APIs). Vérifie/complète-la une
fois le bot déployé avec une vraie clé, en comparant les noms qui
n'apparaissent jamais dans le journal (data.event="unmatched_team").
"""

import difflib

# The Odds API (clé) -> football-data.co.uk (valeur)
ODDS_API_TO_FOOTBALL_DATA = {
    # Premier League
    "Manchester United": "Man United",
    "Manchester City": "Man City",
    "Newcastle United": "Newcastle",
    "Tottenham Hotspur": "Tottenham",
    "Wolverhampton Wanderers": "Wolves",
    "Brighton and Hove Albion": "Brighton",
    "West Ham United": "West Ham",
    "Nottingham Forest": "Nott'm Forest",
    "Leicester City": "Leicester",
    "Ipswich Town": "Ipswich",
    "Sheffield United": "Sheffield United",
    "Leeds United": "Leeds",
    "Luton Town": "Luton",
    # Ligue 1
    "Paris Saint Germain": "Paris SG",
    "Olympique Marseille": "Marseille",
    "Olympique Lyonnais": "Lyon",
    "AS Monaco": "Monaco",
    "Stade Rennais": "Rennes",
    "RC Lens": "Lens",
    "OGC Nice": "Nice",
    "Stade de Reims": "Reims",
    "RC Strasbourg": "Strasbourg",
    "Toulouse FC": "Toulouse",
    "Montpellier HSC": "Montpellier",
    "FC Nantes": "Nantes",
    "Stade Brestois": "Brest",
    "Le Havre AC": "Le Havre",
    "AJ Auxerre": "Auxerre",
    "Angers SCO": "Angers",
    # Liga (Espagne) — ajoutée pour donner un historique domestique fiable
    # aux clubs qui jouent aussi la coupe d'Europe (voir config.LEAGUES).
    "Atletico Madrid": "Ath Madrid",
    "Athletic Bilbao": "Ath Bilbao",
    "Athletic Club": "Ath Bilbao",
    "Real Sociedad": "Sociedad",
    "Real Betis": "Betis",
    "Celta Vigo": "Celta",
    "RC Celta de Vigo": "Celta",
    "Rayo Vallecano": "Vallecano",
    "Deportivo Alaves": "Alaves",
    "RCD Espanyol": "Espanol",
    "Espanyol": "Espanol",
    "CD Leganes": "Leganes",
    "Real Valladolid": "Valladolid",
    "CA Osasuna": "Osasuna",
    "Getafe CF": "Getafe",
    "Girona FC": "Girona",
    "UD Las Palmas": "Las Palmas",
    "RCD Mallorca": "Mallorca",
    "Sevilla FC": "Sevilla",
    "Valencia CF": "Valencia",
    "Villarreal CF": "Villarreal",
    # Bundesliga (Allemagne)
    "Bayern München": "Bayern Munich",
    "FC Bayern Munich": "Bayern Munich",
    "Borussia Dortmund": "Dortmund",
    "Bayer Leverkusen": "Leverkusen",
    "Bayer 04 Leverkusen": "Leverkusen",
    "1. FC Union Berlin": "Union Berlin",
    "SC Freiburg": "Freiburg",
    "VfL Wolfsburg": "Wolfsburg",
    "1. FSV Mainz 05": "Mainz",
    "Mainz 05": "Mainz",
    "Borussia Monchengladbach": "M'gladbach",
    "Borussia Mönchengladbach": "M'gladbach",
    "TSG Hoffenheim": "Hoffenheim",
    "1899 Hoffenheim": "Hoffenheim",
    "FC Augsburg": "Augsburg",
    "VfB Stuttgart": "Stuttgart",
    "VfL Bochum": "Bochum",
    "1. FC Heidenheim": "Heidenheim",
    "FC St. Pauli": "St Pauli",
    "1. FC Koln": "FC Koln",
    "1. FC Köln": "FC Koln",
    # Serie A (Italie)
    "Inter Milan": "Inter",
    "FC Internazionale Milano": "Inter",
    "Internazionale": "Inter",
    "AC Milan": "Milan",
    "Hellas Verona": "Verona",
    "AS Roma": "Roma",
    "SS Lazio": "Lazio",
    "US Sassuolo": "Sassuolo",
    "Genoa CFC": "Genoa",
    "US Lecce": "Lecce",
    "AC Monza": "Monza",
    "Parma Calcio 1913": "Parma",
    "Cagliari Calcio": "Cagliari",
}


def normalize_team_name(name: str, known_names: list[str] | None = None) -> str:
    """Convertit un nom d'équipe The Odds API vers son équivalent
    football-data.co.uk. Si le nom n'est pas dans la table de correspondance,
    tente un rapprochement flou avec les noms connus de l'historique
    (`known_names`) ; sinon retourne le nom tel quel (le match sera
    probablement ignoré faute d'historique trouvé, plutôt que de planter)."""
    if name in ODDS_API_TO_FOOTBALL_DATA:
        return ODDS_API_TO_FOOTBALL_DATA[name]

    if known_names:
        matches = difflib.get_close_matches(name, known_names, n=1, cutoff=0.6)
        if matches:
            return matches[0]

    return name
