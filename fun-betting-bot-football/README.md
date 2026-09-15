# ⚽ fun-betting-bot-football

Bot de paris sportifs **100% virtuels**, sur le football, pour comprendre
comment fonctionne le value betting (EV, cotes, gestion de bankroll) en le
regardant tourner sur de vrais matchs à venir — **aucun argent réel n'est
jamais en jeu, aucun pari réel n'est jamais placé.**

C'est l'équivalent "paris sportifs" du bot de trading crypto du dossier
`trading_bot/` : même logique (un modèle prédit, une stratégie décide, une
bankroll virtuelle évolue), même infrastructure cloud gratuite.

## Ça tourne où ?

**Nulle part sur ta machine.** Ce bot est fait pour tourner en continu
dans le cloud, gratuitement, et se consulter depuis un navigateur (mobile
compris) :

- **Render** héberge le service qui règle les paris et en place de nouveaux.
- **Supabase** stocke la bankroll, l'historique des matchs, le modèle et le
  journal des paris (rien ne vit en mémoire locale).
- **GitHub Actions** ré-entraîne le modèle chaque semaine sur l'historique
  à jour.
- **UptimeRobot** garde le service éveillé et déclenche les cycles.

👉 **Voir [DEPLOIEMENT.md](DEPLOIEMENT.md) pour la mise en place complète,
étape par étape.**

## Comment ça marche

1. **Historique** : [Football-Data.co.uk](https://www.football-data.co.uk)
   fournit gratuitement, sans clé, les résultats et cotes de plusieurs
   bookmakers pour Premier League (E0), Ligue 1 (F1), Liga (SP1),
   Bundesliga (D1) et Serie A (I1), saisons 2018-19 à 2025-26
   (`src/data_loader.py`). Liga/Bundesliga/Serie A ajoutées pour donner un
   historique domestique fiable aux clubs qui jouent aussi la coupe
   d'Europe — étape préalable à toute tentative d'y parier un jour (voir
   "Limites connues").
2. **Features** (`src/features.py`) : forme récente de chaque équipe sur
   ses 5 derniers matchs (points, buts marqués/encaissés) + repos (jours
   depuis le match précédent), calculées sans fuite de données (uniquement
   les matchs *antérieurs*). Une équipe reléguée depuis longtemps ou tout
   juste promue (moins de `MIN_RECENT_MATCHES` matchs dans l'élite sur les
   `MIN_RECENT_MATCHES_WINDOW_DAYS` derniers jours) est exclue des matchs à
   venir — ses derniers matchs connus seraient trop vieux/rares pour
   représenter l'équipe actuelle (observé en prod : EV aberrantes à +200%
   sur des équipes comme Troyes ou Sunderland).
3. **Modèle de buts** (`src/model.py`) : deux régressions de Poisson
   (scikit-learn) qui prédisent le nombre de buts ATTENDU de chaque
   équipe. Toutes les probabilités de marché (1X2, double chance,
   over/under, résultat+buts) sont ensuite déduites d'une grille de
   Poisson jointe (`src/markets.py`) — un seul modèle cohérent plutôt
   qu'un classifieur par marché. Les probabilités du 1X2 sont en plus
   *calibrées* pour éviter la surconfiance (voir "Un incident, une
   correction" plus bas). Ré-entraîné chaque semaine par `retrain.py`.
4. **Marchés** (`src/markets.py`) : 1X2, double chance (1X/X2/12),
   over/under (0.5/1.5/2.5 buts) et résultat+buts (ex: "Domicile & +2.5
   buts"). Seuls le 1X2 et l'over/under ont une VRAIE cote de marché (The
   Odds API) — double chance et résultat+buts sont toujours des cotes
   ESTIMÉES (cote équitable du modèle avec une marge bookmaker), affichées
   pour comprendre gains/pertes mais jamais pariables : le bot ne parie
   jamais contre sa propre estimation faute de prix de marché indépendant.
5. **Stratégie** (`src/strategy.py`) : pour chaque match, calcule l'EV de
   chaque sélection à cote réelle (`EV = p × cote − 1`) et garde la
   meilleure. Les jambes dont l'EV dépasse 15% sont regroupées en
   **paris combinés jusqu'à 3 matchs** (jamais deux sélections du même
   match) ; les matchs restants dont l'EV dépasse 10% deviennent des
   paris seuls. Mise = fraction de Kelly réduite (15%, plafonnée à 3% de
   la bankroll par ticket).
6. **Cotes à venir** (`src/data_loader.py`) : via
   [The Odds API](https://the-odds-api.com) si une clé est configurée
   (free tier ~500 requêtes/mois, suivi du quota restant à chaque appel),
   sinon via `data/upcoming_matches_sample.json` (quelques matchs
   fictifs, pour que le bot tourne même sans clé). Seuls les matchs à
   J+`UPCOMING_MATCH_WINDOW_DAYS` (3 par défaut) sont évalués/pariés — un
   match plus lointain entre dans la fenêtre de lui-même à un prochain
   fetch, sans consommer inutilement le quota blessures/coupe d'Europe
   (voir section suivante) sur des matchs encore loin dans le temps.
7. **Web app** (`web_app.py`) : `/tick` règle les tickets dont tous les
   matchs sont finis et en place de nouveaux ; `/` affiche bankroll,
   derniers tickets, dernier backtest et journal — consultable au
   téléphone.

## Structure du projet

```
fun-betting-bot-football/
  src/
    config.py          constantes (ligues, saisons, seuils, throttle API)
    data_loader.py      téléchargement historique + cotes à venir/scores
    features.py          forme récente + repos des équipes
    team_names.py        correspondance des noms d'équipes entre sources
    model.py              modèle de buts (Poisson), entraînement + sérialisation
    markets.py             1X2/double chance/over-under/résultat+buts déduits du modèle
    strategy.py            EV, Kelly, sélection + combos jusqu'à 3 matchs
    simulation.py           backtest (utilisé par retrain.py)
    metrics.py                ROI, yield, win rate, drawdown, graphiques
    supabase_state.py          persistance (Supabase, REST)
  web_app.py            service Render : /tick, /, /alert-test
  alerts.py              notifications optionnelles (Discord/Slack/Telegram)
  retrain.py              entraînement hebdomadaire (GitHub Actions)
  render.yaml               config déploiement Render
  data/upcoming_matches_sample.json   matchs fictifs (mode sans clé API)
  notebooks/plots/            graphiques du dernier backtest (artifact CI)
```

## Ajouter d'autres ligues ou saisons

Dans `src/config.py` :

```python
LEAGUES = {
    "E0": "Premier League",
    "F1": "Ligue 1",
    "SP1": "Liga",
    "D1": "Bundesliga",
    "I1": "Serie A",
    "N1": "Eredivisie",   # code football-data.co.uk de la ligue à ajouter
}
```

Il faut aussi ajouter la clé de sport correspondante dans
`ODDS_API_SPORT_KEYS` (voir la liste complète sur `/v4/sports` de The Odds
API) et compléter `src/team_names.ODDS_API_TO_FOOTBALL_DATA` pour cette
ligue si les noms d'équipes diffèrent entre les deux sources. Pense aussi
à réduire les intervalles de throttle (`ODDS_FETCH_INTERVAL_HOURS`,
`SCORES_FETCH_INTERVAL_HOURS`) si le nombre de ligues augmente
significativement, pour rester dans le quota gratuit de l'API.

## Brancher The Odds API

Crée un compte gratuit sur [the-odds-api.com](https://the-odds-api.com),
récupère ta clé, et renseigne-la comme variable d'environnement
`ODDS_API_KEY` sur Render (voir DEPLOIEMENT.md). Sans clé, le bot reste
fonctionnel mais se limite aux matchs d'exemple et ne peut pas régler ses
paris automatiquement (il a besoin de l'endpoint `/scores` de la même API
pour connaître les résultats).

## Un incident, une correction

Le tout premier backtest (modèle non calibré, seuil EV à 5%) a perdu
**99,79% de la bankroll** : la régression logistique brute sortait des
probabilités bien trop tranchées sans en avoir l'information, et la
stratégie EV confondait ce bruit de modèle avec de la vraie valeur en
pariant sur 87% des matchs contre un marché pourtant efficient. Corrigé
par la calibration des probabilités (`CalibratedClassifierCV` à l'époque
de la régression logistique, conservée en philosophie avec le modèle de
Poisson actuel) + des seuils plus conservateurs + un coupe-circuit
(`DRAWDOWN_STOP_FRACTION`) qui arrête les nouveaux paris si la bankroll
tombe sous 20% du capital de départ. Aucun pari réel (même virtuel)
n'avait encore été placé au moment de la découverte.

## Blessures et coupe d'Europe — affichage uniquement

Si `API_FOOTBALL_KEY` et/ou `FOOTBALL_DATA_ORG_KEY` sont configurées
(`src/external_data.py`), le journal (`match_preview`) affiche en plus,
pour chaque match considéré, le nombre de blessés de chaque équipe
(API-Football) et si elle a joué une coupe d'Europe dans les 5 derniers
jours (football-data.org). **Ni l'une ni l'autre n'entraîne le modèle ni
n'influence automatiquement un pari** : il n'existe pas d'historique de
blessures ou de calendrier européen passé pour apprendre un coefficient
fiable là-dessus. C'est de l'information affichée pour que TU juges —
exactement l'esprit "j'y mettrai ma touche perso" du projet.

API-Football est utilisé pour les deux signaux (blessures ET détection
"a joué en coupe d'Europe" via `/fixtures?team=<id>&last=5`, plus fiable
qu'un rapprochement par nom puisque basé sur l'id d'équipe déjà résolu) —
conforme à sa doc officielle : vérification du champ `errors` à chaque
réponse, suivi du quota journalier (`x-ratelimit-requests-remaining`,
100/jour en free tier) avec arrêt automatique des appels sous le seuil,
et plafond d'appels par cycle (`API_FOOTBALL_MAX_CALLS_PER_CYCLE`, 6)
pour rester sous la limite de 10 requêtes/**minute** du free tier — la
dépasser de façon répétée peut bloquer la clé.
football-data.org sert de second avis indépendant pour la coupe d'Europe
(rapprochement par sous-chaîne, moins fiable, à confirmer une fois
déployé) — les IDs de compétition UEFA d'API-Football
(`config.API_FOOTBALL_UEFA_LEAGUE_IDS`) sont les IDs usuels mais non
vérifiés en direct non plus.

## Logos d'équipe — TheSportsDB

Chaque nom d'équipe affiché sur un ticket (page de statut, `/historique`)
est précédé de son logo si [TheSportsDB](https://www.thesportsdb.com/)
en trouve un (`src/external_data.fetch_team_logo_url`) — purement
cosmétique, aucun impact sur le modèle ou la stratégie. Gratuit, clé de
test publique par défaut (`config.SPORTSDB_API_KEY`, overridable via la
variable d'environnement `SPORTSDB_API_KEY` si elle devient invalide).
Résultat mis en cache définitivement dans `footballbot_team_refs.logo_url`
(un logo ne change jamais) — absence de logo trouvé = rien d'affiché,
jamais une icône d'image cassée.

## Limites connues
- **Backtest 1X2 uniquement** : football-data.co.uk ne fournit pas de
  cotes over/under historiques, donc le backtest hebdomadaire ne peut
  valider que le marché 1X2, même si le bot EN LIVE peut aussi parier sur
  les totals quand The Odds API fournit une vraie cote.
- **Double chance et résultat+buts sont toujours des cotes estimées**
  (pas un vrai marché chez The Odds API) : affichées pour comprendre,
  jamais pariables — voir section "Comment ça marche".
- Les mappings de noms d'équipes (`src/team_names.py`) sont construits à
  la main et n'ont pas pu être vérifiés contre les vraies API au moment de
  l'écriture — voir DEPLOIEMENT.md, section "Limites à connaître". Les
  ajouts Liga/Bundesliga/Serie A partagent la même réserve.
- Pas de données xG (non fournies par football-data.co.uk gratuitement) :
  les features se limitent à la forme récente (points, buts) et au repos.
- **Logos TheSportsDB pas garantis pour toutes les équipes** : la
  recherche se fait sur le nom football-data.co.uk tel quel (ex: "Man
  United", "Paris SG") — TheSportsDB ne les reconnaît pas toujours sous
  cette forme abrégée. Pas grave par conception (rien d'affiché plutôt
  qu'un logo faux), mais certaines équipes n'auront jamais de logo sans
  une table de correspondance dédiée (non faite pour l'instant).
- **Coupe d'Europe (C1/C3/Conference) pas encore pariable** : Liga/
  Bundesliga/Serie A ont été ajoutées comme étape préalable (historique
  domestique fiable pour les clubs européens), mais les matchs de coupe
  d'Europe eux-mêmes ne sont ni téléchargés ni pariés. L'historique
  spécifique à ces compétitions (API-Football, plan gratuit) n'est
  disponible que sur les saisons 2022 à 2024, contre 2018-2025 pour les
  championnats domestiques — vérifié en direct le 2026-09-14.

---

**Ce projet est un outil éducatif et ludique. Aucun pari réel n'est
effectué, aucun argent réel n'est en jeu.**
