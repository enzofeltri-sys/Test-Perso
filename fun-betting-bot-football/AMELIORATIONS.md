# Pistes d'amélioration

Liste vivante des trucs intéressants repérés en cours de route, pas encore
traités (ou traités partiellement), à ajuster au fur et à mesure plutôt que
tout corriger d'un coup. Chaque entrée : quand ça a été repéré, ce qu'on a
observé, l'hypothèse, et une piste de correctif si on décide de s'y mettre.

## Idées tirées d'une revue d'APIs/repos externes
*Ajouté le 2026-09-15, à partir d'un document de référence fourni par Enzo
(APIs football gratuites + repos GitHub de bots de paris).*

Vérification utile en passant : nos intégrations actuelles (API-Football,
football-data.org, The Odds API, football-data.co.uk) correspondent
exactement aux specs techniques du document (base URL, header d'auth,
rate limits) — pas d'erreur de config de notre côté à chercher là-dessus.
Ce qui en ressort de vraiment nouveau :

### Méthodologie de backtest : walk-forward plutôt qu'un split unique
Le repo `georgedouzas/sports-betting` (788⭐, toolbox Python dédiée aux
paris sportifs — dataloaders + bettors scikit-learn + CLI) fait un
backtest walk-forward (ré-entraînement glissant dans le temps), alors
que notre `retrain.py` fait un split train/test UNIQUE et figé
(`config.TEST_SEASON_START`). Un split unique peut donner un ROI backtest
qui dépend beaucoup de la saison de test choisie, plutôt qu'une estimation
robuste de la performance réelle du modèle.

**Piste** (moyen terme, pas urgent) : jeter un œil à leur approche pour
voir si un backtest glissant (réentraîner sur chaque saison N-1, tester
sur N, répéter) donnerait une image plus fiable que notre split actuel —
pourrait aussi éclairer le -31,81% de ROI résiduel (voir plus bas) : est-ce
structurel ou un artefact du découpage train/test ?

### TheSportsDB pour les logos d'équipes — ✅ fait et confirmé en prod le 2026-09-15
Ajouté (`src/external_data.fetch_team_logo_url`, affiché via
`web_app._team_logo_html` sur les tickets) : cache permanent dans
`footballbot_team_refs.logo_url`, rien d'affiché si pas trouvé (jamais
d'icône cassée). Deux vrais bugs trouvés et corrigés avant que ça marche
(voir "Historique des correctifs") : mauvais nom de champ (`strBadge` au
lieu de `strTeamBadge`) et pollution du cache sur un 429 temporaire.
Confirmé fonctionnel par Enzo.

Limite connue restante : la recherche se fait sur le nom
football-data.co.uk tel quel ("Man United"), pas toujours reconnu par
TheSportsDB — pas de table de correspondance dédiée pour l'instant, donc
certaines équipes n'auront jamais de logo. À enrichir si ça se voit trop
à l'usage.

### RapidOddsAPI comme source de cotes de secours
250 crédits gratuits, SDK Python officiel. Pas nécessaire tant que The
Odds API (500 crédits/mois) suffit, mais à garder en tête si on ajoute
encore des championnats et qu'on retombe sur un problème de quota comme
avec le throttle scores à 24h.

## Modèle

### Le modèle Poisson n'a aucune notion de championnat
*Repéré le 2026-09-15.*

Après l'ajout de Liga/Bundesliga/Serie A, le backtest s'est effondré
(-11,34% → -88,8% ROI). Une partie de la cause était un vrai bug (voir
"Historique des correctifs" ci-dessous), mais même après ce correctif le
ROI reste négatif (-31,81%).

Vérifié en base (`footballbot_matches`) : les 5 championnats ont des
profils de buts nettement différents —

| Championnat | Buts/match | % victoire dom. |
|---|---|---|
| Bundesliga (D1) | 3.16 | 43.5% |
| Premier League (E0) | 2.86 | 43.9% |
| Ligue 1 (F1) | 2.75 | 43.0% |
| Serie A (I1) | 2.72 | 41.0% |
| Liga (SP1) | 2.57 | 45.0% |

`src/features.py` (FEATURE_COLUMNS) n'inclut aucune colonne "championnat" —
le modèle apprend une seule relation forme→buts, appliquée uniformément
aux 5 championnats, alors que leur baseline de buts diffère de ~23% entre
le plus et le moins offensif. Hypothèse : ça biaise systématiquement les
probabilités, surtout gênant pour les combinés (l'erreur de calibration
se compose entre les jambes).

**Piste** : ajouter le championnat comme feature catégorielle (one-hot)
dans `src/model.py` (`ColumnTransformer` : `OneHotEncoder` pour la ligue +
`StandardScaler` pour le reste), et propager dans
`features.build_features_for_match` (passer la ligue du match à venir).
Retester le backtest avant/après pour confirmer l'effet.

## Historique / statistiques

### Historique buts/résultats par équipe (pas seulement par championnat)
*Repéré le 2026-09-15, demandé par Enzo.*

On a aujourd'hui la moyenne de buts et les % de victoire dom/ext par
CHAMPIONNAT (calculable à la demande via SQL sur `footballbot_matches`),
mais rien par équipe, et rien d'affiché en permanence sur la web app.

**Piste** : soit garder ça en requête ponctuelle (SQL sur demande), soit
ajouter une page `/stats` sur `web_app.py` avec un classement par équipe
(buts marqués/encaissés, % victoire dom/ext, forme récente) — à discuter
si Enzo veut que ce soit permanent ou juste consultable au coup par coup.

## Blessures / fatigue (API-Football)

### Le suivi des blessures ne fonctionne pas — ⏸️ abandonné, compte API-Football suspendu
*Repéré le 2026-09-15, cause confirmée et décision prise le 2026-09-16.*

Root cause finalement confirmée via `footballbot_errors` : le compte
API-Football d'Enzo est suspendu (`"Your account is suspended"`), pas
un bug de notre côté. Cause probable d'après le support API-Football :
Render (hébergement gratuit) utilise des IPs sortantes partagées entre
plein de comptes non-liés — le trafic abusif d'un AUTRE utilisateur
Render sur la même IP a pu déclencher leur protection anti-abus pour
tout le monde.

Deux solutions existent mais coûtent de l'argent réel sur un projet
100% gratuit/éducatif : IP dédiée Render (~$100/mois + nécessite un
workspace Pro) ou plan payant API-Football (à partir de $19/mois).
Vérifié qu'aucune alternative gratuite du document d'APIs fourni par
Enzo ne couvre les blessures pour nos 5 championnats (Sportmonks gratuit
= 2 championnats sans rapport ; TheSportsDB = métadonnées/logos, pas de
suivi de blessures ; les autres n'en proposent pas du tout).

**Décision (Enzo, 2026-09-16)** : on continue sans les blessures plutôt
que de payer pour une fonctionnalité purement informative (n'a jamais
influencé le modèle ni les paris). Rien à coder — `fetch_injury_count`
reste en place et retournera `None` tant que le compte n'est pas
réactivé, sans jamais bloquer un cycle (déjà conçu comme best-effort).

### La fatigue coupe d'Europe semble marcher, mais peut-être seulement via le fallback
*Repéré le 2026-09-15.*

Les messages "a joué en coupe d'Europe le ..." apparaissent bien dans le
journal, mais rien ne prouve que ça vient d'API-Football
(`fetch_recent_uefa_fixture`, essayé en premier) plutôt que du fallback
football-data.org (`played_in_europe_recently`). Si API-Football échoue
silencieusement pour la même raison que les blessures, cette fonctionnalité
tourne peut-être à 100% sur le second avis sans qu'on le sache.

**Piste** : une fois la vraie cause de l'échec blessures identifiée
(voir ci-dessus), vérifier si elle affecte aussi `fetch_recent_uefa_fixture`
(même fonction `_api_football_get` sous-jacente).

### Détecter l'absence des meilleurs joueurs — idée notée, pas prioritaire
*Proposé par Enzo le 2026-09-16, discuté, mis en attente.*

Idée : repérer quand un joueur clé (top buteur/passeur d'une équipe) est
absent (blessure, suspension, rotation) et en tenir compte dans le modèle,
au-delà du simple compteur de blessures déjà abandonné (voir ci-dessus).

Analyse faite avec Enzo : le gain attendu est probablement défensif plutôt
qu'offensif. Les bookmakers intègrent une absence confirmée dans leurs
cotes très vite, souvent avant même la sortie de la compo officielle — donc
l'info est rarement "nouvelle" pour le marché au moment où on parie. Le
bénéfice réel serait d'éviter que NOTRE modèle calcule une EV artificiellement
gonflée parce qu'il ignore l'absence alors que la cote, elle, l'a déjà
digérée (moins de faux positifs plutôt que de nouveaux paris gagnants).

Deux prérequis avant de pouvoir seulement tester ça :
1. Un score d'importance par joueur (calculable nous-mêmes : buts+passes
   sur la saison, déjà dans les données disponibles).
2. Une source fiable d'absences à temps — même blocage que les blessures
   (compte API-Football suspendu) si on veut l'info en avance ; sinon
   attendre les compos officielles (~1h avant le coup d'envoi) change la
   mécanique actuelle du bot (cotes/EV calculées plus tôt).

**Priorité** : jugée plus faible que le biais championnat manquant du
modèle Poisson (potentiellement lié aux -31,81% de ROI résiduel, voir plus
haut) — à reprendre après, pas maintenant.

## Données manquantes

### 3 clubs espagnols absents de l'historique
*Repéré le 2026-09-14/15.*

Deportivo La Coruña, Real Racing Club de Santander et Málaga n'apparaissent
sous aucune orthographe dans `footballbot_matches` — pas un bug de mapping
(`team_names.py`), une vraie absence : ces clubs jouent actuellement en
division inférieure espagnole, non couverte par le flux SP1 (Liga, D1) de
football-data.co.uk.

**Piste** : si ça devient gênant (beaucoup de matchs ignorés), regarder si
football-data.co.uk fournit un flux SP2 (Segunda División) à ajouter en
config — sinon laisser ces matchs se faire ignorer comme prévu
("Historique insuffisant").

## Throttle / infra

### Règlement des paris retardé jusqu'à 24h après la fin du match
*Repéré le 2026-09-14/15, pas un bug — trade-off déclibéré.*

`SCORES_FETCH_INTERVAL_HOURS=24` (relevé de 12h lors de l'ajout des 3
championnats, pour rester sous le quota The Odds API à 5 ligues). Effet de
bord : un match qui se termine juste après un fetch peut attendre jusqu'à
24h avant d'être réglé.

**Piste** : si le quota le permet un jour (moins de ligues suivies, ou
upgrade de plan), redescendre l'intervalle. Pour l'instant, laissé tel
quel — le quota est plus contraignant que le confort d'affichage.

---

## Historique des correctifs déjà appliqués (pour mémoire, pas des pistes ouvertes)

- **2026-09-15** : `strategy.build_tickets` ne vérifiait `config.MAX_SANE_EV`
  que jambe par jambe, jamais sur l'EV composée du ticket combiné — des
  combinés à EV délirante (+2901%) passaient. Corrigé : le filtre
  s'applique maintenant aussi à l'EV du ticket final. Backtest : ROI
  -88,8% → -31,81% après correctif.
- **2026-09-15** : `_api_football_get` avalait tout échec silencieusement
  (aucune trace de la vraie raison). Ajout d'un log dédupliqué par
  (endpoint, raison) dans `footballbot_errors`.
- **2026-09-15** : logos TheSportsDB — le champ cherché (`strTeamBadge`)
  n'existe pas dans la réponse actuelle de l'API, le vrai nom est
  `strBadge`. Diagnostic confirmé via `footballbot_errors` (Rayo Vallecano
  trouvée avec ses vraies métadonnées, champ absent). Corrigé, avec repli
  sur `strTeamBadge` par robustesse.
- **2026-09-15** : purger tout `footballbot_team_refs` d'un coup (bouton
  "vider le cache") a fait relancer une recherche TheSportsDB pour toutes
  les équipes d'une page en même temps, dépassant le rate limit (429).
  `fetch_team_logo_url` mettait ce 429 temporaire en cache comme "aucun
  logo" DÉFINITIVEMENT. Corrigé : un échec de l'appel (`data is None`) ne
  sauvegarde plus rien, pour permettre une nouvelle tentative plus tard.
